"""Frame-interval VLM-OCR transcript source.

Extracts frames every N seconds, drops visually identical frames (md5 of a
downscaled grayscale probe), sends the survivors to the configured vision
model through the Responses API, and shapes the results into the same
``TranscriptionResult``/``Segment`` structure STT produces, so the rest of
the review pipeline (semantic prefilter, response chaining, reports) works
unchanged on OCR segments.

The request mechanics mirror the unified vision path
(``unified.wire._build_unified_payload`` + bearer + ``retry_request``);
the OCR text cache is per frame content hash so re-runs and --resume never
re-pay for a frame that was already read.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404 - ffmpeg/ffprobe invocation, no shell
import tempfile
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from .api_utils import extract_response_payload_error, retry_request
from .audio import get_video_duration
from .config import ScreenScribeConfig
from .prompts import apply_analysis_prompt_override
from .screenshots import extract_screenshot
from .transcribe_types import Segment, TranscriptionResult
from .unified.response_parsing import extract_response_content
from .unified.wire import _build_unified_payload

console = Console()

DEFAULT_FRAME_INTERVAL_SECONDS = 5.0

# The vision model is asked for a verbatim transcription only; analysis stays
# with the downstream semantic/vision passes that consume the segments.
OCR_FRAME_PROMPT = """Transcribe ALL text visible on this screen recording frame, verbatim.

Rules:
- Output only the text you can actually read on the frame, in reading order.
- Keep UI labels, button captions, error messages, code and terminal output as-is.
- Do not describe the image, do not summarize, do not add commentary.
- If there is no readable text on the frame, output nothing (empty response)."""


def default_ocr_cache_dir() -> Path:
    """Process-local temp cache for OCR results (keyed by frame content hash)."""
    return Path(tempfile.gettempdir()) / "screenscribe-ocr"


def extract_interval_frames(
    video_path: Path,
    interval_seconds: float,
    output_dir: Path,
    *,
    duration: float | None = None,
) -> list[tuple[float, Path]]:
    """Extract one frame every ``interval_seconds`` via the shared ffmpeg helper.

    Returns ``(timestamp, frame_path)`` pairs. Frame extraction failures are
    skipped (a missing frame only means a gap in OCR coverage, never a crash).
    """
    if interval_seconds <= 0:
        raise ValueError("frame interval must be positive")
    if duration is None:
        duration = get_video_duration(video_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    frames: list[tuple[float, Path]] = []
    timestamp = 0.0
    index = 0
    while timestamp < duration:
        frame_path = output_dir / f"frame_{index:05d}_{timestamp:.1f}s.jpg"
        try:
            extract_screenshot(video_path, timestamp, frame_path)
            frames.append((timestamp, frame_path))
        except RuntimeError as e:
            console.print(f"  [red]✗[/] Frame at {timestamp:.1f}s failed: {e}")
        timestamp += interval_seconds
        index += 1
    return frames


def _frame_probe_hash(frame_path: Path) -> str:
    """md5 of a downscaled grayscale probe — cheap perceptual-ish dedupe key.

    Decoding through ffmpeg to raw gray pixels (not comparing JPEG bytes)
    makes the hash insensitive to encoder noise while still collapsing
    consecutive frames of a static screen into one.
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(frame_path),
        "-vf",
        "scale=160:-2",
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-v",
        "error",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)  # nosec B603 - fixed argv, no shell
    if result.returncode != 0 or not result.stdout:
        # Unprobeable frame: give it a unique hash so it is never deduplicated
        # away (losing a frame to a broken hash would lose OCR coverage).
        return hashlib.md5(frame_path.read_bytes(), usedforsecurity=False).hexdigest()
    return hashlib.md5(result.stdout, usedforsecurity=False).hexdigest()


def dedupe_frames(frames: list[tuple[float, Path]]) -> list[tuple[float, Path]]:
    """Drop frames identical to the previous kept frame (consecutive dedupe)."""
    kept: list[tuple[float, Path]] = []
    previous_hash: str | None = None
    for timestamp, frame_path in frames:
        probe = _frame_probe_hash(frame_path)
        if probe == previous_hash:
            continue
        previous_hash = probe
        kept.append((timestamp, frame_path))
    return kept


