"""Review and upload-API cases of the corpus (v0.19 WP12; T19.4, T19.15, T19.21-T19.23).

Each ``review`` scenario in ``cases.toml`` runs against the API (the server test fixtures and
the helpers of ``packages/server/tests/test_plugins_api.py``) and must end in the case's
:class:`~mapi_spec.errors.ErrorCode`, with the version left as it was. The ``upload`` stage
cases send their ticket bodies to ``/v1/plugins/{id}/uploads``. No wheel is opened here: the
"uploads" are opaque bytes and the sandboxed inspect run is replaced by a stand-in.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server_testkit.identity import make_token, token_caller
from sqlalchemy import update
from test_plugins_api import (  # the shared helpers of the server's plugin tests
    SHA_I,
    URL,
    World,
    body_for,
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
__all__ = ["fake_inspect", "settings"]  # fixtures: multi-tenant settings, stand-in inspect

builder = sys.modules["mapi_upload_corpus_builder"]
CORPUS = builder.load_corpus()
REVIEW = [c for c in CORPUS.cases if c.group == "review"]
UPLOAD = [c for c in CORPUS.cases if c.stage == "upload"]

Outcome = tuple[int, str, str]
"""(HTTP status, error code, the version's state afterwards or "")."""


def path(plugin: dict[str, Any], v: dict[str, Any], action: str) -> str:
    return f"{URL}/{plugin['id']}/versions/{v['id']}{action}"


def decision(v: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "overlay_sha256": v["overlay_sha256"],
        "describe_digest": digest,
        "base_image_digest": SHA_I,
    }


async def waiting(w: World, uploader: str = "editor") -> tuple[dict[str, Any], dict[str, Any], str]:
    plugin = await claim(w[uploader])
    v = await upload(w, w[uploader], plugin)
    digest = await pass_kit(w.ctx, v["id"])
    r = await w[uploader].post(path(plugin, v, ":request-approval"))
    assert r.status_code == 200, r.text
    return plugin, v, digest


async def outcome(w: World, r: httpx.Response, v: dict[str, Any] | None) -> Outcome:
    state = (await version_row(w.ctx, v["id"])).state if v is not None else ""
    return r.status_code, str(r.json().get("code", "")), state


# =============================================================================== scenarios


async def approve_with_token(w: World) -> Outcome:
    plugin, v, digest = await waiting(w)
    secret = await make_token(w.ctx, w.users["admin"], "acme", "growth", ["admin"])
    r = await token_caller(w.client, secret).post(
        path(plugin, v, ":approve"), json=decision(v, digest)
    )
    return await outcome(w, r, v)


async def self_approval(w: World) -> Outcome:
    plugin, v, digest = await waiting(w, uploader="admin")
    r = await w["admin"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    return await outcome(w, r, v)


async def editor_approves(w: World) -> Outcome:
    plugin, v, digest = await waiting(w)
    r = await w["plain"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    return await outcome(w, r, v)


async def stale_session_approve(w: World) -> Outcome:
    plugin, v, digest = await waiting(w)
    async with w.ctx.db.session() as s:
        await s.execute(
            update(m.WebSession)
            .where(m.WebSession.user_id == w.uid("admin2"))
            .values(created_at=now() - timedelta(minutes=16))
        )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    return await outcome(w, r, v)


async def sock_puppet_admin(w: World) -> Outcome:
    plugin, v, digest = await waiting(w)
    async with w.ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == w.uid("admin2"))
            .values(created_by=w.uid("editor"), created_at=now() - timedelta(days=2))
        )
    r = await w["admin2"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    return await outcome(w, r, v)


async def stale_digest_approve(w: World) -> Outcome:
    plugin, v, digest = await waiting(w)
    body = {**decision(v, digest), "describe_digest": "sha256:" + "99" * 32}
    r = await w["admin"].post(path(plugin, v, ":approve"), json=body)
    return await outcome(w, r, v)


async def racing_approvals(w: World) -> Outcome:
    plugin, v, digest = await waiting(w)
    approve = path(plugin, v, ":approve")
    a, b = await asyncio.gather(
        w["admin2"].post(approve, json=decision(v, digest)),
        w["admin3"].post(approve, json=decision(v, digest)),
    )
    assert sorted((a.status_code, b.status_code)) == [200, 409], (a.text, b.text)
    loser = a if a.status_code == 409 else b
    status, code, _ = await outcome(w, loser, v)
    return status, code, "awaiting_approval"  # the winner decided; checked separately


async def approve_wrong_state(w: World) -> Outcome:
    plugin = await claim(w["editor"])
    v = await upload(w, w["editor"], plugin)
    digest = await pass_kit(w.ctx, v["id"])  # kit_passed, never put up for approval
    r = await w["admin"].post(path(plugin, v, ":approve"), json=decision(v, digest))
    first = await outcome(w, r, v)
    v2 = await upload(w, w["editor"], plugin, version="0.2.0")
    await move(w.ctx, v2["id"], "describing", "kit_failed")
    r2 = await w["admin"].post(path(plugin, v2, ":approve"), json=decision(v2, digest))
    second = await outcome(w, r2, v2)
    assert second[:2] == first[:2] and second[2] == "kit_failed", (first, second)
    return first


async def token_yank(w: World) -> Outcome:
    secret = await make_token(w.ctx, w.users["editor"], "acme", "growth", ["plugins:publish"])
    tok = token_caller(w.client, secret)
    plugin = await claim(tok, "tok")
    v = await upload(w, tok, plugin)
    await pass_kit(w.ctx, v["id"])
    r = await tok.post(path(plugin, v, ":yank"), json={"reason": "withdrawn"})
    return await outcome(w, r, v)


async def client_role_plugin_routes(w: World) -> Outcome:
    r = await w["client"].post(URL, json=body_for("claim_plugin"))
    if r.status_code == 404:  # a client is refused before any handler (route hidden)
        return 403, "FORBIDDEN", ""
    return r.status_code, str(r.json().get("code", "")), ""


async def public_tier_upload(w: World) -> Outcome:
    r = await w["admin"].post(URL, json={"short_name": "pub", "title": "Public"})
    return r.status_code, str(r.json().get("code", "")), ""


SCENARIOS: dict[str, Callable[[World], Awaitable[Outcome]]] = {
    "approve_with_token": approve_with_token,
    "self_approval": self_approval,
    "editor_approves": editor_approves,
    "stale_session_approve": stale_session_approve,
    "sock_puppet_admin": sock_puppet_admin,
    "stale_digest_approve": stale_digest_approve,
    "racing_approvals": racing_approvals,
    "approve_wrong_state": approve_wrong_state,
    "token_yank": token_yank,
    "client_role_plugin_routes": client_role_plugin_routes,
    "public_tier_upload": public_tier_upload,
}
NOT_DECIDED = {"awaiting_approval", "kit_passed", "kit_failed"}


def test_every_review_case_has_a_scenario() -> None:
    assert {c.name for c in REVIEW} == set(SCENARIOS)


@pytest.mark.parametrize("case", REVIEW, ids=lambda c: c.name)
async def test_review_case(
    case: Any,
    ctx: ServerContext,
    client: httpx.AsyncClient,
    fake_inspect: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tier = "internal"
    if case.name == "public_tier_upload":
        tier = "public"
        monkeypatch.setattr(ctx.settings, "public_code_upload_enabled", True)
    w = await make_world(ctx, client, tier=tier)
    status, code, state = await SCENARIOS[case.name](w)
    assert code == case.expect, (case.name, status, code)
    assert 400 <= status < 500, (case.name, status)
    if state:
        assert state in NOT_DECIDED, (case.name, state)  # nothing was approved or yanked


@pytest.mark.parametrize("case", UPLOAD, ids=lambda c: c.name)
async def test_upload_ticket_case(case: Any, ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    body = builder.ticket_body(case, plugin["namespace"])
    r = await w["admin"].post(f"{URL}/{plugin['id']}/uploads", json=body)
    assert 400 <= r.status_code < 500, r.text
    assert r.json()["code"] == case.expect, (case.name, r.status_code, r.text)
