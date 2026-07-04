"""Script-breakout safety for JSON islands in both HTML renderers.

Model/STT/VLM text (summaries, fixes, transcript segments) is embedded raw
inside <script> blocks via json.dumps, which does NOT escape "</". A malicious
model response could therefore close the <script> tag and inject live DOM.
These tests pin the fix: every JSON island escapes "<" so the HTML parser
never sees a tag boundary, while JSON.parse/JS still read identical values.
"""

from __future__ import annotations

import json
import re

from screenscribe.html_pro.data import prepare_findings_json, prepare_segments_json
from screenscribe.html_pro.renderer import render_html_report_pro
from screenscribe.transcribe import Segment

PAYLOAD = "</script><img src=x onerror=alert(1)>"

_FINDINGS_ISLAND = re.compile(
    r'<script id="original-findings" type="application/json">\s*(.*?)\s*</script>',
    re.DOTALL,
)
_SEGMENTS_ISLAND = re.compile(r"window\.TRANSCRIPT_SEGMENTS = (\[.*?\]);")


def _malicious_findings() -> list[dict[str, object]]:
    """Finding with the payload in every model-controlled text field."""
    return [
        {
            "timestamp": 1.0,
            "summary": PAYLOAD,
            "transcript": f"narrator says {PAYLOAD}",
            "unified_analysis": {
                "is_issue": True,
                "severity": "high",
                "summary": PAYLOAD,
                "suggested_fix": PAYLOAD,
            },
        }
    ]


def test_pro_renderer_escapes_script_breakout_in_findings() -> None:
    rendered = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-06-12T00:00:00",
        executive_summary="",
        findings=_malicious_findings(),
        segments=[],
        errors=[],
    )

    assert PAYLOAD not in rendered

    island = _FINDINGS_ISLAND.search(rendered)
    assert island, "findings JSON island missing from Pro report"
    # No "<" may survive inside the island — that is what keeps the HTML
    # parser from ever seeing </script> in user-controlled data.
    assert "<" not in island.group(1)
    # JSON.parse must still recover the exact payload (round-trip intact).
    parsed = json.loads(island.group(1))
    assert parsed[0]["summary"] == PAYLOAD
    assert parsed[0]["unified_analysis"]["suggested_fix"] == PAYLOAD


def test_pro_renderer_escapes_script_breakout_in_segments() -> None:
    rendered = render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-06-12T00:00:00",
        executive_summary="",
        findings=[],
        segments=[Segment(id=0, start=0.0, end=2.5, text=f"speaker: {PAYLOAD}")],
        errors=[],
    )

    assert PAYLOAD not in rendered

    island = _SEGMENTS_ISLAND.search(rendered)
    assert island, "TRANSCRIPT_SEGMENTS island missing from Pro report"
    assert "<" not in island.group(1)
    parsed = json.loads(island.group(1))
    assert parsed[0]["text"] == f"speaker: {PAYLOAD}"
    assert parsed[0]["id"] == 0
    assert parsed[0]["start"] == 0.0
    assert parsed[0]["end"] == 2.5


def test_prepare_findings_json_escapes_all_angle_brackets() -> None:
    out = prepare_findings_json([{"summary": PAYLOAD, "note": "<!-- <script x"}])
    assert "<" not in out
    parsed = json.loads(out)
    assert parsed[0]["summary"] == PAYLOAD
    assert parsed[0]["note"] == "<!-- <script x"


def test_prepare_segments_json_escapes_and_keeps_player_shape() -> None:
    out = prepare_segments_json([Segment(id=3, start=1.0, end=2.0, text=PAYLOAD + "\u2028line")])
    assert "<" not in out
    assert "\u2028" not in out  # raw JS line terminators are escaped too
    parsed = json.loads(out)
    assert parsed == [{"id": 3, "start": 1.0, "end": 2.0, "text": PAYLOAD + "\u2028line"}]

    assert prepare_segments_json(None) == "[]"
    assert prepare_segments_json([]) == "[]"


# ---------------------------------------------------------------------------
# Attribute / JS-context breakout coverage (PKG1 C1.1/C1.2/C1.3 effect pins).
#
# Model/STT-controlled fields (id, severity, timestamp, category, verdict) are
# interpolated by _render_finding() into HTML *attributes* and (formerly) inline
# JS. These tests prove that an attribute-breakout payload cannot create a live
# attribute/handler in the rendered DOM markup.
#
# NOTE: the report also embeds the raw findings as a JSON island inside
# <script type="application/json">. Those islands legitimately carry the raw
# payload text but are inert (the angle-bracket escaping in the script-breakout
# tests above guarantees the parser never sees a tag boundary). To assert on the
# *live* DOM markup we strip every <script> block before checking.
# ---------------------------------------------------------------------------

