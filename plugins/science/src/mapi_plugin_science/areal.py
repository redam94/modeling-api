"""``sci.areal_bym2``: disease mapping on a graph of areas - iid, ICAR, proper CAR, BYM2 (E18).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  area:   categorical, policy fixed   (districts, counties, health boards)
          area_:  categorical, policy fixed   (the same areas: second axis of the adjacency)
          vertex: integer, policy fixed       (polygon vertices, NaN padded)
    vars  cases(area):               target, float64 - observed counts (NaN = held out)
          expected(area):            exposure - expected counts (age-standardised)
          covariate(area):           covariate - one area-level covariate (E18: AFF, %)
          adjacency(area, area_):    constant - 1 where two areas are neighbours
          ring_x, ring_y(area, vertex): constant - the polygon outlines (map units)

The model is the E18 ladder; ``kind`` picks the prior of the area effects ``u``::

    cases_i ~ Poisson(expected_i * rr_i),   log rr_i = b0 + b1 x_i + u_i
    x = (covariate - mean) / covariate_scale       (E18: per 10 points of AFF)
    poisson: u = 0
    iid:     u = sigma theta,                       theta ~ Normal(0, 1)
    icar:    u = sigma phi,                         phi ~ ICAR(W) (soft sum-to-zero)
    car:     u = phi ~ CAR(0, W, alpha, tau = 1 / sigma^2),   alpha ~ Uniform(0, 1)
    bym2:    u = sigma (sqrt(rho / s) phi + sqrt(1 - rho) theta),   rho ~ Beta(1, 1)

with ``b0, b1 ~ Normal(0, 1)`` and ``sigma ~ HalfNormal(1)``. ``s`` is the **scaling
factor** of the graph: the geometric mean of the diagonal of the generalised inverse of the
graph Laplacian ``Q = D - W`` (:func:`scaling_factor`), so that ``phi / sqrt(s)`` has a
typical marginal variance of 1, like ``theta``.

**Islands.** An area without neighbours gets no pooling under the ICAR (its prior is flat)
and makes the BYM2 scaling factor undefined. ``islands`` decides what happens:
``link_nearest`` (the E18 fix) links each island to the area whose boundary is closest
(the polygons give the distance), then links any remaining separate pieces the same way;
``isolate`` keeps the graph as given (iid and icar only); ``error`` refuses the data.

**Scoring.** The posterior keeps the area effect ``effect`` for every fitted area, so a
score needs only the area labels: ``expected`` and ``covariate`` default to the fitted
values of each area, and a payload may change them (what if the covariate were different).
``simulate`` draws a **new** map of area effects for each posterior draw from the fitted
prior (``sigma``, ``rho``, ``alpha``) - with a hand-written ICAR sampler, because
``pm.ICAR`` has no random number generator - and then new counts.

**Leave-areas-out.** With a ``mask_block`` split over ``area``, the held-out areas keep
their place in the graph and lose only their counts: the likelihood runs over the
observed areas (dim ``area__obs``), and the holdout is predicted from the neighbours.
"""

from __future__ import annotations

import hashlib
import itertools
import math
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import scipy.stats
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import PREDICTIONS, FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.capabilities import Capabilities, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field
from scipy.sparse.csgraph import connected_components

__all__ = [
    "ADJACENCY",
    "AREA_DIM",
    "AREA_PAIR_DIM",
    "COVARIATE",
    "EXPECTED",
    "OBS_DIM",
    "RING_X",
    "RING_Y",
    "TARGET",
    "VERTEX_DIM",
    "AreaGraph",
    "ArealBYM2Model",
    "ArealBYM2Settings",
    "area_graph",
    "boundary_distance",
    "contiguity",
    "draw_car",
    "draw_icar",
    "morans_i",
    "scaling_factor",
]

AREA_DIM = "area"
AREA_PAIR_DIM = "area_"
VERTEX_DIM = "vertex"
TARGET = "cases"
EXPECTED = "expected"
COVARIATE = "covariate"
ADJACENCY = "adjacency"
RING_X = "ring_x"
RING_Y = "ring_y"
OBS_DIM = "area__obs"
"""Dim of the fit likelihood when some areas are held out (their counts are NaN)."""
CENTROID = "centroid"
XY_DIM = "xy"
SMR = "smr"

