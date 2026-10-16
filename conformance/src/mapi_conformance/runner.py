"""The conformance corpus runner (plan §13: spec → fit → score → evaluate, per fixture).

For every fixture (see :mod:`mapi_conformance.fixtures`) :func:`run_fixture`:

1. **load** - generates the data at the requested size and reads ``spec.yaml`` (applying the
   fixture's ``TINY_FIT`` overrides at size ``tiny``);
2. **validate** - static validation (``mapi_spec.validate``) against the registry's step,
   model and plot catalogs, exactly as the control plane does;
3. **fit** - a ``fit`` run on the in-process executor (``mapi_sdk.executors``), i.e. the
   SDK harness with ``fit.mode`` honoured (``evaluate_then_refit`` included);
4. **score** - every request of ``score_requests.yaml`` as a ``score`` run against the
   bundle; requests that must fail are checked for their typed error code;
5. **evaluate** - an ``evaluate`` run of the evaluation bundle on the holdout (or of the
   production bundle in sample when the spec has no split) with the spec's metrics;
6. **contract** - :func:`mapi_sdk.testing.check_model` on the fixture;
7. **plots** - the spec's ``outputs.plots`` rendered from the bundle or evaluation tree;
8. **expectations** - the fixture's own extra checks.

Each step is independent where possible (a failed score request does not hide the others);
:class:`FixtureReport` and :class:`CorpusReport` collect the outcome and print a table.

``score_requests.yaml`` entries::

    - name: forecast_12                    # unique within the fixture
      request: {mode: forecast, horizon: {dim: t, periods: 12}}
      payload: none                        # none | fit_data | tail:<n> | <builder name>
      scenario: <builder name>             # optional scenario payload (counterfactual vs data)
      drop_targets: true                   # drop target variables from the payload
      side_tables: true                    # pass the fixture's side tables (default)
      expect: {groups: [summary], vars: [y_hat], sizes: {date: 12}}
      expect_error: HORIZON_EXCEEDS_FIT    # the run must fail with this code instead

The ``expectations.py`` hooks are called as ``check_fit(bundle, data)``,
``check_score(name, tree, bundle, data)`` and ``check_evaluate(tree, bundle, data)``. A hook
that declares a ``ctx`` parameter also receives a :class:`FixtureContext` (seed, size,
registry, side tables and the score results so far), e.g. to compare two requests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import inspect
import json
import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from types import ModuleType
from typing import Any, Literal

import numpy as np
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.executors import InprocExecutor
from mapi_sdk.harness import SCHEMA_ATTR, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import infer_schema
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import DataSchema, ModelSpec, load_model_spec, parse_document, validate_spec
from mapi_spec.capabilities import REPRESENTATION_RULES
from mapi_spec.errors import ErrorCode, Severity
from mapi_spec.executor import ExecState, RunKind, RunSubmit
from mapi_spec.loader import parse_yaml
from mapi_spec.spec import AsOfSplit, ScorePolicy, ScoreRequest, TimeHoldoutSplit

from mapi_conformance.fixtures import FIXTURES

__all__ = [
    "CorpusReport",
    "Fixture",
    "FixtureContext",
    "FixtureReport",
    "ScoreCase",
    "Size",
    "StepResult",
    "load_fixture",
    "run_corpus",
    "run_fixture",
]

Size = Literal["tiny", "full"]
FIXTURE_PACKAGE = "mapi_conformance.fixtures"
STEPS = ("load", "validate", "fit", "score", "evaluate", "contract", "plots", "expectations")


# ============================================================================== fixtures


@dataclass(slots=True)
class ScoreCase:
    """One entry of ``score_requests.yaml``."""

    name: str
    request: dict[str, Any]
    payload: str = "none"
    drop_targets: bool = True
    expect: dict[str, Any] = field(default_factory=dict)
    expect_error: str | None = None
    side_tables: bool = True
    scenario: str | None = None

    @classmethod
    def parse(cls, raw: Mapping[str, Any], fixture_id: str) -> ScoreCase:
        unknown = set(raw) - {
            "name",
            "request",
            "payload",
            "drop_targets",
            "expect",
            "expect_error",
            "side_tables",
            "scenario",
        }
        if unknown:
            raise ValueError(f"score request entry has unknown keys {sorted(unknown)}")
        if "name" not in raw or "request" not in raw:
            raise ValueError("score request entries need 'name' and 'request'")
        request = {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"endpoint://conformance/{fixture_id.replace('_', '-')}",
            **dict(raw["request"]),
        }
        err = raw.get("expect_error")
        if err is not None:
            ErrorCode(err)  # fail early on typos
        return cls(
            name=str(raw["name"]),
            request=request,
            payload=str(raw.get("payload", "none")),
            drop_targets=bool(raw.get("drop_targets", True)),
            expect=dict(raw.get("expect") or {}),
            expect_error=err,
            side_tables=bool(raw.get("side_tables", True)),
            scenario=None if raw.get("scenario") is None else str(raw["scenario"]),
        )

    def score_request(self) -> ScoreRequest:
        doc = parse_document(self.request)
        if not isinstance(doc, ScoreRequest):
            raise ValueError(f"{self.name}: request is not a ScoreRequest")
        return doc


@dataclass(slots=True)
class FixtureContext:
    """What a ``ctx``-accepting expectation hook sees besides the bundle and the data."""

    fixture_id: str
    seed: int
    size: Size
    registry: Registry
    side_tables: dict[str, xr.Dataset]
    results: dict[str, xr.DataTree] = field(default_factory=dict)
    """Trees of the score requests that succeeded so far, by request name (file order)."""


def _call_hook(hook: Callable[..., Any], *args: Any, ctx: FixtureContext) -> Any:
    """Call an expectation hook, passing ``ctx`` only when the hook declares it."""
    params: Mapping[str, inspect.Parameter]
    try:
        params = inspect.signature(hook).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins without signatures
        params = {}
    if "ctx" in params:
        return hook(*args, ctx=ctx)
    return hook(*args)


@dataclass(slots=True)
class Fixture:
    """A loaded fixture: its modules, spec text and score requests."""

    id: str
    data_module: ModuleType
    spec_text: str
    cases: list[ScoreCase]
    expectations: ModuleType | None

    def hook(self, name: str) -> Any:
        return getattr(self.expectations, name, None) if self.expectations else None

    def generate(self, seed: int, size: Size) -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
        out = self.data_module.generate(seed=seed, size=size)
        if not (isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], xr.Dataset)):
            raise TypeError(f"{self.id}: generate() must return (Dataset, side_tables)")
        return out[0], dict(out[1] or {})

    def raw_schema(self, data: xr.Dataset) -> DataSchema:
        fn = getattr(self.data_module, "schema", None)
        if callable(fn):
            schema = fn()
            if not isinstance(schema, DataSchema):
                raise TypeError(f"{self.id}: schema() must return a DataSchema")
            return schema
        stored = data.attrs.get(SCHEMA_ATTR)
        if isinstance(stored, str):
            return DataSchema.model_validate_json(stored)
        return infer_schema(data)

    def spec(self, size: Size) -> ModelSpec:
        """The fixture's spec; at size ``tiny`` the ``TINY_FIT`` overrides replace fit
        fields and ``TINY_SETTINGS`` are deep-merged into the model settings."""
        spec = load_model_spec(self.spec_text)
        if size != "tiny":
            return spec
        fit, settings = self.hook("TINY_FIT"), self.hook("TINY_SETTINGS")
        if not fit and not settings:
            return spec
        doc = spec.model_dump(mode="json", by_alias=True, exclude_unset=True)
        model = doc.setdefault("model", {})
        if isinstance(fit, Mapping):
            model["fit"] = {**model.get("fit", {}), **fit}
        if isinstance(settings, Mapping):
            model["settings"] = _deep_merge(model.get("settings") or {}, settings)
        return load_model_spec(json.dumps(doc))

    def payload(
        self,
        case: ScoreCase,
        data: xr.Dataset,
        spec: ModelSpec,
        schema: DataSchema,
        seed: int,
        size: Size,
    ) -> xr.Dataset | None:
        kind = case.payload
        if kind == "none":
            return None
        if kind == "fit_data":
            out = data
        elif kind.startswith("tail:"):
            n = int(kind.split(":", 1)[1])
            dim = _raw_time_dim(spec, data)
            if dim is None:
                raise ValueError(f"{case.name}: 'tail' needs an ordered raw dim")
            out = data.isel({dim: slice(-n, None)})
        else:
            fn = getattr(self.data_module, f"payload_{kind}", None)
            if not callable(fn):
                raise ValueError(f"{case.name}: data.py has no payload_{kind}()")
            out = fn(data, seed, size)
        if case.drop_targets:
            out = out.drop_vars(_targets(spec, schema, out), errors="ignore")
        return out


def _deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        cur = out.get(k)
        out[k] = _deep_merge(cur, v) if isinstance(cur, Mapping) and isinstance(v, Mapping) else v
    return out


def load_fixture(fixture_id: str) -> Fixture:
    """Import ``mapi_conformance.fixtures.<fixture_id>`` and read its files."""
    pkg = f"{FIXTURE_PACKAGE}.{fixture_id}"
    data_module = importlib.import_module(f"{pkg}.data")
    if not callable(getattr(data_module, "generate", None)):
        raise TypeError(f"{fixture_id}: data.py must define generate(seed, size)")
    files = resources.files(pkg)
    spec_text = (files / "spec.yaml").read_text(encoding="utf-8")
    cases: list[ScoreCase] = []
    req_file = files / "score_requests.yaml"
    if req_file.is_file():
        raw = parse_yaml(req_file.read_text(encoding="utf-8")) or []
        if not isinstance(raw, list):
            raise ValueError(f"{fixture_id}: score_requests.yaml must be a list")
        cases = [ScoreCase.parse(r, fixture_id) for r in raw]
        names = [c.name for c in cases]
        if len(set(names)) != len(names):
            raise ValueError(f"{fixture_id}: duplicate score request names")
    try:
        expectations: ModuleType | None = importlib.import_module(f"{pkg}.expectations")
    except ModuleNotFoundError as exc:
        if exc.name != f"{pkg}.expectations":
            raise
        expectations = None
    return Fixture(fixture_id, data_module, spec_text, cases, expectations)


# =============================================================================== reports


@dataclass(slots=True)
class StepResult:
    """Outcome of one runner step for one fixture."""

    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    skipped: bool = False
    passed: int | None = None
    total: int | None = None

    @property
    def cell(self) -> str:
        if self.skipped:
            return "skip"
        mark = "ok" if self.ok else "FAIL"
        if self.total is not None:
            return f"{mark} {self.passed}/{self.total}"
        return mark


@dataclass(slots=True)
class FixtureReport:
    """Every step of one fixture run."""

    fixture: str
    size: Size
    model: str = ""
    steps: list[StepResult] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return all(s.ok or s.skipped for s in self.steps) and bool(self.steps)

    def step(self, name: str) -> StepResult | None:
        return next((s for s in self.steps if s.name == name), None)

    @property
    def failures(self) -> list[StepResult]:
        return [s for s in self.steps if not s.ok and not s.skipped]

    def to_json(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "size": self.size,
            "model": self.model,
            "ok": self.ok,
            "seconds": round(self.seconds, 3),
            "metrics": self.metrics,
            "steps": [
                {
                    "name": s.name,
                    "ok": s.ok,
                    "skipped": s.skipped,
                    "passed": s.passed,
                    "total": s.total,
                    "seconds": round(s.seconds, 3),
                    "detail": s.detail,
                }
                for s in self.steps
            ],
        }


@dataclass(slots=True)
class CorpusReport:
    """The reports of every fixture run."""

    fixtures: list[FixtureReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.fixtures)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "fixtures": [f.to_json() for f in self.fixtures]}

    def table(self) -> str:
        """A fixed-width summary table (one row per fixture)."""
        # stack-mode reports (mapi_conformance.stack) add an "ingest" step
        steps = (["ingest"] if any(f.step("ingest") for f in self.fixtures) else []) + list(
            STEPS[1:]
        )
        head = ["fixture", "model", *steps, "metrics", "time", "result"]
        rows: list[list[str]] = []
        for f in self.fixtures:
            cells = [f.fixture, f.model or "-"]
            for name in steps:
                s = f.step(name)
                cells.append("-" if s is None else s.cell)
            cells.append(_fmt_metrics(f.metrics))
            cells.append(f"{f.seconds:.1f}s")
            cells.append("PASS" if f.ok else "FAIL")
            rows.append(cells)
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(head)]
        line = "  ".join(h.ljust(w) for h, w in zip(head, widths, strict=True)).rstrip()
        sep = "  ".join("-" * w for w in widths)
        body = [
            "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows
        ]
        total = sum(f.seconds for f in self.fixtures)
        n_ok = sum(f.ok for f in self.fixtures)
        footer = f"{n_ok}/{len(self.fixtures)} fixtures passed in {total:.1f}s"
        return "\n".join([line, sep, *body, sep, footer])

    def failure_details(self) -> str:
        out = []
        for f in self.fixtures:
            for s in f.failures:
                out.append(f"[{f.fixture}] {s.name}: {s.detail}")
        return "\n".join(out)


def _fmt_metrics(metrics: Mapping[str, float | None]) -> str:
    parts = [f"{k}={v:.3g}" for k, v in sorted(metrics.items()) if isinstance(v, int | float)]
    return ", ".join(parts) or "-"


# ================================================================================ runner


class _Step:
    """Context manager recording one step (exceptions become a failed step)."""

    def __init__(self, report: FixtureReport, name: str) -> None:
        self.report = report
        self.result = StepResult(name, ok=True)
        self._t0 = 0.0

    def __enter__(self) -> StepResult:
        self._t0 = time.perf_counter()
        return self.result

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> bool:
        self.result.seconds = time.perf_counter() - self._t0
        if exc is not None:
            self.result.ok = False
            last = traceback.format_exception_only(type(exc), exc)[-1].strip()
            self.result.detail = (self.result.detail + "; " if self.result.detail else "") + last
            self.result.detail = self.result.detail[:4000]
        self.report.steps.append(self.result)
        return exc is not None and not isinstance(exc, KeyboardInterrupt)


def _skip(report: FixtureReport, name: str, why: str) -> None:
    report.steps.append(StepResult(name, ok=True, detail=why, skipped=True))


def _targets(spec: ModelSpec, schema: DataSchema, data: xr.Dataset) -> list[str]:
    """Dataset names of the targets: the raw schema's, plus whatever ``y`` is bound to."""
    names = {*schema.targets, spec.data.bind.vars.get("y", "y")}
    return sorted(n for n in names if n in data)


