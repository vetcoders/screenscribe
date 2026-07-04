"""C7 — Review report header wording (R1) + executive-summary markdown (R2).

R1: the review surface header/wordmark must read ``screenscribe review`` (the mode
is review), never the old ``screenscribe report``.

R2: the executive summary is LLM-authored markdown; it must render to HTML
(``**bold**`` -> ``<strong>``, ``- item`` -> lists) instead of leaking raw
asterisks, while raw HTML in the model output stays escaped (no injection).
"""

from __future__ import annotations

from screenscribe.html_pro.renderer import _render_summary_markdown, render_html_report_pro


def _render(summary: str) -> str:
    return render_html_report_pro(
        video_name="demo.mov",
        video_path=None,
        generated_at="2026-06-30T10:00:00Z",
        executive_summary=summary,
        findings=[],
        segments=[],
    )


def test_review_header_says_review_not_report() -> None:
    out = _render("placeholder summary")
    assert "screenscribe review" in out
    assert "screenscribe report" not in out


def test_executive_summary_renders_bold() -> None:
    out = _render("**Podsumowanie:** problem klasy **MEDIUM**.")
    assert "<strong>Podsumowanie:</strong>" in out
    assert "<strong>MEDIUM</strong>" in out
    assert "**Podsumowanie:**" not in out  # raw markdown must be gone


def test_executive_summary_renders_lists() -> None:
    out = _render("Punkty:\n\n- pierwszy\n- drugi\n")
    assert "<ul>" in out
    assert "<li>pierwszy</li>" in out
    assert "<li>drugi</li>" in out


def test_executive_summary_escapes_raw_html() -> None:
    out = _render("**ok** <script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_executive_summary_does_not_render_remote_images() -> None:
    """An LLM-authored summary must never emit an <img> tag. A remote image URL
    (e.g. via prompt injection) would make the local report beacon a third-party
    host on open — a data-exfil-on-open vector. Disabling the image rule keeps
    the ``![...]`` syntax from producing an auto-loading <img>. (A plain link is
    out of scope here: it does not auto-fetch, it requires a user click.)

    Asserted on the summary renderer directly: the full report legitimately
    carries static <img> template tags (lightbox, frame preview), so the check
    would be meaningless against the whole page.
    """
    html = _render_summary_markdown("**ok** ![x](https://example.com/a.png)")
    assert "<img" not in html
    # ``**bold**`` still renders, so the fix is scoped to the image rule only.
    assert "<strong>ok</strong>" in html
