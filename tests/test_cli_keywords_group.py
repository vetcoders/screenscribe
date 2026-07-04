"""CLI contract tests for the ``screenscribe keywords`` command group.

Covers init / edit / add / list against the global keywords file. The global
path is monkeypatched onto a tmp file in every test so nothing touches the real
``~/.config/screenscribe/keywords.yaml``.

CLI help/output is color-fragile: conftest forces NO_COLOR, and we still strip
ANSI + normalize whitespace via ``_plain`` so asserts test the text contract,
not the color-rendering substrate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from screenscribe import cli, keywords
from screenscribe.cli import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """Strip ANSI color codes and box borders, then collapse whitespace."""
    return " ".join(_ANSI_RE.sub("", output).replace("│", " ").split())


def _point_global_at(monkeypatch: Any, path: Path) -> None:
    """Redirect the global keywords path (in both keywords + cli) to ``path``."""
    monkeypatch.setattr(keywords, "GLOBAL_KEYWORDS_PATH", path)
    monkeypatch.setattr(cli, "GLOBAL_KEYWORDS_PATH", path)


# ---------------------------------------------------------------------------
# keywords init
# ---------------------------------------------------------------------------


def test_keywords_init_creates_global_file_from_defaults(tmp_path: Path, monkeypatch: Any) -> None:
    """init writes the global file with the built-in defaults when absent."""
    target = tmp_path / "keywords.yaml"
    _point_global_at(monkeypatch, target)

    result = CliRunner().invoke(app, ["keywords", "init"])

    assert result.exit_code == 0
    assert target.exists()
    assert target.read_bytes() == keywords.DEFAULT_KEYWORDS_PATH.read_bytes()


def test_keywords_init_does_not_overwrite_without_confirm(tmp_path: Path, monkeypatch: Any) -> None:
    """An existing file is preserved when the overwrite prompt is declined."""
    target = tmp_path / "keywords.yaml"
    target.write_text(yaml.safe_dump({"bug": ["mine"]}), encoding="utf-8")
    _point_global_at(monkeypatch, target)

    # Answer "n" to the overwrite confirmation.
    result = CliRunner().invoke(app, ["keywords", "init"], input="n\n")

    assert result.exit_code == 0
    # Original content is untouched.
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"bug": ["mine"]}
    assert "preserved" in _plain(result.output)


def test_keywords_init_overwrites_when_confirmed(tmp_path: Path, monkeypatch: Any) -> None:
    """Confirming the prompt replaces the file with the built-in defaults."""
    target = tmp_path / "keywords.yaml"
    target.write_text(yaml.safe_dump({"bug": ["mine"]}), encoding="utf-8")
    _point_global_at(monkeypatch, target)

    result = CliRunner().invoke(app, ["keywords", "init"], input="y\n")

    assert result.exit_code == 0
    assert target.read_bytes() == keywords.DEFAULT_KEYWORDS_PATH.read_bytes()


# ---------------------------------------------------------------------------
# keywords edit
# ---------------------------------------------------------------------------


def test_keywords_edit_creates_then_opens_editor(tmp_path: Path, monkeypatch: Any) -> None:
    """edit creates the file from defaults when missing, then opens the editor."""
    target = tmp_path / "keywords.yaml"
    _point_global_at(monkeypatch, target)

    opened: dict[str, Any] = {}

    def _fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        opened["cmd"] = cmd
        return None

    monkeypatch.setenv("EDITOR", "my-editor")
    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    result = CliRunner().invoke(app, ["keywords", "edit"])

    assert result.exit_code == 0
    assert target.exists()
    assert opened["cmd"][0] == "my-editor"
    assert opened["cmd"][1] == str(target)


def test_keywords_edit_opens_existing_file_without_recreating(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An existing file is opened as-is, not overwritten by defaults."""
    target = tmp_path / "keywords.yaml"
    target.write_text(yaml.safe_dump({"bug": ["keepme"]}), encoding="utf-8")
    _point_global_at(monkeypatch, target)

    monkeypatch.setenv("EDITOR", "my-editor")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)

    result = CliRunner().invoke(app, ["keywords", "edit"])

    assert result.exit_code == 0
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == {"bug": ["keepme"]}


