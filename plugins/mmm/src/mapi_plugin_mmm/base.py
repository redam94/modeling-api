"""The platform contract on top of mmm-framework's ``BayesianMMM`` (shared by the Bayesian
and the frequentist plugins).

**fit** maps the model-stage data and settings onto mmm-framework
(:mod:`mapi_plugin_mmm.framework`), fits, and stores in the bundle - never a pickle:

* ``posterior`` - mmm-framework's parameters plus the compact replay variables
  (:func:`~mapi_plugin_mmm.forward.compact_posterior`);
* ``sample_stats``, a thinned ``prior`` of the free parameters (the scorecard's
  prior/posterior overlap), ``log_likelihood`` when the spec keeps it;
* ``fit_data`` - the model-stage KPI, spend and control features, from which scoring
  rebuilds the model with the fit's exact scaling.

**score** replays the fitted model (:class:`~mapi_plugin_mmm.forward.Replay`) on the
scored window with the full spend history in front of it (the model owns carry-over),
for fitted and new panel cells: ``y_hat(chain, draw, *panel, time)`` - ``expected`` is the
mean, ``predictive`` adds observation noise from a seeded generator (a counterfactual's
scenario and baseline share it) - and ``contributions(chain, draw, component, *panel,
time)``: every channel, ``baseline`` (intercept and level offsets), ``trend``,
``seasonality`` and every control; they sum to ``y_hat`` (``contribution_scale:
additive``). Outputs are in the model stage's units (``owns: inverse_units``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, Final

import numpy as np
import xarray as xr
from mapi_sdk.models import PREDICTIONS, CustomModel, Fitted, ScoreContext
from mapi_sdk.summarise import select_draws
from mapi_spec.capabilities import Diagnostic
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.optimize import OptimizeRequest, OptimizeResult
from mapi_spec.spec import FitConfig, ScoreRequest

from mapi_plugin_mmm.derived import (
    FitWindow,
    contributions_dataset,
    marginal_cost_dataset,
    response_curves_dataset,
)
from mapi_plugin_mmm.forward import CellRef, Posterior, Replay, compact_posterior
from mapi_plugin_mmm.framework import (
    BASELINE,
    CHANNEL,
    COMPONENT,
    FIT_DATA,
    GEO,
    PRODUCT,
    SEASONALITY,
    SPEND,
    TARGET,
    TIME,
    TREND,
    Controls,
    PanelData,
    detect_controls,
    framework_version,
    infer_frequency,
    quiet,
    rebuild_model,
)
from mapi_plugin_mmm.planning import optimize_budget_plan, thin_index
from mapi_plugin_mmm.settings import BayesianSettings, FrequentistSettings

__all__ = ["SCHEMA_TEMPLATE", "FrameworkMMM", "draw_samples", "positions_of"]

SCHEMA_TEMPLATE: Final[dict[str, Any]] = {
    "params": {"panel": {"type": "string_list", "path": "hierarchy.pool_over", "default": []}},
    "template": {
        "dims": {
            TIME: {"kind": "time", "policy": "extend"},
            CHANNEL: {"kind": "categorical", "policy": "fixed"},
            "{*panel as d}": {
                "kind": "categorical",
                "policy": "new_levels",
                "new_level_strategy": "pooled",
            },
        },
        "vars": {
            TARGET: {"dims": ["{*panel}", TIME], "dtype": "float64", "role": "target"},
            SPEND: {
                "dims": ["{*panel}", CHANNEL, TIME],
                "dtype": "float64",
                "role": "spend",
                "future_required": True,
            },
        },
    },
}
"""Canonical names: ``y(*panel, time)``, ``spend(*panel, channel, time)``; the panel dims
are ``hierarchy.pool_over``. New geos/products are scored from the pooled level."""

OUTPUTS: Final[dict[str, Any]] = {
    "y_hat": {
        "dims": [TIME],
        "dims_of": TARGET,
        "units_of": TARGET,
        "description": "Expected (or predictive) KPI over the panel dims and time",
    },
    "contributions": {
        "dims": [COMPONENT, TIME],
        "dims_of": TARGET,
        "units_of": TARGET,
        "description": "Every channel, the baseline, trend, seasonality and each control; "
        "they sum to y_hat",
    },
}
DERIVED: Final[list[dict[str, Any]]] = [
    {
        "name": "contributions",
        "dims": ["sample", CHANNEL, TIME],
        "units": "target",
        "cache": "fit",
        "description": "Channel, baseline, trend, seasonality and control contributions over "
        "the fit window per thinned draw (panel dims included)",
    },
    {
        "name": "response_curves",
        "dims": ["sample", CHANNEL, "multiplier"],
        "units": "target",
        "cache": "fit",
        "description": "Total contribution per spend multiplier and channel (and geo) per "
        "thinned draw (Guided-mode slider grids, budget optimisation)",
    },
    {
        "name": "marginal_cost",
        "dims": ["sample", CHANNEL],
        "units": "spend per target",
        "cache": "fit",
        "description": "Extra spend per extra unit of the KPI at current spend",
    },
]
_PRIOR_KEEP: Final = 200


def draw_samples(posterior: xr.Dataset, draw_index: np.ndarray | None) -> xr.Dataset:
    """Posterior draws along one ``sample`` dim (``draw_index``: the bundle thinning index;
    ``None`` uses every draw; an empty index the posterior mean)."""
    if draw_index is None:
        stacked = posterior.stack({"sample": ("chain", "draw")}, create_index=False)
        stacked = stacked.drop_vars(["chain", "draw"], errors="ignore")
        return stacked.assign_coords({"sample": np.arange(stacked.sizes["sample"])})
    if len(draw_index) == 0:
        return posterior.mean(("chain", "draw")).expand_dims({"sample": [0]})
    out = select_draws(posterior, np.asarray(draw_index), len(draw_index))
    return out.drop_vars(["chain", "draw"], errors="ignore")


def _unit_index(times: np.ndarray, t0: Any, frequency: str) -> np.ndarray:
    """Periods from ``t0`` to every label (``NaN`` when not a whole number of periods)."""
    arr = np.asarray(times)
    if arr.dtype.kind == "M":
        a = arr.astype("datetime64[D]")
        b = np.datetime64(t0, "D")
        if frequency == "M":
            am = a.astype("datetime64[M]").astype(np.int64)
            bm = np.datetime64(t0, "M").astype(np.int64)
            return (am - bm).astype(float)
        days = (a - b).astype(np.int64).astype(float)
        step = {"D": 1.0, "W": 7.0}[frequency]
        out = days / step
        return np.where(np.isclose(out, np.round(out)), np.round(out), np.nan)
    return np.asarray(arr, dtype=float) - float(t0)


def positions_of(fit_times: np.ndarray, times: np.ndarray, frequency: str) -> np.ndarray:
    """Period positions of score labels relative to the fit start (``COORD_NOT_ALLOWED``
    before the start or between fit labels, ``NON_CONTIGUOUS_EXTENSION`` for a gap after
    the fit window)."""
    fit_times = np.asarray(fit_times)
    n_fit = fit_times.size
    if fit_times.dtype.kind == "M" or np.asarray(times).dtype.kind == "M":
        raw = _unit_index(times, fit_times[0], frequency)
    else:
        diffs = np.diff(fit_times.astype(float))
        step = float(np.median(diffs)) if diffs.size else 1.0
        raw = (np.asarray(times, dtype=float) - float(fit_times[0])) / step
        raw = np.where(np.isclose(raw, np.round(raw)), np.round(raw), np.nan)
    if np.isnan(raw).any() or (raw < 0).any():
        raise MapiError(
            f"{int(np.isnan(raw).sum() + (raw < 0).sum())} time label(s) are before the fit "
            "window or off the data's period grid",
            code=ErrorCode.COORD_NOT_ALLOWED,
            pointer="/data",
        )
    pos = raw.astype(int)
    inside = pos < n_fit
    if inside.any():
        want = fit_times[pos[inside]]
        got = np.asarray(times)[inside]
        if not np.array_equal(np.asarray(want).astype(got.dtype), got):
            raise MapiError(
                "time labels inside the fit window do not match the fitted periods",
                code=ErrorCode.COORD_NOT_ALLOWED,
                pointer="/data",
            )
    beyond = np.unique(pos[~inside])
    if beyond.size and not np.array_equal(beyond, np.arange(n_fit, beyond.max() + 1)):
        raise MapiError(
            "the scored periods after the fit window must follow it without a gap",
            code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
            pointer="/data",
        )
    return pos


class FrameworkMMM[SettingsT: BayesianSettings | FrequentistSettings](CustomModel[SettingsT]):
    """Shared fit/score/derive/optimise of the mmm-framework plugins."""

    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = OUTPUTS
    derived: ClassVar[list[dict[str, Any]]] = DERIVED
    pad_extend = False
    default_engine: ClassVar[str] = "nutpie"
    _cache: tuple[Any, Any, Replay, PanelData] | None = None

    # ------------------------------------------------------------------ helpers
    def _settings(self, fitted: Fitted) -> SettingsT:
        return self.parse_settings(fitted.settings)

    @staticmethod
    def _controls(fitted: Fitted) -> Controls:
        return Controls.from_json(fitted.learned.get("controls"))

    @staticmethod
    def _frequency(fitted: Fitted) -> str:
        freq = fitted.learned.get("frequency")
        if freq not in ("W", "D", "M"):
            raise MapiError(
                "the bundle does not record the data frequency", code=ErrorCode.INTEGRITY_FAILURE
            )
        return str(freq)

    def replay(
        self, fitted: Fitted, posterior: xr.Dataset | None = None
    ) -> tuple[Replay, PanelData]:
        """The fitted model's forward pass over ``posterior`` (default: all draws)."""
        post = fitted.posterior if posterior is None else posterior
        cached = self._cache
        if cached is not None and cached[0] is fitted and cached[1] is post:
            return cached[2], cached[3]
        s = self._settings(fitted)
        model, fit = rebuild_model(
            fitted.group(FIT_DATA),
            s,
            controls=self._controls(fitted),
            frequency=self._frequency(fitted),
        )
        rp = Replay(model, fit, Posterior.from_dataset(post))
        self._cache = (fitted, post, rp, fit)
        return rp, fit

    # ------------------------------------------------------------------ fit
    def _panel(self, ds: xr.Dataset, s: SettingsT, *, with_target: bool) -> tuple[PanelData, str]:
        controls = detect_controls(ds, s.controls)
        data = PanelData.from_dataset(ds, s.panel_dims, controls, with_target=with_target)
        freq = infer_frequency(data.times, s.frequency)
        return data, freq

    def _groups(
        self,
        model: Any,
        data: PanelData,
        cfg: FitConfig,
        *,
        bayesian: bool,
    ) -> dict[str, xr.Dataset]:
        trace = model._trace
        post = trace["posterior"].to_dataset(inherit=False)
        groups: dict[str, xr.Dataset] = {
            "posterior": _clean(
                compact_posterior(post, n_obs=int(model.n_obs), n_cells=int(model.n_cells))
            ),
            FIT_DATA: _clean(data.to_dataset()),
        }
        if "sample_stats" in trace.children:
            stats = trace["sample_stats"].to_dataset(inherit=False)
            groups["sample_stats"] = _clean(
                stats.drop_vars(
                    [v for v in stats.data_vars if stats[v].dtype.kind not in "biuf"],
                    errors="ignore",
                )
            )
        if bayesian and "prior" in trace.children:
            prior = trace["prior"].to_dataset(inherit=False)
            free = [rv.name for rv in model.model.free_RVs if rv.name in prior]
            if free:
                kept = prior[free]
                n = int(kept.sizes.get("draw", 0))
                if n > _PRIOR_KEEP:
                    kept = kept.isel(draw=thin_index(n, _PRIOR_KEEP))
                groups["prior"] = _clean(kept)
        if bayesian and cfg.keep.log_likelihood:
            import pymc as pm

            try:
                with quiet():
                    ll = pm.compute_log_likelihood(
                        xr.DataTree.from_dict({"posterior": post}),
                        model=model.model,
                        var_names=["y_obs"],
                        extend_inferencedata=False,
                        progressbar=False,
                    )
                ll_ds = ll if isinstance(ll, xr.Dataset) else ll["log_likelihood"].to_dataset()
                groups["log_likelihood"] = _clean(ll_ds)
            except Exception:
                pass
        return groups

    @staticmethod
    def _diagnostics_json(raw: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(v, bool | int | str) or v is None:
                out[str(k)] = v
            elif isinstance(v, float):
                out[str(k)] = v if np.isfinite(v) else None
            elif isinstance(v, np.generic):
                item = v.item()
                out[str(k)] = item if not isinstance(item, float) or np.isfinite(item) else None
        return out

    def _learned(
        self, data: PanelData, freq: str, model: Any, extra: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "mmm_framework": framework_version(),
            "frequency": freq,
            "panel": list(data.panel),
            "channels": list(data.channels),
            "controls": data.controls.to_json(),
            "scaling": {
                "y_mean": float(model.y_mean),
                "y_std": float(model.y_std),
                "media_max": {c: float(model._media_raw_max[c]) for c in data.channels},
            },
            **extra,
        }

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        rp, fit = self.replay(fitted)
        s = self._settings(fitted)
        controls = self._controls(fitted)
        freq = self._frequency(fitted)
        order = np.asarray(ds[TIME].values) if TIME in ds.coords else None
        work = ds.sortby(TIME) if order is not None else ds
        data = PanelData.from_dataset(
            work,
            s.panel_dims,
            list(controls.variables),
            with_target=False,
            fill_missing=True,
            control_features=controls,
        )
        pos = positions_of(fit.times, data.times, freq)
        cells = self._cells(data, fit, ctx)
        strategy = {d: ctx.strategy(d) for d in data.panel}
        spend_full, ctrl_full = self._history(fit, data, cells, pos)
        ctx.check_cancelled()
        comps = rp.evaluate(spend_full, ctrl_full, pos, cells, strategy=strategy, seed=ctx.seed)
        for note in comps.notes:
            ctx.info(note)
        mu = comps.mu
        kind = "expected"
        noise = "none"
        if ctx.kind == "predictive":
            mu = mu + comps.noise(ctx.seed)
            kind, noise = "predictive", "crn_seeded"
        parts = [comps.channel.transpose(1, 0, 2, 3)]  # (C, S, n_cell, n)
        labels = list(fit.channels)
        n_cell = len(cells)
        n = rp.n
        for name, arr in (
            (BASELINE, comps.baseline),
            (TREND, np.broadcast_to(comps.trend[:, None, :], (n, n_cell, pos.size))),
            (SEASONALITY, np.broadcast_to(comps.seasonality[:, None, :], (n, n_cell, pos.size))),
        ):
            parts.append(np.asarray(arr)[None])
            labels.append(name)
        if comps.controls.shape[1]:
            parts.append(comps.controls.transpose(1, 0, 2, 3))
            labels += list(controls.variables)
        contrib = np.concatenate(parts, axis=0).transpose(1, 0, 2, 3)  # (S, K, n_cell, n)
        y_hat = self._labelled(mu, data, fitted, lead=[], lead_coords={})
        y_hat.attrs = {"kind": kind, "noise": noise}
        contributions = self._labelled(
            contrib, data, fitted, lead=[COMPONENT], lead_coords={COMPONENT: np.asarray(labels)}
        )
        contributions.attrs = {"kind": "expected", "noise": "none"}
        pred = xr.Dataset({"y_hat": y_hat, "contributions": contributions})
        if order is not None:
            pred = pred.sel({TIME: order})
        pred.attrs = {"units": "original", "owner": "mmm-framework"}
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    @staticmethod
    def _cells(data: PanelData, fit: PanelData, ctx: ScoreContext) -> list[CellRef]:
        def index(labels: list[str] | None, fitted: list[str] | None, dim: str) -> list[int | None]:
            if not labels:
                return [0]
            out: list[int | None] = []
            for lab in labels:
                if fitted is not None and lab in fitted:
                    out.append(fitted.index(lab))
                    continue
                if ctx.strategy(dim) == "error":
                    raise MapiError(
                        f"{dim} {lab!r} was not seen at fit and the {dim} new-level strategy "
                        "is 'error'",
                        code=ErrorCode.UNSEEN_LEVELS,
                        pointer="/data",
                    )
                out.append(None)
            return out

        geos = index(data.geos, fit.geos, GEO)
        products = index(data.products, fit.products, PRODUCT)
        return [
            CellRef(g, p, f"{(data.geos or ['_'])[gi]}/{(data.products or ['_'])[pi]}")
            for gi, g in enumerate(geos)
            for pi, p in enumerate(products)
        ]

    @staticmethod
    def _history(
        fit: PanelData, data: PanelData, cells: list[CellRef], pos: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Raw spend and controls ``(T_full, n_cell, ·)``: the fit history of each cell with
        the scored periods written over it (new cells start from zero spend)."""
        n_fit = fit.n_periods
        t_full = max(n_fit, int(pos.max()) + 1 if pos.size else n_fit)
        n_cell = len(cells)
        spend = np.zeros((t_full, n_cell, len(fit.channels)))
        ctrl = np.zeros((t_full, n_cell, len(fit.controls.features)))
        hist_s = fit.spend.reshape(n_fit, fit.n_cells, -1)
        hist_c = fit.ctrl.reshape(n_fit, fit.n_cells, -1)
        for j, cell in enumerate(cells):
            if cell.new:
                continue
            assert cell.geo is not None and cell.product is not None
            k = cell.geo * fit.n_products + cell.product
            spend[:n_fit, j] = hist_s[:, k]
            ctrl[:n_fit, j] = hist_c[:, k]
        spend[pos] = data.spend.reshape(data.n_periods, n_cell, -1)
        ctrl[pos] = data.ctrl.reshape(data.n_periods, n_cell, -1)
        return spend, ctrl

    @staticmethod
    def _labelled(
        values: np.ndarray,
        data: PanelData,
        fitted: Fitted,
        *,
        lead: list[str],
        lead_coords: dict[str, Any],
    ) -> xr.DataArray:
        """``(S, *lead, n_cell, n)`` to ``(chain, draw, *lead, *panel, time)``."""
        post = fitted.posterior
        chains, draws = int(post.sizes["chain"]), int(post.sizes["draw"])
        shape = [chains, draws, *values.shape[1 : 1 + len(lead)], data.n_geos, data.n_products,
                 data.n_periods]  # fmt: skip
        arr = values.reshape(shape)
        dims = ["chain", "draw", *lead, GEO, PRODUCT, TIME]
        da = xr.DataArray(
            arr,
            dims=dims,
            coords={
                "chain": post["chain"].values,
                "draw": post["draw"].values,
                **lead_coords,
                TIME: data.times,
            },
        )
        drop = [d for d in (GEO, PRODUCT) if d not in data.panel]
        if drop:
            da = da.isel({d: 0 for d in drop}, drop=True)
        coords: dict[str, Any] = {}
        if data.geos:
            coords[GEO] = np.asarray(data.geos)
        if data.products:
            coords[PRODUCT] = np.asarray(data.products)
        return da.assign_coords(coords)

    # ------------------------------------------------------------------ derived
    def _window(self, fitted: Fitted, draw_index: np.ndarray | None) -> FitWindow:
        post = draw_samples(fitted.posterior, draw_index)
        rp, fit = self.replay(fitted, post)
        return FitWindow(rp, fit)

    def derive(self, fitted: Fitted, name: str, ds: xr.Dataset, ctx: ScoreContext) -> xr.Dataset:
        """Derived quantities over the fit window (see :mod:`mapi_plugin_mmm.derived`)."""
        s = self._settings(fitted)
        to_original = getattr(ctx, "to_original", None)
        win = self._window(fitted, ctx.draw_index)
        if name == "contributions":
            return contributions_dataset(win, to_original)
        if name == "response_curves":
            return response_curves_dataset(win, s.grid, to_original)
        if name == "marginal_cost":
            return marginal_cost_dataset(win, s.grid, to_original)
        raise MapiError(
            f"model {self.id!r} has no derived quantity {name!r}",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/outputs/derived",
        )

    # ------------------------------------------------------------------ decisions
    def optimize(
        self, fitted: Fitted, request: OptimizeRequest, ctx: ScoreContext
    ) -> OptimizeResult:
        """The budget plan for ``request`` (:mod:`mapi_plugin_mmm.planning`), over at most
        ``request.max_draws`` evenly thinned posterior draws."""
        post = fitted.posterior
        n = int(post.sizes["chain"]) * int(post.sizes["draw"])
        idx = thin_index(n, request.max_draws)
        stacked = post.stack({"sample": ("chain", "draw")}, create_index=False)
        stacked = stacked.drop_vars(["chain", "draw"], errors="ignore").isel(sample=idx)
        rp, fit = self.replay(fitted, stacked.assign_coords(sample=np.arange(idx.size)))
        ctx.check_cancelled()
        return optimize_budget_plan(FitWindow(rp, fit), request, seed=ctx.seed)

    def objective(self, fitted: Fitted, decision: xr.Dataset, ctx: ScoreContext) -> xr.DataArray:
        """Expected total KPI of a per-period budget at steady state, per posterior draw.

        ``decision["spend"]`` is spend per period over ``channel`` (split across the panel
        cells in proportion to each cell's fit-window spend) or over ``(geo, channel)``;
        channels left out get zero. ``decision.attrs["periods"]`` (default 1) is how many
        periods the budget runs."""
        if "spend" not in decision:
            raise MapiError(
                "the decision needs a 'spend' variable over channel or (geo, channel)",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        periods = float(decision.attrs.get("periods", 1))
        if not periods > 0:
            raise MapiError(
                "decision periods must be > 0",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/periods",
            )
        rp, fit = self.replay(fitted)
        da = decision["spend"]
        extra = sorted(set(map(str, da.dims)) - {GEO, CHANNEL})
        if CHANNEL not in da.dims or extra:
            raise MapiError(
                f"decision spend must be over channel or (geo, channel), got {list(da.dims)}",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        unknown = sorted(set(map(str, da[CHANNEL].values)) - set(fit.channels))
        if unknown:
            raise MapiError(
                f"unknown channel(s) {unknown}",
                code=ErrorCode.UNSEEN_LEVELS,
                pointer="/decision/spend",
            )
        values = np.asarray(da.values, dtype=float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise MapiError(
                "decision spend must be finite and non-negative",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/decision/spend",
            )
        hist = fit.spend.reshape(fit.n_periods, fit.n_cells, -1).sum(axis=0)  # (cell, C)
        col = hist.sum(axis=0, keepdims=True)
        share = np.where(col > 0, hist / np.where(col > 0, col, 1.0), 1.0 / fit.n_cells)
        if GEO in da.dims:
            if not fit.geos:
                raise MapiError(
                    "the model has no geo dim", code=ErrorCode.SCHEMA_INVALID,
                    pointer="/decision/spend",
                )  # fmt: skip
            bad = sorted(set(map(str, da[GEO].values)) - set(fit.geos))
            if bad:
                raise MapiError(
                    f"geo(s) {bad} were not seen at fit",
                    code=ErrorCode.UNSEEN_LEVELS,
                    pointer="/decision/spend",
                )
            full = da.reindex({GEO: fit.geos, CHANNEL: fit.channels}, fill_value=0.0)
            per_geo = np.asarray(full.transpose(GEO, CHANNEL).values, dtype=float)
            # within a geo, split across products by their fit-window spend
            cell_geo = np.repeat(np.arange(fit.n_geos), fit.n_products)
            geo_tot = np.zeros((fit.n_geos, hist.shape[1]))
            np.add.at(geo_tot, cell_geo, hist)
            within = np.where(
                geo_tot[cell_geo] > 0,
                hist / np.where(geo_tot[cell_geo] > 0, geo_tot[cell_geo], 1.0),
                1.0 / fit.n_products,
            )
            spend = per_geo[cell_geo] * within
        else:
            ch = np.asarray(da.reindex({CHANNEL: fit.channels}, fill_value=0.0).values, dtype=float)
            spend = ch[None, :] * share
        per_period = rp.steady_state(spend, rp.fit_cells())  # (S, cell, C)
        total = per_period.sum(axis=(1, 2)) * periods
        post = fitted.posterior
        out = xr.DataArray(
            total.reshape(post.sizes["chain"], post.sizes["draw"]),
            dims=["chain", "draw"],
            coords={"chain": post["chain"].values, "draw": post["draw"].values},
            name="objective",
        )
        out.attrs = {
            "units": "target (model-stage units)",
            "periods": periods,
            "description": "expected total KPI of the budget at steady state",
        }
        return out


def _clean(ds: xr.Dataset) -> xr.Dataset:
    """Bundle-safe group: numeric/string arrays only, plain coords, no volatile attributes."""
    keep = [v for v in ds.data_vars if ds[v].dtype.kind in "biufcMmU"]
    out = ds[keep]
    fixes = {}
    for c in out.coords:
        if out[c].dtype.kind == "O":
            vals = np.asarray(out[c].values.tolist())
            fixes[c] = (out[c].dims, vals if vals.dtype.kind != "O" else vals.astype(str))
        elif out[c].dtype.kind == "M":
            fixes[c] = (out[c].dims, np.asarray(out[c].values).astype("datetime64[ns]"))
    if fixes:
        out = out.assign_coords(fixes)
    for v in out.variables:
        out[v].attrs = {}
    out.attrs = {k: v for k, v in ds.attrs.items() if k == "panel" and isinstance(v, str)} | {
        "inference_library": "mmm-framework"
    }
    return out


def diagnostics_from(raw: Mapping[str, Any]) -> list[Diagnostic]:
    """mmm-framework's fit notes as platform diagnostics (approximate fits, caveats)."""
    out: list[Diagnostic] = []
    if raw.get("approximate"):
        out.append(
            Diagnostic(
                code="APPROXIMATE_FIT",
                severity="warning",
                message_plain="This fit used a fast approximation; its ranges are not "
                "reliable. Refit with full sampling before making decisions.",
                message_expert=f"fit_method={raw.get('fit_method')}: uncertainty is not "
                "calibrated (mmm-framework approximate inference)",
            )
        )
    return out
