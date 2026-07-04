// Shared i18n runtime for all screenscribe browser surfaces.
// Generated from the former REVIEW and ANALYZE dictionaries; edit this file
// when adding UI strings, keeping en/pl parity in every namespace.

window.I18N_BUNDLE = {
    "en": {
        "shell": {
            "language_toggle_aria": "Language",
            "resize_findings_panel": "Resize findings panel"
        },
        "media": {
            "noSubtitle": "No subtitle",
            "videoUnsupported": "Unable to play this video file (check the format or source).",
            "videoPlaybackFailed": "Failed to start video playback.",
            "staticDemoNoVideo": "Sample report — the source recording is not included.",
            "lightboxClose": "Close (ESC)",
            "lightboxAlt": "Full size",
            "manualFrameZoomTitle": "Click to enlarge and annotate",
            "manualFrameAnnotateHint": "Click to annotate",
            "playLabel": "Play",
            "pauseLabel": "Pause",
            "captureFrame": "Add moment",
            "toolPen": "Pen",
            "toolRect": "Rect",
            "toolArrow": "Arrow",
            "toolText": "Text",
            "toolUndo": "Undo",
            "toolClear": "Clear",
            "toolDone": "Done",
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
            "manualFrameTitle": "Manual moment @ {{ts}}"
        },
        "review": {
            "summary": "Summary",
            "findings": "Moments",
            "export": "Export",
            "transcript": "Transcript",
            "searchTranscript": "Search transcript...",
            "total": "Total",
            "critical": "Critical",
            "high": "High",
            "medium": "Medium",
            "low": "Low",
            "executiveSummary": "Executive Summary",
            "noSummary": "No AI summary available",
            "pipelineErrors": "Pipeline Errors",
            "review": "Review",
            "verdict": "Confirmed?",
            "yes": "Yes",
            "noFalseAlarm": "No / False alarm",
            "changePriority": "Change priority",
            "noChange": "-- No change --",
            "notes": "Notes / Actions",
            "notesPlaceholder": "Your notes, actions to take...",
            "reviewer": "Reviewer:",
            "reviewerPlaceholder": "Your name",
            "embedScreenshots": "Embed screenshots (external audit)",
            "exportJson": "Export JSON",
            "exportTodo": "Export TODO",
            "exportDoneSimple": "Export complete:",
            "noReviewed": "No findings reviewed. Export anyway?",
            "draftRestored": "Draft restored",
            "draftSaved": "Draft saved",
            "draftQuotaWarning": "Local draft is full — your decisions are kept and saved to the server; only the browser cache could not be updated.",
            "reviewSaved": "Review saved to the report",
            "findingRejected": "Finding rejected",
            "findingAccepted": "Finding accepted",
            "mergeFindingsBtn": "Merge selected",
            "mergeNeedTwo": "Select at least 2 findings to merge",
            "mergeDone": "Findings merged into one",
            "mergedBadge": "MERGED",
            "mergedFromLabel": "Merged from",
            "mergeSelectLabel": "Select for merge",
            "todoRejectedSection": "Rejected as false alarm",
            "findingSummary": "Summary:",
            "affectedComponents": "Affected Components",
            "suggestedFix": "Suggested Fix",
            "visualIssues": "Visual Issues",
            "clickToSeek": "Click to jump to this moment",
            "aiSuggestions": "AI Suggestions:",
            "exportZip": "Export ZIP",
            "exportZipTitle": "ZIP with annotated screenshots",
            "generatingZip": "Generating ZIP...",
            "zipExported": "ZIP exported:",
            "zipError": "ZIP export error:",
            "saveToDisk": "Save review",
            "detachReview": "Open Review Window",
            "focusReview": "Focus Review Window",
            "attachWorkspace": "Return to One Window",
            "separateWindowOpened": "Review panel opened in a separate window.",
            "separateWindowFocused": "Review panel is already open.",
            "separateWindowBlocked": "The browser blocked the separate review window.",
            "separateWindowClosed": "The separate review window was closed.",
            "singleWindowRestored": "Returned to the unified workspace.",
            "unsavedChangesWarning": "You have unsaved changes.",
            "voiceNote": "Voice note",
            "voiceRecording": "Recording...",
            "voiceTranscribing": "Transcribing...",
            "voiceReady": "Speech added to notes",
            "voiceMicOff": "Microphone off.",
            "voiceNotSupported": "This browser does not support speech recognition",
            "voiceDenied": "Microphone access was denied",
            "voiceError": "Speech recognition error",
            "todoReviewerLabel": "Reviewer",
            "todoDateLabel": "Date",
            "todoAiFindingsSection": "AI findings",
            "todoNoAiFindings": "No AI findings.",
            "todoNotesLabel": "Notes",
            "todoActionsLabel": "Actions",
            "todoManualSection": "Manual captures",
            "todoNoManualCaptures": "No manual captures.",
            "todoManualItemLabel": "Manual",
            "todoManualCaptureDefault": "Manual capture",
            "todoCategoryLabel": "Category",
            "todoTranscriptLabel": "Transcript",
            "todoSuggestedFixLabel": "Suggested fix",
            "todoManualNotAnalyzed": "Manual capture, not AI-analyzed yet",
            "todoFileLabel": "File",
            "todoNoDescription": "No description",
            "todoAnnotationLabel": "Annotation",
            "annotationArrow": "arrow",
            "annotationRect": "rectangle",
            "annotationPen": "drawing",
            "annotationText": "text",
            "saveFailed": "Save failed: {{message}}",
            "manualFrameAdded": "Moment added.",
            "manualFrameAnalyzed": "Manual moment analyzed.",
            "statusSavingFrame": "Saving moment...",
            "statusRunningAnalysis": "Running VLM analysis...",
            "manualFrameSaveFailed": "Failed to save manual moment.",
            "manualFrameAnalysisFailed": "Manual VLM analysis failed.",
            "manualFrameAnalysisComplete": "Analysis complete.",
            "voiceNoSpeech": "No speech recognized — nothing added.",
            "manualFrameDelete": "Remove moment",
            "manualFrameDeleteConfirm": "Remove this manual moment from the review?",
            "manualFrameDeleted": "Moment removed.",
            "manualFrameEditNote": "Edit note",
            "manualFrameNoteSave": "Save",
            "manualFrameNoteCancel": "Cancel",
            "manualFrameNoPriority": "-- No priority --",
            "category_bug": "Bug",
            "category_change": "Change",
            "category_ui": "UI",
            "category_performance": "Performance",
            "category_accessibility": "Accessibility",
            "category_other": "Other",
            "category_unknown": "Unknown"
        },
        "analyze": {
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
            "mic_permission_denied": "Microphone access denied. Please allow microphone access.",
            "recording": "Recording...",
            "transcript_placeholder": "Voice transcript preview",
            "notes_placeholder": "Text note...",
            "mark_frame": "Add moment",
            "transcript_heading": "Voice notes",
            "transcript_search": "Search transcript...",
            "transcript_empty_1": "Record a voice note while marking a moment.",
            "transcript_empty_2": "The transcript will appear here.",
            "findings_empty_1": "No moments marked yet.",
            "findings_empty_2": "Watch the video and mark important moments.",
            "status_ready": "Ready",
            "status_marking": "Marking moment...",
            "status_frame_marked": "Moment marked",
            "status_frame_marked_without_note": "Moment marked without note",
            "status_transcribing": "Transcribing...",
            "status_mic_off": "Microphone off",
            "status_recording_too_short": "Hold to record longer · Microphone off",
            "status_analyzing": "Analyzing...",
            "status_deleting": "Deleting...",
            "status_delete_failed": "Delete failed",
            "status_analyze_failed": "Analysis failed",
            "status_saving_note": "Saving note...",
            "status_save_failed": "Save failed",
            "status_export_failed": "Export failed",
            "status_finalizing": "Finalizing annotations...",
            "status_finalizing_progress": "Finalizing... {{processed}}/{{total}}",
            "status_building_md": "Building Markdown report...",
            "status_report_ready": "Report ready: {{completed}} completed, {{errors}} errors",
            "status_report_failed": "Report generation failed",
            "export_json": "Download JSON",
            "report_md": "Report MD",
            "export_gate_hint": "Export is available after you add your first moment.",
            "errors_count": "{{n}} errors",
            "action_analyze": "Analyze",
            "action_reanalyze": "Re-analyze",
            "action_edit_note": "Edit note",
            "action_delete": "Delete",
            "action_save": "Save",
            "action_cancel": "Cancel",
            "kafelek_status_analyzing": "Analyzing...",
            "kafelek_status_analyzed": "Analyzed",
            "kafelek_status_error": "Error",
            "kafelek_status_pending": "Pending",
            "no_transcript": "(no transcript)",
            "category_user_marked": "Manual moment",
            "category_bug": "Bug",
            "category_change": "Change",
            "category_ui": "UI",
            "category_performance": "Performance",
            "category_accessibility": "Accessibility",
            "category_other": "Other",
            "category_unknown": "Unknown",
            "action_change_priority": "Change priority",
            "severity_no_change": "-- No priority --",
            "severity_critical": "Critical",
            "severity_high": "High",
            "severity_medium": "Medium",
            "severity_low": "Low",
            "confirm_reanalyze": "Re-analyze this finding? The previous analysis will be overwritten.",
            "confirm_delete": "Delete this finding? This cannot be undone.",
            "modal_aria": "Captured frame preview",
            "modal_alt": "Captured frame",
            "modal_close": "Close",
            "marker_timeline_aria": "Marked moments on video timeline",
            "markers_list_aria": "Marked moments",
            "marker_tick_aria": "Marker at {{time}}",
            "video_status_idle": "Pause the video to mark a moment",
            "video_status_playing": "{{time}} · pause the video to mark a moment",
            "video_status_paused": "Paused at {{time}} — you can add a moment"
        }
    },
    "pl": {
        "shell": {
            "language_toggle_aria": "Język",
            "resize_findings_panel": "Zmień rozmiar panelu znalezisk"
        },
        "media": {
            "noSubtitle": "Brak napisu",
            "videoUnsupported": "Nie można odtworzyć tego pliku wideo (sprawdź format lub źródło).",
            "videoPlaybackFailed": "Nie udało się uruchomić odtwarzania wideo.",
            "staticDemoNoVideo": "Raport przykładowy — nagranie źródłowe nie jest dołączone.",
            "lightboxClose": "Zamknij (ESC)",
            "lightboxAlt": "Pełny rozmiar",
            "manualFrameZoomTitle": "Kliknij, aby powiększyć i adnotować",
            "manualFrameAnnotateHint": "Kliknij, aby adnotować",
            "playLabel": "Odtwórz",
            "pauseLabel": "Pauza",
            "captureFrame": "Dodaj moment",
            "toolPen": "Ołówek",
            "toolRect": "Prostokąt",
            "toolArrow": "Strzałka",
            "toolText": "Tekst",
            "toolUndo": "Cofnij",
            "toolClear": "Wyczyść",
            "toolDone": "Gotowe",
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
            "manualFrameTitle": "Ręczny moment @ {{ts}}"
        },
        "review": {
            "summary": "Podsumowanie",
            "findings": "Momenty",
            "export": "Eksport",
            "transcript": "Transkrypcja",
            "searchTranscript": "Szukaj w transkrypcji...",
            "total": "Razem",
            "critical": "Krytyczne",
            "high": "Wysokie",
            "medium": "Średnie",
            "low": "Niskie",
            "executiveSummary": "Streszczenie",
            "noSummary": "Brak podsumowania AI",
            "pipelineErrors": "Błędy pipeline",
            "review": "Recenzja",
            "verdict": "Potwierdzone?",
            "yes": "Tak",
            "noFalseAlarm": "Nie / Fałszywy alarm",
            "changePriority": "Zmień priorytet",
            "noChange": "-- Bez zmian --",
            "notes": "Notatki / Akcje",
            "notesPlaceholder": "Twoje uwagi, akcje do podjęcia...",
            "reviewer": "Recenzent:",
            "reviewerPlaceholder": "Twoje imię i nazwisko",
            "embedScreenshots": "Dołącz zrzuty ekranu (do zewnętrznego audytu)",
            "exportJson": "Eksportuj JSON",
            "exportTodo": "Eksportuj TODO",
            "exportDoneSimple": "Eksport ukończony:",
            "noReviewed": "Nie przejrzano żadnych znalezisk. Eksportować mimo to?",
            "draftRestored": "Przywrócono wersję roboczą",
            "draftSaved": "Zapisano wersję roboczą",
            "draftQuotaWarning": "Lokalna wersja robocza jest pełna — decyzje są zachowane i zapisane na serwerze; nie udało się zaktualizować jedynie lokalnej pamięci podręcznej przeglądarki.",
            "reviewSaved": "Recenzja zapisana do raportu",
            "findingRejected": "Znalezisko odrzucone",
            "findingAccepted": "Znalezisko potwierdzone",
            "mergeFindingsBtn": "Scal zaznaczone",
            "mergeNeedTwo": "Zaznacz co najmniej 2 znaleziska do scalenia",
            "mergeDone": "Znaleziska scalone w jedno",
            "mergedBadge": "SCALONE",
            "mergedFromLabel": "Scalone z",
            "mergeSelectLabel": "Zaznacz do scalenia",
            "todoRejectedSection": "Odrzucone jako fałszywy alarm",
            "findingSummary": "Podsumowanie:",
            "affectedComponents": "Powiązane komponenty",
            "suggestedFix": "Sugerowana poprawka",
            "visualIssues": "Wizualne problemy",
            "clickToSeek": "Kliknij, aby przejść do tego momentu",
            "aiSuggestions": "Sugestie AI:",
            "exportZip": "Eksportuj ZIP",
            "exportZipTitle": "ZIP z adnotowanymi screenshotami",
            "generatingZip": "Generowanie ZIP...",
            "zipExported": "ZIP wyeksportowany:",
            "zipError": "Błąd eksportu ZIP:",
            "saveToDisk": "Zapisz recenzję",
            "detachReview": "Otwórz okno recenzji",
            "focusReview": "Pokaż okno recenzji",
            "attachWorkspace": "Wróć do jednego okna",
            "separateWindowOpened": "Panel recenzji otwarty w osobnym oknie.",
            "separateWindowFocused": "Panel recenzji jest już otwarty.",
            "separateWindowBlocked": "Przeglądarka zablokowała osobne okno recenzji.",
            "separateWindowClosed": "Osobne okno recenzji zostało zamknięte.",
            "singleWindowRestored": "Wrócono do jednego okna.",
            "unsavedChangesWarning": "Masz niezapisane zmiany.",
            "voiceNote": "Notatka głosowa",
            "voiceRecording": "Nagrywanie...",
            "voiceTranscribing": "Transkrypcja...",
            "voiceReady": "Mowa dodana do notatki",
            "voiceMicOff": "Mikrofon wyłączony.",
            "voiceNotSupported": "Ta przeglądarka nie obsługuje rozpoznawania mowy",
            "voiceDenied": "Dostęp do mikrofonu został zablokowany",
            "voiceError": "Błąd rozpoznawania mowy",
            "todoReviewerLabel": "Recenzent",
            "todoDateLabel": "Data",
            "todoAiFindingsSection": "Znaleziska AI",
            "todoNoAiFindings": "Brak znalezisk AI.",
            "todoNotesLabel": "Notatki",
            "todoActionsLabel": "Akcje",
            "todoManualSection": "Ręczne przechwycenia",
            "todoNoManualCaptures": "Brak ręcznych przechwyceń.",
            "todoManualItemLabel": "Ręczne",
            "todoManualCaptureDefault": "Ręczne przechwycenie",
            "todoCategoryLabel": "Kategoria",
            "todoTranscriptLabel": "Transkrypcja",
            "todoSuggestedFixLabel": "Sugerowana poprawka",
            "todoManualNotAnalyzed": "Ręczne przechwycenie, jeszcze nieanalizowane przez AI",
            "todoFileLabel": "Plik",
            "todoNoDescription": "Brak opisu",
            "todoAnnotationLabel": "Anotacja",
            "annotationArrow": "strzałka",
            "annotationRect": "prostokąt",
            "annotationPen": "rysunek",
            "annotationText": "tekst",
            "saveFailed": "Zapis nie powiódł się: {{message}}",
            "manualFrameAdded": "Moment dodany.",
            "manualFrameAnalyzed": "Ręczny moment przeanalizowany.",
            "statusSavingFrame": "Zapisywanie momentu...",
            "statusRunningAnalysis": "Uruchamianie analizy VLM...",
            "manualFrameSaveFailed": "Nie udało się zapisać ręcznego momentu.",
            "manualFrameAnalysisFailed": "Analiza VLM ręcznego momentu nie powiodła się.",
            "manualFrameAnalysisComplete": "Analiza ukończona.",
            "voiceNoSpeech": "Nie rozpoznano mowy — nic nie dodano.",
            "manualFrameDelete": "Usuń moment",
            "manualFrameDeleteConfirm": "Usunąć ten ręczny moment z recenzji?",
            "manualFrameDeleted": "Moment usunięty.",
            "manualFrameEditNote": "Edytuj notatkę",
            "manualFrameNoteSave": "Zapisz",
            "manualFrameNoteCancel": "Anuluj",
            "manualFrameNoPriority": "-- Bez priorytetu --",
            "category_bug": "Błąd",
            "category_change": "Zmiana",
            "category_ui": "UI",
            "category_performance": "Wydajność",
            "category_accessibility": "Dostępność",
            "category_other": "Inne",
            "category_unknown": "Nieznane"
        },
        "analyze": {
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
            "mic_permission_denied": "Mikrofon zablokowany. Zezwól na dostęp do mikrofonu.",
            "recording": "Nagrywanie...",
            "transcript_placeholder": "Podgląd transkrypcji notatki głosowej",
            "notes_placeholder": "Notatka tekstowa...",
            "mark_frame": "Dodaj moment",
            "transcript_heading": "Notatki głosowe",
            "transcript_search": "Szukaj w transkrypcji...",
            "transcript_empty_1": "Nagraj notatkę głosową przy oznaczaniu momentu.",
            "transcript_empty_2": "Transkrypcja pojawi się tutaj.",
            "findings_empty_1": "Brak oznaczonych momentów.",
            "findings_empty_2": "Oglądaj film i oznaczaj ważne momenty.",
            "status_ready": "Gotowe",
            "status_marking": "Oznaczanie momentu...",
            "status_frame_marked": "Moment oznaczony",
            "status_frame_marked_without_note": "Moment oznaczony bez notatki",
            "status_transcribing": "Transkrypcja...",
            "status_mic_off": "Mikrofon wyłączony",
            "status_recording_too_short": "Przytrzymaj dłużej, żeby nagrać · Mikrofon wyłączony",
            "status_analyzing": "Analizowanie...",
            "status_deleting": "Usuwanie...",
            "status_delete_failed": "Usuwanie nie powiodło się",
            "status_analyze_failed": "Analiza nie powiodła się",
            "status_saving_note": "Zapisywanie notatki...",
            "status_save_failed": "Zapis nie powiódł się",
            "status_export_failed": "Eksport nie powiódł się",
            "status_finalizing": "Finalizowanie analiz...",
            "status_finalizing_progress": "Finalizowanie... {{processed}}/{{total}}",
            "status_building_md": "Generowanie raportu Markdown...",
            "status_report_ready": "Raport gotowy: {{completed}} ukończono, {{errors}} błędów",
            "status_report_failed": "Generowanie raportu nie powiodło się",
            "export_json": "Pobierz JSON",
            "report_md": "Pobierz raport",
            "export_gate_hint": "Eksport będzie dostępny po dodaniu pierwszego momentu.",
            "errors_count": "{{n}} błędów",
            "action_analyze": "Analizuj",
            "action_reanalyze": "Analizuj ponownie",
            "action_edit_note": "Edytuj notatkę",
            "action_delete": "Usuń",
            "action_save": "Zapisz",
            "action_cancel": "Anuluj",
            "kafelek_status_analyzing": "Analizowanie...",
            "kafelek_status_analyzed": "Przeanalizowane",
            "kafelek_status_error": "Błąd",
            "kafelek_status_pending": "Oczekuje",
            "no_transcript": "(brak transkrypcji)",
            "category_user_marked": "Moment ręczny",
            "category_bug": "Błąd",
            "category_change": "Zmiana",
            "category_ui": "UI",
            "category_performance": "Wydajność",
            "category_accessibility": "Dostępność",
            "category_other": "Inne",
            "category_unknown": "Nieznane",
            "action_change_priority": "Zmień priorytet",
            "severity_no_change": "-- Bez priorytetu --",
            "severity_critical": "Krytyczne",
            "severity_high": "Wysokie",
            "severity_medium": "Średnie",
            "severity_low": "Niskie",
            "confirm_reanalyze": "Przeanalizować ponownie? Poprzednia analiza zostanie nadpisana.",
            "confirm_delete": "Usunąć to znalezisko? Tej operacji nie można cofnąć.",
            "modal_aria": "Podgląd przechwyconej klatki",
            "modal_alt": "Przechwycona klatka",
            "modal_close": "Zamknij",
            "marker_timeline_aria": "Oznaczone momenty na osi czasu wideo",
            "markers_list_aria": "Oznaczone momenty",
            "marker_tick_aria": "Marker w czasie {{time}}",
            "video_status_idle": "Zatrzymaj film, żeby oznaczyć moment",
            "video_status_playing": "{{time}} · zatrzymaj film, żeby oznaczyć moment",
            "video_status_paused": "Zatrzymane na {{time}} — możesz dodać moment"
        }
    }
};

