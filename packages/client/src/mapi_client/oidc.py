"""Interactive OpenID Connect sign-in for programs (``mapi login``).

Two grants are supported, both for *public* clients (no client secret needed, though one
can be configured for identity providers that insist on it, e.g. Google "desktop" clients):

* **Loopback redirect with PKCE** (RFC 8252 §7.3, RFC 7636): a one-shot HTTP listener on
  ``127.0.0.1:<random port>`` receives the authorization code; the browser is opened on the
  identity provider's authorization endpoint. ``state`` and ``nonce`` are 256-bit random
  values; the code verifier never leaves this process.
* **Device authorization grant** (RFC 8628): for machines without a browser; the user
  enters a short code on another device.

The identity provider is found by OIDC discovery (``<issuer>/.well-known/openid-
configuration``); the document's ``issuer`` must equal the configured issuer. Plain ``http``
is accepted only for loopback issuers (a local development identity provider).

The resulting **ID token** is what the client sends to the API (``Authorization: Bearer``);
its claims (issuer, audience, nonce, expiry) are checked here. Its signature is not: the
token came straight from the token endpoint over TLS (OIDC Core §3.1.3.7), and the API
verifies the signature of every token it receives. A refresh token, when issued, renews the
ID token without a browser (:class:`OidcTokenProvider`).
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from mapi_spec.auth import OIDC_SCOPES, pkce_challenge, pkce_verifier, random_state
from mapi_spec.errors import ErrorCode, MapiError

from mapi_client._version import __version__
from mapi_client.config import ClientConfig, ConfigError, check_issuer, is_loopback
from mapi_client.credentials import CredentialStore, OidcSession

__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEVICE_GRANT",
    "DeviceCode",
    "DeviceLogin",
    "LoopbackLogin",
    "OidcError",
    "OidcTokenProvider",
    "ProviderMetadata",
    "TokenResponse",
    "check_id_token",
    "decode_jwt_claims",
    "discover",
    "login",
    "logout",
    "refresh_tokens",
    "revoke_token",
]

DEFAULT_CLIENT_ID: Final = "mapi-cli"
"""Client id used when none is configured (the local development identity provider's
public CLI client)."""
DEVICE_GRANT: Final = "urn:ietf:params:oauth:grant-type:device_code"
DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
LOGIN_TIMEOUT: Final = 300.0
_LEEWAY: Final = timedelta(seconds=120)
_MAX_DOC_BYTES: Final = 256 * 1024
_USER_AGENT: Final = f"mapi-client/{__version__}"

_PAGE: Final = (
    "<!doctype html><html><head><meta charset='utf-8'><title>mapi</title></head>"
    "<body style='font-family:sans-serif;margin:3em'><h1>{title}</h1><p>{body}</p></body></html>"
)
_PAGE_HEADERS: Final = {
    "Content-Type": "text/html; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


class OidcError(MapiError):
    """Sign-in failed or a stored sign-in can no longer be used (``UNAUTHENTICATED``)."""

    default_code = ErrorCode.UNAUTHENTICATED


# ===================================================================================== helpers


def _check_endpoint(url: object, what: str, *, required: bool = True) -> str | None:
    if url is None and not required:
        return None
    if not isinstance(url, str):
        raise OidcError(f"the identity provider's discovery document has no {what}")
    parts = urlsplit(url)
    secure = parts.scheme == "https" or (parts.scheme == "http" and is_loopback(parts.hostname))
    if not secure or not parts.netloc or parts.fragment:
        raise OidcError(f"the identity provider's {what} must be an https URL")
    return url


def _http(cfg: ClientConfig) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(cfg.timeout),
        verify=cfg.verify,
        follow_redirects=False,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )


def _json_body(response: httpx.Response, what: str) -> dict[str, Any]:
    if len(response.content) > _MAX_DOC_BYTES:
        raise OidcError(f"the {what} response is too large")
    try:
        data = response.json()
    except ValueError:
        raise OidcError(
            f"the {what} response is not JSON (HTTP {response.status_code})",
            untrusted_text=response.text[:500] or None,
        ) from None
    if not isinstance(data, dict):
        raise OidcError(f"the {what} response is not a JSON object")
    return data


def _oauth_error(data: Mapping[str, Any], what: str) -> OidcError:
    code = str(data.get("error", "unknown_error"))[:64]
    desc = data.get("error_description")
    return OidcError(
        f"{what} failed: {code}",
        untrusted_text=str(desc)[:500] if isinstance(desc, str) and desc else None,
    )


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """The payload of a compact JWS (no signature check: see the module docstring)."""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts[:2]):
        raise OidcError("the ID token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        raise OidcError("the ID token payload is not valid base64url JSON") from None
    if not isinstance(claims, dict):
        raise OidcError("the ID token payload is not a JSON object")
    return claims


def check_id_token(
    claims: Mapping[str, Any],
    *,
    issuer: str,
    client_id: str,
    nonce: str | None,
    now: datetime,
    leeway: timedelta = _LEEWAY,
) -> datetime:
    """Check the claims of an ID token from the token endpoint; returns its expiry.

    Raises :class:`OidcError` for a wrong issuer, audience, authorized party or nonce, or an
    expired token.
    """
    if str(claims.get("iss", "")).rstrip("/") != issuer.rstrip("/"):
        raise OidcError("the ID token was issued by a different identity provider")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if client_id not in audiences:
        raise OidcError("the ID token is for a different client (audience mismatch)")
    if len(audiences) > 1 and claims.get("azp") not in (None, client_id):
        raise OidcError("the ID token was issued to another authorized party")
    if nonce is not None and not secrets.compare_digest(
        str(claims.get("nonce", "")).encode(), nonce.encode()
    ):
        raise OidcError("the ID token nonce does not match this sign-in")
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or isinstance(exp, bool):
        raise OidcError("the ID token has no expiry")
    expires = datetime.fromtimestamp(exp, UTC)
    if expires + leeway < now:
        raise OidcError("the identity provider returned an expired ID token")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise OidcError("the ID token has no subject")
    return expires


# ================================================================================== discovery


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """The parts of an OIDC discovery document the client uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    device_authorization_endpoint: str | None = None
    revocation_endpoint: str | None = None

    @classmethod
    def from_document(cls, doc: Mapping[str, Any], *, issuer: str) -> ProviderMetadata:
        found = str(doc.get("issuer", "")).rstrip("/")
        if found != issuer.rstrip("/"):
            raise OidcError(
                "the discovery document names a different issuer",
                untrusted_text=found[:300] or None,
            )
        return cls(
            issuer=found,
            authorization_endpoint=str(
                _check_endpoint(doc.get("authorization_endpoint"), "authorization endpoint")
            ),
            token_endpoint=str(_check_endpoint(doc.get("token_endpoint"), "token endpoint")),
            device_authorization_endpoint=_check_endpoint(
                doc.get("device_authorization_endpoint"),
                "device authorization endpoint",
                required=False,
            ),
            revocation_endpoint=_check_endpoint(
                doc.get("revocation_endpoint"), "revocation endpoint", required=False
            ),
        )


