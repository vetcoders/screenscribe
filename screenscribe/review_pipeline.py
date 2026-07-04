"""Per-video orchestration for the screenscribe ``review`` command.

Extracted from ``cli.review``'s ~720-LOC per-video for-loop. ``run_review``
owns the stage orchestration (audio -> transcribe + fallback -> detection ->
screenshots -> unified VLM -> report), checkpoint restore/save, the empty-state
and dry-run branches, batch response-id chaining, ``pipeline_errors`` and the
optional local serve at the end.

Monkeypatch contract (load-bearing): every pipeline-step that tests patch on
``screenscribe.cli`` is called **through the cli module object** (``cli.<name>``)
so ``monkeypatch.setattr("screenscribe.cli.transcribe_audio", ...)`` etc. still
bind. The cli<->review_pipeline import cycle is broken with a function-local
``import screenscribe.cli as cli`` inside ``run_review`` (same pattern
``analyze()`` / ``_serve_report`` already use).
"""

import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
from rich.panel import Panel
from rich.prompt import Prompt

from . import __version__
from .api_utils import APIError
from .checkpoint import (
    PipelineCheckpoint,
    checkpoint_valid_for_video,
    create_checkpoint,
    delete_checkpoint,
    deserialize_detection,
    deserialize_screenshot,
    deserialize_transcription,
    deserialize_unified_finding,
    get_checkpoint_path,
    load_checkpoint,
    save_checkpoint,
    serialize_detection,
    serialize_screenshot,
    serialize_transcription,
    serialize_unified_finding,
)
from .config import ScreenScribeConfig
from .detect import format_timestamp
from .keywords import KeywordsConfig
from .screenshots import extract_screenshots_for_detections
from .semantic_filter import (
    SemanticFilterResult,
    deduplicate_pois,
    pois_to_detections,
)
from .summary_fallback import generate_detection_executive_summary
from .transcribe import (
    MIN_TIMELINE_GUARD_VIDEO_SECONDS,
    MIN_TRANSCRIPT_TIMELINE_COVERAGE,
    calculate_transcript_timeline_coverage,
    filter_hallucinated_segments,
    transcript_last_segment_end,
    validate_audio_quality,
)
from .unified_analysis import (
    UnifiedFinding,
    analyze_all_findings_unified,
    deduplicate_findings,
    generate_unified_summary,
    generate_visual_summary_unified,
    llm_merge_findings,
)


def _stdin_is_tty() -> bool:
    """Whether stdin is an interactive terminal.

    Wrapped in a function so callers have a single, easily-patched seam for the
    interactive-vs-CI branch (``CliRunner`` swaps ``sys.stdin`` during a test
    invoke, so patching this is more reliable than patching ``sys.stdin``).
    """
    return sys.stdin.isatty()


def _prompt_rerun_action(base_output: Path, console: Any, *, allow_resume: bool) -> str:
    """Ask the user how to handle an existing review bundle (interactive only).

    Re-reviewing the same video currently auto-bumps the output directory to
    ``<stem>_review_2``, ``_3`` … which silently buries the prior bundle (and any
    resumable checkpoint) under an ever-growing pile of versions. On a real TTY we
    instead let the operator decide. Returns one of:

    - ``"overwrite"`` — replace the existing review in place,
    - ``"resume"``    — continue from the saved checkpoint (retry failed items),
    - ``"new"``       — keep the old review and create a new versioned copy
      (the historical auto-bump behaviour).

    ``allow_resume`` gates the [R]esume option: it is offered ONLY when a
    checkpoint actually exists in ``base_output``. A *completed* review deletes
    its checkpoint on success, so offering Resume there would let the user pick
    it and then silently overwrite the prior report (there is nothing to resume
    from) — exactly the bug this guard closes.

    Callers gate this on ``sys.stdin.isatty()``; under a pipe/CI the auto-bump
    default is used without prompting so behaviour stays deterministic.
    """
    resume_line = (
        "[bold]R[/]esume — continue from the saved checkpoint (retry failed items)\n"
        if allow_resume
        else ""
    )
    console.print(
        Panel(
            f"[yellow]A previous review already exists at:[/] {base_output.name}\n\n"
            "[bold]O[/]verwrite — replace the existing review in place\n"
            f"{resume_line}"
            "[bold]N[/]ew — keep the old review, create a new versioned copy",
            title="[bold]Existing Review Found[/]",
            border_style="yellow",
        )
    )
    choices = ["o", "r", "n"] if allow_resume else ["o", "n"]
    choice = Prompt.ask(
        "How would you like to proceed?",
        choices=choices,
        default="n",
        console=console,
    ).lower()
    # ``.get(..., "new")`` keeps a defensive default: even if a "resume" answer
    # arrives when no checkpoint exists, we never fall through to a silent
    # overwrite — the call site treats it as the safe version-bump path.
    return {"o": "overwrite", "r": "resume", "n": "new"}.get(choice, "new")


def _has_valid_checkpoint(base_output: Path, video: Path, language: str) -> bool:
    """Whether ``base_output`` holds a checkpoint that is actually resumable.

    A checkpoint *file* existing is not enough to justify reusing the directory:
    ``load_checkpoint`` may reject it (corrupt/outdated schema) and
    ``checkpoint_valid_for_video`` may reject it (different video/language). If we
    pin ``video_output`` to ``base_output`` on mere presence and the checkpoint is
    then rejected downstream, the pipeline starts fresh and silently overwrites
    the prior bundle. Validate here so an invalid checkpoint falls back to the
    safe preserve-and-version-bump path -- consistent with the no-checkpoint gate.
    """
    if not get_checkpoint_path(base_output).exists():
        return False
    checkpoint = load_checkpoint(base_output)
    return bool(checkpoint and checkpoint_valid_for_video(checkpoint, video, base_output, language))


