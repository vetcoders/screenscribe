"""Resilience tests for the analyze server (bucket G7).

Invariant: "the analyze server does not fall over under real use" -- deleted
markers don't kill the batch, blocking work is off the event loop, no stuck
markers, honest status, no response_id chain corruption.

Each test maps to a bug-hunt finding:
- BH12: a marker deleted mid-finalize must not abort the whole batch.
- BH58: a corrupt frame payload must not leave the marker stuck "analyzing".
- BH48: a degraded (parse-fallback) finding must be reported as failure.
- BH29: an empty VLM response_id must not clobber the chain head.
- BH40: a marker deleted while its VLM call is in flight must not resurrect.
- BH46/BH49: an empty STT response_id must not clobber the chain head.
- P3-2: an orphan "analyzing" marker must be retried by finalize.
- P3-5: a finalize background thread must survive a vanished job id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig

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


def _mark_one(client: TestClient, *, notes: str = "", transcript: str = "x") -> str:
    response = client.post(
        "/api/mark",
        json={
            "timestamp": 5.0,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": transcript,
            "notes": notes,
        },
    )
    assert response.status_code == 200
    return str(response.json()["marker_id"])


def _finding(
    screenshot_path: Path | None,
    *,
    summary: str = "ok",
    response_id: str = "resp",
    confidence: str = "high",
) -> Any:
    from screenscribe.unified_analysis import UnifiedFinding

    return UnifiedFinding(
        detection_id=0,
        screenshot_path=screenshot_path,
        timestamp=0.0,
        category="ui",
        is_issue=True,
        sentiment="problem",
        severity="medium",
        summary=summary,
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id=response_id,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# BH48 - degraded finding is a failure, not a "completed" result.
# ---------------------------------------------------------------------------


def test_degraded_finding_is_reported_as_error(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **kw: _finding(None, confidence="degraded"),
    )

    body = client.post(f"/api/analyze/{marker_id}").json()
    assert body["status"] == "error"

    marker = client.get("/api/markers").json()[0]
    assert marker["status"] == "error"
    assert "result" not in marker  # no falsely-completed finding written


# ---------------------------------------------------------------------------
# BH29 - empty VLM response_id must not clobber the chain head.
# ---------------------------------------------------------------------------


def test_empty_vlm_response_id_does_not_clobber_chain(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    first = _mark_one(client)
    second = _mark_one(client)

    # First analysis returns a real id -> becomes the chain head.
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **kw: _finding(None, response_id="real-head"),
    )
    assert client.post(f"/api/analyze/{first}").json()["status"] == "completed"

    # Second analysis returns an EMPTY id and must NOT overwrite the head; it
    # should also receive the real head as previous_response_id.
    seen: dict[str, Any] = {}

    def capture(*_: object, previous_response_id: str | None = None, **__: object) -> Any:
        seen["previous"] = previous_response_id
        return _finding(None, response_id="")

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", capture)
    assert client.post(f"/api/analyze/{second}").json()["status"] == "completed"
    assert seen["previous"] == "real-head"

    # A third call must still see the preserved real head, not a blank.
    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", capture)
    client.post(f"/api/analyze/{first}")
    assert seen["previous"] == "real-head"


# ---------------------------------------------------------------------------
# BH46/BH49 - empty STT response_id must not clobber the chain head.
# ---------------------------------------------------------------------------


def test_empty_stt_response_id_does_not_clobber_chain(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screenscribe.transcribe import Segment, TranscriptionResult

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    # Seed a real chain head via a VLM analysis.
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **kw: _finding(None, response_id="vlm-head"),
    )
    client.post(f"/api/analyze/{marker_id}")

    def fake_stt(*_: object, **__: object) -> TranscriptionResult:
        return TranscriptionResult(
            text="hello there this is a voice note",
            segments=[Segment(id=0, start=0.0, end=1.0, text="hello there")],
            language="en",
            response_id="",  # empty -> must not clobber
        )

    monkeypatch.setattr("screenscribe.analyze_server.transcribe_browser_audio", fake_stt)
    monkeypatch.setattr(
        "screenscribe.transcribe.validate_audio_quality",
        lambda _result: (True, None, False),
    )

    audio = b"0" * 4096
    resp = client.post("/api/stt", files={"audio": ("v.webm", audio, "audio/webm")})
    assert resp.status_code == 200

    # The next VLM analysis must still get the real head, not a blank.
    seen: dict[str, Any] = {}

    def capture(*_: object, previous_response_id: str | None = None, **__: object) -> Any:
        seen["previous"] = previous_response_id
        return _finding(None, response_id="vlm-head-2")

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", capture)
    client.post(f"/api/analyze/{marker_id}")
    assert seen["previous"] == "vlm-head"


# ---------------------------------------------------------------------------
# BH58 - corrupt frame payload must not leave the marker stuck "analyzing".
# ---------------------------------------------------------------------------


def _reach_session(app: Any) -> Any:
    """Walk the app's route closures to reach the live AnalyzeSession."""
    from screenscribe import analyze_server as mod

    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        closure = getattr(endpoint, "__closure__", None)
        if not closure:
            continue
        for cell in closure:
            val = cell.cell_contents
            if isinstance(val, mod.AnalyzeSession):
                return val
    raise AssertionError("could not reach AnalyzeSession")


