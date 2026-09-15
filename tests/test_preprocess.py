from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

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
    assert target.read_bytes() == b"i am a file"
    assert not (tmp_path / "taken_2").exists()


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


def test_preprocess_version_slot_skips_ordinary_non_empty_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real bundle at ``<base>`` plus an ordinary non-empty ``<base>_2`` must
    version to ``<base>_3``: ``_2`` is occupied even though it is not a bundle,
    so its foreign files are never overwritten."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    run_dir = tmp_path / "my-run"

    first = _run_preprocess(runner, video_path, run_dir)
    assert first.exit_code == 0, first.output
    assert is_preprocess_bundle(run_dir)

    slot_2 = tmp_path / "my-run_2"
    slot_2.mkdir()
    foreign_transcript = b"someone else's transcript\n"
    foreign_manifest = b'{"tool": "something-else", "mode": "other"}'
    (slot_2 / "transcript.txt").write_bytes(foreign_transcript)
    (slot_2 / "preprocess.json").write_bytes(foreign_manifest)
    assert not is_preprocess_bundle(slot_2)

    second = _run_preprocess(runner, video_path, run_dir)
    assert second.exit_code == 0, second.output
    assert _manifest_mode(tmp_path / "my-run_3") == "preprocess"
    assert (slot_2 / "transcript.txt").read_bytes() == foreign_transcript
    assert (slot_2 / "preprocess.json").read_bytes() == foreign_manifest
    assert sorted(p.name for p in slot_2.iterdir()) == ["preprocess.json", "transcript.txt"]


