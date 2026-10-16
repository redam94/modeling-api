"""v0.4 resources (guided, endpoints, webhooks, plots, trust) against a respx-mocked API,
``Client.endpoint(alias)`` and the generated Python snippet run as-is."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mapi_client import AsyncClient, Client, ClientConfig, RemoteEndpoint
from mapi_spec.api import (
    Endpoint,
    EndpointRevision,
    EndpointSchema,
    EndpointValidation,
    GdprExport,
    GuidedDraft,
    GuidedTemplate,
    PlotEmbed,
    PlotEmbedCreated,
    PlotRender,
    PriorTranslation,
    RoleSuggestions,
    Run,
    SavedPlot,
    ScoreRequest,
    SignupAccepted,
    SignupReview,
    SnippetSet,
    TenantDeletion,
    VerificationResult,
    VerificationStatus,
    Webhook,
    WebhookCreated,
    WebhookDelivery,
)
from mapi_spec.errors import ErrorCode, IntegrityFailure, MapiError, Problem
from mapi_spec.sharing import EmbedView
from mapi_spec.snippets import SnippetContext, render_snippet
from mapi_spec.storage import crc32c, crc32c_to_base64

BASE = "http://api.test"
V1 = f"{BASE}/v1"
STORAGE = "http://storage.test"
HEADER = "X-Mapi-Project"
Ex = Callable[..., dict[str, Any]]
HAS_XARRAY = importlib.util.find_spec("mapi_sdk") is not None


def page(items: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": cursor}


def problem(code: ErrorCode, detail: str, status: int) -> httpx.Response:
    body = Problem.from_code(code, detail).model_dump(mode="json")
    return httpx.Response(status, json=body)


def body_of(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


@pytest.fixture
def scoped(api: respx.MockRouter) -> Iterator[Client]:
    cfg = ClientConfig(
        base_url=BASE, project="acme/growth", token="t0k", allow_insecure_token=True, max_retries=0
    )
    with Client(config=cfg, sleep=lambda s: None) as c:
        yield c


# ===================================================================================== guided


def test_guided_templates_detect_draft_compile(
    scoped: Client, api: respx.MockRouter, example: Ex
) -> None:
    tpl = example(GuidedTemplate)
    first = api.get(f"{V1}/guided/templates", params={"cursor": "c2"}).respond(
        200, json=page([tpl])
    )
    api.get(f"{V1}/guided/templates").respond(200, json=page([tpl], "c2"))
    assert [t.id for t in scoped.guided.templates()] == [tpl["id"], tpl["id"]]
    assert HEADER not in first.calls.last.request.headers  # templates are not per project
    api.get(f"{V1}/guided/templates/geo_mmm").respond(200, json=tpl)
    assert scoped.guided.template("geo_mmm").id == tpl["id"]

    detect = api.post(f"{V1}/guided/detect-roles").respond(200, json=example(RoleSuggestions))
    got = scoped.guided.detect_roles(
        "geo_mmm", columns=[{"name": "revenue_eur", "dtype": "number", "n_rows": 10}]
    )
    assert got.questions[0].question_id == "kpi"
    assert body_of(detect)["columns"][0]["name"] == "revenue_eur"
    assert detect.calls.last.request.headers[HEADER] == "acme/growth"
    with pytest.raises(ValueError):
        scoped.guided.detect_roles("geo_mmm")  # neither a dataset nor columns

    draft = api.post(f"{V1}/guided/drafts").respond(200, json=example(GuidedDraft))
    out = scoped.guided.draft(
        template_id="geo_mmm",
        name="eu-geo-mmm",
        dataset="eu_media_weekly",
        answers={"kpi": "revenue_eur"},
    )
    assert out.template_id == example(GuidedDraft)["template_id"]
    assert body_of(draft)["answers"] == {"kpi": "revenue_eur"}

    compiled = ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "endpoint://growth/geo-mmm-prod",
            "mode": "forecast",
            "horizon": {"dim": "time", "periods": 13},
        }
    ).model_dump(mode="json", by_alias=True, exclude_none=True)
    comp = api.post(f"{V1}/guided/requests:compile").respond(200, json=compiled)
    req = scoped.guided.compile(
        {"kind": "forecast", "model": "endpoint://growth/geo-mmm-prod", "periods": 13}
    )
    assert req.mode == compiled["mode"]
    assert body_of(comp)["kind"] == "forecast"


def test_prior_translation_waits_for_its_run(
    scoped: Client, api: respx.MockRouter, example: Ex
) -> None:
    done = example(PriorTranslation)
    running = {**done, "done": False, "result": None, "run": {**done["run"], "state": "running"}}
    post = api.post(f"{V1}/guided/prior-translations").respond(202, json=running)
    get = api.get(f"{V1}/guided/prior-translations/{done['run']['id']}").mock(
        side_effect=[httpx.Response(200, json=running), httpx.Response(200, json=done)]
    )
    got = scoped.guided.preview_priors(_business_priors(), spec_id="spc_1", revision=2)
    assert got.done and got.result is not None
    assert post.calls.last.request.headers["Idempotency-Key"]
    assert body_of(post)["spec_id"] == "spc_1" and get.call_count == 2


def _business_priors() -> dict[str, Any]:
    from mapi_spec.calibration import BusinessPriors

    return dict(BusinessPriors.model_json_schema()["examples"][0])


def test_failed_prior_translation_raises(
    scoped: Client, api: respx.MockRouter, example: Ex
) -> None:
    done = example(PriorTranslation)
    failed_run = {
        **done["run"],
        "state": "failed",
        "failure_class": "user",
        "problem": Problem.from_code(ErrorCode.BUSINESS_PRIORS_INVALID, "bad").model_dump(
            mode="json"
        ),
    }
    api.post(f"{V1}/guided/prior-translations").respond(
        200, json={**done, "done": True, "result": None, "run": failed_run}
    )
    with pytest.raises(MapiError) as info:
        scoped.guided.preview_priors(_business_priors(), spec_id="spc_1", revision=2)
    assert info.value.code is ErrorCode.BUSINESS_PRIORS_INVALID


# ================================================================================= endpoints


def test_endpoint_lifecycle(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    ep = example(Endpoint)
    api.get(f"{V1}/endpoints").respond(200, json=page([ep]))
    assert [e.alias for e in scoped.endpoints.list()] == ["geo-mmm-prod"]
    put = api.put(f"{V1}/endpoints/geo-mmm-prod").mock(
        side_effect=[
            problem(ErrorCode.OVERRIDE_REQUIRED, "the scorecard warns", 409),
            httpx.Response(200, json=ep),
        ]
    )
    with pytest.raises(MapiError) as info:
        scoped.endpoints.promote("geo-mmm-prod", "model://growth/eu-geo-mmm@v3")
    assert info.value.code is ErrorCode.OVERRIDE_REQUIRED
    got = scoped.endpoints.promote(
        "geo-mmm-prod",
        "model://growth/eu-geo-mmm@v3",
        override=True,
        reason="accepted by the CMO",
        expected_revision=3,
    )
    assert got.revision == ep["revision"]
    assert body_of(put) == {
        "model": "model://growth/eu-geo-mmm@v3",
        "override": True,
        "reason": "accepted by the CMO",
        "expected_revision": 3,
    }
    with pytest.raises(ValueError, match="reason"):
        scoped.endpoints.promote("geo-mmm-prod", "model://growth/x@v1", override=True)

    api.get(f"{V1}/endpoints/geo-mmm-prod/history").respond(
        200, json=page([example(EndpointRevision)])
    )
    assert scoped.endpoints.history("geo-mmm-prod").take(1)[0].action == "promote"
    rb = api.post(f"{V1}/endpoints/geo-mmm-prod/rollback").respond(200, json=ep)
    scoped.endpoints.rollback("geo-mmm-prod", "v3 over-forecasts", to_revision=2)
    assert body_of(rb) == {"reason": "v3 over-forecasts", "to_revision": 2}
    api.delete(f"{V1}/endpoints/geo-mmm-prod").respond(204)
    scoped.endpoints.delete("geo-mmm-prod")

    snip = api.get(f"{V1}/endpoints/geo-mmm-prod/snippets").respond(200, json=example(SnippetSet))
    scoped.endpoints.snippets("geo-mmm-prod", ["python", "curl"], base_url="https://a.example")
    assert snip.calls.last.request.url.query == (
        b"language=python&language=curl&base_url=https%3A%2F%2Fa.example"
    )


def _result(example: Ex, done: bool) -> dict[str, Any]:
    run = example(Run, kind="score", state="succeeded" if done else "running", progress=None)
    return {"done": done, "run": run, "outputs": []}


def test_endpoint_score_replays_its_key_until_done(
    scoped: Client, api: respx.MockRouter, example: Ex
) -> None:
    route = api.post(f"{V1}/endpoints/geo-mmm-prod/score").mock(
        side_effect=[
            httpx.Response(202, json=_result(example, False)),
            httpx.Response(202, json=_result(example, False)),
            httpx.Response(200, json=_result(example, True)),
        ]
    )
    res = scoped.endpoints.score(
        "geo-mmm-prod", {"mode": "forecast", "horizon": {"dim": "time", "periods": 8}}, wait=30
    )
    assert res.done and route.call_count == 3
    keys = {c.request.headers["Idempotency-Key"] for c in route.calls}
    assert len(keys) == 1
    assert route.calls.last.request.headers["Prefer"] == "wait=30"
    assert body_of(route)["horizon"] == {"dim": "time", "periods": 8}


def test_endpoint_score_times_out(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    api.post(f"{V1}/endpoints/a/score").respond(202, json=_result(example, False))
    with pytest.raises(TimeoutError):
        scoped.endpoints.score("a", wait=0, timeout=0)


def test_endpoint_schema_and_validate(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    schema = example(EndpointSchema)
    api.get(f"{V1}/endpoints/geo-mmm-prod/schema").respond(200, json=schema)
    got = scoped.endpoints.schema("geo-mmm-prod")
    assert got.snapshot.horizon_dim == "time"
    val = api.post(f"{V1}/endpoints/geo-mmm-prod/validate").respond(
        200, json=example(EndpointValidation)
    )
    out = scoped.endpoints.validate("geo-mmm-prod", got.example)
    assert not out.valid and out.issues[0].code is ErrorCode.MISSING_FUTURE_COVARIATE
    assert body_of(val)["mode"] == "forecast"


@pytest.mark.skipif(not HAS_XARRAY, reason="needs the xarray extra")
def test_remote_endpoint_scores_a_dataset(
    scoped: Client, fake_endpoint: Callable[..., Any]
) -> None:
    import xarray as xr

    def double(ds: xr.Dataset) -> xr.Dataset:
        return xr.Dataset({"revenue": (ds["spend"] * 2).sum("channel")})

    ep = fake_endpoint(double, pending=1)
    remote = scoped.endpoint("plan-prod")
    assert isinstance(remote, RemoteEndpoint) and "plan-prod" in repr(remote)
    ds = xr.Dataset(
        {"spend": (("market", "channel"), [[1.0, 2.0], [3.0, 4.0]])},
        coords={"market": ["de", "fr"], "channel": ["tv", "search"]},
    )
    out = remote.score(ds, wait=5)
    assert out["revenue"].sel(stat="mean").values.tolist() == [6.0, 14.0]
    assert out.attrs["representation"] == "draws"
    sent = ep.bodies[-1]
    assert sent["data"]["coords"]["market"]["data"] == ["de", "fr"]
    assert ep.score_calls == 2  # 202, then the same key collected the result
    draws = remote.score(ds, group="draws", output={"draws": 4})
    assert draws.sizes["sample"] == 4
    with pytest.raises(ValueError, match="no group 'delta/summary'"):
        remote.score(ds, group="delta/summary")
    assert remote.validate(ds).valid
    assert ep.bodies[-1]["data"]["data_vars"]["spend"]["dims"] == ["market", "channel"]
    assert remote.schema().alias == "plan-prod"


def test_python_snippet_runs_against_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Python snippet ``GET …/snippets`` returns is runnable as generated."""
    base = "https://api.example.com"
    payload = {"mode": "forecast", "horizon": {"dim": "time", "periods": 8}}
    ctx = SnippetContext(
        base_url=base, project="acme/growth", alias="geo-mmm-prod", payload=payload
    )
    code = render_snippet("python", ctx).code
    run = Run.model_json_schema()["examples"][0] | {"state": "succeeded", "progress": None}
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{base}/v1/endpoints/geo-mmm-prod/score").respond(
            200, json={"done": True, "run": run, "outputs": []}
        )
        monkeypatch.setenv("MAPI_TOKEN", "snippet-token")
        printed: list[tuple[Any, ...]] = []
        scope: dict[str, Any] = {"print": lambda *a: printed.append(a)}
        exec(compile(code, "<python snippet>", "exec"), scope)
        scope["api"].close()
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer snippet-token"
    assert request.headers[HEADER] == "acme/growth"
    assert request.headers["Prefer"] == "wait=60"
    assert json.loads(request.content)["horizon"] == {"dim": "time", "periods": 8}
    assert printed == [("succeeded", [])]


