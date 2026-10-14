"""Screenscribe-local tools the review agent may call.

Read tools inspect the loaded report. Write tools do **not** touch
``report.json``: they validate against the report and return a ``review_patch``
(or a ``review_plan``). The browser is the single writer of review state.
"""

from __future__ import annotations

import base64
import json
import logging
import math
from pathlib import Path
from typing import Any

from ..work_item import VERDICT_VALUES
from .corrections import CorrectionRejected, append_correction, validate_correction_fields

logger = logging.getLogger(__name__)

_MAX_OPEN_FILE_BYTES = 80_000
_MAX_TRANSCRIPT_HITS = 80
# view_frame: real pixels for the model. A frame is a full-resolution screenshot
# (~1-2 MB JPEG); the per-call and per-turn caps keep the provider payload bounded,
# especially on stateless providers that resend the whole turn every tool round.
_MAX_FRAME_BYTES = 8_000_000
_MAX_FRAMES_PER_CALL = 4
_MAX_FRAMES_PER_TURN = 8
_MAX_DETAIL_STRING_CHARS = 6_000
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_SCREENSHOTS_DIRNAME = "screenshots"
_SAFE_REPO_FILE_SUFFIXES = frozenset(
    {
        ".bash",
        ".c",
        ".cc",
        ".cfg",
        ".cjs",
        ".cpp",
        ".css",
        ".fish",
        ".go",
        ".graphql",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".mjs",
        ".proto",
        ".py",
        ".pyi",
        ".rs",
        ".rst",
        ".scss",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
        ".zsh",
    }
)
_SAFE_REPO_FILE_NAMES = frozenset({"dockerfile", "license", "makefile", "notice", "readme"})
_SENSITIVE_REPO_NAME_PARTS = ("credential", "secret", "token")
_SENSITIVE_REPO_SUFFIXES = frozenset({".der", ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"})

SEVERITY_VALUES: tuple[str, ...] = ("critical", "high", "medium", "low", "none")
EDIT_FIELD_KEYS: tuple[str, ...] = ("summary", "category", "notes", "action_items")
_EDIT_FIELD_TO_PATCH: dict[str, str] = {
    "summary": "summary_override",
    "category": "category_override",
    "notes": "notes",
    "action_items": "action_items",
    "summary_override": "summary_override",
    "category_override": "category_override",
}

READ_TOOL_NAMES = (
    "list_findings",
    "get_finding",
    "view_frame",
    "get_transcript",
    "seek",
    "show_frame",
    "get_report_summary",
    "open_repo_file",
)
WRITE_TOOL_NAMES = (
    "set_verdict",
    "set_severity",
    "edit_finding",
    "add_finding",
    "merge_findings",
    "unmerge_finding",
    "propose_review",
    "correct_transcript",
)
TOOL_NAMES = READ_TOOL_NAMES + WRITE_TOOL_NAMES

_MERGE_STUB: dict[str, Any] = {
    "unsupported": True,
    "reason": (
        "merge/unmerge is a client-only fold (mergeFindings / unmergeFindingGroup "
        "in review_app.js). There is no patch-callable HTTP API; applying it from "
        "the agent would invent client internals. W2-02 can replace this stub."
    ),
}


def responses_tool_schemas(*, include_repo: bool) -> list[dict[str, Any]]:
    """Responses API ``tools`` list (function tools)."""
    tools = [
        _function_tool(
            "list_findings",
            "List findings from the current review. Optional filter matches "
            "severity, category, id, or text (case-insensitive substring).",
            {
                "filter": {
                    "type": "string",
                    "description": "Optional substring filter, e.g. 'critical' or 'layout'.",
                }
            },
        ),
        _function_tool(
            "get_finding",
            "Return the complete record of one finding: narration text and context, "
            "the full visual analysis (ui_elements, issues_detected, "
            "accessibility_notes, design_feedback, technical_observations, "
            "suggested_fix, action_items, affected_components) and every merged "
            "frame with its own narration and screenshot. The seed and "
            "list_findings carry only a one-line summary; call this before "
            "answering detailed questions about a finding. Also accepts a merged "
            "member id or a manual marker id.",
            {
                "finding_id": {
                    "description": "Finding id, merged member id, or manual marker id.",
                }
            },
            required=["finding_id"],
        ),
        _function_tool(
            "view_frame",
            "Look at the actual screenshot pixels yourself. The frames are attached "
            "to the conversation right after this tool result, so you can read the "
            "screen, UI state, text and errors directly instead of relying on the "
            "stored summary. Pass finding_id (a finding returns its main frame "
            "plus merged frames; a merged member id or manual marker id returns "
            "that frame), or timestamp for the nearest frame, or frame for one "
            "specific screenshot file name from get_finding. This does not change "
            "the user's UI; use show_frame for that.",
            {
                "finding_id": {
                    "description": "Finding id, merged member id, or manual marker id.",
                },
                "timestamp": {
                    "type": "number",
                    "description": "Video timestamp in seconds; picks the nearest frame.",
                },
                "frame": {
                    "type": "string",
                    "description": "Screenshot file name, e.g. 03_bug_11-59.jpg.",
                },
            },
        ),
        _function_tool(
            "get_transcript",
            "Return transcript segments overlapping [start, end] seconds. "
            "Omit both to get the start of the transcript.",
            {
                "start": {"type": "number", "description": "Start time in seconds."},
                "end": {"type": "number", "description": "End time in seconds."},
            },
        ),
        _function_tool(
            "seek",
            "Ask the review UI to seek the video to a timestamp. Returns a UI instruction.",
            {
                "timestamp": {
                    "type": "number",
                    "description": "Video timestamp in seconds.",
                }
            },
            required=["timestamp"],
        ),
        _function_tool(
            "show_frame",
            "Ask the review UI to show the frame for a finding id or timestamp.",
            {
                "finding_id": {
                    "description": "Finding id from the report.",
                },
                "timestamp": {
                    "type": "number",
                    "description": "Video timestamp in seconds (used when finding_id is omitted).",
                },
            },
        ),
        _function_tool(
            "get_report_summary",
            "Return the executive summary, severity breakdown, and analysis-pass status.",
            {},
        ),
        _function_tool(
            "set_verdict",
            "Set a finding's review verdict. Returns a review_patch; the panel applies "
            "and saves it. Do not claim the edit is already saved.",
            {
                "finding_id": {
                    "description": "Finding id from the report.",
                },
                "verdict": {
                    "type": "string",
                    "description": "accepted | rejected | none.",
                    "enum": list(VERDICT_VALUES),
                },
            },
            required=["finding_id", "verdict"],
        ),
        _function_tool(
            "set_severity",
            "Set a finding's severity override. Returns a review_patch; the panel applies "
            "and saves it. Do not claim the edit is already saved.",
            {
                "finding_id": {
                    "description": "Finding id from the report.",
                },
                "severity": {
                    "type": "string",
                    "description": "critical | high | medium | low | none.",
                    "enum": list(SEVERITY_VALUES),
                },
            },
            required=["finding_id", "severity"],
        ),
        _function_tool(
            "edit_finding",
            "Correct finding text. summary/category become reviewer overrides "
            "(summary_override, category_override). notes and action_items update "
            "the human-review fields. Returns a review_patch; the panel applies it.",
            {
                "finding_id": {
                    "description": "Finding id from the report.",
                },
                "summary": {
                    "type": "string",
                    "description": "Replacement summary (stored as summary_override).",
                },
                "category": {
                    "type": "string",
                    "description": "Replacement category (stored as category_override).",
                },
                "notes": {
                    "type": "string",
                    "description": "Reviewer notes.",
                },
                "action_items": {
                    "description": "Action items as a string or list of strings.",
                },
                "fields": {
                    "type": "object",
                    "description": "Optional nested form of summary/category/notes/action_items.",
                },
            },
            required=["finding_id"],
        ),
        _function_tool(
            "add_finding",
            "Propose a manual finding at a timestamp. The patch tells the panel to run "
            "the existing manual-mark flow; this tool does not extract a frame.",
            {
                "timestamp": {
                    "type": "number",
                    "description": "Video timestamp in seconds.",
                },
                "summary": {
                    "type": "string",
                    "description": "Short description of the issue.",
                },
                "severity": {
                    "type": "string",
                    "description": "critical | high | medium | low | none.",
                    "enum": list(SEVERITY_VALUES),
                },
                "category": {
                    "type": "string",
                    "description": "Finding category, e.g. bug or ui.",
                },
            },
            required=["timestamp", "summary", "severity", "category"],
        ),
        _function_tool(
            "merge_findings",
            "Fold member findings into a survivor. Currently a stub: merge is a "
            "client-only fold with no patch-callable API.",
            {
                "survivor_id": {"description": "Id of the finding that should remain."},
                "member_ids": {
                    "type": "array",
                    "items": {},
                    "description": "Ids of findings to absorb into the survivor.",
                },
            },
            required=["survivor_id", "member_ids"],
        ),
        _function_tool(
            "unmerge_finding",
            "Undo a human merge of a survivor finding. Currently a stub: unmerge is "
            "a client-only fold with no patch-callable API.",
            {
                "survivor_id": {"description": "Id of the merged survivor to unmerge."},
            },
            required=["survivor_id"],
        ),
        _function_tool(
            "propose_review",
            "Mentor-style plan for a broad or ambiguous request. Pass the user's "
            "instruction plus the patch ops you would apply. This tool validates "
            "them and returns a review_plan WITHOUT applying. The panel shows "
            "Apply/Cancel. Use this instead of firing many patches at once.",
            {
                "instruction": {
                    "type": "string",
                    "description": "The user's broad request, in their words.",
                },
                "ops": {
                    "type": "array",
                    "description": "Patch operations that would apply if the user confirms.",
                    "items": {"type": "object"},
                },
                "rationale": {
                    "type": "array",
                    "description": "One-line rationale per op, same order as ops.",
                    "items": {"type": "string"},
                },
            },
            required=["instruction", "ops"],
        ),
        _function_tool(
            "correct_transcript",
            "Record a transcript-recognition correction. The original transcript "
            "file is never rewritten; the correction is a ledger overlay. match is "
            "a literal phrase, not a regex. scope is 'report' (this review only) "
            "or 'global' (also roll into the user glossary).",
            {
                "match": {
                    "type": "string",
                    "description": "Literal phrase to replace, e.g. 'cloud'. Not a regex.",
                },
                "replacement": {
                    "type": "string",
                    "description": "Text to show instead, e.g. 'Claude'.",
                },
                "scope": {
                    "type": "string",
                    "description": "report | global.",
                    "enum": ["report", "global"],
                },
                "note": {
                    "type": "string",
                    "description": "Optional reason for the correction.",
                },
            },
            required=["match", "replacement", "scope"],
        ),
    ]
    if include_repo:
        tools.append(
            _function_tool(
                "open_repo_file",
                "Read an allowlisted source or documentation file from the repo root "
                "supplied programmatically to the review server. Hidden and secret-like "
                "paths are refused.",
                {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative path to a text file.",
                    }
                },
                required=["path"],
            )
        )
    return tools


def anthropic_tool_schemas(*, include_repo: bool) -> list[dict[str, Any]]:
    """Anthropic Messages ``tools`` list."""
    converted: list[dict[str, Any]] = []
    for tool in responses_tool_schemas(include_repo=include_repo):
        converted.append(
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["parameters"],
            }
        )
    return converted


