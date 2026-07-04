"""Re-merge must remove the stale generated card with normalized ids (finding O2).

``applyMergeToDom`` drops any prior ``.finding-merged`` card for the survivor
before inserting the freshly merged one. With NUMERIC report ids the comparison
broke: ``card.dataset.findingId`` is a STRING ('1', as a real DOM coerces every
dataset value) while ``merged.id`` is the NUMBER parsed from the report JSON, so
the strict ``===`` never matched and the old card was never removed.

Scenario from the finding: merge 1+2, then re-merge that survivor with 3. The
1+2 card should be replaced by the 1+2+3 card, but instead BOTH stayed visible
while ``reportState.merges`` held exactly one group -- two visible representatives
for one export group.

Driven against the functional DOM (``tests/_review_dom.js``) so dataset values
are string-coerced exactly as in a browser, which is what reproduces the bug.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "screenscribe/html_pro_assets/scripts"
I18N_JS = SCRIPTS / "i18n.js"
LANGUAGE_CONTROL_JS = SCRIPTS / "lib/language-control.js"
STT_TRANSPORT_JS = SCRIPTS / "lib/stt-transport.js"
TAB_KEYBOARD_JS = SCRIPTS / "lib/tab-keyboard.js"
REVIEW_APP_JS = SCRIPTS / "review_app.js"
DOM_HELPER_JS = Path(__file__).resolve().parent / "_review_dom.js"


def _finding(fid: int, ts: float, summary: str) -> dict:
    return {
        "id": fid,
        "timestamp": ts,
        "timestamp_formatted": f"{int(ts) // 60:02d}:{int(ts) % 60:02d}",
        "category": "ui",
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


# Numeric ids reproduce the string-vs-number dataset mismatch.
_FINDINGS = [
    _finding(1, 1.0, "save button unresponsive"),
    _finding(2, 2.0, "save does nothing"),
    _finding(3, 3.0, "save still broken"),
]


def _run(driver_body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js merge tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const {{ createDocument, buildFindingArticle }} = require({str(DOM_HELPER_JS)!r});

        const findings = {json.dumps(_FINDINGS)};
        const {{ document, body, tab }} = createDocument(findings);
        for (const f of findings) tab.appendChild(buildFindingArticle(document, f));

        const sandbox = {{
            console, setTimeout, clearTimeout, setInterval, clearInterval,
            Math, Date, JSON, Promise, Array, Object, String, Number, Boolean, Set, Map,
            window: {{
                location: {{ search: '' }},
                addEventListener() {{}}, removeEventListener() {{}},
                setTimeout, clearTimeout, setInterval, clearInterval,
                TRANSCRIPT_SEGMENTS: [],
            }},
            document, tab, body,
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            navigator: {{ mediaDevices: {{}} }},
            Blob: class Blob {{ constructor(c, o = {{}}) {{ this.chunks = c; this.type = o.type || ''; }} }},
            ResizeObserver: class ResizeObserver {{ observe() {{}} disconnect() {{}} }},
            Image: class Image {{}},
            process, confirm() {{ return true; }},
            fetch() {{ throw new Error('no network in test'); }},
        }};
        sandbox.window.document = document;
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
            initMergeUI();
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


def test_remerge_removes_stale_merged_card_with_numeric_ids() -> None:
    """RED before fix: the 1+2 card survives the re-merge, leaving two merged
    cards for one export group."""
    _run(
        textwrap.dedent(
            """
            // Merge 1+2: one merged card, dataset.findingId coerced to '1'.
            const first = mergeFindings(['1', '2']);
            if (!first) throw new Error('first mergeFindings returned null');
            applyMergeToDom(first);
            if (tab.querySelectorAll('.finding-merged').length !== 1)
                throw new Error('expected 1 merged card after first merge');

            // Re-merge survivor (1) + a third duplicate (3). merged.id is the
            // NUMBER 1; the stale card's dataset.findingId is the STRING '1'.
            const second = mergeFindings(['1', '3']);
            if (!second) throw new Error('re-merge returned null');
            applyMergeToDom(second);

            // Exactly ONE merged representative must remain, matching the single
            // merge group in state.
            const mergedCards = tab.querySelectorAll('.finding-merged');
            if (mergedCards.length !== 1)
                throw new Error('expected exactly 1 merged card after re-merge, got '
                    + mergedCards.length);
            if (reportState.merges.length !== 1)
                throw new Error('expected exactly 1 merge group, got '
                    + reportState.merges.length);
            // The survivor card must represent the chained group.
            if (mergedCards[0].dataset.findingId !== '1')
                throw new Error('merged card has wrong survivor id: '
                    + mergedCards[0].dataset.findingId);
            """
        )
    )
