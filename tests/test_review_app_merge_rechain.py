"""Re-merge (chaining) contract for a folded survivor card (finding C).

After a human-merge of two findings, the freshly rendered merged card is the only
visible representative of the group (the absorbed member cards are hidden as
``mergedAway``). The reviewer must still be able to fold a THIRD duplicate into
that group: ``mergeFindings`` already chains an existing merge base into a new
group, but the survivor card carried no merge-select checkbox, so it could never
be selected and the chaining path was unreachable from the UI.

These tests execute ``renderMergedCard`` and ``mergeFindings`` from
``review_app.js`` in a node sandbox. The DOM stub tracks created elements and
their children so a real ``querySelector('.merge-select')`` can assert the
checkbox exists on the rendered survivor card.
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

# Three findings: a + b are paraphrases to merge (a earliest -> base), c is a
# third duplicate that must be foldable into the existing a/b group.
_FINDINGS = [
    {
        "id": "a",
        "timestamp": 1.0,
        "timestamp_formatted": "00:01",
        "category": "ui",
        "text": "transcript A",
        "unified_analysis": {"summary": "Save broken", "severity": "low"},
    },
    {
        "id": "b",
        "timestamp": 2.0,
        "timestamp_formatted": "00:02",
        "category": "ui",
        "text": "transcript B",
        "unified_analysis": {"summary": "Save unresponsive", "severity": "high"},
    },
    {
        "id": "c",
        "timestamp": 3.0,
        "timestamp_formatted": "00:03",
        "category": "ui",
        "text": "transcript C",
        "unified_analysis": {"summary": "Save still broken", "severity": "medium"},
    },
]


# Numeric finding ids reproduce the rechain id-type mismatch: ids selected from
# the DOM arrive as STRINGS (dataset.findingId) while merge entries store the
# NUMBER parsed from the report JSON (m.id). A strict `Array.includes` then
# misses the existing group on a re-merge.
_FINDINGS_NUMERIC = [
    {
        "id": 1,
        "timestamp": 1.0,
        "timestamp_formatted": "00:01",
        "category": "ui",
        "text": "transcript 1",
        "unified_analysis": {"summary": "Save broken", "severity": "low"},
    },
    {
        "id": 2,
        "timestamp": 2.0,
        "timestamp_formatted": "00:02",
        "category": "ui",
        "text": "transcript 2",
        "unified_analysis": {"summary": "Save unresponsive", "severity": "high"},
    },
    {
        "id": 3,
        "timestamp": 3.0,
        "timestamp_formatted": "00:03",
        "category": "ui",
        "text": "transcript 3",
        "unified_analysis": {"summary": "Save still broken", "severity": "medium"},
    },
]


def _run(driver_body: str, findings: list | None = None) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js merge-rechain tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findings = {json.dumps(_FINDINGS if findings is None else findings)};
        const findingsEl = {{ textContent: JSON.stringify(findings) }};

        // DOM stub that tracks children so querySelector('.class') works on the
        // tree renderMergedCard builds.
        function matchesClass(el, sel) {{
            if (!sel.startsWith('.')) return false;
            const want = sel.slice(1);
            return String(el.className || '').split(/\\s+/).includes(want);
        }}
        function queryOne(el, sel) {{
            for (const child of el.children) {{
                if (matchesClass(child, sel)) return child;
                const deep = queryOne(child, sel);
                if (deep) return deep;
            }}
            return null;
        }}
        function makeEl(tag) {{
            return {{
                tagName: tag, className: '', textContent: '', hidden: false,
                href: '', download: '', type: '', checked: false, value: '',
                style: {{}}, dataset: {{}}, children: [], attributes: {{}},
                parentNode: null,
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}}, contains() {{ return false; }} }},
                appendChild(c) {{ this.children.push(c); if (c) c.parentNode = this; return c; }},
                insertBefore(c, ref) {{ this.children.unshift(c); if (c) c.parentNode = this; return c; }},
                removeChild(c) {{ const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }},
                remove() {{ if (this.parentNode) this.parentNode.removeChild(this); }},
                click() {{}}, addEventListener() {{}},
                setAttribute(k, v) {{ this.attributes[k] = v; }},
                get firstChild() {{ return this.children[0] || null; }},
                querySelector(sel) {{ return queryOne(this, sel); }},
                querySelectorAll() {{ return []; }},
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
            createElement(tag) {{ return makeEl(tag); }},
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
            {driver_body}
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


def test_merged_card_is_selectable_for_remerge() -> None:
    """The rendered merged survivor card must carry a merge-select checkbox so it
    can be selected to absorb a third duplicate."""
    _run(
        """
        const merged = mergeFindings(['a', 'b']);
        if (!merged) throw new Error('mergeFindings returned null');
        const card = renderMergedCard(merged);
        if (card.dataset.merged !== 'true') throw new Error('merged card lost dataset.merged');
        const checkbox = card.querySelector('.merge-select');
        if (!checkbox) throw new Error('merged survivor card has no merge-select checkbox - cannot re-merge');
        if (checkbox.type !== 'checkbox') throw new Error('merge-select is not a checkbox: ' + checkbox.type);
        """
    )


def test_merge_chains_third_duplicate_into_existing_group() -> None:
    """Selecting the merged survivor (a) + a third duplicate (c) folds c into the
    existing a/b group, producing one survivor representing a, b and c."""
    _run(
        """
        mergeFindings(['a', 'b']);
        const merged = mergeFindings(['a', 'c']);
        if (!merged) throw new Error('chained mergeFindings returned null');
        if (String(merged.id) !== 'a') throw new Error('survivor changed on chain: ' + merged.id);
        const trail = (merged.merged_from_ids || []).map(String);
        if (!trail.includes('b') || !trail.includes('c'))
            throw new Error('chain lost a member from merged_from_ids: ' + JSON.stringify(trail));
        if (trail.includes('a'))
            throw new Error('base a must not appear in its own merged_from_ids: ' + JSON.stringify(trail));
        // Exactly one merge group remains, covering all three.
        if (reportState.merges.length !== 1)
            throw new Error('expected one chained merge group, got ' + reportState.merges.length);
        const members = reportState.merges[0].member_ids.map(String).sort();
        if (JSON.stringify(members) !== JSON.stringify(['a', 'b', 'c']))
            throw new Error('chained group members wrong: ' + JSON.stringify(members));
        """
    )


def test_merge_chains_third_duplicate_with_numeric_ids() -> None:
    """With NUMERIC finding ids, re-merging the survivor (1) + a third duplicate (3)
    must still fold into the existing 1/2 group.

    Selected ids arrive as strings ('1','3') from the DOM while the stored merge
    entry's id is the number 1. Without normalizing both sides of the membership
    check the old group is not expanded/removed, leaving two merge entries and an
    orphaned member."""
    _run(
        """
        mergeFindings(['1', '2']);
        const merged = mergeFindings(['1', '3']);
        if (!merged) throw new Error('chained mergeFindings returned null');
        if (String(merged.id) !== '1') throw new Error('survivor changed on chain: ' + merged.id);
        // Exactly one merge group remains, covering all three.
        if (reportState.merges.length !== 1)
            throw new Error('expected one chained merge group, got ' + reportState.merges.length);
        const members = reportState.merges[0].member_ids.map(String).sort();
        if (JSON.stringify(members) !== JSON.stringify(['1', '2', '3']))
            throw new Error('chained group members wrong: ' + JSON.stringify(members));
        """,
        findings=_FINDINGS_NUMERIC,
    )
