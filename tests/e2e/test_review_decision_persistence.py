"""F1 — review decision persistence round-trip (DIAGNOSTIC).

Closes the P1 coverage gap from the e2e adversarial audit: there was ZERO
coverage of reviewer decisions over AI findings (verdict / severity-override /
notes / merge), because the pipeline fixture yields an empty-state report
(0 findings) and every browser test drives manual frames only. A suspected bug
("export drops or overwrites decisions to accepted") would pass green.

This file renders a review report with ≥3 real findings over ``file://`` (no API,
no ffmpeg — same self-contained path as ``test_smoke_no_api``), sets real
decisions through the REAL client handlers (verdict radios, severity select,
notes textarea via document-delegated ``handleChangeEvent``/``handleInputEvent``),
then runs the REAL export (``exportReviewedJSON``) and asserts the round-trip.

Diagnostic intent: each assertion encodes CORRECT behaviour. A RED here is a
FOUND BUG (input to cut D for notes, cut B for merge), never a reason to weaken
the test. Genuinely-missing features are marked ``xfail(strict=True)`` with an
explicit cut reference so the suite stays green while the gap stays tracked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.browser, pytest.mark.requires_playwright]

# Notes set on the two findings that carry them. The rejected note travels the
# `rejected[]` summary path (buildRejectedSummary); the accepted note travels the
# `human_review.notes` path (buildReviewData) — two DIFFERENT code paths, both
# must round-trip. Non-ASCII on purpose: notes are reviewer prose, often Polish.
REJECTED_NOTE = "STT zle zrozumialo, core to refactor not a layout bug"
ACCEPTED_NOTE = "confirmed regression, raise priority"
REVIEWER = "F1 Reviewer"


def _render_review_html_with_findings() -> str:
    """Self-contained REVIEW report HTML carrying 3 findings (no API/ffmpeg).

    Mirrors ``test_smoke_no_api._render_review_html`` but with a non-empty
    ``findings`` list so the reviewer-decision surface (verdict / severity /
    notes / rejected[]) is actually reachable.
    """
    from screenscribe.html_pro.renderer import render_html_report_pro

    findings = [
        {
            "id": "f1",
            "category": "layout",
            "timestamp": 1.0,
            "timestamp_formatted": "00:01",
            "text": "Finding one transcript",
            "unified_analysis": {
                "severity": "high",
                "summary": "High severity finding one",
                "action_items": ["Fix layout overflow", "Add responsive test"],
                "affected_components": ["Header"],
            },
        },
        {
            "id": "f2",
            "category": "transcription",
            "timestamp": 2.0,
            "timestamp_formatted": "00:02",
            "text": "Finding two transcript",
            "unified_analysis": {
                "severity": "medium",
                "summary": "Medium severity finding two",
            },
        },
        {
            "id": "f3",
            "category": "performance",
            "timestamp": 3.0,
            "timestamp_formatted": "00:03",
            "text": "Finding three transcript",
            "unified_analysis": {
                "severity": "low",
                "summary": "Low severity finding three, a noticeably richer description",
                "action_items": ["Profile the render path", "Add responsive test"],
                "affected_components": ["Renderer"],
            },
        },
    ]
    return render_html_report_pro(
        video_name="persistence-demo.mov",
        video_path=None,
        generated_at="2026-06-27T10:00:00Z",
        executive_summary="Persistence round-trip fixture.",
        findings=findings,
        segments=[],
    )


@pytest.fixture(scope="module")
def review_findings_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Render the 3-finding REVIEW report once and return a ``file://`` URL."""
    path = tmp_path_factory.mktemp("f1_review") / "review_report.html"
    path.write_text(_render_review_html_with_findings(), encoding="utf-8")
    return path.as_uri()


def _open_findings_tab(page) -> None:
    """Switch to the Findings tab so the finding cards become visible/interactive.

    Findings live in ``#tab-findings`` which is NOT the default-active tab
    (Summary is), so the articles render hidden until the reviewer opens the tab.
    """
    page.locator('.tab-btn[data-tab="findings"]').click()
    page.wait_for_selector("#tab-findings.active", timeout=15000)
    page.wait_for_selector('.finding[data-finding-id="f1"]', state="visible", timeout=15000)


def _finding(page, finding_id: str):
    return page.locator(f'.finding[data-finding-id="{finding_id}"]')


