"""``sci.phylo_regression``: a regression across species with residuals correlated by a tree (E23).

Species are not independent data points: close relatives share most of their history. This
model regresses a trait of each species on other traits and reads the correlation of the
residuals off a phylogeny (Felsenstein 1985; PGLS, Grafen 1989).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  species: categorical, fixed     (one level per species; levels are tip labels)
          node:    integer, fixed         (every node of the tree, tips and internal)
    vars  trait(species): target, float64, missing allowed - the response (missing values
            are predicted from relatives)
          <p>(species) for each p in settings.predictors: covariate, float64, no missing
          parent(node):        constant, int64   - parent's position on ``node``, -1 at the root
          branch_length(node): constant, float64 - length of the branch above the node
          is_tip(node):        constant, bool
          tip_label(node):     constant, str     - the species label of each tip

**Model** (on standardised values; ``log_target``/``log_predictors`` take logs first and
``standardise`` centres and scales by the fitted species)::

    trait ~ MvNormal(intercept + X slope, K)
    intercept ~ N(0, intercept_sd),  slope ~ N(0, slope_sd)

with the residual covariance ``K`` of the ``kind`` setting, from the shared branch length
``C = A diag(l) A^T`` of the tips (``A`` the ancestry matrix), ``R = C / T`` (``T`` the
largest tip depth, so ``R`` is a correlation matrix on an ultrametric tree) and the patristic
distance ``Dn = (C_ii + C_jj - 2 C_ij) / 2T``:

* ``iid``: ``K = sigma^2 I`` (no tree);
* ``brownian``: ``K = sigma^2 R`` (Brownian motion along the branches);
* ``lambda``: ``K = sigma^2 (lam R + (1 - lam) I)``, ``lam ~ Beta(1, 1)`` (Pagel's lambda: a
  Brownian part plus a species-level nugget);
* ``ou``: ``K = eta2 exp(-rho Dn) + 1e-6 I`` (Ornstein-Uhlenbeck, a GP on patristic
  distance). The data identify only the Brownian rate ``rate = eta2 rho``; with
  ``ou_parameterisation: rate`` (the default) the prior is on ``rate ~ HalfNormal(rate_sd)``
  and ``rho`` and ``eta2 = rate / rho`` is derived, which removes the ridge of the
  ``(eta2, rho)`` posterior; ``variance`` samples ``eta2`` and ``rho`` directly.

``sigma ~ Exponential(sigma_rate)``, ``rho ~ HalfNormal(rho_sd)``, ``eta2 ~ HalfNormal(eta2_sd)``.

**Prediction from relatives.** Given one posterior draw, a species' trait given the observed
traits of the others is normal (Gaussian conditioning). For a fitted species ``i`` it uses
the precision ``Q = K_OO^-1`` of the observed set (Sundararajan and Keerthi 2001)::

    y_i | y_-i ~ N(y_i - g_i / Q_ii, 1 / Q_ii),    g = Q (y - mu)

and for a species without an observed trait ``N(mu_i + K_iO g, K_ii - K_iO Q K_Oi)``. The
output ``trait`` is this conditional prediction in the units of the target (back-transformed
from the log scale when ``log_target``: then ``expected`` is the conditional median). The fit
keeps the same quantity as ``posterior_predictive/trait`` (conditional means, no noise), so
the tree plot finds it next to ``observed_data/trait``. When the score data changes the
predictors of a fitted species, its leave-one-out mean moves by the change in ``mu_i``.

**Pointwise LOO.** ``log_likelihood/trait`` holds ``log p(y_i | y_-i, theta)`` per draw and
observed species (one inverse per draw), so PSIS-LOO (``elpd_loo``) compares the kinds
correctly. PyMC's own log-likelihood of an ``MvNormal`` is one joint term, and the marginal
density ``N(y_i | mu_i, K_ii)`` reverses the ranking of the kinds (E23 section 7).

**Score modes.** ``predict``/``evaluate``: the conditional prediction of each requested
species from the fitted species (a held-out species of a ``mask_block`` split is predicted
from all others); ``simulate``: joint draws ``MvNormal(mu, K)`` for the requested species
from the fitted parameters, ignoring the observed traits. The score uses the tree, the
standardised predictors and the observed traits kept in the bundle (``constant_data`` and
``observed_data``) and the predictors of the score data.
"""

from __future__ import annotations

import hashlib
import logging
import math
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.checkpoint import CheckpointSink, fit_key
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import EngineHooks, EnginePlan, run_engine
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, field_validator

__all__ = [
    "BRANCH_LENGTH",
    "IS_TIP",
    "KINDS",
    "NODE_DIM",
    "PARENT",
    "PREDICTOR_DIM",
    "SPECIES_DIM",
    "TARGET",
    "TIP_LABEL",
    "TREE_VARS",
    "PhyloRegressionModel",
    "PhyloRegressionSettings",
    "PhyloTree",
    "conditional_loo",
    "conditional_predict",
    "covariance",
    "parse_newick",
    "to_newick",
]

SPECIES_DIM = "species"
NODE_DIM = "node"
PREDICTOR_DIM = "predictor"
TARGET = "trait"
PARENT = "parent"
BRANCH_LENGTH = "branch_length"
IS_TIP = "is_tip"
TIP_LABEL = "tip_label"
TREE_VARS = (PARENT, BRANCH_LENGTH, IS_TIP, TIP_LABEL)
X_VAR = "predictors"
"""Constant data: the standardised predictors ``(species, predictor)`` of the fitted species."""
"""Posterior: the conditional prediction of every fitted species (target units)."""

