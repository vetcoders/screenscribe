"""Text annotation input must survive a REAL mouse click (cut I3 regression).

Operator signal: after clicking with the text tool, "there is no field to type
in" -- the inline ``<input class="annotation-text-input">`` vanishes the instant
it appears on a real click.

Root cause (verified live in Chrome, real vs synthetic click) in
``LightboxAnnotationTool.createTextInput`` (review_app.js):

* A real mouse gesture is ``pointerdown`` (which creates the input and called a
  SYNCHRONOUS ``input.focus()``) followed by ``pointerup`` (which pulls focus
  back off the brand-new input). That focus loss fires the input's ``blur``
  handler, whose ``onBlur = () => commitTextDraft(input.value)`` committed an
  EMPTY value -> ``commitTextDraft`` deletes an empty draft -> the field is gone
  before the operator can type. Synthetic ``pointerdown`` events skip real focus
  management, so the original node-vm I3 tests passed falsely.

The fix has two halves and these tests pin BOTH:

1. Focus is DEFERRED past the opening gesture (``requestAnimationFrame`` /
   ``setTimeout``) instead of synchronous, so ``pointerup`` finishes first and
   focus sticks.
2. ``onBlur`` carries a ``ready`` guard: an instant blur fired BEFORE the draft
   is armed (focused) is ignored, so the field stays alive; a blur AFTER arming
   still commits/cancels exactly as before.

The structural tests assert the source invariants statically. The behavioural
test drives the real handlers through a node sandbox with a CONTROLLABLE
``requestAnimationFrame`` and a DOM stub that actually records and dispatches
listeners -- so it reproduces the instant-blur the original I3 sandbox could
not, then proves the guard keeps the field alive.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "screenscribe/html_pro_assets"
I18N_JS = ASSETS / "scripts/i18n.js"
LANGUAGE_CONTROL_JS = ASSETS / "scripts/lib/language-control.js"
STT_TRANSPORT_JS = ASSETS / "scripts/lib/stt-transport.js"
TAB_KEYBOARD_JS = ASSETS / "scripts/lib/tab-keyboard.js"
REVIEW_APP_JS = ASSETS / "scripts/review_app.js"

SVG_NS = "http://www.w3.org/2000/svg"


def _create_text_input_block() -> str:
    """Return the source of ``createTextInput`` (up to the next method)."""
    js = REVIEW_APP_JS.read_text(encoding="utf-8")
    start = js.index("createTextInput(pos) {")
    end = js.index("removeTextInput(", start)
    return js[start:end]


# --------------------------------------------------------------------------- #
# Structural invariants (catch the synchronous-focus / unguarded-blur shapes). #
# --------------------------------------------------------------------------- #


def test_focus_is_deferred_not_synchronous() -> None:
    """``createTextInput`` must NOT call ``input.focus()`` synchronously; it must
    schedule it past the gesture via requestAnimationFrame/setTimeout."""
    block = _create_text_input_block()
    assert "input.focus()" in block, "focus must still happen (just deferred)"
    assert re.search(r"requestAnimationFrame\(", block) or re.search(r"setTimeout\(", block), (
        "focus is not deferred (no requestAnimationFrame/setTimeout in createTextInput)"
    )
    # The focus call must live inside a deferred callback, not at the top level.
    assert re.search(r"=>\s*\{[\s\S]*?input\.focus\(\)", block), (
        "input.focus() is not wrapped in a callback (still synchronous?)"
    )


def test_onblur_has_ready_guard_against_instant_blur() -> None:
    """``onBlur`` must early-return while the draft is not yet armed, so the
    instant blur from the opening gesture cannot commit/delete an empty draft."""
    block = _create_text_input_block()
    assert re.search(r"let\s+ready\s*=\s*false", block), "no `ready` arming flag declared"
    onblur = block[block.index("const onBlur") : block.index("input.addEventListener")]
    assert re.search(r"if\s*\(\s*!\s*ready\s*\)\s*return", onblur), (
        "onBlur does not guard against the instant (pre-ready) blur"
    )
    # The arming callback must flip the flag true after focusing.
    assert re.search(r"ready\s*=\s*true", block), "`ready` is never set true after focus"


# --------------------------------------------------------------------------- #
# Behavioural test: reproduce the instant-blur the original I3 sandbox missed.  #
# --------------------------------------------------------------------------- #


def _run_js(assertions: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js annotation tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        // DOM stub that ACTUALLY records and can dispatch listeners, so we can
        // fire a real 'blur' the way the browser does (the original I3 sandbox
        // used no-op addEventListener and so never reproduced this bug).
        function makeEl(tag) {{
            return {{
                tagName: tag, attributes: {{}}, children: [], textContent: '', _parent: null,
                value: '', type: '', className: '', style: {{}},
                _listeners: {{}},
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                setAttribute(k, v) {{ this.attributes[k] = String(v); }},
                getAttribute(k) {{ return this.attributes[k]; }},
                appendChild(c) {{ c._parent = this; this.children.push(c); return c; }},
                removeChild(c) {{ const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }},
                remove() {{ if (this._parent) this._parent.removeChild(this); }},
                addEventListener(type, fn) {{ (this._listeners[type] || (this._listeners[type] = [])).push(fn); }},
                removeEventListener() {{}},
                dispatch(type, evt) {{ (this._listeners[type] || []).forEach((fn) => fn(evt || {{}})); }},
                get firstChild() {{ return this.children[0] || null; }},
                get parentNode() {{ return this._parent; }},
            }};
        }}

        const documentStub = {{
            documentElement: {{}},
            body: makeEl('body'),
            addEventListener() {{}},
            querySelector() {{ return null; }},
            querySelectorAll() {{ return []; }},
            getElementById() {{ return null; }},
            createElementNS(_ns, tag) {{ return makeEl(tag); }},
            createElement(tag) {{ return makeEl(tag); }},
        }};

        function getComputedStyle() {{ return {{ getPropertyValue() {{ return ''; }} }}; }}
        class XMLSerializer {{ serializeToString() {{ return ''; }} }}

        // Controllable requestAnimationFrame: callbacks queue until flushRaf().
        const rafQueue = [];
        function requestAnimationFrame(cb) {{ rafQueue.push(cb); return rafQueue.length; }}
        function flushRaf() {{ const q = rafQueue.splice(0); q.forEach((cb) => cb()); }}

        const sandbox = {{
            console, setTimeout, clearTimeout, Math, Date, JSON, Promise,
            getComputedStyle, XMLSerializer, requestAnimationFrame, flushRaf,
            window: {{ location: {{ search: '' }}, addEventListener() {{}}, removeEventListener() {{}} }},
            document: documentStub,
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            navigator: {{ mediaDevices: {{}} }},
            Blob: class Blob {{ constructor(c, o = {{}}) {{ this.chunks = c; this.type = o.type || ''; }} }},
            ResizeObserver: class ResizeObserver {{ observe() {{}} disconnect() {{}} }},
            Image: class Image {{}},
            URL: {{ createObjectURL() {{ return 'blob:x'; }}, revokeObjectURL() {{}} }},
            process, confirm() {{ return true; }},
            fetch() {{ throw new Error('no network in test'); }},
        }};
        sandbox.window.document = sandbox.document;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.window.getComputedStyle = getComputedStyle;
        sandbox.window.XMLSerializer = XMLSerializer;
        sandbox.window.requestAnimationFrame = requestAnimationFrame;
        sandbox.globalThis = sandbox;

        const sources = [
            {str(I18N_JS)!r}, {str(LANGUAGE_CONTROL_JS)!r}, {str(STT_TRANSPORT_JS)!r},
            {str(TAB_KEYBOARD_JS)!r}, {str(REVIEW_APP_JS)!r}
        ].map((p) => fs.readFileSync(p, 'utf8')).join("\\n");

        const driver = `
            function assertTrue(cond, msg) {{ if (!cond) throw new Error(msg); }}
            function makeTool(color) {{
                const tool = Object.create(LightboxAnnotationTool.prototype);
                tool.color = color;
                tool.annotations = [];
                tool.svg = document.createElementNS('{SVG_NS}', 'svg');
                tool.svg._parent = document.body; // give createTextInput a host
                tool.textDraft = null;
                tool.draftEl = null;
                return tool;
            }}
            {assertions}
        `;

        const script = new vm.Script(sources + "\\n" + driver, {{ filename: 'review_app.js' }});
        script.runInNewContext(sandbox);
        """
    )
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_instant_blur_before_ready_keeps_field_alive() -> None:
    """The blur fired by the opening gesture (before the draft is armed) must NOT
    delete the field -- this is the exact real-click failure mode."""
    _run_js(
        """
        const tool = makeTool('#bc1515');
        const draft = tool.beginTextDraft({ x: 0.2, y: 0.3 });
        assertTrue(tool.textDraft, 'beginTextDraft did not open a draft');
        const input = draft.input;
        assertTrue(input, 'no input element created');

        // Simulate the real gesture's instant blur BEFORE requestAnimationFrame
        // has armed the draft (i.e. before focus settles). With the bug this
        // committed an empty value and removed the input.
        input.dispatch('blur');

        assertTrue(tool.textDraft !== null, 'instant blur deleted the text draft (regression)');
        assertTrue(tool.annotations.length === 0, 'instant blur wrongly committed an empty annotation');
        assertTrue(input._parent !== null, 'input was removed from the DOM on instant blur');
        """
    )


