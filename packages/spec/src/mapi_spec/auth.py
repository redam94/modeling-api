"""Authentication protocols shared by the server and the web app (plan §8.1, §8.7, §11).

**Browser sessions (BFF).** The SPA never sees an OAuth token. ``GET /auth/login`` starts an
OpenID Connect Authorization Code flow with PKCE (S256) and a ``nonce``; the transaction is
kept server-side, referenced by a short-lived ``__Host-`` cookie. ``GET /auth/callback``
checks ``state``, exchanges the code, verifies the ID token (issuer, audience, nonce, expiry)
and sets the session cookie: ``HttpOnly; Secure; SameSite=Strict; Path=/`` with no
``Domain``. Because the cookie is ``SameSite=Strict`` and every state-changing endpoint takes
JSON (a CORS-preflighted content type), cross-site requests never carry it.

**Programs** send ``Authorization: Bearer mapi_…`` (see :mod:`mapi_spec.tokens`).

**Pub/Sub push** to ``POST /internal/events`` carries a Google-signed OIDC ID token; after
verifying the signature, the server checks its claims with :func:`check_push_claims`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlencode

from pydantic import AliasChoices, AwareDatetime, Field, field_validator

from mapi_spec._dto import EXAMPLE_TS, ResponseModel, dto_examples
from mapi_spec._types import StrictModel
from mapi_spec.tenancy import UserPrincipal

__all__ = [
    "DEV_SESSION_COOKIE",
    "GOOGLE_ISSUERS",
    "LOGIN_COOKIE",
    "LOGIN_TTL",
    "MAX_PUSH_DATA_BYTES",
    "OIDC_SCOPES",
    "SESSION_ABSOLUTE_TTL",
    "SESSION_COOKIE",
    "SESSION_IDLE_TTL",
    "CookieSpec",
    "LoginTransaction",
    "PubSubMessage",
    "PubSubPushEnvelope",
    "PushAuthExpectation",
    "SessionInfo",
    "build_authorize_url",
    "check_push_claims",
    "pkce_challenge",
    "pkce_verifier",
    "random_state",
    "safe_return_to",
]

SESSION_COOKIE: Final = "__Host-mapi_session"
"""The session cookie (the ``__Host-`` prefix forces ``Secure``, ``Path=/``, no ``Domain``)."""
DEV_SESSION_COOKIE: Final = "mapi_session"
"""Plain-HTTP local development only (browsers refuse ``__Host-`` cookies without TLS on
some hosts). Never used in a multi-tenant profile."""
LOGIN_COOKIE: Final = "__Host-mapi_login"
"""References the pending :class:`LoginTransaction` between login and callback."""
LOGIN_TTL: Final = timedelta(minutes=10)
SESSION_IDLE_TTL: Final = timedelta(hours=2)
SESSION_ABSOLUTE_TTL: Final = timedelta(hours=12)
OIDC_SCOPES: Final = ("openid", "email", "profile")
GOOGLE_ISSUERS: Final = ("https://accounts.google.com", "accounts.google.com")
MAX_PUSH_DATA_BYTES: Final = 256 * 1024
"""Upper bound on the decoded ``message.data`` of one push (a log entry with one event)."""


# ======================================================================================= PKCE


def pkce_verifier(n_bytes: int = 32) -> str:
    """A fresh RFC 7636 ``code_verifier`` (43-128 unreserved characters)."""
    if not 32 <= n_bytes <= 96:
        raise ValueError("use 32-96 random bytes")
    return base64.urlsafe_b64encode(secrets.token_bytes(n_bytes)).rstrip(b"=").decode("ascii")


_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


def pkce_challenge(verifier: str) -> str:
    """The S256 ``code_challenge`` of ``verifier``: ``BASE64URL(SHA256(verifier))``."""
    if not _VERIFIER_RE.match(verifier):
        raise ValueError("a code_verifier is 43-128 characters of [A-Za-z0-9-._~]")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def random_state() -> str:
    """An unguessable ``state``/``nonce`` value (256 bits, URL-safe)."""
    return secrets.token_urlsafe(32)


_RETURN_TO_RE = re.compile(r"^/(?![/\\])[A-Za-z0-9\-._~!$&'()*+,;=:@%/?#]{0,2047}$")


def safe_return_to(value: str | None, default: str = "/") -> str:
    """A same-origin path to send the browser to after login (blocks open redirects).

    Only absolute *paths* are accepted: no scheme, no host, no ``//`` or ``/\\`` prefix,
    no control characters. Anything else yields ``default``.
    """
    if value is None or not _RETURN_TO_RE.match(value):
        return default
    return value


class LoginTransaction(StrictModel):
    """Server-side state of one login between ``/auth/login`` and ``/auth/callback``."""

    state: str = Field(..., min_length=32, max_length=128)
    nonce: str = Field(..., min_length=32, max_length=128)
    code_verifier: str = Field(..., pattern=_VERIFIER_RE.pattern)
    return_to: str = "/"
    created_at: AwareDatetime

    @field_validator("return_to")
    @classmethod
    def _safe(cls, v: str) -> str:
        if safe_return_to(v, default="") != v:
            raise ValueError("return_to must be a same-origin path")
        return v

    @classmethod
    def start(cls, *, now: datetime, return_to: str | None = None) -> LoginTransaction:
        return cls(
            state=random_state(),
            nonce=random_state(),
            code_verifier=pkce_verifier(),
            return_to=safe_return_to(return_to),
            created_at=now,
        )

    def expired(self, now: datetime) -> bool:
        return now - self.created_at > LOGIN_TTL

    @property
    def code_challenge(self) -> str:
        return pkce_challenge(self.code_verifier)

    def __repr__(self) -> str:
        return f"LoginTransaction(return_to={self.return_to!r}, created_at={self.created_at!r})"


def build_authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    tx: LoginTransaction,
    scopes: Sequence[str] = OIDC_SCOPES,
    extra: Mapping[str, str] | None = None,
) -> str:
    """The IdP authorization URL for ``tx`` (Authorization Code + PKCE S256 + nonce)."""
    if not authorization_endpoint.startswith("https://"):
        raise ValueError("the authorization endpoint must be https")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": tx.state,
        "nonce": tx.nonce,
        "code_challenge": tx.code_challenge,
        "code_challenge_method": "S256",
    }
    for key, value in (extra or {}).items():
        if key in params:
            raise ValueError(f"{key!r} is set by the flow and cannot be overridden")
        params[key] = value
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


# ===================================================================================== cookies


@dataclass(frozen=True, slots=True)
class CookieSpec:
    """Attributes of a security-sensitive cookie (rendered by :meth:`header`)."""

    name: str
    max_age: int
    http_only: bool = True
    secure: bool = True
    same_site: Literal["Strict", "Lax"] = "Strict"
    path: str = "/"

    def __post_init__(self) -> None:
        if self.name.startswith("__Host-") and (not self.secure or self.path != "/"):
            raise ValueError("__Host- cookies must be Secure with Path=/")

    def header(self, value: str) -> str:
        """A ``Set-Cookie`` header value. ``value`` must be cookie-safe (no ``;`` or space)."""
        if not re.fullmatch(r"[A-Za-z0-9\-._~+/=]{0,4096}", value):
            raise ValueError("cookie values must be URL-safe tokens")
        parts = [f"{self.name}={value}", f"Path={self.path}", f"Max-Age={self.max_age}"]
        if self.secure:
            parts.append("Secure")
        if self.http_only:
            parts.append("HttpOnly")
        parts.append(f"SameSite={self.same_site}")
        return "; ".join(parts)

    def clear(self) -> str:
        """A ``Set-Cookie`` header value that deletes the cookie."""
        return CookieSpec(
            self.name, 0, self.http_only, self.secure, self.same_site, self.path
        ).header("")

    @classmethod
    def session(cls, *, secure: bool = True) -> CookieSpec:
        name = SESSION_COOKIE if secure else DEV_SESSION_COOKIE
        return cls(name, int(SESSION_ABSOLUTE_TTL.total_seconds()), secure=secure)

    @classmethod
    def login(cls, *, secure: bool = True) -> CookieSpec:
        # Lax: the IdP redirects back with a top-level cross-site GET, which Strict would
        # strip. The cookie only references a single-use, 10-minute transaction.
        name = LOGIN_COOKIE if secure else "mapi_login"
        return cls(name, int(LOGIN_TTL.total_seconds()), secure=secure, same_site="Lax")


class SessionInfo(ResponseModel):
    """``GET /auth/session``: is the browser signed in, as whom, until when."""

    authenticated: bool
    principal: UserPrincipal | None = None
    operator: bool = False
    expires_at: AwareDatetime | None = None
    login_url: str = Field("/auth/login", description="Where to send the browser to sign in")

    model_config = dto_examples(
        {
            "authenticated": True,
            "principal": {
                "kind": "user",
                "id": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
                "issuer": "https://accounts.google.com",
                "subject": "110169484474386276334",
                "email": "ana@acme.example",
                "email_verified": True,
                "name": "Ana Analyst",
            },
            "operator": False,
            "expires_at": "2026-10-15T21:30:00Z",
            "login_url": "/auth/login",
        },
        {"authenticated": False, "login_url": "/auth/login"},
    )


# ================================================================================== Pub/Sub push


class PubSubMessage(ResponseModel):
    """The ``message`` of a Pub/Sub push request (both field spellings are accepted)."""

    data: str = Field("", description="Base64 payload (a Cloud Logging entry for run events)")
    attributes: dict[str, str] = Field(default_factory=dict)
    message_id: str = Field(
        ..., validation_alias=AliasChoices("messageId", "message_id"), max_length=128
    )
    publish_time: AwareDatetime | None = Field(
        None, validation_alias=AliasChoices("publishTime", "publish_time")
    )
    ordering_key: str | None = Field(
        None, validation_alias=AliasChoices("orderingKey", "ordering_key")
    )

    def decoded(self, *, max_bytes: int = MAX_PUSH_DATA_BYTES) -> bytes:
        """The decoded payload (raises ``ValueError`` for bad base64 or an oversized body)."""
        if len(self.data) > (max_bytes * 4) // 3 + 4:
            raise ValueError("push data exceeds the size limit")
        try:
            raw = base64.b64decode(self.data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("push data is not valid base64") from exc
        if len(raw) > max_bytes:
            raise ValueError("push data exceeds the size limit")
        return raw


_SUBSCRIPTION_RE = (
    r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/subscriptions/[A-Za-z][A-Za-z0-9_.~+%-]{2,254}$"
)


class PubSubPushEnvelope(ResponseModel):
    """Body of a Pub/Sub push to ``POST /internal/events``.

    Unknown fields are ignored (Google adds fields over time). Authentication is the OIDC
    token in ``Authorization``, never anything in the body.
    """

    message: PubSubMessage
    subscription: str = Field(..., pattern=_SUBSCRIPTION_RE)
    delivery_attempt: int | None = Field(
        None, ge=1, validation_alias=AliasChoices("deliveryAttempt", "delivery_attempt")
    )

    model_config = dto_examples(
        {
            "message": {
                "data": "eyJqc29uUGF5bG9hZCI6eyJtYXBpX2V2ZW50Ijp7fX19",
                "attributes": {"logging.googleapis.com/timestamp": EXAMPLE_TS},
                "messageId": "9876543210",
                "publishTime": EXAMPLE_TS,
            },
            "subscription": "projects/mapi-prod-control/subscriptions/mapi-run-events-push",
            "deliveryAttempt": 1,
        }
    )


class PushAuthExpectation(StrictModel):
    """What the ID token on a Pub/Sub push must say (checked after signature verification)."""

    audience: str = Field(
        ..., min_length=1, description="The push endpoint URL (or configured audience)"
    )
    service_account_email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$")
    issuers: tuple[str, ...] = GOOGLE_ISSUERS
    leeway_seconds: int = Field(60, ge=0, le=300)


def check_push_claims(
    claims: Mapping[str, Any], expect: PushAuthExpectation, *, now: datetime
) -> list[str]:
    """Problems with a push token's claims (empty list: accept). Signature, key rotation and
    JWKS fetching are the verifier library's job; this checks *what* the token asserts."""
    problems: list[str] = []
    if claims.get("iss") not in expect.issuers:
        problems.append("issuer is not Google")
    aud = claims.get("aud")
    if aud != expect.audience and not (isinstance(aud, list) and expect.audience in aud):
        problems.append("audience mismatch")
    if claims.get("email") != expect.service_account_email:
        problems.append("token is not for the push service account")
    if claims.get("email_verified") is not True:
        problems.append("service-account email is not verified")
    ts = now.timestamp()
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int | float) or exp + expect.leeway_seconds < ts:
        problems.append("token expired")
    if not isinstance(iat, int | float) or iat - expect.leeway_seconds > ts:
        problems.append("token issued in the future")
    return problems
