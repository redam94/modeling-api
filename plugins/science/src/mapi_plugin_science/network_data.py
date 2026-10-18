"""Raw schema, example specs, synthetic graphs and the real E21 data for
``sci.network_latent_space``.

* :func:`network_dataset` - a graph simulated from the model's own forward process
  (covariates from node attributes, sociality, latent positions), with known truth
  :data:`NETWORK_TRUTH`;
* :func:`from_challenges` - the Lazega (2001) strong-coworker network of pymc-challenges E21
  (71 lawyers, 378 ties) with the six pair covariates of the notebook, in the same schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema
from scipy.special import expit

__all__ = [
    "ATTRIBUTES_FILE",
    "CHALLENGES_FILE",
    "COVARIATE_NAMES",
    "LAZEGA_SPEC_YAML",
    "NETWORK_SCHEMA",
    "NETWORK_SPEC_YAML",
    "NETWORK_TRUTH",
    "challenges_reference",
    "from_challenges",
    "network_dataset",
    "network_latent",
    "pair_covariates",
]

CHALLENGES_FILE = "lazega_cowork.txt"
"""The 71 x 71 coworker matrix in the pymc-challenges ``data/`` directory."""
ATTRIBUTES_FILE = "lazega_attributes.txt"
"""The attributes of the 71 lawyers (read from the same directory)."""

COVARIATE_NAMES = (
    "same_office",
    "same_practice",
    "same_status",
    "same_gender",
    "same_school",
    "years_gap",
)
"""The pair covariates of E21 (``years_gap`` is the gap in years with the firm / 10)."""

NETWORK_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "lawyer": {"kind": "integer", "policy": "fixed"},
            "lawyer_": {"kind": "integer", "policy": "fixed"},
            "covariate": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            "cowork": {
                "dims": ["lawyer", "lawyer_"],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
                "bounds": [0, 1],
            },
            "homophily": {
                "dims": ["lawyer", "lawyer_", "covariate"],
                "dtype": "float64",
                "role": "covariate",
            },
            "office": {"dims": ["lawyer"], "dtype": "int64", "role": "constant"},
        },
    }
)
"""Raw schema of :func:`network_dataset` and :func:`from_challenges`:
``cowork(lawyer, lawyer_)``, ``homophily(lawyer, lawyer_, covariate)`` and ``office(lawyer)``."""

NETWORK_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: coworker-latent-space, project: sociology}
data:
  dataset: dataset://sociology/coworkers@latest
  bind:
    dims: {node: lawyer, node_: lawyer_, cov: covariate}
    vars: {tie: cowork, dyad_cov: homophily, node_group: office}
  split: {kind: mask_block, dims: [node, node_], fraction: 0.1, seed: 4}
model:
  use: sci.network_latent_space@^1
  settings: {kind: latent_space, node_groups: true}
  fit: {mode: evaluate_then_refit, seed: 21, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae, elpd_loo]}
outputs: {plots: [core.convergence, core.posterior_forest]}
"""
"""A tiny fit on :func:`network_dataset` (30 nodes): 10% of the cells held out."""

LAZEGA_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: lazega-coworkers-latent-space, project: sociology}
data:
  dataset: dataset://sociology/lazega_coworkers@latest
  bind:
    dims: {node: lawyer, node_: lawyer_, cov: covariate}
    vars: {tie: cowork, dyad_cov: homophily, node_group: office}
model:
  use: sci.network_latent_space@^1
  settings: {kind: latent_space, node_groups: true}
  fit: {mode: train_only, seed: 1991, tune: 1000, draws: 1000, chains: 4, engine: nutpie}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, core.posterior_forest, net.graph, sci.field_map]}
