/* screenscribe landing — progressive enhancement only.
   Three concerns: copy button, smooth in-page scroll, fade-up on scroll.
   No external requests, no dependencies. */
(function () {
    "use strict";

    /* Progressive enhancement flag: mark the document as JS-capable so CSS can
       scope the initially-hidden fade-up state under `.js`. Without JS this
       class is never added and all content stays visible. Must run first. */
    document.documentElement.classList.add("js");

    var reduceMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    /* -- Copy button ------------------------------------------------------ */
    document.querySelectorAll(".copy-btn").forEach(function (btn) {
        /* Capture the original label ONCE at wire time. Reading it inside the
           click handler meant a rapid second click (while showing 'Copied')
           captured 'Copied' as the original and froze the button there. */
        var original = btn.textContent;
        var resetTimer = null;
        btn.addEventListener("click", function () {
            var text = btn.getAttribute("data-copy") || "";
            var done = function () {
                btn.textContent = "Copied";
                btn.classList.add("copied");
                /* Debounce: a fresh click restarts the window rather than
                   stacking timers that could fight over the label. */
                if (resetTimer) { clearTimeout(resetTimer); }
                resetTimer = setTimeout(function () {
                    btn.textContent = original;
                    btn.classList.remove("copied");
                    resetTimer = null;
                }, 1600);
            };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done, fallbackCopy);
            } else {
                fallbackCopy();
            }
            function fallbackCopy() {
                var ta = document.createElement("textarea");
                ta.value = text;
                ta.setAttribute("readonly", "");
                ta.style.position = "absolute";
                ta.style.left = "-9999px";
                document.body.appendChild(ta);
                ta.select();
                try { if (document.execCommand("copy")) { done(); } } catch (e) { /* no-op */ }
                document.body.removeChild(ta);
            }
        });
    });

    /* -- Smooth in-page scroll (progressive enhancement over CSS) --------- */
    document.querySelectorAll('a[href^="#"]').forEach(function (link) {
        link.addEventListener("click", function (e) {
            var id = link.getAttribute("href");
            if (id === "#" || id.length < 2) { return; }
            var target = document.querySelector(id);
            if (!target) { return; }
            e.preventDefault();
            target.scrollIntoView({
                behavior: reduceMotion ? "auto" : "smooth",
                block: "start"
            });
        });
    });

    /* -- Fade-up on scroll ------------------------------------------------ */
    var faders = document.querySelectorAll(".fade-up");
    if (reduceMotion || !("IntersectionObserver" in window)) {
        faders.forEach(function (el) { el.classList.add("is-visible"); });
        return;
    }
    var observer = new IntersectionObserver(function (entries, obs) {
        entries.forEach(function (entry) {
            if (entry.isIntersecting) {
                entry.target.classList.add("is-visible");
                obs.unobserve(entry.target);
            }
        });
    }, { threshold: 0.08, rootMargin: "0px 0px -24px 0px" });
    faders.forEach(function (el) { observer.observe(el); });

    /* Failsafe: reveal anything already within the viewport (covers elements
       trapped near the page bottom that never clear the observer margin). */
    function revealInView() {
        faders.forEach(function (el) {
            if (el.classList.contains("is-visible")) { return; }
            var r = el.getBoundingClientRect();
            if (r.top < (window.innerHeight || 0) && r.bottom > 0) {
                el.classList.add("is-visible");
                observer.unobserve(el);
            }
        });
    }
    window.addEventListener("load", revealInView);
    window.addEventListener("scroll", revealInView, { passive: true });
})();
