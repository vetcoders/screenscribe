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
            reportState.findings = {{
                a: {{
                    verdict: 'accepted', severity: 'high', notes: 'keep survivor',
                    annotations: [{{ type: 'rect', x: 0.1, y: 0.2 }}],
                }},
                b: {{
                    verdict: 'rejected', severity: 'low', notes: 'member note',
                    annotations: [{{ type: 'arrow', x1: 0.2, y1: 0.3 }}],
                }},
                c: {{ verdict: 'none', severity: null, notes: '', annotations: [] }},
            }};
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


def test_agent_overrides_survive_merged_review_serialization() -> None:
    """Agent-authored reviewer fields stay in human_review after a human merge."""
    _run_merge(
        """
        reportState.findings.a.summary_override = 'Corrected by reviewer';
        reportState.findings.a.category_override = 'ui';
        reportState.findings.a.actionItems = 'Add regression test';
        const data2 = buildReviewData();
        const human = data2.findings.find((f) => f.id === 'a').human_review;
        if (human.summary_override !== 'Corrected by reviewer')
            throw new Error('merged summary_override lost: ' + JSON.stringify(human));
        if (human.category_override !== 'ui')
            throw new Error('merged category_override lost: ' + JSON.stringify(human));
        if (human.action_items !== 'Add regression test')
            throw new Error('merged action_items lost: ' + JSON.stringify(human));
        """
    )


def test_merge_persists_member_review_snapshots_for_durable_unmerge() -> None:
    """Saved merge data retains each member's pre-merge reviewer state."""
    _run_merge(
        """
        const snapshots = byId['a'].human_review.merged_member_reviews || {};
        if (snapshots.b.verdict !== 'rejected' || snapshots.b.notes !== 'member note')
            throw new Error('absorbed member snapshot missing: ' + JSON.stringify(snapshots));
        if ((snapshots.b.annotations || [])[0]?.type !== 'arrow')
            throw new Error('absorbed member annotations missing from snapshot');
        const actual = byId['a'].human_review.merged_survivor_review || {};
        const baseline = byId['a'].human_review.merged_review_baseline || {};
        if (actual.notes !== 'keep survivor' || actual.severity !== 'high')
            throw new Error('actual survivor state missing: ' + JSON.stringify(actual));
        if (baseline.notes !== 'keep survivor\\\\n\\\\nmember note'
            || baseline.severity !== 'high')
            throw new Error('merged review baseline missing: ' + JSON.stringify(baseline));
        """
    )


def test_unmerge_restores_members_and_keeps_current_survivor_edits() -> None:
    """Unmerge returns N source findings without losing review work."""
    _run_merge(
        """
        reportState.findings.a.notes = 'edited while merged';
        reportState.findings.a.severity = 'critical';
        // Exercise the none -> auto-accepted -> undo path explicitly. The
        // harness starts a as accepted for the other merge assertions.
        reportState.merges[0].member_reviews.a.verdict = 'none';
        if (!unmergeFindings('a')) throw new Error('unmergeFindings returned false');
        if (reportState.merges.length !== 0) throw new Error('merge entry survived unmerge');
        if (reportState.findings.a.notes !== 'edited while merged'
            || reportState.findings.a.severity !== 'critical')
            throw new Error('current survivor edits were lost');
        if (reportState.findings.a.verdict !== 'none')
            throw new Error('unmerge kept the merge-generated accepted verdict');
        if (reportState.findings.b.verdict !== 'rejected'
            || reportState.findings.b.notes !== 'member note')
            throw new Error('absorbed member review was not restored: '
                + JSON.stringify(reportState.findings.b));
        const restored = buildReviewData().findings;
        if (restored.length !== 3)
            throw new Error('unmerge did not restore all source findings: ' + restored.length);
        if (restored.some((finding) => (finding.merged_from_ids || []).length > 0))
            throw new Error('unmerge leaked merge provenance into deliverable');
        """
    )
