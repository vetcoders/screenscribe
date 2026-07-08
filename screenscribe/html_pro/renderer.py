"""HTML Pro report renderer.

Main rendering functions for the screenscribe Pro HTML report.
"""

from __future__ import annotations

import base64
import html
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from markdown_it import MarkdownIt

from ..shell import REVIEW_SURFACE, render_surface
from .data import generate_report_id, prepare_findings_json, prepare_segments_json

if TYPE_CHECKING:
    from ..transcribe import Segment

# Shared CommonMark renderer for LLM-authored prose (the executive summary).
# ``html=False`` escapes any raw HTML in the model output, so ``**bold**`` / lists
# render while injected markup (e.g. ``<script>``) stays inert text. Verified by
# runtime probe: markdown-it-py escapes raw HTML to entities under this config.
# ``.disable("image")`` drops the CommonMark image rule so ``![x](https://host)``
# never emits an <img>: a remote image URL (e.g. via prompt injection) would make
# the local report beacon a third-party host on open — a data-exfil vector. The
# image syntax then stays inert text.
_SUMMARY_MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable("image")


def _render_summary_markdown(text: str) -> str:
    """Render an LLM-authored summary as safe HTML (raw HTML escaped)."""
    # markdown-it-py's render() is untyped (returns Any); coerce to str so the
    # str-returning contract holds under strict mypy.
    return str(_SUMMARY_MARKDOWN.render(text or "")).strip()


