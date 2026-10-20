"""Business priors and lift tests on mmm-framework (plan §11 Guided task 3; M3, v0.5).

Units
=====

mmm-framework owns scaling, so the model-stage data are in business units already (the
pipeline only converts currencies and resamples). When the harness supplies
``to_original`` the KPI and spend factors are read through it anyway (original units per
model unit, which must be constant: ``CALIBRATION_UNSUPPORTED`` otherwise), so a pipeline
that rescales either one still gets its ranges right.

Business priors → model-space priors
====================================

mmm-framework parameterises a channel's effect by its *return on spend* when the channel
has a per-channel ROI prior (``MediaChannelConfig.roi_prior_mu/sigma``)::

    roi_c ~ LogNormal(mu_c, sigma_c)
    beta_c = roi_c * spend_c / (y_std * sum_obs sat_c(adstock_c(x_c)))

so that on the observed media, the channel's contribution over the fit window divided by
its spend *is* ``roi_c`` exactly - the same quantity as a business ROI belief. The
translation is therefore closed form, not a Monte Carlo solve:

* an ROI range ``[low, high]`` held with credibility ``p`` is the log-normal whose central
  ``p`` interval it is (:func:`mapi_spec.calibration.lognormal_from_range`), after the unit
  conversion ``roi_model = roi_business * spend_factor / kpi_factor``;
* a contribution-share range is an ROI range scaled by ``Y / spend_c`` (``Y`` the observed
  KPI total over the fit window), because ``share_c = roi_c * spend_c / Y``;
* a range starting at 0 is read as ``[high / 20, high]`` (a log-normal has no mass at 0),
  with an ``info`` issue; a share for a channel without spend is an ``error``.

The baseline share (KPI not explained by media: intercept, trend, seasonality, geo and
product levels, controls) is linear in the intercept ``Normal(m, s)`` (standardised KPI)::

    B = a * (m + s * eps) + E,      a = n_obs * y_std / Y

with ``E`` the rest of the baseline, sampled from the model's own prior. ``s`` is solved
by bisection so the central ``p`` interval of ``B`` has the stated width, then ``m``
centres it; if the rest alone is already wider than the range, ``s`` stops at its floor
and a warning says so (``coverage`` shows by how much).

The preview samples the translated model's prior predictive (mmm-framework's own graph,
another seed) and reports ROI, contribution share, baseline share and a KPI fan in
business units, plus the prior mass inside every stated range (``coverage``).

Lift tests
==========

Each lift test becomes an mmm-framework ``ExperimentMeasurement`` - a likelihood term on
the model-implied estimand that updates ``beta``, the saturation curve and the adstock
kernel jointly (``BayesianMMM.add_experiment_calibration``):

* extra spend (``spend_delta > 0``): a marginal ROAS (``MROAS``) measurement with
  ``spend_lift_pct = 100 * spend_delta / window spend`` and value ``kpi_delta /
  spend_delta``;
* a holdout that removes (nearly) all of the channel's spend in the window
  (``-spend_delta >= 95%`` of it): a ``CONTRIBUTION`` measurement of ``-kpi_delta``;
* a partial cut: skipped with a note. It measures the slope of the response curve *below*
  today's spend, ``(f(S) - f((1 - c) S)) / (c S)``, while mmm-framework's ``MROAS``
  estimand is the slope *above* it, ``(f((1 + l) S) - f(S)) / (l S)`` with ``l > 0`` only
  (and off-panel calibration has no marginal estimand). Under diminishing returns the two
  differ - by ``e^(c lam)`` for ``1 - exp(-lam x)`` at a 20% cut - so mapping one onto the
  other would push the fitted curve away from what the experiment measured.

``geo`` restricts the measurement to that geo (``holdout_regions``). Tests after the data
or cut by the fit window (a holdout fit ending inside the test) are skipped and listed.
The tests used are kept in the bundle's ``lift_tests`` group (:func:`lift_test_group`):
the measured value and its standard error next to the model's own estimand per draw.

Limits
======

A count or proportion likelihood models the KPI on a link scale: the ROI parameterisation
and the identity-scale experiment likelihood do not apply, so business priors and lift
tests are ``CALIBRATION_UNSUPPORTED`` (:func:`check_calibration`). A time-varying channel
has no single ROI, so a business prior on it is an ``error`` issue; lift tests on it are
used (mmm-framework evaluates the estimand with the coefficient of each test period).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import xarray as xr
from mapi_spec.calibration import (
    PRIOR_TRANSLATION_FORMAT,
    BusinessPriors,
    DistributionSummary,
    FanChart,
    LiftTest,
    PriorPreview,
    PriorTranslationResult,
    lognormal_from_range,
)
from mapi_spec.errors import ErrorCode, Issue, MapiError, Severity

from mapi_plugin_mmm.framework import SPEND, TARGET, TIME, PanelData, quiet
from mapi_plugin_mmm.settings import BayesianSettings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mmm_framework.model import BayesianMMM

__all__ = [
    "SIGMA_MAX",
    "SIGMA_MIN",
    "LiftPlan",
    "TranslationOutcome",
    "UnitScales",
    "check_calibration",
    "lift_measurements",
    "lift_test_group",
    "lift_window_cut",
    "prior_preview",
    "translate_business_priors",
    "unit_scales",
]

ToOriginal = Callable[[xr.DataArray, str], xr.DataArray]
ModelFactory = Callable[[BayesianSettings], "BayesianMMM"]

SIGMA_MIN: Final = 0.02
"""Floor of a translated log-normal's sigma."""
SIGMA_MAX: Final = 4.0
_ZERO_LOW_RATIO: Final = 20.0
_BISECT_ITERS: Final = 60
_HOLDOUT_SHARE: Final = 0.95
_INTERCEPT_SD_MIN: Final = 1e-3
_INTERCEPT_SD_MAX: Final = 50.0


