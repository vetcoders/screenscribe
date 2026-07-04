"""Regression tests for AI summary fallbacks and unified preflight behavior."""

from pathlib import Path
from types import TracebackType
from typing import Any, Literal

import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.summary_fallback import generate_detection_executive_summary
from screenscribe.transcribe import Segment
from screenscribe.unified_analysis import (
    UnifiedFinding,
    analyze_all_findings_unified,
    generate_unified_summary,
)


def _sample_detection(detection_id: int = 1, start: float = 12.5) -> Detection:
    return Detection(
        segment=Segment(
            id=detection_id,
            start=start,
            end=start + 2.5,
            text="Przycisk dalej nie działa poprawnie.",
        ),
        category="bug",
        keywords_found=["semantic:bug"],
        context="Użytkownik raportuje problem z przyciskiem na ekranie konfiguracji.",
    )


class _FakeSummaryResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Najważniejszym problemem jest niedziałający przycisk dalej. Ogólnie UX wymaga dopracowania przepływu konfiguracji.",
                        }
                    ],
                }
            ]
        }


class _FakeSummaryClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_FakeSummaryClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def post(self, *args: Any, **kwargs: Any) -> _FakeSummaryResponse:
        return _FakeSummaryResponse()


class _FailedUnifiedSummaryResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "output": [],
            "error": {"message": "Provider summary failed"},
        }


class _FailedUnifiedSummaryClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_FailedUnifiedSummaryClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        return False

    def post(self, *args: Any, **kwargs: Any) -> _FailedUnifiedSummaryResponse:
        return _FailedUnifiedSummaryResponse()


def _sample_unified_finding(summary: str, severity: str = "medium") -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=1,
        screenshot_path=None,
        timestamp=12.5,
        category="bug",
        is_issue=True,
        sentiment="problem",
        severity=severity,
        summary=summary,
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="resp_test",
    )


def test_generate_detection_executive_summary_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
    )
    monkeypatch.setattr("screenscribe.summary_fallback.httpx.Client", _FakeSummaryClient)

    summary = generate_detection_executive_summary([_sample_detection()], config)

    assert "niedziałający przycisk dalej" in summary


def test_generate_unified_summary_falls_back_to_local_summary_on_failed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        llm_endpoint="https://api.example.com/v1/responses",
        llm_model="test-model",
        language="pl",
    )
    monkeypatch.setattr("screenscribe.unified_analysis.httpx.Client", _FailedUnifiedSummaryClient)

    summary = generate_unified_summary(
        [
            _sample_unified_finding("Niespójne tłumaczenie kafelka.", severity="high"),
            _sample_unified_finding(
                "Brakuje właściwych materiałów referencyjnych.", severity="medium"
            ),
        ],
        config,
    )

    assert "Wykryto 2 problem(y)" in summary
    assert "Niespójne tłumaczenie kafelka" in summary


def test_analyze_all_findings_unified_fast_fails_when_preflight_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")
    detection = _sample_detection()
    config = ScreenScribeConfig(
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        lambda *args, **kwargs: None,
    )

    results = analyze_all_findings_unified([(detection, screenshot)], config)

    assert results == []


