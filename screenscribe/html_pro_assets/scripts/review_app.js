// Localhost session token: the server hands it to us via the URL fragment
// (#token=...). Echo it back as X-ScreenScribe-Token on same-origin requests and
// strip it from the address bar. A cross-site page can't read our fragment.
(function () {
    if (typeof window === 'undefined' || typeof location === 'undefined') { return; }
    // Survive a reload: the token is also cached in sessionStorage (per-tab,
    // origin-isolated, cleared on tab close) keyed by origin+path. Stripping it
    // from the address bar must not strip it from the running session, or every
    // /api/* call (save, STT, manual-analyze, delete, review-state) 403s after a
    // refresh. A fresh #token always overwrites the cached value.
    var storageKey = 'screenscribe:token:' + location.origin + location.pathname;
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
})();

// Idempotent global: both review_app.js and video_player.js load as inline
// <script> tags in report.html. Using `const` collided; `var` + typeof guard
// lets whichever script loads first declare the flag without the second one
// throwing a SyntaxError and killing the rest of the bundle.
var DEBUG;
if (typeof DEBUG === 'undefined') { DEBUG = typeof window !== 'undefined' && window.location?.search?.includes('debug=1'); }

// "Momenty (N)" tab counter must sum AI findings + manual moments (dispatcher
// decision). The span's server-rendered initial value is the AI-only count;
// cache it once on first read so later updates (which mutate the same span)
// don't re-read our own running total.
let aiFindingsCount = null;

const reportState = {
    findings: {},
    manualFrames: [],
    // Human-merge groups: each is { id, member_ids: [...], summary_override }.
    // `id` is the surviving (earliest-timestamp) finding id; the union of every
    // member is recomputed from the original findings at export/build time so the
    // editable summary is the only mutable state we persist here.
    merges: [],
    reviewer: '',
    modified: false,
    reportId: ''
};

const WINDOW_MODES = {
    workspace: 'workspace',
    player: 'player',
    review: 'review',
};

const stateSyncRuntime = {
    draftKey: '',
    syncKey: '',
    commandKey: '',
    uiKey: '',
    langKey: '',
    sourceId: Math.random().toString(16).slice(2),
    syncTimer: null,
    draftSaveTimer: null,
    suppressEvents: false,
    // BH37: epoch-ms freshness of the state THIS tab last persisted. A cross-tab
    // `storage` snapshot may only overwrite our in-memory state when it is
    // strictly newer than this (last-writer-wins by savedAt); otherwise a stale
    // snapshot from another tab would clobber a fresher local edit. null = this
    // tab has not persisted yet, so any incoming snapshot is accepted.
    lastLocalSavedAt: null,
};

const detachedWindowRuntime = {
    childWindow: null,
    closePoll: null,
    mode: WINDOW_MODES.workspace,
};

const uiState = {
    activeTab: 'summary',
};

window.__screenscribeAllowProgrammaticClose = false;

function getInitialReportLanguage() {
    const rawLanguage = document.body?.dataset?.reportLanguage || document.documentElement.lang || 'pl';
    const normalizedLanguage = rawLanguage.toLowerCase().replace('_', '-');
    const primaryLanguage = normalizedLanguage.split('-', 1)[0];
    if (hasI18nLanguage(normalizedLanguage)) {
        return normalizedLanguage;
    }
    if (hasI18nLanguage(primaryLanguage)) {
        return primaryLanguage;
    }
    return 'pl';
}

function createDefaultFindingState() {
    return {
        verdict: 'none',
        severity: null,
        notes: ''
    };
}

// One decision vocabulary everywhere: accepted | rejected | none. `none` is the
// explicit "not reviewed" string, never absence. This also migrates an old
// localStorage draft that still carried the boolean `confirmed`.
function normalizeVerdict(value) {
    if (value === 'accepted' || value === 'rejected' || value === 'none') {
        return value;
    }
    // legacy migration: confirmed -> verdict
    if (value === true) return 'accepted';
    if (value === false) return 'rejected';
    return 'none';
}

function migrateFindingStates(findings) {
    if (!findings || typeof findings !== 'object') {
        return {};
    }
    for (const state of Object.values(findings)) {
        if (!state || typeof state !== 'object') {
            continue;
        }
        if ('verdict' in state) {
            state.verdict = normalizeVerdict(state.verdict);
        } else {
            // legacy migration: confirmed -> verdict
            state.verdict = normalizeVerdict(state.confirmed);
        }
        if ('confirmed' in state) {
            delete state.confirmed;
        }
    }
    return findings;
}

function getDefaultTabForWindowMode(mode = detachedWindowRuntime.mode) {
    if (mode === WINDOW_MODES.review && document.querySelector('.finding')) {
        return 'findings';
    }
    return 'summary';
}

function getAvailableTabIds() {
    return Array.from(document.querySelectorAll('.tab-btn[data-tab]'))
        .map((button) => button.dataset.tab)
        .filter(Boolean);
}

function normalizeTabId(tabId) {
    const availableTabIds = getAvailableTabIds();
    if (availableTabIds.includes(tabId)) {
        return tabId;
    }
    return availableTabIds.includes(getDefaultTabForWindowMode())
        ? getDefaultTabForWindowMode()
        : availableTabIds[0] || 'summary';
}

function persistUiState() {
    if (!stateSyncRuntime.uiKey) {
        return;
    }

    try {
        localStorage.setItem(
            stateSyncRuntime.uiKey,
            JSON.stringify({
                sourceId: stateSyncRuntime.sourceId,
                savedAt: new Date().toISOString(),
                tabId: uiState.activeTab,
            })
        );
    } catch (error) {
        console.debug('Could not persist detached window UI state:', error);
    }
}

function persistLanguagePreference(lang) {
    window.ScreenScribeLib?.persistLanguage(
        stateSyncRuntime.langKey,
        lang,
        { mode: 'envelope', sourceId: stateSyncRuntime.sourceId }
    );
}

function allowProgrammaticClose(targetWindow = window) {
    if (!targetWindow) {
        return;
    }

    try {
        targetWindow.__screenscribeAllowProgrammaticClose = true;
        window.setTimeout(() => {
            try {
                if (!targetWindow.closed) {
                    targetWindow.__screenscribeAllowProgrammaticClose = false;
                }
            } catch (_error) {
                // Ignore reset failures when the target window is already gone.
            }
        }, 1500);
    } catch (_error) {
        // Ignore close-intent propagation failures.
    }
}

