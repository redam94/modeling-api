"""Self-service sign-up and e-mail verification (M3): the happy path end to end, the
refusals (terms, disposable domains, rate limits, used or expired links), the Sybil review
queue, the mailers, and the dev outbox."""

from __future__ import annotations

import json
import smtplib
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from mapi_server.app import create_app
from mapi_server.auth.identity import VerifiedIdentity, claim_or_create_user
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.mail import (
    MailerError,
    MailMessage,
    OutboxMailer,
    SmtpMailer,
    build_mailer,
    read_outbox,
)
from mapi_server.services.runs import now
from mapi_server.services.signup import tenant_slug, token_digest
from mapi_server.services.trust import load_email_policy
from mapi_server.settings import MailerKind, Profile, Settings
from mapi_server_testkit.identity import make_user, session_caller
from mapi_server_testkit.trust import RecordingMailer
from mapi_spec.trust import TERMS_VERSION, SignupReview, VerificationResult
from sqlalchemy import select, update

pytestmark = pytest.mark.integration

OPS = "ops@platform.test"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "profile": Profile.MULTI_TENANT,
            "operator_emails": [OPS],
            "signup_enabled": True,
            "mailer": MailerKind.SMTP,
            "smtp_host": "smtp.invalid",
        }
    )


@pytest.fixture
def mailer(ctx: ServerContext) -> RecordingMailer:
    rec = RecordingMailer()
    assert ctx.trust is not None
    ctx.trust.mailer = rec
    return rec


def _body(email: str, **kw: Any) -> dict[str, Any]:
    return {
        "email": email,
        "name": "Ana Analyst",
        "organisation": "Acme Retail",
        "accepted_terms": True,
        "terms_version": TERMS_VERSION,
        **kw,
    }


async def _signup(client: httpx.AsyncClient, email: str, **kw: Any) -> httpx.Response:
    headers = kw.pop("headers", {})
    return await client.post("/auth/signup", json=_body(email, **kw), headers=headers)


async def _audit(ctx: ServerContext, action: str) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        return list(
            (await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == action))).scalars()
        )


async def test_sign_up_verify_and_sign_in(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    r = await _signup(client, "Ana@Acme.example")
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "check_email", "expires_in_s": 86400}
    assert r.headers["cache-control"] == "no-store"
    assert "mapi_device=" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
    [mail] = mailer.sent
    assert mail.to == "ana@acme.example"
    token = mail.tags["token"]
    assert token in mail.body and mail.tags["link"].endswith(f"?token={token}")
    async with ctx.db.session() as s:
        row = (await s.execute(select(m.Signup))).scalar_one()
    assert row.token_hash == token_digest(ctx, token) and token not in row.token_hash
    assert row.ip_hash is not None and row.ip_hash != "127.0.0.1"

    r = await client.post("/auth/verify-email", json={"token": token})
    assert r.status_code == 200, r.text
    result = VerificationResult.model_validate(r.json())
    assert (result.tenant_id, result.project_id, result.trust_tier) == (
        "acme-retail",
        "default",
        "public",
    )
    async with ctx.db.session() as s:
        tenant = await s.get(m.Tenant, "acme-retail")
        assert tenant is not None and (tenant.trust_tier, tenant.status) == ("public", "active")
        assert await s.get(m.Project, ("acme-retail", "default")) is not None
        ver = await s.get(m.TenantVerification, "acme-retail")
        assert ver is not None and ver.email_verified and not ver.identity_verified
        grants = (
            await s.execute(select(m.Membership).where(m.Membership.tenant_id == "acme-retail"))
        ).scalars()
        [grant] = list(grants)
        assert (grant.project_id, grant.role, grant.email) == (None, "admin", "ana@acme.example")
        # the first sign-in with that verified address claims the invitation
        user = await claim_or_create_user(
            s, VerifiedIdentity("https://idp.test", "ana-1", "ana@acme.example", True, "Ana")
        )
        assert user.id == grant.principal_id
    ana = await session_caller(ctx, client, user)
    me = (await ana.get("/v1/me")).json()
    assert [t["tenant"]["id"] for t in me["tenants"]] == ["acme-retail"]
    ver_status = (await ana.get("/v1/tenants/acme-retail/verification")).json()
    assert ver_status["email_verified"] is True and ver_status["uploaded_code_allowed"] is False
    for action in ("auth.signup", "auth.email_verified", "tenant.create"):
        assert await _audit(ctx, action), action
    # the link is single use
    r = await client.post("/auth/verify-email", json={"token": token})
    assert r.status_code == 410 and r.json()["code"] == "LINK_EXPIRED"


