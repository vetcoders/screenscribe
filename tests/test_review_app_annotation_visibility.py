"""Annotation legibility contract for the HTML Pro review app.

The reported bug: manually added annotation text was invisible on light
backgrounds ("thin black") because the contrast halo lived ONLY in the report
CSS. The annotated PNG export serializes the SVG through XMLSerializer -> blob
-> <img> -> canvas, and an external stylesheet does not travel with a
rasterized blob, so the halo was lost and only the light-grey ``#c8ccd2`` fill
survived.

These tests genuinely execute ``serializeAnnotationsToSvg`` /
``denormalizeAnnotations`` from ``review_app.js`` inside a node sandbox with a
minimal DOM + XMLSerializer, then assert on the SERIALIZED string. Because the
halo must survive serialization, it has to be present as SVG presentation
*attributes* (set via ``setAttribute``), never as a CSS class — class names go
to ``classList`` and are intentionally NOT emitted by the fake serializer, so a
regression that pushes the halo back into CSS-only territory fails here.
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

THEME_CSS = ASSETS / "styles/screenscribe-theme.css"
REPORT_CSS = ASSETS / "styles/report-pro.css"
LIGHTBOX_HTML = ASSETS / "templates/partials/lightbox.html"

LEGACY_DEFAULT = "#c8ccd2"


def _run_svg(assertions: str) -> None:
    """Serialize annotations through review_app.js in node, then assert in JS."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js annotation tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        // Minimal DOM node: records setAttribute (presentation attributes) and
        // keeps classList separate so the fake serializer can prove the halo is
        // an attribute, not a CSS class.
        function makeEl(tag) {{
            return {{
                tagName: tag, attributes: {{}}, children: [], textContent: '', _parent: null,
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                setAttribute(k, v) {{ this.attributes[k] = String(v); }},
                getAttribute(k) {{ return this.attributes[k]; }},
                appendChild(c) {{ c._parent = this; this.children.push(c); return c; }},
                removeChild(c) {{ const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }},
                remove() {{ if (this._parent) this._parent.removeChild(this); }},
                get firstChild() {{ return this.children[0] || null; }},
            }};
        }}

        function serializeEl(el) {{
            const attrs = Object.entries(el.attributes)
                .map(([k, v]) => ` ${{k}}="${{v}}"`).join('');
            const text = el.children.length === 0 ? (el.textContent || '') : '';
            const kids = el.children.map(serializeEl).join('');
            return `<${{el.tagName}}${{attrs}}>${{text}}${{kids}}</${{el.tagName}}>`;
        }}

        const documentStub = {{
            documentElement: {{}},
            body: {{ dataset: {{}}, classList: {{ add() {{}}, remove() {{}} }},
                     contains() {{ return true; }}, appendChild() {{}}, removeChild() {{}} }},
            addEventListener() {{}},
            querySelector() {{ return null; }},
            querySelectorAll() {{ return []; }},
            getElementById() {{ return null; }},
            createElementNS(_ns, tag) {{ return makeEl(tag); }},
            createElement(tag) {{ return makeEl(tag); }},
        }};

        // Force the JS *fallback* default-colour path (no theme stylesheet in
        // node): getComputedStyle returns empty so readAnnotationDefaultColor()
        // falls back to the literal baked into review_app.js.
        function getComputedStyle() {{ return {{ getPropertyValue() {{ return ''; }} }}; }}
        class XMLSerializer {{ serializeToString(el) {{ return serializeEl(el); }} }}

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

        // Helpers run INSIDE the vm context, so they live in the driver string.
        const driver = `
            function assertTrue(cond, msg) {{ if (!cond) throw new Error(msg); }}
            function count(hay, needle) {{ return hay.split(needle).length - 1; }}
            {assertions}
        `;

        const script = new vm.Script(sources + "\\n" + driver, {{ filename: 'review_app.js' }});
        script.runInNewContext(sandbox);
        """
    )
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_text_halo_is_baked_into_svg_attributes() -> None:
    """An annotation text element must carry the contrast halo as SVG
    presentation attributes (paint-order + dark stroke) so it survives PNG
    serialization, with a light/contrastive fill that is not the legacy grey."""
    _run_svg(
        """
        const anns = [{ type: 'text', text: 'hello', x: 0.1, y: 0.1, fontSizeRel: 0.036 }];
        const px = denormalizeAnnotations(anns, 1920, 1080);
        const svg = serializeAnnotationsToSvg(px, 1920, 1080);

        assertTrue(svg.includes('<text'), 'no <text> element serialized: ' + svg);
        assertTrue(svg.includes('paint-order="stroke"'),
            'text halo paint-order missing from attributes (CSS-only regression?): ' + svg);
        assertTrue(svg.includes('stroke="rgba(0, 0, 0'),
            'text dark halo stroke missing from attributes: ' + svg);
        assertTrue(svg.includes('stroke-width="'),
            'text halo stroke-width missing: ' + svg);
        // Default fill must be light/contrastive, NOT the legacy invisible grey.
        assertTrue(!svg.includes('#c8ccd2'), 'legacy default colour #c8ccd2 still emitted: ' + svg);
        assertTrue(svg.includes('fill="#ffffff"'), 'expected white default fill: ' + svg);
        // Emoji content is preserved verbatim (stroke must not strip it).
        const emoji = [{ type: 'text', text: 'done \\u{1F389}', x: 0.1, y: 0.1, fontSizeRel: 0.036 }];
        const esvg = serializeAnnotationsToSvg(denormalizeAnnotations(emoji, 1920, 1080), 1920, 1080);
        assertTrue(esvg.includes('\\u{1F389}'), 'emoji glyph dropped from serialized text: ' + esvg);
        """
    )


def test_shapes_get_contrast_under_stroke() -> None:
    """rect and arrow must render a darker, wider under-stroke beneath the
    coloured stroke so thin light shapes stay legible on any background."""
    _run_svg(
        """
        const rect = denormalizeAnnotations([{ type: 'rect', x: 0.1, y: 0.1, width: 0.2, height: 0.2 }], 1920, 1080);
        const rsvg = serializeAnnotationsToSvg(rect, 1920, 1080);
        assertTrue(count(rsvg, '<rect') >= 2, 'rect missing under-stroke companion: ' + rsvg);
        assertTrue(rsvg.includes('stroke="rgba(0, 0, 0'), 'rect under-stroke halo colour missing: ' + rsvg);

        const arrow = denormalizeAnnotations([{ type: 'arrow', startX: 0.1, startY: 0.1, endX: 0.5, endY: 0.5 }], 1920, 1080);
        const asvg = serializeAnnotationsToSvg(arrow, 1920, 1080);
        assertTrue(asvg.includes('stroke="rgba(0, 0, 0'), 'arrow under-stroke halo colour missing: ' + asvg);
        // Two strokes per limb: dark wide under + coloured main (line + head -> >=4).
        assertTrue(count(asvg, '<line') + count(asvg, '<path') >= 4, 'arrow under-stroke limbs missing: ' + asvg);
        """
    )


def test_default_colour_is_not_legacy_grey_across_surfaces() -> None:
    """The default annotation colour must be synced away from the invisible
    legacy grey in all three sources: theme token, JS fallback, picker value."""
    theme = THEME_CSS.read_text(encoding="utf-8")
    assert "--annotation-color-default" in theme
    assert f"--annotation-color-default: {LEGACY_DEFAULT}" not in theme, (
        "theme token still legacy grey"
    )

    js = REVIEW_APP_JS.read_text(encoding="utf-8")
    assert f"'{LEGACY_DEFAULT}'" not in js and f'"{LEGACY_DEFAULT}"' not in js, (
        "review_app.js fallback still legacy grey"
    )

    lightbox = LIGHTBOX_HTML.read_text(encoding="utf-8")
    assert f'value="{LEGACY_DEFAULT}"' not in lightbox, "lightbox picker still legacy grey"


def test_css_halo_rule_removed_to_avoid_double_outline() -> None:
    """The CSS-only text halo rule must be gone: it does not survive export and,
    once the halo is an SVG attribute, it would double the outline / re-split
    UI vs export."""
    css = REPORT_CSS.read_text(encoding="utf-8")
    assert ".annotation-shape.annotation-text" not in css, (
        "CSS-only annotation-text halo rule still present (re-splits UI vs export)"
    )
