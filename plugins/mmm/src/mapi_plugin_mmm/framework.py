"""Our model-stage data and settings, mapped onto mmm-framework (and back).

Model-stage data (canonical names, after a pipeline that only converts currencies and
resamples - mmm-framework owns scaling and carry-over)::

    y(*panel, time)                 target (KPI)
    spend(*panel, channel, time)    spend per channel
    <control>(*panel?, time, ...)   controls; extra dims (Fourier modes, holiday names) are
                                    flattened into one feature per label

``*panel`` are the dims listed in ``hierarchy.pool_over`` (``geo``, ``product``). The
panel becomes mmm-framework's :class:`~mmm_framework.data_loader.PanelDataset` with the
MFF dims ``Period`` (+ ``Geography``, ``Product``) - observations period-major, then geo,
then product, exactly the layout mmm-framework's ``MFFLoader`` builds from a Master Flat
File, so a dataset ingested with our ``mff`` preset and one loaded with mmm-framework's
own loader give the same model.

The fit data is persisted in the bundle as the ``fit_data`` group (never pickled); scoring
rebuilds the ``BayesianMMM`` from it (:func:`rebuild_model`), which reproduces the fit's
scaling (``y_mean``, ``y_std``, the media and control scales) exactly.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError

from mapi_plugin_mmm.settings import (
    AdstockSettings,
    BayesianSettings,
    FrequentistSettings,
    SaturationSettings,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mmm_framework.config import MFFConfig, ModelConfig
    from mmm_framework.data_loader import PanelDataset
    from mmm_framework.model import BayesianMMM
    from mmm_framework.model.trend_config import TrendConfig

__all__ = [
    "APPROX_ENGINES",
    "BASELINE",
    "CHANNEL",
    "COMPONENT",
    "FIT_DATA",
    "GEO",
    "MCMC_ENGINES",
    "PANEL_DIMS",
    "PRODUCT",
    "RESERVED_COMPONENTS",
    "SEASONALITY",
    "SPEND",
    "TARGET",
    "TIME",
    "TREND",
    "Controls",
    "EnginePlan",
    "PanelData",
    "build_configs",
    "detect_controls",
    "engine_plan",
    "framework_version",
    "infer_frequency",
    "quiet",
    "rebuild_model",
]

TIME: Final = "time"
CHANNEL: Final = "channel"
TARGET: Final = "y"
SPEND: Final = "spend"
GEO: Final = "geo"
PRODUCT: Final = "product"
PANEL_DIMS: Final = (GEO, PRODUCT)
COMPONENT: Final = "component"
BASELINE: Final = "baseline"
TREND: Final = "trend"
SEASONALITY: Final = "seasonality"
RESERVED_COMPONENTS: Final = (BASELINE, TREND, SEASONALITY)
FIT_DATA: Final = "fit_data"
"""Bundle group holding the model-stage fit data (``y``, ``spend``, control features)."""

_MFF_DIM = {GEO: "Geography", PRODUCT: "Product"}
_SYNTHETIC_START = "2000-01-02"
_SYNTHETIC_FREQ = {"W": "W-SUN", "D": "D", "M": "MS"}

MCMC_ENGINES: Final = ("nutpie", "pymc", "numpyro")
"""Engines that run NUTS (mmm-framework ``nuts_sampler``)."""
APPROX_ENGINES: Final = {
    "map": "map",
    "advi": "advi",
    "fullrank_advi": "fullrank_advi",
    "pathfinder": "pathfinder",
    "laplace": "laplace",
    "smc": "smc",
}


def framework_version() -> str:
    """The installed mmm-framework version (recorded in every bundle's lineage)."""
    import mmm_framework

    return str(getattr(mmm_framework, "__version__", "unknown"))


@contextmanager
def quiet() -> Iterator[None]:
    """Silence library logging, warnings and progress output around mmm-framework calls
    (it logs through loguru and PyMC prints sampling banners)."""
    names = ("pymc", "pytensor", "mmm_framework", "numpyro", "jax")
    loggers = [logging.getLogger(n) for n in names]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.ERROR)
    loguru_logger: Any = None
    try:
        from loguru import logger

        loguru_logger = logger
        loguru_logger.disable("mmm_framework")
    except ImportError:  # pragma: no cover - a dependency of mmm-framework
        pass
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter("ignore")
            yield
    finally:
        for lg, lvl in zip(loggers, levels, strict=True):
            lg.setLevel(lvl)
        if loguru_logger is not None:
            loguru_logger.enable("mmm_framework")


