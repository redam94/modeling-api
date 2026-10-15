"""Lazy iterators over cursor-paginated list endpoints (``Page[T]``).

Nothing is fetched until iteration starts. Iterating yields items across pages; ``pages()``
yields whole pages; ``first_page()`` and ``take(n)`` are conveniences.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

from mapi_spec.api import Page

from mapi_client.errors import UnexpectedResponse

__all__ = ["AsyncPaginator", "Paginator"]

MAX_PAGES = 100_000
"""Guard against a server that keeps returning the same cursor."""


def _next_cursor[T](page: Page[T], seen: set[str]) -> str | None:
    cursor = page.next_cursor
    if cursor is None:
        return None
    if cursor in seen:
        raise UnexpectedResponse(f"the server returned cursor {cursor!r} twice (pagination loop)")
    seen.add(cursor)
    if len(seen) > MAX_PAGES:
        raise UnexpectedResponse("too many pages")
    if not page.items:
        # an empty page with a cursor would loop forever on a misbehaving server
        raise UnexpectedResponse("the server returned an empty page with a next cursor")
    return cursor


class Paginator[T]:
    """Synchronous lazy iterator; ``fetch(cursor)`` returns one page."""

    def __init__(self, fetch: Callable[[str | None], Page[T]]) -> None:
        self._fetch = fetch

    def pages(self) -> Iterator[Page[T]]:
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self._fetch(cursor)
            yield page
            cursor = _next_cursor(page, seen)
            if cursor is None:
                return

    def __iter__(self) -> Iterator[T]:
        for page in self.pages():
            yield from page.items

    def first_page(self) -> Page[T]:
        return self._fetch(None)

    def take(self, n: int) -> list[T]:
        """At most ``n`` items (fetches only the pages needed)."""
        if n < 0:
            raise ValueError("n must be >= 0")
        out: list[T] = []
        if n == 0:
            return out
        for item in self:
            out.append(item)
            if len(out) >= n:
                break
        return out


class AsyncPaginator[T]:
    """Asynchronous lazy iterator (``async for item in paginator``)."""

    def __init__(self, fetch: Callable[[str | None], Awaitable[Page[T]]]) -> None:
        self._fetch = fetch

    async def pages(self) -> AsyncIterator[Page[T]]:
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = await self._fetch(cursor)
            yield page
            cursor = _next_cursor(page, seen)
            if cursor is None:
                return

    async def __aiter__(self) -> AsyncIterator[T]:
        async for page in self.pages():
            for item in page.items:
                yield item

    async def first_page(self) -> Page[T]:
        return await self._fetch(None)

    async def take(self, n: int) -> list[T]:
        if n < 0:
            raise ValueError("n must be >= 0")
        out: list[T] = []
        if n == 0:
            return out
        async for item in self:
            out.append(item)
            if len(out) >= n:
                break
        return out

    async def collect(self) -> list[T]:
        return [item async for item in self]
