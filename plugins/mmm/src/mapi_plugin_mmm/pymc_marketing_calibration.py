"""Business priors for ``mmm.pymc_marketing`` (plan §11 Guided task 3; M3).

pymc-marketing's MMM scales the KPI by ``target_scale`` (its maximum) and each channel by
its maximum, and every saturation curve it offers here is ``beta * g(adstock(x))`` (or
``adstock(beta * g(x))`` with ``adstock_first: false``), with ``beta`` the
``saturation_beta`` prior (``saturation_b`` for ``none``) over ``(*dims, channel)``. A
channel's contribution over the fit window in KPI units is therefore::

    C_c = target_scale * sum_{cells} beta[cell, c] * A[cell, c],
    A[cell, c] = sum_t g(...)[t, cell, c]

where ``cells`` are the panel ``dims`` (one cell without them) and ``A`` depends on the
adstock and curve priors only. The translation draws ``A`` by Monte Carlo from
pymc-marketing's own model - prior draws of ``channel_contribution / beta`` - so its
transforms, scaling and padding are exactly the ones the fit uses, and then solves, as for
``mmm.hierarchical_geo`` (:mod:`mapi_plugin_mmm.calibration`), for one log-normal
``beta ~ LogNormal(mu_c, sigma_c)`` per channel (independent per cell, as pymc-marketing
draws it): ``mu_c`` shifts ``log ROI_c`` and ``sigma_c`` widens it, so a bisection on
``sigma_c`` matches the stated interval's width at its credibility and ``mu_c`` centres
it. Shares use the observed KPI total instead of the channel's spend; channels without a
belief keep their prior refitted as the log-normal with the same 5% and 95% quantiles.

The **baseline share** (the KPI without media: intercepts, controls and seasonality) is
linear in the intercept, ``intercept ~ Normal(m, s)`` per cell::

    B = a * (m + s * zbar) + E,    a = target_scale * T * cells / Y

with ``zbar`` the mean of the cells' standard normals and ``E`` the controls' and
seasonality's share (prior draws of ``control_contribution`` and
``yearly_seasonality_contribution``), so ``s`` is solved by bisection and ``m`` centres
the range. When ``E`` alone is wider than the stated range, the intercept is as tight as
it can be and a warning says so (the control and seasonality priors are left alone).

The result is written to the adapter's ``settings.priors`` (``saturation_beta``,
``intercept``), which the adapter hands to pymc-marketing as ``model_config`` priors.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.calibration import (
    PRIOR_TRANSLATION_FORMAT,
    BusinessPriors,
    FanChart,
    LiftTest,
    PriorPreview,
    PriorTranslationResult,
)
from mapi_spec.errors import ErrorCode, Issue, Severity

from mapi_plugin_mmm.calibration import (
    SIGMA_MAX,
    SIGMA_MIN,
    TranslationOutcome,
    _covered,
    _issue,
    _labels,
    _lognormal_like,
    _pointer,
    _quantile_spread,
    _range,
    _refuse_degenerate,
    _scaled,
    _share_without_spend,
    _solve_scale,
    _summary,
)
from mapi_plugin_mmm.priors import PriorSpec
from mapi_plugin_mmm.settings import PyMCMarketingSettings

__all__ = [
    "BETA_PARAM",
    "PmmPriorDraws",
    "pmm_prior_draws",
    "pmm_prior_preview",
    "translate_pmm_business_priors",
]

BETA_PARAM: Final[dict[str, str]] = {"logistic": "beta", "hill": "beta", "none": "b"}
"""The multiplicative parameter of each saturation curve (``saturation_<name>``)."""
_DEFAULT_BETA: Final[dict[str, PriorSpec]] = {
    "logistic": PriorSpec.model_validate({"distribution": "HalfNormal", "sigma": 2.0}),
    "hill": PriorSpec.model_validate({"distribution": "HalfNormal", "sigma": 1.5}),
    "none": PriorSpec.model_validate({"distribution": "HalfNormal", "sigma": 2.0}),
}
"""pymc-marketing 1.2's default ``beta`` priors per curve."""


