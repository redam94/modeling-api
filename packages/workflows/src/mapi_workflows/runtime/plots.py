"""Plots in the runtime: the ``plot.render`` node and the plot outputs of other nodes.

A plot output is a sanitized :class:`~mapi_spec.plots.PlotSpec` as JSON (``kind``, ``spec``
and ``inline_arrow`` datasets), the same document a fit run stores in its ``plots.json``:
the web run view renders it in the plot sandbox and ``report.summary`` puts it in a
``plot`` section. Recipes resolve through the node's registry first (plugin recipes), then
the bundled catalog (:func:`mapi_plots.recipes.all_recipes`), so every recipe id works.

Sizes: a plot larger than :data:`MAX_PLOT_BYTES` fails the node (draw fewer draws or
panels); the sandbox inlines a plot up to :data:`~mapi_workflows.runtime.sandbox.
MAX_INLINE_PLOT` bytes in ``node_result.json`` and writes a larger one as
``out/<port>.json``.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

import xarray as xr
from mapi_sdk.harness import score_bundle
from mapi_sdk.registry import Registry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_workflows.runtime.models import _registry, _request, _target_names
from mapi_workflows.runtime.values import DatasetValue, ModelValue, NodeContext, NodeOutcome
from pydantic import JsonValue

__all__ = [
    "FORECAST_PLOT",
    "MAX_PLOT_BYTES",
    "PLOT_NODES",
    "RESIDUALS_PLOT",
    "fit_predictions_tree",
    "forecast_plot",
    "plot_node",
    "render_recipe",
    "residuals_plot",
    "try_render",
]

MAX_PLOT_BYTES: Final = 1024 * 1024
"""The largest plot a node may output (the control plane reads JSON outputs up to 1 MiB)."""

FORECAST_PLOT: Final = "core.ppc_fan"
"""The recipe of ``model.forecast``'s ``plot`` output."""
RESIDUALS_PLOT: Final = "core.residuals_over_time"
"""The recipe of ``evaluate.residuals``'s ``plot`` output."""

TreeSource = tuple[str, xr.DataTree | Callable[[], xr.DataTree | None]]


def _has_recipe(registry: Registry | None, ref: str) -> bool:
    if registry is None:
        return False
    try:
        registry.plot(ref)
    except (MapiError, KeyError, ValueError):
        return False
    return True


def _bundled(ref: str) -> bool:
    from mapi_plots.recipes import all_recipes
    from mapi_spec.refs import UseRef

    parsed = UseRef.parse(ref) if "@" in ref else None
    rid = parsed.id if parsed else ref
    return any(
        r.id == rid and (parsed is None or parsed.resolve([r.version]) is not None)
        for r in all_recipes()
    )


def _check_recipe(ref: str, registry: Registry | None) -> Registry | None:
    """The registry to render ``ref`` with (``None``: the bundled recipes); raises
    ``REF_UNRESOLVED`` when neither knows it."""
    if _has_recipe(registry, ref):
        return registry
    if _bundled(ref):
        return None
    raise MapiError(
        f"no plot recipe satisfies {ref!r}. Look at the plot catalog for the recipe ids.",
        code=ErrorCode.REF_UNRESOLVED,
    )


def render_recipe(
    ref: str,
    trees: Iterable[TreeSource],
    *,
    settings: Mapping[str, Any] | None = None,
    registry: Registry | None = None,
) -> tuple[dict[str, JsonValue], str]:
    """Render ``ref`` against each tree in turn until it draws: the PlotSpec JSON and the
    name of the tree that drew it. Raises ``MapiError`` with every tree's reason."""
    from mapi_plots.recipes import render_plots

    reg = _check_recipe(ref, registry)
    rid = ref.split("@", 1)[0]
    reasons: list[str] = []
    for name, source in trees:
        try:
            tree = source() if callable(source) else source
        except MapiError as exc:
            reasons.append(f"{name}: {exc.detail}")
            continue
        if tree is None:
            continue
        with warnings.catch_warnings():  # e.g. all-NaN bands over a forecast's history
            warnings.simplefilter("ignore", RuntimeWarning)
            report = render_plots(tree, [ref], registry=reg, options={rid: dict(settings or {})})
        if report.plots:
            return report.plots[0].model_dump(mode="json", exclude_none=True), name
        for issue in report.issues:
            detail = f" ({issue.untrusted_text[:300]})" if issue.untrusted_text else ""
            reasons.append(f"{name}: {issue.message}{detail}")
    raise MapiError(
        f"the recipe {ref!r} could not draw a plot from the inputs of the node",
        code=ErrorCode.SCHEMA_INVALID,
        untrusted_text="; ".join(reasons)[:8000] or "no input had the data that it needs",
    )


def try_render(
    ref: str,
    trees: Iterable[TreeSource],
    *,
    settings: Mapping[str, Any] | None = None,
    registry: Registry | None = None,
) -> tuple[dict[str, JsonValue] | None, str | None]:
    """:func:`render_recipe` for optional plot outputs: ``(plot, None)`` or ``(None,
    warning)``. A plot never fails the node that draws it on the side."""
    try:
        plot, _ = render_recipe(ref, trees, settings=settings, registry=registry)
    except MapiError as exc:
        return None, f"the {ref} plot was not drawn: {exc.detail}"
    if _size(plot) > MAX_PLOT_BYTES:
        return None, f"the {ref} plot was not drawn: it is larger than {MAX_PLOT_BYTES} bytes"
    return plot, None


