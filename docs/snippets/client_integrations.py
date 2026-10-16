"""The v0.4 Python client: endpoints, scoring an endpoint into xarray, snippets, webhooks,
static plot renders and the GDPR export.

Runs offline (conformance/tests/test_docs_snippets.py): a stand-in for the API and the
storage service answers through ``httpx.MockTransport``. The stand-in really decodes the
inline XrJSON the client sends, scores it with a toy response and serves the result as a
CRC32C-checked ``score.zarr.zip``, so every call below is a real client call.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import numpy as np
import xarray as xr
from mapi_client import Client, ClientConfig
from mapi_sdk.wire import from_xrjson, write_datatree_zip
from mapi_spec.api import Endpoint, EndpointSchema, GdprExport, PlotRender, Run, WebhookCreated
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.snippets import SnippetContext, render_snippets
from mapi_spec.storage import crc32c, crc32c_to_base64
from mapi_spec.tokens import generate_token

BASE = "https://api.mapi.example"
STORAGE = "https://storage.mapi.example"
BLOBS: dict[str, bytes] = {}
SEEN: list[httpx.Request] = []
ROI = {"tv": 1.8, "search": 3.1}


def _example(model: Any, index: int = 0) -> dict[str, Any]:
    return copy.deepcopy(model.model_json_schema()["examples"][index])


def _link(name: str, data: bytes, ctype: str) -> dict[str, Any]:
    BLOBS[name] = data
    return {
        "name": name, "url": f"{STORAGE}/o/{name}?X-Goog-Signature=x", "size": len(data),
        "crc32c": crc32c_to_base64(crc32c(data)), "generation": 1, "content_type": ctype,
        "expires_at": "2099-01-01T00:00:00Z",
    }  # fmt: skip


def _score(body: dict[str, Any]) -> dict[str, Any]:
    """Toy model: revenue = sum over channels of ROI x spend (+ a 90% interval)."""
    spend = from_xrjson(body["data"])["spend_local"]
    roi = xr.DataArray(list(ROI.values()), dims=["channel"], coords={"channel": list(ROI)})
    mean = (spend * roi).sum("channel")
    summary = xr.concat([mean, mean * 0.85, mean * 1.15], dim="stat").assign_coords(
        stat=["mean", "hdi_90_lower", "hdi_90_upper"]
    )
    tree = xr.DataTree.from_dict(
        {"/": xr.Dataset(attrs={"representation": "draws"}),
         "/summary": xr.Dataset({"revenue_local": summary})}
    )  # fmt: skip
    blob = write_datatree_zip(tree)
    assert blob is not None
    run = _example(Run) | {"kind": "score", "state": "succeeded", "progress": None}
    return {"done": True, "run": run, "outputs": [_link("score.zarr.zip", blob, "application/zip")]}


def _api(request: httpx.Request) -> httpx.Response:
    """A stand-in for the v0.4 API (only what this page calls)."""
    SEEN.append(request)
    path, method = request.url.path, request.method
    alias = "/v1/endpoints/geo-mmm-prod"
    if path == alias and method == "PUT":
        return httpx.Response(200, json=_example(Endpoint))
    if path == f"{alias}/schema":
        return httpx.Response(200, json=_example(EndpointSchema))
    if path == f"{alias}/score":
        return httpx.Response(200, json=_score(json.loads(request.content)))
    if path == f"{alias}/snippets":
        ctx = SnippetContext(base_url=BASE, project="acme/growth", alias="geo-mmm-prod")
        langs = request.url.params.get_list("language") or None
        return httpx.Response(200, json=render_snippets(ctx, langs).model_dump(mode="json"))
    if path == "/v1/webhooks" and method == "POST":
        return httpx.Response(201, json=_example(WebhookCreated))
    if path == "/v1/plots:render":
        png = b"\x89PNG\r\n\x1a\n" + bytes(64)
        render = _example(PlotRender) | {"format": "png"}
        render["outputs"] = [_link("renders/mmm.response_curves.png", png, "image/png")]
        return httpx.Response(200, json=render)
    if path.startswith("/v1/tenants/acme/exports"):
        archive = b"PK\x05\x06" + bytes(18)
        BLOBS["export.zip"] = archive
        export = _example(GdprExport) | {
            "tenant_id": "acme", "size_bytes": len(archive),
            "download_url": f"{STORAGE}/o/export.zip?X-Goog-Signature=x",
        }  # fmt: skip
        return httpx.Response(202 if method == "POST" else 200, json=export)
    return httpx.Response(404, json=Problem.from_code(ErrorCode.NOT_FOUND).model_dump())


def _storage(request: httpx.Request) -> httpx.Response:
    assert "authorization" not in request.headers  # signed links never get API credentials
    return httpx.Response(200, content=BLOBS[request.url.path.removeprefix("/o/")])


config = ClientConfig(base_url=BASE, token=str(generate_token()), project="acme/growth")
api = Client(
    config=config,
    transport=httpx.MockTransport(_api),
    storage_transport=httpx.MockTransport(_storage),
)

# --8<-- [start:promote]
endpoint = api.endpoints.promote(
    "geo-mmm-prod",
    "model://growth/eu-geo-mmm@v3",
    expected_revision=3,  # 409 CONFLICT if someone else promoted in between
)
print(endpoint.ref, "->", endpoint.model_ref, endpoint.gate.outcome)
# --8<-- [end:promote]

# --8<-- [start:score]
geo_mmm = api.endpoint("geo-mmm-prod")
schema = geo_mmm.schema()
print(sorted(schema.snapshot.input_schema.vars))

plan = xr.Dataset(
    {"spend_local": (("market", "channel", "date"), np.array([[[1200.0], [5300.0]]]))},
    coords={"market": ["de"], "channel": ["search", "tv"], "date": ["2025-12-30"]},
)
result = geo_mmm.score(plan, wait=60)  # inline XrJSON in, xr.Dataset out
print(result["revenue_local"].sel(stat="mean").values)
# --8<-- [end:score]
assert result["revenue_local"].sel(stat="mean").values.tolist() == [[1200 * 3.1 + 5300 * 1.8]]
sent = json.loads(next(r for r in SEEN if r.url.path.endswith("/score")).content)
assert sent["data"]["coords"]["channel"]["data"] == ["search", "tv"]

# --8<-- [start:snippets]
python = api.endpoints.snippets("geo-mmm-prod", ["python"]).items[0]
print(python.code.splitlines()[2])
# --8<-- [end:snippets]

# --8<-- [start:webhook]
created = api.webhooks.create(
    "https://hooks.example.com/mapi", ["model.version.ready", "endpoint.promoted"]
)
signing_secret = created.secret  # the only copy: store it with the receiver
print(created.webhook.id, created.webhook.secret_hint)
# --8<-- [end:webhook]
assert signing_secret not in repr(created)

with TemporaryDirectory() as tmp:
    # --8<-- [start:render]
    render = api.plots.render(
        model_id="mdl_01jaf3c1d2e3f4g5h6j7k8m9n0",
        version=3,
        plot_id="mmm.response_curves",
        format="png",
        width=1200,
    )
    path = api.plots.download_render(render, Path(tmp) / "response_curves.png")
    # --8<-- [end:render]
    assert path.read_bytes().startswith(b"\x89PNG")

    # --8<-- [start:export]
    export = api.tenants.export("acme")
    archive = api.tenants.download_export("acme", export.id, Path(tmp) / "acme-export.zip")
    # --8<-- [end:export]
    assert archive.stat().st_size == 22

api.close()
