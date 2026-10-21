"""Dashboard share links (v0.16, plan D13): tokens, passcodes, sessions, revocation, CORS.

A link is made by ``ana`` (admin) on a dashboard of ``default/default`` (the helpers of
``test_dashboards_crud``); everything after that is anonymous: no cookie, no Authorization.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dashboards.shares import (
    DashboardShareRevoker,
    hash_passcode,
    share_token_hash,
    verify_passcode,
)
from mapi_server.services.runs import now
from mapi_server.settings import Profile, Settings
from mapi_spec.dashboards import DASHBOARD_SHARE_TOKEN_RE, SHARE_SESSION_HEADER, SHARE_SESSION_RE
from sqlalchemy import select, update
from test_dashboards_crud import LAYOUT1, create_published, dashboard_kit, dashboard_spec

pytestmark = pytest.mark.integration

WEB = "http://127.0.0.1:5173"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"profile": Profile.MULTI_TENANT, "web_origin": WEB, **LAYOUT1}
    )


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    kit = await dashboard_kit(ctx, client)
    kit["dash_id"] = await create_published(kit)
    return kit


async def _share(kit: dict[str, Any], **body: Any) -> dict[str, Any]:
    r = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}/shares", json=body)
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _session(client: httpx.AsyncClient, token: str, passcode: str | None = None) -> Any:
    body: dict[str, Any] = {"token": token}
    if passcode is not None:
        body["passcode"] = passcode
    return await client.post("/v1/share-sessions", json=body)


def _shared(session: str) -> dict[str, str]:
    return {SHARE_SESSION_HEADER: session}


# ================================================================================== creating


async def test_the_token_is_shown_once_and_stored_as_a_keyed_hash(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    created = await _share(kit, label="Board pack", expires_in_days=7)
    token = created["token"]
    assert DASHBOARD_SHARE_TOKEN_RE.match(token)
    assert created["url_fragment"] == f"/s#{token}"
    assert created["display"] == f"mpd_…{token[-4:]}"
    assert created["status"] == "active" and created["allowed_inputs"] == []
    listed = (await kit["ana"].get(f"/v1/dashboards/{kit['dash_id']}/shares")).json()["items"]
    assert len(listed) == 1 and "token" not in listed[0]
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardShare, created["id"])
        assert row is not None
        assert row.token_hash == share_token_hash(ctx.secrets.token_pepper, token)
        assert token not in row.token_hash and row.passcode_hash is None


async def test_links_with_inputs_need_the_runner_role(kit: dict[str, Any]) -> None:
    path = f"/v1/dashboards/{kit['dash_id']}/shares"
    r = await kit["eve"].post(path, json={"allowed_inputs": ["whatif"]})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert (await kit["eve"].post(path, json={})).status_code == 201  # read-only: an editor may
    assert (await kit["vic"].post(path, json={})).status_code == 403
    r = await kit["ana"].post(path, json={"allowed_inputs": ["whatif"]})
    assert r.status_code == 201 and r.json()["allowed_inputs"] == ["whatif"]
    r = await kit["ana"].post(path, json={"passcode": "short"})
    assert r.status_code == 422  # passcodes are 8-128 characters


async def test_drafts_cannot_be_shared(kit: dict[str, Any]) -> None:
    r = await kit["eve"].post("/v1/dashboards", json={"name": "wip", "spec": dashboard_spec()})
    r = await kit["ana"].post(f"/v1/dashboards/{r.json()['id']}/shares", json={})
    assert r.status_code == 409 and r.json()["code"] == "DASHBOARD_NOT_PUBLISHED"


# ================================================================================== sessions


async def test_a_session_opens_the_published_dashboard(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await _share(kit, allowed_inputs=["whatif"])
    r = await _session(client, created["token"])
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in r.headers
    body = r.json()
    assert SHARE_SESSION_RE.match(body["session"])
    dash = body["dashboard"]
    assert dash["id"] == kit["dash_id"] and dash["ends_at"] is not None
    assert dash["inputs"]["whatif"] is True and dash["inputs"]["actuals"] is False
    r = await client.get("/v1/shared/dashboard", headers=_shared(body["session"]))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Weekly media results"
    r = await client.post("/v1/share-sessions:refresh", headers=_shared(body["session"]))
    assert r.status_code == 200 and SHARE_SESSION_RE.match(r.json()["session"])
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardShare, created["id"])
        assert row is not None and row.views == 1 and row.last_viewed_at is not None


async def test_cookies_and_authorization_are_never_looked_at(
    client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await _share(kit)
    junk = {"Authorization": "Bearer mapi_junk", "Cookie": "mapi_session=forged"}
    r = await client.post("/v1/share-sessions", json={"token": created["token"]}, headers=junk)
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    r = await client.get("/v1/shared/dashboard", headers={**_shared(session), **junk})
    assert r.status_code == 200, r.text
    # a signed-in session gives no access to share routes either
    r = await kit["ana"].get("/v1/shared/dashboard")
    assert r.status_code == 401


async def test_unknown_forged_and_superseded_sessions(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await _share(kit)
    unknown = "mpd_" + "0" * 40
    assert (await _session(client, unknown)).status_code == 404
    assert (await _session(client, "not-a-token")).status_code == 404
    session = (await _session(client, created["token"])).json()["session"]
    forged = session[:-2] + ("AA" if not session.endswith("AA") else "BB")
    for bad in (forged, "mps_x.y", ""):
        r = await client.get("/v1/shared/dashboard", headers=_shared(bad))
        assert r.status_code == 401, (bad, r.text)
    assert (await client.get("/v1/shared/dashboard")).status_code == 401
    async with ctx.db.session() as s:  # e.g. an operator ended every session of the link
        await s.execute(
            update(m.DashboardShare)
            .where(m.DashboardShare.id == created["id"])
            .values(session_version=m.DashboardShare.session_version + 1)
        )
    r = await client.get("/v1/shared/dashboard", headers=_shared(session))
    assert r.status_code == 401
    assert (await _session(client, created["token"])).status_code == 200


async def test_revoking_ends_the_link_on_the_next_request(
    client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await _share(kit)
    session = (await _session(client, created["token"])).json()["session"]
    path = f"/v1/dashboards/{kit['dash_id']}/shares/{created['id']}:revoke"
    for _ in range(2):  # idempotent
        r = await kit["ana"].post(path)
        assert r.status_code == 200 and r.json()["status"] == "revoked"
    r = await client.get("/v1/shared/dashboard", headers=_shared(session))
    assert r.status_code == 410 and r.json()["code"] == "LINK_EXPIRED"
    assert (await _session(client, created["token"])).status_code == 410


async def test_expiry_suspension_and_unpublishing_end_the_link(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await _share(kit)
    session = (await _session(client, created["token"])).json()["session"]
    async with ctx.db.session() as s:
        await s.execute(update(m.Tenant).where(m.Tenant.id == "default").values(status="suspended"))
    assert (await client.get("/v1/shared/dashboard", headers=_shared(session))).status_code == 410
    assert (await _session(client, created["token"])).status_code == 410
    async with ctx.db.session() as s:
        await s.execute(update(m.Tenant).where(m.Tenant.id == "default").values(status="active"))
        await s.execute(
            update(m.DashboardShare)
            .where(m.DashboardShare.id == created["id"])
            .values(expires_at=now() - timedelta(seconds=1))
        )
    assert (await client.get("/v1/shared/dashboard", headers=_shared(session))).status_code == 410
    assert (await _session(client, created["token"])).status_code == 410
    other = await _share(kit)
    session = (await _session(client, other["token"])).json()["session"]
    assert (await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}:archive")).status_code == 200
    assert (await client.get("/v1/shared/dashboard", headers=_shared(session))).status_code == 410


# ================================================================================= passcodes


def test_passcode_hashes_are_salted_scrypt() -> None:
    pepper = b"p" * 32
    a, b = hash_passcode(pepper, "correct horse"), hash_passcode(pepper, "correct horse")
    assert a != b and a.startswith("scrypt$14$8$1$")
    assert verify_passcode(pepper, "correct horse", a)
    assert not verify_passcode(pepper, "correct hors", a)
    assert not verify_passcode(b"q" * 32, "correct horse", a)
    assert not verify_passcode(pepper, "x", "scrypt$99$8$1$AA$AA")
    assert not verify_passcode(pepper, "x", "garbage")


async def test_wrong_passcodes_lock_the_link(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await _share(kit, passcode="correct horse")
    assert created["has_passcode"] is True
    r = await _session(client, created["token"])
    assert r.status_code == 401 and r.json()["code"] == "SHARE_PASSCODE_REQUIRED"
    for _ in range(4):
        r = await _session(client, created["token"], "wrong passcode")
        assert r.status_code == 401 and r.json()["code"] == "SHARE_PASSCODE_INVALID"
    r = await _session(client, created["token"], "wrong passcode")
    assert r.status_code == 429 and r.json()["code"] == "SHARE_LOCKED"
    assert int(r.headers["retry-after"]) >= 60
    r = await _session(client, created["token"], "correct horse")  # locked: even the right one
    assert r.status_code == 429 and r.json()["code"] == "SHARE_LOCKED"
    listed = (await kit["ana"].get(f"/v1/dashboards/{kit['dash_id']}/shares")).json()["items"]
    assert listed[0]["locked_until"] is not None
    async with ctx.db.session() as s:
        audited = [
            bool((e.details or {}).get("locked"))
            for e in (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "dashboard.share_unlock_failed",
                        m.AuditEvent.resource_id == created["id"],
                    )
                )
            ).scalars()
        ]
        await s.execute(
            update(m.DashboardShare)
            .where(m.DashboardShare.id == created["id"])
            .values(locked_until=now() - timedelta(seconds=1))
        )
    # bounded (anyone with the link can cause these): one wrong-passcode row a minute, and
    # the lockout itself always (its own budget)
    assert sorted(audited) == [False, True]
    r = await _session(client, created["token"], "correct horse")
    assert r.status_code == 200, r.text
    async with ctx.db.session() as s:
        row = await s.get(m.DashboardShare, created["id"])
        assert row is not None and row.failed_attempts == 0 and row.locked_until is None


# ================================================================================ rate, CORS


async def test_the_address_limit_counts_before_the_token_is_read(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    created = await _share(kit)
    ctx.settings.rate_limits_enabled = True
    ctx.settings.share_ip_rate_per_minute = 3
    try:
        for _ in range(3):
            assert (await _session(client, "garbage")).status_code == 404
        r = await _session(client, created["token"])  # a valid token, but the address is spent
        assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED"
        assert "retry-after" in r.headers
    finally:
        ctx.settings.rate_limits_enabled = False


async def test_input_bodies_are_capped_before_they_are_parsed(
    client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    """No session, no token: an oversized or deeply nested body is refused (413 / 422)
    before FastAPI reads it into a model, on the public and the member routes."""
    from mapi_server.api.shares import ACTUALS_BODY_MAX_BYTES, INPUT_BODY_MAX_BYTES

    big = b'{"multipliers": {"tv": 1.0}, "label": "' + b"x" * INPUT_BODY_MAX_BYTES + b'"}'
    for path in ("/v1/shared/scenarios", "/v1/shared/whatif:evaluate",
                 f"/v1/dashboards/{kit['dash_id']}/scenarios"):  # fmt: skip
        r = await client.post(path, content=big, headers={"content-type": "application/json"})
        assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE", (path, r.text)
    huge = b'{"inputs": "' + b"x" * (ACTUALS_BODY_MAX_BYTES + 1) + b'"}'
    r = await client.post(
        "/v1/shared/actuals", content=huge, headers={"content-type": "application/json"}
    )
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    deep = b"[" * 200 + b"]" * 200
    r = await client.post(
        "/v1/shared/actuals", content=deep, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    # a small body still reaches the session check
    r = await client.post("/v1/shared/scenarios", json={"multipliers": {"tv": 1.0}})
    assert r.status_code == 401


def test_the_address_budget_leaves_room_for_the_share_page_itself() -> None:
    """The page polls a pending readout (with a backoff) besides its own reads: several
    viewers behind one office address must fit the per-address budget."""
    fields = Settings.model_fields
    assert fields["share_ip_rate_per_minute"].default >= 120
    assert fields["share_rate_per_minute"].default >= fields["share_ip_rate_per_minute"].default


async def test_cors_allows_the_web_origin_without_credentials(
    client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    pre = {
        "Origin": WEB,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": SHARE_SESSION_HEADER,
    }
    r = await client.options("/v1/share-sessions", headers=pre)
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == WEB
    assert SHARE_SESSION_HEADER in r.headers["access-control-allow-headers"]
    assert "access-control-allow-credentials" not in r.headers
    r = await client.options("/v1/shared/dashboard", headers={**pre, "Origin": "https://evil.test"})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
    created = await _share(kit)
    r = await client.post(
        "/v1/share-sessions", json={"token": created["token"]}, headers={"Origin": WEB}
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == WEB
    assert "access-control-allow-credentials" not in r.headers


# ======================================================================== tenant deletion


async def test_tenant_deletion_revokes_every_link(
    ctx: ServerContext, client: httpx.AsyncClient, kit: dict[str, Any]
) -> None:
    assert ctx.trust is not None
    assert list(ctx.trust.revoker_names()).count("dashboard_shares") == 1
    created = await _share(kit)
    session = (await _session(client, created["token"])).json()["session"]
    async with ctx.db.session() as s:
        assert await DashboardShareRevoker().revoke(s, "default", now=now()) == 1
    assert (await client.get("/v1/shared/dashboard", headers=_shared(session))).status_code == 410
