"""Node-vm tests for the floating agent chat panel (w1-05-agent-floating)."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from screenscribe.html_pro.renderer import render_html_report_pro

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "screenscribe/html_pro_assets/scripts"
I18N_JS = SCRIPTS / "i18n.js"
AGENT_PANEL_JS = SCRIPTS / "agent_panel.js"
REVIEW_APP_JS = SCRIPTS / "review_app.js"
AGENT_PANEL_CSS = REPO_ROOT / "screenscribe/html_pro_assets/styles/agent_panel.css"

_CI_TRUE = {"1", "true", "yes", "on"}


def _resolve_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    where = "CI" if os.environ.get("CI", "").strip().lower() in _CI_TRUE else "local"
    pytest.fail(f"node is missing ({where}): agent_panel.js runtime tests must run, not skip")


_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const { TextDecoder } = require('util');

function makeClassList() {
    const values = new Set();
    return {
        add(...items) { items.forEach((v) => values.add(v)); },
        remove(...items) { items.forEach((v) => values.delete(v)); },
        contains(value) { return values.has(value); },
        toggle(value, force) {
            if (force === true) { values.add(value); return true; }
            if (force === false) { values.delete(value); return false; }
            if (values.has(value)) { values.delete(value); return false; }
            values.add(value); return true;
        },
        values,
        toString() { return [...values].join(' '); },
    };
}

function makeElement(tag) {
    const listeners = {};
    const attrs = {};
    const children = [];
    const el = {
        tagName: String(tag || 'div').toUpperCase(),
        children,
        parentNode: null,
        attributes: attrs,
        dataset: {},
        style: {
            setProperty(name, value) { this[name] = String(value); },
            removeProperty(name) { delete this[name]; },
        },
        classList: makeClassList(),
        className: '',
        id: '',
        hidden: false,
        disabled: false,
        textContent: '',
        value: '',
        placeholder: '',
        innerHTML: '',
        scrollTop: 0,
        scrollHeight: 0,
        addEventListener(type, fn) {
            (listeners[type] || (listeners[type] = [])).push(fn);
        },
        removeEventListener() {},
        dispatchEvent(event) {
            const type = event && event.type;
            (listeners[type] || []).forEach((fn) => fn(event));
            return true;
        },
        setAttribute(name, value) {
            attrs[name] = String(value);
            if (name === 'id') el.id = String(value);
            if (name === 'hidden') el.hidden = true;
        },
        getAttribute(name) { return attrs[name] == null ? null : attrs[name]; },
        appendChild(child) {
            child.parentNode = el;
            children.push(child);
            if (child.id) registry.set(child.id, child);
            return child;
        },
        removeChild(child) {
            const idx = children.indexOf(child);
            if (idx >= 0) children.splice(idx, 1);
            if (child) child.parentNode = null;
            return child;
        },
        requestSubmit() {
            (listeners.submit || []).forEach((fn) => fn({ preventDefault() {} }));
        },
        querySelector(sel) { return query(el, sel, true); },
        querySelectorAll(sel) { return query(el, sel, false); },
        closest(sel) { return matches(el, sel) ? el : (el.parentNode && el.parentNode.closest ? el.parentNode.closest(sel) : null); },
        getBoundingClientRect() {
            return el._rect || { left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100 };
        },
        scrollIntoView() { el._scrolled = true; },
        _listeners: listeners,
        click() { (listeners.click || []).forEach((fn) => fn({ preventDefault() {} })); },
        submit() { (listeners.submit || []).forEach((fn) => fn({ preventDefault() {} })); },
    };
    Object.defineProperty(el, 'className', {
        get() { return el.classList.toString(); },
        set(v) {
            el.classList.values.clear();
            String(v || '').split(/\s+/).filter(Boolean).forEach((c) => el.classList.add(c));
        },
    });
    return el;
}

function matches(node, selector) {
    if (!selector) return false;
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    if (selector.startsWith('.')) return node.classList.contains(selector.slice(1));
    if (selector.includes('[')) {
        const m = selector.match(/\[data-finding-id="([^"]+)"\]/);
        if (m) return String(node.dataset.findingId) === m[1] && (!selector.startsWith('.finding') || node.classList.contains('finding'));
        const ts = selector.match(/\[data-timestamp\]/);
        if (ts) return node.dataset.timestamp != null;
    }
    if (selector === node.tagName.toLowerCase()) return true;
    if (selector.includes('.')) {
        const [tag, cls] = selector.split('.');
        if (tag && node.tagName !== tag.toUpperCase()) return false;
        return node.classList.contains(cls.split('[')[0]);
    }
    return false;
}

function walk(node, out) {
    out.push(node);
    (node.children || []).forEach((child) => walk(child, out));
}

function query(rootEl, selector, first) {
    const all = [];
    walk(rootEl, all);
    const hit = all.filter((n) => n !== rootEl && matches(n, selector.split(/\s+/).pop()));
    if (selector.includes(' ')) {
        const parts = selector.trim().split(/\s+/);
        const filtered = all.filter((n) => {
            if (!matches(n, parts[parts.length - 1])) return false;
            let cur = n.parentNode;
            for (let i = parts.length - 2; i >= 0; i--) {
                while (cur && !matches(cur, parts[i])) cur = cur.parentNode;
                if (!cur) return false;
                cur = cur.parentNode;
            }
            return true;
        });
        return first ? (filtered[0] || null) : filtered;
    }
    return first ? (hit[0] || null) : hit;
}

const registry = new Map();
const body = makeElement('body');
body.dataset = { reportLanguage: 'en', mode: 'review' };
const htmlEl = makeElement('html');
htmlEl.lang = 'en';
const video = makeElement('video');
video.id = 'videoPlayer';
video._rect = { left: 20, top: 80, right: 520, bottom: 400, width: 500, height: 320 };
registry.set('videoPlayer', video);
body.appendChild(video);
const toolbar = makeElement('div');
toolbar.id = 'videoControls';
toolbar.className = 'video-controls-pro';
toolbar._rect = { left: 28, top: 48, right: 420, bottom: 80, width: 392, height: 32 };
registry.set('videoControls', toolbar);
body.appendChild(toolbar);

const finding = makeElement('article');
finding.className = 'finding';
finding.id = 'finding-1';
finding.dataset.findingId = '1';
const meta = makeElement('div');
meta.className = 'finding-meta';
meta.dataset.timestamp = '12.5';
finding.appendChild(meta);
const thumb = makeElement('div');
thumb.className = 'annotation-container';
finding.appendChild(thumb);
body.appendChild(finding);

const docListeners = {};
const documentRef = {
    body,
    documentElement: htmlEl,
    readyState: 'complete',
    addEventListener(type, fn) {
        (docListeners[type] || (docListeners[type] = [])).push(fn);
    },
    dispatchEvent(event) {
        const type = event && event.type;
        (docListeners[type] || []).forEach((fn) => fn(event));
        return true;
    },
    getElementById(id) { return registry.get(id) || null; },
    querySelector(sel) {
        if (sel.startsWith('#')) return registry.get(sel.slice(1)) || query(body, sel, true);
        return query(body, sel, true);
    },
    querySelectorAll(sel) { return query(body, sel, false); },
    createElement(tag) { return makeElement(tag); },
};

const store = new Map();
const localStorage = {
    getItem(key) { return store.has(key) ? store.get(key) : null; },
    setItem(key, value) { store.set(key, String(value)); },
    removeItem(key) { store.delete(key); },
    _store: store,
};

const consoleCalls = { error: [], warn: [], log: [] };
const sandbox = {
    console: {
        error: (...a) => { consoleCalls.error.push(a); },
        warn: (...a) => { consoleCalls.warn.push(a); },
        log: (...a) => { consoleCalls.log.push(a); },
    },
    consoleCalls,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    AbortController,
    TextDecoder,
    TextEncoder,
    JSON,
    Math,
    Date,
    Object,
    Number,
    String,
    Boolean,
    Array,
    Error,
    TypeError,
    Promise,
    Map,
    Set,
    localStorage,
    window: {},
    document: documentRef,
    location: { protocol: 'http:', origin: 'http://localhost', pathname: '/report.html', href: 'http://localhost/report.html' },
    innerWidth: 1280,
    innerHeight: 800,
    fetch: async () => { throw new Error('unexpected fetch'); },
    process,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.window.document = documentRef;
sandbox.window.location = sandbox.location;
sandbox.window.localStorage = localStorage;
sandbox.window.innerWidth = sandbox.innerWidth;
sandbox.window.innerHeight = sandbox.innerHeight;
sandbox.window.fetch = (...args) => sandbox.fetch(...args);
sandbox.window.addEventListener = (type, fn) => {
    (sandbox._windowListeners[type] || (sandbox._windowListeners[type] = [])).push(fn);
};
sandbox._windowListeners = {};
sandbox.window.dispatchEvent = (event) => {
    (sandbox._windowListeners[event.type] || []).forEach((fn) => fn(event));
    return true;
};
sandbox.document.defaultView = sandbox.window;
sandbox.__screenscribeAgentPanelSkipInit = true;

const sources = [
    fs.readFileSync(I18N_PATH, 'utf8'),
    fs.readFileSync(AGENT_PATH, 'utf8'),
].join('\n');
vm.runInNewContext(sources, sandbox, { filename: 'agent_panel.js' });
"""