def _raw_time_dim(spec: ModelSpec, data: xr.Dataset) -> str | None:
    split = spec.data.split
    dims = dict(spec.data.bind.dims)
    if isinstance(split, TimeHoldoutSplit | AsOfSplit):
        name = dims.get(split.dim, split.dim)
        if name in data.dims:
            return name
    for d in data.dims:
        if data[d].dtype.kind in "Miu" and d in data.coords:
            return str(d)
    return None


class _Runner:
    """Drives one fixture through the in-process executor."""

    def __init__(self, registry: Registry, datasets: dict[str, xr.Dataset]) -> None:
        self.executor = InprocExecutor(registry=registry, resolver=datasets)
        self.datasets = datasets
        self._n = 0

    def _submit(self, kind: RunKind, payload: dict[str, Any], label: str) -> Any:
        self._n += 1
        run_id = f"{label}-{self._n}"[:120]

        async def go() -> tuple[Any, Any, int]:
            rs = RunSubmit(run_id=run_id, kind=kind, payload=payload)
            handle = await self.executor.submit(rs, nonce=f"nonce-{run_id}")
            status = await self.executor.wait(handle)
            events = len(self.executor.events(handle))
            result = self.executor.result(handle) if status.state is ExecState.SUCCEEDED else None
            return status, result, events

        return asyncio.run(go())

    def fit(
        self, spec: ModelSpec, fixture_id: str, schema: DataSchema, tables: Sequence[str]
    ) -> tuple[Bundle, int]:
        payload = {
            "spec": spec.model_dump(mode="json", by_alias=True),
            "data": f"fixture://{fixture_id}/data",
            "side_tables": {t: f"fixture://{fixture_id}/side/{t}" for t in tables},
            "raw_schema": schema.model_dump(mode="json"),
        }
        status, bundle, events = self._submit(RunKind.FIT, payload, f"{fixture_id}-fit")
        if status.state is not ExecState.SUCCEEDED:
            raise RuntimeError(_problem_text(status))
        if not isinstance(bundle, Bundle):
            raise TypeError("the fit run did not return a bundle")
        return bundle, events

    def score(
        self,
        kind: RunKind,
        request: Mapping[str, Any],
        bundle_key: str,
        data_ref: str | None,
        label: str,
        tables: Sequence[str],
        fixture_id: str,
        metrics: Sequence[str] | None = None,
        scenario_ref: str | None = None,
    ) -> tuple[Any, Any]:
        payload: dict[str, Any] = {
            "request": dict(request),
            "bundle": bundle_key,
            "side_tables": {t: f"fixture://{fixture_id}/side/{t}" for t in tables},
        }
        if data_ref is not None:
            payload["data"] = data_ref
        if scenario_ref is not None:
            payload["scenario"] = scenario_ref
        if metrics is not None:
            payload["metrics"] = list(metrics)
        status, tree, _ = self._submit(kind, payload, label)
        return status, tree


