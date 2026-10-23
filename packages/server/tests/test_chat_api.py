"""The chat routes of members and clients, the chat settings and the kept conversations
(v0.20, plan D1, D4, D8, I-3 and WP5).

``eve`` (editor), ``vic`` (viewer) and ``ana`` (admin) are members of ``default/default``;
``cleo`` is a client with a grant on the published layout 2 dashboard. Chat is on for every
audience (:func:`test_chat_loop.chat_kit`). The app's chat model is ``app.state.chat_model``
(a :class:`FakeChatModel`; no test reaches a real model).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mapi_server.api.dashboard_chat import get_chat_model
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.chat.fake import FAKE_MODEL_ID, FakeChatModel, FakeStep
from mapi_server.services.chat.model import (
    ChatModelError,
    FunctionDecl,
    ModelReply,
    ModelTurn,
)
from mapi_server.services.quotas import KILL_SWITCHES_KEY
from mapi_server.services.runs import now
from mapi_server_testkit.identity import Caller, make_token, token_caller
from mapi_spec.admin import KillSwitches
from mapi_spec.chat import CHAT_SUGGESTIONS
from sqlalchemy import func, select, update
from test_chat_loop import chat_kit, enable_chat
from test_chat_loop import settings as settings  # chat_provider="fake", layout 1 creation on
from test_dashboards_crud import create_published

pytestmark = pytest.mark.integration

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
    return await chat_kit(ctx, client)


def _url(kit: dict[str, Any], tail: str = "") -> str:
    return f"/v1/dashboards/{kit['dash_id']}/chat{tail}"


def _ask(text: str = QUESTION, **over: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "text": text}], **over}


def _code(r: httpx.Response) -> str:
    return str(r.json()["code"])


def _reason(r: httpx.Response) -> str:
    """The reason of a ``409 CHAT_DISABLED``."""
    assert r.status_code == 409 and _code(r) == "CHAT_DISABLED", r.text
    issue = r.json()["errors"][0]
    assert issue["pointer"] == "/reason"
    return str(issue["message"])


async def _availability(caller: Caller, kit: dict[str, Any]) -> dict[str, Any]:
    r = await caller.get(_url(kit))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    body: dict[str, Any] = r.json()
    return body


async def _kill(ctx: ServerContext, chat_enabled: bool) -> None:
    async with ctx.db.session() as s:
        row = await s.get(m.PlatformSetting, KILL_SWITCHES_KEY)
        value = KillSwitches(chat_enabled=chat_enabled).model_dump(mode="json")
        if row is None:
            s.add(m.PlatformSetting(key=KILL_SWITCHES_KEY, value=value, updated_at=now()))
        else:
            row.value = value


async def _usage_count(ctx: ServerContext) -> int:
    async with ctx.db.session() as s:
        return int(
            (await s.execute(select(func.count()).select_from(m.DashboardChatUsage))).scalar_one()
        )


# ============================================================================== the model


def test_the_app_builds_its_chat_model_once(ctx: ServerContext) -> None:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    assert isinstance(app.state.chat_model, FakeChatModel)
    off = create_app(
        ctx.settings.model_copy(update={"chat_provider": "off"}), ctx=ctx, manage_context=False
    )
    assert off.state.chat_model is None


async def test_the_routes_use_the_apps_model(
    app: FastAPI, ctx: ServerContext, kit: dict[str, Any]
) -> None:
    model = FakeChatModel([FakeStep(text="Readout 2 shows sales of 1,234,567.")])
    app.state.chat_model = model
    r = await kit["eve"].post(_url(kit), json=_ask())
    assert r.status_code == 200, r.text
    assert len(model.calls) == 1
    assert r.json()["reply"]["text"] == "Readout 2 shows sales of 1,234,567."
    assert r.json()["usage"]["model"] == FAKE_MODEL_ID


def test_get_chat_model_reads_the_app_state() -> None:
    class _State:
        chat_model = None

    class _App:
        state = _State()

    class _Request:
        app = _App()

    assert get_chat_model(_Request()) is None  # type: ignore[arg-type]


# =========================================================================== availability


async def test_available_to_members_and_clients(kit: dict[str, Any]) -> None:
    for who in ("eve", "vic", "ana", "cleo"):
        got = await _availability(kit[who], kit)
        assert got["enabled_for_me"] is True and got["reason"] is None, who
        assert got["suggestions"] == list(CHAT_SUGGESTIONS)
        assert got["model"] == FAKE_MODEL_ID and got["keeps_transcripts"] is False


async def test_availability_reasons(app: FastAPI, ctx: ServerContext, kit: dict[str, Any]) -> None:
    eve = kit["eve"]
    # off
    await enable_chat(ctx, kit["dash_id"], enabled=False)
    got = await _availability(eve, kit)
    assert (got["enabled_for_me"], got["reason"], got["suggestions"]) == (False, "off", [])
    # audience
    await enable_chat(ctx, kit["dash_id"], aud_members=False)
    assert (await _availability(eve, kit))["reason"] == "audience"
    assert (await _availability(kit["cleo"], kit))["reason"] is None
    await enable_chat(ctx, kit["dash_id"])
    # killswitch
    await _kill(ctx, False)
    assert (await _availability(eve, kit))["reason"] == "killswitch"
    await _kill(ctx, True)
    # region
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Tenant).where(m.Tenant.id == "default").values(data_residency="eu")
        )
    assert (await _availability(eve, kit))["reason"] == "region"
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Tenant).where(m.Tenant.id == "default").values(data_residency="us")
        )
    # budget
    await enable_chat(ctx, kit["dash_id"], tokens_per_day=0)
    assert (await _availability(eve, kit))["reason"] == "budget"
    await enable_chat(ctx, kit["dash_id"])
    # provider: no model in this app
    app.state.chat_model = None
    got = await _availability(eve, kit)
    assert (got["enabled_for_me"], got["reason"], got["model"]) == (False, "provider", None)


async def test_a_layout_1_dashboard_has_no_chat(ctx: ServerContext, kit: dict[str, Any]) -> None:
    v1_id = await create_published(kit, "chat-v1")
    await enable_chat(ctx, v1_id)
    v1 = {**kit, "dash_id": v1_id}
    assert (await _availability(kit["eve"], v1))["reason"] == "layout"
    assert _reason(await kit["eve"].post(_url(v1), json=_ask())) == "layout"


# =================================================================================== turns


async def test_a_viewer_chats_and_continues_the_conversation(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    vic = kit["vic"]
    r = await vic.post(_url(kit), json=_ask())
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    first = r.json()
    assert first["reply"]["role"] == "assistant" and first["reply"]["sig"]
    assert "1,234,567" in first["reply"]["text"]
    assert [c["name"] for c in first["tool_calls"]] == ["get_latest_readout"]
    assert first["conversation_id"].startswith("cnv_") and first["transcript_kept"] is False
    assert await _usage_count(ctx) == 1
    follow = {
        "conversation_id": first["conversation_id"],
        "messages": [
            {"role": "user", "text": QUESTION},
            first["reply"],
            {"role": "user", "text": "And the readout before?"},
        ],
    }
    r = await vic.post(_url(kit), json=follow)
    assert r.status_code == 200, r.text
    assert r.json()["conversation_id"] == first["conversation_id"]
    # an edited answer is not ours any more
    forged = {**follow, "messages": [follow["messages"][0],
                                     {**first["reply"], "text": "Sales fell by 90%."},
                                     follow["messages"][2]]}  # fmt: skip
    r = await vic.post(_url(kit), json=forged)
    assert r.status_code == 400 and _code(r) == "CHAT_HISTORY_INVALID", r.text
    # a signature made for another caller is refused too
    r = await kit["eve"].post(_url(kit), json=follow)
    assert r.status_code == 400 and _code(r) == "CHAT_HISTORY_INVALID", r.text


async def test_a_client_with_a_grant_chats_and_without_it_gets_404(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    cleo = kit["cleo"]
    r = await cleo.post(_url(kit), json=_ask())
    assert r.status_code == 200, r.text
    r = await kit["ana"].delete(f"/v1/dashboards/{kit['dash_id']}/grants/{kit['users']['cleo'].id}")
    assert r.status_code in (200, 204), r.text
    r = await cleo.post(_url(kit), json=_ask())
    assert r.status_code == 404 and _code(r) == "NOT_FOUND", r.text
    r = await cleo.get(_url(kit))
    assert r.status_code == 404, r.text


async def test_chat_is_409_when_off(ctx: ServerContext, kit: dict[str, Any]) -> None:
    await enable_chat(ctx, kit["dash_id"], enabled=False)
    before = await _usage_count(ctx)
    assert _reason(await kit["eve"].post(_url(kit), json=_ask())) == "off"
    assert await _usage_count(ctx) == before


async def test_no_model_is_409_provider(app: FastAPI, kit: dict[str, Any]) -> None:
    app.state.chat_model = None
    assert _reason(await kit["eve"].post(_url(kit), json=_ask())) == "provider"
    # the credential is checked first: a hidden dashboard stays hidden
    other = "/v1/dashboards/dsh_" + "0" * 26 + "/chat"
    r = await kit["eve"].post(other, json=_ask())
    assert r.status_code == 404, r.text


async def test_an_api_token_cannot_chat(ctx: ServerContext, kit: dict[str, Any]) -> None:
    """A chat turn is web-session only (``SESSION_ONLY_OPERATIONS``)."""
    secret = await make_token(ctx, kit["users"]["eve"], "default", "default", ["models:read"])
    tok = token_caller(kit["eve"].client, secret)
    r = await tok.post(_url(kit), json=_ask())
    assert r.status_code == 403 and _code(r) == "FORBIDDEN", r.text
    assert await _usage_count(ctx) == 0


async def test_the_budget_answers_429_with_retry_after(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await enable_chat(ctx, kit["dash_id"], tokens_per_day=10)
    # the first model call spends past the limit: the next model call (in this turn, or
    # the next turn) is refused
    for _ in range(2):
        r = await kit["eve"].post(_url(kit), json=_ask())
        if r.status_code == 429:
            break
    assert r.status_code == 429 and _code(r) == "CHAT_BUDGET_EXHAUSTED", r.text
    assert int(r.headers["retry-after"]) >= 1
    assert (await _availability(kit["eve"], kit))["reason"] == "budget"


class _Failing:
    """A model whose provider failed with text that must never reach the caller."""

    model_id = "broken-model"

    async def generate(
        self,
        *,
        system: str,
        turns: Sequence[ModelTurn],
        tools: Sequence[FunctionDecl],
        timeout_s: float,
    ) -> ModelReply:
        err = ChatModelError("unavailable", status=500)
        err.args = ("upstream said: SECRET-PROVIDER-TEXT key=AIzaXYZ",)
        raise err


async def test_chat_unavailable_hides_the_provider_text(
    app: FastAPI, ctx: ServerContext, kit: dict[str, Any]
) -> None:
    app.state.chat_model = _Failing()
    r = await kit["eve"].post(_url(kit), json=_ask())
    assert r.status_code == 503 and _code(r) == "CHAT_UNAVAILABLE", r.text
    assert "SECRET" not in r.text and "AIza" not in r.text and "upstream" not in r.text


async def test_a_bad_request_is_refused(kit: dict[str, Any]) -> None:
    eve = kit["eve"]
    for body in (
        {"messages": []},
        {"messages": [{"role": "assistant", "text": "hi", "sig": "x" * 43}]},
        {"messages": [{"role": "user", "text": "x" * 2001}]},
        {"messages": [{"role": "user", "text": "hi"}], "dashboard_id": "dsh_" + "1" * 26},
    ):
        r = await eve.post(_url(kit), json=body)
        assert r.status_code == 422 and _code(r) == "SCHEMA_INVALID", (body, r.text)


# =============================================================================== settings


async def test_editors_read_and_change_the_settings_with_if_match(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    eve = kit["eve"]
    r = await eve.get(_url(kit, "/settings"))
    assert r.status_code == 200, r.text
    view = r.json()
    assert r.headers["etag"] == view["etag"]
    assert view["enabled"] is True and view["dashboard_id"] == kit["dash_id"]
    body = {
        "enabled": True,
        "audiences": {"members": True, "clients": False, "share_links": False},
        "keep_transcripts": True,
        "tokens_per_day": 1000,
        "share_tokens_per_day": 100,
    }
    r = await eve.put(_url(kit, "/settings"), json=body)
    assert r.status_code == 412 and _code(r) == "PRECONDITION_FAILED", r.text
    r = await eve.put(_url(kit, "/settings"), json=body, headers={"If-Match": '"c999"'})
    assert r.status_code == 412 and _code(r) == "PRECONDITION_FAILED", r.text
    r = await eve.put(_url(kit, "/settings"), json=body, headers={"If-Match": view["etag"]})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["version"] == view["version"] + 1 and r.headers["etag"] == new["etag"]
    assert new["audiences"]["clients"] is False and new["keep_transcripts"] is True
    # the second tab still holds the old etag
    r = await eve.put(_url(kit, "/settings"), json=body, headers={"If-Match": view["etag"]})
    assert r.status_code == 412, r.text
    # audited, without text
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "dashboard.chat_settings")
                )
            ).scalars()
        )
    assert len(rows) == 1
    # the change applies at once
    assert (await _availability(kit["cleo"], kit))["reason"] == "audience"


async def test_settings_are_for_editors(kit: dict[str, Any]) -> None:
    r = await kit["vic"].get(_url(kit, "/settings"))
    assert r.status_code == 403 and _code(r) == "FORBIDDEN", r.text
    r = await kit["cleo"].get(_url(kit, "/settings"))
    assert r.status_code == 404, r.text
    body = {
        "enabled": False,
        "audiences": {"members": True, "clients": True, "share_links": False},
        "keep_transcripts": False,
        "tokens_per_day": 1,
        "share_tokens_per_day": 1,
    }
    r = await kit["vic"].put(_url(kit, "/settings"), json=body, headers={"If-Match": '"c1"'})
    assert r.status_code == 403, r.text
    r = await kit["cleo"].put(_url(kit, "/settings"), json=body, headers={"If-Match": '"c1"'})
    assert r.status_code == 404, r.text
    bad = {**body, "tokens_per_day": 5_000_001}
    r = await kit["eve"].put(_url(kit, "/settings"), json=bad, headers={"If-Match": '"c1"'})
    assert r.status_code == 422, r.text


# ============================================================================ transcripts


async def test_kept_conversations_list_read_and_delete(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    await enable_chat(ctx, kit["dash_id"], keep_transcripts=True)
    assert (await _availability(kit["cleo"], kit))["keeps_transcripts"] is True
    r = await kit["cleo"].post(_url(kit), json=_ask())
    assert r.status_code == 200, r.text
    answer = r.json()
    assert answer["transcript_kept"] is True and "transcript_kept" in answer["notices"]
    cid = answer["conversation_id"]

    eve = kit["eve"]
    r = await eve.get(_url(kit, "/transcripts"))
    assert r.status_code == 200, r.text
    page = r.json()
    assert [t["conversation_id"] for t in page["items"]] == [cid]
    summary = page["items"][0]
    assert summary["principal_kind"] == "client" and summary["turns"] == 2
    r = await eve.get(_url(kit, f"/transcripts/{cid}"))
    assert r.status_code == 200, r.text
    turns = r.json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == QUESTION and turns[1]["tool_calls"] == ["get_latest_readout"]
    r = await eve.get(_url(kit, "/transcripts"), params={"limit": 0})
    assert r.status_code == 422, r.text

    # clients and viewers never see them
    r = await kit["cleo"].get(_url(kit, "/transcripts"))
    assert r.status_code == 404, r.text
    r = await kit["cleo"].delete(_url(kit, f"/transcripts/{cid}"))
    assert r.status_code == 404, r.text
    r = await kit["vic"].get(_url(kit, f"/transcripts/{cid}"))
    assert r.status_code == 403, r.text
    # nor does an API token (web session only)
    secret = await make_token(ctx, kit["users"]["eve"], "default", "default", ["models:read"])
    r = await token_caller(eve.client, secret).get(_url(kit, "/transcripts"))
    assert r.status_code == 403, r.text

    r = await eve.delete(_url(kit, f"/transcripts/{cid}"))
    assert r.status_code == 204, r.text
    r = await eve.get(_url(kit, f"/transcripts/{cid}"))
    assert r.status_code == 404, r.text
    r = await eve.delete(_url(kit, f"/transcripts/{cid}"))
    assert r.status_code == 404, r.text
    r = await eve.get(_url(kit, "/transcripts/not-a-conversation"))
    assert r.status_code == 422, r.text


async def test_nothing_is_kept_when_the_dashboard_does_not_keep_chats(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    r = await kit["eve"].post(_url(kit), json=_ask())
    assert r.status_code == 200 and r.json()["transcript_kept"] is False
    async with ctx.db.session() as s:
        n = (
            await s.execute(select(func.count()).select_from(m.DashboardChatTranscript))
        ).scalar_one()
    assert n == 0
    r = await kit["eve"].get(_url(kit, "/transcripts"))
    assert r.status_code == 200 and r.json()["items"] == []