def discover(
    http: httpx.Client, issuer: str, *, discovery_url: str | None = None
) -> ProviderMetadata:
    """Fetch and check ``<issuer>/.well-known/openid-configuration`` (or the document at
    ``discovery_url``, which must still name ``issuer``)."""
    issuer = check_issuer(issuer)
    if discovery_url is None:
        url = issuer + DISCOVERY_PATH
    else:
        url = check_issuer(discovery_url)
        if not url.endswith(DISCOVERY_PATH):
            url += DISCOVERY_PATH
    try:
        response = http.get(url)
    except httpx.TransportError as exc:
        raise OidcError(f"cannot reach the identity provider {issuer}: {exc}") from exc
    if response.status_code != 200:
        raise OidcError(
            f"OIDC discovery at {issuer} failed (HTTP {response.status_code})",
            untrusted_text=response.text[:500] or None,
        )
    return ProviderMetadata.from_document(_json_body(response, "discovery"), issuer=issuer)


# ==================================================================================== tokens


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """A token endpoint answer. Tokens are hidden from ``repr``."""

    id_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_in: int | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> TokenResponse:
        id_token = data.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcError(
                "the identity provider returned no ID token (is the 'openid' scope allowed?)"
            )
        refresh = data.get("refresh_token")
        expires = data.get("expires_in")
        return cls(
            id_token=id_token,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_in=int(expires) if isinstance(expires, int | float) else None,
        )


def _token_request(
    http: httpx.Client, endpoint: str, form: Mapping[str, str], what: str
) -> dict[str, Any]:
    try:
        response = http.post(endpoint, data=dict(form))
    except httpx.TransportError as exc:
        raise OidcError(f"{what} failed: cannot reach the identity provider ({exc})") from exc
    data = _json_body(response, what)
    if response.status_code != 200:
        raise _oauth_error(data, what)
    return data


