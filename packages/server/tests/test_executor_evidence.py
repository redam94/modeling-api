"""The shared-tier gate (plan §8.4): verified and public tenants are placed only on an
executor that passed the executor conformance suite and the security suite for the caps it
currently reports, with neither result expired. Trusted tiers are not gated."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.runs import load_run, now
from mapi_server_testkit.builders import TREND_SPEC, ready_dataset
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.identity import make_tenant
from mapi_spec.admin import NETWORK_PROBES
from mapi_spec.api import new_id
from mapi_spec.conformance_executor import ConformanceTarget, applicable_cases
from mapi_spec.executor import ExecutorCaps, Isolation
from mapi_spec.resources import ResourceClass
from mapi_spec.run import RunState
from sqlalchemy import func, select, update

pytestmark = pytest.mark.integration


@dataclass
class SandboxedExecutor(FakeExecutor):
    """A fake whose caps would allow shared tiers (what cloudrun-jobs declares)."""

    isolation: Isolation = Isolation.MICROVM
    max_tasks: int = 1
    dns: bool = True

    def caps(self) -> ExecutorCaps:
        return ExecutorCaps(
            isolation=self.isolation,
            egress_enforced=True,
            dns_enforced=self.dns,
            classes=[ResourceClass.S, ResourceClass.M, ResourceClass.L],
            max_tasks=self.max_tasks,
        )


async def _queue(ctx: ServerContext, tenant: str) -> str:
    ts = now()
    run_id = new_id("run")
    async with ctx.db.session() as s:
        s.add(
            m.Run(
                id=run_id, tenant_id=tenant, project_id="main", kind="fit", state="queued",
                attempt=1, resource_class="S", timeout_s=600, labels={}, payload={},
                inputs={}, checkpointable=False, infra_retries=0, finalize_attempts=0,
                finalize={}, last_event_id=0, created_at=ts, updated_at=ts, queued_at=ts,
                priority="batch", task_count=1, untrusted_code=False,
            )
        )  # fmt: skip
    return run_id


async def _state(ctx: ServerContext, run_id: str) -> str:
    async with ctx.db.session() as s:
        return (await load_run(s, None, run_id)).state


def _target(ex: FakeExecutor) -> dict[str, Any]:
    return {"executor": ex.name, "caps": ex.caps().model_dump(mode="json")}


def _conformance(ex: FakeExecutor, *, at: datetime, passed: bool = True) -> dict[str, Any]:
    target = ConformanceTarget.model_validate(_target(ex))
    cases = applicable_cases(target)
    return {
        "executor": ex.name,
        "environment": "nightly",
        "started_at": at.isoformat(),
        "results": [
            {"case_id": c.id.value, "outcome": "passed" if passed or i else "failed"}
            for i, c in enumerate(cases)
        ],
    }


def _security(ex: FakeExecutor, *, at: datetime, dns: str = "blocked") -> dict[str, Any]:
    rows = [{"probe_id": p, "category": "isolation", "outcome": "blocked"} for p in NETWORK_PROBES]
    rows = [r if r["probe_id"] != "dns_egress" else {**r, "outcome": dns} for r in rows]
    return {
        "executor": ex.name,
        "isolation": ex.caps().isolation.name.lower(),
        "ok": True,
        "unexpected": [],
        "rows": rows,
        "started_at": at.isoformat(),
    }


async def _put(client: httpx.AsyncClient, ex: FakeExecutor, **reports: Any) -> dict[str, Any]:
    r = await client.put(
        f"/v1/admin/executors/{ex.name}/evidence", json={"target": _target(ex), **reports}
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def _health(client: httpx.AsyncClient, ex: FakeExecutor) -> dict[str, Any]:
    r = await client.get(f"/v1/admin/executors/{ex.name}")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_shared_tiers_wait_for_passing_evidence_for_the_current_caps(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await make_tenant(ctx, "pub", tier="public", projects=("main",))
    await make_tenant(ctx, "ops", tier="internal", projects=("main",))
    ex = SandboxedExecutor()
    dispatcher = Dispatcher(ctx, ex)
    pub = await _queue(ctx, "pub")
    ops = await _queue(ctx, "ops")

    # no evidence: the trusted tier runs, the public run waits (it is not failed)
    assert [r for r, _ in await dispatcher.lease()] == [ops]
    assert await _state(ctx, pub) == RunState.QUEUED.value
    await dispatcher.report()
    health = await _health(client, ex)
    # ``enabled`` is the operators' switch; missing evidence only holds the shared tiers
    assert health["enabled"] is True and health["status"] != "disabled"
    assert health["shared_tiers_enabled"] is False and health["conformance_passed"] is False
    assert "no executor conformance or security-suite report" in health["message"]

    t = now()
    # a failing conformance report, then a passing one without the security suite: held
    body = await _put(client, ex, conformance=_conformance(ex, at=t, passed=False))
    assert body["conformance_attestation"]["passed"] is False
    assert await dispatcher.lease() == []
    await _put(client, ex, conformance=_conformance(ex, at=t))
    assert await dispatcher.lease() == []
    # a security run whose DNS probe was skipped is no evidence of DNS enforcement
    body = await _put(client, ex, security=_security(ex, at=t, dns="skipped"))
    assert body["security_attestation"]["passed"] is False
    assert await dispatcher.lease() == []

    await _put(client, ex, security=_security(ex, at=t))
    assert [r for r, _ in await dispatcher.lease()] == [pub]
    await dispatcher.report()
    health = await _health(client, ex)
    assert health["enabled"] is True and health["shared_tiers_enabled"] is True
    assert health["conformance_passed"] is True and health["security_passed"] is True
    assert health["conformance"]["executor"] == ex.name

    # the evidence was measured for other caps than the executor now reports: held again
    later = await _queue(ctx, "pub")
    ex.max_tasks = 100
    assert await dispatcher.lease() == []
    await dispatcher.report()
    assert (await _health(client, ex))["shared_tiers_enabled"] is False
    ex.max_tasks = 1

    # expired evidence (older than MAPI_EXECUTOR_EVIDENCE_MAX_AGE_S): held
    old = t - timedelta(seconds=ctx.settings.executor_evidence_max_age_s + 60)
    await _put(client, ex, conformance=_conformance(ex, at=old))
    assert await dispatcher.lease() == []
    await _put(client, ex, conformance=_conformance(ex, at=t))
    assert [r for r, _ in await dispatcher.lease()] == [later]

    async with ctx.db.session() as s:
        actions = [
            e.action
            for e in (await s.execute(select(m.AuditEvent))).scalars()
            if e.action == "executor.evidence_update"
        ]
    assert len(actions) == 6


async def test_evidence_must_name_the_executor(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ex = SandboxedExecutor()
    t = now()
    r = await client.put(
        "/v1/admin/executors/other/evidence",
        json={"target": _target(ex), "conformance": _conformance(ex, at=t)},
    )
    assert r.status_code == 422
    r = await client.put(
        f"/v1/admin/executors/{ex.name}/evidence",
        json={"target": _target(ex), "security": {**_security(ex, at=t), "executor": "x"}},
    )
    assert r.status_code == 422
    assert (await client.get(f"/v1/admin/executors/{ex.name}/evidence")).status_code == 404


async def test_runs_nobody_can_place_are_refused_at_submission(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    """A verified tenant (the default tier) on a stack whose executor cannot take it gets a
    clear refusal when submitting, instead of a run that fails as INFRA_ERROR at lease."""
    await ready_dataset(ctx)
    r = await client.post("/v1/specs", json={"yaml": TREND_SPEC})
    assert r.status_code == 201, r.text
    fit = {"kind": "fit", "spec_id": r.json()["id"], "revision": 1}
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Tenant).where(m.Tenant.id == "default").values(trust_tier="verified")
        )

    async def runs() -> int:
        async with ctx.db.session() as s:
            return int((await s.execute(select(func.count()).select_from(m.Run))).scalar_one())

    # no executor has reported yet: accepted (the dispatcher decides at lease)
    assert (await client.post("/v1/runs", json=fit)).status_code == 201
    # cloudrun-jobs as Terraform deploys it: DNS enforcement not verified
    deployed = SandboxedExecutor(name="cloudrun-jobs", dns=False)
    await Dispatcher(ctx, deployed).report()
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 503 and r.json()["code"] == "INFRA_ERROR", r.text
    assert "verified" in r.json()["detail"] and "DNS" in r.json()["detail"]
    assert await runs() == 1
    # DNS verified, but no suite evidence recorded yet: still refused
    verified = SandboxedExecutor(name="cloudrun-jobs")
    await Dispatcher(ctx, verified).report()
    r = await client.post("/v1/runs", json=fit)
    assert r.status_code == 503 and "report" in r.json()["detail"]
    t = now()
    await _put(
        client,
        verified,
        conformance=_conformance(verified, at=t),
        security=_security(verified, at=t),
    )
    assert (await client.post("/v1/runs", json=fit)).status_code == 201
    assert await runs() == 2
