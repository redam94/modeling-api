"""Actuals of layout 2 dashboards (v0.17, WP8, plan §3.3): matched by time (the v0.16
path), by row (one row per case; the outcome stripped from what the model scores) and the
updated effect of a causal impact model (a forecast without the outcome).

The helpers and the trend model are those of :mod:`test_dashboard_cases`; rows of cases use
its ``store``/``price``/``promo``/``y`` input contract and record the submissions.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.services.dashboards import actuals_v2, sources
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller, grant_role, make_user, session_caller
from mapi_spec.dashboard_metrics import effect_of
from mapi_spec.dashboard_v2 import MAX_ROW_ACTUALS
from mapi_spec.views import DistributionSummary
from test_dashboard_cases import (
    CASE_ROWS,
    cases_contract,
    publish_v2,
    readout_row,
    score_runs,
)
from test_dashboards_crud import dashboard_kit

pytestmark = pytest.mark.integration

NOTE: list[dict[str, Any]] = [{"id": "n", "title": "Note", "kind": "note", "text": "Results."}]
ROW_ACTUALS: dict[str, Any] = {"enabled": True, "match": "row", "id_column": "store"}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    carl = await make_user(ctx, "carl@default.test")
    await grant_role(ctx, carl, "default", "default", "client")
    kit["users"]["carl"] = carl
    kit["carl"] = await session_caller(ctx, client, carl, tenant_id="default", project_id="default")
    return kit


def months(start: int, n: int, year: int = 2024) -> list[str]:
    return [f"{year}-{start + k:02d}-01" for k in range(n)]


def time_upload(ys: list[Any], labels: list[str]) -> dict[str, Any]:
    return {
        "inputs": {
            "coords": {"t": {"dims": ["t"], "data": labels}},
            "data_vars": {"y": {"dims": ["t"], "data": ys}},
        }
    }


def row_upload(ids: list[Any], ys: list[Any], *, prices: list[Any] | None = None) -> dict[str, Any]:
    n = len(ids)
    return {
        "inputs": {
            "coords": {"store": {"dims": ["store"], "data": ids}},
            "data_vars": {
                "price": {"dims": ["store"], "data": prices or [12.0] * n},
                "promo": {"dims": ["store"], "data": [False] * n},
                "y": {"dims": ["store"], "data": ys},
            },
        }
    }


# ======================================================================================= time


async def test_time_actuals_take_the_v016_path_on_layout_2(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await publish_v2(ctx, kit, actuals={"enabled": True})
    base = f"/v1/dashboards/{dash_id}"
    r = await kit["cleo"].get(f"{base}/actuals/template")
    assert r.status_code == 200, r.text
    assert r.json()["kpi_column"] == "y" and r.json()["match"] == "time"
    # gaps and a missing outcome column are refused with pointers before a run
    gap = time_upload([1.0, 2.0], ["2024-01-01", "2024-03-01"])
    r = await kit["cleo"].post(f"{base}/actuals", json=gap)
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_INVALID", r.text
    assert any(e["pointer"] == "/inputs/coords/t" for e in r.json()["errors"])
    assert await score_runs(ctx) == []
    r = await kit["cleo"].post(f"{base}/actuals", json=time_upload([410.0, 412.5], months(1, 2)))
    assert r.status_code == 202, r.text
    head = r.json()
    assert head["kind"] == "actuals" and head["visibility"] == "pending_review"
    (run,) = await score_runs(ctx)
    assert run.created_by == kit["users"]["ana"].id
    assert run.payload["request"]["mode"] == "predict"
    assert run.payload["request"]["output"]["kind"] == "predictive"
    row = await readout_row(ctx, dash_id, head["seq"])
    assert row.input_ref is not None
    assert row.input_ref["kpi_column"] == "y" and row.input_ref["match"] == "time"
    assert row.input_ref["bucket"] and row.cache_key
    # the other client sees it once an editor published it
    assert (await kit["carl"].get(f"{base}/readouts/{head['seq']}")).status_code == 404
    r = await kit["eve"].post(f"{base}/readouts/{head['seq']}:publish")
    assert r.status_code == 200


async def test_causal_impact_actuals_forecast_without_the_outcome(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = await publish_v2(
        ctx, kit, preset="causal_impact", panels=NOTE, actuals={"enabled": True,
                                                                 "publish": "auto"},
    )  # fmt: skip
    url = f"/v1/dashboards/{dash_id}/actuals"
    # a forecast starts right after the fit (December 2023)
    r = await kit["cleo"].post(url, json=time_upload([5.0, 6.0], months(2, 2)))
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_INVALID", r.text
    assert r.json()["errors"][0]["pointer"] == "/inputs/coords/t/data/0"
    r = await kit["cleo"].post(url, json=time_upload([5.0, 6.0, 7.0], months(1, 3)))
    assert r.status_code == 202, r.text
    assert r.json()["visibility"] == "published"
    (run,) = await score_runs(ctx)
    req = run.payload["request"]
    assert req["mode"] == "forecast" and req["horizon"] == {"dim": "t", "periods": 3}
    assert "inline.data" not in run.inputs  # the outcome is never scored
    row = await readout_row(ctx, dash_id, r.json()["seq"])
    assert row.input_ref is not None and row.input_ref["bucket"]  # the whole upload is kept
    raw = await ctx.store.get(
        row.input_ref["bucket"], row.input_ref["name"], generation=row.input_ref["generation"]
    )
    assert json.loads(raw)["data_vars"]["y"]["data"] == [5.0, 6.0, 7.0]


def test_the_effect_range_is_the_actuals_minus_the_forecast_range() -> None:
    """``[Σa − high, Σa − low]``: the actuals are constants, the forecast is not."""
    cf = DistributionSummary.model_validate(
        {"mean": 80.0, "sd": 6.0, "quantiles": {"0.05": 70.0, "0.5": 80.0, "0.95": 90.0}}
    )
    got = effect_of(100.0, cf, n_periods=3)
    assert (got.total, got.low, got.high) == (20.0, 10.0, 30.0)


# ======================================================================================== row


async def test_row_actuals_strip_the_outcome_from_the_scored_cases(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = cases_contract(monkeypatch)
    dash_id = await publish_v2(
        ctx, kit, preset="generic", scenario_rows=CASE_ROWS, actuals=ROW_ACTUALS
    )
    base = f"/v1/dashboards/{dash_id}"
    r = await kit["cleo"].get(f"{base}/actuals/template")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["match"] == "row" and body["id_column"] == "store"
    assert body["example_csv"].splitlines()[0] == "store,price,promo,y"
    assert body["max_rows"] == MAX_ROW_ACTUALS
    # B10 (live): the table is the one of the upload (one row per case), not the time one
    table = body["input_table"]
    assert [c["name"] for c in table["columns"]] == ["store", "price", "promo", "y"]
    assert [c["role"] for c in table["columns"]] == ["coord", "input", "input", "target"]
    assert table["rows"] == []
    extra = row_upload(["a"], [3.0])
    extra["inputs"]["data_vars"]["time"] = {"dims": ["store"], "data": ["2024-01-01"]}
    r = await kit["cleo"].post(f"{base}/actuals", json=extra)
    assert r.status_code == 422, r.text
    assert "does not take the column 'time'" in r.json()["errors"][0]["message"]
    r = await kit["cleo"].post(f"{base}/actuals", json=row_upload(["a", "b"], [3.0, 4.5]))
    assert r.status_code == 202, r.text
    (sub,) = seen
    assert sub.request["mode"] == "predict" and sub.owner == kit["users"]["ana"].id
    ref = dict(sub.inputs["inline.data"])
    scored = json.loads(
        await ctx.store.get(ref["bucket"], ref["name"], generation=ref["generation"])
    )
    assert set(scored["data_vars"]) == {"price", "promo"}  # the outcome is stripped
    assert scored["coords"]["store"]["data"] == ["a", "b"]
    row = await readout_row(ctx, dash_id, r.json()["seq"])
    assert row.input_ref is not None
    assert row.input_ref["match"] == "row" and row.input_ref["kpi_column"] == "y"
    kept = json.loads(
        await ctx.store.get(
            row.input_ref["bucket"], row.input_ref["name"], generation=row.input_ref["generation"]
        )
    )
    assert kept["data_vars"]["y"]["data"] == [3.0, 4.5]


async def test_row_actuals_are_refused_with_pointers(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = cases_contract(monkeypatch)
    dash_id = await publish_v2(
        ctx, kit, preset="generic", scenario_rows=CASE_ROWS, actuals=ROW_ACTUALS
    )
    url = f"/v1/dashboards/{dash_id}/actuals"
    cleo: Caller = kit["cleo"]

    async def refused(body: dict[str, Any]) -> list[str]:
        r = await cleo.post(url, json=body)
        assert r.status_code == 422 and r.json()["code"] == "ACTUALS_INVALID", r.text
        return [e["pointer"] for e in r.json()["errors"]]

    assert await refused(row_upload(["a", "a"], [1.0, 2.0])) == ["/inputs/coords/store/data/1"]
    no_y = row_upload(["a"], [1.0])
    del no_y["inputs"]["data_vars"]["y"]
    assert await refused(no_y) == ["/inputs/data_vars/y"]
    assert await refused(row_upload(["a", "b"], [1.0, "lots"])) == ["/inputs/data_vars/y/data/1"]
    ids = [f"c{k}" for k in range(MAX_ROW_ACTUALS + 1)]
    assert await refused(row_upload(ids, [1.0] * len(ids))) == ["/inputs/coords/store"]
    assert seen == []


async def test_class_labels_must_be_classes_of_the_model(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = cases_contract(monkeypatch)
    dash_id = await publish_v2(
        ctx, kit, preset="classifier", panels=NOTE, scenario_rows=CASE_ROWS,
        actuals=ROW_ACTUALS,
    )  # fmt: skip
    url = f"/v1/dashboards/{dash_id}/actuals"
    # a yes/no model (no class dim) takes two labels
    r = await kit["cleo"].post(url, json=row_upload(["a", "b", "c"], ["yes", "no", "maybe"]))
    assert r.status_code == 422 and r.json()["code"] == "ACTUALS_INVALID", r.text
    # with classes, labels are matched as normalise_label does
    monkeypatch.setattr(actuals_v2, "_classes", lambda p, catalog: ["high", "low"])
    r = await kit["cleo"].post(url, json=row_upload(["a", "b"], ["HIGH ", "mid"]))
    assert r.status_code == 422, r.text
    assert r.json()["errors"][0]["pointer"] == "/inputs/data_vars/y/data/1"
    r = await kit["cleo"].post(url, json=row_upload(["a", "b"], ["HIGH ", "low"]))
    assert r.status_code == 202, r.text
    assert len(seen) == 1


async def test_an_upload_scored_as_sent_keeps_its_stored_generation(ctx: ServerContext) -> None:
    """B2 (live): the upload and the score call's identical inline data share one
    content-addressed name. The second write must reuse the stored generation, or the
    readout's pinned copy of the upload 404s ("the uploaded numbers can no longer be read")."""
    from mapi_server.services.endpoints import store_inline_inputs
    from mapi_server.services.runs import Scope
    from mapi_spec.endpoints import EndpointScoreCall
    from mapi_spec.xrjson import XrJsonDataset

    payload = XrJsonDataset.model_validate(time_upload([410.0, 412.5], months(1, 2))["inputs"])
    scope = Scope("default", "default")
    first = await store_inline_inputs(ctx, scope, actuals_v2._upload_call(payload))
    again = await store_inline_inputs(ctx, scope, EndpointScoreCall(mode="predict", data=payload))
    (a,) = first.values()
    (b,) = again.values()
    assert a == b
    raw = await ctx.store.get(a["bucket"], a["name"], generation=int(a["generation"]))
    assert json.loads(raw)["data_vars"]["y"]["data"] == [410.0, 412.5]


