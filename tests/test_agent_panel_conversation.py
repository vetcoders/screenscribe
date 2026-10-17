"""Agent panel conversation UX: live streaming, Stop, timeout/retry, composer,
resize and dictation (node-vm harness from test_agent_panel)."""

from __future__ import annotations

import textwrap

from tests.test_agent_panel import _run_agent_panel

_STREAM_HELPERS = r"""
const enc = new TextEncoder();
function makeStream() {
    const queue = [];
    let waiter = null;
    let closed = false;
    const reader = {
        read() {
            if (queue.length) return Promise.resolve({ done: false, value: queue.shift() });
            if (closed) return Promise.resolve({ done: true });
            return new Promise((resolve) => { waiter = resolve; });
        },
        cancel() {
            closed = true;
            if (waiter) { const w = waiter; waiter = null; w({ done: true }); }
            return Promise.resolve();
        },
    };
    return {
        push(text) {
            const value = enc.encode(text);
            if (waiter) { const w = waiter; waiter = null; w({ done: false, value }); }
            else queue.push(value);
        },
        close() {
            closed = true;
            if (waiter) { const w = waiter; waiter = null; w({ done: true }); }
        },
        response: { ok: true, status: 200, body: { getReader() { return reader; } } },
    };
}
const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms || 0));
const token = (text) => 'event: token\ndata: ' + JSON.stringify({ text }) + '\n\n';
const DONE = 'event: done\ndata: {"response_id":"r1","provider":"primary","protocol":"responses","host":"h"}\n\n';
function rows(cls) {
    const log = document.getElementById('ss-agent-log');
    return (log.children || []).filter((n) => String(n.className).includes(cls));
}
"""


def _run(body: str) -> str:
    return _run_agent_panel(_STREAM_HELPERS + textwrap.dedent(body))


def test_first_token_renders_while_the_turn_is_still_streaming() -> None:
    output = _run(
        """
        const api = screenscribeAgentPanel;
        const s = makeStream();
        fetch = async () => s.response;
        window.fetch = fetch;
        api.init();
        api.setCollapsed(false);
        await api.whenThreadReady();
        const pending = api.send('What is on screen?');
        await tick(5);
        s.push(token('First '));
        await tick(5);
        const live = rows('ss-agent-msg-assistant');
        if (live.length !== 1 || live[0].textContent !== 'First ') {
            throw new Error('first token not rendered live: ' + JSON.stringify(live.map((n) => n.textContent)));
        }
        if (!live[0].classList.contains('ss-agent-msg-streaming')) throw new Error('no streaming marker');
        if (!api.getState().streaming) throw new Error('turn ended early');
        if (document.getElementById('ss-agent-stop').hidden) throw new Error('Stop hidden while streaming');
        if (document.getElementById('ss-agent-activity').hidden) throw new Error('responding bar hidden');
        if (!document.getElementById('ss-agent-send').disabled) throw new Error('Send enabled while streaming');
        if (document.getElementById('ss-agent-input').disabled) throw new Error('draft locked while streaming');
        s.push(token('answer.') + DONE);
        s.close();
        await pending;
        const done = rows('ss-agent-msg-assistant');
        if (done.length !== 1 || done[0].textContent !== 'First answer.') throw new Error('final text ' + done[0].textContent);
        if (done[0].classList.contains('ss-agent-msg-streaming')) throw new Error('streaming marker left behind');
        if (api.getState().streaming) throw new Error('still streaming');
        if (!document.getElementById('ss-agent-stop').hidden) throw new Error('Stop still visible');
        const hist = api.getState().history;
        if (hist[hist.length - 1].content !== 'First answer.') throw new Error('history ' + JSON.stringify(hist));
        if (api.getState().previousResponseId !== 'r1') throw new Error('cursor not kept');
        process.stdout.write('stream-live:ok');
        """
    )
    assert "stream-live:ok" in output