@dataclass(frozen=True, slots=True)
class PmmPriorDraws:
    """Prior draws of a built pymc-marketing model, flattened to ``(sample, cell, ...)``.

    ``media(n, cell, channel)`` is ``A`` (``channel_contribution / beta`` summed over
    time) times ``target_scale``; ``beta(n, cell, channel)`` the curve's multiplier;
    ``intercept(n, cell)``; ``other(n, time)`` controls plus seasonality summed over cells
    and ``kpi(n, time)`` the prior predictive KPI summed over cells, both in KPI units.
    """

    media: np.ndarray
    beta: np.ndarray
    intercept: np.ndarray
    other: np.ndarray
    kpi: np.ndarray | None
    target_scale: float
    n_time: int


def _flat(da: xr.DataArray, dims: Sequence[str], tail: Sequence[str]) -> np.ndarray:
    """``da`` as ``(sample, cell, *tail)`` with the panel ``dims`` flattened into ``cell``."""
    order = ["chain", "draw", *dims, *tail]
    arr = np.asarray(da.transpose(*order).values, dtype=np.float64)
    n = arr.shape[0] * arr.shape[1]
    cells = int(np.prod(arr.shape[2 : 2 + len(dims)])) if dims else 1
    return arr.reshape(n, cells, *arr.shape[2 + len(dims) :])


def pmm_prior_draws(mmm: Any, s: PyMCMarketingSettings, *, draws: int, seed: int) -> PmmPriorDraws:
    """Prior draws of the built pymc-marketing model ``mmm`` (see :class:`PmmPriorDraws`)."""
    import pymc as pm

    model = mmm.model
    beta_name = f"saturation_{BETA_PARAM[s.saturation.kind]}"
    names = [beta_name, "channel_contribution", "intercept_contribution"]
    names += [n for n in ("control_contribution", "yearly_seasonality_contribution")
              if n in model.named_vars]  # fmt: skip
    observed = [rv.name for rv in model.observed_RVs if rv.name == "y"]
    prior = pm.sample_prior_predictive(
        draws=draws, model=model, var_names=names + observed, random_seed=seed
    )
    group = prior["prior"].to_dataset()
    dims = list(s.dims)
    scale = float(np.asarray(mmm.get_scales_as_xarray()["target_scale"]).reshape(-1)[0])
    cc = group["channel_contribution"]
    beta = group[beta_name].broadcast_like(cc.isel(date=0, drop=True))
    contrib = _flat(cc, dims, ["channel", "date"])  # (n, cell, channel, time)
    b = _flat(beta, dims, ["channel"])
    with np.errstate(divide="ignore", invalid="ignore"):
        media = np.where(b > 0, contrib.sum(axis=-1) / b, 0.0) * scale
    icpt = group["intercept_contribution"]
    icpt = icpt.broadcast_like(cc.isel(date=0, channel=0, drop=True)) if dims else icpt
    intercept = _flat(icpt, dims, [])
    n, n_time = contrib.shape[0], contrib.shape[-1]
    other = np.zeros((n, n_time))
    for name in ("control_contribution", "yearly_seasonality_contribution"):
        if name in group:
            da = group[name]
            extra = [str(d) for d in da.dims if d not in ("chain", "draw", "date", *dims)]
            if extra:
                da = da.sum(extra)
            other += _flat(da, dims, ["date"]).sum(axis=1) * scale
    kpi = None
    if observed and "prior_predictive" in prior.children:
        y = prior["prior_predictive"].to_dataset()["y"]
        kpi = _flat(y, dims, ["date"]).sum(axis=1) * scale
    return PmmPriorDraws(
        media=media,
        beta=b,
        intercept=intercept.reshape(n, -1),
        other=other,
        kpi=kpi,
        target_scale=scale,
        n_time=int(n_time),
    )


def _channel_spend(frame: pd.DataFrame, channels: Sequence[str]) -> np.ndarray:
    return np.asarray([float(frame[c].sum()) for c in channels], dtype=np.float64)


def _kpi_total(frame: pd.DataFrame) -> float | None:
    if "y" not in frame:
        return None
    total = float(np.nansum(frame["y"].to_numpy(dtype=float)))
    return total if math.isfinite(total) else None