def _problem_text(status: Any) -> str:
    p = status.problem
    if p is None:
        return f"run {status.state.value}: {status.message or ''}"
    extra = f" ({p.untrusted_text[:500]})" if p.untrusted_text else ""
    return f"{p.code.value}: {p.detail}{extra}"


def _supplied_side_tables(spec: ModelSpec, tables: Iterable[str]) -> dict[str, str]:
    """Refs of the spec's ``score_policy: supplied`` side tables the fixture provides; a
    score request must name these to replay the steps that use them."""
    have = set(tables)
    return {
        name: str(st.ref)
        for name, st in spec.data.side_tables.items()
        if st.score_policy is ScorePolicy.SUPPLIED and name in have
    }


def _has_group(tree: xr.DataTree, path: str) -> bool:
    node: xr.DataTree = tree
    for part in path.strip("/").split("/"):
        if part not in node.children:
            return False
        node = node.children[part]
    return True


def _check_expect(case: ScoreCase, tree: xr.DataTree) -> list[str]:
    """Generic expectations of a score case (groups, variables, sizes, finiteness)."""
    problems: list[str] = []
    exp = case.expect
    for g in exp.get("groups", []):
        if not _has_group(tree, g):
            problems.append(f"missing group {g!r}")
    summary = tree["summary"].to_dataset() if "summary" in tree else xr.Dataset()
    for v in exp.get("vars", []):
        if v not in summary:
            problems.append(f"summary has no variable {v!r}")
    for dim, size in dict(exp.get("sizes", {})).items():
        if summary.sizes.get(dim) != size:
            problems.append(f"summary dim {dim!r} has size {summary.sizes.get(dim)}, not {size}")
    if exp.get("finite", True):
        for v, da in summary.data_vars.items():
            if da.dtype.kind == "f" and not bool(np.isfinite(da).all()):
                problems.append(f"summary {v!r} has non-finite values")
    if "issues" in exp:
        codes = {i["code"] for i in json.loads(tree.attrs.get("issues", "[]"))}
        missing = set(exp["issues"]) - codes
        if missing:
            problems.append(f"expected issues {sorted(missing)} (got {sorted(codes)})")
    return problems


