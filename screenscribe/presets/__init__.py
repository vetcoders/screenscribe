"""Analysis presets: keyword dictionary + prompt fragment + finding categories.

A preset bundles three things for one domain:

1. **keywords** — the vocabulary hints injected into detection prompts,
2. **prompt** — a system-prompt fragment describing who the viewer is and what
   counts as a finding (appended next to ``--prompt`` as analysis
   instructions),
3. **categories** — the finding categories used by detection and carried into
   the JSON report (the HTML report renders unknown categories via its
   upper-cased fallback, so preset categories work without UI changes).

The default ``programming`` preset reproduces the historical behavior
bit-for-bit: it carries no inline keywords (the built-in
``default_keywords.yaml`` stays the single source) and its categories are the
hard-coded six from ``keywords.CATEGORIES``.

Keyword load priority with presets (locked): explicit ``--keywords-file`` >
global user file > preset dictionary > built-in default.

``custom`` has no YAML definition: it is built from the user's own
``--keywords-file`` (its top-level keys become the categories), which is why
``--preset custom`` requires that flag.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Names with a shipped YAML definition. ``custom`` is resolved separately from
# the user's --keywords-file.
PRESET_NAMES: tuple[str, ...] = ("programming", "casual", "medical", "veterinary")
PRESET_CHOICES: tuple[str, ...] = (*PRESET_NAMES, "custom")

PRESETS_DIR = Path(__file__).parent


class PresetError(ValueError):
    """A preset name is unknown or its definition/keywords file is unusable."""


@dataclass(frozen=True)
class Preset:
    """One analysis preset.

    ``keywords`` maps category -> phrases. ``None`` means "no dictionary of
    its own" — the built-in default keywords file is used instead (this is
    what keeps ``programming`` identical to the pre-preset behavior).
    """

    name: str
    categories: tuple[str, ...]
    prompt: str = ""
    keywords: dict[str, list[str]] | None = field(default=None)


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and str(item).strip()]


def _preset_path(name: str) -> Path:
    return PRESETS_DIR / f"{name}.yaml"


def load_preset(name: str) -> Preset:
    """Load a shipped preset by name.

    Raises :class:`PresetError` with a readable message for unknown names or
    malformed definitions.
    """
    normalized = (name or "").strip().lower()
    if normalized == "custom":
        raise PresetError(
            "The 'custom' preset has no built-in definition; it is built from "
            "--keywords-file. Use: screenscribe review <video> --preset custom "
            "--keywords-file my-keywords.yaml"
        )
    if normalized not in PRESET_NAMES:
        raise PresetError(f"Unknown preset: {name!r}. Available: {', '.join(PRESET_CHOICES)}.")

    path = _preset_path(normalized)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise PresetError(f"Preset definition unreadable: {path} ({e})") from e

    if not isinstance(data, dict):
        raise PresetError(f"Preset definition is not a mapping: {path}")

    categories = tuple(_as_str_list(data.get("categories")))
    if not categories:
        raise PresetError(f"Preset {normalized!r} defines no categories: {path}")

    raw_keywords = data.get("keywords")
    keywords: dict[str, list[str]] | None = None
    if isinstance(raw_keywords, dict):
        keywords = {str(key): _as_str_list(value) for key, value in raw_keywords.items()}

    return Preset(
        name=normalized,
        categories=categories,
        prompt=str(data.get("prompt") or "").strip(),
        keywords=keywords,
    )


def load_custom_preset(keywords_file: Path) -> Preset:
    """Build the ``custom`` preset from the user's own keywords file.

    The file's top-level keys become the finding categories (in file order).
    An empty mapping falls back to the default categories so an empty custom
    dictionary stays a safe no-op.
    """
    from ..keywords import CATEGORIES

    try:
        with open(keywords_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise PresetError(
            f"Cannot read the custom keywords file: {keywords_file} ({e}). "
            "--preset custom needs a valid YAML mapping of category -> phrases."
        ) from e

    if data is not None and not isinstance(data, dict):
        raise PresetError(
            f"Custom keywords file is not a YAML mapping: {keywords_file}. "
            "Expected category -> list of phrases, e.g. 'bug: [\"nie działa\"]'."
        )

    keys = [str(key) for key in data] if isinstance(data, dict) else []
    categories = tuple(keys) if keys else CATEGORIES
    return Preset(name="custom", categories=categories, prompt="", keywords=None)
