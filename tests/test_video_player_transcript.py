"""Shared video player transcript empty-state tests."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VIDEO_PLAYER_JS = REPO_ROOT / "screenscribe/html_pro_assets/scripts/video_player.js"


def _run_video_player_node_test(test_body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for video_player.js transcript tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');

        const source = fs.readFileSync({str(VIDEO_PLAYER_JS)!r}, 'utf8');

        function makeClassList() {{
            const values = new Set();
            return {{
                add(value) {{ values.add(value); }},
                remove(value) {{ values.delete(value); }},
                contains(value) {{ return values.has(value); }},
                toggle(value, force) {{
                    if (force === true) values.add(value);
                    else if (force === false) values.delete(value);
                    else if (values.has(value)) values.delete(value);
                    else values.add(value);
                }},
                values,
            }};
        }}

        function makeElement(id) {{
            return {{
                id,
                tagName: String(id || 'div').toUpperCase(),
                textContent: '',
                hidden: false,
                disabled: false,
                dataset: {{}},
                value: '',
                children: [],
                classList: makeClassList(),
                style: {{}},
                addEventListener() {{}},
                setAttribute(name, value) {{ this[name] = value; }},
                appendChild(child) {{ this.children.push(child); }},
                replaceChildren(...children) {{
                    this.replaceChildrenCalled = true;
                    this.children = children;
                }},
                querySelector() {{ return null; }},
                querySelectorAll() {{ return []; }},
                scrollIntoView() {{}},
            }};
        }}

        const elements = new Map();
        const getElement = (id) => {{
            if (!elements.has(id)) elements.set(id, makeElement(id));
            return elements.get(id);
        }};

        const video = getElement('videoPlayer');
        video.tagName = 'VIDEO';
        video.paused = true;
        video.currentTime = 0;
        video.duration = 10;
        video.play = () => Promise.resolve();
        video.pause = () => {{ video.paused = true; }};

        const subtitleList = getElement('subtitleList');
        subtitleList.children = [makeElement('empty-state')];
        const listeners = new Map();

        const sandbox = {{
            console,
            setTimeout,
            clearTimeout,
            Math,
            Date,
            process,
            listeners,
            video,
            window: {{
                DEBUG: false,
                location: {{ search: '' }},
                TRANSCRIPT_SEGMENTS: undefined,
                addEventListener() {{}},
            }},
            document: {{
                body: {{ tagName: 'BODY', dataset: {{ mode: 'analyze' }} }},
                getElementById: getElement,
                createElement: makeElement,
                querySelector() {{ return null; }},
                querySelectorAll() {{ return []; }},
                addEventListener(event, handler) {{
                    if (!listeners.has(event)) listeners.set(event, []);
                    listeners.get(event).push(handler);
                }},
            }},
        }};
        sandbox.globalThis = sandbox;

        const script = new vm.Script(source + "\\n" + {test_body!r}, {{
            filename: 'video_player.js',
        }});
        script.runInNewContext(sandbox);
        """
    )
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_analyze_without_transcript_preserves_empty_state_and_disables_search() -> None:
    _run_video_player_node_test(
        """
        (() => {
            const player = new ScreenScribePlayer();
            const transcriptPanel = document.getElementById('transcriptPanel');
            const subtitleList = document.getElementById('subtitleList');
            const searchBox = document.getElementById('subtitleSearch');

            if (!transcriptPanel.classList.contains('transcript-panel--disabled')) {
                throw new Error('transcript panel was not marked disabled');
            }
            if (!searchBox.disabled || searchBox.hidden !== true) {
                throw new Error('search box was not disabled/hidden');
            }
            if (subtitleList.replaceChildrenCalled) {
                throw new Error('empty transcript state was removed');
            }
            if (subtitleList.children.length !== 1) {
                throw new Error('empty transcript state child count changed');
            }
            if (player.segments.length !== 0) {
                throw new Error('expected no transcript segments');
            }
        })();
        """
    )


def test_spacebar_handler_ignores_native_controls_and_form_fields() -> None:
    _run_video_player_node_test(
        """
        (() => {
            const player = new ScreenScribePlayer();
            const keydown = listeners.get('keydown')?.[0];
            if (!keydown) throw new Error('keydown handler was not registered');

            let playCalls = 0;
            let pauseCalls = 0;
            video.play = () => {
                playCalls += 1;
                video.paused = false;
                return Promise.resolve();
            };
            video.pause = () => {
                pauseCalls += 1;
                video.paused = true;
            };

            const fireSpace = (target) => {
                let prevented = false;
                keydown({
                    code: 'Space',
                    target,
                    preventDefault() { prevented = true; },
                });
                return prevented;
            };

            if (!fireSpace(document.body)) {
                throw new Error('body Space should be handled');
            }
            if (playCalls !== 1 || pauseCalls !== 0 || video.paused !== false) {
                throw new Error(`expected one play from body Space, got play=${playCalls} pause=${pauseCalls}`);
            }

            const ignoredTargets = [
                { tagName: 'VIDEO' },
                { tagName: 'BUTTON' },
                { tagName: 'SELECT' },
                { tagName: 'TEXTAREA' },
                { tagName: 'INPUT' },
                { tagName: 'DIV', isContentEditable: true },
            ];

            for (const target of ignoredTargets) {
                const beforePlay = playCalls;
                const beforePause = pauseCalls;
                const prevented = fireSpace(target);
                if (prevented) throw new Error(`Space was prevented for ${target.tagName}`);
                if (playCalls !== beforePlay || pauseCalls !== beforePause) {
                    throw new Error(`Space toggled video for ${target.tagName}`);
                }
            }

            void player;
        })();
        """
    )