def _run_agent_panel(test_body: str) -> str:
    node = _resolve_node()
    runner = (
        f"const I18N_PATH = {str(I18N_JS)!r};\n"
        f"const AGENT_PATH = {str(AGENT_PANEL_JS)!r};\n"
        + _HARNESS
        + "\nconst testBody = "
        + repr(test_body)
        + ";\n"
        + textwrap.dedent(
            """
            const script = new vm.Script(
                '(async () => {\\n' + testBody + '\\n})()',
                { filename: 'agent_panel_test.js' }
            );
            (async () => {
                const result = script.runInNewContext(sandbox);
                if (result && typeof result.then === 'function') await result;
            })().catch((error) => {
                console.error(error && error.stack || error);
                process.exitCode = 1;
            });
            """
        )
    )
    completed = subprocess.run(
        [node, "-e", runner],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            "agent_panel.js node harness failed:\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def test_toggle_and_persist() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            const rootEl = document.getElementById('ss-agent-root');
            const fab = document.getElementById('ss-agent-fab');
            if (!rootEl || !fab) throw new Error('fab/root missing');
            if (!rootEl.classList.contains('ss-agent-collapsed')) throw new Error('should start collapsed');
            fab.click();
            const after = api.getState();
            if (after.collapsed) throw new Error('click should expand');
            if (!rootEl.classList.contains('ss-agent-expanded')) throw new Error('expanded class missing');
            const saved = JSON.parse(localStorage.getItem(Object.fromEntries(localStorage._store) && [...localStorage._store.keys()][0]));
            if (saved.collapsed !== false) throw new Error('persist collapsed=false');
            if (!Number.isFinite(saved.left) || !Number.isFinite(saved.top)) throw new Error('persist position');
            // New session restores expanded state + last position.
            sandboxReload = true;
            const key = [...localStorage._store.keys()][0];
            const snapshot = localStorage.getItem(key);
            document.getElementById('ss-agent-root').parentNode.children.length;
            localStorage._store.clear();
            localStorage.setItem(key, snapshot);
            const state = api.getState();
            state.collapsed = true;
            state.left = null;
            state.top = null;
            api.restore();
            api.setCollapsed(api.getState().collapsed);
            if (api.getState().collapsed !== false) throw new Error('restore should reopen');
            process.stdout.write('toggle-persist:ok');
            """
        )
    )
    assert "toggle-persist:ok" in output


def test_sse_and_tools() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const stream = [
                'event: token',
                'data: {"text":"Hello "}',
                '',
                'event: token',
                'data: {"text":"world"}',
                '',
                'event: tool_call',
                'data: {"name":"seek","input":{"timestamp":12.5}}',
                '',
                'event: tool_result',
                'data: {"name":"seek","result":{"action":"seek","timestamp":12.5}}',
                '',
                'event: tool_call',
                'data: {"name":"show_frame","input":{"finding_id":"1","timestamp":12.5}}',
                '',
                'event: done',
                'data: {"response_id":"resp_1"}',
                '',
                'event: error',
                'data: {"message":"egress deny: external provider blocked"}',
                '',
            ].join('\\n');
            const events = api.parseSseStream(stream);
            const names = events.map((e) => e.event);
            if (names.join(',') !== 'token,token,tool_call,tool_result,tool_call,done,error') {
                throw new Error('event set: ' + names.join(','));
            }
            const seeks = [];
            const frames = [];
            api.applyToolCall('seek', { timestamp: 12.5 }, { seek: (t) => seeks.push(t) });
            api.applyToolCall('show_frame', { finding_id: '1', timestamp: 12.5 }, { showFrame: (p) => frames.push(p) });
            if (seeks[0] !== 12.5) throw new Error('seek mapping');
            if (frames[0].finding_id !== '1') throw new Error('show_frame mapping');
            // Player-host mapping used by review_app.js hook.
            const hostCalls = [];
            window.__screenscribeAgentHost = {
                seek(ts) { hostCalls.push(['seek', ts]); },
                showFrame(input) { hostCalls.push(['show_frame', input.finding_id]); },
            };
            api.applyToolCall('seek', { timestamp: 4 });
            api.applyToolCall('show_frame', { finding_id: '9' });
            if (hostCalls[0][0] !== 'seek' || hostCalls[0][1] !== 4) throw new Error('host seek');
            if (hostCalls[1][0] !== 'show_frame' || hostCalls[1][1] !== '9') throw new Error('host frame');
            process.stdout.write('sse-tools:ok');
            """
        )
    )
    assert "sse-tools:ok" in output


def test_streaming_sse_accepts_crlf_delimiters() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const bytes = new TextEncoder().encode(
                'event: token\\r\\ndata: {"text":"CRLF works"}\\r\\n\\r\\n'
                + 'event: done\\r\\ndata: {}\\r\\n\\r\\n'
            );
            let reads = 0;
            fetch = async () => ({
                ok: true,
                body: { getReader() { return { async read() {
                    reads += 1;
                    return reads === 1 ? { done: false, value: bytes } : { done: true };
                } }; } },
            });
            window.fetch = fetch;
            api.init();
            await api.send('test CRLF');
            const log = document.getElementById('ss-agent-log');
            const text = (log.children || []).map((n) => n.textContent).join('|');
            if (!/CRLF works/.test(text)) throw new Error('CRLF event was not emitted: ' + text);
            process.stdout.write('sse-crlf:ok');
            """
        )
    )
    assert "sse-crlf:ok" in output


def test_offline_mode() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            location.protocol = 'file:';
            window.location.protocol = 'file:';
            const api = screenscribeAgentPanel;
            api.init();
            const beforeErrors = consoleCalls.error.length;
            await api.send('What is critical in this recording?');
            if (consoleCalls.error.length !== beforeErrors) {
                throw new Error('console.error during offline send');
            }
            const notice = document.getElementById('ss-agent-offline');
            const text = (notice && notice.textContent) || '';
            if (!/screenscribe review/.test(text) || !/screenscribe analyze/.test(text)) {
                throw new Error('offline copy: ' + text);
            }
            if (notice.hidden) throw new Error('offline notice should be visible');
            process.stdout.write('offline:ok');
            """
        )
    )
    assert "offline:ok" in output


def test_static_demo_is_offline_even_over_http() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            document.body.dataset.staticDemo = 'true';
            let fetches = 0;
            fetch = async () => { fetches += 1; throw new Error('must stay offline'); };
            window.fetch = fetch;
            const api = screenscribeAgentPanel;
            api.init();
            if (!api.isOffline()) throw new Error('static demo not classified offline');
            await api.send('do not post');
            if (fetches !== 0) throw new Error('static demo attempted network: ' + fetches);
            process.stdout.write('static-offline:ok');
            """
        )
    )
    assert "static-offline:ok" in output


def test_geometry_beside_player_stays_in_viewport() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const player = { left: 20, top: 80, right: 520, bottom: 400, width: 500, height: 320 };
            const viewport = { width: 1280, height: 800 };
            const placed = api.placeBesidePlayer(player, viewport, { width: 360, height: 480 });
            const panel = { left: placed.left, top: placed.top, right: placed.left + 360, bottom: placed.top + 480 };
            const overlap = panel.left < player.right && panel.right > player.left && panel.top < player.bottom && panel.bottom > player.top;
            if (overlap) throw new Error('panel overlaps player at ' + JSON.stringify(placed));
            if (placed.left < 0 || placed.top < 0) throw new Error('negative pos');
            if (placed.left + 360 > viewport.width + 0.5) throw new Error('right overflow');
            if (placed.top + 480 > viewport.height + 0.5) throw new Error('bottom overflow');
            const clamped = api.clampToViewport(5000, 5000, 360, 480, 1280, 800);
            if (clamped.left + 360 > 1280 || clamped.top + 480 > 800) throw new Error('clamp failed');
            process.stdout.write('geometry:ok left=' + placed.left + ' top=' + placed.top);
            """
        )
    )
    assert "geometry:ok" in output


def test_review_app_exposes_player_hook() -> None:
    source = REVIEW_APP_JS.read_text(encoding="utf-8")
    assert "window.__screenscribeAgentHost" in source
    assert "showAgentFrame" in source
    assert "seek: seekToTimestamp" in source
    assert "applyReviewPatch" in source
    assert "saveReview: saveReviewToDisk" in source


def test_rendered_report_includes_agent_panel_assets() -> None:
    html = render_html_report_pro(
        video_name="agent.mov",
        video_path=None,
        generated_at="2026-09-15T00:00:00",
        executive_summary="",
        findings=[],
        segments=[],
        errors=[],
        language="en",
    )
    assert "screenscribeAgentPanel" in html
    assert "ss-agent-fab" in html or "ss-agent-root" in html
    assert ".ss-agent-fab" in html
    assert "screenscribe review" in html
    assert "screenscribe analyze &lt;video&gt;" in html or "screenscribe analyze <video>" in html
    assert "cdnjs.cloudflare.com" not in html


