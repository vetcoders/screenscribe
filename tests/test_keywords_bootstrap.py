"""Unit tests for keywords config loading and bootstrap startup logic.

Pins:
- keywords.KeywordsConfig.load priority chain (explicit / search-path / defaults)
- keywords.KeywordsConfig._load_from_file error + format handling
- keywords accessor methods (get_keywords, total_keywords, summary)
- keywords.save_default_keywords copy behavior
- bootstrap completion detection, banner gating, version resolve, main() wiring
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
import yaml

from screenscribe import bootstrap, keywords
from screenscribe.keywords import (
    KeywordsConfig,
    format_keywords_hint,
    save_default_keywords,
)

# ---------------------------------------------------------------------------
# keywords.KeywordsConfig.load
# ---------------------------------------------------------------------------


def test_load_explicit_file_used_when_it_exists(tmp_path: Path) -> None:
    """An explicit existing keywords_file is parsed directly."""
    kw = tmp_path / "custom.yaml"
    kw.write_text(
        yaml.safe_dump({"bug": ["boom"], "change": ["tweak"], "ui": ["button"]}),
        encoding="utf-8",
    )

    cfg = KeywordsConfig.load(keywords_file=kw)

    assert cfg.bug == ["boom"]
    assert cfg.change == ["tweak"]
    assert cfg.ui == ["button"]


def test_load_explicit_missing_file_falls_back_to_defaults(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    """A missing explicit file warns and falls back to built-in defaults."""
    # Point the global path at a non-existent file so the default is reached.
    monkeypatch.setattr(keywords, "GLOBAL_KEYWORDS_PATH", tmp_path / "absent-global.yaml")
    missing = tmp_path / "nope.yaml"

    cfg = KeywordsConfig.load(keywords_file=missing)

    out = capsys.readouterr().out
    assert "not found" in out
    # Built-in defaults are non-empty across categories.
    assert cfg.total_keywords > 0
    assert cfg.bug  # default file has bug keywords


def test_load_uses_global_file_when_present(tmp_path: Path, monkeypatch: Any) -> None:
    """With no explicit file, the global user file is loaded (not a cwd file)."""
    global_file = tmp_path / "global-keywords.yaml"
    global_file.write_text(yaml.safe_dump({"bug": ["globalhit"]}), encoding="utf-8")
    monkeypatch.setattr(keywords, "GLOBAL_KEYWORDS_PATH", global_file)

    cfg = KeywordsConfig.load()

    assert cfg.bug == ["globalhit"]
    # categories absent in the file default to empty lists
    assert cfg.change == []
    assert cfg.ui == []
    assert cfg.performance == []
    assert cfg.accessibility == []
    assert cfg.other == []


def test_load_ignores_cwd_keywords_file(tmp_path: Path, monkeypatch: Any) -> None:
    """A keywords.yaml in the cwd is NOT auto-loaded (cwd search removed)."""
    monkeypatch.chdir(tmp_path)
    # Point the global path elsewhere so only the cwd file could win — it must not.
    monkeypatch.setattr(keywords, "GLOBAL_KEYWORDS_PATH", tmp_path / "absent-global.yaml")
    (tmp_path / "keywords.yaml").write_text(yaml.safe_dump({"bug": ["cwdhit"]}), encoding="utf-8")

    cfg = KeywordsConfig.load()

    # The cwd file is ignored; the built-in default is used instead.
    assert "cwdhit" not in cfg.bug
    assert cfg.total_keywords > 0


def test_load_falls_back_to_embedded_defaults_when_no_files(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """With no explicit file and no global file, built-in defaults load."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(keywords, "GLOBAL_KEYWORDS_PATH", tmp_path / "absent-global.yaml")

    cfg = KeywordsConfig.load()

    assert cfg.total_keywords > 0


# ---------------------------------------------------------------------------
# keywords.KeywordsConfig._load_from_file
# ---------------------------------------------------------------------------


def test_load_from_file_non_dict_yaml_returns_defaults(tmp_path: Path, capsys: Any) -> None:
    """A YAML list (not a dict) is rejected and defaults are loaded (lines 71-73)."""
    bad = tmp_path / "list.yaml"
    bad.write_text(yaml.safe_dump(["a", "b"]), encoding="utf-8")

    cfg = KeywordsConfig._load_from_file(bad)

    assert "Invalid keywords file format" in capsys.readouterr().out
    # Defaults are populated, not the rejected list.
    assert cfg.total_keywords > 0