Kind = Literal["iid", "brownian", "lambda", "ou"]
KINDS: tuple[str, ...] = ("iid", "brownian", "lambda", "ou")
_RESERVED = frozenset({TARGET, *TREE_VARS, SPECIES_DIM, NODE_DIM, PREDICTOR_DIM, X_VAR})
_CHUNK_BYTES = 32 * 2**20


class PhyloRegressionSettings(ModelSettings):
    """Settings of ``sci.phylo_regression``."""

    kind: Kind = Field(
        "lambda",
        description="Residual correlation: iid (no tree), brownian (Brownian motion on the "
        "tree), lambda (Pagel's lambda: tree part plus a species part) or ou "
        "(Ornstein-Uhlenbeck) (x-ui-level: basic)",
    )
    predictors: list[str] = Field(
        default_factory=lambda: ["body", "group_size"],
        max_length=16,
        description="Names of the predictor variables on the species dim (x-ui-level: basic)",
    )
    log_target: bool = Field(True, description="Take the log of the trait (it must be positive)")
    log_predictors: bool = Field(
        True, description="Take the log of each predictor (they must be positive)"
    )
    standardise: bool = Field(
        True,
        description="Centre and scale the trait and the predictors by the fitted species (the "
        "slopes are then in sd of the trait per sd of the predictor)",
    )
    intercept_sd: float = Field(1.0, gt=0, description="Prior sd of the intercept")
    slope_sd: float = Field(0.5, gt=0, description="Prior sd of each slope")
    sigma_rate: float = Field(1.0, gt=0, description="Rate of the exponential prior of sigma")
    lambda_alpha: float = Field(1.0, gt=0, description="Beta prior of lambda: first shape")
    lambda_beta: float = Field(1.0, gt=0, description="Beta prior of lambda: second shape")
    ou_parameterisation: Literal["rate", "variance"] = Field(
        "rate",
        description="ou: put the prior on the Brownian rate eta2*rho (rate, recommended) or on "
        "eta2 and rho (variance)",
    )
    rate_sd: float = Field(1.0, gt=0, description="ou (rate): HalfNormal sd of eta2*rho")
    rho_sd: float = Field(5.0, gt=0, description="ou: HalfNormal sd of rho (pull to the optimum)")
    eta2_sd: float = Field(5.0, gt=0, description="ou (variance): HalfNormal sd of eta2")
    jitter: float = Field(1e-6, ge=0, le=1e-2, description="ou: added to the diagonal of K")

    @field_validator("predictors")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("predictors must be distinct")
        bad = sorted(set(v) & _RESERVED)
        if bad:
            raise ValueError(f"predictor names {bad} are reserved")
        for name in v:
            if (
                not name
                or not (name[0].isalpha() or name[0] == "_")
                or not all(c.isalnum() or c == "_" for c in name)
            ):
                raise ValueError(f"predictor name {name!r} is not a valid variable name")
        return v


_TEMPLATE: dict[str, Any] = {
    "params": {"predictors": {"type": "string_list", "default": ["body", "group_size"]}},
    "template": {
        "dims": {
            SPECIES_DIM: {
                "kind": "categorical",
                "policy": "fixed",
                "description": "One level per species; the labels are tip labels of the tree",
            },
            NODE_DIM: {
                "kind": "integer",
                "policy": "fixed",
                "description": "Every node of the tree (tips and internal nodes)",
            },
        },
        "vars": {
            TARGET: {
                "dims": [SPECIES_DIM],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
                "description": "The trait to explain; missing values are predicted from relatives",
            },
            "{*predictors as p}": {
                "dims": [SPECIES_DIM],
                "dtype": "float64",
                "role": "covariate",
                "description": "A predictor of the trait",
            },
            PARENT: {
                "dims": [NODE_DIM],
                "dtype": "int64",
                "role": "constant",
                "description": "Position of the parent node; -1 at the root",
            },
            BRANCH_LENGTH: {
                "dims": [NODE_DIM],
                "dtype": "float64",
                "role": "constant",
                "description": "Length of the branch above the node (time, e.g. Myr)",
            },
            IS_TIP: {
                "dims": [NODE_DIM],
                "dtype": "bool",
                "role": "constant",
                "description": "True for a tip (a species), false for an internal node",
            },
            TIP_LABEL: {
                "dims": [NODE_DIM],
                "dtype": "str",
                "role": "constant",
                "description": "The species label of each tip (empty for internal nodes)",
            },
        },
    },
}


# ============================================================================== the tree


