"""API tokens: wire format, scopes, at-rest hashing and the token DTOs (plan §8.2, §9).

Token format (all lowercase Crockford base32, ``0-9a-z`` without ``i l o u``)::

    mapi_<prefix>_<secret><checksum>
         8 chars  32 chars 7 chars

* ``prefix`` (40 random bits) is public: it is shown in listings and is the lookup key;
* ``secret`` (160 random bits) is shown exactly once, in :class:`TokenCreated`;
* ``checksum`` is the CRC-32 of ``mapi_<prefix>_<secret>`` (ASCII) in 7 base32 digits, so
  clients and secret scanners can reject typos and random strings without a database call.

At rest the server stores only :func:`hash_token` of the whole token (HMAC-SHA256 under a
server-side pepper) and compares with :func:`verify_token_hash` in constant time. No API
response ever contains the hash or, after creation, the secret.

A token acts **as its owner, narrowed by its scopes**: every request needs both the owner's
project role (:func:`mapi_spec.tenancy.can`) and a scope that covers the action
(:func:`scope_allows`). Tokens are bound to one project of one tenant.
"""

from __future__ import annotations

import hmac
import re
import secrets
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from mapi_spec._dto import (
    EXAMPLE_TS,
    Id,
    RequestModel,
    ResponseModel,
    TenantId,
    dto_examples,
    keyed_digest,
)
from mapi_spec._types import StrictModel
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "AUTHORIZATION_SCHEME",
    "DEFAULT_TOKEN_TTL_DAYS",
    "MAX_TOKEN_TTL_DAYS",
    "TOKEN_ALPHABET",
    "TOKEN_CHECKSUM_LEN",
    "TOKEN_ID_PREFIX",
    "TOKEN_NEVER",
    "TOKEN_PREFIX_LEN",
    "TOKEN_RE",
    "TOKEN_SECRET_LEN",
    "TOKEN_TAG",
    "ApiToken",
    "BudgetPeriod",
    "ParsedToken",
    "ScopeName",
    "TokenCreate",
    "TokenCreated",
    "TokenRecord",
    "TokenScope",
    "TokenStatus",
    "bearer_token",
    "check_scope",
    "generate_token",
    "hash_token",
    "looks_like_token",
    "parse_scope",
    "parse_token",
    "redact_token",
    "scope_allows",
    "token_checksum",
    "verify_token_hash",
]

TOKEN_TAG: Final = "mapi"
TOKEN_ALPHABET: Final = "0123456789abcdefghjkmnpqrstvwxyz"
"""Lowercase Crockford base32 (the same alphabet as resource ids)."""
TOKEN_PREFIX_LEN: Final = 8
TOKEN_SECRET_LEN: Final = 32
TOKEN_CHECKSUM_LEN: Final = 7
TOKEN_ID_PREFIX: Final = "tok"
AUTHORIZATION_SCHEME: Final = "Bearer"
DEFAULT_TOKEN_TTL_DAYS: Final = 90
MAX_TOKEN_TTL_DAYS: Final = 365

_B32 = f"[{TOKEN_ALPHABET}]"
TOKEN_RE: Final = re.compile(
    rf"^{TOKEN_TAG}_(?P<prefix>{_B32}{{{TOKEN_PREFIX_LEN}}})_"
    rf"(?P<secret>{_B32}{{{TOKEN_SECRET_LEN}}})(?P<checksum>{_B32}{{{TOKEN_CHECKSUM_LEN}}})$"
)
"""A syntactically valid token (the checksum is checked separately)."""
_TAIL_LEN = TOKEN_SECRET_LEN + TOKEN_CHECKSUM_LEN
_SCAN_RE = re.compile(rf"\b{TOKEN_TAG}_{_B32}{{{TOKEN_PREFIX_LEN}}}_{_B32}{{{_TAIL_LEN}}}\b")
_PREFIX_RE = rf"^{_B32}{{{TOKEN_PREFIX_LEN}}}$"


# ====================================================================================== format


def _b32(value: int, width: int) -> str:
    chars = []
    for _ in range(width):
        chars.append(TOKEN_ALPHABET[value & 31])
        value >>= 5
    if value:
        raise ValueError("value does not fit the requested width")
    return "".join(reversed(chars))


def _random_b32(n: int) -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(n))


def token_checksum(prefix: str, secret: str) -> str:
    """The 7-character checksum of ``mapi_<prefix>_<secret>`` (CRC-32, base32)."""
    body = f"{TOKEN_TAG}_{prefix}_{secret}".encode("ascii")
    return _b32(zlib.crc32(body) & 0xFFFFFFFF, TOKEN_CHECKSUM_LEN)


