"""D5 (bugfix): a fresh ``review`` re-analyze must NOT carry reviewer overlay.

``screenscribe review`` is a FRESH RE-ANALYZE: it produces a brand-new AI report
in a new versioned directory (``demo_review`` -> ``demo_review_2``). It must be
CLEAN -- it must not copy the prior review's ``manual_frames/`` images forward,
and the new ``report.json`` must not contain the prior reviewer's manual markers
(``human_review.manual_frames`` / legacy ``manual_review.markers``).

A previous attempt (commit 6bf97ec) copied ``manual_frames/`` into the new
version. That created ORPHAN images: files on disk with no matching markers in
the fresh report -- worse than not copying. Restoring reviewer overlay onto an
existing review state is a separate future feature; fresh regen stays clean.

This drives the REAL ``run_review`` end-to-end (via the ``review`` CLI command)
with the heavy pipeline steps mocked, exactly like ``test_review_empty_state``.
The assertions are on the OUTCOME (no orphan dir, no manual markers in the new
report), so if carry-over were ever reintroduced this test would fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import screenscribe.cli as cli_module
from screenscribe.config import ScreenScribeConfig
from screenscribe.review_server import MANUAL_FRAMES_DIRNAME
from screenscribe.semantic_filter import SemanticFilterResult
from screenscribe.transcribe import Segment, TranscriptionResult


def _transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="OK, success.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="OK, success.", no_speech_prob=0.05),
        ],
        language="en",
        response_id="resp_stt_regen_clean",
    )


def _seed_prior_review_with_manual_overlay(review_dir: Path, video_stem: str) -> Path:
    """Create a completed prior review dir carrying a reviewer overlay.

    Writes (a) a stemmed ``<stem>_report.json`` so ``_find_next_review_path``
    recognizes a completed bundle and bumps the next run to ``_2``, and (b) a
    durable manual-frame image under ``manual_frames/``. The report carries a
    ``human_review.manual_frames`` marker referencing that image -- i.e. a real,
    consistent reviewer overlay that a fresh regen must NOT inherit.
    """
    review_dir.mkdir(parents=True, exist_ok=True)

    frames = review_dir / MANUAL_FRAMES_DIRNAME
    frames.mkdir(parents=True, exist_ok=True)
    # Minimal JPEG magic bytes -- enough to stand in for a captured frame.
    (frames / "cap1.jpg").write_bytes(b"\xff\xd8\xff\xe0seed-manual-frame")

    report_path = review_dir / f"{video_stem}_report.json"
    report_path.write_text(
        json.dumps(
            {
                "summary": {"total": 1},
                "findings": [],
                "transcript": "OK, success.",
                "executive_summary": "prior reviewed report",
                "errors": [],
                "human_review": {
                    "manual_frames": [
                        {
                            "id": "cap1",
                            "frame_path": f"{MANUAL_FRAMES_DIRNAME}/cap1.jpg",
                            "note": "reviewer overlay that must not leak forward",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return report_path


def _report_has_manual_markers(report: dict) -> bool:
    """True if a report carries reviewer manual-frame overlay in any known shape."""
    human = report.get("human_review")
    if isinstance(human, dict) and human.get("manual_frames"):
        return True
    manual_review = report.get("manual_review")
    if isinstance(manual_review, dict) and (
        manual_review.get("markers") or manual_review.get("results")
    ):
        return True
    return False


def test_fresh_regen_does_not_carry_manual_frames_or_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fresh re-analyze over a video with a prior reviewed dir => clean new dir.

    Outcome assertions (mechanism-agnostic):
      * the new versioned dir has NO orphan ``manual_frames/`` (absent or empty),
      * the new ``report.json`` carries NO manual markers.

    Non-vacuous: if regen copied ``manual_frames/`` forward (the reverted
    6bf97ec behavior), the new dir would hold ``manual_frames/cap1.jpg`` with no
    matching marker in the fresh report -- exactly the orphan this asserts away.
    """
    runner = CliRunner()

    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")

    base_output = tmp_path / "demo_review"
    prior_report = _seed_prior_review_with_manual_overlay(base_output, "demo")
    # Precondition: the seeded prior review really carries an overlay.
    seeded = json.loads(prior_report.read_text(encoding="utf-8"))
    assert _report_has_manual_markers(seeded)
    assert (base_output / MANUAL_FRAMES_DIRNAME / "cap1.jpg").is_file()

    # Stub the heavy pipeline so REAL run_review reaches report generation with no
    # FFmpeg / network / models -- same harness shape as test_review_empty_state.
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.cli.transcribe_audio",
        lambda *args, **kwargs: _transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *args, **kwargs: SemanticFilterResult(pois=[], response_id="resp_filter_empty"),
    )
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),
    )

    # Fresh regen: -o points at the SAME base, NO --force, so versioning bumps to
    # demo_review_2 (the prior bundle is detected via demo_report.json).
    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video_path),
            "-o",
            str(base_output),
            "--no-serve",
            "--skip-validation",
        ],
    )

    assert result.exit_code == 0, result.output

    new_dir = tmp_path / "demo_review_2"
    assert new_dir.is_dir(), f"expected a new versioned dir. CLI output:\n{result.output}"

    # 1) No orphan manual_frames carried into the fresh dir.
    new_frames = new_dir / MANUAL_FRAMES_DIRNAME
    assert not new_frames.exists() or not any(new_frames.iterdir()), (
        "fresh regen must not copy the prior review's manual_frames/ forward "
        f"(found: {list(new_frames.iterdir()) if new_frames.exists() else []})"
    )

    # 2) The fresh report.json must not inherit the reviewer's manual markers.
    new_report_path = new_dir / "demo_report.json"
    assert new_report_path.is_file(), f"fresh report missing. CLI output:\n{result.output}"
    new_report = json.loads(new_report_path.read_text(encoding="utf-8"))
    assert not _report_has_manual_markers(new_report), (
        "fresh regen report must not carry reviewer manual markers"
    )

    # The prior review (and its overlay) is untouched -- regen does not mutate it.
    assert (base_output / MANUAL_FRAMES_DIRNAME / "cap1.jpg").is_file()
