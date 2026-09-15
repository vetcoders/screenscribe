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
        style: {},
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
sandbox.window.addEventListener = () => {};
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
