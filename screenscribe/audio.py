"""Audio extraction from video files using FFmpeg."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Console

console = Console()

# ffprobe metadata reads are near-instant; a minute is already generous and only
# trips when the tool wedges on a corrupt container or an unresponsive mount.
_FFPROBE_TIMEOUT_SECONDS = 60.0

# Full-decode ffmpeg passes (transcode, silence/volume analysis, chunk split) can
# legitimately run for a while on long recordings, so this ceiling is a safety
# net against an infinite hang, not a tight performance bound.
_FFMPEG_TIMEOUT_SECONDS = 3600.0


def _run_media_command(
    cmd: list[str],
    *,
    timeout: float,
    tool: str,
) -> "subprocess.CompletedProcess[str]":
    """Run an ffmpeg/ffprobe command with a hard timeout.

    A malformed container, a non-terminating stream, or a stalled network mount
    can make ffmpeg/ffprobe block forever. Without a timeout the whole CLI hangs
    with no recovery and no message. Convert the timeout into a readable
    RuntimeError instead of letting the process wedge indefinitely.
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{tool} timed out after {timeout:.0f}s. The input may be corrupt, a "
            "non-terminating stream, or on an unresponsive mount."
        ) from exc


class FFmpegNotFoundError(Exception):
    """Raised when FFmpeg is not installed."""

    pass


class MediaDecodeError(RuntimeError):
    """Raised when FFmpeg cannot decode the provided media file."""

    pass


class MissingAudioStreamError(MediaDecodeError):
    """Raised when input media has no audio stream to extract."""

    pass


def _raise_ffmpeg_error(input_path: Path, stderr: str) -> None:
    """Raise a more helpful decode error for invalid or corrupted media files."""
    cleaned_stderr = stderr.strip()
    lowered = cleaned_stderr.lower()
    invalid_media_markers = (
        "moov atom not found",
        "invalid data found when processing input",
        "error opening input",
        "error opening input files",
        "could not find codec parameters",
    )
    if any(marker in lowered for marker in invalid_media_markers):
        raise MediaDecodeError(
            f"Could not decode media file '{input_path.name}'. "
            "The file does not look like a valid video/audio recording or is corrupted."
        )
    raise RuntimeError(f"FFmpeg failed for {input_path.name}: {cleaned_stderr}")


def _transcode_input_to_mp3(input_path: Path, output_path: Path) -> Path:
    """Normalize audio or video input into a speech-friendly MP3 for STT."""
    cmd = [
        "ffmpeg",
        "-i",
        str(input_path),
        "-vn",  # No video
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",  # High quality
        "-ar",
        "16000",  # 16kHz for speech recognition
        "-ac",
        "1",  # Mono
        "-y",  # Overwrite
        str(output_path),
    ]

    result = _run_media_command(cmd, timeout=_FFMPEG_TIMEOUT_SECONDS, tool="ffmpeg")
    if result.returncode != 0:
        _raise_ffmpeg_error(input_path, result.stderr)
    return output_path


def check_ffmpeg_installed() -> None:
    """
    Check if FFmpeg and FFprobe are installed and accessible.

    Raises:
        FFmpegNotFoundError: If FFmpeg or FFprobe is not found
    """
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")

    missing = []
    if not ffmpeg_path:
        missing.append("ffmpeg")
    if not ffprobe_path:
        missing.append("ffprobe")

    if missing:
        # Detect platform for install instructions
        if sys.platform == "darwin":
            install_cmd = "brew install ffmpeg"
        elif sys.platform == "win32":
            install_cmd = "choco install ffmpeg"
        else:
            install_cmd = "sudo apt install ffmpeg"

        raise FFmpegNotFoundError(
            f"Required tools not found: {', '.join(missing)}\n\n"
            f"Install FFmpeg:\n  {install_cmd}\n\n"
            f"Then try again."
        )