"""
"""E21 model (c): covariates + sociality + latent space on the 71 lawyers; set ``kind`` to
``covariates`` or ``sociality`` (or ``covariates: false``) for the other rungs."""

NETWORK_TRUTH: dict[str, Any] = {
    "alpha": -0.5,
    "beta": {"same_office": 1.8, "same_practice": 1.2, "years_gap": -0.4},
    "sigma_s": 0.6,
    "sigma_z": 1.5,
}
"""Generating parameters of :func:`network_dataset` (``sigma_z = 0``: no latent space)."""


def pair_covariates(
    attrs: pd.DataFrame, same: tuple[str, ...], gaps: dict[str, float]
) -> np.ndarray:
    """``(n, n, K)`` pair covariates: 1 when two nodes share a value of each ``same`` column,
    then ``|a_i - a_j| / scale`` for each ``gaps`` column (the notebook's ``X``)."""
    cols = []
    for c in same:
        v = attrs[c].to_numpy()
        cols.append((v[:, None] == v[None, :]).astype(float))
    for c, scale in gaps.items():
        v = attrs[c].to_numpy().astype(float)
        cols.append(np.abs(v[:, None] - v[None, :]) / scale)
    return np.stack(cols, axis=-1)


def _dataset(
    adj: np.ndarray, x: np.ndarray, names: list[str], office: np.ndarray, labels: np.ndarray
) -> xr.Dataset:
    return xr.Dataset(
        {
            "cowork": (("lawyer", "lawyer_"), adj.astype(float)),
            "homophily": (("lawyer", "lawyer_", "covariate"), x.astype(float)),
            "office": (("lawyer",), office.astype(np.int64)),
        },
        coords={
            "lawyer": labels.astype(np.int64),
            "lawyer_": labels.astype(np.int64),
            "covariate": names,
        },
    )


def network_dataset(
    *,
    n_nodes: int = 30,
    n_offices: int = 3,
    truth: dict[str, Any] | None = None,
    seed: int = 0,
) -> xr.Dataset:
    """A simulated coworker graph in :data:`NETWORK_SCHEMA`.

    Each node gets an office (``n_offices`` levels), a practice (2 levels) and years with
    the firm; the pair covariates are same office, same practice and the years gap / 10. The
    ties follow the model: ``alpha + x . beta + s_i + s_j - ||z_i - z_j||`` with ``s ~
    N(0, sigma_s)`` and ``z ~ N(0, sigma_z^2 I_2)``. :func:`network_latent` gives the drawn
    ``s`` and ``z`` for the same arguments.
    """
    return _simulate(n_nodes, n_offices, truth, seed)[0]


def network_latent(
    *,
    n_nodes: int = 30,
    n_offices: int = 3,
    truth: dict[str, Any] | None = None,
    seed: int = 0,
) -> dict[str, np.ndarray]:
    """The sociality ``s (node,)`` and positions ``z (node, 2)`` behind
    :func:`network_dataset` with the same arguments."""
    return _simulate(n_nodes, n_offices, truth, seed)[1]


def _simulate(
    n_nodes: int, n_offices: int, truth: dict[str, Any] | None, seed: int
) -> tuple[xr.Dataset, dict[str, np.ndarray]]:
    p = {**NETWORK_TRUTH, **(truth or {})}
    rng = np.random.default_rng(seed)
    office = np.arange(n_nodes) % n_offices + 1
    rng.shuffle(office)
    attrs = pd.DataFrame(
        {
            "office": office,
            "practice": rng.integers(1, 3, n_nodes),
            "years": rng.integers(1, 30, n_nodes),
        }
    )
    x = pair_covariates(attrs, ("office", "practice"), {"years": 10.0})
    names = ["same_office", "same_practice", "years_gap"]
    beta = np.array([p["beta"][k] for k in names])
    soc = p["sigma_s"] * rng.standard_normal(n_nodes)
    z = p["sigma_z"] * rng.standard_normal((n_nodes, 2))
    dist = np.sqrt(((z[:, None] - z[None, :]) ** 2).sum(-1) + 1e-6)
    eta = p["alpha"] + x @ beta + soc[:, None] + soc[None, :] - (dist if p["sigma_z"] else 0.0)
    i, j = np.triu_indices(n_nodes, 1)
    adj = np.zeros((n_nodes, n_nodes))
    adj[i, j] = rng.random(i.size) < expit(eta[i, j])
    adj = adj + adj.T
    ds = _dataset(adj, x, names, office, np.arange(1, n_nodes + 1))
    return ds, {"sociality": soc, "z": z}


def from_challenges(path: str | Path, attributes: str | Path | None = None) -> xr.Dataset:
    """Read pymc-challenges' ``data/lazega_cowork.txt`` (71 x 71, whitespace separated) and
    ``lazega_attributes.txt`` (next to it unless ``attributes`` is given) into
    :data:`NETWORK_SCHEMA`, with the six pair covariates of the E21 notebook."""
    path = Path(path)
    attr_path = Path(attributes) if attributes is not None else path.with_name(ATTRIBUTES_FILE)
    w = pd.read_csv(path, sep=r"\s+", header=None).to_numpy()
    cols = ["seniority", "status", "gender", "office", "years", "age", "practice", "school"]
    att = pd.read_csv(attr_path, sep=r"\s+", header=None)
    if att.shape[1] != len(cols):
        raise ValueError(
            f"{attr_path}: expected {len(cols)} columns ({', '.join(cols)}), got {att.shape[1]}"
        )
    att.columns = cols
    n = len(att)
    if w.shape != (n, n):
        raise ValueError(f"{path}: expected a {n} x {n} matrix, got {w.shape[0]} x {w.shape[1]}")
    if not ((w == w.T).all() and np.isin(w, (0, 1)).all() and np.diag(w).sum() == 0):
        raise ValueError(f"{path}: not a symmetric 0/1 matrix with an empty diagonal")
    x = pair_covariates(att, ("office", "practice", "status", "gender", "school"), {"years": 10.0})
    return _dataset(w, x, list(COVARIATE_NAMES), att["office"].to_numpy(), np.arange(1, n + 1))


def challenges_reference() -> dict[str, dict[str, float]]:
    """Posterior means printed by the E21 notebook (4 chains x 1000 draws, nutpie):
    model (b) sociality, model (c) latent space with covariates and (c0) without, and the
    leave-one-dyad-out elpd of each."""
    return {
        "sociality": {
            "alpha": -5.14,
            "same_office": 2.43,
            "same_practice": 2.01,
            "same_status": -0.55,
            "same_gender": 0.35,
            "same_school": 0.31,
            "years_gap": 0.18,
            "sigma_s": 0.90,
            "elpd_loo": -763.4,
        },
        "latent_space": {
            "same_office": 2.54,
            "same_practice": 2.08,
            "same_status": -0.59,
            "same_gender": 0.38,
            "same_school": 0.33,
            "years_gap": 0.21,
            "sigma_s": 0.90,
            "sigma_z": 0.88,
            "elpd_loo": -761.6,
        },
        "latent_space_no_covariates": {"sigma_z": 2.28, "elpd_loo": -795.7},
        "covariates": {"elpd_loo": -894.2},
        "erdos_renyi": {"elpd_loo": -1060.5},
    }