def run_fixture(
    fixture_id: str,
    *,
    size: Size = "tiny",
    seed: int = 0,
    registry: Registry | None = None,
    contract: bool = True,
    log: Callable[[str], None] | None = None,
) -> FixtureReport:
    """Run one fixture end to end; never raises for fixture failures (see the report)."""
    report = FixtureReport(fixture=fixture_id, size=size)
    t0 = time.perf_counter()
    say = log or (lambda _m: None)
    try:
        _run(report, fixture_id, size, seed, registry, contract, say)
    finally:
        report.seconds = time.perf_counter() - t0
    return report


def _run(
    report: FixtureReport,
    fixture_id: str,
    size: Size,
    seed: int,
    registry: Registry | None,
    contract: bool,
    say: Callable[[str], None],
) -> None:
    state: dict[str, Any] = {}
    with _Step(report, "load") as st:
        fx = load_fixture(fixture_id)
        data, tables = fx.generate(seed, size)
        spec = fx.spec(size)
        schema = fx.raw_schema(data)
        reg = (registry or default_registry()).copy()
        hook = fx.hook("registry")
        if callable(hook):
            reg = hook(reg)
        state.update(fx=fx, data=data, tables=tables, spec=spec, schema=schema, reg=reg)
        report.model = str(spec.model.use)
        st.detail = f"{dict(data.sizes)}; {len(fx.cases)} score requests"
    if not report.steps[-1].ok:
        return
    fx, data, tables, spec, schema, reg = (
        state["fx"], state["data"], state["tables"], state["spec"], state["schema"], state["reg"]
    )  # fmt: skip
    ctx = FixtureContext(fixture_id, seed, size, reg, dict(tables))
    say(f"[{fixture_id}] loaded {report.model}")

    with _Step(report, "validate") as st:
        res = validate_spec(spec, reg.catalog(), schema)
        if not res.ok:
            raise AssertionError(
                "; ".join(f"{i.code.value} at {i.pointer}: {i.message}" for i in res.errors)
            )
        rs = res.raise_for_errors()
        report.model = rs.model.resolved
        warnings = [i for i in res.issues if i.severity is Severity.WARNING]
        st.detail = f"resolved {rs.model.resolved}; {len(warnings)} warning(s)"
    if not report.steps[-1].ok:
        return

    datasets: dict[str, xr.Dataset] = {f"fixture://{fixture_id}/data": data}
    datasets.update({f"fixture://{fixture_id}/side/{k}": v for k, v in tables.items()})
    runner = _Runner(reg, datasets)
    bundle: Bundle | None = None
    with _Step(report, "fit") as st:
        bundle, events = runner.fit(spec, fixture_id, schema, sorted(tables))
        report.metrics.update(bundle.metrics.values)
        st.detail = (
            f"bundle {bundle.bundle_id[:12]} ({spec.model.fit.mode.value}); {events} run events"
        )
    say(f"[{fixture_id}] fit {'ok' if report.steps[-1].ok else 'FAILED'}")
    if bundle is None:
        for name in STEPS[3:]:
            _skip(report, name, "fit failed")
        return
    key = f"{fixture_id}/production"
    runner.executor.put_bundle(key, bundle)

    _score_cases(report, fx, runner, spec, schema, data, bundle, key, ctx)
    eval_tree = _evaluate(report, fx, runner, spec, data, bundle, sorted(tables), ctx)

    if contract:
        with _Step(report, "contract") as st:
            plugin = reg.model(f"{bundle.model.manifest.id}@{bundle.model.manifest.version}")
            checks = fx.hook("MODEL_CHECKS")
            kit = check_model(
                plugin,
                ModelFixture(
                    spec=spec, data=data, side_tables=tables, raw_schema=schema, registry=reg
                ),
                raise_on_failure=False,
                checks=list(checks) if checks is not None else None,
            )
            ran = [r for r in kit.results if not r.skipped]
            st.passed, st.total = sum(r.passed for r in ran), len(ran)
            st.ok = kit.ok
            st.detail = "; ".join(f"{r.name}: {r.detail}" for r in kit.failures) or (
                f"skipped: {[r.name for r in kit.results if r.skipped]}"
            )
    else:
        _skip(report, "contract", "disabled")

    _plots(report, spec, bundle, eval_tree, reg)

    with _Step(report, "expectations") as st:
        hook = fx.hook("check_fit")
        ran_checks = []
        if callable(hook):
            _call_hook(hook, bundle, data, ctx=ctx)
            ran_checks.append("check_fit")
        st.detail = ", ".join(ran_checks) or "no fixture-level fit checks"