def test_analyze_all_findings_unified_continues_after_first_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    screenshots: list[tuple[Detection, Path]] = []
    for idx in range(2):
        screenshot = tmp_path / f"shot-{idx}.jpg"
        screenshot.write_bytes(b"fake-image")
        screenshots.append((_sample_detection(detection_id=idx + 1, start=12.5 + idx), screenshot))

    config = ScreenScribeConfig(
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    def fake_streaming(
        detection: Detection,
        screenshot_path: Path,
        config: ScreenScribeConfig,
        previous_response_id: str | None = None,
        **kwargs: Any,
    ) -> UnifiedFinding | None:
        del screenshot_path, config, previous_response_id, kwargs
        if detection.segment.id == 1:
            return None
        return UnifiedFinding(
            detection_id=detection.segment.id,
            screenshot_path=None,
            timestamp=detection.segment.start,
            category=detection.category,
            is_issue=True,
            sentiment="problem",
            severity="high",
            summary="Pozostałe screenshoty nadal przechodzą analizę.",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="resp_success",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        fake_streaming,
    )

    results = analyze_all_findings_unified(screenshots, config)

    assert len(results) == 1
    assert results[0].detection_id == 2


def test_analyze_all_findings_unified_keeps_successes_on_threshold_breach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH3: when failures exceed the threshold the genuine successes must be
    preserved (partial report), not discarded into a 100%-fallback []."""
    screenshots: list[tuple[Detection, Path]] = []
    for idx in range(3):
        screenshot = tmp_path / f"shot-{idx}.jpg"
        screenshot.write_bytes(b"fake-image")
        screenshots.append((_sample_detection(detection_id=idx + 1, start=12.5 + idx), screenshot))

    config = ScreenScribeConfig(
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    def fake_streaming(
        detection: Detection,
        screenshot_path: Path,
        config: ScreenScribeConfig,
        previous_response_id: str | None = None,
        **kwargs: Any,
    ) -> UnifiedFinding | None:
        del screenshot_path, config, previous_response_id, kwargs
        if detection.segment.id == 3:
            return UnifiedFinding(
                detection_id=detection.segment.id,
                screenshot_path=None,
                timestamp=detection.segment.start,
                category=detection.category,
                is_issue=True,
                sentiment="problem",
                severity="medium",
                summary="Udany wynik musi przetrwać przewagę porażek.",
                action_items=[],
                affected_components=[],
                suggested_fix="",
                ui_elements=[],
                issues_detected=[],
                accessibility_notes=[],
                design_feedback="",
                technical_observations="",
                response_id="resp_success",
            )
        return None

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        fake_streaming,
    )

    results = analyze_all_findings_unified(screenshots, config)

    assert len(results) == 1
    assert results[0].detection_id == 3


def test_analyze_all_findings_unified_falls_back_when_all_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With zero genuine successes the batch still falls back fully ([])."""
    screenshots: list[tuple[Detection, Path]] = []
    for idx in range(3):
        screenshot = tmp_path / f"shot-{idx}.jpg"
        screenshot.write_bytes(b"fake-image")
        screenshots.append((_sample_detection(detection_id=idx + 1, start=12.5 + idx), screenshot))

    config = ScreenScribeConfig(
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        lambda *args, **kwargs: None,
    )

    results = analyze_all_findings_unified(screenshots, config)

    assert results == []


def _degraded_finding(detection: Detection) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=detection.segment.id,
        screenshot_path=None,
        timestamp=detection.segment.start,
        category=detection.category,
        is_issue=True,
        sentiment="problem",
        severity="medium",
        summary="Raw unparseable output.",
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="resp_degraded",
        confidence="degraded",
        parsed_from_unstructured_output=True,
    )


def test_analyze_all_findings_unified_treats_degraded_as_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH27: a degraded/parse-error finding is truthy but is not a real VLM
    result. Two degraded + one genuine over three items is a 2/3 failure ratio
    (> 0.5 threshold), and only the genuine success is returned."""
    screenshots: list[tuple[Detection, Path]] = []
    for idx in range(3):
        screenshot = tmp_path / f"shot-{idx}.jpg"
        screenshot.write_bytes(b"fake-image")
        screenshots.append((_sample_detection(detection_id=idx + 1, start=12.5 + idx), screenshot))

    config = ScreenScribeConfig(
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    def fake_streaming(
        detection: Detection,
        screenshot_path: Path,
        config: ScreenScribeConfig,
        previous_response_id: str | None = None,
        **kwargs: Any,
    ) -> UnifiedFinding | None:
        del screenshot_path, config, previous_response_id, kwargs
        if detection.segment.id == 3:
            return UnifiedFinding(
                detection_id=detection.segment.id,
                screenshot_path=None,
                timestamp=detection.segment.start,
                category=detection.category,
                is_issue=True,
                sentiment="problem",
                severity="medium",
                summary="Genuine high-confidence finding.",
                action_items=[],
                affected_components=[],
                suggested_fix="",
                ui_elements=[],
                issues_detected=[],
                accessibility_notes=[],
                design_feedback="",
                technical_observations="",
                response_id="resp_success",
            )
        return _degraded_finding(detection)

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        fake_streaming,
    )

    results = analyze_all_findings_unified(screenshots, config)

    # Degraded findings are excluded; only the genuine success survives.
    assert [f.detection_id for f in results] == [3]


def test_analyze_all_findings_unified_single_degraded_aborts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH27 single-item path: a degraded probe is a 1/1 failure, not a success."""
    screenshot = tmp_path / "shot.jpg"
    screenshot.write_bytes(b"fake-image")
    detection = _sample_detection()
    config = ScreenScribeConfig(
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        lambda d, *args, **kwargs: _degraded_finding(d),
    )

    results = analyze_all_findings_unified([(detection, screenshot)], config)

    assert results == []


