(function attachLanguageControl(root) {
    const namespace = root.ScreenScribeLib || {};

    function normalizeLanguage(lang) {
        if (typeof lang !== 'string') return null;
        const normalized = lang.trim().toLowerCase().replace('_', '-');
        const primary = normalized.split('-', 1)[0];
        if (root.hasI18nLanguage?.(normalized)) return normalized;
        if (root.hasI18nLanguage?.(primary)) return primary;
        return null;
    }

    function readStoredLanguage(value) {
        if (!value) return null;
        if (typeof value === 'object') return normalizeLanguage(value.lang);
        if (typeof value !== 'string') return null;

        const direct = normalizeLanguage(value);
        if (direct) return direct;

        try {
            const parsed = JSON.parse(value);
            return normalizeLanguage(parsed?.lang);
        } catch (_error) {
            return null;
        }
    }

    function getInitialLanguage(sources) {
        for (const source of Array.from(sources || [])) {
            let value = source;
            try {
                value = typeof source === 'function' ? source() : source;
            } catch (_error) {
                value = null;
            }
            const lang = readStoredLanguage(value);
            if (lang) return lang;
        }
        return 'en';
    }

    function persistLanguage(key, lang, options) {
        const mode = options?.mode || 'string';
        const normalized = normalizeLanguage(lang);
        if (!key || !normalized) return;

        try {
            const value = mode === 'envelope'
                ? JSON.stringify({
                    sourceId: options?.sourceId || '',
                    savedAt: new Date().toISOString(),
                    lang: normalized,
                })
                : normalized;
            root.localStorage?.setItem(key, value);
        } catch (_error) {
            // localStorage can be unavailable in private/locked-down contexts.
        }
    }

    function wireToggle(toggleEl, options, onChange) {
        const rootEl = typeof toggleEl === 'string' ? document.querySelector(toggleEl) : toggleEl;
        const order = Array.isArray(options?.order) ? options.order : null;
        const buttons = Array.from(
            rootEl?.querySelectorAll?.('button[data-lang]')
                || document.querySelectorAll('.lang-toggle button[data-lang]')
                || []
        );
        buttons.forEach((btn) => {
            btn.addEventListener('click', () => {
                const lang = btn.getAttribute('data-lang') || btn.dataset?.lang;
                if (!order || order.includes(lang)) {
                    onChange?.(lang);
                }
            });
        });
    }

    namespace.getInitialLanguage = getInitialLanguage;
    namespace.persistLanguage = persistLanguage;
    namespace.wireLanguageToggle = wireToggle;
    root.ScreenScribeLib = namespace;
})(typeof window !== 'undefined' ? window : globalThis);