def test_agent_panel_runtime_builds_accessible_live_chat_controls() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            const log = document.getElementById('ss-agent-log');
            const input = document.getElementById('ss-agent-input');
            const offline = document.getElementById('ss-agent-offline');
            if (log.getAttribute('role') !== 'log') throw new Error('chat log role missing');
            if (log.getAttribute('aria-live') !== 'polite') throw new Error('chat log live region missing');
            if (!input.getAttribute('aria-label')) throw new Error('composer accessible name missing');
            if (offline.getAttribute('data-i18n') !== 'agentOffline') throw new Error('offline notice not translatable');
            process.stdout.write('a11y:ok');
            """
        )
    )
    assert "a11y:ok" in output


def test_enter_sends_and_respects_ime_shift_and_whitespace() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const fetches = [];
            fetch = async (url, opts) => {
                fetches.push({ url, body: JSON.parse(opts.body) });
                return {
                    ok: true,
                    text: async () => 'event: token\\ndata: {"text":"ok"}\\n\\nevent: done\\ndata: {}\\n\\n',
                };
            };
            window.fetch = fetch;
            api.init();
            const form = document.getElementById('ss-agent-form');
            const input = document.getElementById('ss-agent-input');

            function fireKey(partial) {
                const event = Object.assign({
                    key: 'Enter',
                    shiftKey: false,
                    isComposing: false,
                    keyCode: 13,
                    preventDefault() { event._prevented = true; },
                    _prevented: false,
                }, partial);
                input.dispatchEvent(Object.assign({ type: 'keydown' }, event));
                return event;
            }

            input.value = 'hello from enter';
            const sent = fireKey({});
            if (!sent._prevented) throw new Error('Enter must preventDefault');
            await new Promise((resolve) => setTimeout(resolve, 20));
            if (input.value !== '') throw new Error('input should clear after send starts');
            if (fetches.length !== 1) throw new Error('Enter should send once, got ' + fetches.length);
            if (fetches[0].body.message !== 'hello from enter') throw new Error('message');

            input.value = 'keep newline';
            const shifted = fireKey({ shiftKey: true });
            if (shifted._prevented) throw new Error('Shift+Enter must not send');
            if (input.value !== 'keep newline') throw new Error('Shift+Enter must not clear');
            if (fetches.length !== 1) throw new Error('Shift+Enter sent');

            input.value = 'ime text';
            const ime = fireKey({ isComposing: true, keyCode: 229 });
            if (ime._prevented) throw new Error('IME Enter must not send');
            if (input.value !== 'ime text') throw new Error('IME must not clear');
            if (fetches.length !== 1) throw new Error('IME sent');

            const imeKey = fireKey({ key: 'Enter', keyCode: 229, isComposing: false });
            if (imeKey._prevented) throw new Error('keyCode 229 must not send');
            if (fetches.length !== 1) throw new Error('keyCode 229 sent');

            input.value = '   \\t  ';
            form.submit();
            await new Promise((resolve) => setTimeout(resolve, 20));
            if (input.value !== '   \\t  ') throw new Error('whitespace-only must not clear');
            if (fetches.length !== 1) throw new Error('whitespace-only sent');

            process.stdout.write('enter:ok');
            """
        )
    )
    assert "enter:ok" in output


def test_error_turn_reuses_assistant_bubble() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            fetch = async () => ({
                ok: true,
                text: async () => 'event: error\\ndata: {"message":"egress deny: external provider blocked"}\\n\\n',
            });
            window.fetch = fetch;
            api.init();
            await api.send('What broke?');
            const log = document.getElementById('ss-agent-log');
            const msgs = (log.children || []).filter((n) => String(n.className || '').includes('ss-agent-msg'));
            if (msgs.length !== 2) throw new Error('expected user+error, got ' + msgs.length + ' ' + msgs.map((m) => m.className + ':' + m.textContent).join('|'));
            if (!String(msgs[0].className).includes('ss-agent-msg-user')) throw new Error('first should be user');
            if (!String(msgs[1].className).includes('ss-agent-msg-error')) throw new Error('error class: ' + msgs[1].className);
            if (!msgs[1].textContent.includes('egress deny: external provider blocked')) throw new Error('error text: ' + msgs[1].textContent);
            if (!msgs[1].querySelector('.ss-agent-retry')) throw new Error('retry control missing');
            const input = document.getElementById('ss-agent-input');
            if (!input || input.disabled) throw new Error('composer dead after error');
            if (api.getState().streaming) throw new Error('streaming stuck after error');
            const emptyAssistant = msgs.filter((m) => String(m.className).includes('ss-agent-msg-assistant') && !String(m.textContent || '').trim());
            if (emptyAssistant.length) throw new Error('empty assistant bubble remained');
            process.stdout.write('error-bubble:ok');
            """
        )
    )
    assert "error-bubble:ok" in output


def test_cursor_identity_round_trips_then_fallback_clears_without_losing_history() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const bodies = [];
            const replies = [
                'event: token\\ndata: {"text":"primary answer"}\\n\\n'
                    + 'event: done\\ndata: {"response_id":"resp-primary","provider":"primary","protocol":"responses","host":"api.x.ai"}\\n\\n',
                'event: token\\ndata: {"text":"fallback answer"}\\n\\n'
                    + 'event: done\\ndata: {"response_id":null,"provider":"fallback","protocol":"anthropic","host":"api.anthropic.com"}\\n\\n',
                'event: token\\ndata: {"text":"fresh answer"}\\n\\n'
                    + 'event: done\\ndata: {"response_id":"resp-fresh","provider":"primary","protocol":"responses","host":"api.x.ai"}\\n\\n',
            ];
            fetch = async (_url, opts) => {
                bodies.push(JSON.parse(opts.body));
                return { ok: true, text: async () => replies.shift() };
            };
            window.fetch = fetch;
            api.init();

            await api.send('first');
            const firstCursor = api.getState();
            if (firstCursor.previousResponseId !== 'resp-primary'
                || firstCursor.previousResponseIdentity.host !== 'api.x.ai') {
                throw new Error('primary cursor identity not retained: ' + JSON.stringify(firstCursor));
            }

            await api.send('second');
            if (bodies[1].previous_response_id !== 'resp-primary'
                || bodies[1].previous_response_provider !== 'primary'
                || bodies[1].previous_response_protocol !== 'responses'
                || bodies[1].previous_response_host !== 'api.x.ai') {
                throw new Error('identity bundle missing from next request: ' + JSON.stringify(bodies[1]));
            }
            if (api.getState().previousResponseId !== null
                || api.getState().previousResponseIdentity !== null) {
                throw new Error('fallback cursor was retained');
            }

            await api.send('third');
            if (bodies[2].previous_response_id !== null
                || bodies[2].previous_response_provider !== null
                || bodies[2].previous_response_protocol !== null
                || bodies[2].previous_response_host !== null) {
                throw new Error('cleared cursor leaked into fresh request: ' + JSON.stringify(bodies[2]));
            }
            const history = api.getState().history.map((item) => item.content).join('|');
            for (const expected of ['first', 'primary answer', 'second', 'fallback answer', 'third', 'fresh answer']) {
                if (!history.includes(expected)) throw new Error('history lost ' + expected + ': ' + history);
            }
            process.stdout.write('cursor-identity:ok');
            """
        )
    )
    assert "cursor-identity:ok" in output


def test_legacy_done_provider_change_and_error_clear_cursor() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const cases = [
                'event: done\\ndata: {"response_id":"legacy-no-identity"}\\n\\n',
                'event: done\\ndata: {"response_id":"other-host","provider":"primary","protocol":"responses","host":"other.example"}\\n\\n',
                'event: error\\ndata: {"message":"provider failed"}\\n\\n',
            ];
            fetch = async () => ({ ok: true, text: async () => cases.shift() });
            window.fetch = fetch;
            api.init();
            for (const message of ['legacy', 'changed', 'errored']) {
                const state = api.getState();
                state.previousResponseId = 'seed';
                state.previousResponseIdentity = {
                    provider: 'primary', protocol: 'responses', host: 'api.x.ai'
                };
                await api.send(message);
                if (state.previousResponseId !== null || state.previousResponseIdentity !== null) {
                    throw new Error(message + ' retained a foreign/ambiguous cursor');
                }
            }
            if (api.getState().history.filter((item) => item.role === 'user').length < 3) {
                throw new Error('cursor clearing discarded history');
            }
            process.stdout.write('cursor-clear:ok');
            """
        )
    )
    assert "cursor-clear:ok" in output


def test_http_failure_is_not_reported_as_offline() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            fetch = async () => ({ ok: false, status: 403 });
            window.fetch = fetch;
            api.init();
            await api.send('forbidden');
            const log = document.getElementById('ss-agent-log');
            const error = (log.children || []).find((node) => String(node.className).includes('ss-agent-msg-error'));
            if (!error || !/403/.test(error.textContent)) throw new Error('HTTP status missing: ' + (error && error.textContent));
            if (/screenscribe review|screenscribe analyze/.test(error.textContent)) {
                throw new Error('HTTP failure mislabeled offline: ' + error.textContent);
            }
            const notice = document.getElementById('ss-agent-offline');
            if (!notice.hidden) throw new Error('HTTP failure opened offline notice');
            process.stdout.write('http-error:ok');
            """
        )
    )
    assert "http-error:ok" in output


