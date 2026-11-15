"""Security primitives for Screenscribe Cloud (stdlib only).

- scrypt password hashing with constant-time verification
- opaque bearer/refresh/device credentials stored only as SHA-256 hashes
- CSRF double-submit + Origin/Referer host checks for cookie sessions
- archive extraction that refuses traversal, links, devices and bombs
- small UTC time helpers shared by every cloud module
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import shutil
import stat
import tarfile
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, Literal
from urllib.parse import urlsplit

# -- Time --------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat_z(value: datetime) -> str:
    """RFC 3339 UTC timestamp with a ``Z`` suffix and second precision."""
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO timestamp produced by :func:`isoformat_z` (or similar)."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


# -- Identifiers & opaque credentials ---------------------------------------

USER_PREFIX = "usr_"
JOB_PREFIX = "job_"
REVIEW_PREFIX = "rev_"
SHARE_PREFIX = "shr_"
ACCESS_PREFIX = "ss_acc_"
REFRESH_PREFIX = "ss_ref_"
DEVICE_PREFIX = "ss_dev_"
WEB_PREFIX = "ss_web_"

SHARE_ID_RE = re.compile(r"^shr_[A-Za-z0-9_-]{16,64}$")
JOB_ID_RE = re.compile(r"^job_[0-9a-f]{24}$")
_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ" + "23456789"


def new_id(prefix: str) -> str:
    """Random identifier such as ``usr_3f9c...`` (96 bits of entropy)."""
    return f"{prefix}{secrets.token_hex(12)}"


def new_share_id() -> str:
    """High-entropy capability identifier for unlisted review links (128 bits)."""
    return f"{SHARE_PREFIX}{secrets.token_urlsafe(16)}"


def new_opaque_credential(prefix: str) -> str:
    """Opaque bearer-style credential (256 bits). Only its hash is persisted."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def credential_hash(value: str) -> str:
    """SHA-256 hex digest used to look up opaque credentials at rest."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_user_code() -> str:
    """RFC 8628 user code ``XXXX-XXXX`` without vowels or lookalike glyphs."""
    raw = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_user_code(value: str) -> str:
    """Uppercase, drop separators/whitespace, and re-insert the dash."""
    cleaned = "".join(ch for ch in value.upper() if ch.isalnum())
    if len(cleaned) != 8:
        return ""
    return f"{cleaned[:4]}-{cleaned[4:]}"


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


# -- Passwords (scrypt) ------------------------------------------------------

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def hash_password(password: str) -> str:
    """Return ``scrypt$N$r$p$<salt>$<dk>`` for ``password``."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str | None) -> bool:
    """Constant-time check of ``password`` against an encoded scrypt hash."""
    if not encoded:
        return False
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _unb64(parts[4])
        expected = _unb64(parts[5])
    except ValueError:
        return False
    if n > 2**20 or r > 32 or p > 16 or not expected:
        return False
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=256 * 1024 * 1024,
    )
    return hmac.compare_digest(derived, expected)


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,63}$")


def normalize_email(value: str) -> str:
    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    return len(value) <= 320 and bool(_EMAIL_RE.match(value))


# -- CSRF & Origin -----------------------------------------------------------

SESSION_COOKIE = "ss_session"
CSRF_COOKIE = "ss_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FORM_FIELD = "_csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def new_csrf_value() -> str:
    return secrets.token_urlsafe(32)


def request_origin_host(headers: Mapping[str, str]) -> str | None:
    """Host of ``Origin`` (preferred) or ``Referer``; ``None`` when absent."""
    origin = headers.get("origin", "").strip()
    if origin and origin.lower() != "null":
        return urlsplit(origin).netloc.lower() or None
    referer = headers.get("referer", "").strip()
    if referer:
        return urlsplit(referer).netloc.lower() or None
    return None


def origin_allowed(headers: Mapping[str, str], allowed_hosts: Iterable[str]) -> bool:
    """True when the browser-declared origin host is one we serve.

    A cookie-authenticated write without ``Origin``/``Referer`` is rejected:
    modern browsers always send ``Origin`` on cross-site and same-site POSTs.
    """
    host = request_origin_host(headers)
    if host is None:
        return False
    return host in {item.lower() for item in allowed_hosts}


def csrf_matches(cookie_value: str | None, submitted: str | None) -> bool:
    if not cookie_value or not submitted:
        return False
    return constant_time_equals(cookie_value, submitted)


def safe_next_path(value: str | None, default: str = "/dashboard") -> str:
    """Only allow same-site relative redirects (``/path?query``)."""
    if not value:
        return default
    candidate = value.strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return default
    if any(ord(ch) < 32 for ch in candidate):
        return default
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return default
    return candidate


