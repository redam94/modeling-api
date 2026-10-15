"""Pub/Sub push authentication and decoding for ``POST /internal/events`` (plan §8.7, §9).

The cloud event path is: supervisor stdout → Cloud Logging (sink filter
``jsonPayload.mapi_event:*``) → Pub/Sub topic → **push subscription with OIDC** → this
endpoint. Each push carries ``Authorization: Bearer <ID token>`` minted by Google for the
subscription's service account. Before anything in the body is looked at:

1. the token's **signature** is verified against Google's JWKS (:class:`JwksCache`: keys
   cached per ``Cache-Control``, unknown ``kid`` refetched at most every ``min_refresh_s`` so
   forged key ids cannot make us hammer Google), RS256 only;
2. its **claims** are checked with :func:`mapi_spec.auth.check_push_claims` — issuer is
   Google, audience is ours, ``email`` is the push service account and verified, not
   expired;
3. the envelope's **subscription** must be one we created (when configured).

Only then is the message decoded (:func:`decode_push`): base64 data → Cloud Logging
``LogEntry`` → ``jsonPayload.mapi_event`` plus the ``run.googleapis.com/execution_name``
label. The run event itself is then HMAC-verified with the attempt key, deduplicated on
``(run, attempt, seq)`` and coalesced into ``run_progress`` by
:class:`~mapi_server.services.events.EventIngestor` — the OIDC token proves the push came from
*our* subscription, the HMAC proves the line came from *that run's* supervisor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import jwt
from mapi_spec.auth import PubSubPushEnvelope, PushAuthExpectation, check_push_claims
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunEvent
from mapi_spec.run import InvalidEvent, event_from_log_payload

from mapi_server.jsonlimits import loads_limited

__all__ = [
    "CLOUD_RUN_JOB_RESOURCE",
    "EXECUTION_LABEL",
    "GOOGLE_JWKS_URL",
    "JwksCache",
    "PushAuthError",
    "PushVerifier",
    "PushedEvent",
    "decode_push",
]

log = logging.getLogger(__name__)

GOOGLE_JWKS_URL: Final = "https://www.googleapis.com/oauth2/v3/certs"
EXECUTION_LABEL: Final = "run.googleapis.com/execution_name"
CLOUD_RUN_JOB_RESOURCE: Final = "cloud_run_job"
MAX_JWKS_BYTES: Final = 256 * 1024
_MAX_AGE = re.compile(r"max-age=(\d+)")


class PushAuthError(MapiError):
    """The push is not authenticated (401) or not for us (403)."""

    default_code = ErrorCode.UNAUTHENTICATED


class JwksCache:
    """Google's OIDC signing keys, fetched over HTTPS and cached.

    Args:
        url: the JWKS endpoint (Google's by default).
        client: an ``httpx.AsyncClient`` (tests pass one with a mock transport).
        min_refresh_s: never refetch more often than this for an unknown ``kid``.
        default_ttl_s: cache lifetime when the response has no ``max-age``.
        stale_grace_s: how long past expiry cached keys stay usable while refetching fails.
    """

    def __init__(
        self,
        url: str = GOOGLE_JWKS_URL,
        *,
        client: httpx.AsyncClient | None = None,
        min_refresh_s: float = 30.0,
        default_ttl_s: float = 3600.0,
        stale_grace_s: float = 6 * 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self._client = client
        self._owns_client = client is None
        self._min_refresh = min_refresh_s
        self._default_ttl = default_ttl_s
        self._stale_grace = stale_grace_s
        self._stale_until = float("-inf")
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._expires = float("-inf")
        self._fetched = float("-inf")
        self._lock = asyncio.Lock()
        self.fetches = 0

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _fresh(self) -> bool:
        return self._clock() < self._expires

    async def get(self, kid: str) -> Any | None:
        """The public key for ``kid`` (``None`` if Google does not publish it)."""
        if self._fresh() and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            if not (self._fresh() and kid in self._keys) and (
                not self._fresh() or self._clock() - self._fetched >= self._min_refresh
            ):
                await self._refresh()
        return self._keys.get(kid) if self._fresh() else None

    async def _refresh(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        self._fetched = self._clock()
        self.fetches += 1
        try:
            response = await self._client.get(self.url)
            response.raise_for_status()
            if len(response.content) > MAX_JWKS_BYTES:
                raise ValueError("JWKS response is too large")
            document = json.loads(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("could not fetch the OIDC JWKS from %s: %r", self.url, exc)
            if self._keys and self._clock() < self._stale_until:
                # Google rotates keys with days of overlap: ride out a short outage on the
                # keys we have rather than rejecting every push
                self._expires = min(self._stale_until, self._clock() + self._min_refresh)
            return
        keys: dict[str, Any] = {}
        for jwk in document.get("keys", []) if isinstance(document, Mapping) else []:
            if not isinstance(jwk, Mapping) or jwk.get("kty") != "RSA":
                continue
            if jwk.get("use", "sig") != "sig" or jwk.get("alg", "RS256") != "RS256":
                continue
            kid = jwk.get("kid")
            if not isinstance(kid, str):
                continue
            try:
                keys[kid] = jwt.PyJWK(dict(jwk)).key
            except (jwt.PyJWKError, jwt.PyJWTError, ValueError, TypeError):
                continue
        ttl = self._default_ttl
        match = _MAX_AGE.search(response.headers.get("cache-control", ""))
        if match:
            ttl = float(match.group(1))
        self._keys = keys
        self._expires = self._clock() + ttl
        self._stale_until = self._expires + self._stale_grace


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise PushAuthError("a Pub/Sub OIDC bearer token is required")
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise PushAuthError("the Authorization header must be 'Bearer <token>'")
    return token.strip()


class PushVerifier:
    """Verifies the OIDC token and the subscription of a Pub/Sub push (see module doc)."""

    def __init__(
        self,
        expect: PushAuthExpectation,
        jwks: JwksCache,
        *,
        subscriptions: Collection[str] = (),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.expect = expect
        self.jwks = jwks
        self.subscriptions = frozenset(subscriptions)
        self._now = now

    async def verify_token(self, authorization: str | None) -> dict[str, Any]:
        """The verified claims, or :class:`PushAuthError` (never echoes the token)."""
        token = _bearer(authorization)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise PushAuthError("the bearer token is not a JWT") from None
        if header.get("alg") != "RS256":
            raise PushAuthError("the push token must be signed with RS256")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise PushAuthError("the push token has no key id")
        key = await self.jwks.get(kid)
        if key is None:
            raise PushAuthError("the push token is signed by an unknown key")
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                options={
                    # the claims are checked below against the contract, with leeway
                    "verify_aud": False,
                    "verify_iss": False,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "require": ["iss", "aud", "exp", "iat"],
                },
            )
        except jwt.PyJWTError as exc:
            raise PushAuthError(f"the push token does not verify: {type(exc).__name__}") from None
        problems = check_push_claims(claims, self.expect, now=self._now())
        if problems:
            code = ErrorCode.UNAUTHENTICATED
            if any(
                p in ("audience mismatch", "token is not for the push service account")
                for p in problems
            ):
                code = ErrorCode.FORBIDDEN
            raise PushAuthError("push token rejected: " + "; ".join(problems), code=code)
        return claims

    def check_subscription(self, envelope: PubSubPushEnvelope) -> None:
        if self.subscriptions and envelope.subscription not in self.subscriptions:
            raise PushAuthError(
                "the push comes from an unexpected subscription", code=ErrorCode.FORBIDDEN
            )


@dataclass(frozen=True)
class PushedEvent:
    event: RunEvent
    execution: str | None
    message_id: str
    delivery_attempt: int | None


def decode_push(
    envelope: PubSubPushEnvelope, *, resource_type: str | None = CLOUD_RUN_JOB_RESOURCE
) -> PushedEvent:
    """Unwrap the run event from a verified push (raises :class:`InvalidEvent`)."""
    try:
        data = envelope.message.decoded()
    except ValueError as exc:
        raise InvalidEvent(f"Pub/Sub message data: {exc}") from None
    try:
        entry = loads_limited(data)
    except ValueError as exc:
        raise InvalidEvent("Pub/Sub message data is not a JSON log entry") from exc
    if not isinstance(entry, Mapping):
        raise InvalidEvent("Pub/Sub message data is not a JSON object")
    if resource_type is not None:
        resource = entry.get("resource")
        kind = resource.get("type") if isinstance(resource, Mapping) else None
        if kind is not None and kind != resource_type:
            raise InvalidEvent(f"log entry from an unexpected resource type {kind!r}")
    payload = entry.get("jsonPayload")
    if not isinstance(payload, Mapping):
        raise InvalidEvent("log entry has no jsonPayload object")
    execution: str | None = None
    labels = entry.get("labels")
    if isinstance(labels, Mapping) and isinstance(labels.get(EXECUTION_LABEL), str):
        execution = labels[EXECUTION_LABEL]
    return PushedEvent(
        event=event_from_log_payload(payload),
        execution=execution,
        message_id=envelope.message.message_id,
        delivery_attempt=envelope.delivery_attempt,
    )
