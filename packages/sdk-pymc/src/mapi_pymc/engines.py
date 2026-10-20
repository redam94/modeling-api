"""Inference engines for :class:`~mapi_pymc.PyMCModel` (plan §5).

``fit.engine`` picks one of:

==============  ==========================================================================
``nutpie``      nutpie NUTS (default when installed). Fast, parallel chains. Not bit-for-bit
                reproducible from a seed. Checkpoints are *snapshots* (draws so far): nutpie
                cannot export its adaptation, so a retry samples again from the start.
``pymc``        PyMC NUTS with **segmented sampling**: tune once, then draw in chunks of
                ``fit.checkpoint.every_draws``. After each chunk a resumable checkpoint holds
                the draws, every chain's position and the full sampler state; a retry
                resumes exactly where the last chunk ended. Chains run in this process one
                after another (models with discrete variables fall back to ``pm.sample``
                without segments).
``numpyro``     PyMC's external NUTS bridges, when the library is installed. No segments.
``blackjax``
``map``         ``pm.find_MAP`` - one "draw" (chain 1, draw 1).
``advi``        mean-field ADVI; ``fit.tune`` is the number of optimisation steps (default
                10,000) and ``fit.draws`` the number of draws from the approximation.
``pathfinder``  pymc-extras' Pathfinder when pymc-extras is installed (extra ``pathfinder``:
                ``mapi-pymc[pathfinder]``). One chain; no progress table on stdout.
==============  ==========================================================================

Every engine reports progress through one callback ``(fraction, message)`` where the
message names the chain (``"chain 2/4: sampling 150/500, 0 divergences"``).

**Tracking.** When :attr:`EngineHooks.tracker` is set (:meth:`EngineHooks.for_context` sets it
when the run records metrics), the engines also report every chain's progress
(``nutpie``, ``pymc``) and step metrics: ``step_size``, ``num_steps`` and cumulative
``divergences`` per chain (label ``chain``; step = draws done, tuning included; the ``pymc``
engine adds ``tree_depth`` and logs every k-th draw so that a chain has at most
:data:`MAX_HISTORY_POINTS` points per metric), and the ADVI ``loss`` (negative ELBO) at most
:data:`MAX_HISTORY_POINTS` times per fit. :func:`pymc_draw_callback` gives the same for a
library that calls ``pm.sample`` itself. ``numpyro``, ``blackjax``, ``map`` and
``pathfinder`` report progress only.

**Start points.** By default every MCMC chain starts at the model's initial point with a
random jitter. A plugin whose posterior has a trap near the default start (a mixture whose
components all switch off) sets :attr:`EnginePlan.initvals` (values on the constrained scale,
by free variable name, as ``pm.sample(initvals=...)``) and ``jitter=False`` with
``dataclasses.replace(plan, ...)``: every chain then starts at exactly that point (``pymc``,
``nutpie`` and the external NUTS bridges; ``map``, ``advi`` and ``pathfinder`` ignore both).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import math
import time
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np
import pymc as pm
import xarray as xr
from mapi_sdk.errors import RunCancelled
from mapi_sdk.steps import CancelToken
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig
from pymc.backends.base import MultiTrace
from pymc.backends.ndarray import NDArray
from pymc.step_methods.hmc.nuts import NUTS

from mapi_pymc.checkpoint import CheckpointSink, SamplingCheckpoint, decode_state, encode_state

if TYPE_CHECKING:
    from mapi_sdk.models import FitContext
    from mapi_sdk.tracking import Tracker
    from mapi_spec import run as spec_run
    from mapi_spec.tracking import LogFieldValue, LogLevel, ParamValue

__all__ = [
    "DEFAULT_ADVI_STEPS",
    "DEFAULT_CHAINS",
    "DEFAULT_DRAWS",
    "DEFAULT_TUNE",
    "ENGINES",
    "FIT_HISTORY",
    "MAX_HISTORY_POINTS",
    "MCMC_ENGINES",
    "ChainProgress",
    "EngineHooks",
    "EnginePlan",
    "EngineResult",
    "SamplerPlan",
    "engine_available",
    "nutpie_progress",
    "pymc_draw_callback",
    "pymc_nutpie_progress",
    "resolve_engine",
    "run_engine",
]

_log = logging.getLogger(__name__)

ENGINES = ("nutpie", "pymc", "numpyro", "blackjax", "map", "advi", "pathfinder")
MCMC_ENGINES = frozenset({"nutpie", "pymc", "numpyro", "blackjax"})
DEFAULT_TUNE = 1000
DEFAULT_DRAWS = 1000
DEFAULT_CHAINS = 4
DEFAULT_ADVI_STEPS = 10_000
FIT_HISTORY: Final = "fit_history"
"""Group of an ADVI fit's loss per optimisation step (var ``loss(step)``)."""
MAX_HISTORY_POINTS: Final = 5000
_ENGINE_MODULES = {
    "nutpie": "nutpie",
    "numpyro": "numpyro",
    "blackjax": "blackjax",
    "pathfinder": "pymc_extras",
}
_ENGINE_INSTALL = {"pathfinder": "mapi-pymc[pathfinder]"}
"""Engines whose package comes from an extra: the requirement to name in the error."""
_VOLATILE_STATS = ("perf_counter_diff", "perf_counter_start", "process_time_diff", "warning")
_PROGRESS_EVERY_S = 0.5
_NUTPIE_POLL_S = 0.05
_no_live_progress_logged = False


