"""Interactive sign-in: discovery, loopback PKCE, device code, refresh and stored sessions.

The identity provider is faked with respx; the "browser" is a function that follows the
authorization URL by calling the real loopback listener with urllib (which respx does not
intercept).
"""

from __future__ import annotations

import base64
import json
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import respx
from mapi_client import Client, ClientConfig, ConfigError, CredentialStore, OidcError
from mapi_client.auth import BearerAuth, default_auth
from mapi_client.config import load_config
from mapi_client.credentials import OidcSession
from mapi_client.oidc import (
    DEFAULT_CLIENT_ID,
    DeviceCode,
    DeviceLogin,
    OidcTokenProvider,
    ProviderMetadata,
    check_id_token,
    decode_jwt_claims,
    discover,
    login,
    logout,
)
from mapi_spec.api import Me
from mapi_spec.auth import pkce_challenge

ISSUER = "https://idp.test"
API = "https://api.test"
TOKEN_URL = f"{ISSUER}/token"


def b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def jwt(**claims: Any) -> str:
    base = {
        "iss": ISSUER,
        "aud": DEFAULT_CLIENT_ID,
        "sub": "110169484474386276334",
        "email": "ana@acme.example",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    base.update(claims)
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(base)}.c2ln"


DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": TOKEN_URL,
    "device_authorization_endpoint": f"{ISSUER}/device",
    "revocation_endpoint": f"{ISSUER}/revoke",
}


