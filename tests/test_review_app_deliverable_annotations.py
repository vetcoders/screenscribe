"""Annotation + merge visibility in the review deliverables (cut H2).

Cross-artifact gap: annotations and human-merges live in the data and the
rendered images, but the two *text* deliverables a human / coding agent actually
reads -- the downloadable ``TODO_*.md`` and ``agent_manifest.json`` -- never said
a finding carries an annotation or that it stands in for a merge. These tests
genuinely execute ``buildTodoMarkdown`` and ``exportReviewedZIP`` from
``review_app.js`` inside a node sandbox (same approach as
``test_review_app_zip_export.py``) and assert the real text/JSON the functions
emit, so the description layer cannot silently regress.

Scope is the DESCRIPTION layer only: annotation rendering (H1) and the merge
fold logic (G6) are exercised elsewhere; here we prove the deliverables *talk
about* what those layers produced -- including verbatim emoji in text
annotations.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
I18N_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/i18n.js"
LANGUAGE_CONTROL_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/lib/language-control.js"
STT_TRANSPORT_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/lib/stt-transport.js"
TAB_KEYBOARD_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/lib/tab-keyboard.js"
REVIEW_APP_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/review_app.js"

# A text annotation carrying an emoji proves the description survives verbatim
# all the way into the deliverables (no escaping / mojibake / drop).
_EMOJI_TEXT = "text\U0001f389"  # "text🎉"

# Three findings: a + b are human-merged (a is base by timestamp), c standalone.
_FINDINGS = [
    {
        "id": "a",
        "timestamp": 1.0,
        "timestamp_formatted": "00:01",
        "category": "ui",
        "text": "the save button does nothing",
        "context": "settings screen",
        "screenshot": "data:image/jpeg;base64,QUJD",
        "unified_analysis": {
            "summary": "Save button unresponsive",
            "severity": "high",
            "action_items": ["Fix the click handler"],
            "affected_components": ["SettingsForm"],
        },
    },
    {
        "id": "b",
        "timestamp": 2.0,
        "timestamp_formatted": "00:02",
        "category": "ui",
        "text": "save still broken after reload",
        "screenshot": "data:image/jpeg;base64,REVG",
        "unified_analysis": {
            "summary": "Save button still does nothing after reload",
            "severity": "low",
            "action_items": ["Add a regression test"],
            "affected_components": ["SettingsForm", "Persistence"],
        },
    },
    {
        "id": "c",
        "timestamp": 3.0,
        "timestamp_formatted": "00:03",
        "category": "perf",
        "text": "the page is slow",
        "screenshot": "data:image/jpeg;base64,R0hJ",
        "unified_analysis": {
            "summary": "Slow render",
            "severity": "medium",
            "action_items": ["Profile the render"],
            "affected_components": ["Renderer"],
        },
    },
]

# Annotations on finding a: 2x arrow + rect + pen + a text annotation with emoji.
_ANNOTATIONS_A = [
    {"type": "arrow", "color": "#ff0000"},
    {"type": "arrow", "color": "#ff0000"},
    {"type": "rect", "color": "#00ff00"},
    {"type": "pen", "color": "#0000ff", "points": [{"x": 0.1, "y": 0.1}]},
    {"type": "text", "text": _EMOJI_TEXT, "color": "#000000"},
]


def _sandbox_prelude(findings: list[dict]) -> str:
    """The shared node/vm document + sandbox stubs used by both deliverables."""
    return textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findings = {json.dumps(findings)};
        const findingsEl = {{ textContent: JSON.stringify(findings) }};

        function makeEl() {{
            return {{
                className: '', textContent: '', hidden: false, href: '', download: '',
                style: {{}}, dataset: {{}},
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}}, contains() {{ return false; }} }},
                appendChild() {{}}, removeChild() {{}}, remove() {{}}, click() {{}},
                querySelector() {{ return null; }}, querySelectorAll() {{ return []; }},
                addEventListener() {{}}, setAttribute() {{}}, insertBefore() {{}},
            }};
        }}

        const documentStub = {{
            body: {{
                dataset: {{ videoName: 'demo.mp4', reportLanguage: 'en' }},
                classList: {{ add() {{}}, remove() {{}} }},
                contains() {{ return true; }},
                appendChild() {{}}, removeChild() {{}},
            }},
            documentElement: {{ lang: 'en' }},
            addEventListener() {{}},
            querySelector() {{ return null; }},
            querySelectorAll() {{ return []; }},
            getElementById(id) {{ return id === 'original-findings' ? findingsEl : null; }},
            createElement() {{ return makeEl(); }},
        }};

        const URLStub = {{ createObjectURL() {{ return 'blob:x'; }}, revokeObjectURL() {{}} }};

        class FakeFolder {{
            constructor(store, prefix) {{ this._store = store; this._prefix = prefix; }}
            file(name, data, opts) {{ this._store[this._prefix + name] = {{ data, opts: opts || null }}; }}
        }}
        class JSZip {{
            constructor() {{ this.files = {{}}; }}
            folder(name) {{ return new FakeFolder(this.files, name + '/'); }}
            file(name, data) {{ this.files[name] = {{ data, opts: null }}; }}
            async generateAsync() {{ return {{}}; }}
        }}

        const sandbox = {{
            console, setTimeout, clearTimeout, Math, Date, JSON, Promise,
            JSZip, URL: URLStub,
            window: {{
                location: {{ search: '' }},
                addEventListener() {{}}, removeEventListener() {{}},
                TRANSCRIPT_SEGMENTS: [],
            }},
            document: documentStub,
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            navigator: {{ mediaDevices: {{}} }},
            Blob: class Blob {{ constructor(c, o = {{}}) {{ this.chunks = c; this.type = o.type || ''; }} }},
            ResizeObserver: class ResizeObserver {{ observe() {{}} disconnect() {{}} }},
            Image: class Image {{}},
            process, confirm() {{ return true; }},
            fetch() {{ throw new Error('no network in test'); }},
        }};
        sandbox.window.document = sandbox.document;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.window.JSZip = JSZip;
        sandbox.globalThis = sandbox;

        const i18nSource = fs.readFileSync({str(I18N_JS)!r}, 'utf8');
        const languageControlSource = fs.readFileSync({str(LANGUAGE_CONTROL_JS)!r}, 'utf8');
        const sttTransportSource = fs.readFileSync({str(STT_TRANSPORT_JS)!r}, 'utf8');
        const tabKeyboardSource = fs.readFileSync({str(TAB_KEYBOARD_JS)!r}, 'utf8');
        const source = fs.readFileSync({str(REVIEW_APP_JS)!r}, 'utf8');
        """
    )


