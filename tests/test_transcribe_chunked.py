"""Chunked transcription regression tests.

Restores silence-aware chunked transcription: long audio is split, each chunk is
transcribed via the seed STT path, timestamps are offset back to the global
timeline, and segments duplicated across the overlap zone are dropped. Short
audio takes the ordinary single-shot path unchanged.

All STT calls are mocked — no real API is hit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from screenscribe.transcribe import (
    Segment,
    TranscriptionResult,
    transcribe_audio_chunked,
)


def _result(segments: list[Segment], text: str = "", response_id: str = "") -> TranscriptionResult:
    return TranscriptionResult(
        text=text,
        segments=segments,
        language="en",
        response_id=response_id,
    )


def test_chunked_short_audio_uses_single_shot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 30.0)

    calls: list[Path] = []

    def _fake_transcribe(path: Path, **_kwargs: Any) -> TranscriptionResult:
        calls.append(path)
        return _result([Segment(0, 0.0, 5.0, "hello")], text="hello", response_id="r1")

    # split_audio_chunks must NOT be reached for short audio.
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("short audio must not be chunked")

    monkeypatch.setattr("screenscribe.cli.transcribe_audio", _fake_transcribe)
    monkeypatch.setattr("screenscribe.audio.split_audio_chunks", _boom)

    result = transcribe_audio_chunked(audio, chunk_duration=60.0)

    assert calls == [audio]
    assert result.text == "hello"
    assert [s.text for s in result.segments] == ["hello"]


def test_chunked_long_audio_offsets_and_dedups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    chunk0 = tmp_path / "c0.wav"
    chunk1 = tmp_path / "c1.wav"

    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    # Second chunk starts at offset 8s and overlaps the first chunk's tail.
    monkeypatch.setattr(
        "screenscribe.audio.split_audio_chunks",
        lambda *_a, **_kw: [(chunk0, 0.0), (chunk1, 8.0)],
    )

    def _fake_transcribe(path: Path, **_kwargs: Any) -> TranscriptionResult:
        if path == chunk0:
            return _result(
                [Segment(0, 0.0, 5.0, "a"), Segment(1, 5.0, 10.0, "b")],
                text="a b",
                response_id="r0",
            )
        return _result(
            # First segment overlaps prev tail (8+0=8 < 10-1) -> deduped.
            [Segment(0, 0.0, 3.0, "b-overlap"), Segment(1, 3.0, 9.0, "c")],
            text="b c",
            response_id="r1",
        )

    monkeypatch.setattr("screenscribe.cli.transcribe_audio", _fake_transcribe)

    result = transcribe_audio_chunked(audio, chunk_duration=60.0)

    # Overlapping duplicate dropped; surviving segments carry global timestamps.
    assert [s.text for s in result.segments] == ["a", "b", "c"]
    assert [s.start for s in result.segments] == [0.0, 5.0, 11.0]
    assert [s.end for s in result.segments] == [5.0, 10.0, 17.0]
    # Segment ids are reindexed contiguously after merge.
    assert [s.id for s in result.segments] == [0, 1, 2]
    # Latest non-empty response_id is carried for LLM chaining.
    assert result.response_id == "r1"


def test_chunked_keeps_overlap_segment_extending_past_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding O3: a real overlap segment that STARTS in the overlap zone but
    EXTENDS past the previous chunk's tail carries unique speech after the seam.
    The start-only dedup dropped the whole segment, losing that content from both
    `segments` and `merged_text`. It must be kept."""
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    chunk0 = tmp_path / "c0.wav"
    chunk1 = tmp_path / "c1.wav"

    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    # Chunk 1 starts at global 8s and overlaps chunk 0's tail (which ends at 10s).
    monkeypatch.setattr(
        "screenscribe.audio.split_audio_chunks",
        lambda *_a, **_kw: [(chunk0, 0.0), (chunk1, 8.0)],
    )

    def _fake_transcribe(path: Path, **_kwargs: Any) -> TranscriptionResult:
        if path == chunk0:
            return _result(
                [Segment(0, 0.0, 5.0, "a"), Segment(1, 5.0, 10.0, "b")],
                text="a b",
                response_id="r0",
            )
        # First segment: adjusted_start=8.0 (< 10-1, so it overlaps the tail) but
        # adjusted_end=14.0 extends 4s past the prior tail end (10.0) -- it carries
        # unique speech after the seam and must survive dedup.
        return _result(
            [Segment(0, 0.0, 6.0, "past-seam content"), Segment(1, 6.0, 9.0, "c")],
            text="past-seam content c",
            response_id="r1",
        )

    monkeypatch.setattr("screenscribe.cli.transcribe_audio", _fake_transcribe)

    result = transcribe_audio_chunked(audio, chunk_duration=60.0)

    texts = [s.text for s in result.segments]
    assert "past-seam content" in texts, (
        "segment extending past the seam was dropped by start-only dedup"
    )
    assert "past-seam content" in result.text
    # The fully-covered duplicate case is unchanged: a and b survive, c survives.
    assert "a" in texts and "b" in texts and "c" in texts


