"""Doc-truth guard: default LLM/Vision model declared identically in code and docs.

Falsifiable guard for the C2.1 cut. The product default LLM/Vision model is
``programmer`` (the LibraxisAI profile); ``ai-suggestions`` is the LEGACY name and
must never reappear as a *documented default* in the public-facing surfaces
(``README.md``, ``USAGE.md``, ``.env.example``).

Scope is intentionally doc-narrow so legit ``ai-suggestions`` literals elsewhere
stay untouched:
  - ``tests/test_validation.py`` fixtures (arbitrary model-name input)
  - ``screenscribe/config.py:20`` explanatory comment about the legacy name
  - the ``.ai-suggestions`` CSS class in renderer / report-pro.css

This guard runs under ``make verify`` (``scripts/ss_verify.py`` collects the
pytest suite), so a docs/code drift turns the gate red instead of rotting
silently. The complementary config-side guard lives in
``tests/test_config_env.py`` (``TestModelDefaults``); this one closes the docs
side.
"""

from __future__ import annotations

import re
from pathlib import Path

from screenscribe.config import DEFAULT_LLM_MODEL, DEFAULT_VISION_MODEL

REPO_ROOT = Path(__file__).resolve().parents[1]

# Public-facing surfaces that must agree with the code default. Intentionally a
# small allowlist — NOT a repo-wide scan — so legit legacy-name literals are safe.
DOC_SURFACES = ("README.md", "USAGE.md", ".env.example")

LEGACY_NAME = "ai-suggestions"
PRODUCT_DEFAULT = "programmer"


def test_code_default_is_programmer() -> None:
    """The single source of truth (config.py) declares ``programmer``, not legacy."""
    assert DEFAULT_LLM_MODEL == PRODUCT_DEFAULT, (
        f"config DEFAULT_LLM_MODEL drifted to {DEFAULT_LLM_MODEL!r}; expected {PRODUCT_DEFAULT!r}"
    )
    assert DEFAULT_VISION_MODEL == PRODUCT_DEFAULT, (
        f"config DEFAULT_VISION_MODEL drifted to {DEFAULT_VISION_MODEL!r}; "
        f"expected {PRODUCT_DEFAULT!r}"
    )


def test_docs_do_not_advertise_legacy_default() -> None:
    """No public doc surface may mention the legacy model name at all.

    The legacy name only ever appeared in these files as a documented default,
    so any occurrence here is a drift. Fails loudly naming the offending file.
    """
    offenders = []
    for rel in DOC_SURFACES:
        path = REPO_ROOT / rel
        assert path.exists(), f"expected doc surface missing: {rel}"
        text = path.read_text(encoding="utf-8")
        if LEGACY_NAME in text:
            offenders.append(rel)
    assert not offenders, (
        f"legacy model name {LEGACY_NAME!r} found in public docs {offenders}; "
        f"the documented default must be {PRODUCT_DEFAULT!r} (matching config.py)"
    )


def test_env_example_model_defaults_match_code() -> None:
    """``.env.example`` LLM/Vision model lines equal the code default verbatim."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for var, expected in (
        ("SCREENSCRIBE_LLM_MODEL", DEFAULT_LLM_MODEL),
        ("SCREENSCRIBE_VISION_MODEL", DEFAULT_VISION_MODEL),
    ):
        m = re.search(rf"(?m)^{re.escape(var)}=(.+)$", text)
        assert m is not None, f"{var}= line missing in .env.example"
        assert m.group(1).strip() == expected, (
            f".env.example {var}={m.group(1).strip()!r} != code default {expected!r}"
        )


def test_usage_model_table_matches_code() -> None:
    """USAGE.md Models table declares the code default for LLM and Vision."""
    text = (REPO_ROOT / "USAGE.md").read_text(encoding="utf-8")
    for var, expected in (
        ("SCREENSCRIBE_LLM_MODEL", DEFAULT_LLM_MODEL),
        ("SCREENSCRIBE_VISION_MODEL", DEFAULT_VISION_MODEL),
    ):
        # Markdown table row: | `VAR` | `default` | ... |
        m = re.search(rf"\|\s*`{re.escape(var)}`\s*\|\s*`([^`]+)`", text)
        assert m is not None, f"USAGE.md Models row for {var} not found"
        assert m.group(1).strip() == expected, (
            f"USAGE.md {var} default `{m.group(1).strip()}` != code default `{expected}`"
        )
