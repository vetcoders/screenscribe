"""Markdown report artifacts (legacy basic + enhanced)."""

from datetime import datetime
from pathlib import Path
from typing import Any

from ..detect import Detection, format_timestamp
from ..transcribe import Segment
from .data import (
    DEGRADED_MARKER_LABEL,
    UnifiedFindingResolver,
    _format_timestamped_transcript,
    _is_degraded_analysis,
    console,
    fold_screenshots,
)


def save_markdown_report(
    detections: list[Detection],
    screenshots: list[tuple[Detection, Path]],
    video_path: Path,
    output_path: Path,
) -> Path:
    """Save report as Markdown for documentation."""
    lines = [
        "# Video Review Report",
        "",
        f"**Video:** `{video_path.name}`",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|----------|-------|",
        f"| Bugs | {sum(1 for d in detections if d.category == 'bug')} |",
        f"| Change Requests | {sum(1 for d in detections if d.category == 'change')} |",
        f"| UI Issues | {sum(1 for d in detections if d.category == 'ui')} |",
        f"| **Total** | **{len(detections)}** |",
        "",
        "## Findings",
        "",
    ]

    for i, (detection, screenshot_path) in enumerate(screenshots, 1):
        lines.extend(
            [
                f"### #{i} [{detection.category.upper()}] @ {format_timestamp(detection.segment.start)}",
                "",
                f"> {detection.segment.text}",
                "",
                f"**Keywords:** {', '.join(detection.keywords_found)}",
                "",
                f"**Context:** {detection.context[:300]}...",
                "",
                f"![Screenshot]({screenshot_path.name})",
                "",
                "---",
                "",
            ]
        )

    lines.extend(
        [
            "",
            "---",
            "*Built by Vetcoders*",
        ]
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    console.print(f"[green]Markdown report saved:[/] {output_path}")
    return output_path


def save_enhanced_markdown_report(
    detections: list[Detection],
    screenshots: list[tuple[Detection, Path]],
    video_path: Path,
    output_path: Path,
    unified_findings: list[Any] | None = None,
    executive_summary: str = "",
    visual_summary: str = "",
    errors: list[dict[str, str]] | None = None,
    transcript: str = "",
    transcript_segments: list[Segment] | None = None,
) -> Path:
    """Save enhanced report with unified VLM analysis as Markdown.

    Format optimized for AI consumption:
    - Transcript at the top for full context
    - Sorted by severity (critical first)
    - Consolidated action items at top
    - No emoji clutter
    - Non-issues separated at end

    Args:
        detections: List of detections
        screenshots: List of (detection, screenshot_path) tuples
        video_path: Path to source video
        output_path: Path to save Markdown report
        unified_findings: List of UnifiedFinding from unified VLM analysis
        executive_summary: Executive summary text
        visual_summary: Visual summary text
        errors: List of pipeline errors
        transcript: Full transcript text (embedded at start for AI context)

    Returns:
        Path to saved report
    """
    # Single source of truth shared with the JSON/HTML reports: merged-aware
    # (composite key) plus per-screenshot resolution. A narrow {detection_id: f}
    # dict would resurrect deduplicated screenshots as bare fabricated issues
    # (BH6) and collapse all id=0 POIs to one last-wins finding (BH22/BH51).
    resolver = UnifiedFindingResolver(unified_findings)

    def _lookup(detection: Detection, screenshot_path: Path | None = None) -> Any:
        return resolver.resolve(detection, screenshot_path)

    # G6b: fold per-frame screenshots into per-survivor rows so the auto LLM-merge
    # reduction reaches the Markdown report. Merged-away member frames become
    # sub-evidence under their survivor instead of separate rows.
    rows = fold_screenshots(screenshots, unified_findings)
    folded_screenshots: list[tuple[Detection, Path]] = [
        (row.detection, row.screenshot_path) for row in rows
    ]
    members_by_id: dict[int, list[tuple[Detection, Path]]] = {
        id(row.detection): list(row.members) for row in rows
    }

    # Separate issues from non-issues and sort by severity
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    issues: list[tuple[Detection, Path]] = []
    non_issues: list[tuple[Detection, Path]] = []
    pending_user_marked: list[tuple[Detection, Path]] = []

    for detection, screenshot_path in folded_screenshots:
        uf = _lookup(detection, screenshot_path)
        if uf and not uf.is_issue:
            non_issues.append((detection, screenshot_path))
        elif uf:
            issues.append((detection, screenshot_path))
        elif detection.category == "user_marked":
            pending_user_marked.append((detection, screenshot_path))
        else:
            issues.append((detection, screenshot_path))

    # Sort issues by severity
    def get_severity_rank(item: tuple[Detection, Path]) -> int:
        detection, screenshot_path = item
        uf = _lookup(detection, screenshot_path)
        if uf:
            return severity_order.get(uf.severity, 4)
        return 4

    issues.sort(key=get_severity_rank)

    # Collect all action items upfront
    all_action_items: list[tuple[str, str, list[str]]] = []  # (severity, summary, items)
    for detection, screenshot_path in issues:
        uf = _lookup(detection, screenshot_path)
        if uf and uf.action_items:
            all_action_items.append((uf.severity, uf.summary, uf.action_items))

    # Build components index: component -> [(finding_num, severity)]
    components_index: dict[str, list[tuple[int, str]]] = {}
    for i, (detection, screenshot_path) in enumerate(issues, 1):
        uf = _lookup(detection, screenshot_path)
        if uf and uf.affected_components:
            severity = uf.severity if uf.is_issue else "ok"
            for component in uf.affected_components:
                if component not in components_index:
                    components_index[component] = []
                components_index[component].append((i, severity))

    # Build report
    lines = [
        "# Video Review Report",
        "",
        f"Video: `{video_path.name}`",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    # Quick stats - one line (folded: count survivor rows, not merged-away frames)
    bug_count = sum(1 for d, _ in folded_screenshots if d.category == "bug")
    change_count = sum(1 for d, _ in folded_screenshots if d.category == "change")
    ui_count = sum(1 for d, _ in folded_screenshots if d.category == "ui")

    if unified_findings or pending_user_marked:
        issues_only = [f for f in (unified_findings or []) if f.is_issue]
        crit = sum(1 for f in issues_only if f.severity == "critical")
        high = sum(1 for f in issues_only if f.severity == "high")
        med = sum(1 for f in issues_only if f.severity == "medium")
        low = sum(1 for f in issues_only if f.severity == "low")
        # An explicit 'none' priority is a finding the reviewer kept but did not
        # rank. It stays out of the crit/high/med/low tally, but is surfaced
        # explicitly so the breakdown reconciles with the issue total instead of
        # silently swallowing no-priority findings.
        no_priority = sum(1 for f in issues_only if f.severity == "none")
        severity_breakdown = f"{crit} critical, {high} high, {med} medium, {low} low"
        if no_priority:
            severity_breakdown += f", {no_priority} no-priority"
        lines.append(
            f"**Stats:** {len(issues)} issues ({severity_breakdown}) "
            f"| {bug_count} bugs, {change_count} changes, {ui_count} UI "
            f"| {len(non_issues)} non-issues filtered"
            f" | {len(pending_user_marked)} pending user-marked"
        )
    else:
        lines.append(
            f"**Stats:** {len(folded_screenshots)} findings | "
            f"{bug_count} bugs, {change_count} changes, {ui_count} UI"
        )
    lines.append("")

    # Transcript (at the top for AI context)
    if transcript:
        lines.extend(["## Transcript", "", transcript, ""])

    timestamped_transcript = _format_timestamped_transcript(transcript_segments)
    if timestamped_transcript:
        lines.extend(["## Timestamped Transcript", "", timestamped_transcript, ""])

    # Executive Summary
    if executive_summary:
        lines.extend(["## Summary", "", executive_summary, ""])

    # Consolidated Action Items (critical and high only for quick scan)
    if all_action_items:
        critical_high = [
            (s, summ, items) for s, summ, items in all_action_items if s in ("critical", "high")
        ]
        if critical_high:
            lines.extend(["## Action Items (Critical/High)", ""])
            for severity, summary, items in critical_high:
                lines.append(f"**[{severity.upper()}]** {summary}")
                for item in items:
                    lines.append(f"- [ ] {item}")
                lines.append("")

    # Components Index - shows which components have issues
    if components_index:
        # Sort by number of issues (most affected first), then by max severity
        severity_weight = {"critical": 4, "high": 3, "medium": 2, "low": 1, "ok": 0, "none": 0}

        def component_score(item: tuple[str, list[tuple[int, str]]]) -> tuple[int, int]:
            _, findings = item
            max_sev = max((severity_weight.get(sev, 0) for _, sev in findings), default=0)
            return (-len(findings), -max_sev)

        sorted_components = sorted(components_index.items(), key=component_score)

        lines.extend(["## Components Affected", ""])
        for component, findings in sorted_components:
            finding_nums = [f"#{num}" for num, _ in findings]
            severities = [sev for _, sev in findings]
            sev_counts = []
            for sev in ["critical", "high", "medium", "low"]:
                count = severities.count(sev)
                if count:
                    sev_counts.append(f"{count} {sev}")
            sev_summary = f" ({', '.join(sev_counts)})" if sev_counts else ""
            lines.append(f"- **{component}**: {', '.join(finding_nums)}{sev_summary}")
        lines.append("")

    # Errors section
    if errors:
        lines.extend(["## Errors", ""])
        for error in errors:
            lines.append(f"- {error.get('stage', 'unknown')}: {error.get('message', '')}")
        lines.append("")

    # Visual summary
    if visual_summary:
        lines.extend(["## Visual Summary", "", visual_summary, ""])

    if pending_user_marked:
        lines.extend(
            [
                "## User-Marked Moments (Pending Analysis)",
                "",
                "These moments were marked by the user but have not been analyzed by the VLM yet.",
                "",
            ]
        )
        for i, (detection, screenshot_path) in enumerate(pending_user_marked, 1):
            lines.append(f"### Pending #{i} @ {format_timestamp(detection.segment.start)}")
            lines.append("")
            lines.append(f"> {detection.segment.text}")
            lines.append("")
            if detection.context:
                lines.append(f"**Notes:** {detection.context}")
                lines.append("")
            lines.append(f"Screenshot: {screenshot_path.name}")
            lines.extend(["", "---", ""])

    # Issues (sorted by severity)
    if issues:
        lines.extend(["## Issues", ""])

        for i, (detection, screenshot_path) in enumerate(issues, 1):
            uf = _lookup(detection, screenshot_path)

            raw_severity = uf.severity if uf else "medium"
            category = detection.category.upper()

            # An explicit 'none' priority (reviewer cleared it) is a finding
            # without a severity tag -- render the header bare instead of leaking
            # a stray [NONE], mirroring the review card badge and ZIP manifest.
            if raw_severity == "none":
                lines.append(f"### #{i} {category} @ {format_timestamp(detection.segment.start)}")
            else:
                lines.append(
                    f"### [{raw_severity.upper()}] #{i} {category} "
                    f"@ {format_timestamp(detection.segment.start)}"
                )
            lines.append("")
            # D7: low-confidence model output must announce itself as unverified
            # so a degraded finding never reads as a fully-vetted, certain issue.
            if _is_degraded_analysis(uf):
                lines.append(f"> **[{DEGRADED_MARKER_LABEL}]** This finding was not verified.")
                lines.append("")
            lines.append(f"> {detection.segment.text}")
            lines.append("")

            if uf:
                lines.append(f"**Summary:** {uf.summary}")
                if uf.affected_components:
                    lines.append(f"**Components:** {', '.join(uf.affected_components)}")
                if uf.suggested_fix:
                    lines.append(f"**Fix:** {uf.suggested_fix}")
                lines.append("")

                # Visual issues from unified analysis
                if uf.issues_detected:
                    lines.append("**Visual issues:** " + "; ".join(uf.issues_detected))
                    lines.append("")
            else:
                lines.append(f"**Summary:** {detection.segment.text}")
                lines.append("")

            lines.append(f"Screenshot: {screenshot_path.name}")

            # G6b: merged-away duplicates fold in here as evidence frames, not as
            # separate findings, so the count matches the auto-merge reduction.
            members = members_by_id.get(id(detection), [])
            if members:
                lines.append("")
                lines.append(f"**Merged evidence frames ({len(members)}):**")
                for member_det, member_path in members:
                    lines.append(
                        f"- {format_timestamp(member_det.segment.start)} "
                        f"({member_path.name}): {member_det.segment.text}"
                    )

            lines.extend(["", "---", ""])

    # Non-issues (at the end, collapsed)
    if non_issues:
        lines.extend(
            [
                "## Non-Issues (Confirmed OK)",
                "",
                "These were flagged by detection but the user marked them as working correctly:",
                "",
            ]
        )
        for detection, screenshot_path in non_issues:
            uf = _lookup(detection, screenshot_path)
            summary = uf.summary if uf else detection.segment.text
            lines.append(f"- {format_timestamp(detection.segment.start)}: {summary}")
        lines.append("")

    lines.extend(
        [
            "---",
            "*Generated by screenscribe*",
        ]
    )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    console.print(f"[green]Enhanced Markdown report saved:[/] {output_path}")
    return output_path
