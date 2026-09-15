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

const documentRef = {
    body,
    documentElement: htmlEl,
    readyState: 'complete',
    addEventListener() {},
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
    TextDecoder,
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
            const api = ScreenScribeAgentPanel;
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
            const api = ScreenScribeAgentPanel;
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


def test_offline_mode() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            location.protocol = 'file:';
            window.location.protocol = 'file:';
            const api = ScreenScribeAgentPanel;
            api.init();
            const beforeErrors = consoleCalls.error.length;
            await api.send('What is critical in this recording?');
            if (consoleCalls.error.length !== beforeErrors) {
                throw new Error('console.error during offline send');
            }
            const notice = document.getElementById('ss-agent-offline');
            const text = (notice && notice.textContent) || '';
            if (!/screenscribe serve/.test(text)) throw new Error('offline copy: ' + text);
            if (notice.hidden) throw new Error('offline notice should be visible');
            process.stdout.write('offline:ok');
            """
        )
    )
    assert "offline:ok" in output


def test_geometry_beside_player_stays_in_viewport() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = ScreenScribeAgentPanel;
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
    assert "ScreenScribeAgentPanel" in html
    assert "ss-agent-fab" in html or "ss-agent-root" in html
    assert ".ss-agent-fab" in html
    assert "Run `screenscribe serve` to chat about this report." in html
    assert "cdnjs.cloudflare.com" not in html


def test_enter_sends_and_respects_ime_shift_and_whitespace() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = ScreenScribeAgentPanel;
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
            const api = ScreenScribeAgentPanel;
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
            if (msgs[1].textContent !== 'egress deny: external provider blocked') throw new Error('error text: ' + msgs[1].textContent);
            const emptyAssistant = msgs.filter((m) => String(m.className).includes('ss-agent-msg-assistant') && !String(m.textContent || '').trim());
            if (emptyAssistant.length) throw new Error('empty assistant bubble remained');
            process.stdout.write('error-bubble:ok');
            """
        )
    )
    assert "error-bubble:ok" in output


def test_geometry_default_avoids_player_and_toolbar() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = ScreenScribeAgentPanel;
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
            const api = ScreenScribeAgentPanel;
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
            const api = ScreenScribeAgentPanel;
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
            const api = ScreenScribeAgentPanel;
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


def test_review_patch_applies_and_saves_once() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            const api = ScreenScribeAgentPanel;
            const applied = [];
            const saves = [];
            window.__screenscribeAgentHost = {
                applyReviewPatch(ops) {
                    applied.push(ops);
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
            const api = ScreenScribeAgentPanel;
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
            const api = ScreenScribeAgentPanel;
            const applied = [];
            window.__screenscribeAgentHost = {
                applyReviewPatch(ops) {
                    applied.push(ops.slice());
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
            });
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
            const log = document.getElementById('ss-agent-log');
            const user = (log.children || []).find((n) => String(n.className).includes('ss-agent-msg-user'));
            if (!user || !/2/.test(user.textContent) || !/3/.test(user.textContent)) {
                throw new Error('summary message: ' + (user && user.textContent));
            }
            const hist = api.getState().history;
            if (!hist.some((m) => m.role === 'user' && /2/.test(m.content))) {
                throw new Error('history missing plan summary');
            }
            process.stdout.write('plan:ok');
            """
        )
    )
    assert "plan:ok" in output


def test_review_patch_offline_blocks_apply() -> None:
    output = _run_agent_panel(
        textwrap.dedent(
            """
            location.protocol = 'file:';
            window.location.protocol = 'file:';
            const api = ScreenScribeAgentPanel;
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
            if (!/cannot save agent edits|nie zapisze poprawek/.test(before)) {
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
            const api = ScreenScribeAgentPanel;
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