_I18N: dict[str, dict[str, str]] = {
    "pl": {
        "total": "Razem",
        "critical": "Krytyczne",
        "high": "Wysokie",
        "medium": "Średnie",
        "low": "Niskie",
        "pipelineErrors": "Błędy pipeline",
        "affectedComponents": "Powiązane komponenty",
        "mergedFrom": "Scalone z",
        "mergedEvidence": "Scalone klatki dowodowe",
        "suggestedFix": "Sugerowana poprawka",
        "visualIssues": "Wizualne problemy",
        "clickToAnnotate": "Kliknij, aby adnotować",
        "clickToSeek": "Kliknij, aby przejść do tego momentu",
        "clickToEnlargeAnnotate": "Kliknij, aby powiększyć i adnotować",
        "findingSummary": "Podsumowanie:",
        "noHtml5Video": "Twoja przeglądarka nie obsługuje wideo HTML5.",
        "summaryFallbackWithErrors": (
            "Raport nadal zawiera {count} wykrytych znalezisk, transkrypcję i screenshoty, "
            "ale warstwa AI nie wygenerowała podsumowania. Sprawdź błędy pipeline poniżej "
            "oraz zakładkę Znaleziska."
        ),
        "summaryFallbackWithFindings": (
            "Raport zawiera {count} wykrytych znalezisk, ale warstwa AI nie zwróciła "
            "osobnego podsumowania."
        ),
        "summaryFallbackEmpty": "Brak podsumowania AI",
        "executiveSummary": "Streszczenie",
        "noSummary": "Brak podsumowania AI",
        "review": "Recenzja",
        "verdict": "Potwierdzone?",
        "yes": "Tak",
        "noFalseAlarm": "Nie / Fałszywy alarm",
        "changePriority": "Zmień priorytet",
        "noChange": "-- Bez zmian --",
        "notes": "Notatki / Akcje",
        "aiSuggestions": "Sugestie AI:",
        "voiceNote": "Notatka głosowa",
        "notesPlaceholder": "Twoje uwagi, akcje do podjęcia...",
        # Header window-mode buttons
        "detachReview": "Otwórz okno recenzji",
        "attachWorkspace": "Wróć do jednego okna",
        # Video controls
        "playLabel": "Odtwórz",
        "captureFrame": "Dodaj moment",
        # Lightbox annotation toolbar
        "toolPen": "Ołówek",
        "toolRect": "Prostokąt",
        "toolArrow": "Strzałka",
        "toolText": "Tekst",
        "toolUndo": "Cofnij",
        "toolClear": "Wyczyść",
        "toolDone": "Gotowe",
        # Manual frame section + modal
        "manualFramesHeading": "Ręczne momenty",
        "manualFrameAnalysis": "Analiza ręcznego momentu",
        "manualFramePreviewAlt": "Podgląd przechwyconej klatki",
        "manualFrameTimestamp": "Znacznik czasu",
        "manualFrameSpokenDescription": "Opis mówiony",
        "manualFrameHoldToRecord": "Przytrzymaj, aby nagrać",
        "manualFrameNoSpoken": "Brak opisu mówionego.",
        "manualFrameNotes": "Notatki",
        "manualFrameNotesPlaceholder": "Dodaj opcjonalny kontekst dla tego momentu...",
        "manualFrameReady": "Gotowe",
        "manualFrameCancel": "Anuluj",
        "manualFrameAdd": "Dodaj moment",
        "manualFrameAnalyze": "Analizuj moment",
        "severityBadgeAria": "Ważność",
        "category_bug": "Błąd",
        "category_change": "Zmiana",
        "category_ui": "UI",
        "category_performance": "Wydajność",
        "category_accessibility": "Dostępność",
        "category_other": "Inne",
        "category_unknown": "Nieznane",
    },
    "en": {
        "total": "Total",
        "critical": "Critical",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
        "pipelineErrors": "Pipeline Errors",
        "affectedComponents": "Affected Components",
        "mergedFrom": "Merged from",
        "mergedEvidence": "Merged evidence frames",
        "suggestedFix": "Suggested Fix",
        "visualIssues": "Visual Issues",
        "clickToAnnotate": "Click to annotate",
        "clickToSeek": "Click to jump to this moment",
        "clickToEnlargeAnnotate": "Click to enlarge and annotate",
        "findingSummary": "Summary:",
        "noHtml5Video": "Your browser does not support HTML5 video.",
        "summaryFallbackWithErrors": (
            "This report still contains {count} detected findings, transcript data, and "
            "screenshots, but the AI layer did not produce a summary. Check the pipeline "
            "errors below and review the Findings tab."
        ),
        "summaryFallbackWithFindings": (
            "This report contains {count} detected findings, but the AI layer did not "
            "return a separate summary."
        ),
        "summaryFallbackEmpty": "No AI summary available",
        "executiveSummary": "Executive Summary",
        "noSummary": "No AI summary available",
        # Header window-mode buttons
        "detachReview": "Open Review Window",
        "attachWorkspace": "Return to One Window",
        # Video controls
        "playLabel": "Play",
        "captureFrame": "Add moment",
        # Lightbox annotation toolbar
        "toolPen": "Pen",
        "toolRect": "Rect",
        "toolArrow": "Arrow",
        "toolText": "Text",
        "toolUndo": "Undo",
        "toolClear": "Clear",
        "toolDone": "Done",
        # Manual frame section + modal
        "manualFramesHeading": "Manual Moments",
        "manualFrameAnalysis": "Manual Moment Analysis",
        "manualFramePreviewAlt": "Captured frame preview",
        "manualFrameTimestamp": "Timestamp",
        "manualFrameSpokenDescription": "Spoken description",
        "manualFrameHoldToRecord": "Hold to record",
        "manualFrameNoSpoken": "No spoken description yet.",
        "manualFrameNotes": "Notes",
        "manualFrameNotesPlaceholder": "Add optional context for this moment...",
        "manualFrameReady": "Ready",
        "manualFrameCancel": "Cancel",
        "manualFrameAdd": "Add Moment",
        "manualFrameAnalyze": "Analyze Moment",
        "severityBadgeAria": "Severity",
        "review": "Review",
        "verdict": "Confirmed?",
        "yes": "Yes",
        "noFalseAlarm": "No / False alarm",
        "changePriority": "Change priority",
        "noChange": "-- No change --",
        "notes": "Notes / Actions",
        "aiSuggestions": "AI Suggestions:",
        "voiceNote": "Voice note",
        "notesPlaceholder": "Your notes, actions to take...",
        "category_bug": "Bug",
        "category_change": "Change",
        "category_ui": "UI",
        "category_performance": "Performance",
        "category_accessibility": "Accessibility",
        "category_other": "Other",
        "category_unknown": "Unknown",
    },
}


def _t(key: str, language: str = "en") -> str:
    """Get translated string for key, falling back to Polish."""
    return _I18N.get(language, _I18N["pl"]).get(key, _I18N["pl"].get(key, key))