async def test_known_addresses_get_the_same_answer_and_a_new_workspace(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    await make_user(ctx, "known@acme.example")
    fresh = await _signup(client, "new@acme.example", organisation=None)
    known = await _signup(client, "known@acme.example", organisation=None)
    assert (fresh.status_code, fresh.json()) == (known.status_code, known.json())
    r = await client.post(
        "/auth/verify-email", json={"token": mailer.token_for("known@acme.example")}
    )
    assert r.status_code == 200 and r.json()["tenant_id"] == "known"
    async with ctx.db.session() as s:
        users = (
            await s.execute(select(m.User).where(m.User.email == "known@acme.example"))
        ).scalars()
        assert len(list(users)) == 1  # the existing account administers the new tenant


@pytest.mark.parametrize(
    ("body", "code", "pointer"),
    [
        ({"accepted_terms": False}, "TERMS_NOT_ACCEPTED", "/accepted_terms"),
        ({"terms_version": "2020-01"}, "TERMS_NOT_ACCEPTED", "/accepted_terms"),
        ({"email": "bot@mailinator.com"}, "EMAIL_NOT_ALLOWED", "/email"),
        ({"email": "bot@eu.yopmail.com"}, "EMAIL_NOT_ALLOWED", "/email"),
        ({"email": "bot@blocked.example"}, "EMAIL_NOT_ALLOWED", "/email"),
    ],
)
async def test_refused_sign_ups(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    mailer: RecordingMailer,
    body: dict[str, Any],
    code: str,
    pointer: str,
) -> None:
    assert ctx.trust is not None
    ctx.trust.email_policy = load_email_policy(
        ctx.settings.model_copy(update={"signup_blocked_domains": ["blocked.example"]})
    )
    r = await client.post("/auth/signup", json={**_body("ana@acme.example"), **body})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == code and r.json()["errors"][0]["pointer"] == pointer
    assert not mailer.sent
    [ev] = await _audit(ctx, "auth.signup_rejected")
    assert ev.outcome == "denied" and ev.details["reason"] == code and ev.ip_hash
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.Signup))).first() is None


async def test_blocklist_file(tmp_path: Path, settings: Settings) -> None:
    path = tmp_path / "disposable.txt"
    path.write_text("# maintained list\nthrowaway.test\n  spam.example # trailing\n")
    policy = load_email_policy(settings.model_copy(update={"signup_blocklist_path": path}))
    assert policy.is_disposable("throwaway.test")
    assert policy.is_disposable("x.spam.example")
    assert policy.is_disposable("mailinator.com")
    assert not policy.is_disposable("acme.example")


async def test_rate_limits_per_ip_domain_and_platform(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    ctx.settings = ctx.settings.model_copy(
        update={"signup_per_ip_per_hour": 2, "signup_per_domain_per_day": 3}
    )
    for i in range(2):
        assert (await _signup(client, f"u{i}@one.example")).status_code == 202
    r = await _signup(client, "u3@two.example")
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
    assert 0 < int(r.headers["retry-after"]) <= 3601
    assert "network address" in r.json()["detail"]
    # another client address: the per-domain limit (3 a day) still holds
    async with ctx.db.session() as s:
        await s.execute(update(m.Signup).values(ip_hash="elsewhere"))
    assert (await _signup(client, "u4@one.example")).status_code == 202
    async with ctx.db.session() as s:
        await s.execute(update(m.Signup).values(ip_hash="elsewhere"))
    r = await _signup(client, "u5@one.example")
    assert r.status_code == 429 and "domain" in r.json()["detail"]
    ctx.settings = ctx.settings.model_copy(update={"signup_global_per_hour": 3})
    r = await _signup(client, "u6@three.example")
    assert r.status_code == 429 and "platform" in r.json()["detail"]
    assert len(mailer.sent) == 3
    assert await _audit(ctx, "auth.signup_rejected")


async def test_links_expire_and_only_the_newest_works(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    await _signup(client, "ana@acme.example")
    first = mailer.token_for("ana@acme.example")
    await _signup(client, "ana@acme.example")
    second = mailer.token_for("ana@acme.example")
    assert first != second
    r = await client.post("/auth/verify-email", json={"token": first})
    assert r.status_code == 410
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Signup)
            .where(m.Signup.token_hash == token_digest(ctx, second))
            .values(expires_at=now() - timedelta(seconds=1))
        )
    r = await client.post("/auth/verify-email", json={"token": second})
    assert r.status_code == 410 and r.json()["code"] == "LINK_EXPIRED"
    async with ctx.db.session() as s:
        states = sorted((await s.execute(select(m.Signup.state))).scalars())
        assert states == ["expired", "expired"]
    bad = await client.post("/auth/verify-email", json={"token": "not-a-token"})
    assert bad.status_code == 422


