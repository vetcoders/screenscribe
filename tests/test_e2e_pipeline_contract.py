"""Offline regressions for the live E2E gate's evidence requirements."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.e2e.conftest import (
    FIXTURE_NARRATION,
    _assert_generated_review,
    _generate_fixture_video,
)


@pytest.fixture
def report_path(tmp_path: Path) -> Path:
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    (screenshots / "frame.png").write_bytes(b"synthetic screenshot placeholder")
    report = {
        "transcript_source": "audio",
        "transcript": FIXTURE_NARRATION,
        "transcript_segments": [{"start": 0, "end": 10, "text": FIXTURE_NARRATION}],
        "errors": [],
        "analysis_passes": {
            stage: {"status": "completed", "count": 1}
            for stage in ("detections", "screenshots", "unified_analysis")
        },
        "findings": [
            {
                "screenshot": "frame.png",
                "unified_analysis": {"status": "completed", "degraded": False, "is_issue": True},
            }
        ],
    }
    path = tmp_path / "fixture_report.json"
    path.write_text(json.dumps(report))
    return path


def test_complete_pipeline_evidence_is_accepted(report_path: Path) -> None:
    _assert_generated_review(report_path)


@pytest.mark.parametrize(
    "failure",
    [
        "empty_stt",
        "truncated_stt",
        "no_segments",
        "no_llm",
        "no_vlm",
        "degraded",
        "errors",
        "missing_screenshot",
        "no_issue",
    ],
)
def test_incomplete_pipeline_cannot_unlock_browser_tests(report_path: Path, failure: str) -> None:
    report = json.loads(report_path.read_text())
    if failure == "empty_stt":
        report["transcript"] = ""
        report["transcript_segments"] = []
        report["findings"] = []
        for stage in report["analysis_passes"].values():
            stage.update(status="empty", count=0)
    elif failure == "truncated_stt":
        report["transcript"] = "There is a bug."
    elif failure == "no_segments":
        report["transcript_segments"] = []
    elif failure == "no_llm":
        report["analysis_passes"]["detections"].update(status="empty", count=0)
    elif failure == "no_vlm":
        report["analysis_passes"]["unified_analysis"].update(status="partial", count=0)
    elif failure == "degraded":
        report["findings"][0]["unified_analysis"]["degraded"] = True
    elif failure == "errors":
        report["errors"] = ["Synthetic provider failure"]
    elif failure == "missing_screenshot":
        report["findings"][0]["screenshot"] = "absent.png"
    elif failure == "no_issue":
        report["findings"][0]["unified_analysis"]["is_issue"] = False
    report_path.write_text(json.dumps(report))
    with pytest.raises(AssertionError):
        _assert_generated_review(report_path)


@pytest.mark.parametrize("synth", ["say", "espeak-ng"])
def test_fixture_uses_english_speech_without_truncating_video(tmp_path: Path, synth: str) -> None:
    with (
        patch("tests.e2e.conftest._ffmpeg_bin", return_value="ffmpeg"),
        patch(
            "tests.e2e.conftest.shutil.which",
            side_effect=lambda name: name if name == synth else None,
        ),
        patch("tests.e2e.conftest.subprocess.run") as run,
    ):
        _generate_fixture_video(tmp_path / "fixture.mov")
    voice, mux = [call.args[0] for call in run.call_args_list]
    assert FIXTURE_NARRATION in voice
    assert voice[voice.index("-v") + 1] == ("Samantha" if synth == "say" else "en-us")
    assert "-shortest" in mux
    assert not any("sine=" in arg or "duration=" in arg for arg in mux)


def test_no_synthesizer_fails_instead_of_substituting_silence(tmp_path: Path) -> None:
    with (
        patch("tests.e2e.conftest._ffmpeg_bin", return_value="ffmpeg"),
        patch("tests.e2e.conftest.shutil.which", return_value=None),
        pytest.raises(pytest.fail.Exception, match="speech E2E fixture"),
    ):
        _generate_fixture_video(tmp_path / "fixture.mov")
