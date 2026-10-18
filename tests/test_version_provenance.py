"""Commit-provenance tests for the --version string."""

from types import SimpleNamespace

import screenscribe._commit
import screenscribe.cli as cli


def test_version_string_prefers_baked_commit(monkeypatch) -> None:
    monkeypatch.setattr(screenscribe._commit, "COMMIT", "abc1234")
    monkeypatch.setattr(screenscribe._commit, "DIRTY", False)
    assert cli._version_string() == f"v{cli.__version__}+gabc1234"


def test_version_string_baked_dirty_tree(monkeypatch) -> None:
    monkeypatch.setattr(screenscribe._commit, "COMMIT", "abc1234")
    monkeypatch.setattr(screenscribe._commit, "DIRTY", True)
    assert cli._version_string() == f"v{cli.__version__}+gabc1234-dirty"


def test_version_string_falls_back_to_live_git_when_unbaked(monkeypatch) -> None:
    monkeypatch.setattr(screenscribe._commit, "COMMIT", None)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/mock/git")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(stdout="abc1234\n" if "rev-parse" in args else ""),
    )
    assert cli._version_string() == f"v{cli.__version__}+gabc1234"


def test_baked_provenance_none_when_unbaked(monkeypatch) -> None:
    monkeypatch.setattr(screenscribe._commit, "COMMIT", None)
    assert cli._baked_provenance() is None


def test_version_string_without_baked_commit_or_git(monkeypatch) -> None:
    monkeypatch.setattr(screenscribe._commit, "COMMIT", None)
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
    assert cli._version_string() == f"v{cli.__version__}"