def test_geometry_default_avoids_player_and_toolbar() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            api.setCollapsed(false);
            const rootEl = document.getElementById('ss-agent-root');
            const left = parseFloat(rootEl.style.left);
            const top = parseFloat(rootEl.style.top);
            const panel = { left, top, right: left + 360, bottom: top + 480 };
            const player = document.getElementById('videoPlayer').getBoundingClientRect();
            const toolbar = document.getElementById('videoControls').getBoundingClientRect();
            const overlapPlayer = api.rectsOverlap(panel, player);
            const overlapToolbar = api.rectsOverlap(panel, toolbar);
            if (overlapPlayer) throw new Error('default overlaps player ' + JSON.stringify({panel, player}));
            if (overlapToolbar) throw new Error('default overlaps toolbar ' + JSON.stringify({panel, toolbar}));
            if (api.getState().sheet) throw new Error('1280x800 should float, not sheet');
            process.stdout.write('geometry-a:ok');
            """
        )
    )
    assert "geometry-a:ok" in output


def test_geometry_discards_overlapping_restored_position() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            const key = [...localStorage._store.keys()][0] || (api.STORAGE_KEY + ':http://localhost/report.html');
            localStorage.setItem(key, JSON.stringify({ collapsed: false, left: 30, top: 90 }));
            const state = api.getState();
            state.collapsed = true;
            state.left = null;
            state.top = null;
            state.sheet = false;
            api.restore();
            api.setCollapsed(false);
            const rootEl = document.getElementById('ss-agent-root');
            const left = parseFloat(rootEl.style.left);
            const top = parseFloat(rootEl.style.top);
            const panel = { left, top, right: left + 360, bottom: top + 480 };
            const player = document.getElementById('videoPlayer').getBoundingClientRect();
            const toolbar = document.getElementById('videoControls').getBoundingClientRect();
            if (api.rectsOverlap(panel, player)) throw new Error('restored overlap player ' + JSON.stringify({left, top, player}));
            if (api.rectsOverlap(panel, toolbar)) throw new Error('restored overlap toolbar');
            if (left === 30 && top === 90) throw new Error('overlapping restore was kept');
            process.stdout.write('geometry-b:ok');
            """
        )
    )
    assert "geometry-b:ok" in output


def test_geometry_recomputes_on_metadata_resize_and_player_rect() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            api.setCollapsed(false);
            const video = document.getElementById('videoPlayer');
            const toolbar = document.getElementById('videoControls');
            function panelBox() {
                const rootEl = document.getElementById('ss-agent-root');
                const st = api.getState();
                if (st.sheet) {
                    return { left: 1280 - 360, top: 0, right: 1280, bottom: 800 };
                }
                const left = parseFloat(rootEl.style.left);
                const top = parseFloat(rootEl.style.top);
                return { left, top, right: left + 360, bottom: top + 480 };
            }
            function assertClear(label) {
                const panel = panelBox();
                const player = video.getBoundingClientRect();
                const bar = toolbar.getBoundingClientRect();
                if (!api.getState().sheet && api.rectsOverlap(panel, player)) {
                    throw new Error(label + ' overlaps player ' + JSON.stringify({panel, player}));
                }
                if (!api.getState().sheet && api.rectsOverlap(panel, bar)) {
                    throw new Error(label + ' overlaps toolbar');
                }
            }
            video._rect = { left: 0, top: 0, right: 900, bottom: 700, width: 900, height: 700 };
            toolbar._rect = { left: 8, top: 8, right: 400, bottom: 44, width: 392, height: 36 };
            video.dispatchEvent({ type: 'loadedmetadata' });
            assertClear('loadedmetadata');

            innerWidth = 1440;
            window.innerWidth = 1440;
            video._rect = { left: 20, top: 80, right: 700, bottom: 500, width: 680, height: 420 };
            toolbar._rect = { left: 28, top: 48, right: 420, bottom: 80, width: 392, height: 32 };
            window.dispatchEvent({ type: 'resize' });
            assertClear('resize');

            video._rect = { left: 10, top: 40, right: 1100, bottom: 760, width: 1090, height: 720 };
            toolbar._rect = { left: 16, top: 8, right: 380, bottom: 40, width: 364, height: 32 };
            api.relayout();
            const after = api.getState();
            if (!after.sheet) {
                const panel = panelBox();
                if (api.rectsOverlap(panel, video.getBoundingClientRect())) {
                    throw new Error('player-rect change still overlaps');
                }
            }
            if (document.documentElement.classList.contains('ss-agent-sheet') !== after.sheet) {
                throw new Error('sheet class mismatch');
            }
            process.stdout.write('geometry-c:ok sheet=' + after.sheet);
            """
        )
    )
    assert "geometry-c:ok" in output


def test_geometry_docks_sheet_when_no_clear_spot() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const player = { left: 8, top: 8, right: 1272, bottom: 792, width: 1264, height: 784 };
            const placed = api.placeBesidePlayer(player, { width: 1280, height: 800 }, { width: 360, height: 480 });
            if (!placed.sheet) throw new Error('expected sheet when player fills viewport: ' + JSON.stringify(placed));
            innerWidth = 900;
            innerHeight = 700;
            window.innerWidth = 900;
            window.innerHeight = 700;
            const video = document.getElementById('videoPlayer');
            const toolbar = document.getElementById('videoControls');
            video._rect = { left: 0, top: 0, right: 890, bottom: 690, width: 890, height: 690 };
            toolbar._rect = { left: 8, top: 8, right: 400, bottom: 40, width: 392, height: 32 };
            api.init();
            api.setCollapsed(false);
            if (!api.getState().sheet) throw new Error('expanded cramped layout must dock');
            if (!document.documentElement.classList.contains('ss-agent-sheet')) {
                throw new Error('html sheet class missing');
            }
            if (!document.getElementById('ss-agent-root').classList.contains('ss-agent-sheet')) {
                throw new Error('root sheet class missing');
            }
            process.stdout.write('geometry-sheet:ok');
            """
        )
    )
    assert "geometry-sheet:ok" in output


def test_mobile_sheet_is_stacked_and_viewport_width() -> None:
    css = AGENT_PANEL_CSS.read_text(encoding="utf-8")
    mobile = css.split("@media (max-width: 900px)", 1)[1]
    assert ".ss-agent-root.ss-agent-sheet" in mobile
    assert "position: relative" in mobile
    assert "width: 100% !important" in mobile
    assert "max-width: 100vw" in mobile


def test_review_patch_applies_and_saves_once() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const applied = [];
            const instructions = [];
            const saves = [];
            window.__screenscribeAgentHost = {
                applyReviewPatch(ops, instruction) {
                    applied.push(ops);
                    instructions.push(instruction);
                    return ops.map((op) => ({ op: op.op, findingId: String(op.finding_id || ''), skipped: false }));
                },
                async saveReview() {
                    saves.push(1);
                    return { ok: true, status: 200 };
                },
            };
            const stream = [
                'event: token',
                'data: {"text":"ok"}',
                '',
                'event: tool_result',
                'data: {"name":"set_severity","result":{"type":"review_patch","ops":[{"op":"set_severity","finding_id":"3","severity":"high"}],"explain":"Set finding 3 severity override to high."}}',
                '',
                'event: tool_result',
                'data: {"name":"set_severity","result":{"type":"review_patch","ops":[{"op":"set_severity","finding_id":"3","severity":"high"}],"explain":"Set finding 3 severity override to high."}}',
                '',
                'event: done',
                'data: {}',
                '',
            ].join('\\n');
            fetch = async () => ({ ok: true, text: async () => stream });
            window.fetch = fetch;
            api.init();
            await api.send('zmień finding 3 na high');
            if (applied.length !== 1) throw new Error('expected one apply after SSE dedupe, got ' + applied.length);
            if (applied[0][0].severity !== 'high') throw new Error('severity');
            if (instructions[0] !== 'zmień finding 3 na high') {
                throw new Error('raw user instruction lost: ' + instructions[0]);
            }
            if (saves.length !== 1) throw new Error('save once, got ' + saves.length);
            const log = document.getElementById('ss-agent-log');
            const text = (log.children || []).map((n) => n.textContent).join('|');
            if (!/applied: Set finding 3/.test(text) && !/zastosowano: Set finding 3/.test(text)) {
                throw new Error('confirmation missing: ' + text);
            }
            if (!/Reset/.test(text) && !/Resetuj/.test(text)) {
                throw new Error('undo hint missing: ' + text);
            }
            process.stdout.write('patch-save:ok');
            """
        )
    )
    assert "patch-save:ok" in output


def test_review_patch_save_409_shows_retry_not_silent() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            let saveCalls = 0;
            window.__screenscribeAgentHost = {
                applyReviewPatch(ops) {
                    return ops.map((op) => ({ op: op.op, findingId: '3' }));
                },
                async saveReview() {
                    saveCalls += 1;
                    return { ok: false, status: 409, message: 'Review reset invalidated this save. (HTTP 409)' };
                },
            };
            api.init();
            await api.ingestToolResult('edit_finding', {
                type: 'review_patch',
                ops: [{ op: 'edit_finding', finding_id: '3', fields: { notes: 'sprawdzić na Safari' } }],
                explain: 'Edit finding 3 text fields.',
            }, new Set());
            if (saveCalls !== 1) throw new Error('first save once, got ' + saveCalls);
            const log = document.getElementById('ss-agent-log');
            const fail = (log.children || []).find((n) => String(n.className).includes('ss-agent-msg-error'));
            if (!fail) throw new Error('failure line missing');
            if (!/not saved|nie zapisano/.test(fail.textContent)) throw new Error('fail copy: ' + fail.textContent);
            const retry = fail.querySelector('button') || fail.children.find((n) => n.tagName === 'BUTTON');
            if (!retry) throw new Error('retry button missing');
            retry.click();
            await new Promise((resolve) => setTimeout(resolve, 20));
            if (saveCalls !== 2) throw new Error('retry must save again, got ' + saveCalls);
            process.stdout.write('patch-409:ok');
            """
        )
    )
    assert "patch-409:ok" in output


