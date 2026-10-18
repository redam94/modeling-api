"""``governance`` runs (M3.5): measure a policy's checks on one model version in the sandbox.

Every check of :data:`mapi_spec.governance.GOVERNANCE_CHECKS` is implemented here and is
**model-family aware** (:func:`mapi_spec.governance.model_kind`):

========================== =================================================================
check                      how it is measured
========================== =================================================================
``null.permutation_target`` ``n_permutations`` refits on shuffled data (same budgeted
                           settings as the real refit); statistic: holdout skill against
                           naive on each dataset (scale-free); null: models with drivers keep
                           the result and get the drivers block-shuffled (trend, season and
                           controls alone cannot pass), models without drivers (or that ignore
                           them) get the ``d``-th differences of the result shuffled and
                           re-integrated (``d`` from the ARIMA order or ADF/KPSS);
                           ``p = (1 + #{shuffled >= real}) / (K + 1)``; without a time order,
                           the result shuffled across rows on the holdout metric
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
``sensitivity.prior``      Bayesian only: refit with every prior width the model uses (its
                           settings with the plugin's defaults and translated business
                           priors written out) multiplied by each of ``scales``; the largest
                           relative move of a key posterior mean
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
from mapi_workflows.runtime.models import fit_burn_in

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
_PRIOR_SCALE_KEYS: Final = frozenset({"sigma", "scale", "sd", "tau"})
"""Width parameters inside a prior subtree (``beta``/``b`` are Beta and Gamma rate or shape
parameters: scaling them moves the prior instead of widening it)."""
_PRIOR_WIDTH_KEY: Final = re.compile(r"(?:^|_)prior_(?:sigma|scale|sd)$|_sigma$")
"""Prior widths named as such anywhere in the settings (``seasonality.prior_sigma``,
``trend.growth_prior_sigma``, ``hierarchy.media_geo_sigma``)."""
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
        f"The check did not get a result: {why}",
        expert=expert,
        action="Look at the logs of the run. Correct the model or data problem, then submit again.",
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


class _NoPrediction(ValueError):
    """The model's predictions do not line up with a numeric target (choice models)."""


def _first(preds: list[Prediction], target: str | None = None) -> Prediction:
    for p in preds:
        if target is None or p.target == target:
            return p
    raise _NoPrediction("the model produced no prediction of its target")


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


def _integration_order(env: GovernanceEnv, y: np.ndarray) -> int:
    """How often the target is differenced under the null: the ``d`` of an ARIMA ``order``
    setting, else 1 when ADF keeps a unit root or KPSS rejects stationarity on the training
    periods (of the total series), else 0."""
    order = env.bundle.spec.model.plugin_settings.get("order")
    if isinstance(order, list | tuple) and len(order) == 3 and isinstance(order[1], int):
        return max(0, min(2, int(order[1])))
    total = np.nansum(y.reshape(y.shape[0], -1), axis=1)
    if stats.finite(total).size < 20:
        return 0
    adf = stats.adf_test(total)
    kpss = stats.kpss_test(total)
    verdict, _ = stats.stationarity_verdict(adf, kpss)
    return 1 if verdict == "difference" else 0


def _permute_increments(
    values: np.ndarray, axis: int, d: int, rng: np.random.Generator
) -> np.ndarray:
    """The ``d``-th differences of ``values`` along ``axis`` shuffled i.i.d. (the same
    permutation for every series) and integrated again from the original first ``d``
    values: the integration order and the distribution of the increments are kept, any
    predictable dynamics are destroyed."""
    x = np.moveaxis(np.asarray(values, dtype=float), axis, 0)
    n = x.shape[0]
    if d <= 0 or not np.isfinite(x).all() or n <= d + 1:
        return np.moveaxis(x[rng.permutation(n)], 0, axis)
    levels = [x]
    for _ in range(d):
        levels.append(np.diff(levels[-1], axis=0))
    out = levels[-1][rng.permutation(n - d)]
    # integrate back: the first value of each lower-order difference is kept
    for lvl in reversed(levels[:-1]):
        out = np.concatenate([lvl[:1], lvl[:1] + np.cumsum(out, axis=0)], axis=0)
    return np.moveaxis(out, 0, axis)


