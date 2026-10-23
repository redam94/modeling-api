"""Client users never see or run what uploaded code made (v0.19, plan D16, T19.18).

``cleo`` is a client of ``default/default`` with a grant on a published dashboard (the
helpers of ``test_dashboards_crud``). Once the dashboard's pinned version records an overlay
pin (a fit with an uploaded step, a recipe or a node, not only an uploaded model), or a
readout clients would see came from a run that ran uploaded code, every client read and
input answers ``403 UPLOADED_CODE_NOT_SHAREABLE`` before anything runs; the analysts keep
their view. The client role still reaches no plugin route (``CLIENT_OPERATIONS``).
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.loop import DashboardLoop
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller
from mapi_spec.api import ALL_ENDPOINTS, CLIENT_OPERATIONS
from sqlalchemy import func, select, update
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit

pytestmark = pytest.mark.integration

PIN: Final[dict[str, str]] = {
    "plugin_version_id": "plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
    "namespace": "t_k3vq7m_acme",
    "sha256": "sha256:" + "ab" * 32,
}
REFUSED: Final = "UPLOADED_CODE_NOT_SHAREABLE"
PLAN: Final = {"multipliers": {"tv": 0.8}}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, **LAYOUT1})


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    clear_grid_cache()
    kit = await dashboard_kit(ctx, client)
    kit["dash_id"] = await create_published(kit)
    return kit


def _refused(r: httpx.Response) -> None:
    assert r.status_code == 403, r.text
    assert r.json()["code"] == REFUSED
    assert "t_k3vq7m_acme" not in r.text


async def _score_runs(ctx: ServerContext) -> int:
    async with ctx.db.session() as s:
        q = select(func.count()).select_from(m.Run).where(m.Run.kind == "score")
        return int((await s.execute(q)).scalar_one())


def test_the_client_role_reaches_no_plugin_route() -> None:
    plugin_ops = {e.operation_id for e in ALL_ENDPOINTS if e.path.startswith("/v1/plugins")} | {
        e.operation_id for e in ALL_ENDPOINTS if "plugin-" in e.path
    }
    assert plugin_ops, "the v0.19 plugin routes are declared"
    assert not plugin_ops & set(CLIENT_OPERATIONS)


async def test_a_client_cannot_read_or_run_a_dashboard_made_with_uploaded_code(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    cleo: Caller = kit["cleo"]
    base = f"/v1/dashboards/{kit['dash_id']}"
    assert (await cleo.get(base)).status_code == 200
    r = await cleo.post(f"{base}/whatif:evaluate", json=PLAN)
    assert r.status_code == 200, r.text  # a what-if reads the grid: no run
    before = await _score_runs(ctx)
    async with ctx.db.session() as s:  # the version was fitted with an uploaded step
        mv = await s.get(m.ModelVersion, kit["model"]["mv_id"])
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlays": [PIN]}
    for r in (
        await cleo.get(base),
        await cleo.get(f"{base}/readouts"),
        await cleo.get(f"{base}/readouts/1"),
        await cleo.get(f"{base}/grid"),
        await cleo.post(f"{base}/whatif:evaluate", json=PLAN),
        await cleo.post(f"{base}/scenarios", json={**PLAN, "exact": True}),
        await cleo.get(f"{base}/assets/dsa_{'0' * 26}"),  # refused before the lookup
    ):
        _refused(r)
    assert await _score_runs(ctx) == before  # nothing was submitted
    # the dashboard stays in the client's list (no data); the analysts keep their view
    listed = (await cleo.get("/v1/dashboards")).json()["items"]
    assert [d["id"] for d in listed] == [kit["dash_id"]]
    assert (await kit["vic"].get(base)).status_code == 200
    assert (await kit["vic"].get(f"{base}/readouts")).status_code == 200


async def test_a_client_is_refused_once_a_readout_ran_uploaded_code(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    cleo: Caller = kit["cleo"]
    base = f"/v1/dashboards/{kit['dash_id']}"
    assert await DashboardLoop(ctx).submit_queued() == 1
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (kit["dash_id"], 1))
        assert row is not None and row.run_ids
        await s.execute(
            update(m.Run)
            .where(m.Run.id == row.run_ids[0])
            .values(untrusted_code=True, overlays=[PIN])
        )
    _refused(await cleo.get(base))
    _refused(await cleo.post(f"{base}/whatif:evaluate", json=PLAN))
    # an analyst hides the readout: it leaves the clients' view, and the gate with it
    r = await kit["ana"].post(f"{base}/readouts/1:hide")
    assert r.status_code == 200, r.text
    assert (await cleo.get(base)).status_code == 200


async def test_a_spec_naming_an_uploaded_recipe_is_refused_to_clients(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """Defence in depth: a published revision whose stored spec names a recipe in an
    uploaded namespace (publishing validates against the base catalog, so this needs a
    spec written behind its back) is refused before it is parsed."""
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, kit["dash_id"])
        assert dash is not None and dash.published_revision_id is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        spec = dict(rev.spec)
        rev.spec = {**spec, "panels": [{"kind": "recipe", "recipe": "t_k3vq7m_acme.fan"}]}
    _refused(await kit["cleo"].get(f"/v1/dashboards/{kit['dash_id']}"))
    async with ctx.db.session() as s:
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        rev.spec = spec
    assert (await kit["cleo"].get(f"/v1/dashboards/{kit['dash_id']}")).status_code == 200
