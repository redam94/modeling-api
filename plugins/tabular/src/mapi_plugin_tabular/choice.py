"""``choice.multinomial_logit``: which product a customer picks on each trip (E84-style).

Canonical inputs (a dense ``hh x trip`` panel; ragged panels leave the cells of trips a
household never made empty)::

    dims  hh: categorical, new_levels (hyperprior)    trip: index, extend
          product: categorical, fixed (the alternatives)
    vars  y(hh, trip): target, str - the chosen product ('' or missing: no trip)
          <covariates>(hh, trip, product): alternative-specific (price, promotion, ...)

The model is a **conditional logit** with household-specific alternative intercepts::

    U[h, t, j] = asc[j] + sum_k beta[k] * x_k[h, t, j] + u[h, j]      (asc, u = 0 for j = ref)
    P(y[h, t] = j) = softmax_j U[h, t, j] over the products available on that trip

A product is *available* on a trip when every covariate is known there (a missing price
means the shelf did not carry it). The household effects ``u[h, j] ~ Normal(0, effect_sd)``
make the model a random-intercept (mixed) logit; it is fitted by penalised maximum
likelihood, which is the posterior mode of that model.

**Uncertainty: the Bayesian bootstrap** (Rubin 1981). Each of ``fit.draws`` draws refits the
model with Dirichlet(1, ..., 1) weights on households - every household stays in every draw
(so each keeps its own effect) and the spread of the draws approximates the posterior of a
flat-prior model. The draws are joint, so the representation is ``draws``: shares,
counterfactual deltas and sums over trips are valid. ``inference: bootstrap``; there is no
pointwise log-likelihood, so LOO is not offered.

**Scoring** (output ``y_hat(chain, draw, hh, trip, product)``): ``expected`` gives choice
probabilities, ``predictive`` one sampled choice per draw (one-hot). Households unseen at
fit draw their effects from ``Normal(0, sd)`` per draw, with ``sd`` the spread of the fitted
effects in that draw (``new_level_strategy: hyperprior``; ``pooled`` uses zero). A cell with
no available product (a trip the household never made) gets probability 0 for every product,
so sums over trips count expected purchases. Price and promotion scenarios are interventions on the
covariates (``counterfactual``), e.g. ``{var: price, sel: {product: coke}, multiply: 1.1}``.

Choice values are matched to product labels after lower-casing and replacing every run of
non-alphanumerics by ``_`` (so ``Coke`` and ``coke`` match the label of ``price_coke``).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, ClassVar, Literal

import numpy as np
import xarray as xr
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
    new_level_mask,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, field_validator
from scipy.optimize import minimize

__all__ = [
    "ALTERNATIVE_DIM",
    "GROUP_DIM",
    "OCCASION_DIM",
    "TARGET",
    "MultinomialLogit",
    "MultinomialLogitSettings",
    "choice_key",
]

GROUP_DIM = "hh"
OCCASION_DIM = "trip"
ALTERNATIVE_DIM = "product"
TARGET = "y"
Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
_KEY = re.compile(r"[^a-z0-9]+")
_DEFAULT_DRAWS = 100
_MAX_DRAWS = 2000


def choice_key(label: Any) -> str:
    """How choice values and product labels are compared (``Coke Zero`` -> ``coke_zero``)."""
    return _KEY.sub("_", str(label).strip().lower()).strip("_")


class MultinomialLogitSettings(ModelSettings):
    """Settings of ``choice.multinomial_logit``."""

    covariates: list[Name] | None = Field(
        None,
        max_length=64,
        description="Alternative-specific variables along product (default: every numeric "
        "variable with the product dim)",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    random_effects: list[Literal["hh"]] = Field(
        default=["hh"],
        max_length=1,
        description="Household-specific product intercepts ([hh]) or none ([])",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    effect_sd: float = Field(
        1.0,
        gt=0,
        le=10,
        description="Prior sd of the household effects (smaller pools households more)",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    coef_sd: float = Field(
        10.0,
        gt=0,
        le=1000,
        description="Prior sd of the product intercepts and coefficients (weak)",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    reference: str | None = Field(
        None,
        max_length=128,
        description="Product whose intercept is fixed at 0 (default: the first label)",
        json_schema_extra={"x-ui-level": "advanced"},
    )
    max_iter: int = Field(500, ge=10, le=10_000, json_schema_extra={"x-ui-level": "advanced"})

    @field_validator("covariates")
    @classmethod
    def _covariates(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            if len(set(v)) != len(v):
                raise ValueError("covariates must be unique")
            if TARGET in v:
                raise ValueError("the target cannot be a covariate")
        return v


SCHEMA_TEMPLATE: dict[str, Any] = {
    "params": {"covariates": {"type": "string_list", "path": "covariates", "default": []}},
    "template": {
        "dims": {
            GROUP_DIM: {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
            OCCASION_DIM: {"kind": "index", "policy": "extend"},
            ALTERNATIVE_DIM: {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            TARGET: {"dims": [GROUP_DIM, OCCASION_DIM], "dtype": "str", "role": "target",
                     "missing": "allow"},
            "{*covariates as v}": {
                "dims": [GROUP_DIM, OCCASION_DIM, ALTERNATIVE_DIM],
                "dtype": "float64",
                "role": "covariate",
                "missing": "allow",
            },
        },
    },
}  # fmt: skip


def _schema_error(message: str, pointer: str = "/data") -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


# ============================================================================ design


class _Design:
    """The flattened choice problem: one row per (hh, trip) cell, one column per product."""

    def __init__(
        self,
        ds: xr.Dataset,
        covariates: list[str],
        products: list[str],
        *,
        with_target: bool,
        is_score: bool = False,
    ) -> None:
        for name in covariates:
            if name not in ds:
                code = ErrorCode.MISSING_FUTURE_COVARIATE if is_score else ErrorCode.SCHEMA_INVALID
                raise MapiError(
                    f"the data has no covariate {name!r}", code=code, pointer=f"/data/{name}"
                )
            if ALTERNATIVE_DIM not in ds[name].dims:
                raise _schema_error(f"covariate {name!r} must vary along {ALTERNATIVE_DIM!r}")
        self.hh = np.asarray(ds[GROUP_DIM].values)
        self.trip = np.asarray(ds[OCCASION_DIM].values)
        nh, nt, nj = len(self.hh), len(self.trip), len(products)
        x = np.zeros((nh, nt, nj, len(covariates)))
        for k, name in enumerate(covariates):
            da = ds[name].broadcast_like(ds[TARGET]) if TARGET in ds else ds[name]
            da = da.transpose(GROUP_DIM, OCCASION_DIM, ALTERNATIVE_DIM)
            x[..., k] = np.asarray(da.sel({ALTERNATIVE_DIM: products}).values, dtype=float)
        self.available: np.ndarray = (
            np.asarray(np.isfinite(x).all(axis=-1), dtype=bool)
            if covariates
            else np.ones((nh, nt, nj), dtype=bool)
        )
        self.x = np.where(np.isfinite(x), x, 0.0)
        self.chosen = np.full((nh, nt), -1, dtype=np.int64)
        if with_target:
            self.chosen = _encode(ds[TARGET].transpose(GROUP_DIM, OCCASION_DIM), products)
            bad = (self.chosen >= 0) & ~np.take_along_axis(
                self.available, np.maximum(self.chosen, 0)[..., None], axis=-1
            )[..., 0]
            if bad.any():
                raise _schema_error(
                    f"{int(bad.sum())} trips chose a product whose covariates are missing on "
                    "that trip (a missing price means the product was not available)",
                    f"/data/{TARGET}",
                )
        self.cells = self.available.any(axis=-1)
        if with_target:
            self.cells &= self.chosen >= 0

    @property
    def shape(self) -> tuple[int, int, int]:
        h, t, j = self.available.shape
        return h, t, j


def _encode(y: xr.DataArray, products: list[str]) -> np.ndarray:
    """Chosen product index per cell (-1: no trip)."""
    lookup = {choice_key(p): i for i, p in enumerate(products)}
    values = np.asarray(y.values, dtype=object)
    out = np.full(values.shape, -1, dtype=np.int64)
    unknown: set[str] = set()
    for idx, v in np.ndenumerate(values):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        key = choice_key(v)
        if not key or key == "nan":
            continue
        code = lookup.get(key)
        if code is None:
            unknown.add(str(v)[:40])
        else:
            out[idx] = code
    if unknown:
        raise MapiError(
            f"{len(unknown)} chosen value(s) are not products "
            f"({', '.join(map(choice_key, products))})",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{TARGET}",
            untrusted_text=", ".join(sorted(unknown)[:10]),
        )
    return out


# ============================================================================ likelihood


class _Problem:
    """Penalised negative log-likelihood and its gradient (vectorised)."""

    def __init__(
        self, d: _Design, ref: int, s: MultinomialLogitSettings, weights: np.ndarray
    ) -> None:
        self.d = d
        self.ref = ref
        self.re = bool(s.random_effects)
        nh, _, nj = d.shape
        self.nj, self.nk, self.nh = nj, d.x.shape[-1], nh
        self.coef_var = s.coef_sd**2
        self.effect_var = s.effect_sd**2
        self.free = np.array([j for j in range(nj) if j != ref], dtype=np.int64)
        cells = d.cells
        self.h_idx, self.t_idx = np.nonzero(cells)
        self.w = weights[self.h_idx]
        self.x = d.x[self.h_idx, self.t_idx]  # (n, j, k)
        self.avail = d.available[self.h_idx, self.t_idx]  # (n, j)
        self.y = d.chosen[self.h_idx, self.t_idx]  # (n,)
        self.size = (nj - 1) + self.nk + (nh * (nj - 1) if self.re else 0)

    def unpack(self, theta: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        nf = self.nj - 1
        asc = np.zeros(self.nj)
        asc[self.free] = theta[:nf]
        beta = theta[nf : nf + self.nk]
        u = np.zeros((self.nh, self.nj))
        if self.re:
            u[:, self.free] = theta[nf + self.nk :].reshape(self.nh, nf)
        return asc, beta, u

    def __call__(self, theta: np.ndarray) -> tuple[float, np.ndarray]:
        asc, beta, u = self.unpack(theta)
        util = asc[None, :] + self.x @ beta + u[self.h_idx]
        util = np.where(self.avail, util, -np.inf)
        m = util.max(axis=1, keepdims=True)
        e = np.where(self.avail, np.exp(util - m), 0.0)
        z = e.sum(axis=1, keepdims=True)
        p = e / z
        logp = util[np.arange(len(self.y)), self.y] - (m[:, 0] + np.log(z[:, 0]))
        nll = -float(np.sum(self.w * logp))
        resid = p.copy()
        resid[np.arange(len(self.y)), self.y] -= 1.0
        resid *= self.w[:, None]
        g_asc = resid.sum(axis=0)
        g_beta = np.einsum("nj,njk->k", resid, self.x) if self.nk else np.zeros(0)
        nf = self.nj - 1
        grad = np.zeros(self.size)
        grad[:nf] = g_asc[self.free] + asc[self.free] / self.coef_var
        grad[nf : nf + self.nk] = g_beta + beta / self.coef_var
        penalty = 0.5 * (np.sum(asc**2) + np.sum(beta**2)) / self.coef_var
        if self.re:
            g_u = np.zeros((self.nh, self.nj))
            np.add.at(g_u, self.h_idx, resid)
            g_u += u / self.effect_var
            grad[nf + self.nk :] = g_u[:, self.free].reshape(-1)
            penalty += 0.5 * np.sum(u**2) / self.effect_var
        return nll + penalty, grad

    def solve(self, start: np.ndarray, max_iter: int) -> tuple[np.ndarray, bool]:
        res = minimize(self, start, jac=True, method="L-BFGS-B", options={"maxiter": max_iter})
        return np.asarray(res.x, dtype=float), bool(res.success)


def _flat_draws(draw_index: np.ndarray | None, post: xr.Dataset, n: int) -> np.ndarray:
    """Flat positions of the bundle's thinning index ``(chain, draw)`` pairs (all draws
    without one)."""
    if draw_index is None or len(draw_index) == 0:
        return np.arange(n)
    pairs = np.asarray(draw_index, dtype=np.int64).reshape(-1, 2)
    n_draw = int(post.sizes["draw"])
    flat = pairs[:, 0] * n_draw + pairs[:, 1]
    return flat[(flat >= 0) & (flat < n)]


def _softmax(util: np.ndarray, avail: np.ndarray) -> np.ndarray:
    util = np.where(avail, util, -np.inf)
    m = np.max(util, axis=-1, keepdims=True)
    m = np.where(np.isfinite(m), m, 0.0)
    e = np.where(avail, np.exp(util - m), 0.0)
    z = e.sum(axis=-1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(z > 0, e / z, 0.0)


# ============================================================================ plugin


class MultinomialLogit(CustomModel[MultinomialLogitSettings]):
    """A conditional logit with household effects, uncertainty by the Bayesian bootstrap."""

    id = "choice.multinomial_logit"
    version = "1.0.0"
    Settings = MultinomialLogitSettings
    description = (
        "Which product customers choose on each shopping trip: a conditional (multinomial) "
        "logit over alternative-specific covariates such as price and promotion, with "
        "household-specific product preferences. Uncertainty comes from the Bayesian "
        "bootstrap over households; new households draw their preferences from the fitted "
        "spread."
    )
    capabilities = Capabilities(
        inference="bootstrap",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate"}),
        loo_valid=False,
        loo_invalid_reason="bootstrap draws have no pointwise log-likelihood",
        deterministic_given_seed=True,
    )
    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [GROUP_DIM, OCCASION_DIM, ALTERNATIVE_DIM],
            "units": "probability",
            "description": "Choice probability of each product per household and trip "
            "(predictive: one sampled choice, one-hot)",
        }
    }
    plots: ClassVar[tuple[str, ...]] = ("core.data_overview", "core.posterior_forest")
    resources = ResourceHint(min_class=ResourceClass.S, default_class=ResourceClass.S,
                             est_fit_seconds=120)  # fmt: skip
    fit_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "mode": {"enum": ["evaluate_then_refit", "train_only"],
                     "default": "evaluate_then_refit", "x-ui-level": "advanced"},
            "draws": {"type": "integer", "minimum": 2, "maximum": _MAX_DRAWS,
                      "default": _DEFAULT_DRAWS, "x-ui-level": "advanced",
                      "description": "Bayesian bootstrap draws"},
            "seed": {"type": "integer", "minimum": 0, "x-ui-level": "advanced"},
        },
    }  # fmt: skip
    pad_extend = False

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _covariates(ds: xr.Dataset, s: MultinomialLogitSettings) -> list[str]:
        if s.covariates is not None:
            return list(s.covariates)
        return sorted(
            str(v)
            for v in ds.data_vars
            if v != TARGET and ALTERNATIVE_DIM in ds[v].dims and ds[v].dtype.kind in "fiub"
        )

    @staticmethod
    def _reference(products: list[str], s: MultinomialLogitSettings) -> int:
        if s.reference is None:
            return 0
        keys = [choice_key(p) for p in products]
        try:
            return keys.index(choice_key(s.reference))
        except ValueError:
            raise MapiError(
                f"reference product {s.reference!r} is not a product",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/reference",
            ) from None

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: MultinomialLogitSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        for dim in (GROUP_DIM, OCCASION_DIM, ALTERNATIVE_DIM):
            if dim not in ds.dims:
                raise _schema_error(f"the data has no {dim!r} dim")
        if TARGET not in ds:
            raise _schema_error(f"the data has no target {TARGET!r}")
        products = [str(p) for p in ds[ALTERNATIVE_DIM].values]
        if len(products) < 2:
            raise _schema_error("a choice needs at least two products", f"/data/{ALTERNATIVE_DIM}")
        covariates = self._covariates(ds, s)
        design = _Design(ds, covariates, products, with_target=True)
        n_cells = int(design.cells.sum())
        if n_cells < 2 * len(products):
            raise _schema_error(
                f"only {n_cells} observed trips; a choice model needs several per product",
                f"/data/{TARGET}",
            )
        ref = self._reference(products, s)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        rng = np.random.default_rng(seed)
        draws = min(cfg.draws or _DEFAULT_DRAWS, _MAX_DRAWS)
        nh = design.shape[0]
        ctx.progress(0.02, "fitting the choice model")
        full = _Problem(design, ref, s, np.ones(nh))
        theta0, ok = full.solve(np.zeros(full.size), s.max_iter)
        asc_d, beta_d, u_d, sd_d = [], [], [], []
        failed = 0 if ok else 1
        for d in range(draws):
            ctx.check_cancelled()
            weights = rng.dirichlet(np.ones(nh)) * nh
            prob = _Problem(design, ref, s, weights)
            theta, ok = prob.solve(theta0, s.max_iter)
            failed += 0 if ok else 1
            asc, beta, u = prob.unpack(theta)
            asc_d.append(asc)
            beta_d.append(beta)
            u_d.append(u)
            free = [j for j in range(len(products)) if j != ref]
            sd_d.append(float(np.std(u[:, free])) if s.random_effects and free else 0.0)
            ctx.progress(0.05 + 0.9 * (d + 1) / draws, f"bootstrap draw {d + 1} of {draws}")
        coords = {"chain": [0], "draw": np.arange(draws), ALTERNATIVE_DIM: products}
        post = xr.Dataset(
            {
                "asc": (("chain", "draw", ALTERNATIVE_DIM), np.asarray(asc_d)[None]),
                "effect_sd": (("chain", "draw"), np.asarray(sd_d)[None]),
            },
            coords=coords,
        )
        if covariates:
            post["beta"] = xr.DataArray(
                np.asarray(beta_d)[None],
                dims=("chain", "draw", "covariate"),
                coords={"covariate": covariates},
            )
        if s.random_effects:
            post["u"] = xr.DataArray(
                np.asarray(u_d)[None],
                dims=("chain", "draw", GROUP_DIM, ALTERNATIVE_DIM),
                coords={GROUP_DIM: [str(h) for h in design.hh]},
            )
        post.attrs = {"inference": "bayesian_bootstrap", "reference": products[ref]}
        asc0, beta0, u0 = full.unpack(theta0)
        util = asc0 + design.x @ beta0 + u0[:, None, :]
        p = _softmax(util, design.available)
        cells = design.cells
        hit = float(np.mean(np.argmax(np.nan_to_num(p[cells], nan=-1.0), axis=-1)
                            == design.chosen[cells]))  # fmt: skip
        diags = [
            Diagnostic(
                code="CHOICE_HIT_RATE",
                severity="info",
                value=round(hit, 6),
                message_expert=f"in-sample hit rate {hit:.3f} over {n_cells} trips "
                f"({len(products)} products, {len(covariates)} covariates)",
                message_plain=f"The most probable product of the model was the product that "
                f"the customer bought on {hit:.0%} of the training trips.",
            )
        ]
        if failed:
            diags.append(
                Diagnostic(
                    code="CHOICE_NOT_CONVERGED",
                    severity="warning",
                    value=float(failed),
                    message_expert=f"L-BFGS-B stopped before convergence in {failed} of "
                    f"{draws + 1} fits",
                    message_plain="Some refits did not converge fully. The results can have a "
                    "little more noise than they show.",
                    suggested_action="Increase model.settings.max_iter, or look for products "
                    "that customers almost never select.",
                )
            )
        ctx.progress(1.0, "fit complete")
        learned: dict[str, Any] = {
            "products": products,
            "covariates": covariates,
            "reference": products[ref],
            "households": [str(h) for h in design.hh],
        }
        return FitOutput(groups={"posterior": post}, learned=learned, diagnostics=diags)

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        post = fitted.posterior
        products = [str(p) for p in post[ALTERNATIVE_DIM].values]
        covariates = [str(c) for c in fitted.learned.get("covariates", [])]  # type: ignore[union-attr]
        for dim in (GROUP_DIM, OCCASION_DIM, ALTERNATIVE_DIM):
            if dim not in ds.dims:
                raise _schema_error(f"the score data has no {dim!r} dim")
        have = {str(p) for p in ds[ALTERNATIVE_DIM].values}
        if missing := [p for p in products if p not in have]:
            raise MapiError(
                f"the score data lacks products {missing[:5]} the model was fitted on",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer=f"/data/{ALTERNATIVE_DIM}",
            )
        design = _Design(ds, covariates, products, with_target=False, is_score=True)
        nh, _, nj = design.shape
        asc = np.asarray(post["asc"].values).reshape(-1, nj)  # (draw, j)
        n = asc.shape[0]
        idx = _flat_draws(ctx.draw_index, post, n)
        beta = (
            np.asarray(post["beta"].values).reshape(n, -1)[idx]
            if "beta" in post
            else np.zeros((len(idx), 0))
        )
        asc = asc[idx]
        u = np.zeros((len(idx), nh, nj))
        new = np.asarray(new_level_mask(ds, GROUP_DIM).values, dtype=bool)
        if "u" in post:
            fit_u = np.asarray(post["u"].values).reshape(n, -1, nj)[idx]
            pos = {str(h): i for i, h in enumerate(post[GROUP_DIM].values)}
            for i, h in enumerate(design.hh):
                if not new[i] and str(h) in pos:
                    u[:, i] = fit_u[:, pos[str(h)]]
            if new.any() and ctx.strategy(GROUP_DIM) == "hyperprior":
                sd = np.asarray(post["effect_sd"].values).reshape(-1)[idx]
                ref = products.index(str(post.attrs.get("reference", products[0])))
                noise = ctx.rng.standard_normal((len(idx), int(new.sum()), nj))
                noise[..., ref] = 0.0
                u[:, new] = noise * sd[:, None, None]
        util = asc[:, None, None, :] + np.einsum("htjk,dk->dhtj", design.x, beta) + u[:, :, None, :]
        p = _softmax(util, design.available[None])
        if ctx.kind == "predictive":
            flat = p.reshape(-1, nj)
            cum = np.cumsum(flat, axis=-1)
            draw = ctx.rng.random((flat.shape[0], 1))
            pick = np.minimum((draw > cum).sum(axis=-1), nj - 1)
            one_hot = np.zeros_like(flat)
            one_hot[np.arange(flat.shape[0]), pick] = 1.0
            # a cell without a choice set stays all zero
            has_set = np.asarray(design.available.any(axis=-1), dtype=bool)
            p = np.where(has_set[None, ..., None], one_hot.reshape(p.shape), 0.0)
        y_hat = xr.DataArray(
            p[None],
            dims=("chain", "draw", GROUP_DIM, OCCASION_DIM, ALTERNATIVE_DIM),
            coords={
                "chain": [0],
                "draw": idx,
                GROUP_DIM: ds[GROUP_DIM].values,
                OCCASION_DIM: ds[OCCASION_DIM].values,
                ALTERNATIVE_DIM: products,
            },
            attrs={
                "kind": ctx.kind,
                "units": "probability",
                "noise": "seeded_stream" if ctx.kind == "predictive" else "none",
            },
        )
        pred = xr.Dataset({"y_hat": y_hat})
        pred.attrs = {"units": "probability", "representation": "draws"}
        return xr.DataTree.from_dict({PREDICTIONS: pred})
