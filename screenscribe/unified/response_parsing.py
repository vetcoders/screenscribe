"""Response parsing and normalization for unified analysis.

Pure transforms: raw provider text -> dict -> UnifiedFinding. No I/O, no
console output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..api_utils import extract_llm_response_text, is_chat_completions_endpoint
from ..detect import Detection
from .finding import UnifiedFinding


def parse_json_response(content: str) -> dict[str, Any]:
    """Parse JSON from LLM response, handling various formats.

    Args:
        content: Raw response text from LLM

    Returns:
        Parsed JSON as dict on success. On failure this does not raise:
        it returns a sentinel dict with ``parse_error`` and ``raw_content``
        keys so the pipeline can continue instead of dropping the finding.
    """
    # Strip model control tokens (e.g. <|channel|>final <|constrain|>JSON<|message|>)
    content = re.sub(r"<\|[^|]+\|>\w*\s*", "", content)

    # If content starts with non-JSON, try to find JSON object
    if not content.strip().startswith("{"):
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            content = json_match.group(0)

    # Handle potential markdown code blocks
    json_content = content
    if "```json" in content:
        json_content = content.split("```json")[1].split("```")[0]
    elif "```" in content:
        parts = content.split("```")
        if len(parts) >= 2:
            json_content = parts[1]

    json_candidates: list[str] = [json_content.strip()]

    # If we still fail, try to grab the largest {...} block
    json_match = re.search(r"\{.*\}", json_content, re.DOTALL)
    if json_match:
        json_candidates.append(json_match.group(0).strip())

    # Try to trim trailing ellipsis or stray characters
    if json_content.strip().endswith("..."):
        json_candidates.append(json_content.strip().rstrip("."))

    last_error: json.JSONDecodeError | None = None
    for candidate in json_candidates:
        try:
            result: dict[str, Any] = json.loads(candidate)
            return result
        except json.JSONDecodeError as e:
            last_error = e
            continue

    # Fallback: return sentinel payload instead of raising, so pipeline can continue
    return {
        "parse_error": str(last_error) if last_error else "Unknown JSON parse error",
        "raw_content": content,
    }


def _normalize_string_list(value: Any) -> list[str]:
    """Normalize model output fields that should contain string lists."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


_SEVERITY_ALLOWLIST: frozenset[str] = frozenset({"critical", "high", "medium", "low", "none"})


def _clamp_severity(value: Any) -> str:
    """Clamp an LLM-controlled severity to a safe allowlist token.

    Defense-in-depth at the source: severity ultimately reaches a `class`
    attribute in the HTML report, so any value outside the allowlist is
    collapsed to ``"none"`` before it can propagate downstream.
    """
    token = str(value if value is not None else "none").strip().lower()
    return token if token in _SEVERITY_ALLOWLIST else "none"


