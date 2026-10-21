"""Dashboard inputs (v0.16, plan D8, D9, D14): the grid, what-ifs, scenarios and actuals.

The dashboard is the ``trend`` model of ``default/default`` with a two-channel response grid
(the helpers of ``test_dashboards_crud``), published by ``ana`` (admin, so a runner) and
granted to ``cleo`` (a client). Runs are never executed: an exact result is a ``score``
run in the queue, submitted as ``ana`` with the readout's labels.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import inputs
from mapi_server.services.dashboards.inputs import clear_grid_cache, clip_grid
from mapi_server.services.submit import fit_dataset_ref
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller, grant_role, make_user, session_caller
from mapi_spec.dashboards import DashboardSpec
from mapi_spec.views import ResponseGridView
from mapi_spec.whatif import WhatIfScenario, evaluate_scenario
from sqlalchemy import select, update
from test_dashboards_crud import GRID, create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration

REAL_EXACT = inputs.exact_request


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "client_run_rate_per_hour": 2}
    )


@pytest.fixture(autouse=True)
def predict_for_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``trend`` model has no spend input, so an exact result is a plain prediction of
    the fitted data here (the counterfactual itself is built by ``exact_request``)."""

    async def exact(ctx: Any, session: Any, rev: m.DashboardRevision, mv: Any, x: Any) -> Any:
        return inputs.baseline_request(rev.model_ref, await fit_dataset_ref(session, mv))

    monkeypatch.setattr(inputs, "exact_request", exact)


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    clear_grid_cache()
    kit = await dashboard_kit(ctx, client)
    carl = await make_user(ctx, "carl@default.test")
    await grant_role(ctx, carl, "default", "default", "client")
    kit["users"]["carl"] = carl
    kit["carl"] = await session_caller(ctx, client, carl, tenant_id="default", project_id="default")
    return kit


async def _publish(kit: dict[str, Any], **spec: Any) -> str:
    dash_id = await create_published(kit, **spec)
    r = await kit["ana"].put(f"/v1/dashboards/{dash_id}/grants/{kit['users']['carl'].id}")
    assert r.status_code == 200, r.text
    return dash_id


async def score_runs(ctx: ServerContext) -> list[m.Run]:
    async with ctx.db.session() as s:
        return list((await s.execute(select(m.Run).where(m.Run.kind == "score"))).scalars())


# ======================================================================================= grid


def test_the_grid_is_clipped_to_the_channels_and_their_ranges() -> None:
    grid = ResponseGridView.model_validate(
        {
            **GRID,
            "multipliers": [0.25, 0.5, 1.0, 1.5, 2.0],
            "contribution": [[[300.0, 700.0, 1000.0, 1150.0, 1200.0],
                              [100.0, 300.0, 500.0, 620.0, 700.0]],
                             [[280.0, 650.0, 950.0, 1100.0, 1180.0],
                              [110.0, 320.0, 520.0, 650.0, 720.0]]],
            "spend": [[13000.0, 26000.0, 52000.0, 78000.0, 104000.0],
                      [4500.0, 9000.0, 18000.0, 27000.0, 36000.0]],
        }
    )  # fmt: skip
    spec = DashboardSpec.model_validate(
        dashboard_spec(channels=[{"name": "tv", "label": "TV", "min_mult": 0.6, "max_mult": 1.2}])
    )
    clipped = clip_grid(grid, spec)
    assert clipped.channels == ["tv"]
    # 0.6-1.2 needs the grid points around both ends: 0.5 and 1.5 stay, 0.25 and 2.0 go
    assert clipped.multipliers == [0.5, 1.0, 1.5]
    assert clipped.spend == [[26000.0, 52000.0, 78000.0]]
    # search left the grid: its contribution at the current plan moved into the baseline
    assert clipped.baseline == [2600.0 + 500.0, 2700.0 + 520.0]
    full = evaluate_scenario(grid, WhatIfScenario(multipliers={"tv": 1.2}))
    part = evaluate_scenario(clipped, WhatIfScenario(multipliers={"tv": 1.2}))
    assert part.kpi is not None and full.kpi is not None
    assert part.kpi.mean == pytest.approx(full.kpi.mean)


