"""OpenID Connect for the BFF sign-in: discovery, JWKS-verified ID tokens, code exchange.

The flow is Authorization Code with PKCE (S256) and a ``nonce`` (see
:mod:`mapi_spec.auth`); the browser never sees a token. ID tokens are verified with
``pyjwt`` against the provider's JWKS: signature (asymmetric algorithms only, the ``alg``
must be allowed *and* match the key), issuer, audience (and ``azp`` when there are several
audiences), expiry, issued-at and the transaction's nonce. Keys are cached and refetched
when a token names an unknown ``kid`` (key rotation), at most once per
:data:`JWKS_REFRESH_MIN_S`.

Two providers implement :class:`OidcProvider`: :class:`HttpOidcProvider` (any standard IdP:
Google, Identity Platform, Okta, Entra ID, …) and the in-process development IdP
(:class:`mapi_server.devidp.DevIdP`, ``MAPI_PROFILE=dev``).
"""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol
from urllib.parse import urlencode

import httpx
import jwt
from mapi_spec.auth import GOOGLE_ISSUERS, OIDC_SCOPES, LoginTransaction, build_authorize_url
from mapi_spec.errors import ErrorCode, MapiError

from mapi_server.auth.identity import VerifiedIdentity

__all__ = [
    "JWKS_REFRESH_MIN_S",
    "HttpOidcProvider",
    "JwksCache",
    "OidcClient",
    "OidcError",
    "OidcProvider",
    "ProviderMetadata",
    "verify_jwt",
]

JWKS_TTL_S: Final = 3600.0
JWKS_REFRESH_MIN_S: Final = 60.0
LEEWAY_S: Final = 60
_ASYMMETRIC = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "EdDSA")


class OidcError(MapiError):
    """Sign-in failed (401). ``detail`` is generic; the reason is logged and audited only."""

    default_code = ErrorCode.UNAUTHENTICATED

    def __init__(self, reason: str) -> None:
        super().__init__("sign-in failed")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str | None = None


class OidcProvider(Protocol):
    """Where the metadata, the keys and the code exchange come from."""

    async def metadata(self) -> ProviderMetadata: ...

    async def jwks(self, *, refresh: bool = False) -> Mapping[str, Any]: ...

    async def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str, client_id: str
    ) -> Mapping[str, Any]: ...


class JwksCache:
    """A TTL cache in front of a JWKS fetch, refreshed on unknown key ids (rate-limited)."""

    def __init__(self, fetch: Any, *, ttl_s: float = JWKS_TTL_S) -> None:
        self._fetch = fetch
        self._ttl = ttl_s
        self._value: Mapping[str, Any] | None = None
        self._stamp = 0.0
        self._lock = asyncio.Lock()

    async def get(self, *, refresh: bool = False) -> Mapping[str, Any]:
        async with self._lock:
            age = time.monotonic() - self._stamp
            stale = self._value is None or age > self._ttl
            if stale or (refresh and age > JWKS_REFRESH_MIN_S):
                self._value = await self._fetch()
                self._stamp = time.monotonic()
            assert self._value is not None
            return self._value


@dataclass
class HttpOidcProvider:
    """A standard OpenID provider reached over HTTPS (discovery document + JWKS + token)."""

    issuer: str
    client_secret: str | None = None
    discovery_url: str | None = None
    timeout_s: float = 10.0
    http: httpx.AsyncClient | None = None
    _meta: ProviderMetadata | None = field(default=None, init=False, repr=False)
    _jwks: JwksCache | None = field(default=None, init=False, repr=False)

    def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False)
        return self.http

    async def metadata(self) -> ProviderMetadata:
        if self._meta is None:
            url = self.discovery_url or (
                self.issuer.rstrip("/") + "/.well-known/openid-configuration"
            )
            r = await self._client().get(url)
            r.raise_for_status()
            doc = r.json()
            if doc.get("issuer") not in (self.issuer, self.issuer.rstrip("/")):
                raise OidcError("the discovery document names a different issuer")
            self._meta = ProviderMetadata(
                issuer=str(doc["issuer"]),
                authorization_endpoint=str(doc["authorization_endpoint"]),
                token_endpoint=str(doc["token_endpoint"]),
                jwks_uri=str(doc["jwks_uri"]),
            )
        return self._meta

    async def _fetch_jwks(self) -> Mapping[str, Any]:
        meta = await self.metadata()
        if meta.jwks_uri is None:
            raise OidcError("the provider publishes no jwks_uri")
        r = await self._client().get(meta.jwks_uri)
        r.raise_for_status()
        doc: Mapping[str, Any] = r.json()
        return doc

    async def jwks(self, *, refresh: bool = False) -> Mapping[str, Any]:
        if self._jwks is None:
            self._jwks = JwksCache(self._fetch_jwks)
        return await self._jwks.get(refresh=refresh)

    async def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str, client_id: str
    ) -> Mapping[str, Any]:
        meta = await self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": client_id,
        }
        if self.client_secret:  # client_secret_basic; public clients rely on PKCE alone
            r = await self._client().post(
                meta.token_endpoint, data=form, auth=(client_id, self.client_secret)
            )
        else:
            r = await self._client().post(meta.token_endpoint, data=form)
        if r.status_code != 200:
            raise OidcError(f"the token endpoint answered {r.status_code}")
        body: Mapping[str, Any] = r.json()
        return body

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()


