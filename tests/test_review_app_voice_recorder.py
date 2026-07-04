"""Browser-side voice recorder lifecycle regression tests."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
I18N_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/i18n.js"
LANGUAGE_CONTROL_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/lib/language-control.js"
STT_TRANSPORT_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/lib/stt-transport.js"
TAB_KEYBOARD_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/lib/tab-keyboard.js"
REVIEW_APP_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/review_app.js"


def _run_review_app_node_test(test_body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js lifecycle tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const i18nSource = fs.readFileSync({str(I18N_JS)!r}, 'utf8');
        const languageControlSource = fs.readFileSync({str(LANGUAGE_CONTROL_JS)!r}, 'utf8');
        const sttTransportSource = fs.readFileSync({str(STT_TRANSPORT_JS)!r}, 'utf8');
        const tabKeyboardSource = fs.readFileSync({str(TAB_KEYBOARD_JS)!r}, 'utf8');
        const source = fs.readFileSync({str(REVIEW_APP_JS)!r}, 'utf8');
        const sandbox = {{
            console,
            setTimeout,
            clearTimeout,
            Math,
            Date,
            URL,
            window: {{
                location: {{ search: '' }},
                addEventListener() {{}},
                removeEventListener() {{}},
                __screenscribeAllowProgrammaticClose: false,
            }},
            document: {{
                body: {{
                    dataset: {{ reportLanguage: 'en' }},
                    classList: {{ add() {{}}, remove() {{}} }},
                    contains() {{ return true; }},
                }},
                documentElement: {{ lang: 'en' }},
                addEventListener() {{}},
                querySelector() {{ return null; }},
                querySelectorAll() {{ return []; }},
                getElementById() {{ return null; }},
                createElement() {{
                    return {{
                        className: '',
                        textContent: '',
                        hidden: false,
                        style: {{}},
                        dataset: {{}},
                        classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                        appendChild() {{}},
                        remove() {{}},
                    }};
                }},
            }},
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            navigator: {{ mediaDevices: {{}} }},
            Blob: class Blob {{
                constructor(chunks, options = {{}}) {{
                    this.chunks = chunks;
                    this.type = options.type || '';
                }}
            }},
            FormData: class FormData {{
                constructor() {{ this.entries = []; }}
                append(...args) {{ this.entries.push(args); }}
            }},
            ResizeObserver: class ResizeObserver {{
                observe() {{}}
                disconnect() {{}}
            }},
            Image: class Image {{}},
            process,
            confirm() {{ return true; }},
            fetch() {{ throw new Error('fetch mock not installed'); }},
        }};
        sandbox.window.document = sandbox.document;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.globalThis = sandbox;

        const script = new vm.Script(
            i18nSource + "\\n" +
            languageControlSource + "\\n" +
            sttTransportSource + "\\n" +
            tabKeyboardSource + "\\n" +
            source + "\\n" +
            {test_body!r},
            {{
            filename: 'review_app.js',
        }});
        script.runInNewContext(sandbox);
        """
    )
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_review_timestamp_filename_normalization_is_consistent() -> None:
    """P3-14: timestamp->filename uses /[:.]/g everywhere, never single-colon.

    The annotated-screenshot path used `.replace(':', '-')`, which only swaps the
    FIRST colon — `HH:MM:SS` becomes `HH-MM:SS` (a stray colon in the filename,
    and inconsistent with the manual-frame path that uses /[:.]/g). Guard the
    source so the single-colon form does not creep back in.
    """
    source = REVIEW_APP_JS.read_text(encoding="utf-8")
    assert "replace(':', '-')" not in source, (
        "single-colon timestamp replace resurfaced (P3-14); use /[:.]/g"
    )
    # The annotated-filename builder must use the global character-class form.
    assert "f.timestamp_formatted.replace(/[:.]/g, '-')" in source, (
        "annotated screenshot filename no longer normalises with /[:.]/g"
    )