def engine_available(name: str) -> bool:
    """Whether ``name`` can run in this environment."""
    if name not in ENGINES:
        return False
    module = _ENGINE_MODULES.get(name)
    return module is None or importlib.util.find_spec(module) is not None


def resolve_engine(name: str | None, default: str | None = None) -> str:
    """The engine to use: the spec's, else the plugin default, else nutpie (or pymc)."""
    chosen = name or default or ("nutpie" if engine_available("nutpie") else "pymc")
    if chosen not in ENGINES:
        raise MapiError(
            f"unknown fit engine {chosen!r}; choose one of {', '.join(ENGINES)}",
            code=ErrorCode.SETTINGS_INVALID,
            pointer="/model/fit/engine",
        )
    if not engine_available(chosen):
        module = _ENGINE_MODULES.get(chosen, chosen)
        install = _ENGINE_INSTALL.get(chosen)
        hint = f" (install {install})" if install else ""
        raise MapiError(
            f"fit engine {chosen!r} needs the {module!r} package, which is not installed in "
            f"this image{hint}",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
            pointer="/model/fit/engine",
        )
    return chosen


@dataclass(frozen=True, slots=True)
class EnginePlan:
    """Resolved sampler settings (``FitConfig`` with defaults applied)."""

    engine: str
    tune: int = DEFAULT_TUNE
    draws: int = DEFAULT_DRAWS
    chains: int = DEFAULT_CHAINS
    seed: int = 0
    target_accept: float | None = None
    checkpoint_every: int | None = None
    initvals: Mapping[str, Any] | None = None
    """Start values by free variable name (constrained scale); ``None``: the model's own."""
    jitter: bool = True
    """Jitter the start of each chain (``False``: every chain starts at the same point)."""

    @classmethod
    def from_config(
        cls, cfg: FitConfig, *, default_engine: str | None = None, seed: int = 0
    ) -> EnginePlan:
        engine = resolve_engine(cfg.engine, default_engine)
        if engine == "advi":
            tune = cfg.tune if cfg.tune is not None else DEFAULT_ADVI_STEPS
        else:
            tune = cfg.tune if cfg.tune is not None else DEFAULT_TUNE
        return cls(
            engine=engine,
            tune=tune,
            draws=cfg.draws if cfg.draws is not None else DEFAULT_DRAWS,
            chains=cfg.chains if cfg.chains is not None else DEFAULT_CHAINS,
            seed=cfg.seed if cfg.seed is not None else seed,
            target_accept=cfg.target_accept,
            checkpoint_every=cfg.checkpoint.every_draws if cfg.checkpoint else None,
        )

    @property
    def mcmc(self) -> bool:
        return self.engine in MCMC_ENGINES


@dataclass(frozen=True, slots=True)
class ChainProgress:
    """Progress of one chain."""

    chain: int
    chains: int
    done: int
    total: int
    tuning: bool
    divergences: int = 0
    step_size: float | None = None
    num_steps: int | None = None

    def message(self) -> str:
        phase = "tuning" if self.tuning else "sampling"
        return (
            f"chain {self.chain + 1}/{self.chains}: {phase} {self.done}/{self.total}, "
            f"{self.divergences} divergences"
        )

    def to_spec(self) -> spec_run.ChainProgress:
        """The run-protocol form (``progress`` event ``chains``)."""
        from mapi_spec.run import ChainProgress as SpecChainProgress

        step_size = self.step_size
        if step_size is not None and not (math.isfinite(step_size) and step_size >= 0):
            step_size = None
        return SpecChainProgress(
            chain=self.chain,
            draws_done=max(self.done, 0),
            draws_total=max(self.total, 0),
            tuning=self.tuning,
            divergences=max(self.divergences, 0),
            step_size=step_size,
            num_steps=None if self.num_steps is None else max(self.num_steps, 0),
        )