def test_analyze_all_findings_unified_pool_is_not_serialized_by_stagger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BH43: a worker must sleep only the time REMAINING until its scheduled
    start, never an absolute index-based delay. The pre-fix code slept
    ``(idx-1)*STAGGER_DELAY`` unconditionally, so a task already past its slot
    re-slept its full cumulative delay IN-SLOT, serializing the pool.

    Verified DETERMINISTICALLY, not by wall-clock: the original test asserted a
    tight ``elapsed`` bound, which flaked on loaded CI runners (macos-py3.12).
    Here the orchestrator's ``time`` is swapped for a fake clock so we observe
    the sleep DURATIONS the pool actually requests:

    * Behind schedule (now >> scheduled) — the fix sleeps NOTHING (``remaining``
      is negative); the bug would sleep the absolute index delay. This is the
      discriminator: a regression to absolute-index stagger re-grows these.
    * On schedule (now == submit_t0) — the fix still applies the submit-relative
      offsets, proving the stagger rate-limit is intact (guards against a
      regression that drops scheduling altogether).
    """
    import threading
    import types

    import screenscribe.unified.orchestrator as orch

    monkeypatch.setattr(orch, "STAGGER_DELAY", 0.05)
    n_tasks = 15  # >> MAX_WORKERS (5): enough tasks to clog slots in the buggy pool

    screenshots: list[tuple[Detection, Path]] = []
    for idx in range(n_tasks):
        screenshot = tmp_path / f"shot-{idx}.jpg"
        screenshot.write_bytes(b"img")
        screenshots.append((_sample_detection(detection_id=idx + 1, start=float(idx)), screenshot))

    config = ScreenScribeConfig(
        vision_api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://api.example.com/v1/responses",
        vision_model="test-model",
    )

    def fake_streaming(
        detection: Detection,
        screenshot_path: Path,
        config: ScreenScribeConfig,
        previous_response_id: str | None = None,
        **kwargs: Any,
    ) -> UnifiedFinding:
        del screenshot_path, config, previous_response_id, kwargs
        return UnifiedFinding(
            detection_id=detection.segment.id,
            screenshot_path=None,
            timestamp=detection.segment.start,
            category=detection.category,
            is_issue=True,
            sentiment="problem",
            severity="medium",
            summary="ok",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified_streaming",
        fake_streaming,
    )

    def run_with_clock(now_after_submit: float) -> list[float]:
        """Run the pool against a fake orchestrator clock; return requested sleeps.

        ``submit_t0`` is the FIRST ``monotonic()`` call (captured before any
        worker runs); every later call (the per-worker ``remaining`` check)
        returns ``now_after_submit``. ``sleep`` is recorded, never real.
        """
        slept: list[float] = []
        guard = threading.Lock()
        calls = {"n": 0}

        def fake_monotonic() -> float:
            with guard:
                calls["n"] += 1
                is_first = calls["n"] == 1
            return 0.0 if is_first else now_after_submit

        def fake_sleep(duration: float) -> None:
            with guard:
                slept.append(duration)

        monkeypatch.setattr(
            orch, "time", types.SimpleNamespace(monotonic=fake_monotonic, sleep=fake_sleep)
        )
        results = analyze_all_findings_unified(screenshots, config)
        assert len([r for r in results if r is not None]) == n_tasks
        return slept

    # Behind schedule: every worker runs after its slot, so the fix sleeps
    # NOTHING. A buggy absolute-index pool would re-sleep (idx-1)*STAGGER here —
    # that is exactly the in-slot serialization BH43 removed.
    behind = run_with_clock(now_after_submit=1_000_000.0)
    assert behind == [], f"pool re-slept while behind schedule (BH43 regression): {behind}"

    # On schedule: the fix still applies the submit-relative stagger, so the
    # rate-limit is intact (each requested sleep is a bounded schedule offset).
    on_time = run_with_clock(now_after_submit=0.0)
    assert on_time, "stagger scheduling was dropped entirely (no sleeps applied)"
    assert max(on_time) <= (n_tasks - 1) * orch.STAGGER_DELAY + 1e-6
