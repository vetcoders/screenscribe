/* screenscribe landing - progressive enhancement only.
   No dependencies, no data fetching. */
(function () {
    "use strict";

    document.documentElement.classList.add("js");

    var reduceMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function qsAll(selector, root) {
        return Array.prototype.slice.call((root || document).querySelectorAll(selector));
    }

    function setCopied(btn) {
        var original = btn.getAttribute("data-original-label") || btn.textContent;
        btn.setAttribute("data-original-label", original);
        btn.textContent = "COPIED";
        btn.classList.add("copied");
        window.clearTimeout(btn._copyTimer);
        btn._copyTimer = window.setTimeout(function () {
            btn.textContent = original;
            btn.classList.remove("copied");
        }, 1600);
    }

    function fallbackCopy(text, done) {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "absolute";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        try {
            if (document.execCommand("copy")) { done(); }
        } catch (e) {
            /* no-op: the visible command block remains available */
        }
        document.body.removeChild(ta);
    }

    qsAll(".copy-btn").forEach(function (btn) {
        btn.setAttribute("data-original-label", btn.textContent);
        btn.addEventListener("click", function () {
            var text = btn.getAttribute("data-copy") || "";
            var done = function () { setCopied(btn); };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(done, function () {
                    fallbackCopy(text, done);
                });
            } else {
                fallbackCopy(text, done);
            }
        });
    });

    qsAll('a[href^="#"]').forEach(function (link) {
        link.addEventListener("click", function (event) {
            var id = link.getAttribute("href");
            if (!id || id === "#" || id.length < 2) { return; }
            var target = document.querySelector(id);
            if (!target) { return; }
            event.preventDefault();
            target.scrollIntoView({
                behavior: reduceMotion ? "auto" : "smooth",
                block: "start"
            });
            if (target.hasAttribute("tabindex")) {
                target.focus({ preventScroll: true });
            }
        });
    });

    function revealElements() {
        var reveals = qsAll("[data-reveal]");
        if (reduceMotion || !("IntersectionObserver" in window)) {
            reveals.forEach(function (el) { el.classList.add("ss-in"); });
            return;
        }

        var observer = new IntersectionObserver(function (entries, obs) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    entry.target.classList.add("ss-in");
                    obs.unobserve(entry.target);
                }
            });
        }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });

        reveals.forEach(function (el) { observer.observe(el); });

        function revealInView() {
            reveals.forEach(function (el) {
                if (el.classList.contains("ss-in")) { return; }
                var rect = el.getBoundingClientRect();
                if (rect.top < (window.innerHeight || 0) && rect.bottom > 0) {
                    el.classList.add("ss-in");
                    observer.unobserve(el);
                }
            });
        }

        window.addEventListener("load", revealInView);
        window.addEventListener("scroll", revealInView, { passive: true });
    }

    function runTypewriter() {
        var target = document.querySelector("[data-typewriter]");
        if (!target) { return; }
        var outputs = qsAll("[data-outline]");
        var text = "screenscribe review demo.mov";

        if (reduceMotion) {
            target.textContent = text;
            outputs.forEach(function (line) { line.style.opacity = "1"; });
            return;
        }

        var i = 0;
        window.setTimeout(function type() {
            if (i <= text.length) {
                target.textContent = text.slice(0, i);
                i += 1;
                window.setTimeout(type, 52);
                return;
            }

            outputs.forEach(function (line, index) {
                window.setTimeout(function () {
                    line.style.transition = "opacity 500ms ease";
                    line.style.opacity = "1";
                }, 380 + index * 420);
            });
        }, 700);
    }

    function wireHeroSpotlight() {
        var hero = document.querySelector("[data-hero]");
        var spot = document.querySelector("[data-spotlight]");
        if (!hero || !spot || reduceMotion) { return; }

        hero.addEventListener("mousemove", function (event) {
            var rect = hero.getBoundingClientRect();
            spot.style.left = (event.clientX - rect.left) + "px";
            spot.style.top = (event.clientY - rect.top) + "px";
            spot.style.opacity = "1";
        });

        hero.addEventListener("mouseleave", function () {
            spot.style.opacity = "0";
        });
    }

    function wirePipeline() {
        var pipeline = document.querySelector("[data-pipeline]");
        var nodes = qsAll("[data-node]", pipeline);
        if (!pipeline || !nodes.length) { return; }

        var index = 0;
        var paused = false;
        var timer = null;
        var started = false;

        function startPipeline() {
            if (started) { return; }
            started = true;
            tick();
        }

        function setActive(activeIndex) {
            nodes.forEach(function (node, i) {
                node.classList.toggle("is-active", i === activeIndex);
            });
        }

        function tick() {
            if (reduceMotion) {
                setActive(0);
                return;
            }
            if (!paused) {
                setActive(index);
                index = (index + 1) % nodes.length;
            }
            timer = window.setTimeout(tick, paused ? 260 : 1050);
        }

        nodes.forEach(function (node, i) {
            node.addEventListener("mouseenter", function () {
                paused = true;
                window.clearTimeout(timer);
                setActive(i);
            });
            node.addEventListener("mouseleave", function () {
                paused = false;
                window.clearTimeout(timer);
                if (started) {
                    timer = window.setTimeout(tick, 260);
                }
            });
        });

        if ("IntersectionObserver" in window && !reduceMotion) {
            var observer = new IntersectionObserver(function (entries, obs) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        startPipeline();
                        obs.unobserve(entry.target);
                    }
                });
            }, { threshold: 0.3 });
            observer.observe(pipeline);
        } else {
            startPipeline();
        }
    }

    function wireModes() {
        var buttons = qsAll("[data-mode-btn]");
        var panels = qsAll("[data-mode-panel]");
        if (!buttons.length || !panels.length) { return; }

        function setMode(mode) {
            buttons.forEach(function (btn) {
                var active = btn.getAttribute("data-mode-btn") === mode;
                btn.classList.toggle("is-active", active);
                btn.setAttribute("aria-selected", active ? "true" : "false");
            });

            panels.forEach(function (panel) {
                var active = panel.getAttribute("data-mode-panel") === mode;
                panel.classList.toggle("is-active", active);
                if (active) {
                    panel.removeAttribute("hidden");
                } else {
                    panel.setAttribute("hidden", "");
                }
            });
        }

        buttons.forEach(function (btn) {
            btn.addEventListener("click", function () {
                setMode(btn.getAttribute("data-mode-btn"));
            });
        });

        setMode("review");
    }

    function wireAccordion() {
        qsAll("[data-feat]").forEach(function (item, index) {
            var head = item.querySelector("[data-feat-head]");
            var body = item.querySelector("[data-feat-body]");
            if (!head || !body) { return; }

            var panelId = body.id || "feature-panel-" + index;
            body.id = panelId;
            body.setAttribute("role", "region");
            head.setAttribute("aria-controls", panelId);

            function setOpen(open) {
                item.classList.toggle("is-open", open);
                head.setAttribute("aria-expanded", open ? "true" : "false");
                if (open) {
                    body.hidden = false;
                    body.setAttribute("aria-hidden", "false");
                    body.style.maxHeight = body.scrollHeight + "px";
                } else {
                    body.style.maxHeight = "0px";
                    body.setAttribute("aria-hidden", "true");
                    body.hidden = true;
                }
            }

            setOpen(index === 0);
            head.addEventListener("click", function () {
                setOpen(!item.classList.contains("is-open"));
            });
        });
    }

    revealElements();
    runTypewriter();
    wireHeroSpotlight();
    wirePipeline();
    wireModes();
    wireAccordion();
})();
