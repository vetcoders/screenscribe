"""W1-1 — JS-runtime canary for the ANALYZE dashboard (BH39).

The F0 harness only loads ``review_app.js``; the analyze dashboard has its own
script. This mirror loads ``analyze_dashboard.js`` (+ i18n + libs) in a node
``vm`` so we can drive ``analyzeMarker`` for real and assert its error surface.

BH39: ``/api/analyze/{id}`` returns HTTP 200 even when the VLM run failed
(``payload.status === 'error'``), and an HTTP-level failure was ignored too. The
pre-fix code always set the global status to "Ready", hiding both. The fix
surfaces an explicit failure status, mirroring deleteMarker/saveNote.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "screenscribe/html_pro_assets/scripts"
I18N_JS = SCRIPTS / "i18n.js"
LANGUAGE_CONTROL_JS = SCRIPTS / "lib/language-control.js"
STT_TRANSPORT_JS = SCRIPTS / "lib/stt-transport.js"
ANALYZE_DASHBOARD_JS = SCRIPTS / "analyze_dashboard.js"


def _run_analyze_dashboard_smoke(test_body: str) -> None:
    """Load analyze_dashboard.js (+ siblings) in a node vm and run test_body.

    test_body executes in the same lexical scope as analyze_dashboard.js, so its
    top-level declarations (analyzeMarker, refreshMarkers, t, ...) are visible
    directly. The dashboard's init runs behind a DOMContentLoaded listener, which
    the stub leaves unfired, so loading just defines functions. Signal failure
    with ``process.exitCode = 1``; an uncaught throw also fails the run.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the analyze-dashboard JS-runtime canary")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const sources = [
            {str(I18N_JS)!r},
            {str(LANGUAGE_CONTROL_JS)!r},
            {str(STT_TRANSPORT_JS)!r},
            {str(ANALYZE_DASHBOARD_JS)!r},
        ].map((p) => fs.readFileSync(p, 'utf8')).join("\\n");

        const sandbox = {{
            console,
            setTimeout, clearTimeout, setInterval, clearInterval,
            Math, Date, URL, URLSearchParams, JSON, Headers,
            location: {{ hash: '', search: '', href: 'http://localhost/', pathname: '/', origin: 'http://localhost' }},
            history: {{ replaceState() {{}} }},
            window: {{
                addEventListener() {{}},
                removeEventListener() {{}},
                setTimeout, clearTimeout, setInterval, clearInterval,
                matchMedia() {{ return {{ matches: false, addEventListener() {{}} }}; }},
            }},
            document: {{
                body: {{
                    dataset: {{ reportLanguage: 'en' }},
                    classList: {{ add() {{}}, remove() {{}}, toggle() {{}}, contains() {{ return false; }} }},
                    appendChild() {{}}, removeChild() {{}},
                }},
                documentElement: {{ lang: 'en' }},
                addEventListener() {{}},
                querySelector() {{ return null; }},
                querySelectorAll() {{ return []; }},
                getElementById() {{ return null; }},
                createElement() {{
                    return {{
                        className: '', textContent: '', hidden: false,
                        style: {{}}, dataset: {{}},
                        classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                        appendChild() {{}}, remove() {{}}, setAttribute() {{}},
                    }};
                }},
            }},
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            fetch() {{ throw new Error('unexpected fetch'); }},
            navigator: {{ mediaDevices: {{}} }},
            confirm() {{ return true; }},
            alert() {{}},
            process,
        }};
        sandbox.window.fetch = sandbox.fetch;
        sandbox.window.document = sandbox.document;
        sandbox.window.location = sandbox.location;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.window.localStorage = sandbox.localStorage;
        sandbox.globalThis = sandbox;

        const script = new vm.Script(
            sources + "\\n(async () => {{\\n" + {test_body!r} + "\\n}})()",
            {{ filename: 'analyze_dashboard.js' }},
        );
        (async () => {{
            const result = script.runInNewContext(sandbox);
            if (result && typeof result.then === 'function') {{
                await result;
            }}
        }})().catch((error) => {{
            console.error(error);
            process.exitCode = 1;
        }});
        """
    )
    runner_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", delete=False) as tmp:
            tmp.write(runner)
            runner_path = Path(tmp.name)
        result = subprocess.run(
            [node, str(runner_path)], capture_output=True, text=True, check=False
        )
    finally:
        if runner_path is not None:
            runner_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_smoke_analyze_dashboard_loads_without_referenceerror() -> None:
    """The dashboard script evaluates and exposes analyzeMarker."""
    _run_analyze_dashboard_smoke(
        """
        if (typeof analyzeMarker !== 'function') {
            console.error('analyzeMarker missing after load');
            process.exitCode = 1;
        }
        """
    )


def test_a7a_no_severity_renders_no_badge() -> None:
    """A7a: a marker with no severity renders no badge (was 'Bez ważności').

    formatMarkerSeverity returns '' for none/absent so the severity suffix
    collapses to nothing — analyze has no priority control, so advertising an
    absent severity the user cannot change is pure noise. A real severity still
    formats normally.
    """
    _run_analyze_dashboard_smoke(
        """
        if (formatMarkerSeverity('none') !== '') {
            console.error('expected empty for none, got: ' + formatMarkerSeverity('none'));
            process.exitCode = 1;
        }
        if (formatMarkerSeverity('') !== '' || formatMarkerSeverity(undefined) !== '') {
            console.error('expected empty for falsy severity');
            process.exitCode = 1;
        }
        if (formatMarkerSeverity('high') !== 'High') {
            console.error('expected High for real severity, got: ' + formatMarkerSeverity('high'));
            process.exitCode = 1;
        }
        """
    )


def test_bh39_http_error_surfaces_failure_not_ready() -> None:
    """!response.ok must show the failure status, never analyze.status_ready."""
    _run_analyze_dashboard_smoke(
        """
        refreshMarkers = async () => {};
        const statusEl = { textContent: '' };
        document.getElementById = (id) => (id === 'statusText' ? statusEl : null);

        fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });

        await analyzeMarker('m1');

        const failMsg = t('analyze.status_analyze_failed');
        const readyMsg = t('analyze.status_ready');
        if (statusEl.textContent === readyMsg) {
            console.error('HTTP error still reported Ready');
            process.exitCode = 1;
        }
        if (statusEl.textContent !== failMsg) {
            console.error('HTTP error did not show the failure status: ' + statusEl.textContent);
            process.exitCode = 1;
        }
        """
    )


def test_bh39_status_error_payload_surfaces_failure_not_ready() -> None:
    """HTTP 200 with payload.status==='error' must surface failure, not Ready."""
    _run_analyze_dashboard_smoke(
        """
        let refreshed = false;
        refreshMarkers = async () => { refreshed = true; };
        const statusEl = { textContent: '' };
        document.getElementById = (id) => (id === 'statusText' ? statusEl : null);

        fetch = async () => ({
            ok: true,
            status: 200,
            json: async () => ({ marker_id: 'm1', status: 'error', error: 'VLM exploded' }),
        });

        await analyzeMarker('m1');

        const failMsg = t('analyze.status_analyze_failed');
        const readyMsg = t('analyze.status_ready');
        if (statusEl.textContent === readyMsg) {
            console.error('VLM-error payload still reported Ready');
            process.exitCode = 1;
        }
        if (statusEl.textContent !== failMsg) {
            console.error('VLM-error payload did not show the failure status: ' + statusEl.textContent);
            process.exitCode = 1;
        }
        if (refreshed) {
            console.error('marker list was refreshed despite a failed analysis');
            process.exitCode = 1;
        }
        """
    )


def test_c54_export_gate_held_after_finalize_empties_markers() -> None:
    """C5.4: if markers empty during finalize, the finally must NOT re-open export.

    generateMarkdownReport()'s finally previously did a bare
    ``finalizeBtn.disabled = false``, which re-enabled export even on an empty
    session. The fix re-derives the gate via ``updateExportGate(currentMarkers)``.
    """
    _run_analyze_dashboard_smoke(
        """
        // Single source of marker truth: start non-empty, emptied by finalize.
        currentMarkers = [{ marker_id: 'm1' }];

        const statusEl = { textContent: '' };
        const finalizeBtn = { disabled: false };
        const exportJsonBtn = { disabled: false };
        const exportGateHint = { hidden: true };
        const byId = {
            statusText: statusEl,
            finalizeBtn: finalizeBtn,
            exportJsonBtn: exportJsonBtn,
            exportGateHint: exportGateHint,
        };
        document.getElementById = (id) => (id in byId ? byId[id] : null);

        // Markers get wiped out during the long finalize job.
        refreshMarkers = async () => { currentMarkers = []; };
        downloadBlob = () => {};
        hideFinalizeProgress = () => {};
        updateFinalizeProgress = () => {};

        fetch = async (url) => {
            if (String(url).indexOf('/api/finalize/start') !== -1) {
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({ job_id: 'job-1', status: 'completed', completed: 0, errors: 0 }),
                };
            }
            if (String(url).indexOf('/api/report/markdown') !== -1) {
                return { ok: true, status: 200, blob: async () => ({}) };
            }
            throw new Error('unexpected fetch: ' + url);
        };

        await generateMarkdownReport();

        if (finalizeBtn.disabled !== true) {
            console.error('export NOT held: finalizeBtn re-enabled on empty session');
            process.exitCode = 1;
        }
        if (exportJsonBtn.disabled !== true) {
            console.error('export NOT held: exportJsonBtn re-enabled on empty session');
            process.exitCode = 1;
        }
        if (exportGateHint.hidden !== false) {
            console.error('export gate hint hidden despite empty session');
            process.exitCode = 1;
        }
        """
    )


def test_c54_export_gate_reenabled_when_markers_remain() -> None:
    """C5.4 happy path: markers still present after finalize -> export re-enabled."""
    _run_analyze_dashboard_smoke(
        """
        currentMarkers = [{ marker_id: 'm1' }];

        const statusEl = { textContent: '' };
        const finalizeBtn = { disabled: false };
        const exportJsonBtn = { disabled: false };
        const exportGateHint = { hidden: true };
        const byId = {
            statusText: statusEl,
            finalizeBtn: finalizeBtn,
            exportJsonBtn: exportJsonBtn,
            exportGateHint: exportGateHint,
        };
        document.getElementById = (id) => (id in byId ? byId[id] : null);

        // Markers remain after refresh.
        refreshMarkers = async () => { currentMarkers = [{ marker_id: 'm1' }]; };
        downloadBlob = () => {};
        hideFinalizeProgress = () => {};
        updateFinalizeProgress = () => {};

        fetch = async (url) => {
            if (String(url).indexOf('/api/finalize/start') !== -1) {
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({ job_id: 'job-1', status: 'completed', completed: 1, errors: 0 }),
                };
            }
            if (String(url).indexOf('/api/report/markdown') !== -1) {
                return { ok: true, status: 200, blob: async () => ({}) };
            }
            throw new Error('unexpected fetch: ' + url);
        };

        await generateMarkdownReport();

        if (finalizeBtn.disabled !== false) {
            console.error('export wrongly held despite remaining markers');
            process.exitCode = 1;
        }
        if (exportJsonBtn.disabled !== false) {
            console.error('exportJsonBtn wrongly held despite remaining markers');
            process.exitCode = 1;
        }
        if (exportGateHint.hidden !== true) {
            console.error('export gate hint shown despite remaining markers');
            process.exitCode = 1;
        }
        """
    )


def test_change_marker_severity_patches_and_refreshes() -> None:
    """A7b: changeMarkerSeverity PATCHes the marker with the chosen priority and
    refreshes the list so the override is reflected."""
    _run_analyze_dashboard_smoke(
        """
        let refreshed = false;
        refreshMarkers = async () => { refreshed = true; };
        const statusEl = { textContent: '' };
        document.getElementById = (id) => (id === 'statusText' ? statusEl : null);

        const calls = [];
        fetch = async (url, opts) => {
            calls.push({ url: String(url), opts: opts || {} });
            return { ok: true, status: 200, json: async () => ({}) };
        };

        await changeMarkerSeverity('m1', 'critical');

        if (calls.length !== 1) {
            console.error('expected exactly one PATCH, got ' + calls.length);
            process.exitCode = 1;
        }
        const call = calls[0] || { url: '', opts: {} };
        if (call.url.indexOf('/api/marker/m1') === -1 || call.opts.method !== 'PATCH') {
            console.error('did not PATCH /api/marker/m1: ' + JSON.stringify(call));
            process.exitCode = 1;
        }
        const body = JSON.parse(call.opts.body || '{}');
        if (body.severity !== 'critical') {
            console.error('PATCH body missing severity=critical: ' + call.opts.body);
            process.exitCode = 1;
        }
        if (!refreshed) {
            console.error('marker list not refreshed after priority change');
            process.exitCode = 1;
        }
        if (statusEl.textContent !== t('analyze.status_ready')) {
            console.error('status not set to ready: ' + statusEl.textContent);
            process.exitCode = 1;
        }
        """
    )


def test_bh39_completed_payload_reports_ready() -> None:
    """A genuine success (status==='completed') still ends on Ready and refreshes."""
    _run_analyze_dashboard_smoke(
        """
        let refreshed = false;
        refreshMarkers = async () => { refreshed = true; };
        const statusEl = { textContent: '' };
        document.getElementById = (id) => (id === 'statusText' ? statusEl : null);

        fetch = async () => ({
            ok: true,
            status: 200,
            json: async () => ({ marker_id: 'm1', status: 'completed', result: { summary: 'ok' } }),
        });

        await analyzeMarker('m1');

        if (statusEl.textContent !== t('analyze.status_ready')) {
            console.error('successful analysis did not end on Ready: ' + statusEl.textContent);
            process.exitCode = 1;
        }
        if (!refreshed) {
            console.error('successful analysis did not refresh the marker list');
            process.exitCode = 1;
        }
        """
    )


def _run_token_wrapper(setup_js: str, assert_js: str) -> None:
    """Evaluate ONLY the session-token wrapper IIFE from analyze_dashboard.js.

    Isolated from the full dashboard harness (no i18n / lib load) so we can drive
    the one thing under test: location.hash + sessionStorage at load time. The
    wrapper source is sliced from the real file, not duplicated — a regression
    that drops the sessionStorage recovery turns the reload test red. Signal
    failure with ``process.exitCode = 1``. Mirrors the review_app.js token test.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the analyze-dashboard token canary")

    src = ANALYZE_DASHBOARD_JS.read_text(encoding="utf-8")
    end = src.index("})();")  # the first IIFE in the file is the token wrapper
    wrapper = src[: end + len("})();")]
    assert "X-ScreenScribe-Token" in wrapper, "did not slice the token wrapper IIFE"

    runner = textwrap.dedent(
        f"""
        const vm = require('vm');
        const recorded = {{ calls: 0, headers: null }};
        const sessionStore = {{}};
        const sandbox = {{
            console, process, URL, URLSearchParams, Headers,
            sessionStorage: {{
                getItem(k) {{ return Object.prototype.hasOwnProperty.call(sessionStore, k) ? sessionStore[k] : null; }},
                setItem(k, v) {{ sessionStore[k] = String(v); }},
                removeItem(k) {{ delete sessionStore[k]; }},
            }},
            location: {{ hash: '', search: '', pathname: '/', origin: 'http://localhost', href: 'http://localhost/' }},
        }};
        sandbox.history = {{ replaceState() {{ sandbox.location.hash = ''; }} }};
        sandbox.window = sandbox;
        sandbox.globalThis = sandbox;
        sandbox.fetch = function (input, init) {{
            recorded.calls += 1;
            recorded.headers = (init && init.headers) || null;
            return Promise.resolve({{ ok: true, status: 200, json: async () => ({{}}) }});
        }};
        sandbox.__recorded = recorded;
        sandbox.__sessionStore = sessionStore;
        const code = {setup_js!r} + "\\n" + {wrapper!r} + "\\n" + {assert_js!r};
        try {{ vm.runInNewContext(code, sandbox); }}
        catch (e) {{ console.error(e); process.exitCode = 1; }}
        """
    )
    runner_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", encoding="utf-8", delete=False) as tmp:
            tmp.write(runner)
            runner_path = Path(tmp.name)
        result = subprocess.run(
            [node, str(runner_path)], capture_output=True, text=True, check=False
        )
    finally:
        if runner_path is not None:
            runner_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr or result.stdout