_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_SEVERITY_CLASS = re.compile(r'class="severity-badge (severity-[a-z]+)"')
_DATA_TIMESTAMP = re.compile(r'data-timestamp="([^"]*)"')
_ALLOWED_SEVERITY_CLASSES = {
    "severity-critical",
    "severity-high",
    "severity-medium",
    "severity-low",
    "severity-none",
}


def _render_one(finding: dict[str, object]) -> str:
    return render_html_report_pro(
        video_name="test.mov",
        video_path=None,
        generated_at="2026-06-12T00:00:00",
        executive_summary="",
        findings=[finding],
        segments=[],
        errors=[],
    )


def _markup_without_scripts(rendered: str) -> str:
    """Return rendered HTML with all <script> blocks removed (live DOM only)."""
    return _SCRIPT_BLOCK.sub("", rendered)


def test_severity_attribute_breakout_is_inert() -> None:
    payload = 'high" onmouseover=alert(1) x="'
    markup = _markup_without_scripts(
        _render_one({"id": 1, "unified_analysis": {"severity": payload}})
    )

    # The class-attr sink must not carry the injected handler.
    assert "onmouseover=" not in markup
    match = _SEVERITY_CLASS.search(markup)
    assert match, "severity-badge class missing from rendered finding"
    assert match.group(1) in _ALLOWED_SEVERITY_CLASSES
    assert match.group(1) == "severity-none"  # non-allowlist payload -> fallback
    # data-severity / aria-label are clamped too.
    assert 'data-severity="none"' in markup


def test_severity_legal_value_passes_through() -> None:
    markup = _markup_without_scripts(
        _render_one({"id": 1, "unified_analysis": {"severity": "critical"}})
    )
    assert 'class="severity-badge severity-critical"' in markup
    assert 'data-severity="critical"' in markup


def test_finding_id_attribute_breakout_is_inert() -> None:
    payload = '1" onload="alert(1)'
    markup = _markup_without_scripts(
        _render_one({"id": payload, "unified_analysis": {"severity": "high"}})
    )

    # No raw quote breakout into a new attribute/handler.
    assert '" onload=' not in markup
    assert 'onload="alert(1)"' not in markup
    # The inner quote is HTML-escaped inside the attribute value instead.
    assert "&quot;" in markup


def test_timestamp_js_context_injection_is_inert() -> None:
    payload = "0);alert(1)//"
    markup = _markup_without_scripts(
        _render_one({"id": 1, "timestamp": payload, "unified_analysis": {"severity": "high"}})
    )

    assert "alert(1)" not in markup
    assert 'onclick="seekToTimestamp' not in markup  # inline seek handler removed (C1.3)
    assert 'class="finding-meta" data-timestamp=' in markup  # delegation hook present
    match = _DATA_TIMESTAMP.search(markup)
    assert match, "data-timestamp attribute missing from rendered finding"
    # Value must be a plain number (float-coerced fallback), never the payload.
    assert re.fullmatch(r"-?\d+(\.\d+)?", match.group(1))
    assert float(match.group(1)) == 0.0


def test_timestamp_legal_value_is_preserved() -> None:
    markup = _markup_without_scripts(
        _render_one({"id": 1, "timestamp": "3.5", "unified_analysis": {"severity": "high"}})
    )
    match = _DATA_TIMESTAMP.search(markup)
    assert match
    assert float(match.group(1)) == 3.5


def test_category_tag_injection_is_inert() -> None:
    payload = "<img src=x onerror=alert(1)>"
    markup = _markup_without_scripts(
        _render_one({"id": 1, "category": payload, "unified_analysis": {"severity": "high"}})
    )

    # category renders as an escaped text node, never a live tag/handler.
    assert "<img src=x" not in markup
    assert "&lt;" in markup  # angle brackets escaped


def test_verdict_payload_does_not_leak_to_data_verdict() -> None:
    payload = '" onmouseover="alert(1)'
    markup = _markup_without_scripts(
        _render_one(
            {
                "id": 1,
                "verdict": payload,
                "unified_analysis": {"severity": "high", "verdict": payload},
            }
        )
    )

    # data-verdict is hardcoded empty in the initial render; payload must not leak.
    assert 'data-verdict=""' in markup
    assert 'onmouseover="alert(1)"' not in markup
