"""``tabular.declarative_glm``: the trusted model behind declarative model recipes (v0.18, D3).

Analysts describe a GLM with closed choices only (``mapi_spec.model_recipes`` compiles a
``ModelRecipe{base: declarative}`` into these settings with ``declarative_settings``): an
outcome family, one prior per term from a closed set of distributions, a sign per term,
crossed group intercepts, slopes that vary by one group and two-way interactions. There
are no expressions; this module is the only code that runs.

Canonical inputs (long layout)::

    dims  obs: integer, extend
    vars  y(obs): target            <covariates>(obs): float
          each group g, either      g(obs): a label per row (str or numbers)
                        or          g_idx(obs): int index into the dim g

The model::

    eta_i = b0 + Σ_v beta_v x_{v,i} + Σ_g a_g[g_i] + Σ_v s_v[g(v)_i] x_{v,i}
            + Σ_(u,w) gamma_uw x_{u,i} x_{w,i}
    a_g[l] ~ Normal(0, sigma_g)       s_v[l] ~ Normal(0, sigma_slope_v)   (non-centred)
    y_i    ~ family(link^-1(eta_i))   (the ten families of ``_families``)

**Priors.** ``normal``, ``student_t`` (``nu`` default 3) and ``laplace`` take ``mu`` and
``sigma``; ``half_normal`` and ``exponential`` take ``sigma`` (the scale); ``lognormal``
takes ``mu`` and ``sigma`` on the log scale. **Signs.** With ``sign: positive`` or
``negative`` a ``normal``/``student_t``/``laplace`` prior is truncated at 0 (the free
variable ``b_<var>`` then holds the size of the effect, and ``beta`` carries the sign); the
three positive priors are positive by nature and ``negative`` multiplies them by -1. A sign
constrains the population effect; the group slopes of a varying term are deviations around
it and can cross 0. **Scales.** Group spreads (``group_sigma``, also used for the slopes of
the terms that vary by that group) and the noise must use a positive prior; the negative
binomial dispersion uses the noise prior scaled by 5, as ``tabular.hierarchical_glm`` does.

**Groups.** The model encodes group labels itself (D3 and the WP0 probe (b)): a label
variable on ``obs`` (a long table with a string column), or ``g_idx`` plus the dim ``g``
(what ``core.to_long`` makes of a dense ``(time, group)`` panel). The fit keeps the sorted
labels in ``learned["group_levels"]``; at score time rows are matched by label, so the
order of the rows and of the dim does not matter, and labels the fit did not see are drawn
from the fitted group spread for every posterior draw (``hyperprior``).

Outputs: ``y_hat`` (``expected`` = ``mu``, ``predictive`` = ``y``); derived
``term_effects`` (on demand): the effect of each term and interaction per row on the
scale of the linear predictor.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any, ClassVar, Final, Literal

import numpy as np
import pandas as pd
import pymc as pm
import pytensor.tensor as pt
import scipy.stats
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.about import About, Difficulty, TypicalRuntime
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mapi_plugin_tabular._families import (
    OBS_DIM,
    TARGET,
    Family,
    ScalePrior,
    likelihood,
    stack_columns,
)

__all__ = [
    "DEFAULT_NU",
    "DeclarativeGLM",
    "DeclarativeGLMSettings",
    "InteractionPrior",
    "Prior",
    "TermPrior",
]

Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
PriorDist = Literal["normal", "student_t", "laplace", "half_normal", "exponential", "lognormal"]
Sign = Literal["any", "positive", "negative"]

DEFAULT_NU: Final = 3.0
"""Degrees of freedom of a ``student_t`` prior that sets no ``nu``."""
POSITIVE_DISTS: Final = frozenset({"half_normal", "exponential", "lognormal"})
MIN_SIGN_MASS: Final = 1e-9
"""A signed prior must keep at least this much of its weight on the allowed side of 0."""
LEVELS_KEY: Final = "group_levels"
"""``learned`` key of the fit's group labels: ``{group: [label, ...]}`` (fit order)."""
TERM_EFFECTS: Final = "term_effects"
_RESERVED: Final = frozenset(
    {
        OBS_DIM,
        TARGET,
        "X",
        "mu",
        "beta",
        "beta_interaction",
        "intercept",
        "covariate",
        "interaction",
        "term",
        "sample",
        "sigma",
        "alpha",
        "psi",
        "kappa",
        "nu",
        "cutpoints",
    }
)
"""Names a covariate or group cannot take (model variables, coords and dims)."""


