"""C6.2: VLM partial-failure resume.

When the unified VLM stage only partially succeeds (some items come back, some
fail), the stage must NOT be marked complete, the surviving successes must be
persisted, and a later --resume must retry ONLY the residual (failed) items
instead of re-paying for the ones that already succeeded.

These tests drive the REAL run_review (via the `review` CLI command) with the
heavy steps stubbed, mirroring tests/test_review_empty_state.py. Report writers
are stubbed to no-ops so no `*_report.json/html` bundle lands in the base output
dir; this keeps `_find_next_review_path` from bumping to `_2` on the resume run,
which would otherwise hide the checkpoint (a separate, pre-existing path-version
interaction outside C6.2's file scope).
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


# Genuinely dissimilar summaries so the real similarity-dedup never merges them.
_DISTINCT_SUMMARIES = {
    0: "The save button does nothing when the user clicks it",
    1: "Dropdown menu overlaps the page footer on narrow viewports",
    2: "Search returns stale cached results after a hard refresh",
}


def _finding_for(detection: object) -> UnifiedFinding:
    """Build a genuine-looking finding whose key matches the screenshot key."""
    seg = detection.segment  # type: ignore[attr-defined]
    return UnifiedFinding(
        detection_id=seg.id,
        screenshot_path=None,
        timestamp=seg.start,
        category="bug",
        is_issue=True,
        sentiment="problem",
        severity="high",
        summary=_DISTINCT_SUMMARIES[seg.id],  # distinct -> no dedup merge
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


def _merge_all_into_first(findings: list[object]) -> list[object]:
    """Deterministic dedup stub: collapse every finding into the first, growing
    its ``merged_from_ids`` so coverage is preserved while the raw count drops.

    This mimics the real dedup/LLM-merge outcome (post-merge finding count <
    screenshot count) without depending on similarity thresholds.
    """
    if not findings:
        return findings
    survivor = findings[0]
    merged = list(survivor.merged_from_ids)  # type: ignore[attr-defined]
    for f in findings[1:]:
        merged.append((f.detection_id, f.timestamp))  # type: ignore[attr-defined]
        merged.extend(f.merged_from_ids)  # type: ignore[attr-defined]
    survivor.merged_from_ids = merged  # type: ignore[attr-defined]
    return [survivor]


def _install_common_stubs(
    monkeypatch: pytest.MonkeyPatch,
    config: ScreenScribeConfig,
    extracted_audio: Path,
) -> None:
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
    # No-op the report writers so no *_report.* bundle lands -> no version bump
    # on resume (keeps the checkpoint discoverable in the base dir).
    monkeypatch.setattr("screenscribe.cli.save_enhanced_json_report", lambda *a, **kw: None)
    # Stub the summary generators (paid LLM) reached on any success.
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_unified_summary", lambda *a, **kw: "summary"
    )
    monkeypatch.setattr(
        "screenscribe.review_pipeline.generate_visual_summary_unified", lambda *a, **kw: "visual"
    )


def _run(runner: CliRunner, video_path: Path, output_dir: Path, *, resume: bool) -> object:
    args = ["review", str(video_path), "-o", str(output_dir), "--no-serve", "--skip-validation"]
    if resume:
        args.append("--resume")
    return runner.invoke(cli_module.app, args)


def test_partial_fail_then_resume_retries_only_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A1+A2+A3+A4: run 1 fails 1 of 3 items (stage stays unmarked, 2 successes
    persisted); --resume retries ONLY the 1 residual item and completes."""
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

    _install_common_stubs(monkeypatch, config, extracted_audio)

    def shots(video: object, detections: list[object], _dir: object) -> list[object]:
        return [(d, tmp_path / f"shot-{d.segment.id}.jpg") for d in detections]

    monkeypatch.setattr("screenscribe.review_pipeline.extract_screenshots_for_detections", shots)

    analyze_call_sizes: list[int] = []
    report_finding_counts: list[int] = []

    def fake_unified(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        analyze_call_sizes.append(len(screenshots))
        if len(analyze_call_sizes) == 1:
            # First run: drop the LAST item (simulate one failed VLM item).
            return [_finding_for(d) for (d, _p) in screenshots[:-1]]
        # Resume run: succeed everything we were handed (the residual).
        return [_finding_for(d) for (d, _p) in screenshots]

    monkeypatch.setattr("screenscribe.review_pipeline.analyze_all_findings_unified", fake_unified)

    def capture_report(*a: object, **kw: object) -> None:
        report_finding_counts.append(len(kw.get("unified_findings", [])))

    monkeypatch.setattr("screenscribe.cli._write_report_artifacts", capture_report)

    # --- Run 1: partial failure ---
    result1 = _run(runner, video_path, output_dir, resume=False)
    assert result1.exit_code == 0, result1.output

    checkpoint = load_checkpoint(output_dir)
    assert checkpoint is not None, "partial run must keep the checkpoint"
    # A1: stage NOT marked complete -> resume routes back to it.
    assert not checkpoint.is_stage_complete("unified_analysis")
    assert checkpoint.get_next_stage() == "unified_analysis"
    # A2: exactly the 2 successes persisted, with the right keys.
    assert len(checkpoint.unified_findings) == 2
    persisted_ids = {f["detection_id"] for f in checkpoint.unified_findings}
    assert persisted_ids == {0, 1}
    # Screenshots NOT pruned on partial -> all 3 survive for residual retry.
    assert len(checkpoint.screenshots) == 3
    assert report_finding_counts == [2]

    # --- Run 2: resume retries only the residual ---
    result2 = _run(runner, video_path, output_dir, resume=True)
    assert result2.exit_code == 0, result2.output

    # A3: the second analyze call received ONLY the 1 failed item.
    assert analyze_call_sizes == [3, 1], analyze_call_sizes
    # A4: final report has all 3 findings (2 restored + 1 retried).
    assert report_finding_counts[-1] == 3
    # A4: stage now complete -> checkpoint deleted on full success.
    assert load_checkpoint(output_dir) is None


def test_resume_still_partial_keeps_progress_and_stays_unmarked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A5: if the resume run still fails the residual, the stage stays unmarked,
    the earlier successes are not lost, and another resume can still retry."""
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

    _install_common_stubs(monkeypatch, config, extracted_audio)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections",
        lambda video, detections, _dir: [
            (d, tmp_path / f"shot-{d.segment.id}.jpg") for d in detections
        ],
    )
    monkeypatch.setattr("screenscribe.cli._write_report_artifacts", lambda *a, **kw: None)

    analyze_call_sizes: list[int] = []

    def always_drop_last(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        analyze_call_sizes.append(len(screenshots))
        # Never analyze the last residual item -> permanently partial.
        return [_finding_for(d) for (d, _p) in screenshots[:-1]]

    monkeypatch.setattr(
        "screenscribe.review_pipeline.analyze_all_findings_unified", always_drop_last
    )

    # Run 1: 3 in, 2 out.
    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    cp1 = load_checkpoint(output_dir)
    assert cp1 is not None and len(cp1.unified_findings) == 2

    # Run 2 (resume): residual is 1, still fails -> 0 new, successes preserved.
    assert _run(runner, video_path, output_dir, resume=True).exit_code == 0
    assert analyze_call_sizes == [3, 1], analyze_call_sizes
    cp2 = load_checkpoint(output_dir)
    assert cp2 is not None, "still-partial resume must keep the checkpoint"
    assert not cp2.is_stage_complete("unified_analysis")
    # Progress preserved: the 2 earlier successes are not lost.
    assert len(cp2.unified_findings) == 2
    assert {f["detection_id"] for f in cp2.unified_findings} == {0, 1}


def test_resume_after_dedup_completes_not_forever_partial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#6: when a partial run's surviving successes were deduplicated/merged, a
    resume that covers the residual must COMPLETE, not stay partial forever.

    Progress is measured by covered detection keys (own + merged_from_ids), not
    by the post-merge finding count. A merged finding represents every screenshot
    in its merged_from_ids, so once the residual is covered the stage is done and
    the checkpoint is deleted.
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

    _install_common_stubs(monkeypatch, config, extracted_audio)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections",
        lambda video, detections, _dir: [
            (d, tmp_path / f"shot-{d.segment.id}.jpg") for d in detections
        ],
    )
    monkeypatch.setattr("screenscribe.cli._write_report_artifacts", lambda *a, **kw: None)
    # Force a real-looking merge: collapse all findings into one, preserving
    # coverage via merged_from_ids (post-merge count < screenshot count).
    monkeypatch.setattr("screenscribe.review_pipeline.deduplicate_findings", _merge_all_into_first)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.llm_merge_findings", lambda findings, _config: findings
    )

    analyze_call_sizes: list[int] = []

    def fail_last_then_succeed(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        analyze_call_sizes.append(len(screenshots))
        if len(analyze_call_sizes) == 1:
            # Run 1: items 0 and 1 succeed (and get merged), item 2 fails.
            return [_finding_for(d) for (d, _p) in screenshots[:-1]]
        # Resume: the residual item now succeeds -> every key is covered.
        return [_finding_for(d) for (d, _p) in screenshots]

    monkeypatch.setattr(
        "screenscribe.review_pipeline.analyze_all_findings_unified", fail_last_then_succeed
    )

    # --- Run 1: partial; the 2 successes merge into ONE finding (covers 0,1). ---
    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    cp1 = load_checkpoint(output_dir)
    assert cp1 is not None, "partial run must keep the checkpoint"
    assert not cp1.is_stage_complete("unified_analysis")
    # Merge collapsed 2 successes into 1 persisted finding (raw count < requested).
    assert len(cp1.unified_findings) == 1
    merged = cp1.unified_findings[0]
    covered = {(merged["detection_id"], merged["timestamp"])} | {
        tuple(m) for m in merged["merged_from_ids"]
    }
    assert covered == {(0, 0.0), (1, 2.0)}

    # --- Run 2 (resume): residual (item 2) succeeds -> all keys covered. ---
    assert _run(runner, video_path, output_dir, resume=True).exit_code == 0
    # Residual was exactly the 1 uncovered item.
    assert analyze_call_sizes == [3, 1], analyze_call_sizes
    # Coverage complete -> stage marked done -> checkpoint deleted. Pre-fix this
    # stayed partial forever because post-merge count (2) < requested (3).
    assert load_checkpoint(output_dir) is None


def test_resume_genuinely_missing_screenshot_stays_partial_even_with_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#6 negative: a screenshot covered by NO finding (own key or merged_from_ids)
    must keep the stage partial, even when the surviving successes were merged.

    Proves the covered-keys logic does not over-correct into always-complete.
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

    _install_common_stubs(monkeypatch, config, extracted_audio)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.extract_screenshots_for_detections",
        lambda video, detections, _dir: [
            (d, tmp_path / f"shot-{d.segment.id}.jpg") for d in detections
        ],
    )
    monkeypatch.setattr("screenscribe.cli._write_report_artifacts", lambda *a, **kw: None)
    monkeypatch.setattr("screenscribe.review_pipeline.deduplicate_findings", _merge_all_into_first)
    monkeypatch.setattr(
        "screenscribe.review_pipeline.llm_merge_findings", lambda findings, _config: findings
    )

    analyze_call_sizes: list[int] = []

    def always_drop_last(screenshots: list[object], *a: object, **kw: object) -> list[object]:
        analyze_call_sizes.append(len(screenshots))
        # The last item never comes back -> key 2 stays genuinely uncovered.
        return [_finding_for(d) for (d, _p) in screenshots[:-1]]

    monkeypatch.setattr(
        "screenscribe.review_pipeline.analyze_all_findings_unified", always_drop_last
    )

    # Run 1: items 0,1 succeed and merge into one finding; item 2 fails.
    assert _run(runner, video_path, output_dir, resume=False).exit_code == 0
    cp1 = load_checkpoint(output_dir)
    assert cp1 is not None and len(cp1.unified_findings) == 1

    # Run 2 (resume): residual item 2 still fails -> key 2 remains uncovered.
    assert _run(runner, video_path, output_dir, resume=True).exit_code == 0
    assert analyze_call_sizes == [3, 1], analyze_call_sizes
    cp2 = load_checkpoint(output_dir)
    assert cp2 is not None, "a genuinely missing screenshot must keep the checkpoint"
    assert not cp2.is_stage_complete("unified_analysis")
