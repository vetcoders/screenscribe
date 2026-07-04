"""Reviewer-state inheritance when folding findings (I2).

Regression cover for a bug hit on a live review: ``mergeFindingGroup`` reconciled
only the AI ``unified_analysis`` (max severity + union of action_items / issues /
keywords / transcript_excerpts), but NOT the reviewer's ``human_review`` state
(``verdict``, ``severity_override``, ``notes``, ``annotations``) of the absorbed
members. The survivor kept ONLY its own (base) review, so any verdict / priority /
notes / annotation the reviewer set on a merged-away member silently vanished —
on a real export the folded survivor came out ``none/medium`` despite reviewer
action on its members.

The accepted default the operator signed off on:

  * verdict           -- accepted-wins (accepted > rejected > none across the group);
  * severity_override -- the HIGHEST rank any member set (MERGE_SEVERITY_RANK), null
                         when nobody overrode;
  * notes             -- deduped union of every member's notes (nothing dropped);
  * annotations       -- the survivor's OWN annotations stay on the survivor's
                         image (they are rasterized onto it); absorbed members'
                         annotations are preserved as ``member_annotations`` evidence
                         so they are never lost AND never drawn on the wrong image.

These tests execute ``buildReviewData`` and ``exportReviewedZIP`` from
``review_app.js`` in a node sandbox across BOTH provenance shapes (live UI
``reportState.merges`` and disk-reload ``merged_from_ids``), mirroring
``tests/test_review_app_merge_fold.py``.
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
    browser (the cross-type id condition the fold paths must survive).
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


# Survivor 17 absorbs member 18. 6 is a standalone control finding.
_FINDINGS = [
    _finding(6, 6.0, "standalone control finding"),
    _finding(17, 17.0, "survivor of the merge"),
    _finding(18, 18.0, "absorbed member"),
]

_SEGMENTS = [{"start": 6.0, "text": "spoken line"}]


# ---------------------------------------------------------------------------
# State setups: each carries the SAME logical merge (17 <- 18) where the member
# (18) holds the reviewer signal and the survivor (17) is blank, expressed in
# both provenance shapes.
# ---------------------------------------------------------------------------

# (a) live UI: merge lives on reportState.merges; member 18 carries the signal.
_SETUP_LIVE = (
    "reportState.findings = {"
    " '6': { verdict: 'none' },"
    " '17': { verdict: 'none', severity: null, notes: '',"
    "         annotations: [{ type: 'arrow' }] },"
    " '18': { verdict: 'accepted', severity: 'high', notes: 'member note',"
    "         annotations: [{ type: 'text', text: 'MEMBER_MARK' }] }"
    "};"
    " reportState.merges = ["
    "  { id: '17', member_ids: ['17', '18'], summary_override: null }"
    " ];"
)

# (b) disk reload: reportState.merges empty; trail lives on merged_from_ids.
_SETUP_DISK = (
    "reportState.findings = {"
    " '6': { verdict: 'none' },"
    " '17': { verdict: 'none', severity: null, notes: '',"
    "         annotations: [{ type: 'arrow' }], merged_from_ids: [18] },"
    " '18': { verdict: 'accepted', severity: 'high', notes: 'member note',"
    "         annotations: [{ type: 'text', text: 'MEMBER_MARK' }] }"
    "};"
    " reportState.merges = [];"
)

# (b2) TRUE cold reload: the absorbed member (18) is GONE from reportState (only
# the survivor survives /api/review-state), and the member's annotations live
# ONLY as the survivor's persisted member_annotations. The survivor already
# carries the reconciled group verdict/severity/notes (persisted at save time).
_SETUP_COLD_RELOAD = (
    "reportState.findings = {"
    " '6': { verdict: 'none' },"
    " '17': { verdict: 'accepted', severity: 'high', notes: 'member note',"
    "         annotations: [{ type: 'arrow' }], merged_from_ids: [18],"
    "         member_annotations: [{ finding_id: 18,"
    "             annotations: [{ type: 'text', text: 'MEMBER_MARK' }] }] }"
    "};"
    " reportState.merges = [];"
)

# (c) max-priority: members at different priorities -> survivor takes the highest.
_SETUP_MAX_PRIORITY = (
    "reportState.findings = {"
    " '6': { verdict: 'none' },"
    " '17': { verdict: 'accepted', severity: 'medium', notes: '' },"
    " '18': { verdict: 'accepted', severity: 'critical', notes: '' }"
    "};"
    " reportState.merges = ["
    "  { id: '17', member_ids: ['17', '18'], summary_override: null }"
    " ];"
)

# (d) negative: a real merge where NOBODY acted -> survivor stays blank.
_SETUP_NEGATIVE = (
    "reportState.findings = {"
    " '6': { verdict: 'none' },"
    " '17': { verdict: 'none', severity: null, notes: '' },"
    " '18': { verdict: 'none', severity: null, notes: '' }"
    "};"
    " reportState.merges = ["
    "  { id: '17', member_ids: ['17', '18'], summary_override: null }"
    " ];"
)


def _run(driver_body: str, review_setup: str) -> None:
    """Execute review_app.js in node with a recording JSZip and run assertions."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js merge-inheritance tests")

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