@pytest.mark.skipif(not HAS_XARRAY, reason="needs the xarray extra")
@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "forecast", "horizon": {"dim": "time", "periods": 8}, "data": None},
        {"mode": "predict", "timeout": "10m"},  # a field named like a score() keyword
    ],
    ids=["remote-endpoint", "endpoints-score"],
)
def test_notebook_snippet_runs_against_the_api(
    payload: dict[str, Any],
    fake_endpoint: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notebook cell ``GET …/snippets`` returns runs as generated and ends in a dataset."""
    import xarray as xr

    base = "https://api.example.com"
    ctx = SnippetContext(base_url=base, project="acme/growth", alias="plan-prod", payload=payload)
    code = render_snippet("notebook", ctx).code
    monkeypatch.setenv("MAPI_TOKEN", "snippet-token")
    scope: dict[str, Any] = {}
    with respx.mock(base_url=base, assert_all_called=False) as router:
        ep = fake_endpoint(
            lambda _ds: xr.Dataset({"y_hat": ("time", [1.0, 2.0])}), router=router, base_url=base
        )
        exec(compile(code, "<notebook snippet>", "exec"), scope)
        scope["api"].close()
    ds = scope["ds"]
    assert isinstance(ds, xr.Dataset)
    assert ds["y_hat"].sel(stat="mean").values.tolist() == [1.0, 2.0]
    sent = ep.bodies[-1]
    assert {k: v for k, v in sent.items() if k in payload} == {
        k: v for k, v in payload.items() if v is not None
    }
    assert ep.headers[-1]["authorization"] == "Bearer snippet-token"
    assert ep.headers[-1]["x-mapi-project"] == "acme/growth"


# ================================================================================== webhooks


def test_webhooks(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    created = example(WebhookCreated)
    post = api.post(f"{V1}/webhooks").respond(201, json=created)
    got = scoped.webhooks.create("https://hooks.example.com/mapi", ["run.succeeded"])
    assert got.secret.startswith("whsec_") and "whsec_b3" not in repr(got)
    assert body_of(post) == {
        "url": "https://hooks.example.com/mapi",
        "events": ["run.succeeded"],
        "active": True,
    }
    with pytest.raises(ValueError):
        scoped.webhooks.create("http://hooks.example.com", ["run.succeeded"])
    with pytest.raises(ValueError):
        scoped.webhooks.create("https://hooks.example.com", ["run.exploded"])
    wid = created["webhook"]["id"]
    api.get(f"{V1}/webhooks").respond(200, json=page([example(Webhook)]))
    assert scoped.webhooks.list().take(1)[0].id == wid
    api.get(f"{V1}/webhooks/{wid}").respond(200, json=example(Webhook))
    assert scoped.webhooks.get(wid).secret_hint.startswith("whsec_")
    patch = api.patch(f"{V1}/webhooks/{wid}").respond(200, json=example(Webhook))
    scoped.webhooks.update(wid, active=True)
    assert body_of(patch) == {"active": True}
    with pytest.raises(ValueError):
        scoped.webhooks.update(wid)  # nothing to change
    rot = api.post(f"{V1}/webhooks/{wid}:rotate-secret").mock(
        side_effect=[httpx.ConnectError("lost"), httpx.Response(200, json=created)]
    )
    with pytest.raises(Exception, match="lost"):
        scoped.webhooks.rotate_secret(wid)  # never resent: it would retire the new secret
    assert rot.call_count == 1
    dl = api.get(f"{V1}/webhooks/{wid}/deliveries").respond(
        200, json=page([example(WebhookDelivery)])
    )
    assert scoped.webhooks.deliveries(wid, status="retrying").take(1)[0].attempt == 2
    assert dl.calls.last.request.url.params["status"] == "retrying"
    did = example(WebhookDelivery)["id"]
    api.post(f"{V1}/webhooks/{wid}/deliveries/{did}:replay").respond(
        202, json=example(WebhookDelivery)
    )
    assert scoped.webhooks.replay(wid, did).id == did
    api.delete(f"{V1}/webhooks/{wid}").respond(204)
    scoped.webhooks.delete(wid)


# ===================================================================================== plots


def _png_link(data: bytes) -> dict[str, Any]:
    return {
        "name": "renders/mmm.response_curves.png",
        "url": f"{STORAGE}/renders/plot.png?sig=1",
        "size": len(data),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "generation": 1,
        "content_type": "image/png",
        "expires_at": "2099-01-01T00:00:00Z",
    }


def test_render_waits_and_downloads(
    scoped: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    done = example(PlotRender) | {"format": "png", "outputs": [_png_link(png)]}
    running = done | {"done": False, "outputs": [], "run": {**done["run"], "state": "running"}}
    route = api.post(f"{V1}/plots:render").mock(
        side_effect=[httpx.Response(202, json=running), httpx.Response(200, json=done)]
    )
    render = scoped.plots.render(saved_plot_id="plt_01jaf3h1j2k3m4n5p6q7r8s9t0", wait=10)
    assert render.done and route.call_count == 2
    assert len({c.request.headers["Idempotency-Key"] for c in route.calls}) == 1
    assert body_of(route)["saved_plot_id"] == "plt_01jaf3h1j2k3m4n5p6q7r8s9t0"
    dl = api.get(f"{STORAGE}/renders/plot.png").respond(200, content=png)
    assert scoped.plots.download_render(render) == png
    path = scoped.plots.download_render(render, tmp_path / "plot.png")
    assert path.read_bytes() == png
    assert "authorization" not in dl.calls.last.request.headers
    corrupt = done | {"outputs": [_png_link(png) | {"crc32c": "AAAAAA=="}]}
    with pytest.raises(IntegrityFailure):
        scoped.plots.download_render(PlotRender.model_validate(corrupt))
    with pytest.raises(ValueError):
        scoped.plots.render(model_id="mdl_1", version=1)  # a model plot needs its plot_id


def test_saved_plots_and_embeds(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    saved = example(SavedPlot)
    post = api.post(f"{V1}/plots").respond(201, json=saved)
    scoped.plots.save("Response curves", model_id="mdl_1", version=3, plot_id="mmm.curves")
    assert body_of(post)["source"] == {"model_id": "mdl_1", "version": 3, "plot_id": "mmm.curves"}
    api.get(f"{V1}/plots").respond(200, json=page([saved]))
    assert scoped.plots.saved().take(1)[0].id == saved["id"]
    api.get(f"{V1}/plots/{saved['id']}").respond(200, json=saved)
    assert scoped.plots.get_saved(saved["id"]).title == saved["title"]
    created = example(PlotEmbedCreated)
    emb = api.post(f"{V1}/plots/{saved['id']}/embeds").respond(201, json=created)
    got = scoped.plots.create_embed(
        saved["id"], expires_in_days=7, allowed_origins=["https://bi.example"]
    )
    assert got.token == created["token"]
    assert body_of(emb) == {"expires_in_days": 7, "allowed_origins": ["https://bi.example"]}
    api.get(f"{V1}/plots/{saved['id']}/embeds").respond(200, json=page([example(PlotEmbed)]))
    assert scoped.plots.embeds(saved["id"]).take(1)[0].status == "active"
    api.delete(f"{V1}/plots/{saved['id']}/embeds/emb_1").respond(204)
    scoped.plots.revoke_embed(saved["id"], "emb_1")
    view = api.get(f"{V1}/embeds/{created['token']}").respond(200, json=example(EmbedView))
    scoped.plots.embed(created["token"])
    req = view.calls.last.request
    assert "authorization" not in req.headers and HEADER not in req.headers  # public view
    api.delete(f"{V1}/plots/{saved['id']}").respond(204)
    scoped.plots.delete_saved(saved["id"])


# ================================================================================ trust


def test_signup_and_verification_send_no_credentials(
    scoped: Client, api: respx.MockRouter, example: Ex
) -> None:
    signup = api.post(f"{BASE}/auth/signup").respond(202, json=example(SignupAccepted))
    got = scoped.signup("ana@acme.example", "Ana Analyst", accepted_terms=True)
    assert got.status == "check_email"
    assert "authorization" not in signup.calls.last.request.headers
    assert body_of(signup)["accepted_terms"] is True
    verify = api.post(f"{BASE}/auth/verify-email").respond(200, json=example(VerificationResult))
    token = "mev_" + "0123456789abcdefghjkmnpqrstvwxyz01234567"
    assert scoped.verify_email(token).trust_tier.value == "public"
    assert "authorization" not in verify.calls.last.request.headers
    with pytest.raises(ValueError):
        scoped.verify_email("not-a-token")


def test_tenant_deletion_and_export(
    scoped: Client, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    api.get(f"{V1}/tenants/acme/verification").respond(200, json=example(VerificationStatus))
    assert not scoped.tenants.verification("acme").uploaded_code_allowed
    deletion = example(TenantDeletion)
    post = api.post(f"{V1}/tenants/acme/deletion").respond(202, json=deletion)
    with pytest.raises(ValueError, match="confirm"):
        scoped.tenants.request_deletion("acme", confirm="acme-retail")
    scoped.tenants.request_deletion("acme", confirm="acme", reason="closing the account")
    assert body_of(post) == {"confirm": "acme", "reason": "closing the account"}
    assert HEADER not in post.calls.last.request.headers  # tenant routes name the tenant
    api.get(f"{V1}/tenants/acme/deletion").respond(200, json=deletion)
    assert scoped.tenants.deletion("acme").state == deletion["state"]
    api.delete(f"{V1}/tenants/acme/deletion").respond(200, json=deletion)
    scoped.tenants.cancel_deletion("acme")

    archive = b"PK\x03\x04" + b"z" * 100
    ready = example(GdprExport) | {
        "size_bytes": len(archive),
        "download_url": f"{STORAGE}/exports/exp.zip?sig=1",
    }
    queued = ready | {"state": "queued", "download_url": None, "size_bytes": None}
    api.post(f"{V1}/tenants/acme/exports").respond(202, json=queued)
    started = scoped.tenants.export("acme", include=["metadata", "audit"])
    api.get(f"{V1}/tenants/acme/exports/{started.id}").mock(
        side_effect=[httpx.Response(200, json=queued), httpx.Response(200, json=ready)]
    )
    dl = api.get(f"{STORAGE}/exports/exp.zip").respond(200, content=archive)
    path = scoped.tenants.download_export("acme", started.id, tmp_path / "export.zip")
    assert path.read_bytes() == archive
    assert "authorization" not in dl.calls.last.request.headers
    short = ready | {"size_bytes": len(archive) + 1}
    api.get(f"{V1}/tenants/acme/exports/{started.id}").respond(200, json=short)
    with pytest.raises(IntegrityFailure):
        scoped.tenants.download_export("acme", started.id, tmp_path / "again.zip")
    assert not (tmp_path / "again.zip").exists()


def test_admin_trust(scoped: Client, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/admin/signups").respond(200, json=page([example(SignupReview)]))
    assert scoped.admin.signups(review="flagged").take(1)[0].email
    review = api.post(f"{V1}/admin/signups/sgn_1/review").respond(200, json=example(SignupReview))
    scoped.admin.review_signup("sgn_1", "reject", "Sybil cluster")
    assert body_of(review) == {"decision": "reject", "reason": "Sybil cluster"}
    put = api.put(f"{V1}/admin/tenants/acme/verification").respond(
        200, json=example(VerificationStatus)
    )
    scoped.admin.set_verification("acme", identity_verified=True, method="manual", reason="KYC")
    assert body_of(put)["method"] == "manual"
    api.post(f"{V1}/admin/tenants/acme/deletion/retry").respond(202, json=example(TenantDeletion))
    scoped.admin.retry_deletion("acme")


# ===================================================================================== async


async def test_async_m3_resources(api: respx.MockRouter, example: Ex) -> None:
    cfg = ClientConfig(base_url=BASE, project="acme/growth", max_retries=0)

    async def no_sleep(_: float) -> None:
        return None

    async with AsyncClient(config=cfg, sleep=no_sleep) as c:
        api.get(f"{V1}/guided/templates").respond(200, json=page([example(GuidedTemplate)]))
        assert len([t async for t in c.guided.templates()]) == 1
        route = api.post(f"{V1}/endpoints/geo-mmm-prod/score").mock(
            side_effect=[
                httpx.Response(202, json=_result(example, False)),
                httpx.Response(200, json=_result(example, True)),
            ]
        )
        res = await c.endpoints.score("geo-mmm-prod", {"mode": "predict"}, wait=1)
        assert res.done and route.call_count == 2
        api.get(f"{V1}/endpoints/geo-mmm-prod/schema").respond(200, json=example(EndpointSchema))
        assert (await c.endpoint("geo-mmm-prod").schema()).revision == 4
        api.post(f"{V1}/webhooks").respond(201, json=example(WebhookCreated))
        assert (await c.webhooks.create("https://h.example.com/x", ["run.failed"])).secret
        api.post(f"{BASE}/auth/signup").respond(202, json=example(SignupAccepted))
        assert (await c.signup("ana@acme.example", "Ana", accepted_terms=True)).expires_in_s
        api.get(f"{V1}/tenants/acme/deletion").respond(200, json=example(TenantDeletion))
        assert (await c.tenants.deletion("acme")).id
        api.get(f"{V1}/admin/signups").respond(200, json=page([]))
        assert [s async for s in c.admin.signups()] == []
        done = example(PriorTranslation)
        api.post(f"{V1}/guided/prior-translations").respond(200, json=done)
        got = await c.guided.preview_priors(_business_priors(), spec_id="spc_1", revision=2)
        assert got.done
        png = b"png-bytes"
        render = example(PlotRender) | {"format": "png", "outputs": [_png_link(png)]}
        api.post(f"{V1}/plots:render").respond(200, json=render)
        api.get(f"{STORAGE}/renders/plot.png").respond(200, content=png)
        r = await c.plots.render(saved_plot_id="plt_01jaf3h1j2k3m4n5p6q7r8s9t0")
        assert await c.plots.download_render(r) == png
