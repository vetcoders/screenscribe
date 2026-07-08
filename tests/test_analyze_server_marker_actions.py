"""Tests for per-finding marker actions (Group C, Issue #5).

Each finding kafelek in the analyze dashboard exposes three actions:
- ``DELETE /api/marker/{id}`` removes the marker, its result, and frame file.
- ``PATCH /api/marker/{id}`` updates the editable ``notes`` field.
- ``POST /api/analyze/{id}`` re-runs vision analysis and overwrites the
  previous result (no duplicates).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig

# Same 1x1 PNG used in the frame-preview tests; small enough to inline.
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


def _mark_one(client: TestClient, *, notes: str = "") -> str:
    response = client.post(
        "/api/mark",
        json={
            "timestamp": 5.0,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": "broken button",
            "notes": notes,
        },
    )
    assert response.status_code == 200
    return str(response.json()["marker_id"])


def test_delete_marker_removes_from_session_and_frame_file(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark_one(client)

    # Frame file is on disk before delete.
    frame_response = client.get(f"/api/marker/{marker_id}/frame")
    assert frame_response.status_code == 200

    delete_response = client.delete(f"/api/marker/{marker_id}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"marker_id": marker_id, "deleted": True}

    # Marker is gone from listing and frame endpoint 404s.
    markers = client.get("/api/markers").json()
    assert markers == []
    assert client.get(f"/api/marker/{marker_id}/frame").status_code == 404


def test_delete_unknown_marker_returns_404(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.delete("/api/marker/does-not-exist")
    assert response.status_code == 404


def test_patch_marker_updates_notes_only(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark_one(client, notes="initial")

    response = client.patch(
        f"/api/marker/{marker_id}",
        json={"notes": "updated note text"},
    )
    assert response.status_code == 200
    # No prior analysis on this marker, so editing notes invalidates nothing.
    assert response.json() == {
        "marker_id": marker_id,
        "notes": "updated note text",
        "status": "pending",
        "analysis_invalidated": False,
    }

    markers = client.get("/api/markers").json()
    assert len(markers) == 1
    # Transcript must be untouched - we only allow editing notes.
    assert markers[0]["notes"] == "updated note text"
    assert markers[0]["transcript"] == "broken button"


def test_patch_unknown_marker_returns_404(sample_video: Path) -> None:
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.patch("/api/marker/does-not-exist", json={"notes": "x"})
    assert response.status_code == 404


def test_reanalyze_overwrites_previous_result_no_duplicate(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling POST /api/analyze/{id} twice must not produce a duplicate result.

    We stub ``analyze_finding_unified`` so the second call returns a different
    summary. After the second call, ``session.results[marker_id]`` must hold
    the new summary and the markers listing must still report exactly one
    entry (count unchanged).
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark_one(client)

    call_count = {"n": 0}

    def fake_analyze(
        detection: Any,
        screenshot_path: Path | None,
        config: Any,
        previous_response_id: str | None = None,
        force_text_only: bool = False,
    ) -> Any:
        call_count["n"] += 1
        # Build a minimal stub matching UnifiedFinding fields read by
        # analyze_single_marker.
        from screenscribe.unified_analysis import UnifiedFinding

        return UnifiedFinding(
            detection_id=0,
            screenshot_path=screenshot_path,
            timestamp=detection.segment.start,
            category="ui",
            is_issue=True,
            sentiment="problem",
            severity="medium",
            summary=f"summary call {call_count['n']}",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id=f"resp-{call_count['n']}",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    # First analyze.
    r1 = client.post(f"/api/analyze/{marker_id}")
    assert r1.status_code == 200
    assert r1.json()["status"] == "completed"
    assert r1.json()["result"]["summary"] == "summary call 1"

    # Re-analyze - same endpoint. Backend must overwrite, not append.
    r2 = client.post(f"/api/analyze/{marker_id}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"
    assert r2.json()["result"]["summary"] == "summary call 2"

    # Listing must still have exactly one marker (no duplicate finding).
    markers = client.get("/api/markers").json()
    assert len(markers) == 1
    assert markers[0]["marker_id"] == marker_id
    assert markers[0]["result"]["summary"] == "summary call 2"


def _stub_successful_finding(monkeypatch: pytest.MonkeyPatch, summary: str) -> None:
    """Patch analyze_finding_unified to return one minimal successful finding."""

    def fake_analyze(
        detection: Any,
        screenshot_path: Path | None,
        config: Any,
        previous_response_id: str | None = None,
        force_text_only: bool = False,
    ) -> Any:
        from screenscribe.unified_analysis import UnifiedFinding

        return UnifiedFinding(
            detection_id=0,
            screenshot_path=screenshot_path,
            timestamp=detection.segment.start,
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
            response_id="resp-stub",
        )

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", fake_analyze)


def test_failed_reanalysis_clears_stale_result(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-analysis that returns no finding must not leave the old result behind."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    _stub_successful_finding(monkeypatch, "first analysis")
    assert client.post(f"/api/analyze/{marker_id}").json()["status"] == "completed"
    assert client.get("/api/markers").json()[0]["result"]["summary"] == "first analysis"

    # Re-analysis now fails (model returns nothing).
    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        lambda *a, **kw: None,
    )
    assert client.post(f"/api/analyze/{marker_id}").json()["status"] == "error"

    marker = client.get("/api/markers").json()[0]
    assert marker["status"] == "error"
    assert "result" not in marker  # stale completed finding is gone


def test_reanalysis_exception_clears_stale_result(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-analysis that raises must also drop the previous result."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    _stub_successful_finding(monkeypatch, "first analysis")
    client.post(f"/api/analyze/{marker_id}")

    def boom(*_: object, **__: object) -> Any:
        raise RuntimeError("vlm exploded")

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", boom)
    assert client.post(f"/api/analyze/{marker_id}").json()["status"] == "error"
    assert "result" not in client.get("/api/markers").json()[0]


def test_editing_notes_invalidates_existing_analysis(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing notes must invalidate the analysis built from the old notes."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client, notes="old note")

    _stub_successful_finding(monkeypatch, "analysis from old note")
    client.post(f"/api/analyze/{marker_id}")
    assert client.get("/api/markers").json()[0]["result"]["summary"] == "analysis from old note"

    resp = client.patch(f"/api/marker/{marker_id}", json={"notes": "new note"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_invalidated"] is True
    assert body["status"] == "pending"

    marker = client.get("/api/markers").json()[0]
    assert marker["status"] == "pending"
    assert "result" not in marker


def test_unchanged_notes_keep_existing_analysis(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-submitting identical notes must not throw away a valid analysis."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client, notes="same note")

    _stub_successful_finding(monkeypatch, "kept analysis")
    client.post(f"/api/analyze/{marker_id}")

    resp = client.patch(f"/api/marker/{marker_id}", json={"notes": "same note"})
    assert resp.json()["analysis_invalidated"] is False
    assert client.get("/api/markers").json()[0]["result"]["summary"] == "kept analysis"


def test_patch_marker_sets_priority_override_and_persists(sample_video: Path) -> None:
    """A7b: PATCH severity records a manual priority that survives /api/markers.

    A pending marker has no severity until the operator picks one; once set it
    is exposed at the top level so the select can reflect it without a result.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    assert "severity" not in client.get("/api/markers").json()[0]

    resp = client.patch(f"/api/marker/{marker_id}", json={"severity": "high"})
    assert resp.status_code == 200
    # A priority change does not feed the VLM context, so it never invalidates.
    assert resp.json()["analysis_invalidated"] is False

    assert client.get("/api/markers").json()[0]["severity"] == "high"


