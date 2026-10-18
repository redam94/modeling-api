"""Raw schema, example specs, synthetic data and the real E23 data for ``sci.phylo_regression``.

* :func:`phylo_dataset` - species on a random ultrametric tree (a Yule pure-birth tree) whose
  log trait follows the model's own process (a regression plus Pagel-lambda residuals),
  with known truth :data:`PHYLO_TRUTH`;
* :func:`from_challenges` - the 301 primates of pymc-challenges E23 (Street et al. 2017, as in
  McElreath's ``rethinking``) with the 10kTrees consensus tree. The tree is an R object
  (``ape::phylo`` in a gzip XDR ``.rda``); :func:`read_r_object` reads it without R.

The raw layout (:data:`PHYLO_SCHEMA`)::

    brain(species)        target, cc (missing allowed: predicted from relatives)
    body(species)         g
    group_size(species)   animals
    parent(node), branch_length(node) (Myr), is_tip(node), tip_label(node)
"""

from __future__ import annotations

import gzip
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_science.phylo import (
    BRANCH_LENGTH,
    IS_TIP,
    NODE_DIM,
    PARENT,
    SPECIES_DIM,
    TIP_LABEL,
    PhyloTree,
    covariance,
    parse_newick,
)

__all__ = [
    "CHALLENGES_FILE",
    "CHALLENGES_TREE",
    "PHYLO_SCHEMA",
    "PHYLO_SPEC_YAML",
    "PHYLO_TRUTH",
    "PRIMATES_SPEC_YAML",
    "ape_to_tree",
    "challenges_reference",
    "from_challenges",
    "phylo_dataset",
    "random_tree",
    "read_r_object",
    "read_tree",
]

CHALLENGES_FILE = "primates301.csv"
"""The trait table in the pymc-challenges ``data/`` directory (``;`` separated)."""
CHALLENGES_TREE = "primates301_tree.rda"
"""The tree next to it: an ``ape::phylo`` object named ``Primates301_nex``."""

_TREE_VARS: dict[str, Any] = {
    PARENT: {"dims": [NODE_DIM], "dtype": "int64", "role": "constant"},
    BRANCH_LENGTH: {"dims": [NODE_DIM], "dtype": "float64", "role": "constant", "units": "Myr"},
    IS_TIP: {"dims": [NODE_DIM], "dtype": "bool", "role": "constant"},
    TIP_LABEL: {"dims": [NODE_DIM], "dtype": "str", "role": "constant"},
}

PHYLO_SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            SPECIES_DIM: {"kind": "categorical", "policy": "fixed"},
            NODE_DIM: {"kind": "integer", "policy": "fixed"},
        },
        "vars": {
            "brain": {
                "dims": [SPECIES_DIM],
                "dtype": "float64",
                "role": "target",
                "units": "cc",
                "missing": "allow",
            },
            "body": {"dims": [SPECIES_DIM], "dtype": "float64", "role": "covariate", "units": "g"},
            "group_size": {"dims": [SPECIES_DIM], "dtype": "float64", "role": "covariate"},
            **_TREE_VARS,
        },
    }
)
"""Raw schema of :func:`phylo_dataset` and :func:`from_challenges`."""

PHYLO_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: primate-brains-synthetic, project: biology}
data:
  dataset: dataset://biology/primate_traits@latest
  bind:
    vars: {trait: brain}
  split: {kind: mask_block, dims: [species], fraction: 0.2, seed: 3}
model:
  use: sci.phylo_regression@^1
  settings: {kind: lambda, predictors: [body, group_size], standardise: false}
  fit: {mode: evaluate_then_refit, seed: 23, tune: 150, draws: 100, chains: 2, engine: pymc}
evaluate: {metrics: [rmse, mae, coverage_90, elpd_loo]}
outputs: {plots: [core.convergence, core.posterior_forest, sci.phylogeny]}
"""
"""A tiny fit on :func:`phylo_dataset`: logs taken, not standardised, so the slopes are the
truth's; a fifth of the species are held out and predicted from their relatives."""

PRIMATES_SPEC_YAML = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: primate-social-brain, project: biology}
data:
  dataset: dataset://biology/primates301@latest
  bind:
    vars: {trait: brain}
model:
  use: sci.phylo_regression@^1
  settings: {kind: lambda, predictors: [body, group_size]}
  fit: {seed: 2017, tune: 1000, draws: 1000, chains: 4, engine: nutpie}
