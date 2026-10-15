"""Pub/Sub push to ``/internal/events``: OIDC verification with locally generated keys and
JWKS, envelope decoding, then HMAC, ``(run, attempt, seq)`` dedupe and coalesced progress."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.executors.fake_cloudrun import log_entry
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.pubsub import JwksCache, PushAuthError, PushVerifier, decode_push
from mapi_server.services.runs import load_run
from mapi_server.settings import Profile
from mapi_server_testkit.builders import queued_fit, signed_event
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.pubsub import (
    AUDIENCE,
    PUSH_SA,
    SUBSCRIPTION,
    LocalGoogleIdP,
    PubSubPusher,
    envelope,
)
from mapi_spec.auth import PubSubPushEnvelope, PushAuthExpectation
from mapi_spec.errors import ErrorCode
from mapi_spec.run import (
    InvalidEvent,
    ProgressPayload,
    RunnerPhase,
    RunState,
    StatePayload,
    encode_event_line,
    new_hmac_key,
)
from sqlalchemy import select

EXECUTION = "projects/mapi-test/locations/europe-west1/jobs/mapi-prod-base-bayes-m/executions/x1"


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(scope="module")
def idp() -> LocalGoogleIdP:
    return LocalGoogleIdP()


def _verifier(idp: LocalGoogleIdP, **kw: Any) -> PushVerifier:
    return PushVerifier(
        PushAuthExpectation(audience=AUDIENCE, service_account_email=PUSH_SA),
        idp.jwks_cache(),
        subscriptions=[SUBSCRIPTION],
        **kw,
    )


# ------------------------------------------------------------------------- JWKS


async def test_jwks_is_cached_and_unknown_kids_are_throttled(idp: LocalGoogleIdP) -> None:
    clock = Clock()
    before = idp.jwks_requests
    cache = idp.jwks_cache(clock=clock, min_refresh_s=30)
    assert await cache.get(idp.kid) is not None
    assert await cache.get(idp.kid) is not None
    assert idp.jwks_requests - before == 1
    assert await cache.get("forged-kid") is None  # within min_refresh: no refetch
    assert idp.jwks_requests - before == 1
    clock.t += 31
    assert await cache.get("forged-kid") is None  # one refetch, then throttled again
    assert await cache.get("forged-kid") is None
    assert idp.jwks_requests - before == 2
    clock.t += idp.max_age + 1  # expired: refetched even for a known kid
    assert await cache.get(idp.kid) is not None
    assert idp.jwks_requests - before == 3
    await cache.aclose()


async def test_jwks_outage_rides_on_cached_keys_then_fails_closed(idp: LocalGoogleIdP) -> None:
    clock = Clock()
    calls = {"n": 0}
    good = idp.transport()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return good.handle_request(request)
        return httpx.Response(503)

    cache = JwksCache(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=clock,
        default_ttl_s=60,
        min_refresh_s=10,
        stale_grace_s=100,
    )
    assert await cache.get(idp.kid) is not None
    clock.t += idp.max_age + 5  # expired; the refetch fails: keys stay usable for a while
    assert await cache.get(idp.kid) is not None
    clock.t += idp.max_age + 200  # past the grace period: reject
    assert await cache.get(idp.kid) is None


async def test_jwks_ignores_unusable_keys() -> None:
    doc = {"keys": [{"kty": "EC", "kid": "ec"}, {"kty": "RSA", "kid": "x", "n": "!", "e": "AQAB"}]}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=doc))
    cache = JwksCache(client=httpx.AsyncClient(transport=transport))
    assert await cache.get("ec") is None and await cache.get("x") is None


# ----------------------------------------------------------------------- tokens


async def test_valid_token_verifies(idp: LocalGoogleIdP) -> None:
    claims = await _verifier(idp).verify_token(f"Bearer {idp.mint()}")
    assert claims["email"] == PUSH_SA and claims["aud"] == AUDIENCE


@pytest.mark.parametrize(
    ("make", "code", "fragment"),
    [
        (lambda i: None, ErrorCode.UNAUTHENTICATED, "required"),
        (lambda i: "Basic abc", ErrorCode.UNAUTHENTICATED, "Bearer"),
        (lambda i: "Bearer not.a.jwt", ErrorCode.UNAUTHENTICATED, "not a JWT"),
        (lambda i: f"Bearer {i.mint(algorithm='HS256')}", ErrorCode.UNAUTHENTICATED, "RS256"),
        (lambda i: f"Bearer {i.mint(kid='rotated-away')}", ErrorCode.UNAUTHENTICATED, "unknown"),
        (
            lambda i: (
                "Bearer "
                + i.mint(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
            ),
            ErrorCode.UNAUTHENTICATED,
            "does not verify",
        ),
        (lambda i: f"Bearer {i.mint(audience='https://evil')}", ErrorCode.FORBIDDEN, "audience"),
        (
            lambda i: f"Bearer {i.mint(email='x@other.iam.gserviceaccount.com')}",
            ErrorCode.FORBIDDEN,
            "service account",
        ),
        (
            lambda i: f"Bearer {i.mint(email_verified=False)}",
            ErrorCode.UNAUTHENTICATED,
            "not verified",
        ),
        (
            lambda i: f"Bearer {i.mint(issued_at=datetime.now(UTC).timestamp() - 7200)}",
            ErrorCode.UNAUTHENTICATED,
            "expired",
        ),
        (
            lambda i: f"Bearer {i.mint(issued_at=datetime.now(UTC).timestamp() + 600)}",
            ErrorCode.UNAUTHENTICATED,
            "future",
        ),
        (
            lambda i: f"Bearer {LocalGoogleIdP(kid=i.kid, issuer='https://evil').mint()}",
            ErrorCode.UNAUTHENTICATED,
            "",
        ),
    ],
)
async def test_bad_tokens_are_rejected(
    idp: LocalGoogleIdP, make: Any, code: ErrorCode, fragment: str
) -> None:
    with pytest.raises(PushAuthError) as err:
        await _verifier(idp).verify_token(make(idp))
    assert err.value.code is code and fragment in err.value.detail


async def test_wrong_issuer_with_our_key_is_rejected(idp: LocalGoogleIdP) -> None:
    token = idp.mint(extra={"iss": "https://evil.example"})
    with pytest.raises(PushAuthError, match="issuer"):
        await _verifier(idp).verify_token(f"Bearer {token}")


async def test_clock_skew_within_leeway_is_accepted(idp: LocalGoogleIdP) -> None:
    skewed = _verifier(idp, now=lambda: datetime.now(UTC) - timedelta(seconds=30))
    await skewed.verify_token(f"Bearer {idp.mint()}")


def test_subscription_allow_list(idp: LocalGoogleIdP) -> None:
    v = _verifier(idp)
    entry = {"jsonPayload": {}}
    v.check_subscription(PubSubPushEnvelope.model_validate(envelope(entry, message_id="1")))
    other = envelope(
        entry, message_id="1", subscription="projects/attacker-proj/subscriptions/steal"
    )
    with pytest.raises(PushAuthError) as err:
        v.check_subscription(PubSubPushEnvelope.model_validate(other))
    assert err.value.code is ErrorCode.FORBIDDEN


# --------------------------------------------------------------------- decoding


def _entry(line: str, execution: str = EXECUTION) -> dict[str, Any]:
    return log_entry(
        project="mapi-test",
        region="europe-west1",
        job=execution.split("/executions/")[0],
        execution=execution,
        task_index=0,
        line=line,
        insert_id="i1",
        timestamp=datetime(2026, 10, 15, 9, 30, tzinfo=UTC),
    )


def test_decode_push_unwraps_the_log_entry() -> None:
    from mapi_spec.run import make_event

    ev = make_event("run_1", 1, 3, StatePayload(phase=RunnerPhase.FIT), key=new_hmac_key())
    body = envelope(_entry(encode_event_line(ev)), message_id="42", delivery_attempt=2)
    pushed = decode_push(PubSubPushEnvelope.model_validate(body))
    assert pushed.event == ev and pushed.execution == "x1"
    assert pushed.message_id == "42" and pushed.delivery_attempt == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["message"].update(data="%%%"),
        lambda b: b["message"].update(data=base64.b64encode(b"not json").decode()),
        lambda b: b["message"].update(data=base64.b64encode(b"[1]").decode()),
        lambda b: b["message"].update(
            data=base64.b64encode(json.dumps({"textPayload": "x"}).encode()).decode()
        ),
        lambda b: b["message"].update(
            data=base64.b64encode(
                json.dumps({"resource": {"type": "gce_instance"}, "jsonPayload": {}}).encode()
            ).decode()
        ),
        lambda b: b["message"].update(data=base64.b64encode(b"x" * (300 * 1024)).decode()),
    ],
)
def test_decode_push_rejects_garbage(mutate: Any) -> None:
    body = envelope({"jsonPayload": {}}, message_id="1")
    mutate(body)
    with pytest.raises(InvalidEvent):
        decode_push(PubSubPushEnvelope.model_validate(body))


# --------------------------------------------------------------------- endpoint


@pytest.fixture
async def provisioned(ctx: ServerContext) -> m.RunAttempt:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        att.external_id = EXECUTION  # what the cloudrun-jobs executor stores
        return att


@pytest.fixture
def oidc(ctx: ServerContext, idp: LocalGoogleIdP) -> LocalGoogleIdP:
    ctx.settings.pubsub_push_audience = AUDIENCE
    ctx.settings.pubsub_push_service_account = PUSH_SA
    ctx.settings.pubsub_push_subscriptions = [SUBSCRIPTION]
    ctx.push_verifier = _verifier(idp)
    return idp


def _push(att: m.RunAttempt, seq: int, payload: Any, **kw: Any) -> dict[str, Any]:
    ev = signed_event(att, seq, payload, key=kw.pop("key", None))
    entry = _entry(encode_event_line(ev), kw.pop("execution", EXECUTION))
    return envelope(entry, message_id=str(seq))


async def test_authenticated_push_is_ingested_once(
    client: httpx.AsyncClient, ctx: ServerContext, provisioned: m.RunAttempt, oidc: LocalGoogleIdP
) -> None:
    auth = {"Authorization": f"Bearer {oidc.mint()}"}
    body = _push(provisioned, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP))
    r = await client.post("/internal/events", json=body)
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    async with ctx.db.session() as s:
        run = await load_run(s, None, provisioned.run_id)
    assert run.state == RunState.PROVISIONING.value  # nothing happened without the token
    r = await client.post("/internal/events", json=body, headers=auth)
    assert r.status_code == 200 and r.json() == {"result": "accepted"}
    r = await client.post("/internal/events", json=body, headers=auth)  # redelivery
    assert r.json() == {"result": "duplicate"}
    async with ctx.db.session() as s:
        run = await load_run(s, None, provisioned.run_id)
    assert run.state == RunState.RUNNING.value


async def test_push_rejections(
    client: httpx.AsyncClient, ctx: ServerContext, provisioned: m.RunAttempt, oidc: LocalGoogleIdP
) -> None:
    body = _push(provisioned, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP))
    wrong_aud = {"Authorization": f"Bearer {oidc.mint(audience='https://elsewhere')}"}
    r = await client.post("/internal/events", json=body, headers=wrong_aud)
    assert r.status_code == 403
    auth = {"Authorization": f"Bearer {oidc.mint()}"}
    foreign = {**body, "subscription": "projects/attacker-proj/subscriptions/steal"}
    r = await client.post("/internal/events", json=foreign, headers=auth)
    assert r.status_code == 403
    bare = json.loads(base64.b64decode(body["message"]["data"]))  # not an envelope
    r = await client.post("/internal/events", json=bare, headers=auth)
    assert r.status_code == 422
    garbage = {**body, "message": {**body["message"], "data": "%%%"}}
    r = await client.post("/internal/events", json=garbage, headers=auth)
    assert r.status_code == 422
    # a valid push whose event is forged (wrong HMAC key): acknowledged, counted, ignored
    forged = _push(provisioned, 1, StatePayload(phase=RunnerPhase.FIT), key=new_hmac_key())
    r = await client.post("/internal/events", json=forged, headers=auth)
    assert r.status_code == 200 and r.json() == {"result": "bad_hmac"}
    # the execution label must name the attempt's execution
    other = _push(provisioned, 2, StatePayload(phase=RunnerPhase.FIT), execution=EXECUTION + "z")
    r = await client.post("/internal/events", json=other, headers=auth)
    assert r.json() == {"result": "identity_mismatch"}
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (provisioned.run_id, 1))
        run = await load_run(s, None, provisioned.run_id)
    assert att is not None and att.violations == 2
    assert run.state == RunState.PROVISIONING.value


async def test_progress_pushes_are_coalesced_despite_reordering_and_duplicates(
    client: httpx.AsyncClient, ctx: ServerContext, provisioned: m.RunAttempt, oidc: LocalGoogleIdP
) -> None:
    pusher = PubSubPusher(client, oidc, duplicate_rate=1.0)
    for seq, frac in ((1, 0.2), (4, 0.8), (2, 0.4), (3, 0.6)):
        progress = ProgressPayload(phase=RunnerPhase.SAMPLE, fraction=frac)
        ev = signed_event(provisioned, seq, progress)
        await pusher.push(_entry(encode_event_line(ev)))
    assert pusher.delivered == 8 and pusher.results == {"accepted": 2, "duplicate": 6}
    async with ctx.db.session() as s:
        rows = list((await s.execute(select(m.RunProgress))).scalars())
    assert len(rows) == 1 and rows[0].seq == 4 and rows[0].fraction == 0.8


async def test_multi_tenant_profile_fails_closed_without_push_auth(
    client: httpx.AsyncClient, ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ctx.settings.profile = Profile.MULTI_TENANT
    body = _push(provisioned, 0, StatePayload(phase=RunnerPhase.BOOTSTRAP))
    r = await client.post("/internal/events", json=body)
    assert r.status_code == 401 and "MAPI_PUBSUB_PUSH_AUDIENCE" in r.json()["detail"]
    ctx.settings.internal_token = "s3cret"  # a static bearer is the explicit local opt-in
    r = await client.post("/internal/events", json=body, headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


async def test_push_verifier_is_built_from_settings(ctx: ServerContext) -> None:
    from mapi_server.api.internal import push_verifier

    assert push_verifier(ctx) is None
    ctx.settings.pubsub_push_audience = AUDIENCE
    ctx.settings.pubsub_push_service_account = PUSH_SA
    v = push_verifier(ctx)
    assert v is not None and push_verifier(ctx) is v
    assert v.expect.audience == AUDIENCE and v.jwks.url.startswith("https://www.googleapis.com/")
