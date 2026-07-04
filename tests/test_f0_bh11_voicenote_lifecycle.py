"""W1-1 BH11 — voice-note recognizer lifecycle canary.

``recognition.onend`` nulled the GLOBAL ``voiceNoteRuntime.recognition``. When a
second note starts before the first recognizer's onend fires, that late onend
wiped the new session's recognition/button/findingId. The fix only clears the
shared runtime when the firing recognizer is still the active one. Revert the
``=== recognition`` guard and this canary goes red.
"""

from __future__ import annotations

from tests.test_f0_js_runtime_smoke import _run_review_app_smoke


def test_bh11_old_recognizer_onend_does_not_clobber_new_session() -> None:
    """A late onend from a replaced recognizer must not wipe the new session."""
    _run_review_app_smoke(
        """
        const instances = [];
        function FakeRecognition() {
            this.continuous = false; this.interimResults = false; this.lang = '';
            this.onresult = null; this.onerror = null; this.onend = null;
            this.start = () => {};
            this.stop = () => {};
            instances.push(this);
        }
        window.SpeechRecognition = FakeRecognition;

        const mkBtn = () => ({ classList: { toggle() {} }, textContent: '' });
        const btn1 = mkBtn(), btn2 = mkBtn();

        startVoiceNoteCapture(btn1, 'f1');
        const rec1 = instances[0];
        startVoiceNoteCapture(btn2, 'f2');   // new session replaces the old
        const rec2 = instances[1];

        if (voiceNoteRuntime.recognition !== rec2) {
            console.error('new session did not become active');
            process.exitCode = 1;
        }

        // The OLD recognizer fires onend late (the browser delivers it after the
        // new session already started).
        rec1.onend();

        if (voiceNoteRuntime.recognition !== rec2) {
            console.error('late onend from old recognizer wiped the new recognition');
            process.exitCode = 1;
        }
        if (voiceNoteRuntime.activeFindingId !== 'f2') {
            console.error('late onend cleared the new findingId: ' + voiceNoteRuntime.activeFindingId);
            process.exitCode = 1;
        }
        if (voiceNoteRuntime.activeButton !== btn2) {
            console.error('late onend cleared the new active button');
            process.exitCode = 1;
        }

        // The active session's own onend still cleans up the shared runtime.
        rec2.onend();
        if (voiceNoteRuntime.recognition !== null
            || voiceNoteRuntime.activeButton !== null
            || voiceNoteRuntime.activeFindingId !== null) {
            console.error('active-session onend did not clear runtime: '
                + JSON.stringify({
                    rec: voiceNoteRuntime.recognition,
                    fid: voiceNoteRuntime.activeFindingId,
                }));
            process.exitCode = 1;
        }
        """
    )