function activateTab(tabId, { persist = true } = {}) {
    const nextTabId = normalizeTabId(tabId);
    uiState.activeTab = nextTabId;

    document.querySelectorAll('.tab-btn[data-tab]').forEach((button) => {
        const isActive = button.dataset.tab === nextTabId;
        button.classList.toggle('active', isActive);
        button.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    document.querySelectorAll('.tab-content').forEach((content) => {
        content.classList.toggle('active', content.id === `tab-${nextTabId}`);
    });

    if (persist) {
        persistUiState();
    }
}

function getWindowModeFromLocation() {
    const mode = new URLSearchParams(window.location.search).get('window');
    return Object.values(WINDOW_MODES).includes(mode) ? mode : WINDOW_MODES.workspace;
}

function buildWindowModeUrl(mode) {
    const url = new URL(window.location.href);
    if (mode === WINDOW_MODES.workspace) {
        url.searchParams.delete('window');
    } else {
        url.searchParams.set('window', mode);
    }
    return url.toString();
}

function updateWindowActionButtons() {
    const detachBtn = document.getElementById('detachReviewBtn');
    const attachBtn = document.getElementById('attachWorkspaceBtn');
    if (detachBtn) {
        const hasDetachedReview = detachedWindowRuntime.mode === WINDOW_MODES.player
            || Boolean(
                detachedWindowRuntime.childWindow && !detachedWindowRuntime.childWindow.closed
            );
        const detachLabelKey = hasDetachedReview ? 'focusReview' : 'detachReview';
        detachBtn.dataset.i18n = detachLabelKey;
        detachBtn.textContent = t('review.' + detachLabelKey) || detachBtn.textContent;
        detachBtn.hidden = detachedWindowRuntime.mode === WINDOW_MODES.review;
    }
    if (attachBtn) {
        attachBtn.hidden = detachedWindowRuntime.mode === WINDOW_MODES.workspace;
    }
}

function applyWindowMode(mode, { persistUrl = true } = {}) {
    detachedWindowRuntime.mode = Object.values(WINDOW_MODES).includes(mode)
        ? mode
        : WINDOW_MODES.workspace;
    document.body.dataset.windowMode = detachedWindowRuntime.mode;
    updateWindowActionButtons();
    if (persistUrl) {
        window.history.replaceState(
            { windowMode: detachedWindowRuntime.mode },
            '',
            buildWindowModeUrl(detachedWindowRuntime.mode)
        );
    }
}

function startDetachedWindowWatch() {
    if (detachedWindowRuntime.closePoll || detachedWindowRuntime.mode === WINDOW_MODES.review) {
        return;
    }

    detachedWindowRuntime.closePoll = window.setInterval(() => {
        const child = detachedWindowRuntime.childWindow;
        if (!child) {
            return;
        }
        if (child.closed) {
            detachedWindowRuntime.childWindow = null;
            stopDetachedWindowWatch();
            if (detachedWindowRuntime.mode === WINDOW_MODES.player) {
                applyWindowMode(WINDOW_MODES.workspace);
                showNotification(t('review.separateWindowClosed'));
            }
        }
    }, 800);
}

function stopDetachedWindowWatch() {
    if (detachedWindowRuntime.closePoll) {
        window.clearInterval(detachedWindowRuntime.closePoll);
        detachedWindowRuntime.closePoll = null;
    }
}

// localStorage is a draft/cache layer, never the source of truth: durable
// review state — including the manual-frame pixels — lives on the server
// (/api/review-state). frameDataUrl is deliberately stripped here so a real
// report's draft cannot blow the ~5MB localStorage quota and silently drop the
// reviewer's verdicts. The image is restored from server state on reload.
function buildPersistableState(modified) {
    const manualFrames = reportState.manualFrames.map((frame) => {
        const { frameDataUrl, ...rest } = frame;
        return rest;
    });
    return {
        findings: reportState.findings,
        manualFrames,
        // Human-merge groups are unsaved-session-only state (never written to the
        // report until Save). Persist them in the draft so a reload / cross-window
        // sync / draft-restore before Save does not lose the fold and resurrect
        // the absorbed findings standalone.
        merges: Array.isArray(reportState.merges) ? reportState.merges : [],
        reviewer: reportState.reviewer,
        modified,
    };
}

// Persist a review snapshot, treating quota overflow as a soft failure: runtime
// state and the server still hold the reviewer's work, so we surface a gentle
// warning (when the user explicitly saved) instead of losing the flow.
function persistReviewSnapshot(key, envelope, { warnOnQuota = false } = {}) {
    try {
        localStorage.setItem(key, JSON.stringify(envelope));
        return true;
    } catch (error) {
        if (error && error.name === 'QuotaExceededError') {
            if (warnOnQuota) showNotification(t('review.draftQuotaWarning'));
            else console.debug('Review draft cache full; relying on server state.');
        } else {
            console.warn('Could not persist review draft:', error);
        }
        return false;
    }
}

function persistSharedState() {
    if (!stateSyncRuntime.syncKey || stateSyncRuntime.suppressEvents) {
        return;
    }

    const envelope = {
        sourceId: stateSyncRuntime.sourceId,
        savedAt: new Date().toISOString(),
        state: buildPersistableState(reportState.modified),
    };

    persistReviewSnapshot(stateSyncRuntime.syncKey, envelope);
    // Record how fresh our in-memory state now is, so a stale cross-tab snapshot
    // arriving afterwards can be rejected (BH37).
    rememberLocalSavedAt(envelope.savedAt);
}

// BH37 last-writer-wins helpers. ``rememberLocalSavedAt`` advances the local
// freshness watermark (monotonic: it never moves backwards), and
// ``isIncomingReviewEnvelopeFresher`` decides whether a cross-tab snapshot is
// allowed to overwrite the running state.
function rememberLocalSavedAt(savedAt) {
    const time = Date.parse(savedAt || '');
    if (!Number.isFinite(time)) {
        return;
    }
    const current = stateSyncRuntime.lastLocalSavedAt;
    if (current === null || time > current) {
        stateSyncRuntime.lastLocalSavedAt = time;
    }
}

function isIncomingReviewEnvelopeFresher(envelope) {
    const incoming = savedAtTime(envelope);
    // No usable timestamp on the incoming snapshot (legacy/older writer): it
    // cannot be proven stale, so let it through rather than silently dropping it.
    if (incoming === null) {
        return true;
    }
    const local = stateSyncRuntime.lastLocalSavedAt;
    if (local === null) {
        return true;
    }
    return incoming > local;
}

function scheduleSharedStateSync() {
    if (!stateSyncRuntime.syncKey || stateSyncRuntime.suppressEvents) {
        return;
    }

    if (stateSyncRuntime.syncTimer) {
        window.clearTimeout(stateSyncRuntime.syncTimer);
    }
    stateSyncRuntime.syncTimer = window.setTimeout(() => {
        persistSharedState();
    }, 120);
}

// Manual-frame mutations (add/delete) are durable acts, so flush the shared
// snapshot synchronously instead of riding the 120ms debounce: a reload right
// after the change must not drop the frame from the localStorage draft. The
// debounced path stays for high-frequency edits (typing notes, toggling
// verdicts) where collapsing writes is the point.
function flushSharedStateSync() {
    if (stateSyncRuntime.syncTimer) {
        window.clearTimeout(stateSyncRuntime.syncTimer);
        stateSyncRuntime.syncTimer = null;
    }
    persistSharedState();
}

function broadcastWindowCommand(command) {
    broadcastWindowCommandWithPayload(command, {});
}

function broadcastWindowCommandWithPayload(command, payload = {}) {
    if (!stateSyncRuntime.commandKey) {
        return;
    }

    try {
        localStorage.setItem(
            stateSyncRuntime.commandKey,
            JSON.stringify({
                sourceId: stateSyncRuntime.sourceId,
                command,
                payload,
                issuedAt: new Date().toISOString(),
            })
        );
    } catch (error) {
        console.debug('Could not broadcast detached window command:', error);
    }
}

// The report was rendered as a self-contained static demo (baked by the example
// generator via data-static-demo on <body>): no backing server, no source video.
function isStaticDemo() {
    return document.body?.dataset?.staticDemo === 'true';
}

function canControlEmbeddedPlayer() {
    return Boolean(
        detachedWindowRuntime.mode !== WINDOW_MODES.review
        && window.player
        && typeof window.player.seekTo === 'function'
        && window.player.video instanceof HTMLVideoElement
    );
}

function handleIncomingWindowCommand(envelope) {
    if (!envelope || typeof envelope !== 'object') {
        return;
    }

    if (envelope.command === 'reattach-workspace' && detachedWindowRuntime.mode === WINDOW_MODES.player) {
        detachedWindowRuntime.childWindow = null;
        stopDetachedWindowWatch();
        applyWindowMode(WINDOW_MODES.workspace);
        showNotification(t('review.singleWindowRestored'));
        return;
    }

    if (envelope.command === 'seek-to-timestamp' && canControlEmbeddedPlayer()) {
        const timestamp = Number(envelope.payload?.timestamp);
        if (Number.isFinite(timestamp)) {
            window.player.seekTo(timestamp, envelope.payload?.autoplay !== false);
        }
    }
}

// frameDataUrl is a heavy, intentionally non-persisted field: stripped from the
// localStorage draft and restored from the server. A lightweight snapshot — most
// notably a cross-window `storage` sync — carries the frame list but not the
// image, so a wholesale replace would wipe images the running state already holds
// (broken image, empty ZIP). Preserve the in-memory image by marker_id when the
// incoming frame doesn't bring its own; the incoming snapshot still decides which
// frames exist (a deleted frame is not resurrected), and an incoming image wins.
function mergeManualFrameImages(currentFrames, incomingFrames) {
    const currentImages = new Map();
    (Array.isArray(currentFrames) ? currentFrames : []).forEach((frame) => {
        if (frame && typeof frame === 'object' && frame.marker_id != null && frame.frameDataUrl) {
            currentImages.set(String(frame.marker_id), frame.frameDataUrl);
        }
    });
    return (Array.isArray(incomingFrames) ? incomingFrames : []).map((frame) => {
        if (!frame || typeof frame !== 'object' || frame.frameDataUrl) {
            return frame;
        }
        const preserved = currentImages.get(String(frame.marker_id));
        return preserved ? { ...frame, frameDataUrl: preserved } : frame;
    });
}

function hydrateReportState(snapshot, { showRestoreToast = false } = {}) {
    if (!snapshot || typeof snapshot !== 'object') {
        return;
    }

    stateSyncRuntime.suppressEvents = true;
    reportState.findings = migrateFindingStates(snapshot.findings || {});
    reportState.manualFrames = mergeManualFrameImages(reportState.manualFrames, snapshot.manualFrames || []);
    // Restore the human-merge groups persisted alongside findings so the fold
    // survives a draft round-trip (see buildPersistableState).
    reportState.merges = Array.isArray(snapshot.merges) ? snapshot.merges : [];
    reportState.reviewer = snapshot.reviewer || '';
    reportState.modified = Boolean(snapshot.modified);
    stateSyncRuntime.suppressEvents = false;

    const reviewerInput = document.getElementById('reviewer-name');
    if (reviewerInput) {
        reviewerInput.value = reportState.reviewer;
    }

    restoreUIFromState();
    // The fold lives in data (reportState.merges + merged_from_ids), but the DOM
    // only reflects it when applyMergeToDom runs — which previously fired ONLY on
    // an explicit Merge click. On a draft-restore / detached-window sync that
    // carries an existing merge, replay it into the DOM so absorbed cards hide
    // and the merged summary card appears, keeping the UI in sync with the data.
    restoreMergesToDom();
    renderManualFrames();
    initAnnotationTools();

    if (showRestoreToast) {
        showNotification(t('review.draftRestored'));
    }
}

function enrichManualFrameImagesFromServerState(snapshot) {
    if (!snapshot || !Array.isArray(snapshot.manualFrames) || reportState.manualFrames.length === 0) {
        return false;
    }

    const serverFrameImages = new Map();
    snapshot.manualFrames.forEach((frame) => {
        if (!frame || typeof frame !== 'object' || frame.marker_id === undefined || frame.marker_id === null) {
            return;
        }
        if (frame.frameDataUrl) {
            serverFrameImages.set(String(frame.marker_id), frame.frameDataUrl);
        }
    });

    let enriched = false;
    reportState.manualFrames = reportState.manualFrames.map((frame) => {
        if (!frame || typeof frame !== 'object' || frame.frameDataUrl) {
            return frame;
        }
        const frameDataUrl = serverFrameImages.get(String(frame.marker_id));
        if (!frameDataUrl) {
            return frame;
        }
        enriched = true;
        return { ...frame, frameDataUrl };
    });

    if (enriched) {
        renderManualFrames();
    }
    return enriched;
}

async function hydrateReportStateFromDisk({ enrichManualFrameImagesOnly = false } = {}) {
    // Static-demo sample (GitHub Pages): there is no server, so skip the
    // /api/review-state hydration entirely — no request leaves the page and the
    // console stays clean, honoring the landing page's self-contained promise.
    if (isStaticDemo()) {
        return;
    }
    try {
        const response = await fetch('/api/review-state', {
            headers: { 'Accept': 'application/json' },
        });
        if (!response.ok) {
            return;
        }
        const snapshot = await response.json();
        if (enrichManualFrameImagesOnly) {
            enrichManualFrameImagesFromServerState(snapshot);
            return;
        }
        const hasDiskState = Boolean(
            snapshot?.reviewer
            || (snapshot?.findings && Object.keys(snapshot.findings).length > 0)
            || (Array.isArray(snapshot?.manualFrames) && snapshot.manualFrames.length > 0)
        );
        if (!hasDiskState || reportState.modified) {
            return;
        }
        hydrateReportState(snapshot);
    } catch (error) {
        if (DEBUG) console.debug('No persisted review state available:', error);
    }
}

function parseStoredReviewEnvelope(raw) {
    if (!raw) {
        return null;
    }
    try {
        return JSON.parse(raw);
    } catch (error) {
        console.debug('Failed to parse local review state:', error);
        return null;
    }
}

function savedAtTime(envelope) {
    const time = Date.parse(envelope?.savedAt || '');
    return Number.isFinite(time) ? time : null;
}

function chooseReviewRestoreEnvelope(draftEnvelope, syncEnvelope) {
    if (!draftEnvelope) {
        return { envelope: syncEnvelope, source: 'sync' };
    }
    if (!syncEnvelope) {
        return { envelope: draftEnvelope, source: 'draft' };
    }

    const draftTime = savedAtTime(draftEnvelope);
    const syncTime = savedAtTime(syncEnvelope);
    if (draftTime !== null && syncTime !== null && syncTime > draftTime) {
        return { envelope: syncEnvelope, source: 'sync' };
    }
    return { envelope: draftEnvelope, source: 'draft' };
}

function initSharedStateSync() {
    if (!reportState.reportId) {
        return false;
    }

    stateSyncRuntime.draftKey = 'screenscribe_draft_' + reportState.reportId;
    stateSyncRuntime.syncKey = 'screenscribe_state_' + reportState.reportId;
    stateSyncRuntime.commandKey = 'screenscribe_window_event_' + reportState.reportId;
    stateSyncRuntime.uiKey = 'screenscribe_ui_' + reportState.reportId;
    stateSyncRuntime.langKey = 'screenscribe_lang_' + reportState.reportId;

    let restoredReviewState = false;
    try {
        const storedDraft = localStorage.getItem(stateSyncRuntime.draftKey);
        const storedSync = localStorage.getItem(stateSyncRuntime.syncKey);
        const storedUi = localStorage.getItem(stateSyncRuntime.uiKey);
        const draftEnvelope = parseStoredReviewEnvelope(storedDraft);
        const syncEnvelope = parseStoredReviewEnvelope(storedSync);
        const restoredChoice = chooseReviewRestoreEnvelope(draftEnvelope, syncEnvelope);
        const restoredEnvelope = restoredChoice.envelope;
        const restoredState = restoredEnvelope?.state || restoredEnvelope;
        if (restoredState) {
            hydrateReportState(restoredState, { showRestoreToast: restoredChoice.source === 'draft' });
            // Seed the freshness watermark from the restored envelope so a stale
            // cross-tab snapshot arriving right after load cannot clobber it (BH37).
            rememberLocalSavedAt(restoredEnvelope?.savedAt);
            restoredReviewState = true;
        }
        const restoredUi = storedUi ? JSON.parse(storedUi) : null;
        activateTab(restoredUi?.tabId || getDefaultTabForWindowMode(), { persist: false });
    } catch (error) {
        console.warn('localStorage not available:', error);
        activateTab(getDefaultTabForWindowMode(), { persist: false });
    }

    window.addEventListener('storage', (event) => {
        if (event.key === stateSyncRuntime.syncKey && event.newValue) {
            try {
                const envelope = JSON.parse(event.newValue);
                if (envelope.sourceId === stateSyncRuntime.sourceId || !envelope.state) {
                    return;
                }
                // BH37: last-writer-wins by savedAt. A wholesale hydrate here
                // used to let a stale snapshot from another tab overwrite a
                // fresher local edit (lost reviewer work). Only apply when the
                // incoming snapshot is strictly newer than what we last saved.
                // BH37: last-writer-wins by savedAt. A wholesale hydrate here
                // used to let a stale snapshot from another tab overwrite a
                // fresher local edit (lost reviewer work). Only apply when the
                // incoming snapshot is strictly newer than what we last saved.
                if (!isIncomingReviewEnvelopeFresher(envelope)) {
                    return;
                }
                hydrateReportState(envelope.state);
                // The running state now reflects the incoming snapshot; advance
                // the watermark so an even-older follow-up event cannot re-clobber.
                rememberLocalSavedAt(envelope.savedAt);
            } catch (error) {
                console.debug('Failed to apply shared review state:', error);
            }
            return;
        }

        if (event.key === stateSyncRuntime.commandKey && event.newValue) {
            try {
                const envelope = JSON.parse(event.newValue);
                if (envelope.sourceId === stateSyncRuntime.sourceId) {
                    return;
                }
                handleIncomingWindowCommand(envelope);
            } catch (error) {
                console.debug('Failed to process detached window command:', error);
            }
            return;
        }

        if (event.key === stateSyncRuntime.uiKey && event.newValue) {
            try {
                const envelope = JSON.parse(event.newValue);
                if (envelope.sourceId === stateSyncRuntime.sourceId || !envelope.tabId) {
                    return;
                }
                activateTab(envelope.tabId, { persist: false });
            } catch (error) {
                console.debug('Failed to apply detached window UI state:', error);
            }
            return;
        }

        if (event.key === stateSyncRuntime.langKey && event.newValue) {
            try {
                const envelope = JSON.parse(event.newValue);
                if (envelope.sourceId === stateSyncRuntime.sourceId || !envelope.lang) {
                    return;
                }
                setLanguage(envelope.lang, { persist: false });
            } catch (error) {
                console.debug('Failed to apply shared report language:', error);
            }
        }
    });

    return restoredReviewState;
}

function initReviewState() {
    reportState.reportId = document.body.dataset.reportId || '';
    applyWindowMode(getWindowModeFromLocation(), { persistUrl: false });
    const restoredLocalState = initSharedStateSync();
    void hydrateReportStateFromDisk({ enrichManualFrameImagesOnly: restoredLocalState });

    document.addEventListener('input', handleInputEvent);
    document.addEventListener('change', handleChangeEvent);
    document.addEventListener('click', handleFindingSeekClick);

    document.querySelectorAll('.finding').forEach(article => {
        const findingId = article.dataset.findingId;
        if (!reportState.findings[findingId]) {
            reportState.findings[findingId] = createDefaultFindingState();
        }
    });

    const reviewerInput = document.getElementById('reviewer-name');
    if (reviewerInput) {
        reviewerInput.value = reportState.reviewer;
    }

    // Capture the auto-save interval id so it can be torn down on unload.
    // Leaving it running orphans the timer and, on any in-place re-init, stacks
    // a second 30s saveDraft loop (duplicate writes). Clear any prior one first
    // for idempotency.
    if (stateSyncRuntime.draftSaveTimer) {
        window.clearInterval(stateSyncRuntime.draftSaveTimer);
    }
    stateSyncRuntime.draftSaveTimer = window.setInterval(saveDraft, 30000);

    window.addEventListener('beforeunload', (e) => {
        // Tear down the recurring timers so they don't leak across navigations
        // (auto-save loop + the detached-window close poll).
        if (stateSyncRuntime.draftSaveTimer) {
            window.clearInterval(stateSyncRuntime.draftSaveTimer);
            stateSyncRuntime.draftSaveTimer = null;
        }
        stopDetachedWindowWatch();
        if (
            reportState.modified
            && window.__screenscribeAllowProgrammaticClose !== true
        ) {
            e.preventDefault();
            e.returnValue = t('review.unsavedChangesWarning');
        }
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeLightbox();
            closeManualFrameModal();
        }
    });

    bindThumbnailClicks();
    renderManualFrames();
    initAnnotationTools();
    initMergeUI();
    persistSharedState();

    const lightbox = document.getElementById('lightbox');
    if (lightbox) {
        lightbox.addEventListener('click', (event) => {
            // Close on an explicit close/done control, or on a backdrop click
            // (the dark area outside the content). Clicks inside the content or
            // toolbar fall through and keep the lightbox open — this replaces the
            // former inline onclick="event.stopPropagation()" guards (C7.2).
            if (event.target.closest('[data-action="close-lightbox"]') || event.target === lightbox) {
                closeLightbox();
            }
        });
    }
}

function handleInputEvent(e) {
    const target = e.target;
    const article = target.closest('.finding');

    if (target.id === 'reviewer-name') {
        reportState.reviewer = target.value;
        reportState.modified = true;
        scheduleSharedStateSync();
        return;
    }

    if (!article) return;
    const findingId = article.dataset.findingId;
    if (!reportState.findings[findingId]) {
        reportState.findings[findingId] = createDefaultFindingState();
    }

    if (target.matches('.notes textarea')) {
        reportState.findings[findingId].notes = target.value;
        reportState.modified = true;
        scheduleSharedStateSync();
    }
}

function handleChangeEvent(e) {
    const target = e.target;
    const article = target.closest('.finding');

    if (target.classList && target.classList.contains('merge-select')) {
        updateMergeBar();
        return;
    }

    if (!article) return;
    const findingId = article.dataset.findingId;
    if (!reportState.findings[findingId]) {
        reportState.findings[findingId] = createDefaultFindingState();
    }

    if (target.matches('input[type="radio"]') && target.name.startsWith('verdict-')) {
        const verdict = normalizeVerdict(target.value);
        reportState.findings[findingId].verdict = verdict;
        article.dataset.verdict = verdict;
        reportState.modified = true;
        scheduleSharedStateSync();
        updateReviewMeta();
        flashReviewFeedback(article, verdict);
    }

    if (target.matches('.severity-select')) {
        reportState.findings[findingId].severity = target.value;
        reportState.modified = true;
        scheduleSharedStateSync();
        updateReviewMeta();
    }
}

// Inline, immediate confirmation that an accept/reject click registered.
// Fires synchronously on the click so the operator sees feedback well within
// ~100ms: a toast plus a short row flash keyed off data-review-flash.
function flashReviewFeedback(article, verdict) {
    const accepted = verdict === 'accepted';
    showNotification(accepted ? t('review.findingAccepted') : t('review.findingRejected'));

    article.dataset.reviewFlash = accepted ? 'accepted' : 'rejected';
    // Force a reflow so re-triggering the same state replays the CSS animation.
    void article.offsetWidth;
    window.setTimeout(() => {
        if (article.dataset.reviewFlash) delete article.dataset.reviewFlash;
    }, 1200);
}

let currentLightboxFindingId = null;
let lightboxAnnotationTool = null;
// Element focused before a modal opened, so focus returns there on close.
let lightboxReturnFocus = null;

const FOCUSABLE_SELECTOR = [
    'a[href]',
    'button:not([disabled])',
    'input:not([disabled])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
].join(',');

function getFocusable(container) {
    if (!container) return [];
    return Array.from(container.querySelectorAll(FOCUSABLE_SELECTOR)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement
    );
}

// Keep Tab/Shift+Tab inside an open modal so keyboard users cannot escape the
// dialog into the inert page behind it (WCAG 2.4.3 / 2.1.2). Returns the
// keydown handler so the caller can detach it on close.
function trapFocus(container) {
    const handler = (event) => {
        if (event.key !== 'Tab') return;
        const focusable = getFocusable(container);
        if (focusable.length === 0) {
            event.preventDefault();
            return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement;
        if (event.shiftKey && (active === first || !container.contains(active))) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
        }
    };
    container.addEventListener('keydown', handler);
    return handler;
}

function openLightbox(img) {
    const lightbox = document.getElementById('lightbox');
    const lightboxImg = document.getElementById('lightbox-img');
    const lightboxSvg = document.getElementById('lightbox-svg');
    const lightboxToolbar = document.getElementById('lightbox-toolbar');

    // Get finding ID from parent annotation container
    const container = img.closest('.annotation-container');
    currentLightboxFindingId = container ? container.dataset.findingId : null;

    lightboxReturnFocus = document.activeElement;
    lightboxImg.src = img.dataset.full || img.src;
    lightbox.classList.add('active');
    lightbox.setAttribute('aria-hidden', 'false');
    // Trap focus inside the dialog and move focus to the close control.
    if (lightbox.__focusTrapHandler) {
        lightbox.removeEventListener('keydown', lightbox.__focusTrapHandler);
    }
    lightbox.__focusTrapHandler = trapFocus(lightbox);
    const closeBtn = lightbox.querySelector('.lightbox-close');
    if (closeBtn) closeBtn.focus();

    const setupTool = () => {
        if (lightboxAnnotationTool) {
            lightboxAnnotationTool.destroy();
        }
        lightboxAnnotationTool = new LightboxAnnotationTool(
            lightboxSvg,
            lightboxImg,
            lightboxToolbar,
            currentLightboxFindingId
        );
        lightboxToolbar.style.display = 'flex';
    };

    if (lightboxImg.complete && lightboxImg.naturalWidth > 0) {
        setupTool();
    } else {
        lightboxImg.onload = setupTool;
    }
}

function closeLightbox() {
    const lightbox = document.getElementById('lightbox');
    const lightboxToolbar = document.getElementById('lightbox-toolbar');

    // Save annotations before closing
    if (lightboxAnnotationTool && currentLightboxFindingId) {
        lightboxAnnotationTool.saveAnnotations();

        // Update thumbnail canvas with new annotations
        const thumbnailTool = annotationTools.get(currentLightboxFindingId);
        if (thumbnailTool) {
            thumbnailTool.refreshFromState();
        }
        lightboxAnnotationTool.destroy();
    }

    lightbox.classList.remove('active');
    lightbox.setAttribute('aria-hidden', 'true');
    lightboxToolbar.style.display = 'none';
    lightboxAnnotationTool = null;
    currentLightboxFindingId = null;
    if (lightbox.__focusTrapHandler) {
        lightbox.removeEventListener('keydown', lightbox.__focusTrapHandler);
        lightbox.__focusTrapHandler = null;
    }
    // Restore focus to the thumbnail (or whatever opened the lightbox).
    if (lightboxReturnFocus && typeof lightboxReturnFocus.focus === 'function') {
        lightboxReturnFocus.focus();
    }
    lightboxReturnFocus = null;
}

function saveDraft() {
    if (!reportState.modified) return;
    const envelope = {
        sourceId: stateSyncRuntime.sourceId,
        state: buildPersistableState(false),
        savedAt: new Date().toISOString(),
    };
    const saved = persistReviewSnapshot(stateSyncRuntime.draftKey, envelope, { warnOnQuota: true });
    if (saved) {
        showNotification(t('review.draftSaved'));
        reportState.modified = false;
        persistSharedState();
    }
}

function restoreUIFromState() {
    document.querySelectorAll('.finding').forEach((article) => {
        const findingId = article.dataset.findingId;
        const state = reportState.findings[findingId] || createDefaultFindingState();

        const verdict = normalizeVerdict(state.verdict);
        article.dataset.verdict = verdict === 'none' ? '' : verdict;
        article.querySelectorAll(`input[name="verdict-${findingId}"]`).forEach((radio) => {
            radio.checked = radio.value === verdict;
        });

        const select = article.querySelector('.severity-select');
        if (select) {
            select.value = state.severity || '';
        }

        const textarea = article.querySelector('.notes textarea');
        if (textarea) {
            // Single source of truth: the notes textarea round-trips state.notes.
            // The old `actionItems` companion field was a phantom — written by
            // nothing, serialized by nothing — so joining it here only risked
            // resurrecting stale legacy-draft text. Migrate any such legacy
            // value into notes once, then ignore it.
            textarea.value = state.notes || state.actionItems || '';
        }
    });
    updateReviewMeta();
}

function getDetachedWindowFeatures() {
    const width = Math.min(760, Math.max(520, Math.round(window.innerWidth * 0.42)));
    const height = Math.min(1040, Math.max(720, window.innerHeight - 80));
    const left = Math.max(40, window.screenX + window.outerWidth - width - 48);
    const top = Math.max(40, window.screenY + 40);
    return `popup=yes,width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`;
}

function openReviewWindow() {
    if (detachedWindowRuntime.mode === WINDOW_MODES.review) {
        return;
    }

    if (detachedWindowRuntime.childWindow && !detachedWindowRuntime.childWindow.closed) {
        detachedWindowRuntime.childWindow.focus();
        applyWindowMode(WINDOW_MODES.player);
        showNotification(t('review.separateWindowFocused'));
        return;
    }

    const preferredTab = uiState.activeTab === 'summary'
        ? getDefaultTabForWindowMode(WINDOW_MODES.review)
        : uiState.activeTab;
    activateTab(preferredTab);
    persistSharedState();
    const targetUrl = buildWindowModeUrl(WINDOW_MODES.review);
    const popup = window.open(
        targetUrl,
        `screenscribe-review-${reportState.reportId || 'report'}`,
        getDetachedWindowFeatures()
    );

    // Safari + some Chromium configs silently return null or a handle that
    // immediately reports .closed when popups are blocked. Fall back to a
    // same-tab navigation so the user still lands in review view.
    const popupBlocked = !popup || popup.closed;
    if (popupBlocked) {
        showNotification(t('review.separateWindowBlocked'));
        window.location.assign(targetUrl);
        return;
    }

    detachedWindowRuntime.childWindow = popup;
    applyWindowMode(WINDOW_MODES.player);
    startDetachedWindowWatch();
    popup.focus();
    showNotification(t('review.separateWindowOpened'));
}

function reattachWorkspace() {
    if (detachedWindowRuntime.mode === WINDOW_MODES.review) {
        allowProgrammaticClose();
        broadcastWindowCommand('reattach-workspace');
        applyWindowMode(WINDOW_MODES.workspace);
        window.close();
        return;
    }

    if (detachedWindowRuntime.childWindow && !detachedWindowRuntime.childWindow.closed) {
        allowProgrammaticClose(detachedWindowRuntime.childWindow);
        detachedWindowRuntime.childWindow.close();
    }
    detachedWindowRuntime.childWindow = null;
    stopDetachedWindowWatch();
    applyWindowMode(WINDOW_MODES.workspace);
    showNotification(t('review.singleWindowRestored'));
}

function manualFindingId(markerId) {
    return `manual-${markerId}`;
}

function bindThumbnailClicks(scope = document) {
    scope.querySelectorAll('.thumbnail').forEach(img => {
        if (img.dataset.lightboxBound === 'true') return;
        img.dataset.lightboxBound = 'true';
        // Make the clickable thumbnail keyboard-operable: it acts as a button
        // that opens the annotation lightbox, so expose role/tabindex and
        // activate on Enter/Space.
        if (!img.hasAttribute('tabindex')) img.setAttribute('tabindex', '0');
        if (!img.hasAttribute('role')) img.setAttribute('role', 'button');
        img.addEventListener('click', () => openLightbox(img));
        img.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openLightbox(img);
            }
        });
    });
}

// Shared function to build review data - used by both JSON and ZIP export
// =============================================================================
// REVIEW META: live counters + findings filtering (stats cards are filters).
// Rejected findings ("false alarm") disappear from the default list and all
// deliverables; they stay reachable through the "Rejected" filter and are
// recorded explicitly in saves/exports so they are not re-flagged later.
// =============================================================================

function getEffectiveSeverity(article) {
    const findingId = article.dataset.findingId;
    const override = reportState.findings[findingId]?.severity;
    return override || article.dataset.severity || 'medium';
}

function getReviewStatus(article) {
    if (article.dataset.verdict === 'accepted') return 'accepted';
    if (article.dataset.verdict === 'rejected') return 'rejected';
    return 'none';
}

function isRejectedFinding(findingId) {
    return normalizeVerdict(reportState.findings[findingId]?.verdict) === 'rejected';
}

function updateReviewMeta() {
    const articles = Array.from(document.querySelectorAll('.finding')).filter(
        (article) => article.dataset.mergedAway !== 'true'
    );
    const counts = { total: 0, critical: 0, high: 0, medium: 0, low: 0,
        accepted: 0, rejected: 0, none: 0 };
    articles.forEach((article) => {
        const status = getReviewStatus(article);
        counts[status] += 1;
        if (status !== 'rejected') {
            counts.total += 1;
            const sev = getEffectiveSeverity(article);
            if (counts[sev] !== undefined) counts[sev] += 1;
        }
    });
    const findingsBtn = document.querySelector('.tab-btn[data-tab="findings"]');
    if (findingsBtn) {
        findingsBtn.textContent = t('review.findings') + ' (' + counts.total + ')';
    }
}

// =============================================================================
// HUMAN MERGE (N -> 1): the reviewer selects several findings and folds them
// into a single richer finding. This is the human-trigger twin of the machine
// dedup pass (`screenscribe/unified/dedup.py::merge_finding_group`) and MUST
// preserve the same union contract: highest severity, de-duplicated union of
// action_items + affected_components (+ issues / transcript / keywords on this
// UI layer), the richest (editable) description, and a `merged_from_ids`
// provenance trail. Nothing from the merged-away findings is lost.
// =============================================================================

const MERGE_SEVERITY_RANK = {
    critical: 4,
    high: 3,
    medium: 2,
    low: 1,
    info: 0,
    none: 0,
};

// Order-preserving, case-insensitive union of string lists (mirrors the
// dedup pass's action_items / affected_components combination).
function unionStrings(lists) {
    const out = [];
    const seen = new Set();
    for (const list of lists || []) {
        // Skip non-array lists: a string would char-soup (each char is a string
        // and passes the filter below) and a non-iterable (number/object) throws.
        if (!Array.isArray(list)) continue;
        for (const item of list) {
            if (typeof item !== 'string') continue;
            const key = item.trim().toLowerCase();
            if (!key || seen.has(key)) continue;
            seen.add(key);
            out.push(item);
        }
    }
    return out;
}

// Single guarded reader for the server-embedded #original-findings payload.
// A malformed/empty embed must degrade to [] rather than throw and abort the
// whole viewer init; every original-findings consumer routes through here.
function getOriginalFindingsList() {
    const el = document.getElementById('original-findings');
    if (!el) return [];
    try {
        const list = JSON.parse(el.textContent);
        return Array.isArray(list) ? list : [];
    } catch (error) {
        console.warn('Could not parse original-findings embed:', error);
        return [];
    }
}

function getOriginalFindingsById() {
    const byId = {};
    for (const f of getOriginalFindingsList()) {
        byId[f.id] = f;
    }
    return byId;
}

