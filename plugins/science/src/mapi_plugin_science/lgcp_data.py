"""Data for ``sci.lgcp_grid`` (E07): raw schema, example spec, synthetic grids, real catalogue.

* :data:`LGCP_SCHEMA` - ``quakes(quarter, row, col)`` event counts on a regular space-time
  grid (``quarter`` is a quarterly time dim that extends; ``row``/``col`` are fixed integer
  cell indices, rows counted from the south);
* :data:`LGCP_SPEC_YAML` - a tiny spec (PyMC engine, few draws) for tests and the docs;
* :func:`lgcp_dataset` - counts simulated from the model's own forward process (an exact
  Matérn-5/2 field per era that follows a random walk, a random-walk log total, negative
  binomial counts) with known truth :data:`LGCP_TRUTH`;
* :func:`from_challenges` - bins the pymc-challenges Oklahoma catalogue
  (``data/oklahoma_quakes.csv``) like the E07 notebook: a 300 km x 225 km window of 25 km
  cells (12 x 9) and quarters 2009Q1-2019Q4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

__all__ = [
    "CHALLENGES_CSV",
    "LGCP_SCHEMA",
    "LGCP_SPEC_YAML",
    "LGCP_TRUTH",
    "OKLAHOMA_SPEC_YAML",
    "from_challenges",
    "lgcp_dataset",
    "matern52_cov",
]

CHALLENGES_CSV = "oklahoma_quakes.csv"
"""The file name of the real catalogue in the pymc-challenges ``data/`` directory."""

LGCP_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "quarter": {"kind": "time", "policy": "extend", "freq": "QS"},
            "row": {"kind": "integer", "policy": "fixed"},
            "col": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            "quakes": {
                "dims": ["quarter", "row", "col"],
                "dtype": "int64",
                "role": "target",
                "units": "events",
            }
        },
    }
)

LGCP_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: induced-quakes-lgcp, project: geoscience}
data:
  dataset: dataset://geoscience/quake_grid@latest
  bind:
    dims: {period: quarter}
    vars: {count: quakes}
  split: {kind: time_holdout, dim: period, last: 4}
model:
  use: sci.lgcp_grid@^1
  settings: {max_extension: 8, era_periods: 4, m_row: 5, m_col: 5, cell_km: 25.0}
  fit: {mode: evaluate_then_refit, seed: 7, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs: {plots: [core.convergence, core.posterior_forest, core.ppc_fan]}
"""

OKLAHOMA_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: oklahoma-quakes-lgcp, project: geoscience}
data:
  dataset: dataset://geoscience/oklahoma_quakes_grid@latest
  bind:
    dims: {period: quarter}
    vars: {count: quakes}
  split: {kind: time_holdout, dim: period, last: 8}
model:
  use: sci.lgcp_grid@^1
  settings: {max_extension: 8, era_periods: 4, m_row: 10, m_col: 14, cell_km: 25.0}
  fit: {mode: evaluate_then_refit, seed: 2016, tune: 1000, draws: 1000, chains: 4,
        engine: nutpie}
evaluate: {metrics: [rmse, mae, coverage_90]}
outputs: {plots: [core.convergence, core.posterior_forest, core.ppc_fan]}
"""
"""The E07 set-up: 2009-2017 train, 2018-19 held out, the notebook's basis size."""

LGCP_TRUTH: dict[str, Any] = {
    "total": 40.0,  # expected events per period in the whole grid (exp(alpha))
    "sigma_g": 0.15,  # random-walk step of the log total per period
    "ell": 45.0,  # km
    "eta0": 1.6,  # amplitude of the anchor era's log field
    "eta_d": 0.5,  # amplitude of each era's field increment
    "inv_phi": 0.3,  # negative-binomial overdispersion (1/phi)
    "cell_km": 25.0,
    "era_periods": 4,
}
"""Generating values of :func:`lgcp_dataset` (the anchor era is the first one)."""


def matern52_cov(xy: np.ndarray, ell: float, eta: float) -> np.ndarray:
    """Exact Matérn-5/2 covariance between the rows of ``xy`` (km)."""
    d = np.sqrt(((xy[:, None, :] - xy[None, :, :]) ** 2).sum(-1))
    a = np.sqrt(5.0) * d / ell
    return eta**2 * (1.0 + a + a**2 / 3.0) * np.exp(-a)


