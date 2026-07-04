"""Artifact-first preprocessing helpers for transcript-driven review workflows."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

from .checkpoint import serialize_transcription
from .transcribe import (
    MIN_TRANSCRIPT_TIMELINE_COVERAGE,
    TranscriptionResult,
    calculate_transcript_timeline_coverage,
    transcript_last_segment_end,
    transcript_timeline_coverage_is_safe,
)
from .vtt_generator import generate_webvtt

console = Console()


def format_timestamped_transcript(transcription: TranscriptionResult) -> str:
    """Format transcript into stable timestamped lines."""
    return "\n".join(
        f"[{segment.start:.1f}s - {segment.end:.1f}s] {segment.text}"
        for segment in transcription.segments
    )


def write_preprocess_bundle(
    *,
    video_path: Path,
    output_dir: Path,
    transcription: TranscriptionResult,
    duration_seconds: float | None,
    extracted_audio_path: Path | None = None,
    include_audio: bool = True,
) -> dict[str, Path]:
    """Write transcript-first preprocessing artifacts for downstream model work."""
    output_dir.mkdir(parents=True, exist_ok=True)

    transcript_txt = output_dir / "transcript.txt"
    transcript_timestamped = output_dir / "transcript.timestamped.txt"
    segments_json = output_dir / "transcript.segments.json"
    transcript_vtt = output_dir / "transcript.vtt"
    manifest_json = output_dir / "preprocess.json"
    audio_output = output_dir / "audio.mp3"
    output_root = output_dir.resolve()
    project_root = Path.cwd().resolve()

    def to_manifest_path(path: Path | None) -> str | None:
        if not path:
            return None
        resolved_path = path.resolve()
        try:
            return str(resolved_path.relative_to(output_root))
        except ValueError:
            return str(resolved_path)

    def to_project_relative_path(path: Path) -> str:
        resolved_path = path.resolve()
        return os.path.relpath(resolved_path, start=project_root)

    transcript_txt.write_text(transcription.text, encoding="utf-8")
    transcript_timestamped.write_text(
        format_timestamped_transcript(transcription),
        encoding="utf-8",
    )
    segments_json.write_text(
        json.dumps(serialize_transcription(transcription), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    transcript_vtt.write_text(
        generate_webvtt(transcription.segments, language=transcription.language),
        encoding="utf-8",
    )

    audio_path_for_manifest: str | None = None
    if include_audio and extracted_audio_path and extracted_audio_path.exists():
        shutil.copy2(extracted_audio_path, audio_output)
        audio_path_for_manifest = to_manifest_path(audio_output)

    timeline_coverage = calculate_transcript_timeline_coverage(transcription, duration_seconds)
    last_segment_end = transcript_last_segment_end(transcription)
    timeline_safe = (
        transcript_timeline_coverage_is_safe(transcription, duration_seconds)
        if timeline_coverage is not None
        else None
    )

    manifest: dict[str, Any] = {
        "video": to_project_relative_path(video_path),
        # Preserve the legacy key shape while avoiding absolute path leakage.
        "video_absolute": to_project_relative_path(video_path),
        # datetime.UTC is not available on Python 3.10 runtimes used by this project.
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),  # noqa: UP017
        "mode": "preprocess",
        "language": transcription.language,
        "duration_seconds": duration_seconds,
        "transcript_timeline_coverage": timeline_coverage,
        "transcript_last_segment_end_seconds": last_segment_end,
        "transcript_timeline_coverage_minimum": MIN_TRANSCRIPT_TIMELINE_COVERAGE,
        "transcript_timeline_coverage_safe": timeline_safe,
        "response_id": transcription.response_id or None,
        "stats": {
            "segments": len(transcription.segments),
            "words": len(transcription.text.split()),
        },
        "artifacts": {
            "transcript": to_manifest_path(transcript_txt),
            "timestamped_transcript": to_manifest_path(transcript_timestamped),
            "segments_json": to_manifest_path(segments_json),
            "webvtt": to_manifest_path(transcript_vtt),
            "audio": audio_path_for_manifest,
        },
    }
    manifest_json.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    console.print(
        f"[green]Preprocess bundle saved:[/] [link=file://{output_dir}]{output_dir}[/link]"
    )
    if timeline_safe is False:
        console.print(
            "[yellow]Warning:[/] Transcript timeline coverage is low; "
            "timestamp-based screenshots may need chunked transcription."
        )

    return {
        "transcript": transcript_txt,
        "timestamped_transcript": transcript_timestamped,
        "segments_json": segments_json,
        "webvtt": transcript_vtt,
        "manifest": manifest_json,
        **({"audio": audio_output} if audio_path_for_manifest else {}),
    }
