"""Regression checks for bundled HTML Pro assets."""

from __future__ import annotations

import re
from pathlib import Path

from screenscribe.html_pro import assets
from screenscribe.html_pro.assets import load_css, load_css_screenscribe_theme
from screenscribe.shell import ANALYZE_SURFACE, render_surface

_EXTERNAL_SCRIPT = re.compile(r'<script\b[^>]*\bsrc="https?://[^"]+"[^>]*>', re.IGNORECASE)


def _render_analyze_shell() -> str:
    return render_surface(
        ANALYZE_SURFACE,
        {
            "document_language": "en",
            "ui_language": "en",
            "video_name": "asset-test.mp4",
            "video_name_escaped": "asset-test.mp4",
            "speech_lang_label": "EN",
            "body_mode": "analyze",
            "body_default_lang": "en",
            "body_speech_lang": "en",
            "body_has_markers": "false",
            "findings_json": "[]",
            "segments_json": "[]",
        },
    )


def test_external_scripts_in_templates_have_sri() -> None:
    """Every external CDN <script> in shipped templates must be pinned with
    Subresource Integrity (integrity + crossorigin), so a CDN compromise cannot
    inject code into a generated report. Guards against re-introducing an
    un-pinned external script."""
    templates_dir = Path(assets.__file__).resolve().parent.parent / "html_pro_assets" / "templates"
    html_files = sorted(templates_dir.glob("*.html"))
    assert html_files, f"no template html files found in {templates_dir}"

    offenders: list[str] = []
    for html in html_files:
        for tag in _EXTERNAL_SCRIPT.findall(html.read_text(encoding="utf-8")):
            if 'integrity="' not in tag or "crossorigin=" not in tag:
                offenders.append(f"{html.name}: {tag}")
    assert not offenders, "external scripts missing SRI:\n" + "\n".join(offenders)


def test_report_viewer_partials_have_no_inline_onclick() -> None:
    """CSP-readiness (C7.2 + C7.2b): NO report-viewer partial carries an inline
    onclick. Behavior is wired via event delegation in JS, so a strict
    script-src without unsafe-inline event attributes stays possible. Scanned
    across every partial (no exclusions) so a new inline handler regresses here.
    """
    partials_dir = (
        Path(assets.__file__).resolve().parent.parent / "html_pro_assets" / "templates" / "partials"
    )
    offenders = [
        path.name
        for path in sorted(partials_dir.glob("*.html"))
        if "onclick=" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, "inline onclick still present in partials: " + ", ".join(offenders)


def test_stylesheets_have_no_webfont_imports() -> None:
    """The report is an offline evidence bundle: opening it must not trigger
    any network request. No @import in any shipped stylesheet; brand fonts
    resolve only if locally installed, with system stacks as fallback."""
    base_css = load_css()
    theme_css = load_css_screenscribe_theme()

    assert "@import" not in base_css
    assert "@import" not in theme_css
    # The human-voice serif keeps local fallbacks so the design degrades
    # gracefully without the webfont.
    assert "Newsreader" in theme_css
    assert "Georgia" in theme_css


def test_jszip_is_vendored_inline_not_cdn() -> None:
    """The Export-ZIP dependency (JSZip) ships inlined from the vendored copy —
    no third-party CDN script in the report, so reports work fully offline."""
    jszip = assets.load_js_jszip()
    assert "JSZip" in jszip
    assert len(jszip) > 50_000

    from screenscribe.html_pro.renderer import render_html_report_pro

    html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=[],
        segments=[],
        errors=[],
    )
    assert "cdnjs.cloudflare.com" not in html
    assert "integrity=" not in html
    assert "JSZip" in html


_LOADABLE_EXTERNAL = (
    re.compile(r'<script\b[^>]*\bsrc=["\']https?://', re.IGNORECASE),
    re.compile(r'<link\b[^>]*\bhref=["\']https?://', re.IGNORECASE),
    re.compile(r'<img\b[^>]*\bsrc=["\']https?://', re.IGNORECASE),
    re.compile(r'@import\s+(?:url\(\s*)?["\']?https?://', re.IGNORECASE),
    re.compile(r'url\(\s*["\']?https?://', re.IGNORECASE),
)

_BANNED_HOSTS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdnjs.cloudflare.com",
    "unpkg.com",
    "jsdelivr.net",
)


