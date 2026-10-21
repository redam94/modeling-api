"""The predictions download of batch readouts and the pictures of share links (v0.17, WP8,
plan D12, §3.5).

A batch file is scored through the routes (the submissions recorded, as in
:mod:`test_dashboard_cases`); the run's ``summary.json`` is :data:`SUMMARY` here. The file
the caller sent is stored for real and read back for the input columns.
"""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import downloads, readouts, sources
from mapi_server.services.dashboards.downloads import csv_cell
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller, grant_role, make_user, session_caller
from mapi_spec.api import ScoreSummary
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from sqlalchemy import select, update
from test_dashboard_cases import BATCH, CASE_ROWS, batch_file, cases_contract, finish, publish_v2
from test_dashboards_crud import dashboard_kit

pytestmark = pytest.mark.integration

IDS = ["a", "=1+1", "-2"]
SUMMARY = ScoreSummary.model_validate(
    {
        "tables": [
            {
                "variable": "y_hat",
                "columns": ["store", "mean", "hdi_90_lower", "hdi_90_upper"],
                "dims": ["store"],
                "rows": [["a", 10.5, 9.0, 12.0], ["=1+1", -3.5, -5.0, -2.0],
                         ["-2", 1.0, 0.5, 1.5]],
            }
        ]
    }
)  # fmt: skip


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def kit(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    sources.clear_catalog_cache()
    kit = await dashboard_kit(ctx, client)
    carl = await make_user(ctx, "carl@default.test")
    await grant_role(ctx, carl, "default", "default", "client")
    kit["users"]["carl"] = carl
    kit["carl"] = await session_caller(ctx, client, carl, tenant_id="default", project_id="default")
    cases_contract(monkeypatch)
    kit["egress"] = charged = []

    async def summary_of(ctx: Any, session: Any, run_id: str) -> ScoreSummary | None:
        return SUMMARY if kit.get("summary", True) else None

    async def egress(ctx: Any, tenant_id: str, nbytes: int, **kw: Any) -> None:
        charged.append((tenant_id, nbytes))

    monkeypatch.setattr(readouts, "_summary_of", summary_of)
    monkeypatch.setattr(downloads, "charge_egress", egress)
    kit["dash_id"] = await publish_v2(
        ctx, kit, preset="generic", scenario_rows=CASE_ROWS, batch=BATCH
    )
    return kit


async def scored(
    ctx: ServerContext, caller: Any, url: str, dash_id: str, price: float = 12.0, **headers: str
) -> int:
    """A ready batch readout of ``caller`` (its run finished)."""
    r = await caller.post(url, json=batch_file(IDS, [price, 20.0, 30.0]), headers=headers)
    assert r.status_code == 202, r.text
    seq = int(r.json()["seq"])
    await finish(ctx, dash_id, seq, {"lead": "Scored."})
    return seq


async def audits(ctx: ServerContext) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        return list(
            (
                await s.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "dashboard.batch_download")
                )
            ).scalars()
        )


# ======================================================================================= cells


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        ("=1+1", "'=1+1"), ("+cmd", "'+cmd"), ("@SUM(A1)", "'@SUM(A1)"), ("\tx", "'\tx"),
        ("\rx", "'\rx"), ("-2", "-2"), ("-x", "'-x"), ("plain", "plain"), (-3.5, "-3.5"),
        (None, ""), (True, "true"), (3, "3"),
    ],
)  # fmt: skip
def test_formula_cells_are_escaped(value: object, cell: str) -> None:
    assert csv_cell(value) == cell


# ==================================================================================== members


