"""Tests for bug and change detection logic."""

import pytest

from screenscribe.detect import (
    BUG_KEYWORDS,
    CHANGE_KEYWORDS,
    UI_KEYWORDS,
    Detection,
    detect_issues,
    format_timestamp,
    merge_consecutive_detections,
)
from screenscribe.transcribe import Segment, TranscriptionResult

# --- Fixtures ---


@pytest.fixture
def empty_transcription() -> TranscriptionResult:
    """Empty transcription with no segments."""
    return TranscriptionResult(text="", segments=[], language="pl")


@pytest.fixture
def simple_transcription() -> TranscriptionResult:
    """Simple transcription with a few segments."""
    return TranscriptionResult(
        text="To nie działa. Trzeba to poprawić. Layout jest ok.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="To nie działa."),
            Segment(id=1, start=2.0, end=4.0, text="Trzeba to poprawić."),
            Segment(id=2, start=4.0, end=6.0, text="Layout jest ok."),
        ],
        language="pl",
    )


@pytest.fixture
def no_issues_transcription() -> TranscriptionResult:
    """Transcription with no detectable issues."""
    return TranscriptionResult(
        text="Wszystko wygląda świetnie. Super robota.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="Wszystko wygląda świetnie."),
            Segment(id=1, start=2.0, end=4.0, text="Super robota."),
        ],
        language="pl",
    )


@pytest.fixture
def consecutive_bugs_transcription() -> TranscriptionResult:
    """Transcription with consecutive bug mentions (for merge testing)."""
    return TranscriptionResult(
        text="To nie działa. Błąd tutaj. I problem tam.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="To nie działa."),
            Segment(id=1, start=2.5, end=4.0, text="Błąd tutaj."),
            Segment(id=2, start=4.5, end=6.0, text="I problem tam."),
        ],
        language="pl",
    )


@pytest.fixture
def mixed_categories_transcription() -> TranscriptionResult:
    """Transcription with mixed categories close together."""
    return TranscriptionResult(
        text="Bug tutaj. Trzeba zmienić. Button nie działa.",
        segments=[
            Segment(id=0, start=0.0, end=2.0, text="Bug tutaj."),
            Segment(id=1, start=2.5, end=4.0, text="Trzeba zmienić."),
            Segment(id=2, start=4.5, end=6.0, text="Button nie działa."),
        ],
        language="pl",
    )


# --- Test detect_issues ---


class TestDetectIssues:
    """Tests for detect_issues function."""

    def test_empty_transcription(self, empty_transcription: TranscriptionResult) -> None:
        """Empty transcription returns no detections."""
        result = detect_issues(empty_transcription)
        assert result == []

    def test_no_issues(self, no_issues_transcription: TranscriptionResult) -> None:
        """Transcription without keywords returns no detections."""
        result = detect_issues(no_issues_transcription)
        assert result == []

    def test_detects_bug_keywords(self, simple_transcription: TranscriptionResult) -> None:
        """Bug keywords are detected with correct category."""
        result = detect_issues(simple_transcription)
        bug_detections = [d for d in result if d.category == "bug"]
        assert len(bug_detections) >= 1
        assert any("nie działa" in d.keywords_found for d in bug_detections)

    def test_detects_change_keywords(self, simple_transcription: TranscriptionResult) -> None:
        """Change keywords are detected with correct category."""
        result = detect_issues(simple_transcription)
        change_detections = [d for d in result if d.category == "change"]
        assert len(change_detections) >= 1
        assert any("trzeba" in d.keywords_found for d in change_detections)

    def test_detects_ui_keywords(self, simple_transcription: TranscriptionResult) -> None:
        """UI keywords are detected with correct category."""
        result = detect_issues(simple_transcription)
        ui_detections = [d for d in result if d.category == "ui"]
        assert len(ui_detections) >= 1
        assert any("layout" in d.keywords_found for d in ui_detections)

    def test_context_includes_surrounding_segments(
        self, simple_transcription: TranscriptionResult
    ) -> None:
        """Context includes text from surrounding segments."""
        result = detect_issues(simple_transcription, context_window=1)
        # First detection should have context from next segment
        if result:
            assert len(result[0].context) > len(result[0].segment.text)

    def test_bug_category_priority(self) -> None:
        """Bug category takes priority when multiple categories match."""
        transcription = TranscriptionResult(
            text="Ten button nie działa",
            segments=[Segment(id=0, start=0.0, end=2.0, text="Ten button nie działa")],
            language="pl",
        )
        result = detect_issues(transcription)
        # "button" is UI, "nie działa" is bug - bug should win
        assert len(result) == 1
        assert result[0].category == "bug"

    def test_english_keywords(self) -> None:
        """English keywords are detected."""
        transcription = TranscriptionResult(
            text="This is broken. We should fix it.",
            segments=[
                Segment(id=0, start=0.0, end=2.0, text="This is broken."),
                Segment(id=1, start=2.0, end=4.0, text="We should fix it."),
            ],
            language="en",
        )
        result = detect_issues(transcription)
        assert len(result) >= 2
        categories = {d.category for d in result}
        assert "bug" in categories
        assert "change" in categories


