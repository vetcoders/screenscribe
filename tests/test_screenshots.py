"""Tests for screenscribe.screenshots — frame extraction command + path/error logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from screenscribe.detect import Detection, Segment
from screenscribe.screenshots import (
    _select_capture_timestamp,
    extract_keyframes_around_detection,
    extract_screenshot,
    extract_screenshots_for_detections,
)


def _detection(start: float, end: float, category: str = "bug") -> Detection:
    seg = Segment(id=1, start=start, end=end, text="some text")
    return Detection(segment=seg, category=category, keywords_found=["x"], context="ctx")


# --- _select_capture_timestamp -------------------------------------------------


def test_select_timestamp_captures_at_start_not_midpoint():
    """A ranged segment captures at start+offset, NOT the segment midpoint.

    Users describe problems AFTER seeing them; by the midpoint of their
    narration they may have already navigated past the screen that was on
    display when the issue first appeared. start+offset captures that screen.
    """
    det = _detection(start=10.0, end=20.0)
    # midpoint would be 15.0; start+offset must win
    assert _select_capture_timestamp(det, offset=0.5) == 10.5


def test_select_timestamp_point_detection_uses_start_plus_offset():
    """A point-like segment (end==start) also uses start+offset."""
    det = _detection(start=8.0, end=8.0)
    assert _select_capture_timestamp(det, offset=0.5) == 8.5


def test_select_timestamp_default_offset_is_half_second():
    """The default offset is 0.5s past the segment start."""
    det = _detection(start=10.0, end=20.0)
    assert _select_capture_timestamp(det) == 10.5


def test_select_timestamp_clamps_negative_start_to_zero():
    """Negative start never returns below zero."""
    det = _detection(start=-3.0, end=-3.0)
    # start+offset = -2.5 → max(0.0, ...) clamps to 0.0
    assert _select_capture_timestamp(det, offset=0.5) == 0.0


def test_select_timestamp_negative_result_clamps_to_zero():
    """An offset that drives the result below zero clamps to 0."""
    det = _detection(start=5.0, end=5.0)
    # start+offset = -5.0 → max(0.0, ...) clamps to 0.0
    assert _select_capture_timestamp(det, offset=-10.0) == 0.0


def test_select_timestamp_clamps_to_segment_end_for_tail():
    """A ranged tail segment whose start+offset overshoots its end is clamped.

    For a detection in the last fraction of a segment, start+offset can land past
    the segment end (and thus past EOF for a tail finding), making ffmpeg's -ss
    fail so the frame is dropped. Clamp to just below the segment end so a frame
    is still captured (finding E): seeking exactly to the end yields no frame, so
    the clamp backs off a frame-safe margin.
    """
    det = _detection(start=99.8, end=100.0)
    # start+offset = 100.3 overshoots end 100.0 -> clamp to just below it (99.9),
    # NOT to 100.0 (which is EOF and would still drop the frame).
    assert _select_capture_timestamp(det, offset=0.5) == pytest.approx(99.9)


def test_select_timestamp_clamps_to_video_duration_for_point_tail():
    """A point-like detection at the video end clamps to just below the duration."""
    det = _detection(start=100.0, end=100.0)
    # start+offset = 100.5 would seek past EOF; clamp to a frame-safe time just
    # below the known duration (99.9), not exactly to EOF (100.0).
    assert _select_capture_timestamp(det, offset=0.5, video_duration=100.0) == pytest.approx(99.9)


def test_select_timestamp_tail_clamp_stays_below_eof():
    """The tail clamp must never return exactly the EOF bound (frame-safe)."""
    det = _detection(start=100.0, end=100.0)
    ts = _select_capture_timestamp(det, offset=0.5, video_duration=100.0)
    assert ts < 100.0


def test_select_timestamp_no_clamp_when_within_video_duration():
    """Within-bounds detections keep start+offset even when duration is known."""
    det = _detection(start=10.0, end=20.0)
    assert _select_capture_timestamp(det, offset=0.5, video_duration=100.0) == 10.5


# --- extract_screenshot --------------------------------------------------------


@patch("screenscribe.screenshots.subprocess.run")
def test_extract_screenshot_builds_correct_ffmpeg_command(mock_run):
    """ffmpeg command embeds -ss timestamp, -i input, single frame, quality, output."""
    mock_run.return_value = MagicMock(returncode=0, stderr="")

    out = extract_screenshot(Path("/vid/in.mp4"), 12.5, Path("/out/shot.jpg"))

    assert out == Path("/out/shot.jpg")
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "ffmpeg"
    # -ss precedes -i (fast seek), and uses the float timestamp as a string
    assert cmd[1] == "-ss"
    assert cmd[2] == "12.5"
    assert cmd[3] == "-i"
    assert cmd[4] == "/vid/in.mp4"
    assert "-vframes" in cmd and cmd[cmd.index("-vframes") + 1] == "1"
    assert "-q:v" in cmd and cmd[cmd.index("-q:v") + 1] == "2"
    assert "-y" in cmd
    assert cmd[-1] == "/out/shot.jpg"


@patch("screenscribe.screenshots.subprocess.run")
def test_extract_screenshot_captures_output(mock_run):
    """subprocess.run is invoked with capture_output and text mode."""
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    extract_screenshot(Path("in.mp4"), 1.0, Path("o.jpg"))
    kwargs = mock_run.call_args.kwargs
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


@patch("screenscribe.screenshots.subprocess.run")
def test_extract_screenshot_raises_on_nonzero_returncode(mock_run):
    """A non-zero ffmpeg exit raises RuntimeError carrying stderr."""
    mock_run.return_value = MagicMock(returncode=1, stderr="boom decode error")
    with pytest.raises(RuntimeError, match="boom decode error"):
        extract_screenshot(Path("in.mp4"), 1.0, Path("o.jpg"))


# --- extract_screenshots_for_detections ----------------------------------------


@patch("screenscribe.screenshots.extract_screenshot")
def test_extract_for_detections_creates_output_dir(mock_extract, tmp_path):
    """The output directory is created (parents+exist_ok) before extracting."""
    target = tmp_path / "nested" / "shots"
    mock_extract.side_effect = lambda v, t, o: o
    extract_screenshots_for_detections(Path("v.mp4"), [_detection(1.0, 2.0)], target)
    assert target.is_dir()


@patch("screenscribe.screenshots.extract_screenshot")
def test_extract_for_detections_filename_and_pairs(mock_extract, tmp_path):
    """Each result pairs the detection with an indexed, category-tagged jpg path."""
    mock_extract.side_effect = lambda v, t, o: o
    dets = [_detection(10.0, 20.0, category="crash"), _detection(30.0, 40.0, category="ui")]

    results = extract_screenshots_for_detections(Path("v.mp4"), dets, tmp_path)

    assert len(results) == 2
    # start+offset of 10..20 = 10.5s → 00:10 → "00-10"
    d0, p0 = results[0]
    assert d0 is dets[0]
    assert p0.name == "01_crash_00-10.jpg"
    # start+offset of 30..40 = 30.5s → 00:30
    _, p1 = results[1]
    assert p1.name == "02_ui_00-30.jpg"


@patch("screenscribe.screenshots.extract_screenshot")
def test_extract_for_detections_skips_failed_frames(mock_extract, tmp_path):
    """A RuntimeError on one detection is swallowed; other frames still returned."""

    def side(video, ts, out):
        if "fail" in out.name:
            raise RuntimeError("ffmpeg died")
        return out

    mock_extract.side_effect = side
    dets = [_detection(10.0, 20.0, category="ok"), _detection(30.0, 40.0, category="fail")]

    results = extract_screenshots_for_detections(Path("v.mp4"), dets, tmp_path)

    # only the non-failing detection survives
    assert len(results) == 1
    assert results[0][0] is dets[0]


@patch("screenscribe.screenshots.extract_screenshot")
def test_extract_for_detections_passes_offset_through(mock_extract, tmp_path):
    """The offset reaches the timestamp calc for point-like detections."""
    captured = []
    mock_extract.side_effect = lambda v, t, o: (captured.append(t), o)[1]
    # point-like detection so offset matters
    extract_screenshots_for_detections(Path("v.mp4"), [_detection(5.0, 5.0)], tmp_path, offset=2.0)
    assert captured == [7.0]


# --- extract_keyframes_around_detection ----------------------------------------


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_centered_timestamps(mock_extract, tmp_path):
    """Keyframes are spaced by interval and centered on the segment midpoint."""
    captured = []
    mock_extract.side_effect = lambda v, t, o: (captured.append(t), o)[1]
    det = _detection(start=20.0, end=20.0)  # center = 20

    paths = extract_keyframes_around_detection(
        Path("v.mp4"), det, tmp_path, num_frames=3, interval=2.0
    )

    # center 20, start_offset = -((3-1)/2)*2 = -2 → 18, 20, 22
    assert captured == [18.0, 20.0, 22.0]
    assert len(paths) == 3
    # filename carries the frame index so distinct frames never collide
    assert paths[0].name == "keyframe_00_00-18.jpg"


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_clamp_negative_timestamps_to_zero(mock_extract, tmp_path):
    """Timestamps that would go negative are clamped to 0."""
    captured = []
    mock_extract.side_effect = lambda v, t, o: (captured.append(t), o)[1]
    det = _detection(start=0.0, end=0.0)  # center = 0

    extract_keyframes_around_detection(Path("v.mp4"), det, tmp_path, num_frames=3, interval=2.0)

    # would be -2, 0, 2 → clamped to 0, 0, 2
    assert captured == [0, 0, 2.0]


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_skip_failed_frames(mock_extract, tmp_path):
    """A failing extract is silently skipped; surviving paths are returned."""
    call = {"n": 0}

    def side(video, ts, out):
        call["n"] += 1
        if call["n"] == 2:
            raise RuntimeError("frame fail")
        return out

    mock_extract.side_effect = side
    det = _detection(start=20.0, end=20.0)

    paths = extract_keyframes_around_detection(
        Path("v.mp4"), det, tmp_path, num_frames=3, interval=2.0
    )

    assert len(paths) == 2  # one of three frames failed


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_unique_filenames_when_timestamps_collide(mock_extract, tmp_path):
    """Frames clamping to the same whole second get distinct filenames (BH8/BH35).

    center=0.5, num_frames=3, interval=2.0 → raw -1.5, 0.5, 2.5 → clamped
    0, 0.5, 2.5. format_timestamp rounds the first two both to '00-00'; without
    the frame index they would share 'keyframe_00-00.jpg' and overwrite, so
    paths would contain a duplicate path and only one image would survive.
    """
    written = []
    mock_extract.side_effect = lambda v, t, o: (written.append(o), o)[1]
    det = _detection(start=0.0, end=1.0)  # center = 0.5

    paths = extract_keyframes_around_detection(
        Path("v.mp4"), det, tmp_path, num_frames=3, interval=2.0
    )

    # three real, distinct files written and three distinct paths returned
    assert len(paths) == 3
    assert len({p.name for p in paths}) == 3
    assert len({o.name for o in written}) == 3


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_creates_output_dir(mock_extract, tmp_path):
    """Keyframe extraction creates its output dir before writing."""
    target = tmp_path / "kf" / "deep"
    mock_extract.side_effect = lambda v, t, o: o
    extract_keyframes_around_detection(Path("v.mp4"), _detection(5.0, 5.0), target, num_frames=1)
    assert target.is_dir()


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_warns_when_all_frames_fail(mock_extract, tmp_path, capsys):
    """Total keyframe failure is surfaced, not silently returned as [] (P3-7)."""
    mock_extract.side_effect = RuntimeError("ffmpeg died")
    det = _detection(start=20.0, end=20.0)

    paths = extract_keyframes_around_detection(
        Path("v.mp4"), det, tmp_path, num_frames=3, interval=2.0
    )

    assert paths == []
    out = capsys.readouterr().out
    assert "All 3 keyframes failed" in out


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_warns_on_high_failure_ratio(mock_extract, tmp_path, capsys):
    """More-than-half keyframe failure is surfaced as a warning (P3-7)."""
    call = {"n": 0}

    def side(video, ts, out):
        call["n"] += 1
        if call["n"] >= 2:  # frames 2 and 3 of 3 fail -> 2/3 failed
            raise RuntimeError("frame fail")
        return out

    mock_extract.side_effect = side
    det = _detection(start=20.0, end=20.0)

    paths = extract_keyframes_around_detection(
        Path("v.mp4"), det, tmp_path, num_frames=3, interval=2.0
    )

    assert len(paths) == 1
    out = capsys.readouterr().out
    assert "2/3 keyframes failed" in out


@patch("screenscribe.screenshots.extract_screenshot")
def test_keyframes_no_warning_when_all_succeed(mock_extract, tmp_path, capsys):
    """A fully successful keyframe run emits no failure warning (P3-7 guard)."""
    mock_extract.side_effect = lambda v, t, o: o
    det = _detection(start=20.0, end=20.0)

    extract_keyframes_around_detection(Path("v.mp4"), det, tmp_path, num_frames=3, interval=2.0)

    out = capsys.readouterr().out
    assert "failed" not in out
