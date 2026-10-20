"""Guided mode, data path: templates, role detection (browser profiles and stored dataset
versions), drafts validated against the catalog, ``columns.json``
at finalize and the OpenAPI contract of the operations (single-tenant profile)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.settings import Settings
from mapi_server_testkit.builders import ready_dataset, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.integrations import fitted_version
from mapi_spec.errors import PROBLEM_CONTENT_TYPE
from mapi_spec.guided import GuidedDraft, GuidedTemplate, RoleSuggestions
from mapi_spec.resources import RESOURCE_CLASSES, ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.spec import ScoreRequest
from sqlalchemy import select, update

GUIDED_CATALOG = Path(__file__).parent / "fixtures" / "catalog_guided.json"

MMM_COLUMNS: list[dict[str, Any]] = [
    {"name": "Week", "dtype": "date", "n_rows": 520, "n_distinct": 104, "detected_freq": "W-SUN"},
    {"name": "Region", "dtype": "string", "n_rows": 520, "n_distinct": 5},
    {"name": "Revenue", "dtype": "number", "n_rows": 520, "min": 10.0, "max": 9e4},
    {"name": "TV_Spend", "dtype": "number", "n_rows": 520, "min": 0.0, "max": 5e4},
    {"name": "Search_Spend", "dtype": "number", "n_rows": 520, "min": 0.0, "max": 2e4},
    {"name": "avg_price", "dtype": "number", "n_rows": 520, "min": 1.0, "max": 3.0},
]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"catalog_path": GUIDED_CATALOG})


def _problem(r: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body: dict[str, Any] = r.json()
    assert body["code"] == code, body
    return body


# ================================================================================ templates


async def test_templates_are_listed_and_paged(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/guided/templates")
    assert r.status_code == 200
    ids = [GuidedTemplate.model_validate(t).id for t in r.json()["items"]]
    assert ids == ["customer_choice", "geo_mmm", "mediated_mmm", "multi_kpi_mmm",
                   "store_demand", "weekly_forecast"]  # fmt: skip
    first = (await client.get("/v1/guided/templates", params={"limit": 5})).json()
    assert len(first["items"]) == 5 and first["next_cursor"]
    rest = (
        await client.get("/v1/guided/templates", params={"cursor": first["next_cursor"]})
    ).json()
    assert [t["id"] for t in rest["items"]] == ["weekly_forecast"] and rest["next_cursor"] is None
    one = GuidedTemplate.model_validate((await client.get("/v1/guided/templates/geo_mmm")).json())
    assert one.plugin == "mmm.bayesian@^1" and one.ui_levels["/pipeline"] == "hidden"
    nested = GuidedTemplate.model_validate(
        (await client.get("/v1/guided/templates/mediated_mmm")).json()
    )
    assert nested.plugin == "mmm.nested@^1" and nested.about is not None
    choice = (await client.get("/v1/guided/templates/customer_choice")).json()
    assert choice["plugin_available"] is True
    _problem(await client.get("/v1/guided/templates/nope"), 404, "NOT_FOUND")


# ================================================================================ detection


async def test_detect_roles_from_browser_profiles(client: httpx.AsyncClient) -> None:
    body = {"template_id": "geo_mmm", "columns": MMM_COLUMNS}
    r = await client.post("/v1/guided/detect-roles", json=body)
    assert r.status_code == 200, r.text
    sug = RoleSuggestions.model_validate(r.json())
    answers = {q.question_id: q.answer for q in sug.questions}
    assert answers["kpi"] == "Revenue" and answers["geo"] == "Region"
    assert answers["spend"] == ["Search_Spend", "TV_Spend"] and answers["controls"] == ["avg_price"]
    _problem(
        await client.post("/v1/guided/detect-roles", json={**body, "template_id": "nope"}),
        404,
        "NOT_FOUND",
    )
    both = {**body, "dataset": "dataset://default/x@v1"}
    _problem(await client.post("/v1/guided/detect-roles", json=both), 422, "SCHEMA_INVALID")


async def test_detect_roles_from_a_stored_version_without_columns(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await ready_dataset(ctx, "co2")  # ingested before v0.4: schema and summary only
    r = await client.post(
        "/v1/guided/detect-roles",
        json={"template_id": "weekly_forecast", "dataset": "dataset://default/co2@latest"},
    )
    assert r.status_code == 200, r.text
    answers = {q["question_id"]: q["answer"] for q in r.json()["questions"]}
    assert answers["date"] == "t"  # the dataset's time dim
    nope = {"template_id": "weekly_forecast", "dataset": "dataset://default/nope@latest"}
    body = _problem(await client.post("/v1/guided/detect-roles", json=nope), 404, "NOT_FOUND")
    assert "does not exist" in body["detail"]


async def test_ingest_stores_column_profiles_and_detection_reads_them(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
    up = (
        await client.post(
            f"/v1/datasets/{ds['id']}/uploads",
            json={"filename": "co2.csv", "content_type": "text/csv", "size_bytes": 100},
        )
    ).json()
    await ctx.store.put(ctx.buckets.quarantine, up["upload"]["name"], os.urandom(50))
    mapping = {
        "format": "csv",
        "dims": {"t": {"column": "date", "spec": {"kind": "time", "policy": "extend"}}},
        "vars": {"y": {"column": "co2", "role": "target"}},
    }
    dv = (
        await client.post(
            f"/v1/datasets/{ds['id']}/versions", json={"upload_id": up["id"], "mapping": mapping}
        )
    ).json()
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, dv["ingest_run_id"], for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run.id, run.attempt))
        assert att is not None
        s.expunge_all()
    # decoded data must fit the class's scratch budget (a bomb fails before it fills RAM)
    scratch = RESOURCE_CLASSES[ResourceClass(run.resource_class)].scratch_bytes
    assert run.payload["max_uncompressed_bytes"] == scratch
    await simulate_outputs(ctx, run, att)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        row = await s.get(m.DatasetVersion, (ds["id"], 1))
        assert row is not None and row.columns is not None
        assert [c["name"] for c in row.columns] == ["date", "co2"]
    link = await client.get(f"/v1/datasets/{ds['id']}/versions/1/artifacts/columns.json")
    assert link.status_code == 200 and link.json()["name"] == "columns.json"
    r = await client.post(
        "/v1/guided/detect-roles",
        json={"template_id": "weekly_forecast", "dataset": "dataset://default/co2@v1"},
    )
    sug = RoleSuggestions.model_validate(r.json())
    date = next(q for q in sug.questions if q.question_id == "date")
    assert date.answer == "date"  # the uploaded column, not the dataset dim 't'
    assert "values in order" in date.candidates[0].reason_plain


async def test_a_bad_columns_sidecar_is_an_integrity_failure(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    ds = (await client.post("/v1/datasets", json={"name": "co2"})).json()
    up = (
        await client.post(
            f"/v1/datasets/{ds['id']}/uploads",
            json={"filename": "co2.csv", "content_type": "text/csv", "size_bytes": 100},
        )
    ).json()
    await ctx.store.put(ctx.buckets.quarantine, up["upload"]["name"], os.urandom(50))
    mapping = {"format": "zarr_zip"}
    dv = (
        await client.post(
            f"/v1/datasets/{ds['id']}/versions", json={"upload_id": up["id"], "mapping": mapping}
        )
    ).json()
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, dv["ingest_run_id"], for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run.id, run.attempt))
        assert att is not None
        s.expunge_all()
    bad = json.dumps([{"name": "x", "dtype": "pickle", "n_rows": 1}]).encode()
    await simulate_outputs(ctx, run, att, replace={"columns.json": bad})
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.FAILED


# =================================================================================== drafts


def _geo_answers() -> dict[str, Any]:
    return {
        "template_id": "geo_mmm",
        "name": "eu-geo-mmm",
        "dataset": "eu_media",
        "answers": {"kpi": "Revenue", "date": "Week", "geo": "Region",
                    "spend": ["TV_Spend", "Search_Spend"], "controls": ["avg_price"]},
        "columns": MMM_COLUMNS,
    }  # fmt: skip


async def test_a_geo_mmm_draft_is_validated_against_the_catalog(
    client: httpx.AsyncClient,
) -> None:
    r = await client.post("/v1/guided/drafts", json=_geo_answers())
    assert r.status_code == 200, r.text
    draft = GuidedDraft.model_validate(r.json())
    assert draft.dataset_ref == "dataset://default/eu_media@latest"
    assert draft.mapping.wide["spend"].columns == {"tv": "TV_Spend", "search": "Search_Spend"}
    assert draft.spec["model"]["use"] == "mmm.bayesian@^1"  # type: ignore[index]
    assert "kind: ModelSpec" in draft.yaml
    assert not [i for i in draft.issues if i.severity.value == "error"], draft.issues
    # the stored spec round-trips: save the YAML as the user would
    saved = await client.post("/v1/specs", json={"yaml": draft.yaml})
    assert saved.status_code == 201, saved.text


async def test_draft_validation_reports_what_the_model_cannot_use(
    client: httpx.AsyncClient,
) -> None:
    body = _geo_answers()
    # v0.5: mmm-framework owns seasonality, so the template's pipeline adds no Fourier step
    # and a control named like one is just another control
    body["columns"] = [*MMM_COLUMNS, {"name": "Fourier", "dtype": "number", "n_rows": 520}]
    body["answers"]["controls"] = ["avg_price", "Fourier"]
    draft = GuidedDraft.model_validate((await client.post("/v1/guided/drafts", json=body)).json())
    assert not [i for i in draft.issues if i.severity.value == "error"], draft.issues
    assert [s["id"] for s in draft.spec["pipeline"]] == ["weekly"]  # type: ignore[index,union-attr]
    body["columns"] = [{**c, "dtype": "string"} if c["name"] == "Revenue" else c
                       for c in MMM_COLUMNS]  # fmt: skip
    body["answers"]["controls"] = ["avg_price"]
    draft = GuidedDraft.model_validate((await client.post("/v1/guided/drafts", json=body)).json())
    warns = [i for i in draft.issues if i.code == "GUIDED_ANSWERS_INVALID"]
    assert warns and warns[0].pointer == "/answers/kpi"  # 'Revenue' looks like text


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        ({"kpi": None}, "/answers/kpi"),
        ({"geo": "Revenue"}, "/answers/geo"),
        ({"holdout_periods": 0}, "/answers/holdout_periods"),
        ({"nonsense": 1}, "/answers/nonsense"),
        ({"controls": ["Spend"]}, "/answers/controls"),  # would be named like the spend
    ],
)
async def test_bad_answers_are_422(
    client: httpx.AsyncClient, change: dict[str, Any], pointer: str
) -> None:
    body = _geo_answers()
    body["answers"] = {**body["answers"], **change}
    problem = _problem(await client.post("/v1/guided/drafts", json=body), 422,
                       "GUIDED_ANSWERS_INVALID")  # fmt: skip
    assert problem["errors"][0]["pointer"] == pointer


async def test_choice_and_mff_drafts(client: httpx.AsyncClient) -> None:
    choice = {
        "template_id": "customer_choice",
        "name": "soda",
        "dataset": "soda_trips",
        "answers": {"choice": "brand", "customer": "household", "occasion": "trip",
                    "prices": ["price_cola", "price_fizz"], "promos": ["promo_cola",
                                                                     "promo_fizz"]},
        "columns": [
            {"name": "brand", "dtype": "string", "n_rows": 400, "n_distinct": 2},
            {"name": "household", "dtype": "string", "n_rows": 400, "n_distinct": 50},
            {"name": "trip", "dtype": "integer", "n_rows": 400, "n_distinct": 8},
            *({"name": n, "dtype": "number", "n_rows": 400}
              for n in ("price_cola", "price_fizz", "promo_cola", "promo_fizz")),
        ],
    }  # fmt: skip
    draft = GuidedDraft.model_validate((await client.post("/v1/guided/drafts", json=choice)).json())
    assert draft.spec["model"]["use"] == "choice.multinomial_logit@^1"  # type: ignore[index]
    assert draft.issues == []
    mff = {
        "template_id": "weekly_forecast", "name": "sales", "dataset": "mff", "format": "mff",
        "answers": {"kpi": "Sales", "date": "Period"},
    }  # fmt: skip
    draft = GuidedDraft.model_validate((await client.post("/v1/guided/drafts", json=mff)).json())
    assert draft.mapping.format == "mff" and draft.mapping.preset.value == "mff"


async def test_drafts_degrade_without_a_catalog(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    async with ctx.db.session() as s:
        await s.execute(update(m.Catalog).values(image_digest="sha256:" + "cd" * 32))
    ctx.catalogs._cached = None  # forget the startup snapshot
    draft = GuidedDraft.model_validate(
        (await client.post("/v1/guided/drafts", json=_geo_answers())).json()
    )
    assert [i.code for i in draft.issues] == ["INFRA_ERROR"]


# ================================================================================== OpenAPI


async def test_the_guided_operations_document_their_problems(client: httpx.AsyncClient) -> None:
    doc = (await client.get("/openapi.json")).json()
    ops = {
        op["operationId"]: op
        for path, item in doc["paths"].items()
        if path.startswith("/v1/guided")
        for op in item.values()
    }
    assert {"list_guided_templates", "get_guided_template", "detect_roles",
            "create_guided_draft"} <= set(ops)  # fmt: skip
    draft = ops["create_guided_draft"]
    problem = draft["responses"]["422"]["content"][PROBLEM_CONTENT_TYPE]
    assert problem["schema"] == {"$ref": "#/components/schemas/Problem"}
    assert {"SCHEMA_INVALID", "GUIDED_ANSWERS_INVALID"} <= set(problem["examples"])
    assert "401" in draft["responses"] and "403" in draft["responses"]
    schemas = doc["components"]["schemas"]
    assert schemas["GuidedAnswers"]["examples"]


async def test_project_carries_the_guided_lock(client: httpx.AsyncClient) -> None:
    got = (await client.get("/v1/projects/default")).json()
    assert got["guided_locked"] is False


async def test_guided_requests_compile_to_checked_score_requests(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await fitted_version(ctx)
    body = {"kind": "forecast", "model": "model://default/trend@v1", "periods": 4, "dim": "t"}
    r = await client.post("/v1/guided/requests:compile", json=body)
    assert r.status_code == 200, r.text
    req = ScoreRequest.model_validate(r.json())
    assert req.mode == "forecast" and req.horizon is not None and req.horizon.periods == 4
    assert str(req.model) == "model://default/trend@v1"
    assert req.future_covariates == "carry_last"
    # an unknown model, a dim the model has never seen
    r = await client.post(
        "/v1/guided/requests:compile", json={**body, "model": "model://default/nope@v1"}
    )
    assert r.status_code == 404, r.text
    r = await client.post("/v1/guided/requests:compile", json={**body, "dim": "week"})
    assert r.status_code == 422, r.text
    # a what-if scenario on a model without channels is refused with a pointer
    r = await client.post(
        "/v1/guided/requests:compile",
        json={"kind": "whatif", "model": "model://default/trend@v1",
              "scenario": {"multipliers": {"tv": 0.5}}},
    )  # fmt: skip
    assert r.status_code == 422, r.text
    assert r.json()["errors"][0]["pointer"]


async def test_requests_compile_and_validate_against_an_endpoint(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """An ``endpoint://`` target resolves to the version the alias serves now."""
    await fitted_version(ctx)
    r = await client.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    assert r.status_code == 200, r.text
    body = {"kind": "forecast", "model": "endpoint://default/trend-prod", "periods": 4, "dim": "t"}
    r = await client.post("/v1/guided/requests:compile", json=body)
    assert r.status_code == 200, r.text
    compiled = r.json()
    assert compiled["model"] == "endpoint://default/trend-prod"
    # the dim is checked against the served version's metadata
    r = await client.post("/v1/guided/requests:compile", json={**body, "dim": "week"})
    assert r.status_code == 422, r.text
    r = await client.post(
        "/v1/guided/requests:compile", json={**body, "model": "endpoint://default/nope"}
    )
    assert r.status_code == 404, r.text
    # POST /v1/specs:validate of a score request aimed at the endpoint
    r = await client.post("/v1/specs:validate", json={"yaml": json.dumps(compiled)})
    assert r.status_code == 200, r.text
    assert r.json()["valid"], r.json()["issues"]
    missing = {**compiled, "model": "endpoint://default/nope"}
    r = await client.post("/v1/specs:validate", json={"yaml": json.dumps(missing)})
    [issue] = [i for i in r.json()["issues"] if i["code"] == "REF_UNRESOLVED"]
    assert issue["pointer"] == "/model" and "endpoint 'nope' does not exist" in issue["message"]
    assert "v0.4" not in issue["message"]


