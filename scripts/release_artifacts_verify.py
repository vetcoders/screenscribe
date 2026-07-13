#!/usr/bin/env python3
"""Verify the exact wheel and sdist that a release workflow will publish."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


def expected_assets() -> set[str]:
    tracked = run("git", "ls-files", "screenscribe").stdout.splitlines()
    assets = {path for path in tracked if not path.endswith(".py")}
    if not assets:
        raise RuntimeError("tracked runtime asset manifest is empty")
    return assets


def archive_assets(wheel: Path, sdist: Path) -> tuple[set[str], set[str]]:
    with zipfile.ZipFile(wheel) as archive:
        wheel_assets = {
            name
            for name in archive.namelist()
            if name.startswith("screenscribe/") and not name.endswith(("/", ".py"))
        }
    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_assets = {
            "/".join(name.split("/")[1:])
            for name in archive.getnames()
            if "/screenscribe/" in name and not name.endswith(("/", ".py"))
        }
    return wheel_assets, sdist_assets


def assert_assets(label: str, expected: set[str], actual: set[str]) -> None:
    missing = sorted(expected - actual)
    if missing:
        raise RuntimeError(f"{label} missing tracked runtime assets: {missing}")
    print(f"{label}: {len(expected)} tracked runtime assets present")


def effect_smoke_source() -> str:
    namespace = runpy.run_path(str(ROOT / "scripts/ss_verify.py"), run_name="ss_verify_release")
    return str(namespace["EFFECT_SMOKE_SOURCE"])


def python_in(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def installed_assets(python: Path, cwd: Path) -> set[str]:
    source = """
import json
from pathlib import Path
import screenscribe
root = Path(screenscribe.__file__).parent
print(json.dumps(sorted(
    f"screenscribe/{path.relative_to(root).as_posix()}"
    for path in root.rglob("*")
    if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
)))
"""
    return set(json.loads(run(str(python), "-c", source, cwd=cwd).stdout))


def verify_install(
    artifact: Path,
    label: str,
    version: str,
    expected: set[str],
    *,
    render_smoke: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"screenscribe-{label}-") as raw_tmp:
        tmp = Path(raw_tmp)
        venv = tmp / "venv"
        run("uv", "venv", str(venv), cwd=tmp)
        python = python_in(venv)
        run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            str(artifact.resolve()),
            cwd=tmp,
        )
        installed = run(
            str(python),
            "-c",
            'import importlib.metadata; print(importlib.metadata.version("screenscribe"))',
            cwd=tmp,
        ).stdout.strip()
        if installed != version:
            raise RuntimeError(f"{label} installed version {installed!r} != {version!r}")
        run(str(python), "-m", "screenscribe", "--help", cwd=tmp)
        assert_assets(f"{label} clean install", expected, installed_assets(python, tmp))
        if render_smoke:
            smoke = tmp / "ss_effect_smoke.py"
            smoke.write_text(effect_smoke_source(), encoding="utf-8")
            result = run(str(python), str(smoke), cwd=tmp)
            print(result.stdout.strip())
        print(f"{label}: exact version {installed}; entrypoint --help OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    dist = args.dist.resolve()
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            f"expected exactly one wheel and one sdist, found {len(wheels)} wheel(s) "
            f"and {len(sdists)} sdist(s)"
        )

    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = str(tomllib.load(handle)["project"]["version"])
    expected = expected_assets()
    wheel_assets, sdist_assets = archive_assets(wheels[0], sdists[0])
    assert_assets("wheel archive", expected, wheel_assets)
    assert_assets("sdist archive", expected, sdist_assets)
    verify_install(wheels[0], "wheel", version, expected, render_smoke=True)
    verify_install(sdists[0], "sdist", version, expected, render_smoke=False)
    print("release artifacts: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
