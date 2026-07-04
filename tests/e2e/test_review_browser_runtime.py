"""Layer 2 — browser E2E (the whale-killer).

Drives the freshly-served report in a real headless Chromium and asserts the
exact runtime behaviours the four session fixes guarantee:

- token arrives in #fragment, is stripped from the address bar, cached per-tab in
  sessionStorage, and SURVIVES a reload (no /api/* 403 after refresh) ... e79e515
- a manual frame's base64 image survives a reload (review-state -> enrich) ...... e79e515 + arch
- a manual frame's image survives a cross-window lightweight storage sync
  (merge-preserve, not wholesale replace) ............................. ae3db9e
- the ZIP export carries the manual-frame image (non-empty manual_frames/) ..... de41ef1 + ae3db9e

Determinism: the report is generated ONCE (session fixture) and re-served; the
fixture clip yields an empty-state report (0 findings), so the reviewer drives
all manual-frame state from the browser. Confirm dialogs (export with 0 reviewed
findings) are auto-accepted.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]

_I18N_JS = Path(__file__).resolve().parents[2] / "screenscribe/html_pro_assets/scripts/i18n.js"


def _i18n_review() -> dict[str, dict[str, str]]:
    """Load the ``review`` namespace per language from the JS i18n bundle.

    The TODO markdown labels are i18n-sourced (C7.1 residue), so the contract
    assertions below resolve the expected text from the same single source the
    runtime uses — instead of hardcoding English literals that would silently
    pass when the report renders in another language.
    """
    text = _I18N_JS.read_text(encoding="utf-8")
    marker = "window.I18N_BUNDLE = "
    start = text.index(marker) + len(marker)
    end = text.index("\n};", start)
    obj = text[start : end + 2].rstrip().rstrip(";")
    bundle = json.loads(obj)
    return {lang: bundle[lang]["review"] for lang in bundle}


# A valid tiny 1x1 JPEG, base64, NO data: prefix. ~239 chars. We assert the
# restored data URL starts with "data:image" and is > 100 chars (the tiny JPEG
# plus the "data:image/jpeg;base64," prefix is ~260 chars, comfortably > 100).
# We deliberately do NOT require length > 1000 (the brief's alternative) nor
# naturalWidth > 0 — a 1x1 synthetic image is legitimately tiny.
TINY_JPEG_B64 = (  # pragma: allowlist secret
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"  # pragma: allowlist secret
    "AAAAAAAAAAAAAAAAAAAAAv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q=="
)
TINY_JPEG_DATA_URL = "data:image/jpeg;base64," + TINY_JPEG_B64

DATA_URL_MIN_LEN = 100  # prefix (~23) + tiny JPEG (~239) >> 100; 1x1 is fine


def _install_console_capture(page) -> tuple[list, list]:
    console_errors: list = []
    page_errors: list = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    return console_errors, page_errors


def _install_api_403_watch(page) -> list:
    api_403: list = []

    def _on_response(resp):
        if "/api/" in resp.url and resp.status == 403:
            api_403.append(resp.url)

    page.on("response", _on_response)
    return api_403


def _mark_frame_via_client(page) -> None:
    """Add a manual frame through the real client function and save it to disk."""
    page.evaluate(
        """async (b64) => {
            const dataUrl = 'data:image/jpeg;base64,' + b64;
            reportState.reviewer = 'E2E Bot';
            const current = {
                timestamp: 0.5,
                frameBase64: b64,
                frameDataUrl: dataUrl,
            };
            await markManualFrame(current, 'spoken note', 'typed note');
            await saveReviewToDisk();
        }""",
        TINY_JPEG_B64,
    )


def test_review_reload_cross_window_manual_frame_survives(review_server, browser_context) -> None:
    page = browser_context.new_page()
    console_errors, page_errors = _install_console_capture(page)
    api_403 = _install_api_403_watch(page)
    # Auto-accept the "no reviewed findings" confirm() on export.
    page.on("dialog", lambda dialog: dialog.accept())

    # --- 1. First load: token in #fragment ------------------------------------
    page.goto(review_server.url, wait_until="networkidle")

    # --- 2. token stripped from address bar, cached in sessionStorage ----------
    assert page.evaluate("location.hash") == "", "token fragment not stripped from address bar"
    token_key = page.evaluate("'screenscribe:token:' + location.origin + location.pathname")
    cached = page.evaluate("(k) => sessionStorage.getItem(k)", token_key)
    assert cached, "session token not cached in sessionStorage after first load"
    assert cached == review_server.token, "cached token does not match the served token"

    # --- 3. Reload: /api/review-state must be 200, no /api/* 403 (KILLER) ------
    page.reload(wait_until="networkidle")
    status = page.evaluate("fetch('/api/review-state').then(r => r.status)")
    assert status == 200, f"/api/review-state returned {status} after reload (token lost?)"
    assert not api_403, f"/api/* 403 after reload: {api_403}"

    # --- 4. Add a manual frame + save to disk ---------------------------------
    _mark_frame_via_client(page)
    first = page.evaluate("reportState.manualFrames[0] && reportState.manualFrames[0].frameDataUrl")
    assert first and first.startswith("data:image"), f"manual frame frameDataUrl missing: {first!r}"
    assert len(first) > DATA_URL_MIN_LEN, f"frameDataUrl unexpectedly short: {len(first)}"

    # --- 4b. P2-8 notes contract: single `notes` field, no phantom actionItems --
    # The typed note round-trips via the manual frame's `notes` field, and the
    # persisted review state must never carry the removed `actionItems` companion
    # (it was init'd + re-joined into the notes textarea but never serialised).
    state_json = page.evaluate("JSON.stringify(buildPersistableState(false))")
    assert '"actionItems"' not in state_json, (
        "persisted review state still carries the phantom actionItems field (P2-8)"
    )
    manual_notes = page.evaluate("reportState.manualFrames[0].notes")
    assert manual_notes == "typed note", (
        f"manual-frame typed note lost from unified notes field: {manual_notes!r}"
    )
    default_keys = page.evaluate("Object.keys(createDefaultFindingState()).sort().join(',')")
    assert default_keys == "notes,severity,verdict", (
        f"default finding state changed shape (P2-8): {default_keys!r}"
    )

    # --- 5. Manual frame card <img> has a real data: src ----------------------
    # renderManualFrames() rebuilds the cards; ensure one is present.
    page.evaluate("renderManualFrames()")
    img_src = page.evaluate(
        """() => {
            const img = document.querySelector(
                '#manualFindingsList .manual-frame-item img.thumbnail'
            );
            return img ? img.getAttribute('src') : null;
        }"""
    )
    assert img_src, "no manual-frame <img> rendered in the DOM"
    assert img_src.startswith("data:image/"), (
        f"manual-frame img src is not a data URL: {img_src[:40]!r}"
    )

    # --- 6. Reload again: image survives (token recovered -> review-state -> enrich)
    page.reload(wait_until="networkidle")
    status = page.evaluate("fetch('/api/review-state').then(r => r.status)")
    assert status == 200, "review-state 403 on 2nd reload"
    # enrichManualFrameImagesFromServerState runs async after init; wait for it.
    page.wait_for_function(
        "() => reportState.manualFrames.length > 0 "
        "&& reportState.manualFrames[0].frameDataUrl "
        "&& reportState.manualFrames[0].frameDataUrl.startsWith('data:image')",
        timeout=15000,
    )
    after_reload = page.evaluate("reportState.manualFrames[0].frameDataUrl")
    assert after_reload.startswith("data:image"), "manual-frame image lost after reload"

    # --- 7. Cross-window storage sync: image must NOT be wiped (merge-preserve) -
    # Genuine two-page scenario in the SAME context (shared localStorage; a write
    # in page B fires a `storage` event in page A). The second page initialises,
    # writes the lightweight (image-stripped) sync key, page A receives the event
    # and runs hydrateReportState -> mergeManualFrameImages.
    page_b = browser_context.new_page()
    page_b.on("dialog", lambda dialog: dialog.accept())
    page_b.goto(review_server.url, wait_until="networkidle")
    # Force page B to write the lightweight shared-state envelope, which is what a
    # second tab does on any state change; this is the cross-window trigger.
    page_b.evaluate(
        """() => {
            const key = 'screenscribe_state_' + reportState.reportId;
            const envelope = {
                sourceId: 'PAGE_B_' + performance.now(),
                savedAt: new Date().toISOString(),
                state: buildPersistableState(false),
            };
            localStorage.setItem(key, JSON.stringify(envelope));
        }"""
    )
    # Give page A's storage handler a beat to run hydrate/merge.
    page.wait_for_timeout(750)
    preserved = page.evaluate(
        "reportState.manualFrames[0] && reportState.manualFrames[0].frameDataUrl"
    )
    assert preserved and preserved.startswith("data:image"), (
        "manual-frame image wiped by cross-window lightweight storage sync "
        "(merge-preserve regression, ae3db9e)"
    )
    page_b.close()

    # --- 8. Export ZIP: manual_frames/ must contain an image (empty-ZIP canary) -
    with page.expect_download(timeout=30000) as dl_info:
        page.evaluate("exportReviewedZIP()")
    download = dl_info.value
    zip_bytes = _read_download_bytes(download)
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = zf.namelist()
    manual_imgs = [
        n for n in names if n.startswith("manual_frames/") and n.lower().endswith((".jpg", ".png"))
    ]
    assert manual_imgs, f"ZIP manual_frames/ has no image (empty-ZIP regression). entries: {names}"

    # --- 8b. SF-2 (ZIP path): the bundled reviewed JSON must be base64-free -----
    # The ZIP keeps images as files under manual_frames/; the JSON inside the ZIP
    # must stay lightweight + referential. `...frame` used to re-leak frameDataUrl
    # back in — the standalone exportReviewedJSON strips it (de41ef1), the ZIP path
    # did not — bloating the JSON ~100x and storing each image redundantly.
    json_names = [n for n in names if n.startswith("report_reviewed_") and n.endswith(".json")]
    assert json_names, f"ZIP missing bundled reviewed JSON. entries: {names}"
    bundled_json = zf.read(json_names[0]).decode("utf-8")
    assert "frameDataUrl" not in bundled_json, (
        "ZIP-bundled reviewed JSON re-leaks base64 frameDataUrl (SF-2 ZIP-path regression)"
    )
    assert "data:image" not in bundled_json, (
        "ZIP-bundled reviewed JSON embeds an inline data: image "
        "(it must reference manual_frames/ files, not inline base64)"
    )
    bundled = json.loads(bundled_json)
    bundled_frames = bundled.get("manual_frames", [])
    assert bundled_frames, "ZIP reviewed JSON dropped manual_frames entirely"
    assert any(
        (f.get("screenshot_file") or "").startswith("manual_frames/") for f in bundled_frames
    ), "ZIP reviewed JSON manual_frames lost their screenshot_file references"

    # --- 8c. TODO two-section contract: manual captures are never dropped -------
    # A manually captured frame is reviewer evidence; the TODO must surface it
    # even with no AI analysis. buildTodoMarkdown used to iterate AI findings only.
    todo_names = [n for n in names if n.startswith("TODO_") and n.endswith(".md")]
    assert todo_names, f"ZIP missing TODO markdown. entries: {names}"
    todo = zf.read(todo_names[0]).decode("utf-8")
    # The TODO labels are localized (C7.1 residue): resolve the expected text from
    # the i18n bundle for whichever language the report rendered in, so the
    # contract holds in en AND pl rather than only when the labels were hardcoded.
    review_i18n = _i18n_review()
    lang = "pl" if f"## {review_i18n['pl']['todoManualSection']}" in todo else "en"
    labels = review_i18n[lang]
    assert f"## {labels['todoAiFindingsSection']}" in todo, "TODO missing the AI findings section"
    assert f"## {labels['todoManualSection']}" in todo, (
        "TODO missing the Manual captures section (reviewer evidence dropped)"
    )
    # The one manual frame the reviewer captured (no AI result) must be listed.
    assert f"{labels['todoManualItemLabel']} #1" in todo, (
        f"TODO Manual captures dropped the captured frame:\n{todo}"
    )
    assert labels["todoManualNotAnalyzed"] in todo, (
        "TODO did not mark the unanalyzed manual capture as such"
    )
    # Polish: the capture references the EXACT file (matching the ZIP's
    # screenshot_file), not a half-link to the folder; and carries no emoji.
    assert "manual_frames/ (@" not in todo, (
        "TODO still uses the folder half-link, not the exact file"
    )
    assert f"{labels['todoFileLabel']}: manual_frames/" in todo and (
        ".jpg" in todo or ".png" in todo
    ), f"TODO manual capture lost its exact file reference:\n{todo}"
    for emoji in ("🖼️", "📝", "🗣️", "🏷️", "🔧", "🔴", "🟠", "🟡", "🟢", "⚪"):
        assert emoji not in todo, f"TODO still contains emoji {emoji!r} (should be plain text)"

    # --- 8d. Agent handoff bundle: transcript.txt + agent_manifest.json --------
    # The ZIP doubles as a coding-agent handoff. It must carry the full
    # timestamped transcript and a structured manifest (verify criteria per
    # finding), with manifest screenshot paths kept relative to the bundle.
    assert "transcript.txt" in names, f"ZIP missing transcript.txt. entries: {names}"
    transcript = zf.read("transcript.txt").decode("utf-8")
    assert re.search(r"^\[\d{2}:\d{2}\] ", transcript, re.MULTILINE), (
        f"transcript.txt has no timestamped lines:\n{transcript[:200]}"
    )
    assert "agent_manifest.json" in names, f"ZIP missing agent_manifest.json. entries: {names}"
    manifest = json.loads(zf.read("agent_manifest.json").decode("utf-8"))
    assert manifest["meta"]["version"] == "1.0"
    assert manifest["meta"]["total_findings"] == len(manifest["findings"])
    for mf in manifest["findings"]:
        assert mf["screenshot"].startswith("screenshots/"), (
            f"manifest screenshot path must be relative to the bundle: {mf['screenshot']}"
        )
        assert mf["verify"].strip(), f"manifest finding {mf['id']} has empty verify criterion"
        assert mf["status"] == "pending"
        # Every referenced screenshot must actually ship in the bundle.
        assert mf["screenshot"] in names, (
            f"manifest references missing screenshot {mf['screenshot']}. entries: {names}"
        )

    # --- 9. Console + network sanity ------------------------------------------
    assert not page_errors, f"uncaught page errors: {page_errors}"
    assert not api_403, f"/api/* 403 seen during the run: {api_403}"
    # Console errors are surfaced but only the showNotification / app-level ones
    # would matter; a clean run has none.
    assert not console_errors, f"console errors during the run: {console_errors}"

    page.close()


def test_manual_frame_survives_fresh_browser_from_disk(review_server, chromium) -> None:
    """SS-ARCH-1 cold-load: a manual frame marked + saved in one browser is
    restored — image and all — in a BRAND-NEW browser context with empty
    storage, served off the on-disk manual_frames/ store (no inline data: image
    in report.json, no shared localStorage/sessionStorage)."""
    # --- 1. First context: mark a frame + save it to disk ---------------------
    ctx_a = chromium.new_context(accept_downloads=True)
    page_a = ctx_a.new_page()
    page_a.on("dialog", lambda dialog: dialog.accept())
    page_a.goto(review_server.url, wait_until="networkidle")
    _mark_frame_via_client(page_a)
    marker_id = page_a.evaluate("reportState.manualFrames[0].marker_id")
    assert marker_id, "marker_id not assigned after mark+save"
    ctx_a.close()  # tear down ALL of context A's storage — true cold start

    # --- 2. report.json on disk: a path reference, NOT an inline data: image ---
    report_json = next(review_server.output_dir.glob("*_report.json"))
    raw = report_json.read_text(encoding="utf-8")
    assert "data:image" not in raw, "report.json embeds a manual-frame data: image (durability bug)"
    assert "manual_frames/" in raw, "report.json lost the durable frame_path reference"
    # The pixels must live on disk under manual_frames/.
    stored = list((review_server.output_dir / "manual_frames").glob("*"))
    assert stored, "no manual-frame image written under manual_frames/"

    # --- 3. Fresh context (separate storage): image restored from disk --------
    # Context B is a brand-new browser profile: it shares NO localStorage /
    # sessionStorage with context A (which was torn down). Any state here is
    # whatever this profile builds on load — never a leftover frame from A.
    ctx_b = chromium.new_context(accept_downloads=True)
    page_b = ctx_b.new_page()
    page_b.on("dialog", lambda dialog: dialog.accept())
    page_b.goto(review_server.url, wait_until="networkidle")
    # The frame image is never carried in this profile's localStorage — the
    # lightweight shared-state envelope is image-stripped by design, so the only
    # way the picture can come back is the disk store via /api/review-state.
    ls_dump = page_b.evaluate(
        "Object.keys(localStorage).map(k => localStorage.getItem(k)).join('')"
    )
    assert "data:image" not in ls_dump, "manual-frame image leaked into localStorage"

    page_b.wait_for_function(
        "() => reportState.manualFrames.length > 0 "
        "&& reportState.manualFrames[0].frameDataUrl "
        "&& reportState.manualFrames[0].frameDataUrl.startsWith('data:image')",
        timeout=15000,
    )
    restored = page_b.evaluate("reportState.manualFrames[0].frameDataUrl")
    assert restored.startswith("data:image"), (
        "manual frame image not restored from disk in fresh browser"
    )
    assert len(restored) > DATA_URL_MIN_LEN, f"restored data URL too short: {len(restored)}"
    ctx_b.close()


def test_lightbox_backdrop_and_close_control_vs_content_click(
    review_server, browser_context
) -> None:
    """C7.2 runtime parity: after migrating the inline ``event.stopPropagation()``
    guards to event delegation, a click on the lightbox content/toolbar keeps it
    open, while a backdrop click OR the close/done control closes it."""
    page = browser_context.new_page()
    _, page_errors = _install_console_capture(page)
    page.goto(review_server.url, wait_until="networkidle")

    def _show() -> None:
        page.evaluate(
            """() => {
                const lb = document.getElementById('lightbox');
                lb.classList.add('active');
                lb.setAttribute('aria-hidden', 'false');
            }"""
        )

    def _hidden() -> str:
        return page.evaluate("document.getElementById('lightbox').getAttribute('aria-hidden')")

    # 1. Click inside the content must NOT close (former stopPropagation guard).
    _show()
    page.evaluate("document.querySelector('#lightbox .lightbox-content').click()")
    assert _hidden() == "false", "content click closed the lightbox (stopPropagation parity lost)"

    # 2. Click on the toolbar must NOT close either.
    page.evaluate("document.getElementById('lightbox-toolbar').click()")
    assert _hidden() == "false", "toolbar click closed the lightbox"

    # 3. Backdrop click (the #lightbox element itself) closes.
    page.evaluate("document.getElementById('lightbox').click()")
    assert _hidden() == "true", "backdrop click did not close the lightbox"

    # 4. The explicit close control closes.
    _show()
    page.evaluate(
        "document.querySelector('#lightbox [data-action=\\\"close-lightbox\\\"]').click()"
    )
    assert _hidden() == "true", "close control did not close the lightbox"

    assert not page_errors, f"uncaught page errors: {page_errors}"
    page.close()


def _read_download_bytes(download) -> bytes:
    """Read a Playwright download's bytes via its saved path."""
    path = download.path()
    with open(path, "rb") as fh:
        return fh.read()
