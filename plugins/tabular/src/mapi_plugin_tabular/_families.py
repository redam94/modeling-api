"""Likelihood families shared by the tabular GLMs (``tabular.hierarchical_glm`` and
``tabular.declarative_glm``).

Both models build a linear predictor ``eta`` over the row dim ``obs`` and hand it to
:func:`likelihood`, which adds the observed variable ``y`` and the Deterministic ``mu``
(``E[y]``) for one of the ten :data:`Family` values:

==================================  =====  ==============================================
family                              link   likelihood (``mu`` = E[y])
==================================  =====  ==============================================
``normal``                          id     Normal(mu, sigma)
``student_t``                       id     StudentT(nu, mu, sigma), nu ~ Gamma(2, 0.1)
``poisson``                         log    Poisson(exp(eta))
``bernoulli``                       logit  Bernoulli(sigmoid(eta))
``negative_binomial``               log    NB(mu = exp(eta), alpha)
``zero_inflated_poisson``           log    ZIP(psi, exp(eta)), psi ~ Beta(2, 2); mu = psi exp(eta)
``zero_inflated_negative_binomial`` log    ZINB(psi, exp(eta), alpha); mu = psi exp(eta)
``beta``                            logit  Beta(mu kappa, (1 - mu) kappa), kappa ~ HalfNormal(50)
``gamma``                           log    Gamma(alpha, alpha / mu), alpha ~ HalfNormal(5)
``ordinal``                         cum.   OrderedLogistic(eta, cutpoints); mu = sum_k k p_k
==================================  =====  ==============================================

The noise scale (``sigma`` of the normal and Student-t families, ``alpha`` of the negative
binomial ones) comes from a :data:`ScalePrior` the model passes in: ``scale(name, factor)``
creates the positive variable ``name`` with its prior scaled by ``factor`` (the negative
binomial dispersion uses ``factor=5``). The other family parameters keep fixed priors.

The code moved here from ``glm.py`` unchanged (v0.18); ``hierarchical_glm`` builds exactly
the same graph as before.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final, Literal, get_args

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "COUNT_FAMILIES",
    "FAMILIES",
    "OBS_DIM",
    "TARGET",
    "Family",
    "ScalePrior",
    "check_target",
    "half_normal_scale",
    "likelihood",
    "stack_columns",
]

OBS_DIM: Final = "obs"
TARGET: Final = "y"
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
FAMILIES: Final[tuple[str, ...]] = get_args(Family)
COUNT_FAMILIES: Final = frozenset(
    {"negative_binomial", "zero_inflated_poisson", "zero_inflated_negative_binomial"}
)

ScalePrior = Callable[[str, float], Any]
"""``scale(name, factor)`` -> a positive random variable named ``name``."""


def half_normal_scale(sigma: float) -> ScalePrior:
    """The ``hierarchical_glm`` noise prior: ``HalfNormal(sigma * factor)``."""

    def make(name: str, factor: float) -> Any:
        return pm.HalfNormal(name, sigma if factor == 1 else sigma * factor)

    return make


def likelihood(
    eta: Any,
    family: str,
    ctx: BuildContext,
    *,
    scale: ScalePrior,
    n_categories: int | None = None,
) -> None:
    """Add ``mu`` and the observed ``y`` (over ``obs``) of ``family`` to the current model."""
    y = ctx.observed(TARGET, dims=[OBS_DIM])
    if ctx.phase == "fit":
        check_target(y, family, n_categories)
    match family:
        case "normal":
            mu = pm.Deterministic("mu", eta, dims=OBS_DIM)
            sigma = scale("sigma", 1)
            pm.Normal(TARGET, mu, sigma, observed=y, dims=OBS_DIM)
        case "student_t":
            mu = pm.Deterministic("mu", eta, dims=OBS_DIM)
            sigma = scale("sigma", 1)
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
            alpha = scale("alpha", 5)
            pm.NegativeBinomial(TARGET, mu=mu, alpha=alpha, observed=y, dims=OBS_DIM)
        case "zero_inflated_poisson":
            psi = pm.Beta("psi", 2.0, 2.0)
            rate = pt.exp(eta)
            pm.Deterministic("mu", psi * rate, dims=OBS_DIM)
            pm.ZeroInflatedPoisson(TARGET, psi=psi, mu=rate, observed=y, dims=OBS_DIM)
        case "zero_inflated_negative_binomial":
            psi = pm.Beta("psi", 2.0, 2.0)
            alpha = scale("alpha", 5)
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
            assert n_categories is not None
            k = n_categories
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
            pm.Potential("cutpoints_prior", (((cutpoints - grid) ** 2 - cutpoints**2) / 8).sum())
            # E[y] = sum_k k p_k = sum_j P(y > j) = sum_j sigmoid(eta - c_j)
            pm.Deterministic(
                "mu",
                pm.math.sigmoid(eta[:, None] - cutpoints[None, :]).sum(axis=1),
                dims=OBS_DIM,
            )
            pm.OrderedLogistic(TARGET, eta=eta, cutpoints=cutpoints, observed=y, dims=OBS_DIM)
        case _:  # pragma: no cover - settings validation keeps the family closed
            raise MapiError(f"unknown family {family!r}", code=ErrorCode.SETTINGS_INVALID)


def check_target(y: np.ndarray, family: str, n_categories: int | None) -> None:
    """Refuse targets the family cannot describe (NaN cells are left to the model)."""
    v = y[np.isfinite(y)]
    message: str | None = None
    if family in COUNT_FAMILIES:
        if (v < 0).any() or (v != np.round(v)).any():
            message = f"the {family} family needs counts: whole numbers of 0 or more"
    elif family == "beta":
        if ((v <= 0) | (v >= 1)).any():
            message = "the beta family needs values between 0 and 1 (0 and 1 not included)"
    elif family == "gamma":
        if (v <= 0).any():
            message = "the gamma family needs values above 0"
    elif family == "ordinal":
        k = n_categories or 0
        if ((v < 0) | (v > k - 1) | (v != np.round(v))).any():
            message = (
                f"the ordinal family needs whole numbers from 0 to {k - 1} (n_categories is {k})"
            )
    if message is not None:
        raise MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=f"/data/{TARGET}")


def stack_columns(ds: xr.Dataset, names: list[str], dim: str, ctx: BuildContext) -> np.ndarray:
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
