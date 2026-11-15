"""Hosted review surface: the shipped review UI served under ``/r/{share_id}``.

The local ``review_server.create_review_app`` is single-report and guarded by
localhost Host/token checks, so it cannot be mounted for many tenants. This
module re-hosts the same contract per share:

- ``report.json`` on disk stays the single source of truth (atomic
  tempfile + fsync + ``os.replace`` writes, serialized by a per-review
  ``asyncio.Lock``), exactly like the local server;
- live manual-frame markers live in a per-review :class:`ReviewSession`
  hydrated from ``report.json`` on first access;
- the generated HTML is served with ``<base href="/r/{share_id}/">`` and a
  pre-bundle ``window.fetch`` shim that maps the UI's absolute ``/api/...``
  calls onto ``/r/{share_id}/api/...`` and attaches ``X-CSRF-Token``.

Visibility: ``private`` → owner only (401 anonymous / 403 other user);
``unlisted`` → capability URL (anyone holding the share id). Paid AI routes
(STT, manual analysis, agent) additionally require the OWNER's active
subscription; customers never supply AI keys.
"""

from __future__ import annotations

import asyncio
import base64
import email.utils
import hashlib
import html
import json
import logging
import math
import mimetypes
import os
import re
import tempfile
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from screenscribe.agent import corrections as transcript_corrections
from screenscribe.agent.chat import (
    AgentChatError,
    AgentChatRequest,
    collect_agent_chat,
    stream_agent_chat,
)
from screenscribe.agent.tools import ReportToolbelt
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import (
    _VIDEO_SRC_RE,
    JPEG_MAGIC,
    PNG_MAGIC,
    REVIEW_RESET_GENERATION_KEY,
    ManualFrameMarker,
    ManualFrameRequest,
    ManualFrameResult,
    ManualFrameUpdateRequest,
    ReviewSession,
    TranscriptCorrectionRequest,
    _data_url_from_disk,
    _recover_manual_frame_path,
    _remove_manual_frame_image,
    _report_reset_generation,
    _sweep_orphan_manual_frames,
    hydrate_state_with_session_frames,
    store_manual_frame_image,
)
from screenscribe.server_common import (
    MAX_AUDIO_BYTES,
    VALID_MARKER_SEVERITIES,
    advance_response_id_cas,
    read_upload_capped,
    sanitized_error,
    serialize_stt_result,
    transcribe_browser_audio,
    validate_browser_stt_result,
    validate_browser_stt_upload,
)
from screenscribe.shell.renderer import refresh_live_assets
from screenscribe.work_item import WorkItem, from_manual_frame, normalize_verdict

from .auth import AuthContext, CloudServices, enforce_csrf, ensure_csrf_cookie, get_services, resolve_auth
from .billing import require_active_subscription
from .db import ReviewRecord
from .i18n import LANG_COOKIE, negotiate_language, translate
from .schemas import BEARER_SCHEME
from .security import SAFE_METHODS, SHARE_ID_RE, isoformat_z, safe_child
from .worker import build_pipeline_config

logger = logging.getLogger(__name__)