Kind = Literal["poisson", "iid", "icar", "car", "bym2"]
Islands = Literal["link_nearest", "isolate", "error"]


class ArealBYM2Settings(ModelSettings):
    """Settings of ``sci.areal_bym2``."""

    kind: Kind = Field(
        "bym2",
        description="Prior of the area effects: poisson (none), iid, icar, car (proper CAR) or "
        "bym2 (ICAR plus iid with a mixing share rho) (x-ui-level: basic)",
    )
    islands: Islands = Field(
        "link_nearest",
        description="Areas without neighbours: link_nearest links each to the area with the "
        "closest boundary; isolate keeps them alone (iid and icar only); error stops "
        "(x-ui-level: basic)",
    )
    covariate_scale: float = Field(
        10.0,
        gt=0,
        description="Divide the centred covariate by this (b1 is the effect of one unit)",
    )
    center_covariate: bool = Field(True, description="Subtract the mean of the covariate")
    intercept_sd: float = Field(1.0, gt=0, description="Prior sd of the intercept b0")
    slope_sd: float = Field(1.0, gt=0, description="Prior sd of the covariate effect b1")
    sigma_scale: float = Field(
        1.0, gt=0, description="Half-normal scale of sigma, the sd of the area effects"
    )
    rho_alpha: float = Field(1.0, gt=0, description="Beta prior of rho (bym2): first shape")
    rho_beta: float = Field(1.0, gt=0, description="Beta prior of rho (bym2): second shape")
    exceed_threshold: float = Field(
        1.0, gt=0, description="p_exceed is the probability that rr is above this value"
    )


_TEMPLATE: dict[str, Any] = {
    "params": {},
    "template": {
        "dims": {
            AREA_DIM: {
                "kind": "categorical",
                "policy": "fixed",
                "description": "The areas (districts, counties): the nodes of the graph",
            },
            AREA_PAIR_DIM: {
                "kind": "categorical",
                "policy": "fixed",
                "description": "The same areas, as the second axis of the adjacency matrix",
            },
            VERTEX_DIM: {
                "kind": "integer",
                "policy": "fixed",
                "description": "Vertices of the polygon outlines (NaN padded)",
            },
        },
        "vars": {
            TARGET: {
                "dims": [AREA_DIM],
                "dtype": "float64",
                "role": "target",
                "units": "cases",
                "missing": "allow",
                "bounds": [0, None],
            },
            EXPECTED: {
                "dims": [AREA_DIM],
                "dtype": "float64",
                "role": "exposure",
                "units": "cases",
                "bounds": [0, None],
            },
            COVARIATE: {"dims": [AREA_DIM], "dtype": "float64", "role": "covariate"},
            ADJACENCY: {
                "dims": [AREA_DIM, AREA_PAIR_DIM],
                "dtype": "int64",
                "role": "constant",
                "bounds": [0, 1],
            },
            RING_X: {
                "dims": [AREA_DIM, VERTEX_DIM],
                "dtype": "float64",
                "role": "constant",
                "missing": "allow",
            },
            RING_Y: {
                "dims": [AREA_DIM, VERTEX_DIM],
                "dtype": "float64",
                "role": "constant",
                "missing": "allow",
            },
        },
    },
}


# ============================================================================ graph maths
def _parts(x: np.ndarray, y: np.ndarray) -> list[np.ndarray]:
    """The rings of one area: ``(k, 2)`` vertex arrays split at NaN gaps."""
    ok = np.isfinite(x) & np.isfinite(y)
    out: list[np.ndarray] = []
    start: int | None = None
    for i, good in enumerate([*ok.tolist(), False]):
        if good and start is None:
            start = i
        elif not good and start is not None:
            out.append(np.c_[x[start:i], y[start:i]])
            start = None
    return out