def test_load_from_file_invalid_yaml_returns_defaults(tmp_path: Path, capsys: Any) -> None:
    """A YAMLError during parse is caught and defaults load (lines 81-83)."""
    bad = tmp_path / "broken.yaml"
    # Unbalanced bracket triggers a yaml.YAMLError.
    bad.write_text("bug: [unterminated\n", encoding="utf-8")

    cfg = KeywordsConfig._load_from_file(bad)

    assert "Error parsing keywords file" in capsys.readouterr().out
    assert cfg.total_keywords > 0


def test_load_from_file_os_error_returns_defaults(capsys: Any) -> None:
    """An OSError (e.g. path is a directory) is caught and defaults load (lines 84-86)."""
    # Opening a directory path raises OSError (IsADirectoryError) on read.
    cfg = KeywordsConfig._load_from_file(Path("/"))

    assert "Error reading keywords file" in capsys.readouterr().out
    assert cfg.total_keywords > 0


def test_load_from_file_parses_all_six_categories(tmp_path: Path) -> None:
    """All six categories are read from a well-formed dict."""
    good = tmp_path / "k.yaml"
    good.write_text(
        yaml.safe_dump(
            {
                "bug": ["x"],
                "change": ["y"],
                "ui": ["z"],
                "performance": ["p"],
                "accessibility": ["a"],
                "other": ["o"],
            }
        ),
        encoding="utf-8",
    )

    cfg = KeywordsConfig._load_from_file(good)

    assert cfg.bug == ["x"]
    assert cfg.change == ["y"]
    assert cfg.ui == ["z"]
    assert cfg.performance == ["p"]
    assert cfg.accessibility == ["a"]
    assert cfg.other == ["o"]


def test_load_from_file_empty_file_is_safe_empty_config(tmp_path: Path) -> None:
    """An empty YAML file is a no-op: an empty (safe) config, no defaults injected."""
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")

    cfg = KeywordsConfig._load_from_file(empty)

    assert cfg.total_keywords == 0


def test_load_from_file_empty_category_is_safe(tmp_path: Path) -> None:
    """A present-but-empty category yields an empty list, never raises."""
    f = tmp_path / "k.yaml"
    f.write_text(yaml.safe_dump({"bug": [], "change": None}), encoding="utf-8")

    cfg = KeywordsConfig._load_from_file(f)

    assert cfg.bug == []
    assert cfg.change == []


# ---------------------------------------------------------------------------
# keywords accessors
# ---------------------------------------------------------------------------


def test_get_keywords_returns_each_category_and_empty_for_unknown() -> None:
    """get_keywords dispatches per category and returns [] for unknown."""
    cfg = KeywordsConfig(
        bug=["b"],
        change=["c"],
        ui=["u"],
        performance=["p"],
        accessibility=["a"],
        other=["o"],
    )

    assert cfg.get_keywords("bug") == ["b"]
    assert cfg.get_keywords("change") == ["c"]
    assert cfg.get_keywords("ui") == ["u"]
    assert cfg.get_keywords("performance") == ["p"]
    assert cfg.get_keywords("accessibility") == ["a"]
    assert cfg.get_keywords("other") == ["o"]
    assert cfg.get_keywords("does-not-exist") == []


def test_total_keywords_sums_all_six_categories() -> None:
    """total_keywords is the sum across all six categories."""
    cfg = KeywordsConfig(
        bug=["a", "b"],
        change=["c"],
        ui=["d", "e", "f"],
        performance=["g"],
        accessibility=["h", "i"],
        other=["j"],
    )

    assert cfg.total_keywords == 10


def test_summary_reports_per_category_counts() -> None:
    """summary() renders human-readable per-category counts for all six categories."""
    cfg = KeywordsConfig(
        bug=["a"],
        change=["b", "c"],
        ui=[],
        performance=["d"],
        accessibility=[],
        other=["e", "f", "g"],
    )

    assert cfg.summary() == (
        "Keywords: 1 bug, 2 change, 0 ui, 1 performance, 0 accessibility, 3 other"
    )


# ---------------------------------------------------------------------------
# keywords.save_default_keywords
# ---------------------------------------------------------------------------


def test_save_default_keywords_copies_embedded_file(tmp_path: Path, capsys: Any) -> None:
    """save_default_keywords copies the embedded defaults to the target (lines 120-123)."""
    target = tmp_path / "out.yaml"

    save_default_keywords(target)

    assert target.exists()
    # Copied content matches the embedded default file byte-for-byte.
    assert target.read_bytes() == keywords.DEFAULT_KEYWORDS_PATH.read_bytes()
    assert "Default keywords saved" in capsys.readouterr().out
    # The copied file is itself a loadable keywords config.
    cfg = KeywordsConfig.load(keywords_file=target)
    assert cfg.total_keywords > 0


