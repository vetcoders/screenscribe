"""Tests for the standalone portable verifier scripts/ss_verify.py.

These exercise the verifier's contract WITHOUT paying the full heavy run
(wheel build + isolated install + pytest) on every invocation:

* fail-closed semantics: a failing sub-check makes RESULT NOT READY, and an
  unexpected exception in a check FAILS that check (never a false READY),
* no-junk detection on a tmp tree seeded with junk,
* contract loading from .screenscribe-verify.yml with default fallback,
* a full end-to-end READY run on the healthy repo (marked ``integration`` /
  ``slow`` so it stays out of the fast gate but still provable on demand).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SS_VERIFY_PATH = REPO_ROOT / "scripts" / "ss_verify.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ss_verify_under_test", SS_VERIFY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ssv = _load_module()


# ---------------------------------------------------------------------------
# fail-closed semantics
# ---------------------------------------------------------------------------


def test_safe_wrapper_turns_exception_into_fail() -> None:
    """An unexpected exception inside a check FAILS it — never a silent pass."""

    def boom() -> ssv.CheckResult:
        raise RuntimeError("kaboom")

    result = ssv._safe("explosive", boom)
    assert result.ok is False
    assert "kaboom" in result.detail


def test_safe_wrapper_passes_through_ok() -> None:
    result = ssv._safe("fine", lambda: ssv.CheckResult("fine", True, "all good"))
    assert result.ok is True


def test_failing_subcheck_makes_not_ready(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A single failing sub-check forces RESULT: NOT READY + nonzero exit."""
    # Make every check pass except no-junk, which we force to fail.
    monkeypatch.setattr(
        ssv, "check_no_junk", lambda t, c: ssv.CheckResult("no-junk", False, "forced fail")
    )
    monkeypatch.setattr(
        ssv, "check_no_secrets", lambda t, c: ssv.CheckResult("no-secrets", True, "ok")
    )
    monkeypatch.setattr(
        ssv, "check_leak_scan", lambda t, c: ssv.CheckResult("leak-scan", True, "ok")
    )
    monkeypatch.setattr(
        ssv, "check_security", lambda t, c: ssv.CheckResult("security", True, "ok", skipped=True)
    )
    monkeypatch.setattr(
        ssv, "check_semgrep", lambda t, c: ssv.CheckResult("semgrep", True, "ok", skipped=True)
    )
    monkeypatch.setattr(ssv, "check_compiles", lambda t, c: ssv.CheckResult("compiles", True, "ok"))
    monkeypatch.setattr(
        ssv,
        "check_lint_format_types",
        lambda t, c: ssv.CheckResult("lint+format+types", True, "ok", skipped=True),
    )
    monkeypatch.setattr(
        ssv,
        "check_tests_coverage",
        lambda t, c: ssv.CheckResult("tests+coverage", True, "ok", skipped=True),
    )
    monkeypatch.setattr(
        ssv,
        "check_buildable",
        lambda t, c, o: (ssv.CheckResult("buildable", True, "ok", skipped=True), None),
    )
    monkeypatch.setattr(
        ssv,
        "check_cli_effect_smoke",
        lambda t, c, w: ssv.CheckResult("cli+effect-smoke", True, "ok", skipped=True),
    )

    rc = ssv.run_all(REPO_ROOT)
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: NOT READY" in out
    assert "RESULT: READY" not in out


