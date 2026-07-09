from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import MAX_AUDIO_BYTES, create_review_app
from screenscribe.transcribe import Segment, TranscriptionResult

# A browser STT upload comfortably over the 1024-byte min-length guard, so these
# STT tests exercise the transcription path rather than tripping the too-short
# rejection (mirrors ``VALID_BROWSER_AUDIO`` in test_analyze_server_stt.py).
VALID_BROWSER_AUDIO = b"voice-data" + (b"x" * 2048)


@pytest.fixture
def review_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    output_dir = tmp_path / "review"
    output_dir.mkdir()
    report_file = output_dir / "screen_report.html"
    report_file.write_text("<html><body>report</body></html>", encoding="utf-8")
    video_path = output_dir / "screen.mov"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
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


def test_review_server_serves_report_artifact(review_workspace: tuple[Path, Path, Path]) -> None:
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    response = client.get(f"/{report_file.name}")

    assert response.status_code == 200
    assert "report" in response.text


def test_review_server_serves_only_selected_video_symlink(
    review_workspace: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Static report serving must not follow arbitrary symlinks in output_dir."""
    output_dir, report_file, video_path = review_workspace
    secret_file = tmp_path / "outside-secret.txt"
    secret_file.write_text("do not serve me", encoding="utf-8")
    (output_dir / "secret-link.txt").symlink_to(secret_file)

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    secret_response = client.get("/secret-link.txt")
    assert secret_response.status_code == 404

    video_response = client.get("/video")
    assert video_response.status_code == 200
    assert video_response.content == video_path.read_bytes()


def test_review_server_manual_frame_analysis(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    output_dir, report_file, video_path = review_workspace

    def fake_analyze_finding_unified(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            category="manual_capture",
            severity="high",
            summary="Manual frame confirms a UI mismatch.",
            issues_detected=["CTA label is inconsistent"],
            suggested_fix="Align CTA copy with the onboarding step.",
            affected_components=["Onboarding CTA"],
            response_id="resp_manual_123",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze_finding_unified,
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    mark_response = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 12.5,
            "frame_base64": "/9j/4GZha2UtanBlZy1kYXRh",
            "transcript": "Opisuję problem na ekranie.",
            "notes": "To wygląda jak błąd tłumaczenia.",
        },
    )

    assert mark_response.status_code == 200
    marker_id = mark_response.json()["marker_id"]

    analyze_response = client.post(f"/api/manual-analyze/{marker_id}")

    assert analyze_response.status_code == 200
    payload = analyze_response.json()
    assert payload["status"] == "completed"
    assert payload["result"]["severity"] == "high"
    assert payload["result"]["summary"] == "Manual frame confirms a UI mismatch."


def test_review_server_stt_uses_direct_browser_upload_path(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    output_dir, report_file, video_path = review_workspace

    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        assert args[0] == VALID_BROWSER_AUDIO
        assert args[1] == "recording.webm"
        assert kwargs["content_type"] == "audio/webm"
        return TranscriptionResult(
            text="Direct transcript",
            segments=[Segment(id=1, start=0.0, end=1.5, text="Direct transcript")],
            language="pl",
            response_id="resp_direct",
        )

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == "Direct transcript"
    assert payload["response_id"] == "resp_direct"


def test_review_server_stt_payload_matches_unified_key_set(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """P3-12: the review STT payload now carries ``language`` so it shares one
    key-set with the analyze server (previously review dropped ``language``)."""
    output_dir, report_file, video_path = review_workspace

    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        return TranscriptionResult(
            text="Polska notatka",
            segments=[Segment(id=1, start=0.0, end=1.5, text="Polska notatka")],
            language="pl",
            response_id="resp_pl",
        )

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", VALID_BROWSER_AUDIO, "audio/webm")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"text", "segments", "response_id", "language"}
    assert payload["language"] == "pl"
    assert payload["segments"] == [{"start": 0.0, "end": 1.5, "text": "Polska notatka"}]


def test_review_server_save_persistence(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    import json

    output_dir, report_file, video_path = review_workspace

    # Create dummy report.json
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    def fake_analyze_finding_unified(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            category="manual_capture",
            severity="high",
            summary="Manual frame confirms a UI mismatch.",
            issues_detected=["CTA label is inconsistent"],
            suggested_fix="Align CTA copy with the onboarding step.",
            affected_components=["Onboarding CTA"],
            response_id="resp_manual_123",
            is_issue=True,
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze_finding_unified,
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    # 1. Add marker
    mark_response = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 12.5,
            "frame_base64": "/9j/4GZha2UtanBlZy1kYXRh",
            "transcript": "Test transcript",
            "notes": "Test notes",
        },
    )
    marker_id = mark_response.json()["marker_id"]

    # 2. Analyze marker
    client.post(f"/api/manual-analyze/{marker_id}")

    # 3. Save to disk
    save_response = client.post("/api/save")
    assert save_response.status_code == 200
    assert "success" in save_response.json()["status"]

    # 4. Verify report.json content
    with open(json_path, encoding="utf-8") as f:
        saved_data = json.load(f)

    assert "manual_review" in saved_data
    assert len(saved_data["manual_review"]["markers"]) == 1
    assert saved_data["manual_review"]["markers"][0]["marker_id"] == marker_id
    assert (
        saved_data["manual_review"]["results"][0]["summary"]
        == "Manual frame confirms a UI mismatch."
    )


def test_review_server_save_response_omits_absolute_path(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """C5.1: /api/save must not leak the absolute on-disk path back to the UI."""
    import json
    import os

    output_dir, report_file, video_path = review_workspace

    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    save_response = client.post("/api/save")
    assert save_response.status_code == 200

    body = save_response.json()
    assert body["status"] == "success"

    # No leaked absolute path under any key.
    assert "path" not in body
    output_str = str(output_dir)
    for value in body.values():
        assert isinstance(value, str)
        assert output_str not in value
        # No value should look like an absolute filesystem path.
        assert not value.startswith(os.sep)

    # If a filename is surfaced, it must be a bare basename (no separators).
    if "filename" in body:
        assert body["filename"] == json_path.name
        assert os.sep not in body["filename"]


def test_review_state_hydrates_session_manual_frame_image(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """A frame added via /api/manual-mark must come back from /api/review-state
    as a renderable data URL.

    This is what lets the client drop ``frameDataUrl`` from its localStorage
    draft (the quota bomb) and still restore the image after a reload: the
    server session — not the browser cache — is the source of truth for the
    frame pixels.
    """
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    frame_base64 = "/9j/4GZha2UtanBlZy1kYXRh"  # JPEG magic bytes (/9j/ -> FF D8 FF)
    mark = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 12.5,
            "frame_base64": frame_base64,
            "transcript": "spoken note",
            "notes": "needs a fix",
        },
    )
    marker_id = mark.json()["marker_id"]

    # Reload: the browser asks the server for durable review state.
    state = client.get("/api/review-state").json()
    frames = [
        frame for frame in state.get("manualFrames", []) if frame.get("marker_id") == marker_id
    ]
    assert frames, "review-state did not restore the session manual frame"
    data_url = frames[0].get("frameDataUrl", "")
    assert data_url.startswith("data:image/"), data_url
    assert frame_base64 in data_url


def test_review_server_delete_manual_frame_purges_session(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """Deleting a manual frame removes it from the session so a cold reload
    (review-state) cannot resurrect a capture the reviewer discarded; a repeat
    delete is idempotent rather than a 404."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    frame_base64 = "/9j/4GZha2UtanBlZy1kYXRh"  # JPEG magic bytes (/9j/ -> FF D8 FF)
    mark = client.post(
        "/api/manual-mark",
        json={"timestamp": 12.5, "frame_base64": frame_base64, "transcript": "", "notes": ""},
    )
    marker_id = mark.json()["marker_id"]

    delete = client.delete(f"/api/manual-mark/{marker_id}")
    assert delete.status_code == 200, delete.text
    assert delete.json()["status"] == "deleted"

    state = client.get("/api/review-state").json()
    assert not [
        frame for frame in state.get("manualFrames", []) if frame.get("marker_id") == marker_id
    ], "review-state still returned a deleted manual frame"

    # Idempotent: deleting an already-gone marker is a no-op, not a 404.
    again = client.delete(f"/api/manual-mark/{marker_id}")
    assert again.status_code == 200, again.text


def _mark_one_frame(client: TestClient, *, transcript: str = "", notes: str = "") -> str:
    """Mark a single manual frame and return its marker_id."""
    frame_base64 = "/9j/4GZha2UtanBlZy1kYXRh"  # JPEG magic bytes (/9j/ -> FF D8 FF)
    mark = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 12.5,
            "frame_base64": frame_base64,
            "transcript": transcript,
            "notes": notes,
        },
    )
    assert mark.status_code == 200, mark.text
    return mark.json()["marker_id"]


