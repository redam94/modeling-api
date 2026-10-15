"""A small, bounded, streaming MIME multipart reader (``multipart/form-data`` and
``multipart/related``).

Parts are consumed in order and their bodies are streamed, so a large ``file`` part never has
to fit in memory and size limits are enforced while bytes arrive. Header blocks, preambles
and small fields are capped to keep a hostile body from exhausting memory.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from email.message import Message
from typing import Final

from mapi_gcs_shim._errors import ShimError

__all__ = ["MultipartReader", "Part", "parse_boundary"]

MAX_HEADER_BYTES: Final = 16 * 1024
MAX_PREAMBLE_BYTES: Final = 64 * 1024
_BOUNDARY_RE: Final = re.compile(r"^[0-9A-Za-z'()+_,\-./:=? ]{0,69}[0-9A-Za-z'()+_,\-./:=?]$")


def _bad(message: str) -> ShimError:
    return ShimError.bad_request(
        f"malformed multipart body: {message}", xml_code="MalformedPOSTRequest"
    )


def parse_boundary(content_type: str | None, *, subtype: str) -> bytes:
    """Return the boundary of a ``multipart/{subtype}`` content type (400 otherwise)."""
    if not content_type:
        raise ShimError.bad_request(f"expected Content-Type multipart/{subtype}")
    msg = Message()
    msg["content-type"] = content_type
    if msg.get_content_type() != f"multipart/{subtype}":
        raise ShimError.bad_request(f"expected Content-Type multipart/{subtype}")
    boundary = msg.get_param("boundary")
    if not isinstance(boundary, str) or not _BOUNDARY_RE.match(boundary):
        raise ShimError.bad_request("missing or invalid multipart boundary")
    return boundary.encode("ascii")


@dataclass(slots=True)
class Part:
    """Headers of one part (names lowercased) plus the parsed disposition parameters."""

    headers: dict[str, str] = field(default_factory=dict)

    def _param(self, header: str, param: str) -> str | None:
        raw = self.headers.get(header)
        if raw is None:
            return None
        msg = Message()
        msg[header] = raw
        value = msg.get_param(param, header=header)
        if value is None or isinstance(value, tuple):
            return None
        return value

    @property
    def name(self) -> str | None:
        """``Content-Disposition: form-data; name="..."``."""
        return self._param("content-disposition", "name")

    @property
    def filename(self) -> str | None:
        return self._param("content-disposition", "filename")

    @property
    def content_type(self) -> str | None:
        return self.headers.get("content-type")


class MultipartReader:
    """Iterate the parts of a multipart body arriving as an async byte stream.

    Usage::

        reader = MultipartReader(request.stream(), boundary)
        while (part := await reader.next_part()) is not None:
            async for chunk in reader.iter_data(): ...   # or: await reader.read_all(limit)
    """

    def __init__(self, stream: AsyncIterator[bytes], boundary: bytes) -> None:
        self._stream = stream
        # A leading CRLF lets the first delimiter be matched like every other one.
        self._buf = bytearray(b"\r\n")
        self._delim = b"\r\n--" + boundary
        self._eof = False
        self._state = "preamble"  # preamble | after_delim | body | done
        self._consumed_preamble = 0

    async def _fill(self) -> bool:
        if self._eof:
            return False
        try:
            chunk = await anext(self._stream)
        except StopAsyncIteration:
            self._eof = True
            return False
        self._buf += chunk
        return True

    async def _skip_preamble(self) -> None:
        while True:
            idx = self._buf.find(self._delim)
            if idx >= 0:
                self._consumed_preamble += idx
                del self._buf[: idx + len(self._delim)]
                self._state = "after_delim"
                return
            keep = len(self._delim) - 1
            if len(self._buf) > keep:
                self._consumed_preamble += len(self._buf) - keep
                del self._buf[: len(self._buf) - keep]
            if self._consumed_preamble > MAX_PREAMBLE_BYTES:
                raise _bad("no boundary found")
            if not await self._fill():
                raise _bad("no boundary found")

    async def _need(self, n: int) -> None:
        while len(self._buf) < n:
            if not await self._fill():
                raise _bad("unexpected end of body")

    async def next_part(self) -> Part | None:
        """Advance to the next part (draining the rest of the current one); ``None`` at the end."""
        if self._state == "done":
            return None
        if self._state == "body":
            async for _ in self.iter_data():
                pass
        if self._state == "preamble":
            await self._skip_preamble()
        # after a delimiter: "--" closes the body, otherwise optional whitespace then CRLF
        await self._need(2)
        if self._buf[:2] == b"--":
            self._state = "done"
            return None
        while self._buf[0] in (0x20, 0x09):  # optional transport padding
            del self._buf[:1]
            await self._need(2)
        if self._buf[:2] != b"\r\n":
            raise _bad("boundary not followed by CRLF")
        del self._buf[:2]
        headers = await self._read_headers()
        self._state = "body"
        return Part(headers=headers)

    async def _read_headers(self) -> dict[str, str]:
        await self._need(2)
        if self._buf[:2] == b"\r\n":  # no headers at all
            del self._buf[:2]
            return {}
        while True:
            idx = self._buf.find(b"\r\n\r\n")
            if idx >= 0:
                break
            if len(self._buf) > MAX_HEADER_BYTES:
                raise _bad("part headers too large")
            if not await self._fill():
                raise _bad("unexpected end of part headers")
        if idx > MAX_HEADER_BYTES:
            raise _bad("part headers too large")
        block = bytes(self._buf[:idx])
        del self._buf[: idx + 4]
        headers: dict[str, str] = {}
        try:
            text = block.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _bad("part headers are not UTF-8") from exc
        for line in text.split("\r\n"):
            if ":" not in line:
                raise _bad("malformed part header line")
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if not key:
                raise _bad("empty part header name")
            headers[key] = v.strip()
        return headers

    async def iter_data(self) -> AsyncIterator[bytes]:
        """Stream the current part's body; afterwards the reader sits after its delimiter."""
        if self._state != "body":
            return
        dl = len(self._delim)
        while True:
            idx = self._buf.find(self._delim)
            if idx >= 0:
                if idx:
                    yield bytes(self._buf[:idx])
                del self._buf[: idx + dl]
                self._state = "after_delim"
                return
            safe = len(self._buf) - (dl - 1)
            if safe > 0:
                chunk = bytes(self._buf[:safe])
                del self._buf[:safe]
                yield chunk
            if not await self._fill():
                raise _bad("unexpected end of part body")

    async def read_all(self, limit: int) -> bytes:
        """Read the current part's body into memory (400 if it exceeds ``limit`` bytes)."""
        out = bytearray()
        async for chunk in self.iter_data():
            out += chunk
            if len(out) > limit:
                raise _bad(f"a form field exceeds {limit} bytes")
        return bytes(out)
