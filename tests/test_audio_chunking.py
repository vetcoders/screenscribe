"""Silence-aware chunking regression tests.

Covers detect_silence_boundaries (ffmpeg silencedetect parsing) and
split_audio_chunks (silence-cut chunking with fixed-interval fallback and WAV
output). These restore the timestamp-drift fix: Whisper scrambles timestamps on
long recordings, so long audio is cut at natural pauses into WAV chunks.
"""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from typing import Any

import pytest

from screenscribe.audio import (
    detect_silence_boundaries,
    get_audio_duration,
    split_audio_chunks,
)


def _silencedetect_run(stderr: str):
    """Fake subprocess.run mimicking ffmpeg silencedetect (output on stderr)."""

    def _run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=0, stdout="", stderr=stderr)

    return _run


def test_detect_silence_boundaries_returns_sorted_midpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    stderr = (
        "[silencedetect @ 0x1] silence_start: 10.0\n"
        "[silencedetect @ 0x1] silence_end: 12.0 | silence_duration: 2.0\n"
        "[silencedetect @ 0x1] silence_start: 4.0\n"
        "[silencedetect @ 0x1] silence_end: 6.0 | silence_duration: 2.0\n"
    )
    monkeypatch.setattr("screenscribe.audio.subprocess.run", _silencedetect_run(stderr))

    boundaries = detect_silence_boundaries(audio)

    # Midpoints of the two gaps: (4+6)/2=5.0 and (10+12)/2=11.0, sorted.
    assert boundaries == [5.0, 11.0]


def test_detect_silence_boundaries_ignores_unpaired_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    stderr = (
        "[silencedetect @ 0x1] silence_start: 3.0\n"
        "[silencedetect @ 0x1] silence_end: 5.0 | silence_duration: 2.0\n"
        "[silencedetect @ 0x1] silence_start: 99.0\n"  # no matching end -> ignored
    )
    monkeypatch.setattr("screenscribe.audio.subprocess.run", _silencedetect_run(stderr))

    assert detect_silence_boundaries(audio) == [4.0]


def test_split_audio_chunks_short_audio_not_chunked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 30.0)

    chunks = split_audio_chunks(audio, max_chunk_duration=60.0)

    assert chunks == [(audio, 0.0)]


def test_split_audio_chunks_fixed_interval_fallback_when_no_silence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr("screenscribe.audio.detect_silence_boundaries", lambda _p: [])

    recorded_cmds: list[list[str]] = []

    def _run(cmd: list[str], *_args: Any, **_kwargs: Any) -> CompletedProcess[str]:
        recorded_cmds.append(cmd)
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _run)

    chunks = split_audio_chunks(audio, max_chunk_duration=60.0, overlap=3.0)

    offsets = [offset for _path, offset in chunks]
    # Fixed-interval offsets must increase monotonically from 0.
    assert offsets == sorted(offsets)
    assert offsets[0] == 0.0
    assert len(set(offsets)) == len(offsets)
    assert len(chunks) >= 3

    # WAV is part of the fix (MP3 encoder delay scrambles timestamps).
    for path, _offset in chunks:
        assert path.suffix == ".wav"
    assert recorded_cmds, "ffmpeg should have been invoked to cut chunks"
    for cmd in recorded_cmds:
        assert "pcm_s16le" in cmd


