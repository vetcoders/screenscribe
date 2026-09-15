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

    var state = {
        collapsed: true,
        left: null,
        top: null,
        history: [],
        previousResponseId: null,
        streaming: false,
        drag: null,
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
                left: state.left,
                top: state.top,
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
        return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    }

    function placeBesidePlayer(playerRect, viewport, panelSize) {
        var w = panelSize.width;
        var h = panelSize.height;
        var vw = viewport.width;
        var vh = viewport.height;
        var left;
        var spaceRight = vw - playerRect.right - GAP;
        var spaceLeft = playerRect.left - GAP;
        if (spaceRight >= w) {
            left = playerRect.right + GAP;
        } else if (spaceLeft >= w) {
            left = playerRect.left - GAP - w;
        } else {
            left = vw - w - GAP;
        }
        var placed = clampToViewport(left, playerRect.top, w, h, vw, vh);
        var panelBox = {
            left: placed.left,
            top: placed.top,
            right: placed.left + w,
            bottom: placed.top + h,
        };
        if (rectsOverlap(panelBox, playerRect) && (playerRect.bottom + GAP + h) <= vh) {
            placed = clampToViewport(placed.left, playerRect.bottom + GAP, w, h, vw, vh);
        }
        return placed;
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
        var player = doc.getElementById('videoPlayer')
            || doc.querySelector('.main-column')
            || doc.querySelector('.video-panel');
        if (player && typeof player.getBoundingClientRect === 'function') {
            return player.getBoundingClientRect();
        }
        return { left: 16, top: 96, right: 640, bottom: 480, width: 624, height: 384 };
    }

    function defaultExpandedPosition() {
        var view = viewportSize();
        return placeBesidePlayer(playerRect(), view, { width: PANEL_WIDTH, height: PANEL_HEIGHT });
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
            rootEl.style.width = FAB_SIZE + 'px';
            rootEl.style.height = FAB_SIZE + 'px';
            if (panel) panel.hidden = true;
            return;
        }
        var pos = (state.left == null || state.top == null)
            ? defaultExpandedPosition()
            : clampToViewport(state.left, state.top, PANEL_WIDTH, PANEL_HEIGHT, view.width, view.height);
        state.left = pos.left;
        state.top = pos.top;
        rootEl.style.left = pos.left + 'px';
        rootEl.style.top = pos.top + 'px';
        rootEl.style.width = PANEL_WIDTH + 'px';
        rootEl.style.height = PANEL_HEIGHT + 'px';
        if (panel) panel.hidden = false;
    }

    function setCollapsed(next) {
        state.collapsed = Boolean(next);
        if (!state.collapsed && (state.left == null || state.top == null)) {
            var placed = defaultExpandedPosition();
            state.left = placed.left;
            state.top = placed.top;
        }
        applyDomState();
        persist();
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

    function showOfflineNotice() {
        var notice = root.document.getElementById('ss-agent-offline');
        var message = tx('review.agentOffline');
        if (notice) {
            notice.hidden = false;
            notice.textContent = message;
        } else {
            appendMessage('system', message);
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
                    if (block.trim()) onEvent(parseSseBlock(block));
                }
            }
            if (buffer.trim()) onEvent(parseSseBlock(buffer));
            return;
        }
        var text = typeof response.text === 'function' ? await response.text() : '';
        parseSseStream(text).forEach(onEvent);
    }

    async function send(text) {
        var message = String(text || '').trim();
        if (!message || state.streaming) return;
        appendMessage('user', message);
        state.history.push({ role: 'user', content: message });
        if (isOffline()) {
            showOfflineNotice();
            return;
        }
        state.streaming = true;
        var assistantRow = appendMessage('assistant', '');
        var assembled = '';
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
                showPanelError(fallback);
                state.streaming = false;
                return;
            }
            await consumeSse(response, function (evt) {
                if (!evt) return;
                if (evt.event === 'token') {
                    assembled += (evt.data && evt.data.text) || '';
                    if (assistantRow) assistantRow.textContent = assembled;
                } else if (evt.event === 'tool_call') {
                    applyToolCall(
                        evt.data && evt.data.name,
                        evt.data && (evt.data.input != null ? evt.data.input : evt.data.arguments)
                    );
                } else if (evt.event === 'done') {
                    if (evt.data && evt.data.response_id) {
                        state.previousResponseId = evt.data.response_id;
                    }
                } else if (evt.event === 'error') {
                    showPanelError((evt.data && evt.data.message) || tx('review.agentOffline'));
                }
            });
            if (assembled) {
                state.history.push({ role: 'assistant', content: assembled });
            }
        } catch (_err) {
            showOfflineNotice();
        }
        state.streaming = false;
    }

    function startDrag(event) {
        if (state.collapsed) return;
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

    function bindComposer(form, input) {
        form.addEventListener('submit', function (event) {
            if (event && event.preventDefault) event.preventDefault();
            send(input.value);
            input.value = '';
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
        if (root.addEventListener) {
            root.addEventListener('resize', function () { applyDomState(); });
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
        placeBesidePlayer: placeBesidePlayer,
        clampToViewport: clampToViewport,
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