class _ContextTracker:
    """A :class:`~mapi_sdk.tracking.Tracker` over a ``FitContext``.

    Forwards to the context's logging methods so every engine value is validated by
    ``TrackingMixin`` (the context's own signatures differ from the protocol's).
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: FitContext) -> None:
        self._ctx = ctx

    def log_metrics(
        self,
        values: Mapping[str, float],
        *,
        step: int | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self._ctx.log_metrics(values, step=step, labels=labels)

    def log_params(self, params: Mapping[str, ParamValue]) -> None:
        self._ctx.log_params(params)

    def set_tags(self, tags: Mapping[str, str]) -> None:
        self._ctx.set_tags(tags)

    def log_line(
        self,
        message: str,
        *,
        level: LogLevel = "info",
        fields: Mapping[str, LogFieldValue] | None = None,
    ) -> None:
        self._ctx.log_line(message, level=level, fields=fields)

    def chains(self, chains: Sequence[spec_run.ChainProgress]) -> None:
        self._ctx.chains(chains)

    def flush(self) -> None:
        if self._ctx.tracker is not None:
            self._ctx.tracker.flush()


@dataclass(slots=True)
class EngineHooks:
    """Callbacks an engine run uses: progress, cancellation, logging, checkpoints and
    tracking (chains and step metrics; ``tracker=None`` skips building them)."""

    progress: Callable[[float, str], None] | None = None
    cancel: CancelToken | None = None
    log: Callable[[str], None] | None = None
    sink: CheckpointSink | None = None
    key: str | None = None
    min_interval: float = _PROGRESS_EVERY_S
    tracker: Tracker | None = None
    _last: float = field(default=float("-inf"), repr=False)
    _chains_last: float = field(default=float("-inf"), repr=False)

    @classmethod
    def for_context(
        cls,
        ctx: FitContext,
        *,
        progress: Callable[[float, str], None] | None,
        sink: CheckpointSink | None = None,
        key: str | None = None,
    ) -> EngineHooks:
        """Hooks wired to a plugin's ``FitContext`` (its cancel token, log and tracker)."""
        return cls(
            progress=progress,
            cancel=ctx.cancel,
            log=ctx.log,
            sink=sink,
            key=key,
            tracker=_ContextTracker(ctx) if ctx.tracking else None,
        )

    def report(self, fraction: float, message: str, *, force: bool = False) -> None:
        """Forward progress, at most once per ``min_interval`` seconds unless forced."""
        if self.progress is None:
            return
        now = time.monotonic()
        if force or now - self._last >= self.min_interval:
            self._last = now
            self.progress(min(max(fraction, 0.0), 1.0), message)

    def info(self, message: str) -> None:
        if self.log is not None:
            self.log(message)

    def check(self) -> None:
        if self.cancel is not None:
            self.cancel.check()

    def metrics(
        self, values: Mapping[str, float], *, step: int, labels: Mapping[str, str] | None = None
    ) -> None:
        """Log step metrics (no-op without a tracker)."""
        if self.tracker is not None:
            self.tracker.log_metrics(values, step=step, labels=labels)

    def chains(self, chains: Sequence[ChainProgress]) -> None:
        """Report every chain's progress (no-op without a tracker)."""
        if self.tracker is not None:
            self.tracker.chains([c.to_spec() for c in chains])

    def chains_every(
        self, build: Callable[[], Sequence[ChainProgress]], *, force: bool = False
    ) -> None:
        """:meth:`chains`, at most once per ``min_interval`` seconds unless forced."""
        if self.tracker is None:
            return
        now = time.monotonic()
        if force or now - self._chains_last >= self.min_interval:
            self._chains_last = now
            self.chains(build())


@dataclass(slots=True)
class EngineResult:
    """What an engine produced: ArviZ groups plus bookkeeping."""

    idata: xr.DataTree
    plan: EnginePlan
    resumed_from: int = 0
    checkpoints: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def engine(self) -> str:
        return self.plan.engine

    def group(self, name: str) -> xr.Dataset | None:
        if name not in self.idata.children:
            return None
        node = self.idata[name]
        assert isinstance(node, xr.DataTree)
        return node.to_dataset(inherit=False)


def run_engine(model: pm.Model, plan: EnginePlan, hooks: EngineHooks | None = None) -> EngineResult:
    """Fit ``model`` with ``plan.engine``."""
    hooks = hooks or EngineHooks()
    hooks.check()
    match plan.engine:
        case "pymc":
            return _sample_pymc(model, plan, hooks)
        case "nutpie":
            return _sample_nutpie(model, plan, hooks)
        case "numpyro" | "blackjax":
            return _sample_external(model, plan, hooks)
        case "map":
            return _find_map(model, plan, hooks)
        case "advi":
            return _advi(model, plan, hooks)
        case "pathfinder":
            return _pathfinder(model, plan, hooks)
    raise MapiError(f"unknown fit engine {plan.engine!r}", code=ErrorCode.SETTINGS_INVALID)


def _start_kwargs(plan: EnginePlan) -> dict[str, Any]:
    """``initvals`` for ``pm.init_nuts`` / ``pm.sample`` (empty when the plan sets none)."""
    return {"initvals": dict(plan.initvals)} if plan.initvals else {}


# ===================================================================================== pymc


@dataclass(slots=True)
class _Chains:
    """Per-chain buffers of the segmented PyMC sampler."""

    names: list[str]
    points: list[dict[str, np.ndarray]]
    states: list[Any]
    values: list[dict[str, list[np.ndarray]]]
    stats: list[dict[str, list[Any]]]
    divergences: list[int]
    completed: int = 0


@dataclass(slots=True)
class _LiveChains:
    """Per-chain live state of the in-process PyMC sampler, for tracking only.

    ``divergences`` counts tuning draws too (the step metric is cumulative over the run).
    The step metrics go out for every ``every``-th draw and the last one, so a chain logs at
    most :data:`MAX_HISTORY_POINTS` points per series (under the run's per-series budget).
    """

    hooks: EngineHooks
    chains: int
    total: int
    tune: int
    done: list[int]
    divergences: list[int]
    step_size: list[float | None]
    num_steps: list[int | None]
    every: int = 1

    @classmethod
    def start(cls, hooks: EngineHooks, plan: SamplerPlan) -> _LiveChains:
        n = plan.chains
        total = plan.tune + plan.draws
        return cls(
            hooks,
            n,
            total,
            plan.tune,
            [0] * n,
            [0] * n,
            [None] * n,
            [None] * n,
            every=max(1, math.ceil(total / MAX_HISTORY_POINTS)),
        )

    def draw(self, c: int, stats: Mapping[str, Any]) -> None:
        """Record one draw of chain ``c`` (``stats``: the NUTS step stats)."""
        self.done[c] += 1
        if bool(stats.get("diverging", False)):
            self.divergences[c] += 1
        size = _finite(stats.get("step_size"))
        steps = _stat(stats, "tree_size", "n_steps")
        depth = _stat(stats, "depth", "tree_depth")
        self.step_size[c] = size
        self.num_steps[c] = int(steps) if steps is not None else None
        if self.done[c] % self.every and self.done[c] != self.total:
            self.hooks.chains_every(self.snapshot)
            return
        values: dict[str, float] = {"divergences": float(self.divergences[c])}
        if size is not None:
            values["step_size"] = size
        if depth is not None:
            values["tree_depth"] = depth
        if steps is not None:
            values["num_steps"] = steps
        self.hooks.metrics(values, step=self.done[c], labels={"chain": str(c)})
        self.hooks.chains_every(self.snapshot)

    def chain(self, c: int) -> ChainProgress:
        return ChainProgress(
            c,
            self.chains,
            self.done[c],
            self.total,
            self.done[c] < self.tune,
            self.divergences[c],
            self.step_size[c],
            self.num_steps[c],
        )

    def snapshot(self) -> list[ChainProgress]:
        return [self.chain(c) for c in range(self.chains)]