def translate_pmm_business_priors(
    priors: BusinessPriors,
    draws_: PmmPriorDraws,
    frame: pd.DataFrame,
    channels: Sequence[str],
    s: PyMCMarketingSettings,
    *,
    seed: int = 0,
    pointer: str = "/business_priors",
) -> TranslationOutcome:
    """Solve for ``saturation_beta`` (and ``intercept``) priors matching ``priors`` (see
    the module doc). Unknown channels and channels without spend are ``error`` issues."""
    labels = [str(c) for c in channels]
    n_ch = len(labels)
    issues: list[Issue] = []
    spend_total = _channel_spend(frame, labels)
    kpi_total = _kpi_total(frame)
    declared = s.priors.saturation_beta or _DEFAULT_BETA[s.saturation.kind]
    mu, sigma = _lognormal_like(declared, n_ch, seed + 17)
    default = (mu.copy(), sigma.copy())
    quantity = np.zeros(n_ch, dtype=int)
    lo_t = np.ones(n_ch)
    hi_t = np.ones(n_ch)
    cred = np.full(n_ch, 0.8)
    ranges: dict[str, tuple[str, float, float, float]] = {}
    for label, belief in priors.channels.items():
        here = pointer + _pointer("channels", label)
        if label not in labels:
            issues.append(_issue(
                f"the data has no channel {label!r} (channels: {', '.join(labels)})",
                pointer=here, severity=Severity.ERROR,
                action="Use the channel names exactly as they appear in the data.",
            ))  # fmt: skip
            continue
        c = labels.index(label)
        if belief.roi is not None:
            if spend_total[c] <= 0:
                issues.append(_issue(
                    f"channel {label!r} has no spend in the data, so its ROI is undefined",
                    pointer=here + "/roi", severity=Severity.ERROR,
                    action="State a contribution share for it instead.",
                ))  # fmt: skip
                continue
            lo, hi = _range(label, belief.roi, issues, here + "/roi")
            quantity[c] = 1
            ranges[label] = ("roi", belief.roi[0], belief.roi[1], belief.credibility)
        else:
            assert belief.contribution_share is not None
            if kpi_total is None or kpi_total <= 0:
                issues.append(_issue(
                    "the data has no positive KPI total, so shares are undefined",
                    pointer=here + "/contribution_share", severity=Severity.ERROR,
                ))  # fmt: skip
                continue
            no_spend = _share_without_spend(
                label, float(spend_total[c]), here + "/contribution_share"
            )
            if no_spend is not None:
                issues.append(no_spend)
                continue
            lo, hi = _range(label, belief.contribution_share, issues, here + "/contribution_share")
            quantity[c] = 2
            ranges[label] = ("contribution_share", belief.contribution_share[0],
                             belief.contribution_share[1], belief.credibility)  # fmt: skip
        lo_t[c], hi_t[c], cred[c] = lo, hi, belief.credibility
    stated = quantity > 0
    if stated.any():
        rng = np.random.default_rng(seed)
        log_a = np.log(np.maximum(draws_.media, 1e-300))  # (n, cell, channel)
        eps = rng.standard_normal(log_a.shape)

        def log_r(sig: np.ndarray) -> np.ndarray:
            z = sig[None, None, :] * eps + log_a
            top = z.max(axis=1, keepdims=True)
            return np.asarray(np.log(np.exp(z - top).sum(axis=1)) + top[:, 0, :])

        q = (1 - cred) / 2
        idx = np.flatnonzero(stated)

        def spread(sig: np.ndarray) -> np.ndarray:
            full = sigma.copy()
            full[idx] = sig
            return _quantile_spread(log_r(full)[:, idx], q[idx])[0]

        target = np.log(hi_t[idx] / lo_t[idx])
        solved, narrow, wide = _solve_scale(spread, target, SIGMA_MIN, SIGMA_MAX)
        sigma[idx] = solved
        centre = _quantile_spread(log_r(sigma)[:, idx], q[idx])[1]
        denom = np.where(quantity[idx] == 1, spend_total[idx], kpi_total or 1.0)
        mu[idx] = (np.log(lo_t[idx]) + np.log(hi_t[idx])) / 2 + np.log(denom) - centre
        _refuse_degenerate(mu, sigma, default, idx, labels, quantity, ranges, issues, pointer)
        for j, ci in enumerate(idx.tolist()):
            if labels[ci] not in ranges:
                continue
            what = "ROI" if quantity[ci] == 1 else "contribution share"
            if narrow[j]:
                issues.append(_issue(
                    f"the {what} range for {labels[ci]!r} is narrower than the adstock and "
                    "saturation priors allow; the prior is as tight as they permit (see the "
                    "coverage)",
                    pointer=pointer + _pointer("channels", labels[ci]),
                    action="Widen the range, or lower its credibility.",
                ))  # fmt: skip
            elif wide[j]:
                issues.append(_issue(
                    f"the {what} range for {labels[ci]!r} is extremely wide; the prior was "
                    f"capped at sigma {SIGMA_MAX}",
                    pointer=pointer + _pointer("channels", labels[ci]), severity=Severity.INFO,
                ))  # fmt: skip
    out: dict[str, dict[str, Any]] = {
        "saturation_beta": {
            "distribution": "LogNormal",
            "mu": [_sig(v) for v in mu],
            "sigma": [_sig(v) for v in sigma],
            "dims": ["channel"],
        }
    }
    baseline_range: tuple[float, float, float] | None = None
    if priors.baseline_share is not None:
        here = pointer + "/baseline_share"
        if kpi_total is None or kpi_total <= 0:
            issues.append(_issue(
                "the data has no positive KPI total, so the baseline share is undefined",
                pointer=here, severity=Severity.ERROR,
            ))  # fmt: skip
        else:
            lo_b, hi_b = priors.baseline_share.share
            cb = priors.baseline_share.credibility
            baseline_range = (lo_b, hi_b, cb)
            m, s_int, unreachable, factor = _solve_intercept(
                draws_, kpi_total, lo_b, hi_b, cb, seed
            )
            out["intercept"] = {"distribution": "Normal", "mu": _sig(m), "sigma": _sig(s_int)}
            narrowed = _narrowed(s, factor) if factor < 1.0 else {}
            out.update(narrowed)
            if narrowed:
                issues.append(_issue(
                    "to reach the baseline share range, the control and seasonality priors "
                    f"were narrowed by a factor {factor:.2g} ({', '.join(sorted(narrowed))})",
                    pointer=here + "/share", severity=Severity.INFO,
                ))  # fmt: skip
            if unreachable:
                issues.append(_issue(
                    "the baseline share range is narrower than the control and seasonality "
                    "priors allow; the intercept is as tight as it can be (see the coverage)",
                    pointer=here + "/share",
                    action="Widen the range, or lower its credibility.",
                ))  # fmt: skip
    return TranslationOutcome(
        priors=out, channels=labels, issues=issues, ranges=ranges, baseline_range=baseline_range
    )


