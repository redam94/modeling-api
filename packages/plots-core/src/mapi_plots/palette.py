"""The colours platform recipes draw with, each with a dark-mode variant.

Vega-Lite applies an explicit mark colour over any theme config, so recipes never pick ad
hoc colours: they use these swatches, which are the light-theme values. The plot sandbox
(``apps/plot-sandbox/src/recipe-palette.ts``, kept in sync by a test) swaps every light value
for its dark variant when it renders in dark mode. Static renders keep the light values.

Swatches marked ``legible`` are strokes, dots or text: they keep at least 3:1 contrast (WCAG
non-text) against the light card (``#ffffff``) and the dark card (``#1b212d``). The others
are fills (bands at :func:`band_opacity`, empty cells, separators), never a data mark: a bar,
histogram, density or marker that carries data uses ``ESTIMATE`` or a series colour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ALERT",
    "BAND",
    "BAND_OPACITY",
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
    "band_opacity",
    "dark_variants",
]


@dataclass(frozen=True, slots=True)
class Swatch:
    light: str
    dark: str
    legible: bool = True


PALETTE: Final[dict[str, Swatch]] = {
    # uncertainty bands (areas): the light value is the pale ``--chart-band`` fill, so it is no
    # stroke colour on the light card; its dark variant keeps 3:1 on the dark card.
    "band": Swatch("#c7dbff", "#5c85ff", legible=False),
    "estimate": Swatch("#0014cc", "#85a3ff"),  # the point estimate over the bands
    "observed": Swatch("#121619", "#f4f5f7"),  # actual values
    "alert": Swatch("#c20021", "#ff8597"),  # split line, thresholds, failed checks
    "ok": Swatch("#036e1b", "#4fd26b"),  # passed checks
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


BAND_OPACITY: Final = 0.75
"""The opacity of a single uncertainty band (MEI draws ``--chart-band`` at .75). ``BAND`` is
a pale fill: at the old .15-.2 it blended to about 1.07:1 on the white card (invisible)."""


def band_opacity(i: int = 0, n: int = 1) -> float:
    """The opacity of band ``i`` of ``n`` nested bands, ``0`` the widest: a single band gets
    :data:`BAND_OPACITY`; nested ones climb a .45-.75 ladder (where they overlap the inner
    band is darker still), so every band stays visible in light mode and static exports."""
    if n <= 1:
        return BAND_OPACITY
    return round(0.45 + 0.3 * min(i, n - 1) / (n - 1), 3)


def dark_variants() -> dict[str, str]:
    """``{light: dark}`` for every swatch (what the plot sandbox applies in dark mode)."""
    return {s.light: s.dark for s in PALETTE.values()}
