"""Text similarity helpers shared across the pipeline."""

from __future__ import annotations

import re


def _normalize_text_for_similarity(text: str) -> set[str]:
    """Normalize text for similarity comparison.

    Removes stopwords, normalizes numbers, and extracts meaningful words.
    """
    # Polish and English stopwords
    stopwords = {
        # Polish
        "i",
        "w",
        "z",
        "na",
        "do",
        "że",
        "to",
        "jest",
        "się",
        "nie",
        "tak",
        "ale",
        "jak",
        "co",
        "ten",
        "ta",
        "te",
        "za",
        "od",
        "po",
        "o",
        "a",
        "oraz",
        "lub",
        "by",
        "być",
        "aby",
        "już",
        "też",
        "tylko",
        "czy",
        "tego",
        "tej",
        "tym",
        "tę",
        "tych",
        "które",
        "który",
        "która",
        "których",
        "którzy",
        "której",
        "którą",
        "chce",
        "chciałabym",
        "chciałaby",
        "mówi",
        "prosi",
        "sugeruje",
        "uważa",
        "wskazuje",
        "użytkownik",
        "użytkowniczka",
        "najlepiej",
        "około",
        "ok",
        "ok.",
        # English
        "the",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "need",
        "dare",
        "ought",
        "used",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "then",
        "once",
        "here",
        "there",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "and",
        "but",
        "if",
        "or",
        "because",
        "until",
        "while",
        "this",
        "that",
        "these",
        "those",
        "user",
        "wants",
        "says",
        "suggests",
    }

    # Number normalization: map Polish number words to digits
    number_map = {
        "jeden": "1",
        "jedna": "1",
        "jedno": "1",
        "jednego": "1",
        "dwa": "2",
        "dwie": "2",
        "dwóch": "2",
        "dwu": "2",
        "trzy": "3",
        "trzech": "3",
        "cztery": "4",
        "czterech": "4",
        "pięć": "5",
        "pięciu": "5",
        "pieciu": "5",
        "sześć": "6",
        "sześciu": "6",
        "siedem": "7",
        "siedmiu": "7",
        "osiem": "8",
        "ośmiu": "8",
        "dziewięć": "9",
        "dziewięciu": "9",
        "dziesięć": "10",
        "dziesięciu": "10",
    }

    # Simple Polish stemming: normalize common word forms
    stem_map = {
        # lista (list)
        "listy": "lista",
        "liście": "lista",
        "liscie": "lista",
        "listę": "lista",
        "liste": "lista",
        "liści": "lista",
        # krótki/skrócić (short/shorten)
        "krótsza": "krotki",
        "krotsza": "krotki",
        "krótszy": "krotki",
        "skrócić": "krotki",
        "skrocic": "krotki",
        "skrócona": "krotki",
        # pozycja (position/item)
        "pozycji": "pozycja",
        "pozycje": "pozycja",
        "pozycją": "pozycja",
        # pacjent (patient)
        "pacjenta": "pacjent",
        "pacjentów": "pacjent",
        "pacjentow": "pacjent",
        "pacjenci": "pacjent",
        "pacjentem": "pacjent",
        # dodać (add)
        "dodaj": "dodac",
        "dodać": "dodac",
        "dodania": "dodac",
        # nagłówek (header)
        "nagłówka": "naglowek",
        "naglowka": "naglowek",
        "nagłówku": "naglowek",
        # sekcja (section)
        "sekcji": "sekcja",
        "sekcję": "sekcja",
        "sekcje": "sekcja",
        # szuflada (drawer)
        "szuflady": "szuflada",
        "szufladzie": "szuflada",
        "szufladę": "szuflada",
        # rozwin / rozwiń
        "rozwiń": "rozwin",
        "rozwin": "rozwin",
        "rozwinac": "rozwin",
        "rozwinąć": "rozwin",
        # otwórz
        "otwórz": "otworz",
        "otworzyc": "otworz",
        "otworzyć": "otworz",
        "otwiera": "otworz",
        "otwarcie": "otworz",
        # zamaz / rozmaz
        "zamazana": "zamaz",
        "zamazane": "zamaz",
        "zamazany": "zamaz",
        "zamazania": "zamaz",
        "rozmazany": "rozmaz",
        "rozmazane": "rozmaz",
        "rozmazania": "rozmaz",
        # alert
        "alerty": "alert",
        "alertów": "alert",
        "alertow": "alert",
        # dane
        "danych": "dane",
        "danymi": "dane",
        # historia
        "historii": "historia",
        "history": "historia",
        # wizyta
        "wizyty": "wizyta",
        "wizytę": "wizyta",
        "wizyt": "wizyta",
    }

    # Normalize: lowercase, remove punctuation, split
    text_lower = text.lower()
    # Remove punctuation except numbers
    text_clean = re.sub(r"[^\w\s]", " ", text_lower)
    words = text_clean.split()

    # Process words
    result = set()
    for word in words:
        # Normalize numbers first so digits survive length filter
        if word in number_map:
            result.add(number_map[word])
            continue

        # Allow key short tokens like UI/UX/AI and digits
        if len(word) <= 2 and not word.isdigit() and word not in {"ui", "ux", "ai"}:
            continue
        if word in stopwords:
            continue

        # Apply stemming
        if word in stem_map:
            result.add(stem_map[word])
        else:
            result.add(word)

    return result


def _text_similarity(text1: str, text2: str) -> float:
    """Calculate concept-based similarity between two texts."""
    words1 = _normalize_text_for_similarity(text1)
    words2 = _normalize_text_for_similarity(text2)

    if not words1 or not words2:
        return 0.0

    # Key concepts that indicate similar topics
    key_concepts = {
        "lista",
        "pozycja",
        "krotki",
        "sekcja",
        "naglowek",
        "pacjent",
        "dodac",
        "przycisk",
        "button",
        "dropdown",
        "menu",
        "modal",
        "okno",
        "formularz",
        "pole",
        "input",
        "wybor",
        "opcja",
        "szuflada",
        "rozwin",
        "otworz",
        "zamaz",
        "rozmaz",
        "alert",
        "dane",
        "historia",
        "wizyta",
        "timeline",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
    }

    # Find shared key concepts
    concepts1 = words1 & key_concepts
    concepts2 = words2 & key_concepts
    shared_concepts = concepts1 & concepts2

    # If they share 2+ key concepts, consider them similar
    if len(shared_concepts) >= 2:
        # Score based on concept overlap
        concept_score = len(shared_concepts) / max(len(concepts1), len(concepts2), 1)

        # Also factor in overall word overlap (Jaccard)
        intersection = words1 & words2
        union = words1 | words2
        jaccard = len(intersection) / len(union) if union else 0.0

        # Weighted: 60% concept match, 40% jaccard
        return 0.6 * concept_score + 0.4 * jaccard

    # Fallback to pure Jaccard for non-concept matches
    intersection = words1 & words2
    union = words1 | words2

    return len(intersection) / len(union) if union else 0.0
