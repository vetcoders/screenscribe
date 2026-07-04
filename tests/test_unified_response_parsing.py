"""Unit tests for unified response parsing/normalization — degraded paths.

BH56: a present-but-null model summary must trigger the deterministic local
fallback instead of surfacing the literal string "None".
"""

from __future__ import annotations

from screenscribe.unified.response_parsing import _clean_summary_response


def test_clean_summary_response_null_summary_falls_back_not_literal_none() -> None:
    """BH56: {"summary": null} must NOT yield the literal string 'None'; it returns
    '' so the caller triggers its deterministic local-summary fallback."""
    assert _clean_summary_response('{"summary": null}') == ""
    assert _clean_summary_response('{"summary": null, "action_items": []}') == ""
    # a real summary is still returned verbatim
    assert (
        _clean_summary_response('{"summary": "Real exec summary text."}')
        == "Real exec summary text."
    )


def test_clean_summary_action_items_header_follows_language() -> None:
    """When rebuilding a summary from action_items, the header follows the report
    language instead of the previously hardcoded Polish. EN users must not see a
    Polish header on an otherwise English report (FW-05 commit 3)."""
    payload = '{"action_items": ["Fix the click handler", "Add a test"]}'

    en = _clean_summary_response(payload, "en")
    assert en.startswith("Priority actions:")
    assert "Priorytetowe akcje:" not in en

    pl = _clean_summary_response(payload, "pl")
    assert pl.startswith("Priorytetowe akcje:")

    # Default (no language) stays English-neutral, not Polish.
    assert _clean_summary_response(payload).startswith("Priority actions:")
