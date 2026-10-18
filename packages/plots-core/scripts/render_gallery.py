"""Render the recipe gallery PNGs used by the documentation site: every bundled recipe on
its example data (:mod:`mapi_plots.examples`), the same render the catalog shows.

Needs vl-convert (Vega-Lite) and kaleido (Plotly; it drives a local Chrome), neither a
project dependency; writes to ``docs/assets/plots`` by default::

    uv run --with vl-convert-python --with kaleido \\
        python packages/plots-core/scripts/render_gallery.py

Datasets are inlined the way the browser's Arrow loader presents them (timestamps as epoch
milliseconds), so the images match what the plot sandbox draws.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.io as pio
import vl_convert as vlc  # type: ignore[import-not-found]
from mapi_plots.convert import arrow_to_frame, decode_typed_array
from mapi_plots.examples import example_inputs
from mapi_plots.recipes import get_recipe
from mapi_spec.plots import PlotSpec


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


def inline_plotly(plot: PlotSpec) -> dict[str, Any]:
    """The plot's Plotly figure with every ``{"$ref": "dataset.column"}`` (and typed array)
    replaced by its values, as the sandbox resolves them."""
    frames = {k: arrow_to_frame(v) for k, v in plot.datasets.items()}

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if set(node) == {"$ref"}:
                name, col = str(node["$ref"]).split(".", 1)
                return json.loads(frames[name][col].to_json(orient="values", date_format="iso"))
            if "bdata" in node:
                return decode_typed_array(node).tolist()
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(dict(plot.spec))  # type: ignore[no-any-return]


def main(out_dir: str) -> int:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for rid, (data, options) in sorted(example_inputs().items()):
        plot = get_recipe(rid).render(data, **options)
        if plot.kind == "plotly":
            fig = inline_plotly(plot)
            fig.setdefault("layout", {}).setdefault("template", "plotly_white")
            png = pio.to_image(fig, format="png", width=900, height=460, scale=1.0)
        else:
            png = vlc.vegalite_to_png(inline_datasets(plot), vl_version="v6_4", scale=1.0)
        path = out / (rid.replace(".", "_") + ".png")
        path.write_bytes(png)
        print(f"{path} ({len(png)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "docs/assets/plots"))