function getCurrentI18nLanguage() {
    if (typeof currentLang !== 'undefined' && currentLang) return currentLang;
    const bodyLang = document.body?.dataset?.defaultLang || document.body?.dataset?.reportLanguage;
    if (bodyLang && window.I18N_BUNDLE[bodyLang]) return bodyLang;
    const htmlLang = document.documentElement?.lang?.slice(0, 2);
    if (htmlLang && window.I18N_BUNDLE[htmlLang]) return htmlLang;
    return 'en';
}

function hasI18nLanguage(lang) {
    return Boolean(window.I18N_BUNDLE[lang]);
}

function resolveI18nKey(key) {
    if (!key) return { namespace: '', item: '' };
    if (key.includes('.')) {
        const index = key.indexOf('.');
        return { namespace: key.slice(0, index), item: key.slice(index + 1) };
    }
    const surface = document.body?.dataset?.mode === 'analyze' ? 'analyze' : 'review';
    for (const namespace of [surface, 'media', 'shell']) {
        if (window.I18N_BUNDLE.en?.[namespace]?.[key] !== undefined
            || window.I18N_BUNDLE.pl?.[namespace]?.[key] !== undefined) {
            return { namespace, item: key };
        }
    }
    return { namespace: surface, item: key };
}

function formatI18n(template, args) {
    let output = String(template ?? '');
    if (!args) return output;
    for (const [key, value] of Object.entries(args)) {
        output = output.split('{{' + key + '}}').join(String(value));
        output = output.split('{' + key + '}').join(String(value));
    }
    return output;
}