def extract_audio(video_path: Path, output_path: Path | None = None) -> Path:
    """
    Extract audio from video file using FFmpeg.

    Args:
        video_path: Path to input video file
        output_path: Optional output path. If None, creates temp file.

    Returns:
        Path to extracted audio file (MP3 format for API compatibility)
    """
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    require_audio_stream(video_path)

    created_temp = False
    if output_path is None:
        # A fixed ``screenscribe_<stem>.mp3`` name let two concurrent runs of
        # files with the same stem clobber each other's audio (ffmpeg -y) and
        # leaked a predictable temp file. mkstemp mints a unique, atomically
        # created path per call, matching the split_audio_chunks fix (M4).
        fd, temp_name = tempfile.mkstemp(prefix=f"screenscribe_{video_path.stem}_", suffix=".mp3")
        os.close(fd)
        output_path = Path(temp_name)
        created_temp = True

    console.print(f"[blue]Extracting audio from:[/] {video_path.name}")
    try:
        _transcode_input_to_mp3(video_path, output_path)
    except BaseException:
        # Do not leak the placeholder we created when the transcode fails or is
        # interrupted. A caller-supplied output_path is left untouched.
        if created_temp:
            output_path.unlink(missing_ok=True)
        raise

    console.print(f"[green]Audio extracted:[/] {output_path}")
    return output_path


def normalize_audio_for_stt(audio_path: Path, output_path: Path | None = None) -> Path:
    """Convert arbitrary browser-uploaded audio into a stable MP3 for STT fallback."""
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    if output_path is None:
        temp_dir = Path(tempfile.gettempdir())
        output_path = temp_dir / f"{audio_path.stem}_normalized.mp3"

    console.print(f"[blue]Normalizing audio for STT:[/] {audio_path.name}")
    _transcode_input_to_mp3(audio_path, output_path)
    console.print(f"[green]Audio normalized:[/] {output_path}")
    return output_path


def has_audio_stream(video_path: Path) -> bool:
    """Return True if the media file has at least one audio stream.

    Returns True optimistically when ffprobe cannot inspect the file, so the
    canonical decode error from ffmpeg surfaces downstream instead of a
    misleading "no audio" message for genuinely corrupt media.
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    result = _run_media_command(cmd, timeout=_FFPROBE_TIMEOUT_SECONDS, tool="ffprobe")
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def require_audio_stream(video_path: Path) -> None:
    """Raise a clean user-facing error when media has no audio stream."""
    if not has_audio_stream(video_path):
        raise MissingAudioStreamError(
            f"Video '{video_path.name}' has no audio track. "
            "The 'review' and 'transcribe' commands require audio for transcription. "
            "Tip: run:\n"
            "  screenscribe analyze <video-path>\n"
            "Video path:\n"
            f"  {video_path}\n"
            "Then mark frames manually and add optional text/voice notes for "
            "interactive vision-only review."
        )


def get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds using FFprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]

    result = _run_media_command(cmd, timeout=_FFPROBE_TIMEOUT_SECONDS, tool="ffprobe")

    if result.returncode != 0:
        raise RuntimeError(f"FFprobe failed: {result.stderr}")

    # ffprobe can exit 0 yet emit an empty string or the literal "N/A" for the
    # duration (streamed inputs, some container muxes). float() would raise a
    # bare ValueError that callers only guarding RuntimeError do not catch, so
    # normalize the unparseable case into RuntimeError here.
    raw_duration = result.stdout.strip()
    try:
        return float(raw_duration)
    except ValueError as exc:
        raise RuntimeError(
            f"FFprobe returned an unusable duration ('{raw_duration}') for {video_path.name}"
        ) from exc


# Below this peak level the tail is treated as effectively silent (room tone /
# encoded silence sits well under this; speech peaks far above it).
_TAIL_SILENCE_MAX_VOLUME_DBFS = -45.0


def tail_is_silent(
    audio_path: Path,
    start_seconds: float,
    end_seconds: float,
    *,
    max_volume_dbfs_threshold: float = _TAIL_SILENCE_MAX_VOLUME_DBFS,
) -> bool | None:
    """Report whether the audio slice [start, end] is effectively silent.

    Used to tell a legitimately quiet tail (narrator stopped talking) apart from
    a tail where STT dropped or compressed speech. Runs ffmpeg ``volumedetect``
    on the slice and compares its peak level against ``max_volume_dbfs_threshold``.

    Returns True for a silent tail, False for a tail that still carries sound,
    and None when the slice cannot be measured (empty range, missing file,
    missing ffmpeg, decode error, or no parseable peak). A None result lets
    callers fall back to a non-destructive warning instead of guessing.
    """
    if end_seconds - start_seconds <= 0:
        return None
    if not audio_path.exists():
        return None

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(audio_path),
        "-t",
        f"{end_seconds - start_seconds:.3f}",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        # TimeoutExpired is a SubprocessError subclass, so a wedged volumedetect
        # is caught here and reported as "cannot measure" (None), matching the
        # other unmeasurable cases -- callers then fall back to a warning.
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        return None

    # volumedetect prints its summary to stderr, e.g. "max_volume: -91.0 dB".
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", result.stderr)
    if match is None:
        return None
    return float(match.group(1)) < max_volume_dbfs_threshold


def get_audio_duration(audio_path: Path) -> float:
    """Get audio file duration in seconds using FFprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = _run_media_command(cmd, timeout=_FFPROBE_TIMEOUT_SECONDS, tool="ffprobe")
    if result.returncode != 0:
        raise RuntimeError(f"FFprobe failed: {result.stderr}")

    raw_duration = result.stdout.strip()
    try:
        return float(raw_duration)
    except ValueError as exc:
        raise RuntimeError(
            f"FFprobe returned an unusable duration ('{raw_duration}') for {audio_path.name}"
        ) from exc


