"""Seed the review agent from a report JSON document."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_SEED_BUDGET_CHARS = 12_000
_FINDING_CAP = 40
_SEGMENT_CAP = 80

_SYSTEM = (
    "You are the Screenscribe review agent. You help an engineer continue a "
    "conversation about a narrated screen-recording review: findings, transcript, "
    "and frames. Screen recordings often contain secrets (API keys, tokens, "
    "personal data) — never quote secrets, never ask the user to paste keys, "
    "and prefer referring to findings by id and timestamp. Use tools to inspect "
    "the report rather than guessing. Reply in the language of the user's message. "
    "When the user asks to jump in the video, call seek. When they ask to see a "
    "frame, call show_frame."
)


@dataclass(frozen=True)
class PreparedTurn:
    """What the first/next model call should send."""

    previous_response_id: str | None
    instructions: str
    input_items: list[dict[str, Any]]
    seeded: bool


def report_chain_response_id(report: dict[str, Any] | None) -> str | None:
    """Last analysis-pass ``response_id`` stored on the report, if any.

    Newer reports write it at ``analysis_passes.unified_analysis.response_id``.
    Existing reports only keep it on each finding's ``unified_analysis``; the
    last non-empty value is the chain head from the VLM pass.
    """
    if not isinstance(report, dict):
        return None
    passes = report.get("analysis_passes")
    if isinstance(passes, dict):
        unified = passes.get("unified_analysis")
        if isinstance(unified, dict):
            stored = _nonempty_id(unified.get("response_id"))
            if stored:
                return stored
    last: str | None = None
    findings = report.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            unified = finding.get("unified_analysis")
            if isinstance(unified, dict):
                stored = _nonempty_id(unified.get("response_id"))
                if stored:
                    last = stored
    return last


def seed_report_context(report: dict[str, Any] | None) -> str:
    """Compact JSON-report seed used when there is no chain head to resume."""
    if not isinstance(report, dict):
        return "(no report JSON loaded)"

    findings_out: list[dict[str, Any]] = []
    findings = report.get("findings")
    if isinstance(findings, list):
        for finding in findings[:_FINDING_CAP]:
            if not isinstance(finding, dict):
                continue
            unified = finding.get("unified_analysis")
            unified_dict = unified if isinstance(unified, dict) else {}
            findings_out.append(
                {
                    "id": finding.get("id"),
                    "category": finding.get("category"),
                    "timestamp": finding.get("timestamp_start"),
                    "timestamp_formatted": finding.get("timestamp_formatted"),
                    "severity": unified_dict.get("severity") or finding.get("severity"),
                    "summary": unified_dict.get("summary") or finding.get("text"),
                    "is_issue": unified_dict.get("is_issue"),
                }
            )

    segments_out: list[dict[str, Any]] = []
    segments = report.get("transcript_segments")
    if isinstance(segments, list):
        for segment in segments[:_SEGMENT_CAP]:
            if not isinstance(segment, dict):
                continue
            segments_out.append(
                {
                    "id": segment.get("id"),
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "text": segment.get("text"),
                }
            )

    blob = {
        "video": report.get("video"),
        "executive_summary": report.get("executive_summary") or "",
        "severity_breakdown": report.get("severity_breakdown") or {},
        "summary": report.get("summary") or {},
        "analysis_passes": report.get("analysis_passes") or {},
        "findings": findings_out,
        "transcript_segments": segments_out,
    }
    import json

    text = json.dumps(blob, ensure_ascii=False, indent=2)
    if len(text) > _SEED_BUDGET_CHARS:
        text = text[:_SEED_BUDGET_CHARS] + "\n…(truncated)"
    return text


def prepare_turn(
    *,
    report: dict[str, Any] | None,
    message: str,
    history: list[dict[str, str]] | None = None,
    previous_response_id: str | None = None,
) -> PreparedTurn:
    """Pick ``previous_response_id`` (request, else report) or seed from JSON."""
    requested = _nonempty_id(previous_response_id)
    chained = requested or report_chain_response_id(report)
    input_items = _history_items(history)
    input_items.append(
        {
            "role": "user",
            "content": [{"type": "input_text", "text": message}],
        }
    )
    if chained:
        return PreparedTurn(
            previous_response_id=chained,
            instructions=_SYSTEM,
            input_items=input_items,
            seeded=False,
        )
    seed = seed_report_context(report)
    instructions = f"{_SYSTEM}\n\nCurrent review report (JSON seed; use tools for details):\n{seed}"
    return PreparedTurn(
        previous_response_id=None,
        instructions=instructions,
        input_items=input_items,
        seeded=True,
    )


def _history_items(history: list[dict[str, str]] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not history:
        return items
    for entry in history:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip()
        content = str(entry.get("content") or "")
        if role not in {"user", "assistant"} or not content:
            continue
        items.append(
            {
                "role": role,
                "content": [{"type": "input_text", "text": content}],
            }
        )
    return items


def _nonempty_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