@dataclass(frozen=True, slots=True)
class PhyloTree:
    """A rooted tree as node arrays (positions ``0..n-1``)."""

    parent: np.ndarray  # int, -1 at the root
    length: np.ndarray  # branch above each node (0 at the root)
    is_tip: np.ndarray  # bool
    labels: tuple[str, ...]  # tip label per node ("" for internal nodes)

    @classmethod
    def build(
        cls,
        parent: Sequence[int] | np.ndarray,
        length: Sequence[float] | np.ndarray,
        is_tip: Sequence[bool] | np.ndarray | None = None,
        labels: Sequence[str] | None = None,
    ) -> PhyloTree:
        """Check the arrays (one root, no cycle, finite non-negative lengths)."""
        par = np.asarray(parent, dtype=np.int64)
        n = par.size
        if par.ndim != 1 or n < 2:
            raise ValueError("a tree needs at least two nodes in one dim")
        bl = np.asarray(length, dtype=float)
        if bl.shape != (n,):
            raise ValueError("branch_length must have one value per node")
        par = np.where((par < 0) | (par == np.arange(n)), -1, par)
        if (par >= n).any():
            raise ValueError("parent points outside the nodes")
        roots = np.flatnonzero(par < 0)
        if roots.size != 1:
            raise ValueError(f"the tree must have exactly one root, found {roots.size}")
        bl = np.where(par < 0, 0.0, bl)
        if not np.isfinite(bl).all() or (bl < 0).any():
            raise ValueError("branch lengths must be finite and not negative")
        has_child = np.zeros(n, dtype=bool)
        has_child[par[par >= 0]] = True
        tip = ~has_child if is_tip is None else np.asarray(is_tip, dtype=bool)
        if tip.shape != (n,):
            raise ValueError("is_tip must have one value per node")
        if (tip & has_child).any():
            raise ValueError("a node marked as a tip has children")
        labs = tuple("" if v is None else str(v) for v in labels) if labels is not None else None
        if labs is None:
            labs = tuple(str(i) if t else "" for i, t in enumerate(tip))
        if len(labs) != n:
            raise ValueError("tip_label must have one value per node")
        tip_labels = [labs[i] for i in np.flatnonzero(tip)]
        if any(not t for t in tip_labels):
            raise ValueError("every tip needs a label")
        if len(set(tip_labels)) != len(tip_labels):
            raise ValueError("tip labels must be unique")
        tree = cls(par, bl, tip, labs)
        tree.depth()  # raises on a cycle
        return tree

    @property
    def n(self) -> int:
        return int(self.parent.size)

    @property
    def root(self) -> int:
        return int(np.flatnonzero(self.parent < 0)[0])

    @property
    def tips(self) -> np.ndarray:
        return np.flatnonzero(self.is_tip)

    def tip_positions(self, labels: Sequence[Any]) -> np.ndarray:
        """Node position of the tip with each label; raises ``KeyError`` naming the missing."""
        pos = {self.labels[i]: int(i) for i in self.tips}
        missing = [str(v) for v in labels if str(v) not in pos]
        if missing:
            raise KeyError(", ".join(missing[:5]) + (" ..." if len(missing) > 5 else ""))
        return np.array([pos[str(v)] for v in labels], dtype=np.int64)

    def depth(self) -> np.ndarray:
        """Distance of every node from the root (raises ``ValueError`` on a cycle)."""
        out = np.full(self.n, np.nan)
        out[self.root] = 0.0
        for v in range(self.n):
            path: list[int] = []
            u = v
            while np.isnan(out[u]):
                path.append(u)
                if len(path) > self.n:
                    raise ValueError("parent has a cycle: it is not a tree")
                u = int(self.parent[u])
                if u < 0:
                    raise ValueError("parent has a cycle: it is not a tree")
            d = out[u]
            for w in reversed(path):
                d = d + self.length[w]
                out[w] = d
        return out

    def ancestry(self, rows: np.ndarray) -> np.ndarray:
        """``A[k, m] = 1`` if the branch above node ``m`` is on the root-to-``rows[k]`` path."""
        a = np.zeros((rows.size, self.n))
        for k, v in enumerate(rows.tolist()):
            u = int(v)
            while self.parent[u] >= 0:
                a[k, u] = 1.0
                u = int(self.parent[u])
        return a

    def shared_length(self, rows: np.ndarray) -> np.ndarray:
        """``C = A diag(l) A^T``: the length of the root path that two nodes share."""
        a = self.ancestry(np.asarray(rows, dtype=np.int64))
        return (a * self.length) @ a.T

    def tip_depth(self) -> float:
        """``T``: the largest depth of a tip."""
        return float(self.depth()[self.tips].max())

    def children(self) -> list[list[int]]:
        out: list[list[int]] = [[] for _ in range(self.n)]
        for c, p in enumerate(self.parent.tolist()):
            if p >= 0:
                out[p].append(c)
        return out

    def prune(self, keep: Sequence[str]) -> PhyloTree:
        """The subtree that spans the tips labelled ``keep`` (nodes without a kept tip below
        are dropped; unary nodes are kept, so shared lengths do not change)."""
        tips = self.tip_positions(keep)
        alive = np.zeros(self.n, dtype=bool)
        for t in tips.tolist():
            u = int(t)
            while u >= 0 and not alive[u]:
                alive[u] = True
                u = int(self.parent[u])
        alive[self.root] = True
        old = np.flatnonzero(alive)
        new_of = np.full(self.n, -1, dtype=np.int64)
        new_of[old] = np.arange(old.size)
        par = np.where(self.parent[old] >= 0, new_of[np.maximum(self.parent[old], 0)], -1)
        tip = np.isin(old, tips)
        labels = tuple(self.labels[i] if tip[k] else "" for k, i in enumerate(old.tolist()))
        return PhyloTree.build(par, self.length[old], tip, labels)

    def to_dataset(self) -> xr.Dataset:
        """The node arrays as the canonical tree variables on ``node``."""
        return xr.Dataset(
            {
                PARENT: ((NODE_DIM,), self.parent.astype(np.int64)),
                BRANCH_LENGTH: ((NODE_DIM,), self.length.astype(float)),
                IS_TIP: ((NODE_DIM,), self.is_tip.astype(bool)),
                TIP_LABEL: ((NODE_DIM,), np.array(self.labels, dtype=str)),
            },
            coords={NODE_DIM: np.arange(self.n, dtype=np.int64)},
        )

    @classmethod
    def from_dataset(cls, ds: xr.Dataset) -> PhyloTree:
        """The tree from the canonical variables (``parent`` as positions on ``node``, or as
        ``node`` labels)."""
        missing = [v for v in (PARENT, BRANCH_LENGTH, TIP_LABEL) if v not in ds]
        if missing:
            raise MapiError(
                f"the data has no tree variables {missing} on dim {NODE_DIM!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{missing[0]}",
            )
        raw = np.asarray(ds[PARENT].values)
        if NODE_DIM in ds.coords:
            coords = np.asarray(ds[NODE_DIM].values)
            if not np.array_equal(coords, np.arange(coords.size)):
                pos = {v.item() if hasattr(v, "item") else v: i for i, v in enumerate(coords)}
                raw = np.array([pos.get(v.item() if hasattr(v, "item") else v, -1) for v in raw])
        tip = ds[IS_TIP].values if IS_TIP in ds else None
        try:
            return cls.build(
                np.where(np.isfinite(raw.astype(float)), raw, -1).astype(np.int64),
                np.asarray(ds[BRANCH_LENGTH].values, dtype=float),
                tip,
                [str(v) for v in ds[TIP_LABEL].values],
            )
        except ValueError as exc:
            raise MapiError(
                f"the tree is not valid: {exc}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{PARENT}",
            ) from None


