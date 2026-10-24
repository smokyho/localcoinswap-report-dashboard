"""Enforces the WCAG 2.1 AA claims made in report.css.

The stylesheet documents that every text pair meets 4.5:1 (or 3:1 for large
text). These tests parse the actual token values so a palette change that
breaks contrast fails the suite rather than silently shipping.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "lcs_report" / "static" / "report.css"


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    l1 = _relative_luminance(_hex_to_rgb(fg))
    l2 = _relative_luminance(_hex_to_rgb(bg))
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def _block(name: str) -> str:
    """Extract one top-level block from the stylesheet.

    ``name`` is a selector or media-query prefix, e.g. ``:root`` or
    ``prefers-color-scheme: light``. The first matching ``{ ... }`` is taken.
    """
    text = CSS.read_text()
    match = re.compile(re.escape(name) + r"\)?\s*\{(.*?)\n\}", re.DOTALL).search(text)
    assert match, f"no block matching {name!r} found in {CSS}"
    return match.group(1)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,6})\s*;", block))


ROOT = _tokens(_block(":root"))
LIGHT = _tokens(_block("prefers-color-scheme: light"))


def test_token_blocks_were_found() -> None:
    assert "--bg-surface" in ROOT, "dark tokens missing"
    assert "--bg-surface" in LIGHT, "light tokens missing"


# (foreground, background, minimum ratio, what it is used for)
DARK_PAIRS = [
    ("--text-primary", "--bg-surface", 4.5, "body text on panels"),
    ("--text-primary", "--bg-base", 4.5, "body text on page"),
    ("--text-primary", "--bg-raised", 4.5, "body text on raised surfaces"),
    ("--text-secondary", "--bg-surface", 4.5, "secondary text"),
    ("--text-secondary", "--bg-base", 4.5, "secondary text on page"),
    ("--text-tertiary", "--bg-surface", 4.5, "tertiary text"),
    ("--text-tertiary", "--bg-inset", 4.5, "table headers"),
    ("--text-primary", "--bg-inset", 4.5, "form control text"),
    ("--positive", "--bg-surface", 4.5, "positive figures"),
    ("--positive", "--positive-muted", 4.5, "positive chip text"),
    ("--negative", "--bg-surface", 4.5, "negative figures"),
    ("--negative", "--negative-muted", 4.5, "negative chip text"),
    ("--warning", "--bg-surface", 4.5, "warning text"),
    ("--warning", "--warning-muted", 4.5, "warning notice text"),
    ("--accent", "--bg-surface", 4.5, "accent text"),
    ("--text-tertiary", "--bg-raised", 4.5, "chart axis labels"),
    ("--text-secondary", "--bg-hover", 4.5, "hovered row secondary text"),
    ("--text-inverse", "--accent", 4.5, "primary button label"),
    ("--accent-bright", "--accent-muted", 4.5, "current year tab"),
]

LIGHT_PAIRS = [
    ("--text-primary", "--bg-surface", 4.5, "body text on panels"),
    ("--text-primary", "--bg-base", 4.5, "body text on page"),
    ("--text-secondary", "--bg-surface", 4.5, "secondary text"),
    ("--text-tertiary", "--bg-surface", 4.5, "tertiary text"),
    ("--text-tertiary", "--bg-inset", 4.5, "table headers"),
    ("--positive", "--bg-surface", 4.5, "positive figures"),
    ("--negative", "--bg-surface", 4.5, "negative figures"),
    ("--warning", "--bg-surface", 4.5, "warning text"),
    ("--accent", "--bg-surface", 4.5, "accent text"),
    ("--text-inverse", "--accent", 4.5, "primary button label"),
    ("--accent-bright", "--accent-muted", 4.5, "current year tab"),
]


@pytest.mark.parametrize("fg,bg,minimum,label", DARK_PAIRS)
def test_dark_palette_meets_aa(fg: str, bg: str, minimum: float, label: str) -> None:
    assert fg in ROOT and bg in ROOT, f"{fg}/{bg} not defined in dark theme"
    ratio = contrast(ROOT[fg], ROOT[bg])
    assert ratio >= minimum, (
        f"{label}: {fg} on {bg} is {ratio:.2f}:1, needs {minimum}:1 "
        f"({ROOT[fg]} on {ROOT[bg]})"
    )


@pytest.mark.parametrize("fg,bg,minimum,label", LIGHT_PAIRS)
def test_light_palette_meets_aa(fg: str, bg: str, minimum: float, label: str) -> None:
    assert fg in LIGHT and bg in LIGHT, f"{fg}/{bg} not defined in light theme"
    ratio = contrast(LIGHT[fg], LIGHT[bg])
    assert ratio >= minimum, (
        f"{label}: {fg} on {bg} is {ratio:.2f}:1, needs {minimum}:1 "
        f"({LIGHT[fg]} on {LIGHT[bg]})"
    )


def test_borders_are_perceptible() -> None:
    """Borders must clear 3:1 against their surface to be non-decorative."""
    for theme, tokens, surface in (
        ("dark", ROOT, "--bg-surface"),
        ("light", LIGHT, "--bg-surface"),
    ):
        for border in ("--border-default", "--border-strong"):
            ratio = contrast(tokens[border], tokens[surface])
            assert ratio >= 1.3, (
                f"{theme} {border} on {surface} is only {ratio:.2f}:1"
            )


def test_focus_ring_is_visible() -> None:
    """The focus outline must stand out against both page and panels."""
    for theme, tokens in (("dark", ROOT), ("light", LIGHT)):
        ratio = contrast(tokens["--accent"], tokens["--bg-base"])
        assert ratio >= 3.0, f"{theme} focus ring is {ratio:.2f}:1 on the page"

def test_table_cells_establish_a_containing_block() -> None:
    """Regression: a visually-hidden label must not widen the document.

    `.sr-only` is absolutely positioned. With no positioned ancestor it resolves
    against the document origin, so a label inside a horizontally scrolling
    table sits far off to the right and drags the page's scroll width out to the
    table's full width -- the whole page then scrolls sideways on narrow
    viewports. Measured at 375px: document scrollWidth 1373 against a 375px
    viewport. Giving the cell `position: relative` contains it.
    """
    css = CSS.read_text()
    import re

    td_rule = re.search(r"^td \{([^}]*)\}", css, re.MULTILINE)
    assert td_rule, "no td rule found"
    assert "position: relative" in td_rule.group(1), (
        "table cells must be positioned so .sr-only labels are contained"
    )
