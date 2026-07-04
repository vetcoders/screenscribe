"""ZIP export contract for the HTML Pro review app (agent handoff bundle).

These tests genuinely execute ``exportReviewedZIP`` from ``review_app.js`` inside
a node sandbox with a recording fake ``JSZip``. They assert the real files the
function writes into the bundle, rather than string-matching the source — so a
refactor that silently drops a file is caught.
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

# One AI finding (accepted) carrying a base64 screenshot + unified analysis, so
# the export must emit a screenshots/ JPG and a manifest entry for it.
_FINDINGS = [
    {
        "id": "f1",
        "timestamp_formatted": "01:15",
        "category": "ui",
        "text": "the save button does nothing",
        "context": "on the settings screen the save button is unresponsive",
        "screenshot": "data:image/jpeg;base64,QUJD",  # "ABC"
        "unified_analysis": {
            "summary": "Save button unresponsive",
            "severity": "high",
            "action_items": ["Fix the click handler", "Add a regression test"],
            "affected_components": ["SettingsForm"],
        },
    }
]

_SEGMENTS = [
    {"start": 5.0, "text": "let me show you the settings screen"},
    {"start": 75.0, "text": "now the save button does nothing"},
]


# Default review state: f1 accepted, no merges. Routing tests override this.
_DEFAULT_REVIEW_SETUP = (
    "reportState.findings = { f1: { verdict: 'accepted', severity: 'high', "
    "notes: '', annotations: [] } };"
)


def _run_export(
    extra_assertions: str,
    findings: list[dict] | None = None,
    review_setup: str = _DEFAULT_REVIEW_SETUP,
) -> None:
    """Execute exportReviewedZIP in node and run JS assertions on the bundle."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js ZIP export tests")

    findings = findings if findings is not None else _FINDINGS

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findings = {json.dumps(findings)};
        const segments = {json.dumps(_SEGMENTS)};

        // Recording fake JSZip: captures every file written to the bundle.
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
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                appendChild() {{}}, removeChild() {{}}, remove() {{}}, click() {{}},
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
            JSZip,
            URL: URLStub,
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
            globalThis.__zip = new JSZip();
            const _origJSZip = JSZip;
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
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_zip_includes_timestamped_transcript() -> None:
    """transcript.txt ships with one timestamped line per transcript segment."""
    _run_export(
        """
        if (!('transcript.txt' in files)) throw new Error('transcript.txt missing from ZIP');
        const txt = files['transcript.txt'].data;
        if (!txt.includes('[00:05] let me show you the settings screen'))
            throw new Error('transcript missing first timestamped line: ' + txt);
        if (!txt.includes('[01:15] now the save button does nothing'))
            throw new Error('transcript missing second timestamped line: ' + txt);
        """
    )


def test_zip_includes_agent_manifest_and_screenshots() -> None:
    """agent_manifest.json (with verify criteria) and screenshots/ JPG ship."""
    _run_export(
        """
        if (!('agent_manifest.json' in files)) throw new Error('agent_manifest.json missing');
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        if (!manifest.meta || manifest.meta.total_findings !== 1)
            throw new Error('manifest meta wrong: ' + JSON.stringify(manifest.meta));
        const finding = manifest.findings[0];
        if (finding.id !== 'F01') throw new Error('manifest finding id: ' + finding.id);
        if (finding.priority !== 'P0') throw new Error('high severity must map to P0, got ' + finding.priority);
        if (!finding.verify || !finding.verify.trim()) throw new Error('manifest finding has empty verify');
        if (finding.status !== 'pending') throw new Error('manifest finding status: ' + finding.status);
        if (!finding.screenshot.startsWith('screenshots/'))
            throw new Error('manifest screenshot path not relative: ' + finding.screenshot);
        if (!(finding.screenshot in files))
            throw new Error('screenshot file missing from ZIP: ' + finding.screenshot + ' have ' + Object.keys(files).join(','));
        """
    )


