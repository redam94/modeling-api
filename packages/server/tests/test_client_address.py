"""The client address behind trusted proxies (MAPI_TRUSTED_PROXIES): per-address limits,
Sybil heuristics and audit fingerprints see the caller, not the load balancer or the web
container that forwards to the API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mapi_server.app import create_app
from mapi_server.clientaddr import client_address, parse_trusted_proxies
from mapi_server.context import ServerContext
from mapi_server.settings import MailerKind, Profile, Settings
from mapi_server_testkit.trust import RecordingMailer
from mapi_spec.trust import TERMS_VERSION
from pydantic import ValidationError

PROXY = "10.0.0.7"


def _body(email: str) -> dict[str, Any]:
    return {
        "email": email,
        "name": "Ana Analyst",
        "organisation": "Acme Retail",
        "accepted_terms": True,
        "terms_version": TERMS_VERSION,
    }


@pytest.mark.unit
def test_right_most_untrusted_hop_is_the_client() -> None:
    trusted = parse_trusted_proxies(["10.0.0.0/8", "35.191.0.0/16"])
    # a direct (untrusted) peer: its forwarded headers are ignored (a client could forge them)
    assert client_address("203.0.113.5", ["198.51.100.1"], trusted) == "203.0.113.5"
    # through the proxy: the hop it appended
    assert client_address(PROXY, ["203.0.113.5"], trusted) == "203.0.113.5"
    # a forged left-most entry is skipped: only the hops the proxies appended count
    assert client_address(PROXY, ["1.2.3.4, 203.0.113.5, 35.191.3.4"], trusted) == "203.0.113.5"
    # several headers are one list, in order
    assert client_address(PROXY, ["1.2.3.4", "203.0.113.9"], trusted) == "203.0.113.9"
    # no header, or only trusted hops: the left-most trusted hop
    assert client_address(PROXY, [], trusted) == PROXY
    assert client_address(PROXY, ["10.1.1.1, 10.2.2.2"], trusted) == "10.1.1.1"
    # junk stops the walk at the last good hop (never a non-address)
    assert client_address(PROXY, ["203.0.113.5, not-an-ip"], trusted) == PROXY
    assert client_address(PROXY, ["[2001:db8::1]:443"], trusted) == PROXY
    assert client_address(PROXY, [" 2001:db8::1 "], trusted) == "2001:db8::1"
    # nothing trusted: always the peer
    assert client_address(PROXY, ["203.0.113.5"], []) == PROXY
    assert client_address(None, ["203.0.113.5"], trusted) is None


@pytest.mark.unit
def test_settings_parse_and_require_a_proxy_decision_multi_tenant() -> None:
    s = Settings(database_url="postgresql://x/y", trusted_proxies="10.0.0.0/8, 192.0.2.1")
    assert s.trusted_proxies == ["10.0.0.0/8", "192.0.2.1/32"]
    assert Settings(database_url="postgresql://x/y", trusted_proxies="none").trusted_proxies == []
    with pytest.raises(ValidationError, match="not an address"):
        Settings(database_url="postgresql://x/y", trusted_proxies="10.0.0.0/8,example.com")
    with pytest.raises(ValidationError, match="MAPI_TRUSTED_PROXIES"):
        Settings(database_url="postgresql://x/y", profile="multi-tenant")
    direct = Settings(database_url="postgresql://x/y", profile="multi-tenant",
                      trusted_proxies="none")  # fmt: skip
    assert direct.trusted_proxies == []


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "profile": Profile.MULTI_TENANT,
            "signup_enabled": True,
            "mailer": MailerKind.SMTP,
            "smtp_host": "smtp.invalid",
            "signup_per_ip_per_hour": 1,
            "trusted_proxies": [f"{PROXY}/32"],
        }
    )


@pytest.fixture
async def proxied(ctx: ServerContext) -> AsyncIterator[httpx.AsyncClient]:
    """A client whose every request arrives from the proxy's address."""
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    transport = httpx.ASGITransport(app=app, client=(PROXY, 41000))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
        yield c


@pytest.mark.integration
async def test_clients_behind_one_proxy_get_their_own_limits(
    ctx: ServerContext, proxied: httpx.AsyncClient
) -> None:
    assert ctx.trust is not None
    ctx.trust.mailer = RecordingMailer()

    def xff(ip: str) -> dict[str, str]:
        return {"X-Forwarded-For": ip}

    r = await proxied.post("/auth/signup", json=_body("a@one.example"), headers=xff("203.0.113.5"))
    assert r.status_code == 202, r.text
    r = await proxied.post("/auth/signup", json=_body("b@two.example"), headers=xff("198.51.100.7"))
    assert r.status_code == 202, r.text
    # the same client again: its own limit (1 an hour) is spent
    r = await proxied.post("/auth/signup", json=_body("c@three.example"),
                           headers=xff("203.0.113.5"))  # fmt: skip
    assert r.status_code == 429 and "network address" in r.json()["detail"]


@pytest.mark.integration
async def test_an_untrusted_peer_cannot_forge_its_address(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    assert ctx.trust is not None
    ctx.trust.mailer = RecordingMailer()
    # the default test client connects from 127.0.0.1, which is not a trusted proxy here
    r = await client.post("/auth/signup", json=_body("a@one.example"),
                          headers={"X-Forwarded-For": "203.0.113.5"})  # fmt: skip
    assert r.status_code == 202
    r = await client.post("/auth/signup", json=_body("b@two.example"),
                          headers={"X-Forwarded-For": "198.51.100.7"})  # fmt: skip
    assert r.status_code == 429