# --- buildReviewData (the in-memory data path) --------------------------------

_INHERIT_BUILD_ASSERTIONS = textwrap.dedent(
    """
    const data = buildReviewData();
    const survivor = data.findings.find((f) => String(f.id) === '17');
    if (!survivor) throw new Error('survivor 17 missing from fold');
    if (data.findings.some((f) => String(f.id) === '18'))
        throw new Error('absorbed member 18 leaked');
    const hr = survivor.human_review || {};
    if (hr.verdict !== 'accepted')
        throw new Error('verdict not inherited (accepted-wins): ' + hr.verdict);
    if (hr.severity_override !== 'high')
        throw new Error('severity_override not inherited: ' + hr.severity_override);
    if (!String(hr.notes || '').includes('member note'))
        throw new Error("member note not inherited: '" + hr.notes + "'");
    """
)


def test_build_review_data_inherits_live() -> None:
    _run(_INHERIT_BUILD_ASSERTIONS, _SETUP_LIVE)


def test_build_review_data_inherits_disk_reload() -> None:
    _run(_INHERIT_BUILD_ASSERTIONS, _SETUP_DISK)


_MAX_PRIORITY_ASSERTIONS = textwrap.dedent(
    """
    const data = buildReviewData();
    const survivor = data.findings.find((f) => String(f.id) === '17');
    if (!survivor) throw new Error('survivor 17 missing');
    const hr = survivor.human_review || {};
    if (hr.severity_override !== 'critical')
        throw new Error('max-priority not taken: ' + hr.severity_override);
    """
)


def test_build_review_data_takes_max_priority() -> None:
    _run(_MAX_PRIORITY_ASSERTIONS, _SETUP_MAX_PRIORITY)


_NEGATIVE_ASSERTIONS = textwrap.dedent(
    """
    const data = buildReviewData();
    const survivor = data.findings.find((f) => String(f.id) === '17');
    if (!survivor) throw new Error('survivor 17 missing');
    const hr = survivor.human_review || {};
    if (hr.verdict !== 'none')
        throw new Error('negative verdict drifted: ' + hr.verdict);
    if (hr.severity_override !== null)
        throw new Error('negative severity_override should be null: ' + JSON.stringify(hr.severity_override));
    if ((hr.notes || '') !== '')
        throw new Error("negative notes should be empty: '" + hr.notes + "'");
    if (hr.member_annotations && hr.member_annotations.length)
        throw new Error('negative should carry no member_annotations');
    """
)


def test_build_review_data_negative_no_inheritance() -> None:
    _run(_NEGATIVE_ASSERTIONS, _SETUP_NEGATIVE)


