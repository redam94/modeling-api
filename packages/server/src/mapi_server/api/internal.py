"""``POST /internal/events``: the cloud event path (Logging sink → Pub/Sub push).

The body is a Pub/Sub push envelope whose message data is a Cloud Logging entry with the
``{"mapi_event": …}`` jsonPayload (a bare log entry or line object is accepted too). Each
event is HMAC-verified with its attempt key and deduplicated on ``(run, attempt, seq)``.
Verification failures are acknowledged with 200 (redelivery would not help) and counted as
sandbox violations; only malformed envelopes are rejected (422).

**Who may push.** With ``MAPI_PUBSUB_AUDIENCE``/``MAPI_PUBSUB_SERVICE_ACCOUNT`` set, the
request must carry a Google-signed OIDC ID token for the push subscription's service account
(signature against Google's JWKS, then :func:`mapi_spec.auth.check_push_claims`: issuer,
audience, e-mail, ``email_verified``, expiry). Otherwise ``MAPI_INTERNAL_TOKEN`` is required
when set; a multi-tenant deployment with neither refuses every push (fail closed). Refused
pushes are audited as ``event.rejected``.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.auth import GOOGLE_ISSUERS, PushAuthExpectation, check_push_claims
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import InvalidEvent
from mapi_spec.tenancy import ResourceKind
from mapi_spec.tokens import bearer_token
from pydantic import BaseModel

from mapi_server.api.common import Ctx
from mapi_server.auth.oidc import OidcError, verify_jwt
from mapi_server.context import ServerContext
from mapi_server.errors import unauthenticated
from mapi_server.services.audit import write_audit
from mapi_server.services.events import EventIngestor, IngestResult, events_from_envelope
from mapi_server.services.runs import now

__all__ = ["IngestResponse", "authenticate_push", "router"]

router = APIRouter(prefix="/internal", tags=["internal"])


class IngestResponse(BaseModel):
    result: IngestResult


async def authenticate_push(ctx: ServerContext, authorization: str | None) -> None:
    """Raise ``UNAUTHENTICATED`` unless the caller may push run events."""
    s = ctx.settings
    if s.pubsub_audience and s.pubsub_service_account:
        raw = bearer_token(authorization)
        if raw is None or ctx.push_jwks is None:
            raise unauthenticated("a Google-signed OIDC token is required")
        try:
            claims = await verify_jwt(
                raw,
                jwks=ctx.push_jwks.get,
                algorithms=("RS256",),
                audience=s.pubsub_audience,
                issuers=GOOGLE_ISSUERS,
            )
        except OidcError as exc:
            raise unauthenticated(f"push token rejected ({exc.reason})") from None
        expect = PushAuthExpectation(
            audience=s.pubsub_audience, service_account_email=s.pubsub_service_account
        )
        problems = check_push_claims(claims, expect, now=now())
        if problems:
            raise unauthenticated("push token rejected: " + "; ".join(problems))
        return
    token = s.internal_token
    if token is not None:
        presented = (bearer_token(authorization) or "").encode()
        if not hmac.compare_digest(presented, token.encode()):
            raise unauthenticated("a valid bearer token is required")
        return
    if s.multi_tenant:
        raise unauthenticated("event pushes are not configured on this server")


@router.post("/events", response_model=IngestResponse, operation_id="ingest_event")
async def ingest_event(
    ctx: Ctx,
    request: Request,
    body: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> IngestResponse:
    try:
        await authenticate_push(ctx, authorization)
    except MapiError as exc:
        await write_audit(
            ctx.db,
            [
                make_audit_event(
                    action=AuditAction.EVENT_REJECTED,
                    resource_kind=ResourceKind.EVENT,
                    outcome=AuditOutcome.DENIED,
                    ts=now(),
                    fingerprint_key=ctx.secrets.audit_key,
                    code=ErrorCode.UNAUTHENTICATED,
                    client_ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    details={"reason": exc.detail[:200]},
                )
            ],
        )
        raise
    try:
        event, execution = events_from_envelope(body)
    except InvalidEvent as exc:
        raise MapiError(exc.detail, code=ErrorCode.SCHEMA_INVALID) from None
    result = await EventIngestor(ctx).ingest(event, execution=execution)
    return IngestResponse(result=result)