class ReportToolbelt:
    """Executes agent tools against one loaded report JSON."""

    def __init__(
        self,
        report: dict[str, Any],
        *,
        output_dir: Path | None = None,
        repo_root: Path | None = None,
        report_stem: str | None = None,
        glossary_dir: Path | None = None,
    ) -> None:
        self.report = report
        self.output_dir = output_dir
        self.repo_root = repo_root.resolve() if repo_root is not None else None
        self.report_stem = report_stem or ""
        self.glossary_dir = glossary_dir
        self._pending_images: list[dict[str, str]] = []
        self._frames_attached = 0

    def drain_images(self) -> list[dict[str, str]]:
        """Return and clear frames loaded by ``view_frame`` since the last drain.

        Each entry is ``{"media_type", "data" (base64), "label"}``. The chat loop
        attaches them to the provider conversation; they never enter the SSE
        stream or the JSON tool result.
        """
        images, self._pending_images = self._pending_images, []
        return images

    def execute(self, name: str, arguments: dict[str, Any] | None) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "list_findings":
                result = self.list_findings(str(args.get("filter") or ""))
            elif name == "get_finding":
                result = self.get_finding(args.get("finding_id"))
            elif name == "view_frame":
                result = self.view_frame(
                    finding_id=args.get("finding_id"),
                    timestamp=args.get("timestamp"),
                    frame=args.get("frame"),
                )
            elif name == "get_transcript":
                result = self.get_transcript(args.get("start"), args.get("end"))
            elif name == "seek":
                result = self.seek(args.get("timestamp"))
            elif name == "show_frame":
                result = self.show_frame(
                    finding_id=args.get("finding_id"),
                    timestamp=args.get("timestamp"),
                )
            elif name == "get_report_summary":
                result = self.get_report_summary()
            elif name == "open_repo_file":
                result = self.open_repo_file(str(args.get("path") or ""))
            elif name == "set_verdict":
                result = self.set_verdict(args.get("finding_id"), args.get("verdict"))
            elif name == "set_severity":
                result = self.set_severity(args.get("finding_id"), args.get("severity"))
            elif name == "edit_finding":
                result = self.edit_finding(args.get("finding_id"), args)
            elif name == "add_finding":
                result = self.add_finding(
                    args.get("timestamp"),
                    args.get("summary"),
                    args.get("severity"),
                    args.get("category"),
                )
            elif name == "merge_findings":
                result = self.merge_findings(args.get("survivor_id"), args.get("member_ids"))
            elif name == "unmerge_finding":
                result = self.unmerge_finding(args.get("survivor_id"))
            elif name == "propose_review":
                result = self.propose_review(
                    args.get("instruction"),
                    args.get("ops"),
                    args.get("rationale"),
                )
            elif name == "correct_transcript":
                result = self.correct_transcript(
                    args.get("match"),
                    args.get("replacement"),
                    args.get("scope"),
                    args.get("note"),
                )
            else:
                result = {"error": f"Unknown tool: {name}"}
        except Exception as exc:
            logger.warning("Agent tool %s failed: %s", name, exc)
            result = {"error": "Agent tool failed. Check server logs."}
        return json.dumps(result, ensure_ascii=False)

    def list_findings(self, filter_text: str = "") -> dict[str, Any]:
        needle = filter_text.strip().lower()
        rows: list[dict[str, Any]] = []
        for finding in self._findings():
            unified = _unified(finding)
            row = {
                "id": finding.get("id"),
                "category": finding.get("category"),
                "timestamp": finding.get("timestamp_start"),
                "timestamp_formatted": finding.get("timestamp_formatted"),
                "severity": unified.get("severity"),
                "is_issue": unified.get("is_issue"),
                "summary": unified.get("summary") or finding.get("text"),
                "screenshot": finding.get("screenshot"),
                "frame_count": len(self._finding_frames(finding)),
            }
            hay_values = [*row.values(), finding.get("text"), finding.get("context")]
            hay = " ".join(str(v) for v in hay_values if v is not None).lower()
            if needle and needle not in hay:
                continue
            rows.append(row)
        return {"count": len(rows), "findings": rows, "filter": filter_text}

    def get_transcript(self, start: object = None, end: object = None) -> dict[str, Any]:
        start_s = _as_float(start)
        end_s = _as_float(end)
        if start not in (None, "") and start_s is None:
            return {"error": "start must be a finite, non-negative timestamp"}
        if end not in (None, "") and end_s is None:
            return {"error": "end must be a finite, non-negative timestamp"}
        if start_s is not None and end_s is not None and start_s > end_s:
            return {"error": "start must be less than or equal to end"}
        hits: list[dict[str, Any]] = []
        for segment in self._segments():
            seg_start = _as_float(segment.get("start"))
            seg_end = _as_float(segment.get("end"))
            if start_s is not None and seg_end is not None and seg_end < start_s:
                continue
            if end_s is not None and seg_start is not None and seg_start > end_s:
                continue
            hits.append(
                {
                    "id": segment.get("id"),
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "text": segment.get("text"),
                }
            )
        truncated = len(hits) > _MAX_TRANSCRIPT_HITS
        return {
            "start": start_s,
            "end": end_s,
            "count": len(hits),
            "truncated": truncated,
            "segments": hits[:_MAX_TRANSCRIPT_HITS],
        }

    def seek(self, timestamp: object) -> dict[str, Any]:
        ts = _as_float(timestamp)
        if ts is None:
            return {"error": "seek requires a finite, non-negative timestamp"}
        return {"action": "seek", "timestamp": ts}

    def show_frame(self, *, finding_id: object = None, timestamp: object = None) -> dict[str, Any]:
        finding = self._finding_by_id(finding_id)
        if finding is None:
            ts = _as_float(timestamp)
            if ts is not None:
                finding = self._finding_nearest(ts)
        if finding is None:
            return {"error": "No finding matched finding_id or timestamp."}
        ts = _as_float(finding.get("timestamp_start"))
        screenshot = finding.get("screenshot")
        return {
            "action": "show_frame",
            "finding_id": finding.get("id"),
            "timestamp": ts,
            "screenshot": screenshot,
        }

    def get_finding(self, finding_id: object) -> dict[str, Any]:
        target = self._frame_target(finding_id)
        if target is None:
            return _unknown_or_missing_finding(finding_id)
        kind, record, parent = target
        if kind == "manual_marker":
            detail: dict[str, Any] = dict(_detail_copy(record))
            detail["kind"] = "manual_marker"
            detail["frames"] = [
                {"frame": record.get("frame_path"), "timestamp": record.get("timestamp")}
            ]
            return detail
        finding = parent if parent is not None else record
        detail = dict(_detail_copy(finding))
        detail["kind"] = "finding"
        if parent is not None:
            detail["matched_member_id"] = record.get("id")
        detail["frames"] = [
            {
                "frame": frame["ref"],
                "timestamp": frame["timestamp"],
                "member_id": frame["member_id"],
                "text": frame["text"],
            }
            for frame in self._finding_frames(finding)
        ]
        return detail

    def view_frame(
        self,
        *,
        finding_id: object = None,
        timestamp: object = None,
        frame: object = None,
    ) -> dict[str, Any]:
        if self.output_dir is None:
            return {"error": "view_frame is unavailable: the report directory is unknown."}
        frames = self._select_frames(finding_id, timestamp, frame)
        if isinstance(frames, dict):
            return frames
        rows: list[dict[str, Any]] = []
        attached = 0
        for entry in frames[:_MAX_FRAMES_PER_CALL]:
            row: dict[str, Any] = {
                "frame": entry["ref"],
                "timestamp": entry["timestamp"],
                "label": entry["label"],
            }
            if self._frames_attached >= _MAX_FRAMES_PER_TURN:
                row["attached"] = False
                row["error"] = f"frame budget for this turn ({_MAX_FRAMES_PER_TURN}) is used up"
                rows.append(row)
                continue
            loaded = self._load_frame(entry["ref"])
            if isinstance(loaded, str):
                row["attached"] = False
                row["error"] = loaded
            else:
                media_type, raw = loaded
                self._pending_images.append(
                    {
                        "media_type": media_type,
                        "data": base64.b64encode(raw).decode("ascii"),
                        "label": entry["label"],
                    }
                )
                self._frames_attached += 1
                attached += 1
                row["attached"] = True
            rows.append(row)
        result: dict[str, Any] = {"attached": attached, "frames": rows}
        if len(frames) > _MAX_FRAMES_PER_CALL:
            result["omitted"] = len(frames) - _MAX_FRAMES_PER_CALL
            result["hint"] = "Call view_frame with frame=<name> for the remaining frames."
        if attached:
            result["note"] = (
                "The attached images follow this tool result in the same order as "
                "frames[]. Base your answer on what is visible in them."
            )
        return result

    def get_report_summary(self) -> dict[str, Any]:
        passes = self.report.get("analysis_passes")
        return {
            "video": self.report.get("video"),
            "executive_summary": self.report.get("executive_summary") or "",
            "severity_breakdown": self.report.get("severity_breakdown") or {},
            "summary": self.report.get("summary") or {},
            "analysis_passes": passes if isinstance(passes, dict) else {},
            "finding_count": len(self._findings()),
        }

    def open_repo_file(self, path: str) -> dict[str, Any]:
        if self.repo_root is None:
            return {
                "error": "open_repo_file is unavailable because the review server "
                "was not started with a repo root."
            }
        raw = (path or "").strip()
        if not raw:
            return {"error": "path is required"}
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.repo_root / candidate).resolve()
        try:
            relative = resolved.relative_to(self.repo_root)
        except ValueError:
            return {"error": "path escapes the repo root"}
        if not resolved.is_file():
            return {"error": "not a file"}
        if _repo_path_is_sensitive(relative):
            return {"error": "path is not an allowlisted source or documentation file"}
        size = resolved.stat().st_size
        if size > _MAX_OPEN_FILE_BYTES:
            return {"error": f"file too large ({size} bytes)"}
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return {"path": str(relative), "content": text}

    def set_verdict(self, finding_id: object, verdict: object) -> dict[str, Any]:
        fid = self._require_finding_id(finding_id)
        if fid is None:
            return _unknown_or_missing_finding(finding_id)
        checked = _require_verdict(verdict)
        if "error" in checked:
            return checked
        return _review_patch(
            [{"op": "set_verdict", "finding_id": fid, "verdict": checked["verdict"]}],
            f"Set finding {fid} verdict to {checked['verdict']}.",
        )

    def set_severity(self, finding_id: object, severity: object) -> dict[str, Any]:
        fid = self._require_finding_id(finding_id)
        if fid is None:
            return _unknown_or_missing_finding(finding_id)
        checked = _require_severity(severity)
        if "error" in checked:
            return checked
        return _review_patch(
            [{"op": "set_severity", "finding_id": fid, "severity": checked["severity"]}],
            f"Set finding {fid} severity override to {checked['severity']}.",
        )

    def edit_finding(self, finding_id: object, args: dict[str, Any] | None) -> dict[str, Any]:
        fid = self._require_finding_id(finding_id)
        if fid is None:
            return _unknown_or_missing_finding(finding_id)
        fields, error = _edit_fields(args if isinstance(args, dict) else {})
        if error is not None:
            return error
        return _review_patch(
            [{"op": "edit_finding", "finding_id": fid, "fields": fields}],
            f"Edit finding {fid} text fields.",
        )

    def add_finding(
        self,
        timestamp: object,
        summary: object,
        severity: object,
        category: object,
    ) -> dict[str, Any]:
        ts = _as_float(timestamp)
        if ts is None:
            return {"error": "add_finding requires a finite, non-negative timestamp"}
        text = str(summary or "").strip()
        if not text:
            return {"error": "add_finding requires a non-empty summary"}
        checked = _require_severity(severity)
        if "error" in checked:
            return checked
        cat = str(category or "").strip()
        if not cat:
            return {"error": "add_finding requires a non-empty category"}
        return _review_patch(
            [
                {
                    "op": "add_finding",
                    "timestamp": ts,
                    "summary": text,
                    "severity": checked["severity"],
                    "category": cat,
                }
            ],
            f"Add a manual finding at {ts}s.",
        )

    def merge_findings(
        self, survivor_id: object = None, member_ids: object = None
    ) -> dict[str, Any]:
        _ = survivor_id, member_ids
        return dict(_MERGE_STUB)

    def unmerge_finding(self, survivor_id: object = None) -> dict[str, Any]:
        _ = survivor_id
        return dict(_MERGE_STUB)

    def correct_transcript(
        self,
        match: object,
        replacement: object,
        scope: object,
        note: object = None,
    ) -> dict[str, Any]:
        """Validate a literal transcript correction and return a review_patch.

        When this toolbelt has an output dir, the ledger entry is written
        before the patch returns (``persisted: true``). The patch is still
        the confirmation the panel renders. Transcript files are not modified.
        """
        try:
            phrase, repl, scope_text, note_text, _source = validate_correction_fields(
                match, replacement, scope, note, "agent"
            )
        except CorrectionRejected as exc:
            return {"error": str(exc)}
        op: dict[str, Any] = {
            "op": "correct_transcript",
            "match": phrase,
            "replacement": repl,
            "scope": scope_text,
            "note": note_text,
            "persisted": False,
        }
        explain = f"Transcript correction: {phrase!r} → {repl!r} (scope {scope_text})."
        if self.output_dir is None:
            return _review_patch([op], explain)
        try:
            entry = append_correction(
                self.output_dir,
                match=phrase,
                replacement=repl,
                scope=scope_text,
                note=note_text,
                source="agent",
                report_stem=self.report_stem,
                glossary_dir=self.glossary_dir,
            )
        except CorrectionRejected as exc:
            return {"error": str(exc)}
        except OSError as exc:
            reason = exc.strerror or "os error"
            return {"error": f"could not write corrections ledger: {reason}"}
        op["persisted"] = True
        op["id"] = entry["id"]
        op["entry"] = dict(entry)
        return _review_patch([op], explain)

    def propose_review(
        self,
        instruction: object,
        ops: object,
        rationale: object = None,
    ) -> dict[str, Any]:
        text = str(instruction or "").strip()
        if not text:
            return {"error": "propose_review requires a non-empty instruction"}
        raw_ops = _coerce_ops(ops)
        if not isinstance(raw_ops, list):
            return raw_ops
        validated: list[dict[str, Any]] = []
        for index, op in enumerate(raw_ops):
            checked = self._validate_plan_op(op, index)
            if "error" in checked or checked.get("unsupported"):
                return checked
            validated.append(checked)
        lines = _coerce_rationale(rationale, len(validated))
        if not isinstance(lines, list):
            return lines
        return {
            "type": "review_plan",
            "ops": validated,
            "rationale": lines,
        }

    def _validate_plan_op(self, op: object, index: int) -> dict[str, Any]:
        if not isinstance(op, dict):
            return {"error": f"propose_review ops[{index}] must be an object"}
        name = str(op.get("op") or "").strip()
        if name == "set_verdict":
            fid = self._require_finding_id(op.get("finding_id"))
            if fid is None:
                return _unknown_or_missing_finding(op.get("finding_id"))
            checked = _require_verdict(op.get("verdict"))
            if "error" in checked:
                return checked
            return {
                "op": "set_verdict",
                "finding_id": fid,
                "verdict": checked["verdict"],
            }
        if name == "set_severity":
            fid = self._require_finding_id(op.get("finding_id"))
            if fid is None:
                return _unknown_or_missing_finding(op.get("finding_id"))
            checked = _require_severity(op.get("severity"))
            if "error" in checked:
                return checked
            return {
                "op": "set_severity",
                "finding_id": fid,
                "severity": checked["severity"],
            }
        if name == "edit_finding":
            fid = self._require_finding_id(op.get("finding_id"))
            if fid is None:
                return _unknown_or_missing_finding(op.get("finding_id"))
            payload = op.get("fields") if isinstance(op.get("fields"), dict) else op
            fields, error = _edit_fields(payload if isinstance(payload, dict) else {})
            if error is not None:
                return error
            return {
                "op": "edit_finding",
                "finding_id": fid,
                "fields": fields,
            }
        if name == "add_finding":
            added = self.add_finding(
                op.get("timestamp"),
                op.get("summary"),
                op.get("severity"),
                op.get("category"),
            )
            if "error" in added:
                return added
            ops = added.get("ops")
            if isinstance(ops, list) and ops and isinstance(ops[0], dict):
                return ops[0]
            return {"error": f"propose_review ops[{index}] add_finding failed"}
        if name in {"merge_findings", "unmerge_finding"}:
            return {
                "unsupported": True,
                "error": str(_MERGE_STUB["reason"]),
            }
        return {
            "error": (
                f"propose_review ops[{index}] has unknown op {name!r}. "
                "Allowed: set_verdict, set_severity, edit_finding, add_finding."
            )
        }

    def _findings(self) -> list[dict[str, Any]]:
        findings = self.report.get("findings")
        if not isinstance(findings, list):
            return []
        return [f for f in findings if isinstance(f, dict)]

    def _segments(self) -> list[dict[str, Any]]:
        segments = self.report.get("transcript_segments")
        if not isinstance(segments, list):
            return []
        return [s for s in segments if isinstance(s, dict)]

    def _finding_by_id(self, finding_id: object) -> dict[str, Any] | None:
        if finding_id is None or finding_id == "":
            return None
        wanted = str(finding_id)
        for finding in self._findings():
            if str(finding.get("id")) == wanted:
                return finding
        return None

    def _require_finding_id(self, finding_id: object) -> str | None:
        if finding_id is None or finding_id == "":
            return None
        wanted = str(finding_id)
        if self._finding_by_id(wanted) is None:
            return None
        return wanted

    def _manual_markers(self) -> list[dict[str, Any]]:
        markers: list[dict[str, Any]] = []
        manual = self.report.get("manual_review")
        if isinstance(manual, dict):
            for marker in manual.get("markers") or []:
                if isinstance(marker, dict):
                    markers.append(marker)
        human = self.report.get("human_review")
        if isinstance(human, dict):
            for frame in human.get("manual_frames") or []:
                if isinstance(frame, dict):
                    markers.append(frame)
        return markers

    def _frame_target(
        self, finding_id: object
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None] | None:
        """Resolve a finding, merged member, or manual marker id.

        Returns ``(kind, record, parent_finding)``; ``parent_finding`` is set only
        when the id names a member folded into another finding.
        """
        if finding_id is None or finding_id == "":
            return None
        wanted = str(finding_id)
        finding = self._finding_by_id(wanted)
        if finding is not None:
            return ("finding", finding, None)
        for parent in self._findings():
            members = parent.get("merged_frames")
            if not isinstance(members, list):
                continue
            for member in members:
                if isinstance(member, dict) and str(member.get("id")) == wanted:
                    return ("member", member, parent)
        for marker in self._manual_markers():
            marker_id = marker.get("marker_id", marker.get("id"))
            if marker_id is not None and str(marker_id) == wanted:
                return ("manual_marker", marker, None)
        return None

    def _finding_frames(self, finding: dict[str, Any]) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(ref: object, timestamp: object, member_id: object, text: object) -> None:
            if not isinstance(ref, str) or not ref.strip() or ref in seen:
                return
            seen.add(ref)
            frames.append(
                {
                    "ref": ref,
                    "timestamp": _as_float(timestamp),
                    "member_id": member_id,
                    "text": text if isinstance(text, str) else None,
                    "label": f"finding {finding.get('id')}"
                    + (f" / member {member_id}" if member_id is not None else "")
                    + f" @ {_format_ts(_as_float(timestamp))}",
                }
            )

        add(finding.get("screenshot"), finding.get("timestamp_start"), None, finding.get("text"))
        members = finding.get("merged_frames")
        if isinstance(members, list):
            for member in members:
                if isinstance(member, dict):
                    add(
                        member.get("screenshot"),
                        member.get("timestamp_start"),
                        member.get("id"),
                        member.get("text"),
                    )
        return frames

    def _all_frames(self) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        for finding in self._findings():
            frames.extend(self._finding_frames(finding))
        for marker in self._manual_markers():
            ref = marker.get("frame_path")
            if isinstance(ref, str) and ref.strip():
                ts = _as_float(marker.get("timestamp"))
                marker_id = marker.get("marker_id", marker.get("id"))
                frames.append(
                    {
                        "ref": ref,
                        "timestamp": ts,
                        "member_id": None,
                        "text": marker.get("transcript") or marker.get("notes"),
                        "label": f"manual marker {marker_id} @ {_format_ts(ts)}",
                    }
                )
        return frames

    def _select_frames(
        self, finding_id: object, timestamp: object, frame: object
    ) -> list[dict[str, Any]] | dict[str, Any]:
        if isinstance(frame, str) and frame.strip():
            wanted = frame.strip()
            for entry in self._all_frames():
                if entry["ref"] == wanted or Path(entry["ref"]).name == wanted:
                    return [entry]
            return {"error": f"Unknown frame: {wanted}. Use get_finding to list frame names."}
        if finding_id not in (None, ""):
            target = self._frame_target(finding_id)
            if target is None:
                return _unknown_or_missing_finding(finding_id)
            kind, record, parent = target
            if kind == "finding":
                frames = self._finding_frames(record)
            elif kind == "member" and parent is not None:
                ref = record.get("screenshot")
                frames = [e for e in self._finding_frames(parent) if e["ref"] == ref]
            else:
                ref = record.get("frame_path")
                frames = [e for e in self._all_frames() if e["ref"] == ref]
            if not frames:
                return {"error": f"Finding {finding_id} has no stored frame."}
            return frames
        ts = _as_float(timestamp)
        if ts is None:
            return {"error": "view_frame requires finding_id, timestamp, or frame."}
        timed = [e for e in self._all_frames() if e["timestamp"] is not None]
        if not timed:
            return {"error": "This report has no timestamped frames."}
        return [min(timed, key=lambda e: abs(e["timestamp"] - ts))]

    def _load_frame(self, ref: str) -> tuple[str, bytes] | str:
        """Read one report-owned image, confined to the report directory."""
        assert self.output_dir is not None
        base = Path(self.output_dir).resolve()
        rel = Path(ref)
        if rel.is_absolute() or ".." in rel.parts:
            return "frame path is not report-relative"
        candidates = [base / rel]
        if len(rel.parts) == 1:
            candidates.insert(0, base / _SCREENSHOTS_DIRNAME / rel)
        for candidate in candidates:
            resolved = candidate.resolve()
            if base != resolved and base not in resolved.parents:
                continue
            if not resolved.is_file():
                continue
            size = resolved.stat().st_size
            if size > _MAX_FRAME_BYTES:
                return f"frame too large ({size} bytes)"
            raw = resolved.read_bytes()
            if raw.startswith(_PNG_MAGIC):
                return ("image/png", raw)
            if raw.startswith(_JPEG_MAGIC):
                return ("image/jpeg", raw)
            return "stored file is not a JPEG or PNG image"
        return "frame file is missing from the report directory"

    def _finding_nearest(self, timestamp: float) -> dict[str, Any] | None:
        best: dict[str, Any] | None = None
        best_delta = None
        for finding in self._findings():
            ts = _as_float(finding.get("timestamp_start"))
            if ts is None:
                continue
            delta = abs(ts - timestamp)
            if best_delta is None or delta < best_delta:
                best = finding
                best_delta = delta
        return best


