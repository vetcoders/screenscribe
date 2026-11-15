"""Deterministic, sanitized ``publish_manifest.v1`` bundles for Screenscribe Cloud.

``screenscribe publish <review_dir>`` lifts a finished local review onto a live
``/r/<share_id>`` page. This module owns everything that happens on disk before
the upload:

1. :func:`resolve_review_source` finds the report (and, unless ``--no-video``,
   the recording) the same way ``screenscribe serve`` does, plus two edge
   cases: a ``--no-html`` review (JSON report only) and a ``--no-video``
   publish whose recording was moved or deleted.
2. :func:`build_publish_bundle` packs an allow-list of artifacts -- report
   JSON, report Markdown, transcript, ``review_state.json``, ``screenshots/``
   and ``manual_frames/`` images -- into a byte-for-byte reproducible
   ``.tar.gz`` (gzip ``mtime=0``, sorted members, normalized headers). Nothing
   else in the review directory can leak: the HTML report, cached audio,
   recordings, dotfiles and ``publish_receipt.json`` are never candidates.
   Text artifacts are scrubbed of local absolute paths first.
3. :class:`MultipartBody` streams ``manifest`` + ``bundle`` + optional
   ``video`` in 1 MiB chunks with a precomputed ``Content-Length``: memory use
   stays flat for multi-gigabyte recordings and every retry re-reads from a
   fresh handle.

The ``Idempotency-Key`` is content addressed: unchanged inputs produce the
same key on every run, so a retried or repeated publish never creates a second
share.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import secrets
import tarfile
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import IO, Any

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCHEMA_VERSION = "publish_manifest.v1"
RECEIPT_SCHEMA_VERSION = "publish_receipt.v1"
JOB_KEY_SCHEMA = "cloud_job.v1"
RECEIPT_FILENAME = "publish_receipt.json"
VISIBILITIES = ("unlisted", "private")

#: Read size for hashing and for streaming upload parts.
CHUNK_SIZE = 1024 * 1024
#: Bundles up to this size stay in memory; larger ones spill to the OS temp dir.
SPOOL_MAX_BYTES = 16 * 1024 * 1024

REPORT_JSON_SUFFIX = "_report.json"
REPORT_MD_SUFFIX = "_report.md"
REPORT_HTML_SUFFIX = "_report.html"
TRANSCRIPT_SUFFIX = "_transcript.txt"
LEGACY_REPORT_JSON = "report.json"
LEGACY_TRANSCRIPT = "transcript.txt"
REVIEW_STATE_NAME = "review_state.json"
BUNDLE_FILENAME = "bundle.tar.gz"
BUNDLE_CONTENT_TYPE = "application/gzip"
#: Stand-in for a local directory that must not travel to the cloud.
LOCAL_PATH_MARKER = "<local-path>"

VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".mkv", ".webm", ".avi", ".m4v", ".wmv"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
IMAGE_DIRS = (("screenshots", "screenshot"), ("manual_frames", "manual_frame"))

_VIDEO_CONTENT_TYPES = {
    ".avi": "video/x-msvideo",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
}
#: Keys removed wherever they appear: inline base64 frames are already stored
#: as ``manual_frames/<id>.<ext>`` files (``review_server`` strips them too).
_DROPPED_KEYS = frozenset({"frameDataUrl"})
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""
_SEPARATORS = re.compile(r"[\\/]+")
#: Absolute paths inside a macOS / Linux / Windows user home. The user name is
#: never kept: the match becomes ``<local-path>/<basename>``. The lookbehind
#: keeps URL paths intact: a home-like segment right after a host name is
#: preceded by a word character, so it never matches.
_HOME_PATH_RE = re.compile(
    r"(?<![\w.~-])"
    r"(?P<root>/(?:Users|home)/[^\s\"'<>\\/]+|[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'<>\\/]+)"
    r"(?P<rest>(?:[\\/][^\s\"'<>]*)?)"
)
_UNSAFE_PARAM_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: ``(review_dir, video) -> (review_dir, video_path, report_html_name)``;
#: ``cli_serve.resolve_existing_review`` injected by the CLI.
ReviewResolver = Callable[[Path, Path | None], tuple[Path, Path, str]]


class PublishBundleError(Exception):
    """The review cannot be packed or streamed. The message is safe to print."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def canonical_json(value: object) -> bytes:
    """Stable JSON bytes for hashing: sorted keys, no whitespace, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _pretty_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _basename(value: str) -> str:
    """Last component of a POSIX or Windows path string."""
    parts = [part for part in _SEPARATORS.split(value.strip()) if part]
    return parts[-1] if parts else ""


def validate_visibility(visibility: str) -> str:
    """``unlisted`` or ``private`` (case-insensitive); anything else raises."""
    value = visibility.strip().lower()
    if value not in VISIBILITIES:
        raise PublishBundleError(
            f"Invalid visibility {visibility!r}: use {' or '.join(VISIBILITIES)}."
        )
    return value


def validate_archive_path(name: str) -> str:
    """Archive member names are relative POSIX paths without ``..`` or NUL."""
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise PublishBundleError(f"Unsafe archive path {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PublishBundleError(f"Unsafe archive path {name!r}")
    return name


def hash_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    """``(sha256_hex, size)`` read in ``chunk_size`` pieces (flat memory)."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise PublishBundleError(
            f"Cannot read {path.name}: {error.strerror or error.__class__.__name__}"
        ) from None
    return digest.hexdigest(), size


