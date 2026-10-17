"""Commit-provenance tests for the --version string."""

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
    # Tests run inside the repo checkout, so the live work-tree probe must
    # produce the same +g<sha> shape a baked wheel would.
    assert cli._version_string().startswith(f"v{cli.__version__}+g")


def test_baked_provenance_none_when_unbaked() -> None:
    assert screenscribe._commit.COMMIT is None
    assert cli._baked_provenance() is None
