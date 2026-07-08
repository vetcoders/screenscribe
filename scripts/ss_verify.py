#!/usr/bin/env python3
"""ss-verify — one portable "READY / NOT READY" verifier.

Entry: ``python scripts/ss_verify.py [PATH=.]``

Take a folder of code, run a sequence of checks against it, print per-check
``✔/✘`` lines, then ``RESULT: READY`` (exit 0) or ``RESULT: NOT READY``
(exit 1).

Design principles
-----------------
* **Standalone & portable.** Pure Python stdlib (cross-platform: macOS, Linux,
  Windows, CI, Docker). NOT a ``screenscribe`` subcommand — ss-verify must run
  on ANY folder / ZIP-extract, including one where the package is not installed.
  It orchestrates ``uv`` / ``python`` against the *target* folder via subprocess.
* **Fail-closed.** Any check that fails → ``NOT READY``. An *unexpected
  exception* inside a check makes that check FAIL (it is never silently
  swallowed into a pass). There is no path where a broken check reports READY.
* **Effect over substrate.** The runtime check asserts the product *behaves*
  (renders a self-contained REVIEW report with an embedded image), not that a
  file exists.
* **Contract declared by the repo.** Optional ``.screenscribe-verify.yml`` in
  the target declares ``{tests, coverage_floor, runtime_command,
  critical_assets}``. The script reads it when present; otherwise it uses
  sensible defaults + heuristics. Nothing project-specific is hardcoded as a
  *requirement* in this script.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults / heuristics (used only when .screenscribe-verify.yml is absent)
# ---------------------------------------------------------------------------

DEFAULT_TESTS = "not integration"
DEFAULT_COVERAGE_FLOOR = 80
# A no-op runtime contract: the cli+effect-smoke check has its own built-in
# REVIEW render contract; `runtime_command` is reserved for future surfaces.
DEFAULT_PACKAGE = "screenscribe"

# ---------------------------------------------------------------------------
# Branding guard (repo-wide).
# ---------------------------------------------------------------------------
# The canonical public spelling is the lowercase `screenscribe` package /
# `vetcoders` org. CamelCase `ScreenScribe` / `VetCoders` in any tracked text
# file is a brand regression. The guard scans the WHOLE git-tracked tree so a
# bare brand in a NEW file (a doc/module added after any hand-maintained list)
# cannot slip through — the bounded-list blind spot is gone.
#
# A few tracked paths legitimately NAME the brand they forbid or are non-prose
# churn/vendored, so they are excluded (prefix match on POSIX paths):
BRANDING_SCAN_EXCLUDES = (
    "scripts/ss_verify.py",  # this guard's own source declares the forbidden tokens
    "tests/test_branding_guard.py",  # the guard's tests use the brand as fixtures
    "Makefile",  # brand-scan help/comment text names the guard it drives
    "uv.lock",  # resolver lockfile (no public prose; churny)
    ".secrets.baseline",  # detect-secrets baseline (generated)
    "screenscribe/html_pro_assets/vendor",  # vendored minified third-party
    "site/demo",  # generated demo report (inlined JSZip + base64 assets)
)

# Allowlisted technical identifiers — these CONTAIN the camelCase brand but are
# code/protocol tokens, not public prose, so they must NOT fail. Each entry is
# masked out of the text before the bare-brand scan runs. Comments explain why.
BRANDING_ALLOWLIST = (
    "ScreenScribeConfig",  # config class (screenscribe.config.ScreenScribeConfig)
    "ScreenScribeLib",  # JS namespace (window.ScreenScribeLib)
    "ScreenScribePlayer",  # JS player class
    "X-ScreenScribe-Token",  # HTTP header / wire protocol name
    "SCREENSCRIBE_",  # env-var prefix (SCREENSCRIBE_API_KEY, ...)
)

# Bare brand tokens that constitute a regression once allowlisted identifiers
# are masked out. Maps brand -> suggested public spelling.
BRANDING_FORBIDDEN = {
    "ScreenScribe": "screenscribe (mid-sentence) / Screenscribe (line start)",
    "VetCoders": "vetcoders (mid-sentence) / Vetcoders (line start)",
}

JUNK_NAMES = {".env", ".DS_Store", "__pycache__", ".venv"}
# Tracked build/coverage artifacts that must not live in a shippable tree.
TRACKED_ARTIFACT_DIRS = ("dist/", "build/", "htmlcov/")
TRACKED_ARTIFACT_FILES = (".coverage",)

# leak-scan parity: secret/token/local-path content classes and forbidden
# artifact/media file types. Kept in lock-step with the `leak-scan` Makefile
# target so consolidation into ss-verify loses nothing. GENERIC risk classes
# only (private keys, provider tokens, local user paths) — never project names.
#
# Provider-specific key formats (P3-9): this product's default provider is
# LibraxisAI, which serves the OpenAI-compatible Responses API — so a hardcoded
# LibraxisAI key takes the `sk-` form already covered below. No verified
# LibraxisAI-specific prefix exists to encode here, and fabricating one would
# add false negatives/positives, so we deliberately do NOT invent a regex.
# The AUTHORITATIVE layer for any provider-specific key shape is detect-secrets
# (entropy-based, via `.secrets.baseline`, run by check_no_secrets); this regex
# is a generic-class belt-and-suspenders layer, not the sole secret defense.
LEAK_CONTENT_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|ghp_[A-Za-z0-9]{30,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|/Users/[A-Za-z]"
    r"|/home/[A-Za-z]"
)
# Forbidden artifact/media file extensions in a shippable tree (matches the
# Makefile leak-scan artifact/media list).
LEAK_FORBIDDEN_EXTS = (
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".log",
    ".pem",
    ".key",
)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

CHECK = "✔"  # ✔
CROSS = "✘"  # ✘


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


def _emit(result: CheckResult) -> None:
    mark = CHECK if result.ok else CROSS
    tail = f" — {result.detail}" if result.detail else ""
    if result.skipped:
        # A skip is reported explicitly (never silent) and counts as OK.
        print(f"{mark} {result.name} [SKIPPED]{tail}")
    else:
        print(f"{mark} {result.name}{tail}")


# ---------------------------------------------------------------------------
# Minimal YAML reader (no PyYAML dependency — the verifier is stdlib-only).
# Supports the flat subset we need: scalars and simple `- ` lists.
# ---------------------------------------------------------------------------


def _strip_inline_comment(value: str) -> str:
    # Only strip a comment that is not inside quotes.
    out: list[str] = []
    quote: str | None = None
    for ch in value:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).strip()


def _coerce_scalar(value: str) -> object:
    v = value.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _parse_simple_yaml(text: str) -> dict[str, object]:
    """Parse a flat key: value YAML with simple block lists. Stdlib-only.

    Intentionally small. The contract file is operator-authored and simple;
    we never need full YAML. Anything we cannot parse is ignored, falling back
    to defaults (fail-safe, not fail-mystery).
    """
    data: dict[str, object] = {}
    current_list_key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("- ") and current_list_key is not None and indent > 0:
            item = _coerce_scalar(_strip_inline_comment(stripped[2:]))
            lst = data.get(current_list_key)
            if isinstance(lst, list):
                lst.append(item)
            continue
        if ":" in stripped:
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = _strip_inline_comment(rest)
            if rest == "":
                # Could be a block list/mapping header; assume list.
                data[key] = []
                current_list_key = key
            else:
                data[key] = _coerce_scalar(rest)
                current_list_key = None
    return data


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@dataclass
class Contract:
    tests: str
    coverage_floor: int
    runtime_command: str | None
    critical_assets: list[str]
    # Paths the no-secrets scan excludes (e.g. vendored minified bundles whose
    # high-entropy strings are benign). Repo-declared, NOT hardcoded in the
    # script — keeps ss-verify portable while matching the repo's real contract.
    secrets_exclude: list[str]
    source: str  # "config" or "default"


def load_contract(target: Path) -> Contract:
    cfg_path = target / ".screenscribe-verify.yml"
    if cfg_path.is_file():
        try:
            parsed = _parse_simple_yaml(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:  # broad: fall back loudly, not silently
            print(f"  (warning: could not parse .screenscribe-verify.yml: {exc}; using defaults)")
            parsed = {}
        tests = parsed.get("tests")
        floor = parsed.get("coverage_floor")
        runtime = parsed.get("runtime_command")
        assets = parsed.get("critical_assets")
        excludes = parsed.get("secrets_exclude")
        return Contract(
            tests=str(tests) if isinstance(tests, str) and tests else DEFAULT_TESTS,
            coverage_floor=int(floor) if isinstance(floor, int) else DEFAULT_COVERAGE_FLOOR,
            runtime_command=str(runtime) if isinstance(runtime, str) and runtime else None,
            critical_assets=[str(a) for a in assets] if isinstance(assets, list) else [],
            secrets_exclude=[str(e) for e in excludes] if isinstance(excludes, list) else [],
            source="config",
        )
    return Contract(
        tests=DEFAULT_TESTS,
        coverage_floor=DEFAULT_COVERAGE_FLOOR,
        runtime_command=None,
        critical_assets=[],
        secrets_exclude=[],
        source="default",
    )


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_git_repo(target: Path) -> bool:
    try:
        cp = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=target, timeout=30)
    except Exception:  # broad: any failure means "not a git repo here"
        return False
    return cp.returncode == 0 and cp.stdout.strip() == "true"


def _git_tracked_files(target: Path) -> list[str]:
    cp = _run(["git", "ls-files"], cwd=target, timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {cp.stderr.strip()}")
    return [line for line in cp.stdout.splitlines() if line]


def _has_pyproject(target: Path) -> bool:
    return (target / "pyproject.toml").is_file()


# ---------------------------------------------------------------------------
# Checks. Each returns CheckResult. Each is wrapped so an unexpected exception
# becomes a FAIL (fail-closed), never a silent pass.
# ---------------------------------------------------------------------------


def check_no_junk(target: Path, contract: Contract) -> CheckResult:
    offenders: list[str] = []
    if _is_git_repo(target):
        tracked = _git_tracked_files(target)
        for rel in tracked:
            parts = Path(rel).parts
            base = parts[-1] if parts else rel
            if base in JUNK_NAMES or any(p in JUNK_NAMES for p in parts):
                offenders.append(rel)
            elif rel.endswith("/.DS_Store") or base == ".DS_Store":
                offenders.append(rel)
            elif any(rel == d.rstrip("/") or rel.startswith(d) for d in TRACKED_ARTIFACT_DIRS):
                offenders.append(rel)
            elif base in TRACKED_ARTIFACT_FILES:
                offenders.append(rel)
    else:
        for root, dirs, files in os.walk(target):
            rel_root = os.path.relpath(root, target)
            for name in list(dirs) + files:
                if name in JUNK_NAMES:
                    offenders.append(os.path.normpath(os.path.join(rel_root, name)))
            for f in files:
                if f in TRACKED_ARTIFACT_FILES:
                    offenders.append(os.path.normpath(os.path.join(rel_root, f)))
    offenders = sorted(set(offenders))
    if offenders:
        return CheckResult("no-junk", False, f"found {len(offenders)}: {', '.join(offenders[:8])}")
    return CheckResult("no-junk", True, "no .env/.DS_Store/__pycache__/.venv/tracked artifacts")


def check_no_secrets(target: Path, contract: Contract) -> CheckResult:
    baseline = target / ".secrets.baseline"
    if not baseline.is_file():
        return CheckResult(
            "no-secrets", True, "no .secrets.baseline present; skipped", skipped=True
        )
    if not _is_git_repo(target):
        # detect-secrets-hook needs an explicit file list; without git we walk.
        files = []
        for root, dirs, fnames in os.walk(target):
            dirs[:] = [d for d in dirs if d not in JUNK_NAMES and d != ".git"]
            for f in fnames:
                files.append(os.path.relpath(os.path.join(root, f), target))
    else:
        files = _git_tracked_files(target)

    # Apply repo-declared excludes (e.g. vendored minified bundles). Prefix
    # match on normalized POSIX paths so this matches the Makefile pathspec.
    if contract.secrets_exclude:
        excl = tuple(e.strip("/") for e in contract.secrets_exclude if e.strip())
        files = [f for f in files if not f.replace(os.sep, "/").startswith(excl)]

    if not files:
        return CheckResult("no-secrets", True, "no files to scan", skipped=True)
    cmd = [
        "uv",
        "run",
        "detect-secrets-hook",
        "--baseline",
        ".secrets.baseline",
        *files,
    ]
    cp = _run(cmd, cwd=target, timeout=600)
    if cp.returncode == 0:
        return CheckResult("no-secrets", True, "no new secrets vs baseline")
    return CheckResult(
        "no-secrets",
        False,
        (cp.stdout + cp.stderr).strip().splitlines()[-1:][0]
        if (cp.stdout + cp.stderr).strip()
        else "detect-secrets reported new secrets",
    )


def _iter_scan_files(target: Path) -> list[str]:
    """Files to scan for leaks: tracked files in a git repo, else a tree walk."""
    if _is_git_repo(target):
        return _git_tracked_files(target)
    files: list[str] = []
    for root, dirs, fnames in os.walk(target):
        dirs[:] = [d for d in dirs if d not in JUNK_NAMES and d != ".git"]
        for f in fnames:
            files.append(os.path.relpath(os.path.join(root, f), target))
    return files


def check_leak_scan(target: Path, contract: Contract) -> CheckResult:
    """Parity with the Makefile ``leak-scan`` target.

    Scans the shippable tree for (1) secret/token/local-path content classes
    and (2) forbidden artifact/media file types. Generic risk classes only —
    no project-private names live in this script.
    """
    files = _iter_scan_files(target)
    if not files:
        return CheckResult("leak-scan", True, "no files to scan", skipped=True)

    content_hits: list[str] = []
    artifact_hits: list[str] = []
    for rel in files:
        if rel.lower().endswith(LEAK_FORBIDDEN_EXTS):
            artifact_hits.append(rel)
        path = target / rel
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in blob:  # skip binary files (grep -I parity)
            continue
        text = blob.decode("utf-8", errors="ignore")
        if LEAK_CONTENT_RE.search(text):
            content_hits.append(rel)

    problems: list[str] = []
    if content_hits:
        head = ", ".join(sorted(set(content_hits))[:8])
        problems.append(f"secret/token/local-path class in: {head}")
    if artifact_hits:
        head = ", ".join(sorted(set(artifact_hits))[:8])
        problems.append(f"forbidden artifact/media tracked: {head}")
    if problems:
        return CheckResult("leak-scan", False, " | ".join(problems))
    return CheckResult(
        "leak-scan", True, "no secret/token/local-path classes, no forbidden artifacts"
    )


def check_security(target: Path, contract: Contract) -> CheckResult:
    """Parity with the Makefile ``security`` target (bandit static analysis).

    Runs ``bandit -r screenscribe -c pyproject.toml`` via uv so the same
    severity/skip config the repo declares is honoured. Skips (explicitly,
    never silently) when there is no pyproject or no package to scan.
    """
    if not _has_pyproject(target):
        return CheckResult(
            "security", True, "no pyproject.toml; bandit skipped (heuristic)", skipped=True
        )
    pkg = target / DEFAULT_PACKAGE
    scan_target = DEFAULT_PACKAGE if pkg.is_dir() else "."
    cp = _run(
        ["uv", "run", "bandit", "-r", scan_target, "-c", "pyproject.toml"],
        cwd=target,
        timeout=600,
    )
    if cp.returncode == 0:
        return CheckResult("security", True, f"bandit clean ({scan_target})")
    tail = (cp.stdout + cp.stderr).strip().splitlines()
    return CheckResult("security", False, tail[-1] if tail else "bandit reported issues")


# Semgrep ruleset + invocation kept in lock-step with .pre-commit-config.yaml so
# this gate flags exactly what a passing pre-commit flags (no more, no less):
#   args: [--config, semgrep.yml, --error, --metrics=off]
#   exclude: ^screenscribe/html_pro_assets/vendor/
# When the ruleset file or the semgrep binary is absent, the check SKIPS
# explicitly (never silently) — same posture as the other optional checks.
SEMGREP_CONFIG = "semgrep.yml"
SEMGREP_EXCLUDE = "screenscribe/html_pro_assets/vendor"


def check_semgrep(target: Path, contract: Contract) -> CheckResult:
    """Parity with the ``.pre-commit-config.yaml`` semgrep hook.

    Runs ``semgrep --config semgrep.yml --error --metrics=off`` (excluding the
    vendored minified bundle) via ``uv run`` so the SAME ruleset the pre-commit
    hook enforces is enforced at the gate — closing the gap where ``make
    verify``/CI reported READY while semgrep lived only in opt-in pre-commit.

    FAIL-CLOSED. ``semgrep`` is a declared dev dependency (pyproject dev group),
    so once the target ships a ``semgrep.yml`` ruleset the scanner MUST resolve
    and run. If the ruleset is declared but the toolchain cannot run it (uv or
    semgrep unavailable), that is a broken security gate — reported as a FAIL,
    never a silent skip that lets the whole ruleset go unenforced while the run
    reports READY. A nonzero semgrep run (``--error`` → findings) also FAILS.

    The ONLY skip is portability: a target with no ``semgrep.yml`` at all does
    not declare a ruleset, so there is nothing to enforce (heuristic skip).
    """
    ruleset = target / SEMGREP_CONFIG
    if not ruleset.is_file():
        return CheckResult(
            "semgrep", True, f"no {SEMGREP_CONFIG}; semgrep skipped (heuristic)", skipped=True
        )
    cmd = [
        "uv",
        "run",
        "semgrep",
        "--config",
        SEMGREP_CONFIG,
        "--error",
        "--metrics=off",
        "--exclude",
        SEMGREP_EXCLUDE,
    ]
    try:
        cp = _run(cmd, cwd=target, timeout=900)
    except FileNotFoundError:
        # `uv` (hence semgrep) missing while a ruleset IS declared: the security
        # gate cannot run. Fail-closed — a declared ruleset that cannot execute
        # must never report READY.
        return CheckResult(
            "semgrep",
            False,
            f"{SEMGREP_CONFIG} declared but semgrep/uv unavailable (gate cannot run)",
        )
    if cp.returncode == 0:
        return CheckResult("semgrep", True, f"semgrep clean ({SEMGREP_CONFIG})")
    combined = (cp.stdout + cp.stderr).strip()
    # `uv run` reporting that the semgrep tool itself is missing: with a declared
    # ruleset this is fail-closed, not a skip — otherwise the entire security
    # ruleset silently goes unenforced while the gate stays green.
    lowered = combined.lower()
    if ("no such" in lowered and "semgrep" in lowered) or "command not found" in lowered:
        return CheckResult(
            "semgrep",
            False,
            f"{SEMGREP_CONFIG} declared but semgrep binary unavailable (unenforced)",
        )
    tail = combined.splitlines()
    return CheckResult("semgrep", False, tail[-1] if tail else "semgrep reported findings")


def check_compiles(target: Path, contract: Contract) -> CheckResult:
    pkg = target / DEFAULT_PACKAGE
    compile_target = str(pkg) if pkg.is_dir() else str(target)
    cp = _run(["uv", "run", "python", "-m", "compileall", "-q", compile_target], cwd=target)
    if cp.returncode == 0:
        return CheckResult("compiles", True, f"compileall clean ({Path(compile_target).name})")
    msg = (cp.stdout + cp.stderr).strip().splitlines()
    return CheckResult("compiles", False, msg[-1] if msg else "compileall failed")


def check_lint_format_types(target: Path, contract: Contract) -> CheckResult:
    if not _has_pyproject(target):
        return CheckResult(
            "lint+format+types",
            True,
            "no pyproject.toml; project gates skipped (heuristic)",
            skipped=True,
        )
    steps = [
        ("ruff check", ["uv", "run", "ruff", "check", "screenscribe", "tests"]),
        (
            "ruff format --check",
            ["uv", "run", "ruff", "format", "--check", "screenscribe", "tests"],
        ),
        ("mypy", ["uv", "run", "mypy", "screenscribe"]),
    ]
    failures: list[str] = []
    for label, cmd in steps:
        cp = _run(cmd, cwd=target)
        if cp.returncode != 0:
            tail = (cp.stdout + cp.stderr).strip().splitlines()
            failures.append(f"{label}: {tail[-1] if tail else 'failed'}")
    if failures:
        return CheckResult("lint+format+types", False, " | ".join(failures))
    return CheckResult("lint+format+types", True, "ruff check + format-check + mypy clean")


def check_tests_coverage(target: Path, contract: Contract) -> CheckResult:
    # Coverage scope is DELIBERATELY Python-only (``--cov=screenscribe``): the
    # ``%`` floor measures the Python package, not the served JavaScript. This is
    # a declared scope, not a silent gap (P2-11). The JS runtime surface is
    # covered separately by the F0 node-vm canaries in
    # ``tests/test_f0_js_runtime_smoke.py`` — review_app.js (deep) plus a fast
    # load-array over video_player.js + analyze_dashboard.js + vendored JSZip —
    # which this same pytest run executes and which fail-close under CI when node
    # is absent. A line-coverage instrument over the JS (nyc/istanbul) is
    # explicitly out of scope; the canary is the runtime witness instead. See
    # docs/COVERAGE_SCOPE.md.
    if not _has_pyproject(target):
        return CheckResult(
            "tests+coverage",
            True,
            "no pyproject.toml; tests skipped (heuristic)",
            skipped=True,
        )
    cmd = [
        "uv",
        "run",
        "pytest",
        "-q",
        "-m",
        contract.tests,
        "--cov=screenscribe",
        "--cov-report=term-missing",
        f"--cov-fail-under={contract.coverage_floor}",
    ]
    cp = _run(cmd, cwd=target, timeout=1200)
    if cp.returncode == 0:
        return CheckResult(
            "tests+coverage",
            True,
            f"pytest -m '{contract.tests}' passed, coverage >= {contract.coverage_floor}%",
        )
    lines = (cp.stdout + cp.stderr).strip().splitlines()
    summary = lines[-1] if lines else "pytest/coverage failed"
    # Surface the failing test node-ids (pytest "short test summary info") so a
    # flake or regression is diagnosable straight from the gate output, instead of
    # being hidden behind the one-line count. Without this, a CI-only failure
    # forces a local repro just to learn the test name.
    failed = [ln for ln in lines if ln.startswith("FAILED ")]
    if failed:
        names = "; ".join(
            ln.split(" - ", 1)[0].removeprefix("FAILED ").strip() for ln in failed[:8]
        )
        more = "" if len(failed) <= 8 else f" (+{len(failed) - 8} more)"
        summary = f"{summary} -> {names}{more}"
    return CheckResult("tests+coverage", False, summary)


def check_buildable(
    target: Path, contract: Contract, out_dir: Path
) -> tuple[CheckResult, Path | None]:
    if not _has_pyproject(target):
        return (
            CheckResult("buildable", True, "no pyproject.toml; build skipped", skipped=True),
            None,
        )
    cp = _run(["uv", "build", "--out-dir", str(out_dir)], cwd=target, timeout=600)
    if cp.returncode != 0:
        tail = (cp.stdout + cp.stderr).strip().splitlines()
        return (CheckResult("buildable", False, tail[-1] if tail else "uv build failed"), None)
    wheels = sorted(out_dir.glob("*.whl"))
    sdists = sorted(out_dir.glob("*.tar.gz"))
    if not wheels or not sdists:
        return (
            CheckResult("buildable", False, "uv build produced no wheel and/or sdist"),
            None,
        )
    return (
        CheckResult("buildable", True, f"built {wheels[-1].name} + {sdists[-1].name}"),
        wheels[-1],
    )


# The render effect contract, executed inside the isolated wheel env. Inlined
# so the verifier is standalone and does not depend on the target shipping a
# separate helper file.
EFFECT_SMOKE_SOURCE = r"""
from __future__ import annotations
import base64, re, sys

