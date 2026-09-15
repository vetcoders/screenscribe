"""Output-path versioning helpers for the screenscribe CLI.

Extracted from ``cli.py`` so the path-bumping logic (``video_review`` →
``video_review_2`` …) has a single home. ``cli.py`` re-imports the public
names back into its own namespace so the historical import/patch surface
(``screenscribe.cli._find_next_review_path``,
``screenscribe.cli.MAX_REVIEW_VERSIONS``) is preserved.
"""

from collections.abc import Callable
from pathlib import Path

from .checkpoint import CHECKPOINT_DIR_NAME

# Maximum number of auto-versioned review directories (video_review_2, _3, etc.)
MAX_REVIEW_VERSIONS = 99

# Non-stemmed report names written by pre-public screenscribe builds. Kept so
# older review directories on disk stay protected from being overwritten.
LEGACY_REVIEW_REPORT_MARKERS = ("report.html", "report.json")

# Extensions of the per-video ``<stem>_report.<ext>`` bundle a review writes.
REVIEW_REPORT_EXTENSIONS = ("html", "json", "md")

REVIEW_DIR_SUFFIX = "_review"


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` that treats an unreadable parent as "not a directory"."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _review_stem_from_dir_name(name: str) -> str | None:
    """Recover the video stem from a default ``<stem>_review[_N]`` directory name."""
    base, sep, suffix = name.rpartition("_")
    if sep and suffix.isdigit() and base.endswith(REVIEW_DIR_SUFFIX):
        name = base
    if name.endswith(REVIEW_DIR_SUFFIX) and len(name) > len(REVIEW_DIR_SUFFIX):
        return name[: -len(REVIEW_DIR_SUFFIX)]
    return None


def has_review_report_bundle(path: Path, video_stem: str | None = None) -> bool:
    """Whether ``path`` holds a screenscribe report bundle for ``video_stem``.

    A bundle is either:

    - ``<video_stem>_report.{html,json,md}`` -- what every public release writes.
      The stem must be the video being processed. A bare ``*_report.*`` glob is
      NOT enough: ordinary folders (e.g. ``~/Downloads``) routinely contain
      unrelated ``something_report.md`` files, and matching those made
      ``review -o ~/Downloads`` treat Downloads as a previous review; or
    - a legacy non-stemmed ``report.html`` or ``report.json`` (pre-public
      builds), kept so older review directories stay protected.

    When ``video_stem`` is ``None`` it is derived from a default
    ``<stem>_review`` / ``<stem>_review_N`` directory name; for any other name
    only the legacy markers can match.
    """
    if not _is_dir(path):
        return False
    if any(_is_file(path / marker) for marker in LEGACY_REVIEW_REPORT_MARKERS):
        return True
    stem = video_stem if video_stem is not None else _review_stem_from_dir_name(path.name)
    if not stem:
        return False
    return any(_is_file(path / f"{stem}_report.{ext}") for ext in REVIEW_REPORT_EXTENSIONS)


def is_review_directory(path: Path, video_stem: str | None = None) -> bool:
    """Whether ``path`` is a screenscribe review directory (the single rule).

    True when ``path`` is a directory that contains either:

    - a ``.screenscribe_cache/`` checkpoint directory -- only screenscribe
      creates it, and it marks a partial/failed run that may be resumed; or
    - a report bundle for this video (see ``has_review_report_bundle``).

    Anything else -- including a folder holding unrelated files, reports named
    after a *different* video, or nothing at all -- is an ordinary folder. The
    ``review`` command treats an existing ordinary folder passed via ``-o`` as a
    PARENT and writes ``<folder>/<stem>_review`` inside it, so the check is kept
    deliberately narrow: a false positive would version a sibling next to the
    user's folder (``~/Downloads_2``), a false negative only nests one level.
    """
    if not _is_dir(path):
        return False
    if _is_dir(path / CHECKPOINT_DIR_NAME):
        return True
    return has_review_report_bundle(path, video_stem)


def _find_next_versioned_path(
    base_path: Path,
    *,
    artifact_markers: tuple[str, ...] = (),
    artifact_globs: tuple[str, ...] = (),
    bundle_detector: Callable[[Path], bool] | None = None,
) -> tuple[Path, int | None]:
    """Find next available artifact path, appending _2, _3, etc. if needed.

    Two different questions are asked:

    - ``base_path`` itself is reused unless it holds a completed bundle (the
      marker/glob checks or ``bundle_detector``), so a partial run or a folder
      without a bundle keeps being written in place.
    - A version slot ``<base>_N`` (N >= 2) is available ONLY if it does not
      exist or is an empty directory. Any existing file, or any non-empty
      directory -- a bundle or not (another video's report, notes, a
      checkpoint-only partial run) -- is occupied, so a rerun never mixes its
      output into someone else's folder.

    Args:
        base_path: The initial desired output path (e.g., video_review)
        artifact_markers: Exact filenames that prove the directory already
            contains a completed artifact bundle.
        artifact_globs: Glob patterns (e.g. ``*_report.html``) that prove a
            completed bundle.
        bundle_detector: Optional predicate that decides on its own whether a
            directory holds a completed bundle. When given, it replaces the
            marker/glob checks.

    Returns:
        Tuple of (available_path, version_number or None if first)
    """

    def has_artifact_bundle(path: Path) -> bool:
        if bundle_detector is not None:
            return bundle_detector(path)
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
        if _version_slot_is_free(versioned_path):
            return versioned_path, version
        version += 1
        if version > cli.MAX_REVIEW_VERSIONS:
            raise RuntimeError(f"Too many review versions for {base_path.name}")


def _version_slot_is_free(path: Path) -> bool:
    """A version slot is free only when nothing exists there or it is an empty dir."""
    try:
        if not path.exists():
            return True
        if not path.is_dir():
            return False
        return next(path.iterdir(), None) is None
    except OSError:
        # Unreadable: never treat as free, advance to the next slot instead.
        return False


def _find_next_review_path(
    base_path: Path, video_stem: str | None = None
) -> tuple[Path, int | None]:
    """Find next available review path, appending _2, _3, etc. if needed.

    Versioning is triggered only by a completed report bundle for this video in
    ``base_path`` (``has_review_report_bundle`` -- the same report rule
    ``is_review_directory`` uses), never by foreign files. A checkpoint-only
    ``base_path`` (a partial run that wrote no report) is deliberately NOT a
    completed bundle: it is reused in place and ``--resume`` picks the checkpoint
    up. Once versioning starts, ``<base>_N`` slots follow the stricter rule of
    ``_find_next_versioned_path``: only a missing path or an empty directory is
    used, so an existing non-empty ``_N`` is never written into.
    """
    return _find_next_versioned_path(
        base_path,
        bundle_detector=lambda path: has_review_report_bundle(path, video_stem),
    )
