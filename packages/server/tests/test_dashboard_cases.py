"""Scenario rows, batch scoring and layout 2 baselines (v0.17, WP8, plan §3.2, §3.4, D11,
D13, D17).

The dashboards are layout 2 dashboards of the trend model of the v0.16 tests
(``ts.hsgp_trend``: one ``t`` time dim, a ``y`` target), published by ``ana`` and granted
to ``cleo`` (a client); ``carl`` is a second client. Forecast rows run on the real input
contract. Rows of cases (a ``store`` dim that takes new levels, a ``price`` covariate) use
:data:`CASES_SNAPSHOT` in place of the version's input contract; the runs are queued, never
executed.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import actuals_v2, cases, sources
from mapi_server.services.dashboards.cases import (
    BATCH_CELL_BUDGET,
    batch_cells,
    row_context,
    score_output,
    table_payload,
    table_rows,
)
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller, grant_role, make_user, session_caller
from mapi_spec.dashboard_inputs import rows_payload
from mapi_spec.dashboard_v2 import DashboardSpecV2, ScenarioRowsDef, parse_dashboard_spec
from mapi_spec.endpoints import EndpointSchemaSnapshot
from mapi_spec.errors import MapiError
from mapi_spec.xrjson import XrJsonDataset
from sqlalchemy import select, update
from test_dashboards_crud import dashboard_kit
from test_dashboards_crud_v2 import preset_spec

pytestmark = pytest.mark.integration

CASES_SNAPSHOT = EndpointSchemaSnapshot.model_validate(
    {
        "input_schema": {
            "dims": {
                "store": {"kind": "categorical", "policy": "new_levels",
                          "new_level_strategy": "pooled"},
            },
            "vars": {
                "price": {"dims": ["store"], "dtype": "float64", "role": "covariate"},
                "promo": {"dims": ["store"], "dtype": "bool", "role": "covariate"},
                "y": {"dims": ["store"], "dtype": "float64", "role": "target"},
            },
        },
        "schema_hash": "sha256:" + "0" * 64,
        "score_modes": ["predict"],
        "representation": "draws",
        "fit_coords": {"store": {"kind": "categorical", "size": 2, "values": ["s1", "s2"]}},
    }
)  # fmt: skip

PRICE: dict[str, Any] = {
    "name": "price", "label": "Price", "bind": {"var": "price"}, "type": "number",
    "min": 10.0, "max": 30.0, "step": 0.5, "default": 20.0,
}  # fmt: skip
PROMO: dict[str, Any] = {
    "name": "promo", "label": "Promotion", "bind": {"var": "promo"}, "type": "toggle",
    "default": False,
}  # fmt: skip
CASE_ROWS: dict[str, Any] = {"enabled": True, "outputs": ["y_hat"], "inputs": [PRICE, PROMO]}
BATCH: dict[str, Any] = {"enabled": True, "outputs": ["y_hat"], "id_column": "store",
                         "max_rows": 50}  # fmt: skip
HORIZON_ROWS: dict[str, Any] = {
    "enabled": True, "outputs": ["y_hat"], "horizon": {"dim": "t", "max": 12, "default": 6},
}  # fmt: skip


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "client_run_rate_per_hour": 3}
    )


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    carl = await make_user(ctx, "carl@default.test")
    await grant_role(ctx, carl, "default", "default", "client")
    kit["users"]["carl"] = carl
    kit["carl"] = await session_caller(ctx, client, carl, tenant_id="default", project_id="default")
    return kit


@dataclass
class Submitted:
    """A score the dashboard submitted as its owner (the request and the stored inputs)."""

    owner: str | None
    labels: dict[str, str]
    request: dict[str, Any]
    inputs: dict[str, Any]


def cases_contract(monkeypatch: pytest.MonkeyPatch) -> list[Submitted]:
    """Score rows of cases against :data:`CASES_SNAPSHOT` (the trend model has no case dim,
    so its run admission would refuse them): the submissions are recorded instead."""
    from mapi_server.services.dashboards.readouts import readout_labels

    async def snapshot(session: Any, catalogs: Any, mv: Any) -> EndpointSchemaSnapshot:
        return CASES_SNAPSHOT

    seen: list[Submitted] = []

    async def submit(
        session: Any, octx: ServerContext, row: m.DashboardReadout, request: Any,
        *, extra_inputs: Any = None, inline_summary: Any = None,
    ) -> None:  # fmt: skip
        auth = octx.request
        seen.append(
            Submitted(
                owner=auth.user_id if auth is not None else None,
                labels=readout_labels(row.dashboard_id, row.seq, row.trigger, row.share_id),  # type: ignore[arg-type]
                request=request.model_dump(mode="json", by_alias=True, exclude_none=True),
                inputs=dict(extra_inputs or {}),
            )
        )
        row.run_ids = [f"run_{len(seen):026d}"]
        row.status = "running"
        await session.flush()

    for mod in (cases, actuals_v2):
        monkeypatch.setattr(mod, "live_capabilities_snapshot", snapshot)
        monkeypatch.setattr(mod, "_submit", submit)
    return seen


async def publish_v2(ctx: ServerContext, kit: dict[str, Any], name: str = "v2", **over: Any) -> str:
    """A layout 2 dashboard from the trend model's preset, published by ``ana`` and granted
    to ``cleo`` and ``carl``. ``over`` replaces top-level fields of the published spec (in
    the revision row: the inputs need no fitted model of their own)."""
    ana: Caller = kit["ana"]
    spec = await preset_spec(kit)
    r = await ana.post("/v1/dashboards", json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    dash_id = str(r.json()["id"])
    r = await ana.post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 200, r.text
    for who in ("cleo", "carl"):
        r = await ana.put(f"/v1/dashboards/{dash_id}/grants/{kit['users'][who].id}")
        assert r.status_code == 200, r.text
    if over:
        async with ctx.db.session() as s:
            dash = await s.get(m.Dashboard, dash_id)
            assert dash is not None and dash.published_revision_id is not None
            rev = await s.get(m.DashboardRevision, dash.published_revision_id)
            assert rev is not None
            new = {**rev.spec, **over}
            assert isinstance(parse_dashboard_spec(new), DashboardSpecV2)
            rev.spec = new
    return dash_id


async def score_runs(ctx: ServerContext) -> list[m.Run]:
    async with ctx.db.session() as s:
        return list(
            (
                await s.execute(select(m.Run).where(m.Run.kind == "score").order_by(m.Run.id))
            ).scalars()
        )


async def readout_row(ctx: ServerContext, dash_id: str, seq: int) -> m.DashboardReadout:
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (dash_id, seq))
        assert row is not None
        return row


async def finish(ctx: ServerContext, dash_id: str, seq: int, snapshot: dict[str, Any]) -> None:
    """The readout's run finished and its snapshot was built."""
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == seq)
            .values(status="ready", ready_at=now(), snapshot=snapshot)
        )


