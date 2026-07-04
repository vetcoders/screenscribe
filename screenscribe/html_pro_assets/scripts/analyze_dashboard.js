// =============================================================================
// Localhost session token. The server hands it to us via the URL fragment
// (#token=...), which is never sent to the server (kept out of logs / Referer).
// We echo it back as X-ScreenScribe-Token on same-origin requests and strip it
// from the address bar so it isn't bookmarked. A cross-site page can't read our
// fragment, so it can't forge the header.
// =============================================================================
(function () {
    if (typeof window === 'undefined' || typeof location === 'undefined') { return; }
    // Survive a reload: the token is also cached in sessionStorage (per-tab,
    // origin-isolated, cleared on tab close) keyed by mode+origin+path. Stripping
    // it from the address bar must not strip it from the running session, or every
    // /api/* call (save-note, STT, analyze, delete, mark) 403s after a refresh.
    // The ':analyze:' prefix keeps this key distinct from the review report's
    // token even in a (currently impossible — different port) same-origin+path
    // case, so the two dashboards never read each other's token. A fresh #token
    // always overwrites the cached value.
    var storageKey = 'screenscribe:token:analyze:' + location.origin + location.pathname;
    var hashToken = new URLSearchParams((location.hash || '').replace(/^#/, '')).get('token') || '';
    var token = hashToken;
    if (hashToken) {
        try { sessionStorage.setItem(storageKey, hashToken); } catch (e) { /* ignore */ }
        try { history.replaceState(null, '', location.pathname + location.search); } catch (e) { /* ignore */ }
    }
    if (!token) {
        try { token = sessionStorage.getItem(storageKey) || ''; } catch (e) { token = ''; }
    }
    if (!token) { return; }
    var origFetch = window.fetch.bind(window);
    // Resolve the request target like the browser will (string, URL, or
    // Request; relative or absolute) and attach the token only for
    // same-origin /api requests — robust against future fetch-call refactors.
    function shouldAttachToken(input) {
        var raw = typeof input === 'string'
            ? input
            : (input instanceof URL ? input.href : (input && input.url) || '');
        try {
            var u = new URL(raw, location.href);
            return u.origin === location.origin
                && (u.pathname === '/api' || u.pathname.indexOf('/api/') === 0);
        } catch (e) {
            return false;
        }
    }
    window.fetch = function (input, init) {
        init = init || {};
        if (shouldAttachToken(input)) {
            var headers = new Headers(init.headers || {});
            headers.set('X-ScreenScribe-Token', token);
            init = Object.assign({}, init, { headers: headers });
        }
        return origFetch(input, init);
    };
    // Marker frame thumbnails are NOT covered by this interceptor: they load
    // via <img src>, where the browser cannot attach a custom header. The
    // server instead embeds a per-marker HMAC signature in the frame_url it
    // hands us ("?st=..."), and the guard accepts that signature for GET on
    // exactly that path. Use frame_url as-is; never strip its query string.
})();

// =============================================================================
// ANALYZE MODE - Voice Recording & Frame Marking
// =============================================================================

// Reclaim the native HTML5 video controls. The shared video_player.js attaches
// a click listener directly to the <video> element that toggles play/pause
// (intended for click-on-video-body to toggle playback in the review report).
// On the analyze dashboard that handler races with the browser's native
// controls: clicking play/pause/mute/fullscreen or scrubbing the timeline
// triggers the native default action AND immediately fires the JS toggle,
// which undoes the action. Net visible effect: the controls look dead.
//
// Fix: register an early click listener on the video at script parse time
// (before video_player.js's DOMContentLoaded handler runs) and call
// stopImmediatePropagation so the later-registered toggle handler never
// fires. Native control behavior is the click's default action and is
// unaffected by stopImmediatePropagation. Spacebar play/pause keeps working
// because that keydown listener is bound to `document`, not to the video.
(function reclaimVideoControls() {
    const video = document.getElementById('videoPlayer');
    if (!video) return;
    video.addEventListener('click', (event) => {
        event.stopImmediatePropagation();
    });
})();

class VoiceRecorder {
    constructor(onTranscript) {
        this.isRecording = false;
        this.isStarting = false;
        this.isTranscribing = false;
        this.onTranscript = onTranscript;
        this.recordingStartedAt = 0;
        this.discardCurrentRecording = false;
        this.minRecordingMs = 700;
        this.minAudioBytes = 1024;
        this.transport = window.ScreenScribeLib.createSttTransport({
            onTranscript: (text) => this.onTranscript?.(text),
            onStatus: (message, tone) => {
                const statusText = document.getElementById('statusText');
                if (!statusText) return;
                const statusMessage = tone === 'error'
                    ? `${message} · ${t('analyze.status_mic_off')}`
                    : message;
                statusText.textContent = statusMessage;
            },
            onTranscribingChange: (isTranscribing) => { this.isTranscribing = isTranscribing; },
            onMicError: (error) => console.error('Microphone access denied:', error),
            onError: (error) => console.error('Transcription failed:', error),
            shouldTranscribe: (audioSize) => !this.discardCurrentRecording && audioSize >= this.minAudioBytes,
            onDiscard: () => {
                document.getElementById('statusText').textContent = t('analyze.status_recording_too_short');
            },
            statusTranscribing: t('analyze.status_transcribing'),
            statusReady: t('analyze.status_mic_off'),
            fallbackMessage: 'Transcription failed',
        });
    }

    async start() {
        if (this.isStarting || this.isRecording || this.isTranscribing) {
            return false;
        }

        this.isStarting = true;
        this.discardCurrentRecording = false;
        try {
            const started = await this.transport.start();
            if (!started) {
                alert(t('analyze.mic_permission_denied'));
                return false;
            }
            this.recordingStartedAt = Date.now();
            this.isRecording = this.transport.isRecording;
            return started;
        } finally {
            this.isStarting = false;
        }
    }

    stop() {
        if (this.transport.mediaRecorder && this.isRecording) {
            const elapsedMs = Date.now() - this.recordingStartedAt;
            this.discardCurrentRecording = elapsedMs < this.minRecordingMs;
            this.transport.stop();
            this.isRecording = this.transport.isRecording;
            this.isStarting = false;
        }
    }

    releaseStreamTracks() {
        this.transport.releaseStreamTracks();
    }

    destroy() {
        this.transport.destroy();
        this.isRecording = this.transport.isRecording;
    }

    get stream() {
        return this.transport.stream;
    }

    get mediaRecorder() {
        return this.transport.mediaRecorder;
    }
}

class FrameMarker {
    constructor(video) {
        this.video = video;
    }

    async captureFrame() {
        const canvas = document.createElement('canvas');
        canvas.width = this.video.videoWidth;
        canvas.height = this.video.videoHeight;

        const ctx = canvas.getContext('2d');
        ctx.drawImage(this.video, 0, 0);

        // Get base64 without data URL prefix
        const dataUrl = canvas.toDataURL('image/jpeg', 0.9);
        const base64 = dataUrl.split(',')[1];

        return {
            timestamp: this.video.currentTime,
            frame_base64: base64
        };
    }

    async markCurrentFrame(transcript = '', notes = '') {
        const frameData = await this.captureFrame();

        const marker = {
            ...frameData,
            transcript,
            notes
        };

        document.getElementById('statusText').textContent = t('analyze.status_marking');

        // Mirror review_app's graceful fetch contract: a non-2xx response or a
        // network error must surface as a thrown Error (so the caller can show a
        // failure status) rather than crashing on response.json() of an error
        // body or silently swallowing the failure.
        let response;
        try {
            response = await fetch('/api/mark', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(marker)
            });
        } catch (error) {
            console.error('Mark frame request failed:', error);
            throw error;
        }

        if (!response.ok) {
            const message = 'Mark frame failed: ' + response.status;
            console.error(message);
            throw new Error(message);
        }

        const result = await response.json();

        return result;
    }
}

// Format timestamp as MM:SS
function formatTime(seconds) {
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return m.toString().padStart(2, '0') + ':' + s.toString().padStart(2, '0');
}

// Reactive video status line: reflects play / pause / seek under the player so
// the user always knows whether they can mark. Strings carry a literal {{time}}
// token (matching the dict's {{name}} style); we substitute via replace() rather
// than tFormat, whose single-brace split would leave stray braces around the value.
function setVideoStatus(state, video) {
    const el = document.getElementById('videoStatusLine');
    if (!el) return;
    const v = video || document.getElementById('videoPlayer');
    if (!v) return;
    if (state === 'playing') {
        const dur = Number.isFinite(v.duration) ? formatTime(v.duration) : '00:00';
        const timeStr = `${formatTime(v.currentTime)} / ${dur}`;
        el.textContent = t('analyze.video_status_playing').replace('{{time}}', timeStr);
    } else if (state === 'paused') {
        el.textContent = t('analyze.video_status_paused').replace('{{time}}', formatTime(v.currentTime));
    } else {
        el.textContent = t('analyze.video_status_idle');
    }
}

// Escape HTML to keep user-provided strings (transcript, notes, summaries)
// from breaking the kafelek markup. Used everywhere we splice user text into
// innerHTML.
function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

let activeMarkerId = null;
let statusResetTimer = null;
let currentMarkers = [];

function showTemporaryStatus(message, delay = 2000) {
    const statusEl = document.getElementById('statusText');
    if (!statusEl) return;
    if (statusResetTimer) clearTimeout(statusResetTimer);
    statusEl.textContent = message;
    statusResetTimer = setTimeout(() => {
        if (statusEl.textContent === message) {
            statusEl.textContent = t('analyze.status_ready');
        }
        statusResetTimer = null;
    }, delay);
}

function pulseFindingsTab() {
    const findingsTab = document.querySelector('.tab-btn[data-tab="findings"]');
    if (!findingsTab) return;
    findingsTab.classList.remove('findings-pulse');
    void findingsTab.offsetWidth;
    findingsTab.classList.add('findings-pulse');
    setTimeout(() => findingsTab.classList.remove('findings-pulse'), 1900);
}

function selectMarker(markerId, timestamp, options) {
    activeMarkerId = markerId;
    updateActiveMarkerUi();

    const shouldSeek = !options || options.seek !== false;
    const nextTime = Number(timestamp);
    const video = document.getElementById('videoPlayer');
    if (shouldSeek && video && Number.isFinite(nextTime)) {
        video.currentTime = nextTime;
    }
}

function updateActiveMarkerUi() {
    document.querySelectorAll('.marker-item').forEach((card) => {
        const isActive = card.dataset.markerId === activeMarkerId;
        card.classList.toggle('active', isActive);
        card.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    document.querySelectorAll('.marker-tick').forEach((tick) => {
        const isActive = tick.dataset.markerId === activeMarkerId;
        tick.classList.toggle('active', isActive);
        tick.setAttribute('aria-current', isActive ? 'true' : 'false');
    });
}

function renderMarkerTicks(markers) {
    currentMarkers = Array.isArray(markers) ? markers : [];
    const track = document.getElementById('markerTimelineTrack');
    const video = document.getElementById('videoPlayer');
    if (!track || !video) return;

    track.innerHTML = '';
    const duration = Number(video.duration);
    if (!Number.isFinite(duration) || duration <= 0) return;

    currentMarkers.forEach((marker) => {
        const timestamp = Number(marker.timestamp);
        if (!Number.isFinite(timestamp)) return;
        const percent = Math.max(0, Math.min(100, marker.timestamp / duration * 100));
        const tick = document.createElement('button');
        tick.type = 'button';
        tick.className = 'marker-tick';
        tick.dataset.markerId = marker.marker_id;
        tick.dataset.markerTimestamp = String(timestamp);
        tick.style.left = `${percent}%`;
        const label = t('analyze.marker_tick_aria', { time: formatTime(timestamp) });
        tick.title = formatTime(timestamp);
        tick.setAttribute('aria-label', t('analyze.marker_tick_aria', { time: formatTime(timestamp) }));
        tick.addEventListener('click', (event) => {
            event.stopPropagation();
            selectMarker(marker.marker_id, marker.timestamp);
        });
        track.appendChild(tick);
        void label;
    });

    updateActiveMarkerUi();
}

function rerenderMarkerTicksWhenReady() {
    const video = document.getElementById('videoPlayer');
    if (!video) return;
    if (Number(video.duration) > 0) {
        renderMarkerTicks(currentMarkers);
    }
}

function handleMarkerKeydown(event, markerId, timestamp) {
    if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        event.stopPropagation();
        selectMarker(markerId, timestamp);
    }
}

function formatMarkerCategory(category) {
    if (category === 'user_marked') return t('analyze.category_user_marked');
    if (!category) return '';
    // Known POI categories render through i18n so the badge speaks the UI
    // language; an unmapped value falls back to a title-cased raw string.
    const key = 'analyze.category_' + category;
    const label = t(key);
    if (label !== key) return label;
    return String(category)
        .replace(/_/g, ' ')
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatMarkerSeverity(severity) {
    // No severity set -> render nothing. Analyze has no priority control, so a
    // "No severity" / "Bez ważności" badge only advertises an absent state the
    // user cannot act on. severitySuffix collapses an empty label to no badge. [A7a]
    if (!severity || severity === 'none') return '';
    return String(severity)
        .replace(/_/g, ' ')
        .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

// =============================================================================
// I18N surface state. The shared runtime in i18n.js owns dictionaries,
// lookup, interpolation, and DOM walking; this controller only owns ANALYZE
// language persistence and marker-list refresh after a language switch.
// =============================================================================


const LANG_STORAGE_KEY = 'screenscribe_analyze_lang';

function detectInitialLang() {
    // Priority: localStorage > body[data-default-lang] (= config.language) > "en".
    const body = document.body;
    const fromBody = body && body.dataset ? body.dataset.defaultLang : null;
    return window.ScreenScribeLib?.getInitialLanguage([
        () => localStorage.getItem(LANG_STORAGE_KEY),
        fromBody,
        'en',
    ]) || 'en';
}

let currentLang = detectInitialLang();

function refreshLangToggleUI() {
    document.querySelectorAll('#langToggle button[data-lang]').forEach((btn) => {
        const isActive = btn.getAttribute('data-lang') === currentLang;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });
    const html = document.documentElement;
    if (html) html.setAttribute('lang', currentLang);
}

function setLang(lang, options) {
    if (!hasI18nLanguage(lang)) return;
    currentLang = lang;
    window.ScreenScribeLib?.persistLanguage(LANG_STORAGE_KEY, lang, { mode: 'string' });
    applyTranslations(document);
    refreshLangToggleUI();
    // Re-render markers list because its kafelek text (status label,
    // action buttons, "(no transcript)" placeholder) is built imperatively
    // in updateMarkersList and not picked up by applyI18n's DOM walk.
    if (!options || options.refreshMarkers !== false) {
        if (typeof refreshMarkers === 'function') { refreshMarkers(); }
    }
}

// Helper used by every server call that should run an analysis in the
// currently-selected UI language. Returns a JSON body string with `{lang}`
// so endpoints don't have to special-case "no body".
function langBody(extra) {
    return JSON.stringify(Object.assign({ lang: currentLang }, extra || {}));
}

// Frame preview modal - opens when user clicks a kafelek thumbnail.
// Element that had focus before the frame modal opened; focus is restored to
// it on close so keyboard users land back where they were (a11y focus return).
let frameModalReturnFocus = null;

function openFrameModal(src) {
    const modal = document.getElementById('frameModal');
    const img = document.getElementById('frameModalImg');
    if (!modal || !img) return;
    frameModalReturnFocus = document.activeElement;
    img.src = src;
    modal.classList.add('active');
    modal.setAttribute('aria-hidden', 'false');
    // Move focus into the dialog so Tab/Esc act on it, not the page behind.
    const closeBtn = modal.querySelector('.frame-modal-close');
    if (closeBtn) closeBtn.focus();
}

function closeFrameModal() {
    const modal = document.getElementById('frameModal');
    const img = document.getElementById('frameModalImg');
    if (!modal || !img) return;
    modal.classList.remove('active');
    modal.setAttribute('aria-hidden', 'true');
    img.src = '';
    // Restore focus to the trigger (thumbnail) so the user is not dumped at
    // the top of the document.
    if (frameModalReturnFocus && typeof frameModalReturnFocus.focus === 'function') {
        frameModalReturnFocus.focus();
    }
    frameModalReturnFocus = null;
}

// Update markers list UI. Each kafelek shows the captured frame thumbnail
// alongside the analysis result. The thumbnail is clickable to open a
// full-size modal preview. Strings inside the kafelek are pulled from the
// current i18n dict so the toggle re-renders them correctly.
function updateMarkersList(markers) {
    markers = Array.isArray(markers) ? markers : [];
    const container = document.getElementById('markersList');
    const countEl = document.getElementById('findings-count');
    renderMarkerTicks(markers);
    updateEmptyStateUi(markers.length);
    updateExportGate(markers);

    if (markers.length === 0) {
        container.innerHTML = `<div class="empty-state">${escapeHtml(t('analyze.findings_empty_1'))}<br>${escapeHtml(t('analyze.findings_empty_2'))}</div>`;
        countEl.textContent = '0';
        return;
    }

    countEl.textContent = markers.length.toString();

    container.innerHTML = markers.map(m => {
        const transcript = escapeHtml(m.transcript) || `<em>${escapeHtml(t('analyze.no_transcript'))}</em>`;
        const notesDisplay = m.notes && m.notes.trim()
            ? `<div class="marker-notes-display">${escapeHtml(m.notes)}</div>`
            : '';
        const statusLabel =
            m.status === 'analyzing' ? `<span class="spinner"></span> ${escapeHtml(t('analyze.kafelek_status_analyzing'))}` :
            m.status === 'completed' ? escapeHtml(t('analyze.kafelek_status_analyzed')) :
            m.status === 'error' ? escapeHtml(t('analyze.kafelek_status_error')) : escapeHtml(t('analyze.kafelek_status_pending'));
        // frame_url arrives pre-signed by the server (?st=<per-marker HMAC>) so
        // the <img> request authenticates without the session-token header;
        // the modal reuses the same signed URL.
        const thumb = m.frame_url
            ? `<img class="marker-thumb" src="${escapeHtml(m.frame_url)}" alt="${escapeHtml(t('analyze.modal_alt'))} ${formatTime(m.timestamp)}" loading="lazy" data-action="open-frame" data-frame-url="${escapeHtml(m.frame_url)}">`
            : '';
        const categoryLabel = m.result ? formatMarkerCategory(m.result.category) : '';
        const severityLabel = m.result ? formatMarkerSeverity(m.result.severity) : '';
        const severitySuffix = severityLabel ? ` (${escapeHtml(severityLabel)})` : '';
        const result = m.result
            ? `<div class="marker-result"><strong>${escapeHtml(categoryLabel)}</strong>${severitySuffix}<br>${escapeHtml(m.result.summary)}</div>`
            : '';

        // Per-finding actions:
        // - Pending: Analyze, Edit note, Delete
        // - Analyzed/Error: Re-analyze, Edit note, Delete
        // The primary action (Analyze vs Re-analyze) leads each kafelek's
        // toolbar so the most likely next click is leftmost.
        const markerIdAttr = escapeHtml(m.marker_id);
        const primaryBtn = (m.status === 'pending' || m.status === 'error')
            ? `<button data-action="analyze" data-marker-id="${markerIdAttr}">${escapeHtml(t('analyze.action_analyze'))}</button>`
            : (m.status === 'completed'
                ? `<button data-action="reanalyze" data-marker-id="${markerIdAttr}">${escapeHtml(t('analyze.action_reanalyze'))}</button>`
                : '');

        // Per-marker priority control (A7b). Mirrors Review's severity-select:
        // the operator can SET a marker's priority, persisted server-side via
        // PATCH /api/marker/{id}. The effective severity is the manual override
        // (m.severity) if present, otherwise the VLM-assigned result severity.
        // Lives inside a data-action="stop" region so changing it never
        // selects/seeks the underlying marker kafelek.
        const effectiveSeverity = m.severity || (m.result ? m.result.severity : '') || '';
        // The clear option carries value "none" (not ""): the delegated change
        // handler guards on `severity` truthiness, and the backend treats "none"
        // as an explicit override that clears the priority. An empty string is
        // falsy and would be silently dropped, wedging a set override forever.
        const severityOptions = [
            ['none', 'analyze.severity_no_change'],
            ['critical', 'analyze.severity_critical'],
            ['high', 'analyze.severity_high'],
            ['medium', 'analyze.severity_medium'],
            ['low', 'analyze.severity_low'],
        ].map(([value, key]) => {
            const selected = value === effectiveSeverity ? ' selected' : '';
            return `<option value="${escapeHtml(value)}"${selected}>${escapeHtml(t(key))}</option>`;
        }).join('');

        const actions = `
            <div class="marker-actions" data-action="stop">
                ${primaryBtn}
                <button data-action="edit-note" data-marker-id="${markerIdAttr}">${escapeHtml(t('analyze.action_edit_note'))}</button>
                <button class="danger" data-action="delete" data-marker-id="${markerIdAttr}">${escapeHtml(t('analyze.action_delete'))}</button>
            </div>
            <div class="marker-priority" data-action="stop">
                <label class="marker-priority-label" for="severity-select-${markerIdAttr}">${escapeHtml(t('analyze.action_change_priority'))}</label>
                <select class="severity-select" id="severity-select-${markerIdAttr}" data-marker-id="${markerIdAttr}">
                    ${severityOptions}
                </select>
            </div>
            <div class="marker-note-editor" id="note-editor-${markerIdAttr}" data-action="stop">
                <textarea id="note-input-${markerIdAttr}">${escapeHtml(m.notes || '')}</textarea>
                <div class="editor-buttons">
                    <button class="secondary" data-action="cancel-note" data-marker-id="${markerIdAttr}">${escapeHtml(t('analyze.action_cancel'))}</button>
                    <button class="primary" data-action="save-note" data-marker-id="${markerIdAttr}">${escapeHtml(t('analyze.action_save'))}</button>
                </div>
            </div>
        `;

        return `
            <div class="marker-item ${m.status}${m.marker_id === activeMarkerId ? ' active' : ''}"
                 data-marker-id="${markerIdAttr}"
                 data-marker-timestamp="${Number(m.timestamp) || 0}"
                 role="option"
                 tabindex="0"
                 aria-selected="${m.marker_id === activeMarkerId ? 'true' : 'false'}"
                 data-action="select">
                <div class="marker-header">
                    <span class="marker-time">${formatTime(m.timestamp)}</span>
                    <span class="marker-status">${statusLabel}</span>
                </div>
                <div class="marker-transcript">${transcript}</div>
                ${notesDisplay}
                ${thumb}
                ${result}
                ${actions}
            </div>
        `;
    }).join('');
}

// Event delegation for the markers list. Marker ids and frame URLs are
// user-influenced (signed query strings, server-generated ids) so they must
// never be interpolated raw into inline onclick="" handlers — a quote or a
// special character there breaks the markup or injects script. Instead the
// kafelek markup carries data-action / data-marker-id / data-frame-url
// attributes (HTML-escaped at render time) and a single delegated listener on
// the #markersList container dispatches to the right handler. Attached once.
let markersListDelegated = false;
function wireMarkersListDelegation() {
    const container = document.getElementById('markersList');
    if (!container || markersListDelegated) return;
    markersListDelegated = true;

    container.addEventListener('click', (event) => {
        const target = event.target instanceof Element
            ? event.target.closest('[data-action]')
            : null;
        if (!target || !container.contains(target)) return;
        const action = target.dataset.action;

        // Inner controls (action toolbar, note editor) stop the click from
        // bubbling up to the kafelek's "select" handler.
        if (action === 'stop') {
            event.stopPropagation();
            return;
        }

        if (action === 'open-frame') {
            event.stopPropagation();
            openFrameModal(target.dataset.frameUrl || '');
            return;
        }

        const markerId = target.dataset.markerId;
        switch (action) {
            case 'analyze':
                event.stopPropagation();
                analyzeMarker(markerId);
                return;
            case 'reanalyze':
                event.stopPropagation();
                reanalyzeMarker(markerId);
                return;
            case 'edit-note':
                event.stopPropagation();
                toggleNoteEditor(markerId);
                return;
            case 'delete':
                event.stopPropagation();
                deleteMarker(markerId);
                return;
            case 'cancel-note':
                event.stopPropagation();
                cancelEditNote(markerId);
                return;
            case 'save-note':
                event.stopPropagation();
                saveNote(markerId);
                return;
            case 'select': {
                const timestamp = Number(target.dataset.markerTimestamp) || 0;
                selectMarker(markerId, timestamp);
                return;
            }
            default:
                return;
        }
    });

    // The per-marker priority <select> changes via a delegated 'change' event
    // (click/keydown are already guarded by the data-action="stop" boundary).
    container.addEventListener('change', (event) => {
        const target = event.target;
        if (!(target instanceof Element) || !target.matches('.severity-select')) return;
        event.stopPropagation();
        const markerId = target.dataset.markerId;
        const severity = target.value;
        if (markerId && severity) {
            changeMarkerSeverity(markerId, severity);
        }
    });

    container.addEventListener('keydown', (event) => {
        // Keys typed into the note editor, or pressed on the priority select —
        // any nested form control or [data-action="stop"] region — must not
        // bubble up into marker selection. The editor/select opt out via
        // data-action="stop" (the same boundary the click delegation honors), so
        // an event originating there is not a marker-select gesture. Without this
        // guard, Space in the note <textarea> climbs past the editor to the outer
        // .marker-item, hits handleMarkerKeydown's preventDefault, and is
        // swallowed ("Gotowe do analizy" -> "Gotowedoanalizy"). [A6 + A7b select]
        if (
            event.target instanceof Element
            && event.target.closest('[data-action="stop"], input, textarea, select, [contenteditable="true"]')
        ) {
            return;
        }
        const target = event.target instanceof Element
            ? event.target.closest('.marker-item[data-action="select"]')
            : null;
        if (!target || !container.contains(target)) return;
        const markerId = target.dataset.markerId;
        const timestamp = Number(target.dataset.markerTimestamp) || 0;
        handleMarkerKeydown(event, markerId, timestamp);
    });
}

// Drive the body-level empty-state flag from the single place that always knows
// the current marker count. CSS keys off body[data-has-markers] to compact the
// transcript panel and show/hide the "How it works" helper — no per-element
// toggling needed. Idempotent: safe to call on every refresh.
function updateEmptyStateUi(count) {
    document.body.setAttribute('data-has-markers', Number(count) > 0 ? 'true' : 'false');
}

// Export gating: keep Download JSON / Download report disabled until at least
// one moment exists, so a session can't be exported empty. Driven from the same
// markers payload updateMarkersList renders, so the gate and the list never disagree.
function updateExportGate(markers) {
    const hasMarkers = Array.isArray(markers) && markers.length > 0;
    const jsonBtn = document.getElementById('exportJsonBtn');
    const mdBtn = document.getElementById('finalizeBtn');
    const hint = document.getElementById('exportGateHint');
    [jsonBtn, mdBtn].forEach((btn) => {
        if (!btn) return;
        btn.disabled = !hasMarkers;
    });
    if (hint) hint.hidden = hasMarkers;
}

// Fetch and refresh markers
async function refreshMarkers() {
    try {
        const response = await fetch('/api/markers');
        if (!response.ok) {
            updateMarkersList([]);
            return;
        }
        const markers = await response.json();
        updateMarkersList(markers);
    } catch (error) {
        console.warn('Failed to refresh markers', error);
        updateMarkersList([]);
    }
}

// Analyze a specific marker. Sends the currently-selected UI language in
// the JSON body so the VLM produces a finding in the matching language.
// Backend defaults to config.language when the body is missing or invalid.
async function analyzeMarker(markerId) {
    const statusEl = document.getElementById('statusText');
    if (statusEl) statusEl.textContent = t('analyze.status_analyzing');

    try {
        const response = await fetch('/api/analyze/' + markerId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: langBody(),
        });
        if (!response.ok) {
            throw new Error('Analyze failed: ' + response.status);
        }
        // The endpoint returns HTTP 200 even when the VLM run itself failed
        // (payload.status === 'error'); a blind "Ready" would hide that. Mirror
        // the manual-frame analyze path: only "completed" is success, anything
        // else surfaces as an error like deleteMarker/saveNote do.
        const payload = await response.json().catch(() => null);
        if (payload && payload.status && payload.status !== 'completed') {
            throw new Error(payload.error || ('Analyze status: ' + payload.status));
        }
        await refreshMarkers();
        if (statusEl) statusEl.textContent = t('analyze.status_ready');
    } catch (err) {
        console.error('Analyze failed:', err);
        if (statusEl) statusEl.textContent = t('analyze.status_analyze_failed');
    }
}

// Re-analyze an already-analyzed marker. Backend overwrites the previous
// session.results entry, so no duplicate; status briefly becomes "analyzing"
// and then "completed" again with fresh content.
async function reanalyzeMarker(markerId) {
    if (!confirm(t('analyze.confirm_reanalyze'))) {
        return;
    }
    await analyzeMarker(markerId);
}

// Delete a marker (and its result + persisted frame). User confirms first.
async function deleteMarker(markerId) {
    if (!confirm(t('analyze.confirm_delete'))) {
        return;
    }
    document.getElementById('statusText').textContent = t('analyze.status_deleting');
    try {
        const response = await fetch('/api/marker/' + markerId, { method: 'DELETE' });
        if (!response.ok) {
            throw new Error('Delete failed: ' + response.status);
        }
        await refreshMarkers();
        document.getElementById('statusText').textContent = t('analyze.status_ready');
    } catch (err) {
        console.error('Delete failed:', err);
        document.getElementById('statusText').textContent = t('analyze.status_delete_failed');
    }
}

// Inline note editor toggle: shows/hides the textarea + Save/Cancel buttons.
function toggleNoteEditor(markerId) {
    const editor = document.getElementById('note-editor-' + markerId);
    if (!editor) return;
    editor.classList.toggle('active');
    if (editor.classList.contains('active')) {
        const input = document.getElementById('note-input-' + markerId);
        if (input) input.focus();
    }
}

function cancelEditNote(markerId) {
    const editor = document.getElementById('note-editor-' + markerId);
    if (editor) editor.classList.remove('active');
    // The textarea value is regenerated on next refreshMarkers() call from
    // the server-side notes field, so no manual reset needed here.
}

// Persist edited note via PATCH and refresh the kafelek so the new note is
// visible in the marker-notes-display block.
async function saveNote(markerId) {
    const input = document.getElementById('note-input-' + markerId);
    if (!input) return;
    const notes = input.value;
    document.getElementById('statusText').textContent = t('analyze.status_saving_note');
    try {
        const response = await fetch('/api/marker/' + markerId, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notes })
        });
        if (!response.ok) {
            throw new Error('Save failed: ' + response.status);
        }
        await refreshMarkers();
        document.getElementById('statusText').textContent = t('analyze.status_ready');
    } catch (err) {
        console.error('Save note failed:', err);
        document.getElementById('statusText').textContent = t('analyze.status_save_failed');
    }
}