STREAM_CHUNK_BYTES = 64 * 1024
MAX_HOSTED_SESSIONS = 256
REVIEW_CSP = "; ".join(
    (
        "default-src 'self'",
        # The generated report is a single file of inline <script>/<style>
        # blocks; a nonce would disable them, so inline is allowed here (and
        # only here). No eval, no remote origins.
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_SEVERITY_PRIORITY = {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3"}

# Runs before every bundled script. Rewrites same-origin "/api/..." to the
# share-scoped API, attaches the CSRF double-submit header, and aligns the
# document URL with <base href> so "#fragment" links stay in-page.
_FETCH_SHIM_TEMPLATE = """(function () {
    var prefix = __PREFIX__;
    try {
        if (location.pathname + "/" === prefix) {
            history.replaceState(history.state, "", prefix + location.search + location.hash);
        }
    } catch (e) { /* ignore */ }
    var nativeFetch = window.fetch.bind(window);
    function csrfToken() {
        var match = document.cookie.match(/(?:^|;\\s*)ss_csrf=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : "";
    }
    function scoped(raw) {
        try {
            var url = new URL(raw, location.href);
            if (url.origin !== location.origin) { return null; }
            if (url.pathname === "/api" || url.pathname.indexOf("/api/") === 0) {
                url.pathname = prefix + url.pathname.slice(1);
                return url.toString();
            }
            if (url.pathname.indexOf(prefix + "api/") === 0) { return url.toString(); }
            return null;
        } catch (e) {
            return null;
        }
    }
    window.fetch = function (input, init) {
        var isRequest = typeof Request !== "undefined" && input instanceof Request;
        var raw = typeof input === "string" ? input
            : (input instanceof URL ? input.href : (isRequest ? input.url : String(input)));
        var target = scoped(raw);
        if (target === null) { return nativeFetch(input, init); }
        var options = Object.assign({}, init || {});
        var headers = new Headers(options.headers || (isRequest ? input.headers : undefined));
        var token = csrfToken();
        if (token) { headers.set("X-CSRF-Token", token); }
        options.headers = headers;
        options.credentials = "same-origin";
        return nativeFetch(isRequest ? new Request(target, input) : target, options);
    };
})();"""


# -- Per-review runtime ------------------------------------------------------


@dataclass
class HostedReview:
    share_id: str
    output_dir: Path
    report_json: Path
    report_html: Path | None
    video_path: Path | None
    session: ReviewSession
    save_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def report_stem(self) -> str:
        source = self.report_html or self.report_json
        return Path(source.name).stem


def _rel_path(base: Path, rel: str | None) -> Path | None:
    if not rel:
        return None
    candidate = (base / rel).resolve()
    if not candidate.is_relative_to(base):
        return None
    return candidate


def open_hosted_review(record: ReviewRecord) -> HostedReview:
    """Build the runtime for one review and hydrate it from ``report.json``."""
    output_dir = record.directory.resolve()
    report_json = _rel_path(output_dir, record.report_json_rel) or (output_dir / "report.json")
    report_html = _rel_path(output_dir, record.report_html_rel)
    video_path = _rel_path(output_dir, record.video_rel) if record.has_video else None
    session = ReviewSession(
        output_dir=output_dir,
        report_filename=(report_html or report_json).name,
        video_path=video_path or (output_dir / "video-unavailable"),
    )
    entry = HostedReview(
        share_id=record.share_id,
        output_dir=output_dir,
        report_json=report_json,
        report_html=report_html,
        video_path=video_path,
        session=session,
    )
    try:
        data = load_report_json(entry)
    except HTTPException as exc:
        logger.warning("Hosted review %s has no readable report: %s", record.share_id, exc.detail)
    else:
        session.reset_generation = _report_reset_generation(data)
        hydrate_session_manual_state(entry, data)
    return entry


class ReviewRegistry:
    """Bounded LRU of live review sessions (one web process)."""

    def __init__(self, max_entries: int = MAX_HOSTED_SESSIONS) -> None:
        self._entries: OrderedDict[str, HostedReview] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max(1, max_entries)

    def get(self, record: ReviewRecord) -> HostedReview:
        with self._lock:
            entry = self._entries.get(record.share_id)
            if entry is not None:
                self._entries.move_to_end(record.share_id)
                return entry
        fresh = open_hosted_review(record)
        with self._lock:
            entry = self._entries.setdefault(record.share_id, fresh)
            self._entries.move_to_end(record.share_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
            return entry

    def drop(self, share_id: str) -> None:
        with self._lock:
            self._entries.pop(share_id, None)


def review_registry(request: Request) -> ReviewRegistry:
    registry = getattr(request.app.state, "review_registry", None)
    if not isinstance(registry, ReviewRegistry):
        registry = ReviewRegistry()
        request.app.state.review_registry = registry
    return registry


# -- report.json I/O (mirrors review_server) --------------------------------


def load_report_json(entry: HostedReview) -> dict[str, Any]:
    path = entry.report_json
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report JSON not found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Report JSON is invalid.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Report JSON is invalid.")
    return data


def write_report_json_atomic(json_path: Path, report_data: dict[str, Any]) -> None:
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(json_path.parent), prefix=".report-", suffix=".json.tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            json.dump(report_data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, json_path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# -- Review state (module-level mirrors of review_server closures) ----------


def finding_verdict(human: dict[str, Any]) -> str:
    raw_verdict = human["verdict"] if "verdict" in human else human.get("confirmed")
    return normalize_verdict(raw_verdict)


def review_finding_state(human: dict[str, Any]) -> dict[str, Any]:
    return {
        "verdict": finding_verdict(human),
        "severity": human.get("severity_override") or human.get("severity"),
        "notes": human.get("notes", ""),
        "actionItems": human.get("action_items") or human.get("actionItems", ""),
        "annotations": list(human.get("annotations") or []),
        "merged_from_ids": list(human.get("merged_from_ids") or []),
        "member_annotations": list(human.get("member_annotations") or []),
        "merged_member_reviews": dict(human.get("merged_member_reviews") or {}),
        "merged_survivor_review": dict(human.get("merged_survivor_review") or {}),
        "merged_review_baseline": dict(human.get("merged_review_baseline") or {}),
        "summary_override": human.get("summary_override") or "",
        "category_override": human.get("category_override") or "",
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
        status="reviewed" if finding_verdict(human) != "none" else "pending",
        frame=frame,
        analysis=analysis,
        human_review=human,
    )


def work_item_from_review_manual_frame(
    frame: dict[str, Any], *, reviewer: str, reviewed_at: str
) -> WorkItem:
    result = dict(frame.get("result") or {})
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


def restore_manual_frame_image(entry: HostedReview, frame: dict[str, Any]) -> None:
    if frame.get("frameDataUrl"):
        return
    frame_path = frame.get("frame_path")
    if not isinstance(frame_path, str) or not frame_path:
        frame_path = _recover_manual_frame_path(entry.output_dir, frame.get("marker_id"))
        if not frame_path:
            return
        frame["frame_path"] = frame_path
    data_url = _data_url_from_disk(entry.output_dir, frame_path)
    if data_url is None:
        frame["imageMissing"] = True
        return
    frame["frameDataUrl"] = data_url


def build_review_state_from_report(entry: HostedReview, report_data: dict[str, Any]) -> dict[str, Any]:
    human_raw = report_data.get("human_review")
    human = human_raw if isinstance(human_raw, dict) else {}
    findings = {
        str(fid): review_finding_state(review)
        for fid, review in (human.get("findings") or {}).items()
        if isinstance(review, dict)
    }
    manual_frames_raw = human.get("manual_frames")
    manual_frames: list[dict[str, Any]]
    if isinstance(manual_frames_raw, list):
        manual_frames = [dict(frame) for frame in manual_frames_raw if isinstance(frame, dict)]
    else:
        manual_raw = report_data.get("manual_review")
        manual_review = manual_raw if isinstance(manual_raw, dict) else {}
        markers = manual_review.get("markers") or []
        results_raw = manual_review.get("results") or []
        results = {
            str(result.get("marker_id")): result
            for result in results_raw
            if isinstance(result, dict) and result.get("marker_id") is not None
        }
        manual_frames = []
        for marker in markers:
            if not isinstance(marker, dict):
                continue
            payload = dict(marker)
            marker_id = str(marker.get("marker_id", ""))
            if marker_id in results:
                payload["result"] = results[marker_id]
            manual_frames.append(payload)
    for frame in manual_frames:
        restore_manual_frame_image(entry, frame)
    return {
        "findings": findings,
        "manualFrames": manual_frames,
        "reviewer": human.get("reviewer", ""),
        "modified": False,
    }


def hydrate_session_manual_state(entry: HostedReview, report_data: dict[str, Any]) -> None:
    state = build_review_state_from_report(entry, report_data)
    restored_markers: dict[str, ManualFrameMarker] = {}
    restored_results: dict[str, ManualFrameResult] = {}
    for frame in state.get("manualFrames") or []:
        if not isinstance(frame, dict):
            continue
        marker_id = str(frame.get("marker_id") or "")
        frame_path = str(frame.get("frame_path") or "")
        if not marker_id or not frame_path:
            continue
        timestamp_value = frame.get("timestamp")
        if isinstance(timestamp_value, bool) or not isinstance(timestamp_value, int | float | str):
            continue
        try:
            timestamp = float(timestamp_value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(timestamp) or timestamp < 0:
            continue
        data_url = frame.get("frameDataUrl")
        frame_base64 = data_url.split(",", 1)[1] if isinstance(data_url, str) and "," in data_url else ""
        result_payload = frame.get("result")
        result = result_payload if isinstance(result_payload, dict) else None
        restored_markers[marker_id] = ManualFrameMarker(
            marker_id=marker_id,
            timestamp=timestamp,
            frame_base64=frame_base64,
            transcript=str(frame.get("transcript") or ""),
            notes=str(frame.get("notes") or ""),
            status=str(frame.get("status") or ("completed" if result else "pending")),
            frame_path=frame_path,
            severity=str(frame["severity"]) if frame.get("severity") is not None else None,
        )
        if result is not None:
            restored_results[marker_id] = ManualFrameResult(
                marker_id=marker_id,
                timestamp=timestamp,
                category=str(result.get("category") or "manual_capture"),
                severity=str(result.get("severity") or "medium"),
                summary=str(result.get("summary") or ""),
                issues_detected=[str(item) for item in result.get("issues_detected") or []],
                suggested_fix=str(result.get("suggested_fix") or ""),
                affected_components=[str(item) for item in result.get("affected_components") or []],
                response_id=str(result.get("response_id") or ""),
            )
    with entry.session.lock:
        entry.session.markers.update(restored_markers)
        entry.session.results.update(restored_results)


def review_state_snapshot(entry: HostedReview) -> dict[str, Any]:
    session = entry.session
    report_data = load_report_json(entry)
    state = build_review_state_from_report(entry, report_data)
    with session.lock:
        session.reset_generation = max(session.reset_generation, _report_reset_generation(report_data))
        session_markers = list(session.markers.values())
        reset_generation = session.reset_generation
    hydrate_state_with_session_frames(state, session_markers)
    state["resetGeneration"] = reset_generation
    loaded = transcript_corrections.load_ledger(entry.output_dir)
    state["corrections"] = loaded.entries
    if loaded.warning:
        logger.warning("Corrections ledger ignored for %s: %s", entry.share_id, loaded.warning)
        state["correctionsWarning"] = loaded.warning
    return state


def apply_review_save(entry: HostedReview, review: Any) -> dict[str, Any]:
    """``/api/save`` merge: markers/results + posted human review → report.json."""
    session = entry.session
    report_data = load_report_json(entry)
    requested_generation = review.get("resetGeneration", 0) if isinstance(review, dict) else 0
    if (
        isinstance(requested_generation, bool)
        or not isinstance(requested_generation, int)
        or requested_generation < 0
    ):
        raise HTTPException(status_code=422, detail="Invalid review reset generation.")
    with session.lock:
        session.reset_generation = max(session.reset_generation, _report_reset_generation(report_data))
        current_generation = session.reset_generation
        markers = list(session.markers.values())
        results = list(session.results.values())
    if requested_generation != current_generation:
        raise HTTPException(status_code=409, detail="Review reset invalidated this save.")
    report_data[REVIEW_RESET_GENERATION_KEY] = current_generation
    manual_findings = [
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
        for res in results
    ]
    manual_review = report_data.get("manual_review")
    if not isinstance(manual_review, dict):
        manual_review = {}
        report_data["manual_review"] = manual_review
    manual_review["markers"] = [
        {
            "marker_id": m.marker_id,
            "timestamp": m.timestamp,
            "transcript": m.transcript,
            "notes": m.notes,
            "frame_path": m.frame_path,
            "severity": m.severity,
        }
        for m in markers
    ]
    frame_path_by_id = {m.marker_id: m.frame_path for m in markers}
    manual_review["results"] = manual_findings

    if isinstance(review, dict) and review.get("findings") is not None:
        findings_review: dict[str, Any] = {}
        rejected_ids: list[Any] = []
        work_items: list[dict[str, Any]] = []
        review_findings = review.get("findings")
        for finding in review_findings if isinstance(review_findings, list) else []:
            if not isinstance(finding, dict):
                continue
            fid = finding.get("id")
            human = finding.get("human_review")
            if fid is None or not isinstance(human, dict):
                continue
            findings_review[str(fid)] = human
            work_items.append(work_item_from_review_finding(finding).to_dict())
            if finding_verdict(human) == "rejected":
                rejected_ids.append(fid)
        posted_frames = review.get("manual_frames")
        persisted_manual_frames: list[dict[str, Any]] = []
        for frame in posted_frames if isinstance(posted_frames, list) else []:
            if not isinstance(frame, dict):
                continue
            stored = {k: v for k, v in frame.items() if k != "frameDataUrl"}
            disk_path = frame_path_by_id.get(str(frame.get("marker_id", "")))
            if disk_path:
                stored["frame_path"] = disk_path
            elif isinstance(frame.get("frame_path"), str):
                stored["frame_path"] = frame["frame_path"]
            persisted_manual_frames.append(stored)
        reviewed_at = str(review.get("reviewed_at") or "")
        reviewer = str(review.get("reviewer") or "")
        work_items.extend(
            work_item_from_review_manual_frame(frame, reviewer=reviewer, reviewed_at=reviewed_at).to_dict()
            for frame in persisted_manual_frames
        )
        report_data["human_review"] = {
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "findings": findings_review,
            "rejected_ids": rejected_ids,
            "manual_frames": persisted_manual_frames,
        }
        report_data["work_items"] = work_items
    elif markers or results:
        result_by_id = {result.marker_id: result for result in results}
        report_data["work_items"] = [
            from_manual_frame(marker, result_by_id.get(marker.marker_id)).to_dict()
            for marker in markers
        ]

    write_report_json_atomic(entry.report_json, report_data)

    keep: set[str] = {m.frame_path for m in markers if m.frame_path}
    human_review = report_data.get("human_review")
    if isinstance(human_review, dict):
        for frame in human_review.get("manual_frames") or []:
            if isinstance(frame, dict) and isinstance(frame.get("frame_path"), str):
                keep.add(frame["frame_path"])
    for marker in manual_review.get("markers") or []:
        if isinstance(marker, dict) and isinstance(marker.get("frame_path"), str):
            keep.add(marker["frame_path"])
    _sweep_orphan_manual_frames(entry.output_dir, keep)
    return {"status": "success", "filename": entry.report_json.name}


def apply_review_reset(entry: HostedReview) -> dict[str, Any]:
    session = entry.session
    report_data = load_report_json(entry)
    with session.lock:
        reset_generation = (
            max(session.reset_generation, _report_reset_generation(report_data)) + 1
        )
    report_data.pop("human_review", None)
    report_data.pop("manual_review", None)
    work_items = report_data.get("work_items")
    if isinstance(work_items, list):
        retained = [
            item
            for item in work_items
            if not isinstance(item, dict)
            or item.get("source") not in {"review_detection", "review_manual_frame"}
        ]
        if retained:
            report_data["work_items"] = retained
        else:
            report_data.pop("work_items", None)
    report_data[REVIEW_RESET_GENERATION_KEY] = reset_generation
    write_report_json_atomic(entry.report_json, report_data)
    with session.lock:
        session.reset_generation = reset_generation
        session.markers.clear()
        session.results.clear()
        session.last_response_id = ""
    try:
        _sweep_orphan_manual_frames(entry.output_dir, set())
    except Exception as cleanup_exc:
        logger.warning("Review reset committed, but manual-frame cleanup failed: %s", cleanup_exc)
    return {
        "status": "reset",
        "state": {
            "findings": {},
            "manualFrames": [],
            "reviewer": "",
            "modified": False,
            "resetGeneration": reset_generation,
        },
        "resetGeneration": reset_generation,
    }


def analyze_single_marker(
    entry: HostedReview, marker_id: str, config: ScreenScribeConfig
) -> dict[str, Any]:
    """Run unified VLM analysis on one manual frame (threadpool)."""
    from screenscribe.detect import Detection
    from screenscribe.transcribe import Segment
    from screenscribe.unified_analysis import analyze_finding_unified

    session = entry.session
    with session.lock:
        marker = session.markers.get(marker_id)
        if marker is None:
            raise HTTPException(status_code=404, detail="Manual frame not found")
        if marker.status == "analyzing":
            raise HTTPException(status_code=409, detail="Manual frame analysis already in progress")
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

    def mark_error() -> None:
        with session.lock:
            session.results.pop(marker_id, None)
            marker.status = "error"

    try:
        frame_bytes = base64.b64decode(marker.frame_base64, validate=True)
    except Exception as exc:
        mark_error()
        raise HTTPException(status_code=400, detail="Invalid base64 frame data.") from exc
    if not (frame_bytes.startswith(JPEG_MAGIC) or frame_bytes.startswith(PNG_MAGIC)):
        mark_error()
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
            output_dir=entry.output_dir,
        )
        if not finding:
            mark_error()
            return {"marker_id": marker_id, "status": "error", "error": "Analysis failed"}
        if getattr(finding, "confidence", "high") == "degraded":
            mark_error()
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
            if marker_id not in session.markers:
                session.results.pop(marker_id, None)
                return {
                    "marker_id": marker_id,
                    "status": "error",
                    "error": "Manual frame deleted during analysis",
                }
            if marker.severity is not None:
                result.severity = marker.severity
            session.results[marker_id] = result
            advance_response_id_cas(session, previous_response_id, finding.response_id, logger=logger)
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
        mark_error()
        err = sanitized_error(exc, "marker_analysis_failed")
        return {
            "marker_id": marker_id,
            "status": "error",
            "error": err["message"],
            "error_code": err["error_code"],
        }
    finally:
        screenshot_path.unlink(missing_ok=True)


# -- Agent manifest (server-side handoff) -----------------------------------


def _action_items(review: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
    raw = review.get("action_items") or review.get("actionItems") or analysis.get("action_items")
    if isinstance(raw, str):
        return [line.strip(" -*\t") for line in raw.splitlines() if line.strip(" -*\t")]
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


def build_agent_manifest(services: CloudServices, record: ReviewRecord, report: dict[str, Any]) -> dict[str, Any]:
    """Structured coding-agent handoff derived from the canonical report.json.

    Mirrors the browser ZIP export's ``agent_manifest.json`` field names. The
    server cannot rasterize browser annotations, so ``screenshot`` points at
    the original frame and annotations are summarized from the saved review.
    """
    settings = services.settings
    share = record.share_id
    human_raw = report.get("human_review")
    human = human_raw if isinstance(human_raw, dict) else {}
    reviews_raw = human.get("findings")
    reviews = reviews_raw if isinstance(reviews_raw, dict) else {}
    findings: list[dict[str, Any]] = []
    rejected: list[str] = []
    for position, finding in enumerate(report.get("findings") or [], start=1):
        if not isinstance(finding, dict):
            continue
        finding_id = str(finding.get("id", position))
        review_raw = reviews.get(finding_id)
        review = review_raw if isinstance(review_raw, dict) else {}
        verdict = finding_verdict(review) if review else "none"
        if verdict == "rejected":
            rejected.append(finding_id)
            continue
        analysis_raw = finding.get("unified_analysis")
        analysis = analysis_raw if isinstance(analysis_raw, dict) else {}
        severity = str(
            review.get("severity_override") or review.get("severity") or analysis.get("severity") or "medium"
        ).lower()
        screenshot = finding.get("screenshot")
        screenshot_name = screenshot if isinstance(screenshot, str) and screenshot else None
        findings.append(
            {
                "id": f"F{len(findings) + 1}",
                "finding_id": finding_id,
                "priority": _SEVERITY_PRIORITY.get(severity, "P2"),
                "severity": severity,
                "title": str(
                    review.get("summary_override") or analysis.get("summary") or finding.get("text") or ""
                ),
                "user_said": str(finding.get("text") or ""),
                "context": str(finding.get("context") or ""),
                "category": str(review.get("category_override") or finding.get("category") or ""),
                "timestamp": finding.get("timestamp_start"),
                "timestamp_formatted": finding.get("timestamp_formatted"),
                "review_verdict": verdict,
                "reviewer_notes": str(review.get("notes") or ""),
                "annotations": list(review.get("annotations") or []),
                "action_items": _action_items(review, analysis),
                "suggested_fix": str(analysis.get("suggested_fix") or ""),
                "affected_components": [str(item) for item in analysis.get("affected_components") or []],
                "screenshot": f"screenshots/{screenshot_name}" if screenshot_name else None,
                "screenshot_url": (
                    settings.url(f"/r/{share}/screenshots/{quote(screenshot_name)}")
                    if screenshot_name
                    else None
                ),
                "status": "pending",
            }
        )
    manual_frames: list[dict[str, Any]] = []
    frames_raw = human.get("manual_frames")
    if not isinstance(frames_raw, list):
        manual_raw = report.get("manual_review")
        manual = manual_raw if isinstance(manual_raw, dict) else {}
        results = {
            str(item.get("marker_id")): item
            for item in manual.get("results") or []
            if isinstance(item, dict)
        }
        frames_raw = [
            {**marker, "result": results.get(str(marker.get("marker_id")))}
            for marker in manual.get("markers") or []
            if isinstance(marker, dict)
        ]
    for index, frame in enumerate(frames_raw, start=1):
        if not isinstance(frame, dict):
            continue
        result_raw = frame.get("result")
        result = result_raw if isinstance(result_raw, dict) else {}
        frame_path = frame.get("frame_path")
        frame_name = Path(frame_path).name if isinstance(frame_path, str) and frame_path else None
        severity_value = frame.get("severity") or result.get("severity")
        manual_frames.append(
            {
                "id": f"M{index:02d}",
                "source": "manual_capture",
                "timestamp": frame.get("timestamp"),
                "timestamp_formatted": frame.get("timestamp_formatted"),
                "transcript": str(frame.get("transcript") or ""),
                "notes": str(frame.get("notes") or ""),
                "summary": str(result.get("summary") or ""),
                "status": "ai-analyzed" if result.get("summary") else "not AI-analyzed yet",
                "severity": None if severity_value in (None, "", "none") else str(severity_value),
                "issues_detected": [str(item) for item in result.get("issues_detected") or []],
                "screenshot": f"manual_frames/{frame_name}" if frame_name else None,
                "screenshot_url": (
                    settings.url(f"/r/{share}/manual_frames/{quote(frame_name)}") if frame_name else None
                ),
            }
        )
    return {
        "meta": {
            "version": "1.0",
            "generated_at": isoformat_z(services.now()),
            "video": str(report.get("video") or record.video_name),
            "reviewer": str(human.get("reviewer") or ""),
            "reviewed_at": str(human.get("reviewed_at") or ""),
            "total_findings": len(findings),
            "total_manual_frames": len(manual_frames),
            "transcript_source": str(report.get("transcript_source") or "unknown"),
            "share_id": share,
            "share_url": settings.url(f"/r/{share}"),
            "report_json_url": settings.url(f"/api/v1/reviews/{share}/report.json"),
        },
        "findings": findings,
        "manual_frames": manual_frames,
        "rejected_ids": rejected,
    }


# -- HTML rendering ----------------------------------------------------------


def fetch_shim_script(share_id: str) -> str:
    return _FETCH_SHIM_TEMPLATE.replace("__PREFIX__", json.dumps(f"/r/{share_id}/"))


def render_hosted_report(entry: HostedReview) -> str:
    """Generated HTML + live assets, video src → share stream, base + shim injected."""
    html_path = entry.report_html
    if html_path is None or not html_path.is_file():
        raise HTTPException(status_code=404, detail="Report not found.")
    document = refresh_live_assets(html_path.read_text(encoding="utf-8"))
    video_src = f"/r/{entry.share_id}/video"

    def _video(match: re.Match[str]) -> str:
        current = match.group(2)
        if current.startswith("data:") or entry.video_path is None:
            return match.group(0)
        return f"{match.group(1)}{video_src}{match.group(3)}"

    document = _VIDEO_SRC_RE.sub(_video, document, count=1)
    injection = (
        f'<base href="/r/{entry.share_id}/">'
        f"<script>\n{fetch_shim_script(entry.share_id)}\n</script>"
    )
    head = _HEAD_OPEN_RE.search(document)
    if head is not None:
        return document[: head.end()] + injection + document[head.end() :]
    root = _HTML_OPEN_RE.search(document)
    if root is not None:
        return document[: root.end()] + f"<head>{injection}</head>" + document[root.end() :]
    return f"<head>{injection}</head>" + document


def gate_page(request: Request, status_code: int, share_id: str) -> HTMLResponse:
    lang = negotiate_language(
        query=request.query_params.get("lang"),
        cookie=request.cookies.get(LANG_COOKIE),
        accept_language=request.headers.get("accept-language"),
    )
    key = "review.signin" if status_code == 401 else "review.forbidden"
    login_href = f"/login?next={quote(f'/r/{share_id}', safe='')}"
    body = (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(translate(lang, 'brand'))}</title>"
        '<link rel="stylesheet" href="/static/portal.css"></head><body><main class="card">'
        f"<h1>{html.escape(translate(lang, key + '.title'))}</h1>"
        f"<p>{html.escape(translate(lang, key + '.body'))}</p>"
        f'<p><a class="button" href="{html.escape(login_href)}">'
        f"{html.escape(translate(lang, 'nav.login'))}</a></p></main></body></html>"
    )
    response = HTMLResponse(content=body, status_code=status_code)
    if status_code == 401:
        response.headers["WWW-Authenticate"] = BEARER_SCHEME
    return response


# -- Video streaming (RFC 9110 Range) ----------------------------------------


class RangeNotSatisfiable(Exception):
    pass


def parse_byte_range(header: str | None, total: int) -> tuple[int, int] | None:
    """Return the inclusive ``(start, end)`` to serve, or ``None`` for the full entity.

    Supports ``bytes=a-b``, ``bytes=a-`` and ``bytes=-n``. Multi-range and
    non-``bytes`` units are ignored (the full entity is served, as RFC 9110
    permits); unsatisfiable byte ranges raise :class:`RangeNotSatisfiable`.
    """
    if header is None:
        return None
    value = header.strip().replace(" ", "")
    if not value.lower().startswith("bytes="):
        return None
    if "," in value:
        return None
    match = _RANGE_RE.match("bytes=" + value[6:])
    if match is None:
        raise RangeNotSatisfiable
    first, last = match.groups()
    if not first and not last:
        raise RangeNotSatisfiable
    if total <= 0:
        raise RangeNotSatisfiable
    if not first:
        suffix = int(last)
        if suffix == 0:
            raise RangeNotSatisfiable
        return max(0, total - suffix), total - 1
    start = int(first)
    if start >= total:
        raise RangeNotSatisfiable
    end = total - 1 if not last else min(int(last), total - 1)
    if end < start:
        raise RangeNotSatisfiable
    return start, end


def _entity_tag(stat: os.stat_result) -> str:
    digest = hashlib.sha256(f"{stat.st_ino}-{stat.st_size}-{stat.st_mtime_ns}".encode()).hexdigest()
    return f'"{digest[:32]}"'


async def _file_chunks(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    async with await anyio.open_file(path, "rb") as handle:
        await handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = await handle.read(min(STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


async def video_response(request: Request, video_path: Path | None) -> Response:
    if video_path is None:
        raise HTTPException(status_code=404, detail="Video not available.")
    try:
        stat = await anyio.Path(video_path).stat()
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Video not available.") from exc
    total = stat.st_size
    etag = _entity_tag(stat)
    media_type = mimetypes.guess_type(video_path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": email.utils.formatdate(stat.st_mtime, usegmt=True),
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
    }
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header is not None and if_range is not None and if_range.strip() != etag:
        range_header = None
    if range_header is None and request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=304, headers=headers)
    try:
        selected = parse_byte_range(range_header, total)
    except RangeNotSatisfiable:
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{total}"},
        )
    if selected is None:
        start, end, status_code = 0, total - 1, 200
    else:
        start, end = selected
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    if request.method.upper() == "HEAD":
        return Response(status_code=status_code, headers=headers, media_type=media_type)
    return StreamingResponse(
        _file_chunks(video_path, start, length),
        status_code=status_code,
        headers=headers,
        media_type=media_type,
    )


# -- Access control ----------------------------------------------------------


@dataclass(frozen=True)
class ReviewAccess:
    record: ReviewRecord
    auth: AuthContext | None
    entry: HostedReview

    @property
    def is_owner(self) -> bool:
        return self.auth is not None and self.auth.user.user_id == self.record.user_id


async def lookup_review(request: Request, share_id: str) -> tuple[ReviewRecord, AuthContext | None]:
    """Resolve a share and enforce visibility (no CSRF; read-only callers)."""
    if not SHARE_ID_RE.match(share_id):
        raise HTTPException(status_code=404, detail="Review not found.")
    services = get_services(request)
    record = await run_in_threadpool(services.store.get_review_by_share, share_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    ctx = await resolve_auth(request)
    if record.visibility != "unlisted":
        if ctx is None:
            raise HTTPException(
                status_code=401,
                detail="Sign in to open this review.",
                headers={"WWW-Authenticate": BEARER_SCHEME},
            )
        if ctx.user.user_id != record.user_id:
            raise HTTPException(status_code=403, detail="This review is private.")
    return record, ctx


async def review_access(request: Request, share_id: str) -> ReviewAccess:
    record, ctx = await lookup_review(request, share_id)
    # Cookie sessions AND anonymous capability holders must pass Origin +
    # CSRF double-submit on writes; bearer clients are not ambient.
    if request.method.upper() not in SAFE_METHODS and (ctx is None or ctx.via_cookie):
        await enforce_csrf(request)
    entry = await run_in_threadpool(review_registry(request).get, record)
    return ReviewAccess(record=record, auth=ctx, entry=entry)


Access = Annotated[ReviewAccess, Depends(review_access)]


async def paid_ai_config(request: Request, access: ReviewAccess) -> ScreenScribeConfig:
    """Paid AI under ``/r/``: server capacity + the owner's active subscription."""
    services = get_services(request)
    if not services.settings.has_ai_capacity:
        raise HTTPException(status_code=503, detail="AI capacity is not configured on this server.")
    await run_in_threadpool(require_active_subscription, services, access.record.user_id)
    return build_pipeline_config(services.settings, language=access.record.language or "en")


def _asset_response(base: Path, filename: str) -> FileResponse:
    target = safe_child(base, filename)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    media_type = mimetypes.guess_type(target.name)[0] or ""
    if not media_type.startswith("image/"):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


def _glossary_dir(services: CloudServices, record: ReviewRecord) -> Path:
    directory = services.settings.glossary_root / record.user_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _toolbelt(services: CloudServices, access: ReviewAccess, report: dict[str, Any]) -> ReportToolbelt:
    return ReportToolbelt(
        report,
        output_dir=access.entry.output_dir,
        repo_root=None,
        report_stem=access.entry.report_stem,
        glossary_dir=_glossary_dir(services, access.record),
        video_path=access.entry.video_path,
    )


def _checked_agent_images(payload: AgentChatRequest) -> list[dict[str, str]]:
    for image in payload.images:
        problem = image.problem()
        if problem is not None:
            raise HTTPException(status_code=422, detail=f"Attached image rejected: {problem}")
    return [image.as_dict() for image in payload.images]


# -- Router ------------------------------------------------------------------


def build_review_host_router() -> APIRouter:
    router = APIRouter(tags=["hosted-review"])

    async def _page(request: Request, share_id: str) -> Response:
        services = get_services(request)
        try:
            record, _auth = await lookup_review(request, share_id)
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                return gate_page(request, exc.status_code, share_id)
            raise
        entry = await run_in_threadpool(review_registry(request).get, record)
        document = await run_in_threadpool(render_hosted_report, entry)
        response = HTMLResponse(
            content=document,
            headers={
                "Content-Security-Policy": REVIEW_CSP,
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )
        ensure_csrf_cookie(request, response, services.settings)
        return response

    @router.get("/r/{share_id}", response_class=HTMLResponse)
    async def hosted_report(request: Request, share_id: str) -> Response:
        return await _page(request, share_id)

    @router.get("/r/{share_id}/", response_class=HTMLResponse, include_in_schema=False)
    async def hosted_report_slash(request: Request, share_id: str) -> Response:
        return await _page(request, share_id)

    @router.api_route("/r/{share_id}/video", methods=["GET", "HEAD"])
    async def hosted_video(request: Request, access: Access) -> Response:
        return await video_response(request, access.entry.video_path)

    @router.get("/r/{share_id}/screenshots/{filename}")
    async def hosted_screenshot(access: Access, filename: str) -> FileResponse:
        return _asset_response(access.entry.output_dir / "screenshots", filename)

    @router.get("/r/{share_id}/manual_frames/{filename}")
    async def hosted_manual_frame(access: Access, filename: str) -> FileResponse:
        return _asset_response(access.entry.output_dir / "manual_frames", filename)

    api = "/r/{share_id}/api"

    @router.get(f"{api}/review-state")
    async def review_state(access: Access) -> JSONResponse:
        async with access.entry.save_lock:
            state = await run_in_threadpool(review_state_snapshot, access.entry)
        return JSONResponse(content=state)

    @router.post(f"{api}/save")
    async def save_review(request: Request, access: Access) -> dict[str, Any]:
        try:
            review = await request.json()
        except Exception:
            review = None
        async with access.entry.save_lock:
            try:
                return await run_in_threadpool(apply_review_save, access.entry, review)
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Hosted save failed for %s: %s", access.record.share_id, type(exc).__name__)
                raise HTTPException(status_code=500, detail="Failed to save review state.") from exc

    @router.post(f"{api}/reset-review")
    async def reset_review(access: Access) -> JSONResponse:
        if not access.is_owner:
            raise HTTPException(status_code=403, detail="Only the review owner can reset it.")
        async with access.entry.save_lock:
            try:
                outcome = await run_in_threadpool(apply_review_reset, access.entry)
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Hosted reset failed for %s: %s", access.record.share_id, type(exc).__name__)
                raise HTTPException(status_code=500, detail="Failed to reset review state.") from exc
        return JSONResponse(content=outcome)

    @router.post(f"{api}/corrections")
    async def save_correction(
        request: Request, access: Access, body: TranscriptCorrectionRequest
    ) -> JSONResponse:
        services = get_services(request)
        glossary = await run_in_threadpool(_glossary_dir, services, access.record)
        async with access.entry.save_lock:
            try:
                entry = await run_in_threadpool(
                    lambda: transcript_corrections.append_correction(
                        access.entry.output_dir,
                        match=body.match,
                        replacement=body.replacement,
                        scope=body.scope,
                        note=body.note,
                        source=body.source or "user",
                        report_stem=access.entry.report_stem,
                        glossary_dir=glossary,
                    )
                )
            except transcript_corrections.CorrectionRejected as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except OSError as exc:
                raise HTTPException(status_code=500, detail="Failed to write corrections ledger.") from exc
            loaded = await run_in_threadpool(transcript_corrections.load_ledger, access.entry.output_dir)
        return JSONResponse(
            content={"status": "saved", "entry": dict(entry), "corrections": loaded.entries}
        )

    @router.post(f"{api}/manual-mark")
    async def mark_manual_frame(access: Access, body: ManualFrameRequest) -> JSONResponse:
        session = access.entry.session
        with session.lock:
            if body.reset_generation != session.reset_generation:
                raise HTTPException(status_code=409, detail="Review reset invalidated this manual frame.")
        marker_id = str(uuid.uuid4())
        frame_path = await run_in_threadpool(
            store_manual_frame_image, access.entry.output_dir, marker_id, body.frame_base64
        )
        marker = ManualFrameMarker(
            marker_id=marker_id,
            timestamp=body.timestamp,
            frame_base64=body.frame_base64,
            transcript=body.transcript,
            notes=body.notes,
            status="pending",
            frame_path=frame_path,
        )
        with session.lock:
            stale = body.reset_generation != session.reset_generation
            if not stale:
                session.markers[marker_id] = marker
        if stale:
            _remove_manual_frame_image(access.entry.output_dir, frame_path)
            raise HTTPException(status_code=409, detail="Review reset invalidated this manual frame.")
        return JSONResponse(
            content={
                "marker_id": marker_id,
                "status": "pending",
                "frame_path": frame_path,
                "resetGeneration": body.reset_generation,
            }
        )

    @router.delete(f"{api}/manual-mark/{{marker_id}}")
    async def delete_manual_frame(access: Access, marker_id: str) -> JSONResponse:
        session = access.entry.session
        with session.lock:
            marker = session.markers.pop(marker_id, None)
            session.results.pop(marker_id, None)
        if marker is not None and marker.frame_path:
            _remove_manual_frame_image(access.entry.output_dir, marker.frame_path)
        return JSONResponse(content={"status": "deleted", "marker_id": marker_id})

    @router.patch(f"{api}/manual-mark/{{marker_id}}")
    async def update_manual_frame(
        access: Access, marker_id: str, body: ManualFrameUpdateRequest
    ) -> JSONResponse:
        session = access.entry.session
        with session.lock:
            marker = session.markers.get(marker_id)
            if marker is None:
                raise HTTPException(status_code=404, detail="Manual frame not found")
            if body.transcript is not None:
                marker.transcript = body.transcript
            if body.notes is not None:
                marker.notes = body.notes
            if body.severity is not None:
                severity = body.severity.strip().lower()
                if severity in VALID_MARKER_SEVERITIES:
                    marker.severity = severity
                    existing = session.results.get(marker_id)
                    if existing is not None:
                        existing.severity = severity
            updated = {
                "marker_id": marker.marker_id,
                "transcript": marker.transcript,
                "notes": marker.notes,
                "severity": marker.severity,
            }
        return JSONResponse(content=updated)

    @router.post(f"{api}/manual-analyze/{{marker_id}}")
    async def analyze_manual_frame(
        request: Request, access: Access, marker_id: str, reset_generation: int | None = None
    ) -> JSONResponse:
        session = access.entry.session
        with session.lock:
            if reset_generation is not None and reset_generation != session.reset_generation:
                raise HTTPException(
                    status_code=409, detail="Review reset invalidated this manual frame analysis."
                )
        config = await paid_ai_config(request, access)
        outcome = await run_in_threadpool(analyze_single_marker, access.entry, marker_id, config)
        with session.lock:
            current_generation = session.reset_generation
        if reset_generation is not None and reset_generation != current_generation:
            raise HTTPException(
                status_code=409, detail="Review reset invalidated this manual frame analysis."
            )
        outcome["resetGeneration"] = current_generation
        return JSONResponse(content=outcome)

    @router.post(f"{api}/stt")
    async def transcribe_voice(
        request: Request,
        access: Access,
        audio: Annotated[UploadFile, File(description="Browser-recorded audio")],
        reset_generation: int | None = None,
    ) -> JSONResponse:
        config = await paid_ai_config(request, access)
        audio_data = await read_upload_capped(audio, max_bytes=MAX_AUDIO_BYTES)
        validate_browser_stt_upload(audio_data)
        filename = audio.filename or "recording.webm"
        content_type = audio.content_type or "audio/webm"
        session = access.entry.session
        with session.lock:
            if reset_generation is not None and reset_generation != session.reset_generation:
                raise HTTPException(status_code=409, detail="Review reset invalidated this voice transcription.")
            previous_response_id = session.last_response_id
        try:
            result = await run_in_threadpool(
                transcribe_browser_audio,
                audio_data,
                filename=filename,
                content_type=content_type,
                config=config,
                output_dir=access.entry.output_dir,
            )
            quality_warning = validate_browser_stt_result(result)
            with session.lock:
                if reset_generation is not None and reset_generation != session.reset_generation:
                    raise HTTPException(
                        status_code=409, detail="Review reset invalidated this voice transcription."
                    )
                advance_response_id_cas(session, previous_response_id, result.response_id, logger=logger)
            return JSONResponse(content=serialize_stt_result(result, quality_warning=quality_warning))
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Hosted STT failed (%s): %s", content_type, type(exc).__name__)
            raise HTTPException(
                status_code=502, detail="Voice transcription failed. Try again in a moment."
            ) from exc

    @router.post(f"{api}/agent/stt")
    async def agent_dictation(
        request: Request,
        access: Access,
        audio: Annotated[UploadFile, File(description="Browser-recorded agent dictation")],
    ) -> JSONResponse:
        config = await paid_ai_config(request, access)
        audio_data = await read_upload_capped(audio, max_bytes=MAX_AUDIO_BYTES)
        validate_browser_stt_upload(audio_data)
        filename = audio.filename or "dictation.webm"
        content_type = audio.content_type or "audio/webm"
        try:
            result = await run_in_threadpool(
                transcribe_browser_audio,
                audio_data,
                filename=filename,
                content_type=content_type,
                config=config,
                output_dir=access.entry.output_dir,
            )
            quality_warning = validate_browser_stt_result(result)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Hosted agent dictation failed (%s): %s", content_type, type(exc).__name__)
            raise HTTPException(
                status_code=502, detail="Voice transcription failed. Try again in a moment."
            ) from exc
        return JSONResponse(content=serialize_stt_result(result, quality_warning=quality_warning))

    @router.post(f"{api}/agent/chat/stream")
    async def agent_chat_stream(
        request: Request, access: Access, payload: AgentChatRequest
    ) -> StreamingResponse:
        services = get_services(request)
        config = await paid_ai_config(request, access)
        report = await run_in_threadpool(load_report_json, access.entry)
        toolbelt = _toolbelt(services, access, report)
        images = _checked_agent_images(payload)

        async def frames() -> AsyncIterator[str]:
            async for frame in stream_agent_chat(
                config=config,
                report=report,
                tools=toolbelt,
                message=payload.message,
                history=payload.history,
                previous_response_id=payload.previous_response_id,
                previous_response_provider=payload.previous_response_provider,
                previous_response_protocol=payload.previous_response_protocol,
                previous_response_host=payload.previous_response_host,
                images=images,
            ):
                yield frame

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(f"{api}/agent/chat")
    async def agent_chat(request: Request, access: Access, payload: AgentChatRequest) -> dict[str, Any]:
        services = get_services(request)
        config = await paid_ai_config(request, access)
        report = await run_in_threadpool(load_report_json, access.entry)
        toolbelt = _toolbelt(services, access, report)
        images = _checked_agent_images(payload)
        try:
            return await collect_agent_chat(
                config=config,
                report=report,
                tools=toolbelt,
                message=payload.message,
                history=payload.history,
                previous_response_id=payload.previous_response_id,
                previous_response_provider=payload.previous_response_provider,
                previous_response_protocol=payload.previous_response_protocol,
                previous_response_host=payload.previous_response_host,
                images=images,
            )
        except AgentChatError as exc:
            raise HTTPException(
                status_code=502, detail="Review agent request failed. Check server logs."
            ) from exc

    @router.get(f"{api}/export/agent-manifest")
    async def agent_manifest(request: Request, access: Access) -> JSONResponse:
        services = get_services(request)
        report = await run_in_threadpool(load_report_json, access.entry)
        return JSONResponse(content=build_agent_manifest(services, access.record, report))

    return router


__all__ = [
    "REVIEW_CSP",
    "Access",
    "HostedReview",
    "RangeNotSatisfiable",
    "ReviewAccess",
    "ReviewRegistry",
    "analyze_single_marker",
    "apply_review_reset",
    "apply_review_save",
    "build_agent_manifest",
    "build_review_host_router",
    "build_review_state_from_report",
    "fetch_shim_script",
    "load_report_json",
    "lookup_review",
    "open_hosted_review",
    "parse_byte_range",
    "render_hosted_report",
    "review_access",
    "review_registry",
    "review_state_snapshot",
    "video_response",
    "write_report_json_atomic",
]
