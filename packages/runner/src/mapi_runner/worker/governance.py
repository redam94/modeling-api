"""``governance`` runs (M3.5): measure a policy's checks on one model version in the sandbox.

Every check of :data:`mapi_spec.governance.GOVERNANCE_CHECKS` is implemented here and is
**model-family aware** (:func:`mapi_spec.governance.model_kind`):

========================== =================================================================
check                      how it is measured
========================== =================================================================
``null.permutation_target`` refit on block-shuffled targets ``n_permutations`` times (same
                           budgeted settings as the real refit); ``p = (1 + #{shuffled <=
                           real}) / (K + 1)`` on the holdout metric
``null.noise_regressors``  add ``n_noise`` placebo drivers (a shuffled real driver or
                           Gaussian noise; new channels for channel-dim models, new
                           variables otherwise), refit, and measure each one's effect share
                           by ablation through the score path (Bayesian: per posterior draw,
                           with the 90% interval of its total effect; frequentist: its
                           p-value when the plugin reports one)
``null.placebo_time_shift`` refit with the drivers moved ``shift`` periods (circularly, every
                           group alike); the placebo's total effect over the real one
``baseline.beats_naive``   the train-only model on the held-out periods against naive,
                           seasonal-naive, mean or drift forecasts from the training history:
                           ``1 - L(model) / min_b L(b)`` (plus MASE)
``residuals.whiteness``    Ljung-Box on the in-sample residuals of every series (minimum p;
                           ``model_df`` = p + q of an ARMA ``order`` setting)
``stability.rolling_params`` refits on rolling windows; the largest coefficient of variation
                           of the key estimates across windows
``plausibility.signs``     media/spend effects must raise the KPI (ablation per draw) or the
                           listed ``expected`` signs hold (share of the posterior, or of the
                           sampling distribution, on the wrong side)
``sensitivity.prior``      Bayesian only: refit with every prior scale in the settings
                           multiplied by each of ``scales``; the largest relative move of a
                           key posterior mean
``recovery.simulated``     simulate targets from the fit (expected value + block-bootstrapped
                           residuals), refit, and count how often the 90% intervals contain
                           the fitted values (SBC-lite; ``n_sims`` refits)
``convergence``            Bayesian: max split R-hat (divergences and ESS as evidence);
                           frequentist and ML: the optimiser's converged flag
``holdout.accuracy``       the fit's holdout metric (``metrics.json``), else recomputed
``data.quality``           the Data Explorer's quality warnings that are not ``info``
========================== =================================================================

Refits go through :class:`~mapi_runner.worker.refit.Refitter` under the job's
:class:`~mapi_spec.governance.RefitBudget`. A check that does not apply is ``skipped`` with
the reason; one that fails to compute is ``error`` (a blocking error blocks validation).
Output: ``governance.json`` (:class:`~mapi_spec.governance.GovernanceReport`).
"""

from __future__ import annotations

import math
import re
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    GOVERNANCE_OUTPUT,
    CheckResult,
    CheckStatus,
    EvidenceItem,
    GovernanceJob,
    GovernanceReport,
    ModelKind,
    PolicyCheck,
    check_applies,
    check_status,
)
from mapi_spec.run import RunnerPhase
from mapi_spec.schema import DataSchema, Role, VarSpec
from mapi_spec.scorecard import CheckDirection
from mapi_spec.spec import ModelSpec

from mapi_runner.worker import stats
from mapi_runner.worker.explore_data import data_context, explore_period, quality_warnings
from mapi_runner.worker.explore_model import error_metrics
from mapi_runner.worker.modelview import (
    ParamEstimate,
    Prediction,
    bundle_kind,
    convergence_stats,
    holdout_mask,
    param_estimates,
    predict,
    target_vars,
    time_dim_of,
)
from mapi_runner.worker.refit import BudgetExhausted, Refitter

__all__ = ["CHECK_IMPLEMENTATIONS", "GovernanceEnv", "measure", "run_governance"]

_PERCENT: Final = frozenset({"mape", "smape", "wape"})
_NOT_KEY: Final = re.compile(
    r"(sigma|scale|noise|^nu$|intercept|const|lengthscale|^eta|^ls_|alpha_raw|_offset|"
    r"chol|corr|^z_|_z$|log_lik|^mu$)",
    re.IGNORECASE,
)
_PRIOR_SCALE_KEYS: Final = frozenset({"sigma", "scale", "sd", "tau", "beta", "b"})
_DRIVER_ROLES: Final = (Role.MEDIA, Role.SPEND)
_COVARIATE_ROLES: Final = (Role.COVARIATE, Role.CONTROL, Role.EXPOSURE)