def test_blur_after_ready_still_commits_typed_text() -> None:
    """Once armed (focus settled), a blur with real text still commits -- the
    'click elsewhere after typing = commit' behaviour must be preserved."""
    _run_js(
        """
        const tool = makeTool('#bc1515');
        const draft = tool.beginTextDraft({ x: 0.1, y: 0.4 });
        const input = draft.input;

        // Arm the draft the way the next animation frame would.
        flushRaf();

        // Operator types, then clicks elsewhere -> blur commits.
        input.value = 'hello';
        input.dispatch('blur');

        assertTrue(tool.textDraft === null, 'draft not cleared after commit');
        assertTrue(tool.annotations.length === 1, 'typed text not committed on blur after arming');
        assertTrue(tool.annotations[0].text === 'hello', 'committed text wrong: ' + tool.annotations[0].text);
        assertTrue(tool.annotations[0].color === '#bc1515', 'committed text lost picker colour');
        """
    )


def test_empty_blur_after_ready_discards_draft() -> None:
    """An armed but empty draft blurred away is discarded (no empty annotation),
    matching the pre-regression cancel-on-empty intent."""
    _run_js(
        """
        const tool = makeTool('#bc1515');
        const draft = tool.beginTextDraft({ x: 0.5, y: 0.5 });
        const input = draft.input;
        flushRaf(); // arm

        input.value = '';
        input.dispatch('blur');

        assertTrue(tool.textDraft === null, 'empty armed draft should be discarded on blur');
        assertTrue(tool.annotations.length === 0, 'empty draft must not create an annotation');
        """
    )