# --- Test merge_consecutive_detections ---


class TestMergeConsecutiveDetections:
    """Tests for merge_consecutive_detections function."""

    def test_empty_list(self) -> None:
        """Empty list returns empty list."""
        result = merge_consecutive_detections([])
        assert result == []

    def test_single_detection(self) -> None:
        """Single detection is returned unchanged."""
        detection = Detection(
            segment=Segment(id=0, start=0.0, end=2.0, text="Bug"),
            category="bug",
            keywords_found=["bug"],
            context="Bug context",
        )
        result = merge_consecutive_detections([detection])
        assert len(result) == 1
        assert result[0] == detection

    def test_merges_same_category_within_gap(
        self, consecutive_bugs_transcription: TranscriptionResult
    ) -> None:
        """Consecutive detections of same category within max_gap are merged."""
        detections = detect_issues(consecutive_bugs_transcription)
        # Before merge, we might have multiple bug detections
        # After merge (done inside detect_issues), they should be combined
        bug_detections = [d for d in detections if d.category == "bug"]
        # All bugs within 5s should be merged into one
        assert len(bug_detections) <= 1

    def test_does_not_merge_different_categories(
        self, mixed_categories_transcription: TranscriptionResult
    ) -> None:
        """Different categories are not merged even if close together."""
        result = detect_issues(mixed_categories_transcription)
        categories = [d.category for d in result]
        # Should have at least 2 different categories
        assert len(set(categories)) >= 2

    def test_does_not_merge_beyond_gap(self) -> None:
        """Detections beyond max_gap are not merged."""
        detections = [
            Detection(
                segment=Segment(id=0, start=0.0, end=1.0, text="Bug 1"),
                category="bug",
                keywords_found=["bug"],
                context="context 1",
            ),
            Detection(
                segment=Segment(id=1, start=10.0, end=11.0, text="Bug 2"),
                category="bug",
                keywords_found=["bug"],
                context="context 2",
            ),
        ]
        result = merge_consecutive_detections(detections, max_gap=5.0)
        assert len(result) == 2

    def test_merged_detection_combines_keywords(self) -> None:
        """Merged detection combines keywords from both."""
        detections = [
            Detection(
                segment=Segment(id=0, start=0.0, end=1.0, text="Bug"),
                category="bug",
                keywords_found=["bug"],
                context="context 1",
            ),
            Detection(
                segment=Segment(id=1, start=2.0, end=3.0, text="Error"),
                category="bug",
                keywords_found=["error"],
                context="context 2",
            ),
        ]
        result = merge_consecutive_detections(detections, max_gap=5.0)
        assert len(result) == 1
        assert "bug" in result[0].keywords_found
        assert "error" in result[0].keywords_found

    def test_merged_detection_extends_time_range(self) -> None:
        """Merged detection has start of first and end of last."""
        detections = [
            Detection(
                segment=Segment(id=0, start=1.0, end=2.0, text="Bug"),
                category="bug",
                keywords_found=["bug"],
                context="context 1",
            ),
            Detection(
                segment=Segment(id=1, start=3.0, end=5.0, text="Error"),
                category="bug",
                keywords_found=["error"],
                context="context 2",
            ),
        ]
        result = merge_consecutive_detections(detections, max_gap=5.0)
        assert len(result) == 1
        assert result[0].segment.start == 1.0
        assert result[0].segment.end == 5.0

    def test_merged_detection_carries_stt_confidence_metadata(self) -> None:
        """W1A-16: the merge must carry the leading segment's STT confidence
        metadata (no_speech_prob / avg_logprob / compression_ratio) into the
        merged segment instead of silently resetting it to the 0.0 defaults.

        Without this, a merged detection loses the decode signals the
        anti-hallucination filter and audio-quality validator rely on, so a
        merged span can no longer be judged as (non-)speech.
        """
        detections = [
            Detection(
                segment=Segment(
                    id=0,
                    start=0.0,
                    end=2.0,
                    text="Bug",
                    no_speech_prob=0.42,
                    avg_logprob=-0.31,
                    compression_ratio=1.7,
                ),
                category="bug",
                keywords_found=["bug"],
                context="context 1",
            ),
            Detection(
                segment=Segment(
                    id=1,
                    start=3.0,
                    end=5.0,
                    text="Error",
                    no_speech_prob=0.9,
                    avg_logprob=-1.4,
                    compression_ratio=3.1,
                ),
                category="bug",
                keywords_found=["error"],
                context="context 2",
            ),
        ]
        result = merge_consecutive_detections(detections, max_gap=5.0)
        assert len(result) == 1
        merged = result[0].segment
        # Leading segment's metadata is carried, not reset to the 0.0 defaults.
        assert merged.no_speech_prob == 0.42
        assert merged.avg_logprob == -0.31
        assert merged.compression_ratio == 1.7