# -- Filenames ---------------------------------------------------------------

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str | None, default: str = "upload.bin", *, max_length: int = 160) -> str:
    """Reduce a client filename to a harmless basename.

    ``Path(name).name`` strips directories (``../../x`` -> ``x``); backslash
    separators are normalised first so Windows-style paths cannot smuggle a
    parent segment; control characters and exotic punctuation are dropped.
    """
    if not name:
        return default
    candidate = Path(name.replace("\\", "/")).name
    candidate = unicodedata.normalize("NFKC", candidate)
    candidate = "".join(ch for ch in candidate if ch.isprintable())
    candidate = _FILENAME_SAFE_RE.sub("_", candidate).strip(" .")
    if not candidate or candidate in {".", ".."}:
        return default
    if len(candidate) > max_length:
        stem, dot, ext = candidate.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            candidate = f"{stem[: max_length - len(ext) - 1]}.{ext}"
        else:
            candidate = candidate[:max_length]
    return candidate


def safe_child(base: Path, name: str) -> Path | None:
    """Resolve a single basename inside ``base`` or return ``None``."""
    if not name or name != Path(name).name or name in {".", ".."} or "\\" in name:
        return None
    root = base.resolve()
    candidate = (root / name).resolve()
    if not candidate.is_relative_to(root) or candidate == root:
        return None
    return candidate


# -- Archive extraction ------------------------------------------------------

ArchiveKind = Literal["tar", "zip"]
_COPY_CHUNK = 64 * 1024
_MAX_MEMBER_DEPTH = 12
_MAX_PART_LENGTH = 255


