from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from screenscribe.cli import app
from screenscribe.config import ScreenScribeConfig
from screenscribe.preprocess import write_preprocess_bundle
from screenscribe.transcribe import Segment, TranscriptionResult
from screenscribe.vtt_generator import generate_webvtt


def _sample_transcription(language: str = "en") -> TranscriptionResult:
    return TranscriptionResult(
        text="Open the login screen and finish auth with the pasted code.",
        segments=[
            Segment(id=1, start=0.0, end=2.5, text="Open the login screen."),
            Segment(
                id=2,
                start=2.5,
                end=6.0,
                text="Finish auth with the pasted code.",
            ),
        ],
        language=language,
        response_id="resp_stt_test_123",
    )


def test_write_preprocess_bundle_creates_transcript_artifacts(tmp_path: Path) -> None:
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")

    audio_path = tmp_path / "source.mp3"
    audio_path.write_bytes(b"audio")

    output_dir = tmp_path / "demo_preprocess"
    transcription = _sample_transcription(language="en")

    artifacts = write_preprocess_bundle(
        video_path=video_path,
        output_dir=output_dir,
        transcription=transcription,
        duration_seconds=12.4,
        extracted_audio_path=audio_path,
        include_audio=True,
    )

    assert artifacts["transcript"].read_text(encoding="utf-8") == transcription.text
    timestamped = artifacts["timestamped_transcript"].read_text(encoding="utf-8")
    assert "[0.0s - 2.5s] Open the login screen." in timestamped

    segments_payload = json.loads(artifacts["segments_json"].read_text(encoding="utf-8"))
    assert segments_payload["language"] == "en"
    assert len(segments_payload["segments"]) == 2

    vtt = artifacts["webvtt"].read_text(encoding="utf-8")
    assert "WEBVTT" in vtt
    assert "Language: en" in vtt

    manifest = json.loads(artifacts["manifest"].read_text(encoding="utf-8"))
    assert manifest["mode"] == "preprocess"
    assert manifest["language"] == "en"
    assert manifest["duration_seconds"] == 12.4
    assert manifest["transcript_timeline_coverage"] == pytest.approx(6.0 / 12.4)
    assert manifest["transcript_last_segment_end_seconds"] == 6.0
    assert manifest["transcript_timeline_coverage_safe"] is True
    assert manifest["stats"]["segments"] == 2
    assert manifest["response_id"] == "resp_stt_test_123"
    assert manifest["generated_at"].endswith("+00:00")
    assert manifest["video"].endswith("demo.mov")
    assert manifest["video_absolute"].endswith("demo.mov")
    assert not os.path.isabs(manifest["video"])
    assert not os.path.isabs(manifest["video_absolute"])
    assert manifest["artifacts"]["transcript"] == "transcript.txt"
    assert manifest["artifacts"]["timestamped_transcript"] == "transcript.timestamped.txt"
    assert manifest["artifacts"]["segments_json"] == "transcript.segments.json"
    assert manifest["artifacts"]["webvtt"] == "transcript.vtt"

    audio_path = manifest["artifacts"]["audio"]
    assert audio_path
    assert (output_dir / audio_path).exists()


def test_generate_webvtt_language_header() -> None:
    segments = [
        Segment(id=1, start=0.0, end=3.2, text="Hello from screen", no_speech_prob=0.0),
        Segment(id=2, start=3.2, end=5.6, text="Move to next step", no_speech_prob=0.0),
    ]
    vtt = generate_webvtt(segments, language="en")
    assert vtt.splitlines()[2] == "Language: en"


def test_preprocess_command_builds_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = CliRunner()
    video_path = tmp_path / "auth-flow.mov"
    video_path.write_bytes(b"video")

    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "artifacts"

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 64.2)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _sample_transcription(language="pl"),
    )
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    result = runner.invoke(app, ["preprocess", str(video_path), "-o", str(output_dir)])

    assert result.exit_code == 0, result.output
    assert (output_dir / "preprocess.json").exists()
    assert (output_dir / "transcript.txt").exists()
    assert (output_dir / "transcript.timestamped.txt").exists()
    assert (output_dir / "transcript.segments.json").exists()
    assert (output_dir / "transcript.vtt").exists()
    assert (output_dir / "audio.mp3").exists()


def test_preprocess_auth_error_is_friendly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    video_path = tmp_path / "auth-flow.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "artifacts"

    def raise_403(*_args: object, **_kwargs: object) -> TranscriptionResult:
        request = httpx.Request("POST", "https://api.example.com/v1/audio/transcriptions")
        response = httpx.Response(
            403,
            json={"message": "Forbidden API key"},
            request=request,
        )
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 64.2)
    monkeypatch.setattr("screenscribe.cli.transcribe_audio", raise_403)
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="bad-key")),
    )

    result = runner.invoke(app, ["preprocess", str(video_path), "-o", str(output_dir)])
    normalized_output = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert "Transcription Failed" in normalized_output
    assert "rejected the credentials" in normalized_output
    assert "SCREENSCRIBE_API_KEY" in normalized_output
    assert "Traceback" not in result.output
    assert "HTTPStatusError" not in result.output