// Fold a group of UI findings into one, mirroring dedup.merge_finding_group:
// earliest finding is the base (keeps id/timestamp/category), highest severity
// wins, action_items/affected_components/issues are unioned, the richest summary
// is kept (unless overridden), and merged_from_ids records every absorbed id.
function mergeFindingGroup(group, summaryOverride = null, baseId = null) {
    if (!Array.isArray(group) || group.length === 0) return null;
    if (group.length === 1) return group[0];

    const sorted = [...group].sort(
        (a, b) => (Number(a.timestamp) || 0) - (Number(b.timestamp) || 0)
    );
    // Default base = earliest finding (the live human-merge picks the survivor by
    // timestamp). When reconstructing a fold from a data-side merged_from_ids
    // trail (disk reload), the survivor identity is explicit, so honor it: the
    // surviving id/category/timestamp must be the one the trail was recorded on,
    // not whichever member happens to be earliest.
    let base = sorted[0];
    if (baseId != null) {
        const explicit = group.find((f) => normId(f.id) === normId(baseId));
        if (explicit) base = explicit;
    }
    const ua = (f) => f.unified_analysis || {};

    // Highest severity across the group.
    let bestSeverity = ua(base).severity || 'medium';
    let bestRank = MERGE_SEVERITY_RANK[bestSeverity] ?? 0;
    for (const f of sorted) {
        const sev = ua(f).severity || 'medium';
        const rank = MERGE_SEVERITY_RANK[sev] ?? 0;
        if (rank > bestRank) {
            bestRank = rank;
            bestSeverity = sev;
        }
    }

    const actionItems = unionStrings(sorted.map((f) => ua(f).action_items));
    const affected = unionStrings(sorted.map((f) => ua(f).affected_components));
    const issues = unionStrings(sorted.map((f) => ua(f).issues_detected));
    const keywords = unionStrings(
        sorted.map((f) => f.keywords || ua(f).keywords)
    );
    // Transcript excerpts: every finding's transcript line (`text`) plus any
    // explicit transcript_excerpts arrays, so no spoken evidence is dropped.
    const excerpts = unionStrings(
        sorted.map((f) => {
            const arr = Array.isArray(f.transcript_excerpts)
                ? f.transcript_excerpts.slice()
                : [];
            if (f.text) arr.push(f.text);
            return arr;
        })
    );

    // Richest description = the longest member summary, unless the reviewer
    // edited it (summaryOverride).
    let richest = '';
    for (const f of sorted) {
        const s = ua(f).summary || '';
        if (s.length > richest.length) richest = s;
    }
    const summary =
        summaryOverride != null && summaryOverride !== '' ? summaryOverride : richest;

    // Provenance trail: keep base's prior merged_from_ids, then every absorbed
    // member id (and its own prior trail) so a re-merge never loses ancestry.
    const mergedFromIds = [];
    const seenIds = new Set();
    const pushId = (id) => {
        if (id == null) return;
        if (seenIds.has(id)) return;
        seenIds.add(id);
        mergedFromIds.push(id);
    };
    (base.merged_from_ids || []).forEach(pushId);
    for (const f of sorted) {
        if (f === base) continue;
        pushId(f.id);
        (f.merged_from_ids || []).forEach(pushId);
    }

    // Auto-merge ancestry (Round-7 P2): a member that was ALREADY auto-merged
    // server-side carries its provenance under `unified_analysis.merged_from_ids`
    // (the (detection_id, timestamp) pairs) -- NOT the top-level trail above. Fold
    // every member's nested ancestry into the survivor so a human merge of an
    // auto-merged finding never drops its folded-away provenance.
    const nestedAncestry = [];
    const seenAncestry = new Set();
    for (const f of sorted) {
        for (const pair of ua(f).merged_from_ids || []) {
            const key = JSON.stringify(pair);
            if (seenAncestry.has(key)) continue;
            seenAncestry.add(key);
            nestedAncestry.push(pair);
        }
    }

    // Folded-away evidence frames (Round-7 P2): a member's auto-merged members
    // live in its top-level `merged_frames`. The absorbed member card is the only
    // representative of those frames, so union every member's `merged_frames` into
    // the new group instead of keeping only the base's -- otherwise that evidence
    // is silently dropped on save/export.
    const mergedFrames = [];
    const seenFrames = new Set();
    for (const f of sorted) {
        for (const frame of f.merged_frames || []) {
            const key = frame && frame.id != null ? normId(frame.id) : JSON.stringify(frame);
            if (seenFrames.has(key)) continue;
            seenFrames.add(key);
            mergedFrames.push(frame);
        }
    }

    const mergedUnified = {
        ...(base.unified_analysis || {}),
        summary,
        severity: bestSeverity,
        action_items: actionItems,
        affected_components: affected,
        issues_detected: issues,
    };
    if (nestedAncestry.length) mergedUnified.merged_from_ids = nestedAncestry;

    const merged = {
        ...base,
        id: base.id,
        unified_analysis: mergedUnified,
        transcript_excerpts: excerpts,
        merged_from_ids: mergedFromIds,
    };
    if (keywords.length) merged.keywords = keywords;
    if (mergedFrames.length) merged.merged_frames = mergedFrames;
    return merged;
}

// Collapse the REVIEWER state (human_review) of an absorbed merge group onto the
// survivor. mergeFindingGroup reconciles only the AI `unified_analysis`; without
// this the survivor kept solely its own (base) verdict/severity/notes/annotations
// and every reviewer action on a merged-away member was silently lost (a folded
// survivor exported as none/medium despite reviewer work on its members).
//
// The operator-accepted default:
//   verdict           -> accepted-wins (accepted > rejected > none across the group);
//   severity_override -> the HIGHEST rank any member set (MERGE_SEVERITY_RANK),
//                        null when nobody overrode;
//   notes             -> deduped union of every member's notes (nothing dropped);
//   annotations       -> the survivor's OWN annotations only (they are rasterized
//                        onto the SURVIVOR image, so a member's mark must never be
//                        unioned in here or it would land on the wrong image);
//   memberAnnotations -> absorbed members' annotations kept as separate evidence
//                        {finding_id, annotations} so nothing is lost.
//
// Reads reportState.findings across the group, so it is identical for live UI
// merges and disk-reload folds (computeMergedFindings already reconciles group
// membership into the survivor's merged_from_ids regardless of provenance).
const VERDICT_RANK = { accepted: 2, rejected: 1, none: 0 };
function reconcileMergedReview(survivorId, mergedFromIds) {
    const survivorKey = normId(survivorId);
    const ids = [];
    const seen = new Set();
    const pushId = (id) => {
        const k = normId(id);
        if (k === '' || seen.has(k)) return;
        seen.add(k);
        ids.push(k);
    };
    pushId(survivorKey);
    (mergedFromIds || []).forEach(pushId);

    let verdict = 'none';
    let severity = null;
    let severityRank = -1;
    const noteLists = [];

    const survivorReview = reportState.findings[survivorKey] || {};
    // Cold-reload safety: after a fresh load from /api/review-state the absorbed
    // members are no longer standalone entries in reportState (only the survivor
    // is), so their marks survive ONLY as the survivor's persisted
    // member_annotations. Seed from there, then let any live (same-session) member
    // annotation override its persisted copy by finding_id so a re-merge in the
    // same session still wins. Without this seed the evidence is silently dropped
    // on the next save/export.
    const persistedMemberAnns = new Map();
    if (Array.isArray(survivorReview.member_annotations)) {
        for (const entry of survivorReview.member_annotations) {
            if (entry && entry.finding_id != null
                && Array.isArray(entry.annotations) && entry.annotations.length) {
                persistedMemberAnns.set(normId(entry.finding_id), entry.annotations);
            }
        }
    }

    const memberAnnotations = [];
    const emittedMemberAnns = new Set();
    for (const id of ids) {
        const review = reportState.findings[id] || {};
        const v = normalizeVerdict(review.verdict);
        if ((VERDICT_RANK[v] ?? 0) > (VERDICT_RANK[verdict] ?? 0)) verdict = v;

        if (review.severity) {
            const rank = MERGE_SEVERITY_RANK[review.severity] ?? 0;
            if (rank > severityRank) {
                severityRank = rank;
                severity = review.severity;
            }
        }

        const note = typeof review.notes === 'string' ? review.notes.trim() : '';
        if (note) noteLists.push([note]);

        if (id !== survivorKey) {
            const live = Array.isArray(review.annotations) ? review.annotations : [];
            const anns = live.length ? live : (persistedMemberAnns.get(id) || []);
            if (anns.length) {
                memberAnnotations.push({ finding_id: id, annotations: anns });
                emittedMemberAnns.add(id);
            }
        }
    }
    // Any persisted member that was not in `ids` (defensive: a survivor whose
    // merged_from_ids trail was trimmed) still keeps its evidence.
    for (const [id, anns] of persistedMemberAnns) {
        if (!emittedMemberAnns.has(id)) {
            memberAnnotations.push({ finding_id: id, annotations: anns });
        }
    }

    return {
        verdict,
        severity,
        notes: unionStrings(noteLists).join('\n\n'),
        annotations: Array.isArray(survivorReview.annotations) ? survivorReview.annotations : [],
        memberAnnotations,
    };
}

// Pure-state merge: fold the given finding ids into one group and record it on
// reportState.merges. DOM rendering is handled separately by
// mergeSelectedFindings() so this stays unit-testable without a browser.
function mergeFindings(ids) {
    if (!Array.isArray(ids) || ids.length < 2) return null;
    const byId = getOriginalFindingsById();
    reportState.merges = Array.isArray(reportState.merges) ? reportState.merges : [];

    // Expand any selected id that is itself an existing merge base, absorbing its
    // members (re-merge / chaining), and drop the superseded merge entry.
    const expanded = [];
    const seen = new Set();
    const remaining = [];
    const pushMember = (id) => {
        const key = normId(id);
        if (seen.has(key)) return;
        seen.add(key);
        expanded.push(id);
    };
    // Selected ids arrive from the DOM as strings while a stored merge entry's
    // id is the number parsed from the report JSON. Compare in one space (normId)
    // so a re-merge of an existing group's survivor expands and supersedes that
    // group instead of building a parallel duplicate (same fix as the survivor
    // verdict path; this is the rechain branch).
    const selectedKeys = new Set(ids.map(normId));
    for (const m of reportState.merges) {
        if (selectedKeys.has(normId(m.id))) {
            (m.member_ids || []).forEach(pushMember);
        } else {
            remaining.push(m);
        }
    }
    for (const id of ids) pushMember(id);

    const members = expanded.filter((id) => byId[id]);
    const group = members.map((id) => byId[id]);
    if (group.length < 2) return null;

    const merged = mergeFindingGroup(group, null);
    reportState.merges = remaining;
    reportState.merges.push({
        id: merged.id,
        member_ids: members,
        summary_override: null,
    });

    // Review state: the surviving finding is treated as accepted (the reviewer
    // deliberately kept it); absorbed members revert to "none" so they neither
    // ship as standalone findings nor leak into the rejected[] summary.
    if (!reportState.findings[merged.id]) {
        reportState.findings[merged.id] = createDefaultFindingState();
    }
    reportState.findings[merged.id].verdict = 'accepted';
    for (const id of members) {
        // Member ids arrive from the DOM as strings while merged.id is the integer
        // parsed from the report JSON; compare in one space (normId) so the
        // survivor is skipped and its just-set `accepted` verdict is preserved.
        if (normId(id) === normId(merged.id)) continue;
        if (reportState.findings[id]) reportState.findings[id].verdict = 'none';
    }

    reportState.modified = true;
    scheduleSharedStateSync();
    return merged;
}

// Normalize a finding id to its string form for cross-source comparison.
// Finding ids arrive as INTEGERS from the report JSON (JSON.parse), while merge
// member ids captured from the DOM (dataset.findingId) are STRINGS. Folding must
// compare them in ONE space or a `Set.has` silently no-ops — the export-leak bug
// where absorbed members survived and survivors were duplicated (10 -> 13).
function normId(id) {
    return id == null ? '' : String(id);
}

// Recompute the human-merge groups against the current original findings and
// return the merged survivor entries plus the set of every id those entries
// represent (absorbed members AND the survivor), so callers drop all of them
// from the flat list and re-add exactly one merged entry per group.
//
// The fold is DATA-driven and reconciles BOTH provenance sources so it is
// identical whether the merge is fresh or reloaded from disk:
//   1. reportState.merges    — in-memory UI state (same session). member_ids
//      include the survivor and arrive from the DOM as strings.
//   2. merged_from_ids       — the durable trail persisted on each survivor's
//      review state (reportState.merges is never persisted, so after a reload /
//      re-open this is the ONLY signal). The survivor is the finding carrying
//      the trail; merged_from_ids lists the absorbed members.
// Summing the two would double-fold (the duplicated survivors); reconciling by
// survivor id folds once and is idempotent across fresh / reloaded / mixed state.
function computeMergedFindings(byId) {
    const merges = Array.isArray(reportState.merges) ? reportState.merges : [];

    // survivorKey -> Set(normalized ids in the group, including the survivor).
    const groups = new Map();
    const ensureGroup = (survivor) => {
        const key = normId(survivor);
        if (!groups.has(key)) groups.set(key, new Set([key]));
        return groups.get(key);
    };

    // Source 1: in-memory UI merges.
    for (const m of merges) {
        if (!m || m.id == null) continue;
        const g = ensureGroup(m.id);
        (m.member_ids || []).forEach((id) => g.add(normId(id)));
    }

    // Source 2: data-side merged_from_ids (finding object after a reload, and/or
    // the restored per-finding review state).
    for (const key of Object.keys(byId)) {
        const finding = byId[key] || {};
        const review = reportState.findings[key] || {};
        const trail = []
            .concat(Array.isArray(finding.merged_from_ids) ? finding.merged_from_ids : [])
            .concat(Array.isArray(review.merged_from_ids) ? review.merged_from_ids : []);
        if (trail.length === 0) continue;
        const g = ensureGroup(key);
        trail.forEach((id) => g.add(normId(id)));
    }

    // Chained merges: a survivor that is itself absorbed by another group is not
    // a top-level survivor — fold its members up into the absorbing group and
    // drop its own entry, so it is emitted once (idempotent on re-merge).
    let changed = true;
    while (changed) {
        changed = false;
        for (const [survivorKey, ids] of groups) {
            for (const id of ids) {
                if (id !== survivorKey && groups.has(id)) {
                    groups.get(id).forEach((x) => ids.add(x));
                    groups.delete(id);
                    changed = true;
                    break;
                }
            }
            if (changed) break;
        }
    }

    const mergedList = [];
    const memberIds = new Set();
    for (const [survivorKey, ids] of groups) {
        const group = [...ids].map((id) => byId[id]).filter(Boolean);
        if (group.length < 2) continue; // not a real merge -> leave members intact
        ids.forEach((id) => {
            if (byId[id]) memberIds.add(id);
        });
        const m = merges.find((x) => normId(x.id) === survivorKey);
        mergedList.push(mergeFindingGroup(group, m ? m.summary_override : null, survivorKey));
    }
    return { mergedList, memberIds };
}

function buildMergedReviewEntry(merged) {
    const { screenshot, ...rest } = merged;
    // Inherit the reviewer state of the WHOLE group, not just the survivor's.
    const r = reconcileMergedReview(merged.id, merged.merged_from_ids);
    const human_review = {
        verdict: r.verdict,
        severity_override: r.severity || null,
        notes: r.notes || '',
        annotations: r.annotations,
        reviewer: reportState.reviewer,
        reviewed_at: new Date().toISOString(),
        merged_from_ids: merged.merged_from_ids || [],
    };
    // Absorbed members' annotations are preserved as evidence (never rasterized
    // onto the survivor's image), so reviewer markup on members is not lost.
    if (r.memberAnnotations.length) human_review.member_annotations = r.memberAnnotations;
    const result = { ...rest, human_review };
    if (merged.screenshot_path) {
        result.screenshot_path = merged.screenshot_path;
    }
    return result;
}

// ---- Human-merge UI glue ----------------------------------------------------

function findFindingArticle(id) {
    // Normalize BOTH sides: the data-side restore fallback calls this with numeric
    // ids straight from `merged_from_ids` (report JSON) while `dataset.findingId`
    // is always a string, so a strict `===` silently misses and the absorbed
    // member stays visible (same id-space mismatch normId fixes everywhere else).
    const key = normId(id);
    const articles = Array.from(document.querySelectorAll('.finding')).filter(
        (a) => normId(a.dataset.findingId) === key
    );
    // Prefer an original card over a previously-rendered merged card (chaining).
    return articles.find((a) => a.dataset.merged !== 'true') || articles[0] || null;
}

function getSelectedMergeIds() {
    return Array.from(document.querySelectorAll('#tab-findings .finding'))
        .filter((a) => a.dataset.mergedAway !== 'true')
        .filter((a) => a.querySelector('.merge-select')?.checked)
        .map((a) => a.dataset.findingId);
}

function updateMergeBar() {
    const bar = document.getElementById('merge-action-bar');
    const btn = document.getElementById('merge-action-btn');
    if (!bar || !btn) return;
    const count = getSelectedMergeIds().length;
    bar.hidden = count < 2;
    btn.disabled = count < 2;
    btn.textContent =
        t('review.mergeFindingsBtn') + (count >= 2 ? ` (${count})` : '');
}

function renderMergedCard(merged) {
    const ua = merged.unified_analysis || {};
    const article = document.createElement('article');
    article.className = 'finding finding-merged';
    article.dataset.findingId = merged.id;
    // Honor the restored/persisted verdict instead of a constant: a merge hydrated
    // after the survivor was rejected must stay rejected. dataset.verdict drives
    // filtering, counters, styling and getReviewStatus, and must agree with the
    // reject radio below (both read reportState.findings[merged.id]).
    const state = reportState.findings[merged.id] || {};
    const currentVerdict = normalizeVerdict(state.verdict);
    article.dataset.verdict = currentVerdict;
    article.dataset.severity = ua.severity || 'medium';
    article.dataset.merged = 'true';

    const header = document.createElement('div');
    header.className = 'finding-header';
    // A merged survivor must itself be selectable so the group can absorb a third
    // duplicate (mergeFindings chains an existing merge base into the new group).
    // initMergeUI skips `dataset.merged` cards, and they are rendered AFTER it
    // runs, so the checkbox is added here. getSelectedMergeIds ignores mergedAway
    // members but keeps the survivor, so only the survivor becomes selectable.
    const mergeWrap = document.createElement('label');
    mergeWrap.className = 'merge-select-wrap';
    const mergeCheckbox = document.createElement('input');
    mergeCheckbox.type = 'checkbox';
    mergeCheckbox.className = 'merge-select';
    mergeCheckbox.setAttribute('aria-label', t('review.mergeSelectLabel'));
    mergeWrap.appendChild(mergeCheckbox);
    header.appendChild(mergeWrap);
    const title = document.createElement('span');
    title.className = 'finding-title';
    // Localize the category badge instead of upper-casing the raw EN enum; an
    // unmapped category falls back to its own upper-cased value.
    const rawCategory = (merged.category || '').toString();
    const catKey = 'review.category_' + rawCategory;
    const catLabel = rawCategory ? t(catKey) : '';
    title.textContent = (catLabel && catLabel !== catKey ? catLabel : rawCategory).toUpperCase();
    const badge = document.createElement('span');
    badge.className = 'severity-badge merged-badge';
    badge.textContent = t('review.mergedBadge');
    header.appendChild(title);
    header.appendChild(badge);
    article.appendChild(header);

    const content = document.createElement('div');
    content.className = 'finding-content';

    // Editable richest description -> persisted as the merge summary_override.
    const summaryField = document.createElement('div');
    summaryField.className = 'review-field merged-summary-field';
    const summaryLabel = document.createElement('label');
    summaryLabel.textContent = t('review.findingSummary');
    const summaryTextarea = document.createElement('textarea');
    summaryTextarea.className = 'merged-summary';
    summaryTextarea.value = ua.summary || '';
    summaryTextarea.addEventListener('input', () => {
        // After a cold reload the fold is restored only from merged_from_ids and
        // reportState.merges is empty, so look the entry up in the normalized id
        // space and reconstruct it on demand when missing — otherwise the edit is
        // silently dropped and the review is never marked modified.
        let m = (reportState.merges || []).find((x) => normId(x.id) === normId(merged.id));
        if (!m) m = ensureMergeEntry(merged);
        m.summary_override = summaryTextarea.value;
        reportState.modified = true;
        scheduleSharedStateSync();
    });
    summaryField.appendChild(summaryLabel);
    summaryField.appendChild(summaryTextarea);
    content.appendChild(summaryField);

    if ((ua.action_items || []).length) {
        const actions = document.createElement('div');
        actions.className = 'ai-suggestions';
        actions.textContent =
            t('review.aiSuggestions') + ' ' + ua.action_items.join(', ');
        content.appendChild(actions);
    }
    if ((ua.affected_components || []).length) {
        const comps = document.createElement('div');
        comps.className = 'merged-components';
        comps.textContent =
            t('review.affectedComponents') + ': ' + ua.affected_components.join(', ');
        content.appendChild(comps);
    }

    const from = document.createElement('div');
    from.className = 'merged-from';
    // Human-facing trail shows every source (surviving id + absorbed ids).
    const sources = [merged.id, ...(merged.merged_from_ids || [])];
    from.textContent = t('review.mergedFromLabel') + ': ' + sources.join(', ');
    content.appendChild(from);

    // The survivor's screenshot, as an inspect/annotate surface — a merged
    // finding must stay annotatable like any other (initAnnotationTools binds
    // this container by its data-finding-id after the card is in the DOM).
    if (merged.screenshot) {
        const shot = document.createElement('div');
        shot.className = 'finding-screenshot';
        const container = document.createElement('div');
        container.className = 'annotation-container';
        container.dataset.findingId = merged.id;
        const img = document.createElement('img');
        img.className = 'thumbnail';
        img.src = merged.screenshot;
        img.setAttribute('data-full', merged.screenshot);
        img.title = t('media.manualFrameZoomTitle');
        const svg = document.createElement('svg');
        svg.className = 'annotation-svg';
        const hint = document.createElement('div');
        hint.className = 'annotation-hint';
        hint.textContent = t('media.manualFrameAnnotateHint');
        container.appendChild(img);
        container.appendChild(svg);
        container.appendChild(hint);
        shot.appendChild(container);
        content.appendChild(shot);
    }

    article.appendChild(content);

    // Full review controls so the survivor stays reviewable — without these the
    // merged finding was auto-`accepted` and could no longer be rejected,
    // re-prioritized, or annotated. Same surface as a normal finding card, so the
    // existing input/change delegation (handleInputEvent / handleChangeEvent)
    // drives reportState.findings[merged.id] with no extra listeners.
    const review = document.createElement('div');
    review.className = 'human-review';
    // `state` / `currentVerdict` are computed once at the top of this function so
    // dataset.verdict and the reject radio share the one restored source.

    const reviewRow = document.createElement('div');
    reviewRow.className = 'review-row';

    const verdictField = document.createElement('div');
    verdictField.className = 'review-field';
    const verdictLabel = document.createElement('label');
    verdictLabel.textContent = t('review.verdict');
    verdictField.appendChild(verdictLabel);
    const radioGroup = document.createElement('div');
    radioGroup.className = 'radio-group';
    [['accepted', 'review.yes'], ['rejected', 'review.noFalseAlarm']].forEach(([value, labelKey]) => {
        const wrap = document.createElement('label');
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.setAttribute('type', 'radio');
        radio.setAttribute('name', 'verdict-' + merged.id);
        radio.value = value;
        radio.setAttribute('value', value);
        if (currentVerdict === value) radio.checked = true;
        const span = document.createElement('span');
        span.textContent = t(labelKey);
        wrap.appendChild(radio);
        wrap.appendChild(span);
        radioGroup.appendChild(wrap);
    });
    verdictField.appendChild(radioGroup);
    reviewRow.appendChild(verdictField);

    const sevField = document.createElement('div');
    sevField.className = 'review-field';
    const sevLabel = document.createElement('label');
    sevLabel.textContent = t('review.changePriority');
    sevField.appendChild(sevLabel);
    const sevSelect = document.createElement('select');
    sevSelect.className = 'severity-select';
    [['', 'review.noChange'], ['critical', 'review.critical'], ['high', 'review.high'],
        ['medium', 'review.medium'], ['low', 'review.low']].forEach(([value, labelKey]) => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.setAttribute('value', value);
        opt.textContent = t(labelKey);
        sevSelect.appendChild(opt);
    });
    sevSelect.value = state.severity || '';
    sevField.appendChild(sevSelect);
    reviewRow.appendChild(sevField);
    review.appendChild(reviewRow);

    const notesField = document.createElement('div');
    notesField.className = 'review-field notes';
    const notesLabel = document.createElement('label');
    notesLabel.textContent = t('review.notes');
    notesField.appendChild(notesLabel);
    const notesArea = document.createElement('textarea');
    notesArea.setAttribute('placeholder', t('review.notesPlaceholder'));
    notesArea.value = state.notes || '';
    notesField.appendChild(notesArea);
    review.appendChild(notesField);

    article.appendChild(review);
    return article;
}

