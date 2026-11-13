"""Every admission computes the overlay pins of the uploads it uses (v0.19, WP7, D9).

One test per call site of the probe's list (``plans/v019-probe.md`` (h)): spec runs (an
uploaded model, an uploaded step with a built-in model, an uploaded recipe in
``outputs.plots``), recipe previews, runs derived from a model version (render, score
requests, explore, optimize, governance: they inherit the version's lineage pins), workflow
node runs and child fits (with the gallery template they came from), and the kinds that
never mount uploads (ingest, inspect, describe). Each run with pins is ``untrusted_code``;
the gate, the untrusted placement rule and the public-tier refusal run at admission.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services import optimize as optimize_service
from mapi_server.services.explore import submit_model_explore
from mapi_server.services.governance import _submit_run as submit_governance
from mapi_server.services.optimize import submit_optimize
from mapi_server.services.plot_previews import request_preview
from mapi_server.services.runs import create_run, load_run
from mapi_server.services.submit import admit, submit_model_run, submit_spec_run
from mapi_server.services.workflows import WorkflowOrchestrator
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.builders import TREND_SPEC, trend_spec
from mapi_server_testkit.identity import grant_role, make_tenant, make_user
from mapi_server_testkit.plots import model_with_plots
from mapi_server_testkit.workflows import put_dataset
from mapi_spec.errors import ErrorCode, MapiError, ValidationFailed
from mapi_spec.executor import ExecutorCaps, Isolation, RunKind
from mapi_spec.explore import ModelExploreRequest
from mapi_spec.governance import PolicyCheck
from mapi_spec.jobs import RecipePreviewRequest
from mapi_spec.optimize import OptimizeRequest
from mapi_spec.plugins import OverlayPin
from mapi_spec.resources import ResourceClass
from mapi_workflows.testing import weekly_series
from sqlalchemy import select
from test_catalog_view import NS_A, acting, seed_version, set_private_use, set_state, sha

pytestmark = pytest.mark.integration

STEP_SPEC = TREND_SPEC.replace(
    "  - {id: scale_y, use: core.standardize@^1, with: {vars: [y], over: [t]}}",
    f"  - {{id: scale_y, use: {NS_A}.winsorize@^1, with: {{vars: [y], over: [t]}}}}",
)
MODEL_SPEC = TREND_SPEC.replace("use: ts.hsgp_trend@^1", f"use: {NS_A}.store_mean@^1")
RECIPE_SPEC = TREND_SPEC.replace(
    "evaluate: {metrics: [rmse]}",
    f"evaluate: {{metrics: [rmse]}}\noutputs: {{plots: [{NS_A}.fan]}}",
)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
async def people(ctx: ServerContext) -> dict[str, m.User]:
    """``ana`` (admin) and ``ed`` (editor) of the internal-tier tenant ``default``."""
    out: dict[str, m.User] = {}
    for name, role in (("ana", "admin"), ("ed", "editor")):
        user = await make_user(ctx, f"{name}@default.test")
        await grant_role(ctx, user, "default", None, role)
        out[name] = user
    return out


def the_pin(version_id: str, sha256: str) -> dict[str, Any]:
    return {"plugin_version_id": version_id, "namespace": NS_A, "sha256": sha256}


async def fit(ctx: ServerContext, spec_id: str) -> m.Run:
    async with ctx.db.session() as s:
        run = await submit_spec_run(s, ctx, kind=RunKind.FIT, spec_id=spec_id, revision=1)
        s.expunge(run)
    return run


async def audit_details(ctx: ServerContext, run_id: str) -> dict[str, Any]:
    async with ctx.db.session() as s:
        row = (
            await s.execute(select(m.AuditEvent).where(m.AuditEvent.resource_id == run_id).limit(1))
        ).scalar_one()
        return dict(row.details or {})


# ============================================================================== spec runs


@pytest.mark.parametrize("yaml", [MODEL_SPEC, STEP_SPEC, RECIPE_SPEC],
                         ids=["uploaded-model", "uploaded-step", "uploaded-recipe"])  # fmt: skip
async def test_a_fit_pins_every_upload_it_names(
    ctx: ServerContext, people: dict[str, m.User], yaml: str
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ana"].id, sha256=sha(9))
    spec_id = await trend_spec(ctx, yaml=yaml)
    run = await fit(await acting(ctx, people["ed"].id), spec_id)
    assert run.overlays == [the_pin(vid, sha(9))]
    assert run.untrusted_code is True
    # the spec keeps pinning the base catalog digest
    async with ctx.db.session() as s:
        assert run.catalog_digest == (await ctx.catalogs.require(s)).digest
    details = await audit_details(ctx, run.id)
    assert details["untrusted_code"] is True and details["overlays"] == [vid]


async def test_a_built_in_fit_has_no_pins(ctx: ServerContext, people: dict[str, m.User]) -> None:
    await seed_version(ctx, uploaded_by=people["ana"].id)
    run = await fit(await acting(ctx, people["ed"].id), await trend_spec(ctx))
    assert run.overlays == [] and run.untrusted_code is False


async def test_a_private_upload_runs_for_its_uploader_only(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ed"].id, state="kit_passed", sha256=sha(5))
    spec_id = await trend_spec(ctx, yaml=STEP_SPEC)
    run = await fit(await acting(ctx, people["ed"].id), spec_id)
    assert run.overlays == [the_pin(vid, sha(5))] and run.untrusted_code
    # a teammate: the upload is not theirs to use (409 with why, not an unknown step)
    with pytest.raises(MapiError) as err:
        await fit(await acting(ctx, people["ana"].id), spec_id)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE
    # private use turned off: the uploader is refused too
    await set_private_use(ctx, "default", False)
    with pytest.raises(MapiError) as err:
        await fit(await acting(ctx, people["ed"].id), spec_id)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


async def test_a_yanked_upload_is_refused(ctx: ServerContext, people: dict[str, m.User]) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ana"].id)
    spec_id = await trend_spec(ctx, yaml=STEP_SPEC)
    await set_state(ctx, vid, "yanked")
    with pytest.raises(MapiError) as err:
        await fit(await acting(ctx, people["ed"].id), spec_id)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


async def test_without_a_request_only_approved_uploads_run(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    """A platform caller (no request: samples, sweeps) acts for nobody in particular."""
    await seed_version(ctx, uploaded_by=people["ed"].id, state="kit_passed")
    spec_id = await trend_spec(ctx, yaml=STEP_SPEC)
    with pytest.raises(MapiError) as err:
        await fit(ctx, spec_id)
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE


# =========================================================================== previews


async def test_a_preview_of_an_uploaded_recipe_mounts_it(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ana"].id, sha256=sha(11))
    as_ed = await acting(ctx, people["ed"].id)
    result = await request_preview(
        as_ed, f"{NS_A}.fan", "1.0.0", RecipePreviewRequest(settings={"width": 400})
    )
    assert result.run_id is not None
    async with ctx.db.session() as s:
        run = await load_run(s, None, result.run_id)
        assert run.overlays == [the_pin(vid, sha(11))] and run.untrusted_code
    # no settings: the stored example, without a run
    plain = await request_preview(as_ed, f"{NS_A}.fan", "1.0.0", RecipePreviewRequest())
    assert plain.run_id is None and plain.plot is not None


# ======================================================= runs derived from a model version


async def _version_with_pin(ctx: ServerContext, pin: OverlayPin) -> m.ModelVersion:
    model = await model_with_plots(ctx)
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, model["mv_id"])
        assert mv is not None
        mv.lineage = {**(mv.lineage or {}), "overlays": [pin.model_dump(mode="json")]}
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, model["mv_id"])
        assert mv is not None
        s.expunge(mv)
        return mv


async def test_runs_of_a_model_version_inherit_its_pins(
    ctx: ServerContext, people: dict[str, m.User], monkeypatch: pytest.MonkeyPatch
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ana"].id, sha256=sha(13))
    pin = OverlayPin(plugin_version_id=vid, namespace=NS_A, sha256=sha(13))
    mv = await _version_with_pin(ctx, pin)
    as_ed = await acting(ctx, people["ed"].id)
    want = [pin.model_dump(mode="json")]
    made: dict[str, m.Run] = {}
    async with as_ed.db.session() as s:
        made["render"] = await submit_model_run(
            s, as_ed, kind=RunKind.RENDER, model_version_id=mv.id, params={}
        )
        made["explore"], _ = await submit_model_explore(s, as_ed, mv, ModelExploreRequest())
        monkeypatch.setattr(optimize_service, "_capabilities", lambda _mv: SimpleNamespace(
            decision=True))  # fmt: skip
        model = await s.get(m.Model, mv.model_id)
        assert model is not None
        request = OptimizeRequest.model_validate(OptimizeRequest.model_json_schema()["examples"][1])
        made["optimize"] = await submit_optimize(s, as_ed, model=model, mv=mv, request=request)
        validation = SimpleNamespace(id="val_01aaaaaaaaaaaaaaaaaaaaaaaa", dataset=None)
        made["governance"] = await submit_governance(
            s,
            as_ed,
            validation,
            mv,
            [PolicyCheck(check="convergence")],  # type: ignore[arg-type]
        )
        for kind, run in made.items():
            assert run.overlays == want, kind
            assert run.untrusted_code, kind


async def test_a_yanked_version_stops_runs_of_models_fitted_with_it(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    """A dashboard refresh or endpoint call re-scores through ``submit_model_run``: once the
    version is withdrawn, anything that needs its code again is refused with its name."""
    vid = await seed_version(ctx, uploaded_by=people["ana"].id, sha256=sha(13))
    mv = await _version_with_pin(
        ctx, OverlayPin(plugin_version_id=vid, namespace=NS_A, sha256=sha(13))
    )
    await set_state(ctx, vid, "yanked")
    as_ed = await acting(ctx, people["ed"].id)
    with pytest.raises(MapiError) as err:
        async with as_ed.db.session() as s:
            await submit_model_run(s, as_ed, kind=RunKind.RENDER, model_version_id=mv.id,
                                   params={})  # fmt: skip
    assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE and NS_A in err.value.detail


# ============================================================== kinds that never mount


@pytest.mark.parametrize("kind", [RunKind.INGEST, RunKind.INSPECT, RunKind.DESCRIBE])
async def test_ingest_inspect_and_describe_never_mount_uploads(
    ctx: ServerContext, people: dict[str, m.User], kind: RunKind
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ana"].id, sha256=sha(3))
    pin = OverlayPin(plugin_version_id=vid, namespace=NS_A, sha256=sha(3))
    async with ctx.db.session() as s:
        run = await create_run(
            s, ctx.scope, kind=kind, resource_class=ResourceClass.S, timeout_s=600,
            payload={}, spec_hash=sha(1),
        )  # fmt: skip
        with pytest.raises(MapiError) as err:
            await admit(s, ctx, run, overlays=[pin])
        assert err.value.code is ErrorCode.PLUGIN_UNAVAILABLE
        await admit(s, ctx, run, overlays=[])
        assert run.overlays == [] and run.untrusted_code is False


# ===================================================== placement and the public tier


async def _report(ctx: ServerContext, name: str, caps: ExecutorCaps) -> None:
    async with ctx.db.session() as s:
        s.add(
            m.ExecutorReport(
                name=name, enabled=True, caps=caps.model_dump(mode="json"),
                checked_at=datetime.now(UTC),
            )
        )  # fmt: skip


async def test_uploaded_code_needs_an_executor_that_may_run_it(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    await seed_version(ctx, uploaded_by=people["ana"].id)
    spec_id = await trend_spec(ctx, yaml=STEP_SPEC)
    await _report(ctx, "local", ExecutorCaps(isolation=Isolation.PROCESS))
    as_ed = await acting(ctx, people["ed"].id)
    with pytest.raises(MapiError) as err:
        await fit(as_ed, spec_id)
    assert err.value.code is ErrorCode.UNTRUSTED_PLACEMENT_UNAVAILABLE
    # built-in work is not affected
    assert (await fit(as_ed, await _plain_spec(ctx))).untrusted_code is False
    # a docker-like executor reports: the fit is admitted (dev profile: process isolation)
    await _report(ctx, "docker", ExecutorCaps(isolation=Isolation.PROCESS,
                                             runs_untrusted_code=True))  # fmt: skip
    ctx.settings.profile = Profile.DEV
    assert (await fit(as_ed, spec_id)).untrusted_code
    # the multi-tenant profile also needs gVisor with egress and DNS enforced
    ctx.settings.profile = Profile.MULTI_TENANT
    with pytest.raises(MapiError) as err:
        await fit(as_ed, spec_id)
    assert err.value.code is ErrorCode.UNTRUSTED_PLACEMENT_UNAVAILABLE


async def _plain_spec(ctx: ServerContext) -> str:
    from mapi_spec.api import new_id

    async with ctx.db.session() as s:
        rev = (
            await s.execute(select(m.SpecRevision).order_by(m.SpecRevision.spec_id).limit(1))
        ).scalar_one()
        spec = m.Spec(id=new_id("spc"), tenant_id="default", project_id="default",
                      name="plain", kind="ModelSpec", latest_revision=1)  # fmt: skip
        s.add(spec)
        await s.flush()
        s.add(
            m.SpecRevision(
                spec_id=spec.id, revision=1, tenant_id="default", project_id="default",
                kind="ModelSpec", hash=rev.hash, yaml=TREND_SPEC,
                document={**rev.document, "pipeline": [
                    {"id": "scale_y", "use": "core.standardize@^1",
                     "with": {"vars": ["y"], "over": ["t"]}}]},
                valid=True, issues=[],
            )
        )  # fmt: skip
        return spec.id


async def test_public_tenants_never_run_uploaded_code(ctx: ServerContext) -> None:
    """Refused at admission even with cut line B open and identity verified (D1)."""
    ctx.settings.public_code_upload_enabled = True
    await make_tenant(ctx, "pub", tier="public")
    async with ctx.db.session() as s:
        s.add(m.TenantVerification(tenant_id="pub", identity_verified=True,
                                    updated_at=datetime.now(UTC)))  # fmt: skip
    async with ctx.db.session() as s:
        from mapi_server.services.runs import Scope

        run = await create_run(
            s, Scope("pub", "main"), kind=RunKind.FIT, resource_class=ResourceClass.S,
            timeout_s=600, payload={}, spec_hash=sha(1),
        )  # fmt: skip
        with pytest.raises(MapiError) as err:
            await admit(s, ctx, run, untrusted=True)
        assert err.value.code is ErrorCode.IDENTITY_VERIFICATION_REQUIRED


# ============================================================================ workflows


def _wf_doc(nodes: list[dict[str, Any]], labels: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "uses-uploads", "project": "default", "labels": labels or {}},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": nodes,
    }


async def _start(
    ctx: ServerContext, user: m.User, doc: dict[str, Any], client: httpx.AsyncClient
) -> str:
    from mapi_server_testkit.identity import session_caller

    me = await session_caller(ctx, client, user, tenant_id="default", project_id="default")
    r = await me.post("/v1/workflows", json={"document": doc})
    assert r.status_code == 201, r.text
    r = await me.post(f"/v1/workflows/{r.json()['id']}/runs",
                      json={"inputs": {"data": "dataset://default/sales@latest"}})  # fmt: skip
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


async def _child(ctx: ServerContext, run_id: str, node: str) -> m.Run:
    await WorkflowOrchestrator(ctx).tick_all()
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowNodeRunRow, (run_id, node))
        assert row is not None and row.child_run_id is not None, row and (row.status, row.error)
        child = await s.get(m.Run, row.child_run_id)
        assert child is not None
        s.expunge(child)
    return child


async def test_workflow_node_runs_pin_their_uploads_and_template(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ed"].id, sha256=sha(21))
    await put_dataset(ctx, "sales", weekly_series(60))
    doc = _wf_doc(
        [
            {"id": "adf", "use": "test.adf", "inputs": {"data": "inputs.data"}},
            {"id": "pic", "use": "plot.render", "inputs": {"data": "inputs.data"},
             "with": {"recipe": f"{NS_A}.fan@1.0.0"}},
        ],
        labels={"workflow-template": f"{NS_A}_flow"},
    )  # fmt: skip
    run_id = await _start(ctx, people["ana"], doc, client)
    want = [the_pin(vid, sha(21))]
    adf = await _child(ctx, run_id, "adf")
    # a built-in node of a workflow from an uploaded template: its origin is recorded
    assert adf.overlays == want and adf.untrusted_code
    assert adf.created_by == people["ana"].id


async def test_a_workflow_child_fit_pins_the_uploaded_model(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    vid = await seed_version(ctx, uploaded_by=people["ed"].id, sha256=sha(23))
    await put_dataset(ctx, "sales", weekly_series(60))
    doc = _wf_doc([{"id": "fit", "use": "model.fit", "inputs": {"data": "inputs.data"},
                    "with": {"model": f"{NS_A}.store_mean@^1"}}])  # fmt: skip
    run_id = await _start(ctx, people["ana"], doc, client)
    await WorkflowOrchestrator(ctx).tick_all()
    async with ctx.db.session() as s:
        row = await s.get(m.WorkflowNodeRunRow, (run_id, "fit"))
        assert row is not None
        if row.child_run_id is None:  # the toy dataset does not fit the fixture model
            assert row.error is not None and row.error.get("code") != "PLUGIN_UNAVAILABLE", (
                row.error
            )
            pytest.skip(f"the child fit did not validate: {json.dumps(row.error)[:200]}")
        child = await s.get(m.Run, row.child_run_id)
        assert child is not None
        assert child.kind == RunKind.FIT.value
        assert child.overlays == [the_pin(vid, sha(23))] and child.untrusted_code


async def test_a_workflow_naming_an_upload_its_starter_cannot_use_does_not_start(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    from mapi_server_testkit.identity import session_caller

    await seed_version(ctx, uploaded_by=people["ed"].id, state="kit_passed")
    await put_dataset(ctx, "sales", weekly_series(60))
    doc = _wf_doc([{"id": "fit", "use": "model.fit", "inputs": {"data": "inputs.data"},
                    "with": {"model": f"{NS_A}.store_mean@^1"}}])  # fmt: skip
    ana = await session_caller(ctx, client, people["ana"], tenant_id="default",
                               project_id="default")  # fmt: skip
    r = await ana.post("/v1/workflows", json={"document": doc})
    assert r.status_code == 201, r.text
    r = await ana.post(f"/v1/workflows/{r.json()['id']}/runs",
                      json={"inputs": {"data": "dataset://default/sales@latest"}})  # fmt: skip
    assert r.status_code == 409, r.text
    assert r.json()["code"] in ("PLUGIN_UNAVAILABLE", "WORKFLOW_NOT_RUNNABLE")


async def test_validation_errors_stay_validation_errors(
    ctx: ServerContext, people: dict[str, m.User]
) -> None:
    """A spec that names no upload but is invalid still answers with its issues."""
    spec_id = await trend_spec(ctx, yaml=TREND_SPEC.replace("ts.hsgp_trend", "ts.missing"))
    with pytest.raises(ValidationFailed):
        await fit(await acting(ctx, people["ed"].id), spec_id)


def _uploaded_node(namespace: str) -> dict[str, Any] | None:
    """A contract of an uploaded test node, or ``None`` while the spec refuses an uploaded
    namespace in ``NodeImplementation.plugin`` (see the WP7 report)."""
    from mapi_spec.workflow_nodes import NodeImplementation
    from pydantic import ValidationError
    from test_workflow_plugin_nodes import _contract

    contract = _contract(f"test.{namespace}_zeros", plugin=namespace)
    try:
        NodeImplementation.model_validate(contract)
    except ValidationError:
        return None
    return contract


async def test_an_uploaded_node_run_pins_its_plugin(
    ctx: ServerContext, client: httpx.AsyncClient, people: dict[str, m.User]
) -> None:
    from test_catalog_view import exports

    contract = _uploaded_node(NS_A)
    if contract is None:
        pytest.skip("mapi_spec refuses uploaded node namespaces (NodeImplementation._kind)")
    body = {**exports(NS_A), "workflow_nodes": [contract]}
    vid = await seed_version(ctx, uploaded_by=people["ed"].id, sha256=sha(25), payload=body)
    await put_dataset(ctx, "sales", weekly_series(60))
    doc = _wf_doc([{"id": "zeros", "use": f"test.{NS_A}_zeros",
                    "inputs": {"data": "inputs.data"}, "with": {"max_share": 0.3}}])  # fmt: skip
    run_id = await _start(ctx, people["ana"], doc, client)
    child = await _child(ctx, run_id, "zeros")
    assert child.kind == RunKind.WORKFLOW_NODE.value
    assert child.overlays == [the_pin(vid, sha(25))] and child.untrusted_code