def test_corrupt_frame_payload_resets_marker_to_error(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    # Force the fallback-decode branch (BH58): blank the persisted frame_path so
    # analyze must base64-decode marker.frame_base64, then make that decode
    # raise. The decode + tempfile write now live INSIDE the try, so the marker
    # must end up "error" rather than stuck "analyzing".
    session = _reach_session(app)
    with session.lock:
        session.markers[marker_id].frame_path = None

    import base64 as _b64

    def boom(*_a: object, **_k: object) -> bytes:
        raise ValueError("corrupt base64")

    monkeypatch.setattr(_b64, "b64decode", boom)
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **kw: pytest.fail("VLM must not be reached when frame decode fails"),
    )

    body = client.post(f"/api/analyze/{marker_id}")
    assert body.json()["status"] == "error"
    marker = client.get("/api/markers").json()[0]
    assert marker["status"] == "error"  # never stuck "analyzing"


# ---------------------------------------------------------------------------
# BH40 - marker deleted while its VLM call is in flight must not resurrect.
# ---------------------------------------------------------------------------


def test_marker_deleted_mid_analysis_does_not_resurrect(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    def analyze_then_delete(*_: object, **__: object) -> Any:
        # Simulate the marker being deleted DURING the (unlocked) VLM call.
        client.delete(f"/api/marker/{marker_id}")
        return _finding(None, summary="ghost", response_id="r")

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified", analyze_then_delete
    )

    body = client.post(f"/api/analyze/{marker_id}").json()
    assert body["status"] == "error"  # marker gone -> no completed write

    # No resurrected ghost in markers or export.
    assert client.get("/api/markers").json() == []
    assert client.get("/api/export").json()["work_items"] == []


# ---------------------------------------------------------------------------
# BH12 - a marker deleted mid-finalize must not abort the whole batch.
# ---------------------------------------------------------------------------


