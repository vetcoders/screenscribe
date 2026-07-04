"""Layer 2 — review-sidebar layout regression (UX-LAYOUT-1).

A manual-frame card's text bodies (transcript / summary / notes) must wrap on a
READABLE width — the same way the AI finding cards wrap — not collapse to a
~one-word-per-line column.

Root cause this guards: the card render reuses ``class="manual-frame-body"`` for
plain text divs, while an UNSCOPED ``.manual-frame-body { display:grid;
grid-template-columns: minmax(0,1.15fr) minmax(320px,0.85fr) }`` rule (authored
for the manual-frame MODAL) leaked onto the card. Inside the card the single
text node landed in a track that collapses toward 0 because the second grid
track reserves ``minmax(320px, ...)`` — so the copy wrapped word-per-line.

This test drives the SAME installed binary + real serve path as the runtime
suite, injects a long-transcript manual frame through the real client render
(``renderManualFrames``), and asserts the rendered text body is wide and does
NOT wrap per word.

Falsification: revert the CSS fix (re-leak the modal grid onto the card) and the
width / line-count assertions fail.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]

# Long, multi-word transcript (Polish, per the brief's repro string). Long enough
# that on a readable width it wraps to only a handful of lines, but on a squeezed
# ~one-word column it wraps to ~one line per word.
LONG_TRANSCRIPT = (
    "Nie wiem dlaczego przednia notatka mowiona pojawila sie dwa razy w tym "
    "samym miejscu i czy to jest blad renderowania czy raczej podwojny zapis "
    "stanu po przeladowaniu strony w przegladarce uzytkownika"
)
WORD_COUNT = len(LONG_TRANSCRIPT.split())

# A small synthetic frame image (1x1 JPEG) so the card renders its thumbnail.
TINY_JPEG_B64 = (  # pragma: allowlist secret
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"  # pragma: allowlist secret
    "AAAAAAAAAAAAAAAAAAAAAv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)


def _inject_long_manual_frame(page) -> None:
    """Push a manual frame with a long multi-word transcript into client state
    and render it through the real card renderer."""
    page.evaluate(
        """([b64, transcript]) => {
            const dataUrl = 'data:image/jpeg;base64,' + b64;
            reportState.manualFrames = [{
                marker_id: 'layout-probe-1',
                timestamp: 0.5,
                timestamp_formatted: '00:00.500',
                frameBase64: b64,
                frameDataUrl: dataUrl,
                transcript: transcript,
                notes: '',
                severity: 'medium',
                result: { summary: transcript, issues_detected: [] },
            }];
            renderManualFrames();
            // The manual-frame card lives in the (initially inactive) findings
            // tab; an inactive tab-content is display:none and measures 0 width.
            // Activate it so the card is laid out for real measurement.
            activateTab('findings', { persist: false });
        }""",
        [TINY_JPEG_B64, LONG_TRANSCRIPT],
    )


def _measure(page) -> dict:
    return page.evaluate(
        """() => {
            const px = (el, prop) => el
                ? parseFloat(getComputedStyle(el).getPropertyValue(prop))
                : null;
            const rectW = (el) => el ? el.getBoundingClientRect().width : null;

            const item = document.querySelector(
                '#manualFindingsList .manual-frame-item'
            );
            const content = item
                ? item.querySelector('.manual-frame-content')
                : null;
            // The transcript body is the SECOND .manual-frame-body in the card
            // (the first is the summary). Both share the bug; pick the longest.
            const bodies = item
                ? Array.from(item.querySelectorAll('.manual-frame-body'))
                : [];
            // The sidebar scroll column the card lives in.
            const sidebar =
                document.querySelector('.sidebar-scroll') ||
                document.querySelector('.sidebar') ||
                document.querySelector('.review-column');

            // Choose the body with the most text (the long transcript).
            let body = null, bestLen = -1;
            for (const b of bodies) {
                const len = (b.textContent || '').length;
                if (len > bestLen) { bestLen = len; body = b; }
            }

            // Detect per-word wrap: count rendered line boxes via client rects.
            // A body squeezed to a one-word column produces ~one rect per word.
            let lineCount = null;
            if (body) {
                const range = document.createRange();
                range.selectNodeContents(body);
                const rects = Array.from(range.getClientRects());
                // Collapse rects sharing the same top into one visual line.
                const tops = new Set(rects.map(r => Math.round(r.top)));
                lineCount = tops.size;
            }

            return {
                sidebarWidth: rectW(sidebar),
                itemWidth: rectW(item),
                contentWidth: rectW(content),
                bodyWidth: rectW(body),
                bodyDisplay: body ? getComputedStyle(body).display : null,
                bodyText: body ? body.textContent : null,
                lineCount: lineCount,
                viewport: { w: window.innerWidth, h: window.innerHeight },
            };
        }"""
    )


def test_manual_frame_text_wraps_readably(review_server, browser_context) -> None:
    # A roomy viewport so the sidebar is its full ~48vw width (well above the
    # 900px stacked-fallback breakpoint) — this is where the squeeze bit.
    page = browser_context.new_page()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept())

    page.goto(review_server.url, wait_until="networkidle")
    _inject_long_manual_frame(page)

    m = _measure(page)

    # The card actually rendered.
    assert m["bodyWidth"] is not None, f"no manual-frame body rendered: {m!r}"
    assert m["sidebarWidth"], f"sidebar column not found: {m!r}"

    # 1) The text body must NOT be a leaked 2-column grid.
    assert m["bodyDisplay"] != "grid", (
        "manual-frame card body is display:grid — the modal grid rule leaked onto "
        f"the card (UX-LAYOUT-1 regression). measured: {m!r}"
    )

    # 2) Readable width: the body must be wide, not a one-word column.
    #    Threshold: >= 60% of the sidebar width, AND an absolute >= 200px floor.
    min_readable = max(200.0, 0.60 * m["sidebarWidth"])
    assert m["bodyWidth"] >= min_readable, (
        f"manual-frame text body too narrow: {m['bodyWidth']:.0f}px "
        f"(need >= {min_readable:.0f}px; sidebar {m['sidebarWidth']:.0f}px). "
        f"per-word-wrap squeeze regression. measured: {m!r}"
    )

    # 3) Non-vacuous: the long transcript must wrap to FAR fewer lines than words.
    #    A per-word column produces ~WORD_COUNT lines; a readable width produces
    #    a small handful. Half the word count is a generous, robust ceiling.
    assert m["lineCount"] is not None, f"could not count lines: {m!r}"
    assert m["lineCount"] < WORD_COUNT / 2, (
        f"manual-frame transcript wrapped to {m['lineCount']} lines for "
        f"{WORD_COUNT} words — per-word-wrap squeeze. measured: {m!r}"
    )

    # Surfaced under `-s` for layout debugging / measurement evidence.
    print(f"[UX-LAYOUT-1] manual-frame layout measurements: {m!r}")

    page.close()