class Prior(BaseModel):
    """A prior from the closed set of :data:`PriorDist` with numeric parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dist: PriorDist = Field("normal", description="Distribution of the prior")
    mu: float = Field(
        0.0,
        ge=-1e6,
        le=1e6,
        description="Location (log scale for lognormal; unused by half_normal and exponential)",
    )
    sigma: float = Field(1.0, gt=0, le=1e6, description="Scale")
    nu: float | None = Field(
        None, gt=0, le=1000, description=f"Degrees of freedom (student_t; default {DEFAULT_NU})"
    )

    @model_validator(mode="after")
    def _check_nu(self) -> Prior:
        if self.nu is not None and self.dist != "student_t":
            raise ValueError("only the student_t prior uses nu")
        return self


class TermPrior(Prior):
    """The prior, sign and optional varying group of one term."""

    sign: Sign = Field("any", description="Allowed sign of the population effect")
    varies_by: Name | None = Field(
        None, description="A group in which the slope varies (partially pooled)"
    )


class InteractionPrior(BaseModel):
    """A two-way interaction of two covariates (their values are multiplied)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    a: Name
    b: Name
    prior: Prior = Prior(sigma=0.5)


_HALF_NORMAL: Final = Prior(dist="half_normal")


class DeclarativeGLMSettings(ModelSettings):
    """Settings of ``tabular.declarative_glm`` (the JSON that ``declarative_settings``
    writes; every list and mapping is bounded)."""

    family: Family = Field("normal", description="Likelihood family (its canonical link)")
    n_categories: int | None = Field(
        None, ge=2, le=50, description="Number of ordered categories (ordinal family only)"
    )
    covariates: list[Name] = Field(
        default=[], max_length=64, description="Term variables over obs, in term order"
    )
    groups: list[Name] = Field(
        default=[], max_length=3, description="Groups with their own intercept (crossed)"
    )
    terms: dict[Name, TermPrior] = Field(
        default={}, max_length=64, description="Prior, sign and varying group of each covariate"
    )
    interactions: list[InteractionPrior] = Field(
        default=[], max_length=16, description="Two-way interactions of covariates"
    )
    intercept: Prior = Field(Prior(sigma=2.0), description="Prior of the intercept")
    group_sigma: dict[Name, Prior] = Field(
        default={},
        max_length=3,
        description="Prior of the spread of each group's intercepts and slopes "
        "(default half_normal(1))",
    )
    noise: Prior = Field(_HALF_NORMAL, description="Prior of the noise scale")

    @model_validator(mode="after")
    def _check(self) -> DeclarativeGLMSettings:
        if self.family == "ordinal" and self.n_categories is None:
            raise ValueError("the ordinal family needs n_categories")
        if self.family != "ordinal" and self.n_categories is not None:
            raise ValueError("n_categories applies to the ordinal family only")
        for label, names in (("covariates", self.covariates), ("groups", self.groups)):
            if len(set(names)) != len(names):
                raise ValueError(f"{label} must be distinct")
        both = sorted(set(self.covariates) & set(self.groups))
        if both:
            raise ValueError(f"{both} cannot be both a covariate and a group")
        reserved = sorted(
            (set(self.covariates) | set(self.groups))
            & (_RESERVED | {f"{g}_idx" for g in self.groups})
        )
        if reserved:
            raise ValueError(f"{reserved} are reserved names")
        missing = sorted(set(self.covariates) - set(self.terms))
        extra = sorted(set(self.terms) - set(self.covariates))
        if missing or extra:
            raise ValueError(
                f"terms must have one entry for each covariate (missing {missing}, extra {extra})"
            )
        for name, t in self.terms.items():
            if t.varies_by is not None and t.varies_by not in self.groups:
                raise ValueError(f"term {name!r} varies by {t.varies_by!r}, which is not a group")
        pairs: set[frozenset[str]] = set()
        for x in self.interactions:
            stray = sorted({x.a, x.b} - set(self.covariates))
            if stray:
                raise ValueError(f"interaction terms {stray} are not covariates")
            if x.a == x.b:
                raise ValueError(f"interaction {x.a!r} x {x.b!r} needs two different terms")
            if frozenset((x.a, x.b)) in pairs:
                raise ValueError(f"interaction {x.a!r} x {x.b!r} is listed twice")
            pairs.add(frozenset((x.a, x.b)))
        stray = sorted(set(self.group_sigma) - set(self.groups))
        if stray:
            raise ValueError(f"group_sigma names {stray}, which are not groups")
        scales = {
            "noise": self.noise,
            **{f"group_sigma.{g}": p for g, p in self.group_sigma.items()},
        }
        for where, p in scales.items():
            if p.dist not in POSITIVE_DISTS:
                raise ValueError(f"{where} must be half_normal, exponential or lognormal")
        return self

    def spread(self, group: str) -> Prior:
        """The prior of the spread of ``group``'s intercepts and slopes."""
        return self.group_sigma.get(group, _HALF_NORMAL)