def test_save_default_keywords_creates_parent_dirs(tmp_path: Path) -> None:
    """save_default_keywords creates missing parent directories."""
    target = tmp_path / "nested" / "dir" / "keywords.yaml"

    save_default_keywords(target)

    assert target.exists()


# ---------------------------------------------------------------------------
# keywords.format_keywords_hint
# ---------------------------------------------------------------------------


def test_format_keywords_hint_empty_config_is_empty_string() -> None:
    """An empty config formats to an empty string (no-op injection when empty)."""
    assert format_keywords_hint(KeywordsConfig()) == ""


def test_format_keywords_hint_lists_only_non_empty_categories() -> None:
    """Only categories with phrases appear; phrases are quoted and labelled."""
    cfg = KeywordsConfig(bug=["klikam i nic"], performance=["za ciężkie"])

    hint = format_keywords_hint(cfg)

    assert "hints, not rules" in hint
    assert 'bug: "klikam i nic"' in hint
    assert 'performance: "za ciężkie"' in hint
    # Empty categories are omitted entirely.
    assert "change:" not in hint
    assert "accessibility:" not in hint


# ---------------------------------------------------------------------------
# bootstrap._is_completion_invocation
# ---------------------------------------------------------------------------


def test_is_completion_invocation_true_when_complete_env_present(
    monkeypatch: Any,
) -> None:
    """Any *_COMPLETE env var marks the call as a shell-completion invocation (line 19)."""
    monkeypatch.setenv("_SCREENSCRIBE_COMPLETE", "complete_zsh")

    assert bootstrap._is_completion_invocation() is True


def test_is_completion_invocation_false_without_complete_env(
    monkeypatch: Any,
) -> None:
    """Without a *_COMPLETE env var, it is not a completion invocation."""
    monkeypatch.delenv("_SCREENSCRIBE_COMPLETE", raising=False)
    # Ensure no stray *_COMPLETE keys leak from the test runner.
    for key in list(__import__("os").environ):
        if key.endswith("_COMPLETE"):
            monkeypatch.delenv(key, raising=False)

    assert bootstrap._is_completion_invocation() is False


# ---------------------------------------------------------------------------
# bootstrap._should_render_banner
# ---------------------------------------------------------------------------


def _clear_banner_env(monkeypatch: Any) -> None:
    monkeypatch.delenv(bootstrap._BOOTSTRAP_SHOWN_ENV, raising=False)
    monkeypatch.delenv(bootstrap._DISABLE_BANNER_ENV, raising=False)
    for key in list(__import__("os").environ):
        if key.endswith("_COMPLETE"):
            monkeypatch.delenv(key, raising=False)


def test_should_render_banner_true_for_interactive_tty(monkeypatch: Any) -> None:
    """Banner renders for an interactive tty with a plain subcommand (lines 24-34)."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setattr(bootstrap.sys.stdout, "isatty", lambda: True)

    assert bootstrap._should_render_banner(["transcribe"]) is True


def test_should_render_banner_false_when_already_shown(monkeypatch: Any) -> None:
    """The shown-marker env suppresses a repeat banner (line 24-25)."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setenv(bootstrap._BOOTSTRAP_SHOWN_ENV, "1")
    monkeypatch.setattr(bootstrap.sys.stdout, "isatty", lambda: True)

    assert bootstrap._should_render_banner(["transcribe"]) is False


def test_should_render_banner_false_when_disabled(monkeypatch: Any) -> None:
    """The explicit disable env suppresses the banner (lines 26-27)."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setenv(bootstrap._DISABLE_BANNER_ENV, "1")
    monkeypatch.setattr(bootstrap.sys.stdout, "isatty", lambda: True)

    assert bootstrap._should_render_banner(["transcribe"]) is False


def test_should_render_banner_false_during_completion(monkeypatch: Any) -> None:
    """A completion invocation stays silent (lines 28-29)."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setenv("_SCREENSCRIBE_COMPLETE", "complete_bash")
    monkeypatch.setattr(bootstrap.sys.stdout, "isatty", lambda: True)

    assert bootstrap._should_render_banner(["transcribe"]) is False


