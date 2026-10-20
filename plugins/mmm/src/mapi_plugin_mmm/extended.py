"""Shared pieces of the plugins on mmm-framework's extension models (``NestedMMM``,
``MultivariateMMM``; :mod:`mapi_plugin_mmm.nested`, :mod:`mapi_plugin_mmm.multivariate`).

The extension models are a different, smaller engine than ``BayesianMMM``
(``mmm_framework.mmm_extensions.models.base.BaseExtendedMMM``): one national series, no
control variables, and a fixed media transform per channel::

    x_c = spend_c / (max_t spend_c + 1e-8)
    sat_c = 1 - exp(-lambda_c * adstock(x_c; alpha_c))    # normalised geometric, 8 lags

on a standardised KPI ``(y - mean) / (std + 1e-8)``, with an optional trend
(``components.temporal.build_trend_contribution``: a linear trend over ``t = 0..1``; the
other kinds are zero-centred over the fit window) and Fourier seasonality
(``build_seasonality_contribution`` with ``period_source=FREQUENCY_TABLE``, so the periods
are the core model's: 52 weeks a year on weekly data).

This module maps the platform's settings onto those configs, rebuilds the same media
transform, Fourier design and trend in NumPy for the plugins' replays, and builds the
bundle groups and runs the sampler the same way for every extension plugin.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np
import xarray as xr
from mapi_sdk.models import ModelSettings
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import Field

from mapi_plugin_mmm.base import _clean
from mapi_plugin_mmm.forward import TREND_BY_PERIOD, Posterior, adstock_kernels, compact_posterior
from mapi_plugin_mmm.framework import Controls, PanelData, quiet
from mapi_plugin_mmm.settings import (
    ADVANCED,
    BASIC,
    Frequency,
    GridSettings,
    SeasonalitySettings,
    TrendSettings,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mapi_sdk.models import FitContext
    from mapi_spec.spec import FitConfig
    from mmm_framework.config import ModelConfig
    from mmm_framework.model.trend_config import TrendConfig

    from mapi_plugin_mmm.framework import EnginePlan

__all__ = [
    "EXT_L_MAX",
    "ExtSettings",
    "SeasonTerm",
    "ext_configs",
    "ext_fit_groups",
    "ext_kernels",
    "ext_prior_group",
    "fit_extended",
    "fourier_at",
    "media_scale",
    "national_panel",
    "posterior_of",
    "saturated_media",
    "season_terms",
    "trend_values",
]

EXT_L_MAX: Final = 8
"""Adstock kernel length of the extension models (mmm-framework ``_ADSTOCK_L_MAX``)."""
_ALPHA_CLIP: Final = 1e-6
"""The engine clips the carry-over rate off 0 and 1 (``_media_transform_apply``)."""
_PRIOR_KEEP: Final = 200


class ExtSettings(ModelSettings):
    """Settings shared by the extension-model plugins (the base of ``NestedSettings`` and
    ``MultivariateSettings``). The media transform is fixed by the engine (normalised
    geometric adstock over 8 periods and ``1 - exp(-lambda x)`` saturation)."""

    trend: TrendSettings = Field(TrendSettings(), json_schema_extra=BASIC)
    seasonality: SeasonalitySettings = Field(SeasonalitySettings(), json_schema_extra=BASIC)
    frequency: Frequency | None = Field(
        None,
        description="Data frequency: W (weekly), D (daily) or M (monthly); default: read "
        "from the time labels",
        json_schema_extra=ADVANCED,
    )
    grid: GridSettings = Field(GridSettings(), json_schema_extra=ADVANCED)
    likelihood: Literal["normal", "student_t"] = Field(
        "normal",
        description="Observation noise: normal, or student_t for heavy-tailed outliers",
        json_schema_extra=ADVANCED,
    )


# ======================================================================== seasonality


@dataclass(frozen=True, slots=True)
class SeasonTerm:
    """One Fourier block: ``order`` sine/cosine pairs of a ``period`` (in periods)."""

    name: str
    period: float
    order: int


def season_terms(frequency: str, s: SeasonalitySettings) -> list[SeasonTerm]:
    """The Fourier blocks the extension graph builds for ``s`` at ``frequency``
    (``period_source=FREQUENCY_TABLE``): a component the frequency table has no period for
    is skipped, so is a period below two observations; orders are clamped to
    ``max(1, int(period / 2))``."""
    from mmm_framework.transforms.seasonality import periods_for_frequency

    periods = periods_for_frequency(frequency)
    orders = {
        "yearly": s.yearly_order or 0,
        "monthly": s.monthly_order or 0,
        "weekly": s.weekly_order or 0,
    }
    out: list[SeasonTerm] = []
    for name in ("yearly", "monthly", "weekly"):
        order = int(orders[name])
        period = periods.get(name)
        if order <= 0 or period is None or period < 2.0:
            continue
        out.append(SeasonTerm(name, float(period), min(order, max(1, int(period / 2)))))
    return out


def fourier_at(positions: np.ndarray, terms: Sequence[SeasonTerm]) -> np.ndarray:
    """``(n, n_feat)`` Fourier design at period ``positions`` (blocks in ``terms`` order,
    the column order of the engine's ``seasonality_coefs``)."""
    from mmm_framework.transforms.seasonality import create_fourier_features

    pos = np.asarray(positions, dtype=float)
    blocks = [create_fourier_features(pos, t.period, t.order) for t in terms]
    blocks = [b for b in blocks if b.shape[1] > 0]
    if not blocks:
        return np.zeros((pos.size, 0))
    return np.concatenate(blocks, axis=1).astype(float)


# ======================================================================== configs


def ext_configs(s: ExtSettings, *, frequency: str) -> tuple[ModelConfig, TrendConfig]:
    """The ``ModelConfig`` (seasonality, likelihood) and ``TrendConfig`` an extension model
    takes for ``s``. ``frequency`` is the data's (the engine reads the period from the
    index spacing; :func:`season_terms` gives the same blocks for the replay)."""
    from mmm_framework.config import ModelConfig, SeasonalityConfig
    from mmm_framework.config.enums import LikelihoodFamily
    from mmm_framework.config.likelihood import LikelihoodConfig
    from mmm_framework.model.trend_config import TrendConfig, TrendType
    from mmm_framework.transforms.seasonality import SeasonalityPeriodSource

    del frequency  # the periods come from the index; kept for a uniform call site
    seas = s.seasonality
    kwargs: dict[str, Any] = {
        "seasonality": SeasonalityConfig(
            yearly=seas.yearly_order or None,
            monthly=seas.monthly_order or None,
            weekly=seas.weekly_order or None,
            prior_sigma=float(seas.prior_sigma),
            period_source=SeasonalityPeriodSource.FREQUENCY_TABLE,
        )
    }
    if s.likelihood == "student_t":
        kwargs["likelihood"] = LikelihoodConfig(family=LikelihoodFamily.STUDENT_T)
    t = s.trend
    trend = TrendConfig(
        type={
            "none": TrendType.NONE,
            "linear": TrendType.LINEAR,
            "piecewise": TrendType.PIECEWISE,
            "spline": TrendType.SPLINE,
            "gp": TrendType.GP,
        }[t.kind],
        n_changepoints=int(t.n_changepoints),
        changepoint_range=float(t.changepoint_range),
        n_knots=int(t.n_knots),
        growth_prior_sigma=float(t.growth_prior_sigma),
    )
    return ModelConfig(**kwargs), trend


def national_panel(
    times: np.ndarray, channels: list[str], spend: np.ndarray, y: np.ndarray | None
) -> PanelData:
    """A one-cell :class:`PanelData` from ``(T, C)`` spend and ``(T,)`` KPI (the layout
    :class:`~mapi_plugin_mmm.derived.FitWindow` and planning read)."""
    sp = np.asarray(spend, dtype=float)
    t = sp.shape[0]
    return PanelData(
        times=np.asarray(times),
        panel=(),
        geos=None,
        products=None,
        channels=list(channels),
        controls=Controls((), (), ()),
        spend=sp.reshape(t, 1, 1, sp.shape[1]),
        ctrl=np.zeros((t, 1, 1, 0)),
        y=None if y is None else np.asarray(y, dtype=float).reshape(t, 1, 1),
    )


# ======================================================================== media transform


def media_scale(spend: np.ndarray) -> np.ndarray:
    """``(C,)`` per-channel media scale of ``(T, C)`` fit-window spend (the engine's
    ``_media_scale``)."""
    return np.asarray(np.asarray(spend, dtype=float).max(axis=0) + 1e-8, dtype=float)


def ext_kernels(alpha: np.ndarray) -> np.ndarray:
    """``(S, EXT_L_MAX)`` normalised geometric lag weights per draw of a carry-over rate."""
    a = np.clip(np.asarray(alpha, dtype=float).reshape(-1), _ALPHA_CLIP, 1.0 - _ALPHA_CLIP)
    return adstock_kernels("geometric", EXT_L_MAX, normalize=True, alpha=a, n=a.size)


def saturated_media(
    spend: np.ndarray,
    positions: np.ndarray,
    kernels: Sequence[np.ndarray],
    lam: Sequence[np.ndarray],
    scale: np.ndarray,
) -> np.ndarray:
    """``(S, C, n)`` saturated media ``1 - exp(-lam * adstock(spend / scale))`` at
    ``positions``; ``spend`` is the raw ``(T_full, C)`` history up to the last position
    (zero before the first period, as the engine pads)."""
    sp = np.asarray(spend, dtype=float)
    pos = np.asarray(positions, dtype=int)
    n_draws = int(np.asarray(kernels[0]).shape[0]) if len(kernels) else 0
    out = np.empty((n_draws, sp.shape[1], pos.size))
    for c in range(sp.shape[1]):
        kern = np.asarray(kernels[c])  # (S, L)
        lmax = kern.shape[1]
        x = sp[:, c] / float(scale[c])
        lag_idx = pos[:, None] - np.arange(lmax)[None, :]  # (n, L)
        windows = np.where(lag_idx >= 0, x[np.clip(lag_idx, 0, None)], 0.0)
        ad = windows @ kern.T  # (n, S)
        out[:, c] = 1.0 - np.exp(-np.asarray(lam[c], dtype=float)[:, None] * ad.T)
    return out


# ======================================================================== trend


def trend_values(
    post: Posterior,
    *,
    kind: str,
    slope_name: str,
    by_period: np.ndarray | None,
    n_periods: int,
    positions: np.ndarray,
) -> np.ndarray:
    """``(S, n)`` trend (standardised units) at period ``positions``: a linear trend is
    ``slope * pos / (T - 1)`` (extrapolated past the fit window); the other kinds read
    ``by_period (S, T)`` and hold the last fitted value beyond the window."""
    pos = np.asarray(positions, dtype=int)
    if kind == "none":
        return np.zeros((post.n, pos.size))
    if kind == "linear":
        slope = post.get(slope_name)
        if slope is None:
            return np.zeros((post.n, pos.size))
        return np.asarray(slope.reshape(post.n, 1) * (pos[None, :] / max(n_periods - 1, 1)))
    if by_period is None:
        raise MapiError("the posterior has no per-period trend", code=ErrorCode.INTEGRITY_FAILURE)
    tb = np.asarray(by_period, dtype=float).reshape(post.n, -1)
    return np.asarray(tb[:, np.clip(pos, 0, tb.shape[1] - 1)])


# ======================================================================== bundle groups


def _group(trace: Any, name: str) -> xr.Dataset | None:
    """Group ``name`` of a DataTree or an arviz ``InferenceData`` (``None`` when absent)."""
    if isinstance(trace, xr.DataTree):
        node = trace.children.get(name)
        return None if node is None else node.to_dataset(inherit=False)
    if hasattr(trace, "groups") and name in trace.groups():
        out = getattr(trace, name)
        return out if isinstance(out, xr.Dataset) else None
    return None


def _free_names(model: Any) -> list[str]:
    return [rv.name for rv in model.model.free_RVs]


def ext_prior_group(model: Any, seed: int, keep: int = _PRIOR_KEEP) -> xr.Dataset | None:
    """A ``prior`` group of ``keep`` draws of the free parameters (the engine's NUTS path
    attaches none); ``None`` when the prior cannot be sampled."""
    try:
        with quiet():
            tree = model.sample_prior_predictive(samples=keep, random_seed=int(seed))
    except Exception:
        return None
    prior = _group(tree, "prior")
    if prior is None:
        return None
    free = [n for n in _free_names(model) if n in prior]
    if not free:
        return None
    return _clean(prior[free])


def ext_fit_groups(
    model: Any,
    post: xr.Dataset,
    *,
    fit_data: xr.Dataset,
    observed: xr.Dataset | None,
    extra: Mapping[str, xr.Dataset | xr.DataArray],
    cfg: FitConfig,
    ll_var: str,
) -> dict[str, xr.Dataset]:
    """The bundle groups of an extension fit: ``posterior`` (the compact posterior - the
    engine's per-observation deterministics dropped - plus the plugin's ``extra``
    variables; the engine's ``trend_component`` is in KPI units, so ``trend_by_period``
    is only kept when ``extra`` supplies it), ``fit_data``, ``observed_data``,
    ``sample_stats`` (numeric), a thinned ``prior`` and ``log_likelihood`` of ``ll_var``
    when the spec keeps it."""
    compact = compact_posterior(post, n_obs=int(model.n_obs), n_cells=1)
    compact = compact.drop_vars([TREND_BY_PERIOD], errors="ignore")
    add: dict[str, xr.DataArray] = {}
    for name, value in extra.items():
        if isinstance(value, xr.Dataset):
            add.update({str(k): v for k, v in value.data_vars.items()})
        else:
            add[str(name)] = value
    if add:
        compact = compact.assign(add)
    groups: dict[str, xr.Dataset] = {"posterior": _clean(compact), "fit_data": _clean(fit_data)}
    if observed is not None:
        groups["observed_data"] = _clean(observed)
    stats = _group(model._trace, "sample_stats")
    if stats is not None:
        groups["sample_stats"] = _clean(
            stats.drop_vars(
                [v for v in stats.data_vars if stats[v].dtype.kind not in "biuf"],
                errors="ignore",
            )
        )
    prior = ext_prior_group(model, cfg.seed if cfg.seed is not None else 0)
    if prior is not None:
        groups["prior"] = prior
    if cfg.keep.log_likelihood:
        import pymc as pm

        try:
            with quiet():
                ll = pm.compute_log_likelihood(
                    xr.DataTree.from_dict({"posterior": post}),
                    model=model.model,
                    var_names=[ll_var],
                    extend_inferencedata=False,
                    progressbar=False,
                )
            ll_ds = ll if isinstance(ll, xr.Dataset) else ll["log_likelihood"].to_dataset()
            groups["log_likelihood"] = _clean(ll_ds)
        except Exception:
            pass
    return groups


def posterior_of(model: Any) -> xr.Dataset:
    """The fitted engine model's full posterior group (deterministics included)."""
    post = _group(model._trace, "posterior")
    if post is None:
        raise MapiError("mmm-framework returned no posterior", code=ErrorCode.INTEGRITY_FAILURE)
    return post


# ======================================================================== sampling


def fit_extended(model: Any, plan: EnginePlan, ctx: FitContext) -> Any:
    """Fit an extension model with :func:`mapi_plugin_mmm.sampling.run_sampler` (live
    progress, chains and step metrics, cancellation, errors as ``USER_CODE_ERROR``) and
    return mmm-framework's ``ModelResults`` (its ``diagnostics`` hold the fit notes)."""
    from mapi_plugin_mmm.sampling import run_sampler

    result = run_sampler(model.fit, plan, ctx)
    if getattr(model, "_trace", None) is None:  # pragma: no cover - the engine sets it
        raise MapiError("mmm-framework returned no posterior", code=ErrorCode.INTEGRITY_FAILURE)
    return result
