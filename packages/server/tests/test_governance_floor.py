"""Governance cannot be narrowed below the policies in force (M3.5 review).

A validation is always held to every policy in force for the model (tenant floor and the
project's own); ``policy_ids`` only adds policies. The promotion gate accepts a validated
version only when that validation covered every in-force policy that gates promotion, at
its current version. Concurrent validation requests for one version create one validation.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.governance import workflow_validation_submitter
from mapi_server_testkit.governance import finish_governance as _finish_governance
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_server_testkit.integrations import fitted_version
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    SYSTEM_ACTOR,
    CheckResult,
    CheckSeverity,
    CheckStatus,
)
from sqlalchemy import select, update

pytestmark = pytest.mark.integration

POL = "/v1/governance/policies"
TENANT_POL = "/v1/tenants/default/governance/policies"
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
NOAPPROVAL = {**STRICT, "require_approval": False}


def _vurl(mv: m.ModelVersion) -> str:
    return f"/v1/models/{mv.model_id}/versions/1/validations"


async def _promote(client: httpx.AsyncClient) -> httpx.Response:
    return await client.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})


async def _default_named(client: httpx.AsyncClient, family: str) -> dict[str, object]:
    items = (await client.get(TENANT_POL)).json()["items"]
    return next(p for p in items if family in p["applies_to"]["families"])


async def test_naming_a_lenient_policy_keeps_the_tenant_floor(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    strict = await client.post(TENANT_POL, json=STRICT)
    assert strict.status_code == 201, strict.text
    sid = strict.json()["id"]
    assert (await _promote(client)).json()["code"] == "NOT_VALIDATED"
    bob = await make_user(ctx, "bob@default.test")
    await grant_role(ctx, bob, "default", "default", "runner")
    runner = await session_caller(ctx, client, bob, tenant_id="default", project_id="default")
    # the seeded regression default does not even apply to a ts model
    reg = await _default_named(client, "tabular")
    r = await runner.post(_vurl(mv), json={"policy_ids": [reg["id"]]})
    assert r.status_code == 202, r.text
    val = r.json()
    assert f"{sid}@1" in val["policies"] and f"{reg['id']}@1" in val["policies"]
    assert val["require_approval"] is True
    checks = {c["check"] for c in val["required_checks"]}
    assert {"null.permutation_target", "convergence"} <= checks
    await _finish_governance(ctx, val["workflow_run_id"])
    val = (await client.get(f"/v1/validations/{val['id']}")).json()
    assert val["status"] == "awaiting_approval"
    r = await _promote(client)
    assert r.status_code == 409 and r.json()["code"] == "NOT_VALIDATED", r.text


async def test_the_gate_wants_the_validation_to_cover_the_policies_in_force(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A validated version whose validation was held to other policies (validated before a
    stricter project policy existed) does not pass a gate that policy sets."""
    mv = await fitted_version(ctx)
    r = await client.post(_vurl(mv), json={})
    assert r.status_code == 202, r.text
    val = r.json()
    await _finish_governance(ctx, val["workflow_run_id"])
    assert (await client.get(f"/v1/validations/{val['id']}")).json()["status"] == "validated"
    assert (await client.post(POL, json=NOAPPROVAL)).status_code == 201
    r = await _promote(client)
    assert r.status_code == 409 and r.json()["code"] == "NOT_VALIDATED", r.text
    assert "older policies" in r.text
    # a stale validated validation does not stop a new request under the policies now
    again = await client.post(_vurl(mv), json={})
    assert again.status_code == 202, again.text
    await _finish_governance(ctx, again.json()["workflow_run_id"])
    assert (await _promote(client)).status_code == 200


