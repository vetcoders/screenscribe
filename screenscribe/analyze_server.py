"""Interactive analysis server for reversed review flow.

This module creates a FastAPI server that enables human-first video analysis:
1. Human watches video in browser
2. Marks frames and records voice comments
3. Voice -> STT -> becomes context for VLM analysis
4. Results appear in real-time

Built by Vetcoders
"""

from __future__ import annotations

import atexit
import base64
import binascii
import html
import logging
import mimetypes
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
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
from .work_item import from_analyze_marker

if TYPE_CHECKING:
    from .config import ScreenScribeConfig

logger = logging.getLogger(__name__)

# ``MAX_AUDIO_BYTES`` is imported (re-exported) from server_common so existing
# importers/tests that read it from this module keep working.
MAX_FRAME_BASE64_CHARS = 20_000_000  # ~15 MB decoded frame payload cap
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Cap on the in-memory finalize-job registry. Each finalize run registers one
# ``FinalizeJob`` (kept so the frontend can poll status + fetch the export
# payload); nothing pruned them, so a long-lived server that finalized many
# times grew the dict without bound (each completed job also retains its export
# payload). Keep at most this many entries, evicting the oldest FINISHED jobs
# first (running jobs are never evicted).
MAX_FINALIZE_JOBS = 32


class MarkFrameRequest(BaseModel):
    """Request body for marking a frame."""

    timestamp: float
    frame_base64: str = Field(..., max_length=MAX_FRAME_BASE64_CHARS)
    transcript: str = ""
    notes: str = ""


class InvalidMarkerFrame(ValueError):
    """Raised when a marker frame payload is not a supported image.

    Carries an explicit, client-safe ``client_detail`` so the ``/api/mark``
    handler never echoes ``str(exc)`` to the browser (C5.2 residue). The
    exception's own message keeps the full diagnostic (including the chained
    decoder error or the decoded byte prefix) for the server log; only
    ``client_detail`` — a deliberately authored, cause/path-free validation
    string — is sent to the client.
    """

    def __init__(self, message: str, *, client_detail: str | None = None) -> None:
        super().__init__(message)
        self.client_detail = client_detail if client_detail is not None else message


class UpdateMarkerRequest(BaseModel):
    """Request body for editing a marker's user-editable fields.

    Both fields are optional so the dashboard can PATCH them independently:
    the note editor sends ``{"notes": ...}`` and the per-marker priority
    control sends ``{"severity": ...}`` without clobbering the other field.
    ``severity`` is the user's manual priority override (A7b); ``None`` means
    "leave it unchanged".
    """

    notes: str | None = None
    severity: str | None = None


class AnalyzeLangRequest(BaseModel):
    """Optional request body for analyze endpoints carrying the chosen UI language.

    The dashboard sends ``{"lang": "en" | "pl"}`` so the VLM produces a
    finding in the language the user is currently viewing. When the body is
    missing or the field is absent, the server falls back to
    ``config.language`` (which is the CLI ``--lang`` value).

    Only the first two characters are consulted; everything else is
    discarded so a stray locale tag like ``"pl-PL"`` still resolves to
    ``"pl"``. Unrecognised values fall back to ``config.language`` rather
    than raising — the dashboard should never block on a language hint.
    """

    lang: str | None = None


@dataclass
class FrameMarker:
    """A marked frame with optional voice comment.

    The frame is captured from the browser as base64 image data and persisted
    to ``frame_path`` on disk so it can be served to the dashboard for visual
    reference with the same MIME type.
    """

    marker_id: str
    timestamp: float
    frame_base64: str
    transcript: str = ""
    notes: str = ""
    status: str = "pending"  # pending, analyzing, completed, error
    frame_path: Path | None = None
    frame_media_type: str | None = None
    frame_extension: str | None = None
    # User's manual priority override (A7b). ``None`` until the operator
    # picks a value from the per-marker priority control; once set it wins
    # over the VLM-assigned ``AnalysisResult.severity`` for display.
    severity: str | None = None


@dataclass
class AnalysisResult:
    """VLM analysis result for a marked frame."""

    marker_id: str
    timestamp: float
    category: str = "unknown"
    severity: str = "medium"
    summary: str = ""
    issues_detected: list[str] = field(default_factory=list)
    suggested_fix: str = ""
    affected_components: list[str] = field(default_factory=list)
    response_id: str = ""


@dataclass
class AnalyzeSession:
    """Session state for analyze mode.

    ``frames_dir`` is a per-session temporary directory that holds one image
    per marker. Individual files are removed when a marker is deleted, and the
    directory itself is cleaned up deterministically when the server stops:
    ``create_analyze_app`` registers a FastAPI ``shutdown`` handler plus an
    ``atexit`` fallback that ``rmtree`` it (see C5.3), so the directory no
    longer accumulates in the OS temp dir across runs.
    """

    video_path: Path
    frames_dir: Path = field(
        default_factory=lambda: Path(tempfile.mkdtemp(prefix="screenscribe_analyze_"))
    )
    markers: dict[str, FrameMarker] = field(default_factory=dict)
    results: dict[str, AnalysisResult] = field(default_factory=dict)
    last_response_id: str = ""
    finalize_jobs: dict[str, FinalizeJob] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class FinalizeJob:
    """Background job state for finalize flow."""

    job_id: str
    total: int = 0
    processed: int = 0
    completed: int = 0
    errors: int = 0
    skipped: int = 0
    status: str = "running"  # running, completed, error
    last_error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    export_payload: dict[str, Any] | None = None


