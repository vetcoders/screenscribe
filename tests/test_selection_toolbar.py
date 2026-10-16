"""Node-vm tests for the report selection reference toolbar (A-02)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from screenscribe.html_pro.renderer import render_html_report_pro
from screenscribe.shell import ANALYZE_SURFACE, render_surface

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "screenscribe" / "html_pro_assets"
SCRIPTS = ASSETS / "scripts"
I18N_JS = SCRIPTS / "i18n.js"
TOOLBAR_JS = SCRIPTS / "selection_toolbar.js"
TOOLBAR_CSS = ASSETS / "styles" / "selection_toolbar.css"

_CI_TRUE = {"1", "true", "yes", "on"}
_BUNDLE = re.compile(r"window\.I18N_BUNDLE\s*=\s*(?P<json>\{.*?\});", re.DOTALL)
_REQUIRED_KEYS = {
    "selectionToolbar.addToChat",
    "selectionToolbar.reportTranscriptError",
    "selectionToolbar.copy",
    "selectionToolbar.label",
}

_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

function makeClassList() {
    const values = new Set();
    return {
        add(...items) { items.forEach((item) => values.add(item)); },
        remove(...items) { items.forEach((item) => values.delete(item)); },
        contains(value) { return values.has(value); },
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
        nodeType: 1,
        children,
        parentNode: null,
        dataset: {},
        style: {},
        classList: makeClassList(),
        id: '',
        hidden: false,
        textContent: '',
        value: '',
        offsetWidth: 240,
        offsetHeight: 36,
        addEventListener(type, fn) {
            (listeners[type] || (listeners[type] = [])).push(fn);
        },
        dispatchEvent(event) {
            const delivered = event || { type: 'click' };
            if (!delivered.target) delivered.target = el;
            el._dispatched = el._dispatched || [];
            el._dispatched.push(delivered);
            (listeners[delivered.type] || []).forEach((fn) => fn(delivered));
            if (el.parentNode && typeof el.parentNode.dispatchEvent === 'function') {
                el.parentNode.dispatchEvent(delivered);
            }
            return true;
        },
        setAttribute(name, value) {
            attrs[name] = String(value);
            if (name === 'id') el.id = String(value);
            if (name === 'hidden') el.hidden = true;
            if (name.startsWith('data-')) {
                const key = name.slice(5).replace(/-([a-z])/g, (_, chr) => chr.toUpperCase());
                el.dataset[key] = String(value);
            }
        },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
        },
        appendChild(child) {
            child.parentNode = el;
            children.push(child);
            return child;
        },
        removeChild(child) {
            const index = children.indexOf(child);
            if (index >= 0) children.splice(index, 1);
            if (child) child.parentNode = null;
            return child;
        },
        select() {},
        _listeners: listeners,
    };
    Object.defineProperty(el, 'className', {
        get() { return el.classList.toString(); },
        set(value) {
            el.classList.values.clear();
            String(value || '').split(/\s+/).filter(Boolean).forEach((item) => el.classList.add(item));
        },
    });
    return el;
}

function createTextNode(text) {
    return { nodeType: 3, textContent: String(text), parentNode: null, children: [] };
}

function getElementById(root, id) {
    let found = null;
    function walk(node) {
        if (!node || found) return;
        if (node.id === id) {
            found = node;
            return;
        }
        (node.children || []).forEach(walk);
    }
    walk(root);
    return found;
}

const htmlEl = makeElement('html');
htmlEl.lang = 'en';
const body = makeElement('body');
body.dataset.reportLanguage = 'en';
htmlEl.appendChild(body);
const documentRef = makeElement('#document');
documentRef.documentElement = htmlEl;
documentRef.body = body;
documentRef.readyState = 'complete';
documentRef.defaultView = null;
documentRef.createElement = (tag) => makeElement(tag);
documentRef.createTextNode = createTextNode;
documentRef.getElementById = (id) => getElementById(htmlEl, id);
documentRef.getSelection = () => null;
documentRef.execCommand = () => false;

const clipboardWrites = [];
const timers = [];
let timerSeq = 1;
function setTimeout(fn) {
    const id = timerSeq++;
    timers.push({ id, fn });
    return id;
}
function clearTimeout(id) {
    const index = timers.findIndex((timer) => timer.id === id);
    if (index >= 0) timers.splice(index, 1);
}
function flushTimers() {
    const due = timers.splice(0, timers.length);
    due.forEach((timer) => timer.fn());
}

const sandbox = {
    console,
    process,
    setTimeout,
    clearTimeout,
    flushTimers,
    clipboardWrites,
    CustomEvent: undefined,
    document: documentRef,
    navigator: {
        clipboard: {
            writeText(text) {
                clipboardWrites.push(String(text));
                return Promise.resolve();
            },
        },
    },
    innerWidth: 1280,
    innerHeight: 800,
    TRANSCRIPT_SEGMENTS: [],
    getSelection: () => documentRef.getSelection(),
    addEventListener() {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.document.defaultView = sandbox;
sandbox.window.document = documentRef;
sandbox.window.navigator = sandbox.navigator;
sandbox.window.getSelection = sandbox.getSelection;
sandbox.window.innerWidth = sandbox.innerWidth;

const sources = [
    'function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; }',
    fs.readFileSync(I18N_PATH, 'utf8'),
    fs.readFileSync(TOOLBAR_PATH, 'utf8'),
].join('\n');
vm.runInNewContext(sources, sandbox, { filename: 'selection_toolbar.js' });
"""


