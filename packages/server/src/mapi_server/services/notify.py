"""Run change notifications: Postgres ``NOTIFY`` + one ``LISTEN`` connection per process.

Writers call :func:`notify_run` inside their transaction (delivered on commit). Each API
process runs one :class:`NotificationHub` that LISTENs on :data:`CHANNEL` and fans the
payloads out to in-memory subscribers (SSE streams, ``Prefer: wait`` waiters). A
notification is only a wake-up: readers always re-read the database, so a lost notification
costs latency, never correctness (subscribers also poll on a timeout).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CHANNEL",
    "NoticeKind",
    "NotificationHub",
    "RunNotice",
    "Subscription",
    "notify_run",
]

log = logging.getLogger(__name__)

NoticeKind = Literal["run", "event", "progress", "log", "tracking"]

CHANNEL = "mapi_runs"


@dataclass(frozen=True, slots=True)
class RunNotice:
    run_id: str
    kind: NoticeKind
    event_id: int | None = None


async def notify_run(
    session: AsyncSession,
    run_id: str,
    kind: NoticeKind,
    event_id: int | None = None,
) -> None:
    """Queue a notification for ``run_id`` (sent when the surrounding transaction commits)."""
    payload = json.dumps({"run_id": run_id, "kind": kind, "event_id": event_id})
    await session.execute(text("SELECT pg_notify(:c, :p)"), {"c": CHANNEL, "p": payload})


class Subscription:
    """A per-run mailbox. ``wait()`` returns the next notice or ``None`` on timeout."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._queue: asyncio.Queue[RunNotice] = asyncio.Queue(maxsize=1024)

    def _push(self, notice: RunNotice) -> None:
        with contextlib.suppress(asyncio.QueueFull):  # slow consumer: it re-reads the DB anyway
            self._queue.put_nowait(notice)

    async def wait(self, timeout: float) -> RunNotice | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    def drain(self) -> list[RunNotice]:
        out: list[RunNotice] = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out


class NotificationHub:
    """One LISTEN connection per process with in-memory fan-out to subscribers."""

    def __init__(self, conninfo: str, *, reconnect_s: float = 1.0) -> None:
        self._conninfo = conninfo
        self._reconnect_s = reconnect_s
        self._subs: dict[str, set[Subscription]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self.connected = False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="mapi-notify-listener")
            # do not block startup forever if Postgres is down: subscribers poll anyway
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._ready.wait(), 10)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True)
                async with conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self.connected = True
                    self._ready.set()
                    async for note in conn.notifies():
                        self._dispatch(note.payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # reconnect after any connection failure
                log.warning("notification listener disconnected; reconnecting", exc_info=True)
            finally:
                self.connected = False
            await asyncio.sleep(self._reconnect_s)

    def _dispatch(self, raw: str) -> None:
        try:
            data = json.loads(raw)
            notice = RunNotice(str(data["run_id"]), data["kind"], data.get("event_id"))
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring malformed notification payload")
            return
        self.publish(notice)

    def publish(self, notice: RunNotice) -> None:
        """Deliver ``notice`` to local subscribers (also used directly in-process)."""
        for sub in list(self._subs.get(notice.run_id, ())):
            sub._push(notice)

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[Subscription]:
        sub = Subscription(run_id)
        self._subs[run_id].add(sub)
        try:
            yield sub
        finally:
            subs = self._subs.get(run_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self._subs[run_id]

    def subscriber_count(self, run_id: str | None = None) -> int:
        if run_id is not None:
            return len(self._subs.get(run_id, ()))
        return sum(len(s) for s in self._subs.values())