def _pointer(*parts: str | int) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _issue(
    message: str,
    *,
    pointer: str = "",
    severity: Severity = Severity.WARNING,
    code: ErrorCode = ErrorCode.BUSINESS_PRIORS_INVALID,
    action: str | None = None,
) -> Issue:
    return Issue(
        code=code, severity=severity, pointer=pointer, message=message[:2000],
        suggested_action=action,
    )  # fmt: skip


def _z(credibility: float) -> float:
    from statistics import NormalDist

    return NormalDist().inv_cdf(0.5 + credibility / 2)


# ======================================================================== units


@dataclass(frozen=True, slots=True)
class UnitScales:
    """Original (business) units per model-stage unit of the KPI and of spend."""

    kpi_factor: float = 1.0
    spend_factor: float = 1.0
    kpi_units: str | None = None
    spend_units: str | None = None

    @property
    def roi_factor(self) -> float:
        """Business ROI per model-stage ROI."""
        return self.kpi_factor / self.spend_factor

    @property
    def roi_units(self) -> str | None:
        if self.kpi_units and self.spend_units:
            return f"{self.kpi_units} per {self.spend_units}"[:64]
        return None


def _factor(to_original: ToOriginal | None, da: xr.DataArray, src: str) -> tuple[float, str | None]:
    if to_original is None:
        units = da.attrs.get("units")
        return 1.0, units[:64] if isinstance(units, str) and units else None
    try:
        one = to_original(xr.ones_like(da, dtype=float), src)
        two = to_original(xr.full_like(da, 2.0, dtype=float), src)
    except (MapiError, KeyError, ValueError):
        return 1.0, None
    a = np.asarray(one.values, dtype=float)
    b = np.asarray(two.values, dtype=float)
    finite = np.isfinite(a) & np.isfinite(b)
    if not finite.any():
        return 1.0, None
    a, b = a[finite], b[finite]
    linear = np.allclose(b, 2 * a, rtol=1e-6, atol=1e-12)
    constant = np.allclose(a, a.flat[0], rtol=1e-6, atol=1e-12)
    if not (linear and constant) or a.flat[0] <= 0:
        raise MapiError(
            f"business priors and lift tests need {src!r} in its business units up to one "
            "constant factor. The pipeline transforms it non-linearly or per cell (the "
            "mmm-framework model owns scaling, thus remove the scaling steps)",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer="/pipeline",
        )
    units = one.attrs.get("units")
    return float(a.flat[0]), units[:64] if isinstance(units, str) and units else None


def unit_scales(ds: xr.Dataset, to_original: ToOriginal | None) -> UnitScales:
    """The KPI and spend factors of model-stage ``ds`` (see the module doc)."""
    kf, ku = _factor(to_original, ds[TARGET], TARGET) if TARGET in ds else (1.0, None)
    sf, su = _factor(to_original, ds[SPEND], SPEND) if SPEND in ds else (1.0, None)
    return UnitScales(kf, sf, ku, su)


# ======================================================================== translation


