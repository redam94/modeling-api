"""Every v0.19 body is capped at 8 KiB before it is parsed (plan I-5): claims, tickets,
``:complete``, review and yank, the plugin settings and the uploader attestations. Driven by
:data:`mapi_spec.api.V019_ENDPOINTS`, so a new write route without a cap fails here. (The
wheel itself never passes through the API: it goes to the storage service under a POST
policy.)"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mapi_server.api.problems import JsonBodyLimit
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_spec.api import V019_ENDPOINTS

pytestmark = pytest.mark.integration

KIB = 1024
_PARAM = re.compile(r"\{([^}:]+)\}")
_SAMPLE = {
    "plugin_id": "plg_" + "0" * 26,
    "version_id": "plv_" + "0" * 26,
    "upload_id": "pup_" + "0" * 26,
    "plot_id": "t_abcdef_acme.chart",
    "tenant_id": "acme",
    "user_id": "usr_" + "0" * 26,
}
WRITES = ("POST", "PUT", "PATCH")


def _caps(app: FastAPI, method: str, path: str) -> list[int]:
    out = []
    for mw in app.user_middleware:
        if mw.cls is JsonBodyLimit:
            kwargs: dict[str, Any] = dict(mw.kwargs)
            methods = kwargs.get("methods", ("POST",))
            if method in methods and any(re.fullmatch(p, path) for p in kwargs["paths"]):
                out.append(int(kwargs["max_bytes"]))
    return out


def _path(template: str) -> str:
    return _PARAM.sub(lambda mt: _SAMPLE[mt.group(1)], template)


def test_every_v019_write_route_is_capped(ctx: ServerContext) -> None:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    writes = [e for e in V019_ENDPOINTS if e.method in WRITES]
    assert len(writes) >= 9
    for e in writes:
        caps = _caps(app, e.method, _path(e.path))
        assert caps, f"{e.method} {e.path} ({e.operation_id}) has no JsonBodyLimit"
        assert min(caps) == 8 * KIB, (e.operation_id, caps)


async def test_oversized_bodies_are_refused_before_parsing(client: httpx.AsyncClient) -> None:
    body = b'{"title": "' + b"x" * (8 * KIB) + b'"}'
    for e in V019_ENDPOINTS:
        if e.method not in WRITES:
            continue
        r = await client.request(
            e.method, _path(e.path), content=body, headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 413, (e.operation_id, r.status_code)
        assert r.json()["code"] == "PAYLOAD_TOO_LARGE"
