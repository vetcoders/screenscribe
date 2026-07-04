"""Report-artifact emission helpers for the screenscribe ``review`` command.

Extracted from ``cli.py``. ``_write_report_artifacts`` emits the
JSON/Markdown/HTML report trio for one video and ``_print_report_artifact_paths``
echoes only the artifacts the user asked for. ``cli.py`` re-exports both so the
historical surface (``cli._write_report_artifacts``,
``cli._print_report_artifact_paths``) is preserved, and the path-echo prints
through ``cli.console`` so a monkeypatch on it still binds.
"""

from pathlib import Path
from typing import Any

from .detect import Detection
from .report import (
    save_enhanced_json_report,
    save_enhanced_markdown_report,
    save_html_report_pro,
)


def _write_report_artifacts(
    *,
    detections: list[Detection],
    screenshots: list[tuple[Detection, Path]],
    video: Path,
    video_output: Path,
    video_stem: str,
    unified_findings: list[Any],
    executive_summary: str,
    visual_summary: str,
    errors: list[Any] | None,
    transcript: str,
    transcript_segments: list[Any] | None,
    embed_video: bool,
    language: str,
    json_report: bool,
    markdown_report: bool,
    html_report: bool,
) -> None:
    """Emit the JSON/Markdown/HTML report trio for one video.

    Shared between the empty-state path (0 POIs from prefilter, so
    ``unified_findings=[]`` and the caller passes a synthetic
    ``executive_summary``) and the main success path. Both callers route
    through this single site so output-format flags stay in lockstep
    regardless of how many findings landed.
    """
    if json_report:
        save_enhanced_json_report(
            detections,
            screenshots,
            video,
            video_output / f"{video_stem}_report.json",
            unified_findings=unified_findings,
            executive_summary=executive_summary,
            errors=errors,
            transcript=transcript,
            transcript_segments=transcript_segments,
        )

    if markdown_report:
        save_enhanced_markdown_report(
            detections,
            screenshots,
            video,
            video_output / f"{video_stem}_report.md",
            unified_findings=unified_findings,
            executive_summary=executive_summary,
            visual_summary=visual_summary,
            errors=errors,
            transcript=transcript,
            transcript_segments=transcript_segments,
        )

    if html_report:
        save_html_report_pro(
            detections,
            screenshots,
            video,
            video_output / f"{video_stem}_report.html",
            segments=transcript_segments,
            unified_findings=unified_findings,
            executive_summary=executive_summary,
            errors=errors,
            embed_video=embed_video,
            language=language,
        )


def _print_report_artifact_paths(
    *,
    video_output: Path,
    video_stem: str,
    json_report: bool,
    markdown_report: bool,
    html_report: bool,
) -> None:
    """Print only the report artifacts the user asked screenscribe to emit."""
    import screenscribe.cli as cli

    console = cli.console

    json_path = video_output / f"{video_stem}_report.json"
    md_path = video_output / f"{video_stem}_report.md"
    html_path = video_output / f"{video_stem}_report.html"

    if json_report:
        console.print(
            f"[green]Enhanced report saved:[/]\n[link=file://{json_path}]{json_path}[/link]"
        )
    if markdown_report:
        console.print(
            f"[green]Enhanced Markdown report saved:[/]\n[link=file://{md_path}]{md_path}[/link]"
        )
    if html_report:
        console.print(
            f"[green]Interactive HTML report saved:[/]\n[link=file://{html_path}]{html_path}[/link]"
        )