function t(key, args) {
    const lang = getCurrentI18nLanguage();
    const { namespace, item } = resolveI18nKey(key);
    const value = window.I18N_BUNDLE[lang]?.[namespace]?.[item]
        ?? window.I18N_BUNDLE.en?.[namespace]?.[item]
        ?? key;
    return formatI18n(value, args);
}

function parseI18nArgs(raw) {
    if (!raw) return {};
    const trimmed = raw.trim();
    if (!trimmed) return {};
    if (trimmed.startsWith('{')) {
        try { return JSON.parse(trimmed); } catch (_err) { return {}; }
    }
    const values = trimmed.split(',').map((value) => value.trim());
    return { n: values[0] || '0' };
}

function applyTranslations(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach((el) => {
        const key = el.getAttribute('data-i18n');
        if (!key) return;
        const value = t(key);
        if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.placeholder) {
            el.placeholder = value;
        } else if (key === 'voiceNote' && el.classList.contains('notes-mic-btn')) {
            el.textContent = '🎤 ' + value;
        } else if (key === 'manualFrameHoldToRecord' && el.id === 'manualFrameMicBtn') {
            el.textContent = '🎤 ' + value;
        } else {
            el.textContent = value;
        }
    });
    scope.querySelectorAll('[data-i18n-attr]').forEach((el) => {
        const spec = el.getAttribute('data-i18n-attr');
        if (!spec) return;
        spec.split(',').forEach((pair) => {
            const [attr, key] = pair.split(':').map((part) => part.trim());
            if (attr && key) el.setAttribute(attr, t(key));
        });
    });
    scope.querySelectorAll('[data-i18n-title]').forEach((el) => {
        const key = el.getAttribute('data-i18n-title');
        if (key) el.title = t(key);
    });
    scope.querySelectorAll('[data-i18n-alt]').forEach((el) => {
        const key = el.getAttribute('data-i18n-alt');
        if (key) el.alt = t(key);
    });
    scope.querySelectorAll('[data-i18n-tpl]').forEach((el) => {
        const key = el.getAttribute('data-i18n-tpl');
        if (key) el.textContent = t(key, parseI18nArgs(el.getAttribute('data-i18n-tpl-args')));
    });
}

window.getCurrentI18nLanguage = getCurrentI18nLanguage;
window.hasI18nLanguage = hasI18nLanguage;
window.t = t;
window.applyTranslations = applyTranslations;
