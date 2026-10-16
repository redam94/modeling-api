"""Business priors and lift tests for ``mmm.hierarchical_geo`` (plan §11 Guided task 3; M3).

Everything here works on the model-stage data (canonical names: ``y(geo, time)``,
``spend(geo, channel, time, lag)``) plus a ``to_original`` callback that maps model units
back to business units with the fitted (or, at draft time, data-derived) preprocessing
state. Without a callback model units *are* business units.

Scales
======

:func:`model_scales` reads the original units per model unit of the KPI (``target_scale``,
per geo and period) and of spend (``spend_scale``, per geo, channel and period) by sending
arrays of ones through ``to_original``, and checks that the mapping is linear (``f(2x) =
2 f(x)``): business priors and lift tests are statements about *amounts*, which only
translate exactly through scalings (``mean_scale``, ``max_abs_scale``, currency
conversion), not through ``log1p`` or ``standardize`` of the KPI (``CALIBRATION_UNSUPPORTED``).

Business priors → model-space priors
====================================

With the model's channel effect ``beta[g, c]`` and the saturated, carried-over spend
``s[g, c, t]`` (a function of the adstock and saturation parameters), the channel's
contribution over the fit window in KPI units is::

    C_c = sum_{g,t} target_scale[g, t] * beta[g, c] * s[g, c, t]

and the two business quantities are ``ROI_c = C_c / spend_c`` (``spend_c`` the channel's
total spend over the window, spend units) and ``share_c = C_c / Y`` (``Y`` the observed KPI
total). The translation gives ``beta_channel`` a ``LogNormal(mu_c, sigma_c)`` prior
(``beta_channel`` is the median channel effect across geos):

* ``partial``/``complete`` pooling: ``beta[g, c] = beta_channel[c] * exp(beta_sigma[c] *
  z[g, c])`` (``z = 0`` without pooling across geos), so with ``beta_channel = exp(mu +
  sigma * eps)``::

      log ROI_c = mu_c + sigma_c * eps_c + log K_c - log spend_c,
      K_c = sum_{g,t} target_scale * exp(beta_sigma * z) * s

* ``none`` pooling: every geo draws its own ``beta[g, c] = exp(mu + sigma * eps[g, c])``, so
  ``log ROI_c = mu_c + log sum_g exp(sigma_c * eps[g, c]) * A[g, c] - log spend_c``.

In both cases ``mu_c`` only *shifts* ``log ROI_c`` (every term is proportional to
``exp(mu_c)``) and ``sigma_c`` only *widens* it. ``K``/``A`` depend on the other priors
alone (adstock, saturation, pooling spread), which are independent of ``beta_channel``, so
they are sampled once by Monte Carlo (``draws`` common random numbers). The stated range
``[low, high]`` held with credibility ``p`` fixes the ``q = (1 - p) / 2`` and ``1 - q``
quantiles of ``log ROI_c``:

1. ``sigma_c`` solves ``Q_{1-q}(R(sigma)) - Q_q(R(sigma)) = log(high / low)`` by bisection,
   vectorised over channels, where ``R(sigma)`` is ``log ROI_c`` with ``mu_c = 0`` - the
   width of the ROI interval grows with ``sigma``;
2. ``mu_c`` then centres the interval: ``mu_c = (log low + log high) / 2 - (Q_q + Q_{1-q})
   / 2``.

Both quantiles therefore match the stated ones up to Monte Carlo error. If the other priors
alone already make the interval wider than stated, ``sigma_c`` stops at its floor and a
warning says the stated range cannot be met (the reported ``coverage`` shows by how much);
an interval wider than ``sigma_max`` allows is capped the same way. A share range works
identically with ``Y`` in place of ``spend_c``. A range starting at 0 is read as
``[high / 20, high]`` (a log-normal has no mass at 0), with an ``info`` issue. Channels
without a belief keep their prior, refitted as the log-normal with the same 5% and 95%
quantiles, so ``beta_channel`` stays one per-channel log-normal.

The baseline share (KPI not explained by media: the geo intercepts plus the controls) is
linear in the intercept's population mean ``intercept ~ Normal(m, s)``: ``B = a (m + s eps)
+ E`` with ``a = sum_{g,t} target_scale / Y`` and ``E`` the spread from the intercept's geo
spread and the control effects, so the same two-step solve (scale by bisection, then
location) runs in linear space.

The prior preview then samples the *translated* priors afresh (another seed) and reports
the ROI, share, baseline share and a KPI fan in business units, plus the prior mass inside
each stated range (``coverage``, close to the stated credibility when the translation is
faithful).

Lift tests
==========

A lift test says that ``spend_delta`` extra spend on channel ``c`` (in one geo, or spread
over all geos) during ``[start, end]`` caused ``kpi_delta`` incremental KPI. The model's
implied lift over the same window is::

    lift = sum_{g, t in W} target_scale[g, t] * beta[g, c] *
           (sat(adstock(x + dx))[g, c, t] - sat(adstock(x))[g, c, t])

where ``W`` are the model periods whose label lies in ``[start, end]``, ``x`` the lag
tensor and ``dx`` the extra spend in model units, spread over the window's cells in
proportion to the channel's actual spend there (evenly when there was none) and carried
over through the lag tensor (``dx[t - l]`` at lag ``l``). Carry-over past ``end`` is not
counted: tests report the lift measured during the test. Each test adds
``kpi_delta ~ Normal(lift, se)`` to the likelihood (``se`` from the test, or from its
confidence interval) - pymc-marketing's ``add_lift_test_measurements`` uses the
saturation curve alone; this version also carries the extra spend over. A negative
``spend_delta`` (a holdout) may remove at most the spend the data shows.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

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

from mapi_plugin_mmm.design import (
    CHANNEL,
    GEO,
    TARGET,
    TIME,
    control_design,
    control_vars,
    spend_tensor,
)
from mapi_plugin_mmm.priors import PriorSpec
from mapi_plugin_mmm.settings import HierarchicalGeoSettings
from mapi_plugin_mmm.transforms import adstock_weights, saturate, saturation_params

__all__ = [
    "LIFT_DIM",
    "LIFT_VAR",
    "SIGMA_MAX",
    "SIGMA_MIN",
    "LiftDesign",
    "ModelScales",
    "PriorDraws",
    "TranslationOutcome",
    "draw_prior",
    "lift_design",
    "model_scales",
    "prior_preview",
    "translate_business_priors",
]

ToOriginal = Callable[[xr.DataArray, str], xr.DataArray]

LIFT_VAR: Final = "lift_tests"
"""Name of the observed lift-test variable in the PyMC model."""
LIFT_DIM: Final = "lift_test"
SIGMA_MIN: Final = 0.02
"""Floor of a translated log-normal's sigma (a stated range narrower than the other priors
allow is met as closely as this permits)."""
SIGMA_MAX: Final = 4.0
MU_MAX: Final = 50.0
"""Largest ``|mu|`` of a translated log-normal: beyond it (a coefficient of e^50 in model
units) the stated range cannot be met by the model's media effect, and the solve is refused."""
_ZERO_LOW_RATIO: Final = 20.0
_BISECT_ITERS: Final = 60
_CHUNK_CELLS: Final = 2_000_000
_LINEAR_RTOL: Final = 1e-6


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
        code=code,
        severity=severity,
        pointer=pointer,
        message=message[:2000],
        suggested_action=action,
    )


