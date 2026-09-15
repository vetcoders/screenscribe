"""Frame-interval VLM-OCR: extraction grid, dedupe, cache, request and segment shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import screenscribe.frame_ocr as frame_ocr
from screenscribe.config import ScreenScribeConfig


def _ocr_config(**overrides: object) -> ScreenScribeConfig:
    cfg = ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_endpoint="https://vision.example/v1/responses",
        vision_model="grok-test",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _responses_payload(text: str, response_id: str = "resp-1") -> dict[str, object]:
    return {
        "id": response_id,
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }


def test_extract_interval_frames_grid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Frames land on the interval grid and reuse the shared ffmpeg helper."""
    monkeypatch.setattr(frame_ocr, "get_video_duration", lambda _p: 12.0)
    calls: list[tuple[Path, float]] = []

    def fake_extract(video: Path, timestamp: float, out: Path) -> Path:
        out.write_bytes(b"jpeg")
        calls.append((video, timestamp))
        return out

    monkeypatch.setattr(frame_ocr, "extract_screenshot", fake_extract)

    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    frames = frame_ocr.extract_interval_frames(video, 5.0, tmp_path / "frames")

    assert [t for t, _ in frames] == [0.0, 5.0, 10.0]
    assert all(path.exists() for _, path in frames)
    assert [t for _, t in calls] == [0.0, 5.0, 10.0]


def test_extract_interval_frames_rejects_nonpositive_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        frame_ocr.extract_interval_frames(tmp_path / "clip.mov", 0.0, tmp_path / "out")


def test_dedupe_frames_drops_consecutive_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A static run of identical frames collapses to its first frame only."""
    hashes = {"a.jpg": "h1", "b.jpg": "h1", "c.jpg": "h2", "d.jpg": "h2", "e.jpg": "h1"}
    monkeypatch.setattr(frame_ocr, "_frame_probe_hash", lambda p: hashes[p.name])

    frames = [(float(i), tmp_path / name) for i, name in enumerate(hashes)]
    kept = frame_ocr.dedupe_frames(frames)

    assert [p.name for _, p in kept] == ["a.jpg", "c.jpg", "e.jpg"]


def test_ocr_frame_request_shape_and_prompt_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The OCR call hits the vision endpoint with an image payload, and the
    operator's --prompt override is appended to the OCR prompt."""
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-bytes")
    captured: dict[str, object] = {}

    def fake_post(endpoint: str, api_key: str, payload: dict[str, object]) -> dict[str, object]:
        captured["endpoint"] = endpoint
        captured["api_key"] = api_key
        captured["payload"] = payload
        return _responses_payload("Save button\nError: quota exceeded")

    monkeypatch.setattr(frame_ocr, "_post_ocr_request", fake_post)
    cfg = _ocr_config(analysis_prompt_override="Nagranie bez audio; opisy w tekscie")

    text, response_id = frame_ocr.ocr_frame(frame, cfg, cache_dir=tmp_path / "cache")

    assert text == "Save button\nError: quota exceeded"
    assert response_id == "resp-1"
    assert captured["endpoint"] == "https://vision.example/v1/responses"
    assert captured["api_key"] == "test-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "grok-test"
    content = payload["input"][0]["content"]  # type: ignore[index]
    types = {part["type"] for part in content}
    assert types == {"input_text", "input_image"}
    prompt_text = next(part["text"] for part in content if part["type"] == "input_text")
    assert "Nagranie bez audio; opisy w tekscie" in prompt_text
    assert "Transcribe ALL text visible" in prompt_text


