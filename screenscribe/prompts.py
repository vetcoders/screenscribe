"""Internationalized prompts for LLM and Vision analysis."""

from typing import Literal

PromptLanguage = Literal["pl", "en"]


def apply_analysis_prompt_override(base_prompt: str, override: str = "") -> str:
    """Append operator instructions without breaking required output contracts."""
    cleaned = override.strip()
    if not cleaned:
        return base_prompt

    return (
        f"{base_prompt}\n\n"
        "ADDITIONAL ANALYSIS INSTRUCTIONS:\n"
        f"{cleaned}\n\n"
        "These instructions refine the analysis but do not replace the required "
        "output format, schema, or requested language. Preserve every required "
        "field and formatting rule from the original prompt."
    )


# Semantic analysis prompts
SEMANTIC_ANALYSIS_PROMPTS: dict[str, str] = {
    "pl": """Jesteś ekspertem UX/UI i programistą analizującym feedback z nagrania screencast.

Przeanalizuj poniższy fragment transkrypcji. UWAGA: Użytkownik może zgłaszać problem, ALE TAKŻE może potwierdzać że coś działa poprawnie.

Fragment:
{text}

Kontekst (otaczające wypowiedzi):
{context}

Kategoria wykryta automatycznie: {category}

WAŻNE - Przykłady interpretacji:
- "To nie działa" → is_issue: true (zgłasza problem)
- "Te białe tła nie przeszkadzają" → is_issue: false (potwierdza że OK)
- "Powinno być przeźroczyste" → is_issue: true (zgłasza problem)
- "Działa ładnie" → is_issue: false (potwierdza że OK)
- "Jest brzydkie" → is_issue: true (zgłasza problem)
- "Teraz jest ok" → is_issue: false (potwierdza że OK)

Zwróć szczególną uwagę na NEGACJE ("nie przeszkadza", "nie ma problemu", "jest ok").

Odpowiedz w formacie JSON:
{{
    "is_issue": true/false,
    "sentiment": "problem|positive|neutral",
    "severity": "critical|high|medium|low|none",
    "summary": "Krótkie podsumowanie - CO użytkownik mówi (1-2 zdania)",
    "action_items": ["Lista konkretnych zadań do wykonania (pusta jeśli is_issue=false)"],
    "affected_components": ["Lista komponentów UI/funkcji których dotyczy"],
    "suggested_fix": "Sugerowane rozwiązanie techniczne (lub 'Brak - nie jest to problem' jeśli is_issue=false)"
}}

Odpowiadaj tylko JSON, bez dodatkowego tekstu.""",
    "en": """You are a UX/UI expert and developer analyzing feedback from a screencast recording.

Analyze the following transcript fragment. NOTE: The user may be reporting a problem, BUT ALSO may be confirming that something works correctly.

Fragment:
{text}

Context (surrounding speech):
{context}

Automatically detected category: {category}

IMPORTANT - Interpretation examples:
- "This doesn't work" → is_issue: true (reports problem)
- "The white backgrounds don't bother me" → is_issue: false (confirms OK)
- "Should be transparent" → is_issue: true (reports problem)
- "Works nicely" → is_issue: false (confirms OK)
- "It's ugly" → is_issue: true (reports problem)
- "Now it's fine" → is_issue: false (confirms OK)

Pay special attention to NEGATIONS ("doesn't bother", "no problem", "is ok").

Respond in JSON format:
{{
    "is_issue": true/false,
    "sentiment": "problem|positive|neutral",
    "severity": "critical|high|medium|low|none",
    "summary": "Brief summary - WHAT the user is saying (1-2 sentences)",
    "action_items": ["List of specific tasks to complete (empty if is_issue=false)"],
    "affected_components": ["List of affected UI components/features"],
    "suggested_fix": "Suggested technical solution (or 'None - not an issue' if is_issue=false)"
}}

Respond only with JSON, no additional text.""",
}

# Executive summary prompts
EXECUTIVE_SUMMARY_PROMPTS: dict[str, str] = {
    "pl": """Jesteś product managerem przygotowującym raport z przeglądu UX.

Na podstawie poniższych znalezisk, przygotuj krótkie podsumowanie wykonawcze (executive summary) dla zespołu developerskiego.

Znaleziska:
{findings}

Napisz podsumowanie w 3-5 zdaniach, skupiając się na:
1. Najważniejszych problemach do naprawienia
2. Ogólnym stanie UX aplikacji
3. Rekomendacji priorytetów

Odpowiadaj po polsku, zwięźle i konkretnie.""",
    "en": """You are a product manager preparing a UX review report.

Based on the following findings, prepare a brief executive summary for the development team.

Findings:
{findings}

Write a summary in 3-5 sentences, focusing on:
1. Most critical issues to fix
2. Overall state of the application UX
3. Priority recommendations

Be concise and specific.""",
}