async def test_verification_attempts_are_rate_limited(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"verify_per_ip_per_minute": 2})
    junk = {"token": "mev_" + "0" * 40}
    assert [(await client.post("/auth/verify-email", json=junk)).status_code for _ in range(3)] == [
        410,
        410,
        429,
    ]


async def test_sign_up_is_off_without_a_mailer(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"signup_enabled": False})
    r = await _signup(client, "ana@acme.example")
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"


async def _verify(client: httpx.AsyncClient, mailer: RecordingMailer, email: str) -> str:
    assert (await _signup(client, email)).status_code == 202
    r = await client.post("/auth/verify-email", json={"token": mailer.token_for(email)})
    assert r.status_code == 200, r.text
    return str(r.json()["tenant_id"])


async def test_sybil_heuristics_flag_and_operators_review(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"signup_sybil_threshold": 2})
    cookie = {"Cookie": "mapi_device=device-0123456789abcdef"}
    tenants = []
    for i in range(3):
        email = f"farm{i}@farm{i}.example"
        r = await client.post("/auth/signup", json=_body(email, organisation=f"Farm {i}"),
                              headers=cookie)  # fmt: skip
        assert r.status_code == 202 and "set-cookie" not in r.headers  # the cookie is reused
        v = await client.post("/auth/verify-email", json={"token": mailer.token_for(email)})
        tenants.append(v.json()["tenant_id"])
    ops = await session_caller(ctx, client, await make_user(ctx, OPS, operator=True))
    page = (await ops.get("/v1/admin/signups", params={"review": "flagged"})).json()
    [flagged] = [SignupReview.model_validate(x) for x in page["items"]]
    assert flagged.tenant_id == tenants[2]
    assert set(flagged.flags) == {"same_ip", "same_device"}
    assert flagged.reasons_plain and flagged.ip_hash and flagged.device_hash
    [ev] = await _audit(ctx, "auth.signup_flagged")
    assert ev.tenant_id == tenants[2] and ev.details["suspended"] is False
    # the flagged workspace works until an operator decides
    async with ctx.db.session() as s:
        t = await s.get(m.Tenant, tenants[2])
        assert t is not None and t.status == "active"
    r = await ops.post(f"/v1/admin/signups/{flagged.id}/review",
                       json={"decision": "reject", "reason": "device farm"})  # fmt: skip
    assert r.status_code == 200 and r.json()["review"] == "rejected"
    async with ctx.db.session() as s:
        t = await s.get(m.Tenant, tenants[2])
        assert t is not None and t.status == "suspended"
        assert (t.status_reason or "").startswith("sign-up review: ")
    r = await ops.post(f"/v1/admin/signups/{flagged.id}/review",
                       json={"decision": "approve", "reason": "a reseller, confirmed"})  # fmt: skip
    assert r.json()["review"] == "approved"
    async with ctx.db.session() as s:
        t = await s.get(m.Tenant, tenants[2])
        assert t is not None and t.status == "active" and t.status_reason is None
    assert len(await _audit(ctx, "auth.signup_review")) == 2
    assert await _audit(ctx, "tenant.suspend") and await _audit(ctx, "tenant.reactivate")
    # every sign-up is listed, newest first, with cursor paging
    page1 = (await ops.get("/v1/admin/signups", params={"limit": 2})).json()
    page2 = (await ops.get("/v1/admin/signups",
                           params={"limit": 2, "cursor": page1["next_cursor"]})).json()  # fmt: skip
    ids = [x["id"] for x in page1["items"] + page2["items"]]
    assert len(ids) == 3 and ids == sorted(ids, reverse=True) and page2["next_cursor"] is None
    # tenant members and anonymous callers cannot see the queue
    assert (await client.get("/v1/admin/signups")).status_code == 401
    ana = await session_caller(ctx, client, await make_user(ctx, "ana@x.test"))
    assert (await ana.get("/v1/admin/signups")).status_code == 403