async def test_the_owner_downloads_the_predictions_as_csv(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    dash_id = kit["dash_id"]
    base = f"/v1/dashboards/{dash_id}"
    seq = await scored(ctx, kit["cleo"], f"{base}/batch", dash_id)
    r = await kit["cleo"].get(f"{base}/readouts/{seq}/predictions.csv")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert f'filename="predictions-{seq}.csv"' in r.headers["content-disposition"]
    assert "no-store" in r.headers["cache-control"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["store", "price", "promo", "y_hat", "y_hat_low", "y_hat_high"]
    assert rows[1] == ["a", "12.0", "false", "10.5", "9.0", "12.0"]
    assert rows[2] == ["'=1+1", "20.0", "false", "-3.5", "-5.0", "-2.0"]  # escaped id
    assert rows[3][0] == "-2"  # a number is never escaped
    assert kit["egress"] == [("default", len(r.content))]
    (event,) = await audits(ctx)
    assert event.resource_id == dash_id and event.details["seq"] == seq
    assert event.details["rows"] == 3 and event.actor_id == kit["users"]["cleo"].id
    # an editor may download it too; a viewer or another client may not
    assert (await kit["eve"].get(f"{base}/readouts/{seq}/predictions.csv")).status_code == 200
    for who in ("carl", "vic"):
        r = await kit[who].get(f"{base}/readouts/{seq}/predictions.csv")
        assert r.status_code == 404, (who, r.text)


async def test_only_ready_batch_readouts_download(
    ctx: ServerContext, kit: dict[str, Any], client: httpx.AsyncClient
) -> None:
    dash_id = kit["dash_id"]
    base = f"/v1/dashboards/{dash_id}"
    r = await kit["cleo"].post(f"{base}/batch", json=batch_file(IDS, [12.0, 20.0, 30.0]))
    running = int(r.json()["seq"])
    assert (await kit["cleo"].get(f"{base}/readouts/{running}/predictions.csv")).status_code == 404
    # the baseline is no batch readout
    assert (await kit["eve"].get(f"{base}/readouts/1/predictions.csv")).status_code == 404
    # nobody signed in
    anon = await client.get(f"{base}/readouts/{running}/predictions.csv")
    assert anon.status_code in (401, 404)
    # B5 (live): a batch whose scoring failed says so (410), never "readout not found"
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == running)
            .values(status="failed")
        )
    r = await kit["cleo"].get(f"{base}/readouts/{running}/predictions.csv")
    assert r.status_code == 410 and r.json()["code"] == "DASHBOARD_DOWNLOAD_GONE", r.text
    assert "failed" in r.json()["detail"]
    assert kit["egress"] == [] and await audits(ctx) == []


async def test_an_expired_download_answers_410(ctx: ServerContext, kit: dict[str, Any]) -> None:
    dash_id = kit["dash_id"]
    base = f"/v1/dashboards/{dash_id}"
    seq = await scored(ctx, kit["cleo"], f"{base}/batch", dash_id)
    kit["summary"] = False  # the run's results were deleted
    r = await kit["cleo"].get(f"{base}/readouts/{seq}/predictions.csv")
    assert r.status_code == 410 and r.json()["code"] == "DASHBOARD_DOWNLOAD_GONE", r.text
    kit["summary"] = True
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardReadout)
            .where(m.DashboardReadout.dashboard_id == dash_id, m.DashboardReadout.seq == seq)
            .values(expires_at=now() - timedelta(minutes=1))
        )
    r = await kit["cleo"].get(f"{base}/readouts/{seq}/predictions.csv")
    assert r.status_code == 410, r.text
    assert kit["egress"] == []


# ================================================================================ share links


async def _link(kit: dict[str, Any], client: httpx.AsyncClient, inputs: list[str]) -> str:
    r = await kit["ana"].post(
        f"/v1/dashboards/{kit['dash_id']}/shares", json={"allowed_inputs": inputs}
    )
    assert r.status_code == 201, r.text
    r = await client.post("/v1/share-sessions", json={"token": r.json()["token"]})
    assert r.status_code == 200, r.text
    return str(r.json()["session"])


