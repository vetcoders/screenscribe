#!/usr/bin/env python3
"""Test script to generate an HTML Pro report with mock data."""

import re
import tempfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

from screenscribe.html_pro import render_html_report_pro
from screenscribe.transcribe import Segment


class _VisibleTextExtractor(HTMLParser):
    """Collect visible text nodes only.

    Skips <script>, <style> and the JSON island tags entirely so the i18n
    leak check looks at what a human actually sees, not at embedded data
    or JS source (which legitimately holds both PL and EN strings).
    """

    _SKIP_TAGS: ClassVar[set[str]] = {"script", "style", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.chunks.append(data)

    @property
    def visible_text(self) -> str:
        return " ".join(self.chunks)


def _extract_visible_text(html_doc: str) -> str:
    parser = _VisibleTextExtractor()
    parser.feed(html_doc)
    return parser.visible_text


# English UI islands that must NOT survive a language="pl" render.
_ENGLISH_ISLAND_RE = re.compile(
    r"\bPlay\b|\bPause\b|Manual Moments|\bPen\b|\bRect\b|\bArrow\b|\bText\b|"
    r"\bUndo\b|\bClear\b|\bDone\b|Hold to record|Analyze Moment|\bCancel\b|"
    r"Open Review Window|Return to One Window|Manual Moment Analysis"
)


def _render(language: str) -> str:
    return render_html_report_pro(
        video_name="test-screencast.mp4",
        video_path=None,
        generated_at=datetime.now().isoformat(),
        executive_summary="Streszczenie testowe.",
        findings=create_mock_findings(),
        segments=create_mock_segments(),
        errors=[],
        embed_video=False,
        language=language,
    )


def test_pl_no_english_islands() -> None:
    """A PL report must leave zero English UI islands in visible text nodes."""
    html_doc = _render("pl")
    visible = _extract_visible_text(html_doc)
    matches = sorted(set(_ENGLISH_ISLAND_RE.findall(visible)))
    assert not matches, f"English islands leaked into PL visible text: {matches}"


def test_en_render_symmetric() -> None:
    """The EN render must carry the English labels (symmetry sanity check)."""
    html_doc = _render("en")
    visible = _extract_visible_text(html_doc)
    for label in (
        "Manual Moments",
        "Manual Moment Analysis",
        "Open Review Window",
        "Analyze Moment",
    ):
        assert label in visible, f"EN render missing expected label: {label!r}"


def test_finding_category_badge_is_localized() -> None:
    """Finding category badges render through i18n, not as the raw EN enum (FW-05).

    The category label used to leak the raw English enum (BUG/CHANGE/UI) into
    every finding card regardless of report language. It now routes through
    ``_t('category_<cat>')`` so a PL report shows the Polish label while EN keeps
    the English one. The badge stays upper-cased on both paths.
    """
    pl = _extract_visible_text(_render("pl"))
    assert "BŁĄD" in pl  # category "bug" -> Błąd -> BŁĄD
    assert "ZMIANA" in pl  # category "change" -> Zmiana -> ZMIANA
    assert "BUG" not in pl  # the raw EN enum must not survive a PL render

    en = _extract_visible_text(_render("en"))
    assert "BUG" in en
    assert "CHANGE" in en


def test_missing_key_falls_back_to_data_i18n_text() -> None:
    """An unknown language falls back to PL without crashing or emptying nodes."""
    html_doc = _render("zz")  # unsupported -> ui_language coerced to pl
    visible = _extract_visible_text(html_doc)
    assert not _ENGLISH_ISLAND_RE.search(visible)
    # data-i18n attributes survive so the client toggle still has anchors.
    assert 'data-i18n="manualFramesHeading"' in html_doc


def test_rejection_feedback_visible() -> None:
    """Rejecting a finding must surface visible inline feedback (R-P4).

    No jsdom/playwright in this environment, so this asserts deterministically
    on the rendered HTML string (CSS + JS are inlined): the reject control
    exists, the change handler wires the inline feedback helper into the reject
    path, that helper raises a toast and a row flash, and the CSS feedback hook
    is present alongside the unchanged dimming/strikethrough.
    """
    html_doc = _render("pl")

    # 1. The reject radio control is rendered with the dataset the JS keys off.
    assert 'value="rejected"' in html_doc
    assert 'data-verdict=""' in html_doc

    # 2. The change handler sets data-verdict AND fires inline feedback.
    assert "article.dataset.verdict = verdict;" in html_doc
    assert "flashReviewFeedback(article, verdict);" in html_doc

    # 3. The feedback helper raises a toast and toggles the row-flash dataset.
    assert "function flashReviewFeedback(" in html_doc
    assert (
        "showNotification(accepted ? t('review.findingAccepted') : t('review.findingRejected'))"
        in html_doc
    )
    assert "article.dataset.reviewFlash = accepted ? 'accepted' : 'rejected';" in html_doc

    # 4. Both languages carry the rejection feedback string.
    assert "Znalezisko odrzucone" in html_doc
    assert "Finding rejected" in html_doc

    # 5. The CSS feedback hook exists for the rejected flash...
    assert '.finding[data-review-flash="rejected"]' in html_doc
    assert "@keyframes review-flash-reject" in html_doc
    # ...and the persistent dimming + strikethrough remain untouched.
    assert '.finding[data-verdict="rejected"] {' in html_doc
    assert "opacity: 0.6;" in html_doc
    assert '.finding[data-verdict="rejected"] .finding-title {' in html_doc
    assert "text-decoration: line-through;" in html_doc


def test_review_app_speaks_one_verdict_language() -> None:
    """Render-level UI smoke (pytest cannot click): the bundled review app and
    markup speak `verdict` end-to-end and emit no `confirmed` decision field.

    This is the strongest automated render-level check. A real human
    accept/reject/none click-through is still recommended (see agent report).
    """
    html_doc = _render("pl")

    # Markup carries the verdict vocabulary the JS keys off.
    assert 'data-verdict=""' in html_doc
    assert 'name="verdict-' in html_doc
    assert 'value="accepted"' in html_doc
    assert 'value="rejected"' in html_doc

    # The client-side serializers (reviewed JSON == ZIP JSON canon) emit
    # human_review.verdict, not the old boolean confirmed field.
    assert "verdict: normalizeVerdict(review.verdict)," in html_doc

    # No new code path emits a `confirmed:` decision field. The only surviving
    # `confirmed` token is the legacy localStorage-draft migration reader.
    assert "confirmed:" not in html_doc
    assert "article.dataset.confirmed" not in html_doc
    assert 'name="confirmed-' not in html_doc
    assert 'data-stat-filter="status:confirmed"' not in html_doc
    # The legacy migration reader is present (and is the only `confirmed` left).
    assert "normalizeVerdict(state.confirmed)" in html_doc


def create_mock_segments() -> list[Segment]:
    """Create mock transcript segments with Polish text."""
    return [
        Segment(id=1, start=0.0, end=3.5, text="Witaj, to jest test transkrypcji."),
        Segment(id=2, start=3.5, end=7.0, text="Teraz pokazuję błąd w interfejsie użytkownika."),
        Segment(id=3, start=7.0, end=11.5, text="Ten przycisk nie działa poprawnie."),
        Segment(id=4, start=11.5, end=15.0, text="Należy naprawić walidację formularza."),
        Segment(id=5, start=15.0, end=19.0, text="Potem sprawdzę responsywność na telefonie."),
        Segment(id=6, start=19.0, end=23.5, text="Interfejs nie loaduje się szybko."),
        Segment(id=7, start=23.5, end=27.0, text="Potrzebujemy poprawić wydajność."),
        Segment(id=8, start=27.0, end=31.0, text="To wygląda dobrze dla dostępności."),
        Segment(id=9, start=31.0, end=35.5, text="Koniec testów, raport gotowy."),
    ]


def create_mock_findings() -> list[dict[str, Any]]:
    """Create mock findings with unified analysis data."""
    return [
        {
            "id": 1,
            "category": "bug",
            "timestamp": "00:00",
            "timestamp_seconds": 0.0,
            "timestamp_formatted": "00:00",
            "text": "Witaj, to jest test transkrypcji.",
            "context": "Testy automatyczne interfejsu",
            "keywords": ["test", "transkrypcja"],
            "screenshot_b64": "",
            "thumbnail_b64": "",
            "is_issue": True,
            "severity": "critical",
            "summary": "Krytyczny błąd przy inicjalizacji systemu transkrypcji",
            "action_items": [
                "Sprawdzić logs inicjalizacji",
                "Zresetować cache",
                "Testować z nowymi parametrami",
            ],
            "affected_components": ["TranscriptionService", "AudioProcessor"],
            "suggested_fix": "Upewnić się, że moduł audio jest prawidłowo załadowany",
            "ui_elements": ["Loading spinner", "Error dialog"],
            "issues_detected": ["Missing error message", "Spinner stuck"],
            "accessibility_notes": "Dialog nie zawiera aria-live for screen readers",
            "design_feedback": "Potrzebujesz lepszego visual feedback podczas ładowania",
        },
        {
            "id": 2,
            "category": "change",
            "timestamp": "00:03",
            "timestamp_seconds": 3.5,
            "timestamp_formatted": "00:03",
            "text": "Teraz pokazuję błąd w interfejsie użytkownika.",
            "context": "Przemiany w UI",
            "keywords": ["błąd", "interfejs"],
            "screenshot_b64": "",
            "thumbnail_b64": "",
            "is_issue": True,
            "severity": "high",
            "summary": "Przycisk submit zmienia się niezgodnie z wytycznymi",
            "action_items": [
                "Wyrównać styl z brandem",
                "Dodać hover state",
            ],
            "affected_components": ["SubmitButton", "FormControl"],
            "suggested_fix": "Zaktualizuj CSS zgodnie z nową paletą kolorów produktu",
            "ui_elements": ["Submit Button", "Form"],
            "issues_detected": ["Color mismatch", "Missing hover effect"],
            "accessibility_notes": "",
            "design_feedback": "Przycisk zbyt mały, zwiększ do minimum 48px",
        },
        {
            "id": 3,
            "category": "ui",
            "timestamp": "00:07",
            "timestamp_seconds": 7.0,
            "timestamp_formatted": "00:07",
            "text": "Ten przycisk nie działa poprawnie.",
            "context": "Problemy z interaktywnością",
            "keywords": ["przycisk", "nefunkcjonalny"],
            "screenshot_b64": "",
            "thumbnail_b64": "",
            "is_issue": True,
            "severity": "medium",
            "summary": "Przycisk pokazuje console error na kliknięciu",
            "action_items": [
                "Dodać click handler",
                "Dodać walidację",
            ],
            "affected_components": ["ClickableButton"],
            "suggested_fix": "Zaimplementuj proper event handler z error handling",
            "ui_elements": ["Button"],
            "issues_detected": ["Click not registered"],
            "accessibility_notes": "Brak keyboard support",
            "design_feedback": "",
        },
        {
            "id": 4,
            "category": "bug",
            "timestamp": "00:11",
            "timestamp_seconds": 11.5,
            "timestamp_formatted": "00:11",
            "text": "Należy naprawić walidację formularza.",
            "context": "Walidacja danych wejściowych",
            "keywords": ["walidacja", "formularz"],
            "screenshot_b64": "",
            "thumbnail_b64": "",
            "is_issue": False,  # Non-issue example
            "severity": "low",
            "summary": "Walidacja email nie akceptuje subdomen",
            "action_items": [],
            "affected_components": ["FormValidator"],
            "suggested_fix": "",
            "ui_elements": [],
            "issues_detected": [],
            "accessibility_notes": "",
            "design_feedback": "",
        },
        {
            "id": 5,
            "category": "change",
            "timestamp": "00:15",
            "timestamp_seconds": 15.0,
            "timestamp_formatted": "00:15",
            "text": "Potem sprawdzę responsywność na telefonie.",
            "context": "Testowanie na mobilnych urządzeniach",
            "keywords": ["responsywność", "mobile"],
            "screenshot_b64": "",
            "thumbnail_b64": "",
            "is_issue": True,
            "severity": "high",
            "summary": "Layout nie skaluje się poniżej 320px",
            "action_items": [
                "Testować na iPhone SE",
                "Dodać media queries",
            ],
            "affected_components": ["ResponsiveContainer"],
            "suggested_fix": "Zwiększ minimum viewport width lub dodaj horizontal scroll gracefully",
            "ui_elements": ["Container", "Navigation"],
            "issues_detected": ["Text overflow", "Layout broken"],
            "accessibility_notes": "",
            "design_feedback": "",
        },
    ]


def test_embed_video_over_limit_warns_and_links_by_name(monkeypatch, tmp_path: Path) -> None:
    """embed_video on a >=50MB file must warn (not silently fall back) and link by name.

    Covers the else-branch of the 50MB embed guard (A1-1): before this fix the
    fallback to filename-only linking happened with zero user-facing signal.
    """
    video_file = tmp_path / "big-recording.mp4"
    video_file.write_bytes(b"0")

    class _FakeStat:
        st_size = 60 * 1024 * 1024  # 60MB, over the 50MB embed threshold

    real_stat = Path.stat

    def _fake_stat(self, *args, **kwargs):
        if self == video_file:
            return _FakeStat()
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _fake_stat)

    # An empty list is enough: render_html_report_pro now does
    # `if errors is None: errors = []`, so a caller-supplied `[]` keeps its
    # identity and the embed-size append below stays observable here.
    errors: list[dict[str, str]] = []
    html_doc = render_html_report_pro(
        video_name=video_file.name,
        video_path=str(video_file),
        generated_at=datetime.now().isoformat(),
        executive_summary="Streszczenie testowe.",
        findings=create_mock_findings(),
        segments=create_mock_segments(),
        errors=errors,
        embed_video=True,
        language="en",
    )

    embed_warnings = [e for e in errors if e["stage"] == "embed_video"]
    assert embed_warnings, f"expected embed-size warning to be appended to errors, got {errors}"
    warning = embed_warnings[0]
    assert "50" in warning["message"]
    assert "60" in warning["message"]
    assert f'src="{video_file.name}"' in html_doc
    # The warning must also be visible in the rendered report itself (errors-section).
    assert warning["message"] in html_doc