evaluate: {metrics: [elpd_loo]}
outputs: {plots: [core.convergence, core.posterior_forest, sci.phylogeny]}
"""
"""E23: log brain on log body mass and log group size, Pagel's lambda, the notebook sampler
settings. Fit it with each ``kind`` (iid, brownian, lambda, ou) and compare ``elpd_loo``."""

PHYLO_TRUTH: dict[str, Any] = {
    "intercept": 0.5,
    "slope": {"body": 0.7, "group_size": 0.1},
    "sigma": 0.3,
    "lam": 0.9,
}
"""Generating parameters of :func:`phylo_dataset` (log scale, not standardised)."""


# ============================================================================ synthetic


def random_tree(n_tips: int, *, depth: float = 50.0, seed: int = 0) -> PhyloTree:
    """An ultrametric binary tree of ``n_tips`` (a Yule pure-birth tree scaled to ``depth``).

    Tips are nodes ``0..n_tips-1`` labelled ``sp001``...; internal nodes follow."""
    if n_tips < 2:
        raise ValueError("a tree needs at least two tips")
    rng = np.random.default_rng(seed)
    n = 2 * n_tips - 1
    parent = np.full(n, -1, dtype=np.int64)
    split = np.full(n, np.nan)  # time a node splits (a tip: the present)
    root = n - 1
    split[root] = 0.0
    nxt_inner = n - 2
    # two lineages leave the root; each lineage splits at rate 1 until n_tips are alive
    alive = [-1, -2]  # open lineages (placeholders) with their parent
    owner = {-1: root, -2: root}
    t, k = 0.0, -3
    while len(alive) < n_tips:
        t += rng.exponential(1.0 / len(alive))
        i = int(rng.integers(len(alive)))
        lin = alive.pop(i)
        node = nxt_inner
        nxt_inner -= 1
        parent[node], split[node] = owner.pop(lin), t
        for _ in range(2):
            alive.append(k)
            owner[k] = node
            k -= 1
    end = t + rng.exponential(1.0 / len(alive))
    for node, lin in enumerate(alive):
        parent[node] = owner[lin]
        split[node] = end
    born = np.where(parent >= 0, split[np.maximum(parent, 0)], 0.0)
    length = np.where(parent >= 0, (split - born) * depth / end, 0.0)
    width = len(str(n_tips))
    labels = [f"sp{v + 1:0{max(3, width)}d}" if v < n_tips else "" for v in range(n)]
    return PhyloTree.build(parent, length, np.arange(n) < n_tips, labels)


def phylo_dataset(
    *,
    n_species: int = 40,
    missing: int = 4,
    kind: str = "lambda",
    truth: dict[str, Any] | None = None,
    seed: int = 0,
) -> xr.Dataset:
    """Simulated species traits in the raw layout of :data:`PHYLO_SCHEMA`.

    Log body mass and log group size are part Brownian motion on the tree and part species
    noise; log brain = intercept + slopes . predictors + residual, with the residual drawn
    from the model's covariance of ``kind``. ``missing`` species lose their brain value
    (the model predicts it from relatives)."""
    p = {**PHYLO_TRUTH, **(truth or {})}
    tree = random_tree(n_species, seed=seed)
    rng = np.random.default_rng(seed + 1)
    tips = tree.tips
    c = tree.shared_length(tips)
    depth = tree.tip_depth()
    d = np.diag(c)
    r, dn = c / depth, (d[:, None] + d[None, :] - 2 * c) / (2 * depth)
    chol = np.linalg.cholesky(r + 1e-9 * np.eye(r.shape[0]))
    names = ("body", "group_size")
    x = {
        n: 0.5 * (chol @ rng.standard_normal(r.shape[0])) + 0.85 * rng.standard_normal(r.shape[0])
        for n in names
    }
    params: dict[str, np.ndarray] = {
        k: np.atleast_1d(float(p[k])) for k in ("sigma", "lam", "eta2", "rho") if k in p
    }
    k = covariance(kind, r, dn, params)[0]
    resid = np.linalg.cholesky(k + 1e-9 * np.eye(k.shape[0])) @ rng.standard_normal(k.shape[0])
    logs = {n: x[n] + c0 for n, c0 in zip(names, (7.0, 1.5), strict=True)}
    log_brain = p["intercept"] + sum(p["slope"][n] * logs[n] for n in names) + resid
    brain = np.exp(log_brain)
    if missing:
        brain[rng.choice(brain.size, size=min(missing, brain.size - 3), replace=False)] = np.nan
    labels = np.array([tree.labels[t] for t in tips])
    ds = tree.to_dataset()
    ds = ds.assign(
        {
            "brain": ((SPECIES_DIM,), brain, {"units": "cc"}),
            "body": ((SPECIES_DIM,), np.exp(logs["body"]), {"units": "g"}),
            "group_size": ((SPECIES_DIM,), np.exp(logs["group_size"])),
        }
    )
    ds["branch_length"].attrs["units"] = "Myr"
    ds = ds.assign_coords({SPECIES_DIM: labels})
    return ds


# ============================================================================ the R reader


def read_r_object(path: str | Path) -> Any:
    """Minimal reader of R's XDR serialisation (``.rda``/``.rds``), enough for an ``ape``
    ``phylo`` tree (ported from E23). Lists with names become dicts; vectors become NumPy
    arrays or lists of strings. Raises ``ValueError`` on anything else."""
    with gzip.open(path) as fh:
        buf = fh.read()
    if buf[:5] in (b"RDX2\n", b"RDX3\n"):
        head = buf[5:7]
        pos0 = 7
    else:
        head, pos0 = buf[:2], 2  # an .rds has no RDX header
    if head != b"X\n":
        raise ValueError(f"{path}: not an XDR R file")
    pos = pos0
    refs: list[Any] = []
    end = len(buf)

    def take(n: int) -> bytes:
        nonlocal pos
        if n < 0 or pos + n > end:
            raise ValueError(f"{path}: truncated R object")
        pos += n
        return buf[pos - n : pos]

    def i32() -> int:
        return int(struct.unpack(">i", take(4))[0])

    def item(depth: int = 0) -> Any:
        if depth > 200:
            raise ValueError(f"{path}: R object nested too deeply")
        flags = i32()
        kind, has_attr, has_tag = flags & 0xFF, flags & (1 << 9), flags & (1 << 10)
        if kind in (254, 242):  # NULL, empty environment
            return None
        if kind == 255:  # reference to an earlier symbol
            idx = (flags >> 8) - 1
            if not 0 <= idx < len(refs):
                raise ValueError(f"{path}: bad reference in R object")
            return refs[idx]
        if kind == 1:  # symbol: its name follows
            refs.append(item(depth + 1))
            return refs[-1]
        if kind == 9:  # string
            n = i32()
            return None if n == -1 else take(n).decode("utf-8", "replace")
        if kind == 2:  # pairlist -> dict (tag: value)
            out: dict[Any, Any] = {}
            while kind == 2:
                if has_attr:
                    item(depth + 1)
                tag = item(depth + 1) if has_tag else None
                out[tag] = item(depth + 1)
                flags = i32()
                kind, has_attr, has_tag = flags & 0xFF, flags & (1 << 9), flags & (1 << 10)
            if kind != 254:
                raise ValueError(f"{path}: malformed pairlist in R object")
            return out
        n = i32()
        value: Any
        if kind in (10, 13):  # logical / integer vector
            value = np.frombuffer(take(4 * n), ">i4").astype(np.int64)
        elif kind == 14:  # double vector
            value = np.frombuffer(take(8 * n), ">f8").astype(float)
        elif kind in (16, 19):  # character vector / list
            if n < 0 or n > end:
                raise ValueError(f"{path}: bad vector length in R object")
            value = [item(depth + 1) for _ in range(n)]
        else:
            raise ValueError(f"{path}: R type {kind} is not supported")
        attrs = item(depth + 1) if has_attr else None
        if kind == 19 and isinstance(attrs, dict) and "names" in attrs:
            value = dict(zip(attrs["names"], value, strict=False))
        return value

    version = i32()
    take(8)  # writer and minimal reader versions
    if version == 3:  # native encoding name
        take(i32())
    return item()


def ape_to_tree(phylo: dict[str, Any]) -> PhyloTree:
    """An ``ape::phylo`` list (``edge``, ``edge.length``, ``Nnode``, ``tip.label``) as node
    arrays: tips are nodes ``0..n_tip-1`` (R's ``1..n_tip``), internal nodes follow."""
    for key in ("edge", "edge.length", "Nnode", "tip.label"):
        if key not in phylo:
            raise ValueError(f"not an ape phylo object: no {key!r}")
    edge = np.asarray(phylo["edge"], dtype=np.int64).reshape(2, -1).T - 1  # column-major
    blen = np.asarray(phylo["edge.length"], dtype=float)
    labels = [str(v) for v in phylo["tip.label"]]
    n_tip = len(labels)
    n = n_tip + int(np.asarray(phylo["Nnode"]).ravel()[0])
    if edge.shape[0] != blen.size or edge.min() < 0 or edge.max() >= n:
        raise ValueError("ape phylo object: the edge table does not match the nodes")
    parent = np.full(n, -1, dtype=np.int64)
    length = np.zeros(n)
    parent[edge[:, 1]] = edge[:, 0]
    length[edge[:, 1]] = blen
    tip = np.arange(n) < n_tip
    return PhyloTree.build(parent, length, tip, labels + [""] * (n - n_tip))


def read_tree(path: str | Path) -> PhyloTree:
    """A tree from an R ``.rda``/``.rds`` (the first ``phylo`` object in it) or a Newick file
    (``.nwk``, ``.newick``, ``.tre``, ``.tree``)."""
    p = Path(path)
    if p.suffix.lower() in (".nwk", ".newick", ".tre", ".tree"):
        return parse_newick(p.read_text(encoding="utf-8"))
    obj = read_r_object(p)
    if isinstance(obj, dict) and "edge" not in obj:
        found = [v for v in obj.values() if isinstance(v, dict) and "edge" in v]
        if not found:
            raise ValueError(f"{p}: no ape phylo object in the file")
        obj = found[0]
    if not isinstance(obj, dict):
        raise ValueError(f"{p}: not an ape phylo object")
    return ape_to_tree(obj)


# ============================================================================ real data


def from_challenges(path: str | Path, tree_path: str | Path | None = None) -> xr.Dataset:
    """Read pymc-challenges' ``primates301.csv`` (``;`` separated) and the tree next to it
    (``primates301_tree.rda``, or ``tree_path``) into :data:`PHYLO_SCHEMA`.

    The species are those with a body mass and a group size (186; 151 of them also have a
    brain volume, the notebook's complete cases); the tree is pruned to them (unary nodes
    kept, so shared branch lengths do not change). ``path`` may also be the ``data/``
    directory."""
    p = Path(path)
    if p.is_dir():
        p = p / CHALLENGES_FILE
    df = pd.read_csv(p, sep=";")
    need = {"name", "brain", "body", "group_size"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"{p}: missing columns {sorted(missing)}")
    full = read_tree(Path(tree_path) if tree_path is not None else p.parent / CHALLENGES_TREE)
    keep = df[df["body"].notna() & df["group_size"].notna()].reset_index(drop=True)
    names = [str(v) for v in keep["name"]]
    try:
        tree = full.prune(names)
    except KeyError as exc:
        raise ValueError(f"{p}: species not in the tree: {exc}") from None
    ds = tree.to_dataset()
    ds = ds.assign(
        {
            "brain": ((SPECIES_DIM,), keep["brain"].to_numpy(dtype=float), {"units": "cc"}),
            "body": ((SPECIES_DIM,), keep["body"].to_numpy(dtype=float), {"units": "g"}),
            "group_size": ((SPECIES_DIM,), keep["group_size"].to_numpy(dtype=float)),
        }
    )
    ds["branch_length"].attrs["units"] = "Myr"
    ds = ds.assign_coords({SPECIES_DIM: np.array(names, dtype=str)})
    ds.attrs["tree_depth_full"] = full.tip_depth()
    return ds


def challenges_reference() -> dict[str, dict[str, float]]:
    """Posterior means printed by the E23 notebook (4 chains x 1000 draws, nutpie), per
    kind; ``elpd`` is the conditional PSIS-LOO of section 7 (rounded there)."""
    return {
        "iid": {"intercept": 0.000, "body": 0.893, "group_size": 0.123, "sigma": 0.217},
        "brownian": {
            "intercept": -0.193,
            "body": 0.701,
            "group_size": -0.013,
            "sigma": 0.401,
            "elpd": 90.0,
        },
        "lambda": {
            "intercept": -0.121,
            "body": 0.766,
            "group_size": 0.026,
            "sigma": 0.277,
            "lam": 0.954,
            "elpd": 130.0,
        },
        "ou": {"body": 0.739, "group_size": -0.006, "rate": 0.185, "rho": 1.596, "elpd": 90.0},
    }
