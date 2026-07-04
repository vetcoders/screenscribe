"""Keywords-as-hints reach the analyze server's per-marker analysis prompt.

CUT 3 contract: in the analyze flow the active keyword vocabulary (loaded from
``--keywords-file`` / global file / built-in default into ``config.keywords``)
must flow through ``create_analyze_app``'s session/config into the per-marker
unified-analysis prompt. Keywords here do NOT find the moment (the human marks
it) -- they only help interpret the user's comment/voice note. An empty
dictionary is a safe no-op.

These tests drive the real ``analyze_finding_unified`` through the
``POST /api/analyze/{id}`` endpoint and capture the prompt actually sent to the
model (by stubbing ``_build_unified_payload``), so they prove the hint text is
present in the prompt rather than asserting on an intermediate stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.keywords import KeywordsConfig

# 1x1 PNG, matching the other analyze-server tests.
PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

BUG_PHRASE = "klikam i nic"
UI_PHRASE = "potworek"


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config(keywords: KeywordsConfig | None) -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        language="en",
        keywords=keywords,
    )


def _mark_one(client: TestClient) -> str:
    response = client.post(
        "/api/mark",
        json={
            "timestamp": 5.0,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": "the save button klikam i nic",
            "notes": "looks like a potworek",
        },
    )
    assert response.status_code == 200
    return str(response.json()["marker_id"])


class _CapturingResponse:
    """Minimal stand-in for the model HTTP response used by analyze_finding_unified."""

    status_code = 200

    def __init__(self) -> None:
        self.text = '{"is_issue": true, "severity": "medium", "summary": "ok"}'

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "id": "resp_test",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": self.text,
                        }
                    ],
                }
            ],
        }


def _capture_prompt(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the payload builder + HTTP client so the real prompt is captured.

    Returns a list that will hold each prompt string passed to
    ``_build_unified_payload``.
    """
    captured: list[str] = []

    real_build = None
    from screenscribe.unified import analyze_one

    real_build = analyze_one._build_unified_payload

    def spy_build(*args: Any, **kwargs: Any) -> dict[str, object]:
        captured.append(str(kwargs["prompt"]))
        return real_build(*args, **kwargs)

    monkeypatch.setattr(analyze_one, "_build_unified_payload", spy_build)

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

        def post(self, *args: Any, **kwargs: Any) -> _CapturingResponse:
            return _CapturingResponse()

    monkeypatch.setattr(analyze_one.httpx, "Client", _FakeClient)
    return captured


def test_active_keywords_reach_marker_analysis_prompt(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When config carries keywords, the per-marker prompt contains the hint."""
    captured = _capture_prompt(monkeypatch)
    keywords = KeywordsConfig(bug=[BUG_PHRASE], ui=[UI_PHRASE])
    app = create_analyze_app(sample_video, _config(keywords))
    client = TestClient(app)

    marker_id = _mark_one(client)
    response = client.post(f"/api/analyze/{marker_id}")
    assert response.status_code == 200

    assert captured, "expected the analyze path to build at least one prompt"
    prompt = captured[0]
    # The hint header + per-category phrase lines are injected as vocabulary hints.
    # Assert on the formatted category lines (which only the hint block emits) so
    # the bare phrases also occurring in the transcript can't pass the test alone.
    assert "Treat them as hints" in prompt
    assert f'- bug: "{BUG_PHRASE}"' in prompt
    assert f'- ui: "{UI_PHRASE}"' in prompt


def test_empty_keywords_are_a_noop_in_marker_analysis(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty dictionary injects no hint text (safe-when-empty)."""
    captured = _capture_prompt(monkeypatch)
    app = create_analyze_app(sample_video, _config(KeywordsConfig()))
    client = TestClient(app)

    marker_id = _mark_one(client)
    response = client.post(f"/api/analyze/{marker_id}")
    assert response.status_code == 200

    assert captured
    prompt = captured[0]
    # The vocabulary-hints header is the unambiguous signal that a hint block was
    # injected; the bare phrases also occur naturally in the marker transcript, so
    # we assert on the header (and its category list lines) instead.
    assert "Treat them as hints" not in prompt
    assert "signals for problem types" not in prompt


def test_keywords_survive_language_override_replace(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-call ``dataclasses.replace`` for the lang toggle keeps keywords.

    ``analyze_single_marker`` rebuilds the config with ``dataclasses.replace`` to
    apply the dashboard language override; the active vocabulary must survive that
    copy so the hint still reaches the prompt when a ``{"lang": ...}`` body is set.
    """
    captured = _capture_prompt(monkeypatch)
    keywords = KeywordsConfig(bug=[BUG_PHRASE])
    app = create_analyze_app(sample_video, _config(keywords))
    client = TestClient(app)

    marker_id = _mark_one(client)
    response = client.post(f"/api/analyze/{marker_id}", json={"lang": "pl"})
    assert response.status_code == 200

    assert captured
    assert f'- bug: "{BUG_PHRASE}"' in captured[0]
