"""Agent panel conversation in a real browser against the real review server.

Covers the four reviewer-facing promises end to end: the answer streams in
before the turn finishes, Stop keeps the partial answer, the panel resizes
within bounds and remembers it, and dictation fills an editable draft without
sending it. Chromium runs with a fake microphone fed by a synthesized speech
file, so the STT round-trip is real.

Falsification: buffer the provider round server-side again (one ``token`` frame
at the end) and ``test_answer_streams_before_the_turn_finishes`` fails because no
text is visible while the "responding" bar is still shown.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]

_LAST_ANSWER = (
    "() => { const r = [...document.querySelectorAll('.ss-agent-msg-assistant')];"
    " return r.length ? r[r.length - 1].textContent : ''; }"
)


@pytest.fixture(scope="module")
def speech_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    say = shutil.which("say")
    if say is None:
        pytest.skip("macOS `say` is needed to synthesize the fake-microphone speech")
    wav = tmp_path_factory.mktemp("ss_e2e_mic") / "dictation.wav"
    subprocess.run(
        [
            say,
            "-o",
            str(wav),
            "--data-format=LEI16@48000",
            "Show me what is on the first frame of this recording.",
        ],
        check=True,
        capture_output=True,
    )
    return wav


@pytest.fixture
def mic_page(review_server: Any, speech_wav: Path) -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        executable_path=os.environ.get("SCREENSCRIBE_E2E_CHROMIUM") or None,
        args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={speech_wav}",
        ],
    )
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    context.grant_permissions(["microphone"], origin=f"http://localhost:{review_server.port}")
    page = context.new_page()
    try:
        page.goto(review_server.url)
        page.wait_for_selector("#ss-agent-fab")
        page.evaluate("localStorage.clear()")
        page.click("#ss-agent-fab")
        page.wait_for_selector("#ss-agent-panel:not([hidden])")
        yield page
    finally:
        context.close()
        browser.close()
        pw.stop()


def test_answer_streams_before_the_turn_finishes(mic_page: Any) -> None:
    page = mic_page
    assert page.is_disabled("#ss-agent-send"), "Send must be disabled for an empty draft"
    page.fill("#ss-agent-input", "Describe the recording in five sentences.")
    page.keyboard.press("Enter")
    page.wait_for_selector("#ss-agent-activity:not([hidden])", timeout=10_000)
    page.wait_for_function(
        f"() => ({_LAST_ANSWER})().trim().length > 0", timeout=180_000, polling=50
    )
    still_streaming = page.evaluate("() => !document.getElementById('ss-agent-activity').hidden")
    first_len = len(page.evaluate(_LAST_ANSWER))
    page.wait_for_selector("#ss-agent-activity[hidden]", state="attached", timeout=240_000)
    final_len = len(page.evaluate(_LAST_ANSWER))
    assert still_streaming, "text appeared only after the turn ended"
    assert final_len > first_len, (first_len, final_len)
    assert page.is_enabled("#ss-agent-input")


def test_stop_keeps_the_partial_answer(mic_page: Any) -> None:
    page = mic_page
    page.fill(
        "#ss-agent-input",
        "Write a long, detailed, point-by-point walkthrough of every finding and frame.",
    )
    page.keyboard.press("Enter")
    page.wait_for_function(
        f"() => ({_LAST_ANSWER})().trim().length > 20", timeout=180_000, polling=50
    )
    page.click("#ss-agent-stop")
    page.wait_for_selector("#ss-agent-activity[hidden]", state="attached", timeout=15_000)
    stopped = page.evaluate(
        "() => { const r = [...document.querySelectorAll('.ss-agent-msg-assistant')];"
        " const last = r[r.length - 1]; return { cls: last.className,"
        " badge: !!last.querySelector('.ss-agent-badge'), len: last.textContent.length }; }"
    )
    assert "ss-agent-msg-interrupted" in stopped["cls"]
    assert stopped["badge"]
    assert stopped["len"] > 20
    assert page.locator(".ss-agent-msg-error").count() == 0


def test_panel_resizes_and_remembers_its_size(mic_page: Any) -> None:
    page = mic_page
    before = page.eval_on_selector("#ss-agent-root", "el => [el.offsetWidth, el.offsetHeight]")
    page.focus("#ss-agent-resize")
    for _ in range(3):
        page.keyboard.press("ArrowLeft")
    page.keyboard.press("Shift+ArrowDown")
    after = page.eval_on_selector("#ss-agent-root", "el => [el.offsetWidth, el.offsetHeight]")
    assert after[0] == before[0] + 72 and after[1] == before[1] + 96, (before, after)
    box = page.evaluate(
        "() => { const p = document.getElementById('ss-agent-root').getBoundingClientRect();"
        " const v = document.getElementById('videoPlayer').getBoundingClientRect();"
        " return { p: [p.left, p.top, p.right, p.bottom], v: [v.left, v.top, v.right, v.bottom],"
        " vw: innerWidth, vh: innerHeight }; }"
    )
    p, v = box["p"], box["v"]
    overlaps = p[0] < v[2] and p[2] > v[0] and p[1] < v[3] and p[3] > v[1]
    assert not overlaps, box
    assert p[0] >= 0 and p[1] >= 0 and p[2] <= box["vw"] and p[3] <= box["vh"], box
    page.reload()
    page.wait_for_selector("#ss-agent-panel:not([hidden])")
    restored = page.eval_on_selector("#ss-agent-root", "el => [el.offsetWidth, el.offsetHeight]")
    assert restored == after


def test_dictation_fills_the_draft_without_sending(mic_page: Any) -> None:
    page = mic_page
    users_before = page.locator(".ss-agent-msg-user").count()
    page.click("#ss-agent-mic")
    page.wait_for_function(
        "() => document.getElementById('ss-agent-mic').dataset.state === 'recording'",
        timeout=15_000,
    )
    assert page.is_visible("#ss-agent-mic-cancel")
    page.wait_for_timeout(4000)
    page.click("#ss-agent-mic")
    page.wait_for_function(
        "() => ['idle', 'error'].includes(document.getElementById('ss-agent-mic').dataset.state)",
        timeout=120_000,
    )
    state = page.eval_on_selector("#ss-agent-mic", "el => el.dataset.state")
    status = page.text_content("#ss-agent-mic-status") or ""
    draft = page.input_value("#ss-agent-input")
    assert state == "idle", status
    assert "frame" in draft.lower(), draft
    assert page.locator(".ss-agent-msg-user").count() == users_before, "dictation was sent"
    assert page.is_enabled("#ss-agent-send")