def test_chunked_falls_back_to_single_shot_when_duration_unprobeable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fake/undecodable audio (ffprobe fails) must still transcribe once.

    This keeps every existing review/preprocess test that feeds a stub audio
    file working after the call sites route through the chunked entry point.
    """
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")

    def _raise(_p: Path) -> float:
        raise RuntimeError("FFprobe failed")

    monkeypatch.setattr("screenscribe.audio.get_audio_duration", _raise)

    calls: list[Path] = []

    def _fake_transcribe(path: Path, **_kwargs: Any) -> TranscriptionResult:
        calls.append(path)
        return _result([Segment(0, 0.0, 1.0, "ok")], text="ok")

    monkeypatch.setattr("screenscribe.cli.transcribe_audio", _fake_transcribe)

    result = transcribe_audio_chunked(audio, chunk_duration=60.0)

    assert calls == [audio]
    assert result.text == "ok"


def test_chunked_merge_propagates_synthetic_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A text-only (synthetic-timestamp) chunk must taint the merged result.

    When a chunk's STT backend returns text without segment timings, that chunk's
    timestamps are speaking-rate estimates (timestamps_are_synthetic=True). The
    merged TranscriptionResult must carry the flag forward so the
    timeline-coverage guard and screenshot selection do not treat the estimated
    timeline as real STT timing (finding G).
    """
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    chunk0 = tmp_path / "c0.wav"
    chunk1 = tmp_path / "c1.wav"

    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr(
        "screenscribe.audio.split_audio_chunks",
        lambda *_a, **_kw: [(chunk0, 0.0), (chunk1, 100.0)],
    )

    def _fake_transcribe(path: Path, **_kwargs: Any) -> TranscriptionResult:
        if path == chunk0:
            return TranscriptionResult(
                text="real",
                segments=[Segment(0, 0.0, 5.0, "real")],
                language="en",
                response_id="r0",
                timestamps_are_synthetic=False,
            )
        # Second chunk came back text-only -> synthetic estimated timestamps.
        return TranscriptionResult(
            text="estimated",
            segments=[Segment(0, 0.0, 12.0, "estimated")],
            language="en",
            response_id="r1",
            timestamps_are_synthetic=True,
        )

    monkeypatch.setattr("screenscribe.cli.transcribe_audio", _fake_transcribe)

    result = transcribe_audio_chunked(audio, chunk_duration=60.0)

    assert result.timestamps_are_synthetic is True


def test_chunked_merge_keeps_synthetic_false_when_all_real(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All-real chunks must NOT mark the merged result as synthetic (finding G control)."""
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    chunk0 = tmp_path / "c0.wav"
    chunk1 = tmp_path / "c1.wav"

    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    monkeypatch.setattr(
        "screenscribe.audio.split_audio_chunks",
        lambda *_a, **_kw: [(chunk0, 0.0), (chunk1, 100.0)],
    )

    def _fake_transcribe(path: Path, **_kwargs: Any) -> TranscriptionResult:
        return _result([Segment(0, 0.0, 5.0, "x")], text="x", response_id="r")

    monkeypatch.setattr("screenscribe.cli.transcribe_audio", _fake_transcribe)

    result = transcribe_audio_chunked(audio, chunk_duration=60.0)

    assert result.timestamps_are_synthetic is False


def test_chunked_synthetic_chunks_keep_text_despite_overlap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Synthetic-timestamp chunks must not lose text to the overlap dedupe.

    When the STT backend returns one synthetic segment per chunk, each chunk's
    timestamps are speaking-rate estimates spanning the whole chunk. Because
    consecutive chunks overlap, the next chunk's estimated start lands inside the
    previous chunk's estimated tail, so the real-timestamp overlap heuristic would
    drop the ENTIRE chunk as a duplicate and silently omit large portions of long
    recordings. Synthetic chunks must skip that timestamp-overlap dedupe (M3,
    edge of the synthetic-flag fix G).
    """
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    chunk0 = tmp_path / "c0.wav"
    chunk1 = tmp_path / "c1.wav"

    monkeypatch.setattr("screenscribe.audio.get_audio_duration", lambda _p: 200.0)
    # Chunk 1 starts at 8s and overlaps chunk 0's estimated tail (8 < 12-1).
    monkeypatch.setattr(
        "screenscribe.audio.split_audio_chunks",
        lambda *_a, **_kw: [(chunk0, 0.0), (chunk1, 8.0)],
    )

    def _fake_transcribe(path: Path, **_kwargs: Any) -> TranscriptionResult:
        if path == chunk0:
            return TranscriptionResult(
                text="first chunk words",
                segments=[Segment(0, 0.0, 12.0, "first chunk words")],
                language="en",
                response_id="r0",
                timestamps_are_synthetic=True,
            )
        return TranscriptionResult(
            text="second chunk words",
            segments=[Segment(0, 0.0, 12.0, "second chunk words")],
            language="en",
            response_id="r1",
            timestamps_are_synthetic=True,
        )

    monkeypatch.setattr("screenscribe.cli.transcribe_audio", _fake_transcribe)

    result = transcribe_audio_chunked(audio, chunk_duration=60.0)

    # Both synthetic chunks survive; no text is dropped by the overlap heuristic.
    assert [s.text for s in result.segments] == ["first chunk words", "second chunk words"]
    assert "second chunk words" in result.text
    assert result.timestamps_are_synthetic is True


def test_transcribe_or_exit_routes_through_chunked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The single-command STT wrapper must call the chunked entry point."""
    from screenscribe.cli_messages import _transcribe_audio_or_exit

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")

    seen: dict[str, Any] = {}

    def _fake_chunked(path: Path, **kwargs: Any) -> TranscriptionResult:
        seen["path"] = path
        seen["kwargs"] = kwargs
        return _result([Segment(0, 0.0, 1.0, "routed")], text="routed")

    monkeypatch.setattr("screenscribe.cli.transcribe_audio_chunked", _fake_chunked)

    result = _transcribe_audio_or_exit(
        audio,
        language="en",
        use_local=False,
        api_key="k",
        stt_endpoint=None,
        stt_model="whisper-1",
    )

    assert seen["path"] == audio
    assert result.text == "routed"