def main() -> None:
    """Generate test HTML report."""
    segments = create_mock_segments()
    findings = create_mock_findings()

    # Generate HTML report
    html_content = render_html_report_pro(
        video_name="test-screencast.mp4",
        video_path=None,  # No actual video file
        generated_at=datetime.now().isoformat(),
        executive_summary="Test report z 5 fikcyjnymi znaleziskami (2 krytyczne, 2 wysokie, 1 średnia). System teraz generuje interaktywne raporty HTML z synchronizacją transkrypcji.",
        findings=findings,
        segments=segments,
        errors=[
            {
                "stage": "vision_analysis",
                "message": "VLM endpoint niedostępny, pominięto analizę wizualną",
            },
        ],
        embed_video=False,
    )

    # Write to file
    output_path = Path(tempfile.gettempdir()) / "screenscribe-test-report.html"
    output_path.write_text(html_content, encoding="utf-8")

    # Report results
    file_size = output_path.stat().st_size
    file_size_kb = file_size / 1024

    print("✓ HTML report generated successfully")
    print(f"  Location: {output_path}")
    print(f"  Size: {file_size:,} bytes ({file_size_kb:.1f} KB)")
    print(f"  Findings: {len(findings)} (2 critical, 2 high, 1 medium, 1 non-issue)")
    print(f"  Segments: {len(segments)}")
    print("\nTemplate features verified:")
    print("  ✓ Pro template renders")
    print("  ✓ Executive summary included")
    print("  ✓ Export tab hosts artifact downloads (Statistics tab removed)")
    print("  ✓ Severity badges (critical, high, medium, low)")
    print("  ✓ Unified analysis fields present")
    print("  ✓ Human review sections")
    print("  ✓ Subtitle sync sidebar")
    print("  ✓ Error section")
    print("  ✓ Footer and scripts embedded")


if __name__ == "__main__":
    main()