def _set_decisions(page) -> None:
    """Drive real reviewer decisions through the actual client handlers.

    ``.check()`` / ``.fill()`` / ``.select_option()`` dispatch genuine
    input/change events that bubble to the document-level delegation registered
    in ``initReviewState`` — so this exercises ``handleChangeEvent`` /
    ``handleInputEvent`` -> ``reportState.findings``, not a back-door state poke.
    """
    # f1 -> accepted (no override, no notes).
    _finding(page, "f1").locator('input[type="radio"][value="accepted"]').check()

    # f2 -> rejected, with a note (must land in rejected[].notes, NOT deliverable).
    _finding(page, "f2").locator('input[type="radio"][value="rejected"]').check()
    _finding(page, "f2").locator(".review-field.notes textarea").fill(REJECTED_NOTE)

    # f3 -> accepted, severity override low->critical, with a note
    # (must land in human_review.severity_override + human_review.notes).
    _finding(page, "f3").locator('input[type="radio"][value="accepted"]').check()
    _finding(page, "f3").locator(".severity-select").select_option("critical")
    _finding(page, "f3").locator(".review-field.notes textarea").fill(ACCEPTED_NOTE)


def _export_reviewed_json(page) -> dict:
    """Run the REAL export and return the parsed deliverable JSON.

    Drives ``exportReviewedJSON()`` — the exact user path that builds the
    deliverable (``findings`` minus rejected, plus ``rejected[]``) — and captures
    the downloaded blob, so the assertions check shipped bytes, not an internal."""
    page.evaluate("(name) => { reportState.reviewer = name; }", REVIEWER)
    with page.expect_download(timeout=30000) as dl_info:
        page.evaluate("exportReviewedJSON()")
    download = dl_info.value
    raw = Path(download.path()).read_text(encoding="utf-8")
    return json.loads(raw)


def _by_id(findings: list[dict]) -> dict[str, dict]:
    return {f["id"]: f for f in findings}


def test_finding_decisions_roundtrip_through_export(review_findings_url, browser_context) -> None:
    """accept / reject / severity-override / notes survive export, with negatives.

    GREEN-expected (encodes correct behaviour, hard asserts). If any of these go
    RED at runtime it is a discovered persistence bug -> escalate to cut D, do not
    soften the assertion.
    """
    page = browser_context.new_page()
    # Export with reviewed findings does not pop the "no reviewed" confirm, but
    # auto-accept any dialog defensively.
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(review_findings_url, wait_until="load")
    _open_findings_tab(page)

    _set_decisions(page)
    out = _export_reviewed_json(page)

    deliverable = _by_id(out.get("findings", []))
    rejected = _by_id(out.get("rejected", []))

    # --- accepted stays in the deliverable ------------------------------------
    assert "f1" in deliverable, "accepted finding f1 dropped from deliverable findings"
    assert "f3" in deliverable, "accepted finding f3 dropped from deliverable findings"

    # --- rejected leaves the deliverable and lands in rejected[] (NEGATIVE) ----
    assert "f2" not in deliverable, (
        "rejected finding f2 leaked into deliverable findings "
        "(this is exactly the suspected 'reject not honored' regression)"
    )
    assert "f2" in rejected, "rejected finding f2 missing from rejected[] summary"

    # --- verdict not overwritten ----------------------------------------------
    assert deliverable["f1"]["human_review"]["verdict"] == "accepted", (
        "f1 verdict overwritten in export"
    )
    assert deliverable["f3"]["human_review"]["verdict"] == "accepted", (
        "f3 verdict overwritten in export"
    )

    # --- anti-flip-to-accepted (hard negative) --------------------------------
    for f in out.get("findings", []):
        assert f["human_review"]["verdict"] != "rejected", (
            f"a rejected finding survived in the deliverable: {f['id']}"
        )
    assert rejected["f2"].get("rejected_by") == REVIEWER, "rejected[] lost the reviewer attribution"

    # --- severity override round-trip -----------------------------------------
    assert deliverable["f3"]["human_review"]["severity_override"] == "critical", (
        "f3 severity override (low->critical) lost in export"
    )
    assert deliverable["f1"]["human_review"]["severity_override"] is None, (
        "f1 gained a phantom severity override (none was set)"
    )

    page.close()


