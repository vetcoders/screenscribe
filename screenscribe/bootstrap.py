"""Fast startup entrypoint for screenscribe CLI.

Shows immediate visual feedback before importing heavy CLI modules.
"""

from __future__ import annotations

import os
import sys
from importlib import metadata

_BOOTSTRAP_SHOWN_ENV = "SCREENSCRIBE_BOOTSTRAP_BANNER_SHOWN"
_DISABLE_BANNER_ENV = "SCREENSCRIBE_BOOTSTRAP_NO_BANNER"
_BANNER_TEXT_WIDTH = 50


def _is_completion_invocation() -> bool:
    """Detect shell completion bootstrap calls and stay silent."""
    return any(key.endswith("_COMPLETE") for key in os.environ)


def _should_render_banner(argv: list[str]) -> bool:
    """Render banner only in interactive terminal usage."""
    if os.environ.get(_BOOTSTRAP_SHOWN_ENV) == "1":
        return False
    if os.environ.get(_DISABLE_BANNER_ENV) == "1":
        return False
    if _is_completion_invocation():
        return False
    if not sys.stdout.isatty():
        return False
    if "--help" in argv or "-h" in argv:
        return False
    return True


def _resolve_version() -> str:
    """Read installed package version without importing screenscribe package."""
    try:
        return metadata.version("screenscribe")
    except metadata.PackageNotFoundError:
        return "dev"


def _banner_line(text: str) -> str:
    """Render one fixed-width banner content line."""
    clipped = text[:_BANNER_TEXT_WIDTH]
    return f"│ {clipped:<{_BANNER_TEXT_WIDTH}} │"


def _render_banner(argv: list[str]) -> None:
    """Print immediate startup feedback after command submit."""
    version = _resolve_version()
    first_arg = argv[0] if argv else None

    lines = [
        f"╭{'─' * (_BANNER_TEXT_WIDTH + 2)}╮",
        _banner_line(f"Screenscribe v{version}"),
        _banner_line("Turn screen recordings into engineering reports"),
    ]
    # Only claim "Starting command" when argv[0] is plausibly a subcommand
    # or an auto-review path. Flags (-x / --foo) are parser input — typer
    # may reject them, and the banner should not announce execution of
    # something that will not run.
    if first_arg is None:
        lines.append(_banner_line("Starting command: interactive"))
    elif not first_arg.startswith("-"):
        lines.append(_banner_line(f"Starting command: {first_arg}"))
    lines.append(f"╰{'─' * (_BANNER_TEXT_WIDTH + 2)}╯")

    # Keep it plain/fast so it appears before heavy imports begin.
    print("\n".join(lines), flush=True)


def main() -> None:
    """Entry point used by console script."""
    argv = sys.argv[1:]
    if _should_render_banner(argv):
        _render_banner(argv)
        os.environ[_BOOTSTRAP_SHOWN_ENV] = "1"

    from .cli import app

    app()
