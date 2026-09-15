"""Screenscribe-local tools the review agent may call. Read-only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MAX_OPEN_FILE_BYTES = 80_000
_MAX_TRANSCRIPT_HITS = 80

TOOL_NAMES = (
    "list_findings",
    "get_transcript",
    "seek",
    "show_frame",
    "get_report_summary",
    "open_repo_file",
)


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
