"""``causal.synthetic_control``: Bayesian synthetic control for geo-lift tests.

Canonical inputs (bind dataset names onto them in the spec)::

    dims  unit: categorical, fixed        time: time, extend
    vars  y(unit, time): target, float64, missing allowed

``settings.treated`` names the units that got the intervention; every other unit (or
``settings.donors``) is a donor. For each treated unit ``j`` the model, on the normalised
scale (each unit divided by its pre-period mean when ``normalize: mean``), is::

    y_j,t ~ Normal | StudentT(alpha_j + Σ_d w_jd · y_d,t, sigma_j)      t before the start
    w_j   ~ Dirichlet(concentration · 1)     (simplex: a convex mix, classic synthetic control)
          | regularised horseshoe            (horseshoe: sparse, signed, may extrapolate)
    alpha_j ~ Normal(0, 1)  (intercept: true; else 0)     sigma_j ~ HalfNormal(noise_sigma)

Only the observed pre-period cells of the treated units enter the likelihood. The
counterfactual of every period is the synthetic copy ``alpha_j + Y_donor · w_j`` plus
observation noise drawn with common random numbers (``crn_normal(seed, "counterfactual")``)
in numpy after the fit; it is stored with the effect in the plugin-owned ``impact`` group,
next to ``donor_weights`` (see :mod:`mapi_plugin_causal._common`).

**Scoring** (``predict``) returns ``y_hat(unit, time)``: the synthetic copy of each treated
unit from the payload's donor values (expected, or a predictive draw with seeded noise).
Donor rows of ``y_hat`` are NaN (the platform restores the requested unit labels, so the
output keeps the full ``unit`` dim). Every donor value of a scored period is necessary.

Pointwise LOO is not offered: only the pre-period of the treated units is modelled, so the
likelihood does not describe the data that is scored (``loo_valid = False``).
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import scipy.stats
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
    ScoreContext,
)
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, field_validator, model_validator

from mapi_plugin_causal._common import (
    DONOR,
    IMPACT,
    IMPACT_DRAWS,
    MIN_PRE,
    SAMPLE,
    TARGET,
    TIME,
    UNIT,
    CausalBase,
    CausalSettings,
    Window,
    crn_normal,
    impact_group,
    log_effect_summary,
    parse_window,
    split_labels,
    thin_samples,
    time_label,
    unique_labels,
)

__all__ = [
    "DONOR_WEIGHTS",
    "OBS_DIM",
    "TREATED",
    "SyntheticControlModel",
    "SyntheticControlSettings",
    "counterfactual_draws",
    "unit_scales",
]

TREATED = "treated"
"""Dim of the treated units in the posterior (``weights(treated, donor)``)."""
OBS_DIM = "cell__obs"
"""Dim of the likelihood: the observed pre-period cells of the treated units."""
DONOR_WEIGHTS = "donor_weights"
TOP_DONORS = 10
"""Donors per treated unit whose weight is logged as a tracking metric."""


class SyntheticControlSettings(CausalSettings):
    """Settings of ``causal.synthetic_control`` (priors on the normalised scale)."""

    treated: list[str] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Labels of the units (markets) that got the intervention; a text "
        "'a, b' is split at the commas (x-ui-level: basic)",
    )
    donors: list[str] | None = Field(
        None,
        max_length=500,
        description="Units to build the synthetic copy from; default: "
        "every unit that is not treated",
    )
    weights: Literal["simplex", "horseshoe"] = Field(
        "simplex",
        description="simplex: positive weights that sum to 1 (classic synthetic control); "
        "horseshoe: sparse weights of any sign (can extrapolate)",
    )
    concentration: float = Field(
        1.0, gt=0, le=100, description="Dirichlet concentration of simplex weights (< 1: sparse)"
    )
    intercept: bool = Field(
        True, description="Allow a level shift between a unit and its synthetic copy"
    )
    normalize: Literal["mean", "none"] = Field(
        "mean", description="mean: divide each unit by its pre-period mean before the fit"
    )
    noise_sigma: float = Field(0.5, gt=0, le=10, description="Half-normal scale of sigma")
    likelihood: Literal["normal", "student_t"] = "normal"

    @field_validator("treated", mode="before")
    @classmethod
    def _treated_text(cls, v: Any) -> Any:
        return split_labels(v) if isinstance(v, str | list) else v

    @field_validator("donors", mode="before")
    @classmethod
    def _donors_text(cls, v: Any) -> Any:
        if isinstance(v, str | list):
            return split_labels(v) or None
        return v

    @model_validator(mode="after")
    def _labels(self) -> SyntheticControlSettings:
        if len(set(self.treated)) != len(self.treated):
            raise ValueError("treated must not repeat a unit")
        if self.donors is not None:
            if len(set(self.donors)) != len(self.donors):
                raise ValueError("donors must not repeat a unit")
            if set(self.donors) & set(self.treated):
                raise ValueError("a unit cannot be treated and a donor")
        return self


_TEMPLATE: dict[str, Any] = {
    "template": {
        "dims": {
            UNIT: {"kind": "categorical", "policy": "fixed"},
            TIME: {"kind": "time", "policy": "extend"},
        },
        "vars": {
            TARGET: {
                "dims": [UNIT, TIME],
                "dtype": "float64",
                "role": "target",
                "missing": "allow",
            }
        },
    }
}


@dataclass(slots=True)
class _Panel:
    """The fit data split into treated units and donors (model-stage values)."""

    times: np.ndarray
    units: list[str]
    treated: list[str]
    donors: list[str]
    y_treated: np.ndarray  # (J, n), NaN allowed
    y_donors: np.ndarray  # (D, n), complete
    window: Window

    @property
    def n(self) -> int:
        return int(self.times.shape[0])


class SyntheticControlModel(CausalBase, CustomModel[SyntheticControlSettings]):
    """Bayesian synthetic control with an ``impact`` and a ``donor_weights`` group (I-7)."""

    id = "causal.synthetic_control"
    version = "1.0.0"
    Settings = SyntheticControlSettings
    description = (
        "Bayesian synthetic control for geo-lift tests: each treated market is a weighted mix "
        "of untreated markets fitted before the test; the mix after the start is the "
        "counterfactual, and the difference to the actual sales is the effect."
    )
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict"}),
        loo_valid=False,
        loo_invalid_reason="the likelihood covers only the pre-period of the treated units, "
        "so pointwise leave-one-out does not describe the scored data",
        identified_quantities=["weights", "sigma"],
        owns=frozenset({"scaling"}),
        deterministic_given_seed=False,
        checkpointable=True,
    )
    schema_template = _TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {
            "dims": [UNIT, TIME],
            "units_of": TARGET,
            "description": "The synthetic control: what y of a treated unit would be without "
            "the intervention (NaN for donor units)",
        }
    }
    plots = ("causal.impact", "causal.pre_fit", "causal.donor_weights", "core.convergence")
    pad_extend = False
    default_engine: ClassVar[str | None] = None

    # ================================================================== data
    def panel(self, ds: xr.Dataset, s: SyntheticControlSettings) -> _Panel:
        """Check the fit data against the settings and split it (SC data checks, §2.4)."""
        for d in (UNIT, TIME):
            if d not in ds.dims:
                raise MapiError(f"the data has no dim {d!r}", code=ErrorCode.SCHEMA_INVALID)
        if TARGET not in ds:
            raise MapiError(f"the data has no variable {TARGET!r}", code=ErrorCode.SCHEMA_INVALID)
        times = np.asarray(ds[TIME].values)
        self.check_time(times)
        units = [str(u) for u in np.asarray(ds[UNIT].values).tolist()]
        missing = [u for u in s.treated if u not in units]
        if missing:
            raise MapiError(
                f"{len(missing)} treated unit(s) are not in the data",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/treated",
                untrusted_text=", ".join(missing)[:500],
            )
        if s.donors is None:
            donors = [u for u in units if u not in s.treated]
        else:
            absent = [u for u in s.donors if u not in units]
            if absent:
                raise MapiError(
                    f"{len(absent)} donor unit(s) are not in the data",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer="/model/settings/donors",
                    untrusted_text=", ".join(absent)[:500],
                )
            donors = list(s.donors)
        if len(donors) < 2:
            raise MapiError(
                f"synthetic control needs at least 2 donor units, the data gives {len(donors)}",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/donors",
            )
        window = parse_window(times, s.intervention_start, s.intervention_end)
        y = ds[TARGET].transpose(UNIT, TIME)
        y_t = np.asarray(y.isel({UNIT: _positions(units, s.treated)}).values, dtype=float)
        y_d = np.asarray(y.isel({UNIT: _positions(units, donors)}).values, dtype=float)
        gaps = [d for d, row in zip(donors, y_d, strict=True) if not np.isfinite(row).all()]
        if gaps:
            raise MapiError(
                f"{len(gaps)} donor unit(s) have missing values; synthetic control needs every "
                "value of a donor (remove the donor or fill the gaps)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
                untrusted_text=", ".join(gaps)[:500],
            )
        pre = window.pre_mask(times.shape[0])
        short = [
            u
            for u, row in zip(s.treated, y_t, strict=True)
            if int(np.isfinite(row[pre]).sum()) < MIN_PRE
        ]
        if short:
            raise MapiError(
                f"{len(short)} treated unit(s) have fewer than {MIN_PRE} observed values before "
                "the intervention start",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=f"/data/{TARGET}",
                untrusted_text=", ".join(short)[:500],
            )
        return _Panel(times, units, list(s.treated), donors, y_t, y_d, window)

    # ================================================================== model
    def build_model(
        self,
        y_treated: np.ndarray,
        y_donors: np.ndarray,
        s: SyntheticControlSettings,
        treated: list[str],
        donors: list[str],
    ) -> pm.Model:
        """The model of the pre-period, on the normalised scale.

        ``y_treated`` is ``(J, n_pre)`` (NaN = missing, left out of the likelihood) and
        ``y_donors`` ``(D, n_pre)``.
        """
        n_donors = len(donors)
        coords = {TREATED: treated, DONOR: donors}
        mask = np.isfinite(y_treated)
        jj, tt = np.nonzero(mask)
        with pm.Model(coords=coords) as model:
            if s.weights == "simplex":
                w = pm.Dirichlet(
                    "weights",
                    a=np.full((len(treated), n_donors), s.concentration),
                    dims=(TREATED, DONOR),
                )
            else:
                # regularised horseshoe (Piironen & Vehtari 2017), non-centred
                tau = pm.HalfCauchy("tau", beta=2.0 / n_donors, dims=TREATED)
                lam = pm.HalfCauchy("lam", beta=1.0, dims=(TREATED, DONOR))
                c2 = pm.InverseGamma("c2", alpha=2.0, beta=2.0, dims=TREATED)
                lam_t = pt.sqrt(c2[:, None] * lam**2 / (c2[:, None] + tau[:, None] ** 2 * lam**2))
                z = pm.Normal("z", 0.0, 1.0, dims=(TREATED, DONOR))
                w = pm.Deterministic("weights", z * tau[:, None] * lam_t, dims=(TREATED, DONOR))
            alpha: Any = (
                pm.Normal("alpha", 0.0, 1.0, dims=TREATED)
                if s.intercept
                else pt.zeros((len(treated),))
            )
            sigma = pm.HalfNormal("sigma", s.noise_sigma, dims=TREATED)
            mu = alpha[:, None] + pt.dot(w, pt.as_tensor_variable(y_donors))
            model.add_coord(OBS_DIM, np.arange(jj.size))
            if s.likelihood == "student_t":
                nu = pm.Gamma("nu", alpha=2.0, beta=0.1)
                pm.StudentT(
                    TARGET,
                    nu=nu,
                    mu=mu[jj, tt],
                    sigma=sigma[jj],
                    observed=y_treated[jj, tt],
                    dims=OBS_DIM,
                )
            else:
                pm.Normal(TARGET, mu[jj, tt], sigma[jj], observed=y_treated[jj, tt], dims=OBS_DIM)
        return model

    @staticmethod
    def kept_vars(s: SyntheticControlSettings) -> list[str]:
        """The posterior variables stored in the bundle."""
        names = ["weights", "sigma"]
        if s.intercept:
            names.append("alpha")
        if s.likelihood == "student_t":
            names.append("nu")
        if s.weights == "horseshoe":
            names.append("tau")
        return names

    # ================================================================== fit
    def fit(
        self, ds: xr.Dataset, s: SyntheticControlSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        p = self.panel(ds, s)
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        pre = p.window.pre_mask(p.n)
        scale_t = unit_scales(p.y_treated, pre, s.normalize, labels=p.treated)
        scale_d = unit_scales(p.y_donors, pre, s.normalize, labels=p.donors)
        yt_n = p.y_treated / scale_t[:, None]
        yd_n = p.y_donors / scale_d[:, None]
        model = self.build_model(yt_n[:, pre], yd_n[:, pre], s, p.treated, p.donors)
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
                model, plan, EngineHooks.for_context(ctx, progress=report, sink=sink, key=key)
            )
        ctx.check_cancelled()
        raw = result.group("posterior")
        if raw is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        posterior = _clean(raw[self.kept_vars(s)], result.engine)
        groups: dict[str, xr.Dataset] = {"posterior": posterior}
        stats = result.group("sample_stats")
        if stats is not None:
            keep = [
                v
                for v in stats.data_vars
                if stats[v].dtype.kind in "biuf" and set(stats[v].dims) <= {"chain", "draw"}
            ]
            groups["sample_stats"] = _clean(stats[keep], result.engine)
        groups["observed_data"] = _clean(
            xr.Dataset(
                {TARGET: ((UNIT, TIME), p.y_treated)},
                coords={UNIT: p.treated, TIME: p.times},
            ),
            result.engine,
        )
        groups["constant_data"] = _clean(
            xr.Dataset(
                {
                    "donor_y": ((DONOR, TIME), p.y_donors),
                    "scale": ((UNIT,), np.concatenate([scale_t, scale_d])),
                },
                coords={DONOR: p.donors, TIME: p.times, UNIT: [*p.treated, *p.donors]},
            ),
            result.engine,
        )
        ctx.progress(0.9, "computing the counterfactual")
        impact, weights = self._effect_groups(posterior, p, yd_n, scale_t, s, seed, ctx)
        groups[IMPACT] = impact
        groups[DONOR_WEIGHTS] = weights
        pre_rmse = {
            str(u): float(v)
            for u, v in zip(weights[UNIT].values, weights["pre_rmse"].values, strict=True)
        }
        log_effect_summary(
            ctx, impact, params={"causal.n_donors": len(p.donors), "causal.weights": s.weights}
        )
        if ctx.tracking:
            mean_w = weights["weight"].mean(SAMPLE).transpose(UNIT, DONOR)
            names = unique_labels([*p.treated, *p.donors])  # treated first: as log_effect_summary
            for u in p.treated:
                row = mean_w.sel({UNIT: u})
                order = np.argsort(-np.abs(row.values))[:TOP_DONORS]
                for k in order.tolist():
                    ctx.log_metric(
                        "donor_weight",
                        float(row.values[k]),
                        labels={"unit": names[str(u)], "donor": names[str(p.donors[k])]},
                    )
        ctx.progress(1.0, "posterior ready")
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "notes": [str(n) for n in result.notes],
            "treated": p.treated,
            "donors": p.donors,
            "scales": {
                u: float(v)
                for u, v in zip([*p.treated, *p.donors], [*scale_t, *scale_d], strict=True)
            },
            "normalize": s.normalize,
            "pre_rmse": pre_rmse,
            "intervention_start": p.window.start_label,
            "intervention_end": "" if p.window.open_end else p.window.end_label,
            "n_pre": int(pre.sum()),
            "n_post": int(p.window.post_mask(p.n).sum()),
            "first_label": time_label(p.times[0]),
            "last_label": time_label(p.times[-1]),
        }
        return FitOutput(groups=groups, learned=learned)

    def _effect_groups(
        self,
        posterior: xr.Dataset,
        p: _Panel,
        yd_n: np.ndarray,
        scale_t: np.ndarray,
        s: SyntheticControlSettings,
        seed: int,
        ctx: FitContext,
    ) -> tuple[xr.Dataset, xr.Dataset]:
        """The ``impact`` and ``donor_weights`` groups (target units, ``sample`` draws)."""
        thin = thin_samples(posterior, IMPACT_DRAWS, seed=seed)
        w = np.asarray(thin["weights"].transpose(SAMPLE, TREATED, DONOR).values, dtype=float)
        alpha = _draws(thin, "alpha", TREATED)
        sigma = _draws(thin, "sigma", TREATED)
        assert sigma is not None
        nu = _draws(thin, "nu")
        _, pred = counterfactual_draws(
            w, alpha, sigma, yd_n, seed=seed, name="counterfactual", nu=nu
        )
        coords_u = {UNIT: p.treated, TIME: p.times}
        cf = xr.DataArray(
            pred * scale_t[None, :, None],
            dims=(SAMPLE, UNIT, TIME),
            coords={SAMPLE: np.arange(w.shape[0]), **coords_u},
        )
        actual = xr.DataArray(p.y_treated, dims=(UNIT, TIME), coords=coords_u)
        units_label = None
        if ctx.to_original is not None:
            cf = ctx.to_original(cf, TARGET)
            actual = ctx.to_original(actual, TARGET)
            units_label = cf.attrs.get("units")
        impact = impact_group(
            p.times,
            np.asarray(actual.transpose(UNIT, TIME).values),
            np.asarray(cf.transpose(SAMPLE, UNIT, TIME).values),
            p.window.post_mask(p.n),
            units=p.treated,
            window=p.window,
            model_ref=self.ref(),
            units_label=units_label,
        )
        act = impact["actual"].values
        cf_mean = impact["counterfactual"].mean(SAMPLE).values
        pre = p.window.pre_mask(p.n)[None, :] & np.isfinite(act)
        rmse = np.array(
            [
                float(np.sqrt(np.mean((act[j] - cf_mean[j])[pre[j]] ** 2)))
                for j in range(act.shape[0])
            ]
        )
        weights = xr.Dataset(
            {
                "weight": ((SAMPLE, UNIT, DONOR), w),
                "pre_rmse": ((UNIT,), rmse),
            },
            coords={SAMPLE: np.arange(w.shape[0]), UNIT: p.treated, DONOR: p.donors},
            attrs={"prior": s.weights, "normalize": s.normalize, "model": self.ref()},
        )
        return impact, weights

    def diagnostics(self, fitted: Fitted, ds: xr.Dataset) -> list[Diagnostic]:
        post = fitted.groups.get("posterior")
        if post is None:
            return []
        return convergence_diagnostics(
            post, fitted.groups.get("sample_stats"), var_names=[str(v) for v in post.data_vars]
        )

    # ================================================================== score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        treated = _learned_list(fitted, "treated")
        donors = _learned_list(fitted, "donors")
        scales = fitted.learned.get("scales")
        if not isinstance(scales, dict):
            raise MapiError(
                "the fitted synthetic control has no unit scales; refit the model",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        if TARGET not in ds:
            raise MapiError(
                f"scoring a synthetic control needs the donor values of {TARGET!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data",
            )
        units = [str(u) for u in np.asarray(ds[UNIT].values).tolist()]
        post = fitted.posterior
        chain, draw = post["chain"].values, post["draw"].values
        out = np.full((chain.size * draw.size, len(units), ds.sizes[TIME]), np.nan)
        flag = f"{UNIT}__requested"
        requested = (
            set(np.asarray(ds[UNIT].values)[np.asarray(ds[flag].values, dtype=bool)].astype(str))
            if flag in ds.coords
            else set(units)
        )
        # a payload of donor units only (the platform pads the treated rows) has nothing to
        # score: its y_hat rows are NaN, as for every donor
        if requested & set(treated):
            y = ds[TARGET].transpose(UNIT, TIME)
            y_d = np.asarray(y.isel({UNIT: _positions(units, donors)}).values, dtype=float)
            gaps = [d for d, row in zip(donors, y_d, strict=True) if not np.isfinite(row).all()]
            if gaps:
                raise MapiError(
                    f"{len(gaps)} donor unit(s) have missing values in the scored periods; the "
                    "synthetic control needs every donor value",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"/data/{TARGET}",
                    untrusted_text=", ".join(gaps)[:500],
                )
            scale_t = np.array([_number(scales.get(u)) for u in treated])
            scale_d = np.array([_number(scales.get(u)) for u in donors])
            flat = post.stack(__s=("chain", "draw")).transpose("__s", ...)
            w = np.asarray(flat["weights"].transpose("__s", TREATED, DONOR).values, dtype=float)
            alpha = _draws(flat, "alpha", TREATED, sample="__s")
            sigma = _draws(flat, "sigma", TREATED, sample="__s")
            assert sigma is not None
            nu = _draws(flat, "nu", sample="__s")
            mu, pred = counterfactual_draws(
                w, alpha, sigma, y_d / scale_d[:, None], seed=ctx.seed, name="y_hat", nu=nu
            )
            ctx.check_cancelled()
            values = pred if ctx.kind == "predictive" else mu
            out[:, _positions(units, treated), :] = values * scale_t[None, :, None]
        y_hat = xr.DataArray(
            out.reshape(chain.size, draw.size, len(units), -1),
            dims=("chain", "draw", UNIT, TIME),
            coords={"chain": chain, "draw": draw, UNIT: ds[UNIT].values, TIME: ds[TIME].values},
            attrs={
                "kind": ctx.kind,
                "noise": "crn_seeded" if ctx.kind == "predictive" else "none",
            },
        )
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y_hat})})


# ============================================================================== numerics


def unit_scales(
    y: np.ndarray, pre: np.ndarray, normalize: str, *, labels: list[str] | None = None
) -> np.ndarray:
    """``(U,)`` divisors: the pre-period mean of each row (``mean``) or ones (``none``)."""
    if normalize == "none":
        return np.ones(y.shape[0])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        means = np.nanmean(y[:, pre], axis=1)
    bad = ~(np.isfinite(means) & (means > 0))
    if bad.any():
        names = [labels[i] for i in np.flatnonzero(bad)] if labels else []
        raise MapiError(
            f"{int(bad.sum())} unit(s) have a pre-period mean that is not positive; "
            "set normalize: none for data that can be zero or negative",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/{TARGET}",
            untrusted_text=", ".join(names)[:500] or None,
        )
    return np.asarray(means, dtype=float)


def counterfactual_draws(
    weights: np.ndarray,
    alpha: np.ndarray | None,
    sigma: np.ndarray,
    donors: np.ndarray,
    *,
    seed: int,
    name: str,
    nu: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """``(mu, predictive)``, each ``(S, J, n)``, on the scale of ``donors`` ``(D, n)``.

    ``mu = alpha + weights @ donors`` (``weights (S, J, D)``, ``alpha``/``sigma`` ``(S, J)``);
    the predictive draw adds ``sigma`` times standard normals (or Student-t with ``nu (S,)``
    degrees of freedom) from ``crn_normal(seed, name)``, so two calls with the same seed and
    name share their noise (common random numbers).
    """
    mu = np.einsum("sjd,dn->sjn", weights, donors)
    if alpha is not None:
        mu = mu + alpha[:, :, None]
    z = crn_normal(seed, name, mu.shape)
    if nu is not None:
        u = scipy.stats.norm.cdf(z)
        z = scipy.stats.t.ppf(u, df=np.asarray(nu)[:, None, None])
    return mu, mu + sigma[:, :, None] * z


# ============================================================================== helpers


def _positions(units: list[str], wanted: list[str]) -> list[int]:
    index = {u: i for i, u in enumerate(units)}
    return [index[u] for u in wanted]


def _draws(ds: xr.Dataset, name: str, *dims: str, sample: str = SAMPLE) -> np.ndarray | None:
    if name not in ds:
        return None
    return np.asarray(ds[name].transpose(sample, *dims).values, dtype=float)


def _number(value: Any) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise MapiError(
            "the fitted synthetic control has no scale for a unit; refit the model",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    return float(value)


def _learned_list(fitted: Fitted, key: str) -> list[str]:
    value = fitted.learned.get(key)
    if not isinstance(value, list) or not value:
        raise MapiError(
            f"the fitted synthetic control is missing {key!r}; refit the model",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    return [str(v) for v in value]


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