@dataclass(frozen=True, slots=True)
class ParsedToken:
    """The parts of a well-formed token. ``str(parsed)`` is the full token again."""

    prefix: str
    secret: str
    checksum: str

    def __str__(self) -> str:
        return f"{TOKEN_TAG}_{self.prefix}_{self.secret}{self.checksum}"

    def __repr__(self) -> str:  # never print the secret
        return f"ParsedToken(prefix={self.prefix!r}, secret=<redacted>)"

    @property
    def display(self) -> str:
        """The redacted form shown in listings: ``mapi_<prefix>_…``."""
        return redact_token(self.prefix)


def generate_token(prefix: str | None = None) -> ParsedToken:
    """A new random token (160-bit secret). ``prefix`` is for tests and migrations only."""
    if prefix is None:
        prefix = _random_b32(TOKEN_PREFIX_LEN)
    elif not re.fullmatch(_PREFIX_RE, prefix):
        raise ValueError(f"a token prefix is {TOKEN_PREFIX_LEN} lowercase base32 characters")
    secret = _random_b32(TOKEN_SECRET_LEN)
    return ParsedToken(prefix, secret, token_checksum(prefix, secret))


def parse_token(value: str) -> ParsedToken:
    """Split and checksum-verify a token. Raises ``UNAUTHENTICATED`` on any defect.

    The error never echoes the presented value (it may be a real secret with a typo).
    """
    m = TOKEN_RE.fullmatch(value.strip()) if len(value) <= 128 else None
    if m is None:
        raise MapiError("malformed API token", code=ErrorCode.UNAUTHENTICATED)
    parsed = ParsedToken(m["prefix"], m["secret"], m["checksum"])
    if not hmac.compare_digest(parsed.checksum, token_checksum(parsed.prefix, parsed.secret)):
        raise MapiError("API token checksum mismatch", code=ErrorCode.UNAUTHENTICATED)
    return parsed


def looks_like_token(text: str) -> bool:
    """True if ``text`` contains a checksum-valid token (for secret scanning and log scrubbing)."""
    for m in _SCAN_RE.finditer(text):
        try:
            parse_token(m.group(0))
        except MapiError:
            continue
        return True
    return False


def redact_token(prefix: str) -> str:
    """``mapi_<prefix>_…``: the only form of a token that may be displayed or logged."""
    return f"{TOKEN_TAG}_{prefix}_…"


def bearer_token(authorization: str | None) -> str | None:
    """Extract the credential from an ``Authorization: Bearer <token>`` header value."""
    if not authorization:
        return None
    scheme, _, credential = authorization.strip().partition(" ")
    if scheme.lower() != AUTHORIZATION_SCHEME.lower() or not credential.strip():
        return None
    return credential.strip()


def hash_token(token: str | ParsedToken, pepper: bytes) -> str:
    """At-rest digest of a whole token: ``hmac-sha256:<64 hex>`` under the server pepper."""
    return keyed_digest(str(token), pepper, length=64)


def verify_token_hash(token: str | ParsedToken, stored: str, pepper: bytes) -> bool:
    """Constant-time comparison of a presented token with its stored :func:`hash_token`."""
    return hmac.compare_digest(hash_token(token, pepper), stored)


# ====================================================================================== scopes


class ScopeName(StrEnum):
    """Scope families. ``score`` is always written with a target: ``score:<endpoint>``."""

    MODELS_READ = "models:read"
    RUNS_CREATE = "runs:create"
    SCORE = "score"
    PLUGINS_PUBLISH = "plugins:publish"
    DATASETS_WRITE = "datasets:write"
    ADMIN = "admin"


_ENDPOINT_TARGET = re.compile(r"^(?:\*|[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?)$")
_FIXED_SCOPES = frozenset(s.value for s in ScopeName if s is not ScopeName.SCORE)


def parse_scope(value: str) -> tuple[ScopeName, str | None]:
    """``"score:geo-mmm-prod"`` → ``(SCORE, "geo-mmm-prod")``; other scopes have no target.

    ``score:*`` covers every endpoint of the token's project.
    """
    if value in _FIXED_SCOPES:
        return ScopeName(value), None
    family, sep, target = value.partition(":")
    if family == ScopeName.SCORE.value and sep and _ENDPOINT_TARGET.fullmatch(target):
        return ScopeName.SCORE, target
    raise ValueError(
        f"unknown scope {value[:80]!r}; expected one of "
        f"{sorted(_FIXED_SCOPES)} or 'score:<endpoint alias>'"
    )


