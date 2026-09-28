"""Hatchling build hook that bakes git commit provenance into wheels.

``make install`` (``uv tool install .``) drops the package into site-packages,
where no ``.git`` work tree exists for ``screenscribe --version`` to probe.
Baking ``g<sha>[-dirty]`` into ``screenscribe/_commit.py`` at wheel-build time
keeps the "version always carries its commit" contract for installed builds.
Editable builds are skipped on purpose: they probe the live work tree, which
is always fresher than anything a build could bake.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_GIT_TIMEOUT = 5


class CommitProvenanceHook(BuildHookInterface):
    """Bake ``_commit.py`` with the current HEAD sha into standard wheels."""

    PLUGIN_NAME = "commit-provenance"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version != "standard":
            return
        git = shutil.which("git")
        if git is None:
            return
        commit = self._git(git, "rev-parse", "--short", "HEAD")
        if not commit:
            return
        dirty = bool(self._git(git, "status", "--porcelain"))
        staging = Path(tempfile.mkdtemp(prefix="screenscribe-commit-"))
        module = staging / "_commit.py"
        module.write_text(
            '"""Build-time commit provenance baked by hatch_build.py. Do not edit."""\n\n'
            f'COMMIT = "{commit}"\n'
            f"DIRTY = {dirty}\n",
            encoding="utf-8",
        )
        build_data.setdefault("force_include", {})[str(module)] = "screenscribe/_commit.py"

    def _git(self, git: str, *args: str) -> str:
        try:
            return subprocess.run(
                [git, "-C", self.root, *args],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
                check=True,
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return ""
