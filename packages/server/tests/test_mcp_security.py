"""The MCP endpoint's credential and transport rules (v0.20, plan D11, D12, D14, D18, D19).

Only a dashboard read token or a share session opens ``/v1/mcp``: no cookie, no query
credential, no ordinary API token. Browsers are refused unless an operator allowed their
origin, and then never with credentials; the ``Host`` must be the API's own. A credential is
checked again on every request, so a revoked link, a removed grant or a readout made with
uploaded code stops the next call. Results and credentials never reach the logs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.runs import now
from mapi_server.settings import Settings
from mapi_server_testkit.identity import make_token
from mapi_spec.mcp import mcp_tools
from sqlalchemy import delete, select, update
from test_mcp_endpoint import (
    MCP,
    MCP_SETTINGS,
    bearer,
    call,
    mcp_client,
    mcp_kit,
    post_mcp,
    read_token,
    rpc,
    share_session,
    shared,
    text_of,
)
from test_token_context import pin_uploaded

pytestmark = pytest.mark.integration

AGENT: Final = "https://agent.example"
WEB: Final = "http://127.0.0.1:5173"
PING: Final = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            **MCP_SETTINGS,
            "mcp_allowed_origins": [AGENT],
            "web_origin": WEB,
        }
    )


@pytest.fixture
async def client(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    async with mcp_client(ctx) as c:
        yield c


@pytest.fixture
async def kit(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    return await mcp_kit(ctx, client)


def _code(r: httpx.Response) -> str:
    return str(r.json()["code"])


# ============================================================================ credentials


async def test_a_session_cookie_alone_is_not_a_credential(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    cleo = kit["cleo"]
    cookie = "; ".join(f"{k}={v}" for k, v in cleo.cookies.items())
    r = await post_mcp(client, {**cleo.headers, "Cookie": cookie}, PING)
    assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED", r.text
    assert r.headers["www-authenticate"] == "Bearer"
    r = await post_mcp(client, {}, PING)
    assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED"


async def test_credentials_in_the_query_string_are_refused(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    secret = await read_token(kit["cleo"], kit["dash_id"])
    for query in (f"token={secret}", "key=abc", "access_token=x", f"q={secret}", "share=mps_x"):
        r = await post_mcp(client, {}, PING, params=dict([query.split("=", 1)]))
        assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED", query
        assert secret not in r.text
    # even next to a valid header (the URL may already be in a log)
    r = await post_mcp(client, bearer(secret), PING, params={"token": secret})
    assert r.status_code == 401
    # mcp-5: a token anywhere in a value (not only at its start), or a Bearer value
    for params in ({"x": f"Bearer {secret}"}, {"q": f"foo,{secret}"},
                   {"config": '{"env": {"MAPI_DASHBOARD_TOKEN": "' + secret + '"}}'},
                   {"x": "Bearer abc"}, {f"k{secret}": "1"}):  # fmt: skip
        r = await post_mcp(client, bearer(secret), PING, params=params)
        assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED", params
        assert secret not in r.text
    # an unrelated parameter is ignored
    r = await post_mcp(client, bearer(secret), PING, params={"trace": "1"})
    assert r.status_code == 200, r.text


async def test_ordinary_api_tokens_are_refused(ctx: ServerContext, kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    for scopes in (["models:read"], ["admin"], ["runs:create"]):
        secret = await make_token(ctx, kit["users"]["ana"], "default", "default", scopes)
        r = await post_mcp(client, bearer(secret), PING)
        assert r.status_code == 403 and _code(r) == "FORBIDDEN", (scopes, r.text)


async def test_bad_read_tokens_are_unauthenticated(ctx: ServerContext, kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    for value in ("Bearer mapi_nope", "Bearer ", "Basic Zm9vOmJhcg==", "mapi_x"):
        r = await post_mcp(client, {"Authorization": value}, PING)
        assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED", value
    secret = await read_token(kit["cleo"], kit["dash_id"])
    assert (await post_mcp(client, bearer(secret), PING)).status_code == 200
    # expired, then revoked: the next call is refused
    async with ctx.db.session() as s:
        await s.execute(
            update(m.ApiToken)
            .where(m.ApiToken.dashboard_id == kit["dash_id"])
            .values(created_at=now() - timedelta(days=2), expires_at=now() - timedelta(seconds=1))
        )
    assert (await post_mcp(client, bearer(secret), PING)).status_code == 401
    secret = await read_token(kit["cleo"], kit["dash_id"], "second")
    r = await kit["cleo"].get(f"/v1/dashboards/{kit['dash_id']}/read-tokens")
    token_id = next(t["id"] for t in r.json() if t["revoked_at"] is None and t["name"] == "second")
    r = await kit["cleo"].delete(f"/v1/dashboards/{kit['dash_id']}/read-tokens/{token_id}")
    assert r.status_code == 204, r.text
    assert (await post_mcp(client, bearer(secret), PING)).status_code == 401


async def test_one_credential_at_a_time(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    secret = await read_token(kit["cleo"], kit["dash_id"])
    _, session = await share_session(kit, kit["dash_id"])
    r = await post_mcp(client, {**bearer(secret), **shared(session)}, PING)
    assert r.status_code == 401 and _code(r) == "UNAUTHENTICATED"
    r = await post_mcp(client, shared("mps_forged"), PING)
    assert r.status_code == 401


async def test_a_revoked_share_link_stops_the_next_call(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    share_id, session = await share_session(kit, kit["dash_id"])
    assert (await call(client, shared(session), "get_latest_readout"))["isError"] is False
    r = await kit["ana"].post(f"/v1/dashboards/{kit['dash_id']}/shares/{share_id}:revoke")
    assert r.status_code == 200, r.text
    r = await post_mcp(
        client,
        shared(session),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "get_latest_readout", "arguments": {}}},
    )  # fmt: skip
    assert r.status_code == 410 and _code(r) == "LINK_EXPIRED", r.text


async def test_a_removed_grant_kills_the_clients_token(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    client: httpx.AsyncClient = kit["client"]
    secret = await read_token(kit["cleo"], kit["dash_id"])
    assert (await call(client, bearer(secret), "get_dashboard"))["isError"] is False
    async with ctx.db.session() as s:
        await s.execute(
            delete(m.DashboardGrant).where(m.DashboardGrant.dashboard_id == kit["dash_id"])
        )
    r = await post_mcp(client, bearer(secret), PING)
    assert r.status_code == 404 and _code(r) == "NOT_FOUND", r.text


async def test_uploaded_code_is_refused_to_tokens_and_links(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """``mcp_tool_on_overlay_readout``: once the dashboard shows anything made with uploaded
    code, client tokens, member tokens and share sessions are all refused (plan D11)."""
    client: httpx.AsyncClient = kit["client"]
    credentials = {
        "client token": bearer(await read_token(kit["cleo"], kit["dash_id"])),
        "member token": bearer(await read_token(kit["eve"], kit["dash_id"])),
        "share session": shared((await share_session(kit, kit["dash_id"]))[1]),
    }
    for headers in credentials.values():
        assert (await call(client, headers, "get_readout", {"seq": 2}))["isError"] is False
    await pin_uploaded(ctx, kit)
    for who, headers in credentials.items():
        for msg in (
            PING,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "get_readout", "arguments": {"seq": 2}}},
        ):  # fmt: skip
            r = await post_mcp(client, headers, msg)
            assert r.status_code == 403, (who, r.text)
            assert _code(r) == "UPLOADED_CODE_NOT_SHAREABLE", who


# ================================================================================ transport


async def test_browser_origins_are_refused_unless_allowed(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    for origin in ("https://evil.example", WEB, "null"):
        r = await post_mcp(client, {**headers, "Origin": origin}, PING)
        assert r.status_code == 403 and _code(r) == "FORBIDDEN", origin
        assert "access-control-allow-origin" not in r.headers
    r = await post_mcp(client, {**headers, "Origin": AGENT}, PING)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == AGENT
    assert "access-control-allow-credentials" not in r.headers
    assert r.headers["vary"] == "Origin"


async def test_cors_preflight_never_allows_credentials(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    ask = {"Access-Control-Request-Method": "POST",
           "Access-Control-Request-Headers": "authorization, content-type"}  # fmt: skip
    r = await client.options(MCP, headers={**ask, "Origin": AGENT})
    assert r.status_code == 204, r.text
    assert r.headers["access-control-allow-origin"] == AGENT
    assert r.headers["access-control-allow-methods"] == "POST"
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in r.headers
    for origin in (WEB, "https://evil.example"):  # the web app's credentialed CORS never applies
        r = await client.options(MCP, headers={**ask, "Origin": origin})
        assert r.status_code == 403, origin
        assert "access-control-allow-origin" not in r.headers
        assert "access-control-allow-credentials" not in r.headers


async def test_get_and_delete_are_not_allowed(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    for method in ("GET", "DELETE", "PUT"):
        r = await client.request(method, MCP, headers=headers)
        assert r.status_code == 405, method
        assert r.headers["allow"] == "POST, OPTIONS"


async def test_every_answer_is_no_store_and_sets_no_cookie(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    for r in (
        await post_mcp(client, headers, PING),
        await post_mcp(client, {}, PING),
        await post_mcp(client, headers, {"jsonrpc": "2.0", "method": "notifications/x"}),
        await client.get(MCP),
    ):
        assert r.headers["cache-control"] == "no-store", r.status_code
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "set-cookie" not in r.headers


async def test_an_oversized_message_is_refused(kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    headers = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": ""}}
    base = len(httpx.Request("POST", "/", json=msg).content)
    msg["params"] = {"pad": "x" * (65_536 - base + 1)}
    r = await client.post(MCP, json=msg, headers=headers)
    assert len(r.request.content) == 65_537
    assert r.status_code == 413 and _code(r) == "PAYLOAD_TOO_LARGE", r.text
    msg["params"] = {"pad": "x" * (65_536 - base)}
    r = await client.post(MCP, json=msg, headers=headers)
    assert r.status_code == 200, r.text


async def test_requests_are_limited_per_credential(ctx: ServerContext, kit: dict[str, Any]) -> None:
    client: httpx.AsyncClient = kit["client"]
    first = bearer(await read_token(kit["cleo"], kit["dash_id"]))
    second = bearer(await read_token(kit["cleo"], kit["dash_id"], "second"))
    ctx.settings = ctx.settings.model_copy(update={"mcp_rate_per_minute": 3})
    for _ in range(3):
        assert (await post_mcp(client, first, PING)).status_code == 200
    r = await post_mcp(client, first, PING)
    assert r.status_code == 429 and _code(r) == "RATE_LIMITED", r.text
    assert 1 <= int(r.headers["retry-after"]) <= 60
    # another token has its own budget
    assert (await post_mcp(client, second, PING)).status_code == 200


async def test_tool_descriptions_carry_no_tenant_strings(
    ctx: ServerContext, kit: dict[str, Any]
) -> None:
    """``tool_desc_has_no_dashboard_title``: the tool surface is fixed text."""
    client: httpx.AsyncClient = kit["client"]
    async with ctx.db.session() as s:
        dash = await s.get(m.Dashboard, kit["dash_id"])
        assert dash is not None
        rev = await s.get(m.DashboardRevision, dash.published_revision_id)
        assert rev is not None
        strings = {str(rev.spec.get(k)) for k in ("title", "client_name") if rev.spec.get(k)}
        strings |= {dash.name, "Readout 2", "chat-fc"}
    assert strings
    for headers in (
        bearer(await read_token(kit["cleo"], kit["dash_id"])),
        shared((await share_session(kit, kit["dash_id"]))[1]),
    ):
        init = {"protocolVersion": "2025-11-25"}
        r1 = await post_mcp(
            client, headers, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init}
        )
        listed = await rpc(client, headers, "tools/list")
        assert listed["result"]["tools"] == mcp_tools()
        for text in (r1.text, str(listed)):
            for value in strings:
                assert value not in text, value


async def test_credentials_and_results_never_reach_the_logs(
    ctx: ServerContext, kit: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    client: httpx.AsyncClient = kit["client"]
    secret = await read_token(kit["cleo"], kit["dash_id"])
    _, session = await share_session(kit, kit["dash_id"])
    caplog.set_level(logging.DEBUG)
    result = await call(client, bearer(secret), "get_readout", {"seq": 2})
    marker = text_of(result)["title"]
    await call(client, shared(session), "get_readout", {"seq": 2})
    await post_mcp(client, bearer(secret + "x"), PING)  # a failure is audited, not logged
    await post_mcp(client, {}, PING, params={"token": secret})
    # the server's records (the test's own HTTP client logs the URLs it requests)
    records = [r for r in caplog.records if r.name != "httpx"]
    text = "".join(f"{r.getMessage()} {r.__dict__}" for r in records)
    assert secret not in text and secret.split("_")[2] not in text
    assert session not in text
    assert marker not in text
    async with ctx.db.session() as s:
        audits = (await s.execute(select(m.AuditEvent))).scalars().all()
    assert all(secret not in str(a.details) for a in audits)


async def test_the_host_must_be_the_apis_own(ctx: ServerContext, kit: dict[str, Any]) -> None:
    """``mcp_bad_host``: with the generic host check off (``*``), MCP still answers only for
    the API's own host names (421 otherwise)."""
    ctx.settings = ctx.settings.model_copy(update={"allowed_hosts": ["*"]})
    secret = await read_token(kit["cleo"], kit["dash_id"])
    async with mcp_client(ctx) as c:  # a new app: the middleware reads the hosts at start
        r = await post_mcp(c, bearer(secret), PING)
        assert r.status_code == 200, r.text
        for host in ("evil.example", "127.0.0.1.evil.example:8000", ""):
            r = await post_mcp(c, {**bearer(secret), "Host": host}, PING)
            assert r.status_code == 421, (host, r.text)
            assert r.json()["error"]["code"] == -32600 and r.json()["id"] is None


async def test_nothing_is_answered_without_a_credential(client: httpx.AsyncClient) -> None:
    for msg in (PING, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, [PING]):
        r = await post_mcp(client, {}, msg)
        assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
        assert "tools" not in r.text
