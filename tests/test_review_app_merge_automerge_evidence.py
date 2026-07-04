"""Round-7 P2: human-merge must preserve a member's auto-merged evidence.

When a reviewer human-merges a finding that was ALREADY auto-merged server-side
(it carries its own folded-away evidence in top-level ``merged_frames`` and its
provenance trail in nested ``unified_analysis.merged_from_ids``) and that finding
is absorbed (not the survivor), ``mergeFindingGroup`` only copied the absorbed
member's TOP-LEVEL ``merged_from_ids`` -- it dropped both the nested auto-merge
ancestry and the ``merged_frames`` evidence. The member card was the only
representative of those folded-away frames, so they vanished on save/export.

The fix unions every member's ``merged_frames`` into the new group and folds the
nested ``unified_analysis.merged_from_ids`` ancestry into the survivor's trail, so
no auto-merged evidence/provenance is lost. Executed in a node sandbox, mirroring
``tests/test_review_app_merge_inheritance.py``.
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


def _finding(fid: int, ts: float, summary: str, **extra: object) -> dict:
    base = {
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
    base.update(extra)
    return base


# Survivor 17 (earliest) absorbs member 18. Member 18 is ITSELF an auto-merged
# survivor: it folded away finding 99, whose evidence lives in 18's merged_frames
# and whose provenance lives in 18's unified_analysis.merged_from_ids.
_MEMBER_18 = _finding(18, 18.0, "absorbed auto-merged member")
_MEMBER_18["merged_frames"] = [
    {
        "id": 99,
        "timestamp_start": 99.0,
        "timestamp_formatted": "01:39",
        "text": "auto-folded evidence line",
        "screenshot": "shot99.jpg",
    }
]
_MEMBER_18["unified_analysis"]["merged_from_ids"] = [[99, 99.0]]

_FINDINGS = [
    _finding(6, 6.0, "standalone control finding"),
    _finding(17, 17.0, "survivor of the human merge"),
    _MEMBER_18,
]

_SEGMENTS = [{"start": 6.0, "text": "spoken line"}]

# Live UI merge: 17 absorbs 18.
_SETUP_LIVE = (
    "reportState.findings = {"
    " '6': { verdict: 'none' },"
    " '17': { verdict: 'none' },"
    " '18': { verdict: 'accepted' }"
    "};"
    " reportState.merges = ["
    "  { id: '17', member_ids: ['17', '18'], summary_override: null }"
    " ];"
)

# Disk reload: trail lives on merged_from_ids instead of reportState.merges.
_SETUP_DISK = (
    "reportState.findings = {"
    " '6': { verdict: 'none' },"
    " '17': { verdict: 'none', merged_from_ids: [18] },"
    " '18': { verdict: 'accepted' }"
    "};"
    " reportState.merges = [];"
)


def _run(driver_body: str, review_setup: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js merge-evidence tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findings = {json.dumps(_FINDINGS)};
        const segments = {json.dumps(_SEGMENTS)};

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
            URL: {{ createObjectURL() {{ return 'blob:x'; }}, revokeObjectURL() {{}} }},
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


_PRESERVE_ASSERTIONS = textwrap.dedent(
    """
    const data = buildReviewData();
    const survivor = data.findings.find((f) => String(f.id) === '17');
    if (!survivor) throw new Error('survivor 17 missing from fold');
    if (data.findings.some((f) => String(f.id) === '18'))
        throw new Error('absorbed member 18 leaked');

    // The auto-merged member's folded-away evidence frame must survive.
    const frames = survivor.merged_frames || [];
    if (!frames.some((m) => String(m.id) === '99'))
        throw new Error('auto-merged evidence frame (99) dropped on human merge: '
            + JSON.stringify(frames));

    // The nested auto-merge ancestry must survive in the trail.
    const trail = (survivor.unified_analysis || {}).merged_from_ids || [];
    const hasAncestry = trail.some((p) => String(Array.isArray(p) ? p[0] : p) === '99');
    if (!hasAncestry)
        throw new Error('nested auto-merge ancestry (99) dropped on human merge: '
            + JSON.stringify(trail));
    """
)


def test_human_merge_preserves_member_automerge_evidence_live() -> None:
    _run(_PRESERVE_ASSERTIONS, _SETUP_LIVE)


def test_human_merge_preserves_member_automerge_evidence_disk_reload() -> None:
    _run(_PRESERVE_ASSERTIONS, _SETUP_DISK)
