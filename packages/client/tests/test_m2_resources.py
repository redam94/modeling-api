"""v0.3 resources (identity, tokens, usage, operators) and project selection."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from mapi_client import AsyncClient, Client, ClientConfig, ConfigError
from mapi_spec.api import (
    ApiToken,
    AuditEvent,
    ExecutorHealth,
    KillSwitches,
    Me,
    Membership,
    Project,
    QuotaPolicy,
    Run,
    Tenant,
    TokenCreated,
    UsageReport,
)
from mapi_spec.errors import ErrorCode, MapiError, Problem

BASE = "http://api.test"
V1 = f"{BASE}/v1"
HEADER = "X-Mapi-Project"
Ex = Callable[..., dict[str, Any]]


def page(items: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": cursor}


@pytest.fixture
def scoped(api: respx.MockRouter) -> Iterator[Client]:
    cfg = ClientConfig(base_url=BASE, project="acme/growth", max_retries=0)
    with Client(config=cfg, sleep=lambda s: None) as c:
        yield c


def test_project_header_on_project_routes_only(
    scoped: Client, api: respx.MockRouter, example: Ex
) -> None:
    runs = api.get(f"{V1}/runs/run_1").respond(200, json=example(Run))
    catalog = api.get(f"{V1}/tenants/acme").respond(200, json=example(Tenant))
    me = api.get(f"{V1}/me").respond(200, json=example(Me))
    scoped.runs.get("run_1")
    scoped.tenants.get("acme")
    scoped.me()
    assert runs.calls.last.request.headers[HEADER] == "acme/growth"
    assert HEADER not in catalog.calls.last.request.headers
    assert HEADER not in me.calls.last.request.headers


def test_use_switches_project(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    route = api.get(f"{V1}/runs/run_1").respond(200, json=example(Run))
    scoped.use("acme/retail")
    scoped.runs.get("run_1")
    assert route.calls.last.request.headers[HEADER] == "acme/retail"
    assert "acme/retail" in repr(scoped)
    scoped.use(None)
    scoped.runs.get("run_1")
    assert HEADER not in route.calls.last.request.headers
    with pytest.raises(ConfigError, match="invalid project"):
        scoped.use("Acme/Growth!")


def test_project_argument_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAPI_PROJECT", "acme/env")
    with Client() as c:
        assert c.project == "acme/env"
    with Client(project="acme/arg") as c:
        assert c.project == "acme/arg"
    with Client(config=ClientConfig(base_url=BASE), project="acme/x") as c:
        assert c.project == "acme/x"


def test_sse_stream_carries_the_project(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    from mapi_spec.api import encode_sse

    run = Run.model_validate(example(Run, state="succeeded", finished_at="2026-10-15T10:00:00Z"))
    route = api.get(f"{V1}/runs/{run.id}/events").respond(
        200,
        headers={"content-type": "text/event-stream"},
        content=(encode_sse("snapshot", run) + encode_sse("end", run)).encode(),
    )
    items = list(scoped.runs.stream_events(run.id))
    assert len(items) == 2
    assert route.calls.last.request.headers[HEADER] == "acme/growth"


def test_me_tenants_and_projects(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/me").respond(200, json=example(Me))
    me = scoped.me()
    assert me.tenants[0].projects[0].project_id == "growth"
    api.get(f"{V1}/tenants").respond(200, json=page([example(Tenant)]))
    assert [t.id for t in scoped.tenants.list()] == ["acme"]
    patch = api.patch(f"{V1}/tenants/acme").respond(200, json=example(Tenant, name="Acme 2"))
    assert scoped.tenants.rename("acme", "Acme 2").name == "Acme 2"
    assert json.loads(patch.calls.last.request.content) == {"name": "Acme 2"}
    api.get(f"{V1}/tenants/acme/projects").respond(200, json=page([example(Project)]))
    assert len(list(scoped.tenants.projects("acme"))) == 1
    create = api.post(f"{V1}/tenants/acme/projects").respond(201, json=example(Project))
    scoped.tenants.create_project("acme", "growth", "Growth", description="EU")
    assert json.loads(create.calls.last.request.content) == {
        "id": "growth",
        "name": "Growth",
        "description": "EU",
    }
    upd = api.patch(f"{V1}/tenants/acme/projects/growth").respond(200, json=example(Project))
    scoped.tenants.update_project("acme", "growth", description="UK too")
    assert json.loads(upd.calls.last.request.content) == {"description": "UK too"}
    with pytest.raises(ValueError):
        scoped.tenants.update_project("acme", "growth")
    gone = api.delete(f"{V1}/tenants/acme/projects/growth").respond(204)
    assert scoped.tenants.delete_project("acme", "growth") is None
    assert gone.called


def test_members(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    usr = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
    api.get(f"{V1}/tenants/acme/projects/growth/members").respond(
        200, json=page([example(Membership)])
    )
    assert next(iter(scoped.tenants.members("acme", "growth"))).role.value == "runner"
    add = api.post(f"{V1}/tenants/acme/members").respond(201, json=example(Membership))
    scoped.tenants.add_member("acme", "admin", email="it@acme.example")
    assert json.loads(add.calls.last.request.content) == {
        "email": "it@acme.example",
        "role": "admin",
    }
    with pytest.raises(ValueError):
        scoped.tenants.add_member("acme", "admin")  # neither principal nor email
    role = api.patch(f"{V1}/tenants/acme/projects/growth/members/{usr}").respond(
        200, json=example(Membership)
    )
    scoped.tenants.set_role("acme", usr, "editor", project_id="growth")
    assert json.loads(role.calls.last.request.content) == {"role": "editor"}
    # M3.5: the reviewer capability on top of a role (granted, then taken away)
    scoped.tenants.set_capabilities("acme", usr, ["reviewer"], project_id="growth")
    assert json.loads(role.calls.last.request.content) == {"capabilities": ["reviewer"]}
    scoped.tenants.set_capabilities("acme", usr, [], project_id="growth")
    assert json.loads(role.calls.last.request.content) == {"capabilities": []}
    with pytest.raises(ValueError):
        scoped.tenants.set_capabilities("acme", usr, ["approver"])
    scoped.tenants.add_member("acme", "editor", principal_id=usr, capabilities=["reviewer"])
    assert json.loads(add.calls.last.request.content) == {
        "principal_id": usr,
        "role": "editor",
        "capabilities": ["reviewer"],
    }
    rm = api.delete(f"{V1}/tenants/acme/members/{usr}").respond(204)
    scoped.tenants.remove_member("acme", usr)
    assert rm.called


def test_tokens(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    created = example(TokenCreated)
    mint = api.post(f"{V1}/tokens").respond(201, json=created)
    result = scoped.tokens.create("ci", ["models:read"], expires_in_days=7)
    assert result.secret == created["secret"]
    assert created["secret"] not in repr(result)
    assert json.loads(mint.calls.last.request.content) == {
        "name": "ci",
        "scopes": ["models:read"],
        "expires_in_days": 7,
        "budget_period": "month",
    }
    assert mint.calls.last.request.headers[HEADER] == "acme/growth"
    with pytest.raises(ValueError):
        scoped.tokens.create("ci", ["models:write"])  # unknown scope
    listing = api.get(f"{V1}/tokens").respond(200, json=page([example(ApiToken)]))
    tokens = list(scoped.tokens.list(all_owners=True, status="active"))
    assert tokens[0].display.startswith("mapi_")
    assert listing.calls.last.request.url.params["all"] == "true"
    api.get(f"{V1}/tokens/tok_1").respond(200, json=example(ApiToken))
    assert scoped.tokens.get("tok_1").name == "budget-planner"
    revoke = api.delete(f"{V1}/tokens/tok_1").respond(204)
    scoped.tokens.revoke("tok_1")
    assert revoke.calls.last.request.headers[HEADER] == "acme/growth"


def test_token_mint_is_not_resent_after_a_gateway_error(api: respx.MockRouter, example: Ex) -> None:
    with Client(config=ClientConfig(base_url=BASE), sleep=lambda s: None) as c:
        route = api.post(f"{V1}/tokens").respond(503)
        with pytest.raises(MapiError):
            c.tokens.create("ci", ["models:read"])
    assert route.call_count == 1


def test_usage(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    proj = api.get(f"{V1}/usage").respond(200, json=example(UsageReport))
    report = scoped.usage.get(period="month", group_by="resource_class")
    assert report.totals.amount_micros == 93600
    assert proj.calls.last.request.headers[HEADER] == "acme/growth"
    tenant = api.get(f"{V1}/tenants/acme/usage").respond(200, json=example(UsageReport))
    scoped.usage.tenant("acme", period="day", start=datetime(2026, 10, 2, tzinfo=UTC))
    params = tenant.calls.last.request.url.params
    assert params["period"] == "day" and params["start"].startswith("2026-10-02")
    assert HEADER not in tenant.calls.last.request.headers


def test_audit(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    route = api.get(f"{V1}/tenants/acme/audit").respond(200, json=page([example(AuditEvent)]))
    events = list(scoped.tenants.audit("acme", action="token.create", outcome="success"))
    assert events[0].action.value == "token.create"
    assert route.calls.last.request.url.params["action"] == "token.create"


def test_admin(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/admin/tenants").respond(200, json=page([example(Tenant)]))
    assert len(list(scoped.admin.tenants(status="active"))) == 1
    api.get(f"{V1}/admin/tenants/acme").respond(200, json=example(Tenant))
    assert scoped.admin.tenant("acme").trust_tier.value == "verified"
    create = api.post(f"{V1}/admin/tenants").respond(201, json=example(Tenant))
    scoped.admin.create_tenant({"id": "acme", "name": "Acme"})
    assert json.loads(create.calls.last.request.content)["trust_tier"] == "verified"
    upd = api.patch(f"{V1}/admin/tenants/acme").respond(200, json=example(Tenant))
    scoped.admin.update_tenant("acme", {"status": "suspended", "status_reason": "overdue"})
    assert json.loads(upd.calls.last.request.content) == {
        "status": "suspended",
        "status_reason": "overdue",
    }
    with pytest.raises(ValueError, match="status_reason"):
        scoped.admin.update_tenant("acme", {"status": "suspended"})

    api.get(f"{V1}/admin/quotas").respond(200, json=page([example(QuotaPolicy)]))
    assert next(iter(scoped.admin.quotas(scope="tenant"))).subject_id == "acme"
    put = api.put(f"{V1}/admin/quotas").respond(200, json=example(QuotaPolicy))
    scoped.admin.set_quota("tenant", "acme", {"concurrent_runs": 40}, note="beta")
    assert json.loads(put.calls.last.request.content) == {
        "scope": "tenant",
        "subject_id": "acme",
        "limits": {"concurrent_runs": 40},
        "note": "beta",
    }
    rm = api.delete(f"{V1}/admin/quotas/token/tok_1").respond(204)
    scoped.admin.delete_quota("token", "tok_1")
    assert rm.called

    api.get(f"{V1}/admin/kill-switches").respond(200, json=example(KillSwitches))
    assert scoped.admin.kill_switches().untrusted_execution_enabled is False
    ks = api.put(f"{V1}/admin/kill-switches").respond(200, json=example(KillSwitches))
    scoped.admin.set_kill_switches({"untrusted_execution_enabled": False, "reason": "incident"})
    assert json.loads(ks.calls.last.request.content) == {
        "untrusted_execution_enabled": False,
        "clear_spend_cap": False,
        "cancel_untrusted_runs": False,
        "reason": "incident",
    }
    with pytest.raises(ValueError):
        scoped.admin.set_kill_switches({"untrusted_execution_enabled": True})  # no reason

    api.get(f"{V1}/admin/executors").respond(200, json=page([example(ExecutorHealth)]))
    assert next(iter(scoped.admin.executors())).name == "cloudrun-jobs"
    api.get(f"{V1}/admin/executors/cloudrun-jobs").respond(200, json=example(ExecutorHealth))
    assert scoped.admin.executor("cloudrun-jobs").enabled
    ex = api.put(f"{V1}/admin/executors/cloudrun-jobs").respond(200, json=example(ExecutorHealth))
    scoped.admin.set_executor("cloudrun-jobs", enabled=False, reason="incident 42")
    assert json.loads(ex.calls.last.request.content) == {"enabled": False, "reason": "incident 42"}
    assert "X-Mapi-Project" not in ex.calls.last.request.headers
    audit = api.get(f"{V1}/admin/audit").respond(200, json=page([example(AuditEvent)]))
    list(scoped.admin.audit(tenant_id="acme", since=datetime(2026, 10, 1, tzinfo=UTC)))
    assert audit.calls.last.request.url.params["tenant_id"] == "acme"
    for route in (create, upd, put, ks, audit):
        assert HEADER not in route.calls.last.request.headers


def test_forbidden_is_a_typed_error(scoped: Client, api: respx.MockRouter) -> None:
    body = Problem.from_code(ErrorCode.FORBIDDEN, "operators only").model_dump(mode="json")
    api.get(f"{V1}/admin/kill-switches").respond(403, json=body)
    with pytest.raises(MapiError) as err:
        scoped.admin.kill_switches()
    assert err.value.code is ErrorCode.FORBIDDEN


async def test_async_resources(api: respx.MockRouter, example: Ex) -> None:
    cfg = ClientConfig(base_url=BASE, project="acme/growth")
    async with AsyncClient(config=cfg) as c:
        api.get(f"{V1}/me").respond(200, json=example(Me))
        assert (await c.me()).auth == "session"
        mint = api.post(f"{V1}/tokens").respond(201, json=example(TokenCreated))
        created = await c.tokens.create("ci", ["runs:create"])
        assert created.token.id.startswith("tok_")
        assert mint.calls.last.request.headers[HEADER] == "acme/growth"
        api.get(f"{V1}/tokens").respond(200, json=page([example(ApiToken)]))
        assert len([t async for t in c.tokens.list()]) == 1
        api.delete(f"{V1}/tokens/tok_1").respond(204)
        await c.tokens.revoke("tok_1")
        api.get(f"{V1}/usage").respond(200, json=example(UsageReport))
        assert (await c.usage.get()).period == "month"
        api.get(f"{V1}/tenants/acme/usage").respond(200, json=example(UsageReport))
        assert (await c.usage.tenant("acme")).tenant_id == "acme"
        api.get(f"{V1}/tenants/acme/projects").respond(200, json=page([example(Project)]))
        assert len([p async for p in c.tenants.projects("acme")]) == 1
        api.put(f"{V1}/admin/kill-switches").respond(200, json=example(KillSwitches))
        await c.admin.set_kill_switches({"global_spend_cap_micros": 5, "reason": "cap"})
        api.get(f"{V1}/admin/quotas").respond(200, json=page([example(QuotaPolicy)]))
        assert len([q async for q in c.admin.quotas()]) == 1
        c.use("acme/retail")
        route = api.get(f"{V1}/runs/run_1").respond(200, json=example(Run))
        await c.runs.get("run_1")
        assert route.calls.last.request.headers[HEADER] == "acme/retail"
        api.get(f"{V1}/admin/executors/x").respond(
            404, json=Problem.from_code(ErrorCode.NOT_FOUND, "no").model_dump(mode="json")
        )
        with pytest.raises(MapiError):
            await c.admin.executor("x")


def test_responses_are_validated(scoped: Client, api: respx.MockRouter) -> None:
    api.get(f"{V1}/me").mock(return_value=httpx.Response(200, json={"auth": "magic"}))
    with pytest.raises(MapiError, match="not a valid Me"):
        scoped.me()
