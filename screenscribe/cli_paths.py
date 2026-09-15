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
from typing import Literal

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


OutputSlotState = Literal["free", "own_partial", "own_complete", "foreign"]


class OutputVersionsExhaustedError(RuntimeError):
    """No free ``<base>_N`` output slot is left below the version limit.

    Command-neutral (shared by every caller of ``_find_next_versioned_path``).
    Subclasses ``RuntimeError`` so older ``except RuntimeError`` callers still
    catch it; CLI commands should catch it and print a friendly error.
    """

    def __init__(self, base_path: Path, limit: int) -> None:
        super().__init__(f"Too many existing versions of {base_path.name} (limit {limit})")
        self.base_path = base_path
        self.limit = limit


def classify_output_slot(
    path: Path,
    *,
    owns_dir: Callable[[Path], bool],
    has_completed_bundle: Callable[[Path], bool],
) -> OutputSlotState:
    """Classify an output location before screenscribe writes anything there.

    - ``"free"``: nothing exists at ``path``, or it is an empty directory.
    - ``"own_complete"``: a directory the command owns (``owns_dir``) that holds
      a completed bundle (``has_completed_bundle``).
    - ``"own_partial"``: an owned directory without a completed bundle (e.g. a
      review with only a ``.screenscribe_cache/`` checkpoint).
    - ``"foreign"``: an existing file, a non-empty directory the command does not
      own, or anything that cannot be read. Screenscribe never writes into,
      reuses, or deletes inside a foreign slot.

    ``owns_dir`` and ``has_completed_bundle`` are command-specific predicates
    (review: ``is_review_directory`` / ``has_review_report_bundle``); a command
    whose ownership marker IS its completed bundle passes the same predicate
    for both.
    """
    try:
        if not path.exists():
            return "free"
        if not path.is_dir():
            return "foreign"
        if next(path.iterdir(), None) is None:
            return "free"
        if not owns_dir(path):
            return "foreign"
        return "own_complete" if has_completed_bundle(path) else "own_partial"
    except OSError:
        return "foreign"


def classify_review_slot(path: Path, video_stem: str | None = None) -> OutputSlotState:
    """``classify_output_slot`` with the review ownership rules for ``video_stem``."""
    return classify_output_slot(
        path,
        owns_dir=lambda candidate: is_review_directory(candidate, video_stem),
        has_completed_bundle=lambda candidate: has_review_report_bundle(candidate, video_stem),
    )


def _find_next_versioned_path(
    base_path: Path,
    *,
    owns_dir: Callable[[Path], bool] | None = None,
    has_completed_bundle: Callable[[Path], bool] | None = None,
    artifact_markers: tuple[str, ...] = (),
    artifact_globs: tuple[str, ...] = (),
) -> tuple[Path, int | None]:
    """Find the output path to use, appending _2, _3, etc. if needed.

    Slots are classified with ``classify_output_slot``:

    - ``base_path`` is used when it is ``free`` or ``own_partial`` (a partial run
      keeps being reused in place, so ``--resume`` finds its checkpoint). An
      ``own_complete`` or ``foreign`` base advances to ``<base>_2``.
    - A version slot ``<base>_N`` (N >= 2) is used ONLY when it is ``free``
      (missing or an empty directory). Any existing file or non-empty directory
      -- owned or not -- is occupied, so a run never mixes its output into
      another folder.

    Args:
        base_path: The initial desired output path (e.g., video_review).
        owns_dir: Predicate: does the command own this directory?
        has_completed_bundle: Predicate: does the directory hold a completed
            bundle? Pass both predicates for the ownership-aware contract.
        artifact_markers: Legacy (used when the predicates are omitted): exact
            filenames that prove a completed bundle.
        artifact_globs: Legacy: glob patterns that prove a completed bundle.
            In legacy mode every existing directory counts as owned, so only a
            file at ``base_path`` is treated as foreign.

    Returns:
        Tuple of (available_path, version_number or None if the base is used).

    Raises:
        OutputVersionsExhaustedError: no free slot up to ``MAX_REVIEW_VERSIONS``.
    """
    if owns_dir is None or has_completed_bundle is None:

        def legacy_bundle(path: Path) -> bool:
            if any((path / marker).exists() for marker in artifact_markers):
                return True
            return any(next(path.glob(pattern), None) is not None for pattern in artifact_globs)

        owns_dir = owns_dir or (lambda _path: True)
        has_completed_bundle = has_completed_bundle or legacy_bundle

    base_state = classify_output_slot(
        base_path, owns_dir=owns_dir, has_completed_bundle=has_completed_bundle
    )
    if base_state in ("free", "own_partial"):
        return base_path, None

    # Read the cap through the cli module so tests that patch
    # ``cli.MAX_REVIEW_VERSIONS`` (the historical surface) still bind here.
    import screenscribe.cli as cli

    version = 2
    while True:
        versioned_path = base_path.parent / f"{base_path.name}_{version}"
        state = classify_output_slot(
            versioned_path, owns_dir=owns_dir, has_completed_bundle=has_completed_bundle
        )
        if state == "free":
            return versioned_path, version
        version += 1
        if version > cli.MAX_REVIEW_VERSIONS:
            raise OutputVersionsExhaustedError(base_path, cli.MAX_REVIEW_VERSIONS)


def _find_next_review_path(
    base_path: Path, video_stem: str | None = None
) -> tuple[Path, int | None]:
    """Find the review output path for ``video_stem``, versioning when needed.

    Ownership follows the single review rule: a directory is the review's own
    when ``is_review_directory`` says so, and complete when it holds this
    video's report bundle (``has_review_report_bundle``). So a checkpoint-only
    base (``own_partial``) is reused in place, a completed review or any foreign
    non-empty folder/file at the base advances to ``_2``, and ``<base>_N`` slots
    are used only when missing or empty (see ``_find_next_versioned_path``).
    """
    return _find_next_versioned_path(
        base_path,
        owns_dir=lambda path: is_review_directory(path, video_stem),
        has_completed_bundle=lambda path: has_review_report_bundle(path, video_stem),
    )