function applyMergeToDom(merged) {
    // Drop any stale merged card for this id (re-merge / chaining). The card's
    // dataset.findingId is a STRING (a real DOM coerces every dataset value)
    // while merged.id is the NUMBER parsed from the report JSON, so compare in
    // one space via normId() (consistent with ensureMergeEntry / the merge
    // group lookup below) -- a strict `===` here left numeric-id groups with two
    // visible merged cards for one export group.
    Array.from(document.querySelectorAll('.finding-merged')).forEach((card) => {
        if (normId(card.dataset.findingId) === normId(merged.id)) card.remove();
    });

    const group = (reportState.merges || []).find((m) => normId(m.id) === normId(merged.id));
    // On a reload the fold can come purely from the data-side merged_from_ids
    // trail (reportState.merges is never persisted), so fall back to the merged
    // object's own member set instead of only the survivor — otherwise the
    // absorbed originals would never hide on restore.
    const memberIds = group
        ? group.member_ids
        : [merged.id, ...(merged.merged_from_ids || [])];

    let anchor = null;
    for (const id of memberIds) {
        const article = findFindingArticle(id);
        if (!article) continue;
        if (!anchor) anchor = article;
        article.hidden = true;
        article.dataset.mergedAway = 'true';
        const checkbox = article.querySelector('.merge-select');
        if (checkbox) checkbox.checked = false;
    }

    const card = renderMergedCard(merged);
    if (anchor && anchor.parentNode) {
        anchor.parentNode.insertBefore(card, anchor);
    } else {
        const tab = document.getElementById('tab-findings');
        if (tab) tab.appendChild(card);
    }

    // Bind the merged card's screenshot as a live annotation surface. The
    // survivor's now-hidden original keeps the same data-finding-id, so
    // initAnnotationTools (which skips absorbed/hidden containers) hands the key
    // to the visible merged card.
    initAnnotationTools();
    // The merged card is inserted AFTER the initial bindThumbnailClicks() pass, so
    // its generated thumbnail would otherwise have no lightbox/keyboard binding —
    // the survivor card is the only visible screenshot for the group, so reviewers
    // must still be able to enlarge and annotate it. Idempotent (lightboxBound).
    bindThumbnailClicks(card);
}

// Reconstruct a normalized in-memory merge entry for a survivor whose fold was
// restored purely from the persisted merged_from_ids trail (cold reload, where
// reportState.merges starts empty — the /api/review-state path). Without an entry
// every merge-entry-keyed path silently no-ops: summary-edit persistence, member
// location (applyMergeToDom's group lookup), and re-merge chaining. Idempotent:
// an existing entry (e.g. one already carrying the reviewer's summary override) is
// returned untouched, and member_ids are stored in the one string id space.
function ensureMergeEntry(merged) {
    reportState.merges = Array.isArray(reportState.merges) ? reportState.merges : [];
    let entry = reportState.merges.find((x) => normId(x.id) === normId(merged.id));
    if (!entry) {
        entry = {
            id: normId(merged.id),
            member_ids: [merged.id, ...(merged.merged_from_ids || [])].map(normId),
            summary_override: null,
        };
        reportState.merges.push(entry);
    }
    return entry;
}

// Replay every persisted/data-side merge group into the DOM, idempotently. Used
// on hydration (draft-restore / detached-window sync) so a fold that lives only
// in state reaches the UI without an explicit Merge click. Mirrors the user-click
// path (applyMergeToDom) so absorbed cards hide and a merged summary card shows.
function restoreMergesToDom() {
    const findingsTab = document.getElementById('tab-findings');
    if (!findingsTab) return;

    // Reset any prior fold first so re-hydration is idempotent: un-hide absorbed
    // originals and drop stale generated cards, then re-apply from current state.
    findingsTab.querySelectorAll('.finding[data-merged-away="true"]').forEach((article) => {
        article.hidden = false;
        delete article.dataset.mergedAway;
    });
    findingsTab.querySelectorAll('.finding-merged').forEach((card) => card.remove());

    const byId = getOriginalFindingsById();
    const { mergedList } = computeMergedFindings(byId);
    // Reconstruct a normalized in-memory merge entry for every restored group
    // BEFORE rendering, so a cold-reload card behaves exactly like a freshly-merged
    // one: summary edits persist, members locate by normalized id, and re-merge
    // chains. This single reconstruction is what makes the cold-reload fold fully
    // functional rather than a fragile, partially-wired replay.
    mergedList.forEach((merged) => ensureMergeEntry(merged));
    mergedList.forEach((merged) => applyMergeToDom(merged));
}

function mergeSelectedFindings() {
    const ids = getSelectedMergeIds();
    if (ids.length < 2) {
        showNotification(t('review.mergeNeedTwo'));
        return null;
    }
    const merged = mergeFindings(ids);
    if (!merged) {
        showNotification(t('review.mergeNeedTwo'));
        return null;
    }
    applyMergeToDom(merged);
    updateReviewMeta();
    updateMergeBar();
    showNotification(t('review.mergeDone'));
    return merged;
}

function initMergeUI() {
    const findingsTab = document.getElementById('tab-findings');
    if (!findingsTab) return;

    findingsTab.querySelectorAll('.finding').forEach((article) => {
        if (article.dataset.merged === 'true') return;
        const header = article.querySelector('.finding-header');
        if (!header || header.querySelector('.merge-select')) return;
        const wrap = document.createElement('label');
        wrap.className = 'merge-select-wrap';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'merge-select';
        checkbox.setAttribute('aria-label', t('review.mergeSelectLabel'));
        wrap.appendChild(checkbox);
        header.insertBefore(wrap, header.firstChild);
    });

    if (!document.getElementById('merge-action-bar')) {
        const bar = document.createElement('div');
        bar.id = 'merge-action-bar';
        bar.className = 'merge-action-bar';
        bar.hidden = true;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.id = 'merge-action-btn';
        btn.className = 'btn merge-action-btn';
        btn.dataset.action = 'merge-findings';
        btn.disabled = true;
        btn.textContent = t('review.mergeFindingsBtn');
        btn.addEventListener('click', () => mergeSelectedFindings());
        bar.appendChild(btn);
        findingsTab.insertBefore(bar, findingsTab.firstChild);
    }

    updateMergeBar();
}

function buildRejectedSummary(originalFindings) {
    const rejected = [];
    for (const f of originalFindings) {
        const review = reportState.findings[f.id] || {};
        if (normalizeVerdict(review.verdict) !== 'rejected') continue;
        rejected.push({
            id: f.id,
            timestamp_formatted: f.timestamp_formatted,
            summary: (f.unified_analysis || {}).summary || f.text || '',
            severity: review.severity || (f.unified_analysis || {}).severity || 'medium',
            notes: review.notes || '',
            rejected_by: reportState.reviewer,
            rejected_at: new Date().toISOString(),
        });
    }
    return rejected;
}

function buildReviewData() {
    const originalFindings = getOriginalFindingsList();
    const reviewedFindings = [];

    const byId = {};
    for (const f of originalFindings) byId[f.id] = f;
    // Human-merge groups collapse N findings into one: absorbed members drop out
    // of the deliverable and are represented by a single merged entry.
    const { mergedList, memberIds } = computeMergedFindings(byId);

    for (const f of originalFindings) {
        if (memberIds.has(normId(f.id))) continue; // absorbed into a merge group
        const review = reportState.findings[f.id] || {};
        // Remove base64 screenshot - keep it lightweight
        const { screenshot, ...findingWithoutBase64 } = f;
        const annotations = review.annotations || [];

        const result = {
            ...findingWithoutBase64,
            human_review: {
                verdict: normalizeVerdict(review.verdict),
                severity_override: review.severity || null,
                notes: review.notes || '',
                annotations: annotations,
                reviewer: reportState.reviewer,
                reviewed_at: new Date().toISOString()
            }
        };

        // Keep original screenshot path reference (not base64)
        if (f.screenshot_path) {
            result.screenshot_path = f.screenshot_path;
        }

        reviewedFindings.push(result);
    }

    // One richer entry per merge group, carrying the union + merged_from_ids.
    for (const merged of mergedList) {
        reviewedFindings.push(buildMergedReviewEntry(merged));
    }

    const manualFrames = reportState.manualFrames.map((frame) => {
        const reviewKey = manualFindingId(frame.marker_id);
        const annotations = reportState.findings[reviewKey]?.annotations || [];
        return {
            ...frame,
            annotations,
        };
    });

    return {
        video: document.body.dataset.videoName,
        reviewed_at: new Date().toISOString(),
        reviewer: reportState.reviewer,
        findings: reviewedFindings,
        manual_frames: manualFrames,
    };
}

// Render a human-readable, i18n'd summary of a finding's annotations for the
// text deliverables (TODO markdown). The annotations themselves and their pixels
// live in the data + the annotated PNG; this is the DESCRIPTION layer so a reader
// of the TODO knows a finding was marked up and what was drawn/typed — without
// opening the image. Repeated types are counted (`2× arrow`); text annotations
// also carry their content verbatim (emoji included). Returns '' for none, so
// callers never emit an empty/garbage line.
function describeAnnotations(annotations) {
    const list = Array.isArray(annotations) ? annotations : [];
    if (list.length === 0) return '';
    const counts = { arrow: 0, rect: 0, pen: 0, text: 0 };
    const texts = [];
    for (const ann of list) {
        const type = ann && ann.type;
        if (Object.prototype.hasOwnProperty.call(counts, type)) counts[type] += 1;
        if (type === 'text' && ann.text) texts.push(ann.text);
    }
    const labels = {
        arrow: t('review.annotationArrow'),
        rect: t('review.annotationRect'),
        pen: t('review.annotationPen'),
        text: t('review.annotationText'),
    };
    const parts = [];
    for (const type of ['arrow', 'rect', 'pen', 'text']) {
        const n = counts[type];
        if (n <= 0) continue;
        parts.push(n > 1 ? `${n}× ${labels[type]}` : labels[type]);
    }
    let desc = parts.join(', ');
    if (texts.length > 0) {
        desc += ' „' + texts.join('", „') + '"';
    }
    return desc;
}

function buildTodoMarkdown(originalFindings, videoName, reviewer) {
    let md = `# TODO: ${videoName}\n`;
    md += `> ${t('review.todoReviewerLabel')}: ${reviewer} | ${t('review.todoDateLabel')}: ${new Date().toISOString().split('T')[0]}\n\n`;

    // ===== AI findings (severity-grouped, accepted only; rejected listed below) =====
    const bySeverity = { critical: [], high: [], medium: [], low: [] };

    // Collapse human-merge groups so the TODO carries one richer item per merge
    // (matching the JSON/ZIP deliverable), never the absorbed members separately.
    const byId = {};
    for (const f of originalFindings) byId[f.id] = f;
    const { mergedList, memberIds } = computeMergedFindings(byId);
    const effectiveFindings = originalFindings
        .filter((f) => !memberIds.has(normId(f.id)))
        .concat(mergedList);

    effectiveFindings.forEach((f, idx) => {
        // A folded merge survivor reflects the inherited group review
        // (verdict/severity/notes); a standalone finding keeps its own.
        const isMerged = Array.isArray(f.merged_from_ids) && f.merged_from_ids.length > 0;
        const review = isMerged
            ? reconcileMergedReview(f.id, f.merged_from_ids)
            : (reportState.findings[f.id] || {});
        const unified = f.unified_analysis || {};
        const severity = review.severity || unified.severity || 'medium';
        const verdict = normalizeVerdict(review.verdict);

        if (verdict === 'rejected') return;

        const checkbox = '[ ]';
        const summary = unified.summary || f.text || t('review.todoNoDescription');
        const notes = review.notes || '';
        const actionItems = unified.action_items || [];

        let item = `- ${checkbox} **#${idx + 1}** [${severity.toUpperCase()}] ${summary}`;
        if (notes) item += `\n  - ${t('review.todoNotesLabel')}: ${notes}`;
        if (actionItems.length > 0) {
            item += `\n  - ${t('review.todoActionsLabel')}: ${actionItems.slice(0, 3).join(', ')}`;
        }
        // Surface the reviewer's annotation + any human-merge so the TODO reader
        // sees the finding was marked up / stands in for several, without opening
        // the annotated PNG or the JSON.
        const annDesc = describeAnnotations(review.annotations || []);
        if (annDesc) item += `\n  - ${t('review.todoAnnotationLabel')}: ${annDesc}`;
        const mergedFrom = Array.isArray(f.merged_from_ids) ? f.merged_from_ids : [];
        if (mergedFrom.length > 0) {
            item += `\n  - ${t('review.mergedFromLabel')}: ${mergedFrom.map((id) => `#${id}`).join(', ')}`;
        }

        if (bySeverity[severity]) {
            bySeverity[severity].push(item);
        } else {
            bySeverity.medium.push(item);
        }
    });

    md += `## ${t('review.todoAiFindingsSection')}\n`;
    const aiBlocks = [];
    if (bySeverity.critical.length > 0) aiBlocks.push(`### ${t('review.critical')}\n${bySeverity.critical.join('\n')}`);
    if (bySeverity.high.length > 0) aiBlocks.push(`### ${t('review.high')}\n${bySeverity.high.join('\n')}`);
    if (bySeverity.medium.length > 0) aiBlocks.push(`### ${t('review.medium')}\n${bySeverity.medium.join('\n')}`);
    if (bySeverity.low.length > 0) aiBlocks.push(`### ${t('review.low')}\n${bySeverity.low.join('\n')}`);
    md += aiBlocks.length > 0 ? `${aiBlocks.join('\n\n')}\n\n` : `_${t('review.todoNoAiFindings')}_\n\n`;

    // Rejected findings stay out of the actionable list, but the dismissal is
    // recorded explicitly so nobody re-reports them.
    const rejected = buildRejectedSummary(originalFindings);
    if (rejected.length > 0) {
        md += `### ${t('review.todoRejectedSection')}\n`;
        md += rejected
            .map((r) => `- ~~[${r.severity.toUpperCase()}] ${r.summary}~~${r.notes ? ` — ${r.notes}` : ''}`)
            .join('\n');
        md += '\n\n';
    }

    // ===== Manual captures (ALL reviewer-captured frames — never dropped) =====
    // A manually captured frame means the reviewer flagged it as important. It
    // must show up as reviewer evidence even when it carries no AI analysis
    // (result == null). Analyzed frames render their result; unanalyzed ones are
    // listed with a "not AI-analyzed" status so nothing the reviewer did is lost.
    const manualFrames = reportState.manualFrames || [];
    md += `## ${t('review.todoManualSection')}\n`;
    if (manualFrames.length === 0) {
        md += `_${t('review.todoNoManualCaptures')}_\n\n`;
    } else {
        const baseName = (videoName || 'report').replace(/\.[^.]+$/, '');
        manualFrames.forEach((frame, idx) => {
            const reviewKey = manualFindingId(frame.marker_id);
            const review = reportState.findings[reviewKey] || {};
            const result = frame.result || null;
            const ts = frame.timestamp_formatted || '?';
            const transcript = frame.transcript || '';
            const notes = review.notes || frame.notes || '';
            let item;
            if (result) {
                // R14/R14b: use the shared effective-severity helper so the TODO
                // tag agrees with the on-card badge and the ZIP manifest (:2620).
                // It collapses an explicit 'none' (reviewer cleared the priority)
                // and a missing severity to '' → no tag at all, instead of leaking
                // a stray [NONE] or inventing a [LOW] the card never showed.
                const effectiveSev = manualFrameEffectiveSeverity(frame);
                // Defense-in-depth: the helper already collapses both an override
                // 'none' and a model-provided 'none' to '', so this guard is now
                // redundant -- kept as a cheap belt-and-suspenders against a stray
                // 'none' ever reaching the export tag.
                const severity = effectiveSev === 'none' ? '' : effectiveSev;
                const severityTag = severity ? ` [${severity.toUpperCase()}]` : '';
                const summary = result.summary || result.category || t('review.todoManualCaptureDefault');
                item = `- [ ] **${t('review.todoManualItemLabel')} #${idx + 1}** @ ${ts}${severityTag} ${summary}`;
                if (result.category) item += `\n  - ${t('review.todoCategoryLabel')}: ${result.category}`;
                if (transcript) item += `\n  - ${t('review.todoTranscriptLabel')}: ${transcript}`;
                if (notes) item += `\n  - ${t('review.todoNotesLabel')}: ${notes}`;
                if (result.suggested_fix) item += `\n  - ${t('review.todoSuggestedFixLabel')}: ${result.suggested_fix}`;
                const actionItems = result.action_items || [];
                if (actionItems.length > 0) item += `\n  - ${t('review.todoActionsLabel')}: ${actionItems.slice(0, 3).join(', ')}`;
            } else {
                item = `- [ ] **${t('review.todoManualItemLabel')} #${idx + 1}** @ ${ts} — ${t('review.todoManualNotAnalyzed')}`;
                if (transcript) item += `\n  - ${t('review.todoTranscriptLabel')}: ${transcript}`;
                if (notes) item += `\n  - ${t('review.todoNotesLabel')}: ${notes}`;
            }
            // A manually captured frame can also be annotated — describe it so the
            // TODO reader knows what was drawn/typed (text content incl. emoji).
            const manualAnnDesc = describeAnnotations(review.annotations || []);
            if (manualAnnDesc) item += `\n  - ${t('review.todoAnnotationLabel')}: ${manualAnnDesc}`;
            // Exact file reference, matching the ZIP's manual_frames/<file> naming
            // in exportReviewedZIP — the precise file per frame, not a half-link to
            // the folder (which becomes a mini-maze at 15 captures).
            const hasAnnotations = (review.annotations || []).length > 0;
            if (frame.frameDataUrl || hasAnnotations) {
                const ext =
                    hasAnnotations || (frame.frameDataUrl || '').startsWith('data:image/png')
                        ? 'png'
                        : 'jpg';
                item += `\n  - ${t('review.todoFileLabel')}: manual_frames/${baseName}_manual_${ts.replace(/[:.]/g, '-')}.${ext}`;
            }
            md += `${item}\n`;
        });
        md += '\n';
    }

    md += `---\n_Generated by screenscribe_\n`;
    return md;
}

async function saveReviewToDisk() {
    try {
        // Full review state travels with the request: report.json on disk is
        // the canonical record of verdicts/notes — not just manual markers.
        const response = await fetch('/api/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(buildReviewData()),
        });
        await fetchJsonOrThrow(response, 'Failed to save review to disk.');
        showNotification(t('review.reviewSaved'));

        // Clear draft after successful save to workspace
        try {
            localStorage.removeItem(stateSyncRuntime.draftKey);
        } catch (e) {}
        reportState.modified = false;
        persistSharedState();

    } catch (error) {
        if (DEBUG) console.error('Save to disk failed:', error);
        showNotification(t('review.saveFailed', { message: error.message }));
    }
}

