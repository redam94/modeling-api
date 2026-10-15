"""FrozenPredictor: one compiled PyTensor function for fast posterior predictions (plan §5).

Ported from ``pymc-model-interactivity/frozen_predictor.py`` to PyMC 6 / PyTensor 3 and
ArviZ 1.x (posteriors are ``xr.DataTree`` / ``xr.Dataset``). Given a model, posterior
samples, response expressions and the ``pm.Data`` inputs to vary, it compiles a single
function vectorised over posterior samples and scenarios:

* only the free random variables that are ancestors of the responses are bound, to constant
  posterior samples (so compilation stays small);
* observed random variables upstream of a response are replaced by their observed values
  when they do not depend on the inputs, and by ``icdf(dist, U)`` with **frozen** uniform
  quantiles ``U`` (one per sample) when they do - common random numbers across scenarios;
* ``SpecifyShape`` constraints on dims that only the inputs use are removed, so inputs may
  change length (dims shared with other model variables stay fixed).

Inputs may have the data variable's own shape (one scenario) or an extra leading scenario
axis. Outputs have shape ``(sample, [scenario,] *dims)`` before the optional reducer.

The plan's ``FrozenPredictorSpec(response_exprs, input_vars, reducer)`` is the JSON form a
plugin declares when ``capabilities.fast_score`` is true.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any

import numpy as np
import pymc as pm
import pytensor
import pytensor.tensor as pt
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import BaseModel, ConfigDict, Field
from pytensor.graph.basic import Variable
from pytensor.graph.replace import clone_replace, vectorize_graph
from pytensor.graph.traversal import ancestors
from pytensor.tensor.shape import SpecifyShape
from pytensor.tensor.variable import TensorVariable

__all__ = [
    "FrozenPredictor",
    "FrozenPredictorSpec",
    "Reducer",
    "create_frozen_predictor",
]

logger = logging.getLogger(__name__)

ExprSpec = str | TensorVariable | tuple[str | TensorVariable, Callable[[np.ndarray], np.ndarray]]


class Reducer(StrEnum):
    """How to collapse the sample axis of each response."""

    NONE = "none"
    MEAN = "mean"
    MEDIAN = "median"


class FrozenPredictorSpec(BaseModel):
    """Declarative fast-score recipe: ``{output: model variable}``, the ``pm.Data`` inputs to
    vary, and how to reduce over posterior samples."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    response_exprs: dict[str, str] = Field(..., min_length=1)
    input_vars: list[str] = Field(..., min_length=1)
    reducer: Reducer = Reducer.NONE
    num_samples: int | None = Field(None, ge=1, le=100_000)


# ============================================================================ graph analysis


def _find_needed_nodes(
    model: pm.Model, outputs: Sequence[Variable], input_var_names: Sequence[str]
) -> tuple[set[Variable], set[Variable], set[Variable], set[Variable]]:
    """Free RVs, observed RVs and inputs that the outputs depend on."""
    all_ancestors = set(ancestors(list(outputs)))
    needed_free = all_ancestors & set(model.free_RVs)
    needed_obs = all_ancestors & set(model.observed_RVs)
    used_inputs = {
        model.named_vars[n] for n in input_var_names if model.named_vars[n] in all_ancestors
    }
    unused = sorted(n for n in input_var_names if model.named_vars[n] not in all_ancestors)
    if unused:
        logger.warning("input variables %s do not affect the responses", unused)
    return needed_free, needed_obs, all_ancestors, used_inputs


def _find_protected_dims(
    model: pm.Model,
    outputs: Sequence[Variable],
    input_names: set[str],
    all_ancestors: set[Variable],
) -> tuple[set[str], set[str]]:
    """Dims of the inputs that other ancestors also use (these must keep their length)."""
    dims = model.named_vars_to_dims
    output_names = {o.name for o in outputs if o.name is not None}
    ancestor_names = {v.name for v in (all_ancestors - set(outputs)) if v.name in model.named_vars}
    input_dims = {d for n in input_names & dims.keys() for d in dims[n]}
    other_dims = {
        d for n in (ancestor_names - output_names - input_names) & dims.keys() for d in dims[n]
    }
    return input_dims & other_dims, input_dims