def detect_silence_boundaries(
    audio_path: Path,
    min_silence_duration: float = 1.0,
    noise_threshold: str = "-35dB",
) -> list[float]:
    """Detect silence boundaries in audio using ffmpeg ``silencedetect``.

    Returns the midpoints of detected silence gaps — natural cut points for
    chunking, so speech is never split mid-word.

    Args:
        audio_path: Path to audio file.
        min_silence_duration: Minimum silence length to detect (seconds).
        noise_threshold: Volume threshold below which audio counts as silence.

    Returns:
        Sorted list of timestamps (seconds) at silence midpoints.
    """
    cmd = [
        "ffmpeg",
        "-i",
        str(audio_path),
        "-af",
        f"silencedetect=noise={noise_threshold}:d={min_silence_duration}",
        "-f",
        "null",
        "-",
    ]
    result = _run_media_command(cmd, timeout=_FFMPEG_TIMEOUT_SECONDS, tool="ffmpeg")

    boundaries: list[float] = []
    silence_start: float | None = None

    # silencedetect reports its findings on stderr.
    for line in result.stderr.split("\n"):
        start_match = re.search(r"silence_start:\s*([\d.]+)", line)
        end_match = re.search(r"silence_end:\s*([\d.]+)", line)

        if start_match:
            silence_start = float(start_match.group(1))
        if end_match and silence_start is not None:
            silence_end = float(end_match.group(1))
            boundaries.append((silence_start + silence_end) / 2)
            silence_start = None

    return sorted(boundaries)


def _discard_partial_chunks(chunks: list[tuple[Path, float]], temp_dir: Path) -> None:
    """Remove already-written chunk WAVs and their temp dir after a split abort."""
    for made_path, _offset in chunks:
        made_path.unlink(missing_ok=True)
    try:
        temp_dir.rmdir()
    except OSError:
        pass


