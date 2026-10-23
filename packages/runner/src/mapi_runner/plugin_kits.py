"""The contract kits of an uploaded plugin, run inside its ``plugin_describe`` run (v0.19, D8).

The CLI's ``mapi plugin validate`` reads a *source* tree (``pyproject.toml``); a describe run
has an *installed* upload instead: an overlay the registry already loaded. This module runs
the same kit functions on it (:mod:`mapi_spec.about`, :mod:`mapi_spec.schema_safety`,
:func:`mapi_plots.testing.check_recipe`, :func:`mapi_spec.workflow_nodes.plugin_contract_issues`,
:func:`mapi_workflows.kit.check_template`, :func:`mapi_sdk.testing.check_step`,
:func:`mapi_sdk.testing.check_model`, :func:`mapi_workflows.kit.check_node`) and reports each
result as a :class:`~mapi_spec.plugins.KitCheck`.

* :func:`static_checks` - always: what the overlay could not load (load diagnostics), at
  least one export, the namespace rule for uploads (``check_namespace(..., claimed=ns)``,
  which replaces the generic rule that refuses every ``t_`` namespace), complete About cards
  with the upload URL rules, safe settings/fit/params schemas, the recipe kit (renders the
  example), the node contract rules and the template kit.
* :func:`data_checks` - on platform toy data (:func:`toy_fixtures`) where one fits: a step
  with its default settings on a weekly store panel; a model whose data template (with its
  default settings) is a panel or a single series, on a daily market panel resampled to weeks;
  a node whose required inputs are all datasets, with its default parameters. Anything else is
  ``skipped`` with the reason. Each data check runs under a wall-clock limit; one that runs
  over it is a ``fail`` ("took too long").

A kit result is a quality signal, not a security property: the plugin's code runs in this
process and could fake it. Messages may hold plugin text (exception messages): the
:class:`~mapi_spec.plugins.KitCheck` model normalizes them and the control plane treats them as
untrusted.
"""

from __future__ import annotations

import contextlib
import functools
import signal
import threading
import traceback
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import FrameType
from typing import Any, Final, Literal

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.registry import Registry, UploadOverlay
from mapi_spec.about import About, about_issues
from mapi_spec.catalog import CatalogDiagnostic
from mapi_spec.errors import Issue
from mapi_spec.namespaces import check_namespace, uploaded_namespace_of
from mapi_spec.plugins import KitCheck
from mapi_spec.schema import DataSchema, Role
from mapi_spec.schema_safety import sanitize_schema
from mapi_spec.workflow_nodes import NodeImplementation, PortType, plugin_contract_issues
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES

__all__ = [
    "DATA_CHECK_TIMEOUT_S",
    "KitTimeout",
    "ToyFixtures",
    "data_checks",
    "static_checks",
    "time_limit",
    "toy_fixtures",
]

KitKind = Literal["model", "step", "plot", "node", "template", "package"]

DATA_CHECK_TIMEOUT_S: Final = 120.0
"""Default wall-clock limit of one data check (the job carries the effective value)."""
_MESSAGE_MAX: Final = 500
_GROUP_KIND: Final[Mapping[str, KitKind]] = {
    "mapi.models": "model",
    "mapi.steps": "step",
    "mapi.plots": "plot",
    "mapi.workflow_nodes": "node",
    "mapi.workflow_templates": "template",
}
_PANEL_SHAPES: Final = frozenset({"panel", "one_series", "several_series"})
"""``About.fits["data_shape"]`` answers the toy model data can stand for."""


# ================================================================================ time limit


class KitTimeout(BaseException):
    """Raised in the main thread when a data check runs over its time limit (a
    ``BaseException``, so ``except Exception`` in plugin or kit code cannot swallow it)."""


