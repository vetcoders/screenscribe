"""Hosted processing jobs: streaming upload, atomic quota reservation, status, SSE.

Upload bodies are parsed straight off ``request.stream()`` with
``python_multipart`` (no Starlette ``UploadFile`` spooling), so an oversize
upload is aborted with ``413`` the moment the running total crosses the cap.
Gate order for ``POST /api/v1/jobs``: 413 (size) → 422 (ffprobe) → 402
(subscription / quota, reserved atomically under ``BEGIN IMMEDIATE``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser, parse_options_header

from .auth import CloudServices, CurrentAuth, get_services
from .billing import SubscriptionRequired
from .db import JobRecord, NewJob
from .schemas import JOB_LANGUAGES, JOB_PRESETS, JOB_VISIBILITIES
from .security import JOB_ID_RE, JOB_PREFIX, new_id, safe_filename

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

logger = logging.getLogger(__name__)

CHUNK_BYTES = 64 * 1024
MULTIPART_OVERHEAD_BYTES = 1024 * 1024
MAX_TEXT_FIELD_BYTES = 64 * 1024
MAX_PROMPT_CHARS = 4000
MAX_IDEMPOTENCY_KEY_CHARS = 200
SSE_MAX_SECONDS = 2 * 3600.0
VIDEO_FIELDS = ("file", "video")


# -- Streaming multipart → disk ---------------------------------------------


@dataclass(frozen=True)
class StreamedFile:
    field_name: str
    filename: str
    path: Path
    size: int
    content_type: str


@dataclass
class StreamedForm:
    fields: dict[str, str] = field(default_factory=dict)
    files: dict[str, StreamedFile] = field(default_factory=dict)


@dataclass
class _Part:
    headers: dict[str, str] = field(default_factory=dict)
    header_name: bytearray = field(default_factory=bytearray)
    header_value: bytearray = field(default_factory=bytearray)
    name: str = ""
    filename: str | None = None
    content_type: str = ""
    handle: IO[bytes] | None = None
    path: Path | None = None
    size: int = 0
    text: bytearray = field(default_factory=bytearray)
    skip: bool = False


def _too_large(detail: str = "Upload exceeds the size limit.") -> HTTPException:
    return HTTPException(status_code=413, detail=detail)


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Content-Length.") from exc
    if value < 0:
        raise HTTPException(status_code=400, detail="Invalid Content-Length.")
    return value


async def stream_multipart(
    request: Request,
    dest_dir: Path,
    *,
    file_limits: Mapping[str, int],
    max_body_bytes: int,
    max_text_bytes: int = MAX_TEXT_FIELD_BYTES,
) -> StreamedForm:
    """Parse ``multipart/form-data`` incrementally, streaming file parts to disk.

    ``file_limits`` maps accepted file field names to their byte caps; other
    file parts are discarded (still counted against ``max_body_bytes``). Text
    fields are buffered up to ``max_text_bytes`` each. Any limit breach raises
    ``413`` immediately; the caller owns cleanup of ``dest_dir``.
    """
    mime, params = parse_options_header(request.headers.get("content-type"))
    if mime != b"multipart/form-data":
        raise HTTPException(status_code=415, detail="Use multipart/form-data.")
    boundary = params.get(b"boundary")
    if not boundary:
        raise HTTPException(status_code=400, detail="Multipart boundary missing.")
    declared = _declared_length(request)
    if declared is not None and declared > max_body_bytes:
        raise _too_large()

    dest_dir.mkdir(parents=True, exist_ok=True)
    events: list[tuple[str, bytes]] = []
    callbacks: MultipartCallbacks = {
        "on_part_begin": lambda: events.append(("begin", b"")),
        "on_header_field": lambda data, start, end: events.append(("hfield", data[start:end])),
        "on_header_value": lambda data, start, end: events.append(("hvalue", data[start:end])),
        "on_header_end": lambda: events.append(("hend", b"")),
        "on_headers_finished": lambda: events.append(("hdone", b"")),
        "on_part_data": lambda data, start, end: events.append(("data", data[start:end])),
        "on_part_end": lambda: events.append(("end", b"")),
    }
    parser = MultipartParser(boundary, callbacks)
    form = StreamedForm()
    current: _Part | None = None
    received = 0

    def open_file_part(part: _Part) -> None:
        if part.name in form.files:
            raise HTTPException(status_code=400, detail=f"Duplicate file field '{part.name}'.")
        part.path = dest_dir / f".{part.name}.part"
        part.handle = part.path.open("wb")

    def finish(part: _Part) -> None:
        if part.handle is not None and part.path is not None:
            part.handle.close()
            part.handle = None
            form.files[part.name] = StreamedFile(
                field_name=part.name,
                filename=part.filename or "",
                path=part.path,
                size=part.size,
                content_type=part.content_type,
            )
        elif not part.skip and part.name and part.filename is None:
            form.fields[part.name] = part.text.decode("utf-8", errors="replace")

    def drain() -> None:
        nonlocal current
        for kind, payload in events:
            if kind == "begin":
                current = _Part()
            elif current is None:
                continue
            elif kind == "hfield":
                current.header_name.extend(payload)
            elif kind == "hvalue":
                current.header_value.extend(payload)
            elif kind == "hend":
                key = current.header_name.decode("latin-1").strip().lower()
                current.headers[key] = current.header_value.decode(
                    "utf-8", errors="replace"
                ).strip()
                current.header_name.clear()
                current.header_value.clear()
            elif kind == "hdone":
                _disposition, options = parse_options_header(
                    current.headers.get("content-disposition", "")
                )
                current.name = options.get(b"name", b"").decode("utf-8", errors="replace")
                raw_filename = options.get(b"filename")
                current.filename = (
                    raw_filename.decode("utf-8", errors="replace")
                    if raw_filename is not None
                    else None
                )
                current.content_type = current.headers.get("content-type", "")
                if current.filename is not None and current.name in file_limits:
                    open_file_part(current)
                elif current.filename is not None or not current.name:
                    current.skip = True
            elif kind == "data":
                current.size += len(payload)
                if current.handle is not None:
                    if current.size > file_limits[current.name]:
                        raise _too_large()
                    current.handle.write(payload)
                elif not current.skip:
                    if current.size > max_text_bytes:
                        raise _too_large("Form field too large.")
                    current.text.extend(payload)
            elif kind == "end":
                finish(current)
                current = None
        events.clear()

    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > max_body_bytes:
                raise _too_large()
            for offset in range(0, len(chunk), CHUNK_BYTES):
                try:
                    parser.write(chunk[offset : offset + CHUNK_BYTES])
                except FormParserError as exc:
                    raise HTTPException(
                        status_code=400, detail="Malformed multipart body."
                    ) from exc
                drain()
        try:
            parser.finalize()
        except FormParserError as exc:
            raise HTTPException(status_code=400, detail="Malformed multipart body.") from exc
        drain()
    finally:
        if current is not None and current.handle is not None:
            current.handle.close()
    return form


def finalize_upload(streamed: StreamedFile, directory: Path, default_name: str) -> Path:
    """Move a streamed part to its sanitized final name inside ``directory``."""
    name = safe_filename(Path(streamed.filename or default_name).name, default_name)
    target = directory / name
    streamed.path.replace(target)
    return target


def idempotency_key(request: Request) -> str | None:
    raw = request.headers.get("idempotency-key")
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_IDEMPOTENCY_KEY_CHARS or not value.isprintable():
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key header.")
    return value


# -- Response bodies ---------------------------------------------------------


def job_urls(job_id: str) -> dict[str, str]:
    return {
        "status_url": f"/api/v1/jobs/{job_id}",
        "events_url": f"/api/v1/jobs/{job_id}/events",
    }


def accepted_body(job: JobRecord) -> dict[str, Any]:
    """Exact ``202`` body of ``POST /api/v1/jobs``."""
    return {
        "job_id": job.job_id,
        "status": job.status,
        "stage": job.stage,
        "created_at": job.created_at,
        **job_urls(job.job_id),
    }


def job_status_body(services: CloudServices, job: JobRecord) -> dict[str, Any]:
    share_id = job.share_id
    settings = services.settings
    return {
        "job_id": job.job_id,
        "status": job.status,
        "stage": job.stage,
        "progress_pct": job.progress_pct,
        "review_id": job.review_id,
        "share_id": share_id,
        "share_url": settings.url(f"/r/{share_id}") if share_id else None,
        "video_stream_url": settings.url(f"/r/{share_id}/video") if share_id else None,
        "agent_manifest_url": (
            settings.url(f"/r/{share_id}/api/export/agent-manifest") if share_id else None
        ),
        "report_json_url": settings.url(f"/api/v1/reviews/{share_id}/report.json")
        if share_id
        else None,
        "error": job.error_message,
    }


def format_sse(event: str, payload: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# -- Upload helpers ----------------------------------------------------------


def _choice(form: StreamedForm, name: str, allowed: tuple[str, ...], default: str) -> str:
    value = form.fields.get(name, "").strip().lower() or default
    if value not in allowed:
        raise HTTPException(
            status_code=422, detail=f"Invalid {name}; expected one of: {', '.join(allowed)}."
        )
    return value


def _probe_duration(services: CloudServices, path: Path) -> float:
    probe: Callable[[Path], float]
    if services.duration_probe is not None:
        probe = services.duration_probe
    else:
        from screenscribe.audio import get_video_duration

        probe = get_video_duration
    try:
        duration = float(probe(path))
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="The uploaded file is not a readable video."
        ) from exc
    if not math.isfinite(duration) or duration <= 0:
        raise HTTPException(status_code=422, detail="The uploaded file is not a readable video.")
    return duration


def required_minutes(services: CloudServices, duration_seconds: float) -> float:
    return max(services.settings.min_job_minutes, round(duration_seconds / 60.0, 2))


# -- Router ------------------------------------------------------------------


def build_jobs_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

    @router.post("", status_code=202)
    async def create_job(request: Request, auth: CurrentAuth) -> JSONResponse:
        services = get_services(request)
        settings = services.settings
        store = services.store
        user_id = auth.user.user_id
        key = idempotency_key(request)
        if key:
            existing = await run_in_threadpool(store.get_job_by_idempotency, user_id, key)
            if existing is not None:
                return JSONResponse(status_code=202, content=accepted_body(existing))

        job_id = new_id(JOB_PREFIX)
        upload_dir = settings.uploads_dir / job_id
        keep_upload = False
        try:
            form = await stream_multipart(
                request,
                upload_dir,
                file_limits=dict.fromkeys(VIDEO_FIELDS, settings.max_upload_bytes),
                max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
            )
            streamed = next((form.files[name] for name in VIDEO_FIELDS if name in form.files), None)
            if streamed is None:
                raise HTTPException(
                    status_code=400, detail="Attach the recording in the 'file' (or 'video') field."
                )
            if streamed.size == 0:
                raise HTTPException(status_code=422, detail="The uploaded file is empty.")
            language = _choice(form, "language", JOB_LANGUAGES, "auto")
            preset = _choice(form, "preset", JOB_PRESETS, "programming")
            visibility = _choice(form, "visibility", JOB_VISIBILITIES, "private")
            prompt = form.fields.get("prompt", "").strip()
            if len(prompt) > MAX_PROMPT_CHARS:
                raise HTTPException(
                    status_code=422, detail=f"Prompt is limited to {MAX_PROMPT_CHARS} characters."
                )
            video_path = finalize_upload(streamed, upload_dir, "recording.mp4")
            duration = await run_in_threadpool(_probe_duration, services, video_path)
            new_job = NewJob(
                job_id=job_id,
                user_id=user_id,
                video_filename=video_path.name,
                video_path=str(video_path.resolve()),
                duration_seconds=duration,
                language=language,
                preset=preset,
                prompt_override=prompt or None,
                visibility=visibility,
                idempotency_key=key,
            )
            outcome = await run_in_threadpool(
                store.reserve_job, new_job, required_minutes(services, duration), services.now()
            )
            if outcome.status == "duplicate" and outcome.job is not None:
                return JSONResponse(status_code=202, content=accepted_body(outcome.job))
            if outcome.status == "no_active_subscription" or outcome.status == "quota_exceeded":
                raise SubscriptionRequired(outcome.status)
            if outcome.status != "ok" or outcome.job is None:
                raise HTTPException(status_code=409, detail="The job could not be queued.")
            keep_upload = True
            return JSONResponse(status_code=202, content=accepted_body(outcome.job))
        finally:
            if not keep_upload:
                await run_in_threadpool(shutil.rmtree, upload_dir, True)

    @router.get("")
    async def list_jobs(request: Request, auth: CurrentAuth) -> dict[str, Any]:
        services = get_services(request)
        jobs = await run_in_threadpool(services.store.list_jobs, auth.user.user_id)
        return {"jobs": [job_status_body(services, job) for job in jobs]}

    async def _owned_job(request: Request, auth: CurrentAuth, job_id: str) -> JobRecord:
        if not JOB_ID_RE.match(job_id):
            raise HTTPException(status_code=404, detail="Job not found.")
        services = get_services(request)
        job = await run_in_threadpool(services.store.get_job_for_user, job_id, auth.user.user_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job

    @router.get("/{job_id}")
    async def get_job(request: Request, auth: CurrentAuth, job_id: str) -> dict[str, Any]:
        job = await _owned_job(request, auth, job_id)
        return job_status_body(get_services(request), job)

    @router.get("/{job_id}/events")
    async def job_events(request: Request, auth: CurrentAuth, job_id: str) -> StreamingResponse:
        await _owned_job(request, auth, job_id)
        services = get_services(request)
        user_id = auth.user.user_id
        poll = max(0.2, services.settings.sse_poll_seconds)
        keepalive = max(1.0, services.settings.sse_keepalive_seconds)

        async def frames() -> AsyncIterator[str]:
            started = last_frame = time.monotonic()
            last_snapshot: tuple[str, str, int] | None = None
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    return
                job = await run_in_threadpool(services.store.get_job_for_user, job_id, user_id)
                if job is None:
                    return
                snapshot = (job.status, job.stage, job.progress_pct)
                now = time.monotonic()
                if snapshot != last_snapshot:
                    last_snapshot = snapshot
                    last_frame = now
                    body = job_status_body(services, job)
                    if job.status == "completed":
                        yield format_sse("job_completed", body)
                        return
                    if job.status == "failed":
                        yield format_sse("job_failed", body)
                        return
                    yield format_sse("job_progress", body)
                elif now - last_frame >= keepalive:
                    last_frame = now
                    yield ": keepalive\n\n"
                if now - started >= SSE_MAX_SECONDS:
                    return
                await asyncio.sleep(poll)

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


__all__ = [
    "CHUNK_BYTES",
    "MULTIPART_OVERHEAD_BYTES",
    "StreamedFile",
    "StreamedForm",
    "accepted_body",
    "build_jobs_router",
    "finalize_upload",
    "format_sse",
    "idempotency_key",
    "job_status_body",
    "required_minutes",
    "stream_multipart",
]