def test_stop_keeps_partial_text_and_marks_it_stopped() -> None:
    output = _run(
        """
        const api = screenscribeAgentPanel;
        const s = makeStream();
        fetch = async () => s.response;
        window.fetch = fetch;
        api.init();
        api.setCollapsed(false);
        await api.whenThreadReady();
        const pending = api.send('Explain finding 4');
        await tick(5);
        s.push(token('Partial answer'));
        await tick(5);
        if (!api.stop()) throw new Error('stop refused');
        await pending;
        const answer = rows('ss-agent-msg-assistant');
        if (answer.length !== 1 || answer[0].textContent !== 'Partial answer') throw new Error('partial lost');
        if (!answer[0].classList.contains('ss-agent-msg-interrupted')) throw new Error('not marked stopped');
        const badge = answer[0].querySelector('.ss-agent-badge');
        if (!badge || !/stopped/.test(badge.textContent)) throw new Error('no stopped badge');
        if (rows('ss-agent-msg-error').length) throw new Error('stop rendered as an error');
        const hist = api.getState().history;
        const last = hist[hist.length - 1];
        if (last.role !== 'assistant' || !last.content.startsWith('Partial answer') || !/stopped/.test(last.content)) {
            throw new Error('history ' + JSON.stringify(hist));
        }
        if (api.getState().previousResponseId !== null) throw new Error('cursor survived a stopped turn');
        if (api.getState().streaming) throw new Error('still streaming');
        process.stdout.write('stop:ok');
        """
    )
    assert "stop:ok" in output


def test_escape_in_the_composer_stops_a_streaming_turn() -> None:
    output = _run(
        """
        const api = screenscribeAgentPanel;
        const s = makeStream();
        fetch = async () => s.response;
        window.fetch = fetch;
        api.init();
        api.setCollapsed(false);
        await api.whenThreadReady();
        const pending = api.send('Long question');
        await tick(5);
        s.push(token('Working'));
        await tick(5);
        const input = document.getElementById('ss-agent-input');
        input.dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {} });
        await pending;
        if (api.getState().streaming) throw new Error('Escape did not stop');
        if (!rows('ss-agent-msg-interrupted').length) throw new Error('not marked stopped');
        process.stdout.write('escape-stop:ok');
        """
    )
    assert "escape-stop:ok" in output


def test_idle_timeout_keeps_partial_and_retry_does_not_duplicate() -> None:
    output = _run(
        """
        const api = screenscribeAgentPanel;
        window.__screenscribeAgentIdleTimeoutMs = 40;
        const first = makeStream();
        const second = makeStream();
        const bodies = [];
        fetch = async (_url, opts) => {
            bodies.push(JSON.parse(opts.body));
            return bodies.length === 1 ? first.response : second.response;
        };
        window.fetch = fetch;
        api.init();
        api.setCollapsed(false);
        await api.whenThreadReady();
        const pending = api.send('Summarise');
        await tick(5);
        first.push(token('Half of '));
        await pending;  // resolves once the idle timer aborts the dead stream
        const partial = rows('ss-agent-msg-assistant');
        if (partial.length !== 1 || partial[0].textContent !== 'Half of ') throw new Error('partial lost');
        if (!partial[0].classList.contains('ss-agent-msg-interrupted')) throw new Error('partial not marked');
        const errors = rows('ss-agent-msg-error');
        if (errors.length !== 1 || !/No response from the agent/.test(errors[0].textContent)) {
            throw new Error('timeout message missing: ' + JSON.stringify(errors.map((n) => n.textContent)));
        }
        if (api.getState().history.length !== 1) throw new Error('failed partial entered history');
        const retry = errors[0].querySelector('.ss-agent-retry');
        if (!retry) throw new Error('retry missing');
        window.__screenscribeAgentIdleTimeoutMs = 5000;
        second.push(token('Full answer.') + DONE);
        second.close();
        retry.click();
        await tick(30);
        if (bodies.length !== 2 || bodies[1].message !== 'Summarise') throw new Error('retry body');
        const answers = rows('ss-agent-msg-assistant');
        if (answers.length !== 1 || answers[0].textContent !== 'Full answer.') {
            throw new Error('duplicated answer rows: ' + JSON.stringify(answers.map((n) => n.textContent)));
        }
        if (rows('ss-agent-msg-error').length) throw new Error('stale error row');
        if (rows('ss-agent-msg-user').length !== 1) throw new Error('user bubble duplicated');
        const hist = api.getState().history;
        if (hist.length !== 2 || hist[1].content !== 'Full answer.') throw new Error('history ' + JSON.stringify(hist));
        process.stdout.write('timeout-retry:ok');
        """
    )
    assert "timeout-retry:ok" in output


