"""How the M3 components fit together (integration of the guided, endpoints, webhooks,
sharing and trust-lifecycle branches):

* a tenant deletion revokes the tenant's embed links and webhooks at the start, purges every
  M3 table, and then delivers a signed ``tenant.deletion.completed`` to the webhooks that
  subscribed to it (through the SSRF-guarded sender), leaving other tenants untouched;
* sign-up tenants are public tier: built-ins and declarative specs only (cut line A), and
  placement needs a sandbox with enforced egress (no local-process executor).
"""

from __future__ import annotations

import json

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.evidence import placement_problem
from mapi_server.services.lifecycle import InlineLifecycleJobs, LifecycleScheduler
from mapi_server.services.runs import now
from mapi_server.services.submit import admit
from mapi_server.services.tenant_hooks import (
    DELETION_DISABLED_REASON,
    EmbedRevoker,
    WebhookDeletionNotifier,
    WebhookRevoker,
)
from mapi_server.services.webhooks.delivery import WebhookSender
from mapi_server.services.webhooks.ssrf import StaticResolver, UrlPolicy
from mapi_server.settings import MailerKind, Profile, Settings
from mapi_server_testkit.identity import session_caller
from mapi_server_testkit.integrations import http_receiver
from mapi_server_testkit.trust import WEBHOOK_SECRET, RecordingMailer, seed_tenant
from mapi_spec.api import new_id
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import ExecutorCaps, Isolation
from mapi_spec.resources import ResourceClass
from mapi_spec.trust import TERMS_VERSION
from mapi_spec.webhooks import SIGNATURE_HEADER, WebhookEnvelope, verify_signature
from sqlalchemy import select

pytestmark = pytest.mark.integration

OPS = "ops@platform.test"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "profile": Profile.MULTI_TENANT,
            "operator_emails": [OPS],
            "tenant_deletion_grace_s": 0.0,
            "signup_enabled": True,
            "mailer": MailerKind.SMTP,
            "smtp_host": "smtp.invalid",
        }
    )


def _local_sender() -> WebhookSender:
    """Deliveries to http://hooks.test (127.0.0.1): the dev exemptions, which the
    multi-tenant profile refuses in settings, granted to this sender only."""
    return WebhookSender(
        UrlPolicy(allow_http=True, dev_allowlist=frozenset({"hooks.test"})),
        resolver=StaticResolver({"hooks.test": ["127.0.0.1"]}),
    )


# ======================================================================== tenant deletion


def test_the_m3_hooks_are_registered(ctx: ServerContext) -> None:
    assert ctx.trust is not None
    names = list(ctx.trust.revoker_names())
    assert names.count("embeds") == 1 and names.count("webhooks") == 1
    assert isinstance(ctx.trust.notifier, WebhookDeletionNotifier)


async def test_deletion_revokes_purges_and_notifies(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert ctx.trust is not None
    ctx.trust.notifier = WebhookDeletionNotifier(ctx, sender=_local_sender(), sleep=no_sleep)
    async with http_receiver() as rec:
        rec.responses = [(503, {}, b"busy")]  # the first try fails, the retry lands
        url = f"http://hooks.test:{rec.port}/in"
        alpha = await seed_tenant(ctx, "alpha", webhook_url=url)
        beta = await seed_tenant(ctx, "beta", webhook_url=url)
        owner = await session_caller(ctx, client, alpha.user)
        r = await owner.post("/v1/tenants/alpha/deletion", json={"confirm": "alpha"})
        assert r.status_code == 202, r.text
        deletion_id = r.json()["id"]
        jobs = InlineLifecycleJobs(ctx)
        assert (await LifecycleScheduler(ctx, jobs).tick())["purges"] == 1
        await jobs.drain()

    # one signed tenant.deletion.completed, to alpha's webhook only (beta's is untouched)
    assert len(rec.requests) == 2 and sleeps == [2.0]
    got = rec.requests[-1]
    assert got.target == "/in" and got.headers["mapi-webhook-id"] == alpha.webhook_id
    assert verify_signature(got.body, got.headers[SIGNATURE_HEADER.lower()], [WEBHOOK_SECRET])
    env = WebhookEnvelope.model_validate_json(got.body)
    assert (env.type.value, env.tenant, env.project) == ("tenant.deletion.completed", "alpha", None)
    assert env.data["deletion_id"] == deletion_id and env.data["requested_at"]
    async with ctx.db.session() as s:
        row = await s.get(m.TenantDeletion, deletion_id)
        assert row is not None and row.state == "purged" and row.notify == []
        hooks = {stp["store"]: stp for stp in row.stores}
        assert hooks["webhooks"]["items_done"] == 1
        [start] = (
            await s.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.action == "tenant.delete", m.AuditEvent.tenant_id == "alpha"
                )
            )
        ).scalars()
        assert start.details["revoked"]["embeds"] == 1
        assert start.details["revoked"]["webhooks"] == 1
        # every M3 row of alpha is gone; beta keeps all of its own
        for model in (
            m.Endpoint, m.EndpointRevision, m.EndpointRun, m.Webhook, m.WebhookEvent,
            m.WebhookDelivery, m.SavedPlot, m.PlotEmbed, m.DatasetVersion,
        ):  # fmt: skip
            mine = (await s.execute(select(model).where(model.tenant_id == "alpha"))).all()
            theirs = (await s.execute(select(model).where(model.tenant_id == "beta"))).all()
            assert mine == [] and len(theirs) >= 1, model.__tablename__
        hook = await s.get(m.Webhook, beta.webhook_id)
        assert hook is not None and hook.active and hook.disabled_reason is None
        embeds = await s.execute(select(m.PlotEmbed).where(m.PlotEmbed.tenant_id == "beta"))
        assert all(e.revoked_at is None for e in embeds.scalars())


