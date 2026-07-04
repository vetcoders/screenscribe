// Minimal functional DOM for driving review_app.js merge UI in a node sandbox.
//
// The other review_app.js node tests stub the DOM with no-op elements
// (querySelectorAll -> []), which is enough to exercise pure state functions but
// CANNOT prove DOM-mutating behaviour: applyMergeToDom hiding absorbed cards,
// renderMergedCard's control tree, or event-delegated verdict changes. This file
// implements just enough of the DOM (a real node tree + a small CSS selector
// engine) for those assertions. It is test-only scaffolding, not shipped code.

'use strict';

function camel(s) {
    return String(s).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

function parseToken(token) {
    // Strip pseudo-classes/elements (e.g. :not(...), :disabled) — not needed for
    // the selectors these tests exercise; treat the base compound only.
    const base = token.replace(/:[a-zA-Z-]+(\([^)]*\))?/g, '');
    const spec = { tag: null, id: null, classes: [], attrs: [] };
    if (base === '*' || base === '') return spec;
    const tagMatch = base.match(/^[a-zA-Z][\w-]*/);
    if (tagMatch) spec.tag = tagMatch[0].toUpperCase();
    const idMatch = base.match(/#([\w-]+)/);
    if (idMatch) spec.id = idMatch[1];
    let m;
    const classRe = /\.([\w-]+)/g;
    while ((m = classRe.exec(base))) spec.classes.push(m[1]);
    const attrRe = /\[([\w-]+)(?:\s*=\s*["']?([^"'\]]*)["']?)?\]/g;
    while ((m = attrRe.exec(base))) spec.attrs.push({ name: m[1], value: m[2] });
    return spec;
}

function matchToken(el, token) {
    const spec = parseToken(token);
    if (spec.tag && el.tagName !== spec.tag) return false;
    if (spec.id && el.id !== spec.id) return false;
    for (const c of spec.classes) {
        if (!el._classes.has(c)) return false;
    }
    for (const a of spec.attrs) {
        const actual = el.getAttribute(a.name);
        if (a.value === undefined) {
            if (actual === null || actual === undefined) return false;
        } else if (String(actual) !== a.value) {
            return false;
        }
    }
    return true;
}

function matchesSelector(el, selector) {
    return selector.split(',').some((group) => {
        const tokens = group.trim().split(/\s+/).filter(Boolean);
        if (tokens.length === 0) return false;
        // Rightmost compound must match el; preceding ones must match ancestors.
        if (!matchToken(el, tokens[tokens.length - 1])) return false;
        let ancestor = el.parentNode;
        for (let i = tokens.length - 2; i >= 0; i--) {
            let found = null;
            let node = ancestor;
            while (node) {
                if (node.matchToken && matchToken(node, tokens[i])) { found = node; break; }
                node = node.parentNode;
            }
            if (!found) return false;
            ancestor = found.parentNode;
        }
        return true;
    });
}

class CElement {
    constructor(tag) {
        this.tagName = String(tag || 'div').toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        // Real DOM DOMStringMap coerces every value to a string; mirror that so
        // tests see dataset.findingId === '17', not the raw number 17.
        this.dataset = new Proxy({}, {
            set(target, key, value) {
                target[key] = value == null ? value : String(value);
                return true;
            },
        });
        this.style = {};
        this._classes = new Set();
        this._listeners = {};
        this.value = '';
        this.checked = false;
        this.hidden = false;
        this.disabled = false;
        this.textContent = '';
        this.type = '';
        this.name = '';
        this.htmlFor = '';
        this.id = '';
        this.placeholder = '';
    }

    get className() { return [...this._classes].join(' '); }
    set className(v) {
        this._classes = new Set(String(v || '').split(/\s+/).filter(Boolean));
    }

    get classList() {
        const self = this;
        return {
            add: (...c) => c.forEach((x) => self._classes.add(x)),
            remove: (...c) => c.forEach((x) => self._classes.delete(x)),
            toggle: (x) => {
                if (self._classes.has(x)) { self._classes.delete(x); return false; }
                self._classes.add(x); return true;
            },
            contains: (x) => self._classes.has(x),
        };
    }

    get firstChild() { return this.children[0] || null; }
    get offsetWidth() { return 0; }
    get offsetParent() { return this.parentNode; }

    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }

    insertBefore(node, ref) {
        node.parentNode = this;
        const i = ref ? this.children.indexOf(ref) : -1;
        if (i === -1) this.children.push(node);
        else this.children.splice(i, 0, node);
        return node;
    }

    removeChild(child) {
        const i = this.children.indexOf(child);
        if (i >= 0) this.children.splice(i, 1);
        child.parentNode = null;
        return child;
    }

    remove() { if (this.parentNode) this.parentNode.removeChild(this); }

    setAttribute(k, v) {
        this.attributes[k] = String(v);
        if (k === 'type') this.type = String(v);
        else if (k === 'name') this.name = String(v);
        else if (k === 'value') this.value = String(v);
        else if (k === 'id') this.id = String(v);
        else if (k === 'placeholder') this.placeholder = String(v);
        else if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = String(v);
    }

    hasAttribute(k) {
        const v = this.getAttribute(k);
        return v !== null && v !== undefined;
    }

    getAttribute(k) {
        if (k === 'type') return this.type || null;
        if (k === 'name') return this.name || null;
        if (k === 'id') return this.id || null;
        if (k === 'class') return this.className;
        if (k.startsWith('data-')) {
            const key = camel(k.slice(5));
            return key in this.dataset ? this.dataset[key] : null;
        }
        return k in this.attributes ? this.attributes[k] : null;
    }

    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
    removeEventListener() {}
    dispatch(type, event) {
        (this._listeners[type] || []).forEach((fn) => fn(event));
    }

    matchToken(token) { return matchToken(this, token); }
    matches(selector) { return matchesSelector(this, selector); }

    closest(selector) {
        let el = this;
        while (el) {
            if (el.matches && el.matches(selector)) return el;
            el = el.parentNode;
        }
        return null;
    }

    _descendants() {
        const out = [];
        const walk = (n) => {
            for (const c of n.children) { out.push(c); walk(c); }
        };
        walk(this);
        return out;
    }

    contains(node) {
        let el = node;
        while (el) { if (el === this) return true; el = el.parentNode; }
        return false;
    }

    querySelectorAll(selector) {
        return this._descendants().filter((el) => matchesSelector(el, selector));
    }

    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }

    get outerText() { return this.textContent; }
}

