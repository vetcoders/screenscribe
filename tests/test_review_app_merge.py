"""Human-merge (N -> 1) contract for the HTML Pro review app.

Genuinely executes ``mergeFindings`` and ``buildReviewData`` from
``review_app.js`` inside a node sandbox (same approach as
``test_review_app_zip_export.py``), so it asserts the real merged deliverable
rather than string-matching the source. The merge must mirror the machine dedup
pass (``screenscribe/unified/dedup.py::merge_finding_group``): one surviving
finding, highest severity, de-duplicated UNION of action_items /
affected_components / transcript_excerpts, the richest description, and a
``merged_from_ids`` provenance trail. Nothing from the merged-away findings is
lost.
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

# Three findings: a + b are paraphrases to merge (a earliest -> base), c stays.
# b carries the longest summary (richest), case-variant duplicate action item,
# and an overlapping component, so dedup-union behaviour is observable.
_FINDINGS = [
    {
        "id": "a",
        "timestamp": 1.0,
        "timestamp_formatted": "00:01",
        "category": "ui",
        "text": "transcript A about the broken save button",
        "unified_analysis": {
            "summary": "Save button broken",
            "severity": "low",
            "action_items": ["Fix the click handler", "Common follow-up"],
            "affected_components": ["SettingsForm"],
            "issues_detected": ["no click feedback"],
        },
    },
    {
        "id": "b",
        "timestamp": 2.0,
        "timestamp_formatted": "00:02",
        "category": "bug",
        "text": "transcript B about the same unresponsive save action",
        "unified_analysis": {
            "summary": "The save button is completely unresponsive on the settings screen",
            "severity": "high",
            "action_items": ["Add a regression test", "common follow-up"],
            "affected_components": ["SaveService", "settingsform"],
            "issues_detected": ["silent failure"],
        },
    },
    {
        "id": "c",
        "timestamp": 3.0,
        "timestamp_formatted": "00:03",
        "category": "performance",
        "text": "transcript C about a slow unrelated screen",
        "unified_analysis": {
            "summary": "Distinct performance problem",
            "severity": "medium",
            "action_items": ["Profile the render path"],
            "affected_components": ["Renderer"],
        },
    },
]


def _run_merge(extra_assertions: str) -> None:
    """Merge a+b in node, then run JS assertions on buildReviewData()."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js merge tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findings = {json.dumps(_FINDINGS)};
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

        const sandbox = {{
            console, setTimeout, clearTimeout, Math, Date, JSON, Promise,
            window: {{ location: {{ search: '' }}, addEventListener() {{}}, removeEventListener() {{}} }},
            document: documentStub,
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            navigator: {{ mediaDevices: {{}} }},
            process, confirm() {{ return true; }},
            fetch() {{ throw new Error('no network in test'); }},
        }};
        sandbox.window.document = sandbox.document;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.globalThis = sandbox;

        const i18nSource = fs.readFileSync({str(I18N_JS)!r}, 'utf8');
        const languageControlSource = fs.readFileSync({str(LANGUAGE_CONTROL_JS)!r}, 'utf8');
        const sttTransportSource = fs.readFileSync({str(STT_TRANSPORT_JS)!r}, 'utf8');
        const tabKeyboardSource = fs.readFileSync({str(TAB_KEYBOARD_JS)!r}, 'utf8');
        const source = fs.readFileSync({str(REVIEW_APP_JS)!r}, 'utf8');

        const driver = `
            reportState.reviewer = 'qa';
            reportState.manualFrames = [];
            reportState.findings = {{}};
            reportState.merges = [];
            const merged = mergeFindings(['a', 'b']);
            if (!merged) throw new Error('mergeFindings returned null');
            const data = buildReviewData();
            const out = data.findings;
            const byId = {{}};
            out.forEach((f) => {{ byId[f.id] = f; }});
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
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_merge_collapses_two_findings_into_one() -> None:
    """a + b -> a single deliverable entry; the absorbed id is gone, c untouched."""
    _run_merge(
        """
        if (out.length !== 2) throw new Error('expected 2 findings after merge, got ' + out.length);
        if (!('a' in byId)) throw new Error('surviving base finding a missing');
        if ('b' in byId) throw new Error('absorbed finding b leaked as a standalone entry');
        if (!('c' in byId)) throw new Error('unrelated finding c was dropped');
        """
    )


def test_merge_records_provenance_trail() -> None:
    """The surviving entry carries merged_from_ids with the absorbed id."""
    _run_merge(
        """
        const m = byId['a'];
        const trail = m.merged_from_ids || [];
        if (!trail.includes('b')) throw new Error('merged_from_ids missing absorbed id b: ' + JSON.stringify(trail));
        if (trail.includes('a')) throw new Error('base id a must not appear in merged_from_ids: ' + JSON.stringify(trail));
        if (!(m.human_review && (m.human_review.merged_from_ids || []).includes('b')))
            throw new Error('human_review.merged_from_ids lost the trail');
        """
    )


def test_merge_unions_all_value_fields() -> None:
    """No theme is lost: action_items / affected_components / transcript union."""
    _run_merge(
        """
        const ua = (byId['a'].unified_analysis) || {};
        const ai = ua.action_items || [];
        // Union of both, case-insensitive de-dup of "Common follow-up".
        for (const want of ['Fix the click handler', 'Common follow-up', 'Add a regression test']) {
            if (!ai.includes(want)) throw new Error('action_items lost theme ' + want + ': ' + JSON.stringify(ai));
        }
        const lower = ai.map((x) => x.toLowerCase());
        if (lower.filter((x) => x === 'common follow-up').length !== 1)
            throw new Error('action_items did not de-duplicate case-variant: ' + JSON.stringify(ai));

        const comps = (ua.affected_components || []).map((x) => x.toLowerCase());
        if (!comps.includes('settingsform') || !comps.includes('saveservice'))
            throw new Error('affected_components union incomplete: ' + JSON.stringify(ua.affected_components));
        if (comps.filter((x) => x === 'settingsform').length !== 1)
            throw new Error('affected_components did not de-duplicate: ' + JSON.stringify(ua.affected_components));

        const excerpts = byId['a'].transcript_excerpts || [];
        if (excerpts.length !== 2)
            throw new Error('transcript_excerpts must union both member transcripts: ' + JSON.stringify(excerpts));
        """
    )


def test_merge_keeps_richest_severity_and_summary() -> None:
    """Highest severity wins; richest (longest) summary becomes the description."""
    _run_merge(
        """
        const ua = (byId['a'].unified_analysis) || {};
        if (ua.severity !== 'high') throw new Error('merged severity must be highest (high), got ' + ua.severity);
        if (!ua.summary.includes('completely unresponsive'))
            throw new Error('merged summary must keep the richest description, got ' + ua.summary);
        """
    )


def test_merge_summary_override_is_editable() -> None:
    """An edited summary (summary_override) wins over the auto-picked richest one."""
    _run_merge(
        """
        reportState.merges[0].summary_override = 'Reviewer edited summary';
        const data2 = buildReviewData();
        const m = data2.findings.find((f) => f.id === 'a');
        if (m.unified_analysis.summary !== 'Reviewer edited summary')
            throw new Error('editable summary override not honored: ' + m.unified_analysis.summary);
        """
    )
