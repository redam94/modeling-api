"""The colours platform recipes draw with, each with a dark-mode variant.

Vega-Lite applies an explicit mark colour over any theme config, so recipes never pick ad
hoc colours: they use these swatches, which are the light-theme values. The plot sandbox
(``apps/plot-sandbox/src/recipe-palette.ts``, kept in sync by a test) swaps every light value
for its dark variant when it renders in dark mode. Static renders keep the light values.

Swatches marked ``legible`` are strokes, dots or text: they keep at least 3:1 contrast (WCAG
non-text) against the light card (``#ffffff``) and the dark card (``#1b212d``). The others
are fills (bands at low opacity, empty cells, separators).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ALERT",
    "BAND",
    "BASELINE",
    "EMPTY",
    "ESTIMATE",
    "MUTED",
    "NEGATIVE",
    "OBSERVED",
    "OK",
    "PALETTE",
    "POSITIVE",
    "REFERENCE",
    "SCENARIO",
    "STRONG",
    "SURFACE",
    "Swatch",
    "dark_variants",
]


@dataclass(frozen=True, slots=True)
class Swatch:
    light: str
    dark: str
    legible: bool = True


PALETTE: Final[dict[str, Swatch]] = {
    "band": Swatch("#3366cc", "#6fa8f0"),  # uncertainty bands (areas) and their lines
    "estimate": Swatch("#1f4e99", "#8ec5ff"),  # the point estimate over the bands
    "observed": Swatch("#1b2333", "#e3e7ee"),  # actual values
    "alert": Swatch("#c62828", "#ff7b72"),  # split line, thresholds, failed checks
    "ok": Swatch("#2e7d32", "#5cc46a"),  # passed checks
    "reference": Swatch("#767e8c", "#8b95a5"),  # zero lines, ideal lines, reference rules
    "muted": Swatch("#5b6577", "#9aa3b2"),  # annotations
    "strong": Swatch("#333b48", "#c8cfdb"),  # interval rules and means drawn over bars
    "baseline": Swatch("#6b7685", "#97a6b6"),  # the baseline of a counterfactual
    "scenario": Swatch("#c45a00", "#f0a35e"),  # the scenario of a counterfactual
    "positive": Swatch("#1f6fb4", "#6fb0f2"),  # signed effects above zero
    "empty": Swatch("#e3e6ec", "#2a3140", legible=False),  # cells with no data
    "surface": Swatch("#ffffff", "#1b212d", legible=False),  # separators (the card colour)
}

BAND: Final = PALETTE["band"].light
ESTIMATE: Final = PALETTE["estimate"].light
OBSERVED: Final = PALETTE["observed"].light
ALERT: Final = PALETTE["alert"].light
NEGATIVE: Final = ALERT
OK: Final = PALETTE["ok"].light
REFERENCE: Final = PALETTE["reference"].light
MUTED: Final = PALETTE["muted"].light
STRONG: Final = PALETTE["strong"].light
BASELINE: Final = PALETTE["baseline"].light
SCENARIO: Final = PALETTE["scenario"].light
POSITIVE: Final = PALETTE["positive"].light
EMPTY: Final = PALETTE["empty"].light
SURFACE: Final = PALETTE["surface"].light


def dark_variants() -> dict[str, str]:
    """``{light: dark}`` for every swatch (what the plot sandbox applies in dark mode)."""
    return {s.light: s.dark for s in PALETTE.values()}