def test_review_plan_card_apply_selected_ops() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const applied = [];
            const instructions = [];
            window.__screenscribeAgentHost = {
                applyReviewPatch(ops, instruction) {
                    applied.push(ops.slice());
                    instructions.push(instruction);
                    return ops.map((op) => ({ op: op.op, findingId: String(op.finding_id || '') }));
                },
                async saveReview() { return { ok: true, status: 200 }; },
            };
            api.init();
            const card = api.renderPlanCard({
                type: 'review_plan',
                ops: [
                    { op: 'set_severity', finding_id: '3', severity: 'high' },
                    { op: 'edit_finding', finding_id: '3', fields: { notes: 'n' } },
                    { op: 'set_verdict', finding_id: '1', verdict: 'accepted' },
                ],
                rationale: ['raise severity', 'note', 'accept'],
            }, 'popraw tylko finding 3');
            if (!card) throw new Error('plan card missing');
            const checks = card.querySelectorAll('.ss-agent-plan-check');
            if (checks.length !== 3) throw new Error('checkboxes: ' + checks.length);
            if (!checks.every((c) => c.checked)) throw new Error('all on');
            checks[2].checked = false;
            const apply = card.querySelector('.ss-agent-plan-apply');
            apply.click();
            await new Promise((resolve) => setTimeout(resolve, 30));
            if (applied.length !== 1) throw new Error('apply once, got ' + applied.length);
            if (applied[0].length !== 2) throw new Error('selected 2, got ' + applied[0].length);
            if (instructions[0] !== 'popraw tylko finding 3') {
                throw new Error('plan lost raw user instruction: ' + instructions[0]);
            }
            const log = document.getElementById('ss-agent-log');
            const user = (log.children || []).find((n) => String(n.className).includes('ss-agent-msg-user'));
            if (!user || !/2/.test(user.textContent) || !/3/.test(user.textContent)) {
                throw new Error('summary message: ' + (user && user.textContent));
            }
            const hist = api.getState().history;
            if (!hist.some((m) => m.role === 'user' && /^review_applied:/.test(m.content))) {
                throw new Error('history missing review_applied confirmation');
            }
            process.stdout.write('plan:ok');
            """
        )
    )
    assert "plan:ok" in output


_GLOBAL_CORRECTION_HOST = """
            const fetches = [];
            fetch = async (url, opts) => {
                fetches.push({
                    url: url,
                    method: (opts && opts.method) || 'GET',
                    body: opts && opts.body ? JSON.parse(opts.body) : null,
                });
                return {
                    ok: true,
                    status: 200,
                    async json() { return { status: 'saved', entry: { id: 'e1' }, corrections: [] }; },
                };
            };
            window.fetch = fetch;
            // Stands in for review_app.js: applyReviewPatch routes a
            // correct_transcript op that is not already persisted through
            // applyTranscriptCorrectionOp, which POSTs /api/corrections.
            window.__screenscribeAgentHost = {
                async applyReviewPatch(ops) {
                    const out = [];
                    for (const op of ops) {
                        if (op.op === 'correct_transcript' && !op.persisted) {
                            await fetch('/api/corrections', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    match: op.match,
                                    replacement: op.replacement,
                                    scope: op.scope,
                                    note: op.note || '',
                                    source: 'agent',
                                }),
                            });
                        }
                        out.push({ op: op.op });
                    }
                    return out;
                },
                async saveReview() { return { ok: true, status: 200 }; },
            };
            const planResult = {
                type: 'review_plan',
                instruction: 'Transcript correction',
                status: 'pending_approval',
                ops: [{
                    op: 'correct_transcript',
                    match: 'cloud',
                    replacement: 'Claude',
                    scope: 'global',
                    note: '',
                    persisted: false,
                    requires_approval: true,
                }],
                rationale: ['Global corrections change the user glossary.'],
            };
"""


def test_global_correction_plan_waits_for_apply_before_posting() -> None:
    """A global correction must not reach /api/corrections without Apply."""
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            """
            + _GLOBAL_CORRECTION_HOST
            + """
            api.init();
            await api.ingestToolResult('correct_transcript', planResult, new Set(), 'fix cloud');
            const log = document.getElementById('ss-agent-log');
            const card = (log.children || []).find(
                (n) => String(n.className).includes('ss-agent-plan')
            );
            if (!card) throw new Error('global correction auto-applied: no approval card');
            const caption = card.querySelector('.ss-agent-plan-caption');
            const text = caption ? caption.textContent : '';
            if (text.indexOf('cloud') < 0 || text.indexOf('Claude') < 0
                || text.indexOf('global') < 0) {
                throw new Error('card does not describe the correction: ' + text);
            }
            if (fetches.length !== 0) {
                throw new Error('posted before approval: ' + JSON.stringify(fetches));
            }
            const apply = card.querySelector('.ss-agent-plan-apply');
            apply.click();
            await new Promise((resolve) => setTimeout(resolve, 30));
            const posts = fetches.filter((f) => f.url === '/api/corrections');
            if (posts.length !== 1) throw new Error('posts: ' + JSON.stringify(fetches));
            if (posts[0].method !== 'POST') throw new Error('method: ' + posts[0].method);
            if (posts[0].body.scope !== 'global') throw new Error('scope: ' + posts[0].body.scope);
            if (posts[0].body.match !== 'cloud' || posts[0].body.replacement !== 'Claude') {
                throw new Error('payload: ' + JSON.stringify(posts[0].body));
            }
            process.stdout.write('global-apply:ok');
            """
        )
    )
    assert "global-apply:ok" in output


def test_global_correction_plan_cancel_writes_nothing() -> None:
    """Cancel removes the card and leaves the glossary untouched."""
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            """
            + _GLOBAL_CORRECTION_HOST
            + """
            api.init();
            await api.ingestToolResult('correct_transcript', planResult, new Set(), 'fix cloud');
            const log = document.getElementById('ss-agent-log');
            const card = (log.children || []).find(
                (n) => String(n.className).includes('ss-agent-plan')
            );
            if (!card) throw new Error('approval card missing');
            card.querySelector('.ss-agent-plan-cancel').click();
            await new Promise((resolve) => setTimeout(resolve, 30));
            const stillThere = (log.children || []).find(
                (n) => String(n.className).includes('ss-agent-plan')
            );
            if (stillThere) throw new Error('cancel left the card in the log');
            if (fetches.length !== 0) {
                throw new Error('cancel wrote something: ' + JSON.stringify(fetches));
            }
            process.stdout.write('global-cancel:ok');
            """
        )
    )
    assert "global-cancel:ok" in output


def test_review_app_posts_only_unpersisted_corrections() -> None:
    """The Apply route in review_app.js is what reaches /api/corrections."""
    source = REVIEW_APP_JS.read_text(encoding="utf-8")
    assert "if (kind === 'correct_transcript') return applyTranscriptCorrectionOp(op);" in source
    route = source.split("async function applyTranscriptCorrectionOp(op) {", 1)[1]
    route = route.split("\n}\n", 1)[0]
    assert "op.persisted" in route
    assert "'/api/corrections'" in route
    assert "source: 'agent'" in route


def test_review_plan_does_not_confirm_or_update_history_when_save_fails() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            window.__screenscribeAgentHost = {
                applyReviewPatch(ops) { return ops.map((op) => ({ op: op.op, findingId: '3' })); },
                async saveReview() { return { ok: false, status: 409, message: 'stale generation' }; },
            };
            api.init();
            const card = api.renderPlanCard({
                type: 'review_plan',
                ops: [{ op: 'set_severity', finding_id: '3', severity: 'high' }],
                rationale: ['raise severity'],
            });
            card.querySelector('.ss-agent-plan-apply').click();
            await new Promise((resolve) => setTimeout(resolve, 30));
            const log = document.getElementById('ss-agent-log');
            const userLines = (log.children || []).filter((n) => String(n.className).includes('ss-agent-msg-user'));
            if (userLines.length) throw new Error('failed plan was confirmed: ' + userLines[0].textContent);
            if (api.getState().history.some((m) => /1/.test(m.content || ''))) {
                throw new Error('failed plan leaked confirmation into model history');
            }
            const error = (log.children || []).find((n) => String(n.className).includes('ss-agent-msg-error'));
            if (!error) throw new Error('save failure was not shown');
            process.stdout.write('plan-failure:ok');
            """
        )
    )
    assert "plan-failure:ok" in output


