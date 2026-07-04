"""Bootstrap banner formatting regression tests."""

from __future__ import annotations

from typing import Any

from screenscribe import bootstrap


def _rendered_banner_lines(argv: list[str], capsys: Any) -> list[str]:
    bootstrap._render_banner(argv)
    return capsys.readouterr().out.splitlines()


def test_bootstrap_banner_lines_keep_same_width(capsys: object) -> None:
    lines = _rendered_banner_lines(["transcribe"], capsys)

    widths = {len(line) for line in lines}
    assert len(widths) == 1


def test_bootstrap_banner_long_command_is_clipped_to_box(capsys: object) -> None:
    lines = _rendered_banner_lines(["very-long-command-name-for-smoke"], capsys)

    widths = {len(line) for line in lines}
    assert len(widths) == 1
    assert "very-long-command-name-for-smoke" in "\n".join(lines)
