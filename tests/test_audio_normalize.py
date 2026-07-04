"""Unit tests for audio normalization (normalize_audio_for_stt, _transcode_input_to_mp3)."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from screenscribe.audio import MediaDecodeError, normalize_audio_for_stt


def _fake_ffmpeg_success(*args: object, **kwargs: object) -> CompletedProcess[str]:
    return CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fake_ffmpeg_decode_error(*args: object, **kwargs: object) -> CompletedProcess[str]:
    return CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="moov atom not found\nInvalid data found when processing input",
    )


def _fake_ffmpeg_generic_error(*args: object, **kwargs: object) -> CompletedProcess[str]:
    return CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="Unknown error occurred",
    )


def test_normalize_creates_output_at_default_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio_file = tmp_path / "recording.webm"
    audio_file.write_bytes(b"fake-audio-data")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _fake_ffmpeg_success)

    result = normalize_audio_for_stt(audio_file)
    assert result.name == "recording_normalized.mp3"


def test_normalize_creates_output_at_custom_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio_file = tmp_path / "recording.webm"
    audio_file.write_bytes(b"fake-audio-data")
    custom_output = tmp_path / "custom_output.mp3"

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _fake_ffmpeg_success)

    result = normalize_audio_for_stt(audio_file, output_path=custom_output)
    assert result == custom_output


def test_normalize_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.wav"
    with pytest.raises(FileNotFoundError, match="Audio file not found"):
        normalize_audio_for_stt(missing)


def test_normalize_raises_media_decode_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio_file = tmp_path / "corrupt.ogg"
    audio_file.write_bytes(b"corrupt")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _fake_ffmpeg_decode_error)

    with pytest.raises(MediaDecodeError, match="Could not decode media file"):
        normalize_audio_for_stt(audio_file)


def test_normalize_raises_runtime_on_generic_ffmpeg_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio_file = tmp_path / "bad.flac"
    audio_file.write_bytes(b"bad-data")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _fake_ffmpeg_generic_error)

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        normalize_audio_for_stt(audio_file)


def test_transcode_passes_correct_ffmpeg_params(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio_file = tmp_path / "input.webm"
    audio_file.write_bytes(b"audio")
    captured_cmds: list[list[str]] = []

    def _capture_cmd(*args: object, **kwargs: object) -> CompletedProcess[str]:
        if args and isinstance(args[0], list):
            captured_cmds.append(args[0])
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _capture_cmd)

    normalize_audio_for_stt(audio_file)

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert cmd[0] == "ffmpeg"
    assert "-vn" in cmd
    assert "-ar" in cmd
    idx = cmd.index("-ar")
    assert cmd[idx + 1] == "16000"
    assert "-ac" in cmd
    idx = cmd.index("-ac")
    assert cmd[idx + 1] == "1"
