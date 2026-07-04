"""Regression guard for the shared HTML i18n runtime."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from screenscribe.html_pro.renderer import render_html_report_pro
from screenscribe.shell import ANALYZE_SURFACE, render_surface

ASSETS = Path("screenscribe/html_pro_assets")
SCRIPTS = ASSETS / "scripts"
RUNTIME = SCRIPTS / "i18n.js"
SURFACE_JS = (
    SCRIPTS / "review_app.js",
    SCRIPTS / "analyze_dashboard.js",
    SCRIPTS / "video_player.js",
)
ANCHOR_SOURCES = (
    (ASSETS / "templates/shell.html", "review"),
    (ASSETS / "templates/partials/tabbar.html", "review"),
    (ASSETS / "templates/partials/header_right.html", "review"),
    (ASSETS / "templates/partials/video_panel.html", "review"),
    (ASSETS / "templates/partials/transcript_panel.html", "review"),
    (ASSETS / "templates/partials/capture_panel.html", "analyze"),
    (ASSETS / "templates/partials/voice_notes_panel.html", "analyze"),
    (ASSETS / "templates/partials/export_panel.html", "analyze"),
    (ASSETS / "templates/partials/frame_modal.html", "analyze"),
)

_BUNDLE = re.compile(
    r"window\.I18N_BUNDLE\s*=\s*(?P<json>\{.*?\});",
    re.DOTALL,
)
_T_CALL = re.compile(r"\bt\(\s*['\"](?P<key>[a-z]+(?:\.[A-Za-z0-9_]+)+)['\"]")
_ANCHOR = re.compile(r"""data-i18n(?:-(?:attr|title|alt|tpl))?=["'](?P<value>[^"']+)["']""")
_USER_FACING_LITERAL = re.compile(r"['\"]([^'\"]*[A-Za-z][^'\"]*)['\"]")
_DOM_WRITE = re.compile(
    r"(?:\.textContent\s*=|\.innerHTML\s*=|insertAdjacentHTML\s*\(|showNotification\s*\(|alert\s*\()"
)

_ALLOW_LITERAL_SUBSTRINGS = (
    "t(",
    "escapeHtml(t(",
    "JSON.stringify",
)
_DEAD_CHROME_DUPLICATES = (
    ASSETS / "templates/report.html",
    ASSETS / "templates/partials/analyze_tabbar.html",
    ASSETS / "templates/partials/analyze_header_right.html",
    ASSETS / "templates/partials/analyze_video_panel.html",
    ASSETS / "templates/partials/analyze_transcript_panel.html",
)
_PL_CHROME_STRINGS = (
    "Podsumowanie",
    "Momenty",
    "Statystyki",
    "Transkrypcja",
    "Szukaj w transkrypcji",
    "Recenzja",
    "Potwierdzone",
    "Fałszywy alarm",
    "Zmień priorytet",
    "Zmien priorytet",
    "Bez zmian",
    "Krytyczny",
    "Wysoki",
    "Średnie",
    "Sredni",
    "Niski",
    "Notatki / Akcje",
    "Sugestie AI",
    "Notatka głosowa",
    "Twoje uwagi",
    "Podsumowanie AI niedostępne",
    "Oznaczanie",
    "Eksport",
    "Analiza ręczna",
    "Mowa:",
    "Interfejs:",
)
_EN_CHROME_STRINGS = (
    "Summary",
    "Moments",
    "Statistics",
    "Transcript",
    "Search transcript",
    "Review",
    "Confirmed?",
    "No / False alarm",
    "Change priority",
    "-- No change --",
    "Critical",
    "High",
    "Medium",
    "Low",
    "Notes / Actions",
    "AI Suggestions",
    "Voice note",
    "Your notes",
    "No AI summary available",
    "Mark",
    "Manual analysis",
    "Speech:",
    "UI:",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_runtime_payloads(source: str) -> str:
    source = re.sub(r"<script\b.*?</script>", "", source, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<style\b.*?</style>", "", source, flags=re.IGNORECASE | re.DOTALL)


class _ChromeTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        for name, value in attrs:
            if name in {"placeholder", "title", "aria-label", "alt"} and value:
                self.parts.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _visible_chrome_text(source: str) -> str:
    parser = _ChromeTextParser()
    parser.feed(source)
    return "\n".join(part.strip() for part in parser.parts if part.strip())


def _review_html(language: str) -> str:
    return render_html_report_pro(
        video_name="i18n.mov",
        video_path=None,
        generated_at="2026-06-13T00:00:00",
        executive_summary="",
        findings=[
            {
                "id": 1,
                "category": "bug",
                "timestamp": 1.0,
                "timestamp_formatted": "00:01",
                "text": "neutral finding",
                "unified_analysis": {
                    "is_issue": True,
                    "severity": "high",
                    "summary": "neutral summary",
                    "suggested_fix": "neutral fix",
                    "affected_components": ["panel"],
                    "issues_detected": ["neutral issue"],
                    "action_items": ["neutral action"],
                },
            }
        ],
        segments=[],
        errors=[],
        language=language,
    )


def _analyze_html(language: str) -> str:
    return render_surface(
        ANALYZE_SURFACE,
        {
            "document_language": language,
            "ui_language": language,
            "video_name": "i18n.mp4",
            "video_name_escaped": "i18n.mp4",
            "speech_lang_label": language.upper(),
            "body_mode": "analyze",
            "body_default_lang": language,
            "body_speech_lang": language,
            "body_has_markers": "false",
            "findings_json": "[]",
            "segments_json": "[]",
        },
    )


def _bundle() -> dict[str, dict[str, dict[str, str]]]:
    match = _BUNDLE.search(_read(RUNTIME))
    assert match, "scripts/i18n.js must assign window.I18N_BUNDLE = {...};"
    return json.loads(match.group("json"))


def _resolve(
    bundle: dict[str, dict[str, dict[str, str]]], lang: str, key: str, surface: str
) -> bool:
    if "." in key:
        namespace, item = key.split(".", 1)
        return item in bundle[lang].get(namespace, {})
    return any(key in bundle[lang].get(namespace, {}) for namespace in (surface, "media", "shell"))


def _anchor_keys(source: str) -> set[str]:
    keys: set[str] = set()
    for match in _ANCHOR.finditer(source):
        value = match.group("value")
        if "{" in value:
            continue
        if ":" in value:
            for pair in value.split(","):
                _attr, key = pair.split(":", 1)
                keys.add(key.strip())
        else:
            keys.add(value.strip())
    return keys


def test_single_i18n_runtime_replaces_dual_globals() -> None:
    assert RUNTIME.exists(), "single i18n runtime asset is missing"
    assert "const i18n =" not in _read(SCRIPTS / "review_app.js")
    assert "resolveI18nDict" not in _read(SCRIPTS / "video_player.js")

    bundle = _bundle()
    assert set(bundle) == {"en", "pl"}
    for lang in ("en", "pl"):
        assert set(bundle[lang]) == {"shell", "media", "review", "analyze"}


def test_i18n_keys_have_language_parity() -> None:
    bundle = _bundle()
    offenders: list[str] = []

    for js_path in SURFACE_JS:
        for match in _T_CALL.finditer(_read(js_path)):
            key = match.group("key")
            for lang in ("en", "pl"):
                if not _resolve(bundle, lang, key, "review"):
                    offenders.append(f"{js_path.name}: t('{key}') missing in {lang}")

    for source_path, surface in ANCHOR_SOURCES:
        for key in _anchor_keys(_read(source_path)):
            for lang in ("en", "pl"):
                if not _resolve(bundle, lang, key, surface):
                    offenders.append(f"{source_path}: data-i18n '{key}' missing in {lang}")

    assert not offenders, "unresolved i18n keys:\n" + "\n".join(offenders)


def test_rendered_chrome_is_bidirectionally_localized() -> None:
    offenders: list[str] = []

    for surface, html in {
        "review/en": _review_html("en"),
        "analyze/en": _analyze_html("en"),
    }.items():
        visible_html = _visible_chrome_text(_strip_runtime_payloads(html))
        for text in _PL_CHROME_STRINGS:
            if text in visible_html:
                offenders.append(f"{surface}: Polish chrome leaked: {text}")

    for surface, html in {
        "review/pl": _review_html("pl"),
        "analyze/pl": _analyze_html("pl"),
    }.items():
        visible_html = _visible_chrome_text(_strip_runtime_payloads(html))
        for text in _EN_CHROME_STRINGS:
            if text in visible_html:
                offenders.append(f"{surface}: English chrome leaked: {text}")

    assert not offenders, "hardcoded chrome language leaks:\n" + "\n".join(offenders)


def test_dead_shell_duplicates_are_deleted() -> None:
    offenders = [str(path) for path in _DEAD_CHROME_DUPLICATES if path.exists()]
    assert not offenders, "dead per-surface shell duplicates still exist:\n" + "\n".join(offenders)


def test_surface_js_does_not_write_hardcoded_user_facing_literals() -> None:
    offenders: list[str] = []

    for js_path in SURFACE_JS:
        for line_number, line in enumerate(_read(js_path).splitlines(), start=1):
            if not _DOM_WRITE.search(line):
                continue
            if any(marker in line for marker in _ALLOW_LITERAL_SUBSTRINGS):
                continue
            literals = [
                literal
                for literal in _USER_FACING_LITERAL.findall(line)
                if len(literal.strip()) > 1 and not literal.strip().startswith(("#", ".", "/"))
            ]
            if literals:
                offenders.append(f"{js_path.name}:{line_number}: {', '.join(literals)}")

    assert not offenders, "hardcoded user-facing JS DOM literals:\n" + "\n".join(offenders)
