"""Round-7 P3: live annotation drafts must update inside the contrast group.

Finding H1 made ``createAnnotationElement`` return a ``<g>`` wrapper with separate
halo (dark under-stroke) and foreground (coloured) shapes for rect / pen / arrow.
But ``LightboxAnnotationTool.draw()`` still treated ``this.draftEl`` as a flat
rect/path and mutated ``width`` / ``d`` / ``stroke`` directly on it. For rect and
pen those writes landed on the GROUP (no visual effect); for the arrow only the
first two children (both halo shapes) were updated, so the foreground coloured
arrow never followed the drag. The reviewer lost the live coloured preview while
placing an annotation.

The fix rebuilds the draft element from the live geometry via
``createAnnotationElement`` each move, so BOTH halo and foreground shapes inside
the ``<g>`` reflect the current geometry and colour. These tests genuinely
execute ``LightboxAnnotationTool`` ``startDraw`` / ``draw`` in a node sandbox.
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
                removeChild(c) {{ const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c._parent = null; return c; }},
                replaceChild(nw, old) {{
                    const i = this.children.indexOf(old);
                    if (i >= 0) {{ this.children[i] = nw; nw._parent = this; old._parent = null; }}
                    return old;
                }},
                remove() {{ if (this._parent) this._parent.removeChild(this); }},
                setPointerCapture() {{}}, releasePointerCapture() {{}},
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
            const evt = {{ stopPropagation() {{}}, pointerId: 1 }};
            // A bare tool with only the fields startDraw/draw touch.
            function makeTool(toolName, color) {{
                const tool = Object.create(LightboxAnnotationTool.prototype);
                tool.tool = toolName;
                tool.color = color;
                tool.strokeWidth = 0.01;
                tool.annotations = [];
                tool.currentPath = [];
                tool.isDrawing = false;
                tool.svg = document.createElementNS('{SVG_NS}', 'svg');
                tool.textDraft = null;
                tool.draftEl = null;
                tool._next = {{ x: 0.5, y: 0.5, w: 1, h: 1 }};
                tool.getPosPct = () => tool._next;
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


def test_arrow_draft_updates_foreground_inside_group() -> None:
    """The arrow draft is a <g> with [halo_line, halo_head, fg_line, fg_head].
    Dragging must move the FOREGROUND (coloured) line, not just the halo."""
    _run_js(
        """
        const tool = makeTool('arrow', '#ff0000');
        tool._next = { x: 0.5, y: 0.5, w: 1, h: 1 };
        tool.startDraw(evt);
        // Drag the end point away from the start.
        tool._next = { x: 0.8, y: 0.6, w: 1, h: 1 };
        tool.draw(evt);
        const kids = tool.draftEl.children || [];
        const HALO = 'rgba(0, 0, 0, 0.85)';
        const dump = JSON.stringify(kids.map((k) => k.tagName + ':' + k.getAttribute('stroke')));
        // The dark halo line must survive (pre-fix draw() recoloured the first two
        // halo children to this.color, leaving no dark under-stroke).
        const haloLine = kids.find((k) => k.tagName === 'line' && k.getAttribute('stroke') === HALO);
        assertTrue(haloLine, 'halo line lost/recoloured (halo must stay dark): ' + dump);
        // The foreground (coloured) line must follow the drag to the new end (0.8).
        const fgLine = kids.find((k) => k.tagName === 'line' && k.getAttribute('stroke') === '#ff0000');
        assertTrue(fgLine, 'no coloured foreground line in arrow draft: ' + dump);
        assertTrue(Math.abs(Number(fgLine.getAttribute('x2')) - 0.8) < 1e-6,
            'arrow foreground line did not follow the drag: x2=' + fgLine.getAttribute('x2'));
        assertTrue(Math.abs(Number(haloLine.getAttribute('x2')) - 0.8) < 1e-6,
            'arrow halo line did not follow the drag: x2=' + haloLine.getAttribute('x2'));
        """
    )


def test_rect_draft_updates_foreground_inside_group() -> None:
    """The rect draft is a <g> with [halo_rect, fg_rect]. Dragging must size the
    FOREGROUND coloured rect (the writes must reach the child, not the group)."""
    _run_js(
        """
        const tool = makeTool('rect', '#00ff00');
        tool._next = { x: 0.2, y: 0.2, w: 1, h: 1 };
        tool.startDraw(evt);
        tool._next = { x: 0.6, y: 0.5, w: 1, h: 1 };
        tool.draw(evt);
        const kids = tool.draftEl.children || [];
        const fgRect = kids.find((k) => k.tagName === 'rect'
            && k.getAttribute('stroke') === '#00ff00');
        assertTrue(fgRect, 'no coloured foreground rect in rect draft: '
            + JSON.stringify(kids.map((k) => k.tagName + ':' + k.getAttribute('stroke'))));
        assertTrue(Math.abs(Number(fgRect.getAttribute('width')) - 0.4) < 1e-6,
            'rect foreground width did not follow the drag: width='
            + fgRect.getAttribute('width'));
        """
    )


def test_pen_draft_updates_foreground_inside_group() -> None:
    """The pen draft is a <g> with [halo_path, fg_path]. Each move must extend the
    FOREGROUND coloured path, not write 'd' onto the inert group."""
    _run_js(
        """
        const tool = makeTool('pen', '#0000ff');
        tool._next = { x: 0.1, y: 0.1, w: 1, h: 1 };
        tool.startDraw(evt);
        tool._next = { x: 0.9, y: 0.9, w: 1, h: 1 };
        tool.draw(evt);
        const kids = tool.draftEl.children || [];
        const fgPath = kids.find((k) => k.tagName === 'path'
            && k.getAttribute('stroke') === '#0000ff');
        assertTrue(fgPath, 'no coloured foreground path in pen draft: '
            + JSON.stringify(kids.map((k) => k.tagName + ':' + k.getAttribute('stroke'))));
        const d = fgPath.getAttribute('d') || '';
        assertTrue(d.includes('0.9'),
            'pen foreground path did not extend to the dragged point: d=' + d);
        """
    )
