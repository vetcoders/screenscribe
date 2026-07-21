"""Shared helpers for the analyze and review FastAPI servers.

``analyze_server`` and ``review_server`` are intentionally two distinct
surfaces, but they carried byte-identical copies of a couple of helpers.
Those duplicates live here so both servers import one implementation:

- :data:`MAX_AUDIO_BYTES` — the browser STT upload cap.
- :func:`install_stt_upload_cap` — middleware that rejects an honestly
  declared oversized STT upload before the body is parsed.
- :func:`read_upload_capped` — chunked ``UploadFile`` read that aborts as
  soon as the running total crosses a byte cap, so a client that lies
  about (or omits) Content-Length cannot balloon memory.
- :func:`transcribe_browser_audio` — direct browser-upload transcription
  with an MP3-normalization fallback.
- :func:`validate_browser_stt_result` — the single browser-STT quality
  gate (422 on unusable speech, non-blocking warning otherwise).
- :func:`serialize_stt_result` — the single STT result serializer, so
  both servers emit one key-set (``text``/``segments``/``response_id``/
  ``language`` plus optional ``quality_warning``).

Behavior is identical to the previous per-server copies; only the home of
the code changed.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .api_utils import AUTH_FAILURE_STATUS_CODES

if TYPE_CHECKING:
    from .config import ScreenScribeConfig
    from .transcribe import TranscriptionResult

logger = logging.getLogger(__name__)


class ResponseChainSession(Protocol):
    """Structural view of the analyze/review session needed to advance the
    shared VLM/STT conversation-chain head under its lock."""

    last_response_id: str
    lock: threading.RLock


def advance_response_id_cas(
    session: ResponseChainSession,
    expected: str,
    new_response_id: str,
    *,
    logger: logging.Logger,
) -> bool:
    """Compare-and-set the shared conversation-chain head (``last_response_id``).

    Both servers chain VLM/STT calls through ``previous_response_id``: an
    operation reads the current head, runs a long *unlocked* upstream call, then
    writes the new id back. Two operations that overlapped both read the same
    head; the one that writes second would clobber the id the first already
    committed, so the next analysis chains from a stale/overwritten id.

    This advances the head to ``new_response_id`` only when BOTH hold:

    - ``new_response_id`` is non-empty — an empty id must never clobber a valid
      head (the existing BH29/BH45/BH46/BH49 guard, folded in here);
    - the head still equals ``expected`` — the value this operation read when it
      began. If the head moved, a concurrent operation finished first and owns
      the newer id; this (losing) writer keeps that newer id and logs at debug
      rather than overwriting it.

    Runs under ``session.lock`` (a reentrant :class:`threading.RLock`) so it
    composes with a caller that already holds the lock. Returns ``True`` iff the
    head was advanced.
    """
    with session.lock:
        if not new_response_id:
            return False
        current = session.last_response_id
        if current != expected:
            logger.debug(
                "last_response_id CAS lost: head moved from %r to %r during the "
                "operation; keeping the newer id and discarding %r",
                expected,
                current,
                new_response_id,
            )
            return False
        session.last_response_id = new_response_id
        return True


# Generic, user-safe message handed to API clients in place of raw exception
# text. The full detail (exception type, args, traceback, upstream STT/LLM
# error bodies, filesystem paths) is written to the server log only — never to
# the browser.
_GENERIC_ERROR_MESSAGE = "An internal error occurred. Please check the server logs for details."


def sanitized_error(
    exc: BaseException,
    error_code: str,
    *,
    log_message: str | None = None,
) -> dict[str, str]:
    """Return a client-safe error payload and log the full exception.

    The wire payload is exactly ``{"error_code": <code>, "message": <generic>}``
    — it never carries ``str(exc)``, a traceback, or any path/upstream detail.
    The full exception (with traceback) is logged at ERROR level so operators
    keep the diagnostic without leaking it to the client.
    """
    logger.error(
        "%s [error_code=%s]: %s",
        log_message or "Unhandled API error",
        error_code,
        exc,
        exc_info=exc,
    )
    return {"error_code": error_code, "message": _GENERIC_ERROR_MESSAGE}


def _is_non_format_error(error: Exception) -> bool:
    """True for failures the MP3-normalization fallback can never fix.

    BH18: ``transcribe_browser_audio`` retries a failed direct upload via
    ffmpeg normalization, which only ever fixes a FORMAT problem (a codec the
    STT endpoint could not accept). Two failure classes are not format
    problems and must fail fast instead of paying for a pointless re-encode +
    second STT round-trip that masks the real cause:

    - a missing/invalid API key (and the empty-payload guard), raised as
      ``ValueError`` before any HTTP call is made;
    - an auth rejection from the STT endpoint, surfaced as
      ``httpx.HTTPStatusError`` with a 401/403 status.
    """
    if isinstance(error, ValueError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in AUTH_FAILURE_STATUS_CODES
    return False


MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB cap on browser STT uploads

# Priority values a manual per-marker override accepts (Analyze A7b + Review R14).
# The four severity levels plus an explicit "none" so an operator can CLEAR a
# priority. Anything else is ignored rather than raising — a stale client must
# never wedge a marker edit. Single-sourced here so the analyze + review servers
# validate against the same vocabulary.
VALID_MARKER_SEVERITIES: frozenset[str] = frozenset({"critical", "high", "medium", "low", "none"})

# Read uploads one MiB at a time so memory tracks the cap, not the payload.
_UPLOAD_READ_CHUNK = 1024 * 1024


async def read_upload_capped(
    upload: UploadFile,
    *,
    max_bytes: int = MAX_AUDIO_BYTES,
    detail: str = "Audio upload exceeds 25 MB limit.",
) -> bytes:
    """Read an ``UploadFile`` in chunks, aborting once ``max_bytes`` is crossed.

    The ``_stt_upload_cap`` middleware already rejects an honestly-declared
    oversized Content-Length before the body is parsed; this helper closes
    the remaining hole — a client that lies about or omits Content-Length
    can no longer force an unbounded ``await upload.read()`` into RAM. As
    soon as the running total exceeds ``max_bytes`` we stop reading and
    raise ``413`` instead of buffering the rest of the body.
    """
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = await upload.read(_UPLOAD_READ_CHUNK)
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise HTTPException(status_code=413, detail=detail)
        chunks.append(chunk)
    return b"".join(chunks)


def validate_browser_stt_upload(content: bytes) -> None:
    """Reject an empty or too-short browser STT upload before transcription.

    Both ``/api/stt`` handlers apply the same two guards to the bytes read by
    :func:`read_upload_capped`: an empty upload is rejected outright, and a
    sub-1KB browser recording is too short to carry usable speech, so it is
    refused before paying for a provider round-trip (and the possible
    ffmpeg-normalization fallback). The ``400`` detail strings are a contract
    with the browser frontends, which surface them verbatim, so they must stay
    byte-identical across both servers — that is exactly why this guard lives
    in one place.
    """
    if not content:
        raise HTTPException(status_code=400, detail="Voice recording is empty.")
    if len(content) < 1024:
        raise HTTPException(
            status_code=400,
            detail="Voice recording is too short. Hold to record longer.",
        )


def validate_browser_stt_result(result: Any) -> str | None:
    """Validate browser voice notes before exposing STT text to the UI.

    BH59 (mirror of the pipeline ``validate_audio_quality``): a degraded
    browser-STT result (silent/near-silent recording, generic hallucination,
    repetitive output) must be rejected (422) instead of being handed back as
    a usable transcript. A non-blocking warning is returned instead so the
    client can surface it via ``quality_warning`` without dropping the text.
    """
    from .transcribe import validate_audio_quality

    is_valid, message, is_warning = validate_audio_quality(result)
    if not is_valid:
        raise HTTPException(
            status_code=422,
            detail=message or "Voice recording did not contain usable speech.",
        )
    return message if is_warning else None


def serialize_stt_result(
    result: TranscriptionResult,
    *,
    quality_warning: str | None = None,
) -> dict[str, Any]:
    """Serialize an STT :class:`TranscriptionResult` into the wire payload.

    Both ``/api/stt`` handlers share this one key-set so the single-frame
    (review) and analyze surfaces no longer diverge: ``text``, ``segments``
    (``start``/``end``/``text``), ``response_id`` and ``language`` are always
    present; ``quality_warning`` is added only when a caller passes one.
    """
    payload: dict[str, Any] = {
        "text": result.text,
        "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in result.segments],
        "response_id": result.response_id,
        "language": result.language,
    }
    if quality_warning:
        payload["quality_warning"] = quality_warning
    return payload


def install_stt_upload_cap(app: FastAPI) -> None:
    """Register the ``/api/stt`` upload-cap middleware on ``app``.

    Rejects an honestly-declared oversized STT upload before the body is
    parsed/spooled at all (the handler still enforces the cap chunk-wise
    for clients that lie about or omit Content-Length).
    """

    @app.middleware("http")
    async def _stt_upload_cap(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/api/stt":
            declared = request.headers.get("content-length", "")
            # Small slack on top of the cap for multipart framing overhead.
            if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES + 64 * 1024:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Audio upload exceeds 25 MB limit."},
                )
        return await call_next(request)


def transcribe_browser_audio(
    audio_data: bytes,
    *,
    filename: str,
    content_type: str,
    config: ScreenScribeConfig,
) -> Any:
    """Transcribe browser uploads directly, then fall back to MP3 normalization."""
    from .audio import normalize_audio_for_stt
    from .transcribe import transcribe_audio, transcribe_audio_bytes

    try:
        return transcribe_audio_bytes(
            audio_data,
            filename,
            content_type=content_type,
            language=config.language,
            api_key=config.get_stt_api_key(),
            stt_endpoint=config.stt_endpoint,
            stt_model=config.stt_model,
        )
    except Exception as direct_error:
        # BH18: only FORMAT failures benefit from the ffmpeg-normalization
        # retry. Auth/missing-key errors are not format problems — re-encoding
        # and re-calling STT just doubles latency and buries the real error, so
        # fail fast on those instead of attempting the fallback.
        if _is_non_format_error(direct_error):
            raise
        logger.warning(
            "Direct browser audio transcription failed; retrying via MP3 normalization: %s",
            direct_error,
        )
        source_suffix = Path(filename).suffix or ".webm"
        source_path: Path | None = None
        normalized_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=source_suffix, delete=False) as tmp:
                tmp.write(audio_data)
                source_path = Path(tmp.name)
            normalized_path = normalize_audio_for_stt(source_path)
            return transcribe_audio(
                normalized_path,
                language=config.language,
                api_key=config.get_stt_api_key(),
                stt_endpoint=config.stt_endpoint,
                stt_model=config.stt_model,
            )
        finally:
            if source_path is not None:
                source_path.unlink(missing_ok=True)
            if normalized_path is not None:
                normalized_path.unlink(missing_ok=True)
