"""unionStrings must guard against non-array inputs (MED, finding #8).

``unionStrings(lists)`` does ``for (const list of lists) for (const item of list)``.
Without an ``Array.isArray(list)`` guard:

  * a STRING list iterates per-character — each char IS a string, so it passes
    the ``typeof item !== 'string'`` filter and pollutes the union (char-soup);
  * a non-iterable list (number/object) throws ``TypeError`` and aborts the
    whole merge.

The fix skips any list that is not an array. This test drives the real
``unionStrings`` from ``review_app.js`` in a node sandbox with a mixed input
(string, null, number, real array) and asserts only the array is processed.
"""

from __future__ import annotations

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


def _run(driver_body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const findingsEl = {{ textContent: '[]' }};
        function makeEl() {{
            return {{
                className: '', textContent: '', hidden: false, style: {{}}, dataset: {{}},
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
                contains() {{ return true; }}, appendChild() {{}}, removeChild() {{}},
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
            Blob: class Blob {{ constructor() {{}} }},
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


def test_union_strings_ignores_non_array_inputs() -> None:
    """A mixed input (string, null, number, array) yields only the array's items.

    RED before the fix: the number 42 throws TypeError (non-iterable) and the
    string 'hello' char-soups into h/e/l/o.
    """
    _run(
        textwrap.dedent(
            """
            const result = unionStrings(['hello', null, undefined, 42, ['real', 'array'], 'WORLD']);
            const want = JSON.stringify(['real', 'array']);
            if (JSON.stringify(result) !== want)
                throw new Error('unionStrings mishandled non-array input: ' + JSON.stringify(result));
            """
        )
    )