def _score_cases(
    report: FixtureReport,
    fx: Fixture,
    runner: _Runner,
    spec: ModelSpec,
    schema: DataSchema,
    data: xr.Dataset,
    bundle: Bundle,
    key: str,
    ctx: FixtureContext,
) -> None:
    all_tables = sorted(ctx.side_tables)
    if not fx.cases:
        _skip(report, "score", "no score_requests.yaml")
        return
    with _Step(report, "score") as st:
        failures: list[str] = []
        passed = 0
        for case in fx.cases:
            try:
                payload = fx.payload(case, data, spec, schema, ctx.seed, ctx.size)
                ref = None
                if payload is not None:
                    ref = f"fixture://{fx.id}/payload/{case.name}"
                    runner.datasets[ref] = payload
                sref = None
                if case.scenario is not None:
                    scen_case = dataclasses.replace(case, payload=case.scenario)
                    scen = fx.payload(scen_case, data, spec, schema, ctx.seed, ctx.size)
                    if scen is not None:
                        sref = f"fixture://{fx.id}/scenario/{case.name}"
                        runner.datasets[sref] = scen
                case.score_request()  # validates the document before submitting
                status, tree = runner.score(
                    RunKind.EVALUATE if case.request.get("mode") == "evaluate" else RunKind.SCORE,
                    case.request,
                    key,
                    ref,
                    f"{fx.id}-{case.name}",
                    all_tables if case.side_tables else [],
                    fx.id,
                    scenario_ref=sref,
                )
                problems: list[str] = []
                if case.expect_error is not None:
                    code = status.problem.code.value if status.problem is not None else None
                    if status.state is ExecState.SUCCEEDED:
                        problems.append(f"expected {case.expect_error} but the run succeeded")
                    elif code != case.expect_error:
                        problems.append(
                            f"expected {case.expect_error}, got {_problem_text(status)}"
                        )
                elif status.state is not ExecState.SUCCEEDED:
                    problems.append(_problem_text(status))
                else:
                    problems += _check_expect(case, tree)
                    hook = fx.hook("check_score")
                    if callable(hook) and not problems:
                        _call_hook(hook, case.name, tree, bundle, data, ctx=ctx)
                    ctx.results[case.name] = tree
                if problems:
                    failures.append(f"{case.name}: {'; '.join(problems)}")
                else:
                    passed += 1
            except Exception as exc:  # one broken case never hides the others
                last = traceback.format_exception_only(type(exc), exc)[-1].strip()
                failures.append(f"{case.name}: {last}")
        st.passed, st.total = passed, len(fx.cases)
        st.ok = not failures
        st.detail = " | ".join(failures)[:4000]