def test_patch_marker_priority_overrides_and_mirrors_result_severity(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manual override wins over the VLM severity and is mirrored onto the
    result so the badge + export agree with the operator's call."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    _stub_successful_finding(monkeypatch, "analysis")  # severity defaults to "medium"
    client.post(f"/api/analyze/{marker_id}")
    assert client.get("/api/markers").json()[0]["result"]["severity"] == "medium"

    resp = client.patch(f"/api/marker/{marker_id}", json={"severity": "critical"})
    assert resp.status_code == 200
    assert resp.json()["analysis_invalidated"] is False

    marker = client.get("/api/markers").json()[0]
    assert marker["severity"] == "critical"
    assert marker["result"]["severity"] == "critical"  # mirrored onto the result


def test_patch_marker_priority_none_clears_override(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit "none" priority is a real override that CLEARS a prior pick.

    An operator who set a marker to "critical" must be able to take it back to
    "no priority". PATCH severity="none" persists literally on the marker and is
    mirrored onto any existing result so the badge disappears even when the VLM
    assigned a severity.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    _stub_successful_finding(monkeypatch, "analysis")  # VLM severity == "medium"
    client.post(f"/api/analyze/{marker_id}")

    # Operator first bumps priority, then clears it.
    client.patch(f"/api/marker/{marker_id}", json={"severity": "critical"})
    assert client.get("/api/markers").json()[0]["severity"] == "critical"

    resp = client.patch(f"/api/marker/{marker_id}", json={"severity": "none"})
    assert resp.status_code == 200
    marker = client.get("/api/markers").json()[0]
    assert marker["severity"] == "none"  # cleared, not reverted to the VLM value
    assert marker["result"]["severity"] == "none"  # mirrored onto the result too


def test_dashboard_priority_select_clear_option_uses_none_value(sample_video: Path) -> None:
    """The clear option must carry value "none" (not "") so the change handler's
    truthiness guard fires and the override can actually be removed. An empty
    string is falsy and would be silently dropped."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text
    # The clear option's value is "none"; the empty-string value is gone.
    assert "['none', 'analyze.severity_no_change']" in html
    assert "['', 'analyze.severity_no_change']" not in html


def test_analyze_preserves_operator_severity_override(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An override set BEFORE analysis must survive the analysis run.

    Operator sets a priority on a pending marker, then runs Analyze. The VLM
    returns its own severity, but the operator's call wins: result/export/badge
    must reflect the override, not the model value. Without re-applying the
    override on persist, analysis silently reverts to the VLM severity while the
    priority <select> still shows the override — a lie.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    # Override on a still-pending marker (no result block yet).
    client.patch(f"/api/marker/{marker_id}", json={"severity": "critical"})

    _stub_successful_finding(monkeypatch, "analysis")  # VLM severity == "medium"
    resp = client.post(f"/api/analyze/{marker_id}")
    assert resp.status_code == 200
    assert resp.json()["result"]["severity"] == "critical"  # override, not "medium"

    marker = client.get("/api/markers").json()[0]
    assert marker["result"]["severity"] == "critical"

    # Export rides the WorkItem spine; the override must be there too.
    export = client.get("/api/export").json()
    assert export["work_items"][0]["analysis"]["severity"] == "critical"


def test_analyze_after_notes_edit_keeps_override(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notes-edit -> pending -> analyze path must not drop a set override.

    Editing notes invalidates the analysis (marker back to pending) but leaves
    the operator's severity override on the marker. Re-analyzing must re-apply
    that override, not revert to the freshly-produced VLM severity.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client, notes="old note")

    _stub_successful_finding(monkeypatch, "first analysis")
    client.post(f"/api/analyze/{marker_id}")
    client.patch(f"/api/marker/{marker_id}", json={"severity": "low"})

    # Notes edit drops the result and sends the marker back to pending.
    invalidated = client.patch(f"/api/marker/{marker_id}", json={"notes": "new note"})
    assert invalidated.json()["analysis_invalidated"] is True

    # Re-analyze: VLM returns "medium" again, override "low" must win.
    resp = client.post(f"/api/analyze/{marker_id}")
    assert resp.json()["result"]["severity"] == "low"
    assert client.get("/api/markers").json()[0]["result"]["severity"] == "low"


def test_patch_marker_ignores_unknown_priority(sample_video: Path) -> None:
    """An out-of-vocabulary priority is ignored, never wedging the marker edit."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    resp = client.patch(f"/api/marker/{marker_id}", json={"severity": "bogus"})
    assert resp.status_code == 200
    assert "severity" not in client.get("/api/markers").json()[0]


def test_patch_marker_priority_only_leaves_notes_untouched(sample_video: Path) -> None:
    """A severity-only PATCH must not clobber an existing note (fields are
    updated independently)."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client, notes="keep me")

    client.patch(f"/api/marker/{marker_id}", json={"severity": "low"})
    marker = client.get("/api/markers").json()[0]
    assert marker["notes"] == "keep me"
    assert marker["severity"] == "low"


def test_dashboard_ships_priority_select_control(sample_video: Path) -> None:
    """The marker card wires the A7b priority control (select + change handler)."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text
    assert "marker-priority" in html
    assert 'class="severity-select"' in html
    assert "changeMarkerSeverity(" in html
    assert "analyze.action_change_priority" in html
    assert "analyze.severity_no_change" in html


def test_dashboard_includes_marker_action_buttons(sample_video: Path) -> None:
    """The HTML page wires the per-finding action toolbar JS hooks."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "deleteMarker(" in html
    assert "saveNote(" in html
    assert "toggleNoteEditor(" in html
    assert "reanalyzeMarker(" in html
    assert "marker-actions" in html


def test_dashboard_marker_cards_are_selectable_and_seekable(sample_video: Path) -> None:
    """Marker kafeleks must behave like selectable controls, not static cards."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "let activeMarkerId" in html
    assert "function selectMarker" in html
    assert 'role="button"' not in html
    assert 'tabindex="0"' in html
    assert 'aria-selected="${m.marker_id === activeMarkerId' in html
    assert "video.currentTime = nextTime" in html
    assert "event.key === 'Enter' || event.key === ' '" in html
    assert "event.stopPropagation()" in html


def test_dashboard_renders_clickable_marker_timeline_ticks(sample_video: Path) -> None:
    """Analyze video rail should mirror markers without duplicating state."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text

    assert 'id="markerTimeline"' in html
    assert 'id="markerTimelineTrack"' in html
    assert "let currentMarkers = []" in html
    assert "function renderMarkerTicks" in html
    assert "marker-tick" in html
    assert "track.innerHTML = ''" in html
    assert "marker.timestamp / duration * 100" in html
    assert "selectMarker(marker.marker_id, marker.timestamp)" in html
    assert "aria-label', t('analyze.marker_tick_aria'" in html
    assert "renderMarkerTicks(markers)" in html
    assert "renderMarkerTicks(currentMarkers)" in html


def test_dashboard_mark_frame_success_shows_temporary_status(sample_video: Path) -> None:
    """Successful Mark Frame should acknowledge the action without switching tabs."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "status_frame_marked" in html
    assert "showTemporaryStatus(t(hasMarkerNote ? 'analyze.status_frame_marked'" in html
    assert "findings-pulse" in html
    assert (
        "click()"
        not in html[html.find("markFrameBtn.addEventListener") : html.find("// Tab switching")]
    )


def test_dashboard_mark_frame_copy_explains_optional_notes(sample_video: Path) -> None:
    """Mark Frame copy/status must make note-less markers feel intentional."""
    app = create_analyze_app(sample_video, _config(language="pl"))
    client = TestClient(app)

    html = client.get("/").text

    assert "Add moment" in html
    assert "Dodaj moment" in html
    assert "Moment marked without note" in html
    assert "Moment oznaczony bez notatki" in html
    assert (
        "Pause the video and mark a moment. Add a voice or text note now or later"
        " — notes are optional" in html
    )
    assert (
        "Zatrzymaj film i oznacz moment. Notatkę głosową lub tekstową dodasz teraz"
        " lub później — jest opcjonalna" in html
    )
    assert "hasMarkerNote" in html
    assert "status_frame_marked_without_note" in html


def test_dashboard_note_editor_buttons_use_action_styles(sample_video: Path) -> None:
    """Save/Cancel in the inline editor should not fall back to browser defaults."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text

    assert ".marker-note-editor .editor-buttons button" in html
    assert ".marker-note-editor .editor-buttons .primary" in html
    assert ".marker-note-editor .editor-buttons .secondary" in html
    # P2-9: note-editor buttons use event delegation via data-action (no inline
    # onclick with raw interpolation). The delegated handler routes cancel-note /
    # save-note from data-action + data-marker-id.
    assert 'class="secondary" data-action="cancel-note"' in html
    assert 'class="primary" data-action="save-note"' in html


def test_dashboard_maps_marker_result_labels_for_display(sample_video: Path) -> None:
    """Technical category/severity values should not leak raw into the UI."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text

    assert "function formatMarkerCategory" in html
    assert "function formatMarkerSeverity" in html
    assert "Manual moment" in html
    assert "No severity" in html
    assert "formatMarkerCategory(m.result.category)" in html
    assert "formatMarkerSeverity(m.result.severity)" in html


def test_dashboard_does_not_duplicate_shared_spacebar_handler(sample_video: Path) -> None:
    """Analyze keeps modal Escape handling separate from native video controls."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text
    modal_keydown = html[
        html.find("// Frame preview modal - close") : html.find("// Language toggle")
    ]

    assert "function isTextEntryElement" not in html
    assert "event.key === ' '" not in modal_keydown
    assert "video.paused" not in modal_keydown


def test_analyze_server_loads_dashboard_controller_from_asset(sample_video: Path) -> None:
    """The shell renderer, not the FastAPI endpoint, owns the dashboard JS asset."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text
    server_source = Path("screenscribe/analyze_server.py").read_text(encoding="utf-8")
    renderer_source = Path("screenscribe/shell/renderer.py").read_text(encoding="utf-8")

    assert "class VoiceRecorder" in html
    assert "function renderMarkerTicks" in html
    assert "load_js_analyze_dashboard" in renderer_source
    assert "load_js_analyze_dashboard" not in server_source
    assert "{js_analyze_dashboard}" not in server_source
    assert "class VoiceRecorder {{" not in server_source
    assert "document.addEventListener('DOMContentLoaded', () => {{" not in server_source


def test_markers_payload_includes_notes_field(sample_video: Path) -> None:
    """The markers listing must surface ``notes`` so the editor can pre-fill."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    _mark_one(client, notes="hello world")

    markers = client.get("/api/markers").json()
    assert len(markers) == 1
    assert markers[0]["notes"] == "hello world"


def test_delete_also_removes_analysis_result(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a marker must wipe its entry from session.results too.

    Otherwise re-marking with the same id (unlikely but possible across
    sessions) would inherit a stale analysis. Belt and suspenders.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark_one(client)

    def fake_analyze(detection: Any, screenshot_path: Path | None, **_: Any) -> Any:
        from screenscribe.unified_analysis import UnifiedFinding

        return UnifiedFinding(
            detection_id=0,
            screenshot_path=screenshot_path,
            timestamp=0.0,
            category="ui",
            is_issue=True,
            sentiment="problem",
            severity="low",
            summary="x",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="resp",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    client.post(f"/api/analyze/{marker_id}")
    client.delete(f"/api/marker/{marker_id}")

    # Re-mark with a new id and verify export contains no leftover analysis.
    new_id = _mark_one(client)
    export = client.get("/api/export").json()
    assert len(export["work_items"]) == 1
    assert export["work_items"][0]["id"] == new_id
    # The new marker has no analysis attached yet (WorkItem.analysis stays empty).
    assert export["work_items"][0]["analysis"] == {}


def test_analyze_export_rides_workitem_spine_without_frame_base64(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /api/export findings JSON rides the WorkItem spine, metadata-only.

    Each marker becomes a WorkItem (source=analyze_marker, status=processing,
    analysis preserved) under "work_items" -- ONE output contract. The frame
    BINARY (base64) must NOT be inlined: the findings JSON stays light, diffable
    and agent-readable; full frames belong to the ZIP/manifest, not here. (This
    matches the established canon that the export JSON is one agent-readable
    contract, not a binary container.)
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    marker_id = _mark_one(client, notes="check this")

    def fake_analyze(detection: Any, screenshot_path: Path | None, **_: Any) -> Any:
        from screenscribe.unified_analysis import UnifiedFinding

        return UnifiedFinding(
            detection_id=0,
            screenshot_path=screenshot_path,
            timestamp=0.0,
            category="ui",
            is_issue=True,
            sentiment="problem",
            severity="low",
            summary="button is broken",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="resp",
        )

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )
    assert client.post(f"/api/analyze/{marker_id}").status_code == 200

    response = client.get("/api/export")
    assert response.status_code == 200
    export = response.json()

    # One output contract: WorkItem-shaped items under "work_items".
    assert "markers" not in export  # the old hand-rolled marker shape is gone
    assert len(export["work_items"]) == 1
    item = export["work_items"][0]
    assert item["id"] == marker_id
    assert item["source"] == "analyze_marker"
    assert item["status"] == "processing"  # export status, not the live marker state
    assert item["transcript"] == "broken button"
    assert item["notes"] == "check this"
    assert item["analysis"]["summary"] == "button is broken"  # analysis preserved

    # No frame BINARY in the findings JSON (light / diffable / agent-readable).
    assert "base64" not in item["frame"]
    assert "base64" not in response.text  # belt + suspenders: no blob anywhere


def test_export_does_not_leak_absolute_video_path(sample_video: Path) -> None:
    """Download JSON references the video by basename, never the absolute local
    input path (privacy: shared artifacts must not leak the filesystem)."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    _mark_one(client)

    response = client.get("/api/export")
    payload = response.json()
    assert payload["video"] == sample_video.name
    assert "/" not in payload["video"]
    assert str(sample_video) not in response.text


def test_concurrent_analysis_of_one_marker_returns_409(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second /api/analyze for a marker whose VLM call is already in flight
    must 409, invoke the VLM exactly once, and leave exactly one result.

    Without the concurrent-analysis guard the duplicate request would start a
    second VLM run (double cost) and race two writers onto ``session.results``.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    marker_id = _mark_one(client)

    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def fake_analyze(
        detection: Any,
        screenshot_path: Path | None,
        config: Any,
        previous_response_id: str | None = None,
        force_text_only: bool = False,
    ) -> Any:
        calls["n"] += 1
        started.set()
        assert release.wait(timeout=5)
        from screenscribe.unified_analysis import UnifiedFinding

        return UnifiedFinding(
            detection_id=0,
            screenshot_path=screenshot_path,
            timestamp=0.0,
            category="ui",
            is_issue=True,
            sentiment="problem",
            severity="medium",
            summary="only-run",
            action_items=[],
            affected_components=[],
            suggested_fix="",
            ui_elements=[],
            issues_detected=[],
            accessibility_notes=[],
            design_feedback="",
            technical_observations="",
            response_id="resp",
        )

    monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified", fake_analyze)

    holder: dict[str, Any] = {}

    def first() -> None:
        holder["r1"] = client.post(f"/api/analyze/{marker_id}")

    worker = threading.Thread(target=first)
    worker.start()
    try:
        # First VLM call is now in flight; the marker is "analyzing".
        assert started.wait(timeout=5)
        # Duplicate request while the first is in flight must be rejected.
        r2 = client.post(f"/api/analyze/{marker_id}")
        assert r2.status_code == 409
    finally:
        release.set()
        worker.join(timeout=5)

    assert holder["r1"].status_code == 200
    assert holder["r1"].json()["status"] == "completed"
    assert calls["n"] == 1  # VLM invoked exactly once, not twice

    markers = client.get("/api/markers").json()
    assert len(markers) == 1
    assert markers[0]["result"]["summary"] == "only-run"


def test_finalize_jobs_registry_is_bounded(sample_video: Path) -> None:
    """The finalize-job registry must not grow without limit: registering a new
    job evicts the oldest FINISHED jobs down to ``MAX_FINALIZE_JOBS``."""
    from screenscribe.analyze_server import MAX_FINALIZE_JOBS, FinalizeJob

    app = create_analyze_app(sample_video, _config())
    session = app.state.session

    # Pre-fill with more than the cap of finished jobs, ascending age.
    for i in range(MAX_FINALIZE_JOBS + 10):
        job = FinalizeJob(job_id=f"job-{i}")
        job.status = "completed"
        job.started_at = float(i)
        session.finalize_jobs[job.job_id] = job

    client = TestClient(app)
    # Starting a finalize registers a fresh job and triggers eviction under lock.
    resp = client.post("/api/finalize/start")
    assert resp.status_code == 200

    assert len(session.finalize_jobs) <= MAX_FINALIZE_JOBS
    # Oldest finished jobs were evicted; the newest pre-filled ones survive.
    assert "job-0" not in session.finalize_jobs
    assert f"job-{MAX_FINALIZE_JOBS + 9}" in session.finalize_jobs
