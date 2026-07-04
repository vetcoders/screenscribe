"""Keyword configuration loading and management.

Keywords are user-editable *hints* for AI detection. They are
always-on additional context, safe when empty, and never replace the LLM.
This module owns loading/storage of the keyword dictionary; it does not
perform detection.

Load priority:
1. Explicit ``--keywords-file`` path (when provided).
2. Global user file ``~/.config/screenscribe/keywords.yaml``.
3. Built-in default (``default_keywords.yaml`` shipped with the package).

There is intentionally NO current-working-directory auto-search: analysis
must not depend on which directory the terminal sits in. Screenscribe
analyzes a video, not "the project in cwd".
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rich.console import Console

console = Console()

# Path to embedded built-in default keywords (used only when the user has no
# own dictionary).
DEFAULT_KEYWORDS_PATH = Path(__file__).parent / "default_keywords.yaml"

# Global user keywords file. This is the single editable location; there is no
# cwd auto-search.
GLOBAL_KEYWORDS_PATH = Path("~/.config/screenscribe/keywords.yaml").expanduser()

# Supported keyword categories. These match the semantic detection categories.
CATEGORIES: tuple[str, ...] = (
    "bug",
    "change",
    "ui",
    "performance",
    "accessibility",
    "other",
)


@dataclass
class KeywordsConfig:
    """Keywords configuration: AI hints grouped by category."""

    bug: list[str] = field(default_factory=list)
    change: list[str] = field(default_factory=list)
    ui: list[str] = field(default_factory=list)
    performance: list[str] = field(default_factory=list)
    accessibility: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, keywords_file: Path | None = None) -> "KeywordsConfig":
        """Load keywords configuration following the locked load priority.

        Priority:
        1. Explicit ``keywords_file`` parameter (when it exists).
        2. Global user file (``~/.config/screenscribe/keywords.yaml``).
        3. Built-in default file shipped with the package.

        There is no current-working-directory search.

        Args:
            keywords_file: Optional explicit path to a keywords file.

        Returns:
            A :class:`KeywordsConfig`. Never raises; on any problem it warns
            and falls back to the built-in default.
        """
        # 1. Explicit file.
        if keywords_file is not None:
            if keywords_file.exists():
                return cls._load_from_file(keywords_file)
            console.print(f"[yellow]Keywords file not found: {keywords_file}[/]")
            console.print("[dim]Falling back to defaults[/]")

        # 2. Global user file.
        if GLOBAL_KEYWORDS_PATH.exists():
            return cls._load_from_file(GLOBAL_KEYWORDS_PATH)

        # 3. Built-in default.
        return cls._load_from_file(DEFAULT_KEYWORDS_PATH)

    @classmethod
    def _load_from_file(cls, path: Path) -> "KeywordsConfig":
        """Load keywords from a YAML file, safely.

        Missing / empty / malformed input never raises: it warns and falls
        back to the built-in default (or an empty config when the default
        itself is what failed).
        """
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            # Empty file (``yaml.safe_load`` returns ``None``) is a no-op: an
            # empty dictionary is a valid, safe configuration.
            if data is None:
                return cls()

            if not isinstance(data, dict):
                console.print(f"[yellow]Invalid keywords file format: {path}[/]")
                return cls._load_defaults(path)

            return cls(**{category: _as_phrase_list(data.get(category)) for category in CATEGORIES})

        except yaml.YAMLError as e:
            console.print(f"[yellow]Error parsing keywords file: {e}[/]")
            return cls._load_defaults(path)
        except OSError as e:
            console.print(f"[yellow]Error reading keywords file: {e}[/]")
            return cls._load_defaults(path)

    @classmethod
    def _load_defaults(cls, failed_path: Path | None = None) -> "KeywordsConfig":
        """Fall back to the built-in default keywords.

        If the failure happened while loading the built-in default itself,
        return an empty (safe) config instead of recursing.
        """
        if failed_path is not None and failed_path == DEFAULT_KEYWORDS_PATH:
            return cls()
        return cls._load_from_file(DEFAULT_KEYWORDS_PATH)

    def get_keywords(self, category: str) -> list[str]:
        """Get keywords for a specific category, or ``[]`` for an unknown one."""
        if category in CATEGORIES:
            return getattr(self, category)  # type: ignore[no-any-return]
        return []

    @property
    def total_keywords(self) -> int:
        """Total number of keywords across all categories."""
        return sum(len(self.get_keywords(category)) for category in CATEGORIES)

    def summary(self) -> str:
        """Return a human-readable summary of loaded keywords."""
        parts = [f"{len(self.get_keywords(category))} {category}" for category in CATEGORIES]
        return "Keywords: " + ", ".join(parts)


def _as_phrase_list(value: object) -> list[str]:
    """Coerce a parsed YAML category value into a clean list of phrases.

    A missing or ``None`` category becomes an empty list. A malformed
    (non-list) value is treated as empty rather than raising, keeping the
    loader safe.
    """
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and str(item).strip()]


def format_keywords_hint(config: KeywordsConfig) -> str:
    """Format the active keywords as prompt-hint text for the LLM.

    The output is a vocabulary-hints block describing the phrases a user/team
    uses to signal problem types. It is a hint, not a rule: the prompt frames
    it so the model still judges context, negation, and intent, and does not
    auto-create a finding just because a phrase appears.

    Returns an empty string when there are no keywords, so callers can inject
    it unconditionally and it is a no-op when empty.
    """
    lines = []
    for category in CATEGORIES:
        phrases = config.get_keywords(category)
        if phrases:
            joined = ", ".join(f'"{phrase}"' for phrase in phrases)
            lines.append(f"- {category}: {joined}")

    if not lines:
        return ""

    header = (
        "This user uses the following phrases as signals for problem types. "
        "Treat them as hints, not rules: still judge context, negation, and "
        "intent. Do NOT auto-create a finding just because a phrase appears, "
        "and if none appear, analyze normally."
    )
    return header + "\n" + "\n".join(lines)


def save_default_keywords(path: Path = GLOBAL_KEYWORDS_PATH) -> None:
    """Write the built-in default keywords to a file for user customization.

    Defaults to the global keywords path. Creates parent directories as
    needed.

    Args:
        path: Destination path. Defaults to the global keywords file.
    """
    import shutil

    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(DEFAULT_KEYWORDS_PATH, path)
    console.print(f"[green]Default keywords saved to: {path}[/]")
