"""User-facing message builders and STT/audio guard wrappers for the CLI.

Extracted from ``cli.py``. These turn raw transport/decode errors into
actionable, traceback-free guidance and provide the ``*_or_exit`` guards the
single-command paths (``transcribe``/``preprocess``) and the review pipeline
use.

Routing contract: the guards call the patchable pipeline steps
(``extract_audio``, ``require_audio_stream``, ``transcribe_audio``) and print
through the console **via the cli module object** so the historical
``monkeypatch.setattr("screenscribe.cli.<name>", ...)`` surface keeps binding.
``cli.py`` re-exports every public name here back into its namespace.

Transcript-source routing: ``_require_audio_or_exit`` is no longer an
unconditional pre-flight gate in ``review``. ``cli.review`` first resolves the
transcript source per video (``transcript_sources.resolve_transcript_source``)
and only calls this guard for videos routed to the ``audio`` source, so an
explicit ``--transcript-source audio`` keeps the historical readable error
while ``auto``/``ocr`` route silent recordings to frame OCR instead.
"""

import errno
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.markup import escape
from rich.panel import Panel

from .api_utils import APIError, redact_error_message, redact_urls_in_text
from .audio import MediaDecodeError
from .cli_paths import OutputSlotError
from .detect import format_timestamp
from .transcribe import (
    MIN_TRANSCRIPT_TIMELINE_COVERAGE,
    calculate_transcript_timeline_coverage,
    transcript_last_segment_end,
)


def _extract_audio_or_exit(video: Path) -> Path:
    """Extract audio with CLI-friendly error handling."""
    import screenscribe.cli as cli

    try:
        return cli.extract_audio(video)
    except MediaDecodeError as e:
        cli.console.print(f"[red]Media Error:[/] {e}")
        raise typer.Exit(1) from None


def _require_audio_or_exit(video: Path) -> None:
    """Fail before config/model validation when a review input has no audio."""
    import screenscribe.cli as cli

    try:
        cli.require_audio_stream(video)
    except MediaDecodeError as e:
        cli.console.print(f"[red]Media Error:[/] {e}")
        raise typer.Exit(1) from None


def _build_transcript_timeline_coverage_message(
    transcription: Any,
    duration_seconds: float | None,
) -> str:
    """Build a user-facing warning for unsafe STT timestamp coverage."""
    coverage = calculate_transcript_timeline_coverage(transcription, duration_seconds)
    last_segment_end = transcript_last_segment_end(transcription)
    coverage_text = "unknown" if coverage is None else f"{coverage:.0%}"
    last_text = "unknown" if last_segment_end is None else format_timestamp(last_segment_end)
    duration_text = "unknown" if duration_seconds is None else format_timestamp(duration_seconds)
    minimum_text = f"{MIN_TRANSCRIPT_TIMELINE_COVERAGE:.0%}"
    return (
        "Transcript timeline coverage is low; end-of-video screenshots may not "
        "match findings.\n"
        f"STT timeline coverage: {coverage_text} "
        f"(last segment at {last_text}, video duration {duration_text}; "
        f"minimum {minimum_text} for videos longer than 5 minutes).\n\n"
        "The audio tail still carries sound, so STT timestamps may have drifted "
        "or compressed on this recording.\n"
        "Continuing the review; for tighter timestamp alignment, try chunked "
        "transcription or a shorter recording."
    )


def _describe_output_os_error(path: Path, exc: OSError) -> tuple[str, str]:
    """``(reason, where)`` for an OSError hit while creating/writing output."""
    blocked = Path(exc.filename) if exc.filename else path
    if isinstance(exc, PermissionError):
        reason = "permission denied"
    elif exc.errno == errno.EROFS:
        reason = "the file system is read-only"
    elif isinstance(exc, (NotADirectoryError, FileExistsError)):
        reason = "a file (not a folder) already exists at that location"
    else:
        reason = exc.strerror or str(exc)
    where = "" if blocked == path else f"\n[dim]Blocked at:[/] {escape(str(blocked))}"
    return reason, where


def _build_output_dir_error_message(path: Path, exc: OSError) -> str:
    """Turn an output-directory ``mkdir`` OSError into actionable guidance."""
    reason, where = _describe_output_os_error(path, exc)
    return (
        f"Cannot create the output directory: {escape(str(path))}\n"
        f"[dim]Reason:[/] {escape(reason)}{where}\n\n"
        "Pass [bold]-o[/] with a folder you can write to, for example one inside "
        "your home directory."
    )


def _build_bundle_write_error_message(path: Path, exc: OSError) -> str:
    """An OSError while writing the preprocess bundle files into ``path``.

    Partial files are left in place (never cleaned up), so the message says so.
    """
    reason, where = _describe_output_os_error(path, exc)
    return (
        f"Cannot write the preprocess bundle to: {escape(str(path))}\n"
        f"[dim]Reason:[/] {escape(reason)}{where}\n\n"
        "Files written before the error were left in place. Free up space or fix "
        "permissions and re-run, or pass [bold]-o[/] with a folder you can write to."
    )


def _build_force_foreign_message(path: Path, artifact: str = "review") -> str:
    """Why ``--force`` refuses a target screenscribe does not own.

    ``artifact`` names what the command owns (``"review"`` for ``review``,
    ``"preprocess bundle"`` for ``preprocess``).
    """
    return (
        f"{escape(str(path))} already exists and is not a screenscribe {artifact} folder.\n\n"
        f"[bold]--force[/] only overwrites a previous screenscribe {artifact}, so it will "
        "not overwrite or clean this location. Pass [bold]-o[/] with a new folder."
    )


