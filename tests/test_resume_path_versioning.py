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


# --------------------------------------------------------------------------- #
# -o PATH resolution: an existing ORDINARY folder is a parent, not a review.   #
# --------------------------------------------------------------------------- #


def _success_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[CliRunner, Path, Path]:
    """Full-success stubs (real report writers) + a ``demo.mov`` input."""
    runner = CliRunner()
    video_path = tmp_path / "demo.mov"
    video_path.write_bytes(b"video")
    extracted_audio = tmp_path / "audio.mp3"
    extracted_audio.write_bytes(b"audio")
    config = ScreenScribeConfig(
        llm_api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-vision-key",  # pragma: allowlist secret
    )
    _install_real_report_stubs(monkeypatch, config, extracted_audio)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.analyze_all_findings_unified",
        lambda screenshots, *a, **kw: [_finding_for(d) for (d, _p) in screenshots],
    )
    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: False)
    return runner, video_path, extracted_audio


def test_output_ordinary_folder_with_foreign_report_is_a_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o ~/Downloads`` holding an unrelated ``*_report.md`` must write
    ``Downloads/demo_review`` -- not announce an existing review and create a
    ``Downloads_2`` sibling (the v0.1.19 operator report)."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    foreign = downloads / "2026-08-17_vc-notes_report.md"
    foreign.write_text("# unrelated")

    result = _run(runner, video_path, downloads, resume=False)
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert (downloads / "demo_review" / "demo_report.json").exists()
    assert not (tmp_path / "Downloads_2").exists()
    assert "Existing Review Found" not in normalized
    assert "Found Previous Review" not in normalized
    assert foreign.read_text() == "# unrelated"

    # Re-run: versions INSIDE the folder, never next to it.
    result2 = _run(runner, video_path, downloads, resume=False)
    assert result2.exit_code == 0, result2.output
    assert (downloads / "demo_review_2" / "demo_report.json").exists()
    assert not (tmp_path / "Downloads_2").exists()


def test_output_existing_review_dir_still_versions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o`` naming a real previous review keeps the historical behaviour:
    the path is the review dir itself and a re-run versions it (``_2``)."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    run_dir = tmp_path / "my-run"

    assert _run(runner, video_path, run_dir, resume=False).exit_code == 0
    assert (run_dir / "demo_report.json").exists()  # non-existent -> used as-is

    result = _run(runner, video_path, run_dir, resume=False)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "my-run_2" / "demo_report.json").exists()
    assert not (run_dir / "demo_review").exists()


def test_output_existing_review_dir_still_prompts_on_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real review dir passed via ``-o`` still gets the rerun prompt on a TTY."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    run_dir = tmp_path / "my-run"
    assert _run(runner, video_path, run_dir, resume=False).exit_code == 0

    prompted: list[bool] = []

    def _answer(*a: object, **kw: object) -> str:
        prompted.append(True)
        return "o"

    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: True)
    monkeypatch.setattr("screenscribe.review_pipeline.Prompt.ask", _answer)

    assert _run(runner, video_path, run_dir, resume=False).exit_code == 0
    assert prompted == [True]
    assert not (tmp_path / "my-run_2").exists()
    assert not (run_dir / "demo_review").exists()


def test_output_folder_with_checkpoint_is_a_review_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A folder holding ``.screenscribe_cache`` is a screenscribe review dir, so
    ``-o`` uses it directly instead of nesting a new review inside it."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    run_dir = tmp_path / "partial"
    (run_dir / ".screenscribe_cache").mkdir(parents=True)

    assert _run(runner, video_path, run_dir, resume=False).exit_code == 0
    assert (run_dir / "demo_report.json").exists()
    assert not (run_dir / "demo_review").exists()


def test_batch_output_layout_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Batch mode keeps writing ``<output>/<stem>_review`` per video."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    second = tmp_path / "second.mov"
    second.write_bytes(b"video2")
    out = tmp_path / "batch"
    out.mkdir()
    (out / "unrelated_report.md").write_text("# foreign")

    result = runner.invoke(
        cli_module.app,
        [
            "review",
            str(video_path),
            str(second),
            "-o",
            str(out),
            "--no-serve",
            "--skip-validation",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "demo_review" / "demo_report.json").exists()
    assert (out / "second_review" / "second_report.json").exists()


# --------------------------------------------------------------------------- #
# Output directory cannot be created: friendly error, non-zero exit.          #
# --------------------------------------------------------------------------- #


def _assert_friendly_output_error(result: object, reason: str) -> None:
    output = result.output  # type: ignore[attr-defined]
    normalized = " ".join(output.split())
    assert result.exit_code == 1, output  # type: ignore[attr-defined]
    assert result.exception is None or isinstance(  # type: ignore[attr-defined]
        result.exception,  # type: ignore[attr-defined]
        SystemExit,
    ), result.exception  # type: ignore[attr-defined]
    assert "Traceback" not in output
    assert "Output Directory Error" in normalized
    assert reason in normalized
    assert "-o" in normalized


def test_output_under_a_file_exits_with_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o some_file/sub`` -> NotADirectoryError becomes a clear message + exit 1."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")

    result = _run(runner, video_path, blocker / "sub", resume=False)
    _assert_friendly_output_error(result, "a file (not a folder) already exists")