class ArchiveRejected(ValueError):
    """The uploaded bundle is unsafe or exceeds limits."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ExtractedArchive:
    root: Path
    files: tuple[Path, ...]
    total_bytes: int


def sniff_archive_kind(path: Path) -> ArchiveKind | None:
    """Detect zip / tar / tar.gz by magic bytes, never by client filename."""
    with path.open("rb") as handle:
        head = handle.read(512)
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "zip"
    if head.startswith(b"\x1f\x8b"):
        return "tar"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return None


def normalize_member_name(name: str) -> PurePosixPath | None:
    """Validate an archive member name; ``None`` means "skip" (e.g. ``./``).

    Raises :class:`ArchiveRejected` for absolute paths, parent segments,
    backslashes, drive letters, NUL bytes, or absurd depth.
    """
    if "\x00" in name or "\\" in name:
        raise ArchiveRejected("Archive member name contains forbidden characters.")
    if name.startswith("/"):
        raise ArchiveRejected("Archive member uses an absolute path.")
    parts = [part for part in name.split("/") if part not in {"", "."}]
    if not parts:
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        raise ArchiveRejected("Archive member uses a drive-letter path.")
    for part in parts:
        if part == "..":
            raise ArchiveRejected("Archive member escapes the bundle root.")
        if len(part) > _MAX_PART_LENGTH or any(ord(ch) < 32 for ch in part):
            raise ArchiveRejected("Archive member name is invalid.")
    if len(parts) > _MAX_MEMBER_DEPTH:
        raise ArchiveRejected("Archive member nesting is too deep.")
    return PurePosixPath(*parts)


def _target_for(dest_root: Path, relative: PurePosixPath) -> Path:
    target = (dest_root / Path(*relative.parts)).resolve()
    if not target.is_relative_to(dest_root) or target == dest_root:
        raise ArchiveRejected("Archive member escapes the bundle root.")
    return target


def _copy_capped(source: IO[bytes], target: Path, *, budget: int) -> int:
    """Copy ``source`` into ``target``; raise 413 once ``budget`` is exceeded."""
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as out:
        while True:
            chunk = source.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > budget:
                raise ArchiveRejected(
                    "Bundle exceeds the uncompressed size limit.", status_code=413
                )
            out.write(chunk)
    return written


def _extract_tar(
    archive: Path, dest_root: Path, *, max_members: int, max_total_bytes: int
) -> tuple[list[Path], int]:
    files: list[Path] = []
    total = 0
    count = 0
    try:
        with tarfile.open(archive, mode="r:*") as bundle:
            while True:
                member = bundle.next()
                if member is None:
                    break
                count += 1
                if count > max_members:
                    raise ArchiveRejected("Bundle has too many entries.", status_code=413)
                if member.issym() or member.islnk():
                    raise ArchiveRejected("Bundle must not contain links.")
                if member.isdev() or member.ischr() or member.isblk() or member.isfifo():
                    raise ArchiveRejected("Bundle must not contain device or FIFO entries.")
                relative = normalize_member_name(member.name)
                if relative is None:
                    continue
                if member.isdir():
                    _target_for(dest_root, relative).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ArchiveRejected("Bundle contains an unsupported entry type.")
                if member.size < 0 or total + member.size > max_total_bytes:
                    raise ArchiveRejected(
                        "Bundle exceeds the uncompressed size limit.", status_code=413
                    )
                source = bundle.extractfile(member)
                if source is None:
                    raise ArchiveRejected("Bundle entry could not be read.")
                target = _target_for(dest_root, relative)
                if target.exists():
                    raise ArchiveRejected("Bundle contains duplicate entries.")
                with source:
                    total += _copy_capped(source, target, budget=max_total_bytes - total)
                files.append(target)
    except (tarfile.TarError, EOFError, OSError) as exc:
        if isinstance(exc, FileExistsError):
            raise ArchiveRejected("Bundle contains duplicate entries.") from exc
        raise ArchiveRejected("Bundle is not a readable tar archive.") from exc
    return files, total


def _extract_zip(
    archive: Path, dest_root: Path, *, max_members: int, max_total_bytes: int
) -> tuple[list[Path], int]:
    files: list[Path] = []
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            if len(infos) > max_members:
                raise ArchiveRejected("Bundle has too many entries.", status_code=413)
            declared = sum(max(0, info.file_size) for info in infos)
            if declared > max_total_bytes:
                raise ArchiveRejected(
                    "Bundle exceeds the uncompressed size limit.", status_code=413
                )
            for info in infos:
                if info.flag_bits & 0x1:
                    raise ArchiveRejected("Encrypted bundle entries are not supported.")
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type == stat.S_IFLNK:
                    raise ArchiveRejected("Bundle must not contain links.")
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ArchiveRejected("Bundle must not contain device or FIFO entries.")
                relative = normalize_member_name(info.filename)
                if relative is None:
                    continue
                if info.is_dir() or file_type == stat.S_IFDIR:
                    _target_for(dest_root, relative).mkdir(parents=True, exist_ok=True)
                    continue
                target = _target_for(dest_root, relative)
                if target.exists():
                    raise ArchiveRejected("Bundle contains duplicate entries.")
                with bundle.open(info, "r") as source:
                    total += _copy_capped(source, target, budget=max_total_bytes - total)
                files.append(target)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError, RuntimeError) as exc:
        if isinstance(exc, FileExistsError):
            raise ArchiveRejected("Bundle contains duplicate entries.") from exc
        raise ArchiveRejected("Bundle is not a readable zip archive.") from exc
    return files, total


def safe_extract_archive(
    archive: Path,
    dest: Path,
    *,
    max_members: int,
    max_total_bytes: int,
) -> ExtractedArchive:
    """Extract a tar / tar.gz / zip bundle into ``dest`` defensively.

    Rejects (400) absolute or parent-escaping names, backslashes, drive
    letters, symlinks, hardlinks, devices, FIFOs and encrypted entries; rejects
    (413) too many members or too many uncompressed bytes (counted while
    copying, so lying headers cannot bypass the cap). On any rejection the
    partially-extracted tree is removed.
    """
    kind = sniff_archive_kind(archive)
    if kind is None:
        raise ArchiveRejected("Bundle must be a .tar, .tar.gz/.tgz, or .zip archive.")
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    try:
        if kind == "zip":
            files, total = _extract_zip(
                archive, dest_root, max_members=max_members, max_total_bytes=max_total_bytes
            )
        else:
            files, total = _extract_tar(
                archive, dest_root, max_members=max_members, max_total_bytes=max_total_bytes
            )
    except ArchiveRejected:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise
    return ExtractedArchive(root=dest_root, files=tuple(files), total_bytes=total)


__all__ = [
    "ACCESS_PREFIX",
    "CSRF_COOKIE",
    "CSRF_FORM_FIELD",
    "CSRF_HEADER",
    "DEVICE_PREFIX",
    "JOB_ID_RE",
    "JOB_PREFIX",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "REFRESH_PREFIX",
    "REVIEW_PREFIX",
    "SAFE_METHODS",
    "SESSION_COOKIE",
    "SHARE_ID_RE",
    "SHARE_PREFIX",
    "USER_PREFIX",
    "WEB_PREFIX",
    "ArchiveRejected",
    "ExtractedArchive",
    "constant_time_equals",
    "credential_hash",
    "csrf_matches",
    "from_epoch",
    "hash_password",
    "is_valid_email",
    "isoformat_z",
    "new_csrf_value",
    "new_id",
    "new_opaque_credential",
    "new_share_id",
    "new_user_code",
    "normalize_email",
    "normalize_member_name",
    "normalize_user_code",
    "origin_allowed",
    "parse_iso",
    "request_origin_host",
    "safe_child",
    "safe_extract_archive",
    "safe_filename",
    "safe_next_path",
    "sniff_archive_kind",
    "utc_now",
    "verify_password",
]