class SamplerPlan(Protocol):
    """The part of a plan that :func:`pymc_draw_callback` reads (an :class:`EnginePlan` or a
    library's own plan)."""

    @property
    def tune(self) -> int: ...
    @property
    def draws(self) -> int: ...
    @property
    def chains(self) -> int: ...


def pymc_draw_callback(hooks: EngineHooks, plan: SamplerPlan) -> Callable[[Any, Any], None]:
    """A ``pm.sample(callback=...)`` handler for PyMC's own NUTS when a library samples
    outside :func:`run_engine` (``mmm.bayesian`` through mmm-framework).

    It reports progress (the slowest chain), stops the sampler on a cancel request
    (``RunCancelled`` raised in the callback) and, with a tracker, reports the chains and
    the same step metrics as the ``pymc`` engine: ``step_size``, ``num_steps``,
    ``tree_depth`` and cumulative ``divergences`` per chain, thinned as the engine does.
    """
    live = _LiveChains.start(hooks, plan)
    total = max(live.total * plan.chains, 1)

    def callback(trace: Any, draw: Any) -> None:
        c = int(draw.chain)
        if c >= plan.chains:
            return
        hooks.check()
        stats = draw.stats[0] if isinstance(draw.stats, list | tuple) and draw.stats else {}
        if hooks.tracker is not None:
            live.draw(c, stats if isinstance(stats, Mapping) else {})
            if min(live.done) >= live.total:  # the last draw: the final lanes always go out
                hooks.chains_every(live.snapshot, force=True)
        else:
            live.done[c] += 1
            if isinstance(stats, Mapping) and bool(stats.get("diverging", False)):
                live.divergences[c] += 1
        k = min(range(plan.chains), key=lambda j: live.done[j])
        hooks.report(sum(live.done) / total, live.chain(k).message())

    return callback