@dataclass(slots=True)
class TranslationOutcome:
    """The translated priors (``settings.priors`` form) and what was solved."""

    priors: dict[str, dict[str, Any]]
    channels: list[str]
    issues: list[Issue] = field(default_factory=list)
    ranges: dict[str, tuple[str, float, float, float]] = field(default_factory=dict)
    """``channel -> (quantity, low, high, credibility)`` in business units."""
    baseline_range: tuple[float, float, float] | None = None


def _range(
    label: str, bounds: tuple[float, float], issues: list[Issue], pointer: str
) -> tuple[float, float]:
    lo, hi = bounds
    if lo > 0:
        return lo, hi
    issues.append(
        _issue(
            f"the range for {label!r} starts at 0, but a log-normal prior cannot have 0. The "
            f"server uses [{hi / _ZERO_LOW_RATIO:.3g}, {hi:.3g}]",
            pointer=pointer,
            severity=Severity.INFO,
        )
    )
    return hi / _ZERO_LOW_RATIO, hi


def check_calibration(
    s: BayesianSettings,
    *,
    business_priors: bool = False,
    lift_tests: bool = False,
    pointer: str = "/calibration",
) -> None:
    """``CALIBRATION_UNSUPPORTED`` for business priors or lift tests on a model with a
    count or proportion likelihood (see the module doc)."""
    if s.identity_link:
        return
    for used, what, path in (
        (business_priors, "business priors", "business_priors"),
        (lift_tests, "lift tests", "lift_tests"),
    ):
        if used:
            raise MapiError(
                f"a {s.likelihood} likelihood models the KPI on a link scale, so {what} do "
                "not apply. Use the normal or the student_t likelihood to calibrate the model",
                code=ErrorCode.CALIBRATION_UNSUPPORTED,
                pointer=f"{pointer}/{path}",
            )


def with_priors(s: BayesianSettings, priors: dict[str, Any]) -> BayesianSettings:
    """``s`` with prior overrides merged (translated keys win, per channel for ``roi``)."""
    tvp = sorted(set(priors.get("roi") or {}) & set(s.time_varying.channels))
    if tvp:
        raise MapiError(
            f"the effect of {tvp[0]} varies over time, so a prior on its ROI does not apply",
            code=ErrorCode.BUSINESS_PRIORS_INVALID,
            pointer=_pointer("priors", "roi", tvp[0]),
        )
    raw = s.model_dump(mode="json", exclude_none=True)
    old = raw.get("priors") or {}
    merged = {**old, **priors}
    if "roi" in priors and "roi" in old:
        merged["roi"] = {**old["roi"], **priors["roi"]}
    raw["priors"] = merged
    return BayesianSettings.model_validate(raw)