def check_scope(value: str) -> str:
    """Pydantic validator for :data:`TokenScope`."""
    parse_scope(value)
    return value


TokenScope = Annotated[
    str,
    Field(max_length=140, examples=["models:read", "runs:create", "score:geo-mmm-prod"]),
    AfterValidator(check_scope),
]
"""``models:read`` | ``runs:create`` | ``score:{endpoint}`` | ``plugins:publish`` |
``datasets:write`` | ``admin``."""


def scope_allows(
    scopes: Iterable[str],
    action: str,
    resource: str,
    *,
    resource_name: str | None = None,
) -> bool:
    """Does any of ``scopes`` cover ``action`` on ``resource``?

    ``action``/``resource`` are :class:`mapi_spec.tenancy.Action` /
    :class:`mapi_spec.tenancy.ResourceKind` values (plain strings here so this module does
    not depend on :mod:`mapi_spec.tenancy`). ``resource_name`` is the endpoint alias for
    ``score:<alias>``. Scopes only *narrow*: the owner's role is checked separately.

    ===================  =========================================================
    scope                covers
    ===================  =========================================================
    ``admin``            everything the owner's role allows
    ``models:read``      ``read`` on catalog, dataset, spec, run, model, endpoint,
                         plot, usage, project
    ``runs:create``      ``models:read`` + ``create``/``cancel`` run, ``run`` model,
                         endpoint, plot
    ``score:<alias>``    ``read`` + ``run`` on that endpoint (``*``: every endpoint)
    ``datasets:write``   ``read``/``create``/``update`` dataset
    ``plugins:publish``  ``read``/``create``/``delete`` plugin
    ===================  =========================================================

    No scope covers :data:`TOKEN_NEVER` (minting tokens): a child token would escape its
    parent's expiry, budget, rate limit and revocation, so minting needs a signed-in
    session.
    """
    if (resource, action) in TOKEN_NEVER:
        return False
    for raw in scopes:
        try:
            name, target = parse_scope(raw)
        except ValueError:
            continue
        if name is ScopeName.ADMIN:
            return True
        if name is ScopeName.SCORE:
            if (
                resource == "endpoint"
                and action in ("read", "run")
                and (target == "*" or (resource_name is not None and target == resource_name))
            ):
                return True
            continue
        if (resource, action) in _SCOPE_GRANTS[name]:
            return True
    return False


TOKEN_NEVER: Final[frozenset[tuple[str, str]]] = frozenset({("token", "create")})
"""``(resource, action)`` pairs no token may do, whatever its scopes (even ``admin``)."""

_READABLE = ("catalog", "dataset", "spec", "run", "model", "endpoint", "plot", "usage", "project")
_READ = frozenset((r, "read") for r in _READABLE)
_SCOPE_GRANTS: dict[ScopeName, frozenset[tuple[str, str]]] = {
    ScopeName.MODELS_READ: _READ,
    ScopeName.RUNS_CREATE: _READ
    | {
        ("run", "create"),
        ("run", "cancel"),
        ("model", "run"),
        ("endpoint", "run"),
        ("plot", "run"),
    },
    ScopeName.DATASETS_WRITE: frozenset(
        {("dataset", "read"), ("dataset", "create"), ("dataset", "update")}
    ),
    ScopeName.PLUGINS_PUBLISH: frozenset(
        {("plugin", "read"), ("plugin", "create"), ("plugin", "delete")}
    ),
    ScopeName.ADMIN: frozenset(),
    ScopeName.SCORE: frozenset(),
}


# ======================================================================================== DTOs


class BudgetPeriod(StrEnum):
    """Window of a per-token vCPU-second budget (UTC calendar day or month)."""

    DAY = "day"
    MONTH = "month"


class TokenStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


_TOKEN_ID = r"^tok_[0-9a-hjkmnp-tv-z]{26}$"
_PREFIX_FIELD = Field(..., pattern=_PREFIX_RE, examples=["7k2m9q4x"])
_SCOPES_FIELD = Field(..., min_length=1, max_length=32)


def _unique_scopes(scopes: list[str]) -> list[str]:
    if len(set(scopes)) != len(scopes):
        raise ValueError("scopes must be unique")
    return scopes


