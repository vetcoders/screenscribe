"""Unit tests for deterministic publish_manifest.v1 bundles, path scrubbing, and streaming multipart."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from screenscribe.publish_bundle import (
    CHUNK_SIZE,
    LOCAL_PATH_MARKER,
    RECEIPT_FILENAME,
    FormField,
    MultipartBody,
    PathScrubber,
    PublishBundleError,
    VideoFile,
    build_publish_bundle,
    job_idempotency_key,
    resolve_review_source,
    validate_archive_path,
    validate_visibility,
    write_publish_receipt,
)


def _fake_mac_home(user: str, rest: str) -> str:
    return "/" + "Users" + "/" + user + "/" + rest


def _fake_linux_home(user: str, rest: str) -> str:
    return "/" + "home" + "/" + user + "/" + rest


def _fake_win_home(user: str, rest: str) -> str:
    return "C:\\" + "Users" + "\\" + user + "\\" + rest


def _extract_text(tf: tarfile.TarFile, name: str) -> str:
    member = tf.extractfile(name)
    assert member is not None
    return member.read().decode("utf-8")


def test_deterministic_archive_allowlist_sanitization_and_symlinks(tmp_path: Path) -> None:
    review_dir = tmp_path / "demo_review"
    review_dir.mkdir()
    shots = review_dir / "screenshots"
    shots.mkdir()
    manual = review_dir / "manual_frames"
    manual.mkdir()
    cache = review_dir / ".screenscribe_cache"
    cache.mkdir()
    (cache / "secret.bin").write_bytes(b"never-pack")

    shot_file = shots / "001.jpg"
    shot_file.write_bytes(b"\xff\xd8\xff\xd9")
    manual_file = manual / "m1.png"
    manual_file.write_bytes(b"\x89PNG\r\n\x1a\n")

    # Symlink inside screenshots is skipped safely and recorded in bundle.skipped
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    (shots / "sym.jpg").symlink_to(outside)

    # Extra files that must NEVER enter the archive allowlist
    (review_dir / "demo_report.html").write_text("<html>local</html>", encoding="utf-8")
    (review_dir / RECEIPT_FILENAME).write_text("{}", encoding="utf-8")
    (review_dir / ".DS_Store").write_bytes(b"junk")

    mac_leak = _fake_mac_home("alice", "secret_repo/demo.mp4")
    linux_leak = _fake_linux_home("bob", "notes/spec.md")
    win_leak = _fake_win_home("carol", "Videos\\clip.mov")

    report = {
        "video": mac_leak,
        "generated_at": "2026-06-20T10:00:00Z",
        "executive_summary": f"Checked {linux_leak} and {win_leak}.",
        "frameDataUrl": "data:image/png;base64,AAAA",
        "human_review": {
            "reviewer": "Maciej",
            "findings": {"1": {"verdict": "confirmed", "frameDataUrl": "data:image/png;base64,BBBB"}},
        },
        "findings": [
            {
                "id": 1,
                "category": "bug",
                "timestamp_start": 1.0,
                "timestamp_end": 2.0,
                "text": f"Path {review_dir / 'screenshots/001.jpg'} failed",
                "screenshot": "001.jpg",
            }
        ],
    }
    (review_dir / "demo_report.json").write_text(json.dumps(report), encoding="utf-8")
    (review_dir / "demo_report.md").write_text(f"# Notes from {mac_leak}\n", encoding="utf-8")
    (review_dir / "demo_transcript.txt").write_text(
        f"Spoken reference to {linux_leak}\n", encoding="utf-8"
    )

    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"mp4-binary-stream")

    src = resolve_review_source(review_dir, include_video=True)
    assert src.video == video_path.resolve() or src.video == video_path

    with (
        build_publish_bundle(src, visibility="unlisted") as b1,
        build_publish_bundle(src, visibility="unlisted") as b2,
    ):
        b1.archive.seek(0)
        b2.archive.seek(0)
        raw1 = b1.archive.read()
        raw2 = b2.archive.read()
        # Byte-for-byte deterministic .tar.gz and identical Idempotency-Key!
        assert raw1 == raw2
        assert b1.sha256 == b2.sha256
        assert b1.idempotency_key == b2.idempotency_key
        assert any("sym.jpg" in item for item in b1.skipped)

        b1.archive.seek(0)
        with tarfile.open(fileobj=b1.archive, mode="r:gz") as tf:
            names = tf.getnames()
            assert names == sorted(names)
            assert "demo_report.html" not in names
            assert RECEIPT_FILENAME not in names
            assert not any(".screenscribe_cache" in name for name in names)
            assert "screenshots/sym.jpg" not in names
            assert "review_state.json" in names

            packed_report = json.loads(_extract_text(tf, "demo_report.json"))
            packed_md = _extract_text(tf, "demo_report.md")
            packed_txt = _extract_text(tf, "demo_transcript.txt")

        assert packed_report["video"] == "demo.mp4"
        assert "frameDataUrl" not in json.dumps(packed_report)
        assert "alice" not in json.dumps(packed_report)
        assert "bob" not in json.dumps(packed_report)
        assert "carol" not in json.dumps(packed_report)
        assert LOCAL_PATH_MARKER in packed_report["executive_summary"]
        assert "alice" not in packed_md
        assert "bob" not in packed_txt

    # Symlinked review directory itself is rejected
    sym_dir = tmp_path / "sym_review"
    sym_dir.symlink_to(review_dir)
    with pytest.raises(PublishBundleError):
        resolve_review_source(sym_dir)


def test_validation_helpers_receipt_atomic_write_and_large_sparse_multipart(
    tmp_path: Path,
) -> None:
    assert validate_visibility("UNLISTED") == "unlisted"
    with pytest.raises(PublishBundleError):
        validate_visibility("public")

    for unsafe in ("", "/abs/path", "../up", "a/../../b", "back\\slash", "nul\x00byte"):
        with pytest.raises(PublishBundleError):
            validate_archive_path(unsafe)

    key_a = job_idempotency_key(
        video_sha256="a" * 64, visibility="private", video_filename="v.mp4", language="en"
    )
    key_b = job_idempotency_key(
        video_sha256="a" * 64, visibility="private", video_filename="v.mp4", language="pl"
    )
    assert key_a != key_b

    scrubber = PathScrubber({}, ())
    assert scrubber.scrub({"a": 1, "frameDataUrl": "drop"}) == {"a": 1}

    # Atomic receipt write + refusal to overwrite symlink receipt
    rdir = tmp_path / "receipt_dir"
    rdir.mkdir()
    written = write_publish_receipt(rdir, {"share_id": "shr_1", "ok": True})
    assert json.loads(written.read_text(encoding="utf-8"))["share_id"] == "shr_1"

    written.unlink()
    other = tmp_path / "target.json"
    other.write_text("{}", encoding="utf-8")
    written.symlink_to(other)
    with pytest.raises(PublishBundleError):
        write_publish_receipt(rdir, {"share_id": "shr_2"})

    # >55 MiB sparse recording streamed in bounded <= 1 MiB chunks with exact Content-Length
    sparse_video = tmp_path / "large_recording.mp4"
    size_bytes = 56 * 1024 * 1024 + 321
    with sparse_video.open("wb") as handle:
        handle.seek(size_bytes - 4)
        handle.write(b"END!")

    vf = VideoFile.from_path(sparse_video)
    assert vf.size_bytes == size_bytes
    mp = MultipartBody(
        [FormField("visibility", "private")],
        [vf.part("video")],
        boundary="fixed-boundary-123",
    )
    declared_len = mp.content_length
    emitted_len = 0
    max_chunk = 0
    for chunk in mp.iter_chunks():
        emitted_len += len(chunk)
        max_chunk = max(max_chunk, len(chunk))

    assert emitted_len == declared_len
    assert max_chunk <= CHUNK_SIZE