def translate_business_priors(
    beliefs: BusinessPriors,
    data: PanelData,
    s: BayesianSettings,
    scales: UnitScales,
    *,
    model_factory: ModelFactory,
    draws: int = 1000,
    seed: int = 0,
    pointer: str = "/business_priors",
) -> TranslationOutcome:
    """Business beliefs to ``priors.roi`` / ``priors.intercept`` (see the module doc).
    Unknown channels and shares of channels without spend are ``error`` issues."""
    assert data.y is not None
    issues: list[Issue] = []
    ranges: dict[str, tuple[str, float, float, float]] = {}
    roi_priors: dict[str, dict[str, float]] = {}
    kpi_total = float(data.y.sum()) * scales.kpi_factor
    spend_total = data.spend.sum(axis=(0, 1, 2)) * scales.spend_factor
    for label, belief in beliefs.channels.items():
        here = pointer + _pointer("channels", label)
        if label not in data.channels:
            issues.append(
                _issue(
                    f"the data has no channel {label!r}",
                    pointer=here,
                    severity=Severity.ERROR,
                    action="Use the channel names exactly as they are in the data.",
                )
            )
            continue
        if label in s.time_varying.channels:
            issues.append(
                _issue(
                    f"the effect of {label} varies over time, so a prior on its ROI does not apply",
                    pointer=here,
                    severity=Severity.ERROR,
                    action="Remove the channel from business priors or from time_varying.channels.",
                )
            )
            continue
        c = data.channels.index(label)
        if belief.roi is not None:
            lo, hi = _range(label, belief.roi, issues, here + "/roi")
            what = "roi"
            roi_lo, roi_hi = lo, hi
        else:
            assert belief.contribution_share is not None
            lo, hi = _range(label, belief.contribution_share, issues, here + "/contribution_share")
            what = "contribution_share"
            if spend_total[c] <= 0 or kpi_total <= 0:
                issues.append(
                    _issue(
                        f"channel {label!r} has no spend in the fit window (or the KPI total "
                        "is not positive). Thus a prior cannot set its share of the KPI",
                        pointer=here + "/contribution_share",
                        severity=Severity.ERROR,
                        action="Do not put the channel in business priors until it has "
                        "spend in the data.",
                    )
                )
                continue
            roi_lo, roi_hi = lo * kpi_total / spend_total[c], hi * kpi_total / spend_total[c]
        # business ROI -> model-stage ROI
        mu, sigma = lognormal_from_range(
            roi_lo / scales.roi_factor, roi_hi / scales.roi_factor, belief.credibility
        )
        if not (math.isfinite(mu) and math.isfinite(sigma)):  # pragma: no cover - guarded
            continue
        sigma_c = min(max(sigma, SIGMA_MIN), SIGMA_MAX)
        if sigma_c != sigma:
            issues.append(
                _issue(
                    f"the {what.replace('_', ' ')} range for {label!r} is "
                    + ("narrower" if sigma < SIGMA_MIN else "wider")
                    + " than a useful prior. The server set its spread to "
                    f"{sigma_c:g} on the log scale",
                    pointer=here,
                    severity=Severity.WARNING,
                )
            )
        roi_priors[label] = {"mu": float(mu), "sigma": float(sigma_c)}
        ranges[label] = (what, float(lo), float(hi), float(belief.credibility))
    priors: dict[str, dict[str, Any]] = {}
    if roi_priors:
        priors["roi"] = roi_priors
    baseline_range = None
    if beliefs.baseline_share is not None and kpi_total > 0:
        here = pointer + "/baseline_share"
        lo, hi = beliefs.baseline_share.share
        cred = float(beliefs.baseline_share.credibility)
        translated = with_priors(s, priors)
        intercept, note = _solve_baseline(
            translated, data, scales, (lo, hi, cred), model_factory, draws=draws, seed=seed
        )
        if note:
            issues.append(_issue(note, pointer=here, severity=Severity.WARNING))
        priors["intercept"] = intercept
        baseline_range = (float(lo), float(hi), cred)
    return TranslationOutcome(
        priors=priors,
        channels=list(data.channels),
        issues=issues,
        ranges=ranges,
        baseline_range=baseline_range,
    )


def _prior_tree(model: BayesianMMM, draws: int, seed: int) -> Any:
    with quiet():
        return model.get_prior(samples=int(draws), random_seed=int(seed))


def _flat(prior: Any, name: str, n_obs: int) -> np.ndarray | None:
    """``(S, ...)`` prior draws of ``name`` (``None`` when absent)."""
    node = prior["prior"] if "prior" in prior.children else prior
    ds = node.to_dataset() if isinstance(node, xr.DataTree) else node
    if name not in ds:
        return None
    arr = np.asarray(ds[name].values, dtype=float)
    return arr.reshape(arr.shape[0] * arr.shape[1], *arr.shape[2:])


def _baseline_rest(prior: Any, model: BayesianMMM) -> np.ndarray:
    """``(S, n_obs)`` standardised baseline without the intercept (trend, seasonality, geo
    and product levels, controls)."""
    n = int(model.n_obs)
    parts = []
    for name in ("trend_component", "seasonality_component", "geo_component",
                 "product_component", "controls_total"):  # fmt: skip
        arr = _flat(prior, name, n)
        if arr is not None:
            parts.append(arr.reshape(arr.shape[0], -1))
    if not parts:
        return np.zeros((1, n))
    return np.asarray(sum(parts))


def _solve_baseline(
    s: BayesianSettings,
    data: PanelData,
    scales: UnitScales,
    target: tuple[float, float, float],
    factory: ModelFactory,
    *,
    draws: int,
    seed: int,
) -> tuple[dict[str, float], str | None]:
    """The intercept ``Normal(m, s)`` (standardised KPI) giving the baseline share range."""
    assert data.y is not None
    lo, hi, cred = target
    model = factory(s)
    prior = _prior_tree(model, draws, seed)
    y_std, y_mean = float(model.y_std), float(model.y_mean)
    n_obs = int(model.n_obs)
    y_total = float(data.y.sum())
    rest = _baseline_rest(prior, model)  # (S, n_obs)
    e = (rest.sum(axis=1) * y_std + n_obs * y_mean) / y_total  # (S,)
    a = n_obs * y_std / y_total
    rng = np.random.default_rng(seed + 7)
    eps = rng.standard_normal(e.size)
    q = (1 - cred) / 2

    def width(sd: float) -> float:
        b = a * sd * eps + e
        qs = np.quantile(b, [q, 1 - q])
        return float(qs[1] - qs[0])

    want = hi - lo
    note = None
    lo_sd, hi_sd = _INTERCEPT_SD_MIN, _INTERCEPT_SD_MAX
    if width(lo_sd) >= want:
        sd = lo_sd
        note = (
            "the trend, seasonality and controls alone make the baseline share vary more "
            f"than the stated range [{lo:.3g}, {hi:.3g}]. The server made the intercept prior "
            "as narrow as possible"
        )
    elif width(hi_sd) <= want:
        sd = hi_sd
    else:
        for _ in range(_BISECT_ITERS):
            mid = math.sqrt(lo_sd * hi_sd)
            if width(mid) < want:
                lo_sd = mid
            else:
                hi_sd = mid
        sd = math.sqrt(lo_sd * hi_sd)
    b0 = a * sd * eps + e
    qs = np.quantile(b0, [q, 1 - q])
    centre = (qs[0] + qs[1]) / 2
    m = ((lo + hi) / 2 - centre) / a
    return {"mu": float(np.clip(m, -50, 50)), "sigma": float(sd)}, note