def _replace_observed_rvs(
    model: pm.Model,
    needed_obs: set[Variable],
    inputs: set[Variable],
    has_dynamic_dims: bool,
) -> tuple[dict[Variable, Variable], dict[Variable, TensorVariable]]:
    """Observed values for input-independent observed RVs; ICDF of frozen U otherwise."""
    replacements: dict[Variable, Variable] = {}
    uniforms: dict[Variable, TensorVariable] = {}
    for rv in needed_obs:
        assert rv.owner is not None
        depends = bool(set(ancestors(rv.owner.inputs[2:])) & inputs)
        if not depends:
            value = model.rvs_to_values[rv]
            data = value.get_value() if hasattr(value, "get_value") else value.eval()
            replacements[rv] = pt.constant(np.asarray(data).astype(rv.dtype), name=rv.name)
            continue
        if has_dynamic_dims and rv.type.shape and any(s is not None for s in rv.type.shape):
            logger.warning(
                "observed RV %r has the fixed shape %s; give it dims to allow other input sizes",
                rv.name,
                rv.type.shape,
            )
        u = pt.scalar(f"{rv.name}_uniform", dtype=rv.dtype)
        uniforms[rv] = u
        try:
            replacements[rv] = pm.icdf(rv, u, warn_rvs=False)
        except NotImplementedError as exc:
            raise MapiError(
                f"observed variable {rv.name!r} depends on the inputs but its distribution has "
                "no inverse CDF, which the frozen predictor needs",
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                untrusted_text=str(exc)[:2000],
            ) from exc
    return replacements, uniforms


def _remove_specify_shape(
    outputs: list[Variable],
    protected: set[str],
    named_dims: Mapping[str, tuple[str, ...]],
) -> list[Variable]:
    """Drop ``SpecifyShape`` on variables whose dims may change length."""
    replacements: dict[Variable, Variable] = {}
    for node in [*ancestors(outputs), *outputs]:
        owner = getattr(node, "owner", None)
        if owner is None or not isinstance(owner.op, SpecifyShape):
            continue
        tensor = owner.inputs[0]
        if tensor.name is not None and tensor.name in named_dims:
            if set(named_dims[tensor.name]) & protected:
                continue
        else:
            logger.debug("removing SpecifyShape on an unnamed tensor")
        replacements[node] = tensor
    if not replacements:
        return outputs
    return list(clone_replace(outputs, replace=replacements, rebuild_strict=False))


# ============================================================================ the predictor


def _samples(
    posterior: xr.DataTree | xr.Dataset,
    *,
    group: str,
    num_samples: int | None,
    random_seed: int | None,
) -> xr.Dataset:
    """Posterior samples stacked on a ``sample`` dim (chain-major), optionally subsampled."""
    if isinstance(posterior, xr.DataTree):
        node = posterior[group]
        assert isinstance(node, xr.DataTree)
        ds = node.to_dataset(inherit=False)
    else:
        ds = posterior
    if not {"chain", "draw"} <= set(ds.dims):
        raise MapiError("posterior samples need chain and draw dims", code=ErrorCode.SCHEMA_INVALID)
    chain = np.repeat(np.asarray(ds["chain"].values), ds.sizes["draw"])
    draw = np.tile(np.asarray(ds["draw"].values), ds.sizes["chain"])
    flat = ds.stack(sample=("chain", "draw")).drop_vars(["sample", "chain", "draw"])
    flat = flat.assign_coords(chain=("sample", chain), draw=("sample", draw))
    flat = flat.transpose("sample", ...)
    if num_samples is not None and num_samples < flat.sizes["sample"]:
        rng = np.random.default_rng(random_seed)
        idx = np.sort(rng.choice(flat.sizes["sample"], size=num_samples, replace=False))
        flat = flat.isel(sample=idx)
    return flat