def batch_file(
    ids: list[Any], prices: list[Any], promos: list[Any] | None = None
) -> dict[str, Any]:
    data_vars: dict[str, Any] = {"price": {"dims": ["store"], "data": prices}}
    data_vars["promo"] = {"dims": ["store"], "data": promos or [False] * len(ids)}
    return {"inputs": {"coords": {"store": {"dims": ["store"], "data": ids}},
                       "data_vars": data_vars}}  # fmt: skip


# ================================================================================ pure parts


def test_rows_of_cases_take_new_levels_and_the_defaults() -> None:
    defs = ScenarioRowsDef.model_validate(CASE_ROWS)
    rctx = row_context(defs, CASES_SNAPSHOT)
    assert rctx.dims == ["store"] and rctx.row_dim == "store"
    ds = rows_payload([{"price": 12.5}, {"promo": True}], defs, rctx)
    assert ds.coords["store"].data == ["case1", "case2"]
    assert ds.data_vars["price"].data == [12.5, 20.0]
    assert ds.data_vars["promo"].data == [False, True]


def test_outcome_ranges_are_predictive_for_draws_only() -> None:
    spec = DashboardSpecV2.model_validate({**_spec_example(), "preset": "generic"})
    assert score_output(spec, "draws").kind == "predictive"
    for rep in ("point", "quantiles", "interval"):
        assert score_output(spec, rep).kind == "expected", rep
    cls = spec.model_copy(update={"preset": "classifier"})
    assert score_output(cls, "draws").kind == "expected"


