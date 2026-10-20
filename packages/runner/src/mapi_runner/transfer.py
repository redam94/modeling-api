"""Signed-URL transfers: ranged downloads, streaming PUT and V4 POST-policy uploads.

Only the URLs in a :class:`~mapi_spec.run.RunSpec` are ever contacted. Hashes are computed
over the exact bytes sent or received (CRC32C and MD5 in the GCS base64 forms), so an
output manifest always describes what reached the store, even if a file changes on disk
while it streams.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

import httpx
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.storage import (
    PostPolicy,
    SignedGet,
    SignedPut,
    crc32c,
    crc32c_to_base64,
)

__all__ = [
    "CHUNK_BYTES",
    "Digest",
    "TransferError",
    "Transfers",
    "hashes_of",
    "new_client",
]

CHUNK_BYTES: Final = 8 * 1024 * 1024
"""Range-read and upload chunk size."""
_RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class TransferError(MapiError):
    """An upload or download failed after retries (``INFRA_ERROR``)."""

    default_code = ErrorCode.INFRA_ERROR


@dataclass(frozen=True, slots=True)
class Digest:
    """Size and GCS-style hashes of transferred bytes."""

    size: int
    crc32c: str
    md5: str


class _Hasher:
    def __init__(self) -> None:
        self.crc = 0
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.size = 0

    def update(self, data: bytes) -> None:
        self.crc = crc32c(data, self.crc)
        self.md5.update(data)
        self.size += len(data)

    def digest(self) -> Digest:
        import base64

        return Digest(
            size=self.size,
            crc32c=crc32c_to_base64(self.crc),
            md5=base64.b64encode(self.md5.digest()).decode("ascii"),
        )


def new_client(
    *, timeout: float = 120.0, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """An HTTP client that ignores proxy environment variables and never follows redirects."""
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=15.0),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def _redact(url: str) -> str:
    return url.split("?", 1)[0]


class Transfers:
    """Signed-URL operations with bounded retries (exponential back-off)."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 4,
        backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self._sleep = sleep

    # ------------------------------------------------------------------ retries
    def _retry(self, what: str, url: str, op: Callable[[], httpx.Response]) -> httpx.Response:
        last: str = ""
        for i in range(self.attempts):
            if i:
                self._sleep(self.backoff * (2 ** (i - 1)))
            try:
                resp = op()
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code in _RETRY_STATUS:
                last = f"HTTP {resp.status_code}"
                continue
            return resp
        raise TransferError(
            f"{what} failed after {self.attempts} attempts",
            untrusted_text=f"{_redact(url)}: {last}"[:2000],
        )

    @staticmethod
    def _check(resp: httpx.Response, what: str, ok: tuple[int, ...]) -> None:
        if resp.status_code not in ok:
            body = resp.text[:500] if resp.content else ""
            raise TransferError(
                f"{what} was refused with HTTP {resp.status_code}",
                untrusted_text=f"{_redact(str(resp.request.url))}: {body}"[:2000],
            )

    # ---------------------------------------------------------------- download
    def download(
        self,
        get: SignedGet,
        dest: Path,
        *,
        budget: int | None = None,
        chunk: int = CHUNK_BYTES,
    ) -> Digest:
        """Range-read ``get`` into ``dest`` and verify its size and CRC32C (and MD5 if known).

        ``budget`` is the scratch space left; an object larger than it is refused before any
        byte is read (``RESOURCE_EXHAUSTED``). A mismatch is ``INTEGRITY_FAILURE``.
        """
        if budget is not None and get.size > budget:
            raise MapiError(
                f"an input of {get.size} bytes does not fit the {budget}-byte scratch budget",
                code=ErrorCode.RESOURCE_EXHAUSTED,
            )
        hasher = _Hasher()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            if get.size == 0:
                resp = self._retry("download", get.url, lambda: self.client.get(get.url))
                self._check(resp, "download", (200,))
                fh.write(resp.content)
                hasher.update(resp.content)
            offset = 0
            while offset < get.size:
                rng = f"bytes={offset}-{min(offset + chunk, get.size) - 1}"
                resp = self._retry(
                    "download",
                    get.url,
                    lambda r=rng: self.client.get(get.url, headers={"Range": r}),  # type: ignore[misc]
                )
                self._check(resp, "download", (200, 206))
                data = resp.content
                if resp.status_code == 200 and offset != 0:
                    raise TransferError("the store ignored a range request mid-download")
                if resp.status_code == 206 and len(data) != min(chunk, get.size - offset):
                    raise MapiError(
                        "a range read returned the wrong number of bytes",
                        code=ErrorCode.INTEGRITY_FAILURE,
                    )
                if hasher.size + len(data) > get.size:
                    raise MapiError(
                        f"input is larger than its declared {get.size} bytes",
                        code=ErrorCode.INTEGRITY_FAILURE,
                    )
                fh.write(data)
                hasher.update(data)
                offset += len(data)
                if resp.status_code == 200:
                    break  # the server ignored the range and sent the whole object
        got = hasher.digest()
        if got.size != get.size:
            raise MapiError(
                f"input size {got.size} does not match the declared {get.size} bytes",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        if got.crc32c != get.crc32c:
            raise MapiError(
                "input CRC32C does not match its declared value", code=ErrorCode.INTEGRITY_FAILURE
            )
        if get.md5_hash is not None and got.md5 != get.md5_hash:
            raise MapiError(
                "input MD5 does not match its declared value", code=ErrorCode.INTEGRITY_FAILURE
            )
        return got

    def read_range(self, get: SignedGet, start: int, end: int) -> bytes:
        """Bytes ``[start, end)`` of ``get`` with one HTTP ``Range`` read (v0.15: lazy reads of
        image packs). Only a ``206`` answer of exactly ``end - start`` bytes is accepted; the
        caller verifies the content (every image read checks its own sha256)."""
        if not 0 <= start < end <= get.size:
            raise ValueError(f"range [{start}, {end}) is outside the {get.size}-byte object")
        rng = f"bytes={start}-{end - 1}"
        resp = self._retry(
            "range read", get.url, lambda: self.client.get(get.url, headers={"Range": rng})
        )
        self._check(resp, "range read", (206,))
        data = resp.content
        if len(data) != end - start:
            raise MapiError(
                "a range read returned the wrong number of bytes",
                code=ErrorCode.INTEGRITY_FAILURE,
            )
        return data

    def fetch(self, get: SignedGet, *, max_bytes: int) -> bytes:
        """Download a small object into memory (verified like :meth:`download`)."""
        if get.size > max_bytes:
            raise MapiError(
                f"object of {get.size} bytes exceeds the {max_bytes}-byte limit",
                code=ErrorCode.PAYLOAD_TOO_LARGE,
            )
        resp = self._retry("download", get.url, lambda: self.client.get(get.url))
        self._check(resp, "download", (200,))
        data = resp.content
        h = _Hasher()
        h.update(data)
        d = h.digest()
        if d.size != get.size or d.crc32c != get.crc32c:
            raise MapiError(
                "object does not match its declared hashes", code=ErrorCode.INTEGRITY_FAILURE
            )
        return data

    # ------------------------------------------------------------------ upload
    def put_bytes(self, put: SignedPut, data: bytes, *, content_type: str | None = None) -> Digest:
        """PUT ``data`` to a signed URL, sending its signed headers verbatim."""
        if put.max_bytes is not None and len(data) > put.max_bytes:
            raise MapiError(
                f"{put.name!r} is {len(data)} bytes; the signed PUT allows {put.max_bytes}",
                code=ErrorCode.SANDBOX_VIOLATION,
            )
        headers = dict(put.headers)
        if content_type and not any(k.lower() == "content-type" for k in headers):
            headers["content-type"] = content_type
        h = _Hasher()
        h.update(data)
        resp = self._retry(
            "upload", put.url, lambda: self.client.put(put.url, content=data, headers=headers)
        )
        self._check(resp, f"upload of {put.name!r}", (200, 201, 204))
        return h.digest()

    def post_file(
        self,
        policy: PostPolicy,
        key: str,
        source: BinaryIO | int,
        size: int,
        *,
        content_type: str = "application/octet-stream",
        chunk: int = CHUNK_BYTES,
    ) -> Digest:
        """Stream ``size`` bytes of ``source`` (an open file or fd) to ``key`` via a POST policy.

        The multipart body is ``fields``, then ``key``, then the file part last. The source
        is read with ``pread`` from offset 0 on every attempt, so retries resend the same
        bytes; the returned digest covers exactly the bytes sent.
        """
        if not policy.allows(key, size):
            raise MapiError(
                f"upload of {size} bytes to {key!r} is outside the POST policy",
                code=ErrorCode.SANDBOX_VIOLATION,
            )
        fd = source if isinstance(source, int) else source.fileno()
        boundary, head, tail = _multipart(policy, key, content_type)
        total = len(head) + size + len(tail)
        state: dict[str, Digest] = {}

        def body() -> Iterator[bytes]:
            h = _Hasher()
            yield head
            offset = 0
            while offset < size:
                data = os.pread(fd, min(chunk, size - offset), offset)
                if not data:
                    raise MapiError(
                        f"{key!r} shrank while it was uploading", code=ErrorCode.SANDBOX_VIOLATION
                    )
                h.update(data)
                offset += len(data)
                yield data
            state["digest"] = h.digest()
            yield tail

        headers = {
            "content-type": f"multipart/form-data; boundary={boundary}",
            "content-length": str(total),
        }
        resp = self._retry(
            "upload",
            policy.url,
            lambda: self.client.post(policy.url, content=body(), headers=headers),
        )
        self._check(resp, f"upload of {key!r}", (200, 201, 204))
        digest = state.get("digest")
        if digest is None or digest.size != size:
            raise TransferError(f"upload of {key!r} did not send the whole file")
        return digest

    def post_bytes(
        self,
        policy: PostPolicy,
        key: str,
        data: bytes,
        *,
        content_type: str,
    ) -> Digest:
        """Upload in-memory ``data`` to ``key`` via a POST policy (the body of :meth:`post_file`).

        The digest covers exactly ``data``: the caller hashes and checks the same bytes.
        """
        if not policy.allows(key, len(data)):
            raise MapiError(
                f"upload of {len(data)} bytes to {key!r} is outside the POST policy",
                code=ErrorCode.SANDBOX_VIOLATION,
            )
        boundary, head, tail = _multipart(policy, key, content_type)
        body = head + data + tail
        headers = {
            "content-type": f"multipart/form-data; boundary={boundary}",
            "content-length": str(len(body)),
        }
        resp = self._retry(
            "upload",
            policy.url,
            lambda: self.client.post(policy.url, content=body, headers=headers),
        )
        self._check(resp, f"upload of {key!r}", (200, 201, 204))
        return hashes_of(data)


def _multipart(policy: PostPolicy, key: str, content_type: str) -> tuple[str, bytes, bytes]:
    """``(boundary, head, tail)`` of a POST-policy upload: ``fields``, then ``key``, then the
    file part (its bytes go between ``head`` and ``tail``)."""
    boundary = "mapi" + secrets.token_hex(16)
    head = bytearray()
    for name, value in [*policy.fields.items(), ("key", key)]:
        head += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_q(name)}"\r\n\r\n{value}\r\n'
        ).encode()
    filename = _q(key.rsplit("/", 1)[-1])
    head += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"'
        f"\r\nContent-Type: {content_type}\r\n\r\n"
    ).encode()
    return boundary, bytes(head), f"\r\n--{boundary}--\r\n".encode()


def _q(value: str) -> str:
    """Quote a multipart header parameter (no CR/LF/quotes can break out)."""
    return value.replace("\\", "\\\\").replace('"', "%22").replace("\r", "").replace("\n", "")


def hashes_of(data: bytes) -> Digest:
    """Digest of an in-memory payload."""
    h = _Hasher()
    h.update(data)
    return h.digest()
