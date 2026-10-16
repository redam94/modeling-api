"""The settings of ``mmm.bayesinsight``: BayesInsight's ``model_def.json``, typed (Q8, M3).

The built-in plugin ``mmm.bayesinsight`` (``mapi_plugin_mmm.bayesinsight``) fits a
BayesInsight model exactly as its ``model_def.json`` describes it. This module is the
contract between the plugin, the control plane (static validation of a spec's settings)
and the importer (:func:`mapi_spec.bayesinsight.import_bayesinsight` with
``target: mmm.bayesinsight``): pure Pydantic, no numpy.

The model (``panel`` = the model's row ids besides ``Period``, in BayesInsight's order
``Geography, Product, Outlet, Campaign, Creative``; ``t`` the period)::

    eta[p, t] = intercept[p] + sum_media coef_m[p] * sum_l w_m[l] * f_m(z_m[p, t - l])
                + sum_controls coef_c[p] * x_c[p, t]
                + sum_seasons sum_k coef_sk[p] * fourier_k(t)
                + sum_trends  B(t) @ beta_trend[p]
    y[p, t] ~ Likelihood(mean(eta[p, t]))

* **Likelihoods** (``exog.likelihood``): ``Normal`` and ``LogNormal`` use ``eta`` itself
  (identity: ``y ~ Normal(eta, sigma)``, ``log y ~ Normal(eta, sigma)``); ``Poisson``,
  ``NegativeBinomial``, ``ZeroInflatedPoisson`` and ``ZeroInflatedNegativeBinomial`` use a
  log link, ``exp(clip(eta, -20, 20))``. Without a ``likelihood``, BayesInsight - and so this
  plugin - fits a **Poisson** model. ``MvNormal`` is not supported.
* **Coefficients in KPI units**: every coefficient prior (``coeff_prior``, the KPI's
  ``intercept_prior``) is a distribution with its parameters, for the KPI exactly as stored
  (BayesInsight does not rescale the KPI). ``fixed_ind_coeff_dims`` estimate one coefficient
  per level combination of those row ids; ``random_coeff_dims`` add a partially pooled
  effect ``sigma * z`` (``sigma ~ HalfNormal(partial_pooling_sigma)``, ``z ~ Normal(0, 1)``
  per level combination) when ``partial_pooling_sigma > 0`` - BayesInsight's default of 0
  pools nothing.
* **Sign constraints** (``sign: positive | negative``; media are always positive): the
  coefficient keeps the sign - a real-valued prior is truncated at zero, a positive prior is
  negated for ``negative``, and random effects become multiplicative (``coef * exp(sigma *
  z)``) so no level can cross zero. BayesInsight declares signs but does not enforce them.
* **Media**: ``z = transform(x) / index`` with ``index`` each series' non-zero median (or
  mean, ``index_to``) over the fit window; the curve ``f`` is ``hill`` (``z^n / (K^n +
  z^n)``, ``K ~ LogNormal(log K_ave, log(K_std / K_ave + 1))``, ``n = 1 + Exponential(1 /
  (n_ave - 1))``), ``linear`` (``z``) or ``sshaped`` (BayesInsight's ``SShaped`` prior:
  ``(b^(alpha^(100 x / x_max)) - b) / (1 - b)`` with ``b = beta / 1e8``, ``x_max`` the
  series' largest fit-window value, anchored so no spend gives no effect). The curve comes
  first, then the carry-over over ``l_max`` lags (BayesInsight's order): ``delayed``
  (``alpha^((l - theta)^2)``, ``alpha ~ Beta(mu=retention_rate_mean, nu=retention_rate_std)``,
  ``theta ~ Exponential(lam=lag_max)``), ``geometric`` (``alpha^l``, the same retention
  prior), ``weibull`` (pymc-marketing's PDF or CDF weights, :class:`WeibullAdstockPrior`)
  or ``none``; weights are normalised to sum to one.
* **Controls**: ``x = normalize(transform(x))`` with the deterministic transforms
  ``linear | log | exp(alpha) | indexp(alpha, mean) | power(alpha)`` and ``Global
  Standardize`` (mean and sample sd over every cell of the fit window, or the stored
  ``mean``/``std``).
* **Seasons**: ``n_fourier`` cos/sin pairs of period ``period`` (in periods), computed as
  BayesInsight does (``t`` spans ``2 pi n / period`` over the ``n`` fit periods); one
  coefficient per component (``coeff_prior``, default ``Normal(0, 3)``; pooling sigma 1).
* **Local trends** (``localtrend``): a B-spline basis (``num_knots`` interior knots, degree
  ``order``) over the fit window scaled to ``[0, 1]`` with Gaussian-random-walk
  coefficients (:class:`LocalTrendPrior`), per level of ``random_coeff_dims`` around a
  shared walk, optionally grouped (``grouping_map``). Forecasts hold the trend at its last
  fitted level (splines do not extrapolate).

Settings besides ``model_def`` tie BayesInsight's names to the model-stage data: the KPI
is the canonical target ``y``; ``variables`` maps every analytic variable name
(``TV_Total_Total_Total``) to its model-stage variable; ``media`` and ``controls`` list
those in ``model_def`` order (the schema template reads them); ``row_dims`` maps the row
ids to the panel dims. Media variables arrive as lag tensors (``core.lag_tensor`` with
``pad: zero``, BayesInsight's convolution) over ``lag_dim`` with ``l_max`` lags.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "BAYESINSIGHT_LIKELIHOODS",
    "BAYESINSIGHT_ROW_IDS",
    "COEFF_DISTRIBUTIONS",
    "RESERVED_NAMES",
    "AdstockPrior",
    "BIControl",
    "BIExog",
    "BILocalTrend",
    "BIMedia",
    "BISeason",
    "BIVariable",
    "BayesInsightModel",
    "BayesInsightSettings",
    "CoeffPrior",
    "DelayedAdstockPrior",
    "DeterministicTransform",
    "HillPrior",
    "Likelihood",
    "LocalTrendPrior",
    "MediaPrior",
    "RowId",
    "SShapedPrior",
    "WeibullAdstockPrior",
    "coeff_param_problem",
]

RowId = Literal["Geography", "Product", "Outlet", "Campaign", "Creative"]
BAYESINSIGHT_ROW_IDS: Final[tuple[str, ...]] = (
    "Geography",
    "Product",
    "Outlet",
    "Campaign",
    "Creative",
)
"""The MFF key columns a model can be indexed by, in BayesInsight's order."""
BAYESINSIGHT_LIKELIHOODS: Final[tuple[str, ...]] = (
    "Normal",
    "LogNormal",
    "Poisson",
    "NegativeBinomial",
    "ZeroInflatedPoisson",
    "ZeroInflatedNegativeBinomial",
)
"""The supported ``likelihood.type`` values (BayesInsight's, without ``MvNormal``)."""

