"""Sending webhook deliveries: the pinned-address HTTP sender, one-attempt delivery with
retries, auto-disable, and the relay that hands due deliveries to the queue.

* :class:`WebhookSender` resolves the host once, checks every address
  (:mod:`~mapi_server.services.webhooks.ssrf`), connects to the pinned IP with the original
  name as TLS SNI and ``Host`` (the certificate is verified against that name), follows no
  redirect, gives up after the policy timeout and reads at most 256 KiB of the answer. A
  fresh client per delivery: connections are never pooled across hosts sharing an address.
* :class:`WebhookDeliverer` runs **one try** of one delivery: claim (a lease, so a crashed
  worker's try is retried), sign (``Mapi-Signature``, one ``v1`` per active secret), send,
  record. A failed try is rescheduled by :func:`mapi_spec.webhooks.next_retry_delay`
  (exponential schedule plus up to 10% jitter) until ``MAPI_WEBHOOK_MAX_ATTEMPTS``; ``410``
  never retries. A delivery chain that ends failed or blocked counts one consecutive failure
  on its webhook; ``MAPI_WEBHOOK_AUTO_DISABLE_AFTER`` of them (or one ``410``) disable the
  webhook, with an audit row (``webhook.disabled``) and the reason shown on the webhook.
* :class:`WebhookRelay` (an orchestrator loop) finds due deliveries and defers them on the
  queue: procrastinate in production (:class:`~mapi_server.services.queue.ProcrastinateQueue`),
  in-process tasks otherwise (:class:`InlineWebhookQueue`). It also prunes old events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import httpx
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import PrincipalKind, ResourceKind
from mapi_spec.webhooks import (
    DELIVERY_HEADERS,
    SSRF_POLICY,
    DeliveryStatus,
    next_retry_delay,
    signature_header,
)
from sqlalchemy import delete, select

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.observability import metrics as obs_metrics
from mapi_server.services.audit import add_audit
from mapi_server.services.webhooks.crypto import SealError, SecretBox
from mapi_server.services.webhooks.outbox import envelope_body
from mapi_server.services.webhooks.ssrf import (
    DeliveryTarget,
    ResolutionFailed,
    Resolver,
    SystemResolver,
    UrlPolicy,
    describe_host,
)

__all__ = [
    "DISABLED_GONE",
    "InlineWebhookQueue",
    "SendResult",
    "WebhookDeliverer",
    "WebhookQueue",
    "WebhookRelay",
    "WebhookSender",
    "excerpt",
    "webhook_ssl_context",
]

log = logging.getLogger(__name__)

SERVICE_ACTOR: Final = "svc_webhooks"
LEASE: Final = timedelta(seconds=SSRF_POLICY.timeout_s * 4 + 10)
"""How long one try may take before the relay hands the delivery out again."""
DISABLED_GONE: Final = "The receiver answered 410 Gone: the webhook was disabled."
_EXCERPT_CHARS: Final = 1024


def now() -> datetime:
    return datetime.now(UTC)


def webhook_ssl_context(ca_file: str | None = None) -> ssl.SSLContext:
    """TLS for deliveries: the system trust store (plus ``ca_file``), TLS 1.2 or newer,
    hostname checking on (against the original host name, sent as SNI)."""
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def excerpt(raw: bytes) -> str | None:
    """The first bytes of an answer as plain text (control characters dropped): untrusted
    text, shown as text only."""
    if not raw:
        return None
    text = raw[: _EXCERPT_CHARS * 4].decode("utf-8", errors="replace")
    clean = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)
    return clean[:_EXCERPT_CHARS] or None


@dataclass(frozen=True, slots=True)
class SendResult:
    """What one try produced."""

    status: int | None = None
    body: bytes = b""
    error: str | None = None
    duration_ms: int = 0
    blocked: bool = False
    target: DeliveryTarget | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


class WebhookSender:
    """One HTTP POST to a webhook under the SSRF policy."""

    def __init__(
        self,
        policy: UrlPolicy | None = None,
        *,
        resolver: Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.policy = policy or UrlPolicy()
        self.resolver = resolver or SystemResolver(self.policy.policy.timeout_s)
        self.ssl_context = ssl_context or webhook_ssl_context()
        self.transport = transport

    async def target(self, url: str) -> DeliveryTarget:
        """Resolve and check ``url`` (``WEBHOOK_URL_BLOCKED`` / :class:`ResolutionFailed`)."""
        return await self.policy.resolve(url, self.resolver)

    async def send(self, url: str, body: bytes, headers: dict[str, str]) -> SendResult:
        started = time.monotonic()

        def ms() -> int:
            return int((time.monotonic() - started) * 1000)

        try:
            target = await self.target(url)
        except MapiError as exc:
            return SendResult(error=exc.detail[:500], duration_ms=ms(), blocked=True)
        except ResolutionFailed as exc:
            return SendResult(error=str(exc)[:500], duration_ms=ms())
        p = self.policy.policy
        send_headers = {
            **headers,
            "Host": target.host_header,
            "Content-Type": "application/json",
            "User-Agent": p.user_agent,
        }
        cap = p.max_response_bytes
        try:
            async with (
                asyncio.timeout(p.timeout_s * 2),
                httpx.AsyncClient(
                    verify=self.ssl_context,
                    timeout=httpx.Timeout(p.timeout_s),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    "POST",
                    target.connect_url,
                    content=body,
                    headers=send_headers,
                    extensions={"sni_hostname": target.host},
                ) as response,
            ):
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_raw():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= cap:
                        break
                raw = b"".join(chunks)[:cap]
                status = response.status_code
        except TimeoutError:
            return SendResult(
                error="the receiver did not answer in time", duration_ms=ms(), target=target
            )
        except httpx.HTTPError as exc:
            return SendResult(error=_http_error(exc), duration_ms=ms(), target=target)
        error = None
        if 300 <= status < 400:
            error = f"the receiver answered {status} (redirects are not followed)"
        elif not 200 <= status < 300:
            error = f"the receiver answered {status}"
        return SendResult(status=status, body=raw, error=error, duration_ms=ms(), target=target)


def _http_error(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "the receiver did not answer in time"
    if isinstance(exc, httpx.ConnectError):
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, ssl.SSLError) or "certificate" in str(exc).lower():
            return "the TLS handshake failed (certificate not valid for the host)"
        return "could not connect to the receiver"
    return f"the request failed ({type(exc).__name__})"


# ------------------------------------------------------------------------------- delivery


@dataclass(frozen=True, slots=True)
class _Claim:
    delivery_id: str
    webhook_id: str
    url: str
    body: bytes
    headers: dict[str, str]
    attempt: int


class WebhookDeliverer:
    """One try of one delivery (claim → sign → send → record)."""

    def __init__(
        self,
        ctx: ServerContext,
        *,
        sender: WebhookSender | None = None,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], datetime] = now,
    ) -> None:
        self.ctx = ctx
        settings = ctx.settings
        self.sender = sender or WebhookSender(
            UrlPolicy.from_settings(settings),
            resolver=ctx.webhook_resolver,
            ssl_context=webhook_ssl_context(
                str(settings.webhook_ca_file) if settings.webhook_ca_file else None
            ),
        )
        self.box = SecretBox(ctx.secrets.webhook_key)
        self.jitter = jitter
        self.clock = clock

    async def deliver(self, delivery_id: str) -> DeliveryStatus | None:
        """Try ``delivery_id`` once if it is due. Returns its new status, or ``None`` when
        there was nothing to do (not due, in flight elsewhere, already final or gone)."""
        claim = await self._claim(delivery_id)
        if claim is None:
            return None
        if isinstance(claim, DeliveryStatus):
            return claim
        result = await self.sender.send(claim.url, claim.body, claim.headers)
        return await self._record(claim, result)

    async def _claim(self, delivery_id: str) -> _Claim | DeliveryStatus | None:
        ts = self.clock()
        async with self.ctx.db.session() as s:
            d = (
                await s.execute(
                    select(m.WebhookDelivery)
                    .where(m.WebhookDelivery.id == delivery_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if d is None or d.status not in (DeliveryStatus.PENDING, DeliveryStatus.RETRYING):
                return None
            if d.next_attempt_at is not None and d.next_attempt_at > ts:
                return None
            if d.lease_until is not None and d.lease_until > ts:
                return None
            hook = await s.get(m.Webhook, d.webhook_id)
            event = await s.get(m.WebhookEvent, d.event_id)
            if hook is None or event is None:
                return None
            if not hook.active:
                _final(d, DeliveryStatus.FAILED, ts, error="the webhook is disabled")
                return DeliveryStatus.FAILED
            try:
                keys = [self.box.open(hook.secret_sealed, owner=hook.id)]
                if (
                    hook.previous_secret_sealed
                    and hook.previous_secret_expires_at is not None
                    and hook.previous_secret_expires_at > ts
                ):
                    keys.append(self.box.open(hook.previous_secret_sealed, owner=hook.id))
            except SealError:
                log.error(
                    "webhook %s: the signing secret does not open (server secret "
                    "rotated?); rotate the webhook secret",
                    hook.id,
                )
                _final(
                    d,
                    DeliveryStatus.FAILED,
                    ts,
                    error="the signing secret is unusable; rotate the webhook secret",
                )
                return DeliveryStatus.FAILED
            d.lease_until = ts + LEASE
            d.updated_at = ts
            body = envelope_body(event.envelope)
            headers = {
                DELIVERY_HEADERS["signature"]: signature_header(body, keys, int(ts.timestamp())),
                DELIVERY_HEADERS["event_id"]: event.id,
                DELIVERY_HEADERS["event_type"]: event.type,
                DELIVERY_HEADERS["webhook_id"]: hook.id,
                DELIVERY_HEADERS["delivery_id"]: d.id,
            }
            return _Claim(d.id, hook.id, hook.url, body, headers, d.attempt)

    async def _record(self, claim: _Claim, result: SendResult) -> DeliveryStatus | None:
        settings = self.ctx.settings
        ts = self.clock()
        async with self.ctx.db.session() as s:
            # the webhook first, like the API's update and delete (whose cascade then locks
            # the deliveries): one lock order, no deadlock
            hook = await s.get(m.Webhook, claim.webhook_id, with_for_update=True)
            d = await s.get(m.WebhookDelivery, claim.delivery_id, with_for_update=True)
            if d is None or hook is None or d.attempt != claim.attempt:
                return None  # deleted meanwhile (or another worker recorded this try)
            if d.status not in (DeliveryStatus.PENDING, DeliveryStatus.RETRYING):
                return None
            d.response_code = result.status
            d.duration_ms = result.duration_ms
            d.response_excerpt = excerpt(result.body)
            d.error = result.error[:500] if result.error else None
            d.lease_until = None
            d.updated_at = ts
            hook.last_delivery_at = ts
            audits = []
            disable_reason: str | None = None
            if result.ok:
                status = DeliveryStatus.SUCCEEDED
                d.status = status.value
                d.next_attempt_at = None
                hook.failure_count = 0
                hook.last_success_at = ts
            elif result.blocked:
                status = DeliveryStatus.BLOCKED
                _final(d, status, ts, error=d.error)
                obs_metrics.record_webhook_blocked("delivery")
                audits.append(
                    self._audit(
                        hook,
                        AuditAction.WEBHOOK_URL_BLOCKED,
                        ts,
                        outcome=AuditOutcome.DENIED,
                        delivery=d.id,
                        code=ErrorCode.WEBHOOK_URL_BLOCKED,
                        reason=d.error or "",
                    )
                )
            else:
                delay = None
                if claim.attempt < settings.webhook_max_attempts:
                    delay = next_retry_delay(claim.attempt, result.status, jitter=self.jitter())
                if delay is not None:
                    status = DeliveryStatus.RETRYING
                    d.status = status.value
                    d.attempt = claim.attempt + 1
                    d.next_attempt_at = ts + delay
                else:
                    status = DeliveryStatus.FAILED
                    _final(d, status, ts, error=d.error)
                if result.status == 410:
                    disable_reason = DISABLED_GONE
            if status in (DeliveryStatus.FAILED, DeliveryStatus.BLOCKED):
                hook.failure_count += 1
                if hook.failure_count >= settings.webhook_auto_disable_after:
                    disable_reason = disable_reason or (
                        f"{hook.failure_count} events in a row could not be delivered: the "
                        "webhook was disabled. Fix the receiver, then re-activate it."
                    )
            if disable_reason is not None and hook.active:
                hook.active = False
                hook.disabled_reason = disable_reason[:500]
                hook.updated_at = ts
                reason = "gone" if disable_reason == DISABLED_GONE else "failures"
                obs_metrics.record_webhook_disabled(reason)
                audits.append(
                    self._audit(
                        hook,
                        AuditAction.WEBHOOK_DISABLED,
                        ts,
                        outcome=AuditOutcome.SUCCESS,
                        delivery=d.id,
                        reason=reason,
                    )
                )
                log.warning(
                    "webhook %s of %s/%s disabled: %s",
                    hook.id,
                    hook.tenant_id,
                    hook.project_id,
                    disable_reason,
                )
            if audits:
                await add_audit(s, audits)
        obs_metrics.record_webhook_delivery(status.value, result.duration_ms / 1000.0)
        return status

    def _audit(
        self,
        hook: m.Webhook,
        action: AuditAction,
        ts: datetime,
        *,
        outcome: AuditOutcome,
        delivery: str,
        reason: str,
        code: ErrorCode | None = None,
    ) -> Any:
        return make_audit_event(
            action=action,
            resource_kind=ResourceKind.WEBHOOK,
            outcome=outcome,
            ts=ts,
            fingerprint_key=b"",
            tenant_id=hook.tenant_id,
            project_id=hook.project_id,
            actor_kind=PrincipalKind.SERVICE,
            actor_id=SERVICE_ACTOR,
            resource_id=hook.id,
            code=code,
            details={
                "delivery_id": delivery,
                "host": describe_host(hook.url),
                "reason": reason[:300],
                "failure_count": hook.failure_count,
            },
        )


def _final(
    d: m.WebhookDelivery, status: DeliveryStatus, ts: datetime, *, error: str | None
) -> None:
    d.status = status.value
    d.next_attempt_at = None
    d.lease_until = None
    d.error = error[:500] if error else None
    d.updated_at = ts


# ---------------------------------------------------------------------------------- relay


class WebhookQueue(Protocol):
    """Where the relay hands due deliveries (procrastinate in production)."""

    async def defer_delivery(self, delivery_id: str, attempt: int) -> None: ...


class InlineWebhookQueue:
    """Runs deliveries as tasks of this process (single-process dev mode and tests)."""

    def __init__(self, deliverer: WebhookDeliverer) -> None:
        self.deliverer = deliverer
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def defer_delivery(self, delivery_id: str, attempt: int) -> None:
        task = self._tasks.get(delivery_id)
        if task is not None and not task.done():
            return
        self._tasks[delivery_id] = asyncio.create_task(self._run(delivery_id))

    async def _run(self, delivery_id: str) -> None:
        try:
            await self.deliverer.deliver(delivery_id)
        except Exception:  # the relay hands it out again once its lease runs out
            log.exception("webhook delivery %s failed", delivery_id)

    async def drain(self) -> None:
        while pending := [t for t in self._tasks.values() if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)


class WebhookRelay:
    """The orchestrator loop: due deliveries → queue; old events pruned."""

    def __init__(self, ctx: ServerContext, queue: WebhookQueue, *, batch: int = 200) -> None:
        self.ctx = ctx
        self.queue = queue
        self.batch = batch
        self._last_prune = 0.0

    async def pump_once(self) -> int:
        """Defer every due delivery that is not in flight; returns how many."""
        ts = now()
        async with self.ctx.db.session() as s:
            rows = (
                await s.execute(
                    select(m.WebhookDelivery.id, m.WebhookDelivery.attempt)
                    .where(
                        m.WebhookDelivery.status.in_(
                            [DeliveryStatus.PENDING.value, DeliveryStatus.RETRYING.value]
                        ),
                        m.WebhookDelivery.next_attempt_at <= ts,
                        (m.WebhookDelivery.lease_until.is_(None))
                        | (m.WebhookDelivery.lease_until < ts),
                    )
                    .order_by(m.WebhookDelivery.next_attempt_at)
                    .limit(self.batch)
                )
            ).all()
        for delivery_id, attempt in rows:
            await self.queue.defer_delivery(delivery_id, attempt)
        return len(rows)

    async def prune_once(self) -> int:
        """Delete events (and their delivery logs) past the retention period, unless one of
        their deliveries is still due."""
        cutoff = now() - timedelta(days=self.ctx.settings.webhook_retention_days)
        async with self.ctx.db.session() as s:
            busy = select(m.WebhookDelivery.event_id).where(
                m.WebhookDelivery.status.in_(
                    [DeliveryStatus.PENDING.value, DeliveryStatus.RETRYING.value]
                )
            )
            result = await s.execute(
                delete(m.WebhookEvent).where(
                    m.WebhookEvent.created_at < cutoff, m.WebhookEvent.id.not_in(busy)
                )
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def run_forever(self, stop: asyncio.Event) -> None:
        interval = self.ctx.settings.webhook_poll_interval_s
        while not stop.is_set():
            try:
                await self.pump_once()
                if time.monotonic() - self._last_prune > 3600:
                    self._last_prune = time.monotonic()
                    await self.prune_once()
            except Exception:  # a database hiccup must not kill the loop
                log.exception("webhook relay pass failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