# --- Test format_timestamp ---


class TestFormatTimestamp:
    """Tests for format_timestamp function."""

    def test_zero_seconds(self) -> None:
        """Zero seconds formats as 00:00."""
        assert format_timestamp(0) == "00:00"

    def test_seconds_only(self) -> None:
        """Seconds under a minute format correctly."""
        assert format_timestamp(45) == "00:45"

    def test_minutes_and_seconds(self) -> None:
        """Minutes and seconds format correctly."""
        assert format_timestamp(125) == "02:05"

    def test_large_minutes(self) -> None:
        """Large minute values format correctly."""
        assert format_timestamp(3661) == "61:01"

    def test_fractional_seconds(self) -> None:
        """Fractional seconds are truncated."""
        assert format_timestamp(65.7) == "01:05"


# --- Test keywords coverage ---


class TestKeywordsCoverage:
    """Tests to ensure keyword lists are comprehensive."""

    def test_bug_keywords_not_empty(self) -> None:
        """Bug keywords list is not empty."""
        assert len(BUG_KEYWORDS) > 0

    def test_change_keywords_not_empty(self) -> None:
        """Change keywords list is not empty."""
        assert len(CHANGE_KEYWORDS) > 0

    def test_ui_keywords_not_empty(self) -> None:
        """UI keywords list is not empty."""
        assert len(UI_KEYWORDS) > 0

    def test_bug_keywords_include_polish(self) -> None:
        """Bug keywords include Polish words."""
        polish_patterns = ["błąd", "nie działa", "problem"]
        found = [p for p in polish_patterns if any(p in k for k in BUG_KEYWORDS)]
        assert len(found) > 0

    def test_bug_keywords_include_english(self) -> None:
        """Bug keywords include English words."""
        english_patterns = ["bug", "error", "broken"]
        found = [p for p in english_patterns if any(p in k for k in BUG_KEYWORDS)]
        assert len(found) > 0
