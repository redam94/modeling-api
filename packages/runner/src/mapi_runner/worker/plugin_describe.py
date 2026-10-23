"""``plugin_describe`` (v0.19, D8): what one uploaded wheel exports, and its kit report.

The run mounts exactly one overlay (the upload under review) like any run that uses uploaded
code. It writes ``plugin/describe.json`` (:class:`~mapi_spec.plugins.PluginDescribe`, at most
:data:`~mapi_spec.plugins.DESCRIBE_MAX_BYTES`):

* ``exports``: the catalog entries of that overlay only
  (:func:`~mapi_runner.catalog_export.full_catalog` with ``only_distribution="upload:<pvid>"``;
  built-in and other installed entries never appear), each with ``provider`` =
  ``upload:<pvid>`` and ``overlay_hash`` = the pinned sha256;
* ``kit``: the static kit and, with ``data_kits``, the data kit on toy fixtures
  (:mod:`mapi_runner.plugin_kits`);
* ``plot_examples``: each exported recipe's example rendered as ``PlotSpec`` JSON, keyed by
  plot id (the control plane sanitizes them again);
* ``diagnostics``: what the overlay could not register.

Everything here is a claim made by code the plugin controls (it runs in this process): the
control plane re-checks the JSON with :func:`mapi_spec.plugins.check_describe` and never
trusts the kit for a security decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from mapi_spec.catalog import Catalog, CatalogDiagnostic
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.jobs import PLUGIN_DESCRIBE_OUTPUT, PluginDescribeJob
from mapi_spec.plots import PlotExamples
from mapi_spec.plugins import (
    DESCRIBE_MAX_BYTES,
    KitCheck,
    PluginDescribe,
    PluginExports,
    PluginKitReport,
)
from mapi_spec.run import RunnerPhase, catalog_digest

from mapi_runner.catalog_export import full_catalog
from mapi_runner.plugin_kits import KitTimeout, data_checks, static_checks, time_limit
from mapi_runner.worker.context import WorkerContext

__all__ = ["run_plugin_describe"]

_IMAGE_CATALOG_DIGEST: Final = Path("/opt/mapi/catalog.digest")
"""The base image's catalog digest, baked in at build time (runner images)."""
_KIT_MAX: Final = 500
_DIAGNOSTICS_MAX: Final = 100
_EXAMPLES_MAX: Final = 64
_EXPORT_CAPS: Final = {
    "models": 64,
    "steps": 128,
    "plots": 128,
    "workflow_nodes": 64,
    "workflow_templates": 32,
}
_WARNINGS: Final = 20


def run_plugin_describe(ctx: WorkerContext, job: PluginDescribeJob) -> PluginDescribe:
    """Describe the overlay of ``job`` and write :data:`PLUGIN_DESCRIBE_OUTPUT`."""
    overlay = next((o for o in ctx.overlays if o.namespace == job.namespace), None)
    if overlay is None or overlay.pvid != job.plugin_version_id or len(ctx.overlays) != 1:
        raise MapiError(
            "The run does not mount exactly the uploaded version it describes.",
            code=ErrorCode.EXTENSION_UNAVAILABLE,
        )
    provider = f"upload:{overlay.pvid}"
    ctx.state(RunnerPhase.VALIDATE, "loading the uploaded code")
    registry = ctx.registry
    catalog = full_catalog(registry, only_distribution=provider)
    kit: list[KitCheck] = []
    exports = _exports(catalog, job.overlay_sha256, kit)

    ctx.state(RunnerPhase.VALIDATE, "running the static checks")
    kit += static_checks(registry, namespace=job.namespace, timeout_s=job.data_check_timeout_s)
    if job.data_kits:
        ctx.state(RunnerPhase.EVALUATE, "running the checks on toy data")
        kit += data_checks(registry, namespace=job.namespace, timeout_s=job.data_check_timeout_s)
    ctx.state(RunnerPhase.VALIDATE, "rendering the recipe examples")
    examples = _examples(registry, provider, kit, job.data_check_timeout_s)

    describe = PluginDescribe(
        plugin_version_id=job.plugin_version_id,
        overlay_sha256=job.overlay_sha256,
        base_image_digest=_base_digest(registry, provider),
        exports=exports,
        plot_examples=examples,
        kit=_report(kit),
        diagnostics=_diagnostics(catalog.diagnostics),
    )
    describe = _fit(describe)
    ctx.state(RunnerPhase.UPLOAD, "writing the describe result")
    ctx.write_output(PLUGIN_DESCRIBE_OUTPUT, describe.model_dump_json().encode("utf-8"))
    for c in [c for c in describe.kit.checks if c.status == "fail"][:_WARNINGS]:
        ctx.warning(
            "PLUGIN_KIT_FAILED",
            f"a {c.kind} check of the upload failed: {c.check}",
            untrusted_text=" ".join(x for x in (c.entry_id, c.message) if x) or None,
        )
    ctx.progress(1.0, "the upload is described", force=True)
    return describe


