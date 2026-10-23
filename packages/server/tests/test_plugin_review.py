"""Second-person review of uploaded code (v0.19 D13-D15, T19.15): ask for approval, approve,
reject, yank, and who is notified.

Approval needs a web session (never a token), a tenant admin who is not the uploader, a
sign-in at most 15 minutes old, a membership the uploader did not create in the last 7
days, and the digests the page showed; of two racing decisions one wins.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.plugins import notify
from mapi_server.services.runs import now
from mapi_server_testkit.identity import make_token, token_caller
from sqlalchemy import select, update
from test_plugins_api import (
    SHA_I,
    URL,
    World,
    claim,
    fake_inspect,
    make_world,
    move,
    pass_kit,
    settings,
    upload,
    version_row,
)

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]
__all__ = ["fake_inspect", "settings"]  # fixtures shared with test_plugins_api


@pytest.fixture
def webhooks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def emit(session: Any, *, tenant_id: str, event_type: Any, data: Any) -> None:
        sent.append((str(event_type), dict(data)))

    monkeypatch.setattr(notify, "emit_tenant_event", emit)
    return sent


async def waiting(w: World, uploader: str = "editor") -> tuple[dict[str, Any], dict[str, Any], str]:
    """A version of ``uploader`` waiting for review: (plugin, version, describe digest)."""
    plugin = await claim(w[uploader])
    v = await upload(w, w[uploader], plugin)
    digest = await pass_kit(w.ctx, v["id"])
    r = await w[uploader].post(f"{URL}/{plugin['id']}/versions/{v['id']}:request-approval")
    assert r.status_code == 200 and r.json()["state"] == "awaiting_approval", r.text
    return plugin, v, digest


def decision(v: dict[str, Any], digest: str, note: str | None = None) -> dict[str, Any]:
    body = {
        "overlay_sha256": v["overlay_sha256"],
        "describe_digest": digest,
        "base_image_digest": SHA_I,
    }
    if note is not None:
        body["note"] = note
    return body


def path(plugin: dict[str, Any], v: dict[str, Any], action: str) -> str:
    return f"{URL}/{plugin['id']}/versions/{v['id']}{action}"


async def test_request_approval_rules(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    fake_inspect: list[str],
    webhooks: list[tuple[str, dict[str, Any]]],
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["editor"])
    v = await upload(w, w["editor"], plugin)
    ask = path(plugin, v, ":request-approval")
    r = await w["editor"].post(ask)
    assert r.status_code == 409 and r.json()["code"] == "PLUGIN_STATE_CONFLICT"  # inspecting
    await pass_kit(ctx, v["id"])
    assert (await w["plain"].post(ask)).status_code == 403  # another editor
    assert (await w["viewer"].post(ask)).status_code == 403
    r = await w["editor"].post(ask)
    assert r.status_code == 200 and r.json()["state"] == "awaiting_approval"
    assert r.json()["scope"] == "private"
    assert webhooks == [
        (
            "plugin.version.awaiting_approval",
            {
                "plugin_id": plugin["id"],
                "plugin_version_id": v["id"],
                "namespace": plugin["namespace"],
                "version": "0.1.0",
                "state": "awaiting_approval",
                "overlay_sha256": v["overlay_sha256"],
            },
        )
    ]
    # the admins are mailed, the uploader is not; no uploaded text in the mail
    to = sorted(msg.to for msg in w.mail.sent)
    assert to == ["admin2@acme.test", "admin3@acme.test", "admin@acme.test"]
    assert all("Acme tools" not in msg.body + msg.subject for msg in w.mail.sent)


async def test_approve(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    fake_inspect: list[str],
    webhooks: list[tuple[str, dict[str, Any]]],
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w)
    approve = path(plugin, v, ":approve")
    w.mail.sent.clear()
    webhooks.clear()
    r = await w["admin"].post(approve, json=decision(v, digest))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "approved" and body["scope"] == "tenant"
    [record] = body["approvals"]
    assert record["decision"] == "approve" and record["approver_id"] == w.uid("admin")
    assert record["describe_digest"] == digest and record["base_image_digest"] == SHA_I
    assert [e for e, _ in webhooks] == ["plugin.version.approved"]
    to = sorted(msg.to for msg in w.mail.sent)
    assert to == ["admin2@acme.test", "admin3@acme.test", "admin@acme.test", "editor@acme.test"]
    async with ctx.db.session() as s:
        audit = (
            await s.execute(
                select(m.AuditEvent.details).where(m.AuditEvent.action == "plugin.approve")
            )
        ).scalar_one()
    assert audit["approver_id"] == w.uid("admin") and audit["uploader_id"] == w.uid("editor")
    # decided once: a second approval is a state conflict
    r = await w["admin2"].post(approve, json=decision(v, digest))
    assert r.status_code == 409 and r.json()["code"] == "PLUGIN_STATE_CONFLICT"


async def test_nobody_approves_their_own_upload(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w, uploader="admin")
    r = await w["admin"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 403 and r.json()["code"] == "SELF_APPROVAL_FORBIDDEN", r.text
    assert "this plugin version" in r.json()["detail"]
    r = await w["admin"].post(path(plugin, v, ":reject"), json=decision(v, digest, "no"))
    assert r.status_code == 403 and r.json()["code"] == "SELF_APPROVAL_FORBIDDEN"
    assert (await version_row(ctx, v["id"])).state == "awaiting_approval"


async def test_only_tenant_admins_with_a_session_approve(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w)
    approve = path(plugin, v, ":approve")
    for name in ("plain", "viewer"):
        r = await w[name].post(approve, json=decision(v, digest))
        assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", (name, r.text)
    secret = await make_token(ctx, w.users["admin"], "acme", "growth", ["admin"])
    r = await token_caller(client, secret).post(approve, json=decision(v, digest))
    assert r.status_code == 403, r.text
    assert (await version_row(ctx, v["id"])).state == "awaiting_approval"


async def test_a_stale_sign_in_must_reauthenticate(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.WebSession)
            .where(m.WebSession.user_id == w.uid("admin2"))
            .values(auth_time=now() - timedelta(minutes=16))
        )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 401 and r.json()["code"] == "REAUTH_REQUIRED", r.text
    # review-flow-3: a session made just now by a silent SSO round trip (no fresh auth_time,
    # or none at all) is not a recent sign-in
    async with ctx.db.session() as s:
        await s.execute(
            update(m.WebSession)
            .where(m.WebSession.user_id == w.uid("admin2"))
            .values(created_at=now(), auth_time=None)
        )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 401 and r.json()["code"] == "REAUTH_REQUIRED", r.text


async def test_an_admin_the_uploader_just_added_cannot_review(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == w.uid("admin2"))
            .values(created_by=w.uid("editor"), created_at=now() - timedelta(days=2))
        )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", r.text
    assert "less than 7 days" in r.json()["detail"]
    async with ctx.db.session() as s:
        denied = (
            await s.execute(
                select(m.AuditEvent.outcome, m.AuditEvent.details).where(
                    m.AuditEvent.action == "plugin.approve"
                )
            )
        ).one()
    assert denied[0] == "denied"
    assert denied[1]["approver_id"] == w.uid("admin2")
    assert denied[1]["uploader_id"] == w.uid("editor")
    # an old membership is fine
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == w.uid("admin2"))
            .values(created_at=now() - timedelta(days=8))
        )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 200, r.text


async def test_a_fresh_promotion_by_the_uploader_cannot_review(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    """review-flow-2 (a): an old membership the uploader promoted to tenant admin today."""
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w, uploader="admin")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == w.uid("admin2"))
            .values(role="viewer", created_at=now() - timedelta(days=60), created_by=None)
        )
    r = await w["admin"].patch(f"/v1/tenants/acme/members/{w.uid('admin2')}",
                               json={"role": "admin"})  # fmt: skip
    assert r.status_code == 200, r.text
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", r.text
    # an admin the uploader did not bring in still approves
    r = await w["admin3"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 200, r.text


async def test_an_admin_added_through_a_chain_cannot_review(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    """review-flow-2 (b): the uploader adds C, C adds B, B approves at once."""
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w, uploader="admin")
    async with ctx.db.session() as s:
        for who, by in (("admin3", "admin"), ("admin2", "admin3")):
            await s.execute(
                update(m.Membership)
                .where(m.Membership.principal_id == w.uid(who))
                .values(created_by=w.uid(by), created_at=now() - timedelta(minutes=5))
            )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", r.text
    # C's own grant is old: the chain is broken
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == w.uid("admin3"))
            .values(created_at=now() - timedelta(days=8))
        )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    assert r.status_code == 200, r.text


async def test_stale_digests_are_a_conflict(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w)
    approve = path(plugin, v, ":approve")
    other = "sha256:" + "99" * 32
    for field in ("overlay_sha256", "describe_digest", "base_image_digest"):
        body = {**decision(v, digest), field: other}
        r = await w["admin"].post(approve, json=body)
        assert r.status_code == 409 and r.json()["code"] == "PLUGIN_STATE_CONFLICT", field
    assert (await version_row(ctx, v["id"])).state == "awaiting_approval"


async def test_two_racing_approvals_one_wins(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w)
    approve = path(plugin, v, ":approve")
    a, b = await asyncio.gather(
        w["admin2"].post(approve, json=decision(v, digest)),
        w["admin3"].post(approve, json=decision(v, digest)),
    )
    assert sorted((a.status_code, b.status_code)) == [200, 409], (a.text, b.text)
    async with ctx.db.session() as s:
        n = len(list((await s.execute(select(m.PluginApproval.id))).scalars()))
    assert n == 1


async def test_reject_needs_a_note(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    fake_inspect: list[str],
    webhooks: list[tuple[str, dict[str, Any]]],
) -> None:
    w = await make_world(ctx, client)
    plugin, v, digest = await waiting(w)
    reject = path(plugin, v, ":reject")
    r = await w["admin"].post(reject, json=decision(v, digest))
    assert r.status_code == 422 and r.json()["errors"][0]["pointer"] == "/note", r.text
    r = await w["admin"].post(reject, json=decision(v, digest, "Sends data to ‮example.org"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "rejected" and body["scope"] == "none"
    assert body["purge_after"] is not None
    assert body["approvals"][0]["note"] == "Sends data to example.org"  # bidi stripped
    assert webhooks[-1][0] == "plugin.version.rejected"


async def test_yank(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    fake_inspect: list[str],
    webhooks: list[tuple[str, dict[str, Any]]],
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["editor"])
    v = await upload(w, w["editor"], plugin)
    yank = path(plugin, v, ":yank")
    r = await w["editor"].post(yank, json={"reason": "withdrawn"})
    assert r.status_code == 409, r.text  # still inspecting: nothing to withdraw
    await pass_kit(ctx, v["id"])
    r = await w["plain"].post(yank, json={"reason": "withdrawn"})
    assert r.status_code == 403, r.text
    r = await w["editor"].post(yank, json={"reason": "superseded", "note": "Use 0.2.0."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "yanked" and body["yank_reason"] == "superseded"
    assert body["yank_note"] == "Use 0.2.0." and body["purge_after"] is not None
    assert webhooks[-1][0] == "plugin.version.yanked"
    # an admin yanks another person's approved version
    v2 = await upload(w, w["editor"], plugin, version="0.2.0")
    await pass_kit(ctx, v2["id"])
    await move(ctx, v2["id"], "awaiting_approval", "approved")
    r = await w["admin"].post(path(plugin, v2, ":yank"), json={"reason": "withdrawn"})
    assert r.status_code == 200, r.text
    # an approved-then-yanked version stays readable by the team (to explain failing runs)
    assert (await w["viewer"].get(path(plugin, v2, ""))).status_code == 200
    assert (await w["viewer"].get(path(plugin, v, ""))).status_code == 404