_ANNOTATION_ASSERTIONS = textwrap.dedent(
    """
    const data = buildReviewData();
    const survivor = data.findings.find((f) => String(f.id) === '17');
    if (!survivor) throw new Error('survivor 17 missing');
    const hr = survivor.human_review || {};
    // The member annotation must survive somewhere...
    const memberAnns = hr.member_annotations || [];
    const kept = memberAnns.some((m) =>
        String(m.finding_id) === '18' &&
        (m.annotations || []).some((a) => a.text === 'MEMBER_MARK'));
    if (!kept) throw new Error('member annotation lost on fold');
    // ...but it must NOT land on the survivor's own image annotations.
    for (const a of (hr.annotations || [])) {
        if (a && a.text === 'MEMBER_MARK')
            throw new Error('member annotation rasterized onto survivor image');
    }
    // Survivor keeps its own annotation.
    if (!(hr.annotations || []).some((a) => a.type === 'arrow'))
        throw new Error("survivor's own annotation dropped");
    """
)


def test_build_review_data_preserves_member_annotations_off_survivor_image() -> None:
    _run(_ANNOTATION_ASSERTIONS, _SETUP_LIVE)


def test_build_review_data_preserves_member_annotations_after_cold_reload() -> None:
    """The absorbed member is gone from reportState (cold reload); its marks must
    survive via the survivor's persisted member_annotations, not be recomputed
    from the (now absent) member finding and silently dropped."""
    _run(_ANNOTATION_ASSERTIONS, _SETUP_COLD_RELOAD)


# --- exportReviewedZIP (the deliverable bundle path) --------------------------

_EXPORT_INHERIT_ASSERTIONS = textwrap.dedent(
    """
    globalThis.__zip = new JSZip();
    globalThis.JSZip = function() { return globalThis.__zip; };
    await exportReviewedZIP();
    const files = globalThis.__zip.files;
    const reviewedName = Object.keys(files).find((n) => n.startsWith('report_reviewed_'));
    if (!reviewedName) throw new Error('report_reviewed_*.json missing');
    const reviewed = JSON.parse(files[reviewedName].data);
    const survivor = reviewed.findings.find((f) => String(f.id) === '17');
    if (!survivor) throw new Error('survivor 17 missing from reviewed export');
    if (reviewed.findings.some((f) => String(f.id) === '18'))
        throw new Error('absorbed member 18 leaked into reviewed export');
    const hr = survivor.human_review || {};
    if (hr.verdict !== 'accepted')
        throw new Error('export verdict not inherited: ' + hr.verdict);
    if (hr.severity_override !== 'high')
        throw new Error('export severity_override not inherited: ' + hr.severity_override);
    if (!String(hr.notes || '').includes('member note'))
        throw new Error("export member note not inherited: '" + hr.notes + "'");
    const memberAnns = hr.member_annotations || [];
    const kept = memberAnns.some((m) =>
        String(m.finding_id) === '18' &&
        (m.annotations || []).some((a) => a.text === 'MEMBER_MARK'));
    if (!kept) throw new Error('export dropped member annotation evidence');
    // agent_manifest priority must follow the inherited (high) severity -> P0.
    const manifest = JSON.parse(files['agent_manifest.json'].data);
    const entry = manifest.findings.find((e) => Array.isArray(e.merged_from_ids));
    if (!entry) throw new Error('merged manifest entry missing');
    if (entry.severity !== 'high' || entry.priority !== 'P0')
        throw new Error('manifest priority not inherited: ' + entry.priority + '/' + entry.severity);
    """
)


def test_export_zip_inherits_live() -> None:
    _run(_EXPORT_INHERIT_ASSERTIONS, _SETUP_LIVE)


def test_export_zip_inherits_disk_reload() -> None:
    _run(_EXPORT_INHERIT_ASSERTIONS, _SETUP_DISK)


def test_export_zip_preserves_member_annotations_after_cold_reload() -> None:
    """Cold reload: the absorbed member finding is gone, so the export must keep
    the member annotation evidence from the survivor's persisted copy."""
    _run(_EXPORT_INHERIT_ASSERTIONS, _SETUP_COLD_RELOAD)
