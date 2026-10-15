"""A local stand-in for Google's OIDC issuer and for Pub/Sub push delivery (tests only).

* :class:`LocalGoogleIdP` — an RSA key pair published as a JWKS (served through an
  ``httpx.MockTransport``) that mints RS256 ID tokens shaped like the ones Pub/Sub attaches
  to authenticated pushes (``iss``, ``aud``, ``email``, ``email_verified``, ``iat``,
  ``exp``, ``sub``);
* :class:`PubSubPusher` — wraps Cloud Logging entries in push envelopes and POSTs them to
  ``/internal/events`` with a fresh token, redelivering on non-2xx answers (like Pub/Sub,
  with back-off) and optionally duplicating or reordering deliveries.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mapi_server.services.pubsub import GOOGLE_JWKS_URL, JwksCache

__all__ = ["PUSH_SA", "SUBSCRIPTION", "LocalGoogleIdP", "PubSubPusher"]

PUSH_SA = "mapi-events-push@mapi-test-control.iam.gserviceaccount.com"
SUBSCRIPTION = "projects/mapi-test-control/subscriptions/mapi-run-events-push"
AUDIENCE = "https://api.mapi.test/internal/events"


class LocalGoogleIdP:
    """RSA keys + JWKS + ID-token minting, so OIDC verification runs for real offline."""

    def __init__(self, *, kid: str = "test-key-1", issuer: str = "https://accounts.google.com"):
        self.kid = kid
        self.issuer = issuer
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.jwks_requests = 0
        self.max_age = 3600

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update({"kid": self.kid, "alg": "RS256", "use": "sig"})
        return {"keys": [jwk]}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.jwks_requests += 1
            return httpx.Response(
                200,
                json=self.jwks(),
                headers={"cache-control": f"public, max-age={self.max_age}"},
            )

        return httpx.MockTransport(handler)

    def jwks_cache(self, **kwargs: Any) -> JwksCache:
        return JwksCache(
            GOOGLE_JWKS_URL, client=httpx.AsyncClient(transport=self.transport()), **kwargs
        )

    def mint(
        self,
        *,
        audience: str = AUDIENCE,
        email: str = PUSH_SA,
        email_verified: bool = True,
        lifetime_s: int = 3600,
        issued_at: float | None = None,
        kid: str | None = None,
        key: rsa.RSAPrivateKey | None = None,
        algorithm: str = "RS256",
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        iat = int(issued_at if issued_at is not None else time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "aud": audience,
            "azp": "1234567890",
            "sub": "1234567890",
            "email": email,
            "email_verified": email_verified,
            "iat": iat,
            "exp": iat + lifetime_s,
            **(extra or {}),
        }
        signing_key: Any = key or self._key
        if algorithm.startswith("HS"):
            signing_key = "not-a-real-secret-but-long-enough-for-hs256"
        return jwt.encode(
            claims, signing_key, algorithm=algorithm, headers={"kid": kid or self.kid}
        )


def envelope(
    entry: Mapping[str, Any],
    *,
    message_id: str,
    subscription: str = SUBSCRIPTION,
    delivery_attempt: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message": {
            "data": base64.b64encode(json.dumps(entry).encode()).decode(),
            "attributes": {"logging.googleapis.com/timestamp": entry.get("timestamp", "")},
            "messageId": message_id,
            "publishTime": entry.get("timestamp") or "2026-10-15T09:30:00Z",
        },
        "subscription": subscription,
    }
    if delivery_attempt is not None:
        body["deliveryAttempt"] = delivery_attempt
    return body


@dataclass
class PubSubPusher:
    """Delivers log entries to ``/internal/events`` the way an OIDC push subscription does."""

    client: httpx.AsyncClient
    idp: LocalGoogleIdP
    path: str = "/internal/events"
    audience: str = AUDIENCE
    subscription: str = SUBSCRIPTION
    duplicate_rate: float = 0.0
    """Probability of delivering a message twice (at-least-once delivery)."""
    max_attempts: int = 8
    backoff_s: float = 0.02
    seed: int = 0
    delivered: int = 0
    redelivered: int = 0
    failures: list[tuple[int, str]] = field(default_factory=list)
    results: dict[str, int] = field(default_factory=dict)
    _ids: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    async def push(self, entry: dict[str, Any]) -> None:
        self._ids += 1
        message_id = f"m{self._ids}"
        copies = 2 if self._rng.random() < self.duplicate_rate else 1
        for _ in range(copies):
            await self._deliver(entry, message_id)

    async def _deliver(self, entry: Mapping[str, Any], message_id: str) -> None:
        for attempt in range(1, self.max_attempts + 1):
            body = envelope(
                entry,
                message_id=message_id,
                subscription=self.subscription,
                delivery_attempt=attempt,
            )
            token = self.idp.mint(audience=self.audience)
            r = await self.client.post(
                self.path, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            if 200 <= r.status_code < 300:
                self.delivered += 1
                result = str(r.json().get("result"))
                self.results[result] = self.results.get(result, 0) + 1
                return
            self.redelivered += 1
            self.failures.append((r.status_code, r.text[:200]))
            await asyncio.sleep(self.backoff_s * attempt)
        raise AssertionError(f"push of {message_id} failed {self.max_attempts} times")