# ======================================================================== preview


def _summary(
    values: np.ndarray, quantiles: Sequence[float], units: str | None
) -> DistributionSummary:
    v = values[np.isfinite(values)]
    if not v.size:
        return DistributionSummary(mean=0.0, sd=0.0, quantiles={}, units=units)
    qs = np.quantile(v, list(quantiles))
    return DistributionSummary(
        mean=float(v.mean()),
        sd=float(v.std()),
        quantiles={f"{q:g}": float(x) for q, x in zip(quantiles, qs, strict=True)},
        units=units,
    )


def _time_labels(times: np.ndarray) -> list[str | int | float]:
    if np.issubdtype(times.dtype, np.datetime64):
        return [str(x) for x in np.datetime_as_string(times, unit="D")]
    out: list[str | int | float] = []
    for t in times.tolist():
        out.append(t if isinstance(t, int | float) and not isinstance(t, bool) else str(t))
    return out


def _covered(values: np.ndarray, lo: float, hi: float) -> float:
    v = values[np.isfinite(values)]
    return float(((v >= lo) & (v <= hi)).mean()) if v.size else 0.0


def prior_preview(
    outcome: TranslationOutcome,
    data: PanelData,
    s: BayesianSettings,
    scales: UnitScales,
    *,
    model_factory: ModelFactory,
    draws: int,
    seed: int,
    quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
    lift_tests: Sequence[LiftTest] = (),
) -> tuple[PriorTranslationResult, xr.Dataset]:
    """Sample the translated model's prior predictive and summarise it in business units.

    Returns the ``priors.json`` document and the draws for plots (``roi``,
    ``contribution_share``, ``baseline_share``, ``kpi`` and ``kpi_observed`` plus the
    stated ranges)."""
    assert data.y is not None
    translated = with_priors(s, outcome.priors)
    model = model_factory(translated)
    prior = _prior_tree(model, draws, seed)
    n_obs = int(model.n_obs)
    y_std, y_mean = float(model.y_std), float(model.y_mean)
    kf, sf = scales.kpi_factor, scales.spend_factor
    contrib = _flat(prior, "channel_contributions", n_obs)
    n_ch = len(data.channels)
    if contrib is None:  # pragma: no cover - the graph always registers it
        raise MapiError("the prior has no channel contributions", code=ErrorCode.INTERNAL_ERROR)
    contrib = contrib.reshape(contrib.shape[0], n_obs, n_ch).sum(axis=1) * y_std * kf  # (S, C)
    n = contrib.shape[0]
    spend_total = data.spend.sum(axis=(0, 1, 2)) * sf
    kpi_total = float(data.y.sum()) * kf
    with np.errstate(divide="ignore", invalid="ignore"):
        roi = np.where(spend_total[None, :] > 0, contrib / spend_total[None, :], np.nan)
        share = contrib / kpi_total if kpi_total else np.full_like(contrib, np.nan)
    intercept = _flat(prior, "intercept", n_obs)
    rest = _baseline_rest(prior, model)
    base = (
        (np.asarray(intercept).reshape(-1)[:, None] if intercept is not None else 0.0) + rest
    ) * y_std + y_mean  # (S, n_obs)
    base_share = base.sum(axis=1) * kf / kpi_total if kpi_total else np.full(n, np.nan)
    # KPI per period: the prior predictive summed over cells
    pp = prior["prior_predictive"].to_dataset() if "prior_predictive" in prior.children else None
    t, cells = data.n_periods, data.n_cells
    if pp is not None and "y_obs" in pp:
        yo = np.asarray(pp["y_obs"].values, dtype=float)
        yo = yo.reshape(yo.shape[0] * yo.shape[1], -1)
        kpi_t = (yo * y_std + y_mean).reshape(-1, t, cells).sum(axis=2) * kf
    else:  # pragma: no cover - the graph always has y_obs
        kpi_t = np.zeros((n, t))
    observed_t = data.y.reshape(t, cells).sum(axis=1) * kf
    labels = list(data.channels)
    preview = PriorPreview(
        roi={labels[c]: _summary(roi[:, c], quantiles, scales.roi_units) for c in range(n_ch)},
        contribution_share={labels[c]: _summary(share[:, c], quantiles, None) for c in range(n_ch)},
        baseline_share=_summary(base_share, quantiles, None) if kpi_total else None,
        kpi_fan=_fan(kpi_t, data.times, observed_t, quantiles, scales.kpi_units),
    )
    coverage: dict[str, float] = {}
    for label, (what, lo, hi, _cred) in outcome.ranges.items():
        c = labels.index(label)
        coverage[label] = _covered(roi[:, c] if what == "roi" else share[:, c], lo, hi)
    if outcome.baseline_range is not None and kpi_total:
        coverage["baseline"] = _covered(base_share, *outcome.baseline_range[:2])
    issues = list(outcome.issues)
    issues += _lift_checks(lift_tests, data, roi, labels)
    result = PriorTranslationResult(
        format=PRIOR_TRANSLATION_FORMAT,
        channels=labels,
        priors=outcome.priors,
        preview=preview,
        coverage=coverage,
        draws=int(n),
        seed=seed,
        issues=issues[:256],
    )
    sample = np.arange(n)
    ch_coords = {"channel": labels}
    out: dict[str, xr.DataArray] = {
        "roi": xr.DataArray(
            roi, dims=["sample", "channel"], coords={"sample": sample, **ch_coords}
        ),
        "contribution_share": xr.DataArray(
            share, dims=["sample", "channel"], coords={"sample": sample, **ch_coords}
        ),
        "baseline_share": xr.DataArray(base_share, dims=["sample"], coords={"sample": sample}),
        "kpi": xr.DataArray(
            kpi_t, dims=["sample", TIME], coords={"sample": sample, TIME: data.times}
        ),
        "kpi_observed": xr.DataArray(observed_t, dims=[TIME], coords={TIME: data.times}),
    }
    stated_q = np.full(n_ch, "", dtype=object)
    stated_lo = np.full(n_ch, np.nan)
    stated_hi = np.full(n_ch, np.nan)
    stated_c = np.full(n_ch, np.nan)
    for name, (what, lo, hi, cr) in outcome.ranges.items():
        c = labels.index(name)
        stated_q[c], stated_lo[c], stated_hi[c], stated_c[c] = what, lo, hi, cr
    out["stated_quantity"] = xr.DataArray(stated_q.astype(str), dims=["channel"], coords=ch_coords)
    out["stated_low"] = xr.DataArray(stated_lo, dims=["channel"], coords=ch_coords)
    out["stated_high"] = xr.DataArray(stated_hi, dims=["channel"], coords=ch_coords)
    out["stated_credibility"] = xr.DataArray(stated_c, dims=["channel"], coords=ch_coords)
    preview_ds = xr.Dataset(out)
    if scales.roi_units:
        preview_ds["roi"].attrs["units"] = scales.roi_units
    if scales.kpi_units:
        preview_ds["kpi"].attrs["units"] = scales.kpi_units
    if outcome.baseline_range is not None:
        lo_b, hi_b, cb = outcome.baseline_range
        preview_ds["baseline_share"].attrs.update(
            {"range_low": lo_b, "range_high": hi_b, "credibility": cb}
        )
    return result, preview_ds


