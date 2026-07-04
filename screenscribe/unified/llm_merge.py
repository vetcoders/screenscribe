"""Semantic LLM-merge pass for near-duplicate findings.

Second, OPTIONAL dedup layer that runs AFTER the cheap heuristic
``deduplicate_findings``. The heuristic only merges within ONE category inside a
30s window using string similarity, so it is blind to cross-category PARAPHRASES
of the same concrete issue ("the buttons look ugly" @02:17 in ``ui`` vs "make the
confirm controls nicer" @02:28 in ``change``). This pass asks the configured LLM
to cluster findings that describe the same specific issue, then reuses the one
canonical merge mechanism (``merge_finding_group``) to fold each cluster into a
single richer finding that keeps the UNION of every member's value plus a
``merged_from_ids`` provenance trail.

Conservative by design: the prompt instructs the model to merge ONLY clearly
identical issues and to leave distinct problems separate. A disabled flag, a
missing API key, an empty/declined grouping, or any transport/parse error all
degrade to a safe no-op (the heuristic result is returned unchanged), so the pass
never costs a distinct signal and never blocks a run without an LLM/budget.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from ..api_utils import (
    build_llm_request_body,
    extract_llm_response_text,
    retry_request,
)
from ..config import ScreenScribeConfig
from ._console import console
from .dedup import merge_finding_group
from .finding import UnifiedFinding

# A raw prompt -> raw model text transport. Injected in tests (mock-LLM) so the
# prompt-build + parse + merge path runs without a real API call.
LlmCaller = Callable[[str], str]


_MERGE_PROMPT_HEADER = """You are consolidating a list of review findings extracted from a screen recording.
Each finding is something the reviewer said about an app. Some findings are
PARAPHRASES of the SAME concrete issue -- possibly filed under different
categories or at different timestamps.

Your job: group together findings that describe the SAME SPECIFIC issue so they
can be merged into one. Be CONSERVATIVE:
- Only group findings that are clearly the SAME concrete problem or observation.
- Different problems stay separate, even when they touch the same screen, feature
  or wording. Touching the same area is NOT enough to merge.
- When in doubt, do NOT group. A wrong merge destroys a distinct signal; a missed
  merge only leaves a duplicate.

Findings (index | category | description):
"""

_MERGE_PROMPT_FOOTER = """
Return ONLY a JSON object of this exact shape, with no prose and no code fence:
{"groups": [[0, 2], [3, 5, 6]]}
Each inner list is a set of indices that are the SAME issue and should merge.
Omit singletons. Any index you do not list is kept as a standalone finding.
"""


def _finding_label(finding: UnifiedFinding) -> str:
    """One-line description of a finding for the prompt (summary, else fields)."""
    text = finding.summary.strip()
    if not text:
        parts = [*finding.action_items, *finding.affected_components]
        text = " ".join(p.strip() for p in parts if p and p.strip())
    return " ".join(text.split())


def _build_merge_prompt(findings: list[UnifiedFinding]) -> str:
    lines = [f"[{i}] ({f.category}) {_finding_label(f)}" for i, f in enumerate(findings)]
    return _MERGE_PROMPT_HEADER + "\n".join(lines) + "\n" + _MERGE_PROMPT_FOOTER


def _parse_merge_groups(raw: str, count: int) -> list[list[int]]:
    """Parse the model's grouping into validated, disjoint index groups.

    Tolerant of code fences / surrounding prose; rejects out-of-range, duplicate,
    boolean, and singleton entries. An index may appear in at most ONE group
    (first wins), so a malformed/over-eager response can never double-merge a
    finding.
    """
    text = raw.strip()
    # Isolate the JSON object even when wrapped in a fence or prose.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    raw_groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(raw_groups, list):
        return []

    groups: list[list[int]] = []
    used: set[int] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, list):
            continue
        members: list[int] = []
        seen: set[int] = set()
        for idx in raw_group:
            # bool is an int subclass -- exclude it explicitly.
            if not isinstance(idx, int) or isinstance(idx, bool):
                continue
            if idx < 0 or idx >= count or idx in used or idx in seen:
                continue
            members.append(idx)
            seen.add(idx)
        if len(members) >= 2:
            groups.append(members)
            used.update(members)
    return groups


def _richest_summary(group: list[UnifiedFinding]) -> str:
    """The longest (richest) summary in the group -- the editable description."""
    return max((f.summary for f in group), key=lambda s: len(s.strip()), default="")


def _apply_merge_groups(
    findings: list[UnifiedFinding], groups: list[list[int]]
) -> list[UnifiedFinding]:
    """Replace each grouped cluster with one merged finding, preserving order.

    The merged finding is emitted at the position of its earliest-listed member;
    all ungrouped findings keep their original position (stable output).
    """
    grouped_idx = {idx for g in groups for idx in g}
    merged_by_anchor: dict[int, UnifiedFinding] = {}
    for g in groups:
        members = [findings[i] for i in g]
        merged_by_anchor[min(g)] = merge_finding_group(
            members,
            summary_override=_richest_summary(members),
            action_items_limit=None,  # keep the full union -- no theme dropped
        )

    result: list[UnifiedFinding] = []
    for i, finding in enumerate(findings):
        if i in merged_by_anchor:
            result.append(merged_by_anchor[i])
        elif i not in grouped_idx:
            result.append(finding)
    return result


def _llm_merge_enabled(config: ScreenScribeConfig) -> bool:
    """The disable flag; defaults to enabled when the attribute is absent."""
    return getattr(config, "llm_merge_enabled", True)


def _default_llm_caller(config: ScreenScribeConfig) -> LlmCaller:
    """Build the real LLM transport from config (generic, no hardcoded secrets)."""
    api_key = config.get_llm_api_key()
    endpoint = config.llm_endpoint
    model = config.llm_model

    def call(prompt: str) -> str:
        body = build_llm_request_body(model, prompt, endpoint)

        def do_request() -> httpx.Response:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                return response

        response = retry_request(do_request, operation_name="LLM-merge pass")
        return extract_llm_response_text(response.json(), endpoint)

    return call


def llm_merge_findings(
    findings: list[UnifiedFinding],
    config: ScreenScribeConfig,
    *,
    llm_caller: LlmCaller | None = None,
) -> list[UnifiedFinding]:
    """Semantically merge near-duplicate findings via the configured LLM.

    Runs AFTER ``deduplicate_findings`` as an optional second layer. Returns the
    input unchanged (safe no-op) when the pass is disabled, when fewer than two
    findings exist, when no LLM API key is configured, or when the LLM call /
    parse fails. ``llm_caller`` injects the raw prompt->text transport for tests
    (mock-LLM) so the prompt-build + parse + merge path is exercised without a
    real API call.
    """
    if not _llm_merge_enabled(config):
        return findings
    if len(findings) <= 1:
        return findings
    if llm_caller is None:
        if not config.get_llm_api_key():
            # No key / no budget -> fall back to the heuristic result (no-op).
            return findings
        llm_caller = _default_llm_caller(config)

    prompt = _build_merge_prompt(findings)
    try:
        raw = llm_caller(prompt)
    except Exception as exc:  # transport / provider failure -> safe no-op
        console.print(f"[yellow]LLM-merge pass skipped (call failed): {exc}[/]")
        return findings

    groups = _parse_merge_groups(raw, len(findings))
    if not groups:
        return findings

    merged = _apply_merge_groups(findings, groups)
    if len(merged) < len(findings):
        console.print(
            f"[green]LLM-merge:[/] {len(findings)} → {len(merged)} findings "
            f"({len(findings) - len(merged)} semantic duplicate(s) merged)"
        )
    return merged