# --------------------------------------------------------------------------- #
# Output slot ownership: foreign base slots are skipped, --force fails closed. #
# --------------------------------------------------------------------------- #


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every entry under ``root`` (and ``root`` itself) -> bytes, or None for dirs."""
    entries = [root, *root.rglob("*")] if root.is_dir() else [root]
    return {
        str(entry.relative_to(root.parent)): (None if entry.is_dir() else entry.read_bytes())
        for entry in entries
    }


def _make_foreign_dir(path: Path) -> dict[str, bytes | None]:
    path.mkdir(parents=True)
    (path / "notes.txt").write_bytes(b"my own notes\n")
    (path / "transcript.txt").write_bytes(b"someone else's transcript\n")
    (path / "preprocess.json").write_bytes(b'{"tool": "something-else"}')
    return _snapshot(path)


def test_preprocess_parent_rule_skips_foreign_base_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o X`` with a foreign non-empty ``X/demo_preprocess`` -> ``X/demo_preprocess_2``."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    parent = tmp_path / "X"
    base = parent / "demo_preprocess"
    before = _make_foreign_dir(base)

    result = _run_preprocess(runner, video_path, parent)
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert _manifest_mode(parent / "demo_preprocess_2") == "preprocess"
    assert _snapshot(base) == before
    assert "demo_preprocess exists and is not a screenscribe preprocess bundle" in normalized
    assert "Found Previous Preprocess Bundle" not in normalized


def test_preprocess_parent_rule_skips_foreign_base_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o X`` with a FILE at ``X/demo_preprocess`` -> ``X/demo_preprocess_2``."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    parent = tmp_path / "X"
    parent.mkdir()
    base = parent / "demo_preprocess"
    base.write_bytes(b"a file named like the bundle")

    result = _run_preprocess(runner, video_path, parent)

    assert result.exit_code == 0, result.output
    assert _manifest_mode(parent / "demo_preprocess_2") == "preprocess"
    assert base.read_bytes() == b"a file named like the bundle"


def test_preprocess_default_output_skips_foreign_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``-o`` with a foreign ``<video dir>/demo_preprocess`` -> ``demo_preprocess_2``."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    base = tmp_path / "demo_preprocess"
    before = _make_foreign_dir(base)

    result = _run_preprocess(runner, video_path, None)

    assert result.exit_code == 0, result.output
    assert _manifest_mode(tmp_path / "demo_preprocess_2") == "preprocess"
    assert _snapshot(base) == before


def test_preprocess_empty_base_dir_is_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty ``demo_preprocess`` base directory is free and written in place."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    base = tmp_path / "demo_preprocess"
    base.mkdir()

    result = _run_preprocess(runner, video_path, None)

    assert result.exit_code == 0, result.output
    assert _manifest_mode(base) == "preprocess"
    assert not (tmp_path / "demo_preprocess_2").exists()


def _assert_force_refused(result: Result) -> None:
    normalized = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "Output Directory Error" in normalized
    assert "is not a screenscribe preprocess bundle folder" in normalized
    assert "--force only overwrites a previous screenscribe preprocess bundle" in normalized
    assert "review" not in normalized


def _forbid_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("--force on a foreign target must stop before any work")

    monkeypatch.setattr("screenscribe.cli.extract_audio", boom)
    monkeypatch.setattr("screenscribe.cli.write_preprocess_bundle", boom)


def test_preprocess_force_refuses_foreign_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--force`` onto a foreign non-empty base folder fails closed, nothing changes."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    _forbid_pipeline(monkeypatch)
    base = tmp_path / "demo_preprocess"
    before = _make_foreign_dir(base)
    tree_before = sorted(p.name for p in tmp_path.iterdir())

    result = _run_preprocess(runner, video_path, None, "--force")

    _assert_force_refused(result)
    assert _snapshot(base) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == tree_before


def test_preprocess_force_refuses_foreign_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--force`` with a FILE at ``X/demo_preprocess`` fails closed, nothing changes."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    _forbid_pipeline(monkeypatch)
    parent = tmp_path / "X"
    parent.mkdir()
    base = parent / "demo_preprocess"
    base.write_bytes(b"a file named like the bundle")

    result = _run_preprocess(runner, video_path, parent, "--force")

    _assert_force_refused(result)
    assert base.read_bytes() == b"a file named like the bundle"
    assert sorted(p.name for p in parent.iterdir()) == ["demo_preprocess"]


def test_preprocess_force_overwrites_own_bundle_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--force`` on screenscribe's own bundle rewrites it in place (no ``_2``)."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    base = tmp_path / "demo_preprocess"
    assert _run_preprocess(runner, video_path, None).exit_code == 0
    (base / "transcript.txt").write_text("stale")

    result = _run_preprocess(runner, video_path, None, "--force")

    assert result.exit_code == 0, result.output
    assert (base / "transcript.txt").read_text() == _sample_transcription().text
    assert not (tmp_path / "demo_preprocess_2").exists()


# --------------------------------------------------------------------------- #
# Friendly errors: version cap and bundle write failures.                     #
# --------------------------------------------------------------------------- #


def test_preprocess_version_cap_is_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No free ``_N`` slot below the cap -> Output Directory Error, exit 1."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    base = tmp_path / "demo_preprocess"
    assert _run_preprocess(runner, video_path, None).exit_code == 0
    slot_2_before = _make_foreign_dir(tmp_path / "demo_preprocess_2")
    base_before = _snapshot(base)
    monkeypatch.setattr("screenscribe.cli.MAX_REVIEW_VERSIONS", 2)

    result = _run_preprocess(runner, video_path, None)
    normalized = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "Output Directory Error" in normalized
    assert "Too many existing versions of demo_preprocess (limit 2)" in normalized
    assert "review" not in normalized
    assert _snapshot(base) == base_before
    assert _snapshot(tmp_path / "demo_preprocess_2") == slot_2_before
    assert not (tmp_path / "demo_preprocess_3").exists()


def _assert_bundle_write_error(result: Result, reason: str) -> None:
    normalized = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "Output Directory Error" in normalized
    assert "Cannot write the preprocess bundle to:" in normalized
    assert reason in normalized


def test_preprocess_write_text_failure_is_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A PermissionError from a bundle file write -> panel, exit 1, dir kept."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    base = tmp_path / "demo_preprocess"
    real_write_text = Path.write_text

    def failing_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.parent == base.resolve() and self.name == "transcript.timestamped.txt":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    result = _run_preprocess(runner, video_path, None)

    _assert_bundle_write_error(result, "permission denied")
    assert base.is_dir()
    assert (base / "transcript.txt").exists()  # partial output is not cleaned up
    assert not (base / "preprocess.json").exists()


def test_preprocess_audio_copy_failure_is_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ENOSPC while copying audio into the bundle -> panel, exit 1, dir kept."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    base = tmp_path / "demo_preprocess"

    def no_space(_src: object, dst: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device", str(dst))

    monkeypatch.setattr("screenscribe.preprocess.shutil.copy2", no_space)

    result = _run_preprocess(runner, video_path, None)

    _assert_bundle_write_error(result, "No space left on device")
    assert base.is_dir()
    assert (base / "transcript.vtt").exists()


# --------------------------------------------------------------------------- #
# Atomic slot reservation + writability probe, before any audio/STT work.     #
# --------------------------------------------------------------------------- #


def _forbid_audio_and_stt(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record extract/transcribe calls; the failure tests assert the list stays empty."""
    calls: list[str] = []

    def no_extract(_video: object) -> Path:
        calls.append("extract_audio")
        raise AssertionError("extract_audio must not run when the output is unusable")

    def no_stt(*_a: object, **_k: object) -> TranscriptionResult:
        calls.append("transcribe_audio")
        raise AssertionError("transcription must not run when the output is unusable")

    monkeypatch.setattr("screenscribe.cli.extract_audio", no_extract)
    monkeypatch.setattr("screenscribe.transcribe.transcribe_audio", no_stt)
    return calls


