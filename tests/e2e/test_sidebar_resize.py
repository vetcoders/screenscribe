"""C3.2 / REG-1 — sidebar resize splitter regression guard.

The vertical splitter ``#sidebarResizer`` between the main video panel and the
right rail must let the user drag the sidebar to any proportion AT EVERY
viewport width, and the chosen width must persist across a reload.

Regression root cause this guards (CSS, shared ``report-pro.css``):
the base grid rule honours ``var(--sidebar-width)`` (which the resize JS sets
inline on ``:root``), but two media queries hard-coded the sidebar track to a
``clamp(...)`` and ignored ``--sidebar-width``:

  * ``@media (max-width: 1200px) and (min-width: 901px)``  -> drag dead on a
    windowed (non-full-screen) viewport.
  * ``@media (min-width: 1600px)``                          -> drag dead on a
    large full-screen monitor.

Together they squeezed the only working band to 1201-1599px, which is why the
operator saw "used to work full-screen, not windowed" degrade to "doesn't work
at all" once on a >=1600px display. The fix makes both queries honour
``var(--sidebar-width, clamp(...))`` (clamp = no-JS fallback, not an override).

Surfaces: the shell (``shell.html``) + ``report-pro.css`` are shared by BOTH the
review report and the analyze dashboard, and ``review_app.js initSidebarResize``
/ ``analyze_dashboard.js initSplitter`` are twins (both ``setProperty(
'--sidebar-width', ...)``). The browser drag below exercises the shared shell on
the review surface; ``test_analyze_splitter_drag_changes_width`` renders the
analyze surface over ``file://`` and performs a REAL pointer drag on its twin
``initSplitter``, so A4 is covered by behavior (not a source string-match) without
a separate analyze server fixture.

Falsification: revert either media-query to a hard ``clamp(...)`` and the drag
assertion at the matching viewport (1100 / 1700) fails.
"""

from __future__ import annotations

import html as _html

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]


def _sidebar_width(page) -> float | None:
    return page.evaluate(
        """() => {
            const s = document.querySelector('.sidebar');
            return s ? s.getBoundingClientRect().width : null;
        }"""
    )


def _resizer_center(page) -> dict | None:
    return page.evaluate(
        """() => {
            const r = document.getElementById('sidebarResizer');
            if (!r) return null;
            const b = r.getBoundingClientRect();
            return { x: b.x + b.width / 2, y: b.y + b.height / 2,
                     display: getComputedStyle(r).display };
        }"""
    )


def _drag_resizer(page, dx: float) -> None:
    """Drag #sidebarResizer horizontally by dx px (negative = widen the rail).

    Playwright mouse actions emit real pointer events (pointerdown/move/up with
    pointerId), so the resizer's setPointerCapture-based handler runs exactly as
    under a human drag.
    """
    c = _resizer_center(page)
    assert c is not None, "no #sidebarResizer in DOM"
    assert c["display"] != "none", "resizer is display:none (hidden) at this viewport"
    page.mouse.move(c["x"], c["y"])
    page.mouse.down()
    steps = 12
    for i in range(1, steps + 1):
        page.mouse.move(c["x"] + dx * i / steps, c["y"])
    page.mouse.up()