def _schema_error(message: str, pointer: str = "") -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def _labels(ds: xr.Dataset | xr.DataArray, dim: str) -> np.ndarray:
    if dim in ds.coords:
        return np.asarray(ds[dim].values)
    return np.arange(int(ds.sizes[dim]))


# ======================================================================== frequency


def infer_frequency(times: np.ndarray, explicit: str | None = None) -> str:
    """mmm-framework's ``W``/``D``/``M`` frequency: ``explicit``, else read from datetime
    labels (the median spacing). Integer labels need ``settings.frequency``."""
    if explicit is not None:
        return explicit
    arr = np.asarray(times)
    if arr.dtype.kind != "M":
        raise _schema_error(
            "the time labels are not dates: set model.settings.frequency (W, D or M)",
            "/model/settings/frequency",
        )
    if arr.size < 2:
        return "W"
    gaps = np.diff(arr.astype("datetime64[ns]")).astype("int64").astype(float)
    days = float(np.median(gaps)) / 86_400e9
    if 0.5 <= days < 2:
        return "D"
    if 5 <= days < 9:
        return "W"
    if 27 <= days < 32:
        return "M"
    raise _schema_error(
        f"cannot tell the data frequency from a {days:g}-day spacing: set "
        "model.settings.frequency (W, D or M) or resample the data",
        "/model/settings/frequency",
    )


def period_index(times: np.ndarray, frequency: str) -> pd.DatetimeIndex:
    """mmm-framework's ``Period`` index for our time labels (dates as they are; integer
    labels get a synthetic calendar at ``frequency`` - only spacing matters to the model)."""
    arr = np.asarray(times)
    if arr.dtype.kind == "M":
        return pd.DatetimeIndex(arr.astype("datetime64[ns]"), name="Period")
    return pd.date_range(
        _SYNTHETIC_START, periods=arr.size, freq=_SYNTHETIC_FREQ[frequency], name="Period"
    )


# ======================================================================== controls


@dataclass(frozen=True, slots=True)
class Controls:
    """Control variables flattened into features: ``features[i]`` belongs to the variable
    ``owner[i]``; ``variables`` keeps the variables' order."""

    variables: tuple[str, ...]
    features: tuple[str, ...]
    owner: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.features

    def block(self, variable: str) -> list[int]:
        return [i for i, o in enumerate(self.owner) if o == variable]

    def to_json(self) -> dict[str, list[str]]:
        return {
            "variables": list(self.variables),
            "features": list(self.features),
            "owner": list(self.owner),
        }

    @classmethod
    def from_json(cls, raw: Any) -> Controls:
        if not isinstance(raw, Mapping):
            return cls((), (), ())
        return cls(
            tuple(map(str, raw.get("variables") or ())),
            tuple(map(str, raw.get("features") or ())),
            tuple(map(str, raw.get("owner") or ())),
        )


def detect_controls(ds: xr.Dataset, explicit: Sequence[str] | None) -> list[str]:
    """Control variables: ``explicit`` (checked) or every other numeric variable over time."""
    if explicit is not None:
        missing = [v for v in explicit if v not in ds]
        if missing:
            raise MapiError(
                f"control variable(s) {missing} are not in the data",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer="/model/settings/controls",
            )
        for v in explicit:
            if TIME not in ds[v].dims:
                raise _schema_error(
                    f"control {v!r} has no {TIME!r} dim", "/model/settings/controls"
                )
        return list(explicit)
    out = []
    for name in sorted(map(str, ds.data_vars)):
        da = ds[name]
        if name in (TARGET, SPEND) or TIME not in da.dims or da.dtype.kind not in "fiub":
            continue
        if CHANNEL in da.dims:
            continue
        out.append(name)
    return out