async def test_a_share_link_downloads_only_its_own_predictions(
    ctx: ServerContext, kit: dict[str, Any], client: httpx.AsyncClient
) -> None:
    dash_id = kit["dash_id"]
    one = {SHARE_SESSION_HEADER: await _link(kit, client, ["batch"])}
    two = {SHARE_SESSION_HEADER: await _link(kit, client, ["batch"])}
    r = await client.get("/v1/shared/batch/template", headers=one)
    assert r.status_code == 200 and r.json()["id_column"] == "store", r.text
    seq = await scored(ctx, client, "/v1/shared/batch", dash_id, **one)
    r = await client.get(f"/v1/shared/readouts/{seq}/predictions.csv", headers=one)
    assert r.status_code == 200, r.text
    assert r.text.splitlines()[0] == "store,price,promo,y_hat,y_hat_low,y_hat_high"
    (event,) = await audits(ctx)
    assert event.details["share_id"]
    # another link of the same dashboard, a member's readout, no session
    assert (await client.get(f"/v1/shared/readouts/{seq}/predictions.csv",
                             headers=two)).status_code == 404  # fmt: skip
    mine = await scored(ctx, kit["cleo"], f"/v1/dashboards/{dash_id}/batch", dash_id, 13.0)
    r = await client.get(f"/v1/shared/readouts/{mine}/predictions.csv", headers=one)
    assert r.status_code == 404
    r = await client.get(f"/v1/shared/readouts/{seq}/predictions.csv")
    assert r.status_code == 401


async def test_a_link_without_batch_scoring_cannot_score_a_file(
    ctx: ServerContext, kit: dict[str, Any], client: httpx.AsyncClient
) -> None:
    view = {SHARE_SESSION_HEADER: await _link(kit, client, ["scenario_rows"])}
    r = await client.get("/v1/shared/batch/template", headers=view)
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED"
    r = await client.post("/v1/shared/batch", json=batch_file(["a"], [12.0]), headers=view)
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_INPUT_DISABLED"
    r = await client.post("/v1/shared/predictions", json={"rows": [{}]}, headers=view)
    assert r.status_code == 202, r.text
    assert r.json()["kind"] == "prediction" and r.json()["visibility"] == "private"


# ================================================================================== pictures


async def test_a_share_link_reads_the_pictures_of_its_revision_only(
    ctx: ServerContext, kit: dict[str, Any], client: httpx.AsyncClient
) -> None:
    from mapi_server.services.dashboards.assets import asset_key

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (16).to_bytes(4, "big") + (9).to_bytes(4, "big")
    dash_id = kit["dash_id"]
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, dash_id)
        assert dash is not None and dash.published_revision_id is not None
        rows = []
        for k, rev_id in enumerate((dash.published_revision_id, None)):
            asset_id = f"dsa_{k:026d}"
            name = asset_key(dash.tenant_id, dash_id, asset_id)
            meta = await ctx.store.put(ctx.buckets.bundles, name, png, content_type="image/png")
            if rev_id is None:  # an asset of another (older) revision
                old = m.DashboardRevision(
                    id=f"drv_{k:026d}", tenant_id=dash.tenant_id, project_id=dash.project_id,
                    dashboard_id=dash_id, seq=99, spec={}, spec_hash="sha256:" + "2" * 64,
                    model_version_id=kit["model"]["mv_id"], model_ref="model://x",
                    created_by=kit["users"]["ana"].id,
                )  # fmt: skip
                s.add(old)
                await s.flush()
                rev_id = old.id
            rows.append(
                m.DashboardAsset(
                    id=asset_id,
                    tenant_id=dash.tenant_id,
                    project_id=dash.project_id,
                    dashboard_id=dash_id,
                    revision_id=rev_id,
                    plot_id=f"p{k}",
                    status="ready",
                    bucket=meta.bucket,
                    name=meta.name,
                    generation=meta.generation,
                    bytes=len(png),
                    width=16,
                    height=9,
                )
            )
        s.add_all(rows)
    view = {SHARE_SESSION_HEADER: await _link(kit, client, [])}
    r = await client.get(f"/v1/shared/assets/dsa_{0:026d}", headers=view)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png" and r.content == png
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    r = await client.get(f"/v1/shared/assets/dsa_{1:026d}", headers=view)
    assert r.status_code == 404, r.text
    assert (await client.get(f"/v1/shared/assets/dsa_{0:026d}")).status_code == 401
    caller: Caller = kit["cleo"]
    assert (await caller.get(f"/v1/dashboards/{dash_id}/assets/dsa_{0:026d}")).status_code == 200