def _fan(
    kpi_t: np.ndarray,
    times: np.ndarray,
    observed: np.ndarray,
    quantiles: Sequence[float],
    units: str | None,
) -> FanChart:
    qs = sorted({min(max(float(q), 1e-6), 1 - 1e-6) for q in quantiles})[:16]
    values = np.quantile(kpi_t, qs, axis=0)
    return FanChart(
        dim=TIME,
        coords=_time_labels(times),
        quantiles=qs,
        values=[[float(v) if math.isfinite(v) else None for v in row] for row in values],
        observed=[float(v) if math.isfinite(v) else None for v in observed],
        units=units,
    )


def _lift_checks(
    tests: Sequence[LiftTest], data: PanelData, roi: np.ndarray, labels: list[str]
) -> list[Issue]:
    """Lift tests on unknown channels/geos (error) and tests whose implied ROI falls outside
    the 90% prior ROI interval (info)."""
    out: list[Issue] = []
    for i, test in enumerate(tests):
        here = _pointer("lift_tests", i)
        if test.channel not in labels:
            out.append(
                _issue(
                    f"lift test on {test.channel!r}: the data has no such channel",
                    pointer=here + "/channel",
                    severity=Severity.ERROR,
                    code=ErrorCode.SCHEMA_INVALID,
                )
            )
            continue
        if test.geo is not None and test.geo not in (data.geos or []):
            out.append(
                _issue(
                    f"lift test on {test.channel!r}: the data has no geo {test.geo!r}",
                    pointer=here + "/geo",
                    severity=Severity.ERROR,
                    code=ErrorCode.SCHEMA_INVALID,
                )
            )
            continue
        v = roi[:, labels.index(test.channel)]
        v = v[np.isfinite(v)]
        if not v.size:
            continue
        lo, hi = np.quantile(v, [0.05, 0.95])
        implied = test.implied_roi
        if not lo <= implied <= hi:
            out.append(
                _issue(
                    f"the lift test on {test.channel!r} implies an ROI of {implied:.3g}, "
                    f"outside the 90% prior range [{lo:.3g}, {hi:.3g}]. The fit will use "
                    "both",
                    pointer=here,
                    severity=Severity.INFO,
                )
            )
    return out