_TEMPLATE: dict[str, Any] = {
    "params": {"covariates": {"type": "string_list", "default": []}},
    "template": {
        "layout": "long",
        "dims": {OBS_DIM: {"kind": "integer", "policy": "extend"}},
        "vars": {
            TARGET: {"dims": [OBS_DIM], "dtype": "float64", "role": "target"},
            "{*covariates as v}": {"dims": [OBS_DIM], "dtype": "float64", "role": "covariate"},
        },
    },
}
"""Groups are not declared: they come either as a label variable or as an index plus a dim
(both shapes are checked at fit, see :func:`_row_labels`)."""

_ABOUT: Final = About(
    title="Your own model, built from choices",
    summary_plain="Explains a number from the columns that you select, with the direction and "
    "size that you expect for each effect. Groups such as stores can have their own baseline "
    "and their own effects.",
    use_when=[
        "You know which columns drive the result and which way each one pushes it",
        "You want to state what you expect before the data speaks, and see what changes",
        "Groups such as stores or regions share most of their behaviour",
    ],
    avoid_when=[
        "The result depends on past values of itself (use a forecast model)",
        "The effects change shape, for example they saturate (use a model with curves)",
    ],
    data_needs=[
        "One row for each observation, or a table over time and one group",
        "A numeric result column and numeric driver columns",
    ],
    assumptions=[
        "Effects add up on the scale of the model (after the link of the family)",
        "Each driver has one effect for all rows, or one for each level of one group",
    ],
    outputs=[
        "The effect of each driver with its range and sign",
        "Baselines for each group, and predictions for groups that the model did not see",
        "Predictions for new rows",
    ],
    difficulty=Difficulty.INTERMEDIATE,
    typical_runtime=TypicalRuntime.MINUTES,
    example="Sales from price (expected to lower sales) and promotion (expected to raise "
    "them, more in some stores than in others). Each store gets its own baseline.",
    related=["tabular.hierarchical_glm"],
    expert_notes="Bayesian GLM (PyMC, NUTS) compiled from a declarative model recipe. Per-term "
    "priors: normal, Student-t, Laplace, half-normal, exponential, lognormal. A positive or "
    "negative sign truncates a symmetric prior at 0 (exact truncated density, log transform) or "
    "flips a positive prior. Up to three crossed, non-centred group intercepts; varying slopes "
    "by one group share that group's spread prior; up to 16 two-way interactions. Families as "
    "tabular.hierarchical_glm (normal, Student-t, Poisson, Bernoulli, negative binomial, "
    "zero-inflated Poisson and negative binomial, beta, gamma, ordinal). Group labels are "
    "encoded at fit; unseen labels are drawn from the fitted group spread.",
    fits={"goal": ["explain_drivers", "predict_values"], "data_shape": ["table", "panel"]},
)

_FIT_LEVELS: ContextVar[Mapping[str, list[str]] | None] = ContextVar(
    "declarative_glm_fit_levels", default=None
)
"""The fitted model's group labels while :meth:`DeclarativeGLM.score` rebuilds the model."""


