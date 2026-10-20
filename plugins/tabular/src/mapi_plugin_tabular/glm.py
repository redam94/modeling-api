"""``tabular.hierarchical_glm``: a partially pooled GLM in long layout (E02 radon style).

Canonical inputs (long layout, plan §3.1; ``{g}`` is ``settings.group_dim``)::

    dims  obs: integer, extend           {g}: categorical, new_levels (hyperprior)
    vars  y(obs): target                 {g}_idx(obs): int64 index into {g}
          <covariates>(obs)              <group_covariates>({g})

The model::

    eta_i = a[g_i] + X_i · beta + Σ_v b_v[g_i] · x_{v,i}
    a[g]  ~ Normal(a0 + U_g · gamma, sigma_a)        (partial pooling; non-centred default)
    b_v[g] ~ Normal(0, sigma_v)                       (varying slopes, optional)
    y_i   ~ family(link^-1(eta_i))

with families ``normal``/``student_t`` (identity link), ``poisson`` (log) and
``bernoulli`` (logit), and (v0.14, model version 1.1.0):

==================================  =====  ==============================================
family                              link   likelihood (``mu`` = E[y])
==================================  =====  ==============================================
``negative_binomial``               log    NB(mu = exp(eta), alpha), alpha ~ HalfNormal
``zero_inflated_poisson``           log    ZIP(psi, exp(eta)), psi ~ Beta(2, 2); mu = psi exp(eta)
``zero_inflated_negative_binomial`` log    ZINB(psi, exp(eta), alpha); mu = psi exp(eta)
``beta``                            logit  Beta(mu kappa, (1 - mu) kappa), kappa ~ HalfNormal(50)
``gamma``                           log    Gamma(alpha, alpha / mu), alpha ~ HalfNormal(5)
``ordinal``                         cum.   OrderedLogistic(eta, cutpoints); mu = sum_k k p_k
==================================  =====  ==============================================

``psi`` is the probability of the count process (PyMC's convention). The ordinal family has
``n_categories`` ordered categories ``0..K-1`` with ``K - 1`` sorted ``Normal(0, 2)``
cutpoints and no population intercept (the cutpoints take its place). The fit checks the
target against the family: counts are non-negative integers, beta values lie strictly
between 0 and 1, gamma values are positive, ordinal values are integers in ``0..K-1``.
Group effects use :meth:`BuildContext.hier_effect`, so **unseen
groups** at score time (``{g}: new_levels``) are drawn from the fitted hyperprior for every
posterior draw; group-level covariates ``U`` of a new group shift its prior mean exactly as
for the fitted groups.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import ModelSettings
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field, model_validator

__all__ = ["OBS_DIM", "TARGET", "HierarchicalGLM", "HierarchicalGLMSettings"]

OBS_DIM = "obs"
TARGET = "y"
Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
Family = Literal[
    "normal",
    "student_t",
    "poisson",
    "bernoulli",
    "negative_binomial",
    "zero_inflated_poisson",
    "zero_inflated_negative_binomial",
    "beta",
    "gamma",
    "ordinal",
]
_COUNT_FAMILIES = frozenset(
    {"negative_binomial", "zero_inflated_poisson", "zero_inflated_negative_binomial"}
)


class HierarchicalGLMSettings(ModelSettings):
    """Settings of ``tabular.hierarchical_glm``."""

    group_dim: Name = Field("county", description="The grouping dim (index var: <dim>_idx)")
    family: Family = Field("normal", description="Likelihood family (its canonical link)")
    covariates: list[Name] = Field(
        default=[], max_length=256, description="Observation-level predictors over obs"
    )
    varying_slopes: list[Name] = Field(
        default=[], max_length=32, description="Covariates whose slope varies by group"
    )
    group_covariates: list[Name] = Field(
        default=[], max_length=64, description="Group-level predictors of the group intercept"
    )
    centered: bool = Field(False, description="Centred group effects (non-centred default)")
    intercept_sigma: float = Field(2.0, gt=0, description="Prior sd of the population intercept")
    group_sigma_scale: float = Field(1.0, gt=0, description="Half-normal scale of group sds")
    beta_sigma: float = Field(1.0, gt=0, description="Prior sd of the population slopes")
    noise_sigma: float = Field(1.0, gt=0, description="Half-normal scale of the noise sd")
    n_categories: int | None = Field(
        None, ge=2, le=50, description="Number of ordered categories (ordinal family only)"
    )

    @model_validator(mode="after")
    def _check(self) -> HierarchicalGLMSettings:
        if self.family == "ordinal" and self.n_categories is None:
            raise ValueError("the ordinal family needs n_categories")
        if self.family != "ordinal" and self.n_categories is not None:
            raise ValueError("n_categories applies to the ordinal family only")
        stray = sorted(set(self.varying_slopes) - set(self.covariates))
        if stray:
            raise ValueError(f"varying_slopes {stray} must also be listed in covariates")
        names = [*self.covariates, *self.group_covariates]
        if len(set(names)) != len(names):
            raise ValueError("covariates and group_covariates must be distinct")
        reserved = {TARGET, f"{self.group_dim}_idx"}
        if reserved & set(names):
            raise ValueError(f"{sorted(reserved & set(names))} cannot be covariates")
        if self.group_dim == OBS_DIM:
            raise ValueError("group_dim must differ from 'obs'")
        return self


_TEMPLATE: dict[str, Any] = {
    "params": {
        "group_dim": {"type": "string", "default": "county"},
        "covariates": {"type": "string_list", "default": []},
        "group_covariates": {"type": "string_list", "default": []},
    },
    "template": {
        "layout": "long",
        "dims": {
            OBS_DIM: {"kind": "integer", "policy": "extend"},
            "{group_dim}": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "hyperprior",
            },
        },
        "vars": {
            TARGET: {"dims": [OBS_DIM], "dtype": "float64", "role": "target"},
            "{group_dim}_idx": {
                "dims": [OBS_DIM],
                "dtype": "int64",
                "role": "index",
                "index_of": "{group_dim}",
            },
            "{*covariates as v}": {"dims": [OBS_DIM], "dtype": "float64", "role": "covariate"},
            "{*group_covariates as v}": {
                "dims": ["{group_dim}"],
                "dtype": "float64",
                "role": "covariate",
            },
        },
    },
}


class HierarchicalGLM(PyMCModel[HierarchicalGLMSettings]):
    """Varying intercepts (and optional slopes) by group, in long layout (plan §5, E02)."""

    id = "tabular.hierarchical_glm"
    version = "1.1.0"
    Settings = HierarchicalGLMSettings
    description = (
        "A hierarchical generalised linear model in long layout: group intercepts (and "
        "optional slopes) partially pooled towards a population mean that may depend on "
        "group-level covariates. Unseen groups are drawn from the fitted hyperprior."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "counterfactual", "evaluate"}),
        deterministic_given_seed=True,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [OBS_DIM],
            "units_of": TARGET,
            "description": "Expected response (mean) or a predictive draw per observation",
        }
    }
    expected: ClassVar[dict[str, str]] = {"y_hat": "mu"}
    predictive: ClassVar[dict[str, str]] = {"y_hat": TARGET}
    plots = ("core.ppc", "core.convergence", "core.posterior_forest")
    default_engine = "pymc"
    pad_extend = False

    def build(self, ds: xr.Dataset, s: HierarchicalGLMSettings, ctx: BuildContext) -> pm.Model:
        g = s.group_dim
        coords = ctx.coords()
        if s.covariates:
            coords["covariate"] = list(s.covariates)
        if s.group_covariates:
            coords["group_covariate"] = list(s.group_covariates)
        with pm.Model(coords=coords) as model:
            idx = ctx.data(f"{g}_idx")
            # ordinal: the cutpoints take the place of the population intercept
            a0: Any = (
                0.0 if s.family == "ordinal" else pm.Normal("intercept", 0.0, s.intercept_sigma)
            )
            a_mu: Any = a0
            if s.group_covariates:
                u = _stack(ds, s.group_covariates, g, ctx)
                u_t = ctx.data("U", u, dims=[g, "group_covariate"])
                gamma = pm.Normal("gamma", 0.0, s.beta_sigma, dims="group_covariate")
                a_mu = a0 + pt.dot(u_t, gamma)
            a = ctx.hier_effect(
                "a", g, mu=a_mu, centered=s.centered, sigma_scale=s.group_sigma_scale
            )
            eta: Any = a[idx]
            if s.covariates:
                x = _stack(ds, s.covariates, OBS_DIM, ctx)
                x_t = ctx.data("X", x, dims=[OBS_DIM, "covariate"])
                beta = pm.Normal("beta", 0.0, s.beta_sigma, dims="covariate")
                eta = eta + pt.dot(x_t, beta)
                for j, v in enumerate(s.covariates):
                    if v in s.varying_slopes:
                        b_v = ctx.hier_effect(
                            f"slope_{v}",
                            g,
                            mu=0.0,
                            centered=s.centered,
                            sigma_scale=s.group_sigma_scale,
                        )
                        eta = eta + b_v[idx] * x_t[:, j]
            self._likelihood(eta, s, ctx)
        return model

    @staticmethod
    def _likelihood(eta: Any, s: HierarchicalGLMSettings, ctx: BuildContext) -> None:
        y = ctx.observed(TARGET, dims=[OBS_DIM])
        if ctx.phase == "fit":
            _check_target(y, s)
        match s.family:
            case "normal":
                mu = pm.Deterministic("mu", eta, dims=OBS_DIM)
                sigma = pm.HalfNormal("sigma", s.noise_sigma)
                pm.Normal(TARGET, mu, sigma, observed=y, dims=OBS_DIM)
            case "student_t":
                mu = pm.Deterministic("mu", eta, dims=OBS_DIM)
                sigma = pm.HalfNormal("sigma", s.noise_sigma)
                nu = pm.Gamma("nu", alpha=2.0, beta=0.1)
                pm.StudentT(TARGET, nu=nu, mu=mu, sigma=sigma, observed=y, dims=OBS_DIM)
            case "poisson":
                mu = pm.Deterministic("mu", pt.exp(eta), dims=OBS_DIM)
                pm.Poisson(TARGET, mu, observed=y, dims=OBS_DIM)
            case "bernoulli":
                mu = pm.Deterministic("mu", pm.math.sigmoid(eta), dims=OBS_DIM)
                pm.Bernoulli(TARGET, p=mu, observed=y, dims=OBS_DIM)
            case "negative_binomial":
                mu = pm.Deterministic("mu", pt.exp(eta), dims=OBS_DIM)
                alpha = pm.HalfNormal("alpha", s.noise_sigma * 5)
                pm.NegativeBinomial(TARGET, mu=mu, alpha=alpha, observed=y, dims=OBS_DIM)
            case "zero_inflated_poisson":
                psi = pm.Beta("psi", 2.0, 2.0)
                rate = pt.exp(eta)
                pm.Deterministic("mu", psi * rate, dims=OBS_DIM)
                pm.ZeroInflatedPoisson(TARGET, psi=psi, mu=rate, observed=y, dims=OBS_DIM)
            case "zero_inflated_negative_binomial":
                psi = pm.Beta("psi", 2.0, 2.0)
                alpha = pm.HalfNormal("alpha", s.noise_sigma * 5)
                rate = pt.exp(eta)
                pm.Deterministic("mu", psi * rate, dims=OBS_DIM)
                pm.ZeroInflatedNegativeBinomial(
                    TARGET, psi=psi, mu=rate, alpha=alpha, observed=y, dims=OBS_DIM
                )
            case "beta":
                mu = pm.Deterministic("mu", pm.math.sigmoid(eta), dims=OBS_DIM)
                kappa = pm.HalfNormal("kappa", 50.0)
                pm.Beta(TARGET, alpha=mu * kappa, beta=(1 - mu) * kappa, observed=y, dims=OBS_DIM)
            case "gamma":
                mu = pm.Deterministic("mu", pt.exp(eta), dims=OBS_DIM)
                shape = pm.HalfNormal("alpha", 5.0)
                pm.Gamma(TARGET, alpha=shape, beta=shape / mu, observed=y, dims=OBS_DIM)
            case "ordinal":
                assert s.n_categories is not None
                k = s.n_categories
                # Sorted Normal(0, 2) cutpoints (ordered transform). The engines refuse an
                # initval, and an ordered vector needs a strictly increasing start, so the
                # Normal is centred on a sorted grid and a potential moves its density back
                # to Normal(0, 2): log N(c | 0, 2) = log N(c | m, 2) + ((c - m)^2 - c^2) / 8.
                grid = np.linspace(-1.0, 1.0, k - 1) if k > 2 else np.zeros(1)
                cutpoints = pm.Normal(
                    "cutpoints",
                    grid,
                    2.0,
                    shape=k - 1,
                    transform=pm.distributions.transforms.ordered,
                )
                pm.Potential(
                    "cutpoints_prior", (((cutpoints - grid) ** 2 - cutpoints**2) / 8).sum()
                )
                # E[y] = sum_k k p_k = sum_j P(y > j) = sum_j sigmoid(eta - c_j)
                pm.Deterministic(
                    "mu",
                    pm.math.sigmoid(eta[:, None] - cutpoints[None, :]).sum(axis=1),
                    dims=OBS_DIM,
                )
                pm.OrderedLogistic(TARGET, eta=eta, cutpoints=cutpoints, observed=y, dims=OBS_DIM)


def _check_target(y: np.ndarray, s: HierarchicalGLMSettings) -> None:
    """Refuse targets the family cannot describe (NaN cells are left to the model)."""
    v = y[np.isfinite(y)]
    message: str | None = None
    if s.family in _COUNT_FAMILIES:
        if (v < 0).any() or (v != np.round(v)).any():
            message = f"the {s.family} family needs counts: whole numbers of 0 or more"
    elif s.family == "beta":
        if ((v <= 0) | (v >= 1)).any():
            message = "the beta family needs values between 0 and 1 (0 and 1 not included)"
    elif s.family == "gamma":
        if (v <= 0).any():
            message = "the gamma family needs values above 0"
    elif s.family == "ordinal":
        k = s.n_categories or 0
        if ((v < 0) | (v > k - 1) | (v != np.round(v))).any():
            message = (
                f"the ordinal family needs whole numbers from 0 to {k - 1} (n_categories is {k})"
            )
    if message is not None:
        raise MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=f"/data/{TARGET}")


def _stack(ds: xr.Dataset, names: list[str], dim: str, ctx: BuildContext) -> np.ndarray:
    """``(len(dim), len(names))`` float matrix of 1-D variables along ``dim`` (in order)."""
    code = ErrorCode.MISSING_FUTURE_COVARIATE if ctx.is_score else ErrorCode.SCHEMA_INVALID
    cols = []
    for name in names:
        if name not in ds:
            raise MapiError(
                f"the {ctx.phase} data has no variable {name!r}", code=code, pointer=f"/data/{name}"
            )
        da = ds[name]
        if da.dims != (dim,):
            raise MapiError(
                f"{name!r} must be one-dimensional along {dim!r}, not {list(da.dims)}",
                code=ErrorCode.SCHEMA_INVALID,
            )
        cols.append(np.asarray(da.values, dtype=float))
    out = np.stack(cols, axis=-1) if cols else np.zeros((ds.sizes[dim], 0))
    bad = [n for n, c in zip(names, cols, strict=True) if np.isnan(c).any()]
    if bad:
        raise MapiError(
            f"covariates {bad} have missing values at {ctx.phase} time; impute them in the "
            "pipeline",
            code=code,
        )
    return out