@dataclass
class FakeIdp:
    router: respx.MockRouter
    nonce: str | None = None
    challenge: str | None = None
    forms: list[dict[str, str]] = field(default_factory=list)
    id_claims: dict[str, Any] = field(default_factory=dict)

    def token_response(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        if form["grant_type"] == "authorization_code":
            if pkce_challenge(form["code_verifier"]) != self.challenge or form["code"] != "c0de":
                return httpx.Response(400, json={"error": "invalid_grant"})
            claims = {"nonce": self.nonce, **self.id_claims}
            return httpx.Response(
                200,
                json={"id_token": jwt(**claims), "refresh_token": "r1", "expires_in": 3600},
            )
        if form["grant_type"] == "refresh_token":
            return httpx.Response(200, json={"id_token": jwt(email="new@acme.example")})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


@pytest.fixture
def idp() -> Iterator[FakeIdp]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{ISSUER}/.well-known/openid-configuration").respond(200, json=DISCOVERY)
        fake = FakeIdp(router)
        router.post(TOKEN_URL).mock(side_effect=fake.token_response)
        yield fake


@pytest.fixture
def cfg(isolated_config: Path) -> ClientConfig:
    return ClientConfig(
        base_url=API,
        oidc_issuer=ISSUER,
        profile="prod",
        credentials_file=isolated_config.parent / "credentials.json",
    )


def browser(idp: FakeIdp, *, query: dict[str, str] | None = None) -> Callable[[str], None]:
    """Follow the authorization URL like a user who signs in successfully."""

    def open_url(url: str) -> None:
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert parts.netloc == "idp.test" and parts.path == "/authorize"
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        assert q["scope"] == "openid email profile"
        assert urlsplit(q["redirect_uri"]).hostname == "127.0.0.1"
        idp.nonce, idp.challenge = q["nonce"], q["code_challenge"]
        params = query if query is not None else {"code": "c0de", "state": q["state"]}
        with urllib.request.urlopen(f"{q['redirect_uri']}?{urlencode(params)}") as resp:
            page = resp.read().decode()
            assert resp.headers["Cache-Control"] == "no-store"
            assert "default-src 'none'" in resp.headers["Content-Security-Policy"]
        assert "c0de" not in page  # nothing from the query is reflected

    return open_url


# ---------------------------------------------------------------------------- loopback


def test_loopback_login_stores_a_private_session(idp: FakeIdp, cfg: ClientConfig) -> None:
    shown: list[str] = []
    session = login(cfg, open_browser=browser(idp), show_url=shown.append, timeout=10)
    assert session.email == "ana@acme.example" and session.api_url == API
    assert session.refresh_token == "r1"
    assert "r1" not in repr(session) and session.id_token not in repr(session)
    assert shown and shown[0].startswith(f"{ISSUER}/authorize?")
    exchange = idp.forms[0]
    assert exchange["client_id"] == DEFAULT_CLIENT_ID and "client_secret" not in exchange
    path = cfg.credentials_file
    assert path is not None and stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = CredentialStore(path).load("prod")
    assert stored is not None and stored.id_token == session.id_token


def test_the_stored_session_authenticates_the_client(
    idp: FakeIdp, cfg: ClientConfig, example: Callable[..., dict[str, Any]]
) -> None:
    session = login(cfg, open_browser=browser(idp), timeout=10)
    me = idp.router.get(f"{API}/v1/me").respond(200, json=example(Me))
    with Client(config=cfg) as client:
        client.me()
    assert me.calls.last.request.headers["Authorization"] == f"Bearer {session.id_token}"
    # never sent to another API, even for the same profile
    other = replace(cfg, base_url="https://elsewhere.test")
    assert default_auth(other) is None
    # an explicit token wins
    auth = default_auth(replace(cfg, token="mapi_explicit"))
    assert isinstance(auth, BearerAuth) and auth.token() == "mapi_explicit"


def test_client_secret_is_sent_when_configured(idp: FakeIdp, cfg: ClientConfig) -> None:
    idp.id_claims = {"aud": "desktop"}
    login(
        replace(cfg, oidc_client_id="desktop", oidc_client_secret="s3"),
        open_browser=browser(idp),
        timeout=10,
    )
    assert idp.forms[-1]["client_secret"] == "s3" and idp.forms[-1]["client_id"] == "desktop"


def test_a_redirect_with_the_wrong_state_is_ignored(idp: FakeIdp, cfg: ClientConfig) -> None:
    good = browser(idp)

    def open_url(url: str) -> None:
        redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{redirect}?code=evil&state=forged")
        assert err.value.code == 400
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(redirect.replace("/callback", "/other"))
        flood = "&".join(f"p{i}=x" for i in range(40))
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{redirect}?{flood}")
        assert err.value.code == 400
        good(url)

    session = login(cfg, open_browser=open_url, timeout=10)
    assert session.subject == "110169484474386276334"


def test_provider_errors_and_timeouts(idp: FakeIdp, cfg: ClientConfig) -> None:
    def deny(url: str) -> None:
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        params = {"error": "access_denied", "error_description": "no", "state": q["state"]}
        urllib.request.urlopen(f"{q['redirect_uri']}?{urlencode(params)}").read()

    with pytest.raises(OidcError, match="access_denied") as err:
        login(cfg, open_browser=deny, timeout=10)
    assert err.value.untrusted_text == "no"
    with pytest.raises(OidcError, match="within"):
        login(cfg, open_browser=lambda url: None, timeout=0.3)
    assert cfg.credentials_file is not None and not cfg.credentials_file.exists()


def test_nonce_and_audience_are_checked(idp: FakeIdp, cfg: ClientConfig) -> None:
    idp.id_claims = {"nonce": "replayed-from-another-login"}
    with pytest.raises(OidcError, match="nonce"):
        login(cfg, open_browser=browser(idp), timeout=10)
    idp.id_claims = {"aud": "someone-else"}
    with pytest.raises(OidcError, match="audience"):
        login(cfg, open_browser=browser(idp), timeout=10)


def test_login_needs_an_issuer_and_a_safe_api(cfg: ClientConfig) -> None:
    with pytest.raises(ConfigError, match="no OIDC issuer"):
        login(replace(cfg, oidc_issuer=None))
    with pytest.raises(ConfigError, match="plain http"):
        login(replace(cfg, base_url="http://api.example.com"))


# --------------------------------------------------------------------------- discovery


def test_discovery_is_checked() -> None:
    with respx.mock() as router, httpx.Client() as http:
        url = f"{ISSUER}/.well-known/openid-configuration"
        router.get(url).respond(200, json={**DISCOVERY, "issuer": "https://evil.test"})
        with pytest.raises(OidcError, match="different issuer"):
            discover(http, ISSUER)
        router.get(url).respond(200, json={**DISCOVERY, "token_endpoint": "http://idp.test/t"})
        with pytest.raises(OidcError, match="https"):
            discover(http, ISSUER)
        router.get(url).respond(404, text="nope")
        with pytest.raises(OidcError, match="HTTP 404"):
            discover(http, ISSUER)
        router.get(url).respond(200, text="<html>")
        with pytest.raises(OidcError, match="not JSON"):
            discover(http, ISSUER)
    with pytest.raises(ConfigError):
        discover(httpx.Client(), "http://idp.example")


def test_a_loopback_dev_idp_may_use_plain_http() -> None:
    dev = "http://127.0.0.1:9100/dev-idp"
    doc = {
        "issuer": dev,
        "authorization_endpoint": f"{dev}/authorize",
        "token_endpoint": f"{dev}/token",
    }
    with respx.mock() as router, httpx.Client() as http:
        router.get(f"{dev}/.well-known/openid-configuration").respond(200, json=doc)
        meta = discover(http, dev)
    assert meta.token_endpoint == f"{dev}/token" and meta.device_authorization_endpoint is None


def test_discovery_url_serves_another_issuers_document() -> None:
    """The dev profile's IdP: issuer ``https://dev-idp.mapi.localhost`` (an identifier),
    document served by the API at ``/dev/idp`` (it must still name that issuer)."""
    issuer = "https://dev-idp.mapi.localhost"
    api = "http://127.0.0.1:8000/dev/idp"
    doc = {
        "issuer": issuer,
        "authorization_endpoint": f"{api}/authorize",
        "token_endpoint": f"{api}/token",
    }
    with respx.mock() as router, httpx.Client() as http:
        route = router.get(f"{api}/.well-known/openid-configuration").respond(200, json=doc)
        meta = discover(http, issuer, discovery_url=api)
        assert route.called and meta.issuer == issuer
        assert meta.authorization_endpoint == f"{api}/authorize"
        router.get(f"{api}/.well-known/openid-configuration").respond(
            200, json={**doc, "issuer": "https://evil.test"}
        )
        with pytest.raises(OidcError, match="different issuer"):
            discover(http, issuer, discovery_url=api)
    with pytest.raises(ConfigError):
        discover(httpx.Client(), issuer, discovery_url="http://idp.example/dev")
    cfg = load_config(
        environ={"MAPI_OIDC_ISSUER": issuer, "MAPI_OIDC_DISCOVERY_URL": api},
        path=Path("/nonexistent/config.toml"),
    )
    assert cfg.oidc_discovery_url == api and cfg.sources["oidc_discovery_url"].startswith("env:")


# ------------------------------------------------------------------------------ device


def test_device_flow_polls_with_backoff(idp: FakeIdp, cfg: ClientConfig) -> None:
    idp.router.post(f"{ISSUER}/device").respond(
        200,
        json={
            "device_code": "dev-1",
            "user_code": "WDJB-MJHT",
            "verification_url": "https://idp.test/activate",
            "expires_in": 600,
            "interval": 5,
        },
    )
    answers = iter(
        [
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"id_token": jwt(), "refresh_token": "r2"}),
        ]
    )
    idp.router.post(TOKEN_URL).mock(side_effect=lambda request: next(answers))
    codes: list[DeviceCode] = []
    sleeps: list[float] = []
    session = login(cfg, method="device", show_code=codes.append, sleep=sleeps.append)
    assert codes[0].user_code == "WDJB-MJHT"
    assert codes[0].verification_uri == "https://idp.test/activate"
    assert sleeps == [5.0, 5.0, 10.0]
    assert session.refresh_token == "r2"


