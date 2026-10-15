"""``POST /internal/events``: the cloud event path (Logging sink → Pub/Sub push).

The body is a Pub/Sub push envelope whose message data is a Cloud Logging entry with the
``{"mapi_event": …}`` jsonPayload (a bare log entry or line object is accepted too). Each
event is HMAC-verified with its attempt key and deduplicated on ``(run, attempt, seq)``.
Verification failures are acknowledged with 200 (redelivery would not help) and counted as
sandbox violations; only malformed envelopes are rejected (422). In the cloud the endpoint
sits behind Pub/Sub OIDC (M2); locally it can require ``MAPI_INTERNAL_TOKEN``.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.run import InvalidEvent
from pydantic import BaseModel

from mapi_server.api.common import Ctx
from mapi_server.services.events import EventIngestor, IngestResult, events_from_envelope

__all__ = ["IngestResponse", "router"]

router = APIRouter(prefix="/internal", tags=["internal"])


class IngestResponse(BaseModel):
    result: IngestResult


@router.post("/events", response_model=IngestResponse, operation_id="ingest_event")
async def ingest_event(
    ctx: Ctx,
    body: Annotated[dict[str, Any], Body()],
    authorization: Annotated[str | None, Header()] = None,
) -> IngestResponse:
    token = ctx.settings.internal_token
    if token is not None:
        presented = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(presented.encode(), token.encode()):
            raise MapiError("a valid bearer token is required", code=ErrorCode.UNAUTHENTICATED)
    try:
        event, execution = events_from_envelope(body)
    except InvalidEvent as exc:
        raise MapiError(exc.detail, code=ErrorCode.SCHEMA_INVALID) from None
    result = await EventIngestor(ctx).ingest(event, execution=execution)
    return IngestResponse(result=result)
