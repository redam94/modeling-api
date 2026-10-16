"""Webhooks end to end: CRUD with a secret shown once (and sealed at rest), the outbox
written in the transaction of the change, signed deliveries to a local receiver, retries
with backoff and jitter, auto-disable, replays, secret rotation, the delivery log, the SSRF
guard at delivery time (DNS rebinding) and tenant isolation of every new table."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.webhooks.delivery import (
    DISABLED_GONE,
    InlineWebhookQueue,
    WebhookDeliverer,
    WebhookRelay,
)
from mapi_server.services.webhooks.outbox import emit_event, envelope_body
from mapi_server.services.webhooks.ssrf import StaticResolver
from mapi_server_testkit.integrations import finish_run, fitted_version, http_receiver
from mapi_spec.webhooks import (
    SIGNATURE_HEADER,
    WebhookCreated,
    WebhookEventType,
    parse_signature_header,
    verify_signature,
)
from sqlalchemy import func, select, text

pytestmark = pytest.mark.integration

PUBLIC = "93.184.216.34"


@pytest.fixture
def dev_ctx(ctx: ServerContext) -> ServerContext:
    """A context that may deliver to http://hooks.test (resolved to 127.0.0.1)."""
    ctx.settings = ctx.settings.model_copy(
        update={"webhook_allow_http": True, "webhook_dev_allowlist": ["hooks.test"]}
    )
    ctx.webhook_resolver = StaticResolver(
        {"hooks.test": ["127.0.0.1"], "hooks.example.com": [PUBLIC]}
    )
    return ctx


async def _create(client: httpx.AsyncClient, url: str, events: list[str]) -> WebhookCreated:
    r = await client.post("/v1/webhooks", json={"url": url, "events": events})
    assert r.status_code == 201, r.text
    return WebhookCreated.model_validate(r.json())


async def _deliveries(ctx: ServerContext) -> list[m.WebhookDelivery]:
    async with ctx.db.session() as s:
        rows = await s.execute(select(m.WebhookDelivery).order_by(m.WebhookDelivery.created_at))
        return list(rows.scalars())


async def _emit(ctx: ServerContext, event: WebhookEventType, **data: Any) -> str | None:
    async with ctx.db.session() as s:
        return await emit_event(
            s, tenant_id="default", project_id="default", event_type=event, data=data
        )


async def _audits(ctx: ServerContext, action: str) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        rows = await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == action))
        return list(rows.scalars())


