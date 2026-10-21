"""Layout 2 dashboards (v0.17, WP6): drafts from a preset, validation, publishing, the
client view, the editor's ``:upgrade`` of a layout 1 draft and the inputs a caller may use.

The model is the trend model of the v0.16 tests (``ts.hsgp_trend``): its preset is
``forecast``. Layout 1 dashboards still come from :func:`create_published`: this module opts
in to layout 1 creation (``dashboard_layout1_create``, off by default from v0.17).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import crud, sources
from mapi_server.services.dashboards.access import effective_inputs
from mapi_server.settings import Settings
from mapi_server_testkit.identity import Caller
from mapi_spec.dashboard_v2 import UPGRADE_NOTE, DashboardSpecV2, parse_dashboard_spec
from mapi_spec.dashboards import DashboardSpec, dashboard_spec_hash
from sqlalchemy import select
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update=LAYOUT1)  # layout 1 dashboards are created here too


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    sources.clear_catalog_cache()
    return await dashboard_kit(ctx, client)


async def preset_spec(kit: dict[str, Any]) -> dict[str, Any]:
    r = await kit["eve"].get(f"/v1/dashboards:presets?model_id={kit['model']['model_id']}")
    assert r.status_code == 200, r.text
    spec: dict[str, Any] = r.json()["spec"]
    return spec


async def create_v2(kit: dict[str, Any], name: str = "fc", **over: Any) -> tuple[str, str]:
    spec = {**await preset_spec(kit), **over}
    r = await kit["ana"].post("/v1/dashboards", json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    return str(r.json()["id"]), str(r.json()["draft_etag"])


# ===================================================================================== drafts


async def test_a_preset_draft_validates_and_publishes_as_layout_2(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    ana: Caller = kit["ana"]
    dash_id, _ = await create_v2(kit)
    r = await ana.get(f"/v1/dashboards/{dash_id}")
    assert r.status_code == 200 and r.json()["draft_spec"]["layout_version"] == 2
    r = await ana.post(f"/v1/dashboards/{dash_id}:validate")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, r.json()
    r = await ana.post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 200, r.text
    rev = r.json()["published_revision"]
    assert rev["spec"]["layout_version"] == 2 and rev["spec"]["preset"] == "forecast"
    parsed = parse_dashboard_spec(rev["spec"])
    assert isinstance(parsed, DashboardSpecV2)
    assert rev["spec_hash"] == dashboard_spec_hash(parsed)
    async with ctx.db.session() as s:
        readouts = list(
            (
                await s.execute(
                    select(m.DashboardReadout).where(m.DashboardReadout.dashboard_id == dash_id)
                )
            ).scalars()
        )
    assert [(x.kind, x.status, x.trigger) for x in readouts] == [("baseline", "queued", "publish")]
    # the list shows layout 2 drafts too
    items = (await ana.get("/v1/dashboards")).json()["items"]
    assert dash_id in {i["id"] for i in items}


async def test_clients_see_the_layout_2_view(kit: dict[str, Any]) -> None:
    ana: Caller = kit["ana"]
    dash_id, _ = await create_v2(kit)
    assert (await ana.post(f"/v1/dashboards/{dash_id}:publish")).status_code == 200
    r = await ana.put(f"/v1/dashboards/{dash_id}/grants/{kit['users']['cleo'].id}")
    assert r.status_code == 200
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["layout_version"] == 2 and body["preset"] == "forecast"
    spec = (await ana.get(f"/v1/dashboards/{dash_id}")).json()["draft_spec"]
    assert [p["id"] for p in body["panels_meta"]] == [p["id"] for p in spec["panels"]]
    assert body["subject"]["variable"] == spec["subject"]["variable"]
    assert body["kpi"]["variable"] == spec["subject"]["variable"]
    assert body["channels"] == [] and body["sections"] == ["overview", "history"]
    assert body["scenario_form"] is None and body["batch_form"] is None
    assert body["upgrade_note"] is None  # never layout 1
    assert "draft_spec" not in body and "run_as" not in body


async def test_layout_1_creation_is_retired_by_default(
    ctx: ServerContext, kit: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    eve: Caller = kit["eve"]
    dash_id, etag = await create_v2(kit)
    assert Settings.model_fields["dashboard_layout1_create"].default is False
    monkeypatch.setattr(ctx.settings, "dashboard_layout1_create", False)
    assert not crud.layout1_create_allowed(ctx)
    r = await eve.post("/v1/dashboards", json={"name": "old", "spec": dashboard_spec()})
    assert r.status_code == 422 and r.json()["code"] == "DASHBOARD_LAYOUT_RETIRED"
    r = await eve.put(
        f"/v1/dashboards/{dash_id}/draft",
        json={"spec": dashboard_spec()},
        headers={"If-Match": etag},
    )
    assert r.status_code == 422 and r.json()["code"] == "DASHBOARD_LAYOUT_RETIRED"
    # a layout 2 draft still saves
    spec = await preset_spec(kit)
    r = await eve.put(
        f"/v1/dashboards/{dash_id}/draft",
        json={"spec": {**spec, "title": "New title"}},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200 and r.json()["draft_spec"]["title"] == "New title"


# ==================================================================================== upgrade


async def test_upgrade_converts_a_layout_1_draft_under_its_etag(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    eve: Caller = kit["eve"]
    r = await eve.post("/v1/dashboards", json={"name": "old", "spec": dashboard_spec()})
    dash_id, etag = r.json()["id"], r.json()["draft_etag"]
    r = await eve.post(f"/v1/dashboards/{dash_id}:upgrade")
    assert r.status_code == 412 and r.json()["code"] == "PRECONDITION_FAILED"
    r = await eve.post(f"/v1/dashboards/{dash_id}:upgrade", headers={"If-Match": '"99"'})
    assert r.status_code == 412 and r.json()["code"] == "DASHBOARD_SPEC_STALE"
    assert (await kit["vic"].post(f"/v1/dashboards/{dash_id}:upgrade")).status_code in (403, 404)
    r = await eve.post(f"/v1/dashboards/{dash_id}:upgrade", headers={"If-Match": etag})
    assert r.status_code == 200, r.text
    body = r.json()
    draft = body["dashboard"]["draft_spec"]
    assert draft["layout_version"] == 2 and draft["preset"] == "forecast"
    assert body["dashboard"]["draft_etag"] != etag and r.headers["etag"] != etag
    assert any("placeholder" in n for n in body["notes"])
    assert "mmm" not in draft or draft["mmm"] is None  # the placeholder channels are gone
    # the KPI is now read from the model output
    assert draft["subject"]["variable"] == "y_hat"
    # again: the draft already is layout 2
    r = await eve.post(
        f"/v1/dashboards/{dash_id}:upgrade", headers={"If-Match": body["dashboard"]["draft_etag"]}
    )
    assert r.status_code == 409
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "dashboard.upgraded",
                        m.AuditEvent.resource_id == dash_id,
                    )
                )
            ).scalars()
        )
    assert len(rows) == 1 and rows[0].outcome == "success" and rows[0].details["draft"] is True


async def test_clients_get_the_upgrade_note_once_the_upgrade_is_published(
    kit: dict[str, Any],
) -> None:
    ana: Caller = kit["ana"]
    dash_id = await create_published(kit)
    r = await kit["cleo"].get(f"/v1/dashboards/{dash_id}")
    assert r.json()["layout_version"] == 1 and r.json().get("upgrade_note") is None
    etag = (await ana.get(f"/v1/dashboards/{dash_id}")).json()["draft_etag"]
    r = await ana.post(f"/v1/dashboards/{dash_id}:upgrade", headers={"If-Match": etag})
    assert r.status_code == 200, r.text
    r = await ana.post(f"/v1/dashboards/{dash_id}:publish")
    assert r.status_code == 200, r.text
    assert r.json()["published_revision"]["seq"] == 2
    body = (await kit["cleo"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["layout_version"] == 2
    assert body["upgrade_note"].startswith(UPGRADE_NOTE)
    # the layout 1 revision stays as it was
    revs = (await ana.get(f"/v1/dashboards/{dash_id}/revisions")).json()["items"]
    assert [r["spec"].get("layout_version", 1) for r in revs] == [2, 1]


# ===================================================================================== inputs


def _v2(**over: Any) -> DashboardSpecV2:
    base: dict[str, Any] = {
        "layout_version": 2,
        "preset": "regression",
        "title": "Sales",
        "source": {"kind": "model_version", "model_id": "m"},
        "subject": {"variable": "y_hat", "label": "Sales", "unit": "count"},
        "panels": [
            {"id": "next", "kind": "kpi", "title": "Sales",
             "source": {"kind": "output", "variable": "y_hat"}},
        ],
        "scenario_rows": {
            "enabled": True, "outputs": ["y_hat"],
            "inputs": [{"name": "price", "label": "Price", "bind": {"var": "price"},
                        "type": "number", "min": 1, "max": 9, "step": 1, "default": 5}],
        },
        "actuals": {"enabled": True, "match": "row", "id_column": "case"},
        "batch": {"enabled": True, "outputs": ["y_hat"], "id_column": "case"},
    }  # fmt: skip
    base.update(over)
    return DashboardSpecV2.model_validate(base)


def test_effective_inputs_of_layout_2() -> None:
    spec = _v2()
    every = effective_inputs(spec, None)
    assert every.scenario_rows and every.batch and every.actuals
    assert every.accuracy_kind == "error" and not every.whatif
    narrow = effective_inputs(spec, None, ["scenario_rows"])
    assert narrow.scenario_rows and not narrow.batch and not narrow.actuals
    assert narrow.accuracy_kind is None
    assert not effective_inputs(spec, None, []).scenario_rows
    cls = effective_inputs(_v2(preset="classifier"), "compared")
    assert cls.accuracy_kind == "classification" and cls.actuals_mode == "compared"


def test_image_batch_is_for_members_only() -> None:
    spec = _v2(
        preset="image",
        panels=[{"id": "acc", "kind": "kpi", "title": "Accuracy",
                 "source": {"kind": "metric", "key": "accuracy"}}],
        scenario_rows={},
        actuals={},
    )  # fmt: skip
    assert effective_inputs(spec, None).batch
    assert not effective_inputs(spec, None, client=True).batch
    assert not effective_inputs(spec, None, ["batch"]).batch


def test_layout_1_inputs_are_unchanged() -> None:
    spec = DashboardSpec.model_validate(
        dashboard_spec(whatif={"enabled": True, "exact": True}, actuals={"enabled": True})
    )
    got = effective_inputs(spec, "conditioned", ["whatif", "scenario_rows"])
    assert got.whatif and got.exact and not got.actuals
    assert not got.scenario_rows and not got.batch and got.accuracy_kind is None
