"""Chat over a share link: ``GET|POST /v1/shared/chat`` (v0.20, plan D1, D9, I-3 and WP5).

The share session in ``X-Mapi-Share`` is the only credential: the share CORS rules drop
cookies and ``Authorization``, every answer is ``no-store``, and the dashboard comes from the
session. Revoking the link ends the conversation at its next request (``410``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.chat.fake import FAKE_MODEL_ID, FakeChatModel, FakeStep
from mapi_spec.chat import CHAT_SUGGESTIONS
from mapi_spec.dashboards import SHARE_SESSION_HEADER
from sqlalchemy import select, update
from test_chat_loop import chat_kit, enable_chat
from test_chat_loop import settings as settings  # chat_provider="fake", layout 1 creation on

pytestmark = pytest.mark.integration

URL = "/v1/shared/chat"
QUESTION = "What does the latest readout show?"


@pytest.fixture
def app(ctx: ServerContext) -> FastAPI:
    return create_app(ctx.settings, ctx=ctx, manage_context=False)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    kit = await chat_kit(ctx, client)
    kit["client"] = client
    return kit


def _h(kit: dict[str, Any]) -> dict[str, str]:
    return {SHARE_SESSION_HEADER: kit["share_session"]}


def _ask(text: str = QUESTION) -> dict[str, Any]:
    return {"messages": [{"role": "user", "text": text}]}


def _code(r: httpx.Response) -> str:
    return str(r.json()["code"])


def _no_store(r: httpx.Response) -> None:
    assert "no-store" in r.headers["cache-control"], r.headers
    assert "set-cookie" not in r.headers


async def test_a_share_session_chats(ctx: ServerContext, kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    r = await client.get(URL, headers=_h(kit))
    assert r.status_code == 200, r.text
    _no_store(r)
    got = r.json()
    assert got["enabled_for_me"] is True and got["model"] == FAKE_MODEL_ID
    assert got["suggestions"] == list(CHAT_SUGGESTIONS)
    r = await client.post(URL, json=_ask(), headers=_h(kit))
    assert r.status_code == 200, r.text
    _no_store(r)
    first = r.json()
    assert "1,234,567" in first["reply"]["text"] and first["reply"]["sig"]
    async with ctx.db.session() as s:
        rows = list((await s.execute(select(m.DashboardChatUsage))).scalars())
    assert [(u.principal_kind, u.share_id, u.dashboard_id) for u in rows] == [
        ("share", kit["share_id"], kit["dash_id"])
    ]
    follow = {
        "conversation_id": first["conversation_id"],
        "messages": [
            {"role": "user", "text": QUESTION},
            first["reply"],
            {"role": "user", "text": "And before that?"},
        ],
    }
    r = await client.post(URL, json=follow, headers=_h(kit))
    assert r.status_code == 200, r.text
    # the share link's signed answer is not a member's
    r = await kit["eve"].post(f"/v1/dashboards/{kit['dash_id']}/chat", json=follow)
    assert r.status_code == 400 and _code(r) == "CHAT_HISTORY_INVALID", r.text


async def test_share_links_audience_off_is_chat_disabled(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await enable_chat(ctx, kit["dash_id"], aud_share=False)
    client: httpx.AsyncClient = kit["client"]
    r = await client.get(URL, headers=_h(kit))
    assert r.status_code == 200, r.text
    assert (r.json()["enabled_for_me"], r.json()["reason"]) == (False, "audience")
    r = await client.post(URL, json=_ask(), headers=_h(kit))
    assert r.status_code == 409 and _code(r) == "CHAT_DISABLED", r.text
    assert r.json()["errors"][0] == {**r.json()["errors"][0], "pointer": "/reason",
                                     "message": "audience"}  # fmt: skip
    _no_store(r)


async def test_no_model_is_provider(app: FastAPI, kit: dict[str, Any]) -> None:
    app.state.chat_model = None
    client: httpx.AsyncClient = kit["client"]
    r = await client.get(URL, headers=_h(kit))
    assert r.status_code == 200 and r.json()["reason"] == "provider", r.text
    r = await client.post(URL, json=_ask(), headers=_h(kit))
    assert r.status_code == 409 and r.json()["errors"][0]["message"] == "provider", r.text


async def test_a_revoked_link_ends_the_conversation(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    client: httpx.AsyncClient = kit["client"]
    r = await client.post(URL, json=_ask(), headers=_h(kit))
    assert r.status_code == 200, r.text
    r = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}/shares/{kit['share_id']}:revoke")
    assert r.status_code in (200, 204), r.text
    r = await client.post(URL, json=_ask(), headers=_h(kit))
    assert r.status_code == 410 and _code(r) == "LINK_EXPIRED", r.text
    r = await client.get(URL, headers=_h(kit))
    assert r.status_code == 410, r.text


async def test_a_revoke_during_the_turn_stops_it(
    app: FastAPI, ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """The analyst revokes the link while the model works: the next tool call's session
    check ends the turn (``410``) and the second tool never runs."""

    async def revoke(n: int) -> None:
        if n == 1:
            url = f"/v1/dashboards/{kit['dash_id']}/shares/{kit['share_id']}:revoke"
            r = await kit["ana"].post(url)
            assert r.status_code in (200, 204), r.text

    script = [
        FakeStep(call=("get_latest_readout", {})),
        FakeStep(call=("list_readouts", {})),
        FakeStep(text="never"),
    ]
    model = FakeChatModel(script, on_generate=revoke)
    app.state.chat_model = model
    r = await kit["client"].post(URL, json=_ask(), headers=_h(kit))
    assert r.status_code == 410 and _code(r) == "LINK_EXPIRED", r.text
    assert len(model.calls) == 2
    async with ctx.db.session() as s:
        outcomes = list((await s.execute(select(m.DashboardChatUsage.outcome))).scalars())
    assert outcomes == ["refused"]


async def test_a_superseded_session_is_refused(ctx: ServerContext, kit: dict[str, Any]) -> None:
    """A new session version (the analyst rotated the link's sessions): the old session
    answers ``401`` (open the link again)."""
    async with ctx.db.session() as s:
        await s.execute(
            update(m.DashboardShare)
            .where(m.DashboardShare.id == kit["share_id"])
            .values(session_version=m.DashboardShare.session_version + 1)
        )
    r = await kit["client"].post(URL, json=_ask(), headers=_h(kit))
    assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED", r.text


async def test_cookies_and_bearer_tokens_are_not_share_credentials(kit: dict[str, Any]) -> None:
    """The member's cookie (and an Authorization header) are dropped on /v1/shared/*: no
    session, no chat."""
    eve = kit["eve"]
    r = await eve.post(URL, json=_ask())
    assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED", r.text
    _no_store(r)
    r = await eve.get(URL)
    assert r.status_code == 401, r.text
    r = await kit["client"].post(
        URL,
        json=_ask(),
        headers={"Authorization": "Bearer mapi_x", SHARE_SESSION_HEADER: "mps_forged"},
    )
    assert r.status_code == 401, r.text


async def test_the_web_origin_reads_answers_without_credentials(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    origin = ctx.settings.web_origin.rstrip("/")
    r = await kit["client"].post(URL, json=_ask(), headers={**_h(kit), "Origin": origin})
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == origin
    assert "access-control-allow-credentials" not in r.headers


async def test_the_session_is_checked_before_the_body(kit: dict[str, Any]) -> None:
    """No session: ``401`` whatever the body; with a session, a bad body is ``422`` with a
    body pointer."""
    client: httpx.AsyncClient = kit["client"]
    for body in ({}, {"messages": []}, {"messages": [{"role": "user", "text": "hi"}]}):
        r = await client.post(URL, json=body)
        assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED", r.text
    r = await client.post(URL, json={"messages": []}, headers=_h(kit))
    assert r.status_code == 422 and _code(r) == "SCHEMA_INVALID", r.text
    assert r.json()["errors"][0]["pointer"].startswith("/body/messages")
    r = await client.post(URL, content=b"not json", headers=_h(kit))
    assert r.status_code == 422 and _code(r) == "SCHEMA_INVALID", r.text
    r = await client.post(URL, json={**_ask(), "dashboard_id": "dsh_" + "1" * 26}, headers=_h(kit))
    assert r.status_code == 422, r.text  # the dashboard comes from the session only
