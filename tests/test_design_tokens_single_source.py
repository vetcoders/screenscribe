"""Regression guard for screenscribe design-token ownership."""

from __future__ import annotations

import re
from pathlib import Path

STYLES_DIR = Path("screenscribe/html_pro_assets/styles")
THEME = STYLES_DIR / "screenscribe-theme.css"
SURFACE_STYLESHEETS = (
    STYLES_DIR / "report-pro.css",
    STYLES_DIR / "analyze_dashboard.css",
)

_CUSTOM_PROPERTY = re.compile(r"(?m)^\s*(--[-_a-zA-Z0-9]+)\s*:\s*([^;]+);")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_PX = re.compile(r"(?<![\w.-])-?\d+(?:\.\d+)?px\b")


def _css(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(css: str) -> str:
    return _COMMENT.sub("", css)


def _theme_tokens() -> dict[str, str]:
    return {
        name: value.strip()
        for name, value in _CUSTOM_PROPERTY.findall(_strip_comments(_css(THEME)))
    }


def _literal_token_values(
    tokens: dict[str, str], *, name_prefixes: tuple[str, ...]
) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for name, raw_value in tokens.items():
        if not name.startswith(name_prefixes):
            continue
        value = raw_value.strip()
        if _HEX.fullmatch(value) or _PX.fullmatch(value):
            values.setdefault(value.lower(), []).append(name)
    return values


def test_design_tokens_are_declared_only_in_theme() -> None:
    """Theme owns shared tokens; surface styles must only consume them."""
    theme_names = set(_theme_tokens())
    offenders: list[str] = []

    for stylesheet in SURFACE_STYLESHEETS:
        for name, _value in _CUSTOM_PROPERTY.findall(_strip_comments(_css(stylesheet))):
            if name in theme_names:
                offenders.append(f"{stylesheet.name}: redeclares {name}")

    assert not offenders, "shared token redeclarations outside theme:\n" + "\n".join(offenders)


def test_surface_styles_do_not_inline_theme_token_literals() -> None:
    """If a literal value is a theme token, surfaces must reference var(--token)."""
    color_token_values = _literal_token_values(
        _theme_tokens(),
        name_prefixes=(
            "--ss-accent",
            "--surface-",
            "--text-",
            "--border-",
            "--color-",
        ),
    )
    radius_token_values = _literal_token_values(_theme_tokens(), name_prefixes=("--radius-",))
    offenders: list[str] = []

    for stylesheet in SURFACE_STYLESHEETS:
        css = _strip_comments(_css(stylesheet))
        for line_number, line in enumerate(css.splitlines(), start=1):
            if "--" in line:
                continue
            for literal in _HEX.findall(line):
                token_names = color_token_values.get(literal.lower())
                if token_names:
                    offenders.append(
                        f"{stylesheet.name}:{line_number}: {literal} duplicates "
                        f"{', '.join(sorted(token_names))}"
                    )
            if "border-radius" not in line:
                continue
            for literal in _PX.findall(line):
                token_names = radius_token_values.get(literal.lower())
                if token_names:
                    offenders.append(
                        f"{stylesheet.name}:{line_number}: {literal} duplicates "
                        f"{', '.join(sorted(token_names))}"
                    )

    assert not offenders, "theme token literals in surface styles:\n" + "\n".join(offenders)