def test_disconnect_mid_answer_reports_and_keeps_partial() -> None:
    output = _run(
        """
        const api = screenscribeAgentPanel;
        let reads = 0;
        const reader = {
            read() {
                reads += 1;
                if (reads === 1) return Promise.resolve({ done: false, value: enc.encode(token('Start')) });
                return Promise.reject(new TypeError('network error'));
            },
            cancel() { return Promise.resolve(); },
        };
        fetch = async () => ({ ok: true, status: 200, body: { getReader() { return reader; } } });
        window.fetch = fetch;
        api.init();
        await api.whenThreadReady();
        await api.send('Question');
        if (rows('ss-agent-msg-assistant')[0].textContent !== 'Start') throw new Error('partial lost');
        const error = rows('ss-agent-msg-error')[0];
        if (!error || !/Connection to the agent was lost: network error/.test(error.textContent)) {
            throw new Error('disconnect message: ' + (error && error.textContent));
        }
        if (!error.querySelector('.ss-agent-retry')) throw new Error('no retry');
        process.stdout.write('disconnect:ok');
        """
    )
    assert "disconnect:ok" in output


def test_composer_send_state_shortcuts_and_draft() -> None:
    output = _run(
        """
        const api = screenscribeAgentPanel;
        const draftKey = 'screenscribe:agent-panel:http://localhost/report.html:draft';
        localStorage.setItem(draftKey, 'restored draft');
        let calls = 0;
        fetch = async () => { calls += 1; return { ok: true, text: async () => DONE }; };
        window.fetch = fetch;
        api.init();
        api.setCollapsed(false);
        await api.whenThreadReady();
        const input = document.getElementById('ss-agent-input');
        const send = document.getElementById('ss-agent-send');
        if (input.value !== 'restored draft') throw new Error('draft not restored: ' + input.value);
        if (input.tagName !== 'TEXTAREA') throw new Error('not multiline');
        input.value = '';
        input.dispatchEvent({ type: 'input' });
        if (!send.disabled) throw new Error('Send enabled for an empty draft');
        if (localStorage.getItem(draftKey) !== null) throw new Error('empty draft persisted');
        input.value = 'line one';
        input.dispatchEvent({ type: 'input' });
        if (send.disabled) throw new Error('Send disabled with text');
        if (localStorage.getItem(draftKey) !== 'line one') throw new Error('draft not saved');
        input.dispatchEvent({ type: 'keydown', key: 'Enter', shiftKey: true, preventDefault() {} });
        input.dispatchEvent({ type: 'keydown', key: 'Enter', isComposing: true, preventDefault() {} });
        input.dispatchEvent({ type: 'keydown', key: 'Enter', keyCode: 229, preventDefault() {} });
        await tick(5);
        if (calls !== 0) throw new Error('Shift+Enter or IME Enter sent the message');
        input.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {} });
        await tick(10);
        if (calls !== 1) throw new Error('Enter did not send');
        if (input.value !== '') throw new Error('composer not cleared');
        if (localStorage.getItem(draftKey) !== null) throw new Error('sent draft kept');
        if (!send.disabled) throw new Error('Send enabled after clearing');
        const label = input.getAttribute('aria-label');
        if (!label) throw new Error('composer has no accessible label');
        if (input.getAttribute('aria-describedby') !== 'ss-agent-composer-hint') throw new Error('hint not linked');
        if (!document.getElementById('ss-agent-composer-hint').textContent.includes('Shift+Enter')) {
            throw new Error('shortcut hint missing');
        }
        process.stdout.write('composer:ok');
        """
    )
    assert "composer:ok" in output