def contiguity(
    ring_x: np.ndarray, ring_y: np.ndarray, *, tol: float = 1.0, rule: str = "queen"
) -> np.ndarray:
    """Adjacency ``(n, n)`` from NaN-padded polygon rings ``(n, vertex)`` (E18).

    ``queen``: two areas share at least one vertex; ``rook``: they share at least one
    boundary segment. Coordinates are rounded to ``tol`` first, so borders digitised twice
    still match.
    """
    if rule not in ("queen", "rook"):
        raise ValueError(f"rule must be 'queen' or 'rook', not {rule!r}")
    rx, ry = np.asarray(ring_x, dtype=float), np.asarray(ring_y, dtype=float)
    sets: list[set[Any]] = []
    for x, y in zip(rx, ry, strict=True):
        keys: set[Any] = set()
        for part in _parts(x, y):
            k = [tuple(v) for v in np.round(part / tol).astype(np.int64).tolist()]
            if rule == "queen":
                keys.update(k)
            else:
                keys.update(frozenset(e) for e in itertools.pairwise(k) if e[0] != e[1])
        sets.append(keys)
    n = len(sets)
    w = np.zeros((n, n), dtype=np.int64)
    for i in range(n):
        for j in range(i + 1, n):
            if sets[i] & sets[j]:
                w[i, j] = w[j, i] = 1
    return w


def boundary_distance(ring_x: np.ndarray, ring_y: np.ndarray, i: int, j: int) -> float:
    """Smallest distance between the vertices of areas ``i`` and ``j`` (inf without rings)."""
    a = np.c_[ring_x[i], ring_y[i]]
    b = np.c_[ring_x[j], ring_y[j]]
    a = a[np.isfinite(a).all(1)]
    b = b[np.isfinite(b).all(1)]
    if not len(a) or not len(b):
        return math.inf
    return float(np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)).min())


def scaling_factor(w: np.ndarray) -> float:
    """The BYM2 scaling factor: the geometric mean of ``diag(pinv(D - W))`` (NaN when the
    graph has an area without neighbours)."""
    q = np.diag(w.sum(1)) - w
    var = np.diag(np.linalg.pinv(q))
    if not (var > 1e-12).all():  # an isolated area: its variance is 0 (or -0 by rounding)
        return math.nan
    return float(np.exp(np.mean(np.log(var))))


def morans_i(z: np.ndarray, w: np.ndarray) -> float:
    """Moran's I of ``z`` on the graph ``w``: the correlation of a value with its
    neighbours' values (about ``-1/(n-1)`` without spatial pattern)."""
    z = np.asarray(z, dtype=float) - np.mean(z)
    return float(len(z) / w.sum() * (z @ w @ z) / (z @ z))


@dataclass(frozen=True, slots=True)
class AreaGraph:
    """The graph the model uses (after the island decision)."""

    w: np.ndarray
    """Symmetric 0/1 adjacency ``(n, n)`` with a zero diagonal."""
    links: tuple[tuple[int, int, float], ...]
    """Edges added by ``link_nearest``: ``(island, neighbour, boundary distance)``."""
    n_components_input: int
    islands_input: tuple[int, ...]
    """Areas without neighbours in the input adjacency."""

    @property
    def n_components(self) -> int:
        return int(connected_components(self.w, directed=False)[0])

    @property
    def degree(self) -> np.ndarray:
        return np.asarray(self.w.sum(1))


