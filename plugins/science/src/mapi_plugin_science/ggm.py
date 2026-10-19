"""``sci.graphical_model``: a Gaussian graphical model - which variables are directly linked (E19).

Canonical inputs (bind dataset names onto them in the spec)::

    dims  cell: integer, policy new_levels (pooled)   - exchangeable samples (cells, rows)
          var:  categorical, policy fixed              - the measured variables (graph nodes)
    vars  value(cell, var): target, float64
    optional, with settings.reference = true:
          var_: categorical, policy fixed (the same labels as var)
          reference(var, var_): constant, int64 - a known network (1 = edge) to compare with

For jointly Gaussian variables the zero pattern of the precision matrix ``Omega = Sigma^-1``
is an undirected graph: ``rho_jk = -Omega_jk / sqrt(Omega_jj Omega_kk)`` is the **partial
correlation** of ``j`` and ``k`` given every other variable, and ``Omega_jk = 0`` means
conditional independence. A posterior ``rho_jk`` is continuous, so an edge needs a stated
relevance threshold ``delta``: the edge probability is ``P(|rho_jk| > delta | data)``.

Each variable is standardised with the mean and sd of the fit data (``standardise``); the
model works on that scale and the observations are modelled in their own units through the
same affine map (``x = center + scale * z``), so partial correlations do not depend on it.

``kind: dense`` (the notebook's model A)::

    z_i ~ MvNormal(mu, L L^T),  L ~ LKJCholeskyCov(eta = 2, sd ~ Exponential(1)),  mu ~ N(0, 1)
    Omega = L^-T L^-1 (a triangular solve; positive definite by construction)

It is a generative joint model: it predicts and simulates new cells, and its pointwise
log-likelihood (one value per cell) gives ``elpd_loo``.

``kind: neighbourhood`` (model B, Meinshausen-Buhlmann neighbourhood selection): one
regularised-horseshoe regression of each standardised variable on all the others, in one
model (the ``P x P`` coefficient matrix ``B`` with its diagonal masked)::

    z_ij ~ Normal(sum_k B_jk z_ik, sigma_j),  B_jk = z_jk * lam~_jk * tau0 * tau_j * sigma_j
    tau0 = p0 / (P - 1 - p0) / sqrt(n),  tau_j ~ HalfCauchy(1),  lam_jk ~ HalfCauchy(1)
    c2 ~ InvGamma(2, 2),  lam~ = sqrt(c2 lam^2 / (c2 + (tau0 tau_j sigma_j)^2 lam^2))

The two coefficients of a pair are symmetrised per draw: ``and`` (both ``|B| > delta``),
``or`` (either) or ``rho`` (``sign(B_jk) sqrt(B_jk B_kj)`` above delta; this is also the
partial correlation it reports). The neighbourhood model conditions on the other variables
of each cell, so it cannot predict or simulate new cells: its score returns only the graph
outputs, and it has no holdout evaluation (use ``elpd_loo`` with no split).

**Pointwise log-likelihood** (``log_likelihood``): ``joint`` stores ``log p(x_i)`` per cell
(dense only); ``conditional`` stores ``sum_j log p(x_ij | x_i,-j)`` per cell, which both
kinds define. Compare a dense and a neighbourhood fit by ``elpd_loo`` only with
``log_likelihood: conditional`` on both.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import PREDICTIONS, FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, model_validator

__all__ = [
    "CELL_DIM",
    "CENTER",
    "PAIR_DIM",
    "REFERENCE",
    "SCALE",
    "TARGET",
    "VAR_DIM",
    "GraphicalModel",
    "GraphicalModelSettings",
    "conditional_log_density",
    "edge_indicator",
    "partial_correlation",
    "precision_from_cholesky",
    "symmetrise_neighbourhood",
]

CELL_DIM = "cell"
VAR_DIM = "var"
PAIR_DIM = "var_"
"""The second node dim of the matrix outputs (the same labels as ``var``)."""
TARGET = "value"
REFERENCE = "reference"
"""Optional input: a known adjacency ``(var, var_)``, kept as constant data for the plots."""
CENTER = "center"
SCALE = "scale"
"""Constant data: the per-variable mean and sd of the fit data (the standardisation)."""
_NS_ROW, _NS_COL = "target", "predictor"
"""Dims of the neighbourhood regressions' coefficient matrix ``B`` (row: the regression)."""

