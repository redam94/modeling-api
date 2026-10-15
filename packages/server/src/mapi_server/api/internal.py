"""``POST /internal/events``: the cloud event path (Logging sink → Pub/Sub push).

Authentication, strongest configured first:

* **Pub/Sub OIDC** (``MAPI_PUBSUB_PUSH_AUDIENCE`` + ``MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT``):
  the bearer must be a Google-signed ID token for our audience and push service account
  (:class:`~mapi_server.services.pubsub.PushVerifier`), and the body must be a Pub/Sub push
  envelope from an accepted subscription whose data is a Cloud Logging entry;
* a static bearer (``MAPI_INTERNAL_TOKEN``) for local and compose setups;
* nothing — only in the single-tenant profile. The multi-tenant profile fails closed: without
  push authentication every request is refused.

The event inside is then HMAC-verified with its attempt key and deduplicated on
``(run, attempt, seq)``. Verification failures are acknowledged with 200 (redelivery would
not help) and counted as sandbox violations; unauthenticated pushes get 401/403 and
malformed envelopes 422, so Pub/Sub retries them and finally dead-letters them.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header
from mapi_spec.auth import PubSubPushEnvelope, PushAuthExpectation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import InvalidEvent
from pydantic import BaseModel, ValidationError

from mapi_server.api.common import Ctx
from mapi_server.context import ServerContext
from mapi_server.services.events import EventIngestor, IngestResult, events_from_envelope
from mapi_server.services.pubsub import JwksCache, PushVerifier, decode_push
from mapi_server.settings import Profile

__all__ = ["IngestResponse", "push_verifier", "router"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class IngestResponse(BaseModel):
    result: IngestResult


def push_verifier(ctx: ServerContext) -> PushVerifier | None:
    """The context's OIDC verifier, built on first use (``None`` when not configured)."""
    s = ctx.settings
    if s.pubsub_push_audience is None or s.pubsub_push_service_account is None:
        return None
    if ctx.push_verifier is None:
        jwks = JwksCache(s.google_jwks_url)
        ctx.on_shutdown.append(jwks.aclose)
        ctx.push_verifier = PushVerifier(
            PushAuthExpectation(
                audience=s.pubsub_push_audience,
                service_account_email=s.pubsub_push_service_account,
            ),
            jwks,
            subscriptions=s.pubsub_push_subscriptions,
        )
    return ctx.push_verifier


@router.post("/events", response_model=IngestResponse, operation_id="ingest_event")
async def ingest_event(
    ctx: Ctx,
    body: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> IngestResponse:
    verifier = push_verifier(ctx)
    execution: str | None
    if verifier is not None:
        await verifier.verify_token(authorization)
        try:
            envelope = PubSubPushEnvelope.model_validate(body)
        except ValidationError:
            raise MapiError(
                "the body is not a Pub/Sub push envelope", code=ErrorCode.SCHEMA_INVALID
            ) from None
        verifier.check_subscription(envelope)
        try:
            pushed = decode_push(envelope)
        except InvalidEvent as exc:
            raise MapiError(exc.detail, code=ErrorCode.SCHEMA_INVALID) from None
        event, execution = pushed.event, pushed.execution
        if (pushed.delivery_attempt or 1) > 1:
            log.info(
                "Pub/Sub redelivery %s of message %s", pushed.delivery_attempt, pushed.message_id
            )
    else:
        token = ctx.settings.internal_token
        if token is not None:
            presented = (authorization or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(presented.encode(), token.encode()):
                raise MapiError("a valid bearer token is required", code=ErrorCode.UNAUTHENTICATED)
        elif ctx.settings.profile is Profile.MULTI_TENANT:
            raise MapiError(
                "push authentication is not configured (MAPI_PUBSUB_PUSH_AUDIENCE)",
                code=ErrorCode.UNAUTHENTICATED,
            )
        try:
            event, execution = events_from_envelope(body)
        except InvalidEvent as exc:
            raise MapiError(exc.detail, code=ErrorCode.SCHEMA_INVALID) from None
    result = await EventIngestor(ctx).ingest(event, execution=execution)
    return IngestResponse(result=result)
