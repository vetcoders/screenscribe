"""Console (rich) report rendering."""

from datetime import datetime
from pathlib import Path

from rich.panel import Panel
from rich.table import Table

from ..detect import Detection, format_timestamp
from .data import console


def print_report(
    detections: list[Detection], screenshots: list[tuple[Detection, Path]], video_path: Path
) -> None:
    """Print a rich console report of findings."""
    console.print()
    console.print(
        Panel(
            f"[bold]Video Review Report[/]\n{video_path.name}",
            subtitle=f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        )
    )
    console.print()

    # Summary table
    table = Table(title="Findings Summary")
    table.add_column("Category", style="cyan")
    table.add_column("Count", justify="right")

    bugs = sum(1 for d in detections if d.category == "bug")
    changes = sum(1 for d in detections if d.category == "change")
    ui = sum(1 for d in detections if d.category == "ui")

    table.add_row("Bugs", str(bugs))
    table.add_row("Change Requests", str(changes))
    table.add_row("UI Issues", str(ui))
    table.add_row("[bold]Total[/]", f"[bold]{len(detections)}[/]")

    console.print(table)
    console.print()

    # Detailed findings
    for i, (detection, screenshot_path) in enumerate(screenshots, 1):
        category_color = {"bug": "red", "change": "yellow", "ui": "blue"}.get(
            detection.category, "white"
        )

        console.print(
            Panel(
                f"[bold]{detection.segment.text}[/]\n\n"
                f"[dim]Context: {detection.context[:200]}...[/]\n\n"
                f"[dim]Screenshot: {screenshot_path}[/]",
                title=f"[{category_color}]#{i} {detection.category.upper()}[/] "
                f"@ {format_timestamp(detection.segment.start)}",
                border_style=category_color,
            )
        )
        console.print()