def test_review_recurring_timers_are_cleared_on_unload() -> None:
    """P3-6: the 30s auto-save interval + detached-window poll must be cleared.

    A bare `setInterval(saveDraft, 30000)` whose id is never stored leaks the
    timer (and stacks duplicates on any in-place re-init). The id must be
    captured in stateSyncRuntime.draftSaveTimer and torn down — together with the
    detached-window close poll — on beforeunload. This is a source-contract guard
    because exercising the full init/unload lifecycle needs a real browser
    (covered by browser smoke); the runtime behaviour is asserted by hand there.
    """
    source = REVIEW_APP_JS.read_text(encoding="utf-8")
    # The auto-save interval must be created exactly once, and that one creation
    # must capture its id into stateSyncRuntime.draftSaveTimer (no bare,
    # uncaptured `setInterval(saveDraft, ...)` statement).
    assert source.count("setInterval(saveDraft, 30000)") == 1, (
        "expected exactly one saveDraft interval creation"
    )
    assert "stateSyncRuntime.draftSaveTimer = window.setInterval(saveDraft, 30000)" in source, (
        "auto-save interval id is not captured (P3-6 leak); store it in "
        "stateSyncRuntime.draftSaveTimer"
    )
    # beforeunload must clear the auto-save timer and stop the detached poll.
    unload_idx = source.find("addEventListener('beforeunload'")
    assert unload_idx != -1, "beforeunload handler not found"
    unload_block = source[unload_idx : unload_idx + 600]
    assert "clearInterval(stateSyncRuntime.draftSaveTimer)" in unload_block, (
        "beforeunload does not clear the auto-save interval"
    )
    assert "stopDetachedWindowWatch()" in unload_block, (
        "beforeunload does not stop the detached-window close poll"
    )


def test_review_default_finding_state_has_single_notes_field() -> None:
    """P2-8/P3-13: the phantom `actionItems` field is gone.

    actionItems was initialised and re-joined into the notes textarea on
    restore, but nothing ever wrote it and buildReviewData never serialised it.
    The unified contract is a single `notes` field; the default finding state
    must not resurrect the phantom companion.
    """
    _run_review_app_node_test(
        """
        (() => {
            const state = createDefaultFindingState();
            const keys = Object.keys(state).sort();
            const expected = ['notes', 'severity', 'verdict'];
            if (keys.join(',') !== expected.join(',')) {
                throw new Error(
                    `default finding state keys = [${keys}], expected [${expected}]`
                );
            }
            if ('actionItems' in state) {
                throw new Error('phantom actionItems field still present');
            }
            if (state.notes !== '') throw new Error('notes default is not empty string');
        })();
        """
    )


def test_review_voice_recorder_stop_releases_audio_tracks() -> None:
    _run_review_app_node_test(
        """
        (async () => {
            let stopCalls = 0;
            const track = { stop() { stopCalls += 1; } };
            const stream = { getTracks() { return [track]; } };
            const statuses = [];

            navigator.mediaDevices.getUserMedia = async () => stream;
            fetch = async () => ({
                ok: true,
                status: 200,
                async json() { return { text: 'spoken note' }; },
            });

            class FakeMediaRecorder {
                constructor() {
                    this.ondataavailable = null;
                    this.onstop = null;
                    FakeMediaRecorder.instances.push(this);
                }
                start() {}
                stop() {
                    this.ondataavailable?.({ data: { size: 1 } });
                    this.stopPromise = Promise.resolve(this.onstop?.());
                }
            }
            FakeMediaRecorder.instances = [];
            MediaRecorder = FakeMediaRecorder;

            const recorder = new ReviewVoiceRecorder(
                () => {},
                (message, tone) => statuses.push([message, tone])
            );

            const started = await recorder.start();
            if (!started) throw new Error('recorder did not start');
            recorder.stop();
            await FakeMediaRecorder.instances[0].stopPromise;

            if (stopCalls !== 1) throw new Error(`expected track.stop once, got ${stopCalls}`);
            if (recorder.stream !== null) throw new Error('stream was not cleared');
            if (!statuses.some(([message]) => message === 'Recording...')) {
                throw new Error('missing recording status');
            }
            if (!statuses.some(([message]) => message === 'Transcribing...')) {
                throw new Error('missing transcribing status');
            }
            if (!statuses.some(([message]) => String(message).includes('Microphone off'))) {
                throw new Error('missing mic off status');
            }
        })().catch((error) => {
            console.error(error.stack || error.message);
            process.exitCode = 1;
        });
        """
    )


