"""A merged survivor card must stay fully reviewable (L3, P2).

When the reviewer merges findings, applyMergeToDom hides every member and leaves
the generated merged card as the sole visible representative — but renderMergedCard
emitted only a summary/content block (editable summary, action items, components,
provenance). The survivor was auto-``accepted`` with no controls, so the reviewer
could no longer reject the merged finding, change its severity, add notes, or
inspect/annotate its screenshot. The merge silently locked review.

This test drives the real ``mergeFindings`` + ``applyMergeToDom`` from
``review_app.js`` against a functional DOM (``tests/_review_dom.js``) and asserts
the merged card carries the same review controls as a normal finding card
(verdict radios, severity select, notes, annotation affordance) AND that changing
the verdict to ``rejected`` via event delegation reaches the survivor's state.
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


_FINDINGS = [
    _finding(17, 17.0, "survivor of the merge"),
    _finding(18, 18.0, "absorbed member"),
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


def test_merged_card_has_full_review_controls() -> None:
    """The merged survivor card carries verdict / severity / notes / annotation.

    RED before the fix: renderMergedCard emits only the summary block, so none of
    these controls exist on the merged card.
    """
    _run(
        textwrap.dedent(
            """
            reportState.findings = {
                '17': { verdict: 'accepted' },
                '18': { verdict: 'accepted' },
            };
            const merged = mergeFindings(['17', '18']);
            if (!merged) throw new Error('mergeFindings returned null');
            applyMergeToDom(merged);

            const card = tab.querySelector('.finding-merged');
            if (!card) throw new Error('merged card not rendered');

            // Verdict radios: accepted AND rejected, named for the survivor.
            const radios = card.querySelectorAll('input[name="verdict-17"]');
            if (radios.length !== 2)
                throw new Error('expected 2 verdict radios, got ' + radios.length);
            const values = radios.map((r) => r.value).sort();
            if (JSON.stringify(values) !== JSON.stringify(['accepted', 'rejected']))
                throw new Error('verdict radio values wrong: ' + JSON.stringify(values));

            // Severity override control.
            if (!card.querySelector('.severity-select'))
                throw new Error('merged card has no severity-select');

            // Notes field.
            if (!card.querySelector('.notes textarea'))
                throw new Error('merged card has no notes textarea');

            // Screenshot inspect/annotate affordance.
            if (!card.querySelector('.annotation-container'))
                throw new Error('merged card has no annotation-container');
            """
        )
    )


def test_merged_card_verdict_can_change_to_rejected() -> None:
    """The auto-accepted survivor is not locked: a reject reaches its state.

    RED before the fix: with no verdict radios on the merged card, the reviewer
    cannot change the verdict at all.
    """
    _run(
        textwrap.dedent(
            """
            reportState.findings = {
                '17': { verdict: 'accepted' },
                '18': { verdict: 'accepted' },
            };
            const merged = mergeFindings(['17', '18']);
            applyMergeToDom(merged);
            if (reportState.findings['17'].verdict !== 'accepted')
                throw new Error('precondition: survivor should start accepted');

            const card = tab.querySelector('.finding-merged');
            const rejectRadio = card.querySelector('input[value="rejected"]');
            if (!rejectRadio) throw new Error('no reject radio on merged card');
            rejectRadio.checked = true;

            // Drive the real event-delegation handler the live page wires up.
            handleChangeEvent({ target: rejectRadio });

            if (reportState.findings['17'].verdict !== 'rejected')
                throw new Error('survivor verdict did not change to rejected: '
                    + reportState.findings['17'].verdict);
            """
        )
    )