def main() -> int:
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000154a24f600000000049454e44ae426082"
    )
    screenshot = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    findings = [{
        "id": 1, "category": "ui", "timestamp_formatted": "00:01", "timestamp": 1,
        "text": "ss-verify fixture finding.", "screenshot": screenshot,
        "unified_analysis": {"severity": "high", "summary": "ss-verify effect fixture."},
    }]
    from screenscribe.html_pro.renderer import render_html_report_pro
    html = render_html_report_pro(
        video_name="ss-verify-smoke.mp4", video_path=None,
        generated_at="2026-06-14T00:00:00Z",
        executive_summary="ss-verify effect-level smoke render.",
        findings=findings, language="en",
    )
    if not isinstance(html, str) or not html.strip():
        print("FAIL: render produced empty/non-string HTML", file=sys.stderr); return 1
    if "data:image/" not in html:
        print("FAIL: no embedded data:image/ payload", file=sys.stderr); return 1
    if re.search(r"<script\b[^>]*\bsrc\s*=", html, flags=re.IGNORECASE):
        print("FAIL: external <script src=> reference", file=sys.stderr); return 1
    print(f"effect render OK: {len(html)} bytes, embedded data:image, no external script.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""


def check_cli_effect_smoke(target: Path, contract: Contract, wheel: Path | None) -> CheckResult:
    if wheel is None:
        if not _has_pyproject(target):
            return CheckResult(
                "cli+effect-smoke",
                True,
                "no pyproject.toml; no wheel to verify (heuristic)",
                skipped=True,
            )
        return CheckResult("cli+effect-smoke", False, "no wheel from build step to verify")

    # Isolated env OUTSIDE the source tree: a separate temp dir we cd into, with
    # `uv run --isolated --no-project --with <wheel>` so the wheel is the only
    # source of the package (editable/source installs cannot mask packaging gaps).
    iso = Path(tempfile.mkdtemp(prefix="ss-verify-iso-"))
    try:
        smoke = iso / "ss_effect_smoke.py"
        smoke.write_text(EFFECT_SMOKE_SOURCE, encoding="utf-8")
        wheel_arg = str(wheel)
        base = ["uv", "run", "--isolated", "--no-project", "--with", wheel_arg]

        # (a) --version + --help exit 0.
        cp_ver = _run([*base, "python", "-m", "screenscribe", "--version"], cwd=iso, timeout=600)
        if cp_ver.returncode != 0:
            tail = (cp_ver.stdout + cp_ver.stderr).strip().splitlines()
            return CheckResult(
                "cli+effect-smoke", False, f"--version failed: {tail[-1] if tail else ''}"
            )
        cp_help = _run([*base, "python", "-m", "screenscribe", "--help"], cwd=iso, timeout=600)
        if cp_help.returncode != 0:
            tail = (cp_help.stdout + cp_help.stderr).strip().splitlines()
            return CheckResult(
                "cli+effect-smoke", False, f"--help failed: {tail[-1] if tail else ''}"
            )

        # (b) EFFECT: render a REVIEW report and assert self-contained HTML with
        # an embedded base64 image and no external scripts / missing template.
        cp_eff = _run([*base, "python", "ss_effect_smoke.py"], cwd=iso, timeout=600)
        if cp_eff.returncode != 0:
            out = (cp_eff.stdout + cp_eff.stderr).strip().splitlines()
            return CheckResult(
                "cli+effect-smoke", False, f"effect render failed: {out[-1] if out else ''}"
            )
        return CheckResult(
            "cli+effect-smoke",
            True,
            "isolated wheel: --version/--help OK; REVIEW render self-contained w/ embedded image",
        )
    finally:
        shutil.rmtree(iso, ignore_errors=True)


# ---------------------------------------------------------------------------
# Branding scan helpers (importable + standalone via `make brand-scan`).
# ---------------------------------------------------------------------------


def scan_text_for_brand(text: str) -> list[tuple[int, str, str]]:
    """Return bare-brand hits in ``text`` as ``(line_no, brand, suggestion)``.

    Allowlisted technical identifiers (``ScreenScribeConfig``,
    ``X-ScreenScribe-Token``, the ``SCREENSCRIBE_`` env prefix, …) are masked
    out first, so only a *bare* public-facing ``ScreenScribe`` / ``VetCoders``
    brand string remains as a regression. Deterministic, side-effect free —
    the unit tests drive this directly on temporary strings.
    """
    hits: list[tuple[int, str, str]] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        masked = raw
        for token in BRANDING_ALLOWLIST:
            masked = masked.replace(token, "\x00" * len(token))
        for brand, suggestion in BRANDING_FORBIDDEN.items():
            if brand in masked:
                hits.append((idx, brand, suggestion))
    return hits


def _branding_scan_files(target: Path) -> list[str]:
    """Every tracked file in the target, minus the branding-machinery excludes.

    Uses the same tracked-or-walk resolution as the leak scan, then drops paths
    that legitimately name the brand (the guard, its tests, Make help text) plus
    non-prose churn/vendored trees (``BRANDING_SCAN_EXCLUDES``). Binary files are
    NOT filtered here — the NUL-byte sniff in ``check_branding`` skips them at
    read time (``grep -I`` parity).
    """
    excludes = tuple(e.strip("/") for e in BRANDING_SCAN_EXCLUDES if e.strip())
    out: list[str] = []
    for rel in _iter_scan_files(target):
        posix = rel.replace(os.sep, "/")
        if posix.startswith(excludes):
            continue
        out.append(rel)
    return sorted(set(out))


def check_branding(target: Path, contract: Contract) -> CheckResult:
    """Fail on public-facing camelCase ``ScreenScribe`` / ``VetCoders`` brand.

    Scans the whole git-tracked tree (minus ``BRANDING_SCAN_EXCLUDES``), so a
    bare brand in ANY new file is caught, not just a hand-listed set. Binary
    files are skipped (NUL-byte sniff, ``grep -I`` parity). Allowlisted technical
    identifiers are exempt via ``scan_text_for_brand``.
    """
    offenders: list[str] = []
    for rel in _branding_scan_files(target):
        path = target / rel
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in blob:  # binary — skip (grep -I parity)
            continue
        text = blob.decode("utf-8", errors="ignore")
        for line_no, brand, suggestion in scan_text_for_brand(text):
            offenders.append(f"{rel}:{line_no}: bare '{brand}' -> use {suggestion}")
    if offenders:
        head = "; ".join(offenders[:8])
        more = f" (+{len(offenders) - 8} more)" if len(offenders) > 8 else ""
        return CheckResult("branding", False, f"{head}{more}")
    return CheckResult("branding", True, "no bare ScreenScribe/VetCoders brand in tracked tree")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _safe(name: str, fn) -> CheckResult:
    """Wrap a check so any unexpected exception becomes a FAIL (fail-closed)."""
    try:
        return fn()
    except Exception as exc:  # broad and intentional: never a silent pass
        return CheckResult(name, False, f"unexpected error: {type(exc).__name__}: {exc}")


def run_all(target: Path) -> int:
    target = target.resolve()
    if not target.is_dir():
        print(f"{CROSS} target {target} is not a directory")
        print("RESULT: NOT READY")
        return 1

    contract = load_contract(target)
    print(f"ss-verify target: {target}")
    print(
        f"contract: source={contract.source} tests='{contract.tests}' "
        f"coverage_floor={contract.coverage_floor}"
    )
    print("-" * 72)

    results: list[CheckResult] = []
    build_out = Path(tempfile.mkdtemp(prefix="ss-verify-dist-"))
    try:
        results.append(_safe("no-junk", lambda: check_no_junk(target, contract)))
        results.append(_safe("no-secrets", lambda: check_no_secrets(target, contract)))
        results.append(_safe("leak-scan", lambda: check_leak_scan(target, contract)))
        results.append(_safe("branding", lambda: check_branding(target, contract)))
        results.append(_safe("compiles", lambda: check_compiles(target, contract)))
        results.append(
            _safe("lint+format+types", lambda: check_lint_format_types(target, contract))
        )
        results.append(_safe("security", lambda: check_security(target, contract)))
        results.append(_safe("semgrep", lambda: check_semgrep(target, contract)))
        results.append(_safe("tests+coverage", lambda: check_tests_coverage(target, contract)))

        def _build() -> CheckResult:
            res, wheel = check_buildable(target, contract, build_out)
            _build.wheel = wheel  # type: ignore[attr-defined]
            return res

        _build.wheel = None  # type: ignore[attr-defined]
        results.append(_safe("buildable", _build))
        wheel = getattr(_build, "wheel", None)
        results.append(
            _safe("cli+effect-smoke", lambda: check_cli_effect_smoke(target, contract, wheel))
        )
    finally:
        shutil.rmtree(build_out, ignore_errors=True)

    print("-" * 72)
    for r in results:
        _emit(r)
    print("-" * 72)

    ready = all(r.ok for r in results)
    print("RESULT: READY" if ready else "RESULT: NOT READY")
    return 0 if ready else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ss_verify",
        description="Portable READY/NOT READY verifier for a screenscribe-shaped folder.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="target folder to verify (default: current directory)",
    )
    parser.add_argument(
        "--branding-only",
        action="store_true",
        help="run ONLY the branding guard (used by `make brand-scan`)",
    )
    args = parser.parse_args(argv)
    target = Path(args.path)
    if args.branding_only:
        contract = load_contract(target.resolve())
        result = _safe("branding", lambda: check_branding(target.resolve(), contract))
        _emit(result)
        print("RESULT: READY" if result.ok else "RESULT: NOT READY")
        return 0 if result.ok else 1
    return run_all(target)


if __name__ == "__main__":
    sys.exit(main())