def area_graph(
    adjacency: np.ndarray,
    ring_x: np.ndarray | None,
    ring_y: np.ndarray | None,
    islands: Islands,
    names: np.ndarray | None = None,
) -> AreaGraph:
    """Check the input adjacency and apply the island decision (raises ``SCHEMA_INVALID``)."""
    a = np.nan_to_num(np.asarray(adjacency, dtype=float))
    n = a.shape[0]
    if a.ndim != 2 or a.shape != (n, n) or n < 2:
        raise MapiError(
            f"{ADJACENCY!r} must be a square matrix over the areas (got shape {a.shape})",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{ADJACENCY}",
        )
    w = ((a != 0) | (a.T != 0)).astype(np.int64)
    np.fill_diagonal(w, 0)
    n_comp = int(connected_components(w, directed=False)[0])
    alone = tuple(int(i) for i in np.flatnonzero(w.sum(1) == 0))

    def name(i: int) -> str:
        return str(names[i]) if names is not None else str(i)

    links: list[tuple[int, int, float]] = []
    if islands == "error" and n_comp > 1:
        raise MapiError(
            f"the adjacency has {n_comp} separate pieces"
            + (
                f" and areas without neighbours ({', '.join(name(i) for i in alone)})"
                if alone
                else ""
            )
            + "; link them in the data or set islands: link_nearest",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{ADJACENCY}",
        )
    if islands == "link_nearest" and n_comp > 1:
        if ring_x is None or ring_y is None:
            raise MapiError(
                "islands: link_nearest needs the polygons (ring_x, ring_y) to find the closest "
                "area",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{RING_X}",
            )
        rx, ry = np.asarray(ring_x, dtype=float), np.asarray(ring_y, dtype=float)

        def nearest(members: np.ndarray) -> tuple[int, int, float]:
            best = (-1, -1, math.inf)
            inside = set(members.tolist())
            for i in members.tolist():
                for j in range(n):
                    if j in inside:
                        continue
                    d = boundary_distance(rx, ry, i, j)
                    if d < best[2]:
                        best = (int(i), j, d)
            if best[0] < 0 or not math.isfinite(best[2]):
                raise MapiError(
                    f"cannot link {name(int(members[0]))!r}: the polygons give no distance to "
                    "another area",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{RING_X}",
                )
            return best

        # E18: each island to the area with the closest boundary, in data order ...
        for i in alone:
            if w[i].sum():
                continue
            _, j, d = nearest(np.array([i]))
            w[i, j] = w[j, i] = 1
            links.append((i, j, d))
        # ... then any separate piece (smallest first) to its closest area
        while True:
            k, comp = connected_components(w, directed=False)
            if k == 1:
                break
            sizes = np.bincount(comp)
            members = np.flatnonzero(comp == int(np.argmin(sizes)))
            i, j, d = nearest(members)
            w[i, j] = w[j, i] = 1
            links.append((i, j, d))
    return AreaGraph(w=w, links=tuple(links), n_components_input=n_comp, islands_input=alone)