def test_rendered_report_has_no_external_network_dependencies() -> None:
    """A generated HTML Pro report must be fully self-contained: no resource
    the browser would auto-load from the network (scripts, styles, fonts,
    images, CSS imports). Text URLs inside vendored-library comments or user
    data are fine — they are not fetched by opening the file."""
    from screenscribe.html_pro.renderer import render_html_report_pro

    html = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=[],
        segments=[],
        errors=[],
    )

    offenders = [pattern.pattern for pattern in _LOADABLE_EXTERNAL if pattern.search(html)]
    assert not offenders, "report auto-loads external resources: " + ", ".join(offenders)
    hosts = [host for host in _BANNED_HOSTS if host in html]
    assert not hosts, "report references CDN/font hosts: " + ", ".join(hosts)


def test_analyze_dashboard_controller_is_bundled_asset() -> None:
    assert hasattr(assets, "load_js_analyze_dashboard")
    dashboard_js = assets.load_js_analyze_dashboard()

    assert "class VoiceRecorder" in dashboard_js
    assert "class FrameMarker" in dashboard_js
    assert "function renderMarkerTicks" in dashboard_js
    assert "document.addEventListener('DOMContentLoaded'" in dashboard_js


def test_analyze_dashboard_marker_refresh_degrades_to_empty_list() -> None:
    """Failed / malformed marker fetches must not feed undefined into the renderer."""
    dashboard_js = assets.load_js_analyze_dashboard()

    assert "markers = Array.isArray(markers) ? markers : [];" in dashboard_js
    assert "if (!response.ok) {" in dashboard_js
    assert "updateMarkersList([]);" in dashboard_js
    assert "catch (error)" in dashboard_js


def test_analyze_dashboard_styles_are_bundled_asset() -> None:
    assert hasattr(assets, "load_css_analyze_dashboard")
    analyze_css = assets.load_css_analyze_dashboard()
    server_source = Path("screenscribe/analyze_server.py").read_text(encoding="utf-8")

    assert ".capture-controls" in analyze_css
    assert ".marker-tick" in analyze_css
    assert ".lang-toggle" in analyze_css
    assert ".ux-hint" in analyze_css
    assert "{{" not in analyze_css
    assert "}}" not in analyze_css
    renderer_source = Path("screenscribe/shell/renderer.py").read_text(encoding="utf-8")
    assert "load_css_analyze_dashboard" in renderer_source
    assert "load_css_analyze_dashboard" not in server_source
    assert ".capture-controls {{" not in server_source
    assert "/* Analyze mode layout" not in server_source


_LEGACY_TOKEN_FAMILIES = (
    "--phosphor",
    "--quantum",
    "--crt-",
    "--accent-teal",
    "--vc-",
    "--vista",
)


def test_stylesheets_speak_one_token_language() -> None:
    """Design-token hygiene: every shipped stylesheet speaks the --ss-*
    identity + semantic token language. The legacy CRT/quantum/teal/vc
    families were consolidated away and must not reappear."""
    stylesheets = {
        "report-pro.css": load_css(),
        "screenscribe-theme.css": load_css_screenscribe_theme(),
        "analyze_dashboard.css": assets.load_css_analyze_dashboard(),
    }

    offenders = [
        f"{name}: {family}"
        for name, css in stylesheets.items()
        for family in _LEGACY_TOKEN_FAMILIES
        if family in css
    ]
    assert not offenders, "legacy design tokens resurfaced:\n" + "\n".join(offenders)

    # Sanity: the consolidation renamed tokens, it did not strip the token
    # system itself — the cascade still resolves custom properties.
    combined = "\n".join(stylesheets.values())
    assert combined.count("var(--") > 0
    assert not re.search(r"(?m)^\s*--ss-accent\s*:", stylesheets["report-pro.css"])
    assert "--ss-accent" in stylesheets["screenscribe-theme.css"]


def test_manual_frame_card_scopes_thumbnail_layout() -> None:
    """Manual-frame cards must constrain preview thumbnails to their grid column."""
    report_css = _collapse_ws(load_css())
    review_js = assets.load_js_review_app()

    assert '<div class="manual-frame-preview-card">' in review_js
    assert '<div class="manual-frame-content">' in review_js

    container_idx = report_css.find(".manual-frame-item .annotation-container {")
    assert container_idx != -1, "manual-frame annotation container override missing"
    container_block = report_css[container_idx : report_css.find("}", container_idx) + 1]
    assert "display: block" in container_block
    assert "width: 100%" in container_block
    assert "margin-top: 0" in container_block

    thumb_idx = report_css.find(".manual-frame-item .thumbnail {")
    assert thumb_idx != -1, "manual-frame thumbnail override missing"
    thumb_block = report_css[thumb_idx : report_css.find("}", thumb_idx) + 1]
    assert "width: 100%" in thumb_block
    assert "max-width: 100%" in thumb_block
    assert "height: auto" in thumb_block

    preview_idx = report_css.find(".manual-frame-item .manual-frame-preview-card {")
    assert preview_idx != -1, "manual-frame preview card rule missing"
    preview_block = report_css[preview_idx : report_css.find("}", preview_idx) + 1]
    assert "overflow: hidden" in preview_block


