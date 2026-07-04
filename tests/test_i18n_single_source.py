"""3-way i18n drift guard (PKG7/C7.1).

Screenscribe keeps UI strings in THREE hand-maintained sources:

  - ``screenscribe/html_pro/renderer.py`` :: ``_I18N``          flat ``dict[lang][key]``
  - ``screenscribe/shell/renderer.py``    :: ``_SERVER_I18N``   ``dict[lang][ns][key]``
  - ``html_pro_assets/scripts/i18n.js``   :: ``window.I18N_BUNDLE`` ``dict[lang][ns][key]``

The three are intentionally different *subsets* (the Python report renderer, the
server-rendered chrome, and the JS runtime each need their own slice), so they
are NOT required to carry identical key sets. What they MUST agree on is the
*value* of any string they happen to share — otherwise the same logical label
renders differently depending on the render path.

Full generator consolidation (one source -> all three) is deferred (see the C7.1
report): the existing English-only drifts recorded below are a user-visible,
operator-owned decision (which casing/wording is canonical), and the anti-drift
doctrine forbids guessing canonical user-facing text. This guard instead PINS
the current truth so no *new* drift can land silently: any shared key whose value
diverges across sources — and is not in the explicit registry below — fails.

Injecting drift into ANY of the three sources (changing a shared value, or
breaking en/pl parity) makes this suite red, which is the C7.1 acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path

from screenscribe.html_pro import renderer as html_pro_renderer
from screenscribe.html_pro.renderer import _I18N as FLAT
from screenscribe.shell.renderer import _SERVER_I18N as SERVER

_I18N_JS = (
    Path(html_pro_renderer.__file__).resolve().parent.parent
    / "html_pro_assets"
    / "scripts"
    / "i18n.js"
)

_REVIEW_APP_JS = (
    Path(html_pro_renderer.__file__).resolve().parent.parent
    / "html_pro_assets"
    / "scripts"
    / "review_app.js"
)


def _load_bundle() -> dict[str, dict[str, dict[str, str]]]:
    """Parse the ``window.I18N_BUNDLE = {...};`` object literal out of i18n.js.

    The bundle is authored as strict JSON (double-quoted keys/values, no trailing
    commas), so once isolated from its assignment it round-trips through
    ``json.loads`` directly.
    """
    text = _I18N_JS.read_text(encoding="utf-8")
    marker = "window.I18N_BUNDLE = "
    start = text.index(marker) + len(marker)
    end = text.index("\n};", start)
    obj = text[start : end + 2].rstrip()
    if obj.endswith(";"):
        obj = obj[:-1]
    return json.loads(obj)


BUNDLE = _load_bundle()

# --- Registries of *existing* divergence (pinned so they cannot change silently) ---

# C7.1b: the 4 English-only _I18N<->bundle drifts were resolved by operator
# decision -- Title Case canonical for the finding headings (affectedComponents /
# suggestedFix / visualIssues), and "jump to" canonical for the seek tooltip
# (clickToSeek). The sources were aligned in the same change, so this registry is
# now EMPTY: the guard below enforces ZERO divergence. Any re-introduced or new
# _I18N<->bundle drift fails the suite -- the standing C7.1/C7.1b acceptance.
KNOWN_FLAT_BUNDLE_DRIFT: dict[tuple[str, str], tuple[str, str]] = {}

# Intentional (NOT drift): the server renders the static initial state ("0 errors")
# while the JS bundle holds the runtime template ("{{n}} errors") substituted at
# render time via data-i18n-tpl. Pinned so the pair cannot mutate unnoticed.
INTENTIONAL_SERVER_BUNDLE_DIVERGENCE: dict[tuple[str, str, str], tuple[str, str]] = {
    ("en", "analyze", "errors_count"): ("0 errors", "{{n}} errors"),
    ("pl", "analyze", "errors_count"): ("0 błędów", "{{n}} błędów"),
}


def _bundle_flat(lang: str) -> dict[str, set[str]]:
    """Collapse the namespaced bundle to ``key -> {values}`` for the given lang."""
    out: dict[str, set[str]] = {}
    for namespace in BUNDLE[lang].values():
        for key, value in namespace.items():
            out.setdefault(key, set()).add(value)
    return out


# ---------------------------------------------------------------------------
# Shape + language parity
# ---------------------------------------------------------------------------


# POI category labels (semantic_filter.POI_CATEGORIES + the "unknown" fallback)
# render as badges in three paths: the server report (_I18N flat, html_pro), the
# analyze dashboard (bundle.analyze) and the review merged card (bundle.review).
# All three must carry the family in both languages or a badge leaks the raw enum.
_CATEGORY_KEYS: tuple[str, ...] = (
    "category_bug",
    "category_change",
    "category_ui",
    "category_performance",
    "category_accessibility",
    "category_other",
    "category_unknown",
)


def test_finding_category_labels_localized_in_all_sources() -> None:
    """The category_* family resolves in every render path (FW-05 commit 1)."""
    offenders: list[str] = []
    for lang in ("en", "pl"):
        for key in _CATEGORY_KEYS:
            if key not in FLAT[lang]:
                offenders.append(f"_I18N.{lang}.{key}")
            for namespace in ("analyze", "review"):
                if key not in BUNDLE[lang].get(namespace, {}):
                    offenders.append(f"bundle.{lang}.{namespace}.{key}")
    assert not offenders, "category i18n keys missing:\n" + "\n".join(offenders)


def test_bundle_has_expected_shape() -> None:
    assert set(BUNDLE) == {"en", "pl"}
    for lang in ("en", "pl"):
        assert set(BUNDLE[lang]) == {"shell", "media", "review", "analyze"}


def test_each_source_has_en_pl_parity() -> None:
    """No key may exist in only one language in any source."""
    offenders: list[str] = []

    # Flat source.
    if set(FLAT["en"]) != set(FLAT["pl"]):
        offenders.append(f"_I18N flat: {set(FLAT['en']) ^ set(FLAT['pl'])}")

    # Namespaced sources.
    for name, source in (("_SERVER_I18N", SERVER), ("I18N_BUNDLE", BUNDLE)):
        if set(source["en"]) != set(source["pl"]):
            offenders.append(
                f"{name}: namespace sets differ {set(source['en']) ^ set(source['pl'])}"
            )
            continue
        for namespace in source["en"]:
            en_keys = set(source["en"][namespace])
            pl_keys = set(source["pl"][namespace])
            if en_keys != pl_keys:
                offenders.append(f"{name}.{namespace}: {en_keys ^ pl_keys}")

    assert not offenders, "en/pl parity broken:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Cross-source value agreement (the drift guard proper)
# ---------------------------------------------------------------------------


def test_flat_and_bundle_shared_values_agree() -> None:
    """Every key shared by ``_I18N`` and the JS bundle must carry the same value,
    except the explicitly registered (pending-fix) drifts."""
    offenders: list[str] = []
    for lang in ("en", "pl"):
        bundle_flat = _bundle_flat(lang)
        for key, value in FLAT[lang].items():
            if key not in bundle_flat:
                continue  # flat-only key (Python report chrome) — not shared
            if value in bundle_flat[key]:
                continue  # agrees
            if (lang, key) in KNOWN_FLAT_BUNDLE_DRIFT:
                continue  # quarantined, asserted exactly by the registry test
            offenders.append(
                f"{lang}.{key}: _I18N={value!r} vs bundle={sorted(bundle_flat[key])!r}"
            )
    assert not offenders, "new _I18N<->bundle drift (align values or register):\n" + "\n".join(
        offenders
    )


def test_server_and_bundle_shared_values_agree() -> None:
    """Every namespace+key shared by ``_SERVER_I18N`` and the JS bundle must agree,
    except the registered intentional template-vs-static divergence."""
    offenders: list[str] = []
    for lang in ("en", "pl"):
        for namespace, items in SERVER[lang].items():
            for key, value in items.items():
                bundle_value = BUNDLE[lang].get(namespace, {}).get(key)
                if bundle_value is None:
                    continue  # server-only key (e.g. aria labels) — not shared
                if bundle_value == value:
                    continue
                if (lang, namespace, key) in INTENTIONAL_SERVER_BUNDLE_DIVERGENCE:
                    continue
                offenders.append(
                    f"{lang}.{namespace}.{key}: server={value!r} vs bundle={bundle_value!r}"
                )
    assert not offenders, "new _SERVER_I18N<->bundle drift:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Registry accuracy (keeps the quarantine honest)
# ---------------------------------------------------------------------------


def test_known_flat_bundle_drift_registry_is_accurate() -> None:
    """Each registered drift must still be a real, current divergence. Aligning a
    pair (the eventual consolidation fix) flips this red, forcing the registry to
    shrink in the same change — the quarantine can never rot into a silent pass."""
    stale: list[str] = []
    for (lang, key), (flat_expected, bundle_expected) in KNOWN_FLAT_BUNDLE_DRIFT.items():
        actual_flat = FLAT.get(lang, {}).get(key)
        bundle_values = _bundle_flat(lang).get(key, set())
        if actual_flat != flat_expected:
            stale.append(f"{lang}.{key}: _I18N now {actual_flat!r}, registry {flat_expected!r}")
        if bundle_expected not in bundle_values:
            stale.append(
                f"{lang}.{key}: bundle now {sorted(bundle_values)!r}, registry {bundle_expected!r}"
            )
        if flat_expected == bundle_expected:
            stale.append(f"{lang}.{key}: registered pair no longer diverges (remove it)")
    assert not stale, "KNOWN_FLAT_BUNDLE_DRIFT is out of date:\n" + "\n".join(stale)


def test_intentional_server_bundle_divergence_registry_is_accurate() -> None:
    stale: list[str] = []
    for (lang, ns, key), (
        server_expected,
        bundle_expected,
    ) in INTENTIONAL_SERVER_BUNDLE_DIVERGENCE.items():
        actual_server = SERVER.get(lang, {}).get(ns, {}).get(key)
        actual_bundle = BUNDLE.get(lang, {}).get(ns, {}).get(key)
        if actual_server != server_expected:
            stale.append(f"{lang}.{ns}.{key}: server now {actual_server!r}")
        if actual_bundle != bundle_expected:
            stale.append(f"{lang}.{ns}.{key}: bundle now {actual_bundle!r}")
    assert not stale, "INTENTIONAL_SERVER_BUNDLE_DIVERGENCE is out of date:\n" + "\n".join(stale)


# ---------------------------------------------------------------------------
# Markdown-export labels under the guard (C7.1 residue)
# ---------------------------------------------------------------------------
#
# ``buildTodoMarkdown`` (review_app.js) builds the reviewer's downloadable TODO
# ``.md``. Its section headings and inline labels used to be hardcoded English,
# so a PL reviewer received a half-translated file ("Recenzent" next to
# "## AI findings") and the strings could drift from the rest of the UI without
# any test going red. C7.1's residue pulls them into the same single-source
# ``review`` namespace of the JS bundle, and the guards below keep them there.

# New ``review`` keys that back the markdown TODO export. They MUST resolve in
# both languages (en/pl parity is also enforced bundle-wide above) and MUST be
# referenced via ``t('review.<key>')`` from ``buildTodoMarkdown`` — never
# re-hardcoded.
TODO_MARKDOWN_KEYS: tuple[str, ...] = (
    "todoAiFindingsSection",
    "todoNoAiFindings",
    "todoNotesLabel",
    "todoActionsLabel",
    "todoManualSection",
    "todoNoManualCaptures",
    "todoManualItemLabel",
    "todoManualCaptureDefault",
    "todoCategoryLabel",
    "todoTranscriptLabel",
    "todoSuggestedFixLabel",
    "todoManualNotAnalyzed",
    "todoFileLabel",
    "todoNoDescription",
    "todoAnnotationLabel",
    "annotationArrow",
    "annotationRect",
    "annotationPen",
    "annotationText",
)

# Severity headings (critical..low) and the merge-provenance label in the TODO
# reuse existing UI labels rather than minting parallel keys — the export must
# speak the same words as the on-screen review surface.
TODO_MARKDOWN_REUSED_KEYS: tuple[str, ...] = (
    "critical",
    "high",
    "medium",
    "low",
    "mergedFromLabel",
)

# Literals that would only appear in ``buildTodoMarkdown`` if a label were
# re-hardcoded instead of sourced from i18n. Their VALUES live in i18n.js, so
# none of these may reappear in review_app.js.
_TODO_HARDCODED_OFFENDERS: tuple[str, ...] = (
    "## AI findings",
    "## Manual captures",
    "_No AI findings._",
    "_No manual captures._",
    "### Critical",
    "### High",
    "### Medium",
    "### Low",
    "not AI-analyzed yet",
)


def _build_todo_markdown_body() -> str:
    """Isolate the markdown-TODO export region from review_app.js.

    The export's label rendering spans ``describeAnnotations`` (the annotation
    summary helper) plus ``buildTodoMarkdown`` itself — both sit back-to-back and
    end before ``saveReviewToDisk`` — so the guard covers the whole surface that
    emits TODO labels, not only the outer function.
    """
    text = _REVIEW_APP_JS.read_text(encoding="utf-8")
    start = text.index("function describeAnnotations(")
    end = text.index("\nasync function saveReviewToDisk", start)
    return text[start:end]


def test_todo_markdown_labels_exist_in_bundle() -> None:
    """Every markdown-export label resolves in both languages of the bundle."""
    offenders: list[str] = []
    for lang in ("en", "pl"):
        review = BUNDLE[lang].get("review", {})
        for key in TODO_MARKDOWN_KEYS + TODO_MARKDOWN_REUSED_KEYS:
            if key not in review:
                offenders.append(f"{lang}.review.{key} missing")
    assert not offenders, "markdown-export labels missing from i18n bundle:\n" + "\n".join(
        offenders
    )


def test_todo_markdown_export_is_i18n_sourced() -> None:
    """``buildTodoMarkdown`` sources its labels from i18n and hardcodes none.

    Drift direction one: a label dropped from a dict is caught by
    ``test_todo_markdown_labels_exist_in_bundle`` (+ en/pl parity). Drift
    direction two: a label re-hardcoded in JS is caught here — the ``t()``
    reference disappears and/or the raw English literal reappears.
    """
    body = _build_todo_markdown_body()

    missing_refs = [
        key for key in TODO_MARKDOWN_KEYS + TODO_MARKDOWN_REUSED_KEYS if f"review.{key}" not in body
    ]
    assert not missing_refs, (
        "buildTodoMarkdown no longer routes these labels through t('review.<key>'):\n"
        + "\n".join(missing_refs)
    )

    re_hardcoded = [literal for literal in _TODO_HARDCODED_OFFENDERS if literal in body]
    assert not re_hardcoded, (
        "buildTodoMarkdown re-hardcoded a markdown label (route it through i18n):\n"
        + "\n".join(re_hardcoded)
    )