def _run_todo(review_setup: str, extra_assertions: str, findings: list[dict] | None = None) -> None:
    """Build the TODO markdown in node, then run JS assertions on the string."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js deliverable tests")
    findings = findings if findings is not None else _FINDINGS

    driver = textwrap.dedent(
        f"""
        const driver = `
            reportState.reviewer = 'qa';
            reportState.manualFrames = [];
            {review_setup}
            const originalFindings = JSON.parse(document.getElementById('original-findings').textContent);
            const md = buildTodoMarkdown(originalFindings, 'demo.mp4', 'qa');
            {extra_assertions}
        `;
        const script = new vm.Script(
            i18nSource + "\\n" + languageControlSource + "\\n" + sttTransportSource + "\\n" +
            tabKeyboardSource + "\\n" + source + "\\n" + driver,
            {{ filename: 'review_app.js' }}
        );
        script.runInNewContext(sandbox);
        """
    )
    runner = _sandbox_prelude(findings) + driver
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def _run_manifest(
    review_setup: str, extra_assertions: str, findings: list[dict] | None = None
) -> None:
    """Run exportReviewedZIP in node and assert on agent_manifest.json."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js deliverable tests")
    findings = findings if findings is not None else _FINDINGS

    driver = textwrap.dedent(
        f"""
        const driver = `
            reportState.reviewer = 'qa';
            reportState.manualFrames = [];
            {review_setup}
            globalThis.__zip = new JSZip();
            globalThis.JSZip = function() {{ return globalThis.__zip; }};
            exportReviewedZIP().then(() => {{
                const files = globalThis.__zip.files;
                {extra_assertions}
            }}).catch((e) => {{ console.error(e && e.stack ? e.stack : String(e)); process.exit(1); }});
        `;
        const script = new vm.Script(
            i18nSource + "\\n" + languageControlSource + "\\n" + sttTransportSource + "\\n" +
            tabKeyboardSource + "\\n" + source + "\\n" + driver,
            {{ filename: 'review_app.js' }}
        );
        script.runInNewContext(sandbox);
        """
    )
    runner = _sandbox_prelude(findings) + driver
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


