"""Hosted job worker: drives the screenscribe stage modules for uploaded videos.

The worker deliberately does NOT call ``review_pipeline.run_review`` (an
interactive CLI orchestrator with prompts, checkpoints and ``typer.Exit``).
It runs the same stage modules directly, in an executor thread, with a
server-built :class:`~screenscribe.config.ScreenScribeConfig` whose key is the
server-owned ``settings.libraxis_api_key`` (ZERO BYOK).

Progress is persisted to SQLite (``jobs.stage`` / ``progress_pct``) so the web
process can stream it over SSE without sharing memory with the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from screenscribe.api_utils import (
    endpoint_host,
    is_chat_completions_endpoint,
    redact_error_message,
)
from screenscribe.config import ScreenScribeConfig
from screenscribe.server_common import sanitized_error

from .config import CloudSettings
from .db import CloudStore, JobRecord, ReviewRecord
from .security import REVIEW_PREFIX, isoformat_z, new_id, new_share_id, safe_filename, utc_now

if TYPE_CHECKING:
    from screenscribe.detect import Detection
    from screenscribe.unified_analysis import UnifiedFinding

logger = logging.getLogger(__name__)

STAGE_PROGRESS: dict[str, int] = {
    "queued": 0,
    "audio": 10,
    "transcribe": 25,
    "detect": 50,
    "screenshots": 62,
    "analyze": 75,
    "report": 92,
    "done": 100,
}
REFUNDABLE_STAGES = frozenset({"queued", "audio", "transcribe"})
SOURCE_VIDEO_DIRNAME = "source"
HEARTBEAT_SECONDS = 60.0
STALE_JOB_AFTER = timedelta(minutes=10)
RECOVERY_SECONDS = 300.0
UI_LANGUAGES = ("en", "pl")


class PipelineFailure(Exception):
    """User-facing, already-sanitized failure of one pipeline stage."""

    def __init__(self, stage: str, message: str, *, refundable: bool = False) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.refundable = refundable


@dataclass(frozen=True)
class JobContext:
    job: JobRecord
    video_path: Path
    output_dir: Path
    language: str
    settings: CloudSettings
    progress: Callable[[str], None]


@dataclass(frozen=True)
class PipelineResult:
    report_json: Path
    report_html: Path | None
    report_md: Path | None
    language: str


PipelineRunner = Callable[[JobContext], PipelineResult]


# -- Config + small helpers --------------------------------------------------


def build_pipeline_config(
    settings: CloudSettings, *, language: str = "en", prompt: str | None = None
) -> ScreenScribeConfig:
    """Server-owned AI config. Built directly, never via ``ScreenScribeConfig.load()``.

    ``load()`` would read the operator's home config / env cascade; the hosted
    service must only ever use the capacity configured in ``CloudSettings``.
    """
    overrides: dict[str, Any] = {}
    for name in (
        "api_base",
        "stt_endpoint",
        "llm_endpoint",
        "vision_endpoint",
        "llm_model",
        "vision_model",
    ):
        value = getattr(settings, name)
        if value:
            overrides[name] = value
    config = ScreenScribeConfig(
        api_key=settings.libraxis_api_key,
        language=language,
        use_vision_analysis=True,
        **overrides,
    )
    if prompt and prompt.strip():
        config.analysis_prompt_override = prompt.strip()
    return config


def llm_processing_receipt(config: ScreenScribeConfig) -> dict[str, str] | None:
    """Safe identity of the semantic LLM host that processed the transcript.

    Mirrors the CLI receipt: host + protocol + provider only (no URL, model,
    credential or response id). The hosted agent's egress policy keeps the
    analysis host as ``trust=processor`` only when this receipt is present.
    """
    host = endpoint_host(config.llm_endpoint).strip().lower()
    if not host or host == "unknown host":
        return None
    return {
        "host": host,
        "protocol": (
            "chat_completions" if is_chat_completions_endpoint(config.llm_endpoint) else "responses"
        ),
        "provider": config.recognized_provider(),
    }


def resolve_job_language(requested: str, preferred: str | None) -> str:
    """``auto`` resolves to the account language (the STT path always sends a code)."""
    if requested in UI_LANGUAGES:
        return requested
    return preferred if preferred in UI_LANGUAGES else "en"


def review_visibility(job_visibility: str) -> str:
    """Map a job visibility onto a hosted review visibility.

    ``team`` has no team model yet and therefore degrades to owner-only.
    """
    return "unlisted" if job_visibility == "unlisted" else "private"


def _extract_job_audio(video: Path, audio_path: Path) -> Path:
    """Extract speech-ready MP3, mixing multiple audio tracks when present.

    Native macOS ScreenCaptureKit recordings carry separate system-audio and
    microphone AAC tracks; bare ``ffmpeg -vn`` picks only ``0:a:0``. When two or
    more audio streams exist, mix them down with ``amix`` before transcoding.
    """
    from screenscribe.audio import (
        _FFMPEG_TIMEOUT_SECONDS,
        _FFPROBE_TIMEOUT_SECONDS,
        _raise_ffmpeg_error,
        _run_media_command,
        extract_audio,
    )

    probe_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video),
    ]
    probe = _run_media_command(probe_cmd, timeout=_FFPROBE_TIMEOUT_SECONDS, tool="ffprobe")
    stream_lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    if probe.returncode == 0 and len(stream_lines) >= 2:
        count = len(stream_lines)
        inputs = "".join(f"[0:a:{idx}]" for idx in range(count))
        mix_filter = (
            f"{inputs}amix=inputs={count}:duration=longest:dropout_transition=0:normalize=0[aout]"
        )
        mix_cmd = [
            "ffmpeg",
            "-i",
            str(video),
            "-filter_complex",
            mix_filter,
            "-map",
            "[aout]",
            "-vn",
            "-acodec",
            "libmp3lame",
            "-q:a",
            "2",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-y",
            str(audio_path),
        ]
        mixed = _run_media_command(mix_cmd, timeout=_FFMPEG_TIMEOUT_SECONDS, tool="ffmpeg")
        if mixed.returncode != 0:
            _raise_ffmpeg_error(video, mixed.stderr)
        return audio_path
    return extract_audio(video, audio_path)


# -- Default pipeline runner (stage modules, no CLI orchestrator) -----------


def run_pipeline(ctx: JobContext) -> PipelineResult:
    """Run audio → STT → semantic detection → screenshots → VLM → reports."""
    from screenscribe.keywords import KeywordsConfig
    from screenscribe.presets import PresetError, load_preset
    from screenscribe.report import (
        save_enhanced_json_report,
        save_enhanced_markdown_report,
        save_html_report_pro,
    )
    from screenscribe.screenshots import extract_screenshots_for_detections
    from screenscribe.semantic_filter import (
        POI_CATEGORIES,
        deduplicate_pois,
        pois_to_detections,
        semantic_prefilter,
    )
    from screenscribe.summary_fallback import generate_detection_executive_summary
    from screenscribe.transcribe import (
        filter_hallucinated_segments,
        transcribe_audio_chunked,
        validate_audio_quality,
    )
    from screenscribe.transcript_sources import response_id_can_chain
    from screenscribe.unified_analysis import (
        analyze_all_findings_unified,
        deduplicate_findings,
        generate_unified_summary,
        generate_visual_summary_unified,
        llm_merge_findings,
    )

    job = ctx.job
    video = ctx.video_path
    output_dir = ctx.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem

    try:
        preset = load_preset(job.preset)
    except PresetError as exc:
        raise PipelineFailure("queued", "Unknown analysis preset.", refundable=True) from exc
    prompt = "\n\n".join(
        part for part in (preset.prompt, (job.prompt_override or "").strip()) if part
    )
    config = build_pipeline_config(ctx.settings, language=ctx.language, prompt=prompt or None)
    keywords = KeywordsConfig.load(None, preset=preset)
    config.keywords = keywords
    preset_categories: tuple[str, ...] | None = tuple(preset.categories) or None
    active_categories = preset_categories or POI_CATEGORIES
    preset_meta: dict[str, Any] | None = None
    if preset.name != "programming":
        preset_meta = {"name": preset.name, "categories": list(preset.categories)}
    errors: list[dict[str, str]] = []
    provenance: dict[str, Any] = {}

    # 1. Audio — scratch file lives next to the upload and is removed with it.
    ctx.progress("audio")
    audio_path = video.parent / f"{stem}.audio.mp3"
    try:
        _extract_job_audio(video, audio_path)
    except (RuntimeError, OSError, ValueError) as exc:
        logger.info("Job %s audio extraction failed: %s", job.job_id, redact_error_message(exc))
        raise PipelineFailure(
            "audio", "The video has no usable audio track.", refundable=True
        ) from exc

    # 2. Transcription (server STT capacity).
    ctx.progress("transcribe")
    try:
        transcription = transcribe_audio_chunked(
            audio_path,
            language=ctx.language,
            use_local=False,
            api_key=config.get_stt_api_key(),
            stt_endpoint=config.stt_endpoint,
            stt_model=config.stt_model,
            output_dir=output_dir,
        )
    except Exception as exc:
        logger.warning("Job %s transcription failed: %s", job.job_id, redact_error_message(exc))
        raise PipelineFailure(
            "transcribe",
            "Transcription failed. Your minutes were returned; please try again.",
            refundable=True,
        ) from exc
    finally:
        audio_path.unlink(missing_ok=True)

    transcription = filter_hallucinated_segments(transcription, job.duration_seconds or None)
    is_valid, quality_message, is_warning = validate_audio_quality(transcription)
    if quality_message and is_valid and is_warning:
        errors.append({"stage": "transcription", "message": quality_message})
    elif not is_valid:
        raise PipelineFailure(
            "transcribe", quality_message or "The recording has no intelligible speech."
        )

    # 3. Detection — always the LLM semantic prefilter.
    ctx.progress("detect")
    chain_id = (
        transcription.response_id
        if response_id_can_chain(config.llm_endpoint, config.llm_endpoint)
        else ""
    )
    filter_result = semantic_prefilter(
        transcription,
        config,
        previous_response_id=chain_id or "",
        keywords=keywords,
        categories=preset_categories,
    )
    if filter_result.failed:
        logger.warning(
            "Job %s semantic prefilter failed: %s",
            job.job_id,
            filter_result.error or "unknown error",
        )
        raise PipelineFailure(
            "detect", "Issue detection is temporarily unavailable. Please try again later."
        )
    receipt = llm_processing_receipt(config)
    if receipt is not None:
        provenance["llm"] = receipt
    pois = list(filter_result.pois)
    if len(pois) > 1:
        pois = deduplicate_pois(pois)
    detections: list[Detection] = (
        list(pois_to_detections(pois, transcription, categories=active_categories)) if pois else []
    )

    screenshots: list[tuple[Detection, Path]] = []
    unified: list[UnifiedFinding] = []
    executive_summary = ""
    visual_summary = ""
    language = transcription.language or ctx.language

    if detections:
        # 4. Screenshots.
        ctx.progress("screenshots")
        screenshots = extract_screenshots_for_detections(
            video, detections, output_dir / "screenshots"
        )
        if not screenshots:
            errors.append(
                {
                    "stage": "screenshots",
                    "message": (
                        f"Screenshots could not be extracted for {len(detections)} detected "
                        "finding(s); the report is transcript/detection-only."
                    ),
                }
            )
        # 5. Unified VLM analysis.
        ctx.progress("analyze")
        if screenshots:
            try:
                unified = analyze_all_findings_unified(
                    screenshots, config, previous_response_id="", output_dir=output_dir
                )
            except Exception as exc:
                errors.append({"stage": "unified_analysis", "message": redact_error_message(exc)})
                unified = []
            requested = {(d.segment.id, d.segment.start) for d, _path in screenshots}
            covered: set[tuple[int, float]] = set()
            for finding in unified:
                covered.add((finding.detection_id, finding.timestamp))
                covered.update((orig_id, orig_ts) for orig_id, orig_ts in finding.merged_from_ids)
            missing = requested - covered
            if missing:
                errors.append(
                    {
                        "stage": "unified_analysis",
                        "message": (
                            f"{len(missing)} of {len(requested)} unified analyses failed. The "
                            "report fell back to transcript/screenshot-only content for those "
                            "findings."
                        ),
                    }
                )
            if unified:
                unified = deduplicate_findings(unified)
                unified = llm_merge_findings(unified, config)
                keep: set[tuple[int, float]] = set()
                for finding in unified:
                    keep.add((finding.detection_id, finding.timestamp))
                    keep.update((orig_id, orig_ts) for orig_id, orig_ts in finding.merged_from_ids)
                if not missing and keep and len(keep) < len(screenshots):
                    screenshots = [
                        (d, p) for d, p in screenshots if (d.segment.id, d.segment.start) in keep
                    ]
                    detections = [d for d, _path in screenshots]
        try:
            if unified:
                executive_summary = generate_unified_summary(unified, config)
                visual_summary = generate_visual_summary_unified(unified, language=language)
            else:
                executive_summary = generate_detection_executive_summary(detections, config)
        except Exception as exc:
            errors.append({"stage": "summary_generation", "message": redact_error_message(exc)})
    else:
        executive_summary = (
            "No issues detected. The transcript contained "
            f"{len(transcription.segments)} segment(s) but the semantic pre-filter "
            "identified no points of interest."
        )

    # 6. Reports (JSON is the review source of truth; HTML is the hosted UI).
    ctx.progress("report")
    json_path = output_dir / f"{stem}_report.json"
    md_path = output_dir / f"{stem}_report.md"
    html_path = output_dir / f"{stem}_report.html"
    save_enhanced_json_report(
        detections,
        screenshots,
        video,
        json_path,
        unified_findings=unified,
        executive_summary=executive_summary,
        errors=errors,
        transcript=transcription.text,
        transcript_segments=transcription.segments,
        preset_meta=preset_meta,
        processing_provenance=provenance or None,
        transcript_source="audio",
    )
    markdown_context: dict[str, Any] = {"transcript_source": "audio"}
    if preset_meta is not None:
        markdown_context["preset_meta"] = preset_meta
    save_enhanced_markdown_report(
        detections,
        screenshots,
        video,
        md_path,
        unified_findings=unified,
        executive_summary=executive_summary,
        visual_summary=visual_summary,
        errors=errors,
        transcript=transcription.text,
        transcript_segments=transcription.segments,
        **markdown_context,
    )
    # ``save_html_report_pro`` symlinks (or copies) an EXISTING video next to
    # the report. The hosted page streams the source via /r/{share}/video, so
    # hand it a path that does not exist: only the basename is embedded.
    unlinked_video = ctx.output_dir / ".unlinked-video" / video.name
    save_html_report_pro(
        detections,
        screenshots,
        unlinked_video,
        html_path,
        segments=transcription.segments,
        unified_findings=unified,
        executive_summary=executive_summary,
        errors=errors,
        embed_video=False,
        language=language,
        transcript_source="audio",
    )
    return PipelineResult(
        report_json=json_path,
        report_html=html_path if html_path.exists() else None,
        report_md=md_path if md_path.exists() else None,
        language=language if language in UI_LANGUAGES else ctx.language,
    )


# -- Worker ------------------------------------------------------------------


class JobWorker:
    """Claims queued jobs from SQLite and runs them on a bounded thread pool."""

    def __init__(
        self,
        settings: CloudSettings,
        store: CloudStore,
        *,
        runner: PipelineRunner | None = None,
        clock: Callable[[], datetime] = utc_now,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.runner: PipelineRunner = runner or run_pipeline
        self.clock = clock
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.concurrency = max(1, settings.worker_concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="screenscribe-job"
        )
        self._active: dict[str, tuple[str, int]] = {}
        self._active_lock = threading.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    # -- bookkeeping ---------------------------------------------------------

    def _mark_active(self, job_id: str, stage: str, pct: int) -> None:
        with self._active_lock:
            self._active[job_id] = (stage, pct)

    def _clear_active(self, job_id: str) -> None:
        with self._active_lock:
            self._active.pop(job_id, None)

    def active_jobs(self) -> dict[str, tuple[str, int]]:
        with self._active_lock:
            return dict(self._active)

    def heartbeat(self) -> None:
        """Touch ``updated_at`` of running jobs so recovery never reaps them."""
        now = self.clock()
        for job_id, (stage, pct) in self.active_jobs().items():
            self.store.update_job_stage(job_id, stage, pct, now)

    def recover_interrupted(self) -> list[str]:
        """Fail + refund jobs whose worker died (no heartbeat for a while)."""
        now = self.clock()
        job_ids = self.store.fail_stale_jobs(now - STALE_JOB_AFTER, now)
        for job_id in job_ids:
            self.store.refund_job(job_id, now)
            job = self.store.get_job(job_id)
            if job is not None:
                self._remove_upload(job)
        if job_ids:
            logger.warning("Recovered %d interrupted job(s).", len(job_ids))
        return job_ids

    # -- file handling ---------------------------------------------------------

    def _upload_dir(self, job: JobRecord) -> Path | None:
        parent = Path(job.video_path).parent
        try:
            uploads_root = self.settings.uploads_dir.resolve()
            resolved = parent.resolve()
        except OSError:
            return None
        if resolved.name != job.job_id or resolved.parent != uploads_root:
            return None
        return resolved

    def _remove_upload(self, job: JobRecord) -> None:
        directory = self._upload_dir(job)
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    def _publish_job_review(
        self, job: JobRecord, share_id: str, review_dir: Path, result: PipelineResult
    ) -> ReviewRecord:
        video_name = safe_filename(Path(job.video_filename).name, "video.mp4")
        source_dir = review_dir / SOURCE_VIDEO_DIRNAME
        source_dir.mkdir(parents=True, exist_ok=True)
        target = source_dir / video_name
        shutil.move(str(job.video_path), str(target))

        def rel(path: Path | None) -> str | None:
            if path is None:
                return None
            return path.resolve().relative_to(review_dir).as_posix()

        stamp = isoformat_z(self.clock())
        record = ReviewRecord(
            review_id=new_id(REVIEW_PREFIX),
            share_id=share_id,
            user_id=job.user_id,
            source_kind="cloud_job",
            job_id=job.job_id,
            visibility=review_visibility(job.visibility),
            title=Path(video_name).stem,
            video_name=video_name,
            video_sha256=None,
            duration_seconds=job.duration_seconds,
            language=result.language,
            storage_dir=str(review_dir),
            report_json_rel=rel(result.report_json) or result.report_json.name,
            report_html_rel=rel(result.report_html),
            report_md_rel=rel(result.report_md),
            video_rel=f"{SOURCE_VIDEO_DIRNAME}/{video_name}",
            has_video=True,
            idempotency_key=None,
            created_at=stamp,
            updated_at=stamp,
        )
        created, _was_new = self.store.create_review(record)
        return created

    # -- one job (executor thread) -------------------------------------------

    def process_job(self, job: JobRecord) -> None:
        settings = self.settings
        store = self.store
        share_id = new_share_id()
        review_dir = (settings.reviews_dir / share_id).resolve()
        reached = {"stage": "queued"}

        def progress(stage: str) -> None:
            pct = STAGE_PROGRESS.get(stage, 0)
            reached["stage"] = stage
            self._mark_active(job.job_id, stage, pct)
            store.update_job_stage(job.job_id, stage, pct, self.clock())

        self._mark_active(job.job_id, "audio", 5)
        try:
            if not settings.has_ai_capacity:
                raise PipelineFailure(
                    "queued",
                    "Processing capacity is not configured on this server.",
                    refundable=True,
                )
            video_path = Path(job.video_path)
            if not video_path.is_file():
                raise PipelineFailure(
                    "queued", "The uploaded video is no longer available.", refundable=True
                )
            user = store.get_user(job.user_id)
            language = resolve_job_language(job.language, user.preferred_lang if user else None)
            ctx = JobContext(
                job=job,
                video_path=video_path,
                output_dir=review_dir,
                language=language,
                settings=settings,
                progress=progress,
            )
            result = self.runner(ctx)
            record = self._publish_job_review(job, share_id, review_dir, result)
            store.complete_job(
                job.job_id, share_id=record.share_id, review_id=record.review_id, now=self.clock()
            )
            logger.info("Job %s completed as %s.", job.job_id, record.share_id)
        except PipelineFailure as failure:
            self._fail(job, failure.message, refund=failure.refundable, review_dir=review_dir)
        except Exception as exc:
            detail = sanitized_error(
                exc,
                "job_failed",
                log_message=f"Job {job.job_id} failed at stage {reached['stage']}",
            )
            self._fail(
                job,
                detail["message"],
                refund=reached["stage"] in REFUNDABLE_STAGES,
                review_dir=review_dir,
            )
        finally:
            self._clear_active(job.job_id)
            self._remove_upload(job)

    def _fail(self, job: JobRecord, message: str, *, refund: bool, review_dir: Path) -> None:
        now = self.clock()
        self.store.fail_job(job.job_id, message, now)
        if refund:
            self.store.refund_job(job.job_id, now)
        if self.store.get_review_by_share(review_dir.name) is None:
            shutil.rmtree(review_dir, ignore_errors=True)
        logger.info("Job %s failed (refund=%s).", job.job_id, refund)

    # -- async loop ----------------------------------------------------------

    def _launch(self, job: JobRecord) -> None:
        loop = asyncio.get_running_loop()

        async def runner() -> None:
            await loop.run_in_executor(self._executor, self.process_job, job)

        task = loop.create_task(runner(), name=f"job-{job.job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self, stop: asyncio.Event) -> None:
        """Poll for work until ``stop`` is set; drains running jobs on exit."""
        await asyncio.to_thread(self.recover_interrupted)
        last_heartbeat = last_recovery = time.monotonic()
        try:
            while not stop.is_set():
                claimed = False
                if len(self._tasks) < self.concurrency:
                    try:
                        job = await asyncio.to_thread(
                            self.store.claim_next_job, self.worker_id, self.clock()
                        )
                    except Exception as exc:
                        logger.error("Job claim failed: %s", redact_error_message(exc))
                        job = None
                    if job is not None:
                        claimed = True
                        self._launch(job)
                current = time.monotonic()
                if current - last_heartbeat >= HEARTBEAT_SECONDS:
                    last_heartbeat = current
                    await asyncio.to_thread(self.heartbeat)
                if current - last_recovery >= RECOVERY_SECONDS:
                    last_recovery = current
                    await asyncio.to_thread(self.recover_interrupted)
                if not claimed:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop.wait(), timeout=max(0.2, self.settings.worker_poll_seconds)
                        )
        finally:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._executor.shutdown(wait=False, cancel_futures=True)


async def serve_worker(settings: CloudSettings, *, stop: asyncio.Event | None = None) -> None:
    """Standalone worker process (``python -m screenscribe.cloud worker``)."""
    store = CloudStore(settings.database_file)
    store.init_schema(utc_now())
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.reviews_dir.mkdir(parents=True, exist_ok=True)
    stop_event = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, stop_event.set)
    if not settings.has_ai_capacity:
        logger.warning("No server AI key configured; queued jobs will fail and be refunded.")
    worker = JobWorker(settings, store)
    logger.info("Worker %s started (concurrency=%d).", worker.worker_id, worker.concurrency)
    try:
        await worker.run(stop_event)
    finally:
        store.close()


__all__ = [
    "REFUNDABLE_STAGES",
    "SOURCE_VIDEO_DIRNAME",
    "STAGE_PROGRESS",
    "JobContext",
    "JobWorker",
    "PipelineFailure",
    "PipelineResult",
    "PipelineRunner",
    "build_pipeline_config",
    "llm_processing_receipt",
    "resolve_job_language",
    "review_visibility",
    "run_pipeline",
    "serve_worker",
]