def _client_form(client_id: str, client_secret: str | None) -> dict[str, str]:
    form = {"client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    return form


def refresh_tokens(
    http: httpx.Client,
    token_endpoint: str,
    *,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
) -> TokenResponse:
    """Exchange a refresh token for a new ID token (``invalid_grant``: sign in again)."""
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        **_client_form(client_id, client_secret),
    }
    return TokenResponse.from_json(_token_request(http, token_endpoint, form, "token refresh"))


def revoke_token(
    http: httpx.Client,
    revocation_endpoint: str,
    token: str,
    *,
    client_id: str,
    client_secret: str | None = None,
    hint: Literal["refresh_token", "access_token"] = "refresh_token",
) -> bool:
    """RFC 7009 revocation (best effort): ``True`` if the provider accepted it."""
    form = {"token": token, "token_type_hint": hint, **_client_form(client_id, client_secret)}
    try:
        response = http.post(revocation_endpoint, data=form)
    except httpx.TransportError:
        return False
    return response.status_code == 200


# ============================================================================ loopback + PKCE


@dataclass(slots=True)
class _Callback:
    expected_state: str
    done: threading.Event = field(default_factory=threading.Event)
    code: str | None = None
    error: OidcError | None = None


def _handler(cb: _Callback) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "mapi-login"
        sys_version = ""
        timeout = 10  # a local client that connects and stalls cannot block the listener

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _page(self, status: int, title: str, body: str) -> None:
            payload = _PAGE.format(title=html.escape(title), body=html.escape(body)).encode()
            self.send_response(status)
            for key, value in _PAGE_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path != "/callback" or cb.done.is_set():
                self._page(404, "Not found", "This address only receives one sign-in.")
                return
            try:
                query = parse_qs(url.query, max_num_fields=16)
            except ValueError:
                self._page(400, "Sign-in not recognised", "Start again from the terminal.")
                return
            state = query.get("state", [""])[0]
            if not secrets.compare_digest(state.encode(), cb.expected_state.encode()):
                # not our redirect (a stale tab, or another local process): keep waiting
                self._page(400, "Sign-in not recognised", "Start again from the terminal.")
                return
            if "error" in query:
                cb.error = _oauth_error(
                    {
                        "error": query["error"][0],
                        "error_description": query.get("error_description", [""])[0],
                    },
                    "sign-in",
                )
                self._page(200, "Sign-in failed", "Return to the terminal for details.")
            elif query.get("code", [""])[0]:
                cb.code = query["code"][0]
                self._page(200, "Signed in to mapi", "You can close this tab.")
            else:
                cb.error = OidcError("the identity provider redirected back without a code")
                self._page(400, "Sign-in failed", "Return to the terminal for details.")
            cb.done.set()

    return Handler


class _QuietServer(HTTPServer):
    """The loopback listener: a broken or hostile local request never prints a traceback."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


def _authorize_url(endpoint: str, params: Mapping[str, str]) -> str:
    sep = "&" if urlsplit(endpoint).query else "?"
    return f"{endpoint}{sep}{urlencode(dict(params))}"


@dataclass(slots=True)
class LoopbackLogin:
    """Authorization Code + PKCE with a loopback redirect (the browser flow)."""

    http: httpx.Client
    provider: ProviderMetadata
    client_id: str
    client_secret: str | None = None
    scopes: Sequence[str] = OIDC_SCOPES
    open_browser: Callable[[str], object] = webbrowser.open
    show_url: Callable[[str], None] = lambda url: None
    timeout: float = LOGIN_TIMEOUT
    host: str = "127.0.0.1"
    port: int = 0

    def run(self, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> TokenResponse:
        """Open the browser, wait for the redirect, exchange the code, check the ID token."""
        if not is_loopback(self.host):
            raise ConfigError("the login listener must bind a loopback address")
        state, nonce, verifier = random_state(), random_state(), pkce_verifier()
        cb = _Callback(expected_state=state)
        server = _QuietServer((self.host, self.port), _handler(cb))
        server.timeout = 0.5
        redirect_uri = f"http://{self.host}:{server.server_address[1]}/callback"
        url = _authorize_url(
            self.provider.authorization_endpoint,
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce_challenge(verifier),
                "code_challenge_method": "S256",
            },
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        thread.start()
        try:
            self.show_url(url)
            self.open_browser(url)
            if not cb.done.wait(self.timeout):
                raise OidcError(f"no sign-in completed within {self.timeout:.0f} s")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        if cb.error is not None:
            raise cb.error
        assert cb.code is not None
        form = {
            "grant_type": "authorization_code",
            "code": cb.code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            **_client_form(self.client_id, self.client_secret),
        }
        tokens = TokenResponse.from_json(
            _token_request(self.http, self.provider.token_endpoint, form, "code exchange")
        )
        check_id_token(
            decode_jwt_claims(tokens.id_token),
            issuer=self.provider.issuer,
            client_id=self.client_id,
            nonce=nonce,
            now=now(),
        )
        return tokens


# ================================================================================ device code


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """What to show the user during a device sign-in (all values come from the identity
    provider: display them as untrusted text)."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int