class FrozenPredictor:
    """A compiled predictor; call it with input arrays or use :meth:`predict` with xarray."""

    def __init__(
        self,
        fn: Callable[..., list[np.ndarray]],
        *,
        output_names: list[str],
        output_dims: dict[str, tuple[str, ...]],
        input_vars: list[str],
        input_dims: dict[str, tuple[str, ...]],
        input_ndim: dict[str, int],
        post_fns: list[Callable[[np.ndarray], np.ndarray] | None],
        samples: xr.Dataset,
        symbolic: list[bool] | None = None,
        reducer: Callable[[np.ndarray], np.ndarray] | None = None,
    ) -> None:
        self._fn = fn
        self.output_names = output_names
        self.output_dims = output_dims
        self.input_vars = input_vars
        self.input_dims = input_dims
        self.input_ndim = input_ndim
        self._post_fns = post_fns
        self._symbolic = symbolic or [False] * len(output_names)
        self._reducer = reducer
        self.sample_chain = np.asarray(samples["chain"].values)
        self.sample_draw = np.asarray(samples["draw"].values)

    @property
    def num_samples(self) -> int:
        return int(self.sample_chain.size)

    def __call__(self, **inputs: Any) -> dict[str, np.ndarray]:
        """Evaluate at ``inputs`` (each the data var's shape, or with a leading scenario
        axis). Returns ``{output: (sample, [scenario,] *dims)}`` (reducers applied)."""
        missing = [n for n in self.input_vars if n not in inputs]
        if missing:
            raise MapiError(f"missing inputs {missing}", code=ErrorCode.MISSING_FUTURE_COVARIATE)
        first = np.asarray(inputs[self.input_vars[0]])
        single = first.ndim <= self.input_ndim[self.input_vars[0]]
        arrays = []
        for name in self.input_vars:
            arr = np.asarray(inputs[name], dtype=np.float64)
            nd = self.input_ndim[name]
            while arr.ndim < nd + 1:
                arr = arr[None, ...]
            arrays.append(arr)
        lead = np.broadcast_shapes(*[a.shape[:1] for a in arrays])
        arrays = [np.broadcast_to(a, lead + a.shape[1:]) for a in arrays]
        results = self._fn(*arrays)
        out: dict[str, np.ndarray] = {}
        for i, (name, res) in enumerate(zip(self.output_names, results, strict=True)):
            arr = np.asarray(res)
            if self._symbolic[i]:
                # reduced in-graph: the scenario axis may now lead; squeeze a singleton one
                if single and arr.ndim >= 1 and arr.shape[0] == 1:
                    arr = arr[0]
                elif single and arr.ndim >= 2 and arr.shape[1] == 1:
                    arr = arr[:, 0]
                out[name] = arr
                continue
            if single and arr.ndim >= 2:
                arr = arr[:, 0]
            post = self._post_fns[i] or self._reducer
            out[name] = post(arr) if post is not None else arr
        return out

    def predict(
        self, inputs: Mapping[str, xr.DataArray] | xr.Dataset, *, scenario_dim: str = "scenario"
    ) -> xr.Dataset:
        """xarray in, xarray out: outputs as ``(sample, [scenario,] *dims)`` with chain/draw
        coordinates on ``sample`` (no reducers - use the raw call for those)."""
        arrays: dict[str, np.ndarray] = {}
        has_scenario = False
        scen_coords: Any = None
        for name in self.input_vars:
            da = inputs[name]
            dims = list(self.input_dims.get(name, da.dims))
            if scenario_dim in da.dims:
                has_scenario = True
                scen_coords = da[scenario_dim].values if scenario_dim in da.coords else None
                da = da.transpose(scenario_dim, *dims)
            else:
                da = da.transpose(*dims)
            arrays[name] = np.asarray(da.values, dtype=np.float64)
        raw = {}
        for i, (name, res) in enumerate(
            zip(self.output_names, self._fn(*self._stack(arrays)), strict=True)
        ):
            if self._symbolic[i]:
                raise MapiError(
                    f"output {name!r} is reduced in-graph; call the predictor directly",
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                )
            raw[name] = np.asarray(res)
        out: dict[str, xr.DataArray] = {}
        for name, arr in raw.items():
            dims = list(self.output_dims.get(name, ()))
            if not has_scenario:
                arr = arr[:, 0]
                full = ["sample", *dims]
            else:
                full = ["sample", scenario_dim, *dims]
            if len(full) != arr.ndim:
                full = ["sample", *[f"{name}_dim_{i}" for i in range(arr.ndim - 1)]]
            out[name] = xr.DataArray(arr, dims=full)
        ds = xr.Dataset(out).assign_coords(
            chain=("sample", self.sample_chain), draw=("sample", self.sample_draw)
        )
        if has_scenario and scen_coords is not None:
            ds = ds.assign_coords({scenario_dim: scen_coords})
        return ds

    def _stack(self, arrays: Mapping[str, np.ndarray]) -> list[np.ndarray]:
        out = []
        for name in self.input_vars:
            arr = arrays[name]
            while arr.ndim < self.input_ndim[name] + 1:
                arr = arr[None, ...]
            out.append(arr)
        lead = np.broadcast_shapes(*[a.shape[:1] for a in out])
        return [np.broadcast_to(a, lead + a.shape[1:]) for a in out]


