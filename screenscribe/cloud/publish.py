"""Review publishing (``/api/v1/reviews``): local review bundle → hosted share.

``POST /api/v1/reviews/publish`` takes a streamed multipart upload:

- ``manifest`` — ``publish_manifest.v1`` JSON (text field or small file part);
- ``bundle`` — the local review output directory as tar / tar.gz / zip;
- ``video`` — optional source recording, streamed to disk.

The bundle is extracted defensively (``security.safe_extract_archive``),
``<stem>_report.json`` is located and validated, ``<stem>_report.html`` is
regenerated with ``save_html_report_pro(..., embed_video=False)`` when the
bundle lacks it, a bundled ``review_state.json`` is folded into the report's
``human_review`` (report.json stays canonical), and the share becomes
available under ``/r/{share_id}``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .auth import CloudServices, CurrentAuth, get_services
from .billing import require_active_subscription
from .db import ReviewRecord
from .jobs import MULTIPART_OVERHEAD_BYTES, StreamedForm, idempotency_key, stream_multipart
from .review_host import (
    build_agent_manifest,
    build_review_state_from_report,
    load_report_json,
    lookup_review,
    open_hosted_review,
    review_registry,
    video_response,
    write_report_json_atomic,
)
from .security import (
    REVIEW_PREFIX,
    SHARE_ID_RE,
    ArchiveRejected,
    isoformat_z,
    new_id,
    new_share_id,
    safe_extract_archive,
    safe_filename,
)
from .worker import SOURCE_VIDEO_DIRNAME, review_visibility

logger = logging.getLogger(__name__)

PUBLISH_MANIFEST_SCHEMA = "publish_manifest.v1"
MAX_MANIFEST_BYTES = 64 * 1024
REVIEW_STATE_FILENAME = "review_state.json"
STAGING_DIRNAME = ".publish-staging"
HASH_CHUNK_BYTES = 1024 * 1024
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv"})


# -- Manifest ----------------------------------------------------------------


class PublishManifest(BaseModel):
    """``publish_manifest.v1`` — what the client claims about the bundle."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_name: str | None = Field(default=None, alias="schema", max_length=64)
    video_name: str = Field(..., min_length=1, max_length=255)
    video_sha256: str = Field(..., min_length=64, max_length=64)
    video_size_bytes: int = Field(..., ge=0)
    duration_seconds: float = Field(..., ge=0)
    language: str = Field(..., min_length=1, max_length=16)
    generated_at: str = Field(..., min_length=1, max_length=64)
    screenscribe_version: str = Field(..., min_length=1, max_length=64)
    visibility: str = Field(default="private", max_length=16)
    title: str | None = Field(default=None, max_length=200)

    @field_validator("schema_name")
    @classmethod
    def _schema(cls, value: str | None) -> str | None:
        if value is not None and value != PUBLISH_MANIFEST_SCHEMA:
            raise ValueError(f"manifest schema must be {PUBLISH_MANIFEST_SCHEMA}")
        return value

    @field_validator("video_sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        lowered = value.strip().lower()
        if len(lowered) != 64 or any(ch not in "0123456789abcdef" for ch in lowered):
            raise ValueError("video_sha256 must be 64 hex characters")
        return lowered

    @field_validator("duration_seconds")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("duration_seconds must be finite")
        return value

    @field_validator("visibility")
    @classmethod
    def _visibility(cls, value: str) -> str:
        lowered = value.strip().lower() or "private"
        if lowered not in {"private", "unlisted", "team"}:
            raise ValueError("visibility must be private, unlisted, or team")
        return lowered

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def safe_video_name(self) -> str:
        return safe_filename(Path(self.video_name).name, "video.mp4")

    @property
    def stem(self) -> str:
        return Path(self.safe_video_name).stem


def parse_manifest(form: StreamedForm) -> PublishManifest:
    raw: str | None = form.fields.get("manifest")
    if raw is None and "manifest" in form.files:
        part = form.files["manifest"]
        try:
            raw = part.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Manifest must be UTF-8 JSON.") from exc
    if raw is None or not raw.strip():
        raise HTTPException(status_code=400, detail="Missing publish manifest.")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Manifest is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Manifest must be a JSON object.")
    try:
        return PublishManifest.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
            for err in exc.errors()[:5]
        )
        raise HTTPException(status_code=422, detail=f"Invalid publish manifest: {problems}") from exc


# -- Bundle inspection -------------------------------------------------------


