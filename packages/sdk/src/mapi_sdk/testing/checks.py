"""Contract-kit checks for step and model authors (plan §13).

:func:`check_step` and :func:`check_model` run every contract check that is possible
without a sandbox and return a :class:`ContractReport` (raising
:class:`ContractViolation` by default when any check fails). Each check is independent: one
failure never hides the others.
"""

from __future__ import annotations

import functools
import json
import math
import tempfile
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from jsonschema import Draft202012Validator
from mapi_spec.capabilities import (
    REPRESENTATION_RULES,
    SCORE_MODES,
    Aggregation,
    OutputSelector,
    Representation,
    can_aggregate,
)
from mapi_spec.effects import StepKind, apply_effect
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.loader import load_model_spec
from mapi_spec.schema import DataSchema, DimPolicy
from mapi_spec.spec import AsOfSplit, ModelSpec, ScorePolicy, ScoreRequest, TimeHoldoutSplit

from mapi_sdk._util import dtype_family
from mapi_sdk.bundle import Bundle, assert_no_pickle
from mapi_sdk.context import drop_context, extract_context, prepend_context
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.metrics import point_estimate
from mapi_sdk.pipeline import Pipeline
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import infer_schema
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState, states_equal
from mapi_sdk.wire import read_datatree_zip, write_datatree_zip, write_zarr_zip

__all__ = [
    "CheckResult",
    "ContractReport",
    "ContractViolation",
    "ModelFixture",
    "check_model",
    "check_step",
]


class ContractViolation(AssertionError):
    """Raised when a contract check fails; the report is attached."""

    def __init__(self, report: ContractReport) -> None:
        super().__init__(report.summary())
        self.report = report


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False


@dataclass(slots=True)
class ContractReport:
    """Outcome of a contract-kit run."""

    subject: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.passed or r.skipped for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and not r.skipped]

    def get(self, name: str) -> CheckResult:
        for r in self.results:
            if r.name == name:
                return r
        raise KeyError(name)

    def summary(self) -> str:
        lines = [f"contract kit: {self.subject}"]
        for r in self.results:
            mark = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
            lines.append(f"  [{mark}] {r.name}" + (f": {r.detail}" if r.detail else ""))
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "ok": self.ok,
            "results": [
                {"name": r.name, "passed": r.passed, "skipped": r.skipped, "detail": r.detail}
                for r in self.results
            ],
        }

    def raise_for_failures(self) -> None:
        if not self.ok:
            raise ContractViolation(self)


class _Skip(Exception):
    """Raised inside a check to mark it skipped (not applicable)."""


def _run(report: ContractReport, name: str, fn: Callable[[], str | None]) -> None:
    try:
        detail = fn() or ""
        report.results.append(CheckResult(name, True, detail))
    except _Skip as exc:
        report.results.append(CheckResult(name, True, str(exc), skipped=True))
    except Exception as exc:
        tb = traceback.format_exception_only(type(exc), exc)[-1].strip()
        report.results.append(CheckResult(name, False, tb[:2000]))


def _expect_code(code: ErrorCode, fn: Callable[[], Any]) -> str:
    try:
        fn()
    except MapiError as exc:
        if exc.code is not code:
            raise AssertionError(
                f"expected {code.value}, got {exc.code.value}: {exc.detail}"
            ) from exc
        return f"raised {code.value}"
    raise AssertionError(f"expected {code.value} but nothing was raised")


# ============================================================================================ steps


def _state_roundtrip(state: StepState, cls: type[StepState]) -> StepState:
    nodes = {"/": xr.Dataset(attrs={"state": json.dumps(state.json_payload())})}
    if state.arrays is not None:
        nodes["/arrays"] = state.arrays
    if state.context is not None:
        nodes["/context"] = state.context
    raw = write_datatree_zip(xr.DataTree.from_dict(nodes))
    assert raw is not None
    tree = read_datatree_zip(raw)
    arrays = tree["arrays"].to_dataset() if "arrays" in tree else None
    context = tree["context"].to_dataset() if "context" in tree else None
    return cls.from_payload(json.loads(tree.attrs["state"]), arrays, context)


def _schema_shape(schema: DataSchema) -> dict[str, Any]:
    return {
        "dims": sorted(schema.dims),
        "vars": {
            n: (sorted(v.dims), dtype_family(v.dtype if v.dtype != "str" else np.dtype("U1")))
            for n, v in schema.vars.items()
        },
    }