def _holdout(spec: ModelSpec, data: xr.Dataset, evaluation: Bundle) -> xr.Dataset | None:
    split = spec.data.split
    if not isinstance(split, TimeHoldoutSplit | AsOfSplit):
        return None
    stage = evaluation.resolved.split_stage or "raw"
    coords = evaluation.fit_coords.get(stage)
    if coords is None or split.dim not in coords.coords:
        return None
    end = coords[split.dim].values.max()
    dim = evaluation.binding.to_dataset(split.dim)
    if dim not in data.dims:
        return None
    mask = np.asarray(data[dim].values) > end
    return data.isel({dim: np.flatnonzero(mask)}) if mask.any() else None


def _evaluate(
    report: FixtureReport,
    fx: Fixture,
    runner: _Runner,
    spec: ModelSpec,
    data: xr.Dataset,
    bundle: Bundle,
    tables: Sequence[str],
    ctx: FixtureContext,
) -> xr.DataTree | None:
    tree: xr.DataTree | None = None
    with _Step(report, "evaluate") as st:
        target = bundle.evaluation or bundle
        holdout = _holdout(spec, data, target) if bundle.evaluation is not None else None
        where = "holdout"
        if holdout is None:
            holdout, where = data, "in sample"
        caps = target.model.manifest.capabilities
        mode = "evaluate" if "evaluate" in caps.score_modes else "predict"
        names = [m for m in spec.evaluate.metrics if not m.startswith("elpd_")] or ["rmse"]
        key = f"{fx.id}/evaluation"
        runner.executor.put_bundle(key, target)
        ref = f"fixture://{fx.id}/payload/__evaluate__"
        runner.datasets[ref] = holdout
        request = {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": f"endpoint://conformance/{fx.id.replace('_', '-')}",
            "mode": mode,
            "data": "dataset://conformance/holdout@v1",
            # the same output kind and seed as the fit-time evaluation, so metrics must agree
            "output": {
                "names": "canonical",
                "kind": "predictive"
                if REPRESENTATION_RULES[caps.representation].predictive
                else "expected",
            },
            "seed": spec.model.fit.seed or 0,
        }
        supplied = _supplied_side_tables(spec, tables)
        if supplied:
            request["side_tables"] = supplied
        status, tree = runner.score(
            RunKind.EVALUATE, request, key, ref, f"{fx.id}-evaluate", tables, fx.id, names
        )
        if status.state is not ExecState.SUCCEEDED:
            raise RuntimeError(_problem_text(status))
        assert tree is not None
        values = json.loads(tree.attrs["metrics"])["values"]
        for k, v in values.items():
            report.metrics.setdefault(k, v)
        if bundle.evaluation is not None and where == "holdout":
            # (a split the runner cannot rebuild - last_per_group - is scored in sample,
            # which is not the fit-time holdout)
            for k, v in values.items():
                want = bundle.metrics.values.get(k)
                if want is not None and v is not None and not np.isclose(want, v, rtol=1e-6):
                    raise AssertionError(f"{k}: fit-time {want} != evaluate run {v}")
        hook = fx.hook("check_evaluate")
        if callable(hook):
            _call_hook(hook, tree, bundle, data, ctx=ctx)
        st.detail = f"{where}: " + ", ".join(
            f"{k}={v:.4g}" for k, v in sorted(values.items()) if isinstance(v, int | float)
        )
        # plots need the full predictions and observed values, which runs do not return
        side = {t: runner.datasets[f"fixture://{fx.id}/side/{t}"] for t in tables}
        tree = score_bundle(
            target,
            ScoreRequest.model_validate(request),
            holdout,
            side_tables=side,
            registry=runner.executor.registry,
            metrics=names,
            return_full=True,
        )
    return tree