async function exportReviewedJSON() {

    const reviewedCount = Object.values(reportState.findings).filter(f => normalizeVerdict(f.verdict) !== 'none').length;
    if (reviewedCount === 0) {
        if (!confirm(t('review.noReviewed'))) {
            return;
        }
    }

    const fullData = buildReviewData();
    const originalFindingsForExport = getOriginalFindingsList();
    const output = {
        ...fullData,
        // Deliverable carries only live findings; rejected ones are recorded
        // explicitly below so downstream consumers do not re-flag them.
        findings: fullData.findings.filter((f) => !isRejectedFinding(f.id)),
        rejected: buildRejectedSummary(originalFindingsForExport),
        // Strip the base64 manual-frame images: the reviewed JSON is the
        // lightweight record (AI findings already drop their base64 screenshot).
        // A self-contained-with-images deliverable is the ZIP export, not this
        // JSON — one frame's data URL is ~500KB and bloats the whole file.
        manual_frames: (fullData.manual_frames || []).map(
            ({ frameDataUrl, ...rest }) => rest
        ),
    };
    const videoName = document.body.dataset.videoName || 'report';
    const baseName = videoName.replace(/\.[^.]+$/, '');
    const filename = 'report_reviewed_' + baseName + '.json';
    const blob = new Blob([JSON.stringify(output, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showNotification(t('review.exportDoneSimple') + ' ' + filename);

    try {
        localStorage.removeItem(stateSyncRuntime.draftKey);
    } catch (e) {}
    reportState.modified = false;
    persistSharedState();
}

async function exportReviewedZIP() {

    const reviewedCount = Object.values(reportState.findings).filter(f => normalizeVerdict(f.verdict) !== 'none').length;
    if (reviewedCount === 0) {
        if (!confirm(t('review.noReviewed'))) {
            return;
        }
    }

    showNotification(t('review.generatingZip'));

    try {
        const zip = new JSZip();
        const originalFindings = getOriginalFindingsList();
        const videoName = document.body.dataset.videoName || 'report';
        const baseName = videoName.replace(/\.[^.]+$/, '');
        const annotatedFolder = zip.folder('annotated');
        const manualFolder = zip.folder('manual_frames');
        const screenshotsFolder = zip.folder('screenshots');

        const reviewedFindings = [];
        const manifestFindings = [];
        let findingIndex = 0;

        // Collapse human-merge groups so the handoff bundle (report_reviewed JSON
        // + agent_manifest.json) carries one richer entry per merge, never the
        // absorbed members. Absorbed members revert to verdict `none` (not
        // `rejected`), so without this fold they would leak as standalone
        // findings here. Mirrors buildReviewData()/buildTodoMarkdown() routing.
        const byId = {};
        for (const f of originalFindings) byId[f.id] = f;
        const { mergedList, memberIds } = computeMergedFindings(byId);
        const effectiveFindings = originalFindings
            .filter((f) => !memberIds.has(normId(f.id)))
            .concat(mergedList);

        for (const f of effectiveFindings) {
            // A folded merge survivor inherits the reviewer state of the whole
            // group (verdict/severity/notes), with absorbed members' annotations
            // kept as evidence; a standalone finding keeps its own review.
            const isMerged = Array.isArray(f.merged_from_ids) && f.merged_from_ids.length > 0;
            const review = isMerged
                ? reconcileMergedReview(f.id, f.merged_from_ids)
                : (reportState.findings[f.id] || {});
            // Rejected findings (false alarms) do not ship in the bundle —
            // they are listed explicitly in the JSON's `rejected` section.
            if (normalizeVerdict(review.verdict) === 'rejected') continue;
            const { screenshot, ...findingWithoutBase64 } = f;
            const annotations = review.annotations || [];

            const result = {
                ...findingWithoutBase64,
                human_review: {
                    verdict: normalizeVerdict(review.verdict),
                    severity_override: review.severity || null,
                    notes: review.notes || '',
                    annotations: annotations,
                    reviewer: reportState.reviewer,
                    reviewed_at: new Date().toISOString()
                }
            };
            // Preserve absorbed members' annotations off the survivor's image.
            if (isMerged && review.memberAnnotations && review.memberAnnotations.length) {
                result.human_review.member_annotations = review.memberAnnotations;
            }

            // Generate annotated screenshot if there are annotations
            if (annotations.length > 0) {
                try {
                    const tool = annotationTools.get(String(f.id));
                    const thumb = document.querySelector(`[data-finding-id="${f.id}"] .thumbnail`);
                    let dataUrl = null;
                    if (tool && typeof tool.getMergedDataURL === 'function') {
                        dataUrl = await tool.getMergedDataURL();
                    } else if (thumb) {
                        dataUrl = await mergeImageAndAnnotations(thumb, annotations);
                    }
                    if (!dataUrl && thumb) {
                        const fallbackW = thumb.naturalWidth || 1920;
                        const fallbackH = thumb.naturalHeight || 1080;
                        dataUrl = await annotationsToPng(annotations, fallbackW, fallbackH);
                    }
                    // Extract base64 data (remove data:image/png;base64, prefix)
                    if (dataUrl && dataUrl.startsWith('data:image')) {
                        const base64Data = dataUrl.split(',')[1];
                        const filename = baseName + '_' + f.timestamp_formatted.replace(/[:.]/g, '-') + '_' + f.category + '_annotated.png';
                        annotatedFolder.file(filename, base64Data, {base64: true});
                        result.screenshot_annotated = 'annotated/' + filename;
                    }
                } catch (e) {
                    console.error('Failed to generate annotated screenshot for finding', f.id, e);
                }
            }

            // Ship the original screenshot as a real file under screenshots/ and
            // reference it by RELATIVE path. Findings reaching here are already
            // non-rejected (rejected ones are skipped above), so each one gets a
            // screenshot file and a manifest entry for coding-agent handoff.
            findingIndex++;
            const fIdx = String(findingIndex).padStart(2, '0');
            const tsClean = (f.timestamp_formatted || '00-00').replace(/[:.]/g, '-');
            const cat = f.category || 'unknown';
            const screenshotFilename = 'F' + fIdx + '_' + cat + '_' + tsClean + '.jpg';
            const screenshotRelPath = 'screenshots/' + screenshotFilename;

            let screenshotWritten = false;
            if (screenshot && screenshot.startsWith('data:image')) {
                const imgBase64 = screenshot.split(',')[1];
                if (imgBase64) {
                    screenshotsFolder.file(screenshotFilename, imgBase64, {base64: true});
                    screenshotWritten = true;
                }
            }

            // Only reference a screenshot that was actually written to the ZIP.
            // Text-only / extraction-failed findings carry no data-URL frame, so
            // the file above is never written; pointing screenshot_original (and
            // the manifest) at screenshots/Fxx then made the reviewed JSON and
            // agent_manifest.json reference a MISSING file in the bundle. Relative
            // path replaces the previous absolute screenshot_path leak.
            if (screenshotWritten) {
                result.screenshot_original = screenshotRelPath;
            }

            // Build the manifest entry: priority, action items, and a testable
            // acceptance criterion so an agent can verify the fix.
            const unified = f.unified_analysis || {};
            const severity = review.severity || unified.severity || 'medium';
            const priorityMap = {critical: 'P0', high: 'P0', medium: 'P1', low: 'P2'};
            const priority = priorityMap[severity] || 'P1';
            const actionItems = (unified.action_items || []).slice(0, 4);
            const title = unified.summary || f.text || 'No description';
            const verify = actionItems.length > 0
                ? actionItems[0] + ' — confirmed visually or by test'
                : title + ' — verified as resolved';

            // Textual annotation summary (type + text, emoji verbatim) so the
            // coding agent knows the finding was marked up AND what was
            // drawn/typed — without rasterizing or fetching the annotated PNG
            // (`annotated` already links the image). Always an array for a stable
            // schema; empty when the finding carries no annotations.
            const annotationSummary = annotations.map((ann) => {
                const entry = { type: ann.type };
                if (ann.type === 'text' && ann.text) entry.text = ann.text;
                return entry;
            });

            const manifestEntry = {
                id: 'F' + fIdx,
                priority: priority,
                severity: severity,
                title: title,
                user_said: f.text || '',
                context: f.context || '',
                screenshot: screenshotWritten ? screenshotRelPath : null,
                annotated: result.screenshot_annotated || null,
                annotations: annotationSummary,
                annotations_description: describeAnnotations(annotations),
                action_items: actionItems,
                affected_components: unified.affected_components || [],
                verify: verify,
                status: 'pending'
            };
            // Provenance trail for a folded merge group: surface the absorbed
            // finding ids (and an explicit count) so the coding agent sees this
            // entry stands in for many.
            if (Array.isArray(f.merged_from_ids) && f.merged_from_ids.length > 0) {
                manifestEntry.merged_from_ids = f.merged_from_ids;
                manifestEntry.merged_from_count = f.merged_from_ids.length;
            }
            manifestFindings.push(manifestEntry);

            reviewedFindings.push(result);
        }

        const manualFramesOutput = [];
        // agent_manifest manual_frames: a coding agent reading the manifest must
        // see the operator's hand-captured frames too, not just the AI findings.
        // One referential entry per manual frame, mirroring the findings schema
        // where it makes sense (id/timestamp/screenshot/annotations).
        const manualManifestFrames = [];
        let manualIndex = 0;
        for (const frame of reportState.manualFrames) {
            const reviewKey = manualFindingId(frame.marker_id);
            const annotations = reportState.findings[reviewKey]?.annotations || [];
            let screenshotFile = null;
            let screenshotDataUrl = frame.frameDataUrl || null;

            if (annotations.length > 0) {
                const tool = annotationTools.get(reviewKey);
                if (tool && typeof tool.getMergedDataURL === 'function') {
                    const merged = await tool.getMergedDataURL();
                    if (merged) {
                        screenshotDataUrl = merged;
                    }
                }
            }

            if (screenshotDataUrl && screenshotDataUrl.startsWith('data:image/')) {
                const ext = screenshotDataUrl.startsWith('data:image/png') ? 'png' : 'jpg';
                screenshotFile = `${baseName}_manual_${frame.timestamp_formatted.replace(/[:.]/g, '-')}.${ext}`;
                manualFolder.file(screenshotFile, screenshotDataUrl.split(',')[1], { base64: true });
            }

            // SF-2 (ZIP path): drop the base64 frameDataUrl that `...frame` would
            // otherwise re-leak into the bundled report_reviewed_*.json. The image
            // already ships as a real file in manual_frames/ (screenshot_file), so
            // the JSON must stay lightweight + referential — mirroring the strip in
            // exportReviewedJSON (de41ef1). Without this the ZIP's JSON balloons
            // ~100x (one frame's data URL is ~500KB) and stores each image 3x.
            const { frameDataUrl, ...frameWithoutBase64 } = frame;
            manualFramesOutput.push({
                ...frameWithoutBase64,
                annotations,
                screenshot_file: screenshotFile ? `manual_frames/${screenshotFile}` : null,
                screenshot_data_url: screenshotFile ? null : screenshotDataUrl,
            });

            // Manifest view of this manual frame: agent-readable metadata only,
            // referencing the real image file shipped above (never base64). A
            // frame that was analyzed carries the VLM summary; otherwise an
            // explicit "not AI-analyzed yet" status so the agent knows it is a
            // raw capture awaiting analysis. Annotations are surfaced both as a
            // structured array (type + verbatim text, like findings) and a
            // human-readable description (reused describeAnnotations).
            manualIndex += 1;
            const manualAnnotationSummary = annotations.map((ann) => {
                const entry = { type: ann.type };
                if (ann.type === 'text' && ann.text) entry.text = ann.text;
                return entry;
            });
            const manualSummary = frame.result?.summary || null;
            manualManifestFrames.push({
                id: 'M' + String(manualIndex).padStart(2, '0'),
                source: 'manual_capture',
                timestamp: frame.timestamp ?? null,
                timestamp_formatted: frame.timestamp_formatted || null,
                transcript: frame.transcript || '',
                notes: frame.notes || '',
                summary: manualSummary,
                status: manualSummary ? 'ai-analyzed' : 'not AI-analyzed yet',
                // R14: reflect the reviewer's priority override (badge) in the
                // agent manifest, not just the raw VLM severity. 'none' → null.
                severity: manualFrameEffectiveSeverity(frame) || null,
                issues_detected: Array.isArray(frame.result?.issues_detected)
                    ? frame.result.issues_detected
                    : [],
                screenshot: screenshotFile ? `manual_frames/${screenshotFile}` : null,
                annotations: manualAnnotationSummary,
                annotations_description: describeAnnotations(annotations),
            });
        }

        const output = {
            video: document.body.dataset.videoName,
            reviewed_at: new Date().toISOString(),
            reviewer: reportState.reviewer,
            findings: reviewedFindings,
            rejected: buildRejectedSummary(originalFindings),
            manual_frames: manualFramesOutput,
        };

        // agent_manifest.json: structured handoff for a coding agent — one entry
        // per non-rejected finding with priority, action items, and a verify
        // criterion, plus where to unpack the bundle.
        const agentManifest = {
            meta: {
                version: '1.0',
                generated_at: new Date().toISOString(),
                video: videoName,
                reviewer: reportState.reviewer,
                total_findings: manifestFindings.length,
                total_manual_frames: manualManifestFrames.length,
                unpack_to: '.screenscribe/reviews/' + baseName + '/'
            },
            findings: manifestFindings,
            manual_frames: manualManifestFrames
        };

        const reviewedJsonName = 'report_reviewed_' + baseName + '.json';
        const todoFilename = 'TODO_' + baseName + '.md';
        const todoMarkdown = buildTodoMarkdown(originalFindings, videoName, reportState.reviewer);

        zip.file(reviewedJsonName, JSON.stringify(output, null, 2));
        zip.file(todoFilename, todoMarkdown);
        zip.file('agent_manifest.json', JSON.stringify(agentManifest, null, 2));

        // Full timestamped transcript for agent context. A coding agent picking
        // up the handoff bundle benefits from reading the complete narration
        // before working through individual findings.
        const transcriptSegments = window.TRANSCRIPT_SEGMENTS || [];
        if (transcriptSegments.length > 0) {
            const transcriptLines = transcriptSegments.map(s => {
                const mm = String(Math.floor(s.start / 60)).padStart(2, '0');
                const ss = String(Math.floor(s.start % 60)).padStart(2, '0');
                return `[${mm}:${ss}] ${s.text}`;
            });
            zip.file('transcript.txt', transcriptLines.join('\n'));
        }

        // Generate and download ZIP
        const zipFilename = baseName + '_review.zip';

        const blob = await zip.generateAsync({type: 'blob'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = zipFilename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        showNotification(t('review.zipExported') + ' ' + zipFilename);

        try {
            localStorage.removeItem(stateSyncRuntime.draftKey);
        } catch (e) {}
        reportState.modified = false;
        persistSharedState();

    } catch (e) {
        console.error('ZIP export failed:', e);
        showNotification(t('review.zipError') + ' ' + e.message);
    }
}

function handleFindingSeekClick(event) {
    const el = event.target.closest?.('.finding-meta[data-timestamp]');
    if (!el) {
        return;
    }
    const seconds = parseFloat(el.dataset.timestamp);
    if (!Number.isFinite(seconds)) {
        return;
    }
    seekToTimestamp(seconds);
}

function seekToTimestamp(seconds) {
    // Static-demo sample carries no source recording; seeking from a finding card
    // must be a clean no-op (nothing to play, no error thrown).
    if (isStaticDemo()) {
        return;
    }
    if (canControlEmbeddedPlayer()) {
        window.player.seekTo(seconds);
        return;
    }

    broadcastWindowCommandWithPayload('seek-to-timestamp', {
        timestamp: seconds,
        autoplay: true,
    });
}

function exportTodoList() {
    const originalFindings = getOriginalFindingsList();
    const videoName = document.body.dataset.videoName || 'report';
    const reviewer = reportState.reviewer;
    const baseName = videoName.replace(/\.[^.]+$/, '');
    const md = buildTodoMarkdown(originalFindings, videoName, reviewer);

    // Download
    const filename = 'TODO_' + baseName + '.md';
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showNotification(t('review.exportDoneSimple') + ' ' + filename);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = String(text ?? '');
    return div.innerHTML;
}

function formatPreciseTime(seconds) {
    const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
    const m = Math.floor(safe / 60);
    const s = Math.floor(safe % 60);
    const ms = Math.floor((safe % 1) * 1000);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
}

async function fetchJsonOrThrow(response, fallbackMessage) {
    let payload = null;
    try {
        payload = await response.json();
    } catch (_error) {
        payload = null;
    }
    if (!response.ok) {
        const detail = payload && typeof payload.detail === 'string' && payload.detail.trim()
            ? payload.detail.trim()
            : fallbackMessage;
        // Append HTTP status so toast tells the reviewer whether it was a
        // client validation (4xx), upstream LibraxisAI 503, or local 500.
        throw new Error(`${detail} (HTTP ${response.status})`);
    }
    return payload || {};
}

const manualFrameRuntime = {
    currentFrame: null,
    recorder: null,
    returnFocus: null,
};

class ReviewVoiceRecorder {
    constructor(onTranscript, onStatus) {
        this.isRecording = false;
        this.isTranscribing = false;
        // Tracks whether the recognizer actually returned text for the current
        // take, so the "done" status never claims speech was added when none was.
        this.hasTranscript = false;
        this.onTranscript = onTranscript;
        this.onStatus = onStatus;
        this.transport = window.ScreenScribeLib.createSttTransport({
            onTranscript: (text) => {
                this.hasTranscript = Boolean(String(text || '').trim());
                this.onTranscript?.(text);
            },
            onStatus: (message, tone) => {
                // The transport reports a generic ready/"success" even when the
                // recognizer returned nothing. Don't tell the reviewer speech
                // was added to the note when the transcript is empty — surface a
                // distinct, non-success "no speech" status instead.
                if (tone === 'success' && !this.hasTranscript) {
                    this.onStatus?.(t('review.voiceNoSpeech'), '');
                    return;
                }
                const nextMessage = tone === 'error' ? `${message}. ${t('review.voiceMicOff')}` : message;
                this.onStatus?.(nextMessage, tone);
            },
            onTranscribingChange: (isTranscribing) => { this.isTranscribing = isTranscribing; },
            onMicError: (error) => {
                if (DEBUG) console.error('Microphone access denied:', error);
            },
            onError: (error) => {
                if (DEBUG) console.error('Manual frame transcription failed:', error);
            },
            onRecordingStart: () => {
                this.isRecording = true;
                this.hasTranscript = false;
                this.onStatus?.(t('review.voiceRecording'), 'busy');
            },
            statusTranscribing: t('review.voiceTranscribing'),
            statusReady: `${t('review.voiceReady')}. ${t('review.voiceMicOff')}`,
            fallbackMessage: 'Voice transcription failed.',
        });
    }

    async start() {
        const started = await this.transport.start();
        if (!started) showNotification(t('review.voiceDenied'));
        return started;
    }

    stop() {
        this.transport.stop();
        this.isRecording = this.transport.isRecording;
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

function setManualFrameTranscript(text) {
    const transcriptEl = document.getElementById('manualFrameTranscript');
    if (!transcriptEl) return;
    const value = String(text || '').trim();
    if (value) {
        transcriptEl.textContent = value;
        transcriptEl.classList.remove('empty');
    } else {
        transcriptEl.textContent = t('media.manualFrameNoSpoken');
        transcriptEl.classList.add('empty');
    }
}

// Read back what the user actually said/typed for the manual frame. When nothing
// was recorded, setManualFrameTranscript fills the element with the
// "no spoken description" placeholder and tags it `.empty`; that placeholder must
// never be persisted as real transcript text (it would leak into the saved
// report and the server session). Mirrors Analyze's has-text guard.
function readManualFrameTranscript() {
    const transcriptEl = document.getElementById('manualFrameTranscript');
    if (!transcriptEl || transcriptEl.classList.contains('empty')) return '';
    return transcriptEl.textContent || '';
}

function setManualFrameStatus(message, tone = '') {
    const statusEl = document.getElementById('manualFrameAnalysisStatus');
    if (!statusEl) return;
    statusEl.textContent = message;
    statusEl.classList.remove('busy', 'error', 'success');
    if (tone) {
        statusEl.classList.add(tone);
    }
}

function openManualFrameModal(frame) {
    manualFrameRuntime.currentFrame = frame;

    const modal = document.getElementById('manualFrameModal');
    const preview = document.getElementById('manualFramePreview');
    const timestampEl = document.getElementById('manualFrameTimestamp');
    const notesInput = document.getElementById('manualFrameNotes');
    const micStatus = document.getElementById('manualFrameMicStatus');

    if (!modal || !preview || !timestampEl || !notesInput) return;

    preview.src = frame.frameDataUrl;
    timestampEl.textContent = formatPreciseTime(frame.timestamp);
    notesInput.value = '';
    setManualFrameTranscript('');
    setManualFrameStatus(t('media.manualFrameReady'));
    if (micStatus) micStatus.textContent = '';

    manualFrameRuntime.returnFocus = document.activeElement;
    modal.hidden = false;
    document.body.classList.add('modal-open');
    // Trap focus inside the dialog and land focus on the dialog panel so the
    // first Tab moves through the modal's controls, not the page behind it.
    const dialog = modal.querySelector('.manual-frame-dialog') || modal;
    if (modal.__focusTrapHandler) {
        modal.removeEventListener('keydown', modal.__focusTrapHandler);
    }
    modal.__focusTrapHandler = trapFocus(modal);
    const firstFocusable = getFocusable(dialog)[0] || notesInput;
    if (firstFocusable) firstFocusable.focus();
}

function closeManualFrameModal() {
    const modal = document.getElementById('manualFrameModal');
    if (!modal) return;

    manualFrameRuntime.recorder?.destroy();
    manualFrameRuntime.currentFrame = null;
    modal.hidden = true;
    document.body.classList.remove('modal-open');
    if (modal.__focusTrapHandler) {
        modal.removeEventListener('keydown', modal.__focusTrapHandler);
        modal.__focusTrapHandler = null;
    }
    // Return focus to the marker/trigger that opened the modal.
    const returnFocus = manualFrameRuntime.returnFocus;
    if (returnFocus && typeof returnFocus.focus === 'function') {
        returnFocus.focus();
    }
    manualFrameRuntime.returnFocus = null;
}

function upsertManualFrame(frame) {
    const existingIndex = reportState.manualFrames.findIndex(
        (entry) => entry.marker_id === frame.marker_id
    );
    if (existingIndex >= 0) {
        reportState.manualFrames[existingIndex] = frame;
    } else {
        reportState.manualFrames.unshift(frame);
    }
    reportState.modified = true;
    flushSharedStateSync();
}

// The manual frame's effective DISPLAY priority (R14). A reviewer override
// (frame.severity, set via the per-card priority <select>) wins over the
// VLM-assigned result severity, mirroring Analyze's marker override. The
// explicit 'none' clears the priority, so it is not a displayable severity and
// collapses to '' (no badge, no export tag). Shared by the card badge, the TODO
// markdown, and the ZIP manifest so all three agree on what the reviewer set.
function manualFrameEffectiveSeverity(frame) {
    const override = frame.severity;
    if (override === 'none') return '';
    if (override) return override;
    // A model-provided (or mirrored) 'none' is not a displayable severity either
    // -- collapse it here too so the badge/TODO/manifest never render a stray
    // localized "NONE" against the "all three agree" contract above.
    const modelSeverity = frame.result?.severity;
    return modelSeverity === 'none' ? '' : (modelSeverity || '');
}

// Keeps the header tab counter in sync with manual moment add/remove, so
// "Momenty (N)" never lags behind the "Ręczne momenty" panel count.
function updateFindingsTabCount() {
    const tabCount = document.getElementById('findings-count');
    if (!tabCount) return;
    if (aiFindingsCount === null) {
        aiFindingsCount = parseInt(tabCount.textContent, 10) || 0;
    }
    tabCount.textContent = String(aiFindingsCount + reportState.manualFrames.length);
}

function renderManualFrames() {
    updateFindingsTabCount();

    const section = document.getElementById('manualFindingsSection');
    const list = document.getElementById('manualFindingsList');
    const count = document.getElementById('manualFindingsCount');
    if (!section || !list || !count) return;

    count.textContent = String(reportState.manualFrames.length);
    section.hidden = reportState.manualFrames.length === 0;

    if (reportState.manualFrames.length === 0) {
        list.replaceChildren();
        return;
    }

    list.innerHTML = reportState.manualFrames.map((frame) => {
        // R14: badge reflects the effective priority (override wins; 'none'
        // clears it → no badge), mirroring Analyze. Show the localized severity
        // word, not the raw English token: a bare "MEDIUM"/"NONE" in the PL UI
        // reads as an i18n leak. Unknown tokens fall back to the raw value rather
        // than printing the missing key.
        const badgeSeverity = manualFrameEffectiveSeverity(frame);
        let severityBadge = '';
        if (badgeSeverity) {
            const severityKey = 'review.' + badgeSeverity;
            const severityRaw = t(severityKey);
            const severityLabel = severityRaw === severityKey ? badgeSeverity : severityRaw;
            severityBadge = `<span class="manual-frame-badge severity-${escapeHtml(badgeSeverity)}">${escapeHtml(severityLabel)}</span>`;
        }
        // The <select> reflects the raw override (incl. the explicit 'none' that
        // clears it), falling back to the VLM result severity when unset — so the
        // control shows the operator exactly what the effective priority is.
        const selectSeverity = frame.severity || frame.result?.severity || '';
        const severityOptions = [
            ['none', 'review.manualFrameNoPriority'],
            ['critical', 'review.critical'],
            ['high', 'review.high'],
            ['medium', 'review.medium'],
            ['low', 'review.low'],
        ].map(([value, key]) => {
            const sel = value === selectSeverity ? ' selected' : '';
            return `<option value="${escapeHtml(value)}"${sel}>${escapeHtml(t(key))}</option>`;
        }).join('');
        const findingId = manualFindingId(frame.marker_id);
        const markerIdAttr = escapeHtml(frame.marker_id);
        const transcript = frame.transcript ? `<div class="manual-frame-body">${escapeHtml(frame.transcript)}</div>` : '';
        const notes = frame.notes ? `<div class="manual-frame-notes-copy">${escapeHtml(frame.notes)}</div>` : '';
        const issues = Array.isArray(frame.result?.issues_detected) && frame.result.issues_detected.length > 0
            ? `<div class="manual-frame-notes-copy">${escapeHtml(frame.result.issues_detected.join('; '))}</div>`
            : '';
        // R13: the "no AI summary" fallback must read as an empty state, not as
        // real reviewer/AI content. Without a result, tag the body `.empty` so it
        // renders muted+italic (the same empty-state language the modal's
        // .manual-frame-transcript.empty uses), instead of styling the placeholder
        // identically to a genuine summary.
        const summaryText = frame.result?.summary;
        const summaryBody = summaryText
            ? `<div class="manual-frame-body">${escapeHtml(summaryText)}</div>`
            : `<div class="manual-frame-body empty">${escapeHtml(t('review.noSummary'))}</div>`;
        // Inline note editor (R12). A manual note is no longer write-once: the
        // reviewer can reopen it, edit the text, and persist the change through
        // the same path a fresh capture uses (updateManualFrameMarker -> PATCH
        // /api/manual-mark/{id}). Hidden until "Edit note" toggles it, mirroring
        // Analyze's per-marker note editor. Regenerated on every render, so the
        // textarea always reflects the current server-backed notes value.
        const noteEditor = `
            <div class="manual-frame-note-editor" id="manual-note-editor-${markerIdAttr}" data-manual-marker-id="${markerIdAttr}">
                <textarea id="manual-note-input-${markerIdAttr}"
                          placeholder="${escapeHtml(t('media.manualFrameNotesPlaceholder'))}">${escapeHtml(frame.notes || '')}</textarea>
                <div class="manual-frame-note-editor-buttons">
                    <button type="button" class="secondary" data-action="cancel-note-manual" data-manual-marker-id="${markerIdAttr}">${escapeHtml(t('review.manualFrameNoteCancel'))}</button>
                    <button type="button" class="primary" data-action="save-note-manual" data-manual-marker-id="${markerIdAttr}">${escapeHtml(t('review.manualFrameNoteSave'))}</button>
                </div>
            </div>`;
        return `
            <article class="manual-frame-item" data-manual-marker-id="${markerIdAttr}">
                <div class="manual-frame-preview-card">
                    <div class="annotation-container" data-finding-id="${escapeHtml(findingId)}">
                        <img class="thumbnail"
                             src="${escapeHtml(frame.frameDataUrl)}"
                             data-full="${escapeHtml(frame.frameDataUrl)}"
                             alt="${escapeHtml(t('media.manualFrameTitle', { ts: frame.timestamp_formatted }))}"
                             title="${escapeHtml(t('media.manualFrameZoomTitle'))}">
                        <svg class="annotation-svg"></svg>
                        <div class="annotation-hint">${escapeHtml(t('media.manualFrameAnnotateHint'))}</div>
                    </div>
                </div>
                <div class="manual-frame-content">
                    <div class="manual-frame-meta">
                        <span class="manual-frame-title">${escapeHtml(t('media.manualFrameTitle', { ts: frame.timestamp_formatted }))}</span>
                        ${severityBadge}
                        <button type="button" class="manual-frame-edit-note"
                                data-action="edit-note-manual"
                                data-manual-marker-id="${markerIdAttr}">${escapeHtml(t('review.manualFrameEditNote'))}</button>
                        <button type="button" class="manual-frame-delete"
                                data-action="delete-manual-frame"
                                data-manual-marker-id="${markerIdAttr}"
                                aria-label="${escapeHtml(t('review.manualFrameDelete'))}"
                                title="${escapeHtml(t('review.manualFrameDelete'))}">&times;</button>
                    </div>
                    ${summaryBody}
                    ${transcript}
                    ${notes}
                    ${issues}
                    <div class="manual-frame-priority">
                        <label class="manual-frame-priority-label" for="manual-severity-select-${markerIdAttr}">${escapeHtml(t('review.changePriority'))}</label>
                        <select class="manual-frame-severity-select" id="manual-severity-select-${markerIdAttr}" data-manual-marker-id="${markerIdAttr}">
                            ${severityOptions}
                        </select>
                    </div>
                    ${noteEditor}
                </div>
            </article>
        `;
    }).join('');

    bindThumbnailClicks(list);
    initAnnotationTools();
}

// Remove a manual frame the reviewer added by mistake. A manual frame is no
// longer a write-once capture: the reviewer can drop it from the review and
// re-add a corrected one. Removal is durable across reload because (a) the
// local draft is flushed synchronously without the frame and (b) the server
// session — the owner of the frame pixels — is told to forget it, so a cold
// load cannot resurrect it.
async function deleteManualFrame(markerId) {
    const id = String(markerId);
    const exists = reportState.manualFrames.some((entry) => String(entry.marker_id) === id);
    if (!exists) return;
    if (!confirm(t('review.manualFrameDeleteConfirm'))) return;

    reportState.manualFrames = reportState.manualFrames.filter(
        (entry) => String(entry.marker_id) !== id
    );
    // Drop the frame's review row too, so a deleted frame leaves no orphaned
    // verdict/notes/annotations behind in state, exports, or the next render.
    delete reportState.findings[manualFindingId(id)];
    reportState.modified = true;
    renderManualFrames();
    flushSharedStateSync();

    // Best-effort server purge; failure only means a cold load (no local draft)
    // could re-show the frame, never that local state is wrong.
    try {
        await fetch(`/api/manual-mark/${encodeURIComponent(id)}`, { method: 'DELETE' });
    } catch (error) {
        if (DEBUG) console.debug('Manual frame server delete failed:', error);
    }
    showNotification(t('review.manualFrameDeleted'));
}

// BH28: push an edited transcript/notes to the server marker after it was first
// marked. The local state is updated regardless of the round-trip (the reviewer
// keeps their edit in-session); a server failure only means a cold reload may
// fall back to the previously persisted text, never that the UI is wrong.
async function updateManualFrameMarker(markerId, transcript, notes) {
    const id = String(markerId);
    const existing = reportState.manualFrames.find(
        (frame) => String(frame.marker_id) === id
    );
    // Nothing changed -> no need for a server round-trip or a re-render.
    if (existing && existing.transcript === transcript && existing.notes === notes) {
        return;
    }

    // NEW-06: distinguish a transient NETWORK failure (fetch throws) from a
    // server REJECTION (4xx/5xx). A network blip should not lose the reviewer's
    // in-session edit, so we still apply it locally (a later cold reload may fall
    // back to the persisted text). But a 4xx/5xx means the server refused the
    // edit — applying it locally anyway silently desyncs the card from the
    // marker, so surface the failure and leave state untouched.
    let serverRejected = false;
    try {
        const response = await fetch(`/api/manual-mark/${encodeURIComponent(id)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ transcript, notes }),
        });
        if (!response.ok) serverRejected = true;
    } catch (error) {
        if (DEBUG) console.debug('Manual frame update failed:', error);
    }

    if (serverRejected) {
        showNotification(t('review.manualFrameSaveFailed'));
        return;
    }

    if (existing) {
        upsertManualFrame({ ...existing, transcript, notes });
        renderManualFrames();
    }
}

// R14: persist a manual priority override for a manual frame and re-render so
// the badge + select agree. The override wins over the VLM-assigned severity;
// the explicit 'none' clears it. Mirrors Analyze's changeMarkerSeverity, plus
// the server mirror onto any existing result. On a failed round-trip the change
// is NOT applied locally (a fail-silent local upsert would desync the client
// from the server — NEW-06 class); the reviewer is told and the re-render
// restores the select to the last persisted value.
async function changeManualFrameSeverity(markerId, severity) {
    const id = String(markerId);
    const existing = reportState.manualFrames.find(
        (frame) => String(frame.marker_id) === id
    );
    if (!existing) return;

    let ok = false;
    try {
        const response = await fetch(`/api/manual-mark/${encodeURIComponent(id)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ severity }),
        });
        ok = response.ok;
    } catch (error) {
        if (DEBUG) console.debug('Manual frame priority change failed:', error);
    }

    if (!ok) {
        showNotification(t('review.manualFrameSaveFailed'));
        renderManualFrames();  // revert the <select> to the persisted value
        return;
    }

    // Mirror the server: the override lands on the frame, and onto any existing
    // result so the badge + export severity agree with the operator's call.
    const updated = { ...existing, severity };
    if (updated.result) {
        updated.result = { ...updated.result, severity };
    }
    upsertManualFrame(updated);
    renderManualFrames();
}

// R12: open/close the inline note editor on a manual-frame card. Mirrors
// Analyze's toggleNoteEditor — the editor is hidden by default so the card stays
// compact, and focus lands in the textarea when it opens.
function toggleManualNoteEditor(markerId) {
    const editor = document.getElementById('manual-note-editor-' + markerId);
    if (!editor) return;
    editor.classList.toggle('active');
    if (editor.classList.contains('active')) {
        const input = document.getElementById('manual-note-input-' + markerId);
        if (input) input.focus();
    }
}

// R12: discard an in-progress note edit. The textarea is regenerated from the
// current frame on the next renderManualFrames(), so closing the editor is the
// only reset needed here.
function cancelManualNote(markerId) {
    const editor = document.getElementById('manual-note-editor-' + markerId);
    if (editor) editor.classList.remove('active');
}

// R12: persist an edited note. The note travels the same path a fresh capture's
// note does (updateManualFrameMarker -> PATCH /api/manual-mark/{id} + local
// state + re-render), so the edit is durable across reload and export. Only the
// note changes; the transcript is preserved verbatim.
async function saveManualNote(markerId) {
    const input = document.getElementById('manual-note-input-' + markerId);
    if (!input) return;
    const existing = reportState.manualFrames.find(
        (frame) => String(frame.marker_id) === String(markerId)
    );
    if (!existing) return;
    await updateManualFrameMarker(markerId, existing.transcript || '', input.value);
}

// Persist the captured frame WITHOUT running VLM analysis. Mirrors the
// ANALYZE sidebar: marking is the durable act, analysis is optional and
// separate. Returns the marker_id so analyze can reuse an already-marked frame.
async function markManualFrame(current, transcript, notes) {
    if (current.marker_id) {
        // BH28: the frame is already marked, but the reviewer may have edited its
        // notes/transcript afterwards (e.g. typed more, then hit Analyze). Without
        // an update path those edits lived only in transient modal state and were
        // dropped on reload — and a later analyze ran against the stale server
        // copy. Persist the edit to the server marker so the change is durable.
        await updateManualFrameMarker(current.marker_id, transcript, notes);
        return current.marker_id;
    }
    // BH10 in-flight idempotency: Add and Analyze can both call markManualFrame on
    // the same frame before the first POST resolves (the marker_id guard above
    // only catches calls arriving AFTER it resolves). Reuse the in-flight POST
    // so two concurrent callers create one marker, not a duplicate.
    if (current._markInFlight) return current._markInFlight;
    current._markInFlight = (async () => {
        try {
            const markResponse = await fetch('/api/manual-mark', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    timestamp: current.timestamp,
                    frame_base64: current.frameBase64,
                    transcript,
                    notes,
                }),
            });
            const markPayload = await fetchJsonOrThrow(markResponse, t('review.manualFrameSaveFailed'));
            current.marker_id = markPayload.marker_id;
            upsertManualFrame({
                marker_id: markPayload.marker_id,
                timestamp: current.timestamp,
                timestamp_formatted: formatPreciseTime(current.timestamp),
                transcript,
                notes,
                frameDataUrl: current.frameDataUrl,
                result: null,
            });
            renderManualFrames();
            return markPayload.marker_id;
        } finally {
            // Clear the latch so a later retry (after a failure) can run; on
            // success current.marker_id now short-circuits re-entry anyway.
            current._markInFlight = null;
        }
    })();
    return current._markInFlight;
}

async function addManualFrame() {
    const current = manualFrameRuntime.currentFrame;
    if (!current) return;

    const transcript = readManualFrameTranscript();
    const notes = document.getElementById('manualFrameNotes')?.value || '';
    const addBtn = document.getElementById('manualFrameAddBtn');

    try {
        if (addBtn) addBtn.disabled = true;
        setManualFrameStatus(t('review.statusSavingFrame'), 'busy');
        await markManualFrame(current, transcript, notes);
        showNotification(t('review.manualFrameAdded'));
        closeManualFrameModal();
    } catch (error) {
        if (DEBUG) console.error('Manual frame save failed:', error);
        const message = error instanceof Error ? error.message : t('review.manualFrameSaveFailed');
        setManualFrameStatus(message, 'error');
    } finally {
        if (addBtn) addBtn.disabled = false;
    }
}

async function analyzeManualFrame() {
    const current = manualFrameRuntime.currentFrame;
    if (!current) return;

    const transcript = readManualFrameTranscript();
    const notes = document.getElementById('manualFrameNotes')?.value || '';
    const analyzeBtn = document.getElementById('manualFrameAnalyzeBtn');

    try {
        if (analyzeBtn) analyzeBtn.disabled = true;
        setManualFrameStatus(t('review.statusSavingFrame'), 'busy');

        // Reuse an already-marked frame; otherwise mark it first so analyze
        // never silently discards the capture.
        const markerId = await markManualFrame(current, transcript, notes);

        setManualFrameStatus(t('review.statusRunningAnalysis'), 'busy');
        const analyzeResponse = await fetch(`/api/manual-analyze/${markerId}`, {
            method: 'POST',
        });
        const analyzePayload = await fetchJsonOrThrow(analyzeResponse, t('review.manualFrameAnalysisFailed'));

        if (analyzePayload.status !== 'completed' || !analyzePayload.result) {
            throw new Error(analyzePayload.error || t('review.manualFrameAnalysisFailed'));
        }

        upsertManualFrame({
            marker_id: markerId,
            timestamp: current.timestamp,
            timestamp_formatted: formatPreciseTime(current.timestamp),
            transcript,
            notes,
            frameDataUrl: current.frameDataUrl,
            result: analyzePayload.result,
        });
        renderManualFrames();
        setManualFrameStatus(t('review.manualFrameAnalysisComplete'), 'success');
        showNotification(t('review.manualFrameAnalyzed'));
        closeManualFrameModal();

        const findingsTab = document.querySelector('.tab-btn[data-tab="findings"]');
        findingsTab?.click();
    } catch (error) {
        if (DEBUG) console.error('Manual frame analysis failed:', error);
        const message = error instanceof Error ? error.message : 'Manual frame analysis failed.';
        setManualFrameStatus(message, 'error');
    } finally {
        if (analyzeBtn) analyzeBtn.disabled = false;
    }
}

// Guard the mic button against a mousedown->mouseup race. recorder.start() is
// async, so a fast press-and-release can fire mouseup (stop) before start()
// resolves. Without serialization the stop runs against a not-yet-started
// recorder while the later-resolving start leaves the mic recording forever
// (mic stuck on, stream never released). The controller guarantees exactly one
// start and one stop, and a release that arrives mid-start is deferred until
// start resolves.
function createMicPressController(recorder, onRecordingChange) {
    let startPromise = null;
    let stopRequested = false;

    async function press() {
        if (startPromise) return; // already pressing; ignore re-entry
        stopRequested = false;
        startPromise = Promise.resolve(recorder.start());
        let started = false;
        try {
            started = await startPromise;
        } finally {
            startPromise = null;
        }
        if (started && !stopRequested) {
            onRecordingChange?.(true);
        }
        // A release that arrived while start() was pending was deferred — honor
        // it now that the recorder has actually started.
        if (stopRequested) {
            stopRequested = false;
            recorder.stop();
            onRecordingChange?.(false);
        }
    }

    function release() {
        if (startPromise) {
            // start() still in flight — defer the stop until it resolves so we
            // never stop a recorder that has not started yet.
            stopRequested = true;
            return;
        }
        recorder.stop();
        onRecordingChange?.(false);
    }

    return { press, release };
}

function initManualFrameCapture() {
    const micBtn = document.getElementById('manualFrameMicBtn');
    const addBtn = document.getElementById('manualFrameAddBtn');
    const analyzeBtn = document.getElementById('manualFrameAnalyzeBtn');

    manualFrameRuntime.recorder = new ReviewVoiceRecorder(
        (text) => setManualFrameTranscript(text),
        (message, tone) => {
            const micStatus = document.getElementById('manualFrameMicStatus');
            if (micStatus) {
                micStatus.textContent = message;
                micStatus.dataset.tone = tone || '';
            }
            setManualFrameStatus(message, tone);
        }
    );

    document.addEventListener('screenscribe:capture-frame', (event) => {
        openManualFrameModal(event.detail);
    });

    // Delegate manual-frame card actions on the stable list container so the
    // bindings survive every renderManualFrames() innerHTML replacement. One
    // listener dispatches delete + the R12 note-editor toggle/save/cancel.
    const manualList = document.getElementById('manualFindingsList');
    manualList?.addEventListener('click', (event) => {
        const actionBtn = event.target.closest?.('[data-action]');
        if (!actionBtn || !manualList.contains(actionBtn)) return;
        const markerId = actionBtn.dataset.manualMarkerId;
        switch (actionBtn.dataset.action) {
            case 'delete-manual-frame':
                event.preventDefault();
                void deleteManualFrame(markerId);
                return;
            case 'edit-note-manual':
                event.preventDefault();
                toggleManualNoteEditor(markerId);
                return;
            case 'cancel-note-manual':
                event.preventDefault();
                cancelManualNote(markerId);
                return;
            case 'save-note-manual':
                event.preventDefault();
                void saveManualNote(markerId);
                return;
            default:
                return;
        }
    });

    // R14: per-card priority <select> fires a delegated 'change' on the same
    // stable container, so it survives every renderManualFrames() replacement.
    manualList?.addEventListener('change', (event) => {
        const select = event.target;
        if (!select || !select.matches?.('.manual-frame-severity-select')) return;
        const markerId = select.dataset.manualMarkerId;
        const severity = select.value;
        if (markerId && severity) {
            void changeManualFrameSeverity(markerId, severity);
        }
    });

    document.querySelectorAll('[data-action="close-manual-frame"]').forEach((button) => {
        button.addEventListener('click', closeManualFrameModal);
    });

    if (micBtn) {
        const micController = createMicPressController(
            manualFrameRuntime.recorder,
            (recording) => micBtn.classList.toggle('recording', recording)
        );
        micBtn.addEventListener('mousedown', () => { void micController.press(); });
        micBtn.addEventListener('mouseup', () => { void micController.release(); });
        micBtn.addEventListener('mouseleave', () => {
            if (manualFrameRuntime.recorder?.isRecording) {
                void micController.release();
            }
        });
    }

    addBtn?.addEventListener('click', addManualFrame);
    analyzeBtn?.addEventListener('click', analyzeManualFrame);
}

function showNotification(msg) {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = msg;
    document.body.appendChild(toast);

    setTimeout(() => {
        if (toast.parentNode) toast.remove();
    }, 3000);
}

const voiceNoteRuntime = {
    recognition: null,
    activeButton: null,
    activeFindingId: null,
};

function getSpeechRecognitionCtor() {
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function setVoiceNoteUi(button, findingId, recording, statusText = '') {
    if (!button) return;
    button.classList.toggle('recording', recording);
    button.textContent = recording
        ? `🎤 ${t('review.voiceRecording')}`
        : `🎤 ${t('review.voiceNote')}`;

    const statusEl = document.querySelector(`.notes-mic-status[data-finding-id="${findingId}"]`);
    if (statusEl) {
        statusEl.textContent = statusText;
    }
}

function appendVoiceTextToNotes(findingId, text) {
    const article = document.querySelector(`[data-finding-id="${findingId}"]`);
    if (!article) return;
    const textarea = article.querySelector('.notes textarea');
    if (!textarea) return;

    const sanitized = String(text || '').trim();
    if (!sanitized) return;

    textarea.value = textarea.value.trim()
        ? `${textarea.value.trim()}\n${sanitized}`
        : sanitized;

    if (!reportState.findings[findingId]) {
        reportState.findings[findingId] = createDefaultFindingState();
    }
    reportState.findings[findingId].notes = textarea.value;
    reportState.modified = true;
    scheduleSharedStateSync();
}

function stopVoiceNoteCapture() {
    if (!voiceNoteRuntime.recognition) return;
    try {
        voiceNoteRuntime.recognition.stop();
    } catch (e) {
        // noop
    }
}

function startVoiceNoteCapture(button, findingId) {
    const Recognition = getSpeechRecognitionCtor();
    if (!Recognition) {
        showNotification(t('review.voiceNotSupported'));
        return;
    }

    // Stop previous capture if another note is active
    if (voiceNoteRuntime.activeButton && voiceNoteRuntime.activeButton !== button) {
        stopVoiceNoteCapture();
    }

    const recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = currentLang === 'pl' ? 'pl-PL' : 'en-US';

    voiceNoteRuntime.recognition = recognition;
    voiceNoteRuntime.activeButton = button;
    voiceNoteRuntime.activeFindingId = findingId;

    setVoiceNoteUi(button, findingId, true, t('review.voiceRecording'));

    recognition.onresult = (event) => {
        const finalChunks = [];
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
            const result = event.results[i];
            if (result.isFinal && result[0] && result[0].transcript) {
                finalChunks.push(result[0].transcript);
            }
        }
        if (finalChunks.length > 0) {
            appendVoiceTextToNotes(findingId, finalChunks.join(' ').trim());
            setVoiceNoteUi(button, findingId, true, t('review.voiceReady'));
        }
    };

    recognition.onerror = (event) => {
        const message = event.error === 'not-allowed'
            ? t('review.voiceDenied')
            : `${t('review.voiceError')}: ${event.error}`;
        setVoiceNoteUi(button, findingId, false, message);
        showNotification(message);
    };

    recognition.onend = () => {
        // Always reset THIS recognizer's own button UI (safe — captured in the
        // closure). But only clear the shared runtime if this recognizer is
        // still the active one: a newer session may have already replaced it,
        // and a late onend from the old recognizer must not wipe the new
        // session's recognition/button/findingId references.
        setVoiceNoteUi(button, findingId, false, '');
        if (voiceNoteRuntime.recognition === recognition) {
            voiceNoteRuntime.recognition = null;
            voiceNoteRuntime.activeButton = null;
            voiceNoteRuntime.activeFindingId = null;
        }
    };

    recognition.start();
}

function initVoiceNotes() {
    document.querySelectorAll('.notes-mic-btn[data-action="voice-note"]').forEach((button) => {
        button.addEventListener('click', () => {
            const findingId = button.dataset.findingId;
            if (!findingId) return;

            if (voiceNoteRuntime.activeButton === button && voiceNoteRuntime.recognition) {
                stopVoiceNoteCapture();
                return;
            }
            startVoiceNoteCapture(button, findingId);
        });
    });
}

// Tab switching. Implements the ARIA tablist keyboard pattern: click or
// Enter/Space activates, ArrowLeft/ArrowRight (plus Home/End) roves focus to
// the sibling tab and activates it.
// Delegated export/save actions (CSP-ready: replaces inline onclick on the
// sidebar-footer save button and the Export-tab TODO/JSON/ZIP buttons). The
// handler bodies are unchanged — only the wiring moved from HTML to JS.
function initExportActions() {
    const handlers = {
        'save-review': saveReviewToDisk,
        'export-todo': exportTodoList,
        'export-json': exportReviewedJSON,
        'export-zip': exportReviewedZIP,
    };
    document.addEventListener('click', (event) => {
        const el = event.target.closest('[data-action]');
        if (!el) return;
        const handler = handlers[el.dataset.action];
        if (handler) handler(event);
    });
}

function initTabs() {
    const tabBtns = Array.from(document.querySelectorAll('.tab-btn'));
    tabBtns.forEach((btn) => {
        btn.addEventListener('click', () => {
            activateTab(btn.dataset.tab);
        });
    });
    window.ScreenScribeLib?.initTabKeyboard(tabBtns, (_btn, tabId) => activateTab(tabId));
}

function initWindowActions() {
    document.getElementById('detachReviewBtn')?.addEventListener('click', openReviewWindow);
    document.getElementById('attachWorkspaceBtn')?.addEventListener('click', reattachWorkspace);
    updateWindowActionButtons();
}

function initSidebarResize() {
    const resizer = document.getElementById('sidebarResizer');
    const sidebar = document.querySelector('.sidebar');
    if (!resizer || !sidebar) return;

    const storageKey = 'screenscribe_sidebar_width';
    let dragState = null;

    const isMobileLayout = () => window.matchMedia('(max-width: 900px)').matches;

    const getResizeBounds = () => {
        const styles = getComputedStyle(document.documentElement);
        const minPx = parseFloat(styles.getPropertyValue('--sidebar-min')) || 320;
        const preferredMax = parseFloat(styles.getPropertyValue('--sidebar-max')) || 720;
        const viewportMax = Math.max(minPx, window.innerWidth - 360);
        return {
            minPx,
            maxPx: Math.max(minPx, Math.min(preferredMax, viewportMax))
        };
    };

    const applySidebarWidth = (width, persist = true) => {
        if (isMobileLayout()) return;
        const { minPx, maxPx } = getResizeBounds();
        const nextWidth = Math.min(maxPx, Math.max(minPx, width));
        document.documentElement.style.setProperty('--sidebar-width', `${nextWidth}px`);
        resizer.setAttribute('aria-valuenow', String(Math.round(nextWidth)));
        if (persist) {
            try {
                localStorage.setItem(storageKey, String(Math.round(nextWidth)));
            } catch (error) {
                console.debug('Failed to persist sidebar width', error);
            }
        }
    };

    try {
        const savedWidth = Number(localStorage.getItem(storageKey));
        if (savedWidth) {
            applySidebarWidth(savedWidth, false);
        }
    } catch (error) {
        console.debug('Failed to restore sidebar width', error);
    }

    // Keyboard resize: the separator is focusable and Arrow keys nudge the
    // width; Home/End jump to the max/min bound (WCAG 2.1.1 keyboard access).
    const KEY_STEP = 24;
    resizer.tabIndex = 0;
    resizer.setAttribute('aria-valuemin', '0');
    resizer.setAttribute('aria-valuemax', '100');
    resizer.addEventListener('keydown', (event) => {
        if (isMobileLayout()) return;
        const { minPx, maxPx } = getResizeBounds();
        const current = sidebar.getBoundingClientRect().width;
        let next = null;
        if (event.key === 'ArrowLeft' || event.key === 'ArrowDown') next = current + KEY_STEP;
        else if (event.key === 'ArrowRight' || event.key === 'ArrowUp') next = current - KEY_STEP;
        else if (event.key === 'Home') next = maxPx;
        else if (event.key === 'End') next = minPx;
        if (next === null) return;
        event.preventDefault();
        applySidebarWidth(next);
    });

    resizer.addEventListener('pointerdown', (event) => {
        if (isMobileLayout()) return;
        dragState = {
            startX: event.clientX,
            startWidth: sidebar.getBoundingClientRect().width
        };
        document.body.classList.add('is-resizing');
        if (resizer.setPointerCapture) {
            resizer.setPointerCapture(event.pointerId);
        }
        event.preventDefault();
    });

    window.addEventListener('pointermove', (event) => {
        if (!dragState) return;
        const delta = dragState.startX - event.clientX;
        applySidebarWidth(dragState.startWidth + delta);
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
        if (isMobileLayout()) {
            document.body.classList.remove('is-resizing');
            return;
        }
        applySidebarWidth(sidebar.getBoundingClientRect().width, false);
    });
}



let currentLang = getInitialReportLanguage();

function setLanguage(lang, { persist = true } = {}) {
    if (!hasI18nLanguage(lang)) return;
    currentLang = lang;

    // Update toggle buttons
    document.querySelectorAll('.lang-toggle button').forEach(btn => {
        const isActive = btn.dataset.lang === lang;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });

    applyTranslations(document);

    // Update tab buttons with count preservation
    document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
        const tab = btn.dataset.tab;
        if (tab === 'summary') btn.textContent = t('review.summary');
        if (tab === 'export') btn.textContent = t('review.export');
        if (tab === 'findings') {
            const count = btn.textContent.match(/\((\d+)\)/);
            btn.textContent = t('review.findings') + (count ? ` (${count[1]})` : '');
        }
    });

    if (persist) {
        persistLanguagePreference(lang);
    }

    if (document.querySelector('.finding')) {
        updateReviewMeta();
    }
}