def _render_errors(errors: list[dict[str, str]], language: str = "en") -> str:
    """Render pipeline errors section."""
    if not errors:
        return ""

    lines = [
        '<div class="errors-section">',
        f'<h3 data-i18n="pipelineErrors">{_t("pipelineErrors", language)}</h3>',
        "<ul>",
    ]

    for error in errors:
        stage = html.escape(error.get("stage", "unknown"))
        message = html.escape(error.get("message", ""))
        lines.append(f"<li><strong>{stage}:</strong> {message}</li>")

    lines.extend(["</ul>", "</div>"])
    return "\n".join(lines)


_SEVERITY_ALLOWLIST: frozenset[str] = frozenset({"critical", "high", "medium", "low", "none"})
_SEVERITY_FALLBACK = "none"


def _clamp_severity(value: Any) -> str:
    """Clamp an LLM-controlled severity to a safe allowlist token.

    Severity flows into a `class` attribute and an aria-label in the report.
    The model is not trusted, so any value outside the allowlist (including
    attribute-breakout payloads like `high" onmouseover=...`) is collapsed to
    a deterministic safe token before it can reach the HTML.
    """
    if not isinstance(value, str):
        return _SEVERITY_FALLBACK
    token = value.strip().lower()
    return token if token in _SEVERITY_ALLOWLIST else _SEVERITY_FALLBACK


def _coerce_timestamp_seconds(value: Any) -> float:
    """Coerce an LLM/STT-controlled timestamp into a safe numeric value.

    The timestamp is emitted into a ``data-timestamp`` attribute consumed by
    JS seek logic, so a string payload like ``0);alert(1)//`` must never reach
    the output. Non-convertible / non-finite values collapse to ``0.0``.
    """
    try:
        coerced = float(value)
    except (TypeError, ValueError):
        return 0.0
    if coerced != coerced or coerced in (float("inf"), float("-inf")):
        return 0.0
    return coerced