# ============================================================================== scales


@dataclass(frozen=True, slots=True)
class ModelScales:
    """Business units of the model-stage data (see the module doc).

    ``target_scale(geo, time)`` and ``spend_scale(geo, channel, time)`` are original units
    per model unit; ``spend(geo, channel, time)`` is each period's own spend and ``kpi(geo,
    time)`` the observed KPI, both in original units (``kpi`` is ``None`` when the data has
    no target, e.g. a score payload).
    """

    geos: np.ndarray
    channels: np.ndarray
    times: np.ndarray
    x: np.ndarray  # (geo, channel, time, lag) model units
    target_scale: np.ndarray
    spend_scale: np.ndarray
    spend: np.ndarray
    kpi: np.ndarray | None
    kpi_units: str | None = None
    spend_units: str | None = None

    @property
    def spend_total(self) -> np.ndarray:
        """``(channel,)`` total spend over the window (original units)."""
        return np.asarray(self.spend.sum(axis=(0, 2)), dtype=np.float64)

    @property
    def kpi_total(self) -> float | None:
        return None if self.kpi is None else float(np.nansum(self.kpi))

    def channel_index(self, label: str) -> int | None:
        hits = np.flatnonzero(self.channels.astype(str) == str(label))
        return int(hits[0]) if hits.size else None

    def geo_index(self, label: str) -> int | None:
        hits = np.flatnonzero(self.geos.astype(str) == str(label))
        return int(hits[0]) if hits.size else None


def _units(da: xr.DataArray) -> str | None:
    u = da.attrs.get("units")
    return u[:64] if isinstance(u, str) and u else None


def _ratio(to_original: ToOriginal | None, ones: xr.DataArray, src: str) -> tuple[Any, str | None]:
    """Original units per model unit of ``src`` over ``ones``' cells (checked linear)."""
    if to_original is None:
        return np.ones(ones.shape), None
    one = to_original(ones, src).transpose(*ones.dims)
    two = to_original(ones * 2.0, src).transpose(*ones.dims)
    zero = to_original(ones * 0.0, src).transpose(*ones.dims)
    a = np.asarray(one.values, dtype=np.float64)
    b = np.asarray(two.values, dtype=np.float64)
    z = np.asarray(zero.values, dtype=np.float64)
    finite = np.isfinite(a) & np.isfinite(b) & np.isfinite(z)
    linear = (
        finite
        & np.isclose(b, 2 * a, rtol=_LINEAR_RTOL, atol=1e-12)
        & np.isclose(z, 0.0, atol=1e-9 * (1 + np.abs(a)))
    )
    if not linear.all() or (a <= 0).any():
        raise MapiError(
            f"business priors and lift tests need the pipeline to scale {src!r} linearly "
            "(mean_scale, max_abs_scale, currency conversion); a log, logit or "
            "standardising step on it makes amounts in business units untranslatable",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer="/calibration",
        )
    return a, _units(one)


def model_scales(
    ds: xr.Dataset,
    s: HierarchicalGeoSettings,
    to_original: ToOriginal | None,
) -> ModelScales:
    """The business units of the model-stage dataset ``ds`` (see :class:`ModelScales`)."""
    x = spend_tensor(ds, s.lag_dim)
    geos = np.asarray(ds[GEO].values)
    channels = np.asarray(ds[CHANNEL].values)
    times = np.asarray(ds[TIME].values)
    ones_y = xr.DataArray(
        np.ones((geos.size, times.size)), dims=[GEO, TIME], coords={GEO: geos, TIME: times}
    )
    ones_x = xr.DataArray(
        np.ones((geos.size, channels.size, times.size)),
        dims=[GEO, CHANNEL, TIME],
        coords={GEO: geos, CHANNEL: channels, TIME: times},
    )
    target_scale, kpi_units = _ratio(to_original, ones_y, TARGET)
    spend_scale, spend_units = _ratio(to_original, ones_x, "spend")
    spend = x[..., 0] * spend_scale
    kpi = None
    if TARGET in ds:
        y = np.asarray(ds[TARGET].transpose(GEO, TIME).values, dtype=np.float64)
        kpi = y * target_scale
    return ModelScales(
        geos=geos,
        channels=channels,
        times=times,
        x=x,
        target_scale=np.asarray(target_scale),
        spend_scale=np.asarray(spend_scale),
        spend=np.asarray(spend),
        kpi=kpi,
        kpi_units=kpi_units,
        spend_units=spend_units,
    )


# ============================================================================ prior draws


def draw_prior(
    spec: PriorSpec,
    n: int,
    *,
    seed: int,
    dims: Sequence[str] = (),
    iid: Sequence[int] = (),
    sizes: Mapping[str, int] | None = None,
) -> np.ndarray:
    """``n`` draws of a prior as the model builds it (:func:`~mapi_plugin_mmm.priors.
    build_prior`): ``(n, *iid, *[sizes[d] for d in dims if d in spec.dims])``."""
    import pymc as pm

    used = [d for d in dims if d in spec.dims]
    shape = (*iid, *[int((sizes or {})[d]) for d in used])
    kwargs: dict[str, Any] = {}
    for key, value in spec.params.items():
        kwargs[key] = np.asarray(value, dtype=np.float64) if isinstance(value, list) else value
    dist = getattr(pm, spec.distribution).dist(**kwargs, shape=shape or None)
    out = pm.draw(dist, draws=n, random_seed=seed)
    return np.asarray(out, dtype=np.float64).reshape((n, *shape))