# ======================================================================== lift tests


def _day(t: date) -> np.datetime64:
    return np.datetime64(t.isoformat(), "D")


def lift_window_cut(days: np.ndarray, start: date, end: date) -> bool:
    """Whether the data's periods (labelled by one date each) cut the test window: it
    reaches more than one period past the first or last label. A test the fit window cuts
    (a holdout fit that ends inside it) cannot be split between the periods kept and those
    cut, so it is left out, never squeezed into the periods kept."""
    d = np.unique(np.asarray(days).astype("datetime64[D]"))
    if not d.size:
        return True
    gaps = np.diff(d).astype(np.int64)
    step = int(np.median(gaps)) if gaps.size else 1
    slack = np.timedelta64(step - 1, "D")
    return bool(_day(start) < d.min() - slack or _day(end) > d.max() + slack)


@dataclass(slots=True)
class LiftPlan:
    """mmm-framework measurements for the usable tests, plus what was skipped and why.
    ``rows`` describes each used test (``id``, ``channel``, ``estimand`` - contribution
    or mroas -, ``start``, ``end``, ``geo`` - empty when national)."""

    measurements: list[Any]
    used: list[str]
    skipped: dict[str, str]
    rows: list[dict[str, str]] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        return {"used": list(self.used), "skipped": dict(self.skipped)}


