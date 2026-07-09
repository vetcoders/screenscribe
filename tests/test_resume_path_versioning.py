"""C6.2b: --resume must reuse the checkpoint directory, not bump to `_2`.

This is the regression that the C6.2 resume tests could NOT catch: they stub the
report writers to no-ops precisely so no `*_report.*` bundle lands in the base
output dir, which keeps ``_find_next_review_path`` from version-bumping. That
stub hides the real-world failure -- in production the report writers DO run, so
a partial run leaves both a checkpoint AND a report bundle in the base dir, and
the next ``--resume`` resolves the output dir to a fresh ``_2`` (no checkpoint)
and silently restarts from scratch.

These tests drive the REAL ``run_review`` through the ``review`` CLI command with
the report writers LEFT INTACT (only the heavy AI/ffmpeg steps are stubbed), so
the version-bump interaction is actually exercised. They assert that ``--resume``
continues in the same directory and retries only the residual failed item.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import screenscribe.cli as cli_module
from screenscribe.checkpoint import load_checkpoint
from screenscribe.config import ScreenScribeConfig
from screenscribe.semantic_filter import PointOfInterest, SemanticFilterResult
from screenscribe.transcribe import Segment, TranscriptionResult
from screenscribe.unified_analysis import UnifiedFinding


def _three_segment_transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="first issue. second issue. third issue.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="first issue", no_speech_prob=0.05),
            Segment(id=1, start=2.0, end=4.0, text="second issue", no_speech_prob=0.05),
            Segment(id=2, start=4.0, end=6.0, text="third issue", no_speech_prob=0.05),
        ],
        language="en",
        response_id="resp_stt_three",
    )


def _three_poi_result() -> SemanticFilterResult:
    return SemanticFilterResult(
        pois=[
            PointOfInterest(
                timestamp_start=float(i * 2),
                timestamp_end=float(i * 2 + 2),
                category="bug",
                confidence=0.9,
                reasoning=f"issue {i}",
                transcript_excerpt=f"issue {i}",
                segment_ids=[i],
            )
            for i in range(3)
        ],
        response_id="resp_filter_three",
    )


_DISTINCT_SUMMARIES = {
    0: "The save button does nothing when the user clicks it",
    1: "Dropdown menu overlaps the page footer on narrow viewports",
    2: "Search returns stale cached results after a hard refresh",
}


def _finding_for(detection: object) -> UnifiedFinding:
    seg = detection.segment  # type: ignore[attr-defined]
    return UnifiedFinding(
        detection_id=seg.id,
        screenshot_path=None,
        timestamp=seg.start,
        category="bug",
        is_issue=True,
        sentiment="problem",
        severity="high",
        summary=_DISTINCT_SUMMARIES[seg.id],
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="resp",
        merged_from_ids=[],
    )


def _install_real_report_stubs(
    monkeypatch: pytest.MonkeyPatch,
    config: ScreenScribeConfig,
    extracted_audio: Path,
) -> None:
    """Stub only the heavy AI/ffmpeg steps. Report writers are LEFT REAL so a
    `*_report.*` bundle actually lands in the output dir -- the condition that
    triggers the version-bump and exposes C6.2b."""
    monkeypatch.setattr("screenscribe.cli.check_ffmpeg_installed", lambda: None)
    monkeypatch.setattr("screenscribe.cli.extract_audio", lambda _: extracted_audio)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _: 29.0)
    monkeypatch.setattr(
        "screenscribe.transcribe.transcribe_audio",
        lambda *a, **kw: _three_segment_transcription(),
    )
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: _three_poi_result(),
    )
    monkeypatch.setattr(ScreenScribeConfig, "load", classmethod(lambda cls: config))
    # Stub the summary generators (paid LLM) reached on any success.
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_unified_summary", lambda *a, **kw: "summary"
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_visual_summary_unified", lambda *a, **kw: "visual"
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections",
        lambda video, detections, _dir: [
            (d, extracted_audio.parent / f"shot-{d.segment.id}.jpg") for d in detections
        ],
    )


def _run(runner: CliRunner, video_path: Path, output_dir: Path, *, resume: bool) -> object:
    args = ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"]
    if resume:
        args.append("--resume")
    return runner.invoke(cli_module.app, args)


def test_resume_reuses_checkpoint_dir_with_real_report_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real-path regression: run 1 fails 1 of 3 items AND writes a real
    `*_report.*` bundle into the base dir; run 2 with --resume must continue in
    that SAME dir (no `_2` bump) and retry ONLY the residual item.

    Pre-fix this fails: the bundle triggers _find_next_review_path to bump the
    output dir to `demo_review_2`, where there is no checkpoint, so the resume
    re-analyzes all 3 items (analyze_call_sizes == [3, 3])."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )

    _install_real_report_stubs(monkeypatch, config, extracted_audio)

    analyze_call_sizes: list[int] = []

    def fake_unified(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        analyze_call_sizes.append(len(screenshots))
        if len(analyze_call_sizes) == 1:
            # First run: drop the LAST item (simulate one failed VLM item).
            return [_finding_for(d) for (d, _p) in screenshots[:-1]]
        # Resume run: succeed everything we were handed (the residual).
        return [_finding_for(d) for (d, _p) in screenshots]

    monkeypatch.setattr("screenscribe.review_pipeline.analyze_all_findings_unified", fake_unified)

    # --- Run 1: partial failure, REAL report bundle written ---
    result1 = _run(runner, video_path, output_dir, resume=False)
    assert result1.exit_code == 0, result1.output

    # A real JSON report bundle must have landed in the base dir -- this is the
    # artifact that makes _find_next_review_path want to bump on the next run.
    assert (output_dir / "demo_report.json").exists(), "real report bundle must be written"
    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None, "partial run must keep the checkpoint in base dir"
    assert not checkpoint.is_stage_complete("unified_analysis")

    # --- Run 2: resume must reuse the SAME dir, not bump to _2 ---
    result2 = _run(runner, video_path, output_dir, resume=True)
    assert result2.exit_code == 0, result2.output

    # No version-bumped sibling directory was created.
    assert not (tmp_path / "demo_review_2").exists(), (
        "C6.2b: --resume must not bump to a fresh _2 dir and lose the checkpoint"
    )
    # The resume actually found the checkpoint: only the 1 residual was retried.
    assert analyze_call_sizes == [3, 1], analyze_call_sizes
    # Full success now -> checkpoint deleted in the base dir.
    assert load_checkpoint(output_dir) is None


def test_no_resume_still_version_bumps_completed_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guard the other half of the contract: WITHOUT --resume an existing
    completed bundle must still be preserved via a `_2` version bump (the fix
    must not regress the no-resume overwrite protection)."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )

    _install_real_report_stubs(monkeypatch, config, extracted_audio)
    # Full success on every run -> completed bundle, checkpoint deleted.
    monkeypatch.setattr(
        "screenscribe.review_pipeline.analyze_all_findings_unified",
        lambda screenshots, *a, **kw: [_finding_for(d) for (d, _p) in screenshots],
    )

    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    assert (output_dir / "demo_report.json").exists()
    assert load_checkpoint(output_dir) is None  # completed -> no checkpoint

    # Second run WITHOUT --resume: completed bundle present, no checkpoint ->
    # must bump to _2 to preserve the prior output.
    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    assert (tmp_path / "demo_review_2").exists(), (
        "no-resume run must still version-bump to preserve a completed bundle"
    )


# --------------------------------------------------------------------------- #
# RERUN-UX: interactive prompt (Overwrite / Resume / New) on a real TTY;       #
# deterministic auto-bump under non-TTY / CI.                                   #
# --------------------------------------------------------------------------- #


def _full_success_first_run(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    video_path: Path,
    output_dir: Path,
    extracted_audio: Path,
    config: ScreenScribeConfig,
) -> None:
    """Land a completed review bundle in ``output_dir`` (full success, no
    checkpoint left behind)."""
    _install_real_report_stubs(monkeypatch, config, extracted_audio)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.analyze_all_findings_unified",
        lambda screenshots, *a, **kw: [_finding_for(d) for (d, _p) in screenshots],
    )
    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    assert (output_dir / "demo_report.json").exists()


def test_rerun_prompt_new_bumps_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TTY + [N]ew: keep the old bundle, create the versioned `_2` copy
    (interactive equivalent of the historical auto-bump)."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    _full_success_first_run(runner, monkeypatch, video_path, output_dir, extracted_audio, config)

    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: True)
    monkeypatch.setattr("screenscribe.review_pipeline.Prompt.ask", lambda *a, **kw: "n")

    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    assert (tmp_path / "demo_review_2").exists(), "[N]ew must version-bump to _2"


def test_rerun_prompt_overwrite_reuses_base_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TTY + [O]verwrite: re-run in the SAME directory, no `_2` sibling."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    _full_success_first_run(runner, monkeypatch, video_path, output_dir, extracted_audio, config)

    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: True)
    monkeypatch.setattr("screenscribe.review_pipeline.Prompt.ask", lambda *a, **kw: "o")

    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    assert not (tmp_path / "demo_review_2").exists(), "[O]verwrite must reuse the base dir"
    assert (output_dir / "demo_report.json").exists()


def test_rerun_prompt_resume_retries_residual(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TTY + [R]esume (without the --resume flag): continue from the checkpoint
    in the base dir and retry ONLY the residual failed item -- proving the
    prompt's [R] drives the same mechanic as the explicit --resume flag."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    _install_real_report_stubs(monkeypatch, config, extracted_audio)

    analyze_call_sizes: list[int] = []

    def fake_unified(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        analyze_call_sizes.append(len(screenshots))
        if len(analyze_call_sizes) == 1:
            return [_finding_for(d) for (d, _p) in screenshots[:-1]]  # drop last -> partial
        return [_finding_for(d) for (d, _p) in screenshots]

    monkeypatch.setattr("screenscribe.review_pipeline.analyze_all_findings_unified", fake_unified)

    # Run 1: partial failure -> checkpoint + report bundle in base dir.
    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None and not checkpoint.is_stage_complete("unified_analysis")

    # Run 2: NO --resume flag, but interactive [R]esume choice.
    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: True)
    monkeypatch.setattr("screenscribe.review_pipeline.Prompt.ask", lambda *a, **kw: "r")

    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    assert not (tmp_path / "demo_review_2").exists(), "[R]esume must reuse the checkpoint dir"
    assert analyze_call_sizes == [3, 1], analyze_call_sizes
    assert load_checkpoint(output_dir) is None  # completed on resume


def test_rerun_prompt_resume_without_checkpoint_preserves_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding H: re-running a COMPLETED review (whose checkpoint was deleted on
    success) and choosing [R]esume must NOT silently overwrite the prior report.

    There is nothing to resume from -- ``load_checkpoint`` would find no
    checkpoint and start fresh in the base dir, clobbering the previous bundle.
    The fix routes a 'resume with no checkpoint' to the preserve-and-version-bump
    path instead of a silent overwrite. Pre-fix this fails: no ``_2`` dir is
    created and the base report is overwritten in place.
    """
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    _full_success_first_run(runner, monkeypatch, video_path, output_dir, extracted_audio, config)
    # Completed run => no checkpoint left behind.
    assert load_checkpoint(output_dir) is None
    original_report = (output_dir / "demo_report.json").read_bytes()

    # TTY + [R]esume, but there is no checkpoint to resume from.
    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: True)
    monkeypatch.setattr("screenscribe.review_pipeline.Prompt.ask", lambda *a, **kw: "r")

    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    # The previous, completed report must be preserved, not overwritten.
    assert (tmp_path / "demo_review_2").exists(), (
        "resume-without-checkpoint must preserve the prior bundle via a version bump"
    )
    assert (output_dir / "demo_report.json").read_bytes() == original_report, (
        "the prior report must not be silently overwritten when there is nothing to resume"
    )


