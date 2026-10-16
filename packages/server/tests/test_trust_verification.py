"""Identity verification and cut line A (M3): public tenants run built-ins and declarative
specs only; uploaded code needs a trusted tier or identity verification, enforced at run
admission. Also row-level security and grants of the new trust tables."""

from __future__ import annotations

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server.services.submit import admit
from mapi_server.services.trust import (
    FakeIdentityVerifier,
    IdentityCheck,
    ManualIdentityVerifier,
)
from mapi_server.services.verification import (
    apply_identity_check,
    require_uploaded_code_allowed,
    verification_status,
)
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.api import new_id
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.trust import VerificationStatus
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, ProgrammingError

pytestmark = pytest.mark.integration

OPS = "ops@platform.test"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT, "operator_emails": [OPS]})


def _run(tenant_id: str) -> m.Run:
    ts = now()
    return m.Run(
        id=new_id("run"), tenant_id=tenant_id, project_id="main", kind="fit", state="created",
        attempt=1, resource_class="S", timeout_s=60, labels={}, payload={}, inputs={},
        checkpointable=False, infra_retries=0, finalize_attempts=0, finalize={},
        last_event_id=0, created_at=ts, updated_at=ts,
    )  # fmt: skip


async def test_operators_record_identity_verification(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await make_tenant(ctx, "acme", tier="verified")
    owner = await make_user(ctx, "owner@acme.test")
    await grant_role(ctx, owner, "acme", None, "admin")
    admin = await session_caller(ctx, client, owner)
    r = await admin.get("/v1/tenants/acme/verification")
    status = VerificationStatus.model_validate(r.json())
    assert (status.identity_verified, status.uploaded_code_allowed) == (False, False)
    assert status.reason_plain == "Uploading code needs a verified identity."
    body = {"identity_verified": True, "method": "manual", "reason": "signed contract"}
    assert (await admin.put("/v1/admin/tenants/acme/verification", json=body)).status_code == 403
    ops = await session_caller(ctx, client, await make_user(ctx, OPS, operator=True))
    r = await ops.put("/v1/admin/tenants/acme/verification", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["identity_verified"] is True and r.json()["uploaded_code_allowed"] is True
    assert r.json()["method"] == "manual"
    r = await admin.get("/v1/tenants/acme/verification")
    assert r.json()["uploaded_code_allowed"] is True
    async with ctx.db.session() as s:
        [ev] = (
            await s.execute(
                select(m.AuditEvent).where(m.AuditEvent.action == "tenant.verification_update")
            )
        ).scalars()
    assert ev.operator and ev.details["reason"] == "signed contract" and ev.details["was"] is False
    assert (await ops.put("/v1/admin/tenants/nope/verification", json=body)).status_code == 404
    # an API token never reaches tenant routes, whatever its scopes
    secret = await make_token(ctx, owner, "acme", "main", ["admin"])
    r = await token_caller(client, secret).get("/v1/tenants/acme/verification")
    assert r.status_code == 403


@pytest.mark.parametrize(
    ("tier", "identity", "public_open", "allowed"),
    [
        ("internal", False, False, True),
        ("self-hosted-trusted", False, False, True),
        ("verified", False, False, False),
        ("verified", True, False, True),
        ("public", True, False, False),  # cut line A: never, verified or not
        ("public", False, True, False),
        ("public", True, True, True),  # cut line B (M5)
    ],
)
async def test_uploaded_code_rule_at_admission(
    ctx: ServerContext, tier: str, identity: bool, public_open: bool, allowed: bool
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"public_code_upload_enabled": public_open})
    await make_tenant(ctx, "t1", tier=tier)
    if identity:
        async with ctx.db.session() as s:
            await apply_identity_check(s, ctx, "t1", _pass())
    async with ctx.db.tenant_session("t1") as s:
        run = _run("t1")
        s.add(run)
        await s.flush()
        if allowed:
            await require_uploaded_code_allowed(s, ctx, "t1")
            await admit(s, ctx, run, untrusted=True)
            assert run.untrusted_code is True
        else:
            with pytest.raises(MapiError) as err:
                await admit(s, ctx, run, untrusted=True)
            assert err.value.code is ErrorCode.IDENTITY_VERIFICATION_REQUIRED
            assert "Built-in models and declarative specs" in err.value.detail
    # built-in plugins and declarative specs are always admitted (untrusted data, trusted code)
    async with ctx.db.tenant_session("t1") as s:
        run = _run("t1")
        s.add(run)
        await s.flush()
        await admit(s, ctx, run, untrusted=False)


def _pass() -> IdentityCheck:
    return IdentityCheck(verified=True, method="document", reference="ref-1", reason="ok")


async def test_identity_providers(ctx: ServerContext) -> None:
    await make_tenant(ctx, "acme", tier="verified")
    fake = FakeIdentityVerifier()
    assert ctx.trust is not None
    ctx.trust.identity = fake
    failed = await fake.check("acme", {"result": "fail"})
    async with ctx.db.session() as s:
        status = await apply_identity_check(s, ctx, "acme", failed)
    assert not status.identity_verified and not status.uploaded_code_allowed
    passed = await fake.check("acme", {"result": "pass"})
    async with ctx.db.session() as s:
        status = await apply_identity_check(s, ctx, "acme", passed)
        row = await s.get(m.TenantVerification, "acme")
    assert status.identity_verified and status.uploaded_code_allowed
    assert status.method == "document"
    assert row is not None and row.provider_reference == "fake-2"
    assert row.verified_by == "provider:fake"
    assert [t for t, _ in fake.checks] == ["acme", "acme"]
    async with ctx.db.session() as s:
        outcomes = (
            await s.execute(
                select(m.AuditEvent.outcome)
                .where(m.AuditEvent.action == "tenant.verification_update")
                .order_by(m.AuditEvent.ts)
            )
        ).scalars()
        assert list(outcomes) == ["denied", "success"]
    with pytest.raises(NotImplementedError):
        await ManualIdentityVerifier().check("acme", {})
    async with ctx.db.session() as s:
        missing = await verification_status(s, ctx, "acme")
    assert missing.identity_verified


async def test_trust_tables_are_isolated_per_tenant(ctx: ServerContext) -> None:
    """Forced row-level security on the three tenant tables, and least privilege: the
    request-path role reads but never changes verification; the gateway never reads
    exports."""
    for tenant in ("alpha", "beta"):
        await make_tenant(ctx, tenant)
        ts = now()
        async with ctx.db.session() as s:
            s.add(
                m.TenantVerification(
                    tenant_id=tenant,
                    email_verified=True,
                    identity_verified=False,
                    method="email",
                    updated_at=ts,
                )
            )
            s.add(
                m.TenantDeletion(
                    id=new_id("del"),
                    tenant_id=tenant,
                    state="cancelled",
                    previous_status="active",
                    requested_at=ts,
                    purge_after=ts,
                    stores=[],
                    retained_objects=[],
                    notify=[],
                    attempts=0,
                )
            )
            s.add(m.TenantExport(id=new_id("exp"), tenant_id=tenant, state="queued",
                                 include=["metadata"], created_at=ts))  # fmt: skip
    for model in (m.TenantVerification, m.TenantDeletion, m.TenantExport):
        async with ctx.db.tenant_session("alpha") as s:
            tenants = (await s.execute(select(model.tenant_id))).scalars()
            assert set(tenants) == {"alpha"}, model.__tablename__
    async with ctx.db.tenant_session("alpha") as s:  # writes to beta's rows affect nothing
        res = await s.execute(
            update(m.TenantDeletion)
            .where(m.TenantDeletion.tenant_id == "beta")
            .values(state="scheduled")
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with ctx.db.tenant_session("alpha") as s:
            await s.execute(update(m.TenantVerification).values(identity_verified=True))
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with ctx.db.tenant_session("alpha") as s:
            await s.execute(text("DELETE FROM tenant_deletions"))
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(m.TenantExport(id=new_id("exp"), tenant_id="beta", state="queued",
                                 include=[], created_at=now()))  # fmt: skip
            await s.flush()
    for stmt in ("SELECT * FROM tenant_exports", "SELECT * FROM tenant_deletions",
                 "DELETE FROM signups"):  # fmt: skip
        with pytest.raises((ProgrammingError, DBAPIError)):
            async with ctx.db.gateway_session() as s:
                if "deletions" in stmt:
                    # gateway reads deletions (operators) but only with its own policy
                    await s.execute(text("DELETE FROM tenant_deletions"))
                else:
                    await s.execute(text(stmt))
    async with ctx.db.gateway_session() as s:
        n = (await s.execute(text("SELECT count(*) FROM tenant_deletions"))).scalar_one()
        assert n == 2
        await s.execute(text("SELECT count(*) FROM signups"))
