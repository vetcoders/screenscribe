"""Transcript corrections ledger.

Original transcript bytes stay immutable. A correction is an entry in
``<output_dir>/corrections.json``. Readers apply it as an overlay.
``scope: "global"`` entries also roll up into the user glossary at
``~/.config/screenscribe/corrections_glossary.json``.

B-01 and Z-01 should call :func:`corrections_summary`. Pass ``limit=None``
for the full effective set (report ledger plus glossary). The default cap
of 20 is the agent-context seed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "corrections.json"
GLOSSARY_FILENAME = "corrections_glossary.json"
LEDGER_VERSION = 1
KIND_TRANSCRIPT = "transcript"
SCOPES: tuple[str, ...] = ("report", "global")
SOURCES: tuple[str, ...] = ("user", "agent")

_ENTRY_ID = re.compile(r"^cor-[0-9a-fA-F-]{8,64}$")
# A correction is a phrase, not a paragraph: long entries bloat every prompt
# and can push other entries out of the byte-capped glossary block.
_MAX_PHRASE_CHARS = 200
_IO_LOCK = threading.Lock()


class CorrectionEntry(TypedDict):
    id: str
    kind: str
    match: str
    replacement: str
    scope: str
    source: str
    note: str
    created_at: str


class CorrectionRejected(ValueError):
    """The caller asked for a correction the ledger will not store."""


@dataclass(frozen=True)
class LedgerLoad:
    """What a ledger read produced.

    ``warning`` is set when the file could not be trusted. Callers degrade
    to ``entries`` (often empty) and must not turn that into a 500.
    """

    entries: list[CorrectionEntry]
    warning: str | None
    report: str


@dataclass(frozen=True)
class CorrectionPiece:
    """One slice of overlay text.

    ``original`` is the transcript slice that was replaced, or ``None`` when
    ``text`` is unchanged source. The UI puts ``original`` on the tooltip.
    """

    text: str
    original: str | None


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    replacement: str
    created_at: str
    order: int


def default_glossary_dir() -> Path:
    """User-level screenscribe config directory (same place as ``config.env``)."""
    return Path.home() / ".config" / "screenscribe"


def ledger_path(output_dir: str | Path) -> Path:
    return Path(output_dir) / LEDGER_FILENAME


def glossary_file(glossary_dir: Path | None = None) -> Path:
    root = glossary_dir if glossary_dir is not None else default_glossary_dir()
    return root / GLOSSARY_FILENAME


def utc_now_stamp() -> str:
    """UTC timestamp matching the ledger schema (``2026-09-28T21:00:00Z``)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_correction_fields(
    match: object,
    replacement: object,
    scope: object,
    note: object = None,
    source: object = "user",
) -> tuple[str, str, str, str, str]:
    """Return stripped ``(match, replacement, scope, note, source)`` or raise.

    ``match`` is stored and applied as a literal phrase. Regex metacharacters
    are not interpreted. ``replacement`` may be empty (delete the phrase).
    """
    if not isinstance(match, str):
        raise CorrectionRejected("match must be a string")
    phrase = match.strip()
    if not phrase:
        raise CorrectionRejected("match must be a non-empty literal phrase")
    if len(phrase) > _MAX_PHRASE_CHARS:
        raise CorrectionRejected(f"match is longer than {_MAX_PHRASE_CHARS} characters")
    if not isinstance(replacement, str):
        raise CorrectionRejected("replacement must be a string")
    if len(replacement) > _MAX_PHRASE_CHARS:
        raise CorrectionRejected(f"replacement is longer than {_MAX_PHRASE_CHARS} characters")
    if not isinstance(scope, str) or scope.strip() not in SCOPES:
        allowed = " or ".join(repr(item) for item in SCOPES)
        raise CorrectionRejected(f"scope must be {allowed}")
    if note is None:
        note_text = ""
    elif isinstance(note, str):
        note_text = note.strip()
    else:
        raise CorrectionRejected("note must be a string")
    if source is None:
        source_text = "user"
    elif isinstance(source, str) and source.strip() in SOURCES:
        source_text = source.strip()
    else:
        allowed = " or ".join(repr(item) for item in SOURCES)
        raise CorrectionRejected(f"source must be {allowed}")
    return phrase, replacement, scope.strip(), note_text, source_text


def load_ledger(output_dir: str | Path) -> LedgerLoad:
    """Load ``corrections.json``. Missing file is an empty ledger, not an error."""
    path = ledger_path(output_dir)
    if not path.exists():
        return LedgerLoad(entries=[], warning=None, report="")
    entries, warning, report = _read_ledger_file(path)
    return LedgerLoad(entries=entries, warning=warning, report=report)


