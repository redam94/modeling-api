"""Render the recipe gallery PNGs used by the documentation site.

Needs vl-convert (not a project dependency); writes to ``docs/assets/plots`` by default::

    uv run --with vl-convert-python python packages/plots-core/scripts/render_gallery.py

Datasets are inlined the way the browser's Arrow loader presents them (timestamps as epoch
milliseconds), so the images match what the plot sandbox draws.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import vl_convert as vlc  # type: ignore[import-not-found]
from mapi_plots.convert import arrow_to_frame
from mapi_plots.examples import example_inputs
from mapi_plots.recipes import get_recipe
from mapi_spec.plots import PlotSpec

GALLERY = [
    "core.ppc_fan",
    "core.convergence",
    "core.ppc",
    "core.posterior_forest",
    "core.decomposition",
    "core.counterfactual",
    "mmm.response_curves",
    "mmm.budget_plans",
]


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    f = frame.copy()
    for col in f.columns:
        if isinstance(f[col].dtype, pd.CategoricalDtype):
            f[col] = f[col].astype(str)
        elif pd.api.types.is_datetime64_any_dtype(f[col].dtype):
            f[col] = f[col].astype("datetime64[ms]").astype("int64")
    out: list[dict[str, Any]] = json.loads(f.to_json(orient="records"))
    return out


def inline_datasets(plot: PlotSpec) -> dict[str, Any]:
    """The plot's Vega-Lite spec with its Arrow datasets inlined (for offline renderers)."""
    spec = dict(plot.spec)
    spec["datasets"] = {k: _records(arrow_to_frame(v)) for k, v in plot.datasets.items()}
    return spec


def main(out_dir: str) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cases = example_inputs()
    for rid in GALLERY:
        data, options = cases[rid]
        plot = get_recipe(rid).render(data, **options)
        png = vlc.vegalite_to_png(inline_datasets(plot), vl_version="v6_4", scale=1.0)
        path = out / (rid.replace(".", "_") + ".png")
        path.write_bytes(png)
        print(f"{path} ({len(png)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "docs/assets/plots"))