_IDENT = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
Ident = Annotated[str, Field(pattern=_IDENT)]
ADVANCED: Final[dict[str, Any]] = {"x-ui-level": "advanced"}
BASIC: Final[dict[str, Any]] = {"x-ui-level": "basic"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


# ==================================================================================== priors

COEFF_DISTRIBUTIONS: Final[dict[str, tuple[frozenset[str], ...]]] = {
    # distribution: the accepted parameter sets (PyMC argument names)
    "Normal": (frozenset({"mu", "sigma"}),),
    "Laplace": (frozenset({"mu", "b"}),),
    "StudentT": (frozenset({"nu", "mu", "sigma"}),),
    "Uniform": (frozenset({"lower", "upper"}),),
    "HalfNormal": (frozenset({"sigma"}),),
    "HalfCauchy": (frozenset({"beta"}),),
    "Exponential": (frozenset({"lam"}), frozenset({"scale"})),
    "LogNormal": (frozenset({"mu", "sigma"}),),
    "Gamma": (frozenset({"alpha", "beta"}), frozenset({"mu", "sigma"})),
    "Beta": (frozenset({"alpha", "beta"}), frozenset({"mu", "sigma"}), frozenset({"mu", "nu"})),
}
"""Coefficient priors the plugin builds (BayesInsight's continuous univariate families plus
``StudentT`` and ``Gamma``), with their parameter sets."""
POSITIVE_DISTRIBUTIONS: Final = frozenset(
    {"HalfNormal", "HalfCauchy", "Exponential", "LogNormal", "Gamma", "Beta"}
)
_SCALES: Final = frozenset({"sigma", "b", "beta", "lam", "scale", "alpha", "nu"})


def coeff_param_problem(dist: str, params: dict[str, float]) -> str | None:
    """Why ``dist(**params)`` is not a valid coefficient prior (``None`` when it is)."""
    sets = COEFF_DISTRIBUTIONS.get(dist)
    if sets is None:
        known = ", ".join(sorted(COEFF_DISTRIBUTIONS))
        return f"{dist!r} is not a supported coefficient distribution ({known})"
    if frozenset(params) not in sets:
        options = " or ".join(str(sorted(s)) for s in sets)
        return f"{dist} needs the parameters {options}, got {sorted(params)}"
    for key, value in params.items():
        if not math.isfinite(value):
            return f"{dist}: {key} must be finite"
        if key in _SCALES and value <= 0 and not (dist in ("Normal", "Laplace") and key == "mu"):
            return f"{dist}: {key} must be > 0"
    if dist == "Uniform" and not params["lower"] < params["upper"]:
        return "Uniform: lower must be below upper"
    if dist == "Beta" and "mu" in params:
        mu = params["mu"]
        if not 0 < mu < 1:
            return "Beta: mu must lie in (0, 1)"
        if "sigma" in params and not params["sigma"] ** 2 < mu * (1 - mu):
            return "Beta: sigma^2 must be below mu (1 - mu)"
    return None


class CoeffPrior(_Model):
    """A coefficient prior: a distribution and its parameters (BayesInsight's
    ``coeff_dist``/``coeff_params``; ``coef_params`` is accepted as a misspelling)."""

    coeff_dist: str = Field(..., max_length=32)
    coeff_params: dict[str, float] = Field(..., max_length=4)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict) and "coef_params" in data and "coeff_params" not in data:
            data = {**data, "coeff_params": data["coef_params"]}
            data.pop("coef_params")
        return data

    @model_validator(mode="after")
    def _check(self) -> CoeffPrior:
        problem = coeff_param_problem(self.coeff_dist, dict(self.coeff_params))
        if problem:
            raise ValueError(problem)
        return self

    @property
    def positive(self) -> bool:
        return self.coeff_dist in POSITIVE_DISTRIBUTIONS