def _render_finding(f: dict[str, Any], index: int, language: str = "en") -> str:
    """Render a single finding as an article element."""
    finding_id = f.get("id", index)
    finding_id_attr = html.escape(str(finding_id), quote=True)
    category = f.get("category", "unknown")
    # Category badges must speak the report language, not leak the raw EN enum
    # (BUG/PERFORMANCE/...). Route through i18n; an unmapped category falls back
    # to its raw value. The badge stays upper-cased for visual parity.
    category_label = _t(f"category_{category}", language)
    if category_label == f"category_{category}":
        category_label = category
    category_display = html.escape(category_label.upper())
    timestamp = f.get("timestamp_formatted", "00:00")
    timestamp_seconds = _coerce_timestamp_seconds(f.get("timestamp", 0))
    text = html.escape(f.get("text", ""))
    screenshot = f.get("screenshot", "")

    unified = f.get("unified_analysis", {})
    severity = _clamp_severity(unified.get("severity", "medium"))
    summary = html.escape(unified.get("summary", ""))
    suggested_fix = html.escape(unified.get("suggested_fix", ""))
    affected_components = unified.get("affected_components", [])
    issues_detected = unified.get("issues_detected", [])
    action_items = unified.get("action_items", [])
    merged_from_ids = unified.get("merged_from_ids", []) or []

    severity_class = f"severity-{severity}" if severity else "severity-none"

    details_html = ""
    # Provenance trace: a finding folded from N earlier findings (server-side
    # LLM-merge) is shown as merged so the report groups, not hides, the union.
    if merged_from_ids:
        merged_count = len(merged_from_ids)
        details_html += (
            f'<dt data-i18n="mergedFrom">{_t("mergedFrom", language)}</dt>'
            f'<dd class="finding-merged-from">{merged_count}</dd>'
        )
    if affected_components:
        components = ", ".join(html.escape(c) for c in affected_components)
        details_html += (
            f'<dt data-i18n="affectedComponents">{_t("affectedComponents", language)}</dt>'
            f"<dd>{components}</dd>"
        )
    if suggested_fix:
        details_html += (
            f'<dt data-i18n="suggestedFix">{_t("suggestedFix", language)}</dt>'
            f"<dd>{suggested_fix}</dd>"
        )
    if issues_detected:
        issues = "; ".join(html.escape(i) for i in issues_detected)
        details_html += (
            f'<dt data-i18n="visualIssues">{_t("visualIssues", language)}</dt><dd>{issues}</dd>'
        )

    screenshot_html = ""
    if screenshot:
        escaped_src = html.escape(screenshot)
        screenshot_html = f"""
        <div class="finding-screenshot">
            <div class="annotation-container" data-finding-id="{finding_id_attr}">
                <img class="thumbnail" src="{escaped_src}" data-full="{escaped_src}"
                     alt="Screenshot @ {timestamp}" title="{_t("clickToEnlargeAnnotate", language)}">
                <svg class="annotation-svg"></svg>
                <div class="annotation-hint">{_t("clickToAnnotate", language)}</div>
            </div>
        </div>
        """

    action_items_display = ", ".join(action_items) if action_items else ""

    # Merged-away evidence frames (G6b): the screenshots + transcript of the
    # findings auto-folded into this survivor. The counter above only states HOW
    # MANY were merged; this block renders the absorbed members' actual evidence
    # so it is not lost from the report UI.
    merged_frames = f.get("merged_frames") or []
    merged_frames_html = ""
    if merged_frames:
        members_html = ""
        for member in merged_frames:
            if not isinstance(member, dict):
                continue
            member_ts = html.escape(str(member.get("timestamp_formatted", "00:00")))
            member_text = html.escape(str(member.get("text", "")))
            member_src = member.get("screenshot_path") or member.get("screenshot") or ""
            member_thumb = ""
            if member_src:
                escaped_member_src = html.escape(str(member_src))
                member_thumb = (
                    f'<img class="merged-frame-thumb" src="{escaped_member_src}" '
                    f'alt="Screenshot @ {member_ts}" loading="lazy">'
                )
            members_html += (
                '<li class="merged-frame">'
                f'<span class="merged-frame-time">@ {member_ts}</span>'
                f"{member_thumb}"
                f'<span class="merged-frame-transcript">{member_text}</span>'
                "</li>"
            )
        if members_html:
            merged_frames_html = (
                '<div class="finding-merged-frames">'
                f'<dt data-i18n="mergedEvidence">{_t("mergedEvidence", language)}</dt>'
                f'<ul class="merged-frame-list">{members_html}</ul>'
                "</div>"
            )

    return f"""
    <article class="finding" data-finding-id="{finding_id_attr}" data-verdict=""
             data-severity="{html.escape(severity)}">
        <div class="finding-header">
            <div>
                <span class="finding-title">
                    <span class="index">#{index}</span>
                    {category_display}
                </span>
                <span class="finding-meta" data-timestamp="{timestamp_seconds}"
                      title="{_t("clickToSeek", language)}">@ {html.escape(timestamp)}</span>
            </div>
            <span class="severity-badge {severity_class}"
                  aria-label="{_t("severityBadgeAria", language)}: {html.escape(severity)}">{html.escape(severity)}</span>
        </div>

        <div class="finding-content">
            <div class="finding-transcript">{text}</div>
            {f'<div class="finding-summary"><strong data-i18n="findingSummary">{html.escape(_t("findingSummary", language))}</strong> {summary}</div>' if summary else ""}
            <dl class="finding-details">
                {details_html}
            </dl>
            {screenshot_html}
            {merged_frames_html}
        </div>

        <div class="human-review">
            <h4 data-i18n="review">{html.escape(_t("review", language))}</h4>
            <div class="review-row">
                <div class="review-field">
                    <label data-i18n="verdict">{html.escape(_t("verdict", language))}</label>
                    <div class="radio-group">
                        <label>
                            <input type="radio" name="verdict-{finding_id_attr}" value="accepted">
                            <span data-i18n="yes">{html.escape(_t("yes", language))}</span>
                        </label>
                        <label>
                            <input type="radio" name="verdict-{finding_id_attr}" value="rejected">
                            <span data-i18n="noFalseAlarm">{html.escape(_t("noFalseAlarm", language))}</span>
                        </label>
                    </div>
                </div>
                <div class="review-field">
                    <label data-i18n="changePriority">{html.escape(_t("changePriority", language))}</label>
                    <select class="severity-select">
                        <option value="" data-i18n="noChange">{html.escape(_t("noChange", language))}</option>
                        <option value="critical" data-i18n="critical">{html.escape(_t("critical", language))}</option>
                        <option value="high" data-i18n="high">{html.escape(_t("high", language))}</option>
                        <option value="medium" data-i18n="medium">{html.escape(_t("medium", language))}</option>
                        <option value="low" data-i18n="low">{html.escape(_t("low", language))}</option>
                    </select>
                </div>
            </div>
            <div class="review-field notes">
                <label data-i18n="notes">{html.escape(_t("notes", language))}</label>
                {f'<div class="ai-suggestions"><strong data-i18n="aiSuggestions">{html.escape(_t("aiSuggestions", language))}</strong> {html.escape(action_items_display)}</div>' if action_items_display else ""}
                <div class="notes-toolbar">
                    <button type="button"
                            class="notes-mic-btn"
                            data-action="voice-note"
                            data-finding-id="{finding_id_attr}"
                            data-i18n="voiceNote">
                        🎤 {html.escape(_t("voiceNote", language))}
                    </button>
                    <span class="notes-mic-status" data-finding-id="{finding_id_attr}"></span>
                </div>
                <textarea placeholder="{html.escape(_t("notesPlaceholder", language), quote=True)}" data-i18n="notesPlaceholder"></textarea>
            </div>
        </div>
    </article>
    """