def test_multiclass_outcomes_are_not_held_to_two_labels() -> None:
    """B4 (live): a model with a chance per class along a ``class`` dim takes its labels
    from the score's coordinates (the catalog lists none), so three labels are fine."""
    from mapi_spec.errors import MapiError

    labels = ["low", "mid", "high", "low"]
    actuals_v2._check_outcomes(
        labels, "tier", classifier=True, classes=[], positive=None, multiclass=True
    )
    with pytest.raises(MapiError, match="two outcome labels"):
        actuals_v2._check_outcomes(labels, "tier", classifier=True, classes=[], positive=None)


def test_the_subject_of_a_class_dim_output_is_multiclass() -> None:
    from types import SimpleNamespace

    from mapi_spec.capabilities import Representation
    from mapi_spec.dashboard_presets import catalog_output

    point = Representation.POINT
    multi = catalog_output("y_hat", ["class", "store"], shape="classification",
                           representation=point, meaning="class_probability")  # fmt: skip
    binary = catalog_output("y_hat", ["store"], shape="categorical",
                            representation=point, meaning="class_probability")  # fmt: skip
    p: Any = SimpleNamespace(spec=SimpleNamespace(subject=SimpleNamespace(variable="y_hat")))
    assert actuals_v2._multiclass(p, SimpleNamespace(outputs=[multi]))
    assert not actuals_v2._multiclass(p, SimpleNamespace(outputs=[binary]))