def _spec_example() -> dict[str, Any]:
    from mapi_spec.dashboard_v2 import _SPEC_V2_EXAMPLE

    return dict(_SPEC_V2_EXAMPLE)


def test_a_table_becomes_rows_and_the_model_layout() -> None:
    ds = XrJsonDataset.model_validate(batch_file(["a", "b"], [11.0, 12.0])["inputs"])
    rows = table_rows(ds, "store")
    assert rows == [
        {"store": "a", "price": 11.0, "promo": False},
        {"store": "b", "price": 12.0, "promo": False},
    ]
    spec = DashboardSpecV2.model_validate({**_spec_example(), "preset": "generic", "batch": BATCH})
    shape = cases.batch_columns(spec, CASES_SNAPSHOT, {})
    assert set(shape.variables) == {"price", "promo"} and shape.dim_columns == []
    payload = table_payload(rows, shape, CASES_SNAPSHOT)
    assert payload.coords["store"].data == ["a", "b"]
    assert payload.data_vars["price"].data == [11.0, 12.0]
    # repeated and empty ids are refused with pointers to the cells
    bad = XrJsonDataset.model_validate(batch_file(["a", "a", ""], [1.0, 2.0, 3.0])["inputs"])
    with pytest.raises(MapiError) as err:
        table_rows(bad, "store")
    pointers = [i.pointer for i in err.value.issues or []]
    assert pointers == ["/inputs/coords/store/data/1", "/inputs/coords/store/data/2"]


def test_the_cell_estimate_counts_every_output_dim() -> None:
    from mapi_spec.dashboard_v2 import CatalogOutput, SourceCatalog

    cat = SourceCatalog(
        outputs=[
            CatalogOutput(name="y_hat", manifest_name="y_hat", dims=["store_id", "week"],
                          shape="time", representation="draws"),
            CatalogOutput(name="p", manifest_name="p", dims=["store_id", "class"],
                          shape="classification", representation="draws",
                          classes=["a", "b", "c"]),
        ]
    )  # fmt: skip
    spec = DashboardSpecV2.model_validate({**_spec_example(), "preset": "generic"})
    n = batch_cells(spec, cat, 100, per_row={"store"}, sizes={"week": 10},
                    dims={"store": "store_id"})  # fmt: skip
    assert n == 100 * 10 * (2 + 3) + 100 * 3 * (2 + 3)
    assert BATCH_CELL_BUDGET == 60_000


# ============================================================================ scenario rows