class TokenCreate(RequestModel):
    """``POST /v1/tokens``: mint a token for the caller in the selected project.

    The token can never do more than its owner's role allows at the time of use. Limits are
    optional: ``None`` means the tenant's defaults apply (they always apply in addition).
    """

    name: str = Field(..., min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")
    scopes: Annotated[list[TokenScope], AfterValidator(_unique_scopes)] = _SCOPES_FIELD
    expires_in_days: int = Field(DEFAULT_TOKEN_TTL_DAYS, ge=1, le=MAX_TOKEN_TTL_DAYS)
    rate_limit_per_minute: int | None = Field(None, ge=1, le=100_000)
    vcpu_seconds_budget: float | None = Field(None, gt=0)
    budget_period: BudgetPeriod = BudgetPeriod.MONTH

    model_config = dto_examples(
        {
            "name": "atlas-optimiser",
            "scopes": ["score:geo-mmm-prod", "models:read"],
            "expires_in_days": 30,
            "rate_limit_per_minute": 120,
            "vcpu_seconds_budget": 36000,
            "budget_period": "month",
        }
    )


class ApiToken(ResponseModel):
    """A token as listed by the API. It never carries the secret or its hash."""

    id: Id = Field(..., pattern=_TOKEN_ID)
    tenant_id: TenantId
    project_id: TenantId
    owner_id: Id = Field(..., description="Principal id of the user or service that minted it")
    name: str
    prefix: str = _PREFIX_FIELD
    display: str = Field(..., description="Redacted form, mapi_<prefix>_…")
    scopes: list[TokenScope]
    status: TokenStatus
    rate_limit_per_minute: int | None = None
    vcpu_seconds_budget: float | None = None
    budget_period: BudgetPeriod = BudgetPeriod.MONTH
    created_at: AwareDatetime
    expires_at: AwareDatetime
    last_used_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None

    model_config = dto_examples(
        {
            "id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
            "tenant_id": "acme",
            "project_id": "growth",
            "owner_id": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
            "name": "atlas-optimiser",
            "prefix": "7k2m9q4x",
            "display": "mapi_7k2m9q4x_…",
            "scopes": ["score:geo-mmm-prod", "models:read"],
            "status": "active",
            "rate_limit_per_minute": 120,
            "vcpu_seconds_budget": 36000,
            "budget_period": "month",
            "created_at": EXAMPLE_TS,
            "expires_at": "2026-11-14T09:30:00Z",
            "last_used_at": "2026-10-16T08:00:00Z",
        }
    )


class TokenCreated(ResponseModel):
    """``201`` body of ``POST /v1/tokens``: the only time ``secret`` is ever returned."""

    token: ApiToken
    secret: str = Field(
        ...,
        pattern=TOKEN_RE.pattern,
        description="The full token. Store it now; it is not shown again.",
    )

    model_config = dto_examples(
        {
            "token": ApiToken.model_json_schema()["examples"][0],
            "secret": str(
                ParsedToken(
                    "7k2m9q4x",
                    "0123456789abcdefghjkmnpqrstvwxyz",
                    token_checksum("7k2m9q4x", "0123456789abcdefghjkmnpqrstvwxyz"),
                )
            ),
        }
    )

    def __repr__(self) -> str:  # keep the secret out of logs and tracebacks
        return f"TokenCreated(token={self.token.id!r}, secret=<redacted>)"

    __str__ = __repr__


class TokenRecord(StrictModel):
    """The server-side row (internal, never serialised into an API response)."""

    id: Id = Field(..., pattern=_TOKEN_ID)
    tenant_id: TenantId
    project_id: TenantId
    owner_id: Id
    name: str
    prefix: str = _PREFIX_FIELD
    secret_hash: str = Field(..., pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    scopes: list[TokenScope] = _SCOPES_FIELD
    rate_limit_per_minute: int | None = Field(None, ge=1)
    vcpu_seconds_budget: float | None = Field(None, gt=0)
    budget_period: BudgetPeriod = BudgetPeriod.MONTH
    created_at: AwareDatetime
    expires_at: AwareDatetime
    last_used_at: AwareDatetime | None = None
    revoked_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _times(self) -> TokenRecord:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    def status_at(self, now: datetime) -> TokenStatus:
        if self.revoked_at is not None and self.revoked_at <= now:
            return TokenStatus.REVOKED
        if now >= self.expires_at:
            return TokenStatus.EXPIRED
        return TokenStatus.ACTIVE

    def usable_at(self, now: datetime) -> bool:
        return self.status_at(now) is TokenStatus.ACTIVE

    def to_public(self, now: datetime) -> ApiToken:
        """The API view: no secret, no hash."""
        data = self.model_dump(exclude={"secret_hash"})
        return ApiToken.model_validate(
            {**data, "display": redact_token(self.prefix), "status": self.status_at(now)}
        )

    def __repr__(self) -> str:
        return f"TokenRecord(id={self.id!r}, prefix={self.prefix!r}, secret_hash=<redacted>)"
