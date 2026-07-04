"""Summary generation for unified findings.

Executive summary (provider + deterministic local fallback) and the visual
issue summary.
"""

from __future__ import annotations

import httpx

from ..api_utils import retry_request
from ..config import ScreenScribeConfig
from ._console import console
from .finding import UnifiedFinding
from .response_parsing import _extract_response_error, extract_response_content


def _build_local_executive_summary(findings: list[UnifiedFinding], language: str) -> str:
    """Build a deterministic executive summary when provider summary generation fails."""
    issues = [f for f in findings if f.is_issue]
    if not issues:
        if language.lower().startswith("pl"):
            return "Nie wykryto problemów. Wszystkie obserwacje potwierdzają poprawne działanie."
        return "No issues found. All observations indicate the reviewed flow works correctly."

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}
    ranked_issues = sorted(issues, key=lambda item: severity_order.get(item.severity, 99))
    critical = sum(1 for f in issues if f.severity == "critical")
    high = sum(1 for f in issues if f.severity == "high")
    medium = sum(1 for f in issues if f.severity == "medium")
    low = sum(1 for f in issues if f.severity == "low")
    top_summaries = [f.summary.strip().rstrip(".") for f in ranked_issues[:3] if f.summary.strip()]

    if language.lower().startswith("pl"):
        summary = (
            f"Wykryto {len(issues)} problem(y): {critical} krytycznych, {high} wysokich, "
            f"{medium} średnich i {low} niskich."
        )
        if top_summaries:
            summary += " Najważniejsze ustalenia: " + "; ".join(top_summaries) + "."
        return summary

    summary = (
        f"Detected {len(issues)} issue(s): {critical} critical, {high} high, "
        f"{medium} medium, and {low} low."
    )
    if top_summaries:
        summary += " Key takeaways: " + "; ".join(top_summaries) + "."
    return summary


def generate_unified_summary(findings: list[UnifiedFinding], config: ScreenScribeConfig) -> str:
    """
    Generate executive summary from unified findings.

    Args:
        findings: List of UnifiedFinding results
        config: screenscribe configuration

    Returns:
        Executive summary text
    """
    if not findings:
        return ""

    # Filter to issues only
    issues = [f for f in findings if f.is_issue]
    if not issues:
        return _build_local_executive_summary(findings, config.language)

    fallback_summary = _build_local_executive_summary(findings, config.language)
    if not config.get_llm_api_key():
        return fallback_summary

    # Build findings summary for prompt
    from ..prompts import get_executive_summary_prompt

    findings_list = []
    for f in issues:
        findings_list.append(f"- [{f.severity.upper()}] {f.summary}")

    prompt_template = get_executive_summary_prompt(config.language)
    prompt = prompt_template.format(findings=chr(10).join(findings_list))

    try:

        def do_summary_request() -> httpx.Response:
            with httpx.Client(timeout=60.0) as client:
                # Build request body based on API format
                from ..api_utils import build_llm_request_body

                response = client.post(
                    config.llm_endpoint,
                    headers={
                        "Authorization": f"Bearer {config.get_llm_api_key()}",
                        "Content-Type": "application/json",
                    },
                    json=build_llm_request_body(config.llm_model, prompt, config.llm_endpoint),
                )
                response.raise_for_status()
                return response

        response = retry_request(
            do_summary_request,
            max_retries=3,
            operation_name="Executive summary",
        )

        result = response.json()
        response_error = _extract_response_error(result)
        if response_error:
            raise RuntimeError(response_error)

        content = extract_response_content(
            result,
            clean_summary=True,
            endpoint=config.llm_endpoint,
            language=config.language,
        )
        if content.strip():
            return content
        return fallback_summary

    except Exception as e:
        console.print(f"[yellow]Executive summary failed: {e}[/]")
        return fallback_summary


def generate_visual_summary_unified(findings: list[UnifiedFinding], language: str = "pl") -> str:
    """
    Generate summary of visual issues found.

    Args:
        findings: List of UnifiedFinding results

    Returns:
        Visual summary text in Markdown
    """
    if not findings:
        return ""

    # Collect all visual issues
    all_issues = []
    for f in findings:
        if f.is_issue:
            all_issues.extend(f.issues_detected)

    if not all_issues:
        return ""

    # Count unique issues
    from collections import Counter

    issue_counts = Counter(all_issues)

    # Format summary (P2-3: headers follow the report language, not hardcoded PL).
    # Default stays "pl" so existing callers that don't pass language keep their
    # prior behaviour; review_pipeline now passes the real transcript language.
    if language.lower().startswith("pl"):
        lines = ["## Podsumowanie analizy wizualnej", "", "### Najczęstsze problemy:"]
    else:
        lines = ["## Visual analysis summary", "", "### Most common issues:"]
    for issue, count in issue_counts.most_common(10):
        lines.append(f"- {issue} ({count}x)")

    return "\n".join(lines)