def test_review_patch_offline_blocks_apply() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            location.protocol = 'file:';
            window.location.protocol = 'file:';
            const api = screenscribeAgentPanel;
            let applied = 0;
            window.__screenscribeAgentHost = {
                applyReviewPatch() { applied += 1; return []; },
                async saveReview() { applied += 10; return { ok: true }; },
            };
            api.init();
            const notice = document.getElementById('ss-agent-offline');
            if (notice.hidden) throw new Error('offline notice hidden');
            const log = document.getElementById('ss-agent-log');
            const before = (log.children || []).map((n) => n.textContent).join('|');
            if (!/cannot save (?:agent )?edits|nie zapisze poprawek/.test(before)) {
                throw new Error('preemptive patch-offline copy missing: ' + before);
            }
            await api.ingestToolResult('set_severity', {
                type: 'review_patch',
                ops: [{ op: 'set_severity', finding_id: '3', severity: 'high' }],
                explain: 'nope',
            }, new Set());
            if (applied !== 0) throw new Error('must not apply or save offline, got ' + applied);
            process.stdout.write('patch-offline:ok');
            """
        )
    )
    assert "patch-offline:ok" in output


def test_review_tool_error_and_unsupported_are_system_lines() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            let applied = 0;
            window.__screenscribeAgentHost = {
                applyReviewPatch() { applied += 1; return []; },
            };
            api.init();
            await api.ingestToolResult('set_verdict', { error: 'Unknown finding_id: 999' }, new Set());
            await api.ingestToolResult('merge_findings', {
                unsupported: true,
                reason: 'merge/unmerge is a client-only fold',
            }, new Set());
            if (applied !== 0) throw new Error('errors must not apply');
            const log = document.getElementById('ss-agent-log');
            const text = (log.children || []).map((n) => n.textContent).join('|');
            if (!/Unknown finding_id: 999/.test(text)) throw new Error('error line: ' + text);
            if (!/client-only fold/.test(text)) throw new Error('unsupported line: ' + text);
            process.stdout.write('patch-errors:ok');
            """
        )
    )
    assert "patch-errors:ok" in output


def test_provider_error_is_retryable_and_composer_stays_live() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const bodies = [];
            fetch = async (_url, opts) => {
                bodies.push(JSON.parse(opts.body));
                if (bodies.length === 1) {
                    return {
                        ok: true,
                        text: async () => 'event: token\\ndata: {"text":"partial"}\\n\\nevent: error\\ndata: {"message":"provider exploded"}\\n\\n',
                    };
                }
                return {
                    ok: true,
                    text: async () => 'event: token\\ndata: {"text":"recovered"}\\n\\nevent: done\\ndata: {"response_id":"resp_ok","provider":"primary","protocol":"responses","host":"api.openai.com"}\\n\\n',
                };
            };
            window.fetch = fetch;
            api.init();
            await api.whenThreadReady();
            await api.send('What broke?');
            const input = document.getElementById('ss-agent-input');
            if (!input || input.disabled) throw new Error('dead input after provider error');
            if (api.getState().streaming) throw new Error('streaming stuck');
            const hist = api.getState().history;
            if (hist.length !== 1 || hist[0].content !== 'What broke?') {
                throw new Error('partial assistant was stored: ' + JSON.stringify(hist));
            }
            const log = document.getElementById('ss-agent-log');
            const errorRow = (log.children || []).find((n) => String(n.className).includes('ss-agent-msg-error'));
            if (!errorRow || !errorRow.textContent.includes('provider exploded')) {
                throw new Error('error row missing');
            }
            const retry = errorRow.querySelector('.ss-agent-retry');
            if (!retry) throw new Error('retry button missing');
            retry.click();
            await new Promise((resolve) => setTimeout(resolve, 40));
            if (bodies.length !== 2) throw new Error('retry did not resend, got ' + bodies.length);
            if (bodies[1].message !== 'What broke?') throw new Error('retry message: ' + bodies[1].message);
            const users = (log.children || []).filter((n) => String(n.className).includes('ss-agent-msg-user'));
            if (users.length !== 1) throw new Error('user bubble duplicated: ' + users.length);
            if (api.getState().previousResponseId !== 'resp_ok') {
                throw new Error('chain after retry: ' + api.getState().previousResponseId);
            }
            if (input.disabled) throw new Error('dead input after retry');
            await api.send('third question');
            if (bodies.length !== 3) throw new Error('composer did not accept a new question');
            if (bodies[2].message !== 'third question') throw new Error('third message');
            if (input.disabled || api.getState().streaming) throw new Error('composer dead after recovery');
            process.stdout.write('retry-live:ok');
            """
        )
    )
    assert "retry-live:ok" in output


def test_thread_restores_messages_and_chain_from_indexeddb() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            function later(fn) { setTimeout(fn, 0); }
            function makeIdb() {
                const stores = {};
                let upgraded = false;
                return {
                    open() {
                        const req = {};
                        later(() => {
                            const db = {
                                objectStoreNames: {
                                    contains(name) { return Object.prototype.hasOwnProperty.call(stores, name); },
                                },
                                createObjectStore(name) { stores[name] = new Map(); },
                                transaction() {
                                    return {
                                        objectStore(name) {
                                            const data = stores[name];
                                            if (!data) throw new Error('missing store ' + name);
                                            return {
                                                get(key) {
                                                    const result = {};
                                                    later(() => {
                                                        result.result = data.has(key) ? data.get(key) : undefined;
                                                        if (result.onsuccess) result.onsuccess();
                                                    });
                                                    return result;
                                                },
                                                put(value, key) {
                                                    data.set(key, JSON.parse(JSON.stringify(value)));
                                                    const result = {};
                                                    later(() => { if (result.onsuccess) result.onsuccess(); });
                                                    return result;
                                                },
                                            };
                                        },
                                    };
                                },
                            };
                            req.result = db;
                            if (!upgraded && req.onupgradeneeded) req.onupgradeneeded();
                            upgraded = true;
                            if (req.onsuccess) req.onsuccess();
                        });
                        return req;
                    },
                };
            }
            window.indexedDB = makeIdb();
            const api = screenscribeAgentPanel;
            const bodies = [];
            fetch = async (_url, opts) => {
                bodies.push(JSON.parse(opts.body));
                const n = bodies.length;
                const text = n === 1 ? 'hello-reply' : 'again-reply';
                const id = n === 1 ? 'resp_a' : 'resp_b';
                return {
                    ok: true,
                    text: async () => 'event: token\\ndata: {"text":"' + text + '"}\\n\\nevent: done\\ndata: {"response_id":"' + id + '","provider":"primary","protocol":"responses","host":"api.openai.com"}\\n\\n',
                };
            };
            window.fetch = fetch;
            api.init();
            await api.whenThreadReady();
            await api.send('hello');
            await api.send('again');
            if (bodies[1].previous_response_id !== 'resp_a') {
                throw new Error('second turn did not chain: ' + bodies[1].previous_response_id);
            }
            if (api.getState().previousResponseId !== 'resp_b') {
                throw new Error('live chain: ' + api.getState().previousResponseId);
            }
            if (api.getState().history.length !== 4) {
                throw new Error('live history: ' + JSON.stringify(api.getState().history));
            }
            api.getState().history = [];
            api.getState().previousResponseId = null;
            const restored = await api.restoreThread();
            if (!restored) throw new Error('restore returned false');
            const after = api.getState();
            if (after.previousResponseId !== 'resp_b') throw new Error('restored chain: ' + after.previousResponseId);
            const contents = after.history.map((m) => m.role + ':' + m.content);
            if (contents.join('|') !== 'user:hello|assistant:hello-reply|user:again|assistant:again-reply') {
                throw new Error('restored messages: ' + contents.join('|'));
            }
            const log = document.getElementById('ss-agent-log');
            const rendered = (log.children || []).map((n) => n.textContent).join('|');
            if (!rendered.includes('hello') || !rendered.includes('again-reply')) {
                throw new Error('dom after reload: ' + rendered);
            }
            process.stdout.write('thread-persist:ok');
            """
        )
    )
    assert "thread-persist:ok" in output


