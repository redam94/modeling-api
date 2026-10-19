"""Raw schema, example specs, synthetic data and the real E19 data for ``sci.graphical_model``.

* :func:`ggm_dataset` - cells drawn from a Gaussian graphical model with a known sparse
  precision matrix (truth :data:`GGM_TRUTH`), plus the true network as ``consensus``;
* :func:`from_challenges` - the Sachs et al. (2005) flow-cytometry data of pymc-challenges
  E19: the 853 cells with the plain CD3/CD28 stimulus, 11 log-transformed proteins, and the
  20-edge consensus signalling network (as an undirected adjacency) when its file is present.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.ggm import partial_correlation

__all__ = [
    "CHALLENGES_FILE",
    "CHALLENGES_REFERENCE",
    "CONSENSUS_FILE",
    "GGM_SCHEMA",
    "GGM_SPEC_YAML",
    "GGM_TRUTH",
    "PROTEINS",
    "SACHS_NS_SPEC_YAML",
    "SACHS_SPEC_YAML",
    "from_challenges",
    "ggm_dataset",
    "read_consensus",
    "true_pcor",
]

CHALLENGES_FILE = "sachs2005.txt"
"""pymc-challenges ``data/`` file: 7466 cells x (11 proteins + 9 condition flags), tab separated."""
CONSENSUS_FILE = "sachs2005_consensus.txt"
"""pymc-challenges ``data/`` file: the 20 directed consensus edges (``1. erk --> akt``)."""

PROTEINS = ("raf", "mek", "plc", "pip2", "pip3", "erk", "akt", "pka", "pkc", "p38", "jnk")
"""The 11 measured molecules, in the column order of the Sachs file."""
_CONDITIONS = ("cd3_cd28", "icam2", "aktinhib", "g0076", "psitect", "u0126", "ly", "pma", "b2camp")

GGM_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "cell": {"kind": "integer", "policy": "new_levels", "new_level_strategy": "pooled"},
            "protein": {"kind": "categorical", "policy": "fixed"},
            "protein_": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            "level": {
                "dims": ["cell", "protein"],
                "dtype": "float64",
                "role": "target",
                "units": "log_intensity",
            },
            "consensus": {"dims": ["protein", "protein_"], "dtype": "int64", "role": "constant"},
        },
    }
)
"""Raw schema of :func:`ggm_dataset` and :func:`from_challenges`: ``level(cell, protein)``
and the known network ``consensus(protein, protein_)``."""

GGM_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: protein-network-ggm, project: biology}
data:
  dataset: dataset://biology/protein_network@latest
  bind:
    dims: {var: protein, var_: protein_}
    vars: {value: level, reference: consensus}
  split: {kind: leave_group_out, dim: cell, n: 40, seed: 19}
model:
  use: sci.graphical_model@^1
  settings: {threshold: 0.1, reference: true}
  fit:
    mode: evaluate_then_refit
    seed: 19
    tune: 150
    draws: 100
    chains: 2
    engine: pymc
    keep: {log_likelihood: true}
evaluate: {metrics: [rmse, crps, coverage_90, elpd_loo]}
outputs: {plots: [core.convergence, net.graph, sci.field_map]}
"""
"""A tiny dense fit on :func:`ggm_dataset`; 40 random cells are held out."""

SACHS_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: sachs-cd3cd28-ggm, project: biology}
data:
  dataset: dataset://biology/sachs_cd3cd28@latest
  bind:
    dims: {var: protein, var_: protein_}
    vars: {value: level, reference: consensus}
  split: {kind: leave_group_out, dim: cell, n: 170, seed: 2005}
model:
  use: sci.graphical_model@^1
  settings: {kind: dense, threshold: 0.1, lkj_eta: 2.0, reference: true}
  fit:
    mode: evaluate_then_refit
    seed: 2005
    tune: 1000
    draws: 1000
    chains: 4
    engine: nutpie
    keep: {log_likelihood: true}
evaluate: {metrics: [rmse, crps, coverage_90, elpd_loo]}
outputs: {plots: [core.convergence, net.graph, sci.field_map]}
"""
"""E19 model A: the dense LKJ model on the 853 CD3/CD28 cells (170 cells held out)."""

SACHS_NS_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: sachs-cd3cd28-neighbourhood, project: biology}
data:
  dataset: dataset://biology/sachs_cd3cd28@latest
  bind:
    dims: {var: protein, var_: protein_}
    vars: {value: level, reference: consensus}
model:
  use: sci.graphical_model@^1
  settings: {kind: neighbourhood, threshold: 0.1, expected_neighbours: 2.0, edge_rule: and,
             reference: true}
  fit:
    seed: 2005
    tune: 1000
    draws: 1000
    chains: 4
    target_accept: 0.99
    engine: nutpie
    keep: {log_likelihood: true}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, net.graph, sci.field_map]}
"""
"""E19 model B: horseshoe neighbourhood selection (no holdout: it cannot score new cells)."""

GGM_TRUTH: dict[str, Any] = {
    "variables": ("g1", "g2", "g3", "g4", "g5", "g6"),
    "edges": {("g1", "g2"): 0.5, ("g2", "g3"): 0.4, ("g3", "g4"): -0.35, ("g1", "g5"): 0.3},
    "mean": (3.0, 2.5, 4.0, 3.5, 2.0, 5.0),
    "sd": (0.5, 1.0, 0.8, 1.5, 0.6, 1.2),
}
"""Generating parameters of :func:`ggm_dataset`: partial correlations of the four true edges
(every other pair is conditionally independent), the means and the marginal sds."""


