"""The pipeline engine: stages, fitting, score-time replay and the inverse chain (plan §4.2).

A *stage* is the dataset after a named step (``raw`` is the bound input before any step).
:meth:`Pipeline.fit` fits and transforms a range of stages, recording each step's state and
its per-group context tail at the step's **input** stage. :meth:`Pipeline.transform`
replays stored states over a range of stages, prepending each step's context before it runs
(:mod:`mapi_sdk.context`). :meth:`Pipeline.inverse_outputs` maps model outputs back through
invertible VALUE steps up to the last RESHAPE step and labels the output space.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import xarray as xr
from mapi_spec.capabilities import Representation
from mapi_spec.effects import ContextNeed, StepKind
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import ModelSpec
from mapi_spec.validate import ResolvedSpec

from mapi_sdk.context import ContextMark, extract_context, prepend_context
from mapi_sdk.errors import StepError
from mapi_sdk.schema import infer_freq
from mapi_sdk.steps import CancelToken, Step, StepContext, StepSettings, StepState

if TYPE_CHECKING:
    from mapi_sdk.registry import Registry

__all__ = [
    "RAW",
    "FitTrace",
    "InverseResult",
    "Pipeline",
    "PipelineStep",
    "ReplayTrace",
    "stage_coords",
]

RAW = "raw"
Retransform = Literal["median", "smearing"]


def stage_coords(ds: xr.Dataset) -> xr.Dataset:
    """Coordinates-only view of ``ds`` (one index coordinate per labelled dim)."""
    return xr.Dataset(coords={d: ds[d].variable for d in ds.dims if d in ds.coords})


@dataclass(slots=True)
class PipelineStep:
    """A step instance bound to a pipeline position and concrete settings."""

    id: str
    step: Step[Any, Any]
    settings: StepSettings
    use: str = ""

    @property
    def kind(self) -> StepKind:
        return self.step.kind

    @property
    def need(self) -> ContextNeed | None:
        return self.step.context(self.settings)

    @property
    def ref(self) -> str:
        return self.step.ref()


@dataclass(slots=True)
class FitTrace:
    """Result of :meth:`Pipeline.fit`."""

    data: xr.Dataset
    states: dict[str, StepState]
    coords: dict[str, xr.Dataset]
    stages: dict[str, xr.Dataset] = field(default_factory=dict)


@dataclass(slots=True)
class ReplayTrace:
    """Result of :meth:`Pipeline.transform`."""

    data: xr.Dataset
    marks: list[ContextMark] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    stages: dict[str, xr.Dataset] = field(default_factory=dict)


@dataclass(slots=True)
class InverseResult:
    """Outputs mapped back to their output space."""

    data: xr.Dataset
    space: dict[str, str] = field(default_factory=dict)
    retransform: dict[str, str] = field(default_factory=dict)
    kept: dict[str, list[str]] = field(default_factory=dict)
    """Decompositions left in model space -> the non-additive steps that blocked them."""


class Pipeline:
    """An ordered list of :class:`PipelineStep` with optional per-stage schemas."""

    def __init__(
        self,
        steps: Sequence[PipelineStep],
        *,
        schemas: Mapping[str, DataSchema | None] | None = None,
    ) -> None:
        ids = [s.id for s in steps]
        if len(set(ids)) != len(ids) or RAW in ids:
            raise ValueError("step ids must be unique and must not be 'raw'")
        self.steps = list(steps)
        self.schemas: dict[str, DataSchema | None] = dict(schemas or {})

    # --------------------------------------------------------------- construction
    @classmethod
    def build(
        cls,
        items: Sequence[tuple[str, type[Step[Any, Any]] | Step[Any, Any], Any]],
        *,
        schemas: Mapping[str, DataSchema | None] | None = None,
    ) -> Pipeline:
        """Build from ``(step_id, step class or instance, settings)`` triples."""
        steps: list[PipelineStep] = []
        for sid, step, settings in items:
            inst = step() if isinstance(step, type) else step
            steps.append(PipelineStep(sid, inst, inst.parse_settings(settings), inst.ref()))
        return cls(steps, schemas=schemas)

    @classmethod
    def from_spec(
        cls, spec: ModelSpec, registry: Registry, resolved: ResolvedSpec | None = None
    ) -> Pipeline:
        """Instantiate a spec's pipeline from a registry (settings with defaults applied)."""
        steps: list[PipelineStep] = []
        for i, call in enumerate(spec.pipeline):
            if resolved is not None:
                rs = resolved.steps[i]
                step_cls = registry.step(rs.resolved)
                raw_settings: Mapping[str, Any] = rs.settings
            else:
                step_cls = registry.step(str(call.use))
                raw_settings = call.with_
            inst = step_cls()
            steps.append(
                PipelineStep(call.id, inst, inst.parse_settings(raw_settings), str(call.use))
            )
        schemas = dict(resolved.stages) if resolved is not None else None
        return cls(steps, schemas=schemas)

    # --------------------------------------------------------------------- stages
    @property
    def stage_names(self) -> list[str]:
        return [RAW, *(s.id for s in self.steps)]

    def index(self, stage: str) -> int:
        """Position of ``stage`` (0 = raw, i = after the i-th step)."""
        try:
            return self.stage_names.index(stage)
        except ValueError:
            raise MapiError(
                f"unknown stage {stage!r}", code=ErrorCode.REF_UNRESOLVED, pointer="/inputs_stage"
            ) from None

    def input_stage(self, step_id: str) -> str:
        return self.stage_names[self.index(step_id) - 1]

    def between(self, start: str = RAW, end: str | None = None) -> list[PipelineStep]:
        """Steps executed to go from stage ``start`` to stage ``end`` (default: last)."""
        lo = self.index(start)
        hi = len(self.steps) if end is None else self.index(end)
        if hi < lo:
            raise ValueError(f"stage {end!r} comes before stage {start!r}")
        return self.steps[lo:hi]

    @property
    def final_stage(self) -> str:
        return self.stage_names[-1]

    @property
    def last_reshape_stage(self) -> str:
        last = RAW
        for s in self.steps:
            if s.kind is StepKind.RESHAPE:
                last = s.id
        return last

    def schema_at(self, stage: str) -> DataSchema | None:
        return self.schemas.get(stage)

    def step(self, step_id: str) -> PipelineStep:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def _ctx(
        self,
        ps: PipelineStep,
        phase: Literal["fit", "transform", "score", "inverse"],
        side_tables: Mapping[str, xr.Dataset] | None,
        seed: int,
        cancel: CancelToken | None,
        log: Callable[[str], None] | None,
    ) -> StepContext:
        return StepContext(
            step_id=ps.id,
            phase=phase,
            schema=self.schema_at(self.input_stage(ps.id)),
            side_tables=side_tables or {},
            seed=seed,
            cancel=cancel,
            log=log,
        )

    @staticmethod
    def _call(ps: PipelineStep, what: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except MapiError:
            raise
        except Exception as exc:
            raise StepError(
                f"step {ps.id!r} ({ps.ref}) failed during {what}",
                untrusted_text=f"{type(exc).__name__}: {exc}"[:8000],
            ) from exc

    # ------------------------------------------------------------------------ fit
    def fit(
        self,
        ds: xr.Dataset,
        *,
        start: str = RAW,
        end: str | None = None,
        side_tables: Mapping[str, xr.Dataset] | None = None,
        seed: int = 0,
        cancel: CancelToken | None = None,
        log: Callable[[str], None] | None = None,
        keep_stages: bool = False,
    ) -> FitTrace:
        """Fit and transform the steps from ``start`` to ``end``.

        Each step's state is learned on its input, its context tail is recorded at that
        input stage, and the transformed data feeds the next step.
        """
        states: dict[str, StepState] = {}
        coords = {start: stage_coords(ds)}
        stages = {start: ds} if keep_stages else {}
        for ps in self.between(start, end):
            if cancel is not None:
                cancel.check()
            ctx = self._ctx(ps, "fit", side_tables, seed, cancel, log)
            state = self._call(ps, "fit", ps.step.fit, ds, ps.settings, ctx)
            if not isinstance(state, StepState):
                raise StepError(f"step {ps.id!r} fit() must return a StepState")
            need = ps.need
            context = self._call(ps, "context recording", extract_context, ds, need)
            state = state.model_copy(
                update={
                    "step_id": ps.id,
                    "step_type": ps.step.id,
                    "step_version": ps.step.version,
                    "settings_hash": ps.step.settings_hash(ps.settings),
                    "context": context,
                    "context_need": need,
                }
            )
            states[ps.id] = state
            tctx = self._ctx(ps, "transform", side_tables, seed, cancel, log)
            out = self._call(ps, "transform", ps.step.transform, ds, ps.settings, state, tctx)
            if not isinstance(out, xr.Dataset):
                raise StepError(f"step {ps.id!r} transform() must return an xarray Dataset")
            ds = out
            coords[ps.id] = stage_coords(ds)
            if keep_stages:
                stages[ps.id] = ds
        return FitTrace(ds, states, coords, stages)

    # ------------------------------------------------------------------ transform
    def transform(
        self,
        ds: xr.Dataset,
        states: Mapping[str, StepState],
        *,
        start: str = RAW,
        end: str | None = None,
        replay: bool = True,
        side_tables: Mapping[str, xr.Dataset] | None = None,
        seed: int = 0,
        cancel: CancelToken | None = None,
        log: Callable[[str], None] | None = None,
        keep_stages: bool = False,
    ) -> ReplayTrace:
        """Replay stored states from ``start`` to ``end``; never learns.

        With ``replay=True`` each step's stored context is prepended (per group, checked
        for contiguity) before it runs; the returned marks say which rows to drop later.
        """
        marks: list[ContextMark] = []
        issues: list[Issue] = []
        stages = {start: ds} if keep_stages else {}
        for ps in self.between(start, end):
            if cancel is not None:
                cancel.check()
            if ps.id not in states:
                raise StepError(f"no fitted state for step {ps.id!r}")
            state = states[ps.id]
            if replay and state.context is not None:
                need = state.context_need or ps.need
                freq = self._freq(ps, need, state.context)
                ds, mark, found = prepend_context(ds, state.context, need, freq=freq, step_id=ps.id)
                issues += found
                if mark is not None:
                    marks.append(mark)
            ctx = self._ctx(ps, "score", side_tables, seed, cancel, log)
            out = self._call(ps, "transform", ps.step.transform, ds, ps.settings, state, ctx)
            if not isinstance(out, xr.Dataset):
                raise StepError(f"step {ps.id!r} transform() must return an xarray Dataset")
            ds = out
            if keep_stages:
                stages[ps.id] = ds
        return ReplayTrace(ds, marks, issues, stages)

    def _freq(self, ps: PipelineStep, need: ContextNeed | None, context: xr.Dataset) -> str | None:
        if need is None:
            return None
        schema = self.schema_at(self.input_stage(ps.id))
        if schema is not None and need.along in schema.dims and schema.dims[need.along].freq:
            return schema.dims[need.along].freq
        if need.along in context.coords and context[need.along].dtype.kind == "M":
            return infer_freq(np.asarray(context[need.along].values))
        return None

    # -------------------------------------------------------------------- inverse
    def inverse_chain(
        self, src: str, *, to_stage: str, from_stage: str | None = None
    ) -> tuple[list[PipelineStep], str]:
        """Invertible steps to apply to ``src`` (newest first) and the stage reached.

        Walks back from ``from_stage`` and stops at ``to_stage``, at any RESHAPE step, or at
        the first non-invertible step that changes ``src``.
        """
        reached = from_stage or self.final_stage
        chain: list[PipelineStep] = []
        for ps in reversed(self.between(to_stage, from_stage)):
            if ps.kind is StepKind.RESHAPE:
                break  # reshapes change every variable structurally; never invert past them
            if src not in ps.step.touched_vars(ps.settings):
                reached = self.input_stage(ps.id)
                continue
            if not ps.step.invertible:
                break
            chain.append(ps)
            reached = self.input_stage(ps.id)
        return chain, reached

    def inverse_outputs(
        self,
        ds: xr.Dataset,
        states: Mapping[str, StepState],
        var_sources: Mapping[str, str],
        *,
        to_stage: str | None = None,
        from_stage: str | None = None,
        representation: Representation = Representation.DRAWS,
        retransform: Retransform = "median",
        residuals: Mapping[str, xr.DataArray] | None = None,
        side_tables: Mapping[str, xr.Dataset] | None = None,
        decompositions: Collection[str] = (),
    ) -> InverseResult:
        """Map output variables back to their output space.

        ``var_sources`` maps each output variable to the input variable whose transforms
        it inherits (``y_hat -> y``). Inversion stops at ``to_stage`` (default: the last
        RESHAPE stage) or at the first non-invertible step that touched the variable.
        Point outputs through a non-linear inverse are labelled ``median`` (plain inverse)
        or use Duan's smearing over stored model-space ``residuals``; moments use the
        delta method for ``sd``.

        ``decompositions`` name additive decompositions (``contributions(component, ...)``).
        They are inverted component by component only through *additive* steps
        (``Step.additive``); through any other inverse (``expm1``, ``x * sd + mean``) the
        components would no longer sum to the inverted prediction, so inversion stops right
        after that step and :attr:`InverseResult.kept` says why.
        """
        target_stage = self.last_reshape_stage if to_stage is None else to_stage
        out = ds.copy()
        result = InverseResult(out)
        for out_var, src in var_sources.items():
            if out_var not in ds:
                continue
            chain, reached = self.inverse_chain(src, to_stage=target_stage, from_stage=from_stage)

            def run(
                x: xr.DataArray, chain: list[PipelineStep] = chain, src: str = src
            ) -> xr.DataArray:
                work = xr.Dataset({src: x})
                for ps in chain:
                    ctx = self._ctx(ps, "inverse", side_tables, 0, None, None)
                    st = states[ps.id]
                    work = self._call(ps, "inverse", ps.step.inverse, work, ps.settings, st, ctx)
                return work[src]

            da = ds[out_var]
            nonlinear = any(not ps.step.linear for ps in chain)
            label = "exact"
            if out_var in decompositions:
                # invert through the newest additive steps only; stop right after the
                # first step whose inverse would break the sum of the components
                k = next((j for j, ps in enumerate(chain) if not ps.step.additive), None)
                if k is not None:
                    result.kept[out_var] = [ps.id for ps in chain[k:] if not ps.step.additive]
                    reached = chain[k].id
                    chain = chain[:k]
            if not chain:
                inv = da
                label = "none"
            elif representation is Representation.MOMENTS and "moment" in da.dims:
                mean, sd = da.sel(moment="mean", drop=True), da.sel(moment="sd", drop=True)
                m = run(mean)
                sd_out = abs(run(mean + sd) - m)
                inv = xr.concat([m, sd_out], dim="moment").assign_coords(moment=["mean", "sd"])
                inv = inv.transpose(*da.dims)
                label = "delta_method" if nonlinear else "exact"
            elif (
                representation is Representation.POINT
                and nonlinear
                and retransform == "smearing"
                and residuals is not None
                and src in residuals
            ):
                r = residuals[src]
                r = r.rename({r.dims[0]: "__smear__"}) if r.dims else r.expand_dims("__smear__")
                inv = run(da + r).mean("__smear__")
                label = "smearing"
            else:
                inv = run(da)
                if representation is Representation.POINT and nonlinear:
                    label = "median"
            attrs = dict(da.attrs)
            schema = self.schema_at(reached)
            units = None
            if schema is not None and src in schema.vars:
                units = schema.vars[src].units
            if units is not None:
                attrs["units"] = units
            else:
                attrs.pop("units", None)
            freqs = {
                str(d): schema.dims[str(d)].freq
                for d in inv.dims
                if schema is not None and str(d) in schema.dims and schema.dims[str(d)].freq
            }
            if freqs:
                attrs["freq"] = freqs
            attrs["output_space"] = reached
            attrs["retransform"] = label
            inv.attrs = attrs
            out[out_var] = inv
            result.space[out_var] = reached
            result.retransform[out_var] = label
        result.data = out
        return result
