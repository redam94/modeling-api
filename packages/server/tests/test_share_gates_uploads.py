"""Uploaded code never leaves the workspace (v0.19, plan D16, T19.18): share links, share
sessions, embeds and the share and dashboard renders.

The facts the gates read are the ones the control plane records: ``lineage.overlays`` of a
model version (the finalizer copies ``runs.overlays``), ``runs.untrusted_code`` of the runs
a readout came from, and catalog ids in an uploaded namespace. The tests write those facts
directly (a fit with an uploaded *step* and a built-in model leaves exactly such a lineage),
so they need no sandbox. A lineage with only the legacy ``overlay_hash`` keeps its v0.16
refusals (``test_dashboards_crud``, ``test_plots_share``).
"""

from __future__ import annotations

import json
from typing import Any, Final

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards import panels as pn
from mapi_server.services.dashboards.inputs import clear_grid_cache
from mapi_server.services.dashboards.loop import DashboardLoop
from mapi_server.services.dashboards.readouts import on_workflow_succeeded
from mapi_server.services.dashboards.tools import call_tool
from mapi_server.services.sharing import (
    ResolvedPlot,
    clear_embed_cache,
    submit_render,
    uses_uploaded_code,
)
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import Caller
from mapi_server_testkit.integrations import refit_version
from mapi_server_testkit.plots import INLINE, plot_source, vl_plot
from mapi_spec.dashboard_v2 import RecipePanel
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from mapi_spec.errors import MapiError
from mapi_spec.plots import PlotSpec
from sqlalchemy import func, select, update
from test_dashboard_refresh import WORKFLOW, _linked_run, _refreshes
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit

pytestmark = pytest.mark.integration

WEB: Final = "http://127.0.0.1:5173"
NS: Final = "t_k3vq7m_acme"
PIN: Final[dict[str, str]] = {
    "plugin_version_id": "plv_01jbz3q2x8m9v7k5h4g3f2e1d1",
    "namespace": NS,
    "sha256": "sha256:" + "ab" * 32,
}
REFUSED: Final = "UPLOADED_CODE_NOT_SHAREABLE"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "web_origin": WEB, **LAYOUT1}
    )


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    clear_grid_cache()
    clear_embed_cache()
    kit = await dashboard_kit(ctx, client)
    kit["dash_id"] = await create_published(kit)
    return kit


async def _pin_version(ctx: ServerContext, mv_id: str) -> None:
    """``mv_id`` was fitted with an uploaded step (a built-in model): its lineage records the
    overlay pin, as the finalizer writes it from ``runs.overlays``."""
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, mv_id)
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlays": [PIN]}


async def _share(kit: dict[str, Any], dash_id: str | None = None) -> httpx.Response:
    return await kit["ana"].post(f"/v1/dashboards/{dash_id or kit['dash_id']}/shares", json={})


