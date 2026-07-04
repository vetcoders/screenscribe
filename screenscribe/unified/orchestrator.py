"""Parallel orchestration for the unified analysis pipeline.

Runs up to MAX_WORKERS concurrent single-finding analyses with staggered
starts, a shared response_id lock for conversation chaining, a rich
Progress/Live display, and a failure-ratio abort gate.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.live import Live
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn

from ..config import ScreenScribeConfig
from ..detect import Detection
from ._console import console
from .finding import UnifiedFinding

if TYPE_CHECKING:
    from types import ModuleType

# Constants for parallel processing
MAX_WORKERS = 5
STAGGER_DELAY = 0.5  # seconds between task starts
MAX_UNIFIED_FAILURE_RATIO = 0.5


def _is_degraded_finding(finding: UnifiedFinding | None) -> bool:
    """Local predicate: did this finding actually come back as a real VLM result?

    A finding produced by the raw-text / parse-error fallback path carries
    ``confidence == "degraded"`` and/or ``parsed_from_unstructured_output``.
    These are truthy objects, so ``if finding:`` silently counts them as
    successes (BH27), keeping the failure_ratio at 0 even when the provider
    returned unparseable garbage. Treat them as failures for the ratio and the
    fallback decision instead.

    Kept intentionally local (not a shared helper) — analyze_server applies the
    same predicate locally (mirror of BH48) so the two surfaces stay decoupled
    while sharing the semantics.
    """
    if finding is None:
        return False
    return getattr(finding, "confidence", "high") == "degraded" or getattr(
        finding, "parsed_from_unstructured_output", False
    )


def _unified_analysis_module() -> ModuleType:
    """Resolve the unified_analysis facade lazily.

    The probe/per-task streaming calls go through the facade module so that
    ``monkeypatch.setattr("screenscribe.unified_analysis.analyze_finding_unified_streaming", ...)``
    is honored. The import is function-local to avoid an import cycle
    (the facade imports this module at load time).
    """
    from .. import unified_analysis

    return unified_analysis


@dataclass
class _TaskState:
    """State for a parallel analysis task."""

    idx: int
    detection: Detection
    screenshot_path: Path
    status: str = "pending"  # pending, running, done, failed
    reasoning_preview: str = ""
    severity: str = ""
    task_id: TaskID | None = None


def analyze_all_findings_unified(
    screenshots: list[tuple[Detection, Path]],
    config: ScreenScribeConfig,
    previous_response_id: str = "",
) -> list[UnifiedFinding]:
    """
    Analyze all findings using unified VLM pipeline with parallel streaming.

    Uses ThreadPoolExecutor to run up to MAX_WORKERS concurrent VLM requests,
    with staggered starts (STAGGER_DELAY between each) to avoid rate limits.
    Response IDs are chained between streams using a shared lock.

    Args:
        screenshots: List of (detection, screenshot_path) tuples
        config: screenscribe configuration
        previous_response_id: Optional response ID from previous batch for context chaining

    Returns:
        List of UnifiedFinding results (ordered by original index)
    """
    if not config.get_vision_api_key():
        console.print("[yellow]No Vision API key - skipping unified analysis[/]")
        return []

    if not screenshots:
        return []

    console.print(
        f"[blue]Running parallel VLM analysis on {len(screenshots)} findings "
        f"(max {MAX_WORKERS} concurrent, {STAGGER_DELAY}s stagger)...[/]"
    )

    # Track task states for display
    task_states: dict[int, _TaskState] = {}
    for idx, (detection, screenshot_path) in enumerate(screenshots):
        task_states[idx] = _TaskState(idx=idx, detection=detection, screenshot_path=screenshot_path)

    # The batch seed comes from the semantic prefilter, which is ALWAYS an LLM
    # call. In a split-provider setup (vision_endpoint != llm_endpoint) that id is
    # LLM-minted and cross-provider: replaying it on a vision call is the very
    # handoff F's gate exists to avoid, but F leaves the FIRST vision probe still
    # carrying it (the probe is a vision call, so the per-call gate would replay
    # it). Drop the seed before the first vision probe in split-provider; the
    # probe then mints a vision id that seeds vision->vision chaining for the rest
    # of the batch (M1). Same-provider keeps the historical seed (it is valid).
    split_provider = config.vision_endpoint != config.llm_endpoint
    seed_response_id = None if split_provider else (previous_response_id or None)

    # Fast-fail probe: if the provider rejects screenshot-backed image input,
    # skip the failed item, but keep the rest of the batch alive.
    first_detection, first_screenshot = screenshots[0]
    probe_result = _unified_analysis_module().analyze_finding_unified_streaming(
        first_detection,
        first_screenshot,
        config,
        previous_response_id=seed_response_id,
    )
    results_by_idx: dict[int, UnifiedFinding | None] = {0: probe_result}
    # A degraded/parse-error probe is not a real success (BH27): count it as a
    # failure so the single-item path and the ratio gate treat it correctly.
    probe_failed = probe_result is None or _is_degraded_finding(probe_result)
    if probe_failed:
        task_states[0].status = "failed"
        console.print(
            "[yellow]Unified VLM preflight failed on the first screenshot - "
            "skipping that item and continuing with the remaining batch.[/]"
        )
    else:
        task_states[0].status = "done"
        task_states[0].severity = probe_result.severity if probe_result.is_issue else "ok"

    if len(screenshots) == 1:
        if probe_failed:
            console.print(
                "[yellow]Unified analysis aborted: 1/1 screenshots failed "
                f"(>{MAX_UNIFIED_FAILURE_RATIO:.0%} threshold).[/]"
            )
            return []
        console.print(
            f"[dim]  [1/1][/] {first_detection.category} @ {first_detection.segment.start:.1f}s "
            f"[green]{probe_result.severity if probe_result.is_issue else 'ok'}[/]"
        )
        console.print(
            f"[green]Unified analysis complete:[/] "
            f"[red]{1 if probe_result.is_issue and probe_result.severity == 'critical' else 0} critical[/], "
            f"[yellow]{1 if probe_result.is_issue and probe_result.severity == 'high' else 0} high[/], "
            f"[blue]{1 if probe_result.is_issue and probe_result.severity == 'medium' else 0} medium[/], "
            f"[dim]{1 if probe_result.is_issue and probe_result.severity == 'low' else 0} low[/]"
        )
        return [probe_result]

    # Shared state for response_id chaining. The fallback never reuses the raw
    # cross-provider seed: in split-provider seed_response_id is None, so a probe
    # that minted no id leaves chaining empty rather than leaking the LLM seed
    # into later vision calls (M1).
    response_id_lock = threading.Lock()
    shared_response_id = (
        probe_result.response_id
        if probe_result and probe_result.response_id
        else (seed_response_id or "")
    )

    def analyze_one(idx: int, scheduled_start: float) -> tuple[int, UnifiedFinding | None]:
        """Analyze a single finding with staggered start and response_id chaining."""
        nonlocal shared_response_id

        # BH43: stagger by scheduled wall-clock start, not by an absolute
        # index-based sleep inside the worker slot. The previous code slept
        # (idx-1)*STAGGER_DELAY regardless of when the task actually started, so
        # tasks beyond MAX_WORKERS held a pool slot while sleeping their full
        # cumulative delay -- serializing the pool. Here a task only sleeps for
        # the time remaining until its scheduled slot; if it already waited in the
        # queue for a free worker, that wait counts and the residual sleep is
        # near zero, keeping the pool genuinely concurrent.
        remaining = scheduled_start - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

        state = task_states[idx]
        state.status = "running"

        # Get latest response_id for chaining
        with response_id_lock:
            prev_id = shared_response_id

        # Create callbacks for live display
        reasoning_buffer = ""

        def on_reasoning(delta: str) -> None:
            nonlocal reasoning_buffer
            reasoning_buffer = (reasoning_buffer + delta)[-50:]
            state.reasoning_preview = reasoning_buffer

        # Run streaming analysis
        finding = _unified_analysis_module().analyze_finding_unified_streaming(
            state.detection,
            state.screenshot_path,
            config,
            previous_response_id=prev_id,
            on_reasoning=on_reasoning,
        )

        # Update shared response_id for next task
        if finding and finding.response_id:
            with response_id_lock:
                shared_response_id = finding.response_id

        # Update state
        if finding:
            state.status = "done"
            state.severity = finding.severity if finding.is_issue else "ok"
        else:
            state.status = "failed"
        return (idx, finding)

    # Build progress display
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=20),
        TextColumn("{task.completed}/{task.total}"),
        TextColumn("[dim]{task.fields[stream]}[/]"),
        console=console,
        transient=True,
    )

    with Live(progress, console=console, refresh_per_second=10):
        # Add main progress task
        main_task = progress.add_task(
            f"Analyzing {len(screenshots)} findings",
            total=len(screenshots),
            completed=1,
            stream=(
                f"[green]ok[/] #1 [{probe_result.severity if probe_result.is_issue else 'ok'}]"
                if probe_result
                else "[yellow]x[/] #1 failed"
            ),
        )

        # Submit all tasks with staggered, submit-time-relative starts. Each task
        # gets an absolute scheduled start (t0 + n*STAGGER_DELAY); a worker only
        # sleeps for whatever time is left until its slot when it actually runs,
        # so queue-wait already spent does not stack on top of the sleep (BH43).
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures: dict[Future[tuple[int, UnifiedFinding | None]], int] = {}

            submit_t0 = time.monotonic()
            for stagger_n, idx in enumerate(range(1, len(screenshots))):
                scheduled_start = submit_t0 + stagger_n * STAGGER_DELAY
                future = executor.submit(analyze_one, idx, scheduled_start)
                futures[future] = idx

            # Process completions as they arrive
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result_idx, finding = future.result()
                    results_by_idx[result_idx] = finding

                    # Update progress display
                    state = task_states[result_idx]
                    if finding:
                        status_str = f"[green]ok[/] #{result_idx + 1} [{state.severity}]"
                    else:
                        status_str = f"[yellow]x[/] #{result_idx + 1} failed"

                    # Show active tasks reasoning
                    active_reasoning = ""
                    for s in task_states.values():
                        if s.status == "running" and s.reasoning_preview:
                            active_reasoning = s.reasoning_preview[:30]
                            break

                    progress.update(
                        main_task,
                        advance=1,
                        stream=active_reasoning or status_str,
                    )

                except Exception as e:
                    results_by_idx[idx] = None
                    progress.update(main_task, advance=1, stream=f"[red]error: {e}[/]")

    # Collect GENUINE results in original order. A degraded/parse-error finding
    # is truthy but is not a real VLM result (BH27), so it is excluded from the
    # returned set and counted as a failure for the ratio below. Dropping it lets
    # the caller's transcript/screenshot fallback cover that item instead of
    # surfacing unparseable output as a confident AI finding.
    results: list[UnifiedFinding] = []
    for idx in range(len(screenshots)):
        finding = results_by_idx.get(idx)
        if finding and not _is_degraded_finding(finding):
            results.append(finding)

    failed_count = len(screenshots) - len(results)
    failure_ratio = failed_count / len(screenshots)
    if failure_ratio > MAX_UNIFIED_FAILURE_RATIO:
        # BH3: even when most items failed, do NOT discard the genuine successes
        # that did come back. Returning [] here erased real findings and forced a
        # 100%-fallback report. Keep the successes (the caller already mixes AI +
        # fallback and records a partial-failure error) instead of pretending the
        # whole batch failed. Only a truly empty success set falls back fully.
        if results:
            console.print(
                "[yellow]Unified analysis mostly failed: "
                f"{failed_count}/{len(screenshots)} screenshots failed "
                f"(>{MAX_UNIFIED_FAILURE_RATIO:.0%} threshold). Keeping the "
                f"{len(results)} successful finding(s); the rest fall back to "
                "transcript/screenshot findings.[/]"
            )
        else:
            console.print(
                "[yellow]Unified analysis aborted: "
                f"{failed_count}/{len(screenshots)} screenshots failed "
                f"(>{MAX_UNIFIED_FAILURE_RATIO:.0%} threshold). Falling back to "
                "transcript/screenshot findings for this batch.[/]"
            )
            return []

    # Print completion status for each task
    for idx in range(len(screenshots)):
        state = task_states[idx]
        finding = results_by_idx.get(idx)
        timestamp = state.detection.segment.start

        # A degraded/parse-error finding is reported as failed here too, so the
        # per-item log matches what is actually returned (no silent success).
        if finding and not _is_degraded_finding(finding):
            chained = " [chained]" if finding.response_id else ""
            severity_color = {
                "critical": "red",
                "high": "yellow",
                "medium": "blue",
                "low": "dim",
                "ok": "green",
            }.get(state.severity, "white")
            console.print(
                f"[dim]  [{idx + 1}/{len(screenshots)}][/] "
                f"{state.detection.category} @ {timestamp:.1f}s "
                f"[{severity_color}]{state.severity}[/]{chained}"
            )
        else:
            label = "degraded" if finding else "failed"
            console.print(
                f"[dim]  [{idx + 1}/{len(screenshots)}][/] "
                f"{state.detection.category} @ {timestamp:.1f}s "
                f"[yellow]{label}[/]"
            )

    # Summary
    issues = [f for f in results if f.is_issue]
    non_issues = [f for f in results if not f.is_issue]
    critical = sum(1 for f in issues if f.severity == "critical")
    high = sum(1 for f in issues if f.severity == "high")
    medium = sum(1 for f in issues if f.severity == "medium")
    low = sum(1 for f in issues if f.severity == "low")

    console.print(
        f"[green]Unified analysis complete:[/] "
        f"[red]{critical} critical[/], "
        f"[yellow]{high} high[/], "
        f"[blue]{medium} medium[/], "
        f"[dim]{low} low[/]"
    )
    if non_issues:
        console.print(f"[dim]  ({len(non_issues)} positive/neutral observations filtered)[/]")

    return results