def _per_channel(values: np.ndarray, n_ch: int) -> np.ndarray:
    """``(n, channel)`` from draws that may be shared across channels (``(n,)``)."""
    return values if values.ndim == 2 else np.repeat(values[:, None], n_ch, axis=1)


@dataclass(slots=True)
class PriorDraws:
    """Monte Carlo draws of every parameter except ``beta_channel`` (see the module doc).

    ``weights(n, channel, lag)`` and ``sat`` are the adstock weights and saturation
    parameters; ``geo_factor(n, geo, channel)`` is ``exp(beta_sigma * z)`` (partial
    pooling), ones (complete) or the standard normals ``eps[g, c]`` of per-geo effects
    (none). ``eps_channel(n, channel)`` and ``eps_baseline(n,)`` are the standard normals
    of ``beta_channel`` and the intercept mean.
    """

    n: int
    weights: np.ndarray
    sat: dict[str, np.ndarray]
    geo_factor: np.ndarray
    eps_channel: np.ndarray
    intercept_mu: np.ndarray  # (n,)
    intercept_sigma: np.ndarray  # (n,)
    z_intercept: np.ndarray  # (n, geo)
    eps_baseline: np.ndarray  # (n,)
    gamma: np.ndarray  # (n, feature)
    sigma: np.ndarray  # (n,)
    noise_seed: int

    @classmethod
    def sample(
        cls,
        priors: Mapping[str, PriorSpec],
        s: HierarchicalGeoSettings,
        *,
        n: int,
        n_geo: int,
        n_ch: int,
        n_lag: int,
        n_feat: int,
        seed: int,
    ) -> PriorDraws:
        seeds = np.random.default_rng(seed).integers(0, 2**31 - 1, size=16)
        sizes = {CHANNEL: n_ch}
        ch = [CHANNEL]

        def per_channel(name: str, k: int) -> np.ndarray:
            raw = draw_prior(priors[name], n, seed=int(seeds[k]), dims=ch, sizes=sizes)
            return _per_channel(raw, n_ch)

        alpha = per_channel("alpha", 0)
        theta = per_channel("theta", 1)[..., None] if s.adstock.kind == "delayed" else None
        weights = np.asarray(
            adstock_weights(
                s.adstock.kind,
                np.arange(n_lag),
                alpha=alpha[..., None],
                theta=theta,
                normalize=s.adstock.normalize,
            )
        )
        sat = {p: per_channel(p, 2 + i) for i, p in enumerate(saturation_params(s.saturation.kind))}
        rng = np.random.default_rng(int(seeds[5]))
        if s.pooling == "partial":
            bs = per_channel("beta_sigma", 6)
            z = rng.standard_normal((n, n_geo, n_ch))
            geo_factor = np.exp(bs[:, None, :] * z)
        elif s.pooling == "complete":
            geo_factor = np.ones((n, n_geo, n_ch))
        else:
            geo_factor = rng.standard_normal((n, n_geo, n_ch))
        gamma = (
            draw_prior(priors["gamma_control"], n, seed=int(seeds[7]), iid=(n_feat,))
            if n_feat
            else np.zeros((n, 0))
        )
        return cls(
            n=n,
            weights=weights,
            sat=sat,
            geo_factor=geo_factor,
            eps_channel=rng.standard_normal((n, n_ch)),
            intercept_mu=draw_prior(priors["intercept"], n, seed=int(seeds[8])),
            intercept_sigma=draw_prior(priors["intercept_sigma"], n, seed=int(seeds[9])),
            z_intercept=rng.standard_normal((n, n_geo)),
            eps_baseline=rng.standard_normal(n),
            gamma=gamma.reshape(n, n_feat),
            sigma=draw_prior(priors["sigma"], n, seed=int(seeds[10])),
            noise_seed=int(seeds[11]),
        )