def test_device_flow_failures() -> None:
    meta = ProviderMetadata(ISSUER, f"{ISSUER}/a", TOKEN_URL, f"{ISSUER}/device")
    with respx.mock() as router, httpx.Client() as http:
        router.post(f"{ISSUER}/device").respond(
            200, json={"device_code": "d", "user_code": "U", "verification_uri": "https://x"}
        )
        router.post(TOKEN_URL).respond(400, json={"error": "access_denied"})
        flow = DeviceLogin(http, meta, "cli", sleep=lambda s: None)
        with pytest.raises(OidcError, match="access_denied"):
            flow.run()
        clock = iter([0.0, 1000.0])
        flow = DeviceLogin(http, meta, "cli", sleep=lambda s: None, clock=lambda: next(clock))
        with pytest.raises(OidcError, match="expired"):
            flow.run()
        no_device = DeviceLogin(http, replace(meta, device_authorization_endpoint=None), "cli")
        with pytest.raises(OidcError, match="does not support device"):
            no_device.run()


# ----------------------------------------------------------------------------- refresh


def _session(path: Path, *, expires_in: float, refresh: str | None = "r1") -> OidcSession:
    now = datetime.now(UTC)
    session = OidcSession(
        api_url=API,
        issuer=ISSUER,
        client_id=DEFAULT_CLIENT_ID,
        id_token=jwt(),
        expires_at=now + timedelta(seconds=expires_in),
        token_endpoint=TOKEN_URL,
        refresh_token=refresh,
        revocation_endpoint=f"{ISSUER}/revoke",
        subject="110169484474386276334",
        email="ana@acme.example",
    )
    CredentialStore(path).save("prod", session)
    return session