async def test_a_forecast_row_scores_its_horizon_as_the_publisher(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await publish_v2(ctx, kit, scenario_rows=HORIZON_ROWS)
    url = f"/v1/dashboards/{dash_id}/predictions"
    r = await kit["cleo"].post(url, json={"rows": [{"horizon": 3}]})
    assert r.status_code == 202, r.text
    head = r.json()
    assert head["kind"] == "prediction" and head["visibility"] == "private"
    assert head["trigger"] == "scenario_rows" and "snapshot" not in head
    runs = await score_runs(ctx)
    run = runs[-1]
    assert run.created_by == kit["users"]["ana"].id  # the publisher, never the client
    req = run.payload["request"]
    assert req["mode"] == "forecast" and req["horizon"] == {"dim": "t", "periods": 3}
    assert req["output"]["kind"] == "predictive"  # D9: one new result (draws)
    assert run.labels["mapi.trigger"] == "scenario_rows"
    row = await readout_row(ctx, dash_id, head["seq"])
    assert row.input_ref is not None and row.input_ref["rows"] == [{"horizon": 3}]
    assert row.cache_key and row.expires_at is not None
    # out of range: refused with a pointer, never clipped
    r = await kit["cleo"].post(url, json={"rows": [{"horizon": 13}]})
    assert r.status_code == 422 and r.json()["code"] == "DASHBOARD_INPUT_INVALID", r.text
    assert r.json()["errors"][0]["pointer"] == "/rows/0/horizon"
    # a forecast scores one case at a time
    r = await kit["cleo"].post(url, json={"rows": [{"horizon": 2}, {"horizon": 4}]})
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/rows"


async def test_scenario_rows_are_checked_against_the_revision(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = cases_contract(monkeypatch)
    dash_id = await publish_v2(ctx, kit, preset="generic", scenario_rows=CASE_ROWS)
    url = f"/v1/dashboards/{dash_id}/predictions"
    cleo: Caller = kit["cleo"]
    for row, pointer in (
        ({"price": 31.0}, "/rows/0/price"),  # above max
        ({"price": 12.3}, "/rows/0/price"),  # off the step grid
        ({"price": "cheap"}, "/rows/0/price"),  # wrong type
        ({"promo": 1}, "/rows/0/promo"),  # a toggle takes true or false
        ({"colour": "red"}, "/rows/0/colour"),  # not an input
    ):
        r = await cleo.post(url, json={"rows": [{"price": 20.0}, row]})
        assert r.status_code == 422, r.text
        body = r.json()
        assert body["code"] == "DASHBOARD_INPUT_INVALID", body
        assert body["errors"][0]["pointer"] == pointer.replace("/0/", "/1/"), body
    # more rows than the request takes (20) or the defs allow (5)
    r = await cleo.post(url, json={"rows": [{}] * 21})
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    r = await cleo.post(url, json={"rows": [{"price": 10.0 + k} for k in range(6)]})
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/rows"
    assert seen == []
    # rows in range: a predict on new cases, scored as the publisher
    r = await cleo.post(url, json={"rows": [{"price": 12.5}, {"price": 25.0, "promo": True}]})
    assert r.status_code == 202, r.text
    (run,) = seen
    assert run.owner == kit["users"]["ana"].id
    assert run.request["mode"] == "predict" and "inline.data" in run.inputs
    assert run.request["output"]["kind"] == "predictive"
    row = await readout_row(ctx, dash_id, r.json()["seq"])
    assert row.input_ref is not None
    assert row.input_ref["row_dim"] == "store"
    assert row.input_ref["row_labels"] == ["case1", "case2"]
    assert row.input_ref["rows"] == [
        {"price": 12.5, "promo": False}, {"price": 25.0, "promo": True},
    ]  # fmt: skip


async def test_scenario_rows_need_the_input_turned_on(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await publish_v2(ctx, kit)
    r = await kit["cleo"].post(f"/v1/dashboards/{dash_id}/predictions", json={"rows": [{}]})
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED", r.text
    for path in ("batch/template",):
        r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}/{path}")
        assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED"


async def test_a_repeated_case_answers_from_the_cache_in_a_private_copy(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await publish_v2(ctx, kit, scenario_rows=HORIZON_ROWS)
    base = f"/v1/dashboards/{dash_id}"
    body = {"rows": [{"horizon": 4}]}
    first = (await kit["cleo"].post(f"{base}/predictions", json=body)).json()
    await finish(ctx, dash_id, first["seq"], {"lead": "Cached."})
    mine = await kit["cleo"].post(f"{base}/predictions", json=body)
    assert mine.status_code == 200 and mine.json()["seq"] == first["seq"]
    theirs = await kit["carl"].post(f"{base}/predictions", json=body)
    assert theirs.status_code == 200, theirs.text
    copy = theirs.json()
    assert copy["seq"] != first["seq"] and copy["snapshot"]["lead"] == "Cached."
    assert len(await score_runs(ctx)) == 1
    # private: nobody else's readout, and never in the published history
    assert (await kit["cleo"].get(f"{base}/readouts/{copy['seq']}")).status_code == 404
    assert (await kit["carl"].get(f"{base}/readouts/{first['seq']}")).status_code == 404
    assert (await kit["vic"].get(f"{base}/readouts/{first['seq']}")).status_code == 200
    r = await kit["eve"].post(f"{base}/readouts/{first['seq']}:publish")
    assert r.status_code == 409


async def test_scenario_rows_count_against_the_run_caps(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await publish_v2(
        ctx, kit, scenario_rows=HORIZON_ROWS,
        limits={"exact_runs_per_day": 4, "batch_runs_per_day": 3},
    )  # fmt: skip
    url = f"/v1/dashboards/{dash_id}/predictions"
    for h in (1, 2, 3):
        r = await kit["cleo"].post(url, json={"rows": [{"horizon": h}]})
        assert r.status_code == 202, r.text
    r = await kit["cleo"].post(url, json={"rows": [{"horizon": 4}]})  # the client's hourly cap
    assert r.status_code == 429 and r.json()["code"] == "DASHBOARD_RUN_LIMIT", r.text
    assert int(r.headers["retry-after"]) >= 1
    r = await kit["carl"].post(url, json={"rows": [{"horizon": 5}]})
    assert r.status_code == 202
    r = await kit["carl"].post(url, json={"rows": [{"horizon": 6}]})  # the daily cap (4)
    assert r.status_code == 429
    assert len(await score_runs(ctx)) == 4


async def test_a_version_that_ran_uploaded_code_is_refused_on_every_new_input(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = cases_contract(monkeypatch)
    dash_id = await publish_v2(
        ctx, kit, preset="generic", scenario_rows=CASE_ROWS, batch=BATCH,
        actuals={"enabled": True, "match": "row", "id_column": "store"},
    )  # fmt: skip
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, kit["model"]["mv_id"])
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlay_hash": "sha256:" + "1" * 64}
    base = f"/v1/dashboards/{dash_id}"
    cleo: Caller = kit["cleo"]
    answers = [
        await cleo.post(f"{base}/predictions", json={"rows": [{}]}),
        await cleo.get(f"{base}/batch/template"),
        await cleo.post(f"{base}/batch", json=batch_file(["a"], [12.0])),
        await cleo.get(f"{base}/actuals/template"),
        await cleo.post(f"{base}/actuals", json=batch_file(["a"], [12.0])),
    ]
    for r in answers:
        assert r.json()["code"] == "CAPABILITY_UNSUPPORTED", r.text
    assert seen == [] and await score_runs(ctx) == []


async def test_a_paused_dashboard_refuses_scenario_rows(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await publish_v2(ctx, kit, scenario_rows=HORIZON_ROWS)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == kit["users"]["ana"].id)
            .values(role="editor")
        )
    r = await kit["cleo"].post(
        f"/v1/dashboards/{dash_id}/predictions", json={"rows": [{"horizon": 2}]}
    )
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_LIVE_PAUSED", r.text
    assert await score_runs(ctx) == []


# ===================================================================================== batch


async def test_the_batch_template_names_the_id_and_the_drivers(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_contract(monkeypatch)
    dash_id = await publish_v2(ctx, kit, preset="generic", scenario_rows=CASE_ROWS, batch=BATCH)
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}/batch/template")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id_column"] == "store" and body["max_rows"] == 50
    cols = {c["name"]: c for c in body["columns"]}
    assert cols["store"]["role"] == "id"
    assert cols["price"]["type"] == "number" and cols["price"]["min"] == 10.0
    assert cols["promo"]["type"] == "toggle"
    lines = body["example_csv"].splitlines()
    assert lines[0] == "store,price,promo" and lines[1] == "case1,20,false"


async def test_a_batch_is_checked_then_scored_as_the_publisher(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = cases_contract(monkeypatch)
    dash_id = await publish_v2(ctx, kit, preset="generic", scenario_rows=CASE_ROWS, batch=BATCH)
    url = f"/v1/dashboards/{dash_id}/batch"
    cleo: Caller = kit["cleo"]
    # ranges are enforced cell by cell, with pointers
    r = await cleo.post(url, json=batch_file(["a", "b"], [12.0, 99.0]))
    assert r.status_code == 422 and r.json()["code"] == "DASHBOARD_INPUT_INVALID", r.text
    assert r.json()["errors"][0]["pointer"] == "/inputs/data_vars/price/data/1"
    # unknown and missing columns
    bad = batch_file(["a"], [12.0])
    bad["inputs"]["data_vars"]["colour"] = {"dims": ["store"], "data": ["red"]}
    del bad["inputs"]["data_vars"]["promo"]
    r = await cleo.post(url, json=bad)
    assert r.status_code == 422, r.text
    assert {e["pointer"] for e in r.json()["errors"]} == {
        "/inputs/data_vars/colour", "/inputs/data_vars/promo",
    }  # fmt: skip
    # more rows than the batch takes: refused before a run
    ids = [f"c{k}" for k in range(51)]
    r = await cleo.post(url, json=batch_file(ids, [12.0] * 51))
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_BATCH_TOO_LARGE", r.text
    assert seen == []
    r = await cleo.post(url, json=batch_file(["a", "b", "c"], [12.0, 20.5, 30.0]))
    assert r.status_code == 202, r.text
    head = r.json()
    assert head["kind"] == "batch" and head["visibility"] == "private"
    (run,) = seen
    assert run.owner == kit["users"]["ana"].id
    assert run.labels["mapi.trigger"] == "batch" and "inline.data" in run.inputs
    row = await readout_row(ctx, dash_id, head["seq"])
    assert row.input_ref is not None
    assert row.input_ref["n_rows"] == 3 and row.input_ref["id_column"] == "store"


async def test_a_batch_whose_summary_would_not_fit_is_refused(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_contract(monkeypatch)
    dash_id = await publish_v2(
        ctx, kit, preset="generic", scenario_rows=CASE_ROWS,
        batch={**BATCH, "max_rows": 5000},
    )  # fmt: skip
    monkeypatch.setattr(cases, "BATCH_CELL_BUDGET", 100)
    ids = [f"c{k}" for k in range(20)]
    r = await kit["cleo"].post(f"/v1/dashboards/{dash_id}/batch", json=batch_file(ids, [12.0] * 20))
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_BATCH_TOO_LARGE", r.text
    assert "fewer rows" in r.json()["detail"]


async def test_batch_scores_have_daily_caps(
    ctx: ServerContext, kit: dict[str, Any], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_contract(monkeypatch)
    dash_id = await publish_v2(
        ctx, kit, preset="generic", scenario_rows=CASE_ROWS,
        batch={**BATCH, "max_rows": 5000}, limits={"exact_runs_per_day": 20,
                                                    "batch_runs_per_day": 3},
    )  # fmt: skip
    url = f"/v1/dashboards/{dash_id}/batch"
    monkeypatch.setattr(ctx.settings, "client_batch_per_day", 2)
    monkeypatch.setattr(ctx.settings, "client_run_rate_per_hour", 50)
    for k in range(2):
        r = await kit["cleo"].post(url, json=batch_file([f"a{k}"], [12.0]))
        assert r.status_code == 202, r.text
    r = await kit["cleo"].post(url, json=batch_file(["a9"], [12.0]))  # cleo's daily batches
    assert r.status_code == 429 and r.json()["code"] == "DASHBOARD_RUN_LIMIT", r.text
    assert "scoring files" in r.json()["detail"] and int(r.headers["retry-after"]) >= 1
    r = await kit["carl"].post(url, json=batch_file(["b0"], [12.0]))
    assert r.status_code == 202, r.text
    r = await kit["carl"].post(url, json=batch_file(["b1"], [12.0]))  # the dashboard's (3)
    assert r.status_code == 429, r.text
    # rows per dashboard and day
    monkeypatch.setattr(ctx.settings, "batch_rows_per_day", 3)
    other = await publish_v2(
        ctx, kit, name="v2b", preset="generic", scenario_rows=CASE_ROWS,
        batch={**BATCH, "max_rows": 5000},
    )  # fmt: skip
    r = await kit["eve"].post(
        f"/v1/dashboards/{other}/batch", json=batch_file(["x", "y", "z", "w"], [12.0] * 4)
    )
    assert r.status_code == 429, r.text


async def test_concurrent_batches_cannot_pass_the_cap_together(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_contract(monkeypatch)
    dash_id = await publish_v2(
        ctx, kit, preset="generic", scenario_rows=CASE_ROWS, batch=BATCH,
        limits={"exact_runs_per_day": 20, "batch_runs_per_day": 2},
    )  # fmt: skip
    url = f"/v1/dashboards/{dash_id}/batch"
    files = [batch_file([f"k{k}"], [12.0]) for k in range(6)]
    answers = await asyncio.gather(*(kit["eve"].post(url, json=f) for f in files))
    codes = sorted(r.status_code for r in answers)
    assert codes.count(202) == 2 and codes.count(429) == 4, [r.text for r in answers]


async def test_images_are_never_batch_scored_by_clients(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cases_contract(monkeypatch)
    note = [{"id": "n", "title": "Note", "kind": "note", "text": "Pictures."}]
    dash_id = await publish_v2(ctx, kit, preset="image", panels=note, batch=BATCH)
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}")
    assert r.json()["inputs"]["batch"] is False
    r = await kit["cleo"].post(f"/v1/dashboards/{dash_id}/batch", json=batch_file(["a"], [12.0]))
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED", r.text


# ================================================================================= baselines


async def test_a_forecast_baseline_is_a_forecast_run(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    from mapi_server.services.dashboards.loop import DashboardLoop

    dash_id = await publish_v2(ctx, kit)
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        rev.spec = {**rev.spec, "refresh": {**rev.spec.get("refresh", {}), "horizon": 4}}
    assert await DashboardLoop(ctx).submit_queued() == 1
    (run,) = await score_runs(ctx)
    req = run.payload["request"]
    assert req["mode"] == "forecast" and req["horizon"] == {"dim": "t", "periods": 4}
    assert req["output"]["kind"] == "predictive" and "data" not in req
    row = await readout_row(ctx, dash_id, 1)
    assert row.kind == "baseline" and row.status == "running" and row.run_ids == [run.id]


async def test_a_reference_row_is_the_regression_baseline(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.dashboards.loop import DashboardLoop

    seen = cases_contract(monkeypatch)
    note = [{"id": "n", "title": "Note", "kind": "note", "text": "Cases."}]
    dash_id = await publish_v2(ctx, kit, preset="regression", panels=note, scenario_rows=CASE_ROWS)
    assert await DashboardLoop(ctx).submit_queued() == 1
    (run,) = seen
    assert run.request["mode"] == "predict" and "inline.data" in run.inputs
    row = await readout_row(ctx, dash_id, 1)
    assert row.input_ref is not None and row.input_ref["row_labels"] == ["case1"]
    assert row.input_ref["rows"] == [{"price": 20.0, "promo": False}]


async def test_dashboards_that_read_fit_metrics_only_spend_no_run(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    from mapi_server.services.dashboards.loop import DashboardLoop

    note = [{"id": "n", "title": "Note", "kind": "note", "text": "Pictures."}]
    dash_id = await publish_v2(ctx, kit, preset="image", panels=note)
    assert await DashboardLoop(ctx).submit_queued() == 1
    assert await score_runs(ctx) == []
    row = await readout_row(ctx, dash_id, 1)
    assert row.status == "materialising" and row.run_ids == []


def test_no_interval_is_built_here() -> None:
    """Cases only pass values on: the summary's bounds are never added up (D6)."""
    import inspect

    src = inspect.getsource(cases)
    assert "low +" not in src and "high +" not in src
    assert math.isfinite(BATCH_CELL_BUDGET)


# =================================================================== start-up (plan §10.1)


async def test_the_worker_upgrades_layout_1_dashboards_once_at_start_up(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.dashboards import loop
    from mapi_server.services.dashboards.upgrade import UpgradeReport

    calls: list[ServerContext] = []

    async def upgrade(c: ServerContext) -> UpgradeReport:
        calls.append(c)
        return UpgradeReport(ran=True)

    monkeypatch.setattr(loop, "run_auto_upgrade", upgrade)
    worker = loop.DashboardLoop(ctx)
    stop = asyncio.Event()
    stop.set()  # one start, no pass
    await worker.run_forever(stop)
    await worker.start_up()
    assert calls == [ctx] and worker.upgraded is not None and worker.upgraded.ran


async def test_the_start_up_upgrade_can_be_turned_off(
    ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mapi_server.services.dashboards.loop import DashboardLoop

    assert ctx.settings.dashboard_auto_upgrade is True  # MAPI_DASHBOARD_AUTO_UPGRADE
    monkeypatch.setattr(ctx.settings, "dashboard_auto_upgrade", False)
    report = await DashboardLoop(ctx).start_up()
    assert report.ran is False
