"""Mocked analyze-server + finalize-flow tests that run in the DEFAULT CI run.

Context (W1B-01/05): the equivalent assertions historically lived in
``tests/test_integration.py::TestAnalyzeServer``, gated behind the
``integration`` marker AND a ``config_with_api`` fixture that calls
``pytest.skip`` unless a real Libraxis API key is configured. None of these
behaviours actually touch the network:

* ``create_analyze_app`` only constructs a FastAPI app + in-memory session.
* ``GET /``, ``GET /api/markers``, ``POST /api/mark`` are pure local handlers.
* the finalize flow's only outbound call is ``analyze_finding_unified``, which
  is monkeypatched here.

So they are mocked integration tests masquerading as live ones. This module
re-homes them as ordinary unit tests built on a synthetic config (dummy key,
no ``.load()``, no skip), so they execute on every ``pytest`` invocation
without ``--run-integration`` and pin the local server contract.

The stale ``test_analyze_page_has_theme_support`` assertion (which probed for
``--bg`` / ``--background`` / ``prefers-color-scheme`` markers no longer present
in the rendered page) is NOT ported verbatim; instead a theme test pins the
theming convention the page actually ships today (``:root`` + ``--ss-*`` /
``--color*`` / ``--surface*`` CSS custom properties).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.unified_analysis import UnifiedFinding

# 1x1 transparent PNG: a valid base64 frame the /api/mark validator accepts.
PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _config(language: str = "en") -> ScreenScribeConfig:
    """Synthetic config: a dummy key so handlers run, no network, no skip."""
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        language=language,
    )


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    """Minimal valid-enough MP4 (ftyp box) the analyze app accepts as input."""
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


# ---------------------------------------------------------------------------
# App construction + index page contract
# ---------------------------------------------------------------------------


def test_analyze_app_creates(sample_video: Path) -> None:
    """create_analyze_app returns a titled FastAPI app without any API call."""
    app = create_analyze_app(sample_video, _config())

    assert app is not None
    assert app.title == "Screenscribe Analyze"


def test_analyze_index_returns_html(sample_video: Path) -> None:
    """GET / returns a 200 HTML page branded for the analyze surface."""
    client = TestClient(create_analyze_app(sample_video, _config()))

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Screenscribe Analyze" in response.text


def test_analyze_page_respects_language_setting(sample_video: Path) -> None:
    """The rendered <html lang> attribute follows config.language (pl vs en)."""
    pl_client = TestClient(create_analyze_app(sample_video, _config(language="pl")))
    assert 'lang="pl"' in pl_client.get("/").text

    en_client = TestClient(create_analyze_app(sample_video, _config(language="en")))
    assert 'lang="en"' in en_client.get("/").text


def test_analyze_page_has_ui_controls(sample_video: Path) -> None:
    """The page exposes the Mark Frame and Record affordances."""
    html = TestClient(create_analyze_app(sample_video, _config())).get("/").text

    assert "Mark Frame" in html or "markFrame" in html
    assert "Record" in html or "record" in html.lower()


def test_analyze_page_has_video_player(sample_video: Path) -> None:
    """The page embeds a <video> element with the videoPlayer id the JS binds."""
    html = TestClient(create_analyze_app(sample_video, _config())).get("/").text

    assert "<video" in html
    assert 'id="videoPlayer"' in html or 'id="video"' in html


def test_analyze_page_has_mic_button(sample_video: Path) -> None:
    """The page ships a microphone/voice-record affordance."""
    html = TestClient(create_analyze_app(sample_video, _config())).get("/").text

    assert "mic" in html.lower() or "microphone" in html.lower() or "record" in html.lower()


def test_analyze_page_has_theme_support(sample_video: Path) -> None:
    """The page declares CSS custom properties under :root for theming.

    The historical assertion probed for ``--bg`` / ``--background`` /
    ``prefers-color-scheme`` tokens which the current page no longer emits;
    this pins the theming convention the page actually ships: a ``:root``
    block with the ``--ss-*`` / ``--color*`` / ``--surface*`` design tokens.
    """
    html = TestClient(create_analyze_app(sample_video, _config())).get("/").text

    assert ":root" in html
    assert "--ss-" in html or "--color" in html or "--surface" in html


def test_analyze_page_has_voicerecorder_js(sample_video: Path) -> None:
    """The page wires the VoiceRecorder class onto the MediaRecorder API."""
    html = TestClient(create_analyze_app(sample_video, _config())).get("/").text

    assert "VoiceRecorder" in html
    assert "MediaRecorder" in html


def test_analyze_page_has_finalize_trigger(sample_video: Path) -> None:
    """The page wires the finalize button to the async start/status endpoints."""
    html = TestClient(create_analyze_app(sample_video, _config())).get("/").text

    assert 'id="finalizeBtn"' in html
    assert "fetch('/api/finalize/start'" in html
    assert "fetch('/api/finalize/status/' + jobId)" in html


# ---------------------------------------------------------------------------
# Marker CRUD contract
# ---------------------------------------------------------------------------


def test_analyze_markers_empty_initially(sample_video: Path) -> None:
    """A fresh session reports an empty marker list."""
    client = TestClient(create_analyze_app(sample_video, _config()))

    response = client.get("/api/markers")

    assert response.status_code == 200
    assert response.json() == []


def test_analyze_mark_frame(sample_video: Path) -> None:
    """POST /api/mark creates a pending marker that then shows in /api/markers."""
    client = TestClient(create_analyze_app(sample_video, _config()))

    response = client.post(
        "/api/mark",
        json={
            "timestamp": 5.0,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": "Test transcript",
            "notes": "Test notes",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert "marker_id" in data
    assert data["status"] == "pending"

    markers = client.get("/api/markers").json()
    assert len(markers) == 1
    assert markers[0]["timestamp"] == 5.0


# ---------------------------------------------------------------------------
# Finalize flow (analyze_finding_unified monkeypatched — no real VLM/LLM call)
# ---------------------------------------------------------------------------


def _stub_finding(*, severity: str, response_id: str) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=1,
        screenshot_path=None,
        timestamp=5.0,
        category="ui",
        is_issue=True,
        sentiment="problem",
        severity=severity,
        summary="Mock summary",
        action_items=["Mock action"],
        affected_components=["Capture controls"],
        suggested_fix="Mock fix",
        ui_elements=["button"],
        issues_detected=["alignment"],
        accessibility_notes=[],
        design_feedback="ok",
        technical_observations="ok",
        response_id=response_id,
    )


def test_finalize_analyzes_all_markers_and_returns_export(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/finalize analyzes every pending marker and returns the export.

    With analyze_finding_unified stubbed, the synchronous finalize endpoint must
    process all markers, report zero errors, mark them completed, and embed an
    analysis per marker in the export payload.
    """

    def fake_analyze_finding_unified(*args: object, **kwargs: object) -> UnifiedFinding:
        return _stub_finding(severity="high", response_id="resp_mock_1")

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified", fake_analyze_finding_unified
    )

    client = TestClient(create_analyze_app(sample_video, _config()))

    for timestamp in (5.0, 9.0):
        response = client.post(
            "/api/mark",
            json={
                "timestamp": timestamp,
                "frame_base64": PNG_1X1_BASE64,
                "transcript": f"Marker {timestamp}",
                "notes": "Test note",
            },
        )
        assert response.status_code == 200

    finalize_response = client.post("/api/finalize")
    assert finalize_response.status_code == 200
    payload = finalize_response.json()

    assert payload["analysis"]["processed"] == 2
    assert payload["analysis"]["completed"] == 2
    assert payload["analysis"]["errors"] == 0

    assert len(payload["markers"]) == 2
    assert all(marker["status"] == "completed" for marker in payload["markers"])

    exported = payload["export"]
    assert "video" in exported
    assert len(exported["work_items"]) == 2
    assert all("analysis" in item for item in exported["work_items"])