async def test_grid_and_whatifs_answer_without_a_run(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit)
    cleo: Caller = kit["cleo"]
    r = await cleo.get(f"/v1/dashboards/{dash_id}/grid")
    assert r.status_code == 200, r.text
    assert r.json()["channels"] == ["tv", "search"] and r.json()["multipliers"] == [0.5, 1.0, 1.5]
    plan = {"multipliers": {"tv": 0.8, "search": 1.3}}
    r = await cleo.post(f"/v1/dashboards/{dash_id}/whatif:evaluate", json=plan)
    assert r.status_code == 200, r.text
    want = evaluate_scenario(ResponseGridView.model_validate(GRID), WhatIfScenario(**plan))
    assert r.json()["delta_kpi"] == pytest.approx(want.kpi_delta.mean)
    assert "than the current plan" in r.json()["lead"]
    assert {c["name"] for c in r.json()["channels"]} == {"tv", "search"}
    # outside a slider's range (1.5 is the default max) or an unknown channel: 422
    for bad in ({"tv": 1.6}, {"radio": 1.0}):
        r = await cleo.post(f"/v1/dashboards/{dash_id}/whatif:evaluate", json={"multipliers": bad})
        assert r.status_code == 422 and r.json()["code"] == "SCENARIO_OUT_OF_RANGE", r.text
    assert await score_runs(ctx) == []


async def test_inputs_the_spec_turns_off_answer_409(kit: dict[str, Any]) -> None:
    dash_id = await _publish(kit, whatif={"enabled": False})
    for path in ("grid", "actuals/template"):
        r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}/{path}")
        assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED", r.text
    r = await kit["cleo"].post(
        f"/v1/dashboards/{dash_id}/scenarios", json={"multipliers": {"tv": 0.9}, "exact": True}
    )
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED"


# ================================================================================== scenarios


