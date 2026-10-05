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
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    marker_id: 'm1',
                    frame_path: 'manual_frames/m1.jpg',
                }),
            };
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
        if (rows[0].frame_path !== 'manual_frames/m1.jpg') {
            console.error('durable frame_path not recorded in client state: ' + rows[0].frame_path);
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


def test_manual_mark_started_before_reset_does_not_upsert_afterward() -> None:
    """A delayed mark response from an older reset generation is discarded."""
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        restoreUIFromState = () => {};
        restoreMergesToDom = () => {};
        initAnnotationTools = () => {};
        flushSharedStateSync = () => {};

        let resolveMark;
        fetch = async () => await new Promise((resolve) => { resolveMark = resolve; });
        let upserts = 0;
        upsertManualFrame = () => { upserts += 1; };

        const current = { timestamp: 1, frameBase64: 'x', frameDataUrl: 'data:,' };
        const pending = markManualFrame(current, 't', 'n');
        await Promise.resolve();

        hydrateReportState({
            findings: {}, manualFrames: [], reviewer: '', resetGeneration: 1,
        });
        resolveMark({
            ok: true,
            status: 200,
            json: async () => ({ marker_id: 'stale', resetGeneration: 0 }),
        });
        const markerId = await pending;

        if (markerId !== null || upserts !== 0 || current.marker_id !== undefined) {
            console.error('stale mark response resurrected the frame: '
                + JSON.stringify({ markerId, upserts, currentMarker: current.marker_id }));
            process.exitCode = 1;
        }
        """
    )


def test_manual_analysis_started_before_reset_does_not_upsert_afterward() -> None:
    """A delayed VLM result from an older reset generation is discarded."""
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        restoreUIFromState = () => {};
        restoreMergesToDom = () => {};
        initAnnotationTools = () => {};
        flushSharedStateSync = () => {};
        updateManualFrameMarker = async () => {};
        readManualFrameTranscript = () => 'spoken';
        setManualFrameStatus = () => {};
        showNotification = () => {};

        let resolveAnalyze;
        fetch = async (url) => {
            if (!url.startsWith('/api/manual-analyze/')) {
                throw new Error('unexpected fetch: ' + url);
            }
            return await new Promise((resolve) => { resolveAnalyze = resolve; });
        };
        let upserts = 0;
        upsertManualFrame = () => { upserts += 1; };

        const current = {
            marker_id: 'm1', _resetGeneration: 0,
            timestamp: 1, frameBase64: 'x', frameDataUrl: 'data:,',
        };
        manualFrameRuntime.currentFrame = current;
        const pending = analyzeManualFrame();
        for (let i = 0; i < 5 && typeof resolveAnalyze !== 'function'; i += 1) {
            await Promise.resolve();
        }
        if (typeof resolveAnalyze !== 'function') {
            throw new Error('analysis request did not reach the delayed fetch');
        }

        hydrateReportState({
            findings: {}, manualFrames: [], reviewer: '', resetGeneration: 1,
        });
        resolveAnalyze({
            ok: true,
            status: 200,
            json: async () => ({
                status: 'completed', resetGeneration: 0,
                result: { summary: 'stale result', severity: 'high' },
            }),
        });
        await pending;

        if (upserts !== 0 || reportState.manualFrames.length !== 0) {
            console.error('stale analysis result resurrected the frame');
            process.exitCode = 1;
        }
        """
    )


def test_manual_note_patch_started_before_reset_does_not_upsert_afterward() -> None:
    """A delayed note PATCH continuation cannot recreate a reset frame."""
    _run_review_app_smoke(
        """
        reportState.resetGeneration = 0;
        reportState.manualFrames = [
            { marker_id: 'm1', timestamp: 1, transcript: 'spoken', notes: 'old' },
        ];

        let resolvePatch;
        fetch = async () => await new Promise((resolve) => { resolvePatch = resolve; });
        let upserts = 0;
        let renders = 0;
        upsertManualFrame = () => { upserts += 1; };
        renderManualFrames = () => { renders += 1; };
        showNotification = () => {};

        const pending = updateManualFrameMarker('m1', 'spoken', 'stale edit');
        await Promise.resolve();
        reportState.resetGeneration = 1;
        reportState.manualFrames = [];
        resolvePatch({ ok: true, status: 200, json: async () => ({}) });
        await pending;

        if (upserts !== 0 || renders !== 0 || reportState.manualFrames.length !== 0) {
            console.error('stale note PATCH resurrected the frame: '
                + JSON.stringify({ upserts, renders, frames: reportState.manualFrames }));
            process.exitCode = 1;
        }
        """
    )


def test_manual_priority_patch_started_before_reset_does_not_upsert_afterward() -> None:
    """A delayed priority PATCH continuation cannot recreate a reset frame."""
    _run_review_app_smoke(
        """
        reportState.resetGeneration = 0;
        reportState.manualFrames = [
            { marker_id: 'm1', timestamp: 1, severity: 'high', result: { severity: 'high' } },
        ];

        let resolvePatch;
        fetch = async () => await new Promise((resolve) => { resolvePatch = resolve; });
        let upserts = 0;
        let renders = 0;
        upsertManualFrame = () => { upserts += 1; };
        renderManualFrames = () => { renders += 1; };
        showNotification = () => {};

        const pending = changeManualFrameSeverity('m1', 'low');
        await Promise.resolve();
        reportState.resetGeneration = 1;
        reportState.manualFrames = [];
        resolvePatch({ ok: true, status: 200, json: async () => ({}) });
        await pending;

        if (upserts !== 0 || renders !== 0 || reportState.manualFrames.length !== 0) {
            console.error('stale priority PATCH resurrected the frame: '
                + JSON.stringify({ upserts, renders, frames: reportState.manualFrames }));
            process.exitCode = 1;
        }
        """
    )
