"""Interactive review server for HTML Pro reports.

Turns the report viewer into a lightweight local web app:
- serves the generated report and linked video files
- provides STT for spoken manual notes
- lets reviewers capture manual frames and run VLM analysis on demand
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .server_common import (
    MAX_AUDIO_BYTES,
    VALID_MARKER_SEVERITIES,
    advance_response_id_cas,
    install_stt_upload_cap,
    read_upload_capped,
    sanitized_error,
    serialize_stt_result,
    transcribe_browser_audio,
    validate_browser_stt_result,
    validate_browser_stt_upload,
)
from .work_item import WorkItem, from_manual_frame, normalize_verdict

if TYPE_CHECKING:
    from .config import ScreenScribeConfig

logger = logging.getLogger(__name__)

# Security limits and image format markers. ``MAX_AUDIO_BYTES`` is imported
# (re-exported) from server_common so existing importers/tests keep working.
MAX_FRAME_BASE64_CHARS = 20_000_000  # ~15 MB decoded frame payload cap
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# The generated report's <video src="..."> — captured so the review server can
# sign it at serve time (see serve_report). Groups: (prefix incl. src="), value,
# (closing quote). Lazy [^>]*? keeps the match inside the single <video> tag.
_VIDEO_SRC_RE = re.compile(r'(<video\b[^>]*?\bsrc=")([^"]*)(")', re.IGNORECASE)


class ManualFrameRequest(BaseModel):
    """Request body for manual frame capture inside review mode."""

    timestamp: float
    frame_base64: str = Field(..., max_length=MAX_FRAME_BASE64_CHARS)
    transcript: str = ""
    notes: str = ""


class ManualFrameUpdateRequest(BaseModel):
    """Partial update for an already-marked manual frame (BH28).

    Both fields are optional: only the ones present are applied, so the client can
    patch ``notes`` without resending ``transcript`` (and vice versa). ``None``
    means "leave unchanged"; an explicit ``""`` clears the field.
    """

    transcript: str | None = None
    notes: str | None = None
    # R14: the reviewer's manual priority override. ``None`` means "leave
    # unchanged"; a value in VALID_MARKER_SEVERITIES sets it (incl. the explicit
    # "none" that clears a prior pick).
    severity: str | None = None


@dataclass
class ManualFrameMarker:
    """A frame manually captured by the reviewer.

    ``frame_base64`` holds the pixels for the live session (analysis, immediate
    hydrate). ``frame_path`` is the DURABLE source: a relative reference into the
    output dir (e.g. ``manual_frames/<id>.jpg``) written to disk at mark time, so
    the image survives a cold load / fresh browser / server restart even after
    the in-memory base64 is gone.
    """

    marker_id: str
    timestamp: float
    frame_base64: str
    transcript: str = ""
    notes: str = ""
    status: str = "pending"  # pending, analyzing, completed, error
    frame_path: str = ""  # relative path into output_dir, durable image source
    # R14: reviewer's manual priority override (A7b mirror). ``None`` until the
    # reviewer picks a value from the per-card priority control; once set it wins
    # over the VLM-assigned ``ManualFrameResult.severity`` for display + export.
    severity: str | None = None


@dataclass
class ManualFrameResult:
    """Unified VLM result for a manual frame."""

    marker_id: str
    timestamp: float
    category: str = "manual_capture"
    severity: str = "medium"
    summary: str = ""
    issues_detected: list[str] = field(default_factory=list)
    suggested_fix: str = ""
    affected_components: list[str] = field(default_factory=list)
    response_id: str = ""


@dataclass
class ReviewSession:
    """In-memory session state for the local review app."""

    output_dir: Path
    report_filename: str
    video_path: Path
    markers: dict[str, ManualFrameMarker] = field(default_factory=dict)
    results: dict[str, ManualFrameResult] = field(default_factory=dict)
    last_response_id: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)


def _manual_frame_data_url(frame_base64: str) -> str:
    """Build a renderable data URL from a stored manual-frame base64 blob.

    Frame pixels live server-side as raw base64 (ManualFrameMarker); the browser
    needs a ``data:`` URL to put in an <img src>. Sniff JPEG/PNG from the leading
    bytes and default to JPEG (manual captures are JPEG/PNG by construction).
    """
    mime = "image/jpeg"
    try:
        head = base64.b64decode(frame_base64[:16], validate=False)[:8]
    except (ValueError, TypeError):
        head = b""
    if head.startswith(b"\x89PNG"):
        mime = "image/png"
    return f"data:{mime};base64,{frame_base64}"


MANUAL_FRAMES_DIRNAME = "manual_frames"


def _validated_image_ext(frame_bytes: bytes) -> str:
    """Return the file extension for a validated JPEG/PNG, else raise 400.

    Magic-byte validation is the durability gate: only real image bytes reach the
    disk store, so a cold-load read can trust ``frame_path`` points at a renderable
    image (never a base64 typo or a non-image blob).
    """
    if frame_bytes.startswith(JPEG_MAGIC):
        return "jpg"
    if frame_bytes.startswith(PNG_MAGIC):
        return "png"
    raise HTTPException(status_code=400, detail="Frame must be JPEG or PNG.")


def store_manual_frame_image(output_dir: Path, marker_id: str, frame_base64: str) -> str:
    """Decode + magic-validate a manual frame and write it under output_dir.

    Returns the relative ``manual_frames/<marker_id>.<ext>`` reference to persist
    on the marker / in report.json. Raises HTTP 400 on bad base64 or non-image
    bytes — the disk is the durable source of truth, so junk never lands there.
    """
    try:
        frame_bytes = base64.b64decode(frame_base64, validate=True)
    except Exception as exc:  # binascii.Error and friends
        raise HTTPException(status_code=400, detail="Invalid base64 frame data.") from exc
    ext = _validated_image_ext(frame_bytes)
    frames_dir = output_dir / MANUAL_FRAMES_DIRNAME
    frames_dir.mkdir(parents=True, exist_ok=True)
    rel_path = f"{MANUAL_FRAMES_DIRNAME}/{marker_id}.{ext}"
    (output_dir / rel_path).write_bytes(frame_bytes)
    return rel_path


def _remove_manual_frame_image(output_dir: Path, frame_path: str) -> bool:
    """Delete a stored manual-frame image off disk when its marker is removed.

    Returns ``True`` when a file was unlinked, ``False`` otherwise. Idempotent:
    a missing file is not an error (``missing_ok``). The path is contained inside
    ``output_dir`` (frame_path is server-authored, but a discarded delete must
    never reach outside the review dir) so an unexpected/absolute reference is
    ignored rather than unlinking something it shouldn't.
    """
    if not frame_path:
        return False
    candidate = (output_dir / frame_path).resolve()
    base = output_dir.resolve()
    if base != candidate and base not in candidate.parents:
        return False
    existed = candidate.is_file()
    candidate.unlink(missing_ok=True)
    return existed


def _sweep_orphan_manual_frames(output_dir: Path, keep: set[str]) -> int:
    """Unlink stored manual-frame images no longer referenced by any live state.

    A frame written at mark time (``store_manual_frame_image``) but never saved
    into report.json and never explicitly deleted would otherwise linger on disk
    forever: HTTP-reachable through the static mount (a privacy leak) and pure
    disk litter. At save time the full live reference set is known — every
    ``frame_path`` persisted into report.json plus every live session marker
    (pending / unanalyzed included) — so any ``manual_frames/*`` file outside it
    is orphaned and swept.

    ``keep`` holds server-authored relative references
    (``manual_frames/<id>.<ext>``). Each on-disk file is unlinked only when it is
    contained inside ``output_dir`` (the same base-in-parents guard as
    ``_remove_manual_frame_image``), so a sweep can never reach outside the
    review dir. Idempotent and crash-safe: a missing file is not an error.
    Returns the count of files removed.
    """
    frames_dir = output_dir / MANUAL_FRAMES_DIRNAME
    if not frames_dir.is_dir():
        return 0
    base = output_dir.resolve()
    keep_resolved = {(output_dir / rel).resolve() for rel in keep if rel}
    removed = 0
    for entry in frames_dir.iterdir():
        if not entry.is_file():
            continue
        candidate = entry.resolve()
        if base != candidate and base not in candidate.parents:
            continue
        if candidate in keep_resolved:
            continue
        entry.unlink(missing_ok=True)
        removed += 1
    return removed


def _data_url_from_disk(output_dir: Path, frame_path: str) -> str | None:
    """Read a stored manual-frame image off disk and build a renderable data URL.

    Returns ``None`` when the file is missing (regen to a new version dir, manual
    deletion, partial copy) OR when the bytes on disk are not a real JPEG/PNG:
    callers surface a missing-image signal instead of crashing the load or
    serving a ``data:image`` the browser can never decode. The mime is derived
    from the magic bytes, not the file extension — a file swapped on disk for
    junk (or for a different real format) is re-validated on read rather than
    blindly trusted from its ``.jpg``/``.png`` name.
    """
    if not frame_path:
        return None
    candidate = (output_dir / frame_path).resolve()
    # Keep the read inside output_dir — frame_path is server-authored, but a
    # defensive containment check costs nothing and matches the static mount.
    base = output_dir.resolve()
    if base != candidate and base not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    raw = candidate.read_bytes()
    # Re-validate by magic bytes (same gate as the write path's
    # _validated_image_ext). Trusting the extension would let a swapped junk file
    # produce a broken data:image; instead, signal missing so the client shows an
    # imageMissing placeholder.
    if raw.startswith(PNG_MAGIC):
        mime = "image/png"
    elif raw.startswith(JPEG_MAGIC):
        mime = "image/jpeg"
    else:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def hydrate_state_with_session_frames(
    state: dict[str, Any], markers: list[ManualFrameMarker]
) -> None:
    """Make every in-memory manual frame restorable from review-state.

    A frame added via /api/manual-mark lives only in the server session until a
    save, so review-state must still hand it back with a renderable
    ``frameDataUrl``. This is what lets the client keep base64 frames out of its
    localStorage draft: the server session, not the browser cache, owns the
    pixels. Disk frames that already carry an image are left untouched.
    """
    frames = state.get("manualFrames")
    if not isinstance(frames, list):
        frames = []
        state["manualFrames"] = frames
    by_id = {
        str(frame.get("marker_id")): frame
        for frame in frames
        if isinstance(frame, dict) and frame.get("marker_id") is not None
    }
    for marker in markers:
        if not marker.frame_base64:
            continue
        existing = by_id.get(marker.marker_id)
        if existing is not None:
            if not existing.get("frameDataUrl"):
                existing["frameDataUrl"] = _manual_frame_data_url(marker.frame_base64)
            # R14: surface a server-side priority override onto a frame the client
            # already knows about, so the card's select reflects it after a reload
            # even when the frame row itself did not carry the override.
            if marker.severity is not None and existing.get("severity") is None:
                existing["severity"] = marker.severity
            continue
        frames.append(
            {
                "marker_id": marker.marker_id,
                "timestamp": marker.timestamp,
                "transcript": marker.transcript,
                "notes": marker.notes,
                "severity": marker.severity,
                "frameDataUrl": _manual_frame_data_url(marker.frame_base64),
                "result": None,
            }
        )


def create_review_app(
    output_dir: Path,
    report_filename: str,
    video_path: Path,
    config: ScreenScribeConfig,
) -> FastAPI:
    """Create the interactive report review app."""

    app = FastAPI(
        title="Screenscribe Review",
        description="Interactive review server for screenscribe reports",
        version=__version__,
    )

    # Localhost-only security: per-process session token + Host/Origin guards on
    # /api/* (the token is handed to the UI via the URL fragment). The source
    # video is served into a <video src>, which can't carry the token header, so
    # both video paths authenticate via the signed ``st`` query parameter.
    from .server_security import generate_session_token, install_security, video_access_token

    install_security(
        app,
        generate_session_token(),
        video_paths=frozenset({"/video", f"/{video_path.name}"}),
    )

    install_stt_upload_cap(app)

    session = ReviewSession(
        output_dir=output_dir.resolve(),
        report_filename=report_filename,
        video_path=video_path.resolve(),
    )

    # Serializes the whole /api/save read-modify-write cycle (load report.json ->
    # merge session markers + posted human review -> atomic write). The atomic
    # write (BH30 fsync+replace) already guards against a torn file, but two
    # concurrent saves each load->merge->write independently: the second writer's
    # load predates the first writer's replace, so it overwrites report.json with
    # a report_data that never saw the first save's contribution (last-writer-wins
    # drops a reviewer's verdict). The session's threading.RLock does NOT help
    # here — every save coroutine runs on the one event-loop thread, so the RLock
    # is reentrant and never blocks a second coroutine. A dedicated asyncio.Lock
    # is the primitive that actually serializes overlapping async saves. Saves are
    # short local file I/O, so full serialization is cheap.
    save_lock = asyncio.Lock()

    def report_json_path() -> Path:
        report_path = session.output_dir / session.report_filename
        if report_path.exists():
            return report_path.with_suffix(".json")
        return session.output_dir / "report.json"

    def load_report_json() -> tuple[Path, dict[str, Any]]:
        import json

        json_path = report_json_path()
        if not json_path.exists():
            logger.error("Report JSON not found at %s", json_path)
            raise HTTPException(status_code=404, detail="Report JSON not found.")
        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="Report JSON is invalid.")
        return json_path, data

    def _finding_verdict(human: dict[str, Any]) -> str:
        # legacy migration: confirmed -> verdict — read either the new
        # `verdict` string or the old boolean `confirmed` from report.json.
        raw_verdict = human["verdict"] if "verdict" in human else human.get("confirmed")
        return normalize_verdict(raw_verdict)

    def review_finding_state(human: dict[str, Any]) -> dict[str, Any]:
        return {
            "verdict": _finding_verdict(human),
            "severity": human.get("severity_override") or human.get("severity"),
            "notes": human.get("notes", ""),
            "actionItems": human.get("action_items") or human.get("actionItems", ""),
            "annotations": list(human.get("annotations") or []),
            # Human-merge provenance: without it a cold reload cannot rebuild the
            # fold (computeMergedFindings keys on the survivor's merged_from_ids),
            # so merged-away findings would resurface standalone.
            "merged_from_ids": list(human.get("merged_from_ids") or []),
            # Absorbed members' annotations, kept as evidence on the survivor (they
            # are never rasterized onto the survivor's own image). On a cold reload
            # the merged-away member findings are gone from reportState, so this is
            # the ONLY surviving copy — without hydrating it the next save/export
            # would recompute the merged review with empty members and silently
            # drop the evidence. Mirror of the merged_from_ids hydrate above.
            "member_annotations": list(human.get("member_annotations") or []),
        }

    def work_item_from_review_finding(finding: dict[str, Any]) -> WorkItem:
        human = dict(finding.get("human_review") or {})
        analysis = dict(finding.get("unified_analysis") or {})
        frame: dict[str, Any] = {}
        if finding.get("screenshot_path"):
            frame["path"] = finding["screenshot_path"]
        return WorkItem(
            id=str(finding.get("id", "")),
            source="review_detection",
            timestamp=finding.get("timestamp"),
            transcript=str(finding.get("text") or ""),
            category=str(finding.get("category") or ""),
            status="reviewed" if _finding_verdict(human) != "none" else "pending",
            frame=frame,
            analysis=analysis,
            human_review=human,
        )

    def work_item_from_review_manual_frame(
        frame: dict[str, Any], *, reviewer: str, reviewed_at: str
    ) -> WorkItem:
        result = dict(frame.get("result") or {})
        # Prefer the durable disk reference; fall back to an inline data URL only
        # if a (legacy) frame still carries one. report.json should reference the
        # image by path, not embed the pixels.
        frame_ref: dict[str, Any] = {}
        if frame.get("frame_path"):
            frame_ref["path"] = frame["frame_path"]
        elif frame.get("frameDataUrl"):
            frame_ref["data_url"] = frame["frameDataUrl"]
        return WorkItem(
            id=str(frame.get("marker_id", "")),
            source="review_manual_frame",
            timestamp=frame.get("timestamp"),
            transcript=str(frame.get("transcript") or ""),
            notes=str(frame.get("notes") or ""),
            category=str(result.get("category") or "manual_capture"),
            status=str(frame.get("status") or "completed"),
            frame=frame_ref,
            analysis=result,
            human_review={
                "annotations": list(frame.get("annotations") or []),
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
            },
            export_meta={"timestamp_formatted": frame.get("timestamp_formatted", "")},
        )

    def _restore_manual_frame_image(frame: dict[str, Any]) -> None:
        """Rehydrate a manual frame's renderable image from the disk store.

        ``frame_path`` is the durable source: read it off disk and attach a
        ``frameDataUrl`` so a cold load restores the image without any inline
        base64 in report.json. A missing file is reported (``imageMissing``), not
        crashed on, so the rest of review-state still loads.
        """
        if frame.get("frameDataUrl"):
            return
        frame_path = frame.get("frame_path")
        if not isinstance(frame_path, str) or not frame_path:
            return
        data_url = _data_url_from_disk(session.output_dir, frame_path)
        if data_url is None:
            frame["imageMissing"] = True
            return
        frame["frameDataUrl"] = data_url

    def build_review_state_from_report(report_data: dict[str, Any]) -> dict[str, Any]:
        human = report_data.get("human_review") if isinstance(report_data, dict) else {}
        human = human if isinstance(human, dict) else {}
        findings = {
            str(fid): review_finding_state(review)
            for fid, review in (human.get("findings") or {}).items()
            if isinstance(review, dict)
        }

        manual_frames = human.get("manual_frames")
        if isinstance(manual_frames, list):
            # Primary path frames may carry a frame_path persisted by /api/save;
            # work on copies so we never mutate the loaded report_data in place.
            manual_frames = [dict(frame) for frame in manual_frames if isinstance(frame, dict)]
        else:
            manual_review = report_data.get("manual_review") or {}
            markers = manual_review.get("markers") or []
            results = {
                str(result.get("marker_id")): result
                for result in manual_review.get("results", [])
                if isinstance(result, dict) and result.get("marker_id") is not None
            }
            manual_frames = []
            for marker in markers:
                if not isinstance(marker, dict):
                    continue
                marker_id = str(marker.get("marker_id", ""))
                payload = dict(marker)
                if marker_id in results:
                    payload["result"] = results[marker_id]
                manual_frames.append(payload)

        for frame in manual_frames:
            _restore_manual_frame_image(frame)

        return {
            "findings": findings,
            "manualFrames": manual_frames,
            "reviewer": human.get("reviewer", ""),
            "modified": False,
        }

    def analyze_single_marker(marker_id: str) -> dict[str, Any]:
        """Run unified analysis on one manual frame."""
        from .detect import Detection
        from .transcribe import Segment
        from .unified_analysis import analyze_finding_unified

        with session.lock:
            if marker_id not in session.markers:
                raise HTTPException(status_code=404, detail="Manual frame not found")
            marker = session.markers[marker_id]
            # Concurrent-analysis guard (mirror analyze-side): a second analyze
            # request for a marker whose VLM call is already in flight would
            # double the VLM cost and race two writers onto one result. Reject the
            # duplicate with 409 instead of starting a second run; the in-flight
            # analysis keeps ownership of the marker.
            if marker.status == "analyzing":
                raise HTTPException(
                    status_code=409, detail="Manual frame analysis already in progress"
                )
            marker.status = "analyzing"
            previous_response_id = session.last_response_id

        segment = Segment(
            id=0,
            start=marker.timestamp,
            end=marker.timestamp + 1.0,
            text=marker.transcript or marker.notes or "User captured this frame during review.",
        )
        detection = Detection(
            segment=segment,
            category="manual_capture",
            keywords_found=[],
            context=f"User comment: {marker.transcript}\nNotes: {marker.notes}",
        )

        try:
            frame_bytes = base64.b64decode(marker.frame_base64, validate=True)
        except Exception as exc:
            with session.lock:
                # BH48 mirror: a failed (re)analysis must not leave a stale
                # completed result behind under an error marker.
                session.results.pop(marker_id, None)
                marker.status = "error"
            logger.warning("Manual frame %s base64 decode failed: %s", marker_id, exc)
            raise HTTPException(status_code=400, detail="Invalid base64 frame data.") from exc

        if not (frame_bytes.startswith(JPEG_MAGIC) or frame_bytes.startswith(PNG_MAGIC)):
            with session.lock:
                session.results.pop(marker_id, None)
                marker.status = "error"
            raise HTTPException(status_code=400, detail="Frame must be JPEG or PNG.")

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(frame_bytes)
            screenshot_path = Path(tmp.name)

        try:
            finding = analyze_finding_unified(
                detection=detection,
                screenshot_path=screenshot_path,
                config=config,
                previous_response_id=previous_response_id,
            )

            if not finding:
                with session.lock:
                    # BH48 mirror: drop any prior result so a failed re-analysis
                    # can't leave a stale completed finding under an error marker.
                    session.results.pop(marker_id, None)
                    marker.status = "error"
                return {"marker_id": marker_id, "status": "error", "error": "Analysis failed"}

            # BH48 mirror (analyze-side BH48): a finding the parser flagged
            # ``confidence == "degraded"`` is a schema-drift / raw-text fallback,
            # NOT a real analysis. Treat it as failure instead of persisting a
            # falsely "completed" finding the reviewer would trust.
            if getattr(finding, "confidence", "high") == "degraded":
                with session.lock:
                    session.results.pop(marker_id, None)
                    marker.status = "error"
                return {
                    "marker_id": marker_id,
                    "status": "error",
                    "error": "Analysis degraded (parse failure)",
                }

            result = ManualFrameResult(
                marker_id=marker_id,
                timestamp=marker.timestamp,
                category=finding.category,
                severity=finding.severity,
                summary=finding.summary,
                issues_detected=finding.issues_detected,
                suggested_fix=finding.suggested_fix,
                affected_components=finding.affected_components,
                response_id=finding.response_id,
            )
            with session.lock:
                # BH40 mirror: the marker can be deleted mid-flight
                # (delete_manual_frame pops it under the same lock while the VLM
                # call ran unlocked). Writing a result for an absent marker would
                # resurrect a ghost finding in results/review-state/export.
                # Re-check presence before persisting.
                if marker_id not in session.markers:
                    session.results.pop(marker_id, None)
                    return {
                        "marker_id": marker_id,
                        "status": "error",
                        "error": "Manual frame deleted during analysis",
                    }
                # R14 (mirror of analyze-side A7b / f3e482e): a reviewer priority
                # override set on the marker (incl. the explicit "none" that
                # clears it) wins over the freshly VLM-assigned severity. Re-apply
                # it here so a (re)analysis never silently reverts the badge/export
                # to the model severity while the card's select still shows the
                # override.
                if marker.severity is not None:
                    result.severity = marker.severity
                session.results[marker_id] = result
                # BH45 (mirror of analyze-side BH29): only advance the
                # conversation chain when the finding actually carried a
                # response_id. An empty id would clobber the valid chain head
                # with a blank, breaking the previous_response_id chaining the
                # next manual-frame analysis relies on. Compare-and-set against
                # previous_response_id (read at :537): a concurrent analysis that
                # finished first owns the newer head, so this call must not
                # overwrite it with an id chained off the stale head.
                advance_response_id_cas(
                    session, previous_response_id, finding.response_id, logger=logger
                )
                marker.status = "completed"

            return {
                "marker_id": marker_id,
                "status": "completed",
                "timestamp": marker.timestamp,
                "transcript": marker.transcript,
                "notes": marker.notes,
                "result": {
                    "category": result.category,
                    "severity": result.severity,
                    "summary": result.summary,
                    "issues_detected": result.issues_detected,
                    "suggested_fix": result.suggested_fix,
                    "affected_components": result.affected_components,
                    "response_id": result.response_id,
                },
            }
        except Exception as exc:
            with session.lock:
                # BH48 mirror: a raised re-analysis must not keep the previous
                # completed result around.
                session.results.pop(marker_id, None)
                marker.status = "error"
            err = sanitized_error(exc, "marker_analysis_failed")
            return {
                "marker_id": marker_id,
                "status": "error",
                "error": err["message"],
                "error_code": err["error_code"],
            }
        finally:
            screenshot_path.unlink(missing_ok=True)

    @app.get("/")
    async def review_root() -> RedirectResponse:
        """Redirect the root URL to the generated report."""
        return RedirectResponse(url=f"/{report_filename}", status_code=307)

    def selected_video_response() -> FileResponse:
        """Serve only the selected source video, even when it lives outside output_dir."""
        if not session.video_path.exists():
            raise HTTPException(status_code=404, detail="Video not found.")
        media_type, _ = mimetypes.guess_type(session.video_path.name)
        return FileResponse(
            session.video_path,
            media_type=media_type or "application/octet-stream",
            filename=session.video_path.name,
        )

    @app.get("/video")
    async def serve_selected_video() -> FileResponse:
        """Return the source video via a stable local endpoint."""
        return selected_video_response()

    @app.get(f"/{video_path.name}")
    async def serve_selected_video_by_filename() -> FileResponse:
        """Keep generated report video URLs working without following arbitrary symlinks."""
        return selected_video_response()

    @app.get(f"/{report_filename}")
    async def serve_report() -> HTMLResponse:
        """Serve the generated report with the ``<video src>`` signed for the guard.

        The on-disk report is untouched (it stays shareable: opened via
        ``file://`` its bare-filename ``<video src>`` still loads the sibling
        video). When served through this authenticated server, the source video
        endpoints require the signed ``st`` query — which a pre-generated,
        token-free report cannot bake — so the signature is injected into the
        served bytes here (a ``data:`` embed needs no server round-trip and is
        left as-is).
        """
        report_path = session.output_dir / report_filename
        if not report_path.exists():
            raise HTTPException(status_code=404, detail="Report not found.")
        html_text = report_path.read_text(encoding="utf-8")
        signed_src = f"/video?st={video_access_token(app.state.session_token)}"

        def _sign(match: re.Match[str]) -> str:
            current = match.group(2)
            if not current or current.startswith("data:"):
                return match.group(0)
            return f"{match.group(1)}{signed_src}{match.group(3)}"

        html_text = _VIDEO_SRC_RE.sub(_sign, html_text, count=1)
        return HTMLResponse(content=html_text)

    @app.post("/api/stt")
    async def transcribe_voice(
        audio: Annotated[UploadFile, File(description="Browser-recorded audio")],
    ) -> JSONResponse:
        """Transcribe spoken description for a manual frame."""
        # P2-10: read in chunks and abort the moment the running total crosses
        # the cap, so a lying/absent Content-Length cannot force an unbounded
        # read into RAM (the prior ``await audio.read()`` buffered the whole
        # body before the size check).
        audio_data = await read_upload_capped(audio, max_bytes=MAX_AUDIO_BYTES)
        validate_browser_stt_upload(audio_data)

        filename = audio.filename or "recording.webm"
        content_type = audio.content_type or "audio/webm"

        # Snapshot the chain head before the unlocked STT call so the write-back
        # can compare-and-set: a VLM/STT that finished first must not be clobbered
        # by this call landing later (mirror of analyze-side).
        with session.lock:
            previous_response_id = session.last_response_id

        try:
            # BH13: transcribe_browser_audio makes blocking HTTP STT calls (plus
            # a possible ffmpeg-normalization fallback). Running it directly in
            # this async handler would block the event loop for the whole STT
            # round-trip, freezing every other request. Offload to the
            # threadpool (mirror of analyze-side /api/stt).
            result = await run_in_threadpool(
                transcribe_browser_audio,
                audio_data,
                filename=filename,
                content_type=content_type,
                config=config,
            )
            quality_warning = validate_browser_stt_result(result)
            # BH46/BH49 mirror (analyze-side): only advance the shared
            # conversation chain on a real STT response_id (empty id must not
            # clobber a valid head), and only if the head has not moved since we
            # read it — compare-and-set drops the write when a concurrent
            # operation already advanced the chain. NOTE: STT and VLM still share
            # one chain field here (as on the analyze side); full STT/VLM chain
            # separation is deferred design.
            advance_response_id_cas(
                session, previous_response_id, result.response_id, logger=logger
            )
            return JSONResponse(
                content=serialize_stt_result(result, quality_warning=quality_warning)
            )
        except HTTPException:
            # Keep the 422 quality rejection (BH59) on its own contract instead
            # of collapsing it into the generic 502 below.
            raise
        except Exception as exc:
            logger.error("Review STT failed for %s (%s): %s", filename, content_type, exc)
            raise HTTPException(
                status_code=502,
                detail="Voice transcription failed. Try again in a moment.",
            ) from exc

    @app.post("/api/manual-mark")
    async def mark_manual_frame(request: ManualFrameRequest) -> JSONResponse:
        """Persist a manual frame before analysis.

        The pixels are written to disk under ``manual_frames/`` (magic-validated
        JPEG/PNG; 400 on junk) so the capture survives a cold load even after the
        in-memory base64 is gone. The base64 is still kept on the live marker for
        immediate analysis/hydrate within the session.
        """
        marker_id = str(uuid.uuid4())
        frame_path = store_manual_frame_image(session.output_dir, marker_id, request.frame_base64)
        marker = ManualFrameMarker(
            marker_id=marker_id,
            timestamp=request.timestamp,
            frame_base64=request.frame_base64,
            transcript=request.transcript,
            notes=request.notes,
            status="pending",
            frame_path=frame_path,
        )
        with session.lock:
            session.markers[marker_id] = marker
        return JSONResponse(
            content={"marker_id": marker_id, "status": "pending", "frame_path": frame_path}
        )

    @app.delete("/api/manual-mark/{marker_id}")
    async def delete_manual_frame(marker_id: str) -> JSONResponse:
        """Forget a manual frame the reviewer removed in the UI.

        The session owns the manual-mark pixels, so a delete must purge it here
        too: otherwise review-state would re-hand the frame on a cold reload and
        resurrect a capture the reviewer already discarded. Missing markers are a
        no-op (idempotent) so a double-click or a stale client never 404s.

        The durable image on disk (``manual_frames/<id>.<ext>``) is removed as
        well: leaving it behind orphans a discarded capture on disk (a privacy /
        storage leak) and a later save could re-stamp its ``frame_path`` back
        into report.json. The unlink is idempotent (``missing_ok``) and never
        crashes the delete when the file is already gone.
        """
        with session.lock:
            marker = session.markers.pop(marker_id, None)
            session.results.pop(marker_id, None)
        if marker is not None and marker.frame_path:
            _remove_manual_frame_image(session.output_dir, marker.frame_path)
        return JSONResponse(content={"status": "deleted", "marker_id": marker_id})

    @app.patch("/api/manual-mark/{marker_id}")
    async def update_manual_frame(
        marker_id: str, request: ManualFrameUpdateRequest
    ) -> JSONResponse:
        """Update an already-marked frame's transcript/notes (BH28).

        ``POST /api/manual-mark`` only captures the text present at mark time;
        edits the reviewer made afterwards had no server path, so they were lost
        on a cold reload and a later analyze ran against the stale server copy.
        This applies a partial update to the live session marker, so
        ``review-state`` hands the new text back on reload. Unknown markers 404 —
        a frame must exist to be edited.

        R14: ``severity`` is the reviewer's manual priority override. A value in
        VALID_MARKER_SEVERITIES (incl. the explicit "none" that clears it) is
        persisted on the marker and mirrored onto any existing analysis result so
        the badge + export agree with the reviewer's call. An out-of-vocabulary
        value is ignored, never wedging the edit (mirror of Analyze A7b).
        """
        with session.lock:
            marker = session.markers.get(marker_id)
            if marker is None:
                raise HTTPException(status_code=404, detail="Manual frame not found")
            if request.transcript is not None:
                marker.transcript = request.transcript
            if request.notes is not None:
                marker.notes = request.notes
            if request.severity is not None:
                severity = request.severity.strip().lower()
                if severity in VALID_MARKER_SEVERITIES:
                    marker.severity = severity
                    existing_result = session.results.get(marker_id)
                    if existing_result is not None:
                        existing_result.severity = severity
            updated = {
                "marker_id": marker.marker_id,
                "transcript": marker.transcript,
                "notes": marker.notes,
                "severity": marker.severity,
            }
        return JSONResponse(content=updated)

    @app.get("/api/review-state")
    async def get_review_state() -> JSONResponse:
        """Return persisted human-review state for fresh browser loads.

        Includes in-memory session frames (manual marks not yet saved to disk)
        with a renderable frameDataUrl, so the client can drop base64 frames
        from its localStorage draft and still restore the image after a reload.
        """
        _, report_data = load_report_json()
        state = build_review_state_from_report(report_data)
        with session.lock:
            session_markers = list(session.markers.values())
        hydrate_state_with_session_frames(state, session_markers)
        return JSONResponse(content=state)

    @app.post("/api/manual-analyze/{marker_id}")
    async def analyze_manual_frame(marker_id: str) -> JSONResponse:
        """Run VLM analysis for one manual frame."""
        # BH14: analyze_single_marker makes a blocking ~120s VLM HTTP call.
        # Running it directly in this async handler would freeze the event loop
        # for the whole analysis, blocking every other request. Offload to the
        # threadpool (mirror of analyze-side /api/analyze BH4). The
        # HTTPException(404) for an unknown marker still propagates unchanged.
        outcome = await run_in_threadpool(analyze_single_marker, marker_id)
        return JSONResponse(content=outcome)

    # Under /api/ so the Host+Origin+session-token guards cover it — a write
    # endpoint must not be weaker-guarded than the read API.
    @app.post("/api/save")
    async def save_review_state(request: Request) -> dict[str, Any]:
        """Persist the human review (verdicts, notes, annotations) plus manual
        markers and results to the disk report.json."""
        import json

        # Serialize the entire load->merge->write cycle: a concurrent save must
        # not load report.json before this one's atomic replace lands, or it
        # would overwrite with a stale snapshot and drop this save's verdicts.
        await save_lock.acquire()
        try:
            with session.lock:
                markers = list(session.markers.values())
                results = list(session.results.values())

            # Load existing report
            json_path, report_data = load_report_json()

            # Update with manual findings
            # We map ManualFrameResult to a format compatible with UnifiedFinding for the report
            manual_findings = []
            for res in results:
                manual_findings.append(
                    {
                        "marker_id": res.marker_id,
                        "timestamp": res.timestamp,
                        "category": res.category,
                        "severity": res.severity,
                        "summary": res.summary,
                        "issues_detected": res.issues_detected,
                        "suggested_fix": res.suggested_fix,
                        "affected_components": res.affected_components,
                        "response_id": res.response_id,
                        "is_manual": True,
                    }
                )

            # Merge into report_data
            if "manual_review" not in report_data:
                report_data["manual_review"] = {}

            report_data["manual_review"]["markers"] = [
                {
                    "marker_id": m.marker_id,
                    "timestamp": m.timestamp,
                    "transcript": m.transcript,
                    "notes": m.notes,
                    "frame_path": m.frame_path,
                    # R14: persist the priority override on the marker so the
                    # fallback build path (markers + results, no human_review
                    # manual_frames) reconstructs the reviewer's pick on cold load.
                    "severity": m.severity,
                }
                for m in markers
            ]
            # Marker -> durable disk reference, used to (a) rewrite the human
            # review's manual frames with a frame_path and (b) strip the heavy
            # inline data: image so report.json never carries manual-frame pixels.
            frame_path_by_id = {m.marker_id: m.frame_path for m in markers}
            report_data["manual_review"]["results"] = manual_findings

            # Merge the human review sent by the UI (optional body — older
            # clients post without one). The canonical report.json keeps every
            # finding; rejected ones stay, explicitly marked, so a re-run or a
            # downstream agent knows the human already dismissed them.
            try:
                review = await request.json()
            except Exception:
                review = None
            if isinstance(review, dict) and review.get("findings") is not None:
                findings_review: dict[str, Any] = {}
                rejected_ids: list[Any] = []
                work_items = []
                review_findings = review.get("findings", [])
                for finding in review_findings:
                    fid = finding.get("id")
                    human = finding.get("human_review")
                    if fid is None or not isinstance(human, dict):
                        continue
                    findings_review[str(fid)] = human
                    work_items.append(work_item_from_review_finding(finding).to_dict())
                    if _finding_verdict(human) == "rejected":
                        rejected_ids.append(fid)
                manual_frames = review.get("manual_frames")
                if not isinstance(manual_frames, list):
                    manual_frames = []
                # Disk is the durable source: drop the heavy inline data: image and
                # stamp the relative frame_path so report.json carries a reference,
                # not ~500KB of base64 per manual frame. Cold-load reads the image
                # back off disk via frame_path.
                persisted_manual_frames: list[dict[str, Any]] = []
                for frame in manual_frames:
                    if not isinstance(frame, dict):
                        continue
                    stored = {k: v for k, v in frame.items() if k != "frameDataUrl"}
                    marker_id = str(frame.get("marker_id", ""))
                    disk_path = frame_path_by_id.get(marker_id)
                    if disk_path:
                        stored["frame_path"] = disk_path
                    elif isinstance(frame.get("frame_path"), str):
                        stored["frame_path"] = frame["frame_path"]
                    persisted_manual_frames.append(stored)
                manual_frames = persisted_manual_frames
                reviewed_at = str(review.get("reviewed_at") or "")
                reviewer = str(review.get("reviewer") or "")
                work_items.extend(
                    work_item_from_review_manual_frame(
                        frame,
                        reviewer=reviewer,
                        reviewed_at=reviewed_at,
                    ).to_dict()
                    for frame in manual_frames
                )
                report_data["human_review"] = {
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                    "findings": findings_review,
                    "rejected_ids": rejected_ids,
                    "manual_frames": manual_frames,
                }
                report_data["work_items"] = work_items
            elif markers or results:
                result_by_id = {result.marker_id: result for result in results}
                report_data["work_items"] = [
                    from_manual_frame(marker, result_by_id.get(marker.marker_id)).to_dict()
                    for marker in markers
                ]

            # BH30: write report.json atomically. The previous direct
            # ``open(json_path, "w")`` truncated the file in place, so a crash
            # mid-write (process killed, disk full) could leave a half-written,
            # unparseable report.json — destroying the reviewer's saved state.
            # Write to a temp file in the same directory, then os.replace it in.
            # ``os.replace`` is atomic on the same filesystem, so a crash before
            # the replace leaves the previous report.json fully intact.
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(json_path.parent), prefix=".report-", suffix=".json.tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(report_data, f, indent=2, ensure_ascii=False)
                    # Flush Python + OS buffers to the platter before the atomic
                    # replace. Without fsync, os.replace can commit the rename
                    # while the temp file's bytes are still in the OS page cache,
                    # so a crash right after leaves report.json pointing at a
                    # zero/short file — the exact data-loss os.replace is meant to
                    # prevent.
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, json_path)
            except Exception:
                Path(tmp_name).unlink(missing_ok=True)
                raise

            # Sweep orphaned manual-frame images only after report.json is durably
            # written (finding 156). The keep-set is the full live reference set:
            # every frame_path persisted into report.json plus every live session
            # marker (pending / unanalyzed included — those are marked-but-unsaved
            # captures whose disk image must survive). Anything else under
            # manual_frames/ was marked then discarded without a delete, and would
            # otherwise linger on disk forever (privacy leak + litter). Running it
            # before the atomic write would risk deleting a frame on a save that
            # then fails; here report.json already reflects `keep`.
            keep: set[str] = {m.frame_path for m in markers if m.frame_path}
            human_review = report_data.get("human_review")
            if isinstance(human_review, dict):
                for frame in human_review.get("manual_frames") or []:
                    if isinstance(frame, dict) and isinstance(frame.get("frame_path"), str):
                        keep.add(frame["frame_path"])
            manual_review_data = report_data.get("manual_review")
            if isinstance(manual_review_data, dict):
                for marker in manual_review_data.get("markers") or []:
                    if isinstance(marker, dict) and isinstance(marker.get("frame_path"), str):
                        keep.add(marker["frame_path"])
            _sweep_orphan_manual_frames(session.output_dir, keep)

            logger.info("Saved manual review state to %s", json_path)
            return {"status": "success", "filename": json_path.name}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to save review state: %s", e)
            raise HTTPException(status_code=500, detail="Failed to save review state.") from e
        finally:
            save_lock.release()

    app.mount(
        "/",
        StaticFiles(directory=str(session.output_dir), html=False, follow_symlink=False),
        name="review-static",
    )

    return app
