from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from click.testing import Result
from typer.testing import CliRunner

from screenscribe.cli import app
from screenscribe.cli_paths import MAX_PREPROCESS_MANIFEST_BYTES, is_preprocess_bundle
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
        "screenscribe.transcribe.transcribe_audio",
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
    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio", raise_403)
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


# --------------------------------------------------------------------------- #
# -o PATH resolution: an existing ORDINARY folder is a parent, not a bundle.   #
# --------------------------------------------------------------------------- #


def _preprocess_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[CliRunner, Path]:
    """Success stubs (real bundle writer) + a ``demo.mov`` input."""
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")

    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 6.0)
    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio",
        lambda *args, **kwargs: _sample_transcription(),
    )
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )
    return CliRunner(), video_path


def _run_preprocess(
    runner: CliRunner, video_path: Path, output: Path | None, *extra: str
) -> Result:
    args = ["preprocess", str(video_path)]
    if output is not None:
        args += ["-o", str(output)]
    return runner.invoke(app, [*args, *extra])


def _manifest_mode(bundle_dir: Path) -> str:
    manifest = json.loads((bundle_dir / "preprocess.json").read_text(encoding="utf-8"))
    return str(manifest["mode"])


def test_preprocess_ordinary_folder_with_foreign_transcript_is_a_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o ~/Downloads`` holding an unrelated ``transcript.txt`` writes
    ``Downloads/demo_preprocess`` -- no ``Downloads_2`` sibling, no loose files."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    foreign = downloads / "transcript.txt"
    foreign.write_text("someone else's transcript")

    result = _run_preprocess(runner, video_path, downloads)
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert _manifest_mode(downloads / "demo_preprocess") == "preprocess"
    assert not (tmp_path / "Downloads_2").exists()
    assert not (downloads / "preprocess.json").exists()
    assert foreign.read_text() == "someone else's transcript"
    assert "not a previous preprocess bundle" in normalized
    assert "Found Previous Preprocess Bundle" not in normalized

    # Re-run: versions INSIDE the folder, never next to it.
    result2 = _run_preprocess(runner, video_path, downloads)
    assert result2.exit_code == 0, result2.output
    assert _manifest_mode(downloads / "demo_preprocess_2") == "preprocess"
    assert not (tmp_path / "Downloads_2").exists()

    # --force reuses <folder>/<stem>_preprocess in place.
    result3 = _run_preprocess(runner, video_path, downloads, "--force")
    assert result3.exit_code == 0, result3.output
    assert not (downloads / "demo_preprocess_3").exists()
    assert "Found Previous Preprocess Bundle" not in " ".join(result3.output.split())


def test_preprocess_existing_bundle_dir_still_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o`` naming a real previous bundle keeps versioning next to it."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    run_dir = tmp_path / "my-run"

    first = _run_preprocess(runner, video_path, run_dir)
    assert first.exit_code == 0, first.output
    assert _manifest_mode(run_dir) == "preprocess"  # non-existent -> used as-is

    second = _run_preprocess(runner, video_path, run_dir)
    assert second.exit_code == 0, second.output
    assert _manifest_mode(tmp_path / "my-run_2") == "preprocess"
    assert not (run_dir / "demo_preprocess").exists()
    assert "Found Previous Preprocess Bundle" in " ".join(second.output.split())

    forced = _run_preprocess(runner, video_path, run_dir, "--force")
    assert forced.exit_code == 0, forced.output
    assert not (tmp_path / "my-run_3").exists()
    assert not (run_dir / "demo_preprocess").exists()


def test_preprocess_without_output_uses_default_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``-o``: ``<video>_preprocess`` next to the video, then ``_2``."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)

    assert _run_preprocess(runner, video_path, None).exit_code == 0
    assert _manifest_mode(tmp_path / "demo_preprocess") == "preprocess"

    assert _run_preprocess(runner, video_path, None).exit_code == 0
    assert _manifest_mode(tmp_path / "demo_preprocess_2") == "preprocess"