def load_glossary(glossary_dir: Path | None = None) -> LedgerLoad:
    """Load the user glossary. Missing file is an empty ledger."""
    path = glossary_file(glossary_dir)
    if not path.exists():
        return LedgerLoad(entries=[], warning=None, report="global")
    entries, warning, report = _read_ledger_file(path)
    return LedgerLoad(entries=entries, warning=warning, report=report or "global")


def save_ledger(
    output_dir: str | Path,
    entries: Sequence[CorrectionEntry],
    *,
    report: str,
    glossary_dir: Path | None = None,
) -> Path:
    """Atomically replace the report ledger and roll global entries into the glossary."""
    path = ledger_path(output_dir)
    payload = _ledger_payload(list(entries), report)
    with _IO_LOCK:
        _write_json_atomic(path, payload)
        _roll_up_globals(list(entries), glossary_dir)
    return path


def append_correction(
    output_dir: str | Path,
    *,
    match: object,
    replacement: object,
    scope: object,
    note: object = None,
    source: object = "user",
    report_stem: str = "",
    glossary_dir: Path | None = None,
    created_at: str | None = None,
    entry_id: str | None = None,
) -> CorrectionEntry:
    """Validate, append one entry, and save.

    A broken existing ledger degrades to empty and is replaced by the new
    valid document (the read path never raises for bad JSON). Transcript
    files in ``output_dir`` are not opened.
    """
    phrase, repl, scope_text, note_text, source_text = validate_correction_fields(
        match, replacement, scope, note, source
    )
    stamp = (
        created_at.strip()
        if isinstance(created_at, str) and created_at.strip()
        else utc_now_stamp()
    )
    entry = _make_entry(
        phrase,
        repl,
        scope_text,
        note_text,
        source_text,
        created_at=stamp,
        entry_id=entry_id,
    )
    with _IO_LOCK:
        loaded_entries, warning, report = _read_existing_for_append(output_dir)
        if warning:
            logger.warning("Corrections ledger degraded before append: %s", warning)
        stored_report = report or report_stem
        for existing in loaded_entries:
            if (
                existing["match"] == phrase
                and existing["replacement"] == repl
                and existing["scope"] == scope_text
                and existing["source"] == source_text
                and existing["note"] == note_text
            ):
                # Idempotent retry: a previous attempt already stored this
                # correction (e.g. the ledger write landed but the glossary
                # roll-up failed, or an agent turn was retried). Re-running
                # the roll-up heals a missing global entry without
                # duplicating the ledger row.
                _roll_up_globals_degraded(loaded_entries, glossary_dir)
                return existing
        loaded_entries.append(entry)
        path = ledger_path(output_dir)
        _write_json_atomic(path, _ledger_payload(loaded_entries, stored_report))
        _roll_up_globals_degraded(loaded_entries, glossary_dir)
    return entry


def corrections_summary(
    output_dir: str | Path,
    *,
    limit: int | None = 20,
    glossary_dir: Path | None = None,
) -> dict[str, Any]:
    """Effective corrections for context seeding and pipeline re-runs.

    Signature::

        corrections_summary(output_dir, *, limit=20, glossary_dir=None) -> dict

    Merges ``corrections.json`` in ``output_dir`` with the user glossary.
    Dedup key is ``(match, replacement)``; the newest ``created_at`` wins.
    A report entry beats a glossary entry when timestamps tie.

    Keys:
        ``count`` — effective entries before the cap.
        ``entries`` — ``{id, kind, match, replacement, scope, source, note, created_at}``.
        ``truncated`` — true when ``limit`` dropped older entries.
        ``warning`` — set when either file was unreadable; never raises.

    ``limit=None`` returns every effective entry (B-01). The default ``20``
    is the agent-context cap (newest entries kept, still in ``created_at`` order).
    """
    report_load = load_ledger(output_dir)
    glossary_load = load_glossary(glossary_dir)
    warnings = [item for item in (report_load.warning, glossary_load.warning) if item]
    report_ids = {entry["id"] for entry in report_load.entries}
    merged = _merge_effective(glossary_load.entries, report_load.entries, report_ids)
    ordered = sorted(merged, key=lambda item: (item["created_at"], item["id"]))
    count = len(ordered)
    if limit is None:
        shown = ordered
        truncated = False
    else:
        cap = max(0, limit)
        truncated = count > cap
        shown = ordered[-cap:] if cap else []
    warning = "; ".join(warnings) if warnings else None
    return {
        "count": count,
        "entries": [dict(entry) for entry in shown],
        "truncated": truncated,
        "warning": warning,
    }