def split_audio_chunks(
    audio_path: Path,
    max_chunk_duration: float = 60.0,
    overlap: float = 3.0,
) -> list[tuple[Path, float]]:
    """Split audio at silence boundaries for accurate STT timestamps.

    Whisper-style STT drifts timestamps and scrambles text when a silence gap
    falls inside a chunk (its hallucination-skip heuristic discards the gap and
    everything after it slides forward). Cutting at natural pauses keeps each
    chunk to continuous speech, so timestamps stay accurate.

    Falls back to fixed-interval splitting when no usable silence is detected.
    Chunks are written as 16 kHz mono WAV (PCM): WAV avoids the MP3 encoder
    delay (~66 ms per chunk) that otherwise corrupts the offsets.

    Args:
        audio_path: Path to audio file.
        max_chunk_duration: Maximum chunk duration; cuts at the nearest silence
            before this, or at fixed intervals when silence is too far apart.
        overlap: Overlap between chunks in seconds, to avoid clipping words at
            the seams (deduplicated later when merging segments).

    Returns:
        List of ``(chunk_path, offset_seconds)`` tuples. ``offset_seconds`` is
        the chunk's true start on the global timeline, used to correct
        per-chunk timestamps back to absolute time.
    """
    total_duration = get_audio_duration(audio_path)

    if total_duration <= max_chunk_duration:
        return [(audio_path, 0.0)]

    silence_points = detect_silence_boundaries(audio_path)
    console.print(f"[dim]  Found {len(silence_points)} silence boundaries[/]")

    # Build cut points at silence boundaries, but no closer than 15s apart and
    # no further than max_chunk_duration apart (fill wide gaps with fixed cuts).
    cut_points: list[float] = [0.0]
    for sp in silence_points:
        last_cut = cut_points[-1]
        if sp - last_cut < 15.0:
            continue  # too close to previous cut
        while sp - cut_points[-1] > max_chunk_duration:
            cut_points.append(cut_points[-1] + max_chunk_duration)
        cut_points.append(sp)

    # Fill the remaining gap to the end with fixed-interval cuts.
    while total_duration - cut_points[-1] > max_chunk_duration:
        cut_points.append(cut_points[-1] + max_chunk_duration)

    if total_duration - cut_points[-1] > 5.0:
        cut_points.append(total_duration)
    else:
        cut_points[-1] = total_duration

    # No useful silence found at all — fall back to pure fixed intervals.
    if len(cut_points) <= 2:
        console.print("[dim]  No silence boundaries — using fixed interval chunking[/]")
        cut_points = [0.0]
        offset = 0.0
        while offset + max_chunk_duration < total_duration:
            offset += max_chunk_duration
            cut_points.append(offset)
        cut_points.append(total_duration)

    chunks: list[tuple[Path, float]] = []
    # Per-run isolated temp directory. A shared dir keyed only by the audio stem
    # let two concurrent reviews of files with the same stem clobber each other's
    # WAVs (ffmpeg -y) and clean up each other's chunks. mkdtemp is atomic and
    # deterministic per process (no external randomness) so every split gets its
    # own directory (M4).
    temp_dir = Path(tempfile.mkdtemp(prefix="screenscribe_chunks_"))

    for chunk_idx in range(len(cut_points) - 1):
        start = max(0.0, cut_points[chunk_idx] - overlap) if chunk_idx > 0 else 0.0
        end = min(total_duration, cut_points[chunk_idx + 1] + overlap)
        # The offset is the chunk's real start on the global timeline — i.e. the
        # actual ffmpeg extraction point (``start``), which for overlapped chunks
        # sits ``overlap`` seconds before the cut point. Returning the bare cut
        # point would slide every segment of chunks idx>=1 forward by ``overlap``.
        offset = start
        duration = end - start

        chunk_path = temp_dir / f"{audio_path.stem}_chunk_{chunk_idx:02d}.wav"

        cmd = [
            "ffmpeg",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            str(audio_path),
            "-c:a",
            "pcm_s16le",  # WAV/PCM: no encoder delay to corrupt timestamps
            "-ar",
            "16000",
            "-ac",
            "1",
            "-y",
            str(chunk_path),
        ]
        # A mid-recording split failure (nonzero exit OR a wedged ffmpeg that
        # trips the timeout) must NOT silently return the chunks produced so far:
        # the caller would transcribe that prefix and report success, dropping the
        # rest of a long recording from the transcript and every downstream
        # finding. Clean up what we made and raise so transcribe_audio_chunked
        # falls back to a single-shot transcription of the whole file instead of
        # shipping a partial as complete.
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            _discard_partial_chunks(chunks, temp_dir)
            raise RuntimeError(
                f"chunk {chunk_idx} split timed out after {_FFMPEG_TIMEOUT_SECONDS:.0f}s"
            ) from exc
        if result.returncode != 0:
            _discard_partial_chunks(chunks, temp_dir)
            raise RuntimeError(f"chunk {chunk_idx} split failed: {result.stderr[:200]}")

        chunks.append((chunk_path, offset))

    console.print(f"[dim]  Split into {len(chunks)} chunks (silence-aware, WAV)[/]")
    return chunks
