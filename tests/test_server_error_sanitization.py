"""C5.2 — API error responses must not leak raw exception detail.

Every sanitized error path returns a stable ``error_code`` + a generic,
user-safe message; the full exception (type, args, traceback, upstream
STT/LLM bodies, filesystem paths) goes to the server log only — never to
the browser.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import create_review_app
from screenscribe.server_common import sanitized_error

# Marker text smuggled into raised exceptions; must never reach a client body.
# Intentionally path-free (no /Users//home/ etc.) so it does not trip the
# repo leak-scan while still standing in for sensitive internal detail.
LEAK_MARKER = "LEAKSENTINEL_internal_detail_must_stay_in_logs"

PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config(language: str = "en") -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        language=language,
    )


def _mark_one(client: TestClient) -> str:
    response = client.post(
        "/api/mark",
        json={
            "timestamp": 5.0,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": "broken button",
            "notes": "",
        },
    )
    assert response.status_code == 200
    return str(response.json()["marker_id"])


# --------------------------------------------------------------------------- #
# A1 / A7 — helper contract
# --------------------------------------------------------------------------- #
def test_sanitized_error_strips_detail_and_logs_full(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        result = sanitized_error(Exception(LEAK_MARKER), "unit_test_code")

    assert set(result.keys()) == {"error_code", "message"}
    assert result["error_code"] == "unit_test_code"
    assert LEAK_MARKER not in result["message"]
    assert "LEAKSENTINEL" not in result["message"]
    # Full detail is preserved in the log for operators.
    assert LEAK_MARKER in caplog.text


# --------------------------------------------------------------------------- #
# A2 — single-marker analyze path
# --------------------------------------------------------------------------- #
def test_analyze_single_marker_error_omits_raw_exception(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError(LEAK_MARKER)

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", boom)

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    response = client.post(f"/api/analyze/{marker_id}")
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "error"
    assert LEAK_MARKER not in json.dumps(body)
    assert body.get("error_code") == "marker_analysis_failed"
    assert "error" in body and LEAK_MARKER not in body["error"]


# --------------------------------------------------------------------------- #
# A3 — batch finalize per-marker error
# --------------------------------------------------------------------------- #
def test_finalize_batch_marker_error_omits_raw_exception(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError(LEAK_MARKER)

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", boom)

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    _mark_one(client)

    response = client.post("/api/finalize")
    assert response.status_code == 200
    raw = response.text
    assert LEAK_MARKER not in raw

    summary = response.json()["analysis"]
    assert summary["errors"] >= 1
    for outcome in summary["results"]:
        if outcome.get("status") == "error":
            assert outcome.get("error_code") == "marker_analysis_failed"
            assert LEAK_MARKER not in json.dumps(outcome)


# --------------------------------------------------------------------------- #
# A4 — finalize job error chain (:707 source -> :1006 sink)
# --------------------------------------------------------------------------- #
def test_finalize_job_error_omits_raw_exception(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_finding = SimpleNamespace(
        category="x",
        severity="high",
        summary="ok",
        issues_detected=[],
        suggested_fix="",
        affected_components=[],
        response_id="resp",
        is_issue=True,
    )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **k: fake_finding,
    )
    # Force an unexpected failure inside run_finalize_job's try block, after the
    # per-marker analysis loop, so the :707 source path runs.
    monkeypatch.setattr(
        "screenscribe.analyze_server.from_analyze_marker",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(LEAK_MARKER)),
    )

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    _mark_one(client)

    with caplog.at_level(logging.ERROR):
        start = client.post("/api/finalize/start")
        assert start.status_code == 200
        job_id = start.json()["job_id"]

        deadline = time.time() + 10.0
        status_body: dict[str, Any] = {}
        while time.time() < deadline:
            status_body = client.get(f"/api/finalize/status/{job_id}").json()
            if status_body["status"] in {"completed", "error"}:
                break
            time.sleep(0.05)

    assert status_body["status"] == "error"
    # Status poll (serialize_finalize_job) must not leak the raw exception.
    assert LEAK_MARKER not in json.dumps(status_body)
    assert status_body["last_error"] and LEAK_MARKER not in status_body["last_error"]

    # Result endpoint 500 inherits the generic last_error.
    result = client.get(f"/api/finalize/result/{job_id}")
    assert result.status_code == 500
    assert LEAK_MARKER not in result.text

    # Full detail preserved in the log.
    assert LEAK_MARKER in caplog.text


# --------------------------------------------------------------------------- #
# A5 — markdown report 500
# --------------------------------------------------------------------------- #
def test_markdown_report_error_omits_raw_exception(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError(LEAK_MARKER)

    monkeypatch.setattr("screenscribe.report.save_enhanced_markdown_report", boom)

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/api/report/markdown")
    assert response.status_code == 500
    assert LEAK_MARKER not in response.text


# --------------------------------------------------------------------------- #
# A6 — review server mirror
# --------------------------------------------------------------------------- #
def test_review_marker_error_omits_raw_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "screen_report.html"
    report_file.write_text("<html><body>report</body></html>", encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError(LEAK_MARKER)

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", boom)

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    mark = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 12.5,
            "frame_base64": "/9j/4GZha2UtanBlZy1kYXRh",
            "transcript": "t",
            "notes": "n",
        },
    )
    marker_id = mark.json()["marker_id"]

    response = client.post(f"/api/manual-analyze/{marker_id}")
    body = response.json()
    assert body["status"] == "error"
    assert LEAK_MARKER not in json.dumps(body)
    assert body.get("error_code") == "marker_analysis_failed"


# --------------------------------------------------------------------------- #
# A8 — /api/mark validation 400 (C5.2 residue): the client receives only the
# authored, cause-free validation message, never ``str(exc)``; the full decoder
# diagnostic (chained binascii error) stays in the server log.
# --------------------------------------------------------------------------- #
def test_mark_frame_bad_base64_omits_raw_decoder_detail(
    sample_video: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/api/mark",
            json={
                "timestamp": 5.0,
                "frame_base64": "@@not-valid-base64@@",
                "transcript": "x",
                "notes": "",
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    # Client sees only the authored, cause-free validation message.
    assert detail == "frame_base64 must be valid base64"
    # The raw decoder diagnostic (str(exc) / chained binascii error) must not leak.
    assert "is not valid base64:" not in detail
    assert "base64-encoded" not in detail.lower()
    # The rejected frame must not be persisted.
    assert client.get("/api/markers").json() == []
    # Full diagnostic preserved server-side for operators.
    assert "is not valid base64:" in caplog.text


# --------------------------------------------------------------------------- #
# A9 — /api/mark non-image 400: the decoded byte prefix (attacker-controlled
# payload) is logged for diagnosis but never echoed back to the client.
# --------------------------------------------------------------------------- #
def test_mark_frame_non_image_omits_decoded_bytes_prefix(
    sample_video: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import base64 as _b64

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    payload = _b64.b64encode(b"NOTIMAGE_sensitive_payload_bytes").decode()

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/api/mark",
            json={
                "timestamp": 5.0,
                "frame_base64": payload,
                "transcript": "x",
                "notes": "",
            },
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == "frame_base64 must decode to a JPEG or PNG image"
    # Neither the diagnostic wrapper nor the decoded payload bytes may leak.
    assert "prefix=" not in detail
    assert "NOTIMAGE" not in detail
    assert client.get("/api/markers").json() == []
    # Diagnostic (incl. decoded byte prefix) preserved server-side.
    assert "prefix=" in caplog.text