def test_save_sweeps_orphan_manual_frame_but_keeps_live_pending(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """finding 156: /api/save sweeps an abandoned manual-frame image while a live
    pending marker's image survives.

    A capture is written to disk at mark time so it survives a cold load. If it is
    then never saved into report.json and never explicitly deleted (e.g. left
    behind by a prior process whose in-memory marker is gone), the file lingers
    forever — HTTP-reachable via the static mount (privacy leak) and disk litter.
    The save-time sweep removes it, but the keep-set is the union of report.json
    frame_paths and EVERY live session marker, so a marked-but-unsaved (pending,
    unanalyzed) capture must NOT be swept: that would be the data-loss regression
    this cut most risks.
    """
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    # A live pending marker: marked this session, never analyzed and never in a
    # save body. Its on-disk image must survive the sweep purely because the
    # marker is still live in the session.
    live_marker_id = _mark_one_frame(client)
    frames_dir = output_dir / "manual_frames"
    live_files = list(frames_dir.glob(f"{live_marker_id}.*"))
    assert len(live_files) == 1, "mark should have written the live frame to disk"
    live_file = live_files[0]

    # An orphaned capture: a real JPEG left under manual_frames/ with no live
    # marker and no report.json reference (an abandoned mark). Written directly to
    # mimic a file whose in-memory marker is already gone.
    orphan_file = frames_dir / "orphan-abandoned.jpg"
    orphan_file.write_bytes(bytes.fromhex("FFD8FF") + b"stale-capture-bytes")
    assert orphan_file.is_file()

    save_response = client.post("/api/save")
    assert save_response.status_code == 200, save_response.text

    assert not orphan_file.exists(), "orphaned manual frame should be swept on save"
    assert live_file.exists(), "live pending marker's frame must survive the sweep"


def test_update_manual_frame_persists_notes_and_transcript_for_reload(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """BH28: editing a marked frame's notes/transcript after the mark must be
    durable — review-state (the cold-reload source of truth) returns the new
    text, not the value captured at mark time."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_id = _mark_one_frame(client, transcript="first spoken", notes="first note")

    update = client.patch(
        f"/api/manual-mark/{marker_id}",
        json={"transcript": "edited spoken", "notes": "edited note"},
    )
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["transcript"] == "edited spoken"
    assert body["notes"] == "edited note"

    # Reload: the browser asks the server for durable review state.
    state = client.get("/api/review-state").json()
    frames = [f for f in state.get("manualFrames", []) if f.get("marker_id") == marker_id]
    assert frames, "review-state dropped the edited manual frame"
    assert frames[0]["transcript"] == "edited spoken"
    assert frames[0]["notes"] == "edited note"


def test_update_manual_frame_partial_leaves_other_field_untouched(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """BH28: a partial update (notes only) must not wipe the transcript.

    ``None`` means "leave unchanged" so the client can patch one field without
    resending the other.
    """
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_id = _mark_one_frame(client, transcript="keep me", notes="old note")

    update = client.patch(f"/api/manual-mark/{marker_id}", json={"notes": "new note"})
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["notes"] == "new note"
    assert body["transcript"] == "keep me", "partial update wiped the untouched transcript"


def test_update_manual_frame_unknown_marker_is_404(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """BH28: editing a marker that does not exist is a 404, not a silent create."""
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    update = client.patch(
        "/api/manual-mark/does-not-exist",
        json={"notes": "ghost edit"},
    )
    assert update.status_code == 404, update.text


def _write_report_json(output_dir: Path) -> None:
    """Give the review server a report.json to load (review-state/save need it)."""
    import json

    (output_dir / "screen_report.json").write_text(
        json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8"
    )


def test_patch_manual_frame_severity_persists_and_surfaces_in_review_state(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """R14: a manual priority override persists on the marker and comes back from
    review-state (the cold-reload source of truth) so the card select reflects it."""
    output_dir, report_file, video_path = review_workspace
    _write_report_json(output_dir)
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_id = _mark_one_frame(client)

    resp = client.patch(f"/api/manual-mark/{marker_id}", json={"severity": "high"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["severity"] == "high"

    state = client.get("/api/review-state").json()
    frames = [f for f in state.get("manualFrames", []) if f.get("marker_id") == marker_id]
    assert frames, "review-state dropped the frame"
    assert frames[0]["severity"] == "high"


def test_patch_manual_frame_severity_override_wins_over_fresh_vlm_severity(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """R14 (f3e482e trap): an override set BEFORE analysis must survive the
    analysis. The VLM assigns "medium" but the reviewer already picked "critical";
    the completed result must carry the override, not the model severity."""
    output_dir, report_file, video_path = review_workspace

    def fake_analyze_finding_unified(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            category="manual_capture",
            severity="medium",
            summary="VLM says medium.",
            issues_detected=[],
            suggested_fix="",
            affected_components=[],
            response_id="resp_1",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze_finding_unified,
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_id = _mark_one_frame(client)
    client.patch(f"/api/manual-mark/{marker_id}", json={"severity": "critical"})

    analyze = client.post(f"/api/manual-analyze/{marker_id}")
    assert analyze.status_code == 200, analyze.text
    assert analyze.json()["result"]["severity"] == "critical"  # override won, not "medium"


def test_patch_manual_frame_severity_mirrors_existing_result_and_survives_save(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """R14: overriding priority AFTER analysis mirrors onto the existing result,
    and the override survives /api/save into report.json (persistence path)."""
    import json

    output_dir, report_file, video_path = review_workspace
    _write_report_json(output_dir)

    def fake_analyze_finding_unified(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            category="manual_capture",
            severity="high",
            summary="VLM says high.",
            issues_detected=[],
            suggested_fix="",
            affected_components=[],
            response_id="resp_2",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze_finding_unified,
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_id = _mark_one_frame(client)
    client.post(f"/api/manual-analyze/{marker_id}")
    client.patch(f"/api/manual-mark/{marker_id}", json={"severity": "low"})

    save = client.post("/api/save", json={})
    assert save.status_code == 200, save.text

    report = json.loads((output_dir / "screen_report.json").read_text(encoding="utf-8"))
    results = report["manual_review"]["results"]
    mine = [r for r in results if r["marker_id"] == marker_id]
    assert mine and mine[0]["severity"] == "low", "override did not mirror onto the saved result"


def test_patch_manual_frame_severity_none_clears_the_override(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """R14: the explicit "none" is a real override that CLEARS a prior pick."""
    output_dir, report_file, video_path = review_workspace
    _write_report_json(output_dir)
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_id = _mark_one_frame(client)
    client.patch(f"/api/manual-mark/{marker_id}", json={"severity": "critical"})

    resp = client.patch(f"/api/manual-mark/{marker_id}", json={"severity": "none"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["severity"] == "none"

    state = client.get("/api/review-state").json()
    frames = [f for f in state.get("manualFrames", []) if f.get("marker_id") == marker_id]
    assert frames and frames[0]["severity"] == "none"


def test_patch_manual_frame_ignores_unknown_severity(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """R14: an out-of-vocabulary priority is ignored, never wedging the edit."""
    output_dir, report_file, video_path = review_workspace
    _write_report_json(output_dir)
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_id = _mark_one_frame(client)
    resp = client.patch(f"/api/manual-mark/{marker_id}", json={"severity": "bogus"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["severity"] is None  # unchanged, not set to junk


def test_review_server_stt_rejects_oversize_audio(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """An audio upload above MAX_AUDIO_BYTES must be rejected with 413."""
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    oversize = b"\x00" * (MAX_AUDIO_BYTES + 1)
    response = client.post(
        "/api/stt",
        files={"audio": ("big.webm", oversize, "audio/webm")},
    )

    assert response.status_code == 413
    assert "25 MB" in response.json()["detail"]


def test_review_server_stt_upload_cap_rejects_declared_oversize(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """The _stt_upload_cap middleware must reject an honestly-declared oversized
    upload via Content-Length before the body is parsed (parity with
    analyze_server). A tiny body with a forged oversized Content-Length proves
    the early 413 comes from the middleware, not the handler's read-all guard."""
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    declared = MAX_AUDIO_BYTES + 64 * 1024 + 1
    response = client.post(
        "/api/stt",
        content=b"x",
        headers={
            "content-type": "audio/webm",
            "content-length": str(declared),
        },
    )

    assert response.status_code == 413
    assert "25 MB" in response.json()["detail"]


def test_review_server_stt_rejects_empty_upload(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """An empty browser STT upload is rejected 400 with the shared detail string
    (byte-identical to analyze_server via validate_browser_stt_upload)."""
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", b"", "audio/webm")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Voice recording is empty."


def test_review_server_stt_rejects_tiny_browser_recording_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """A sub-1KB browser recording is rejected 400 before any STT provider call,
    with the shared detail string (byte-identical to analyze_server)."""

    def fail_transcribe(*args: Any, **kwargs: Any) -> TranscriptionResult:
        raise AssertionError("Tiny browser recordings should not reach STT provider")

    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio_bytes", fail_transcribe)

    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    response = client.post(
        "/api/stt",
        files={"audio": ("recording.webm", b"\x1aE\xdf\xa3", "audio/webm")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Voice recording is too short. Hold to record longer."


def test_review_server_mark_rejects_oversize_base64(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """Pydantic must reject frame_base64 strings longer than the configured cap."""
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    oversize_b64 = "A" * 20_000_001  # one character over the cap
    response = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 1.0,
            "frame_base64": oversize_b64,
            "transcript": "",
            "notes": "",
        },
    )

    assert response.status_code == 422


def test_review_server_mark_rejects_non_image_bytes(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """A well-formed base64 payload that is not a JPEG/PNG must be rejected with
    400 at mark time — the disk store is the durable source of truth, so junk must
    never reach it."""
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    non_image_b64 = base64.b64encode(b"not an image").decode()
    mark_response = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 1.0,
            "frame_base64": non_image_b64,
            "transcript": "",
            "notes": "",
        },
    )
    assert mark_response.status_code == 400
    assert "JPEG or PNG" in mark_response.json()["detail"]
    # No file should have been written for a rejected capture.
    assert (
        not list((output_dir / "manual_frames").glob("*"))
        if (output_dir / "manual_frames").exists()
        else True
    )


def test_review_server_error_does_not_leak_paths(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """/save must not echo absolute filesystem paths in error bodies."""
    output_dir, report_file, video_path = review_workspace
    # Intentionally do NOT create a report.json so /save trips the 404 branch.
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    response = client.post("/api/save")

    assert response.status_code == 404
    body = response.text
    assert str(output_dir) not in body
    assert str(output_dir.resolve()) not in body
    assert "/" + report_file.stem not in body
    assert ".json" not in body


def test_review_server_save_merges_human_review(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """/api/save with a review body persists verdicts into report.json —
    rejected findings stay in the canonical file, explicitly marked."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps({"video": "screen.mov", "findings": [{"id": 1}, {"id": 2}]}),
        encoding="utf-8",
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    body = {
        "video": "screen.mov",
        "reviewer": "alex",
        "reviewed_at": "2026-06-10T12:00:00Z",
        "findings": [
            {
                "id": 1,
                "human_review": {
                    "verdict": "accepted",
                    "notes": "real bug",
                    "severity_override": None,
                },
            },
            {
                "id": 2,
                "human_review": {
                    "verdict": "rejected",
                    "notes": "false alarm",
                    "severity_override": None,
                },
            },
        ],
        "manual_frames": [],
    }
    response = client.post("/api/save", json=body)
    assert response.status_code == 200

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    human = saved["human_review"]
    assert human["reviewer"] == "alex"
    assert human["findings"]["1"]["verdict"] == "accepted"
    assert human["findings"]["2"]["verdict"] == "rejected"
    assert human["rejected_ids"] == [2]
    # The canonical findings list is untouched — rejection is a marker, not a deletion.
    assert len(saved["findings"]) == 2


def test_review_server_reloads_human_review_from_disk_after_fresh_load(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """Saved decisions must survive a fresh browser load with no localStorage."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [
                    {"id": 1, "timestamp": 1.25, "category": "bug"},
                    {"id": 2, "timestamp": 2.5, "category": "ui"},
                ],
            }
        ),
        encoding="utf-8",
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    save_body = {
        "video": "screen.mov",
        "reviewer": "alex",
        "reviewed_at": "2026-06-13T12:00:00Z",
        "findings": [
            {
                "id": 1,
                "timestamp": 1.25,
                "category": "bug",
                "human_review": {
                    "verdict": "accepted",
                    "severity_override": "critical",
                    "notes": "real bug, fix first",
                    "annotations": [{"type": "rect", "x": 0.1, "y": 0.2}],
                    "reviewer": "alex",
                    "reviewed_at": "2026-06-13T12:00:00Z",
                },
            },
            {
                "id": 2,
                "timestamp": 2.5,
                "category": "ui",
                "human_review": {
                    "verdict": "rejected",
                    "severity_override": None,
                    "notes": "false alarm",
                    "annotations": [],
                    "reviewer": "alex",
                    "reviewed_at": "2026-06-13T12:00:00Z",
                },
            },
        ],
        "manual_frames": [
            {
                "marker_id": "manual-1",
                "timestamp": 3.75,
                "timestamp_formatted": "00:03.750",
                "transcript": "spoken manual context",
                "notes": "manual note",
                "frameDataUrl": "data:image/png;base64,iVBORfake",
                "result": {"severity": "medium", "summary": "Manual issue"},
                "annotations": [{"type": "arrow", "x1": 0.2, "y1": 0.3}],
            }
        ],
    }

    assert client.post("/api/save", json=save_body).status_code == 200

    fresh_app = create_review_app(output_dir, report_file.name, video_path, _config())
    fresh_client = TestClient(fresh_app)
    response = fresh_client.get("/api/review-state")

    assert response.status_code == 200
    state = response.json()
    assert state["reviewer"] == "alex"
    assert state["findings"]["1"]["verdict"] == "accepted"
    assert state["findings"]["1"]["severity"] == "critical"
    assert state["findings"]["1"]["notes"] == "real bug, fix first"
    assert state["findings"]["1"]["annotations"] == [{"type": "rect", "x": 0.1, "y": 0.2}]
    assert state["findings"]["2"]["verdict"] == "rejected"
    assert state["findings"]["2"]["notes"] == "false alarm"
    assert state["manualFrames"][0]["marker_id"] == "manual-1"
    assert state["manualFrames"][0]["annotations"] == [{"type": "arrow", "x1": 0.2, "y1": 0.3}]


def test_review_server_migrates_legacy_confirmed_to_verdict(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """A legacy report.json carrying the old boolean `confirmed` is read into the
    verdict vocabulary: True -> accepted, False -> rejected, missing -> none.
    `none` is an explicit string, never invented as accepted/rejected."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [{"id": 1}, {"id": 2}, {"id": 3}],
                "human_review": {
                    "reviewer": "legacy",
                    "findings": {
                        "1": {"confirmed": True, "notes": "kept"},
                        "2": {"confirmed": False, "notes": "dismissed"},
                        "3": {"notes": "untouched"},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    response = client.get("/api/review-state")

    assert response.status_code == 200
    state = response.json()
    assert state["findings"]["1"]["verdict"] == "accepted"
    assert state["findings"]["2"]["verdict"] == "rejected"
    # Missing/null decision is the honest "not reviewed", never a fail-open.
    assert state["findings"]["3"]["verdict"] == "none"
    # The legacy boolean key must not survive into the new emitted state.
    assert "confirmed" not in state["findings"]["1"]


# A valid tiny 1x1 JPEG (base64, no data: prefix). Decodes to FF D8 FF ...
_TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"  # pragma: allowlist secret
    "AAAAAAAAAAAAAAAAAAAAAv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)


def test_manual_mark_writes_image_to_disk(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """POST /api/manual-mark writes the validated image under manual_frames/ and
    returns a relative frame_path reference (durable source of truth)."""
    import json

    output_dir, report_file, video_path = review_workspace
    (output_dir / "screen_report.json").write_text(
        json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8"
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    mark = client.post(
        "/api/manual-mark",
        json={"timestamp": 1.0, "frame_base64": _TINY_JPEG_B64, "transcript": "", "notes": ""},
    )
    assert mark.status_code == 200, mark.text
    payload = mark.json()
    marker_id = payload["marker_id"]
    frame_path = payload["frame_path"]
    assert frame_path == f"manual_frames/{marker_id}.jpg"

    on_disk = output_dir / frame_path
    assert on_disk.is_file(), f"manual frame image not written to disk: {on_disk}"
    assert on_disk.read_bytes().startswith(b"\xff\xd8\xff"), "stored bytes are not JPEG"


def test_manual_mark_rejects_bad_base64(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """Undecodable base64 is rejected with 400 and nothing is written to disk."""
    output_dir, report_file, video_path = review_workspace
    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    resp = client.post(
        "/api/manual-mark",
        json={"timestamp": 1.0, "frame_base64": "!!!not-base64!!!", "transcript": "", "notes": ""},
    )
    assert resp.status_code == 400, resp.text
    assert not (output_dir / "manual_frames").exists() or not list(
        (output_dir / "manual_frames").glob("*")
    )


def test_manual_frame_survives_cold_load_from_disk(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """A manual frame added then saved is restorable as a renderable data URL on a
    FRESH app/session over the same output_dir (cold load), with the pixels read
    back off disk — no in-memory base64 and no inline data: image in report.json."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    mark = client.post(
        "/api/manual-mark",
        json={
            "timestamp": 4.2,
            "frame_base64": _TINY_JPEG_B64,
            "transcript": "spoken",
            "notes": "typed",
        },
    )
    marker_id = mark.json()["marker_id"]

    # Save a full review body (the real client posts buildReviewData()).
    save_body = {
        "video": "screen.mov",
        "reviewer": "alex",
        "reviewed_at": "2026-06-18T12:00:00Z",
        "findings": [],
        "manual_frames": [
            {
                "marker_id": marker_id,
                "timestamp": 4.2,
                "timestamp_formatted": "00:04.200",
                "transcript": "spoken",
                "notes": "typed",
                "frameDataUrl": "data:image/jpeg;base64," + _TINY_JPEG_B64,
                "result": None,
                "annotations": [],
            }
        ],
    }
    assert client.post("/api/save", json=save_body).status_code == 200

    # report.json must carry a path reference, NOT the inline data: image.
    raw_report = json_path.read_text(encoding="utf-8")
    assert "data:image" not in raw_report, "report.json still embeds a manual-frame data: image"
    assert f"manual_frames/{marker_id}.jpg" in raw_report, "report.json lost the frame_path ref"

    # Cold load: a brand-new app/session over the same dir, empty in-memory state.
    fresh_app = create_review_app(output_dir, report_file.name, video_path, _config())
    fresh_client = TestClient(fresh_app)
    state = fresh_client.get("/api/review-state").json()
    frames = [f for f in state["manualFrames"] if f.get("marker_id") == marker_id]
    assert frames, "cold-load review-state did not restore the manual frame"
    data_url = frames[0].get("frameDataUrl", "")
    assert data_url.startswith("data:image/"), data_url
    # The restored image is the real stored JPEG, not the original posted base64
    # string verbatim (it is re-encoded off disk), so just assert it is renderable.
    assert len(data_url) > len("data:image/jpeg;base64,") + 10


def test_manual_frame_missing_file_does_not_crash_review_state(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """A marker whose disk image is gone yields a metadata frame with a missing
    signal — review-state still loads, never 500s."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [],
                "human_review": {
                    "reviewer": "alex",
                    "manual_frames": [
                        {
                            "marker_id": "gone-1",
                            "timestamp": 1.0,
                            "frame_path": "manual_frames/gone-1.jpg",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    resp = client.get("/api/review-state")
    assert resp.status_code == 200, resp.text
    frames = [f for f in resp.json()["manualFrames"] if f.get("marker_id") == "gone-1"]
    assert frames, "missing-image frame dropped from review-state"
    assert frames[0].get("imageMissing") is True
    assert not frames[0].get("frameDataUrl")


# --- D6 (SS-ARCH-3): delete cleanup + read revalidate -----------------------


def test_delete_manual_frame_unlinks_disk_image(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """D6-3a: deleting a manual frame removes the durable file under
    manual_frames/, not just the in-memory marker. Otherwise the discarded
    capture orphans on disk (privacy/storage) and a later save could re-stamp
    its frame_path back into report.json."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    frame_base64 = "/9j/4GZha2UtanBlZy1kYXRh"  # JPEG magic bytes (/9j/ -> FF D8 FF)
    mark = client.post(
        "/api/manual-mark",
        json={"timestamp": 12.5, "frame_base64": frame_base64, "transcript": "", "notes": ""},
    )
    marker_id = mark.json()["marker_id"]
    frame_path = mark.json()["frame_path"]
    on_disk = output_dir / frame_path
    assert on_disk.is_file(), "manual-mark should have written the image to disk"

    delete = client.delete(f"/api/manual-mark/{marker_id}")
    assert delete.status_code == 200, delete.text
    assert not on_disk.exists(), "delete left the manual-frame image orphaned on disk"


def test_delete_manual_frame_disk_cleanup_idempotent_when_file_gone(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """D6-3a: the disk cleanup is idempotent — if the image is already gone the
    delete still returns 200 instead of crashing."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(json.dumps({"video": "screen.mov", "findings": []}), encoding="utf-8")

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    frame_base64 = "/9j/4GZha2UtanBlZy1kYXRh"
    mark = client.post(
        "/api/manual-mark",
        json={"timestamp": 1.0, "frame_base64": frame_base64, "transcript": "", "notes": ""},
    )
    marker_id = mark.json()["marker_id"]
    # Remove the file out-of-band so the endpoint's cleanup hits a missing file.
    (output_dir / mark.json()["frame_path"]).unlink()

    delete = client.delete(f"/api/manual-mark/{marker_id}")
    assert delete.status_code == 200, delete.text


def test_data_url_from_disk_revalidates_swapped_junk_file(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """D6-3b: a file swapped on disk for non-image junk must NOT be served as a
    data:image. The read re-validates magic bytes (not the extension), so a
    .jpg whose bytes are junk yields None instead of a broken data:image."""
    from screenscribe.review_server import _data_url_from_disk

    output_dir, _report_file, _video_path = review_workspace
    frames_dir = output_dir / "manual_frames"
    frames_dir.mkdir()

    # Valid JPEG bytes -> a real data:image is returned.
    good = frames_dir / "good.jpg"
    good.write_bytes(b"\xff\xd8\xff" + b"realjpegpayload")
    good_url = _data_url_from_disk(output_dir, "manual_frames/good.jpg")
    assert good_url is not None and good_url.startswith("data:image/jpeg;base64,")

    # A .jpg whose actual bytes are junk -> None (imageMissing/skip), not a
    # broken data:image blindly labelled image/jpeg from the extension.
    junk = frames_dir / "junk.jpg"
    junk.write_bytes(b"NOT-AN-IMAGE-AT-ALL")
    assert _data_url_from_disk(output_dir, "manual_frames/junk.jpg") is None


def test_review_state_marks_swapped_junk_frame_as_missing(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """D6-3b end-to-end: a manual frame whose disk bytes are junk surfaces as
    imageMissing in review-state instead of a broken data:image."""
    import json

    output_dir, report_file, video_path = review_workspace
    frames_dir = output_dir / "manual_frames"
    frames_dir.mkdir()
    (frames_dir / "junk-1.jpg").write_bytes(b"NOT-AN-IMAGE")

    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [],
                "human_review": {
                    "reviewer": "alex",
                    "manual_frames": [
                        {
                            "marker_id": "junk-1",
                            "timestamp": 1.0,
                            "frame_path": "manual_frames/junk-1.jpg",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    resp = client.get("/api/review-state")
    assert resp.status_code == 200, resp.text
    frames = [f for f in resp.json()["manualFrames"] if f.get("marker_id") == "junk-1"]
    assert frames, "junk-image frame dropped from review-state"
    assert frames[0].get("imageMissing") is True
    assert not frames[0].get("frameDataUrl")


# --- W1-2 g8b server robustness -------------------------------------------


def _jpeg_b64() -> str:
    """Base64 whose decoded bytes start with the JPEG magic (FF D8 FF)."""
    return "/9j/4GZha2UtanBlZy1kYXRh"


def test_bh45_empty_response_id_does_not_clobber_chain(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """BH45 (mirror of analyze-side BH29): a manual-frame finding with an empty
    response_id must NOT overwrite the conversation chain head. A later analyze
    call must still receive the last *real* response_id as previous_response_id."""
    output_dir, report_file, video_path = review_workspace

    seen_previous: list[str] = []
    response_ids = iter(["resp_good", "", "ignored"])

    def fake_analyze_finding_unified(*args: Any, **kwargs: Any) -> SimpleNamespace:
        seen_previous.append(kwargs.get("previous_response_id", ""))
        return SimpleNamespace(
            category="manual_capture",
            severity="medium",
            summary="ok",
            issues_detected=[],
            suggested_fix="",
            affected_components=[],
            response_id=next(response_ids),
            is_issue=True,
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze_finding_unified,
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    marker_ids = []
    for _ in range(3):
        resp = client.post(
            "/api/manual-mark",
            json={"timestamp": 1.0, "frame_base64": _jpeg_b64(), "transcript": "t", "notes": ""},
        )
        marker_ids.append(resp.json()["marker_id"])

    # 1st analyze: chain head becomes "resp_good".
    client.post(f"/api/manual-analyze/{marker_ids[0]}")
    # 2nd analyze: finding carries an EMPTY response_id. Without the guard this
    # would clobber the chain head with "".
    client.post(f"/api/manual-analyze/{marker_ids[1]}")
    # 3rd analyze: previous_response_id passed here proves whether the chain
    # head survived the empty id.
    client.post(f"/api/manual-analyze/{marker_ids[2]}")

    # seen_previous = [head before #1, head before #2, head before #3]
    assert seen_previous[1] == "resp_good"  # #1 advanced the chain
    assert seen_previous[2] == "resp_good"  # #2's empty id did NOT clobber it


def _stt_result(*, text: str, no_speech_prob: float, segments: int = 1) -> TranscriptionResult:
    segs = [
        Segment(id=i, start=float(i), end=float(i) + 1.0, text=text, no_speech_prob=no_speech_prob)
        for i in range(segments)
    ]
    return TranscriptionResult(text=text, segments=segs, language="pl", response_id="r")


def test_bh59_degraded_browser_stt_is_rejected(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """BH59: a degraded STT result (no usable speech) must be rejected 422,
    not handed back to the reviewer as a usable transcript."""
    output_dir, report_file, video_path = review_workspace

    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        # Empty segments -> validate_audio_quality returns (False, ...): no speech.
        return TranscriptionResult(text="", segments=[], language="pl", response_id="r")

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    resp = client.post("/api/stt", files={"audio": ("a.webm", VALID_BROWSER_AUDIO, "audio/webm")})
    assert resp.status_code == 422, resp.text


def test_bh59_marginal_browser_stt_passes_with_quality_warning(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """BH59: a marginal-but-usable recording is still returned (200) but carries
    a non-blocking quality_warning so the UI can flag it."""
    output_dir, report_file, video_path = review_workspace

    # word_count 45 (>= stop threshold 40 so not rejected) + high no_speech 0.80
    # (>= warn threshold 0.75) -> is_valid True, is_warning True.
    warn_text = " ".join(["word"] * 45)

    def fake_transcribe_audio_bytes(*args: Any, **kwargs: Any) -> TranscriptionResult:
        return _stt_result(text=warn_text, no_speech_prob=0.80)

    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes", fake_transcribe_audio_bytes
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    resp = client.post("/api/stt", files={"audio": ("a.webm", VALID_BROWSER_AUDIO, "audio/webm")})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("quality_warning")


def test_bh13_stt_offloaded_to_threadpool(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """BH13: the blocking STT call must run via run_in_threadpool, not directly
    in the async handler (which would block the event loop)."""
    output_dir, report_file, video_path = review_workspace

    offloaded: list[str] = []
    import screenscribe.review_server as rs

    real_threadpool = rs.run_in_threadpool

    async def recording_threadpool(func: Any, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await real_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(rs, "run_in_threadpool", recording_threadpool)
    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio_bytes",
        lambda *a, **k: _stt_result(text="usable transcript here", no_speech_prob=0.0),
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    resp = client.post("/api/stt", files={"audio": ("a.webm", VALID_BROWSER_AUDIO, "audio/webm")})
    assert resp.status_code == 200, resp.text
    assert "transcribe_browser_audio" in offloaded


def test_bh14_manual_analyze_offloaded_to_threadpool(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """BH14: the ~120s VLM call must run via run_in_threadpool, not block the loop."""
    output_dir, report_file, video_path = review_workspace

    offloaded: list[str] = []
    import screenscribe.review_server as rs

    real_threadpool = rs.run_in_threadpool

    async def recording_threadpool(func: Any, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(getattr(func, "__name__", repr(func)))
        return await real_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(rs, "run_in_threadpool", recording_threadpool)
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **k: SimpleNamespace(
            category="manual_capture",
            severity="low",
            summary="ok",
            issues_detected=[],
            suggested_fix="",
            affected_components=[],
            response_id="r",
            is_issue=True,
        ),
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    marker_id = client.post(
        "/api/manual-mark",
        json={"timestamp": 1.0, "frame_base64": _jpeg_b64(), "transcript": "t", "notes": ""},
    ).json()["marker_id"]

    resp = client.post(f"/api/manual-analyze/{marker_id}")
    assert resp.status_code == 200, resp.text
    assert "analyze_single_marker" in offloaded


# --- D (SPEC review-model-v2 §7 / D7): notes persistence through disk ---------


def test_notes_persist_through_disk_at_every_verdict_spec_section_7(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """SPEC review-model-v2 §7 (D7): a reviewer's note must persist through the
    SERVER/DISK round-trip — POST /api/save -> report.json -> fresh load
    (/api/review-state) — at EVERY verdict, exact text, never silently dropped.

    The critical SPEC example is the *rejected* note ("STT źle zrozumiało, core
    problem to X"): a rejection carries real information and must not vanish.
    F1 already proved notes survive the export (file://) path; this guards the
    untested server/disk path, especially for a rejected finding.
    """
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [
                    {"id": 1, "timestamp": 1.0, "category": "bug"},
                    {"id": 2, "timestamp": 2.0, "category": "stt"},
                    {"id": 3, "timestamp": 3.0, "category": "ui"},
                ],
            }
        ),
        encoding="utf-8",
    )

    # Exact SPEC §7 rejected-note wording — the note that "must not disappear".
    rejected_note = "STT źle zrozumiało, core problem to X"
    accepted_note = "real bug, blocks onboarding"
    none_note = "still thinking about this one"

    save_body = {
        "video": "screen.mov",
        "reviewer": "alex",
        "reviewed_at": "2026-06-27T12:00:00Z",
        "findings": [
            {
                "id": 1,
                "human_review": {
                    "verdict": "accepted",
                    "severity_override": None,
                    "notes": accepted_note,
                    "annotations": [],
                },
            },
            {
                "id": 2,
                "human_review": {
                    "verdict": "rejected",
                    "severity_override": None,
                    "notes": rejected_note,
                    "annotations": [],
                },
            },
            {
                "id": 3,
                "human_review": {
                    "verdict": "none",
                    "severity_override": None,
                    "notes": none_note,
                    "annotations": [],
                },
            },
        ],
        "manual_frames": [],
    }

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    assert client.post("/api/save", json=save_body).status_code == 200

    # The note must actually be serialized onto disk (not only echoed in memory).
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["human_review"]["findings"]["2"]["notes"] == rejected_note
    assert 2 in saved["human_review"]["rejected_ids"]

    # Cold load: a brand-new app/session over the same dir reads notes back.
    fresh_app = create_review_app(output_dir, report_file.name, video_path, _config())
    fresh_client = TestClient(fresh_app)
    state = fresh_client.get("/api/review-state").json()

    # Notes survive at EVERY verdict, exact text (SPEC §7 table).
    assert state["findings"]["1"]["verdict"] == "accepted"
    assert state["findings"]["1"]["notes"] == accepted_note
    assert state["findings"]["2"]["verdict"] == "rejected"
    assert state["findings"]["2"]["notes"] == rejected_note  # must not vanish
    assert state["findings"]["3"]["verdict"] == "none"
    assert state["findings"]["3"]["notes"] == none_note


def test_empty_notes_roundtrip_clean_without_crash(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """NEGATIVE (D acceptance): a finding with no note round-trips through the
    server/disk path as an empty string — no exception, no fail-open into a
    fabricated note. Missing the ``notes`` key entirely is equally clean."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [{"id": 1, "category": "bug"}, {"id": 2, "category": "ui"}],
            }
        ),
        encoding="utf-8",
    )

    save_body = {
        "video": "screen.mov",
        "reviewer": "alex",
        "reviewed_at": "2026-06-27T12:00:00Z",
        "findings": [
            # Explicit empty note.
            {"id": 1, "human_review": {"verdict": "rejected", "notes": ""}},
            # No ``notes`` key at all — must not crash, defaults to "".
            {"id": 2, "human_review": {"verdict": "accepted"}},
        ],
        "manual_frames": [],
    }

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    assert client.post("/api/save", json=save_body).status_code == 200

    fresh_app = create_review_app(output_dir, report_file.name, video_path, _config())
    fresh_client = TestClient(fresh_app)
    resp = fresh_client.get("/api/review-state")
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["findings"]["1"]["notes"] == ""
    assert state["findings"]["2"]["notes"] == ""


def test_bh30_crash_mid_write_leaves_old_report_intact(
    monkeypatch: pytest.MonkeyPatch, review_workspace: tuple[Path, Path, Path]
) -> None:
    """BH30: if the process crashes after the temp write but before os.replace,
    the previous report.json must stay fully intact (no truncation), and no
    temp file is leaked."""
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    old_payload = {"video": "screen.mov", "findings": [], "sentinel": "ORIGINAL-INTACT"}
    json_path.write_text(json.dumps(old_payload), encoding="utf-8")
    old_bytes = json_path.read_bytes()

    import screenscribe.review_server as rs

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated crash during atomic replace")

    monkeypatch.setattr(rs.os, "replace", boom)

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)
    # Add a marker so /api/save actually has state to write.
    client.post(
        "/api/manual-mark",
        json={"timestamp": 1.0, "frame_base64": _jpeg_b64(), "transcript": "t", "notes": ""},
    )

    resp = client.post("/api/save")
    assert resp.status_code == 500  # the crash surfaces as a failed save

    # Old report.json is byte-for-byte intact (atomic replace never landed).
    assert json_path.read_bytes() == old_bytes
    assert json.loads(json_path.read_text())["sentinel"] == "ORIGINAL-INTACT"
    # No leaked temp files from the aborted write.
    assert not list(output_dir.glob(".report-*.json.tmp"))


def test_review_state_returns_merged_from_ids(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """/api/review-state must hand back each survivor's ``merged_from_ids`` trail.

    A human-merge persists ``merged_from_ids`` onto the survivor's human_review
    (review_app.js buildReviewData/buildMergedReviewEntry). On a cold reload the
    client rebuilds the fold from /api/review-state. Before the fix the
    per-finding state returned only verdict/severity/notes/actionItems/
    annotations, so the trail was dropped and merged findings resurfaced
    standalone (the fold was lost).
    """
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [],
                "human_review": {
                    "findings": {
                        "17": {
                            "verdict": "accepted",
                            "notes": "survivor",
                            "merged_from_ids": [18, 26, 27],
                        },
                        "6": {"verdict": "accepted", "notes": "standalone"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    state = client.get("/api/review-state").json()
    findings = state.get("findings", {})

    survivor = findings.get("17", {})
    assert survivor.get("merged_from_ids") == [18, 26, 27], survivor
    # A standalone finding carries an empty trail, never a missing key.
    assert findings.get("6", {}).get("merged_from_ids") == []


def test_review_state_returns_member_annotations(
    review_workspace: tuple[Path, Path, Path],
) -> None:
    """/api/review-state must hand back the survivor's ``member_annotations``.

    When a reviewer annotates a finding and then absorbs it in a human-merge,
    save/export persists those marks as ``human_review.member_annotations`` on
    the survivor (review_app.js reconcileMergedReview/buildMergedReviewEntry).
    On a cold reload the absorbed member findings no longer exist standalone, so
    the per-finding review state is the ONLY surviving copy. Before the fix the
    state returned verdict/severity/notes/actionItems/annotations/merged_from_ids
    but dropped member_annotations, so the next save/export recomputed the merged
    review with empty members and silently lost that evidence.
    """
    import json

    output_dir, report_file, video_path = review_workspace
    json_path = output_dir / "screen_report.json"
    json_path.write_text(
        json.dumps(
            {
                "video": "screen.mov",
                "findings": [],
                "human_review": {
                    "findings": {
                        "17": {
                            "verdict": "accepted",
                            "notes": "survivor",
                            "merged_from_ids": [18],
                            "member_annotations": [
                                {
                                    "finding_id": 18,
                                    "annotations": [{"type": "text", "text": "MEMBER_MARK"}],
                                }
                            ],
                        },
                        "6": {"verdict": "accepted", "notes": "standalone"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    app = create_review_app(output_dir, report_file.name, video_path, _config())
    client = TestClient(app)

    state = client.get("/api/review-state").json()
    findings = state.get("findings", {})

    survivor = findings.get("17", {})
    member_anns = survivor.get("member_annotations")
    assert member_anns == [
        {"finding_id": 18, "annotations": [{"type": "text", "text": "MEMBER_MARK"}]}
    ], survivor
    # A standalone finding carries an empty list, never a missing key.
    assert findings.get("6", {}).get("member_annotations") == []