def test_review_voice_recorder_stop_releases_all_audio_tracks() -> None:
    _run_review_app_node_test(
        """
        (async () => {
            const stopCalls = [];
            const stream = {
                getAudioTracks() {
                    return [
                        { stop() { stopCalls.push('left'); } },
                        { stop() { stopCalls.push('right'); } },
                    ];
                },
                getTracks() {
                    throw new Error('audio recorder should release audio tracks directly');
                },
            };

            navigator.mediaDevices.getUserMedia = async () => stream;
            fetch = async () => ({
                ok: true,
                status: 200,
                async json() { return { text: 'spoken note' }; },
            });

            class FakeMediaRecorder {
                constructor() {
                    this.ondataavailable = null;
                    this.onstop = null;
                    FakeMediaRecorder.instances.push(this);
                }
                start() {}
                stop() {
                    this.ondataavailable?.({ data: { size: 1 } });
                    this.stopPromise = Promise.resolve(this.onstop?.());
                }
            }
            FakeMediaRecorder.instances = [];
            MediaRecorder = FakeMediaRecorder;

            const recorder = new ReviewVoiceRecorder(() => {}, () => {});

            await recorder.start();
            recorder.stop();
            await FakeMediaRecorder.instances[0].stopPromise;

            if (stopCalls.join(',') !== 'left,right') {
                throw new Error(`expected both audio tracks to stop, got ${stopCalls.join(',')}`);
            }
        })().catch((error) => {
            console.error(error.stack || error.message);
            process.exitCode = 1;
        });
        """
    )


def test_review_voice_recorder_stops_audio_tracks_after_stt_error() -> None:
    _run_review_app_node_test(
        """
        (async () => {
            let stopCalls = 0;
            const track = { stop() { stopCalls += 1; } };
            const stream = { getTracks() { return [track]; } };
            const statuses = [];

            navigator.mediaDevices.getUserMedia = async () => stream;
            fetch = async () => ({
                ok: false,
                status: 502,
                async json() { return { detail: 'STT unavailable' }; },
            });

            class FakeMediaRecorder {
                constructor() {
                    this.ondataavailable = null;
                    this.onstop = null;
                    FakeMediaRecorder.instances.push(this);
                }
                start() {}
                stop() {
                    this.ondataavailable?.({ data: { size: 1 } });
                    this.stopPromise = Promise.resolve(this.onstop?.());
                }
            }
            FakeMediaRecorder.instances = [];
            MediaRecorder = FakeMediaRecorder;

            const recorder = new ReviewVoiceRecorder(
                () => {},
                (message, tone) => statuses.push([message, tone])
            );

            await recorder.start();
            recorder.stop();
            await FakeMediaRecorder.instances[0].stopPromise;

            if (stopCalls !== 1) throw new Error(`expected track.stop once, got ${stopCalls}`);
            if (recorder.stream !== null) throw new Error('stream was not cleared after STT error');
            if (!statuses.some(([message, tone]) => String(message).includes('STT unavailable') && tone === 'error')) {
                throw new Error('missing STT error status');
            }
            if (!statuses.some(([message]) => String(message).includes('Microphone off'))) {
                throw new Error('missing mic off status after STT error');
            }
        })().catch((error) => {
            console.error(error.stack || error.message);
            process.exitCode = 1;
        });
        """
    )