def test_expired_sessions_are_refreshed_and_saved(idp: FakeIdp, cfg: ClientConfig) -> None:
    assert cfg.credentials_file is not None
    old = _session(cfg.credentials_file, expires_in=10)  # inside the 60 s skew
    store = CredentialStore(cfg.credentials_file)
    provider = OidcTokenProvider(store, "prod", old)
    token = provider()
    assert token != old.id_token
    assert decode_jwt_claims(token)["email"] == "new@acme.example"
    saved = store.load("prod")
    assert saved is not None and saved.id_token == token and saved.refresh_token == "r1"
    assert idp.forms[-1] == {
        "grant_type": "refresh_token",
        "refresh_token": "r1",
        "client_id": DEFAULT_CLIENT_ID,
    }
    assert provider() == token  # fresh now: no second refresh
    assert len(idp.forms) == 1


def test_a_401_triggers_one_refresh_and_resend(
    idp: FakeIdp, cfg: ClientConfig, example: Callable[..., dict[str, Any]]
) -> None:
    assert cfg.credentials_file is not None
    old = _session(cfg.credentials_file, expires_in=3600)
    route = idp.router.get(f"{API}/v1/me").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=example(Me))]
    )
    with Client(config=cfg) as client:
        client.me()
    first, second = (c.request.headers["Authorization"] for c in route.calls)
    assert first == f"Bearer {old.id_token}" and second != first


def test_unrefreshable_sessions_ask_for_a_new_login(idp: FakeIdp, cfg: ClientConfig) -> None:
    assert cfg.credentials_file is not None
    old = _session(cfg.credentials_file, expires_in=-10, refresh=None)
    provider = OidcTokenProvider(CredentialStore(cfg.credentials_file), "prod", old)
    with pytest.raises(OidcError, match="mapi login"):
        provider()
    assert provider.refresh() is False
    idp.router.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    expiring = _session(cfg.credentials_file, expires_in=-10)
    provider = OidcTokenProvider(CredentialStore(cfg.credentials_file), "prod", expiring)
    with pytest.raises(OidcError, match="mapi login") as err:
        provider()
    assert err.value.untrusted_text is not None and "invalid_grant" in err.value.untrusted_text


