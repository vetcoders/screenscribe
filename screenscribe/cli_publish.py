"""``screenscribe publish``: hand a review to Screenscribe Cloud, get a share URL.

Routes of :func:`run_publish_command`:

- **review directory** -> sanitized bundle (``publish_bundle``) ->
  ``POST /api/v1/reviews/publish`` -> ``publish_receipt.json`` in the directory.
- **recording + ``--cloud-run``** -> ``POST /api/v1/jobs`` -> live SSE progress
  from ``events_url`` (bounded ``status_url`` polling when the stream drops).
  No local ffmpeg, STT or API key is involved.
- **recording** -> the local ``screenscribe review`` pipeline (no local
  server) -> publish the review directory it produced.

``screenscribe review --publish`` wraps the local pipeline with
:func:`prepare_review_publish` (validation + sign-in check before any paid
work) and :meth:`ReviewPublishPlan.finish` (publish every new review).

Every Cloud request goes to the signed-in host only: job URLs returned by the
server are joined onto that origin and refused when they point anywhere else,
and redirects are never followed. A 401 triggers one token refresh and one
replay; 408/429/5xx and transport errors retry with backoff (uploads carry an
``Idempotency-Key``, so a replay never duplicates a share). Tokens are never
printed. With ``--json`` stdout carries exactly one JSON object and every
human-readable line goes to stderr.

This module never imports ``cli``, ``cli_serve`` or ``review_pipeline``: the
CLI injects the review-directory resolver and the local review runner.
"""

from __future__ import annotations

import contextlib
import glob
import json
import sys
import time
import webbrowser
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from .account_auth import AccountAuthError
from .account_auth import cloud as cloud_auth
from .account_auth.cloud import (
    CLOUD_LOGIN_HINT,
    CloudAuthError,
    CloudAuthRequiredError,
    CloudSession,
    SubscriptionRequired,
    clean_server_text,
    parse_subscription_required,
    safe_browser_url,
)
from .api_utils import (
    RETRIABLE_STATUS_CODES,
    RETRY_AFTER_MAX_SECONDS,
    UnsafeEndpointError,
    endpoint_host,
    retry_after_seconds,
    validate_provider_endpoint,
)
from .publish_bundle import (
    RECEIPT_FILENAME,
    RECEIPT_SCHEMA_VERSION,
    REPORT_JSON_SUFFIX,
    FormField,
    MultipartBody,
    PublishBundle,
    PublishBundleError,
    ReviewResolver,
    VideoFile,
    build_publish_bundle,
    job_idempotency_key,
    resolve_review_source,
    validate_visibility,
    write_publish_receipt,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PUBLISH_PATH = "/api/v1/reviews/publish"
JOBS_PATH = "/api/v1/jobs"
#: Multipart field of the recording on ``POST /api/v1/jobs`` (wire contract 3.2).
JOB_VIDEO_FIELD = "video"

#: Uploads: generous write window per chunk, never unbounded.
UPLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=300.0, pool=30.0)
STATUS_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
#: SSE: a silent stream for this long counts as dropped -> status polling.
SSE_TIMEOUT = httpx.Timeout(connect=30.0, read=90.0, write=30.0, pool=30.0)

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 3.0
MAX_POLL_INTERVAL_SECONDS = 15.0
CLOUD_RUN_MAX_WAIT_SECONDS = 2 * 60 * 60
#: File-system mtime granularity slack when spotting freshly written reports.
MTIME_SLACK_SECONDS = 2.0

AUTH_REQUIRED = "auth_required"
SUBSCRIPTION_REQUIRED = "subscription_required"
_SUBSCRIPTION_CODES = frozenset({SUBSCRIPTION_REQUIRED, "no_active_subscription", "quota_exceeded"})


# --------------------------------------------------------------------------- #
# Failures + output
# --------------------------------------------------------------------------- #


class PublishFailure(Exception):
    """A publish step failed. ``payload()`` is the ``--json`` error object."""

    def __init__(
        self,
        error: str,
        message: str,
        *,
        exit_code: int = 1,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.exit_code = exit_code
        self.extra = dict(extra or {})

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.error, "message": self.message}
        if self.error == AUTH_REQUIRED:
            body["hint"] = CLOUD_LOGIN_HINT
        body.update(self.extra)
        return body