async def _open(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/v1/share-sessions", json={"token": token})


def _hdr(session: str) -> dict[str, str]:
    return {SHARE_SESSION_HEADER: session}


def _refused(r: httpx.Response) -> None:
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == REFUSED
    assert "uploaded code" in body["detail"]
    assert NS not in r.text  # the refusal names no uploaded id or text


# ===================================================================================== pure


def test_uses_uploaded_code_reads_lineage_runs_and_ids() -> None:
    assert not uses_uploaded_code()
    assert uses_uploaded_code(lineage={"overlays": [PIN]})
    assert uses_uploaded_code(lineage={"overlay_hash": "sha256:" + "1" * 64})  # legacy key
    assert not uses_uploaded_code(lineage={"overlays": [], "overlay_hash": None})
    trusted = m.Run(untrusted_code=False, overlays=[])
    assert not uses_uploaded_code(run=trusted)
    assert uses_uploaded_code(run=m.Run(untrusted_code=True, overlays=[]))
    for uploaded in (
        f"{NS}.winsorize",  # a step
        f"{NS}.store_mean@^0.1",  # a model ref
        f"{NS}.fan",  # a recipe
        f"model.{NS}_score",  # a workflow node
        f"{NS}_weekly",  # a workflow template
        "t_notacode.x",  # malformed, but in the reserved upload prefix: fail closed
    ):
        assert uses_uploaded_code(ids=[uploaded]), uploaded
    for builtin in ("core.ppc_fan@1.0.0", "mmm.bayesian", "model.score", "weekly_check"):
        assert not uses_uploaded_code(ids=[builtin]), builtin


def test_a_picture_panel_of_an_uploaded_recipe_is_never_drawn() -> None:
    panel = RecipePanel(id="p1", title="Fan", recipe=f"{NS}.fan", plot_id="fan")
    assert panel.is_picture
    asset = pn.AssetInfo(asset_id="dsa_" + "0" * 26, status="ready", width=10, height=10)
    mat = pn.Material(assets={"fan": asset})
    block = pn._picture(panel, mat)
    assert block.kind == "unavailable" and block.reason == pn.UPLOADED_PICTURE
    builtin = RecipePanel(id="p2", title="Fan", recipe="core.ppc_fan", plot_id="fan")
    assert pn._picture(builtin, mat).kind == "picture"


# ============================================================================== share links


async def test_a_dashboard_on_a_version_with_an_uploaded_step_is_never_shared(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    first = await _share(kit)  # built-in only: shares (regression)
    assert first.status_code == 201, first.text
    token = first.json()["token"]
    await _pin_version(ctx, kit["model"]["mv_id"])
    _refused(await _share(kit))
    # the link made before is refused at the exchange, before any session exists
    _refused(await _open(client, token))
    async with ctx.db.session() as s:
        n = await s.execute(
            select(func.count())
            .select_from(m.DashboardShare)
            .where(m.DashboardShare.dashboard_id == kit["dash_id"])
        )
        assert n.scalar_one() == 1


async def test_a_refresh_with_uploaded_code_ends_every_shared_read(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    token = (await _share(kit)).json()["token"]
    session = (await _open(client, token)).json()["session"]
    reads = ("/v1/shared/dashboard", "/v1/shared/readouts", "/v1/shared/readouts/1",
             "/v1/shared/grid")  # fmt: skip
    for path in reads:
        r = await client.get(path, headers=_hdr(session))
        assert r.status_code in (200, 409), (path, r.text)  # 409: the link has no what-ifs
    # the baseline's score run is submitted; then a refresh run turns out to have mounted an
    # uploaded overlay (e.g. a workflow child fit): the readout's run is untrusted
    assert await DashboardLoop(ctx).submit_queued() == 1
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (kit["dash_id"], 1))
        assert row is not None and row.run_ids
        await s.execute(
            update(m.Run)
            .where(m.Run.id == row.run_ids[0])
            .values(untrusted_code=True, overlays=[PIN])
        )
    for path in reads:
        _refused(await client.get(path, headers=_hdr(session)))
    _refused(await client.post("/v1/share-sessions:refresh", headers=_hdr(session)))
    _refused(await _open(client, token))
    _refused(await _share(kit))
    # the analyst still reads it, and hiding the readout takes it out of the shared view
    assert (await kit["ana"].get(f"/v1/dashboards/{kit['dash_id']}")).status_code == 200
    r = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}/readouts/1:hide")
    assert r.status_code == 200, r.text
    r = await client.get("/v1/shared/dashboard", headers=_hdr(session))
    assert r.status_code == 200, r.text


async def test_the_tool_surface_refuses_share_and_client_principals(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    from mapi_server.services.dashboards.access import DashboardContext
    from mapi_spec.dashboards import EffectiveInputs

    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, kit["dash_id"])
        assert dash is not None and dash.published_revision_id is not None
        rev_id = dash.published_revision_id

    def dctx(kind: Any) -> DashboardContext:
        return DashboardContext(
            tenant_id="default", project_id="default", dashboard_id=kit["dash_id"],
            revision_id=rev_id, principal_kind=kind, principal_id="x",
            inputs=EffectiveInputs(),
            is_analyst=kind == "member",
        )  # fmt: skip

    for kind in ("member", "client", "share"):
        await call_tool(ctx, dctx(kind), "get_dashboard", {})
    await _pin_version(ctx, kit["model"]["mv_id"])
    await call_tool(ctx, dctx("member"), "get_dashboard", {})  # members: inside the workspace
    for kind in ("client", "share"):
        with pytest.raises(MapiError) as err:
            await call_tool(ctx, dctx(kind), "list_readouts", {"limit": 5})
        assert err.value.code == REFUSED


# ================================================================== refreshes and readouts


@pytest.fixture
async def wf_kit(kit: dict[str, Any]) -> dict[str, Any]:
    r = await kit["ana"].post("/v1/workflows", json={"document": WORKFLOW})
    assert r.status_code == 201, r.text
    kit["wf_id"] = r.json()["id"]
    return kit


async def _refused_refresh(ctx: ServerContext, kit: dict[str, Any], dash_id: str, wr: str) -> None:
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowRunRow, wr)
        assert row is not None
        assert await on_workflow_succeeded(s, row) == 0
    assert await _refreshes(ctx, dash_id) == []
    body = (await kit["ana"].get(f"/v1/dashboards/{dash_id}")).json()
    assert body["problem"] == "workflow_version_refused"