def to_newick(tree: PhyloTree, digits: int = 6) -> str:
    """The tree as a Newick string (``name:length``; unary nodes are kept)."""
    kids = tree.children()

    def rec(v: int) -> str:
        body = tree.labels[v] if not kids[v] else "(" + ",".join(rec(c) for c in kids[v]) + ")"
        if tree.parent[v] < 0:
            return body
        return f"{body}:{tree.length[v]:.{digits}g}"

    return rec(tree.root) + ";"


def parse_newick(text: str) -> PhyloTree:
    """Parse a Newick string (recursive descent, as in E23). Node 0 is the root; nodes
    without children are tips (named internal nodes keep no label)."""
    s = text.strip()
    if not s.endswith(";"):
        s += ";"
    names: list[str | None] = []
    par: list[int] = []
    length: list[float] = []
    pos = 0

    def node(p: int) -> None:
        nonlocal pos
        k = len(par)
        names.append(None)
        par.append(p)
        length.append(0.0)
        if s[pos] == "(":
            pos += 1
            node(k)
            while s[pos] == ",":
                pos += 1
                node(k)
            if s[pos] != ")":
                raise ValueError(f"newick: expected ')' at {pos}")
            pos += 1
        start = pos
        while s[pos] not in ",():;":
            pos += 1
        names[k] = s[start:pos].strip().strip("'\"") or None
        if s[pos] == ":":
            pos += 1
            start = pos
            while s[pos] not in ",();":
                pos += 1
            length[k] = float(s[start:pos])

    try:
        node(-1)
    except IndexError:
        raise ValueError("newick: the string ends too early") from None
    parent = np.array(par, dtype=np.int64)
    has_child = np.zeros(parent.size, dtype=bool)
    has_child[parent[parent >= 0]] = True
    labels = [(n or "") if not has_child[i] else "" for i, n in enumerate(names)]
    return PhyloTree.build(parent, np.array(length), ~has_child, labels)


# ============================================================================== covariance


def covariance(
    kind: str,
    r: np.ndarray,
    dn: np.ndarray,
    params: dict[str, np.ndarray],
    *,
    jitter: float = 1e-6,
) -> np.ndarray:
    """NumPy twin of the model's ``K``: one matrix per draw ``(S, n, n)`` from parameter
    vectors of length ``S`` (``sigma``; ``lam``; ``eta2`` and ``rho``)."""
    n = r.shape[0]
    eye = np.eye(n)
    if kind == "ou":
        eta2 = np.asarray(params["eta2"], dtype=float)[:, None, None]
        rho = np.asarray(params["rho"], dtype=float)[:, None, None]
        return eta2 * np.exp(-rho * dn[None]) + jitter * eye
    s2 = np.asarray(params["sigma"], dtype=float)[:, None, None] ** 2
    if kind == "iid":
        return s2 * eye
    if kind == "brownian":
        return s2 * r[None]
    if kind == "lambda":
        lam = np.asarray(params["lam"], dtype=float)[:, None, None]
        return s2 * (lam * r[None] + (1.0 - lam) * eye)
    raise ValueError(f"unknown kind {kind!r}")


def conditional_loo(y: np.ndarray, mu: np.ndarray, k: np.ndarray) -> tuple[np.ndarray, ...]:
    """Leave-one-out conditionals of ``y ~ MvNormal(mu, K)`` per draw.

    ``y (n,)``, ``mu (S, n)``, ``K (S, n, n)`` -> ``(mean, var, loglik)``, each ``(S, n)``:
    ``y_i | y_-i ~ N(y_i - g_i / Q_ii, 1 / Q_ii)`` with ``Q = K^-1``, ``g = Q (y - mu)``."""
    q = np.linalg.inv(k)
    g = np.einsum("sij,sj->si", q, y[None, :] - mu)
    qd = np.einsum("sii->si", q)
    mean = y[None, :] - g / qd
    var = 1.0 / qd
    ll = -0.5 * math.log(2 * math.pi) + 0.5 * np.log(qd) - 0.5 * g**2 / qd
    return mean, var, ll


