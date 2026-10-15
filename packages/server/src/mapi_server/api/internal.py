"""``POST /internal/events``: the cloud event path (Logging sink → Pub/Sub push).

Authentication, strongest configured first:

* **Pub/Sub OIDC** (``MAPI_PUBSUB_PUSH_AUDIENCE`` + ``MAPI_PUBSUB_PUSH_SERVICE_ACCOUNT``):
  the bearer must be a Google-signed ID token for our audience and push service account
  (:class:`~mapi_server.services.pubsub.PushVerifier`: signature against Google's JWKS, then
  :func:`mapi_spec.auth.check_push_claims`), and the body must be a Pub/Sub push envelope
  from an accepted subscription whose data is a Cloud Logging entry;
* a static bearer (``MAPI_INTERNAL_TOKEN``) for local and compose setups;
* nothing — only in the single-tenant profile. Multi-tenant profiles fail closed: without
  push authentication every request is refused.

Refused pushes are audited as ``event.rejected``. The event inside is then HMAC-verified
with its attempt key and deduplicated on ``(run, attempt, seq)``. Verification failures are
acknowledged with 200 (redelivery would not help) and counted as sandbox violations;
unauthenticated pushes get 401/403 and malformed envelopes 422, so Pub/Sub retries them and
finally dead-letters them.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.auth import PubSubPushEnvelope, PushAuthExpectation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunEvent
from mapi_spec.run import InvalidEvent
from mapi_spec.tenancy import ResourceKind
from mapi_spec.tokens import bearer_token
from pydantic import BaseModel, ValidationError

from mapi_server.api.common import Ctx
from mapi_server.context import ServerContext
from mapi_server.errors import unauthenticated
from mapi_server.services.audit import write_audit
from mapi_server.services.events import EventIngestor, IngestResult, events_from_envelope
from mapi_server.services.pubsub import JwksCache, PushVerifier, decode_push
from mapi_server.services.runs import now

__all__ = ["IngestResponse", "authenticate_push", "push_verifier", "router"]

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


async def authenticate_push(
    ctx: ServerContext, authorization: str | None, body: dict[str, Any]
) -> tuple[RunEvent, str | None]:
    """Authenticate a push and unwrap its run event (raises ``UNAUTHENTICATED``/
    ``FORBIDDEN`` for the caller, ``SCHEMA_INVALID`` for a malformed body)."""
    verifier = push_verifier(ctx)
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
        if (pushed.delivery_attempt or 1) > 1:
            log.info(
                "Pub/Sub redelivery %s of message %s", pushed.delivery_attempt, pushed.message_id
            )
        return pushed.event, pushed.execution
    token = ctx.settings.internal_token
    if token is not None:
        presented = (bearer_token(authorization) or "").encode()
        if not hmac.compare_digest(presented, token.encode()):
            raise unauthenticated("a valid bearer token is required")
    elif ctx.settings.multi_tenant:
        raise unauthenticated("push authentication is not configured (MAPI_PUBSUB_PUSH_AUDIENCE)")
    try:
        return events_from_envelope(body)
    except InvalidEvent as exc:
        raise MapiError(exc.detail, code=ErrorCode.SCHEMA_INVALID) from None


@router.post("/events", response_model=IngestResponse, operation_id="ingest_event")
async def ingest_event(
    ctx: Ctx,
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> IngestResponse:
    try:
        event, execution = await authenticate_push(ctx, authorization, body)
    except MapiError as exc:
        if exc.code in (ErrorCode.UNAUTHENTICATED, ErrorCode.FORBIDDEN):
            await write_audit(
                ctx.db,
                [
                    make_audit_event(
                        action=AuditAction.EVENT_REJECTED,
                        resource_kind=ResourceKind.EVENT,
                        outcome=AuditOutcome.DENIED,
                        ts=now(),
                        fingerprint_key=ctx.secrets.audit_key,
                        code=exc.code,
                        client_ip=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"),
                        details={"reason": exc.detail[:200]},
                    )
                ],
            )
        raise
    result = await EventIngestor(ctx).ingest(event, execution=execution)
    return IngestResponse(result=result)
