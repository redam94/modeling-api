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

Lift tests (M3, ``calibration.lift_tests``) go through pymc-marketing's own
``add_lift_test_measurements``: each test becomes one row per period of its window - the
channel's average spend per period ``x``, the extra spend per period ``delta_x`` and the
lift per period ``delta_y`` with its ``sigma`` - conditioned on the saturation curve
(pymc-marketing's model of a lift; carry-over is not part of it). With panel ``dims`` a test
must name its ``geo`` (the first panel dim).

Business priors (M3, ``calibration.business_priors``) become pymc-marketing ``model_config``
priors in ``settings.priors``: a per-channel log-normal ``saturation_beta`` matching each
ROI or contribution-share range, a Normal ``intercept`` for the baseline share (with the
control and seasonality priors narrowed when they alone are wider than the range) - solved
on prior draws of pymc-marketing's own model, so its scaling and transforms are exactly
the fit's (:mod:`mapi_plugin_mmm.pymc_marketing_calibration`). A ``translate_priors`` run
previews them in business units; a fit without one translates them itself.

``pymc_marketing`` is imported lazily, so describing the plugin (manifest, catalog) does
not pay its import cost.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import logging
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import xarray as xr
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    PriorTranslation,
    ScoreContext,
)
from mapi_spec.calibration import BusinessPriors, LiftTest, PriorTranslationJob
from mapi_spec.capabilities import Capabilities, Diagnostic, Representation, ResourceHint
from mapi_spec.errors import ErrorCode, MapiError, Severity
from mapi_spec.resources import ResourceClass
from mapi_spec.spec import FitConfig, ScoreRequest

from mapi_plugin_mmm.calibration import TranslationOutcome, lift_window_cut
from mapi_plugin_mmm.pymc_marketing_calibration import (
    BETA_PARAM,
    PmmPriorDraws,
    pmm_prior_draws,
    pmm_prior_preview,
    translate_pmm_business_priors,
)
from mapi_plugin_mmm.settings import PyMCMarketingSettings, mcmc_fit_schema

__all__ = ["SCHEMA_TEMPLATE", "PyMCMarketingMMM", "pymc_marketing_available"]

_TRANSLATION_SOLVE_DRAWS = 1000
"""Prior draws of the pymc-marketing model when solving a business-prior translation."""


def _units(ctx: FitContext, ds: xr.Dataset, name: str) -> str | None:
    """The business units of model-stage variable ``name`` (``None`` when unknown)."""
    if ctx.to_original is None or name not in ds:
        return None
    try:
        out = ctx.to_original(xr.ones_like(ds[name]), name)
    except (MapiError, KeyError, ValueError):
        return None
    units = out.attrs.get("units")
    return units[:64] if isinstance(units, str) and units else None


def _with_priors(s: PyMCMarketingSettings, priors: dict[str, Any]) -> PyMCMarketingSettings:
    """``s`` with prior overrides replaced by ``priors`` (translated keys win)."""
    raw = s.model_dump(mode="json", exclude_none=True)
    raw["priors"] = {**raw.get("priors", {}), **priors}
    return PyMCMarketingSettings.model_validate(raw)


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
    """Silence library logging, warnings and progress bars (pymc-marketing recomputes
    deterministics with an unconditional rich progress bar on stdout)."""
    loggers = [logging.getLogger(n) for n in ("pymc", "pymc_marketing", "pytensor")]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
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
        model_config=_model_config(s, len(channels)) or None,
    )