def correction_pieces(
    text: str,
    entries: Sequence[Mapping[str, Any]],
) -> list[CorrectionPiece]:
    """Split ``text`` into plain and replaced slices.

    Matching is case-insensitive, whole-phrase, and literal (no regex).
    Entries apply in ``created_at`` order; a later entry wins on overlap.
    """
    spans = _resolve_spans(text, entries)
    pieces: list[CorrectionPiece] = []
    cursor = 0
    for span in spans:
        if span.start > cursor:
            pieces.append(CorrectionPiece(text[cursor : span.start], None))
        pieces.append(CorrectionPiece(span.replacement, text[span.start : span.end]))
        cursor = span.end
    if cursor < len(text) or not pieces:
        pieces.append(CorrectionPiece(text[cursor:], None))
    return pieces


def apply_transcript_overlay(text: str, entries: Sequence[Mapping[str, Any]]) -> str:
    """Return ``text`` with ledger entries applied. The input is not mutated."""
    return "".join(piece.text for piece in correction_pieces(text, entries))


def _make_entry(
    match: str,
    replacement: str,
    scope: str,
    note: str,
    source: str,
    *,
    created_at: str,
    entry_id: str | None,
) -> CorrectionEntry:
    if entry_id and _ENTRY_ID.fullmatch(entry_id):
        chosen = entry_id
    else:
        chosen = f"cor-{uuid.uuid4()}"
    return CorrectionEntry(
        id=chosen,
        kind=KIND_TRANSCRIPT,
        match=match,
        replacement=replacement,
        scope=scope,
        source=source,
        note=note,
        created_at=created_at,
    )


def _ledger_payload(entries: list[CorrectionEntry], report: str) -> dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "report": report,
        "entries": [_public_entry(entry) for entry in entries],
    }


def _public_entry(entry: CorrectionEntry) -> CorrectionEntry:
    return CorrectionEntry(
        id=entry["id"],
        kind=entry["kind"],
        match=entry["match"],
        replacement=entry["replacement"],
        scope=entry["scope"],
        source=entry["source"],
        note=entry["note"],
        created_at=entry["created_at"],
    )


def _read_existing_for_append(
    output_dir: str | Path,
) -> tuple[list[CorrectionEntry], str | None, str]:
    path = ledger_path(output_dir)
    if not path.exists():
        return [], None, ""
    return _read_ledger_file(path)


