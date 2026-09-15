// Floating Screenscribe agent chat for the HTML review report.
// Separate from review_app.js so W1-01/W1-05 can merge with one player hook.
// SSE contract (w1-04): token / tool_call / tool_result / done / error.
(function (root) {
    'use strict';

    var STORAGE_KEY = 'screenscribe:agent-panel';
    var STREAM_URL = '/api/agent/chat/stream';
    var PANEL_WIDTH = 360;
    var PANEL_HEIGHT = 480;
    var FAB_SIZE = 56;
    var GAP = 16;
    var VIEW_MARGIN = 8;
    var SHEET_CLASS = 'ss-agent-sheet';

    var state = {
        collapsed: true,
        left: null,
        top: null,
        sheet: false,
        history: [],
        previousResponseId: null,
        streaming: false,
        drag: null,
        playerObserver: null,
        patchOfflineAnnounced: false,
    };

    function tx(key, args) {
        var fn = typeof t === 'function' ? t : root.t;
        if (typeof fn === 'function') {
            return fn(key, args);
        }
        return key;
    }

    function storageKey() {
        var loc = root.location || {};
        return STORAGE_KEY + ':' + String(loc.origin || '') + String(loc.pathname || '');
    }

    function persist() {
        try {
            root.localStorage.setItem(storageKey(), JSON.stringify({
                collapsed: state.collapsed,
                left: state.sheet ? null : state.left,
                top: state.sheet ? null : state.top,
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

    async function applyOpsAndSave(ops, explain) {
        if (isOffline()) {
            appendSystemLine(tx('review.agentPatchOffline'), 'ss-agent-msg-warn');
            return { ok: false, offline: true };
        }
        var host = getAgentHost();
        if (typeof host.applyReviewPatch !== 'function') {
            appendSystemLine(tx('review.agentPatchSaveFailed', { message: 'applyReviewPatch' }), 'ss-agent-msg-error');
            return { ok: false };
        }
        var results = await host.applyReviewPatch(ops);
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

    function renderPlanCard(plan) {
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
            }))).then(function () {
                var summary = tx('review.agentPlanApplied', {
                    applied: selected.length,
                    total: ops.length,
                });
                appendMessage('user', summary);
                state.history.push({ role: 'user', content: summary });
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

    async function ingestToolResult(name, result, seen) {
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
            renderPlanCard(payload);
            return;
        }
        if (payload.type === 'review_patch') {
            await applyOpsAndSave(payload.ops || [], payload.explain || '');
        }
    }

    function isOffline() {
        try {
            if (root.location && root.location.protocol === 'file:') return true;
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
            htmlEl.style.setProperty('--ss-agent-sheet-width', PANEL_WIDTH + 'px');
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
        var obstacle = protectedRect();
        var probe = obstacle;
        if (state.sheet) {
            probe = unionBoxes(obstacle, {
                left: obstacle.right,
                top: obstacle.top,
                right: Math.min(view.width, obstacle.right + PANEL_WIDTH + GAP),
                bottom: obstacle.bottom,
                width: 0,
                height: 0,
            }) || obstacle;
        }
        var savedLeft = state.drag ? state.left : (state.sheet ? null : state.left);
        var savedTop = state.drag ? state.top : (state.sheet ? null : state.top);
        if (state.drag && Number.isFinite(state.left) && Number.isFinite(state.top)) {
            var dragged = positionClear(state.left, state.top, PANEL_WIDTH, PANEL_HEIGHT, obstacle, view);
            if (dragged) {
                state.left = dragged.left;
                state.top = dragged.top;
                state.sheet = false;
            } else {
                var placedDrag = placeBesidePlayer(obstacle, view, { width: PANEL_WIDTH, height: PANEL_HEIGHT });
                state.left = placedDrag.left;
                state.top = placedDrag.top;
                state.sheet = Boolean(placedDrag.sheet);
            }
        } else {
            var placed = resolveExpandedPlacement(
                probe,
                view,
                { width: PANEL_WIDTH, height: PANEL_HEIGHT },
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
            rootEl.style.width = PANEL_WIDTH + 'px';
            rootEl.style.height = '';
        } else {
            rootEl.style.right = 'auto';
            rootEl.style.left = state.left + 'px';
            rootEl.style.top = state.top + 'px';
            rootEl.style.width = PANEL_WIDTH + 'px';
            rootEl.style.height = PANEL_HEIGHT + 'px';
        }
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

    function appendMessage(role, text) {
        var log = root.document.getElementById('ss-agent-log');
        if (!log) return null;
        var row = el('div', 'ss-agent-msg ss-agent-msg-' + role);
        row.textContent = text;
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

    async function consumeSse(response, onEvent) {
        var body = response && response.body;
        if (body && typeof body.getReader === 'function') {
            var reader = body.getReader();
            var decoder = new TextDecoder();
            var buffer = '';
            while (true) {
                var chunk = await reader.read();
                if (chunk.done) break;
                buffer += decoder.decode(chunk.value, { stream: true });
                var idx;
                while ((idx = buffer.indexOf('\n\n')) !== -1) {
                    var block = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    if (block.trim()) await onEvent(parseSseBlock(block));
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

    function canSend(text) {
        return Boolean(String(text || '').trim()) && !state.streaming;
    }

    async function send(text) {
        var message = String(text || '').trim();
        if (!message || state.streaming) return false;
        appendMessage('user', message);
        state.history.push({ role: 'user', content: message });
        if (isOffline()) {
            showOfflineNotice();
            return true;
        }
        state.streaming = true;
        var assistantRow = appendMessage('assistant', '');
        var assembled = '';
        var errored = false;
        var seenToolResults = new Set();
        try {
            var response = await root.fetch(STREAM_URL, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Accept: 'text/event-stream',
                },
                body: JSON.stringify({
                    message: message,
                    history: state.history.slice(0, -1),
                    previous_response_id: state.previousResponseId,
                }),
            });
            if (!response || !response.ok) {
                var fallback = tx('review.agentOffline');
                paintAssistantError(assistantRow, fallback);
                var notice = root.document.getElementById('ss-agent-offline');
                if (notice && /screenscribe serve/i.test(String(fallback))) {
                    notice.hidden = false;
                    notice.textContent = fallback;
                }
                state.streaming = false;
                return true;
            }
            await consumeSse(response, async function (evt) {
                if (!evt) return;
                if (evt.event === 'token') {
                    assembled += (evt.data && evt.data.text) || '';
                    if (assistantRow) assistantRow.textContent = assembled;
                } else if (evt.event === 'tool_call') {
                    applyToolCall(
                        evt.data && evt.data.name,
                        evt.data && (evt.data.input != null ? evt.data.input : evt.data.arguments)
                    );
                } else if (evt.event === 'tool_result') {
                    await ingestToolResult(
                        evt.data && evt.data.name,
                        evt.data && evt.data.result,
                        seenToolResults
                    );
                } else if (evt.event === 'done') {
                    if (evt.data && evt.data.response_id) {
                        state.previousResponseId = evt.data.response_id;
                    }
                } else if (evt.event === 'error') {
                    errored = true;
                    paintAssistantError(
                        assistantRow,
                        (evt.data && evt.data.message) || tx('review.agentOffline')
                    );
                }
            });
            if (assembled) {
                state.history.push({ role: 'assistant', content: assembled });
            } else if (!errored) {
                removeEmptyAssistant(assistantRow);
            }
        } catch (_err) {
            showOfflineNotice();
            if (assistantRow && !assembled) {
                paintAssistantError(assistantRow, tx('review.agentOffline'));
            }
        }
        state.streaming = false;
        return true;
    }

    function startDrag(event) {
        if (state.collapsed || state.sheet) return;
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
        var next = clampToViewport(
            state.drag.origLeft + (point.clientX - state.drag.startX),
            state.drag.origTop + (point.clientY - state.drag.startY),
            PANEL_WIDTH,
            PANEL_HEIGHT,
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

    function bindComposer(form, input) {
        form.addEventListener('submit', function (event) {
            if (event && event.preventDefault) event.preventDefault();
            var value = input.value;
            if (!canSend(value)) return;
            input.value = '';
            send(value);
        });
        input.addEventListener('keydown', function (event) {
            if (!isComposerEnter(event)) return;
            if (event.shiftKey) return;
            if (isComposerIme(event)) return;
            if (event.preventDefault) event.preventDefault();
            if (typeof form.requestSubmit === 'function') {
                form.requestSubmit();
            } else if (typeof form.dispatchEvent === 'function') {
                form.dispatchEvent({ type: 'submit', preventDefault: function () {} });
            } else {
                form.submit();
            }
        });
    }

    function buildDom() {
        var doc = root.document;
        if (getRoot()) return;
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
        header.addEventListener('mousedown', startDrag);
        header.addEventListener('touchstart', startDrag);

        var log = el('div', 'ss-agent-log', { id: 'ss-agent-log' });
        var empty = el('p', 'ss-agent-empty', { 'data-i18n': 'agentEmpty' });
        empty.textContent = t('review.agentEmpty');
        log.appendChild(empty);

        var offline = el('p', 'ss-agent-offline', { id: 'ss-agent-offline', hidden: 'hidden' });
        offline.textContent = t('review.agentOffline');

        var form = el('form', 'ss-agent-form', { id: 'ss-agent-form' });
        var input = el('textarea', 'ss-agent-input', {
            id: 'ss-agent-input',
            rows: '2',
            'data-i18n': 'agentPlaceholder',
        });
        input.placeholder = tx('review.agentPlaceholder');
        var sendBtn = el('button', 'ss-agent-send', { type: 'submit', 'data-i18n': 'agentSend' });
        sendBtn.textContent = t('review.agentSend');
        form.appendChild(input);
        form.appendChild(sendBtn);
        bindComposer(form, input);

        panel.appendChild(header);
        panel.appendChild(offline);
        panel.appendChild(log);
        panel.appendChild(form);
        rootEl.appendChild(fab);
        rootEl.appendChild(panel);
        (doc.body || doc.documentElement).appendChild(rootEl);

        doc.addEventListener('mousemove', moveDrag);
        doc.addEventListener('mouseup', endDrag);
        doc.addEventListener('touchmove', moveDrag);
        doc.addEventListener('touchend', endDrag);
        bindLayoutListeners();
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
        if (isOffline()) {
            showOfflineNotice();
        }
        if (typeof applyTranslations === 'function') {
            applyTranslations(getRoot());
        }
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
    };
    root.ScreenScribeAgentPanel = api;

    if (!root.__screenscribeAgentPanelSkipInit && root.document) {
        if (root.document.readyState === 'loading' && root.document.addEventListener) {
            root.document.addEventListener('DOMContentLoaded', function () { init(); });
        } else {
            init();
        }
    }
})(typeof window !== 'undefined' ? window : globalThis);