def create_frozen_predictor(
    model: pm.Model,
    posterior: xr.DataTree | xr.Dataset,
    response_exprs: Mapping[str, ExprSpec],
    input_vars: Sequence[str],
    *,
    group: str = "posterior",
    num_samples: int | None = None,
    random_seed: int | None = None,
    reducer: Reducer | str = Reducer.NONE,
) -> FrozenPredictor:
    """Compile a predictor of ``response_exprs`` at new values of ``input_vars``.

    ``response_exprs`` maps output names to a model variable name, a PyTensor expression of
    the model, or ``(expr, post_fn)`` where ``post_fn`` post-processes the numpy result.
    ``num_samples`` subsamples the posterior (seeded by ``random_seed``); ``random_seed``
    also seeds the frozen uniforms for input-dependent observed variables.
    """
    input_vars = list(input_vars)
    missing = sorted(set(input_vars) - set(model.named_vars))
    if missing:
        raise MapiError(
            f"input_vars not found in the model: {missing}", code=ErrorCode.SCHEMA_INVALID
        )
    red = Reducer(reducer)
    samples = _samples(posterior, group=group, num_samples=num_samples, random_seed=random_seed)

    names = list(response_exprs)
    outputs: list[Variable] = []
    post_fns: list[Callable[[np.ndarray], np.ndarray] | None] = []
    for spec in response_exprs.values():
        expr, post = spec if isinstance(spec, tuple) else (spec, None)
        outputs.append(model[expr] if isinstance(expr, str) else expr)
        post_fns.append(post)

    needed_free, needed_obs, all_anc, used_inputs = _find_needed_nodes(model, outputs, input_vars)
    protected, input_dims = _find_protected_dims(model, outputs, set(input_vars), all_anc)
    has_dynamic = not input_dims or bool(input_dims - protected)

    absent = sorted(rv.name for rv in needed_free if rv.name not in samples)
    if absent:
        raise MapiError(
            f"the posterior has no samples of {absent}; a frozen predictor cannot draw new "
            "latent variables (for example new levels)",
            code=ErrorCode.CAPABILITY_UNSUPPORTED,
        )
    rv_placeholders = {
        rv: pt.tensor(name=rv.name, shape=rv.type.shape, dtype=rv.dtype) for rv in needed_free
    }
    data_vars = {n: model.named_vars[n] for n in input_vars}
    ndims = {n: int(v.type.ndim) for n, v in data_vars.items()}
    sym_in = {
        n: pt.tensor(f"{n}_in", shape=(None,) * ndims[n], dtype="float64") for n in input_vars
    }
    sym_batch = {
        n: pt.tensor(f"{n}_batch", shape=(None,) * (ndims[n] + 1), dtype="float64")
        for n in input_vars
    }
    data_repl: dict[Variable, Variable] = {
        v: sym_in[n].astype(v.dtype) if v.dtype != "float64" else sym_in[n]
        for n, v in data_vars.items()
    }
    obs_repl, uniforms = _replace_observed_rvs(model, needed_obs, used_inputs, has_dynamic)
    base: dict[Variable, Variable] = {
        **rv_placeholders,
        **data_repl,
        **{u: u for u in uniforms.values()},
    }
    if obs_repl:
        cloned = clone_replace(list(obs_repl.values()), replace=base)
        obs_repl = dict(zip(obs_repl.keys(), cloned, strict=True))
    graph = clone_replace(outputs, replace={**base, **obs_repl})
    batched = vectorize_graph(graph, replace={sym_in[n]: sym_batch[n] for n in input_vars})

    sample_repl: dict[Variable, Variable] = {
        ph: pt.constant(np.asarray(samples[ph.name].values).astype(ph.dtype), name=ph.name)
        for ph in rv_placeholders.values()
    }
    urng = np.random.default_rng(random_seed if random_seed is not None else 42)
    for u in uniforms.values():
        sample_repl[u] = pt.constant(
            urng.uniform(size=samples.sizes["sample"]).astype(u.dtype), name=u.name
        )
    with_samples = vectorize_graph(batched, replace=sample_repl)
    final = _remove_specify_shape(list(with_samples), protected, dict(model.named_vars_to_dims))
    symbolic = [False] * len(final)
    for i, post in enumerate(post_fns):
        if post is None:
            continue
        try:
            sym = post(final[i])  # type: ignore[arg-type]
        except Exception:  # not a PyTensor-compatible function: apply it to numpy results
            continue
        if isinstance(sym, Variable):
            final[i] = sym
            symbolic[i] = True
    fn = pytensor.function([sym_batch[n] for n in input_vars], final, on_unused_input="ignore")
    out_dims: dict[str, tuple[str, ...]] = {}
    for name, var in zip(names, outputs, strict=True):
        if var.name is not None and var.name in model.named_vars_to_dims:
            out_dims[name] = tuple(model.named_vars_to_dims[var.name])
    in_dims = {
        n: tuple(model.named_vars_to_dims[n]) for n in input_vars if n in model.named_vars_to_dims
    }
    return FrozenPredictor(
        fn,
        output_names=names,
        output_dims=out_dims,
        input_vars=input_vars,
        input_dims=in_dims,
        input_ndim=ndims,
        post_fns=[None if symbolic[i] else p for i, p in enumerate(post_fns)],
        symbolic=symbolic,
        reducer=_reducer_fn(red),
        samples=samples,
    )


def _reducer_fn(red: Reducer) -> Callable[[np.ndarray], np.ndarray] | None:
    if red is Reducer.MEAN:
        return lambda a: np.mean(a, axis=0)
    if red is Reducer.MEDIAN:
        return lambda a: np.median(a, axis=0)
    return None