@pytest.mark.parametrize(
    "viewport",
    [1100, 1400, 1700],
    ids=["tablet-1100", "base-1400", "wide-1700"],
)
def test_sidebar_drag_changes_width_at_viewport(review_server, browser_context, viewport) -> None:
    """A1 (1100, tablet media-query), A2 (1400, base rule), A-wide (1700, >=1600
    media-query): dragging the splitter must change the sidebar width."""
    page = browser_context.new_page()
    page.set_viewport_size({"width": viewport, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(review_server.url, wait_until="networkidle")

    before = _sidebar_width(page)
    assert before, f"sidebar not laid out at {viewport}px: {before!r}"

    # Drag left -> widen the right rail. JS: delta = startX - clientX, so a
    # leftward drag increases width (bounded by maxPx).
    _drag_resizer(page, -140)
    after = _sidebar_width(page)
    assert after is not None

    delta = abs(after - before)
    assert delta > 20, (
        f"sidebar width did not change on drag at {viewport}px "
        f"(before={before:.0f} after={after:.0f} delta={delta:.0f}). "
        "media-query likely ignores --sidebar-width (REG-1)."
    )
    print(f"[REG-1] viewport={viewport} before={before:.0f} after={after:.0f} delta={delta:.0f}")
    page.close()


def test_sidebar_width_persists_after_reload(review_server, browser_context) -> None:
    """A3: dragged width is saved to localStorage and restored after reload."""
    page = browser_context.new_page()
    page.set_viewport_size({"width": 1400, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(review_server.url, wait_until="networkidle")

    before = _sidebar_width(page)
    _drag_resizer(page, -160)
    after = _sidebar_width(page)
    assert abs(after - before) > 20, f"no drag effect before reload: {before!r}->{after!r}"

    saved = page.evaluate("() => localStorage.getItem('screenscribe_sidebar_width')")
    assert saved is not None, "drag did not persist --sidebar-width to localStorage"
    saved_px = float(saved)

    page.reload(wait_until="networkidle")
    restored = _sidebar_width(page)
    assert restored is not None
    assert abs(restored - saved_px) < 30, (
        f"sidebar width not restored after reload: saved={saved_px:.0f} restored={restored:.0f}"
    )
    print(f"[REG-1] persist saved={saved_px:.0f} restored={restored:.0f}")
    page.close()


def test_sidebar_resizer_hidden_on_mobile(review_server, browser_context) -> None:
    """A6 negative guard: at <=900px the resizer stays display:none (drag is a
    deliberate no-op on mobile) — the fix must NOT activate it there."""
    page = browser_context.new_page()
    page.set_viewport_size({"width": 800, "height": 900})
    page.goto(review_server.url, wait_until="networkidle")
    c = _resizer_center(page)
    assert c is not None
    assert c["display"] == "none", (
        f"resizer should be hidden at 800px, got display={c['display']!r}"
    )
    page.close()


def _render_analyze_html() -> str:
    """Self-contained ANALYZE dashboard HTML from the source tree (no API/ffmpeg).

    Mirrors ``test_smoke_no_api._render_analyze_html``: the surface is a pure
    render over plain context args, so it loads over ``file://`` in the cached
    Chromium with no server and no API key — which is exactly what lets the
    analyze splitter twin be exercised by a real drag in keyless CI too.
    """
    from screenscribe.shell import ANALYZE_SURFACE, render_surface

    lang = "en"
    context = {
        "document_language": _html.escape(lang),
        "ui_language": _html.escape(lang),
        "video_name": "splitter-demo.mov",
        "video_name_escaped": _html.escape("splitter-demo.mov"),
        "speech_lang_label": _html.escape(lang.upper()),
        "body_mode": "analyze",
        "body_default_lang": lang,
        "body_speech_lang": lang,
        "body_has_markers": "false",
        "findings_json": "[]",
        "segments_json": "[]",
    }
    return render_surface(ANALYZE_SURFACE, context)


@pytest.fixture(scope="module")
def analyze_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Render the ANALYZE dashboard once and return a ``file://`` URL."""
    path = tmp_path_factory.mktemp("analyze_splitter") / "analyze.html"
    path.write_text(_render_analyze_html(), encoding="utf-8")
    return path.as_uri()


@pytest.mark.parametrize(
    "viewport",
    [1400, 1700],
    ids=["base-1400", "wide-1700"],
)
def test_analyze_splitter_drag_changes_width(analyze_url, browser_context, viewport) -> None:
    """A4 (analyze surface, BEHAVIOR not string-match): the analyze dashboard
    shares ``shell.html`` + ``report-pro.css`` and its ``initSplitter`` is a twin
    of review's ``initSidebarResize``. Render analyze over ``file://`` and perform
    a REAL pointer drag on ``#sidebarResizer`` — the sidebar width must change.

    Replaces the former ``test_analyze_splitter_parity_static`` string-match
    (THEATER): that test stayed green even if ``initSplitter``'s drag logic was
    gutted, as long as the source strings survived. This one goes RED the moment
    the analyze drag stops moving the rail. Viewports 1400 (base rule) and 1700
    (>=1600 media-query) also exercise the shared ``--sidebar-width`` CSS fix.
    """
    page = browser_context.new_page()
    page.set_viewport_size({"width": viewport, "height": 900})
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(analyze_url, wait_until="load")
    # Let the DOMContentLoaded init (initSplitter) run before measuring.
    page.wait_for_timeout(250)

    before = _sidebar_width(page)
    assert before, f"analyze sidebar not laid out at {viewport}px: {before!r}"

    # Drag left -> widen the right rail (delta = startX - clientX), same handler
    # contract as review's initSidebarResize.
    _drag_resizer(page, -140)
    after = _sidebar_width(page)
    assert after is not None

    delta = abs(after - before)
    assert delta > 20, (
        f"analyze sidebar width did not change on drag at {viewport}px "
        f"(before={before:.0f} after={after:.0f} delta={delta:.0f}). "
        "analyze initSplitter twin is broken or a media-query ignores --sidebar-width."
    )
    print(
        f"[A4] analyze viewport={viewport} before={before:.0f} after={after:.0f} delta={delta:.0f}"
    )
    page.close()