def _truth_precision(truth: dict[str, Any]) -> np.ndarray:
    names = list(truth["variables"])
    omega = np.eye(len(names))
    for (a, b), rho in truth["edges"].items():
        i, j = names.index(a), names.index(b)
        omega[i, j] = omega[j, i] = -rho
    if np.linalg.eigvalsh(omega)[0] <= 0:
        raise ValueError("the true partial correlations do not give a positive definite matrix")
    return omega


def ggm_dataset(
    *, cells: int = 300, truth: dict[str, Any] | None = None, seed: int = 0
) -> xr.Dataset:
    """Simulated cells ``level(cell, protein)`` from :data:`GGM_TRUTH`, with the true
    network as ``consensus(protein, protein_)``.

    The precision matrix has a unit diagonal and ``-rho`` at each true edge, so the true
    partial correlations are exactly the ``edges`` values; each variable is then scaled to
    its ``sd`` and shifted to its ``mean``.
    """
    p = {**GGM_TRUTH, **(truth or {})}
    names = list(p["variables"])
    omega = _truth_precision(p)
    corr = np.linalg.inv(omega)
    d = np.sqrt(np.diag(corr))
    corr = corr / np.outer(d, d)
    sd = np.asarray(p["sd"], dtype=float)
    cov = corr * np.outer(sd, sd)
    rng = np.random.default_rng(seed)
    x = rng.multivariate_normal(np.asarray(p["mean"], dtype=float), cov, size=cells)
    adj = np.zeros((len(names), len(names)), dtype=np.int64)
    for a, b in p["edges"]:
        i, j = names.index(a), names.index(b)
        adj[i, j] = adj[j, i] = 1
    return _dataset(x, names, adj)


def true_pcor(truth: dict[str, Any] | None = None) -> np.ndarray:
    """The true partial-correlation matrix of :func:`ggm_dataset` (unit diagonal)."""
    return partial_correlation(_truth_precision({**GGM_TRUTH, **(truth or {})}))


def _dataset(x: np.ndarray, names: list[str], adj: np.ndarray | None) -> xr.Dataset:
    data: dict[str, Any] = {
        "level": (("cell", "protein"), np.asarray(x, dtype=float), {"units": "log_intensity"})
    }
    coords: dict[str, Any] = {"cell": np.arange(x.shape[0], dtype=np.int64), "protein": names}
    if adj is not None:
        data["consensus"] = (("protein", "protein_"), adj.astype(np.int64))
        coords["protein_"] = names
    return xr.Dataset(data, coords=coords)


def read_consensus(path: str | Path, names: list[str] | tuple[str, ...] = PROTEINS) -> np.ndarray:
    """The consensus file's directed edges ``a --> b`` as a symmetric 0/1 adjacency."""
    text = Path(path).read_text(encoding="utf-8")
    edges = re.findall(r"\d+\.\s*(\w+)\s*-->\s*(\w+)", text)
    if not edges:
        raise ValueError(f"{path}: no edges like '1. erk --> akt'")
    order = list(names)
    adj = np.zeros((len(order), len(order)), dtype=np.int64)
    for a, b in edges:
        if a not in order or b not in order:
            raise ValueError(f"{path}: edge {a} --> {b} names an unknown variable")
        i, j = order.index(a), order.index(b)
        adj[i, j] = adj[j, i] = 1
    return adj


def from_challenges(
    path: str | Path, *, consensus: str | Path | None = None, observational: bool = True
) -> xr.Dataset:
    """Read pymc-challenges' ``data/sachs2005.txt`` into :data:`GGM_SCHEMA`.

    ``observational`` keeps the 853 cells with the plain CD3/CD28 stimulus and no reagent,
    as the E19 notebook does (pooling the nine conditions mixes nine different graphs).
    The consensus network is read from ``consensus`` (default: ``sachs2005_consensus.txt``
    next to ``path``, when present).
    """
    path = Path(path)
    df = pd.read_csv(path, sep="\t")
    missing = (set(PROTEINS) | set(_CONDITIONS)) - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    if observational:
        flags = df[list(_CONDITIONS)]
        df = df[(df["cd3_cd28"] == 1) & (flags.sum(axis=1) == 1)]
    x = df[list(PROTEINS)].to_numpy(dtype=float)
    if not np.isfinite(x).all():
        raise ValueError(f"{path}: the protein columns have missing values")
    cpath = Path(consensus) if consensus is not None else path.parent / CONSENSUS_FILE
    adj = read_consensus(cpath) if cpath.is_file() else None
    ds = _dataset(x, list(PROTEINS), adj)
    ds.attrs["source"] = "Sachs et al. (2005), via pymc-challenges data/" + path.name
    ds.attrs["cells"] = "cd3_cd28 only (observational)" if observational else "all conditions"
    return ds


CHALLENGES_REFERENCE: dict[str, Any] = {
    "cells": 853,
    "certain_edges": (
        ("erk", "akt"),
        ("raf", "mek"),
        ("pkc", "p38"),
        ("pip2", "pip3"),
        ("pkc", "jnk"),
        ("akt", "pka"),
    ),
    "edge_prob": {("p38", "jnk"): 0.86, ("plc", "pip3"): 0.60, ("mek", "erk"): 0.12},
    "pcor_sd_mean": 0.033,
    "edges": 8,
    "edges_in_consensus": 7,
    "undecided_pairs": 2,
    "prior_pcor_sd": 0.40,
}
"""Numbers the E19 notebook prints for the dense model on the 853 cells (delta = 0.1):
the six pairs with P = 1.00, three edge probabilities, the mean posterior sd of a partial
correlation, the full-data graph (P > 0.5: 8 edges, 7 in the consensus) and the pairs with
0.2 < P < 0.8."""
