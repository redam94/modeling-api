"""The fitted mmm-framework model, replayed from posterior draws in NumPy.

mmm-framework's ``BayesianMMM`` is additive on the standardised KPI::

    mu[cell, t] = intercept + trend[t] + seasonality[t] + level_offset[cell]
                  + sum_c beta_c[geo] * sat_c(adstock_c(x_c[cell, t] / max_c))
                  + sum_k gamma_k * (z_k[cell, t] - mean_k) / sd_k
    y = mu * y_std + y_mean

:class:`Replay` evaluates every term per posterior sample at arbitrary period positions
(inside the fit window or after it), for the fitted panel cells and for new ones, with the
full spend history so carry-over into a scored window is exact. It is the same forward
pass as mmm-framework's own ``validation.backtest.PosteriorForecaster`` - built from the
same primitives (``transforms.seasonality.create_fourier_features``,
``frequentist._transforms.saturate``, the ``adstock_weights`` kernels, the fitted
scaling) - vectorised over samples and cells and split into components; tests check it
against both the forecaster and the graph's own deterministics.

Beyond the fit window a linear trend is extrapolated; piecewise, spline and GP trends are
held at their last fitted level (mmm-framework's documented forecasting policy), so their
intervals do not widen with the horizon.

A time-varying channel (``time_varying``) has one coefficient per fitted period,
``beta_tv_<ch>(period)``; after the fit window it follows the settings' forecast policy
(:meth:`Replay.beta_at`). Count and proportion likelihoods put the sum above on a link
scale (``log`` or ``logit``, ``y_mean = 0``, ``y_std = 1``): :class:`Components` keeps the
link-scale terms, ``mu`` is the inverse link of their sum, and :meth:`Components.kpi_parts`
splits ``mu`` into a baseline and channel parts that add up in KPI units.

:class:`ReplayLike` is the protocol every MMM replay of this package implements (the
extension models' replays too), so the derived quantities, budget optimisation and run
tracking work on any of them.

Bundle layout of the posterior (:func:`compact_posterior`): mmm-framework's free
parameters and per-channel scalars, plus two compact replay variables computed from its
per-observation deterministics - ``trend_by_period(chain, draw, period)`` and
``level_offset(chain, draw, cell)`` (geo + product baseline offsets per panel cell) - so
the bundle never stores an observation-sized array per draw.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError

from mapi_plugin_mmm.framework import PanelData

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mmm_framework.model import BayesianMMM

    from mapi_plugin_mmm.settings import TvpForecast

__all__ = [
    "LEVEL_OFFSET",
    "LIKELIHOOD_LINK",
    "TREND_BY_PERIOD",
    "TVP_PERIOD",
    "CellRef",
    "Components",
    "Link",
    "Posterior",
    "Replay",
    "ReplayLike",
    "adstock_kernels",
    "compact_posterior",
    "inverse_link",
]

TREND_BY_PERIOD: Final = "trend_by_period"
LEVEL_OFFSET: Final = "level_offset"
TVP_PERIOD: Final = "tvp_period"
"""Period dim of the time-varying coefficients ``beta_tv_<ch>`` and ``beta_<ch>_tvpz``."""
_EXP_CAP: Final = 50.0

Link = Literal["identity", "log", "logit"]
LIKELIHOOD_LINK: Final[dict[str, Link]] = {
    "normal": "identity",
    "student_t": "identity",
    "poisson": "log",
    "negative_binomial": "log",
    "beta": "logit",
}
"""The link of each likelihood family (the canonical one mmm-framework declares)."""
_DISPERSION: Final = {"negative_binomial": "nb_alpha", "beta": "beta_kappa"}


def inverse_link(eta: np.ndarray, link: Link) -> np.ndarray:
    """The expected KPI of link-scale ``eta`` (``exp`` capped at ``e^50``)."""
    if link == "identity":
        return eta
    if link == "log":
        return np.exp(np.minimum(eta, _EXP_CAP))
    from scipy.special import expit

    return np.asarray(expit(eta))


def _rng(seed: int, name: str) -> np.random.Generator:
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


_OBS_DETERMINISTICS: Final = frozenset(
    {
        "intercept_component",
        "trend_component",
        "seasonality_component",
        "seasonality_by_period",
        "geo_component",
        "product_component",
        "channel_contributions",
        "media_total",
        "control_contributions",
        "controls_total",
        "y_obs_scaled",
        "event_component",
        "interaction_component",
        "lever_component",
    }
)
_DRAW_DIMS: Final = ("chain", "draw")


# ======================================================================== posterior


@dataclass(slots=True)
class Posterior:
    """Posterior draws flattened to one leading sample axis ``S``."""

    values: dict[str, np.ndarray]
    n: int

    @classmethod
    def from_dataset(cls, post: xr.Dataset) -> Posterior:
        """From ``(chain, draw, ...)`` draws or a ``sample`` dim (thinned draws)."""
        out: dict[str, np.ndarray] = {}
        n = 0
        for name in map(str, post.data_vars):
            da = post[name]
            if "sample" in da.dims:
                arr = np.asarray(da.transpose("sample", ...).values, dtype=np.float64)
            elif set(_DRAW_DIMS) <= set(map(str, da.dims)):
                arr = np.asarray(da.transpose(*_DRAW_DIMS, ...).values, dtype=np.float64)
                arr = arr.reshape(arr.shape[0] * arr.shape[1], *arr.shape[2:])
            else:
                continue
            out[name] = arr
            n = int(arr.shape[0])
        if not out:
            raise MapiError(
                "the fitted model has no posterior draws", code=ErrorCode.INTEGRITY_FAILURE
            )
        return cls(out, n)

    def get(self, name: str) -> np.ndarray | None:
        return self.values.get(name)

    def need(self, name: str) -> np.ndarray:
        arr = self.values.get(name)
        if arr is None:
            raise MapiError(
                f"the posterior has no {name!r} (was the model fitted with other settings?)",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        return arr


def compact_posterior(post: xr.Dataset | xr.DataTree, *, n_obs: int, n_cells: int) -> xr.Dataset:
    """The bundle's posterior: drop mmm-framework's per-observation deterministics and add
    ``trend_by_period`` and ``level_offset`` (see the module doc)."""
    if isinstance(post, xr.DataTree):
        post = post.to_dataset(inherit=False)
    # time-varying coefficients: one value per period, never an observation-sized array
    tvp: dict[str, xr.DataArray] = {}
    for name in map(str, post.data_vars):
        if not (name.startswith("beta_") and name.endswith("_tvpz")):
            continue
        ch = name[len("beta_") : -len("_tvpz")]
        for var in (name, f"beta_tv_{ch}"):
            if var not in post:
                continue
            da = post[var]
            period = [str(d) for d in da.dims if d not in _DRAW_DIMS]
            if len(period) == 1:
                tvp[var] = da.rename({period[0]: TVP_PERIOD}).drop_vars(TVP_PERIOD, errors="ignore")
    keep: list[str] = []
    for name in map(str, post.data_vars):
        da = post[name]
        if name in tvp or name in _OBS_DETERMINISTICS or "obs" in da.dims:
            continue
        auto = [str(d) for d in da.dims if str(d).startswith(f"{name}_dim_")]
        if any(int(da.sizes[d]) == n_obs for d in auto) and n_obs > 1:
            continue
        if da.dtype.kind not in "biuf":
            continue
        keep.append(name)
    out = post[keep]
    extra: dict[str, xr.DataArray] = {}
    if "trend_component" in post:
        tc = post["trend_component"]
        other = [d for d in tc.dims if d not in _DRAW_DIMS]
        arr = np.asarray(tc.transpose(*_DRAW_DIMS, *other).values, dtype=np.float64)
        arr = arr.reshape(arr.shape[0], arr.shape[1], -1)[:, :, :: max(n_cells, 1)]
        extra[TREND_BY_PERIOD] = xr.DataArray(
            arr,
            dims=[*_DRAW_DIMS, "trend_period"],
            coords={d: post[d].values for d in _DRAW_DIMS if d in post.coords},
        )
    level = None
    for comp in ("geo_component", "product_component"):
        if comp not in post:
            continue
        da = post[comp]
        other = [d for d in da.dims if d not in _DRAW_DIMS]
        arr = np.asarray(da.transpose(*_DRAW_DIMS, *other).values, dtype=np.float64)
        arr = arr.reshape(arr.shape[0], arr.shape[1], -1)[:, :, :n_cells]
        level = arr if level is None else level + arr
    if level is not None:
        extra[LEVEL_OFFSET] = xr.DataArray(
            level,
            dims=[*_DRAW_DIMS, "cell"],
            coords={d: post[d].values for d in _DRAW_DIMS if d in post.coords},
        )
    extra.update(tvp)
    if extra:
        out = out.assign(extra)
    return out


# ======================================================================== kernels


def adstock_kernels(
    kind: str,
    l_max: int,
    *,
    normalize: bool,
    alpha: np.ndarray | None = None,
    theta: np.ndarray | None = None,
    shape: np.ndarray | None = None,
    scale: np.ndarray | None = None,
    n: int = 1,
) -> np.ndarray:
    """``(S, l_max)`` lag-indexed weights per sample - mmm-framework's
    ``transforms.adstock.adstock_weights`` vectorised over samples (``w[:, 0]`` weights the
    current period). Missing parameters take that function's defaults."""
    lags = np.arange(l_max, dtype=float)[None, :]
    if kind == "none":
        w = np.zeros((n, l_max))
        w[:, 0] = 1.0
        return w

    def col(v: np.ndarray | None, default: float) -> np.ndarray:
        if v is None:
            return np.full((n, 1), default)
        return np.asarray(v, dtype=float).reshape(-1, 1)

    if kind == "geometric":
        w = np.power(col(alpha, 0.5), lags)
    elif kind == "delayed":
        w = np.power(col(alpha, 0.5), (lags - col(theta, 0.0)) ** 2)
    elif kind == "weibull":
        k, lam = col(shape, 2.0), col(scale, 2.0)
        t = lags + 1.0
        w = (k / lam) * (t / lam) ** (k - 1.0) * np.exp(-((t / lam) ** k))
    else:  # pragma: no cover - settings restrict the kinds
        raise ValueError(f"unknown adstock kind {kind!r}")
    w = np.broadcast_to(w, (max(n, w.shape[0]), l_max)).astype(float)
    if normalize:
        total = w.sum(axis=1, keepdims=True)
        w = np.where(total > 0, w / np.where(total > 0, total, 1.0), w)
    return np.asarray(w)


