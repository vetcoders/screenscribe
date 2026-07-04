"""W1-1 BH10 — manual-mark in-flight idempotency canary.

Add and Analyze can both call ``markManualFrame(current)`` before the first POST
resolves. The ``marker_id`` guard only catches calls that arrive AFTER the first
resolves, so the pre-fix code fired a second ``/api/manual-mark`` and created a
duplicate marker. The in-flight latch collapses concurrent callers to one POST.
Revert the latch and this canary goes red (two fetches).
"""

from __future__ import annotations

from tests.test_f0_js_runtime_smoke import _run_review_app_smoke


def test_bh10_concurrent_mark_creates_one_marker_one_fetch() -> None:
    """Two parallel markManualFrame() calls share one POST and one marker."""
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        flushSharedStateSync = () => {};

        // fetch resolves immediately (still after a microtask — await always
        // yields) so both concurrent calls have launched before either settles.
        // A pending-forever fetch would let node exit on an unresolved promise
        // and skip the assertions, so we resolve eagerly and assert on the count.
        let fetchCalls = 0;
        fetch = async () => {
            fetchCalls += 1;
            return { ok: true, status: 200, json: async () => ({ marker_id: 'm1' }) };
        };

        const current = { timestamp: 1, frameBase64: 'x', frameDataUrl: 'data:,' };
        const p1 = markManualFrame(current, 't', 'n');
        const p2 = markManualFrame(current, 't', 'n');  // concurrent, pre-resolve

        const [r1, r2] = await Promise.all([p1, p2]);

        if (fetchCalls !== 1) {
            console.error('expected exactly 1 fetch, got ' + fetchCalls);
            process.exitCode = 1;
        }
        if (r1 !== 'm1' || r2 !== 'm1') {
            console.error('both callers must return the single marker_id: ' + r1 + ',' + r2);
            process.exitCode = 1;
        }
        const rows = reportState.manualFrames.filter((f) => f.marker_id === 'm1');
        if (rows.length !== 1) {
            console.error('duplicate manual-frame rows created: ' + rows.length);
            process.exitCode = 1;
        }
        if (current.marker_id !== 'm1') {
            console.error('marker_id not recorded on the frame: ' + current.marker_id);
            process.exitCode = 1;
        }

        // A later call short-circuits on the recorded marker_id — still one fetch.
        const r3 = await markManualFrame(current, 't', 'n');
        if (r3 !== 'm1' || fetchCalls !== 1) {
            console.error('post-resolve call refetched: ' + r3 + ' / ' + fetchCalls);
            process.exitCode = 1;
        }
        """
    )
