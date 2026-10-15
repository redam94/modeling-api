"""Using signed URLs: download a :class:`SignedGet`, upload to a :class:`SignedPut` or through
a :class:`PostPolicy`.

These helpers speak plain GCS XML-API semantics, so they work against the shim and against
real Cloud Storage alike; they hold no credentials of their own.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

import httpx
from mapi_spec.errors import ErrorCode
from mapi_spec.storage import (
    PostPolicy,
    SignedGet,
    SignedPut,
    StorageError,
    crc32c,
    crc32c_to_base64,
)

from mapi_gcs_shim.client import raise_for_storage_error

__all__ = ["UploadResult", "download_signed", "upload_post_policy", "upload_signed_put"]


@dataclass(frozen=True, slots=True)
class UploadResult:
    """What the storage service reported for a completed upload."""

    status: int
    generation: int | None
    crc32c: str | None
    md5_hash: str | None


def _result(response: httpx.Response) -> UploadResult:
    hashes: dict[str, str] = {}
    for item in response.headers.get("x-goog-hash", "").split(","):
        k, sep, v = item.strip().partition("=")
        if sep:
            hashes[k] = v
    gen = response.headers.get("x-goog-generation")
    return UploadResult(
        status=response.status_code,
        generation=int(gen) if gen else None,
        crc32c=hashes.get("crc32c"),
        md5_hash=hashes.get("md5"),
    )


async def download_signed(
    client: httpx.AsyncClient,
    signed: SignedGet,
    *,
    byte_range: tuple[int, int | None] | None = None,
    verify: bool = True,
) -> bytes:
    """GET a signed URL (optionally one byte range).

    A full download is checked against ``signed.size``/``signed.crc32c`` when ``verify`` is
    set (a corruption check; integrity is the control plane's job).
    """
    headers = {}
    if byte_range is not None:
        start, end = byte_range
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    response = await client.get(signed.url, headers=headers)
    if response.status_code >= 400:
        raise_for_storage_error(response)
    data = response.content
    if (
        verify
        and byte_range is None
        and (len(data) != signed.size or crc32c_to_base64(crc32c(data)) != signed.crc32c)
    ):
        raise StorageError(
            "downloaded bytes do not match the signed size/CRC32C",
            code=ErrorCode.INTEGRITY_FAILURE,
        )
    return data


async def upload_signed_put(
    client: httpx.AsyncClient,
    signed: SignedPut,
    data: bytes,
    *,
    send_md5: bool = True,
) -> UploadResult:
    """PUT ``data`` to a signed URL with its signed headers (plus ``Content-MD5``)."""
    headers = dict(signed.headers)
    if send_md5:
        headers["Content-MD5"] = base64.b64encode(
            hashlib.md5(data, usedforsecurity=False).digest()
        ).decode()
    response = await client.put(signed.url, content=data, headers=headers)
    if response.status_code >= 400:
        raise_for_storage_error(response, name=signed.name)
    return _result(response)


async def upload_post_policy(
    client: httpx.AsyncClient,
    policy: PostPolicy,
    key: str,
    data: bytes,
    *,
    content_type: str | None = None,
) -> UploadResult:
    """Multipart form upload of ``data`` as ``key`` (which must lie under ``policy.prefix``)."""
    form: dict[str, str] = {"key": key, **policy.fields}
    if content_type is not None:
        form["content-type"] = content_type
    response = await client.post(
        policy.url,
        data=form,
        files={"file": (key.rsplit("/", 1)[-1] or "file", data, content_type)},
    )
    if response.status_code >= 400:
        raise_for_storage_error(response, name=key)
    return _result(response)