def check_step(
    step: type[Step[Any, Any]] | Step[Any, Any],
    ds: xr.Dataset,
    settings: Mapping[str, Any] | StepSettings | None = None,
    *,
    schema: DataSchema | None = None,
    side_tables: Mapping[str, xr.Dataset] | None = None,
    holdout: int = 2,
    raise_on_failure: bool = True,
) -> ContractReport:
    """Run the step contract checks on ``ds`` (the step's input) with ``settings``.

    ``schema`` describes ``ds`` (inferred when omitted); targets in it are used for the
    "absent target" check. ``holdout`` is the number of trailing rows used for the
    context-window check.
    """
    inst = step() if isinstance(step, type) else step
    s = inst.parse_settings(settings)
    tables = dict(side_tables or {})
    report = ContractReport(f"step {inst.ref()}")
    in_schema = schema or infer_schema(ds)

    def ctx(phase: str = "fit") -> StepContext:
        return StepContext(step_id="check", phase=phase, schema=in_schema, side_tables=tables)  # type: ignore[arg-type]

    state_holder: dict[str, Any] = {}

    def fitted() -> tuple[StepState, xr.Dataset]:
        if "state" not in state_holder:
            st = inst.fit(ds, s, ctx())
            state_holder["state"] = st
            state_holder["out"] = inst.transform(ds, s, st, ctx("transform"))
        return state_holder["state"], state_holder["out"]

    def catalog() -> str:
        entry = type(inst).catalog_entry()
        errors = list(
            Draft202012Validator(entry.settings_schema).iter_errors(inst.settings_json(s))
        )
        if errors:
            raise AssertionError(
                f"settings do not validate against the exported schema: {errors[0].message}"
            )
        raw_props = entry.settings_schema.get("properties")
        props: dict[str, Any] = raw_props if isinstance(raw_props, dict) else {}

        def exported(name: str) -> bool:
            prop = props.get(name)
            return isinstance(prop, dict) and "default" in prop

        hidden = sorted(
            name
            for name, f in type(s).model_fields.items()
            if f.default_factory is not None and not exported(f.alias or name)
        )
        if hidden:
            raise AssertionError(
                f"the defaults of {hidden} are missing from the exported settings schema "
                "(default_factory); use plain defaults such as Field(default=[]) so the control "
                "plane can apply them before rendering the effect template"
            )
        return f"exported {entry.ref}"

    def fit_deterministic() -> None:
        a = inst.fit(ds, s, ctx())
        b = inst.fit(ds, s, ctx())
        if not isinstance(a, StepState):
            raise AssertionError("fit() must return a StepState")
        if not states_equal(a, b):
            raise AssertionError("fitting twice on the same data gave different states")

    def transform_pure() -> None:
        before = ds.copy(deep=True)
        st, out = fitted()
        again = inst.transform(ds, s, st, ctx("transform"))
        if not ds.identical(before):
            raise AssertionError("transform() modified its input dataset")
        if not again.identical(out):
            raise AssertionError("transform() is not deterministic")

    def matches_fit_output() -> None:
        pipe = Pipeline.build([("check", inst, s)])
        trace = pipe.fit(ds, side_tables=tables)
        replay = pipe.transform(ds, trace.states, replay=False, side_tables=tables)
        xr.testing.assert_identical(replay.data, trace.data)

    def transform_is_local() -> str:
        if inst.kind is StepKind.RESHAPE:
            raise _Skip("reshape steps aggregate rows")
        need = inst.context(s)
        dims = [str(d) for d in ds.dims if ds.sizes[d] >= 2 and (need is None or d != need.along)]
        if not dims:
            raise _Skip("no dim with at least two labels")
        st, full = fitted()
        dim = dims[0]
        half = ds.isel({dim: slice(0, ds.sizes[dim] // 2)})
        part = inst.transform(half, s, st, ctx("score"))
        expected = full.isel({dim: slice(0, ds.sizes[dim] // 2)})
        xr.testing.assert_allclose(part, expected)
        return f"transform of a subset along {dim!r} equals the subset of the transform"

    def state_roundtrip() -> None:
        pipe = Pipeline.build([("check", inst, s)])
        trace = pipe.fit(ds, side_tables=tables)
        st = trace.states["check"]
        back = _state_roundtrip(st, type(inst).State or StepState)
        if not states_equal(st, back):
            raise AssertionError("the state changed after a bundle round trip")
        out1 = inst.transform(ds, s, st, ctx("transform"))
        out2 = inst.transform(ds, s, back, ctx("transform"))
        xr.testing.assert_identical(out1, out2)
        json.dumps(st.json_payload())
        for arr in (st.arrays, st.context):
            if arr is not None:
                write_zarr_zip(arr)

    def effect_matches() -> str:
        eff = inst.effect(s)
        if eff.dynamic:
            raise _Skip("dynamic effect")
        expected, problems = apply_effect(in_schema, eff)
        if expected is None:
            raise AssertionError(f"effect does not apply to the input schema: {problems}")
        _, out = fitted()
        actual = infer_schema(out)
        want, got = _schema_shape(expected), _schema_shape(actual)
        if want != got:
            raise AssertionError(f"declared effect gives {want} but the output is {got}")
        return "output schema matches the declared SchemaEffect"

    def absent_target() -> str:
        targets = [t for t in in_schema.targets if t in ds]
        if not targets:
            raise _Skip("no target variables in the schema")
        st, _ = fitted()
        inst.transform(ds.drop_vars(targets), s, st, ctx("score"))
        return f"transform works without {targets}"

    def inverse() -> str:
        if not inst.invertible:
            raise _Skip("step is not invertible")
        st, out = fitted()
        touched = [v for v in inst.touched_vars(s) if v in ds and v in out]
        back = inst.inverse(out[touched], s, st, ctx("inverse"))
        for v in touched:
            np.testing.assert_allclose(
                back[v].values, ds[v].transpose(*back[v].dims).values, rtol=1e-6, atol=1e-9
            )
        draws = out[touched].expand_dims(draw=2)
        back2 = inst.inverse(draws, s, st, ctx("inverse"))
        for v in touched:
            if "draw" not in back2[v].dims:
                raise AssertionError(f"inverse dropped the leading draw dim of {v!r}")
        return f"inverse(transform(x)) == x for {touched}"

    def context_window() -> str:
        need = inst.context(s)
        if need is None or need.n == 0:
            raise _Skip("no context need")
        n = int(ds.sizes[need.along])
        k = min(holdout, n - 1)
        if k < 1:
            raise _Skip("not enough rows along the context dim")
        st, full = fitted()
        head, tail = (
            ds.isel({need.along: slice(0, n - k)}),
            ds.isel({need.along: slice(n - k, None)}),
        )
        freq = in_schema.dims[need.along].freq if need.along in in_schema.dims else None
        combined, mark, _ = prepend_context(tail, extract_context(head, need), need, freq=freq)
        out = inst.transform(combined, s, st, ctx("score"))
        out = drop_context(out, [mark] if mark else [])
        xr.testing.assert_allclose(out, full.isel({need.along: slice(n - k, None)}))
        return f"last {k} rows replayed with stored context equal the full transform"

    def unseen_levels() -> str:
        param = type(inst).unseen_levels_param
        if param is None:
            raise _Skip("step declares no unseen-level policy")
        st, _ = fitted()
        dims = sorted(
            {
                str(d)
                for arr in (st.arrays.data_vars.values() if st.arrays is not None else [])
                for d in arr.dims
                if d in ds.coords and ds[d].dtype.kind in "UO"
            }
        )
        if not dims:
            raise _Skip("state has no categorical dims")
        dim = dims[0]
        new = ds.isel({dim: [0]}).assign_coords({dim: ["__contract_new__"]})
        extended = xr.concat(
            [ds, new], dim=dim, data_vars="minimal", coords="minimal", compat="override"
        )
        raw = inst.settings_json(s)
        done = []
        for policy in ("error", "pooled", "fill"):
            ps = inst.parse_settings({**raw, param: policy})
            if policy == "error":
                _expect_code(
                    ErrorCode.UNSEEN_LEVELS,
                    functools.partial(inst.transform, extended, ps, st, ctx("score")),
                )
            else:
                out = inst.transform(extended, ps, st, ctx("score"))
                for v in out.data_vars:
                    if dim in out[v].dims and out[v].dtype.kind == "f":
                        vals = out[v].sel({dim: "__contract_new__"})
                        src = extended[v].sel({dim: "__contract_new__"}) if v in extended else None
                        if (
                            src is not None
                            and bool(np.isfinite(src).all())
                            and not bool(np.isfinite(vals).all())
                        ):
                            raise AssertionError(
                                f"policy {policy!r} left non-finite values in {v!r}"
                            )
            done.append(policy)
        return f"policies {done} behave on a new level of {dim!r}"

    _run(report, "catalog_entry", catalog)
    _run(report, "fit_deterministic", fit_deterministic)
    _run(report, "transform_pure_and_deterministic", transform_pure)
    _run(report, "transform_equals_fit_output", matches_fit_output)
    _run(report, "transform_never_learns", transform_is_local)
    _run(report, "state_bundle_roundtrip", state_roundtrip)
    _run(report, "effect_matches_output", effect_matches)
    _run(report, "absent_target_tolerated", absent_target)
    _run(report, "inverse_roundtrip", inverse)
    _run(report, "context_window_equivalence", context_window)
    _run(report, "unseen_level_policies", unseen_levels)
    if raise_on_failure:
        report.raise_for_failures()
    return report


# =========================================================================================== models


@dataclass(slots=True)
class ModelFixture:
    """A tiny end-to-end fixture for :func:`check_model`.

    ``spec`` must reference the plugin under test; keep fit settings tiny (few draws).
    """

    spec: ModelSpec | str
    data: xr.Dataset
    side_tables: Mapping[str, xr.Dataset] = field(default_factory=dict)
    raw_schema: DataSchema | None = None
    registry: Registry | None = None
    window: int | None = None

    def model_spec(self) -> ModelSpec:
        return load_model_spec(self.spec) if isinstance(self.spec, str) else self.spec


def _request(
    mode: str = "predict",
    *,
    horizon_dim: str | None = None,
    interventions: list[dict[str, Any]] | None = None,
    **output: Any,
) -> ScoreRequest:
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://contract/kit",
        "mode": mode,
        "output": OutputSelector.model_validate(output).model_dump(
            mode="json", exclude_defaults=True
        ),
        "seed": 11,
    }
    if mode in ("evaluate", "filter"):
        doc["data"] = "dataset://contract/kit@v1"
    if horizon_dim is not None:
        doc["horizon"] = {"dim": horizon_dim, "periods": 1}
    if interventions:
        doc["interventions"] = interventions
    return ScoreRequest.model_validate(doc)


def _summary(tree: xr.DataTree) -> xr.Dataset:
    node = tree["summary"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def check_model(
    plugin: type[Any],
    fixture: ModelFixture,
    *,
    raise_on_failure: bool = True,
    checks: Sequence[str] | None = None,
) -> ContractReport:
    """Run the model contract checks: fit, bundle round trip, coordinate handling, typed
    errors, context-window equivalence, evaluate path == score path, output space,
    aggregation legality, additive contributions, no pickle and seed determinism."""
    reg = (fixture.registry or default_registry()).copy()
    reg.register_model(plugin, replace=True)
    spec = fixture.model_spec()
    data = fixture.data
    tables = dict(fixture.side_tables)
    report = ContractReport(f"model {plugin.id}@{plugin.version}")
    manifest = plugin.manifest()
    caps = manifest.capabilities
    rep = caps.representation
    holder: dict[str, Any] = {}

    def fit_once() -> Bundle:
        if "bundle" not in holder:
            holder["bundle"] = fit_spec(spec, data, tables, reg, raw_schema=fixture.raw_schema)
        bundle: Bundle = holder["bundle"]
        return bundle

    def binding_dim(canon: str) -> str:
        return fit_once().binding.to_dataset(canon)

    def raw_time_dim() -> str | None:
        b = fit_once()
        raw = b.resolved.stages.get("raw")
        if raw is None:
            return None
        for name, d in raw.dims.items():
            if d.policy is DimPolicy.EXTEND and d.kind in ("time", "integer"):
                return binding_dim(name)
        return None

    def raw_time_freq() -> str | None:
        raw = fit_once().resolved.stages.get("raw")
        for d in raw.dims.values() if raw is not None else []:
            if d.policy is DimPolicy.EXTEND and d.kind == "time":
                return d.freq
        return None

    def window() -> xr.Dataset:
        tdim = raw_time_dim()
        if tdim is None:
            return data
        n = int(data.sizes[tdim])
        k = fixture.window or max(1, n // 4)
        return data.isel({tdim: slice(n - k, None)})

    def categorical_dims() -> list[tuple[str, str, DimPolicy, str]]:
        """(canonical, dataset name, effective policy, strategy) for raw categorical dims."""
        b = fit_once()
        raw = b.resolved.stages.get("raw")
        model = b.model_schema
        out: list[tuple[str, str, DimPolicy, str]] = []
        for name, d in raw.dims.items() if raw is not None else []:
            if d.kind != "categorical":
                continue
            pol, strat = d.policy, d.new_level_strategy
            if model is not None and name in model.dims:
                pol = model.dims[name].policy if pol is not DimPolicy.FIXED else pol
                strat = model.dims[name].new_level_strategy
            ds_name = binding_dim(name)
            if ds_name in data.dims:
                out.append((name, ds_name, pol, strat))
        return out

    # a 'supplied' side table must be named by every score request that replays its step
    supplied = {
        name: st.ref
        for name, st in spec.data.side_tables.items()
        if st.score_policy is ScorePolicy.SUPPLIED and name in tables
    }

    def score_on(
        bundle: Bundle, req: ScoreRequest, ds: xr.Dataset | None, **kw: Any
    ) -> xr.DataTree:
        if supplied:
            req = req.model_copy(update={"side_tables": {**supplied, **req.side_tables}})
        return score_bundle(bundle, req, ds, side_tables=tables, registry=reg, **kw)

    def score(req: ScoreRequest, ds: xr.Dataset | None, **kw: Any) -> xr.DataTree:
        return score_on(fit_once(), req, ds, **kw)

    def c_manifest() -> str:
        reg.catalog()
        return f"{manifest.id}@{manifest.version} ({rep.value})"

    def c_fit() -> str:
        b = fit_once()
        return f"bundle {b.bundle_id[:12]}, metrics {sorted(b.metrics.values)}"

    def c_roundtrip() -> str:
        b = fit_once()
        with tempfile.TemporaryDirectory() as tmp:
            b.write(tmp)
            back = Bundle.read(tmp, reg)
            # native artifacts keep their names in attrs (the archive keys are zarr paths),
            # so the check runs on the read-back artifacts: names, opcodes, npy, archives
            assert_no_pickle(back.model.native)
        w = window()
        a = score_on(b, _request(), w)
        c = score_on(back, _request(), w)
        xr.testing.assert_allclose(_summary(a), _summary(c))
        return "scores match after write/read"

    def long_index(name: str) -> tuple[str, list[str]] | None:
        """Long layout: ``(row dim, index variables)`` pointing into dataset dim ``name``.

        Index variables hold positions into ``name``; reordering or subsetting its labels
        must move them too, and new levels arrive as new rows pointing at a new label.
        """
        raw = fit_once().resolved.stages.get("raw")
        if raw is None:
            return None
        canon = next((c for c in raw.dims if binding_dim(c) == name), None)
        if canon is None:
            return None
        idx = [
            binding_dim(v)
            for v, spec in raw.vars.items()
            if spec.index_of == canon and binding_dim(v) in data
        ]
        if not idx:
            return None
        return str(data[idx[0]].dims[0]), idx

    def populated_level(w: xr.Dataset, idx: list[str], prefer: int = 0) -> int:
        """A level of the index variables that has rows in ``w`` (``prefer`` if possible)."""
        used = np.unique(np.asarray(w[idx[0]].values))
        if not used.size:
            raise _Skip("the scoring window has no rows")
        return prefer if prefer in used else int(used[min(prefer, used.size - 1)])

    def c_shuffled() -> str:
        dims = categorical_dims()
        if not dims:
            raise _Skip("no categorical dims")
        w = window()
        base = _summary(score(_request(), w))
        for _, name, _, _ in dims:
            perm = np.arange(w.sizes[name])[::-1]
            shuffled = w.isel({name: perm})
            long = long_index(name)
            if long is not None:
                inv = np.argsort(perm)
                for v in long[1]:
                    shuffled[v] = w[v].copy(data=inv[np.asarray(w[v].values)])
            got = _summary(score(_request(), shuffled))
            if name in base.dims:
                got = got.sel({name: base[name].values})
            xr.testing.assert_allclose(got, base)
        return f"shuffled {[d[1] for d in dims]}"

    def c_subset() -> str:
        dims = [d for d in categorical_dims() if data.sizes[d[1]] > 1]
        if not dims:
            raise _Skip("no categorical dim with several levels")
        w = window()
        base = _summary(score(_request(), w))
        _, name, _, _ = dims[0]
        long = long_index(name)
        if long is None:
            sub = w.isel({name: [1]})
            got = _summary(score(_request(), sub))
            xr.testing.assert_allclose(got, base.isel({name: [1]}))
            return f"subset of {name!r}"
        row, idx = long
        level = populated_level(w, idx, prefer=1)
        rows = np.flatnonzero(np.asarray(w[idx[0]].values) == level)
        sub = w.isel({name: [level], row: rows})
        for v in idx:
            sub[v] = sub[v].copy(data=np.zeros(rows.size, dtype=sub[v].dtype))
        got = _summary(score(_request(), sub))
        want = base.sel({row: w[row].values[rows]}) if row in base.dims else base
        xr.testing.assert_allclose(got, want)
        return f"subset of {name!r} ({rows.size} {row} rows, long layout)"

    def with_new_level(w: xr.Dataset, name: str) -> tuple[xr.Dataset, dict[str, Any]]:
        """``w`` plus a level ``__contract_new__`` copied from an existing one; returns the
        selection that picks the new level's outputs."""
        long = long_index(name)
        if long is None:
            new = w.isel({name: [0]}).assign_coords({name: ["__contract_new__"]})
            mixed = xr.concat(
                [w, new], dim=name, data_vars="minimal", coords="minimal", compat="override"
            )
            return mixed, {name: "__contract_new__"}
        row, idx = long
        n = int(w.sizes[name])
        level = populated_level(w, idx)
        labels = [*np.asarray(w[name].values).tolist(), "__contract_new__"]
        ext = w.isel({name: [*range(n), level]}).assign_coords({name: labels})
        rows = np.flatnonzero(np.asarray(w[idx[0]].values) == level)
        extra = ext.isel({row: rows})
        for v in idx:
            extra[v] = extra[v].copy(data=np.full(rows.size, n, dtype=extra[v].dtype))
        if row in w.coords:
            last = np.asarray(w[row].values).max()
            if np.asarray(w[row].values).dtype.kind not in "iu":
                raise _Skip(f"new rows need integer {row!r} labels")
            extra = extra.assign_coords({row: last + 1 + np.arange(rows.size)})
        mixed = xr.concat(
            [ext, extra], dim=row, data_vars="minimal", coords="minimal", compat="override"
        )
        return mixed, {row: np.asarray(extra[row].values)}

    def c_new_levels() -> str:
        dims = categorical_dims()
        if not dims:
            raise _Skip("no categorical dims")
        w = window()
        notes = []
        for _, name, pol, strat in dims:
            mixed, pick = with_new_level(w, name)
            if pol is DimPolicy.NEW_LEVELS and strat != "error":
                out = _summary(score(_request(), mixed))
                vals = out.sel(pick)
                for v in vals.data_vars:
                    if not bool(np.isfinite(vals[v]).all()):
                        raise AssertionError(f"new level of {name!r} gave non-finite {v!r}")
                notes.append(f"{name}: new level scored ({strat})")
            else:
                notes.append(
                    f"{name}: "
                    + _expect_code(
                        ErrorCode.UNSEEN_LEVELS, functools.partial(score, _request(), mixed)
                    )
                )
        return "; ".join(notes)

    def c_extend_errors() -> str:
        tdim = raw_time_dim()
        if tdim is None:
            raise _Skip("no extend dim at the raw stage")
        coords = data[tdim].values
        if coords.dtype.kind != "M" or len(coords) < 4:
            raise _Skip("extend checks need a datetime dim")
        n = len(coords)

        def shifted(values: np.ndarray, periods: int) -> np.ndarray:
            # calendar frequencies (MS, QS, ...) have no fixed step: shift by whole periods
            freq = raw_time_freq()
            if freq is not None:
                offset = pd.tseries.frequencies.to_offset(freq)
                return np.array(
                    [np.datetime64(pd.Timestamp(v) + periods * offset, "ns") for v in values]
                )
            return np.asarray(values + (coords[1] - coords[0]) * periods)

        gap = data.isel({tdim: slice(n // 2, None)}).assign_coords(
            {tdim: shifted(coords[n // 2 :], n + 30)}
        )
        notes = [_expect_code(ErrorCode.NON_CONTIGUOUS_EXTENSION, lambda: score(_request(), gap))]
        before = data.isel({tdim: slice(0, 3)}).assign_coords({tdim: shifted(coords[:3], -400)})
        notes.append(_expect_code(ErrorCode.COORD_NOT_ALLOWED, lambda: score(_request(), before)))
        return "; ".join(notes)

    def c_modes() -> str:
        unsupported = [
            m for m in SCORE_MODES if m not in caps.score_modes and m not in ("filter", "evaluate")
        ]
        if not unsupported:
            raise _Skip("every mode is supported")
        mode = unsupported[0]
        kw: dict[str, Any] = {}
        if mode == "forecast":
            kw["horizon_dim"] = "time"
        if mode == "counterfactual":
            kw["interventions"] = [{"var": "__any__", "multiply": 1.0}]
        return _expect_code(
            ErrorCode.MODE_NOT_SUPPORTED, lambda: score(_request(mode, **kw), window())
        )

    def holdout_raw() -> tuple[Bundle, xr.Dataset]:
        b = fit_once()
        ev = b.evaluation or (b if spec.model.fit.mode.value == "train_only" else None)
        split = spec.data.split
        if ev is None or not isinstance(split, TimeHoldoutSplit | AsOfSplit):
            raise _Skip("needs an ordered holdout split")
        stage = b.resolved.split_stage or "raw"
        train_end = ev.fit_coords[stage][split.dim].values.max()
        tdim = binding_dim(split.dim)
        if tdim not in data.dims:
            raise _Skip("the split dim does not exist in the raw data")
        mask = data[tdim].values > train_end
        if not mask.any():
            raise _Skip("empty holdout")
        return ev, data.isel({tdim: np.flatnonzero(mask)})

    def c_context_window() -> str:
        ev, hold = holdout_raw()
        targets = [binding_dim(t) for t in (ev.model_schema.targets if ev.model_schema else [])]
        req = _request(names="canonical")
        a = score_on(
            ev,
            req,
            hold.drop_vars(targets, errors="ignore"),
            return_full=True,
        )
        full = score_on(
            ev,
            req,
            data.drop_vars(targets, errors="ignore"),
            return_full=True,
        )
        pa = a["predictions"].to_dataset()
        pf = full["predictions"].to_dataset()
        for v in pa.data_vars:
            x = point_estimate(pa[v], rep) if rep is not Representation.POINT else pa[v]
            y = point_estimate(pf[v], rep) if rep is not Representation.POINT else pf[v]
            xr.testing.assert_allclose(
                x, y.sel({d: x[d] for d in x.dims if d in y.dims}), rtol=1e-6
            )
        return "holdout via stored context == in-sample full series"

    def c_evaluate_path() -> str:
        b = fit_once()
        ev, hold = holdout_raw()
        names = [m for m in b.metrics.values if not m.startswith("elpd_")]
        if not names:
            raise _Skip("no holdout metrics were computed")
        rules = REPRESENTATION_RULES[rep]
        req = ScoreRequest.model_validate(
            {
                "apiVersion": "mapi/v1",
                "kind": "ScoreRequest",
                "model": "endpoint://contract/kit",
                "mode": "evaluate" if "evaluate" in caps.score_modes else "predict",
                "data": "dataset://contract/holdout@v1",
                "output": {
                    "kind": "predictive" if rules.predictive else "expected",
                    "names": "canonical",
                },
                "seed": spec.model.fit.seed or 0,
            }
        )
        tree = score_on(ev, req, hold, metrics=names)
        got = json.loads(tree.attrs["metrics"])["values"]
        for n in names:
            want = b.metrics.values[n]
            if want is None or got.get(n) is None:
                continue
            if not math.isclose(want, got[n], rel_tol=1e-6, abs_tol=1e-9):
                raise AssertionError(f"{n}: fit-time evaluation {want} != score path {got[n]}")
        return f"metrics {names} reproduced through the score path"

    def c_output_space() -> str:
        tree = score(_request(), window(), return_full=True)
        space = json.loads(tree.attrs["output_space"])
        b = fit_once()
        notes = []
        for var, stage in space.items():
            decl = manifest.outputs.get(var)
            if decl is None or decl.units_of is None or stage == "model-owned":
                continue
            sch = b.resolved.stages.get(stage)
            want = (
                sch.vars[decl.units_of].units
                if sch is not None and decl.units_of in sch.vars
                else None
            )
            # outputs are reverse-bound: 'y' bound to 'revenue_local' comes back as the latter
            preds = tree["predictions"]
            name = binding_dim(var)
            out = preds[name] if name in preds.data_vars else preds[var]
            got = out.attrs.get("units")
            if want != got:
                raise AssertionError(f"{var}: units {got!r} but stage {stage!r} says {want!r}")
            if out.attrs.get("output_space") != stage:
                raise AssertionError(f"{var}: missing output_space attribute")
            notes.append(f"{var} in {stage} ({want})")
        if not notes:
            raise _Skip("no outputs declare units_of")
        return "; ".join(notes)

    def c_aggregation() -> str:
        b = fit_once()
        dims = [
            d
            for d in (b.model_schema.dims if b.model_schema else {})
            if d in b.fit_coords[b.model_stage].dims
        ]
        if not dims:
            raise _Skip("no model dims")
        dim = dims[0]
        verdict = can_aggregate(rep, [dim])
        req = _request(aggregate=Aggregation(over=[binding_dim(dim)]).model_dump())
        if verdict.allowed:
            tree = score(req, window())
            if binding_dim(dim) in _summary(tree).dims:
                raise AssertionError("aggregation did not reduce the dim")
            return f"{rep.value} aggregation over {dim!r} allowed"
        return _expect_code(ErrorCode.CAPABILITY_UNSUPPORTED, lambda: score(req, window()))

    def c_additive() -> str:
        if caps.contribution_scale != "additive":
            raise _Skip("contribution_scale is not additive")
        tree = score(_request(units="model"), window(), return_full=True)
        pred = tree["predictions"].to_dataset()
        if "contributions" not in pred:
            raise _Skip("the model does not return contributions at score time")
        target = next(iter(v for v in pred.data_vars if v != "contributions"))
        total = pred["contributions"].sum("component")
        xr.testing.assert_allclose(total, pred[target].transpose(*total.dims), rtol=1e-6)
        return f"contributions sum to {target!r}"

    def c_seed() -> str:
        if not caps.deterministic_given_seed:
            raise _Skip("the model does not claim seed determinism")
        if spec.model.fit.seed is None:
            raise _Skip("the fixture spec sets no fit seed")
        a = fit_spec(
            spec, data, tables, reg, raw_schema=fixture.raw_schema, keep_evaluation_bundle=False
        )
        b = fit_once()
        for name, grp in a.model.groups.items():
            xr.testing.assert_identical(grp, b.model.groups[name])
        kind = "predictive" if REPRESENTATION_RULES[rep].predictive else "expected"
        s1 = _summary(score_on(a, _request(kind=kind), window()))
        s2 = _summary(score_on(b, _request(kind=kind), window()))
        xr.testing.assert_identical(s1, s2)
        return "same seed, same fit and scores"

    all_checks: list[tuple[str, Callable[[], str | None]]] = [
        ("manifest", c_manifest),
        ("fit", c_fit),
        ("bundle_roundtrip_no_pickle", c_roundtrip),
        ("shuffled_scoring", c_shuffled),
        ("subset_scoring", c_subset),
        ("new_level_policies", c_new_levels),
        ("extend_typed_errors", c_extend_errors),
        ("unsupported_mode", c_modes),
        ("context_window_equivalence", c_context_window),
        ("evaluate_path_equals_score_path", c_evaluate_path),
        ("output_space_and_units", c_output_space),
        ("representation_aggregation", c_aggregation),
        ("additive_contributions", c_additive),
        ("seed_determinism", c_seed),
    ]
    for name, fn in all_checks:
        if checks is not None and name not in checks:
            continue
        fit_failed = any(r.name == "fit" and not r.passed for r in report.results)
        if name not in ("manifest", "fit") and fit_failed:
            report.results.append(CheckResult(name, True, "fit failed", skipped=True))
            continue
        _run(report, name, fn)
    if raise_on_failure:
        report.raise_for_failures()
    return report