def _resolve_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    where = "CI" if os.environ.get("CI", "").strip().lower() in _CI_TRUE else "local"
    pytest.fail(f"node is missing ({where}): selection_toolbar.js runtime tests must run, not skip")


def _run_toolbar(test_body: str) -> str:
    node = _resolve_node()
    runner = (
        f"const I18N_PATH = {str(I18N_JS)!r};\n"
        f"const TOOLBAR_PATH = {str(TOOLBAR_JS)!r};\n"
        + _HARNESS
        + "\nconst testBody = "
        + repr(test_body)
        + ";\n"
        + textwrap.dedent(
            """
            const script = new vm.Script(testBody, { filename: 'selection_toolbar_test.js' });
            try {
                script.runInNewContext(sandbox);
            } catch (error) {
                console.error(error && error.stack || error);
                process.exitCode = 1;
            }
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
            "selection_toolbar.js node harness failed:\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def _bundle() -> dict[str, dict[str, dict[str, str]]]:
    match = _BUNDLE.search(I18N_JS.read_text(encoding="utf-8"))
    assert match, "scripts/i18n.js must assign window.I18N_BUNDLE"
    return json.loads(match.group("json"))


def _analyze_html() -> str:
    return render_surface(
        ANALYZE_SURFACE,
        {
            "document_language": "en",
            "ui_language": "en",
            "video_name": "toolbar.mp4",
            "video_name_escaped": "toolbar.mp4",
            "speech_lang_label": "EN",
            "body_mode": "analyze",
            "body_default_lang": "en",
            "body_speech_lang": "en",
            "body_has_markers": "false",
            "findings_json": "[]",
            "segments_json": "[]",
        },
    )


def test_selection_toolbar_shows_hides_and_emits_reference_envelope() -> None:
    output = _run_toolbar(
        textwrap.dedent(
            """
            const api = screenscribeSelectionToolbar;
            if (api.HIDE_DELAY_MS !== 250) throw new Error('delay ' + api.HIDE_DELAY_MS);
            const bar = document.getElementById('ss-selection-toolbar');
            if (!bar) throw new Error('toolbar was not built');
            if (bar.classList.contains('is-open')) throw new Error('toolbar starts open');
            const add = [...bar.children].find((node) => node.getAttribute('data-action') === 'insert-reference');
            const err = [...bar.children].find((node) => node.getAttribute('data-action') === 'transcript-error');
            const copy = [...bar.children].find((node) => node.getAttribute('data-action') === 'copy');
            if (add.textContent !== 'Add to chat') throw new Error('add label ' + add.textContent);
            if (err.hidden !== true) throw new Error('transcript action should start hidden');

            const captured = [];
            document.addEventListener('screenscribe:agent-insert-reference', (event) => {
                captured.push(event.detail);
            });

            function text(value, parent) {
                const node = document.createTextNode(value);
                parent.appendChild(node);
                return node;
            }
            function arm(node, quote, collapsed) {
                const selection = {
                    anchorNode: node,
                    isCollapsed: Boolean(collapsed),
                    rangeCount: collapsed ? 0 : 1,
                    toString() { return quote; },
                    getRangeAt() {
                        return {
                            startContainer: node,
                            getBoundingClientRect() {
                                return { left: 40, top: 120, right: 240, bottom: 140, width: 200, height: 20 };
                            },
                        };
                    },
                };
                document.getSelection = () => selection;
                window.getSelection = () => selection;
                document.dispatchEvent({ type: 'selectionchange' });
            }
            function clickAction(action) {
                const button = [...bar.children].find((node) => node.getAttribute('data-action') === action);
                button.dispatchEvent({ type: 'click', target: button });
            }

            const outside = document.createElement('div');
            outside.className = 'app-header';
            const outsideText = text('chrome', outside);
            document.body.appendChild(outside);
            arm(outsideText, 'chrome', false);
            flushTimers();
            if (bar.classList.contains('is-open')) throw new Error('outside selection showed the toolbar');

            const card = document.createElement('article');
            card.className = 'finding';
            card.setAttribute('data-finding-id', 'f-12');
            const meta = document.createElement('span');
            meta.className = 'finding-meta';
            meta.setAttribute('data-timestamp', '12.5');
            card.appendChild(meta);
            const transcript = document.createElement('div');
            transcript.className = 'finding-transcript';
            const transcriptText = text('the button label is clipped', transcript);
            card.appendChild(transcript);
            const summary = document.createElement('div');
            summary.className = 'finding-summary';
            const summaryText = text('Login button overflows', summary);
            card.appendChild(summary);
            document.body.appendChild(card);

            arm(transcriptText, 'the button label is clipped', false);
            if (bar.classList.contains('is-open')) throw new Error('toolbar opened before 250ms');
            flushTimers();
            if (!bar.classList.contains('is-open')) throw new Error('toolbar stayed hidden for a finding selection');
            if (err.hidden !== false) throw new Error('transcript error action should be visible');
            const holder = document.getElementById('ss-selection-toolbar-root');
            if (holder.style.top !== '76px') throw new Error('placement ' + holder.style.top);

            const beforeCopy = captured.length;
            clickAction('copy');
            if (captured.length !== beforeCopy) throw new Error('copy must not emit the reference event');
            if (clipboardWrites[clipboardWrites.length - 1] !== 'the button label is clipped') {
                throw new Error('clipboard ' + clipboardWrites.join('|'));
            }

            clickAction('insert-reference');
            const reference = captured[captured.length - 1];
            process.stdout.write('ENVELOPE ' + JSON.stringify(reference) + '\\n');
            if (reference.kind !== 'reference') throw new Error('kind');
            if (reference.moment_id !== 'f-12') throw new Error('moment ' + reference.moment_id);
            if (reference.field !== 'transcript') throw new Error('field ' + reference.field);
            if (reference.quote !== 'the button label is clipped') throw new Error('quote');
            if (reference.timestamp !== '12.5') throw new Error('timestamp ' + reference.timestamp);
            if ('transcriptError' in reference || 'rect' in reference) {
                throw new Error('event detail leaked internal fields');
            }

            arm(transcriptText, '', true);
            if (!bar.classList.contains('is-open')) throw new Error('hide should wait out the 250ms');
            flushTimers();
            if (bar.classList.contains('is-open')) throw new Error('collapsed selection should hide the toolbar');

            arm(summaryText, 'Login button overflows', false);
            flushTimers();
            if (!bar.classList.contains('is-open')) throw new Error('summary selection should show the toolbar');
            if (err.hidden !== true) throw new Error('transcript error action must hide outside transcript containers');
            clickAction('insert-reference');
            const summaryEvent = captured[captured.length - 1];
            if (summaryEvent.field !== 'summary' || summaryEvent.moment_id !== 'f-12') {
                throw new Error('summary envelope ' + JSON.stringify(summaryEvent));
            }
            process.stdout.write('show-hide:ok\\n');
            """
        )
    )
    assert "show-hide:ok" in output
    envelope_line = next(line for line in output.splitlines() if line.startswith("ENVELOPE "))
    assert json.loads(envelope_line.removeprefix("ENVELOPE ")) == {
        "kind": "reference",
        "moment_id": "f-12",
        "field": "transcript",
        "quote": "the button label is clipped",
        "timestamp": "12.5",
    }


def test_transcript_error_summary_fallback_and_silent_dispatch() -> None:
    output = _run_toolbar(
        textwrap.dedent(
            """
            const api = screenscribeSelectionToolbar;
            const bar = document.getElementById('ss-selection-toolbar');
            const captured = [];
            document.addEventListener('screenscribe:agent-insert-reference', (event) => {
                captured.push(event.detail);
            });

            function text(value, parent) {
                const node = document.createTextNode(value);
                parent.appendChild(node);
                return node;
            }
            function arm(node, quote) {
                const selection = {
                    anchorNode: node,
                    isCollapsed: false,
                    rangeCount: 1,
                    toString() { return quote; },
                    getRangeAt() {
                        return {
                            startContainer: node,
                            getBoundingClientRect() {
                                return { left: 12, top: 40, right: 80, bottom: 60, width: 68, height: 20 };
                            },
                        };
                    },
                };
                document.getSelection = () => selection;
                window.getSelection = () => selection;
                document.dispatchEvent({ type: 'selectionchange' });
                flushTimers();
                return selection;
            }
            function clickAction(action) {
                const button = [...bar.children].find((node) => node.getAttribute('data-action') === action);
                button.dispatchEvent({ type: 'click', target: button });
            }

            const card = document.createElement('article');
            card.className = 'finding';
            card.setAttribute('data-finding-id', 'f-9');
            const meta = document.createElement('span');
            meta.className = 'finding-meta';
            meta.setAttribute('data-timestamp', '4');
            card.appendChild(meta);
            const details = document.createElement('dl');
            details.className = 'finding-details';
            const fixDt = document.createElement('dt');
            fixDt.setAttribute('data-i18n', 'suggestedFix');
            const fixDd = document.createElement('dd');
            const fixText = text('align the label', fixDd);
            details.appendChild(fixDt);
            details.appendChild(fixDd);
            card.appendChild(details);
            document.body.appendChild(card);
            arm(fixText, 'align the label');
            clickAction('insert-reference');
            const fixEvent = captured[captured.length - 1];
            if (fixEvent.field !== 'suggested_fix' || fixEvent.moment_id !== 'f-9' || fixEvent.timestamp !== '4') {
                throw new Error('suggested_fix envelope ' + JSON.stringify(fixEvent));
            }
            const err = [...bar.children].find((node) => node.getAttribute('data-action') === 'transcript-error');
            if (err.hidden !== true) throw new Error('suggested fix is not a transcript container');

            const bare = document.createElement('article');
            bare.className = 'finding';
            const title = document.createElement('span');
            title.className = 'finding-title';
            title.textContent = '#2 BUG';
            const bareTranscript = document.createElement('div');
            bareTranscript.className = 'finding-transcript';
            const bareText = text('missing id quote', bareTranscript);
            bare.appendChild(title);
            bare.appendChild(bareTranscript);
            document.body.appendChild(bare);
            arm(bareText, 'missing id quote');
            clickAction('transcript-error');
            const fallback = captured[captured.length - 1];
            process.stdout.write('ENVELOPE ' + JSON.stringify(fallback) + '\\n');
            if (fallback.kind !== 'transcript-error') throw new Error('kind ' + fallback.kind);
            if (fallback.moment_id !== '#2 BUG') throw new Error('heading fallback ' + fallback.moment_id);
            if (fallback.field !== 'transcript') throw new Error('field ' + fallback.field);
            if (fallback.timestamp !== null) throw new Error('bare card timestamp ' + fallback.timestamp);

            const summary = document.createElement('div');
            summary.className = 'executive-summary';
            const summaryText = text('Ship the login fix', summary);
            document.body.appendChild(summary);
            arm(summaryText, 'Ship the login fix');
            if (err.hidden !== true) throw new Error('executive summary must not offer transcript error');
            clickAction('insert-reference');
            const executive = captured[captured.length - 1];
            if (executive.moment_id !== null || executive.field !== 'executive_summary' || executive.timestamp !== null) {
                throw new Error('executive envelope ' + JSON.stringify(executive));
            }

            const cue = document.createElement('div');
            cue.className = 'subtitle-item';
            cue.setAttribute('data-segment-id', '7');
            const cueText = text('hello from the tape', cue);
            const list = document.createElement('div');
            list.className = 'subtitle-list transcript-list';
            list.appendChild(cue);
            document.body.appendChild(list);
            window.TRANSCRIPT_SEGMENTS = [{ id: 7, start: 3.5, end: 4.2, text: 'hello from the tape' }];
            arm(cueText, 'hello from the tape');
            if (err.hidden !== false) throw new Error('subtitle selection should offer transcript error');
            clickAction('transcript-error');
            const cueEvent = captured[captured.length - 1];
            if (cueEvent.kind !== 'transcript-error' || cueEvent.field !== 'subtitle') {
                throw new Error('subtitle kind/field ' + JSON.stringify(cueEvent));
            }
            if (cueEvent.moment_id !== null || cueEvent.timestamp !== '3.5') {
                throw new Error('subtitle envelope ' + JSON.stringify(cueEvent));
            }

            const longQuote = 'a'.repeat(2001);
            const capped = api.envelopeForSelection({
                isCollapsed: false,
                rangeCount: 1,
                anchorNode: bareText,
                toString() { return longQuote; },
                getRangeAt() {
                    return { startContainer: bareText, getBoundingClientRect() { return { left: 0, top: 0, right: 1, bottom: 1, width: 1, height: 1 }; } };
                },
            }, document);
            if (!capped || capped.quote.length !== 2000) throw new Error('quote cap');

            document._listeners = {};
            const originalDispatch = document.dispatchEvent.bind(document);
            document.dispatchEvent = (event) => {
                if (event && event.type === 'screenscribe:agent-insert-reference' && window.__explode) {
                    throw new Error('listener exploded');
                }
                return originalDispatch(event);
            };
            arm(bareText, 'missing id quote');
            window.__explode = false;
            clickAction('insert-reference');
            window.__explode = true;
            clickAction('transcript-error');
            if (!bar.classList.contains('is-open')) throw new Error('throwing listener wedged the toolbar');
            process.stdout.write('envelope-edges:ok\\n');
            """
        )
    )
    assert "envelope-edges:ok" in output
    envelope_line = next(line for line in output.splitlines() if line.startswith("ENVELOPE "))
    assert json.loads(envelope_line.removeprefix("ENVELOPE ")) == {
        "kind": "transcript-error",
        "moment_id": "#2 BUG",
        "field": "transcript",
        "quote": "missing id quote",
        "timestamp": None,
    }


def test_selection_toolbar_i18n_keys_have_pl_en_parity() -> None:
    bundle = _bundle()
    toolbar_js = TOOLBAR_JS.read_text(encoding="utf-8")
    en_keys = {key for key in bundle["en"]["review"] if key.startswith("selectionToolbar.")}
    pl_keys = {key for key in bundle["pl"]["review"] if key.startswith("selectionToolbar.")}
    assert en_keys == pl_keys == _REQUIRED_KEYS
    for key in _REQUIRED_KEYS:
        assert bundle["en"]["review"][key]
        assert bundle["pl"]["review"][key]
        assert bundle["en"]["review"][key] != bundle["pl"]["review"][key]
        assert f"review.{key}" in toolbar_js


def test_review_report_inlines_selection_toolbar_and_analyze_does_not() -> None:
    html = render_html_report_pro(
        video_name="toolbar.mov",
        video_path=None,
        generated_at="2026-09-28T00:00:00",
        executive_summary="A claim.",
        findings=[],
        segments=[],
        errors=[],
        language="en",
    )
    assert "screenscribe:agent-insert-reference" in html
    assert "screenscribeSelectionToolbar" in html
    assert ".ss-selection-toolbar" in html
    assert html.index("screenscribeAgentPanel") < html.index("screenscribeSelectionToolbar")
    assert "cdnjs.cloudflare.com" not in html
    assert 'src="http' not in html

    analyze = _analyze_html()
    assert "screenscribe:agent-insert-reference" not in analyze
    assert "ss-selection-toolbar" not in analyze


def test_selection_toolbar_assets_avoid_cdn_onclick_and_raw_colors() -> None:
    css = TOOLBAR_CSS.read_text(encoding="utf-8")
    js = TOOLBAR_JS.read_text(encoding="utf-8")
    assert "http://" not in css and "https://" not in css
    assert "@import" not in css
    assert "onclick" not in css and "onclick" not in js
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", css) is None
    assert "var(--" in css
    assert "pointer-events: none" in css
    assert "pointer-events: auto" in css


def test_selection_spanning_two_findings_is_refused() -> None:
    """R4-01: a cross-finding selection must not be attributed to the first card.

    envelopeForSelection used to take the finding id and timestamp from the
    selection START only, so marking text across two cards sent both cards'
    words to the agent under the first finding's id.
    """
    output = _run_toolbar(
        textwrap.dedent(
            """
            const api = screenscribeSelectionToolbar;
            const bar = document.getElementById('ss-selection-toolbar');
            const captured = [];
            document.addEventListener('screenscribe:agent-insert-reference', (event) => {
                captured.push(event.detail);
            });

            function text(value, parent) {
                const node = document.createTextNode(value);
                parent.appendChild(node);
                return node;
            }
            function card(id, ts, quote) {
                const el = document.createElement('article');
                el.className = 'finding';
                el.setAttribute('data-finding-id', id);
                const meta = document.createElement('span');
                meta.className = 'finding-meta';
                meta.setAttribute('data-timestamp', ts);
                el.appendChild(meta);
                const transcript = document.createElement('div');
                transcript.className = 'finding-transcript';
                const node = text(quote, transcript);
                el.appendChild(transcript);
                document.body.appendChild(el);
                return node;
            }
            const first = card('first', '12', 'Problem with the Login button.');
            const second = card('second', '55', 'Secret in another finding.');

            function armSpan(startNode, endNode, quote) {
                const selection = {
                    anchorNode: startNode,
                    focusNode: endNode,
                    isCollapsed: false,
                    rangeCount: 1,
                    toString() { return quote; },
                    getRangeAt() {
                        return {
                            startContainer: startNode,
                            endContainer: endNode,
                            getBoundingClientRect() {
                                return { left: 40, top: 120, right: 240, bottom: 140, width: 200, height: 20 };
                            },
                        };
                    },
                };
                document.getSelection = () => selection;
                window.getSelection = () => selection;
                document.dispatchEvent({ type: 'selectionchange' });
            }

            // Selection crossing both cards: the toolbar must stay away.
            armSpan(first, second, 'Problem with the Login button. Secret in another finding.');
            flushTimers();
            if (bar.classList.contains('is-open')) {
                throw new Error('cross-finding selection must not open the toolbar');
            }

            // A selection inside ONE card still works, with its own id.
            armSpan(second, second, 'Secret in another finding.');
            flushTimers();
            if (!bar.classList.contains('is-open')) {
                throw new Error('single-card selection must still open the toolbar');
            }
            const button = [...bar.children].find((node) => node.getAttribute('data-action') === 'insert-reference');
            button.dispatchEvent({ type: 'click', target: button });
            const reference = captured[captured.length - 1];
            if (reference.moment_id !== 'second' || reference.timestamp !== '55') {
                throw new Error('single-card envelope ' + JSON.stringify(reference));
            }
            process.stdout.write('cross-finding:ok\\n');
            """
        )
    )
    assert "cross-finding:ok" in output


def test_merged_frame_transcript_offers_the_transcript_error_action() -> None:
    """R2-03: merged-frame narration is transcript too — same correction path."""
    output = _run_toolbar(
        textwrap.dedent(
            """
            const bar = document.getElementById('ss-selection-toolbar');
            const err = [...bar.children].find((node) => node.getAttribute('data-action') === 'transcript-error');

            const card = document.createElement('article');
            card.className = 'finding';
            card.setAttribute('data-finding-id', 'f-7');
            const merged = document.createElement('span');
            merged.className = 'merged-frame-transcript';
            const node = document.createTextNode('zeto zeten in a merged frame');
            merged.appendChild(node);
            card.appendChild(merged);
            document.body.appendChild(card);

            const selection = {
                anchorNode: node,
                isCollapsed: false,
                rangeCount: 1,
                toString() { return 'zeto zeten in a merged frame'; },
                getRangeAt() {
                    return {
                        startContainer: node,
                        getBoundingClientRect() {
                            return { left: 40, top: 120, right: 240, bottom: 140, width: 200, height: 20 };
                        },
                    };
                },
            };
            document.getSelection = () => selection;
            window.getSelection = () => selection;
            document.dispatchEvent({ type: 'selectionchange' });
            flushTimers();
            if (!bar.classList.contains('is-open')) throw new Error('toolbar closed for merged-frame text');
            if (err.hidden !== false) throw new Error('transcript-error action hidden for merged-frame text');
            process.stdout.write('merged-frame:ok\\n');
            """
        )
    )
    assert "merged-frame:ok" in output