def _size(plot: Mapping[str, JsonValue]) -> int:
    return len(json.dumps(plot, separators=(",", ":"), allow_nan=False, default=str))


def _with_binding(tree: xr.DataTree, mv: ModelValue) -> xr.DataTree:
    """``tree`` with the bundle's ``/binding`` node, so recipes title axes with the
    dataset's own names."""
    if "binding" not in tree.children:
        tree["binding"] = xr.DataTree(
            xr.Dataset(attrs={"json": mv.bundle.resolved.binding.model_dump_json()})
        )
    return tree


def fit_predictions_tree(
    mv: ModelValue, registry: Registry | None, data: xr.Dataset | None = None
) -> xr.DataTree:
    """In-sample predictions of a model over ``data`` (default: its fit data) with the
    observed target as ``observed_data``: what fit-versus-actual and residual plots read."""
    payload = data if data is not None else mv.data
    if payload is None:
        raise MapiError(
            "the fit data of the model is not available for its predictions",
            code=ErrorCode.SCHEMA_INVALID,
        )
    bundle = mv.bundle
    tree = score_bundle(
        bundle,
        _request(bundle.spec, "predict"),
        data=payload,
        registry=registry,
        return_full=True,
        tree=mv.tree,
    )
    _, _, target = _target_names(bundle)
    if target in payload and "observed_data" not in tree.children:
        tree["observed_data"] = xr.DataTree(payload[[target]])
    return _with_binding(tree, mv)


def forecast_plot(
    mv: ModelValue, tree: xr.DataTree, registry: Registry | None
) -> tuple[dict[str, JsonValue] | None, str | None]:
    """The fan chart of a forecast's score result (``model.forecast``)."""
    return try_render(FORECAST_PLOT, [("forecast", _with_binding(tree, mv))], registry=registry)


def residuals_plot(
    mv: ModelValue, scored: xr.DataTree | None, registry: Registry | None
) -> tuple[dict[str, JsonValue] | None, str | None]:
    """Actual minus predicted over the fit data (``evaluate.residuals``): the in-sample
    score result with the observed target."""
    if scored is None or mv.data is None:
        return None, None
    tree = scored.copy()
    _, _, target = _target_names(mv.bundle)
    if target in mv.data and "observed_data" not in tree.children:
        tree["observed_data"] = xr.DataTree(mv.data[[target]])
    return try_render(RESIDUALS_PLOT, [("predictions", _with_binding(tree, mv))], registry=registry)


def _sources(ctx: NodeContext, source: str) -> list[TreeSource]:
    dv = ctx.dataset("data", required=False)
    mv = ctx.model("model", required=False)
    if dv is None and mv is None:
        raise MapiError(
            "plot.render needs a model or a dataset. Connect one of them.",
            code=ErrorCode.SCHEMA_INVALID,
        )
    reg = _registry(ctx)
    out: list[TreeSource] = []
    if dv is not None and source in ("auto", "data"):
        out.append(("data", _data_tree(dv)))
    if mv is not None and source in ("auto", "model"):
        out.append(("model", lambda: _with_binding(mv.bundle.to_tree(), mv)))
    if mv is not None and source in ("auto", "predictions"):
        payload = dv.data if dv is not None and source == "predictions" else None
        out.append(("predictions", lambda: fit_predictions_tree(mv, reg, payload)))
    if not out:
        raise MapiError(
            f"plot.render has no {source!r} input. Connect it, or set 'source' to 'auto'.",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return out


def _data_tree(dv: DatasetValue) -> xr.DataTree:
    return xr.DataTree(dataset=dv.data)


def plot_node(ctx: NodeContext) -> NodeOutcome:
    ref = str(ctx.params.get("recipe") or "")
    if not ref:
        raise MapiError("plot.render needs a 'recipe'", code=ErrorCode.WORKFLOW_INVALID)
    raw = ctx.params.get("settings")
    settings: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    title = ctx.params.get("title")
    if isinstance(title, str) and title:
        settings["title"] = title
    source = str(ctx.params.get("source") or "auto")
    ctx.info(f"drawing {ref}")
    plot, used = render_recipe(ref, _sources(ctx, source), settings=settings, registry=ctx.registry)
    size = _size(plot)
    if size > MAX_PLOT_BYTES:
        raise MapiError(
            f"the plot is {size} bytes; the limit is {MAX_PLOT_BYTES}. Use fewer draws, "
            "panels or frames in the settings of the recipe.",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
        )
    prov = plot.get("provenance")
    recipe = prov.get("recipe") if isinstance(prov, dict) else None
    result: dict[str, JsonValue] = {
        "recipe": recipe if isinstance(recipe, str) else ref,
        "kind": plot.get("kind"),
        "source": used,
        "bytes": size,
        "title": plot.get("title"),
    }
    return NodeOutcome(result=result, outputs={"plot": plot})


PLOT_NODES: Final = {"plot.render": plot_node}