def _holdout_skill(
    b: Bundle, data: xr.Dataset, target: str, dim: str, held: np.ndarray, metric: str, d: int
) -> float | None:
    """``1 - L(model) / L(null forecast)`` on the held-out periods of ``data``: the refit's
    holdout metric against the forecast of the null model of each series - its last
    training value when it is integrated (``d >= 1``: a random walk), its training mean
    otherwise (white noise). Scale-free, so the shuffles are compared on skill rather than
    on the level jumps a shuffle creates."""
    model = _holdout_metric(b, metric)
    if model is None:
        return None
    da = data[target].transpose(dim, ...)
    y = np.asarray(da.values, dtype=float).reshape(da.sizes[dim], -1)
    train = y[~held]
    if train.shape[0] == 0 or not held.any():
        return None
    ref = []
    for j in range(y.shape[1]):
        hist = stats.finite(train[:, j])
        ref.append(np.nan if hist.size == 0 else (hist[-1] if d >= 1 else float(hist.mean())))
    bench = np.broadcast_to(np.asarray(ref), y[held].shape)
    key = "mae" if metric == "crps" else metric
    loss = error_metrics(y[held], bench).get(key)
    if loss is None or loss <= 0:
        return None
    return 1.0 - model / loss


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
    k = min(k, env.refitter.remaining - 1)
    if k < 5:
        return _error(check, "the refit budget has space for fewer than 5 shuffled fits")
    spec = env.refitter.spec(metrics=[metric])
    real_b = env.refitter.fit(env.data, spec)
    target = env.targets[0]
    rng = env.rng(check.check)
    held = None
    if env.time_dim is not None:
        hm = holdout_mask(real_b, env.data)
        held = (
            np.asarray(hm.values, dtype=bool) if hm is not None and str(hm.dims[0]) == dim else None
        )

    if held is not None:
        # time series: a scale-free statistic (skill against naive on each dataset) and a
        # null that matches what is tested (see the module docstring)
        drivers = [v for v in env.drivers() if dim in env.data[v].dims]
        block = _pint(p, "block", 0) or max(1, n // 20)
        d = _integration_order(
            env, np.asarray(env.data[target].transpose(dim, ...).values, dtype=float)[~held]
        )

        def statistic(b: Bundle, data: xr.Dataset) -> float | None:
            assert held is not None
            return _holdout_skill(b, data, target, dim, held, metric, d)

        def shuffle_drivers() -> xr.Dataset:
            perm = stats.block_permutation(n, block, rng)  # one permutation for all drivers
            return env.data.assign(
                {
                    v: env.data[v].copy(
                        data=np.take(env.data[v].values, perm, axis=env.data[v].get_axis_num(dim))
                    )
                    for v in drivers
                }
            )

        def shuffle_target() -> xr.Dataset:
            return env.data.assign(
                {
                    t: env.data[t].copy(
                        data=_permute_increments(
                            env.data[t].values, env.data[t].get_axis_num(dim), d, rng
                        )
                    )
                    for t in env.targets
                    if dim in env.data[t].dims
                }
            )

        null = "drivers" if drivers else "target"
        how = (
            f"the drivers {', '.join(drivers[:5])} shuffled in blocks of {block} (no change "
            "to the result)"
            if drivers
            else f"the result's {_ORDINAL.get(d, f'{d}th')} shuffled"
        )
    else:
        # rows without a time order: the classic permutation test on the error itself
        def statistic(b: Bundle, data: xr.Dataset) -> float | None:
            s = _holdout_metric(b, metric)
            return None if s is None else -s

        null, how, block, d = "target", "the result shuffled across rows", 1, 0

        def shuffle_target() -> xr.Dataset:
            perm = rng.permutation(n)
            return env.data.assign(
                {
                    t: env.data[t].copy(
                        data=np.take(env.data[t].values, perm, axis=env.data[t].get_axis_num(dim))
                    )
                    for t in env.targets
                    if dim in env.data[t].dims
                }
            )

        def shuffle_drivers() -> xr.Dataset:  # pragma: no cover - never used without time
            return shuffle_target()

    real = statistic(real_b, env.data)
    if real is None:
        return _error(
            check, f"the refit has no holdout {metric}", real_b.metrics.skipped.get(metric)
        )
    scores: list[float] = []
    for i in range(k):
        shuffled = shuffle_drivers() if null == "drivers" else shuffle_target()
        try:
            b = env.refitter.fit(shuffled, spec)
        except BudgetExhausted:
            break
        s = statistic(b, shuffled)
        if s is None:
            continue
        if i == 0 and null == "drivers" and abs(s - real) <= 1e-9 * (1.0 + abs(real)):
            # the model does not use these drivers: test its dynamics instead
            null, how = "target", f"the result's {_ORDINAL.get(d, f'{d}th')} shuffled"
            continue
        scores.append(s)
    if len(scores) < 5:
        return _error(check, "fewer than 5 shuffled fits gave a score")
    as_good = sum(1 for s in scores if s >= real)
    pval = (1 + as_good) / (len(scores) + 1)
    timed = held is not None

    def shown(v: float) -> str:
        return f"skill {v:.3f}" if timed else f"{metric} {-v:.3g}"

    ev = [
        _metric(
            "holdout_skill_vs_naive" if timed else f"holdout_{metric}",
            real if timed else -real,
            "The skill of the real model on holdout data against the null forecast (the last "
            "value of an integrated series, or else its mean)"
            if timed
            else "The error of the real model on holdout data",
        ),
        _table(
            "permutation_scores",
            {
                "real": real if timed else -real,
                "shuffled": [round(s if timed else -s, 6) for s in scores],
                "shuffled_median": float(np.median(scores)) * (1 if timed else -1),
                "statistic": f"skill vs the null forecast ({metric})" if timed else metric,
                "axis": (
                    f"Skill on holdout data ({metric.upper()} against the null forecast, "
                    "higher is better)"
                    if timed
                    else f"{metric.upper()} on holdout data (lower is better)"
                ),
                "null": how,
                "block": block,
                "d": d,
                "metric": metric,
            },
            "Scores of the fits on shuffled data",
        ),
    ]
    if as_good == 0:
        msg = (
            f"The model was better on holdout data ({shown(real)}) than all {len(scores)} "
            f"fits with {how} (p = {pval:.3f})."
        )
        if len(scores) < 19:
            msg += (
                f" With {len(scores)} shuffles, the smallest possible p is {pval:.3f}. "
                "Use 19 or more shuffles for a 5% test."
            )
    else:
        msg = (
            f"{as_good} of {len(scores)} fits with {how} were as good as the real model "
            f"(p = {pval:.3f}). The model possibly fits noise, not signal."
        )
    return result(
        check,
        pval,
        msg,
        expert=f"Permutation test, null: {how}; statistic: "
        + (
            f"holdout skill 1 - {metric}(model)/{metric}(null forecast) on each dataset"
            if timed
            else f"holdout {metric}"
        )
        + f"; real {shown(real)}, shuffled median {shown(float(np.median(scores)))}; "
        f"p = (1 + {as_good}) / ({len(scores)} + 1).",
        action="Look for leakage between the training data and the test data. Make the model "
        "simpler, or add drivers that have real signal.",
        evidence=ev,
    )


_ORDINAL: Final = {0: "values", 1: "period-to-period changes", 2: "second differences"}


@dataclass
class _Placebo:
    data: xr.Dataset
    schema: DataSchema
    spec: ModelSpec
    names: list[str]
    how: str
    ablate: Callable[[xr.Dataset, str], xr.Dataset]
    params_like: Callable[[str], bool]
    ablate_real: Callable[[xr.Dataset], xr.Dataset] | None = None
    """Every real driver switched off (the denominator of the placebo's share)."""


_PRIOR_KEYS: Final = frozenset(
    {"priors", "prior", "business_priors", "calibration", "lift_tests", "experiments"}
)
"""Settings subtrees that hold beliefs about the real channels (priors, calibration): never
copied to a placebo, which gets the platform's default (weakly informative) prior."""


def _walk_settings(
    settings: Any, existing: set[str], extra: Sequence[str], source: str, *, beliefs: bool = False
) -> Any:
    """Append ``extra`` names to lists that enumerate ``existing`` names, and copy the
    ``source`` entry of mappings keyed by them (per-channel carry-over and saturation
    shapes) - except under :data:`_PRIOR_KEYS`: a placebo must not inherit a real channel's
    informative prior or calibration."""
    if beliefs:
        return settings
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
        out = {
            k: _walk_settings(v, existing, extra, source, beliefs=str(k).lower() in _PRIOR_KEYS)
            for k, v in settings.items()
        }
        if keys and keys <= existing and source in settings:
            for e in extra:
                out[e] = settings[source]
        return out
    return settings


def _holm_significant(p_values: Mapping[str, float | None], alpha: float = 0.05) -> list[str]:
    """The placebos whose p-value is significant at ``alpha`` after Holm's step-down
    correction for the number of placebos (family-wise error rate ``alpha``)."""
    ranked = sorted(
        ((n, float(p)) for n, p in p_values.items() if p is not None and math.isfinite(p)),
        key=lambda x: x[1],
    )
    m = len(ranked)
    out: list[str] = []
    for i, (name, p) in enumerate(ranked):
        if p * (m - i) >= alpha:
            break
        out.append(name)
    return out


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

        def ablate_channels(ds: xr.Dataset) -> xr.Dataset:
            out = ds
            for label in labels:
                out = ablate(out, label)
            return out

        return _Placebo(
            extended,
            schema,
            spec,
            names,
            f"channels on {dim!r}",
            ablate,
            lambda s: any(n in s for n in names),
            ablate_channels,
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
        vars_[nm] = (
            spec_src.model_copy(update={"dtype": "float64", "units": None})
            if spec_src is not None
            else VarSpec(
                dims=tuple(str(d) for d in src_da.dims), dtype="float64", role=Role.COVARIATE
            )
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

    def ablate_real(ds: xr.Dataset) -> xr.Dataset:
        out = ds
        for v in drivers:
            out = _ablate(env, out, v)
        return out

    return _Placebo(
        data.assign(added),
        new_schema,
        spec,
        names,
        "variables",
        ablate_var,
        lambda s: any(s == n or s.startswith(n + "[") for n in names),
        ablate_real,
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
    warn_at = check.thresholds.warn if check.thresholds else GOVERNANCE_CHECKS[check.check].warn
    # the share is taken of the real drivers' total effect (so the limit does not depend on
    # how large the baseline is); of the whole result when the drivers' effect is unknown
    real_num, real_draws = 0.0, None
    if placebo.ablate_real is not None:
        abl_real = _first(
            predict(b, placebo.ablate_real(placebo.data), env.registry,
                    side_tables=env.side_tables),
            env.targets[0],
        )  # fmt: skip
        real_num, _d, real_draws = _total_effect(full, abl_real)
    of_drivers = real_num > 0
    shares: dict[str, float] = {}
    intervals: dict[str, list[float | None]] = {}
    material: dict[str, float] = {}
    for nm in placebo.names:
        abl = _first(
            predict(b, placebo.ablate(placebo.data, nm), env.registry, side_tables=env.side_tables),
            env.targets[0],
        )
        num, den, per_draw = _total_effect(full, abl)
        ref = real_num if of_drivers else den
        shares[nm] = num / ref if ref > 0 else 0.0
        if per_draw is not None and per_draw.size > 1:
            lo, hi = np.quantile(per_draw, [0.05, 0.95])
            intervals[nm] = [_fv(lo), _fv(hi)]
            # practical equivalence, per posterior draw: an effect with a sign constraint
            # (an MMM's positive channel effects) never has an interval covering 0, so the
            # question is whether the placebo's share is above the limit, not above 0
            if of_drivers and real_draws is not None and real_draws.size == per_draw.size:
                ref_d = np.abs(real_draws)
            else:
                ref_d = np.full(per_draw.size, ref)
            with np.errstate(divide="ignore", invalid="ignore"):
                share_d = np.where(ref_d > 0, np.abs(per_draw) / ref_d, 0.0)
            material[nm] = float(np.mean(share_d > warn_at))
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
            "the model has a fixed set of drivers and did not use the false drivers. "
            "To run this check, add a noise driver to the settings of the model",
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
    # Bayesian: the placebo's share is above the limit with 90% posterior probability
    clear = [n for n, pr in material.items() if pr >= 0.9]
    # frequentist: significant after Holm's correction for the number of placebos
    sig = _holm_significant(p_values)
    of_what = "the effect of the real drivers" if of_drivers else "the result"
    ev = [
        _table(
            "noise_effect_share",
            {k_: round(v, 6) for k_, v in shares.items()},
            f"The share of {of_what} that the model gave to each false driver",
        ),
        _table(
            "placebo",
            {
                "how": placebo.how,
                "kind": kind,
                "names": placebo.names,
                "share_of": "real drivers' effect" if of_drivers else "result",
                "prior": "the default prior of the model (the check does not copy the priors "
                "and the calibration of the real channels)",
            },
        ),
    ]
    if intervals:
        ev.append(
            _table(
                "noise_effect_interval_90",
                intervals,
                "The 90% interval of the total effect of each false driver",
            )
        )
    if material:
        ev.append(
            _table(
                "noise_share_prob_material",
                {k_: round(v, 4) for k_, v in material.items()},
                f"The posterior probability that the share of a false driver is more than "
                f"{_pct(warn_at)}",
            )
        )
    if p_values:
        ev.append(
            _table(
                "noise_p_values",
                p_values,
                "The p-values of the estimates of the false drivers (with Holm's correction)",
            )
        )
    status = None
    if clear or sig:
        status = (
            CheckStatus.FAIL
            if check_status(check.check, value, check.thresholds) is CheckStatus.FAIL
            else CheckStatus.WARN
        )
    if value <= warn_at and not (clear or sig):
        msg = (
            f"{len(shares)} false drivers of only noise got a maximum of {_pct(value)} of "
            f"{of_what}. The model gives no credit to drivers that have no signal."
        )
    else:
        extra = ""
        if clear:
            extra = (
                f" {', '.join(clear)} got more than {_pct(warn_at)} with a probability of 90% "
                "or more."
            )
        elif sig:
            extra = f" {', '.join(sig)} is statistically significant."
        msg = (
            f"A false driver of only noise ({worst}) got {_pct(value)} of {of_what}. The "
            f"model gives credit to drivers too easily.{extra}"
        )
    return result(
        check,
        value,
        msg,
        status=status,
        expert=f"{len(shares)} placebo {placebo.how} ({kind}) with the model's default prior; "
        "effect share = sum|y(full) - y(placebo off)| / sum|y(full) - y(real drivers off)| "
        "through the score path (of sum|y(full)| when the drivers' effect is unknown); max "
        "over placebos. Bayesian: flagged when P(share > warn limit) >= 0.9 per posterior "
        "draw (practical equivalence; sign-constrained effects never cover 0). "
        "Frequentist: placebo p-values Holm-corrected.",
        action="Use stronger regularisation or priors, fewer drivers or more data. Then "
        "trust how the model divides the result between drivers.",
        evidence=ev,
    )


def _coef_effect(env: GovernanceEnv, b: Bundle, data: xr.Dataset, var: str) -> float | None:
    """``|beta| * sum |x - x_off|`` from the coefficient named after driver ``var`` (``None``
    without one): the effect of a driver on a model that is linear in it, for plugins whose
    in-sample predictions follow the stored history rather than the scored drivers (the
    statsmodels models)."""
    names = {var, b.binding.to_canonical(var)}

    def named(label: str) -> bool:  # ``price`` or a panel cell's ``north/price``
        return label in names or label.rsplit("/", 1)[-1] in names

    betas = [abs(e.estimate) for v, _c, e in _estimates(b) if named(e.name) or named(v)]
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
            "The effect after the drivers move, as a share of the real effect, for each shift",
        ),
        _table("drivers", drivers),
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = (
            f"A move of the drivers by {abs(int(worst))} periods keeps only {_pct(value)} of "
            "their effect. The effect has a connection to the dates of the drivers."
        )
    else:
        msg = (
            f"The model still finds {_pct(value)} of the effect after the drivers move by "
            f"{abs(int(worst))} periods. The model finds time patterns (trend, season), not "
            "the drivers."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Circular time-shift placebo of {len(drivers)} driver(s) by {shifts}; ratio "
        "of the placebo fit's total ablation effect to the real fit's, maximised over shifts.",
        action="Add trend and seasonality controls. Or look for drivers that only follow the "
        "calendar.",
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
        return _skip(check, "the model has no holdout periods at the end of its data")
    t = env.time_dim
    preds = predict(trained, env.data, env.registry, side_tables=env.side_tables)
    p = _first(preds, env.targets[0] if env.targets else None)
    mask = holdout_mask(env.bundle, p.observed, trained)  # on the model's periods
    if mask is None:
        return _skip(check, "the model has no holdout periods at the end of its data")
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
        return _skip(check, "the training history does not give a benchmark")
    best = min(losses, key=lambda m: losses[m])
    if losses[best] == 0:
        return _error(
            check,
            f"the benchmark '{_METHOD_PLAIN.get(best, best)}' is perfect on the holdout data",
        )
    skill = 1.0 - l_model / losses[best]
    scale = float(np.mean(scale_parts)) if scale_parts else None
    mae = _loss(model_err, "mae")
    mase = mae / scale if scale and mae is not None else None
    ev = [
        _table("losses", {"model": l_model, **losses, "metric": metric, "held_out_periods": h}),
        _metric("mase", mase, "The error relative to the in-sample seasonal-naive error"),
    ]
    plain = _METHOD_PLAIN.get(best, best)
    if skill >= 0:
        msg = (
            f"On the {h} holdout periods, the error of the model is {_pct(skill)} lower than "
            f"'{plain}' (the best simple benchmark)."
        )
    else:
        msg = (
            f"On the {h} holdout periods, the model is {_pct(-skill)} worse than "
            f"'{plain}'. A simple rule gives a better forecast."
        )
    return result(
        check,
        skill,
        msg,
        expert=f"Relative skill 1 - {metric}(model)/{metric}({best}) = {skill:.4f} over {h} "
        f"periods x {obs.shape[1]} series; MASE {mase if mase is None else round(mase, 4)}.",
        action="Do a check of the drivers and the seasonality. Do not use a model for "
        "forecasts if a naive forecast is better.",
        evidence=ev,
    )


def _arma_df(settings: Mapping[str, Any], bundle: Bundle | None = None) -> int:
    """The Ljung-Box degrees-of-freedom correction: the ARMA terms the plugin stored per
    series (``learned.cells[].model_df``, the statsmodels plugin), else ``p + q (+ P + Q)``
    of an ``order``/``seasonal_order`` setting."""
    if bundle is not None:
        cells = (bundle.model.learned or {}).get("cells")
        stored: list[int] = []
        for c in cells if isinstance(cells, list) else []:
            v = c.get("model_df") if isinstance(c, dict) else None
            if isinstance(v, int):
                stored.append(v)
        if stored:
            return max(stored)
    df = 0
    order = settings.get("order")
    if (
        isinstance(order, list | tuple)
        and len(order) == 3
        and all(isinstance(x, int) for x in order)
    ):
        df += int(order[0]) + int(order[2])
    seasonal = settings.get("seasonal_order")
    if (
        isinstance(seasonal, list | tuple)
        and len(seasonal) == 4
        and all(isinstance(x, int) for x in seasonal)
    ):
        df += int(seasonal[0]) + int(seasonal[2])
    return df


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
    model_df = _arma_df(settings, env.bundle)
    # the start-up residuals of a diffuse-initialised fit (the first is the level itself
    # for an integrated or unobserved-components model): one such outlier swamps every
    # autocorrelation and lets patterned errors pass
    burn = max(_burn_in(settings), fit_burn_in(env.bundle))
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
        return _skip(check, "the residual series are too short or do not change")
    worst = min(rows, key=lambda x: x[1])
    value = worst[1]
    ev = [
        _table(
            "ljung_box",
            {
                name: {"p_value": round(pv, 6), "q": round(q, 4)}
                for name, pv, q in sorted(rows, key=lambda x: x[1])[:20]
            },
            f"Ljung-Box at {lags} lags for each series (worst first)",
        )
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = f"No pattern is left in the errors (Ljung-Box p = {value:.2f} at {lags} lags)."
    else:
        msg = (
            f"The errors still have a pattern over time (Ljung-Box p = {value:.3f}). "
            "The model does not find all of the structure."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Minimum Ljung-Box p over {len(rows)} series, lags={lags}, model_df={model_df}"
        f" (first {burn} start-up residual(s) of the differencing left out).",
        action="Add lags, seasonality or missing drivers. Look at the residual ACF in the "
        "Model Explorer.",
        evidence=ev,
    )


def _stability(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    assert env.time_dim is not None
    base = _estimates(env.bundle)
    raw = check.params.get("params")
    wanted = [str(x) for x in raw] if isinstance(raw, list) else None
    key = _key_params(base, wanted)
    if not key:
        return _skip(check, "the model has no parameters that you can explain and compare")
    n = int(env.data.sizes[env.time_dim])
    window = _pint(check.params, "window", 0) or max(8, n // 2)
    step = _pint(check.params, "step", 4)
    if window >= n:
        return _skip(check, f"the window ({window} periods) is not shorter than the history ({n})")
    starts = list(range(0, n - window + 1, max(1, step)))
    if len(starts) > 8:
        starts = [starts[int(i)] for i in np.linspace(0, len(starts) - 1, 8).round()]
    if len(starts) < 2:
        return _skip(check, "the history has space for only one window")
    spec = env.refitter.spec(split=None)
    names = [e.name for e in key]
    per: dict[str, list[float]] = {nm: [] for nm in names}
    used = 0
    failed: list[str] = []
    for s in starts:
        part = env.data.isel({env.time_dim: slice(s, s + window)})
        try:
            b = env.refitter.fit(part, spec)
        except BudgetExhausted:
            break
        except Exception as exc:  # a window the model cannot be fitted on (too short, ...)
            failed.append(f"{type(exc).__name__}: {str(exc)[:300]}")
            continue
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
        if failed:
            return _error(
                check,
                f"the check could not fit the model again on windows of {window} periods. "
                "Give a longer 'window' in the parameters of the check",
                failed[0],
            )
        return _error(check, "the check could not estimate a parameter in two windows")
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
        msg = (
            f"The key estimates change by a maximum of {_pct(value)} in {used} windows of history."
        )
    else:
        msg = (
            f"{worst} changes much between windows of history (variation {_pct(value)}). The "
            "relationship is possibly not stable over time."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Coefficient of variation across {used} rolling refits (window {window}, step "
        f"{step}), relative to max(|mean|, the full fit's standard error); max over "
        f"{len(variation)} parameters.",
        action="Look for a structural break (CUSUM or Chow tests in a workflow). Or use a "
        "shorter history, or let the effect change over time.",
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
                "the estimates of the model do not include the expected "
                f"parameters ({', '.join(map(str, expected))})",
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
                    "no expected directions. Give them in the 'expected' parameter of the "
                    "check (no media or spend drivers are positive by default)",
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
        _table("estimates", estimates, "The estimated effect (total contribution or parameter)"),
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = f"Each effect has the expected direction ({how})."
    else:
        msg = (
            f"{worst} has the wrong direction. {_pct(value)} of its estimate"
            f"{' (uncertainty)' if env.kind != 'ml' else ''} is on the wrong side."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Share of the posterior (or sampling distribution) on the wrong side, max over "
        f"{len(shares)} effects ({how}).",
        action="Do a check of the data of the driver (units, sign). Add the missing "
        "confounder, or use a prior that keeps the effect positive.",
        evidence=ev,
    )


def _scale_priors(node: Any, factor: float, under_prior: bool = False) -> tuple[Any, int]:
    """Multiply prior widths: ``sigma``, ``scale``, ``sd`` and ``tau`` inside any settings
    subtree whose key mentions ``prior``, and keys named as prior widths anywhere
    (:data:`_PRIOR_WIDTH_KEY`)."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        n = 0
        for k, v in node.items():
            inside = under_prior or "prior" in str(k).lower()
            width = (inside and str(k) in _PRIOR_SCALE_KEYS) or bool(
                _PRIOR_WIDTH_KEY.search(str(k))
            )
            if width and isinstance(v, int | float) and not isinstance(v, bool) and v > 0:
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


def _deep_fill(base: dict[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """``base`` with ``defaults`` filled in where ``base`` has no value."""
    out = dict(base)
    for k, v in defaults.items():
        if out.get(k) is None:
            out[k] = v
        elif isinstance(out[k], dict) and isinstance(v, Mapping):
            out[k] = _deep_fill(out[k], v)
    return out


def _prior_settings(
    model_cls: Any, plugin_settings: Mapping[str, Any], learned: Mapping[str, Any]
) -> dict[str, Any]:
    """The model's settings with every prior it uses written out: the plugin's ``Settings``
    defaults (``seasonality.prior_sigma``, ``trend.growth_prior_sigma``, ...), the priors it
    declares in ``prior_scale_defaults`` for values its engine fills in itself (mmm's
    ``priors.roi_default``), and business priors the fit translated into ``priors``
    (``learned.calibration.translated_priors``)."""
    base: dict[str, Any] = dict(plugin_settings)
    settings_cls = getattr(model_cls, "Settings", None)
    if settings_cls is not None and hasattr(settings_cls, "model_validate"):
        try:
            base = settings_cls.model_validate(base).model_dump(mode="json", exclude_none=True)
        except Exception:
            base = dict(plugin_settings)
    defaults = getattr(model_cls, "prior_scale_defaults", None)
    if isinstance(defaults, Mapping):
        base = _deep_fill(base, defaults)
    cal = learned.get("calibration")
    translated = cal.get("translated_priors") if isinstance(cal, Mapping) else None
    if isinstance(translated, Mapping) and translated:
        priors = dict(base.get("priors") or {})
        for k, v in translated.items():
            if k == "roi" and isinstance(v, Mapping) and isinstance(priors.get("roi"), Mapping):
                priors["roi"] = {**priors["roi"], **v}
            else:
                priors[k] = v
        base["priors"] = priors
    return base


def _sensitivity(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    raw = check.params.get("scales", [0.5, 2.0])
    scales = (
        [float(s) for s in raw if isinstance(s, int | float)]
        if isinstance(raw, list)
        else [0.5, 2.0]
    )
    man = env.bundle.model.manifest
    try:
        model_cls: Any = env.registry.model(f"{man.id}@{man.version}")
    except Exception:
        model_cls = None
    learned = env.bundle.model.learned or {}
    settings = _prior_settings(model_cls, env.bundle.spec.model.plugin_settings, learned)
    _probe, found = _scale_priors(settings, 1.0)
    if found == 0:
        return _skip(
            check,
            "the settings of the model do not set its priors, so the check cannot "
            "change them automatically",
        )
    # business priors translated at fit time are written out above: refit without
    # translating them again, or the translation would overwrite the scaled priors
    cal = env.bundle.spec.calibration
    drop_beliefs = (
        cal is not None
        and cal.business_priors is not None
        and isinstance(learned.get("calibration"), Mapping)
    )

    def refit_spec(s: Mapping[str, Any]) -> ModelSpec:
        spec = env.refitter.spec(split=None, settings=s)
        if drop_beliefs and spec.calibration is not None:
            plain = spec.calibration.model_copy(update={"business_priors": None})
            spec = spec.model_copy(update={"calibration": plain})
        return spec

    spec0 = refit_spec(settings)
    b0 = env.refitter.fit(env.data, spec0)
    base = {e.name: e for e in _key_params(_estimates(b0), None)}
    if not base:
        return _skip(check, "the model has no key parameters to compare")
    moves: dict[str, dict[str, float]] = {}
    for s in scales:
        scaled, _n = _scale_priors(settings, s)
        try:
            b = env.refitter.fit(env.data, refit_spec(scaled))
        except BudgetExhausted:
            break
        got = {e.name: e for _v, _c, e in _estimates(b)}
        for nm, e0 in base.items():
            if nm in got:
                denom = max(abs(e0.estimate), e0.sd or 0.0, 1e-12)
                moves.setdefault(nm, {})[str(s)] = abs(got[nm].estimate - e0.estimate) / denom
    if not moves:
        return _error(check, "no fit with changed priors could run")
    flat = [(nm, sc, v) for nm, per in moves.items() for sc, v in per.items()]
    nm, sc, value = max(flat, key=lambda x: x[2])
    ev = [
        _table(
            "relative_change",
            {k_: {s_: round(v, 6) for s_, v in per.items()} for k_, per in moves.items()},
            "The relative change of each key estimate for each prior scale",
        ),
        _metric("prior_parameters_scaled", float(found)),
    ]
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = (
            f"Starting assumptions that are {min(scales):g}x to {max(scales):g}x as wide "
            f"move the key estimates by a maximum of {_pct(value)}. The estimates come from "
            "the data."
        )
    else:
        msg = (
            f"{nm} moves by {_pct(value)} when the starting assumptions change by a scale of "
            f"{sc}. The data does not set its value."
        )
    return result(
        check,
        value,
        msg,
        expert=f"Refit-based prior sensitivity: {found} prior scale parameter(s) multiplied by "
        f"{scales}; |mean_s - mean_0| / max(|mean_0|, sd_0), max over parameters.",
        action="Get data with more information (experiments, lift tests), or show evidence "
        "for the prior. Then use these estimates.",
        evidence=ev,
    )


_DYNAMIC_FAMILIES: Final = frozenset({"forecast", "ts_multivariate", "regime"})


def _recovery(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    if not env.targets:
        return _skip(check, "the model has no target to simulate")
    if env.family in _DYNAMIC_FAMILIES and env.kind != "bayesian":
        return _skip(
            check,
            "the model predicts each period from the past of the series. Data that the "
            "check simulates around the fitted values does not follow the model (use the "
            "backtest and residual checks for this model)",
        )
    raw = check.params.get("params")
    wanted = [str(x) for x in raw] if isinstance(raw, list) else None
    truth = [
        e
        for e in _key_params(_estimates(env.bundle), wanted)
        if e.lower is not None or e.draws is not None
    ]
    if not truth:
        return _skip(check, "the model gives no parameter intervals for the check")
    n_sims = min(_pint(check.params, "n_sims", 20), env.refitter.remaining)
    if n_sims < 3:
        return _error(check, "the refit budget has space for fewer than 3 simulations")
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
        return _error(check, "no simulated refit gave parameter intervals")
    coverage = sum(sum(v) for v in hits.values()) / total
    per = {nm: round(sum(v) / len(v), 4) for nm, v in hits.items() if v}
    sims = max(len(v) for v in hits.values())
    ev = [
        _table(
            "coverage_by_parameter", per, "How often each 90% interval contained the known value"
        ),
        _metric("simulations", float(sims)),
    ]
    status = check_status(check.check, coverage, check.thresholds)
    if status is CheckStatus.PASS:
        msg = (
            f"The 90% ranges of the model contained the known values {_pct(coverage)} of "
            f"the time in {sims} simulated datasets (target 90%)."
        )
        action = None
    else:
        narrow = coverage < 0.9
        msg = (
            f"The 90% ranges of the model contained the known values only {_pct(coverage)} "
            f"of the time (target 90%). Its uncertainty is "
            f"{'too narrow' if narrow else 'too wide'}."
        )
        action = (
            "The model is too confident. Look for missing structure (autocorrelation, "
            "hierarchy) and convergence problems."
            if narrow
            else "The model is not confident enough. The priors are possibly too wide, or "
            "the model is too flexible."
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
            return _skip(check, "R-hat needs two or more chains with recorded draws")
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
            msg = f"All chains agree (R-hat {r:.3f}). The fit found a stable answer."
        else:
            msg = (
                f"The chains do not agree (R-hat {r:.3f})"
                + (f" and {int(div)} transitions diverged" if div else "")
                + ". If you fit the model again, you can get a "
                "different answer."
            )
        return result(
            check,
            r,
            msg,
            status=status,
            expert=f"max split R-hat {r:.4f}; min bulk ESS {s.get('ess_bulk_min')}; "
            f"divergences {div:g}.",
            action="Use more tuning steps and draws, or reparameterise (non-centred priors). "
            "Do not use the model until R-hat is below 1.01.",
            evidence=ev,
        )
    conv = s.get("converged")
    ev = [_metric(k_, v) for k_, v in s.items() if v is not None][:10]
    if conv == 0.0:
        return result(
            check,
            None,
            "The optimiser did not converge. The estimates are not reliable.",
            status=CheckStatus.FAIL,
            expert="The plugin reported converged=false.",
            action="Change the scale of the drivers, make the model simpler or increase the "
            "iteration limit.",
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
    "mape": "mean percentage error",
    "smape": "symmetric percentage error",
    "mase": "error relative to a naive forecast",
    "rmse": "root mean squared error",
    "mae": "mean absolute error",
}


def _accuracy(env: GovernanceEnv, check: PolicyCheck) -> CheckResult:
    metric = str(check.params.get("metric", "mape"))
    value = _holdout_metric(env.bundle, metric)
    source = "the fit's holdout metrics"
    if value is None:
        trained = env.trained()
        mask = holdout_mask(env.bundle, env.data, trained)
        if mask is None:
            return _skip(check, f"the fit did not evaluate {metric} on holdout data")
        p = _first(
            predict(trained, env.data, env.registry, side_tables=env.side_tables),
            env.targets[0] if env.targets else None,
        )
        mask = holdout_mask(env.bundle, p.observed, trained)  # on the model's periods
        if mask is None:
            return _skip(check, f"the fit did not evaluate {metric} on holdout data")
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
        source = "calculated again on the holdout periods"
    if value is None:
        return _error(check, f"the check could not calculate {metric} on the holdout data")
    limit = check.thresholds.warn if check.thresholds else GOVERNANCE_CHECKS[check.check].warn
    shown = _pct(value) if metric in _PERCENT else f"{value:.3g}"
    lim = _pct(limit) if metric in _PERCENT else f"{limit:.3g}"
    plain = _METRIC_PLAIN.get(metric, metric)
    if check_status(check.check, value, check.thresholds) is CheckStatus.PASS:
        msg = (
            f"On data that the fit did not use, the {plain} of the model is {shown} (limit {lim})."
        )
    else:
        msg = (
            f"On data that the fit did not use, the {plain} of the model is {shown}. This "
            f"is more than the limit of {lim}."
        )
    note = (
        ""
        if metric in _PERCENT or check.thresholds
        else (
            " The default limits of the catalog are fractions. Set limits in the units of "
            f"the KPI for {metric}."
        )
    )
    return result(
        check,
        value,
        msg + note,
        expert=f"holdout {metric} = {value:.6g} ({source}).",
        action="Make the model better (drivers, seasonality, more history). Or agree on a "
        "realistic limit for this KPI.",
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
        msg = "The check found no data problems" + (
            f" ({periods} periods of history)." if periods else "."
        )
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
        return _error(
            check, f"{exc}. Increase the budget, or decrease the sample sizes of the check."
        )
    except _NoPrediction:
        return _skip(
            check,
            "the predictions of the model are not numbers for its target (a choice or "
            "classification model). This check compares numbers",
        )
    except Exception as exc:
        env.log("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:])
        return _error(
            check,
            "the check could not fit or score the model again.",
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
