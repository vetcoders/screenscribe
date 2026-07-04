"""Processing-time estimate table for the screenscribe ``review`` command.

Extracted from ``cli.py``. ``cli.py`` re-imports ``_show_estimate`` (and the
``ESTIMATE_*`` tuning constants) back into its namespace so the historical
patch surface (``monkeypatch.setattr(cli, "_show_estimate", ...)``) and the
``review`` god-function keep working unchanged.
"""

# Time estimates (seconds per unit)
ESTIMATE_STT_PER_MINUTE = 2.0  # ~2s per minute of video
ESTIMATE_SEMANTIC_PER_DETECTION = 12.0  # ~12s per detection (legacy)
ESTIMATE_VISION_PER_DETECTION = 25.0  # ~25s per screenshot (legacy)
ESTIMATE_UNIFIED_PER_DETECTION = 20.0  # ~20s per finding (unified VLM)
ESTIMATE_SEMANTIC_PREFILTER_PER_MINUTE = 8.0  # ~8s per minute for semantic pre-filter


def _show_estimate(
    duration: float,
    vision: bool,
    detection_count: int | None = None,
    use_unified: bool = True,
) -> None:
    """Show estimated processing times.

    Args:
        duration: Video duration in seconds
        vision: Whether the unified VLM (visual/screenshot) analysis is enabled
        detection_count: Known detection count (None for estimate)
        use_unified: Whether using unified VLM analysis (default True)
    """
    from rich.table import Table

    # Route printing through the cli module so the single shared console
    # instance (and any monkeypatch on it) is honored.
    import screenscribe.cli as cli

    console = cli.console

    table = Table(title="Estimated Processing Time")
    table.add_column("Step", style="cyan")
    table.add_column("Estimate", justify="right")
    table.add_column("Notes", style="dim")

    # Audio extraction (~5s fixed)
    table.add_row("Audio extraction", "~5s", "FFmpeg")

    # STT transcription
    video_minutes = duration / 60
    stt_time = max(30, video_minutes * ESTIMATE_STT_PER_MINUTE)
    table.add_row("Transcription", f"~{int(stt_time)}s", f"{video_minutes:.1f} min video")

    # Detection -- always the LLM semantic pre-filter (single live path).
    prefilter_time = video_minutes * ESTIMATE_SEMANTIC_PREFILTER_PER_MINUTE
    table.add_row("Semantic pre-filter", f"~{int(prefilter_time)}s", "LLM analyzes full transcript")
    table.add_row("Issue detection", "<1s", "From semantic analysis")

    # Screenshot extraction
    table.add_row("Screenshots", "~10s", "FFmpeg frame extraction")

    # Estimate detections if not provided
    if detection_count is None:
        est_detections = int(video_minutes * 6)  # semantic prefilter yields more findings
    else:
        est_detections = detection_count

    # Analysis step
    if not vision:
        table.add_row("Visual (VLM) analysis", "skipped", "--no-vision")
        analysis_per_detection = 0.0
    elif use_unified:
        analysis_per_detection = ESTIMATE_UNIFIED_PER_DETECTION
        unified_time = est_detections * analysis_per_detection
        table.add_row(
            "Unified VLM analysis",
            f"~{int(unified_time / 60)}min",
            f"{est_detections} findings x ~{int(ESTIMATE_UNIFIED_PER_DETECTION)}s",
        )
    else:
        # Legacy separate semantic + vision passes
        analysis_per_detection = ESTIMATE_SEMANTIC_PER_DETECTION + ESTIMATE_VISION_PER_DETECTION
        sem_time = est_detections * ESTIMATE_SEMANTIC_PER_DETECTION
        table.add_row(
            "Semantic analysis",
            f"~{int(sem_time / 60)}min",
            f"{est_detections} detections x ~{int(ESTIMATE_SEMANTIC_PER_DETECTION)}s",
        )
        vis_time = est_detections * ESTIMATE_VISION_PER_DETECTION
        table.add_row(
            "Vision analysis",
            f"~{int(vis_time / 60)}min",
            f"{est_detections} screenshots x ~{int(ESTIMATE_VISION_PER_DETECTION)}s",
        )

    console.print(table)

    # Total estimate
    total_fixed = 5 + stt_time + 1 + 10 + prefilter_time
    total_analysis = est_detections * analysis_per_detection

    total = total_fixed + total_analysis
    console.print(f"\n[bold]Total estimated time:[/] ~{int(total / 60)} minutes")

    if not vision:
        console.print("[dim]Tip: Without visual (VLM) analysis, processing is very fast![/]")
    elif use_unified:
        console.print("[dim]Using unified VLM pipeline (screenshot + context in single call)[/]")