def _ocr_cache_key(frame_path: Path, model: str) -> str:
    digest = hashlib.sha256(frame_path.read_bytes()).hexdigest()
    return hashlib.sha256(f"{model}:{digest}".encode()).hexdigest()


def _post_ocr_request(endpoint: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Transport for one OCR request (the patch seam for tests)."""
    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def ocr_frame(
    frame_path: Path,
    config: ScreenScribeConfig,
    *,
    cache_dir: Path | None = None,
) -> tuple[str, str]:
    """OCR one frame through the vision endpoint. Returns ``(text, response_id)``.

    Results are cached by frame content hash + model so identical frames
    never hit the API twice, even across runs.
    """
    api_key = config.get_vision_api_key()
    if not api_key:
        raise ValueError(
            "Vision API key required for OCR transcript source. "
            "Run `screenscribe auth login` or set SCREENSCRIBE_VISION_API_KEY."
        )

    cache_dir = cache_dir or default_ocr_cache_dir()
    cache_key = _ocr_cache_key(frame_path, config.vision_model)
    cache_path = cache_dir / f"{cache_key}.json"
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return str(cached.get("text", "")), str(cached.get("response_id", ""))
        except (OSError, ValueError):
            pass  # Corrupt cache entry: fall through and re-request.

    prompt = apply_analysis_prompt_override(OCR_FRAME_PROMPT, config.analysis_prompt_override)
    payload = _build_unified_payload(
        endpoint=config.vision_endpoint,
        model=config.vision_model,
        prompt=prompt,
        screenshot_path=frame_path,
        previous_response_id=None,
        stream=False,
    )

    result = retry_request(
        lambda: _post_ocr_request(config.vision_endpoint, api_key, payload),
        max_retries=3,
        operation_name="Frame OCR",
    )
    response_error = extract_response_payload_error(result)
    if response_error is not None:
        raise response_error

    text = extract_response_content(result, endpoint=config.vision_endpoint).strip()
    response_id = str(result.get("id", ""))

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"text": text, "response_id": response_id}), encoding="utf-8"
        )
    except OSError:
        pass  # Cache is an optimization; never fail the OCR over it.

    return text, response_id


def transcribe_video_ocr(
    video_path: Path,
    config: ScreenScribeConfig,
    *,
    frame_interval: float = DEFAULT_FRAME_INTERVAL_SECONDS,
    cache_dir: Path | None = None,
    work_dir: Path | None = None,
) -> TranscriptionResult:
    """Build a transcript from OCR'd frames in the exact STT segment shape.

    Each surviving (non-duplicate) frame with readable text becomes one
    segment spanning ``[t, t + frame_interval)`` clamped to the video
    duration, so timestamps come from the frame grid, not from the model.
    """
    duration = get_video_duration(video_path)
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="screenscribe-ocr-frames-"))

    console.print(f"[blue]OCR transcript:[/] frames every {frame_interval:g}s over {duration:.0f}s")
    frames = extract_interval_frames(video_path, frame_interval, work_dir, duration=duration)
    kept = dedupe_frames(frames)
    if len(kept) < len(frames):
        console.print(f"[dim]  Frame dedup: {len(frames)} → {len(kept)}[/]")

    segments: list[Segment] = []
    last_response_id = ""
    for timestamp, frame_path in kept:
        text, response_id = ocr_frame(frame_path, config, cache_dir=cache_dir)
        if response_id:
            last_response_id = response_id
        if not text:
            continue
        segments.append(
            Segment(
                id=len(segments),
                start=timestamp,
                end=min(timestamp + frame_interval, duration),
                text=text,
            )
        )

    full_text = "\n".join(segment.text for segment in segments)
    console.print(
        f"[green]OCR transcript:[/] {len(segments)} segment(s) from {len(kept)} unique frame(s)"
    )
    return TranscriptionResult(
        text=full_text,
        segments=segments,
        language=config.language,
        response_id=last_response_id,
    )
