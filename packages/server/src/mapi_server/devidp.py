"""A built-in OpenID provider for local development (``MAPI_PROFILE=dev`` only).

It lets the whole stack, browser sign-in included, run offline: ``/auth/login`` redirects to
``/dev/idp/authorize``, which shows the configured accounts (``MAPI_DEVIDP_USERS``); picking
one redirects back to ``/auth/callback`` with an authorization code. The code exchange and
the key set are served in-process to the BFF, so the ID token goes through exactly the same
verification as a real provider's (RS256 signature against the JWKS, issuer, audience,
expiry, nonce) and PKCE (S256) is enforced.

The signing key is generated at start-up and never leaves the process; codes are single-use
and live 60 seconds. There are no passwords: anybody who can reach a ``dev`` API can sign in
as any configured account, which is why :func:`mount_devidp` refuses every other profile.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mapi_spec.errors import ErrorCode, MapiError

from mapi_server.auth.oidc import OidcError, ProviderMetadata
from mapi_server.settings import Settings

__all__ = [
    "DEVIDP_PREFIX",
    "DEV_CLIENT_ID",
    "DEV_ISSUER",
    "DevIdP",
    "mount_devidp",
]

DEV_ISSUER: Final = "https://dev-idp.mapi.localhost"
"""``iss`` of dev tokens (an identifier, never fetched)."""
DEV_CLIENT_ID: Final = "mapi-dev"
DEVIDP_PREFIX: Final = "/dev/idp"
CODE_TTL_S: Final = 60.0
TOKEN_TTL_S: Final = 300


@dataclass(frozen=True, slots=True)
class _Grant:
    email: str
    client_id: str
    redirect_uri: str
    nonce: str | None
    code_challenge: str
    expires: float


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class DevIdP:
    """The development identity provider (implements :class:`~mapi_server.auth.oidc.
    OidcProvider` in-process)."""

    def __init__(
        self,
        *,
        users: Sequence[str],
        base_url: str,
        client_id: str = DEV_CLIENT_ID,
        redirect_uri: str,
    ) -> None:
        if not users:
            raise ValueError("the development IdP needs at least one account")
        self.users = tuple(u.strip().lower() for u in users if u.strip())
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = uuid.uuid4().hex[:16]
        self._codes: dict[str, _Grant] = {}

    # ------------------------------------------------------------------ provider protocol
    async def metadata(self) -> ProviderMetadata:
        return ProviderMetadata(
            issuer=DEV_ISSUER,
            authorization_endpoint=f"{self.base_url}{DEVIDP_PREFIX}/authorize",
            token_endpoint=f"{self.base_url}{DEVIDP_PREFIX}/token",
            jwks_uri=f"{self.base_url}{DEVIDP_PREFIX}/jwks",
        )

    def jwks_document(self) -> dict[str, Any]:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

    async def jwks(self, *, refresh: bool = False) -> Mapping[str, Any]:
        return self.jwks_document()

    async def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str, client_id: str
    ) -> Mapping[str, Any]:
        try:
            return self.redeem(
                code=code, redirect_uri=redirect_uri, code_verifier=code_verifier,
                client_id=client_id,
            )  # fmt: skip
        except MapiError as exc:
            raise OidcError(exc.detail) from None

    # -------------------------------------------------------------------------- the IdP
    def subject(self, email: str) -> str:
        return "dev|" + hashlib.sha256(email.encode()).hexdigest()[:24]

    def grant(
        self,
        *,
        email: str,
        client_id: str,
        redirect_uri: str,
        nonce: str | None,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        """Issue a single-use authorization code for a configured account."""
        if email not in self.users:
            raise MapiError("unknown development account", code=ErrorCode.UNAUTHENTICATED)
        if client_id != self.client_id or redirect_uri != self.redirect_uri:
            raise MapiError("unknown client or redirect_uri", code=ErrorCode.SCHEMA_INVALID)
        if code_challenge_method != "S256" or not 43 <= len(code_challenge) <= 128:
            raise MapiError("PKCE with S256 is required", code=ErrorCode.SCHEMA_INVALID)
        now = time.monotonic()
        self._codes = {c: g for c, g in self._codes.items() if g.expires > now}
        code = secrets.token_urlsafe(32)
        self._codes[code] = _Grant(
            email, client_id, redirect_uri, nonce, code_challenge, now + CODE_TTL_S
        )
        return code

    def redeem(
        self, *, code: str, redirect_uri: str, code_verifier: str, client_id: str
    ) -> dict[str, Any]:
        """The token response for a code (PKCE verified); the code is consumed."""
        grant = self._codes.pop(code, None)
        if grant is None or grant.expires < time.monotonic():
            raise MapiError("invalid or expired code", code=ErrorCode.UNAUTHENTICATED)
        if client_id != grant.client_id or redirect_uri != grant.redirect_uri:
            raise MapiError("client or redirect_uri mismatch", code=ErrorCode.UNAUTHENTICATED)
        challenge = _b64(hashlib.sha256(code_verifier.encode("ascii", "replace")).digest())
        if not hmac.compare_digest(challenge, grant.code_challenge):
            raise MapiError("PKCE verification failed", code=ErrorCode.UNAUTHENTICATED)
        return {
            "access_token": secrets.token_urlsafe(24),
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL_S,
            "id_token": self.id_token(grant.email, nonce=grant.nonce),
        }

    def id_token(self, email: str, *, nonce: str | None, audience: str | None = None) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": DEV_ISSUER,
            "sub": self.subject(email),
            "aud": audience or self.client_id,
            "iat": now,
            "exp": now + TOKEN_TTL_S,
            "email": email,
            "email_verified": True,
            "name": email.split("@", 1)[0].replace(".", " ").title(),
        }
        if nonce is not None:
            claims["nonce"] = nonce
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})


def _page(idp: DevIdP, params: Mapping[str, str]) -> str:
    links = []
    for email in idp.users:
        href = f"{DEVIDP_PREFIX}/authorize?" + urlencode({**params, "login_hint": email})
        links.append(f'<li><a href="{html.escape(href)}">{html.escape(email)}</a></li>')
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<title>Development sign-in</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto'>"
        "<h1>Development sign-in</h1>"
        "<p>This identity provider exists only in the <code>dev</code> profile. "
        "Choose an account:</p><ul>" + "".join(links) + "</ul></body></html>"
    )


def _idp(request: Request) -> DevIdP:
    idp = getattr(request.app.state.ctx, "devidp", None)
    if not isinstance(idp, DevIdP):
        raise MapiError("the development IdP is not enabled", code=ErrorCode.NOT_FOUND)
    return idp


def mount_devidp(app: FastAPI, settings: Settings) -> None:
    """Serve the IdP's browser and protocol endpoints under :data:`DEVIDP_PREFIX` (the
    :class:`DevIdP` instance is ``ServerContext.devidp``)."""
    if not settings.devidp_enabled:
        raise RuntimeError("the development IdP is only available with MAPI_PROFILE=dev")
    router = APIRouter(prefix=DEVIDP_PREFIX, include_in_schema=False)

    @router.get("/.well-known/openid-configuration", name="devidp_discovery")
    async def discovery(request: Request) -> JSONResponse:
        meta = await _idp(request).metadata()
        return JSONResponse(
            {
                "issuer": meta.issuer,
                "authorization_endpoint": meta.authorization_endpoint,
                "token_endpoint": meta.token_endpoint,
                "jwks_uri": meta.jwks_uri,
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "code_challenge_methods_supported": ["S256"],
            }
        )

    @router.get("/jwks", name="devidp_jwks")
    async def jwks(request: Request) -> JSONResponse:
        return JSONResponse(_idp(request).jwks_document())

    @router.get("/authorize", name="devidp_authorize")
    async def authorize(
        request: Request,
        response_type: Annotated[str, Query()],
        client_id: Annotated[str, Query()],
        redirect_uri: Annotated[str, Query()],
        state: Annotated[str, Query(max_length=256)],
        code_challenge: Annotated[str, Query(max_length=128)],
        code_challenge_method: Annotated[str, Query()] = "plain",
        nonce: Annotated[str | None, Query(max_length=256)] = None,
        scope: Annotated[str, Query(max_length=512)] = "openid",
        login_hint: Annotated[str | None, Query(max_length=320)] = None,
    ) -> Response:
        if response_type != "code":
            raise MapiError("only response_type=code", code=ErrorCode.SCHEMA_INVALID)
        if login_hint is None:
            params = {k: v for k, v in request.query_params.items() if k != "login_hint"}
            return HTMLResponse(
                _page(_idp(request), params),
                headers={
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"
                },
            )
        code = _idp(request).grant(
            email=login_hint.strip().lower(),
            client_id=client_id,
            redirect_uri=redirect_uri,
            nonce=nonce,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        sep = "&" if "?" in redirect_uri else "?"
        target = f"{redirect_uri}{sep}{urlencode({'code': code, 'state': state})}"
        return RedirectResponse(target, status_code=302)

    @router.post("/token", name="devidp_token")
    async def token(request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > 8192:
            raise MapiError("request too large", code=ErrorCode.PAYLOAD_TOO_LARGE)
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        if form.get("grant_type") != "authorization_code":
            raise MapiError("unsupported grant_type", code=ErrorCode.SCHEMA_INVALID)
        body = _idp(request).redeem(
            code=form.get("code", ""),
            redirect_uri=form.get("redirect_uri", ""),
            code_verifier=form.get("code_verifier", ""),
            client_id=form.get("client_id", ""),
        )
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    app.include_router(router)