def test_should_render_banner_false_when_not_a_tty(monkeypatch: Any) -> None:
    """Non-tty stdout (pipe/redirect) suppresses the banner (lines 30-31)."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setattr(bootstrap.sys.stdout, "isatty", lambda: False)

    assert bootstrap._should_render_banner(["transcribe"]) is False


def test_should_render_banner_false_for_help_flag(monkeypatch: Any) -> None:
    """--help / -h suppress the banner so help output stays clean (lines 32-33)."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setattr(bootstrap.sys.stdout, "isatty", lambda: True)

    assert bootstrap._should_render_banner(["--help"]) is False
    assert bootstrap._should_render_banner(["-h"]) is False


# ---------------------------------------------------------------------------
# bootstrap._resolve_version
# ---------------------------------------------------------------------------


def test_resolve_version_returns_installed_version(monkeypatch: Any) -> None:
    """_resolve_version returns the packaging metadata version when present (line 41)."""
    monkeypatch.setattr(bootstrap.metadata, "version", lambda name: "9.9.9")

    assert bootstrap._resolve_version() == "9.9.9"


def test_resolve_version_falls_back_to_dev_when_not_installed(
    monkeypatch: Any,
) -> None:
    """When the package is not installed, version resolves to 'dev' (lines 40-42)."""

    def _raise(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(bootstrap.metadata, "version", _raise)

    assert bootstrap._resolve_version() == "dev"


# ---------------------------------------------------------------------------
# bootstrap._render_banner content branches
# ---------------------------------------------------------------------------


def test_render_banner_interactive_line_for_empty_argv(monkeypatch: Any, capsys: Any) -> None:
    """Empty argv renders the 'interactive' start line (lines 65-66)."""
    monkeypatch.setattr(bootstrap, "_resolve_version", lambda: "1.0.0")

    bootstrap._render_banner([])

    out = capsys.readouterr().out
    assert "Starting command: interactive" in out


def test_render_banner_subcommand_line_for_plain_first_arg(monkeypatch: Any, capsys: Any) -> None:
    """A plain (non-flag) first arg is announced as the starting command (lines 67-68)."""
    monkeypatch.setattr(bootstrap, "_resolve_version", lambda: "1.0.0")

    bootstrap._render_banner(["review"])

    out = capsys.readouterr().out
    assert "Starting command: review" in out


def test_render_banner_omits_start_line_for_flag_first_arg(monkeypatch: Any, capsys: Any) -> None:
    """A flag first arg suppresses the 'Starting command' line entirely."""
    monkeypatch.setattr(bootstrap, "_resolve_version", lambda: "1.0.0")

    bootstrap._render_banner(["--version"])

    out = capsys.readouterr().out
    assert "Starting command" not in out


# ---------------------------------------------------------------------------
# bootstrap.main
# ---------------------------------------------------------------------------


def test_main_renders_banner_and_sets_shown_env_then_invokes_app(
    monkeypatch: Any,
) -> None:
    """main() renders banner, marks shown env, then calls the CLI app (lines 77-84)."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setattr(bootstrap.sys, "argv", ["screenscribe", "transcribe"])
    monkeypatch.setattr(bootstrap, "_should_render_banner", lambda argv: True)

    rendered: dict[str, Any] = {}
    monkeypatch.setattr(bootstrap, "_render_banner", lambda argv: rendered.setdefault("argv", argv))

    called: dict[str, bool] = {}

    import screenscribe.cli as cli_mod

    monkeypatch.setattr(cli_mod, "app", lambda: called.setdefault("app", True))

    bootstrap.main()

    assert rendered["argv"] == ["transcribe"]
    assert __import__("os").environ[bootstrap._BOOTSTRAP_SHOWN_ENV] == "1"
    assert called["app"] is True


def test_main_skips_banner_when_not_rendered_but_still_invokes_app(
    monkeypatch: Any,
) -> None:
    """When the banner is gated off, main() skips render but still runs the app."""
    _clear_banner_env(monkeypatch)
    monkeypatch.setattr(bootstrap.sys, "argv", ["screenscribe"])
    monkeypatch.setattr(bootstrap, "_should_render_banner", lambda argv: False)

    def _fail_render(argv: list[str]) -> None:
        raise AssertionError("banner should not render when gated off")

    monkeypatch.setattr(bootstrap, "_render_banner", _fail_render)

    called: dict[str, bool] = {}

    import screenscribe.cli as cli_mod

    monkeypatch.setattr(cli_mod, "app", lambda: called.setdefault("app", True))

    bootstrap.main()

    # Shown-env is not set when the banner was gated off.
    assert bootstrap._BOOTSTRAP_SHOWN_ENV not in __import__("os").environ
    assert called["app"] is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
