"""Share a model's chart: save it, create an embed link, check it, render a PNG, revoke.

Included by ``docs/tutorials/embed-a-chart.md``; runs offline
(conformance/tests/test_docs_snippets.py). A stand-in for the API answers through
``httpx.MockTransport`` with the platform's own DTOs; its public embed view carries a real
``mmm.response_curves`` recipe, checked by the same model the API serves it with.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
from mapi_client import Client, ClientConfig
from mapi_plots.examples import example_inputs
from mapi_plots.recipes import get_recipe
from mapi_spec.api import EmbedView, PlotEmbed, PlotEmbedCreated, PlotRender, SavedPlot
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.storage import crc32c, crc32c_to_base64
from mapi_spec.tokens import generate_token

BASE = "https://api.mapi.example"
STORAGE = "https://storage.mapi.example"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)
STATE: dict[str, Any] = {"embeds": []}


def _example(model: Any) -> dict[str, Any]:
    return copy.deepcopy(model.model_json_schema()["examples"][0])


def _view() -> dict[str, Any]:
    data, options = example_inputs()["mmm.response_curves"]
    plot = get_recipe("mmm.response_curves").render(data, **options)
    view = EmbedView(title="EU response curves (v3)", caption_plain=plot.caption_plain,
                     plot=plot, generated_at="2026-10-15T09:40:00Z",
                     expires_at="2026-11-14T09:40:00Z")  # fmt: skip
    return view.model_dump(mode="json")


def _api(request: httpx.Request) -> httpx.Response:
    """A stand-in for the plot-sharing endpoints of the v0.4 API."""
    path, method = request.url.path, request.method
    plot = _example(SavedPlot)
    embeds = f"/v1/plots/{plot['id']}/embeds"
    if path == "/v1/plots" and method == "POST":
        return httpx.Response(201, json=plot | {"title": json.loads(request.content)["title"]})
    if path == embeds and method == "POST":
        body = json.loads(request.content)
        created = _example(PlotEmbedCreated)
        created["embed"] |= {"label": body.get("label"), "views": 0,
                             "allowed_origins": body.get("allowed_origins", [])}  # fmt: skip
        created["url"] = f"https://plots.mapi.example/embed/{created['token']}"
        STATE["embeds"].append(created["embed"])
        return httpx.Response(201, json=created)
    if path == embeds and method == "GET":
        return httpx.Response(200, json={"items": STATE["embeds"], "next_cursor": None})
    if path.startswith(f"{embeds}/") and method == "DELETE":
        STATE["embeds"] = [e | {"status": "revoked"} for e in STATE["embeds"]]
        return httpx.Response(204)
    if path.startswith("/v1/embeds/"):
        assert "authorization" not in request.headers  # the public view needs no credential
        if any(e["status"] == "revoked" for e in STATE["embeds"]):
            return httpx.Response(410, json=Problem.from_code(ErrorCode.LINK_EXPIRED).model_dump())
        return httpx.Response(200, json=_view())
    if path == "/v1/plots:render":
        render = _example(PlotRender) | {"format": "png"}
        render["outputs"] = [{
            "name": "renders/mmm.response_curves.png", "url": f"{STORAGE}/o/plot.png",
            "size": len(PNG), "crc32c": crc32c_to_base64(crc32c(PNG)), "generation": 1,
            "content_type": "image/png", "expires_at": "2099-01-01T00:00:00Z",
        }]  # fmt: skip
        return httpx.Response(200, json=render)
    return httpx.Response(404, json=Problem.from_code(ErrorCode.NOT_FOUND).model_dump())


def _storage(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PNG)


api = Client(
    config=ClientConfig(base_url=BASE, token=str(generate_token()), project="acme/growth"),
    transport=httpx.MockTransport(_api),
    storage_transport=httpx.MockTransport(_storage),
)

# --8<-- [start:save]
saved = api.plots.save(
    "EU response curves (v3)",
    model_id="mdl_01jaf3c1d2e3f4g5h6j7k8m9n0",  # the model version that drew the plot
    version=3,
    plot_id="mmm.response_curves",  # one of its plots: a platform recipe
)
print(saved.id, "embeddable" if saved.embeddable else "not embeddable")
# --8<-- [end:save]

# --8<-- [start:link]
link = api.plots.create_embed(
    saved.id,
    label="Weekly marketing review",
    expires_in_days=90,
    allowed_origins=["https://lookerstudio.google.com"],  # leave out: any site may frame it
)
print(link.url)  # shown once: paste it into the tool now
iframe = (f'<iframe src="{link.url}" width="900" height="520" style="border:0" '
          f'loading="lazy" title="{saved.title}"></iframe>')  # fmt: skip
# --8<-- [end:link]
assert link.url.startswith("https://plots.mapi.example/embed/mpe_")
assert link.token not in repr(link.embed)

# --8<-- [start:check]
view = api.plots.embed(link.token)  # what the link shows, fetched without credentials
print(view.title, "| expires", view.expires_at.date(), "|", view.plot.provenance.recipe)
for embed in api.plots.embeds(saved.id):
    print(embed.display, embed.label, embed.status, embed.views, "views")
# --8<-- [end:check]
assert view.plot.provenance.recipe is not None and view.plot.provenance.recipe.startswith("mmm.")

# --8<-- [start:png]
render = api.plots.render(saved_plot_id=saved.id, format="png", width=1200, height=600)
png = api.plots.download_render(render, Path("eu-response-curves.png"))  # CRC32C-checked
# --8<-- [end:png]
assert png.read_bytes().startswith(b"\x89PNG")

# --8<-- [start:revoke]
api.plots.revoke_embed(saved.id, link.embed.id)  # dashboards showing it stop on next load
# --8<-- [end:revoke]
try:
    api.plots.embed(link.token)
except Exception as exc:  # MapiError: 410 LINK_EXPIRED
    assert getattr(exc, "code", None) is ErrorCode.LINK_EXPIRED, exc
else:
    raise AssertionError("a revoked link must answer 410")
api.close()