async def test_revokers_and_capture(ctx: ServerContext) -> None:
    alpha = await seed_tenant(ctx, "alpha")
    beta = await seed_tenant(ctx, "beta")
    notifier = WebhookDeletionNotifier(ctx, sender=_local_sender())
    at = now()
    async with ctx.db.session() as s:
        assert await EmbedRevoker().revoke(s, "alpha", now=at) == 1
        assert await EmbedRevoker().revoke(s, "alpha", now=at) == 0  # idempotent
        assert await WebhookRevoker().revoke(s, "alpha", now=at) == 1
        captured = await notifier.capture(s, "alpha")
        # a webhook that was disabled for its failures is not notified
        other = await s.get(m.Webhook, beta.webhook_id)
        assert other is not None
        other.active, other.disabled_reason = False, "20 events in a row could not be delivered"
        assert await notifier.capture(s, "beta") == []
    async with ctx.db.session() as s:
        hook = await s.get(m.Webhook, alpha.webhook_id)
        assert hook is not None and not hook.active
        assert hook.disabled_reason == DELETION_DISABLED_REASON
    [target] = captured
    assert isinstance(target, dict)
    assert target["webhook_id"] == alpha.webhook_id and target["url"] == alpha.webhook_url
    # the stored target keeps the secret sealed, never in clear
    assert WEBHOOK_SECRET not in json.dumps(captured) and target["secret_sealed"]
    # nothing to deliver to: no error
    assert await notifier.deliver("alpha", new_id("del"), [], now=at) == 0


async def test_blocked_targets_are_not_retried(ctx: ServerContext) -> None:
    alpha = await seed_tenant(ctx, "alpha", webhook_url="https://169.254.169.254/latest")
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    notifier = WebhookDeletionNotifier(ctx, sender=_local_sender(), sleep=no_sleep)
    async with ctx.db.session() as s:
        captured = await notifier.capture(s, "alpha")
    assert await notifier.deliver("alpha", new_id("del"), captured, now=now()) == 1
    assert sleeps == []  # a blocked address is final
    assert alpha.webhook_id


# ===================================================================== sign-up: cut line A


async def test_sign_up_tenants_follow_cut_line_a(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mailer = RecordingMailer()
    assert ctx.trust is not None
    ctx.trust.mailer = mailer
    r = await client.post(
        "/auth/signup",
        json={
            "email": "ana@acme.example",
            "name": "Ana",
            "organisation": "Acme Retail",
            "accepted_terms": True,
            "terms_version": TERMS_VERSION,
        },
    )
    assert r.status_code == 202, r.text
    r = await client.post(
        "/auth/verify-email", json={"token": mailer.token_for("ana@acme.example")}
    )
    assert r.status_code == 200, r.text
    tenant_id = r.json()["tenant_id"]
    async with ctx.db.session() as s:
        tenant = await s.get(m.Tenant, tenant_id)
        assert tenant is not None and tenant.trust_tier == "public"
    run = m.Run(
        id=new_id("run"), tenant_id=tenant_id, project_id="default", kind="fit",
        state="created", attempt=0, resource_class="S", timeout_s=60, labels={}, payload={},
        inputs={}, checkpointable=False, infra_retries=0, finalize_attempts=0, finalize={},
        last_event_id=0, created_at=now(), updated_at=now(),
    )  # fmt: skip
    # uploaded code: never for a public tenant (cut line B is M5)
    async with ctx.db.tenant_session(tenant_id) as s:
        with pytest.raises(MapiError) as err:
            await admit(s, ctx, run, untrusted=True, audit_action=None)
    assert err.value.code is ErrorCode.IDENTITY_VERIFICATION_REQUIRED
    # built-ins: admitted, but only an executor with enforced egress and DNS may place them
    local = ExecutorCaps(
        isolation=Isolation.PROCESS, egress_enforced=False, dns_enforced=False,
        max_runtime=3600, classes=[ResourceClass.S], max_tasks=4,
    )  # fmt: skip
    async with ctx.db.session() as s:
        s.add(
            m.ExecutorReport(
                name="local-process", enabled=True, caps=local.model_dump(mode="json"),
                submits_last_minute=0, throttled_last_hour=0, checked_at=now(),
            )
        )  # fmt: skip
    async with ctx.db.session() as s:
        problem = await placement_problem(
            s, tier="public", resource_class=ResourceClass.S, residency=None, now=now()
        )
        internal = await placement_problem(
            s, tier="internal", resource_class=ResourceClass.S, residency=None, now=now()
        )
    assert problem is not None and "gvisor" in problem
    assert internal is None