def _media(
    scales: ModelScales,
    draws: PriorDraws,
    s: HierarchicalGeoSettings,
    beta: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray | None]:
    """``A(n, geo, channel) = sum_t target_scale * s`` and, with ``beta(n, geo, channel)``,
    the media KPI per period ``(n, time) = sum_{g,c} target_scale * beta * s``."""
    x = scales.x
    n_geo, n_ch, n_time, _ = x.shape
    ts = scales.target_scale  # (geo, time)
    step = max(1, _CHUNK_CELLS // max(1, n_geo * n_ch * n_time))
    a_out = np.empty((draws.n, n_geo, n_ch))
    m_out = None if beta is None else np.empty((draws.n, n_time))
    for lo in range(0, draws.n, step):
        hi = min(draws.n, lo + step)
        ad = np.einsum("gctl,scl->sgct", x, draws.weights[lo:hi])
        params = {k: v[lo:hi, None, :, None] for k, v in draws.sat.items()}
        sat = np.asarray(saturate(ad, s.saturation.kind, **params))
        weighted = sat * ts[None, :, None, :]
        a_out[lo:hi] = weighted.sum(axis=3)
        if beta is not None and m_out is not None:
            m_out[lo:hi] = np.einsum("sgct,sgc->st", weighted, beta[lo:hi])
    return a_out, m_out


# ================================================================== the bisection solve


def _quantile_spread(values: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(spread, centre)`` of the central interval ``[Q_q, Q_{1-q}]`` along axis 0."""
    lo = np.quantile(values, q, axis=0) if q.ndim == 0 else _q_rows(values, q)
    hi = np.quantile(values, 1 - q, axis=0) if q.ndim == 0 else _q_rows(values, 1 - q)
    return hi - lo, (hi + lo) / 2


def _q_rows(values: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Per-column quantile ``q[c]`` of ``values(n, c)``."""
    srt = np.sort(values, axis=0)
    n = srt.shape[0]
    pos = q * (n - 1)
    lo = np.floor(pos).astype(int)
    hi = np.minimum(lo + 1, n - 1)
    cols = np.arange(values.shape[1])
    frac = pos - lo
    return np.asarray(srt[lo, cols] + frac * (srt[hi, cols] - srt[lo, cols]))


def _solve_scale(
    spread_of: Callable[[np.ndarray], np.ndarray],
    target: np.ndarray,
    lo: float,
    hi: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per column, the scale in ``[lo, hi]`` whose spread equals ``target`` (bisection;
    the spread grows with the scale). Returns ``(scale, too_narrow, too_wide)``: the masks
    of columns whose target lies below the spread at ``lo`` / above the spread at ``hi``."""
    k = target.shape[0]
    s_lo = np.full(k, lo)
    s_hi = np.full(k, hi)
    too_narrow = spread_of(s_lo) >= target
    too_wide = spread_of(s_hi) <= target
    for _ in range(_BISECT_ITERS):
        mid = (s_lo + s_hi) / 2
        up = spread_of(mid) < target
        s_lo = np.where(up, mid, s_lo)
        s_hi = np.where(up, s_hi, mid)
    out = (s_lo + s_hi) / 2
    out = np.where(too_narrow, lo, np.where(too_wide, hi, out))
    return out, too_narrow, too_wide


# ======================================================================== translation


@dataclass(slots=True)
class TranslationOutcome:
    """The translated priors (``settings.priors`` dict form), with what was solved."""

    priors: dict[str, dict[str, Any]]
    channels: list[str]
    issues: list[Issue] = field(default_factory=list)
    ranges: dict[str, tuple[str, float, float, float]] = field(default_factory=dict)
    """``channel -> (quantity, low, high, credibility)`` of every stated channel belief."""
    baseline_range: tuple[float, float, float] | None = None


def _lognormal_like(spec: PriorSpec, n_ch: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """``(mu, sigma)`` per channel of the log-normal with the 5%/95% quantiles of ``spec``."""
    if spec.distribution == "LogNormal" and set(spec.params) == {"mu", "sigma"}:
        mu = np.broadcast_to(np.asarray(spec.params["mu"], dtype=float), (n_ch,))
        sd = np.broadcast_to(np.asarray(spec.params["sigma"], dtype=float), (n_ch,))
        return mu.copy(), sd.copy()
    draws = _per_channel(
        draw_prior(spec, 4000, seed=seed, dims=[CHANNEL], sizes={CHANNEL: n_ch}), n_ch
    )
    draws = np.maximum(draws, 1e-12)
    q05, q95 = np.quantile(draws, [0.05, 0.95], axis=0)
    mu = np.empty(n_ch)
    sd = np.empty(n_ch)
    for c in range(n_ch):
        lo, hi = float(q05[c]), float(max(q95[c], q05[c] * 1.0001))
        mu[c], sd[c] = lognormal_from_range(lo, hi, 0.9)
    return mu, np.clip(sd, SIGMA_MIN, SIGMA_MAX)


def _range(
    label: str, bounds: tuple[float, float], issues: list[Issue], pointer: str
) -> tuple[float, float]:
    lo, hi = bounds
    if lo > 0:
        return lo, hi
    issues.append(
        _issue(
            f"the range for {label!r} starts at 0, which a log-normal prior cannot reach; it "
            f"was read as [{hi / _ZERO_LOW_RATIO:.3g}, {hi:.3g}]",
            pointer=pointer,
            severity=Severity.INFO,
        )
    )
    return hi / _ZERO_LOW_RATIO, hi


def _share_without_spend(label: str, spend: float, pointer: str) -> Issue | None:
    """The ``error`` for a contribution share stated for a channel without spend (its share
    is 0 whatever its coefficient, so no prior can match the range)."""
    if spend > 0:
        return None
    return _issue(
        f"channel {label!r} has no spend in the fit window, so its share of the KPI is 0 "
        "whatever the model believes",
        pointer=pointer,
        severity=Severity.ERROR,
        action="Leave the channel out of business priors until it has spend in the data.",
    )


def _refuse_degenerate(
    mu: np.ndarray,
    sigma: np.ndarray,
    default: tuple[np.ndarray, np.ndarray],
    idx: np.ndarray,
    labels: Sequence[str],
    quantity: np.ndarray,
    ranges: dict[str, tuple[str, float, float, float]],
    issues: list[Issue],
    pointer: str,
) -> None:
    """Put back the default prior of every stated channel whose solved log-normal is not
    finite or has ``|mu| > MU_MAX``, with an ``error`` issue (never emit an absurd prior)."""
    for ci in idx.tolist():
        m, sd = float(mu[ci]), float(sigma[ci])
        if math.isfinite(m) and math.isfinite(sd) and abs(m) <= MU_MAX:
            continue
        label = labels[ci]
        what = "roi" if quantity[ci] == 1 else "contribution_share"
        issues.append(
            _issue(
                f"the {what.replace('_', ' ')} range for {label!r} cannot be met by the "
                f"model's media effect (the prior it needs is degenerate: log-scale centre "
                f"{m:.3g}); the channel keeps its default prior",
                pointer=pointer + _pointer("channels", label, what),
                severity=Severity.ERROR,
                action="Check the channel's spend and the KPI units, or state another range.",
            )
        )
        mu[ci], sigma[ci] = default[0][ci], default[1][ci]
        ranges.pop(label, None)


def translate_business_priors(
    priors: BusinessPriors,
    scales: ModelScales,
    s: HierarchicalGeoSettings,
    ds: xr.Dataset,
    *,
    draws: int = 1000,
    seed: int = 0,
    pointer: str = "/business_priors",
) -> TranslationOutcome:
    """Solve for ``beta_channel`` (and ``intercept``) priors matching ``priors`` (see the
    module doc). Unknown channels and channels without spend are ``error`` issues; the
    returned priors then leave those channels at their (refitted) defaults."""
    resolved = s.resolved_priors()
    n_geo, n_ch, _, n_lag = scales.x.shape
    labels = [str(c) for c in scales.channels]
    issues: list[Issue] = []
    controls = control_vars(ds, s.controls)
    design = control_design(ds, controls)
    pd_ = PriorDraws.sample(
        resolved,
        s,
        n=draws,
        n_geo=n_geo,
        n_ch=n_ch,
        n_lag=n_lag,
        n_feat=len(design.features),
        seed=seed,
    )
    a_cells, _ = _media(scales, pd_, s)  # (n, geo, channel)
    kpi_total = scales.kpi_total
    spend_total = scales.spend_total

    mu, sigma = _lognormal_like(resolved["beta_channel"], n_ch, seed + 17)
    default = (mu.copy(), sigma.copy())
    quantity = np.zeros(n_ch, dtype=int)  # 0 none, 1 roi, 2 share
    lo_t = np.ones(n_ch)
    hi_t = np.ones(n_ch)
    cred = np.full(n_ch, 0.8)
    ranges: dict[str, tuple[str, float, float, float]] = {}
    for label, belief in priors.channels.items():
        here = pointer + _pointer("channels", label)
        c = scales.channel_index(label)
        if c is None:
            issues.append(
                _issue(
                    f"the data has no channel {label!r} (channels: {', '.join(labels)})",
                    pointer=here,
                    severity=Severity.ERROR,
                    action="Use the channel names exactly as they appear in the data.",
                )
            )
            continue
        if belief.roi is not None:
            if spend_total[c] <= 0:
                issues.append(
                    _issue(
                        f"channel {label!r} has no spend in the data, so its ROI is undefined",
                        pointer=here + "/roi",
                        severity=Severity.ERROR,
                        action="State a contribution share for it instead.",
                    )
                )
                continue
            lo, hi = _range(label, belief.roi, issues, here + "/roi")
            quantity[c] = 1
            ranges[label] = ("roi", belief.roi[0], belief.roi[1], belief.credibility)
        else:
            assert belief.contribution_share is not None
            if kpi_total is None or kpi_total <= 0:
                issues.append(
                    _issue(
                        "the data has no positive KPI total, so shares are undefined",
                        pointer=here + "/contribution_share",
                        severity=Severity.ERROR,
                    )
                )
                continue
            no_spend = _share_without_spend(
                label, float(spend_total[c]), here + "/contribution_share"
            )
            if no_spend is not None:
                issues.append(no_spend)
                continue
            lo, hi = _range(label, belief.contribution_share, issues, here + "/contribution_share")
            quantity[c] = 2
            ranges[label] = (
                "contribution_share",
                belief.contribution_share[0],
                belief.contribution_share[1],
                belief.credibility,
            )
        lo_t[c], hi_t[c], cred[c] = lo, hi, belief.credibility

    stated = quantity > 0
    if stated.any():
        log_a = np.log(np.maximum(a_cells, 1e-300))  # (n, geo, channel)
        if s.pooling == "none":
            eps = pd_.geo_factor

            def log_r(sig: np.ndarray) -> np.ndarray:
                z = sig[None, None, :] * eps + log_a
                top = z.max(axis=1, keepdims=True)
                return np.asarray(np.log(np.exp(z - top).sum(axis=1)) + top[:, 0, :])

        else:
            log_k = np.log(np.maximum((pd_.geo_factor * a_cells).sum(axis=1), 1e-300))

            def log_r(sig: np.ndarray) -> np.ndarray:
                return np.asarray(sig[None, :] * pd_.eps_channel + log_k)

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
            label = labels[ci]
            if label not in ranges:
                continue
            what = "ROI" if quantity[ci] == 1 else "contribution share"
            if narrow[j]:
                issues.append(
                    _issue(
                        f"the {what} range for {label!r} is narrower than the adstock, "
                        "saturation and pooling priors allow; the prior is as tight as they "
                        "permit (see the coverage)",
                        pointer=pointer + _pointer("channels", label),
                        action="Widen the range, or lower its credibility.",
                    )
                )
            elif wide[j]:
                issues.append(
                    _issue(
                        f"the {what} range for {label!r} is extremely wide; the prior was "
                        f"capped at sigma {SIGMA_MAX}",
                        pointer=pointer + _pointer("channels", label),
                        severity=Severity.INFO,
                    )
                )

    out: dict[str, dict[str, Any]] = {
        "beta_channel": {
            "distribution": "LogNormal",
            "mu": [round(float(v), 6) for v in mu],
            "sigma": [round(float(v), 6) for v in sigma],
            "dims": [CHANNEL],
        }
    }
    baseline_range: tuple[float, float, float] | None = None
    if priors.baseline_share is not None:
        if kpi_total is None or kpi_total <= 0:
            issues.append(
                _issue(
                    "the data has no positive KPI total, so the baseline share is undefined",
                    pointer=pointer + "/baseline_share",
                    severity=Severity.ERROR,
                )
            )
        else:
            lo_b, hi_b = priors.baseline_share.share
            cb = priors.baseline_share.credibility
            baseline_range = (lo_b, hi_b, cb)
            solve = _solve_baseline(scales, pd_, design.values, resolved, lo_b, hi_b, cb)
            out["intercept"] = {
                "distribution": "Normal",
                "mu": round(solve.m, 6),
                "sigma": round(solve.s, 6),
            }
            out.update(solve.narrowed)
            here = pointer + "/baseline_share/share"
            if solve.narrowed:
                issues.append(
                    _issue(
                        "to reach the baseline share range, the priors on how much geos differ "
                        f"and on the control effects were narrowed by a factor {solve.factor:.2g}"
                        f" ({', '.join(sorted(solve.narrowed))})",
                        pointer=here,
                        severity=Severity.INFO,
                    )
                )
            if solve.unreachable:
                issues.append(
                    _issue(
                        "the baseline share range is narrower than the intercept's spread "
                        "across geos and the control effects allow; the prior is as tight as "
                        "they permit (see the coverage)",
                        pointer=here,
                        action="Widen the range, or lower its credibility.",
                    )
                )
    return TranslationOutcome(
        priors=out, channels=labels, issues=issues, ranges=ranges, baseline_range=baseline_range
    )


def _baseline_parts(
    scales: ModelScales, draws: PriorDraws, design: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """``(geo, controls)``: ``(n,)`` baseline KPI over the window from the geo intercepts'
    spread around their mean (``intercept_sigma * z``) and from the controls."""
    ts = scales.target_scale  # (geo, time)
    w_geo = ts.sum(axis=1)  # (geo,)
    geo = (draws.intercept_sigma[:, None] * draws.z_intercept) @ w_geo
    ctrl = np.zeros(draws.n)
    if design.shape[-1]:
        ctrl = draws.gamma @ np.einsum("gt,gtf->f", ts, design)
    return np.asarray(geo), np.asarray(ctrl)


_SCALE_PARAMS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    # distribution: (params multiplied by k, params divided by k) when the variable is
    # scaled by k > 0 (only families closed under scaling)
    "Normal": (("mu", "sigma"), ()),
    "StudentT": (("mu", "sigma"), ()),
    "Laplace": (("mu", "b"), ()),
    "HalfNormal": (("sigma",), ()),
    "HalfStudentT": (("sigma",), ()),
    "HalfCauchy": (("beta",), ()),
    "Exponential": (("scale",), ("lam",)),
    "Gamma": (("mu", "sigma"), ("beta",)),
}


def _scaled(spec: PriorSpec, k: float) -> PriorSpec | None:
    """The prior of ``k * X`` for ``X ~ spec`` (``None`` if the family is not closed)."""
    rule = _SCALE_PARAMS.get(spec.distribution)
    if rule is None:
        return None
    times, divide = rule
    params: dict[str, Any] = {}
    for key, value in spec.params.items():
        vals = np.asarray(value, dtype=np.float64)
        if key in times:
            vals = vals * k
        elif key in divide:
            vals = vals / k
        params[key] = [float(v) for v in vals] if isinstance(value, list) else float(vals)
    return PriorSpec.model_validate(
        {"distribution": spec.distribution, "dims": spec.dims, **params}
    )


@dataclass(frozen=True, slots=True)
class _BaselineSolve:
    m: float
    s: float
    narrowed: dict[str, dict[str, Any]]
    factor: float
    unreachable: bool


def _solve_baseline(
    scales: ModelScales,
    draws: PriorDraws,
    design: np.ndarray,
    priors: Mapping[str, PriorSpec],
    lo: float,
    hi: float,
    cred: float,
) -> _BaselineSolve:
    """The intercept mean's ``Normal(m, s)`` prior for a baseline-share range.

    When the geo intercepts' spread and the control effects alone make the baseline share
    wider than stated, their priors (``intercept_sigma``, ``gamma_control``, when of a
    family closed under scaling) are narrowed by a common factor ``k`` so the spread fills
    80% of the stated width before ``m`` and ``s`` are solved.
    """
    y_total = scales.kpi_total
    assert y_total is not None and y_total > 0
    a = float(scales.target_scale.sum() / y_total)
    geo, ctrl = _baseline_parts(scales, draws, design)
    names = ("intercept_sigma", "gamma_control")
    scalable = {n: _scaled(priors[n], 0.5) is not None for n in names}
    q = np.asarray([(1 - cred) / 2])
    width = hi - lo

    def rest(k: np.ndarray) -> np.ndarray:  # (n, len(k))
        kg = k if scalable["intercept_sigma"] else np.ones_like(k)
        kc = k if scalable["gamma_control"] else np.ones_like(k)
        return np.asarray((geo[:, None] * kg[None, :] + ctrl[:, None] * kc[None, :]) / y_total)

    def rest_spread(k: np.ndarray) -> np.ndarray:
        return _quantile_spread(rest(k), np.repeat(q, k.size))[0]

    factor = 1.0
    if float(rest_spread(np.ones(1))[0]) > 0.8 * width and any(scalable.values()):
        k, narrow_k, _ = _solve_scale(rest_spread, np.asarray([0.8 * width]), 1e-6, 1.0)
        factor = 1e-6 if bool(narrow_k[0]) else float(k[0])
    e = rest(np.asarray([factor]))[:, 0]

    def share(scale: np.ndarray) -> np.ndarray:
        return np.asarray(a * scale[None, :] * draws.eps_baseline[:, None] + e[:, None])

    def spread(scale: np.ndarray) -> np.ndarray:
        return _quantile_spread(share(scale), np.repeat(q, scale.size))[0]

    s_max = max(10.0 * width / max(a, 1e-12), 1e-6)
    solved, narrow, _ = _solve_scale(spread, np.asarray([width]), 1e-9, s_max)
    centre = float(_quantile_spread(share(solved), q)[1][0])
    narrowed: dict[str, dict[str, Any]] = {}
    if factor < 1.0:
        for n in names:
            spec = _scaled(priors[n], factor) if scalable[n] else None
            if spec is not None:
                narrowed[n] = spec.model_dump(mode="json")
    return _BaselineSolve(
        m=((lo + hi) / 2 - centre) / a,
        s=float(max(solved[0], 1e-6)),
        narrowed=narrowed,
        factor=factor,
        unreachable=bool(narrow[0]),
    )


# ============================================================================ preview


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
        quantiles={_qkey(q): float(x) for q, x in zip(quantiles, qs, strict=True)},
        units=units,
    )


def _qkey(q: float) -> str:
    return f"{q:g}"


def _labels(times: np.ndarray) -> list[str | int | float]:
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
    scales: ModelScales,
    s: HierarchicalGeoSettings,
    ds: xr.Dataset,
    *,
    draws: int,
    seed: int,
    quantiles: Sequence[float] = (0.05, 0.25, 0.5, 0.75, 0.95),
    lift_tests: Sequence[LiftTest] = (),
) -> tuple[PriorTranslationResult, xr.Dataset]:
    """Sample the translated priors afresh and summarise them in business units.

    Returns the ``priors.json`` document and the draws for plots (``roi``,
    ``contribution_share``, ``baseline_share``, ``kpi`` and ``kpi_observed``).
    """
    merged = HierarchicalGeoSettings.model_validate(
        {
            **s.model_dump(mode="json", exclude_none=True),
            "priors": {
                **s.priors.model_dump(mode="json", exclude_none=True),
                **outcome.priors,
            },
        }
    )
    resolved = merged.resolved_priors()
    n_geo, n_ch, n_time, n_lag = scales.x.shape
    controls = control_vars(ds, s.controls)
    design = control_design(ds, controls)
    pd_ = PriorDraws.sample(
        resolved,
        merged,
        n=draws,
        n_geo=n_geo,
        n_ch=n_ch,
        n_lag=n_lag,
        n_feat=len(design.features),
        seed=seed,
    )
    bc = _per_channel(
        draw_prior(
            resolved["beta_channel"],
            draws,
            seed=pd_.noise_seed + 1,
            dims=[CHANNEL],
            sizes={CHANNEL: n_ch},
        ),
        n_ch,
    )
    if merged.pooling == "none":
        spec = resolved["beta_channel"]
        mu = np.broadcast_to(np.asarray(spec.params.get("mu", 0.0), dtype=float), (n_ch,))
        sd = np.broadcast_to(np.asarray(spec.params.get("sigma", 1.0), dtype=float), (n_ch,))
        if spec.distribution == "LogNormal":
            beta = np.exp(mu[None, None, :] + sd[None, None, :] * pd_.geo_factor)
        else:  # not translated: per-geo draws of the declared prior
            beta = draw_prior(
                spec, draws, seed=pd_.noise_seed + 2, dims=[CHANNEL], iid=(n_geo,),
                sizes={CHANNEL: n_ch},
            )  # fmt: skip
            beta = beta if beta.ndim == 3 else np.repeat(beta[..., None], n_ch, axis=2)
    else:
        beta = bc[:, None, :] * pd_.geo_factor
    a_cells, media_t = _media(scales, pd_, merged, beta)
    assert media_t is not None
    contrib = (beta * a_cells).sum(axis=1)  # (n, channel)
    spend_total = scales.spend_total
    kpi_total = scales.kpi_total
    with np.errstate(divide="ignore", invalid="ignore"):
        roi = np.where(spend_total[None, :] > 0, contrib / spend_total[None, :], np.nan)
        share = contrib / kpi_total if kpi_total else np.full_like(contrib, np.nan)
    # baseline and the KPI per period
    ts = scales.target_scale
    intercept = pd_.intercept_mu[:, None] + pd_.intercept_sigma[:, None] * pd_.z_intercept
    base_t = np.einsum("sg,gt->st", intercept, ts)
    if design.values.shape[-1]:
        base_t = base_t + np.einsum("gtf,sf,gt->st", design.values, pd_.gamma, ts)
    noise = np.random.default_rng(pd_.noise_seed).standard_normal((draws, n_geo, n_time))
    noise_t = np.einsum("sgt,gt->st", noise * pd_.sigma[:, None, None], ts)
    kpi_t = base_t + media_t + noise_t
    base_share = base_t.sum(axis=1) / kpi_total if kpi_total else np.full(draws, np.nan)

    roi_units = (
        f"{scales.kpi_units} per {scales.spend_units}"
        if scales.kpi_units and scales.spend_units
        else None
    )
    labels = outcome.channels
    preview = PriorPreview(
        roi={labels[c]: _summary(roi[:, c], quantiles, roi_units) for c in range(n_ch)},
        contribution_share={labels[c]: _summary(share[:, c], quantiles, None) for c in range(n_ch)},
        baseline_share=_summary(base_share, quantiles, None) if kpi_total else None,
        kpi_fan=_fan(kpi_t, scales, quantiles),
    )
    coverage: dict[str, float] = {}
    for label, (what, lo, hi, _cred) in outcome.ranges.items():
        c = labels.index(label)
        coverage[label] = _covered(roi[:, c] if what == "roi" else share[:, c], lo, hi)
    if outcome.baseline_range is not None and kpi_total:
        coverage["baseline"] = _covered(base_share, *outcome.baseline_range[:2])
    issues = list(outcome.issues)
    issues += _lift_checks(lift_tests, scales, roi, labels)
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
    sample = np.arange(draws)
    data: dict[str, xr.DataArray] = {
        "roi": xr.DataArray(
            roi, dims=["sample", CHANNEL], coords={"sample": sample, CHANNEL: scales.channels}
        ),
        "contribution_share": xr.DataArray(
            share, dims=["sample", CHANNEL], coords={"sample": sample, CHANNEL: scales.channels}
        ),
        "baseline_share": xr.DataArray(base_share, dims=["sample"], coords={"sample": sample}),
        "kpi": xr.DataArray(
            kpi_t, dims=["sample", TIME], coords={"sample": sample, TIME: scales.times}
        ),
    }
    if scales.kpi is not None:
        data["kpi_observed"] = xr.DataArray(
            scales.kpi.sum(axis=0), dims=[TIME], coords={TIME: scales.times}
        )
    stated_q = np.full(n_ch, "", dtype=object)
    stated_lo = np.full(n_ch, np.nan)
    stated_hi = np.full(n_ch, np.nan)
    stated_c = np.full(n_ch, np.nan)
    for name, (what, lo, hi, cr) in outcome.ranges.items():
        c = labels.index(name)
        stated_q[c], stated_lo[c], stated_hi[c], stated_c[c] = what, lo, hi, cr
    coords_ch = {CHANNEL: scales.channels}
    data["stated_quantity"] = xr.DataArray(stated_q.astype(str), dims=[CHANNEL], coords=coords_ch)
    data["stated_low"] = xr.DataArray(stated_lo, dims=[CHANNEL], coords=coords_ch)
    data["stated_high"] = xr.DataArray(stated_hi, dims=[CHANNEL], coords=coords_ch)
    data["stated_credibility"] = xr.DataArray(stated_c, dims=[CHANNEL], coords=coords_ch)
    preview_ds = xr.Dataset(data)
    if roi_units:
        preview_ds["roi"].attrs["units"] = roi_units
    if scales.kpi_units:
        preview_ds["kpi"].attrs["units"] = scales.kpi_units
    if outcome.baseline_range is not None:
        lo_b, hi_b, cb = outcome.baseline_range
        preview_ds["baseline_share"].attrs.update(
            {"range_low": lo_b, "range_high": hi_b, "credibility": cb}
        )
    return result, preview_ds


def _fan(kpi_t: np.ndarray, scales: ModelScales, quantiles: Sequence[float]) -> FanChart:
    qs = sorted({min(max(float(q), 1e-6), 1 - 1e-6) for q in quantiles})[:16]
    values = np.quantile(kpi_t, qs, axis=0)
    observed: list[float | None] | None = None
    if scales.kpi is not None:
        tot = scales.kpi.sum(axis=0)
        observed = [float(v) if math.isfinite(v) else None for v in tot]
    return FanChart(
        dim=TIME,
        coords=_labels(scales.times),
        quantiles=qs,
        values=[[float(v) if math.isfinite(v) else None for v in row] for row in values],
        observed=observed,
        units=scales.kpi_units,
    )


def _lift_checks(
    tests: Sequence[LiftTest], scales: ModelScales, roi: np.ndarray, labels: list[str]
) -> list[Issue]:
    """Lift tests whose implied ROI falls outside the 90% prior ROI interval (info), and
    tests on channels the data does not have (error)."""
    out: list[Issue] = []
    for i, test in enumerate(tests):
        c = scales.channel_index(test.channel)
        here = _pointer("lift_tests", i)
        if c is None:
            out.append(
                _issue(
                    f"lift test on {test.channel!r}: the data has no such channel",
                    pointer=here + "/channel",
                    severity=Severity.ERROR,
                    code=ErrorCode.SCHEMA_INVALID,
                )
            )
            continue
        if test.geo is not None and scales.geo_index(test.geo) is None:
            out.append(
                _issue(
                    f"lift test on {test.channel!r}: the data has no geo {test.geo!r}",
                    pointer=here + "/geo",
                    severity=Severity.ERROR,
                    code=ErrorCode.SCHEMA_INVALID,
                )
            )
            continue
        v = roi[:, c]
        v = v[np.isfinite(v)]
        if not v.size:
            continue
        lo, hi = np.quantile(v, [0.05, 0.95])
        implied = test.implied_roi
        if not lo <= implied <= hi:
            out.append(
                _issue(
                    f"the lift test on {labels[c]!r} implies an ROI of {implied:.3g}, outside "
                    f"the 90% prior range [{lo:.3g}, {hi:.3g}]; the fit will weigh both",
                    pointer=here,
                    severity=Severity.INFO,
                )
            )
    return out


# ============================================================================= lift tests


@dataclass(frozen=True, slots=True)
class LiftDesign:
    """Constant arrays of every lift test's cells (see the module doc).

    One row per cell ``k`` (a test, a geo and a period in its window): ``x(k, lag)`` the
    lag tensor row, ``dx(k, lag)`` the extra spend carried into it, ``target_scale(k,)``
    and the cell's geo, channel and test index. ``observed``/``sigma`` per test.
    """

    ids: list[str]
    test: np.ndarray
    geo: np.ndarray
    channel: np.ndarray
    x: np.ndarray
    dx: np.ndarray
    target_scale: np.ndarray
    observed: np.ndarray
    sigma: np.ndarray
    skipped: list[str] = field(default_factory=list)

    @property
    def n_tests(self) -> int:
        return len(self.ids)

    def segments(self) -> np.ndarray:
        """``(test, cell)`` 0/1 matrix summing cells into tests."""
        m = np.zeros((self.n_tests, self.test.size))
        m[self.test, np.arange(self.test.size)] = 1.0
        return m

    def lift(
        self,
        weights: np.ndarray,
        sat: Mapping[str, np.ndarray],
        beta: np.ndarray,
        kind: Any,
    ) -> np.ndarray:
        """NumPy lift per test for one parameter set (``weights(channel, lag)``,
        ``sat[p](channel,)``, ``beta(geo, channel)``) - the graph's twin, for tests."""
        w = weights[self.channel]
        a0 = (self.x * w).sum(axis=1)
        a1 = ((self.x + self.dx) * w).sum(axis=1)
        params = {k: v[self.channel] for k, v in sat.items()}
        cell = (
            self.target_scale
            * beta[self.geo, self.channel]
            * (saturate(a1, kind, **params) - saturate(a0, kind, **params))
        )
        return np.asarray(self.segments() @ cell)


def _day(t: date) -> np.datetime64:
    return np.datetime64(t.isoformat(), "D")


def lift_design(
    tests: Sequence[LiftTest],
    scales: ModelScales,
    *,
    pointer: str = "/calibration/lift_tests",
    log: Callable[[str], None] | None = None,
) -> LiftDesign | None:
    """The lift-test cells over the model-stage data (``None`` without any usable test).

    Tests whose window starts after the data ends are skipped (a holdout fit sees only the
    training periods); other problems raise ``SCHEMA_INVALID`` at the test's pointer.
    """
    if not tests:
        return None
    times = scales.times
    if not np.issubdtype(times.dtype, np.datetime64):
        raise MapiError(
            "lift tests need a date-valued time axis",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer=pointer,
        )
    days = times.astype("datetime64[D]")
    n_geo, _, n_time, n_lag = scales.x.shape
    ids: list[str] = []
    rows: dict[str, list[Any]] = {k: [] for k in ("test", "geo", "channel", "x", "dx", "ts")}
    observed: list[float] = []
    sigma: list[float] = []
    skipped: list[str] = []
    for i, test in enumerate(tests):
        here = f"{pointer}/{i}"
        label = test.id or f"{test.channel}-{test.start.isoformat()}"
        c = scales.channel_index(test.channel)
        if c is None:
            raise MapiError(
                f"lift test {label!r}: the data has no channel {test.channel!r}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=here + "/channel",
            )
        if test.geo is not None:
            g = scales.geo_index(test.geo)
            if g is None:
                raise MapiError(
                    f"lift test {label!r}: the data has no geo {test.geo!r}",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/geo",
                )
            geos = np.asarray([g])
        else:
            geos = np.arange(n_geo)
        start, end = _day(test.start), _day(test.end)
        window = np.flatnonzero((days >= start) & (days <= end))
        if not window.size:
            if start > days.max():
                skipped.append(label)
                if log is not None:
                    log(f"lift test {label!r} lies after the fitted periods; not used here")
                continue
            raise MapiError(
                f"lift test {label!r}: {test.start} to {test.end} covers no whole period of "
                "the model's time axis (each period is labelled by one date)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=here,
            )
        base = scales.spend[np.ix_(geos, np.asarray([c]), window)][:, 0, :]  # (geos, window)
        total = float(base.sum())
        if test.spend_delta < 0 and -test.spend_delta > total * (1 + 1e-9):
            raise MapiError(
                f"lift test {label!r} removes {-test.spend_delta:.6g} of spend but the data "
                f"shows only {total:.6g} on {test.channel!r} in that window",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=here + "/spend_delta",
            )
        alloc = (
            test.spend_delta * base / total
            if total > 0
            else np.full(base.shape, test.spend_delta / base.size)
        )
        scale = scales.spend_scale[np.ix_(geos, np.asarray([c]), window)][:, 0, :]
        dx_w = alloc / scale  # model units, (geos, window)
        dx = np.zeros((geos.size, n_time))
        dx[:, window] = dx_w
        k_test = len(ids)
        for gi, g in enumerate(geos):
            for t in window:
                lagged = np.zeros(n_lag)
                for lag in range(n_lag):
                    if t - lag >= 0:
                        lagged[lag] = dx[gi, t - lag]
                rows["test"].append(k_test)
                rows["geo"].append(int(g))
                rows["channel"].append(c)
                rows["x"].append(scales.x[g, c, t])
                rows["dx"].append(lagged)
                rows["ts"].append(scales.target_scale[g, t])
        ids.append(label)
        observed.append(test.kpi_delta)
        sigma.append(test.sigma)
    if not ids:
        return None
    if len(set(ids)) != len(ids):
        raise MapiError(
            "lift tests on the same channel and start date need distinct ids",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=pointer,
        )
    return LiftDesign(
        ids=ids,
        test=np.asarray(rows["test"], dtype=np.int64),
        geo=np.asarray(rows["geo"], dtype=np.int64),
        channel=np.asarray(rows["channel"], dtype=np.int64),
        x=np.asarray(rows["x"], dtype=np.float64).reshape(-1, n_lag),
        dx=np.asarray(rows["dx"], dtype=np.float64).reshape(-1, n_lag),
        target_scale=np.asarray(rows["ts"], dtype=np.float64),
        observed=np.asarray(observed, dtype=np.float64),
        sigma=np.asarray(sigma, dtype=np.float64),
        skipped=skipped,
    )