class DeclarativeGLM(PyMCModel[DeclarativeGLMSettings]):
    """A GLM described by closed choices: per-term priors and signs, crossed group
    intercepts, varying slopes and two-way interactions (D3)."""

    id = "tabular.declarative_glm"
    version = "1.0.0"
    Settings = DeclarativeGLMSettings
    description = (
        "A Bayesian GLM built from closed choices: a family, one prior and sign per term, up "
        "to three crossed group intercepts, slopes that vary by a group and two-way "
        "interactions. Group labels are encoded at fit; unseen groups are drawn from the "
        "fitted group spread."
    )
    about = _ABOUT
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
    derived: ClassVar[list[dict[str, Any]]] = [
        {
            "name": TERM_EFFECTS,
            "dims": ["sample", "term", OBS_DIM],
            "cache": "on_demand",
            "description": "The effect of each term and interaction per row, on the scale of "
            "the linear predictor (before the link), per posterior draw",
        }
    ]
    expected: ClassVar[dict[str, str]] = {"y_hat": "mu"}
    predictive: ClassVar[dict[str, str]] = {"y_hat": TARGET}
    plots = ("core.ppc", "core.convergence", "core.posterior_forest", "core.prior_posterior")
    default_engine = "pymc"
    pad_extend = False

    # ------------------------------------------------------------------ build
    def build(self, ds: xr.Dataset, s: DeclarativeGLMSettings, ctx: BuildContext) -> pm.Model:
        codes = _encode_groups(ds, s, ctx)  # puts each group's levels on ctx.ds
        coords = ctx.coords()
        if s.covariates:
            coords["covariate"] = list(s.covariates)
        if s.interactions:
            coords["interaction"] = [_pair(x) for x in s.interactions]
        n = int(ds.sizes[OBS_DIM])
        with pm.Model(coords=coords) as model:
            # ordinal: the cutpoints take the place of the intercept
            eta: Any = pt.zeros((n,))
            if s.family != "ordinal":
                eta = eta + _prior_rv("intercept", s.intercept)
            idx: dict[str, Any] = {}
            for g in s.groups:
                idx[g] = ctx.data(f"{g}__code", codes[g], dims=[OBS_DIM])
                spread = _prior_rv(f"sigma_{g}", s.spread(g))
                a = ctx.hier_effect(f"a_{g}", g, mu=0.0, sigma=spread)
                eta = eta + a[idx[g]]
            if s.covariates:
                cols = stack_columns(ds, list(s.covariates), OBS_DIM, ctx)
                x_t = ctx.data("X", cols, dims=[OBS_DIM, "covariate"])
                coefs = [_coefficient(f"b_{v}", s.terms[v]) for v in s.covariates]
                beta = pm.Deterministic("beta", pt.stack(coefs), dims="covariate")
                eta = eta + pt.dot(x_t, beta)
                for j, v in enumerate(s.covariates):
                    g_v = s.terms[v].varies_by
                    if g_v is None:
                        continue
                    spread = _prior_rv(f"sigma_slope_{v}", s.spread(g_v))
                    slope = ctx.hier_effect(f"slope_{v}", g_v, mu=0.0, sigma=spread)
                    eta = eta + slope[idx[g_v]] * x_t[:, j]
                if s.interactions:
                    col = {v: j for j, v in enumerate(s.covariates)}
                    gammas = [
                        _prior_rv(f"ix_{i}", pair.prior) for i, pair in enumerate(s.interactions)
                    ]
                    pm.Deterministic("beta_interaction", pt.stack(gammas), dims="interaction")
                    for pair, gamma in zip(s.interactions, gammas, strict=True):
                        eta = eta + gamma * x_t[:, col[pair.a]] * x_t[:, col[pair.b]]
            likelihood(eta, s.family, ctx, scale=_scale_prior(s.noise), n_categories=s.n_categories)
        return model

    # ------------------------------------------------------------------ fit / score
    def fit(
        self, ds: xr.Dataset, s: DeclarativeGLMSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        out = super().fit(ds, s, cfg, ctx)
        out.learned[LEVELS_KEY] = {
            g: list(_fit_levels(_row_labels(ds, g, "fit"))) for g in s.groups
        }
        return out

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        with _fitted_levels(fitted):
            return super().score(fitted, ds, req, ctx)

    def _overlap_prior(self, model: pm.Model, seed: int | None) -> xr.Dataset | None:
        """Prior draws of the population parameters (intercept, terms, spreads, noise), for
        ``core.prior_posterior`` and the scorecard's prior-overlap check. The parameter
        names depend on the settings, so they come from the built model."""
        names = [
            rv.name
            for rv in model.free_RVs
            if not model.named_vars_to_dims.get(rv.name) and rv.name != "cutpoints"
        ]
        if not names or self.prior_overlap_draws <= 0:
            return None
        prior = pm.sample_prior_predictive(
            draws=self.prior_overlap_draws, model=model, var_names=names, random_seed=seed
        )
        node = prior["prior"]
        assert isinstance(node, xr.DataTree)
        return node.to_dataset(inherit=False)

    # ------------------------------------------------------------------ derived
    def derive(self, fitted: Fitted, name: str, ds: xr.Dataset, ctx: ScoreContext) -> xr.Dataset:
        """``term_effects``: ``effect(sample, term, obs)`` on the linear-predictor scale.

        A term's effect is ``beta_v x_v`` plus its group slope ``s_v[g] x_v``; groups the
        fit did not see get the population slope only. Interactions are terms named
        ``a:b``. ``ctx.draw_index`` thins the posterior draws."""
        if name != TERM_EFFECTS:
            raise MapiError(
                f"model {self.id!r} has no derived quantity {name!r}",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer="/outputs/derived",
            )
        s = self.parse_settings(fitted.settings)
        post = fitted.posterior.stack({"sample": ("chain", "draw")}, create_index=False)
        post = post.drop_vars(["chain", "draw"], errors="ignore")
        if ctx.draw_index is not None:
            post = post.isel(sample=np.asarray(ctx.draw_index))
        n_sample = int(post.sizes["sample"])
        cols: dict[str, np.ndarray] = {}
        for v in s.covariates:
            if v not in ds:
                raise MapiError(
                    f"the data has no variable {v!r}",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{v}",
                )
            cols[v] = np.asarray(ds[v].transpose(OBS_DIM).values, dtype=float)
        levels = _levels_of(fitted)
        terms: list[str] = []
        effects: list[np.ndarray] = []
        beta = post["beta"].transpose("sample", "covariate").values if s.covariates else None
        for j, v in enumerate(s.covariates):
            assert beta is not None
            per_row = beta[:, j][:, None] * cols[v][None, :]
            g = s.terms[v].varies_by
            if g is not None:
                fit = levels.get(g, [])
                pos = {label: i for i, label in enumerate(fit)}
                labels = _row_labels(ds, g, "score")
                known = np.array([label in pos for label in labels])
                code = np.array([pos.get(label or "", 0) for label in labels], dtype=np.int64)
                slope = post[f"slope_{v}"].transpose("sample", ...).values  # (sample, levels)
                dev = np.where(known[None, :], slope[:, code], 0.0)
                per_row = per_row + dev * cols[v][None, :]
            terms.append(v)
            effects.append(per_row)
        if s.interactions:
            gamma = post["beta_interaction"].transpose("sample", "interaction").values
            for i, x in enumerate(s.interactions):
                terms.append(_pair(x))
                effects.append(gamma[:, i][:, None] * (cols[x.a] * cols[x.b])[None, :])
        n_obs = int(ds.sizes[OBS_DIM])
        arr = np.stack(effects, axis=1) if effects else np.zeros((n_sample, 0, n_obs), dtype=float)
        effect = xr.DataArray(
            arr,
            dims=("sample", "term", OBS_DIM),
            coords={"sample": np.arange(n_sample), "term": terms, OBS_DIM: ds[OBS_DIM].values},
            attrs={"scale": "linear predictor"},
        )
        return xr.Dataset({"effect": effect})


# ---------------------------------------------------------------------- priors


def _prior_rv(name: str, p: Prior) -> Any:
    """A free variable ``name`` with prior ``p`` (no sign constraint)."""
    match p.dist:
        case "normal":
            return pm.Normal(name, p.mu, p.sigma)
        case "student_t":
            return pm.StudentT(
                name, nu=p.nu if p.nu is not None else DEFAULT_NU, mu=p.mu, sigma=p.sigma
            )
        case "laplace":
            return pm.Laplace(name, p.mu, p.sigma)
        case "half_normal":
            return pm.HalfNormal(name, p.sigma)
        case "exponential":
            return pm.Exponential(name, scale=p.sigma)
        case "lognormal":
            return pm.LogNormal(name, p.mu, p.sigma)
    raise MapiError(
        f"unknown prior {p.dist!r}", code=ErrorCode.SETTINGS_INVALID
    )  # pragma: no cover


def _scale_prior(p: Prior) -> ScalePrior:
    """The noise prior as a :data:`ScalePrior`: ``factor`` scales the variable (exactly:
    the scale of a half-normal or exponential, ``mu + log(factor)`` of a lognormal)."""

    def make(name: str, factor: float) -> Any:
        if factor == 1:
            return _prior_rv(name, p)
        if p.dist == "lognormal":
            return _prior_rv(name, p.model_copy(update={"mu": p.mu + math.log(factor)}))
        return _prior_rv(name, p.model_copy(update={"sigma": p.sigma * factor}))

    return make


def _coefficient(name: str, t: TermPrior) -> Any:
    """The signed population effect of one term (free variable ``name``)."""
    if t.dist in POSITIVE_DISTS:  # positive by nature; "any" is treated as positive
        size = _prior_rv(name, t)
        return -size if t.sign == "negative" else size
    if t.sign == "any":
        return _prior_rv(name, t)
    size = _half_line(name, t, negative=t.sign == "negative")
    return -size if t.sign == "negative" else size


def _frozen(p: Prior) -> Any:
    """``p`` as a frozen scipy distribution (symmetric priors only)."""
    if p.dist == "normal":
        return scipy.stats.norm(p.mu, p.sigma)
    if p.dist == "student_t":
        return scipy.stats.t(p.nu if p.nu is not None else DEFAULT_NU, p.mu, p.sigma)
    return scipy.stats.laplace(p.mu, p.sigma)


def _pm_dist(p: Prior) -> Any:
    if p.dist == "normal":
        return pm.Normal.dist(p.mu, p.sigma)
    if p.dist == "student_t":
        return pm.StudentT.dist(nu=p.nu if p.nu is not None else DEFAULT_NU, mu=p.mu, sigma=p.sigma)
    return pm.Laplace.dist(p.mu, p.sigma)


def _half_line(name: str, p: Prior, *, negative: bool) -> Any:
    """The size ``v > 0`` of an effect whose prior ``p`` is truncated to one side of 0.

    The density is the prior's at ``+v`` (positive) or ``-v`` (negative), divided by the
    prior's weight on that side, with a log transform. Forward draws invert the CDF, so
    prior predictive sampling is exact. ``pm.Truncated`` is not used: its start point is
    the boundary 0 for a prior centred at 0, and its forward draws of a truncated Student-t
    are NaN.
    """
    base = _frozen(p)
    log_mass = float(base.logcdf(0.0) if negative else base.logsf(0.0))
    mass = math.exp(log_mass)
    if not mass >= MIN_SIGN_MASS:
        side = "negative" if negative else "positive"
        raise MapiError(
            f"the prior of {name!r} has almost no weight on {side} values: move mu to the "
            f"{side} side or remove the sign",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/settings/terms",
        )
    flip = -1.0 if negative else 1.0
    start = float(-base.ppf(mass / 2) if negative else base.isf(mass / 2))
    dist = _pm_dist(p)

    def logp(value: Any, *_: Any) -> Any:
        return pm.logp(dist, flip * value) - log_mass

    def random(*_: Any, rng: np.random.Generator | None = None, size: Any = None) -> Any:
        u = (rng or np.random.default_rng()).uniform(size=size)
        q = np.clip(u, 1e-12, 1.0) * mass
        return -base.ppf(q) if negative else base.isf(q)

    def support_point(*_: Any, **__: Any) -> Any:
        return pt.as_tensor_variable(start)

    return pm.CustomDist(
        name,
        logp=logp,
        random=random,
        support_point=support_point,
        transform=pm.distributions.transforms.log,
    )


# ---------------------------------------------------------------------- groups


def _pair(x: InteractionPrior) -> str:
    return f"{x.a}:{x.b}"


def _row_labels(ds: xr.Dataset, g: str, phase: str) -> list[str | None]:
    """The label of group ``g`` for each row: ``g[g_idx]`` when the data has the index and
    the dim, else the label variable ``g(obs)``. Labels are compared as text.

    A row without a label is an error, except a padding row of a score (``obs__requested``
    false: the platform fills it to keep the axis whole and drops its prediction), which
    gets ``None``."""
    code = ErrorCode.MISSING_FUTURE_COVARIATE if phase == "score" else ErrorCode.SCHEMA_INVALID
    idx = f"{g}_idx"
    if idx in ds.data_vars and g in ds.dims:
        positions = np.asarray(ds[idx].transpose(OBS_DIM).values)
        labels = np.asarray(ds[g].values) if g in ds.coords else np.arange(int(ds.sizes[g]))
        if positions.dtype.kind not in "iu" or (
            positions.size and (positions.min() < 0 or positions.max() >= labels.size)
        ):
            raise MapiError(
                f"{idx!r} must hold whole numbers from 0 to {labels.size - 1}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{idx}",
            )
        values = labels[positions]
    elif g in ds.data_vars:
        if ds[g].dims != (OBS_DIM,):
            raise MapiError(
                f"group {g!r} must have one label for each row (dims [{OBS_DIM!r}]), not "
                f"{list(ds[g].dims)}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{g}",
            )
        values = np.asarray(ds[g].values)
    else:
        raise MapiError(
            f"the {phase} data has no group {g!r}: give a label for each row, or {idx!r} "
            f"with the dim {g!r}",
            code=code,
            pointer=f"/data/{g}",
        )
    missing = np.asarray(pd.isna(values), dtype=bool)
    flag = f"{OBS_DIM}__requested"
    if flag in ds.coords:
        missing &= np.asarray(ds[flag].values, dtype=bool)
        padding = ~np.asarray(ds[flag].values, dtype=bool)
    else:
        padding = np.zeros(values.shape, dtype=bool)
    if np.any(missing):
        raise MapiError(
            f"group {g!r} has {int(np.sum(missing))} row(s) without a label",
            code=code,
            pointer=f"/data/{g}",
        )
    return [None if pad else str(v) for v, pad in zip(values.tolist(), padding, strict=True)]


def _fit_levels(labels: list[str | None]) -> list[str]:
    """The fit's levels of a group: its distinct labels, sorted (row order does not count)."""
    return sorted({v for v in labels if v is not None})


def _levels_of(fitted: Fitted) -> dict[str, list[str]]:
    raw = fitted.learned.get(LEVELS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {
        str(g): [str(v) for v in levels] for g, levels in raw.items() if isinstance(levels, list)
    }


@contextmanager
def _fitted_levels(fitted: Fitted) -> Iterator[None]:
    token = _FIT_LEVELS.set(_levels_of(fitted))
    try:
        yield
    finally:
        _FIT_LEVELS.reset(token)


def _encode_groups(
    ds: xr.Dataset, s: DeclarativeGLMSettings, ctx: BuildContext
) -> dict[str, np.ndarray]:
    """Row codes of each group, and ``ctx.ds`` with each group as a dim of its levels.

    At fit the levels are the sorted labels; at score they are the fit's levels followed by
    the unseen labels (sorted), which ``<g>__is_new`` flags, so :meth:`BuildContext.
    hier_effect` draws those from the fitted spread. A dim ``g`` of the data (the index
    shape) is replaced by the levels; the label variable ``g`` is dropped from the context
    only (``ds`` is unchanged)."""
    if not s.groups:
        return {}
    fitted: Mapping[str, list[str]] | None = None
    if ctx.is_score:
        fitted = _FIT_LEVELS.get()
        if fitted is None or any(g not in fitted for g in s.groups):
            raise MapiError(
                "the fitted model has no group levels; score it through the platform",
                code=ErrorCode.INTERNAL_ERROR,
            )
    labels = {g: _row_labels(ds, g, ctx.phase) for g in s.groups}
    aug = ds
    codes: dict[str, np.ndarray] = {}
    for g in s.groups:
        fit = list(fitted[g]) if fitted is not None else _fit_levels(labels[g])
        known = set(fit)
        new = sorted({v for v in labels[g] if v is not None and v not in known})
        levels = fit + new
        pos = {label: i for i, label in enumerate(levels)}
        # padding rows take the first level; their predictions are dropped
        codes[g] = np.asarray([0 if v is None else pos[v] for v in labels[g]], dtype=np.int64)
        if g in aug.dims:
            aug = aug.drop_dims(g)
        if g in aug.variables:
            aug = aug.drop_vars(g)
        aug = aug.assign_coords({g: np.asarray(levels, dtype=str)})
        if new:
            flags = np.asarray([False] * len(fit) + [True] * len(new))
            aug = aug.assign_coords({f"{g}__is_new": (g, flags)})
    ctx.ds = aug
    return codes
