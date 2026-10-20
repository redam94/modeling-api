"""``causal.did``: Bayesian difference-in-differences and event study on a panel.

Canonical inputs (bind dataset names onto them in the spec)::

    dims  unit: categorical, fixed        time: time, fixed (time effects are per period)
    vars  y(unit, time): target, float64, missing allowed
          treatment(unit, time): float64 in {0, 1}, 1 from the adoption period on
          <covariate>(unit, time): one per settings.covariates

The model (two-way fixed effects with event-time dummies), with ``y`` standardised by the
mean and sd of the never-treated units (stored in ``learned["scaling"]``)::

    y_ut ~ Normal | StudentT(mu_ut, sigma)
    mu_ut = loc + scale · (intercept + alpha_u + gamma_t + Σ_k beta_k · D_k,ut + x_ut · b)
    alpha ~ ZeroSumNormal (unit)    gamma ~ ZeroSumNormal | centred random walk (time)

``D_k`` marks the cells ``k`` periods after the adoption of their unit (the first period with
``treatment = 1``): ``k = -leads .. -2`` (leads) and ``0 .. lags`` (lags); ``k = -1`` is
the reference and has no dummy. With ``bin_endpoints`` the end dummies also take the cells
further out (``k <= -leads``, ``k >= lags``); without it those cells have no dummy. Design
``did`` has one dummy, the treatment itself. ``event_coef = scale · beta`` is in the units of
y. Only observed cells enter the fit likelihood (dim ``cell__obs``); scoring uses every cell.

**Counterfactual** scores use the platform intervention ``{var: treatment, set: 0}``: every
dummy is then 0 and ``y_hat`` is the outcome without the treatment. The fit also stores the
plugin-owned groups ``impact`` (treated units; the counterfactual is ``mu`` without the
dummies plus observation noise with common random numbers) and ``event_study`` (the
coefficients with the reference period at 0, the cells behind each and the ATT).

A score payload may cover only some periods. The harness pads the others (the time dim is
fixed) and the padded treatment cells take the fitted adoption, so the event times of the
sent cells do not move (:func:`fill_padded_treatment`).

Staggered adoption (different adoption periods) gives the diagnostic ``STAGGERED_ADOPTION``:
two-way fixed effects can be biased when the effect changes between cohorts.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import scipy.stats
import xarray as xr
from mapi_pymc.context import BuildContext
from mapi_pymc.model import PyMCModel
from mapi_sdk.models import FitContext, FitOutput, Fitted, ModelSettings, ScoreContext
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, JsonValue, field_validator

from mapi_plugin_causal._common import (
    IMPACT,
    IMPACT_DRAWS,
    SAMPLE,
    TARGET,
    TIME,
    UNIT,
    CausalBase,
    Window,
    crn_normal,
    impact_group,
    log_effect_summary,
    thin_samples,
    time_label,
)

__all__ = [
    "EVENT_DIM",
    "EVENT_STUDY",
    "OBS_DIM",
    "TREATMENT",
    "DiDModel",
    "DiDScaling",
    "DiDSettings",
    "adoption_positions",
    "check_treatment",
    "event_dummies",
    "event_times",
    "fill_padded_treatment",
    "fitted_treatment",
    "requested_cells",
    "staggered_diagnostic",
]

TREATMENT = "treatment"
EVENT_DIM = "event_time"
COVARIATE_DIM = "covariate"
OBS_DIM = "cell__obs"
"""Dim of the fit likelihood when some cells are missing (the observed cells, row-major)."""
EVENT_STUDY = "event_study"
REFERENCE = -1


class DiDSettings(ModelSettings):
    """Settings of ``causal.did`` (priors on the standardised target)."""

    design: Literal["event_study", "did"] = Field(
        "event_study",
        description="event_study: one effect per period relative to the adoption; "
        "did: one effect for all treated cells (x-ui-level: basic)",
    )
    leads: int = Field(4, ge=0, le=52, description="Periods before the adoption with an effect")
    lags: int = Field(8, ge=0, le=104, description="Periods after the adoption with an effect")
    bin_endpoints: bool = Field(
        True, description="The first lead and the last lag also take the periods further out"
    )
    time_effects: Literal["iid", "random_walk"] = Field(
        "iid", description="iid: independent period effects; random_walk: smooth period effects"
    )
    covariates: list[str] = Field(
        default_factory=list, max_length=16, description="Control variables over (unit, time)"
    )
    likelihood: Literal["normal", "student_t"] = "normal"
    effect_sigma: float = Field(1.0, gt=0, le=10, description="Prior sd of the event effects")
    noise_sigma: float = Field(0.5, gt=0, le=10, description="Half-normal scale of sigma")

    @field_validator("covariates")
    @classmethod
    def _covariates(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("covariates must not repeat a variable")
        bad = sorted({x for x in v if x in (TARGET, TREATMENT, UNIT, TIME) or not x.isidentifier()})
        if bad:
            raise ValueError(f"covariates cannot use the names {bad}")
        return v


_TEMPLATE: dict[str, Any] = {
    "params": {"covariates": {"type": "string_list", "default": []}},
    "template": {
        "dims": {
            UNIT: {"kind": "categorical", "policy": "fixed"},
            TIME: {"kind": "time", "policy": "fixed"},
        },
        "vars": {
            TARGET: {
                "dims": [UNIT, TIME],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            },
            TREATMENT: {
                "dims": [UNIT, TIME],
                "dtype": "float64",
                "role": "covariate",
                "bounds": [0.0, 1.0],
            },
            "{*covariates as x}": {
                "dims": [UNIT, TIME],
                "dtype": "float64",
                "role": "covariate",
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class DiDScaling:
    """Standardisation of y (never-treated cells) and of the covariates (all cells)."""

    loc: float
    scale: float
    x_loc: list[float]
    x_scale: list[float]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> DiDScaling:
        raw = json.loads(text)
        return cls(
            loc=float(raw["loc"]),
            scale=float(raw["scale"]),
            x_loc=[float(v) for v in raw["x_loc"]],
            x_scale=[float(v) for v in raw["x_scale"]],
        )


_SCALING: ContextVar[DiDScaling | None] = ContextVar("did_scaling", default=None)
"""The fitted scaling while a score (or the fit) rebuilds the model."""
_ADOPTION: ContextVar[dict[str, Any] | None] = ContextVar("did_adoption", default=None)
"""The fitted adoption period of each unit (``learned["adoption"]``) while a score rebuilds
the model: it fills the cells the score payload left out (see :func:`fill_padded_treatment`)."""


class DiDModel(CausalBase, PyMCModel[DiDSettings]):
    """Two-way fixed effects DiD / event study with ``impact`` and ``event_study`` groups (I-8)."""

    id = "causal.did"
    version = "1.0.0"
    Settings = DiDSettings
    description = (
        "Bayesian difference-in-differences and event study on a panel of units over time: "
        "unit and period effects plus one effect per period relative to the adoption of the "
        "treatment, compared with units that never got it."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "counterfactual"}),
        identified_quantities=["event_coef", "sigma"],
        owns=frozenset({"scaling"}),
        deterministic_given_seed=False,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [UNIT, TIME],
            "units_of": TARGET,
            "description": "Expected value or predictive draw of y; with the intervention "
            "treatment = 0, the outcome without the treatment",
        }
    }
    expected: ClassVar[dict[str, str]] = {"y_hat": "mu"}
    predictive: ClassVar[dict[str, str]] = {"y_hat": TARGET}
    plots = (
        "causal.event_study",
        "causal.impact",
        "causal.pre_fit",
        "core.convergence",
        "core.posterior_forest",
    )
    prior_overlap_vars = ("event_effect", "sigma")

    # ------------------------------------------------------------------ model
    def build(self, ds: xr.Dataset, s: DiDSettings, ctx: BuildContext) -> pm.Model:
        if TREATMENT not in ds:
            raise MapiError(
                f"the data has no variable {TREATMENT!r}",
                code=ErrorCode.MISSING_FUTURE_COVARIATE
                if ctx.is_score
                else ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TREATMENT}",
            )
        treat = np.asarray(ds[TREATMENT].transpose(UNIT, TIME).values, dtype=float)
        adoption = _ADOPTION.get()
        if ctx.is_score and adoption is not None:
            treat = fill_padded_treatment(
                treat, requested_cells(ds), fitted_treatment(ds, adoption)
            )
        check_treatment(treat, fit=ctx.is_fit)
        sc = _SCALING.get() or fit_scaling(ds, s)
        dummies = event_dummies(treat, s)
        coords = ctx.coords()
        coords[EVENT_DIM] = event_times(s)
        if s.covariates:
            coords[COVARIATE_DIM] = list(s.covariates)
        with pm.Model(coords=coords) as model:
            ctx.data(TREATMENT, treat, dims=[UNIT, TIME])
            d_t = ctx.data("event_dummies", dummies, dims=[UNIT, TIME, EVENT_DIM])
            intercept = pm.Normal("intercept", 0.0, 1.0)
            alpha = pm.ZeroSumNormal(
                "unit_effect", sigma=pm.HalfNormal("unit_sigma", 1.0), dims=UNIT
            )
            if s.time_effects == "random_walk":
                walk = pm.GaussianRandomWalk(
                    "time_walk",
                    sigma=pm.HalfNormal("time_sigma", 0.2),
                    init_dist=pm.Normal.dist(0.0, 1.0),
                    dims=TIME,
                )
                gamma = pm.Deterministic("time_effect", walk - walk.mean(), dims=TIME)
            else:
                gamma = pm.ZeroSumNormal(
                    "time_effect", sigma=pm.HalfNormal("time_sigma", 0.5), dims=TIME
                )
            beta = pm.Normal("event_effect", 0.0, s.effect_sigma, dims=EVENT_DIM)
            pm.Deterministic("event_coef", beta * sc.scale, dims=EVENT_DIM)
            mu_std = (
                intercept
                + alpha[:, None]
                + gamma[None, :]
                + pt.tensordot(d_t, beta, axes=[[2], [0]])
            )
            if s.covariates:
                xs = [
                    ctx.data(
                        name,
                        (_values(ds, name) - sc.x_loc[i]) / sc.x_scale[i],
                        dims=[UNIT, TIME],
                    )
                    for i, name in enumerate(s.covariates)
                ]
                b = pm.Normal("covariate_effect", 0.0, 1.0, dims=COVARIATE_DIM)
                mu_std = mu_std + pt.tensordot(pt.stack(xs, axis=-1), b, axes=[[2], [0]])
            mu = pm.Deterministic("mu", sc.loc + sc.scale * mu_std, dims=(UNIT, TIME))
            sigma = pm.HalfNormal("sigma", s.noise_sigma)
            self._likelihood(model, mu, sigma * sc.scale, s, ctx)
        return model

    @staticmethod
    def _likelihood(
        model: pm.Model, mu: Any, sigma: Any, s: DiDSettings, ctx: BuildContext
    ) -> None:
        y = ctx.observed(TARGET, dims=[UNIT, TIME])
        nu = pm.Gamma("nu", alpha=2.0, beta=0.1) if s.likelihood == "student_t" else None

        def like(mean: Any, obs: np.ndarray, dims: Any) -> None:
            if nu is None:
                pm.Normal(TARGET, mean, sigma, observed=obs, dims=dims)
            else:
                pm.StudentT(TARGET, nu=nu, mu=mean, sigma=sigma, observed=obs, dims=dims)

        if ctx.is_score:
            like(mu, y, (UNIT, TIME))
            return
        ok = np.isfinite(y)
        if not ok.any():
            raise MapiError(
                f"every value of the target {TARGET!r} is missing", code=ErrorCode.SCHEMA_INVALID
            )
        if ok.all():
            like(mu, y, (UNIT, TIME))
            return
        idx = np.flatnonzero(ok.ravel())
        model.add_coord(OBS_DIM, np.arange(idx.size))
        like(mu.flatten()[idx], y.ravel()[idx], OBS_DIM)
        ctx.observed_vars[TARGET] = (OBS_DIM,)
        ctx.observed_values[TARGET] = y.ravel()[idx]

    # ------------------------------------------------------------------ fit
    def fit(self, ds: xr.Dataset, s: DiDSettings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        for d in (UNIT, TIME):
            if d not in ds.dims:
                raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
        if TREATMENT not in ds:
            raise MapiError(
                f"the data has no variable {TREATMENT!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TREATMENT}",
            )
        times = np.asarray(ds[TIME].values)
        self.check_time(times)
        treat = np.asarray(ds[TREATMENT].transpose(UNIT, TIME).values, dtype=float)
        check_treatment(treat, fit=True)
        sc = fit_scaling(ds, s)
        token = _SCALING.set(sc)
        try:
            out = super().fit(ds, s, cfg, ctx)
        finally:
            _SCALING.reset(token)
        for name, grp in list(out.groups.items()):
            if OBS_DIM in grp.dims and OBS_DIM not in grp.coords:
                out.groups[name] = grp.assign_coords({OBS_DIM: np.arange(grp.sizes[OBS_DIM])})
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        units = [str(u) for u in np.asarray(ds[UNIT].values).tolist()]
        adopt = adoption_positions(treat)
        cohorts = sorted({int(a) for a in adopt if a >= 0})
        staggered = len(cohorts) > 1
        impact, study = self._effect_groups(out.groups["posterior"], ds, treat, s, sc, seed, ctx)
        out.groups[IMPACT] = impact
        out.groups[EVENT_STUDY] = study
        out.learned["scaling"] = sc.to_json()
        out.learned["design"] = s.design
        out.learned["event_times"] = list[JsonValue](event_times(s))
        out.learned["adoption"] = {
            u: (time_label(times[a]) if a >= 0 else None)
            for u, a in zip(units, adopt.tolist(), strict=True)
        }
        out.learned["staggered"] = staggered
        out.learned["n_never_treated"] = int((adopt < 0).sum())
        diagnostic = staggered_diagnostic(adopt)
        if diagnostic is not None:
            out.diagnostics.append(diagnostic)
        if ctx.tracking:
            log_effect_summary(
                ctx,
                impact,
                params={
                    "causal.design": s.design,
                    "causal.staggered": staggered,
                    "causal.leads": s.leads,
                    "causal.lags": s.lags,
                },
            )
            att = np.asarray(study["att"].values, dtype=float)
            att = att[np.isfinite(att)]
            if att.size:
                lo, hi = np.quantile(att, [0.05, 0.95])
                ctx.log_metrics(
                    {"att": float(att.mean()), "att_p05": float(lo), "att_p95": float(hi)}
                )
            coef = study["coef"].mean(SAMPLE)
            for k, v in zip(study[EVENT_DIM].values.tolist(), coef.values.tolist(), strict=True):
                ctx.log_metric("event_coef", float(v), labels={"event_time": str(int(k))})
        return out

    def _effect_groups(
        self,
        posterior: xr.Dataset,
        ds: xr.Dataset,
        treat: np.ndarray,
        s: DiDSettings,
        sc: DiDScaling,
        seed: int,
        ctx: FitContext,
    ) -> tuple[xr.Dataset, xr.Dataset]:
        """The ``impact`` (treated units) and ``event_study`` groups, in target units."""
        times = np.asarray(ds[TIME].values)
        units = [str(u) for u in np.asarray(ds[UNIT].values).tolist()]
        adopt = adoption_positions(treat)
        rows = np.flatnonzero(adopt >= 0)
        treated = [units[i] for i in rows]
        dummies = event_dummies(treat, s)[rows]  # (Ut, n, K)
        thin = thin_samples(
            posterior[[v for v in ("mu", "event_coef", "sigma", "nu") if v in posterior]],
            IMPACT_DRAWS,
            seed=seed,
        )
        mu = np.asarray(thin["mu"].transpose(SAMPLE, UNIT, TIME).values, dtype=float)[:, rows]
        coef = np.asarray(thin["event_coef"].transpose(SAMPLE, EVENT_DIM).values, dtype=float)
        sigma = np.asarray(thin["sigma"].values, dtype=float) * sc.scale
        cf_mean = mu - np.einsum("unk,sk->sun", dummies, coef)
        z = crn_normal(seed, "counterfactual", cf_mean.shape)
        if "nu" in thin:
            nu = np.asarray(thin["nu"].values, dtype=float)
            z = scipy.stats.t.ppf(scipy.stats.norm.cdf(z), df=nu[:, None, None])
        cf_pred = cf_mean + sigma[:, None, None] * z
        y = np.asarray(ds[TARGET].transpose(UNIT, TIME).values, dtype=float)[rows]
        coords = {SAMPLE: np.arange(mu.shape[0]), UNIT: treated, TIME: times}
        dims3 = (SAMPLE, UNIT, TIME)

        def target(arr: np.ndarray, dims: tuple[str, ...]) -> np.ndarray:
            da = xr.DataArray(arr, dims=dims, coords={d: coords[d] for d in dims})
            if ctx.to_original is not None:
                da = ctx.to_original(da, TARGET)
            return np.asarray(da.transpose(*dims).values, dtype=float)

        units_label = None
        if ctx.to_original is not None:
            probe = ctx.to_original(
                xr.DataArray(y, dims=(UNIT, TIME), coords={UNIT: treated, TIME: times}), TARGET
            )
            units_label = probe.attrs.get("units")
        diff = target(mu, dims3) - target(cf_mean, dims3)  # expected effect per cell
        post = treat[rows] >= 0.5
        start = int(adopt[rows].min())
        window = Window(start, len(times) - 1, time_label(times[start]), "", True)
        impact = impact_group(
            times,
            target(y, (UNIT, TIME)),
            target(cf_pred, dims3),
            post,
            units=treated,
            window=window,
            model_ref=self.ref(),
            units_label=units_label,
        )
        ks = event_times(s)
        full_k = sorted({*ks, REFERENCE})
        rel = np.arange(len(times))[None, :] - adopt[rows][:, None]
        out_coef = np.zeros((mu.shape[0], len(full_k)))
        n_cells = np.zeros(len(full_k), dtype=np.int64)
        for i, k in enumerate(full_k):
            if k == REFERENCE:
                n_cells[i] = int((rel == REFERENCE).sum())
                continue
            j = ks.index(k)
            cells = dummies[..., j] >= 0.5
            n_cells[i] = int(cells.sum())
            out_coef[:, i] = diff[:, cells].mean(axis=1) if cells.any() else coef[:, j]
        effect_cells = post & (dummies.sum(axis=-1) >= 0.5)
        with np.errstate(invalid="ignore"):
            att = (
                diff[:, effect_cells].mean(axis=1)
                if effect_cells.any()
                else np.full(mu.shape[0], np.nan)
            )
        study = xr.Dataset(
            {
                "coef": ((SAMPLE, EVENT_DIM), out_coef),
                "n_cells": ((EVENT_DIM,), n_cells),
                "att": ((SAMPLE,), att),
            },
            coords={SAMPLE: coords[SAMPLE], EVENT_DIM: np.asarray(full_k, dtype=np.int64)},
            attrs={
                "reference": REFERENCE,
                "binned": int(s.bin_endpoints),
                "design": s.design,
                "model": self.ref(),
            },
        )
        return impact, study

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        raw = fitted.learned.get("scaling")
        if not isinstance(raw, str):
            raise MapiError(
                "the fitted DiD model has no scaling; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        adoption = fitted.learned.get("adoption")
        token = _SCALING.set(DiDScaling.from_json(raw))
        token_adopt = _ADOPTION.set(adoption if isinstance(adoption, dict) else None)
        try:
            return super().score(fitted, ds, req, ctx)
        finally:
            _ADOPTION.reset(token_adopt)
            _SCALING.reset(token)


# ============================================================================== design


def staggered_diagnostic(adopt: np.ndarray) -> Diagnostic | None:
    """``STAGGERED_ADOPTION`` (warning) when the units adopt at different periods."""
    cohorts = {int(a) for a in np.asarray(adopt).tolist() if a >= 0}
    if len(cohorts) < 2:
        return None
    return Diagnostic(
        code="STAGGERED_ADOPTION",
        severity="warning",
        value=float(len(cohorts)),
        message_expert=f"the treatment starts at {len(cohorts)} different periods; two-way "
        "fixed effects estimates can be biased when the effect changes between adoption "
        "cohorts (Goodman-Bacon 2021, Sun & Abraham 2021)",
        message_plain="The units started the treatment at different times. If the effect is "
        "different for early and late units, the estimates can be wrong.",
        suggested_action="Compare the event study of each adoption group, or fit one model "
        "per group with the never-treated units as controls.",
    )


def check_treatment(treat: np.ndarray, *, fit: bool) -> None:
    """``treatment`` must be 0/1 and never go back to 0; the fit needs never-treated units."""
    pointer = f"/data/{TREATMENT}"
    if not np.isfinite(treat).all():
        raise MapiError(
            f"{TREATMENT!r} has missing values", code=ErrorCode.SCHEMA_INVALID, pointer=pointer
        )
    if not np.isin(treat, (0.0, 1.0)).all():
        raise MapiError(
            f"{TREATMENT!r} must be 0 or 1 in every cell",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
        )
    if treat.shape[1] > 1 and (np.diff(treat, axis=1) < 0).any():
        raise MapiError(
            f"{TREATMENT!r} must stay 1 after the adoption (no unit may go back to 0)",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
        )
    if not fit:
        return
    ever = treat.max(axis=1) >= 0.5
    if ever.all():
        raise MapiError(
            "difference-in-differences needs at least one unit that never gets the treatment",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
        )
    if not ever.any():
        raise MapiError(
            "no unit gets the treatment", code=ErrorCode.SCHEMA_INVALID, pointer=pointer
        )


def requested_cells(ds: xr.Dataset) -> np.ndarray:
    """``(U, n)`` True for the cells the score payload sent; the harness pads the other
    labels of the fixed dims (``<dim>__requested`` is False there). All True at fit."""
    shape = (int(ds.sizes[UNIT]), int(ds.sizes[TIME]))
    mask = np.ones(shape, dtype=bool)
    for axis, dim in enumerate((UNIT, TIME)):
        flag = f"{dim}__requested"
        if flag in ds.coords:
            sent = np.asarray(ds[flag].values, dtype=bool)
            mask &= sent[:, None] if axis == 0 else sent[None, :]
    return mask


def fitted_treatment(ds: xr.Dataset, adoption: dict[str, Any]) -> np.ndarray:
    """``(U, n)`` treatment of the fit data, rebuilt from ``learned["adoption"]`` (unit ->
    label of the first treated period, ``None``: never treated) over the labels of ``ds``."""
    labels = [time_label(t) for t in np.asarray(ds[TIME].values)]
    pos = {lab: i for i, lab in enumerate(labels)}
    out = np.zeros((int(ds.sizes[UNIT]), len(labels)))
    for i, unit in enumerate(np.asarray(ds[UNIT].values).tolist()):
        start = adoption.get(str(unit))
        if isinstance(start, str) and start in pos:
            out[i, pos[start] :] = 1.0
    return out


def fill_padded_treatment(
    treat: np.ndarray, requested: np.ndarray, fitted: np.ndarray
) -> np.ndarray:
    """The score treatment with the padded cells filled from the fit.

    The time dim is fixed, so a payload that covers only some periods is padded with 0, and
    the adoption period (the first 1) would move. A padded cell takes the fitted value,
    clipped so that the unit stays monotone around the cells the payload sent: never below a
    sent value before it, never above a sent value after it. A payload that sets the
    treatment to 0 (the counterfactual) therefore also turns the earlier padded cells to 0.
    """
    if requested.all():
        return treat
    sent_lo = np.where(requested, treat, 0.0)
    sent_hi = np.where(requested, treat, 1.0)
    lo = np.maximum.accumulate(sent_lo, axis=1)
    hi = np.minimum.accumulate(sent_hi[:, ::-1], axis=1)[:, ::-1]
    filled = np.minimum(np.maximum(fitted, lo), hi)
    return np.where(requested, treat, filled)


def adoption_positions(treat: np.ndarray) -> np.ndarray:
    """``(U,)`` position of the first treated period of each unit (``-1``: never treated)."""
    on = treat >= 0.5
    return np.where(on.any(axis=1), on.argmax(axis=1), -1).astype(np.int64)


def event_times(s: DiDSettings) -> list[int]:
    """The event times with a dummy: ``-leads .. -2`` and ``0 .. lags`` (``[0]`` for did)."""
    if s.design == "did":
        return [0]
    return [*range(-s.leads, REFERENCE), *range(0, s.lags + 1)]


def event_dummies(treat: np.ndarray, s: DiDSettings) -> np.ndarray:
    """``(U, n, K)`` 0/1 dummies over :func:`event_times` (never-treated units: all 0)."""
    ks = event_times(s)
    n_units, n = treat.shape
    out = np.zeros((n_units, n, len(ks)))
    if s.design == "did":
        out[..., 0] = treat >= 0.5
        return out
    adopt = adoption_positions(treat)
    ever = adopt >= 0
    rel = np.arange(n)[None, :] - adopt[:, None]
    first, last = ks[0], ks[-1]
    for j, k in enumerate(ks):
        if s.bin_endpoints and k == first and k < REFERENCE:
            cells = rel <= k
        elif s.bin_endpoints and k == last and k >= 0:
            cells = rel >= k
        else:
            cells = rel == k
        out[..., j] = cells & ever[:, None]
    return out


def fit_scaling(ds: xr.Dataset, s: DiDSettings) -> DiDScaling:
    """Location and scale of y over the never-treated units (all untreated cells if that is
    constant) and of each covariate over every cell."""
    y = np.asarray(ds[TARGET].transpose(UNIT, TIME).values, dtype=float)
    treat = np.asarray(ds[TREATMENT].transpose(UNIT, TIME).values, dtype=float)
    never = treat.max(axis=1) < 0.5
    ref = y[never]
    ref = ref[np.isfinite(ref)]
    if ref.size < 2 or float(ref.std()) <= 0:
        ref = y[(treat < 0.5) & np.isfinite(y)]
    if ref.size == 0:
        raise MapiError(
            f"the target {TARGET!r} has no observed untreated value",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{TARGET}",
        )
    scale = float(ref.std())
    x_loc, x_scale = [], []
    for name in s.covariates:
        x = _values(ds, name)
        x_loc.append(float(np.nanmean(x)))
        sd = float(np.nanstd(x))
        x_scale.append(sd if sd > 0 else 1.0)
    return DiDScaling(float(ref.mean()), scale if scale > 0 else 1.0, x_loc, x_scale)


def _values(ds: xr.Dataset, name: str) -> np.ndarray:
    if name not in ds:
        raise MapiError(
            f"the data has no covariate {name!r}",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{name}",
        )
    return np.asarray(ds[name].transpose(UNIT, TIME).values, dtype=float)
