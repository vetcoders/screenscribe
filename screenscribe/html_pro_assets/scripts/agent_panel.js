// Floating Screenscribe agent chat for the HTML review report.
// Separate from review_app.js so W1-01/W1-05 can merge with one player hook.
// SSE contract (w1-04): token / tool_call / tool_result / done / error. `token`
// frames are provider deltas rendered as they arrive; `: keepalive` comment
// frames only prove the turn is still alive.
(function (root) {
    'use strict';

    var STORAGE_KEY = 'screenscribe:agent-panel';
    var STREAM_URL = '/api/agent/chat/stream';
    var PANEL_WIDTH = 360;
    var PANEL_HEIGHT = 480;
    var MIN_WIDTH = 300;
    var MIN_HEIGHT = 320;
    var MAX_WIDTH = 960;
    var MAX_HEIGHT = 1200;
    var RESIZE_STEP = 24;
    var SIZE_PRESETS = [
        { id: 'compact', width: 320, height: 420 },
        { id: 'default', width: PANEL_WIDTH, height: PANEL_HEIGHT },
        { id: 'large', width: 560, height: 720 },
    ];
    // No byte or keepalive for this long means the turn is dead, not slow: the
    // server emits a keepalive every ~10 s while the model is still working.
    var IDLE_TIMEOUT_MS = 60000;
    var MIC_MAX_SECONDS = 300;
    var COMPOSER_MAX_HEIGHT = 200;
    var FAB_SIZE = 56;
    var GAP = 16;
    var VIEW_MARGIN = 8;
    var SHEET_CLASS = 'ss-agent-sheet';
    // Screenshots attached to one message (paste, drop or the image button).
    // Large captures are downscaled so a Retina screenshot stays readable but
    // the request stays a few hundred KB.
    // Mirror of the backend request contract (agent/chat.py): more history
    // items than _MAX_HISTORY_ITEMS or longer fields than
    // _MAX_HISTORY_FIELD_CHARS get a 422 before the agent even starts.
    var HISTORY_MAX_ITEMS = 50;
    var HISTORY_MAX_FIELD_CHARS = 16000;
    var MAX_ATTACHMENTS = 4;
    var ATTACH_MAX_SIDE = 2048;
    var ATTACH_KEEP_BYTES = 1500000;
    var ATTACH_JPEG_QUALITY = 0.9;

    var state = {
        collapsed: true,
        left: null,
        top: null,
        sheet: false,
        history: [],
        previousResponseId: null,
        previousResponseIdentity: null,
        streaming: false,
        drag: null,
        playerObserver: null,
        patchOfflineAnnounced: false,
        retryMessage: null,
        retryRow: null,
        threadReady: null,
        referenceBound: false,
        width: PANEL_WIDTH,
        height: PANEL_HEIGHT,
        resize: null,
        abort: null,
        stopRequested: false,
        idleTimer: null,
        timedOut: false,
        retryRows: [],
        mic: { status: 'idle', transport: null, timer: null, startedAt: 0, message: '' },
        attachments: [],
        retryImages: [],
        autosend: false,
        threadFingerprint: null,
    };

    function tx(key, args) {
        var fn = typeof t === 'function' ? t : root.t;
        if (typeof fn === 'function') {
            return fn(key, args);
        }
        return key;
    }

    function clearResponseCursor() {
        state.previousResponseId = null;
        state.previousResponseIdentity = null;
    }

    function acceptDoneCursor(data, previousIdentity) {
        var payload = data && typeof data === 'object' ? data : {};
        var responseId = typeof payload.response_id === 'string' ? payload.response_id.trim() : '';
        var provider = typeof payload.provider === 'string' ? payload.provider.trim() : '';
        var protocol = typeof payload.protocol === 'string' ? payload.protocol.trim() : '';
        var host = typeof payload.host === 'string' ? payload.host.trim().toLowerCase() : '';
        if (!responseId || provider !== 'primary' || protocol !== 'responses' || !host) {
            clearResponseCursor();
            return false;
        }
        if (previousIdentity && (
            previousIdentity.provider !== provider
            || previousIdentity.protocol !== protocol
            || previousIdentity.host !== host
        )) {
            clearResponseCursor();
            return false;
        }
        clearResponseCursor();
        state.previousResponseId = responseId;
        state.previousResponseIdentity = { provider: provider, protocol: protocol, host: host };
        return true;
    }

    function storageKey() {
        var loc = root.location || {};
        return STORAGE_KEY + ':' + String(loc.origin || '') + String(loc.pathname || '');
    }

    function contentHash(text) {
        // djb2: tiny, stable, no crypto dependency; this is a bucket key, not a secret.
        var hash = 5381;
        for (var i = 0; i < text.length; i += 1) {
            hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
        }
        return hash.toString(16);
    }

    function threadFingerprint() {
        // Two different reports can share origin+pathname (same port, same
        // report filename, different directory). The conversation must follow
        // the REPORT, not the URL, or one recording's chat resurfaces - and
        // gets resent to the model - under another recording.
        if (state.threadFingerprint) return state.threadFingerprint;
        var parts = [];
        try {
            var doc = root.document;
            parts.push(String(doc.title || ''));
            var island = doc.getElementById ? doc.getElementById('original-findings') : null;
            var findings = island && island.textContent ? String(island.textContent) : '';
            parts.push(String(findings.length));
            parts.push(findings.slice(0, 400));
            var segments = root.TRANSCRIPT_SEGMENTS;
            if (Array.isArray(segments) && segments.length) {
                var first = segments[0] || {};
                var last = segments[segments.length - 1] || {};
                parts.push(String(segments.length));
                parts.push(String(first.start) + ':' + String(first.text || '').slice(0, 120));
                parts.push(String(last.end) + ':' + String(last.text || '').slice(0, 120));
            }
            var video = doc.getElementById ? doc.getElementById('videoPlayer') : null;
            if (video && typeof video.getAttribute === 'function') {
                parts.push(String(video.getAttribute('src') || ''));
            }
        } catch (_err) { /* a partial fingerprint still beats a URL-only key */ }
        state.threadFingerprint = contentHash(parts.join('|'));
        return state.threadFingerprint;
    }

    function threadKey() {
        return storageKey() + ':' + threadFingerprint();
    }

    function persist() {
        try {
            root.localStorage.setItem(storageKey(), JSON.stringify({
                collapsed: state.collapsed,
                left: state.sheet ? null : state.left,
                top: state.sheet ? null : state.top,
                width: state.width,
                height: state.height,
            }));
        } catch (_err) { /* private-mode storage: position is session-only */ }
    }

    function restore() {
        try {
            var raw = root.localStorage.getItem(storageKey());
            if (!raw) return;
            var saved = JSON.parse(raw);
            if (saved && typeof saved === 'object') {
                if (typeof saved.collapsed === 'boolean') state.collapsed = saved.collapsed;
                if (Number.isFinite(saved.left)) state.left = saved.left;
                if (Number.isFinite(saved.top)) state.top = saved.top;
                if (Number.isFinite(saved.width)) state.width = saved.width;
                if (Number.isFinite(saved.height)) state.height = saved.height;
            }
        } catch (_err) { /* ignore corrupt cache */ }
    }

    function viewportSize() {
        var doc = root.document;
        var el = doc && doc.documentElement;
        return {
            width: (root.innerWidth || (el && el.clientWidth) || 1280),
            height: (root.innerHeight || (el && el.clientHeight) || 720),
        };
    }

    function sizeBounds(view) {
        var v = view || viewportSize();
        var maxW = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, v.width - 2 * VIEW_MARGIN));
        var maxH = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, v.height - 2 * VIEW_MARGIN));
        return { minWidth: MIN_WIDTH, minHeight: MIN_HEIGHT, maxWidth: maxW, maxHeight: maxH };
    }

    function clampSize(width, height, view) {
        var b = sizeBounds(view);
        var w = Number.isFinite(width) ? width : PANEL_WIDTH;
        var h = Number.isFinite(height) ? height : PANEL_HEIGHT;
        return {
            width: Math.round(Math.min(b.maxWidth, Math.max(b.minWidth, w))),
            height: Math.round(Math.min(b.maxHeight, Math.max(b.minHeight, h))),
        };
    }

    // The panel's effective size: the reviewer's choice, never beyond the viewport.
    function panelSize(view) {
        return clampSize(state.width, state.height, view);
    }

    function currentPresetId() {
        for (var i = 0; i < SIZE_PRESETS.length; i += 1) {
            if (SIZE_PRESETS[i].width === state.width && SIZE_PRESETS[i].height === state.height) {
                return SIZE_PRESETS[i].id;
            }
        }
        return 'custom';
    }

    function clampToViewport(left, top, width, height, vw, vh) {
        var maxL = Math.max(VIEW_MARGIN, vw - width - VIEW_MARGIN);
        var maxT = Math.max(VIEW_MARGIN, vh - height - VIEW_MARGIN);
        return {
            left: Math.min(Math.max(VIEW_MARGIN, left), maxL),
            top: Math.min(Math.max(VIEW_MARGIN, top), maxT),
        };
    }

    function rectsOverlap(a, b) {
        if (!a || !b) return false;
        return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    }

    function boxFromRect(rect) {
        if (!rect) return null;
        var left = Number(rect.left) || 0;
        var top = Number(rect.top) || 0;
        var width = Number(rect.width);
        var height = Number(rect.height);
        var right = Number.isFinite(Number(rect.right)) ? Number(rect.right) : left + (Number.isFinite(width) ? width : 0);
        var bottom = Number.isFinite(Number(rect.bottom)) ? Number(rect.bottom) : top + (Number.isFinite(height) ? height : 0);
        return {
            left: left,
            top: top,
            right: right,
            bottom: bottom,
            width: right - left,
            height: bottom - top,
        };
    }

    function unionBoxes(a, b) {
        if (!a) return b || null;
        if (!b) return a;
        var left = Math.min(a.left, b.left);
        var top = Math.min(a.top, b.top);
        var right = Math.max(a.right, b.right);
        var bottom = Math.max(a.bottom, b.bottom);
        return {
            left: left,
            top: top,
            right: right,
            bottom: bottom,
            width: right - left,
            height: bottom - top,
        };
    }

    function nodeBox(node) {
        if (!node || typeof node.getBoundingClientRect !== 'function') return null;
        var box = boxFromRect(node.getBoundingClientRect());
        if (!box || (box.width <= 0 && box.height <= 0)) return null;
        return box;
    }

    function panelBoxAt(left, top, width, height) {
        return {
            left: left,
            top: top,
            right: left + width,
            bottom: top + height,
            width: width,
            height: height,
        };
    }

    function positionClear(left, top, width, height, obstacle, viewport) {
        var vw = viewport.width;
        var vh = viewport.height;
        var placed = clampToViewport(left, top, width, height, vw, vh);
        var box = panelBoxAt(placed.left, placed.top, width, height);
        if (box.left < VIEW_MARGIN - 0.5 || box.top < VIEW_MARGIN - 0.5) return null;
        if (box.right > vw - VIEW_MARGIN + 0.5 || box.bottom > vh - VIEW_MARGIN + 0.5) return null;
        if (obstacle && rectsOverlap(box, obstacle)) return null;
        return placed;
    }

    function placeBesidePlayer(playerRect, viewport, panelSize) {
        var w = panelSize.width;
        var h = panelSize.height;
        var vw = viewport.width;
        var vh = viewport.height;
        var obstacle = boxFromRect(playerRect) || playerRect;
        var candidates = [
            { left: obstacle.right + GAP, top: obstacle.top },
            { left: obstacle.left - GAP - w, top: obstacle.top },
            { left: obstacle.left, top: obstacle.bottom + GAP },
            { left: Math.max(VIEW_MARGIN, vw - w - GAP), top: obstacle.bottom + GAP },
            { left: obstacle.right + GAP, top: VIEW_MARGIN },
            { left: VIEW_MARGIN, top: obstacle.bottom + GAP },
            { left: obstacle.left, top: Math.max(VIEW_MARGIN, obstacle.top - GAP - h) },
        ];
        for (var i = 0; i < candidates.length; i += 1) {
            var hit = positionClear(candidates[i].left, candidates[i].top, w, h, obstacle, viewport);
            if (hit) {
                return { left: hit.left, top: hit.top, sheet: false };
            }
        }
        var docked = clampToViewport(vw - w - VIEW_MARGIN, VIEW_MARGIN, w, h, vw, vh);
        return { left: docked.left, top: docked.top, sheet: true };
    }

    function parseSseBlock(block) {
        var eventName = 'message';
        var dataLines = [];
        var lines = String(block || '').split(/\r?\n/);
        for (var i = 0; i < lines.length; i += 1) {
            var line = lines[i];
            if (!line || line.charAt(0) === ':') continue;
            if (line.indexOf('event:') === 0) {
                eventName = line.slice(6).trim();
            } else if (line.indexOf('data:') === 0) {
                dataLines.push(line.slice(5).replace(/^\s/, ''));
            }
        }
        var raw = dataLines.join('\n');
        var data = raw;
        if (raw) {
            try { data = JSON.parse(raw); } catch (_err) { data = raw; }
        } else {
            data = {};
        }
        return { event: eventName, data: data };
    }

    function parseSseStream(text) {
        return String(text || '')
            .split(/\r?\n\r?\n/)
            .filter(function (chunk) { return chunk.trim().length > 0; })
            .map(parseSseBlock);
    }

    function normalizeToolInput(input) {
        if (input == null) return {};
        if (typeof input === 'string') {
            try { return JSON.parse(input); } catch (_err) { return {}; }
        }
        if (typeof input === 'object') return input;
        return {};
    }

    function applyToolCall(name, input, host) {
        var target = host || root.__screenscribeAgentHost || {};
        var payload = normalizeToolInput(input);
        if (name === 'seek') {
            var timestamp = Number(payload.timestamp != null ? payload.timestamp : payload.time);
            if (Number.isFinite(timestamp) && typeof target.seek === 'function') {
                target.seek(timestamp);
            }
            return 'seek';
        }
        if (name === 'show_frame') {
            if (typeof target.showFrame === 'function') {
                target.showFrame(payload);
            }
            return 'show_frame';
        }
        return null;
    }

    function getAgentHost() {
        return root.__screenscribeAgentHost || {};
    }

    function fingerprintToolResult(name, result) {
        try {
            return String(name || '') + ':' + JSON.stringify(result);
        } catch (_err) {
            return String(name || '') + ':' + String(result);
        }
    }

    function appendSystemLine(text, className) {
        var row = appendMessage('system', text);
        if (row && className) {
            row.className = 'ss-agent-msg ss-agent-msg-system ' + className;
        }
        return row;
    }

    function describeOp(op) {
        if (!op || typeof op !== 'object') return '';
        var kind = String(op.op || '');
        var id = op.finding_id != null ? String(op.finding_id) : '';
        if (id) return kind + ' #' + id;
        if (kind === 'add_finding') return kind + ' @' + String(op.timestamp);
        return kind;
    }

    function confirmationLine(explain, extra) {
        var body = tx('review.agentPatchApplied', { explain: explain || '' });
        var hint = tx('review.agentPatchUndoHint');
        var line = body + ' — ' + hint;
        if (extra) line += ' ' + extra;
        return line;
    }

    function recordReviewApplied(explain) {
        var message = 'review_applied' + (explain ? ': ' + explain : '');
        var last = state.history.length ? state.history[state.history.length - 1] : null;
        if (!last || last.role !== 'user' || last.content !== message) {
            state.history.push({ role: 'user', content: message });
        }
    }

    function attachRetry(row, retryFn) {
        if (!row) return;
        var btn = el('button', 'ss-agent-retry', { type: 'button' });
        btn.textContent = t('review.agentPatchRetry');
        btn.addEventListener('click', function () {
            if (btn.disabled) return;
            btn.disabled = true;
            Promise.resolve(retryFn()).finally(function () {
                btn.disabled = false;
            });
        });
        row.appendChild(btn);
        return btn;
    }

    async function saveAfterPatch() {
        var host = getAgentHost();
        var saveFn = host.saveReview || host.saveReviewToDisk;
        if (typeof saveFn !== 'function') {
            return { ok: false, message: 'saveReview' };
        }
        return saveFn();
    }

    async function applyOpsAndSave(ops, explain, userInstruction) {
        if (isOffline()) {
            appendSystemLine(tx('review.agentPatchOffline'), 'ss-agent-msg-warn');
            return { ok: false, offline: true };
        }
        var host = getAgentHost();
        if (typeof host.applyReviewPatch !== 'function') {
            appendSystemLine(tx('review.agentPatchSaveFailed', { message: 'applyReviewPatch' }), 'ss-agent-msg-error');
            return { ok: false };
        }
        var results = await host.applyReviewPatch(ops, userInstruction || '');
        var unknown = (results || []).filter(function (row) { return row && row.unknown; });
        unknown.forEach(function (row) {
            appendSystemLine(tx('review.agentPatchUnknown', { op: row.op || '' }));
        });
        var unsupported = (results || []).filter(function (row) { return row && row.unsupported; });
        unsupported.forEach(function (row) {
            appendSystemLine(row.reason || tx('review.agentPatchUnknown', { op: row.op || '' }));
        });
        var applied = (results || []).filter(function (row) { return row && !row.skipped; });
        if (!applied.length) {
            return { ok: true, applied: 0, results: results };
        }
        var saved = await saveAfterPatch();
        if (saved && saved.ok) {
            appendSystemLine(confirmationLine(explain), 'ss-agent-msg-ok');
            recordReviewApplied(explain);
            return { ok: true, applied: applied.length, saved: true, results: results };
        }
        var message = (saved && saved.message) || tx('review.agentPatchSaveFailed', { message: '' });
        var failRow = appendSystemLine(
            tx('review.agentPatchSaveFailed', { message: message }),
            'ss-agent-msg-error'
        );
        attachRetry(failRow, function () { return retrySave(explain); });
        return { ok: false, status: saved && saved.status, applied: applied.length, results: results };
    }

    async function retrySave(explain) {
        if (isOffline()) {
            appendSystemLine(tx('review.agentPatchOffline'), 'ss-agent-msg-warn');
            return { ok: false, offline: true };
        }
        var saved = await saveAfterPatch();
        if (saved && saved.ok) {
            appendSystemLine(confirmationLine(explain), 'ss-agent-msg-ok');
            recordReviewApplied(explain);
            return saved;
        }
        var message = (saved && saved.message) || '';
        var failRow = appendSystemLine(
            tx('review.agentPatchSaveFailed', { message: message }),
            'ss-agent-msg-error'
        );
        attachRetry(failRow, function () { return retrySave(explain); });
        return saved;
    }

    function renderPlanCard(plan, userInstruction) {
        var log = root.document.getElementById('ss-agent-log');
        if (!log) return null;
        var ops = Array.isArray(plan.ops) ? plan.ops : [];
        var rationale = Array.isArray(plan.rationale) ? plan.rationale : [];
        var card = el('div', 'ss-agent-plan ss-agent-msg ss-agent-msg-system');
        var list = el('ul', 'ss-agent-plan-ops');
        ops.forEach(function (op, index) {
            var item = el('li', 'ss-agent-plan-op');
            var label = el('label', 'ss-agent-plan-label');
            var box = el('input', 'ss-agent-plan-check', { type: 'checkbox' });
            box.checked = true;
            box.setAttribute('data-op-index', String(index));
            var caption = el('span', 'ss-agent-plan-caption');
            var reason = rationale[index] ? ' — ' + String(rationale[index]) : '';
            caption.textContent = describeOp(op) + reason;
            label.appendChild(box);
            label.appendChild(caption);
            item.appendChild(label);
            list.appendChild(item);
        });
        card.appendChild(list);
        var actions = el('div', 'ss-agent-plan-actions');
        var applyBtn = el('button', 'ss-agent-plan-apply', { type: 'button' });
        applyBtn.textContent = t('review.agentPlanApply');
        var cancelBtn = el('button', 'ss-agent-plan-cancel', { type: 'button' });
        cancelBtn.textContent = t('review.agentPlanCancel');
        applyBtn.addEventListener('click', function () {
            if (applyBtn.disabled) return;
            applyBtn.disabled = true;
            cancelBtn.disabled = true;
            var selected = [];
            var checks = card.querySelectorAll('.ss-agent-plan-check');
            checks.forEach(function (check) {
                if (check.checked) {
                    var idx = Number(check.getAttribute('data-op-index'));
                    if (ops[idx]) selected.push(ops[idx]);
                }
            });
            Promise.resolve(applyOpsAndSave(selected, tx('review.agentPlanApplied', {
                applied: selected.length,
                total: ops.length,
            }), userInstruction)).then(function (result) {
                if (!result || !result.ok) return;
                var summary = tx('review.agentPlanApplied', {
                    applied: result.applied || 0,
                    total: ops.length,
                });
                appendMessage('user', summary);
            }).finally(function () {
                applyBtn.disabled = false;
                cancelBtn.disabled = false;
            });
        });
        cancelBtn.addEventListener('click', function () {
            if (card.parentNode) card.parentNode.removeChild(card);
        });
        actions.appendChild(applyBtn);
        actions.appendChild(cancelBtn);
        card.appendChild(actions);
        log.appendChild(card);
        if (typeof log.scrollTop === 'number') {
            log.scrollTop = log.scrollHeight || 0;
        }
        return card;
    }

    async function ingestToolResult(name, result, seen, userInstruction) {
        if (result == null) return;
        var payload = result;
        if (typeof payload === 'string') {
            try { payload = JSON.parse(payload); } catch (_err) { payload = { error: payload }; }
        }
        if (typeof payload !== 'object') return;
        var fp = fingerprintToolResult(name, payload);
        if (seen && seen.has(fp)) return;
        if (seen) seen.add(fp);
        if (payload.error) {
            appendSystemLine(String(payload.error), 'ss-agent-msg-error');
            return;
        }
        if (payload.unsupported) {
            appendSystemLine(String(payload.reason || payload.unsupported), 'ss-agent-msg-warn');
            return;
        }
        if (payload.type === 'review_plan') {
            renderPlanCard(payload, userInstruction);
            return;
        }
        if (payload.type === 'review_patch') {
            await applyOpsAndSave(payload.ops || [], payload.explain || '', userInstruction);
        }
    }

    function isOffline() {
        try {
            if (root.location && root.location.protocol === 'file:') return true;
            var body = root.document && root.document.body;
            if (body && body.dataset && body.dataset.staticDemo === 'true') return true;
        } catch (_err) { /* ignore */ }
        return false;
    }

    function el(tag, className, attrs) {
        var node = root.document.createElement(tag);
        if (className) node.className = className;
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                var value = attrs[key];
                if (key === 'id') node.id = value;
                else if (key === 'hidden') node.hidden = Boolean(value);
                else if (key === 'placeholder') node.placeholder = value;
                else if (typeof node.setAttribute === 'function') node.setAttribute(key, value);
                else node[key] = value;
            });
        }
        return node;
    }

    function getRoot() {
        return root.document.getElementById('ss-agent-root');
    }

    function playerRect() {
        var doc = root.document;
        var box = nodeBox(doc.getElementById('videoPlayer'));
        box = unionBoxes(box, nodeBox(doc.getElementById('videoControls')));
        box = unionBoxes(box, nodeBox(doc.querySelector('.video-controls-pro')));
        if (!box) {
            box = nodeBox(doc.querySelector('.video-container'))
                || nodeBox(doc.querySelector('.main-column'))
                || nodeBox(doc.querySelector('.video-panel'));
        }
        if (box) return box;
        return { left: 16, top: 96, right: 640, bottom: 480, width: 624, height: 384 };
    }

    function protectedRect() {
        return playerRect();
    }

    function resolveExpandedPlacement(obstacle, viewport, panelSize, savedLeft, savedTop) {
        var w = panelSize.width;
        var h = panelSize.height;
        if (Number.isFinite(savedLeft) && Number.isFinite(savedTop)) {
            var kept = positionClear(savedLeft, savedTop, w, h, obstacle, viewport);
            if (kept) {
                return { left: kept.left, top: kept.top, sheet: false };
            }
        }
        return placeBesidePlayer(obstacle, viewport, panelSize);
    }

    function setSheetMode(rootEl, enabled) {
        var doc = root.document;
        var htmlEl = doc && doc.documentElement;
        state.sheet = Boolean(enabled);
        if (rootEl) {
            if (state.sheet) rootEl.classList.add(SHEET_CLASS);
            else rootEl.classList.remove(SHEET_CLASS);
        }
        if (htmlEl && htmlEl.classList) {
            if (state.sheet) htmlEl.classList.add(SHEET_CLASS);
            else htmlEl.classList.remove(SHEET_CLASS);
        }
        if (htmlEl && htmlEl.style) {
            htmlEl.style.setProperty('--ss-agent-sheet-width', panelSize().width + 'px');
        }
    }

    function applyDomState() {
        var rootEl = getRoot();
        if (!rootEl) return;
        var collapsed = state.collapsed;
        if (collapsed) {
            rootEl.classList.add('ss-agent-collapsed');
            rootEl.classList.remove('ss-agent-expanded');
        } else {
            rootEl.classList.add('ss-agent-expanded');
            rootEl.classList.remove('ss-agent-collapsed');
        }
        var fab = root.document.getElementById('ss-agent-fab');
        if (fab) {
            fab.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            fab.setAttribute('aria-label', collapsed ? tx('review.agentToggle') : tx('review.agentCollapse'));
        }
        var panel = root.document.getElementById('ss-agent-panel');
        var view = viewportSize();
        if (collapsed) {
            setSheetMode(rootEl, false);
            var fabPos = clampToViewport(
                view.width - FAB_SIZE - GAP,
                view.height - FAB_SIZE - GAP,
                FAB_SIZE,
                FAB_SIZE,
                view.width,
                view.height
            );
            rootEl.style.left = fabPos.left + 'px';
            rootEl.style.top = fabPos.top + 'px';
            rootEl.style.right = 'auto';
            rootEl.style.width = FAB_SIZE + 'px';
            rootEl.style.height = FAB_SIZE + 'px';
            if (panel) panel.hidden = true;
            return;
        }
        var size = panelSize(view);
        var obstacle = protectedRect();
        var probe = obstacle;
        if (state.sheet) {
            probe = unionBoxes(obstacle, {
                left: obstacle.right,
                top: obstacle.top,
                right: Math.min(view.width, obstacle.right + size.width + GAP),
                bottom: obstacle.bottom,
                width: 0,
                height: 0,
            }) || obstacle;
        }
        var savedLeft = state.drag ? state.left : (state.sheet ? null : state.left);
        var savedTop = state.drag ? state.top : (state.sheet ? null : state.top);
        if (state.resize) {
            // Live resize: follow the pointer inside the viewport; placement
            // against the player is settled once when the resize ends.
            if (!state.sheet) {
                var held = clampToViewport(state.left, state.top, size.width, size.height, view.width, view.height);
                state.left = held.left;
                state.top = held.top;
            }
        } else if (state.drag && Number.isFinite(state.left) && Number.isFinite(state.top)) {
            var dragged = positionClear(state.left, state.top, size.width, size.height, obstacle, view);
            if (dragged) {
                state.left = dragged.left;
                state.top = dragged.top;
                state.sheet = false;
            } else {
                var placedDrag = placeBesidePlayer(obstacle, view, size);
                state.left = placedDrag.left;
                state.top = placedDrag.top;
                state.sheet = Boolean(placedDrag.sheet);
            }
        } else {
            var placed = resolveExpandedPlacement(
                probe,
                view,
                size,
                savedLeft,
                savedTop
            );
            state.left = placed.left;
            state.top = placed.top;
            state.sheet = Boolean(placed.sheet);
        }
        setSheetMode(rootEl, state.sheet);
        if (state.sheet) {
            rootEl.style.left = 'auto';
            rootEl.style.right = '0px';
            rootEl.style.top = '';
            rootEl.style.width = size.width + 'px';
            rootEl.style.height = '';
        } else {
            rootEl.style.right = 'auto';
            rootEl.style.left = state.left + 'px';
            rootEl.style.top = state.top + 'px';
            rootEl.style.width = size.width + 'px';
            rootEl.style.height = size.height + 'px';
        }
        syncSizeControls(size);
        if (panel) panel.hidden = false;
    }

    function setCollapsed(next) {
        state.collapsed = Boolean(next);
        if (state.collapsed) {
            state.sheet = false;
        }
        applyDomState();
        persist();
    }

    function relayout() {
        if (state.drag) return;
        applyDomState();
        if (!state.collapsed) persist();
    }

    function toggle() {
        setCollapsed(!state.collapsed);
    }

    // ---- Markdown ----------------------------------------------------------
    // Agent answers are Markdown. They are parsed into a small block/inline tree
    // and built with createElement + text nodes only: model text never reaches
    // innerHTML or a URL attribute, so a reply cannot inject markup or navigation.

    function mdIsBlockStart(line) {
        return /^\s*```/.test(line) || /^#{1,6}\s+/.test(line) || /^\s*>/.test(line)
            || /^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)
            || /^\s*([-*_])(\s*\1){2,}\s*$/.test(line);
    }

    function mdTableCells(line) {
        var trimmed = String(line).trim().replace(/^\|/, '').replace(/\|$/, '');
        return trimmed.split('|').map(function (cell) { return cell.trim(); });
    }

    function mdBlocks(text) {
        var lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
        var blocks = [];
        var i = 0;
        while (i < lines.length) {
            var line = lines[i];
            var fence = line.match(/^\s*```\s*([\w+#.-]*)\s*$/);
            if (fence) {
                var code = [];
                i += 1;
                while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
                    code.push(lines[i]);
                    i += 1;
                }
                i += 1;
                blocks.push({ type: 'code', lang: fence[1] || '', text: code.join('\n') });
                continue;
            }
            if (/^\s*$/.test(line)) { i += 1; continue; }
            var heading = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
            if (heading) {
                blocks.push({ type: 'heading', level: heading[1].length, text: heading[2] });
                i += 1;
                continue;
            }
            if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
                blocks.push({ type: 'hr' });
                i += 1;
                continue;
            }
            if (line.indexOf('|') >= 0 && i + 1 < lines.length
                && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
                var header = mdTableCells(line);
                var rows = [];
                i += 2;
                while (i < lines.length && lines[i].indexOf('|') >= 0 && !/^\s*$/.test(lines[i])) {
                    rows.push(mdTableCells(lines[i]));
                    i += 1;
                }
                blocks.push({ type: 'table', header: header, rows: rows });
                continue;
            }
            if (/^\s*>/.test(line)) {
                var quote = [];
                while (i < lines.length && /^\s*>/.test(lines[i])) {
                    quote.push(lines[i].replace(/^\s*>\s?/, ''));
                    i += 1;
                }
                blocks.push({ type: 'quote', text: quote.join('\n') });
                continue;
            }
            var bullet = /^\s*[-*+]\s+(.*)$/;
            var ordered = /^\s*(\d+)[.)]\s+(.*)$/;
            if (bullet.test(line) || ordered.test(line)) {
                var isOrdered = !bullet.test(line);
                var pattern = isOrdered ? ordered : bullet;
                var first = line.match(pattern);
                var list = { type: isOrdered ? 'ol' : 'ul', start: isOrdered ? Number(first[1]) : 1, items: [] };
                while (i < lines.length) {
                    var item = lines[i].match(pattern);
                    if (item) {
                        list.items.push(isOrdered ? item[2] : item[1]);
                        i += 1;
                    } else if (list.items.length && /^\s{2,}\S/.test(lines[i])) {
                        list.items[list.items.length - 1] += '\n' + lines[i].trim();
                        i += 1;
                    } else {
                        break;
                    }
                }
                blocks.push(list);
                continue;
            }
            var para = [line];
            i += 1;
            while (i < lines.length && !/^\s*$/.test(lines[i]) && !mdIsBlockStart(lines[i])) {
                para.push(lines[i]);
                i += 1;
            }
            blocks.push({ type: 'p', text: para.join('\n') });
        }
        return blocks;
    }

    var MD_INLINE = /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)|\*\*(?=\S)([\s\S]*?\S)\*\*|\*(?=[^\s*])([^*\n]*?[^\s*])\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"])|(\n)/g;

    function mdInline(text) {
        var src = String(text || '');
        var out = [];
        var last = 0;
        var re = new RegExp(MD_INLINE.source, 'g');
        var match;
        while ((match = re.exec(src)) !== null) {
            if (match.index > last) out.push({ t: 'text', v: src.slice(last, match.index) });
            if (match[1]) out.push({ t: 'code', v: match[2] });
            else if (match[3] != null) out.push({ t: 'strong', c: mdInline(match[3]) });
            else if (match[4] != null) out.push({ t: 'em', c: mdInline(match[4]) });
            else if (match[5] != null) out.push({ t: 'link', href: match[6], c: mdInline(match[5]) });
            else if (match[7] != null) out.push({ t: 'link', href: match[7], c: [{ t: 'text', v: match[7] }] });
            else if (match[8] != null) out.push({ t: 'br' });
            last = re.lastIndex;
            if (match[0] === '') re.lastIndex += 1;
        }
        if (last < src.length) out.push({ t: 'text', v: src.slice(last) });
        return out;
    }

    function mdInlineNodes(doc, parent, tokens) {
        tokens.forEach(function (token) {
            var node;
            if (token.t === 'text') {
                parent.appendChild(doc.createTextNode(token.v));
                return;
            }
            if (token.t === 'br') {
                parent.appendChild(doc.createElement('br'));
                return;
            }
            if (token.t === 'code') {
                node = doc.createElement('code');
                node.textContent = token.v;
            } else if (token.t === 'link') {
                // Links stay text: a model-written URL never becomes an href
                // (no javascript:/data: navigation from an answer). The address
                // is shown so it can be copied.
                node = doc.createElement('span');
                node.className = 'ss-agent-md-link';
                mdInlineNodes(doc, node, token.c);
                var label = token.c.map(function (part) { return part.v || ''; }).join('');
                if (label !== token.href) node.appendChild(doc.createTextNode(' (' + token.href + ')'));
            } else {
                node = doc.createElement(token.t === 'strong' ? 'strong' : 'em');
                mdInlineNodes(doc, node, token.c);
            }
            parent.appendChild(node);
        });
    }

    function mdNodes(doc, text) {
        return mdBlocks(text).map(function (block) {
            var node;
            if (block.type === 'code') {
                node = doc.createElement('pre');
                var code = doc.createElement('code');
                if (block.lang) code.setAttribute('data-lang', block.lang);
                code.textContent = block.text;
                node.appendChild(code);
            } else if (block.type === 'heading') {
                node = doc.createElement('h' + Math.min(6, block.level + 2));
                mdInlineNodes(doc, node, mdInline(block.text));
            } else if (block.type === 'hr') {
                node = doc.createElement('hr');
            } else if (block.type === 'quote') {
                node = doc.createElement('blockquote');
                mdNodes(doc, block.text).forEach(function (child) { node.appendChild(child); });
            } else if (block.type === 'ul' || block.type === 'ol') {
                node = doc.createElement(block.type);
                if (block.type === 'ol' && block.start !== 1) node.setAttribute('start', String(block.start));
                block.items.forEach(function (item) {
                    var li = doc.createElement('li');
                    mdInlineNodes(doc, li, mdInline(item));
                    node.appendChild(li);
                });
            } else if (block.type === 'table') {
                node = doc.createElement('div');
                node.className = 'ss-agent-table-wrap';
                var table = doc.createElement('table');
                var thead = doc.createElement('thead');
                var headRow = doc.createElement('tr');
                block.header.forEach(function (cell) {
                    var th = doc.createElement('th');
                    mdInlineNodes(doc, th, mdInline(cell));
                    headRow.appendChild(th);
                });
                thead.appendChild(headRow);
                table.appendChild(thead);
                var tbody = doc.createElement('tbody');
                block.rows.forEach(function (cells) {
                    var tr = doc.createElement('tr');
                    cells.forEach(function (cell) {
                        var td = doc.createElement('td');
                        mdInlineNodes(doc, td, mdInline(cell));
                        tr.appendChild(td);
                    });
                    tbody.appendChild(tr);
                });
                table.appendChild(tbody);
                node.appendChild(table);
            } else {
                node = doc.createElement('p');
                mdInlineNodes(doc, node, mdInline(block.text));
            }
            return node;
        });
    }

    // Real DOM: rebuild the row from Markdown. Minimal DOMs without
    // replaceChildren (the node-vm harness) keep the plain text.
    function renderRich(row, text) {
        if (!row) return;
        var doc = root.document;
        var value = String(text || '');
        if (typeof row.replaceChildren !== 'function' || !doc || typeof doc.createTextNode !== 'function') {
            row.textContent = value;
            return;
        }
        row.replaceChildren.apply(row, mdNodes(doc, value));
        if (row.classList) row.classList.add('ss-agent-md');
    }

    function roleLabel(role) {
        if (role === 'user') return t('review.agentRoleUser');
        if (role === 'assistant') return t('review.agentRoleAgent');
        return '';
    }

    function appendImageStrip(row, images) {
        if (!row || !images || !images.length) return;
        var strip = el('div', 'ss-agent-msg-images');
        images.forEach(function (image) {
            var src = image.url || ('data:' + image.media_type + ';base64,' + image.data);
            var img = el('img', 'ss-agent-msg-image', { alt: image.name || t('review.agentAttachmentAlt') });
            img.src = src;
            strip.appendChild(img);
        });
        row.appendChild(strip);
    }

    function appendMessage(role, text, images) {
        var log = root.document.getElementById('ss-agent-log');
        if (!log) return null;
        var row = el('div', 'ss-agent-msg ss-agent-msg-' + role);
        var label = roleLabel(role);
        if (label && typeof row.setAttribute === 'function') row.setAttribute('data-role-label', label);
        if (role === 'assistant') renderRich(row, text);
        else row.textContent = text;
        appendImageStrip(row, images);
        log.appendChild(row);
        if (typeof log.scrollTop === 'number') {
            log.scrollTop = log.scrollHeight || 0;
        }
        return row;
    }

    function paintAssistantError(row, message) {
        var text = message || tx('review.agentEmpty');
        if (row) {
            row.textContent = text;
            row.className = 'ss-agent-msg ss-agent-msg-error';
            return row;
        }
        return appendMessage('error', text);
    }

    function removeEmptyAssistant(row) {
        if (!row) return;
        var text = String(row.textContent || '');
        if (text) return;
        if (row.parentNode && typeof row.parentNode.removeChild === 'function') {
            row.parentNode.removeChild(row);
        }
    }

    function showOfflineNotice() {
        var notice = root.document.getElementById('ss-agent-offline');
        var message = tx('review.agentOffline');
        if (notice) {
            notice.hidden = false;
            notice.textContent = message;
        } else {
            appendMessage('system', message);
        }
        if (!state.patchOfflineAnnounced) {
            state.patchOfflineAnnounced = true;
            appendSystemLine(tx('review.agentPatchOffline'), 'ss-agent-msg-warn');
        }
    }

    function showPanelError(message) {
        var text = message || tx('review.agentEmpty');
        appendMessage('error', text);
        var notice = root.document.getElementById('ss-agent-offline');
        if (notice && /screenscribe serve/i.test(String(text))) {
            notice.hidden = false;
            notice.textContent = text;
        }
    }

    function abortError() {
        var error = new Error('aborted');
        error.name = 'AbortError';
        return error;
    }

    // Reads the SSE body as it arrives: each frame is handled the moment its
    // blank-line delimiter lands, so the first token renders before the turn
    // ends. `options.onActivity` fires on every received chunk (keepalives
    // included); `options.signal` aborts the read.
    async function consumeSse(response, onEvent, options) {
        var opts = options || {};
        var signal = opts.signal || null;
        var body = response && response.body;
        if (body && typeof body.getReader === 'function') {
            var reader = body.getReader();
            var decoder = new TextDecoder();
            var buffer = '';
            var onAbort = function () {
                try { reader.cancel(); } catch (_err) { /* already closed */ }
            };
            if (signal && typeof signal.addEventListener === 'function') {
                signal.addEventListener('abort', onAbort);
            }
            try {
                while (true) {
                    if (signal && signal.aborted) throw abortError();
                    var chunk = await reader.read();
                    if (signal && signal.aborted) throw abortError();
                    if (chunk.done) break;
                    if (typeof opts.onActivity === 'function') opts.onActivity();
                    buffer += decoder.decode(chunk.value, { stream: true });
                    var delimiter;
                    while ((delimiter = /\r?\n\r?\n/.exec(buffer)) !== null) {
                        var idx = delimiter.index;
                        var block = buffer.slice(0, idx);
                        buffer = buffer.slice(idx + delimiter[0].length);
                        if (block.trim()) await onEvent(parseSseBlock(block));
                    }
                }
            } finally {
                if (signal && typeof signal.removeEventListener === 'function') {
                    signal.removeEventListener('abort', onAbort);
                }
            }
            if (buffer.trim()) await onEvent(parseSseBlock(buffer));
            return;
        }
        var text = typeof response.text === 'function' ? await response.text() : '';
        var events = parseSseStream(text);
        for (var i = 0; i < events.length; i += 1) {
            await onEvent(events[i]);
        }
    }

    var THREAD_DB = 'screenscribe-agent';
    var THREAD_STORE = 'threads';

    // Composer state is derived, never stored: Send is enabled only for a
    // non-empty draft while no turn is streaming; the draft itself always stays
    // editable, and Stop is visible exactly while a turn streams.
    function syncComposer() {
        var doc = root.document;
        if (!doc) return;
        var input = doc.getElementById('ss-agent-input');
        var sendBtn = doc.getElementById('ss-agent-send');
        var stopBtn = doc.getElementById('ss-agent-stop');
        var activity = doc.getElementById('ss-agent-activity');
        var panel = doc.getElementById('ss-agent-panel');
        if (input) input.disabled = false;
        var hasText = Boolean(input && String(input.value || '').trim()) || state.attachments.length > 0;
        if (sendBtn) {
            sendBtn.disabled = !hasText || state.streaming;
            if (typeof sendBtn.setAttribute === 'function') {
                sendBtn.setAttribute('aria-disabled', sendBtn.disabled ? 'true' : 'false');
            }
        }
        if (stopBtn) stopBtn.hidden = !state.streaming;
        if (activity) activity.hidden = !state.streaming;
        if (panel && typeof panel.setAttribute === 'function') {
            panel.setAttribute('aria-busy', state.streaming ? 'true' : 'false');
        }
    }

    function setComposerLive() {
        syncComposer();
    }

    function draftKey() {
        return storageKey() + ':draft';
    }

    function saveDraft(value) {
        try {
            var text = String(value || '');
            if (text) root.localStorage.setItem(draftKey(), text);
            else root.localStorage.removeItem(draftKey());
        } catch (_err) { /* private-mode storage: the draft lives only in the field */ }
    }

    function loadDraft() {
        try {
            return root.localStorage.getItem(draftKey()) || '';
        } catch (_err) {
            return '';
        }
    }

    // Grow the textarea with its content up to ~40% of the panel, then scroll.
    function autosizeInput(input) {
        if (!input || !input.style) return;
        var maxPx = Math.max(64, Math.min(COMPOSER_MAX_HEIGHT, Math.round(panelSize().height * 0.4)));
        input.style.height = 'auto';
        var scroll = Number(input.scrollHeight) || 0;
        if (scroll > 0) input.style.height = Math.min(scroll, maxPx) + 'px';
        input.style.overflowY = scroll > maxPx ? 'auto' : 'hidden';
    }

    function onDraftChanged(input) {
        saveDraft(input.value);
        autosizeInput(input);
        syncComposer();
    }

    function openThreadDb() {
        var factory = root.indexedDB;
        if (!factory || typeof factory.open !== 'function') return Promise.resolve(null);
        return new Promise(function (resolve) {
            var req;
            try {
                req = factory.open(THREAD_DB, 1);
            } catch (_err) {
                resolve(null);
                return;
            }
            req.onupgradeneeded = function () {
                var db = req.result;
                if (!db || typeof db.createObjectStore !== 'function') return;
                var names = db.objectStoreNames;
                if (names && typeof names.contains === 'function' && names.contains(THREAD_STORE)) return;
                db.createObjectStore(THREAD_STORE);
            };
            req.onsuccess = function () { resolve(req.result || null); };
            req.onerror = function () { resolve(null); };
        });
    }

    function threadRecord() {
        var identity = state.previousResponseIdentity;
        return {
            messages: state.history.map(function (entry) {
                return { role: entry.role, content: entry.content };
            }),
            previousResponseId: state.previousResponseId || null,
            // Without the identity a restored id is rejected by send() and the
            // server-side chain silently restarts after every reload.
            previousResponseIdentity: identity ? {
                provider: identity.provider,
                protocol: identity.protocol,
                host: identity.host,
            } : null,
        };
    }

    function withThreadStore(mode, fn) {
        return openThreadDb().then(function (db) {
            if (!db) return null;
            try {
                var tx = db.transaction(THREAD_STORE, mode);
                return fn(tx.objectStore(THREAD_STORE));
            } catch (_err) {
                return null;
            }
        });
    }

    function persistThread() {
        if (!root.indexedDB) return Promise.resolve(false);
        var key = threadKey();
        var record = threadRecord();
        return withThreadStore('readwrite', function (store) {
            return new Promise(function (resolve) {
                var req;
                try {
                    req = store.put(record, key);
                } catch (_err) {
                    resolve(false);
                    return;
                }
                req.onsuccess = function () { resolve(true); };
                req.onerror = function () { resolve(false); };
            });
        }).then(function (saved) { return Boolean(saved); });
    }

    function readThread() {
        if (!root.indexedDB) return Promise.resolve(null);
        var key = threadKey();
        return withThreadStore('readonly', function (store) {
            return new Promise(function (resolve) {
                var req;
                try {
                    req = store.get(key);
                } catch (_err) {
                    resolve(null);
                    return;
                }
                req.onsuccess = function () { resolve(req.result || null); };
                req.onerror = function () { resolve(null); };
            });
        });
    }

    function clearMessageRows() {
        var log = root.document.getElementById('ss-agent-log');
        if (!log || !log.children) return;
        var doomed = [];
        for (var i = 0; i < log.children.length; i += 1) doomed.push(log.children[i]);
        doomed.forEach(function (child) {
            if (!child || !child.classList) return;
            if (!child.classList.contains('ss-agent-msg') && !child.classList.contains('ss-agent-plan')) return;
            if (child.parentNode && typeof child.parentNode.removeChild === 'function') {
                child.parentNode.removeChild(child);
            }
        });
    }

    function paintStoredMessages(messages) {
        clearMessageRows();
        var empty = root.document.querySelector('.ss-agent-empty');
        if (empty) empty.hidden = !messages.length;
        messages.forEach(function (entry) {
            appendMessage(entry.role, entry.content);
        });
    }

    function restoreThread() {
        return readThread().then(function (record) {
            if (!record || typeof record !== 'object') return false;
            var messages = Array.isArray(record.messages) ? record.messages : [];
            var restored = [];
            messages.forEach(function (entry) {
                if (!entry || (entry.role !== 'user' && entry.role !== 'assistant')) return;
                if (typeof entry.content !== 'string' || !entry.content) return;
                restored.push({ role: entry.role, content: entry.content });
            });
            var chain = record.previousResponseId;
            if (!restored.length && typeof chain !== 'string') return false;
            state.history = restored;
            state.previousResponseId = typeof chain === 'string' && chain ? chain : null;
            var identity = record.previousResponseIdentity;
            state.previousResponseIdentity = (
                identity
                && typeof identity === 'object'
                && typeof identity.provider === 'string'
                && typeof identity.protocol === 'string'
                && typeof identity.host === 'string'
            ) ? {
                provider: identity.provider,
                protocol: identity.protocol,
                host: identity.host,
            } : null;
            if (!state.previousResponseIdentity) state.previousResponseId = null;
            state.retryMessage = null;
            state.retryRow = null;
            paintStoredMessages(restored);
            return true;
        });
    }

    function formatReference(detail) {
        var d = detail && typeof detail === 'object' ? detail : {};
        var kind = d.kind === 'transcript-error' ? 'transcript-error' : 'reference';
        var quote = String(d.quote == null ? '' : d.quote).slice(0, 2000);
        var moment = d.moment_id == null ? '' : String(d.moment_id);
        var field = d.field == null ? '' : String(d.field);
        var timestamp = d.timestamp == null ? '' : String(d.timestamp);
        var marker = kind;
        if (moment) marker += ' moment=' + moment;
        if (timestamp) marker += ' t=' + timestamp;
        if (field) marker += ' field=' + field;
        var head = '[' + marker + ']';
        if (!quote) return head;
        return head + '\n> ' + quote;
    }

    function insertReference(detail) {
        setCollapsed(false);
        var block = formatReference(detail);
        var input = root.document.getElementById('ss-agent-input');
        if (!input) return block;
        var existing = String(input.value || '');
        if (existing.trim()) input.value = existing.replace(/\s+$/, '') + '\n\n' + block;
        else input.value = block;
        input.disabled = false;
        onDraftChanged(input);
        if (typeof input.focus === 'function') input.focus();
        return block;
    }

    function bindReferenceListener() {
        if (state.referenceBound) return;
        var doc = root.document;
        if (!doc || typeof doc.addEventListener !== 'function') return;
        doc.addEventListener('screenscribe:agent-insert-reference', function (event) {
            insertReference(event && event.detail);
        });
        state.referenceBound = true;
    }

    function disarmRetry() {
        var row = state.retryRow;
        state.retryMessage = null;
        state.retryRow = null;
        state.retryRows = [];
        if (!row || typeof row.querySelector !== 'function') return;
        var btn = row.querySelector('.ss-agent-retry');
        if (btn && btn.parentNode && typeof btn.parentNode.removeChild === 'function') {
            btn.parentNode.removeChild(btn);
        }
    }

    function markRetryable(row, messageText, retryText) {
        var painted = paintAssistantError(row, messageText) || row;
        state.retryRow = painted;
        state.retryRows = [painted];
        state.retryMessage = retryText;
        attachRetry(painted, function () { return retryTurn(retryText); });
    }

    function markPartial(row) {
        if (!row || !row.classList) return;
        row.classList.add('ss-agent-msg-interrupted');
        var badge = el('span', 'ss-agent-badge');
        badge.textContent = t('review.agentInterrupted');
        row.appendChild(badge);
    }

    // A failed turn keeps whatever text already streamed (marked partial) and
    // reports the failure on its own row; Retry removes both before resending,
    // so a recovered answer never duplicates the partial one.
    function failTurn(row, assembled, messageText, retryText) {
        if (!assembled) {
            markRetryable(row, messageText, retryText);
            return;
        }
        if (row) {
            renderRich(row, assembled);
            markPartial(row);
        }
        var errorRow = appendMessage('error', messageText);
        state.retryRow = errorRow;
        state.retryRows = [row, errorRow].filter(Boolean);
        state.retryMessage = retryText;
        attachRetry(errorRow, function () { return retryTurn(retryText); });
    }

    function retryTurn(message) {
        var rows = state.retryRows && state.retryRows.length ? state.retryRows : [state.retryRow];
        state.retryRow = null;
        state.retryRows = [];
        state.retryMessage = null;
        rows.forEach(function (row) {
            if (row && row.parentNode && typeof row.parentNode.removeChild === 'function') {
                row.parentNode.removeChild(row);
            }
        });
        return send(message, { retry: true, images: state.retryImages });
    }

    function clearIdleTimer() {
        if (state.idleTimer) {
            clearTimeout(state.idleTimer);
            state.idleTimer = null;
        }
    }

    function armIdleTimer() {
        clearIdleTimer();
        state.idleTimer = setTimeout(function () {
            state.idleTimer = null;
            if (!state.streaming) return;
            state.timedOut = true;
            if (state.abort) {
                try { state.abort.abort(); } catch (_err) { /* already aborted */ }
            }
        }, idleTimeoutMs());
    }

    function idleTimeoutMs() {
        var override = Number(root.__screenscribeAgentIdleTimeoutMs);
        return override > 0 ? override : IDLE_TIMEOUT_MS;
    }

    // Stop the streaming turn. Text received so far stays, marked as stopped.
    function stop() {
        if (!state.streaming) return false;
        state.stopRequested = true;
        if (state.abort) {
            try { state.abort.abort(); } catch (_err) { /* already aborted */ }
        }
        return true;
    }

    function finishInterrupted(row, assembled) {
        if (!assembled) {
            if (row && row.parentNode && typeof row.parentNode.removeChild === 'function') {
                row.parentNode.removeChild(row);
            }
            appendSystemLine(tx('review.agentInterrupted'), 'ss-agent-msg-warn');
            return;
        }
        if (row) {
            renderRich(row, assembled);
            markPartial(row);
        }
        state.history.push({
            role: 'assistant',
            content: assembled + '\n\n' + tx('review.agentInterruptedHistory'),
        });
    }

    async function readFailureMessage(response) {
        // The HTTP status is the one fact the reviewer can act on; the server's
        // own detail (when it sent one) follows it, never replaces it.
        var status = response && Number.isFinite(Number(response.status))
            ? String(response.status)
            : '?';
        var headline = tx('review.agentRequestFailed', { status: status });
        var detail = '';
        if (response && typeof response.text === 'function') {
            try {
                var raw = await response.text();
                if (raw) {
                    detail = String(raw).slice(0, 500);
                    try {
                        var parsed = JSON.parse(raw);
                        if (parsed && typeof parsed.detail === 'string' && parsed.detail) {
                            detail = parsed.detail;
                        } else if (parsed && typeof parsed.message === 'string' && parsed.message) {
                            detail = parsed.message;
                        }
                    } catch (_err) { /* body was not JSON */ }
                }
            } catch (_err) { /* body unreadable; the status still explains the failure */ }
        }
        return detail ? headline + ' ' + detail : headline;
    }

    function canSend(text) {
        return (Boolean(String(text || '').trim()) || state.attachments.length > 0) && !state.streaming;
    }

    function makeAbortController() {
        var Ctor = root.AbortController || (typeof AbortController === 'function' ? AbortController : null);
        if (typeof Ctor !== 'function') return null;
        try {
            return new Ctor();
        } catch (_err) {
            return null;
        }
    }

    function requestHistory() {
        // The full conversation stays in state.history (and IndexedDB) for the
        // log; the API accepts at most HISTORY_MAX_ITEMS entries of at most
        // HISTORY_MAX_FIELD_CHARS each. Sending everything used to 422 the
        // 27th turn and every Retry after it.
        var past = state.history.slice(0, -1);
        return past.slice(-HISTORY_MAX_ITEMS).map(function (entry) {
            return {
                role: entry.role,
                content: String(entry.content || '').slice(0, HISTORY_MAX_FIELD_CHARS),
            };
        });
    }

    async function send(text, options) {
        if (state.threadReady) {
            try {
                await state.threadReady;
            } catch (_err) { /* restore is best-effort; never block the turn */ }
        }
        var retrying = Boolean(options && options.retry);
        var images = options && Array.isArray(options.images) ? options.images : [];
        var message = String(text || '').trim();
        if (!message && images.length) message = t('review.agentImageOnly');
        if (!message || state.streaming) return false;
        if (!retrying) {
            disarmRetry();
            state.retryImages = images;
            appendMessage('user', message, images);
            state.history.push({
                role: 'user',
                content: images.length
                    ? message + '\n' + tx('review.agentAttachedHistory', { count: images.length })
                    : message,
            });
        }
        if (isOffline()) {
            showOfflineNotice();
            await persistThread();
            return true;
        }
        var controller = makeAbortController();
        state.abort = controller;
        state.stopRequested = false;
        state.timedOut = false;
        state.streaming = true;
        var assistantRow = appendMessage('assistant', '');
        if (assistantRow && assistantRow.classList) {
            assistantRow.classList.add('ss-agent-msg-streaming');
        }
        syncComposer();
        armIdleTimer();
        var assembled = '';
        var errored = false;
        var sawDone = false;
        var seenToolResults = new Set();
        var requestCursor = state.previousResponseId;
        var requestIdentity = state.previousResponseIdentity;
        if (!requestIdentity || !requestCursor) {
            requestCursor = null;
            requestIdentity = null;
        }
        // A cursor is reusable only after this turn produces a complete, identified
        // primary Responses `done`. History remains authoritative across a cleared cursor.
        clearResponseCursor();
        try {
            var request = {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'text/event-stream',
                },
                body: JSON.stringify({
                    message: message,
                    images: images.map(function (image) {
                        return { media_type: image.media_type, data: image.data };
                    }),
                    history: requestHistory(),
                    previous_response_id: requestCursor,
                    previous_response_provider: requestIdentity && requestIdentity.provider,
                    previous_response_protocol: requestIdentity && requestIdentity.protocol,
                    previous_response_host: requestIdentity && requestIdentity.host,
                }),
            };
            if (controller) request.signal = controller.signal;
            var response = await root.fetch(STREAM_URL, request);
            if (state.stopRequested || state.timedOut) throw abortError();
            if (!response) {
                errored = true;
                showOfflineNotice();
                paintAssistantError(assistantRow, tx('review.agentOffline'));
                return true;
            }
            if (!response.ok) {
                errored = true;
                markRetryable(assistantRow, await readFailureMessage(response), message);
                return true;
            }
            await consumeSse(response, async function (evt) {
                if (!evt) return;
                if (evt.event === 'token') {
                    assembled += (evt.data && evt.data.text) || '';
                    if (assistantRow && !errored) renderRich(assistantRow, assembled);
                    var log = root.document.getElementById('ss-agent-log');
                    if (log && typeof log.scrollTop === 'number') log.scrollTop = log.scrollHeight || 0;
                } else if (evt.event === 'tool_call') {
                    applyToolCall(
                        evt.data && evt.data.name,
                        evt.data && (evt.data.input != null ? evt.data.input : evt.data.arguments)
                    );
                } else if (evt.event === 'tool_result') {
                    await ingestToolResult(
                        evt.data && evt.data.name,
                        evt.data && evt.data.result,
                        seenToolResults,
                        message
                    );
                } else if (evt.event === 'done') {
                    sawDone = true;
                    if (!errored) {
                        acceptDoneCursor(evt.data, requestIdentity);
                    }
                } else if (evt.event === 'error') {
                    errored = true;
                    clearResponseCursor();
                    failTurn(
                        assistantRow,
                        assembled,
                        (evt.data && evt.data.message) || tx('review.agentPatchRetry'),
                        message
                    );
                }
            }, {
                signal: controller ? controller.signal : null,
                onActivity: armIdleTimer,
            });
            if (state.stopRequested || state.timedOut) throw abortError();
            if (!errored && !sawDone) {
                // The server stream ended without its own `done` event: the
                // text on screen is a fragment. Mark it and offer Retry
                // instead of logging it as a finished answer.
                errored = true;
                failTurn(assistantRow, assembled, tx('review.agentStreamCut'), message);
            } else if (assembled && !errored) {
                state.history.push({ role: 'assistant', content: assembled });
            } else if (!errored) {
                removeEmptyAssistant(assistantRow);
            }
        } catch (err) {
            clearResponseCursor();
            if (state.stopRequested) {
                finishInterrupted(assistantRow, assembled);
            } else if (state.timedOut) {
                errored = true;
                failTurn(
                    assistantRow,
                    assembled,
                    tx('review.agentTimeout', { seconds: Math.round(idleTimeoutMs() / 1000) }),
                    message
                );
            } else if (isOffline()) {
                errored = true;
                showOfflineNotice();
                if (assistantRow && !assembled) {
                    paintAssistantError(assistantRow, tx('review.agentOffline'));
                }
            } else {
                errored = true;
                var reason = err && err.message ? String(err.message) : tx('review.agentPatchRetry');
                failTurn(
                    assistantRow,
                    assembled,
                    tx('review.agentDisconnected', { message: reason }),
                    message
                );
            }
        } finally {
            clearIdleTimer();
            state.abort = null;
            state.streaming = false;
            state.stopRequested = false;
            state.timedOut = false;
            if (assistantRow && assistantRow.classList) {
                assistantRow.classList.remove('ss-agent-msg-streaming');
            }
            syncComposer();
            await persistThread();
        }
        return true;
    }

    function startDrag(event) {
        if (state.collapsed || state.sheet) return;
        var target = event && event.target;
        if (target && typeof target.closest === 'function' && target.closest('button')) return;
        var point = event.touches ? event.touches[0] : event;
        state.drag = {
            startX: point.clientX,
            startY: point.clientY,
            origLeft: state.left,
            origTop: state.top,
        };
        if (event.preventDefault) event.preventDefault();
    }

    function moveDrag(event) {
        if (!state.drag) return;
        var point = event.touches ? event.touches[0] : event;
        var view = viewportSize();
        var size = panelSize(view);
        var next = clampToViewport(
            state.drag.origLeft + (point.clientX - state.drag.startX),
            state.drag.origTop + (point.clientY - state.drag.startY),
            size.width,
            size.height,
            view.width,
            view.height
        );
        state.left = next.left;
        state.top = next.top;
        applyDomState();
    }

    function endDrag() {
        if (!state.drag) return;
        state.drag = null;
        persist();
    }

    function isComposerIme(event) {
        return Boolean(event && (event.isComposing || event.keyCode === 229));
    }

    function isComposerEnter(event) {
        if (!event) return false;
        return event.key === 'Enter' || event.keyCode === 13;
    }

    function submitComposer(form) {
        if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
        } else if (typeof form.dispatchEvent === 'function') {
            form.dispatchEvent({ type: 'submit', preventDefault: function () {} });
        } else {
            form.submit();
        }
    }

    function bindComposer(form, input) {
        form.addEventListener('submit', function (event) {
            if (event && event.preventDefault) event.preventDefault();
            var value = input.value;
            if (!canSend(value)) return;
            var images = takeAttachments();
            input.value = '';
            onDraftChanged(input);
            send(value, { images: images });
        });
        input.addEventListener('input', function () { onDraftChanged(input); });
        input.addEventListener('paste', function (event) {
            var files = clipboardImages(event);
            if (!files.length) return;
            if (event.preventDefault) event.preventDefault();
            addImageFiles(files);
        });
        input.addEventListener('keydown', function (event) {
            if (event && event.key === 'Escape') {
                if (state.mic.status === 'recording' || state.mic.status === 'requesting') {
                    if (event.preventDefault) event.preventDefault();
                    cancelMic();
                } else if (state.streaming) {
                    if (event.preventDefault) event.preventDefault();
                    stop();
                }
                return;
            }
            if (!isComposerEnter(event)) return;
            if (event.shiftKey) return;
            if (isComposerIme(event)) return;
            if (event.preventDefault) event.preventDefault();
            submitComposer(form);
        });
    }

    // ---- Screenshot attachments --------------------------------------------

    function clipboardImages(event) {
        var data = event && (event.clipboardData || event.dataTransfer);
        var files = [];
        if (!data) return files;
        var items = data.items || [];
        for (var i = 0; i < items.length; i += 1) {
            var item = items[i];
            if (item && item.kind === 'file' && /^image\//.test(String(item.type || ''))) {
                var file = typeof item.getAsFile === 'function' ? item.getAsFile() : null;
                if (file) files.push(file);
            }
        }
        if (!files.length && data.files) {
            for (var j = 0; j < data.files.length; j += 1) {
                if (/^image\//.test(String(data.files[j].type || ''))) files.push(data.files[j]);
            }
        }
        return files;
    }

    function readAsDataUrl(blob) {
        return new Promise(function (resolve, reject) {
            var reader = new root.FileReader();
            reader.onload = function () { resolve(String(reader.result || '')); };
            reader.onerror = function () { reject(reader.error || new Error('read failed')); };
            reader.readAsDataURL(blob);
        });
    }

    function loadImage(url) {
        return new Promise(function (resolve, reject) {
            var img = new root.Image();
            img.onload = function () { resolve(img); };
            img.onerror = function () { reject(new Error('not an image')); };
            img.src = url;
        });
    }

    function splitDataUrl(url) {
        var match = String(url).match(/^data:([^;,]+);base64,(.*)$/);
        return match ? { media_type: match[1], data: match[2] } : null;
    }

    // Keep small PNG/JPEG screenshots byte-exact (sharp text); re-encode large
    // or exotic ones as JPEG with the long side capped.
    async function prepareImage(file) {
        var original = await readAsDataUrl(file);
        var img = await loadImage(original);
        var width = img.naturalWidth || img.width;
        var height = img.naturalHeight || img.height;
        var keep = (file.type === 'image/png' || file.type === 'image/jpeg')
            && file.size <= ATTACH_KEEP_BYTES
            && Math.max(width, height) <= ATTACH_MAX_SIDE;
        if (keep) {
            var parts = splitDataUrl(original);
            if (parts) return { media_type: parts.media_type, data: parts.data, url: original, name: file.name || '' };
        }
        var scale = Math.min(1, ATTACH_MAX_SIDE / Math.max(width, height || 1));
        var canvas = root.document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(width * scale));
        canvas.height = Math.max(1, Math.round(height * scale));
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        var jpeg = canvas.toDataURL('image/jpeg', ATTACH_JPEG_QUALITY);
        var encoded = splitDataUrl(jpeg);
        if (!encoded) throw new Error('encode failed');
        return { media_type: encoded.media_type, data: encoded.data, url: jpeg, name: file.name || '' };
    }

    function setAttachStatus(message) {
        var status = root.document.getElementById('ss-agent-mic-status');
        if (!status) return;
        status.textContent = message || '';
        status.hidden = !message;
    }

    async function addImageFiles(files) {
        var list = Array.prototype.slice.call(files || []);
        var room = MAX_ATTACHMENTS - state.attachments.length;
        if (room <= 0) {
            setAttachStatus(tx('review.agentAttachLimit', { count: MAX_ATTACHMENTS }));
            return 0;
        }
        if (list.length > room) {
            setAttachStatus(tx('review.agentAttachLimit', { count: MAX_ATTACHMENTS }));
            list = list.slice(0, room);
        }
        var added = 0;
        for (var i = 0; i < list.length; i += 1) {
            try {
                state.attachments.push(await prepareImage(list[i]));
                added += 1;
            } catch (_err) {
                setAttachStatus(tx('review.agentAttachFailed'));
            }
        }
        renderAttachments();
        syncComposer();
        return added;
    }

    function removeAttachment(index) {
        state.attachments.splice(index, 1);
        renderAttachments();
        syncComposer();
    }

    function takeAttachments() {
        var taken = state.attachments;
        state.attachments = [];
        renderAttachments();
        return taken;
    }

    function renderAttachments() {
        var strip = root.document.getElementById('ss-agent-attachments');
        if (!strip) return;
        if (typeof strip.replaceChildren === 'function') strip.replaceChildren();
        else if (strip.children) strip.children.length = 0;
        strip.hidden = state.attachments.length === 0;
        state.attachments.forEach(function (image, index) {
            var chip = el('div', 'ss-agent-attachment');
            var img = el('img', 'ss-agent-attachment-img', { alt: image.name || t('review.agentAttachmentAlt') });
            img.src = image.url;
            var remove = iconButton(
                'ss-agent-attachment-remove-' + index,
                'ss-agent-icon-btn ss-agent-attachment-remove',
                'close',
                tx('review.agentAttachRemove')
            );
            remove.addEventListener('click', function () { removeAttachment(index); });
            chip.appendChild(img);
            chip.appendChild(remove);
            strip.appendChild(chip);
        });
    }

    function autosendKey() {
        return storageKey() + ':autosend';
    }

    function loadAutosend() {
        try {
            return root.localStorage.getItem(autosendKey()) === '1';
        } catch (_err) {
            return false;
        }
    }

    function setAutosend(enabled) {
        state.autosend = Boolean(enabled);
        try {
            if (state.autosend) root.localStorage.setItem(autosendKey(), '1');
            else root.localStorage.removeItem(autosendKey());
        } catch (_err) { /* private mode: the toggle lasts for this page */ }
        var box = root.document.getElementById('ss-agent-autosend');
        if (box) box.checked = state.autosend;
    }

    function autosendDictation() {
        if (!state.autosend || state.streaming) return false;
        var form = root.document.getElementById('ss-agent-form');
        if (!form) return false;
        submitComposer(form);
        return true;
    }

    // ---- Panel size -------------------------------------------------------

    function setPanelSize(width, height, options) {
        var before = panelSize();
        var next = clampSize(width, height);
        // Grow toward the left so a panel docked at the right edge can widen.
        if (!state.sheet && Number.isFinite(state.left)) {
            state.left = state.left + (before.width - next.width);
        }
        state.width = next.width;
        state.height = next.height;
        applyDomState();
        if (!options || options.persist !== false) persist();
        return panelSize();
    }

    function cycleSizePreset() {
        var id = currentPresetId();
        var index = -1;
        for (var i = 0; i < SIZE_PRESETS.length; i += 1) {
            if (SIZE_PRESETS[i].id === id) index = i;
        }
        var next = SIZE_PRESETS[(index + 1) % SIZE_PRESETS.length];
        return setPanelSize(next.width, next.height);
    }

    function presetLabel(id) {
        var key = 'review.agentSize' + id.charAt(0).toUpperCase() + id.slice(1);
        return tx(key);
    }

    function syncSizeControls(size) {
        var doc = root.document;
        if (!doc) return;
        var current = size || panelSize();
        var bounds = sizeBounds();
        var handle = doc.getElementById('ss-agent-resize');
        if (handle && typeof handle.setAttribute === 'function') {
            handle.setAttribute('aria-valuenow', String(current.width));
            handle.setAttribute('aria-valuemin', String(bounds.minWidth));
            handle.setAttribute('aria-valuemax', String(bounds.maxWidth));
            handle.setAttribute('aria-valuetext', tx('review.agentResizeValue', {
                width: current.width,
                height: current.height,
            }));
        }
        var sizeBtn = doc.getElementById('ss-agent-size');
        if (sizeBtn && typeof sizeBtn.setAttribute === 'function') {
            var id = currentPresetId();
            var label = tx('review.agentSize', { size: presetLabel(id) });
            sizeBtn.setAttribute('aria-label', label);
            sizeBtn.setAttribute('data-size', id);
            sizeBtn.title = label;
        }
        autosizeInput(doc.getElementById('ss-agent-input'));
    }

    function startResize(event) {
        if (state.collapsed) return;
        var point = event && event.touches ? event.touches[0] : event;
        if (!point) return;
        var size = panelSize();
        state.resize = {
            startX: point.clientX,
            startY: point.clientY,
            width: size.width,
            height: size.height,
            left: state.left,
        };
        var rootEl = getRoot();
        if (rootEl && rootEl.classList) rootEl.classList.add('ss-agent-resizing');
        var target = event && event.target;
        if (target && typeof target.setPointerCapture === 'function' && event.pointerId != null) {
            try { target.setPointerCapture(event.pointerId); } catch (_err) { /* capture is optional */ }
        }
        if (event && event.preventDefault) event.preventDefault();
    }

    function moveResize(event) {
        var r = state.resize;
        if (!r) return;
        var point = event && event.touches ? event.touches[0] : event;
        if (!point) return;
        // The grip sits on the bottom-left corner: dragging left widens,
        // dragging down makes the panel taller. The right edge stays put.
        var next = clampSize(
            r.width + (r.startX - point.clientX),
            state.sheet ? r.height : r.height + (point.clientY - r.startY)
        );
        state.width = next.width;
        state.height = next.height;
        if (!state.sheet && Number.isFinite(r.left)) state.left = r.left + (r.width - next.width);
        applyDomState();
    }

    function endResize() {
        if (!state.resize) return;
        state.resize = null;
        var rootEl = getRoot();
        if (rootEl && rootEl.classList) rootEl.classList.remove('ss-agent-resizing');
        // Settle placement now: the final size must not cover the player.
        applyDomState();
        persist();
    }

    function onResizeKey(event) {
        var key = event && event.key;
        var step = event && event.shiftKey ? RESIZE_STEP * 4 : RESIZE_STEP;
        var size = panelSize();
        var bounds = sizeBounds();
        var width = size.width;
        var height = size.height;
        if (key === 'ArrowLeft') width += step;
        else if (key === 'ArrowRight') width -= step;
        else if (key === 'ArrowDown') height += step;
        else if (key === 'ArrowUp') height -= step;
        else if (key === 'Home') { width = bounds.minWidth; height = bounds.minHeight; }
        else if (key === 'End') { width = bounds.maxWidth; height = bounds.maxHeight; }
        else return false;
        if (event.preventDefault) event.preventDefault();
        setPanelSize(width, height);
        return true;
    }

    // ---- Dictation ----------------------------------------------------------

    function micSupport() {
        if (isOffline()) return { ok: false, reason: tx('review.agentMicOffline') };
        var nav = root.navigator;
        var lib = root.ScreenScribeLib;
        if (
            !nav
            || !nav.mediaDevices
            || typeof nav.mediaDevices.getUserMedia !== 'function'
            || typeof root.MediaRecorder !== 'function'
            || !lib
            || typeof lib.createSttTransport !== 'function'
        ) {
            return { ok: false, reason: tx('review.agentMicUnsupported') };
        }
        return { ok: true, reason: '' };
    }

    function pickRecorderFormat() {
        var candidates = [
            { mime: 'audio/webm;codecs=opus', blob: 'audio/webm', file: 'dictation.webm' },
            { mime: 'audio/webm', blob: 'audio/webm', file: 'dictation.webm' },
            { mime: 'audio/mp4', blob: 'audio/mp4', file: 'dictation.m4a' },
            { mime: 'audio/ogg;codecs=opus', blob: 'audio/ogg', file: 'dictation.ogg' },
        ];
        var Recorder = root.MediaRecorder;
        if (Recorder && typeof Recorder.isTypeSupported === 'function') {
            for (var i = 0; i < candidates.length; i += 1) {
                try {
                    if (Recorder.isTypeSupported(candidates[i].mime)) return candidates[i];
                } catch (_err) { /* keep probing */ }
            }
        }
        return candidates[0];
    }

    function micErrorText(error) {
        var name = error && error.name;
        if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') {
            return tx('review.agentMicDenied');
        }
        if (name === 'NotFoundError' || name === 'OverconstrainedError' || name === 'DevicesNotFoundError') {
            return tx('review.agentMicMissing');
        }
        if (name === 'NotReadableError' || name === 'TrackStartError') {
            return tx('review.agentMicBusy');
        }
        if (name === 'NotSupportedError') return tx('review.agentMicUnsupported');
        var detail = error && error.message ? String(error.message) : '';
        return tx('review.agentMicFailed', { message: detail });
    }

    function stopMicTicker() {
        if (state.mic.timer) {
            clearInterval(state.mic.timer);
            state.mic.timer = null;
        }
    }

    function micElapsedSeconds() {
        if (!state.mic.startedAt) return 0;
        return Math.max(0, Math.floor((Date.now() - state.mic.startedAt) / 1000));
    }

    function startMicTicker() {
        stopMicTicker();
        state.mic.startedAt = Date.now();
        if (typeof setInterval !== 'function') return;
        state.mic.timer = setInterval(function () {
            if (state.mic.status !== 'recording') {
                stopMicTicker();
                return;
            }
            if (micElapsedSeconds() >= MIC_MAX_SECONDS) {
                finishMic();
                return;
            }
            renderMic();
        }, 1000);
    }

    function micStatusText() {
        var mic = state.mic;
        if (mic.status === 'recording') {
            return tx('review.agentMicRecording', { seconds: micElapsedSeconds() });
        }
        return mic.message || '';
    }

    function renderMic() {
        var doc = root.document;
        if (!doc) return;
        var status = state.mic.status;
        var btn = doc.getElementById('ss-agent-mic');
        var cancel = doc.getElementById('ss-agent-mic-cancel');
        var line = doc.getElementById('ss-agent-mic-status');
        var recording = status === 'recording';
        if (btn) {
            btn.classList.toggle('is-recording', recording);
            btn.classList.toggle('is-busy', status === 'requesting' || status === 'transcribing');
            btn.setAttribute('aria-pressed', recording ? 'true' : 'false');
            btn.setAttribute('aria-label', recording ? tx('review.agentMicStopLabel') : tx('review.agentMicLabel'));
            btn.title = recording ? tx('review.agentMicStopLabel') : tx('review.agentMicLabel');
            var unavailable = status === 'unavailable';
            btn.setAttribute('aria-disabled', unavailable || status === 'transcribing' ? 'true' : 'false');
            btn.setAttribute('data-state', status);
        }
        if (cancel) cancel.hidden = !(recording || status === 'requesting');
        if (line) {
            var text = micStatusText();
            line.textContent = text;
            line.hidden = !text;
            line.setAttribute('data-tone', status === 'error' || status === 'unavailable' ? 'error' : status);
        }
    }

    function setMicState(status, message) {
        state.mic.status = status;
        state.mic.message = message || '';
        if (status !== 'recording') stopMicTicker();
        renderMic();
    }

    function insertDictation(text) {
        var input = root.document.getElementById('ss-agent-input');
        var spoken = String(text || '').trim();
        if (!input || !spoken) return false;
        var value = String(input.value || '');
        var start = Number.isInteger(input.selectionStart) ? input.selectionStart : value.length;
        var end = Number.isInteger(input.selectionEnd) ? input.selectionEnd : start;
        var before = value.slice(0, start);
        var after = value.slice(end);
        var lead = before && !/\s$/.test(before) ? ' ' : '';
        var trail = after && !/^\s/.test(after) ? ' ' : '';
        input.value = before + lead + spoken + trail + after;
        var caret = (before + lead + spoken).length;
        if (typeof input.setSelectionRange === 'function') {
            try { input.setSelectionRange(caret, caret); } catch (_err) { /* not focusable yet */ }
        }
        onDraftChanged(input);
        if (typeof input.focus === 'function') input.focus();
        return true;
    }

    function ensureMicTransport() {
        if (state.mic.transport) return state.mic.transport;
        var format = pickRecorderFormat();
        state.mic.transport = root.ScreenScribeLib.createSttTransport({
            endpoint: '/api/agent/stt',
            mimeType: format.mime,
            blobType: format.blob,
            filename: format.file,
            shouldTranscribe: function (size) { return size > 0; },
            onRecordingStart: function () {
                startMicTicker();
                setMicState('recording', '');
            },
            onMicError: function (error) {
                setMicState('error', micErrorText(error));
            },
            onTranscribingChange: function (busy) {
                if (busy) setMicState('transcribing', tx('review.agentMicTranscribing'));
            },
            onTranscript: function (text) {
                if (insertDictation(text)) {
                    setMicState('idle', tx(state.autosend ? 'review.agentMicSent' : 'review.agentMicInserted'));
                    autosendDictation();
                } else {
                    setMicState('idle', tx('review.agentMicNoSpeech'));
                }
            },
            onError: function (error) {
                var detail = error && error.message ? String(error.message) : '';
                setMicState('error', tx('review.agentMicFailed', { message: detail }));
            },
            onDiscard: function (size) {
                var cancelled = state.mic.cancelling || !size;
                state.mic.cancelling = false;
                setMicState('idle', cancelled ? tx('review.agentMicCancelled') : tx('review.agentMicNoSpeech'));
            },
            fallbackMessage: tx('review.agentMicFailed', { message: '' }),
        });
        return state.mic.transport;
    }

    async function startMic() {
        var support = micSupport();
        if (!support.ok) {
            setMicState('unavailable', support.reason);
            return false;
        }
        var status = state.mic.status;
        if (status === 'requesting' || status === 'transcribing' || status === 'recording') return false;
        var transport = ensureMicTransport();
        state.mic.cancelling = false;
        state.mic.cancelPending = false;
        setMicState('requesting', tx('review.agentMicRequesting'));
        var started = false;
        try {
            started = await transport.start();
        } catch (error) {
            setMicState('error', micErrorText(error));
            return false;
        }
        if (!started) {
            if (state.mic.status === 'requesting') setMicState('error', tx('review.agentMicDenied'));
            return false;
        }
        if (state.mic.cancelPending) {
            state.mic.cancelPending = false;
            cancelMic();
            return false;
        }
        return true;
    }

    function finishMic() {
        if (state.mic.status !== 'recording' || !state.mic.transport) return false;
        stopMicTicker();
        setMicState('transcribing', tx('review.agentMicTranscribing'));
        state.mic.transport.stop();
        return true;
    }

    function cancelMic() {
        var status = state.mic.status;
        if (status === 'requesting') {
            state.mic.cancelPending = true;
            return true;
        }
        if (status !== 'recording' || !state.mic.transport) return false;
        state.mic.cancelling = true;
        stopMicTicker();
        state.mic.transport.cancel();
        if (state.mic.status === 'recording') setMicState('idle', tx('review.agentMicCancelled'));
        return true;
    }

    function toggleMic() {
        if (state.mic.status === 'recording') return Promise.resolve(finishMic());
        return startMic();
    }

    // Small inline icons; built with DOM APIs so no markup string is parsed.
    function icon(name) {
        var doc = root.document;
        var paths = {
            mic: 'M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z',
            stop: 'M7 7h10v10H7z',
            close: 'M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19l5.6-5.6 5.6 5.6 1.4-1.4-5.6-5.6L19 6.4 17.6 5 12 10.6z',
            resize: 'M4 20h2v-2H4zm0-4h2v-2H4zm4 4h2v-2H8zm-4-8h2v-2H4zm4 4h2v-2H8zm4 4h2v-2h-2z',
            size: 'M4 4h7v2H7.4l4.3 4.3-1.4 1.4L6 7.4V11H4zm16 16h-7v-2h3.6l-4.3-4.3 1.4-1.4 4.3 4.3V13h2z',
            image: 'M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2zm0 2v9.6l3.5-3.5 2.5 2.5 4.5-4.5L19 13.6V6H5zm3.5 1.5a1.5 1.5 0 1 1 0 3 1.5 1.5 0 0 1 0-3z',
        };
        if (!doc || typeof doc.createElementNS !== 'function' || !paths[name]) {
            return null;
        }
        var ns = 'http://www.w3.org/2000/svg';
        var svg = doc.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        svg.setAttribute('class', 'ss-agent-icon');
        var path = doc.createElementNS(ns, 'path');
        path.setAttribute('d', paths[name]);
        path.setAttribute('fill', 'currentColor');
        svg.appendChild(path);
        return svg;
    }

    function iconButton(id, className, iconName, label) {
        var btn = el('button', className, { id: id, type: 'button', 'aria-label': label });
        btn.title = label;
        var svg = icon(iconName);
        if (svg) btn.appendChild(svg);
        return btn;
    }

    function buildDom() {
        var doc = root.document;
        if (getRoot()) return;
        state.autosend = loadAutosend();
        var rootEl = el('div', 'ss-agent-root ss-agent-collapsed', { id: 'ss-agent-root' });
        var fab = el('button', 'ss-agent-fab', {
            id: 'ss-agent-fab',
            type: 'button',
            'aria-expanded': 'false',
            'data-i18n-attr': 'aria-label:agentToggle',
        });
        fab.setAttribute('aria-label', tx('review.agentToggle'));
        var logo = el('span', 'ss-agent-logo', { 'data-i18n': 'agentLogo' });
        logo.textContent = t('review.agentLogo');
        fab.appendChild(logo);
        fab.addEventListener('click', function () { toggle(); });

        var panel = el('section', 'ss-agent-panel', {
            id: 'ss-agent-panel',
            hidden: 'hidden',
            'aria-label': tx('review.agentTitle'),
            'data-i18n-attr': 'aria-label:agentTitle',
        });
        var header = el('header', 'ss-agent-header', { id: 'ss-agent-header' });
        var title = el('h2', 'ss-agent-title', { 'data-i18n': 'agentTitle' });
        title.textContent = t('review.agentTitle');
        header.appendChild(title);
        var sizeBtn = iconButton('ss-agent-size', 'ss-agent-icon-btn ss-agent-size', 'size', tx('review.agentSize', {
            size: presetLabel(currentPresetId()),
        }));
        sizeBtn.addEventListener('click', function () { cycleSizePreset(); });
        header.appendChild(sizeBtn);
        header.addEventListener('mousedown', startDrag);
        panel.addEventListener('dragover', function (event) {
            if (!clipboardImages(event).length && !(event && event.dataTransfer
                && Array.prototype.indexOf.call(event.dataTransfer.types || [], 'Files') >= 0)) return;
            if (event.preventDefault) event.preventDefault();
            if (panel.classList) panel.classList.add('ss-agent-dropping');
        });
        panel.addEventListener('dragleave', function () {
            if (panel.classList) panel.classList.remove('ss-agent-dropping');
        });
        panel.addEventListener('drop', function (event) {
            if (panel.classList) panel.classList.remove('ss-agent-dropping');
            var files = clipboardImages(event);
            if (!files.length) return;
            if (event.preventDefault) event.preventDefault();
            addImageFiles(files);
        });
        header.addEventListener('touchstart', startDrag);

        var log = el('div', 'ss-agent-log', {
            id: 'ss-agent-log',
            role: 'log',
            'aria-live': 'polite',
            'aria-relevant': 'additions text',
        });
        var empty = el('p', 'ss-agent-empty', { 'data-i18n': 'agentEmpty' });
        empty.textContent = t('review.agentEmpty');
        log.appendChild(empty);

        var offline = el('p', 'ss-agent-offline', {
            id: 'ss-agent-offline',
            hidden: 'hidden',
            'data-i18n': 'agentOffline',
        });
        offline.textContent = t('review.agentOffline');

        // "Agent is responding…" bar with the Stop control, shown only while a
        // turn streams. role=status announces the start without stealing focus.
        var activity = el('div', 'ss-agent-activity', {
            id: 'ss-agent-activity',
            role: 'status',
            hidden: 'hidden',
        });
        var pulse = el('span', 'ss-agent-pulse', { 'aria-hidden': 'true' });
        var activityText = el('span', 'ss-agent-activity-text', { 'data-i18n': 'agentResponding' });
        activityText.textContent = t('review.agentResponding');
        var stopBtn = el('button', 'ss-agent-stop', {
            id: 'ss-agent-stop',
            type: 'button',
            hidden: 'hidden',
            'aria-label': tx('review.agentStopLabel'),
            'data-i18n-attr': 'aria-label:agentStopLabel',
        });
        var stopIcon = icon('stop');
        if (stopIcon) stopBtn.appendChild(stopIcon);
        var stopText = el('span', '', { 'data-i18n': 'agentStop' });
        stopText.textContent = t('review.agentStop');
        stopBtn.appendChild(stopText);
        stopBtn.addEventListener('click', function () { stop(); });
        activity.appendChild(pulse);
        activity.appendChild(activityText);
        activity.appendChild(stopBtn);

        var form = el('form', 'ss-agent-form', { id: 'ss-agent-form' });
        var composer = el('div', 'ss-agent-composer');
        var hintId = 'ss-agent-composer-hint';
        var input = el('textarea', 'ss-agent-input', {
            id: 'ss-agent-input',
            rows: '1',
            'data-i18n': 'agentPlaceholder',
            'aria-label': tx('review.agentPlaceholder'),
            'data-i18n-attr': 'aria-label:agentPlaceholder',
            'aria-describedby': hintId,
            enterkeyhint: 'send',
        });
        input.placeholder = tx('review.agentPlaceholder');
        var actions = el('div', 'ss-agent-actions');
        var micBtn = iconButton('ss-agent-mic', 'ss-agent-icon-btn ss-agent-mic', 'mic', tx('review.agentMicLabel'));
        micBtn.setAttribute('aria-pressed', 'false');
        micBtn.addEventListener('click', function () { toggleMic(); });
        var micCancel = iconButton(
            'ss-agent-mic-cancel',
            'ss-agent-icon-btn ss-agent-mic-cancel',
            'close',
            tx('review.agentMicCancelLabel')
        );
        micCancel.hidden = true;
        micCancel.addEventListener('click', function () { cancelMic(); });
        var attachBtn = iconButton(
            'ss-agent-attach',
            'ss-agent-icon-btn ss-agent-attach',
            'image',
            tx('review.agentAttachLabel')
        );
        var fileInput = el('input', 'ss-agent-visually-hidden', {
            id: 'ss-agent-file',
            type: 'file',
            accept: 'image/png,image/jpeg,image/webp,image/gif',
            multiple: 'multiple',
            tabindex: '-1',
            'aria-hidden': 'true',
        });
        attachBtn.addEventListener('click', function () {
            if (typeof fileInput.click === 'function') fileInput.click();
        });
        fileInput.addEventListener('change', function () {
            var files = fileInput.files;
            addImageFiles(files).then(function () { fileInput.value = ''; });
        });
        var sendBtn = el('button', 'ss-agent-send', {
            id: 'ss-agent-send',
            type: 'submit',
            'data-i18n': 'agentSend',
            'aria-label': tx('review.agentSendLabel'),
        });
        sendBtn.textContent = t('review.agentSend');
        actions.appendChild(micCancel);
        actions.appendChild(attachBtn);
        actions.appendChild(micBtn);
        actions.appendChild(sendBtn);
        composer.appendChild(input);
        composer.appendChild(actions);
        var attachments = el('div', 'ss-agent-attachments', {
            id: 'ss-agent-attachments',
            hidden: 'hidden',
            'aria-label': tx('review.agentAttachmentsLabel'),
        });

        var foot = el('div', 'ss-agent-composer-foot');
        var micStatus = el('p', 'ss-agent-mic-status', {
            id: 'ss-agent-mic-status',
            role: 'status',
            'aria-live': 'polite',
            hidden: 'hidden',
        });
        var hint = el('span', 'ss-agent-hint', { id: hintId, 'data-i18n': 'agentComposerHint' });
        hint.textContent = t('review.agentComposerHint');
        var autosendLabel = el('label', 'ss-agent-autosend');
        var autosendBox = el('input', '', { id: 'ss-agent-autosend', type: 'checkbox' });
        autosendBox.checked = state.autosend;
        autosendBox.addEventListener('change', function () { setAutosend(autosendBox.checked); });
        var autosendText = el('span', '', { 'data-i18n': 'agentAutosend' });
        autosendText.textContent = t('review.agentAutosend');
        autosendLabel.appendChild(autosendBox);
        autosendLabel.appendChild(autosendText);
        foot.appendChild(micStatus);
        foot.appendChild(autosendLabel);
        foot.appendChild(hint);
        form.appendChild(attachments);
        form.appendChild(composer);
        form.appendChild(foot);
        form.appendChild(fileInput);
        bindComposer(form, input);

        var resizeHandle = el('div', 'ss-agent-resize', {
            id: 'ss-agent-resize',
            role: 'separator',
            tabindex: '0',
            'aria-orientation': 'vertical',
            'aria-label': tx('review.agentResize'),
            'aria-describedby': 'ss-agent-resize-hint',
        });
        resizeHandle.title = tx('review.agentResizeHint');
        var grip = icon('resize');
        if (grip) resizeHandle.appendChild(grip);
        var resizeHint = el('span', 'ss-agent-visually-hidden', { id: 'ss-agent-resize-hint' });
        resizeHint.textContent = t('review.agentResizeHint');
        resizeHandle.addEventListener('pointerdown', startResize);
        resizeHandle.addEventListener('keydown', onResizeKey);
        resizeHandle.addEventListener('dblclick', function () {
            setPanelSize(PANEL_WIDTH, PANEL_HEIGHT);
        });

        panel.appendChild(header);
        panel.appendChild(offline);
        panel.appendChild(log);
        panel.appendChild(activity);
        panel.appendChild(form);
        panel.appendChild(resizeHandle);
        panel.appendChild(resizeHint);
        rootEl.appendChild(fab);
        rootEl.appendChild(panel);
        (doc.body || doc.documentElement).appendChild(rootEl);

        var draft = loadDraft();
        if (draft) input.value = draft;

        doc.addEventListener('mousemove', moveDrag);
        doc.addEventListener('mouseup', endDrag);
        doc.addEventListener('touchmove', moveDrag);
        doc.addEventListener('touchend', endDrag);
        doc.addEventListener('pointermove', moveResize);
        doc.addEventListener('pointerup', endResize);
        doc.addEventListener('pointercancel', endResize);
        bindLayoutListeners();
        var support = micSupport();
        if (!support.ok) setMicState('unavailable', '');
        syncComposer();
    }

    function observePlayer(node) {
        if (!node || !state.playerObserver || typeof state.playerObserver.observe !== 'function') return;
        try {
            state.playerObserver.observe(node);
        } catch (_err) { /* ignore unobservable nodes in tests */ }
    }

    function bindLayoutListeners() {
        var doc = root.document;
        if (root.addEventListener) {
            root.addEventListener('resize', function () { relayout(); });
        }
        var video = doc.getElementById('videoPlayer');
        if (video && typeof video.addEventListener === 'function') {
            video.addEventListener('loadedmetadata', function () { relayout(); });
        }
        if (typeof root.ResizeObserver === 'function' && !state.playerObserver) {
            state.playerObserver = new root.ResizeObserver(function () { relayout(); });
            observePlayer(video);
            observePlayer(doc.getElementById('videoControls'));
            observePlayer(doc.querySelector('.video-controls-pro'));
            observePlayer(doc.querySelector('.video-container'));
            observePlayer(doc.querySelector('.video-panel'));
        }
    }

    function init() {
        restore();
        buildDom();
        applyDomState();
        bindReferenceListener();
        if (isOffline()) {
            showOfflineNotice();
        }
        if (typeof applyTranslations === 'function') {
            applyTranslations(getRoot());
        }
        state.threadReady = restoreThread();
    }

    var api = {
        STORAGE_KEY: STORAGE_KEY,
        STREAM_URL: STREAM_URL,
        PANEL_WIDTH: PANEL_WIDTH,
        PANEL_HEIGHT: PANEL_HEIGHT,
        parseSseBlock: parseSseBlock,
        parseSseStream: parseSseStream,
        applyToolCall: applyToolCall,
        ingestToolResult: ingestToolResult,
        applyOpsAndSave: applyOpsAndSave,
        renderPlanCard: renderPlanCard,
        placeBesidePlayer: placeBesidePlayer,
        clampToViewport: clampToViewport,
        rectsOverlap: rectsOverlap,
        protectedRect: protectedRect,
        resolveExpandedPlacement: resolveExpandedPlacement,
        applyDomState: applyDomState,
        relayout: relayout,
        persist: persist,
        restore: restore,
        setCollapsed: setCollapsed,
        toggle: toggle,
        isOffline: isOffline,
        send: send,
        init: init,
        getState: function () { return state; },
        formatReference: formatReference,
        insertReference: insertReference,
        restoreThread: restoreThread,
        threadKey: threadKey,
        requestHistory: requestHistory,
        whenThreadReady: function () { return state.threadReady || Promise.resolve(false); },
        stop: stop,
        setPanelSize: setPanelSize,
        panelSize: panelSize,
        sizeBounds: sizeBounds,
        cycleSizePreset: cycleSizePreset,
        onResizeKey: onResizeKey,
        startResize: startResize,
        moveResize: moveResize,
        endResize: endResize,
        startMic: startMic,
        finishMic: finishMic,
        cancelMic: cancelMic,
        toggleMic: toggleMic,
        micSupport: micSupport,
        insertDictation: insertDictation,
        mdBlocks: mdBlocks,
        mdInline: mdInline,
        renderRich: renderRich,
        addImageFiles: addImageFiles,
        takeAttachments: takeAttachments,
        setAutosend: setAutosend,
        autosendDictation: autosendDictation,
        MAX_ATTACHMENTS: MAX_ATTACHMENTS,
        syncComposer: syncComposer,
        IDLE_TIMEOUT_MS: IDLE_TIMEOUT_MS,
        MIN_WIDTH: MIN_WIDTH,
        MIN_HEIGHT: MIN_HEIGHT,
    };
    root.screenscribeAgentPanel = api;

    if (!root.__screenscribeAgentPanelSkipInit && root.document) {
        if (root.document.readyState === 'loading' && root.document.addEventListener) {
            root.document.addEventListener('DOMContentLoaded', function () { init(); });
        } else {
            init();
        }
    }
})(typeof window !== 'undefined' ? window : globalThis);