def _normal(mu: float, sigma: float) -> CoeffPrior:
    return CoeffPrior(coeff_dist="Normal", coeff_params={"mu": mu, "sigma": sigma})


MEDIA_COEFF_DEFAULT: Final = CoeffPrior(
    coeff_dist="LogNormal", coeff_params={"mu": math.log(0.05), "sigma": math.log(1.3)}
)
"""BayesInsight's ``MediaCoeffPrior`` default (KPI units per unit of transformed media)."""


class DelayedAdstockPrior(_Model):
    """``alpha ~ Beta(mu=retention_rate_mean, nu=retention_rate_std)`` and ``theta ~
    Exponential(lam=lag_max)`` (BayesInsight's ``DelayedAdStockPrior``; ``lag_min`` is kept
    but unused, as in BayesInsight). Geometric adstock uses the retention prior alone."""

    type: Literal["Delayed"] = "Delayed"
    retention_rate_mean: float = Field(0.2, gt=0, lt=1)
    retention_rate_std: float = Field(10.0, gt=0)
    lag_min: float = Field(1e-4, gt=0)
    lag_max: float = Field(3.0, gt=0)


class WeibullAdstockPrior(_Model):
    """Weibull carry-over (pymc-marketing's ``WeibullPDFAdstock``/``WeibullCDFAdstock``,
    which BayesInsight copies but cannot call): scale ``lam ~ Gamma(mu=lam_mean,
    sigma=lam_std)`` and shape ``k ~ Gamma(mu=k_mean, sigma=k_std)`` (pymc-marketing's
    defaults per kind). ``PDF`` weights are the density at lags 1..l_max scaled to [0, 1];
    ``CDF`` weights the survival products starting at 1."""

    type: Literal["Weibull"] = "Weibull"
    kind: Literal["PDF", "CDF"] = "PDF"
    lam_mean: float | None = Field(None, gt=0)
    lam_std: float | None = Field(None, gt=0)
    k_mean: float | None = Field(None, gt=0)
    k_std: float | None = Field(None, gt=0)

    def resolved(self) -> tuple[float, float, float, float]:
        """``(lam_mean, lam_std, k_mean, k_std)`` with the kind's defaults."""
        d = (2.0, 1.0, 3.0, 1.0) if self.kind == "PDF" else (2.0, 2.5, 2.0, 2.5)
        got = (self.lam_mean, self.lam_std, self.k_mean, self.k_std)
        lm, ls, km, ks = (g if g is not None else v for g, v in zip(got, d, strict=True))
        return float(lm), float(ls), float(km), float(ks)