def _repo_path_is_sensitive(relative: Path) -> bool:
    """Fail closed for hidden, credential-like, and non-source repo paths."""
    if any(part.startswith(".") for part in relative.parts):
        return True
    name = relative.name.lower()
    if name in {"id_dsa", "id_ed25519", "id_rsa", "id_ecdsa"}:
        return True
    if any(marker in name for marker in _SENSITIVE_REPO_NAME_PARTS):
        return True
    if relative.suffix.lower() in _SENSITIVE_REPO_SUFFIXES:
        return True
    return (
        relative.suffix.lower() not in _SAFE_REPO_FILE_SUFFIXES
        and name not in _SAFE_REPO_FILE_NAMES
    )


def _detail_copy(value: Any, *, key: str = "") -> Any:
    """JSON-safe deep copy for the model: no inline pixels, bounded strings."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            name = str(k)
            if name.endswith("base64") or name in {"frame_base64", "response_id"}:
                continue
            out[name] = _detail_copy(v, key=name)
        return out
    if isinstance(value, list):
        return [_detail_copy(item, key=key) for item in value]
    if isinstance(value, str):
        if value.startswith("data:"):
            return "(inline image omitted; use view_frame)"
        if len(value) > _MAX_DETAIL_STRING_CHARS:
            return value[:_MAX_DETAIL_STRING_CHARS] + "…(truncated)"
    return value


def _format_ts(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _unified(finding: dict[str, Any]) -> dict[str, Any]:
    unified = finding.get("unified_analysis")
    return unified if isinstance(unified, dict) else {}


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _unknown_or_missing_finding(finding_id: object) -> dict[str, Any]:
    if finding_id is None or finding_id == "":
        return {"error": "finding_id is required"}
    return {"error": f"Unknown finding_id: {finding_id}"}


def _require_verdict(value: object) -> dict[str, Any]:
    raw = str(value).strip() if value is not None else ""
    if raw in VERDICT_VALUES:
        return {"verdict": raw}
    allowed = ", ".join(VERDICT_VALUES)
    return {"error": f"Invalid verdict {raw!r}. Allowed: {allowed}"}


def _require_severity(value: object) -> dict[str, Any]:
    raw = str(value).strip() if value is not None else ""
    if raw in SEVERITY_VALUES:
        return {"severity": raw}
    allowed = ", ".join(SEVERITY_VALUES)
    return {"error": f"Invalid severity {raw!r}. Allowed: {allowed}"}


def _edit_fields(args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    nested = args.get("fields")
    source: dict[str, Any] = dict(args)
    if isinstance(nested, dict):
        source.update(nested)
    fields: dict[str, Any] = {}
    for key, patch_key in _EDIT_FIELD_TO_PATCH.items():
        if key not in source:
            continue
        fields[patch_key] = _normalize_edit_value(patch_key, source[key])
    if not fields:
        return {}, {
            "error": ("edit_finding requires at least one of: " + ", ".join(EDIT_FIELD_KEYS))
        }
    return fields, None


def _normalize_edit_value(patch_key: str, value: object) -> str:
    if patch_key == "action_items" and isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ""
    return str(value)


def _coerce_ops(ops: object) -> list[Any] | dict[str, Any]:
    if isinstance(ops, str):
        try:
            loaded = json.loads(ops)
        except json.JSONDecodeError:
            return {"error": "propose_review ops must be a JSON array of patch operations"}
        ops = loaded
    if not isinstance(ops, list):
        return {"error": "propose_review requires ops: a list of review patch operations"}
    return ops


def _coerce_rationale(rationale: object, count: int) -> list[str] | dict[str, Any]:
    if rationale is None:
        return [""] * count
    if isinstance(rationale, str):
        try:
            loaded = json.loads(rationale)
        except json.JSONDecodeError:
            loaded = [rationale]
        rationale = loaded
    if not isinstance(rationale, list):
        return {"error": "propose_review rationale must be a list of strings, one per op"}
    if len(rationale) not in {0, count}:
        return {
            "error": (
                f"propose_review rationale must have one entry per op "
                f"(got {len(rationale)}, expected {count})"
            )
        }
    if not rationale:
        return [""] * count
    return [str(item) for item in rationale]


def _review_patch(ops: list[dict[str, Any]], explain: str) -> dict[str, Any]:
    return {"type": "review_patch", "ops": ops, "explain": explain}


def _function_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": parameters,
    }