def test_reference_event_inserts_quote_and_opens_panel() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            await api.whenThreadReady();
            api.setCollapsed(true);
            const input = document.getElementById('ss-agent-input');
            input.value = 'draft';
            document.dispatchEvent({
                type: 'screenscribe:agent-insert-reference',
                detail: {
                    kind: 'reference',
                    moment_id: '12',
                    field: 'transcript',
                    quote: 'sessions nie wchodzą',
                    timestamp: '00:16',
                },
            });
            const rootEl = document.getElementById('ss-agent-root');
            if (rootEl.classList.contains('ss-agent-collapsed')) throw new Error('panel stayed closed');
            if (!rootEl.classList.contains('ss-agent-expanded')) throw new Error('panel not expanded');
            if (input.disabled) throw new Error('composer disabled');
            const value = input.value;
            if (!value.startsWith('draft')) throw new Error('draft lost: ' + value);
            if (!value.includes('sessions nie wchodzą')) throw new Error('quote missing: ' + value);
            if (!value.includes('moment=12') || !value.includes('t=00:16') || !value.includes('field=transcript')) {
                throw new Error('marker missing: ' + value);
            }
            document.dispatchEvent({
                type: 'screenscribe:agent-insert-reference',
                detail: {
                    kind: 'transcript-error',
                    moment_id: null,
                    field: 'subtitle',
                    quote: 'zła linia',
                    timestamp: null,
                },
            });
            if (!input.value.includes('[transcript-error field=subtitle]')) {
                throw new Error('transcript-error marker: ' + input.value);
            }
            if (!input.value.includes('> zła linia')) throw new Error('second quote missing');
            process.stdout.write('reference:ok');
            """
        )
    )
    assert "reference:ok" in output


def test_markdown_parser_builds_blocks_and_inline_tokens() -> None:
    """Agent answers are Markdown; the parser must keep structure, not raw markers.

    Falsification: render the assistant row with textContent again and the
    real-DOM e2e shows literal ``**`` and ``-`` markers; here mdBlocks would be
    missing from the API.
    """
    output = _run_agent_panel(
        textwrap.dedent(
            r"""
            const api = screenscribeAgentPanel;
            const text = '### Co widać\n\nTo **pulpit** z `Activity Monitor`.\n\n'
                + '- pierwszy\n- drugi *ważny*\n\n1. raz\n2. dwa\n\n'
                + '```bash\nls -la\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n'
                + '> cytat\n\nLink [docs](https://example.com/x) i https://a.io/b.';
            const blocks = api.mdBlocks(text);
            const types = blocks.map((b) => b.type).join(',');
            if (types !== 'heading,p,ul,ol,code,table,quote,p') throw new Error('types ' + types);
            if (blocks[2].items.join('|') !== 'pierwszy|drugi *ważny*') throw new Error('ul');
            if (blocks[4].lang !== 'bash' || blocks[4].text !== 'ls -la') throw new Error('code');
            if (blocks[5].rows[0].join('|') !== '1|2') throw new Error('table');
            const inline = api.mdInline('To **pulpit** z `x` i *y*');
            const kinds = inline.map((t) => t.t).join(',');
            if (kinds !== 'text,strong,text,code,text,em') throw new Error('inline ' + kinds);
            const links = api.mdInline('[docs](https://example.com/x) https://a.io/b.');
            if (links[0].t !== 'link' || links[0].href !== 'https://example.com/x') throw new Error('link');
            if (links[2].t !== 'link' || links[2].href !== 'https://a.io/b') throw new Error('autolink');
            const unsafe = api.mdInline('[x](javascript:alert(1))');
            if (unsafe.some((t) => t.t === 'link')) throw new Error('javascript: must not link');
            process.stdout.write('md:ok');
            """
        )
    )
    assert "md:ok" in output


def test_attached_screenshots_ride_on_the_request_and_role_labels_are_set() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const fetches = [];
            fetch = async (url, opts) => {
                fetches.push(JSON.parse(opts.body));
                return {
                    ok: true,
                    text: async () => 'event: token\\ndata: {"text":"Widzę."}\\n\\nevent: done\\ndata: {}\\n\\n',
                };
            };
            window.fetch = fetch;
            api.init();
            const state = api.getState();
            state.attachments.push({ media_type: 'image/png', data: 'iVBORw0KGgo=', url: 'data:image/png;base64,iVBORw0KGgo=' });
            api.syncComposer();
            const sendBtn = document.getElementById('ss-agent-send');
            if (sendBtn.disabled) throw new Error('a screenshot alone must enable Send');
            const input = document.getElementById('ss-agent-input');
            input.value = '';
            document.getElementById('ss-agent-form').submit();
            await new Promise((resolve) => setTimeout(resolve, 30));
            if (fetches.length !== 1) throw new Error('expected one request');
            const body = fetches[0];
            if (!body.images || body.images.length !== 1) throw new Error('images missing');
            if (body.images[0].media_type !== 'image/png' || body.images[0].data !== 'iVBORw0KGgo=') {
                throw new Error('image shape');
            }
            if (!body.message) throw new Error('image-only message needs default text');
            if (state.attachments.length !== 0) throw new Error('attachments must clear after send');
            const user = document.querySelectorAll('.ss-agent-msg-user')[0];
            const agent = document.querySelectorAll('.ss-agent-msg-assistant')[0];
            if (!user.getAttribute('data-role-label')) throw new Error('user role label');
            if (!agent.getAttribute('data-role-label')) throw new Error('agent role label');
            if (user.getAttribute('data-role-label') === agent.getAttribute('data-role-label')) {
                throw new Error('roles must differ');
            }
            process.stdout.write('attach:ok');
            """
        )
    )
    assert "attach:ok" in output


def test_autosend_submits_dictation_only_when_enabled() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const fetches = [];
            fetch = async (url, opts) => {
                fetches.push(JSON.parse(opts.body));
                return { ok: true, text: async () => 'event: done\\ndata: {}\\n\\n' };
            };
            window.fetch = fetch;
            api.init();
            const box = document.getElementById('ss-agent-autosend');
            if (!box || box.checked) throw new Error('autosend starts off');
            api.insertDictation('pierwsze');
            if (api.autosendDictation()) throw new Error('off: dictation must stay a draft');
            await new Promise((resolve) => setTimeout(resolve, 20));
            if (fetches.length !== 0) throw new Error('off: nothing may be sent');
            api.setAutosend(true);
            if (!box.checked) throw new Error('checkbox follows the setting');
            if (![...localStorage._store.keys()].some((k) => k.endsWith(':autosend'))) {
                throw new Error('autosend must persist');
            }
            if (!api.autosendDictation()) throw new Error('on: dictation must be sent');
            await new Promise((resolve) => setTimeout(resolve, 20));
            if (fetches.length !== 1 || fetches[0].message !== 'pierwsze') {
                throw new Error('on: expected the dictated draft, got ' + JSON.stringify(fetches));
            }
            process.stdout.write('autosend:ok');
            """
        )
    )
    assert "autosend:ok" in output


def test_dictation_status_reflects_whether_autosend_actually_runs() -> None:
    output = _run_agent_panel(
        textwrap.dedent("""
        const api = screenscribeAgentPanel;
        let callbacks;
        const sent = [];
        window.navigator = { mediaDevices: { getUserMedia: async () => ({}) } };
        window.MediaRecorder = function () {};
        window.ScreenScribeLib = Object.assign(window.ScreenScribeLib || {}, {
            createSttTransport(options) { callbacks = options; return { start: async () => true }; }
        });
        fetch = async (url, opts) => {
            if (url === api.STREAM_URL) sent.push(JSON.parse(opts.body));
            return { ok: true, text: async () => 'event: done\\ndata: {}\\n\\n' };
        };
        window.fetch = fetch;
        api.init();
        await api.startMic();
        if (!callbacks) throw new Error('microphone callback missing');
        callbacks.onTranscript('first phrase');
        const inserted = api.getState().mic.message;
        api.setAutosend(true);
        api.getState().streaming = true;
        callbacks.onTranscript('second phrase');
        if (api.getState().mic.message !== inserted) throw new Error('unsent dictation marked sent');
        if (!document.getElementById('ss-agent-input').value.includes('second phrase')) {
            throw new Error('dictation lost from draft');
        }
        if (sent.length) throw new Error('dictation sent while streaming');
        api.getState().streaming = false;
        callbacks.onTranscript('third phrase');
        if (api.getState().mic.message === inserted) throw new Error('sent dictation marked inserted');
        await new Promise((resolve) => setTimeout(resolve, 20));
        if (sent.length !== 1) throw new Error('expected one autosend');
        process.stdout.write('dictation-status:ok');
    """)
    )
    assert "dictation-status:ok" in output


def test_request_history_is_capped_to_the_api_contract() -> None:
    """R3-02: the 27th turn must not 422 on history length.

    The backend accepts at most 50 history items of at most 16000 chars each;
    the panel used to send the whole conversation verbatim.
    """
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            const bodies = [];
            fetch = async (_url, opts) => {
                bodies.push(JSON.parse(opts.body));
                return {
                    ok: true,
                    text: async () => 'event: token\\ndata: {"text":"ok"}\\n\\nevent: done\\ndata: {"response_id":null}\\n\\n',
                };
            };
            window.fetch = fetch;
            api.init();
            await api.whenThreadReady();
            const history = api.getState().history;
            for (let i = 0; i < 30; i += 1) {
                history.push({ role: 'user', content: 'q' + i });
                history.push({ role: 'assistant', content: i === 29 ? 'x'.repeat(20000) : 'a' + i });
            }
            await api.send('turn 31');
            const sent = bodies[0].history;
            if (sent.length !== 50) throw new Error('history length ' + sent.length);
            if (sent[sent.length - 1].content.length !== 16000) {
                throw new Error('field not truncated: ' + sent[sent.length - 1].content.length);
            }
            if (sent[0].content !== 'q5') throw new Error('oldest kept entry ' + sent[0].content);
            // The full conversation stays local.
            if (api.getState().history.length !== 62) {
                throw new Error('local history ' + api.getState().history.length);
            }
            process.stdout.write('history-cap:ok');
            """
        )
    )
    assert "history-cap:ok" in output


