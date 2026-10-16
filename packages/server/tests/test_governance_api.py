"""Model governance through the API (M3.5): versioned policies (project and tenant-wide,
defaults seeded), validations that run as governance runs and advance on the run's result,
second-person approval with the reviewer capability, the promotion gate, the evidence pack,
audit and webhook rows, and cross-tenant isolation."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import simulate_outputs
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
)
from mapi_server_testkit.integrations import fitted_version
from mapi_server_testkit.plots import _to_finalizing
from mapi_spec.api import new_id
from mapi_spec.governance import (
    DEFAULT_POLICIES,
    GOVERNANCE_CHECKS,
    GOVERNANCE_OUTPUT,
    CheckResult,
    CheckStatus,
    GovernanceJob,
    GovernanceReport,
)
from mapi_spec.run import Actor, RunState, RunTrigger
from sqlalchemy import select, text, update

pytestmark = pytest.mark.integration

POL = "/v1/governance/policies"
STRICT = {
    "name": "Everything we promote",
    "checks": [
        {"check": "data.quality"},
        {"check": "convergence"},
        {"check": "null.permutation_target", "params": {"n_permutations": 20}},
    ],
    "require_approval": True,
    "require_validated_for_promotion": True,
}


async def _audit(ctx: ServerContext) -> list[str]:
    async with ctx.db.session() as s:
        return [r.action for r in (await s.execute(select(m.AuditEvent))).scalars()]


def _report(job: dict[str, Any], *, fail: str | None = None) -> bytes:
    j = GovernanceJob.model_validate(job)
    results = [
        CheckResult(
            check=c.check,
            severity=c.severity,
            metric=GOVERNANCE_CHECKS[c.check].metric,
            status=CheckStatus.FAIL if c.check == fail else CheckStatus.PASS,
            value=0.5 if c.check == fail else 0.01,
            message_plain=f"{c.check} measured",
            suggested_action="do something" if c.check == fail else None,
        )
        for c in j.checks
    ]
    report = GovernanceReport(
        model_ref=j.model_ref,
        model_id="ts.hsgp_trend",
        family="ts",
        model_kind="bayesian",
        results=results,
        refits=21,
    )
    return report.model_dump_json().encode()


async def _finish_governance(ctx: ServerContext, run_id: str, *, fail: str | None = None) -> None:
    run, att = await _to_finalizing(ctx, run_id)
    body = _report(run.payload, fail=fail)
    await simulate_outputs(ctx, run, att, add={GOVERNANCE_OUTPUT: (body, "application/json")})
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED


async def _crash_governance(ctx: ServerContext, run_id: str) -> None:
    from mapi_server.services.dispatcher import Dispatcher
    from mapi_server_testkit.fake_executor import FakeExecutor
    from mapi_spec.errors import ErrorCode, Problem

    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(
            s,
            run,
            RunTrigger.FAIL_USER,
            Actor.RECONCILER,
            problem=Problem.from_code(ErrorCode.USER_CODE_ERROR, "boom"),
        )


async def _people(ctx: ServerContext, client: httpx.AsyncClient) -> tuple[Any, Any, m.User]:
    rita = await make_user(ctx, "rita@default.test")
    eve = await make_user(ctx, "eve@default.test")
    await grant_role(ctx, rita, "default", "default", "editor")
    await grant_role(ctx, eve, "default", "default", "editor")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == rita.id)
            .values(capabilities=["reviewer"])
        )
    reviewer = await session_caller(ctx, client, rita, tenant_id="default", project_id="default")
    editor = await session_caller(ctx, client, eve, tenant_id="default", project_id="default")
    return reviewer, editor, rita


async def test_the_check_catalog_is_served(client: httpx.AsyncClient) -> None:
    page = (await client.get("/v1/catalog/governance-checks?limit=200")).json()
    assert {c["id"] for c in page["items"]} == set(GOVERNANCE_CHECKS)
    assert all(c["about"]["summary_plain"] for c in page["items"])
    first = (await client.get("/v1/catalog/governance-checks?limit=5")).json()
    rest = (await client.get(f"/v1/catalog/governance-checks?cursor={first['next_cursor']}")).json()
    assert len(first["items"]) + len(rest["items"]) == len(GOVERNANCE_CHECKS)


async def test_policies_are_seeded_versioned_and_retired(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    page = (await client.get(POL)).json()
    assert [p["name"] for p in page["items"]] == [p.name for p in DEFAULT_POLICIES]
    assert all(p["scope"] == "tenant" and p["created_by"] == "system" for p in page["items"])
    r = await client.post(POL, json=STRICT)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["version"] == 1 and r.json()["scope"] == "project"
    stale = await client.put(f"{POL}/{pid}", json={**STRICT, "expected_version": 2})
    assert stale.status_code == 409
    v2 = await client.put(f"{POL}/{pid}", json={**STRICT, "name": "Renamed", "expected_version": 1})
    assert v2.status_code == 200 and v2.json()["version"] == 2
    versions = (await client.get(f"{POL}/{pid}/versions")).json()["items"]
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["name"] == STRICT["name"]
    bad = await client.post(POL, json={**STRICT, "checks": [{"check": "telepathy"}]})
    assert bad.status_code == 422
    assert {p["scope"] for p in (await client.get(POL + "?scope=project")).json()["items"]} == {
        "project"
    }
    assert (await client.delete(f"{POL}/{pid}")).status_code == 204
    assert pid not in [p["id"] for p in (await client.get(POL)).json()["items"]]
    assert (await client.get(f"{POL}/{pid}")).json()["active"] is False
    assert (await client.put(f"{POL}/{pid}", json=STRICT)).status_code == 409
    tenant = (await client.get("/v1/tenants/default/governance/policies")).json()["items"]
    assert len(tenant) == len(DEFAULT_POLICIES)
    tid = tenant[0]["id"]
    r = await client.put(
        f"/v1/tenants/default/governance/policies/{tid}", json={**STRICT, "name": "Floor"}
    )
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["project_id"] is None
    actions = await _audit(ctx)
    assert {"policy.create", "policy.update", "policy.delete"} <= set(actions)


async def test_validation_approval_and_promotion(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    reviewer, editor, _rita = await _people(ctx, client)
    assert (await client.post(POL, json=STRICT)).status_code == 201
    # promotion is blocked before validation (no override helps)
    r = await client.put(
        "/v1/endpoints/trend-prod",
        json={"model": "model://default/trend@v1", "override": True, "reason": "trust me"},
    )
    assert r.status_code == 409 and r.json()["code"] == "NOT_VALIDATED", r.text
    r = await client.post(
        "/v1/models/" + mv.model_id + "/versions/1/validations", json={"note": "Q4 refresh"}
    )
    assert r.status_code == 202, r.text
    val = r.json()
    assert val["status"] == "checks_running" and val["require_approval"] is True
    checks = {c["check"] for c in val["required_checks"]}
    assert {
        "data.quality",
        "convergence",
        "null.permutation_target",
        "baseline.beats_naive",
    } <= checks
    again = await client.post(f"/v1/models/{mv.model_id}/versions/1/validations", json={})
    assert again.status_code == 409 and again.json()["code"] == "VALIDATION_TRANSITION_INVALID"
    run = (await client.get(f"/v1/runs/{val['workflow_run_id']}")).json()
    assert run["kind"] == "governance" and run["model_version_id"] == mv.id
    async with ctx.db.session() as s:
        row = await s.get(m.Run, run["id"])
        assert row is not None and {"bundle", "dataset"} <= set(row.inputs)
    await _finish_governance(ctx, run["id"])
    val = (await client.get(f"/v1/validations/{val['id']}")).json()
    assert val["status"] == "awaiting_approval", val
    assert {r["status"] for r in val["results"]} == {"pass"} and not val["failing_checks"]
    assert [h["to_status"] for h in val["history"]] == [
        "checks_running",
        "checks_passed",
        "awaiting_approval",
    ]
    queue = (await client.get("/v1/validations?status=awaiting_approval")).json()["items"]
    assert [v["id"] for v in queue] == [val["id"]]
    # the submitter (local admin) cannot approve; an editor without the capability neither
    own = await client.post(f"/v1/validations/{val['id']}/approvals", json={"decision": "approve"})
    assert own.status_code == 403 and own.json()["code"] == "SELF_APPROVAL_FORBIDDEN"
    plain = await editor.post(
        f"/v1/validations/{val['id']}/approvals", json={"decision": "approve"}
    )
    assert plain.status_code == 403
    ok = await reviewer.post(
        f"/v1/validations/{val['id']}/approvals",
        json={"decision": "approve", "comment": "Evidence reviewed"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "validated" and len(ok.json()["approvals"]) == 1
    twice = await reviewer.post(
        f"/v1/validations/{val['id']}/approvals", json={"decision": "approve"}
    )
    assert twice.status_code == 409
    r = await client.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    assert r.status_code == 200, r.text
    # the evidence pack
    pack = (
        await client.post(f"/v1/validations/{val['id']}/evidence-pack", json={"format": "json"})
    ).json()
    assert pack["format"] == "mapi.evidence-pack/v1" and pack["digest"].startswith("sha256:")
    assert pack["model_ref"] == "model://default/trend@v1" and pack["download"] is None
    assert {p["name"] for p in pack["policies"]} >= {STRICT["name"]}
    html_pack = (await client.post(f"/v1/validations/{val['id']}/evidence-pack", json={})).json()
    assert html_pack["digest"] == pack["digest"]
    link = html_pack["download"]
    assert link["name"] == f"evidence-{val['id']}.html" and link["url"]
    async with ctx.db.session() as s:
        actions = [r.action for r in (await s.execute(select(m.AuditEvent))).scalars()]
    assert {
        "model.validation_request",
        "model.validation_transition",
        "model.approve",
        "model.evidence_export",
        "endpoint.promote_not_validated",
    } <= set(actions)
    # deprecation needs a reason; then promotion is blocked again
    assert (await client.post(f"/v1/validations/{val['id']}:deprecate", json={})).status_code == 422
    dep = await client.post(
        f"/v1/validations/{val['id']}:deprecate", json={"note": "Superseded by v2"}
    )
    assert dep.json()["status"] == "deprecated"


async def test_the_evidence_html_is_self_contained(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server.services.governance import evidence_pack, evidence_pack_html, load_validation
    from mapi_server.services.runs import Scope

    mv = await fitted_version(ctx)
    assert (await client.post(POL, json={**STRICT, "require_approval": False})).status_code == 201
    val = (await client.post(f"/v1/models/{mv.model_id}/versions/1/validations", json={})).json()
    await _finish_governance(ctx, val["workflow_run_id"], fail="convergence")
    val = (await client.get(f"/v1/validations/{val['id']}")).json()
    assert val["status"] == "checks_failed" and val["failing_checks"] == ["convergence"]
    async with ctx.db.session() as s:
        v = await load_validation(s, Scope("default", "default"), val["id"])
        pack = await evidence_pack(s, ctx, v)
        v.results = [{**v.results[0], "message_plain": "<script>alert(1)</script>"}, *v.results[1:]]
        pack2 = await evidence_pack(s, ctx, v)
    page = evidence_pack_html(pack2)
    assert "<script" not in page and "&lt;script&gt;" in page
    assert "http://" not in page and "https://" not in page and "src=" not in page
    assert "default-src 'none'" in page and pack.digest and pack.digest in evidence_pack_html(pack)
    evidence = (await client.get(f"/v1/validations/{val['id']}/evidence")).json()["items"]
    assert any(e["status"] == "fail" and e["suggested_action"] for e in evidence)


async def test_a_failed_run_fails_the_checks_and_resubmission_starts_a_new_round(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    val = (await client.post(f"/v1/models/{mv.model_id}/versions/1/validations", json={})).json()
    await _crash_governance(ctx, val["workflow_run_id"])
    val = (await client.get(f"/v1/validations/{val['id']}")).json()
    assert val["status"] == "checks_failed"
    assert val["results"] and {r["status"] for r in val["results"]} == {"error"}
    assert "boom" in val["results"][0]["message_plain"]
    first_run = val["workflow_run_id"]
    r = await client.post(f"/v1/validations/{val['id']}:resubmit", json={"note": "fixed"})
    assert r.status_code == 202 and r.json()["status"] == "checks_running"
    assert r.json()["workflow_run_id"] != first_run
    c = await client.post(f"/v1/validations/{val['id']}:cancel", json={})
    assert c.status_code == 200 and c.json()["status"] == "draft"
    run = (await client.get(f"/v1/runs/{r.json()['workflow_run_id']}")).json()
    assert run["state"] in ("cancelling", "cancelled")
    listed = (await client.get(f"/v1/models/{mv.model_id}/versions/1/validations")).json()
    assert [v["id"] for v in listed["items"]] == [val["id"]]


async def test_tokens_cannot_approve_and_tenants_are_isolated(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    assert (await client.post(POL, json=STRICT)).status_code == 201
    val = (await client.post(f"/v1/models/{mv.model_id}/versions/1/validations", json={})).json()
    await _finish_governance(ctx, val["workflow_run_id"])
    boss = await make_user(ctx, "boss@default.test")
    await grant_role(ctx, boss, "default", "default", "admin")
    secret = await make_token(ctx, boss, "default", "default", ["admin"])
    r = await client.post(
        f"/v1/validations/{val['id']}/approvals",
        json={"decision": "approve"},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert r.status_code == 403
    # another tenant sees none of it, through the API or row-level security
    await make_tenant(ctx, "beta", projects=("main",))
    async with ctx.db.tenant_session("beta") as s:
        assert (await s.execute(select(m.ModelValidation))).scalars().all() == []
        assert (await s.execute(select(m.GovernancePolicyRow))).scalars().all() == []
        assert (await s.execute(select(m.ValidationApproval))).scalars().all() == []
    async with ctx.db.tenant_session("default") as s:
        assert len((await s.execute(select(m.ModelValidation))).scalars().all()) == 1
        with pytest.raises(Exception, match=r"row-level security|violates"):
            await s.execute(
                text(
                    "INSERT INTO model_validations (id, tenant_id, project_id, model_id, "
                    "model_version_id, version, status, round, policies, required_checks, "
                    "results, failing_checks, require_approval, approvals_required, history) "
                    "VALUES (:id, 'beta', 'main', 'x', :mv, 1, 'draft', 1, '[]', '[]', '[]', "
                    "'[]', false, 1, '[]')"
                ),
                {"id": new_id("val"), "mv": mv.id},
            )


async def test_webhooks_announce_validation_steps(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    async with ctx.db.session() as s:
        s.add(
            m.Webhook(
                id=new_id("wh"),
                tenant_id="default",
                project_id="default",
                url="https://hooks.example.test/x",
                events=["model.validation.checks_passed", "model.validated"],
                active=True,
                secret_sealed="x",
                secret_hint="x",
                failure_count=0,
            )
        )
    assert (await client.post(POL, json={**STRICT, "require_approval": False})).status_code == 201
    val = (await client.post(f"/v1/models/{mv.model_id}/versions/1/validations", json={})).json()
    await _finish_governance(ctx, val["workflow_run_id"])
    async with ctx.db.session() as s:
        types = [e.type for e in (await s.execute(select(m.WebhookEvent))).scalars()]
    assert types == ["model.validation.checks_passed", "model.validated"]
    async with ctx.db.session() as s:
        envelope = (await s.execute(select(m.WebhookEvent))).scalars().first()
        assert envelope is not None and envelope.envelope["data"]["validation_id"] == val["id"]
        assert json.dumps(envelope.envelope).count("model_version_id") == 1
