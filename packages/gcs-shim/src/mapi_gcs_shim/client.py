"""``ShimObjectStore``: :class:`mapi_spec.storage.ObjectStore` over the shim's HTTP API.

Reads and writes go through the GCS JSON API with the principal's bearer token. Signed URLs
and POST policies are computed locally from the principal's HMAC secret (like GCS signing
with a service-account key: no network round trip), against ``public_endpoint`` — the address
a sandbox will use, which may differ from the address the control plane uses.
"""

from __future__ import annotations

import builtins
import html
import json
import os
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Final, NoReturn
from urllib.parse import quote

import httpx
from mapi_spec.errors import ErrorCode
from mapi_spec.localdev import LocalDev
from mapi_spec.storage import (
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
)

from mapi_gcs_shim.config import PRINCIPAL_CONTROL, ShimCredentials, default_root
from mapi_gcs_shim.signing import (
    SigningIdentity,
    build_post_policy,
    hmac_policy_signer,
    sign_url,
)

__all__ = ["InvalidRange", "ShimObjectStore", "raise_for_storage_error"]

_DEFAULT_TIMEOUT: Final = 60.0


class InvalidRange(StorageError):
    """The requested byte range starts beyond the end of the object (HTTP 416)."""

    default_code = ErrorCode.PRECONDITION_FAILED


_XML_FIELD = re.compile(r"<(Code|Message)>(.*?)</\1>", re.S)


def _message(response: httpx.Response) -> str:
    """The error message of a JSON-API or XML-API error body."""
    try:
        body = response.json()
        return str(body["error"]["message"])
    except (ValueError, KeyError, TypeError):
        pass
    text = response.text.strip()
    found = dict(_XML_FIELD.findall(text))
    if found:
        return ": ".join(html.unescape(found[k]) for k in ("Code", "Message") if k in found)
    return text[:500] or response.reason_phrase


def raise_for_storage_error(
    response: httpx.Response, *, bucket: str | None = None, name: str | None = None
) -> NoReturn:
    """Map a failed shim/GCS response to the ``mapi_spec.storage`` error hierarchy."""
    status = response.status_code
    detail = f"{status} {_message(response)}"
    if status == 404:
        raise NotFound(detail, bucket=bucket, name=name)
    if status == 412:
        raise PreconditionFailed(detail, bucket=bucket, name=name)
    if status in (401, 403):
        raise AccessDenied(detail, bucket=bucket, name=name)
    if status == 416:
        raise InvalidRange(detail, bucket=bucket, name=name)
    if status == 409:
        raise StorageError(detail, bucket=bucket, name=name, code=ErrorCode.CONFLICT)
    if status == 413:
        raise StorageError(detail, bucket=bucket, name=name, code=ErrorCode.PAYLOAD_TOO_LARGE)
    raise StorageError(detail, bucket=bucket, name=name)


def _o(name: str) -> str:
    return quote(name, safe="")


def _b(bucket: str) -> str:
    return quote(bucket, safe="")


