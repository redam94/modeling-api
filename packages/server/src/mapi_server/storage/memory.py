"""An in-process :class:`~mapi_spec.storage.ObjectStore` with GCS semantics.

Used by the test-suite and by embedders that run the control plane without the GCS shim.
It implements generations, server-computed CRC32C/MD5, preconditions, server-side copy,
listing with delimiters and pagination, and HMAC-signed URLs. Signed URLs point at
``base_url``; they only resolve if something serves this store over HTTP (the tests mount a
small ASGI app on it). For local development use the GCS shim (``MAPI_OBJECT_STORE=shim``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from mapi_spec.errors import ErrorCode
from mapi_spec.storage import (
    BUCKET_RE,
    AccessDenied,
    NotFound,
    ObjectMeta,
    ObjectPage,
    PostPolicy,
    PreconditionFailed,
    SignedGet,
    SignedPut,
    StorageError,
    check_object_name,
    check_ttl,
    crc32c,
    crc32c_to_base64,
    md5_base64,
)

__all__ = ["MemoryObjectStore", "PolicyDoc"]


@dataclass(frozen=True, slots=True)
class PolicyDoc:
    """Decoded POST policy (what the HTTP front end enforces)."""

    bucket: str
    prefix: str
    min_bytes: int
    max_bytes: int
    expires: int


@dataclass(slots=True)
class _Obj:
    meta: ObjectMeta
    data: bytes


class MemoryObjectStore:
    """Thread-unsafe, asyncio-safe in-memory object store (one event loop)."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:4443",
        buckets: Iterable[str] = (),
        signing_key: bytes | None = None,
        retention_locked: Iterable[str] = (),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._key = signing_key or secrets.token_bytes(32)
        self._buckets: set[str] = set()
        for name in buckets:
            self.create_bucket(name)
        self._live: dict[tuple[str, str], int] = {}
        self._objs: dict[tuple[str, str, int], _Obj] = {}
        self._gen = 0
        self._locked = set(retention_locked)
        self._lock = asyncio.Lock()
        self.calls: list[tuple[str, str, str]] = []  # (op, bucket, name) for assertions

    # ------------------------------------------------------------------ helpers
    def create_bucket(self, name: str) -> None:
        if not re.match(BUCKET_RE, name):
            raise ValueError(f"invalid bucket name {name!r}")
        self._buckets.add(name)

    def _bucket(self, bucket: str) -> None:
        if bucket not in self._buckets:
            raise NotFound(f"bucket {bucket!r} does not exist", bucket=bucket)

    def _next_generation(self) -> int:
        self._gen = max(self._gen + 1, time.time_ns() // 1000)
        return self._gen

    def _get_obj(self, bucket: str, name: str, generation: int | None) -> _Obj:
        self._bucket(bucket)
        gen = generation if generation is not None else self._live.get((bucket, name))
        obj = None if gen is None else self._objs.get((bucket, name, gen))
        if obj is None or (generation is None and (bucket, name) not in self._live):
            raise NotFound(f"object {name!r} not found", bucket=bucket, name=name)
        return obj

    def _precondition(self, bucket: str, name: str, if_generation_match: int | None) -> None:
        if if_generation_match is None:
            return
        live = self._live.get((bucket, name), 0)
        if live != if_generation_match:
            raise PreconditionFailed(
                f"generation precondition failed for {name!r}", bucket=bucket, name=name
            )

    def _store(
        self,
        bucket: str,
        name: str,
        data: bytes,
        content_type: str | None,
        metadata: Mapping[str, str] | None,
    ) -> ObjectMeta:
        gen = self._next_generation()
        meta = ObjectMeta(
            bucket=bucket,
            name=name,
            generation=gen,
            size=len(data),
            crc32c=crc32c_to_base64(crc32c(data)),
            md5_hash=md5_base64(data),
            content_type=content_type,
            metadata=dict(metadata or {}),
            time_created=datetime.now(UTC),
        )
        self._objs[(bucket, name, gen)] = _Obj(meta, bytes(data))
        self._live[(bucket, name)] = gen
        return meta

    def _sign(self, *parts: object) -> str:
        msg = "\n".join(str(p) for p in parts).encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def _check_sig(self, sig: str, *parts: object) -> bool:
        return hmac.compare_digest(self._sign(*parts), sig)

    # ------------------------------------------------------------------ protocol
    async def put(
        self,
        bucket: str,
        name: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
        if_generation_match: int | None = None,
    ) -> ObjectMeta:
        check_object_name(name)
        async with self._lock:
            self._bucket(bucket)
            self._precondition(bucket, name, if_generation_match)
            self.calls.append(("put", bucket, name))
            return self._store(bucket, name, data, content_type, metadata)

    async def get(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        byte_range: tuple[int, int | None] | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        obj = self._get_obj(bucket, name, generation)
        self.calls.append(("get", bucket, name))
        data = obj.data
        if byte_range is not None:
            start, end = byte_range
            data = data[start : (None if end is None else end + 1)]
        if max_bytes is not None and len(data) > max_bytes:
            raise StorageError(
                f"object {name!r} is larger than {max_bytes} bytes",
                code=ErrorCode.PAYLOAD_TOO_LARGE,
                bucket=bucket,
                name=name,
            )
        return data

    async def stat(self, bucket: str, name: str, *, generation: int | None = None) -> ObjectMeta:
        self.calls.append(("stat", bucket, name))
        return self._get_obj(bucket, name, generation).meta

    async def list(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        page_token: str | None = None,
        max_results: int = 1000,
    ) -> ObjectPage:
        self._bucket(bucket)
        self.calls.append(("list", bucket, prefix))
        names = sorted(n for (b, n) in self._live if b == bucket and n.startswith(prefix))
        if page_token:
            names = [n for n in names if n > page_token]
        items: list[ObjectMeta] = []
        prefixes: list[str] = []
        last: str | None = None
        for n in names:
            if len(items) + len(prefixes) >= max_results:
                break
            rest = n[len(prefix) :]
            if delimiter and delimiter in rest:
                p = prefix + rest.split(delimiter, 1)[0] + delimiter
                if p not in prefixes:
                    prefixes.append(p)
                last = n
                continue
            items.append(self._objs[(bucket, n, self._live[(bucket, n)])].meta)
            last = n
        more = last is not None and any(n > last for n in names)
        return ObjectPage(items=items, prefixes=prefixes, next_page_token=last if more else None)

    async def delete(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        async with self._lock:
            self._bucket(bucket)
            if bucket in self._locked:
                raise AccessDenied(f"bucket {bucket!r} is retention-locked", bucket=bucket)
            self._precondition(bucket, name, if_generation_match)
            obj = self._get_obj(bucket, name, generation)
            self.calls.append(("delete", bucket, name))
            del self._objs[(bucket, name, obj.meta.generation)]
            if self._live.get((bucket, name)) == obj.meta.generation:
                del self._live[(bucket, name)]

    async def copy(
        self,
        src_bucket: str,
        src_name: str,
        dst_bucket: str,
        dst_name: str,
        *,
        source_generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> ObjectMeta:
        check_object_name(dst_name)
        async with self._lock:
            src = self._get_obj(src_bucket, src_name, source_generation)
            self._bucket(dst_bucket)
            self._precondition(dst_bucket, dst_name, if_generation_match)
            self.calls.append(("copy", dst_bucket, dst_name))
            return self._store(
                dst_bucket, dst_name, src.data, src.meta.content_type, src.meta.metadata
            )

    async def sign_get(
        self, bucket: str, name: str, *, expires_in: timedelta, generation: int | None = None
    ) -> SignedGet:
        check_ttl(expires_in)
        meta = self._get_obj(bucket, name, generation).meta
        expires = int(time.time() + expires_in.total_seconds())
        sig = self._sign("GET", bucket, name, meta.generation, expires)
        query = urlencode(
            {"generation": meta.generation, "X-Goog-Expires": expires, "X-Goog-Signature": sig}
        )
        url = f"{self.base_url}/download/storage/v1/b/{bucket}/o/{quote(name, safe='')}?{query}"
        return SignedGet(
            url=url,
            expires_at=datetime.fromtimestamp(expires, UTC),
            generation=meta.generation,
            size=meta.size,
            crc32c=meta.crc32c,
            md5_hash=meta.md5_hash,
            content_type=meta.content_type,
        )

    async def sign_put(
        self,
        bucket: str,
        name: str,
        *,
        expires_in: timedelta,
        content_type: str | None = None,
        max_bytes: int | None = None,
        if_generation_match: int | None = 0,
    ) -> SignedPut:
        check_ttl(expires_in)
        check_object_name(name)
        self._bucket(bucket)
        expires = int(time.time() + expires_in.total_seconds())
        ifgen = "" if if_generation_match is None else str(if_generation_match)
        maxb = "" if max_bytes is None else str(max_bytes)
        ctype = content_type or ""
        sig = self._sign("PUT", bucket, name, expires, ifgen, maxb, ctype)
        query = urlencode(
            {
                "name": name,
                "X-Goog-Expires": expires,
                "X-Goog-Signature": sig,
                "ifgen": ifgen,
                "maxb": maxb,
                "ctype": ctype,
            }
        )
        headers: dict[str, str] = {}
        if content_type:
            headers["content-type"] = content_type
        if if_generation_match is not None:
            headers["x-goog-if-generation-match"] = ifgen
        if max_bytes is not None:
            headers["x-goog-content-length-range"] = f"0,{max_bytes}"
        return SignedPut(
            url=f"{self.base_url}/upload/storage/v1/b/{bucket}/o?{query}",
            name=name,
            expires_at=datetime.fromtimestamp(expires, UTC),
            headers=headers,
            max_bytes=max_bytes,
        )

    async def post_policy(
        self,
        bucket: str,
        prefix: str,
        *,
        expires_in: timedelta,
        max_bytes: int,
        min_bytes: int = 0,
    ) -> PostPolicy:
        check_ttl(expires_in)
        self._bucket(bucket)
        expires = int(time.time() + expires_in.total_seconds())
        doc = {"bucket": bucket, "prefix": prefix, "min": min_bytes, "max": max_bytes}
        doc["expires"] = expires
        policy = base64.b64encode(json.dumps(doc, sort_keys=True).encode()).decode()
        return PostPolicy(
            url=f"{self.base_url}/{bucket}",
            prefix=prefix,
            fields={"policy": policy, "x-goog-signature": self._sign("POST", policy)},
            content_length_range=(min_bytes, max_bytes),
            expires_at=datetime.fromtimestamp(expires, UTC),
        )

    # -------------------------------------------------------- signature checks (HTTP fronts)
    def verify_get(self, bucket: str, name: str, generation: int, expires: int, sig: str) -> bool:
        return expires >= time.time() and self._check_sig(
            sig, "GET", bucket, name, generation, expires
        )

    def verify_put(self, bucket: str, params: Mapping[str, str]) -> bool:
        try:
            expires = int(params["X-Goog-Expires"])
        except (KeyError, ValueError):
            return False
        return expires >= time.time() and self._check_sig(
            params.get("X-Goog-Signature", ""),
            "PUT",
            bucket,
            params.get("name", ""),
            expires,
            params.get("ifgen", ""),
            params.get("maxb", ""),
            params.get("ctype", ""),
        )

    def verify_policy(self, policy_b64: str, sig: str) -> PolicyDoc | None:
        if not self._check_sig(sig, "POST", policy_b64):
            return None
        doc: dict[str, Any] = json.loads(base64.b64decode(policy_b64))
        if doc["expires"] < time.time():
            return None
        return PolicyDoc(doc["bucket"], doc["prefix"], doc["min"], doc["max"], doc["expires"])

    def snapshot(self) -> dict[tuple[str, str], ObjectMeta]:
        """Live objects (for assertions)."""
        return {k: self._objs[(*k, g)].meta for k, g in self._live.items()}