def test_panel_resizes_within_viewport_bounds_and_persists() -> None:
    output = _run(
        """
        const api = screenscribeAgentPanel;
        api.init();
        api.setCollapsed(false);
        const key = 'screenscribe:agent-panel:http://localhost/report.html';
        let size = api.setPanelSize(5000, 5000);
        const b = api.sizeBounds();
        if (size.width !== b.maxWidth || size.height !== b.maxHeight) throw new Error('max clamp ' + JSON.stringify(size));
        if (b.maxWidth > 1280 - 16 || b.maxHeight > 800 - 16) throw new Error('bounds exceed viewport');
        size = api.setPanelSize(10, 10);
        if (size.width !== api.MIN_WIDTH || size.height !== api.MIN_HEIGHT) throw new Error('min clamp');
        api.setPanelSize(360, 480);
        const ev = (k, shift) => ({ key: k, shiftKey: Boolean(shift), preventDefault() {} });
        api.onResizeKey(ev('ArrowLeft'));
        if (api.panelSize().width !== 384) throw new Error('arrow widen ' + api.panelSize().width);
        api.onResizeKey(ev('ArrowLeft', true));
        if (api.panelSize().width !== 480) throw new Error('shift step');
        api.onResizeKey(ev('ArrowDown'));
        if (api.panelSize().height !== 504) throw new Error('arrow taller');
        api.onResizeKey(ev('Home'));
        if (api.panelSize().width !== api.MIN_WIDTH) throw new Error('Home');
        api.onResizeKey(ev('End'));
        if (api.panelSize().width !== b.maxWidth) throw new Error('End');
        if (api.onResizeKey(ev('a')) !== false) throw new Error('unrelated key handled');
        const saved = JSON.parse(localStorage.getItem(key));
        if (saved.width !== b.maxWidth || saved.height !== b.maxHeight) throw new Error('size not persisted');
        api.setPanelSize(360, 480);
        api.cycleSizePreset();
        if (api.panelSize().width !== 560) throw new Error('large preset');
        const sizeBtn = document.getElementById('ss-agent-size');
        if (!/large/.test(sizeBtn.getAttribute('aria-label'))) throw new Error('size label ' + sizeBtn.getAttribute('aria-label'));
        api.cycleSizePreset();
        if (api.panelSize().width !== 320) throw new Error('compact preset');
        api.cycleSizePreset();
        if (api.panelSize().width !== 360) throw new Error('default preset');
        api.startResize({ clientX: 1000, clientY: 500, preventDefault() {} });
        api.moveResize({ clientX: 900, clientY: 560 });
        if (api.panelSize().width !== 460 || api.panelSize().height !== 540) throw new Error('pointer resize ' + JSON.stringify(api.panelSize()));
        api.endResize();
        const handle = document.getElementById('ss-agent-resize');
        if (handle.getAttribute('role') !== 'separator' || handle.getAttribute('tabindex') !== '0') throw new Error('handle not keyboard reachable');
        if (!/460 \u00d7 540/.test(handle.getAttribute('aria-valuetext'))) throw new Error('valuetext ' + handle.getAttribute('aria-valuetext'));
        const rootEl = document.getElementById('ss-agent-root');
        if (!api.getState().sheet) {
            const left = parseFloat(rootEl.style.left);
            const top = parseFloat(rootEl.style.top);
            const panel = { left, top, right: left + 460, bottom: top + 540 };
            if (api.rectsOverlap(panel, api.protectedRect())) throw new Error('resized panel covers the player');
            if (panel.right > 1280 || panel.bottom > 800 || left < 0 || top < 0) throw new Error('outside viewport');
        }
        process.stdout.write('resize:ok');
        """
    )
    assert "resize:ok" in output


_MIC_STUB = r"""
const calls = [];
let created = null;
window.navigator = { mediaDevices: { getUserMedia: async () => ({}) } };
window.MediaRecorder = function () {};
window.MediaRecorder.isTypeSupported = (mime) => mime === 'audio/webm;codecs=opus';
window.ScreenScribeLib = {
    createSttTransport(opts) {
        created = opts;
        return {
            async start() { calls.push('start'); return START(opts); },
            stop() { calls.push('stop'); },
            cancel() { calls.push('cancel'); if (opts.onDiscard) opts.onDiscard(0); },
        };
    },
};
"""


