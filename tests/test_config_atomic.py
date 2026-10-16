"""Secret-bearing config replacement must preserve the old state on failure."""

import os
import stat
from pathlib import Path

import pytest

from screenscribe.config import ScreenScribeConfig


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_key_replacement_preserves_exact_backup_and_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newline: bytes
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".config/screenscribe/config.env"
    path.parent.mkdir(parents=True)
    original = newline.join([b"# comment", b"SCREENSCRIBE_API_KEY=old", b"OTHER=value", b""])
    path.write_bytes(original)
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(b"older backup")
    path.chmod(0o644)
    backup.chmod(0o644)

    ScreenScribeConfig().save_api_key("new")

    assert backup.read_bytes() == original
    assert path.read_bytes() == original.replace(b"=old", b"=new")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600


@pytest.mark.parametrize("failed_name", ["config.env.bak", "config.env"])
def test_failed_replace_keeps_original_config_and_previous_or_completed_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_name: str
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".config/screenscribe/config.env"
    path.parent.mkdir(parents=True)
    original = b"SCREENSCRIBE_API_KEY=old\r\n# note\nOTHER=value\r"
    path.write_bytes(original)
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(b"previous backup")
    replace = os.replace

    def fail(source: str | Path, destination: str | Path) -> None:
        # The existing config remains intact while the replacement is staged.
        assert path.read_bytes() == original
        if os.name != "nt":
            assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        if Path(destination).name == failed_name:
            raise OSError("simulated replace failure")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", fail)
    config = ScreenScribeConfig(api_key="old")
    with pytest.raises(OSError, match="simulated"):
        config.save_api_key("new")
    assert path.read_bytes() == original
    assert config.api_key == "old"  # pragma: allowlist secret -- synthetic rollback value
    assert backup.read_bytes() == (b"previous backup" if failed_name.endswith(".bak") else original)
    assert not list(path.parent.glob(".config.env.*"))


def test_failed_flush_never_truncates_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.env"
    path.write_bytes(b"original bytes")

    def fail(_fd: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="fsync"):
        ScreenScribeConfig._rewrite_api_key_line(path, "new")
    assert path.read_bytes() == b"original bytes"
    assert not list(tmp_path.glob(".config.env.*"))


def test_backup_is_binary_exact(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    original = b"\xff\xfe\x00\r\n"
    path.write_bytes(original)
    assert ScreenScribeConfig._backup_config(path).read_bytes() == original


def test_append_respects_existing_crlf(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_bytes(b"# comment\r\nOTHER=value")
    ScreenScribeConfig._rewrite_api_key_line(path, "new")
    assert path.read_bytes() == b"# comment\r\nOTHER=value\r\nSCREENSCRIBE_API_KEY=new\r\n"