def _control_columns(ds: xr.Dataset, name: str, panel: Sequence[str]) -> dict[str, xr.DataArray]:
    """One ``(time, *panel)``-able array per feature of control ``name``."""
    da = ds[name]
    if CHANNEL in da.dims:
        raise _schema_error(f"control {name!r} must not vary by channel")
    extra = [str(d) for d in da.dims if d not in (TIME, *panel)]
    if not extra:
        return {name: da}
    labels = [_labels(da, d) for d in extra]
    cols: dict[str, xr.DataArray] = {}
    for combo in np.ndindex(*[len(lab) for lab in labels]):
        sel = {d: int(i) for d, i in zip(extra, combo, strict=True)}
        parts = [f"{d}={labels[k][i]}" for k, (d, i) in enumerate(sel.items())]
        col = da.isel(sel)
        cols[f"{name}[{','.join(parts)}]"] = col.drop_vars(
            [c for c in col.coords if c not in (TIME, *panel)]
        )
    return {k: cols[k] for k in sorted(cols)}


# ======================================================================== panel data


@dataclass(slots=True)
class PanelData:
    """A model-stage panel as plain arrays (``time`` first, then ``geo``, ``product``).

    ``spend`` is ``(T, G, P, C)``, ``controls`` ``(T, G, P, K)`` and ``y`` ``(T, G, P)``
    (``G``/``P`` are 1 without a geo/product dim).
    """

    times: np.ndarray
    panel: tuple[str, ...]
    geos: list[str] | None
    products: list[str] | None
    channels: list[str]
    controls: Controls
    spend: np.ndarray
    ctrl: np.ndarray
    y: np.ndarray | None

    @property
    def n_periods(self) -> int:
        return int(self.times.size)

    @property
    def n_geos(self) -> int:
        return len(self.geos) if self.geos else 1

    @property
    def n_products(self) -> int:
        return len(self.products) if self.products else 1

    @property
    def n_cells(self) -> int:
        return self.n_geos * self.n_products

    # ------------------------------------------------------------------ from xarray
    @classmethod
    def from_dataset(
        cls,
        ds: xr.Dataset,
        panel: Sequence[str],
        controls: Sequence[str],
        *,
        with_target: bool,
        fill_missing: bool = False,
        control_features: Controls | None = None,
    ) -> PanelData:
        """Arrays from model-stage ``ds`` (checks names, dims and values).

        ``fill_missing`` (score time) replaces missing spend/controls with 0 - only cells
        the platform padded (``<dim>__requested`` false) may be missing; a missing value
        in a requested cell is ``MISSING_FUTURE_COVARIATE``. ``control_features`` pins the
        fit's feature list (score time).
        """
        panel = tuple(panel)
        for d in panel:
            if d not in PANEL_DIMS:
                raise _schema_error(f"unknown panel dim {d!r}", "/model/settings/hierarchy")
        if TIME not in ds.dims:
            raise _schema_error(f"the data has no {TIME!r} dim (bind your date column to it)")
        if SPEND not in ds:
            raise MapiError(
                "the model needs the variable 'spend' (bind your spend variable to it)",
                code=ErrorCode.MISSING_FUTURE_COVARIATE
                if fill_missing
                else ErrorCode.SCHEMA_INVALID,
                pointer="/data/spend",
            )
        want = {*panel, CHANNEL, TIME}
        if set(map(str, ds[SPEND].dims)) != want:
            raise _schema_error(
                f"'spend' must have dims {sorted(want)}, got {sorted(map(str, ds[SPEND].dims))} "
                "(hierarchy.pool_over lists the panel dims)",
                "/data/spend",
            )
        geo_dim = GEO if GEO in panel else None
        prod_dim = PRODUCT if PRODUCT in panel else None
        order = [TIME, *(d for d in (geo_dim, prod_dim) if d)]
        times = _labels(ds, TIME)
        if times.size > 1 and not bool(np.all(times[1:] > times[:-1])):
            raise _schema_error(f"the {TIME!r} labels must be strictly increasing")
        geos = [str(g) for g in _labels(ds, GEO)] if geo_dim else None
        products = [str(p) for p in _labels(ds, PRODUCT)] if prod_dim else None
        channels = [str(c) for c in _labels(ds, CHANNEL)]
        if len(set(channels)) != len(channels):
            raise _schema_error("channel labels must be unique", "/data/spend")
        clash = sorted(set(channels) & {*RESERVED_COMPONENTS, *controls})
        if clash:
            raise _schema_error(
                f"channel labels {clash} clash with control names or the reserved component "
                f"names {list(RESERVED_COMPONENTS)}",
                "/data/spend",
            )
        clash = sorted(set(controls) & set(RESERVED_COMPONENTS))
        if clash:
            raise _schema_error(f"controls may not be named {clash}", "/model/settings/controls")
        requested = _requested_mask(ds, order)
        spend = np.asarray(ds[SPEND].transpose(*order, CHANNEL).values, dtype=np.float64).reshape(
            times.size, len(geos or [0]), len(products or [0]), len(channels)
        )
        spend = _finite("spend", spend, requested, fill_missing, "/data/spend")
        if (spend < 0).any():
            raise _schema_error("'spend' must be non-negative", "/data/spend")
        feats: list[str] = []
        owner: list[str] = []
        cols: list[np.ndarray] = []
        frame = xr.DataArray(
            np.zeros([int(ds.sizes[d]) for d in order]),
            dims=order,
            coords={d: ds[d].values for d in order if d in ds.coords},
        )
        for name in controls:
            if name not in ds:
                raise MapiError(
                    f"control variable {name!r} is not in the data",
                    code=ErrorCode.MISSING_FUTURE_COVARIATE,
                    pointer="/model/settings/controls",
                )
            for label, col in _control_columns(ds, name, panel).items():
                feats.append(label)
                owner.append(name)
                arr = np.asarray(
                    col.broadcast_like(frame).transpose(*order).values, dtype=np.float64
                )
                cols.append(arr.reshape(times.size, len(geos or [0]), len(products or [0])))
        if control_features is not None and tuple(feats) != control_features.features:
            missing = sorted(set(control_features.features) - set(feats))
            raise MapiError(
                f"the control features differ from the fit's (missing: {missing[:5]})",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer="/model/settings/controls",
            )
        ctrl = (
            np.stack(cols, axis=-1)
            if cols
            else np.zeros((times.size, len(geos or [0]), len(products or [0]), 0))
        )
        ctrl = _finite("control variables", ctrl, requested, fill_missing, "/data")
        y = None
        if with_target:
            if TARGET not in ds:
                raise _schema_error(
                    "the model needs the target 'y' (bind your KPI to it)", "/data/y"
                )
            want_y = {*panel, TIME}
            if set(map(str, ds[TARGET].dims)) != want_y:
                raise _schema_error(
                    f"'y' must have dims {sorted(want_y)}, got {sorted(map(str, ds[TARGET].dims))}",
                    "/data/y",
                )
            y = np.asarray(ds[TARGET].transpose(*order).values, dtype=np.float64).reshape(
                times.size, len(geos or [0]), len(products or [0])
            )
            bad = ~np.isfinite(y)
            if bad.any():
                raise _schema_error(
                    f"the target has {int(bad.sum())} missing value(s); impute or filter them "
                    "in the pipeline",
                    "/data/y",
                )
        return cls(
            times=times,
            panel=panel,
            geos=geos,
            products=products,
            channels=channels,
            controls=Controls(tuple(controls), tuple(feats), tuple(owner)),
            spend=spend,
            ctrl=ctrl,
            y=y,
        )

    # ------------------------------------------------------------------ bundle group
    def to_dataset(self) -> xr.Dataset:
        """The ``fit_data`` bundle group (float64 arrays, string labels)."""
        dims = [TIME, GEO, PRODUCT]
        coords: dict[str, Any] = {
            TIME: self.times,
            GEO: np.asarray(self.geos or ["__all__"], dtype=str),
            PRODUCT: np.asarray(self.products or ["__all__"], dtype=str),
            CHANNEL: np.asarray(self.channels, dtype=str),
            "control_feature": np.asarray(self.controls.features, dtype=str),
        }
        out = xr.Dataset(
            {
                SPEND: ([*dims, CHANNEL], self.spend),
                "controls": ([*dims, "control_feature"], self.ctrl),
            },
            coords=coords,
        )
        if self.y is not None:
            out[TARGET] = (dims, self.y)
        out.attrs = {"panel": ",".join(self.panel)}
        return out

    @classmethod
    def from_fit_data(cls, ds: xr.Dataset, controls: Controls) -> PanelData:
        """The inverse of :meth:`to_dataset`."""
        panel = tuple(p for p in str(ds.attrs.get("panel", "")).split(",") if p)
        geos = [str(g) for g in ds[GEO].values] if GEO in panel else None
        products = [str(p) for p in ds[PRODUCT].values] if PRODUCT in panel else None
        return cls(
            times=np.asarray(ds[TIME].values),
            panel=panel,
            geos=geos,
            products=products,
            channels=[str(c) for c in ds[CHANNEL].values],
            controls=controls,
            spend=np.asarray(ds[SPEND].transpose(TIME, GEO, PRODUCT, CHANNEL).values, dtype=float),
            ctrl=np.asarray(
                ds["controls"].transpose(TIME, GEO, PRODUCT, "control_feature").values, dtype=float
            ),
            y=(
                np.asarray(ds[TARGET].transpose(TIME, GEO, PRODUCT).values, dtype=float)
                if TARGET in ds
                else None
            ),
        )

    # ------------------------------------------------------------------ mmm-framework
    def flat(self, arr: np.ndarray) -> np.ndarray:
        """``(T, G, P, ...)`` to mmm-framework's observation axis (period-major)."""
        return arr.reshape(self.n_periods * self.n_cells, *arr.shape[3:])

    def to_panel(self, mff: MFFConfig, frequency: str) -> PanelDataset:
        """mmm-framework's :class:`PanelDataset` (period-major MultiIndex, like ``MFFLoader``)."""
        from mmm_framework.data_loader import PanelCoordinates, PanelDataset

        if self.y is None:
            raise _schema_error("the fit data has no target")
        periods = period_index(self.times, frequency)
        cols = mff.columns
        levels: list[Any] = [periods]
        names = [cols.period]
        if self.geos:
            levels.append(self.geos)
            names.append(cols.geography)
        if self.products:
            levels.append(self.products)
            names.append(cols.product)
        index: pd.MultiIndex | pd.DatetimeIndex
        if len(levels) > 1:
            index = pd.MultiIndex.from_product(levels, names=names)
        else:
            index = pd.DatetimeIndex(periods, name=cols.period)
        y = pd.Series(self.flat(self.y), index=index, name=TARGET)
        x_media = pd.DataFrame(self.flat(self.spend), index=index, columns=self.channels)
        x_ctrl = (
            pd.DataFrame(self.flat(self.ctrl), index=index, columns=list(self.controls.features))
            if not self.controls.empty
            else None
        )
        coords = PanelCoordinates(
            periods=periods,
            geographies=self.geos,
            products=self.products,
            channels=list(self.channels),
            controls=list(self.controls.features),
        )
        return PanelDataset(
            y=y,
            X_media=x_media,
            X_controls=x_ctrl,
            coords=coords,
            index=index,
            config=mff,
            media_stats={},
        )


