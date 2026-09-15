"""Annotation editor model: stable ids, select, move/resize, delete, overlay sync.

The lightbox editor used to be write-only (push / pop / clear). These tests load
``review_app.js`` in a node ``vm`` (same harness shape as
``test_review_app_draft_contrast_group.py``) and assert the editable model:

* every annotation gets a stable ``id``; records without one are migrated
* select hit-tests rect / arrow / pen / text; empty space clears selection
* move and resize mutate geometry in normalized 0-1 space
* Delete removes the selection; colour / stroke / fontSizeRel apply to it
* overlay layout re-reads ``getActualImageRect`` after a scroll-like box change
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
        pytest.fail("node is required for annotation model tests (fail-closed)")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        function makeEl(tag) {{
            const classes = new Set();
            return {{
                tagName: tag, attributes: {{}}, children: [], textContent: '', _parent: null,
                style: {{}}, dataset: {{}},
                classList: {{
                    add(...xs) {{ xs.forEach((x) => classes.add(x)); }},
                    remove(...xs) {{ xs.forEach((x) => classes.delete(x)); }},
                    toggle(x, on) {{
                        if (on === undefined) {{
                            if (classes.has(x)) classes.delete(x); else classes.add(x);
                            return;
                        }}
                        if (on) classes.add(x); else classes.delete(x);
                    }},
                    contains(x) {{ return classes.has(x); }},
                }},
                setAttribute(k, v) {{ this.attributes[k] = String(v); }},
                getAttribute(k) {{ return this.attributes[k]; }},
                appendChild(c) {{ c._parent = this; this.children.push(c); return c; }},
                removeChild(c) {{
                    const i = this.children.indexOf(c);
                    if (i >= 0) this.children.splice(i, 1);
                    c._parent = null;
                    return c;
                }},
                replaceChild(nw, old) {{
                    const i = this.children.indexOf(old);
                    if (i >= 0) {{ this.children[i] = nw; nw._parent = this; old._parent = null; }}
                    return old;
                }},
                remove() {{ if (this._parent) this._parent.removeChild(this); }},
                setPointerCapture() {{}}, releasePointerCapture() {{}},
                addEventListener() {{}}, removeEventListener() {{}},
                querySelector() {{ return null; }},
                querySelectorAll() {{ return []; }},
                get firstChild() {{ return this.children[0] || null; }},
                get parentNode() {{ return this._parent; }},
            }};
        }}

        const documentStub = {{
            documentElement: {{}},
            body: makeEl('body'),
            addEventListener() {{}},
            removeEventListener() {{}},
            querySelector() {{ return null; }},
            querySelectorAll() {{ return []; }},
            getElementById() {{ return null; }},
            createElementNS(_ns, tag) {{ return makeEl(tag); }},
            createElement(tag) {{ return makeEl(tag); }},
        }};

        function getComputedStyle() {{ return {{ getPropertyValue() {{ return ''; }} }}; }}
        class XMLSerializer {{ serializeToString() {{ return ''; }} }}

        let uuidSeq = 0;
        const sandbox = {{
            console, setTimeout, clearTimeout, Math, Date, JSON, Promise,
            getComputedStyle, XMLSerializer,
            crypto: {{ randomUUID() {{ uuidSeq += 1; return '11111111-1111-4111-8111-' + String(uuidSeq).padStart(12, '0'); }} }},
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
        sandbox.window.crypto = sandbox.crypto;
        sandbox.globalThis = sandbox;

        const sources = [
            {str(I18N_JS)!r}, {str(LANGUAGE_CONTROL_JS)!r}, {str(STT_TRANSPORT_JS)!r},
            {str(TAB_KEYBOARD_JS)!r}, {str(REVIEW_APP_JS)!r}
        ].map((p) => fs.readFileSync(p, 'utf8')).join("\\n");

        const driver = `
            function assertTrue(cond, msg) {{ if (!cond) throw new Error(msg); }}
            function makeTool() {{
                const tool = Object.create(LightboxAnnotationTool.prototype);
                tool.tool = 'select';
                tool.color = '#ffffff';
                tool.strokeWidth = 0.008;
                tool.annotations = [];
                tool.selectedId = null;
                tool.dragMode = null;
                tool.findingId = null;
                tool.toolbar = null;
                tool.textDraft = null;
                tool.draftEl = null;
                tool.svg = document.createElementNS('{SVG_NS}', 'svg');
                tool.svg.style = {{}};
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


def test_ids_and_migration() -> None:
    """New annotations get ids; records without id are filled on ensure/load."""
    _run_js(
        """
        const fresh = ensureAnnotationId({ type: 'rect', x: 0.1, y: 0.1, width: 0.2, height: 0.2 });
        assertTrue(typeof fresh.id === 'string' && fresh.id.length > 8, 'new annotation missing id: ' + fresh.id);
        const legacy = { type: 'arrow', startX: 0.1, startY: 0.1, endX: 0.4, endY: 0.4 };
        assertTrue(legacy.id === undefined, 'fixture should start without id');
        const migrated = ensureAnnotationsHaveIds([legacy, { type: 'text', x: 0.2, y: 0.2, text: 'hi' }]);
        assertTrue(migrated.length === 2, 'migration dropped records');
        assertTrue(migrated.every((a) => typeof a.id === 'string' && a.id.length > 8),
            'legacy records did not receive ids: ' + JSON.stringify(migrated));
        assertTrue(migrated[0].id !== migrated[1].id, 'migration reused the same id');
        const snap = snapshotFindingReview({ annotations: [{ type: 'pen', points: [{ x: 0.1, y: 0.1 }] }] });
        assertTrue(snap.annotations[0].id, 'snapshotFindingReview did not migrate id');
        const tool = makeTool();
        tool.annotations = [];
        tool.commitTextDraft = LightboxAnnotationTool.prototype.commitTextDraft;
        tool.beginTextDraft = LightboxAnnotationTool.prototype.beginTextDraft;
        tool.cancelTextDraft = LightboxAnnotationTool.prototype.cancelTextDraft;
        tool.removeTextInput = LightboxAnnotationTool.prototype.removeTextInput;
        tool.createTextInput = () => ({ parentNode: null });
        tool.render = () => {};
        tool.beginTextDraft({ x: 0.3, y: 0.4 });
        tool.commitTextDraft('hello');
        assertTrue(tool.annotations[0].id, 'committed text missing id');
        """
    )


def test_select_hit_test() -> None:
    """Clicking an object selects it; clicking empty space clears selection."""
    _run_js(
        """
        const rect = ensureAnnotationId({ type: 'rect', x: 0.1, y: 0.1, width: 0.2, height: 0.2, color: '#f00' });
        const arrow = ensureAnnotationId({ type: 'arrow', startX: 0.6, startY: 0.6, endX: 0.9, endY: 0.9, color: '#0f0' });
        const pen = ensureAnnotationId({ type: 'pen', points: [{ x: 0.05, y: 0.8 }, { x: 0.2, y: 0.85 }], color: '#00f' });
        const text = ensureAnnotationId({ type: 'text', x: 0.4, y: 0.05, text: 'Hi', fontSizeRel: 0.04, color: '#fff' });
        assertTrue(hitTestAnnotation(rect, 0.2, 0.2), 'rect interior miss');
        assertTrue(!hitTestAnnotation(rect, 0.9, 0.9), 'rect exterior hit');
        assertTrue(hitTestAnnotation(arrow, 0.75, 0.75), 'arrow shaft miss');
        assertTrue(hitTestAnnotation(pen, 0.12, 0.825), 'pen stroke miss');
        assertTrue(hitTestAnnotation(text, 0.42, 0.07), 'text box miss');
        const list = [rect, arrow, pen, text];
        assertTrue(hitTestTopAnnotation(list, 0.2, 0.2).id === rect.id, 'top hit should be rect');
        const tool = makeTool();
        tool.annotations = list;
        tool.render = () => {};
        tool.updatePropertyControls = () => {};
        const picked = LightboxAnnotationTool.prototype.selectAt.call(tool, 0.75, 0.75);
        assertTrue(picked && picked.id === arrow.id, 'selectAt missed arrow: ' + (picked && picked.id));
        assertTrue(tool.selectedId === arrow.id, 'selectedId not set');
        const none = LightboxAnnotationTool.prototype.selectAt.call(tool, 0.5, 0.5);
        assertTrue(none === null, 'empty click should deselect');
        assertTrue(tool.selectedId === null, 'selectedId not cleared on empty click');
        """
    )


def test_move_resize() -> None:
    """Drag translates; handles resize rect/arrow; text fontSizeRel changes."""
    _run_js(
        """
        const rect = ensureAnnotationId({ type: 'rect', x: 0.2, y: 0.2, width: 0.2, height: 0.1 });
        moveAnnotationBy(rect, 0.1, -0.05);
        assertTrue(Math.abs(rect.x - 0.3) < 1e-9 && Math.abs(rect.y - 0.15) < 1e-9, 'rect move failed');
        resizeAnnotationHandle(rect, 'se', 0.6, 0.4);
        assertTrue(Math.abs(rect.width - 0.3) < 1e-9 && Math.abs(rect.height - 0.25) < 1e-9, 'rect se resize failed');
        const arrow = ensureAnnotationId({ type: 'arrow', startX: 0.1, startY: 0.1, endX: 0.4, endY: 0.2 });
        resizeAnnotationHandle(arrow, 'end', 0.5, 0.3);
        assertTrue(Math.abs(arrow.endX - 0.5) < 1e-9 && Math.abs(arrow.endY - 0.3) < 1e-9, 'arrow end resize failed');
        const text = ensureAnnotationId({ type: 'text', x: 0.1, y: 0.1, text: 'A', fontSizeRel: 0.036 });
        resizeAnnotationHandle(text, 'size', 0.2, 0.16);
        assertTrue(Math.abs(text.fontSizeRel - 0.06) < 1e-9, 'text fontSizeRel resize failed: ' + text.fontSizeRel);
        const pen = ensureAnnotationId({ type: 'pen', points: [{ x: 0.1, y: 0.1 }, { x: 0.2, y: 0.2 }] });
        moveAnnotationBy(pen, 0.05, 0.05);
        assertTrue(Math.abs(pen.points[0].x - 0.15) < 1e-9 && Math.abs(pen.points[1].y - 0.25) < 1e-9, 'pen move failed');
        const tool = makeTool();
        tool.annotations = [rect];
        tool.selectedId = rect.id;
        tool.render = () => {};
        LightboxAnnotationTool.prototype.moveSelected.call(tool, -0.1, 0);
        assertTrue(Math.abs(rect.x - 0.2) < 1e-9, 'moveSelected failed');
        """
    )


def test_delete_and_properties() -> None:
    """Delete/Backspace drops the selection; toolbar properties mutate it."""
    _run_js(
        """
        const a = ensureAnnotationId({ type: 'rect', x: 0.1, y: 0.1, width: 0.2, height: 0.2, color: '#ffffff', strokeWidthRel: 0.008 });
        const b = ensureAnnotationId({ type: 'text', x: 0.5, y: 0.5, text: 'x', color: '#ffffff', fontSizeRel: 0.036 });
        const tool = makeTool();
        tool.annotations = [a, b];
        tool.selectedId = a.id;
        tool.render = () => {};
        tool.updatePropertyControls = () => {};
        tool.saveAnnotations = () => {};
        LightboxAnnotationTool.prototype.applyPropertiesToSelected.call(tool, { color: '#bc1515', strokeWidthRel: 0.02 });
        assertTrue(a.color === '#bc1515' && Math.abs(a.strokeWidthRel - 0.02) < 1e-9, 'rect properties not applied');
        tool.selectedId = b.id;
        LightboxAnnotationTool.prototype.applyPropertiesToSelected.call(tool, { color: '#22cc44', fontSizeRel: 0.08 });
        assertTrue(b.color === '#22cc44' && Math.abs(b.fontSizeRel - 0.08) < 1e-9, 'text properties not applied');
        tool.selectedId = a.id;
        const removed = LightboxAnnotationTool.prototype.deleteSelected.call(tool);
        assertTrue(removed === true, 'deleteSelected returned false');
        assertTrue(tool.annotations.length === 1 && tool.annotations[0].id === b.id, 'wrong annotation deleted');
        assertTrue(tool.selectedId === null, 'selection not cleared after delete');
        let prevented = false;
        LightboxAnnotationTool.prototype.onKeyDown.call(tool, {
            key: 'Backspace', target: { tagName: 'DIV' }, preventDefault() {{ prevented = true; }}
        });
        tool.selectedId = b.id;
        LightboxAnnotationTool.prototype.onKeyDown.call(tool, {
            key: 'Delete', target: { tagName: 'DIV' }, preventDefault() {{ prevented = true; }}
        });
        assertTrue(tool.annotations.length === 0, 'Delete key did not remove the selected annotation');
        assertTrue(prevented, 'Delete key should preventDefault so the browser does not go back');
        """
    )


def test_overlay_tracks_image_rect_after_scroll() -> None:
    """Founder offset: overlay left/top must follow a live image box, not a pointerdown cache.

    getActualImageRect itself is live (it always reads getBoundingClientRect).
    The regression was syncOverlaySize / getPosPct not re-reading after scroll
    or lightbox layout change. getPosPct now calls syncOverlaySize first.
    """
    _run_js(
        """
        const box = { left: 120, top: 80, width: 200, height: 100, parentLeft: 100, parentTop: 40 };
        const img = {
            naturalWidth: 200, naturalHeight: 100, width: 200, height: 100,
            getBoundingClientRect() {
                return {
                    left: box.left, top: box.top, width: box.width, height: box.height,
                    right: box.left + box.width, bottom: box.top + box.height
                };
            },
            parentElement: {
                getBoundingClientRect() {
                    return {
                        left: box.parentLeft, top: box.parentTop, width: 240, height: 160,
                        right: box.parentLeft + 240, bottom: box.parentTop + 160
                    };
                },
                addEventListener() {},
            }
        };
        const first = getActualImageRect(img);
        assertTrue(Math.abs(first.left - 120) < 1e-9 && Math.abs(first.top - 80) < 1e-9, 'live rect 1 failed');
        const tool = makeTool();
        tool.img = img;
        LightboxAnnotationTool.prototype.syncOverlaySize.call(tool);
        assertTrue(Number.parseFloat(tool.svg.style.top) === 40, 'initial overlay top: ' + tool.svg.style.top);
        assertTrue(Number.parseFloat(tool.svg.style.left) === 20, 'initial overlay left: ' + tool.svg.style.left);
        // Image scrolled inside a positioned parent whose viewport box stays put:
        // relative offset must change (this is the Founder drift).
        box.left = 120; box.top = 20;
        LightboxAnnotationTool.prototype.getPosPct.call(tool, { clientX: 220, clientY: 70 });
        assertTrue(Number.parseFloat(tool.svg.style.top) === -20,
            'overlay top did not refresh after inner scroll: ' + tool.svg.style.top);
        const layout = overlayLayoutFromImage(img);
        assertTrue(Math.abs(layout.offsetY - (-20)) < 1e-9, 'overlayLayoutFromImage offsetY stale: ' + layout.offsetY);
        """
    )