def test_is_preprocess_bundle_rule(tmp_path: Path) -> None:
    """Only screenscribe's own ``preprocess.json`` manifest makes a bundle."""

    def make(name: str, manifest: str | bytes | None, *, transcript: bool = False) -> Path:
        folder = tmp_path / name
        folder.mkdir()
        if transcript:
            (folder / "transcript.txt").write_text("hello")
        if isinstance(manifest, bytes):
            (folder / "preprocess.json").write_bytes(manifest)
        elif manifest is not None:
            (folder / "preprocess.json").write_text(manifest)
        return folder

    assert not is_preprocess_bundle(make("transcript_only", None, transcript=True))
    assert not is_preprocess_bundle(make("garbage", "not json {"))
    assert not is_preprocess_bundle(make("binary", b"\xff\xfe\x00junk"))
    assert not is_preprocess_bundle(make("array", "[1, 2, 3]"))
    assert not is_preprocess_bundle(make("foreign_tool", json.dumps({"artifacts": {}})))
    assert not is_preprocess_bundle(
        make("other_mode", json.dumps({"mode": "review", "artifacts": {}}))
    )
    assert not is_preprocess_bundle(
        make("no_artifacts", json.dumps({"mode": "preprocess", "artifacts": None}))
    )
    oversized = json.dumps(
        {"mode": "preprocess", "artifacts": {}, "pad": "x" * MAX_PREPROCESS_MANIFEST_BYTES}
    )
    assert not is_preprocess_bundle(make("oversized", oversized))
    assert not is_preprocess_bundle(tmp_path / "missing")

    manifest_dir = make("manifest_is_dir", None)
    (manifest_dir / "preprocess.json").mkdir()
    assert not is_preprocess_bundle(manifest_dir)

    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    assert not is_preprocess_bundle(a_file)

    assert is_preprocess_bundle(
        make("real", json.dumps({"mode": "preprocess", "artifacts": {"transcript": "t.txt"}}))
    )


def test_preprocess_real_writer_output_is_a_bundle(tmp_path: Path) -> None:
    """The rule matches what ``write_preprocess_bundle`` actually writes."""
    out = tmp_path / "bundle"
    write_preprocess_bundle(
        video_path=tmp_path / "demo.mov",
        output_dir=out,
        transcription=_sample_transcription(),
        duration_seconds=6.0,
        include_audio=False,
    )
    assert is_preprocess_bundle(out)


def test_preprocess_foreign_manifest_folder_is_a_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A folder whose ``preprocess.json`` is not ours is an ordinary folder."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "preprocess.json").write_text('{"tool": "something-else"}')

    result = _run_preprocess(runner, video_path, folder)
    assert result.exit_code == 0, result.output
    assert _manifest_mode(folder / "demo_preprocess") == "preprocess"
    assert (folder / "preprocess.json").read_text() == '{"tool": "something-else"}'
    assert not (tmp_path / "work_2").exists()


def _assert_friendly_preprocess_output_error(result: Result, reason: str) -> None:
    output = result.output
    normalized = " ".join(output.split())
    assert result.exit_code == 1, output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in output
    assert "Output Directory Error" in normalized
    assert reason in normalized


def test_preprocess_output_under_a_file_exits_with_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o some_file/sub`` -> NotADirectoryError: clear message, exit 1."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")

    result = _run_preprocess(runner, video_path, blocker / "sub")
    _assert_friendly_preprocess_output_error(result, "a file (not a folder) already exists")


def test_preprocess_output_is_an_existing_file_exits_with_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o existing_file`` -> FileExistsError: clear message, exit 1."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    target = tmp_path / "taken"
    target.write_text("i am a file")

    result = _run_preprocess(runner, video_path, target)
    _assert_friendly_preprocess_output_error(result, "a file (not a folder) already exists")


def test_preprocess_output_permission_denied_exits_with_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An existing locked folder is a parent; creating its subfolder fails cleanly."""
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("needs a non-root POSIX user for permission checks")
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = _run_preprocess(runner, video_path, locked)
    finally:
        locked.chmod(0o700)
    _assert_friendly_preprocess_output_error(result, "permission denied")
    assert "demo_preprocess" in " ".join(result.output.split())