def _read_ledger_file(path: Path) -> tuple[list[CorrectionEntry], str | None, str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [], f"{path.name} could not be read", ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return [], f"{path.name} is not valid JSON", ""
    if not isinstance(parsed, dict):
        return [], f"{path.name} is not a corrections object", ""
    version = parsed.get("version", LEDGER_VERSION)
    if version != LEDGER_VERSION:
        return [], f"{path.name} version {version!r} is not supported", ""
    report = parsed.get("report")
    report_text = report if isinstance(report, str) else ""
    raw_entries = parsed.get("entries")
    if raw_entries is None:
        raw_entries = []
    if not isinstance(raw_entries, list):
        return [], f"{path.name} entries must be a list", report_text
    kept: list[CorrectionEntry] = []
    rejected = 0
    for item in raw_entries:
        parsed_entry = _parse_stored_entry(item)
        if parsed_entry is None:
            rejected += 1
            continue
        kept.append(parsed_entry)
    warning = None
    if rejected:
        warning = f"rejected {rejected} invalid corrections ledger entries"
    return kept, warning, report_text


def _parse_stored_entry(item: object) -> CorrectionEntry | None:
    if not isinstance(item, dict):
        return None
    try:
        match, replacement, scope, note, source = validate_correction_fields(
            item.get("match"),
            item.get("replacement"),
            item.get("scope"),
            item.get("note", ""),
            item.get("source", "user"),
        )
    except CorrectionRejected:
        return None
    kind = item.get("kind", KIND_TRANSCRIPT)
    if kind != KIND_TRANSCRIPT:
        return None
    entry_id = item.get("id")
    created_at = item.get("created_at")
    if not isinstance(entry_id, str) or not _ENTRY_ID.fullmatch(entry_id):
        return None
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    return _make_entry(
        match,
        replacement,
        scope,
        note,
        source,
        created_at=created_at.strip(),
        entry_id=entry_id,
    )


def _roll_up_globals_degraded(
    entries: list[CorrectionEntry], glossary_dir: Path | None
) -> None:
    """Roll up globals; a failed glossary write degrades instead of raising.

    By this point the report ledger already holds the entry, so raising would
    report the whole append as failed and invite a retry that used to append
    a duplicate row. The glossary self-heals: every save rolls up ALL global
    entries of the ledger, so the next successful write catches up.
    """
    try:
        _roll_up_globals(entries, glossary_dir)
    except OSError as exc:
        logger.warning(
            "Correction saved to the report ledger, but the global glossary "
            "write failed (will catch up on the next save): %s",
            exc,
        )


def _roll_up_globals(entries: list[CorrectionEntry], glossary_dir: Path | None) -> None:
    incoming = [entry for entry in entries if entry["scope"] == "global"]
    if not incoming:
        return
    path = glossary_file(glossary_dir)
    if path.exists():
        existing, warning, _report = _read_ledger_file(path)
        if warning:
            logger.warning("Global corrections glossary degraded before merge: %s", warning)
    else:
        existing = []
    merged = merge_glossary_entries(existing, incoming)
    _ensure_private_dir(path.parent)
    _write_json_atomic(path, _ledger_payload(merged, "global"))


def merge_glossary_entries(
    existing: Sequence[CorrectionEntry],
    incoming: Sequence[CorrectionEntry],
) -> list[CorrectionEntry]:
    """Dedup by ``(match, replacement)``. Newest ``created_at`` wins."""
    best: dict[tuple[str, str], CorrectionEntry] = {}
    for entry in [*existing, *incoming]:
        key = (entry["match"], entry["replacement"])
        current = best.get(key)
        if current is None or _newer(entry, current):
            best[key] = entry
    return sorted(best.values(), key=lambda item: (item["created_at"], item["id"]))


def _merge_effective(
    glossary_entries: Sequence[CorrectionEntry],
    report_entries: Sequence[CorrectionEntry],
    report_ids: set[str],
) -> list[CorrectionEntry]:
    best: dict[tuple[str, str], CorrectionEntry] = {}

    def rank(entry: CorrectionEntry) -> tuple[str, int, str]:
        local = 1 if entry["id"] in report_ids else 0
        return (entry["created_at"], local, entry["id"])

    for entry in [*glossary_entries, *report_entries]:
        key = (entry["match"], entry["replacement"])
        current = best.get(key)
        if current is None or rank(entry) >= rank(current):
            best[key] = entry
    return list(best.values())


def _newer(left: CorrectionEntry, right: CorrectionEntry) -> bool:
    return (left["created_at"], left["id"]) >= (right["created_at"], right["id"])


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        return


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Tempfile + ``os.replace`` + fsync, same durability pattern as report JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".corrections-", suffix=".json.tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _boundary_before(text: str, index: int) -> bool:
    if index <= 0:
        return True
    return not _is_word_char(text[index - 1])


def _boundary_after(text: str, index: int) -> bool:
    if index >= len(text):
        return True
    return not _is_word_char(text[index])


def _find_phrase_spans(text: str, phrase: str) -> list[tuple[int, int]]:
    """Literal case-insensitive whole-phrase spans. ``phrase`` is not a regex."""
    if phrase == "":
        return []
    folded = phrase.casefold()
    target = len(folded)
    spans: list[tuple[int, int]] = []
    length = len(text)
    index = 0
    while index < length:
        if not _boundary_before(text, index):
            index += 1
            continue
        matched_end: int | None = None
        end = index
        while end < length:
            end += 1
            folded_slice = text[index:end].casefold()
            if folded_slice == folded and _boundary_after(text, end):
                matched_end = end
                break
            if len(folded_slice) > target:
                break
        if matched_end is None:
            index += 1
            continue
        spans.append((index, matched_end))
        index = matched_end
    return spans


def _resolve_spans(text: str, entries: Sequence[Mapping[str, Any]]) -> list[_Span]:
    candidates: list[_Span] = []
    for order, entry in enumerate(entries):
        match = entry.get("match")
        replacement = entry.get("replacement")
        if not isinstance(match, str) or match == "" or not isinstance(replacement, str):
            continue
        created = entry.get("created_at")
        created_at = created if isinstance(created, str) else ""
        for start, end in _find_phrase_spans(text, match):
            candidates.append(_Span(start, end, replacement, created_at, order))
    # Later created_at (then later file order) claims an overlapping region first.
    candidates.sort(key=lambda span: (span.created_at, span.order), reverse=True)
    chosen: list[_Span] = []
    for span in candidates:
        if any(span.start < other.end and other.start < span.end for other in chosen):
            continue
        chosen.append(span)
    chosen.sort(key=lambda span: span.start)
    return chosen


__all__ = [
    "CorrectionEntry",
    "CorrectionPiece",
    "CorrectionRejected",
    "LedgerLoad",
    "append_correction",
    "apply_transcript_overlay",
    "correction_pieces",
    "corrections_summary",
    "default_glossary_dir",
    "glossary_file",
    "ledger_path",
    "load_glossary",
    "load_ledger",
    "merge_glossary_entries",
    "save_ledger",
    "utc_now_stamp",
    "validate_correction_fields",
]