_READ_AUTH_HEADER_JS = (
    "window.fetch('/api/x', {});"
    "var tok = __recorded.headers && __recorded.headers.get"
    " && __recorded.headers.get('X-ScreenScribe-Token');"
)

# The analyze wrapper namespaces its sessionStorage key with ':analyze:' so a
# same-origin+path collision with the review report's token is impossible.
_ANALYZE_KEY = "screenscribe:token:analyze:http://localhost/"


def test_token_first_load_attaches_header_strips_hash_persists() -> None:
    """#token=abc on first load: fetch carries it, hash is stripped, token saved."""
    _run_token_wrapper(
        "location.hash = '#token=abc';",
        _READ_AUTH_HEADER_JS
        + f"""
        if (tok !== 'abc') {{ console.error('missing/wrong token header: ' + tok); process.exitCode = 1; }}
        if (location.hash !== '') {{ console.error('hash not stripped: ' + location.hash); process.exitCode = 1; }}
        if (__sessionStore[{_ANALYZE_KEY!r}] !== 'abc') {{
            console.error('token not persisted: ' + JSON.stringify(__sessionStore)); process.exitCode = 1;
        }}
        """,
    )


def test_token_reload_without_hash_recovers_from_session_storage() -> None:
    """Reload with no #token but a stored token: fetch still carries the token.

    This is the P1 bug (FW-02 A): stripping #token from the address bar must not
    strip it from the running session — else every /api/* call 403s after F5.
    """
    _run_token_wrapper(
        f"location.hash = ''; sessionStorage.setItem({_ANALYZE_KEY!r}, 'abc');",
        _READ_AUTH_HEADER_JS
        + """
        if (tok !== 'abc') { console.error('token not recovered from storage: ' + tok); process.exitCode = 1; }
        """,
    )


def test_token_no_hash_no_storage_leaves_fetch_unpatched() -> None:
    """No #token and no stored token: no patch, no token attached."""
    _run_token_wrapper(
        "location.hash = '';",
        _READ_AUTH_HEADER_JS
        + """
        if (tok) { console.error('token attached without any source: ' + tok); process.exitCode = 1; }
        """,
    )


def test_token_under_review_key_is_not_used_by_analyze() -> None:
    """A token stored under the review report's key is not used by analyze.

    The ':analyze:' namespace prevents a same-origin+path bleed between modes.
    """
    _run_token_wrapper(
        "location.hash = '';"
        " sessionStorage.setItem('screenscribe:token:http://localhost/', 'xyz');",
        _READ_AUTH_HEADER_JS
        + """
        if (tok) { console.error('used review-scoped token in analyze: ' + tok); process.exitCode = 1; }
        """,
    )