// Persist a manual priority override for a marker (A7b) and refresh so the
// badge + select reflect the new value. The change is delegated from the
// per-marker priority <select>; the server mirrors it onto any existing
// analysis result (PATCH /api/marker/{id}). Reuses the note-save failure
// status string rather than minting a parallel one.
async function changeMarkerSeverity(markerId, severity) {
    const statusEl = document.getElementById('statusText');
    try {
        const response = await fetch('/api/marker/' + markerId, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ severity }),
        });
        if (!response.ok) {
            throw new Error('Priority change failed: ' + response.status);
        }
        await refreshMarkers();
        if (statusEl) statusEl.textContent = t('analyze.status_ready');
    } catch (err) {
        console.error('Priority change failed:', err);
        if (statusEl) statusEl.textContent = t('analyze.status_save_failed');
    }
}

// Export findings as JSON
async function exportFindings() {
    const btn = document.getElementById('exportJsonBtn');
    if (btn && btn.disabled) return;  // gated: no moments yet
    const statusEl = document.getElementById('statusText');
    const response = await fetch('/api/export');
    if (!response.ok) {
        if (statusEl) statusEl.textContent = t('analyze.status_export_failed');
        return;
    }
    const data = await response.json();
    downloadJson(data, 'analyze_findings.json');
}

