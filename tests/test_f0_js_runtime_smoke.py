"""F0 — JS-runtime canary for the HTML Pro review surface.

`make verify` proves a lot, but it never executes the report's JavaScript, so a
whole class of UI-runtime failures (ReferenceError on load, broken export paths)
stays invisible while the gate still says READY. This canary closes that blind
spot: it loads ``review_app.js`` in a node ``vm`` sandbox (the same harness shape
as ``test_review_app_voice_recorder.py``) and asserts the core review functions
behave — no real browser, no npm dependency, just a runtime witness.

Scope is deliberately narrow (F0 canary, not a full browser smoke):
  * the script loads without a top-level ReferenceError,
  * ``buildTodoMarkdown`` does not crash on a rejected finding (the F1 bug),
  * the reviewed export speaks ``human_review.verdict`` and never ``confirmed``,
  * a verdict change writes ``accepted`` into review state.

If this canary ever proves too shallow to catch a real runtime failure, the next
step is a jsdom-based smoke (F0b), not stubbing deeper here — a stub that fakes
too much would turn the witness into theatre.
"""

from __future__ import annotations

import os
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
TAB_KEYBOARD_JS = SCRIPTS / "lib/tab-keyboard.js"
REVIEW_APP_JS = SCRIPTS / "review_app.js"
VIDEO_PLAYER_JS = SCRIPTS / "video_player.js"
ANALYZE_DASHBOARD_JS = SCRIPTS / "analyze_dashboard.js"
JSZIP_MIN_JS = REPO_ROOT / "screenscribe/html_pro_assets/vendor/jszip.min.js"

# Truthy spellings GitHub Actions (and most CI) use for the ``CI`` env var.
_CI_TRUE = {"1", "true", "yes", "on"}


def _resolve_node() -> str:
    """Resolve the ``node`` binary — fail-closed everywhere (local AND CI).

    The F0 canary is a *runtime* gate: silently skipping it when node is absent
    would let a JS ReferenceError ship while the verifier still prints READY. A
    skip is a green-by-omission hole, so a missing node is ALWAYS a hard FAILURE
    — locally as well as under CI — never a skip. node is part of the browser /
    JS-runtime gate surface (PKG-4 C4.2); provision it (e.g. setup-node in CI,
    or install node for `make verify` locally). This is the P1-3/SYS-1
    fail-closed contract.
    """
    node = shutil.which("node")
    if node:
        return node
    where = "CI" if os.environ.get("CI", "").strip().lower() in _CI_TRUE else "local"
    pytest.fail(
        f"node is missing ({where}): the F0 JS-runtime canary must run, not skip "
        "(fail-closed gate — install node / provision setup-node)"
    )