def _collapse_ws(text: str) -> str:
    """Squash runs of whitespace so CSS-rule assertions are independent of
    indentation / line breaks in the source stylesheet."""
    return re.sub(r"\s+", " ", text)


def test_analyze_dashboard_css_polish_rules_are_served() -> None:
    """R-P8/9/10 — dashboard CSS polish, asserted against the *served* assets.

    F-08 overlay click-through, F-09 responsive stats grid (no 4+1 orphan), and
    F-10 Export panel fill + centered helper copy are CSS-only fixes; this guards
    them against regression. Playwright screenshot-diff is unavailable in this
    suite, so we assert on the served CSS text plus the rendered class hooks the
    rules target (see deviations note)."""
    report_css = _collapse_ws(load_css())
    dashboard_css = _collapse_ws(assets.load_css_analyze_dashboard())

    # --- F-08: control overlay must not eat clicks over the video corner. ---
    # PRESENCE smoke, not a real click-through guarantee: we assert the overlay
    # container declares pointer-events:none and the interactive controls
    # re-enable pointer-events:auto. A present `pointer-events:none` string does
    # NOT prove a click actually passes through to the <video> beneath — that is
    # a runtime/browser property (real verification needs Playwright + axe-core;
    # see module TODO above).
    # Anchor on the main overlay rule (it carries the backdrop-filter chrome);
    # a narrow-viewport media-query override also declares .video-controls-pro.
    overlay_anchor = report_css.find("backdrop-filter: blur(6px)")
    assert overlay_anchor != -1, ".video-controls-pro main rule missing from served CSS"
    overlay_start = report_css.rfind(".video-controls-pro {", 0, overlay_anchor)
    assert overlay_start != -1, ".video-controls-pro rule missing from served CSS"
    overlay_block = report_css[overlay_start : report_css.find("}", overlay_anchor) + 1]
    assert "pointer-events: none" in overlay_block, (
        "overlay container must be click-through (pointer-events:none)"
    )
    assert "pointer-events: auto" in report_css, (
        "interactive controls must re-enable pointer-events:auto"
    )
    # The re-enable selector must actually cover the buttons block.
    assert ".video-controls-buttons, .video-controls-pro .player-btn" in report_css

    # --- F-10: Export tab fills and centers its helper copy (not empty). ---
    panel_idx = dashboard_css.find(".export-panel {")
    assert panel_idx != -1, ".export-panel rule missing from served CSS"
    panel_block = dashboard_css[panel_idx : dashboard_css.find("}", panel_idx) + 1]
    assert "min-height: 100%" in panel_block, "Export panel must fill its tab height"
    assert "justify-content: center" in panel_block, "Export helper copy must center"
    assert "flex: 1 1 auto" in panel_block, "Export panel must grow to fill the sidebar"

    # --- Rendered class hooks the rules target actually exist in served HTML. ---
    analyze_html = _render_analyze_shell()
    assert 'class="export-panel"' in analyze_html
    assert 'class="export-gate-hint"' in analyze_html
    from screenscribe.html_pro.renderer import render_html_report_pro

    report_html = render_html_report_pro(
        video_name="t.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="",
        findings=[],
        segments=[],
        errors=[],
    )
    assert 'class="video-controls-pro"' in report_html
    # Review's final tab hosts the artifact-download buttons (Statistics tab removed).
    assert 'id="tab-export"' in report_html


# =============================================================================
# R-P6 (W1G-F-06) — a11y-affordance PRESENCE smoke (NOT an a11y guarantee).
#
# These tests grep the served HTML + JS + CSS for the concrete a11y affordances
# the cut shipped: ARIA roles / states on the custom controls, tabindex on
# keyboard-operable widgets, focus-trap + focus-restore handlers in the modals,
# arrow-key tab nav, and :focus-visible rings. They assert those strings are
# PRESENT — a present string is not proof the affordance actually works.
#
# A present `role="dialog"` / `pointer-events:none` / focus-trap handler does
# NOT prove the dialog traps focus, the overlay is click-through, or a screen
# reader announces it correctly. That is a runtime/browser property these
# string greps cannot observe.
#
# TODO(a11y): real accessibility verification needs a browser-level tool —
# Playwright driving the rendered report/dashboard + axe-core auditing the live
# DOM (focus order, computed roles, click-through, contrast). That is a separate
# follow-up cut and a feature, not part of this presence-smoke. Do not let these
# greps stand in for it.
# =============================================================================