# ======================================================================== replay


@dataclass(frozen=True, slots=True)
class CellRef:
    """One scored panel cell: fitted geo/product indices (``None`` = a new level) and the
    label used to seed a new level's hyperprior draws."""

    geo: int | None
    product: int | None
    label: str = ""

    @property
    def new(self) -> bool:
        return self.geo is None or self.product is None


@dataclass(slots=True)
class Components:
    """Every additive term per sample: ``channel (S, C, n_cell, n)``, ``baseline (S, n_cell,
    n)`` (``y_mean`` + intercept + level offsets), ``trend`` and ``seasonality`` ``(S, n)``,
    ``controls (S, K, n_cell, n)`` (one per control variable).

    With the identity link the terms are in KPI units and add up to the expected KPI. With
    a ``log``/``logit`` link (count and proportion likelihoods) they are on the link scale:
    :attr:`mu` is the inverse link of their sum and :meth:`kpi_parts` gives KPI-unit parts.
    """

    channel: np.ndarray
    baseline: np.ndarray
    trend: np.ndarray
    seasonality: np.ndarray
    controls: np.ndarray
    sigma: np.ndarray
    nu: float | None
    y_std: float
    notes: list[str] = field(default_factory=list)
    link: Link = "identity"
    family: str = "normal"
    dispersion: np.ndarray | None = None
    """``(S,)`` ``nb_alpha`` (negative binomial) or ``beta_kappa`` (beta), else ``None``."""

    @property
    def eta(self) -> np.ndarray:
        """The sum of every term ``(S, n_cell, n)`` (identity link: the expected KPI)."""
        total = self.baseline + self.trend[:, None, :] + self.seasonality[:, None, :]
        total = total + self.channel.sum(axis=1)
        if self.controls.shape[1]:
            total = total + self.controls.sum(axis=1)
        return total

    @property
    def mu(self) -> np.ndarray:
        """The expected KPI ``(S, n_cell, n)``."""
        return inverse_link(self.eta, self.link)

    def noise(self, seed: int, name: str = "y") -> np.ndarray:
        """Observation noise ``(S, n_cell, n)`` in KPI units of a Gaussian family, from a
        generator seeded by ``(seed, name)`` - a counterfactual's scenario and baseline
        share it (common random numbers)."""
        rng = _rng(seed, name)
        shape = self.baseline.shape
        eps = (
            rng.standard_t(self.nu, size=shape)
            if self.nu is not None
            else rng.standard_normal(shape)
        )
        return eps * self.sigma[:, None, None] * self.y_std

    def predictive(self, seed: int, name: str = "y") -> np.ndarray:
        """A draw of the KPI ``(S, n_cell, n)``: ``mu`` plus Gaussian noise, or for the
        count and proportion families the inverse CDF of uniforms seeded by ``(seed,
        name)`` (common random numbers: a scenario and its baseline share the uniforms,
        so their difference reflects the change in ``mu`` only)."""
        mu = self.mu
        if self.link == "identity":
            return mu + self.noise(seed, name)
        from scipy import stats

        u = np.clip(_rng(seed, name).random(mu.shape), 1e-12, 1 - 1e-12)
        if self.family == "poisson":
            return np.asarray(stats.poisson.ppf(u, mu), dtype=float)
        disp = self.dispersion
        if disp is None:
            raise MapiError(
                f"the posterior has no dispersion of the {self.family} likelihood",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        d = np.asarray(disp, dtype=float)[:, None, None]
        if self.family == "negative_binomial":
            return np.asarray(stats.nbinom.ppf(u, d, d / (d + mu)), dtype=float)
        if self.family == "beta":
            p = np.clip(mu, 1e-12, 1 - 1e-12)
            return np.asarray(stats.beta.ppf(u, p * d, (1 - p) * d), dtype=float)
        raise MapiError(  # pragma: no cover - settings restrict the families
            f"no predictive draws for the {self.family} likelihood",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )

    def kpi_parts(self) -> tuple[np.ndarray, dict[str, np.ndarray], np.ndarray]:
        """``(channel (S, C, n_cell, n), {"baseline", "trend", "seasonality"} (S, n_cell,
        n), controls (S, K, n_cell, n))`` in KPI units; they add up to :attr:`mu`.

        Identity link: the terms themselves. Other links: the baseline is the expected KPI
        without media, ``inverse_link(eta - sum_c channel_c)``; the media increment ``mu -
        baseline`` is split across channels in proportion to their link-scale terms; the
        trend, seasonality and controls are part of the baseline (zeros here)."""
        n, n_cell, n_t = self.baseline.shape
        if self.link == "identity":
            shape = (n, n_cell, n_t)
            return (
                self.channel,
                {
                    "baseline": self.baseline,
                    "trend": np.broadcast_to(self.trend[:, None, :], shape),
                    "seasonality": np.broadcast_to(self.seasonality[:, None, :], shape),
                },
                self.controls,
            )
        eta = self.eta
        media = self.channel.sum(axis=1)  # (S, n_cell, n)
        base = inverse_link(eta - media, self.link)
        lift = inverse_link(eta, self.link) - base
        share = np.divide(
            self.channel,
            media[:, None],
            out=np.zeros_like(self.channel),
            where=media[:, None] != 0,
        )
        zeros = np.zeros_like(base)
        return (
            share * lift[:, None],
            {"baseline": base, "trend": zeros, "seasonality": zeros},
            np.zeros_like(self.controls),
        )


class ReplayLike(Protocol):
    """What the derived quantities, budget optimisation and run tracking need from a
    fitted MMM's forward pass (:class:`Replay` and the extension models' replays)."""

    n: int
    channels: list[str]
    kernels: list[np.ndarray]
    """Per channel ``(S, L)`` lag weights (``w[:, 0]`` weights the spend period)."""

    def fit_cells(self) -> list[CellRef]: ...

    def evaluate(
        self,
        spend: np.ndarray,
        ctrl: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        *,
        strategy: Mapping[str, str] | None = None,
        seed: int = 0,
    ) -> Components: ...

    def channel_totals(
        self,
        spend: np.ndarray,
        ctrl: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        multiplier: float,
    ) -> np.ndarray:
        """``(S, C, n_cell)`` KPI total over ``positions`` of each channel when only its
        spend is scaled by ``multiplier``, against that channel at zero spend (every other
        term at the actual spend)."""
        ...

    def steady_state(
        self, spend: np.ndarray, cells: Sequence[CellRef], *, position: int | None = None
    ) -> np.ndarray:
        """``(S, n_cell, C)`` KPI per period of a constant raw spend ``(n_cell, C)``."""
        ...


class Replay:
    """The forward pass of a fitted ``BayesianMMM`` (see the module doc).

    ``model`` is the model rebuilt from the bundle's ``fit_data`` (its scaling, Fourier
    periods, configs); ``post`` the posterior (all draws or a thinned subset);
    ``tvp_forecast`` the policy of time-varying coefficients after the fit window
    (:meth:`beta_at`).
    """

    def __init__(
        self,
        model: BayesianMMM,
        fit: PanelData,
        post: Posterior,
        *,
        tvp_forecast: TvpForecast = "random_walk",
    ) -> None:
        from mmm_framework.config.enums import SaturationType
        from mmm_framework.frequentist._transforms import SATURATION_PARAMS
        from mmm_framework.model.base import _ADSTOCK_KIND
        from mmm_framework.transforms.seasonality import periods_for_frequency

        self.model = model
        self.fit = fit
        self.post = post
        self.n = post.n
        self.y_mean = float(model.y_mean)
        self.y_std = float(model.y_std)
        self.channels = list(model.channel_names)
        self.n_periods = int(model.n_periods)
        self.tvp_forecast: TvpForecast = tvp_forecast
        self.frequency = str(getattr(model.mff_config, "frequency", "W") or "W")
        self.n_geos = fit.n_geos
        self.n_products = fit.n_products
        self.media_max = np.array(
            [float(model._media_raw_max[c]) + 1e-8 for c in self.channels], dtype=float
        )
        # per-channel kernels, saturation and effects
        self.kernels: list[np.ndarray] = []
        self.sat_kind: list[SaturationType] = []
        self.sat_params: list[dict[str, np.ndarray]] = []
        self.beta: list[np.ndarray] = []
        self.tvp: dict[int, np.ndarray] = {}
        """Channel index -> ``beta_tv (S, n_periods)`` of a time-varying channel."""
        self.tvp_sigma: dict[int, np.ndarray] = {}
        for c, ch in enumerate(self.channels):
            cfg = model._get_adstock_config(ch)
            kind = _ADSTOCK_KIND.get(cfg.type, "geometric")
            self.kernels.append(
                adstock_kernels(
                    kind,
                    int(cfg.l_max) if kind != "none" else 1,
                    normalize=bool(cfg.normalize),
                    alpha=post.get(f"adstock_alpha_{ch}"),
                    theta=post.get(f"adstock_theta_{ch}"),
                    shape=post.get(f"adstock_shape_{ch}"),
                    scale=post.get(f"adstock_scale_{ch}"),
                    n=self.n,
                )
            )
            skind = model._get_saturation_config(ch).type
            self.sat_kind.append(skind)
            self.sat_params.append({p: post.need(f"{p}_{ch}") for p in SATURATION_PARAMS[skind]})
            beta = post.need(f"beta_{ch}")
            self.beta.append(beta.reshape(self.n, -1) if beta.ndim > 1 else beta)
            media_cfg = model.mff_config.get_media_config(ch)
            if bool(getattr(media_cfg, "time_varying", False)):
                tv = post.need(f"beta_tv_{ch}")
                if tv.ndim != 2 or tv.shape[1] != self.n_periods:
                    raise MapiError(
                        f"the time-varying coefficient of {ch!r} has {tv.shape[1:]} periods "
                        f"for {self.n_periods} fitted periods",
                        code=ErrorCode.INTEGRITY_FAILURE,
                    )
                self.tvp[c] = tv
                self.tvp_sigma[c] = post.need(f"beta_{ch}_tvpsigma").reshape(self.n)
        self.intercept = post.need("intercept")
        sigma = post.get("sigma")
        self.sigma = sigma if sigma is not None else np.zeros(self.n)
        lik = getattr(model.model_config, "likelihood", None)
        fam = str(getattr(getattr(lik, "family", None), "value", "normal"))
        self.family = fam
        self.link: Link = LIKELIHOOD_LINK.get(fam, "identity")
        disp = _DISPERSION.get(fam)
        self.dispersion: np.ndarray | None = (
            post.need(disp).reshape(self.n) if disp is not None else None
        )
        self.nu: float | None = (
            float((getattr(lik, "params", None) or {}).get("nu", 4.0))
            if fam == "student_t"
            else None
        )
        self._base_eta: dict[tuple[CellRef, ...], np.ndarray] = {}
        # trend
        self.trend_type = str(getattr(model.trend_config.type, "value", model.trend_config.type))
        self.trend_by_period = post.get(TREND_BY_PERIOD)
        self.trend_slope = post.get("trend_slope")
        # seasonality: (period, order, coefficients)
        freq = getattr(model.mff_config, "frequency", "W") or "W"
        periods = periods_for_frequency(freq)
        self.season: list[tuple[float, int, np.ndarray]] = []
        for name, feats in model.seasonality_features.items():
            coef = post.get(f"season_{name}")
            if coef is None:
                continue
            self.season.append((float(periods[name]), int(feats.shape[1] // 2), coef))
        # level offsets per fitted cell, and hyper-parameters for new levels
        lvl = post.get(LEVEL_OFFSET)
        self.level = (
            lvl.reshape(self.n, -1)
            if lvl is not None
            else np.zeros((self.n, self.n_geos * self.n_products))
        )
        self.geo_sigma = post.get("geo_sigma")
        self.product_sigma = post.get("product_sigma")
        # controls
        self.controls = fit.controls
        bc = post.get("beta_controls")
        k = len(fit.controls.features)
        self.beta_controls = bc.reshape(self.n, -1) if bc is not None else np.zeros((self.n, k))
        if self.beta_controls.shape[1] != k:
            raise MapiError(
                f"the posterior has {self.beta_controls.shape[1]} control coefficients for "
                f"{k} control features",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        self.control_mean = np.asarray(getattr(model, "control_mean", np.zeros(k)), dtype=float)
        self.control_std = np.asarray(getattr(model, "control_std", np.ones(k)), dtype=float)

    # ------------------------------------------------------------------ terms
    def trend_at(self, positions: np.ndarray) -> np.ndarray:
        """``(S, n)`` trend (standardised units) at period positions."""
        pos = np.asarray(positions, dtype=int)
        if self.trend_type == "none" or (self.trend_by_period is None and self.trend_slope is None):
            return np.zeros((self.n, pos.size))
        if self.trend_type == "linear" and self.trend_slope is not None:
            denom = max(self.n_periods - 1, 1)
            return self.trend_slope[:, None] * (pos[None, :] / denom)
        tb = self.trend_by_period
        if tb is None:  # pragma: no cover - compact_posterior always stores it
            raise MapiError(
                "the posterior has no per-period trend", code=ErrorCode.INTEGRITY_FAILURE
            )
        idx = np.clip(pos, 0, tb.shape[1] - 1)
        return tb[:, idx]

    def seasonality_at(self, positions: np.ndarray) -> np.ndarray:
        """``(S, n)`` Fourier seasonality (standardised units) at period positions."""
        from mmm_framework.transforms.seasonality import create_fourier_features

        pos = np.asarray(positions, dtype=float)
        out = np.zeros((self.n, pos.size))
        for period, order, coef in self.season:
            feats = create_fourier_features(pos, period, order)  # (n, 2*order)
            out += coef @ feats.T
        return out

    def _cell_index(self, cell: CellRef) -> int | None:
        if cell.new:
            return None
        assert cell.geo is not None and cell.product is not None
        return cell.geo * self.n_products + cell.product

    def _frozen(self, seed: int, name: str) -> np.ndarray:
        digest = hashlib.sha256(name.encode()).digest()
        rng = np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])
        return rng.standard_normal(self.n)

    def level_at(
        self, cells: Sequence[CellRef], strategy: Mapping[str, str], seed: int
    ) -> np.ndarray:
        """``(S, n_cell)`` baseline offsets; new levels per ``strategy`` (``pooled``/``zero``:
        none; ``hyperprior``: drawn from the fitted level spread)."""
        out = np.zeros((self.n, len(cells)))
        for j, cell in enumerate(cells):
            idx = self._cell_index(cell)
            if idx is not None:
                out[:, j] = self.level[:, idx]
                continue
            if (
                cell.geo is None
                and strategy.get("geo") == "hyperprior"
                and self.geo_sigma is not None
            ):
                out[:, j] += self.geo_sigma * self._frozen(seed, f"geo__new:{cell.label}")
            elif cell.geo is not None and self.n_geos > 1:
                out[:, j] += self._geo_part(cell.geo)
            if (
                cell.product is None
                and strategy.get("product") == "hyperprior"
                and self.product_sigma is not None
            ):
                out[:, j] += self.product_sigma * self._frozen(seed, f"product__new:{cell.label}")
            elif cell.product is not None and self.n_products > 1:
                out[:, j] += self._product_part(cell.product)
        return out

    def _geo_part(self, g: int) -> np.ndarray:
        """A fitted geo's own offset: ``geo_sigma * geo_offset[g]`` (else the mean offset of
        its cells, for estimators without a geo hierarchy)."""
        off = self.post.get("geo_offset")
        if off is not None and self.geo_sigma is not None:
            return np.asarray(self.geo_sigma * off.reshape(self.n, -1)[:, g])
        cells = [g * self.n_products + p for p in range(self.n_products)]
        return np.asarray(self.level[:, cells].mean(axis=1))

    def _product_part(self, p: int) -> np.ndarray:
        off = self.post.get("product_offset")
        if off is not None and self.product_sigma is not None:
            return np.asarray(self.product_sigma * off.reshape(self.n, -1)[:, p])
        cells = [g * self.n_products + p for g in range(self.n_geos)]
        return np.asarray(self.level[:, cells].mean(axis=1))

    def _beta(
        self, c: int, cells: Sequence[CellRef], strategy: Mapping[str, str], seed: int
    ) -> np.ndarray:
        """``(S, n_cell)`` effect of channel ``c`` per cell (a time-varying channel: ones,
        :meth:`beta_at` carries its coefficient)."""
        beta = self.beta[c]
        n_cell = len(cells)
        if c in self.tvp:
            out = np.ones((self.n, n_cell))
        elif beta.ndim == 1:
            out = np.repeat(beta[:, None], n_cell, axis=1)
        else:
            out = np.empty((self.n, n_cell))
            ch = self.channels[c]
            for j, cell in enumerate(cells):
                if cell.geo is not None:
                    out[:, j] = beta[:, cell.geo]
                elif strategy.get("geo") == "hyperprior":
                    mu = self.post.need(f"beta_{ch}_logmu")
                    tau = self.post.need(f"beta_{ch}_logtau")
                    out[:, j] = np.exp(
                        mu + tau * self._frozen(seed, f"beta_{ch}__new:{cell.label}")
                    )
                else:
                    out[:, j] = beta.mean(axis=1)
        if strategy.get("geo") == "zero" or strategy.get("product") == "zero":
            for j, cell in enumerate(cells):
                if cell.new:
                    out[:, j] = 0.0
        return out

    def beta_at(self, c: int, positions: np.ndarray, seed: int = 0) -> np.ndarray:
        """``(S, n)`` coefficient of time-varying channel ``c`` at period positions.

        Inside the fit window its fitted value; after it per ``tvp_forecast``: ``hold_last``
        the last fitted value, ``mean`` the average over the fit window, ``random_walk``
        the random walk continued from the last fitted value with the fitted step size,
        ``exp(log beta_last + tvpsigma * cumsum(z))``, ``z`` seeded by ``(seed,
        "tvp:<channel>")`` - the same path for every scenario of one request (common random
        numbers), and a longer horizon extends the same path."""
        tv = self.tvp[c]
        pos = np.asarray(positions, dtype=int)
        last = self.n_periods - 1
        out = tv[:, np.clip(pos, 0, last)]
        ahead = pos > last
        if not ahead.any():
            return out
        if self.tvp_forecast == "hold_last":
            return out
        if self.tvp_forecast == "mean":
            out[:, ahead] = tv.mean(axis=1)[:, None]
            return out
        h = int(pos.max()) - last
        z = _rng(seed, f"tvp:{self.channels[c]}").standard_normal((h, self.n)).T  # (S, h)
        drift = self.tvp_sigma[c][:, None] * np.cumsum(z, axis=1)
        path = np.log(np.maximum(tv[:, last], 1e-300))[:, None] + drift
        out[:, ahead] = np.exp(np.minimum(path[:, pos[ahead] - last - 1], _EXP_CAP))
        return out

    def channel_at(
        self,
        spend: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        *,
        strategy: Mapping[str, str] | None = None,
        seed: int = 0,
    ) -> np.ndarray:
        """``(S, C, n_cell, n)`` channel contributions (standardised units).

        ``spend`` is the raw spend ``(T_full, n_cell, C)`` over the whole history up to the
        last position (carry-over reads back ``l_max - 1`` periods).
        """
        from mmm_framework.frequentist._transforms import saturate

        strategy = strategy or {}
        pos = np.asarray(positions, dtype=int)
        n_cell = spend.shape[1]
        out = np.empty((self.n, len(self.channels), n_cell, pos.size))
        for c in range(len(self.channels)):
            kern = self.kernels[c]  # (S, L)
            lmax = kern.shape[1]
            x = spend[:, :, c] / self.media_max[c]  # (T_full, n_cell)
            lag_idx = pos[:, None] - np.arange(lmax)[None, :]  # (n, L)
            valid = lag_idx >= 0
            windows = np.where(
                valid[:, None, :], x[np.clip(lag_idx, 0, None)].transpose(0, 2, 1), 0.0
            )  # (n, n_cell, L)
            ad = np.einsum("nkl,sl->skn", windows, kern)  # (S, n_cell, n)
            params = {k: v[:, None, None] for k, v in self.sat_params[c].items()}
            sat = saturate(ad, self.sat_kind[c], **params)
            coef = self._beta(c, cells, strategy, seed)[:, :, None]
            if c in self.tvp:
                coef = coef * self.beta_at(c, pos, seed)[:, None, :]
            out[:, c] = coef * sat
        return out

    def controls_at(self, ctrl: np.ndarray, positions: np.ndarray) -> np.ndarray:
        """``(S, K_vars, n_cell, n)`` control contributions (standardised units), one per
        control variable (its features summed)."""
        pos = np.asarray(positions, dtype=int)
        k_vars = len(self.controls.variables)
        n_cell = ctrl.shape[1]
        out = np.zeros((self.n, k_vars, n_cell, pos.size))
        if not k_vars:
            return out
        z = (ctrl[pos] - self.control_mean) / self.control_std  # (n, n_cell, K)
        for v, name in enumerate(self.controls.variables):
            block = self.controls.block(name)
            if not block:
                continue
            out[:, v] = np.einsum("nkf,sf->skn", z[:, :, block], self.beta_controls[:, block])
        return out

    def evaluate(
        self,
        spend: np.ndarray,
        ctrl: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        *,
        strategy: Mapping[str, str] | None = None,
        seed: int = 0,
    ) -> Components:
        """Every term at ``positions`` for ``cells`` in KPI units."""
        strategy = strategy or {}
        pos = np.asarray(positions, dtype=int)
        notes: list[str] = []
        if (
            pos.size
            and int(pos.max()) >= self.n_periods
            and self.trend_type
            in (
                "piecewise",
                "spline",
                "gaussian_process",
            )
        ):
            notes.append(
                f"the {self.trend_type} trend is held at its last fitted level beyond the fit "
                "window, so the interval does not widen with the horizon"
            )
        if pos.size and int(pos.max()) >= self.n_periods:
            policy = {
                "random_walk": "a random walk from its last fitted value, so the interval "
                "widens with the horizon",
                "hold_last": "its last fitted value",
                "mean": "its average over the fit window",
            }[self.tvp_forecast]
            for c in self.tvp:
                notes.append(
                    f"the effect of {self.channels[c]} after the fit window follows {policy}"
                )
        level = self.level_at(cells, strategy, seed)  # (S, n_cell)
        baseline = (self.intercept[:, None] + level)[:, :, None] * self.y_std + self.y_mean
        baseline = np.broadcast_to(baseline, (self.n, len(cells), pos.size)).copy()
        return Components(
            channel=self.channel_at(spend, pos, cells, strategy=strategy, seed=seed) * self.y_std,
            baseline=baseline,
            trend=self.trend_at(pos) * self.y_std,
            seasonality=self.seasonality_at(pos) * self.y_std,
            controls=self.controls_at(ctrl, pos) * self.y_std,
            sigma=self.sigma,
            nu=self.nu,
            y_std=self.y_std,
            notes=notes,
            link=self.link,
            family=self.family,
            dispersion=self.dispersion,
        )

    # ------------------------------------------------------------------ totals
    def channel_totals(
        self,
        spend: np.ndarray,
        ctrl: np.ndarray,
        positions: np.ndarray,
        cells: Sequence[CellRef],
        multiplier: float,
    ) -> np.ndarray:
        """``(S, C, n_cell)`` KPI total over ``positions`` of each channel with only its
        spend scaled by ``multiplier``, against that channel at zero spend.

        Identity link: the channel terms add up, so this is the sum of the channel's own
        contribution. Other links: removal-based, ``sum_t mu(channel c at multiplier) -
        mu(channel c at zero)`` with every other term at the actual spend."""
        pos = np.asarray(positions, dtype=int)
        scaled = self.channel_at(spend * multiplier, pos, cells)  # (S, C, n_cell, n)
        if self.link == "identity":
            return np.asarray(scaled.sum(axis=3) * self.y_std)
        comps = self.evaluate(spend, ctrl, pos, cells)
        eta = comps.eta
        out = np.empty(scaled.shape[:3])
        for c in range(len(self.channels)):
            rest = eta - comps.channel[:, c]
            on = inverse_link(rest + scaled[:, c], self.link)
            off = inverse_link(rest, self.link)
            out[:, c] = (on - off).sum(axis=2)
        return out

    # ------------------------------------------------------------------ steady state
    def steady_state(
        self, spend: np.ndarray, cells: Sequence[CellRef], *, position: int | None = None
    ) -> np.ndarray:
        """``(S, n_cell, C)`` contribution per period (KPI units) of a constant raw spend
        ``(n_cell, C)`` held long enough for the carry-over to settle.

        ``position`` picks the coefficient of a time-varying channel (default: the last
        fitted period). Identity link: each channel's own term. Other links: the joint
        increment over the mean media-free link value of the fit window, allocated to the
        channels in proportion to their link-scale terms."""
        from mmm_framework.frequentist._transforms import saturate

        at = np.array([self.n_periods - 1 if position is None else int(position)])
        out = np.empty((self.n, len(cells), len(self.channels)))
        for c in range(len(self.channels)):
            level = spend[None, :, c] / self.media_max[c] * self.kernels[c].sum(axis=1)[:, None]
            params = {k: v[:, None] for k, v in self.sat_params[c].items()}
            sat = saturate(level, self.sat_kind[c], **params)
            coef = self._beta(c, cells, {}, 0)
            if c in self.tvp:
                coef = coef * self.beta_at(c, at)[:, :1]
            out[:, :, c] = coef * sat * self.y_std
        if self.link == "identity":
            return out
        base = self._media_free_eta(cells)  # (S, n_cell)
        total = out.sum(axis=2)
        lift = inverse_link(base + total, self.link) - inverse_link(base, self.link)
        share = np.divide(out, total[:, :, None], out=np.zeros_like(out),
                          where=total[:, :, None] != 0)  # fmt: skip
        return np.asarray(share * lift[:, :, None])

    def _media_free_eta(self, cells: Sequence[CellRef]) -> np.ndarray:
        """``(S, n_cell)`` mean link value without media over the fit window (a new cell:
        its baseline at the fitted cells' average controls)."""
        key = tuple(cells)
        cached = self._base_eta.get(key)
        if cached is not None:
            return cached
        t = self.fit.n_periods
        hist_s, hist_c = history_arrays(self.fit)
        spend = np.zeros((t, len(cells), hist_s.shape[2]))
        ctrl = np.repeat(hist_c.mean(axis=1, keepdims=True), len(cells), axis=1)
        for j, cell in enumerate(cells):
            idx = self._cell_index(cell)
            if idx is not None:
                ctrl[:, j] = hist_c[:, idx]
        comps = self.evaluate(spend, ctrl, np.arange(t), cells)
        base = (comps.eta - comps.channel.sum(axis=1)).mean(axis=2)
        self._base_eta[key] = base
        return base

    def fit_cells(self) -> list[CellRef]:
        """The fitted cells in mmm-framework's order (geo-major, then product)."""
        return [
            CellRef(g, p, f"{g}/{p}") for g in range(self.n_geos) for p in range(self.n_products)
        ]


def history_arrays(fit: PanelData) -> tuple[np.ndarray, np.ndarray]:
    """``(T, n_cell, C)`` spend and ``(T, n_cell, K)`` control features of the fit window."""
    t = fit.n_periods
    return (
        fit.spend.reshape(t, fit.n_cells, -1),
        fit.ctrl.reshape(t, fit.n_cells, -1),
    )


def stack_chains(arr: np.ndarray, chains: int, draws: int) -> np.ndarray:
    """``(S, ...)`` back to ``(chain, draw, ...)``."""
    return arr.reshape(chains, draws, *arr.shape[1:])


def sample_shape(post: xr.Dataset) -> tuple[int, int] | None:
    """``(chains, draws)`` of a posterior with chain/draw dims, else ``None``."""
    if set(_DRAW_DIMS) <= set(map(str, post.dims)):
        return int(post.sizes["chain"]), int(post.sizes["draw"])
    return None


def as_float(values: Any) -> np.ndarray:
    return np.asarray(values, dtype=np.float64)