@dataclass(slots=True)
class DeviceLogin:
    """The device authorization grant (RFC 8628)."""

    http: httpx.Client
    provider: ProviderMetadata
    client_id: str
    client_secret: str | None = None
    scopes: Sequence[str] = OIDC_SCOPES
    show_code: Callable[[DeviceCode], None] = lambda code: None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def start(self) -> tuple[str, DeviceCode]:
        endpoint = self.provider.device_authorization_endpoint
        if endpoint is None:
            raise OidcError(
                "the identity provider does not support device sign-in; "
                "use the browser flow (mapi login without --device)"
            )
        form = {"scope": " ".join(self.scopes), **_client_form(self.client_id, None)}
        data = _token_request(self.http, endpoint, form, "device authorization")
        device_code = data.get("device_code")
        user_code = data.get("user_code")
        # Google spells it verification_url
        uri = data.get("verification_uri", data.get("verification_url"))
        if not all(isinstance(v, str) and v for v in (device_code, user_code, uri)):
            raise OidcError("the device authorization response is incomplete")
        complete = data.get("verification_uri_complete")
        expires = data.get("expires_in", 600)
        interval = data.get("interval", 5)
        code = DeviceCode(
            user_code=str(user_code)[:64],
            verification_uri=str(uri)[:500],
            verification_uri_complete=str(complete)[:600] if isinstance(complete, str) else None,
            expires_in=int(expires) if isinstance(expires, int | float) else 600,
            interval=max(1, int(interval)) if isinstance(interval, int | float) else 5,
        )
        return str(device_code), code

    def run(self, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> TokenResponse:
        device_code, code = self.start()
        self.show_code(code)
        deadline = self.clock() + code.expires_in
        interval = float(code.interval)
        form = {
            "grant_type": DEVICE_GRANT,
            "device_code": device_code,
            **_client_form(self.client_id, self.client_secret),
        }
        while True:
            if self.clock() + interval > deadline:
                raise OidcError("the device code expired before sign-in completed")
            self.sleep(interval)
            try:
                response = self.http.post(self.provider.token_endpoint, data=form)
            except httpx.TransportError:
                interval = min(interval * 2, 60.0)  # back off on a flaky network (§3.5)
                continue
            data = _json_body(response, "device sign-in")
            if response.status_code == 200:
                tokens = TokenResponse.from_json(data)
                break
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise _oauth_error(data, "device sign-in")
        check_id_token(
            decode_jwt_claims(tokens.id_token),
            issuer=self.provider.issuer,
            client_id=self.client_id,
            nonce=None,
            now=now(),
        )
        return tokens


# ================================================================================= sessions


def _session(
    cfg: ClientConfig,
    provider: ProviderMetadata,
    client_id: str,
    tokens: TokenResponse,
    *,
    now: datetime,
    previous: OidcSession | None = None,
) -> OidcSession:
    claims = decode_jwt_claims(tokens.id_token)
    expires = check_id_token(
        claims, issuer=provider.issuer, client_id=client_id, nonce=None, now=now
    )
    email = claims.get("email")
    return OidcSession(
        api_url=cfg.base_url,
        issuer=provider.issuer,
        client_id=client_id,
        id_token=tokens.id_token,
        refresh_token=tokens.refresh_token or (previous.refresh_token if previous else None),
        expires_at=expires,
        token_endpoint=provider.token_endpoint,
        revocation_endpoint=provider.revocation_endpoint,
        subject=str(claims["sub"]),
        email=email if isinstance(email, str) else None,
        obtained_at=now,
    )


def login(
    cfg: ClientConfig,
    *,
    method: Literal["browser", "device"] = "browser",
    open_browser: Callable[[str], object] = webbrowser.open,
    show_url: Callable[[str], None] = lambda url: None,
    show_code: Callable[[DeviceCode], None] = lambda code: None,
    timeout: float = LOGIN_TIMEOUT,
    port: int = 0,
    store: CredentialStore | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OidcSession:
    """Sign in with the configured issuer and store the session for ``cfg``'s profile.

    ``cfg.oidc_issuer`` is required; ``cfg.oidc_client_id`` defaults to
    :data:`DEFAULT_CLIENT_ID`.
    """
    if cfg.oidc_issuer is None:
        raise ConfigError(
            "no OIDC issuer configured: pass --issuer, set MAPI_OIDC_ISSUER or "
            "oidc_issuer in the profile (or use an API token: mapi login --with-token)"
        )
    if cfg.insecure_transport and not cfg.allow_insecure_token:
        raise ConfigError(
            f"refusing to sign in for {cfg.base_url}: credentials are never sent over plain "
            "http to a non-loopback host"
        )
    client_id = cfg.oidc_client_id or DEFAULT_CLIENT_ID
    target = store or _store_of(cfg)
    with _http(cfg) as http:
        provider = discover(http, cfg.oidc_issuer, discovery_url=cfg.oidc_discovery_url)
        flow: LoopbackLogin | DeviceLogin
        if method == "device":
            flow = DeviceLogin(
                http,
                provider,
                client_id,
                cfg.oidc_client_secret,
                show_code=show_code,
                sleep=sleep,
            )
        else:
            flow = LoopbackLogin(
                http,
                provider,
                client_id,
                cfg.oidc_client_secret,
                open_browser=open_browser,
                show_url=show_url,
                timeout=timeout,
                port=port,
            )
        tokens = flow.run(now=now)
    session = _session(cfg, provider, client_id, tokens, now=now())
    target.save(cfg.profile_key, session)
    return session


def _store_of(cfg: ClientConfig) -> CredentialStore:
    if cfg.credentials_file is None:
        raise ConfigError("this configuration has no credentials file (use load_config())")
    return CredentialStore(cfg.credentials_file)


def logout(cfg: ClientConfig, *, revoke: bool = True) -> OidcSession | None:
    """Forget the stored sign-in of ``cfg``'s profile (revoking its refresh token at the
    identity provider when possible). Returns the session that was removed."""
    store = _store_of(cfg)
    session = store.load(cfg.profile_key)
    if session is None:
        return None
    if revoke and session.refresh_token and session.revocation_endpoint:
        with _http(cfg) as http:
            revoke_token(
                http,
                session.revocation_endpoint,
                session.refresh_token,
                client_id=session.client_id,
                client_secret=cfg.oidc_client_secret,
            )
    store.delete(cfg.profile_key)
    return session


class OidcTokenProvider:
    """A :class:`~mapi_client.auth.TokenProvider` over a stored sign-in.

    Returns the ID token, refreshing it (and saving the result) shortly before it expires or
    when the API answers ``401``. Without a refresh token an expired session raises
    :class:`OidcError` asking to run ``mapi login`` again.
    """

    def __init__(
        self,
        store: CredentialStore,
        profile: str,
        session: OidcSession,
        *,
        client_secret: str | None = None,
        verify: bool = True,
        timeout: float = 30.0,
        http: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._profile = profile
        self._session = session
        self._client_secret = client_secret
        self._http = http
        self._verify = verify
        self._timeout = timeout
        self._now = now
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"OidcTokenProvider(profile={self._profile!r}, issuer={self._session.issuer!r})"

    @property
    def session(self) -> OidcSession:
        return self._session

    def __call__(self) -> str:
        with self._lock:
            if self._session.expired(self._now()):
                self._refresh_locked()
            return self._session.id_token

    def refresh(self) -> bool:
        """Force a refresh (after a ``401``); ``False`` if the session cannot refresh."""
        with self._lock:
            if not self._session.can_refresh:
                return False
            self._refresh_locked()
            return True

    def _refresh_locked(self) -> None:
        old = self._session
        if not old.refresh_token:
            raise OidcError("your sign-in has expired; run `mapi login` again")
        http = self._http or httpx.Client(
            timeout=httpx.Timeout(self._timeout),
            verify=self._verify,
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        try:
            tokens = refresh_tokens(
                http,
                old.token_endpoint,
                client_id=old.client_id,
                refresh_token=old.refresh_token,
                client_secret=self._client_secret,
            )
        except OidcError as exc:
            raise OidcError(
                "your sign-in could not be renewed; run `mapi login` again",
                untrusted_text=exc.detail,
            ) from None
        finally:
            if self._http is None:
                http.close()
        now = self._now()
        claims = decode_jwt_claims(tokens.id_token)
        expires = check_id_token(
            claims, issuer=old.issuer, client_id=old.client_id, nonce=None, now=now
        )
        if claims.get("sub") != old.subject:
            raise OidcError("the renewed ID token is for a different user")
        self._session = replace(
            old,
            id_token=tokens.id_token,
            refresh_token=tokens.refresh_token or old.refresh_token,
            expires_at=expires,
            obtained_at=now,
        )
        self._store.save(self._profile, self._session)