async def test_flagged_sign_ups_can_be_held_until_approved(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    ctx.settings = ctx.settings.model_copy(
        update={"signup_sybil_threshold": 1, "signup_flagged_suspend": True}
    )
    first = await _verify(client, mailer, "a@a.example")
    second = await _verify(client, mailer, "b@b.example")
    async with ctx.db.session() as s:
        t1, t2 = await s.get(m.Tenant, first), await s.get(m.Tenant, second)
        assert t1 is not None and t1.status == "active"
        assert t2 is not None and t2.status == "suspended"
    [ev] = await _audit(ctx, "auth.signup_flagged")
    assert ev.outcome == "denied" and ev.details["suspended"] is True


async def test_rejecting_a_pending_sign_up_kills_its_link(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    await _signup(client, "ana@acme.example")
    ops = await session_caller(ctx, client, await make_user(ctx, OPS, operator=True))
    [item] = (await ops.get("/v1/admin/signups")).json()["items"]
    r = await ops.post(f"/v1/admin/signups/{item['id']}/review",
                       json={"decision": "reject", "reason": "abuse report"})  # fmt: skip
    assert r.status_code == 200 and r.json()["state"] == "expired"
    r = await client.post("/auth/verify-email",
                          json={"token": mailer.token_for("ana@acme.example")})  # fmt: skip
    assert r.status_code == 410
    missing = "sgn_" + "0" * 26
    r = await ops.post(f"/v1/admin/signups/{missing}/review",
                       json={"decision": "approve", "reason": "x"})  # fmt: skip
    assert r.status_code == 404


def test_tenant_slugs() -> None:
    assert tenant_slug("Acme Retail GmbH") == "acme-retail-gmbh"
    assert tenant_slug("  ÄÖ!! ") == "workspace"
    assert tenant_slug("ab") == "ws-ab"
    assert len(tenant_slug("x" * 300)) <= 40


async def test_unverified_sign_ups_are_swept(
    ctx: ServerContext, client: httpx.AsyncClient, mailer: RecordingMailer
) -> None:
    from mapi_server.services.signup import sweep_signups

    await _signup(client, "ana@acme.example")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Signup).values(
                created_at=now() - timedelta(days=40), expires_at=now() - timedelta(days=39)
            )
        )
    async with ctx.db.session() as s:
        assert await sweep_signups(s, ctx, at=now()) == 1
        assert (await s.execute(select(m.Signup))).first() is None


# ================================================================================ mailers


async def test_outbox_mailer_and_reader(tmp_path: Path) -> None:
    box = OutboxMailer(tmp_path / "outbox", "mapi <no-reply@mapi.test>", keep=3)
    for i in range(5):
        await box.send(MailMessage(to=f"u{i}@x.test", subject="hi", body="b", tags={"i": str(i)}))
    files = sorted((tmp_path / "outbox").glob("*.json"))
    assert len(files) == 3 and oct(files[0].stat().st_mode & 0o777) == "0o600"
    newest = read_outbox(tmp_path / "outbox")
    assert [m_["to"] for m_ in newest] == ["u4@x.test", "u3@x.test", "u2@x.test"]
    assert read_outbox(tmp_path / "outbox", to="U3@x.test")[0]["tags"] == {"i": "3"}
    assert read_outbox(tmp_path / "missing") == []


def test_messages_refuse_header_injection() -> None:
    with pytest.raises(ValueError):
        MailMessage(to="a@x.test\r\nBcc: victim@x.test", subject="s", body="b")
    with pytest.raises(ValueError):
        MailMessage(to="a@x.test", subject="hi\nthere", body="b")
    msg = MailMessage(to="a@x.test", subject="Confirm", body="link").to_email("m <n@mapi.test>")
    assert msg["Auto-Submitted"] == "auto-generated" and msg["Message-ID"]