def test_finalize_continues_after_one_marker_disappears(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    # Three markers in insertion (== iteration) order. While the FIRST is being
    # analyzed we delete the SECOND (still-pending) one. When the batch reaches
    # the second id, analyze_single_marker raises HTTPException(404). Without the
    # BH12 per-marker guard that 404 aborts the whole batch and the THIRD marker
    # is never processed.
    first = _mark_one(client, transcript="first")
    doomed = _mark_one(client, transcript="doomed")
    third = _mark_one(client, transcript="third")

    calls = {"n": 0}

    def analyze(detection: Any, *_: object, **__: object) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            # Delete a marker that has NOT been processed yet (the second one).
            assert client.delete(f"/api/marker/{doomed}").status_code == 200
        return _finding(None, summary=detection.segment.text)

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", analyze)

    # /api/finalize runs the whole batch (offloaded to threadpool).
    resp = client.post("/api/finalize")
    assert resp.status_code == 200
    summary = resp.json()["analysis"]
    # The batch did NOT abort on the vanished marker: it processed all three ids,
    # two completing and the doomed one counted as an error.
    assert summary["processed"] == 3
    assert summary["completed"] == 2
    assert summary["errors"] == 1

    # Both surviving markers have a real completed result; the third (after the
    # vanished one) was reached, proving the batch kept going.
    markers = {m["marker_id"]: m for m in client.get("/api/markers").json()}
    assert markers[first]["status"] == "completed"
    assert markers[third]["status"] == "completed"
    assert doomed not in markers


# ---------------------------------------------------------------------------
# P3-2 - an orphan "analyzing" marker must be retried by finalize.
# ---------------------------------------------------------------------------


def test_finalize_orphan_analyzing_status_is_demoted_and_retried(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker stuck 'analyzing' with a stale result (orphan from a crashed
    prior run) must be demoted to pending and re-processed by finalize (P3-2).

    Without the reset, finalize skips it (it has a result and its status is not
    pending/error) -> stuck forever.
    """
    from screenscribe import analyze_server as mod

    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)
    session = _reach_session(app)

    # Plant the orphan: analyzing + stale result.
    with session.lock:
        marker = session.markers[marker_id]
        marker.status = "analyzing"
        session.results[marker_id] = mod.AnalysisResult(
            marker_id=marker_id, timestamp=marker.timestamp, summary="stale"
        )

    # A fresh analysis should overwrite the stale result once the orphan is
    # demoted to pending and re-queued.
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **kw: _finding(None, summary="refreshed", response_id="r2"),
    )
    resp = client.post("/api/finalize")
    assert resp.status_code == 200
    summary = resp.json()["analysis"]
    assert summary["processed"] == 1  # orphan was re-queued, not skipped

    with session.lock:
        assert session.markers[marker_id].status == "completed"  # no longer stuck
        assert session.results[marker_id].summary == "refreshed"


# ---------------------------------------------------------------------------
# P3-5 - finalize background thread survives a vanished job id.
# ---------------------------------------------------------------------------


def test_run_finalize_job_survives_vanished_job(sample_video: Path) -> None:
    """run_finalize_job must not crash the thread when the job id is gone."""
    from screenscribe import analyze_server as mod

    app = create_analyze_app(sample_video, _config())

    # Reach run_finalize_job from the app's closures.
    run_finalize_job = None
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        closure = getattr(endpoint, "__closure__", None)
        if not closure:
            continue
        for cell in closure:
            val = cell.cell_contents
            if callable(val) and getattr(val, "__name__", "") == "run_finalize_job":
                run_finalize_job = val
                break
        if run_finalize_job is not None:
            break
    assert run_finalize_job is not None, "could not reach run_finalize_job"

    # Must return cleanly (logging an error) rather than raising HTTPException.
    run_finalize_job("does-not-exist")  # no exception == pass
    assert mod is not None


# ---------------------------------------------------------------------------
# Concurrency contract (W4 fix/concurrency-contract) — real asyncio.gather.
# ---------------------------------------------------------------------------


def test_concurrent_analyses_last_response_id_uses_cas(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overlapping analyses must not clobber the chain head.

    Both analyses read ``last_response_id == ""`` at start. The fast one finishes
    first and commits its id; the slow one returns later having read the stale
    ``""`` and must LOSE the compare-and-set instead of overwriting the newer
    head. Without CAS the late writer would clobber the head with an id chained
    off a stale predecessor.
    """
    import asyncio
    import threading

    import httpx

    app = create_analyze_app(sample_video, _config())
    token = app.state.session_token

    slow_has_read = threading.Event()
    release_slow = threading.Event()

    def fake_vlm(**kwargs: Any) -> Any:
        text = kwargs["detection"].segment.text
        if "SLOW" in text:
            # SLOW has already read last_response_id (== "") before this call.
            slow_has_read.set()
            release_slow.wait(5)
            return _finding(None, response_id="slow-id")
        # FAST must not commit until SLOW has read the stale "" head.
        slow_has_read.wait(5)
        return _finding(None, response_id="fast-id")

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", fake_vlm)

    async def _run() -> tuple[str, str]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            headers={"X-ScreenScribe-Token": token},
        ) as client:
            fast_id = (
                await client.post(
                    "/api/mark",
                    json={
                        "timestamp": 1.0,
                        "frame_base64": PNG_1X1_BASE64,
                        "transcript": "FAST",
                        "notes": "",
                    },
                )
            ).json()["marker_id"]
            slow_id = (
                await client.post(
                    "/api/mark",
                    json={
                        "timestamp": 2.0,
                        "frame_base64": PNG_1X1_BASE64,
                        "transcript": "SLOW",
                        "notes": "",
                    },
                )
            ).json()["marker_id"]

            t_fast = asyncio.create_task(client.post(f"/api/analyze/{fast_id}"))
            t_slow = asyncio.create_task(client.post(f"/api/analyze/{slow_id}"))
            r_fast = await t_fast
            release_slow.set()
            r_slow = await t_slow
            return r_fast.json()["status"], r_slow.json()["status"]

    status_fast, status_slow = asyncio.run(_run())
    assert status_fast == "completed"
    assert status_slow == "completed"

    session = _reach_session(app)
    # Fast committed "fast-id"; slow read the stale "" and lost the CAS, so the
    # head keeps the newer id rather than being clobbered by the late writer.
    assert session.last_response_id == "fast-id"