def test_exception_in_check_never_yields_false_ready(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """If a check raises, the run must end NOT READY, not READY."""

    def raising(_t, _c):
        raise ValueError("boom in check")

    monkeypatch.setattr(ssv, "check_no_junk", raising)
    # Everything else trivially passes (skipped).
    for name, attr in [
        ("check_no_secrets", "no-secrets"),
        ("check_leak_scan", "leak-scan"),
        ("check_security", "security"),
        ("check_semgrep", "semgrep"),
        ("check_compiles", "compiles"),
        ("check_lint_format_types", "lint+format+types"),
        ("check_tests_coverage", "tests+coverage"),
    ]:
        monkeypatch.setattr(
            ssv, name, lambda t, c, _a=attr: ssv.CheckResult(_a, True, "ok", skipped=True)
        )
    monkeypatch.setattr(
        ssv,
        "check_buildable",
        lambda t, c, o: (ssv.CheckResult("buildable", True, "ok", skipped=True), None),
    )
    monkeypatch.setattr(
        ssv,
        "check_cli_effect_smoke",
        lambda t, c, w: ssv.CheckResult("cli+effect-smoke", True, "ok", skipped=True),
    )

    rc = ssv.run_all(REPO_ROOT)
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: NOT READY" in out


# ---------------------------------------------------------------------------
# no-junk
# ---------------------------------------------------------------------------


def test_no_junk_passes_on_clean_tmp(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_no_junk(tmp_path, contract)
    assert result.ok is True


def test_no_junk_fails_on_dotenv_and_dsstore(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".DS_Store").write_text("junk\n")
    (tmp_path / "__pycache__").mkdir()
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_no_junk(tmp_path, contract)
    assert result.ok is False
    assert ".env" in result.detail or "__pycache__" in result.detail or ".DS_Store" in result.detail


def test_no_junk_fails_on_coverage_artifact(tmp_path: Path) -> None:
    (tmp_path / ".coverage").write_text("\n")
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_no_junk(tmp_path, contract)
    assert result.ok is False


# ---------------------------------------------------------------------------
# leak-scan (parity with the Makefile leak-scan target)
# ---------------------------------------------------------------------------


def test_leak_scan_passes_on_clean_tmp(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n# a perfectly innocent module\n")
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_leak_scan(tmp_path, contract)
    assert result.ok is True


# Trigger strings are assembled at runtime, NOT written as source literals — a
# literal here would make THIS tracked test file trip the very leak-scan it
# exercises (the audit's self-trip trap). Same reason the regex lives only in
# the script, not duplicated as a literal in tests.
_LOCAL_PATH_LEAK = "/" + "Users" + "/who/project/x"  # matches /Users/[A-Za-z]
_PROVIDER_TOKEN_LEAK = "ghp" + "_" + ("a" * 36)  # matches ghp_[A-Za-z0-9]{30,}
# LibraxisAI is an OpenAI-compatible gateway (/v1/responses), so a hardcoded
# LibraxisAI key takes the `sk-` form covered by LEAK_CONTENT_RE.
_OPENAI_COMPAT_KEY_LEAK = "sk" + "-" + ("b" * 40)  # matches sk-[A-Za-z0-9_-]{20,}
# AWS access-key shape (AKIA + 16 upper/digits), split so the literal never
# appears contiguously in this source file — otherwise detect-secrets would
# flag the test itself. Not a real key.
_AWS_KEY_LEAK = "AKIA" + "Z7XVW3RTLK4NQ2PB"


def test_leak_scan_fails_on_local_user_path(tmp_path: Path) -> None:
    (tmp_path / "leaky.py").write_text(f'PATH = "{_LOCAL_PATH_LEAK}"\n')
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_leak_scan(tmp_path, contract)
    assert result.ok is False
    assert "secret/token/local-path" in result.detail


def test_leak_scan_fails_on_provider_token(tmp_path: Path) -> None:
    # Synthetic, obviously-fake token shaped like a provider key class.
    (tmp_path / "cfg.py").write_text(f'KEY = "{_PROVIDER_TOKEN_LEAK}"\n')
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_leak_scan(tmp_path, contract)
    assert result.ok is False
    assert "secret/token/local-path" in result.detail


def test_leak_scan_catches_openai_compatible_key(tmp_path: Path) -> None:
    """A LibraxisAI/OpenAI-compatible `sk-` key trips the content leak-scan.

    LibraxisAI (the product's default provider) serves the OpenAI-compatible
    Responses API, so its keys take the `sk-` form already covered by
    LEAK_CONTENT_RE — no provider-specific prefix regex is needed for that
    shape. detect-secrets (`.secrets.baseline`, entropy-based) remains the
    authoritative layer for any other provider-specific key format.
    """
    (tmp_path / "cfg.py").write_text(f'KEY = "{_OPENAI_COMPAT_KEY_LEAK}"\n')
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_leak_scan(tmp_path, contract)
    assert result.ok is False
    assert "secret/token/local-path" in result.detail


def test_leak_content_re_layering_is_documented() -> None:
    """The LibraxisAI key-format layering decision is documented in the script.

    P3-9 (less-risky choice): rather than fabricate an unverified LibraxisAI
    prefix regex, the script documents that detect-secrets is the authoritative
    layer for provider-specific formats and LEAK_CONTENT_RE covers the generic
    `sk-` shape the gateway shares. This guards that note from silent removal.
    """
    src = SS_VERIFY_PATH.read_text(encoding="utf-8")
    assert "detect-secrets" in src
    assert "LibraxisAI" in src


def test_leak_scan_fails_on_forbidden_artifact(tmp_path: Path) -> None:
    (tmp_path / "capture.mp4").write_bytes(b"\x00\x00fake media\x00")
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_leak_scan(tmp_path, contract)
    assert result.ok is False
    assert "forbidden artifact/media" in result.detail


def test_leak_scan_skips_binary_content(tmp_path: Path) -> None:
    # A binary file whose bytes happen to contain a local path must not trip the
    # CONTENT scan (grep -I parity: binary files are skipped for content).
    (tmp_path / "blob.bin").write_bytes(b"\x00" + _LOCAL_PATH_LEAK.encode() + b"\x00")
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_leak_scan(tmp_path, contract)
    assert result.ok is True


def test_leak_scan_failure_makes_not_ready(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A leak-scan failure alone forces RESULT: NOT READY (fail-closed)."""
    monkeypatch.setattr(ssv, "check_no_junk", lambda t, c: ssv.CheckResult("no-junk", True, "ok"))
    monkeypatch.setattr(
        ssv, "check_no_secrets", lambda t, c: ssv.CheckResult("no-secrets", True, "ok")
    )
    monkeypatch.setattr(
        ssv,
        "check_leak_scan",
        lambda t, c: ssv.CheckResult("leak-scan", False, "secret/token/local-path class in: x.py"),
    )
    monkeypatch.setattr(ssv, "check_compiles", lambda t, c: ssv.CheckResult("compiles", True, "ok"))
    monkeypatch.setattr(
        ssv,
        "check_lint_format_types",
        lambda t, c: ssv.CheckResult("lint+format+types", True, "ok", skipped=True),
    )
    monkeypatch.setattr(
        ssv, "check_security", lambda t, c: ssv.CheckResult("security", True, "ok", skipped=True)
    )
    monkeypatch.setattr(
        ssv,
        "check_tests_coverage",
        lambda t, c: ssv.CheckResult("tests+coverage", True, "ok", skipped=True),
    )
    monkeypatch.setattr(
        ssv,
        "check_buildable",
        lambda t, c, o: (ssv.CheckResult("buildable", True, "ok", skipped=True), None),
    )
    monkeypatch.setattr(
        ssv,
        "check_cli_effect_smoke",
        lambda t, c, w: ssv.CheckResult("cli+effect-smoke", True, "ok", skipped=True),
    )

    rc = ssv.run_all(REPO_ROOT)
    out = capsys.readouterr().out
    assert rc == 1
    assert "RESULT: NOT READY" in out
    assert "RESULT: READY" not in out


# ---------------------------------------------------------------------------
# security (bandit parity)
# ---------------------------------------------------------------------------


def test_security_skips_without_pyproject(tmp_path: Path) -> None:
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_security(tmp_path, contract)
    assert result.ok is True and result.skipped is True


def test_security_reports_bandit_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A nonzero bandit run FAILS the security check (fail-closed)."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "screenscribe").mkdir()

    class _CP:
        returncode = 1
        stdout = ">> Issue: [B602] subprocess with shell=True\n"
        stderr = ""

    monkeypatch.setattr(ssv, "_run", lambda *a, **k: _CP())
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_security(tmp_path, contract)
    assert result.ok is False


# ---------------------------------------------------------------------------
# semgrep (parity with the .pre-commit-config.yaml semgrep hook)
# ---------------------------------------------------------------------------


def test_semgrep_skips_without_ruleset(tmp_path: Path) -> None:
    """No semgrep.yml in the target → explicit SKIP (never silent, never fail)."""
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_semgrep(tmp_path, contract)
    assert result.ok is True and result.skipped is True


def test_semgrep_reports_findings_as_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A nonzero semgrep run (findings via --error) FAILS the check (fail-closed)."""
    (tmp_path / "semgrep.yml").write_text("rules: []\n")

    class _CP:
        returncode = 1
        stdout = "review_app.js:12: js-no-eval: eval() is forbidden\n"
        stderr = ""

    monkeypatch.setattr(ssv, "_run", lambda *a, **k: _CP())
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_semgrep(tmp_path, contract)
    assert result.ok is False
    assert result.skipped is False


def test_semgrep_clean_run_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A clean semgrep run (exit 0) PASSES the check (not skipped)."""
    (tmp_path / "semgrep.yml").write_text("rules: []\n")

    class _CP:
        returncode = 0
        stdout = "Ran 9 rules on 64 files: 0 findings.\n"
        stderr = ""

    monkeypatch.setattr(ssv, "_run", lambda *a, **k: _CP())
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_semgrep(tmp_path, contract)
    assert result.ok is True and result.skipped is False


def test_semgrep_missing_toolchain_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ruleset declared but uv/semgrep unavailable → FAIL, never a silent skip.

    semgrep is a declared dev dependency, so a target that ships semgrep.yml MUST
    be able to run it. A missing toolchain means the whole security ruleset goes
    unenforced — fail-closed instead of reporting READY (the old no-op gate bug).
    """
    (tmp_path / "semgrep.yml").write_text("rules: []\n")

    def _boom(*a, **k):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(ssv, "_run", _boom)
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_semgrep(tmp_path, contract)
    assert result.ok is False and result.skipped is False


def test_semgrep_uninstalled_binary_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uv resolves but semgrep is not installed → FAIL (ruleset unenforced)."""
    (tmp_path / "semgrep.yml").write_text("rules: []\n")

    class _CP:
        returncode = 2
        stdout = ""
        stderr = "error: Failed to spawn: `semgrep`\n  Caused by: No such file or directory\n"

    monkeypatch.setattr(ssv, "_run", lambda *a, **k: _CP())
    contract = ssv.load_contract(tmp_path)
    result = ssv.check_semgrep(tmp_path, contract)
    assert result.ok is False and result.skipped is False


# ---------------------------------------------------------------------------
# contract loading
# ---------------------------------------------------------------------------


def test_contract_defaults_when_absent(tmp_path: Path) -> None:
    c = ssv.load_contract(tmp_path)
    assert c.source == "default"
    assert c.tests == ssv.DEFAULT_TESTS
    assert c.coverage_floor == ssv.DEFAULT_COVERAGE_FLOOR


def test_contract_reads_yaml(tmp_path: Path) -> None:
    (tmp_path / ".screenscribe-verify.yml").write_text(
        'tests: "not integration"\ncoverage_floor: 73\nruntime_command: null\n'
        "critical_assets:\n  - review-render\n  - second-asset\n"
        "secrets_exclude:\n  - vendor/dir\n"
    )
    c = ssv.load_contract(tmp_path)
    assert c.source == "config"
    assert c.tests == "not integration"
    assert c.coverage_floor == 73
    assert c.runtime_command is None
    assert c.critical_assets == ["review-render", "second-asset"]
    assert c.secrets_exclude == ["vendor/dir"]


def test_repo_contract_matches_real_values() -> None:
    c = ssv.load_contract(REPO_ROOT)
    assert c.source == "config"
    assert c.tests == "not integration"
    assert c.coverage_floor == 80
    assert "screenscribe/html_pro_assets/vendor" in c.secrets_exclude
    # site/demo is deliberately NOT excluded from the secrets scan: a real
    # secret pushed into the generated demo report must be caught (fail-closed).
    assert not any(e.strip("/") == "site/demo" for e in c.secrets_exclude), (
        "site/demo must NOT be blanket-excluded from the no-secrets scan"
    )


# ---------------------------------------------------------------------------
# no-secrets scan narrowing: site/demo is scanned (fail-closed), vendor stays
# excluded. The planted-secret proof is the point of the narrowing — without
# it the change would be a claim, not a fact.
# ---------------------------------------------------------------------------


def _narrowed_contract() -> ssv.Contract:
    """Contract mirroring the repo after the site/demo exclude was dropped."""
    return ssv.Contract(
        tests="not integration",
        coverage_floor=80,
        runtime_command=None,
        critical_assets=[],
        secrets_exclude=["screenscribe/html_pro_assets/vendor"],
        source="config",
    )


def test_no_secrets_routes_site_demo_into_scan_after_narrowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With site/demo removed from the exclude list, the generated demo report
    reaches the detect-secrets scanner; the vendored bundle stays excluded."""
    (tmp_path / ".secrets.baseline").write_text("{}\n")
    demo = tmp_path / "site" / "demo"
    demo.mkdir(parents=True)
    (demo / "report.html").write_text("<html>ok</html>\n")
    vendor = tmp_path / "screenscribe" / "html_pro_assets" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "jszip.min.js").write_text("var a=1;\n")
    (tmp_path / "app.py").write_text("x = 1\n")

    captured: list[list[str]] = []
    real_run = ssv._run

    def fake_run(cmd, cwd=None, env=None, timeout=900):
        # Intercept only the detect-secrets-hook invocation; let real git run.
        if len(cmd) >= 3 and cmd[:3] == ["uv", "run", "detect-secrets-hook"]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(ssv, "_run", fake_run)

    res = ssv.check_no_secrets(tmp_path, _narrowed_contract())
    assert res.ok is True  # fake hook returns clean
    assert captured, "detect-secrets-hook was never invoked"
    scanned = [f.replace(os.sep, "/") for f in captured[0]]
    assert "site/demo/report.html" in scanned, scanned
    assert not any("html_pro_assets/vendor" in f for f in scanned), scanned


def test_no_secrets_keeps_vendor_excluded_when_still_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exclude mechanism still drops a listed path (guards vendor)."""
    (tmp_path / ".secrets.baseline").write_text("{}\n")
    vendor = tmp_path / "screenscribe" / "html_pro_assets" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "jszip.min.js").write_text("var a=1;\n")
    (tmp_path / "app.py").write_text("x = 1\n")

    captured: list[list[str]] = []
    real_run = ssv._run

    def fake_run(cmd, cwd=None, env=None, timeout=900):
        if len(cmd) >= 3 and cmd[:3] == ["uv", "run", "detect-secrets-hook"]:
            captured.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, cwd=cwd, env=env, timeout=timeout)

    monkeypatch.setattr(ssv, "_run", fake_run)

    res = ssv.check_no_secrets(tmp_path, _narrowed_contract())
    assert res.ok is True
    assert captured, "detect-secrets-hook was never invoked"
    scanned = [f.replace(os.sep, "/") for f in captured[0]]
    assert not any("html_pro_assets/vendor" in f for f in scanned), scanned
    assert "app.py" in scanned


def test_detect_secrets_flags_planted_key_in_demo_file(tmp_path: Path) -> None:
    """The engine that now sees site/demo catches a real secret shape: an AWS
    access key planted into a demo report is flagged (fail-closed proof)."""
    from detect_secrets.core.scan import scan_line
    from detect_secrets.settings import transient_settings

    demo = tmp_path / "site" / "demo"
    demo.mkdir(parents=True)
    (demo / "report.html").write_text(f"<html><body>{_AWS_KEY_LEAK}</body></html>\n")

    with transient_settings({"plugins_used": [{"name": "AWSKeyDetector"}]}):
        text = (demo / "report.html").read_text()
        found = {s.type for line in text.splitlines() for s in scan_line(line)}
    assert "AWS Access Key" in found, found


@pytest.mark.integration
@pytest.mark.slow
def test_no_secrets_end_to_end_catches_demo_secret(tmp_path: Path) -> None:
    """Full path through check_no_secrets: a real secret in a git-tracked
    site/demo file makes the no-secrets check FAIL, now that site/demo is not
    excluded. Integration-marked: needs the uv-resolvable detect-secrets-hook."""
    # A git repo so check_no_secrets takes its git-tracked branch (the real
    # path); `git ls-files` sees staged files, so no commit is needed. `git` is
    # a trusted system tool here (S607 partial-path is the intended test setup).
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)  # noqa: S607
    (tmp_path / ".secrets.baseline").write_text(
        (REPO_ROOT / ".secrets.baseline").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    demo = tmp_path / "site" / "demo"
    demo.mkdir(parents=True)
    (demo / "report.html").write_text(f"<html><body>{_AWS_KEY_LEAK}</body></html>\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)  # noqa: S607

    res = ssv.check_no_secrets(tmp_path, _narrowed_contract())
    assert res.ok is False, res.detail


# ---------------------------------------------------------------------------
# yaml mini-parser
# ---------------------------------------------------------------------------


def test_simple_yaml_strips_inline_comments() -> None:
    parsed = ssv._parse_simple_yaml("coverage_floor: 80  # floor\ntests: x\n")
    assert parsed["coverage_floor"] == 80
    assert parsed["tests"] == "x"


# ---------------------------------------------------------------------------
# heuristic skips are explicit (never silent)
# ---------------------------------------------------------------------------


def test_project_gates_skip_without_pyproject_are_explicit(tmp_path: Path) -> None:
    contract = ssv.load_contract(tmp_path)
    lf = ssv.check_lint_format_types(tmp_path, contract)
    assert lf.ok is True and lf.skipped is True
    tc = ssv.check_tests_coverage(tmp_path, contract)
    assert tc.ok is True and tc.skipped is True


# ---------------------------------------------------------------------------
# full end-to-end READY on the healthy repo (heavy: build + isolated install).
# Marked integration so it stays out of the fast `-m "not integration"` gate
# but is provable on demand: `pytest -m integration --run-integration`.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_healthy_repo_is_ready(capsys) -> None:
    rc = ssv.run_all(REPO_ROOT)
    out = capsys.readouterr().out
    assert "RESULT: READY" in out, out
    assert rc == 0


@pytest.mark.integration
@pytest.mark.slow
def test_tmp_tree_with_junk_is_not_ready(tmp_path: Path, capsys) -> None:
    # A non-git folder with junk and no pyproject: no-junk fails -> NOT READY.
    (tmp_path / ".env").write_text("SECRET=1\n")
    os.makedirs(tmp_path / "__pycache__", exist_ok=True)
    rc = ssv.run_all(tmp_path)
    out = capsys.readouterr().out
    assert "RESULT: NOT READY" in out, out
    assert rc == 1
