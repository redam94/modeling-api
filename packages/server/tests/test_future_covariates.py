"""Forecast score runs whose future covariates are extended from the fitted series get the
model's fit dataset as their ``history`` input (M3, ``ScoreRequest.future_covariates``)."""

from __future__ import annotations

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import queued_fit, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.jobs import INPUT_HISTORY
from mapi_spec.run import Actor, RunTrigger

pytestmark = pytest.mark.integration


async def _ready_model(ctx: ServerContext) -> m.ModelVersion:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        s.expunge_all()
    await simulate_outputs(ctx, run, att)
    await Finalizer(ctx).finalize(run.id, att.attempt)
    async with ctx.db.session() as s:
        fitted = await load_run(s, None, run_id)
        mv = await s.get(m.ModelVersion, fitted.output_model_version_id)
        assert mv is not None
        s.expunge_all()
    return mv


@pytest.mark.parametrize(
    ("policy", "history"),
    [("carry_last", True), ("seasonal_naive", True), ("zero", False), (None, False)],
)
async def test_forecasts_get_the_fit_dataset_when_they_extend_covariates(
    ctx: ServerContext, client: httpx.AsyncClient, policy: str | None, history: bool
) -> None:
    mv = await _ready_model(ctx)
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "model://default/trend@v1",
        "mode": "forecast",
        "horizon": {"dim": "t", "periods": 6},
    }
    if policy is not None:
        request["future_covariates"] = policy
    r = await client.post(f"/v1/models/{mv.model_id}/versions/1:score", json={"request": request})
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        run = await s.get(m.Run, r.json()["run"]["id"])
        fit = await s.get(m.Run, mv.run_id)
        assert run is not None and fit is not None
        assert (INPUT_HISTORY in run.inputs) is history
        if history:
            dv = await s.get(m.DatasetVersion, (fit.dataset_id, fit.dataset_version))
            assert dv is not None
            assert run.inputs[INPUT_HISTORY]["name"] == dv.objects["data.zarr.zip"]["name"]
            assert run.payload["request"]["future_covariates"] == policy