# ---------------------------------------------------------------------------
# keywords add
# ---------------------------------------------------------------------------


def test_keywords_add_creates_file_and_appends_phrase(tmp_path: Path, monkeypatch: Any) -> None:
    """add creates the file from defaults if missing and appends the phrase."""
    target = tmp_path / "keywords.yaml"
    _point_global_at(monkeypatch, target)

    result = CliRunner().invoke(app, ["keywords", "add", "bug", "klikam i nic"])

    assert result.exit_code == 0
    assert target.exists()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert "klikam i nic" in data["bug"]


def test_keywords_add_rejects_unsupported_category(tmp_path: Path, monkeypatch: Any) -> None:
    """An unsupported category is rejected with a clear message and no write."""
    target = tmp_path / "keywords.yaml"
    _point_global_at(monkeypatch, target)

    result = CliRunner().invoke(app, ["keywords", "add", "nonsense", "x"])

    assert result.exit_code == 1
    out = _plain(result.output)
    assert "Unsupported category: nonsense" in out
    assert "bug" in out  # lists the supported categories
    # Nothing was created for a rejected category.
    assert not target.exists()


def test_keywords_add_does_not_duplicate_existing_phrase(tmp_path: Path, monkeypatch: Any) -> None:
    """An identical phrase is not appended twice."""
    target = tmp_path / "keywords.yaml"
    target.write_text(yaml.safe_dump({"bug": ["dup"]}), encoding="utf-8")
    _point_global_at(monkeypatch, target)

    result = CliRunner().invoke(app, ["keywords", "add", "bug", "dup"])

    assert result.exit_code == 0
    assert "Already present" in _plain(result.output)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["bug"] == ["dup"]


def test_keywords_add_creates_supported_category_when_missing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A supported category absent from the file is created on add."""
    target = tmp_path / "keywords.yaml"
    target.write_text(yaml.safe_dump({"bug": ["b"]}), encoding="utf-8")
    _point_global_at(monkeypatch, target)

    result = CliRunner().invoke(app, ["keywords", "add", "accessibility", "kontrast"])

    assert result.exit_code == 0
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert data["accessibility"] == ["kontrast"]


# ---------------------------------------------------------------------------
# keywords list
# ---------------------------------------------------------------------------


def test_keywords_list_reports_default_source_when_no_global_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """With no global file, list reports the default source and the hint line."""
    _point_global_at(monkeypatch, tmp_path / "absent.yaml")

    result = CliRunner().invoke(app, ["keywords", "list"])

    assert result.exit_code == 0
    out = _plain(result.output)
    assert "built-in default" in out
    # Never expose the packaged read-only default path to the user.
    assert "default_keywords.yaml" not in out
    # Point them at their own editable dictionary instead.
    assert "screenscribe keywords init" in out
    # The canonical concept name is "keywords"; "vocabulary hints" must not be a
    # second name for the same thing (locked naming decision).
    assert (
        "Keywords are passed to AI as hints during detection. "
        "They do not replace LLM analysis." in out
    )
    assert "vocabulary hints" not in out


def test_keywords_list_reports_global_source_and_counts(tmp_path: Path, monkeypatch: Any) -> None:
    """With a global file, list reports the global source and per-category counts."""
    target = tmp_path / "keywords.yaml"
    target.write_text(
        yaml.safe_dump({"bug": ["alpha", "beta"], "ui": ["gamma"]}),
        encoding="utf-8",
    )
    _point_global_at(monkeypatch, target)

    result = CliRunner().invoke(app, ["keywords", "list"])

    assert result.exit_code == 0
    out = _plain(result.output)
    assert "global dictionary" in out
    # per-category counts + sample phrases
    assert "bug: 2" in out
    assert "alpha" in out
    assert "ui: 1" in out
    assert "Total: 3" in out