# Two findings: f1 carries a real data-URL screenshot, f2 is text-only (e.g.
# frame extraction failed but the report still generated). f2 has NO `screenshot`
# data URL, so the bundle must not reference a screenshots/Fxx file for it.
_TEXT_ONLY_FINDINGS = [
    {
        "id": "f1",
        "timestamp_formatted": "01:15",
        "category": "ui",
        "text": "the save button does nothing",
        "screenshot": "data:image/jpeg;base64,QUJD",
        "unified_analysis": {
            "summary": "Save button unresponsive",
            "severity": "high",
            "action_items": ["Fix the click handler"],
            "affected_components": ["SettingsForm"],
        },
    },
    {
        "id": "f2",
        "timestamp_formatted": "02:30",
        "category": "perf",
        "text": "the narrator describes a slow screen with no usable frame",
        # No `screenshot` key at all: extraction produced no image.
        "unified_analysis": {
            "summary": "Slow screen (no frame captured)",
            "severity": "medium",
            "action_items": ["Profile the render path"],
            "affected_components": ["Renderer"],
        },
    },
]

_TEXT_ONLY_REVIEW_SETUP = (
    "reportState.findings = {"
    " f1: { verdict: 'accepted', severity: 'high', notes: '', annotations: [] },"
    " f2: { verdict: 'accepted', severity: 'medium', notes: '', annotations: [] }"
    "};"
)


def test_text_only_finding_does_not_reference_missing_screenshot() -> None:
    """A text-only finding (no data-URL frame) must NOT claim a screenshot file:
    neither the reviewed JSON's screenshot_original nor the manifest's screenshot
    may point at a screenshots/Fxx the bundle never wrote. The finding WITH a
    screenshot must still reference its real, present file."""
    _run_export(
        """
        // Reviewed JSON: f1 references a present file; f2 has no dead link.
        const reviewedName = Object.keys(files).find((n) => n.startsWith('report_reviewed_'));
        if (!reviewedName) throw new Error('report_reviewed_*.json missing');
        const reviewed = JSON.parse(files[reviewedName].data);
        const f1 = reviewed.findings.find((f) => f.id === 'f1');
        const f2 = reviewed.findings.find((f) => f.id === 'f2');
        if (!f1 || !f2) throw new Error('expected both f1 and f2 in reviewed export');
        if (!f1.screenshot_original || !(f1.screenshot_original in files))
            throw new Error('f1 screenshot_original must point at a present file: ' + f1.screenshot_original);
        if (f2.screenshot_original)
            throw new Error('text-only f2 must not reference a screenshot: ' + f2.screenshot_original);

        // Manifest: same rule. f2's screenshot is null and points at nothing real.
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        const m1 = manifest.findings.find((m) => m.title.includes('unresponsive'));
        const m2 = manifest.findings.find((m) => m.title.includes('no frame'));
        if (!m1 || !m2) throw new Error('manifest missing an entry: ' + JSON.stringify(manifest.findings.map((m) => m.id)));
        if (!m1.screenshot || !(m1.screenshot in files))
            throw new Error('manifest f1 screenshot must be a present file: ' + m1.screenshot);
        if (m2.screenshot !== null)
            throw new Error('manifest text-only entry must have null screenshot, got: ' + m2.screenshot);
        // No manifest entry may point at a screenshots/ path that is absent.
        for (const m of manifest.findings) {
            if (m.screenshot && !(m.screenshot in files))
                throw new Error('manifest references missing screenshot: ' + m.screenshot);
        }
        """,
        findings=_TEXT_ONLY_FINDINGS,
        review_setup=_TEXT_ONLY_REVIEW_SETUP,
    )


# Three findings where f1 + f3 are human-merged (f1 is base by timestamp), f2
# stays standalone. Routing must fold the merge into ONE deliverable entry.
_MERGE_FINDINGS = [
    {
        "id": "f1",
        "timestamp": 75.0,
        "timestamp_formatted": "01:15",
        "category": "ui",
        "text": "the save button does nothing",
        "screenshot": "data:image/jpeg;base64,QUJD",
        "unified_analysis": {
            "summary": "Save button unresponsive",
            "severity": "high",
            "action_items": ["Fix the click handler"],
            "affected_components": ["SettingsForm"],
        },
    },
    {
        "id": "f2",
        "timestamp": 80.0,
        "timestamp_formatted": "01:20",
        "category": "perf",
        "text": "the page is slow",
        "screenshot": "data:image/jpeg;base64,REVG",
        "unified_analysis": {
            "summary": "Slow render",
            "severity": "medium",
            "action_items": ["Profile the render"],
            "affected_components": ["Renderer"],
        },
    },
    {
        "id": "f3",
        "timestamp": 90.0,
        "timestamp_formatted": "01:30",
        "category": "ui",
        "text": "save still broken",
        "screenshot": "data:image/jpeg;base64,R0hJ",
        "unified_analysis": {
            "summary": "Save button still does nothing after reload",
            "severity": "low",
            "action_items": ["Add a regression test"],
            "affected_components": ["SettingsForm", "Persistence"],
        },
    },
]

