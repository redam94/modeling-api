"""Receiving webhooks: verify a delivery and read its event.

Every delivery is a ``POST`` with a JSON :class:`~mapi_spec.webhooks.WebhookEnvelope` body and
``Mapi-Signature: t=<unix seconds>,v1=<hex>`` (HMAC-SHA256 of ``"<t>.<raw body>"`` under the
webhook's secret; one ``v1`` per active secret during a rotation). :func:`verify` checks the
signature in constant time over the **raw** body, refuses timestamps more than 300 seconds
from the receiver's clock (which bounds replays), and returns the parsed envelope:

>>> from mapi_client.webhooks import verify                    # doctest: +SKIP
>>> event = verify(request.headers, request.body, os.environ["MAPI_WEBHOOK_SECRET"])
>>> if event.type == "endpoint.promoted": ...

Deliveries are at least once (retries, replays): use ``event.id`` to ignore duplicates.
Envelopes carry ids and states only - fetch results with the API.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.webhooks import (
    DELIVERY_HEADERS,
    SIGNATURE_HEADER,
    SIGNATURE_TOLERANCE_S,
    WebhookEnvelope,
    WebhookEventType,
    check_signature,
)
from pydantic import ValidationError

__all__ = [
    "EVENT_ID_HEADER",
    "EVENT_TYPE_HEADER",
    "SIGNATURE_HEADER",
    "WebhookEnvelope",
    "WebhookEventType",
    "is_valid",
    "verify",
]

EVENT_ID_HEADER: Final = DELIVERY_HEADERS["event_id"]
EVENT_TYPE_HEADER: Final = DELIVERY_HEADERS["event_type"]
MAX_BODY_BYTES: Final = 256 * 1024
"""Deliveries are small envelopes; anything larger is refused before it is parsed."""

Headers = Mapping[str, str] | Iterable[tuple[str, str]]


def _header(headers: Headers, name: str) -> str | None:
    """Case-insensitive header lookup (dicts, ``request.headers`` objects, pair lists)."""
    get = getattr(headers, "get", None)
    if callable(get):
        value = get(name)
        if value is None:
            value = get(name.lower())
        if value is not None:
            return str(value)
    items = headers.items() if isinstance(headers, Mapping) else headers
    wanted = name.lower()
    for key, value in items:
        if str(key).lower() == wanted:
            return str(value)
    return None


def _invalid(detail: str) -> MapiError:
    return MapiError(detail, code=ErrorCode.WEBHOOK_SIGNATURE_INVALID)


def verify(
    headers: Headers,
    body: bytes | str,
    secret: str | Sequence[str],
    *,
    tolerance: int = SIGNATURE_TOLERANCE_S,
    now: int | None = None,
) -> WebhookEnvelope:
    """Verify one delivery and return its event.

    Args:
        headers: The request headers (any mapping or ``(name, value)`` pairs; names are
            matched case-insensitively).
        body: The **raw** request body, exactly as received. A body parsed and re-serialised
            by a framework does not match its signature.
        secret: The webhook secret (``whsec_…``), or several (old and new during a rotation).
        tolerance: Seconds the signature timestamp may be away from ``now``.
        now: The receiver's clock (unix seconds; default: the system clock).

    Raises:
        MapiError: ``WEBHOOK_SIGNATURE_INVALID`` if the signature header is missing,
            malformed, too old or does not match; the body is not a webhook envelope; or
            ``Mapi-Event-Id`` names another event than the body.
    """
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    if len(raw) > MAX_BODY_BYTES:
        raise _invalid(f"the delivery body exceeds {MAX_BODY_BYTES} bytes")
    secrets_ = [secret] if isinstance(secret, str) else list(secret)
    if not secrets_ or not all(secrets_):
        raise ValueError("give the webhook secret (whsec_...)")
    check_signature(raw, _header(headers, SIGNATURE_HEADER), secrets_, now=now, tolerance=tolerance)
    # authenticated from here on: the body was sent by the platform
    try:
        envelope = WebhookEnvelope.model_validate_json(raw)
    except ValidationError:
        raise _invalid("the signed body is not a webhook envelope") from None
    event_id = _header(headers, EVENT_ID_HEADER)
    if event_id is not None and event_id != envelope.id:
        raise _invalid(f"{EVENT_ID_HEADER} does not match the signed event")
    return envelope


def is_valid(
    headers: Headers,
    body: bytes | str,
    secret: str | Sequence[str],
    *,
    tolerance: int = SIGNATURE_TOLERANCE_S,
    now: int | None = None,
) -> bool:
    """:func:`verify` as a boolean."""
    try:
        verify(headers, body, secret, tolerance=tolerance, now=now)
    except MapiError:
        return False
    return True
