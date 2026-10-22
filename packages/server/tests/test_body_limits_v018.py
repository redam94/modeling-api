"""Every v0.18 ``POST`` body is capped before it is parsed (plan I-5): library paths at
320 KiB, ``:validate`` and ``charts:preview`` at 64 KiB, onboarding, samples, recipe previews
and the catalog refresh at 4 KiB. Driven by :data:`mapi_spec.api.V018_ENDPOINTS`, so a new
``POST`` row without a cap fails here."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mapi_server.api.problems import JsonBodyLimit
from mapi_server.app import create_app
from mapi_server.context import ServerContext
from mapi_spec.api import V018_ENDPOINTS

pytestmark = pytest.mark.integration

KIB = 1024
ITEM = "lib_" + "0" * 26
_SAMPLE_PARAMS = {
    "item_id": ITEM,
    "revision": "1",
    "tenant_id": "acme",
    "sample_id": "geo_mmm",
    "plot_id": "core.ppc_fan",
    "version": "1.0.0",
    "run_id": "run_" + "0" * 26,
}
_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")


def _caps(app: FastAPI, path: str) -> list[int]:
    out = []
    for mw in app.user_middleware:
        if mw.cls is JsonBodyLimit:
            kwargs: dict[str, Any] = dict(mw.kwargs)
            if any(re.fullmatch(p, path) for p in kwargs["paths"]):
                out.append(int(kwargs["max_bytes"]))
    return out


def _path(template: str) -> str:
    return _PARAM.sub(lambda mt: _SAMPLE_PARAMS[mt.group(1)], template)


def _planned(path: str) -> int:
    if path in ("/v1/library:validate", "/v1/library/charts:preview"):
        return 64 * KIB
    if path.startswith("/v1/library"):
        return 320 * KIB
    return 4 * KIB


def test_every_v018_post_route_is_capped(ctx: ServerContext) -> None:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    posts = [e for e in V018_ENDPOINTS if e.method == "POST"]
    assert len(posts) >= 14
    for e in posts:
        path = _path(e.path)
        caps = _caps(app, path)
        assert caps, f"POST {e.path} ({e.operation_id}) has no JsonBodyLimit"
        assert min(caps) == _planned(path), (e.operation_id, caps)


async def test_oversized_bodies_are_refused_before_parsing(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    for path, cap in (
        ("/v1/library", 320 * KIB),
        (f"/v1/library/{ITEM}/revisions", 320 * KIB),
        ("/v1/library:validate", 64 * KIB),
        ("/v1/library/charts:preview", 64 * KIB),
        ("/v1/tenants/acme/samples/geo_mmm:install", 4 * KIB),
    ):
        body = b'{"text": "' + b"x" * cap + b'"}'
        r = await client.post(path, content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 413, (path, r.status_code)
        assert r.json()["code"] == "PAYLOAD_TOO_LARGE"
