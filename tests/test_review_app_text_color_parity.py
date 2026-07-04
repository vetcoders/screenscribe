"""Text annotation colour control must be IDENTICAL to shape colour control.

Operator signal (cut I3): "just like you can change the colour of arrows, the
drawing and the rectangle on the photo while annotating, it would be easiest if
you could change the text colour identically too".

Root cause of the asymmetry (review_app.js ``LightboxAnnotationTool``):

* Shapes (pen / rect / arrow) flow through the on-canvas pointer draft loop.
  The persistent toolbar ``.color-picker`` stays reachable the whole time and
  the draft is recoloured live from ``this.color`` as you draw.
* Text used a BLOCKING native ``window.prompt('Text annotation')`` fired the
  instant you clicked. While that OS modal is up the toolbar (and the colour
  picker) is unreachable and there is no live colour preview, so the colour
  could only ever be whatever ``this.color`` happened to be BEFORE the click --
  it could not be picked / changed "in the same place, in the same moment" as
  for shapes.

The fix routes text through the same single ``this.color`` source of truth via a
non-blocking inline draft that the toolbar picker recolours live, exactly like a
shape draft. These tests genuinely execute ``LightboxAnnotationTool`` methods
and ``createAnnotationElement`` from ``review_app.js`` in a node sandbox.
"""

from __future__ import annotations

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


def _run_js(assertions: str) -> None:
    """Execute review_app.js in node, then run assertions in the SAME vm scope."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js annotation tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        function makeEl(tag) {{
            return {{
                tagName: tag, attributes: {{}}, children: [], textContent: '', _parent: null,
                style: {{}},
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                setAttribute(k, v) {{ this.attributes[k] = String(v); }},
                getAttribute(k) {{ return this.attributes[k]; }},
                appendChild(c) {{ c._parent = this; this.children.push(c); return c; }},
                removeChild(c) {{ const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }},
                remove() {{ if (this._parent) this._parent.removeChild(this); }},
                addEventListener() {{}}, removeEventListener() {{}},
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

        const sandbox = {{
            console, setTimeout, clearTimeout, Math, Date, JSON, Promise,
            getComputedStyle, XMLSerializer,
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
        sandbox.globalThis = sandbox;

        const sources = [
            {str(I18N_JS)!r}, {str(LANGUAGE_CONTROL_JS)!r}, {str(STT_TRANSPORT_JS)!r},
            {str(TAB_KEYBOARD_JS)!r}, {str(REVIEW_APP_JS)!r}
        ].map((p) => fs.readFileSync(p, 'utf8')).join("\\n");

        const driver = `
            function assertTrue(cond, msg) {{ if (!cond) throw new Error(msg); }}
            // A bare LightboxAnnotationTool with only the fields a method needs,
            // bypassing the DOM-heavy constructor.
            function makeTool(color) {{
                const tool = Object.create(LightboxAnnotationTool.prototype);
                tool.color = color;
                tool.annotations = [];
                tool.svg = document.createElementNS('{SVG_NS}', 'svg');
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


def test_text_annotation_commits_with_shared_picker_colour() -> None:
    """A committed text annotation must carry ``this.color`` (the one toolbar
    picker value) -- the same single source of truth shapes read."""
    _run_js(
        """
        const tool = makeTool('#bc1515');
        tool.beginTextDraft({ x: 0.2, y: 0.3 });
        assertTrue(tool.textDraft, 'beginTextDraft did not open a text draft');
        tool.commitTextDraft('hello');
        assertTrue(tool.annotations.length === 1, 'text not committed: ' + JSON.stringify(tool.annotations));
        const ann = tool.annotations[0];
        assertTrue(ann.type === 'text', 'wrong annotation type: ' + ann.type);
        assertTrue(ann.text === 'hello', 'text content lost: ' + ann.text);
        assertTrue(ann.color === '#bc1515', 'text did NOT take picker colour this.color: ' + ann.color);
        assertTrue(ann.x === 0.2 && ann.y === 0.3, 'text position lost: ' + ann.x + ',' + ann.y);
        assertTrue(tool.textDraft === null, 'text draft not cleared after commit');
        """
    )


def test_color_picker_recolours_active_text_draft_live() -> None:
    """Changing the picker while a text draft is open must recolour the live
    draft -- the same 'change colour in the moment' affordance shapes have."""
    _run_js(
        """
        const tool = makeTool('#bc1515');
        tool.beginTextDraft({ x: 0.1, y: 0.1 });
        // Simulate the user moving the toolbar colour picker mid-entry.
        tool.color = '#22cc44';
        tool.applyColorToActiveDraft();
        const el = tool.textDraft.el;
        assertTrue(el && el.getAttribute('fill') === '#22cc44',
            'picker change did NOT recolour the live text draft: ' + (el && el.getAttribute('fill')));
        // And the committed annotation keeps the changed colour.
        tool.commitTextDraft('later');
        assertTrue(tool.annotations[0].color === '#22cc44',
            'committed text lost the changed colour: ' + tool.annotations[0].color);
        """
    )


def test_text_and_shapes_share_one_colour_source() -> None:
    """createAnnotationElement must apply ``ann.color`` to BOTH text (fill) and
    shapes (stroke) -- proving one shared colour mechanism across types."""
    _run_js(
        """
        const text = createAnnotationElement({ type: 'text', text: 'x', x: 0.1, y: 0.1, color: '#abcdef', fontSizeRel: 0.04 });
        assertTrue(text && text.getAttribute('fill') === '#abcdef', 'text fill != ann.color: ' + (text && text.getAttribute('fill')));
        const rect = createAnnotationElement({ type: 'rect', x: 0.1, y: 0.1, width: 0.2, height: 0.2, color: '#abcdef', strokeWidthRel: 0.01 });
        // rect is a contrast group: the coloured stroke is the SECOND child.
        const coloured = rect.children[rect.children.length - 1];
        assertTrue(coloured && coloured.getAttribute('stroke') === '#abcdef', 'rect stroke != ann.color: ' + (coloured && coloured.getAttribute('stroke')));
        """
    )


def test_text_path_no_longer_blocks_on_window_prompt() -> None:
    """The blocking native ``window.prompt`` text path (which locked out the
    colour picker) must be gone -- it is the root cause of the asymmetry."""
    js = REVIEW_APP_JS.read_text(encoding="utf-8")
    assert "window.prompt(" not in js, (
        "review_app.js still uses blocking window.prompt for text annotation "
        "(colour picker unreachable during text entry -> not identical to shapes)"
    )