def _requested_mask(ds: xr.Dataset, order: Sequence[str]) -> np.ndarray:
    """``(T, G, P)`` bool: cells a score request asked for (all True outside scoring)."""
    axes = {TIME: 0, GEO: 1, PRODUCT: 2}
    shape = [1, 1, 1]
    for d in order:
        shape[axes[d]] = int(ds.sizes[d])
    mask = np.ones(shape, dtype=bool)
    for d in order:
        flag = f"{d}__requested"
        if flag in ds.coords:
            vals = np.asarray(ds[flag].values, dtype=bool)
            view = [1, 1, 1]
            view[axes[d]] = vals.size
            mask = mask & vals.reshape(view)
    return mask


def _finite(
    what: str, arr: np.ndarray, requested: np.ndarray, fill: bool, pointer: str
) -> np.ndarray:
    bad = ~np.isfinite(arr)
    if not bad.any():
        return arr
    if fill:
        req = np.broadcast_to(requested.reshape(*requested.shape, 1), arr.shape)
        if (bad & req).any():
            raise MapiError(
                f"{what} has {int((bad & req).sum())} missing value(s) in the scored cells; "
                "supply them (or set future_covariates for a forecast)",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer=pointer,
            )
        return np.where(bad, 0.0, arr)
    raise _schema_error(
        f"{what} has {int(bad.sum())} missing or infinite value(s); impute them in the pipeline",
        pointer,
    )


