"""A minimal in-test GCS fake with signed URLs (stand-in until ``mapi_gcs_shim`` is merged).

It implements exactly the semantics the runner relies on:

* objects with generations and server-computed CRC32C/MD5 (GCS base64 forms);
* the JSON API ``/storage/v1/b/{bucket}/o/{name}`` (``alt=media`` GET, metadata GET, DELETE)
  for the *exec* principal (bearer token), restricted to the bootstrap bucket, no listing;
* signed GET URLs pinned to a generation, honouring ``Range``;
* signed PUT URLs whose signed headers (``x-goog-if-generation-match``,
  ``x-goog-content-length-range``) must be sent verbatim;
* V4-style POST policies: ``multipart/form-data`` with ``policy`` + ``x-goog-signature``
  fields, ``key`` under the policy prefix and a ``content-length-range``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from mapi_spec.storage import (
    Buckets,
    ObjectMeta,
    PostPolicy,
    SignedGet,
    SignedPut,
    crc32c,
    crc32c_to_base64,
    md5_base64,
)

__all__ = ["FakeGcs", "StoredObject"]


@dataclass(slots=True)
class StoredObject:
    data: bytes
    generation: int
    content_type: str | None
    time_created: datetime

    def meta(self, bucket: str, name: str) -> ObjectMeta:
        return ObjectMeta(
            bucket=bucket,
            name=name,
            generation=self.generation,
            size=len(self.data),
            crc32c=crc32c_to_base64(crc32c(self.data)),
            md5_hash=md5_base64(self.data),
            content_type=self.content_type,
            time_created=self.time_created,
        )


@dataclass(slots=True)
class _Request:
    method: str
    path: str
    status: int


@dataclass
class FakeGcs:
    env: str = "test"
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    exec_token: str = field(default_factory=lambda: secrets.token_hex(16))
    objects: dict[tuple[str, str], StoredObject] = field(default_factory=dict)
    requests: list[_Request] = field(default_factory=list)
    _gen: int = 1_760_000_000_000_000
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> FakeGcs:
        fake = self

        class Handler(_Handler):
            gcs = fake

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def endpoint(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def buckets(self) -> Buckets:
        return Buckets(self.env)

    # ------------------------------------------------------------------- store
    def put(self, bucket: str, name: str, data: bytes, content_type: str | None = None) -> ObjectMeta:
        with self._lock:
            self._gen += 1
            obj = StoredObject(data, self._gen, content_type, datetime.now(UTC))
            self.objects[(bucket, name)] = obj
        return obj.meta(bucket, name)

    def get(self, bucket: str, name: str) -> bytes:
        return self.objects[(bucket, name)].data

    def exists(self, bucket: str, name: str) -> bool:
        return (bucket, name) in self.objects

    def list(self, bucket: str, prefix: str = "") -> list[ObjectMeta]:
        return [
            o.meta(b, n)
            for (b, n), o in sorted(self.objects.items())
            if b == bucket and n.startswith(prefix)
        ]

    # ----------------------------------------------------------------- signing
    def _sig(self, *parts: str) -> str:
        return hmac.new(self.secret, "\n".join(parts).encode(), hashlib.sha256).hexdigest()

    def sign_get(self, bucket: str, name: str, ttl: float = 3600) -> SignedGet:
        obj = self.objects[(bucket, name)]
        exp = int(time.time() + ttl)
        sig = self._sig("GET", bucket, name, str(exp), str(obj.generation))
        url = (
            f"{self.endpoint}/download/{bucket}/{quote(name, safe='')}"
            f"?generation={obj.generation}&X-Goog-Expires={exp}&X-Goog-Signature={sig}"
        )
        meta = obj.meta(bucket, name)
        return SignedGet(
            url=url,
            expires_at=datetime.fromtimestamp(exp, UTC),
            generation=obj.generation,
            size=meta.size,
            crc32c=meta.crc32c,
            md5_hash=meta.md5_hash,
            content_type=obj.content_type,
        )

    def sign_put(self, bucket: str, name: str, *, max_bytes: int = 1 << 20) -> SignedPut:
        exp = int(time.time() + 3600)
        headers = {
            "x-goog-if-generation-match": "0",
            "x-goog-content-length-range": f"0,{max_bytes}",
        }
        sig = self._sig("PUT", bucket, name, str(exp), json.dumps(headers, sort_keys=True))
        url = (
            f"{self.endpoint}/upload/{bucket}/{quote(name, safe='')}"
            f"?X-Goog-Expires={exp}&X-Goog-Signature={sig}"
        )
        return SignedPut(
            url=url,
            name=name,
            expires_at=datetime.fromtimestamp(exp, UTC),
            headers=headers,
            max_bytes=max_bytes,
        )

    def post_policy(self, bucket: str, prefix: str, *, max_bytes: int = 1 << 30) -> PostPolicy:
        exp = int(time.time() + 3600)
        doc = {"bucket": bucket, "prefix": prefix, "min": 0, "max": max_bytes, "expires": exp}
        policy = base64.b64encode(json.dumps(doc, sort_keys=True).encode()).decode()
        return PostPolicy(
            url=f"{self.endpoint}/{bucket}",
            prefix=prefix,
            fields={"policy": policy, "x-goog-signature": self._sig("POST", policy)},
            content_length_range=(0, max_bytes),
            expires_at=datetime.fromtimestamp(exp, UTC) + timedelta(seconds=0),
        )

    def bootstrap_template(self) -> str:
        return f"{self.endpoint}/storage/v1/b/{self.buckets.bootstrap}/o/{{nonce}}"


class _Handler(BaseHTTPRequestHandler):
    gcs: FakeGcs
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    # ---------------------------------------------------------------- plumbing
    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.gcs.requests.append(_Request(self.command, self.path.split("?")[0], status))
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _route(self) -> tuple[list[str], dict[str, str]]:
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        return [unquote(p) for p in parts.path.strip("/").split("/")], query

    def _expired(self, query: dict[str, str]) -> bool:
        return int(query.get("X-Goog-Expires", "0")) < time.time()

    # -------------------------------------------------------------------- verbs
    def do_GET(self) -> None:
        parts, q = self._route()
        g = self.gcs
        if parts[:3] == ["storage", "v1", "b"] and len(parts) == 6 and parts[4] == "o":
            return self._json_api_get(parts[3], parts[5], q)
        if parts[:3] == ["storage", "v1", "b"] and len(parts) == 5:
            return self._send(403, b"listing is not allowed")
        if parts[0] == "download" and len(parts) == 3:
            bucket, name = parts[1], parts[2]
            obj = g.objects.get((bucket, name))
            gen = q.get("generation", "")
            want = g._sig("GET", bucket, name, q.get("X-Goog-Expires", ""), gen)
            if not hmac.compare_digest(want, q.get("X-Goog-Signature", "")) or self._expired(q):
                return self._send(403, b"bad signature")
            if obj is None or str(obj.generation) != gen:
                return self._send(404, b"no such generation")
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                lo, _, hi = rng[6:].partition("-")
                start = int(lo)
                end = min(int(hi) if hi else len(obj.data) - 1, len(obj.data) - 1)
                body = obj.data[start : end + 1]
                return self._send(
                    206, body, {"Content-Range": f"bytes {start}-{end}/{len(obj.data)}"}
                )
            return self._send(200, obj.data)
        return self._send(404)

    def _json_api_get(self, bucket: str, name: str, q: dict[str, str]) -> None:
        g = self.gcs
        if self.headers.get("Authorization") != f"Bearer {g.exec_token}":
            return self._send(401, b"unauthenticated")
        if bucket != g.buckets.bootstrap:
            return self._send(403, b"the exec principal may only read the bootstrap bucket")
        obj = g.objects.get((bucket, name))
        if obj is None:
            return self._send(404, b"not found")
        if q.get("alt") == "media":
            return self._send(200, obj.data, {"x-goog-generation": str(obj.generation)})
        body = json.dumps(obj.meta(bucket, name).to_gcs_resource()).encode()
        return self._send(200, body, {"Content-Type": "application/json"})

    def do_DELETE(self) -> None:
        parts, q = self._route()
        g = self.gcs
        if not (parts[:3] == ["storage", "v1", "b"] and len(parts) == 6 and parts[4] == "o"):
            return self._send(404)
        bucket, name = parts[3], parts[5]
        if self.headers.get("Authorization") != f"Bearer {g.exec_token}":
            return self._send(401)
        if bucket != g.buckets.bootstrap:
            return self._send(403)
        with g._lock:
            obj = g.objects.get((bucket, name))
            if obj is None or ("generation" in q and str(obj.generation) != q["generation"]):
                return self._send(404)
            del g.objects[(bucket, name)]
        return self._send(204)

    def do_PUT(self) -> None:
        parts, q = self._route()
        g = self.gcs
        body = self._body()
        if parts[0] != "upload" or len(parts) != 3:
            return self._send(404)
        bucket, name = parts[1], parts[2]
        signed = {
            k: self.headers.get(k, "")
            for k in ("x-goog-if-generation-match", "x-goog-content-length-range")
        }
        want = g._sig(
            "PUT", bucket, name, q.get("X-Goog-Expires", ""), json.dumps(signed, sort_keys=True)
        )
        if not hmac.compare_digest(want, q.get("X-Goog-Signature", "")) or self._expired(q):
            return self._send(403, b"bad signature")
        lo, _, hi = signed["x-goog-content-length-range"].partition(",")
        if not int(lo) <= len(body) <= int(hi):
            return self._send(400, b"content length out of range")
        with g._lock:
            if signed["x-goog-if-generation-match"] == "0" and (bucket, name) in g.objects:
                return self._send(412, b"precondition failed")
        meta = g.put(bucket, name, body, self.headers.get("content-type"))
        return self._send(200, json.dumps(meta.to_gcs_resource()).encode())

    def do_POST(self) -> None:
        parts, _q = self._route()
        g = self.gcs
        body = self._body()
        if len(parts) != 1:
            return self._send(404)
        bucket = parts[0]
        ctype = self.headers.get("Content-Type", "")
        if "boundary=" not in ctype:
            return self._send(400, b"not multipart")
        fields, file_data, file_type = _parse_multipart(body, ctype.split("boundary=", 1)[1])
        policy = fields.get("policy", "")
        if not hmac.compare_digest(g._sig("POST", policy), fields.get("x-goog-signature", "")):
            return self._send(403, b"bad policy signature")
        doc = json.loads(base64.b64decode(policy))
        key = fields.get("key", "")
        if doc["bucket"] != bucket or not key.startswith(doc["prefix"]) or key == doc["prefix"]:
            return self._send(403, b"key outside the policy")
        if doc["expires"] < time.time():
            return self._send(403, b"policy expired")
        if not doc["min"] <= len(file_data) <= doc["max"]:
            return self._send(400, b"content length out of range")
        g.put(bucket, key, file_data, file_type)
        return self._send(204)


def _parse_multipart(body: bytes, boundary: str) -> tuple[dict[str, str], bytes, str | None]:
    fields: dict[str, str] = {}
    file_data = b""
    file_type: str | None = None
    delim = b"--" + boundary.encode()
    for part in body.split(delim)[1:]:
        if part.startswith(b"--"):
            break
        part = part[2:] if part.startswith(b"\r\n") else part
        head, _, content = part.partition(b"\r\n\r\n")
        content = content[:-2] if content.endswith(b"\r\n") else content
        headers = head.decode().split("\r\n")
        disp = next(h for h in headers if h.lower().startswith("content-disposition"))
        name = disp.split('name="', 1)[1].split('"', 1)[0]
        if name == "file":
            file_data = content
            for h in headers:
                if h.lower().startswith("content-type:"):
                    file_type = h.split(":", 1)[1].strip()
        else:
            fields[name] = content.decode()
    return fields, file_data, file_type
