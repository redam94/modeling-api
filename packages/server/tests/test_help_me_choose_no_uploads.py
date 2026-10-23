"""Help me choose never ranks uploaded code (v0.19, WP7, default d).

The catalog lists and the About cards follow the caller's tenant catalog view (an approved
upload is there for the whole tenant), but ``GET /v1/catalog/recommendations`` ranks the
base catalog of the image only, whatever the answers, and the diagnostics never show
uploaded text.
"""

from __future__ import annotations

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_spec.about import CHOOSER_QUESTIONS
from test_catalog_view import NS_A, seed_version

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


async def test_recommendations_never_offer_an_upload(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    ana = await make_user(ctx, "ana@default.test")
    await grant_role(ctx, ana, "default", None, "admin")
    await seed_version(ctx, uploaded_by=ana.id)
    me = await session_caller(ctx, client, ana, tenant_id="default", project_id="default")
    # the upload is in the caller's catalog and its About card is listed ...
    models = (await me.get("/v1/catalog/models", params={"limit": 200})).json()["items"]
    assert f"{NS_A}.store_mean" in {e["manifest"]["id"] for e in models}
    abouts = (await me.get("/v1/catalog/abouts", params={"limit": 200, "kind": "model"})).json()
    assert f"{NS_A}.store_mean" in {a["id"] for a in abouts["items"]}
    # ... but no answer, and no kind, ever ranks it
    seen: set[str] = set()
    queries: list[dict[str, str]] = [{}]
    for q in CHOOSER_QUESTIONS:
        queries += [{q.id: choice.value} for choice in q.choices]
    for kind in (None, "model", "step", "plot"):
        for query in queries:
            params = {**query, "limit": "50", **({"kind": kind} if kind else {})}
            r = await me.get("/v1/catalog/recommendations", params=params)
            assert r.status_code == 200, r.text
            seen |= {item["id"] for item in r.json()["items"]}
    assert seen, "the chooser ranked nothing at all"
    assert not {i for i in seen if i.startswith("t_")}
    r = await me.get("/v1/catalog/diagnostics")
    assert r.status_code == 200 and NS_A not in r.text
