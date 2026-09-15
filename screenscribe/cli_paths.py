"""Output-path versioning helpers for the screenscribe CLI.

Extracted from ``cli.py`` so the path-bumping logic (``video_review`` →
``video_review_2`` …) has a single home. ``cli.py`` re-imports the public
names back into its own namespace so the historical import/patch surface
(``screenscribe.cli._find_next_review_path``,
``screenscribe.cli.MAX_REVIEW_VERSIONS``) is preserved.
"""

import json
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

# Manifest written by ``write_preprocess_bundle`` (screenscribe/preprocess.py).
PREPROCESS_MANIFEST_NAME = "preprocess.json"

# A real manifest is a few KB; anything bigger is not ours and is not read.
MAX_PREPROCESS_MANIFEST_BYTES = 1024 * 1024


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


def is_preprocess_bundle(path: Path) -> bool:
    """Whether ``path`` is a screenscribe preprocess bundle (the single rule).

    True only when ``path`` is a directory holding a ``preprocess.json`` that is
    screenscribe's own manifest: a JSON object with ``"mode": "preprocess"`` and
    an ``"artifacts"`` object. ``write_preprocess_bundle`` has written both keys
    since the first public release, and ``mode`` is the field that names what
    produced the file. The manifest is read with a size cap; an unreadable,
    oversized, non-JSON or foreign ``preprocess.json`` is not a bundle, and a
    lone ``transcript.txt`` never is (plenty of tools write one).

    The ``preprocess`` command treats an existing ordinary folder passed via
    ``-o`` as a PARENT and writes ``<folder>/<stem>_preprocess`` inside it, so
    the check is kept deliberately narrow: a false positive would version a
    sibling next to the user's folder (``~/Downloads_2``), a false negative only
    nests one level. A bundle made for a different video still counts -- it is
    screenscribe output either way, and versioning keeps it intact.
    """
    if not _is_dir(path):
        return False
    manifest = path / PREPROCESS_MANIFEST_NAME
    if not _is_file(manifest):
        return False
    try:
        with manifest.open("rb") as handle:
            raw = handle.read(MAX_PREPROCESS_MANIFEST_BYTES + 1)
    except OSError:
        return False
    if len(raw) > MAX_PREPROCESS_MANIFEST_BYTES:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError.
        return False
    return (
        isinstance(data, dict)
        and data.get("mode") == "preprocess"
        and isinstance(data.get("artifacts"), dict)
    )


def _find_next_versioned_path(
    base_path: Path,
    *,
    artifact_markers: tuple[str, ...] = (),
    artifact_globs: tuple[str, ...] = (),
    bundle_detector: Callable[[Path], bool] | None = None,
) -> tuple[Path, int | None]:
    """Find next available artifact path, appending _2, _3, etc. if needed.

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
        if not versioned_path.exists() or not has_artifact_bundle(versioned_path):
            return versioned_path, version
        version += 1
        if version > cli.MAX_REVIEW_VERSIONS:
            raise RuntimeError(f"Too many review versions for {base_path.name}")


def _find_next_review_path(
    base_path: Path, video_stem: str | None = None
) -> tuple[Path, int | None]:
    """Find next available review path, appending _2, _3, etc. if needed.

    Versioning is triggered only by a completed report bundle for this video
    (``has_review_report_bundle`` -- the same report rule ``is_review_directory``
    uses), never by foreign files. A checkpoint-only directory (a partial run
    that wrote no report) is deliberately NOT a completed bundle here: it is
    reused in place, as before, and ``--resume`` picks the checkpoint up.
    """
    return _find_next_versioned_path(
        base_path,
        bundle_detector=lambda path: has_review_report_bundle(path, video_stem),
    )