def conditional_predict(
    y_obs: np.ndarray,
    mu_obs: np.ndarray,
    mu_new: np.ndarray,
    k_oo: np.ndarray,
    k_no: np.ndarray,
    k_nn_diag: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Gaussian conditioning of new species on the observed ones, per draw:
    ``mean = mu_n + K_no K_oo^-1 (y - mu_o)``, ``var = K_nn - K_no K_oo^-1 K_on`` (diag)."""
    q = np.linalg.inv(k_oo)
    g = np.einsum("sij,sj->si", q, y_obs[None, :] - mu_obs)
    mean = mu_new + np.einsum("sij,sj->si", k_no, g)
    w = np.einsum("sij,sjk->sik", k_no, q)
    var = k_nn_diag - np.einsum("sij,sij->si", w, k_no)
    return mean, np.maximum(var, 1e-12)


# ============================================================================== the model


@dataclass(slots=True)
class _Design:
    """Everything the fit and the scores need, on the standardised scale."""

    labels: np.ndarray  # species labels (in data order)
    tips: np.ndarray  # node position of each species
    x: np.ndarray  # (n, p) standardised predictors
    y: np.ndarray  # (n,) standardised trait (NaN = missing)
    r: np.ndarray  # (n, n) shared length / T
    dn: np.ndarray  # (n, n) patristic distance / 2T
    tree: PhyloTree
    depth: float

    @property
    def observed(self) -> np.ndarray:
        return np.flatnonzero(np.isfinite(self.y))


def _err(msg: str, pointer: str) -> MapiError:
    return MapiError(msg, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def _values(ds: xr.Dataset, name: str) -> np.ndarray:
    if name not in ds:
        raise _err(f"the data has no variable {name!r} on dim {SPECIES_DIM!r}", f"/data/{name}")
    return np.asarray(ds[name].transpose(SPECIES_DIM).values, dtype=float)


def _log(v: np.ndarray, name: str) -> np.ndarray:
    ok = np.isfinite(v)
    if (v[ok] <= 0).any():
        raise _err(
            f"{name!r} has values <= 0; it must be positive to take its log (set "
            f"{'log_target' if name == TARGET else 'log_predictors'}: false)",
            f"/data/{name}",
        )
    out = np.full_like(v, np.nan)
    out[ok] = np.log(v[ok])
    return out


def _distance(c: np.ndarray, depth: float) -> tuple[np.ndarray, np.ndarray]:
    d = np.diag(c)
    dist = np.maximum(d[:, None] + d[None, :] - 2.0 * c, 0.0)
    return c / depth, dist / (2.0 * depth)


def _transform_x(ds: xr.Dataset, s: PhyloRegressionSettings) -> np.ndarray:
    cols = []
    for p in s.predictors:
        v = _values(ds, p)
        if not np.isfinite(v).all():
            raise _err(f"predictor {p!r} has missing values; drop those species", f"/data/{p}")
        cols.append(_log(v, p) if s.log_predictors else v)
    n = int(ds.sizes[SPECIES_DIM])
    return np.column_stack(cols) if cols else np.zeros((n, 0))


class PhyloRegressionModel(CustomModel[PhyloRegressionSettings]):
    """Regression across species with residuals correlated by a phylogeny (E23)."""

    id = "sci.phylo_regression"
    version = "1.0.0"
    Settings = PhyloRegressionSettings
    description = (
        "A regression of one trait of species on other traits whose residuals are correlated "
        "by a phylogeny: i.i.d., Brownian motion, Pagel's lambda or Ornstein-Uhlenbeck on "
        "the shared branch length of the tree. Predicts a missing trait from relatives and "
        "stores the leave-one-out conditional log density for elpd_loo."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["slope", "intercept", "sigma", "lam", "rate"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        TARGET: {
            "dims": [SPECIES_DIM],
            "units_of": TARGET,
            "description": "The trait of each species predicted from its relatives (Gaussian "
            "conditioning on the other species; the median when the trait is logged), or a "
            "draw with the conditional noise",
        },
        "slope": {
            "dims": [PREDICTOR_DIM],
            "units": "1",
            "description": "Slope of each predictor (sd of the trait per sd of the predictor "
            "when standardised)",
        },
    }
    plots = ("core.convergence", "core.posterior_forest", "sci.phylogeny")
    resources = ResourceHint(est_fit_seconds=30)
    default_engine: ClassVar[str] = "pymc"

    # ================================================================== data
    def design(
        self,
        ds: xr.Dataset,
        s: PhyloRegressionSettings,
        *,
        scaling: dict[str, Any] | None = None,
        tree: PhyloTree | None = None,
    ) -> tuple[_Design, dict[str, Any]]:
        """The standardised design of ``ds`` and the scaling it used (``scaling`` given: the
        fit's, for score data)."""
        if SPECIES_DIM not in ds.dims:
            raise _err(f"the data has no dim {SPECIES_DIM!r}", "/data")
        labels = np.asarray(ds[SPECIES_DIM].values) if SPECIES_DIM in ds.coords else None
        if labels is None:
            raise _err(f"dim {SPECIES_DIM!r} needs labels (the tip labels)", "/data")
        tree = tree if tree is not None else PhyloTree.from_dataset(ds)
        try:
            tips = tree.tip_positions(labels.tolist())
        except KeyError as exc:
            raise _err(
                f"species without a tip of the same label in the tree: {exc}", "/data"
            ) from None
        x = _transform_x(ds, s)
        if TARGET in ds:
            y = _values(ds, TARGET)
            y = _log(y, TARGET) if s.log_target else y
        else:
            y = np.full(labels.size, np.nan)
        if scaling is None:
            obs = np.isfinite(y)
            if obs.sum() < 3:
                raise _err(
                    f"the fit needs at least 3 species with an observed {TARGET!r}",
                    f"/data/{TARGET}",
                )
            if s.standardise:
                yc, ys = float(y[obs].mean()), float(y[obs].std())
                xc, xs = x[obs].mean(axis=0), x[obs].std(axis=0)
            else:
                yc, ys = 0.0, 1.0
                xc, xs = np.zeros(x.shape[1]), np.ones(x.shape[1])
            if ys <= 0 or (xs <= 0).any():
                bad = TARGET if ys <= 0 else s.predictors[int(np.argmin(xs))]
                raise _err(f"{bad!r} has the same value for every fitted species", f"/data/{bad}")
            scaling = {
                "y_center": yc,
                "y_scale": ys,
                "x_center": [float(v) for v in xc],
                "x_scale": [float(v) for v in xs],
                "tree_depth": tree.tip_depth(),
            }
        xz = (x - np.asarray(scaling["x_center"])) / np.asarray(scaling["x_scale"])
        yz = (y - scaling["y_center"]) / scaling["y_scale"]
        depth = float(scaling["tree_depth"])
        if not depth > 0:
            raise _err("every tip of the tree is at the root (all branch lengths 0)", "/data")
        r, dn = _distance(tree.shared_length(tips), depth)
        return _Design(labels, tips, xz, yz, r, dn, tree, depth), scaling

    def to_data_units(self, z: np.ndarray, learned: dict[str, Any], s: Any) -> np.ndarray:
        """Standardised (log) trait values back to the units of the target."""
        v = np.asarray(z) * float(learned["y_scale"]) + float(learned["y_center"])
        return np.exp(v) if s.log_target else v

    # ================================================================== fit
    def build_model(self, d: _Design, s: PhyloRegressionSettings) -> pm.Model:
        """The PyMC model over the species with an observed trait."""
        o = d.observed
        coords = {PREDICTOR_DIM: list(s.predictors), SPECIES_DIM: [str(v) for v in d.labels[o]]}
        y = d.y[o]
        xo = d.x[o]
        with pm.Model(coords=coords) as model:
            a = pm.Normal("intercept", 0.0, s.intercept_sd)
            b = pm.Normal("slope", 0.0, s.slope_sd, dims=PREDICTOR_DIM)
            mu = a + pt.dot(pt.as_tensor(xo), b) if xo.shape[1] else a + pt.zeros(o.size)
            if s.kind == "ou":
                rho = pm.HalfNormal("rho", s.rho_sd)
                if s.ou_parameterisation == "rate":
                    rate = pm.HalfNormal("rate", s.rate_sd)
                    eta2 = pm.Deterministic("eta2", rate / rho)
                else:
                    eta2 = pm.HalfNormal("eta2", s.eta2_sd)
                    pm.Deterministic("rate", eta2 * rho)
                dn = pt.as_tensor(d.dn[np.ix_(o, o)])
                k = eta2 * pt.exp(-rho * dn) + s.jitter * pt.eye(o.size)
                pm.MvNormal(TARGET, mu, cov=k, observed=y, dims=SPECIES_DIM)
                return model
            sigma = pm.Exponential("sigma", s.sigma_rate)
            if s.kind == "iid":
                pm.Normal(TARGET, mu, sigma, observed=y, dims=SPECIES_DIM)
                return model
            r = pt.as_tensor(d.r[np.ix_(o, o)])
            if s.kind == "brownian":
                k = sigma**2 * r
            else:
                lam = pm.Beta("lam", s.lambda_alpha, s.lambda_beta)
                k = sigma**2 * (lam * r + (1.0 - lam) * pt.eye(o.size))
            pm.MvNormal(TARGET, mu, cov=k, observed=y, dims=SPECIES_DIM)
        return model

    def fit(
        self, ds: xr.Dataset, s: PhyloRegressionSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        d, scaling = self.design(ds, s)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        model = self.build_model(d, s)
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
        sink = ctx.checkpoint_sink if isinstance(ctx.checkpoint_sink, CheckpointSink) else None
        key = None
        if sink is not None:
            key = fit_key(
                model_id=self.id,
                version=self.version,
                settings=s.model_dump(mode="json"),
                fit_config=cfg.model_dump(mode="json"),
                data=ds,
            )
        report = None
        if ctx.on_progress is not None:

            def report(fraction: float, message: str) -> None:
                ctx.progress(0.85 * fraction, message)

        with _quiet():
            result = run_engine(
                model,
                plan,
                EngineHooks(progress=report, cancel=ctx.cancel, log=ctx.log, sink=sink, key=key),
            )
        ctx.check_cancelled()
        posterior = result.group("posterior")
        if posterior is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        names = [v for v in _PARAMS if v in posterior]
        posterior = posterior[names]
        if PREDICTOR_DIM in posterior.dims:
            posterior = posterior.assign_coords({PREDICTOR_DIM: list(s.predictors)})
        learned: dict[str, Any] = {
            **scaling,
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "kind": s.kind,
            "n_observed": int(d.observed.size),
            "n_species": int(d.labels.size),
        }
        ctx.progress(0.9, "conditional predictions and pointwise log density")
        hat, loglik = self._fit_conditionals(d, s, posterior, learned, ctx)
        chain, draw = posterior["chain"].values, posterior["draw"].values
        shape = (chain.size, draw.size)
        o = d.observed
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, result.engine)}
        # the conditional predictions sit under the target's name, beside observed_data/trait
        groups["posterior_predictive"] = _clean(
            xr.Dataset(
                {
                    TARGET: (
                        ("chain", "draw", SPECIES_DIM),
                        hat.reshape(*shape, -1).astype(np.float32),
                    )
                },
                coords={"chain": chain, "draw": draw, SPECIES_DIM: d.labels},
            ),
            result.engine,
        )
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], result.engine)
        groups["log_likelihood"] = _clean(
            xr.Dataset(
                {TARGET: (("chain", "draw", SPECIES_DIM), loglik.reshape(*shape, -1))},
                coords={"chain": chain, "draw": draw, SPECIES_DIM: d.labels[o]},
            ),
            result.engine,
        )
        raw_y = _values(ds, TARGET)
        groups["observed_data"] = _clean(
            xr.Dataset({TARGET: ((SPECIES_DIM,), raw_y)}, coords={SPECIES_DIM: d.labels}),
            result.engine,
        )
        const = d.tree.to_dataset()
        const[X_VAR] = xr.DataArray(
            d.x,
            dims=(SPECIES_DIM, PREDICTOR_DIM),
            coords={SPECIES_DIM: d.labels, PREDICTOR_DIM: list(s.predictors)},
        )
        groups["constant_data"] = _clean(const, result.engine)
        free = [rv.name for rv in model.free_RVs]
        with _quiet():
            prior = pm.sample_prior_predictive(
                draws=200, model=model, var_names=free, random_seed=seed
            )
        groups["prior"] = _clean(prior["prior"].to_dataset(), result.engine)
        ctx.progress(1.0, "posterior ready")
        return FitOutput(groups=groups, learned=learned)

    def _fit_conditionals(
        self,
        d: _Design,
        s: PhyloRegressionSettings,
        post: xr.Dataset,
        learned: dict[str, Any],
        ctx: FitContext,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Per draw: the conditional prediction of every species (target units) and the LOO
        log density of every observed species (standardised scale, as the model)."""
        o, m = d.observed, np.flatnonzero(~np.isfinite(d.y))
        params = _flat_params(post)
        n_draws = params["intercept"].size
        mu_all = _mean(d.x, params)
        hat = np.empty((n_draws, d.labels.size))
        loglik = np.empty((n_draws, o.size))
        y = d.y[o]
        for sl in _chunks(n_draws, d.labels.size):
            ctx.check_cancelled()
            p = {k: v[sl] for k, v in params.items()}
            k_oo = covariance(s.kind, d.r[np.ix_(o, o)], d.dn[np.ix_(o, o)], p, jitter=s.jitter)
            mean, _, ll = conditional_loo(y, mu_all[sl][:, o], k_oo)
            hat[sl, o] = mean
            loglik[sl] = ll
            if m.size:
                mm, _ = self._new_conditionals(d, s, p, mu_all[sl], o, m)
                hat[sl, m] = mm
        return self.to_data_units(hat, learned, s), loglik

    def _new_conditionals(
        self,
        d: _Design,
        s: PhyloRegressionSettings,
        p: dict[str, np.ndarray],
        mu: np.ndarray,
        o: np.ndarray,
        m: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        idx = np.concatenate([o, m])
        k = covariance(s.kind, d.r[np.ix_(idx, idx)], d.dn[np.ix_(idx, idx)], p, jitter=s.jitter)
        no = o.size
        return conditional_predict(
            d.y[o],
            mu[:, o],
            mu[:, m],
            k[:, :no, :no],
            k[:, no:, :no],
            np.einsum("sii->si", k[:, no:, no:]),
        )

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        names = [n for n in _FREE if n in post]
        return convergence_diagnostics(post, fitted.groups.get("sample_stats"), var_names=names)

    # ================================================================== score
    def _fitted_design(self, fitted: Fitted, s: PhyloRegressionSettings) -> _Design:
        const = fitted.groups.get("constant_data")
        obs = fitted.groups.get("observed_data")
        if const is None or obs is None or X_VAR not in const or TARGET not in obs:
            raise MapiError(
                f"the bundle of {self.id!r} has no tree, predictors or observed traits; "
                "refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        tree = PhyloTree.from_dataset(const)
        labels = np.asarray(const[SPECIES_DIM].values)
        tips = tree.tip_positions(labels.tolist())
        x = np.asarray(const[X_VAR].transpose(SPECIES_DIM, PREDICTOR_DIM).values, dtype=float)
        raw = np.asarray(obs[TARGET].sel({SPECIES_DIM: labels}).values, dtype=float)
        yv = _log(raw, TARGET) if s.log_target else raw
        learned: dict[str, Any] = dict(fitted.learned)
        y = (yv - float(learned["y_center"])) / float(learned["y_scale"])
        depth = float(learned["tree_depth"])
        r, dn = _distance(tree.shared_length(tips), depth)
        return _Design(labels, tips, x, y, r, dn, tree, depth)

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        fit = self._fitted_design(fitted, s)
        if SPECIES_DIM not in ds.dims or SPECIES_DIM not in ds.coords:
            raise _err(f"the score data has no labelled dim {SPECIES_DIM!r}", "/data")
        # fixed dims arrive padded to the fitted species; score the requested ones only
        all_labels = np.asarray(ds[SPECIES_DIM].values)
        flag = f"{SPECIES_DIM}__requested"
        wanted = (
            np.asarray(ds[flag].values, dtype=bool)
            if flag in ds.coords
            else np.ones(all_labels.size, dtype=bool)
        )
        if not wanted.any():
            raise _err("the score data requests no species", "/data")
        ds = ds.isel({SPECIES_DIM: np.flatnonzero(wanted)})
        labels = np.asarray(ds[SPECIES_DIM].values)
        try:
            new_tips = fit.tree.tip_positions(labels.tolist())
        except KeyError as exc:
            raise _err(f"species without a tip in the fitted tree: {exc}", "/data") from None
        learned = dict(fitted.learned)
        xc = np.asarray(learned["x_center"], dtype=float)
        xs = np.asarray(learned["x_scale"], dtype=float)
        x_new = (_transform_x(ds, s) - xc) / xs
        post = fitted.posterior
        params = _flat_params(post)
        chain, draw = post["chain"].values, post["draw"].values
        n_draws = params["intercept"].size
        # the score species next to the fitted species with an observed trait
        o = fit.observed
        fit_pos = {str(v): i for i, v in enumerate(fit.labels.tolist())}
        in_obs = np.array([fit_pos.get(str(v), -1) for v in labels.tolist()])
        obs_set = set(o.tolist())
        tips_all = np.concatenate([fit.tips[o], new_tips])
        c = fit.tree.shared_length(tips_all)
        r_all, dn_all = _distance(c, fit.depth)
        no, nn = o.size, labels.size
        mu_o = _mean(fit.x[o], params)
        mu_n = _mean(x_new, params)
        rng = _rng(ctx.seed, f"{ctx.mode}:{ctx.kind}")
        out = np.empty((n_draws, nn))
        simulate = ctx.mode == "simulate"
        for sl in _chunks(n_draws, no + nn):
            ctx.check_cancelled()
            p = {k: v[sl] for k, v in params.items()}
            k = covariance(s.kind, r_all, dn_all, p, jitter=s.jitter)
            k_nn = k[:, no:, no:]
            if simulate:
                chol = np.linalg.cholesky(k_nn + 1e-9 * np.eye(nn))
                z = rng.standard_normal((chol.shape[0], nn))
                out[sl] = mu_n[sl] + np.einsum("sij,sj->si", chol, z)
                continue
            mean = np.empty((k.shape[0], nn))
            var = np.empty((k.shape[0], nn))
            fresh = np.array([j for j in range(nn) if in_obs[j] not in obs_set], dtype=np.int64)
            seen = np.array([j for j in range(nn) if in_obs[j] in obs_set], dtype=np.int64)
            if fresh.size:
                mm, vv = conditional_predict(
                    fit.y[o],
                    mu_o[sl],
                    mu_n[sl][:, fresh],
                    k[:, :no, :no],
                    k[:, no + fresh, :no],
                    np.einsum("sii->si", k_nn)[:, fresh],
                )
                mean[:, fresh], var[:, fresh] = mm, vv
            if seen.size:
                lm, lv, _ = conditional_loo(fit.y[o], mu_o[sl], k[:, :no, :no])
                where = np.searchsorted(o, in_obs[seen])
                # the LOO residual is fixed by the fitted data; the score predictors move
                # only the species' own regression mean
                shift = mu_n[sl][:, seen] - mu_o[sl][:, where]
                mean[:, seen], var[:, seen] = lm[:, where] + shift, lv[:, where]
            if ctx.kind == "predictive":
                mean = mean + np.sqrt(var) * rng.standard_normal(mean.shape)
            out[sl] = mean
        noisy = simulate or ctx.kind == "predictive"
        trait = xr.DataArray(
            self.to_data_units(out, learned, s).reshape(chain.size, draw.size, nn),
            dims=("chain", "draw", SPECIES_DIM),
            coords={"chain": chain, "draw": draw, SPECIES_DIM: labels},
            attrs={
                "kind": "predictive" if noisy else "expected",
                "noise": "seeded" if noisy else "none",
            },
        )
        if not wanted.all():
            trait = trait.reindex({SPECIES_DIM: all_labels})
        slope = post["slope"].transpose("chain", "draw", PREDICTOR_DIM)
        pred = xr.Dataset(
            {
                TARGET: trait,
                "slope": xr.DataArray(
                    np.asarray(slope.values, dtype=float),
                    dims=("chain", "draw", PREDICTOR_DIM),
                    coords={PREDICTOR_DIM: list(s.predictors)},
                    attrs={"kind": "posterior", "units": "1"},
                ),
            },
            coords={"chain": chain, "draw": draw},
        )
        return xr.DataTree.from_dict({PREDICTIONS: pred})


# ============================================================================== helpers

_FREE = ("intercept", "slope", "sigma", "lam", "rate", "rho", "eta2")
_PARAMS = _FREE


def _flat_params(post: xr.Dataset) -> dict[str, np.ndarray]:
    """``{name: (S,)}`` (``slope``: ``(S, p)``) over chains then draws."""
    out: dict[str, np.ndarray] = {}
    for name in _PARAMS:
        if name not in post:
            continue
        da = post[name]
        dims = ["chain", "draw", *[d for d in da.dims if d not in ("chain", "draw")]]
        v = np.asarray(da.transpose(*dims).values, dtype=float)
        out[name] = v.reshape(v.shape[0] * v.shape[1], *v.shape[2:])
    if "slope" not in out:
        out["slope"] = np.zeros((out["intercept"].size, 0))
    return out


def _mean(x: np.ndarray, params: dict[str, np.ndarray]) -> np.ndarray:
    """``(S, n)`` regression means."""
    return params["intercept"][:, None] + params["slope"] @ x.T


def _chunks(n_draws: int, n: int) -> Iterator[slice]:
    per = max(1, int(_CHUNK_BYTES // (8 * max(n, 1) ** 2 * 3)))
    for start in range(0, n_draws, per):
        yield slice(start, min(n_draws, start + per))


def _rng(seed: int, name: str) -> np.random.Generator:
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


def _clean(ds: xr.Dataset, engine: str) -> xr.Dataset:
    out = ds.copy()
    for v in out.variables:
        out[v].attrs = {}
    out.attrs = {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": engine,
    }
    return out


@contextmanager
def _quiet() -> Iterator[None]:
    logger = logging.getLogger("pymc")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)