# ======================================================================== configs


def _adstock_config(a: AdstockSettings) -> Any:
    from mmm_framework.config import AdstockConfig

    if a.kind == "geometric":
        cfg = AdstockConfig.geometric(l_max=a.l_max)
    elif a.kind == "delayed":
        cfg = AdstockConfig.delayed(l_max=a.l_max)
    elif a.kind == "weibull":
        cfg = AdstockConfig.weibull(l_max=a.l_max)
    else:
        cfg = AdstockConfig.none()
    return cfg.model_copy(update={"normalize": a.normalize}) if a.kind != "none" else cfg


def _saturation_config(s: SaturationSettings) -> Any:
    from mmm_framework.config import SaturationConfig

    factories: dict[str, Any] = {
        "logistic": SaturationConfig.logistic,
        "hill": SaturationConfig.hill,
        "michaelis_menten": SaturationConfig.michaelis_menten,
        "tanh": SaturationConfig.tanh,
        "root": SaturationConfig.root,
        "none": SaturationConfig.none,
    }
    return factories[s.kind]()


@dataclass(frozen=True, slots=True)
class EnginePlan:
    """How to call ``BayesianMMM.fit``: the fit method and NUTS backend."""

    engine: str
    method: str
    nuts_sampler: str | None
    draws: int
    tune: int
    chains: int
    target_accept: float | None
    seed: int

    @property
    def mcmc(self) -> bool:
        return self.method in ("nuts", "smc")


