"""Layer 2 — cross-tab review-state E2E (BH37).

Two tabs of the same report share one ``localStorage``: a write in tab B fires a
``storage`` event in tab A. The bug (BH37) was that tab A's handler hydrated the
incoming snapshot WHOLESALE, so a stale snapshot from tab B could overwrite a
fresher edit tab A had just made — silent loss of reviewer work.

The fix is last-writer-wins by ``savedAt`` (D-7): tab A applies an incoming
snapshot only when it is strictly newer than the state tab A last persisted.

These tests drive two real pages in one browser context (genuine ``storage``
events), and pin the incoming snapshot's ``savedAt`` explicitly so the staleness
comparison is deterministic regardless of wall-clock jitter.

Falsify: drop the ``savedAt`` compare in the sync ``storage`` handler and the
stale-snapshot test goes red (the fresh edit is gone).
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]


_WRITE_SYNC_ENVELOPE = """
([sourceId, savedAtMs, reviewer]) => {
    const key = 'screenscribe_state_' + reportState.reportId;
    const envelope = {
        sourceId,
        savedAt: new Date(savedAtMs).toISOString(),
        state: { ...buildPersistableState(false), reviewer },
    };
    // A same-page write does NOT fire `storage` in this page; it fires in the
    // OTHER tab of the same origin, which is exactly the cross-tab path we test.
    localStorage.setItem(key, JSON.stringify(envelope));
}
"""


def test_cross_tab_stale_snapshot_does_not_clobber_fresh_edit(
    review_server, browser_context
) -> None:
    page_a = browser_context.new_page()
    page_a.goto(review_server.url, wait_until="networkidle")
    page_b = browser_context.new_page()
    page_b.goto(review_server.url, wait_until="networkidle")

    # Tab A makes a fresh edit and persists it -> lastLocalSavedAt = now.
    page_a.evaluate(
        """() => {
            reportState.reviewer = 'FRESH_A';
            reportState.modified = true;
            const input = document.getElementById('reviewer-name');
            if (input) input.value = 'FRESH_A';
            persistSharedState();
        }"""
    )
    local_saved = page_a.evaluate("stateSyncRuntime.lastLocalSavedAt")
    assert isinstance(local_saved, (int, float)) and local_saved > 0, (
        f"tab A did not record a local savedAt watermark: {local_saved!r}"
    )

    # Tab B writes a STALE snapshot (older savedAt) carrying a different reviewer.
    # A real cross-tab `storage` event fires in tab A.
    page_b.evaluate(_WRITE_SYNC_ENVELOPE, ["STALE_TAB", local_saved - 60000, "STALE_B"])
    page_a.wait_for_timeout(750)  # let tab A's storage handler run

    assert page_a.evaluate("reportState.reviewer") == "FRESH_A", (
        "stale cross-tab snapshot clobbered a fresher local edit (BH37 regression)"
    )
    assert page_a.evaluate("document.getElementById('reviewer-name')?.value") == "FRESH_A", (
        "stale cross-tab snapshot overwrote the reviewer input (BH37 regression)"
    )

    # Positive control: a genuinely NEWER snapshot from another tab still applies,
    # so the fix is last-writer-wins, not a blanket "ignore cross-tab".
    page_b.evaluate(_WRITE_SYNC_ENVELOPE, ["FRESH_TAB", local_saved + 60000, "NEWER_B"])
    page_a.wait_for_function(
        "() => reportState.reviewer === 'NEWER_B'",
        timeout=5000,
    )