def _normalize_unified_response_data(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce unified response payload into a stable internal shape."""
    raw_summary = str(data.get("summary") or data.get("raw_content") or "").strip()
    normalized: dict[str, Any] = {
        "summary": raw_summary,
        "action_items": _normalize_string_list(data.get("action_items")),
        "affected_components": _normalize_string_list(data.get("affected_components")),
        "ui_elements": _normalize_string_list(data.get("ui_elements")),
        "issues_detected": _normalize_string_list(data.get("issues_detected")),
        "accessibility_notes": _normalize_string_list(data.get("accessibility_notes")),
        "design_feedback": str(data.get("design_feedback", "") or ""),
        "technical_observations": str(data.get("technical_observations", "") or ""),
        "suggested_fix": str(data.get("suggested_fix", "") or ""),
        "sentiment": str(data.get("sentiment", "neutral") or "neutral"),
        "severity": _clamp_severity(data.get("severity", "none")),
        "confidence": str(data.get("confidence", "high") or "high"),
        "parsed_from_unstructured_output": bool(data.get("parsed_from_unstructured_output", False)),
        "is_issue": False,
    }

    if "parse_error" in data:
        # NOTE (BH32 revisited): we intentionally KEEP raw_summary (the model's
        # non-JSON text) as the summary here. The parse_error sentinel also fires
        # for useful plain-prose answers (the designed degraded fallback — see the
        # streaming tests), and parsing cannot tell garbage from a useful summary.
        # The finding is already flagged confidence="degraded" +
        # parsed_from_unstructured_output=True; de-emphasising degraded output is a
        # report-side concern, not a reason to blank a possibly-useful summary.
        normalized.update(
            {
                "is_issue": False,
                "sentiment": "unknown",
                "severity": "none",
                "confidence": "degraded",
                "parsed_from_unstructured_output": True,
                "suggested_fix": str(data.get("parse_error", "") or ""),
            }
        )
        return normalized

    is_issue_value = data.get("is_issue")
    if isinstance(is_issue_value, bool):
        normalized["is_issue"] = is_issue_value
        return normalized

    normalized.update(
        {
            "is_issue": False,
            "severity": "none",
            "confidence": "degraded",
            "suggested_fix": normalized["suggested_fix"]
            or "Model output did not match the expected unified analysis schema.",
        }
    )
    return normalized


def _build_unified_finding(
    detection: Detection,
    screenshot_path: Path | None,
    data: dict[str, Any],
    response_id: str,
) -> UnifiedFinding:
    """Create a UnifiedFinding from normalized provider output."""
    normalized = _normalize_unified_response_data(data)
    return UnifiedFinding(
        detection_id=detection.segment.id,
        screenshot_path=screenshot_path,
        timestamp=detection.segment.start,
        category=detection.category,
        is_issue=normalized["is_issue"],
        sentiment=normalized["sentiment"],
        severity=normalized["severity"],
        summary=normalized["summary"],
        action_items=normalized["action_items"],
        affected_components=normalized["affected_components"],
        suggested_fix=normalized["suggested_fix"],
        ui_elements=normalized["ui_elements"],
        issues_detected=normalized["issues_detected"],
        accessibility_notes=normalized["accessibility_notes"],
        design_feedback=normalized["design_feedback"],
        technical_observations=normalized["technical_observations"],
        response_id=response_id,
        confidence=normalized["confidence"],
        parsed_from_unstructured_output=normalized["parsed_from_unstructured_output"],
    )


def _clean_summary_response(text: str, language: str = "en") -> str:
    """Clean up LLM response that may contain markdown fences or JSON.

    Some models return JSON wrapped in markdown code fences even when asked
    for plain text. This function:
    1. Strips markdown code fences (```json ... ``` or ``` ... ```)
    2. If remaining content is JSON with a "summary" key, extracts it
    3. Otherwise returns the clean text

    Args:
        text: Raw response text from LLM

    Returns:
        Cleaned plain text
    """
    cleaned = text.strip()

    # Strip markdown code fences
    fence_pattern = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)
    match = fence_pattern.match(cleaned)
    if match:
        cleaned = match.group(1).strip()

    # Try to parse as JSON and extract summary if present
    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                # BH56: a present-but-null/empty "summary" must NOT become the
                # literal string "None" via str(None). Only return a truthy
                # summary; otherwise fall through and return "" below so the
                # caller triggers its deterministic local-summary fallback
                # instead of surfacing "None" or the raw JSON object.
                summary_val = parsed.get("summary")
                if summary_val:
                    return str(summary_val)
                # Build readable output from known fields
                parts = []
                if parsed.get("action_items"):
                    items = parsed["action_items"]
                    if isinstance(items, list):
                        # The header follows the report language; the bullets are
                        # the model's own action_items (already localized). A raw
                        # hardcoded PL header here surfaced Polish to EN users.
                        if language.lower().startswith("pl"):
                            parts.append("Priorytetowe akcje:")
                        else:
                            parts.append("Priority actions:")
                        for item in items[:5]:
                            parts.append(f"• {item}")
                if parts:
                    return "\n".join(parts)
                return ""
        except json.JSONDecodeError:
            pass

    return cleaned


def extract_response_content(
    result: dict[str, Any],
    clean_summary: bool = False,
    endpoint: str = "",
    language: str = "en",
) -> str:
    """Extract text content from API response (supports both formats).

    Handles both LibraxisAI v1/responses and OpenAI Chat Completions formats.

    Args:
        result: API response JSON
        clean_summary: If True, clean up markdown fences and extract from JSON
        endpoint: API endpoint URL (used to detect format)
        language: Report language; drives the localized header emitted by
            ``_clean_summary_response`` when it rebuilds a summary from
            ``action_items`` (only consulted when ``clean_summary`` is True)

    Returns:
        Extracted text content
    """
    # Use unified helper if endpoint provided
    if endpoint and is_chat_completions_endpoint(endpoint):
        content = extract_llm_response_text(result, endpoint)
    else:
        # LibraxisAI v1/responses format
        content = ""
        output_list = result.get("output", [])
        if not isinstance(output_list, list):
            return content
        for item in output_list:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")
            # Handle reasoning blocks (skip - look for actual output)
            if item_type == "reasoning":
                pass
            # Handle message blocks
            elif item_type == "message":
                item_content = item.get("content", [])
                if isinstance(item_content, list):
                    for part in item_content:
                        if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                            text = part.get("text", "")
                            if isinstance(text, str):
                                content += text
            # Handle direct output_text or text
            elif item_type in ("output_text", "text"):
                text = item.get("text", "")
                if isinstance(text, str):
                    content += text

    if clean_summary:
        content = _clean_summary_response(content, language)

    return content


def _extract_response_error(result: dict[str, Any]) -> str:
    """Extract provider-side error from a non-streaming response payload."""
    error_payload = result.get("error", {})
    if isinstance(error_payload, dict):
        message = error_payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    status = str(result.get("status", "")).strip().lower()
    if status == "failed":
        return "Unified analysis response completed with failed status."

    return ""