def test_preprocess_slot_taken_after_allocation_is_reselected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_2`` turns into a foreign file between allocation and use -> bundle in ``_3``."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    assert _run_preprocess(runner, video_path, None).exit_code == 0
    slot_2 = tmp_path / "demo_preprocess_2"
    foreign = b"appeared during the race"

    import screenscribe.cli as cli_module

    real_allocator = cli_module._find_next_versioned_path
    raced: list[Path] = []

    def racing_allocator(base: Path, **kwargs: Any) -> tuple[Path, int | None]:
        path, version = real_allocator(base, **kwargs)
        if not raced and path == slot_2:
            slot_2.write_bytes(foreign)  # someone grabs the slot right after allocation
            raced.append(path)
        return path, version

    monkeypatch.setattr("screenscribe.cli._find_next_versioned_path", racing_allocator)

    result = _run_preprocess(runner, video_path, None)

    assert result.exit_code == 0, result.output
    assert raced == [slot_2]
    assert slot_2.read_bytes() == foreign
    assert _manifest_mode(tmp_path / "demo_preprocess_3") == "preprocess"


def test_preprocess_force_on_unwritable_own_bundle_fails_before_stt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--force`` on our own bundle that fails the write probe -> exit 1, no STT."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    base = tmp_path / "demo_preprocess"
    assert _run_preprocess(runner, video_path, None).exit_code == 0
    before = _snapshot(base)
    calls = _forbid_audio_and_stt(monkeypatch)

    def denied(*_a: object, **_k: object) -> object:
        raise PermissionError(errno.EACCES, "Permission denied", str(base))

    monkeypatch.setattr("screenscribe.cli_paths.tempfile.NamedTemporaryFile", denied)

    result = _run_preprocess(runner, video_path, None, "--force")
    normalized = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "Output Directory Error" in normalized
    assert "exists but cannot be written" in normalized
    assert "permission denied" in normalized
    assert calls == []
    assert _snapshot(base) == before


def test_preprocess_new_output_under_unwritable_parent_fails_before_stt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o`` naming a new folder whose parent cannot be written -> panel, no STT."""
    runner, video_path = _preprocess_harness(monkeypatch, tmp_path)
    calls = _forbid_audio_and_stt(monkeypatch)
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "new-bundle"
    real_mkdir = Path.mkdir

    def guarded_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == target or locked in self.parents:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)

    result = _run_preprocess(runner, video_path, target)
    normalized = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert "Traceback" not in result.output
    assert "Output Directory Error" in normalized
    assert "permission denied" in normalized
    assert calls == []
    assert list(locked.iterdir()) == []