def test_output_is_an_existing_file_exits_with_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o existing_file`` -> FileExistsError becomes a clear message + exit 1."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    target = tmp_path / "taken"
    target.write_text("i am a file")

    result = _run(runner, video_path, target, resume=False)
    _assert_friendly_output_error(result, "a file (not a folder) already exists")


def test_output_permission_denied_exits_with_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``-o`` under an unwritable folder -> PermissionError, no traceback."""
    import os

    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("needs a non-root POSIX user for permission checks")
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        result = _run(runner, video_path, locked / "review", resume=False)
    finally:
        locked.chmod(0o700)
    _assert_friendly_output_error(result, "permission denied")


def test_output_read_only_filesystem_reason() -> None:
    """EROFS (read-only volume) is named as such in the message."""
    import errno

    from screenscribe.cli_messages import _build_output_dir_error_message

    exc = OSError(errno.EROFS, "Read-only file system", "/Volumes/ro/out")
    message = _build_output_dir_error_message(Path("/Volumes/ro/out"), exc)
    assert "read-only" in message
    assert "/Volumes/ro/out" in message


# --------------------------------------------------------------------------- #
# Version slots: an existing non-empty <base>_N is never reused.              #
# --------------------------------------------------------------------------- #


def _completed_base(tmp_path: Path) -> Path:
    base = tmp_path / "demo_review"
    base.mkdir()
    (base / "demo_report.json").write_text("{}")
    return base


def test_version_slot_skips_ordinary_non_empty_folder(tmp_path: Path) -> None:
    """``demo_review_2`` holds foreign files (not a bundle): it is occupied, so the
    rerun goes to ``_3`` instead of mixing its output into that folder."""
    base = _completed_base(tmp_path)
    occupied = tmp_path / "demo_review_2"
    occupied.mkdir()
    (occupied / "notes.md").write_text("# someone else's notes")

    assert cli_module._find_next_review_path(base, video_stem="demo") == (
        tmp_path / "demo_review_3",
        3,
    )


def test_version_slot_reuses_empty_folder(tmp_path: Path) -> None:
    base = _completed_base(tmp_path)
    (tmp_path / "demo_review_2").mkdir()

    assert cli_module._find_next_review_path(base, video_stem="demo") == (
        tmp_path / "demo_review_2",
        2,
    )


def test_version_slot_skips_existing_file(tmp_path: Path) -> None:
    base = _completed_base(tmp_path)
    (tmp_path / "demo_review_2").write_text("a file, not a folder")

    assert cli_module._find_next_review_path(base, video_stem="demo") == (
        tmp_path / "demo_review_3",
        3,
    )


def test_checkpoint_only_base_is_still_reused_in_place(tmp_path: Path) -> None:
    """The narrow bundle rule still applies to the base itself: a partial run with
    only a checkpoint (no report) is reused, not version-bumped."""
    base = tmp_path / "demo_review"
    (base / ".screenscribe_cache").mkdir(parents=True)

    assert cli_module._find_next_review_path(base, video_stem="demo") == (base, None)


# --------------------------------------------------------------------------- #
# Foreign base slots: never written into, reused, or cleaned.                 #
# --------------------------------------------------------------------------- #


def _run_default(runner: CliRunner, video_path: Path, *extra: str) -> object:
    return runner.invoke(
        cli_module.app, ["review", str(video_path), "--no-serve", "--skip-validation", *extra]
    )


def test_foreign_base_folder_allocates_next_slot_and_is_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A user-created non-empty ``demo_review`` next to the video is not ours: the
    review goes to ``demo_review_2``; the foreign folder stays byte-identical and
    gets no checkpoint cache. No Overwrite/Resume prompt is offered for it."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    foreign = tmp_path / "demo_review"
    foreign.mkdir()
    notes = foreign / "notes.txt"
    notes.write_bytes(b"my own notes")
    monkeypatch.setattr("screenscribe.review_pipeline._stdin_is_tty", lambda: True)

    def _no_prompt(*a: object, **kw: object) -> str:
        raise AssertionError("a foreign base must not trigger the rerun prompt")

    monkeypatch.setattr("screenscribe.review_pipeline.Prompt.ask", _no_prompt)

    result = _run_default(runner, video_path)

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert (tmp_path / "demo_review_2" / "demo_report.json").exists()
    assert sorted(p.name for p in foreign.iterdir()) == ["notes.txt"]
    assert notes.read_bytes() == b"my own notes"


def test_foreign_base_and_foreign_second_slot_allocates_third(tmp_path: Path) -> None:
    base = tmp_path / "demo_review"
    base.mkdir()
    (base / "notes.txt").write_text("foreign")
    second = tmp_path / "demo_review_2"
    second.mkdir()
    (second / "other.txt").write_text("foreign too")

    assert cli_module._find_next_review_path(base, video_stem="demo") == (
        tmp_path / "demo_review_3",
        3,
    )


def test_empty_base_folder_is_used(tmp_path: Path) -> None:
    base = tmp_path / "demo_review"
    base.mkdir()

    assert cli_module._find_next_review_path(base, video_stem="demo") == (base, None)


def test_foreign_base_file_is_skipped_by_allocator(tmp_path: Path) -> None:
    base = tmp_path / "demo_review"
    base.write_text("a file where the review folder would go")

    assert cli_module._find_next_review_path(base, video_stem="demo") == (
        tmp_path / "demo_review_2",
        2,
    )


def test_version_cap_exhausted_is_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No free slot below the cap -> Output Directory Error panel, exit 1, no traceback."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_module, "MAX_REVIEW_VERSIONS", 3)
    for name in ("demo_review", "demo_review_2", "demo_review_3"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "demo_report.json").write_text("{}")

    result = _run_default(runner, video_path)
    output = result.output  # type: ignore[attr-defined]
    normalized = " ".join(output.split())

    assert result.exit_code == 1, output  # type: ignore[attr-defined]
    assert result.exception is None or isinstance(result.exception, SystemExit)  # type: ignore[attr-defined]
    assert "Traceback" not in output
    assert "Output Directory Error" in normalized
    assert "Too many existing versions of demo_review (limit 3)" in normalized
    assert not (tmp_path / "demo_review_4").exists()


# --------------------------------------------------------------------------- #
# --force: only screenscribe's own review folder (or a free slot).            #
# --------------------------------------------------------------------------- #


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _panel_text(output: str) -> str:
    """Collapse a Rich panel to plain words (drop borders, join wrapped lines)."""
    borderless = "".join(" " if ch in "│╭╮╰╯─" else ch for ch in output)
    return " ".join(borderless.split())


def _assert_force_refused(result: object) -> None:
    output = result.output  # type: ignore[attr-defined]
    normalized = _panel_text(output)
    assert result.exit_code == 1, output  # type: ignore[attr-defined]
    assert "Traceback" not in output
    assert "Output Directory Error" in normalized
    assert "is not a screenscribe review folder" in normalized
    assert "--force only overwrites a previous screenscribe review" in normalized


def test_force_refuses_foreign_folder_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    foreign = tmp_path / "demo_review"
    (foreign / "sub").mkdir(parents=True)
    (foreign / "notes.txt").write_bytes(b"my notes")
    (foreign / "sub" / "data.bin").write_bytes(b"\x00\x01")
    before = _snapshot(foreign)

    def _no_rmtree(*a: object, **kw: object) -> None:
        raise AssertionError("--force must not delete anything in a foreign folder")

    monkeypatch.setattr("screenscribe.review_pipeline.shutil.rmtree", _no_rmtree)

    result = _run_default(runner, video_path, "--force")

    _assert_force_refused(result)
    assert _snapshot(foreign) == before
    assert not (foreign / ".screenscribe_cache").exists()
    assert not (tmp_path / "demo_review_2").exists()


def test_force_refuses_foreign_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    blocker = tmp_path / "demo_review"
    blocker.write_bytes(b"a file")

    result = _run_default(runner, video_path, "--force")

    _assert_force_refused(result)
    assert blocker.read_bytes() == b"a file"


def test_force_overwrites_own_complete_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A previous screenscribe review is still overwritten in place with --force."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    assert _run_default(runner, video_path).exit_code == 0  # type: ignore[attr-defined]
    assert (tmp_path / "demo_review" / "demo_report.json").exists()

    result = _run_default(runner, video_path, "--force")

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert not (tmp_path / "demo_review_2").exists()
    assert (tmp_path / "demo_review" / "demo_report.json").exists()


def test_force_cache_clear_failure_is_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    own = tmp_path / "demo_review"
    (own / ".screenscribe_cache").mkdir(parents=True)

    def _locked(path: object, *a: object, **kw: object) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("screenscribe.review_pipeline.shutil.rmtree", _locked)

    result = _run_default(runner, video_path, "--force")
    output = result.output  # type: ignore[attr-defined]
    normalized = _panel_text(output)

    assert result.exit_code == 1, output  # type: ignore[attr-defined]
    assert "Traceback" not in output
    assert "Output Directory Error" in normalized
    assert "Cannot clear the previous checkpoint cache" in normalized
    assert "Reason: permission denied" in normalized
    assert (own / ".screenscribe_cache").is_dir()


# --------------------------------------------------------------------------- #
# Reserving the output slot right before use (race + writability).            #
# --------------------------------------------------------------------------- #


def test_reserve_moves_on_when_slot_is_taken_after_classification(tmp_path: Path) -> None:
    """A foreign file appears at the chosen slot between classification and mkdir:
    the reserve step never writes into it and takes the reselected slot."""
    from screenscribe.cli_paths import reserve_review_output_slot

    chosen = tmp_path / "demo_review_2"
    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == chosen and not chosen.exists():
            chosen.write_bytes(b"someone else")  # the race: appears just before us
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    base = tmp_path / "demo_review"
    base.mkdir()
    (base / "demo_report.json").write_text("{}")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "mkdir", racing_mkdir)
        reserved = reserve_review_output_slot(
            chosen,
            "demo",
            reselect=lambda: cli_module._find_next_review_path(base, video_stem="demo")[0],
        )

    assert reserved == tmp_path / "demo_review_3"
    assert chosen.read_bytes() == b"someone else"
    assert reserved.is_dir()


def test_reserve_without_reselect_fails_closed_on_race(tmp_path: Path) -> None:
    from screenscribe.cli_paths import OutputSlotError, reserve_review_output_slot

    chosen = tmp_path / "demo_review"
    real_mkdir = Path.mkdir

    def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == chosen and not chosen.exists():
            chosen.write_bytes(b"someone else")
        real_mkdir(self, *args, **kwargs)  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "mkdir", racing_mkdir)
        with pytest.raises(OutputSlotError) as caught:
            reserve_review_output_slot(chosen, "demo")

    assert caught.value.kind == "foreign"
    assert chosen.read_bytes() == b"someone else"


def test_review_race_on_allocated_slot_never_writes_into_foreign_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: the allocated ``_2`` is taken by a foreign file after allocation;
    the review lands in ``_3`` and the foreign bytes are untouched."""
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    assert _run_default(runner, video_path).exit_code == 0  # type: ignore[attr-defined]
    slot_2 = tmp_path / "demo_review_2"
    real_find = cli_module._find_next_review_path
    calls = {"n": 0}

    def find_then_race(base: Path, video_stem: str | None = None) -> tuple[Path, int | None]:
        result = real_find(base, video_stem=video_stem)
        calls["n"] += 1
        if calls["n"] == 1 and result[0] == slot_2:
            slot_2.write_bytes(b"raced in")
        return result

    monkeypatch.setattr(cli_module, "_find_next_review_path", find_then_race)

    result = _run_default(runner, video_path)

    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert slot_2.read_bytes() == b"raced in"
    assert (tmp_path / "demo_review_3" / "demo_report.json").exists()


@pytest.mark.parametrize("flag", ["--force", "--resume"])
def test_unwritable_existing_review_dir_is_a_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str
) -> None:
    runner, video_path, _ = _success_harness(monkeypatch, tmp_path)
    own = tmp_path / "demo_review"
    (own / ".screenscribe_cache").mkdir(parents=True)
    (own / "demo_report.json").write_text("{}")
    before = sorted(str(p.relative_to(own)) for p in own.rglob("*"))

    def _denied(*args: object, **kwargs: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("screenscribe.cli_paths.tempfile.NamedTemporaryFile", _denied)

    result = _run_default(runner, video_path, flag)
    output = result.output  # type: ignore[attr-defined]
    normalized = _panel_text(output)

    assert result.exit_code == 1, output  # type: ignore[attr-defined]
    assert "Traceback" not in output
    assert "Output Directory Error" in normalized
    assert "The output folder exists but cannot be written" in normalized
    assert "Reason: permission denied" in normalized
    assert sorted(str(p.relative_to(own)) for p in own.rglob("*")) == before
