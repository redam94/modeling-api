"""E19 data: ``level(cell, protein)`` drawn from a sparse Gaussian graphical model, with the
true network as ``consensus(protein, protein_)``.

``tiny`` is the six-variable truth of :data:`~mapi_plugin_science.ggm_data.GGM_TRUTH` with
300 cells; ``full`` has the size of the notebook's data (853 cells) and eleven variables in
a ring with two chords.
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_plugin_science.ggm_data import GGM_SCHEMA, ggm_dataset
from mapi_spec.schema import DataSchema

_FULL_NAMES = tuple(f"p{i:02d}" for i in range(11))
FULL_TRUTH = {
    "variables": _FULL_NAMES,
    "edges": {
        **{(_FULL_NAMES[i], _FULL_NAMES[(i + 1) % 11]): 0.3 for i in range(11)},
        ("p00", "p05"): -0.25,
        ("p02", "p08"): 0.2,
    },
    "mean": tuple(3.0 + 0.1 * i for i in range(11)),
    "sd": tuple(0.5 + 0.1 * i for i in range(11)),
}
"""Generating truth at size ``full``: a ring of eleven variables plus two chords."""


def generate(seed: int = 0, size: str = "tiny") -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    if size == "tiny":
        return ggm_dataset(cells=300, seed=seed), {}
    return ggm_dataset(cells=853, truth=FULL_TRUTH, seed=seed), {}


def schema() -> DataSchema:
    return GGM_SCHEMA


def payload_new_cells(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Five cells the fit has not seen (new levels of ``cell``, pooled)."""
    start = int(data["cell"].values.max()) + 1
    return xr.Dataset(
        coords={"cell": np.arange(start, start + 5), "protein": data["protein"].values}
    )


def payload_extra_protein(data: xr.Dataset, seed: int, size: str) -> xr.Dataset:
    """Cells with a variable the fit has not seen (``protein`` is fixed)."""
    names = [*map(str, data["protein"].values), "unknown"]
    return xr.Dataset(coords={"cell": [0, 1], "protein": names})