async def test_a_workflow_that_ran_uploaded_code_never_refreshes_a_dashboard(
    ctx: ServerContext, wf_kit: dict[str, Any]
) -> None:
    kit = wf_kit
    dash_id = await create_published(kit, name="linked", refresh={"workflow_id": kit["wf_id"]})
    # the score node ran on the pinned version, but its run mounted an overlay
    wr = await _linked_run(kit, ctx, kit["model"]["mv_id"])
    async with ctx.db.session() as s:
        node = (
            await s.execute(
                select(m.WorkflowNodeRunRow).where(
                    m.WorkflowNodeRunRow.run_id == wr, m.WorkflowNodeRunRow.node_id == "score"
                )
            )
        ).scalar_one()
        await s.execute(
            update(m.Run).where(m.Run.id == node.child_run_id).values(untrusted_code=True)
        )
    await _refused_refresh(ctx, kit, dash_id, wr)
    # an uploaded node earlier in the workflow taints the run as well
    async with ctx.db.session() as s:
        await s.execute(update(m.Dashboard).where(m.Dashboard.id == dash_id).values(problem=None))
    wr = await _linked_run(kit, ctx, kit["model"]["mv_id"])
    async with ctx.db.session() as s:
        s.add(
            m.WorkflowNodeRunRow(
                run_id=wr, tenant_id="default", project_id="default", node_id="clean",
                position=0, use=f"data.{NS}_clean@0.1.0", kind="data", status="succeeded",
            )
        )  # fmt: skip
    await _refused_refresh(ctx, kit, dash_id, wr)
    # a workflow of platform code refreshes it (regression)
    async with ctx.db.session() as s:
        await s.execute(update(m.Dashboard).where(m.Dashboard.id == dash_id).values(problem=None))
    wr = await _linked_run(kit, ctx, kit["model"]["mv_id"])
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowRunRow, wr)
        assert row is not None
        assert await on_workflow_succeeded(s, row) == 1


async def test_a_readout_whose_run_ran_uploaded_code_never_materialises(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    from mapi_server_testkit.plots import complete_run
    from test_dashboard_readouts import SUMMARY

    loop = DashboardLoop(ctx)
    assert await loop.submit_queued() == 1
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (kit["dash_id"], 1))
        assert row is not None
        run_id = row.run_ids[0]
    await complete_run(
        ctx, run_id, add={"summary.json": (json.dumps(SUMMARY).encode(), "application/json")}
    )
    async with ctx.db.session() as s:
        await s.execute(update(m.Run).where(m.Run.id == run_id).values(untrusted_code=True))
    await loop.materialise()
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardReadout, (kit["dash_id"], 1))
        assert row is not None
        assert row.status == "failed" and row.problem is not None
        assert row.problem["code"] == REFUSED
    # a failed readout leaves the history (hidden): clients never see it, and the rest of
    # the dashboard stays theirs
    cleo: Caller = kit["cleo"]
    assert row.visibility == "hidden"
    assert (await cleo.get(f"/v1/dashboards/{kit['dash_id']}/readouts/1")).status_code == 404
    assert (await cleo.get(f"/v1/dashboards/{kit['dash_id']}")).status_code == 200


# ========================================================================= embeds, renders


@pytest.fixture
async def plots(ctx: ServerContext, kit: dict[str, Any]) -> dict[str, Any]:
    """Two plots of the kit's version (its signed bundle verifies): a platform recipe's and
    an uploaded recipe's (as a fit with that recipe in ``outputs.plots`` stores it), both
    with inline data."""
    model = kit["model"]
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, model["mv_id"])
        assert mv is not None
        for plot_id, recipe in (
            ("core.inline", "core.fan@1.0.0"),
            (f"{NS}.fan", f"{NS}.fan@0.1.0"),
        ):
            doc = vl_plot(plot_id, {"d": {"kind": "inline_arrow", "data": INLINE}}, recipe=recipe)
            s.add(
                m.Plot(
                    tenant_id=mv.tenant_id, project_id=mv.project_id, model_version_id=mv.id,
                    plot_id=plot_id, title=plot_id, kind="vega-lite", persona="analyst",
                    recipe=recipe, caption_plain=None, spec=doc,
                )
            )  # fmt: skip
    return dict(model)


