"""Screenscribe-local tools the review agent may call.

Read tools inspect the loaded report. Write tools do **not** touch
``report.json``: they validate against the report and return a ``review_patch``
(or a ``review_plan``). The browser is the single writer of review state.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..work_item import VERDICT_VALUES

_MAX_OPEN_FILE_BYTES = 80_000
_MAX_TRANSCRIPT_HITS = 80

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
    ]
    if include_repo:
        tools.append(
            _function_tool(
                "open_repo_file",
                "Read a text file from the repo the review server was started with (--repo). "
                "Path must stay inside that repo root.",
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
    ) -> None:
        self.report = report
        self.output_dir = output_dir
        self.repo_root = repo_root.resolve() if repo_root is not None else None

    def execute(self, name: str, arguments: dict[str, Any] | None) -> str:
        args = arguments if isinstance(arguments, dict) else {}
        try:
            if name == "list_findings":
                result = self.list_findings(str(args.get("filter") or ""))
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
            else:
                result = {"error": f"Unknown tool: {name}"}
        except Exception as exc:
            result = {"error": str(exc)}
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
            }
            hay = " ".join(str(v) for v in row.values() if v is not None).lower()
            if needle and needle not in hay:
                continue
            rows.append(row)
        return {"count": len(rows), "findings": rows, "filter": filter_text}

    def get_transcript(self, start: object = None, end: object = None) -> dict[str, Any]:
        start_s = _as_float(start)
        end_s = _as_float(end)
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
            return {"error": "seek requires a numeric timestamp"}
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
            resolved.relative_to(self.repo_root)
        except ValueError:
            return {"error": "path escapes the repo root"}
        if not resolved.is_file():
            return {"error": "not a file"}
        size = resolved.stat().st_size
        if size > _MAX_OPEN_FILE_BYTES:
            return {"error": f"file too large ({size} bytes)"}
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return {"path": str(resolved.relative_to(self.repo_root)), "content": text}

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
            return {"error": "add_finding requires a numeric timestamp"}
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


def _unified(finding: dict[str, Any]) -> dict[str, Any]:
    unified = finding.get("unified_analysis")
    return unified if isinstance(unified, dict) else {}


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


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