def test_dictation_fills_an_editable_draft_and_never_sends() -> None:
    output = _run(
        "const START = (opts) => { opts.onRecordingStart(); return true; };\n"
        + _MIC_STUB
        + """
        const api = screenscribeAgentPanel;
        let fetches = 0;
        fetch = async () => { fetches += 1; throw new Error('must not send'); };
        window.fetch = fetch;
        api.init();
        api.setCollapsed(false);
        const input = document.getElementById('ss-agent-input');
        input.value = 'Please';
        input.dispatchEvent({ type: 'input' });
        const mic = document.getElementById('ss-agent-mic');
        const cancel = document.getElementById('ss-agent-mic-cancel');
        const status = document.getElementById('ss-agent-mic-status');
        await api.toggleMic();
        if (api.getState().mic.status !== 'recording') throw new Error('not recording: ' + api.getState().mic.status);
        if (mic.getAttribute('aria-pressed') !== 'true') throw new Error('mic not pressed');
        if (cancel.hidden) throw new Error('cancel hidden while recording');
        if (!/Recording/.test(status.textContent)) throw new Error('status ' + status.textContent);
        if (created.endpoint !== '/api/agent/stt') throw new Error('endpoint ' + created.endpoint);
        if (created.mimeType !== 'audio/webm;codecs=opus') throw new Error('format ' + created.mimeType);
        await api.toggleMic();
        if (calls.join(',') !== 'start,stop') throw new Error('calls ' + calls.join(','));
        if (api.getState().mic.status !== 'transcribing') throw new Error('not transcribing');
        if (!/Transcribing/.test(status.textContent)) throw new Error('transcribing status');
        created.onTranscript('check frame four');
        if (input.value !== 'Please check frame four') throw new Error('draft ' + JSON.stringify(input.value));
        if (fetches !== 0) throw new Error('dictation was sent');
        if (document.getElementById('ss-agent-send').disabled) throw new Error('Send not enabled for dictated draft');
        if (api.getState().mic.status !== 'idle') throw new Error('mic not idle');
        if (!/Edit it, then send/.test(status.textContent)) throw new Error('inserted status ' + status.textContent);
        if (!cancel.hidden) throw new Error('cancel left visible');
        await api.toggleMic();
        api.cancelMic();
        if (calls[calls.length - 1] !== 'cancel') throw new Error('cancel not forwarded');
        if (api.getState().mic.status !== 'idle' || !/cancelled/.test(status.textContent)) throw new Error('cancel state');
        if (input.value !== 'Please check frame four') throw new Error('cancel touched the draft');
        if (consoleCalls.log.length || consoleCalls.error.length) throw new Error('mic flow logged to console');
        process.stdout.write('dictation:ok');
        """
    )
    assert "dictation:ok" in output


def test_dictation_permission_denied_is_explained() -> None:
    output = _run(
        "const START = (opts) => { const e = new Error('denied'); e.name = 'NotAllowedError'; "
        "opts.onMicError(e); return false; };\n"
        + _MIC_STUB
        + """
        const api = screenscribeAgentPanel;
        api.init();
        api.setCollapsed(false);
        const started = await api.toggleMic();
        if (started) throw new Error('reported started');
        const state = api.getState().mic;
        if (state.status !== 'error') throw new Error('status ' + state.status);
        const status = document.getElementById('ss-agent-mic-status');
        if (!/Microphone access was blocked/.test(status.textContent)) throw new Error('message ' + status.textContent);
        if (status.getAttribute('data-tone') !== 'error') throw new Error('tone');
        process.stdout.write('denied:ok');
        """
    )
    assert "denied:ok" in output


def test_dictation_unavailable_without_media_apis() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            api.setCollapsed(false);
            const mic = document.getElementById('ss-agent-mic');
            if (mic.getAttribute('aria-disabled') !== 'true') throw new Error('mic not marked unavailable');
            await api.toggleMic();
            const status = document.getElementById('ss-agent-mic-status');
            if (api.getState().mic.status !== 'unavailable') throw new Error('status');
            if (!/not available in this browser/.test(status.textContent)) throw new Error('fallback text ' + status.textContent);
            process.stdout.write('unavailable:ok');
            """
        )
    )
    assert "unavailable:ok" in output
