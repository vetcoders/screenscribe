"""Audio extraction regression tests."""

from pathlib import Path
from subprocess import CompletedProcess

import pytest

from screenscribe.audio import (
    FFmpegNotFoundError,
    MediaDecodeError,
    MissingAudioStreamError,
    check_ffmpeg_installed,
    extract_audio,
    get_video_duration,
    has_audio_stream,
    tail_is_silent,
)


def test_macos_shared_homebrew_guidance_does_not_recommend_chown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def which(tool: str) -> str | None:
        return "/opt/homebrew/bin/brew" if tool == "brew" else None

    monkeypatch.setattr("screenscribe.audio.sys.platform", "darwin")
    monkeypatch.setattr("screenscribe.audio.shutil.which", which)
    monkeypatch.setattr(
        "screenscribe.audio.subprocess.run",
        lambda *_args, **_kwargs: CompletedProcess(
            args=["brew", "--prefix"],
            returncode=0,
            stdout="/opt/homebrew\n",
            stderr="",
        ),
    )
    monkeypatch.setattr("screenscribe.audio.os.access", lambda *_args: False)

    with pytest.raises(FFmpegNotFoundError) as error:
        check_ffmpeg_installed()

    message = str(error.value)
    assert "Homebrew is installed, but this user cannot modify it." in message
    assert "Ask the Homebrew owner or an administrator to run:" in message
    assert "brew install ffmpeg" in message
    assert "Do not change ownership of the Homebrew prefix." in message
    assert "chown" not in message


def _volumedetect_run(max_volume_db: float):
    """Build a fake subprocess.run that mimics ffmpeg volumedetect stderr output."""

    def _run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr=(
                "[Parsed_volumedetect_0 @ 0x0] n_samples: 480000\n"
                "[Parsed_volumedetect_0 @ 0x0] mean_volume: -70.0 dB\n"
                f"[Parsed_volumedetect_0 @ 0x0] max_volume: {max_volume_db} dB\n"
            ),
        )

    return _run


def test_tail_is_silent_true_for_quiet_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.subprocess.run", _volumedetect_run(-91.0))

    assert tail_is_silent(audio, 510.0, 838.0) is True


def test_tail_is_silent_false_for_audible_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.subprocess.run", _volumedetect_run(-12.0))

    assert tail_is_silent(audio, 510.0, 838.0) is False


def test_tail_is_silent_none_for_empty_range(tmp_path: Path) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")

    assert tail_is_silent(audio, 838.0, 838.0) is None


def test_tail_is_silent_none_when_audio_missing(tmp_path: Path) -> None:
    assert tail_is_silent(tmp_path / "nope.mp3", 0.0, 10.0) is None


def test_tail_is_silent_none_when_no_peak_parsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")

    def _run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=1, stdout="", stderr="ffmpeg exploded")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _run)

    assert tail_is_silent(audio, 0.0, 10.0) is None


def test_extract_audio_raises_friendly_decode_error_for_invalid_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "broken.mov"
    video.write_bytes(b"not-a-real-mov")

    def _fake_run(*args: object, **kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=(
                "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x0] moov atom not found\n"
                "Error opening input file broken.mov.\n"
                "Error opening input files: Invalid data found when processing input"
            ),
        )

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _fake_run)

    with pytest.raises(MediaDecodeError) as exc_info:
        extract_audio(video)

    assert "Could not decode media file 'broken.mov'" in str(exc_info.value)


def _make_ffprobe_router(audio_present: bool, ffprobe_returncode: int = 0) -> object:
    """Build a subprocess.run replacement that distinguishes ffprobe vs ffmpeg.

    ffprobe stdout encodes audio presence ("audio" line per stream); ffmpeg
    leg fails loudly so the test catches accidental fallthrough.
    """

    def _fake_run(cmd: list[str], *args: object, **kwargs: object) -> CompletedProcess[str]:
        binary = cmd[0] if cmd else ""
        if binary == "ffprobe":
            return CompletedProcess(
                args=cmd,
                returncode=ffprobe_returncode,
                stdout="audio\n" if audio_present else "",
                stderr="" if ffprobe_returncode == 0 else "ffprobe error",
            )
        raise AssertionError(f"ffmpeg should not be invoked when audio is missing; got cmd={cmd}")

    return _fake_run


def test_has_audio_stream_returns_false_when_video_has_no_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "silent.mov"
    video.write_bytes(b"placeholder")

    monkeypatch.setattr(
        "screenscribe.audio.subprocess.run", _make_ffprobe_router(audio_present=False)
    )

    assert has_audio_stream(video) is False


def test_has_audio_stream_returns_true_when_video_has_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "loud.mov"
    video.write_bytes(b"placeholder")

    monkeypatch.setattr(
        "screenscribe.audio.subprocess.run", _make_ffprobe_router(audio_present=True)
    )

    assert has_audio_stream(video) is True


def test_has_audio_stream_returns_true_optimistically_when_ffprobe_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "weird_container.mov"
    video.write_bytes(b"placeholder")

    monkeypatch.setattr(
        "screenscribe.audio.subprocess.run",
        _make_ffprobe_router(audio_present=False, ffprobe_returncode=1),
    )

    assert has_audio_stream(video) is True


def test_extract_audio_raises_missing_audio_stream_when_no_audio_track(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "no_audio.mov"
    video.write_bytes(b"placeholder")

    monkeypatch.setattr(
        "screenscribe.audio.subprocess.run", _make_ffprobe_router(audio_present=False)
    )

    with pytest.raises(MissingAudioStreamError) as exc_info:
        extract_audio(video)

    message = str(exc_info.value)
    assert "no_audio.mov" in message
    assert "no audio track" in message.lower()
    assert "Tip: run:\n  screenscribe analyze <video-path>" in message
    assert f"Video path:\n  {video}" in message
    assert "interactive vision-only review" in message


def test_missing_audio_stream_error_is_caught_as_media_decode_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI handler catches MediaDecodeError; ensure inheritance holds."""
    video = tmp_path / "no_audio.mov"
    video.write_bytes(b"placeholder")

    monkeypatch.setattr(
        "screenscribe.audio.subprocess.run", _make_ffprobe_router(audio_present=False)
    )

    with pytest.raises(MediaDecodeError):
        extract_audio(video)


def _duration_run(stdout: str, returncode: int = 0) -> object:
    """Build a subprocess.run replacement that emits a fixed ffprobe duration."""

    def _run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="boom")

    return _run


def test_get_video_duration_parses_numeric(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr("screenscribe.audio.subprocess.run", _duration_run("123.45\n"))

    assert get_video_duration(video) == 123.45


def test_get_video_duration_na_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ffprobe can return rc=0 with 'N/A' duration; float() would ValueError.

    The caller (review_pipeline) only guards RuntimeError, so the N/A case must
    surface as RuntimeError rather than an unguarded ValueError crash.
    """
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr("screenscribe.audio.subprocess.run", _duration_run("N/A\n"))

    with pytest.raises(RuntimeError):
        get_video_duration(video)


def test_get_video_duration_empty_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr("screenscribe.audio.subprocess.run", _duration_run("   \n"))

    with pytest.raises(RuntimeError):
        get_video_duration(video)
