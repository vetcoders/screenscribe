"""Asset loader for HTML Pro template.

Loads CSS, JavaScript, and HTML template files from html_pro_assets directory.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

# Asset directory (sibling to this package)
ASSETS_DIR = Path(__file__).parent.parent / "html_pro_assets"


@lru_cache(maxsize=10)
def load_asset(filename: str) -> str:
    """Load an asset file from html_pro_assets directory.

    Args:
        filename: Relative path within html_pro_assets (e.g., "styles/report-pro.css")

    Returns:
        File contents as string

    Raises:
        FileNotFoundError: If asset file doesn't exist
    """
    asset_path = ASSETS_DIR / filename
    if not asset_path.exists():
        raise FileNotFoundError(f"Asset not found: {asset_path}")
    return asset_path.read_text(encoding="utf-8")


def load_css() -> str:
    """Load the HTML Pro report stylesheet."""
    return load_asset("styles/report-pro.css")


def load_css_screenscribe_theme() -> str:
    """Load the screenscribe identity theme override.

    Imported AFTER report-pro.css; remaps existing design tokens onto the
    screenscribe identity (pure monochrome on near-black + a neutral off-white
    interaction tone, no brand hue, with a monochrome severity brightness ramp)
    so existing rules pick up the look without component-level rewrites.
    """
    return load_asset("styles/screenscribe-theme.css")


def load_css_screenscribe_theme_polish() -> str:
    """Load the operator polish layer for the screenscribe theme.

    Loaded LAST, after screenscribe-theme.css (which loads after report-pro.css).
    Purely additive: completes the two-corner viewfinder header lockup, styles
    monochrome instrument scrollbars, adds the blinking footer underscore echo,
    and brackets the finding index into a terminal chip. Reads only existing
    theme tokens; defines no new token. Remove this load line to revert.
    """
    return load_asset("styles/screenscribe-theme.polish.css")


def load_css_analyze_dashboard() -> str:
    """Load analyze-dashboard-only layout and interaction styles."""
    return load_asset("styles/analyze_dashboard.css")


def load_js_video_player() -> str:
    """Load the video player JavaScript."""
    return load_asset("scripts/video_player.js")


def load_js_i18n_runtime() -> str:
    """Load the shared namespaced i18n runtime (REVIEW + ANALYZE)."""
    return load_asset("scripts/i18n.js")


def load_js_lib_tab_keyboard() -> str:
    """Load shared tab keyboard behavior."""
    return load_asset("scripts/lib/tab-keyboard.js")


def load_js_lib_language_control() -> str:
    """Load shared UI language persistence and toggle wiring."""
    return load_asset("scripts/lib/language-control.js")


def load_js_lib_stt_transport() -> str:
    """Load shared speech-to-text transport plumbing."""
    return load_asset("scripts/lib/stt-transport.js")


def load_js_review_app() -> str:
    """Load the review/annotation JavaScript application."""
    return load_asset("scripts/review_app.js")


def load_js_jszip() -> str:
    """Load vendored JSZip (3.10.1), inlined into the report so the Export ZIP
    action works fully offline with no third-party CDN request."""
    return load_asset("vendor/jszip.min.js")


def load_js_analyze_dashboard() -> str:
    """Load the analyze dashboard controller JavaScript."""
    return load_asset("scripts/analyze_dashboard.js")


@lru_cache(maxsize=1)
def load_favicon_data_uri() -> str:
    """Return the screenscribe favicon as a self-contained data URI.

    Base64-encoded so the generated report/dashboard carries its own icon with
    no external request (the static report is a single shippable file) and no
    favicon 404 on the analyze server. SVG renders crisply at every size.
    """
    svg = load_asset("icons/favicon.svg")
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