def _model_config(s: PyMCMarketingSettings, n_channels: int) -> dict[str, Any]:
    """``settings.priors`` as pymc-marketing ``model_config`` priors: the channel effect
    per ``(*dims, channel)`` and the intercept per panel cell."""
    from pymc_extras.prior import Prior

    out: dict[str, Any] = {}

    def prior(spec: Any, dims: tuple[str, ...]) -> Any:
        params = {k: np.asarray(v, dtype=float) if isinstance(v, list) else float(v)
                  for k, v in spec.params.items()}  # fmt: skip
        for k, v in params.items():
            if isinstance(v, np.ndarray) and v.shape != (n_channels,):
                raise MapiError(
                    f"priors: {k} has {v.size} values for {n_channels} channels",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer="/model/settings/priors",
                )
        return Prior(spec.distribution, **params, dims=dims)

    beta = s.priors.saturation_beta
    if beta is not None:
        out[f"saturation_{BETA_PARAM[s.saturation.kind]}"] = prior(beta, (*s.dims, CHANNEL))
    if s.priors.intercept is not None:
        out["intercept"] = prior(s.priors.intercept, tuple(s.dims))
    if s.priors.gamma_control is not None:
        out["gamma_control"] = prior(s.priors.gamma_control, (*s.dims, "control"))
    if s.priors.gamma_fourier is not None:
        out["gamma_fourier"] = prior(s.priors.gamma_fourier, (*s.dims, "fourier_mode"))
    return out


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
    version = "1.1.0"
    Settings = PyMCMarketingSettings
    capabilities = Capabilities(
        inference="mcmc",
        representation=Representation.DRAWS,
        score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate"}),
        contribution_scale="additive",
        owns=frozenset({"scaling", "carryover", "inverse_units"}),
        deterministic_given_seed=False,
        checkpointable=False,
        calibration=frozenset({"business_priors", "lift_tests"}),
    )
    schema_template = SCHEMA_TEMPLATE
    outputs: ClassVar[dict[str, Any]] = {
        # the panel dims (settings ``dims``) come in front of ``time``
        "y_hat": {
            "dims": [TIME],
            "dims_of": TARGET,
            "units_of": TARGET,
            "description": "Expected (or predictive) target in original units, over the "
            "panel dims and time",
        },
        "contributions": {
            "dims": ["component", TIME],
            "dims_of": TARGET,
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
        cal = ctx.calibration
        calibration: dict[str, Any] = {}
        beliefs = cal.business_priors if cal is not None else None
        if beliefs is not None and cal is not None and cal.prior_translation_run is None:
            # no translate_priors run wrote the priors yet: translate them here
            ctx.progress(0.01, "translating business priors")
            outcome = self._translate(
                frame, s, channels, beliefs, draws=_TRANSLATION_SOLVE_DRAWS, seed=seed
            )
            errors = [i for i in outcome.issues if i.severity is Severity.ERROR]
            if errors:
                raise MapiError(
                    errors[0].message,
                    code=ErrorCode.BUSINESS_PRIORS_INVALID,
                    pointer="/calibration" + errors[0].pointer,
                    issues=errors,
                )
            s = _with_priors(s, outcome.priors)
            calibration["translated_priors"] = outcome.priors
        mmm = _make_mmm(s, channels)
        lift = lift_frame(cal.lift_tests, frame, s, channels) if cal and cal.lift_tests else None
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
                x_fit, y_fit = frame.drop(columns=[TARGET]), frame[TARGET]
                if lift is not None and not lift.empty:
                    mmm.build_model(x_fit, y_fit)
                    mmm.add_lift_test_measurements(lift, dist=_lift_dist(), name=LIFT_NAME)
                idata = mmm.fit(x_fit, y_fit, **kwargs)
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
        if lift is not None:
            calibration["lift_rows"] = len(lift)
        if calibration:
            learned["calibration"] = calibration
        ctx.progress(1.0, "fit complete")
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    # ------------------------------------------------------------------ business priors
    @staticmethod
    def _prior_draws(
        frame: pd.DataFrame, s: PyMCMarketingSettings, channels: list[str], *, draws: int,
        seed: int,
    ) -> PmmPriorDraws:  # fmt: skip
        """Prior draws of the pymc-marketing model ``s`` describes, built on ``frame``."""
        mmm = _make_mmm(s, channels)
        with _quiet():
            mmm.build_model(frame.drop(columns=[TARGET]), frame[TARGET])
            return pmm_prior_draws(mmm, s, draws=draws, seed=seed)

    def _translate(
        self,
        frame: pd.DataFrame,
        s: PyMCMarketingSettings,
        channels: list[str],
        beliefs: BusinessPriors,
        *,
        draws: int,
        seed: int,
    ) -> TranslationOutcome:
        d = self._prior_draws(frame, s, channels, draws=draws, seed=seed)
        return translate_pmm_business_priors(beliefs, d, frame, channels, s, seed=seed)

    def translate_priors(
        self,
        ds: xr.Dataset,
        s: PyMCMarketingSettings,
        job: PriorTranslationJob,
        ctx: FitContext,
    ) -> PriorTranslation:
        """Business priors to ``saturation_beta``/``intercept`` priors with a
        prior-predictive preview in business units
        (:mod:`mapi_plugin_mmm.pymc_marketing_calibration`)."""
        frame, channels = _frame(ds, s, with_target=True)
        seed = job.seed if job.seed is not None else ctx.seed
        ctx.progress(0.1, "solving for model-space priors")
        outcome = self._translate(
            frame, s, channels, job.business_priors,
            draws=max(_TRANSLATION_SOLVE_DRAWS, job.draws), seed=seed,
        )  # fmt: skip
        ctx.check_cancelled()
        ctx.progress(0.6, "sampling the prior predictive")
        translated = _with_priors(s, outcome.priors)
        d = self._prior_draws(frame, translated, channels, draws=job.draws, seed=seed + 1)
        result, preview = pmm_prior_preview(
            outcome, d, frame, draws=job.draws, seed=seed + 1, quantiles=job.quantiles,
            lift_tests=job.lift_tests, kpi_units=_units(ctx, ds, TARGET),
            spend_units=_units(ctx, ds, SPEND),
        )  # fmt: skip
        ctx.progress(1.0, "priors translated")
        return PriorTranslation(result=result, preview=preview)

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


LIFT_NAME = "lift_measurements"


def _lift_dist() -> Any:
    """pymc-marketing's default lift likelihood (a Gamma on the absolute lift)."""
    import pymc.dims as pmd

    return pmd.Gamma


def lift_frame(
    tests: list[LiftTest], frame: pd.DataFrame, s: PyMCMarketingSettings, channels: list[str]
) -> pd.DataFrame:
    """Lift tests as pymc-marketing's ``df_lift_test`` (one row per period of a window).

    Tests whose window lies after the data or that the data's periods cut (a holdout fit
    that ends inside the test) are left out; a window inside the data must cover at least one
    period. Raises ``SCHEMA_INVALID`` at the test's pointer.
    """
    rows: list[dict[str, Any]] = []
    dates = pd.to_datetime(frame[DATE])
    last = dates.max()
    if len(s.dims) > 1:
        raise MapiError(
            "lift tests support at most one panel dim in the pymc-marketing adapter",
            code=ErrorCode.CALIBRATION_UNSUPPORTED,
            pointer="/calibration/lift_tests",
        )
    for i, test in enumerate(tests):
        here = f"/calibration/lift_tests/{i}"
        if test.channel not in channels:
            raise _schema_error(f"lift test {i}: no channel {test.channel!r}", here + "/channel")
        mask = (dates >= pd.Timestamp(test.start)) & (dates <= pd.Timestamp(test.end))
        sub = frame[mask]
        if s.dims:
            dim = s.dims[0]
            if test.geo is None:
                raise MapiError(
                    f"lift test {i}: name the {dim!r} it ran in (the model has panel dims)",
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=here + "/geo",
                )
            if str(test.geo) not in set(map(str, frame[dim])):
                raise _schema_error(f"lift test {i}: no {dim} {test.geo!r}", here + "/geo")
            sub = sub[sub[dim].astype(str) == str(test.geo)]
        periods = int(sub[DATE].nunique())
        if not periods:
            if pd.Timestamp(test.start) > last:
                continue
            raise _schema_error(f"lift test {i}: its window covers no period of the data", here)
        if lift_window_cut(dates.to_numpy(), test.start, test.end):
            continue
        row: dict[str, Any] = {
            "channel": test.channel,
            "x": float(sub[test.channel].mean()),
            "delta_x": test.spend_delta / periods,
            "delta_y": test.kpi_delta / periods,
            "sigma": test.sigma / periods,
        }
        if s.dims:
            row[s.dims[0]] = test.geo
        if row["x"] + row["delta_x"] < -1e-9:
            raise _schema_error(
                f"lift test {i} removes more spend than the data shows", here + "/spend_delta"
            )
        rows.append(row)
    return pd.DataFrame(rows)


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
