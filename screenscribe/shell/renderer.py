"""Slot-based HTML surface renderer.

The shell owns the invariant frame and asset/script ordering. Surface-specific
renderers supply already escaped slot data and JSON islands.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from typing import Any

from ..html_pro.assets import (
    load_asset,
    load_css,
    load_css_analyze_dashboard,
    load_css_screenscribe_theme,
    load_css_screenscribe_theme_polish,
    load_favicon_data_uri,
    load_js_analyze_dashboard,
    load_js_i18n_runtime,
    load_js_jszip,
    load_js_lib_language_control,
    load_js_lib_stt_transport,
    load_js_lib_tab_keyboard,
    load_js_review_app,
    load_js_video_player,
)
from .surface import HeaderCellConfig, SurfaceConfig, TabConfig

_PARTIAL_PREFIX = "templates/partials/"
_SERVER_I18N: dict[str, dict[str, dict[str, str]]] = {
    "en": {
        "shell": {
            "language_toggle_aria": "Language",
            "resize_findings_panel": "Resize findings panel",
        },
        "media": {
            "noHtml5Video": "Your browser does not support HTML5 video.",
            "staticDemoNoVideo": "Sample report — the source recording is not included.",
        },
        "review": {
            "tabs_aria": "Report sections",
            "summary": "Summary",
            "findings": "Moments",
            "export": "Export",
            "transcript": "Transcript",
            "searchTranscript": "Search transcript...",
            "reviewer": "Reviewer:",
            "reviewerPlaceholder": "Your name",
            "saveToDisk": "Save review",
            "exportTodo": "Export TODO",
            "exportJson": "Export JSON",
            "exportZip": "Export ZIP",
            "exportZipTitle": "ZIP with annotated screenshots",
        },
        "analyze": {
            "tabs_aria": "Workflow sections",
            "tab_capture": "Mark",
            "tab_findings": "Moments",
            "tab_export": "Export",
            "meta_mode": "Manual analysis",
            "speech_language_label": "Speech:",
            "speech_language_title": "Speech transcription follows CLI --lang, not the UI/VLM toggle.",
            "ui_language_label": "UI:",
            "panel_heading": "Mark the moment",
            "ux_hint": "Pause the video and mark a moment. Add a voice or text note now or later — notes are optional.",
            "howto_heading": "How it works",
            "howto_step_1": "Pause the video",
            "howto_step_2": "Mark a moment",
            "howto_step_3": "Add or edit a note anytime (optional)",
            "howto_step_4": "Review and export",
            "mic_title": "Record a voice note",
            "mic_label": "Record a voice note",
            "recording": "Recording...",
            "notes_placeholder": "Text note...",
            "mark_frame": "Add moment",
            "marker_timeline_aria": "Marked moments on video timeline",
            "video_status_idle": "Pause the video to mark a moment",
            "transcript_heading": "Voice notes",
            "transcript_search": "Search transcript...",
            "transcript_empty_1": "Record a voice note while marking a moment.",
            "transcript_empty_2": "The transcript will appear here.",
            "findings_empty_1": "No moments marked yet.",
            "findings_empty_2": "Watch the video and mark important moments.",
            "markers_list_aria": "Marked moments",
            "status_ready": "Ready",
            "errors_count": "0 errors",
            "export_json": "Download JSON",
            "report_md": "Report MD",
            "export_gate_hint": "Export is available after you add your first moment.",
            # Per-marker priority control (A7b). Rendered client-side by
            # analyze_dashboard.js; mirrored here so the analyze-surface i18n
            # sources stay value-aligned (single-source guard).
            "action_change_priority": "Change priority",
            "severity_no_change": "-- No priority --",
            "severity_critical": "Critical",
            "severity_high": "High",
            "severity_medium": "Medium",
            "severity_low": "Low",
        },
    },
    "pl": {
        "shell": {
            "language_toggle_aria": "Język",
            "resize_findings_panel": "Zmień rozmiar panelu znalezisk",
        },
        "media": {
            "noHtml5Video": "Twoja przeglądarka nie obsługuje wideo HTML5.",
            "staticDemoNoVideo": "Raport przykładowy — nagranie źródłowe nie jest dołączone.",
        },
        "review": {
            "tabs_aria": "Sekcje raportu",
            "summary": "Podsumowanie",
            "findings": "Momenty",
            "export": "Eksport",
            "transcript": "Transkrypcja",
            "searchTranscript": "Szukaj w transkrypcji...",
            "reviewer": "Recenzent:",
            "reviewerPlaceholder": "Twoje imię i nazwisko",
            "saveToDisk": "Zapisz recenzję",
            "exportTodo": "Eksportuj TODO",
            "exportJson": "Eksportuj JSON",
            "exportZip": "Eksportuj ZIP",
            "exportZipTitle": "ZIP z adnotowanymi screenshotami",
        },
        "analyze": {
            "tabs_aria": "Sekcje pracy",
            "tab_capture": "Oznaczanie",
            "tab_findings": "Momenty",
            "tab_export": "Eksport",
            "meta_mode": "Analiza ręczna",
            "speech_language_label": "Mowa:",
            "speech_language_title": "Transkrypcja mowy używa CLI --lang, nie przełącznika UI/VLM.",
            "ui_language_label": "Interfejs:",
            "panel_heading": "Oznacz ważny moment",
            "ux_hint": "Zatrzymaj film i oznacz moment. Notatkę głosową lub tekstową dodasz teraz lub później — jest opcjonalna.",
            "howto_heading": "Jak to działa",
            "howto_step_1": "Zatrzymaj film",
            "howto_step_2": "Oznacz moment",
            "howto_step_3": "Dodaj lub edytuj notatkę kiedykolwiek (opcjonalnie)",
            "howto_step_4": "Przejrzyj i wyeksportuj raport",
            "mic_title": "Nagraj notatkę głosową",
            "mic_label": "Nagraj notatkę głosową",
            "recording": "Nagrywanie...",
            "notes_placeholder": "Notatka tekstowa...",
            "mark_frame": "Dodaj moment",
            "marker_timeline_aria": "Oznaczone momenty na osi czasu wideo",
            "video_status_idle": "Zatrzymaj film, żeby oznaczyć moment",
            "transcript_heading": "Notatki głosowe",
            "transcript_search": "Szukaj w transkrypcji...",
            "transcript_empty_1": "Nagraj notatkę głosową przy oznaczaniu momentu.",
            "transcript_empty_2": "Transkrypcja pojawi się tutaj.",
            "findings_empty_1": "Brak oznaczonych momentów.",
            "findings_empty_2": "Oglądaj film i oznaczaj ważne momenty.",
            "markers_list_aria": "Oznaczone momenty",
            "status_ready": "Gotowe",
            "errors_count": "0 błędów",
            "export_json": "Pobierz JSON",
            "report_md": "Pobierz raport",
            "export_gate_hint": "Eksport będzie dostępny po dodaniu pierwszego momentu.",
            # Per-marker priority control (A7b) — see the EN block above.
            "action_change_priority": "Zmień priorytet",
            "severity_no_change": "-- Bez priorytetu --",
            "severity_critical": "Krytyczne",
            "severity_high": "Wysokie",
            "severity_medium": "Średnie",
            "severity_low": "Niskie",
        },
    },
}
_STYLE_LOADERS: Mapping[str, Callable[[], str]] = {
    "analyze_dashboard": load_css_analyze_dashboard,
}
_SCRIPT_LOADERS: Mapping[str, Callable[[], str]] = {
    "i18n": load_js_i18n_runtime,
    "lib/language-control": load_js_lib_language_control,
    "lib/stt-transport": load_js_lib_stt_transport,
    "lib/tab-keyboard": load_js_lib_tab_keyboard,
    "analyze_dashboard": load_js_analyze_dashboard,
    "video_player": load_js_video_player,
    "review_app": load_js_review_app,
}


def _format_template(template: str, context: Mapping[str, Any]) -> str:
    return template.format_map(dict(context))


def _load_partial(name: str) -> str:
    return load_asset(f"{_PARTIAL_PREFIX}{name}.html")


def _render_partials(names: list[str], context: Mapping[str, Any]) -> str:
    return "\n".join(_format_template(_load_partial(name), context) for name in names)


def _render_optional_partial(name: str | None, context: Mapping[str, Any]) -> str:
    if not name:
        return ""
    return _format_template(_load_partial(name), context)


def _language(context: Mapping[str, Any]) -> str:
    language = str(context.get("ui_language", context.get("document_language", "en")))
    language = language.split("-", 1)[0].lower()
    return language if language in _SERVER_I18N else "en"


def _t(
    config: SurfaceConfig, context: Mapping[str, Any], key: str, namespace: str | None = None
) -> str:
    language = _language(context)
    namespaces = (
        [namespace] if namespace else [config.i18n_namespace, "review", "analyze", "media", "shell"]
    )
    for item_namespace in namespaces:
        value = _SERVER_I18N[language].get(item_namespace, {}).get(key)
        if value is not None:
            return value
    for item_namespace in namespaces:
        value = _SERVER_I18N["en"].get(item_namespace, {}).get(key)
        if value is not None:
            return value
    return key


def _render_tabs(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    buttons = [
        _render_tab(tab, index == 0, config, context) for index, tab in enumerate(config.tabs)
    ]
    return _format_template(
        _load_partial("tabbar"),
        {
            **context,
            "tabs_aria_label": html.escape(_t(config, context, config.tabs_aria_key)),
            "tab_buttons": "\n    ".join(buttons),
        },
    )


def _render_tab(
    tab: TabConfig,
    active: bool,
    config: SurfaceConfig,
    context: Mapping[str, Any],
) -> str:
    attrs = [
        'class="tab-btn' + (" active" if active else "") + '"',
        f'data-tab="{html.escape(tab.id, quote=True)}"',
        'role="tab"',
        f'aria-selected="{str(active).lower()}"',
        f'aria-controls="tab-{html.escape(tab.id, quote=True)}"',
    ]
    label = (
        f'<span data-i18n="{html.escape(tab.label_key, quote=True)}">'
        f"{html.escape(_t(config, context, tab.label_key))}</span>"
    )
    if tab.count_id:
        count = str(context.get(tab.count_value_key or "", 0))
        label += (
            f' (<span id="{html.escape(tab.count_id, quote=True)}">{html.escape(count)}</span>)'
        )
    elif tab.count_value_key:
        label += f" ({html.escape(str(context.get(tab.count_value_key, 0)))})"
    return "<button " + " ".join(attrs) + ">" + label + "</button>"


def _render_header_cells(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    cells = [_render_header_cell(cell, config, context) for cell in config.header_right]
    return _format_template(
        _load_partial("header_right"),
        {
            **context,
            "header_right_cells": "\n    ".join(cell for cell in cells if cell),
        },
    )


def _render_header_cell(
    cell: HeaderCellConfig,
    config: SurfaceConfig,
    context: Mapping[str, Any],
) -> str:
    if cell.kind == "meta":
        return f'<div class="meta">{html.escape(str(context.get(cell.value_key or "", "")))}</div>'
    if cell.kind == "timestamp":
        return f'<div class="meta">{html.escape(str(context.get(cell.value_key or "", "")))}</div>'
    if cell.kind == "mode":
        label_key = cell.label_key or "meta_mode"
        return (
            f'<div class="meta" data-i18n="{html.escape(label_key, quote=True)}">'
            f"{html.escape(_t(config, context, label_key))}</div>"
        )
    if cell.kind == "speech_lang":
        label_key = cell.label_key or "speech_language_label"
        title = html.escape(_t(config, context, "speech_language_title"), quote=True)
        return (
            '<div class="speech-language" '
            f'title="{title}" data-i18n-title="speech_language_title">'
            f'<span data-i18n="{html.escape(label_key, quote=True)}">'
            f"{html.escape(_t(config, context, label_key))}</span>"
            f'<strong id="speechLanguageValue">{html.escape(str(context.get("speech_lang_label", "")))}</strong>'
            "</div>"
        )
    if cell.kind == "lang_toggle":
        label_key = cell.label_key or "ui_language_label"
        label = ""
        if cell.show_label:
            label = (
                f'<span class="ui-language-label" data-i18n="{html.escape(label_key, quote=True)}">'
                f"{html.escape(_t(config, context, label_key))}</span>"
            )
        return f'<div class="ui-language">{label}{_render_lang_toggle(config, context)}</div>'
    raise ValueError(f"Unknown header cell kind: {cell.kind}")


def _render_lang_toggle(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    language = _language(context)
    label = html.escape(_t(config, context, "language_toggle_aria", namespace="shell"), quote=True)
    buttons = []
    for lang in ("en", "pl"):
        active = lang == language
        attrs = [
            'type="button"',
            f'data-lang="{lang}"',
            f'aria-pressed="{str(active).lower()}"',
        ]
        if active:
            attrs.append('class="active"')
        buttons.append("<button " + " ".join(attrs) + f">{lang.upper()}</button>")
    return (
        '<div class="lang-toggle" id="langToggle" role="group" '
        f'aria-label="{label}" data-i18n-attr="aria-label:language_toggle_aria">'
        + "".join(buttons)
        + "</div>"
    )


def _video_panel_after_video(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    if context.get("static_demo"):
        # Static-demo sample: no source recording ships with the report, so the
        # player controls are dropped entirely and an honest muted empty state
        # takes the (CSS-hidden) video's place. data-i18n keeps it re-translatable
        # on the client language toggle (media namespace).
        message = html.escape(_t(config, context, "staticDemoNoVideo", namespace="media"))
        return f'<p class="video-empty-state" data-i18n="staticDemoNoVideo">{message}</p>'
    if config.features.get("native_video"):
        timeline_label = html.escape(_t(config, context, "marker_timeline_aria"), quote=True)
        status = html.escape(_t(config, context, "video_status_idle"))
        return "\n".join(
            [
                '<div id="markerTimeline" class="marker-timeline"',
                '     data-i18n-attr="aria-label:marker_timeline_aria"',
                f'     aria-label="{timeline_label}">',
                '    <div id="markerTimelineTrack" class="marker-timeline-track"></div>',
                "</div>",
                '<p id="videoStatusLine" class="video-status-line"',
                f'   data-i18n="video_status_idle">{status}</p>',
            ]
        )
    return """
        <div class="video-controls-pro" id="videoControls">
            <div class="video-controls-buttons">
                <button type="button" id="secondBackBtn" class="player-btn">-1s</button>
                <button type="button" id="stepBackBtn" class="player-btn">-1f</button>
                <button type="button" id="playPauseBtn" class="player-btn" data-i18n="playLabel">{t_play}</button>
                <button type="button" id="stepForwardBtn" class="player-btn">+1f</button>
                <button type="button" id="secondForwardBtn" class="player-btn">+1s</button>
                <button type="button" id="jumpBackBtn" class="player-btn">-5s</button>
                <button type="button" id="jumpForwardBtn" class="player-btn">+5s</button>
                <button type="button" id="captureFrameBtn" class="player-btn capture-btn" data-i18n="captureFrame">{t_capture_frame}</button>
            </div>
            <div id="currentTimeLabel" class="current-time-label">00:00.000 / 00:00.000</div>
        </div>
    """.format_map(dict(context))


def _transcript_empty_state(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    if not config.transcript_empty_state:
        return ""
    return "\n".join(
        [
            '<div class="empty-state">',
            f'    <span data-i18n="transcript_empty_1">{html.escape(_t(config, context, "transcript_empty_1"))}</span><br>',
            f'    <span data-i18n="transcript_empty_2">{html.escape(_t(config, context, "transcript_empty_2"))}</span>',
            "</div>",
        ]
    )


def _surface_context(config: SurfaceConfig, context: Mapping[str, Any]) -> dict[str, Any]:
    surface_context = dict(context)
    language = _language(surface_context)
    for namespace in ("shell", "media", "review", "analyze"):
        for key, value in _SERVER_I18N[language].get(namespace, {}).items():
            surface_context[f"t_{key}"] = html.escape(value, quote=True)
    if config.features.get("native_video"):
        surface_context["video_src_attr"] = 'src="/video"'
        surface_context["vtt_track"] = ""
        surface_context["no_html5_video_text"] = html.escape(
            _t(config, context, "noHtml5Video", namespace="media")
        )
    surface_context["video_panel_after_video"] = _video_panel_after_video(config, surface_context)
    surface_context["transcript_heading_key"] = config.transcript_heading_key
    surface_context["transcript_heading_text"] = html.escape(
        _t(config, surface_context, config.transcript_heading_key)
    )
    surface_context["transcript_search_key"] = config.transcript_search_key
    surface_context["transcript_search_text"] = html.escape(
        _t(config, surface_context, config.transcript_search_key),
        quote=True,
    )
    surface_context["transcript_empty_state"] = _transcript_empty_state(config, surface_context)
    surface_context["t_resize_findings_panel"] = html.escape(
        _t(config, surface_context, "resize_findings_panel", namespace="shell"),
        quote=True,
    )
    return surface_context


def _body_attrs(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    attrs = {
        "data-surface-id": config.id,
        "data-i18n-namespace": config.i18n_namespace,
        "data-lang-persist-mode": config.lang_persist_mode,
        "data-report-id": str(context.get("report_id", "")),
        "data-video-name": str(context.get("video_name", context.get("video_name_escaped", ""))),
        "data-report-language": str(context.get("ui_language", "")),
        "data-window-mode": "workspace",
    }
    for attr_name in ("mode", "default_lang", "speech_lang", "has_markers"):
        context_key = f"body_{attr_name}"
        if context_key in context:
            attrs[f"data-{attr_name.replace('_', '-')}"] = str(context[context_key])
    # Static-demo flag (opt-in, example generator only): the client reads this to
    # skip server hydration and render the video empty state (self-contained sample).
    if context.get("static_demo"):
        attrs["data-static-demo"] = "true"
    for feature, enabled in config.features.items():
        attrs[f"data-feature-{feature.replace('_', '-')}"] = "true" if enabled else "false"
    return " ".join(
        f'{html.escape(key)}="{html.escape(value, quote=True)}"' for key, value in attrs.items()
    )


def _head(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    css_blocks = [load_css(), load_css_screenscribe_theme(), load_css_screenscribe_theme_polish()]
    for style in config.extra_styles:
        try:
            css_blocks.append(_STYLE_LOADERS[style]())
        except KeyError as exc:
            raise ValueError(f"Unknown shell style asset: {style}") from exc
    css_content = "\n".join(css_blocks)
    return "\n".join(
        [
            '<meta charset="UTF-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
            f'<link rel="icon" type="image/svg+xml" href="{load_favicon_data_uri()}">',
            f"<title>{config.title_prefix} - {context['video_name_escaped']}</title>",
            "<style>",
            css_content,
            "</style>",
            "<!-- JSZip vendored + inlined (offline, no CDN) - see html_pro_assets/vendor/ -->",
            f"<script>{load_js_jszip()}</script>",
        ]
    )


def _json_islands(context: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            '<script id="original-findings" type="application/json">',
            str(context.get("findings_json", "[]")),
            "</script>",
            "<script>",
            f"    window.TRANSCRIPT_SEGMENTS = {context.get('segments_json', '[]')};",
            "</script>",
        ]
    )


def _script_blocks(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    blocks = [_json_islands(context)]
    for script in config.scripts:
        try:
            js = _SCRIPT_LOADERS[script]()
        except KeyError as exc:
            raise ValueError(f"Unknown shell script asset: {script}") from exc
        blocks.append("<script>\n" + js + "\n</script>")
    return "\n".join(blocks)


def _document_footer(config: SurfaceConfig) -> str:
    if not config.document_footer:
        return ""
    return "<footer>\n        Generated by screenscribe\n    </footer>"


_SLOT_MARKER_RE = re.compile(r"\{slot:([a-zA-Z0-9_]+)\}")


def render_surface(config: SurfaceConfig, context: Mapping[str, Any]) -> str:
    """Render a complete HTML document by composing shell slots."""

    context = _surface_context(config, context)
    shell = load_asset("templates/shell.html")
    plain_replacements = {
        "{document_language}": str(context["document_language"]),
        "{body_attrs}": _body_attrs(config, context),
        "{wordmark}": html.escape(config.wordmark),
        "{t_detach_review}": str(context.get("t_detach_review", "")),
        "{t_attach_workspace}": str(context.get("t_attach_workspace", "")),
    }
    slot_values = {
        "head": _head(config, context),
        "window_actions": _render_optional_partial(config.window_actions, context),
        "tabs": _render_tabs(config, context),
        "header_right": _render_header_cells(config, context),
        "main_panels": _render_partials(config.main_panels, context),
        "sidebar_panels": _render_partials(config.sidebar_panels, context),
        "sidebar_footer": _render_optional_partial(config.footer, context),
        "modals": _render_partials(config.modals, context),
        "scripts": _script_blocks(config, context),
        "document_footer": _document_footer(config),
    }
    rendered = shell
    for marker, value in plain_replacements.items():
        rendered = rendered.replace(marker, value)

    def _fill_slot(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return slot_values[name]
        except KeyError:
            # A {slot:NAME} the shell template declares but we did not fill is a
            # real template/composition bug — fail loud. Validated against the
            # TEMPLATE (via re.sub over the shell), not the final output, so a
            # literal "{slot:...}" inside transcript/summary/error text never
            # trips it.
            raise ValueError(f"Unresolved shell slot: {{slot:{name}}}") from None

    # Single pass over the shell template. Slot content is emitted verbatim and
    # NOT re-scanned, so a literal "{slot:head}" arriving in externally-driven
    # text (STT transcript / LLM summary / error message) is preserved as text
    # instead of exploding the render or being clobbered by a later slot.
    return _SLOT_MARKER_RE.sub(_fill_slot, rendered)