def _rendered_report_html() -> str:
    from screenscribe.html_pro.renderer import render_html_report_pro

    return render_html_report_pro(
        video_name="t.mov",
        video_path=None,
        generated_at="2026-02-15T17:31:26",
        executive_summary="An issue was found.",
        findings=[
            {
                "id": "f1",
                "category": "ui",
                "timestamp_formatted": "00:01",
                "timestamp": 1.0,
                "text": "Button misaligned.",
                "screenshot": "data:image/png;base64,AAAA",
                "unified_analysis": {
                    "severity": "critical",
                    "summary": "Critical layout bug.",
                    "is_issue": True,
                },
            }
        ],
        segments=[],
        errors=[],
        language="en",
    )


def test_report_template_aria_roles_and_states_presence_smoke() -> None:
    """Presence smoke, not an a11y guarantee. Asserts the rendered report
    *contains* ARIA roles/states on its custom controls: a tablist with
    aria-selected tabs, an aria-pressed language toggle, and a
    role=dialog/aria-modal lightbox with a role=toolbar annotation bar.
    Presence of these strings does not prove they behave correctly at runtime
    (see module TODO: real a11y needs Playwright + axe-core)."""
    html = _rendered_report_html()

    # Tabs: tablist + tab role + aria-selected on the active/inactive tabs.
    assert 'role="tablist"' in html
    assert html.count('role="tab"') >= 3
    assert 'aria-selected="true"' in html
    assert 'aria-selected="false"' in html
    assert 'role="tabpanel"' in html

    # Language toggle exposes pressed state for assistive tech.
    assert 'aria-pressed="true"' in html
    assert 'aria-pressed="false"' in html

    # Lightbox is a modal dialog with an accessible name + a labelled toolbar.
    assert 'id="lightbox"' in html
    lightbox_region = html[html.index('id="lightbox"') : html.index('id="lightbox"') + 1200]
    assert 'role="dialog"' in lightbox_region
    assert 'aria-modal="true"' in lightbox_region
    assert 'role="toolbar"' in lightbox_region
    assert 'aria-label="Annotation tools"' in lightbox_region

    # Manual-frame modal is a labelled modal dialog.
    assert 'class="manual-frame-dialog" role="dialog" aria-modal="true"' in html
    assert 'aria-labelledby="manualFrameTitle"' in html

    # The keyboard-operable resizer is a labelled separator.
    assert 'id="sidebarResizer"' in html
    assert 'role="separator"' in html


def test_filters_and_severity_aria_labels_presence_smoke() -> None:
    """Presence smoke, not an a11y guarantee. The Statistics tab (which hosted
    the stat-card filters) is removed, so this now asserts the surviving
    grouped controls and that the severity badge *carries* an explicit
    aria-label so screen-reader users get the meaning, not a bare word.
    Presence of the label string does not prove the screen-reader experience
    (see module TODO: real a11y needs Playwright + axe-core)."""
    html = _rendered_report_html()

    # The language toggle remains a labelled control group; the stat-card
    # filter group is gone with the Statistics tab.
    assert 'role="group"' in html
    assert 'aria-label="Filter findings by severity"' not in html
    assert 'class="stat-card' not in re.sub(
        r"<script\b.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE
    )

    # Severity badge on the finding carries a labelled meaning.
    assert 'class="severity-badge severity-critical"' in html
    assert "Severity: critical" in html


def test_review_js_focus_traps_and_keyboard_nav_presence_smoke() -> None:
    """Presence smoke, not an a11y guarantee. Asserts the review app JS *ships*
    the keyboard/focus machinery source: a focus-trap helper, focus-restore in
    both modals, arrow-key tab roving, keyboard tab activation of stat states,
    and a keyboard-resizable separator. Presence of the source strings does not
    prove the focus trap actually traps at runtime (see module TODO: real a11y
    needs Playwright + axe-core)."""
    js = assets.load_js_review_app()
    tab_js = assets.load_js_lib_tab_keyboard()

    # Focus trap helper + usage in lightbox and manual-frame modal.
    assert "function trapFocus(" in js
    assert "lightbox.__focusTrapHandler" in js
    assert "modal.__focusTrapHandler" in js

    # Focus restore: capture activeElement on open, restore on close.
    assert "lightboxReturnFocus = document.activeElement" in js
    assert "manualFrameRuntime.returnFocus = document.activeElement" in js

    # Escape closes both modals.
    assert "closeLightbox()" in js
    assert "closeManualFrameModal()" in js
    assert "if (e.key === 'Escape')" in js

    # Tabs respond to ArrowLeft / ArrowRight (roving tabindex pattern).
    assert "initTabKeyboard(tabBtns" in js
    assert "ArrowRight" in tab_js
    assert "ArrowLeft" in tab_js
    assert "nextBtn.focus()" in tab_js

    # activateTab / setLanguage keep ARIA state in sync.
    assert "aria-selected" in js
    assert "aria-pressed" in js

    # Resizer is keyboard-operable.
    assert "resizer.tabIndex = 0" in js
    assert "applySidebarWidth(next)" in js

    # Clickable thumbnails become keyboard-operable buttons.
    assert "img.setAttribute('role', 'button')" in js


