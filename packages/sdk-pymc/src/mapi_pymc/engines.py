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
``pathfinder``  pymc-extras' Pathfinder when pymc-extras is installed.
==============  ==========================================================================

Every engine reports progress through one callback ``(fraction, message)`` where the
message names the chain (``"chain 2/4: sampling 150/500, 0 divergences"``).

**Start points.** By default every MCMC chain starts at the model's initial point with a
random jitter. A plugin whose posterior has a trap near the default start (a mixture whose
components all switch off) sets :attr:`EnginePlan.initvals` (values on the constrained scale,
by free variable name, as ``pm.sample(initvals=...)``) and ``jitter=False`` with
``dataclasses.replace(plan, ...)``: every chain then starts at exactly that point (``pymc``,
``nutpie`` and the external NUTS bridges; ``map``, ``advi`` and ``pathfinder`` ignore both).
"""

from __future__ import annotations

import importlib.util
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

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
    "engine_available",
    "resolve_engine",
    "run_engine",
]

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
_VOLATILE_STATS = ("perf_counter_diff", "perf_counter_start", "process_time_diff", "warning")
_PROGRESS_EVERY_S = 0.5
_NUTPIE_POLL_S = 0.05


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
        raise MapiError(
            f"fit engine {chosen!r} needs the {module!r} package, which is not installed in "
            "this image",
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

    def message(self) -> str:
        phase = "tuning" if self.tuning else "sampling"
        return (
            f"chain {self.chain + 1}/{self.chains}: {phase} {self.done}/{self.total}, "
            f"{self.divergences} divergences"
        )


@dataclass(slots=True)
class EngineHooks:
    """Callbacks an engine run uses: progress, cancellation, logging and checkpoints."""

    progress: Callable[[float, str], None] | None = None
    cancel: CancelToken | None = None
    log: Callable[[str], None] | None = None
    sink: CheckpointSink | None = None
    key: str | None = None
    min_interval: float = _PROGRESS_EVERY_S
    _last: float = field(default=float("-inf"), repr=False)

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
    if chains is None:
        chains = _tune(model, plan, hooks, step, initial_points, names, seeds)
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
            point, _ = step.step(point)
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

    def callback(trace: Any, draw: Any) -> None:
        counts[draw.chain] += 1
        hooks.check()
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
    notes = ["the model has non-continuous variables: sampled without segments"]
    return EngineResult(idata, plan, notes=notes)


# ===================================================================================== nutpie


def _sample_nutpie(model: pm.Model, plan: EnginePlan, hooks: EngineHooks) -> EngineResult:
    import nutpie

    latest: list[Any] = []

    def on_progress(progress: list[Any]) -> None:
        latest[:] = list(progress)
        total = sum(int(p.total_draws) for p in progress) or 1
        done = sum(int(p.finished_draws) for p in progress)
        lagging = min(progress, key=lambda p: int(p.finished_draws))
        msg = ChainProgress(
            progress.index(lagging),
            len(progress),
            int(lagging.finished_draws),
            int(lagging.total_draws),
            bool(lagging.tuning),
            int(lagging.divergences),
        ).message()
        hooks.report(done / total, msg)

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

    def callback(approx: Any, losses: Any, i: int) -> None:
        if i % 50 == 0:
            hooks.check()
        hooks.report(i / max(steps, 1), f"ADVI step {i}/{steps}")

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
        idata = pmx.fit(method="pathfinder", num_draws=plan.draws, random_seed=plan.seed)
    hooks.report(1.0, "Pathfinder finished", force=True)  # pragma: no cover
    keep = {k: idata[k] for k in ("posterior",) if k in idata.children}  # pragma: no cover
    tree = xr.DataTree.from_dict(  # pragma: no cover
        {f"/{k}": v.to_dataset(inherit=False) for k, v in keep.items()}
    )
    plan_pf = EnginePlan("pathfinder", tune=0, draws=plan.draws, chains=1, seed=plan.seed)
    return EngineResult(tree, plan_pf, notes=["Pathfinder approximation"])  # pragma: no cover