def _pick_key(jwks: Mapping[str, Any], kid: str | None, alg: str) -> jwt.PyJWK | None:
    keys = jwks.get("keys") if isinstance(jwks, Mapping) else None
    if not isinstance(keys, list):
        return None
    for jwk in keys:
        if not isinstance(jwk, dict):
            continue
        if kid is not None and jwk.get("kid") != kid:
            continue
        if jwk.get("use") not in (None, "sig"):
            continue
        if jwk.get("alg") not in (None, alg):
            continue
        try:
            return jwt.PyJWK(jwk, algorithm=alg)
        except (jwt.PyJWKError, jwt.InvalidKeyError):
            continue
    return None


async def verify_jwt(
    token: str,
    *,
    jwks: Any,
    algorithms: Sequence[str],
    audience: str,
    issuers: Sequence[str],
    leeway: int = LEEWAY_S,
) -> dict[str, Any]:
    """Verify a JWS against a JWKS source (``await jwks(refresh=bool)``); return its claims.

    Raises :class:`OidcError` for any defect (the reason is in ``.reason``).
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise OidcError("the token is not a JWS") from None
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in algorithms or alg not in _ASYMMETRIC:
        raise OidcError(f"algorithm {alg!r} is not allowed")
    kid = header.get("kid")
    key = _pick_key(await jwks(refresh=False), kid, alg)
    if key is None:
        key = _pick_key(await jwks(refresh=True), kid, alg)
    if key is None:
        raise OidcError("no matching signing key")
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            key=key.key,
            algorithms=[alg],
            audience=audience,
            leeway=leeway,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcError(f"token rejected: {type(exc).__name__}") from None
    if claims.get("iss") not in issuers:
        raise OidcError("issuer mismatch")
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != audience:
        raise OidcError("azp does not name this client")
    return claims


def _issuers(issuer: str) -> tuple[str, ...]:
    if issuer in GOOGLE_ISSUERS:
        return GOOGLE_ISSUERS
    return (issuer, issuer.rstrip("/"))


class OidcClient:
    """The relying party: builds authorization URLs and turns a callback into an identity."""

    def __init__(
        self,
        provider: OidcProvider,
        *,
        client_id: str,
        redirect_uri: str,
        algorithms: Sequence[str] = ("RS256", "ES256"),
        scopes: Sequence[str] = OIDC_SCOPES,
        extra_params: Mapping[str, str] | None = None,
        allow_http: bool = False,
    ) -> None:
        self.provider = provider
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.algorithms = tuple(algorithms)
        self.scopes = tuple(scopes)
        self.extra_params = dict(extra_params or {})
        self.allow_http = allow_http
        """Only for the development IdP served by this API over plain HTTP."""

    async def authorize_url(self, tx: LoginTransaction) -> str:
        meta = await self.provider.metadata()
        endpoint = meta.authorization_endpoint
        if endpoint.startswith("https://"):
            return build_authorize_url(
                endpoint,
                client_id=self.client_id,
                redirect_uri=self.redirect_uri,
                tx=tx,
                scopes=self.scopes,
                extra=self.extra_params,
            )
        if not (self.allow_http and endpoint.startswith("http://")):
            raise OidcError("the authorization endpoint must be https")
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": tx.state,
            "nonce": tx.nonce,
            "code_challenge": tx.code_challenge,
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in endpoint else "?"
        return f"{endpoint}{sep}{urlencode(params)}"

    async def verify_id_token(self, token: str, *, nonce: str) -> VerifiedIdentity:
        meta = await self.provider.metadata()
        claims = await verify_jwt(
            token,
            jwks=self.provider.jwks,
            algorithms=self.algorithms,
            audience=self.client_id,
            issuers=_issuers(meta.issuer),
        )
        presented = claims.get("nonce")
        if not isinstance(presented, str) or not hmac.compare_digest(presented, nonce):
            raise OidcError("nonce mismatch")
        iss = str(claims["iss"])
        if not iss.startswith("https://"):
            iss = "https://" + iss  # Google's legacy form "accounts.google.com"
        sub = str(claims["sub"])
        if not sub or len(sub) > 255:
            raise OidcError("invalid subject")
        email = claims.get("email")
        name = claims.get("name")
        return VerifiedIdentity(
            issuer=iss,
            subject=sub,
            email=str(email)[:320] if isinstance(email, str) and "@" in email else None,
            email_verified=claims.get("email_verified") is True,
            name=str(name)[:200] if isinstance(name, str) and name else None,
        )

    async def complete(self, *, code: str, tx: LoginTransaction) -> VerifiedIdentity:
        """Exchange the authorization code (with the PKCE verifier) and verify the ID token."""
        if not code or len(code) > 2048:
            raise OidcError("missing or oversized authorization code")
        body = await self.provider.exchange_code(
            code=code,
            redirect_uri=self.redirect_uri,
            code_verifier=tx.code_verifier,
            client_id=self.client_id,
        )
        token = body.get("id_token")
        if not isinstance(token, str):
            raise OidcError("the token response has no id_token")
        return await self.verify_id_token(token, nonce=tx.nonce)
