"""Unit tests for the concept-based text similarity helpers."""

from screenscribe.text_similarity import _normalize_text_for_similarity, _text_similarity


class TestNormalize:
    def test_lowercases_and_strips_punctuation(self) -> None:
        assert _normalize_text_for_similarity("Button, broken!") == {"button", "broken"}

    def test_drops_stopwords_and_short_non_keyword_tokens(self) -> None:
        # "the", "is", "a" are stopwords; "x" is a short non-keyword token.
        assert _normalize_text_for_similarity("the button is a x") == {"button"}

    def test_keeps_ui_ux_ai_and_digits(self) -> None:
        out = _normalize_text_for_similarity("UI UX AI 5")
        assert {"ui", "ux", "ai", "5"} <= out

    def test_maps_polish_number_words_to_digits(self) -> None:
        assert "3" in _normalize_text_for_similarity("trzy pozycje")

    def test_stems_known_polish_forms(self) -> None:
        out = _normalize_text_for_similarity("listy pozycji")
        assert "lista" in out
        assert "pozycja" in out

    def test_all_stopwords_yields_empty_set(self) -> None:
        assert _normalize_text_for_similarity("the is a") == set()


class TestSimilarity:
    def test_identical_concept_text_is_maximally_similar(self) -> None:
        # "dropdown" + "menu" are two shared key concepts -> concept-weighted path.
        assert _text_similarity("broken dropdown menu", "broken dropdown menu") == 1.0

    def test_empty_input_returns_zero(self) -> None:
        assert _text_similarity("", "anything meaningful") == 0.0

    def test_all_stopwords_returns_zero(self) -> None:
        assert _text_similarity("the is a", "the is a") == 0.0

    def test_disjoint_texts_are_dissimilar(self) -> None:
        assert _text_similarity("blue ocean sky", "purple mountain river") == 0.0

    def test_shared_concepts_score_higher_than_disjoint(self) -> None:
        shared = _text_similarity("dodaj pozycje do listy", "skroc liste pozycji")
        disjoint = _text_similarity("dodaj pozycje do listy", "purple mountain river")
        assert shared > disjoint
        assert shared > 0.0

    def test_partial_overlap_between_zero_and_one(self) -> None:
        s = _text_similarity("broken dropdown menu button", "dropdown menu works")
        assert 0.0 < s < 1.0