def _hash_stream(handle: IO[bytes], *, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    handle.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := handle.read(chunk_size):
        digest.update(chunk)
        size += len(chunk)
    handle.seek(0)
    return digest.hexdigest(), size


def _package_version() -> str | None:
    try:
        return version("screenscribe")
    except PackageNotFoundError:
        from . import __version__

        return __version__


def video_content_type(path: Path) -> str:
    return _VIDEO_CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


# --------------------------------------------------------------------------- #
# Path scrubbing
# --------------------------------------------------------------------------- #


def _redact_home(match: re.Match[str]) -> str:
    rest = match.group("rest")
    core = rest.rstrip(_TRAILING_PUNCTUATION)
    tail = rest[len(core) :]
    parts = [part for part in _SEPARATORS.split(core) if part]
    return (f"{LOCAL_PATH_MARKER}/{parts[-1]}" if parts else LOCAL_PATH_MARKER) + tail


def _spellings(path: Path) -> set[str]:
    spellings = {str(path.absolute())}
    try:
        spellings.add(str(path.resolve()))
    except OSError:
        pass
    return {item.rstrip("/\\") for item in spellings if item.rstrip("/\\")}


class PathScrubber:
    """Removes local absolute paths from text that is about to leave the machine.

    Known directories (the review directory, the recording's folder) collapse
    to relative paths: ``<review_dir>/screenshots/01.jpg`` becomes
    ``screenshots/01.jpg``. Any other path inside a user home becomes
    ``<local-path>/<basename>``; the account name never survives.
    """

    def __init__(
        self, prefixes: Mapping[str, str], home_patterns: Sequence[re.Pattern[str]]
    ) -> None:
        # Longest first: the review directory wins over the folder holding it.
        self._prefixes = sorted(prefixes.items(), key=lambda item: -len(item[0]))
        self._home_patterns = tuple(home_patterns)

    @classmethod
    def for_review(cls, review_dir: Path, video: Path | None = None) -> PathScrubber:
        home = Path.home()
        home_spellings = _spellings(home)
        prefixes: dict[str, str] = {}
        folders = [review_dir]
        if video is not None:
            folders.append(video.parent)
        for folder in folders:
            for spelling in _spellings(folder):
                # A folder that is the home directory or above it would expose
                # the account name once stripped; the home patterns cover it.
                if any(home_item.startswith(spelling) for home_item in home_spellings):
                    continue
                if len(PurePosixPath(spelling).parts) < 2:
                    continue
                prefixes[spelling] = folder.name or LOCAL_PATH_MARKER
        patterns = [_HOME_PATH_RE]
        for spelling in sorted(home_spellings):
            if len(PurePosixPath(spelling).parts) >= 2 and not _HOME_PATH_RE.fullmatch(spelling):
                patterns.append(
                    re.compile(
                        r"(?<![\w.~-])(?P<root>"
                        + re.escape(spelling)
                        + r")(?P<rest>(?:[\\/][^\s\"'<>]*)?)(?![^\s\"'<>\\/])"
                    )
                )
        return cls(prefixes, patterns)

    def text(self, value: str) -> str:
        for prefix, name in self._prefixes:
            if prefix in value:
                value = value.replace(prefix + "/", "").replace(prefix + "\\", "")
                value = value.replace(prefix, name)
        for pattern in self._home_patterns:
            value = pattern.sub(_redact_home, value)
        return value

    def scrub(self, value: object) -> object:
        """Recursive copy of a JSON value with every string scrubbed."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, dict):
            return {
                key: self.scrub(item) for key, item in value.items() if key not in _DROPPED_KEYS
            }
        return value


def sanitize_report(report: Mapping[str, Any], scrubber: PathScrubber) -> dict[str, Any]:
    """Report JSON safe to publish: paths scrubbed, inline frames dropped.

    ``video`` is always the recording's basename.
    """
    clean: dict[str, Any] = {
        key: scrubber.scrub(value) for key, value in report.items() if key not in _DROPPED_KEYS
    }
    raw_video = report.get("video")
    if isinstance(raw_video, str) and raw_video.strip():
        clean["video"] = _basename(raw_video)
    return clean


# --------------------------------------------------------------------------- #
# Review directory resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReviewSource:
    """What to publish: a review directory, its report and (optionally) the recording."""

    review_dir: Path
    stem: str
    report_json: Path
    video: Path | None


def _is_regular_file(path: Path) -> bool:
    """True for a real file; a symlink is refused so it can never be followed."""
    if path.is_symlink():
        raise PublishBundleError(f"Refusing to publish {path.name}: it is a symlink.")
    return path.is_file()


def _checked_review_dir(review_dir: Path) -> Path:
    path = review_dir.expanduser()
    try:
        if path.is_symlink():
            raise PublishBundleError(
                f"Refusing to publish {path}: the review directory is a symlink. "
                "Pass the real directory."
            )
        if not path.exists():
            raise PublishBundleError(f"Review directory not found: {path}")
        if not path.is_dir():
            raise PublishBundleError(f"Not a directory: {path}")
        return path.resolve()
    except OSError as error:
        raise PublishBundleError(
            f"Cannot read review directory {path}: {error.strerror or error}"
        ) from None


def _checked_video(video: Path) -> Path:
    path = video.expanduser()
    try:
        ok = path.is_file()
    except OSError as error:
        raise PublishBundleError(f"Cannot read video {path}: {error.strerror or error}") from None
    if not ok:
        raise PublishBundleError(f"Video not found: {path}")
    return path


def _listdir(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except OSError as error:
        raise PublishBundleError(
            f"Cannot read {directory}: {error.strerror or error.__class__.__name__}"
        ) from None


def _json_reports(root: Path) -> list[Path]:
    return [
        path
        for path in _listdir(root)
        if path.name.endswith(REPORT_JSON_SUFFIX)
        and not path.name.startswith(".")
        and _is_regular_file(path)
    ]


def _load_report(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise PublishBundleError(f"{path.name} is not a readable JSON report.") from None
    if not isinstance(raw, dict):
        raise PublishBundleError(f"{path.name} is not a JSON report object.")
    return raw


def _legacy_stem(root: Path, legacy: Path, video: Path | None) -> str:
    if video is not None:
        return video.stem
    recorded = _load_report(legacy).get("video")
    if isinstance(recorded, str) and _basename(recorded):
        return Path(_basename(recorded)).stem
    return root.name.removesuffix("_review") or root.name


def _pick_json_report(root: Path, video: Path | None) -> tuple[str, Path]:
    """``(stem, report_json)`` for a review directory, without the HTML report."""
    if video is not None:
        candidate = root / f"{video.stem}{REPORT_JSON_SUFFIX}"
        if _is_regular_file(candidate):
            return video.stem, candidate
    reports = _json_reports(root)
    if len(reports) == 1:
        return reports[0].name[: -len(REPORT_JSON_SUFFIX)], reports[0]
    if reports:
        names = ", ".join(path.name for path in reports)
        raise PublishBundleError(
            f"Multiple review reports in {root}: {names}. Pass --video to choose one."
        )
    legacy = root / LEGACY_REPORT_JSON
    if _is_regular_file(legacy):
        return _legacy_stem(root, legacy, video), legacy
    raise PublishBundleError(
        f"No review report in {root}. Expected a <video>{REPORT_JSON_SUFFIX} file."
    )


def _find_recordings(root: Path, stem: str) -> list[Path]:
    found: dict[Path, Path] = {}
    for folder in (root, root.parent):
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.suffix.lower() in VIDEO_SUFFIXES and path.stem == stem and path.is_file():
                found.setdefault(path.resolve(), path)
    return sorted(found.values())


def resolve_review_source(
    review_dir: Path,
    *,
    video: Path | None = None,
    include_video: bool = True,
    resolver: ReviewResolver | None = None,
    resolver_errors: tuple[type[Exception], ...] = (),
) -> ReviewSource:
    """Find the report and recording of ``review_dir``.

    With ``include_video`` the injected ``resolver`` (``screenscribe serve``'s
    discovery: recording inside the directory, beside it, or renamed) runs
    first. A ``--no-html`` review falls back to its single
    ``<stem>_report.json`` paired with ``<stem>.<ext>`` in or beside the
    directory. Without ``include_video`` only the report is needed, so a
    moved or deleted recording is no obstacle; ``video`` then only selects the
    report.
    """
    root = _checked_review_dir(review_dir)
    if not include_video:
        stem, report = _pick_json_report(root, video.expanduser() if video else None)
        return ReviewSource(review_dir=root, stem=stem, report_json=report, video=None)

    explicit = _checked_video(video) if video is not None else None
    resolver_message: str | None = None
    if resolver is not None:
        try:
            _resolved_dir, found_video, report_name = resolver(root, explicit)
        except resolver_errors as error:
            resolver_message = str(error)
        else:
            stem = report_name.removesuffix(REPORT_HTML_SUFFIX)
            report = root / f"{stem}{REPORT_JSON_SUFFIX}"
            if not _is_regular_file(report):
                legacy = root / LEGACY_REPORT_JSON
                if not _is_regular_file(legacy):
                    raise PublishBundleError(
                        f"{report_name} in {root} has no {report.name} next to it "
                        "(was the review run with --no-json?). Re-run the review with JSON output."
                    )
                report = legacy
            return ReviewSource(
                review_dir=root, stem=stem, report_json=report, video=_checked_video(found_video)
            )

    stem, report = _pick_json_report(root, explicit)
    if explicit is not None:
        return ReviewSource(review_dir=root, stem=stem, report_json=report, video=explicit)
    recordings = _find_recordings(root, stem)
    if len(recordings) == 1:
        return ReviewSource(review_dir=root, stem=stem, report_json=report, video=recordings[0])
    if len(recordings) > 1:
        names = ", ".join(path.name for path in recordings)
        raise PublishBundleError(f"Multiple videos match {report.name}: {names}. Pass --video.")
    message = resolver_message or (
        f"Found {report.name} in {root}, but not the recording it was made from "
        "(moved, renamed or deleted). Pass --video with the original recording."
    )
    raise PublishBundleError(f"{message} Or pass --no-video to publish without the recording.")


# --------------------------------------------------------------------------- #
# Bundle
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BundleArtifact:
    """One archive member as listed in the manifest."""

    archive_path: str
    kind: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_path": self.archive_path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class VideoFile:
    """The recording that travels as the separate multipart ``video`` part."""

    path: Path
    filename: str
    size_bytes: int
    sha256: str
    content_type: str

    @classmethod
    def from_path(cls, path: Path) -> VideoFile:
        digest, size = hash_file(path)
        return cls(
            path=path,
            filename=path.name,
            size_bytes=size,
            sha256=digest,
            content_type=video_content_type(path),
        )

    def part(self, name: str = "video") -> FilePart:
        path = self.path
        return FilePart(
            name=name,
            filename=self.filename,
            content_type=self.content_type,
            size=self.size_bytes,
            opener=lambda: path.open("rb"),
        )


@dataclass(frozen=True)
class _Member:
    archive_path: str
    kind: str
    data: bytes | None = None
    source: Path | None = None


class _HashingReader:
    """File wrapper hashing exactly what ``tarfile`` copies into the archive."""

    def __init__(self, handle: IO[bytes]) -> None:
        self._handle = handle
        self.digest = hashlib.sha256()
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        self.digest.update(chunk)
        self.count += len(chunk)
        return chunk


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(validate_archive_path(name))
    info.size = size
    info.type = tarfile.REGTYPE
    info.mode = 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root)
    except OSError:
        return False


def _optional_text(root: Path, names: Sequence[str], skipped: list[str]) -> str | None:
    """First regular, non-symlink file of ``names`` inside ``root``, decoded."""
    for name in names:
        path = root / name
        if path.is_symlink():
            skipped.append(f"{name} (symlink)")
            continue
        if not path.is_file() or not _inside(root, path):
            continue
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped.append(f"{name} (unreadable)")
    return None


def _transcript_from_report(report: Mapping[str, Any]) -> str | None:
    for key in ("transcript_timestamped", "transcript"):
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip() + "\n"
    segments = report.get("transcript_segments")
    if isinstance(segments, list):
        lines = [
            segment["text"].strip()
            for segment in segments
            if isinstance(segment, dict)
            and isinstance(segment.get("text"), str)
            and segment["text"].strip()
        ]
        if lines:
            return "\n".join(lines) + "\n"
    return None


def _image_members(root: Path, skipped: list[str]) -> list[_Member]:
    members: list[_Member] = []
    for dirname, kind in IMAGE_DIRS:
        directory = root / dirname
        if directory.is_symlink():
            skipped.append(f"{dirname}/ (symlink)")
            continue
        if not directory.is_dir():
            continue
        for path in _listdir(directory):
            name = path.name
            if name.startswith(".") or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if path.is_symlink():
                skipped.append(f"{dirname}/{name} (symlink)")
                continue
            if not path.is_file() or not _inside(root, path):
                continue
            members.append(_Member(archive_path=f"{dirname}/{name}", kind=kind, source=path))
    return members


def _summary(report: Mapping[str, Any]) -> dict[str, int]:
    raw = report.get("summary")
    summary: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    findings = report.get("findings")
    fallback_total = len(findings) if isinstance(findings, list) else 0

    def count(key: str, default: int = 0) -> int:
        value = summary.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return default
        return int(value)

    return {
        "total": count("total", fallback_total),
        "bugs": count("bugs"),
        "changes": count("changes"),
        "ui": count("ui"),
    }


def _report_field(report: Mapping[str, Any], keys: Sequence[str]) -> object:
    sources: list[Mapping[str, Any]] = [report]
    provenance = report.get("processing_provenance")
    if isinstance(provenance, dict):
        sources.append(provenance)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None and value != "":
                return value
    return None


def publish_idempotency_key(
    *,
    bundle_sha256: str,
    visibility: str,
    video_filename: str | None,
    video_sha256: str | None,
) -> str:
    """``sha256(canonical_json(...))`` of what makes a publish unique."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "bundle_sha256": bundle_sha256,
        "visibility": visibility,
        "video_filename": video_filename,
        "video_sha256": video_sha256,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def job_idempotency_key(
    *,
    video_sha256: str,
    visibility: str,
    video_filename: str,
    language: str | None = None,
    preset: str | None = None,
    prompt: str | None = None,
) -> str:
    """Key for ``POST /api/v1/jobs``: same recording + same options = same job."""
    payload = {
        "schema_version": JOB_KEY_SCHEMA,
        "video_sha256": video_sha256,
        "video_filename": video_filename,
        "visibility": visibility,
        "language": language,
        "preset": preset,
        "prompt": prompt,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


@dataclass
class PublishBundle:
    """A packed review: manifest, archive spool and the optional recording.

    Close it (or use it as a context manager) to drop the temporary archive.
    """

    source: ReviewSource
    manifest: dict[str, Any]
    archive: IO[bytes]
    size_bytes: int
    sha256: str
    video: VideoFile | None
    artifacts: list[BundleArtifact] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def idempotency_key(self) -> str:
        return str(self.manifest["idempotency_key"])

    def manifest_json(self) -> str:
        return canonical_json(self.manifest).decode("utf-8")

    def bundle_part(self) -> FilePart:
        archive = self.archive

        def rewind() -> IO[bytes]:
            archive.seek(0)
            return archive

        return FilePart(
            name="bundle",
            filename=BUNDLE_FILENAME,
            content_type=BUNDLE_CONTENT_TYPE,
            size=self.size_bytes,
            opener=rewind,
            owned=False,
        )

    def multipart(self) -> MultipartBody:
        """Fresh ``manifest`` + ``bundle`` (+ ``video``) body for one attempt."""
        files = [self.bundle_part()]
        if self.video is not None:
            files.append(self.video.part("video"))
        return MultipartBody([FormField("manifest", self.manifest_json())], files)

    def close(self) -> None:
        self.archive.close()

    def __enter__(self) -> PublishBundle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _write_archive(members: Sequence[_Member], spool: IO[bytes]) -> list[BundleArtifact]:
    artifacts: list[BundleArtifact] = []
    with (
        gzip.GzipFile(filename="", mode="wb", fileobj=spool, mtime=0, compresslevel=6) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for member in members:
            if member.data is not None:
                data = member.data
                tar.addfile(_tar_info(member.archive_path, len(data)), io.BytesIO(data))
                digest = hashlib.sha256(data).hexdigest()
                artifacts.append(
                    BundleArtifact(member.archive_path, member.kind, len(data), digest)
                )
                continue
            if member.source is None:
                raise PublishBundleError(f"{member.archive_path} has no content.")
            try:
                size = member.source.stat().st_size
                with member.source.open("rb") as handle:
                    reader = _HashingReader(handle)
                    tar.addfile(_tar_info(member.archive_path, size), reader)
            except (OSError, tarfile.TarError):
                raise PublishBundleError(
                    f"{member.archive_path} changed or vanished while packing; retry."
                ) from None
            if reader.count != size:
                raise PublishBundleError(f"{member.archive_path} changed while packing; retry.")
            artifacts.append(
                BundleArtifact(member.archive_path, member.kind, size, reader.digest.hexdigest())
            )
    return artifacts


def build_publish_bundle(source: ReviewSource, *, visibility: str) -> PublishBundle:
    """Pack ``source`` into a sanitized, reproducible ``publish_manifest.v1`` bundle."""
    visibility = validate_visibility(visibility)
    root = source.review_dir
    stem = source.stem
    report = _load_report(source.report_json)
    scrubber = PathScrubber.for_review(root, source.video)
    clean = sanitize_report(report, scrubber)
    skipped: list[str] = []

    report_name = f"{stem}{REPORT_JSON_SUFFIX}"
    members = [_Member(report_name, "report_json", data=_pretty_json(clean))]

    markdown = _optional_text(root, [f"{stem}{REPORT_MD_SUFFIX}"], skipped)
    if markdown is not None:
        markdown_bytes = scrubber.text(markdown).encode("utf-8")
        members.append(_Member(f"{stem}{REPORT_MD_SUFFIX}", "report_markdown", markdown_bytes))

    transcript_name = f"{stem}{TRANSCRIPT_SUFFIX}"
    transcript = _optional_text(root, [transcript_name, LEGACY_TRANSCRIPT], skipped)
    if transcript is None:
        transcript = _transcript_from_report(clean)
    else:
        transcript = scrubber.text(transcript)
    if transcript is not None:
        members.append(_Member(transcript_name, "transcript", transcript.encode("utf-8")))

    has_review_state = (
        clean.get("human_review") is not None or clean.get("work_items") is not None
    )
    if has_review_state:
        state = {"human_review": clean.get("human_review"), "work_items": clean.get("work_items")}
        members.append(_Member(REVIEW_STATE_NAME, "review_state", data=_pretty_json(state)))

    members.extend(_image_members(root, skipped))
    members.sort(key=lambda member: member.archive_path)
    names = [validate_archive_path(member.archive_path) for member in members]
    if len(set(names)) != len(names):
        raise PublishBundleError("Duplicate archive paths in the review directory.")

    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES, mode="w+b")
    try:
        artifacts = _write_archive(members, spool)
        bundle_sha, bundle_size = _hash_stream(spool)
        video = VideoFile.from_path(source.video) if source.video is not None else None
    except BaseException:
        spool.close()
        raise

    recorded = clean.get("video")
    video_filename = (
        video.filename if video is not None else (recorded if isinstance(recorded, str) else None)
    ) or None
    key = publish_idempotency_key(
        bundle_sha256=bundle_sha,
        visibility=visibility,
        video_filename=video_filename,
        video_sha256=video.sha256 if video is not None else None,
    )
    generated_at = clean.get("generated_at")
    language = _report_field(clean, ("language", "lang"))
    duration = _report_field(clean, ("duration_seconds", "duration"))
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "idempotency_key": key,
        "bundle_sha256": bundle_sha,
        "visibility": visibility,
        "video_stem": stem,
        "video_filename": video_filename,
        "video_name": video_filename or f"{stem}.mp4",
        "video_included": video is not None,
        "video_sha256": video.sha256 if video is not None else None,
        "video_size_bytes": video.size_bytes if video is not None else None,
        "report_filename": report_name,
        "transcript_filename": transcript_name if transcript is not None else None,
        "has_review_state": has_review_state,
        "summary": _summary(clean),
        "artifacts": [artifact.to_dict() for artifact in artifacts],
        "generated_at": generated_at if isinstance(generated_at, str) else None,
        "language": language if isinstance(language, str) else None,
        "duration_seconds": (
            float(duration)
            if isinstance(duration, int | float) and not isinstance(duration, bool)
            else None
        ),
        "screenscribe_version": _package_version(),
        "title": stem,
    }
    return PublishBundle(
        source=source,
        manifest=manifest,
        archive=spool,
        size_bytes=bundle_size,
        sha256=bundle_sha,
        video=video,
        artifacts=artifacts,
        skipped=skipped,
    )


# --------------------------------------------------------------------------- #
# Streaming multipart
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormField:
    """A plain ``multipart/form-data`` text field."""

    name: str
    value: str


@dataclass(frozen=True)
class FilePart:
    """A streamed file part. ``opener`` returns a handle positioned at byte 0."""

    name: str
    filename: str
    content_type: str
    size: int
    opener: Callable[[], IO[bytes]]
    #: Close the handle after streaming (False for the reusable bundle spool).
    owned: bool = True


def _quote_param(value: str) -> str:
    """HTML5 form-data parameter escaping (same rules as ``httpx``)."""
    value = value.replace("\\", "\\\\").replace('"', "%22")
    return _UNSAFE_PARAM_CHARS.sub(lambda match: f"%{ord(match.group(0)):02X}", value)


class MultipartBody:
    """``multipart/form-data`` produced in ``chunk_size`` pieces.

    ``Content-Length`` is known up front (field bytes + declared file sizes),
    so ``httpx`` sends a plain length-delimited body, never chunked encoding.
    Iterate :meth:`iter_chunks` once per attempt: each pass reopens files.
    """

    def __init__(
        self,
        fields: Sequence[FormField],
        files: Sequence[FilePart],
        *,
        boundary: str | None = None,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self.boundary = boundary or f"screenscribe-{secrets.token_hex(16)}"
        self._fields = tuple(fields)
        self._files = tuple(files)
        self._chunk_size = chunk_size

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def _field_head(self, item: FormField) -> bytes:
        return (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{_quote_param(item.name)}"\r\n\r\n'
        ).encode()

    def _file_head(self, part: FilePart) -> bytes:
        return (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{_quote_param(part.name)}"; '
            f'filename="{_quote_param(part.filename)}"\r\n'
            f"Content-Type: {part.content_type}\r\n\r\n"
        ).encode()

    def _tail(self) -> bytes:
        return f"--{self.boundary}--\r\n".encode()

    @property
    def content_length(self) -> int:
        total = len(self._tail())
        for item in self._fields:
            total += len(self._field_head(item)) + len(item.value.encode("utf-8")) + 2
        for part in self._files:
            total += len(self._file_head(part)) + part.size + 2
        return total

    def headers(self) -> dict[str, str]:
        return {"Content-Type": self.content_type, "Content-Length": str(self.content_length)}

    def iter_chunks(self) -> Iterator[bytes]:
        for item in self._fields:
            yield self._field_head(item) + item.value.encode("utf-8") + b"\r\n"
        for part in self._files:
            yield self._file_head(part)
            try:
                handle = part.opener()
            except OSError:
                raise PublishBundleError(f"Cannot open {part.filename} for upload.") from None
            try:
                remaining = part.size
                while remaining > 0:
                    chunk = handle.read(min(self._chunk_size, remaining))
                    if not chunk:
                        raise PublishBundleError(
                            f"{part.filename} shrank during upload; publish again."
                        )
                    remaining -= len(chunk)
                    yield chunk
            finally:
                if part.owned:
                    handle.close()
            yield b"\r\n"
        yield self._tail()


# --------------------------------------------------------------------------- #
# Receipt
# --------------------------------------------------------------------------- #


def write_publish_receipt(review_dir: Path, receipt: Mapping[str, Any]) -> Path:
    """Atomically write ``<review_dir>/publish_receipt.json`` (temp + ``os.replace``)."""
    target = review_dir / RECEIPT_FILENAME
    if target.is_symlink():
        raise PublishBundleError(f"Refusing to overwrite {RECEIPT_FILENAME}: it is a symlink.")
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".publish_receipt.", suffix=".tmp", dir=review_dir)
    except OSError as error:
        raise PublishBundleError(
            f"Cannot write {RECEIPT_FILENAME}: {error.strerror or error.__class__.__name__}"
        ) from None
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(receipt), handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except OSError as error:
        temp_path.unlink(missing_ok=True)
        raise PublishBundleError(
            f"Cannot write {RECEIPT_FILENAME}: {error.strerror or error.__class__.__name__}"
        ) from None
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return target
