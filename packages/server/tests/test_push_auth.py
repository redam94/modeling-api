"""``POST /internal/events`` authentication: Google-signed Pub/Sub OIDC tokens (verified
against a JWKS by :class:`~mapi_server.services.pubsub.PushVerifier`, then
:func:`mapi_spec.auth.check_push_claims`), the internal bearer, and fail-closed behaviour in
multi-tenant profiles. Refusals are audited."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.pubsub import GOOGLE_JWKS_URL, JwksCache, PushVerifier
from mapi_server.settings import Profile
from mapi_spec.auth import PushAuthExpectation
from sqlalchemy import select

pytestmark = pytest.mark.integration

AUDIENCE = "https://api.mapi.test/internal/events"
PUSHER = "pubsub-push@mapi-control.iam.gserviceaccount.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks() -> Mapping[str, Any]:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    jwk.update(kid="g1", alg="RS256", use="sig")
    return {"keys": [jwk]}


def _jwks_cache() -> JwksCache:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=dict(_jwks())))
    return JwksCache(GOOGLE_JWKS_URL, client=httpx.AsyncClient(transport=transport))


def _token(key: Any = KEY, **overrides: Any) -> str:
    t = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "sub": "1234567890",
        "email": PUSHER,
        "email_verified": True,
        "iat": t,
        "exp": t + 300,
        **overrides,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "g1"})


@pytest.fixture
def push_ctx(ctx: ServerContext) -> ServerContext:
    ctx.settings = ctx.settings.model_copy(
        update={"pubsub_push_audience": AUDIENCE, "pubsub_push_service_account": PUSHER}
    )
    ctx.push_verifier = PushVerifier(
        PushAuthExpectation(audience=AUDIENCE, service_account_email=PUSHER), _jwks_cache()
    )
    return ctx


async def _post(client: httpx.AsyncClient, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.post("/internal/events", json={}, headers=headers)


async def test_google_signed_push_tokens(
    push_ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    # authenticated: the (empty) envelope itself is what is wrong now
    assert (await _post(client, _token())).status_code == 422
    t = int(time.time())
    refused = {
        "no token": None,
        "wrong signer": _token(OTHER),
        "wrong audience": _token(aud="https://elsewhere.test"),
        "wrong account": _token(email="attacker@evil.iam.gserviceaccount.com"),
        "unverified email": _token(email_verified=False),
        "expired": _token(iat=t - 3600, exp=t - 1800),
        "not google": _token(iss="https://evil.test"),
        "garbage": "not-a-jwt",
    }
    for why, token in refused.items():
        r = await _post(client, token)
        # 401: not authenticated; 403: a valid Google token, but not for our push identity
        assert r.status_code in (401, 403), why
    async with push_ctx.db.session() as s:
        rejected = [
            e for e in (await s.execute(select(m.AuditEvent))).scalars()
            if e.action == "event.rejected"
        ]  # fmt: skip
    assert len(rejected) == len(refused)


async def test_multi_tenant_profiles_fail_closed_without_push_auth(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ctx.settings = ctx.settings.model_copy(update={"profile": Profile.MULTI_TENANT})
    assert (await _post(client, None)).status_code == 401
    ctx.settings = ctx.settings.model_copy(update={"internal_token": "s3cret"})
    assert (await _post(client, "nope")).status_code == 401
    assert (await _post(client, "s3cret")).status_code == 422
