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
"""

from pathlib import Path
from typing import Any

import httpx
import typer
from rich.panel import Panel

from .api_utils import APIError
from .audio import MediaDecodeError
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


def _build_transcription_failure_message(exc: Exception) -> str:
    """Turn a raw STT transport/HTTP error into actionable, traceback-free guidance."""
    status: int | None = None
    server_detail = ""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            body = exc.response.json()
            server_detail = str(
                body.get("message") or body.get("error") or body.get("detail") or ""
            )
        except Exception:
            server_detail = (exc.response.text or "").strip()[:200]
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
            "Set SCREENSCRIBE_API_KEY, run `uv run screenscribe config --set-key YOUR_KEY`, "
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
            "Set SCREENSCRIBE_API_KEY, run `uv run screenscribe config --set-key YOUR_KEY`, "
            "or use --local for local STT."
        )
    return (
        f"Could not reach the speech-to-text service: {exc}\n\n"
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