def engine_plan(
    engine: str | None,
    *,
    draws: int | None,
    tune: int | None,
    chains: int | None,
    target_accept: float | None,
    seed: int,
    default_engine: str,
) -> EnginePlan:
    """``fit.engine`` to mmm-framework's method/sampler (``CAPABILITY_UNSUPPORTED`` else)."""
    name = engine or default_engine
    if name in MCMC_ENGINES:
        method, sampler = "nuts", name
    elif name in APPROX_ENGINES:
        method, sampler = APPROX_ENGINES[name], None
    else:
        raise MapiError(
            f"engine {name!r} is not supported by mmm-framework (use one of "
            f"{[*MCMC_ENGINES, *APPROX_ENGINES]})",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/model/fit/engine",
        )
    return EnginePlan(
        engine=name,
        method=method,
        nuts_sampler=sampler,
        draws=int(draws or 1000),
        tune=int(tune if tune is not None else 1000),
        chains=int(chains or 4),
        target_accept=target_accept,
        seed=int(seed),
    )


def build_configs(
    data: PanelData,
    s: BayesianSettings | FrequentistSettings,
    *,
    frequency: str,
    plan: EnginePlan | None = None,
) -> tuple[MFFConfig, ModelConfig, TrendConfig]:
    """The ``MFFConfig``, ``ModelConfig`` and ``TrendConfig`` our settings describe."""
    from mmm_framework.config import (
        ControlVariableConfig,
        DimensionType,
        HierarchicalConfig,
        InferenceMethod,
        KPIConfig,
        MediaChannelConfig,
        MFFConfig,
        ModelConfig,
        SeasonalityConfig,
    )
    from mmm_framework.config.enums import LikelihoodFamily
    from mmm_framework.config.likelihood import LikelihoodConfig
    from mmm_framework.model.trend_config import TrendConfig, TrendType

    dims = [DimensionType.PERIOD]
    if data.geos:
        dims.append(DimensionType.GEOGRAPHY)
    if data.products:
        dims.append(DimensionType.PRODUCT)
    bayes = isinstance(s, BayesianSettings)
    unknown = sorted(set(s.channels) - set(data.channels))
    if unknown:
        raise _schema_error(
            f"settings.channels names unknown channels {unknown}", "/model/settings/channels"
        )
    media = []
    for ch in data.channels:
        extra: dict[str, Any] = {}
        if bayes:
            assert isinstance(s, BayesianSettings)
            roi = s.priors.roi.get(ch)
            if roi is not None:
                extra = {"roi_prior_mu": float(roi.mu), "roi_prior_sigma": float(roi.sigma)}
        media.append(
            MediaChannelConfig(
                name=ch,
                dimensions=list(dims),
                adstock=_adstock_config(s.adstock_for(ch)),
                saturation=_saturation_config(s.saturation_for(ch)),
                **extra,
            )
        )
    if bayes:
        assert isinstance(s, BayesianSettings)
        unknown = sorted(set(s.priors.roi) - set(data.channels))
        if unknown:
            raise MapiError(
                f"priors.roi names channels the data does not have: {unknown}",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings/priors/roi",
            )
    controls = [
        ControlVariableConfig(name=f, dimensions=list(dims)) for f in data.controls.features
    ]
    mff = MFFConfig(
        kpi=KPIConfig(name=TARGET, dimensions=list(dims)),
        media_channels=media,
        controls=controls,
        frequency=frequency,
    )
    seas = s.seasonality
    model_kwargs: dict[str, Any] = {
        "hierarchical": HierarchicalConfig(
            enabled=bool(data.geos or data.products),
            pool_across_geo=bool(data.geos),
            pool_across_product=bool(data.products),
            vary_media_by_geo=bool(s.hierarchy.media_by_geo and data.geos),
            media_geo_sigma=float(s.hierarchy.media_geo_sigma),
        ),
        "seasonality": SeasonalityConfig(
            yearly=seas.yearly_order or None,
            monthly=seas.monthly_order or None,
            weekly=seas.weekly_order or None,
            prior_sigma=float(seas.prior_sigma),
        ),
    }
    if bayes:
        assert isinstance(s, BayesianSettings)
        sampler = (plan.nuts_sampler if plan is not None else None) or "pymc"
        model_kwargs["inference_method"] = {
            "pymc": InferenceMethod.BAYESIAN_PYMC,
            "nutpie": InferenceMethod.BAYESIAN_NUTPIE,
            "numpyro": InferenceMethod.BAYESIAN_NUMPYRO,
        }[sampler]
        model_kwargs["media_prior_mode"] = s.media_prior
        if s.priors.roi_default is not None:
            model_kwargs["media_roi_prior_mu"] = float(s.priors.roi_default.mu)
            model_kwargs["media_roi_prior_sigma"] = float(s.priors.roi_default.sigma)
        if s.priors.intercept is not None:
            model_kwargs["intercept_prior_mu"] = float(s.priors.intercept.mu)
            model_kwargs["intercept_prior_sigma"] = float(s.priors.intercept.sigma)
        if s.likelihood == "student_t":
            model_kwargs["likelihood"] = LikelihoodConfig(family=LikelihoodFamily.STUDENT_T)
        if plan is not None:
            model_kwargs.update(
                n_chains=plan.chains,
                n_draws=plan.draws,
                n_tune=plan.tune,
                optim_seed=plan.seed,
            )
            if plan.target_accept is not None:
                model_kwargs["target_accept"] = float(plan.target_accept)
    else:
        assert isinstance(s, FrequentistSettings)
        model_kwargs["inference_method"] = (
            InferenceMethod.FREQUENTIST_RIDGE
            if s.estimator == "ridge"
            else InferenceMethod.FREQUENTIST_CVXPY
        )
        model_kwargs["bootstrap_samples"] = int(s.bootstrap)
        model_kwargs["optim_maxiter"] = int(s.search_budget)
        model_kwargs["ridge_alpha"] = float(s.ridge_alpha)
        if plan is not None:
            model_kwargs["optim_seed"] = plan.seed
    model = ModelConfig(**model_kwargs)
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
    return mff, model, trend


def make_model(
    data: PanelData,
    s: BayesianSettings | FrequentistSettings,
    *,
    frequency: str,
    plan: EnginePlan | None = None,
) -> BayesianMMM:
    """A (not yet fitted) mmm-framework ``BayesianMMM`` over ``data``."""
    from mmm_framework.model import BayesianMMM

    mff, model_cfg, trend = build_configs(data, s, frequency=frequency, plan=plan)
    panel = data.to_panel(mff, frequency)
    with quiet():
        try:
            return BayesianMMM(panel, model_cfg, trend)
        except (ValueError, NotImplementedError) as exc:
            raise MapiError(
                "mmm-framework refused the model configuration",
                code=ErrorCode.SETTINGS_INVALID,
                pointer="/model/settings",
                untrusted_text=f"{type(exc).__name__}: {exc}"[:4000],
            ) from exc


def rebuild_model(
    fit_data: xr.Dataset,
    s: BayesianSettings | FrequentistSettings,
    *,
    controls: Controls,
    frequency: str,
) -> tuple[BayesianMMM, PanelData]:
    """The fitted model's ``BayesianMMM`` rebuilt from its ``fit_data`` group: the same
    data gives the same scaling, Fourier features and trend basis as at fit."""
    data = PanelData.from_fit_data(fit_data, controls)
    return make_model(data, s, frequency=frequency), data


def finite_or_none(x: float) -> float | None:
    return x if math.isfinite(x) else None
