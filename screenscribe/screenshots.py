"""Screenshot extraction from video at specific timestamps."""

import subprocess
from pathlib import Path

from rich.console import Console

from .detect import Detection, format_timestamp

console = Console()

# Seconds to back off below an EOF/segment-end bound so ``ffmpeg -ss`` lands on a
# real frame instead of seeking exactly to EOF (which yields no frame).
_TAIL_FRAME_MARGIN = 0.1


def _select_capture_timestamp(
    detection: Detection, offset: float = 0.5, video_duration: float | None = None
) -> float:
    """Select screenshot timestamp for a detection.

    Uses start+offset — the screen the user was LOOKING AT when they began
    describing the issue. Midpoint is too late: by then the user may have
    already navigated to the next screen while still talking about the previous
    one. The result is clamped to >= 0.

    For findings at the very end of the recording, start+offset can land past the
    segment end (and thus past EOF), making ffmpeg's ``-ss`` fail so the frame is
    dropped and visual analysis is lost. Clamp to the segment end (for ranged
    segments) and to the known video duration when available, so a tail finding
    still yields a frame instead of seeking past EOF.
    """
    segment = detection.segment
    candidate = segment.start + offset

    upper_bounds: list[float] = []
    if segment.end > segment.start:
        upper_bounds.append(segment.end)
    if video_duration is not None and video_duration > 0:
        upper_bounds.append(video_duration)
    if upper_bounds:
        bound = min(upper_bounds)
        # ``ffmpeg -ss <t> -vframes 1`` yields NO frame when ``t`` lands exactly
        # at (or past) EOF, so clamping to the bound itself still loses the tail
        # frame. Back off a frame-safe margin below the bound so a finding at the
        # very end of the recording still captures a real frame.
        if candidate >= bound:
            candidate = bound - _TAIL_FRAME_MARGIN

    return max(0.0, candidate)


def extract_screenshot(video_path: Path, timestamp: float, output_path: Path) -> Path:
    """
    Extract a single screenshot from video at timestamp.

    Args:
        video_path: Path to video file
        timestamp: Time in seconds
        output_path: Where to save the screenshot

    Returns:
        Path to saved screenshot
    """
    cmd = [
        "ffmpeg",
        "-ss",
        str(timestamp),
        "-i",
        str(video_path),
        "-vframes",
        "1",
        "-q:v",
        "2",  # High quality JPEG
        "-y",
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg screenshot failed: {result.stderr}")

    return output_path


def extract_screenshots_for_detections(
    video_path: Path, detections: list[Detection], output_dir: Path, offset: float = 0.5
) -> list[tuple[Detection, Path]]:
    """
    Extract screenshots for all detections.

    Args:
        video_path: Path to video file
        detections: List of detections
        output_dir: Directory to save screenshots
        offset: Seconds after start to capture (default: 0.5s into segment)

    Returns:
        List of (Detection, screenshot_path) tuples
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Probe the video length once so tail detections can be clamped within EOF.
    # An unprobeable/missing video falls back to None (no extra clamp), keeping
    # the segment-end clamp as the only bound.
    video_duration: float | None
    try:
        from . import audio

        video_duration = audio.get_video_duration(video_path)
    except (OSError, RuntimeError, ValueError):
        video_duration = None

    results = []
    console.print(f"[blue]Extracting {len(detections)} screenshots...[/]")

    for i, detection in enumerate(detections, 1):
        timestamp = _select_capture_timestamp(
            detection, offset=offset, video_duration=video_duration
        )

        # Generate filename
        ts_str = format_timestamp(timestamp).replace(":", "-")
        filename = f"{i:02d}_{detection.category}_{ts_str}.jpg"
        output_path = output_dir / filename

        try:
            extract_screenshot(video_path, timestamp, output_path)
            results.append((detection, output_path))
            console.print(f"  [green]✓[/] {filename} [dim]({format_timestamp(timestamp)})[/]")
        except RuntimeError as e:
            console.print(f"  [red]✗[/] Failed: {e}")

    console.print(f"[green]Extracted {len(results)} screenshots[/]")
    return results


def extract_keyframes_around_detection(
    video_path: Path,
    detection: Detection,
    output_dir: Path,
    num_frames: int = 3,
    interval: float = 2.0,
) -> list[Path]:
    """
    Extract multiple keyframes around a detection for context.

    Args:
        video_path: Path to video file
        detection: The detection
        output_dir: Directory to save screenshots
        num_frames: Number of frames to extract
        interval: Seconds between frames

    Returns:
        List of screenshot paths
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Calculate timestamps centered on detection
    center = (detection.segment.start + detection.segment.end) / 2
    start_offset = -((num_frames - 1) / 2) * interval

    paths = []
    for i in range(num_frames):
        timestamp = max(0, center + start_offset + (i * interval))
        ts_str = format_timestamp(timestamp).replace(":", "-")
        # Prefix the frame index so distinct frames never collide on filename.
        # format_timestamp resolves only to whole seconds (MM-SS), so near t=0
        # (or interval<1s) multiple frames clamp/round to the same second and,
        # without the index, would overwrite each other on disk — fewer real
        # images than reported paths, with duplicate paths returned (BH8/BH35).
        filename = f"keyframe_{i:02d}_{ts_str}.jpg"
        output_path = output_dir / filename

        try:
            extract_screenshot(video_path, timestamp, output_path)
            paths.append(output_path)
        except RuntimeError:
            pass  # Skip failed frames

    # Surface a high keyframe-failure ratio instead of silently returning a
    # short/empty list. Callers otherwise cannot tell "no frames requested"
    # apart from "every frame failed" (P3-7).
    if num_frames > 0:
        failed = num_frames - len(paths)
        if not paths:
            console.print(f"  [red]✗[/] All {num_frames} keyframes failed to extract")
        elif failed * 2 > num_frames:  # more than half failed
            console.print(f"  [yellow]![/] {failed}/{num_frames} keyframes failed to extract")

    return paths
