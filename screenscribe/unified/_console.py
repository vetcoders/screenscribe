"""Shared rich Console for the unified package.

A single Console instance is shared by every submodule that prints, so the
rich Live/Progress display in the orchestrator does not fragment across
multiple consoles.
"""

from __future__ import annotations

from rich.console import Console

console = Console()
