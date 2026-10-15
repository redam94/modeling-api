"""Browser sign-in with the development IdP (``MAPI_PROFILE=dev``), the BFF session, CSRF,
logout, and ID-token verification edge cases (signature, audience, nonce, expiry,
algorithm confusion, key rotation) for both the dev IdP and an HTTP provider."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mapi_server.auth.oidc import HttpOidcProvider, JwksCache, OidcClient, OidcError
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.devidp import DEV_ISSUER, DevIdP
from mapi_server.settings import Profile, Settings
from mapi_spec.api import Me
from mapi_spec.auth import LoginTransaction, SessionInfo
from mapi_spec.tenancy import PROJECT_HEADER
from sqlalchemy import select

pytestmark = pytest.mark.integration

API = "http://127.0.0.1:8000"
WEB = "http://127.0.0.1:5173"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.DEV})


async def sign_in(client: httpx.AsyncClient, email: str, return_to: str = "/runs") -> str:
    """Run the whole browser flow; returns the CSRF token."""
    r = await client.get("/auth/login", params={"return_to": return_to})
    assert r.status_code == 302, r.text
    authorize = r.headers["location"]
    assert authorize.startswith(f"{API}/dev/idp/authorize?")
    q = parse_qs(urlsplit(authorize).query)
    assert q["code_challenge_method"] == ["S256"] and q["nonce"] and q["state"]
    page = await client.get(authorize)
    assert page.status_code == 200 and email in page.text  # the account picker
    r = await client.get(authorize + "&" + httpx.QueryParams({"login_hint": email}).__str__())
    assert r.status_code == 302
    callback = r.headers["location"]
    assert callback.startswith(f"{API}/auth/callback?")
    r = await client.get(callback)
    assert r.status_code == 302, r.text
    # back to the web app (its own origin in local dev), never the API's
    assert r.headers["location"] == WEB + return_to
    cookies = r.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("mapi_session="))
    assert "HttpOnly" in session and "SameSite=Strict" in session and "Path=/" in session
    assert "Domain" not in session
    csrf = next(c for c in cookies if c.startswith("mapi_csrf="))
    assert "HttpOnly" not in csrf
    info = await client.get("/auth/session")
    token = info.headers["X-CSRF-Token"]
    assert SessionInfo.model_validate(info.json()).authenticated
    return token


async def test_dev_login_flow_session_csrf_and_logout(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    assert (await client.get("/v1/runs")).status_code == 401
    assert (await client.get("/v1/runs")).headers["www-authenticate"] == "Bearer"
    csrf = await sign_in(client, "analyst@dev.local")
    me = Me.model_validate((await client.get("/v1/me")).json())
    assert me.auth == "session" and not me.operator
    assert me.principal.kind == "user" and me.principal.email == "analyst@dev.local"
    assert me.principal.issuer == DEV_ISSUER
    [tenant] = me.tenants
    assert tenant.tenant.id == "default" and tenant.role is None
    assert [(p.project_id, p.role) for p in tenant.projects] == [("default", "runner")]

    project = {PROJECT_HEADER: "default/default"}
    body = {"name": "sales"}
    r = await client.post("/v1/datasets", json=body, headers=project)
    assert r.status_code == 403 and "X-CSRF-Token" in r.json()["detail"]
    r = await client.post(
        "/v1/datasets", json=body, headers={**project, "X-CSRF-Token": "wrong" * 8}
    )
    assert r.status_code == 403
    r = await client.post(
        "/v1/datasets",
        json=body,
        headers={**project, "X-CSRF-Token": csrf, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403 and "cross-origin" in r.json()["detail"]
    r = await client.post(
        "/v1/datasets",
        json=body,
        headers={**project, "X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403
    r = await client.post(
        "/v1/datasets",
        json=body,
        headers={**project, "X-CSRF-Token": csrf, "Origin": ctx.settings.web_origin},
    )
    assert r.status_code == 201, r.text
    # reads need no CSRF token
    assert (await client.get("/v1/datasets", headers=project)).status_code == 200

    assert (await client.post("/auth/logout")).status_code == 403  # CSRF applies here too
    r = await client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204
    assert (await client.get("/v1/me")).status_code == 401
    info = SessionInfo.model_validate((await client.get("/auth/session")).json())
    assert not info.authenticated
    async with ctx.db.session() as s:
        actions = [e.action for e in (await s.execute(select(m.AuditEvent))).scalars()]
    assert {"auth.login", "auth.logout"} <= set(actions)


async def test_first_dev_account_is_operator_and_tenant_admin(
    client: httpx.AsyncClient,
) -> None:
    await sign_in(client, "admin@dev.local")
    me = Me.model_validate((await client.get("/v1/me")).json())
    assert me.operator and me.tenants[0].role == "admin"
    assert (await client.get("/v1/admin/tenants")).status_code == 200


async def _cli_id_token(client: httpx.AsyncClient, email: str, *, client_id: str) -> str:
    """What ``mapi login`` does against the dev IdP: discovery, authorize with PKCE and a
    nonce to a loopback redirect, code exchange as a public client."""
    import base64
    import hashlib
    import secrets

    doc = (await client.get("/dev/idp/.well-known/openid-configuration")).json()
    assert doc["issuer"] == DEV_ISSUER and "none" in doc["token_endpoint_auth_methods_supported"]
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    redirect = "http://127.0.0.1:53682/callback"
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "state": "st", "nonce": "n0nce", "code_challenge": challenge,
        "code_challenge_method": "S256", "scope": "openid email", "login_hint": email,
    }  # fmt: skip
    r = await client.get(doc["authorization_endpoint"], params=params)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(redirect + "?")
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    r = await client.post(
        doc["token_endpoint"],
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
              "code_verifier": verifier, "client_id": client_id},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    token: str = r.json()["id_token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["aud"] == client_id and claims["nonce"] == "n0nce"
    return token


async def test_cli_id_tokens_are_accepted_as_bearers(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    token = await _cli_id_token(client, "analyst@dev.local", client_id="mapi-cli")
    r = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    me = Me.model_validate(r.json())
    assert me.auth == "session" and me.principal.email == "analyst@dev.local"
    project = {PROJECT_HEADER: "default/default", "Authorization": f"Bearer {token}"}
    # a bearer needs no CSRF token (no cookie is involved)
    r = await client.post("/v1/datasets", json={"name": "from-cli"}, headers=project)
    assert r.status_code == 201, r.text
    # the same person as a browser sign-in: one user row
    async with ctx.db.session() as s:
        users = [
            u for u in (await s.execute(select(m.User))).scalars()
            if u.email == "analyst@dev.local"
        ]  # fmt: skip
    assert len(users) == 1


async def test_bearer_id_tokens_need_a_cli_audience_and_a_valid_signature(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    assert isinstance(ctx.devidp, DevIdP)
    # the BFF's own client id is not a bearer audience
    bff = ctx.devidp.id_token("analyst@dev.local", nonce="x")
    r = await client.get("/v1/me", headers={"Authorization": f"Bearer {bff}"})
    assert r.status_code == 401
    # unknown public clients cannot even get a code
    r = await client.get(
        "/dev/idp/authorize",
        params={"response_type": "code", "client_id": "evil-cli",
                "redirect_uri": "http://127.0.0.1:1/callback", "state": "s",
                "code_challenge": "x" * 43, "code_challenge_method": "S256",
                "login_hint": "analyst@dev.local"},
    )  # fmt: skip
    assert r.status_code == 422
    # a loopback redirect only: a CLI client id cannot send codes elsewhere
    r = await client.get(
        "/dev/idp/authorize",
        params={"response_type": "code", "client_id": "mapi-cli",
                "redirect_uri": "https://evil.example/callback", "state": "s",
                "code_challenge": "x" * 43, "code_challenge_method": "S256",
                "login_hint": "analyst@dev.local"},
    )  # fmt: skip
    assert r.status_code == 422
    forged = jwt.encode(
        {"iss": DEV_ISSUER, "aud": "mapi-cli", "sub": "dev|x", "iat": int(time.time()),
         "exp": int(time.time()) + 60},
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        algorithm="RS256",
        headers={"kid": ctx.devidp.kid},
    )  # fmt: skip
    r = await client.get("/v1/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    # with no CLI client ids configured, only API tokens are bearers
    token = await _cli_id_token(client, "analyst@dev.local", client_id="mapi-cli")
    ctx.settings = ctx.settings.model_copy(update={"oidc_cli_client_ids": []})
    r = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


async def test_callback_is_single_use_and_checks_state(client: httpx.AsyncClient) -> None:
    r = await client.get("/auth/login")
    authorize = r.headers["location"]
    r = await client.get(authorize + "&login_hint=viewer%40dev.local")
    callback = r.headers["location"]
    # a forged state is refused (and burns the transaction)
    forged = callback.replace("state=", "state=x")
    r = await client.get(forged)
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    assert "mapi_login=" in r.headers["set-cookie"]  # cleared
    r = await client.get(callback)  # the real one cannot be replayed afterwards
    assert r.status_code == 401
    # without any login cookie
    fresh = httpx.AsyncClient(transport=client._transport, base_url=API)  # type: ignore[attr-defined]
    async with fresh:
        assert (await fresh.get(callback)).status_code == 401
    # an IdP error is reported as a failed sign-in, never reflected
    r = await client.get("/auth/login")
    r = await client.get("/auth/callback?error=<script>&state=x")
    assert r.status_code == 401 and "<script>" not in r.text


async def test_unknown_account_and_bad_redirect_are_refused(client: httpx.AsyncClient) -> None:
    r = await client.get("/auth/login")
    authorize = r.headers["location"]
    r = await client.get(authorize + "&login_hint=mallory%40evil.example")
    assert r.status_code == 401
    evil = authorize.replace("redirect_uri=", "redirect_uri=https%3A%2F%2Fevil.example%2F")
    r = await client.get(evil + "&login_hint=viewer%40dev.local")
    assert r.status_code == 422 and "location" not in r.headers


async def test_open_redirects_are_blocked(client: httpx.AsyncClient) -> None:
    for target in ("https://evil.example/x", "//evil.example", "/\\evil.example"):
        await client.get("/auth/login", params={"return_to": target})
        r = await client.get("/auth/login", params={"return_to": target})
        authorize = r.headers["location"]
        r = await client.get(authorize + "&login_hint=viewer%40dev.local")
        r = await client.get(r.headers["location"])
        assert r.status_code == 302 and r.headers["location"] == WEB + "/"


async def test_secure_cookie_attributes(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    ctx.settings = ctx.settings.model_copy(update={"cookie_secure": True})
    r = await client.get("/auth/login")
    login = r.headers["set-cookie"]
    assert login.startswith("__Host-mapi_login=") and "Secure" in login
    assert "SameSite=Lax" in login and "HttpOnly" in login and "Path=/" in login
    value = login.split(";")[0].split("=", 1)[1]
    r = await client.get(r.headers["location"] + "&login_hint=viewer%40dev.local")
    r = await client.get(r.headers["location"], headers={"Cookie": f"__Host-mapi_login={value}"})
    assert r.status_code == 302
    session = next(
        c for c in r.headers.get_list("set-cookie") if c.startswith("__Host-mapi_session=")
    )
    for attr in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
        assert attr in session
    assert "Domain" not in session


# ======================================================================== token verification


async def _reason(awaitable: Any) -> str:
    """The (logged, never returned) reason an ID token or exchange was refused."""
    with pytest.raises(OidcError) as info:
        await awaitable
    assert info.value.detail == "sign-in failed"  # clients only ever see this
    return info.value.reason


def _tx() -> LoginTransaction:
    from mapi_server.services.runs import now

    return LoginTransaction.start(now=now())


async def test_dev_idp_tokens_are_verified_like_any_provider() -> None:
    idp = DevIdP(users=["a@dev.local"], base_url=API, redirect_uri=f"{API}/auth/callback")
    rp = OidcClient(idp, client_id=idp.client_id, redirect_uri=idp.redirect_uri,
                    algorithms=("RS256",), allow_http=True)  # fmt: skip
    tx = _tx()
    ident = await rp.verify_id_token(idp.id_token("a@dev.local", nonce=tx.nonce), nonce=tx.nonce)
    assert ident.email == "a@dev.local" and ident.email_verified and ident.issuer == DEV_ISSUER
    assert "nonce" in await _reason(
        rp.verify_id_token(idp.id_token("a@dev.local", nonce="other"), nonce=tx.nonce)
    )
    assert "InvalidAudience" in await _reason(
        rp.verify_id_token(
            idp.id_token("a@dev.local", nonce=tx.nonce, audience="someone-else"), nonce=tx.nonce
        )
    )
    good = idp.id_token("a@dev.local", nonce=tx.nonce)
    head, payload, sig = good.split(".")
    tampered = ".".join([head, payload[:-4] + ("AAAA" if payload[-4:] != "AAAA" else "BBBB"), sig])
    await _reason(rp.verify_id_token(tampered, nonce=tx.nonce))
    now_ = int(time.time())
    claims = {"iss": DEV_ISSUER, "sub": "x", "aud": idp.client_id, "iat": now_ - 900,
              "exp": now_ - 600, "nonce": tx.nonce}  # fmt: skip
    expired = jwt.encode(claims, idp._key, algorithm="RS256", headers={"kid": idp.kid})
    assert "ExpiredSignature" in await _reason(rp.verify_id_token(expired, nonce=tx.nonce))
    unsigned = jwt.encode({**claims, "exp": now_ + 600}, None, algorithm="none")
    assert "not allowed" in await _reason(rp.verify_id_token(unsigned, nonce=tx.nonce))
    # algorithm confusion: an HMAC "signed" with the public key is refused outright
    assert "not allowed" in await _reason(
        rp.verify_id_token(
            jwt.encode(
                {**claims, "exp": now_ + 600}, "public-key-as-secret" * 2, algorithm="HS256"
            ),
            nonce=tx.nonce,
        )
    )


async def test_code_exchange_enforces_pkce_and_single_use() -> None:
    idp = DevIdP(users=["a@dev.local"], base_url=API, redirect_uri=f"{API}/auth/callback")
    tx = _tx()
    code = idp.grant(
        email="a@dev.local", client_id=idp.client_id, redirect_uri=idp.redirect_uri,
        nonce=tx.nonce, code_challenge=tx.code_challenge, code_challenge_method="S256",
    )  # fmt: skip
    common = {"code": code, "redirect_uri": idp.redirect_uri, "client_id": idp.client_id}
    wrong = _tx().code_verifier
    assert "PKCE" in await _reason(idp.exchange_code(**common, code_verifier=wrong))
    # the failed attempt consumed the code: the right verifier is now too late
    right = tx.code_verifier
    assert "invalid or expired" in await _reason(idp.exchange_code(**common, code_verifier=right))


class _RotatingProvider:
    """Serves one JWKS, then (after rotation) another; counts fetches."""

    def __init__(self, issuer: str) -> None:
        self.issuer = issuer
        self.keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048)]
        self.fetches = 0
        self.cache = JwksCache(self._fetch)

    async def _fetch(self) -> Mapping[str, Any]:
        self.fetches += 1
        out = []
        for i, key in enumerate(self.keys):
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            jwk.update(kid=f"k{i}", alg="RS256", use="sig")
            out.append(jwk)
        return {"keys": out}

    def sign(self, i: int, nonce: str) -> str:
        now_ = int(time.time())
        claims = {
            "iss": self.issuer, "sub": "s", "aud": "client", "iat": now_, "exp": now_ + 60,
            "nonce": nonce, "email": "x@y.test", "email_verified": True,
        }  # fmt: skip
        return jwt.encode(claims, self.keys[i], algorithm="RS256", headers={"kid": f"k{i}"})


async def test_jwks_rotation_refetches_unknown_key_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    import mapi_server.auth.oidc as oidc

    provider = _RotatingProvider("https://idp.test")
    verify = oidc.verify_jwt
    tx = _tx()
    claims = await verify(
        provider.sign(0, tx.nonce), jwks=provider.cache.get, algorithms=("RS256",),
        audience="client", issuers=("https://idp.test",),
    )  # fmt: skip
    assert claims["sub"] == "s" and provider.fetches == 1
    provider.keys.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    monkeypatch.setattr(oidc, "JWKS_REFRESH_MIN_S", 0.0)
    await verify(
        provider.sign(1, tx.nonce), jwks=provider.cache.get, algorithms=("RS256",),
        audience="client", issuers=("https://idp.test",),
    )  # fmt: skip
    assert provider.fetches == 2
    assert "issuer" in await _reason(
        verify(
            provider.sign(0, tx.nonce), jwks=provider.cache.get, algorithms=("RS256",),
            audience="client", issuers=("https://other.test",),
        )
    )  # fmt: skip


async def test_http_provider_discovery_exchange_and_verification() -> None:
    issuer = "https://idp.test"
    signer = _RotatingProvider(issuer)
    jwks = await signer._fetch()
    tx = _tx()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "jwks_uri": f"{issuer}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=jwks)
        if request.url.path == "/token":
            seen["form"] = parse_qs(request.content.decode())
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id_token": signer.sign(0, tx.nonce)})
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpOidcProvider(issuer, client_secret="s3cret", http=http)
    rp = OidcClient(provider, client_id="client", redirect_uri="https://app.test/auth/callback")
    url = await rp.authorize_url(tx)
    q = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{issuer}/authorize?") and q["code_challenge"] == [tx.code_challenge]
    ident = await rp.complete(code="the-code", tx=tx)
    assert ident.subject == "s" and ident.email == "x@y.test"
    assert seen["form"]["code_verifier"] == [tx.code_verifier]
    assert seen["form"]["grant_type"] == ["authorization_code"]
    assert seen["auth"].startswith("Basic ")
    await http.aclose()

    def wrong_issuer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"issuer": "https://evil.test", "authorization_endpoint":
                                         "x", "token_endpoint": "y", "jwks_uri": "z"})  # fmt: skip

    bad = HttpOidcProvider(
        issuer, http=httpx.AsyncClient(transport=httpx.MockTransport(wrong_issuer))
    )
    assert "different issuer" in await _reason(bad.metadata())
    rp_plain = OidcClient(bad, client_id="c", redirect_uri="https://app/cb")
    await _reason(rp_plain.authorize_url(tx))
