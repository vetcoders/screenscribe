"""Restored merge groups must reach the DOM on hydration (L2, P2).

Fix #3 round-trips ``reportState.merges`` through the draft serializer and
``hydrateReportState`` so the fold survives in DATA. But hydration only calls
``restoreUIFromState`` / ``renderManualFrames`` / ``initAnnotationTools`` —
``applyMergeToDom`` fires ONLY on an explicit Merge click. So a draft-restore or
detached-window sync that carries an existing merge left the absorbed member
cards visible and never produced a merged summary card: the UI diverged from the
persisted data.

This test drives the real ``hydrateReportState`` from ``review_app.js`` against a
functional DOM (``tests/_review_dom.js``): it builds two server-rendered finding
cards, hydrates a snapshot whose ``merges`` folds them, and asserts the absorbed
member is hidden (``mergedAway``) AND a merged summary card appears — WITHOUT any
Merge click.
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
            // Add the merge checkboxes the server adds on init so applyMergeToDom
            // and getSelectedMergeIds see the same surface a live page has.
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


def test_hydration_applies_restored_merge_to_dom() -> None:
    """A persisted merge group folds the DOM on hydrate, with no Merge click.

    RED before the fix: hydrateReportState restores reportState.merges but never
    calls applyMergeToDom, so both original cards stay visible and no
    .finding-merged summary card exists.
    """
    _run(
        textwrap.dedent(
            """
            // A snapshot as persisted by a draft that already held a merge group.
            const snapshot = {
                reviewer: 'qa',
                modified: false,
                manualFrames: [],
                findings: {
                    '17': { verdict: 'accepted' },
                    '18': { verdict: 'none' },
                },
                merges: [
                    { id: 17, member_ids: ['17', '18'], summary_override: null },
                ],
            };

            hydrateReportState(snapshot);

            // The absorbed member (18) must be folded away.
            const member = tab.querySelectorAll('.finding')
                .find((a) => a.dataset.findingId === '18' && a.dataset.merged !== 'true');
            if (!member) throw new Error('member card 18 not found');
            if (member.dataset.mergedAway !== 'true' || member.hidden !== true)
                throw new Error('absorbed member 18 still visible after hydrate: '
                    + 'mergedAway=' + member.dataset.mergedAway + ' hidden=' + member.hidden);

            // A merged summary card must now exist for the survivor.
            const mergedCards = tab.querySelectorAll('.finding-merged');
            if (mergedCards.length !== 1)
                throw new Error('expected exactly 1 merged summary card, got ' + mergedCards.length);
            if (mergedCards[0].dataset.findingId !== '17')
                throw new Error('merged card has wrong survivor id: ' + mergedCards[0].dataset.findingId);
            """
        )
    )