function createDocument(findings) {
    const root = new CElement('html');
    const body = new CElement('body');
    body.dataset.reportLanguage = 'en';
    body.dataset.videoName = 'demo.mp4';
    root.appendChild(body);

    const findingsScript = new CElement('script');
    findingsScript.id = 'original-findings';
    findingsScript.textContent = JSON.stringify(findings);
    body.appendChild(findingsScript);

    const tab = new CElement('div');
    tab.id = 'tab-findings';
    body.appendChild(tab);

    const document = {
        body,
        documentElement: { lang: 'en' },
        addEventListener() {},
        removeEventListener() {},
        createElement(tag) { return new CElement(tag); },
        getElementById(id) {
            return root._descendants().find((el) => el.id === id) || null;
        },
        querySelector(sel) { return root.querySelector(sel); },
        querySelectorAll(sel) { return root.querySelectorAll(sel); },
        get activeElement() { return null; },
    };
    return { document, root, body, tab };
}

// Build a server-rendered finding article (mirrors html_pro/renderer.py output
// for the parts review_app.js reads: header, verdict radios, severity select,
// notes textarea, screenshot annotation container).
function buildFindingArticle(document, finding) {
    const article = document.createElement('article');
    article.className = 'finding';
    article.dataset.findingId = String(finding.id);
    article.dataset.verdict = '';
    article.dataset.severity = (finding.unified_analysis || {}).severity || 'medium';

    const header = document.createElement('div');
    header.className = 'finding-header';
    article.appendChild(header);

    const review = document.createElement('div');
    review.className = 'human-review';
    const radioGroup = document.createElement('div');
    radioGroup.className = 'radio-group';
    for (const value of ['accepted', 'rejected']) {
        const radio = document.createElement('input');
        radio.setAttribute('type', 'radio');
        radio.setAttribute('name', `verdict-${finding.id}`);
        radio.setAttribute('value', value);
        radioGroup.appendChild(radio);
    }
    review.appendChild(radioGroup);
    const select = document.createElement('select');
    select.className = 'severity-select';
    review.appendChild(select);
    const notes = document.createElement('div');
    notes.className = 'notes';
    const textarea = document.createElement('textarea');
    notes.appendChild(textarea);
    review.appendChild(notes);
    article.appendChild(review);

    return article;
}

module.exports = { CElement, createDocument, buildFindingArticle, camel };
