(function attachTabKeyboard(root) {
    const namespace = root.ScreenScribeLib || {};

    function initTabKeyboard(tabButtons, activate) {
        const buttons = Array.from(tabButtons || []);
        if (buttons.length === 0 || typeof activate !== 'function') return;

        buttons.forEach((btn, index) => {
            btn.addEventListener('keydown', (event) => {
                let nextIndex = null;
                if (event.key === 'ArrowRight') nextIndex = (index + 1) % buttons.length;
                else if (event.key === 'ArrowLeft') nextIndex = (index - 1 + buttons.length) % buttons.length;
                else if (event.key === 'Home') nextIndex = 0;
                else if (event.key === 'End') nextIndex = buttons.length - 1;
                if (nextIndex === null) return;
                event.preventDefault();
                const nextBtn = buttons[nextIndex];
                activate(nextBtn, nextBtn.dataset?.tab);
                nextBtn.focus();
            });
        });
    }

    namespace.initTabKeyboard = initTabKeyboard;
    root.ScreenScribeLib = namespace;
})(typeof window !== 'undefined' ? window : globalThis);