def locate_report_json(files: tuple[Path, ...], stem: str) -> Path:
    """``<stem>_report.json``; falls back to the single ``*_report.json`` member."""
    expected = f"{stem}_report.json"
    exact = [path for path in files if path.name == expected]
    if len(exact) == 1:
        return exact[0]
    candidates = [path for path in files if path.name.endswith("_report.json")]
    if len(exact) > 1 or len(candidates) != 1:
        raise HTTPException(
            status_code=400,
            detail=f"Bundle must contain exactly one {expected} report.",
        )
    return candidates[0]


def read_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Bundle report JSON is not valid JSON.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise HTTPException(status_code=422, detail="Bundle report JSON has no findings list.")
    return data


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else default
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _strs(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _merged_pairs(value: Any) -> list[tuple[int, float]]:
    pairs: list[tuple[int, float]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, list | tuple) and len(item) == 2:
            pairs.append((_int(item[0]), _num(item[1])))
    return pairs


def regenerate_html_report(
    report: dict[str, Any],
    report_root: Path,
    html_path: Path,
    *,
    video_name: str,
    language: str,
) -> Path:
    """Rebuild ``<stem>_report.html`` from report.json via ``save_html_report_pro``.

    The JSON carries the same serialized ``unified_analysis`` the pipeline
    wrote, so findings, merged-evidence frames and transcript segments are
    reconstructed into the writer's native types instead of re-implementing
    the renderer.
    """
    from screenscribe.detect import Detection
    from screenscribe.report import save_html_report_pro
    from screenscribe.transcribe import Segment
    from screenscribe.unified.finding import UnifiedFinding

    screenshots_dir = report_root / "screenshots"
    detections: list[Detection] = []
    screenshots: list[tuple[Detection, Path]] = []
    unified: list[UnifiedFinding] = []

    def frame_path(raw_name: Any, fallback: str) -> Path:
        name = Path(str(raw_name)).name if isinstance(raw_name, str) and raw_name else ""
        return screenshots_dir / (name or fallback)

    for index, raw in enumerate(report.get("findings") or []):
        if not isinstance(raw, dict):
            continue
        start = _num(raw.get("timestamp_start", raw.get("timestamp")))
        segment = Segment(
            id=_int(raw.get("id"), index),
            start=start,
            end=_num(raw.get("timestamp_end"), start),
            text=str(raw.get("text") or ""),
        )
        category = str(raw.get("category") or "other")
        detection = Detection(
            segment=segment,
            category=category,
            keywords_found=_strs(raw.get("keywords")),
            context=str(raw.get("context") or ""),
        )
        detections.append(detection)
        # Rows are driven by screenshots: keep a (possibly missing) path so a
        # finding without a bundled frame still renders as a row.
        shot = frame_path(raw.get("screenshot"), f".missing-finding-{index}.jpg")
        screenshots.append((detection, shot))
        analysis = raw.get("unified_analysis")
        if isinstance(analysis, dict) and analysis.get("status") == "completed":
            unified.append(
                UnifiedFinding(
                    detection_id=segment.id,
                    screenshot_path=shot,
                    timestamp=segment.start,
                    category=category,
                    is_issue=bool(analysis.get("is_issue", True)),
                    sentiment=str(analysis.get("sentiment") or "neutral"),
                    severity=str(analysis.get("severity") or "medium"),
                    summary=str(analysis.get("summary") or ""),
                    action_items=_strs(analysis.get("action_items")),
                    affected_components=_strs(analysis.get("affected_components")),
                    suggested_fix=str(analysis.get("suggested_fix") or ""),
                    ui_elements=_strs(analysis.get("ui_elements")),
                    issues_detected=_strs(analysis.get("issues_detected")),
                    accessibility_notes=_strs(analysis.get("accessibility_notes")),
                    design_feedback=str(analysis.get("design_feedback") or ""),
                    technical_observations=str(analysis.get("technical_observations") or ""),
                    response_id=str(analysis.get("response_id") or ""),
                    merged_from_ids=_merged_pairs(analysis.get("merged_from_ids")),
                    confidence=str(analysis.get("confidence") or "high"),
                    parsed_from_unstructured_output=bool(
                        analysis.get("parsed_from_unstructured_output", False)
                    ),
                )
            )
        for member_index, member in enumerate(raw.get("merged_frames") or []):
            if not isinstance(member, dict) or not member.get("screenshot"):
                continue
            member_start = _num(member.get("timestamp_start", member.get("timestamp")))
            member_segment = Segment(
                id=_int(member.get("id")),
                start=member_start,
                end=member_start,
                text=str(member.get("text") or ""),
            )
            member_detection = Detection(
                segment=member_segment, category=category, keywords_found=[], context=""
            )
            screenshots.append(
                (
                    member_detection,
                    frame_path(member.get("screenshot"), f".missing-member-{index}-{member_index}.jpg"),
                )
            )

    segments = [
        Segment(
            id=_int(item.get("id"), position),
            start=_num(item.get("start")),
            end=_num(item.get("end")),
            text=str(item.get("text") or ""),
        )
        for position, item in enumerate(report.get("transcript_segments") or [])
        if isinstance(item, dict)
    ]
    errors = [
        {str(key): str(value) for key, value in item.items()}
        for item in report.get("errors") or []
        if isinstance(item, dict)
    ]
    transcript_source = report.get("transcript_source")
    # A path that does not exist: the writer embeds only the basename and
    # never symlinks/copies the video next to the report.
    unlinked_video = report_root / ".unlinked-video" / video_name
    return save_html_report_pro(
        detections,
        screenshots,
        unlinked_video,
        html_path,
        segments=segments or None,
        unified_findings=unified or None,
        executive_summary=str(report.get("executive_summary") or ""),
        errors=errors or None,
        embed_video=False,
        language=language if language in {"en", "pl"} else "en",
        transcript_source=transcript_source if isinstance(transcript_source, str) else None,
    )


def _human_from_state(state: dict[str, Any]) -> dict[str, Any]:
    """Local review-state snapshot (``/api/review-state`` shape) → ``human_review``."""
    findings_raw = state.get("findings")
    findings: dict[str, Any] = {}
    rejected: list[str] = []
    for fid, review in (findings_raw.items() if isinstance(findings_raw, dict) else []):
        if not isinstance(review, dict):
            continue
        human = {
            "verdict": review.get("verdict", "none"),
            "severity": review.get("severity"),
            "notes": review.get("notes", ""),
            "action_items": review.get("actionItems") or review.get("action_items") or "",
            "annotations": list(review.get("annotations") or []),
        }
        for key in (
            "merged_from_ids",
            "member_annotations",
            "merged_member_reviews",
            "merged_survivor_review",
            "merged_review_baseline",
            "summary_override",
            "category_override",
        ):
            if review.get(key):
                human[key] = review[key]
        findings[str(fid)] = human
        if str(review.get("verdict", "")).lower() == "rejected":
            rejected.append(str(fid))
    frames_raw = state.get("manualFrames")
    frames = [
        {k: v for k, v in frame.items() if k not in {"frameDataUrl", "imageMissing"}}
        for frame in (frames_raw if isinstance(frames_raw, list) else [])
        if isinstance(frame, dict)
    ]
    return {
        "reviewer": str(state.get("reviewer") or ""),
        "reviewed_at": str(state.get("reviewed_at") or ""),
        "findings": findings,
        "rejected_ids": rejected,
        "manual_frames": frames,
    }


def fold_bundled_review_state(report_root: Path, report: dict[str, Any]) -> bool:
    """Fold a bundled ``review_state.json`` into ``human_review`` (report wins)."""
    if isinstance(report.get("human_review"), dict):
        return False
    path = report_root / REVIEW_STATE_FILENAME
    if not path.is_file() or path.is_symlink():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.info("Ignoring unreadable bundled review_state.json")
        return False
    if not isinstance(state, dict):
        return False
    human = _human_from_state(state)
    if not human["findings"] and not human["manual_frames"]:
        return False
    report["human_review"] = human
    return True


def write_review_state_snapshot(record: ReviewRecord, report_root: Path) -> None:
    """Derived, image-free ``review_state.json`` next to the canonical report."""
    entry = open_hosted_review(record)
    state = build_review_state_from_report(entry, load_report_json(entry))
    for frame in state.get("manualFrames") or []:
        if isinstance(frame, dict):
            frame.pop("frameDataUrl", None)
    target = report_root / REVIEW_STATE_FILENAME
    write_report_json_atomic(target, state)


# -- Bodies ------------------------------------------------------------------


def publish_body(services: CloudServices, record: ReviewRecord) -> dict[str, Any]:
    """Exact ``201`` body of ``POST /api/v1/reviews/publish``."""
    settings = services.settings
    share = record.share_id
    return {
        "review_id": record.review_id,
        "share_id": share,
        "share_url": settings.url(f"/r/{share}"),
        "video_stream_url": settings.url(f"/r/{share}/video") if record.has_video else None,
        "agent_manifest_url": settings.url(f"/r/{share}/api/export/agent-manifest"),
        "visibility": record.visibility,
        "created_at": record.created_at,
    }


def review_summary(services: CloudServices, record: ReviewRecord) -> dict[str, Any]:
    share = record.share_id
    return {
        **publish_body(services, record),
        "title": record.title,
        "video_name": record.video_name,
        "source_kind": record.source_kind,
        "job_id": record.job_id,
        "duration_seconds": record.duration_seconds,
        "language": record.language,
        "has_video": record.has_video,
        "report_json_url": f"/api/v1/reviews/{share}/report.json",
        "updated_at": record.updated_at,
    }


def review_root(services: CloudServices, share_id: str) -> Path | None:
    """``reviews/<share_id>`` (top of the extracted tree), containment-checked."""
    base = services.settings.reviews_dir.resolve()
    candidate = (base / share_id).resolve()
    if candidate.parent != base:
        return None
    return candidate


# -- Publish (threadpool) ----------------------------------------------------


def materialize_publish(
    services: CloudServices,
    *,
    user_id: str,
    form: StreamedForm,
    manifest: PublishManifest,
    share_id: str,
    review_dir: Path,
    key: str | None,
) -> tuple[ReviewRecord, bool]:
    settings = services.settings
    bundle = form.files.get("bundle")
    if bundle is None or bundle.size == 0:
        raise HTTPException(status_code=400, detail="Missing review bundle.")
    try:
        extracted = safe_extract_archive(
            bundle.path,
            review_dir,
            max_members=settings.max_archive_members,
            max_total_bytes=settings.max_archive_total_bytes,
        )
    except ArchiveRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        bundle.path.unlink(missing_ok=True)

    report_json = locate_report_json(extracted.files, manifest.stem)
    report_root = report_json.parent
    report = read_report(report_json)
    report_stem = report_json.name[: -len(".json")]
    html_path = report_root / f"{report_stem}.html"
    md_path = report_root / f"{report_stem}.md"

    video_name = manifest.safe_video_name
    video_target: Path | None = None
    upload = form.files.get("video")
    if upload is not None and upload.size > 0:
        if upload.size != manifest.video_size_bytes:
            raise HTTPException(status_code=422, detail="Video size does not match the manifest.")
        if sha256_file(upload.path) != manifest.video_sha256:
            raise HTTPException(status_code=422, detail="Video checksum does not match the manifest.")
        source_dir = report_root / SOURCE_VIDEO_DIRNAME
        source_dir.mkdir(parents=True, exist_ok=True)
        video_target = source_dir / video_name
        upload.path.replace(video_target)
    else:
        bundled = report_root / video_name
        if (
            bundled.is_file()
            and not bundled.is_symlink()
            and bundled.suffix.lower() in _VIDEO_SUFFIXES
            and bundled.stat().st_size == manifest.video_size_bytes
        ):
            video_target = bundled

    if fold_bundled_review_state(report_root, report):
        write_report_json_atomic(report_json, report)

    if not html_path.is_file():
        try:
            regenerate_html_report(
                report, report_root, html_path, video_name=video_name, language=manifest.language
            )
        except Exception as exc:
            logger.warning("HTML regeneration failed for %s: %s", share_id, type(exc).__name__)
            raise HTTPException(
                status_code=422, detail="Bundle has no HTML report and it could not be rebuilt."
            ) from exc

    def rel(path: Path | None) -> str | None:
        if path is None or not path.is_file():
            return None
        return path.resolve().relative_to(report_root.resolve()).as_posix()

    stamp = isoformat_z(services.now())
    record = ReviewRecord(
        review_id=new_id(REVIEW_PREFIX),
        share_id=share_id,
        user_id=user_id,
        source_kind="published_bundle",
        job_id=None,
        visibility=review_visibility(manifest.visibility),
        title=(manifest.title or "").strip() or Path(video_name).stem,
        video_name=video_name,
        video_sha256=manifest.video_sha256,
        duration_seconds=manifest.duration_seconds,
        language=manifest.language if manifest.language in {"en", "pl"} else "en",
        storage_dir=str(report_root.resolve()),
        report_json_rel=rel(report_json) or report_json.name,
        report_html_rel=rel(html_path),
        report_md_rel=rel(md_path),
        video_rel=rel(video_target),
        has_video=video_target is not None,
        idempotency_key=key,
        created_at=stamp,
        updated_at=stamp,
    )
    stored, created = services.store.create_review(record)
    if created:
        try:
            write_review_state_snapshot(stored, report_root)
        except Exception as exc:  # derived artifact; report.json stays canonical
            logger.info("review_state.json snapshot skipped for %s: %s", share_id, type(exc).__name__)
    return stored, created


def remove_tree(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


# -- Router ------------------------------------------------------------------


def build_reviews_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/reviews", tags=["reviews"])

    @router.post("/publish", status_code=201)
    async def publish(request: Request, auth: CurrentAuth) -> JSONResponse:
        services = get_services(request)
        settings = services.settings
        user_id = auth.user.user_id
        key = idempotency_key(request)
        if key is not None:
            existing = await run_in_threadpool(services.store.get_review_by_idempotency, user_id, key)
            if existing is not None:
                return JSONResponse(status_code=201, content=publish_body(services, existing))
        await run_in_threadpool(require_active_subscription, services, user_id)

        share_id = new_share_id()
        review_dir = review_root(services, share_id)
        if review_dir is None:
            raise HTTPException(status_code=500, detail="Review storage is misconfigured.")
        staging = settings.reviews_dir / STAGING_DIRNAME / share_id
        keep_review = False
        try:
            form = await stream_multipart(
                request,
                staging,
                file_limits={
                    "bundle": settings.max_bundle_bytes,
                    "video": settings.max_upload_bytes,
                    "manifest": MAX_MANIFEST_BYTES,
                },
                max_body_bytes=settings.max_bundle_bytes
                + settings.max_upload_bytes
                + MULTIPART_OVERHEAD_BYTES,
            )
            manifest = parse_manifest(form)
            record, created = await run_in_threadpool(
                materialize_publish,
                services,
                user_id=user_id,
                form=form,
                manifest=manifest,
                share_id=share_id,
                review_dir=review_dir,
                key=key,
            )
            keep_review = created
            return JSONResponse(status_code=201, content=publish_body(services, record))
        finally:
            await run_in_threadpool(remove_tree, staging)
            if not keep_review:
                await run_in_threadpool(remove_tree, review_dir)

    @router.get("")
    async def list_reviews(request: Request, auth: CurrentAuth) -> dict[str, Any]:
        services = get_services(request)
        records = await run_in_threadpool(services.store.list_reviews, auth.user.user_id)
        return {"reviews": [review_summary(services, record) for record in records]}

    @router.get("/{share_id}/report.json")
    async def report_json(request: Request, share_id: str) -> JSONResponse:
        record, _auth = await lookup_review(request, share_id)
        entry = await run_in_threadpool(review_registry(request).get, record)
        report = await run_in_threadpool(load_report_json, entry)
        return JSONResponse(
            content=report,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.api_route("/{share_id}/video", methods=["GET", "HEAD"])
    async def review_video(request: Request, share_id: str) -> Response:
        record, _auth = await lookup_review(request, share_id)
        entry = await run_in_threadpool(review_registry(request).get, record)
        return await video_response(request, entry.video_path)

    @router.get("/{share_id}/agent-manifest")
    async def agent_manifest(request: Request, share_id: str) -> JSONResponse:
        services = get_services(request)
        record, _auth = await lookup_review(request, share_id)
        entry = await run_in_threadpool(review_registry(request).get, record)
        report = await run_in_threadpool(load_report_json, entry)
        return JSONResponse(
            content=build_agent_manifest(services, record, report),
            headers={"Cache-Control": "private, no-store"},
        )

    @router.delete("/{share_id}")
    async def delete_review(request: Request, share_id: str, auth: CurrentAuth) -> dict[str, Any]:
        services = get_services(request)
        if not SHARE_ID_RE.match(share_id):
            raise HTTPException(status_code=404, detail="Review not found.")
        record = await run_in_threadpool(services.store.get_review_by_share, share_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Review not found.")
        if record.user_id != auth.user.user_id:
            raise HTTPException(status_code=403, detail="Only the review owner can delete it.")
        deleted = await run_in_threadpool(services.store.delete_review, share_id, auth.user.user_id)
        if deleted is None:
            raise HTTPException(status_code=404, detail="Review not found.")
        review_registry(request).drop(share_id)
        await run_in_threadpool(remove_tree, review_root(services, share_id))
        return {"deleted": True, "share_id": share_id}

    return router


__all__ = [
    "MAX_MANIFEST_BYTES",
    "PUBLISH_MANIFEST_SCHEMA",
    "REVIEW_STATE_FILENAME",
    "PublishManifest",
    "build_reviews_router",
    "fold_bundled_review_state",
    "locate_report_json",
    "materialize_publish",
    "parse_manifest",
    "publish_body",
    "read_report",
    "regenerate_html_report",
    "remove_tree",
    "review_root",
    "review_summary",
    "sha256_file",
    "write_review_state_snapshot",
]
