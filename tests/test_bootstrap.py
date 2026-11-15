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


def test_should_render_banner_suppressed_for_json_flag(monkeypatch: Any) -> None:
    monkeypatch.delenv(bootstrap._BOOTSTRAP_SHOWN_ENV, raising=False)
    monkeypatch.delenv(bootstrap._DISABLE_BANNER_ENV, raising=False)
    monkeypatch.setattr(bootstrap, "_is_completion_invocation", lambda: False)
    monkeypatch.setattr(bootstrap.sys.stdout, "isatty", lambda: True)

    assert bootstrap._should_render_banner(["publish", "demo_review"]) is True
    assert bootstrap._should_render_banner(["publish", "demo_review", "--json"]) is False
    assert bootstrap._should_render_banner(["--help"]) is False
