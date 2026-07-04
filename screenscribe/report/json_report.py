"""JSON report artifacts (legacy basic + enhanced)."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..detect import Detection, format_timestamp
from ..transcribe import Segment
from .data import (
    UnifiedFindingResolver,
    _build_analysis_passes,
    _format_timestamped_transcript,
    _serialize_transcript_segments,
    _serialize_unified_analysis,
    console,
    fold_screenshots,
)


def save_json_report(
    detections: list[Detection],
    screenshots: list[tuple[Detection, Path]],
    video_path: Path,
    output_path: Path,
) -> Path:
    """Save report as JSON for further processing."""
    report: dict[str, Any] = {
        # Basename only — shareable JSON must not leak the absolute input path.
        "video": video_path.name,
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total": len(detections),
            "bugs": sum(1 for d in detections if d.category == "bug"),
            "changes": sum(1 for d in detections if d.category == "change"),
            "ui": sum(1 for d in detections if d.category == "ui"),
        },
        "findings": [],
    }

    for detection, screenshot_path in screenshots:
        report["findings"].append(
            {
                "id": detection.segment.id,
                "category": detection.category,
                "timestamp_start": detection.segment.start,
                "timestamp_end": detection.segment.end,
                "timestamp_formatted": format_timestamp(detection.segment.start),
                "text": detection.segment.text,
                "context": detection.context,
                "keywords": detection.keywords_found,
                "screenshot": screenshot_path.name,
            }
        )

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    console.print(f"[green]Report saved:[/] {output_path}")
    return output_path


def save_enhanced_json_report(
    detections: list[Detection],
    screenshots: list[tuple[Detection, Path]],
    video_path: Path,
    output_path: Path,
    unified_findings: list[Any] | None = None,
    executive_summary: str = "",
    errors: list[dict[str, str]] | None = None,
    transcript: str = "",
    transcript_segments: list[Segment] | None = None,
) -> Path:
    """Save enhanced report with unified VLM analysis as JSON.

    Args:
        detections: List of detections
        screenshots: List of (detection, screenshot_path) tuples
        video_path: Path to source video
        output_path: Path to save JSON report
        unified_findings: List of UnifiedFinding from unified VLM analysis
        executive_summary: Executive summary text
        errors: List of pipeline errors

    Returns:
        Path to saved report
    """
    resolver = UnifiedFindingResolver(unified_findings)
    # G6b: fold per-frame screenshots into per-survivor rows so the auto LLM-merge
    # reduction (e.g. "10 -> 7 findings") reaches the saved report. Merged-away
    # member frames are kept as evidence under their survivor, not as extra rows.
    rows = fold_screenshots(screenshots, unified_findings)

    report: dict[str, Any] = {
        # Basename only — shareable JSON must not leak the absolute input path.
        "video": video_path.name,
        "generated_at": datetime.now().isoformat(),
        "executive_summary": executive_summary,
        "transcript": transcript,
        "transcript_timestamped": _format_timestamped_transcript(transcript_segments),
        "transcript_segments": _serialize_transcript_segments(transcript_segments),
        "analysis_passes": _build_analysis_passes(detections, screenshots, unified_findings),
        # Counts derive from the folded rows so summary.total == len(findings).
        "summary": {
            "total": len(rows),
            "bugs": sum(1 for r in rows if r.detection.category == "bug"),
            "changes": sum(1 for r in rows if r.detection.category == "change"),
            "ui": sum(1 for r in rows if r.detection.category in ("ui", "accessibility")),
        },
        "severity_breakdown": {},
        "errors": errors or [],
        "findings": [],
    }

    # Build severity breakdown from unified findings
    if unified_findings:
        for severity in ["critical", "high", "medium", "low"]:
            count = sum(
                1
                for f in unified_findings
                if f.is_issue
                and getattr(f, "confidence", "high") != "degraded"
                and f.severity == severity
            )
            report["severity_breakdown"][severity] = count

    for row in rows:
        detection = row.detection
        screenshot_path = row.screenshot_path
        unified_finding = resolver.resolve(detection, screenshot_path)
        finding = {
            "id": detection.segment.id,
            "category": detection.category,
            "timestamp_start": detection.segment.start,
            "timestamp_end": detection.segment.end,
            "timestamp_formatted": format_timestamp(detection.segment.start),
            "text": detection.segment.text,
            "context": detection.context,
            "keywords": detection.keywords_found,
            "screenshot": screenshot_path.name if screenshot_path else None,
            "unified_analysis": _serialize_unified_analysis(unified_finding),
            # Merged-away evidence frames folded into this survivor (G6b). Empty
            # for un-merged findings, misses, and pre-AI snapshots.
            "merged_frames": [
                {
                    "id": member_det.segment.id,
                    "timestamp_start": member_det.segment.start,
                    "timestamp_formatted": format_timestamp(member_det.segment.start),
                    "text": member_det.segment.text,
                    "screenshot": member_path.name if member_path else None,
                }
                for member_det, member_path in row.members
            ],
        }
        report["findings"].append(finding)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    console.print(f"[green]Enhanced report saved:[/] {output_path}")
    return output_path
