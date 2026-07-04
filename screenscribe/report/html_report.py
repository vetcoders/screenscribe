"""Pro HTML report artifact."""

from datetime import datetime
from pathlib import Path
from typing import Any

from ..detect import Detection, format_timestamp
from ..html_pro import render_html_report_pro
from ..image_utils import encode_image_base64
from ..transcribe import Segment
from .data import (
    DEGRADED_MARKER_LABEL,
    UnifiedFindingResolver,
    _prepare_html_video_source,
    _serialize_unified_analysis,
    console,
    fold_screenshots,
)


def save_html_report_pro(
    detections: list[Detection],
    screenshots: list[tuple[Detection, Path]],
    video_path: Path,
    output_path: Path,
    segments: list[Segment] | None = None,
    unified_findings: list[Any] | None = None,
    executive_summary: str = "",
    errors: list[dict[str, str]] | None = None,
    embed_video: bool = False,
    language: str = "en",
) -> Path:
    """Save report as Pro HTML with video player and synchronized subtitles.

    Args:
        detections: List of detections
        screenshots: List of (detection, screenshot_path) tuples
        video_path: Path to source video
        output_path: Path to save HTML report
        segments: List of transcript segments for subtitle sync
        unified_findings: List of UnifiedFinding from unified VLM analysis
        executive_summary: Executive summary text
        errors: List of pipeline errors
        embed_video: Whether to embed video as base64 (for smaller files)
        language: Subtitle language code used in embedded VTT metadata

    Returns:
        Path to saved report
    """
    # Single source of truth shared with the JSON report. The resolver is
    # merged-aware (composite (detection_id, timestamp) key) and also resolves
    # by the screenshot a finding was analyzed from — so a narrow
    # {detection_id: f} dict no longer (a) resurrects deduplicated screenshots
    # as fabricated "medium" issues nor (b) collapses all id=0 POIs to one
    # last-wins analysis (BH5/BH55).
    resolver = UnifiedFindingResolver(unified_findings)

    # G6b: fold per-frame screenshots into per-survivor rows so the auto LLM-merge
    # reduction reaches the HTML report. Merged-away member frames fold in as
    # evidence under their survivor rather than rendering as extra findings.
    rows = fold_screenshots(screenshots, unified_findings)

    # Build findings data for template
    findings_data: list[dict[str, Any]] = []
    for row in rows:
        detection = row.detection
        screenshot_path = row.screenshot_path
        uf = resolver.resolve(detection, screenshot_path)

        # Encode screenshot as base64 if exists
        screenshot_b64 = ""
        if screenshot_path.exists():
            screenshot_b64 = encode_image_base64(screenshot_path)

        finding: dict[str, Any] = {
            "id": detection.segment.id,
            "category": detection.category,
            "timestamp_formatted": format_timestamp(detection.segment.start),
            "timestamp": detection.segment.start,
            "text": detection.segment.text,
            "context": detection.context,
            "keywords": detection.keywords_found,
            # Base64 for HTML display, file path for JSON export
            "screenshot": f"data:image/png;base64,{screenshot_b64}" if screenshot_b64 else "",
            "screenshot_path": screenshot_path.name if screenshot_path.exists() else "",
            # Merged-away evidence frames (G6b); empty for un-merged findings.
            "merged_frames": [
                {
                    "id": member_det.segment.id,
                    "timestamp_formatted": format_timestamp(member_det.segment.start),
                    "timestamp": member_det.segment.start,
                    "text": member_det.segment.text,
                    # Screenshots ship under "<report-root>/screenshots/" while
                    # the HTML report lives at the report root, and the merged-
                    # evidence thumbnail is rendered straight from this ref (the
                    # main thumbnails embed base64 instead). A bare ".name"
                    # resolves to "report-root/<file>" → 404, so keep the
                    # loadable "screenshots/<file>" prefix.
                    "screenshot_path": f"screenshots/{member_path.name}" if member_path else "",
                }
                for member_det, member_path in row.members
            ],
        }

        # One serializer, one key-set — shared with the JSON report. When no
        # unified finding matches (genuine analysis miss, NOT a merged-away
        # duplicate, which now resolves to its merged finding) the serializer
        # emits an honest status="missing" payload instead of a fabricated
        # "medium" issue carrying the raw transcript text.
        unified_analysis = _serialize_unified_analysis(uf)
        # D7: the Pro template (out of this module) renders the summary field but
        # has no degraded-aware badge of its own. Prefix a visible, operator-
        # readable marker into the rendered summary so a low-confidence finding
        # is shown as unverified instead of masquerading as a vetted issue. The
        # machine-readable `degraded` flag + raw confidence fields stay intact in
        # the embedded findings JSON for the interactive viewer.
        if unified_analysis.get("degraded"):
            existing_summary = unified_analysis.get("summary", "")
            marker = f"[{DEGRADED_MARKER_LABEL}]"
            unified_analysis["summary"] = (
                f"{marker} {existing_summary}".strip() if existing_summary else marker
            )
        finding["unified_analysis"] = unified_analysis

        findings_data.append(finding)

    # Sort findings by severity (critical=0, high=1, medium=2, low=3)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    findings_data.sort(
        key=lambda f: severity_order.get(f.get("unified_analysis", {}).get("severity", "medium"), 4)
    )

    # Render HTML using Pro template
    report_video_source = _prepare_html_video_source(video_path, output_path)
    html_content = render_html_report_pro(
        video_name=video_path.name,
        video_path=report_video_source,
        generated_at=datetime.now().isoformat(),
        executive_summary=executive_summary,
        findings=findings_data,
        segments=segments,
        errors=errors or [],
        embed_video=embed_video,
        language=language,
    )

    # Write HTML file
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    console.print(f"[green]Interactive HTML report saved:[/] {output_path}")
    return output_path
