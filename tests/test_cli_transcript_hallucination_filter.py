"""The transcribe/preprocess CLI lanes must run the anti-hallucination filter.

`filter_hallucinated_segments` used to be wired only into the review pipeline
(FW-09), so `screenscribe transcribe` and `screenscribe preprocess` emitted the
raw STT segments -- including no-speech phantom captions Whisper invents on
music/silence. The preprocess bundle is the transcript-first handoff for
downstream agents, so a phantom outro there poisons everything downstream.
These tests pin that both single-command lanes drop the phantom segment (FW-09b).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from screenscribe.cli import app
from screenscribe.config import ScreenScribeConfig
from screenscribe.transcribe import Segment, TranscriptionResult


def _speech_plus_phantom() -> TranscriptionResult:
    """A real, confident finding segment followed by a phantom past-audio-end
    outro (the exact shape measured on Utah001.mp4's no-speech tail)."""
    speech = Segment(
        id=0,
        start=0.0,
        end=4.0,
        text="okay so the save button in settings does not persist my changes",
        no_speech_prob=0.02,
        avg_logprob=-0.28,
        compression_ratio=1.4,
    )
    phantom = Segment(
        id=1,
        start=30.0,
        end=59.98,
        text="Dziękuję za oglądanie!",
        no_speech_prob=0.581,
        avg_logprob=-0.369,
        compression_ratio=0.75,
    )
    return TranscriptionResult(
        text=f"{speech.text} {phantom.text}",
        segments=[speech, phantom],
        language="pl",
    )


def _patch_stt_pipeline(
    monkeypatch: pytest.MonkeyPatch, audio_path: Path, result: TranscriptionResult
) -> None:
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: audio_path)
    # Real audio end is 46.37s: the phantom's 59.98s end is past it -> dropped.
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 46.37)
    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )


def test_transcribe_command_drops_hallucinated_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    transcript_out = tmp_path / "transcript.txt"

    _patch_stt_pipeline(monkeypatch, audio_path, _speech_plus_phantom())

    result = runner.invoke(app, ["transcribe", str(video_path), "-o", str(transcript_out)])

    assert result.exit_code == 0, result.output
    text = transcript_out.read_text(encoding="utf-8")
    assert "save button" in text
    assert "oglądanie" not in text


def test_preprocess_bundle_drops_hallucinated_segment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    output_dir = tmp_path / "artifacts"

    _patch_stt_pipeline(monkeypatch, audio_path, _speech_plus_phantom())

    result = runner.invoke(app, ["preprocess", str(video_path), "-o", str(output_dir)])

    assert result.exit_code == 0, result.output

    transcript = (output_dir / "transcript.txt").read_text(encoding="utf-8")
    assert "save button" in transcript
    assert "oglądanie" not in transcript

    segments_payload = json.loads(
        (output_dir / "transcript.segments.json").read_text(encoding="utf-8")
    )
    kept_texts = [seg["text"] for seg in segments_payload["segments"]]
    assert any("save button" in t for t in kept_texts)
    assert all("oglądanie" not in t for t in kept_texts)