def _laplacian_modes(w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(sqrt of the eigenvalues of pinv(D - W), eigenvectors)`` without the constant
    directions (one per connected piece)."""
    q = np.diag(w.sum(1)).astype(float) - w
    lam, vec = np.linalg.eigh(q)
    keep = lam > 1e-9 * max(1.0, float(lam.max()))
    return 1.0 / np.sqrt(lam[keep]), vec[:, keep]


def draw_icar(w: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """``(n, areas)`` draws of a unit ICAR on the graph ``w`` (sum-to-zero on each piece).

    ``pm.ICAR`` has no random number generator. With ``pinv(D - W) = V diag(l) V'``, a
    constrained draw is ``V sqrt(l) z`` (E18 ``draw_scaled_icar`` without the scaling).
    """
    sd, vec = _laplacian_modes(w)
    return np.asarray(rng.standard_normal((n, sd.size)) * sd) @ vec.T


def draw_car(
    w: np.ndarray, alpha: np.ndarray, tau: np.ndarray, rng: np.random.Generator
) -> np.ndarray:
    """One proper-CAR draw for each ``(alpha, tau)``: precision ``tau (D - alpha W)``."""
    d = np.diag(w.sum(1)).astype(float)
    out = np.empty((alpha.size, w.shape[0]))
    for k, (a, t) in enumerate(zip(alpha.tolist(), tau.tolist(), strict=True)):
        chol = np.linalg.cholesky(t * (d - a * w))
        out[k] = np.linalg.solve(chol.T, rng.standard_normal(w.shape[0]))
    return out


# ============================================================================ the model
class ArealBYM2Model(PyMCModel[ArealBYM2Settings]):
    """Poisson disease mapping with iid, ICAR, proper CAR or BYM2 area effects (E18)."""

    id = "sci.areal_bym2"
    version = "1.0.0"
    Settings = ArealBYM2Settings
    description = (
        "Counts of a disease in areas (districts, counties) against their expected counts, "
        "with a covariate and area effects that borrow strength from neighbouring areas: an "
        "ICAR, a proper CAR, BYM2 (a scaled ICAR mixed with independent noise) or iid "
        "effects. Outputs the smoothed relative risk of each area, the probability that it "
        "is above 1, and the expected counts. The polygons and the graph go into the bundle "
        "for the maps."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["b0", "b1", "sigma", "rho", "rr"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "cases": {
            "dims": [AREA_DIM],
            "units_of": TARGET,
            "description": "Cases in each area: the expected count expected * rr, or a "
            "Poisson draw",
        },
        "rr": {
            "dims": [AREA_DIM],
            "units": "1",
            "description": "Relative risk of each area (1 = the reference rate)",
        },
        "p_exceed": {
            "dims": [AREA_DIM],
            "units": "probability",
            "description": "1 when rr is above exceed_threshold in a draw, else 0: the mean "
            "over draws is the probability P(rr > threshold)",
        },
        "effect": {
            "dims": [AREA_DIM],
            "units": "1",
            "description": "The area effect u on the log relative risk (what the covariate "
            "does not explain)",
        },
    }
    expected: ClassVar[dict[str, str]] = {"rr": "rr", "effect": "effect"}
    plots = (
        "core.convergence",
        "core.posterior_forest",
        "sci.area_map",
        "net.graph",
        "core.ridge",
    )
    resources = ResourceHint(est_fit_seconds=30)
    default_engine = "pymc"
    prior_overlap_vars = ("b0", "b1", "sigma", "rho", "alpha")

    # ------------------------------------------------------------------ data
    @staticmethod
    def graph(ds: xr.Dataset, s: ArealBYM2Settings) -> AreaGraph:
        """The model graph of ``ds`` (canonical names) after the island decision."""
        if ADJACENCY not in ds:
            raise MapiError(
                f"the data has no {ADJACENCY!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{ADJACENCY}",
            )
        adj = ds[ADJACENCY]
        if AREA_PAIR_DIM in adj.coords and AREA_DIM in adj.coords:
            pair = [str(v) for v in adj[AREA_PAIR_DIM].values]
            mine = [str(v) for v in adj[AREA_DIM].values]
            if sorted(pair) != sorted(mine):
                raise MapiError(
                    f"the labels of {AREA_PAIR_DIM!r} must be the labels of {AREA_DIM!r}",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{ADJACENCY}",
                )
            if pair != mine:
                adj = adj.isel({AREA_PAIR_DIM: [pair.index(v) for v in mine]})
        a = np.asarray(adj.transpose(AREA_DIM, AREA_PAIR_DIM).values, dtype=float)
        rx = ry = None
        if RING_X in ds and RING_Y in ds:
            rx = np.asarray(ds[RING_X].transpose(AREA_DIM, ...).values, dtype=float)
            ry = np.asarray(ds[RING_Y].transpose(AREA_DIM, ...).values, dtype=float)
        names = np.asarray(ds[AREA_DIM].values) if AREA_DIM in ds.coords else None
        g = area_graph(a, rx, ry, s.islands, names)
        if s.kind in ("bym2", "car") and g.n_components > 1:
            raise MapiError(
                f"kind {s.kind} needs a connected graph, but it has {g.n_components} separate "
                f"pieces ({len(g.islands_input)} areas without neighbours): the BYM2 scaling "
                "factor and the proper CAR are not defined. Set islands: link_nearest, or use "
                "kind iid or icar",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/islands",
            )
        return g

    @staticmethod
    def _vector(ds: xr.Dataset, name: str) -> np.ndarray:
        return np.asarray(ds[name].transpose(AREA_DIM).values, dtype=float)

    # ------------------------------------------------------------------ build
    def build(self, ds: xr.Dataset, s: ArealBYM2Settings, ctx: BuildContext) -> pm.Model:
        if ctx.is_score:  # pragma: no cover - score() never rebuilds the graph
            raise MapiError("score without a rebuild", code=ErrorCode.INTERNAL_ERROR)
        g = self.graph(ds, s)
        labels = ctx.labels(AREA_DIM)
        n = labels.size
        expected = self._vector(ds, EXPECTED)
        if not (np.isfinite(expected).all() and (expected > 0).all()):
            raise MapiError(
                f"{EXPECTED!r} must be positive in every area",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{EXPECTED}",
            )
        cov = self._vector(ds, COVARIATE)
        center = float(cov.mean()) if s.center_covariate else 0.0
        y = ctx.observed(TARGET, dims=[AREA_DIM])
        seen = np.isfinite(y)
        if not seen.any():
            raise MapiError(
                f"every count of {TARGET!r} is missing",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        yo = y[seen]
        if (yo < 0).any() or not np.allclose(yo, np.round(yo)):
            raise MapiError(
                f"{TARGET!r} must hold whole non-negative counts",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        with pm.Model(coords=ctx.coords()) as model:
            e = ctx.data(EXPECTED, expected, dims=[AREA_DIM])
            c = ctx.data(COVARIATE, cov, dims=[AREA_DIM])
            x = (c - center) / s.covariate_scale
            b0 = pm.Normal("b0", 0.0, s.intercept_sd)
            b1 = pm.Normal("b1", 0.0, s.slope_sd)
            u: Any
            if s.kind == "poisson":
                u = pt.zeros((n,))
            elif s.kind == "iid":
                sigma = pm.HalfNormal("sigma", s.sigma_scale)
                u = sigma * pm.Normal("theta", 0.0, 1.0, dims=AREA_DIM)
            elif s.kind == "icar":
                sigma = pm.HalfNormal("sigma", s.sigma_scale)
                u = sigma * pm.ICAR("phi", W=g.w, dims=AREA_DIM)
            elif s.kind == "car":
                sigma = pm.HalfNormal("sigma", s.sigma_scale)
                alpha = pm.Uniform("alpha", 0.0, 1.0)
                u = pm.CAR(
                    "phi", mu=np.zeros(n), W=g.w, alpha=alpha, tau=1.0 / sigma**2, dims=AREA_DIM
                )
            else:
                scale = scaling_factor(g.w)
                sigma = pm.HalfNormal("sigma", s.sigma_scale)
                rho = pm.Beta("rho", s.rho_alpha, s.rho_beta)
                phi = pm.ICAR("phi", W=g.w, dims=AREA_DIM)
                theta = pm.Normal("theta", 0.0, 1.0, dims=AREA_DIM)
                u = sigma * (pt.sqrt(rho / scale) * phi + pt.sqrt(1.0 - rho) * theta)
            u = pm.Deterministic("effect", u, dims=AREA_DIM)
            log_rr = b0 + b1 * x + u
            rr = pm.Deterministic("rr", pt.exp(log_rr), dims=AREA_DIM)
            mu = e * rr
            if seen.all():
                pm.Poisson(TARGET, mu, observed=y, dims=AREA_DIM)
            else:
                idx = np.flatnonzero(seen)
                model.add_coord(OBS_DIM, labels[idx].tolist())
                pm.Poisson(TARGET, mu[idx], observed=yo, dims=OBS_DIM)
                ctx.observed_vars[TARGET] = (OBS_DIM,)
                ctx.observed_values[TARGET] = yo
        return model

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: ArealBYM2Settings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        if not cfg.keep.log_likelihood:
            # always keep the pointwise log-likelihood (one value per area and draw), so
            # elpd_loo compares the kinds of a ladder without a keep block in the spec
            keep = cfg.keep.model_copy(update={"log_likelihood": True})
            cfg = cfg.model_copy(update={"keep": keep})
        out = super().fit(ds, s, cfg, ctx)
        g = self.graph(ds, s)
        labels = np.asarray(ds[AREA_DIM].values)
        str_labels = [str(v) for v in labels]
        y = self._vector(ds, TARGET)
        seen = np.isfinite(y)
        for name, grp in list(out.groups.items()):
            if OBS_DIM in grp.dims and OBS_DIM not in grp.coords:
                out.groups[name] = grp.assign_coords({OBS_DIM: np.asarray(str_labels)[seen]})
        expected = self._vector(ds, EXPECTED)
        cov = self._vector(ds, COVARIATE)
        const = out.groups.get("constant_data", xr.Dataset())
        extra: dict[str, Any] = {
            ADJACENCY: ((AREA_DIM, AREA_PAIR_DIM), g.w),
            SMR: ((AREA_DIM,), y / expected),
            CENTROID: ((AREA_DIM, XY_DIM), self._centroids(ds)),
        }
        if RING_X in ds and RING_Y in ds:
            extra[RING_X] = ((AREA_DIM, VERTEX_DIM), self._rings(ds, RING_X))
            extra[RING_Y] = ((AREA_DIM, VERTEX_DIM), self._rings(ds, RING_Y))
        const = const.assign(extra).assign_coords(
            {
                AREA_DIM: str_labels,
                AREA_PAIR_DIM: str_labels,
                XY_DIM: ["x", "y"],
            }
        )
        if VERTEX_DIM in const.dims:
            const = const.assign_coords({VERTEX_DIM: np.arange(const.sizes[VERTEX_DIM])})
        out.groups["constant_data"] = const
        smr = np.log((np.where(seen, y, 0.0) + 0.5) / expected)
        out.learned.update(
            {
                "kind": s.kind,
                "n_areas": int(labels.size),
                "n_observed": int(seen.sum()),
                "n_edges": int(g.w.sum() // 2),
                "n_components_input": g.n_components_input,
                "n_components": g.n_components,
                "islands_input": [str_labels[i] for i in g.islands_input],
                "links": [[str_labels[i], str_labels[j], round(d, 6)] for i, j, d in g.links],
                "scaling_factor": scaling_factor(g.w) if g.n_components == 1 else None,
                "covariate_center": float(cov.mean()) if s.center_covariate else 0.0,
                "covariate_scale": s.covariate_scale,
                "morans_i_log_smr": morans_i(smr[seen], g.w[np.ix_(seen, seen)])
                if seen.sum() > 2 and g.w[np.ix_(seen, seen)].sum() > 0
                else None,
            }
        )
        return out

    @staticmethod
    def _rings(ds: xr.Dataset, name: str) -> np.ndarray:
        return np.asarray(ds[name].transpose(AREA_DIM, ...).values, dtype=float)

    @classmethod
    def _centroids(cls, ds: xr.Dataset) -> np.ndarray:
        """Mean vertex of each area's rings (the graph's node positions)."""
        n = int(ds.sizes[AREA_DIM])
        if RING_X not in ds or RING_Y not in ds:
            return np.full((n, 2), np.nan)
        rx, ry = cls._rings(ds, RING_X), cls._rings(ds, RING_Y)
        out = np.full((n, 2), np.nan)
        for i in range(n):
            parts = _parts(rx[i], ry[i])
            if parts:
                pts = np.concatenate([p[:-1] if len(p) > 3 else p for p in parts])
                out[i] = pts.mean(0)
        return out

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        const = fitted.group("constant_data")
        post = fitted.posterior
        fit_labels = [str(v) for v in const[AREA_DIM].values]
        pos = {v: i for i, v in enumerate(fit_labels)}
        labels = (
            np.asarray(ds[AREA_DIM].values) if AREA_DIM in ds.coords else np.asarray(fit_labels)
        )
        try:
            idx = np.array([pos[str(v)] for v in labels], dtype=int)
        except KeyError as exc:  # pragma: no cover - the coordinate plan refuses new areas
            raise MapiError(
                f"area {exc.args[0]!r} was not in the fit", code=ErrorCode.UNSEEN_LEVELS
            ) from None
        expected = self._payload(ds, const, EXPECTED, idx)
        cov = self._payload(ds, const, COVARIATE, idx)
        center = float(fitted.learned.get("covariate_center") or 0.0)  # type: ignore[arg-type]
        x = (cov - center) / s.covariate_scale
        chain, draw = post["chain"].values, post["draw"].values
        n = chain.size * draw.size

        def flat(name: str) -> np.ndarray:
            return np.asarray(post[name].transpose("chain", "draw", ...).values, float).reshape(
                n, -1
            )

        b0, b1 = flat("b0")[:, 0], flat("b1")[:, 0]
        simulate = ctx.mode == "simulate"
        if simulate:
            w = np.asarray(const[ADJACENCY].transpose(AREA_DIM, AREA_PAIR_DIM).values)
            u_all = self._new_effects(s, w, flat, n, ctx.seed)
        else:
            u_all = flat("effect")
        u = u_all[:, idx]
        rr = np.exp(b0[:, None] + b1[:, None] * x[None, :] + u)
        mean = expected[None, :] * rr
        noisy = simulate or ctx.kind == "predictive"
        if noisy:
            uni = np.stack([_uniforms(ctx.seed, str(v), n) for v in labels], axis=1)
            cases = scipy.stats.poisson.ppf(uni, mean)
        else:
            cases = mean
        shape = (chain.size, draw.size, labels.size)
        dims = ("chain", "draw", AREA_DIM)
        coords = {"chain": chain, "draw": draw, AREA_DIM: labels}
        how = {"kind": "expected", "noise": "none"}
        arrays = {
            "cases": xr.DataArray(
                cases.reshape(shape),
                dims=dims,
                coords=coords,
                attrs={"kind": "predictive", "noise": "crn_ppf"} if noisy else how,
            ),
            "rr": xr.DataArray(rr.reshape(shape), dims=dims, coords=coords, attrs=how),
            "p_exceed": xr.DataArray(
                (rr > s.exceed_threshold).astype(float).reshape(shape),
                dims=dims,
                coords=coords,
                attrs=how,
            ),
            "effect": xr.DataArray(u.reshape(shape), dims=dims, coords=coords, attrs=how),
        }
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset(arrays)})

    @staticmethod
    def _payload(ds: xr.Dataset, const: xr.Dataset, name: str, idx: np.ndarray) -> np.ndarray:
        """``name`` from the score data, with the fitted value where it is missing."""
        stored = np.asarray(const[name].transpose(AREA_DIM).values, dtype=float)[idx]
        if name not in ds:
            return stored
        given = np.asarray(ds[name].transpose(AREA_DIM).values, dtype=float)
        return np.where(np.isfinite(given), given, stored)

    @staticmethod
    def _new_effects(
        s: ArealBYM2Settings, w: np.ndarray, flat: Any, n: int, seed: int
    ) -> np.ndarray:
        """``(n, areas)`` new area effects, one map for each posterior draw (simulate)."""
        n_area = w.shape[0]
        rng = np.random.default_rng([int(seed), _hash("simulate-effects")])
        if s.kind == "poisson":
            return np.zeros((n, n_area))
        if s.kind in ("icar", "bym2") and int(connected_components(w, directed=False)[0]) > 1:
            raise MapiError(
                "simulate needs a connected graph for an ICAR (an area without neighbours has "
                "a flat prior); set islands: link_nearest",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/islands",
            )
        sigma = flat("sigma")[:, 0]
        if s.kind == "iid":
            return sigma[:, None] * rng.standard_normal((n, n_area))
        if s.kind == "icar":
            return sigma[:, None] * draw_icar(w, n, rng)
        if s.kind == "car":
            return draw_car(w, flat("alpha")[:, 0], 1.0 / sigma**2, rng)
        rho = flat("rho")[:, 0]
        scale = scaling_factor(w)
        phi = draw_icar(w, n, rng)
        theta = rng.standard_normal((n, n_area))
        return sigma[:, None] * (
            np.sqrt(rho / scale)[:, None] * phi + np.sqrt(1.0 - rho)[:, None] * theta
        )


def _hash(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "little")


def _uniforms(seed: int, label: str, n: int) -> np.ndarray:
    """Frozen uniforms of one area (common random numbers keyed by its label)."""
    return np.random.default_rng([int(seed), _hash(f"cases:{label}")]).uniform(size=n)
