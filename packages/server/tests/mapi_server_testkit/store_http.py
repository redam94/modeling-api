"""A minimal GCS-like HTTP front for :class:`MemoryObjectStore` (tests only).

It serves exactly what an execution uses: generation-pinned signed GET (with Range), signed
PUT (write-once preconditions, length range), V4-style POST policy uploads, and the
bootstrap object (bearer exec token, get + delete, no list).
"""

from __future__ import annotations

import asyncio
import contextlib
import email.policy
from collections.abc import AsyncIterator
from email.parser import BytesParser

import uvicorn
from mapi_server.storage import MemoryObjectStore
from mapi_spec.storage import NotFound, PreconditionFailed
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

__all__ = ["serve_store", "store_app"]


def _multipart(content_type: str, body: bytes) -> tuple[dict[str, str], bytes | None, str | None]:
    """Parse a multipart/form-data body (fields + the ``file`` part) with the stdlib."""
    msg = BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + body
    )
    fields: dict[str, str] = {}
    data: bytes | None = None
    ctype: str | None = None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if name == "file":
            data, ctype = payload, part.get_content_type()
        elif isinstance(name, str):
            fields[name] = payload.decode()
    return fields, data, ctype


def store_app(store: MemoryObjectStore, *, exec_token: str, bootstrap_bucket: str) -> Starlette:
    async def download(request: Request) -> Response:
        bucket, name = request.path_params["bucket"], request.path_params["name"]
        q = request.query_params
        try:
            gen, expires = int(q["generation"]), int(q["X-Goog-Expires"])
        except (KeyError, ValueError):
            return Response(status_code=400)
        if not store.verify_get(bucket, name, gen, expires, q.get("X-Goog-Signature", "")):
            return Response(status_code=403)
        try:
            data = await store.get(bucket, name, generation=gen)
        except NotFound:
            return Response(status_code=404)
        rng = request.headers.get("range")
        if rng and rng.startswith("bytes="):
            start_s, _, end_s = rng[6:].partition("-")
            start, end = int(start_s), (int(end_s) if end_s else len(data) - 1)
            return Response(data[start : end + 1], status_code=206)
        return Response(data)

    async def upload(request: Request) -> Response:
        bucket = request.path_params["bucket"]
        params = dict(request.query_params)
        if not store.verify_put(bucket, params):
            return Response(status_code=403)
        body = await request.body()
        if params.get("maxb") and len(body) > int(params["maxb"]):
            return Response(status_code=413)
        if params.get("ctype") and request.headers.get("content-type") != params["ctype"]:
            return Response(status_code=403)
        ifgen = params.get("ifgen")
        try:
            meta = await store.put(
                bucket,
                params["name"],
                body,
                content_type=params.get("ctype") or None,
                if_generation_match=int(ifgen) if ifgen else None,
            )
        except PreconditionFailed:
            return Response(status_code=412)
        return JSONResponse(meta.to_gcs_resource())

    async def post_form(request: Request) -> Response:
        bucket = request.path_params["bucket"]
        fields, file_data, file_type = _multipart(
            request.headers.get("content-type", ""), await request.body()
        )
        policy = store.verify_policy(fields.get("policy", ""), fields.get("x-goog-signature", ""))
        if policy is None or policy.bucket != bucket:
            return Response(status_code=403)
        key = fields.get("key", "")
        if not key.startswith(policy.prefix) or key == policy.prefix or file_data is None:
            return Response(status_code=403)
        if not policy.min_bytes <= len(file_data) <= policy.max_bytes:
            return Response(status_code=400)
        try:
            await store.put(bucket, key, file_data, content_type=file_type, if_generation_match=0)
        except PreconditionFailed:
            return Response(status_code=412)
        return Response(status_code=204)

    async def bootstrap(request: Request) -> Response:
        bucket, name = request.path_params["bucket"], request.path_params["name"]
        if bucket != bootstrap_bucket:
            return Response(status_code=403)
        if request.headers.get("authorization") != f"Bearer {exec_token}":
            return Response(status_code=401)
        try:
            if request.method == "DELETE":
                await store.delete(bucket, name)
                return Response(status_code=204)
            return Response(await store.get(bucket, name))
        except NotFound:
            return Response(status_code=404)

    return Starlette(
        routes=[
            Route("/download/storage/v1/b/{bucket}/o/{name:path}", download, methods=["GET"]),
            Route("/upload/storage/v1/b/{bucket}/o", upload, methods=["PUT"]),
            Route("/storage/v1/b/{bucket}/o/{name:path}", bootstrap, methods=["GET", "DELETE"]),
            Route("/{bucket}", post_form, methods=["POST"]),
        ]
    )


@contextlib.asynccontextmanager
async def serve_store(
    store: MemoryObjectStore, *, exec_token: str, bootstrap_bucket: str
) -> AsyncIterator[str]:
    """Serve ``store`` on an ephemeral 127.0.0.1 port; sets ``store.base_url``."""
    app = store_app(store, exec_token=exec_token, bootstrap_bucket=bootstrap_bucket)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    store.base_url = url
    try:
        yield url
    finally:
        server.should_exit = True
        await task