class SubscriptionFailure(PublishFailure):
    """HTTP 402 (or an async job failure) ``subscription_required``."""

    def __init__(self, details: SubscriptionRequired) -> None:
        super().__init__(SUBSCRIPTION_REQUIRED, details.message)
        self.details = details

    def payload(self) -> dict[str, Any]:
        return self.details.to_json()


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


class PublishOutput:
    """Human output on stdout, or ``--json``: one object on stdout, notes on stderr."""

    def __init__(self, *, json_mode: bool) -> None:
        self.json_mode = json_mode
        self.console = Console(stderr=True) if json_mode else Console()

    def note(self, text: str) -> None:
        self.console.print(f"[dim]{escape(text)}[/]")

    def progress(self, text: str) -> None:
        if not self.json_mode:
            self.console.print(f"  [cyan]{escape(text)}[/]")

    def emit(self, payload: Mapping[str, Any]) -> None:
        sys.stdout.write(json.dumps(dict(payload), sort_keys=True, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def published(self, receipt: Mapping[str, Any], *, receipt_path: Path | None = None) -> None:
        if self.json_mode:
            self.emit(receipt)
            return
        lines = [
            f"[bold]Share URL:[/] {escape(str(receipt.get('share_url') or 'not returned'))}",
            f"[dim]Visibility:[/] {escape(str(receipt.get('visibility') or 'unknown'))}",
        ]
        for label, key in (("Video", "video_stream_url"), ("Agent manifest", "agent_manifest_url")):
            if receipt.get(key):
                lines.append(f"[dim]{label}:[/] {escape(str(receipt[key]))}")
        if receipt_path is not None:
            lines.append(f"[dim]Receipt:[/] {escape(str(receipt_path))}")
        self.console.print(
            Panel(
                "\n".join(lines),
                title="[bold green]Published to Screenscribe Cloud[/]",
                border_style="green",
            )
        )

    def failed(self, failure: PublishFailure, session: CloudSession | None = None) -> None:
        if self.json_mode:
            self.emit(failure.payload())
            return
        if isinstance(failure, SubscriptionFailure):
            lines = [escape(failure.details.message)]
            quota = session.subscription.quota_line() if session and session.subscription else None
            if quota:
                lines.append(f"[dim]{escape(quota)}[/]")
            if failure.details.upgrade_url:
                url = escape(failure.details.upgrade_url)
                lines.append(f"Upgrade: [link={url}]{url}[/link]")
            self.console.print(
                Panel(
                    "\n".join(lines),
                    title="[bold yellow]Subscription required[/]",
                    border_style="yellow",
                )
            )
            return
        if failure.error == AUTH_REQUIRED:
            self.console.print(
                Panel(
                    f"{escape(failure.message)}\n[bold]{escape(CLOUD_LOGIN_HINT)}[/]",
                    title="[bold red]Screenscribe Cloud sign-in required[/]",
                    border_style="red",
                )
            )
            return
        self.console.print(f"[red]Error:[/] {escape(failure.message)}")


@contextlib.contextmanager
def _stdout_to_stderr(enabled: bool) -> Iterator[None]:
    """Keep stdout clean for ``--json`` while the local pipeline prints progress."""
    if not enabled:
        yield
        return
    with contextlib.redirect_stdout(sys.stderr):
        yield


# --------------------------------------------------------------------------- #
# Session + transport
# --------------------------------------------------------------------------- #


def checked_visibility(visibility: str) -> str:
    try:
        return validate_visibility(visibility)
    except PublishBundleError as error:
        raise PublishFailure("invalid_usage", str(error), exit_code=2) from None


def preflight_cloud_session(cloud_url: str | None = None) -> CloudSession:
    """The signed-in session for the target host, before any heavy work."""
    try:
        return cloud_auth.resolve_cloud_bearer(cloud_url)
    except CloudAuthRequiredError as error:
        raise PublishFailure(AUTH_REQUIRED, str(error)) from None
    except CloudAuthError as error:
        if error.kind == "config":
            raise PublishFailure("invalid_cloud_url", str(error), exit_code=2) from None
        raise PublishFailure(
            "auth_unavailable", f"Could not check the Screenscribe Cloud session: {error}"
        ) from None
    except AccountAuthError as error:
        raise PublishFailure("auth_unavailable", str(error)) from None


class _Bearer:
    """The session's bearer with a single forced refresh after a 401."""

    def __init__(self, session: CloudSession) -> None:
        self.session = session
        self.refreshed = False

    def headers(self) -> dict[str, str]:
        return self.session.authorization()

    def refresh_after_401(self) -> bool:
        if self.refreshed:
            return False
        self.refreshed = True
        try:
            self.session = cloud_auth.force_cloud_refresh(
                self.session.tokens.access_token, self.session.cloud_url
            )
        except CloudAuthRequiredError as error:
            raise PublishFailure(AUTH_REQUIRED, str(error)) from None
        except AccountAuthError as error:
            raise PublishFailure(
                AUTH_REQUIRED,
                f"Screenscribe Cloud rejected the session and renewal failed: {error}",
            ) from None
        return True


@contextlib.contextmanager
def _upload_client(cloud_url: str, client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    try:
        own = cloud_auth._build_http_client(cloud_url, timeout=UPLOAD_TIMEOUT)
    except CloudAuthError as error:
        raise PublishFailure("invalid_cloud_url", str(error), exit_code=2) from None
    try:
        yield own
    finally:
        own.close()


def _backoff(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(RETRY_AFTER_MAX_SECONDS, retry_after)
    return float(BACKOFF_BASE_SECONDS * (2**attempt))


def _send(
    http: httpx.Client,
    bearer: _Bearer,
    method: str,
    url: str,
    *,
    what: str,
    timeout: httpx.Timeout,
    sleep: Callable[[float], None],
    body: Callable[[], MultipartBody] | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """One logical Cloud request: backoff retries, one refresh + replay on 401."""
    try:
        validate_provider_endpoint(url)
    except UnsafeEndpointError as error:
        raise PublishFailure("invalid_cloud_url", str(error), exit_code=2) from None
    attempt = 0
    while True:
        request_headers = {"Accept": "application/json", **(headers or {}), **bearer.headers()}
        content: Iterator[bytes] | None = None
        if body is not None:
            multipart = body()
            request_headers.update(multipart.headers())
            content = multipart.iter_chunks()
        try:
            response = http.request(
                method,
                url,
                content=content,
                headers=request_headers,
                timeout=timeout,
                follow_redirects=False,
            )
        except PublishBundleError as error:
            raise PublishFailure("upload_failed", str(error)) from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
            if attempt >= MAX_RETRIES:
                raise PublishFailure(
                    "network_error",
                    f"{what} failed after {attempt + 1} attempts ({error.__class__.__name__}) "
                    f"talking to {endpoint_host(url)}.",
                ) from None
            sleep(_backoff(attempt, None))
            attempt += 1
            continue
        status = response.status_code
        if status == 401 and bearer.refresh_after_401():
            response.close()
            continue
        if status in RETRIABLE_STATUS_CODES and attempt < MAX_RETRIES:
            delay = _backoff(
                attempt,
                retry_after_seconds(
                    httpx.HTTPStatusError("retry", request=response.request, response=response)
                ),
            )
            response.close()
            sleep(delay)
            attempt += 1
            continue
        return response


def _error_detail(response: httpx.Response) -> str:
    """Sanitized server explanation of an error response (never the raw body)."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        for key in ("message", "detail", "error_description", "error"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("detail") or value.get("code")
            if isinstance(value, list):  # FastAPI validation errors
                parts = [
                    item.get("msg") for item in value if isinstance(item, dict) and item.get("msg")
                ]
                value = "; ".join(str(part) for part in parts[:3])
            text = clean_server_text(value)
            if text:
                return f"{text} (HTTP {response.status_code})"
    return f"HTTP {response.status_code}"


def _json_or_none(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _check_response(response: httpx.Response, what: str) -> None:
    status = response.status_code
    if status < 300:
        return
    if 300 <= status < 400:
        raise PublishFailure(
            "redirect_refused",
            f"{what} answered with a redirect (HTTP {status}); refusing to follow it. "
            "Check --cloud-url / SCREENSCRIBE_CLOUD_URL.",
        )
    if status == 401:
        raise PublishFailure(AUTH_REQUIRED, "Screenscribe Cloud rejected the session.")
    if status == 402:
        raise SubscriptionFailure(parse_subscription_required(_json_or_none(response)))
    if status == 413:
        raise PublishFailure(
            "payload_too_large", f"{what} was refused as too large: {_error_detail(response)}"
        )
    raise PublishFailure("http_error", f"{what} failed: {_error_detail(response)}")


def _response_object(response: httpx.Response, what: str) -> dict[str, Any]:
    body = _json_or_none(response)
    if not isinstance(body, dict):
        raise PublishFailure(
            "unexpected_response",
            f"{what} returned an unexpected body (HTTP {response.status_code}).",
        )
    return body


def _text(body: Mapping[str, Any], key: str, limit: int = 200) -> str | None:
    return clean_server_text(body.get(key), limit)


def same_origin_url(cloud_url: str, candidate: object, fallback_path: str) -> str:
    """Join a server-returned URL onto ``cloud_url``; refuse any other origin.

    The bearer goes with every job request, so a ``status_url`` or
    ``events_url`` pointing at a different scheme, host, port or carrying
    credentials is rejected instead of followed.
    """
    raw = candidate.strip() if isinstance(candidate, str) and candidate.strip() else fallback_path
    joined = urljoin(cloud_url + "/", raw)
    base = urlsplit(cloud_url)
    target = urlsplit(joined)
    try:
        same = (
            target.scheme.lower() == base.scheme.lower()
            and (target.hostname or "") == (base.hostname or "")
            and target.port == base.port
            and "@" not in target.netloc
        )
    except ValueError:
        same = False
    if not same:
        raise PublishFailure(
            "unsafe_url",
            f"Screenscribe Cloud returned a job URL outside {endpoint_host(cloud_url)}; "
            "refusing it.",
        )
    try:
        validate_provider_endpoint(joined)
    except UnsafeEndpointError:
        raise PublishFailure(
            "unsafe_url", "Screenscribe Cloud returned an unusable job URL."
        ) from None
    return joined


def _absolute_url(cloud_url: str, value: object) -> str | None:
    """Server-provided link for display/opening: relative paths join the Cloud host."""
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        value = urljoin(cloud_url + "/", value)
    return safe_browser_url(value)


# --------------------------------------------------------------------------- #
# Receipt
# --------------------------------------------------------------------------- #


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_receipt(
    *,
    mode: str,
    session: CloudSession,
    result: Mapping[str, Any],
    visibility: str,
    bundle: PublishBundle | None = None,
    video: VideoFile | None = None,
    idempotency_key: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """``publish_receipt.v1``: what was published where. No tokens, no local paths."""
    media = bundle.video if bundle is not None else video
    manifest = bundle.manifest if bundle is not None else {}
    cloud_url = session.cloud_url
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "mode": mode,
        "cloud_url": cloud_url,
        "review_id": _text(result, "review_id"),
        "share_id": _text(result, "share_id"),
        "share_url": _absolute_url(cloud_url, result.get("share_url")),
        "video_stream_url": _absolute_url(cloud_url, result.get("video_stream_url")),
        "agent_manifest_url": _absolute_url(cloud_url, result.get("agent_manifest_url")),
        "report_json_url": _absolute_url(cloud_url, result.get("report_json_url")),
        "visibility": _text(result, "visibility", 20) or visibility,
        "created_at": _text(result, "created_at", 64),
        "published_at": _utc_now(),
        "idempotency_key": idempotency_key or (bundle.idempotency_key if bundle else None),
        "bundle_sha256": bundle.sha256 if bundle is not None else None,
        "video_filename": media.filename if media else manifest.get("video_filename"),
        "video_included": media is not None,
        "video_sha256": media.sha256 if media else None,
        "job_id": job_id,
    }


# --------------------------------------------------------------------------- #
# Publish a review directory
# --------------------------------------------------------------------------- #


def publish_existing_review(
    review_dir: Path,
    *,
    session: CloudSession,
    visibility: str,
    output: PublishOutput,
    video: Path | None = None,
    include_video: bool = True,
    resolver: ReviewResolver | None = None,
    resolver_errors: tuple[type[Exception], ...] = (),
    sleep: Callable[[float], None] = time.sleep,
    client: httpx.Client | None = None,
) -> tuple[dict[str, Any], Path | None]:
    """Pack + upload one review directory. Returns ``(receipt, receipt_path)``."""
    visibility = checked_visibility(visibility)
    try:
        source = resolve_review_source(
            review_dir,
            video=video,
            include_video=include_video,
            resolver=resolver,
            resolver_errors=resolver_errors,
        )
        bundle = build_publish_bundle(source, visibility=visibility)
    except PublishBundleError as error:
        raise PublishFailure("invalid_review", str(error)) from None
    with bundle:
        media = (
            f" + {bundle.video.filename} ({_human_size(bundle.video.size_bytes)})"
            if bundle.video is not None
            else " (without the recording)"
        )
        output.note(
            f"Packed {source.review_dir.name}: {len(bundle.artifacts)} files, "
            f"{_human_size(bundle.size_bytes)}{media}"
        )
        for item in bundle.skipped:
            output.note(f"Skipped {item}")
        output.note(f"Uploading to {session.host} ...")
        bearer = _Bearer(session)
        with _upload_client(session.cloud_url, client) as http:
            response = _send(
                http,
                bearer,
                "POST",
                f"{session.cloud_url}{PUBLISH_PATH}",
                what="Publish",
                timeout=UPLOAD_TIMEOUT,
                sleep=sleep,
                body=bundle.multipart,
                headers={"Idempotency-Key": bundle.idempotency_key},
            )
        _check_response(response, "Publish")
        result = _response_object(response, "Publish")
        receipt = build_receipt(
            mode="publish",
            session=bearer.session,
            result=result,
            visibility=visibility,
            bundle=bundle,
        )
    try:
        receipt_path: Path | None = write_publish_receipt(source.review_dir, receipt)
    except PublishBundleError as error:
        output.note(f"Published, but {RECEIPT_FILENAME} was not saved: {error}")
        receipt_path = None
    return receipt, receipt_path


# --------------------------------------------------------------------------- #
# --cloud-run
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SseEvent:
    event: str
    data: str


def iter_sse_events(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Minimal ``text/event-stream`` parser (``event:`` / ``data:``, comments skipped)."""
    event = ""
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if event or data:
                yield SseEvent(event or "message", "\n".join(data))
            event = ""
            data = []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if event or data:
        yield SseEvent(event or "message", "\n".join(data))


def _event_payload(event: SseEvent) -> dict[str, Any]:
    try:
        payload = json.loads(event.data) if event.data else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _progress_line(body: Mapping[str, Any]) -> str:
    stage = _text(body, "stage", 40) or _text(body, "status", 40) or "working"
    pct = body.get("progress_pct")
    if isinstance(pct, int | float) and not isinstance(pct, bool):
        return f"{stage} {max(0, min(100, int(pct)))}%"
    return stage


def _job_failure(value: object) -> PublishFailure:
    body: Mapping[str, Any] = value if isinstance(value, dict) else {}
    inner = body.get("error")
    if isinstance(inner, dict):
        body = inner
    codes = {item for item in (body.get("code"), body.get("error")) if isinstance(item, str)}
    if codes & _SUBSCRIPTION_CODES:
        return SubscriptionFailure(parse_subscription_required(dict(body)))
    message = (
        _text(body, "message", 300)
        or (clean_server_text(inner, 300) if isinstance(inner, str) else None)
        or (clean_server_text(value, 300) if isinstance(value, str) else None)
        or "no reason given"
    )
    raw_code = body.get("code")
    label = clean_server_text(raw_code, 64) if isinstance(raw_code, str) else None
    suffix = f" ({label})" if label else ""
    return PublishFailure("job_failed", f"Cloud job failed{suffix}: {message}")


def _handle_job_event(event: SseEvent, output: PublishOutput) -> dict[str, Any] | None:
    payload = _event_payload(event)
    name = event.event
    if name == "message":
        name = str(payload.get("type") or payload.get("event") or name)
    if name == "job_progress":
        output.progress(_progress_line(payload))
        return None
    if name == "job_completed":
        result = payload.get("result")
        return result if isinstance(result, dict) else payload
    if name == "job_failed":
        raise _job_failure(payload)
    return None


def _stream_job_events(
    http: httpx.Client, bearer: _Bearer, url: str, output: PublishOutput
) -> dict[str, Any] | None:
    """Follow the SSE stream; ``None`` when it ends without a terminal event."""
    for _attempt in range(2):
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache", **bearer.headers()}
        try:
            with http.stream(
                "GET", url, headers=headers, timeout=SSE_TIMEOUT, follow_redirects=False
            ) as response:
                if response.status_code == 401 and bearer.refresh_after_401():
                    continue
                if response.status_code == 402:
                    response.read()
                    raise SubscriptionFailure(parse_subscription_required(_json_or_none(response)))
                if response.status_code != 200:
                    return None
                for event in iter_sse_events(response.iter_lines()):
                    result = _handle_job_event(event, output)
                    if result is not None:
                        return result
                return None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            return None
    return None


def _poll_job(
    http: httpx.Client,
    bearer: _Bearer,
    url: str,
    output: PublishOutput,
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    deadline: float,
    job_id: str,
) -> dict[str, Any]:
    interval = POLL_INTERVAL_SECONDS
    last_line = ""
    while True:
        response = _send(
            http, bearer, "GET", url, what="Cloud job status", timeout=STATUS_TIMEOUT, sleep=sleep
        )
        _check_response(response, "Cloud job status")
        status = _response_object(response, "Cloud job status")
        state = _text(status, "status", 40) or "unknown"
        if state == "completed":
            result = status.get("result")
            return {**status, **result} if isinstance(result, dict) else status
        if state == "failed":
            raise _job_failure(status.get("error") or status)
        line = _progress_line(status)
        if line != last_line:
            output.progress(line)
            last_line = line
        if monotonic() + interval > deadline:
            raise PublishFailure(
                "timeout",
                f"Cloud job {job_id} is still {state}; stopped waiting. It keeps running "
                "in Screenscribe Cloud.",
                extra={"job_id": job_id, "status_url": url},
            )
        sleep(interval)
        interval = min(interval * 1.5, MAX_POLL_INTERVAL_SECONDS)


def publish_cloud_run(
    video: Path,
    *,
    session: CloudSession,
    visibility: str,
    output: PublishOutput,
    language: str | None = None,
    preset: str | None = None,
    prompt: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    client: httpx.Client | None = None,
    max_wait_seconds: float = CLOUD_RUN_MAX_WAIT_SECONDS,
) -> dict[str, Any]:
    """Upload a raw recording to ``POST /api/v1/jobs`` and follow it to the share URL."""
    visibility = checked_visibility(visibility)
    path = video.expanduser()
    if not path.is_file():
        raise PublishFailure("invalid_usage", f"Video not found: {path}", exit_code=2)
    output.note(f"Hashing {path.name} ...")
    try:
        media = VideoFile.from_path(path)
    except PublishBundleError as error:
        raise PublishFailure("invalid_video", str(error)) from None
    key = job_idempotency_key(
        video_sha256=media.sha256,
        visibility=visibility,
        video_filename=media.filename,
        language=language,
        preset=preset,
        prompt=prompt,
    )
    fields = [FormField("visibility", visibility), FormField("title", path.stem)]
    for name, value in (("language", language), ("preset", preset), ("prompt", prompt)):
        if value:
            fields.append(FormField(name, value))

    def body() -> MultipartBody:
        return MultipartBody(fields, [media.part(JOB_VIDEO_FIELD)])

    bearer = _Bearer(session)
    size = _human_size(media.size_bytes)
    output.note(f"Uploading {media.filename} ({size}) to {session.host} ...")
    deadline = monotonic() + max_wait_seconds
    with _upload_client(session.cloud_url, client) as http:
        response = _send(
            http,
            bearer,
            "POST",
            f"{session.cloud_url}{JOBS_PATH}",
            what="Cloud job upload",
            timeout=UPLOAD_TIMEOUT,
            sleep=sleep,
            body=body,
            headers={"Idempotency-Key": key},
        )
        _check_response(response, "Cloud job upload")
        job = _response_object(response, "Cloud job upload")
        job_id = _text(job, "job_id", 120)
        if not job_id:
            raise PublishFailure("unexpected_response", "Cloud job upload returned no job_id.")
        job_url = f"{session.cloud_url}{JOBS_PATH}/{quote(job_id, safe='')}"
        status_url = same_origin_url(session.cloud_url, job.get("status_url"), job_url)
        events_url = same_origin_url(session.cloud_url, job.get("events_url"), f"{job_url}/events")
        output.note(f"Cloud job {job_id} accepted; following progress ...")
        result = _stream_job_events(http, bearer, events_url, output)
        if result is None:
            output.note("Progress stream ended early; polling the job status.")
            result = _poll_job(
                http,
                bearer,
                status_url,
                output,
                sleep=sleep,
                monotonic=monotonic,
                deadline=deadline,
                job_id=job_id,
            )
    return build_receipt(
        mode="cloud_run",
        session=bearer.session,
        result=result,
        visibility=visibility,
        video=media,
        idempotency_key=key,
        job_id=job_id,
    )


# --------------------------------------------------------------------------- #
# Local review -> publish
# --------------------------------------------------------------------------- #


def _report_video_name(report: Path) -> str | None:
    try:
        body = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    value = body.get("video") if isinstance(body, dict) else None
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class ReviewOutputWatch:
    """Finds the review directories a local pipeline run just produced.

    ``run_review`` returns nothing, and reruns version their output
    (``<stem>_review_2``), so a directory counts only when it holds a
    ``*_report.json`` written after the watch started whose ``video`` field
    names this recording. Zero or several matches fail loudly.
    """

    videos: tuple[Path, ...]
    output: Path | None
    started_at: float

    @classmethod
    def start(cls, videos: Sequence[Path], output: Path | None) -> ReviewOutputWatch:
        return cls(
            videos=tuple(video.expanduser() for video in videos),
            output=output.expanduser() if output is not None else None,
            started_at=time.time() - MTIME_SLACK_SECONDS,
        )

    def _candidates(self, video: Path) -> list[Path]:
        pattern = f"{glob.escape(video.stem)}_review*"
        parent = video.parent if self.output is None else self.output
        found: list[Path] = [] if self.output is None else [self.output]
        with contextlib.suppress(OSError):
            found.extend(sorted(parent.glob(pattern)))
        return found

    def find(self, video: Path) -> Path:
        matches: dict[Path, Path] = {}
        for directory in self._candidates(video):
            if directory.is_symlink() or not directory.is_dir():
                continue
            for report in sorted(directory.glob(f"*{REPORT_JSON_SUFFIX}")):
                try:
                    fresh = report.stat().st_mtime >= self.started_at
                except OSError:
                    continue
                if fresh and _report_video_name(report) == video.name:
                    matches.setdefault(directory.resolve(), directory)
        if len(matches) == 1:
            return next(iter(matches.values()))
        if not matches:
            raise PublishFailure(
                "review_not_found",
                f"The review of {video.name} finished, but no new {video.stem}"
                f"{REPORT_JSON_SUFFIX} was found. Publish its review directory with: "
                "screenscribe publish <review_dir>",
            )
        names = ", ".join(sorted(path.name for path in matches.values()))
        raise PublishFailure(
            "review_ambiguous",
            f"Several fresh reviews of {video.name} were found ({names}). Publish one with: "
            "screenscribe publish <review_dir>",
        )


def _open_share(receipt: Mapping[str, Any], output: PublishOutput) -> None:
    url = safe_browser_url(receipt.get("share_url"))
    if url is None:
        output.note("No share URL to open.")
        return
    try:
        webbrowser.open(url)
    except (webbrowser.Error, OSError):
        output.note("Could not open a browser; use the share URL above.")


def run_publish_command(
    *,
    target: Path,
    video: Path | None,
    visibility: str,
    no_video: bool,
    cloud_run: bool,
    cloud_url: str | None,
    json_output: bool,
    open_browser: bool,
    language: str | None,
    preset: str | None,
    prompt: str | None,
    output_dir: Path | None,
    resolver: ReviewResolver,
    resolver_errors: tuple[type[Exception], ...],
    run_local_review: Callable[[], None],
) -> None:
    """Body of ``screenscribe publish`` (the Typer wrapper lives in ``cli.py``)."""
    out = PublishOutput(json_mode=json_output)
    session: CloudSession | None = None
    receipt_path: Path | None = None
    try:
        visibility = checked_visibility(visibility)
        path = target.expanduser()
        if not path.exists():
            raise PublishFailure("invalid_usage", f"Not found: {path}", exit_code=2)
        is_dir = path.is_dir()
        if is_dir and cloud_run:
            raise PublishFailure(
                "invalid_usage",
                "--cloud-run expects a video file, not a review directory.",
                exit_code=2,
            )
        if cloud_run and no_video:
            raise PublishFailure(
                "invalid_usage",
                "--no-video cannot be combined with --cloud-run "
                "(cloud pipeline requires uploading the video).",
                exit_code=2,
            )
        if not is_dir and video is not None:
            raise PublishFailure(
                "invalid_usage",
                "--video applies to a review directory; the target is already a recording.",
                exit_code=2,
            )
        session = preflight_cloud_session(cloud_url)
        if is_dir:
            receipt, receipt_path = publish_existing_review(
                path,
                session=session,
                visibility=visibility,
                output=out,
                video=video,
                include_video=not no_video,
                resolver=resolver,
                resolver_errors=resolver_errors,
                sleep=time.sleep,
            )
        elif cloud_run:
            receipt = publish_cloud_run(
                path,
                session=session,
                visibility=visibility,
                output=out,
                language=language,
                preset=preset,
                prompt=prompt,
                sleep=time.sleep,
                monotonic=time.monotonic,
            )
        else:
            watch = ReviewOutputWatch.start([path], output_dir)
            try:
                with _stdout_to_stderr(json_output):
                    run_local_review()
            except typer.Exit as exit_:
                if exit_.exit_code:
                    raise PublishFailure(
                        "review_failed",
                        "The local review failed; nothing was published.",
                        exit_code=exit_.exit_code,
                    ) from None
            receipt, receipt_path = publish_existing_review(
                watch.find(path),
                session=session,
                visibility=visibility,
                output=out,
                video=None if no_video else path,
                include_video=not no_video,
                resolver=resolver,
                resolver_errors=resolver_errors,
                sleep=time.sleep,
            )
    except PublishFailure as failure:
        out.failed(failure, session)
        raise typer.Exit(failure.exit_code) from None
    except KeyboardInterrupt:
        out.note("Interrupted.")
        raise typer.Exit(130) from None
    out.published(receipt, receipt_path=receipt_path)
    if open_browser:
        _open_share(receipt, out)


# --------------------------------------------------------------------------- #
# screenscribe review --publish
# --------------------------------------------------------------------------- #


@dataclass
class ReviewPublishPlan:
    """State carried from the pre-pipeline checks to the post-pipeline publish."""

    session: CloudSession
    visibility: str
    watch: ReviewOutputWatch
    serve_after: bool

    def finish(
        self,
        *,
        resolver: ReviewResolver,
        resolver_errors: tuple[type[Exception], ...],
    ) -> tuple[Path, Path] | None:
        """Publish every fresh review. Returns what to serve for an explicit ``--serve``."""
        out = PublishOutput(json_mode=False)
        failures = 0
        last: tuple[Path, Path] | None = None
        for video in self.watch.videos:
            try:
                review_dir = self.watch.find(video)
                receipt, receipt_path = publish_existing_review(
                    review_dir,
                    session=self.session,
                    visibility=self.visibility,
                    output=out,
                    video=video,
                    resolver=resolver,
                    resolver_errors=resolver_errors,
                    sleep=time.sleep,
                )
            except PublishFailure as failure:
                failures += 1
                out.failed(failure, self.session)
                if failure.error in {AUTH_REQUIRED, SUBSCRIPTION_REQUIRED}:
                    break
                continue
            out.published(receipt, receipt_path=receipt_path)
            last = (review_dir, video)
        if failures:
            raise typer.Exit(1)
        return last if self.serve_after else None


def prepare_review_publish(
    *,
    videos: Sequence[Path],
    output: Path | None,
    visibility: str,
    cloud_url: str | None,
    json_report: bool,
    estimate: bool,
    dry_run: bool,
    serve: bool,
    serve_source: object,
) -> ReviewPublishPlan | None:
    """Checks for ``review --publish`` before the paid pipeline starts.

    ``None`` means "run the review without publishing" (``--estimate`` /
    ``--dry-run`` produce nothing to publish). ``serve_source`` is Click's
    parameter source for ``--serve``: only an explicit ``--serve`` keeps the
    local server, started after publishing.
    """
    out = PublishOutput(json_mode=False)
    try:
        checked = checked_visibility(visibility)
        if not json_report:
            raise PublishFailure(
                "invalid_usage",
                "--publish needs the JSON report; drop --no-json.",
                exit_code=2,
            )
        if estimate or dry_run:
            out.note("--publish skipped: --estimate and --dry-run produce no review to publish.")
            return None
        session = preflight_cloud_session(cloud_url)
    except PublishFailure as failure:
        out.failed(failure)
        raise typer.Exit(failure.exit_code) from None
    explicit_serve = serve and getattr(serve_source, "name", None) == "COMMANDLINE"
    return ReviewPublishPlan(
        session=session,
        visibility=checked,
        watch=ReviewOutputWatch.start(videos, output),
        serve_after=explicit_serve,
    )