AdstockPrior = Annotated[DelayedAdstockPrior | WeibullAdstockPrior, Field(discriminator="type")]


class HillPrior(_Model):
    """``K ~ LogNormal(log K_ave, log(K_std / K_ave + 1))`` and ``n = 1 + Exponential(1 /
    (n_ave - 1))`` (BayesInsight's ``HillPrior``; ``n_std`` is kept but unused, as in
    BayesInsight, and ``n_ave`` must exceed 1)."""

    type: Literal["Hill"] = "Hill"
    K_ave: float = Field(0.85, gt=0)
    K_std: float = Field(0.6, gt=0)
    n_ave: float = Field(1.5, gt=1)
    n_std: float = Field(1.2, gt=0)


class SShapedPrior(_Model):
    """``alpha ~ Beta(mu=alpha_ave, sigma=alpha_std)`` and ``beta = 10^(8 beta_)`` with
    ``beta_ ~ Beta(mu=log10(beta_ave) / 8, sigma=log10(beta_std) / 8)`` (BayesInsight's
    ``SShapedPrior``)."""

    type: Literal["SShaped"] = "SShaped"
    alpha_ave: float = Field(0.88, gt=0, lt=1)
    alpha_std: float = Field(0.03, gt=0)
    beta_ave: float = Field(100.0, gt=1, lt=1e8)
    beta_std: float = Field(1000.0, gt=1)

    @model_validator(mode="after")
    def _beta_moments(self) -> SShapedPrior:
        if not self.alpha_std**2 < self.alpha_ave * (1 - self.alpha_ave):
            raise ValueError("alpha_std^2 must be below alpha_ave (1 - alpha_ave)")
        mu, sd = self.beta_moments()
        if not (0 < mu < 1 and sd**2 < mu * (1 - mu)):
            raise ValueError("beta_ave and beta_std do not give a valid Beta prior")
        return self

    def beta_moments(self) -> tuple[float, float]:
        return math.log10(self.beta_ave) / 8, math.log10(self.beta_std) / 8


MediaPrior = Annotated[HillPrior | SShapedPrior, Field(discriminator="type")]


class LocalTrendPrior(_Model):
    """BayesInsight's ``LocalTrendPrior`` (``variability``: the random walk's step scale,
    ``group_variablility`` - spelled as in BayesInsight - the spread of levels around the
    shared walk, ``partial_pooling`` the spread within a group, ``initial_dist_var`` the sd
    of the walk's first value)."""

    type: Literal["LocalTrend"] = "LocalTrend"
    variability: float = Field(0.2, gt=0)
    group_variablility: float = Field(0.01, gt=0)
    partial_pooling: float = Field(0.01, gt=0)
    initial_dist_var: float = Field(1.0, gt=0)


# ================================================================================ transforms

_TRANSFORM_PARAMS: Final[dict[str, frozenset[str]]] = {
    "linear": frozenset(),
    "log": frozenset(),
    "exp": frozenset({"alpha"}),
    "indexp": frozenset({"alpha", "mean"}),
    "power": frozenset({"alpha"}),
}