def test_split_audio_chunks_isolates_temp_dir_per_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two runs of the same audio stem must not share chunk paths.

    Concurrent reviews of files with the same stem previously wrote into one
    shared temp dir keyed only by stem + index, so each run overwrote the other's
    WAVs (ffmpeg -y) and the per-run cleanup could remove a chunk the other run
    was still transcribing. Each split must get its own isolated temp directory.
    """
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr("screenscribe.audio.detect_silence_boundaries", lambda _p: [])

    def _run(cmd: list[str], *_args: Any, **_kwargs: Any) -> CompletedProcess[str]:
        # Simulate ffmpeg writing the chunk WAV to the path it was given.
        Path(cmd[-1]).write_bytes(b"chunk")
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _run)

    run1 = split_audio_chunks(audio, max_chunk_duration=60.0, overlap=3.0)
    run2 = split_audio_chunks(audio, max_chunk_duration=60.0, overlap=3.0)

    dirs1 = {p.parent for p, _ in run1}
    dirs2 = {p.parent for p, _ in run2}
    # Each run writes into a single dedicated dir, and the two never share it.
    assert len(dirs1) == 1
    assert len(dirs2) == 1
    assert dirs1.isdisjoint(dirs2), "concurrent same-stem runs collide in one temp dir"

    # No individual chunk path is reused across runs (no clobber/race surface).
    paths1 = {p for p, _ in run1}
    paths2 = {p for p, _ in run2}
    assert paths1.isdisjoint(paths2)


def test_split_audio_chunks_raises_and_cleans_up_on_mid_split_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed chunk split must raise (not silently return a partial prefix).

    Returning only the chunks made before an ffmpeg failure used to let the
    caller transcribe that prefix and report success, dropping the rest of a long
    recording. The failure must surface as RuntimeError, and every WAV/temp dir
    created so far must be cleaned up rather than orphaned.
    """
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr("screenscribe.audio.detect_silence_boundaries", lambda _p: [])

    made_paths: list[Path] = []
    calls = {"n": 0}

    def _run(cmd: list[str], *_args: Any, **_kwargs: Any) -> CompletedProcess[str]:
        calls["n"] += 1
        # First chunk succeeds (writes its WAV); the second one fails mid-split.
        if calls["n"] == 1:
            out = Path(cmd[-1])
            out.write_bytes(b"chunk")
            made_paths.append(out)
            return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="ffmpeg boom")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _run)

    with pytest.raises(RuntimeError, match="chunk 1 split failed"):
        split_audio_chunks(audio, max_chunk_duration=60.0, overlap=3.0)

    # The first chunk's WAV and its temp dir must not be left behind.
    assert made_paths, "the first chunk should have been written before the failure"
    for p in made_paths:
        assert not p.exists(), f"orphaned chunk left behind: {p}"
        assert not p.parent.exists(), f"orphaned temp dir left behind: {p.parent}"


def test_split_audio_chunks_discards_in_flight_partial_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A partial WAV from the failing chunk itself must be cleaned up.

    ffmpeg can write a truncated output file and then exit non-zero. That
    in-flight ``chunk_path`` is not yet in the ``chunks`` list (the append only
    happens on success), so if cleanup ignores it the stray file keeps the temp
    dir non-empty and ``rmdir`` silently leaks both.
    """
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr("screenscribe.audio.detect_silence_boundaries", lambda _p: [])

    seen: list[Path] = []

    def _run(cmd: list[str], *_args: Any, **_kwargs: Any) -> CompletedProcess[str]:
        out = Path(cmd[-1])
        # Every call writes a (partial) file, then the first chunk fails.
        out.write_bytes(b"partial")
        seen.append(out)
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="ffmpeg boom")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _run)

    with pytest.raises(RuntimeError, match="chunk 0 split failed"):
        split_audio_chunks(audio, max_chunk_duration=60.0, overlap=3.0)

    assert seen, "the failing chunk should have written a partial file"
    partial = seen[0]
    assert not partial.exists(), f"in-flight partial chunk left behind: {partial}"
    assert not partial.parent.exists(), f"orphaned temp dir left behind: {partial.parent}"


def test_split_audio_chunks_discards_in_flight_partial_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A partial WAV from a timed-out ffmpeg must be cleaned up too."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr("screenscribe.audio.detect_silence_boundaries", lambda _p: [])

    seen: list[Path] = []

    def _run(cmd: list[str], *_args: Any, **_kwargs: Any) -> CompletedProcess[str]:
        out = Path(cmd[-1])
        out.write_bytes(b"partial")  # ffmpeg wrote some bytes before wedging
        seen.append(out)
        raise TimeoutExpired(cmd=cmd, timeout=1.0)

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _run)

    with pytest.raises(RuntimeError, match="chunk 0 split timed out"):
        split_audio_chunks(audio, max_chunk_duration=60.0, overlap=3.0)

    assert seen, "the timed-out chunk should have written a partial file"
    partial = seen[0]
    assert not partial.exists(), f"in-flight partial chunk left behind: {partial}"
    assert not partial.parent.exists(), f"orphaned temp dir left behind: {partial.parent}"


def test_get_audio_duration_parses_ffprobe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")

    def _run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(args=[], returncode=0, stdout="123.45\n", stderr="")

    monkeypatch.setattr("screenscribe.audio.subprocess.run", _run)

    assert get_audio_duration(audio) == pytest.approx(123.45)