# Post-merge review state: base accepted, absorbed member reverts to none, f2
# stays accepted. Mirrors what mergeFindings() writes into reportState.
_MERGE_REVIEW_SETUP = (
    "reportState.findings = {"
    " f1: { verdict: 'accepted', severity: 'high', notes: '', annotations: [] },"
    " f2: { verdict: 'accepted', severity: 'medium', notes: '', annotations: [] },"
    " f3: { verdict: 'none', severity: '', notes: '', annotations: [] }"
    "};"
    " reportState.merges = [{ id: 'f1', member_ids: ['f1', 'f3'], summary_override: null }];"
)


def test_agent_manifest_folds_merged_group_to_single_entry() -> None:
    """A merged group ships as ONE manifest+bundle entry; the absorbed member
    (verdict `none`, not `rejected`) must not leak as a standalone finding, and
    the surviving entry carries the `merged_from_ids` provenance trail."""
    _run_export(
        """
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        // f2 standalone + one folded f1/f3 entry == 2, NOT 3.
        if (manifest.meta.total_findings !== 2)
            throw new Error('expected 2 manifest findings (f2 + merged), got ' + manifest.meta.total_findings);
        if (manifest.findings.length !== 2)
            throw new Error('manifest findings array length: ' + manifest.findings.length);
        const merged = manifest.findings.find((f) => Array.isArray(f.merged_from_ids) && f.merged_from_ids.length > 0);
        if (!merged) throw new Error('no manifest entry carries merged_from_ids');
        if (!merged.merged_from_ids.includes('f3'))
            throw new Error('merged_from_ids must record absorbed f3: ' + JSON.stringify(merged.merged_from_ids));
        if (merged.merged_from_ids.includes('f1'))
            throw new Error('base f1 must NOT appear in its own merged_from_ids');
        // Union of action items from f1 + f3 reaches the merged manifest entry.
        if (!merged.action_items.includes('Fix the click handler') || !merged.action_items.includes('Add a regression test'))
            throw new Error('merged entry lost unioned action_items: ' + JSON.stringify(merged.action_items));

        // The bundle's report_reviewed JSON must fold too: f3 gone as standalone.
        const reviewedName = Object.keys(files).find((n) => n.startsWith('report_reviewed_'));
        const reviewed = JSON.parse(files[reviewedName].data);
        if (reviewed.findings.length !== 2)
            throw new Error('report_reviewed findings should fold to 2, got ' + reviewed.findings.length);
        const ids = reviewed.findings.map((f) => f.id);
        if (ids.includes('f3'))
            throw new Error('absorbed f3 leaked into report_reviewed findings: ' + JSON.stringify(ids));
        // Absorbed member also must not surface in rejected[] (it is none).
        const rej = (reviewed.rejected || []).map((r) => r.id);
        if (rej.includes('f3'))
            throw new Error('absorbed f3 must not leak into rejected[]: ' + JSON.stringify(rej));
        """,
        findings=_MERGE_FINDINGS,
        review_setup=_MERGE_REVIEW_SETUP,
    )