class DeterministicTransform(_Model):
    """A transform with known parameters: ``linear`` ``x``, ``log`` ``log(x)``, ``exp``
    ``1 - exp(-x / alpha)``, ``indexp`` ``1 - exp(-x / mean * alpha)``, ``power`` ``x^alpha``
    (BayesInsight's ``DeterministicTransform``)."""

    functional_form: Literal["linear", "log", "exp", "indexp", "power"] = "linear"
    params: dict[str, float] | None = None

    @model_validator(mode="after")
    def _params(self) -> DeterministicTransform:
        want = _TRANSFORM_PARAMS[self.functional_form]
        got = frozenset(self.params or {})
        if got != want:
            raise ValueError(
                f"the {self.functional_form} transform takes the parameters {sorted(want)}, "
                f"got {sorted(got)}"
            )
        for k, v in (self.params or {}).items():
            if not math.isfinite(v) or (
                k == "alpha" and self.functional_form != "power" and v == 0
            ):
                raise ValueError(f"{self.functional_form}: {k} must be finite and non-zero")
        return self


Normalization = Literal["none", "Global Standardize"]


def _null_time_transform(v: Any) -> None:
    if v is not None:
        raise ValueError("time transforms (moving averages) are not implemented")
    return None


# ================================================================================= variables


class _Variable(_Model):
    variable_name: str = Field(..., min_length=1, max_length=512)
    deterministic_transform: DeterministicTransform = DeterministicTransform()
    normalization: Normalization = "none"
    std: float | None = Field(None, gt=0)
    mean: float | None = None
    time_transform: None = None
    partial_pooling_sigma: float = Field(0.0, ge=0)

    @field_validator("time_transform", mode="before")
    @classmethod
    def _no_time_transform(cls, v: Any) -> None:
        return _null_time_transform(v)

    @property
    def coeff_dims(self) -> tuple[list[str], list[str]]:
        """``(fixed, random)`` row ids of the variable's coefficient (empty lists if none)."""
        fixed = list(getattr(self, "fixed_ind_coeff_dims", None) or [])
        random = list(getattr(self, "random_coeff_dims", None) or [])
        return fixed, random


def _orthogonal(fixed: Sequence[str] | None, random: Sequence[str] | None) -> None:
    for name, dims in (("fixed_ind_coeff_dims", fixed), ("random_coeff_dims", random)):
        if dims is not None and len(set(dims)) != len(dims):
            raise ValueError(f"{name} lists a row id twice")
    if fixed and random and set(fixed) & set(random):
        raise ValueError("fixed_ind_coeff_dims and random_coeff_dims must not share row ids")


class BIControl(_Variable):
    """A control variable: ``coef * normalize(transform(x))``."""

    variable_type: Literal["control"] = "control"
    sign: Literal["positive", "negative"] | None = None
    coeff_prior: CoeffPrior = _normal(0.0, 3.0)
    fixed_ind_coeff_dims: list[RowId] | None = None
    random_coeff_dims: list[RowId] | None = None

    @model_validator(mode="after")
    def _dims(self) -> BIControl:
        _orthogonal(self.fixed_ind_coeff_dims, self.random_coeff_dims)
        _check_sign(self.coeff_prior, self.sign)
        return self


def _check_sign(prior: CoeffPrior, sign: str | None) -> None:
    if sign is None or prior.coeff_dist != "Uniform":
        return
    lo, hi = prior.coeff_params["lower"], prior.coeff_params["upper"]
    if (sign == "positive" and hi <= 0) or (sign == "negative" and lo >= 0):
        raise ValueError(f"a Uniform({lo}, {hi}) prior has no {sign} values")