function downloadJson(data, filename) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

function updateFinalizeProgress(state) {
    const wrap = document.getElementById('finalizeProgress');
    const fill = document.getElementById('finalizeProgressFill');
    const label = document.getElementById('finalizeProgressLabel');
    const errors = document.getElementById('finalizeProgressErrors');

    if (!wrap || !fill || !label || !errors) return;

    wrap.hidden = false;
    const total = Number(state.total || 0);
    const processed = Number(state.processed || 0);
    const ratio = total > 0 ? (processed / total) : (state.status === 'completed' ? 1 : 0);

    fill.style.width = `${Math.max(0, Math.min(100, ratio * 100))}%`;
    label.textContent = `${processed}/${total}`;
    errors.textContent = t('analyze.errors_count', { n: Number(state.errors || 0) });
}

function hideFinalizeProgress() {
    const wrap = document.getElementById('finalizeProgress');
    if (wrap) wrap.hidden = true;
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Trigger a browser download of an arbitrary blob (used by the MD report
// path - JSON has its own helper above because it stringifies first).
function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

// Generate a Markdown report. First ensures every pending marker has been
// analyzed (via the existing finalize background job), then fetches the MD
// from /api/report/markdown and triggers a browser download. Replaces the
// previous JSON-only "finalize" path.
async function generateMarkdownReport() {
    const statusEl = document.getElementById('statusText');
    const finalizeBtn = document.getElementById('finalizeBtn');
    if (finalizeBtn && finalizeBtn.disabled) return;  // gated: no moments yet
    const setStatus = (text) => { if (statusEl) statusEl.textContent = text; };
    if (finalizeBtn) finalizeBtn.disabled = true;
    hideFinalizeProgress();

    setStatus(t('analyze.status_finalizing'));
    try {
        // Pass the active UI language so the batch analysis the finalize
        // job triggers produces findings in the chosen language. Same
        // header/body convention as analyzeMarker.
        const startResponse = await fetch('/api/finalize/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: langBody(),
        });
        if (!startResponse.ok) {
            throw new Error('Finalize start failed: ' + startResponse.status);
        }

        let state = await startResponse.json();
        const jobId = state.job_id;
        if (!jobId) {
            throw new Error('Finalize job id missing');
        }

        updateFinalizeProgress(state);

        while (state.status === 'running') {
            await sleep(250);
            const statusResponse = await fetch('/api/finalize/status/' + jobId);
            if (!statusResponse.ok) {
                throw new Error('Finalize status failed: ' + statusResponse.status);
            }
            state = await statusResponse.json();
            updateFinalizeProgress(state);
            setStatus(t('analyze.status_finalizing_progress', {
                processed: state.processed || 0,
                total: state.total || 0,
            }));
        }

        if (state.status !== 'completed') {
            throw new Error(state.last_error || 'Finalize failed');
        }

        // Refresh the list so the kafelek statuses match what the report
        // will reflect (completed/error counts).
        await refreshMarkers();

        setStatus(t('analyze.status_building_md'));
        const reportResponse = await fetch('/api/report/markdown');
        if (!reportResponse.ok) {
            throw new Error('Report build failed: ' + reportResponse.status);
        }
        const blob = await reportResponse.blob();
        downloadBlob(blob, 'analyze_report.md');

        const summary = state || {};
        setStatus(t('analyze.status_report_ready', {
            completed: summary.completed || 0,
            errors: summary.errors || 0,
        }));
    } catch (error) {
        console.error(error);
        setStatus(t('analyze.status_report_failed'));
    } finally {
        // Re-derive the export gate from the current marker truth instead of
        // unconditionally re-enabling. If markers were emptied during finalize,
        // a bare `disabled = false` would re-open export on an empty session,
        // bypassing updateExportGate (the only mutator that keeps Download
        // JSON / report + the hint consistent). C5.4.
        updateExportGate(currentMarkers);
    }
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    const video = document.getElementById('videoPlayer');
    const micBtn = document.getElementById('micBtn');
    const recordingStatus = document.getElementById('recordingStatus');
    const transcriptPreview = document.getElementById('transcriptPreview');
    const notesInput = document.getElementById('notesInput');
    const markFrameBtn = document.getElementById('markFrameBtn');

    const recorder = new VoiceRecorder((text) => {
        transcriptPreview.textContent = text;
        transcriptPreview.classList.add('has-text');
        transcriptPreview.hidden = false;
    });

    const frameMarker = new FrameMarker(video);
    wireMarkersListDelegation();

    // Export panel buttons (CSP-ready: replaces inline onclick). The ids are
    // stable and updateExportGate toggles `disabled`; a disabled button never
    // fires click, so the export-gate behavior is preserved unchanged (C7.2).
    document.getElementById('exportJsonBtn')?.addEventListener('click', exportFindings);
    document.getElementById('finalizeBtn')?.addEventListener('click', generateMarkdownReport);

    window.addEventListener('beforeunload', () => recorder.destroy());
    if (video) {
        // One loadedmetadata listener: re-render ticks (duration known) and set
        // the initial status. Reactive status line otherwise: idle until first
        // play, live time while playing, a "Paused at …" cue on pause/seek.
        video.addEventListener('loadedmetadata', () => {
            renderMarkerTicks(currentMarkers);
            setVideoStatus(video.paused ? 'idle' : 'playing', video);
        });
        video.addEventListener('play', () => setVideoStatus('playing', video));
        video.addEventListener('playing', () => setVideoStatus('playing', video));
        video.addEventListener('timeupdate', () => {
            if (!video.paused) setVideoStatus('playing', video);
        });
        video.addEventListener('pause', () => setVideoStatus('paused', video));
        video.addEventListener('seeked', () => {
            setVideoStatus(video.paused ? 'paused' : 'playing', video);
        });
    }

    // Mic button - hold to record
    let micPressActive = false;

    const beginRecording = async (event) => {
        if (event) event.preventDefault();
        if (micPressActive || recorder.isRecording || recorder.isStarting || recorder.isTranscribing) {
            return;
        }
        micPressActive = true;
        const started = await recorder.start();
        if (started) {
            if (!micPressActive) {
                recorder.stop();
                return;
            }
            micBtn.classList.add('recording');
            recordingStatus.classList.add('active');
            document.getElementById('statusText').textContent = t('analyze.recording');
        }
    };

    const finishRecording = () => {
        micPressActive = false;
        if (recorder.isRecording) {
            recorder.stop();
        }
        micBtn.classList.remove('recording');
        recordingStatus.classList.remove('active');
    };

    micBtn.addEventListener('mousedown', beginRecording);
    window.addEventListener('mouseup', finishRecording);
    micBtn.addEventListener('touchstart', beginRecording, { passive: false });
    window.addEventListener('touchend', finishRecording);
    window.addEventListener('touchcancel', finishRecording);

    // Mark frame button
    markFrameBtn.addEventListener('click', async () => {
        const transcript = transcriptPreview.classList.contains('has-text')
            ? transcriptPreview.textContent
            : '';
        const notes = notesInput.value;
        const hasMarkerNote = Boolean(String(transcript || '').trim() || String(notes || '').trim());

        try {
            await frameMarker.markCurrentFrame(transcript, notes);
        } catch (error) {
            // markCurrentFrame already logged the cause. Surface a non-crashing
            // failure status and keep the user's inputs intact so they can retry.
            console.error('Mark frame failed:', error);
            showTemporaryStatus(t('analyze.status_report_failed'));
            return;
        }

        // Clear inputs - placeholder text follows the active language so a
        // toggle made mid-session doesn't leave a stale EN string visible
        // after the next Mark Frame click.
        transcriptPreview.textContent = '';
        transcriptPreview.classList.remove('has-text');
        transcriptPreview.hidden = true;
        notesInput.value = '';

        // Refresh list
        await refreshMarkers();
        showTemporaryStatus(t(hasMarkerNote ? 'analyze.status_frame_marked' : 'analyze.status_frame_marked_without_note'));
        pulseFindingsTab();
    });

    // Tab switching — header tabs drive sidebar tab-content visibility.
    // Tabs implement the ARIA tablist pattern: aria-selected tracks the active
    // tab and ArrowLeft/ArrowRight roving moves focus + activates the sibling.
    const tabButtons = Array.from(document.querySelectorAll('.tab-btn'));
    const activateDashboardTab = (btn) => {
        const target = btn.dataset.tab;
        tabButtons.forEach((b) => {
            const isActive = b === btn;
            b.classList.toggle('active', isActive);
            b.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
        document.querySelectorAll('.tab-content').forEach((pane) => pane.classList.remove('active'));
        const pane = document.getElementById('tab-' + target);
        if (pane) pane.classList.add('active');
    };
    tabButtons.forEach((btn) => {
        btn.addEventListener('click', () => activateDashboardTab(btn));
    });
    window.ScreenScribeLib?.initTabKeyboard(tabButtons, activateDashboardTab);

    // Splitter — drag to resize the right rail. Mirrors review_app.js's
    // initSidebarResize so the analyze dashboard behaves the same as the
    // review report. Width persists in localStorage across reloads. Bounds
    // come from --sidebar-min / --sidebar-max CSS variables (with a 360px
    // viewport-derived ceiling so the left column always has room).
    (function initSplitter() {
        const resizer = document.getElementById('sidebarResizer');
        const sidebar = document.querySelector('.sidebar');
        if (!resizer || !sidebar) return;

        const storageKey = 'screenscribe_sidebar_width';
        const isMobile = () => window.matchMedia('(max-width: 900px)').matches;

        const getBounds = () => {
            const styles = getComputedStyle(document.documentElement);
            const minPx = parseFloat(styles.getPropertyValue('--sidebar-min')) || 320;
            const preferredMax = parseFloat(styles.getPropertyValue('--sidebar-max')) || 720;
            const viewportMax = Math.max(minPx, window.innerWidth - 360);
            return { minPx, maxPx: Math.max(minPx, Math.min(preferredMax, viewportMax)) };
        };

        const applyWidth = (width, persist = true) => {
            if (isMobile()) return;
            const { minPx, maxPx } = getBounds();
            const nextWidth = Math.min(maxPx, Math.max(minPx, width));
            document.documentElement.style.setProperty('--sidebar-width', `${nextWidth}px`);
            resizer.setAttribute('aria-valuenow', String(Math.round(nextWidth)));
            if (persist) {
                try { localStorage.setItem(storageKey, String(Math.round(nextWidth))); }
                catch (_err) { /* localStorage unavailable; non-fatal */ }
            }
        };

        try {
            const saved = Number(localStorage.getItem(storageKey));
            if (saved) applyWidth(saved, false);
        } catch (_err) { /* localStorage unavailable; non-fatal */ }

        // Keyboard resize: focusable separator, Arrow keys nudge, Home/End jump
        // to the min/max bound. Mirrors review_app.js so both surfaces match.
        const KEY_STEP = 24;
        resizer.tabIndex = 0;
        resizer.setAttribute('aria-valuemin', '0');
        resizer.setAttribute('aria-valuemax', '100');
        resizer.addEventListener('keydown', (event) => {
            if (isMobile()) return;
            const { minPx, maxPx } = getBounds();
            const current = sidebar.getBoundingClientRect().width;
            let next = null;
            if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') next = current + KEY_STEP;
            else if (event.key === 'ArrowRight' || event.key === 'ArrowUp') next = current - KEY_STEP;
            else if (event.key === 'Home') next = maxPx;
            else if (event.key === 'End') next = minPx;
            if (next === null) return;
            event.preventDefault();
            applyWidth(next);
        });

        let dragState = null;

        resizer.addEventListener('pointerdown', (event) => {
            if (isMobile()) return;
            dragState = { startX: event.clientX, startWidth: sidebar.getBoundingClientRect().width };
            document.body.classList.add('is-resizing');
            if (resizer.setPointerCapture) resizer.setPointerCapture(event.pointerId);
            event.preventDefault();
        });

        window.addEventListener('pointermove', (event) => {
            if (!dragState) return;
            const delta = dragState.startX - event.clientX;
            applyWidth(dragState.startWidth + delta);
        });

        const stopResize = (event) => {
            if (!dragState) return;
            dragState = null;
            document.body.classList.remove('is-resizing');
            if (event && resizer.releasePointerCapture && resizer.hasPointerCapture?.(event.pointerId)) {
                resizer.releasePointerCapture(event.pointerId);
            }
        };

        window.addEventListener('pointerup', stopResize);
        window.addEventListener('pointercancel', stopResize);
        window.addEventListener('resize', () => {
            if (isMobile()) {
                document.body.classList.remove('is-resizing');
                return;
            }
            applyWidth(sidebar.getBoundingClientRect().width, false);
        });
    })();

    // Frame preview modal - close on overlay click, visible X, or Esc keypress.
    const frameModal = document.getElementById('frameModal');
    if (frameModal) {
        frameModal.addEventListener('click', (event) => {
            // Close on overlay click OR on the visible X. The X is wired here by
            // delegation (CSP-ready) — it replaces frame_modal.html's former
            // inline onclick="event.stopPropagation(); closeFrameModal()" (C7.2b).
            // A click on the <img> matches neither branch, so it does not close.
            if (event.target === frameModal || event.target.closest('.frame-modal-close')) {
                closeFrameModal();
            }
        });
    }
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && frameModal && frameModal.classList.contains('active')) {
            closeFrameModal();
        }
    });

    // Language toggle - clicking either pill flips currentLang, persists
    // to localStorage and re-applies translations across the dashboard.
    window.ScreenScribeLib?.wireLanguageToggle(
        document.getElementById('langToggle'),
        { order: ['en', 'pl'] },
        setLang
    );

    // Apply translations on first paint based on the resolved initial
    // language (localStorage > config.language > "en"). Skip the marker
    // re-render here — the explicit refreshMarkers() below will do it
    // once the markers payload is fetched.
    applyTranslations(document);
    refreshLangToggleUI();

    // Initial load
    refreshMarkers();
});
