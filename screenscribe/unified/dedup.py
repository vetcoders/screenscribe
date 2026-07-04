"""Finding deduplication for the unified pipeline.

Two-stage dedup: identical-summary grouping (cross-category) followed by
similarity grouping (same category, 30s window), then merge.
"""

from __future__ import annotations

from ..text_similarity import _text_similarity
from ._console import console
from .finding import UnifiedFinding

# Severity ranking shared by every merge path (heuristic dedup + LLM-merge).
_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "none": 0}


def _union_list_field(group: list[UnifiedFinding], attr: str) -> list[str]:
    """Order-preserving, case-insensitive union of a list[str] field across a group."""
    out: list[str] = []
    seen: set[str] = set()
    for f in group:
        for item in getattr(f, attr):
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(item)
    return out


def _concat_distinct_field(group: list[UnifiedFinding], attr: str) -> str:
    """Join distinct non-empty values of a str field, earliest member first.

    Vision/text fields like ``technical_observations`` are free text, so they
    cannot be set-unioned; concatenate the distinct contributions (blank-line
    separated) so a merge keeps every member's evidence instead of only base's.
    """
    parts: list[str] = []
    seen: set[str] = set()
    for f in group:
        value = (getattr(f, attr) or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            parts.append(value)
    return "\n\n".join(parts)


def merge_finding_group(
    group: list[UnifiedFinding],
    *,
    summary_override: str | None = None,
    action_items_limit: int | None = 5,
) -> UnifiedFinding:
    """Fold a group of findings into one, preserving the union of their value.

    THE single merge mechanism, shared by the heuristic ``deduplicate_findings``
    and the semantic ``llm_merge_findings`` pass (DRY: two triggers, one merge).
    The merged finding keeps the EARLIEST finding as base (timestamp / ids /
    vision fields), the highest severity, and the de-duplicated UNION of
    ``action_items`` + ``affected_components``. Every merged-away
    ``(detection_id, timestamp)`` -- including provenance already accumulated by
    an earlier pass -- is recorded in ``merged_from_ids`` so the trail survives a
    second (LLM) merge and the screenshot-prune step keeps every original frame.
    Reliability state is merged conservatively (any degraded => degraded; any
    unstructured => unstructured), never silently upgraded.

    ``summary_override`` lets the LLM pass keep the RICHEST description; when None
    the earliest finding's summary is kept (heuristic-dedup behaviour).
    ``action_items_limit`` caps the merged action list (heuristic dedup keeps 5
    for UX); ``None`` keeps the full union so no theme is dropped.
    """
    if len(group) == 1:
        return group[0]

    # Sort by timestamp to get the earliest as base.
    group = sorted(group, key=lambda f: f.timestamp)
    base = group[0]

    best_severity = max(group, key=lambda f: _SEVERITY_RANK.get(f.severity, 0)).severity

    # Combine action items (deduplicate, case-insensitive, order-preserving).
    all_actions: list[str] = []
    seen_actions: set[str] = set()
    for f in group:
        for action in f.action_items:
            action_lower = action.lower()
            if action_lower not in seen_actions:
                all_actions.append(action)
                seen_actions.add(action_lower)

    # Combine affected components.
    all_components: list[str] = []
    seen_components: set[str] = set()
    for f in group:
        for comp in f.affected_components:
            comp_lower = comp.lower()
            if comp_lower not in seen_components:
                all_components.append(comp)
                seen_components.add(comp_lower)

    # Provenance trail: keep base's own prior merged_from_ids, then record every
    # other member (and its own prior provenance) so a second pass never loses
    # the original ids the screenshot-prune step keys by.
    merged_ids: list[tuple[int, float]] = list(base.merged_from_ids)
    seen_ids: set[tuple[int, float]] = set(merged_ids)
    for f in group:
        if f is base:
            continue
        key = (f.detection_id, f.timestamp)
        if key not in seen_ids:
            merged_ids.append(key)
            seen_ids.add(key)
        for prior in f.merged_from_ids:
            if prior not in seen_ids:
                merged_ids.append(prior)
                seen_ids.add(prior)

    # C6.6: preserve reliability state across the merge -- never silently upgrade
    # a group that contained a "degraded" (raw-text fallback) member to "high".
    merged_confidence = "degraded" if any(f.confidence == "degraded" for f in group) else "high"
    merged_parsed_from_unstructured = any(f.parsed_from_unstructured_output for f in group)

    return UnifiedFinding(
        detection_id=base.detection_id,
        screenshot_path=base.screenshot_path,
        timestamp=base.timestamp,
        category=base.category,
        is_issue=any(f.is_issue for f in group),
        sentiment=base.sentiment,
        severity=best_severity,
        summary=summary_override if summary_override is not None else base.summary,
        action_items=(
            all_actions if action_items_limit is None else all_actions[:action_items_limit]
        ),
        affected_components=all_components,
        # Vision/text evidence: union (not base-only) so a cross-category semantic
        # merge keeps every member's unique issues/elements/notes instead of
        # dropping the non-anchor members' visual evidence that is later
        # serialized and shown/exported.
        suggested_fix=_concat_distinct_field(group, "suggested_fix"),
        ui_elements=_union_list_field(group, "ui_elements"),
        issues_detected=_union_list_field(group, "issues_detected"),
        accessibility_notes=_union_list_field(group, "accessibility_notes"),
        design_feedback=_concat_distinct_field(group, "design_feedback"),
        technical_observations=_concat_distinct_field(group, "technical_observations"),
        response_id=base.response_id,
        merged_from_ids=merged_ids,
        confidence=merged_confidence,
        parsed_from_unstructured_output=merged_parsed_from_unstructured,
    )


def deduplicate_findings(
    findings: list[UnifiedFinding],
    similarity_threshold: float = 0.4,
) -> list[UnifiedFinding]:
    """Deduplicate similar findings by merging them.

    Two-stage deduplication:
    1. Group findings with IDENTICAL summaries (cross-category, always merge)
    2. Group SIMILAR findings only if same category AND within 30s timestamp

    The merged finding keeps:
    - Highest severity
    - Combined action items (deduplicated)
    - First screenshot (earliest timestamp)
    - Combined affected components

    Args:
        findings: List of UnifiedFinding objects
        similarity_threshold: Minimum similarity (0-1) to consider as duplicate

    Returns:
        Deduplicated list of UnifiedFinding objects
    """
    if not findings or len(findings) <= 1:
        return findings

    def normalize_text(text: str) -> str:
        """Normalize text for comparison: lowercase and collapse whitespace."""
        return " ".join(text.lower().split())

    def extract_similarity_text(finding: UnifiedFinding) -> str:
        """Extract text from finding for similarity comparison."""
        if finding.summary.strip():
            return finding.summary
        parts = []
        parts.extend(finding.action_items or [])
        parts.extend(finding.affected_components or [])
        parts.extend(finding.issues_detected or [])
        parts.extend(finding.ui_elements or [])
        return " ".join(part.strip() for part in parts if part and part.strip())

    # Stage 1: group findings with IDENTICAL summaries (cross-category, always
    # merge). C6.6: this MUST run as a COMPLETE pass BEFORE any similarity
    # grouping. The previous single-pass version consumed identical groups lazily
    # inside the similarity loop, so an earlier finding's similarity scan could
    # "steal" one identical-summary member into its own group, stranding the
    # identical twin as a separate output finding -- silently breaking the
    # always-merge guarantee (and, when the stolen member was the earliest one,
    # even emitting the same summary in two output findings). Claiming all
    # identical groups up front makes the guarantee structural, not order-luck.
    summary_groups: dict[str, list[int]] = {}
    for idx, finding in enumerate(findings):
        key = normalize_text(finding.summary)
        if key:
            summary_groups.setdefault(key, []).append(idx)

    groups: list[list[UnifiedFinding]] = []
    group_min_index: list[int] = []
    used: set[int] = set()

    for idxs in summary_groups.values():
        if len(idxs) > 1:
            groups.append([findings[idx] for idx in idxs])
            group_min_index.append(min(idxs))
            used.update(idxs)

    # Stage 2: group SIMILAR findings (same category AND within 30s window),
    # over only the findings not already claimed by an identical-summary group.
    for i, finding in enumerate(findings):
        if i in used:
            continue

        # Start new group with this finding
        group = [finding]
        used.add(i)

        # Find similar findings (same category + close timestamp). All indices
        # <= i are already in `used`, so scan forward only.
        for j in range(i + 1, len(findings)):
            if j in used:
                continue
            other = findings[j]

            # Only compare within same category and 30s window
            if finding.category == other.category:
                if abs(finding.timestamp - other.timestamp) > 30:
                    continue
                similarity = _text_similarity(
                    extract_similarity_text(finding), extract_similarity_text(other)
                )
            else:
                similarity = 0.0

            if similarity >= similarity_threshold:
                group.append(other)
                used.add(j)

        groups.append(group)
        group_min_index.append(i)

    # Stable output order: by the earliest original index in each group, so the
    # Stage-1-first restructure does not reshuffle findings versus input order.
    groups = [groups[g] for g in sorted(range(len(groups)), key=lambda g: group_min_index[g])]

    # Merge each group into single finding via the shared merge mechanism.
    # Heuristic dedup keeps the earliest summary (summary_override=None) and caps
    # action items at 5 -- the long-standing UX behaviour preserved here.
    result: list[UnifiedFinding] = []

    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue

        merged = merge_finding_group(group)
        result.append(merged)

        # Log merge (group sorted by timestamp inside merge_finding_group; the
        # earliest summary is the merged summary).
        earliest_summary = min(group, key=lambda f: f.timestamp).summary
        console.print(
            f"[dim]  Merged {len(group)} similar findings → '{earliest_summary[:50]}...'[/]"
        )

    return result