def _stat(stats: Mapping[str, Any], *names: str) -> float | None:
    """The first of ``names`` in a NUTS step's raw stats (``depth``/``tree_size`` there are
    ``tree_depth``/``n_steps`` in ``sample_stats``)."""
    for name in names:
        if name in stats:
            return _finite(stats[name])
    return None


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _sample_pymc(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    if not all(v.dtype.startswith("float") for v in model.value_vars) or not model.value_vars:
        return _sample_pymc_compound(model, plan, hooks)
    seeds = np.random.SeedSequence(plan.seed).spawn(plan.chains + 1)
    init_seeds = [int(s.generate_state(1)[0] % (2**31)) for s in seeds[: plan.chains]]
    kwargs: dict[str, Any] = _start_kwargs(plan)
    if plan.target_accept is not None:
        kwargs["target_accept"] = plan.target_accept
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        initial_points, step = pm.init_nuts(
            init="jitter+adapt_diag" if plan.jitter else "adapt_diag",
            chains=plan.chains,
            model=model,
            random_seed=init_seeds,
            progressbar=False,
            quiet=True,
            tune=plan.tune,
            **kwargs,
        )
    assert isinstance(step, NUTS)
    names = [v.name for v in model.value_vars]
    chains = _resume(hooks, plan, names, step)
    resumed_from = chains.completed if chains is not None else 0
    live: _LiveChains | None = None
    if hooks.tracker is not None:
        live = _LiveChains.start(hooks, plan)
        if chains is not None:  # resumed: tuning and the saved draws are done
            live.done = [plan.tune + resumed_from] * plan.chains
            live.divergences = list(chains.divergences)
    if chains is None:
        chains = _tune(model, plan, hooks, step, initial_points, names, seeds, live)
    total = plan.chains * (plan.tune + plan.draws)
    chunk = plan.checkpoint_every or plan.draws
    saved = 0
    start_states, start_points = list(chains.states), list(chains.points)
    start_divs = list(chains.divergences)
    try:
        while chains.completed < plan.draws:
            n = min(chunk, plan.draws - chains.completed)
            start_states, start_points = list(chains.states), list(chains.points)
            start_divs = list(chains.divergences)
            for c in range(plan.chains):
                step.sampling_state = chains.states[c]
                point = chains.points[c]
                for i in range(n):
                    if i % 16 == 0:
                        hooks.check()
                    point, stats = step.step(point)
                    _record(chains, c, point, stats[0])
                    if live is not None:
                        live.draw(c, stats[0])
                    done = plan.chains * (plan.tune + chains.completed) + c * n + i + 1
                    hooks.report(
                        done / total,
                        ChainProgress(
                            c,
                            plan.chains,
                            chains.completed + i + 1,
                            plan.draws,
                            False,
                            chains.divergences[c],
                        ).message(),
                    )
                chains.points[c] = point
                chains.states[c] = step.sampling_state
            chains.completed += n
            if hooks.sink is not None and plan.checkpoint_every and chains.completed < plan.draws:
                _save(hooks, plan, chains)
                saved += 1
    except RunCancelled:
        # roll back to the last chunk boundary every chain reached, flush, re-raise
        chains.states, chains.points = start_states, start_points
        chains.divergences = start_divs
        _trim(chains)
        if hooks.sink is not None and chains.completed:
            _save(hooks, plan, chains)
        raise
    if live is not None:
        hooks.chains_every(live.snapshot, force=True)
    hooks.report(1.0, f"sampled {plan.chains} chains x {plan.draws} draws", force=True)
    idata = _to_idata(model, step, chains, plan)
    if hooks.sink is not None and hooks.key is not None:
        hooks.sink.clear(hooks.key)
    notes = [f"resumed after {resumed_from} draws per chain"] if resumed_from else []
    return EngineResult(idata, plan, resumed_from=resumed_from, checkpoints=saved, notes=notes)


def _tune(
    model: pm.Model,
    plan: EnginePlan,
    hooks: EngineHooks,
    step: NUTS,
    initial_points: Any,
    names: list[str],
    seeds: list[np.random.SeedSequence],
    live: _LiveChains | None = None,
) -> _Chains:
    initial_state = step.sampling_state
    chains = _Chains(
        names=names,
        points=[],
        states=[],
        values=[{n: [] for n in names} for _ in range(plan.chains)],
        stats=[{} for _ in range(plan.chains)],
        divergences=[0] * plan.chains,
    )
    total = plan.chains * (plan.tune + plan.draws)
    for c in range(plan.chains):
        step.sampling_state = initial_state
        step.setup_chain(np.random.default_rng(seeds[c]), plan.tune, plan.draws)
        step.tune = bool(plan.tune)
        step.reset_tuning()
        if hasattr(step, "iter_count"):
            step.iter_count = 0
        point = initial_points[c]
        for i in range(plan.tune):
            if i % 16 == 0:
                hooks.check()
            point, stats = step.step(point)
            if live is not None:
                live.draw(c, stats[0])
            hooks.report(
                (c * plan.tune + i + 1) / total,
                ChainProgress(c, plan.chains, i + 1, plan.tune, True).message(),
            )
        step.stop_tuning()
        chains.points.append({n: np.asarray(point[n]) for n in names})
        chains.states.append(step.sampling_state)
    return chains


def _record(chains: _Chains, c: int, point: Mapping[str, Any], stats: Mapping[str, Any]) -> None:
    for n in chains.names:
        chains.values[c][n].append(np.array(point[n], copy=True))
    buf = chains.stats[c]
    for k, v in stats.items():
        if k in _VOLATILE_STATS:
            continue
        arr = np.asarray(v)
        if arr.dtype.kind not in "biuf":
            continue
        buf.setdefault(k, []).append(arr)
    if bool(stats.get("diverging", False)):
        chains.divergences[c] += 1


def _trim(chains: _Chains) -> None:
    """Drop draws past ``chains.completed`` (a chunk interrupted part-way)."""
    n = chains.completed
    for c in range(len(chains.values)):
        for k in chains.values[c]:
            chains.values[c][k] = chains.values[c][k][:n]
        for k in chains.stats[c]:
            chains.stats[c][k] = chains.stats[c][k][:n]


def _save(hooks: EngineHooks, plan: EnginePlan, chains: _Chains) -> None:
    assert hooks.sink is not None
    if hooks.key is None:
        return
    data: dict[str, Any] = {}
    for n in chains.names:
        arr = np.stack([np.stack(chains.values[c][n]) for c in range(plan.chains)])
        data[f"value__{n}"] = (("chain", "draw", *[f"{n}__d{k}" for k in range(arr.ndim - 2)]), arr)
    keys = sorted(set.intersection(*(set(s) for s in chains.stats))) if chains.stats else []
    for k in keys:
        arr = np.stack([np.stack(chains.stats[c][k]) for c in range(plan.chains)])
        data[f"stat__{k}"] = (("chain", "draw", *[f"{k}__s{j}" for j in range(arr.ndim - 2)]), arr)
    state = {
        "chains": [
            {
                "sampler": encode_state(chains.states[c]),
                "point": encode_state(chains.points[c]),
                "divergences": chains.divergences[c],
            }
            for c in range(plan.chains)
        ],
        "names": chains.names,
    }
    hooks.sink.save(
        SamplingCheckpoint(
            key=hooks.key,
            engine="pymc",
            resumable=True,
            chains=plan.chains,
            tune=plan.tune,
            draws=plan.draws,
            completed=chains.completed,
            data=xr.Dataset(data),
            state=state,
        )
    )
    hooks.info(f"checkpoint: {chains.completed}/{plan.draws} draws per chain")


def _resume(hooks: EngineHooks, plan: EnginePlan, names: list[str], step: NUTS) -> _Chains | None:
    if hooks.sink is None or hooks.key is None:
        return None
    ckpt = hooks.sink.load(hooks.key)
    if ckpt is None:
        return None
    compatible = (
        ckpt.resumable
        and ckpt.engine == "pymc"
        and ckpt.chains == plan.chains
        and ckpt.tune == plan.tune
        and ckpt.draws == plan.draws
        and ckpt.state.get("names") == names
    )
    if not compatible:
        hooks.info("ignoring an incompatible checkpoint; sampling from the start")
        return None
    template = type(step.sampling_state)
    chains = _Chains(
        names=names,
        points=[],
        states=[],
        values=[{} for _ in range(plan.chains)],
        stats=[{} for _ in range(plan.chains)],
        divergences=[],
        completed=ckpt.completed,
    )
    for c, entry in enumerate(ckpt.state["chains"]):
        state = decode_state(entry["sampler"])
        if not isinstance(state, template):
            hooks.info("checkpoint sampler state does not match this sampler; starting over")
            return None
        chains.states.append(state)
        chains.points.append({k: np.asarray(v) for k, v in decode_state(entry["point"]).items()})
        chains.divergences.append(int(entry.get("divergences", 0)))
        for n in names:
            arr = np.asarray(ckpt.data[f"value__{n}"].values[c])
            chains.values[c][n] = [np.array(arr[i]) for i in range(arr.shape[0])]
        for var in ckpt.data.data_vars:
            if str(var).startswith("stat__"):
                arr = np.asarray(ckpt.data[var].values[c])
                chains.stats[c][str(var)[6:]] = [arr[i] for i in range(arr.shape[0])]
    hooks.info(f"resuming from a checkpoint after {ckpt.completed}/{plan.draws} draws per chain")
    return chains


def _to_idata(model: pm.Model, step: NUTS, chains: _Chains, plan: EnginePlan) -> xr.DataTree:
    traces = []
    for c in range(plan.chains):
        tr = NDArray(model=model)
        tr.setup(plan.draws, c, sampler_vars=step.stats_dtypes)
        keys = list(chains.stats[c])
        for i in range(plan.draws):
            point = {n: chains.values[c][n][i] for n in chains.names}
            stats = {k: chains.stats[c][k][i] for k in keys}
            tr.record(point, [stats], in_warmup=False)
        tr.close()
        traces.append(tr)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        idata: xr.DataTree = pm.to_inference_data(MultiTrace(traces), model=model)
    return idata


def _sample_pymc_compound(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    """Models with discrete variables: plain ``pm.sample`` (compound steps, no segments)."""
    total = plan.chains * (plan.tune + plan.draws)
    counts = [0] * plan.chains
    tuning = [bool(plan.tune)] * plan.chains

    def snapshot() -> list[ChainProgress]:
        return [
            ChainProgress(c, plan.chains, counts[c], plan.tune + plan.draws, tuning[c])
            for c in range(plan.chains)
        ]

    def callback(trace: Any, draw: Any) -> None:
        counts[draw.chain] += 1
        tuning[draw.chain] = bool(draw.tuning)
        hooks.check()
        hooks.chains_every(snapshot)
        hooks.report(
            sum(counts) / total,
            ChainProgress(
                draw.chain, plan.chains, draw.draw_idx + 1, plan.tune + plan.draws, draw.tuning
            ).message(),
        )

    kwargs: dict[str, Any] = _start_kwargs(plan)
    if not plan.jitter:
        kwargs["init"] = "adapt_diag"
    if plan.target_accept is not None:
        kwargs["target_accept"] = plan.target_accept
    with model, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        idata = pm.sample(
            plan.draws,
            tune=plan.tune,
            chains=plan.chains,
            cores=1,
            random_seed=plan.seed,
            progressbar=False,
            quiet=True,
            nuts_sampler="pymc",
            callback=callback,
            compute_convergence_checks=False,
            **kwargs,
        )
    hooks.chains_every(snapshot, force=True)
    notes = ["the model has non-continuous variables: sampled without segments"]
    return EngineResult(idata, plan, notes=notes)


# ===================================================================================== nutpie


def _sample_nutpie(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    import nutpie

    latest: list[Any] = []
    report = nutpie_progress(hooks)

    def on_progress(progress: list[Any]) -> None:
        latest[:] = list(progress)
        report(progress)

    notes: list[str] = []
    if hooks.sink is not None and hooks.key is not None:
        prior = hooks.sink.load(hooks.key)
        if prior is not None:
            notes.append(
                "a nutpie snapshot checkpoint exists but nutpie cannot restore its adaptation "
                "state; sampling again from the start"
            )
            hooks.info(notes[-1])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        compiled = nutpie.compile_pymc_model(
            model,
            initial_points=dict(plan.initvals) if plan.initvals else None,
            jitter_rvs=None if plan.jitter else set(),
        )
    kwargs: dict[str, Any] = {
        "draws": plan.draws,
        "tune": plan.tune,
        "chains": plan.chains,
        "seed": plan.seed,
        "progress_bar": False,
        "progress_callback": on_progress,
        "progress_rate": 200,
        "save_warmup": False,
    }
    if plan.target_accept is not None:
        kwargs["target_accept"] = plan.target_accept
    sampler = nutpie.sample(compiled, blocking=False, **kwargs)
    saved = 0
    every = plan.checkpoint_every
    next_mark = every or plan.draws + 1
    try:
        while not _finished(sampler):
            hooks.check()
            time.sleep(_NUTPIE_POLL_S)
            if hooks.sink is None or hooks.key is None or not every or not latest:
                continue
            done = min(int(p.finished_draws) for p in latest) - plan.tune
            if next_mark <= done < plan.draws:
                sampler.pause()
                try:
                    snap = sampler.inspect()
                finally:
                    sampler.resume()
                _save_snapshot(hooks, plan, snap, done)
                saved += 1
                next_mark = (done // every + 1) * every
    except BaseException:
        sampler.cancel()  # never leave a background sampler running (its __del__ joins)
        raise
    trace = sampler.wait()
    if hooks.sink is not None and hooks.key is not None:
        hooks.sink.clear(hooks.key)
    hooks.report(1.0, f"sampled {plan.chains} chains x {plan.draws} draws", force=True)
    keep = {k: trace[k] for k in ("posterior", "sample_stats") if k in trace.children}
    idata = xr.DataTree.from_dict({f"/{k}": v.to_dataset(inherit=False) for k, v in keep.items()})
    return EngineResult(idata, plan, checkpoints=saved, notes=notes)


def nutpie_progress(hooks: EngineHooks) -> Callable[[list[Any]], None]:
    """A handler for nutpie's ``progress_callback`` (a list of nutpie ``ChainProgress``).

    Reports the slowest chain as the progress message and, with a tracker, every chain's
    progress plus the ``step_size``/``num_steps``/``divergences`` step metrics of each chain
    that finished draws since the last call. It runs on nutpie's sampler thread and never
    raises (a failure is logged once).
    """
    last_done: dict[int, int] = {}
    failed = False

    def handle(progress: list[Any]) -> None:
        nonlocal failed
        if not progress:
            return
        try:
            n = len(progress)
            total = sum(int(p.total_draws) for p in progress) or 1
            done = sum(int(p.finished_draws) for p in progress)
            lagging = min(range(n), key=lambda c: int(progress[c].finished_draws))
            if hooks.tracker is not None:
                chains = [_nutpie_chain(c, n, p) for c, p in enumerate(progress)]
                hooks.chains(chains)
                for ch in chains:
                    if ch.done > last_done.get(ch.chain, 0):
                        last_done[ch.chain] = ch.done
                        values = {"divergences": float(ch.divergences)}
                        if ch.step_size is not None:
                            values["step_size"] = ch.step_size
                        if ch.num_steps is not None:
                            values["num_steps"] = float(ch.num_steps)
                        hooks.metrics(values, step=ch.done, labels={"chain": str(ch.chain)})
                msg = chains[lagging].message()
            else:
                msg = _nutpie_chain(lagging, n, progress[lagging]).message()
            hooks.report(done / total, msg)
        except Exception as exc:  # never break nutpie's sampler thread
            if not failed:
                failed = True
                _log.warning("nutpie progress handler failed: %s", exc, exc_info=True)

    return handle


def _nutpie_chain(c: int, n: int, p: Any) -> ChainProgress:
    steps = getattr(p, "latest_num_steps", None)
    return ChainProgress(
        c,
        n,
        int(p.finished_draws),
        int(p.total_draws),
        bool(p.tuning),
        int(p.divergences),
        _finite(getattr(p, "step_size", None)),
        int(steps) if steps is not None else None,
    )


@contextmanager
def pymc_nutpie_progress(callback: Callable[[list[Any]], None]) -> Iterator[None]:
    """Receive nutpie's per-chain progress when PyMC drives nutpie (``pm.sample(nuts_sampler=
    "nutpie")``).

    PyMC passes its own ``progress_callback`` to nutpie, so a caller cannot pass one. Inside
    this block ``pymc.sampling.mcmc.NutpieProgressBarManager`` is replaced by a subclass whose
    ``update`` first calls ``callback`` (exceptions are logged, never raised). The patch is
    process-global: use it around one fit at a time. With a PyMC that has no such class the
    block runs without live progress.
    """
    global _no_live_progress_logged
    mcmc = importlib.import_module("pymc.sampling.mcmc")
    base: Any = getattr(mcmc, "NutpieProgressBarManager", None)
    if base is None:
        if not _no_live_progress_logged:
            _no_live_progress_logged = True
            _log.info("live sampler progress is not available with this PyMC version")
        yield
        return

    class _LiveProgress(base):
        def update(self, chain_progresses: Any) -> None:
            try:
                callback(list(chain_progresses))
            except Exception as exc:
                _log.warning("nutpie progress callback failed: %s", exc, exc_info=True)
            super().update(chain_progresses)

    setattr(mcmc, "NutpieProgressBarManager", _LiveProgress)  # noqa: B010
    try:
        yield
    finally:
        setattr(mcmc, "NutpieProgressBarManager", base)  # noqa: B010


def _finished(sampler: Any) -> bool:
    """nutpie's ``is_finished`` raises ``TimeoutError`` while sampling is still running."""
    try:
        return bool(sampler.is_finished)
    except TimeoutError:
        return False


def _save_snapshot(hooks: EngineHooks, plan: EnginePlan, snap: xr.DataTree, done: int) -> None:
    assert hooks.sink is not None and hooks.key is not None
    node = snap["posterior"]
    assert isinstance(node, xr.DataTree)
    post = node.to_dataset(inherit=False)
    done = min(done, int(post.sizes["draw"]))
    post = post.isel(draw=slice(0, done))
    data = xr.Dataset({f"posterior__{k}": v for k, v in post.data_vars.items()})
    hooks.sink.save(
        SamplingCheckpoint(
            key=hooks.key,
            engine="nutpie",
            resumable=False,
            chains=plan.chains,
            tune=plan.tune,
            draws=plan.draws,
            completed=done,
            data=data.drop_vars([c for c in data.coords if c not in data.dims]),
        )
    )
    hooks.info(f"snapshot: {done}/{plan.draws} draws per chain")


# ============================================================================ other engines


def _sample_external(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    kwargs: dict[str, Any] = _start_kwargs(plan)
    if not plan.jitter:
        kwargs["init"] = "adapt_diag"
    if plan.target_accept is not None:
        kwargs["target_accept"] = plan.target_accept
    hooks.report(0.0, f"sampling with {plan.engine}", force=True)
    with model, warnings.catch_warnings():  # pragma: no cover - optional engines
        warnings.simplefilter("ignore")
        idata = pm.sample(
            plan.draws,
            tune=plan.tune,
            chains=plan.chains,
            random_seed=plan.seed,
            progressbar=False,
            quiet=True,
            nuts_sampler=plan.engine,
            compute_convergence_checks=False,
            **kwargs,
        )
    hooks.report(1.0, f"sampled with {plan.engine}", force=True)  # pragma: no cover
    return EngineResult(idata, plan, notes=[f"external sampler {plan.engine}: no segments"])


def _find_map(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    hooks.report(0.0, "optimising (MAP)", force=True)
    with model, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        point = pm.find_MAP(progressbar=False, seed=plan.seed, include_transformed=True)
    tr = NDArray(model=model)
    tr.setup(1, 0)
    tr.record({v.name: np.asarray(point[v.name]) for v in model.value_vars}, in_warmup=False)
    tr.close()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        idata = pm.to_inference_data(MultiTrace([tr]), model=model)
    hooks.report(1.0, "found the MAP estimate", force=True)
    plan_one = EnginePlan("map", tune=0, draws=1, chains=1, seed=plan.seed)
    return EngineResult(idata, plan_one, notes=["MAP: a single point estimate (1 chain, 1 draw)"])


def _advi(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    steps = plan.tune
    every = max(1, math.ceil(steps / MAX_HISTORY_POINTS))

    def callback(approx: Any, losses: Any, i: int) -> None:
        if i % 50 == 0:
            hooks.check()
        hooks.report(i / max(steps, 1), f"ADVI step {i}/{steps}")
        if (
            hooks.tracker is not None
            and losses is not None
            and len(losses)
            and (i % every == 0 or i == steps)
            and (loss := _finite(losses[-1])) is not None
        ):
            hooks.metrics({"loss": loss}, step=i)

    with model, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        approx = pm.fit(
            n=steps, method="advi", random_seed=plan.seed, progressbar=False, callbacks=[callback]
        )
        idata = approx.sample(plan.draws, random_seed=plan.seed)
    hooks.report(1.0, "ADVI finished", force=True)
    keep = {k: idata[k] for k in ("posterior",) if k in idata.children}
    groups = {f"/{k}": v.to_dataset(inherit=False) for k, v in keep.items()}
    groups[f"/{FIT_HISTORY}"] = _history_dataset(np.asarray(approx.hist, dtype=float))
    tree = xr.DataTree.from_dict(groups)
    plan_vi = EnginePlan("advi", tune=steps, draws=plan.draws, chains=1, seed=plan.seed)
    return EngineResult(tree, plan_vi, notes=[f"ADVI: {steps} steps, {plan.draws} draws"])


def _history_dataset(hist: np.ndarray) -> xr.Dataset:
    """The loss of each optimisation step as ``loss(step)``, steps numbered from 1.

    More than :data:`MAX_HISTORY_POINTS` steps are thinned to that many evenly spaced steps
    (the last step always kept; ``thinned_from`` records the full count). Non-finite losses
    stay as NaN.
    """
    loss = np.where(np.isfinite(hist), hist, np.nan).ravel()
    n = loss.size
    steps = np.arange(1, n + 1, dtype=np.int64)
    attrs: dict[str, Any] = {}
    if n > MAX_HISTORY_POINTS:
        idx = np.unique(np.linspace(0, n - 1, MAX_HISTORY_POINTS).round().astype(np.int64))
        loss, steps = loss[idx], steps[idx]
        attrs["thinned_from"] = n
    return xr.Dataset(
        {"loss": ("step", loss, {"objective": "negative ELBO", "engine": "advi"})},
        coords={"step": steps},
        attrs=attrs,
    )


def _pathfinder(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    import pymc_extras as pmx  # pragma: no cover - optional dependency

    hooks.report(0.0, "running Pathfinder", force=True)  # pragma: no cover
    with model, warnings.catch_warnings():  # pragma: no cover
        warnings.simplefilter("ignore")
        idata = pmx.fit(
            method="pathfinder",
            num_draws=plan.draws,
            random_seed=plan.seed,
            # The default rich progress table writes to stdout (the worker's log).
            progressbar=False,
        )
    hooks.report(1.0, "Pathfinder finished", force=True)  # pragma: no cover
    keep = {k: idata[k] for k in ("posterior",) if k in idata.children}  # pragma: no cover
    tree = xr.DataTree.from_dict(  # pragma: no cover
        {f"/{k}": v.to_dataset(inherit=False) for k, v in keep.items()}
    )
    plan_pf = EnginePlan("pathfinder", tune=0, draws=plan.draws, chains=1, seed=plan.seed)
    return EngineResult(tree, plan_pf, notes=["Pathfinder approximation"])  # pragma: no cover
