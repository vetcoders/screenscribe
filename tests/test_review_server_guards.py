"""Server guards for ``review_server`` (W2-B).

Closes the drift between the review + analyze twins:

- BH40 mirror: a marker deleted mid-VLM must not resurrect a ghost result.
- BH48 mirror: a failed / raised / degraded re-analysis must drop any stale
  completed result instead of leaving it under an error marker.
- Concurrent-analysis guard: a second analyze for an in-flight marker 409s and
  the VLM runs once.
- STT min-length parity: a sub-1KB browser recording is rejected the same way on
  both servers.
- STT response_id chaining: a real STT response_id advances the conversation
  chain the next manual-frame analysis reads (mirror of analyze BH46/BH49).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import create_review_app
from screenscribe.transcribe import Segment, TranscriptionResult

VALID_BROWSER_AUDIO = b"voice-data" + (b"x" * 2048)


@pytest.fixture
def review_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "screen_report.html"
    report_file.write_text("<html><body>report</body></html>", encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    # A base report.json so /api/save + /api/review-state have a document to load.
    (output_dir / "screen_report.json").write_text(
        json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8"
    )
    return output_dir, report_file, video_path


def _config() -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        llm_model="programmer",
        vision_model="programmer",
    )


def _jpeg_b64() -> str:
    """Base64 whose decoded bytes start with the JPEG magic (FF D8 FF)."""
    return "/9j/4GZha2UtanBlZy1kYXRh"


def _finding(
    *,
    summary: str = "ok",
    severity: str = "medium",
    response_id: str = "resp",
    **extra: Any,
) -> SimpleNamespace:
    fields: dict[str, Any] = {
        "category": "manual_capture",
        "severity": severity,
        "summary": summary,
        "issues_detected": [],
        "suggested_fix": "",
        "affected_components": [],
        "response_id": response_id,
        "is_issue": True,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _mark(client: TestClient) -> str:
    resp = client.post(
        "/api/manual-mark",
        json={"timestamp": 1.0, "frame_base64": _jpeg_b64(), "transcript": "t", "notes": ""},
    )
    assert resp.status_code == 200
    return str(resp.json()["marker_id"])


def _saved_results(json_path: Path) -> list[dict[str, Any]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return data.get("manual_review", {}).get("results", [])


# --- BH40 mirror: delete mid-VLM must not resurrect a ghost result -----------


def test_bh40_marker_deleted_during_vlm_is_not_persisted(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """Deleting a marker while its VLM call is in flight must NOT persist the
    returning result — neither in the session nor on disk."""
    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"

    started = threading.Event()
    release = threading.Event()

    def fake(*_a: Any, **_k: Any) -> SimpleNamespace:
        started.set()
        assert release.wait(timeout=5)
        return _finding(summary="ghost", severity="high")

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", fake)

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    marker_id = _mark(client)

    holder: dict[str, Any] = {}

    def run() -> None:
        holder["r"] = client.post(f"/api/manual-analyze/{marker_id}")

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(timeout=5)  # VLM in flight, marker is "analyzing"
        assert client.delete(f"/api/manual-mark/{marker_id}").status_code == 200
    finally:
        release.set()
        worker.join(timeout=5)

    # The analysis reports an error rather than resurrecting the ghost finding.
    assert holder["r"].json()["status"] == "error"

    # Gone from the session-backed review-state.
    state = client.get("/api/review-state").json()
    assert all(frame.get("marker_id") != marker_id for frame in state["manualFrames"])

    # Gone from disk: a save persists session results, which must not carry it.
    assert client.post("/api/save").status_code == 200
    assert _saved_results(json_path) == []


# --- Concurrent-analysis guard -----------------------------------------------


def test_concurrent_manual_analysis_returns_409(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """A second analyze for a marker whose VLM call is in flight must 409, invoke
    the VLM once, and leave exactly one completed result."""
    output_dir, report_file, video_path = review_workspace

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake(*_a: Any, **_k: Any) -> SimpleNamespace:
        calls["n"] += 1
        started.set()
        assert release.wait(timeout=5)
        return _finding(summary="only-run")

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", fake)

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    marker_id = _mark(client)

    holder: dict[str, Any] = {}

    def first() -> None:
        holder["r1"] = client.post(f"/api/manual-analyze/{marker_id}")

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert started.wait(timeout=5)  # first VLM call in flight
        r2 = client.post(f"/api/manual-analyze/{marker_id}")
        assert r2.status_code == 409
    finally:
        release.set()
        worker.join(timeout=5)

    assert holder["r1"].status_code == 200
    assert holder["r1"].json()["status"] == "completed"
    assert calls["n"] == 1  # VLM invoked exactly once, not twice


# --- BH48 mirror: failed / raised / degraded re-analysis clears stale result --


def test_failed_reanalysis_clears_stale_result(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """A re-analysis that returns no finding must drop the previous result."""
    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **k: _finding(summary="first"),
    )
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    marker_id = _mark(client)

    assert client.post(f"/api/manual-analyze/{marker_id}").json()["status"] == "completed"
    assert client.post("/api/save").status_code == 200
    assert _saved_results(json_path)[0]["summary"] == "first"

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified", lambda *a, **k: None
    )
    assert client.post(f"/api/manual-analyze/{marker_id}").json()["status"] == "error"
    assert client.post("/api/save").status_code == 200
    assert _saved_results(json_path) == []  # stale completed finding is gone


def test_reanalysis_exception_clears_stale_result(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """A re-analysis that raises must also drop the previous result."""
    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **k: _finding(summary="first"),
    )
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    marker_id = _mark(client)

    client.post(f"/api/manual-analyze/{marker_id}")
    client.post("/api/save")
    assert _saved_results(json_path)[0]["summary"] == "first"

    def boom(*_a: object, **_k: object) -> Any:
        raise RuntimeError("vlm exploded")

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", boom)
    assert client.post(f"/api/manual-analyze/{marker_id}").json()["status"] == "error"
    assert client.post("/api/save").status_code == 200
    assert _saved_results(json_path) == []


def test_degraded_reanalysis_marks_error_and_clears_result(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """A parse-fallback (confidence == "degraded") is treated as failure, not
    persisted as a trustworthy completed finding."""
    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **k: _finding(summary="first"),
    )
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    marker_id = _mark(client)

    client.post(f"/api/manual-analyze/{marker_id}")
    client.post("/api/save")
    assert _saved_results(json_path)[0]["summary"] == "first"

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **k: _finding(summary="degraded-junk", confidence="degraded"),
    )
    body = client.post(f"/api/manual-analyze/{marker_id}").json()
    assert body["status"] == "error"
    assert "degraded" in body["error"].lower()

    assert client.post("/api/save").status_code == 200
    assert _saved_results(json_path) == []


# --- STT min-length parity across both server twins --------------------------


def test_stt_short_recording_rejected_on_both_servers(
    monkeypatch: pytest.MonkeyPatch,
    review_workspace: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    """A sub-1KB browser recording is rejected identically on review + analyze,
    before any STT provider round-trip."""

    def fail(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("short recording must not reach the STT provider")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", fail)

    short = b"\x1aE\xdf\xa3"  # 4-byte webm header, well under the 1024-byte floor
    message = "Voice recording is too short. Hold to record longer."

    output_dir, report_file, video_path = review_workspace
    review = TestClient(create_review_app(output_dir, report_file.name, video_path, _config()))
    review_resp = review.post("/api/stt", files={"audio": ("a.webm", short, "audio/webm")})
    assert review_resp.status_code == 400
    assert review_resp.json()["detail"] == message

    analyze_video = tmp_path / "analyze_video.mp4"
    analyze_video.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    analyze = TestClient(create_analyze_app(analyze_video, _config()))
    analyze_resp = analyze.post("/api/stt", files={"audio": ("a.webm", short, "audio/webm")})
    assert analyze_resp.status_code == 400
    assert analyze_resp.json()["detail"] == message


# --- STT response_id chaining (mirror analyze BH46/BH49) ---------------------


def test_review_stt_advances_chain_read_by_next_analysis(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """A real STT response_id advances the shared conversation chain, so the next
    manual-frame analysis receives it as ``previous_response_id`` (twin parity)."""
    output_dir, report_file, video_path = review_workspace
    seen_previous: list[str] = []

    def fake_stt(*_a: Any, **_k: Any) -> TranscriptionResult:
        return TranscriptionResult(
            text="usable transcript here",
            segments=[
                Segment(id=1, start=0.0, end=1.0, text="usable transcript here", no_speech_prob=0.0)
            ],
            language="pl",
            response_id="stt_resp",
        )

    def fake_vlm(*_a: Any, **kwargs: Any) -> SimpleNamespace:
        seen_previous.append(kwargs.get("previous_response_id", ""))
        return _finding(summary="ok", response_id="vlm_resp")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", fake_stt)
    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", fake_vlm)

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    assert (
        client.post(
            "/api/stt", files={"audio": ("a.webm", VALID_BROWSER_AUDIO, "audio/webm")}
        ).status_code
        == 200
    )
    marker_id = _mark(client)
    client.post(f"/api/manual-analyze/{marker_id}")

    assert seen_previous[0] == "stt_resp"