def _announce_new_version(console: Any, base_name: str, new_name: str) -> None:
    """Print the historical 'creating a new versioned copy' panel."""
    console.print(
        Panel(
            f"[yellow]Found previous review at:[/] {base_name}\n"
            f"[green]Creating new version:[/] {new_name}",
            title="[bold]Found Previous Review[/]",
            border_style="yellow",
        )
    )


def run_review(
    videos: list[Path],
    config: ScreenScribeConfig,
    *,
    output: Path | None,
    language: str,
    local: bool,
    vision: bool,
    json_report: bool,
    markdown_report: bool,
    html_report: bool,
    embed_video: bool,
    keywords: KeywordsConfig,
    resume: bool,
    force: bool,
    estimate: bool,
    dry_run: bool,
    serve: bool,
    port: int,
) -> None:
    """Run the per-video review pipeline for one or more videos.

    ``cli.review`` performs input/config/model validation and builds ``config``
    first, then delegates the entire stage orchestration here.
    """
    # Break the cli<->review_pipeline cycle and keep the monkeypatch surface:
    # every patchable step below is called as cli.<name>.
    import screenscribe.cli as cli

    console = cli.console

    # Detection is ALWAYS the LLM semantic prefilter. Keywords are injected into
    # that prefilter prompt as vocabulary hints (see semantic_prefilter); there
    # is no keyword-only / regex-detection mode.
    if config.analysis_prompt_override:
        console.print(
            "[yellow]Prompt override also affects semantic prefilter prompts in this session.[/]"
        )

    # Batch mode: show overview
    if len(videos) > 1:
        console.print(f"\n[bold cyan]Batch Mode:[/] {len(videos)} videos")
        for i, v in enumerate(videos, 1):
            console.print(f"  {i}. {v.name}")
        console.print("[dim]Videos will share context via response chaining[/]\n")

    # Track context across videos for chaining
    batch_context_response_id: str = ""

    # Track last processed video for --serve option
    last_output: Path | None = None
    last_video: Path | None = None

    # Process each video
    for video_idx, video in enumerate(videos):
        if len(videos) > 1:
            console.rule(f"[bold magenta]Video {video_idx + 1}/{len(videos)}: {video.name}[/]")

        # Setup output directory (per-video in batch mode)
        video_stem = video.stem  # Video name without extension for file naming
        if output is None:
            base_output = video.parent / f"{video_stem}_review"
        elif len(videos) > 1:
            # Batch mode with -o: use subdirectories
            base_output = output / f"{video_stem}_review"
        else:
            base_output = output

        # Handle existing reviews: append _2, _3, etc. unless --force.
        # A per-iteration copy of `resume`: the interactive [R]esume choice flips
        # it on for THIS video only so batch mode keeps each video's choice
        # isolated and never leaks a prompt answer to the next video.
        effective_resume = resume

        if force:
            video_output = base_output
        elif effective_resume and _has_valid_checkpoint(base_output, video, language):
            # C6.2b: --resume must continue in the directory that actually holds
            # the checkpoint. The version-bump guard (_find_next_review_path)
            # only exists to avoid clobbering a *completed* bundle when the user
            # did NOT ask to resume. But a partial/failed run keeps BOTH its
            # checkpoint AND a `*_report.*` bundle in base_output, so letting the
            # guard run on --resume would bump to a fresh `_2` dir that has no
            # checkpoint -- silently restarting from scratch and re-paying for
            # work that already succeeded. When a *valid* checkpoint exists here,
            # resume into base_output directly. Round-7 P1: gate on validity (not
            # mere presence) so a corrupt/outdated/wrong-video checkpoint falls
            # through to the preserve-and-version-bump path instead of being
            # pinned to base_output and then overwriting the prior bundle when
            # checkpoint_valid_for_video rejects it below.
            video_output = base_output
            console.print(
                "[dim]Resume: reusing existing review directory with checkpoint "
                f"({base_output.name})[/]"
            )
        else:
            video_output, version = cli._find_next_review_path(base_output)
            # A checkpoint only survives in base_output after a *partial*/failed
            # run; a completed run deletes it on success. Resume is only sound
            # when a *valid* one exists -- otherwise "resume" would start fresh in
            # base_output and silently overwrite the prior report. Round-7 P1:
            # validate, don't just check presence, so the [R]esume option is never
            # offered for a checkpoint that would be rejected downstream.
            checkpoint_present = _has_valid_checkpoint(base_output, video, language)
            if version and _stdin_is_tty():
                # RERUN-UX: a prior bundle exists and we are on a real terminal,
                # so let the operator choose instead of silently auto-bumping.
                action = _prompt_rerun_action(base_output, console, allow_resume=checkpoint_present)
                if action == "overwrite":
                    video_output = base_output
                elif action == "resume" and checkpoint_present:
                    effective_resume = True
                    video_output = base_output
                    console.print(
                        f"[dim]Resume: reusing existing review directory ({base_output.name})[/]"
                    )
                else:
                    # "new", OR a "resume" with no checkpoint to resume from. The
                    # latter must NOT overwrite the completed bundle in place:
                    # fall back to the version-bump path and say so explicitly.
                    if action == "resume" and not checkpoint_present:
                        console.print(
                            "[yellow]Nothing to resume:[/] no checkpoint exists for "
                            f"{base_output.name}; keeping the previous review and "
                            "creating a new versioned copy instead of overwriting it."
                        )
                    _announce_new_version(console, base_output.name, video_output.name)
            elif version:
                # non-TTY / CI: deterministic auto-bump, no prompt.
                _announce_new_version(console, base_output.name, video_output.name)

        video_output.mkdir(parents=True, exist_ok=True)

        console.print(f"\n[blue]Video:[/] [link=file://{video}]{video}[/link]")
        console.print(f"[blue]Output:[/] [link=file://{video_output}]{video_output}[/link]")
        console.print(f"[blue]Visual (VLM) analysis:[/] {'✓' if vision else '✗'}")
        console.print("[blue]Detection:[/] semantic pre-filter (LLM)")
        if batch_context_response_id:
            console.print("[blue]Context:[/] Chained from previous video")

        # Get video duration
        duration = 0.0
        try:
            duration = cli.get_video_duration(video)
            console.print(f"[blue]Duration:[/] {format_timestamp(duration)}\n")
        except RuntimeError:
            console.print("[yellow]Could not determine video duration[/]\n")

        # --estimate mode: show time estimates and exit
        if estimate:
            cli._show_estimate(duration, vision)
            continue  # Continue to next video in batch mode

        # Handle --force: delete existing checkpoint
        if force:
            cache_dir = video_output / ".screenscribe_cache"
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
                console.print(
                    "[yellow]Force mode:[/] Deleted existing checkpoint, starting fresh\n"
                )

        # Check for existing checkpoint
        checkpoint: PipelineCheckpoint | None = None
        if effective_resume and not force:
            checkpoint = load_checkpoint(video_output)
            if checkpoint and checkpoint_valid_for_video(checkpoint, video, video_output, language):
                console.print(
                    f"[green]Resuming from checkpoint:[/] "
                    f"{len(checkpoint.completed_stages)} stages complete"
                )
                console.print(f"[dim]Completed: {', '.join(checkpoint.completed_stages)}[/]\n")
            else:
                checkpoint = None
                console.print("[dim]No valid checkpoint found, starting fresh[/]\n")

        # Create new checkpoint if not resuming
        if checkpoint is None:
            checkpoint = create_checkpoint(video, video_output, language)

        # Initialize variables from checkpoint or fresh
        transcription = None
        detections: list[Any] = []
        screenshots: list[Any] = []
        executive_summary = ""
        visual_summary = ""
        pipeline_errors: list[dict[str, str]] = []  # Collect errors for best-effort processing
        # True when vision was REQUESTED but skipped for a missing key: the run
        # is incomplete (VLM never ran), so the checkpoint must be kept truthful
        # and resumable instead of being deleted on success (see Step 5 / Step 6).
        vision_skipped_no_key = False
        # True when the unified VLM stage raised a hard failure (P2-1 + SYS-3):
        # the stage did NOT complete, so the checkpoint must be kept and
        # unified_analysis left unmarked so --resume re-runs it.
        unified_failed = False
        # True when the unified VLM stage only PARTIALLY succeeded (C6.2): some
        # items came back, some failed. The stage is not complete, so it must
        # stay unmarked and the surviving successes persisted, so --resume retries
        # ONLY the residual (failed) items instead of pretending the stage is done.
        unified_partial = False

        # Restore state from checkpoint
        if checkpoint.transcription:
            transcription = deserialize_transcription(checkpoint.transcription)
        if checkpoint.detections:
            detections = [deserialize_detection(d) for d in checkpoint.detections]
        if checkpoint.screenshots:
            screenshots = [deserialize_screenshot(s) for s in checkpoint.screenshots]
        executive_summary = checkpoint.executive_summary
        visual_summary = checkpoint.visual_summary

        # Step 1: Extract audio
        if not checkpoint.is_stage_complete("audio"):
            console.rule("[bold]Step 1: Audio Extraction[/]")
            audio_path = cli._extract_audio_or_exit(video)
            checkpoint.mark_stage_complete("audio")
            save_checkpoint(checkpoint, video_output)
            console.print()
        else:
            console.print("[dim]Step 1: Audio Extraction - skipped (cached)[/]")
            # Audio is extracted to temp location - need to re-extract if not found
            # This is fine since audio extraction is fast
            audio_path = cli._extract_audio_or_exit(video)

        # Step 2: Transcribe
        if not checkpoint.is_stage_complete("transcription"):
            console.rule("[bold]Step 2: Transcription[/]")
            try:
                # Chunked entry point: single-shot for short audio, silence-aware
                # chunking for long recordings (keeps STT timestamps accurate).
                transcription = cli.transcribe_audio_chunked(
                    audio_path,
                    language=language,
                    use_local=local,
                    api_key=config.get_stt_api_key(),
                    stt_endpoint=config.stt_endpoint,
                    stt_model=config.stt_model,
                )
            except (
                httpx.HTTPStatusError,
                httpx.RequestError,
                APIError,
                ValueError,
                RuntimeError,
            ) as exc:
                # Primary STT failed (e.g. 429 capacity limit, or an unexpected
                # payload shape raised as RuntimeError in transcribe.py). If the
                # user opted into a fallback STT provider, try it before giving up.
                transcription = None
                if config.has_stt_fallback():
                    status = (
                        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    )
                    short = f"HTTP {status}" if status else type(exc).__name__
                    console.print()
                    console.print(
                        f"[yellow]Primary STT failed ({short}); "
                        "trying configured fallback endpoint...[/]"
                    )
                    try:
                        transcription = cli.transcribe_audio_chunked(
                            audio_path,
                            language=language,
                            use_local=False,
                            api_key=config.get_stt_fallback_api_key(),
                            stt_endpoint=config.stt_fallback_endpoint,
                            stt_model=config.get_stt_fallback_model(),
                        )
                    except (
                        httpx.HTTPStatusError,
                        httpx.RequestError,
                        APIError,
                        ValueError,
                        RuntimeError,
                    ) as fallback_exc:
                        exc = fallback_exc  # report the fallback's failure
                        transcription = None

                if transcription is None:
                    # Surface actionable guidance instead of a raw traceback, and
                    # keep the checkpoint so --resume can retry without re-extracting.
                    console.print()
                    console.print(
                        Panel(
                            cli._build_transcription_failure_message(exc),
                            title="[bold red]Transcription Failed[/]",
                            border_style="red",
                        )
                    )
                    console.print(
                        "[yellow]Skipping this video.[/] Extracted audio is kept; "
                        "re-run with [bold]--resume[/] to retry."
                    )
                    continue  # Skip to next video in batch
            # Drop no-speech hallucinations (outros Whisper invents on music /
            # silence) before they reach the transcript, checkpoint and report.
            # Runs once here so the checkpoint stores the cleaned transcript and
            # --resume stays consistent (FW-09).
            transcription = filter_hallucinated_segments(
                transcription,
                duration if duration > 0 else None,
                verbose=config.verbose,
            )
            checkpoint.transcription = serialize_transcription(transcription)
            checkpoint.mark_stage_complete("transcription")
            save_checkpoint(checkpoint, video_output)
            # Transcript is now embedded in the MD report (no separate file)
        else:
            console.print("[dim]Step 2: Transcription - skipped (cached)[/]")
            if transcription is None and checkpoint.transcription:
                transcription = deserialize_transcription(checkpoint.transcription)

        if transcription is None:
            console.print("[red]Error: No transcription available[/]")
            continue  # Skip to next video in batch

        # Validate audio quality before proceeding
        is_valid, validation_message, is_warning = validate_audio_quality(transcription)
        if validation_message:
            console.print()
            if is_valid and is_warning:
                console.print(
                    Panel(
                        validation_message,
                        title="[bold yellow]Audio Quality Warning[/]",
                        border_style="yellow",
                    )
                )
                console.print()
            elif not is_valid:
                console.print(
                    Panel(
                        validation_message,
                        title="[bold red]Audio Quality Issue[/]",
                        border_style="red",
                    )
                )
                console.print()
                console.print(
                    "[yellow]Processing stopped.[/] Please fix the audio issue and try again."
                )
                console.print("[dim]If you believe this is a false positive, please report it.[/]")
                delete_checkpoint(video_output)
                continue  # Skip to next video in batch

        coverage = calculate_transcript_timeline_coverage(transcription, duration)
        if (
            duration > MIN_TIMELINE_GUARD_VIDEO_SECONDS
            and coverage is not None
            and coverage < MIN_TRANSCRIPT_TIMELINE_COVERAGE
        ):
            # Low coverage on a long video has two very different causes:
            #   (a) the narrator simply stopped talking before the video ended
            #       -> transcript is complete for the spoken part, screenshots align;
            #   (b) STT dropped/compressed the tail -> end-of-video screenshots drift.
            # The audio tail tells them apart. Either way we keep going (and keep the
            # checkpoint) rather than discarding a completed transcription.
            last_end = transcript_last_segment_end(transcription) or 0.0
            console.print()
            if cli.tail_is_silent(audio_path, last_end, duration) is True:
                silent_tail = format_timestamp(duration - last_end)
                narration_end = format_timestamp(last_end)
                console.print(
                    f"[dim]Note: last {silent_tail} of audio is silent "
                    f"(narration ended at {narration_end}). "
                    "Screenshots cover the narrated portion.[/]"
                )
            else:
                console.print(
                    Panel(
                        cli._build_transcript_timeline_coverage_message(transcription, duration),
                        title="[bold yellow]Transcript Timeline Warning[/]",
                        border_style="yellow",
                    )
                )
            console.print()

        # Step 3: Issue Detection -- ALWAYS the LLM semantic prefilter.
        # Active keywords are injected into the prefilter prompt as vocabulary
        # hints (see semantic_prefilter); there is no keyword-only mode.
        pois = []  # Points of interest from semantic pre-filter

        if not checkpoint.is_stage_complete("detection"):
            console.rule("[bold]Step 3: Issue Detection[/]")

            console.print("[cyan]Using semantic pre-filter (analyzing entire transcript)[/]")
            # Chain from STT → semantic filter → VLM
            stt_context = transcription.response_id or batch_context_response_id
            filter_result: SemanticFilterResult = cli.semantic_prefilter(
                transcription,
                config,
                previous_response_id=stt_context,
                keywords=keywords,
            )

            if filter_result.failed:
                # The LLM detection stage failed (auth/network/rate-limit). Treat
                # it like the STT failure path: surface actionable guidance, keep
                # the checkpoint (transcript stays) WITHOUT marking detection
                # complete, and skip -- never write a confident "no issues"
                # report from a stage that never actually ran.
                detail = filter_result.error or "the language model could not be reached"
                console.print()
                console.print(
                    Panel(
                        "The semantic pre-filter (the LLM detection stage) failed, so no "
                        "findings could be produced.\n\n"
                        f"[dim]Reason:[/] {detail}\n\n"
                        "This is usually transient -- a rate limit, a network drop, or an "
                        "invalid/expired API key. Your transcript was saved.",
                        title="[bold red]Issue Detection Failed[/]",
                        border_style="red",
                    )
                )
                console.print(
                    "[yellow]Skipping this video.[/] Re-run with [bold]--resume[/] to retry "
                    "without re-transcribing."
                )
                continue  # Skip to next video in batch

            pois = filter_result.pois
            # Deduplicate similar POIs before VLM analysis
            if pois and len(pois) > 1:
                original_count = len(pois)
                pois = deduplicate_pois(pois)
                if len(pois) < original_count:
                    console.print(f"[dim]  POI dedup: {original_count} → {len(pois)}[/]")
            # Chain semantic filter context to VLM analysis
            if filter_result.response_id:
                batch_context_response_id = filter_result.response_id
            if pois:
                # Convert POIs to Detection objects for compatibility
                detections = pois_to_detections(pois, transcription)
                console.print(
                    f"[green]Semantic pre-filter identified {len(detections)} findings[/]"
                )
            else:
                console.print("[yellow]Semantic pre-filter returned no points of interest[/]")
                detections = []

            checkpoint.detections = [serialize_detection(d) for d in detections]
            checkpoint.mark_stage_complete("detection")
            save_checkpoint(checkpoint, video_output)
            console.print()
        else:
            console.print("[dim]Step 3: Issue Detection - skipped (cached)[/]")

        if not detections:
            if dry_run:
                # Dry-run must not write artifacts or a final report; report the
                # zero-detection outcome plus the full-processing estimate, then stop.
                console.rule("[bold]Dry Run Results[/]")
                console.print(
                    "\n[green]Found 0 issues.[/] The semantic pre-filter "
                    "identified no points of interest."
                )
                console.print("\n[bold]Estimated time for full processing:[/]")
                cli._show_estimate(
                    duration,
                    vision,
                    detection_count=0,
                )
                console.print("\n[dim]Run without --dry-run to generate the empty-state report.[/]")
                delete_checkpoint(video_output)
                continue

            # Empty-state path: prefilter found nothing actionable, but still emit
            # the full set of report artifacts so the user gets a real deliverable
            # (transcript embedded + clear "no findings" summary) instead of a
            # silent exit with an empty review directory.
            segment_count = len(transcription.segments) if transcription else 0
            empty_summary = (
                "No issues detected. The transcript contained "
                f"{segment_count} segment(s) but the semantic pre-filter "
                "identified no points of interest."
            )

            console.print("[yellow]No issues detected in the video.[/]")
            console.print("[dim]Generating empty-state report so the transcript is preserved.[/]")
            console.print()
            console.rule("[bold]Step 6: Report Generation[/]")

            cli._write_report_artifacts(
                detections=detections,
                screenshots=screenshots,
                video=video,
                video_output=video_output,
                video_stem=video_stem,
                unified_findings=[],
                executive_summary=empty_summary,
                visual_summary="",
                errors=pipeline_errors,
                transcript=transcription.text if transcription else "",
                transcript_segments=transcription.segments if transcription else None,
                embed_video=embed_video,
                language=transcription.language if transcription else language,
                json_report=json_report,
                markdown_report=markdown_report,
                html_report=html_report,
            )

            console.print()
            console.print(
                Panel(empty_summary, title="[bold]Executive Summary[/]", border_style="yellow")
            )
            console.print()
            cli.print_report(detections, screenshots, video)

            delete_checkpoint(video_output)

            console.rule("[bold green]Finished successfully![/]")
            console.print()
            cli._print_report_artifact_paths(
                video_output=video_output,
                video_stem=video_stem,
                json_report=json_report,
                markdown_report=markdown_report,
                html_report=html_report,
            )
            console.print()
            console.rule(f"[dim]Screenscribe v{__version__} by Vetcoders[/]")

            last_video = video
            last_output = video_output
            continue

        # --dry-run mode: show detection results and estimates, then exit
        if dry_run:
            console.rule("[bold]Dry Run Results[/]")
            console.print(f"\n[green]Found {len(detections)} issues:[/]")
            console.print(f"  • {sum(1 for d in detections if d.category == 'bug')} bugs")
            console.print(f"  • {sum(1 for d in detections if d.category == 'change')} changes")
            console.print(f"  • {sum(1 for d in detections if d.category == 'ui')} UI issues")

            console.print("\n[bold]Sample detections:[/]")
            for i, d in enumerate(detections[:5], 1):
                console.print(
                    f"  {i}. [{d.category}] @ {format_timestamp(d.segment.start)}: "
                    f"{d.segment.text[:60]}..."
                )
            if len(detections) > 5:
                console.print(f"  ... and {len(detections) - 5} more")

            console.print("\n[bold]Estimated time for full processing:[/]")
            cli._show_estimate(
                duration,
                vision,
                detection_count=len(detections),
            )

            console.print("\n[dim]Run without --dry-run to process fully.[/]")
            delete_checkpoint(video_output)
            continue

        # Step 4: Extract screenshots
        if not checkpoint.is_stage_complete("screenshots"):
            console.rule("[bold]Step 4: Screenshot Extraction[/]")
            screenshots_dir = video_output / "screenshots"
            screenshots = extract_screenshots_for_detections(video, detections, screenshots_dir)
            checkpoint.screenshots = [serialize_screenshot(d, p) for d, p in screenshots]
            checkpoint.mark_stage_complete("screenshots")
            save_checkpoint(checkpoint, video_output)
            console.print()
        else:
            console.print("[dim]Step 4: Screenshot Extraction - skipped (cached)[/]")

        # BH50: detections exist but every screenshot was lost during extraction
        # (e.g. per-detection ffmpeg failures). Step 5 would call the unified
        # analysis with an empty list, which returns [] immediately; with
        # requested_unified_count == 0 the partial-fail branch below never fires,
        # so the user would get a silent transcript-only report with no hint that
        # the visual stage had nothing to work on. Surface it as an explicit
        # pipeline error instead.
        if detections and not screenshots:
            console.print(
                "[yellow]All screenshots failed to extract for the detected findings; "
                "the report will be transcript/detection-only.[/]"
            )
            pipeline_errors.append(
                {
                    "stage": "screenshots",
                    "message": (
                        f"Screenshot extraction produced no frames for "
                        f"{len(detections)} detected finding(s); visual (VLM) analysis "
                        "had nothing to analyze and the report is transcript/detection-only."
                    ),
                }
            )

        # Save basic JSON report immediately (before AI analysis)
        # This ensures we have results even if AI steps fail.
        # NOTE: this intentionally duplicates the JSON branch of
        # _write_report_artifacts as a pre-AI snapshot; do NOT dedupe.
        if json_report:
            cli.save_enhanced_json_report(
                detections,
                screenshots,
                video,
                video_output / f"{video_stem}_report.json",
                unified_findings=[],
                executive_summary="",
                errors=[],
                transcript=transcription.text if transcription else "",
                transcript_segments=transcription.segments if transcription else None,
            )
            console.print("[dim]Basic JSON report saved (AI analysis pending)[/]")

        # Step 5: Unified VLM Analysis - replaces separate semantic + vision
        # VLM analyzes both screenshot AND full transcript context together
        unified_findings: list[UnifiedFinding] = []

        if vision and config.get_vision_api_key():
            if not checkpoint.is_stage_complete("unified_analysis"):
                console.rule("[bold]Step 5: Unified VLM Analysis[/]")
                console.print("[cyan]Analyzing screenshots + transcript context together...[/]")
                # P2-1 + SYS-3: the unified stage previously marked itself complete
                # unconditionally even after the except below caught a hard failure
                # (auth/network/crash), so a later --resume trusted the stale
                # "completed" and skipped Step 5 -- a failed stage silently treated
                # as done. Track the failure and skip the mark, mirroring the STT /
                # detection / no-key pattern: keep the checkpoint (transcript /
                # detection / screenshots survive) WITHOUT marking unified_analysis
                # complete, so --resume actually re-runs visual analysis.
                # C6.2: a prior partial run persisted its successes to the
                # checkpoint WITHOUT marking the stage complete. On --resume,
                # restore those successes and re-run ONLY the residual (failed)
                # items, so we never re-pay for VLM calls that already succeeded.
                prior_unified_findings: list[UnifiedFinding] = []
                if checkpoint.unified_findings:
                    prior_unified_findings = [
                        deserialize_unified_finding(f) for f in checkpoint.unified_findings
                    ]
                done_keys: set[tuple[int, float]] = set()
                for prior in prior_unified_findings:
                    done_keys.add((prior.detection_id, prior.timestamp))
                    for orig_id, orig_ts in prior.merged_from_ids:
                        done_keys.add((orig_id, orig_ts))
                if done_keys:
                    residual_screenshots = [
                        (d, p)
                        for (d, p) in screenshots
                        if (d.segment.id, d.segment.start) not in done_keys
                    ]
                    if residual_screenshots:
                        console.print(
                            f"[cyan]Resuming partial VLM analysis: retrying "
                            f"{len(residual_screenshots)} previously-failed item(s); "
                            f"{len(prior_unified_findings)} kept from prior run.[/]"
                        )
                else:
                    residual_screenshots = list(screenshots)
                try:
                    # requested = the FULL set of screenshot keys; residual is
                    # what we still owe. Use COVERED detection keys (not the raw
                    # post-merge finding count) so partial detection stays honest
                    # across dedup/merge.
                    requested_keys: set[tuple[int, float]] = {
                        (d.segment.id, d.segment.start) for (d, _p) in screenshots
                    }
                    requested_unified_count = len(requested_keys)
                    new_unified_findings = analyze_all_findings_unified(
                        residual_screenshots,
                        config,
                        previous_response_id=batch_context_response_id,
                    )
                    # Merge restored successes with the newly-retried ones.
                    unified_findings = prior_unified_findings + new_unified_findings
                    # A deduplicated finding represents EVERY screenshot in its
                    # merged_from_ids, so each finding covers its own key plus all
                    # merged members. Counting raw findings would mark a fully
                    # covered resume as forever-partial once earlier successes were
                    # merged (post-merge count < screenshot count): the stage would
                    # never complete and the checkpoint would be retried forever.
                    # Composite (detection_id, timestamp) keys stay stable even for
                    # detection_id == 0 (G6c), since the timestamp disambiguates.
                    covered_keys: set[tuple[int, float]] = set()
                    for finding in unified_findings:
                        covered_keys.add((finding.detection_id, finding.timestamp))
                        covered_keys.update(finding.merged_from_ids)
                    missing_keys = requested_keys - covered_keys
                    if missing_keys:
                        # Partial: stage must NOT be marked complete; keep
                        # surviving successes so --resume retries only residual.
                        unified_partial = True
                        failed_count = len(missing_keys)
                        covered_unified_count = requested_unified_count - failed_count
                        if covered_unified_count == 0:
                            console.print(
                                "[yellow]All unified analyses failed - falling back to "
                                "transcript/screenshot findings.[/]"
                            )
                        else:
                            console.print(
                                f"[yellow]{failed_count}/{requested_unified_count} unified analyses "
                                "failed - report will mix AI and fallback findings.[/]"
                            )
                        pipeline_errors.append(
                            {
                                "stage": "unified_analysis",
                                "message": (
                                    f"{failed_count} of {requested_unified_count} unified analyses "
                                    "failed. The report fell back to transcript/screenshot-only "
                                    "findings for those items."
                                ),
                            }
                        )
                    # Deduplicate similar findings before saving
                    if unified_findings:
                        original_count = len(unified_findings)
                        # Layer 1: cheap heuristic dedup (exact / same-category).
                        unified_findings = deduplicate_findings(unified_findings)
                        # Layer 2: semantic LLM-merge of cross-category
                        # paraphrases the heuristic is blind to. No-op when
                        # disabled, when no LLM key, or on any LLM/parse error.
                        unified_findings = llm_merge_findings(unified_findings, config)
                        if len(unified_findings) < original_count:
                            console.print(
                                f"[green]Deduplicated:[/] {original_count} → "
                                f"{len(unified_findings)} findings"
                            )
                            # Filter detections/screenshots to match deduplicated findings
                            # Include all merged_from_ids to keep screenshots from merged findings
                            keep_keys: set[tuple[int, float]] = set()
                            for f in unified_findings:
                                # Add the finding's own ID
                                keep_keys.add((f.detection_id, f.timestamp))
                                # Add all IDs from merged findings
                                for orig_id, orig_ts in f.merged_from_ids:
                                    keep_keys.add((orig_id, orig_ts))
                            # C6.2: only prune screenshots on a FULL success.
                            # On a partial run the failed items are not in
                            # keep_keys; pruning would drop them from the
                            # checkpoint and --resume could never retry them.
                            if (
                                not unified_partial
                                and keep_keys
                                and len(keep_keys) < len(screenshots)
                            ):
                                screenshots = [
                                    (d, p)
                                    for (d, p) in screenshots
                                    if (d.segment.id, d.segment.start) in keep_keys
                                ]
                                detections = [d for (d, _) in screenshots]
                                # BH42: persist the pruned (post-dedup) screenshots
                                # and detections back to the checkpoint. They were
                                # saved pre-dedup in Step 3/4, so without this a
                                # later --resume would restore the full pre-dedup
                                # set and the report would regrow the duplicates the
                                # unified stage just merged away.
                                checkpoint.screenshots = [
                                    serialize_screenshot(d, p) for d, p in screenshots
                                ]
                                checkpoint.detections = [serialize_detection(d) for d in detections]
                    checkpoint.unified_findings = [
                        serialize_unified_finding(f) for f in unified_findings
                    ]
                    if unified_findings:
                        try:
                            executive_summary = generate_unified_summary(unified_findings, config)
                            checkpoint.executive_summary = executive_summary
                            visual_summary = generate_visual_summary_unified(
                                unified_findings,
                                language=transcription.language if transcription else language,
                            )
                            checkpoint.visual_summary = visual_summary
                        except Exception as e:
                            console.print(f"[yellow]Summary generation failed: {e}[/]")
                            pipeline_errors.append(
                                {
                                    "stage": "summary_generation",
                                    "message": str(e),
                                }
                            )
                    elif detections:
                        try:
                            executive_summary = generate_detection_executive_summary(
                                detections, config
                            )
                            checkpoint.executive_summary = executive_summary
                            if executive_summary:
                                console.print(
                                    "[yellow]Generated transcript-only executive summary "
                                    "because screenshot-backed AI analysis was unavailable.[/]"
                                )
                        except Exception as e:
                            console.print(
                                f"[yellow]Transcript-only summary generation failed: {e}[/]"
                            )
                            pipeline_errors.append(
                                {
                                    "stage": "summary_generation",
                                    "message": f"Transcript-only summary fallback failed: {e}",
                                }
                            )
                except Exception as e:
                    unified_failed = True
                    console.print(f"[yellow]Unified analysis failed: {e}[/]")
                    console.print(
                        "[dim]Continuing without AI analysis; checkpoint kept so "
                        "--resume can retry visual analysis.[/]"
                    )
                    pipeline_errors.append(
                        {
                            "stage": "unified_analysis",
                            "message": str(e),
                        }
                    )
                if not unified_failed and not unified_partial:
                    # Only a stage that FULLY completed may be marked done. A
                    # partial run (C6.2) stays unmarked so --resume retries the
                    # residual failed items instead of trusting a stale "done".
                    checkpoint.mark_stage_complete("unified_analysis")
                    # Also mark legacy stages complete for checkpoint compatibility
                    checkpoint.mark_stage_complete("semantic")
                    checkpoint.mark_stage_complete("vision")
                save_checkpoint(checkpoint, video_output)
                console.print()
            else:
                console.print("[dim]Step 5: Unified VLM Analysis - skipped (cached)[/]")
                # Restore from checkpoint if available
                if checkpoint.unified_findings:
                    unified_findings = [
                        deserialize_unified_finding(f) for f in checkpoint.unified_findings
                    ]
        else:
            if vision and not config.get_vision_api_key():
                # The user asked for visual analysis (vision=True) but no vision
                # API key is configured, so Step 5 cannot run. Silently degrading
                # to a transcript-only report hides that the requested VLM pass
                # never happened. Warn loudly and record it in the report errors
                # so the user knows the report is transcript/detection-only by
                # accident, not by choice. (A deliberate --no-vision opt-out
                # leaves vision=False and stays quiet.)
                console.print()
                console.print(
                    Panel(
                        "Visual (VLM) analysis was requested but no vision API key is "
                        "configured, so screenshots were not analyzed. The report below "
                        "is transcript/detection-only.\n\n"
                        "[dim]Fix:[/] set a vision API key (SCREENSCRIBE_VISION_API_KEY "
                        "or SCREENSCRIBE_API_KEY), or pass --no-vision to silence this.",
                        title="[bold yellow]Vision Skipped[/]",
                        border_style="yellow",
                    )
                )
                pipeline_errors.append(
                    {
                        "stage": "unified_analysis",
                        "message": (
                            "Visual analysis was requested but no vision API key was "
                            "configured; screenshots were not analyzed and the report is "
                            "transcript/detection-only."
                        ),
                    }
                )
                # Truth + resumability: the VLM/unified stage did NOT run (no
                # vision key), so do NOT mark it (or vision) complete, and do NOT
                # delete the checkpoint on exit (see Step 6 cleanup). Marking it
                # would let a later `--resume` (after the key is added) trust a
                # stale "completed" and skip vision; deleting the checkpoint would
                # force a full re-transcribe. Keeping it truthful (detection /
                # screenshots done, unified_analysis NOT done) makes --resume
                # actually finish visual analysis. The transcript/detection-only
                # report is still produced now as an honest partial deliverable.
                vision_skipped_no_key = True
            else:
                # Genuine opt-out (vision=False) or nothing to analyze: the vision
                # stage is legitimately finished for this run.
                checkpoint.mark_stage_complete("unified_analysis")
                checkpoint.mark_stage_complete("semantic")
                checkpoint.mark_stage_complete("vision")

        # Step 6: Generate reports
        console.rule("[bold]Step 6: Report Generation[/]")

        cli._write_report_artifacts(
            detections=detections,
            screenshots=screenshots,
            video=video,
            video_output=video_output,
            video_stem=video_stem,
            unified_findings=unified_findings,
            executive_summary=executive_summary,
            visual_summary=visual_summary,
            errors=pipeline_errors,
            transcript=transcription.text if transcription else "",
            transcript_segments=transcription.segments if transcription else None,
            embed_video=embed_video,
            language=transcription.language if transcription else language,
            json_report=json_report,
            markdown_report=markdown_report,
            html_report=html_report,
        )

        # Show errors summary if any
        if pipeline_errors:
            console.print(
                f"[yellow]⚠️ {len(pipeline_errors)} error(s) occurred during processing.[/]"
            )
            console.print("[dim]Check report for details. Results are partial.[/]")

        console.print()

        # Print executive summary if available
        if executive_summary:
            console.print(
                Panel(executive_summary, title="[bold]Executive Summary[/]", border_style="green")
            )
            console.print()

        # Print summary to console
        cli.print_report(detections, screenshots, video)

        # Clean up checkpoint on success -- but keep it when vision was skipped
        # for a missing key, or when the unified VLM stage hard-failed: either
        # way the run is incomplete (visual analysis never finished), so a
        # truthful, resumable checkpoint must survive for `--resume` to finish
        # visual analysis. Deleting it here would let a re-run re-transcribe from
        # scratch or, worse, lose the only signal that Step 5 must re-run.
        if vision_skipped_no_key:
            console.print(
                "[dim]Checkpoint kept: vision was skipped (no vision API key). Add a "
                "vision key and re-run with [bold]--resume[/] to complete visual analysis.[/]"
            )
        elif unified_failed:
            console.print(
                "[dim]Checkpoint kept: unified VLM analysis failed. Re-run with "
                "[bold]--resume[/] to retry visual analysis without re-transcribing.[/]"
            )
        elif unified_partial:
            # C6.2: some VLM items failed. Keep the checkpoint (with the surviving
            # successes persisted and unified_analysis still unmarked) so --resume
            # retries ONLY the failed items instead of re-paying for the whole
            # stage. Deleting it here would erase the partial progress.
            console.print(
                "[dim]Checkpoint kept: some VLM items failed. Re-run with "
                "[bold]--resume[/] to retry only the failed items without "
                "re-analyzing the ones that succeeded.[/]"
            )
        else:
            delete_checkpoint(video_output)

        # Final success output
        console.rule("[bold green]Finished successfully![/]")
        console.print()
        cli._print_report_artifact_paths(
            video_output=video_output,
            video_stem=video_stem,
            json_report=json_report,
            markdown_report=markdown_report,
            html_report=html_report,
        )
        console.print()
        console.rule(f"[dim]Screenscribe v{__version__} by Vetcoders[/]")

        # Update context for next video in batch
        if unified_findings:
            last_finding = unified_findings[-1]
            if hasattr(last_finding, "response_id") and last_finding.response_id:
                batch_context_response_id = last_finding.response_id

        # Store last video info for serve
        last_video = video
        last_output = video_output

    # After all videos processed, optionally serve the last report
    if serve and last_output is not None and last_video is not None:
        cli._serve_report(last_output, last_video, port)
