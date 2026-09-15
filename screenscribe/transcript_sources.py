"""Transcript source abstraction for the review pipeline.

A transcript source produces timestamped segments (``{start, end, text}``)
that feed semantic analysis and response chaining. Two sources exist today:

- ``audio``: the existing STT path (extract audio -> chunked transcription).
  The review pipeline keeps its historical two-stage checkpointed code for
  this source; ``AudioTranscriptSource`` is the protocol-conformant wrapper
  for callers that do not need stage granularity.
- ``ocr``: frame-interval VLM-OCR (see ``frame_ocr``), used when the
  recording has no audio track or when the operator passes
  ``--transcript-source ocr`` / ``--no-audio``.

Hook for future sources (e.g. ``.srt`` subtitle files): implement the
``TranscriptSource`` protocol and register the instance in
``TRANSCRIPT_SOURCE_REGISTRY`` — the pipeline resolves sources by name.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .config import ScreenScribeConfig
from .transcribe_types import TranscriptionResult

TranscriptSourceName = Literal["auto", "audio", "ocr"]
VALID_TRANSCRIPT_SOURCES: tuple[str, ...] = ("auto", "audio", "ocr")
ResolvedTranscriptSource = Literal["audio", "ocr"]


@runtime_checkable
class TranscriptSource(Protocol):
    """A named producer of TranscriptionResult from a video file."""

    name: str

    def transcribe(self, video_path: Path, config: ScreenScribeConfig) -> TranscriptionResult:
        """Produce timestamped transcript segments for ``video_path``."""
        ...


class AudioTranscriptSource:
    """STT transcript source (today's behavior, protocol-wrapped).

    Reads the extract/transcribe steps through ``screenscribe.cli`` so the
    historical monkeypatch surface keeps binding, exactly like the guards in
    ``cli_messages``.
    """

    name = "audio"

    def transcribe(self, video_path: Path, config: ScreenScribeConfig) -> TranscriptionResult:
        import screenscribe.cli as cli

        audio_path = cli._extract_audio_or_exit(video_path)
        return cli.transcribe_audio_chunked(
            audio_path,
            language=config.language,
            use_local=False,
            api_key=config.get_stt_api_key(),
            stt_endpoint=config.stt_endpoint,
            stt_model=config.stt_model,
        )


class OcrTranscriptSource:
    """VLM-OCR transcript source: frames every N seconds -> OCR -> segments."""

    name = "ocr"

    def __init__(self, frame_interval: float = 5.0) -> None:
        self.frame_interval = frame_interval

    def transcribe(self, video_path: Path, config: ScreenScribeConfig) -> TranscriptionResult:
        from .frame_ocr import transcribe_video_ocr

        return transcribe_video_ocr(video_path, config, frame_interval=self.frame_interval)


TRANSCRIPT_SOURCE_REGISTRY: dict[str, TranscriptSource] = {
    "audio": AudioTranscriptSource(),
    "ocr": OcrTranscriptSource(),
}


def get_transcript_source(name: ResolvedTranscriptSource) -> TranscriptSource:
    """Look up a resolved source implementation by name."""
    return TRANSCRIPT_SOURCE_REGISTRY[name]


def normalize_transcript_source(requested: str, *, no_audio: bool = False) -> TranscriptSourceName:
    """Validate the flag combination and fold ``--no-audio`` into the source.

    ``--no-audio`` is an alias for ``--transcript-source ocr``; combining it
    with an explicit ``--transcript-source audio`` is a contradiction and is
    rejected here instead of being silently resolved either way.
    """
    source = requested.strip().lower()
    if source not in VALID_TRANSCRIPT_SOURCES:
        valid = ", ".join(VALID_TRANSCRIPT_SOURCES)
        raise ValueError(f"Invalid --transcript-source '{requested}' (expected one of: {valid}).")
    if no_audio:
        if source == "audio":
            raise ValueError(
                "--no-audio conflicts with --transcript-source audio "
                "(--no-audio is an alias for --transcript-source ocr)."
            )
        return "ocr"
    return source  # type: ignore[return-value]


def resolve_transcript_source(
    requested: str,
    video_path: Path,
    *,
    has_audio: Callable[[Path], bool],
) -> ResolvedTranscriptSource:
    """Resolve ``auto``/explicit source for one video to ``audio`` or ``ocr``.

    ``auto`` picks ``audio`` when the container carries an audio stream and
    falls back to ``ocr`` otherwise, so a silent recording reaches semantic
    analysis instead of dying at the audio gate.
    """
    if requested == "ocr":
        return "ocr"
    if requested == "audio":
        return "audio"
    return "audio" if has_audio(video_path) else "ocr"
