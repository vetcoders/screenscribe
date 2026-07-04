"""TODO-markdown export must not leak a stray [NONE] severity tag (G1).

`buildTodoMarkdown` used to fall through `overrideSev || review.severity ||
result.severity || 'low'`, so a manual frame whose effective priority was
'none' (reviewer cleared it, or a VLM result carrying 'none') rendered a
`[NONE]` tag in the exported TODO -- while the on-card badge and the ZIP
manifest (:2620) showed no priority at all. These tests run the real
`buildTodoMarkdown` in a node sandbox and pin that 'none' collapses to no tag,
consistent with the badge/manifest, and that a real priority still tags.
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


def _sandbox_prelude() -> str:
    return textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findingsEl = {{ textContent: JSON.stringify([]) }};

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
        """
    )


def _run_todo(review_setup: str, assertions: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js TODO-export tests")

    driver = textwrap.dedent(
        f"""
        const driver = `
            reportState.reviewer = 'qa';
            reportState.merges = [];
            reportState.findings = {{}};
            reportState.manualFrames = [];
            {review_setup}
            const md = buildTodoMarkdown([], 'demo.mp4', 'qa');
            {assertions}
        `;
        const script = new vm.Script(
            i18nSource + "\\n" + languageControlSource + "\\n" + sttTransportSource + "\\n" +
            tabKeyboardSource + "\\n" + source + "\\n" + driver,
            {{ filename: 'review_app.js' }}
        );
        script.runInNewContext(sandbox);
        """
    )
    runner = _sandbox_prelude() + driver
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


_FRAME_RESULT_NONE = [
    {
        "marker_id": "m1",
        "timestamp_formatted": "00:01",
        "transcript": "",
        "notes": "",
        "frameDataUrl": "data:image/png;base64,QUJD",
        "result": {"summary": "Layout glitch", "category": "ui", "severity": "none"},
    }
]


def test_todo_result_severity_none_renders_no_tag() -> None:
    """A manual frame whose VLM result severity is 'none' must not print [NONE];
    the item is still listed (no priority is not no finding)."""
    _run_todo(
        f"reportState.manualFrames = {json.dumps(_FRAME_RESULT_NONE)};",
        """
        if (md.includes('[NONE]'))
            throw new Error('TODO leaked a [NONE] severity tag: ' + md);
        if (!md.includes('Layout glitch'))
            throw new Error('no-priority manual frame was dropped from the TODO: ' + md);
        """,
    )


def test_helper_collapses_model_none_for_badge() -> None:
    """A frame WITHOUT an override whose VLM result severity is 'none' must
    collapse to '' in the shared helper itself -- the on-card badge and ZIP
    manifest read the helper directly, so a leaked 'none' would render a
    localized "NONE" badge against the "all three agree" contract. The existing
    TODO test passes through the redundant buildTodoMarkdown guard, so this pins
    the helper (badge path) directly, and re-checks the TODO carries no tag."""
    _run_todo(
        f"reportState.manualFrames = {json.dumps(_FRAME_RESULT_NONE)};",
        """
        const eff = manualFrameEffectiveSeverity(reportState.manualFrames[0]);
        if (eff !== '')
            throw new Error('helper leaked a model-provided none (badge would show NONE): ' + JSON.stringify(eff));
        if (md.includes('[NONE]'))
            throw new Error('TODO leaked a [NONE] severity tag: ' + md);
        if (!md.includes('Layout glitch'))
            throw new Error('no-priority manual frame was dropped from the TODO: ' + md);
        """,
    )


def test_todo_cleared_override_collapses_to_no_tag() -> None:
    """Reviewer explicitly cleared the priority (frame.severity='none') even
    though the VLM said 'high' -> the effective severity is none, so the TODO
    carries no severity tag (mirrors the badge / manifest), not [HIGH]/[NONE]."""
    frame = [
        {
            "marker_id": "m1",
            "timestamp_formatted": "00:01",
            "transcript": "",
            "notes": "",
            "frameDataUrl": "data:image/png;base64,QUJD",
            "severity": "none",
            "result": {"summary": "Layout glitch", "category": "ui", "severity": "high"},
        }
    ]
    _run_todo(
        f"reportState.manualFrames = {json.dumps(frame)};"
        " reportState.findings = { 'manual-m1': { severity: 'high' } };",
        """
        if (md.includes('[NONE]'))
            throw new Error('cleared-priority TODO leaked [NONE]: ' + md);
        if (md.includes('[HIGH]'))
            throw new Error('cleared-priority TODO leaked the stale [HIGH]: ' + md);
        if (!md.includes('Layout glitch'))
            throw new Error('cleared-priority manual frame was dropped: ' + md);
        """,
    )


def test_todo_real_priority_still_tags() -> None:
    """Positive control: a real reviewer override (frame.severity='high') keeps
    the [HIGH] tag so the collapse does not swallow genuine priorities."""
    frame = [
        {
            "marker_id": "m1",
            "timestamp_formatted": "00:01",
            "transcript": "",
            "notes": "",
            "frameDataUrl": "data:image/png;base64,QUJD",
            "severity": "high",
            "result": {"summary": "Layout glitch", "category": "ui", "severity": "low"},
        }
    ]
    _run_todo(
        f"reportState.manualFrames = {json.dumps(frame)};",
        """
        if (!md.includes('[HIGH]'))
            throw new Error('real priority override lost its [HIGH] tag: ' + md);
        """,
    )