# ---------------------------------------------------------------------------
# TODO markdown: annotation description
# ---------------------------------------------------------------------------

_REVIEW_WITH_ANNOTATIONS = (
    "reportState.findings = { a: { verdict: 'accepted', severity: 'high', notes: '', "
    f"annotations: {json.dumps(_ANNOTATIONS_A)} }} }};"
    " reportState.merges = [];"
)


def test_todo_describes_annotation_types_and_text() -> None:
    """A finding with annotations gets a readable line: counted types + text body."""
    _run_todo(
        _REVIEW_WITH_ANNOTATIONS,
        """
        if (!md.includes('Annotation'))
            throw new Error('TODO missing annotation label: ' + md);
        // 2x arrow must be counted, not listed twice.
        if (!md.includes('2'))
            throw new Error('TODO did not count repeated annotation type: ' + md);
        for (const word of ['arrow', 'rectangle', 'drawing', 'text']) {
            if (!md.toLowerCase().includes(word))
                throw new Error('TODO annotation desc missing type ' + word + ': ' + md);
        }
        """,
    )


def test_todo_preserves_annotation_emoji_verbatim() -> None:
    """Text-annotation content (incl. emoji) reaches the TODO unchanged."""
    _run_todo(
        _REVIEW_WITH_ANNOTATIONS,
        """
        if (!md.includes('text\\u{1F389}'))
            throw new Error('TODO lost the annotation emoji text verbatim: ' + md);
        """,
    )


def test_todo_no_annotation_line_when_none() -> None:
    """Negative: a finding without annotations gets NO annotation line (no junk)."""
    _run_todo(
        "reportState.findings = { a: { verdict: 'accepted', severity: 'high', notes: '', annotations: [] } };"
        " reportState.merges = [];",
        """
        if (md.includes('Annotation:'))
            throw new Error('TODO emitted an empty/garbage annotation line: ' + md);
        """,
    )


# ---------------------------------------------------------------------------
# TODO markdown: merge provenance
# ---------------------------------------------------------------------------

_REVIEW_WITH_MERGE = (
    "reportState.findings = {"
    " a: { verdict: 'accepted', severity: 'high', notes: '', annotations: [] },"
    " b: { verdict: 'none', severity: '', notes: '', annotations: [] },"
    " c: { verdict: 'accepted', severity: 'medium', notes: '', annotations: [] }"
    "};"
    " reportState.merges = [{ id: 'a', member_ids: ['a', 'b'], summary_override: null }];"
)


def test_todo_surfaces_merge_provenance() -> None:
    """A merged survivor's TODO item names the absorbed original ids."""
    _run_todo(
        _REVIEW_WITH_MERGE,
        """
        if (!md.includes('Merged from'))
            throw new Error('TODO missing merge provenance label: ' + md);
        if (!md.includes('#b'))
            throw new Error('TODO merge line did not name the absorbed id b: ' + md);
        """,
    )


def test_todo_no_merge_line_for_standalone_finding() -> None:
    """Negative: an unmerged finding gets NO 'Merged from' line."""
    _run_todo(
        "reportState.findings = { c: { verdict: 'accepted', severity: 'medium', notes: '', annotations: [] } };"
        " reportState.merges = [];",
        """
        if (md.includes('Merged from'))
            throw new Error('TODO emitted a merge line for a standalone finding: ' + md);
        """,
        findings=[_FINDINGS[2]],
    )


# ---------------------------------------------------------------------------
# agent_manifest.json: annotation + merge surfacing
# ---------------------------------------------------------------------------