def get_semantic_analysis_prompt(language: str = "en") -> str:
    """Get semantic analysis prompt for the specified language."""
    lang = _normalize_language(language)
    return SEMANTIC_ANALYSIS_PROMPTS.get(lang, SEMANTIC_ANALYSIS_PROMPTS["en"])


def get_executive_summary_prompt(language: str = "en") -> str:
    """Get executive summary prompt for the specified language."""
    lang = _normalize_language(language)
    return EXECUTIVE_SUMMARY_PROMPTS.get(lang, EXECUTIVE_SUMMARY_PROMPTS["en"])


def _normalize_language(language: str) -> str:
    """Normalize language code to supported values."""
    lang = language.lower().strip()

    # Map common language codes to supported ones
    pl_codes = {"pl", "pl-pl", "polish", "polski"}
    en_codes = {"en", "en-us", "en-gb", "english"}

    if lang in pl_codes:
        return "pl"
    if lang in en_codes:
        return "en"

    # Default to English for unsupported languages
    return "en"


def get_supported_languages() -> list[str]:
    """Get list of supported languages."""
    return ["pl", "en"]


# Unified analysis prompts (VLM-powered: combines semantic + vision in single call)
UNIFIED_ANALYSIS_PROMPTS: dict[str, str] = {
    "pl": """Jesteś ekspertem UX/UI analizującym nagranie screencast z feedbackiem użytkownika.

Masz do dyspozycji:
1. Screenshot z aplikacji (załączony obrazek)
2. Fragment transkrypcji z tego momentu nagrania

Fragment transkrypcji:
{transcript_context}

Pełny kontekst (otaczające wypowiedzi):
{full_context}

Kategoria wykryta automatycznie: {category}
{keywords_hint}
WAŻNE - określ czy użytkownik zgłasza PROBLEM czy POTWIERDZA że coś jest OK:
- "To nie działa" → is_issue: true
- "Nie przeszkadza mi to" → is_issue: false
- "Powinno być inaczej" → is_issue: true
- "Teraz jest ok" → is_issue: false
- "Jest brzydkie" → is_issue: true
- "Działa ładnie" → is_issue: false
- "Nie działa, bo nie mam danych w bazie" → is_issue: false, severity: "none" (user wyjaśnia że to nie bug, tylko brak danych testowych)

KLUCZOWE: Summary musi bazować TYLKO na transkrypcji użytkownika. Screenshot służy tylko do kontekstu wizualnego - NIE dodawaj szczegółów ze screenshota do summary!

Przeanalizuj screenshot i transkrypcję, ale summary pisz TYLKO na podstawie tego co użytkownik powiedział:
{{
    "is_issue": true/false,
    "sentiment": "problem|positive|neutral",
    "severity": "critical|high|medium|low|none",
    "summary": "TYLKO co użytkownik mówi w transkrypcji (1-2 zdania, BEZ dodawania szczegółów widocznych tylko na screenshocie)",
    "action_items": ["Konkretne zadania do wykonania (puste jeśli is_issue=false)"],
    "affected_components": ["Komponenty UI których dotyczy"],
    "suggested_fix": "Sugerowane rozwiązanie techniczne",
    "ui_elements": ["Widoczne elementy UI na screenshocie"],
    "issues_detected": ["Problemy wizualne/UX widoczne na screenshocie"],
    "accessibility_notes": ["Uwagi o dostępności"],
    "design_feedback": "Ocena designu i sugestie (1-2 zdania)",
    "technical_observations": "Obserwacje techniczne - błędy, artefakty, problemy z layoutem"
}}

Wszystkie wartości tekstowe w JSON (summary, action_items, suggested_fix, design_feedback, technical_observations itd.) pisz PO POLSKU.
Odpowiadaj tylko JSON, bez dodatkowego tekstu.""",
    "en": """You are a UX/UI expert analyzing a screencast recording with user feedback.

You have access to:
1. A screenshot from the application (attached image)
2. A transcript fragment from this moment in the recording

Transcript fragment:
{transcript_context}

Full context (surrounding speech):
{full_context}

Automatically detected category: {category}
{keywords_hint}
IMPORTANT - determine if the user is reporting a PROBLEM or CONFIRMING something is OK:
- "This doesn't work" → is_issue: true
- "This doesn't bother me" → is_issue: false
- "Should be different" → is_issue: true
- "Now it's fine" → is_issue: false
- "It's ugly" → is_issue: true
- "Works nicely" → is_issue: false
- "Doesn't work because I have no data in database" → is_issue: false, severity: "none" (user explains it's not a bug, just missing test data)

KEY: Summary must be based ONLY on user's transcript. Screenshot is only for visual context - DO NOT add screenshot details to summary!

Analyze screenshot and transcript, but write summary ONLY based on what user said:
{{
    "is_issue": true/false,
    "sentiment": "problem|positive|neutral",
    "severity": "critical|high|medium|low|none",
    "summary": "ONLY what the user says in the transcript (1-2 sentences, WITHOUT adding details visible only in screenshot)",
    "action_items": ["Specific tasks to complete (empty if is_issue=false)"],
    "affected_components": ["UI components this relates to"],
    "suggested_fix": "Suggested technical solution",
    "ui_elements": ["Visible UI elements in the screenshot"],
    "issues_detected": ["Visual/UX issues visible in the screenshot"],
    "accessibility_notes": ["Accessibility observations"],
    "design_feedback": "Design assessment and suggestions (1-2 sentences)",
    "technical_observations": "Technical observations - errors, artifacts, layout issues"
}}

Write all text values in the JSON (summary, action_items, suggested_fix, design_feedback, technical_observations, etc.) in English.
Respond only with JSON, no additional text.""",
}