def test_resume_with_invalid_checkpoint_preserves_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Round-7 P1: ``--resume`` must validate the checkpoint, not just its mere
    presence, before reusing the base directory.

    A partial run leaves BOTH a checkpoint and a real ``*_report.*`` bundle in
    the base dir. If the video then changes (or the checkpoint is otherwise
    invalid for this video/language), ``checkpoint_valid_for_video`` rejects it
    downstream and the pipeline starts fresh -- but if ``video_output`` was
    already pinned to ``base_output`` only because the checkpoint *file* existed,
    that fresh run silently overwrites the prior bundle in place.

    The fix gates the dir-reuse on an actually-valid checkpoint; an invalid one
    must fall back to the preserve-and-version-bump path. Pre-fix this fails: no
    ``_2`` dir is created and the base report is overwritten.
    """
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    _install_real_report_stubs(monkeypatch, config, extracted_audio)

    analyze_call_sizes: list[int] = []

    def fake_unified(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        analyze_call_sizes.append(len(screenshots))
        if len(analyze_call_sizes) == 1:
            return [_finding_for(d) for (d, _p) in screenshots[:-1]]  # drop last -> partial
        return [_finding_for(d) for (d, _p) in screenshots]

    monkeypatch.setattr("screenscribe.review_pipeline.analyze_all_findings_unified", fake_unified)

    # Run 1: partial failure -> checkpoint + real report bundle in base dir.
    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None and not checkpoint.is_stage_complete("unified_analysis")
    original_report = (output_dir / "demo_report.json").read_bytes()

    # Invalidate the checkpoint for this video: the file still exists, but the
    # video bytes (and therefore the hash) no longer match the checkpoint.
    video_path.write_bytes(b"a completely different video")

    # Run 2 with --resume: the checkpoint is present but invalid for the changed
    # video, so the prior bundle must be preserved via a version bump rather than
    # overwritten in place.
    assert _run(runner, video_path, output_dir, resume=True).exit_code == 0
    assert (tmp_path / "demo_review_2").exists(), (
        "resume with an invalid checkpoint must preserve the prior bundle via a version bump"
    )
    assert (output_dir / "demo_report.json").read_bytes() == original_report, (
        "the prior report must not be silently overwritten when the checkpoint is invalid"
    )


def test_non_tty_does_not_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Non-TTY / CI: never prompt; deterministically auto-bump to `_2`."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    output_dir = tmp_path / "demo_review"
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    _full_success_first_run(runner, monkeypatch, video_path, output_dir, extracted_audio, config)

    # Force non-TTY and make any prompt attempt an explicit failure.
    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: False)

    def _boom(*a: object, **kw: object) -> str:
        raise AssertionError("non-TTY run must not prompt")

    monkeypatch.setattr("screenscribe.review_pipeline.Prompt.ask", _boom)

    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    assert (tmp_path / "demo_review_2").exists(), "non-TTY must auto-bump to _2 without prompting"
