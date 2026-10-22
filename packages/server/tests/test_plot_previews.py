"""Live recipe previews (v0.18 D21): ``POST /v1/catalog/plots/{plot_id}/{version}:preview``
and ``GET /v1/catalog/plots/previews/{run_id}``.

Everything that can be refused is refused before a run: the kill switch, an unknown recipe
or one without an example, settings the recipe's schema refuses (with pointers). The same
recipe, settings and catalog reuse one render run for seven days; new runs are rate
limited per user a minute and per tenant a day."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.plot_previews import preview_key
from mapi_server.settings import Profile, Settings
from mapi_server_testkit.builders import CATALOG
from mapi_server_testkit.identity import (
    grant_role,
    make_tenant,
    make_user,
    session_caller,
)
from mapi_server_testkit.plots import INLINE, complete_run, vl_plot
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.jobs import PREVIEW_LABEL, PREVIEW_OUTPUT, RecipePreviewResult, RenderJob
from mapi_spec.plots import PlotExamples, PlotSpec
from mapi_spec.run import RunState
from sqlalchemy import select, update

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

FAN = "/v1/catalog/plots/core.ppc_fan/1.0.0:preview"
SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "var": {"type": "string", "maxLength": 64},
        "hdi": {"type": "number", "minimum": 0.5, "maximum": 0.99},
    },
    "additionalProperties": False,
}


def _example(pid: str) -> dict[str, Any]:
    return vl_plot(pid, {"d": {"kind": "inline_arrow", "data": INLINE}}, recipe=f"{pid}@1.0.0")


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    data = json.loads(CATALOG.read_text())
    for entry in data["plots"]:
        if entry["id"] == "core.ppc_fan":
            entry["settings_schema"] = SETTINGS_SCHEMA
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return settings.model_copy(update={"catalog_path": path, "profile": Profile.MULTI_TENANT})


@pytest.fixture
async def people(ctx: ServerContext, client: httpx.AsyncClient) -> dict[str, Any]:
    # the describe run stores the example renders with the catalog; the file import does not
    examples = PlotExamples(plots={"core.ppc_fan@1.0.0": PlotSpec.model_validate(
        _example("core.ppc_fan"))})  # fmt: skip
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Catalog)
            .where(m.Catalog.image_digest == ctx.catalogs.image_digest)
            .values(plot_examples=examples.model_dump(mode="json", exclude_none=True))
        )
    ctx.catalogs.invalidate()
    # internal tier: the fake executor's isolation places it (a verified tenant needs gVisor)
    await make_tenant(ctx, "acme", tier="internal", projects=("growth", "other"))
    out: dict[str, Any] = {}
    for role, project in (("viewer", "growth"), ("editor", "other")):
        u = await make_user(ctx, f"{role}@acme.test")
        await grant_role(ctx, u, "acme", project, role)
        out[role] = await session_caller(ctx, client, u, tenant_id="acme", project_id=project)
    cl = await make_user(ctx, "client@acme.test")
    await grant_role(ctx, cl, "acme", "growth", "client")
    out["client"] = await session_caller(ctx, client, cl, tenant_id="acme", project_id="growth")
    return out


async def _runs(ctx: ServerContext) -> list[m.Run]:
    async with ctx.db.session() as s:
        stmt = select(m.Run).where(m.Run.kind == "render").order_by(m.Run.created_at)
        return list((await s.execute(stmt)).scalars())


def _result(r: httpx.Response, status: int) -> RecipePreviewResult:
    assert r.status_code == status, r.text
    return RecipePreviewResult.model_validate(r.json())


async def test_a_new_preview_queues_one_render_run_and_reuses_it(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    viewer = people["viewer"]
    first = _result(await viewer.post(FAN, json={"settings": {"var": "y", "hdi": 0.8}}), 202)
    assert not first.done and not first.cached and first.run_id is not None
    [run] = await _runs(ctx)
    assert run.id == first.run_id and run.project_id == "growth"
    job = RenderJob.model_validate(run.payload)
    assert job.recipe is not None and job.plots is None
    assert job.recipe.ref == "core.ppc_fan@1.0.0"
    assert job.recipe.settings == {"var": "y", "hdi": 0.8}
    assert run.labels[PREVIEW_LABEL] == preview_key(
        "core.ppc_fan@1.0.0", {"hdi": 0.8, "var": "y"}, run.catalog_digest or ""
    )
    assert run.labels["mapi.plot"] == "core.ppc_fan" and not run.inputs

    # the same settings (any key order) while it runs: the same run, no new one
    again = _result(await viewer.post(FAN, json={"settings": {"hdi": 0.8, "var": "y"}}), 202)
    assert again.run_id == run.id and again.cached and not again.done
    pending = _result(await viewer.get(f"/v1/catalog/plots/previews/{run.id}"), 200)
    assert not pending.done and pending.plot is None

    drawn = _example("core.ppc_fan")
    drawn["title"] = "Drawn with hdi 0.8"
    await complete_run(
        ctx, run.id, {PREVIEW_OUTPUT: (json.dumps(drawn).encode(), "application/json")}
    )
    done = _result(await viewer.get(f"/v1/catalog/plots/previews/{run.id}"), 200)
    assert done.done and done.plot is not None and done.plot.title == "Drawn with hdi 0.8"
    assert done.problem is None

    # a cache hit is final at once and starts nothing
    hit = _result(await viewer.post(FAN, json={"settings": {"var": "y", "hdi": 0.8}}), 200)
    assert hit.cached and hit.done and hit.run_id == run.id
    assert hit.plot is not None and hit.plot.title == "Drawn with hdi 0.8"
    assert len(await _runs(ctx)) == 1

    # the cache is per project: another project starts its own run
    other = _result(
        await people["editor"].post(FAN, json={"settings": {"var": "y", "hdi": 0.8}}), 202
    )
    assert other.run_id != run.id and not other.cached
    # and cannot read this project's run
    r = await people["editor"].get(f"/v1/catalog/plots/previews/{run.id}")
    assert r.status_code == 404


async def test_empty_settings_return_the_catalog_example_without_a_run(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    r = _result(await people["viewer"].post(FAN, json={}), 200)
    assert r.done and r.cached and r.run_id is None
    assert r.plot is not None and r.plot.id == "core.ppc_fan"
    assert await _runs(ctx) == []


async def test_invalid_settings_are_refused_with_a_pointer_before_any_run(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    viewer = people["viewer"]
    r = await viewer.post(FAN, json={"settings": {"var": "y", "hdi": 2}})
    assert r.status_code == 422, r.text
    problem = Problem.model_validate(r.json())
    assert problem.code is ErrorCode.SCHEMA_INVALID
    assert [i.pointer for i in problem.errors] == ["/settings/hdi"]
    extra = Problem.model_validate(
        (await viewer.post(FAN, json={"settings": {"colour": "red"}})).json()
    )
    assert extra.code is ErrorCode.SCHEMA_INVALID and extra.errors[0].pointer == "/settings"
    wrong = Problem.model_validate((await viewer.post(FAN, json={"settings": {"var": 3}})).json())
    assert wrong.errors[0].pointer == "/settings/var"
    assert await _runs(ctx) == []


async def test_unknown_recipes_recipes_without_example_or_schema(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    viewer = people["viewer"]
    missing = await viewer.post("/v1/catalog/plots/core.nope/1.0.0:preview", json={})
    assert missing.status_code == 404, missing.text
    # core.convergence is in the catalog but has no example (and no settings schema)
    no_example = await viewer.post("/v1/catalog/plots/core.convergence/1.0.0:preview", json={})
    assert no_example.status_code == 422, no_example.text
    assert no_example.json()["code"] == "MODE_NOT_SUPPORTED"
    assert await _runs(ctx) == []


async def test_a_recipe_without_a_settings_schema_takes_no_settings(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    examples = PlotExamples(
        plots={k: PlotSpec.model_validate(_example(k.split("@")[0]))
               for k in ("core.ppc_fan@1.0.0", "core.convergence@1.0.0")}
    )  # fmt: skip
    async with ctx.db.session() as s:
        await s.execute(update(m.Catalog).values(plot_examples=examples.model_dump(mode="json")))
    ctx.catalogs.invalidate()
    url = "/v1/catalog/plots/core.convergence/1.0.0:preview"
    plain = _result(await people["viewer"].post(url, json={}), 200)
    assert plain.plot is not None and plain.plot.id == "core.convergence"
    r = await people["viewer"].post(url, json={"settings": {"var": "y"}})
    assert r.status_code == 422 and r.json()["code"] == "MODE_NOT_SUPPORTED"
    assert await _runs(ctx) == []


async def test_the_kill_switch_refuses_with_409(ctx: ServerContext, people: dict[str, Any]) -> None:
    ctx.settings = ctx.settings.model_copy(update={"plot_preview_enabled": False})
    r = await people["viewer"].post(FAN, json={"settings": {"var": "y"}})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "PREVIEW_DISABLED"
    assert await _runs(ctx) == []


async def test_the_21st_new_preview_in_a_minute_is_429_with_retry_after(
    ctx: ServerContext, people: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the limiter counts in fixed one-minute windows: keep all requests in one window
    from mapi_server.services import plot_previews

    real = plot_previews.count_requests
    at = plot_previews.now()

    async def pinned(s: Any, subject: str, *, now: Any) -> int:
        return await real(s, subject, now=at)

    monkeypatch.setattr(plot_previews, "count_requests", pinned)
    viewer = people["viewer"]
    limit = ctx.settings.plot_preview_per_minute
    assert limit == 20
    for i in range(limit):
        _result(await viewer.post(FAN, json={"settings": {"var": f"v{i}"}}), 202)
    # a cache hit is not counted
    _result(await viewer.post(FAN, json={"settings": {"var": "v0"}}), 202)
    r = await viewer.post(FAN, json={"settings": {"var": "one-too-many"}})
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "RATE_LIMITED"
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert len(await _runs(ctx)) == limit


async def test_the_tenant_day_cap_is_429(ctx: ServerContext, people: dict[str, Any]) -> None:
    ctx.settings = ctx.settings.model_copy(update={"plot_preview_per_day": 2})
    _result(await people["viewer"].post(FAN, json={"settings": {"var": "a"}}), 202)
    _result(await people["editor"].post(FAN, json={"settings": {"var": "b"}}), 202)
    # another user, another project: the same tenant's day
    r = await people["viewer"].post(FAN, json={"settings": {"var": "c"}})
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) > 60
    assert len(await _runs(ctx)) == 2


async def test_failures_the_settings_would_hit_again_are_cached(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    viewer = people["viewer"]
    sticky = _result(await viewer.post(FAN, json={"settings": {"var": "nope"}}), 202)
    flaky = _result(await viewer.post(FAN, json={"settings": {"var": "flaky"}}), 202)
    refused = Problem.from_code(ErrorCode.SCHEMA_INVALID, "no such variable", errors=()).model_dump(
        mode="json", exclude_none=True
    )
    lost = Problem.from_code(ErrorCode.INFRA_ERROR, "the worker was lost").model_dump(
        mode="json", exclude_none=True
    )
    async with ctx.db.session() as s:
        for run_id, problem in ((sticky.run_id, refused), (flaky.run_id, lost)):
            await s.execute(
                update(m.Run)
                .where(m.Run.id == run_id)
                .values(state=RunState.FAILED.value, problem=problem)
            )
    hit = _result(await viewer.post(FAN, json={"settings": {"var": "nope"}}), 200)
    assert hit.cached and hit.done and hit.run_id == sticky.run_id
    assert hit.problem is not None and hit.problem.code is ErrorCode.SCHEMA_INVALID
    again = _result(await viewer.post(FAN, json={"settings": {"var": "flaky"}}), 202)
    assert again.run_id != flaky.run_id and not again.cached
    assert len(await _runs(ctx)) == 3


async def test_prefer_wait_holds_then_answers_202(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    r = await people["viewer"].post(
        FAN, json={"settings": {"var": "slow"}}, headers={"Prefer": "wait=1"}
    )
    res = _result(r, 202)
    assert not res.done and res.run_id is not None


async def test_only_preview_runs_are_read_and_clients_get_nothing(
    ctx: ServerContext, people: dict[str, Any]
) -> None:
    viewer = people["viewer"]
    first = _result(await viewer.post(FAN, json={"settings": {"var": "y"}}), 202)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Run).where(m.Run.id == first.run_id).values(labels={"mapi.plot": "x"})
        )
    r = await viewer.get(f"/v1/catalog/plots/previews/{first.run_id}")
    assert r.status_code == 404
    assert (await viewer.get("/v1/catalog/plots/previews/run_" + "0" * 26)).status_code == 404
    for r in (
        await people["client"].post(FAN, json={"settings": {"var": "y"}}),
        await people["client"].get(f"/v1/catalog/plots/previews/{first.run_id}"),
    ):
        assert r.status_code == 404, r.text