def _parse_goog_hash(value: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in (value or "").split(","):
        k, sep, v = item.strip().partition("=")
        if sep:
            out[k] = v
    return out


class ShimObjectStore:
    """Async :class:`~mapi_spec.storage.ObjectStore` backed by the GCS shim.

    Args:
        endpoint: where this process reaches the shim (``http://127.0.0.1:4443``).
        credentials: the principal's key (``ShimCredentials.from_root(root, "control")``).
        public_endpoint: base URL embedded in signed URLs (default: ``endpoint``).
        client: an ``httpx.AsyncClient`` to reuse (not closed by :meth:`aclose`).
        clock: ``now()`` used for signing (tests).
    """

    def __init__(
        self,
        endpoint: str,
        credentials: ShimCredentials,
        *,
        public_endpoint: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.public_endpoint = (public_endpoint or endpoint).rstrip("/")
        self.credentials = credentials
        self._identity = SigningIdentity(
            access_id=credentials.client_email, secret=credentials.secret.get_secret_value()
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._auth = {"Authorization": f"Bearer {credentials.token.get_secret_value()}"}
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_root(
        cls,
        root: str | os.PathLike[str],
        principal: str = PRINCIPAL_CONTROL,
        *,
        endpoint: str | None = None,
        **kwargs: Any,
    ) -> ShimObjectStore:
        """Load ``{root}/_shim/keys/{principal}.json`` (endpoint defaults to the key file's)."""
        creds = ShimCredentials.from_root(root, principal)
        url = endpoint or creds.endpoint
        if url is None:
            raise ValueError("no endpoint given and the key file does not record one")
        return cls(url, creds, **kwargs)

    @classmethod
    def from_local_dev(
        cls, local: LocalDev, principal: str = PRINCIPAL_CONTROL, **kwargs: Any
    ) -> ShimObjectStore:
        """The local stack's store: key under ``{MAPI_DATA_DIR}/gcs``, ``MAPI_GCS_ENDPOINT``."""
        return cls.from_root(
            default_root(local.data_dir), principal, endpoint=local.gcs_endpoint, **kwargs
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> ShimObjectStore:
        return self

    async def __aexit__(
        self,
        et: type[BaseException] | None,
        ev: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- plumbing

    def _object_url(self, bucket: str, name: str) -> str:
        return f"{self.endpoint}/storage/v1/b/{_b(bucket)}/o/{_o(name)}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        bucket: str | None = None,
        name: str | None = None,
        params: Mapping[str, str | int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        clean = {k: str(v) for k, v in (params or {}).items()}
        headers = {**self._auth, **kwargs.pop("headers", {})}
        try:
            response = await self._client.request(
                method, url, params=clean, headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise StorageError(f"GCS shim request failed: {exc}", bucket=bucket, name=name) from exc
        if response.status_code >= 400:
            raise_for_storage_error(response, bucket=bucket, name=name)
        return response

    @staticmethod
    def _meta(response: httpx.Response) -> ObjectMeta:
        return ObjectMeta.from_gcs_resource(response.json())

    # ---------------------------------------------------------------- ObjectStore

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
        resource: dict[str, Any] = {"name": name}
        if content_type is not None:
            resource["contentType"] = content_type
        if metadata:
            resource["metadata"] = dict(metadata)
        boundary = "mapi-" + secrets.token_hex(16)
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                json.dumps(resource).encode(),
                f"\r\n--{boundary}\r\nContent-Type: "
                f"{content_type or 'application/octet-stream'}\r\n\r\n".encode(),
                bytes(data),
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        params: dict[str, str | int] = {"uploadType": "multipart"}
        if if_generation_match is not None:
            params["ifGenerationMatch"] = if_generation_match
        response = await self._request(
            "POST",
            f"{self.endpoint}/upload/storage/v1/b/{_b(bucket)}/o",
            bucket=bucket,
            name=name,
            params=params,
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return self._meta(response)

    async def get(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        byte_range: tuple[int, int | None] | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        check_object_name(name)
        params: dict[str, str] = {"alt": "media"}
        if generation is not None:
            params["generation"] = str(generation)
        headers = dict(self._auth)
        if byte_range is not None:
            start, end = byte_range
            if start < 0 or (end is not None and end < start):
                raise ValueError("byte_range must be (start >= 0, end >= start | None)")
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        try:
            async with self._client.stream(
                "GET", self._object_url(bucket, name), params=params, headers=headers
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise_for_storage_error(response, bucket=bucket, name=name)
                length = int(response.headers.get("content-length", "-1"))
                if max_bytes is not None and length > max_bytes:
                    raise StorageError(
                        f"object is {length} bytes, above the {max_bytes}-byte cap",
                        bucket=bucket,
                        name=name,
                        code=ErrorCode.PAYLOAD_TOO_LARGE,
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise StorageError(
                            f"object exceeds the {max_bytes}-byte cap",
                            bucket=bucket,
                            name=name,
                            code=ErrorCode.PAYLOAD_TOO_LARGE,
                        )
                    chunks.append(chunk)
                data = b"".join(chunks)
                if response.status_code == 200:
                    self._verify_full_read(response, data, bucket, name)
                return data
        except httpx.HTTPError as exc:
            raise StorageError(f"GCS shim request failed: {exc}", bucket=bucket, name=name) from exc

    @staticmethod
    def _verify_full_read(response: httpx.Response, data: bytes, bucket: str, name: str) -> None:
        """Corruption check on a full download (CRC32C from ``x-goog-hash``)."""
        expected = _parse_goog_hash(response.headers.get("x-goog-hash")).get("crc32c")
        if expected is not None and crc32c_to_base64(crc32c(data)) != expected:
            raise StorageError(
                "downloaded bytes do not match the server CRC32C",
                bucket=bucket,
                name=name,
                code=ErrorCode.INTEGRITY_FAILURE,
            )

    async def stat(self, bucket: str, name: str, *, generation: int | None = None) -> ObjectMeta:
        check_object_name(name)
        params = {} if generation is None else {"generation": generation}
        response = await self._request(
            "GET", self._object_url(bucket, name), bucket=bucket, name=name, params=params
        )
        return self._meta(response)

    async def list(
        self,
        bucket: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        page_token: str | None = None,
        max_results: int = 1000,
    ) -> ObjectPage:
        params: dict[str, str | int] = {"maxResults": max_results}
        if prefix:
            params["prefix"] = prefix
        if delimiter:
            params["delimiter"] = delimiter
        if page_token:
            params["pageToken"] = page_token
        response = await self._request(
            "GET", f"{self.endpoint}/storage/v1/b/{_b(bucket)}/o", bucket=bucket, params=params
        )
        body = response.json()
        return ObjectPage(
            items=[ObjectMeta.from_gcs_resource(r) for r in body.get("items", [])],
            prefixes=list(body.get("prefixes", [])),
            next_page_token=body.get("nextPageToken"),
        )

    async def delete(
        self,
        bucket: str,
        name: str,
        *,
        generation: int | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        check_object_name(name)
        params: dict[str, str | int] = {}
        if generation is not None:
            params["generation"] = generation
        if if_generation_match is not None:
            params["ifGenerationMatch"] = if_generation_match
        await self._request(
            "DELETE", self._object_url(bucket, name), bucket=bucket, name=name, params=params
        )

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
        check_object_name(src_name)
        check_object_name(dst_name)
        params: dict[str, str | int] = {}
        if source_generation is not None:
            params["sourceGeneration"] = source_generation
        if if_generation_match is not None:
            params["ifGenerationMatch"] = if_generation_match
        src_url = self._object_url(src_bucket, src_name)
        url = f"{src_url}/rewriteTo/b/{_b(dst_bucket)}/o/{_o(dst_name)}"
        while True:
            response = await self._request(
                "POST", url, bucket=dst_bucket, name=dst_name, params=params, json={}
            )
            body = response.json()
            if body.get("done"):
                return ObjectMeta.from_gcs_resource(body["resource"])
            token = body.get("rewriteToken")
            if not token:  # pragma: no cover - a GCS protocol violation
                raise StorageError("rewrite returned neither done nor a rewriteToken")
            params["rewriteToken"] = token

    async def sign_get(
        self, bucket: str, name: str, *, expires_in: timedelta, generation: int | None = None
    ) -> SignedGet:
        check_ttl(expires_in)
        meta = await self.stat(bucket, name, generation=generation)
        now = self._clock()
        url = sign_url(
            self._identity,
            method="GET",
            endpoint=self.public_endpoint,
            bucket=bucket,
            name=name,
            expires_in=expires_in,
            now=now,
            query={"generation": str(meta.generation)},
        )
        return SignedGet(
            url=url,
            expires_at=now.replace(microsecond=0) + expires_in,
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
        headers: dict[str, str] = {}
        if content_type is not None:
            headers["content-type"] = content_type
        if if_generation_match is not None:
            headers["x-goog-if-generation-match"] = str(if_generation_match)
        if max_bytes is not None:
            if max_bytes < 0:
                raise ValueError("max_bytes must be >= 0")
            headers["x-goog-content-length-range"] = f"0,{max_bytes}"
        now = self._clock()
        url = sign_url(
            self._identity,
            method="PUT",
            endpoint=self.public_endpoint,
            bucket=bucket,
            name=name,
            expires_in=expires_in,
            now=now,
            headers=headers,
        )
        return SignedPut(
            url=url,
            name=name,
            expires_at=now.replace(microsecond=0) + expires_in,
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
        create_only: bool = False,
    ) -> PostPolicy:
        check_ttl(expires_in)
        check_object_name(prefix)
        if not prefix.endswith("/"):
            raise ValueError("a POST policy prefix must end with '/'")
        now = self._clock()
        fields, expires_at = build_post_policy(
            access_id=self._identity.access_id,
            bucket=bucket,
            prefix=prefix,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            expires_in=expires_in,
            sign=hmac_policy_signer(self._identity.secret),
            now=now,
            if_generation_match=0 if create_only else None,
        )
        return PostPolicy(
            url=f"{self.public_endpoint}/{_b(bucket)}",
            prefix=prefix,
            fields=fields,
            content_length_range=(min_bytes, max_bytes),
            expires_at=expires_at,
            create_only=create_only,
        )

    # ---------------------------------------------------------------- extras (not protocol)

    async def create_bucket(
        self,
        bucket: str,
        *,
        retention_period_seconds: int | None = None,
        retention_locked: bool = False,
        lifecycle_age_days: int | None = None,
    ) -> dict[str, Any]:
        """Create a bucket (needs ``storage.buckets.create``); returns the bucket resource."""
        resource: dict[str, Any] = {"name": bucket}
        if retention_period_seconds is not None:
            resource["retentionPolicy"] = {
                "retentionPeriod": str(retention_period_seconds),
                "isLocked": retention_locked,
            }
        if lifecycle_age_days is not None:
            resource["lifecycle"] = {
                "rule": [{"action": {"type": "Delete"}, "condition": {"age": lifecycle_age_days}}]
            }
        response = await self._request(
            "POST", f"{self.endpoint}/storage/v1/b", bucket=bucket, json=resource
        )
        result: dict[str, Any] = response.json()
        return result

    async def get_bucket(self, bucket: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"{self.endpoint}/storage/v1/b/{_b(bucket)}", bucket=bucket
        )
        result: dict[str, Any] = response.json()
        return result

    async def sweep_lifecycle(
        self, *, now: datetime | None = None
    ) -> builtins.list[dict[str, str]]:
        """Run the lifecycle sweep now (needs ``shim.admin``); returns deleted objects."""
        params = {} if now is None else {"now": now.astimezone(UTC).isoformat()}
        response = await self._request(
            "POST", f"{self.endpoint}/_shim/v1/lifecycle:sweep", params=params
        )
        deleted: builtins.list[dict[str, str]] = response.json()["deleted"]
        return deleted
