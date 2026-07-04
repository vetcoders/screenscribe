"""Cold-reload merged-card hydration must be FULLY functional (N1-N4, P2).

Fix #3/L2/L3/M2 made a merge survive in DATA (``reportState.merges`` round-trip)
and reach the DOM on hydrate (``restoreMergesToDom``) with full review controls.
But a *cold reload* — the ``/api/review-state`` path where ``reportState.merges``
starts EMPTY and the fold is rebuilt purely from the persisted
``merged_from_ids`` trail — reconstructed the merged card only fragmentarily:

  * N1: ``findFindingArticle`` compared ids strictly, but ``merged_from_ids``
    carries NUMBERS from the report JSON while ``dataset.findingId`` is a STRING,
    so absorbed members failed to fold and stayed visible.
  * N2: the merged summary-edit handler looked the group up only in
    ``reportState.merges`` (empty after cold reload), so edits were dropped and
    the review never marked modified.
  * N3: ``renderMergedCard`` hard-coded ``data-verdict="accepted"``, so a merge
    hydrated after the survivor was rejected appeared/counted as accepted.
  * N4: merged cards are inserted AFTER the initial ``bindThumbnailClicks`` pass,
    so the generated thumbnail had no lightbox/annotation click binding.

The holistic fix reconstructs a normalized in-memory merge entry for every
restored group (``ensureMergeEntry`` in ``restoreMergesToDom``), honours the
persisted verdict, normalizes id comparison, and binds the merged thumbnail.
This test drives the real ``hydrateReportState`` against a functional DOM and
asserts the cold-reload merged card is fully functional in ONE round-trip.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "screenscribe/html_pro_assets/scripts"
I18N_JS = SCRIPTS / "i18n.js"
LANGUAGE_CONTROL_JS = SCRIPTS / "lib/language-control.js"
STT_TRANSPORT_JS = SCRIPTS / "lib/stt-transport.js"
TAB_KEYBOARD_JS = SCRIPTS / "lib/tab-keyboard.js"
REVIEW_APP_JS = SCRIPTS / "review_app.js"
DOM_HELPER_JS = Path(__file__).resolve().parent / "_review_dom.js"


def _finding(fid: int, ts: float, summary: str) -> dict:
    return {
        "id": fid,
        "timestamp": ts,
        "timestamp_formatted": f"{int(ts) // 60:02d}:{int(ts) % 60:02d}",
        "category": "ui",
        "text": f"transcript line for finding {fid}",
        "screenshot": "data:image/jpeg;base64,QUJD",
        "unified_analysis": {
            "summary": summary,
            "severity": "low",
            "action_items": [f"do thing {fid}"],
            "affected_components": [f"Component{fid}"],
            "issues_detected": [f"issue {fid}"],
        },
    }


# INTEGER ids, exactly as JSON.parse yields them in the browser.
_FINDINGS = [
    _finding(17, 17.0, "survivor of the merge"),
    _finding(18, 18.0, "absorbed member"),
]


def _run(driver_body: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for review_app.js merge tests")

    runner = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const {{ createDocument, buildFindingArticle }} = require({str(DOM_HELPER_JS)!r});

        const findings = {json.dumps(_FINDINGS)};
        const {{ document, body, tab }} = createDocument(findings);
        for (const f of findings) tab.appendChild(buildFindingArticle(document, f));

        const sandbox = {{
            console, setTimeout, clearTimeout, setInterval, clearInterval,
            Math, Date, JSON, Promise, Array, Object, String, Number, Boolean, Set, Map,
            window: {{
                location: {{ search: '' }},
                addEventListener() {{}}, removeEventListener() {{}},
                setTimeout, clearTimeout, setInterval, clearInterval,
                TRANSCRIPT_SEGMENTS: [],
            }},
            document, tab, body,
            localStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
            navigator: {{ mediaDevices: {{}} }},
            Blob: class Blob {{ constructor(c, o = {{}}) {{ this.chunks = c; this.type = o.type || ''; }} }},
            ResizeObserver: class ResizeObserver {{ observe() {{}} disconnect() {{}} }},
            Image: class Image {{}},
            process, confirm() {{ return true; }},
            fetch() {{ throw new Error('no network in test'); }},
        }};
        sandbox.window.document = document;
        sandbox.window.navigator = sandbox.navigator;
        sandbox.globalThis = sandbox;

        const i18nSource = fs.readFileSync({str(I18N_JS)!r}, 'utf8');
        const languageControlSource = fs.readFileSync({str(LANGUAGE_CONTROL_JS)!r}, 'utf8');
        const sttTransportSource = fs.readFileSync({str(STT_TRANSPORT_JS)!r}, 'utf8');
        const tabKeyboardSource = fs.readFileSync({str(TAB_KEYBOARD_JS)!r}, 'utf8');
        const source = fs.readFileSync({str(REVIEW_APP_JS)!r}, 'utf8');

        const driver = `
            reportState.reviewer = 'qa';
            reportState.manualFrames = [];
            // The server adds the merge checkboxes on init; mirror that so the
            // cold-reload path sees the same surface a live page has.
            initMergeUI();
            {driver_body}
        `;

        const script = new vm.Script(
            i18nSource + "\\n" + languageControlSource + "\\n" + sttTransportSource + "\\n" +
            tabKeyboardSource + "\\n" + source + "\\n" + driver,
            {{ filename: 'review_app.js' }}
        );
        script.runInNewContext(sandbox);
        """
    )
    result = subprocess.run([node, "-e", runner], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_cold_reload_merged_card_is_fully_functional() -> None:
    """One round-trip: cold reload from merged_from_ids -> fully usable merged card.

    The snapshot mimics a fresh ``/api/review-state`` load AFTER the reviewer had
    merged 17+18 and rejected the survivor: the survivor carries the
    ``merged_from_ids`` trail (NUMERIC, as in the report JSON) and a ``rejected``
    verdict, and there is NO ``merges`` array (so ``reportState.merges`` is empty
    — the cold-reload condition the four edges share).
    """
    _run(
        textwrap.dedent(
            """
            const coldSnapshot = {
                reviewer: 'qa',
                modified: false,
                manualFrames: [],
                findings: {
                    // Survivor: rejected verdict + NUMERIC merged_from_ids trail.
                    '17': { verdict: 'rejected', merged_from_ids: [18] },
                    '18': { verdict: 'none' },
                },
                // merges intentionally OMITTED -> reportState.merges empty.
            };

            hydrateReportState(coldSnapshot);

            if ((reportState.merges || []).length === 0)
                throw new Error('cold reload should reconstruct an in-memory merge entry');

            // (1) absorbed member hidden + a merged summary card exists.
            const member = tab.querySelectorAll('.finding')
                .find((a) => a.dataset.findingId === '18' && a.dataset.merged !== 'true');
            if (!member) throw new Error('member card 18 not found');
            if (member.dataset.mergedAway !== 'true' || member.hidden !== true)
                throw new Error('N1: absorbed member 18 still visible after cold reload: '
                    + 'mergedAway=' + member.dataset.mergedAway + ' hidden=' + member.hidden);

            const mergedCards = tab.querySelectorAll('.finding-merged');
            if (mergedCards.length !== 1)
                throw new Error('expected exactly 1 merged card, got ' + mergedCards.length);
            const card = mergedCards[0];
            if (card.dataset.findingId !== '17')
                throw new Error('merged card wrong survivor id: ' + card.dataset.findingId);

            // (2) member ids matched despite numeric (JSON) vs string (DOM).
            const entry = (reportState.merges || []).find((m) => normId(m.id) === '17');
            if (!entry) throw new Error('reconstructed merge entry missing');
            const members = (entry.member_ids || []).map(normId).sort();
            if (JSON.stringify(members) !== JSON.stringify(['17', '18']))
                throw new Error('N1: member ids not normalized: ' + JSON.stringify(members));

            // (3) restored verdict honoured (rejected, NOT hard-coded accepted).
            if (card.dataset.verdict !== 'rejected')
                throw new Error('N3: merged card verdict not restored: '
                    + card.dataset.verdict + ' (want rejected)');
            if (getReviewStatus(card) !== 'rejected')
                throw new Error('N3: getReviewStatus disagrees: ' + getReviewStatus(card));
            const rejectRadio = card.querySelector('input[value="rejected"]');
            if (!rejectRadio || rejectRadio.checked !== true)
                throw new Error('N3: reject radio not checked on restored merged card');

            // (4) summary edit on the cold-reloaded card is recorded + marks modified.
            reportState.modified = false;
            const summary = card.querySelector('.merged-summary');
            if (!summary) throw new Error('merged card has no summary textarea');
            summary.value = 'EDITED AFTER COLD RELOAD';
            summary.dispatch('input', {});
            const after = (reportState.merges || []).find((m) => normId(m.id) === '17');
            if (!after || after.summary_override !== 'EDITED AFTER COLD RELOAD')
                throw new Error('N2: summary edit dropped after cold reload: '
                    + (after && after.summary_override));
            if (reportState.modified !== true)
                throw new Error('N2: summary edit did not mark review modified');

            // (5) merged thumbnail has lightbox click binding.
            const thumb = card.querySelector('.thumbnail');
            if (!thumb) throw new Error('merged card has no thumbnail');
            if (thumb.dataset.lightboxBound !== 'true')
                throw new Error('N4: merged thumbnail not lightbox-bound');
            if (!thumb._listeners || !(thumb._listeners.click || []).length)
                throw new Error('N4: merged thumbnail has no click handler');
            """
        )
    )


def test_find_finding_article_matches_numeric_ids() -> None:
    """``findFindingArticle`` locates a DOM card given a NUMERIC id (N1 unit).

    The data-side restore fallback calls this helper with numeric ids straight
    from ``merged_from_ids``; ``dataset.findingId`` is always a string. A strict
    ``===`` silently misses, leaving absorbed members visible.
    """
    _run(
        textwrap.dedent(
            """
            const byNumber = findFindingArticle(18);
            if (!byNumber) throw new Error('findFindingArticle(18) returned null for numeric id');
            if (byNumber.dataset.findingId !== '18')
                throw new Error('findFindingArticle matched wrong card: ' + byNumber.dataset.findingId);
            const byString = findFindingArticle('18');
            if (byString !== byNumber)
                throw new Error('numeric and string lookups must resolve to the same card');
            """
        )
    )