function initLanguage() {
    const preferredLanguage = window.ScreenScribeLib?.getInitialLanguage([
        () => stateSyncRuntime.langKey ? localStorage.getItem(stateSyncRuntime.langKey) : null,
        getInitialReportLanguage(),
        'pl',
    ]) || getInitialReportLanguage();

    setLanguage(preferredLanguage, { persist: false });

    window.ScreenScribeLib?.wireLanguageToggle(
        document.querySelector('.lang-toggle'),
        { order: ['en', 'pl'] },
        setLanguage
    );
}

// =============================================================================
// LIGHTBOX ANNOTATION TOOL (for fullscreen drawing)
// =============================================================================

const SVG_NS = 'http://www.w3.org/2000/svg';

// Contrast halo baked into SVG presentation attributes (NOT CSS), so it
// survives PNG export: the annotated export serializes the SVG to a blob and
// rasterizes it on a canvas, and an external stylesheet does not travel with a
// rasterized blob. A dark halo under a light/coloured fill keeps annotations
// legible on ANY background (light Finder shots and dark UIs alike).
const ANNOTATION_HALO_COLOR = 'rgba(0, 0, 0, 0.85)';
// Text halo width as a fraction of font size (scales with px or rel units).
const ANNOTATION_TEXT_HALO_RATIO = 0.14;
// Shape under-stroke width relative to the coloured stroke (peeks out as edge).
const ANNOTATION_SHAPE_HALO_MULT = 2.4;