def render_html_report_pro(
    video_name: str,
    video_path: str | None,
    generated_at: str,
    executive_summary: str,
    findings: list[dict[str, Any]],
    segments: list[Segment] | None = None,
    errors: list[dict[str, str]] | None = None,
    embed_video: bool = False,
    language: str = "en",
    static_demo: bool = False,
) -> str:
    """Render complete HTML Pro report with video player and synchronized subtitles.

    Args:
        video_name: Name of the source video file
        video_path: Path to the video file (for embedding or reference)
        generated_at: ISO timestamp of report generation
        executive_summary: Executive summary text
        findings: List of finding dictionaries
        segments: Optional list of transcript segments for subtitle sync
        errors: Optional list of pipeline error dictionaries
        embed_video: Whether to embed video as base64 (for smaller files)
        language: Subtitle language code for VTT metadata and track markup
        static_demo: Bake a self-contained "static demo" report (the sample shipped
            on GitHub Pages). Opt-in only; set by the example generator. When True
            the client skips the ``/api/review-state`` hydration fetch (zero network
            requests) and the video panel shows an honest empty state instead of a
            dead player, since the sample carries no source recording.

    Returns:
        Complete HTML document as string
    """
    errors = errors or []
    segments = segments or []

    normalized_language = (language or "pl").strip().lower().replace("_", "-")
    document_language = normalized_language or "pl"
    ui_language = document_language.split("-", 1)[0]
    if ui_language not in {"pl", "en"}:
        ui_language = "pl"

    # Generate unique report ID
    report_id = generate_report_id(video_name, generated_at)

    # Format timestamp
    try:
        dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        display_time = dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        display_time = generated_at

    # Video source handling
    video_src = ""
    if video_path:
        video_path_obj = Path(video_path)
        if embed_video and video_path_obj.exists():
            size_mb = video_path_obj.stat().st_size / (1024 * 1024)
            if size_mb < 50:  # Only embed if < 50MB
                with open(video_path_obj, "rb") as vf:
                    video_b64 = base64.b64encode(vf.read()).decode("ascii")
                media_type = {
                    ".mp4": "video/mp4",
                    ".m4v": "video/mp4",
                    ".mov": "video/quicktime",
                    ".webm": "video/webm",
                    ".ogv": "video/ogg",
                }.get(video_path_obj.suffix.lower(), "video/mp4")
                video_src = f"data:{media_type};base64,{video_b64}"
            else:
                video_src = (
                    video_path_obj.name if video_path_obj.is_absolute() else str(video_path_obj)
                )
        else:
            # Reduce an absolute path to its basename whether or not the source
            # video exists at render time: a shareable report must never embed an
            # absolute local path (home dir / project structure fingerprint) into
            # the <video src="..."> attribute. Mirrors the is_absolute() guard the
            # exists() and embed branches already apply.
            if video_path_obj.is_absolute():
                video_src = video_path_obj.name
            else:
                video_src = video_path

    # Generate VTT data URL for subtitles
    vtt_data_url = ""
    if segments:
        from ..vtt_generator import generate_vtt_data_url

        vtt_data_url = generate_vtt_data_url(segments, language=language)

    # Segments as JSON for JavaScript
    segments_json = prepare_segments_json(segments)

    # Build findings HTML
    findings_html = "\n".join(
        _render_finding(f, i + 1, language=ui_language) for i, f in enumerate(findings)
    )

    # Embed findings as JSON for export
    findings_json = prepare_findings_json(findings)

    # Build video source attribute
    video_src_attr = f'src="{html.escape(video_src)}"' if video_src else ""

    # Build VTT track element
    subtitle_label = {
        "pl": "Polski",
        "en": "English",
    }.get(ui_language, document_language.upper())
    vtt_track = (
        f'<track kind="subtitles" src="{vtt_data_url}" srclang="{html.escape(language)}" '
        f'label="{html.escape(subtitle_label)}" default>'
        if vtt_data_url
        else ""
    )

    # Build executive summary HTML
    if executive_summary:
        executive_summary_html = (
            '<div class="executive-summary">'
            f'<h3 data-i18n="executiveSummary">{html.escape(_t("executiveSummary", ui_language))}</h3>'
            f'<div class="summary-body">{_render_summary_markdown(executive_summary)}</div></div>'
        )
    else:
        if errors:
            fallback_text = _t("summaryFallbackWithErrors", ui_language).format(count=len(findings))
        elif findings:
            fallback_text = _t("summaryFallbackWithFindings", ui_language).format(
                count=len(findings)
            )
        else:
            fallback_text = _t("summaryFallbackEmpty", ui_language)

        executive_summary_html = (
            '<div class="executive-summary executive-summary-warning">'
            f'<h3 data-i18n="noSummary">{html.escape(_t("noSummary", ui_language))}</h3>'
            f"<p>{html.escape(fallback_text)}</p>"
            "</div>"
        )

    context = {
        "document_language": html.escape(document_language),
        "ui_language": html.escape(ui_language),
        "video_name": video_name,
        "video_name_escaped": html.escape(video_name),
        "report_id": report_id,
        "findings_count": len(findings),
        "display_time_escaped": html.escape(display_time),
        "t_detach_review": html.escape(_t("detachReview", ui_language)),
        "t_attach_workspace": html.escape(_t("attachWorkspace", ui_language)),
        "t_play": html.escape(_t("playLabel", ui_language)),
        "t_capture_frame": html.escape(_t("captureFrame", ui_language)),
        "t_tool_pen": html.escape(_t("toolPen", ui_language)),
        "t_tool_rect": html.escape(_t("toolRect", ui_language)),
        "t_tool_arrow": html.escape(_t("toolArrow", ui_language)),
        "t_tool_text": html.escape(_t("toolText", ui_language)),
        "t_tool_undo": html.escape(_t("toolUndo", ui_language)),
        "t_tool_clear": html.escape(_t("toolClear", ui_language)),
        "t_tool_done": html.escape(_t("toolDone", ui_language)),
        "t_manual_frames_heading": html.escape(_t("manualFramesHeading", ui_language)),
        "t_manual_frame_analysis": html.escape(_t("manualFrameAnalysis", ui_language)),
        "t_manual_frame_preview_alt": html.escape(_t("manualFramePreviewAlt", ui_language)),
        "t_manual_frame_timestamp": html.escape(_t("manualFrameTimestamp", ui_language)),
        "t_manual_frame_spoken_description": html.escape(
            _t("manualFrameSpokenDescription", ui_language)
        ),
        "t_manual_frame_hold_to_record": html.escape(_t("manualFrameHoldToRecord", ui_language)),
        "t_manual_frame_no_spoken": html.escape(_t("manualFrameNoSpoken", ui_language)),
        "t_manual_frame_notes": html.escape(_t("manualFrameNotes", ui_language)),
        "t_manual_frame_notes_placeholder": html.escape(
            _t("manualFrameNotesPlaceholder", ui_language)
        ),
        "t_manual_frame_ready": html.escape(_t("manualFrameReady", ui_language)),
        "t_manual_frame_cancel": html.escape(_t("manualFrameCancel", ui_language)),
        "t_manual_frame_add": html.escape(_t("manualFrameAdd", ui_language)),
        "t_manual_frame_analyze": html.escape(_t("manualFrameAnalyze", ui_language)),
        "video_src_attr": video_src_attr,
        "vtt_track": vtt_track,
        "no_html5_video_text": html.escape(_t("noHtml5Video", ui_language)),
        "errors_html": _render_errors(errors, language=ui_language),
        "executive_summary_html": executive_summary_html,
        "findings_html": findings_html,
        "findings_json": findings_json,
        "segments_json": segments_json,
        "static_demo": static_demo,
    }
    return render_surface(REVIEW_SURFACE, context)
