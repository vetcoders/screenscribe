"""Tests for deduplicate_findings (the live UnifiedFinding dedup path).

Regression guard for R4-08: the production dedup must use the canonical
text_similarity normalisation (full PL stem map), not the inferior in-module
copy that silently dropped Polish stems and failed to merge real duplicates.
"""

from __future__ import annotations

from screenscribe.unified.dedup import merge_finding_group
from screenscribe.unified_analysis import UnifiedFinding, deduplicate_findings


def _mk(
    detection_id: int,
    summary: str,
    *,
    category: str = "ui",
    timestamp: float = 0.0,
    is_issue: bool = True,
    severity: str = "medium",
    action_items: list[str] | None = None,
    affected_components: list[str] | None = None,
    confidence: str = "high",
    parsed_from_unstructured_output: bool = False,
) -> UnifiedFinding:
    """Minimal UnifiedFinding factory for dedup tests."""
    return UnifiedFinding(
        detection_id=detection_id,
        screenshot_path=None,
        timestamp=timestamp,
        category=category,
        is_issue=is_issue,
        sentiment="neutral",
        severity=severity,
        summary=summary,
        action_items=action_items or [],
        affected_components=affected_components or [],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        confidence=confidence,
        parsed_from_unstructured_output=parsed_from_unstructured_output,
    )


def test_empty_and_single_are_returned_unchanged() -> None:
    assert deduplicate_findings([]) == []
    single = [_mk(1, "only finding")]
    assert deduplicate_findings(single) == single


