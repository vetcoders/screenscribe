"""Annotation text render-size and contrast regression tests.

The lightbox annotation editor rendered Text-tool annotations as giant dark
glyphs because the same ``createAnnotationElement`` factory consumed a pixel
font size (``fontSizePx``) inside a normalized 0-1 SVG viewBox. The text also
appeared dark-on-dark because the shared ``.annotation-shape`` CSS rule set
``fill: none`` on text, removing the light default fill and leaving only the
dark halo.

These tests execute the relevant functions from ``review_app.js`` in a node
``vm`` sandbox and assert the unit-space contract directly.
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
THEME_CSS = ASSETS / "styles/screenscribe-theme.css"

SVG_NS = "http://www.w3.org/2000/svg"


def _read_annotation_default_color() -> str:
    """Read the literal hex value of the theme annotation default token."""
    theme = THEME_CSS.read_text(encoding="utf-8")
    match = re.search(r"--annotation-color-default:\s*(#[0-9a-fA-F]{3,8})", theme)
    if match:
        return match.group(1).lower()
    pytest.fail("--annotation-color-default token not found in theme CSS")


def _hex_to_luminance(hex_color: str) -> float:
    """Relative luminance of an sRGB hex colour (WCAG 2.1)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    r, g, b = (int(hex_color[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    r = r / 12.92 if r <= 0.03928 else ((r + 0.055) / 1.055) ** 2.4
    g = g / 12.92 if g <= 0.03928 else ((g + 0.055) / 1.055) ** 2.4
    b = b / 12.92 if b <= 0.03928 else ((b + 0.055) / 1.055) ** 2.4
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _run_js(assertions: str) -> None:
    """Execute review_app.js in node, then run assertions in the SAME vm scope."""
    node = shutil.which("node")
    if not node:
        pytest.fail("node is required for annotation text tests (fail-closed)")

    default_color = _read_annotation_default_color()

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
                    if (i >= 0) {{ this.children.splice(i, 1); c._parent = null; }}
                    return c;
                }},
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
            removeEventListener() {{}},
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
            const THEME_DEFAULT = '{default_color}';
            function assertTrue(cond, msg) {{ if (!cond) throw new Error(msg); }}
            function assertNear(actual, expected, eps, msg) {{
                if (Math.abs(actual - expected) > eps) throw new Error(msg + ' actual=' + actual + ' expected=' + expected);
            }}
            function classListToArray(el) {{
                const out = [];
                for (const c of ['annotation-shape', 'annotation-text', 'annotation-selected']) {{
                    if (el.classList.contains(c)) out.push(c);
                }}
                return out;
            }}
            {assertions}
        `;

        const script = new vm.Script(sources + "\\n" + driver, {{ filename: 'review_app.js' }});
        script.runInNewContext(sandbox);
        """
    )
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_default_text_font_size_is_height_relative() -> None:
    """A default text annotation on a 2600x1654 frame is ~3.6% of frame height."""
    _run_js(
        """
        const ann = { type: 'text', text: 'TUTAJ JEST BUG', x: 0.1, y: 0.1 };
        const px = annotationFontSizePx(ann, 2600, 1654);
        const relToHeight = px / 1654;
        assertTrue(relToHeight >= 0.025 && relToHeight <= 0.06,
            'default text size outside [2.5%, 6%] of frame height: ' + relToHeight);
        assertNear(px, 0.036 * 1654, 1e-9, 'annotationFontSizePx should scale by frame height');
        """
    )


def test_live_render_ignores_pixel_font_size() -> None:
    """Normalized viewBox rendering must use fontSizeRel even if fontSizePx is present.

    This is the regression guard for the giant-text bug: a denormalized (pixel)
    annotation accidentally rendered in the live 0-1 viewBox would previously use
    its pixel size as a fraction of the viewBox, producing glyphs many times the
    frame height.
    """
    _run_js(
        """
        const ann = { type: 'text', text: 'x', x: 0.1, y: 0.1, fontSizeRel: 0.036, fontSizePx: 93.6 };
        const el = createAnnotationElement(ann, 1, 1);
        assertTrue(el && el.getAttribute('font-size') === '0.036',
            'live render used fontSizePx instead of fontSizeRel: ' + (el && el.getAttribute('font-size')));
        const heightFrac = Number(el.getAttribute('font-size'));
        assertTrue(heightFrac >= 0.025 && heightFrac <= 0.06,
            'live font-size not within target relative range: ' + heightFrac);
        """
    )


def test_export_render_uses_pixel_font_size() -> None:
    """Pixel-coordinate viewBox rendering consumes the pre-computed fontSizePx."""
    _run_js(
        """
        const ann = denormalizeAnnotations([{ type: 'text', text: 'x', x: 0.1, y: 0.1, fontSizeRel: 0.036 }], 2600, 1654)[0];
        const el = createAnnotationElement(ann, 2600, 1654);
        const px = Number(el.getAttribute('font-size'));
        assertTrue(px > 0, 'export font-size missing');
        const relToHeight = px / 1654;
        assertTrue(relToHeight >= 0.025 && relToHeight <= 0.06,
            'export font-size outside [2.5%, 6%] of frame height: ' + relToHeight);
        assertNear(px, annotationFontSizePx({ fontSizeRel: 0.036 }, 2600, 1654), 1e-9,
            'export font-size does not match annotationFontSizePx');
        """
    )


def test_font_size_slider_clamps_to_bounds() -> None:
    """Toolbar / property changes clamp text fontSizeRel to the allowed range."""
    _run_js(
        """
        const tiny = applyAnnotationProperties({ type: 'text', fontSizeRel: 0.036 }, { fontSizeRel: 0.001 });
        assertNear(tiny.fontSizeRel, 0.012, 1e-9, 'fontSizeRel not clamped at lower bound: ' + tiny.fontSizeRel);
        const huge = applyAnnotationProperties({ type: 'text', fontSizeRel: 0.036 }, { fontSizeRel: 0.5 });
        assertNear(huge.fontSizeRel, 0.2, 1e-9, 'fontSizeRel not clamped at upper bound: ' + huge.fontSizeRel);
        const ok = applyAnnotationProperties({ type: 'text', fontSizeRel: 0.036 }, { fontSizeRel: 0.08 });
        assertNear(ok.fontSizeRel, 0.08, 1e-9, 'in-range fontSizeRel was mutated: ' + ok.fontSizeRel);
        """
    )


def test_default_text_fill_and_halo_are_readable_on_dark_theme() -> None:
    """Text carries the light theme default fill and a dark halo for contrast."""
    _run_js(
        """
        const ann = { type: 'text', text: 'x', x: 0.1, y: 0.1 };
        const el = createAnnotationElement(ann, 1, 1);
        const fill = el.getAttribute('fill');
        const stroke = el.getAttribute('stroke');
        assertTrue(fill === THEME_DEFAULT,
            'default text fill does not match theme token: ' + fill + ' vs ' + THEME_DEFAULT);
        assertTrue(stroke && stroke.startsWith('rgba(0, 0, 0'),
            'text halo is not a dark rgba: ' + stroke);
        assertTrue(el.getAttribute('paint-order') === 'stroke',
            'halo paint-order missing');
        """
    )


def test_text_element_drops_annotation_shape_class() -> None:
    """Text must not carry the shape CSS class that forces fill: none on it."""
    _run_js(
        """
        const ann = { type: 'text', text: 'x', x: 0.1, y: 0.1 };
        const el = createAnnotationElement(ann, 1, 1);
        assertTrue(!el.classList.contains('annotation-shape'),
            'text still carries annotation-shape; CSS fill:none would hide it');
        assertTrue(el.classList.contains('annotation-text'),
            'text missing annotation-text class');
        """
    )