Kind = Literal["dense", "neighbourhood"]


class GraphicalModelSettings(ModelSettings):
    """Settings of ``sci.graphical_model``."""

    kind: Kind = Field(
        "dense",
        description="dense: one multivariate normal with an LKJ prior (predicts and simulates "
        "cells); neighbourhood: sparse horseshoe regressions of each variable on the others "
        "(x-ui-level: basic)",
    )
    threshold: float = Field(
        0.1,
        gt=0,
        lt=1,
        description="Relevance threshold delta: an edge is |partial correlation| > delta "
        "(x-ui-level: basic)",
    )
    standardise: bool = Field(
        True, description="Centre and scale each variable with the mean and sd of the fit data"
    )
    reference: bool = Field(
        False,
        description="The data has a known network reference(var, var_) (1 = edge) that the "
        "plots compare with (x-ui-level: basic)",
    )
    log_likelihood: Literal["auto", "joint", "conditional"] = Field(
        "auto",
        description="Pointwise log-likelihood kept for elpd_loo: joint (dense only) or "
        "conditional (each variable given the others; both kinds). auto: joint for dense, "
        "conditional for neighbourhood",
    )
    lkj_eta: float = Field(2.0, gt=0, description="dense: LKJ shape of the correlation prior")
    sd_rate: float = Field(
        1.0, gt=0, description="dense: rate of the Exponential prior of the standardised sds"
    )
    mean_sd: float = Field(1.0, gt=0, description="dense: sd of the Normal prior of the means")
    expected_neighbours: float = Field(
        2.0,
        gt=0,
        description="neighbourhood: prior guess p0 of the neighbours of each variable (sets "
        "the global horseshoe scale)",
    )
    slab_alpha: float = Field(2.0, gt=0, description="neighbourhood: c^2 ~ InvGamma(alpha, beta)")
    slab_beta: float = Field(2.0, gt=0, description="neighbourhood: c^2 ~ InvGamma(alpha, beta)")
    edge_rule: Literal["and", "or", "rho"] = Field(
        "and",
        description="neighbourhood: an edge when both coefficients pass the threshold (and), "
        "either one (or), or their symmetric combination (rho)",
    )

    @model_validator(mode="after")
    def _check(self) -> GraphicalModelSettings:
        if self.kind == "neighbourhood" and self.log_likelihood == "joint":
            raise ValueError(
                "kind neighbourhood has no joint density: set log_likelihood to conditional "
                "(or auto)"
            )
        return self


_TEMPLATE: dict[str, Any] = {
    "params": {"reference": {"type": "boolean", "default": False}},
    "template": {
        "dims": {
            CELL_DIM: {
                "kind": "integer",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
                "description": "Exchangeable samples (cells); new ones may be scored",
            },
            VAR_DIM: {
                "kind": "categorical",
                "policy": "fixed",
                "description": "The measured variables: the nodes of the graph",
            },
        },
        "vars": {
            TARGET: {
                "dims": [CELL_DIM, VAR_DIM],
                "dtype": "float64",
                "role": "target",
                "description": "The measured value of each variable in each cell",
            },
        },
    },
    "cases": [
        {
            "when": {"reference": True},
            "merge": {
                "dims": {
                    PAIR_DIM: {
                        "kind": "categorical",
                        "policy": "fixed",
                        "description": "The variables again (the second node of a pair)",
                    }
                },
                "vars": {
                    REFERENCE: {
                        "dims": [VAR_DIM, PAIR_DIM],
                        "dtype": "int64",
                        "role": "constant",
                        "description": "A known network to compare with (1 = edge)",
                    }
                },
            },
        }
    ],
}


