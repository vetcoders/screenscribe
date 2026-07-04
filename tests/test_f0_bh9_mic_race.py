"""W1-1 BH9 — mic recorder press/release race canary.

The mic button's mousedown handler awaits an async ``recorder.start()``; a fast
press-and-release fires mouseup (stop) before start() resolves. The pre-fix code
ran stop() against a not-yet-started recorder, and the later-resolving start left
the mic recording forever. ``createMicPressController`` serializes both: exactly
one start, exactly one stop, and a release mid-start is deferred until start
resolves. The node ``vm`` harness loads ``review_app.js`` for real; revert the
guard and this canary goes red.
"""

from __future__ import annotations

from tests.test_f0_js_runtime_smoke import _run_review_app_smoke


def test_bh9_press_release_race_yields_one_start_one_stop() -> None:
    """A mouseup that lands before start() resolves must defer its stop."""
    _run_review_app_smoke(
        """
        let startCalls = 0, stopCalls = 0, resolveStart = null;
        const recorder = {
            start: () => { startCalls += 1; return new Promise((r) => { resolveStart = r; }); },
            stop: () => { stopCalls += 1; },
        };
        const ctrl = createMicPressController(recorder, () => {});

        const pressed = ctrl.press();   // start() now pending
        ctrl.release();                 // release BEFORE start resolves

        if (stopCalls !== 0) {
            console.error('stop ran before start resolved: ' + stopCalls);
            process.exitCode = 1;
        }

        resolveStart(true);             // start() resolves
        await pressed;

        if (startCalls !== 1) {
            console.error('expected exactly 1 start, got ' + startCalls);
            process.exitCode = 1;
        }
        if (stopCalls !== 1) {
            console.error('expected exactly 1 stop, got ' + stopCalls);
            process.exitCode = 1;
        }
        """
    )


def test_bh9_normal_press_then_release_records_then_stops() -> None:
    """The non-race path still records on press and stops on a later release."""
    _run_review_app_smoke(
        """
        let startCalls = 0, stopCalls = 0;
        const states = [];
        const recorder = {
            start: async () => { startCalls += 1; return true; },
            stop: () => { stopCalls += 1; },
        };
        const ctrl = createMicPressController(recorder, (rec) => states.push(rec));

        await ctrl.press();             // start resolves before release
        ctrl.release();

        if (startCalls !== 1 || stopCalls !== 1) {
            console.error('expected 1 start / 1 stop, got ' + startCalls + '/' + stopCalls);
            process.exitCode = 1;
        }
        if (states[0] !== true || states[states.length - 1] !== false) {
            console.error('recording flag did not go true then false: ' + JSON.stringify(states));
            process.exitCode = 1;
        }
        """
    )