async def test_run_exact_over_the_fit_window_scores_the_fit_dataset(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A what-if (or a preset without a horizon) compiles to a counterfactual over the data
    the version was fitted on: the request names that dataset version in ``data``, so the
    score run has a payload (the sandbox refuses a request without one)."""
    import mapi_server.api.guided as guided_api

    mv = await fitted_version(ctx)
    seen: list[ScoreRequest] = []

    async def accept(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(args[2])
        return {}

    # the trend model has no channels; only the compiled request is of interest here
    monkeypatch.setattr(guided_api, "check_score_request", accept)
    async with ctx.db.session() as s:
        fit = await s.get(m.Run, mv.run_id)
        assert fit is not None and fit.dataset_version is not None
        ds = await s.get(m.Dataset, fit.dataset_id)
        assert ds is not None
    want = f"dataset://{ds.project_id}/{ds.name}@v{fit.dataset_version}"
    model = "model://default/trend@v1"
    off = {"kind": "channel_off", "channel": "tv"}
    bodies: list[dict[str, Any]] = [
        {"kind": "whatif", "model": model, "scenario": {"multipliers": {"tv": 0.5}}},
        {"kind": "counterfactual", "model": model, "preset": off},
    ]
    for body in bodies:
        r = await client.post("/v1/guided/requests:compile", json=body)
        assert r.status_code == 200, r.text
        req = ScoreRequest.model_validate(r.json())
        assert req.mode == "counterfactual" and str(req.data) == want
        assert str(seen[-1].data) == want  # what was checked is what is returned
    # forecasts (with or without a preset) are scored over their horizon: no data named
    for body in (
        {"kind": "forecast", "model": model, "periods": 4, "dim": "t"},
        {"kind": "forecast", "model": model, "periods": 4, "dim": "t",
         "preset": {"kind": "budget_scale", "k": 1.1}},
    ):  # fmt: skip
        r = await client.post("/v1/guided/requests:compile", json=body)
        assert r.status_code == 200, r.text
        assert ScoreRequest.model_validate(r.json()).data is None


async def test_a_score_request_without_data_is_refused_before_a_run_starts(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "model://default/trend@v1",
        "mode": "predict",
    }
    r = await client.post(f"/v1/models/{mv.model_id}/versions/1:score", json={"request": request})
    assert r.status_code == 422, r.text
    errors = r.json()["errors"]
    assert {(e["code"], e["pointer"]) for e in errors} >= {("SCHEMA_INVALID", "/data")}
    assert "no data" in next(e["message"] for e in errors if e["pointer"] == "/data")
    async with ctx.db.session() as s:
        runs = (await s.execute(select(m.Run).where(m.Run.kind == "score"))).scalars().all()
    assert runs == []