def _exports(catalog: Catalog, sha: str, kit: list[KitCheck]) -> PluginExports:
    """The overlay's entries with their provenance (capped; a cap overflow fails the kit)."""
    groups: dict[str, list[Any]] = {
        "models": [m.model_copy(update={"overlay_hash": sha}) for m in catalog.models],
        "steps": [s.model_copy(update={"overlay_hash": sha}) for s in catalog.steps],
        "plots": [p.model_copy(update={"overlay_hash": sha}) for p in catalog.plots],
        "workflow_nodes": list(catalog.workflow_nodes),
        "workflow_templates": [
            t.model_copy(update={"overlay_hash": sha}) for t in catalog.workflow_templates
        ],
    }
    for name, items in groups.items():
        cap = _EXPORT_CAPS[name]
        if len(items) > cap:
            kit.append(
                KitCheck(
                    kind="package",
                    check="exports_limit",
                    status="fail",
                    message=f"The package exports {len(items)} {name}; the limit is {cap}.",
                )
            )
            groups[name] = items[:cap]
    return PluginExports(**groups)


def _examples(
    registry: Any, provider: str, kit: list[KitCheck], timeout_s: float
) -> dict[str, dict[str, Any]]:
    """``{plot id: PlotSpec JSON}`` of the overlay's recipes with an example."""
    out: dict[str, dict[str, Any]] = {}
    for recipe in registry.plots():
        entry = recipe.catalog_entry()
        if registry.origin_of("plot", entry.id, entry.version) != provider:
            continue
        make = getattr(recipe, "example_spec", None)
        if make is None or len(out) >= _EXAMPLES_MAX:
            continue
        try:
            with time_limit(timeout_s):
                spec = make()
            if spec is None:
                continue
            PlotExamples(plots={entry.ref: spec})  # self-contained, like the image describe
            out[entry.id] = json.loads(spec.model_dump_json())
        except KitTimeout:
            kit.append(
                KitCheck(
                    entry_id=entry.id,
                    kind="plot",
                    check="example",
                    status="fail",
                    message=f"Rendering the example took longer than {timeout_s:g} s.",
                )
            )
        except Exception as exc:  # recipe code: the example is left out and reported
            kit.append(
                KitCheck(
                    entry_id=entry.id,
                    kind="plot",
                    check="example",
                    status="fail",
                    message=f"The example could not be rendered: {type(exc).__name__}: {exc}",
                )
            )
    return out


def _report(checks: list[KitCheck]) -> PluginKitReport:
    """At most 500 checks, failures first when there are more."""
    ok = not any(c.status == "fail" for c in checks)
    if len(checks) > _KIT_MAX:
        failed = [c for c in checks if c.status == "fail"]
        rest = [c for c in checks if c.status != "fail"]
        checks = (failed + rest)[:_KIT_MAX]
    return PluginKitReport(ok=ok, checks=checks)


def _diagnostics(rows: list[CatalogDiagnostic]) -> list[CatalogDiagnostic]:
    return rows[:_DIAGNOSTICS_MAX]


def _base_digest(registry: Any, provider: str) -> str:
    """The base image's catalog digest as this run sees it: the digest baked into the image
    when there is one, else the digest of the step/model/plot catalog without uploads.

    Informational: the control plane records its own image digest for the describe."""
    try:
        text = _IMAGE_CATALOG_DIGEST.read_text(encoding="ascii").strip()
    except OSError:
        text = ""
    if text.startswith("sha256:") and len(text) == 71:
        return text
    base = registry.catalog()
    return catalog_digest(
        Catalog(
            steps=[s for s in base.steps if s.provider != provider],
            models=[m for m in base.models if m.provider != provider],
            plots=[p for p in base.plots if p.provider != provider],
        )
    )


def _fit(d: PluginDescribe) -> PluginDescribe:
    """Keep the JSON under :data:`DESCRIBE_MAX_BYTES`: drop the largest examples first, then
    the diagnostics; a result that is still too large fails the run."""

    def size(x: PluginDescribe) -> int:
        return len(x.model_dump_json().encode("utf-8"))

    if size(d) <= DESCRIBE_MAX_BYTES:
        return d
    examples = dict(d.plot_examples)
    dropped: list[str] = []
    for key in sorted(examples, key=lambda k: -len(json.dumps(examples[k]))):
        del examples[key]
        dropped.append(key)
        trial = d.model_copy(update={"plot_examples": dict(examples)})
        if size(trial) <= DESCRIBE_MAX_BYTES:
            break
    checks = list(d.kit.checks)
    if dropped:
        checks.append(
            KitCheck(
                kind="package",
                check="describe_size",
                status="fail",
                message=f"The describe result is too large; {len(dropped)} recipe examples "
                "were left out. Make the examples smaller.",
            )
        )
    d = d.model_copy(update={"plot_examples": examples, "kit": _report(checks)})
    if size(d) > DESCRIBE_MAX_BYTES:
        d = d.model_copy(update={"diagnostics": []})
    if size(d) > DESCRIBE_MAX_BYTES:
        raise MapiError(
            f"The describe result of the upload is larger than {DESCRIBE_MAX_BYTES} bytes.",
            code=ErrorCode.RESOURCE_EXHAUSTED,
        )
    return d