class BIMedia(_Variable):
    """A media variable: ``coef * adstock(curve(transform(x) / index))``."""

    variable_type: Literal["media"] = "media"
    adstock: Literal["delayed", "geometric", "weibull", "none"] = "delayed"
    media_transform: Literal["hill", "linear", "sshaped"] = "hill"
    coeff_prior: CoeffPrior = MEDIA_COEFF_DEFAULT
    fixed_ind_coeff_dims: list[RowId] | None = None
    random_coeff_dims: list[RowId] | None = None
    media_transform_prior: MediaPrior = HillPrior()
    adstock_prior: AdstockPrior = DelayedAdstockPrior()
    sign: Literal["positive"] = "positive"
    index_to: Literal["mean", "median"] = "median"

    @model_validator(mode="before")
    @classmethod
    def _pair_priors(cls, data: Any) -> Any:
        """Pair the curve and carry-over with their priors: an ``SShaped`` prior selects the
        S-shaped curve; ``sshaped``/``weibull`` without their prior get its defaults."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        mp = data.get("media_transform_prior")
        mp_type = mp.get("type") if isinstance(mp, dict) else getattr(mp, "type", None)
        if mp_type == "SShaped":
            data["media_transform"] = "sshaped"
        elif data.get("media_transform") == "sshaped":
            data["media_transform_prior"] = {"type": "SShaped"}
        ap = data.get("adstock_prior")
        ap_type = ap.get("type") if isinstance(ap, dict) else getattr(ap, "type", None)
        if data.get("adstock") == "weibull" and ap_type != "Weibull":
            data["adstock_prior"] = {"type": "Weibull"}
        elif ap_type == "Weibull" and data.get("adstock") in (None, "delayed"):
            data["adstock"] = "weibull"
        return data

    @model_validator(mode="after")
    def _dims(self) -> BIMedia:
        _orthogonal(self.fixed_ind_coeff_dims, self.random_coeff_dims)
        _check_sign(self.coeff_prior, self.sign)
        return self


class Likelihood(_Model):
    """The KPI's likelihood. ``likelihood_params`` parametrise the dispersion prior:
    ``sigma ~ HalfNormal(**params)`` (Normal, LogNormal; default ``sigma=1``), ``alpha ~
    Gamma(**params)`` (negative binomials; default ``alpha=2, beta=0.001``) or ``psi ~
    Beta(**params)`` (zero-inflated Poisson; default ``alpha=1, beta=1``). The
    zero-inflated negative binomial takes ``alpha`` from the params and ``psi ~ Beta(1,
    1)``. ``dispersion_dims`` give ``sigma``/``alpha`` one value per level of those row ids."""

    type: Literal[
        "Normal",
        "LogNormal",
        "Poisson",
        "NegativeBinomial",
        "ZeroInflatedPoisson",
        "ZeroInflatedNegativeBinomial",
    ]
    likelihood_params: dict[str, float] | None = Field(None, max_length=4)
    dispersion_dims: list[RowId] | None = None

    @property
    def link(self) -> Literal["identity", "log"]:
        return "identity" if self.type in ("Normal", "LogNormal") else "log"

    @property
    def dispersion(self) -> tuple[str, str, dict[str, float]] | None:
        """``(parameter, distribution, kwargs)`` of the dispersion prior (``None``: Poisson)."""
        params = dict(self.likelihood_params or {})
        if self.type in ("Normal", "LogNormal"):
            return "sigma", "HalfNormal", params or {"sigma": 1.0}
        if self.type in ("NegativeBinomial", "ZeroInflatedNegativeBinomial"):
            return "alpha", "Gamma", params or {"alpha": 2.0, "beta": 1.0 / 1000}
        if self.type == "ZeroInflatedPoisson":
            return "psi", "Beta", params or {"alpha": 1.0, "beta": 1.0}
        return None

    @model_validator(mode="after")
    def _check(self) -> Likelihood:
        disp = self.dispersion
        if disp is None:
            if self.likelihood_params:
                raise ValueError("a Poisson likelihood takes no likelihood_params")
            if self.dispersion_dims:
                raise ValueError("a Poisson likelihood has no dispersion to vary by dims")
            return self
        name, dist, params = disp
        problem = coeff_param_problem(dist, params)
        if problem:
            raise ValueError(f"likelihood_params ({name} ~ {dist}): {problem}")
        if self.dispersion_dims and name == "psi":
            raise ValueError("dispersion_dims apply to sigma or alpha, not psi")
        if self.dispersion_dims and len(set(self.dispersion_dims)) != len(self.dispersion_dims):
            raise ValueError("dispersion_dims lists a row id twice")
        return self


class BIExog(_Variable):
    """The KPI (``exog``): the intercept prior, its dims and the likelihood. BayesInsight
    does not transform or normalise the KPI, so neither does the plugin."""

    variable_type: Literal["exog"] = "exog"
    sign: None = None
    intercept_prior: CoeffPrior | None = _normal(0.0, 3.0)
    fixed_ind_coeff_dims: list[RowId] | None = None
    random_coeff_dims: list[RowId] | None = None
    likelihood: Likelihood = Likelihood(type="Poisson")

    @model_validator(mode="after")
    def _dims(self) -> BIExog:
        _orthogonal(self.fixed_ind_coeff_dims, self.random_coeff_dims)
        return self


class BISeason(_Variable):
    """Fourier seasonality: ``n_fourier`` cos/sin pairs of ``period`` (in periods)."""

    variable_type: Literal["season"] = "season"
    n_fourier: int = Field(5, ge=1, le=50)
    period: float = Field(365.25 / 7, gt=0)
    coeff_prior: CoeffPrior = _normal(0.0, 3.0)
    fixed_ind_coeff_dims: list[RowId] | None = None
    random_coeff_dims: list[RowId] | None = None
    partial_pooling_sigma: float = Field(1.0, ge=0)
    sign: None = None

    @model_validator(mode="before")
    @classmethod
    def _defaults(cls, data: Any) -> Any:
        # BayesInsight stores ``null`` for "use the default" on these
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if not (k in _NULL_DEFAULTS and v is None)}
        return data

    @model_validator(mode="after")
    def _dims(self) -> BISeason:
        _orthogonal(self.fixed_ind_coeff_dims, self.random_coeff_dims)
        return self


_NULL_DEFAULTS: Final = frozenset({"n_fourier", "period", "coeff_prior", "partial_pooling_sigma"})


class BILocalTrend(_Variable):
    """A local spline trend (BayesInsight's ``LocalTrendsVariableDetails``)."""

    variable_type: Literal["localtrend"] = "localtrend"
    num_knots: int = Field(6, ge=1, le=100)
    order: int = Field(3, ge=1, le=5)
    random_coeff_dims: list[RowId] | None = Field(default_factory=list)
    llt_prior: LocalTrendPrior = LocalTrendPrior()
    grouping_map: dict[str, list[str]] | None = Field(None, max_length=256)
    grouping_name: Ident | None = None
    sign: None = None

    @model_validator(mode="after")
    def _grouping(self) -> BILocalTrend:
        if self.grouping_map is not None:
            if not self.grouping_name:
                raise ValueError("a grouping_map needs a grouping_name")
            if not self.random_coeff_dims or len(self.random_coeff_dims) != 1:
                raise ValueError("a grouping_map needs exactly one random_coeff_dims row id")
            if not self.grouping_map:
                raise ValueError("grouping_map must name at least one group")
        if self.random_coeff_dims and len(set(self.random_coeff_dims)) != len(
            self.random_coeff_dims
        ):
            raise ValueError("random_coeff_dims lists a row id twice")
        return self


BIVariable = Annotated[
    BIControl | BIMedia | BIExog | BISeason | BILocalTrend,
    Field(discriminator="variable_type"),
]


class BayesInsightModel(_Model):
    """``model_def.json`` as the plugin runs it (``artifact``, ``fitted`` and ``VOF`` are
    local-fit bookkeeping and are dropped)."""

    variable_details: list[BIVariable] = Field(..., min_length=2, max_length=512)

    @model_validator(mode="before")
    @classmethod
    def _bookkeeping(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in ("artifact", "fitted", "VOF", "vof")}
        return data

    @model_validator(mode="after")
    def _check(self) -> BayesInsightModel:
        names = [v.variable_name for v in self.variable_details]
        if len(set(names)) != len(names):
            dup = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"variable names must be unique: {dup[:5]}")
        if sum(isinstance(v, BIExog) for v in self.variable_details) != 1:
            raise ValueError("model_def needs exactly one 'exog' variable (the KPI)")
        if not any(isinstance(v, BIMedia | BIControl) for v in self.variable_details):
            raise ValueError("model_def needs at least one media or control variable")
        return self

    @property
    def kpi(self) -> BIExog:
        return next(v for v in self.variable_details if isinstance(v, BIExog))

    @property
    def media(self) -> list[BIMedia]:
        return [v for v in self.variable_details if isinstance(v, BIMedia)]

    @property
    def controls(self) -> list[BIControl]:
        return [v for v in self.variable_details if isinstance(v, BIControl)]

    @property
    def seasons(self) -> list[BISeason]:
        return [v for v in self.variable_details if isinstance(v, BISeason)]

    @property
    def trends(self) -> list[BILocalTrend]:
        return [v for v in self.variable_details if isinstance(v, BILocalTrend)]