def test_refresh_for_another_user_is_refused(idp: FakeIdp, cfg: ClientConfig) -> None:
    assert cfg.credentials_file is not None
    idp.router.post(TOKEN_URL).respond(200, json={"id_token": jwt(sub="mallory")})
    old = _session(cfg.credentials_file, expires_in=-10)
    provider = OidcTokenProvider(CredentialStore(cfg.credentials_file), "prod", old)
    with pytest.raises(OidcError, match="different user"):
        provider()


def test_logout_revokes_and_forgets(idp: FakeIdp, cfg: ClientConfig) -> None:
    assert cfg.credentials_file is not None
    _session(cfg.credentials_file, expires_in=3600)
    revoke = idp.router.post(f"{ISSUER}/revoke").respond(200)
    removed = logout(cfg)
    assert removed is not None and removed.email == "ana@acme.example"
    assert parse_qs(revoke.calls.last.request.content.decode())["token"] == ["r1"]
    assert not cfg.credentials_file.exists()
    assert logout(cfg) is None


def test_plain_http_apis_never_get_a_stored_session(cfg: ClientConfig) -> None:
    assert cfg.credentials_file is not None
    _session(cfg.credentials_file, expires_in=3600)
    session = CredentialStore(cfg.credentials_file).load("prod")
    assert session is not None
    CredentialStore(cfg.credentials_file).save(
        "prod", replace(session, api_url="http://api.example.com")
    )
    with pytest.raises(ConfigError, match="plain http"):
        default_auth(replace(cfg, base_url="http://api.example.com"))


# ----------------------------------------------------------------------------- claims


def test_id_token_claims() -> None:
    now = datetime.now(UTC)
    ok = decode_jwt_claims(jwt(nonce="n"))
    expires = check_id_token(ok, issuer=ISSUER, client_id=DEFAULT_CLIENT_ID, nonce="n", now=now)
    assert expires > now
    multi = decode_jwt_claims(jwt(aud=[DEFAULT_CLIENT_ID, "other"], azp="other"))
    with pytest.raises(OidcError, match="authorized party"):
        check_id_token(multi, issuer=ISSUER, client_id=DEFAULT_CLIENT_ID, nonce=None, now=now)
    old = decode_jwt_claims(jwt(exp=int(time.time()) - 3600))
    with pytest.raises(OidcError, match="expired"):
        check_id_token(old, issuer=ISSUER, client_id=DEFAULT_CLIENT_ID, nonce=None, now=now)
    other = decode_jwt_claims(jwt(iss="https://evil.test"))
    with pytest.raises(OidcError, match="different identity provider"):
        check_id_token(other, issuer=ISSUER, client_id=DEFAULT_CLIENT_ID, nonce=None, now=now)
    for bad in ("not-a-jwt", "a.b", "a.!!!.c", f"a.{b64({'x': 1})[:-2]}@.c"):
        with pytest.raises(OidcError):
            decode_jwt_claims(bad)


def test_credential_store_errors(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path)
    assert store.load("x") is None and store.profiles() == []

    def write(text: str) -> None:
        path.write_text(text)
        path.chmod(0o600)

    write("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        store.load("x")
    write(json.dumps({"version": 99, "profiles": {}}))
    with pytest.raises(ConfigError, match="version"):
        store.load("x")
    write(json.dumps({"version": 1, "profiles": {"x": {"api_url": "a"}}}))
    with pytest.raises(ConfigError, match="invalid"):
        store.load("x")
    write(json.dumps({"version": 1, "profiles": {}}))
    path.chmod(0o644)
    with pytest.warns(UserWarning, match="chmod 600"):
        store.load("x")