def test_stream_without_done_is_marked_interrupted_with_retry() -> None:
    """R3-01 (frontend): clean EOF without `done` is a fragment, not an answer."""
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            fetch = async () => ({
                ok: true,
                text: async () => 'event: token\\ndata: {"text":"partial answ"}\\n\\n',
            });
            window.fetch = fetch;
            api.init();
            await api.whenThreadReady();
            await api.send('co z tym buildem?');
            const state = api.getState();
            if (state.retryMessage !== 'co z tym buildem?') {
                throw new Error('no retry affordance: ' + state.retryMessage);
            }
            const assistant = state.history.filter((m) => m.role === 'assistant');
            if (assistant.length) throw new Error('fragment logged as an answer');
            const log = document.getElementById('ss-agent-log');
            const rendered = (log.children || []).map((n) => n.textContent).join('|');
            if (!rendered.includes('partial answ')) throw new Error('partial text lost: ' + rendered);
            process.stdout.write('stream-cut:ok');
            """
        )
    )
    assert "stream-cut:ok" in output


def test_thread_key_follows_the_report_not_just_the_url() -> None:
    """R3-03: two reports on one URL must not share a conversation."""
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            document.title = 'Screenscribe - recording_a.mov';
            api.getState().threadFingerprint = null;
            const keyA = api.threadKey();
            api.getState().threadFingerprint = null;
            const keyAgain = api.threadKey();
            if (keyA !== keyAgain) throw new Error('key not stable: ' + keyA + ' vs ' + keyAgain);
            document.title = 'Screenscribe - recording_b.mov';
            api.getState().threadFingerprint = null;
            const keyB = api.threadKey();
            if (keyA === keyB) throw new Error('different reports share a thread key');
            process.stdout.write('thread-key:ok');
            """
        )
    )
    assert "thread-key:ok" in output


def test_thread_key_survives_a_new_per_launch_video_token() -> None:
    """The served src carries a per-launch ``st`` token; restarts must not orphan."""
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            document.title = 'Screenscribe - recording_a.mov';
            const video = document.getElementById('videoPlayer');
            video.setAttribute('src', '/video?st=tokenA');
            api.getState().threadFingerprint = null;
            const keyA = api.threadKey();
            video.setAttribute('src', '/video?st=tokenB#t=12');
            api.getState().threadFingerprint = null;
            const keyB = api.threadKey();
            if (keyA !== keyB) {
                throw new Error('serve restart orphaned the thread: ' + keyA + ' vs ' + keyB);
            }
            video.setAttribute('src', '/other.mov');
            api.getState().threadFingerprint = null;
            const keyOther = api.threadKey();
            if (keyA === keyOther) throw new Error('different videos share a thread key');
            process.stdout.write('thread-key-token:ok');
            """
        )
    )
    assert "thread-key-token:ok" in output


def test_restored_chain_keeps_its_identity_after_reload() -> None:
    """R3-05: previousResponseId without its identity is dropped by send().

    The thread record now stores the provider identity; a real reload restores
    both, so the next turn still chains server-side.
    """
    output = _run_agent_panel(
        textwrap.dedent(
            """
            function later(fn) { setTimeout(fn, 0); }
            function makeIdb() {
                const stores = {};
                let upgraded = false;
                return {
                    open() {
                        const req = {};
                        later(() => {
                            const db = {
                                objectStoreNames: {
                                    contains(name) { return Object.prototype.hasOwnProperty.call(stores, name); },
                                },
                                createObjectStore(name) { stores[name] = new Map(); },
                                transaction() {
                                    return {
                                        objectStore(name) {
                                            const data = stores[name];
                                            if (!data) throw new Error('missing store ' + name);
                                            return {
                                                get(key) {
                                                    const result = {};
                                                    later(() => {
                                                        result.result = data.has(key) ? data.get(key) : undefined;
                                                        if (result.onsuccess) result.onsuccess();
                                                    });
                                                    return result;
                                                },
                                                put(value, key) {
                                                    data.set(key, JSON.parse(JSON.stringify(value)));
                                                    const result = {};
                                                    later(() => { if (result.onsuccess) result.onsuccess(); });
                                                    return result;
                                                },
                                            };
                                        },
                                    };
                                },
                            };
                            req.result = db;
                            if (!upgraded && req.onupgradeneeded) req.onupgradeneeded();
                            upgraded = true;
                            if (req.onsuccess) req.onsuccess();
                        });
                        return req;
                    },
                };
            }
            window.indexedDB = makeIdb();
            const api = screenscribeAgentPanel;
            const bodies = [];
            fetch = async (_url, opts) => {
                bodies.push(JSON.parse(opts.body));
                return {
                    ok: true,
                    text: async () => 'event: token\\ndata: {"text":"ok"}\\n\\nevent: done\\ndata: {"response_id":"resp_a","provider":"primary","protocol":"responses","host":"api.x.ai"}\\n\\n',
                };
            };
            window.fetch = fetch;
            api.init();
            await api.whenThreadReady();
            await api.send('first');
            // Simulate a reload: a fresh state restores from IndexedDB only.
            const state = api.getState();
            state.history = [];
            state.previousResponseId = null;
            state.previousResponseIdentity = null;
            const restored = await api.restoreThread();
            if (!restored) throw new Error('restore returned false');
            if (state.previousResponseId !== 'resp_a') throw new Error('chain lost: ' + state.previousResponseId);
            if (!state.previousResponseIdentity || state.previousResponseIdentity.host !== 'api.x.ai') {
                throw new Error('identity lost: ' + JSON.stringify(state.previousResponseIdentity));
            }
            await api.send('second');
            if (bodies[1].previous_response_id !== 'resp_a') {
                throw new Error('post-reload turn did not chain: ' + bodies[1].previous_response_id);
            }
            process.stdout.write('identity-reload:ok');
            """
        )
    )
    assert "identity-reload:ok" in output


def test_escape_stops_the_stream_from_anywhere_in_the_panel() -> None:
    """R4-05: after click-to-send the focus sits on the button, not the field.

    Escape used to be handled only by the textarea's keydown listener, so a
    mouse user had no keyboard stop.
    """
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            let aborted = false;
            function makeAbort() {
                return { signal: {}, abort() { aborted = true; } };
            }
            window.AbortController = function () { return makeAbort(); };
            fetch = async () => ({
                ok: true,
                text: () => new Promise(() => {}),  // a stream that never ends
            });
            window.fetch = fetch;
            api.init();
            const pending = api.send('long question');
            await Promise.resolve();
            if (!api.getState().streaming) throw new Error('turn did not start streaming');
            const panel = document.getElementById('ss-agent-panel');
            let prevented = false;
            panel.dispatchEvent({
                type: 'keydown',
                key: 'Escape',
                preventDefault() { prevented = true; },
            });
            if (!api.getState().stopRequested && !aborted) {
                throw new Error('Escape on the panel did not request a stop');
            }
            if (!prevented) throw new Error('Escape default not prevented');
            process.stdout.write('panel-escape:ok');
            """
        )
    )
    assert "panel-escape:ok" in output


def test_stacked_sheet_scrolls_into_view_and_hides_size_controls() -> None:
    """R4-03/R4-04: on a phone the sheet sits at the end of the document.

    Opening the chat must scroll it on screen, and the resize controls that
    the mobile CSS overrides into a fixed size must disappear.
    """
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            window.matchMedia = (query) => ({ matches: String(query).includes('max-width: 900px') });
            innerWidth = 390;
            innerHeight = 700;
            window.innerWidth = 390;
            window.innerHeight = 700;
            const video = document.getElementById('videoPlayer');
            video._rect = { left: 0, top: 0, right: 390, bottom: 600, width: 390, height: 600 };
            api.init();
            const rootEl = document.getElementById('ss-agent-root');
            let scrolled = 0;
            rootEl.scrollIntoView = () => { scrolled += 1; };
            api.setCollapsed(false);
            if (!api.getState().sheet) throw new Error('cramped phone layout must dock as a sheet');
            if (scrolled !== 1) throw new Error('open did not scroll the sheet into view: ' + scrolled);
            const handle = document.getElementById('ss-agent-resize');
            const sizeBtn = document.getElementById('ss-agent-size');
            if (!handle.hidden || !sizeBtn.hidden) {
                throw new Error('size controls visible in the fixed-size sheet');
            }
            // Desktop: controls come back.
            window.matchMedia = () => ({ matches: false });
            innerWidth = 1280; innerHeight = 800;
            window.innerWidth = 1280; window.innerHeight = 800;
            video._rect = { left: 8, top: 8, right: 600, bottom: 400, width: 592, height: 392 };
            api.applyDomState();
            if (handle.hidden || sizeBtn.hidden) throw new Error('size controls missing on desktop');
            process.stdout.write('stacked-sheet:ok');
            """
        )
    )
    assert "stacked-sheet:ok" in output


def test_fab_label_survives_a_language_switch_while_open() -> None:
    """R4-07: applyTranslations rewrites aria-label from data-i18n-attr.

    The attribute used to be frozen at 'agentToggle', so a PL→EN switch on an
    OPEN panel announced "open chat" on the collapse control.
    """
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = screenscribeAgentPanel;
            api.init();
            const fab = document.getElementById('ss-agent-fab');
            api.setCollapsed(false);
            const attr = fab.getAttribute('data-i18n-attr');
            if (attr !== 'aria-label:agentCollapse') throw new Error('open attr: ' + attr);
            api.setCollapsed(true);
            const closedAttr = fab.getAttribute('data-i18n-attr');
            if (closedAttr !== 'aria-label:agentToggle') throw new Error('closed attr: ' + closedAttr);
            process.stdout.write('fab-i18n:ok');
            """
        )
    )
    assert "fab-i18n:ok" in output
