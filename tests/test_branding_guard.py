"""Tests for the branding guard in scripts/ss_verify.py (repo-wide scan).

The guard fails on bare public-facing camelCase ``ScreenScribe`` / ``VetCoders``
brand strings anywhere in the tracked tree (minus ``BRANDING_SCAN_EXCLUDES``),
while exempting allowlisted technical identifiers (config class, JS
namespace/player, HTTP header, env prefix). These tests drive the deterministic
helpers (``scan_text_for_brand`` and ``check_branding``) on temporary strings /
tmp trees — never on the real repo — so they stay reproducible regardless of
repo state.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SS_VERIFY_PATH = REPO_ROOT / "scripts" / "ss_verify.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ss_verify_brand_under_test", SS_VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ssv = _load_module()


# ---------------------------------------------------------------------------
# scan_text_for_brand — pure string-level behaviour
# ---------------------------------------------------------------------------


def test_bare_screenscribe_in_prose_is_a_hit() -> None:
    hits = ssv.scan_text_for_brand("Welcome to ScreenScribe, the review tool.")
    assert len(hits) == 1
    line_no, brand, _suggestion = hits[0]
    assert line_no == 1
    assert brand == "ScreenScribe"


def test_bare_vetcoders_in_prose_is_a_hit() -> None:
    hits = ssv.scan_text_for_brand("Built by VetCoders.")
    assert len(hits) == 1
    assert hits[0][1] == "VetCoders"


def test_allowlisted_config_class_passes() -> None:
    assert ssv.scan_text_for_brand("cfg = ScreenScribeConfig.load()") == []


def test_allowlisted_js_lib_passes() -> None:
    assert ssv.scan_text_for_brand("window.ScreenScribeLib.init();") == []


def test_allowlisted_js_player_passes() -> None:
    assert ssv.scan_text_for_brand("const p = new ScreenScribePlayer();") == []


def test_allowlisted_http_header_passes() -> None:
    assert ssv.scan_text_for_brand("headers.set('X-ScreenScribe-Token', token);") == []


def test_allowlisted_env_prefix_passes() -> None:
    assert ssv.scan_text_for_brand("export SCREENSCRIBE_API_KEY=foo") == []


def test_lowercase_canonical_spelling_passes() -> None:
    assert ssv.scan_text_for_brand("Install screenscribe via uv. Built by vetcoders.") == []


def test_bare_brand_alongside_allowlisted_token_still_fails() -> None:
    # The allowlisted token is masked, but the bare brand on the same line stays.
    text = "ScreenScribeConfig powers ScreenScribe the product."
    hits = ssv.scan_text_for_brand(text)
    assert len(hits) == 1
    assert hits[0][1] == "ScreenScribe"


def test_line_numbers_are_reported() -> None:
    text = "ok line\nScreenScribe here\nok line\nVetCoders there"
    hits = ssv.scan_text_for_brand(text)
    assert {(ln, b) for ln, b, _ in hits} == {(2, "ScreenScribe"), (4, "VetCoders")}


# ---------------------------------------------------------------------------
# check_branding — file-tree behaviour on a tmp scan path
# ---------------------------------------------------------------------------


def _contract(target: Path):
    return ssv.load_contract(target)


def test_check_branding_fails_on_public_prose(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("ScreenScribe is great.\n", encoding="utf-8")
    res = ssv.check_branding(tmp_path, _contract(tmp_path))
    assert res.ok is False
    assert "README.md" in res.detail


def test_check_branding_passes_when_clean(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "screenscribe, built by vetcoders. Use ScreenScribeConfig and "
        "X-ScreenScribe-Token and SCREENSCRIBE_API_KEY.\n",
        encoding="utf-8",
    )
    res = ssv.check_branding(tmp_path, _contract(tmp_path))
    assert res.ok is True


def test_check_branding_scans_docs_dir(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "GUIDE.md").write_text("Powered by VetCoders.\n", encoding="utf-8")
    res = ssv.check_branding(tmp_path, _contract(tmp_path))
    assert res.ok is False
    assert "docs/GUIDE.md" in res.detail


def test_check_branding_catches_unlisted_paths(tmp_path: Path) -> None:
    # Repo-wide scan: a bare brand in ANY new file (no hand-maintained list) is
    # caught — this is the blind spot the bounded-list guard used to miss.
    (tmp_path / "analyze_server.py").write_text("# ScreenScribe server\n", encoding="utf-8")
    res = ssv.check_branding(tmp_path, _contract(tmp_path))
    assert res.ok is False
    assert "analyze_server.py" in res.detail


def test_check_branding_honors_excludes(tmp_path: Path) -> None:
    # Paths that legitimately NAME the brand they forbid (the guard's own
    # machinery + churn/vendored) are excluded, so they never self-trip the gate.
    for rel in ("Makefile", "scripts/ss_verify.py", "tests/test_branding_guard.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("mentions ScreenScribe and VetCoders\n", encoding="utf-8")
    res = ssv.check_branding(tmp_path, _contract(tmp_path))
    assert res.ok is True


def test_check_branding_skips_binary(tmp_path: Path) -> None:
    # A binary file in a scanned path is skipped (NUL-byte sniff), not decoded.
    (tmp_path / "README.md").write_bytes(b"\x00\x01ScreenScribe\x00")
    res = ssv.check_branding(tmp_path, _contract(tmp_path))
    assert res.ok is True