@contextlib.contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Raise :class:`KitTimeout` in this (main) thread after ``seconds`` of wall time.

    Outside the main thread (or without ``SIGALRM``) it does nothing: the run's own deadline
    is then the only bound."""
    usable = (
        seconds > 0
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not usable:
        yield
        return

    def on_alarm(_sig: int, _frame: FrameType | None) -> None:
        raise KitTimeout(f"the check took longer than {seconds:g} s")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _too_long(seconds: float) -> str:
    return f"The check took too long (more than {seconds:g} s) and was stopped."


# ================================================================================== fixtures


@dataclass(frozen=True, slots=True)
class ToyFixtures:
    """Platform toy data for the data kits (small, deterministic, no network).

    * ``series``: 60 weeks of ``sales(time)`` and ``y(time)`` (a single series);
    * ``panel``: 30 weeks of ``y(geo, time)`` and ``sales(geo, time)`` for four stores;
    * ``daily_panel``: ``revenue_local(market, date)`` over 24 weeks of days (the SDK's
      ``toy.panel_dataset``), for model fits resampled to weeks by ``toy.resample``;
    * ``registry``: the SDK's toy registry (``toy.resample`` and friends) the model fixtures
      use for their pipeline.
    """

    series: xr.Dataset
    panel: xr.Dataset
    daily_panel: xr.Dataset
    registry: Registry


def toy_fixtures() -> ToyFixtures:
    """Build the toy fixtures (a few milliseconds)."""
    from mapi_sdk.testing import toy

    rng = np.random.default_rng(7)
    weeks = pd.date_range("2024-01-07", periods=60, freq="W-SUN")
    sales = 100.0 + rng.normal(0.0, 5.0, 60)
    series = xr.Dataset(
        {"sales": (("time",), sales), "y": (("time",), sales * 1.5 + 10.0)},
        coords={"time": weeks},
    )
    rng = np.random.default_rng(3)
    time = pd.date_range("2025-01-05", periods=30, freq="W-SUN")
    store = 200.0 + rng.normal(0.0, 10.0, (4, 30))
    panel = xr.Dataset(
        {"y": (("geo", "time"), store.copy()), "sales": (("geo", "time"), store)},
        coords={"geo": ["north", "south", "east", "west"], "time": time},
    )
    daily, _fx = toy.panel_dataset(days=7 * 24)
    return ToyFixtures(
        series=series,
        panel=panel,
        daily_panel=daily[["revenue_local"]],
        registry=toy.toy_registry(),
    )


# ===================================================================================== helpers


def _overlay(registry: Registry, namespace: str) -> UploadOverlay | None:
    return registry.overlay(namespace)


def _clip(text: str) -> str:
    text = text.strip()
    return text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"


def _exc(exc: BaseException) -> str:
    return _clip("".join(traceback.format_exception_only(type(exc), exc)))


def _check(
    kind: KitKind, entry_id: str | None, check: str, problem: str | None, *, ok: str = ""
) -> KitCheck:
    return KitCheck(
        entry_id=entry_id[:128] if entry_id else None,
        kind=kind,
        check=check[:64],
        status="pass" if problem is None else "fail",
        message=_clip(problem if problem is not None else ok),
    )


def _skipped(kind: KitKind, entry_id: str | None, check: str, why: str) -> KitCheck:
    return KitCheck(
        entry_id=entry_id[:128] if entry_id else None,
        kind=kind,
        check=check[:64],
        status="skipped",
        message=_clip(why),
    )


def _guarded(
    out: list[KitCheck], kind: KitKind, entry_id: str | None, check: str, fn: Callable[[], None]
) -> None:
    """Run one block of checks; plugin code that raises fails ``check`` instead."""
    try:
        fn()
    except KitTimeout:
        raise
    except Exception as exc:  # plugin code: its error is the check result
        out.append(_check(kind, entry_id, check, f"The check raised an error: {_exc(exc)}"))


def _schema_problem(schema: Mapping[str, Any] | None, where: str) -> str | None:
    if not schema:
        return None
    _, issues = sanitize_schema(schema, mode="refuse", where=where)
    if not issues:
        return None
    more = f"; {len(issues) - 1} more" if len(issues) > 1 else ""
    return f"{issues[0].message} (at {issues[0].pointer or where}){more}"


def _about_problem(about: About | None) -> str | None:
    problems = about_issues(about, uploaded=True)
    return "; ".join(problems) if problems else None


def _namespace_problem(id_: str, namespace: str, *, plugin: str | None = None) -> str | None:
    reason = check_namespace(id_, None, plugin=plugin, claimed=namespace)
    if reason is None and uploaded_namespace_of(id_) != namespace:
        reason = f"{id_!r} is not an id in the namespace {namespace!r} of this upload."
    return reason


def _issues_text(issues: Sequence[Issue]) -> str | None:
    return "; ".join(i.message for i in issues) or None


def _diagnostic(d: CatalogDiagnostic) -> KitCheck:
    text = d.message if not d.untrusted_text else f"{d.message} {d.untrusted_text}"
    return _check(_GROUP_KIND.get(d.group, "package"), d.id, f"load.{d.code}", text)


@dataclass(slots=True)
class _Exports:
    """What one overlay exports (only what loaded)."""

    models: list[type[Any]]
    steps: list[type[Any]]
    plots: list[Any]
    nodes: list[Any]
    templates: list[Any]
    diagnostics: list[CatalogDiagnostic]
    all_contracts: dict[str, NodeImplementation]

    @property
    def count(self) -> int:
        return (
            len(self.models)
            + len(self.steps)
            + len(self.plots)
            + len(self.nodes)
            + len(self.templates)
        )


def _exports(registry: Registry, ov: UploadOverlay) -> _Exports:
    from mapi_workflows.runtime.governance import load_check_runners
    from mapi_workflows.runtime.nodes import (
        all_node_contracts,
        load_node_exports,
        load_plugin_templates,
    )

    origin = ov.provider
    every, _ = load_node_exports(overlays=registry.overlays)
    contracts = all_node_contracts([every[k].contract for k in sorted(every)])
    own, node_diags = load_node_exports(overlays=registry.overlays, only=origin)
    templates, template_diags = load_plugin_templates(
        contracts, overlays=registry.overlays, only=origin
    )
    check_diags: list[CatalogDiagnostic] = []
    load_check_runners(diagnostics=check_diags, overlays=registry.overlays, only=origin)
    return _Exports(
        models=[
            m for m in registry.models() if registry.origin_of("model", m.id, m.version) == origin
        ],
        steps=[
            s for s in registry.steps() if registry.origin_of("step", s.id, s.version) == origin
        ],
        plots=[
            p
            for p in registry.plots()
            if registry.origin_of("plot", p.catalog_entry().id, p.catalog_entry().version) == origin
        ],
        nodes=[own[k] for k in sorted(own)],
        templates=templates,
        diagnostics=[
            *registry.diagnostics_of(origin),
            *node_diags,
            *template_diags,
            *check_diags,
        ],
        all_contracts=contracts,
    )


# ============================================================================== static checks


def static_checks(
    registry: Registry, *, namespace: str, timeout_s: float = DATA_CHECK_TIMEOUT_S
) -> list[KitCheck]:
    """The static kit of the upload mounted for ``namespace`` (module docs).

    ``timeout_s`` bounds each recipe kit (it renders the recipe's example)."""
    ov = _overlay(registry, namespace)
    if ov is None:
        return [_check("package", None, "mounted", "The upload is not mounted in this run.")]
    ex = _exports(registry, ov)
    out: list[KitCheck] = [_diagnostic(d) for d in ex.diagnostics]
    out.append(
        _check(
            "package",
            None,
            "exports",
            None
            if ex.count
            else "The package exports no model, step, recipe, node or "
            "template that the platform could load.",
            ok=f"{ex.count} exports loaded",
        )
    )
    for m in ex.models:
        _guarded(
            out, "model", m.id, "manifest", functools.partial(_static_model, out, m, namespace)
        )
    for s in ex.steps:
        _guarded(
            out, "step", s.id, "catalog_entry", functools.partial(_static_step, out, s, namespace)
        )
    for p in ex.plots:
        pid = p.catalog_entry().id
        _guarded(
            out,
            "plot",
            pid,
            "recipe_kit",
            functools.partial(_static_recipe, out, p, namespace, timeout_s),
        )
    for n in ex.nodes:
        _guarded(
            out,
            "node",
            n.contract.id,
            "contract",
            functools.partial(_static_node, out, n, namespace),
        )
    for t in ex.templates:
        _guarded(
            out,
            "template",
            t.id,
            "template",
            functools.partial(_static_template, out, t, namespace, ex.all_contracts),
        )
    return out


def _static_model(out: list[KitCheck], m: type[Any], namespace: str) -> None:
    manifest = m.manifest()
    out.append(_check("model", m.id, "namespace", _namespace_problem(m.id, namespace)))
    out.append(_check("model", m.id, "about", _about_problem(manifest.about)))
    out.append(
        _check(
            "model",
            m.id,
            "settings_schema",
            _schema_problem(manifest.settings_schema, "/settings_schema"),
        )
    )
    out.append(
        _check("model", m.id, "fit_schema", _schema_problem(manifest.fit_schema, "/fit_schema"))
    )


def _static_step(out: list[KitCheck], s: type[Any], namespace: str) -> None:
    entry = s.catalog_entry()
    out.append(_check("step", s.id, "namespace", _namespace_problem(s.id, namespace)))
    out.append(_check("step", s.id, "about", _about_problem(entry.about)))
    out.append(
        _check(
            "step",
            s.id,
            "settings_schema",
            _schema_problem(entry.settings_schema, "/settings_schema"),
        )
    )


def _static_recipe(out: list[KitCheck], recipe: Any, namespace: str, timeout_s: float) -> None:
    from mapi_plots.testing import check_recipe

    pid = recipe.catalog_entry().id
    out.append(_check("plot", pid, "namespace", _namespace_problem(pid, namespace)))
    try:
        with time_limit(timeout_s):
            report = check_recipe(recipe, require_about=True, raise_on_failure=False)
    except KitTimeout:
        out.append(_check("plot", pid, "recipe_kit", _too_long(timeout_s)))
        return
    for name, reason in report.checks:
        if name == "namespace":  # the generic rule refuses every t_ namespace; see above
            continue
        out.append(_check("plot", pid, name, reason))
    entry = recipe.catalog_entry()
    out.append(_check("plot", pid, "about_urls", _about_problem(entry.about)))


def _static_node(out: list[KitCheck], export: Any, namespace: str) -> None:
    c: NodeImplementation = export.contract
    issues = [i for i in plugin_contract_issues(c) if i.pointer != "/plugin"]
    problem = _namespace_problem(c.id, namespace, plugin=c.plugin)
    if problem is None and c.plugin != namespace:
        problem = f"A node of this upload must name the namespace {namespace!r} in 'plugin'."
    out.append(_check("node", c.id, "namespace", problem))
    out.append(_check("node", c.id, "contract", _issues_text(issues)))
    out.append(_check("node", c.id, "about_urls", _about_problem(c.about)))


def _static_template(
    out: list[KitCheck], t: Any, namespace: str, nodes: Mapping[str, NodeImplementation]
) -> None:
    from mapi_workflows.kit import check_template

    problem = _namespace_problem(t.id, namespace, plugin=uploaded_namespace_of(t.id))
    if problem is None and not t.id.startswith(f"{namespace}_"):
        problem = f"A template id of this upload must start with '{namespace}_'."
    out.append(_check("template", t.id, "namespace", problem))
    # the kit's own /id namespace rule refuses every t_ namespace: the rule above replaces
    # it; the built-in id rule is checked here again
    issues = [i for i in check_template(t, nodes=nodes) if i.pointer != "/id"]
    if t.id in WORKFLOW_TEMPLATES:
        out.append(_check("template", t.id, "builtin_id", "The id is a built-in template id."))
    out.append(
        _check(
            "template",
            t.id,
            "template",
            "; ".join(f"{i.pointer or '/'}: {i.message}" for i in issues) or None,
        )
    )


# ================================================================================ data checks


def data_checks(
    registry: Registry,
    *,
    namespace: str,
    timeout_s: float = DATA_CHECK_TIMEOUT_S,
    fixtures: ToyFixtures | None = None,
) -> list[KitCheck]:
    """The data kit of the upload mounted for ``namespace`` on toy data (module docs).

    Every check of one item shares one ``timeout_s`` wall-clock limit."""
    ov = _overlay(registry, namespace)
    if ov is None:
        return []
    ex = _exports(registry, ov)
    toys = fixtures or toy_fixtures()
    out: list[KitCheck] = []
    for s in ex.steps:
        out += _limited("step", s.id, timeout_s, functools.partial(_data_step, s, toys))
    for m in ex.models:
        out += _limited("model", m.id, timeout_s, functools.partial(_data_model, m, toys))
    for n in ex.nodes:
        out += _limited(
            "node", n.contract.id, timeout_s, functools.partial(_data_node, n, toys, registry)
        )
    return out


def _limited(
    kind: KitKind, entry_id: str, timeout_s: float, fn: Callable[[], list[KitCheck]]
) -> list[KitCheck]:
    try:
        with time_limit(timeout_s):
            return fn()
    except KitTimeout:
        return [_check(kind, entry_id, "data", _too_long(timeout_s))]
    except Exception as exc:  # plugin code (or the kit refusing the object)
        return [_check(kind, entry_id, "data", f"The data check raised an error: {_exc(exc)}")]


def _report(
    kind: KitKind, entry_id: str, report: Any, *, drop: Sequence[str] = ()
) -> list[KitCheck]:
    """A :class:`mapi_sdk.testing.ContractReport` as kit checks named ``data.<check>``."""
    out: list[KitCheck] = []
    for r in report.results:
        if r.name in drop:
            continue
        name = f"data.{r.name}"
        if r.skipped:
            out.append(_skipped(kind, entry_id, name, r.detail or "Not applicable."))
        else:
            out.append(
                _check(
                    kind, entry_id, name, None if r.passed else (r.detail or "failed"), ok=r.detail
                )
            )
    return out


def _data_step(step: type[Any], toys: ToyFixtures) -> list[KitCheck]:
    from mapi_sdk.schema import infer_schema
    from mapi_sdk.testing import check_step

    try:
        step().parse_settings(None)
    except Exception:  # the step has required settings: no defaults to try
        return [
            _skipped(
                "step",
                step.id,
                "data",
                "The step needs settings without defaults, so the kit cannot run it on toy "
                "data. Run its own tests with `mapi plugin test`.",
            )
        ]
    data = toys.panel
    report = check_step(
        step,
        data,
        None,
        schema=infer_schema(data, targets=["y"]),
        raise_on_failure=False,
        require_about=True,
    )
    return _report("step", step.id, report)


def _model_shape(schema: DataSchema) -> tuple[str | None, str, str] | None:
    """``(group dim, time dim, target var)`` when the model's data is a panel or one series
    with exactly one target and nothing else, else ``None``."""
    times = [n for n, d in schema.dims.items() if d.kind == "time"]
    cats = [n for n, d in schema.dims.items() if d.kind == "categorical"]
    if len(times) != 1 or len(cats) > 1 or len(schema.dims) != len(times) + len(cats):
        return None
    targets = [n for n, v in schema.vars.items() if v.role is Role.TARGET]
    if len(targets) != 1 or len(schema.vars) != 1:
        return None
    group = cats[0] if cats else None
    if set(schema.vars[targets[0]].dims) != {d for d in (group, times[0]) if d}:
        return None
    return group, times[0], targets[0]


_MODEL_SPEC: Final = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: upload-kit, project: kit}}
data:
  dataset: dataset://kit/panel@latest
  bind:
    dims: {{{dims}}}
    vars: {{{target}: revenue_local}}
  split: {{kind: time_holdout, dim: {time}, last: 4, after_step: weekly}}
