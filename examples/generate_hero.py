#!/usr/bin/env python3
"""Render the example report and capture a deterministic hero screenshot.

Offline and reproducible: it opens the locally generated
``examples/example_report.html`` over ``file://`` in headless Chromium
(Playwright) at a fixed viewport and writes a PNG to
``docs/showcase/example_report.png`` — the hero image embedded in the README
and ``docs/SHOWCASE.md``.

This depends on the artifacts produced by ``generate_example.py``; run that
first (or it is run automatically below if the HTML is missing). No API key,
no network, no real video.

Run from the repo root:

    uv run python examples/generate_hero.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = REPO_ROOT / "examples" / "example_report.html"
OUT_PATH = REPO_ROOT / "docs" / "showcase" / "example_report.png"

# Fixed viewport keeps the capture deterministic across runs/machines.
VIEWPORT = {"width": 1440, "height": 900}


def _ensure_html() -> None:
    if HTML_PATH.exists():
        return
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("generate_example.py"))],
        check=True,
    )


def main() -> int:
    _ensure_html()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    console_errors: list[str] = []

    def _record(msg) -> None:
        if msg.type != "error":
            return
        # The standalone report polls the review-server API (/api/review-state),
        # which is absent over file://. That fetch failure is expected for a
        # server-less example and is not a render error — ignore it.
        if "review-state" in msg.text or "/api/" in msg.text:
            return
        console_errors.append(msg.text)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        page.on("console", _record)
        page.goto(HTML_PATH.resolve().as_uri(), wait_until="networkidle")
        # Above-the-fold hero: header + summary + first findings.
        page.screenshot(path=str(OUT_PATH), clip={"x": 0, "y": 0, **VIEWPORT})
        browser.close()

    if console_errors:
        print("Console errors detected:", file=sys.stderr)
        for err in console_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"Wrote {OUT_PATH.relative_to(REPO_ROOT)} (no console errors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