def test_finalize_async_job_status_and_result(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async finalize endpoints expose x/y progress and a final export.

    /api/finalize/start returns a job id, /api/finalize/status/<id> moves from
    running to completed with processed==completed==marker count and zero
    errors, and /api/finalize/result/<id> returns the export for every marker.
    """

    def fake_analyze_finding_unified(*args: object, **kwargs: object) -> UnifiedFinding:
        return _stub_finding(severity="medium", response_id="resp_mock_async")

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified", fake_analyze_finding_unified
    )

    client = TestClient(create_analyze_app(sample_video, _config()))

    for timestamp in (2.0, 4.0, 6.0):
        response = client.post(
            "/api/mark",
            json={
                "timestamp": timestamp,
                "frame_base64": PNG_1X1_BASE64,
                "transcript": f"Marker {timestamp}",
                "notes": "Async note",
            },
        )
        assert response.status_code == 200

    start_response = client.post("/api/finalize/start")
    assert start_response.status_code == 200
    start_payload = start_response.json()
    assert "job_id" in start_payload
    job_id = start_payload["job_id"]

    status_payload = start_payload
    for _ in range(100):
        status_response = client.get(f"/api/finalize/status/{job_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        if status_payload["status"] != "running":
            break
        time.sleep(0.01)

    assert status_payload["status"] == "completed"
    assert status_payload["processed"] == 3
    assert status_payload["completed"] == 3
    assert status_payload["errors"] == 0

    result_response = client.get(f"/api/finalize/result/{job_id}")
    assert result_response.status_code == 200
    result_payload = result_response.json()
    assert result_payload["analysis"]["processed"] == 3
    assert len(result_payload["export"]["work_items"]) == 3