def _plots(
    report: FixtureReport,
    spec: ModelSpec,
    bundle: Bundle,
    eval_tree: xr.DataTree | None,
    reg: Registry,
) -> None:
    ids = [p.id for p in spec.outputs.plots]
    if not ids:
        _skip(report, "plots", "the spec lists no plots")
        return
    with _Step(report, "plots") as st:
        from mapi_plots.recipes import render_plots

        trees = [bundle.to_tree()]
        if eval_tree is not None:
            trees.append(eval_tree)
        rendered: set[str] = set()
        reasons: dict[str, str] = {}
        for tree in trees:
            todo = [i for i in ids if i not in rendered]
            if not todo:
                break
            out = render_plots(tree, todo, registry=reg)
            rendered.update(p.id for p in out.plots)
            for issue in out.issues:
                why = issue.message + (f" ({issue.untrusted_text})" if issue.untrusted_text else "")
                reasons.setdefault(str(issue.pointer or ""), why)
        missing = [i for i in ids if i not in rendered]
        st.passed, st.total = len(ids) - len(missing), len(ids)
        st.ok = not missing
        st.detail = (
            f"not rendered: {missing} ({'; '.join(reasons.values())[:1000]})"
            if missing
            else f"rendered {sorted(rendered)}"
        )


def run_corpus(
    fixtures: Iterable[str] | None = None,
    *,
    size: Size = "tiny",
    seed: int = 0,
    registry: Registry | None = None,
    contract: bool = True,
    log: Callable[[str], None] | None = None,
) -> CorpusReport:
    """Run ``fixtures`` (default: every registered fixture)."""
    ids = list(fixtures) if fixtures is not None else list(FIXTURES)
    unknown = [i for i in ids if i not in FIXTURES]
    if unknown:
        raise ValueError(f"unknown fixtures {unknown}; known: {FIXTURES}")
    reg = registry or default_registry()
    report = CorpusReport()
    for fid in ids:
        report.fixtures.append(
            run_fixture(fid, size=size, seed=seed, registry=reg, contract=contract, log=log)
        )
    return report