def test_dashboard_js_focus_restore_and_keyboard_controls_presence_smoke() -> None:
    """Presence smoke, not an a11y guarantee. Asserts the analyze dashboard JS
    *ships* marker keyboard activation, aria-selected tracking, frame-modal
    focus restore, arrow-key tab nav, and a keyboard-resizable separator in its
    source. Presence of the source strings does not prove the runtime keyboard
    behavior (see module TODO: real a11y needs Playwright + axe-core)."""
    js = assets.load_js_analyze_dashboard()
    tab_js = assets.load_js_lib_tab_keyboard()

    # Markers: keyboard activation + selected state.
    assert "function handleMarkerKeydown(" in js
    assert "event.key === 'Enter' || event.key === ' '" in js
    assert 'role="option"' in js
    assert "aria-selected" in js

    # Frame modal: capture + restore focus.
    assert "frameModalReturnFocus = document.activeElement" in js
    assert "frameModalReturnFocus.focus()" in js

    # Escape closes the modal.
    assert "event.key === 'Escape'" in js
    assert "closeFrameModal()" in js

    # Tabs: arrow-key roving + aria-selected sync.
    assert "initTabKeyboard(tabButtons" in js
    assert "ArrowRight" in tab_js
    assert "setAttribute('aria-selected'" in js
    assert "nextBtn.focus()" in tab_js

    # Lang toggle keeps aria-pressed in sync.
    assert "setAttribute('aria-pressed'" in js

    # Keyboard-resizable separator.
    assert "resizer.tabIndex = 0" in js
    assert "applyWidth(next)" in js


def test_analyze_server_html_aria_presence_smoke() -> None:
    """Presence smoke, not an a11y guarantee. Asserts the analyze dashboard
    server HTML *contains* a tablist with aria-selected, an aria-pressed/
    role=group language toggle, a role=listbox markers list, and a role=dialog
    frame-preview modal. Presence of these strings does not prove the runtime
    a11y behavior (see module TODO: real a11y needs Playwright + axe-core)."""
    analyze_html = _render_analyze_shell()

    assert 'role="tablist"' in analyze_html
    assert 'role="tab"' in analyze_html
    assert 'aria-selected="true"' in analyze_html
    assert 'aria-selected="false"' in analyze_html
    assert 'role="tabpanel"' in analyze_html
    assert 'role="group" aria-label="Language"' in analyze_html
    assert 'aria-pressed="false"' in analyze_html
    assert 'role="listbox"' in analyze_html
    assert 'class="frame-modal" role="dialog"' in analyze_html


def test_focus_visible_rings_are_served_presence_smoke() -> None:
    """Presence smoke, not an a11y guarantee. Asserts :focus-visible ring rules
    are *present* for the keyboard-reachable custom controls in both the report
    and dashboard stylesheets. Presence of the selector does not prove the ring
    is actually visible / contrast-sufficient at runtime (see module TODO: real
    a11y needs Playwright + axe-core)."""
    report_css = load_css()
    theme_css = load_css_screenscribe_theme()
    dashboard_css = assets.load_css_analyze_dashboard()

    # Global baseline ring in the theme covers buttons/links/[tabindex].
    assert "[tabindex]):focus-visible" in theme_css

    # Report: explicit rings on tabs, lang toggle, toolbar, resizer.
    assert ".lang-toggle button:focus-visible" in report_css
    assert ".lightbox-toolbar .tool-btn:focus-visible" in report_css
    assert ".sidebar-resizer:focus-visible" in report_css

    # Dashboard: rings on tabs, mark-frame, export buttons, resizer.
    assert ".sidebar-resizer:focus-visible" in dashboard_css
    assert ".mark-frame-btn:focus-visible" in dashboard_css