async def test_a_saved_scenario_is_ready_at_once_and_private(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit)
    base = f"/v1/dashboards/{dash_id}"
    r = await kit["cleo"].post(f"{base}/scenarios", json={"multipliers": {"tv": 0.8}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "scenario" and body["visibility"] == "private"
    assert body["status"] == "ready" and body["snapshot"]["scenario"] == {"tv": 0.8}
    assert body["title"] == "TV −20 %"
    seq = body["seq"]
    assert await score_runs(ctx) == []
    # cleo and the analysts see it; another client does not
    assert (await kit["cleo"].get(f"{base}/readouts/{seq}")).status_code == 200
    assert (await kit["vic"].get(f"{base}/readouts/{seq}")).status_code == 200
    assert (await kit["carl"].get(f"{base}/readouts/{seq}")).status_code == 404
    seqs = [i["seq"] for i in (await kit["carl"].get(f"{base}/readouts")).json()["items"]]
    assert seq not in seqs
    # a private readout cannot be published into the history
    r = await kit["eve"].post(f"{base}/readouts/{seq}:publish")
    assert r.status_code == 409


async def test_an_exact_scenario_runs_as_the_publisher_with_labels(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit, whatif={"enabled": True, "exact": True})
    base = f"/v1/dashboards/{dash_id}"
    plan = {"multipliers": {"tv": 0.8}, "exact": True}
    r = await kit["cleo"].post(f"{base}/scenarios", json=plan, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 202, r.text
    head = r.json()
    assert head["status"] == "running" and "snapshot" not in head
    runs = await score_runs(ctx)
    assert len(runs) == 1
    run = runs[0]
    assert run.created_by == kit["users"]["ana"].id  # the publisher, never the client
    assert run.labels == {
        "mapi.dashboard": dash_id, "mapi.readout": str(head["seq"]), "mapi.trigger": "whatif",
    }  # fmt: skip
    assert run.payload["request"]["mode"] == "predict"
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (dash_id, head["seq"]))
        assert (
            row is not None and row.run_ids == [run.id] and row.actor_id == kit["users"]["cleo"].id
        )
    # the same key replays the same readout; somebody else's key use is a conflict
    again = await kit["cleo"].post(
        f"{base}/scenarios", json=plan, headers={"Idempotency-Key": "k1"}
    )
    assert again.json()["seq"] == head["seq"] and len(await score_runs(ctx)) == 1
    other = await kit["carl"].post(
        f"{base}/scenarios", json=plan, headers={"Idempotency-Key": "k1"}
    )
    assert other.status_code == 409 and other.json()["code"] == "IDEMPOTENCY_CONFLICT"


async def test_a_cached_exact_result_answers_without_a_new_run(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit, whatif={"enabled": True, "exact": True})
    base = f"/v1/dashboards/{dash_id}"
    plan = {"multipliers": {"tv": 0.8}, "exact": True}
    first = (await kit["cleo"].post(f"{base}/scenarios", json=plan)).json()
    async with ctx.db.session() as s:  # the run finished and the readout was built
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id,
                   m.DashboardReadout.seq == first["seq"])
            .values(status="ready", ready_at=m.DashboardReadout.created_at,
                    snapshot={"lead": "Cached.", "scenario": {"tv": 0.8}})
        )  # fmt: skip
    mine = await kit["cleo"].post(f"{base}/scenarios", json=plan)
    assert mine.status_code == 200 and mine.json()["seq"] == first["seq"]
    theirs = await kit["carl"].post(f"{base}/scenarios", json=plan)
    assert theirs.status_code == 200, theirs.text
    copy = theirs.json()
    assert copy["seq"] != first["seq"] and copy["snapshot"]["lead"] == "Cached."
    assert len(await score_runs(ctx)) == 1
    # carl's copy is his own: cleo cannot read it
    assert (await kit["cleo"].get(f"{base}/readouts/{copy['seq']}")).status_code == 404


async def test_run_caps_answer_429_with_retry_after(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(
        kit, whatif={"enabled": True, "exact": True}, limits={"exact_runs_per_day": 3}
    )
    base = f"/v1/dashboards/{dash_id}/scenarios"
    for x in (0.6, 0.7):
        r = await kit["cleo"].post(base, json={"multipliers": {"tv": x}, "exact": True})
        assert r.status_code == 202, r.text
    # the client's hourly cap (2 in this module)
    r = await kit["cleo"].post(base, json={"multipliers": {"tv": 0.9}, "exact": True})
    assert r.status_code == 429 and r.json()["code"] == "DASHBOARD_RUN_LIMIT", r.text
    assert int(r.headers["retry-after"]) >= 1
    assert "today's limit" in r.json()["detail"]
    # the dashboard's daily cap (3) counts every caller
    r = await kit["carl"].post(base, json={"multipliers": {"tv": 1.2}, "exact": True})
    assert r.status_code == 202, r.text
    r = await kit["carl"].post(base, json={"multipliers": {"tv": 1.3}, "exact": True})
    assert r.status_code == 429 and r.json()["code"] == "DASHBOARD_RUN_LIMIT"
    assert len(await score_runs(ctx)) == 3
    # grid scenarios are free: never capped
    r = await kit["carl"].post(base, json={"multipliers": {"tv": 1.3}})
    assert r.status_code == 200


async def test_concurrent_exact_runs_cannot_pass_the_caps_together(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    """The count and the insert run under the dashboard row lock: parallel requests with
    distinct plans (no cache hit) see each other's runs, so the caps hold."""
    dash_id = await _publish(
        kit, whatif={"enabled": True, "exact": True}, limits={"exact_runs_per_day": 3}
    )
    base = f"/v1/dashboards/{dash_id}/scenarios"
    plans = [{"multipliers": {"tv": 0.6 + k / 20}, "exact": True} for k in range(8)]
    answers = await asyncio.gather(*(kit["cleo"].post(base, json=p) for p in plans))
    codes = sorted(r.status_code for r in answers)
    assert codes.count(202) == 2, [r.text for r in answers]  # the client's hourly cap
    assert codes.count(429) == 6
    more = [{"multipliers": {"search": 0.6 + k / 20}, "exact": True} for k in range(6)]
    answers = await asyncio.gather(*(kit["carl"].post(base, json=p) for p in more))
    assert sorted(r.status_code for r in answers).count(202) == 1  # the daily cap (3)
    assert len(await score_runs(ctx)) == 3


async def test_a_demoted_publisher_pauses_the_live_inputs(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit, whatif={"enabled": True, "exact": True})
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == kit["users"]["ana"].id)
            .values(role="editor")
        )
    r = await kit["cleo"].post(
        f"/v1/dashboards/{dash_id}/scenarios", json={"multipliers": {"tv": 0.8}, "exact": True}
    )
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_LIVE_PAUSED", r.text
    assert await score_runs(ctx) == []
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None and dash.live_inputs == "paused"
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}")
    assert r.json()["live_inputs"] == "paused"
    # grid answers stay (they spend nothing)
    r = await kit["cleo"].post(
        f"/v1/dashboards/{dash_id}/whatif:evaluate", json={"multipliers": {"tv": 0.8}}
    )
    assert r.status_code == 200