# Unified analysis prompt for text-only fallback (when screenshot extraction fails)
UNIFIED_ANALYSIS_TEXT_ONLY_PROMPTS: dict[str, str] = {
    "pl": """Jesteś ekspertem UX/UI analizującym feedback z nagrania screencast.

Fragment transkrypcji:
{transcript_context}

Pełny kontekst (otaczające wypowiedzi):
{full_context}

Kategoria wykryta automatycznie: {category}
{keywords_hint}
UWAGA: Screenshot nie jest dostępny - analizuj tylko na podstawie transkrypcji.

WAŻNE - określ czy użytkownik zgłasza PROBLEM czy POTWIERDZA że coś jest OK:
- "To nie działa" → is_issue: true
- "Nie przeszkadza mi to" → is_issue: false

Odpowiedz JSON:
{{
    "is_issue": true/false,
    "sentiment": "problem|positive|neutral",
    "severity": "critical|high|medium|low|none",
    "summary": "Co użytkownik mówi (1-2 zdania)",
    "action_items": ["Konkretne zadania (puste jeśli is_issue=false)"],
    "affected_components": ["Komponenty UI których dotyczy (na podstawie transkrypcji)"],
    "suggested_fix": "Sugerowane rozwiązanie",
    "ui_elements": [],
    "issues_detected": [],
    "accessibility_notes": [],
    "design_feedback": "Brak - screenshot niedostępny",
    "technical_observations": "Brak - screenshot niedostępny"
}}

Wszystkie wartości tekstowe w JSON (summary, action_items, suggested_fix itd.) pisz PO POLSKU.
Odpowiadaj tylko JSON.""",
    "en": """You are a UX/UI expert analyzing feedback from a screencast recording.

Transcript fragment:
{transcript_context}

Full context (surrounding speech):
{full_context}

Automatically detected category: {category}
{keywords_hint}
NOTE: Screenshot is not available - analyze based on transcript only.

IMPORTANT - determine if the user is reporting a PROBLEM or CONFIRMING something is OK:
- "This doesn't work" → is_issue: true
- "This doesn't bother me" → is_issue: false

Respond with JSON:
{{
    "is_issue": true/false,
    "sentiment": "problem|positive|neutral",
    "severity": "critical|high|medium|low|none",
    "summary": "What the user says (1-2 sentences)",
    "action_items": ["Specific tasks (empty if is_issue=false)"],
    "affected_components": ["UI components (based on transcript)"],
    "suggested_fix": "Suggested solution",
    "ui_elements": [],
    "issues_detected": [],
    "accessibility_notes": [],
    "design_feedback": "N/A - screenshot unavailable",
    "technical_observations": "N/A - screenshot unavailable"
}}

Write all text values in the JSON (summary, action_items, suggested_fix, etc.) in English.
Respond only with JSON.""",
}


def get_unified_analysis_prompt(language: str = "en", text_only: bool = False) -> str:
    """Get unified analysis prompt for the specified language.

    Args:
        language: Language code (pl or en)
        text_only: If True, return text-only fallback prompt (no screenshot)

    Returns:
        Prompt template string
    """
    lang = _normalize_language(language)
    if text_only:
        return UNIFIED_ANALYSIS_TEXT_ONLY_PROMPTS.get(
            lang, UNIFIED_ANALYSIS_TEXT_ONLY_PROMPTS["en"]
        )
    return UNIFIED_ANALYSIS_PROMPTS.get(lang, UNIFIED_ANALYSIS_PROMPTS["en"])