def lift_measurements(
    tests: Sequence[LiftTest],
    data: PanelData,
    scales: UnitScales,
    *,
    pointer: str = "/calibration/lift_tests",
    outcome: str | None = None,
) -> LiftPlan:
    """``ExperimentMeasurement`` per usable lift test (see the module doc). Unknown
    channels or geos are ``SCHEMA_INVALID`` at the test's pointer. ``outcome`` names the
    outcome the measurements refer to (multi-outcome models; ``None``: the only one)."""
    from mmm_framework.calibration import ExperimentEstimand, ExperimentMeasurement

    times = np.asarray(data.times)
    if tests and times.dtype.kind != "M":
        raise MapiError(
            "lift tests need dated periods; the time labels are not dates",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer=pointer,
        )
    days = times.astype("datetime64[D]") if times.size else times
    out: list[Any] = []
    used: list[str] = []
    skipped: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for i, test in enumerate(tests):
        here = f"{pointer}/{i}"
        tid = test.id or f"lift_{i}"
        if test.channel not in data.channels:
            raise MapiError(
                f"lift test {i}: no channel {test.channel!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=here + "/channel",
            )
        c = data.channels.index(test.channel)
        geo_idx: list[int] | None = None
        if test.geo is not None:
            if not data.geos:
                raise MapiError(
                    f"lift test {i} names geo {test.geo!r} but the model has no geo dim",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/geo",
                )
            if test.geo not in data.geos:
                raise MapiError(
                    f"lift test {i}: no geo {test.geo!r}",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/geo",
                )
            geo_idx = [data.geos.index(test.geo)]
        in_window = (days >= _day(test.start)) & (days <= _day(test.end))
        if not in_window.any():
            if _day(test.start) > days.max():
                skipped[tid] = "after the data"
                continue
            raise MapiError(
                f"lift test {i}: its window covers no period of the data",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=here,
            )
        if lift_window_cut(days, test.start, test.end):
            skipped[tid] = "the fit window cuts the test window"
            continue
        sp = data.spend[in_window][:, :, :, c]  # (t_w, G, P)
        if geo_idx is not None:
            sp = sp[:, geo_idx]
        window_spend = float(sp.sum()) * scales.spend_factor
        kf = scales.kpi_factor
        sigma = test.sigma / kf
        common: dict[str, Any] = {
            "channel": test.channel,
            "test_period": (str(test.start), str(test.end)),
            "holdout_regions": [test.geo] if test.geo is not None else None,
            "name": f"lift_{len(out)}",
        }
        if outcome is not None:
            common["outcome"] = outcome
        delta = test.spend_delta
        if delta > 0:
            if window_spend <= 0:
                skipped[tid] = "no spend in the window to scale (extra spend on a dark channel)"
                continue
            m = ExperimentMeasurement(
                value=float(test.kpi_delta / kf) / (delta / scales.spend_factor),
                se=float(sigma / (delta / scales.spend_factor)),
                estimand=ExperimentEstimand.MROAS,
                spend_lift_pct=100.0 * delta / window_spend,
                **common,
            )
        else:
            cut = -delta
            if window_spend <= 0:
                skipped[tid] = "no spend in the window to remove"
                continue
            if cut > window_spend * 1.000001:
                raise MapiError(
                    f"lift test {i} removes more spend ({cut:g}) than the data shows "
                    f"({window_spend:g})",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/spend_delta",
                )
            if cut >= _HOLDOUT_SHARE * window_spend:
                m = ExperimentMeasurement(
                    value=float(-test.kpi_delta / kf),
                    se=float(sigma),
                    estimand=ExperimentEstimand.CONTRIBUTION,
                    **common,
                )
            else:
                skipped[tid] = (
                    f"a partial cut of {cut / window_spend:.0%} of the window's spend measures "
                    "the return below the current spend. You can calibrate the model only "
                    "on the return above it (an increase in spend) or on a full holdout"
                )
                continue
        out.append(m)
        used.append(tid)
        rows.append(
            {
                "id": tid,
                "channel": test.channel,
                "estimand": "mroas" if delta > 0 else "contribution",
                "start": str(test.start),
                "end": str(test.end),
                "geo": test.geo or "",
            }
        )
    return LiftPlan(out, used, skipped, rows)


def lift_test_group(plan: LiftPlan, posterior: xr.Dataset) -> xr.Dataset | None:
    """The bundle's ``lift_tests`` group (``None`` without used tests): ``observed(test)``
    and ``observed_sd(test)`` (the measurement and its standard error, model-stage units:
    KPI for a contribution, KPI per unit of spend for an mroas), ``model(chain, draw,
    test)`` (the model's estimand, ``<name>_model_estimand``) and the string coords
    ``channel``, ``estimand``, ``start``, ``end``, ``geo`` over ``test``."""
    if not plan.measurements:
        return None
    keep: list[int] = []
    model: list[xr.DataArray] = []
    for k, m in enumerate(plan.measurements):
        name = f"{m.name}_model_estimand"
        if name not in posterior:
            continue
        da = posterior[name]
        extra = [d for d in da.dims if d not in ("chain", "draw")]
        if extra:  # pragma: no cover - mmm-framework registers a scalar per experiment
            continue
        keep.append(k)
        model.append(da.drop_vars([c for c in da.coords if c not in ("chain", "draw")]))
    if not keep:
        return None
    rows = [plan.rows[k] if k < len(plan.rows) else {} for k in keep]
    ids = [r.get("id", plan.used[k]) for r, k in zip(rows, keep, strict=True)]
    coords: dict[str, Any] = {"test": np.asarray(ids, dtype=str)}
    for key in ("channel", "estimand", "start", "end", "geo"):
        coords[key] = ("test", np.asarray([str(r.get(key, "")) for r in rows], dtype=str))
    ms = [plan.measurements[k] for k in keep]
    out = xr.Dataset(
        {
            "observed": ("test", np.asarray([float(m.value) for m in ms])),
            "observed_sd": ("test", np.asarray([float(m.se) for m in ms])),
            "model": xr.concat(model, dim="test").transpose("chain", "draw", "test"),
        }
    )
    return out.assign_coords(coords)