async def test_a_policy_made_stricter_after_validation_blocks_promotion(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    pid = (await client.post(POL, json=NOAPPROVAL)).json()["id"]
    val = (await client.post(_vurl(mv), json={})).json()
    await _finish_governance(ctx, val["workflow_run_id"])
    assert (await client.get(f"/v1/validations/{val['id']}")).json()["status"] == "validated"
    # a cosmetic change (same checks, same approval) keeps the validation good
    cosmetic = {**NOAPPROVAL, "description": "reworded", "expected_version": 1}
    assert (await client.put(f"{POL}/{pid}", json=cosmetic)).json()["version"] == 2
    assert (await _promote(client)).status_code == 200
    stricter = {
        **NOAPPROVAL,
        "checks": [*NOAPPROVAL["checks"], {"check": "null.placebo_time_shift"}],
        "expected_version": 2,
    }
    up = await client.put(f"{POL}/{pid}", json=stricter)
    assert up.status_code == 200 and up.json()["version"] == 3, up.text
    r = await client.put("/v1/endpoints/trend-other", json={"model": "model://default/trend@v1"})
    assert r.status_code == 409 and r.json()["code"] == "NOT_VALIDATED", r.text


async def test_a_policy_that_now_wants_a_reviewer_blocks_an_unreviewed_validation(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    pid = (await client.post(POL, json=NOAPPROVAL)).json()["id"]
    val = (await client.post(_vurl(mv), json={})).json()
    await _finish_governance(ctx, val["workflow_run_id"])
    reviewed = {**NOAPPROVAL, "require_approval": True, "expected_version": 1}
    assert (await client.put(f"{POL}/{pid}", json=reviewed)).status_code == 200
    r = await _promote(client)
    assert r.status_code == 409 and r.json()["code"] == "NOT_VALIDATED", r.text


async def test_naming_extra_policies_adds_their_checks(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """``policy_ids`` holds a model to more policies (even one for another family)."""
    mv = await fitted_version(ctx)
    eve = await make_user(ctx, "eve@default.test")
    await grant_role(ctx, eve, "default", "default", "runner")
    runner = await session_caller(ctx, client, eve, tenant_id="default", project_id="default")
    extra = await client.post(
        POL,
        json={
            "name": "Tabular extras",
            "applies_to": {"families": ["tabular"]},
            "checks": [{"check": "null.noise_regressors"}],
            "require_approval": False,
        },
    )
    assert extra.status_code == 201, extra.text
    r = await runner.post(_vurl(mv), json={"policy_ids": [extra.json()["id"]]})
    assert r.status_code == 202, r.text
    checks = {c["check"] for c in r.json()["required_checks"]}
    assert "null.noise_regressors" in checks and "convergence" in checks


async def test_concurrent_validation_requests_make_one_validation(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    assert (await client.post(POL, json=NOAPPROVAL)).status_code == 201
    a, b = await asyncio.gather(client.post(_vurl(mv), json={}), client.post(_vurl(mv), json={}))
    assert sorted([a.status_code, b.status_code]) == [202, 409], (a.text, b.text)
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.ModelValidation).where(m.ModelValidation.model_version_id == mv.id)
                )
            ).scalars()
        )
        runs = list((await s.execute(select(m.Run).where(m.Run.kind == "governance"))).scalars())
    assert len(rows) == 1 and len(runs) == 1


async def test_submitted_evidence_is_held_to_every_policy_in_force(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """Evidence naming one policy, or missing a required check, does not validate."""
    mv = await fitted_version(ctx)
    strict = await client.post(
        POL,
        json={
            "name": "strict",
            "checks": [{"check": "convergence"}, {"check": "null.permutation_target"}],
            "require_validated_for_promotion": True,
        },
    )
    lenient = await client.post(POL, json={"name": "lenient", "checks": [{"check": "convergence"}]})
    assert strict.status_code == 201 and lenient.status_code == 201
    passed = CheckResult(
        check="convergence",
        status=CheckStatus.PASS,
        severity=CheckSeverity.BLOCKING,
        metric=GOVERNANCE_CHECKS["convergence"].metric,
        message_plain="converged",
    ).model_dump(mode="json")
    evidence = {"results": [passed], "policies": [f"{lenient.json()['id']}@1"]}
    async with ctx.db.session() as s:
        vid = await workflow_validation_submitter(s, ctx, mv, evidence, "run_nope")
    assert vid is not None
    val = (await client.get(f"/v1/validations/{vid}")).json()
    assert f"{strict.json()['id']}@1" in val["policies"]
    assert val["status"] == "checks_failed" and "null.permutation_target" in val["failing_checks"]


async def test_a_validation_no_person_submitted_cannot_be_approved(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    from mapi_server.db import models as m

    mv = await fitted_version(ctx)
    rita = await make_user(ctx, "rita@default.test")
    await grant_role(ctx, rita, "default", "default", "editor")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == rita.id)
            .values(capabilities=["reviewer"])
        )
    reviewer = await session_caller(ctx, client, rita, tenant_id="default", project_id="default")
    r = await client.post(
        POL,
        json={"name": "reviewed", "checks": [{"check": "convergence"}], "require_approval": True},
    )
    assert r.status_code == 201, r.text
    results = [
        CheckResult(
            check=c,
            status=CheckStatus.PASS,
            severity=CheckSeverity.BLOCKING,
            metric=GOVERNANCE_CHECKS[c].metric,
            message_plain="fine",
        ).model_dump(mode="json")
        for c in GOVERNANCE_CHECKS
    ]
    async with ctx.db.session() as s:
        vid = await workflow_validation_submitter(s, ctx, mv, {"results": results}, "run_gone")
    val = (await client.get(f"/v1/validations/{vid}")).json()
    assert val["status"] == "awaiting_approval" and val["submitted_by"] == SYSTEM_ACTOR
    ok = await reviewer.post(f"/v1/validations/{vid}/approvals", json={"decision": "approve"})
    assert ok.status_code == 403, ok.text