pipeline:
  - {{id: weekly, use: toy.resample@^1, with: {{dim: {time}, freq: W-SUN}}}}
model:
  use: {ref}
  fit: {{mode: evaluate_then_refit, seed: 7}}
evaluate: {{metrics: [rmse]}}
"""


def _data_model(model: type[Any], toys: ToyFixtures) -> list[KitCheck]:
    from mapi_sdk.testing import ModelFixture, check_model

    manifest = model.manifest()

    def skip(why: str) -> list[KitCheck]:
        return [_skipped("model", model.id, "data", why)]

    about = manifest.about
    shapes = set(about.fits.get("data_shape", [])) if about is not None else set()
    if shapes and not shapes & _PANEL_SHAPES:
        return skip(
            "The platform has no toy data of the shape this model asks for. Run its own "
            "tests with `mapi plugin test`."
        )
    schema, _err = manifest.schema_template.try_resolve({})
    if schema is None:
        return skip(
            "The data template of the model needs settings without defaults, so the kit "
            "cannot fit it on toy data."
        )
    shape = _model_shape(schema)
    if shape is None:
        return skip(
            "The model asks for data that is not one target over time (with at most one "
            "group dimension), so the kit has no toy data for it."
        )
    group, time, target = shape
    data = toys.daily_panel
    dims = f"{time}: date"
    if group is not None:
        dims = f"{group}: market, {dims}"
    else:
        data = data.sel(market=data["market"].values[0], drop=True)
    spec = _MODEL_SPEC.format(
        dims=dims, target=target, time=time, ref=f"{model.id}@{model.version}"
    )
    fixture = ModelFixture(spec=spec, data=data, registry=toys.registry)
    report = check_model(model, fixture, raise_on_failure=False, require_about=True)
    return _report("model", model.id, report)


def _default_params(schema: Mapping[str, Any]) -> dict[str, Any]:
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return {}
    return {k: v["default"] for k, v in props.items() if isinstance(v, Mapping) and "default" in v}


def _data_node(export: Any, toys: ToyFixtures, registry: Registry) -> list[KitCheck]:
    from jsonschema import Draft202012Validator
    from mapi_workflows.kit import check_node

    c: NodeImplementation = export.contract

    def skip(why: str) -> list[KitCheck]:
        return [_skipped("node", c.id, "data", why)]

    inputs: dict[str, Any] = {}
    for port, spec in c.inputs.items():
        if spec.type is PortType.DATASET:
            inputs[port] = [toys.series] if spec.multiple else toys.series
        elif spec.required:
            return skip(
                f"The node needs a {spec.type.value} input, so the kit cannot run it on toy "
                "data. Run its own tests with `mapi plugin test`."
            )
    params = _default_params(c.params_schema)
    if any(Draft202012Validator(c.params_schema).iter_errors(params)):
        return skip(
            "The node needs parameters without defaults, so the kit cannot run it on toy "
            "data. Run its own tests with `mapi plugin test`."
        )
    report = check_node(
        export, inputs=inputs, params=params, registry=registry, raise_on_failure=False
    )
    # ``contract`` repeats the static rule with the generic namespace check (which refuses
    # every t_ namespace); the static kit reports the contract with the upload rule
    return _report("node", c.id, report, drop=("contract",))