// Build a stroked shape with a darker, wider under-stroke beneath the coloured
// stroke so thin light shapes stay readable on any background.
function makeContrastShape(tag, geom, stroke, strokeWidth) {
    const group = document.createElementNS(SVG_NS, 'g');
    group.appendChild(createSvgElement(tag, {
        ...geom,
        stroke: ANNOTATION_HALO_COLOR,
        'stroke-width': strokeWidth * ANNOTATION_SHAPE_HALO_MULT,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round'
    }));
    group.appendChild(createSvgElement(tag, {
        ...geom,
        stroke,
        'stroke-width': strokeWidth,
        'stroke-linejoin': 'round',
        'stroke-linecap': 'round'
    }));
    return group;
}

function createSvgElement(tag, attrs = {}) {
    const el = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs).forEach(([key, value]) => {
        if (value !== undefined && value !== null) {
            el.setAttribute(key, value);
        }
    });
    el.classList.add('annotation-shape');
    return el;
}

// Calculate actual visible image rect accounting for object-fit: contain
// Returns the rect of the actual image content, not the IMG element box
function getActualImageRect(img) {
    const elemRect = img.getBoundingClientRect();
    const naturalW = img.naturalWidth || img.width;
    const naturalH = img.naturalHeight || img.height;

    if (!naturalW || !naturalH || !elemRect.width || !elemRect.height) {
        return elemRect;
    }

    const elemAspect = elemRect.width / elemRect.height;
    const imgAspect = naturalW / naturalH;

    let actualWidth, actualHeight, offsetX, offsetY;

    if (imgAspect > elemAspect) {
        // Image is wider than container - letterboxing (bars top/bottom)
        actualWidth = elemRect.width;
        actualHeight = elemRect.width / imgAspect;
        offsetX = 0;
        offsetY = (elemRect.height - actualHeight) / 2;
    } else {
        // Image is taller than container - pillarboxing (bars left/right)
        actualHeight = elemRect.height;
        actualWidth = elemRect.height * imgAspect;
        offsetX = (elemRect.width - actualWidth) / 2;
        offsetY = 0;
    }

    return {
        left: elemRect.left + offsetX,
        top: elemRect.top + offsetY,
        width: actualWidth,
        height: actualHeight,
        right: elemRect.left + offsetX + actualWidth,
        bottom: elemRect.top + offsetY + actualHeight
    };
}

function normalizeRect(ann) {
    const x1 = ann.x;
    const y1 = ann.y;
    const x2 = ann.x + ann.width;
    const y2 = ann.y + ann.height;
    const left = Math.min(x1, x2);
    const top = Math.min(y1, y2);
    const right = Math.max(x1, x2);
    const bottom = Math.max(y1, y2);
    return {
        x: left,
        y: top,
        width: right - left,
        height: bottom - top
    };
}

function readAnnotationDefaultColor() {
    // Neutral monochrome annotation default, read from the theme token so the
    // monochrome foundation stays single-source. Falls back to a neutral literal
    // (NOT a saturated hue) for standalone exports rendered without the theme CSS.
    try {
        const token = getComputedStyle(document.documentElement)
            .getPropertyValue('--annotation-color-default')
            .trim();
        if (token) return token;
    } catch (error) {
        console.debug('readAnnotationDefaultColor: token read failed', error);
    }
    // White default fill: paired with the baked dark halo it is legible on any
    // background (standalone exports rendered without the theme stylesheet).
    return '#ffffff';
}

function createAnnotationElement(ann) {
    if (!ann) return null;
    const stroke = ann.color || readAnnotationDefaultColor();
    // Use strokeWidthPx for export (denormalized) or strokeWidthRel for live rendering
    const strokeWidth = ann.strokeWidthPx || Math.max(0.0005, ann.strokeWidthRel || 0.003);
    // Arrow head length: use pixels if available, otherwise normalized
    const headLenNorm = ann.strokeWidthPx ? Math.max(20, ann.strokeWidthPx * 3) : 0.02;

    if (ann.type === 'rect') {
        const rectData = normalizeRect(ann);
        return makeContrastShape('rect', {
            x: rectData.x,
            y: rectData.y,
            width: rectData.width,
            height: rectData.height,
            fill: 'none'
        }, stroke, strokeWidth);
    }

    if (ann.type === 'pen' && Array.isArray(ann.points) && ann.points.length >= 1) {
        const d = ann.points.map((p, idx) => `${idx === 0 ? 'M' : 'L'}${p.x},${p.y}`).join(' ');
        return makeContrastShape('path', {
            d,
            fill: 'none'
        }, stroke, strokeWidth);
    }

    if (ann.type === 'arrow') {
        const dx = ann.endX - ann.startX;
        const dy = ann.endY - ann.startY;
        const len = Math.sqrt(dx * dx + dy * dy) || 0.001;
        // Head length: 15-25% of arrow length, minimum based on stroke width
        const minHead = strokeWidth * 3;
        const headLength = Math.max(minHead, Math.min(len * 0.25, len * 0.15 + minHead));
        const angle = Math.atan2(dy, dx);
        const hx1 = ann.endX - headLength * Math.cos(angle - Math.PI / 7);
        const hy1 = ann.endY - headLength * Math.sin(angle - Math.PI / 7);
        const hx2 = ann.endX - headLength * Math.cos(angle + Math.PI / 7);
        const hy2 = ann.endY - headLength * Math.sin(angle + Math.PI / 7);

        const group = document.createElementNS(SVG_NS, 'g');
        const lineGeom = { x1: ann.startX, y1: ann.startY, x2: ann.endX, y2: ann.endY, fill: 'none' };
        const headGeom = {
            d: `M${ann.endX},${ann.endY} L${hx1},${hy1} M${ann.endX},${ann.endY} L${hx2},${hy2}`,
            fill: 'none'
        };
        const underWidth = strokeWidth * ANNOTATION_SHAPE_HALO_MULT;
        // Dark, wider under-strokes first (line + head), then the coloured pair
        // on top, so the arrow reads on any background and survives PNG export.
        group.appendChild(createSvgElement('line', {
            ...lineGeom, stroke: ANNOTATION_HALO_COLOR, 'stroke-width': underWidth, 'stroke-linecap': 'round'
        }));
        group.appendChild(createSvgElement('path', {
            ...headGeom, stroke: ANNOTATION_HALO_COLOR, 'stroke-width': underWidth,
            'stroke-linecap': 'round', 'stroke-linejoin': 'round'
        }));
        group.appendChild(createSvgElement('line', {
            ...lineGeom, stroke, 'stroke-width': strokeWidth, 'stroke-linecap': 'round'
        }));
        group.appendChild(createSvgElement('path', {
            ...headGeom, stroke, 'stroke-width': strokeWidth,
            'stroke-linecap': 'round', 'stroke-linejoin': 'round'
        }));
        return group;
    }

    if (ann.type === 'text' && ann.text) {
        const fontSize = ann.fontSizePx || ann.fontSizeRel || 0.036;
        // Halo as SVG presentation attributes (paint-order: stroke draws the
        // dark halo UNDER the fill) so the contrast survives PNG export. The
        // user-picked colour (or the white default) stays the fill, so it reads
        // on light AND dark backgrounds. Stroke width scales with the font.
        const textEl = createSvgElement('text', {
            x: ann.x,
            y: ann.y,
            fill: stroke,
            stroke: ANNOTATION_HALO_COLOR,
            'stroke-width': fontSize * ANNOTATION_TEXT_HALO_RATIO,
            'paint-order': 'stroke',
            'stroke-linejoin': 'round',
            'font-size': fontSize,
            'font-family': 'system-ui, -apple-system, sans-serif',
            'font-weight': ann.fontWeight || '700',
            'dominant-baseline': 'hanging',
        });
        textEl.textContent = ann.text;
        textEl.classList.add('annotation-text');
        return textEl;
    }

    return null;
}

function renderAnnotationsToSvg(svg, annotations, renderWidth = 1, renderHeight = 1) {
    if (!svg || !renderWidth || !renderHeight) return;
    while (svg.firstChild) {
        svg.firstChild.remove();
    }
    svg.setAttribute('viewBox', `0 0 ${renderWidth} ${renderHeight}`);
    // Use 'none' to stretch normalized (0-1) coordinates to full container dimensions
    // 'meet' would preserve aspect ratio causing position drift on resize
    svg.setAttribute('preserveAspectRatio', 'none');

    const group = document.createElementNS(SVG_NS, 'g');
    (annotations || []).forEach(ann => {
        const el = createAnnotationElement(ann);
        if (el) group.appendChild(el);
    });
    svg.appendChild(group);
}

function serializeAnnotationsToSvg(annotations, baseWidth, baseHeight) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('xmlns', SVG_NS);
    svg.setAttribute('viewBox', `0 0 ${baseWidth} ${baseHeight}`);
    svg.setAttribute('width', baseWidth);
    svg.setAttribute('height', baseHeight);
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    renderAnnotationsToSvg(svg, annotations, baseWidth, baseHeight);
    const serializer = new XMLSerializer();
    return serializer.serializeToString(svg);
}

function denormalizeAnnotations(annotations, targetWidth, targetHeight) {
    if (!annotations || !targetWidth || !targetHeight) return [];
    const scale = Math.max(targetWidth, targetHeight);
    return annotations.map(ann => {
        const strokeWidthPx = (ann.strokeWidthRel || 0.003) * scale;
        if (ann.type === 'rect') {
            return {
                ...ann,
                x: (ann.x || 0) * targetWidth,
                y: (ann.y || 0) * targetHeight,
                width: (ann.width || 0) * targetWidth,
                height: (ann.height || 0) * targetHeight,
                strokeWidthPx
            };
        }
        if (ann.type === 'arrow') {
            return {
                ...ann,
                startX: (ann.startX || 0) * targetWidth,
                startY: (ann.startY || 0) * targetHeight,
                endX: (ann.endX || 0) * targetWidth,
                endY: (ann.endY || 0) * targetHeight,
                strokeWidthPx
            };
        }
        if (ann.type === 'pen' && Array.isArray(ann.points)) {
            return {
                ...ann,
                points: ann.points.map(p => ({
                    x: (p.x || 0) * targetWidth,
                    y: (p.y || 0) * targetHeight
                })),
                strokeWidthPx
            };
        }
        if (ann.type === 'text') {
            return {
                ...ann,
                x: (ann.x || 0) * targetWidth,
                y: (ann.y || 0) * targetHeight,
                fontSizePx: (ann.fontSizeRel || 0.036) * Math.max(targetWidth, targetHeight),
                strokeWidthPx,
            };
        }
        return { ...ann, strokeWidthPx };
    });
}

function drawSvgMarkupOnCanvas(ctx, svgMarkup, width, height) {
    return new Promise((resolve, reject) => {
        const blob = new Blob([svgMarkup], { type: 'image/svg+xml' });
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
            try {
                ctx.drawImage(img, 0, 0, width, height);
                URL.revokeObjectURL(url);
                resolve();
            } catch (e) {
                URL.revokeObjectURL(url);
                reject(e);
            }
        };
        img.onerror = (e) => {
            URL.revokeObjectURL(url);
            reject(e);
        };
        img.src = url;
    });
}

async function annotationsToPng(annotations, baseWidth, baseHeight) {
    if (!baseWidth || !baseHeight) return null;
    const canvas = document.createElement('canvas');
    canvas.width = baseWidth;
    canvas.height = baseHeight;
    const ctx = canvas.getContext('2d');
    try {
        const annPx = denormalizeAnnotations(annotations, baseWidth, baseHeight);
        const svgMarkup = serializeAnnotationsToSvg(annPx, baseWidth, baseHeight);
        await drawSvgMarkupOnCanvas(ctx, svgMarkup, baseWidth, baseHeight);
        return canvas.toDataURL('image/png');
    } catch (e) {
        console.warn('annotationsToPng failed:', e);
        return null;
    }
}