def _solve_intercept(
    d: PmmPriorDraws, y_total: float, lo: float, hi: float, cred: float, seed: int
) -> tuple[float, float, bool, float]:
    """``(m, s, unreachable, factor)`` of ``intercept ~ Normal(m, s)`` per cell (module
    doc). When the controls and seasonality alone spread the baseline share over more than
    80% of the stated width, their priors are narrowed by ``factor`` (< 1) first: their
    contributions are linear in the coefficients, so the spread shrinks by the same
    factor."""
    cells = d.intercept.shape[1]
    a = d.target_scale * d.n_time * cells / y_total
    e = d.other.sum(axis=1) / y_total  # (n,)
    zbar = np.random.default_rng(seed + 29).standard_normal(e.shape) / math.sqrt(cells)
    q = np.asarray([(1 - cred) / 2])
    width = hi - lo
    e_spread = float(_quantile_spread(e[:, None], q)[0][0])
    factor = 1.0
    if e_spread > 0.8 * width:
        factor = max(0.8 * width / e_spread, 1e-6)
        e = e * factor

    def share(scale: np.ndarray) -> np.ndarray:
        return np.asarray(a * scale[None, :] * zbar[:, None] + e[:, None])

    def spread(scale: np.ndarray) -> np.ndarray:
        return _quantile_spread(share(scale), np.repeat(q, scale.size))[0]

    s_max = max(10.0 * width / max(a, 1e-12), 1e-6)
    solved, narrow, _ = _solve_scale(spread, np.asarray([width]), 1e-9, s_max)
    centre = float(_quantile_spread(share(solved), q)[1][0])
    floor = 0.01 * width / max(a, 1e-12)  # an intercept spread of 1% of the range at least
    m = ((lo + hi) / 2 - centre) / a
    return m, float(max(solved[0], floor)), bool(narrow[0]), factor


