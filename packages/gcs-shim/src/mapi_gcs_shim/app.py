"""The shim's HTTP surface (Starlette): the GCS JSON-API subset plus XML-style signed access.

JSON API (``Authorization: Bearer <principal token>``, IAM-lite enforced per call)::

    GET    /storage/v1/b                                   buckets.list
    POST   /storage/v1/b                                   buckets.insert
    GET    /storage/v1/b/{bucket}                          buckets.get
    DELETE /storage/v1/b/{bucket}                          buckets.delete (must be empty)
    GET    /storage/v1/b/{bucket}/o                        objects.list (prefix, delimiter,
                                                           pageToken, maxResults, start/endOffset)
    GET    /storage/v1/b/{bucket}/o/{object}               objects.get (alt=json | alt=media)
    PATCH  /storage/v1/b/{bucket}/o/{object}               objects.patch (contentType, metadata)
    DELETE /storage/v1/b/{bucket}/o/{object}               objects.delete
    POST   /storage/v1/b/{b}/o/{o}/rewriteTo/b/{b2}/o/{o2} objects.rewrite (also copyTo)
    POST   /upload/storage/v1/b/{bucket}/o                 objects.insert (media | multipart)
    GET    /download/storage/v1/b/{bucket}/o/{object}      media download

Signed access (XML-API style, the signer's authority is checked at use time)::

    GET|HEAD /{bucket}/{object}?X-Goog-Signature=...       Range-capable download (206)
    PUT      /{bucket}/{object}?X-Goog-Signature=...       upload (Content-MD5 / x-goog-hash)
    POST     /{bucket}                                     V4 POST policy form upload

Shim maintenance: ``GET /_shim/healthz`` (no auth) and ``POST /_shim/v1/lifecycle:sweep``
(``shim.admin``). Object preconditions: ``ifGenerationMatch``, ``ifGenerationNotMatch``,
``ifMetagenerationMatch``, ``ifMetagenerationNotMatch`` (and the ``ifSource*`` forms on
rewrite).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from functools import partial
from typing import Any, BinaryIO, Final, TypeVar
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape

import anyio
import anyio.to_thread
from mapi_spec.storage import crc32c_from_base64
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mapi_gcs_shim._errors import ShimError
from mapi_gcs_shim.backend import (
    MAX_LIST_RESULTS,
    FsBackend,
    Preconditions,
    StagedUpload,
    StoredObject,
    validate_name,
)
from mapi_gcs_shim.config import BucketConfig, PrincipalConfig, ShimConfig, check_bucket_name
from mapi_gcs_shim.iam import Permission
from mapi_gcs_shim.multipart import MultipartReader, parse_boundary
from mapi_gcs_shim.signing import P_SIGNATURE, verify_post_policy, verify_signed_request

__all__ = ["ShimApp"]

log = logging.getLogger("mapi_gcs_shim")

T = TypeVar("T")

_CHUNK: Final = 1024 * 1024
_MAX_JSON_BODY: Final = 1024 * 1024
_MAX_FORM_FIELD: Final = 64 * 1024
_MAX_FORM_FIELDS: Final = 64
_DRAIN_LIMIT: Final = 4 * 1024 * 1024
_METHODS: Final = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
CORS_EXPOSE_HEADERS: Final = (
    "content-length",
    "content-range",
    "etag",
    "x-goog-generation",
    "x-goog-hash",
    "x-goog-metageneration",
    "x-goog-stored-content-length",
)
"""Response headers a browser on an allowed origin may read (GCS bucket CORS equivalent)."""


# ---------------------------------------------------------------------------- helpers


def _segments(request: Request) -> list[str]:
    """Percent-decoded path segments, split on the *raw* path so ``%2F`` stays in a segment."""
    raw = request.scope.get("raw_path")
    try:
        path = raw.split(b"?", 1)[0].decode("ascii") if raw else quote(request.url.path)
        return [unquote(p, errors="strict") for p in path.split("/")[1:]]
    except UnicodeDecodeError as exc:
        raise ShimError.bad_request("the request path is not valid UTF-8") from exc


def _raw_query_pairs(request: Request) -> list[tuple[str, str]]:
    """Query parameters exactly as sent (``+`` is a literal plus, as in V4 canonicalisation)."""
    qs = request.scope.get("query_string", b"")
    try:
        text = qs.decode("ascii")
        pairs = []
        for item in text.split("&"):
            if not item:
                continue
            k, _, v = item.partition("=")
            pairs.append((unquote(k, errors="strict"), unquote(v, errors="strict")))
        return pairs
    except UnicodeDecodeError as exc:
        raise ShimError.bad_request("the query string is not valid UTF-8") from exc


def _int(value: str | None, name: str, *, minimum: int = 0) -> int | None:
    if value is None or value == "":
        return None
    try:
        out = int(value)
    except ValueError:
        out = minimum - 1
    if out < minimum:
        raise ShimError.bad_request(f"{name} must be an integer >= {minimum}")
    return out


def _preconditions(params: Mapping[str, str], prefix: str = "if") -> Preconditions:
    return Preconditions(
        if_generation_match=_int(params.get(f"{prefix}GenerationMatch"), "ifGenerationMatch"),
        if_generation_not_match=_int(
            params.get(f"{prefix}GenerationNotMatch"), "ifGenerationNotMatch"
        ),
        if_metageneration_match=_int(
            params.get(f"{prefix}MetagenerationMatch"), "ifMetagenerationMatch"
        ),
        if_metageneration_not_match=_int(
            params.get(f"{prefix}MetagenerationNotMatch"), "ifMetagenerationNotMatch"
        ),
    )


def _check_read_preconditions(pre: Preconditions, obj: StoredObject) -> None:
    """Reads: a failed ``*NotMatch`` is ``304 Not Modified``, a failed ``*Match`` is 412."""
    if pre.if_generation_not_match is not None and obj.generation == pre.if_generation_not_match:
        raise ShimError(304, "notModified", "not modified")
    if (
        pre.if_metageneration_not_match is not None
        and obj.metageneration == pre.if_metageneration_not_match
    ):
        raise ShimError(304, "notModified", "not modified")
    Preconditions(
        if_generation_match=pre.if_generation_match,
        if_metageneration_match=pre.if_metageneration_match,
    ).check(obj)


def _unsatisfiable(size: int) -> ShimError:
    return ShimError(
        416,
        "requestedRangeNotSatisfiable",
        f"the requested range is not satisfiable (object size {size})",
        xml_code="InvalidRange",
        headers={"content-range": f"bytes */{size}"},
    )


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """One RFC 9110 byte range → inclusive ``(start, end)``; ``None`` = serve everything.

    Unknown units, multiple ranges and syntactically invalid values are ignored (full
    response); syntactically valid but unsatisfiable ranges raise 416.
    """
    if header is None:
        return None
    text = header.strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text[6:].strip()
    if "," in spec or "-" not in spec:
        return None
    first, _, last = (s.strip() for s in spec.partition("-"))
    if first == "":
        if not last.isdigit():
            return None
        n = int(last)
        if n == 0 or size == 0:
            raise _unsatisfiable(size)
        return max(0, size - n), size - 1
    if not first.isdigit() or (last and not last.isdigit()):
        return None
    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        return None
    if start >= size:
        raise _unsatisfiable(size)
    return start, min(end, size - 1)


def _md5_hex(obj: StoredObject) -> str:
    return base64.b64decode(obj.md5_hash).hex()


def _object_headers(obj: StoredObject) -> dict[str, str]:
    return {
        "x-goog-generation": str(obj.generation),
        "x-goog-metageneration": str(obj.metageneration),
        "x-goog-hash": obj.goog_hash(),
        "x-goog-stored-content-length": str(obj.size),
        "x-goog-storage-class": "STANDARD",
        "etag": f'"{_md5_hex(obj)}"',
        "last-modified": format_datetime(obj.updated.astimezone(UTC), usegmt=True),
    }


def _parse_goog_hash(value: str | None) -> tuple[str | None, str | None]:
    """``x-goog-hash: crc32c=...,md5=...`` → ``(crc32c, md5)`` (either may be absent)."""
    crc: str | None = None
    md5: str | None = None
    if not value:
        return None, None
    for item in value.split(","):
        k, sep, v = item.strip().partition("=")
        if not sep:
            raise ShimError.bad_request("malformed x-goog-hash header")
        if k == "crc32c":
            crc = v.strip()
            try:
                crc32c_from_base64(crc)
            except ValueError as exc:
                raise ShimError.bad_request("malformed crc32c in x-goog-hash") from exc
        elif k == "md5":
            md5 = _check_md5(v.strip())
    return crc, md5


def _check_md5(value: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ShimError.bad_request("malformed base64 MD5", xml_code="InvalidDigest") from exc
    if len(raw) != 16:
        raise ShimError.bad_request("an MD5 digest must be 16 bytes", xml_code="InvalidDigest")
    return value


def _custom_metadata(items: Mapping[str, str]) -> dict[str, str]:
    return {k[len("x-goog-meta-") :]: v for k, v in items.items() if k.startswith("x-goog-meta-")}


async def _read_json(request: Request) -> Any:
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > _MAX_JSON_BODY:
            raise ShimError.bad_request("the JSON body is too large")
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ShimError.bad_request("the body is not valid JSON") from exc


def _bucket_config_from_resource(body: Any) -> BucketConfig:
    if not isinstance(body, dict) or not isinstance(body.get("name"), str):
        raise ShimError.bad_request("a bucket resource with a 'name' is required")
    retention: int | None = None
    locked = False
    lifecycle: int | None = None
    rp = body.get("retentionPolicy")
    if isinstance(rp, dict):
        retention = _int(str(rp.get("retentionPeriod", "")), "retentionPeriod", minimum=1)
        locked = bool(rp.get("isLocked", False))
    rules = (body.get("lifecycle") or {}).get("rule") or []
    for rule in rules:
        if (rule.get("action") or {}).get("type") != "Delete":
            raise ShimError.bad_request("only Delete lifecycle rules are supported")
        cond = rule.get("condition") or {}
        if set(cond) != {"age"}:
            raise ShimError.bad_request("only the 'age' lifecycle condition is supported")
        age = _int(str(cond["age"]), "lifecycle age", minimum=1)
        assert age is not None
        lifecycle = age * 86400 if lifecycle is None else min(lifecycle, age * 86400)
    try:
        return BucketConfig(
            name=body["name"],
            retention_period_seconds=retention,
            retention_locked=locked,
            lifecycle_age_seconds=lifecycle,
        )
    except ValueError as exc:
        raise ShimError.bad_request(f"invalid bucket: {exc}") from exc


# ------------------------------------------------------------------------------- app


class ShimApp:
    """ASGI application serving one :class:`FsBackend` with one :class:`ShimConfig`."""

    def __init__(
        self,
        backend: FsBackend,
        config: ShimConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        sweep_interval: float | None = 300.0,
        cors_origins: Sequence[str] = (),
    ) -> None:
        """``cors_origins`` are the browser origins allowed to use signed URLs and the JSON
        API cross-origin (like a bucket CORS configuration); empty disables CORS."""
        self.backend = backend
        self.config = config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sweep_interval = sweep_interval
        self.skew = timedelta(seconds=config.clock_skew_seconds)
        for bucket in config.buckets:
            backend.ensure_bucket(bucket)
        starlette = Starlette(
            routes=[Route("/{path:path}", self._endpoint, methods=_METHODS)],
            lifespan=self._lifespan,
        )
        self.cors_origins = tuple(o.rstrip("/") for o in cors_origins)
        self.asgi: ASGIApp = starlette
        if self.cors_origins:
            self.asgi = CORSMiddleware(
                starlette,
                allow_origins=list(self.cors_origins),
                allow_methods=_METHODS,
                allow_headers=["*"],
                expose_headers=list(CORS_EXPOSE_HEADERS),
                max_age=600,
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.asgi(scope, receive, send)

    @contextlib.asynccontextmanager
    async def _lifespan(self, _app: Starlette) -> AsyncIterator[None]:
        async with anyio.create_task_group() as tg:
            if self.sweep_interval:
                tg.start_soon(self._sweeper, self.sweep_interval)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    async def _sweeper(self, interval: float) -> None:
        while True:
            await anyio.sleep(interval)
            try:
                deleted = await self._run(partial(self.backend.sweep, now=self.clock()))
                if deleted:
                    log.info("lifecycle sweep deleted %d object(s)", len(deleted))
            except Exception:  # pragma: no cover - logged, the loop must survive
                log.exception("lifecycle sweep failed")

    @staticmethod
    async def _run(fn: Callable[[], T]) -> T:
        return await anyio.to_thread.run_sync(fn)

    # ---------------------------------------------------------------- dispatch

    async def _endpoint(self, request: Request) -> Response:
        flavor = "json"
        try:
            segs = _segments(request)
            if segs[:2] == ["storage", "v1"]:
                return await self._json_api(request, segs[2:])
            if segs[:3] == ["upload", "storage", "v1"]:
                return await self._upload(request, segs[3:])
            if segs[:3] == ["download", "storage", "v1"]:
                return await self._download(request, segs[3:])
            if segs[:1] == ["_shim"]:
                return await self._admin(request, segs[1:])
            flavor = "xml"
            return await self._xml_api(request, segs)
        except ShimError as err:
            if request.method in ("PUT", "POST", "PATCH"):
                await self._drain(request)
            return self._error(err, flavor)
        except ClientDisconnect:
            return Response(status_code=400)
        except Exception:
            log.exception("unhandled error serving %s %s", request.method, request.url.path)
            return self._error(ShimError(500, "backendError", "internal shim error"), flavor)

    @staticmethod
    def _error(err: ShimError, flavor: str) -> Response:
        if err.status == 304:
            return Response(status_code=304, headers=err.headers)
        if flavor == "xml":
            return Response(
                err.xml_body(),
                status_code=err.status,
                headers=err.headers,
                media_type="application/xml",
            )
        return JSONResponse(err.json_body(), status_code=err.status, headers=err.headers)

    @staticmethod
    async def _drain(request: Request) -> None:
        """Consume a bounded amount of an unread request body so the client sees our reply."""
        total = 0
        with contextlib.suppress(ClientDisconnect, RuntimeError), anyio.move_on_after(2):
            async for chunk in request.stream():
                total += len(chunk)
                if total > _DRAIN_LIMIT:
                    break

    # ---------------------------------------------------------------- auth

    def _authenticate(self, request: Request) -> PrincipalConfig:
        header = request.headers.get("authorization")
        if not header:
            raise ShimError.unauthenticated(
                "Anonymous caller does not have storage access; send 'Authorization: Bearer "
                "<token>' (see {root}/_shim/keys/*.json)"
            )
        scheme, _, token = header.partition(" ")
        principal = self.config.by_token(token.strip()) if scheme.lower() == "bearer" else None
        if principal is None:
            raise ShimError(401, "authError", "Invalid Credentials", xml_code="AccessDenied")
        return principal

    @staticmethod
    def _require(principal: PrincipalConfig, permission: Permission, bucket: str | None) -> None:
        if not principal.can(permission, bucket):
            target = f"bucket {bucket}" if bucket is not None else "project"
            raise ShimError.forbidden(
                f"{principal.email} does not have {permission.value} access to the "
                f"Google Cloud Storage {target}. Permission '{permission.value}' denied on "
                "resource (or it may not exist)."
            )

    def _secret_for(self, access_id: str) -> str | None:
        principal = self.config.by_email(access_id)
        return None if principal is None else principal.secret.get_secret_value()

    def _signer(self, access_id: str) -> PrincipalConfig:
        principal = self.config.by_email(access_id)
        if principal is None:  # pragma: no cover - verify already looked the signer up
            raise ShimError.forbidden("unknown signer", xml_code="SignatureDoesNotMatch")
        return principal

    # ---------------------------------------------------------------- media + staging

    async def _stage(self, chunks: AsyncIterator[bytes], max_bytes: int) -> StagedUpload:
        staged = await self._run(partial(self.backend.stage, max_bytes=max_bytes))
        try:
            buf = bytearray()
            async for chunk in chunks:
                buf += chunk
                if len(buf) >= _CHUNK:
                    data = bytes(buf)
                    buf.clear()
                    await self._run(partial(staged.write, data))
            if buf:
                await self._run(partial(staged.write, bytes(buf)))
        except BaseException:
            staged.discard()
            raise
        return staged

    async def _commit(self, staged: StagedUpload, **kwargs: Any) -> StoredObject:
        try:
            return await self._run(
                partial(self.backend.commit, staged=staged, now=self.clock(), **kwargs)
            )
        finally:
            staged.discard()

    @staticmethod
    def _check_content_length(request: Request, max_bytes: int) -> None:
        declared = _int(request.headers.get("content-length"), "Content-Length")
        if declared is not None and declared > max_bytes:
            raise ShimError.too_large(f"the object exceeds the maximum size of {max_bytes} bytes")

    async def _media(
        self,
        request: Request,
        bucket: str,
        name: str,
        *,
        generation: int | None,
        preconditions: Preconditions,
    ) -> Response:
        obj, fh = await self._run(partial(self.backend.open, bucket, name, generation=generation))
        try:
            _check_read_preconditions(preconditions, obj)
            rng = parse_range(request.headers.get("range"), obj.size)
        except BaseException:
            fh.close()
            raise
        headers = _object_headers(obj)
        headers["accept-ranges"] = "bytes"
        headers["content-type"] = obj.content_type or "application/octet-stream"
        if rng is None:
            status, start, length = 200, 0, obj.size
        else:
            status, start, length = 206, rng[0], rng[1] - rng[0] + 1
            headers["content-range"] = f"bytes {rng[0]}-{rng[1]}/{obj.size}"
        headers["content-length"] = str(length)
        if request.method == "HEAD":
            fh.close()
            return Response(status_code=status, headers=headers)
        return StreamingResponse(
            _file_chunks(fh, start, length), status_code=status, headers=headers
        )

    # ---------------------------------------------------------------- JSON API

    async def _json_api(self, request: Request, segs: list[str]) -> Response:
        method = request.method
        principal = self._authenticate(request)
        params = request.query_params
        if segs == ["b"]:
            if method == "GET":
                self._require(principal, Permission.BUCKETS_LIST, None)
                records = await self._run(self.backend.list_buckets)
                return JSONResponse(
                    {"kind": "storage#buckets", "items": [r.resource() for r in records]}
                )
            if method == "POST":
                self._require(principal, Permission.BUCKETS_CREATE, None)
                cfg = _bucket_config_from_resource(await _read_json(request))
                record = await self._run(partial(self.backend.create_bucket, cfg, now=self.clock()))
                return JSONResponse(record.resource())
            raise ShimError.not_allowed(method)
        if len(segs) < 2 or segs[0] != "b" or not segs[1]:
            raise ShimError(404, "notFound", "unknown JSON API path")
        bucket = segs[1]
        if len(segs) == 2:
            if method == "GET":
                self._require(principal, Permission.BUCKETS_GET, bucket)
                record = await self._run(partial(self.backend.get_bucket, bucket))
                return JSONResponse(record.resource())
            if method == "DELETE":
                self._require(principal, Permission.BUCKETS_DELETE, bucket)
                await self._run(partial(self.backend.delete_bucket, bucket))
                return Response(status_code=204)
            raise ShimError.not_allowed(method)
        if segs[2] != "o":
            raise ShimError(404, "notFound", "unknown JSON API path")
        if len(segs) == 3 or (len(segs) == 4 and segs[3] == ""):
            if method != "GET":
                raise ShimError.not_allowed(method)
            return await self._list_objects(request, principal, bucket)
        rest = segs[3:]
        if (
            method == "POST"
            and len(rest) >= 6
            and rest[1] in ("rewriteTo", "copyTo")
            and rest[2] == "b"
            and rest[4] == "o"
        ):
            return await self._rewrite(
                request, principal, bucket, rest[0], rest[3], "/".join(rest[5:]), rest[1]
            )
        name = "/".join(rest)
        validate_name(name)
        if method == "GET":
            self._require(principal, Permission.OBJECTS_GET, bucket)
            generation = _int(params.get("generation"), "generation", minimum=1)
            pre = _preconditions(params)
            alt = params.get("alt", "json")
            if alt == "media":
                return await self._media(
                    request, bucket, name, generation=generation, preconditions=pre
                )
            if alt != "json":
                raise ShimError.bad_request("alt must be 'json' or 'media'")
            obj = await self._run(partial(self.backend.stat, bucket, name, generation=generation))
            _check_read_preconditions(pre, obj)
            return JSONResponse(obj.resource(self._base(request)))
        if method == "DELETE":
            self._require(principal, Permission.OBJECTS_DELETE, bucket)
            await self._run(
                partial(
                    self.backend.delete,
                    bucket,
                    name,
                    generation=_int(params.get("generation"), "generation", minimum=1),
                    preconditions=_preconditions(params),
                    now=self.clock(),
                )
            )
            return Response(status_code=204)
        if method == "PATCH":
            self._require(principal, Permission.OBJECTS_UPDATE, bucket)
            body = await _read_json(request)
            if not isinstance(body, dict):
                raise ShimError.bad_request("PATCH needs a JSON object")
            metadata = body.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ShimError.bad_request("metadata must be an object")
            obj = await self._run(
                partial(
                    self.backend.patch,
                    bucket,
                    name,
                    generation=_int(params.get("generation"), "generation", minimum=1),
                    content_type=body.get("contentType"),
                    set_content_type="contentType" in body,
                    metadata=metadata,
                    preconditions=_preconditions(params),
                    now=self.clock(),
                )
            )
            return JSONResponse(obj.resource(self._base(request)))
        raise ShimError.not_allowed(method)

    @staticmethod
    def _base(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    async def _list_objects(
        self, request: Request, principal: PrincipalConfig, bucket: str
    ) -> Response:
        self._require(principal, Permission.OBJECTS_LIST, bucket)
        params = request.query_params
        if params.get("versions", "false").lower() == "true":
            raise ShimError.bad_request("object versioning is not supported by the shim")
        max_results = _int(params.get("maxResults"), "maxResults", minimum=1) or MAX_LIST_RESULTS
        items, prefixes, token = await self._run(
            partial(
                self.backend.list,
                bucket,
                prefix=params.get("prefix", ""),
                delimiter=params.get("delimiter") or None,
                page_token=params.get("pageToken") or None,
                max_results=min(max_results, MAX_LIST_RESULTS),
                start_offset=params.get("startOffset") or None,
                end_offset=params.get("endOffset") or None,
            )
        )
        base = self._base(request)
        body: dict[str, Any] = {"kind": "storage#objects"}
        if items:
            body["items"] = [o.resource(base) for o in items]
        if prefixes:
            body["prefixes"] = prefixes
        if token:
            body["nextPageToken"] = token
        return JSONResponse(body)

    async def _rewrite(
        self,
        request: Request,
        principal: PrincipalConfig,
        src_bucket: str,
        src_name: str,
        dst_bucket: str,
        dst_name: str,
        op: str,
    ) -> Response:
        validate_name(src_name)
        validate_name(dst_name)
        self._require(principal, Permission.OBJECTS_GET, src_bucket)
        self._require(principal, Permission.OBJECTS_CREATE, dst_bucket)
        params = request.query_params
        body = await _read_json(request)
        overrides: dict[str, Any] = body if isinstance(body, dict) else {}
        metadata = overrides.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ShimError.bad_request("metadata must be an object")
        obj = await self._run(
            partial(
                self.backend.copy,
                src_bucket,
                src_name,
                dst_bucket,
                dst_name,
                source_generation=_int(
                    params.get("sourceGeneration"), "sourceGeneration", minimum=1
                ),
                source_preconditions=_preconditions(params, "ifSource"),
                preconditions=_preconditions(params),
                allow_overwrite=principal.can(Permission.OBJECTS_DELETE, dst_bucket),
                content_type=overrides.get("contentType"),
                metadata=metadata,
                max_bytes=self.config.max_object_bytes,
                now=self.clock(),
            )
        )
        resource = obj.resource(self._base(request))
        if op == "copyTo":
            return JSONResponse(resource)
        return JSONResponse(
            {
                "kind": "storage#rewriteResponse",
                "totalBytesRewritten": str(obj.size),
                "objectSize": str(obj.size),
                "done": True,
                "resource": resource,
            }
        )

    async def _upload(self, request: Request, segs: list[str]) -> Response:
        if len(segs) != 3 or segs[0] != "b" or segs[2] != "o":
            raise ShimError(404, "notFound", "unknown upload path")
        if request.method != "POST":
            raise ShimError.not_allowed(request.method)
        principal = self._authenticate(request)
        bucket = segs[1]
        self._require(principal, Permission.OBJECTS_CREATE, bucket)
        params = request.query_params
        pre = _preconditions(params)
        allow_overwrite = principal.can(Permission.OBJECTS_DELETE, bucket)
        max_bytes = self.config.max_object_bytes
        upload_type = params.get("uploadType", "media")
        name: str | None = params.get("name") or None
        expected_crc: str | None = None
        expected_md5: str | None = None
        metadata: dict[str, str] = {}
        if upload_type == "media":
            if name is None:
                raise ShimError.bad_request("uploadType=media requires the 'name' parameter")
            validate_name(name)
            self._check_content_length(request, max_bytes)
            await self._early_checks(bucket, name, pre, allow_overwrite)
            content_type = request.headers.get("content-type")
            staged = await self._stage(request.stream(), max_bytes)
        elif upload_type == "multipart":
            reader = MultipartReader(
                request.stream(),
                parse_boundary(request.headers.get("content-type"), subtype="related"),
            )
            if await reader.next_part() is None:
                raise ShimError.bad_request("multipart upload needs a metadata part")
            try:
                resource = json.loads(await reader.read_all(_MAX_JSON_BODY))
            except ValueError as exc:
                raise ShimError.bad_request("the metadata part is not valid JSON") from exc
            if not isinstance(resource, dict):
                raise ShimError.bad_request("the metadata part must be a JSON object")
            name = name or resource.get("name")
            if not isinstance(name, str):
                raise ShimError.bad_request("the object name is missing")
            validate_name(name)
            raw_meta = resource.get("metadata") or {}
            if not isinstance(raw_meta, dict):
                raise ShimError.bad_request("metadata must be an object")
            metadata = raw_meta
            if "crc32c" in resource:
                expected_crc = _parse_goog_hash(f"crc32c={resource['crc32c']}")[0]
            if "md5Hash" in resource:
                expected_md5 = _check_md5(str(resource["md5Hash"]))
            await self._early_checks(bucket, name, pre, allow_overwrite)
            media = await reader.next_part()
            if media is None:
                raise ShimError.bad_request("multipart upload needs a media part")
            content_type = resource.get("contentType") or media.content_type
            staged = await self._stage(reader.iter_data(), max_bytes)
            try:
                if await reader.next_part() is not None:
                    raise ShimError.bad_request("multipart upload must have exactly two parts")
            except BaseException:
                staged.discard()
                raise
        elif upload_type == "resumable":
            raise ShimError(
                501,
                "notImplemented",
                "uploadType=resumable is not supported by the shim; use media or multipart "
                "(or a signed PUT URL)",
                xml_code="NotImplemented",
            )
        else:
            raise ShimError.bad_request(f"unknown uploadType {upload_type!r}")
        obj = await self._commit(
            staged,
            bucket=bucket,
            name=name,
            content_type=content_type,
            metadata=metadata,
            preconditions=pre,
            allow_overwrite=allow_overwrite,
            expected_crc32c=expected_crc,
            expected_md5=expected_md5,
        )
        return JSONResponse(obj.resource(self._base(request)))

    async def _early_checks(
        self, bucket: str, name: str, pre: Preconditions, allow_overwrite: bool
    ) -> None:
        """Fail fast (before reading a body) on a missing bucket or an unmet precondition.

        The authoritative checks are repeated atomically at commit time.
        """

        def check() -> None:
            self.backend.get_bucket(bucket)
            try:
                live: StoredObject | None = self.backend.stat(bucket, name)
            except ShimError as err:
                if err.status != 404:
                    raise
                live = None
            pre.check(live)
            if live is not None and not allow_overwrite:
                raise ShimError.forbidden("overwriting an object requires storage.objects.delete")

        await self._run(check)

    async def _download(self, request: Request, segs: list[str]) -> Response:
        if len(segs) < 4 or segs[0] != "b" or segs[2] != "o":
            raise ShimError(404, "notFound", "unknown download path")
        if request.method not in ("GET", "HEAD"):
            raise ShimError.not_allowed(request.method)
        principal = self._authenticate(request)
        bucket, name = segs[1], "/".join(segs[3:])
        validate_name(name)
        self._require(principal, Permission.OBJECTS_GET, bucket)
        params = request.query_params
        return await self._media(
            request,
            bucket,
            name,
            generation=_int(params.get("generation"), "generation", minimum=1),
            preconditions=_preconditions(params),
        )

    # ---------------------------------------------------------------- admin

    async def _admin(self, request: Request, segs: list[str]) -> Response:
        if segs == ["healthz"] and request.method in ("GET", "HEAD"):
            return JSONResponse({"status": "ok", "env": self.config.env})
        if segs == ["v1", "lifecycle:sweep"] and request.method == "POST":
            principal = self._authenticate(request)
            self._require(principal, Permission.SHIM_ADMIN, None)
            now = self.clock()
            if "now" in request.query_params:
                try:
                    now = datetime.fromisoformat(request.query_params["now"].replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ShimError.bad_request("now must be an ISO-8601 timestamp") from exc
                if now.tzinfo is None:
                    raise ShimError.bad_request("now must carry a timezone")
            deleted = await self._run(partial(self.backend.sweep, now=now))
            return JSONResponse(
                {
                    "deleted": [
                        {"bucket": o.bucket, "name": o.name, "generation": str(o.generation)}
                        for o in deleted
                    ]
                }
            )
        raise ShimError(404, "notFound", "unknown shim path")

    # ---------------------------------------------------------------- XML / signed

    async def _xml_api(self, request: Request, segs: list[str]) -> Response:
        if not segs or not segs[0]:
            raise ShimError.forbidden("listing buckets requires the JSON API")
        bucket = segs[0]
        try:
            check_bucket_name(bucket)
        except ValueError as exc:
            raise ShimError.bad_request(str(exc), xml_code="InvalidBucketName") from exc
        if len(segs) == 1 or (len(segs) == 2 and segs[1] == ""):
            if request.method == "POST":
                return await self._post_policy_upload(request, bucket)
            raise ShimError.not_allowed(request.method)
        name = "/".join(segs[1:])
        validate_name(name)
        if request.method in ("GET", "HEAD"):
            return await self._signed_get(request, bucket, name)
        if request.method == "PUT":
            return await self._signed_put(request, bucket, name)
        raise ShimError.not_allowed(request.method)

    def _verify(
        self, request: Request, bucket: str, name: str
    ) -> tuple[PrincipalConfig, dict[str, str]]:
        pairs = _raw_query_pairs(request)
        if not any(k == P_SIGNATURE for k, _ in pairs):
            raise ShimError.forbidden(
                "Anonymous caller does not have access; this endpoint only accepts V4 signed "
                "URLs (X-Goog-Signature)"
            )
        signed = verify_signed_request(
            method=request.method,
            bucket=bucket,
            name=name,
            query=pairs,
            headers=dict(request.headers),
            lookup_secret=self._secret_for,
            now=self.clock(),
            clock_skew=self.skew,
        )
        return self._signer(signed.access_id), dict(pairs)

    async def _signed_get(self, request: Request, bucket: str, name: str) -> Response:
        signer, query = self._verify(request, bucket, name)
        self._require(signer, Permission.OBJECTS_GET, bucket)
        return await self._media(
            request,
            bucket,
            name,
            generation=_int(query.get("generation"), "generation", minimum=1),
            preconditions=Preconditions(),
        )

    async def _signed_put(self, request: Request, bucket: str, name: str) -> Response:
        signer, _ = self._verify(request, bucket, name)
        self._require(signer, Permission.OBJECTS_CREATE, bucket)
        allow_overwrite = signer.can(Permission.OBJECTS_DELETE, bucket)
        h = request.headers
        pre = Preconditions(
            if_generation_match=_int(
                h.get("x-goog-if-generation-match"), "x-goog-if-generation-match"
            ),
            if_metageneration_match=_int(
                h.get("x-goog-if-metageneration-match"), "x-goog-if-metageneration-match"
            ),
        )
        lo, hi = 0, self.config.max_object_bytes
        length_range = h.get("x-goog-content-length-range")
        if length_range is not None:
            try:
                lo_s, hi_s = length_range.split(",")
                lo, hi = int(lo_s), min(int(hi_s), hi)
            except ValueError as exc:
                raise ShimError.bad_request(
                    "x-goog-content-length-range must be 'min,max'"
                ) from exc
            if lo < 0 or hi < lo:
                raise ShimError.bad_request("x-goog-content-length-range must be 0 <= min <= max")
        expected_crc, expected_md5 = _parse_goog_hash(h.get("x-goog-hash"))
        content_md5 = h.get("content-md5")
        if content_md5 is not None:
            content_md5 = _check_md5(content_md5)
            if expected_md5 is not None and expected_md5 != content_md5:
                raise ShimError.bad_digest("Content-MD5 and x-goog-hash md5 disagree")
            expected_md5 = content_md5
        self._check_content_length(request, hi)
        await self._early_checks(bucket, name, pre, allow_overwrite)
        staged = await self._stage(request.stream(), hi)
        if staged.size < lo:
            staged.discard()
            raise ShimError.too_small(f"the object is smaller than the minimum of {lo} bytes")
        obj = await self._commit(
            staged,
            bucket=bucket,
            name=name,
            content_type=h.get("content-type"),
            metadata=_custom_metadata(h),
            preconditions=pre,
            allow_overwrite=allow_overwrite,
            expected_crc32c=expected_crc,
            expected_md5=expected_md5,
        )
        return Response(status_code=200, headers=_object_headers(obj))

    async def _post_policy_upload(self, request: Request, bucket: str) -> Response:
        reader = MultipartReader(
            request.stream(),
            parse_boundary(request.headers.get("content-type"), subtype="form-data"),
        )
        fields: dict[str, str] = {}
        obj: StoredObject | None = None
        while (part := await reader.next_part()) is not None:
            field = (part.name or "").lower()
            if not field:
                raise ShimError.bad_request(
                    "every form part needs a name", xml_code="InvalidArgument"
                )
            if field == "file":
                obj = await self._policy_file(request, bucket, fields, reader, part.filename)
                break
            if field in fields:
                raise ShimError.bad_request(f"duplicate form field {field!r}")
            if len(fields) >= _MAX_FORM_FIELDS:
                raise ShimError.bad_request("too many form fields")
            try:
                fields[field] = (await reader.read_all(_MAX_FORM_FIELD)).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ShimError.bad_request(f"form field {field!r} is not UTF-8") from exc
        if obj is None:
            raise ShimError.bad_request("the form has no 'file' field", xml_code="InvalidArgument")
        await self._drain(request)  # fields after 'file' are ignored (GCS semantics)
        headers = _object_headers(obj)
        location = f"{self._base(request)}/{quote(bucket, safe='')}/{quote(obj.name, safe='/')}"
        headers["location"] = location
        status = fields.get("success_action_status", "204")
        if status == "201":
            body = (
                "<?xml version='1.0' encoding='UTF-8'?><PostResponse>"
                f"<Location>{escape(location)}</Location><Bucket>{escape(bucket)}</Bucket>"
                f'<Key>{escape(obj.name)}</Key><ETag>"{_md5_hex(obj)}"</ETag></PostResponse>'
            )
            return Response(body, status_code=201, headers=headers, media_type="application/xml")
        if status == "200":
            return Response(status_code=200, headers=headers)
        return Response(status_code=204, headers=headers)

    async def _policy_file(
        self,
        request: Request,
        bucket: str,
        fields: dict[str, str],
        reader: MultipartReader,
        filename: str | None,
    ) -> StoredObject:
        decision = verify_post_policy(
            fields,
            bucket=bucket,
            lookup_secret=self._secret_for,
            now=self.clock(),
            clock_skew=self.skew,
        )
        signer = self._signer(decision.access_id)
        self._require(signer, Permission.OBJECTS_CREATE, bucket)
        allow_overwrite = signer.can(Permission.OBJECTS_DELETE, bucket)
        key = fields["key"].replace("${filename}", filename or "")
        validate_name(key)
        lo, hi = decision.content_length_range or (0, self.config.max_object_bytes)
        hi = min(hi, self.config.max_object_bytes)
        # every form field is covered by a signed condition, so this one cannot be dropped
        pre = Preconditions(
            if_generation_match=_int(
                fields.get("x-goog-if-generation-match"), "x-goog-if-generation-match"
            )
        )
        await self._early_checks(bucket, key, pre, allow_overwrite)
        staged = await self._stage(reader.iter_data(), hi)
        if staged.size < lo:
            staged.discard()
            raise ShimError.too_small(f"the upload is smaller than the minimum of {lo} bytes")
        return await self._commit(
            staged,
            bucket=bucket,
            name=key,
            content_type=fields.get("content-type"),
            metadata=_custom_metadata(fields),
            preconditions=pre,
            allow_overwrite=allow_overwrite,
        )


async def _file_chunks(fh: BinaryIO, start: int, length: int) -> AsyncIterator[bytes]:
    try:
        await anyio.to_thread.run_sync(fh.seek, start)
        remaining = length
        while remaining > 0:
            chunk = await anyio.to_thread.run_sync(fh.read, min(_CHUNK, remaining))
            if not chunk:  # pragma: no cover - the file is immutable once committed
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        fh.close()
