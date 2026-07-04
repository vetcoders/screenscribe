"""Manual frames in the agent_manifest.json deliverable (cut H3).

Cross-artifact gap: an operator's hand-captured frames (manual frames) live in
the report JSON and in the TODO ("Ręczne przechwycenia" / manual-capture
section), but they were absent from ``agent_manifest.json`` -- the structured
handoff a coding agent actually reads. The agent therefore had no idea the
operator manually captured anything. These tests genuinely execute
``exportReviewedZIP`` from ``review_app.js`` inside a node sandbox (same approach
as ``test_review_app_deliverable_annotations.py``) and assert the real JSON the
manifest emits.

Scope is the MANIFEST manual-frame layer only: the report-JSON manual_frames
output (already shipped), TODO rendering (H2), and annotation legibility (H1)
are exercised elsewhere. Here we prove the manifest *carries* the manual frames
with agent-readable metadata + annotations (incl. verbatim emoji), and that an
empty capture set yields an empty list, never garbage.
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

# An emoji in the manual transcript AND in a text annotation proves both survive
# verbatim all the way into the manifest (no escaping / mojibake / drop).
_EMOJI = "\U0001f389"  # 🎉

# One AI-analyzed manual frame with a single text annotation (review-7 had ann=1).
_MANUAL_FRAMES = [
    {
        "marker_id": "m1",
        "timestamp": 1.0,
        "timestamp_formatted": "00:01",
        "transcript": "operator says look here " + _EMOJI,
        "notes": "manual note",
        "frameDataUrl": "data:image/png;base64,QUJD",
        "result": {
            "summary": "Operator-flagged glitch",
            "severity": "high",
            "issues_detected": ["layout breaks on resize"],
        },
    }
]

# Annotation on the manual frame: a text annotation carrying an emoji.
_MANUAL_ANNOTATIONS = [
    {"type": "arrow", "color": "#ff0000"},
    {"type": "text", "text": "see this " + _EMOJI, "color": "#000000"},
]

# No AI findings: the manifest under test is about the manual layer only.
_FINDINGS: list[dict] = []


def _sandbox_prelude(findings: list[dict]) -> str:
    """The shared node/vm document + sandbox stubs (mirrors the H2 test)."""
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
            reportState.findings = {{}};
            reportState.merges = [];
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


# Manual frame WITH a text-emoji annotation, keyed by the manual finding id.
_REVIEW_WITH_MANUAL_FRAME = (
    f"reportState.manualFrames = {json.dumps(_MANUAL_FRAMES)};"
    " reportState.findings = { 'manual-m1': { verdict: 'accepted', severity: 'high', notes: '', "
    f"annotations: {json.dumps(_MANUAL_ANNOTATIONS)} }} }};"
)


def test_manifest_includes_manual_frames_with_metadata() -> None:
    """agent_manifest.manual_frames carries the operator's frame with the
    agent-readable metadata: timestamp, transcript (emoji verbatim), AI summary."""
    _run_manifest(
        _REVIEW_WITH_MANUAL_FRAME,
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        if (!Array.isArray(manifest.manual_frames))
            throw new Error('manifest has no manual_frames array: ' + files['agent_manifest.json'].data);
        if (manifest.manual_frames.length !== 1)
            throw new Error('expected exactly 1 manual frame: ' + JSON.stringify(manifest.manual_frames));
        const mf = manifest.manual_frames[0];
        if (mf.timestamp_formatted !== '00:01')
            throw new Error('manual frame missing timestamp_formatted: ' + JSON.stringify(mf));
        if (!mf.transcript.includes('\\u{1F389}'))
            throw new Error('manual frame transcript lost emoji verbatim: ' + JSON.stringify(mf));
        if (mf.summary !== 'Operator-flagged glitch')
            throw new Error('manual frame missing AI summary: ' + JSON.stringify(mf));
        if (mf.status !== 'ai-analyzed')
            throw new Error('analyzed manual frame should report ai-analyzed status: ' + JSON.stringify(mf));
        if (mf.screenshot !== 'manual_frames/demo_manual_00-01.png')
            throw new Error('manual frame missing relative screenshot path: ' + JSON.stringify(mf));
        if (manifest.meta.total_manual_frames !== 1)
            throw new Error('meta.total_manual_frames not counted: ' + JSON.stringify(manifest.meta));
        """,
    )


def test_manifest_manual_frame_carries_annotations_and_description() -> None:
    """A manually-captured frame with annotations exposes them as a structured
    list (type + verbatim text) AND a human-readable description."""
    _run_manifest(
        _REVIEW_WITH_MANUAL_FRAME,
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const mf = manifest.manual_frames[0];
        if (!Array.isArray(mf.annotations) || mf.annotations.length === 0)
            throw new Error('manual frame carries no structured annotations: ' + JSON.stringify(mf));
        const types = mf.annotations.map((a) => a.type);
        for (const want of ['arrow', 'text']) {
            if (!types.includes(want))
                throw new Error('manual frame annotations missing type ' + want + ': ' + JSON.stringify(mf.annotations));
        }
        const textAnn = mf.annotations.find((a) => a.type === 'text');
        if (!textAnn || !textAnn.text.includes('\\u{1F389}'))
            throw new Error('manual frame text annotation lost emoji content: ' + JSON.stringify(textAnn));
        if (typeof mf.annotations_description !== 'string' || mf.annotations_description.length === 0)
            throw new Error('manual frame missing annotation description: ' + JSON.stringify(mf));
        if (!mf.annotations_description.includes('\\u{1F389}'))
            throw new Error('manual frame annotation description lost emoji: ' + JSON.stringify(mf));
        """,
    )


def test_manifest_manual_frames_empty_when_none() -> None:
    """Negative: no manual captures => an empty manual_frames list, never crash
    or garbage, and the meta counter stays at zero."""
    _run_manifest(
        "reportState.manualFrames = []; reportState.findings = {}; reportState.merges = [];",
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        if (!Array.isArray(manifest.manual_frames))
            throw new Error('manual_frames is not an array when empty: ' + JSON.stringify(manifest));
        if (manifest.manual_frames.length !== 0)
            throw new Error('manual_frames should be empty with no captures: ' + JSON.stringify(manifest.manual_frames));
        if (manifest.meta.total_manual_frames !== 0)
            throw new Error('meta.total_manual_frames should be 0: ' + JSON.stringify(manifest.meta));
        """,
    )


def test_manifest_manual_frame_not_analyzed_status() -> None:
    """A manual frame captured WITHOUT VLM analysis (no result) reports an
    explicit 'not AI-analyzed yet' status and a null summary."""
    raw_frame = [
        {
            "marker_id": "m9",
            "timestamp": 5.0,
            "timestamp_formatted": "00:05",
            "transcript": "raw capture, no analysis",
            "notes": "",
            "frameDataUrl": "data:image/png;base64,QUJD",
        }
    ]
    _run_manifest(
        f"reportState.manualFrames = {json.dumps(raw_frame)};"
        " reportState.findings = {}; reportState.merges = [];",
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const mf = manifest.manual_frames[0];
        if (mf.status !== 'not AI-analyzed yet')
            throw new Error('un-analyzed manual frame should report not-analyzed status: ' + JSON.stringify(mf));
        if (mf.summary !== null)
            throw new Error('un-analyzed manual frame should have null summary: ' + JSON.stringify(mf));
        if (!Array.isArray(mf.annotations) || mf.annotations.length !== 0)
            throw new Error('un-annotated manual frame should expose empty annotations list: ' + JSON.stringify(mf));
        """,
    )
