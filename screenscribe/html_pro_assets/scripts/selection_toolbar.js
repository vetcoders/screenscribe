// Selection reference toolbar for the HTML review report.
// Emits screenscribe:agent-insert-reference. No listener → silent no-op.
(function (root) {
    'use strict';

    var EVENT_NAME = 'screenscribe:agent-insert-reference';
    var QUOTE_CAP = 2000;
    var HIDE_DELAY_MS = 250;
    var doc = root.document;
    var state = { envelope: null, timer: 0 };

    function tx(key) {
        var fn = typeof t === 'function' ? t : root.t;
        if (typeof fn === 'function') return fn(key);
        return key;
    }

    function asElement(node) {
        var current = node;
        while (current && !current.tagName) current = current.parentNode;
        return current || null;
    }

    function closestClass(node, className) {
        var el = asElement(node);
        while (el) {
            if (el.classList && el.classList.contains(className)) return el;
            el = el.parentNode;
        }
        return null;
    }

    function closestTag(node, tag) {
        var el = asElement(node);
        var want = String(tag || '').toUpperCase();
        while (el) {
            if (el.tagName === want) return el;
            el = el.parentNode;
        }
        return null;
    }

    function datasetKey(name) {
        return name.replace(/-([a-z])/g, function (_match, chr) {
            return chr.toUpperCase();
        });
    }

    function attr(el, name) {
        if (!el || !name) return null;
        if (typeof el.getAttribute === 'function') {
            var value = el.getAttribute(name);
            if (value != null && String(value) !== '') return String(value);
        }
        if (name.indexOf('data-') === 0 && el.dataset) {
            var fromDataset = el.dataset[datasetKey(name.slice(5))];
            if (fromDataset != null && String(fromDataset) !== '') return String(fromDataset);
        }
        return null;
    }

    function previousDt(dd) {
        var sib = dd.previousElementSibling || null;
        if (!sib && dd.parentNode && dd.parentNode.children) {
            var kids = dd.parentNode.children;
            for (var i = 0; i < kids.length; i++) {
                if (kids[i] === dd && i > 0) {
                    sib = kids[i - 1];
                    break;
                }
            }
        }
        while (sib && sib.tagName !== 'DT') {
            sib = sib.previousElementSibling || null;
        }
        return sib && sib.tagName === 'DT' ? sib : null;
    }

    function fieldFor(el) {
        if (!el) return null;
        if (closestTag(el, 'INPUT') || closestTag(el, 'TEXTAREA') || closestTag(el, 'SELECT') || closestTag(el, 'BUTTON')) {
            return null;
        }
        if (closestClass(el, 'human-review') || closestClass(el, 'ss-selection-toolbar')) return null;
        if (closestClass(el, 'executive-summary')) return 'executive_summary';
        if (closestClass(el, 'subtitle-item') || closestClass(el, 'subtitle-list') || closestClass(el, 'transcript-list')) {
            return 'subtitle';
        }
        if (closestClass(el, 'finding-transcript') || closestClass(el, 'merged-frame-transcript')) return 'transcript';
        if (closestClass(el, 'finding-summary')) return 'summary';
        var dd = closestTag(el, 'DD');
        if (dd) {
            var dt = previousDt(dd);
            var key = dt ? attr(dt, 'data-i18n') : null;
            if (key === 'suggestedFix') return 'suggested_fix';
            if (key === 'visualIssues') return 'issues';
        }
        if (closestClass(el, 'finding')) return 'summary';
        return null;
    }

    function closestFindingId(el) {
        var current = asElement(el);
        while (current) {
            var id = attr(current, 'data-finding-id');
            if (id != null) return id;
            current = current.parentNode;
        }
        return null;
    }

    function headingFallback(el) {
        var card = closestClass(el, 'finding');
        if (!card) return null;
        var title = null;
        var kids = [];
        function walk(node) {
            if (!node || title) return;
            if (node.classList && node.classList.contains('finding-title')) {
                title = node;
                return;
            }
            var children = node.children || [];
            for (var i = 0; i < children.length; i++) walk(children[i]);
        }
        walk(card);
        if (!title || title.textContent == null) return null;
        var text = String(title.textContent).replace(/\s+/g, ' ').trim();
        return text || null;
    }

    function segmentsFrom(documentRef) {
        var view = (documentRef && documentRef.defaultView) || root;
        var segments = view && view.TRANSCRIPT_SEGMENTS;
        return Array.isArray(segments) ? segments : [];
    }

    function timestampFor(el, documentRef) {
        var cue = closestClass(el, 'subtitle-item');
        if (cue) {
            var direct = attr(cue, 'data-start') || attr(cue, 'data-timestamp');
            if (direct != null) return direct;
            var segId = attr(cue, 'data-segment-id');
            if (segId == null) return null;
            var segments = segmentsFrom(documentRef);
            for (var i = 0; i < segments.length; i++) {
                var segment = segments[i];
                if (!segment || String(segment.id) !== segId || segment.start == null) continue;
                return String(segment.start);
            }
            return null;
        }
        var card = closestClass(el, 'finding');
        if (!card) return null;
        var meta = null;
        function walk(node) {
            if (!node || meta) return;
            if (node.classList && node.classList.contains('finding-meta')) {
                meta = node;
                return;
            }
            var children = node.children || [];
            for (var i = 0; i < children.length; i++) walk(children[i]);
        }
        walk(card);
        if (!meta) return null;
        return attr(meta, 'data-timestamp');
    }

    function selectionRect(selection) {
        try {
            if (selection && selection.getRangeAt && selection.rangeCount) {
                var range = selection.getRangeAt(0);
                if (range && typeof range.getBoundingClientRect === 'function') {
                    return range.getBoundingClientRect();
                }
            }
        } catch (_err) {
            // A detached range is not a reason to throw into the report.
        }
        return { left: 8, top: 8, right: 8, bottom: 8, width: 0, height: 0 };
    }

    function anchorNode(selection) {
        if (!selection) return null;
        if (selection.anchorNode) return selection.anchorNode;
        if (selection.rangeCount && typeof selection.getRangeAt === 'function') {
            var range = selection.getRangeAt(0);
            if (range) return range.startContainer || range.commonAncestorContainer || null;
        }
        return null;
    }

    function envelopeForSelection(selection, documentRef) {
        if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null;
        var raw = '';
        try {
            raw = typeof selection.toString === 'function' ? String(selection.toString()) : '';
        } catch (_err) {
            raw = '';
        }
        if (!raw.replace(/\u00a0/g, ' ').trim()) return null;
        var el = asElement(anchorNode(selection));
        if (!el) return null;
        var field = fieldFor(el);
        if (!field) return null;
        var momentId = null;
        if (field !== 'executive_summary' && field !== 'subtitle') {
            momentId = closestFindingId(el);
            if (momentId == null) momentId = headingFallback(el);
        }
        var transcriptError = Boolean(
            closestClass(el, 'finding-transcript')
            || closestClass(el, 'subtitle-list')
            || closestClass(el, 'subtitle-item')
            || closestClass(el, 'transcript-list')
        );
        return {
            moment_id: momentId,
            field: field,
            quote: raw.length > QUOTE_CAP ? raw.slice(0, QUOTE_CAP) : raw,
            timestamp: timestampFor(el, documentRef || doc),
            transcriptError: transcriptError,
            rect: selectionRect(selection),
        };
    }

    function currentSelection() {
        if (doc && typeof doc.getSelection === 'function') return doc.getSelection();
        if (typeof root.getSelection === 'function') return root.getSelection();
        return null;
    }

    function findAction(bar, action) {
        var children = bar.children || [];
        for (var i = 0; i < children.length; i++) {
            if (children[i].getAttribute && children[i].getAttribute('data-action') === action) {
                return children[i];
            }
        }
        return null;
    }

    function place(bar, rect) {
        var holder = bar.parentNode || bar;
        var margin = 8;
        var height = bar.offsetHeight || 36;
        var width = bar.offsetWidth || 280;
        var box = rect || { left: margin, top: margin, bottom: margin };
        var top = (box.top || 0) - height - margin;
        var left = box.left || margin;
        if (top < margin) top = (box.bottom || 0) + margin;
        var viewW = root.innerWidth || (doc.documentElement && doc.documentElement.clientWidth) || 1024;
        if (left + width > viewW - margin) left = Math.max(margin, viewW - margin - width);
        if (left < margin) left = margin;
        holder.style.top = Math.round(top) + 'px';
        holder.style.left = Math.round(left) + 'px';
    }

    function applyEnvelope(env) {
        state.envelope = env;
        var bar = doc.getElementById('ss-selection-toolbar');
        if (!bar) return;
        if (!env) {
            bar.classList.remove('is-open');
            bar.hidden = true;
            return;
        }
        bar.hidden = false;
        bar.classList.add('is-open');
        var err = findAction(bar, 'transcript-error');
        if (err) err.hidden = !env.transcriptError;
        place(bar, env.rect);
    }

    function syncFromSelection() {
        var selection = currentSelection();
        var el = asElement(anchorNode(selection));
        if (el && closestClass(el, 'ss-selection-toolbar')) return;
        applyEnvelope(envelopeForSelection(selection, doc));
    }

    function schedule() {
        if (state.timer) clearTimeout(state.timer);
        state.timer = setTimeout(function () {
            state.timer = 0;
            syncFromSelection();
        }, HIDE_DELAY_MS);
    }

    function detailFor(env, kind) {
        return {
            kind: kind,
            moment_id: env.moment_id,
            field: env.field,
            quote: env.quote,
            timestamp: env.timestamp,
        };
    }

    function emit(env, kind) {
        if (!env || !doc || typeof doc.dispatchEvent !== 'function') return;
        var EventCtor = root.CustomEvent;
        if (typeof EventCtor !== 'function') return;
        try {
            doc.dispatchEvent(new EventCtor(EVENT_NAME, { detail: detailFor(env, kind) }));
        } catch (_err) {
            // A missing listener is a no-op. A throwing listener must not wedge the toolbar.
        }
    }

    function copyFallback(text) {
        if (!doc || typeof doc.createElement !== 'function' || typeof doc.execCommand !== 'function') return;
        var area = doc.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', 'readonly');
        if (doc.body) doc.body.appendChild(area);
        if (typeof area.select === 'function') area.select();
        try {
            doc.execCommand('copy');
        } catch (_err) {
            // Clipboard unavailable: the button still does not throw.
        }
        if (area.parentNode && typeof area.parentNode.removeChild === 'function') {
            area.parentNode.removeChild(area);
        }
    }

    function copyText(text) {
        var nav = root.navigator;
        try {
            if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
                var pending = nav.clipboard.writeText(text);
                if (pending && typeof pending.catch === 'function') {
                    pending.catch(function () { copyFallback(text); });
                }
                return;
            }
        } catch (_err) {
            // Fall through to the synchronous copy path.
        }
        copyFallback(text);
    }

    function actionTarget(node) {
        var el = asElement(node);
        while (el) {
            if (el.getAttribute && el.getAttribute('data-action')) return el;
            el = el.parentNode;
        }
        return null;
    }

    function onClick(event) {
        var btn = actionTarget(event && event.target);
        if (!btn) return;
        var action = btn.getAttribute('data-action');
        var env = envelopeForSelection(currentSelection(), doc) || state.envelope;
        if (!env) return;
        if (action === 'copy') {
            copyText(env.quote);
            return;
        }
        if (action === 'insert-reference') emit(env, 'reference');
        if (action === 'transcript-error') emit(env, 'transcript-error');
    }

    function makeButton(action, key) {
        var btn = doc.createElement('button');
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-action', action);
        btn.setAttribute('data-i18n', key);
        btn.textContent = tx(key);
        return btn;
    }

    function build() {
        var holder = doc.createElement('div');
        holder.className = 'ss-selection-toolbar-root';
        holder.id = 'ss-selection-toolbar-root';
        var bar = doc.createElement('div');
        bar.className = 'ss-selection-toolbar';
        bar.id = 'ss-selection-toolbar';
        bar.hidden = true;
        bar.setAttribute('role', 'toolbar');
        bar.setAttribute('aria-label', tx('review.selectionToolbar.label'));
        bar.setAttribute('data-i18n-attr', 'aria-label:review.selectionToolbar.label');
        var add = makeButton('insert-reference', 'review.selectionToolbar.addToChat');
        var err = makeButton('transcript-error', 'review.selectionToolbar.reportTranscriptError');
        err.hidden = true;
        var copy = makeButton('copy', 'review.selectionToolbar.copy');
        bar.appendChild(add);
        bar.appendChild(err);
        bar.appendChild(copy);
        holder.appendChild(bar);
        doc.body.appendChild(holder);
        bar.addEventListener('mousedown', function (event) {
            if (event && typeof event.preventDefault === 'function') event.preventDefault();
        });
        bar.addEventListener('click', onClick);
    }

    function onViewport() {
        if (state.envelope) syncFromSelection();
    }

    function init() {
        if (!doc || !doc.body || typeof doc.createElement !== 'function' || typeof doc.addEventListener !== 'function') return;
        if (typeof doc.getElementById === 'function' && doc.getElementById('ss-selection-toolbar')) return;
        build();
        doc.addEventListener('selectionchange', schedule);
        doc.addEventListener('scroll', onViewport, true);
        if (typeof root.addEventListener === 'function') root.addEventListener('resize', onViewport);
    }

    root.screenscribeSelectionToolbar = {
        EVENT_NAME: EVENT_NAME,
        QUOTE_CAP: QUOTE_CAP,
        HIDE_DELAY_MS: HIDE_DELAY_MS,
        envelopeForSelection: envelopeForSelection,
        syncFromSelection: syncFromSelection,
        init: init,
    };

    if (doc && doc.readyState === 'loading' && typeof doc.addEventListener === 'function') {
        doc.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})(typeof window !== 'undefined' ? window : globalThis);