def test_notes_roundtrip_through_export(review_findings_url, browser_context) -> None:
    """Notes survive export on BOTH paths: rejected[].notes and human_review.notes.

    SCAFFOLD falsify guard: assert EXACT note text (not a source string-match) so
    "notes work on a word" cannot pass. A RED here is the cut-D signal.
    """
    page = browser_context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(review_findings_url, wait_until="load")
    _open_findings_tab(page)

    _set_decisions(page)
    out = _export_reviewed_json(page)

    deliverable = _by_id(out.get("findings", []))
    rejected = _by_id(out.get("rejected", []))

    # rejected note path (buildRejectedSummary).
    assert rejected["f2"].get("notes") == REJECTED_NOTE, (
        f"rejected note lost/mangled in export: {rejected['f2'].get('notes')!r}"
    )
    # accepted note path (buildReviewData.human_review).
    assert deliverable["f3"]["human_review"].get("notes") == ACCEPTED_NOTE, (
        f"accepted note lost/mangled in export: {deliverable['f3']['human_review'].get('notes')!r}"
    )

    page.close()


def test_merged_findings_collapse_to_single(review_findings_url, browser_context) -> None:
    """N findings merged -> a single deliverable entry carrying merged_from_ids.

    Cut B (human-merge). The reviewer selects two findings via the real merge
    checkboxes, clicks the real merge button, and the two cards collapse to one
    merged card in the viewer (2 -> 1). On export the deliverable carries a SINGLE
    entry for the group (the surviving base id), with the absorbed id gone, the
    union of action_items / affected_components preserved (nothing lost), the
    richest description kept, and a merged_from_ids provenance trail.
    """
    page = browser_context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(review_findings_url, wait_until="load")
    _open_findings_tab(page)

    # Client merge surface exists (the exact contract the strict-xfail tracked).
    assert page.evaluate(
        "typeof window.mergeSelectedFindings === 'function' "
        "&& typeof window.mergeFindings === 'function'"
    ), "human-merge client functions missing (cut B)"

    # --- drive the REAL UI: select f1 + f3, click merge ----------------------
    _finding(page, "f1").locator(".merge-select").check()
    _finding(page, "f3").locator(".merge-select").check()
    page.locator("#merge-action-btn").click()

    # Walk-around 2 -> 1 in the viewer: a merged card appears, the absorbed
    # original card (f3) is hidden.
    page.wait_for_selector(".finding.finding-merged", state="visible", timeout=15000)
    assert _finding(page, "f3").first.is_hidden(), (
        "absorbed finding f3 should be hidden after merge (still visible -> not collapsed)"
    )

    # --- export and assert the collapsed deliverable -------------------------
    out = _export_reviewed_json(page)
    deliverable = _by_id(out.get("findings", []))

    # f1 (earliest -> base, surviving id) stays; f3 is absorbed; f2 untouched.
    assert "f1" in deliverable, "surviving merged finding f1 dropped from deliverable"
    assert "f3" not in deliverable, "absorbed finding f3 leaked as a standalone deliverable entry"
    assert "f2" in deliverable, "unrelated finding f2 must be untouched by the merge"

    merged = deliverable["f1"]
    trail = merged.get("merged_from_ids") or []
    assert "f3" in trail, f"merged_from_ids must record the absorbed id f3: {trail!r}"

    ua = merged.get("unified_analysis", {})
    actions = set(ua.get("action_items", []))
    assert {"Fix layout overflow", "Profile the render path", "Add responsive test"} <= actions, (
        f"action_items union lost a theme: {ua.get('action_items')!r}"
    )
    assert sum(1 for a in ua.get("action_items", []) if a == "Add responsive test") == 1, (
        f"action_items did not de-duplicate the shared theme: {ua.get('action_items')!r}"
    )
    assert {"Header", "Renderer"} <= set(ua.get("affected_components", [])), (
        f"affected_components union lost a component: {ua.get('affected_components')!r}"
    )
    # Highest severity wins; richest (longest) description is kept.
    assert ua.get("severity") == "high", f"merged severity must be highest: {ua.get('severity')!r}"
    assert "noticeably richer description" in (ua.get("summary") or ""), (
        f"merged summary must keep the richest description: {ua.get('summary')!r}"
    )

    page.close()
