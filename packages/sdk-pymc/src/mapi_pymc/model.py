"""``PyMCModel``: authors write ``build()``; the platform fits, predicts and checkpoints (§5).

.. code-block:: python

    class Radon(PyMCModel[RadonSettings]):
        id = "demo.radon"
        version = "1.0.0"
        Settings = RadonSettings
        schema_template = {...}
        outputs = {"y": {"dims": ["obs"], "units_of": "y"}}
        expected = {"y": "mu"}             # output -> Deterministic holding E[y]

        def build(self, ds, s, ctx):
            with pm.Model(coords=ctx.coords()) as m:
                floor = ctx.data("floor")
                county = ctx.data("county_idx")
                a = ctx.hier_effect("a", "county")
                b = pm.Normal("b", 0, 1)
                mu = pm.Deterministic("mu", a[county] + b * floor, dims="obs")
                pm.Normal("y", mu, pm.HalfNormal("sigma", 1), observed=ctx.observed("y"),
                          dims="obs")
            return m

**fit** dispatches to the engine in ``fit.engine`` (see :mod:`mapi_pymc.engines`), adds the
opt-in groups (``prior``/``prior_predictive``, ``log_likelihood``, a thinned
``posterior_predictive``) and turns r-hat, ESS and divergences into diagnostics.

**score** rebuilds the model on the canonicalised score data and runs
``pm.sample_posterior_predictive(..., predictions=True)`` against the posterior:

* ``output.kind = expected`` returns the Deterministic named in :attr:`PyMCModel.expected`
  (no observation noise; counterfactual deltas on it are exact per draw);
* ``output.kind = predictive`` returns draws of the observed variable through
  ``icdf(dist, U)`` with **frozen** uniforms ``U`` seeded by ``(seed, name, shape)``. A
  counterfactual's scenario and baseline therefore share their noise (common random
  numbers) and the delta's variance is not inflated. Discrete distributions without a
  symbolic inverse CDF (Poisson, negative binomial, binomial) sample their parameters and
  push the same frozen uniforms through a numeric inverse CDF (``noise = crn_ppf``), so a
  change in one cell's mean never reshuffles the noise of the others. Only distributions
  with neither fall back to the seeded sampler stream (``seeded_stream``), whose draws in
  *unchanged* cells can still differ between a scenario and its baseline - prefer
  ``kind: expected`` deltas for those.
"""

from __future__ import annotations

import hashlib
import logging
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import scipy.stats
import xarray as xr
from mapi_sdk.models import PREDICTIONS, CustomModel, FitContext, FitOutput, Fitted, ScoreContext
from mapi_spec.capabilities import Capabilities, Diagnostic, ModelManifest, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, PosteriorPredictiveKeep, ScoreRequest
from pydantic import BaseModel

from mapi_pymc.checkpoint import CheckpointSink, fit_key
from mapi_pymc.context import BuildContext
from mapi_pymc.diagnostics import convergence_diagnostics
from mapi_pymc.engines import EngineHooks, EnginePlan, EngineResult, run_engine
from mapi_pymc.frozen import FrozenPredictor, FrozenPredictorSpec, create_frozen_predictor

__all__ = ["DEFAULT_CAPABILITIES", "PyMCModel"]

DEFAULT_CAPABILITIES = Capabilities(
    inference="mcmc",
    representation=Representation.DRAWS,
    score_modes=frozenset({"predict", "forecast", "counterfactual", "evaluate", "simulate"}),
    deterministic_given_seed=False,
    checkpointable=True,
)
"""Defaults for PyMC plugins. nutpie (the default engine) is not seed-deterministic; a plugin
pinning ``default_engine = "pymc"`` may declare ``deterministic_given_seed=True``."""

_DROP_STATS = ("perf_counter_diff", "perf_counter_start", "process_time_diff", "warning")
_PRIOR_DRAWS = 500
CRN_SUFFIX = "__crn"
CRN_U_SUFFIX = "__crn_u"


