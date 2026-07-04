"""Cross-provider response chaining guard.

previous_response_id may only be chained when the vision and LLM endpoints
belong to the same provider. Screenscribe allows independent vision/LLM
endpoints, and a response id minted by one provider is meaningless (or an
error) when replayed against another.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection, Segment
from screenscribe.unified.finding import UnifiedFinding
from screenscribe.unified.orchestrator import analyze_all_findings_unified
from screenscribe.unified.wire import _build_unified_payload
from screenscribe.unified_analysis import analyze_finding_unified


def _detection() -> Detection:
    seg = Segment(id=1, start=10.0, end=20.0, text="the save button does nothing")
    return Detection(segment=seg, category="bug", keywords_found=["bug"], context="ctx")


# --- _build_unified_payload guard ---------------------------------------------


def test_payload_chains_when_same_provider() -> None:
    """Same-provider endpoints keep previous_response_id in the responses payload."""
    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="p",
        screenshot_path=None,
        previous_response_id="resp_abc",
        stream=False,
        same_provider=True,
    )
    assert payload["previous_response_id"] == "resp_abc"


def test_payload_skips_chaining_when_cross_provider() -> None:
    """Cross-provider endpoints drop previous_response_id entirely."""
    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="p",
        screenshot_path=None,
        previous_response_id="resp_abc",
        stream=False,
        same_provider=False,
    )
    assert "previous_response_id" not in payload


def test_payload_default_is_same_provider() -> None:
    """Omitting same_provider preserves the historical chaining behavior."""
    payload = _build_unified_payload(
        endpoint="https://example.test/v1/responses",
        model="m",
        prompt="p",
        screenshot_path=None,
        previous_response_id="resp_abc",
        stream=False,
    )
    assert payload["previous_response_id"] == "resp_abc"


# --- call-site wiring (analyze_finding_unified) --------------------------------


class _CapturingResponse:
    def __init__(self) -> None:
        self.text = json.dumps(
            {
                "id": "resp_new",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"is_issue": true, "severity": "low", "summary": "ok"}',
                            }
                        ],
                    }
                ],
            }
        )
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)


class _CapturingClient:
    """Records the JSON body of the single non-streaming POST."""

    captured: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _CapturingClient:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def post(self, *args: Any, **kwargs: Any) -> _CapturingResponse:
        _CapturingClient.captured.append(kwargs["json"])
        return _CapturingResponse()


def _config(*, vision: str, llm: str) -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="k",
        llm_api_key="k",
        vision_api_key="k",
        vision_endpoint=vision,
        llm_endpoint=llm,
    )


def test_callsite_skips_chaining_across_providers(monkeypatch) -> None:
    """When vision_endpoint != llm_endpoint, the posted payload has no chain id."""
    _CapturingClient.captured = []
    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _CapturingClient)
    cfg = _config(
        vision="https://vision.example/v1/responses",
        llm="https://llm.example/v1/responses",
    )
    analyze_finding_unified(
        _detection(),
        screenshot_path=None,
        config=cfg,
        previous_response_id="resp_prev",
        force_text_only=True,
    )
    assert _CapturingClient.captured, "no request captured"
    assert "previous_response_id" not in _CapturingClient.captured[0]


def test_callsite_chains_vision_to_vision_in_split_provider(monkeypatch, tmp_path) -> None:
    """Split-provider vision calls keep vision-to-vision chaining (finding F).

    In a split-provider setup (vision_endpoint != llm_endpoint) the orchestrator
    chains the vision conversation: the probe and every screenshot-backed finding
    hit the vision endpoint, so the previous_response_id replayed on a later
    vision call is vision-minted and valid. The previous behavior dropped it for
    EVERY vision request, breaking screenshot-to-screenshot chaining entirely.
    """
    _CapturingClient.captured = []
    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _CapturingClient)
    cfg = _config(
        vision="https://vision.example/v1/responses",
        llm="https://llm.example/v1/responses",
    )
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"\xff\xd8\xff\xe0jpegbytes")

    analyze_finding_unified(
        _detection(),
        screenshot_path=screenshot,
        config=cfg,
        previous_response_id="resp_vision_prev",
        force_text_only=False,
    )
    assert _CapturingClient.captured, "no request captured"
    assert _CapturingClient.captured[0]["previous_response_id"] == "resp_vision_prev"


def test_callsite_chains_within_same_provider(monkeypatch) -> None:
    """When vision_endpoint == llm_endpoint, chaining is preserved."""
    _CapturingClient.captured = []
    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _CapturingClient)
    same = "https://same.example/v1/responses"
    cfg = _config(vision=same, llm=same)
    analyze_finding_unified(
        _detection(),
        screenshot_path=None,
        config=cfg,
        previous_response_id="resp_prev",
        force_text_only=True,
    )
    assert _CapturingClient.captured, "no request captured"
    assert _CapturingClient.captured[0]["previous_response_id"] == "resp_prev"


# --- orchestrator seed handoff (M1) -------------------------------------------
#
# The semantic prefilter is ALWAYS an LLM call, so the response id the review
# pipeline forwards as the batch seed is LLM-minted. In a split-provider setup
# that id is cross-provider and must NOT be replayed on the FIRST vision probe
# (the residual edge of F: F made every vision call replayable, but the seed
# handed to the very first vision call is still LLM-minted). vision->vision
# chaining after the probe must keep working.


def _vision_finding(response_id: str) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=0,
        screenshot_path=None,
        timestamp=0.0,
        category="bug",
        is_issue=True,
        sentiment="problem",
        severity="low",
        summary="s",
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id=response_id,
    )


def _record_streaming(seen: list[str | None]):
    counter = {"n": 0}

    def _fake_stream(
        detection: Detection,
        screenshot_path: Any,
        config: ScreenScribeConfig,
        previous_response_id: str | None = None,
        on_reasoning: Any = None,
        on_content: Any = None,
        force_text_only: bool = False,
    ) -> UnifiedFinding:
        seen.append(previous_response_id)
        rid = f"vision_{counter['n']}"
        counter["n"] += 1
        return _vision_finding(rid)

    return _fake_stream


def test_orchestrator_drops_llm_seed_before_first_vision_probe(monkeypatch, tmp_path) -> None:
    """Split-provider: the LLM prefilter seed must not reach the first vision probe."""
    seen: list[str | None] = []
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        _record_streaming(seen),
    )
    cfg = _config(
        vision="https://vision.example/v1/responses",
        llm="https://llm.example/v1/responses",
    )
    s0 = tmp_path / "s0.jpg"
    s0.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    s1 = tmp_path / "s1.jpg"
    s1.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    screenshots = [(_detection(), s0), (_detection(), s1)]

    analyze_all_findings_unified(screenshots, cfg, previous_response_id="llm_prefilter_id")

    assert seen, "no analysis calls captured"
    assert seen[0] is None, "first split-provider vision probe must not receive the LLM seed"
    # vision->vision chaining after the probe still replays the vision-minted id.
    assert seen[1] == "vision_0"


def test_orchestrator_keeps_seed_on_probe_in_same_provider(monkeypatch, tmp_path) -> None:
    """Same-provider: the seed is replayable, so the probe keeps receiving it."""
    seen: list[str | None] = []
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        _record_streaming(seen),
    )
    same = "https://same.example/v1/responses"
    cfg = _config(vision=same, llm=same)
    s0 = tmp_path / "s0.jpg"
    s0.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    s1 = tmp_path / "s1.jpg"
    s1.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    screenshots = [(_detection(), s0), (_detection(), s1)]

    analyze_all_findings_unified(screenshots, cfg, previous_response_id="seed_id")

    assert seen, "no analysis calls captured"
    assert seen[0] == "seed_id", "same-provider probe must keep the historical seed"
