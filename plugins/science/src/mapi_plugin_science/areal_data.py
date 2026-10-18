"""Raw schema, example specs, synthetic maps and the real E18 data for ``sci.areal_bym2``.

* :func:`areal_dataset` - a map of square areas (plus an optional offshore island) with
  counts simulated from the model's own forward process: a BYM2 field drawn with the
  hand-written ICAR sampler, a covariate effect and Poisson counts (truth :data:`AREAL_TRUTH`);
* :func:`from_challenges` - the Scottish lip-cancer data of pymc-challenges E18 (56 districts,
  1975-80: observed and expected cases, % of workers in agriculture, fishing and forestry, and
  the district polygons in British National Grid metres), with the queen contiguity graph
  built from the polygons as the notebook does (or the published WinBUGS neighbour list).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.areal import area_graph, contiguity, draw_icar, scaling_factor

__all__ = [
    "ADJACENCY_FILE",
    "AREAL_SCHEMA",
    "AREAL_SPEC_YAML",
    "AREAL_TRUTH",
    "CHALLENGES_FILE",
    "SCOTLAND_SPEC_YAML",
    "areal_dataset",
    "challenges_reference",
    "from_challenges",
    "square_rings",
]

CHALLENGES_FILE = "scotland_lip.geojson"
"""The district polygons and counts in the pymc-challenges ``data/`` directory."""
ADJACENCY_FILE = "scotland_lip_adjacency.csv"
"""The published (hand-edited WinBUGS) neighbour list, next to :data:`CHALLENGES_FILE`."""

AREAL_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "district": {"kind": "categorical", "policy": "fixed"},
            "district_": {"kind": "categorical", "policy": "fixed"},
            "vertex": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            "cases": {
                "dims": ["district"],
                "dtype": "float64",
                "role": "target",
                "units": "cases",
                "missing": "allow",
            },
            "expected": {
                "dims": ["district"],
                "dtype": "float64",
                "role": "exposure",
                "units": "cases",
            },
            "aff": {"dims": ["district"], "dtype": "float64", "role": "covariate", "units": "%"},
            "adjacency": {"dims": ["district", "district_"], "dtype": "int64", "role": "constant"},
            "ring_x": {
                "dims": ["district", "vertex"],
                "dtype": "float64",
                "role": "constant",
                "units": "m",
                "missing": "allow",
            },
            "ring_y": {
                "dims": ["district", "vertex"],
                "dtype": "float64",
                "role": "constant",
                "units": "m",
                "missing": "allow",
            },
        },
    }
)
"""Raw schema of :func:`areal_dataset` and :func:`from_challenges`."""

_BIND = """  bind:
    dims: {area: district, area_: district_}
    vars: {covariate: aff}
"""

AREAL_SPEC_YAML = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: synthetic-disease-map, project: epidemiology}}
data:
  dataset: dataset://epidemiology/synthetic_disease_map@latest
{_BIND}  split: {{kind: mask_block, dims: [area], fraction: 0.15, seed: 18}}
model:
  use: sci.areal_bym2@^1
  settings: {{kind: bym2, islands: link_nearest}}
  fit: {{mode: evaluate_then_refit, seed: 18, tune: 300, draws: 150, chains: 2, engine: pymc}}
evaluate: {{metrics: [rmse, mae, coverage_90, elpd_loo]}}
outputs: {{plots: [core.convergence, core.posterior_forest, sci.area_map]}}
"""
"""A small fit on :func:`areal_dataset` with a leave-areas-out holdout (15% of the areas)."""

SCOTLAND_SPEC_YAML = f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: scotland-lip-cancer-bym2, project: epidemiology}}
data:
  dataset: dataset://epidemiology/scotland_lip_cancer@latest
{_BIND}  split: {{kind: mask_block, dims: [area], fraction: 0.125, seed: 1987}}
model:
  use: sci.areal_bym2@^1
  settings: {{kind: bym2, islands: link_nearest, covariate_scale: 10.0}}
  fit: {{mode: evaluate_then_refit, seed: 1987, tune: 1000, draws: 1000, chains: 4,
         target_accept: 0.9, engine: nutpie}}