class PyMCModel[SettingsT: BaseModel](CustomModel[SettingsT]):
    """Base class for PyMC plugins: implement :meth:`build`; declare outputs and ``expected``.

    Class attributes beyond :class:`~mapi_sdk.models.CustomModel`:

    * ``expected`` - ``{output: Deterministic name}`` for ``kind=expected`` predictions;
    * ``predictive`` - ``{output: observed RV name}`` (default: the output's own name);
    * ``default_engine`` - engine when the spec names none (default nutpie, else pymc);
    * ``fast_score_spec`` - a :class:`~mapi_pymc.frozen.FrozenPredictorSpec` enabling
      :meth:`frozen_predictor` (the manifest then declares ``fast_score``);
    * ``prior_overlap_vars`` - free parameters whose prior draws (``prior_overlap_draws``,
      forward sampling only, so cheap) are kept as the ``prior`` group when the spec keeps
      no prior itself, so the sanity scorecard can measure how much the data moved them
      (M3, ``plausibility.prior_overlap``). Empty (the default) keeps nothing.
    """

    capabilities: ClassVar[Capabilities] = DEFAULT_CAPABILITIES
    expected: ClassVar[Mapping[str, str]] = {}
    predictive: ClassVar[Mapping[str, str]] = {}
    default_engine: ClassVar[str | None] = None
    fast_score_spec: ClassVar[FrozenPredictorSpec | None] = None
    prior_overlap_vars: ClassVar[tuple[str, ...]] = ()
    prior_overlap_draws: ClassVar[int] = 200

    # ------------------------------------------------------------------ author API
    def build(self, ds: xr.Dataset, s: SettingsT, ctx: BuildContext) -> pm.Model:
        """Return a ``pm.Model`` for ``ds`` (called at fit and again at every score)."""
        name = getattr(type(self), "id", type(self).__name__)
        raise NotImplementedError(f"model {name!r} does not implement build()")

    # ------------------------------------------------------------------ manifest
    @classmethod
    def manifest(cls) -> ModelManifest:
        m = super().manifest()
        if cls.fast_score_spec is not None and not m.capabilities.fast_score:
            caps = m.capabilities.model_copy(update={"fast_score": True})
            m = m.model_copy(update={"capabilities": caps})
        return m

    # ------------------------------------------------------------------ build
    def build_model(self, ds: xr.Dataset, s: SettingsT, ctx: BuildContext) -> pm.Model:
        """Call :meth:`build` and check that it returned a model with the declared outputs."""
        model = self.build(ds, s, ctx)
        if not isinstance(model, pm.Model):
            raise MapiError(
                f"model {self.id!r}: build() must return a pymc.Model, got {type(model).__name__}",
                code=ErrorCode.USER_CODE_ERROR,
            )
        for out, var in self.expected.items():
            if var not in model.named_vars:
                raise MapiError(
                    f"model {self.id!r}: expected[{out!r}] names {var!r}, which build() did "
                    "not create",
                    code=ErrorCode.USER_CODE_ERROR,
                )
        return model

    def _context(
        self,
        phase: str,
        ds: xr.Dataset,
        *,
        schema: Any,
        policies: Mapping[str, Any],
        fit_coords: Mapping[str, np.ndarray],
        seed: int,
        mode: str | None = None,
        kind: Any = None,
    ) -> BuildContext:
        return BuildContext(
            phase="fit" if phase == "fit" else "score",
            ds=ds,
            schema=schema,
            policies=dict(policies),
            fit_coords=dict(fit_coords),
            seed=seed,
            mode=mode,
            kind=kind,
        )

    # ------------------------------------------------------------------ fit
    def fit(self, ds: xr.Dataset, s: SettingsT, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        seed = cfg.seed if cfg.seed is not None else ctx.seed
        bctx = self._context(
            "fit", ds, schema=ctx.schema, policies=ctx.policies, fit_coords=ctx.coords, seed=seed
        )
        model = self.build_model(ds, s, bctx)
        plan = EnginePlan.from_config(cfg, default_engine=self.default_engine, seed=seed)
        sink = ctx.checkpoint_sink if isinstance(ctx.checkpoint_sink, CheckpointSink) else None
        key = None
        if sink is not None:
            key = fit_key(
                model_id=self.id,
                version=self.version,
                settings=s.model_dump(mode="json"),
                fit_config=cfg.model_dump(mode="json"),
                data=ds,
            )
        hooks = EngineHooks(
            progress=_window(ctx, 0.0, 0.9),
            cancel=ctx.cancel,
            log=ctx.log,
            sink=sink,
            key=key,
        )
        result = run_engine(model, plan, hooks)
        ctx.check_cancelled()
        groups = self._groups(model, result, cfg, bctx, ds)
        ctx.progress(1.0, "posterior ready")
        diagnostics: list[Diagnostic] = []
        if result.plan.mcmc:
            free = [rv.name for rv in model.free_RVs]
            diagnostics = convergence_diagnostics(
                groups["posterior"], groups.get("sample_stats"), var_names=free
            )
        learned: dict[str, Any] = {
            "engine": result.engine,
            "tune": result.plan.tune,
            "draws": result.plan.draws,
            "chains": result.plan.chains,
            "target_accept": result.plan.target_accept,
            "resumed_from": result.resumed_from,
            "checkpoints": result.checkpoints,
            "notes": [str(n) for n in result.notes],
            "observed": sorted(bctx.observed_vars),
            "hier_effects": {k: v.dim for k, v in bctx.hier_effects.items()},
        }
        return FitOutput(groups=groups, learned=learned, diagnostics=diagnostics)

    def _groups(
        self,
        model: pm.Model,
        result: EngineResult,
        cfg: FitConfig,
        bctx: BuildContext,
        ds: xr.Dataset,
    ) -> dict[str, xr.Dataset]:
        posterior = result.group("posterior")
        if posterior is None:
            raise MapiError(
                f"engine {result.engine!r} returned no posterior", code=ErrorCode.INTERNAL_ERROR
            )
        engine = result.engine
        groups: dict[str, xr.Dataset] = {"posterior": _clean(posterior, engine)}
        stats = result.group("sample_stats")
        if stats is not None:
            groups["sample_stats"] = _clean(stats.drop_vars(_DROP_STATS, errors="ignore"), engine)
        if bctx.observed_values:
            groups["observed_data"] = _clean(
                _named(bctx.observed_values, bctx.observed_vars, ds), engine
            )
        if bctx.data_values:
            groups["constant_data"] = _clean(_named(bctx.data_values, bctx.data_vars, ds), engine)
        seed = result.plan.seed
        keep = cfg.keep
        with _quiet():
            if keep.log_likelihood and bctx.observed_vars:
                tree = xr.DataTree.from_dict({"posterior": groups["posterior"]})
                ll = pm.compute_log_likelihood(
                    tree, model=model, extend_inferencedata=False, progressbar=False
                )
                ll_ds = ll if isinstance(ll, xr.Dataset) else _child(ll, "log_likelihood")
                groups["log_likelihood"] = _clean(ll_ds, engine)
            if cfg.prior_predictive is not None or keep.prior:
                n = cfg.prior_predictive.draws if cfg.prior_predictive else _PRIOR_DRAWS
                prior = pm.sample_prior_predictive(draws=n, model=model, random_seed=seed)
                groups["prior"] = _clean(_child(prior, "prior"), engine)
                if cfg.prior_predictive is not None and "prior_predictive" in prior.children:
                    groups["prior_predictive"] = _clean(_child(prior, "prior_predictive"), engine)
            if "prior" not in groups and result.plan.mcmc:
                overlap = self._overlap_prior(model, seed)
                if overlap is not None:
                    groups["prior"] = _clean(overlap, engine)
            if keep.posterior_predictive and bctx.observed_vars:
                thin = (
                    keep.posterior_predictive.thin
                    if isinstance(keep.posterior_predictive, PosteriorPredictiveKeep)
                    else 1
                )
                thinned = groups["posterior"].isel(draw=slice(None, None, thin))
                pp = pm.sample_posterior_predictive(
                    thinned, model=model, random_seed=seed, progressbar=False
                )
                groups["posterior_predictive"] = _clean(_child(pp, "posterior_predictive"), engine)
        return groups

    def _overlap_prior(self, model: pm.Model, seed: int | None) -> xr.Dataset | None:
        """Prior draws of :attr:`prior_overlap_vars` (``None`` when none applies)."""
        free = {rv.name for rv in model.free_RVs}
        names = [n for n in self.prior_overlap_vars if n in free]
        if not names or self.prior_overlap_draws <= 0:
            return None
        prior = pm.sample_prior_predictive(
            draws=self.prior_overlap_draws, model=model, var_names=names, random_seed=seed
        )
        return _child(prior, "prior")

    # ------------------------------------------------------------------ score
    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        s = self.parse_settings(fitted.settings)
        bctx = self._context(
            "score",
            ds,
            schema=ctx.schema or fitted.schema,
            policies=ctx.policies,
            fit_coords=fitted.coords,
            seed=ctx.seed,
            mode=ctx.mode,
            kind=ctx.kind,
        )
        model = self.build_model(ds, s, bctx)
        ctx.check_cancelled()
        wanted = self._targets(model, ctx.kind)
        noise: dict[str, str] = {}
        discrete: dict[str, _DiscreteCrn] = {}
        with model:
            for out, (var, how) in list(wanted.items()):
                if how != "predictive":
                    noise[out] = "none"
                    continue
                crn = _add_crn(model, var, bctx)
                if crn is not None:
                    wanted[out] = (crn, how)
                    noise[out] = "crn_icdf"
                    continue
                disc = _add_discrete_crn(model, var, bctx)
                if disc is not None:
                    discrete[out] = disc
                    wanted[out] = (disc.params[0], how)
                    noise[out] = "crn_ppf"
                else:
                    noise[out] = "seeded_stream"
        trace, freeze = _trace(model, fitted.posterior, bctx)
        var_names = sorted(
            {v for v, _ in wanted.values()} | {p for d in discrete.values() for p in d.params}
        )
        with _quiet():
            pp = pm.sample_posterior_predictive(
                trace,
                model=model,
                var_names=var_names,
                predictions=True,
                random_seed=_seed_int(ctx.seed, "ppc"),
                progressbar=False,
                freeze_vars=freeze or None,
            )
        preds = _child(pp, "predictions")
        arrays: dict[str, xr.DataArray] = {}
        for name, (var, how) in wanted.items():
            da = preds[var].rename(None)
            if name in discrete:
                da = discrete[name].draw(preds, trace, da)
            da = da.assign_coords({d: ds[d].values for d in da.dims if d in ds.coords})
            da.attrs = {"kind": how, "noise": noise[name]}
            arrays[name] = da
        pred = xr.Dataset(arrays)
        drop = [c for c in pred.coords if c not in pred.dims]
        return xr.DataTree.from_dict({PREDICTIONS: pred.drop_vars(drop)})

    def _targets(self, model: pm.Model, kind: str) -> dict[str, tuple[str, str]]:
        """``{output: (model variable, 'expected' | 'predictive')}`` for this request."""
        observed = {rv.name for rv in model.observed_RVs}
        targets: dict[str, tuple[str, str]] = {}
        for out in self.manifest().outputs:
            obs = self.predictive.get(out, out)
            if kind == "predictive" and obs in observed:
                targets[out] = (obs, "predictive")
            elif out in self.expected:
                targets[out] = (self.expected[out], "expected")
        if not targets:
            raise MapiError(
                f"model {self.id!r} cannot produce {kind!r} outputs: declare expected = "
                "{output: deterministic} (and observed variables named like the outputs)",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer="/output/kind",
            )
        return targets

    # ------------------------------------------------------------------ fast scoring
    def check_fast_score(self, fitted: Fitted, ds: xr.Dataset, *, seed: int = 0) -> str:
        """Contract-kit hook (``check_model``'s ``fast_score_matches_exact``): the frozen
        predictor agrees with exact scoring on model-stage ``ds`` within MCSE.

        Raises ``AssertionError`` when it does not (see :func:`mapi_pymc.testing.
        check_fast_score`); returns a one-line summary otherwise.
        """
        from mapi_pymc.testing import check_fast_score

        report = check_fast_score(self, fitted, ds, seed=seed)
        if not report.max_z:
            raise AssertionError(
                "fast_score_spec.response_exprs name no output that exact scoring returns"
            )
        worst = ", ".join(f"{k}={v:.2f}" for k, v in sorted(report.max_z.items()))
        return (
            f"fast == exact within {report.n_sigma:g} MCSE over {report.samples} samples "
            f"(max |z|: {worst})"
        )

    def frozen_predictor(
        self,
        fitted: Fitted,
        ds: xr.Dataset,
        spec: FrozenPredictorSpec | None = None,
        *,
        num_samples: int | None = None,
        seed: int | None = None,
    ) -> FrozenPredictor:
        """Compile a :class:`~mapi_pymc.frozen.FrozenPredictor` from ``fast_score_spec``.

        ``ds`` is a model-stage dataset fixing the non-input data (canonicalised score data
        or the fit data); the predictor then varies only ``spec.input_vars``.
        """
        spec = spec or self.fast_score_spec
        if spec is None:
            raise MapiError(
                f"model {self.id!r} declares no fast_score_spec",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
            )
        s = self.parse_settings(fitted.settings)
        bctx = self._context(
            "score",
            ds,
            schema=fitted.schema,
            policies={},
            fit_coords=fitted.coords,
            seed=seed or 0,
        )
        model = self.build_model(ds, s, bctx)
        return create_frozen_predictor(
            model,
            fitted.posterior,
            spec.response_exprs,
            spec.input_vars,
            num_samples=num_samples or spec.num_samples,
            random_seed=seed,
            reducer=spec.reducer,
        )


# ============================================================================== helpers


@contextmanager
def _quiet() -> Iterator[None]:
    """Silence PyMC's INFO logging ("Sampling: [...]") and warnings around library calls."""
    logger = logging.getLogger("pymc")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logger.setLevel(level)


def _window(ctx: FitContext, lo: float, hi: float) -> Callable[[float, str], None] | None:
    if ctx.on_progress is None:
        return None

    def report(fraction: float, message: str) -> None:
        ctx.progress(lo + (hi - lo) * fraction, message)

    return report


def _seed_int(seed: int, name: str) -> int:
    digest = hashlib.sha256(f"{seed}:{name}".encode()).digest()
    return int.from_bytes(digest[:4], "little")


def _noise_rng(seed: int, name: str) -> np.random.Generator:
    digest = hashlib.sha256(name.encode()).digest()
    return np.random.default_rng([int(seed), int.from_bytes(digest[:8], "little")])


def _child(tree: xr.DataTree, name: str) -> xr.Dataset:
    node = tree[name]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset(inherit=False)


def _named(
    values: Mapping[str, np.ndarray], dims: Mapping[str, tuple[str, ...]], ds: xr.Dataset
) -> xr.Dataset:
    out = {}
    for name, arr in values.items():
        d = dims.get(name, ())
        out[name] = xr.DataArray(
            arr, dims=list(d), coords={k: ds[k].values for k in d if k in ds.coords}
        )
    return xr.Dataset(out)


def _clean(ds: xr.Dataset, engine: str) -> xr.Dataset:
    """Bundle-safe ArviZ group: no object arrays, no volatile attributes (timestamps)."""
    keep = [v for v in ds.data_vars if ds[v].dtype.kind in "biufcMm" or ds[v].dtype.kind == "U"]
    out = ds[keep]
    fixes = {}
    for c in out.coords:
        if out[c].dtype.kind == "O":
            vals = np.asarray(out[c].values.tolist())
            fixes[c] = (out[c].dims, vals if vals.dtype.kind != "O" else vals.astype(str))
    if fixes:
        out = out.assign_coords(fixes)
    for v in out.variables:
        out[v].attrs = {}
    out.attrs = {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": engine,
    }
    return out


def _add_crn(model: pm.Model, obs: str, bctx: BuildContext) -> str | None:
    """Add ``obs__crn = icdf(obs, U)`` with frozen uniforms ``U``; ``None`` if unavailable."""
    rv = model[obs]
    dims = model.named_vars_to_dims.get(obs)
    if not dims:
        return None
    try:
        pm.icdf(rv, pt.constant(0.5, dtype=rv.dtype), warn_rvs=False)
    except NotImplementedError:
        return None
    u = bctx.frozen_noise(f"{obs}{CRN_U_SUFFIX}", dims=dims, kind="uniform")
    pm.Deterministic(f"{obs}{CRN_SUFFIX}", pm.icdf(rv, u, warn_rvs=False), dims=dims)
    return f"{obs}{CRN_SUFFIX}"


_DISCRETE_PPF: dict[str, Callable[..., np.ndarray]] = {
    "poisson": lambda u, mu: scipy.stats.poisson.ppf(u, mu),
    "negative_binomial": lambda u, n, p: scipy.stats.nbinom.ppf(u, n, p),
    "binomial": lambda u, n, p: scipy.stats.binom.ppf(u, n, p),
}


@dataclass(frozen=True, slots=True)
class _DiscreteCrn:
    """Common random numbers for a discrete observed variable without a symbolic icdf:
    its parameters are sampled as Deterministics and the frozen uniforms ``u`` go through
    the family's numeric inverse CDF (``scipy.stats.<family>.ppf``)."""

    family: str
    params: tuple[str, ...]
    u: str

    def draw(self, preds: xr.Dataset, trace: xr.Dataset, like: xr.DataArray) -> xr.DataArray:
        args = [np.asarray(preds[p].transpose(*like.dims).values) for p in self.params]
        u = np.asarray(trace[self.u].values).reshape(args[0].shape)
        with np.errstate(invalid="ignore"):
            values = _DISCRETE_PPF[self.family](u, *args)
        return like.copy(data=np.asarray(values, dtype=np.float64))


def _add_discrete_crn(model: pm.Model, obs: str, bctx: BuildContext) -> _DiscreteCrn | None:
    """Register sampled parameters and frozen uniforms for a known discrete family."""
    rv = model[obs]
    dims = model.named_vars_to_dims.get(obs)
    op = getattr(rv.owner, "op", None)
    family = getattr(op, "name", None)
    if not dims or op is None or family not in _DISCRETE_PPF:
        return None
    try:
        params = op.dist_params(rv.owner)
    except (AttributeError, TypeError, ValueError):
        return None
    names: list[str] = []
    for i, param in enumerate(params):
        name = f"{obs}{CRN_SUFFIX}_p{i}"
        pm.Deterministic(name, pt.broadcast_to(param, rv.shape), dims=dims)
        names.append(name)
    u = f"{obs}{CRN_U_SUFFIX}"
    bctx.frozen_noise(u, dims=dims, kind="uniform")
    return _DiscreteCrn(str(family), tuple(names), u)


def _trace(
    model: pm.Model, posterior: xr.Dataset, bctx: BuildContext
) -> tuple[xr.Dataset, list[str]]:
    """Posterior values of the score model's free RVs plus seeded frozen noise."""
    chains = int(posterior.sizes["chain"])
    draws = int(posterior.sizes["draw"])
    data: dict[str, Any] = {}
    for rv in model.free_RVs:
        name = rv.name
        dims = model.named_vars_to_dims.get(name, ())
        shape = tuple(len(model.coords[d]) for d in dims) if dims else None
        if name in bctx.frozen:
            spec = bctx.frozen[name]
            assert shape is not None
            rng = _noise_rng(bctx.seed, name)
            size = (chains, draws, *shape)
            values = rng.uniform(size=size) if spec.kind == "uniform" else rng.standard_normal(size)
            data[name] = (("chain", "draw", *[f"{name}__d{i}" for i in range(len(shape))]), values)
            continue
        if name not in posterior:
            continue
        arr = np.asarray(posterior[name].transpose("chain", "draw", ...).values)
        if shape is not None and tuple(arr.shape[2:]) != shape:
            changed = [
                d for d, n in zip(dims, shape, strict=True) if n != _fit_len(posterior, name, d)
            ]
            new_dims = [d for d in changed if bctx.new_levels(d).size]
            raise MapiError(
                f"parameter {name!r} is indexed by {list(dims)} with shape {shape} at score time "
                f"but {tuple(arr.shape[2:])} in the posterior"
                + (
                    f"; {new_dims} hold new levels - use ctx.hier_effect() for effects over "
                    "new_levels dims"
                    if new_dims
                    else "; parameters must not be indexed by dims that change between fit "
                    "and score"
                ),
                code=ErrorCode.UNSEEN_LEVELS if new_dims else ErrorCode.COORD_NOT_ALLOWED,
            )
        data[name] = (("chain", "draw", *[f"{name}__d{i}" for i in range(arr.ndim - 2)]), arr)
    trace = xr.Dataset(
        data,
        coords={"chain": posterior["chain"].values, "draw": posterior["draw"].values},
    )
    return trace, sorted(data)


def _fit_len(posterior: xr.Dataset, name: str, dim: str) -> int:
    dims = [str(d) for d in posterior[name].dims if d not in ("chain", "draw")]
    for d in dims:
        if d == dim or d.startswith(f"{dim}__"):
            return int(posterior.sizes[d])
    return -1