async function mergeImageAndAnnotations(imgEl, annotations) {
    if (!imgEl || !annotations || annotations.length === 0) return null;
    const baseWidth = imgEl.naturalWidth || imgEl.videoWidth || imgEl.width || 1920;
    const baseHeight = imgEl.naturalHeight || imgEl.videoHeight || imgEl.height || 1080;
    const annPixels = denormalizeAnnotations(annotations, baseWidth, baseHeight);

    const canvas = document.createElement('canvas');
    canvas.width = baseWidth;
    canvas.height = baseHeight;
    const ctx = canvas.getContext('2d');

    try {
        if (!imgEl.complete && imgEl.decode) {
            await imgEl.decode();
        }
        ctx.drawImage(imgEl, 0, 0, baseWidth, baseHeight);
    } catch (e) {
        console.warn('mergeImageAndAnnotations: base image draw failed, using annotations only', e);
        return await annotationsToPng(annotations, baseWidth, baseHeight);
    }

    try {
        const svgMarkup = serializeAnnotationsToSvg(annPixels, baseWidth, baseHeight);
        await drawSvgMarkupOnCanvas(ctx, svgMarkup, baseWidth, baseHeight);
    } catch (e) {
        console.warn('mergeImageAndAnnotations: overlay draw failed', e);
    }

    try {
        return canvas.toDataURL('image/png');
    } catch (e) {
        console.warn('mergeImageAndAnnotations: toDataURL failed, fallback to annotations-only', e);
        return await annotationsToPng(annotations, baseWidth, baseHeight);
    }
}

class LightboxAnnotationTool {
    constructor(svg, img, toolbar, findingId) {
        this.svg = svg;
        this.img = img;
        this.toolbar = toolbar;
        this.findingId = findingId;

        this.tool = null;
        this.color = readAnnotationDefaultColor();
        // Stroke width in viewBox units (0-1), ~0.8% of image dimension
        this.strokeWidth = 0.008;
        this.isDrawing = false;
        this.startX = 0;
        this.startY = 0;
        this.annotations = [];
        this.currentPath = [];
        this.draftEl = null;
        this.textDraft = null;
        this.baseWidth = img.naturalWidth || img.width || 1920;
        this.baseHeight = img.naturalHeight || img.height || 1080;
        this.resizeObserver = null;
        this.boundHandlers = [];

        this.init();
    }

    init() {
        this.syncOverlaySize();
        this.bindEvents();
        this.loadAnnotations();
        renderAnnotationsToSvg(this.svg, this.annotations, 1, 1);
        // Don't auto-select tool - user must click toolbar to start drawing
        // This prevents accidental annotations when just viewing
    }

    bindEvents() {
        // Tool selection
        this.toolbar.querySelectorAll('.tool-btn').forEach(btn => {
            const handler = (e) => {
                e.stopPropagation();
                this.selectTool(btn.dataset.tool);
            };
            btn.addEventListener('click', handler);
            this.boundHandlers.push({ target: btn, event: 'click', handler });
        });

        // Color picker
        const colorPicker = this.toolbar.querySelector('.color-picker');
        // Keep the picker's swatch in sync with the token-derived default so the
        // HTML literal mirror and the logical default never diverge.
        if (colorPicker) colorPicker.value = this.color;
        const colorClick = (e) => e.stopPropagation();
        // One colour source of truth (this.color) for ALL annotation types.
        // Recolour any in-progress draft live so the picker affects the
        // annotation being placed identically for shapes and text.
        const colorInput = (e) => { this.color = e.target.value; this.applyColorToActiveDraft(); };
        colorPicker.addEventListener('click', colorClick);
        colorPicker.addEventListener('input', colorInput);
        this.boundHandlers.push({ target: colorPicker, event: 'click', handler: colorClick });
        this.boundHandlers.push({ target: colorPicker, event: 'input', handler: colorInput });

        // Undo/Clear
        const undoBtn = this.toolbar.querySelector('.undo-btn');
        const clearBtn = this.toolbar.querySelector('.clear-btn');
        const undoHandler = (e) => { e.stopPropagation(); this.undo(); };
        const clearHandler = (e) => { e.stopPropagation(); this.clear(); };
        undoBtn.addEventListener('click', undoHandler);
        clearBtn.addEventListener('click', clearHandler);
        this.boundHandlers.push({ target: undoBtn, event: 'click', handler: undoHandler });
        this.boundHandlers.push({ target: clearBtn, event: 'click', handler: clearHandler });

        // Drawing events (pointer)
        const start = (e) => this.startDraw(e);
        const move = (e) => this.draw(e);
        const end = (e) => this.endDraw(e);
        this.svg.addEventListener('pointerdown', start);
        this.svg.addEventListener('pointermove', move);
        window.addEventListener('pointerup', end);
        this.boundHandlers.push({ target: this.svg, event: 'pointerdown', handler: start });
        this.boundHandlers.push({ target: this.svg, event: 'pointermove', handler: move });
        this.boundHandlers.push({ target: window, event: 'pointerup', handler: end });

        // Resize observer to keep overlay in sync with image size
        this.resizeObserver = new ResizeObserver(() => {
            this.syncOverlaySize();
            // Always render with normalized coordinates (1, 1) - CSS transform handles scaling
            renderAnnotationsToSvg(this.svg, this.annotations, 1, 1);
        });
        this.resizeObserver.observe(this.img);
    }

    selectTool(tool) {
        this.tool = this.tool === tool ? null : tool;
        this.toolbar.querySelectorAll('.tool-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tool === this.tool);
        });
        this.svg.classList.toggle('drawing', this.tool !== null);
    }

    syncOverlaySize() {
        // Use actual image rect to account for object-fit: contain
        const imgRect = getActualImageRect(this.img);
        const containerRect = this.img.parentElement.getBoundingClientRect();
        const offsetX = imgRect.left - containerRect.left;
        const offsetY = imgRect.top - containerRect.top;
        // Position SVG over the actual visible image area
        // viewBox is set by renderAnnotationsToSvg to 0 0 1 1 (normalized coordinates)
        this.svg.style.width = `${this.baseWidth}px`;
        this.svg.style.height = `${this.baseHeight}px`;
        this.svg.style.left = `${offsetX}px`;
        this.svg.style.top = `${offsetY}px`;
        const scaleX = imgRect.width / this.baseWidth;
        const scaleY = imgRect.height / this.baseHeight;
        this.svg.style.transformOrigin = 'top left';
        this.svg.style.transform = `scale(${scaleX}, ${scaleY})`;
    }

    getPosPct(e) {
        // Use actual image rect to account for object-fit: contain
        const rect = getActualImageRect(this.img);
        // Clamp to 0-1 range to prevent out-of-bounds annotations
        const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
        return { x, y, w: rect.width, h: rect.height };
    }

    startDraw(e) {
        if (!this.tool) return;
        e.stopPropagation();
        const pos = this.getPosPct(e);
        this.startX = pos.x;
        this.startY = pos.y;
        this.startRectWidth = pos.w;
        this.startRectHeight = pos.h;
        if (this.tool === 'text') {
            // Inline, non-blocking text entry. A blocking native prompt locked
            // out the toolbar colour picker and gave no live colour preview, so
            // text colour could not be picked/changed "in the moment" like a
            // shape. The inline draft keeps the picker live (applyColorToActiveDraft).
            this.beginTextDraft(pos);
            return;
        }

        this.isDrawing = true;
        this.svg.setPointerCapture(e.pointerId);
        if (this.tool === 'pen') {
            this.currentPath = [pos];
            this.draftEl = createAnnotationElement({
                type: 'pen',
                points: [{ x: pos.x, y: pos.y }],
                color: this.color,
                strokeWidthRel: this.strokeWidth
            });
        } else if (this.tool === 'rect') {
            this.draftEl = createAnnotationElement({
                type: 'rect',
                x: this.startX,
                y: this.startY,
                width: 0,
                height: 0,
                color: this.color,
                strokeWidthRel: this.strokeWidth
            });
        } else if (this.tool === 'arrow') {
            this.draftEl = createAnnotationElement({
                type: 'arrow',
                startX: this.startX,
                startY: this.startY,
                endX: this.startX,
                endY: this.startY,
                color: this.color,
                strokeWidthRel: this.strokeWidth
            });
        }
        if (this.draftEl) {
            this.draftEl.classList.add('draft');
            this.svg.appendChild(this.draftEl);
        }
    }

    draw(e) {
        if (!this.isDrawing || !this.tool || !this.draftEl) return;
        e.stopPropagation();
        const pos = this.getPosPct(e);

        // H1 made createAnnotationElement return a <g> with separate halo +
        // foreground shapes, so mutating draftEl (the group) or only its first two
        // children (both halo) left the coloured preview behind during a drag.
        // Rebuild the draft from the live geometry via the SAME factory the final
        // render uses, so BOTH halo and foreground shapes inside the group follow
        // the cursor and the colour — and the preview matches the committed shape.
        let ann = null;
        if (this.tool === 'pen') {
            this.currentPath.push(pos);
            ann = {
                type: 'pen',
                points: this.currentPath.map(p => ({ x: p.x, y: p.y })),
                color: this.color,
                strokeWidthRel: this.strokeWidth
            };
        } else if (this.tool === 'rect') {
            const rectData = normalizeRect({
                x: this.startX,
                y: this.startY,
                width: pos.x - this.startX,
                height: pos.y - this.startY
            });
            ann = {
                type: 'rect',
                x: rectData.x,
                y: rectData.y,
                width: rectData.width,
                height: rectData.height,
                color: this.color,
                strokeWidthRel: this.strokeWidth
            };
        } else if (this.tool === 'arrow') {
            ann = {
                type: 'arrow',
                startX: this.startX,
                startY: this.startY,
                endX: pos.x,
                endY: pos.y,
                color: this.color,
                strokeWidthRel: this.strokeWidth
            };
        }
        if (!ann) return;

        const next = createAnnotationElement(ann);
        if (!next) return;
        next.classList.add('draft');
        if (this.draftEl.parentNode) {
            this.draftEl.parentNode.replaceChild(next, this.draftEl);
        } else {
            this.svg.appendChild(next);
        }
        this.draftEl = next;
    }

    endDraw(e) {
        if (!this.isDrawing || !this.tool) return;
        if (e) {
            e.stopPropagation();
            if (e.pointerId) {
                this.svg.releasePointerCapture(e.pointerId);
            }
        }

        const pos = e ? this.getPosPct(e) : { x: this.startX, y: this.startY, w: this.startRectWidth, h: this.startRectHeight };
        const strokeWidthRel = this.strokeWidth;

        if (this.tool === 'pen' && this.currentPath.length > 1) {
            const normPoints = this.currentPath.map(p => ({ x: p.x, y: p.y }));
            this.annotations.push({
                type: 'pen',
                points: normPoints,
                color: this.color,
                strokeWidthRel
            });
        } else if (this.tool === 'rect') {
            const w = pos.x - this.startX;
            const h = pos.y - this.startY;
            // Minimum size: 1% of image dimension (coordinates are normalized 0-1)
            if (Math.abs(w) > 0.01 && Math.abs(h) > 0.01) {
                this.annotations.push({
                    type: 'rect',
                    x: this.startX,
                    y: this.startY,
                    width: w,
                    height: h,
                    color: this.color,
                    strokeWidthRel
                });
            }
        } else if (this.tool === 'arrow') {
            const dx = pos.x - this.startX;
            const dy = pos.y - this.startY;
            // Minimum length: 2% of image diagonal (coordinates are normalized 0-1)
            if (Math.sqrt(dx*dx + dy*dy) > 0.02) {
                this.annotations.push({
                    type: 'arrow',
                    startX: this.startX,
                    startY: this.startY,
                    endX: pos.x,
                    endY: pos.y,
                    color: this.color,
                    strokeWidthRel
                });
            }
        }

        this.isDrawing = false;
        this.currentPath = [];
        if (this.draftEl && this.draftEl.parentNode) {
            this.draftEl.parentNode.removeChild(this.draftEl);
        }
        this.draftEl = null;
        renderAnnotationsToSvg(this.svg, this.annotations, 1, 1);
    }

    undo() {
        this.annotations.pop();
        renderAnnotationsToSvg(this.svg, this.annotations, 1, 1);
    }

    clear() {
        this.annotations = [];
        renderAnnotationsToSvg(this.svg, [], 1, 1);
    }

    // Open a non-blocking inline text draft at the clicked position. The draft
    // <text> previews the picked colour and an overlaid <input> captures the
    // string; the toolbar colour picker stays live (parity with shape tools).
    beginTextDraft(pos) {
        this.cancelTextDraft();
        const draftEl = createAnnotationElement({
            type: 'text',
            x: pos.x,
            y: pos.y,
            text: '​', // zero-width placeholder so the element renders
            color: this.color,
            fontSizeRel: 0.036,
        });
        if (draftEl) {
            draftEl.classList.add('draft');
            this.svg.appendChild(draftEl);
        }
        const input = this.createTextInput(pos);
        this.textDraft = { pos, el: draftEl, input };
        return this.textDraft;
    }

    createTextInput(pos) {
        const doc = (this.svg && this.svg.ownerDocument) || document;
        const input = doc.createElement('input');
        input.type = 'text';
        input.className = 'annotation-text-input';
        input.setAttribute('aria-label', 'Text annotation');
        // Position over the clicked image point.
        try {
            const rect = getActualImageRect(this.img);
            input.style.position = 'fixed';
            input.style.left = `${rect.left + pos.x * rect.width}px`;
            input.style.top = `${rect.top + pos.y * rect.height}px`;
            input.style.zIndex = '10002';
            input.style.color = this.color;
        } catch (error) {
            console.debug('createTextInput: positioning failed', error);
        }
        // The draft only becomes commit/cancel-eligible AFTER it is focused at
        // the end of the opening gesture. A real mouse gesture is pointerdown
        // (creates the input) -> pointerup (pulls focus back), so a SYNCHRONOUS
        // focus() here is immediately blurred by that pointerup: onBlur then
        // commits an empty field and deletes the draft before the operator can
        // type a single character ("no field to type in"). `ready` ignores that
        // one instant blur; a blur AFTER real focus still commits/cancels as
        // before ("click elsewhere after typing = commit").
        let ready = false;
        const onInput = () => {
            if (this.textDraft && this.textDraft.el) {
                this.textDraft.el.textContent = input.value || '​';
                this.textDraft.el.setAttribute('fill', this.color);
            }
        };
        const onKey = (e) => {
            e.stopPropagation();
            if (e.key === 'Enter') { e.preventDefault(); this.commitTextDraft(input.value); }
            else if (e.key === 'Escape') { e.preventDefault(); this.cancelTextDraft(); }
        };
        const onBlur = () => {
            if (!ready) return; // instant blur from the opening gesture: keep the field alive
            this.commitTextDraft(input.value);
        };
        input.addEventListener('input', onInput);
        input.addEventListener('keydown', onKey);
        input.addEventListener('blur', onBlur);
        const host = (this.svg && this.svg.parentElement) || doc.body;
        if (host && host.appendChild) host.appendChild(input);
        // Defer focus past the current pointer gesture so pointerup cannot steal
        // it (synchronous focus here caused the instant-blur-commit regression).
        const arm = () => {
            if (typeof input.focus === 'function') {
                try { input.focus(); } catch (error) { console.debug('createTextInput: focus failed', error); }
            }
            ready = true;
        };
        if (typeof requestAnimationFrame === 'function') {
            requestAnimationFrame(arm);
        } else {
            setTimeout(arm, 0);
        }
        return input;
    }

    removeTextInput(input) {
        if (input && input.parentNode && input.parentNode.removeChild) {
            input.parentNode.removeChild(input);
        }
    }

    commitTextDraft(value) {
        const draft = this.textDraft;
        if (!draft) return;
        this.textDraft = null;
        this.removeTextInput(draft.input);
        if (draft.el && draft.el.parentNode) {
            draft.el.parentNode.removeChild(draft.el);
        }
        const text = (value || '').trim();
        if (text) {
            this.annotations.push({
                type: 'text',
                x: draft.pos.x,
                y: draft.pos.y,
                text,
                color: this.color,
                fontSizeRel: 0.036,
            });
        }
        renderAnnotationsToSvg(this.svg, this.annotations, 1, 1);
    }

    cancelTextDraft() {
        const draft = this.textDraft;
        if (!draft) return;
        this.textDraft = null;
        this.removeTextInput(draft.input);
        if (draft.el && draft.el.parentNode) {
            draft.el.parentNode.removeChild(draft.el);
        }
    }

    // Recolour the in-progress draft from this.color so the toolbar picker
    // affects the annotation being placed. Shape drafts already recolour live
    // in draw(); this carries the same affordance to the inline text draft.
    applyColorToActiveDraft() {
        if (this.textDraft && this.textDraft.el && typeof this.textDraft.el.setAttribute === 'function') {
            this.textDraft.el.setAttribute('fill', this.color);
        }
    }

    saveAnnotations() {
        if (!this.findingId) return;
        if (!reportState.findings[this.findingId]) {
            reportState.findings[this.findingId] = createDefaultFindingState();
        }
        reportState.findings[this.findingId].annotations = [...this.annotations];
        reportState.modified = true;
        scheduleSharedStateSync();
    }

    loadAnnotations() {
        if (!this.findingId) return;
        const state = reportState.findings[this.findingId];
        if (state && state.annotations) {
            this.annotations = [...state.annotations];
        }
    }

    destroy() {
        this.cancelTextDraft();
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
        }
        this.boundHandlers.forEach(({ target, event, handler }) => {
            target.removeEventListener(event, handler);
        });
        this.boundHandlers = [];
        if (this.svg) {
            while (this.svg.firstChild) {
                this.svg.firstChild.remove();
            }
            this.svg.classList.remove('drawing');
        }
    }
}

// =============================================================================
// THUMBNAIL ANNOTATION TOOL (display-only, drawing happens in lightbox)
// =============================================================================

class AnnotationPreview {
    constructor(container) {
        this.container = container;
        this.findingId = container.dataset.findingId;
        this.img = container.querySelector('.thumbnail');
        this.svg = container.querySelector('.annotation-svg');
        this.annotations = [];
        this.baseWidth = 0;
        this.baseHeight = 0;
        this.resizeObserver = null;

        this.init();
    }

    init() {
        const onReady = () => {
            this.baseWidth = this.img.naturalWidth || this.img.width || 1920;
            this.baseHeight = this.img.naturalHeight || this.img.height || 1080;
            this.syncSvgSize();
            this.loadAnnotations();
            this.render();
            this.observe();
        };

        if (this.img.complete) {
            onReady();
        } else {
            this.img.onload = onReady;
        }
    }

    observe() {
        this.resizeObserver = new ResizeObserver(() => {
            this.syncSvgSize();
            this.render();
        });
        this.resizeObserver.observe(this.img);
    }

    syncSvgSize() {
        // Use actual image rect to account for object-fit: contain
        const imgRect = getActualImageRect(this.img);
        const containerRect = this.img.parentElement.getBoundingClientRect();
        const offsetX = imgRect.left - containerRect.left;
        const offsetY = imgRect.top - containerRect.top;
        // Position SVG over the actual visible image area
        // viewBox is set by render() to 0 0 1 1 (normalized coordinates)
        this.svg.style.width = `${this.baseWidth}px`;
        this.svg.style.height = `${this.baseHeight}px`;
        this.svg.style.left = `${offsetX}px`;
        this.svg.style.top = `${offsetY}px`;
        const scaleX = imgRect.width / this.baseWidth;
        const scaleY = imgRect.height / this.baseHeight;
        this.svg.style.transformOrigin = 'top left';
        this.svg.style.transform = `scale(${scaleX}, ${scaleY})`;
    }

    loadAnnotations() {
        const review = reportState.findings[this.findingId];
        if (review && review.annotations && review.annotations.length > 0) {
            this.annotations = [...review.annotations];
            this.container.classList.add('has-annotations');
        } else {
            this.annotations = [];
            this.container.classList.remove('has-annotations');
        }
    }

    render() {
        if (!this.baseWidth || !this.baseHeight) return;
        renderAnnotationsToSvg(this.svg, this.annotations, 1, 1);
    }

    refreshFromState() {
        this.loadAnnotations();
        this.render();
    }

    async getMergedDataURL() {
        this.loadAnnotations();
        return await mergeImageAndAnnotations(this.img, this.annotations);
    }

    destroy() {
        if (this.resizeObserver) {
            this.resizeObserver.disconnect();
        }
    }
}

// Global annotation tools map
const annotationTools = new Map();

function initAnnotationTools() {
    document.querySelectorAll('.annotation-container').forEach(container => {
        // An absorbed/hidden finding must not own the annotation tool keyed by its
        // id — the visible merged survivor card shares that id and is the live
        // surface. Skipping hidden owners lets the merged card win the key.
        const ownerFinding = container.closest('.finding');
        if (ownerFinding && (ownerFinding.dataset.mergedAway === 'true' || ownerFinding.hidden)) {
            return;
        }
        const findingId = container.dataset.findingId;
        const existing = annotationTools.get(findingId);
        if (existing && existing.container !== container) {
            existing.destroy();
            annotationTools.delete(findingId);
        }
        if (!annotationTools.has(findingId)) {
            annotationTools.set(findingId, new AnnotationPreview(container));
        } else {
            annotationTools.get(findingId)?.refreshFromState();
        }
    });

    annotationTools.forEach((preview, findingId) => {
        if (!document.body.contains(preview.container)) {
            preview.destroy();
            annotationTools.delete(findingId);
        }
    });
}

document.addEventListener('DOMContentLoaded', () => {
    initReviewState();
    initTabs();
    initWindowActions();
    initExportActions();
    initLanguage();
    initSidebarResize();
    initAnnotationTools();
    initManualFrameCapture();
    initVoiceNotes();
    window.addEventListener('beforeunload', () => stopVoiceNoteCapture());
});
