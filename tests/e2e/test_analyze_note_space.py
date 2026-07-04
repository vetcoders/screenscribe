"""C1 / A6 regression — Space typed in a marker note editor must insert a space.

The marker card is keyboard-selectable (``.marker-item[data-action="select"]``)
and its delegated keydown handler calls ``preventDefault()`` on Space/Enter to
select the marker. The note editor nested inside the card opts out of the card's
delegation via ``data-action="stop"`` — but the keydown delegation used
``closest('.marker-item[data-action="select"]')``, which climbed *past* the
``stop`` boundary. Result: Space typed into the note ``<textarea>`` bubbled up,
hit the marker-select ``preventDefault``, and was swallowed ("Gotowe do analizy"
became "Gotowedoanalizy").

This drives the REAL delegated handler attached to ``#markersList`` at init by
injecting the exact card structure, then typing into the note textarea.
"""

from __future__ import annotations

import html as _html

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]


def _render_analyze_html() -> str:
    from screenscribe.shell import ANALYZE_SURFACE, render_surface

    lang = "en"
    context = {
        "document_language": _html.escape(lang),
        "ui_language": _html.escape(lang),
        "video_name": "space-demo.mov",
        "video_name_escaped": _html.escape("space-demo.mov"),
        "speech_lang_label": _html.escape(lang.upper()),
        "body_mode": "analyze",
        "body_default_lang": lang,
        "body_speech_lang": lang,
        "body_has_markers": "true",
        "findings_json": "[]",
        "segments_json": "[]",
    }
    return render_surface(ANALYZE_SURFACE, context)


_INJECT_MARKER_CARD = """() => {
    const c = document.getElementById('markersList');
    c.innerHTML = `
      <div class="marker-item pending" data-action="select" data-marker-id="m1"
           data-marker-timestamp="1" role="option" tabindex="0" aria-selected="false">
        <div class="marker-note-editor" id="note-editor-m1" data-action="stop"
             style="display:block">
          <textarea id="note-input-m1" style="display:block"></textarea>
        </div>
      </div>`;
}"""


def test_space_in_marker_note_editor_is_typed(tmp_path) -> None:
    from playwright.sync_api import sync_playwright

    path = tmp_path / "analyze.html"
    path.write_text(_render_analyze_html(), encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(path.as_uri(), wait_until="load")
        page.evaluate(_INJECT_MARKER_CARD)
        # The markers list lives in a non-active tab panel; force the textarea's
        # ancestors visible so it is focusable, then focus it directly. The real
        # delegated keydown handler on #markersList fires regardless of the tab.
        page.evaluate(
            """() => {
                let el = document.getElementById('note-input-m1');
                while (el) {
                    el.hidden = false;
                    if (getComputedStyle(el).display === 'none') el.style.display = 'block';
                    el = el.parentElement;
                }
                document.getElementById('note-input-m1').focus();
            }"""
        )
        page.keyboard.type("Gotowe do analizy")
        value = page.eval_on_selector("#note-input-m1", "el => el.value")
        browser.close()

    assert value == "Gotowe do analizy", (
        f"Space swallowed inside marker note editor (got {value!r}); the marker "
        "keydown delegation must honor the data-action='stop' boundary."
    )
