"""Tests for unified summary generation — P2-3 language-aware visual summary."""

from __future__ import annotations

from screenscribe.unified_analysis import UnifiedFinding, generate_visual_summary_unified


def _issue(issues: list[str]) -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=1,
        screenshot_path=None,
        timestamp=0.0,
        category="ui",
        is_issue=True,
        sentiment="problem",
        severity="medium",
        summary="x",
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=issues,
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
    )


def test_visual_summary_headers_follow_language() -> None:
    """P2-3: EN report must get EN headers, not the hardcoded Polish ones."""
    f = _issue(["Button overlaps text"])

    en = generate_visual_summary_unified([f], language="en")
    assert "## Visual analysis summary" in en
    assert "### Most common issues:" in en
    assert "Podsumowanie analizy wizualnej" not in en

    pl = generate_visual_summary_unified([f], language="pl")
    assert "## Podsumowanie analizy wizualnej" in pl
    assert "### Najczęstsze problemy:" in pl


def test_visual_summary_default_language_stays_pl_backward_compat() -> None:
    """Default (no language arg) preserves prior PL behaviour for any other caller."""
    f = _issue(["Button overlaps text"])
    default = generate_visual_summary_unified([f])
    assert "## Podsumowanie analizy wizualnej" in default