async def test_a_version_that_ran_uploaded_code_is_refused(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit, whatif={"enabled": True, "exact": True})
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, kit["model"]["mv_id"])
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlay_hash": "sha256:" + "1" * 64}
    for exact in (False, True):
        r = await kit["cleo"].post(
            f"/v1/dashboards/{dash_id}/scenarios",
            json={"multipliers": {"tv": 0.8}, "exact": exact},
        )
        assert r.json()["code"] == "CAPABILITY_UNSUPPORTED", r.text
    assert await score_runs(ctx) == []


# ==================================================================================== actuals


def _actuals(ys: list[Any], *, months: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    labels = months or [f"2024-{k + 1:02d}-01" for k in range(len(ys))]
    return {
        "inputs": {
            "coords": {"t": {"dims": ["t"], "data": labels}},
            "data_vars": {"y": {"dims": ["t"], "data": ys}},
        },
        **extra,
    }


async def test_the_actuals_template_names_the_kpi_column(kit: dict[str, Any]) -> None:
    dash_id = await _publish(kit, actuals={"enabled": True})
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}/actuals/template")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "compared" and body["kpi_column"] == "y"
    assert body["example_csv"].splitlines()[0].split(",")[:2] == ["t", "y"]
    assert body["max_rows"] == 520


async def test_actuals_are_checked_with_pointers_before_a_run(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit, actuals={"enabled": True})
    url = f"/v1/dashboards/{dash_id}/actuals"
    no_kpi = {"inputs": {"coords": {"t": {"dims": ["t"], "data": ["2024-01-01"]}},
                         "data_vars": {"x": {"dims": ["t"], "data": [1.0]}}}}  # fmt: skip
    r = await kit["cleo"].post(url, json=no_kpi)
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_INVALID", r.text
    assert r.json()["errors"][0]["pointer"] == "/inputs/data_vars/y"
    gap = _actuals([1.0, 2.0], months=["2024-01-01", "2024-03-01"])
    r = await kit["cleo"].post(url, json=gap)
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_INVALID", r.text
    assert any(i["pointer"] == "/inputs/coords/t" for i in r.json()["errors"])
    text = _actuals(["four hundred"])
    r = await kit["cleo"].post(url, json=text)
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_INVALID", r.text
    assert r.json()["errors"][0]["pointer"].startswith("/inputs/data_vars/y")
    assert await score_runs(ctx) == []