# ============================================================================== numpy twins


def precision_from_cholesky(chol: np.ndarray) -> np.ndarray:
    """``Omega = L^-T L^-1`` for lower-triangular ``chol`` ``(..., P, P)``."""
    eye = np.broadcast_to(np.eye(chol.shape[-1]), chol.shape)
    inv = np.linalg.solve(chol, eye)
    return np.swapaxes(inv, -1, -2) @ inv


def partial_correlation(precision: np.ndarray) -> np.ndarray:
    """``-Omega_jk / sqrt(Omega_jj Omega_kk)`` with a unit diagonal, for ``(..., P, P)``."""
    d = np.sqrt(np.diagonal(precision, axis1=-2, axis2=-1))
    rho = -precision / (d[..., :, None] * d[..., None, :])
    p = precision.shape[-1]
    idx = np.arange(p)
    rho[..., idx, idx] = 1.0
    return rho


def edge_indicator(pcor: np.ndarray, threshold: float) -> np.ndarray:
    """``1.0`` where ``|pcor| > threshold`` off the diagonal, else ``0.0``."""
    p = pcor.shape[-1]
    return (np.abs(pcor) > threshold).astype(float) * (1.0 - np.eye(p))


def symmetrise_neighbourhood(
    coef: np.ndarray, threshold: float, rule: str = "and"
) -> tuple[np.ndarray, np.ndarray]:
    """``(rho, edge)`` from neighbourhood coefficients ``coef[..., j, k]`` (variable ``j``
    regressed on ``k``): ``rho = sign(B_jk) sqrt(max(B_jk B_kj, 0))`` (unit diagonal) and the
    edge indicator by ``rule`` (``and`` / ``or`` on ``|B| > threshold``, or ``rho``)."""
    bt = np.swapaxes(coef, -1, -2)
    p = coef.shape[-1]
    off = 1.0 - np.eye(p)
    rho = np.sign(coef) * np.sqrt(np.clip(coef * bt, 0.0, None)) * off + np.eye(p)
    big, big_t = np.abs(coef) > threshold, np.abs(bt) > threshold
    if rule == "and":
        hit = big & big_t
    elif rule == "or":
        hit = big | big_t
    else:
        hit = np.abs(rho) > threshold
    return rho, hit.astype(float) * off


def conditional_log_density(z: np.ndarray, mu: np.ndarray, precision: np.ndarray) -> np.ndarray:
    """``sum_j log N(z_ij | E[z_ij | z_i,-j], 1 / Omega_jj)`` per row of ``z`` ``(N, P)``,
    for parameters ``mu (..., P)`` and ``precision (..., P, P)``; returns ``(..., N)``."""
    r = z - mu[..., None, :]
    w = np.diagonal(precision, axis1=-2, axis2=-1)[..., None, :]
    e = (r @ precision) / w
    return np.sum(0.5 * np.log(w / (2 * math.pi)) - 0.5 * w * e**2, axis=-1)


# ============================================================================== the model


