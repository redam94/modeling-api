"""Rate limiting primitives shared by the dispatcher and the executor drivers.

* :class:`TokenBucket` — ``rate`` tokens per second with at most ``burst`` saved up, plus a
  shared *cool-down* (:meth:`TokenBucket.penalize`) that blocks every caller after a backend
  answered 429, so concurrent submitters back off together instead of hammering the API;
* :class:`WindowCounter` — how many times something happened in the last ``window`` seconds
  (submits per minute, 429s per hour) for health reporting and alarms.

Both take an injectable monotonic clock and sleep function so tests are deterministic.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

__all__ = ["Clock", "Sleep", "TokenBucket", "WindowCounter"]

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    """Classic token bucket: ``rate`` tokens per second, at most ``burst`` saved up."""

    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self.rate = rate_per_s
        self.burst = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(burst)
        self._stamp = clock()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def per_minute(self) -> float:
        return self.rate * 60.0

    def _refill(self) -> None:
        t = self._clock()
        self._tokens = min(self.burst, self._tokens + (t - self._stamp) * self.rate)
        self._stamp = t

    def cooldown_left(self) -> float:
        """Seconds until a :meth:`penalize` cool-down ends (0 when none is active)."""
        return max(0.0, self._blocked_until - self._clock())

    def penalize(self, seconds: float) -> None:
        """Block every caller for ``seconds`` (extends, never shortens, an active cool-down).

        Saved-up tokens are dropped too: after a 429 the backend's own budget is spent, and
        a burst right after the cool-down would only earn another 429.
        """
        if seconds <= 0:
            return
        self._refill()
        self._tokens = min(self._tokens, 0.0)
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    def try_acquire(self) -> bool:
        if self.cooldown_left() > 0:
            return False
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def acquire(self) -> None:
        """Wait for a token (and for any cool-down to end). Callers are served in order."""
        async with self._lock:
            while not self.try_acquire():
                wait = self.cooldown_left()
                if wait <= 0:
                    wait = (1 - self._tokens) / self.rate
                await self._sleep(max(wait, 1e-4))


class WindowCounter:
    """Counts events in a sliding window of ``window_s`` seconds (bounded memory)."""

    def __init__(
        self, window_s: float, *, clock: Clock = time.monotonic, max_events: int = 100_000
    ) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self.window_s = window_s
        self._clock = clock
        self._events: deque[float] = deque(maxlen=max_events)
        self.total = 0

    def record(self) -> None:
        self._events.append(self._clock())
        self.total += 1

    def count(self) -> int:
        horizon = self._clock() - self.window_s
        while self._events and self._events[0] < horizon:
            self._events.popleft()
        return len(self._events)
