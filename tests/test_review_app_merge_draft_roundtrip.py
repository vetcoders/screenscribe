"""Draft persist/hydrate round-trip for human-merge groups (P2, finding #3).

``reportState.merges`` is the only place that holds unsaved human-merge groups
in the same session. The draft serializer (``buildPersistableState``) persisted
only findings/manualFrames/reviewer/modified, and ``hydrateReportState`` likewise
ignored ``merges``. So a reload / detached-window sync / draft-restore that
happened BEFORE an explicit Save dropped the merge groups entirely: absorbed
findings reappeared standalone and the fold was lost.

The fix round-trips ``merges`` through both functions. This test drives the real
``buildPersistableState`` + ``hydrateReportState`` from ``review_app.js`` in a
node sandbox: it builds a draft with a merge group, serializes it (as
localStorage would), hydrates a fresh state from the parsed snapshot, and proves
``reportState.merges`` survived AND that ``computeMergedFindings`` then folds the
group (so the absorbed member does not resurface).
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

        const findings = {json.dumps(_FINDINGS)};
        const findingsEl = {{ textContent: JSON.stringify(findings) }};

        function makeEl() {{
            return {{
                className: '', textContent: '', hidden: false, href: '', download: '',
                value: '', checked: false, style: {{}}, dataset: {{}},
                classList: {{ add() {{}}, remove() {{}}, toggle() {{}}, contains() {{ return false; }} }},
                appendChild() {{}}, removeChild() {{}}, remove() {{}}, click() {{}},
                querySelector() {{ return null; }}, querySelectorAll() {{ return []; }},
                addEventListener() {{}}, setAttribute() {{}}, insertBefore() {{}},
                forEach() {{}},
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
        sandbox.globalThis = sandbox;

        const i18nSource = fs.readFileSync({str(I18N_JS)!r}, 'utf8');
        const languageControlSource = fs.readFileSync({str(LANGUAGE_CONTROL_JS)!r}, 'utf8');
        const sttTransportSource = fs.readFileSync({str(STT_TRANSPORT_JS)!r}, 'utf8');
        const tabKeyboardSource = fs.readFileSync({str(TAB_KEYBOARD_JS)!r}, 'utf8');
        const source = fs.readFileSync({str(REVIEW_APP_JS)!r}, 'utf8');

        const driver = `
            reportState.reviewer = 'qa';
            reportState.manualFrames = [];
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


def test_draft_persist_hydrate_preserves_merge_groups() -> None:
    """A merge built in a draft survives a serialize -> hydrate round-trip.

    RED before the fix: buildPersistableState drops `merges` and
    hydrateReportState never restores it, so after hydrate
    computeMergedFindings folds nothing and the absorbed member resurfaces.
    """
    _run(
        textwrap.dedent(
            """
            // Build a draft with a merge group (string member ids, numeric survivor).
            reportState.findings = {
                '17': { verdict: 'accepted' },
                '18': { verdict: 'none' },
            };
            reportState.merges = [
                { id: '17', member_ids: ['17', '18'], summary_override: null },
            ];

            // Serialize exactly as the localStorage draft path does.
            const snapshot = JSON.parse(JSON.stringify(buildPersistableState(true)));
            if (!Array.isArray(snapshot.merges) || snapshot.merges.length !== 1)
                throw new Error('buildPersistableState dropped merges: ' + JSON.stringify(snapshot.merges));

            // Wipe runtime state, then hydrate from the parsed snapshot.
            reportState.findings = {};
            reportState.merges = [];
            hydrateReportState(snapshot);

            if (!Array.isArray(reportState.merges) || reportState.merges.length !== 1)
                throw new Error('hydrateReportState dropped merges: ' + JSON.stringify(reportState.merges));
            const m = reportState.merges[0];
            if (normId(m.id) !== '17' || JSON.stringify(m.member_ids) !== JSON.stringify(['17', '18']))
                throw new Error('merge group shape lost: ' + JSON.stringify(m));

            // The restored merge must actually fold: member 18 absorbed, survivor 17 kept once.
            const byId = {};
            const original = JSON.parse(document.getElementById('original-findings').textContent);
            for (const f of original) byId[f.id] = f;
            const { mergedList, memberIds } = computeMergedFindings(byId);
            if (mergedList.length !== 1)
                throw new Error('fold lost after hydrate: mergedList=' + mergedList.length);
            if (!memberIds.has('18'))
                throw new Error('absorbed member resurfaced after hydrate');
            """
        )
    )
