"""The shipped example must feed findings through the renderer correctly.

Regression guard for the advertised ``examples/`` artifacts. The Pro renderer
reads severity/summary/action_items from each finding's ``unified_analysis``
sub-dict and computes its JS seek target from a NUMERIC ``timestamp``. The
example used to put the AI fields at the TOP level and store ``timestamp`` as a
display string ("00:04"), so the generated example HTML rendered every finding
with the fallback severity, no summary, no action items, and a 0.0 seek target —
a broken advertisement of the product.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from screenscribe.html_pro.renderer import render_html_report_pro

_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "examples" / "generate_example.py"


def _load_example_module():
    spec = importlib.util.spec_from_file_location("generate_example", _EXAMPLE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render_example_html() -> str:
    example = _load_example_module()
    return render_html_report_pro(
        video_name=example.VIDEO_NAME,
        video_path=None,
        generated_at=example.GENERATED_AT,
        executive_summary=example.EXECUTIVE_SUMMARY,
        findings=example.FINDINGS,
        segments=example.SEGMENTS,
        errors=None,
        embed_video=False,
        language=example.LANGUAGE,
    )


def test_example_renders_real_per_finding_severities() -> None:
    """The example carries high/medium/low findings — each must survive to HTML."""
    html = _render_example_html()

    # The fictional sample has one high, one medium, one low finding.
    assert 'class="severity-badge severity-high"' in html
    assert 'class="severity-badge severity-medium"' in html
    assert 'class="severity-badge severity-low"' in html
    # No finding should collapse to the renderer's missing-data fallback badge
    # (".severity-none" still exists as a CSS rule, so match the badge usage).
    assert 'class="severity-badge severity-none"' not in html


def test_example_renders_summaries_and_action_items() -> None:
    """AI fields nested under unified_analysis reach the SERVER-rendered report.

    Asserts on the rendered markup (not just substrings): the renderer only
    emits ``finding-summary`` / ``ai-suggestions`` blocks when the summary and
    action items are present under ``unified_analysis``. (Raw field values also
    leak into the embedded findings JSON, so a plain substring check would pass
    even with the old top-level shape — hence the markup-class assertions.)
    """
    html = _render_example_html()

    assert 'class="finding-summary"' in html
    assert "Save reports success but the note is not persisted across reload." in html
    assert 'class="ai-suggestions"' in html
    assert "Confirm the write reaches the backend" in html


def test_example_timestamps_are_numeric_and_nonzero() -> None:
    """Seek targets come from numeric ``timestamp`` — not coerced to 0.0."""
    html = _render_example_html()

    # Finding at 4.0s must emit a real numeric seek target, not the 0.0 fallback.
    assert 'data-timestamp="4.0"' in html
    assert 'data-timestamp="9.5"' in html
    assert 'data-timestamp="15.0"' in html