evaluate: {{metrics: [rmse, mae, coverage_90, elpd_loo]}}
outputs: {{plots: [core.convergence, core.posterior_forest, sci.area_map, net.graph]}}
"""
"""E18: BYM2 on the 56 Scottish districts (islands linked to the closest district); one
district in eight is held out once (leave-districts-out), then the model is refitted."""

AREAL_TRUTH: dict[str, Any] = {"b0": 0.1, "b1": 0.4, "sigma": 0.5, "rho": 0.8}
"""Generating parameters of :func:`areal_dataset` (BYM2 effects, ``covariate_scale`` 10)."""


def square_rings(n_row: int, n_col: int, size: float = 1000.0) -> tuple[np.ndarray, np.ndarray]:
    """Closed square outlines ``(n_row * n_col, 5)`` of a grid of cells (row-major)."""
    r, c = np.divmod(np.arange(n_row * n_col), n_col)
    x0, y0 = c * size, r * size
    dx = np.array([0.0, 1.0, 1.0, 0.0, 0.0]) * size
    dy = np.array([0.0, 0.0, 1.0, 1.0, 0.0]) * size
    return x0[:, None] + dx[None, :], y0[:, None] + dy[None, :]


def areal_dataset(
    *,
    n_row: int = 6,
    n_col: int = 7,
    island: bool = True,
    truth: dict[str, Any] | None = None,
    expected_range: tuple[float, float] = (2.0, 40.0),
    seed: int = 0,
) -> xr.Dataset:
    """A simulated disease map in the :data:`AREAL_SCHEMA` layout.

    Square districts on an ``n_row x n_col`` grid (1 km cells, queen contiguity from the
    polygons) and, with ``island``, one more district offshore with no neighbour. The
    covariate (in %) rises from south-west to north-east; the area effects are BYM2 on the
    graph with the island linked to its closest district; counts are Poisson.
    """
    p = {**AREAL_TRUTH, **(truth or {})}
    rng = np.random.default_rng(seed)
    rx, ry = square_rings(n_row, n_col)
    if island:
        size = 1000.0
        ix = n_col * size + 1500.0 + np.array([0.0, 1.0, 1.0, 0.0, 0.0]) * 600.0
        iy = (n_row - 1) * size + np.array([0.0, 0.0, 1.0, 1.0, 0.0]) * 600.0
        rx, ry = np.vstack([rx, ix]), np.vstack([ry, iy])
    n = rx.shape[0]
    adj = contiguity(rx, ry)
    g = area_graph(adj, rx, ry, "link_nearest")
    scale = scaling_factor(g.w)
    phi = draw_icar(g.w, 1, rng)[0]
    theta = rng.standard_normal(n)
    u = p["sigma"] * (np.sqrt(p["rho"] / scale) * phi + np.sqrt(1 - p["rho"]) * theta)
    cx, cy = rx[:, :4].mean(1), ry[:, :4].mean(1)
    span = max(np.ptp(cx) + np.ptp(cy), 1.0)
    aff = 2.0 + 18.0 * ((cx - cx.min()) + (cy - cy.min())) / span + rng.normal(0.0, 2.0, n)
    aff = np.clip(aff, 0.0, None).round(1)
    lo, hi = expected_range
    expected = np.exp(rng.uniform(np.log(lo), np.log(hi), n)).round(2)
    x = (aff - aff.mean()) / 10.0
    rate = expected * np.exp(p["b0"] + p["b1"] * x + u)
    cases = rng.poisson(rate).astype(float)
    names = np.array([f"D{i + 1:02d}" for i in range(n)], dtype=object)
    return _dataset(names, cases, expected, aff, adj, rx, ry)


def _dataset(
    names: np.ndarray,
    cases: np.ndarray,
    expected: np.ndarray,
    aff: np.ndarray,
    adj: np.ndarray,
    rx: np.ndarray,
    ry: np.ndarray,
) -> xr.Dataset:
    labels = np.asarray([str(v) for v in names])
    return xr.Dataset(
        {
            "cases": (("district",), np.asarray(cases, dtype=float), {"units": "cases"}),
            "expected": (("district",), np.asarray(expected, dtype=float), {"units": "cases"}),
            "aff": (("district",), np.asarray(aff, dtype=float), {"units": "%"}),
            "adjacency": (("district", "district_"), np.asarray(adj, dtype=np.int64)),
            "ring_x": (("district", "vertex"), np.asarray(rx, dtype=float), {"units": "m"}),
            "ring_y": (("district", "vertex"), np.asarray(ry, dtype=float), {"units": "m"}),
        },
        coords={
            "district": labels,
            "district_": labels,
            "vertex": np.arange(rx.shape[1], dtype=np.int64),
        },
    )


def from_challenges(
    path: str | Path, *, graph: Literal["queen", "rook", "published"] = "queen"
) -> xr.Dataset:
    """Read pymc-challenges' ``data/scotland_lip.geojson`` into :data:`AREAL_SCHEMA`.

    As in E18: the outer ring of each district (every geometry is one polygon), observed
    ``CANCER``, expected ``CEXP`` and ``AFF``. ``graph`` picks the adjacency: ``queen`` (the
    notebook's choice: shared polygon vertices, 117 edges, with Orkney - spelt "Okney" -,
    Shetland and the Western Isles alone), ``rook`` (a shared border segment) or
    ``published`` (the hand-edited WinBUGS list of :data:`ADJACENCY_FILE` next to ``path``;
    132 edges, islands linked by hand).
    """
    path = Path(path)
    geo = json.loads(path.read_text())
    feats = geo.get("features") or []
    if not feats:
        raise ValueError(f"{path}: no features")
    props = pd.DataFrame([f.get("properties") or {} for f in feats])
    missing = {"NAME", "CANCER", "CEXP", "AFF"} - set(props.columns)
    if missing:
        raise ValueError(f"{path}: missing properties {sorted(missing)}")
    rings = []
    for f in feats:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "MultiPolygon":
            coords = coords[0]
        if not coords:
            raise ValueError(f"{path}: a feature has no polygon")
        rings.append(np.asarray(coords[0], dtype=float))
    width = max(len(r) for r in rings)
    rx = np.full((len(rings), width), np.nan)
    ry = np.full((len(rings), width), np.nan)
    for i, r in enumerate(rings):
        rx[i, : len(r)], ry[i, : len(r)] = r[:, 0], r[:, 1]
    if graph == "published":
        table = pd.read_csv(path.with_name(ADJACENCY_FILE))
        if "ADJ" not in table.columns or len(table) != len(rings):
            raise ValueError(f"{path.with_name(ADJACENCY_FILE)}: no ADJ column for every district")
        if "CODENO" in table.columns and "CODENO" in props.columns:
            if not (table["CODENO"].to_numpy() == props["CODENO"].to_numpy()).all():
                raise ValueError("the neighbour list is not in the order of the polygons")
        adj = np.zeros((len(rings), len(rings)), dtype=np.int64)
        for i, nbrs in enumerate(table["ADJ"]):
            adj[i, np.asarray(json.loads(nbrs), dtype=int) - 1] = 1
    elif graph in ("queen", "rook"):
        adj = contiguity(rx, ry, rule=graph)
    else:
        raise ValueError(f"graph must be queen, rook or published, not {graph!r}")
    names = props["NAME"].astype(str).to_numpy()
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: district names are not unique")
    return _dataset(
        names,
        props["CANCER"].to_numpy(float),
        props["CEXP"].to_numpy(float),
        props["AFF"].to_numpy(float),
        adj,
        rx,
        ry,
    )


def challenges_reference() -> dict[str, dict[str, float]]:
    """Posterior means printed by E18 (nutpie, 4 x 1000 draws) for each ``kind``."""
    return {
        "poisson": {"b0": 0.095, "b1": 0.736},
        "iid": {"b0": 0.097, "b1": 0.670, "sigma": 0.623},
        "icar": {"b0": 0.099, "b1": 0.374, "sigma": 0.673},
        "car": {"b0": 0.020, "b1": 0.398, "sigma": 0.728, "alpha": 0.958},
        "bym2": {"b0": 0.101, "b1": 0.404, "sigma": 0.515, "rho": 0.819},
    }