def pmm_prior_preview(
    outcome: TranslationOutcome,
    d: PmmPriorDraws,
    frame: pd.DataFrame,
    *,
    draws: int,
    seed: int,
    quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
    lift_tests: Sequence[LiftTest] = (),
    kpi_units: str | None = None,
    spend_units: str | None = None,
) -> tuple[PriorTranslationResult, xr.Dataset]:
    """The ``priors.json`` document and the plot draws from the *translated* model's prior
    draws ``d`` (rebuild the model with ``outcome.priors`` first)."""
    roi_units = f"{kpi_units} per {spend_units}" if kpi_units and spend_units else None
    labels = outcome.channels
    n_ch = len(labels)
    spend_total = _channel_spend(frame, labels)
    kpi_total = _kpi_total(frame)
    contrib = (d.beta * d.media).sum(axis=1)  # (n, channel) KPI units
    with np.errstate(divide="ignore", invalid="ignore"):
        roi = np.where(spend_total[None, :] > 0, contrib / spend_total[None, :], np.nan)
        share = contrib / kpi_total if kpi_total else np.full_like(contrib, np.nan)
    base = d.intercept.sum(axis=1) * d.target_scale * d.n_time + d.other.sum(axis=1)
    base_share = base / kpi_total if kpi_total else np.full(base.shape, np.nan)
    times = np.asarray(pd.DatetimeIndex(frame["date"].unique()).sort_values().values)
    preview = PriorPreview(
        roi={labels[c]: _summary(roi[:, c], quantiles, roi_units) for c in range(n_ch)},
        contribution_share={labels[c]: _summary(share[:, c], quantiles, None) for c in range(n_ch)},
        baseline_share=_summary(base_share, quantiles, None) if kpi_total else None,
        kpi_fan=_fan(d.kpi, frame, times, quantiles, kpi_units) if d.kpi is not None else None,
    )
    coverage: dict[str, float] = {}
    for label, (what, lo, hi, _cred) in outcome.ranges.items():
        c = labels.index(label)
        coverage[label] = _covered(roi[:, c] if what == "roi" else share[:, c], lo, hi)
    if outcome.baseline_range is not None and kpi_total:
        coverage["baseline"] = _covered(base_share, *outcome.baseline_range[:2])
    issues = list(outcome.issues) + _lift_checks(lift_tests, labels, roi)
    result = PriorTranslationResult(
        format=PRIOR_TRANSLATION_FORMAT,
        channels=labels,
        priors=outcome.priors,
        preview=preview,
        coverage=coverage,
        draws=draws,
        seed=seed,
        issues=issues[:256],
    )
    sample = np.arange(roi.shape[0])
    ch = {"sample": sample, "channel": labels}
    data: dict[str, xr.DataArray] = {
        "roi": xr.DataArray(roi, dims=["sample", "channel"], coords=ch),
        "contribution_share": xr.DataArray(share, dims=["sample", "channel"], coords=ch),
        "baseline_share": xr.DataArray(base_share, dims=["sample"], coords={"sample": sample}),
    }
    if d.kpi is not None:
        data["kpi"] = xr.DataArray(d.kpi, dims=["sample", "time"],
                                   coords={"sample": sample, "time": times})  # fmt: skip
        observed = frame.groupby("date")["y"].sum() if "y" in frame else None
        if observed is not None:
            data["kpi_observed"] = xr.DataArray(
                observed.sort_index().to_numpy(dtype=float), dims=["time"], coords={"time": times}
            )
    stated = {name: rng for name, rng in outcome.ranges.items()}
    coords_ch = {"channel": labels}
    data["stated_quantity"] = xr.DataArray(
        np.asarray([stated[c][0] if c in stated else "" for c in labels]).astype(str),
        dims=["channel"], coords=coords_ch,
    )  # fmt: skip
    for k, key in ((1, "stated_low"), (2, "stated_high"), (3, "stated_credibility")):
        data[key] = xr.DataArray(
            np.asarray([float(stated[c][k]) if c in stated else np.nan for c in labels]),
            dims=["channel"], coords=coords_ch,
        )  # fmt: skip
    ds = xr.Dataset(data)
    if roi_units:
        ds["roi"].attrs["units"] = roi_units
    if kpi_units and "kpi" in ds:
        ds["kpi"].attrs["units"] = kpi_units
    if outcome.baseline_range is not None:
        lo_b, hi_b, cb = outcome.baseline_range
        ds["baseline_share"].attrs.update({"range_low": lo_b, "range_high": hi_b,
                                           "credibility": cb})  # fmt: skip
    return result, ds