async def test_compared_actuals_score_as_the_publisher_and_wait_for_review(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    dash_id = await _publish(kit, actuals={"enabled": True})
    base = f"/v1/dashboards/{dash_id}"
    r = await kit["cleo"].post(f"{base}/actuals", json=_actuals([410.0, 412.5]))
    assert r.status_code == 202, r.text
    head = r.json()
    assert head["kind"] == "actuals" and head["visibility"] == "pending_review"
    (run,) = await score_runs(ctx)
    assert run.created_by == kit["users"]["ana"].id
    assert run.payload["request"]["mode"] == "predict"
    assert "inline.data" in run.inputs
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (dash_id, head["seq"]))
        assert row is not None and row.input_ref is not None
        assert row.input_ref["kpi_column"] == "y" and row.input_fingerprint
    # the uploader and analysts see it; the other client does not until it is published
    assert (await kit["cleo"].get(f"{base}/readouts/{head['seq']}")).status_code == 200
    assert (await kit["carl"].get(f"{base}/readouts/{head['seq']}")).status_code == 404
    r = await kit["eve"].post(f"{base}/readouts/{head['seq']}:publish")
    assert r.status_code == 200 and r.json()["visibility"] == "published"
    assert (await kit["carl"].get(f"{base}/readouts/{head['seq']}")).status_code == 200


async def test_a_capable_model_conditions_on_the_actuals(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    from test_endpoints_actuals import _capable

    await _capable(ctx)
    dash_id = await _publish(kit, actuals={"enabled": True, "publish": "auto"})
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}")
    assert r.json()["inputs"]["actuals_mode"] == "conditioned"
    r = await kit["cleo"].post(
        f"/v1/dashboards/{dash_id}/actuals", json=_actuals([410.0, None, None])
    )
    assert r.status_code == 202, r.text
    assert r.json()["visibility"] == "published"
    (run,) = await score_runs(ctx)
    req = run.payload["request"]
    assert req["mode"] == "forecast" and req["horizon"] == {"dim": "t", "periods": 2}


async def test_an_exact_result_needs_a_spend_input_per_channel(
    kit: dict[str, Any], ctx: ServerContext
) -> None:
    """The real request builder: the counterfactual multiplies the model's ``spend``-role
    input; ``trend`` has none, so exact results are not available on it."""
    from mapi_spec.errors import MapiError

    dash_id = await _publish(kit, whatif={"enabled": True, "exact": True})
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None and dash.published_revision_id is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        mv = await s.get(m.ModelVersion, kit["model"]["mv_id"])
        assert rev is not None and mv is not None
        with pytest.raises(MapiError) as err:
            await REAL_EXACT(ctx, s, rev, mv, {"tv": 0.8})
    assert err.value.code.value == "DASHBOARD_INPUT_DISABLED"


@pytest.mark.parametrize(
    ("representation", "summary"),
    [
        (None, ["mean", "hdi_90"]),
        ("draws", ["mean", "hdi_90"]),
        ("quantiles", ["median", "eti_90"]),  # statsmodels forecasts
        ("moments", ["mean", "eti_90"]),
        ("interval", ["point", "interval"]),
        ("point", ["mean"]),
    ],
)
def test_readouts_ask_for_summaries_the_representation_allows(
    representation: str | None, summary: list[str]
) -> None:
    """A ``quantiles`` model cannot give a mean or an HDI: asking for them failed every
    baseline, refresh and actuals readout of a statsmodels forecast dashboard."""
    from mapi_spec.capabilities import Representation, summary_allowed

    out = inputs.readout_output(representation)
    assert out.summary == summary
    rep = Representation(representation or "draws")
    assert all(summary_allowed(rep, s) for s in out.summary)
    req = inputs.baseline_request("model://default/trend@v1", "dataset://default/co2@v1", rep)
    assert req.output.summary == summary