# ================================================================================== settings

RESERVED_NAMES: Final = frozenset(
    {"y", "time", "baseline", "lag", "intercept", "mu", "contributions"}
)
"""Names a media or control variable cannot have (the plugin uses them itself)."""


class BayesInsightSettings(_Model):
    """Settings of ``mmm.bayesinsight`` (see the module doc)."""

    model_def: BayesInsightModel = Field(
        ...,
        description="BayesInsight's model_def.json (variable_details); priors in KPI units",
        json_schema_extra=ADVANCED,
    )
    variables: dict[str, Ident] = Field(
        ...,
        min_length=2,
        max_length=512,
        description="Analytic variable name -> model-stage variable (the KPI is 'y')",
        json_schema_extra=ADVANCED,
    )
    media: list[Ident] = Field(
        default_factory=list,
        max_length=128,
        description="Model-stage media variables (lag tensors), in model_def order",
        json_schema_extra=ADVANCED,
    )
    controls: list[Ident] = Field(
        default_factory=list,
        max_length=256,
        description="Model-stage control variables, in model_def order",
        json_schema_extra=ADVANCED,
    )
    row_dims: dict[RowId, Ident] = Field(
        default_factory=dict,
        description="BayesInsight row id -> panel dim of the model-stage data",
        json_schema_extra=ADVANCED,
    )
    panel: list[Ident] = Field(
        default_factory=list,
        max_length=5,
        description="The panel dims (row_dims in BayesInsight's row order)",
        json_schema_extra=ADVANCED,
    )
    l_max: int = Field(
        12,
        ge=1,
        le=104,
        description="Carry-over lags (the lag tensor's l_max)",
        json_schema_extra=ADVANCED,
    )
    lag_dim: Ident = Field(
        "lag", description="Lag dim of the media tensors", json_schema_extra=ADVANCED
    )

    @field_validator("variables")
    @classmethod
    def _names(cls, v: dict[str, str]) -> dict[str, str]:
        targets = list(v.values())
        if len(set(targets)) != len(targets):
            raise ValueError("two analytic variables map to the same model-stage variable")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> BayesInsightSettings:
        md = self.model_def
        expect_media = [self._var(m.variable_name) for m in md.media]
        expect_controls = [self._var(c.variable_name) for c in md.controls]
        if self.variables.get(md.kpi.variable_name) != "y":
            raise ValueError(f"variables must map the KPI {md.kpi.variable_name!r} to 'y'")
        if self.media != expect_media:
            raise ValueError(f"media must list {expect_media} (model_def order)")
        if self.controls != expect_controls:
            raise ValueError(f"controls must list {expect_controls} (model_def order)")
        named = {v.variable_name for v in md.variable_details}
        if extra := sorted(set(self.variables) - named):
            raise ValueError(f"variables names {extra[:5]} that model_def does not define")
        clash = sorted((set(expect_media) | set(expect_controls)) & RESERVED_NAMES)
        if clash or set(self.panel) & ({"y", "time", self.lag_dim} | set(self.variables.values())):
            raise ValueError(f"reserved or clashing names: {clash or sorted(self.panel)}")
        order = [self.row_dims[r] for r in BAYESINSIGHT_ROW_IDS if r in self.row_dims]
        if self.panel != order:
            raise ValueError(f"panel must list the row_dims in row order: {order}")
        for var in md.variable_details:
            used = set(_dims_of(var))
            if missing := sorted(used - set(self.row_dims)):
                raise ValueError(
                    f"{var.variable_name}: row ids {missing} are not dims of the model "
                    f"(row_dims: {sorted(self.row_dims)})"
                )
        return self

    def _var(self, analytic: str) -> str:
        try:
            return self.variables[analytic]
        except KeyError:
            raise ValueError(f"variables has no entry for {analytic!r}") from None

    def var(self, analytic: str) -> str:
        """The model-stage variable of an analytic name."""
        return self.variables[analytic]


def _dims_of(var: Any) -> list[str]:
    dims = list(getattr(var, "fixed_ind_coeff_dims", None) or [])
    dims += list(getattr(var, "random_coeff_dims", None) or [])
    lik = getattr(var, "likelihood", None)
    if isinstance(lik, Likelihood):
        dims += list(lik.dispersion_dims or [])
    return dims