class GraphicalModel(PyMCModel[GraphicalModelSettings]):
    """Bayesian Gaussian graphical model: partial correlations and edge probabilities (E19)."""

    id = "sci.graphical_model"
    version = "1.0.0"
    Settings = GraphicalModelSettings
    description = (
        "Learns which variables are directly linked: partial correlations (the correlation of "
        "two variables given all the others) and the probability of an edge, P(|rho| > "
        "threshold). kind dense is a multivariate normal with an LKJ prior on the correlations "
        "(it also predicts and simulates new samples); kind neighbourhood is a sparse "
        "horseshoe regression of each variable on the others with AND/OR symmetrisation."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "simulate", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
        identified_quantities=["pcor", "edge_prob"],
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "pcor": {
            "dims": [VAR_DIM, PAIR_DIM],
            "units": "1",
            "description": "Partial correlation of each pair of variables given all the others "
            "(unit diagonal)",
            "grid": {"y": VAR_DIM, "x": PAIR_DIM, "origin": "upper", "colormap": "diverging"},
        },
        "edge_prob": {
            "dims": [VAR_DIM, PAIR_DIM],
            "units": "1",
            "description": "Edge indicator per draw (|partial correlation| > threshold); its "
            "mean over the draws is the edge probability",
            "grid": {"y": VAR_DIM, "x": PAIR_DIM, "origin": "upper", "colormap": "viridis"},
        },
        TARGET: {
            "dims": [CELL_DIM, VAR_DIM],
            "units_of": TARGET,
            "description": "dense only: the values of each cell (a draw from the model with "
            "kind predictive or mode simulate)",
        },
    }
    expected: ClassVar[dict[str, str]] = {"pcor": "pcor", "edge_prob": "edge_prob"}
    predictive: ClassVar[dict[str, str]] = {TARGET: TARGET}
    plots = ("core.convergence", "core.posterior_forest", "net.graph", "sci.field_map")
    default_engine = "pymc"
    prior_overlap_vars = ("mu", "c2")

    # ------------------------------------------------------------------ checks
    @staticmethod
    def check_settings(s: GraphicalModelSettings, n_vars: int) -> None:
        """Typed refusals of the settings against the number of variables (the settings
        alone are checked when they are parsed)."""
        if n_vars < 2:
            raise MapiError(
                f"{VAR_DIM!r} needs at least 2 variables for a graph",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{VAR_DIM}",
            )
        if s.kind == "neighbourhood" and not s.expected_neighbours < n_vars - 1:
            raise MapiError(
                f"expected_neighbours ({s.expected_neighbours:g}) must be below the number of "
                f"other variables ({n_vars - 1})",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/expected_neighbours",
            )

    @staticmethod
    def loglik_kind(s: GraphicalModelSettings) -> str:
        """``joint`` or ``conditional``: what the stored pointwise log-likelihood is."""
        if s.log_likelihood != "auto":
            return s.log_likelihood
        return "joint" if s.kind == "dense" else "conditional"

    @staticmethod
    def _matrix(ds: xr.Dataset) -> np.ndarray:
        x = np.asarray(ds[TARGET].transpose(CELL_DIM, VAR_DIM).values, dtype=float)
        if not np.isfinite(x).all():
            raise MapiError(
                f"{TARGET!r} has missing or infinite values: the model needs every variable "
                "in every cell",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        return x

    def _standardisation(
        self, ds: xr.Dataset, s: GraphicalModelSettings, ctx: BuildContext
    ) -> tuple[np.ndarray, np.ndarray]:
        """``(center, scale)`` per variable: from the fit data at fit, from the bundle at score."""
        p = len(ctx.labels(VAR_DIM))
        if ctx.is_score:
            if CENTER not in ds or SCALE not in ds:
                raise MapiError(
                    f"the score data has no {CENTER!r}/{SCALE!r}: score through the platform, "
                    "which adds the standardisation kept in the bundle",
                    code=ErrorCode.INTERNAL_ERROR,
                )
            return (
                np.asarray(ds[CENTER].transpose(VAR_DIM).values, dtype=float),
                np.asarray(ds[SCALE].transpose(VAR_DIM).values, dtype=float),
            )
        if not s.standardise:
            return np.zeros(p), np.ones(p)
        x = self._matrix(ds)
        if x.shape[0] < 2:
            raise MapiError(
                f"the fit needs at least 2 {CELL_DIM!r} to standardise",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{CELL_DIM}",
            )
        sd = x.std(axis=0)
        if (sd <= 0).any():
            flat = [str(v) for v in np.asarray(ctx.labels(VAR_DIM))[sd <= 0]]
            raise MapiError(
                f"variables {flat} do not vary in the fit data: drop them or set standardise "
                "to false",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
            )
        return x.mean(axis=0), sd

    def _reference(self, ds: xr.Dataset, labels: np.ndarray) -> np.ndarray:
        if REFERENCE not in ds:
            raise MapiError(
                f"settings.reference is true but the data has no {REFERENCE!r}(var, var_)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{REFERENCE}",
            )
        ref = ds[REFERENCE]
        if PAIR_DIM in ref.coords and not np.array_equal(
            np.asarray(ref[PAIR_DIM].values).astype(str), labels.astype(str)
        ):
            ref = ref.reindex({PAIR_DIM: labels})
        values = np.asarray(ref.transpose(VAR_DIM, PAIR_DIM).values, dtype=float)
        if values.shape != (labels.size, labels.size) or not np.isfinite(values).all():
            raise MapiError(
                f"{REFERENCE!r} must be a full {VAR_DIM} x {PAIR_DIM} matrix over the same "
                "variables",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{REFERENCE}",
            )
        return (values != 0).astype(np.int64)

    # ------------------------------------------------------------------ model
    def build(self, ds: xr.Dataset, s: GraphicalModelSettings, ctx: BuildContext) -> pm.Model:
        labels = np.asarray(ctx.labels(VAR_DIM))
        p = labels.size
        self.check_settings(s, p)
        center, scale = self._standardisation(ds, s, ctx)
        coords = ctx.coords()
        coords[PAIR_DIM] = coords[VAR_DIM]
        if s.kind == "neighbourhood":
            coords[_NS_ROW] = coords[VAR_DIM]
            coords[_NS_COL] = coords[VAR_DIM]
        with pm.Model(coords=coords) as model:
            c = ctx.data(CENTER, center, dims=[VAR_DIM])
            sc = ctx.data(SCALE, scale, dims=[VAR_DIM])
            if ctx.is_fit and s.reference:
                ctx.data(REFERENCE, self._reference(ds, labels), dims=[VAR_DIM, PAIR_DIM])
            if s.kind == "dense":
                self._dense(model, s, ctx, p, c, sc)
            else:
                self._neighbourhood(ds, s, ctx, p, center, scale)
        return model

    @staticmethod
    def _dense(
        model: pm.Model,
        s: GraphicalModelSettings,
        ctx: BuildContext,
        p: int,
        c: Any,
        sc: Any,
    ) -> None:
        chol, _, _ = pm.LKJCholeskyCov(
            "chol", n=p, eta=s.lkj_eta, sd_dist=pm.Exponential.dist(s.sd_rate), compute_corr=True
        )
        mu = pm.Normal("mu", 0.0, s.mean_sd, dims=VAR_DIM)
        l_inv = pt.linalg.solve_triangular(chol, pt.eye(p), lower=True)
        omega = l_inv.T @ l_inv
        d = pt.sqrt(pt.diag(omega))
        pcor = pm.Deterministic(
            "pcor", -omega / pt.outer(d, d) + 2 * pt.eye(p), dims=(VAR_DIM, PAIR_DIM)
        )
        pm.Deterministic(
            "edge_prob",
            pt.cast(pt.abs(pcor) > s.threshold, "float64") * (1.0 - pt.eye(p)),
            dims=(VAR_DIM, PAIR_DIM),
        )
        pm.Deterministic("precision", omega, dims=(VAR_DIM, PAIR_DIM))
        mean = pm.Deterministic("mean", c + sc * mu, dims=VAR_DIM)
        y = ctx.observed(TARGET, dims=[CELL_DIM, VAR_DIM])
        if ctx.is_score:
            n = len(model.coords[CELL_DIM])
            pm.Deterministic("value_mean", pt.broadcast_to(mean, (n, p)), dims=(CELL_DIM, VAR_DIM))
        pm.MvNormal(TARGET, mean, chol=sc[:, None] * chol, observed=y, dims=(CELL_DIM, VAR_DIM))

    @staticmethod
    def _neighbourhood(
        ds: xr.Dataset,
        s: GraphicalModelSettings,
        ctx: BuildContext,
        p: int,
        center: np.ndarray,
        scale: np.ndarray,
    ) -> None:
        if ctx.is_score:
            raise MapiError(
                "kind neighbourhood conditions on the other variables of each cell, so it "
                "cannot predict or simulate cells: fit kind dense for that",
                code=ErrorCode.MODE_NOT_SUPPORTED,
                pointer="/model/settings/kind",
            )
        x = GraphicalModel._matrix(ds)
        z = (x - center) / scale
        n = z.shape[0]
        tau0 = s.expected_neighbours / (p - 1 - s.expected_neighbours) / math.sqrt(n)
        mask = 1.0 - np.eye(p)
        sigma = pm.HalfNormal("sigma", 1.0, dims=_NS_ROW)
        tau = pm.HalfCauchy("tau", 1.0, dims=_NS_ROW)
        lam = pm.HalfCauchy("lam", 1.0, dims=(_NS_ROW, _NS_COL))
        c2 = pm.InverseGamma("c2", s.slab_alpha, s.slab_beta)
        t = (tau0 * tau * sigma)[:, None]
        lam_tilde = pt.sqrt(c2 * lam**2 / (c2 + t**2 * lam**2))
        z_raw = pm.Normal("z_raw", 0.0, 1.0, dims=(_NS_ROW, _NS_COL))
        coef = pm.Deterministic("B", z_raw * lam_tilde * t * mask, dims=(_NS_ROW, _NS_COL))
        coef_t = coef.T
        eye = pt.eye(p)
        pcor = pm.Deterministic(
            "pcor",
            pt.sign(coef) * pt.sqrt(pt.clip(coef * coef_t, 0.0, np.inf)) * mask + eye,
            dims=(VAR_DIM, PAIR_DIM),
        )
        big, big_t = pt.abs(coef) > s.threshold, pt.abs(coef_t) > s.threshold
        if s.edge_rule == "and":
            hit = pt.and_(big, big_t)
        elif s.edge_rule == "or":
            hit = pt.or_(big, big_t)
        else:
            hit = pt.abs(pcor) > s.threshold
        pm.Deterministic("edge_prob", pt.cast(hit, "float64") * mask, dims=(VAR_DIM, PAIR_DIM))
        y = ctx.observed(TARGET, dims=[CELL_DIM, VAR_DIM])
        pred = pt.as_tensor(z) @ coef_t
        pm.Normal(
            TARGET,
            center + scale * pred,
            scale * sigma[None, :],
            observed=y,
            dims=(CELL_DIM, VAR_DIM),
        )

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: GraphicalModelSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)
        ll = out.groups.get("log_likelihood")
        how = self.loglik_kind(s)
        if ll is not None and TARGET in ll:
            if s.kind == "neighbourhood":
                cell_ll = ll[TARGET].sum(VAR_DIM)
            elif how == "conditional":
                cell_ll = self._dense_conditional(out.groups, ds)
            else:
                cell_ll = ll[TARGET]
            ll = ll.assign({TARGET: cell_ll})
            used = {d for v in ll.data_vars.values() for d in v.dims}
            out.groups["log_likelihood"] = ll.drop_dims([d for d in ll.dims if d not in used])
        post = out.groups["posterior"]
        prob = post["edge_prob"].mean(("chain", "draw"))
        upper = np.triu(np.ones(prob.shape, dtype=bool), 1)
        out.learned["kind"] = s.kind
        out.learned["threshold"] = s.threshold
        out.learned["log_likelihood"] = how
        out.learned["edges"] = int((prob.values[upper] > 0.5).sum())
        out.learned["undecided_pairs"] = int(
            ((prob.values[upper] > 0.2) & (prob.values[upper] < 0.8)).sum()
        )
        out.learned["pcor_sd_mean"] = float(
            post["pcor"].std(("chain", "draw")).values[upper].mean()
        )
        return out

    @staticmethod
    def _dense_conditional(groups: dict[str, xr.Dataset], ds: xr.Dataset) -> xr.DataArray:
        """Per-cell ``sum_j log p(x_ij | x_i,-j)`` of the dense model, from its posterior."""
        post = groups["posterior"]
        const = groups["constant_data"]
        center = np.asarray(const[CENTER].transpose(VAR_DIM).values, dtype=float)
        scale = np.asarray(const[SCALE].transpose(VAR_DIM).values, dtype=float)
        x = GraphicalModel._matrix(ds)
        z = (x - center) / scale
        omega = post["precision"].transpose("chain", "draw", VAR_DIM, PAIR_DIM).values
        mu = post["mu"].transpose("chain", "draw", VAR_DIM).values
        values = np.stack(
            [conditional_log_density(z, mu[c], omega[c]) for c in range(omega.shape[0])]
        ) - float(np.log(scale).sum())
        return xr.DataArray(
            values,
            dims=("chain", "draw", CELL_DIM),
            coords={
                "chain": post["chain"].values,
                "draw": post["draw"].values,
                CELL_DIM: ds[CELL_DIM].values if CELL_DIM in ds.coords else np.arange(len(z)),
            },
        )

    # ------------------------------------------------------------------ score
    def _targets(self, model: pm.Model, kind: str) -> dict[str, tuple[str, str]]:
        targets = super()._targets(model, kind)
        if kind == "expected" and "value_mean" in model.named_vars:
            targets[TARGET] = ("value_mean", "expected")
        return targets

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        if s.kind == "neighbourhood":
            return self._graph_only(fitted, req, ctx)
        const = fitted.groups.get("constant_data")
        if const is None or CENTER not in const or SCALE not in const:
            raise MapiError(
                f"the bundle of {self.id!r} has no constant_data/{CENTER} and {SCALE}",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if CELL_DIM not in ds.dims:
            raise MapiError(
                f"the score data has no {CELL_DIM!r} dim: send the cells to predict or "
                f"simulate (the values may be missing, one row per {CELL_DIM})",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        labels = ds[VAR_DIM].values if VAR_DIM in ds.coords else const[VAR_DIM].values
        extra = {
            name: ((VAR_DIM,), np.asarray(const[name].sel({VAR_DIM: labels}).values, dtype=float))
            for name in (CENTER, SCALE)
        }
        ds = ds.assign(extra)
        if ctx.mode == "simulate":
            ctx.kind = "predictive"
        return super().score(fitted, ds, req, ctx)

    def _graph_only(self, fitted: Fitted, req: ScoreRequest, ctx: ScoreContext) -> xr.DataTree:
        """The neighbourhood model's score: the graph outputs, straight from the posterior."""
        graph = {"pcor", "edge_prob"}
        wants_cells = bool(set(req.output.vars) - graph) or ctx.kind == "predictive"
        if ctx.mode in ("simulate", "evaluate") or wants_cells:
            raise MapiError(
                f"kind neighbourhood cannot {'score cells' if wants_cells else ctx.mode}: it "
                "conditions on the other variables of each cell. Fit kind dense to predict, "
                "simulate or evaluate held-out cells",
                code=ErrorCode.MODE_NOT_SUPPORTED,
                pointer="/model/settings/kind",
            )
        post = fitted.posterior
        arrays: dict[str, xr.DataArray] = {}
        for name in ("pcor", "edge_prob"):
            da = post[name].rename(None)
            da.attrs = {"kind": "expected", "noise": "none"}
            arrays[name] = da
        pred = xr.Dataset(arrays)
        drop = [c for c in pred.coords if c not in pred.dims]
        return xr.DataTree.from_dict({PREDICTIONS: pred.drop_vars(drop)})
