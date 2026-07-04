"""Atomic checkpoint write tests (C6.1).

Proves that save_checkpoint() writes via a temp file + os.replace so a crash
mid-write can never corrupt checkpoint.json or destroy the previous good state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import screenscribe.checkpoint as cp
from screenscribe.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    PipelineCheckpoint,
    get_checkpoint_dir,
    get_checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)


def _checkpoint(stages: list[str] | None = None, video_hash: str = "hashv1") -> PipelineCheckpoint:
    return PipelineCheckpoint(
        video_path="video.mp4",
        video_hash=video_hash,
        output_dir="out",
        language="en",
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        completed_stages=list(stages or []),
    )


def test_roundtrip(tmp_path: Path) -> None:
    """A5: save -> load returns an equivalent checkpoint."""
    ckpt = _checkpoint(stages=["audio", "transcription"], video_hash="abc123")

    save_checkpoint(ckpt, tmp_path)
    restored = load_checkpoint(tmp_path)

    assert restored is not None
    assert restored.completed_stages == ["audio", "transcription"]
    assert restored.video_hash == "abc123"
    assert restored.schema_version == CHECKPOINT_SCHEMA_VERSION


def test_no_leftover_tmp(tmp_path: Path) -> None:
    """A6: happy-path leaves exactly checkpoint.json, no *.tmp file."""
    save_checkpoint(_checkpoint(stages=["audio"]), tmp_path)

    cache_dir = get_checkpoint_dir(tmp_path)
    entries = sorted(p.name for p in cache_dir.iterdir())
    assert entries == ["checkpoint.json"]


def test_tmp_lives_in_checkpoint_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: the temp file used for the atomic write lives in the SAME dir as the
    final checkpoint (so os.replace is a same-filesystem atomic rename), not in
    the system /tmp."""
    captured: dict[str, Path] = {}
    real_replace = cp.os.replace

    def spy_replace(src, dst):  # type: ignore[no-untyped-def]
        captured["src"] = Path(src)
        captured["dst"] = Path(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(cp.os, "replace", spy_replace)
    save_checkpoint(_checkpoint(stages=["audio"]), tmp_path)

    cache_dir = get_checkpoint_dir(tmp_path)
    assert captured["src"].parent == cache_dir
    assert captured["dst"] == get_checkpoint_path(tmp_path)


def test_crash_mid_write_preserves_previous_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4: a crash mid-write (json.dump raises after partially writing the temp
    file) leaves the PREVIOUS good checkpoint intact — load returns it, not None
    and not a corrupt blob."""
    # First, write a good checkpoint.
    good = _checkpoint(stages=["audio", "transcription"], video_hash="good")
    save_checkpoint(good, tmp_path)

    checkpoint_path = get_checkpoint_path(tmp_path)
    original_bytes = checkpoint_path.read_bytes()

    # Now simulate a crash during the second save: json.dump writes some bytes
    # to the temp file, then raises before completion / before os.replace.
    real_dump = json.dump

    def crashing_dump(obj, fp, **kwargs):  # type: ignore[no-untyped-def]
        fp.write('{"partial": "garbage')  # half-written, invalid JSON
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(cp.json, "dump", crashing_dump)

    second = _checkpoint(stages=["audio", "transcription", "detection"], video_hash="newer")
    with pytest.raises(RuntimeError, match="simulated crash"):
        save_checkpoint(second, tmp_path)

    # restore real dump for the assertions / load path
    monkeypatch.setattr(cp.json, "dump", real_dump)

    # The final checkpoint.json is byte-for-byte the previous good version.
    assert checkpoint_path.read_bytes() == original_bytes

    restored = load_checkpoint(tmp_path)
    assert restored is not None
    assert restored.completed_stages == ["audio", "transcription"]
    assert restored.video_hash == "good"

    # No orphan temp file left behind by the crash.
    cache_dir = get_checkpoint_dir(tmp_path)
    leftover = [p.name for p in cache_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftover == []
