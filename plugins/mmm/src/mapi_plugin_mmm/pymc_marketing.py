"""``mmm.pymc_marketing``: an adapter around ``pymc_marketing.mmm.MMM`` (plan §4.3, M0).

pymc-marketing does its own preprocessing: it scales channels and the target, carries
spend over with ``l_max`` past observations and reports contributions in original units.
The adapter therefore declares ``owns: [scaling, carryover, inverse_units]`` and the
platform skips its own versions:

* **scaling** - the spec's pipeline must not scale spend or the target (fx conversion and
  resampling are fine); pymc-marketing scales them from the fit data;
* **carryover** - at score time the adapter prepends the last ``l_max`` fit periods before
  the first scored period (whatever the window), so adstock sees the true history - the
  role the platform's ``lag_tensor`` context plays for ``mmm.hierarchical_geo``;
* **inverse_units** - outputs are multiplied back by pymc-marketing's target scale; the
  platform labels them ``model-owned`` and does not invert them again.

Canonical names: dims ``time`` and ``channel`` plus the panel ``dims`` from the settings;
target ``y(*dims, time)``; spend ``spend(*dims, channel, time)``; controls
``<name>(*dims, time)``.

Persistence never pickles: the bundle stores the posterior, pymc-marketing's ``fit_data``
group and its JSON ``idata`` attributes; scoring rebuilds the model with
``MMM.attrs_to_init_kwargs`` + ``build_from_idata`` (pymc-marketing's own load path).

``pymc_marketing`` is imported lazily, so describing the plugin (manifest, catalog) does
not pay its import cost.
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import xarray as xr
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_sdk.models import PREDICTIONS, CustomModel, FitContext, FitOutput, Fitted, ScoreContext
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest

from mapi_plugin_mmm.settings import PyMCMarketingSettings, mcmc_fit_schema

__all__ = ["SCHEMA_TEMPLATE", "PyMCMarketingMMM", "pymc_marketing_available"]

TIME = "time"
CHANNEL = "channel"
TARGET = "y"
SPEND = "spend"
DATE = "date"  # pymc-marketing's date column
BASELINE = "baseline"
SEASONALITY = "seasonality"
_ENGINES = {"nutpie": "nutpie", "pymc": "pymc"}

SCHEMA_TEMPLATE: dict[str, Any] = {
    "params": {"dims": {"type": "string_list", "default": []}},
    "template": {
        "dims": {
            TIME: {"kind": "time", "policy": "extend"},
            CHANNEL: {"kind": "categorical", "policy": "fixed"},
            "{*dims as d}": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            TARGET: {"dims": ["{*dims}", TIME], "dtype": "float64", "role": "target"},
            SPEND: {"dims": ["{*dims}", CHANNEL, TIME], "dtype": "float64", "role": "spend"},
        },
    },
}


def pymc_marketing_available() -> bool:
    """Is ``pymc_marketing`` importable in this environment?"""
    try:
        import pymc_marketing  # noqa: F401
    except ImportError:  # pragma: no cover - a hard dependency of this package
        return False
    return True


@contextmanager
def _quiet() -> Iterator[None]:
    loggers = [logging.getLogger(n) for n in ("pymc", "pymc_marketing", "pytensor")]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        for lg, lvl in zip(loggers, levels, strict=True):
            lg.setLevel(lvl)


def _seed(seed: int, name: str) -> int:
    digest = hashlib.sha256(f"{seed}:{name}".encode()).digest()
    return int.from_bytes(digest[:4], "little")


def _schema_error(message: str, pointer: str = "") -> MapiError:
    return MapiError(message, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def _check_names(s: PyMCMarketingSettings, channels: list[str]) -> None:
    taken = {DATE, TARGET, *s.dims, *s.controls}
    clash = sorted(taken & set(channels))
    if clash:
        raise _schema_error(
            f"channel labels {clash} clash with column names used by pymc-marketing "
            f"({sorted(taken)})",
            "/data/spend",
        )


def _frame(
    ds: xr.Dataset, s: PyMCMarketingSettings, *, with_target: bool
) -> tuple[pd.DataFrame, list[str]]:
    """``ds`` as pymc-marketing's long frame: ``date, *dims, <channel columns>, controls``."""
    if SPEND not in ds:
        raise MapiError(
            "the model needs the variable 'spend' (bind your spend variable to it)",
            code=ErrorCode.MISSING_FUTURE_COVARIATE,
            pointer="/data/spend",
        )
    want = {*s.dims, CHANNEL, TIME}
    if set(map(str, ds[SPEND].dims)) != want:
        raise _schema_error(
            f"'spend' must have dims {sorted(want)}, got {list(map(str, ds[SPEND].dims))}",
            "/data/spend",
        )
    channels = [str(c) for c in ds[CHANNEL].values]
    _check_names(s, channels)
    base = ds.drop_vars([c for c in ds.coords if c not in ds.dims])
    parts = [base[SPEND].assign_coords({CHANNEL: channels}).to_dataset(dim=CHANNEL)]
    for name in s.controls:
        if name not in base:
            raise MapiError(
                f"control variable {name!r} is not in the data",
                code=ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer="/model/settings/controls",
            )
        parts.append(base[[name]])
    if with_target:
        parts.append(base[[TARGET]])
    merged = xr.merge(parts, join="exact", compat="override")
    frame = merged.to_dataframe().reset_index()
    frame = frame.rename(columns={TIME: DATE})
    order = [DATE, *s.dims, *channels, *s.controls] + ([TARGET] if with_target else [])
    frame = frame[order].sort_values([DATE, *s.dims]).reset_index(drop=True)
    values = frame[[*channels, *s.controls]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise MapiError(
            f"spend/controls have {int((~np.isfinite(values)).sum())} missing value(s)",
            code=ErrorCode.MISSING_FUTURE_COVARIATE
            if not with_target
            else ErrorCode.SCHEMA_INVALID,
            pointer="/data/spend",
        )
    return frame, channels


def _make_mmm(s: PyMCMarketingSettings, channels: list[str]) -> Any:
    from pymc_marketing.mmm import (
        MMM,
        GeometricAdstock,
        HillSaturation,
        LogisticSaturation,
        NoSaturation,
    )

    saturation = {
        "logistic": LogisticSaturation,
        "hill": HillSaturation,
        "none": NoSaturation,
    }[s.saturation.kind]()
    assert s.adstock.l_max is not None
    return MMM(
        date_column=DATE,
        channel_columns=channels,
        target_column=TARGET,
        dims=tuple(s.dims) or None,
        adstock=GeometricAdstock(l_max=s.adstock.l_max, normalize=s.adstock.normalize),
        saturation=saturation,
        control_columns=list(s.controls) or None,
        yearly_seasonality=s.yearly_seasonality,
        adstock_first=s.adstock_first,
    )


def _rebuild(fitted: Fitted) -> Any:
    """pymc-marketing's load path, from JSON attributes and the stored groups."""
    from pymc_marketing.mmm import MMM

    info = fitted.learned.get("pymc_marketing")
    raw = info.get("attrs") if isinstance(info, dict) else None
    if not isinstance(raw, dict):
        raise MapiError(
            "the bundle has no pymc-marketing model description", code=ErrorCode.INTEGRITY_FAILURE
        )
    attrs = {str(k): str(v) for k, v in raw.items()}
    tree = xr.DataTree.from_dict(
        {"posterior": fitted.posterior, "fit_data": fitted.group("fit_data")}
    )
    tree.attrs = dict(attrs)
    with _quiet():
        mmm = MMM(**MMM.attrs_to_init_kwargs(attrs))
        mmm.build_from_idata(tree)
    return mmm


class PyMCMarketingMMM(CustomModel[PyMCMarketingSettings]):
    """Adapter running ``pymc_marketing.mmm.MMM`` under the platform contract."""

    id = "mmm.pymc_marketing"
    version = "1.0.0"
    Settings = PyMCMarketingSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate"}),
        contribution_scale="additive",
        owns=frozenset({"scaling", "carryover", "inverse_units"}),
        deterministic_given_seed=False,
        checkpointable=False,
    )
    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        # the panel dims (settings ``dims``) come in front of ``time``
        "y_hat": {
            "dims": [TIME],
            "units_of": TARGET,
            "description": "Expected (or predictive) target in original units, over the "
            "panel dims and time",
        },
        "contributions": {
            "dims": ["component", TIME],
            "units_of": TARGET,
            "description": "Channels, baseline, controls and seasonality in original units; "
            "they sum to y_hat",
        },
    }
    plots: ClassVar[list[str]] = ["core.ppc_fan", "core.convergence", "core.counterfactual"]
    resources = ResourceHint(
        min_class=ResourceClass.S, default_class=ResourceClass.M, est_fit_seconds=900
    )
    fit_schema: ClassVar[dict[str, Any]] = mcmc_fit_schema(
        engines=sorted(_ENGINES), default_engine="nutpie"
    )
    description = (
        "pymc-marketing's MMM (geometric adstock, logistic/Hill saturation, controls and "
        "yearly seasonality) behind the platform contract; it owns scaling, carry-over and "
        "the inverse to original units."
    )
    pad_extend = False

    # ------------------------------------------------------------------ fit
    def fit(
        self, ds: xr.Dataset, s: PyMCMarketingSettings, cfg: FitConfig, ctx: FitContext
    ) -> FitOutput:
        frame, channels = _frame(ds, s, with_target=True)
        engine = cfg.engine or "nutpie"
        if engine not in _ENGINES:
            raise MapiError(
                f"engine {engine!r} is not supported by the pymc-marketing adapter "
                f"(use one of {sorted(_ENGINES)})",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer="/model/fit/engine",
            )
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        mmm = _make_mmm(s, channels)
        kwargs: dict[str, Any] = {
            "draws": cfg.draws or 1000,
            "tune": cfg.tune if cfg.tune is not None else 1000,
            "chains": cfg.chains or 4,
            "nuts_sampler": _ENGINES[engine],
            "random_seed": seed,
            "progressbar": False,
        }
        if cfg.target_accept is not None:
            kwargs["target_accept"] = cfg.target_accept
        ctx.progress(0.05, "building the pymc-marketing model")
        ctx.check_cancelled()
        try:
            with _quiet():
                idata = mmm.fit(frame.drop(columns=[TARGET]), frame[TARGET], **kwargs)
        except MapiError:
            raise
        except Exception as exc:
            raise MapiError(
                "pymc-marketing failed to fit",
                code=ErrorCode.USER_CODE_ERROR,
                untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
            ) from exc
        ctx.check_cancelled()
        ctx.progress(0.9, "posterior ready")
        groups = self._groups(idata)
        free = [rv.name for rv in mmm.model.free_RVs]
        diagnostics: list[Diagnostic] = convergence_diagnostics(
            groups["posterior"], groups.get("sample_stats"), var_names=free
        )
        attrs = {str(k): str(v) for k, v in mmm.create_idata_attrs().items()}
        import pymc_marketing

        learned: dict[str, Any] = {
            "engine": engine,
            "channels": channels,
            "pymc_marketing": {"version": pymc_marketing.__version__, "attrs": attrs},
        }
        ctx.progress(1.0, "fit complete")
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    @staticmethod
    def _groups(idata: xr.DataTree) -> dict[str, xr.Dataset]:
        def node(name: str) -> xr.Dataset | None:
            if name not in idata.children:
                return None
            child = idata[name]
            assert isinstance(child, xr.DataTree)
            return child.to_dataset(inherit=False)

        post = node("posterior")
        if post is None:
            raise MapiError("pymc-marketing returned no posterior", code=ErrorCode.INTERNAL_ERROR)
        # date-indexed deterministics are recomputed at score time
        heavy = [str(v) for v in post.data_vars if DATE in post[v].dims]
        groups = {"posterior": _clean(post.drop_vars(heavy))}
        for name in ("sample_stats", "observed_data", "fit_data"):
            ds = node(name)
            if ds is not None:
                if name == "sample_stats":
                    ds = ds.drop_vars(
                        [v for v in ds.data_vars if ds[v].dtype.kind not in "biuf"], errors="ignore"
                    )
                groups[name] = _clean(ds)
        if "fit_data" not in groups:
            raise MapiError(
                "pymc-marketing returned no fit_data group", code=ErrorCode.INTERNAL_ERROR
            )
        return groups

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        frame, channels = _frame(ds, s, with_target=False)
        fit_channels = fitted.learned.get("channels")
        if isinstance(fit_channels, list) and sorted(map(str, fit_channels)) != sorted(channels):
            raise MapiError(
                "the score data's channels differ from the fit",
                code=ErrorCode.UNSEEN_LEVELS,
                pointer="/data/spend",
            )
        mmm = _rebuild(fitted)
        x_full = self._with_history(mmm, frame, s)
        predictive = ctx.kind == "predictive"
        names = ["mu", "channel_contribution"]
        if s.controls:
            names.append("control_contribution")
        if s.yearly_seasonality:
            names.append("yearly_seasonality_contribution")
        if predictive:
            names.append(TARGET)
        ctx.check_cancelled()
        with _quiet():
            pp = mmm.sample_posterior_predictive(
                x_full,
                extend_idata=False,
                combined=False,
                include_last_observations=False,
                var_names=names,
                random_seed=_seed(ctx.seed, "pymc_marketing_ppc"),
                progressbar=False,
            )
        dates = pd.DatetimeIndex(frame[DATE].unique()).sort_values()
        pp = pp.assign_coords({DATE: pd.DatetimeIndex(pp[DATE].values).as_unit("ns")})
        pp = pp.sel({DATE: dates.as_unit("ns").values})
        scale = mmm.get_scales_as_xarray()["target_scale"]
        y_var = TARGET if predictive else "mu"
        y_hat = self._to_output(pp[y_var] * scale, ds, s)
        y_hat.attrs = {"kind": "predictive" if predictive else "expected"}
        y_hat.attrs["noise"] = "seeded_stream" if predictive else "none"
        contributions = self._contributions(pp, fitted, scale, channels, s, ds)
        pred = xr.Dataset({"y_hat": y_hat, "contributions": contributions})
        pred.attrs = {"units": "original", "owner": "pymc-marketing"}
        return xr.DataTree.from_dict({PREDICTIONS: pred})

    def _with_history(
        self, mmm: Any, frame: pd.DataFrame, s: PyMCMarketingSettings
    ) -> pd.DataFrame:
        """Prepend the ``l_max`` fit periods before the first scored period (carry-over)."""
        fit = mmm.xarray_dataset
        fit_dates = pd.DatetimeIndex(fit[DATE].values).as_unit("ns")
        first = pd.Timestamp(frame[DATE].min())
        before = fit_dates[fit_dates < first]
        assert s.adstock.l_max is not None
        keep = before[-s.adstock.l_max :] if before.size else before
        if not keep.size:
            return frame
        hist = mmm.idata["fit_data"].to_dataset()
        past = hist.to_dataframe().reset_index()
        past[DATE] = pd.to_datetime(past[DATE]).dt.as_unit("ns")
        past = past[past[DATE].isin(keep)]
        past = past[[c for c in frame.columns]]
        out = pd.concat([past, frame], ignore_index=True)
        return out.sort_values([DATE, *s.dims]).reset_index(drop=True)

    @staticmethod
    def _to_output(da: xr.DataArray, ds: xr.Dataset, s: PyMCMarketingSettings) -> xr.DataArray:
        """pymc-marketing ``(chain, draw, date, *dims)`` -> ``(chain, draw, *dims, time)``."""
        out = da.rename({DATE: TIME}).assign_coords({TIME: ds[TIME].values})
        for d in s.dims:
            out = out.sel({d: ds[d].values})
        extra = [str(x) for x in out.dims if x not in ("chain", "draw", *s.dims, TIME)]
        out = out.transpose("chain", "draw", *extra, *s.dims, TIME)
        drop = [c for c in out.coords if c not in out.dims]
        return out.drop_vars(drop)

    def _contributions(
        self,
        pp: xr.Dataset,
        fitted: Fitted,
        scale: xr.DataArray,
        channels: list[str],
        s: PyMCMarketingSettings,
        ds: xr.Dataset,
    ) -> xr.DataArray:
        parts: list[xr.DataArray] = []
        ch = (pp["channel_contribution"] * scale).rename({CHANNEL: "component"})
        parts.append(ch.assign_coords(component=[str(c) for c in ch["component"].values]))
        # the intercept is a free parameter: take it from the posterior
        intercept = fitted.posterior["intercept_contribution"]
        base = (intercept * scale).broadcast_like(pp["mu"]).expand_dims(component=[BASELINE])
        parts.append(base)
        if s.controls:
            ctrl = (pp["control_contribution"] * scale).rename({"control": "component"})
            parts.append(ctrl.assign_coords(component=[str(c) for c in ctrl["component"].values]))
        if s.yearly_seasonality:
            season = pp["yearly_seasonality_contribution"] * scale
            parts.append(season.expand_dims(component=[SEASONALITY]))
        labels = [str(c) for p in parts for c in p["component"].values]
        if len(set(labels)) != len(labels):
            raise _schema_error(f"channel/control names clash with {BASELINE!r}/{SEASONALITY!r}")
        aligned = [p.transpose("chain", "draw", "component", ...) for p in parts]
        stacked = xr.concat(aligned, dim="component", join="override", coords="minimal")
        stacked = stacked.assign_coords(component=np.asarray(labels, dtype=str))
        out = self._to_output(stacked, ds, s)
        out.attrs = {"kind": "expected", "noise": "none"}
        return out


def _clean(ds: xr.Dataset) -> xr.Dataset:
    """Bundle-safe group: no object arrays, no volatile attributes."""
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
    out.attrs = {"inference_library": "pymc-marketing"}
    return out