async def test_crud_secret_shown_once_and_sealed(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    created = await _create(client, "https://hooks.example.com/mapi", ["run.succeeded"])
    hook = created.webhook
    assert created.secret.startswith("whsec_") and hook.secret_hint.endswith(created.secret[-4:])
    assert "secret" not in repr(created) or "<redacted>" in repr(created)
    got = (await client.get(f"/v1/webhooks/{hook.id}")).json()
    assert "secret" not in got and got["secret_hint"] == hook.secret_hint
    async with dev_ctx.db.session() as s:
        row = await s.get(m.Webhook, hook.id)
        assert row is not None and created.secret not in row.secret_sealed
        assert row.secret_sealed.startswith("v1.")
    page = (await client.get("/v1/webhooks")).json()
    assert [w["id"] for w in page["items"]] == [hook.id]
    r = await client.patch(f"/v1/webhooks/{hook.id}", json={"events": ["score.completed"]})
    assert r.status_code == 200 and r.json()["events"] == ["score.completed"]
    rot = await client.post(f"/v1/webhooks/{hook.id}:rotate-secret")
    assert rot.status_code == 200
    assert WebhookCreated.model_validate(rot.json()).secret != created.secret
    assert (await client.delete(f"/v1/webhooks/{hook.id}")).status_code == 204
    assert (await client.get(f"/v1/webhooks/{hook.id}")).status_code == 404
    actions = {a.action for a in await _audits(dev_ctx, "webhook.create")}
    assert actions == {"webhook.create"}
    for action in ("webhook.update", "webhook.secret_rotate", "webhook.delete"):
        assert await _audits(dev_ctx, action), action
    # each audit row names only the host, never the URL path or the secret
    for ev in await _audits(dev_ctx, "webhook.create"):
        assert ev.details["host"] == "hooks.example.com" and "mapi" not in str(ev.details)


@pytest.mark.parametrize(
    ("url", "answers"),
    [
        ("https://hooks.example.com/x", ["169.254.169.254"]),
        ("https://rebind.example.com/x", ["10.0.0.1"]),
        ("https://localhost/x", None),
        ("http://hooks.example.com/x", None),
        ("https://nowhere.example.com/x", None),
    ],
)
async def test_blocked_urls_are_refused_and_audited(
    client: httpx.AsyncClient, ctx: ServerContext, url: str, answers: list[str] | None
) -> None:
    ctx.webhook_resolver = StaticResolver(
        {"hooks.example.com": answers or [PUBLIC], "rebind.example.com": answers or [PUBLIC]}
    )
    r = await client.post("/v1/webhooks", json={"url": url, "events": ["run.failed"]})
    assert r.status_code == 422 and r.json()["code"] == "WEBHOOK_URL_BLOCKED", r.text
    denied = await _audits(ctx, "webhook.url_blocked")
    assert denied and denied[-1].outcome == "denied" and denied[-1].details["stage"] == "create"
    async with ctx.db.session() as s:
        assert (await s.execute(select(func.count()).select_from(m.Webhook))).scalar_one() == 0


async def test_updates_recheck_urls_and_projects_have_a_cap(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    hook = (await _create(client, "https://hooks.example.com/a", ["run.failed"])).webhook
    r = await client.patch(f"/v1/webhooks/{hook.id}", json={"url": "https://localhost/a"})
    assert r.status_code == 422 and r.json()["code"] == "WEBHOOK_URL_BLOCKED"
    dev_ctx.settings = dev_ctx.settings.model_copy(update={"webhook_max_per_project": 2})
    await _create(client, "https://hooks.example.com/b", ["run.failed"])
    r = await client.post(
        "/v1/webhooks", json={"url": "https://hooks.example.com/c", "events": ["run.failed"]}
    )
    assert r.status_code == 409


async def test_finishing_a_fit_writes_events_and_a_signed_delivery_arrives(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    async with http_receiver() as rec:
        created = await _create(
            client,
            f"http://hooks.test:{rec.port}/in",
            ["run.succeeded", "model.version.ready", "run.failed"],
        )
        mv = await fitted_version(dev_ctx)
        async with dev_ctx.db.session() as s:
            events = list(
                (await s.execute(select(m.WebhookEvent).order_by(m.WebhookEvent.id))).scalars()
            )
        types = [e.type for e in events]
        assert types == ["run.succeeded", "model.version.ready"]
        ready = events[1].envelope["data"]
        assert ready == {
            "model_id": mv.model_id,
            "model_version_id": mv.id,
            "ref": "model://default/trend@v1",
            "scorecard_overall": "pass",
        }
        assert events[0].envelope["data"]["model_version_id"] == mv.id
        deliveries = await _deliveries(dev_ctx)
        assert [d.status for d in deliveries] == ["pending", "pending"]
        deliverer = WebhookDeliverer(dev_ctx)
        for d in deliveries:
            assert (await deliverer.deliver(d.id)) == "succeeded"
        assert await deliverer.deliver(deliveries[0].id) is None  # already final
    assert len(rec.requests) == 2
    got = rec.requests[0]
    body = got.body
    assert verify_signature(body, got.headers["mapi-signature"], created.secret)
    assert got.headers["mapi-event-type"] == "run.succeeded"
    assert got.headers["mapi-event-id"] == events[0].id
    assert got.headers["mapi-webhook-id"] == created.webhook.id
    assert got.headers["mapi-delivery-id"] == deliveries[0].id
    assert got.headers["user-agent"] == "mapi-webhooks/1"
    assert body == envelope_body(events[0].envelope)
    log = (await client.get(f"/v1/webhooks/{created.webhook.id}/deliveries")).json()["items"]
    assert {d["status"] for d in log} == {"succeeded"}
    assert log[0]["response_code"] == 200 and log[0]["response_excerpt"] == '{"ok":true}'
    hook = (await client.get(f"/v1/webhooks/{created.webhook.id}")).json()
    assert hook["last_success_at"] and hook["failure_count"] == 0


async def test_promotion_and_endpoint_scores_emit_their_events(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    await _create(client, "https://hooks.example.com/x", ["endpoint.promoted", "score.completed"])
    await fitted_version(dev_ctx)
    r = await client.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    assert r.status_code == 200
    r = await client.post(
        "/v1/endpoints/trend-prod/score",
        json={"mode": "forecast", "horizon": {"dim": "t", "periods": 2}},
    )
    run = await finish_run(dev_ctx, r.json()["run"]["id"])
    assert run.state == "succeeded"
    async with dev_ctx.db.session() as s:
        events = list(
            (await s.execute(select(m.WebhookEvent).order_by(m.WebhookEvent.id))).scalars()
        )
    assert [e.type for e in events] == ["endpoint.promoted", "score.completed"]
    promoted = events[0].envelope["data"]
    assert promoted["alias"] == "trend-prod" and promoted["revision"] == 1
    assert promoted["ref"] == "endpoint://default/trend-prod" and not promoted["overridden"]
    scored = events[1].envelope["data"]
    assert scored["run_id"] == run.id and "result.json" in scored["outputs"]
    assert scored["endpoint"] == {
        "alias": "trend-prod", "ref": "endpoint://default/trend-prod", "revision": 1,
    }  # fmt: skip
    for env in (e.envelope for e in events):
        assert env["tenant"] == "default" and env["project"] == "default"
        assert "https://" not in str(env["data"])  # no signed URLs in envelopes


async def test_an_event_without_subscribers_stores_nothing(dev_ctx: ServerContext) -> None:
    assert await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_x") is None
    async with dev_ctx.db.session() as s:
        n = (await s.execute(select(func.count()).select_from(m.WebhookEvent))).scalar_one()
    assert n == 0


async def test_retries_back_off_with_jitter_then_give_up_and_disable(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    dev_ctx.settings = dev_ctx.settings.model_copy(
        update={"webhook_max_attempts": 3, "webhook_auto_disable_after": 2}
    )
    async with http_receiver() as rec:
        rec.default = (503, {}, b"busy")
        hook = (await _create(client, f"http://hooks.test:{rec.port}/in", ["run.failed"])).webhook
        await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_1")
        clock = [datetime.now(UTC) + timedelta(seconds=1)]
        deliverer = WebhookDeliverer(dev_ctx, jitter=lambda: 0.5, clock=lambda: clock[0])
        (d,) = await _deliveries(dev_ctx)
        assert await deliverer.deliver(d.id) == "retrying"
        (d,) = await _deliveries(dev_ctx)
        assert d.attempt == 2 and d.response_code == 503 and d.response_excerpt == "busy"
        assert d.next_attempt_at == clock[0] + timedelta(minutes=1) * 1.05  # 1 min + 5% jitter
        assert await deliverer.deliver(d.id) is None  # not due yet
        clock[0] = d.next_attempt_at
        assert await deliverer.deliver(d.id) == "retrying"
        (d,) = await _deliveries(dev_ctx)
        assert d.attempt == 3 and d.next_attempt_at == clock[0] + timedelta(minutes=5) * 1.05
        clock[0] = d.next_attempt_at
        assert await deliverer.deliver(d.id) == "failed"  # max attempts
        row = (await client.get(f"/v1/webhooks/{hook.id}")).json()
        assert row["failure_count"] == 1 and row["active"]
        # a second event that fails for good disables the webhook (audited)
        await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_2")
        clock[0] = datetime.now(UTC) + timedelta(seconds=1)
        d2 = (await _deliveries(dev_ctx))[-1]
        for _ in range(3):
            clock[0] = max(clock[0], d2.next_attempt_at or clock[0])
            await deliverer.deliver(d2.id)
            d2 = (await _deliveries(dev_ctx))[-1]
        assert d2.status == "failed"
    assert len(rec.requests) == 6
    row = (await client.get(f"/v1/webhooks/{hook.id}")).json()
    assert not row["active"] and row["failure_count"] == 2
    assert "2 events in a row" in row["disabled_reason"]
    (audit,) = await _audits(dev_ctx, "webhook.disabled")
    assert audit.actor_id == "svc_webhooks" and audit.details["reason"] == "failures"
    # events are no longer fanned out to it; re-activating resets the count
    assert await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_3") is None
    r = await client.patch(f"/v1/webhooks/{hook.id}", json={"active": True})
    assert r.json()["failure_count"] == 0 and r.json()["disabled_reason"] is None
    log = await client.get(f"/v1/webhooks/{hook.id}/deliveries", params={"status": "failed"})
    assert len(log.json()["items"]) == 2


async def test_410_gone_disables_at_once(client: httpx.AsyncClient, dev_ctx: ServerContext) -> None:
    async with http_receiver() as rec:
        rec.default = (410, {}, b"")
        hook = (await _create(client, f"http://hooks.test:{rec.port}/in", ["run.failed"])).webhook
        await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_1")
        (d,) = await _deliveries(dev_ctx)
        assert await WebhookDeliverer(dev_ctx).deliver(d.id) == "failed"
    row = (await client.get(f"/v1/webhooks/{hook.id}")).json()
    assert not row["active"] and row["disabled_reason"] == DISABLED_GONE


async def test_dns_rebinding_at_delivery_time_is_blocked_and_never_retried(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """The URL resolved to a public address when it was registered; at delivery the same
    name resolves to the metadata server. The delivery is refused without a connection."""
    resolver = StaticResolver({"hooks.example.com": [[PUBLIC], ["169.254.169.254"]]})
    ctx.webhook_resolver = resolver
    hook = (await _create(client, "https://hooks.example.com/in", ["run.failed"])).webhook
    await _emit(ctx, WebhookEventType.RUN_FAILED, run_id="run_1")
    (d,) = await _deliveries(ctx)
    assert await WebhookDeliverer(ctx).deliver(d.id) == "blocked"
    (d,) = await _deliveries(ctx)
    assert d.status == "blocked" and d.next_attempt_at is None and d.response_code is None
    assert "private" in (d.error or "")
    assert resolver.calls == ["hooks.example.com", "hooks.example.com"]
    (audit,) = await _audits(ctx, "webhook.url_blocked")
    assert audit.outcome == "denied" and audit.resource_id == hook.id
    row = (await client.get(f"/v1/webhooks/{hook.id}")).json()
    assert row["failure_count"] == 1


async def test_rotation_signs_with_both_secrets_for_a_day(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    async with http_receiver() as rec:
        created = await _create(client, f"http://hooks.test:{rec.port}/in", ["run.failed"])
        rotated = WebhookCreated.model_validate(
            (await client.post(f"/v1/webhooks/{created.webhook.id}:rotate-secret")).json()
        )
        await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_1")
        (d,) = await _deliveries(dev_ctx)
        assert await WebhookDeliverer(dev_ctx).deliver(d.id) == "succeeded"
        later = datetime.now(UTC) + timedelta(hours=25)
        await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_2")
        d2 = (await _deliveries(dev_ctx))[-1]
        assert await WebhookDeliverer(dev_ctx, clock=lambda: later).deliver(d2.id) == "succeeded"
    first, second = rec.requests
    _, sigs = parse_signature_header(first.headers["mapi-signature"])
    assert len(sigs) == 2
    for secret in (created.secret, rotated.secret):
        assert verify_signature(first.body, first.headers["mapi-signature"], secret)
    _, sigs = parse_signature_header(second.headers["mapi-signature"])
    assert len(sigs) == 1
    assert not verify_signature(
        second.body, second.headers["mapi-signature"], created.secret, now=int(later.timestamp())
    )


async def test_replay_and_the_relay(client: httpx.AsyncClient, dev_ctx: ServerContext) -> None:
    async with http_receiver() as rec:
        hook = (await _create(client, f"http://hooks.test:{rec.port}/in", ["run.failed"])).webhook
        event_id = await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_1")
        deliverer = WebhookDeliverer(dev_ctx)
        queue = InlineWebhookQueue(deliverer)
        relay = WebhookRelay(dev_ctx, queue)
        assert await relay.pump_once() == 1
        await queue.drain()
        assert await relay.pump_once() == 0  # nothing due any more
        (d,) = await _deliveries(dev_ctx)
        assert d.status == "succeeded"
        r = await client.post(f"/v1/webhooks/{hook.id}/deliveries/{d.id}:replay")
        assert r.status_code == 202, r.text
        replay = r.json()
        assert replay["replay_of"] == d.id and replay["status"] == "pending"
        assert replay["event_id"] == event_id
        assert await relay.pump_once() == 1
        await queue.drain()
        missing = f"/v1/webhooks/{hook.id}/deliveries/whd_{'0' * 26}:replay"
        assert (await client.post(missing)).status_code == 404
    assert [x.headers["mapi-event-id"] for x in rec.requests] == [event_id, event_id]
    assert rec.requests[0].body == rec.requests[1].body
    assert await _audits(dev_ctx, "webhook.replay")
    # an in-flight delivery (leased) is not handed out twice
    await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_2")
    d3 = (await _deliveries(dev_ctx))[-1]
    async with dev_ctx.db.session() as s:
        row = await s.get(m.WebhookDelivery, d3.id)
        assert row is not None
        row.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    assert await relay.pump_once() == 0
    # old events are pruned with their delivery log
    async with dev_ctx.db.session() as s:
        await s.execute(text("UPDATE webhook_events SET created_at = now() - interval '400 days'"))
    assert await relay.prune_once() == 1  # the leased one is still due: kept
    async with dev_ctx.db.session() as s:
        left = (await s.execute(select(func.count()).select_from(m.WebhookDelivery))).scalar_one()
    assert left == 1


async def test_deliveries_of_a_disabled_or_deleted_webhook(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    hook = (await _create(client, "https://hooks.example.com/x", ["run.failed"])).webhook
    await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_1")
    await client.patch(f"/v1/webhooks/{hook.id}", json={"active": False})
    (d,) = await _deliveries(dev_ctx)
    assert await WebhookDeliverer(dev_ctx).deliver(d.id) == "failed"
    r = await client.post(f"/v1/webhooks/{hook.id}/deliveries/{d.id}:replay")
    assert r.status_code == 409
    assert (await client.delete(f"/v1/webhooks/{hook.id}")).status_code == 204
    assert await _deliveries(dev_ctx) == []


# ------------------------------------------------------------------------------ isolation


async def test_webhooks_and_endpoints_are_isolated_between_tenants(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """Row-level security on all six new tables: a tenant session sees and writes only its
    own rows, and the gateway role cannot touch them at all."""
    from mapi_server.db import acting_as_gateway
    from mapi_server_testkit.identity import grant_role, make_tenant, make_user, session_caller
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    ctx.webhook_resolver = StaticResolver({"hooks.example.com": [PUBLIC]})
    for t in ("alpha", "beta"):
        await make_tenant(ctx, t)
    ana = await make_user(ctx, "ana@alpha.test")
    bob = await make_user(ctx, "bob@beta.test")
    await grant_role(ctx, ana, "alpha", "main", "admin")
    await grant_role(ctx, bob, "beta", "main", "admin")
    alpha = await session_caller(ctx, client, ana, tenant_id="alpha", project_id="main")
    beta = await session_caller(ctx, client, bob, tenant_id="beta", project_id="main")
    body = {"url": "https://hooks.example.com/b", "events": ["run.failed"]}
    r = await beta.post("/v1/webhooks", json=body)
    assert r.status_code == 201, r.text
    beta_hook = r.json()["webhook"]["id"]
    assert (await alpha.get(f"/v1/webhooks/{beta_hook}")).status_code == 404
    assert (await alpha.delete(f"/v1/webhooks/{beta_hook}")).status_code == 404
    assert (await alpha.get("/v1/webhooks")).json()["items"] == []
    assert (await alpha.in_project("beta", "main").get("/v1/webhooks")).status_code == 404
    async with ctx.db.session() as s:
        await emit_event(s, tenant_id="beta", project_id="main",
                         event_type=WebhookEventType.RUN_FAILED, data={"run_id": "r"})  # fmt: skip
    tables = ("webhooks", "webhook_events", "webhook_deliveries", "endpoints",
              "endpoint_revisions", "endpoint_runs")  # fmt: skip
    async with ctx.db.tenant_session("alpha") as s:
        for table in tables:
            n = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert n == 0, table
    async with ctx.db.tenant_session("beta") as s:
        assert (await s.execute(text("SELECT count(*) FROM webhook_deliveries"))).scalar_one() == 1
    with pytest.raises((ProgrammingError, DBAPIError)):
        async with ctx.db.tenant_session("alpha") as s:  # writing another tenant's row
            await s.execute(
                text(
                    "INSERT INTO webhook_events (id, tenant_id, type, envelope, created_at) "
                    "VALUES ('evt_x', 'beta', 'run.failed', '{}', now())"
                )
            )
    with pytest.raises((ProgrammingError, DBAPIError)):
        async with ctx.db.tenant_session("beta") as s:  # history is append-only
            await s.execute(text("UPDATE endpoint_revisions SET reason = 'x'"))
    for table in tables:
        with pytest.raises(ProgrammingError, match="permission denied"), acting_as_gateway():
            async with ctx.db.session() as s:
                await s.execute(text("SELECT set_config('app.tenant_id', 'beta', true)"))
                await s.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))


async def test_the_new_tables_force_row_level_security(ctx: ServerContext) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(ctx.db.url) as conn:
        rows = await (
            await conn.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = ANY(%s)",
                (list(m.TENANT_TABLES[-6:]),),
            )
        ).fetchall()
    assert {r[0] for r in rows} == set(m.TENANT_TABLES[-6:])
    assert all(r[1] and r[2] for r in rows)


async def test_sender_reports_the_signature_header_name() -> None:
    assert SIGNATURE_HEADER == "Mapi-Signature"


async def test_deliveries_run_on_procrastinate(
    client: httpx.AsyncClient, dev_ctx: ServerContext
) -> None:
    """The production path: the relay defers one job per due try (de-duplicated while
    pending) on the ``webhooks`` queue and the procrastinate worker delivers it."""
    from mapi_server.services.finalizer import Finalizer
    from mapi_server.services.queue import ProcrastinateQueue

    async with http_receiver() as rec:
        await _create(client, f"http://hooks.test:{rec.port}/in", ["run.failed"])
        await _emit(dev_ctx, WebhookEventType.RUN_FAILED, run_id="run_1")
        queue = ProcrastinateQueue(
            dev_ctx.db.url, Finalizer(dev_ctx), deliverer=WebhookDeliverer(dev_ctx)
        )
        relay = WebhookRelay(dev_ctx, queue)
        try:
            assert await relay.pump_once() == 1
            assert await relay.pump_once() == 1  # deferred again: the queueing lock holds
            async with dev_ctx.db.session() as s:
                jobs = (
                    await s.execute(text("SELECT queue_name, task_name FROM procrastinate_jobs"))
                ).all()
            assert jobs == [("webhooks", "mapi.deliver_webhook")]
            await queue.run_worker(wait=False)
        finally:
            await queue.close()
    (d,) = await _deliveries(dev_ctx)
    assert d.status == "succeeded" and len(rec.requests) == 1


async def test_webhooks_manage_tokens_and_role_checks(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """``webhooks:manage`` tokens of an admin manage webhooks; other scopes cannot even list
    them (deliveries reveal what a project is doing), and neither can non-admins."""
    from mapi_server_testkit.identity import (
        grant_role,
        make_token,
        make_user,
        session_caller,
        token_caller,
    )

    ctx.webhook_resolver = StaticResolver({"hooks.example.com": [PUBLIC]})
    admin = await make_user(ctx, "owner@default.test")
    await grant_role(ctx, admin, "default", "default", "admin")
    tok = token_caller(
        client, await make_token(ctx, admin, "default", "default", ["webhooks:manage"])
    )
    body = {"url": "https://hooks.example.com/x", "events": ["run.failed"]}
    r = await tok.post("/v1/webhooks", json=body)
    assert r.status_code == 201, r.text
    assert (await tok.get("/v1/webhooks")).status_code == 200
    reader = token_caller(
        client, await make_token(ctx, admin, "default", "default", ["models:read", "runs:create"])
    )
    assert (await reader.get("/v1/webhooks")).status_code == 403
    assert (await reader.post("/v1/webhooks", json=body)).status_code == 403
    editor = await make_user(ctx, "editor@default.test")
    await grant_role(ctx, editor, "default", "default", "editor")
    ed = await session_caller(ctx, client, editor)
    assert (await ed.get("/v1/webhooks")).status_code == 403


async def test_tenant_events_and_oversized_envelopes(ctx: ServerContext) -> None:
    from mapi_server.services.webhooks.crypto import SecretBox
    from mapi_server.services.webhooks.manage import new_webhook
    from mapi_server.services.webhooks.outbox import MAX_ENVELOPE_BYTES, emit_tenant_event
    from mapi_server_testkit.identity import make_tenant

    await make_tenant(ctx, "gamma", projects=("a", "b"))
    box = SecretBox(ctx.secrets.webhook_key)
    events = [WebhookEventType.TENANT_DELETION_COMPLETED, WebhookEventType.RUN_FAILED]
    async with ctx.db.session() as s:
        for project in ("a", "b"):
            row, _ = new_webhook(
                box,
                tenant_id="gamma",
                project_id=project,
                url="https://hooks.example.com/x",
                events=events,
                description=None,
                active=True,
                created_by=None,
            )
            s.add(row)
        await s.flush()
        event_id = await emit_tenant_event(
            s,
            tenant_id="gamma",
            event_type=WebhookEventType.TENANT_DELETION_COMPLETED,
            data={"deletion_id": "del_1", "completed_at": "2026-10-15T09:30:00Z"},
        )
        big = await emit_event(
            s,
            tenant_id="gamma",
            project_id="a",
            event_type=WebhookEventType.RUN_FAILED,
            data={"run_id": "run_1", "note": "x" * (MAX_ENVELOPE_BYTES + 10)},
        )
    async with ctx.db.session() as s:
        ev = await s.get(m.WebhookEvent, event_id)
        assert ev is not None and ev.project_id is None and ev.envelope["project"] is None
        n = (
            await s.execute(select(func.count()).where(m.WebhookDelivery.event_id == event_id))
        ).scalar_one()
        assert n == 2  # one per project's webhook
        trimmed = await s.get(m.WebhookEvent, big)
        assert trimmed is not None
        assert trimmed.envelope["data"] == {"run_id": "run_1", "truncated": True}
        assert len(envelope_body(trimmed.envelope)) < MAX_ENVELOPE_BYTES