def test_manifest_finding_carries_annotation_types_and_text() -> None:
    """agent_manifest finding lists its annotations (type + text), emoji intact."""
    _run_manifest(
        _REVIEW_WITH_ANNOTATIONS,
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const entry = manifest.findings.find((f) => Array.isArray(f.annotations) && f.annotations.length > 0);
        if (!entry) throw new Error('no manifest finding carries annotations: ' + files['agent_manifest.json'].data);
        const types = entry.annotations.map((a) => a.type);
        for (const want of ['arrow', 'rect', 'pen', 'text']) {
            if (!types.includes(want))
                throw new Error('manifest annotations missing type ' + want + ': ' + JSON.stringify(entry.annotations));
        }
        const textAnn = entry.annotations.find((a) => a.type === 'text');
        if (!textAnn || textAnn.text !== 'text\\u{1F389}')
            throw new Error('manifest text annotation lost emoji content: ' + JSON.stringify(textAnn));
        """,
    )


def test_manifest_finding_without_annotations_has_empty_list() -> None:
    """Negative: a finding without annotations exposes an empty annotations list,
    never stale/garbage entries."""
    _run_manifest(
        "reportState.findings = { c: { verdict: 'accepted', severity: 'medium', notes: '', annotations: [] } };"
        " reportState.merges = [];",
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const entry = manifest.findings[0];
        if (!Array.isArray(entry.annotations))
            throw new Error('manifest finding annotations is not an array: ' + JSON.stringify(entry));
        if (entry.annotations.length !== 0)
            throw new Error('manifest finding without annotations got non-empty list: ' + JSON.stringify(entry.annotations));
        """,
        findings=[_FINDINGS[2]],
    )


def test_manifest_finding_carries_annotation_description() -> None:
    """agent_manifest finding exposes a human-readable annotations_description
    (counted types + verbatim emoji text), matching the TODO description layer
    produced by the shared describeAnnotations helper."""
    _run_manifest(
        _REVIEW_WITH_ANNOTATIONS,
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const entry = manifest.findings.find((f) => Array.isArray(f.annotations) && f.annotations.length > 0);
        if (!entry) throw new Error('no manifest finding carries annotations: ' + files['agent_manifest.json'].data);
        if (typeof entry.annotations_description !== 'string' || entry.annotations_description.length === 0)
            throw new Error('manifest finding missing annotations_description: ' + JSON.stringify(entry));
        // 2x arrow must be counted, not listed twice -- proves the shared helper ran.
        if (!entry.annotations_description.includes('2'))
            throw new Error('manifest finding annotations_description did not count repeated type: ' + JSON.stringify(entry));
        if (!entry.annotations_description.includes('text\\u{1F389}'))
            throw new Error('manifest finding annotations_description lost the emoji text verbatim: ' + JSON.stringify(entry));
        """,
    )


def test_manifest_finding_description_empty_when_no_annotations() -> None:
    """Negative: a finding without annotations exposes an empty
    annotations_description (no stale/garbage text), consistent with the manual
    frame layer and the TODO."""
    _run_manifest(
        "reportState.findings = { c: { verdict: 'accepted', severity: 'medium', notes: '', annotations: [] } };"
        " reportState.merges = [];",
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const entry = manifest.findings[0];
        if (entry.annotations_description !== '')
            throw new Error('manifest finding without annotations got non-empty description: ' + JSON.stringify(entry));
        """,
        findings=[_FINDINGS[2]],
    )


def test_manifest_merge_is_explicit_with_count() -> None:
    """A merged manifest entry exposes merged_from_ids AND an explicit count."""
    _run_manifest(
        _REVIEW_WITH_MERGE,
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const merged = manifest.findings.find((f) => Array.isArray(f.merged_from_ids) && f.merged_from_ids.length > 0);
        if (!merged) throw new Error('no manifest entry carries merged_from_ids');
        if (!merged.merged_from_ids.includes('b'))
            throw new Error('manifest merge trail missing absorbed id b: ' + JSON.stringify(merged.merged_from_ids));
        if (merged.merged_from_count !== merged.merged_from_ids.length)
            throw new Error('manifest merged_from_count out of sync: ' + JSON.stringify({c: merged.merged_from_count, ids: merged.merged_from_ids}));
        """,
    )
