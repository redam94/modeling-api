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
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError

from mapi_plugin_mmm.framework import PanelData

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mmm_framework.model import BayesianMMM

__all__ = [
    "LEVEL_OFFSET",
    "TREND_BY_PERIOD",
    "CellRef",
    "Components",
    "Posterior",
    "Replay",
    "adstock_kernels",
    "compact_posterior",
]

TREND_BY_PERIOD: Final = "trend_by_period"
LEVEL_OFFSET: Final = "level_offset"
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
    keep: list[str] = []
    for name in map(str, post.data_vars):
        da = post[name]
        if name in _OBS_DETERMINISTICS or "obs" in da.dims:
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
    """Every additive term in KPI units, per sample: ``channel (S, C, n_cell, n)``,
    ``baseline (S, n_cell, n)`` (``y_mean`` + intercept + level offsets), ``trend`` and
    ``seasonality`` ``(S, n)``, ``controls (S, K, n_cell, n)`` (one per control variable)."""

    channel: np.ndarray
    baseline: np.ndarray
    trend: np.ndarray
    seasonality: np.ndarray
    controls: np.ndarray
    sigma: np.ndarray
    nu: float | None
    y_std: float
    notes: list[str] = field(default_factory=list)

    @property
    def mu(self) -> np.ndarray:
        """The expected KPI ``(S, n_cell, n)``."""
        total = self.baseline + self.trend[:, None, :] + self.seasonality[:, None, :]
        total = total + self.channel.sum(axis=1)
        if self.controls.shape[1]:
            total = total + self.controls.sum(axis=1)
        return total

    def noise(self, seed: int, name: str = "y") -> np.ndarray:
        """Observation noise ``(S, n_cell, n)`` in KPI units, from a generator seeded by
        ``(seed, name)`` - a counterfactual's scenario and baseline share it (common random
        numbers)."""
        digest = hashlib.sha256(name.encode()).digest()
        rng = np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])
        shape = self.baseline.shape
        eps = (
            rng.standard_t(self.nu, size=shape)
            if self.nu is not None
            else rng.standard_normal(shape)
        )
        return eps * self.sigma[:, None, None] * self.y_std


class Replay:
    """The forward pass of a fitted ``BayesianMMM`` (see the module doc).

    ``model`` is the model rebuilt from the bundle's ``fit_data`` (its scaling, Fourier
    periods, configs); ``post`` the posterior (all draws or a thinned subset).
    """

    def __init__(self, model: BayesianMMM, fit: PanelData, post: Posterior) -> None:
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
        for ch in self.channels:
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
        self.intercept = post.need("intercept")
        sigma = post.get("sigma")
        self.sigma = sigma if sigma is not None else np.zeros(self.n)
        lik = getattr(model.model_config, "likelihood", None)
        fam = getattr(getattr(lik, "family", None), "value", "normal")
        self.nu: float | None = (
            float((getattr(lik, "params", None) or {}).get("nu", 4.0))
            if fam == "student_t"
            else None
        )
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
        """``(S, n_cell)`` effect of channel ``c`` per cell."""
        beta = self.beta[c]
        n_cell = len(cells)
        if beta.ndim == 1:
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
            out[:, c] = self._beta(c, cells, strategy, seed)[:, :, None] * sat
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
        )

    # ------------------------------------------------------------------ steady state
    def steady_state(self, spend: np.ndarray, cells: Sequence[CellRef]) -> np.ndarray:
        """``(S, n_cell, C)`` contribution per period (KPI units) of a constant raw spend
        ``(n_cell, C)`` held long enough for the carry-over to settle."""
        from mmm_framework.frequentist._transforms import saturate

        out = np.empty((self.n, len(cells), len(self.channels)))
        for c in range(len(self.channels)):
            level = spend[None, :, c] / self.media_max[c] * self.kernels[c].sum(axis=1)[:, None]
            params = {k: v[:, None] for k, v in self.sat_params[c].items()}
            sat = saturate(level, self.sat_kind[c], **params)
            out[:, :, c] = self._beta(c, cells, {}, 0) * sat * self.y_std
        return out

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