def create_analyze_app(video_path: Path, config: ScreenScribeConfig) -> FastAPI:
    """Create FastAPI app for analyze mode.

    Args:
        video_path: Path to video file to analyze
        config: screenscribe configuration

    Returns:
        FastAPI application instance
    """
    app = FastAPI(
        title="Screenscribe Analyze",
        description="Interactive video analysis with human-in-the-loop",
        version=__version__,
    )

    # Localhost-only security: per-process session token + Host/Origin guards on
    # /api/* (the token is handed to the UI via the URL fragment; see index()).
    from .server_security import (
        frame_access_token,
        generate_session_token,
        install_security,
        video_access_token,
    )

    install_security(app, generate_session_token(), video_paths=frozenset({"/video"}))

    def marker_frame_url(marker: FrameMarker) -> str | None:
        """Frame URL for ``<img src>``: image requests can't carry the session
        token header, so the URL itself carries the per-marker signature the
        guard accepts for GET on this one path (see server_security)."""
        if not marker.frame_path:
            return None
        signature = frame_access_token(app.state.session_token, marker.marker_id)
        return f"/api/marker/{marker.marker_id}/frame?st={signature}"

    install_stt_upload_cap(app)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Keep /api/mark frame-size failures on the endpoint's 400 contract."""
        if request.url.path == "/api/mark":
            frame_errors = [
                error
                for error in exc.errors()
                if tuple(error.get("loc", ())) == ("body", "frame_base64")
            ]
            if frame_errors:
                return JSONResponse(status_code=400, content={"detail": "Invalid frame_base64"})
        return await request_validation_exception_handler(request, exc)

    # Session state (in-memory, single user)
    session = AnalyzeSession(video_path=video_path)
    # Expose the session so its lifecycle (notably frames_dir) is observable
    # and testable from outside the closure (mirrors app.state.session_token).
    app.state.session = session

    # C5.3: deterministically reclaim the per-session temp dir instead of
    # leaving it to OS-level cleanup. Registered on FastAPI shutdown (graceful
    # uvicorn stop / Ctrl+C) AND atexit (hard fallback when shutdown never
    # fires). rmtree is idempotent here: ignore_errors makes a second call on
    # an already-removed dir a no-op.
    def _cleanup_frames_dir() -> None:
        shutil.rmtree(session.frames_dir, ignore_errors=True)

    app.router.add_event_handler("shutdown", _cleanup_frames_dir)
    atexit.register(_cleanup_frames_dir)

    def build_markers_payload() -> list[dict[str, Any]]:
        """Build current marker list enriched with analysis results."""
        markers_data: list[dict[str, Any]] = []
        with session.lock:
            markers = list(session.markers.values())
        for marker in markers:
            marker_data: dict[str, Any] = {
                "marker_id": marker.marker_id,
                "timestamp": marker.timestamp,
                "transcript": marker.transcript,
                "notes": marker.notes,
                "status": marker.status,
                "frame_url": marker_frame_url(marker),
            }
            with session.lock:
                result = session.results.get(marker.marker_id)
            if result:
                marker_data["result"] = {
                    "category": result.category,
                    "severity": result.severity,
                    "summary": result.summary,
                    "issues_detected": result.issues_detected,
                    "suggested_fix": result.suggested_fix,
                }
            # Effective priority for the per-marker control (A7b): the user's
            # manual override wins, otherwise fall back to the VLM-assigned
            # result severity. Exposed at top level so the select reflects it
            # even on a pending marker that has no ``result`` block yet.
            effective_severity = marker.severity or (result.severity if result else None)
            if effective_severity is not None:
                marker_data["severity"] = effective_severity
            markers_data.append(marker_data)
        return markers_data

    def build_export_payload() -> dict[str, Any]:
        """Build the exported findings JSON as WorkItem-spine items.

        Each marker (+ its optional VLM result) rides the shared WorkItem spine
        (``from_analyze_marker``) so analyze / review-detection / review-manual
        all emit ONE output contract under ``work_items``. This JSON is the
        lightweight, diffable, agent-readable findings export: it carries
        metadata + analysis but NOT the frame binary. The frame base64 (and the
        absolute on-disk session path) are stripped here -- full frames belong
        to the ZIP/manifest, never inlined as a blob in this JSON (today's
        export carried no frames either, so this is contract hygiene, not a
        regression). The exported ``status`` is normalized to ``"processing"``
        (the human decision lives in ``human_review``); the live
        ``FrameMarker.status`` state machine is untouched.
        """
        work_items: list[dict[str, Any]] = []
        export_data: dict[str, Any] = {
            # Basename only — never the absolute input path (privacy: shareable
            # artifacts must not leak the local filesystem / user home).
            "video": video_path.name,
            "work_items": work_items,
        }
        with session.lock:
            markers = list(session.markers.values())
        for marker in markers:
            with session.lock:
                result = session.results.get(marker.marker_id)
            item = from_analyze_marker(marker, result)
            item.status = "processing"
            # Findings JSON = metadata/analysis only. Drop the frame binary
            # (base64) and the absolute session path (privacy); keep only light,
            # non-path frame descriptors (media_type/extension) when present.
            item.frame = {
                key: value
                for key, value in item.frame.items()
                if key in ("media_type", "extension")
            }
            work_items.append(item.to_dict())
        return export_data

    def build_markdown_report() -> str:
        """Render all markers as a Markdown report via report.save_enhanced_markdown_report.

        Adapter mapping:
        - Each ``FrameMarker`` becomes a ``Detection`` (segment.id is a fresh
          1-based counter so report.py's findings_by_id lookup works).
        - Each ``AnalysisResult`` becomes a partial ``UnifiedFinding`` with
          ``is_issue=True`` so it lands in the report's "Issues" section.
          Fields not captured by analyze (sentiment, ui_elements, etc) default
          to empty - report.py treats those as optional.
        - Markers without a result still appear in the table with the raw
          transcript as the summary, so users see pending findings rather
          than silently dropping them.
        - Markers without a persisted frame fall back to a synthetic relative
          path; report.py only renders ``screenshot_path.name`` so the file
          doesn't need to exist on disk for the MD output.
        """
        from .detect import Detection
        from .report import save_enhanced_markdown_report
        from .transcribe import Segment
        from .unified_analysis import UnifiedFinding

        with session.lock:
            markers = sorted(session.markers.values(), key=lambda m: m.timestamp)
            results = dict(session.results)

        detections: list[Detection] = []
        screenshots: list[tuple[Detection, Path]] = []
        unified_findings: list[UnifiedFinding] = []
        transcript_segments: list[Segment] = []
        transcript_chunks: list[str] = []

        for index, marker in enumerate(markers, start=1):
            text = marker.transcript or marker.notes or "(user-marked frame, no transcript)"
            segment = Segment(
                id=index,
                start=marker.timestamp,
                end=marker.timestamp + 1.0,
                text=text,
            )
            detection = Detection(
                segment=segment,
                category="user_marked",
                keywords_found=[],
                context=marker.notes,
            )
            detections.append(detection)

            screenshot_path: Path = (
                marker.frame_path
                if marker.frame_path is not None
                else Path(f"{marker.marker_id}.png")
            )
            screenshots.append((detection, screenshot_path))

            if marker.transcript:
                transcript_segments.append(segment)
                transcript_chunks.append(marker.transcript)

            result = results.get(marker.marker_id)
            if result:
                unified_findings.append(
                    UnifiedFinding(
                        detection_id=index,
                        screenshot_path=screenshot_path,
                        timestamp=marker.timestamp,
                        category=result.category or "user_marked",
                        is_issue=True,
                        sentiment="problem",
                        severity=result.severity or "medium",
                        summary=result.summary or text,
                        action_items=[],
                        affected_components=result.affected_components,
                        suggested_fix=result.suggested_fix,
                        ui_elements=[],
                        issues_detected=result.issues_detected,
                        accessibility_notes=[],
                        design_feedback="",
                        technical_observations="",
                        response_id=result.response_id,
                    )
                )

        full_transcript = "\n\n".join(transcript_chunks)

        with tempfile.TemporaryDirectory(prefix="screenscribe_md_") as tmp:
            output_path = Path(tmp) / "analyze_report.md"
            save_enhanced_markdown_report(
                detections=detections,
                screenshots=screenshots,
                video_path=video_path,
                output_path=output_path,
                unified_findings=unified_findings or None,
                executive_summary="",
                visual_summary="",
                errors=None,
                transcript=full_transcript,
                transcript_segments=transcript_segments or None,
            )
            return output_path.read_text(encoding="utf-8")

    def resolve_vlm_language(lang_override: str | None) -> str:
        """Pick the language to pass to the VLM analysis call.

        Dashboard sends a runtime override (the active toggle state) so new
        findings come back in the chosen language. When the override is
        missing, blank, or unrecognised we fall back to ``config.language``
        (the CLI ``--lang`` value). Only the first two characters are
        considered so locale tags like ``pl-PL`` still resolve.
        """
        if not lang_override:
            return config.language
        candidate = lang_override.strip().lower()[:2]
        if candidate not in {"en", "pl"}:
            return config.language
        return candidate

    def analyze_single_marker(marker_id: str, lang_override: str | None = None) -> dict[str, Any]:
        """Run unified AI analysis for one marker and persist status/result.

        ``lang_override`` is the language the dashboard currently has
        selected (Issue #9 toggle). When provided, the VLM prompt + summary
        come back in that language; otherwise we use ``config.language``.
        STT language is unaffected — voice notes still transcribe in
        ``config.language``.
        """
        import dataclasses

        from .detect import Detection
        from .transcribe import Segment
        from .unified_analysis import analyze_finding_unified

        with session.lock:
            if marker_id not in session.markers:
                raise HTTPException(status_code=404, detail="Marker not found")
            marker = session.markers[marker_id]
            # Concurrent-analysis guard: a second analyze request for a marker
            # whose VLM call is already in flight would double the VLM cost and
            # race two writers onto one result. Reject the duplicate with 409.
            # The batch finalize path demotes orphan "analyzing" markers to
            # "pending" before it snapshots (P3-2), so this never self-blocks a
            # finalize run.
            if marker.status == "analyzing":
                raise HTTPException(status_code=409, detail="Marker analysis already in progress")
            marker.status = "analyzing"

        segment = Segment(
            id=0,
            start=marker.timestamp,
            end=marker.timestamp + 1.0,
            text=marker.transcript or marker.notes or "User marked this frame",
        )
        detection = Detection(
            segment=segment,
            category="user_marked",
            keywords_found=[],
            context=f"User comment: {marker.transcript}\nNotes: {marker.notes}",
        )

        # BH58: the frame decode + transient tempfile write used to live OUTSIDE
        # the try below. A corrupt base64 payload (b64decode raising) or a disk
        # error on tmp.write would propagate while marker.status was already
        # flipped to "analyzing", leaving the marker stuck "analyzing" forever
        # with no result. Both the fallback decode and the VLM call now run
        # inside the single try, so any failure resets the marker to "error".
        screenshot_path: Path | None = None
        cleanup_screenshot = False
        try:
            # Reuse the persisted frame on disk when available so the analysis
            # operates on the same PNG that backs the dashboard preview. If the
            # frame was never persisted (legacy markers, future regressions),
            # fall back to writing a transient tempfile that is cleaned up below.
            if marker.frame_path is not None and marker.frame_path.exists():
                screenshot_path = marker.frame_path
            else:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    frame_bytes = base64.b64decode(marker.frame_base64)
                    tmp.write(frame_bytes)
                    screenshot_path = Path(tmp.name)
                cleanup_screenshot = True

            with session.lock:
                previous_response_id = session.last_response_id

            # Issue #9 (Group D): VLM output language follows the dashboard
            # toggle state when the request carried one, else falls back to
            # config.language (CLI --lang). Existing already-analyzed
            # findings are not retroactively re-translated.
            vlm_language = resolve_vlm_language(lang_override)
            vlm_config = dataclasses.replace(config, language=vlm_language)

            finding = analyze_finding_unified(
                detection=detection,
                screenshot_path=screenshot_path,
                config=vlm_config,
                previous_response_id=previous_response_id,
            )

            if not finding:
                with session.lock:
                    # Drop any prior result so a failed re-analysis can't leave a
                    # stale completed finding under an error marker.
                    session.results.pop(marker_id, None)
                    marker.status = "error"
                return {"marker_id": marker_id, "status": "error", "error": "Analysis failed"}

            # BH48 (mirror of G3/BH27, local consistent semantics): a finding the
            # parser flagged ``confidence == "degraded"`` is a schema-drift /
            # raw-text fallback, NOT a real analysis. Treat it as failure instead
            # of writing a falsely "completed" finding the user would trust.
            if getattr(finding, "confidence", "high") == "degraded":
                with session.lock:
                    session.results.pop(marker_id, None)
                    marker.status = "error"
                return {
                    "marker_id": marker_id,
                    "status": "error",
                    "error": "Analysis degraded (parse failure)",
                }

            result = AnalysisResult(
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
                # BH40: the marker can be deleted mid-flight (delete_marker pops
                # it under the same lock while the VLM call ran unlocked). Writing
                # a result for an absent marker would resurrect a ghost finding in
                # results/export. Re-check presence before persisting.
                if marker_id not in session.markers:
                    session.results.pop(marker_id, None)
                    return {
                        "marker_id": marker_id,
                        "status": "error",
                        "error": "Marker deleted during analysis",
                    }
                # A7b: an operator priority override set on the marker (incl. the
                # explicit "none" that clears it) wins over the VLM-assigned
                # severity. Re-apply it here so badge/export/markdown-report agree
                # with what the priority <select> shows. Without this, (re)analysis
                # silently reverts to the model severity while the select still
                # displays the override — the effective_severity payload would
                # then disagree with the result block.
                if marker.severity is not None:
                    result.severity = marker.severity
                # Direct dict assignment overwrites any previous result for this
                # marker, so Re-analyze never produces duplicates.
                session.results[marker_id] = result
                # BH29: only advance the conversation chain when the finding
                # actually carried a response_id; an empty id would clobber the
                # valid chain head with a blank, breaking later chaining.
                # Compare-and-set against previous_response_id (read at :531): a
                # concurrent analysis that finished first owns the newer head, so
                # this call must not overwrite it with an id chained off the stale
                # head.
                advance_response_id_cas(
                    session, previous_response_id, finding.response_id, logger=logger
                )
                marker.status = "completed"

            return {
                "marker_id": marker_id,
                "status": "completed",
                "result": {
                    "category": result.category,
                    "severity": result.severity,
                    "summary": result.summary,
                    "issues_detected": result.issues_detected,
                    "suggested_fix": result.suggested_fix,
                },
            }
        except Exception as exc:  # defensive guard for batch finalize
            with session.lock:
                # Same stale-result guard as the no-finding path above: a raised
                # re-analysis must not keep the previous completed result around.
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
            if cleanup_screenshot and screenshot_path is not None:
                screenshot_path.unlink(missing_ok=True)

    def analyze_all_pending_markers(
        job: FinalizeJob | None = None, lang_override: str | None = None
    ) -> dict[str, Any]:
        """Analyze all markers that are pending/error/no-result.

        ``lang_override`` is forwarded to every per-marker call so the
        whole batch lands in the language the dashboard requested.
        """
        with session.lock:
            # P3-2: reset orphan "analyzing" markers before snapshotting. A
            # marker left "analyzing" by a previous run that died (process
            # restart, crashed thread) would otherwise stay stuck forever:
            # finalize only re-runs pending/error/no-result markers, so an
            # orphan that already has a stale result would be silently skipped.
            # Demote orphans to "pending" so this finalize retries them.
            for marker in session.markers.values():
                if marker.status == "analyzing":
                    marker.status = "pending"
            markers = list(session.markers.values())
            result_ids = set(session.results.keys())

        marker_ids = [
            marker.marker_id
            for marker in markers
            if marker.status in {"pending", "error"} or marker.marker_id not in result_ids
        ]

        completed = 0
        errors = 0
        skipped = max(0, len(markers) - len(marker_ids))
        results: list[dict[str, Any]] = []

        if job:
            with session.lock:
                job.total = len(marker_ids)
                job.skipped = skipped

        for marker_id in marker_ids:
            # BH12: a marker deleted between the snapshot above and this
            # iteration makes analyze_single_marker raise HTTPException(404)
            # (the lookup raises BEFORE its own try/except). Without a
            # per-marker guard that 404 would abort the WHOLE finalize batch,
            # dropping every still-pending marker. Catch per marker, count it
            # as an error, and keep going.
            try:
                outcome = analyze_single_marker(marker_id, lang_override=lang_override)
            except HTTPException as exc:
                err = sanitized_error(exc, "marker_unavailable")
                outcome = {
                    "marker_id": marker_id,
                    "status": "error",
                    "error": err["message"],
                    "error_code": err["error_code"],
                }
            except Exception as exc:  # defensive: one bad marker must not abort batch
                err = sanitized_error(exc, "marker_analysis_failed")
                outcome = {
                    "marker_id": marker_id,
                    "status": "error",
                    "error": err["message"],
                    "error_code": err["error_code"],
                }
            results.append(outcome)
            if outcome.get("status") == "completed":
                completed += 1
            elif outcome.get("status") == "error":
                errors += 1
            if job:
                with session.lock:
                    job.processed += 1
                    job.completed = completed
                    job.errors = errors

        return {
            "total_markers": len(markers),
            "processed": len(marker_ids),
            "completed": completed,
            "errors": errors,
            "skipped": skipped,
            "results": results,
        }

    def get_finalize_job(job_id: str) -> FinalizeJob:
        """Get finalize job by id or raise HTTP 404."""
        with session.lock:
            job = session.finalize_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Finalize job not found")
        return job

    def _evict_stale_finalize_jobs() -> None:
        """Bound the finalize-job registry so it can't grow without limit.

        Must be called while holding ``session.lock`` (it mutates
        ``session.finalize_jobs``). Keeps at most ``MAX_FINALIZE_JOBS`` entries by
        evicting the OLDEST finished (completed/error) jobs first; a running job
        is never evicted. The registry is only a status-poll + result-fetch
        surface, so dropping the oldest finished jobs merely loses history a
        client is no longer polling.
        """
        if len(session.finalize_jobs) <= MAX_FINALIZE_JOBS:
            return
        finished = sorted(
            (job for job in session.finalize_jobs.values() if job.status != "running"),
            key=lambda job: job.started_at,
        )
        for job in finished:
            if len(session.finalize_jobs) <= MAX_FINALIZE_JOBS:
                break
            session.finalize_jobs.pop(job.job_id, None)

    def serialize_finalize_job(job: FinalizeJob) -> dict[str, Any]:
        """Serialize finalize job for frontend polling."""
        with session.lock:
            total = job.total
            processed = job.processed
            status = job.status
            completed = job.completed
            errors = job.errors
            skipped = job.skipped
            finished_at = job.finished_at
            started_at = job.started_at
            last_error = job.last_error

        progress = (
            1.0
            if total == 0 and status == "completed"
            else ((processed / total) if total > 0 else 0.0)
        )
        return {
            "job_id": job.job_id,
            "status": status,
            "total": total,
            "processed": processed,
            "completed": completed,
            "errors": errors,
            "skipped": skipped,
            "progress": progress,
            "started_at": started_at,
            "finished_at": finished_at,
            "last_error": last_error,
        }

    def run_finalize_job(job_id: str, lang_override: str | None = None) -> None:
        """Run async finalize job in background thread.

        ``lang_override`` flows through ``analyze_all_pending_markers`` to
        each per-marker analysis call, so the language captured at job
        start is the one used for every finding the job produces.
        """
        # P3-5: this runs in a daemon background thread. get_finalize_job raises
        # HTTPException(404) when the job is missing, which is meaningless off
        # the request path and would kill the thread silently (the raise happens
        # before any try, and job would be unbound inside one). Read the job
        # directly under the lock and handle absence explicitly instead.
        with session.lock:
            job = session.finalize_jobs.get(job_id)
        if job is None:
            logger.error("Finalize background job %s vanished before it could run", job_id)
            return
        try:
            analysis_summary = analyze_all_pending_markers(job=job, lang_override=lang_override)
            payload = {
                "analysis": analysis_summary,
                "markers": build_markers_payload(),
                "export": build_export_payload(),
            }
            with session.lock:
                job.status = "completed"
                job.finished_at = time.time()
                job.export_payload = payload
        except Exception as exc:  # pragma: no cover - defensive
            sanitized = sanitized_error(exc, "finalize_failed")
            with session.lock:
                job.status = "error"
                job.last_error = sanitized["message"]
                job.finished_at = time.time()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Serve the analyze UI."""
        from .shell import ANALYZE_SURFACE, render_surface

        lang = config.language[:2].lower()  # "pl" or "en"
        # The <video src> can't carry the session-token header, so hand the guard
        # its signature in the query string (same idiom as the frame <img> URLs).
        video_query = f"?st={video_access_token(app.state.session_token)}"
        context = {
            "document_language": html.escape(lang),
            "ui_language": html.escape(lang),
            "video_name": video_path.name,
            "video_name_escaped": html.escape(video_path.name),
            "video_query": video_query,
            "speech_lang_label": html.escape(lang.upper()),
            "body_mode": "analyze",
            "body_default_lang": lang,
            "body_speech_lang": lang,
            "body_has_markers": "false",
            "findings_json": "[]",
            "segments_json": "[]",
        }
        return HTMLResponse(content=render_surface(ANALYZE_SURFACE, context))

    @app.get("/video")
    async def serve_video() -> FileResponse:
        """Serve the video file."""
        if not video_path.exists():
            raise HTTPException(status_code=404, detail="Video not found")
        media_type, _ = mimetypes.guess_type(video_path.name)
        return FileResponse(
            video_path,
            media_type=media_type or "application/octet-stream",
            filename=video_path.name,
        )

    @app.post("/api/stt")
    async def transcribe_voice(audio: Annotated[UploadFile, File()]) -> JSONResponse:
        """Transcribe voice recording to text."""
        # Enforce the cap without holding an oversized payload in RAM: the
        # _stt_upload_cap middleware already rejects an honestly-declared
        # oversized Content-Length before the body is parsed; read_upload_capped
        # reads in chunks and stops as soon as the running total crosses the
        # cap, so a lying or absent header still cannot balloon memory.
        content = await read_upload_capped(audio, max_bytes=MAX_AUDIO_BYTES)
        filename = audio.filename or "recording.webm"
        content_type = audio.content_type or "audio/webm"

        validate_browser_stt_upload(content)

        # Snapshot the chain head before the unlocked STT call so the write-back
        # can compare-and-set: a VLM/STT that finished first must not be clobbered
        # by this call landing later (mirror of review-side).
        with session.lock:
            previous_response_id = session.last_response_id

        try:
            # BH13: transcribe_browser_audio makes blocking HTTP STT calls (plus
            # a possible ffmpeg-normalization fallback). Running it directly in
            # this async handler would block the event loop for the whole STT
            # round-trip, freezing every other request. Offload to the
            # threadpool (mirror of review-side /api/stt).
            result = await run_in_threadpool(
                transcribe_browser_audio,
                content,
                filename=filename,
                content_type=content_type,
                config=config,
            )
            quality_warning = validate_browser_stt_result(result)
            # BH46/BH49: an empty STT response_id would clobber the chain head
            # with a blank, breaking the conversation chain the VLM analysis
            # relies on (the next analyze call would pass "" as
            # previous_response_id). Only advance on a real id, and only if the
            # head has not moved since we read it — compare-and-set drops the
            # write when a concurrent operation already advanced the chain.
            # NOTE: STT and VLM still share one chain field; full STT/VLM chain
            # separation is design work (D-4) deferred as needs_design.
            advance_response_id_cas(
                session, previous_response_id, result.response_id, logger=logger
            )
            return JSONResponse(
                content=serialize_stt_result(result, quality_warning=quality_warning)
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Analyze STT failed for %s (%s): %s", filename, content_type, exc)
            raise HTTPException(
                status_code=502,
                detail="Voice transcription failed. Try again in a moment.",
            ) from exc

    def persist_marker_frame(marker: FrameMarker) -> None:
        """Decode the marker's base64 frame and write it to the session frames dir.

        Frames are stored with the browser-provided image format, so the same
        file backs both the dashboard preview and any downstream renderers
        without MIME/extension drift.
        """
        try:
            frame_bytes = base64.b64decode(marker.frame_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidMarkerFrame(
                f"frame_base64 is not valid base64: {exc}",
                client_detail="frame_base64 must be valid base64",
            ) from exc

        if frame_bytes.startswith(JPEG_MAGIC):
            extension = ".jpg"
            media_type = "image/jpeg"
        elif frame_bytes.startswith(PNG_MAGIC):
            extension = ".png"
            media_type = "image/png"
        else:
            raise InvalidMarkerFrame(
                f"frame_base64 decoded to unsupported image bytes (prefix={frame_bytes[:8]!r})",
                client_detail="frame_base64 must decode to a JPEG or PNG image",
            )

        try:
            session.frames_dir.mkdir(parents=True, exist_ok=True)
            target = session.frames_dir / f"{marker.marker_id}{extension}"
            target.write_bytes(frame_bytes)
            marker.frame_path = target
            marker.frame_media_type = media_type
            marker.frame_extension = extension
        except Exception as exc:
            logger.error("Failed to persist frame for marker %s: %s", marker.marker_id, exc)
            marker.frame_path = None
            marker.frame_media_type = None
            marker.frame_extension = None
            raise HTTPException(status_code=500, detail="Failed to persist marker frame") from exc

    @app.post("/api/mark")
    async def mark_frame(request: MarkFrameRequest) -> JSONResponse:
        """Mark a frame for analysis."""
        marker_id = str(uuid.uuid4())[:8]
        marker = FrameMarker(
            marker_id=marker_id,
            timestamp=request.timestamp,
            frame_base64=request.frame_base64,
            transcript=request.transcript,
            notes=request.notes,
            status="pending",
        )
        try:
            # 394: base64 decode (up to ~15 MB) + write_bytes are blocking; run
            # them in the threadpool like STT/analyze/finalize so the event loop
            # stays free. The exception mapping is unchanged: InvalidMarkerFrame
            # and the inner HTTPException(500) propagate through run_in_threadpool
            # exactly as if called inline.
            await run_in_threadpool(persist_marker_frame, marker)
        except InvalidMarkerFrame as exc:
            # C5.2 residue: the full diagnostic (incl. the chained decoder error
            # and decoded byte prefix) goes to the log only; the client receives
            # the authored, cause-free validation message — never str(exc).
            logger.info("Rejected /api/mark frame (400): %s", exc)
            raise HTTPException(status_code=400, detail=exc.client_detail) from exc
        # 412: once the frame is persisted, the file on disk is the source of
        # truth (analyze_single_marker and get_marker_frame both read
        # frame_path). Drop the up-to-20 MB base64 copy so it doesn't sit in
        # session.markers for the marker's whole lifetime. Gated on a successful
        # persist (frame_path set) so the analyze_single_marker fallback still
        # has base64 to decode when persistence never happened.
        if marker.frame_path is not None:
            marker.frame_base64 = ""
        with session.lock:
            session.markers[marker_id] = marker
        return JSONResponse(
            content={
                "marker_id": marker_id,
                "status": "pending",
                "frame_url": marker_frame_url(marker),
            }
        )

    @app.get("/api/markers")
    async def get_markers() -> JSONResponse:
        """Get all markers with their results."""
        return JSONResponse(content=build_markers_payload())

    @app.get("/api/marker/{marker_id}/frame")
    async def get_marker_frame(marker_id: str) -> FileResponse:
        """Return the captured frame for a marker with its real media type."""
        with session.lock:
            marker = session.markers.get(marker_id)
        if not marker:
            raise HTTPException(status_code=404, detail="Marker not found")
        if not marker.frame_path or not marker.frame_path.exists():
            raise HTTPException(status_code=404, detail="Frame not available")
        media_type = marker.frame_media_type or "application/octet-stream"
        extension = marker.frame_extension or marker.frame_path.suffix or ".bin"
        return FileResponse(
            marker.frame_path,
            media_type=media_type,
            filename=f"{marker_id}{extension}",
        )

    @app.patch("/api/marker/{marker_id}")
    async def update_marker(marker_id: str, request: UpdateMarkerRequest) -> JSONResponse:
        """Update editable fields of a marker (``notes`` and/or ``severity``).

        Both fields are optional and updated independently: the note editor
        PATCHes ``notes`` and the per-marker priority control (A7b) PATCHes
        ``severity``. A ``None`` field is left untouched.

        Transcript is intentionally read-only here: it's the STT output and
        editing it would silently invalidate the AI context that produced any
        existing analysis result. Priority, unlike notes, does NOT feed the VLM
        context, so changing it never invalidates an existing analysis.
        """
        with session.lock:
            marker = session.markers.get(marker_id)
            if not marker:
                raise HTTPException(status_code=404, detail="Marker not found")
            analysis_invalidated = False
            if request.notes is not None:
                notes_changed = marker.notes != request.notes
                marker.notes = request.notes
                # Notes feed into the VLM context (analyze_single_marker), so
                # editing them invalidates any existing analysis. Drop the stale
                # result and send the marker back to pending so it must be
                # re-analyzed; otherwise the dashboard/export would show a new
                # note beside an analysis built from the old one.
                analysis_invalidated = notes_changed and marker_id in session.results
                if analysis_invalidated:
                    session.results.pop(marker_id, None)
                    marker.status = "pending"
            if request.severity is not None:
                severity = request.severity.strip().lower()
                if severity in VALID_MARKER_SEVERITIES:
                    # Persist the manual override on the marker so it survives a
                    # refreshMarkers(), and mirror it onto any existing analysis
                    # result so the badge + export agree with the operator's call.
                    marker.severity = severity
                    existing = session.results.get(marker_id)
                    if existing is not None:
                        existing.severity = severity
        return JSONResponse(
            content={
                "marker_id": marker_id,
                "notes": marker.notes,
                "status": marker.status,
                "analysis_invalidated": analysis_invalidated,
            }
        )

    @app.delete("/api/marker/{marker_id}")
    async def delete_marker(marker_id: str) -> JSONResponse:
        """Remove a marker, its analysis result, and its persisted frame.

        The frame PNG cleanup is best-effort - failing to delete the file
        leaves a small temp leak (one PNG per marker) but does not block the
        API call. The session frames_dir itself is reclaimed deterministically
        on server shutdown / atexit (see ``AnalyzeSession`` / C5.3).
        """
        with session.lock:
            marker = session.markers.pop(marker_id, None)
            if not marker:
                raise HTTPException(status_code=404, detail="Marker not found")
            session.results.pop(marker_id, None)
        if marker.frame_path is not None:
            try:
                marker.frame_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "Failed to remove frame file for deleted marker %s: %s",
                    marker_id,
                    exc,
                )
        return JSONResponse(content={"marker_id": marker_id, "deleted": True})

    @app.post("/api/analyze/{marker_id}")
    async def analyze_marked_frame(
        marker_id: str, body: AnalyzeLangRequest | None = None
    ) -> JSONResponse:
        """Run VLM analysis on a marked frame.

        Idempotent w.r.t. ``session.results``: ``analyze_single_marker``
        re-assigns ``session.results[marker_id]`` so calling this on an
        already-analyzed marker overwrites the previous result instead of
        producing a duplicate. This is the Re-analyze path.

        Optional JSON body: ``{"lang": "en" | "pl"}`` — when present, the
        VLM produces the finding in that language. Missing body keeps the
        previous behaviour (uses ``config.language``).
        """
        lang_override = body.lang if body and body.lang else None
        # BH4 (same class as /api/finalize): the single-marker VLM call blocks.
        # Offload off the event loop so a re-analyze does not freeze the server.
        # HTTPException(404) for an unknown marker still propagates unchanged.
        outcome = await run_in_threadpool(
            analyze_single_marker, marker_id, lang_override=lang_override
        )
        return JSONResponse(content=outcome)

    @app.post("/api/finalize/start")
    async def start_finalize_job(
        body: AnalyzeLangRequest | None = None,
    ) -> JSONResponse:
        """Start async finalize job and return job metadata for polling.

        Optional ``{"lang": ...}`` body is captured at job start time and
        forwarded to every per-marker analysis the job runs, so the whole
        batch lands in one language even if the user toggles afterwards.
        """
        lang_override = body.lang if body and body.lang else None
        with session.lock:
            running_job = next(
                (job for job in session.finalize_jobs.values() if job.status == "running"),
                None,
            )
            if running_job:
                return JSONResponse(content=serialize_finalize_job(running_job))

            job_id = str(uuid.uuid4())[:12]
            job = FinalizeJob(job_id=job_id)
            session.finalize_jobs[job_id] = job
            _evict_stale_finalize_jobs()

        thread = threading.Thread(
            target=run_finalize_job,
            args=(job_id,),
            kwargs={"lang_override": lang_override},
            daemon=True,
        )
        thread.start()
        return JSONResponse(content=serialize_finalize_job(job))

    @app.get("/api/finalize/status/{job_id}")
    async def get_finalize_job_status(job_id: str) -> JSONResponse:
        """Get current async finalize job status/progress."""
        job = get_finalize_job(job_id)
        return JSONResponse(content=serialize_finalize_job(job))

    @app.get("/api/finalize/result/{job_id}")
    async def get_finalize_job_result(job_id: str) -> JSONResponse:
        """Get final payload for completed finalize job."""
        job = get_finalize_job(job_id)
        with session.lock:
            status = job.status
            payload = job.export_payload
            last_error = job.last_error

        if status == "running":
            raise HTTPException(status_code=409, detail="Finalize job still running")
        if status == "error":
            raise HTTPException(status_code=500, detail=last_error or "Finalize job failed")
        if not payload:
            raise HTTPException(status_code=500, detail="Finalize result missing")
        return JSONResponse(content=payload)

    @app.post("/api/finalize")
    async def finalize_marked_frames(
        body: AnalyzeLangRequest | None = None,
    ) -> JSONResponse:
        """Finalize annotation session: analyze all markers and return export payload.

        Optional ``{"lang": ...}`` body forwarded to the batch analysis.
        """
        lang_override = body.lang if body and body.lang else None
        # BH4: analyze_all_pending_markers makes blocking VLM HTTP calls (and a
        # retry sleep on the analyze path). Running it directly in this async
        # handler would block the event loop for the whole batch, freezing every
        # other request (status polls, the UI). Offload to the threadpool.
        analysis_summary = await run_in_threadpool(
            analyze_all_pending_markers, lang_override=lang_override
        )
        payload = {
            "analysis": analysis_summary,
            "markers": build_markers_payload(),
            "export": build_export_payload(),
        }
        return JSONResponse(content=payload)

    @app.get("/api/export")
    async def export_findings() -> JSONResponse:
        """Export all findings as JSON."""
        return JSONResponse(content=build_export_payload())

    @app.get("/api/report/markdown")
    async def get_markdown_report() -> Response:
        """Render and return the analyze session as a Markdown report.

        Reuses ``screenscribe.report.save_enhanced_markdown_report`` so the
        output matches the format the rest of the project produces (and which
        is documented as AI-fixer-friendly). Served with
        Content-Disposition: attachment so browsers offer "Save as".
        """
        try:
            # 448: build_markdown_report takes session.lock, spins a
            # TemporaryDirectory, renders and read_text()s — all blocking. Offload
            # to the threadpool so the event loop isn't held for the whole build.
            md_text = await run_in_threadpool(build_markdown_report)
        except Exception as exc:
            logger.exception("Failed to build markdown report")
            raise HTTPException(
                status_code=500,
                detail="Failed to build markdown report.",
            ) from exc

        return Response(
            content=md_text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="analyze_report.md"'},
        )

    return app