async def _save(ana: Caller, model: dict[str, Any], plot_id: str) -> dict[str, Any]:
    r = await ana.post("/v1/plots", json={"title": "x", "source": plot_source(model, plot_id)})
    assert r.status_code == 201, r.text
    return dict(r.json())


async def test_embeds_of_uploaded_recipes_and_of_overlay_versions_are_refused(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any], plots: dict[str, Any]
) -> None:
    ana: Caller = kit["ana"]
    uploaded = await _save(ana, plots, f"{NS}.fan")
    assert uploaded["embeddable"] is False
    _refused(await ana.post(f"/v1/plots/{uploaded['id']}/embeds", json={}))
    builtin = await _save(ana, plots, "core.inline")
    assert builtin["embeddable"] is True
    made = await ana.post(f"/v1/plots/{builtin['id']}/embeds", json={})
    assert made.status_code == 201, made.text
    token = made.json()["token"]
    assert (await client.get(f"/v1/embeds/{token}")).status_code == 200
    # the version turns out to have run an uploaded step: a built-in recipe's plot of it
    # is refused at create and at the public view (the cache is not consulted first)
    await _pin_version(ctx, plots["mv_id"])
    _refused(await ana.post(f"/v1/plots/{builtin['id']}/embeds", json={}))
    _refused(await client.get(f"/v1/embeds/{token}"))
    listed = (await ana.get(f"/v1/plots/{builtin['id']}")).json()
    assert listed["embeddable"] is False
    async with ctx.db.session() as s:
        n = await s.execute(
            select(func.count())
            .select_from(m.PlotEmbed)
            .where(m.PlotEmbed.plot_id.in_([uploaded["id"], builtin["id"]]))
        )
        assert n.scalar_one() == 1


async def test_shared_renders_refuse_uploaded_code_before_admission(
    ctx: ServerContext, kit: dict[str, Any], plots: dict[str, Any]
) -> None:
    """``submit_render(shared=True)`` (an embed's fallback, a dashboard picture) refuses a plot
    made with uploaded code before any run row or input object exists; a signed-in render of
    the same plot stays allowed (no recipe code runs in a render, plan §0 P6)."""
    doc = vl_plot(f"{NS}.fan", {"d": {"kind": "inline_arrow", "data": INLINE}},
                  recipe=f"{NS}.fan@0.1.0")  # fmt: skip
    plot = PlotSpec.model_validate(doc)
    resolved = ResolvedPlot(
        plot=plot, model_version=None, source_kind="model_plot", provenance="recipe",
        recipe=f"{NS}.fan@0.1.0",
    )  # fmt: skip

    async def runs() -> int:
        async with ctx.db.session() as s:
            q = select(func.count()).select_from(m.Run).where(m.Run.kind == "render")
            return int((await s.execute(q)).scalar_one())

    before = await runs()
    async with ctx.db.session() as s:
        with pytest.raises(MapiError) as err:
            await submit_render(s, ctx, resolved, fmt="png", scale=1.0, shared=True)
        assert err.value.code == REFUSED
    assert await runs() == before
    # signed in, inside the workspace: the stored plot renders
    r = await kit["ana"].post(
        "/v1/plots:render", json={"source": plot_source(plots, f"{NS}.fan"), "format": "svg"}
    )
    assert r.status_code in (200, 202), r.text
    assert await runs() == before + 1


async def test_a_built_in_only_dashboard_and_plot_still_share(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    """Regression: nothing of the gates touches platform-only work, and the facts of another
    version of the same project do not leak into this dashboard."""
    async with ctx.db.session() as sess:
        mv = await sess.get(m.ModelVersion, kit["model"]["mv_id"])
        assert mv is not None
    other = await refit_version(ctx, mv)
    await _pin_version(ctx, other.id)
    created = await _share(kit)
    assert created.status_code == 201, created.text
    opened = await _open(client, created.json()["token"])
    assert opened.status_code == 200, opened.text
    r = await client.get("/v1/shared/readouts", headers=_hdr(opened.json()["session"]))
    assert r.status_code == 200, r.text
    assert (await kit["cleo"].get(f"/v1/dashboards/{kit['dash_id']}")).status_code == 200