def lgcp_dataset(
    *,
    rows: int = 6,
    cols: int = 5,
    periods: int = 16,
    start: str = "2009-01-01",
    seed: int = 0,
    truth: dict[str, Any] | None = None,
    return_truth: bool = False,
) -> xr.Dataset:
    """Counts ``quakes(quarter, row, col)`` simulated from the LGCP's forward process.

    ``log rate[t, c] = log total + g_t + log softmax_c(f_e(t)(s_c))`` with ``g`` a
    random walk (centred over the periods), ``f_0 ~ GP(0, eta0² k_ell)`` and
    ``f_e = f_(e-1) + GP(0, eta_d² k_ell)`` (exact Matérn-5/2 on the cell centres), and
    ``quakes ~ NegBinomial(rate, 1/inv_phi)``. With ``return_truth`` the dataset also holds
    the true ``rate`` and ``total`` (not part of the schema; drop them before fitting).
    """
    p = {**LGCP_TRUTH, **(truth or {})}
    rng = np.random.default_rng(seed)
    cell = float(p["cell_km"])
    rr, cc = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    xy = np.column_stack([cc.ravel() * cell, rr.ravel() * cell])  # (cell, 2): x east, y north
    n_cell = rows * cols
    era_len = int(p["era_periods"])
    n_era = -(-periods // era_len)
    jitter = 1e-8 * np.eye(n_cell)
    chol0 = np.linalg.cholesky(matern52_cov(xy, p["ell"], p["eta0"]) + jitter)
    chold = np.linalg.cholesky(matern52_cov(xy, p["ell"], p["eta_d"]) + jitter)
    fields = [chol0 @ rng.standard_normal(n_cell)]
    for _ in range(1, n_era):
        fields.append(fields[-1] + chold @ rng.standard_normal(n_cell))
    field = np.stack(fields)  # (era, cell)
    log_share = field - np.log(np.exp(field).sum(1, keepdims=True))
    walk = np.cumsum(rng.normal(0.0, p["sigma_g"], size=periods))
    g = walk - walk.mean()
    log_total = np.log(p["total"]) + g
    era_of = np.arange(periods) // era_len
    rate = np.exp(log_total[:, None] + log_share[era_of])  # (period, cell)
    n = 1.0 / p["inv_phi"]
    counts = rng.negative_binomial(n, n / (n + rate))
    quarter = pd.date_range(start, periods=periods, freq="QS").as_unit("ns")
    coords = {"quarter": quarter, "row": np.arange(rows), "col": np.arange(cols)}
    dims = ("quarter", "row", "col")
    ds = xr.Dataset(
        {"quakes": (dims, counts.reshape(periods, rows, cols).astype(np.int64))},
        coords=coords,
    )
    ds["quakes"].attrs["units"] = "events"
    if return_truth:
        ds["rate"] = (dims, rate.reshape(periods, rows, cols))
        ds["total"] = (("quarter",), np.exp(log_total))
        ds["field"] = (("era", "row", "col"), field.reshape(n_era, rows, cols))
    return ds


# ------------------------------------------------------------------ the E07 catalogue
LON0, LAT0 = -97.8, 36.1  # the notebook's projection reference point
KM_PER_DEG = np.deg2rad(1.0) * 6371.0
CELL_KM = 25.0
X_EDGES = np.arange(-150.0, 150.0 + CELL_KM, CELL_KM)  # 12 columns
Y_EDGES = np.arange(-112.5, 112.5 + CELL_KM, CELL_KM)  # 9 rows


def _project(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Equirectangular projection to km east / north of (LON0, LAT0) (as in E07)."""
    x = (np.asarray(lon, dtype=float) - LON0) * KM_PER_DEG * np.cos(np.deg2rad(LAT0))
    y = (np.asarray(lat, dtype=float) - LAT0) * KM_PER_DEG
    return x, y


def from_challenges(path: str | Path, *, first: str = "2009Q1", last: str = "2019Q4") -> xr.Dataset:
    """Bin the USGS catalogue of E07 into ``quakes(quarter, row, col)`` (12 x 9 cells).

    Events are projected to km, kept inside the 300 km x 225 km window and between the
    ``first`` and ``last`` quarters, and counted per 25 km cell and quarter. ``row`` 0 is
    the southern edge (draw with ``origin: lower``). The cell centres in km are kept as the
    non-index coordinates ``x_km(col)`` and ``y_km(row)``.
    """
    df = pd.read_csv(path, usecols=["time", "latitude", "longitude"])
    t = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    x, y = _project(df["longitude"].to_numpy(), df["latitude"].to_numpy())
    periods = pd.period_range(first, last, freq="Q")
    in_window = (x >= X_EDGES[0]) & (x < X_EDGES[-1]) & (y >= Y_EDGES[0]) & (y < Y_EDGES[-1])
    q = t.dt.to_period("Q")
    in_time = ((q >= periods[0]) & (q <= periods[-1])).to_numpy()
    keep = in_window & in_time
    col = np.floor((x[keep] - X_EDGES[0]) / CELL_KM).astype(int)
    row = np.floor((y[keep] - Y_EDGES[0]) / CELL_KM).astype(int)
    pidx = np.asarray([p.ordinal for p in q[keep]]) - periods[0].ordinal
    nx, ny = len(X_EDGES) - 1, len(Y_EDGES) - 1
    counts = np.zeros((len(periods), ny, nx), dtype=np.int64)
    np.add.at(counts, (pidx, row, col), 1)
    quarter = periods.to_timestamp(how="start").as_unit("ns")
    ds = xr.Dataset(
        {"quakes": (("quarter", "row", "col"), counts)},
        coords={
            "quarter": quarter,
            "row": np.arange(ny),
            "col": np.arange(nx),
            "x_km": ("col", (X_EDGES[:-1] + X_EDGES[1:]) / 2),
            "y_km": ("row", (Y_EDGES[:-1] + Y_EDGES[1:]) / 2),
        },
    )
    ds["quakes"].attrs["units"] = "events"
    ds.attrs["source"] = "USGS ComCat via pymc-challenges data/oklahoma_quakes.csv (E07)"
    return ds
