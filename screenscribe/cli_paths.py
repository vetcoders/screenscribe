"""Output-path versioning helpers for the screenscribe CLI.

Extracted from ``cli.py`` so the path-bumping logic (``video_review`` →
``video_review_2`` …) has a single home. ``cli.py`` re-imports the public
names back into its own namespace so the historical import/patch surface
(``screenscribe.cli._find_next_review_path``,
``screenscribe.cli.MAX_REVIEW_VERSIONS``) is preserved.
"""

from pathlib import Path

# Maximum number of auto-versioned review directories (video_review_2, _3, etc.)
MAX_REVIEW_VERSIONS = 99


def _find_next_versioned_path(
    base_path: Path,
    *,
    artifact_markers: tuple[str, ...] = (),
    artifact_globs: tuple[str, ...] = (),
) -> tuple[Path, int | None]:
    """Find next available artifact path, appending _2, _3, etc. if needed.

    Args:
        base_path: The initial desired output path (e.g., video_review)
        artifact_markers: Exact filenames that prove the directory already
            contains a completed artifact bundle.
        artifact_globs: Glob patterns (e.g. ``*_report.html``) that prove a
            completed bundle. Needed because report files are written under a
            per-video stem (``<stem>_report.html``), so an exact name cannot be
            known up front.

    Returns:
        Tuple of (available_path, version_number or None if first)
    """

    def has_artifact_bundle(path: Path) -> bool:
        if any((path / marker).exists() for marker in artifact_markers):
            return True
        return any(next(path.glob(pattern), None) is not None for pattern in artifact_globs)

    if not base_path.exists() or not has_artifact_bundle(base_path):
        return base_path, None

    # Read the cap through the cli module so tests that patch
    # ``cli.MAX_REVIEW_VERSIONS`` (the historical surface) still bind here.
    import screenscribe.cli as cli

    # Find next available number
    version = 2
    while True:
        versioned_path = base_path.parent / f"{base_path.name}_{version}"
        if not versioned_path.exists() or not has_artifact_bundle(versioned_path):
            return versioned_path, version
        version += 1
        if version > cli.MAX_REVIEW_VERSIONS:
            raise RuntimeError(f"Too many review versions for {base_path.name}")


def _find_next_review_path(base_path: Path) -> tuple[Path, int | None]:
    """Find next available review path, appending _2, _3, etc. if needed.

    A completed review bundle is written under the video stem
    (``<stem>_report.{html,json,md}``), so detection is glob-based. The legacy
    non-stemmed ``report.{html,json}`` names are kept as exact markers so older
    review directories on disk are still protected from being overwritten.
    """
    return _find_next_versioned_path(
        base_path,
        artifact_markers=("report.html", "report.json"),
        artifact_globs=("*_report.html", "*_report.json", "*_report.md"),
    )