def _run_review_app_smoke(test_body: str) -> None:
    """Load review_app.js (+ its sibling scripts) in a node vm and run test_body.

    test_body executes in the same lexical scope as review_app.js, so top-level
    declarations (buildTodoMarkdown, buildReviewData, handleChangeEvent,
    reportState, ...) are visible directly. Signal failure from JS by setting
    ``process.exitCode = 1``; an uncaught throw (e.g. a ReferenceError) also
    fails the run. The DOM stub is intentionally minimal — each test injects the
    little it needs (document.getElementById, dataset) rather than baking a fake
    DOM in here.
    """
    node = _resolve_node()

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const sources = [
            {str(I18N_JS)!r},
            {str(LANGUAGE_CONTROL_JS)!r},
            {str(STT_TRANSPORT_JS)!r},
            {str(TAB_KEYBOARD_JS)!r},
            {str(REVIEW_APP_JS)!r},
        ].map((p) => fs.readFileSync(p, 'utf8')).join("\\n");

        const sandbox = {{
            console,
            setTimeout,
            clearTimeout,
            setInterval,
            clearInterval,
            Math,
            Date,
            URL,
            URLSearchParams,
            JSON,
            window: {{
                location: {{ search: '', href: 'http://localhost/' }},
                addEventListener() {{}},
                removeEventListener() {{}},
                setTimeout,
                clearTimeout,
                setInterval,
                clearInterval,
                __screenscribeAllowProgrammaticClose: false,
            }},
            document: {{
                body: {{
                    dataset: {{ reportLanguage: 'en' }},
                    classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
                    contains() {{ return true; }},
                    appendChild() {{}},
                    removeChild() {{}},
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
            Blob,
            process,
        }};
        sandbox.window.document = sandbox.document;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.globalThis = sandbox;

        const script = new vm.Script(
            sources + "\\n(async () => {{\\n" + {test_body!r} + "\\n}})()",
            {{ filename: 'review_app.js' }},
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


def test_f0_review_app_loads_without_referenceerror() -> None:
    """The script evaluates and exposes the core review functions."""
    _run_review_app_smoke(
        """
        for (const name of ['buildTodoMarkdown', 'buildReviewData', 'handleChangeEvent', 'normalizeVerdict']) {
            if (typeof eval(name) !== 'function') {
                console.error('missing top-level function: ' + name);
                process.exitCode = 1;
            }
        }
        """
    )


def test_f0_export_todo_does_not_crash_on_rejected_finding() -> None:
    """buildTodoMarkdown must build the rejected section, not ReferenceError.

    F1: review_app.js:1129 references an undefined ``dict``; with a rejected
    finding present this path runs and throws, taking exportTodoList /
    exportReviewedZIP down with it.
    """
    _run_review_app_smoke(
        """
        reportState.findings = { f1: { verdict: 'rejected', severity: 'high', notes: 'false alarm' } };
        reportState.manualFrames = [];
        reportState.reviewer = 'tester';
        const findings = [
            { id: 'f1', timestamp_formatted: '0:01', text: 'boom',
              unified_analysis: { summary: 'boom', severity: 'high' } },
        ];
        const md = buildTodoMarkdown(findings, 'vid.mp4', 'tester');
        if (typeof md !== 'string' || !md.includes('vid.mp4')) {
            console.error('buildTodoMarkdown returned unexpected value: ' + md);
            process.exitCode = 1;
        }
        if (!md.includes('Rejected as false alarm') || !md.includes('boom') || !md.includes('false alarm')) {
            console.error('buildTodoMarkdown omitted rejected section details: ' + md);
            process.exitCode = 1;
        }
        """
    )


def test_f0_export_does_not_block_on_empty_reviewer() -> None:
    """R3: export must run with an empty reviewer name (reviewer is optional).

    exportReviewedJSON/ZIP/TodoList used to hard-gate on
    ``reportState.reviewer.trim()`` — notify + focus the name field + early
    return — so an empty name blocked export entirely. The name field is now
    optional; export must proceed regardless. Drives exportTodoList end-to-end
    and asserts the download anchor was clicked.
    """
    _run_review_app_smoke(
        """
        let clicked = false;
        document.body.dataset.videoName = 'vid.mp4';
        document.body.appendChild = () => {};
        document.body.removeChild = () => {};
        document.getElementById = (id) => id === 'original-findings'
            ? { textContent: JSON.stringify([
                { id: 'f1', timestamp_formatted: '0:01', unified_analysis: { summary: 's', severity: 'high' } },
              ]) }
            : { focus: () => {}, value: '' };
        document.createElement = () => ({ href: '', download: '', style: {}, click: () => { clicked = true; } });
        globalThis.URL = { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} };
        showNotification = () => {};
        reportState.findings = { f1: { verdict: 'accepted', notes: 'ok' } };
        reportState.manualFrames = [];
        reportState.reviewer = '';

        exportTodoList();

        if (!clicked) {
            console.error('export was blocked with an empty reviewer (download never triggered)');
            process.exitCode = 1;
        }
        """
    )


def test_f0_review_export_speaks_verdict_not_confirmed() -> None:
    """buildReviewData emits human_review.verdict and never legacy confirmed."""
    _run_review_app_smoke(
        """
        document.body.dataset.videoName = 'vid.mp4';
        document.getElementById = (id) => id === 'original-findings'
            ? { textContent: JSON.stringify([
                { id: 'f1', timestamp_formatted: '0:01', unified_analysis: { summary: 's', severity: 'high' } },
              ]) }
            : null;
        reportState.findings = { f1: { verdict: 'accepted', notes: 'ok' } };
        reportState.manualFrames = [];
        reportState.reviewer = 'tester';

        const data = buildReviewData();
        const f = data.findings[0];
        if (!f.human_review || f.human_review.verdict !== 'accepted') {
            console.error('expected human_review.verdict=accepted, got ' + JSON.stringify(f.human_review));
            process.exitCode = 1;
        }
        if ('confirmed' in f || ('confirmed' in (f.human_review || {}))) {
            console.error('legacy "confirmed" leaked into export payload');
            process.exitCode = 1;
        }
        """
    )


def test_f0_original_findings_embed_malformed_does_not_crash() -> None:
    """getOriginalFindingsList degrades to [] on a bad #original-findings embed.

    P3-05 (PrView PR #1): a corrupted or non-array server embed used to throw
    out of a raw ``JSON.parse(el.textContent)`` and abort viewer init. The guard
    routes every consumer through ``getOriginalFindingsList()`` (try/catch -> []
    + non-array guard). This pins that contract so a future un-guarding regresses
    here instead of silently crashing the report on load.
    """
    _run_review_app_smoke(
        """
        const make = (text) => (id) => id === 'original-findings' ? { textContent: text } : null;

        // 1) malformed JSON -> [] (not a throw)
        document.getElementById = make('{not: valid json,');
        let list = getOriginalFindingsList();
        if (!Array.isArray(list) || list.length !== 0) {
            console.error('malformed embed did not degrade to []: ' + JSON.stringify(list));
            process.exitCode = 1;
        }

        // 2) valid JSON but not an array -> [] (non-array guard)
        document.getElementById = make('{"id": 1}');
        list = getOriginalFindingsList();
        if (!Array.isArray(list) || list.length !== 0) {
            console.error('non-array embed did not degrade to []: ' + JSON.stringify(list));
            process.exitCode = 1;
        }

        // 3) empty textContent -> [] (not a throw)
        document.getElementById = make('');
        list = getOriginalFindingsList();
        if (!Array.isArray(list) || list.length !== 0) {
            console.error('empty embed did not degrade to []: ' + JSON.stringify(list));
            process.exitCode = 1;
        }

        // 4) well-formed array -> preserved (guard is not over-eager)
        document.getElementById = make(JSON.stringify([{ id: 'f1' }, { id: 'f2' }]));
        list = getOriginalFindingsList();
        if (!Array.isArray(list) || list.length !== 2 || list[0].id !== 'f1') {
            console.error('well-formed embed was not preserved: ' + JSON.stringify(list));
            process.exitCode = 1;
        }
        """
    )


def test_f0_fast_array_loads_player_dashboard_and_jszip() -> None:
    """Fast canary over the *rest* of the served JS runtime surface (P2-13).

    review_app.js gets the deep canary above. This is a single, fast node-vm
    load that witnesses the report's other runtime scripts — the shared
    ``video_player.js``, the ``analyze_dashboard.js`` dashboard, and the
    vendored ``jszip.min.js`` the ZIP export depends on — evaluate their
    top-level scope without a ReferenceError or a redeclaration collision, and
    expose their entry points (``JSZip`` constructible + ``generateAsync``;
    ``ScreenScribePlayer`` defined). The DOMContentLoaded bootstraps are
    registered, not fired, so no real DOM is needed. This consolidates the JS
    load-witness for the whole served surface into one place rather than leaving
    video_player/analyze_dashboard/JSZip with no runtime gate at all.
    """
    node = _resolve_node()

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        // JSZip first so its global is present, then the two report scripts.
        const sources = [
            {str(JSZIP_MIN_JS)!r},
            {str(VIDEO_PLAYER_JS)!r},
            {str(ANALYZE_DASHBOARD_JS)!r},
        ].map((p) => fs.readFileSync(p, 'utf8')).join("\\n;\\n");

        const noop = () => {{}};
        const classList = {{ add: noop, remove: noop, toggle: noop, contains: () => false }};
        const makeEl = () => ({{
            className: '', textContent: '', hidden: false, value: '', disabled: false,
            dataset: {{}}, style: {{}}, classList,
            addEventListener: noop, removeEventListener: noop,
            appendChild: noop, removeChild: noop, remove: noop, setAttribute: noop,
            getBoundingClientRect: () => ({{ width: 0 }}),
        }});
        const sandbox = {{
            console, process, Math, Date, JSON, URL, URLSearchParams,
            setTimeout, clearTimeout, setInterval, clearInterval,
            navigator: {{ userAgent: 'node', mediaDevices: {{}} }},
            localStorage: {{ getItem: () => null, setItem: noop, removeItem: noop }},
            location: {{ hash: '', search: '', href: 'http://localhost/', pathname: '/', origin: 'http://localhost' }},
            history: {{ replaceState: noop }},
            document: {{
                body: {{ classList, dataset: {{}} }},
                documentElement: {{ lang: 'en' }},
                addEventListener: noop, removeEventListener: noop,
                querySelector: () => null, querySelectorAll: () => [],
                getElementById: () => null, createElement: () => makeEl(),
            }},
            fetch: () => Promise.reject(new Error('no fetch in the load canary')),
        }};
        sandbox.window = sandbox;
        sandbox.self = sandbox;
        sandbox.globalThis = sandbox;

        // Exercise the entry points the served report actually relies on.
        const probe = `
            if (typeof JSZip !== 'function') {{
                throw new Error('JSZip global not exported by the vendored bundle');
            }}
            const zip = new JSZip();
            zip.file('todo.md', 'hello');
            if (typeof zip.generateAsync !== 'function') {{
                throw new Error('JSZip instance is missing generateAsync (ZIP export would break)');
            }}
            if (typeof ScreenScribePlayer !== 'function') {{
                throw new Error('video_player.js did not define ScreenScribePlayer');
            }}
        `;

        try {{
            vm.runInNewContext(sources + "\\n" + probe, sandbox, {{ filename: 'fast-array.js' }});
        }} catch (error) {{
            console.error(error);
            process.exitCode = 1;
        }}
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


def test_f3_draft_payload_omits_frame_base64() -> None:
    """The localStorage draft must not carry frameDataUrl (the quota bomb).

    Frame pixels live on the server (/api/review-state); localStorage is a
    lightweight draft of decisions only.
    """
    _run_review_app_smoke(
        """
        stateSyncRuntime.draftKey = 'screenscribe_draft_test';
        stateSyncRuntime.sourceId = 'src1';
        reportState.findings = { f1: { verdict: 'accepted', notes: 'ok' } };
        reportState.manualFrames = [{
            marker_id: 'm1', timestamp: 1, transcript: 't', notes: 'n',
            frameDataUrl: 'data:image/png;base64,' + 'A'.repeat(200),
        }];
        reportState.reviewer = 'tester';
        reportState.modified = true;

        let captured = null;
        localStorage.setItem = (k, v) => { if (k === 'screenscribe_draft_test') captured = v; };
        saveDraft();

        if (captured === null) {
            console.error('saveDraft wrote no draft'); process.exitCode = 1;
        } else if (captured.includes('frameDataUrl') || captured.includes('data:image')) {
            console.error('draft still carries frame base64: ' + captured.slice(0, 120));
            process.exitCode = 1;
        }
        """
    )


def test_f3_quota_exceeded_keeps_verdicts_and_warns() -> None:
    """QuotaExceededError must not crash or silently drop the reviewer's work."""
    _run_review_app_smoke(
        """
        stateSyncRuntime.draftKey = 'screenscribe_draft_test';
        stateSyncRuntime.sourceId = 'src1';
        reportState.findings = { f1: { verdict: 'accepted', notes: 'keep me' } };
        reportState.manualFrames = [];
        reportState.reviewer = 'tester';
        reportState.modified = true;

        let warnings = 0;
        showNotification = () => { warnings += 1; };
        localStorage.setItem = () => {
            const err = new Error('quota'); err.name = 'QuotaExceededError'; throw err;
        };
        saveDraft();

        if (reportState.findings.f1.verdict !== 'accepted'
            || reportState.findings.f1.notes !== 'keep me') {
            console.error('verdict/notes lost on quota'); process.exitCode = 1;
        }
        if (warnings === 0) {
            console.error('no soft warning shown when the draft could not be saved');
            process.exitCode = 1;
        }
        """
    )


def test_f3_local_draft_reload_enriches_manual_frame_image_from_server() -> None:
    """A stripped local draft keeps verdicts/notes while server restores pixels."""
    _run_review_app_smoke(
        """
        document.body.dataset.reportId = 'reload';
        let renderCount = 0;
        renderManualFrames = () => { renderCount += 1; };
        initAnnotationTools = () => {};
        restoreUIFromState = () => {};
        activateTab = () => {};
        applyWindowMode = () => {};
        setInterval = () => 0;
        window.setInterval = setInterval;

        const localDraft = {
            sourceId: 'previous-window',
            state: {
                findings: {
                    f1: { verdict: 'accepted', notes: 'local verdict notes' },
                    manual_m1: { verdict: 'accepted', notes: 'local manual notes' },
                },
                manualFrames: [{
                    marker_id: 'm1',
                    timestamp: 1.5,
                    timestamp_formatted: '00:01.500',
                    transcript: 'local transcript',
                    notes: 'local frame notes',
                    annotations: [{ type: 'arrow', x1: 0.1 }],
                }],
                reviewer: 'local reviewer',
                modified: true,
            },
        };
        const stored = {
            screenscribe_draft_reload: JSON.stringify(localDraft),
        };
        localStorage.getItem = (key) => stored[key] || null;

        let fetchedUrl = null;
        fetch = async (url) => {
            fetchedUrl = url;
            return {
                ok: true,
                json: async () => ({
                    reviewer: 'server reviewer',
                    modified: false,
                    findings: {
                        f1: { verdict: 'rejected', notes: 'server verdict notes' },
                    },
                    manualFrames: [{
                        marker_id: 'm1',
                        frameDataUrl: 'data:image/png;base64,SERVERIMAGE',
                        notes: 'server frame notes',
                        annotations: [{ type: 'rect', x: 0.5 }],
                    }],
                }),
            };
        };

        initReviewState();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const frame = reportState.manualFrames[0];
        if (fetchedUrl !== '/api/review-state') {
            console.error('expected review-state fetch after local restore, got ' + fetchedUrl);
            process.exitCode = 1;
        }
        if (frame.frameDataUrl !== 'data:image/png;base64,SERVERIMAGE') {
            console.error('server frameDataUrl was not merged into stripped local draft');
            process.exitCode = 1;
        }
        if (frame.notes !== 'local frame notes' || frame.annotations[0].type !== 'arrow') {
            console.error('server manual frame overwrote local frame fields: ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        if (reportState.findings.f1.verdict !== 'accepted'
            || reportState.findings.f1.notes !== 'local verdict notes'
            || reportState.reviewer !== 'local reviewer'
            || reportState.modified !== true) {
            console.error('server hydrate overwrote local draft state: ' + JSON.stringify(reportState));
            process.exitCode = 1;
        }
        if (renderCount < 2) {
            console.error('manual frames were not re-rendered after enrichment');
            process.exitCode = 1;
        }
        const persisted = JSON.stringify(buildPersistableState(reportState.modified));
        if (persisted.includes('frameDataUrl') || persisted.includes('data:image')) {
            console.error('enriched image leaked back into persistable localStorage state: ' + persisted);
            process.exitCode = 1;
        }
        """
    )


def test_f4_restore_uses_newer_sync_state_over_stale_draft() -> None:
    """When draft+sync both exist, the fresher savedAt envelope wins."""
    _run_review_app_smoke(
        """
        reportState.reportId = 'priority';
        initAnnotationTools = () => {};
        restoreUIFromState = () => {};
        renderManualFrames = () => {};
        activateTab = () => {};

        const draftEnvelope = {
            savedAt: '2026-06-17T12:00:00.000Z',
            state: {
                findings: { f1: { verdict: 'accepted', notes: 'stale draft' } },
                manualFrames: [{ marker_id: 'deleted-frame', notes: 'stale frame' }],
                reviewer: 'draft reviewer',
                modified: false,
            },
        };
        const syncEnvelope = {
            savedAt: '2026-06-17T12:01:00.000Z',
            state: {
                findings: { f1: { verdict: 'rejected', notes: 'fresh sync' } },
                manualFrames: [],
                reviewer: 'sync reviewer',
                modified: true,
            },
        };
        const stored = {
            screenscribe_draft_priority: JSON.stringify(draftEnvelope),
            screenscribe_state_priority: JSON.stringify(syncEnvelope),
        };
        localStorage.getItem = (key) => stored[key] || null;

        const restored = initSharedStateSync();

        if (!restored) {
            console.error('expected local review state to restore');
            process.exitCode = 1;
        }
        if (reportState.findings.f1.verdict !== 'rejected'
            || reportState.findings.f1.notes !== 'fresh sync'
            || reportState.reviewer !== 'sync reviewer'
            || reportState.modified !== true) {
            console.error('newer sync state did not win: ' + JSON.stringify(reportState));
            process.exitCode = 1;
        }
        if (reportState.manualFrames.length !== 0) {
            console.error('stale draft resurrected deleted manual frames: ' + JSON.stringify(reportState.manualFrames));
            process.exitCode = 1;
        }
        """
    )


def test_f4_restore_uses_newer_draft_state_over_stale_sync() -> None:
    """The savedAt comparison is symmetric: a fresher draft still wins."""
    _run_review_app_smoke(
        """
        reportState.reportId = 'prioritydraft';
        initAnnotationTools = () => {};
        restoreUIFromState = () => {};
        renderManualFrames = () => {};
        activateTab = () => {};

        const draftEnvelope = {
            savedAt: '2026-06-17T12:02:00.000Z',
            state: {
                findings: { f1: { verdict: 'accepted', notes: 'fresh draft' } },
                manualFrames: [{ marker_id: 'kept-frame', notes: 'fresh frame' }],
                reviewer: 'draft reviewer',
                modified: true,
            },
        };
        const syncEnvelope = {
            savedAt: '2026-06-17T12:01:00.000Z',
            state: {
                findings: { f1: { verdict: 'rejected', notes: 'stale sync' } },
                manualFrames: [],
                reviewer: 'sync reviewer',
                modified: false,
            },
        };
        const stored = {
            screenscribe_draft_prioritydraft: JSON.stringify(draftEnvelope),
            screenscribe_state_prioritydraft: JSON.stringify(syncEnvelope),
        };
        localStorage.getItem = (key) => stored[key] || null;

        const restored = initSharedStateSync();

        if (!restored) {
            console.error('expected local review state to restore');
            process.exitCode = 1;
        }
        if (reportState.findings.f1.verdict !== 'accepted'
            || reportState.findings.f1.notes !== 'fresh draft'
            || reportState.reviewer !== 'draft reviewer'
            || reportState.modified !== true) {
            console.error('newer draft state did not win: ' + JSON.stringify(reportState));
            process.exitCode = 1;
        }
        if (reportState.manualFrames.length !== 1
            || reportState.manualFrames[0].marker_id !== 'kept-frame') {
            console.error('newer draft manual frame was not restored: ' + JSON.stringify(reportState.manualFrames));
            process.exitCode = 1;
        }
        """
    )


def test_f4_manual_frame_add_flushes_draft_synchronously() -> None:
    """A freshly added manual frame lands in the localStorage draft immediately.

    Regression: the add path used the 120ms debounced sync, so a reload right
    after capturing a frame (e.g. while rejecting an AI finding) could persist a
    snapshot that never included the new frame — and the enrich-only reload path
    would not re-add it, so the manual frame silently vanished.
    """
    _run_review_app_smoke(
        """
        document.body.dataset.reportId = 'addflush';
        renderManualFrames = () => {};
        initAnnotationTools = () => {};
        restoreUIFromState = () => {};
        activateTab = () => {};
        applyWindowMode = () => {};
        setInterval = () => 0;
        window.setInterval = setInterval;

        const stored = {};
        localStorage.getItem = (key) => stored[key] || null;
        localStorage.setItem = (key, value) => { stored[key] = value; };

        fetch = async () => ({
            ok: true,
            json: async () => ({ findings: {}, manualFrames: [], reviewer: '', modified: false }),
        });

        initReviewState();
        await new Promise((resolve) => setTimeout(resolve, 0));

        // Inspect the draft WITHOUT advancing any timer: the pre-fix debounced
        // sync would not have written yet.
        upsertManualFrame({
            marker_id: 'm9',
            timestamp: 3,
            timestamp_formatted: '00:03.000',
            notes: 'fresh frame',
            frameDataUrl: 'data:image/png;base64,FRESH',
            result: null,
        });

        const draft = stored['screenscribe_state_addflush'];
        if (!draft) {
            console.error('add did not flush shared state synchronously');
            process.exitCode = 1;
        } else {
            const parsed = JSON.parse(draft);
            const ids = (parsed.state.manualFrames || []).map((f) => f.marker_id);
            if (!ids.includes('m9')) {
                console.error('freshly added frame missing from flushed draft: ' + JSON.stringify(ids));
                process.exitCode = 1;
            }
            if (draft.includes('frameDataUrl') || draft.includes('data:image')) {
                console.error('add flush leaked base64 into the localStorage draft');
                process.exitCode = 1;
            }
        }
        """
    )


def test_f4_manual_frame_delete_removes_frame_and_purges_state() -> None:
    """Deleting a manual frame drops it from state + draft and tells the server.

    The kept frame and its review row survive; the deleted frame's review row is
    purged; the flushed draft excludes it (and still carries no base64); and the
    server session is asked to forget it so a cold reload cannot resurrect it.
    """
    _run_review_app_smoke(
        """
        document.body.dataset.reportId = 'del';
        let renderCount = 0;
        renderManualFrames = () => { renderCount += 1; };
        initAnnotationTools = () => {};
        restoreUIFromState = () => {};
        activateTab = () => {};
        applyWindowMode = () => {};
        setInterval = () => 0;
        window.setInterval = setInterval;
        confirm = () => true;
        window.confirm = confirm;

        const stored = {};
        localStorage.getItem = (key) => stored[key] || null;
        localStorage.setItem = (key, value) => { stored[key] = value; };
        localStorage.removeItem = (key) => { delete stored[key]; };

        let deleteCall = null;
        fetch = async (url, opts) => {
            if (opts && opts.method === 'DELETE') {
                deleteCall = { url, method: opts.method };
                return { ok: true, json: async () => ({ status: 'deleted' }) };
            }
            return {
                ok: true,
                json: async () => ({ findings: {}, manualFrames: [], reviewer: '', modified: false }),
            };
        };

        initReviewState();
        await new Promise((resolve) => setTimeout(resolve, 0));

        reportState.manualFrames = [
            { marker_id: 'a', timestamp: 1, timestamp_formatted: '00:01.000', notes: 'keep-a', frameDataUrl: 'data:image/png;base64,AAA' },
            { marker_id: 'b', timestamp: 2, timestamp_formatted: '00:02.000', notes: 'drop-b', frameDataUrl: 'data:image/png;base64,BBB' },
        ];
        reportState.findings['manual-a'] = { verdict: 'accepted', notes: 'row-a' };
        reportState.findings['manual-b'] = { verdict: 'accepted', notes: 'row-b' };

        await deleteManualFrame('b');

        if (reportState.manualFrames.length !== 1 || reportState.manualFrames[0].marker_id !== 'a') {
            console.error('delete did not remove frame b from state: ' + JSON.stringify(reportState.manualFrames));
            process.exitCode = 1;
        }
        if (reportState.findings['manual-b'] !== undefined) {
            console.error('delete left an orphaned review row for manual-b');
            process.exitCode = 1;
        }
        if (reportState.findings['manual-a'] === undefined) {
            console.error('delete wrongly removed the review row for the kept frame a');
            process.exitCode = 1;
        }
        if (!deleteCall || deleteCall.url !== '/api/manual-mark/b' || deleteCall.method !== 'DELETE') {
            console.error('server delete not called correctly: ' + JSON.stringify(deleteCall));
            process.exitCode = 1;
        }
        const draft = stored['screenscribe_state_del'];
        if (!draft) {
            console.error('shared state was not flushed after delete');
            process.exitCode = 1;
        } else {
            const parsed = JSON.parse(draft);
            const ids = (parsed.state.manualFrames || []).map((f) => f.marker_id);
            if (ids.includes('b') || !ids.includes('a')) {
                console.error('flushed draft has the wrong frames after delete: ' + JSON.stringify(ids));
                process.exitCode = 1;
            }
            if (draft.includes('frameDataUrl') || draft.includes('data:image')) {
                console.error('delete flush leaked base64 into the localStorage draft');
                process.exitCode = 1;
            }
        }
        if (renderCount < 1) {
            console.error('renderManualFrames was not called after delete');
            process.exitCode = 1;
        }
        """
    )


def test_f4_manual_frame_delete_cancel_keeps_frame() -> None:
    """Declining the delete confirmation leaves the frame and skips the server."""
    _run_review_app_smoke(
        """
        document.body.dataset.reportId = 'delcancel';
        renderManualFrames = () => {};
        initAnnotationTools = () => {};
        restoreUIFromState = () => {};
        activateTab = () => {};
        applyWindowMode = () => {};
        setInterval = () => 0;
        window.setInterval = setInterval;
        confirm = () => false;
        window.confirm = confirm;

        const stored = {};
        localStorage.getItem = (key) => stored[key] || null;
        localStorage.setItem = (key, value) => { stored[key] = value; };

        let deleteCalled = false;
        fetch = async (url, opts) => {
            if (opts && opts.method === 'DELETE') { deleteCalled = true; }
            return {
                ok: true,
                json: async () => ({ findings: {}, manualFrames: [], reviewer: '', modified: false }),
            };
        };

        initReviewState();
        await new Promise((resolve) => setTimeout(resolve, 0));

        reportState.manualFrames = [
            { marker_id: 'a', timestamp: 1, timestamp_formatted: '00:01.000', notes: 'keep-a' },
        ];
        reportState.findings['manual-a'] = { verdict: 'accepted', notes: 'row-a' };

        await deleteManualFrame('a');

        if (reportState.manualFrames.length !== 1) {
            console.error('cancelled delete still removed the frame');
            process.exitCode = 1;
        }
        if (reportState.findings['manual-a'] === undefined) {
            console.error('cancelled delete still purged the review row');
            process.exitCode = 1;
        }
        if (deleteCalled) {
            console.error('cancelled delete still called the server');
            process.exitCode = 1;
        }
        """
    )


def test_f4_voice_status_no_success_without_transcript() -> None:
    """Voice status reports no-speech (not success) when the recognizer returns
    no text, and keeps the success status when transcript text actually arrives.
    """
    _run_review_app_smoke(
        """
        let capturedOpts = null;
        window.ScreenScribeLib = window.ScreenScribeLib || {};
        window.ScreenScribeLib.createSttTransport = (opts) => {
            capturedOpts = opts;
            return {
                start: async () => true,
                stop: () => {},
                destroy: () => {},
                releaseStreamTracks: () => {},
                isRecording: false,
                mediaRecorder: null,
                stream: null,
            };
        };

        const statuses = [];
        const recorder = new ReviewVoiceRecorder(
            () => {},
            (message, tone) => statuses.push({ message, tone })
        );

        // Empty take: the recognizer returns no text but the transport still
        // fires its generic ready/success — the wrapper must downgrade it.
        capturedOpts.onRecordingStart();
        capturedOpts.onTranscript('');
        capturedOpts.onStatus('Speech added to notes', 'success');

        const emptyFinal = statuses[statuses.length - 1];
        if (emptyFinal.tone === 'success') {
            console.error('empty transcript still reported success: ' + JSON.stringify(emptyFinal));
            process.exitCode = 1;
        }
        if (!/no speech/i.test(emptyFinal.message)) {
            console.error('empty transcript status was not a no-speech message: ' + JSON.stringify(emptyFinal));
            process.exitCode = 1;
        }

        // Real take: transcript text arrives, the success status is preserved.
        statuses.length = 0;
        capturedOpts.onRecordingStart();
        capturedOpts.onTranscript('there is a bug here');
        capturedOpts.onStatus('Speech added to notes', 'success');

        const okFinal = statuses[statuses.length - 1];
        if (okFinal.tone !== 'success' || okFinal.message !== 'Speech added to notes') {
            console.error('non-empty transcript lost its success status: ' + JSON.stringify(okFinal));
            process.exitCode = 1;
        }
        """
    )


def test_f0_verdict_change_updates_review_state() -> None:
    """A verdict radio change writes the verdict into reportState + the article."""
    _run_review_app_smoke(
        """
        reportState.findings = {};
        const article = { dataset: { findingId: 'f1' } };
        const target = {
            closest: (sel) => (sel === '.finding' ? article : null),
            matches: (sel) => sel === 'input[type="radio"]',
            name: 'verdict-f1',
            value: 'accepted',
        };
        handleChangeEvent({ target });
        if (reportState.findings['f1'] && reportState.findings['f1'].verdict !== 'accepted') {
            console.error('reportState verdict not set: ' + JSON.stringify(reportState.findings['f1']));
            process.exitCode = 1;
        }
        if (!reportState.findings['f1']) {
            console.error('reportState finding f1 was never created');
            process.exitCode = 1;
        }
        if (article.dataset.verdict !== 'accepted') {
            console.error('article dataset.verdict not set: ' + article.dataset.verdict);
            process.exitCode = 1;
        }
        """
    )


def _run_token_wrapper(setup_js: str, assert_js: str) -> None:
    """Evaluate ONLY the session-token wrapper IIFE from review_app.js.

    Isolated from the full review_app harness (the shared sandbox stays
    untouched) so we can drive the one thing under test: location.hash +
    sessionStorage at load time. The wrapper source is sliced from the real
    file, not duplicated. Signal failure with ``process.exitCode = 1``.
    """
    node = _resolve_node()

    src = REVIEW_APP_JS.read_text(encoding="utf-8")
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
            location: {{ hash: '', search: '', pathname: '/report.html', origin: 'http://localhost', href: 'http://localhost/report.html' }},
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


def test_f0_token_first_load_attaches_header_strips_hash_persists() -> None:
    """#token=abc on first load: fetch carries it, hash is stripped, token saved."""
    _run_token_wrapper(
        "location.hash = '#token=abc';",
        _READ_AUTH_HEADER_JS
        + """
        if (tok !== 'abc') { console.error('missing/wrong token header: ' + tok); process.exitCode = 1; }
        if (location.hash !== '') { console.error('hash not stripped: ' + location.hash); process.exitCode = 1; }
        if (__sessionStore['screenscribe:token:http://localhost/report.html'] !== 'abc') {
            console.error('token not persisted: ' + JSON.stringify(__sessionStore)); process.exitCode = 1;
        }
        """,
    )


def test_f0_token_reload_without_hash_recovers_from_session_storage() -> None:
    """Reload with no #token but a stored token: fetch still carries the token."""
    _run_token_wrapper(
        "location.hash = '';"
        " sessionStorage.setItem('screenscribe:token:http://localhost/report.html', 'abc');",
        _READ_AUTH_HEADER_JS
        + """
        if (tok !== 'abc') { console.error('token not recovered from storage: ' + tok); process.exitCode = 1; }
        """,
    )


def test_f0_token_no_hash_no_storage_leaves_fetch_unpatched() -> None:
    """No #token and no stored token: no patch, no token attached (today's path)."""
    _run_token_wrapper(
        "location.hash = '';",
        _READ_AUTH_HEADER_JS
        + """
        if (tok) { console.error('token attached without any source: ' + tok); process.exitCode = 1; }
        """,
    )


def test_f0_token_under_other_pathname_is_not_used() -> None:
    """A token stored under a different pathname's key is not used here."""
    _run_token_wrapper(
        "location.hash = ''; location.pathname = '/report.html';"
        " sessionStorage.setItem('screenscribe:token:http://localhost/other.html', 'xyz');",
        _READ_AUTH_HEADER_JS
        + """
        if (tok) { console.error('used a token scoped to another pathname: ' + tok); process.exitCode = 1; }
        """,
    )


def test_f0_reviewed_json_export_strips_manual_frame_base64() -> None:
    """The reviewed JSON download must not carry the base64 manual-frame image.

    buildReviewData spreads the whole frame (incl. frameDataUrl); the download is
    the lightweight record like AI findings, so the export strips it (the image
    belongs in the ZIP export). Save-to-disk/report.json is a separate path and
    is intentionally untouched here.
    """
    _run_review_app_smoke(
        """
        document.body.dataset.videoName = 'vid.mp4';
        reportState.reviewer = 'tester';
        reportState.findings = {};
        reportState.manualFrames = [{
            marker_id: 'm1', timestamp: 1, timestamp_formatted: '0:01',
            transcript: 'spoken', notes: 'a note', result: null,
            frameDataUrl: 'data:image/jpeg;base64,QUJDREVG',
        }];

        showNotification = () => {};
        document.getElementById = (id) => {
            if (id === 'original-findings') return { textContent: '[]' };
            if (id === 'reviewer-name') return { value: 'tester', focus() {} };
            return null;
        };
        document.createElement = () => ({ href: '', download: '', click() {} });

        let captured = null;
        URL.createObjectURL = (blob) => { captured = blob; return 'blob:x'; };
        URL.revokeObjectURL = () => {};

        await exportReviewedJSON();

        if (!captured) {
            console.error('export produced no blob');
            process.exitCode = 1;
        } else {
            const text = await captured.text();
            if (text.includes('data:image') || text.includes('frameDataUrl')) {
                console.error('reviewed JSON leaked the base64 manual-frame image');
                process.exitCode = 1;
            }
            const mf = (JSON.parse(text).manual_frames || [])[0] || {};
            if (mf.marker_id !== 'm1' || mf.notes !== 'a note' || mf.transcript !== 'spoken') {
                console.error('export dropped the light manual-frame fields: ' + JSON.stringify(mf));
                process.exitCode = 1;
            }
        }
        """
    )


def test_f0_hydrate_preserves_manual_frame_image_on_lightweight_snapshot() -> None:
    """A lightweight hydrate (cross-window storage sync) must not wipe the image.

    frameDataUrl is a heavy, intentionally non-persisted field restored from the
    server. A `storage` event delivers the frame list without the image; a
    wholesale replace dropped it (broken image, empty ZIP). Hydrate now preserves
    the in-memory image by marker_id when the incoming frame doesn't carry one —
    while the incoming snapshot still decides which frames exist (no resurrection).
    """
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        restoreUIFromState = () => {};
        initAnnotationTools = () => {};

        reportState.manualFrames = [
            { marker_id: 'm1', timestamp: 1, notes: 'keep', frameDataUrl: 'data:image/jpeg;base64,KEEPME' },
            { marker_id: 'm2', timestamp: 2, notes: 'two', frameDataUrl: 'data:image/jpeg;base64,TWO' },
        ];

        // Incoming lightweight snapshot: no images, and m2 was deleted elsewhere.
        hydrateReportState({ manualFrames: [{ marker_id: 'm1', timestamp: 1, notes: 'keep' }], findings: {}, reviewer: '' });

        const frames = reportState.manualFrames;
        if (frames.length !== 1 || frames[0].marker_id !== 'm1') {
            console.error('snapshot must decide the frame set (m2 stays deleted): ' + JSON.stringify(frames.map(f => f.marker_id)));
            process.exitCode = 1;
        }
        if (frames[0].frameDataUrl !== 'data:image/jpeg;base64,KEEPME') {
            console.error('lightweight hydrate wiped the in-memory image: ' + JSON.stringify(frames[0].frameDataUrl));
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_empty_transcript_placeholder_not_saved() -> None:
    """The "no spoken description" placeholder must never be saved as transcript.

    setManualFrameTranscript writes the ``media.manualFrameNoSpoken`` placeholder
    into #manualFrameTranscript and tags it ``.empty`` when the user recorded /
    typed nothing. The save path reads that element's textContent, so without a
    guard the placeholder string leaked into the persisted transcript (and the
    server session). addManualFrame must send an empty transcript instead.
    """
    _run_review_app_smoke(
        """
        upsertManualFrame = () => {};
        renderManualFrames = () => {};
        closeManualFrameModal = () => {};
        showNotification = () => {};
        setManualFrameStatus = () => {};

        manualFrameRuntime.currentFrame = {
            timestamp: 1, frameBase64: 'B64', frameDataUrl: 'data:image/png;base64,B64',
        };

        const placeholder = t('media.manualFrameNoSpoken');
        const els = {
            manualFrameTranscript: { textContent: placeholder, classList: { contains: (c) => c === 'empty' } },
            manualFrameNotes: { value: '' },
            manualFrameAddBtn: { disabled: false },
        };
        document.getElementById = (id) => (id in els ? els[id] : null);

        let capturedBody = null;
        fetch = async (url, opts) => {
            capturedBody = JSON.parse((opts && opts.body) || '{}');
            return { ok: true, status: 200, json: async () => ({ marker_id: 'm1' }) };
        };

        await addManualFrame();

        if (capturedBody === null) {
            console.error('manual-mark was never called'); process.exitCode = 1;
        } else if (capturedBody.transcript !== '') {
            console.error('placeholder leaked into saved transcript: ' + JSON.stringify(capturedBody.transcript));
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_real_transcript_is_saved() -> None:
    """A genuinely typed/spoken transcript (no .empty class) is saved verbatim."""
    _run_review_app_smoke(
        """
        upsertManualFrame = () => {};
        renderManualFrames = () => {};
        closeManualFrameModal = () => {};
        showNotification = () => {};
        setManualFrameStatus = () => {};

        manualFrameRuntime.currentFrame = {
            timestamp: 1, frameBase64: 'B64', frameDataUrl: 'data:image/png;base64,B64',
        };

        const els = {
            manualFrameTranscript: { textContent: 'there is a real bug here', classList: { contains: () => false } },
            manualFrameNotes: { value: '' },
            manualFrameAddBtn: { disabled: false },
        };
        document.getElementById = (id) => (id in els ? els[id] : null);

        let capturedBody = null;
        fetch = async (url, opts) => {
            capturedBody = JSON.parse((opts && opts.body) || '{}');
            return { ok: true, status: 200, json: async () => ({ marker_id: 'm1' }) };
        };

        await addManualFrame();

        if (!capturedBody || capturedBody.transcript !== 'there is a real bug here') {
            console.error('real transcript was not saved verbatim: ' + JSON.stringify(capturedBody));
            process.exitCode = 1;
        }
        """
    )


def test_f0_hydrate_incoming_image_wins_over_current() -> None:
    """When the incoming snapshot carries its own frameDataUrl, it wins."""
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        restoreUIFromState = () => {};
        initAnnotationTools = () => {};

        reportState.manualFrames = [{ marker_id: 'm1', frameDataUrl: 'data:image/jpeg;base64,OLD' }];
        hydrateReportState({ manualFrames: [{ marker_id: 'm1', frameDataUrl: 'data:image/jpeg;base64,NEW' }], findings: {}, reviewer: '' });

        if (reportState.manualFrames[0].frameDataUrl !== 'data:image/jpeg;base64,NEW') {
            console.error('incoming image should win over the in-memory one: ' + JSON.stringify(reportState.manualFrames[0].frameDataUrl));
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_edit_note_persists_and_keeps_transcript() -> None:
    """R12: editing a manual-frame note PATCHes the new note to the server and
    keeps it in local state, preserving the transcript verbatim.

    saveManualNote reads the card's textarea and routes through
    updateManualFrameMarker, so the edit travels the same durable path a fresh
    capture's note does: PATCH /api/manual-mark/{id} plus a local upsert. The
    transcript must not be clobbered — only the note changes.
    """
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        initAnnotationTools = () => {};
        flushSharedStateSync = () => {};

        reportState.manualFrames = [
            { marker_id: 'm1', timestamp: 1, transcript: 'spoken words', notes: 'old note', frameDataUrl: 'data:image/png;base64,AAA' },
        ];

        const els = { 'manual-note-input-m1': { value: 'edited note' } };
        document.getElementById = (id) => (id in els ? els[id] : null);

        let patchCall = null;
        fetch = async (url, opts) => {
            patchCall = { url, method: opts && opts.method, body: JSON.parse((opts && opts.body) || '{}') };
            return { ok: true, status: 200, json: async () => ({ marker_id: 'm1' }) };
        };

        await saveManualNote('m1');

        if (!patchCall || patchCall.method !== 'PATCH' || patchCall.url !== '/api/manual-mark/m1') {
            console.error('note edit did not PATCH the marker: ' + JSON.stringify(patchCall));
            process.exitCode = 1;
        } else if (patchCall.body.notes !== 'edited note') {
            console.error('PATCH did not carry the edited note: ' + JSON.stringify(patchCall.body));
            process.exitCode = 1;
        } else if (patchCall.body.transcript !== 'spoken words') {
            console.error('note edit clobbered the transcript: ' + JSON.stringify(patchCall.body));
            process.exitCode = 1;
        }

        const frame = reportState.manualFrames.find((f) => f.marker_id === 'm1');
        if (!frame || frame.notes !== 'edited note') {
            console.error('local state note was not updated: ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        if (!frame || frame.transcript !== 'spoken words') {
            console.error('local state transcript was clobbered: ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_no_summary_placeholder_is_visually_muted() -> None:
    """R13: the "no AI summary" fallback renders as an empty state, not as content.

    A frame with no analysis result must render its summary body with the
    ``empty`` class (muted+italic), so the placeholder never masquerades as a
    real AI summary. A frame WITH a summary keeps a plain body (no empty class).
    """
    _run_review_app_smoke(
        """
        bindThumbnailClicks = () => {};
        initAnnotationTools = () => {};
        // escapeHtml relies on a real DOM (createElement/innerHTML) the vm stub
        // does not provide; make it an identity so we can assert on the markup.
        escapeHtml = (v) => String(v == null ? '' : v);

        let listHtml = '';
        const list = { set innerHTML(v) { listHtml = v; }, get innerHTML() { return listHtml; }, replaceChildren() {} };
        const section = { hidden: false };
        const count = { textContent: '' };
        const els = { manualFindingsSection: section, manualFindingsList: list, manualFindingsCount: count };
        document.getElementById = (id) => (id in els ? els[id] : null);

        reportState.manualFrames = [
            { marker_id: 'pending', timestamp: 1, timestamp_formatted: '00:01.000', frameDataUrl: 'data:image/png;base64,AAA', result: null },
            { marker_id: 'done', timestamp: 2, timestamp_formatted: '00:02.000', frameDataUrl: 'data:image/png;base64,BBB', result: { summary: 'a real AI summary', severity: 'high' } },
        ];

        renderManualFrames();

        // The placeholder body must carry the empty modifier and the noSummary text.
        if (!/manual-frame-body empty/.test(listHtml)) {
            console.error('no-summary placeholder is not tagged .empty: ' + listHtml);
            process.exitCode = 1;
        }
        const placeholderText = t('review.noSummary');
        if (!listHtml.includes(placeholderText)) {
            console.error('no-summary placeholder text missing: ' + listHtml);
            process.exitCode = 1;
        }
        // The genuine summary must render in a plain (non-empty) body.
        if (!/manual-frame-body">a real AI summary/.test(listHtml)) {
            console.error('real summary should render in a plain body: ' + listHtml);
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_priority_select_reflects_override_and_hides_badge_on_none() -> None:
    """R14: the per-card priority <select> shows the effective priority, and the
    explicit 'none' override clears the badge (mirroring Analyze).

    Override wins over the VLM result severity; 'none' hides the badge entirely.
    """
    _run_review_app_smoke(
        """
        bindThumbnailClicks = () => {};
        initAnnotationTools = () => {};
        escapeHtml = (v) => String(v == null ? '' : v);

        let listHtml = '';
        const list = { set innerHTML(v) { listHtml = v; }, get innerHTML() { return listHtml; }, replaceChildren() {} };
        const els = { manualFindingsSection: { hidden: false }, manualFindingsList: list, manualFindingsCount: { textContent: '' } };
        document.getElementById = (id) => (id in els ? els[id] : null);

        reportState.manualFrames = [
            // override 'critical' wins over the VLM 'medium'
            { marker_id: 'ovr', timestamp: 1, timestamp_formatted: '00:01', frameDataUrl: 'x', severity: 'critical', result: { summary: 's', severity: 'medium' } },
            // override 'none' clears the badge even though the VLM said 'high'
            { marker_id: 'cleared', timestamp: 2, timestamp_formatted: '00:02', frameDataUrl: 'x', severity: 'none', result: { summary: 's', severity: 'high' } },
        ];

        renderManualFrames();

        // A severity <select> must exist per card, carrying the marker id.
        if (!/manual-frame-severity-select/.test(listHtml) || !/data-manual-marker-id="ovr"/.test(listHtml)) {
            console.error('priority select missing: ' + listHtml);
            process.exitCode = 1;
        }
        // Override 'critical' selected + badge shows critical.
        if (!/<option value="critical" selected>/.test(listHtml)) {
            console.error('override critical not selected in the select: ' + listHtml);
            process.exitCode = 1;
        }
        if (!/manual-frame-badge severity-critical/.test(listHtml)) {
            console.error('override critical badge not rendered: ' + listHtml);
            process.exitCode = 1;
        }
        // The VLM medium must NOT drive a badge when the override says critical.
        if (/severity-medium/.test(listHtml)) {
            console.error('stale VLM severity leaked into the badge: ' + listHtml);
            process.exitCode = 1;
        }
        // 'none' override: option selected, but NO badge for the high VLM value.
        if (!/<option value="none" selected>/.test(listHtml)) {
            console.error("'none' override not selected: " + listHtml);
            process.exitCode = 1;
        }
        if (/severity-high/.test(listHtml)) {
            console.error("'none' override should hide the badge, not show 'high': " + listHtml);
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_change_severity_patches_and_mirrors_state() -> None:
    """R14: changing priority PATCHes {severity} and, on success, updates local
    state (override on the frame + mirrored onto any existing result)."""
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        flushSharedStateSync = () => {};
        showNotification = () => {};

        reportState.manualFrames = [
            { marker_id: 'm1', timestamp: 1, result: { summary: 's', severity: 'high' } },
        ];

        let patchCall = null;
        fetch = async (url, opts) => {
            patchCall = { url, method: opts && opts.method, body: JSON.parse((opts && opts.body) || '{}') };
            return { ok: true, status: 200, json: async () => ({ marker_id: 'm1', severity: 'low' }) };
        };

        await changeManualFrameSeverity('m1', 'low');

        if (!patchCall || patchCall.method !== 'PATCH' || patchCall.url !== '/api/manual-mark/m1') {
            console.error('severity change did not PATCH: ' + JSON.stringify(patchCall));
            process.exitCode = 1;
        } else if (patchCall.body.severity !== 'low') {
            console.error('PATCH did not carry the severity: ' + JSON.stringify(patchCall.body));
            process.exitCode = 1;
        }
        const frame = reportState.manualFrames.find((f) => f.marker_id === 'm1');
        if (!frame || frame.severity !== 'low') {
            console.error('override not applied to frame: ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        if (!frame || frame.result.severity !== 'low') {
            console.error('override not mirrored onto the result: ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_change_severity_failure_notifies_and_no_desync() -> None:
    """R14 / NEW-06: a rejected priority PATCH (4xx/5xx) must NOT apply locally —
    a fail-silent local upsert desyncs the client from the server. The reviewer
    is notified instead."""
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        flushSharedStateSync = () => {};
        let notified = 0;
        showNotification = () => { notified += 1; };

        reportState.manualFrames = [
            { marker_id: 'm1', timestamp: 1, result: { summary: 's', severity: 'high' } },
        ];

        fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });

        await changeManualFrameSeverity('m1', 'low');

        const frame = reportState.manualFrames.find((f) => f.marker_id === 'm1');
        if (!frame || frame.severity !== undefined) {
            console.error('rejected severity was applied locally (desync): ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        if (frame.result.severity !== 'high') {
            console.error('rejected severity mutated the result: ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        if (notified < 1) {
            console.error('reviewer was not notified of the failed priority change');
            process.exitCode = 1;
        }
        """
    )


def test_f0_manual_frame_note_patch_rejection_does_not_desync() -> None:
    """NEW-06: a 4xx/5xx on the note PATCH must NOT upsert the edit locally (that
    silently desyncs the card from the marker); the reviewer is notified."""
    _run_review_app_smoke(
        """
        renderManualFrames = () => {};
        flushSharedStateSync = () => {};
        let notified = 0;
        showNotification = () => { notified += 1; };

        reportState.manualFrames = [
            { marker_id: 'm1', timestamp: 1, transcript: 'spoken', notes: 'old note' },
        ];

        fetch = async () => ({ ok: false, status: 422, json: async () => ({}) });

        await updateManualFrameMarker('m1', 'spoken', 'edited note');

        const frame = reportState.manualFrames.find((f) => f.marker_id === 'm1');
        if (!frame || frame.notes !== 'old note') {
            console.error('rejected note edit was applied locally (desync): ' + JSON.stringify(frame));
            process.exitCode = 1;
        }
        if (notified < 1) {
            console.error('reviewer was not notified of the failed note save');
            process.exitCode = 1;
        }
        """
    )