def _build_cache_clear_error_message(cache_dir: Path, exc: OSError) -> str:
    """Why ``--force`` could not remove the previous checkpoint cache."""
    reason = "permission denied" if isinstance(exc, PermissionError) else (exc.strerror or str(exc))
    return (
        f"Cannot clear the previous checkpoint cache at {escape(str(cache_dir))}\n"
        f"[dim]Reason:[/] {escape(reason)}\n\n"
        "Remove it manually, or pass [bold]-o[/] with a new folder."
    )


def _build_versions_exhausted_message(base_path: Path, limit: int) -> str:
    """Friendly text for ``OutputVersionsExhaustedError`` (no free ``_N`` slot)."""
    return (
        f"Too many existing versions of {escape(base_path.name)} (limit {limit}) in "
        f"{escape(str(base_path.parent))}.\n\n"
        "Pass [bold]-o[/] with a new folder, or remove old versions you no longer need."
    )


def _build_output_slot_error_message(error: OutputSlotError) -> str:
    """Friendly text for ``OutputSlotError`` raised while reserving an output folder."""
    path = escape(str(error.path))
    if error.kind == "create_failed" and error.cause is not None:
        return _build_output_dir_error_message(error.path, error.cause)
    if error.kind == "unwritable":
        cause = error.cause
        if isinstance(cause, PermissionError):
            reason = "permission denied"
        elif cause is not None and cause.errno == errno.EROFS:
            reason = "the file system is read-only"
        else:
            reason = (cause.strerror if cause is not None else "") or "unknown error"
        return (
            f"The output folder exists but cannot be written: {path}\n"
            f"[dim]Reason:[/] {escape(reason)}\n\n"
            "Pass [bold]-o[/] with a folder you can write to."
        )
    if error.kind == "foreign":
        return (
            f"{path} was taken by a file or folder screenscribe does not own while the "
            "output was being prepared; nothing was written there.\n\n"
            "Re-run, or pass [bold]-o[/] with a new folder."
        )
    return (
        f"Could not reserve an output folder near {path}: other files kept appearing "
        "at the chosen location.\n\n"
        "Re-run, or pass [bold]-o[/] with a new folder."
    )


def _build_transcription_failure_message(exc: Exception) -> str:
    """Turn a raw STT transport/HTTP error into actionable, traceback-free guidance."""
    status: int | None = None
    server_detail = ""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            body = exc.response.json()
            server_detail = redact_urls_in_text(
                str(body.get("message") or body.get("error") or body.get("detail") or "")
            )
        except Exception:
            # Redact the whole body before the 200-char cut so a URL on the
            # boundary cannot leak its userinfo or query.
            server_detail = redact_urls_in_text((exc.response.text or "").strip())[:200]
    detail_suffix = f": {server_detail}" if server_detail else "."

    if status == 429:
        return (
            f"The speech-to-text service is rate-limited or at capacity{detail_suffix}\n\n"
            "This is a temporary server-side limit, not a problem with your video.\n"
            "Wait a moment and re-run with --resume to retry, or point "
            "SCREENSCRIBE_STT_ENDPOINT at a different OpenAI-compatible STT endpoint."
        )
    if status in {500, 502, 503, 504}:
        return (
            f"The speech-to-text service returned a server error (HTTP {status}){detail_suffix}\n\n"
            "This is usually temporary. Re-run with --resume to retry."
        )
    if status in {401, 403}:
        return (
            f"The speech-to-text service rejected the credentials (HTTP {status}){detail_suffix}\n\n"
            "Run `screenscribe config setup`, set SCREENSCRIBE_API_KEY, "
            "or set SCREENSCRIBE_STT_API_KEY for a dedicated STT key. "
            "If the key is correct, check SCREENSCRIBE_STT_ENDPOINT."
        )
    if status is not None:
        return (
            f"Speech-to-text failed (HTTP {status}){detail_suffix}\n\n"
            "Check the configured STT endpoint and API key."
        )
    if isinstance(exc, ValueError) and "API key required" in str(exc):
        return (
            "No speech-to-text API key is configured.\n\n"
            "Run `screenscribe config setup`, set SCREENSCRIBE_API_KEY, "
            "or use --local for local STT."
        )
    return (
        f"Could not reach the speech-to-text service: {escape(redact_error_message(exc))}\n\n"
        "Check your network connection and the configured STT endpoint, then "
        "re-run with --resume to retry."
    )


def _transcribe_audio_or_exit(
    audio_path: Path,
    *,
    language: str,
    use_local: bool,
    api_key: str | None,
    stt_endpoint: str | None,
    stt_model: str,
    resume_hint: bool = False,
) -> Any:
    """Run STT for single-command paths with traceback-free CLI errors."""
    import screenscribe.cli as cli

    try:
        # Route through the chunked entry point: it transparently delegates to
        # the single-shot path for short audio and only splits long recordings
        # (silence-aware) to keep STT timestamps accurate.
        return cli.transcribe_audio_chunked(
            audio_path,
            language=language,
            use_local=use_local,
            api_key=api_key,
            stt_endpoint=stt_endpoint,
            stt_model=stt_model,
        )
    except (httpx.HTTPStatusError, httpx.RequestError, APIError, ValueError) as exc:
        cli.console.print()
        cli.console.print(
            Panel(
                _build_transcription_failure_message(exc),
                title="[bold red]Transcription Failed[/]",
                border_style="red",
            )
        )
        if resume_hint:
            cli.console.print(
                "[yellow]Transcription stopped before writing the preprocess bundle.[/] "
                "Update credentials and re-run the command."
            )
        raise typer.Exit(1) from None