def test_ocr_frame_uses_cache_without_http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cached frame never reaches the transport, even across calls."""
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-bytes")
    cfg = _ocr_config()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_key = frame_ocr._ocr_cache_key(frame, cfg.vision_model)
    (cache_dir / f"{cache_key}.json").write_text(
        json.dumps({"text": "cached text", "response_id": "cached-id"}), encoding="utf-8"
    )

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("transport should not run for a cached frame")

    monkeypatch.setattr(frame_ocr, "_post_ocr_request", fail_if_called)

    assert frame_ocr.ocr_frame(frame, cfg, cache_dir=cache_dir) == ("cached text", "cached-id")


def test_ocr_frame_requires_vision_key(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-bytes")
    cfg = _ocr_config(api_key="", vision_api_key="")
    with pytest.raises(ValueError, match="Vision API key required"):
        frame_ocr.ocr_frame(frame, cfg, cache_dir=tmp_path / "cache")


def test_transcribe_video_ocr_segment_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """OCR segments carry the exact STT shape with frame-grid timestamps:
    start/end from the interval grid (end clamped to duration), empty OCR
    results skipped, dedupe applied before any OCR call."""
    monkeypatch.setattr(frame_ocr, "get_video_duration", lambda _p: 12.0)

    def fake_extract(video: Path, timestamp: float, out: Path) -> Path:
        out.write_bytes(f"jpeg-{timestamp}".encode())
        return out

    monkeypatch.setattr(frame_ocr, "extract_screenshot", fake_extract)
    # Frames at 0s and 5s are identical (static screen) -> 5s is deduped away.
    probe_hashes = {0.0: "h1", 5.0: "h1", 10.0: "h2"}
    monkeypatch.setattr(
        frame_ocr,
        "_frame_probe_hash",
        lambda p: probe_hashes[float(p.stem.split("_")[-1].removesuffix("s"))],
    )
    ocr_calls: list[float] = []

    def fake_ocr(frame_path: Path, config: ScreenScribeConfig, *, cache_dir: Path | None = None):
        timestamp = float(frame_path.stem.split("_")[-1].removesuffix("s"))
        ocr_calls.append(timestamp)
        return {0.0: ("Przycisk Zapisz", "resp-0"), 10.0: ("Blad: quota", "resp-10")}[timestamp]

    monkeypatch.setattr(frame_ocr, "ocr_frame", fake_ocr)

    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    result = frame_ocr.transcribe_video_ocr(
        video,
        _ocr_config(),
        frame_interval=5.0,
        cache_dir=tmp_path / "cache",
        work_dir=tmp_path / "frames",
    )

    assert ocr_calls == [0.0, 10.0]  # dedupe happened before OCR
    assert [(s.id, s.start, s.end, s.text) for s in result.segments] == [
        (0, 0.0, 5.0, "Przycisk Zapisz"),
        (1, 10.0, 12.0, "Blad: quota"),  # end clamped to duration
    ]
    assert result.text == "Przycisk Zapisz\nBlad: quota"
    assert result.response_id == "resp-10"
    # The STT consumer contract: plain float timestamps and text on .segments.
    assert all(isinstance(s.start, float) and isinstance(s.end, float) for s in result.segments)


def test_transcribe_video_ocr_skips_empty_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Frames with no readable text produce no segment (but still chain id)."""
    monkeypatch.setattr(frame_ocr, "get_video_duration", lambda _p: 7.0)
    monkeypatch.setattr(
        frame_ocr,
        "extract_screenshot",
        lambda video, timestamp, out: out.write_bytes(b"jpeg") or out,
    )
    monkeypatch.setattr(frame_ocr, "_frame_probe_hash", lambda p: p.name)
    monkeypatch.setattr(
        frame_ocr,
        "ocr_frame",
        lambda frame_path, config, *, cache_dir=None: ("", "resp-x"),
    )

    video = tmp_path / "clip.mov"
    video.write_bytes(b"video")
    result = frame_ocr.transcribe_video_ocr(
        video,
        _ocr_config(),
        frame_interval=5.0,
        cache_dir=tmp_path / "cache",
        work_dir=tmp_path / "frames",
    )

    assert result.segments == []
    assert result.text == ""
    assert result.response_id == "resp-x"
