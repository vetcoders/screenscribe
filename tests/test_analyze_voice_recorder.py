"""Analyze dashboard VoiceRecorder lifecycle tests."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._in_script = True
            self.scripts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and self.scripts:
            self.scripts[-1] += data


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "test_video.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config(language: str = "en") -> ScreenScribeConfig:
    return ScreenScribeConfig(
        **{"api" + "_key": "test-key"},
        **{"vision_api" + "_key": "test-key"},
        language=language,
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
    )


def _get_analyze_script(sample_video: Path, config: ScreenScribeConfig) -> str:
    app = create_analyze_app(sample_video, config)
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200

    parser = _ScriptCollector()
    parser.feed(response.text)
    dashboard_script = ""
    for script in parser.scripts:
        if "class VoiceRecorder" in script and "class FrameMarker" in script:
            dashboard_script = script
    if dashboard_script:
        return "\n".join(parser.scripts)
    raise AssertionError("Analyze VoiceRecorder script not found")


def _run_analyze_script_test(script: str, test_body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for analyze VoiceRecorder lifecycle tests")

    runner = textwrap.dedent(
        f"""
        const vm = require('vm');

        function makeElement(id) {{
            return {{
                id,
                textContent: '',
                value: '',
                hidden: false,
                dataset: {{}},
                style: {{ setProperty() {{}} }},
                classList: {{
                    add() {{}},
                    remove() {{}},
                    toggle() {{}},
                    contains() {{ return false; }},
                }},
                addEventListener() {{}},
                setAttribute() {{}},
                getAttribute() {{ return null; }},
                appendChild() {{}},
                replaceChildren() {{}},
                querySelector() {{ return null; }},
                querySelectorAll() {{ return []; }},
                getBoundingClientRect() {{ return {{ width: 320 }}; }},
            }};
        }}

        const elements = new Map();
        const getElement = (id) => {{
            if (!elements.has(id)) elements.set(id, makeElement(id));
            return elements.get(id);
        }};

        const sandbox = {{
            console,
            setTimeout,
            clearTimeout,
            Math,
            Date,
            URL,
            process,
            alert() {{}},
            confirm() {{ return true; }},
            getComputedStyle() {{ return {{ getPropertyValue() {{ return '320'; }} }}; }},
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            window: {{
                location: {{ search: '' }},
                innerWidth: 1200,
                addEventListener() {{}},
                removeEventListener() {{}},
                matchMedia() {{ return {{ matches: false }}; }},
            }},
            document: {{
                body: {{
                    dataset: {{ defaultLang: 'en' }},
                    classList: {{ add() {{}}, remove() {{}} }},
                }},
                documentElement: {{ lang: 'en', style: {{ setProperty() {{}} }}, setAttribute() {{}} }},
                getElementById: getElement,
                querySelector() {{ return null; }},
                querySelectorAll() {{ return []; }},
                createElement(tag) {{
                    const el = makeElement(tag);
                    el.getContext = () => ({{ drawImage() {{}} }});
                    el.toDataURL = () => 'data:image/jpeg;base64,abc';
                    return el;
                }},
                addEventListener() {{}},
            }},
            navigator: {{ mediaDevices: {{}} }},
            Blob: class Blob {{
                constructor(chunks, options = {{}}) {{
                    this.chunks = chunks;
                    this.type = options.type || '';
                }}
            }},
            FormData: class FormData {{
                constructor() {{ this.entries = []; }}
                append(...args) {{ this.entries.push(args); }}
            }},
            __fetchImpl: async (url) => {{
                if (url === '/api/markers') return {{ ok: true, status: 200, async json() {{ return []; }} }};
                return {{ ok: true, status: 200, async json() {{ return {{}}; }} }};
            }},
        }};
        sandbox.window.document = sandbox.document;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.fetch = (...args) => sandbox.__fetchImpl(...args);
        sandbox.globalThis = sandbox;

        const script = new vm.Script({script!r} + "\\n" + {test_body!r}, {{
            filename: 'analyze-inline.js',
        }});
        script.runInNewContext(sandbox);
        """
    )
    # Write the runner JS to a temp file instead of passing it via `node -e`.
    # A sufficiently long `-e` argument trips ENAMETOOLONG (errno 36) on Linux,
    # which fails CI on ubuntu/py3.13. A `.cjs` file forces CommonJS regardless
    # of any ambient package.json `"type": "module"`.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", delete=False, encoding="utf-8"
    ) as runner_file:
        runner_file.write(runner)
        runner_path = runner_file.name
    try:
        result = subprocess.run([node, runner_path], capture_output=True, text=True, check=False)
    finally:
        Path(runner_path).unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_analyze_voice_recorder_stop_releases_audio_track(sample_video: Path) -> None:
    script = _get_analyze_script(sample_video, _config())
    _run_analyze_script_test(
        script,
        """
        (async () => {
            let stopCalls = 0;
            const track = { stop() { stopCalls += 1; } };
            const stream = { getAudioTracks() { return [track]; } };

            navigator.mediaDevices.getUserMedia = async () => stream;
            __fetchImpl = async (url) => {
                if (url === '/api/stt') {
                    return { ok: true, status: 200, async json() { return { text: 'notatka' }; } };
                }
                return { ok: true, status: 200, async json() { return []; } };
            };

            class FakeMediaRecorder {
                constructor() {
                    this.ondataavailable = null;
                    this.onstop = null;
                    FakeMediaRecorder.instances.push(this);
                }
                start() {}
                stop() {
                    this.ondataavailable?.({ data: { size: 4096 } });
                    this.stopPromise = Promise.resolve(this.onstop?.());
                }
            }
            FakeMediaRecorder.instances = [];
            MediaRecorder = FakeMediaRecorder;

            const recorder = new VoiceRecorder(() => {});
            const started = await recorder.start();
            if (!started) throw new Error('recorder did not start');
            recorder.recordingStartedAt = Date.now() - 1500;

            recorder.stop();
            await FakeMediaRecorder.instances[0].stopPromise;

            if (stopCalls !== 1) throw new Error(`expected track.stop once, got ${stopCalls}`);
            if (recorder.stream !== null) throw new Error('stream was not cleared');
            const status = document.getElementById('statusText').textContent;
            if (!String(status).includes('Microphone off')) {
                throw new Error(`expected microphone-off status, got ${status}`);
            }
        })().catch((error) => {
            console.error(error.stack || error.message);
            process.exitCode = 1;
        });
        """,
    )


def test_analyze_voice_recorder_short_tap_does_not_call_stt(sample_video: Path) -> None:
    script = _get_analyze_script(sample_video, _config())
    _run_analyze_script_test(
        script,
        """
        (async () => {
            const stream = { getAudioTracks() { return [{ stop() {} }]; } };
            navigator.mediaDevices.getUserMedia = async () => stream;
            let sttCalls = 0;
            __fetchImpl = async (url) => {
                if (url === '/api/stt') {
                    sttCalls += 1;
                    return { ok: true, status: 200, async json() { return { text: 'should not happen' }; } };
                }
                return { ok: true, status: 200, async json() { return []; } };
            };

            class FakeMediaRecorder {
                constructor() {
                    this.ondataavailable = null;
                    this.onstop = null;
                    FakeMediaRecorder.instances.push(this);
                }
                start() {}
                stop() {
                    this.ondataavailable?.({ data: { size: 128 } });
                    this.stopPromise = Promise.resolve(this.onstop?.());
                }
            }
            FakeMediaRecorder.instances = [];
            MediaRecorder = FakeMediaRecorder;

            const recorder = new VoiceRecorder(() => {});
            if (!await recorder.start()) throw new Error('recorder did not start');

            recorder.stop();
            await FakeMediaRecorder.instances[0].stopPromise;

            if (sttCalls !== 0) throw new Error(`expected no STT call, got ${sttCalls}`);
            const status = document.getElementById('statusText').textContent;
            if (!String(status).includes('Hold to record longer')) {
                throw new Error(`expected hold-longer status, got ${status}`);
            }
        })().catch((error) => {
            console.error(error.stack || error.message);
            process.exitCode = 1;
        });
        """,
    )


def test_analyze_voice_recorder_ignores_double_start_while_recording(
    sample_video: Path,
) -> None:
    script = _get_analyze_script(sample_video, _config())
    _run_analyze_script_test(
        script,
        """
        (async () => {
            const stream = { getAudioTracks() { return [{ stop() {} }]; } };
            let getUserMediaCalls = 0;
            navigator.mediaDevices.getUserMedia = async () => {
                getUserMediaCalls += 1;
                return stream;
            };

            class FakeMediaRecorder {
                constructor() {
                    FakeMediaRecorder.instances.push(this);
                    this.ondataavailable = null;
                    this.onstop = null;
                }
                start() { this.started = true; }
                stop() { this.onstop?.(); }
            }
            FakeMediaRecorder.instances = [];
            MediaRecorder = FakeMediaRecorder;

            const recorder = new VoiceRecorder(() => {});
            if (!await recorder.start()) throw new Error('first start failed');
            if (await recorder.start()) throw new Error('second start should be ignored');

            if (getUserMediaCalls !== 1) {
                throw new Error(`expected one getUserMedia call, got ${getUserMediaCalls}`);
            }
            if (FakeMediaRecorder.instances.length !== 1) {
                throw new Error(`expected one MediaRecorder, got ${FakeMediaRecorder.instances.length}`);
            }
        })().catch((error) => {
            console.error(error.stack || error.message);
            process.exitCode = 1;
        });
        """,
    )


def test_analyze_format_marker_category_capitalizes_words(sample_video: Path) -> None:
    """P2-5: \\b\\w must be a real word-boundary regex, not a dead double-escaped one.

    With the double-escaped form the capitalization replace never matched, so a
    category like "user_action" stayed lowercase. The fix (`/\\b\\w/g`) must now
    upper-case the first letter of every space-separated word.
    """
    script = _get_analyze_script(sample_video, _config())
    _run_analyze_script_test(
        script,
        """
        (() => {
            const got = formatMarkerCategory('layout_issue');
            if (got !== 'Layout Issue') {
                throw new Error(`expected "Layout Issue", got "${got}"`);
            }
            const sev = formatMarkerSeverity('medium_high');
            if (sev !== 'Medium High') {
                throw new Error(`expected "Medium High", got "${sev}"`);
            }
        })();
        """,
    )


def test_analyze_mark_current_frame_throws_on_http_error(sample_video: Path) -> None:
    """P2-7: markCurrentFrame must check response.ok and surface a failure.

    A non-2xx /api/mark response previously sailed past straight into
    response.json(); the contract now is a thrown Error so the caller can show a
    failure status instead of silently marking or crashing on a parse.
    """
    script = _get_analyze_script(sample_video, _config())
    _run_analyze_script_test(
        script,
        """
        (async () => {
            __fetchImpl = async (url) => {
                if (url === '/api/mark') {
                    return { ok: false, status: 500, async json() { return {}; } };
                }
                return { ok: true, status: 200, async json() { return []; } };
            };

            const video = { videoWidth: 320, videoHeight: 240, currentTime: 1.5 };
            const marker = new FrameMarker(video);

            let threw = false;
            try {
                await marker.markCurrentFrame('t', 'n');
            } catch (error) {
                threw = true;
            }
            if (!threw) {
                throw new Error('markCurrentFrame did not throw on HTTP 500');
            }
        })().catch((error) => {
            console.error(error.stack || error.message);
            process.exitCode = 1;
        });
        """,
    )


def test_analyze_marker_actions_use_data_attributes_not_inline_onclick(
    sample_video: Path,
) -> None:
    """P2-9: marker ids / frame URLs must not be interpolated into inline onclick.

    Inline onclick="handler('${m.marker_id}')" breaks (or injects) on a quote or
    special char. The rendered markup must instead carry data-action /
    data-marker-id / data-frame-url and rely on delegated listeners. We assert
    the served source no longer emits onclick=/onkeydown= for the kafelek
    handlers and that the data-action contract is present.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    html = client.get("/").text

    assert 'data-action="analyze"' in html
    assert 'data-action="delete"' in html
    assert 'data-action="open-frame"' in html
    assert 'data-action="select"' in html
    assert "data-frame-url=" in html
    # No inline onclick/onkeydown interpolating marker_id/frame_url survives.
    assert 'onclick="analyzeMarker' not in html
    assert 'onclick="deleteMarker' not in html
    assert 'onclick="selectMarker' not in html
    assert 'onclick="event.stopPropagation(); openFrameModal' not in html
    assert 'onkeydown="handleMarkerKeydown' not in html


def test_analyze_dashboard_uses_window_mouseup_to_stop_recording(sample_video: Path) -> None:
    """Releasing outside the mic button should still stop hold-to-record."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text

    assert "const finishRecording" in html
    assert "window.addEventListener('mouseup', finishRecording)" in html
    assert "micBtn.addEventListener('mouseleave'" not in html


def test_analyze_capture_ui_removes_duplicate_mic_instructions(sample_video: Path) -> None:
    """Hold-to-record guidance should live in the mic tooltip, not three places."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text

    assert 'id="transcriptPreview" class="transcript-preview" hidden' in html
    assert "Hold mic button to record, or type below" not in html
    assert "Przytrzymaj mikrofon, aby nagrać, lub wpisz poniżej" not in html
    assert 'data-i18n-attr="title:mic_title"' in html