def _fan(
    kpi: np.ndarray,
    frame: pd.DataFrame,
    times: np.ndarray,
    quantiles: Sequence[float],
    units: str | None = None,
) -> FanChart:
    qs = sorted({min(max(float(q), 1e-6), 1 - 1e-6) for q in quantiles})[:16]
    values = np.quantile(kpi, qs, axis=0)
    observed: list[float | None] | None = None
    if "y" in frame:
        tot = frame.groupby("date")["y"].sum().sort_index().to_numpy(dtype=float)
        observed = [float(v) if math.isfinite(v) else None for v in tot]
    return FanChart(
        dim="time",
        coords=_labels(times),
        quantiles=qs,
        values=[[float(v) if math.isfinite(v) else None for v in row] for row in values],
        observed=observed,
        units=units,
    )


def _lift_checks(tests: Sequence[LiftTest], labels: list[str], roi: np.ndarray) -> list[Issue]:
    """Lift tests on unknown channels (error) and tests whose implied ROI falls outside the
    90% prior ROI interval (info)."""
    out: list[Issue] = []
    for i, test in enumerate(tests):
        here = _pointer("lift_tests", i)
        if test.channel not in labels:
            out.append(_issue(f"lift test on {test.channel!r}: the data has no such channel",
                              pointer=here + "/channel", severity=Severity.ERROR,
                              code=ErrorCode.SCHEMA_INVALID))  # fmt: skip
            continue
        v = roi[:, labels.index(test.channel)]
        v = v[np.isfinite(v)]
        if not v.size:
            continue
        lo, hi = np.quantile(v, [0.05, 0.95])
        if not lo <= test.implied_roi <= hi:
            out.append(_issue(
                f"the lift test on {test.channel!r} implies an ROI of {test.implied_roi:.3g}, "
                f"outside the 90% prior range [{lo:.3g}, {hi:.3g}]; the fit will weigh both",
                pointer=here, severity=Severity.INFO,
            ))  # fmt: skip
    return out


_DEFAULT_GAMMA: Final[dict[str, PriorSpec]] = {
    "gamma_control": PriorSpec.model_validate({"distribution": "Normal", "mu": 0, "sigma": 2}),
    "gamma_fourier": PriorSpec.model_validate({"distribution": "Laplace", "mu": 0, "b": 1}),
}
"""pymc-marketing 1.2's default control and Fourier coefficient priors."""


def _narrowed(s: PyMCMarketingSettings, factor: float) -> dict[str, dict[str, Any]]:
    """The control (``gamma_control``) and seasonality (``gamma_fourier``) priors of the
    model scaled by ``factor`` (only families closed under scaling)."""
    out: dict[str, dict[str, Any]] = {}
    used = {"gamma_control": bool(s.controls), "gamma_fourier": bool(s.yearly_seasonality)}
    for name, present in used.items():
        if not present:
            continue
        spec = getattr(s.priors, name) or _DEFAULT_GAMMA[name]
        scaled = _scaled(spec, factor)
        if scaled is not None:
            out[name] = scaled.model_dump(mode="json")
    return out


def _sig(value: float) -> float:
    """``value`` to 6 significant digits (small scales must not round to zero)."""
    return float(f"{float(value):.6g}")
