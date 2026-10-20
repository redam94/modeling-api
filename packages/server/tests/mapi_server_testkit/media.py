"""Media builders (v0.14): tiny PNG images and JSON tables built in pure Python, and a
``media`` event payload for bytes uploaded under a run's media prefix."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import zlib
from typing import Any

from mapi_server.context import ServerContext
from mapi_spec.media import media_name
from mapi_spec.run import MediaPayload
from mapi_spec.storage import crc32c, crc32c_to_base64, run_media_prefix


def png_bytes(width: int = 4, height: int = 3, *, shade: int = 0) -> bytes:
    """An 8-bit grayscale PNG of ``width`` x ``height`` pixels (``shade`` varies the bytes)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00" + bytes((x * 16 + y + shade) % 256 for x in range(width)) for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def table_bytes(rows: int = 2) -> bytes:
    return json.dumps(
        {"columns": ["class", "n"], "rows": [[f"c{i}", i] for i in range(rows)]}
    ).encode()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def media_payload(
    data: bytes,
    *,
    key: str = "samples",
    content_type: str = "image/png",
    step: int | None = 0,
    labels: dict[str, str] | None = None,
    width: int | None = 4,
    height: int | None = 3,
    sha256: str | None = None,
    size: int | None = None,
    caption: str | None = None,
) -> MediaPayload:
    sha = sha256 or hashlib.sha256(data).hexdigest()
    kind = "table" if content_type == "application/json" else "image"
    fields: dict[str, Any] = {
        "key": key,
        "kind": kind,
        "content_type": content_type,
        "name": media_name(sha, content_type),
        "sha256": sha,
        "size": size if size is not None else len(data),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "step": step,
        "labels": labels or {},
        "caption": caption,
    }
    if kind == "image":
        fields.update(width=width, height=height)
    return MediaPayload.model_validate(fields)


async def upload_media(
    ctx: ServerContext,
    run_id: str,
    attempt: int,
    data: bytes,
    name: str,
    *,
    tenant_id: str = "default",
) -> str:
    """Put ``data`` where the supervisor uploads it (the attempt's media prefix)."""
    key = run_media_prefix(tenant_id, run_id, attempt) + name
    await ctx.store.put(ctx.buckets.runs, key, data)
    return key