def test_stage1_identical_summary_merges_across_categories() -> None:
    """Identical summaries merge even across different categories (Stage 1)."""
    findings = [
        _mk(1, "Button is misaligned", category="ui", timestamp=0),
        _mk(2, "Button is misaligned", category="bug", timestamp=200),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1


def test_stage2_similar_same_category_within_window_merges() -> None:
    findings = [
        _mk(1, "The dropdown menu does not open on click", category="ui", timestamp=0),
        _mk(2, "Dropdown menu fails to open when clicked", category="ui", timestamp=10),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1


def test_stage2_outside_timestamp_window_does_not_merge() -> None:
    findings = [
        _mk(1, "The dropdown menu does not open on click", category="ui", timestamp=0),
        _mk(2, "Dropdown menu fails to open when clicked", category="ui", timestamp=100),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 2


def test_different_categories_with_similar_text_do_not_merge() -> None:
    """Cross-category similarity is forced to 0.0 (only identical summaries merge)."""
    findings = [
        _mk(1, "The dropdown menu does not open on click", category="ui", timestamp=0),
        _mk(2, "Dropdown menu fails to open when clicked", category="bug", timestamp=5),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 2


def test_merged_finding_keeps_highest_severity() -> None:
    findings = [
        _mk(1, "Crash on save", category="bug", timestamp=0, severity="low"),
        _mk(2, "Crash on save", category="bug", timestamp=1, severity="critical"),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    assert result[0].severity == "critical"


def test_action_items_merged_deduped_and_capped_at_five() -> None:
    findings = [
        _mk(1, "Same issue", category="ui", timestamp=0, action_items=["a", "b", "C"]),
        _mk(2, "Same issue", category="ui", timestamp=1, action_items=["c", "d", "e", "f", "g"]),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    actions = result[0].action_items
    assert len(actions) == 5  # capped
    # case-insensitive dedup: "c" (from group 2) is dropped because "C" already seen
    lowered = [a.lower() for a in actions]
    assert len(lowered) == len(set(lowered))


def test_affected_components_merged_and_deduped_no_cap() -> None:
    comps1 = [f"comp{i}" for i in range(7)]
    comps2 = ["COMP0", "comp7"]  # COMP0 is a case dup
    findings = [
        _mk(1, "Same issue", category="ui", timestamp=0, affected_components=comps1),
        _mk(2, "Same issue", category="ui", timestamp=1, affected_components=comps2),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    merged = result[0].affected_components
    assert "comp7" in merged  # no cap (8 unique kept)
    assert len(merged) == 8
    # case-insensitive dedup (COMP0 dropped) and first-seen order preserved
    lowered = [c.lower() for c in merged]
    assert len(lowered) == len(set(lowered))
    assert merged[0] == "comp0"


def test_base_is_earliest_and_tracks_merged_ids() -> None:
    findings = [
        _mk(2, "Same issue", category="ui", timestamp=20),
        _mk(1, "Same issue", category="ui", timestamp=5),
        _mk(3, "Same issue", category="ui", timestamp=25),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    merged = result[0]
    # base = earliest timestamp (detection_id 1 @ t=5)
    assert merged.detection_id == 1
    assert merged.timestamp == 5
    # merged_from_ids holds the other two members
    merged_ids = {did for did, _ts in merged.merged_from_ids}
    assert merged_ids == {2, 3}


def test_is_issue_is_true_if_any_member_is_issue() -> None:
    findings = [
        _mk(1, "Same issue", category="ui", timestamp=0, is_issue=False),
        _mk(2, "Same issue", category="ui", timestamp=1, is_issue=True),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    assert result[0].is_issue is True


def test_identical_summary_member_not_double_emitted() -> None:
    """BH1/P2-2: a finding pulled into an earlier similarity group must NOT be
    re-added when its identical-summary group is expanded later.

    Repro: B (id1) and A (id2) are similar + same category + within 30s, so B's
    Stage-2 similarity group claims A. C (id3) has the SAME summary as A but sits
    OUTSIDE B's 30s window, so it is not claimed by B and later triggers the
    identical-summary rebuild for key=A.summary=[A, C]. Without the `idx not in
    used` filter that rebuild re-adds A (already in B's group) → A is emitted in
    two output findings. Each input must be represented exactly once.
    """
    findings = [
        _mk(1, "The dropdown menu does not open on click", category="ui", timestamp=0),
        _mk(2, "The dropdown menu does not open on tap", category="ui", timestamp=10),
        _mk(3, "The dropdown menu does not open on tap", category="ui", timestamp=50),
    ]
    result = deduplicate_findings(findings)
    represented: list[int] = []
    for f in result:
        represented.append(f.detection_id)
        represented.extend(did for did, _ts in f.merged_from_ids)
    assert len(represented) == len(set(represented)), (
        f"a finding was double-emitted by dedup: {sorted(represented)}"
    )
    assert sorted(represented) == [1, 2, 3], (
        f"input findings not all represented once: {sorted(represented)}"
    )


def test_polish_stem_duplicates_merge_regression_guard() -> None:
    """R4-08 regression guard: PL inflected duplicates must merge.

    'rozwiń szufladę' vs 'rozwinąć szuflady' share the concepts {rozwin, szuflada}
    after canonical stemming. The inferior in-module copy lacked those stems, so
    the live dedup left them as two findings. With single-source normalisation
    they merge into one.
    """
    findings = [
        _mk(1, "Rozwiń szufladę z historią wizyt", category="ui", timestamp=0),
        _mk(2, "Rozwinąć szuflady z historii wizyty", category="ui", timestamp=10),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1, "PL inflected duplicates should merge after stem consolidation"


# --- C6.6: always-merge identical + confidence preservation ----------------


def _normalized_summaries(result: list[UnifiedFinding]) -> list[str]:
    return [" ".join(f.summary.lower().split()) for f in result]


def test_identical_summary_always_merges_even_when_similarity_steals_member() -> None:
    """A1/A2: an earlier similarity-stealer must not strand an identical-summary
    twin as a separate output finding.

    B (idx0, t=10) is similar to S and earlier-or-equal so it would, under the
    old single-pass code, steal A into its group with A as the merge base --
    keeping summary S -- while C (identical S, t=200, outside B's 30s window) was
    left standalone, ALSO with summary S. That emitted S in two output findings
    and split the identical pair A/C. Stage-1-first makes A and C always co-merge.
    """
    s = "The dropdown menu does not open on click"
    findings = [
        _mk(1, "Dropdown menu fails to open when clicked", category="ui", timestamp=10),
        _mk(2, s, category="ui", timestamp=0),
        _mk(3, s, category="ui", timestamp=200),
    ]
    result = deduplicate_findings(findings)

    # A1: normalized summaries are unique across output findings.
    norm = _normalized_summaries(result)
    assert len(norm) == len(set(norm)), f"identical summary emitted twice: {norm}"

    # A1: the two identical-summary inputs (id2, id3) live in the SAME finding.
    def finding_of(det_id: int) -> UnifiedFinding:
        for f in result:
            ids = {f.detection_id, *(d for d, _ts in f.merged_from_ids)}
            if det_id in ids:
                return f
        raise AssertionError(f"detection_id {det_id} not represented")

    assert finding_of(2) is finding_of(3), "identical-summary findings must co-merge"

    # A2: every input represented exactly once.
    represented: list[int] = []
    for f in result:
        represented.append(f.detection_id)
        represented.extend(d for d, _ts in f.merged_from_ids)
    assert sorted(represented) == [1, 2, 3]
    assert len(represented) == len(set(represented))


def test_merged_confidence_is_degraded_if_any_member_degraded() -> None:
    """A3: a merge that includes a degraded member must stay degraded, not be
    silently upgraded to the dataclass default 'high'."""
    findings = [
        _mk(1, "Same issue", category="ui", timestamp=0, confidence="high"),
        _mk(2, "Same issue", category="ui", timestamp=1, confidence="degraded"),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    assert result[0].confidence == "degraded"


def test_merged_confidence_stays_high_when_all_high() -> None:
    """A4: when every member is high, the merged finding stays high."""
    findings = [
        _mk(1, "Same issue", category="ui", timestamp=0, confidence="high"),
        _mk(2, "Same issue", category="ui", timestamp=1, confidence="high"),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    assert result[0].confidence == "high"


def test_merged_preserves_parsed_from_unstructured_flag() -> None:
    """A5: parsed_from_unstructured_output is reliability state; any True member
    keeps the merged finding True (not reset to the default False)."""
    findings = [
        _mk(1, "Same issue", category="ui", timestamp=0, parsed_from_unstructured_output=False),
        _mk(2, "Same issue", category="ui", timestamp=1, parsed_from_unstructured_output=True),
    ]
    result = deduplicate_findings(findings)
    assert len(result) == 1
    assert result[0].parsed_from_unstructured_output is True


def _mk_vision(
    detection_id: int,
    summary: str,
    *,
    timestamp: float,
    ui_elements: list[str],
    issues_detected: list[str],
    accessibility_notes: list[str],
    suggested_fix: str,
    design_feedback: str,
    technical_observations: str,
) -> UnifiedFinding:
    f = _mk(detection_id, summary, timestamp=timestamp)
    f.ui_elements = ui_elements
    f.issues_detected = issues_detected
    f.accessibility_notes = accessibility_notes
    f.suggested_fix = suggested_fix
    f.design_feedback = design_feedback
    f.technical_observations = technical_observations
    return f


def test_merge_unions_member_vision_details_not_just_base() -> None:
    """A merge keeps every member's vision evidence, not only the earliest base.

    Non-anchor members can carry unique issues_detected / ui_elements / notes /
    technical observations; a cross-category semantic merge used to keep these
    fields from the earliest base only, silently dropping evidence that is later
    serialized and shown/exported. They must be unioned (lists) / concatenated
    distinct (free text).
    """
    base = _mk_vision(
        1,
        "Same issue",
        timestamp=0,
        ui_elements=["submit button"],
        issues_detected=["contrast too low"],
        accessibility_notes=["missing label"],
        suggested_fix="increase contrast",
        design_feedback="cramped spacing",
        technical_observations="renders at 12px",
    )
    member = _mk_vision(
        2,
        "Same issue",
        timestamp=10,
        ui_elements=["nav link"],
        issues_detected=["focus ring absent"],
        accessibility_notes=["no aria-role"],
        suggested_fix="add focus ring",
        design_feedback="inconsistent radius",
        technical_observations="z-index conflict",
    )

    merged = merge_finding_group([base, member])

    # List fields: union of both members (order-preserving, base first).
    assert merged.ui_elements == ["submit button", "nav link"]
    assert merged.issues_detected == ["contrast too low", "focus ring absent"]
    assert merged.accessibility_notes == ["missing label", "no aria-role"]
    # Free-text fields: distinct contributions concatenated, not base-only.
    assert "increase contrast" in merged.suggested_fix and "add focus ring" in merged.suggested_fix
    assert (
        "cramped spacing" in merged.design_feedback
        and "inconsistent radius" in merged.design_feedback
    )
    assert "renders at 12px" in merged.technical_observations
    assert "z-index conflict" in merged.technical_observations


def test_merge_dedups_identical_vision_details() -> None:
    """Identical vision contributions collapse to one entry (no duplicate noise)."""
    a = _mk_vision(
        1,
        "Same issue",
        timestamp=0,
        ui_elements=["button"],
        issues_detected=["low contrast"],
        accessibility_notes=[],
        suggested_fix="fix contrast",
        design_feedback="",
        technical_observations="obs",
    )
    b = _mk_vision(
        2,
        "Same issue",
        timestamp=5,
        ui_elements=["button"],
        issues_detected=["low contrast"],
        accessibility_notes=[],
        suggested_fix="fix contrast",
        design_feedback="",
        technical_observations="obs",
    )
    merged = merge_finding_group([a, b])
    assert merged.ui_elements == ["button"]
    assert merged.issues_detected == ["low contrast"]
    assert merged.suggested_fix == "fix contrast"
    assert merged.technical_observations == "obs"