# Two findings: f1 accepted (with a severity override low->critical + a note),
# f2 rejected as a false positive (carries a reviewer note). This is the
# decision-persistence shape the audit flagged as uncovered: the suspected bug is
# "export drops/overwrites reviewer decisions to accepted", so we assert both the
# rejected routing AND that the accepted finding keeps its verdict + override.
_REJECT_FINDINGS = [
    {
        "id": "f1",
        "timestamp": 75.0,
        "timestamp_formatted": "01:15",
        "category": "ui",
        "text": "the save button does nothing",
        "screenshot": "data:image/jpeg;base64,QUJD",
        "unified_analysis": {
            "summary": "Save button unresponsive",
            "severity": "low",
            "action_items": ["Fix the click handler"],
            "affected_components": ["SettingsForm"],
        },
    },
    {
        "id": "f2",
        "timestamp": 80.0,
        "timestamp_formatted": "01:20",
        "category": "perf",
        "text": "the page is slow",
        "screenshot": "data:image/jpeg;base64,REVG",
        "unified_analysis": {
            "summary": "Slow render",
            "severity": "medium",
            "action_items": ["Profile the render"],
            "affected_components": ["Renderer"],
        },
    },
]

_REJECT_REVIEW_SETUP = (
    "reportState.findings = {"
    " f1: { verdict: 'accepted', severity: 'critical', notes: 'confirmed on prod', annotations: [] },"
    " f2: { verdict: 'rejected', severity: '', notes: 'false positive, by design', annotations: [] }"
    "};"
)


def test_bundle_routes_rejected_to_summary_and_preserves_accepted() -> None:
    """The bundled report_reviewed_*.json must record a rejected finding ONLY in
    ``rejected[]`` (never as a shippable finding), while the accepted finding
    keeps its verdict and severity override. Directly covers the audit's suspected
    "export overwrites decisions to accepted" regression."""
    _run_export(
        """
        const reviewedName = Object.keys(files).find((n) => n.startsWith('report_reviewed_'));
        if (!reviewedName) throw new Error('report_reviewed_*.json missing from bundle');
        const reviewed = JSON.parse(files[reviewedName].data);

        // Deliverable ships ONLY the accepted finding (f1). f2 (rejected) is out.
        const ids = reviewed.findings.map((f) => f.id);
        if (reviewed.findings.length !== 1)
            throw new Error('findings[] should hold only the accepted f1, got ' + JSON.stringify(ids));
        if (ids.includes('f2'))
            throw new Error('rejected f2 leaked into shippable findings[]: ' + JSON.stringify(ids));

        // The accepted finding keeps its verdict + override + note (anti-flip).
        const f1 = reviewed.findings.find((f) => f.id === 'f1');
        if (!f1) throw new Error('accepted f1 missing from findings[]');
        if (f1.human_review.verdict !== 'accepted')
            throw new Error('f1 verdict overwritten: ' + f1.human_review.verdict);
        if (f1.human_review.severity_override !== 'critical')
            throw new Error('f1 severity override lost: ' + f1.human_review.severity_override);
        if (f1.human_review.notes !== 'confirmed on prod')
            throw new Error('f1 notes round-trip lost: ' + JSON.stringify(f1.human_review.notes));

        // rejected[] records f2 with its note + reviewer; f1 must NOT appear there.
        const rejected = reviewed.rejected || [];
        const f2 = rejected.find((r) => r.id === 'f2');
        if (!f2) throw new Error('rejected f2 missing from rejected[]: ' + JSON.stringify(rejected.map((r) => r.id)));
        if (f2.notes !== 'false positive, by design')
            throw new Error('rejected f2 notes round-trip lost: ' + JSON.stringify(f2.notes));
        if (f2.rejected_by !== 'qa')
            throw new Error('rejected f2 lost reviewer attribution: ' + JSON.stringify(f2.rejected_by));
        if (rejected.some((r) => r.id === 'f1'))
            throw new Error('accepted f1 wrongly listed in rejected[]');

        // Hard negative: nothing may be BOTH shipped and rejected (double-count).
        const rejIds = new Set(rejected.map((r) => r.id));
        if (ids.some((id) => rejIds.has(id)))
            throw new Error('a finding is both in findings[] and rejected[]');

        // agent_manifest folds the same way: one entry (f1), rejected f2 absent.
        const manifest = JSON.parse(files['agent_manifest.json'].data);
        if (manifest.meta.total_findings !== 1)
            throw new Error('manifest should carry only the accepted finding, got ' + manifest.meta.total_findings);
        if (manifest.findings.some((f) => f.id === 'F02' || f.id === 'f2'))
            throw new Error('rejected finding leaked into agent_manifest');
        """,
        findings=_REJECT_FINDINGS,
        review_setup=_REJECT_REVIEW_SETUP,
    )