def _fv(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pint(params: Mapping[str, Any], key: str, default: int) -> int:
    v = params.get(key, default)
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else default


def _pfloat(params: Mapping[str, Any], key: str, default: float) -> float:
    v = params.get(key, default)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else default


def _pct(x: float) -> str:
    return f"{x:.0%}" if abs(x) >= 0.1 else f"{x:.1%}"


# ============================================================================ environment


@dataclass
class GovernanceEnv:
    """What every check reads: the bundle, its data, the refitter and derived facts."""

    bundle: Bundle
    data: xr.Dataset
    registry: Registry
    refitter: Refitter
    evaluation: Bundle | None = None
    seed: int = 0
    side_tables: Mapping[str, xr.Dataset] = field(default_factory=dict)
    log: Callable[[str], None] = lambda _m: None

    def __post_init__(self) -> None:
        self.kind: ModelKind = bundle_kind(self.bundle)
        self.family = self.bundle.model.manifest.model_family
        self.time_dim = time_dim_of(self.bundle, self.data)
        self.targets = [t for t in target_vars(self.bundle) if t in self.data]
        self._pred: list[Prediction] | None = None

    # ----------------------------------------------------------------- facts
    @property
    def schema(self) -> DataSchema:
        return self.bundle.raw_schema

    def role(self, var: str) -> Role:
        spec = self.schema.vars.get(var)
        return spec.role if spec is not None else Role.COVARIATE

    def panel_dims(self) -> set[str]:
        dims: set[str] = set()
        for t in self.targets:
            dims |= {str(d) for d in self.data[t].dims}
        return dims - {self.time_dim} if self.time_dim else dims

    def channel_dim(self) -> tuple[str, list[str]] | None:
        """``(channel dim, media/spend vars on it)``: a non-time, non-panel dim of a media or
        spend variable (``spend(geo, week, channel)`` -> ``channel``)."""
        panel = self.panel_dims()
        for var in self.data.data_vars:
            name = str(var)
            if self.role(name) not in _DRIVER_ROLES:
                continue
            for d in self.data[name].dims:
                dim = str(d)
                if dim != self.time_dim and dim not in panel:
                    carriers = [
                        str(v)
                        for v in self.data.data_vars
                        if self.role(str(v)) in _DRIVER_ROLES and dim in self.data[v].dims
                    ]
                    return dim, carriers
        return None

    def drivers(self) -> list[str]:
        """Media/spend variables, else covariates/controls (never targets)."""
        media = [
            str(v)
            for v in self.data.data_vars
            if self.role(str(v)) in _DRIVER_ROLES and str(v) not in self.targets
        ]
        if media:
            return media
        return [
            str(v)
            for v in self.data.data_vars
            if self.role(str(v)) in _COVARIATE_ROLES
            and str(v) not in self.targets
            and np.issubdtype(self.data[v].dtype, np.number)
        ]

    def has(self) -> set[str]:
        out: set[str] = set()
        if self.bundle.spec.data.split is not None:
            out.add("holdout")
        if self.time_dim is not None:
            out.add("time_series")
        if self.channel_dim() is not None:
            out.add("channels")
        if self.targets:
            out.add("residuals")
        return out

    def season(self) -> int | None:
        if self.time_dim is None:
            return None
        spec = self.schema.dims.get(self.time_dim)
        freq = spec.freq if spec is not None else None
        if freq is None and self.time_dim in self.data.coords:
            from mapi_sdk.schema import infer_freq

            values = np.asarray(self.data[self.time_dim].values)
            freq = infer_freq(values) if values.dtype.kind == "M" else None
        return explore_period(freq)

    def rng(self, salt: str) -> np.random.Generator:
        return np.random.default_rng([self.seed, sum(map(ord, salt))])

    def predictions(self) -> list[Prediction]:
        if self._pred is None:
            self._pred = predict(
                self.bundle, self.data, self.registry, side_tables=self.side_tables
            )
        return self._pred

    def trained(self) -> Bundle:
        """The model fitted on the training data only (refitted if the fit kept none)."""
        if self.evaluation is not None:
            return self.evaluation
        if self.bundle.evaluation is not None:
            return self.bundle.evaluation
        mode = self.bundle.spec.model.fit.mode.value
        if mode == "train_only":
            return self.bundle
        self.evaluation = self.refitter.fit(self.data, self.refitter.spec())
        return self.evaluation


# ================================================================================ results


def _threshold(check: PolicyCheck) -> float:
    d = GOVERNANCE_CHECKS[check.check]
    if d.direction is CheckDirection.BAND and d.target is not None:
        return d.target
    return check.thresholds.warn if check.thresholds is not None else d.warn


def result(
    check: PolicyCheck,
    value: float | None,
    message: str,
    *,
    expert: str | None = None,
    action: str | None = None,
    evidence: Sequence[EvidenceItem] = (),
    status: CheckStatus | None = None,
) -> CheckResult:
    """A :class:`CheckResult`: the status from the value and the policy's thresholds
    unless given; ``suggested_action`` only when the check did not pass."""
    d = GOVERNANCE_CHECKS[check.check]
    if status is None:
        status = (
            check_status(d, value, check.thresholds) if value is not None else CheckStatus.ERROR
        )
    return CheckResult(
        check=check.check,
        status=status,
        severity=check.severity,
        metric=d.metric,
        value=_fv(value) if value is not None else None,
        threshold=_threshold(check),
        message_plain=message[:2000],
        message_expert=expert[:2000] if expert else None,
        suggested_action=(action[:1000] if action and status is not CheckStatus.PASS else None),
        params=dict(check.params),
        evidence=list(evidence)[:50],
        computed_at=datetime.now(UTC),
    )


def _skip(check: PolicyCheck, why: str) -> CheckResult:
    return result(check, None, f"Not applicable: {why}.", status=CheckStatus.SKIPPED)


def _error(check: PolicyCheck, why: str, expert: str | None = None) -> CheckResult:
    return result(
        check,
        None,
        f"The check could not be computed: {why}",
        expert=expert,
        action="Look at the run's logs; fix the model or data problem and resubmit.",
        status=CheckStatus.ERROR,
    )


def _table(name: str, inline: Any, caption: str | None = None) -> EvidenceItem:
    return EvidenceItem(kind="table", name=name, inline=inline, caption_plain=caption)


def _metric(name: str, value: float | None, caption: str | None = None) -> EvidenceItem:
    return EvidenceItem(kind="metric", name=name, value=_fv(value), caption_plain=caption)


# ================================================================================ helpers


def _holdout_metric(b: Bundle, metric: str) -> float | None:
    v = _fv(b.metrics.values.get(metric))
    if v is None:
        return None
    return v / 100.0 if metric in _PERCENT else v


def _total_effect(full: Prediction, ablated: Prediction) -> tuple[float, float, np.ndarray | None]:
    """``(sum |delta|, sum |full|, per-draw signed total delta)`` of an ablation."""
    delta = np.asarray(full.point.values, dtype=float) - np.asarray(
        ablated.point.values, dtype=float
    )
    base = np.asarray(full.point.values, dtype=float)
    per_draw = None
    if full.draws is not None and ablated.draws is not None:
        d = np.asarray(full.draws.values, dtype=float) - np.asarray(
            ablated.draws.values, dtype=float
        )
        per_draw = np.nansum(d.reshape(d.shape[0], -1), axis=1)
    return float(np.nansum(np.abs(delta))), float(np.nansum(np.abs(base))), per_draw


def _first(preds: list[Prediction], target: str | None = None) -> Prediction:
    for p in preds:
        if target is None or p.target == target:
            return p
    raise ValueError("the model produced no prediction of its target")


def _ablate(
    env: GovernanceEnv, data: xr.Dataset, var: str, sel: Mapping[str, Any] | None = None
) -> xr.Dataset:
    """``var`` switched off: media/spend to zero, other drivers to their mean (on ``sel``
    coordinates only, when given)."""
    da = data[var]
    off = (
        xr.zeros_like(da) if env.role(var) in _DRIVER_ROLES else xr.full_like(da, float(da.mean()))
    )
    if sel:
        mask = xr.zeros_like(da, dtype=bool)
        mask.loc[dict(sel)] = True
        off = off.where(mask, da)
    return data.assign({var: off.astype(da.dtype)})


def _key_params(
    est: list[tuple[str, dict[str, Any], ParamEstimate]],
    wanted: Sequence[str] | None,
    limit: int = 10,
) -> list[ParamEstimate]:
    if wanted:
        keep = [e for v, _c, e in est if v in wanted or e.name in wanted]
        return keep[:50]
    return [e for v, _c, e in est if not _NOT_KEY.search(v)][:limit]


def _estimates(
    b: Bundle, *, interval: str = "hdi_90"
) -> list[tuple[str, dict[str, Any], ParamEstimate]]:
    label = interval if bundle_kind(b) == "bayesian" else "ci_90"
    return param_estimates(b, interval=label, max_params=500)


def _shift_time(
    env: GovernanceEnv, data: xr.Dataset, vars_: Iterable[str], shift: int
) -> xr.Dataset:
    assert env.time_dim is not None
    out = {}
    for v in vars_:
        if env.time_dim in data[v].dims:
            out[v] = data[v].copy(
                data=np.roll(data[v].values, shift, axis=data[v].get_axis_num(env.time_dim))
            )
    return data.assign(out)


# ================================================================================= checks


def _permutation(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    p = check.params
    k = _pint(p, "n_permutations", 20)
    metric = str(p.get("metric", "rmse"))
    split = env.bundle.spec.data.split
    assert split is not None
    dims = split.split_dims() if hasattr(split, "split_dims") else []
    dim = env.time_dim or (env.bundle.binding.to_dataset(dims[0]) if dims else None)
    if dim is None or not env.targets:
        return _skip(check, "the model has no target to shuffle along its split")
    n = int(env.data.sizes[dim])
    block = (_pint(p, "block", 0) or max(1, n // 20)) if env.time_dim else 1
    k = min(k, env.refitter.remaining - 1)
    if k < 5:
        return _error(check, "the refit budget allows fewer than 5 shuffled fits")
    spec = env.refitter.spec(metrics=[metric])
    real_b = env.refitter.fit(env.data, spec)
    real = _holdout_metric(real_b, metric)
    if real is None:
        return _error(
            check, f"the refit has no holdout {metric}", real_b.metrics.skipped.get(metric)
        )
    rng = env.rng(check.check)
    scores: list[float] = []
    for _i in range(k):
        perm = stats.block_permutation(n, block, rng)
        shuffled = env.data.assign(
            {
                t: env.data[t].copy(
                    data=np.take(env.data[t].values, perm, axis=env.data[t].get_axis_num(dim))
                )
                for t in env.targets
                if dim in env.data[t].dims
            }
        )
        try:
            b = env.refitter.fit(shuffled, spec)
        except BudgetExhausted:
            break
        s = _holdout_metric(b, metric)
        if s is not None:
            scores.append(s)
    if len(scores) < 5:
        return _error(check, "fewer than 5 shuffled fits produced a score")
    as_good = sum(1 for s in scores if s <= real)
    pval = (1 + as_good) / (len(scores) + 1)
    ev = [
        _metric(f"holdout_{metric}", real, "The real model's error on held-out data"),
        _table(
            "permutation_scores",
            {
                "real": real,
                "shuffled": [round(s, 6) for s in scores],
                "shuffled_min": min(scores),
                "shuffled_median": float(np.median(scores)),
                "block": block,
                "metric": metric,
            },
            "Errors of the fits on shuffled results",
        ),
    ]
    if as_good == 0:
        msg = (
            f"The model's error on held-out data ({real:.3g}) was lower than all "
            f"{len(scores)} fits on shuffled results (p = {pval:.3f})."
        )
        if len(scores) < 19:
            msg += (
                f" With {len(scores)} shuffles the smallest possible p is {pval:.3f}: "
                "use at least 19 for a 5% test."
            )
    else:
        msg = (
            f"{as_good} of {len(scores)} fits on shuffled results did as well as the real "
            f"model (p = {pval:.3f}): it may be fitting noise rather than signal."
        )
    return result(
        check,
        pval,
        msg,
        expert=f"Block permutation test (block {block}), {metric}: real {real:.4g}, shuffled "
        f"median {np.median(scores):.4g}; p = (1 + {as_good}) / ({len(scores)} + 1).",
        action="Check for leakage between training and test data, simplify the model, or add "
        "drivers that carry real signal.",
        evidence=ev,
    )


@dataclass
class _Placebo:
    data: xr.Dataset
    schema: DataSchema
    spec: ModelSpec
    names: list[str]
    how: str
    ablate: Callable[[xr.Dataset, str], xr.Dataset]
    params_like: Callable[[str], bool]


def _walk_settings(settings: Any, existing: set[str], extra: Sequence[str], source: str) -> Any:
    """Append ``extra`` names to lists that enumerate ``existing`` names, and copy the
    ``source`` entry of mappings keyed by them (per-channel priors)."""
    if isinstance(settings, list):
        if (
            settings
            and all(isinstance(x, str) for x in settings)
            and set(settings) <= existing
            and len(settings) >= 1
        ):
            return [*settings, *extra]
        return [_walk_settings(x, existing, extra, source) for x in settings]
    if isinstance(settings, dict):
        keys = set(settings)
        out = {k: _walk_settings(v, existing, extra, source) for k, v in settings.items()}
        if keys and keys <= existing and source in settings:
            for e in extra:
                out[e] = settings[source]
        return out
    return settings


def _placebo_drivers(
    env: GovernanceEnv, k: int, kind: str, rng: np.random.Generator
) -> _Placebo | None:
    data, schema = env.data, env.schema
    names = [f"noise_{i + 1}" for i in range(k)]
    base_settings = env.bundle.spec.model.plugin_settings
    ch = env.channel_dim()
    t = env.time_dim

    def noise_like(values: np.ndarray, axis: int | None, nonneg: bool) -> np.ndarray:
        if kind == "gaussian":
            mu, sd = float(np.nanmean(values)), float(np.nanstd(values)) or 1.0
            out = rng.normal(mu, sd, size=values.shape)
            return np.abs(out) if nonneg else out
        if axis is None:
            return rng.permutation(values.reshape(-1)).reshape(values.shape)
        n = values.shape[axis]
        perm = stats.block_permutation(n, max(1, n // 20), rng)
        return np.take(values, perm, axis=axis)

    if ch is not None:
        dim, carriers = ch
        labels = [str(x) for x in np.asarray(data[dim].values)]
        sources = [labels[int(rng.integers(len(labels)))] for _ in names]
        new_vars: dict[str, xr.DataArray] = {}
        for var in (str(v) for v in data.data_vars):
            da = data[var]
            if dim not in da.dims:
                continue
            pieces = []
            for nm, src in zip(names, sources, strict=True):
                piece = da.sel({dim: [src]}).assign_coords({dim: [nm]})
                if var in carriers:
                    axis = piece.get_axis_num(t) if t and t in piece.dims else None
                    piece = piece.copy(
                        data=noise_like(np.asarray(piece.values, dtype=float), axis, True)
                    )
                pieces.append(piece)
            new_vars[var] = xr.concat([da, *pieces], dim=dim)
        extended = data.drop_dims(dim).assign(new_vars)
        settings = _walk_settings(base_settings, set(labels), names, sources[0])
        spec = (
            env.refitter.spec(split=None, settings=settings)
            if settings != base_settings
            else env.refitter.spec(split=None)
        )

        def ablate(ds: xr.Dataset, name: str) -> xr.Dataset:
            out = ds
            for var in carriers:
                out = _ablate(env, out, var, {dim: name})
            return out

        return _Placebo(
            extended,
            schema,
            spec,
            names,
            f"channels on {dim!r}",
            ablate,
            lambda s: any(n in s for n in names),
        )
    drivers = env.drivers()
    if not drivers:
        return None
    src = drivers[0]
    src_da = data[src]
    added: dict[str, xr.DataArray] = {}
    vars_ = dict(schema.vars)
    for nm in names:
        axis = src_da.get_axis_num(t) if t and t in src_da.dims else None
        added[nm] = src_da.copy(
            data=noise_like(
                np.asarray(src_da.values, dtype=float), axis, env.role(src) in _DRIVER_ROLES
            )
        ).astype(float)
        spec_src = schema.vars.get(src)
        vars_[nm] = VarSpec(
            dims=tuple(str(d) for d in src_da.dims),
            dtype="float64",
            role=spec_src.role if spec_src else Role.COVARIATE,
        )
    new_schema = schema.model_copy(update={"vars": vars_})
    existing = {str(v) for v in data.data_vars} | {
        env.bundle.binding.to_canonical(str(v)) for v in data.data_vars
    }
    settings = _walk_settings(base_settings, existing, names, src)
    spec = (
        env.refitter.spec(split=None, settings=settings)
        if settings != base_settings
        else env.refitter.spec(split=None)
    )

    def ablate_var(ds: xr.Dataset, name: str) -> xr.Dataset:
        return _ablate(env, ds, name)

    return _Placebo(
        data.assign(added),
        new_schema,
        spec,
        names,
        "variables",
        ablate_var,
        lambda s: any(s == n or s.startswith(n + "[") for n in names),
    )


def _noise(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    p = check.params
    k = _pint(p, "n_noise", 3)
    kind = str(p.get("kind", "shuffled_driver"))
    rng = np.random.default_rng(_pint(p, "seed", 0) + env.seed)
    placebo = _placebo_drivers(env, k, kind, rng)
    if placebo is None:
        return _skip(check, "the model has no drivers to imitate")
    if not env.targets:
        return _skip(check, "the model has no target")
    b = env.refitter.fit(placebo.data, placebo.spec, raw_schema=placebo.schema)
    full = _first(
        predict(b, placebo.data, env.registry, side_tables=env.side_tables), env.targets[0]
    )
    shares: dict[str, float] = {}
    intervals: dict[str, list[float | None]] = {}
    for nm in placebo.names:
        abl = _first(
            predict(b, placebo.ablate(placebo.data, nm), env.registry, side_tables=env.side_tables),
            env.targets[0],
        )
        num, den, per_draw = _total_effect(full, abl)
        shares[nm] = num / den if den > 0 else 0.0
        if per_draw is not None and per_draw.size > 1:
            lo, hi = np.quantile(per_draw, [0.05, 0.95])
            intervals[nm] = [_fv(lo), _fv(hi)]
    ignored = all(v < 1e-12 for v in shares.values()) and all(
        (iv[0] or 0.0) == 0.0 and (iv[1] or 0.0) == 0.0 for iv in intervals.values()
    )
    if shares and ignored and placebo.how == "variables":
        den = float(np.nansum(np.abs(np.asarray(full.point.values, dtype=float))))
        coef = {nm: _coef_effect(env, b, placebo.data, nm) for nm in placebo.names}
        if den > 0 and all(c is not None for c in coef.values()):
            shares = {nm: float(c) / den for nm, c in coef.items() if c is not None}
            ignored = False
    if shares and ignored:
        return _skip(
            check,
            "the model takes a fixed set of drivers and ignored the fake ones; "
            "list a noise driver in its settings to run this check",
        )
    rows = _estimates(b)
    p_values = {
        e.name: e.p_value
        for v, c, e in rows
        if e.p_value is not None
        and (placebo.params_like(e.name) or any(placebo.params_like(str(x)) for x in c.values()))
    }
    value = max(shares.values()) if shares else 0.0
    worst = max(shares, key=lambda n: shares[n]) if shares else ""
    clear = [
        n
        for n, iv in intervals.items()
        if iv[0] is not None and iv[1] is not None and (iv[0] > 0 or iv[1] < 0)
    ]
    sig = [n for n, pv in p_values.items() if pv is not None and pv < 0.05]
    ev = [
        _table(
            "noise_effect_share",
            {k_: round(v, 6) for k_, v in shares.items()},
            "Share of the result each fake driver was credited with",
        ),
        _table("placebo", {"how": placebo.how, "kind": kind, "names": placebo.names}),
    ]
    if intervals:
        ev.append(
            _table(
                "noise_effect_interval_90",
                intervals,
                "90% interval of each fake driver's total effect (should cover 0)",
            )
        )
    if p_values:
        ev.append(_table("noise_p_values", p_values, "p-values of the fake drivers' estimates"))
    status = None
    if clear or sig:
        status = (
            CheckStatus.FAIL
            if check_status(check.check, value, check.thresholds) is CheckStatus.FAIL
            else CheckStatus.WARN
        )
    if value <= (
        check.thresholds.warn if check.thresholds else GOVERNANCE_CHECKS[check.check].warn
    ) and not (clear or sig):
        msg = (
            f"{len(shares)} fake drivers made of pure noise got at most {_pct(value)} of the "
            "result: the model does not credit drivers that carry no signal."
        )
    else:
        extra = ""
        if clear:
            extra = f" The effect of {', '.join(clear)} is clearly not zero."
        elif sig:
            extra = f" {', '.join(sig)} came out statistically significant."
        msg = (
            f"A fake driver made of pure noise ({worst}) got {_pct(value)} of the result: the "
            f"model credits drivers too easily.{extra}"
        )
    return result(
        check,
        value,
        msg,
        status=status,
        expert=f"{len(shares)} placebo {placebo.how} ({kind}); effect share = sum|y(full) - "
        "y(placebo off)| / sum|y(full)| through the score path; max over placebos.",
        action="Use stronger regularisation or priors, fewer drivers, or more data before "
        "trusting how the result is split between drivers.",
        evidence=ev,
    )


def _coef_effect(env: GovernanceEnv, b: Bundle, data: xr.Dataset, var: str) -> float | None:
    """``|beta| * sum |x - x_off|`` from the coefficient named after driver ``var`` (``None``
    without one): the effect of a driver on a model that is linear in it, for plugins whose
    in-sample predictions follow the stored history rather than the scored drivers (the
    statsmodels models)."""
    names = {var, b.binding.to_canonical(var)}
    betas = [abs(e.estimate) for _v, _c, e in _estimates(b) if e.name in names or _v in names]
    if not betas:
        return None
    x = np.asarray(data[var].values, dtype=float)
    off = np.asarray(_ablate(env, data, var)[var].values, dtype=float)
    return float(np.mean(betas) * np.nansum(np.abs(x - off)))


def _effect(env: GovernanceEnv, b: Bundle, data: xr.Dataset, drivers: Sequence[str]) -> float:
    full = _first(predict(b, data, env.registry, side_tables=env.side_tables), env.targets[0])
    off = data
    for v in drivers:
        off = _ablate(env, off, v)
    abl = _first(predict(b, off, env.registry, side_tables=env.side_tables), env.targets[0])
    num, _den, _d = _total_effect(full, abl)
    if num <= 0:
        coef = [_coef_effect(env, b, data, v) for v in drivers]
        known = [c for c in coef if c is not None]
        if known:
            return float(sum(known))
    return num


def _placebo_shift(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    drivers = env.drivers()
    if not drivers or not env.targets:
        return _skip(check, "the model has no drivers to move in time")
    assert env.time_dim is not None
    n = int(env.data.sizes[env.time_dim])
    raw = check.params.get("shifts", [-26, 26])
    shifts = [int(s) for s in raw if isinstance(s, int)] if isinstance(raw, list) else [-26, 26]
    shifts = sorted({max(-(n // 3), min(n // 3, s)) for s in shifts} - {0})
    if not shifts:
        return _skip(check, f"the series ({n} periods) is too short to move the drivers")
    spec = env.refitter.spec(split=None)
    real_b = env.refitter.fit(env.data, spec)
    real = _effect(env, real_b, env.data, drivers)
    if real <= 0:
        return _error(
            check,
            "the drivers have no effect in the real model, so there is nothing to compare with",
        )
    ratios: dict[str, float] = {}
    for s in shifts:
        shifted = _shift_time(env, env.data, drivers, s)
        try:
            b = env.refitter.fit(shifted, spec)
        except BudgetExhausted:
            break
        ratios[str(s)] = _effect(env, b, shifted, drivers) / real
    if not ratios:
        return _error(check, "no shifted fit could run within the refit budget")
    worst = max(ratios, key=lambda k_: ratios[k_])
    value = ratios[worst]
    ev = [
        _table(
            "placebo_effect_ratio",
            {k_: round(v, 6) for k_, v in ratios.items()},
            "Effect after moving the drivers, as a share of the real effect, per shift",
        ),
        _table("drivers", drivers),
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = (
            f"Moving the drivers {abs(int(worst))} periods away keeps only {_pct(value)} of "
            "their effect: the effect is tied to when they happened."
        )
    else:
        msg = (
            f"The model still finds {_pct(value)} of the effect when the drivers are moved "
            f"{abs(int(worst))} periods away: it picks up timing patterns (trend, season) "
            "rather than the drivers."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Circular time-shift placebo of {len(drivers)} driver(s) by {shifts}; ratio "
        "of the placebo fit's total ablation effect to the real fit's, maximised over shifts.",
        action="Add trend and seasonality controls, or check for drivers that simply follow "
        "the calendar.",
        evidence=ev,
    )


_METHOD_PLAIN: Final = {
    "naive": "same as last period",
    "seasonal_naive": "same as last season",
    "mean": "the historical average",
    "drift": "the historical trend line",
}


def _loss(e: np.ndarray, metric: str) -> float | None:
    e = e[np.isfinite(e)]
    if e.size == 0:
        return None
    return float(np.sqrt(np.mean(e**2))) if metric == "rmse" else float(np.mean(np.abs(e)))


def _beats_naive(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    raw = check.params.get("methods", ["naive", "seasonal_naive"])
    methods = [str(m) for m in raw] if isinstance(raw, list) else ["naive", "seasonal_naive"]
    metric = str(check.params.get("metric", "mae"))
    trained = env.trained()
    mask = holdout_mask(env.bundle, env.data, trained)
    if mask is None or env.time_dim is None or str(mask.dims[0]) != env.time_dim:
        return _skip(check, "the model has no held-out periods at the end of its data")
    t = env.time_dim
    preds = predict(trained, env.data, env.registry, side_tables=env.side_tables)
    p = _first(preds, env.targets[0] if env.targets else None)
    held = np.asarray(mask.values)
    n_train = int((~held).sum())
    h = int(held.sum())
    obs = np.asarray(p.observed.transpose(t, ...).values, dtype=float).reshape(held.size, -1)
    pt = np.asarray(p.point.transpose(t, ...).values, dtype=float).reshape(held.size, -1)
    season = env.season()
    model_err = (obs[held] - pt[held]).reshape(-1)
    bench_err: dict[str, list[np.ndarray]] = {m: [] for m in methods}
    scale_parts: list[float] = []
    for j in range(obs.shape[1]):
        hist = obs[:n_train, j]
        fc = stats.naive_forecasts(hist, h, season=season, methods=methods)  # type: ignore[arg-type]
        for m, f in fc.items():
            bench_err[m].append(obs[held, j] - f)
        m_ = season if season and stats.finite(hist).size > season else 1
        hf = stats.finite(hist)
        if hf.size > m_:
            scale_parts.append(float(np.mean(np.abs(hf[m_:] - hf[:-m_]))))
    l_model = _loss(model_err, metric)
    losses: dict[str, float] = {}
    for m, v in bench_err.items():
        loss = _loss(np.concatenate(v), metric) if v else None
        if loss is not None:
            losses[m] = loss
    if l_model is None or not losses:
        return _skip(check, "no benchmark could be computed from the training history")
    best = min(losses, key=lambda m: losses[m])
    if losses[best] == 0:
        return _error(
            check,
            f"the benchmark '{_METHOD_PLAIN.get(best, best)}' is perfect on the held-out data",
        )
    skill = 1.0 - l_model / losses[best]
    scale = float(np.mean(scale_parts)) if scale_parts else None
    mae = _loss(model_err, "mae")
    mase = mae / scale if scale and mae is not None else None
    ev = [
        _table("losses", {"model": l_model, **losses, "metric": metric, "held_out_periods": h}),
        _metric("mase", mase, "Error relative to the in-sample seasonal-naive error"),
    ]
    plain = _METHOD_PLAIN.get(best, best)
    if skill >= 0:
        msg = (
            f"On the {h} held-out periods the model's error is {_pct(skill)} lower than "
            f"'{plain}' (the strongest simple benchmark)."
        )
    else:
        msg = (
            f"On the {h} held-out periods the model is worse than '{plain}' by "
            f"{_pct(-skill)}: a simple rule forecasts better."
        )
    return result(
        check,
        skill,
        msg,
        expert=f"Relative skill 1 - {metric}(model)/{metric}({best}) = {skill:.4f} over {h} "
        f"periods x {obs.shape[1]} series; MASE {mase if mase is None else round(mase, 4)}.",
        action="Check the drivers and the seasonality; a model that cannot beat a naive "
        "forecast should not be used for forecasting.",
        evidence=ev,
    )


def _arma_df(settings: Mapping[str, Any]) -> int:
    order = settings.get("order")
    if (
        isinstance(order, list | tuple)
        and len(order) == 3
        and all(isinstance(x, int) for x in order)
    ):
        return int(order[0]) + int(order[2])
    return 0


def _burn_in(settings: Mapping[str, Any]) -> int:
    """Leading periods whose one-step residuals are start-up values: ``d + D * s`` of an
    (S)ARIMA ``order``/``seasonal_order`` setting (statsmodels' diffuse start)."""
    order = settings.get("order")
    seasonal = settings.get("seasonal_order")
    d = (
        int(order[1])
        if isinstance(order, list | tuple) and len(order) == 3 and isinstance(order[1], int)
        else 0
    )
    sd = 0
    if (
        isinstance(seasonal, list | tuple)
        and len(seasonal) == 4
        and all(isinstance(x, int) for x in seasonal)
        and int(seasonal[3]) > 1
    ):
        sd = int(seasonal[1]) * int(seasonal[3])
    return d + sd


def _whiteness(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    assert env.time_dim is not None
    lags = _pint(check.params, "lags", 10)
    settings = env.bundle.spec.model.plugin_settings
    model_df = _arma_df(settings)
    burn = _burn_in(settings)
    t = env.time_dim
    rows: list[tuple[str, float, float]] = []
    for p in env.predictions():
        r = p.resid.transpose(t, ...)
        arr = np.asarray(r.values, dtype=float).reshape(r.sizes[t], -1)[burn:]
        for j in range(min(arr.shape[1], 50)):
            res = stats.ljung_box_test(arr[:, j], lags=lags, model_df=model_df, var=p.target)
            if res.status == "ok" and res.p_value is not None and res.statistic is not None:
                rows.append(
                    (
                        f"{p.target}#{j}" if arr.shape[1] > 1 else p.target,
                        res.p_value,
                        res.statistic,
                    )
                )
    if not rows:
        return _skip(check, "the residual series are too short or constant")
    worst = min(rows, key=lambda x: x[1])
    value = worst[1]
    ev = [
        _table(
            "ljung_box",
            {
                name: {"p_value": round(pv, 6), "q": round(q, 4)}
                for name, pv, q in sorted(rows, key=lambda x: x[1])[:20]
            },
            f"Ljung-Box at {lags} lags per series (worst first)",
        )
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = f"No pattern is left in the errors (Ljung-Box p = {value:.2f} at {lags} lags)."
    else:
        msg = (
            f"The errors still follow a pattern over time (Ljung-Box p = {value:.3f}): "
            "the model misses some structure."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Minimum Ljung-Box p over {len(rows)} series, lags={lags}, model_df={model_df}"
        f" (first {burn} start-up residual(s) of the differencing left out).",
        action="Add lags, seasonality or missing drivers (look at the residual ACF in the "
        "Model Explorer).",
        evidence=ev,
    )


def _stability(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    assert env.time_dim is not None
    base = _estimates(env.bundle)
    raw = check.params.get("params")
    wanted = [str(x) for x in raw] if isinstance(raw, list) else None
    key = _key_params(base, wanted)
    if not key:
        return _skip(check, "the model has no interpretable parameters to compare")
    n = int(env.data.sizes[env.time_dim])
    window = _pint(check.params, "window", 0) or max(8, n // 2)
    step = _pint(check.params, "step", 4)
    if window >= n:
        return _skip(check, f"the window ({window} periods) is not shorter than the history ({n})")
    starts = list(range(0, n - window + 1, max(1, step)))
    if len(starts) > 8:
        starts = [starts[int(i)] for i in np.linspace(0, len(starts) - 1, 8).round()]
    if len(starts) < 2:
        return _skip(check, "the history allows only one window")
    spec = env.refitter.spec(split=None)
    names = [e.name for e in key]
    per: dict[str, list[float]] = {nm: [] for nm in names}
    used = 0
    for s in starts:
        part = env.data.isel({env.time_dim: slice(s, s + window)})
        try:
            b = env.refitter.fit(part, spec)
        except BudgetExhausted:
            break
        used += 1
        got = {e.name: e.estimate for _v, _c, e in _estimates(b)}
        for nm in names:
            if nm in got:
                per[nm].append(got[nm])
    variation: dict[str, float] = {}
    base_sd = {e.name: e.sd for e in key if e.sd is not None and e.sd > 0}
    for nm, vals in per.items():
        if len(vals) >= 2:
            a = np.asarray(vals)
            # relative to the estimate's size, or to its own uncertainty when it is near 0
            scale = max(abs(float(a.mean())), base_sd.get(nm, 0.0), 1e-12)
            variation[nm] = float(a.std(ddof=1) / scale)
    if not variation:
        return _error(check, "no parameter could be estimated in two windows")
    worst = max(variation, key=lambda k_: variation[k_])
    value = variation[worst]
    ev = [
        _table(
            "window_estimates",
            {nm: [round(v, 6) for v in vals] for nm, vals in per.items()},
            f"Estimates in {used} windows of {window} periods",
        ),
        _table("variation", {k_: round(v, 6) for k_, v in variation.items()}),
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = f"The key estimates vary by at most {_pct(value)} across {used} windows of history."
    else:
        msg = (
            f"{worst} changes a lot across windows of history (variation {_pct(value)}): the "
            "relationship may not be stable over time."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Coefficient of variation across {used} rolling refits (window {window}, step "
        f"{step}), relative to max(|mean|, the full fit's standard error); max over "
        f"{len(variation)} parameters.",
        action="Look for a structural break (CUSUM or Chow tests in a workflow), shorten the "
        "history, or let the effect change over time.",
        evidence=ev,
    )


def _wrong_share(e: ParamEstimate, sign: str, kind: ModelKind) -> float:
    positive = sign == "positive"
    if e.draws is not None and e.draws.size:
        return float(np.mean(e.draws < 0) if positive else np.mean(e.draws > 0))
    if e.sd is not None and e.sd > 0 and kind == "frequentist":
        from scipy import stats as sps

        z = e.estimate / e.sd
        return float(sps.norm.cdf(-z) if positive else sps.norm.sf(-z))
    wrong = e.estimate < 0 if positive else e.estimate > 0
    return 1.0 if wrong else 0.0


def _signs(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    expected = check.params.get("expected")
    shares: dict[str, float] = {}
    estimates: dict[str, float | None] = {}
    if isinstance(expected, dict) and expected:
        rows = _estimates(env.bundle)
        for want, sign in expected.items():
            hits = [
                e for v, c, e in rows if want in (v, e.name) or want in {str(x) for x in c.values()}
            ]
            for e in hits:
                shares[e.name] = _wrong_share(e, str(sign), env.kind)
                estimates[e.name] = e.estimate
        if not shares:
            return _error(
                check,
                "none of the expected parameters was found in the model's "
                f"estimates ({', '.join(map(str, expected))})",
            )
        how = "the listed parameters"
    else:
        ch = env.channel_dim()
        if not env.targets:
            return _skip(check, "the model has no target")
        full = _first(env.predictions(), env.targets[0])
        effects: list[tuple[str, xr.Dataset]] = []
        if ch is not None:
            dim, carriers = ch
            for label in np.asarray(env.data[dim].values):
                off = env.data
                for var in carriers:
                    off = _ablate(env, off, var, {dim: label})
                effects.append((f"{dim}={label}", off))
        else:
            media = [v for v in env.drivers() if env.role(v) in _DRIVER_ROLES]
            if not media:
                return _skip(
                    check,
                    "no expected directions: list them in the check's "
                    "'expected' parameter (no media or spend drivers to assume positive)",
                )
            effects = [(v, _ablate(env, env.data, v)) for v in media]
        for name, off in effects[:30]:
            abl = _first(
                predict(env.bundle, off, env.registry, side_tables=env.side_tables), env.targets[0]
            )
            num_signed = float(
                np.nansum(np.asarray(full.point.values) - np.asarray(abl.point.values))
            )
            estimates[name] = num_signed
            if full.draws is not None and abl.draws is not None:
                d = np.asarray(full.draws.values, dtype=float) - np.asarray(
                    abl.draws.values, dtype=float
                )
                per = np.nansum(d.reshape(d.shape[0], -1), axis=1)
                shares[name] = float(np.mean(per < 0))
            else:
                shares[name] = 1.0 if num_signed < 0 else 0.0
        how = "media and spend effects (expected positive)"
    worst = max(shares, key=lambda k_: shares[k_])
    value = shares[worst]
    ev = [
        _table("wrong_sign_share", {k_: round(v, 6) for k_, v in shares.items()}),
        _table("estimates", estimates, "Estimated effect (total contribution or parameter)"),
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = f"Every effect points the expected way ({how})."
    else:
        msg = (
            f"{worst} points the wrong way: {_pct(value)} of its estimate"
            f"{' (uncertainty)' if env.kind != 'ml' else ''} is on the wrong side."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Share of the posterior (or sampling distribution) on the wrong side, max over "
        f"{len(shares)} effects ({how}).",
        action="Check the driver's data (units, sign), add the missing confounder, or use a "
        "prior that keeps the effect positive.",
        evidence=ev,
    )


def _scale_priors(node: Any, factor: float, under_prior: bool = False) -> tuple[Any, int]:
    """Multiply prior scale parameters (``sigma``, ``scale``, ``sd``, ``tau``, ``beta``,
    ``b``) inside any settings subtree whose key mentions ``prior``."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        n = 0
        for k, v in node.items():
            inside = under_prior or "prior" in str(k).lower()
            if (
                inside
                and str(k) in _PRIOR_SCALE_KEYS
                and isinstance(v, int | float)
                and not isinstance(v, bool)
                and v > 0
            ):
                out[k] = float(v) * factor
                n += 1
            else:
                out[k], m = _scale_priors(v, factor, inside)
                n += m
        return out, n
    if isinstance(node, list):
        items = [_scale_priors(x, factor, under_prior) for x in node]
        return [x for x, _m in items], sum(m for _x, m in items)
    return node, 0


def _sensitivity(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    raw = check.params.get("scales", [0.5, 2.0])
    scales = (
        [float(s) for s in raw if isinstance(s, int | float)]
        if isinstance(raw, list)
        else [0.5, 2.0]
    )
    settings = dict(env.bundle.spec.model.plugin_settings)
    _probe, found = _scale_priors(settings, 1.0)
    if found == 0:
        return _skip(
            check,
            "the model's priors are not set in its settings, so they cannot "
            "be varied automatically",
        )
    spec0 = env.refitter.spec(split=None)
    b0 = env.refitter.fit(env.data, spec0)
    base = {e.name: e for e in _key_params(_estimates(b0), None)}
    if not base:
        return _skip(check, "the model has no key parameters to compare")
    moves: dict[str, dict[str, float]] = {}
    for s in scales:
        scaled, _n = _scale_priors(settings, s)
        try:
            b = env.refitter.fit(env.data, env.refitter.spec(split=None, settings=scaled))
        except BudgetExhausted:
            break
        got = {e.name: e for _v, _c, e in _estimates(b)}
        for nm, e0 in base.items():
            if nm in got:
                denom = max(abs(e0.estimate), e0.sd or 0.0, 1e-12)
                moves.setdefault(nm, {})[str(s)] = abs(got[nm].estimate - e0.estimate) / denom
    if not moves:
        return _error(check, "no refit with changed priors could run")
    flat = [(nm, sc, v) for nm, per in moves.items() for sc, v in per.items()]
    nm, sc, value = max(flat, key=lambda x: x[2])
    ev = [
        _table(
            "relative_change",
            {k_: {s_: round(v, 6) for s_, v in per.items()} for k_, per in moves.items()},
            "Relative change of each key estimate per prior scale",
        ),
        _metric("prior_parameters_scaled", float(found)),
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = (
            f"Making the starting assumptions {min(scales):g}x to {max(scales):g}x as wide "
            f"moves the key estimates by at most {_pct(value)}: they come from the data."
        )
    else:
        msg = (
            f"{nm} moves by {_pct(value)} when the starting assumptions are scaled by {sc}: "
            "the data does not pin it down."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Refit-based prior sensitivity: {found} prior scale parameter(s) multiplied by "
        f"{scales}; |mean_s - mean_0| / max(|mean_0|, sd_0), max over parameters.",
        action="Collect more informative data (experiments, lift tests) or justify the prior "
        "with evidence before relying on these estimates.",
        evidence=ev,
    )


_DYNAMIC_FAMILIES: Final = frozenset({"forecast", "ts_multivariate", "regime"})


def _recovery(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    if not env.targets:
        return _skip(check, "the model has no target to simulate")
    if env.family in _DYNAMIC_FAMILIES and env.kind != "bayesian":
        return _skip(
            check,
            "the model predicts each period from the series' own past, so data simulated "
            "around its fitted values would not follow the model (use the backtest and "
            "residual checks for it)",
        )
    raw = check.params.get("params")
    wanted = [str(x) for x in raw] if isinstance(raw, list) else None
    truth = [
        e
        for e in _key_params(_estimates(env.bundle), wanted)
        if e.lower is not None or e.draws is not None
    ]
    if not truth:
        return _skip(check, "the model reports no parameter intervals to check")
    n_sims = min(_pint(check.params, "n_sims", 20), env.refitter.remaining)
    if n_sims < 3:
        return _error(check, "the refit budget allows fewer than 3 simulations")
    p = _first(env.predictions(), env.targets[0])
    y = env.targets[0]
    t = env.time_dim
    resid = np.asarray(p.resid.values, dtype=float)
    point = np.asarray(p.point.values, dtype=float)
    rng = env.rng(check.check)
    spec = env.refitter.spec(split=None)
    hits: dict[str, list[bool]] = {e.name: [] for e in truth}
    axis = p.observed.get_axis_num(t) if t and t in p.observed.dims else 0
    n = resid.shape[axis] if resid.ndim else 1
    block = max(1, n // 20) if t else 1
    for _i in range(n_sims):
        idx = np.concatenate(
            [
                np.arange(s, min(s + block, n))
                for s in rng.integers(0, max(1, n - block + 1), size=n // block + 1)
            ]
        )[:n]
        noise = np.take(np.nan_to_num(resid), idx, axis=axis)
        sim = np.where(np.isfinite(point), point + noise, np.nan)
        data = env.data.assign({y: env.data[y].copy(data=sim.astype(env.data[y].dtype))})
        try:
            b = env.refitter.fit(data, spec)
        except BudgetExhausted:
            break
        got = {e.name: e for _v, _c, e in _estimates(b)}
        for e0 in truth:
            e = got.get(e0.name)
            if e is None or e.lower is None or e.upper is None:
                continue
            hits[e0.name].append(e.lower <= e0.estimate <= e.upper)
    total = sum(len(v) for v in hits.values())
    if total == 0:
        return _error(check, "no simulated refit produced parameter intervals")
    coverage = sum(sum(v) for v in hits.values()) / total
    per = {nm: round(sum(v) / len(v), 4) for nm, v in hits.items() if v}
    sims = max(len(v) for v in hits.values())
    ev = [
        _table("coverage_by_parameter", per, "How often each 90% interval held the known value"),
        _metric("simulations", float(sims)),
    ]
    status = check_status(check.check, coverage, check.thresholds)
    if status is CheckStatus.PASS:
        msg = (
            f"The model found the known values inside its 90% ranges {_pct(coverage)} of "
            f"the time over {sims} simulated datasets (target 90%)."
        )
        action = None
    else:
        narrow = coverage < 0.9
        msg = (
            f"The model's 90% ranges held the known values only {_pct(coverage)} of the time "
            f"(target 90%): its uncertainty is {'too narrow' if narrow else 'too wide'}."
        )
        action = (
            "The model is overconfident: check for missing structure (autocorrelation, "
            "hierarchy) and convergence problems."
            if narrow
            else "The model is too cautious: priors may be too wide or the model too flexible."
        )
    return result(
        check,
        coverage,
        msg,
        expert=f"Parametric bootstrap (SBC-lite): {sims} simulations of y = E[y] + block-"
        f"bootstrapped residuals; coverage of 90% intervals of {len(truth)} key parameters at "
        "the fitted values.",
        action=action,
        evidence=ev,
    )


def _convergence(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    s = convergence_stats(env.bundle)
    if env.kind == "bayesian":
        r = s.get("r_hat_max")
        if r is None:
            return _skip(check, "R-hat needs at least two chains with recorded draws")
        ev = [
            _metric("r_hat_max", r),
            _metric("ess_bulk_min", s.get("ess_bulk_min")),
            _metric("divergences", s.get("divergences")),
        ]
        status = check_status(check.check, r, check.thresholds)
        div = s.get("divergences") or 0.0
        if div > 0 and status is CheckStatus.PASS:
            status = CheckStatus.WARN
        if status is CheckStatus.PASS:
            msg = f"All chains agree (R-hat {r:.3f}): the fit settled on a stable answer."
        else:
            msg = (
                f"The chains disagree (R-hat {r:.3f})"
                + (f" and {int(div)} transitions diverged" if div else "")
                + ": running the fit again could give a "
                "different answer."
            )
        return result(
            check,
            r,
            msg,
            status=status,
            expert=f"max split R-hat {r:.4f}; min bulk ESS {s.get('ess_bulk_min')}; "
            f"divergences {div:g}.",
            action="Run more tuning and draws or reparameterise (non-centred priors); do not "
            "use the model until R-hat is below 1.01.",
            evidence=ev,
        )
    conv = s.get("converged")
    ev = [_metric(k_, v) for k_, v in s.items() if v is not None][:10]
    if conv == 0.0:
        return result(
            check,
            None,
            "The optimiser did not converge: the estimates are unreliable.",
            status=CheckStatus.FAIL,
            expert="The plugin reported converged=false.",
            action="Rescale the drivers, simplify the model or raise the iteration limit.",
            evidence=ev,
        )
    return result(
        check,
        1.0,
        "The fit reported no convergence problems.",
        expert="Frequentist/ML fit: converged flag "
        + ("true" if conv == 1.0 else "not reported (no iterative optimiser)")
        + ".",
        evidence=ev,
    )


_METRIC_PLAIN: Final = {
    "mape": "average percentage error",
    "smape": "symmetric percentage error",
    "mase": "error relative to a naive forecast",
    "rmse": "root mean squared error",
    "mae": "average absolute error",
}


def _accuracy(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    metric = str(check.params.get("metric", "mape"))
    value = _holdout_metric(env.bundle, metric)
    source = "the fit's holdout metrics"
    if value is None:
        trained = env.trained()
        mask = holdout_mask(env.bundle, env.data, trained)
        if mask is None:
            return _skip(check, f"the fit did not evaluate {metric} on held-out data")
        p = _first(
            predict(trained, env.data, env.registry, side_tables=env.side_tables),
            env.targets[0] if env.targets else None,
        )
        dim = str(mask.dims[0])
        obs = p.observed.where(mask, drop=True)
        pt = p.point.where(mask, drop=True)
        scale = None
        if metric == "mase":
            hist = p.observed.where(~mask, drop=True)
            other = [d for d in hist.dims if d != dim]
            h = np.asarray((hist.sum(other) if other else hist).values, dtype=float)
            m_ = env.season() or 1
            scale = float(np.mean(np.abs(h[m_:] - h[:-m_]))) if h.size > m_ else None
        value = error_metrics(np.asarray(obs.values), np.asarray(pt.values), scale=scale).get(
            metric
        )
        source = "recomputed on the held-out periods"
    if value is None:
        return _error(check, f"{metric} could not be computed on the held-out data")
    limit = check.thresholds.warn if check.thresholds else GOVERNANCE_CHECKS[check.check].warn
    shown = _pct(value) if metric in _PERCENT else f"{value:.3g}"
    lim = _pct(limit) if metric in _PERCENT else f"{limit:.3g}"
    plain = _METRIC_PLAIN.get(metric, metric)
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = f"On data it was not fitted on, the model's {plain} is {shown} (limit {lim})."
    else:
        msg = (
            f"On data it was not fitted on, the model's {plain} is {shown}, above the limit "
            f"of {lim}."
        )
    note = (
        ""
        if metric in _PERCENT or check.thresholds
        else (
            " The catalog's default limits are fractions; set limits in the KPI's units for "
            f"{metric}."
        )
    )
    return result(
        check,
        value,
        msg + note,
        expert=f"holdout {metric} = {value:.6g} ({source}).",
        action="Improve the model (drivers, seasonality, more history) or agree a realistic "
        "limit for this KPI.",
        evidence=[_metric(f"holdout_{metric}", value)],
    )


def _data_quality(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    max_missing = _pfloat(check.params, "max_missing", 0.05)
    min_hist = _pint(check.params, "min_history_periods", 52)
    c = data_context(env.data, env.schema)
    warnings_ = quality_warnings(c, max_missing=max_missing, min_history=min_hist)
    issues = [w for w in warnings_ if w.severity != "info"]
    codes = ", ".join(sorted({w.code for w in issues}))
    value = float(len(issues))
    ev = [
        EvidenceItem(kind="text", name=w.code, caption_plain=w.message_plain[:500])
        for w in issues[:30]
    ]
    periods = int(env.data.sizes[env.time_dim]) if env.time_dim else None
    if not issues:
        msg = "No data problems found" + (f" ({periods} periods of history)." if periods else ".")
        action = None
    else:
        msg = f"{len(issues)} data problem(s): " + " ".join(w.message_plain for w in issues[:3])
        action = issues[0].suggested_action
    return result(
        check,
        value,
        msg,
        expert=f"Quality warnings that are not info: {codes or 'none'}"
        f" (max_missing {max_missing:g}, min_history_periods {min_hist}).",
        action=action,
        evidence=ev,
    )


Impl = Callable[[GovernanceEnv, PolicyCheck], CheckResult]

CHECK_IMPLEMENTATIONS: Final[dict[str, Impl]] = {
    "null.permutation_target": _permutation,
    "null.noise_regressors": _noise,
    "null.placebo_time_shift": _placebo_shift,
    "baseline.beats_naive": _beats_naive,
    "residuals.whiteness": _whiteness,
    "stability.rolling_params": _stability,
    "plausibility.signs": _signs,
    "sensitivity.prior": _sensitivity,
    "recovery.simulated": _recovery,
    "convergence": _convergence,
    "holdout.accuracy": _accuracy,
    "data.quality": _data_quality,
}
"""Every check of the governance catalog (a test keeps the two in sync)."""


def measure(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    """One check, with applicability, budget exhaustion and failures turned into results."""
    why = check_applies(check.check, kind=env.kind, family=env.family, has=env.has())
    if why is not None:
        return _skip(check, why)
    impl = CHECK_IMPLEMENTATIONS[check.check]
    try:
        return impl(env, check)
    except BudgetExhausted as exc:
        return _error(check, f"{exc}; raise the budget or lower the check's sample sizes.")
    except Exception as exc:
        env.log("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:])
        return _error(
            check,
            "the model could not be refitted or scored for this check.",
            f"{type(exc).__name__}: {str(exc)[:1500]}",
        )


def run_governance(ctx: Any, job: GovernanceJob) -> GovernanceReport:
    """The ``governance`` run: load the bundle and its data, measure every check, write
    ``governance.json``."""
    from mapi_spec.jobs import INPUT_EVALUATION, INPUT_EVALUATION_MANIFEST

    from mapi_runner.worker.data import load_bundle, load_dataset, load_side_tables

    ctx.state(RunnerPhase.LOAD_INPUTS, "downloading the model and its data")
    bundle = load_bundle(ctx)
    data = load_dataset(ctx)
    tables = load_side_tables(ctx)
    evaluation = None
    if ctx.has_input(INPUT_EVALUATION):
        evaluation = load_bundle(ctx, INPUT_EVALUATION, INPUT_EVALUATION_MANIFEST)
    refitter = Refitter(bundle, ctx.registry, job.budget, side_tables=tables, cancel=ctx.cancel)
    env = GovernanceEnv(
        bundle,
        data,
        ctx.registry,
        refitter,
        evaluation=evaluation,
        seed=job.seed,
        side_tables=tables,
        log=ctx.log,
    )
    ctx.state(RunnerPhase.FIT, f"running {len(job.checks)} governance checks")
    results: list[CheckResult] = []
    for i, check in enumerate(job.checks):
        ctx.cancel.check()
        ctx.progress(
            i / len(job.checks), f"checking: {GOVERNANCE_CHECKS[check.check].title}", force=True
        )
        results.append(measure(env, check))
    report = GovernanceReport(
        model_ref=job.model_ref,
        model_id=bundle.model.manifest.id,
        family=env.family,
        model_kind=env.kind,
        results=results,
        refits=refitter.used,
    )
    ctx.write_json(GOVERNANCE_OUTPUT, report.model_dump(mode="json", exclude_none=True))
    ctx.progress(1.0, "governance checks complete", force=True)
    return report
