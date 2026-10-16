"""A finalized model version with plots for the saved-plot, render and embed tests."""

from __future__ import annotations

import base64
import json
from typing import Any

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.sharing import clear_embed_cache
from mapi_spec.run import Actor, RunState, RunTrigger

from .builders import queued_fit, simulate_outputs
from .fake_executor import FakeExecutor

__all__ = [
    "ARROW",
    "INLINE",
    "PLOTS",
    "complete_run",
    "model_with_plots",
    "plot_source",
    "vl_plot",
]

GENERATED = "2026-10-15T09:40:00Z"
ARROW = b"ARROW1\x00\x00" + bytes(range(200))  # opaque to the control plane: never decoded
INLINE = base64.b64encode(b"ARROW1-inline").decode()
PLOTS_ORIGIN = "http://127.0.0.1:5174"


def vl_plot(pid: str, datasets: dict[str, Any], **prov: str) -> dict[str, Any]:
    return {
        "id": pid,
        "title": pid,
        "kind": "vega-lite",
        "schema_version": "v5",
        "spec": {
            "mark": "line",
            "data": {"name": "d"},
            "encoding": {
                "x": {"field": "t", "type": "quantitative"},
                "y": {"field": "y", "type": "quantitative"},
            },
        },
        "datasets": datasets,
        "caption_plain": "Where the model expects the data to fall.",
        "provenance": {**prov, "generated_at": GENERATED},
    }


PLOTS = [
    vl_plot(
        "core.inline", {"d": {"kind": "inline_arrow", "data": INLINE}}, recipe="core.fan@1.0.0"
    ),
    vl_plot(
        "core.ppc_fan",
        {"d": {"kind": "data_ref", "path": "ui/plotdata/fan.arrow"}},
        recipe="core.ppc_fan@1.0.0",
    ),
    vl_plot("acme.custom", {"d": {"kind": "inline_arrow", "data": INLINE}}, plugin="acme.mmm@1"),
    {
        "id": "core.surface",
        "title": "Surface",
        "kind": "plotly",
        "schema_version": "3.1.0",
        "spec": {"data": [{"type": "surface", "z": {"$ref": "d.y"}}]},
        "datasets": {"d": {"kind": "inline_arrow", "data": INLINE}},
        "provenance": {"recipe": "core.surface@1.0.0", "generated_at": GENERATED},
    },
]


async def _to_finalizing(ctx: ServerContext, run_id: str) -> tuple[m.Run, m.RunAttempt]:
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        s.expunge_all()
    return run, att


async def complete_run(ctx: ServerContext, run_id: str, add: dict[str, tuple[bytes, str]]) -> None:
    run, att = await _to_finalizing(ctx, run_id)
    await simulate_outputs(ctx, run, att, add=add)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED


async def model_with_plots(ctx: ServerContext) -> dict[str, Any]:
    """A ready model version with the four plots and its ``fan.arrow`` bundle object."""
    run_id = await queued_fit(ctx)
    run, att = await _to_finalizing(ctx, run_id)
    await simulate_outputs(
        ctx,
        run,
        att,
        replace={"plots.json": json.dumps(PLOTS).encode()},
        add={"ui/plotdata/fan.arrow": (ARROW, "application/vnd.apache.arrow.stream")},
    )
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        done = await load_run(s, None, run.id)
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
    clear_embed_cache()
    return {"model_id": mv.model_id, "version": mv.version, "mv_id": mv.id}


def plot_source(model: dict[str, Any], plot_id: str) -> dict[str, Any]:
    return {"model_id": model["model_id"], "version": model["version"], "plot_id": plot_id}
