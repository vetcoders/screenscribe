"""Data-side merge fold for the HTML Pro review app (G6).

Regression cover for a bug hit on a live review: a human-merge that collapses
N findings into one was *view-only*. The flat data/export path folded findings
ONLY from the in-memory ``reportState.merges`` (fresh UI), comparing a ``Set`` of
member ids captured from the DOM (strings) against finding ids parsed from the
report JSON (integers). The cross-type ``Set.has`` silently no-opped, so:

  * absorbed members were never removed from the deliverable, and
  * surviving findings were emitted twice (once flat, once merged),

turning a reviewer's 10 -> 5 merge into a 13-finding export. On a disk reload the
merge state lives only as ``merged_from_ids`` (``reportState.merges`` is never
persisted), and that data-side trail was ignored entirely, so re-opening a saved
report and exporting folded nothing.

These tests genuinely execute ``buildReviewData`` and ``exportReviewedZIP`` from
``review_app.js`` in a node sandbox, using the EXACT production topology
(integer ids, three merge groups, 10 -> 5) so a regression cannot hide behind
string-id fixtures. The fold must hold across all three provenance shapes:

  1. fresh UI       -- merges live only in ``reportState.merges``;
  2. disk reload    -- merges live only in ``merged_from_ids`` on review state;
  3. mixed          -- some groups in each source (idempotent reconciliation).
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


def _finding(fid: int, ts: float, summary: str, category: str = "ui") -> dict:
    """A synthetic, neutral AI finding shaped like the real report JSON.

    ``id`` is an INTEGER on purpose: that is what ``JSON.parse`` yields in the
    browser and is the exact condition the string/number Set-membership bug
    needed to surface.
    """
    return {
        "id": fid,
        "timestamp": ts,
        "timestamp_formatted": f"{int(ts) // 60:02d}:{int(ts) % 60:02d}",
        "category": category,
        "text": f"transcript line for finding {fid}",
        "screenshot": "data:image/jpeg;base64,QUJD",
        "unified_analysis": {
            "summary": summary,
            "severity": "low",
            "action_items": [f"do thing {fid}"],
            "affected_components": [f"Component{fid}"],
            "issues_detected": [f"issue {fid}"],
        },
    }


# Production topology from the live review: 10 findings, 3 merge groups, 10 -> 5.
#   survivor 17    <- members [18, 26, 27]
#   survivor 1000004 <- members [21]
#   survivor 22    <- members [29]
# Surviving set after fold: {6, 11, 17, 1000004, 22}.
_FINDINGS = [
    _finding(6, 6.0, "standalone finding six"),
    _finding(11, 11.0, "standalone finding eleven"),
    _finding(17, 17.0, "survivor of group A"),
    _finding(18, 18.0, "member of group A two"),
    _finding(1000004, 40.0, "survivor of group B"),
    _finding(21, 21.0, "member of group B"),
    _finding(22, 22.0, "survivor of group C"),
    _finding(26, 26.0, "member of group A three"),
    _finding(27, 27.0, "member of group A four"),
    _finding(29, 29.0, "member of group C"),
]

_SEGMENTS = [{"start": 6.0, "text": "spoken line"}]

_SURVIVORS = {6, 11, 17, 1000004, 22}
_MEMBERS = {18, 21, 26, 27, 29}

# Three ways the same merge can be represented in live state.
# (a) Fresh UI: merges live only on reportState.merges. member_ids include the
#     survivor and arrive from the DOM as STRINGS (dataset.findingId).
_SETUP_FRESH_UI = (
    "reportState.findings = {"
    " '6': { verdict: 'accepted' }, '11': { verdict: 'accepted' },"
    " '17': { verdict: 'accepted' }, '1000004': { verdict: 'accepted' },"
    " '22': { verdict: 'accepted' }"
    "};"
    " reportState.merges = ["
    "  { id: '17', member_ids: ['17', '18', '26', '27'], summary_override: null },"
    "  { id: '1000004', member_ids: ['1000004', '21'], summary_override: null },"
    "  { id: '22', member_ids: ['22', '29'], summary_override: null }"
    " ];"
)

# (b) Disk reload: reportState.merges is empty (never persisted). The only trail
#     is merged_from_ids on each survivor's restored review state.
_SETUP_DISK_RELOAD = (
    "reportState.findings = {"
    " '6': { verdict: 'accepted' }, '11': { verdict: 'accepted' },"
    " '17': { verdict: 'none', merged_from_ids: [18, 26, 27] },"
    " '1000004': { verdict: 'none', merged_from_ids: [21] },"
    " '22': { verdict: 'none', merged_from_ids: [29] },"
    " '18': { verdict: 'none' }, '21': { verdict: 'none' },"
    " '26': { verdict: 'none' }, '27': { verdict: 'none' }, '29': { verdict: 'none' }"
    "};"
    " reportState.merges = [];"
)

# (c) Mixed: one group still in the UI state, two only in the data trail.
_SETUP_MIXED = (
    "reportState.findings = {"
    " '6': { verdict: 'accepted' }, '11': { verdict: 'accepted' },"
    " '17': { verdict: 'accepted' },"
    " '1000004': { verdict: 'none', merged_from_ids: [21] },"
    " '22': { verdict: 'none', merged_from_ids: [29] }"
    "};"
    " reportState.merges = ["
    "  { id: '17', member_ids: ['17', '18', '26', '27'], summary_override: null }"
    " ];"
)


def _run(driver_body: str, review_setup: str) -> None:
    """Execute review_app.js in node with a recording JSZip and run assertions."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js merge-fold tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findings = {json.dumps(_FINDINGS)};
        const segments = {json.dumps(_SEGMENTS)};

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

        const sandbox = {{
            console, setTimeout, clearTimeout, Math, Date, JSON, Promise,
            JSZip, URL: URLStub,
            window: {{
                location: {{ search: '' }},
                addEventListener() {{}}, removeEventListener() {{}},
                TRANSCRIPT_SEGMENTS: segments,
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

        const driver = `
            reportState.reviewer = 'qa';
            reportState.manualFrames = [];
            {review_setup}
            (async () => {{
                {driver_body}
            }})().catch((e) => {{ console.error(e && e.stack ? e.stack : String(e)); process.exit(1); }});
        `;

        const script = new vm.Script(
            i18nSource + "\\n" + languageControlSource + "\\n" + sttTransportSource + "\\n" +
            tabKeyboardSource + "\\n" + source + "\\n" + driver,
            {{ filename: 'review_app.js' }}
        );
        script.runInNewContext(sandbox);
        """
    )
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


_BUILD_REVIEW_ASSERTIONS = textwrap.dedent(
    f"""
    const data = buildReviewData();
    const ids = data.findings.map((f) => String(f.id)).sort();
    const want = {json.dumps(sorted(str(s) for s in _SURVIVORS))};
    if (data.findings.length !== 5)
        throw new Error('buildReviewData should fold to 5, got ' + data.findings.length + ' :: ' + JSON.stringify(ids));
    if (JSON.stringify(ids) !== JSON.stringify(want))
        throw new Error('survivor set wrong: ' + JSON.stringify(ids) + ' want ' + JSON.stringify(want));
    // No absorbed member may survive in the deliverable.
    const members = {json.dumps(sorted(str(m) for m in _MEMBERS))};
    for (const m of members) {{
        if (ids.includes(m)) throw new Error('absorbed member leaked: ' + m + ' :: ' + JSON.stringify(ids));
    }}
    // No survivor may appear twice (the duplication symptom).
    const seen = {{}};
    for (const id of ids) {{
        if (seen[id]) throw new Error('survivor duplicated in fold: ' + id);
        seen[id] = true;
    }}
    """
)


def test_build_review_data_folds_fresh_ui() -> None:
    """Fresh UI merges (reportState.merges, string member ids) fold 10 -> 5.

    Before the fix the integer finding id vs string member id Set mismatch left
    every member in place and duplicated the survivors -> 13.
    """
    _run(_BUILD_REVIEW_ASSERTIONS, _SETUP_FRESH_UI)


def test_build_review_data_folds_on_disk_reload() -> None:
    """Disk-reload merges (merged_from_ids only, no reportState.merges) fold 10 -> 5.

    This is the path the original view-only fold ignored entirely.
    """
    _run(_BUILD_REVIEW_ASSERTIONS, _SETUP_DISK_RELOAD)


def test_build_review_data_folds_mixed_sources_idempotently() -> None:
    """Merges split across both sources reconcile (not sum) to a single 10 -> 5 fold."""
    _run(_BUILD_REVIEW_ASSERTIONS, _SETUP_MIXED)


_EXPORT_ASSERTIONS = textwrap.dedent(
    f"""
    globalThis.__zip = new JSZip();
    globalThis.JSZip = function() {{ return globalThis.__zip; }};
    await exportReviewedZIP();
    const files = globalThis.__zip.files;

    const reviewedName = Object.keys(files).find((n) => n.startsWith('report_reviewed_'));
    if (!reviewedName) throw new Error('report_reviewed_*.json missing from bundle');
    const reviewed = JSON.parse(files[reviewedName].data);
    const ids = reviewed.findings.map((f) => String(f.id)).sort();
    if (reviewed.findings.length !== 5)
        throw new Error('reviewed export should fold to 5, got ' + reviewed.findings.length + ' :: ' + JSON.stringify(ids));
    const members = {json.dumps(sorted(str(m) for m in _MEMBERS))};
    for (const m of members) {{
        if (ids.includes(m)) throw new Error('absorbed member leaked into reviewed export: ' + m);
    }}
    const seen = {{}};
    for (const id of ids) {{
        if (seen[id]) throw new Error('survivor duplicated in reviewed export: ' + id);
        seen[id] = true;
    }}

    // agent_manifest folds to the same 5 (one entry per surviving finding).
    const manifest = JSON.parse(files['agent_manifest.json'].data);
    if (manifest.meta.total_findings !== 5)
        throw new Error('manifest should carry 5 findings, got ' + manifest.meta.total_findings);
    if (manifest.findings.length !== 5)
        throw new Error('manifest findings length: ' + manifest.findings.length);

    // Every layer agrees: reviewed == manifest count.
    if (reviewed.findings.length !== manifest.findings.length)
        throw new Error('layer drift: reviewed ' + reviewed.findings.length + ' vs manifest ' + manifest.findings.length);
    """
)


def test_export_zip_folds_fresh_ui() -> None:
    """exportReviewedZIP folds 10 -> 5 for fresh UI merges (the 13-finding bug)."""
    _run(_EXPORT_ASSERTIONS, _SETUP_FRESH_UI)


def test_export_zip_folds_on_disk_reload() -> None:
    """exportReviewedZIP folds 10 -> 5 from data-side merged_from_ids on reload."""
    _run(_EXPORT_ASSERTIONS, _SETUP_DISK_RELOAD)


def test_export_zip_folds_mixed_sources() -> None:
    """exportReviewedZIP reconciles both provenance sources to a single 10 -> 5 fold."""
    _run(_EXPORT_ASSERTIONS, _SETUP_MIXED)
