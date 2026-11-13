"""The colours platform recipes draw with, each with a dark-mode variant.

Vega-Lite applies an explicit mark colour over any theme config, so recipes never pick ad
hoc colours: they use these swatches, which are the light-theme values. The plot sandbox
(``apps/plot-sandbox/src/recipe-palette.ts``, kept in sync by a test) swaps every light value
for its dark variant when it renders in dark mode. Static renders keep the light values.

Swatches marked ``legible`` are strokes, dots or text: they keep at least 3:1 contrast (WCAG
non-text) against the light card (``#ffffff``) and the dark card (``#161617``). The others
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
    # Mono (v0.21): each swatch is the app token named on its line, light and dark, so a
    # recipe drawn in the sandbox reads like the tokens around it.
    # uncertainty bands (areas): the light value is the pale ``--chart-band`` fill, so it is no
    # stroke colour on the light card; its dark variant (blue-500) keeps 3:1 on the dark card.
    "band": Swatch("#bfdcec", "#0072b2", legible=False),
    "estimate": Swatch("#0072b2", "#56b4e9"),  # the point estimate over the bands (--series-1)
    "observed": Swatch("#1d1d1f", "#f5f5f7"),  # actual values (--foreground)
    "alert": Swatch("#c00f2c", "#ff6b6b"),  # split line, thresholds, failed checks (--destructive)
    "ok": Swatch("#00704e", "#34d399"),  # passed checks (--success)
    "reference": Swatch("#86868b", "#86868b"),  # zero, ideal and reference rules (--chart-plain)
    "muted": Swatch("#68686d", "#a1a1a6"),  # annotations (--chart-ref)
    "strong": Swatch("#424245", "#d2d2d7"),  # interval rules and means over bars (grey-700/300)
    "baseline": Swatch("#6e6e73", "#8e8e93"),  # the baseline of a counterfactual (--series-8)
    "scenario": Swatch("#8f5600", "#ffc929"),  # the scenario of a counterfactual (--series-7)
    "positive": Swatch("#1d93a5", "#5fe8f2"),  # signed effects above zero (--series-6)
    "empty": Swatch("#e8e8ed", "#2a2a2c", legible=False),  # cells with no data (--chart-grid)
    "surface": Swatch("#ffffff", "#161617", legible=False),  # separators (--card)
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