class _FakeSMTP:
    instances: ClassVar[list[_FakeSMTP]] = []
    starttls_offered: ClassVar[bool] = True

    def __init__(self, host: str, port: int, timeout: float = 0, **kw: Any) -> None:
        self.host, self.port, self.calls = host, port, ["connect"]
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def has_extn(self, name: str) -> bool:
        return self.starttls_offered

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}")

    def send_message(self, msg: Any) -> None:
        self.calls.append(f"send:{msg['To']}")


@pytest.fixture
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeSMTP]]:
    _FakeSMTP.instances = []
    _FakeSMTP.starttls_offered = True
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    yield _FakeSMTP


async def test_smtp_mailer_requires_tls(fake_smtp: type[_FakeSMTP]) -> None:
    mail = MailMessage(to="a@x.test", subject="s", body="b")
    sender = SmtpMailer("smtp.test", sender="n@mapi.test", username="u", password="p")
    await sender.send(mail)
    assert fake_smtp.instances[0].calls == [
        "connect", "ehlo", "starttls", "ehlo", "login:u", "send:a@x.test", "quit",
    ]  # fmt: skip
    await SmtpMailer("smtp.test", port=465, sender="n@mapi.test", tls="ssl").send(mail)
    assert fake_smtp.instances[1].calls == ["connect", "send:a@x.test", "quit"]
    fake_smtp.starttls_offered = False
    with pytest.raises(MailerError, match="STARTTLS"):
        await sender.send(mail)
    with pytest.raises(ValueError):
        SmtpMailer("smtp.test", sender="n@mapi.test", tls="none")


def test_mailer_selection(settings: Settings, tmp_path: Path) -> None:
    assert build_mailer(settings).kind is MailerKind.SMTP
    dev = settings.model_copy(update={"profile": Profile.DEV, "mailer": None,
                                      "outbox_dir": tmp_path})  # fmt: skip
    assert build_mailer(dev).kind is MailerKind.OUTBOX
    single = settings.model_copy(
        update={"profile": Profile.SINGLE_TENANT, "mailer": None, "signup_enabled": None}
    )
    assert build_mailer(single).kind is MailerKind.CONSOLE and not single.signup_open


def test_multi_tenant_sign_up_refuses_readable_mailers() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="smtp"):
        Settings(database_url="postgresql://x/y", profile="multi-tenant", mailer="console")
    with pytest.raises(ValidationError, match="needs a mailer"):
        Settings(database_url="postgresql://x/y", profile="multi-tenant", signup_enabled=True)
    quiet = Settings(
        database_url="postgresql://x/y", profile="multi-tenant", trusted_proxies="none"
    )
    assert not quiet.signup_open  # no SMTP host: sign-up is simply off


async def test_dev_outbox_endpoint(ctx: ServerContext, tmp_path: Path) -> None:
    dev = ctx.settings.model_copy(
        update={"profile": Profile.DEV, "mailer": MailerKind.OUTBOX, "outbox_dir": tmp_path,
                "smtp_host": None}
    )  # fmt: skip
    ctx.settings = dev
    assert ctx.trust is not None
    ctx.trust.mailer = build_mailer(dev)
    app = create_app(dev, ctx=ctx, manage_context=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        assert (await _signup(c, "dev@acme.example")).status_code == 202
        r = await c.get("/dev/outbox", params={"to": "dev@acme.example"})
        assert r.status_code == 200
        [item] = r.json()["items"]
        token = item["tags"]["token"]
        v = await c.post("/auth/verify-email", json={"token": token})
        assert v.status_code == 200
    assert json.loads(next(tmp_path.glob("*.json")).read_text())["to"] == "dev@acme.example"
    # not served in any other profile
    assert all(
        getattr(r, "path", "") != "/dev/outbox"
        for r in create_app(ctx.settings.model_copy(update={"profile": Profile.MULTI_TENANT,
                                                            "mailer": MailerKind.SMTP,
                                                            "smtp_host": "smtp.x"}),
                            ctx=ctx, manage_context=False).routes
    )  # fmt: skip
