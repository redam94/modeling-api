"""Guided prior translations (M3): ``/v1/guided/prior-translations`` end to end against the
real ``mmm.hierarchical_geo`` catalog entry - static checks, the pinned ``translate_priors``
run, finalization of its ``priors.json`` and tenant isolation - plus the runner scorecard
on a model version and the static lift-test checks of spec validation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
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
from mapi_server_testkit.builders import TREND_SPEC, ready_dataset, simulate_outputs
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.api import ModelVersion, PriorTranslation, new_id
from mapi_spec.calibration import PriorTranslationJob, PriorTranslationResult
from mapi_spec.executor import RunKind
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.scorecard import evaluate_check, rollup
from mapi_spec.storage import dataset_prefix

pytestmark = pytest.mark.integration

URL = "/v1/guided/prior-translations"

RAW_SCHEMA: dict[str, Any] = {
    "dims": {
        "market": {"kind": "categorical", "policy": "new_levels",
                   "new_level_strategy": "hyperprior"},
        "channel": {"kind": "categorical"},
        "date": {"kind": "time", "policy": "extend", "freq": "D"},
    },
    "vars": {
        "revenue_local": {"dims": ["market", "date"], "dtype": "float64", "role": "target",
                          "units": "EUR"},
        "spend_local": {"dims": ["market", "channel", "date"], "dtype": "float64",
                        "role": "spend", "units": "EUR"},
    },
}  # fmt: skip
SUMMARY: dict[str, Any] = {
    "vars": ["revenue_local", "spend_local"],
    "coords": {
        "market": {"kind": "categorical", "size": 2, "values": ["NL", "DE"]},
        "channel": {"kind": "categorical", "size": 2, "values": ["tv", "search"]},
        "date": {
            "kind": "time",
            "size": 210,
            "start": "2023-01-02",
            "end": "2023-07-30",
            "freq": "D",
            "contiguous": True,
        },
    },
}
MMM_SPEC = """\
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: geo-mmm, project: default, labels: {guided: geo_mmm}}
data:
  dataset: dataset://default/media@latest
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}
pipeline:
  - {id: weekly, use: core.resample@^1, with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y, use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags, use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 4, pad: first}}
model:
  use: mmm.hierarchical_geo@^1
  settings: {adstock: {kind: geometric, l_max: 4}}
  fit: {engine: pymc, tune: 50, draws: 50, chains: 2, seed: 3}
outputs: {derived: [response_curves]}
"""
BELIEFS: dict[str, Any] = {
    "channels": {
        "tv": {"roi": [1.5, 4.0], "credibility": 0.8},
        "search": {"contribution_share": [0.05, 0.15]},
    },
    "baseline_share": {"share": [0.5, 0.8]},
}


@pytest.fixture(scope="module")
def mmm_catalog(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real catalog of the steps, the plot recipes and ``mmm.hierarchical_geo``."""
    import mapi_steps
    from mapi_plots.recipes import all_recipes
    from mapi_plugin_mmm.hierarchical_geo import HierarchicalGeoMMM
    from mapi_plugin_ts import HSGPTrendModel

    reg = mapi_steps.registry()
    reg.register(all_recipes())
    reg.register_model(HierarchicalGeoMMM)
    reg.register_model(HSGPTrendModel)
    path = tmp_path_factory.mktemp("catalog") / "catalog.json"
    path.write_text(reg.catalog().model_dump_json())
    return path


@pytest.fixture
def settings(settings: Settings, mmm_catalog: Path) -> Settings:
    return settings.model_copy(update={"catalog_path": mmm_catalog})


async def _media_dataset(ctx: ServerContext) -> None:
    data = os.urandom(512)
    sha = hashlib.sha256(data).hexdigest()
    meta = await ctx.store.put(
        ctx.buckets.data, dataset_prefix(ctx.scope.tenant_id, sha) + "data.zarr.zip", data
    )
    async with ctx.db.session() as s:
        ds = m.Dataset(
            id=new_id("ds"),
            tenant_id=ctx.scope.tenant_id,
            project_id=ctx.scope.project_id,
            name="media",
            labels={},
            latest_version=1,
            next_version=2,
        )
        s.add(ds)
        await s.flush()
        s.add(
            m.DatasetVersion(
                dataset_id=ds.id,
                version=1,
                tenant_id=ds.tenant_id,
                project_id=ds.project_id,
                status="ready",
                sha256=f"sha256:{sha}",
                size_bytes=len(data),
                data_schema=RAW_SCHEMA,
                summary=SUMMARY,
                objects={
                    "data.zarr.zip": {
                        "bucket": meta.bucket,
                        "name": meta.name,
                        "generation": meta.generation,
                        "size": meta.size,
                        "crc32c": meta.crc32c,
                        "md5": meta.md5_hash,
                        "content_type": None,
                    }
                },
                issues=[],
                ready_at=datetime.now(UTC),
            )
        )


def _document(text: str = MMM_SPEC) -> dict[str, Any]:
    from mapi_spec.loader import parse_text

    doc = parse_text(text)
    assert isinstance(doc, dict)
    return doc


async def _post(client: httpx.AsyncClient, body: dict[str, Any], **headers: str) -> httpx.Response:
    return await client.post(URL, json=body, headers=headers)


async def test_a_translation_is_validated_pinned_and_queued(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await _media_dataset(ctx)
    body = {"document": _document(), "business_priors": BELIEFS, "draws": 300, "seed": 7}
    r = await _post(client, body)
    assert r.status_code == 202, r.text
    view = PriorTranslation.model_validate(r.json())
    assert view.run.kind == RunKind.TRANSLATE_PRIORS and not view.done and view.result is None
    assert view.apply_at == "/model/settings/priors"
    async with ctx.db.session() as s:
        run = await load_run(s, None, view.run.id)
    assert run.state == RunState.QUEUED.value and run.priority == "interactive"
    job = PriorTranslationJob.model_validate(run.payload)
    assert job.draws == 300 and job.seed == 7
    assert job.spec["model"]["use"] == "mmm.hierarchical_geo@1.3.0"  # resolved and pinned
    assert "@sha256:" in str(job.spec["data"]["dataset"])  # pinned to the version's content
    assert set(run.inputs) == {"dataset"} and run.dataset_version == 1
    got = PriorTranslation.model_validate((await client.get(f"{URL}/{view.run.id}")).json())
    assert got.run.id == view.run.id and not got.done
    # an Idempotency-Key replays the same run
    first = await _post(client, body, **{"Idempotency-Key": "translate-1"})
    again = await _post(client, body, **{"Idempotency-Key": "translate-1"})
    assert first.status_code == 202 and again.status_code == 200
    assert first.json()["run"]["id"] == again.json()["run"]["id"]


async def test_a_saved_revision_can_be_translated(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await _media_dataset(ctx)
    created = await client.post("/v1/specs", json={"yaml": MMM_SPEC})
    assert created.status_code == 201, created.text
    spec_id = created.json()["id"]
    r = await _post(client, {"spec_id": spec_id, "revision": 1, "business_priors": BELIEFS})
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        run = await load_run(s, None, r.json()["run"]["id"])
    assert run.spec_id == spec_id and run.spec_revision == 1


@pytest.mark.parametrize(
    ("beliefs", "lift", "code", "pointer"),
    [
        ({"channels": {"radio": {"roi": [1, 2]}}}, [], "BUSINESS_PRIORS_INVALID",
         "/business_priors/channels/radio"),
        ({"channels": {"tv": {"contribution_share": [0.6, 0.7]}},
          "baseline_share": {"share": [0.5, 0.8]}}, [], "BUSINESS_PRIORS_INVALID",
         "/business_priors/channels"),
        (BELIEFS, [{"channel": "tv", "geo": "FR", "start": "2023-03-06", "end": "2023-04-02",
                    "spend_delta": -10.0, "kpi_delta": -5.0, "se": 1.0}],
         "SCHEMA_INVALID", "/lift_tests/0/geo"),
        (BELIEFS, [{"channel": "tv", "start": "2022-03-06", "end": "2022-04-02",
                    "spend_delta": -10.0, "kpi_delta": -5.0, "se": 1.0}],
         "SCHEMA_INVALID", "/lift_tests/0/start"),
    ],
)  # fmt: skip
async def test_static_problems_are_refused_before_any_run(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    beliefs: dict[str, Any],
    lift: list[dict[str, Any]],
    code: str,
    pointer: str,
) -> None:
    await _media_dataset(ctx)
    r = await _post(
        client, {"document": _document(), "business_priors": beliefs, "lift_tests": lift}
    )
    assert r.status_code == 422, r.text
    problem = r.json()
    assert problem["code"] == code
    assert pointer in [e["pointer"] for e in problem["errors"]]
    async with ctx.db.session() as s:
        assert (await s.execute(m.Run.__table__.select())).first() is None


async def test_a_model_without_business_priors_is_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await ready_dataset(ctx)
    r = await _post(client, {"document": _document(TREND_SPEC), "business_priors": BELIEFS})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "CALIBRATION_UNSUPPORTED"


async def _finalizing(ctx: ServerContext, run_id: str) -> tuple[m.Run, m.RunAttempt]:
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        s.expunge_all()
    return run, att


def _result() -> PriorTranslationResult:
    example = PriorTranslationResult.model_json_schema()["examples"][0]
    return PriorTranslationResult.model_validate(example)


async def test_the_finalized_result_is_served_and_bad_results_are_refused(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await _media_dataset(ctx)
    body = {"document": _document(), "business_priors": BELIEFS}
    run_id = (await _post(client, body)).json()["run"]["id"]
    run, att = await _finalizing(ctx, run_id)
    result = _result()
    await simulate_outputs(
        ctx, run, att, add={"priors.json": (result.model_dump_json().encode(), "application/json")}
    )
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    view = PriorTranslation.model_validate((await client.get(f"{URL}/{run_id}")).json())
    assert view.done and view.result == result
    assert view.result.priors["beta_channel"]["distribution"] == "LogNormal"
    # another run whose priors.json is not a translation fails as an integrity problem
    bad_id = (await _post(client, body)).json()["run"]["id"]
    bad, bad_att = await _finalizing(ctx, bad_id)
    await simulate_outputs(
        ctx, bad, bad_att, add={"priors.json": (json.dumps({"x": 1}).encode(), "application/json")}
    )
    assert await Finalizer(ctx).finalize(bad.id, bad_att.attempt) is RunState.FAILED
    failed = PriorTranslation.model_validate((await client.get(f"{URL}/{bad_id}")).json())
    assert failed.done and failed.result is None
    assert failed.run.problem is not None and failed.run.problem.code == "INTEGRITY_FAILURE"


async def test_other_runs_and_other_tenants_are_not_translations(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    from mapi_server_testkit.builders import queued_fit

    fit_id = await queued_fit(ctx)
    assert (await client.get(f"{URL}/{fit_id}")).status_code == 404
    await _media_dataset(ctx)
    run_id = (await _post(client, {"document": _document(), "business_priors": BELIEFS})).json()[
        "run"
    ]["id"]
    # the same id from another tenant's session: row-level security hides it
    from mapi_server.services.prior_translations import load_prior_translation_result

    async with ctx.db.tenant_session("someone-else") as s:
        with pytest.raises(Exception):  # noqa: B017 - NotFoundError from load_run
            run = await load_run(s, None, run_id)
            await load_prior_translation_result(s, ctx, run)


# ================================================================ runner scorecard on a fit


async def test_the_runners_scorecard_is_stored_on_the_model_version(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    from mapi_server_testkit.builders import queued_fit

    run_id = await queued_fit(ctx)
    run, att = await _finalizing(ctx, run_id)
    card = rollup(
        [evaluate_check("holdout.mape", 0.14), evaluate_check("convergence.rhat", 1.002)],
        {"plausibility.roi_absurd": "the model has no response curves"},
    )
    await simulate_outputs(
        ctx, run, att, add={"scorecard.json": (card.model_dump_json().encode(), "application/json")}
    )
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED
    async with ctx.db.session() as s:
        done = await load_run(s, None, run_id)
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
        model_id = mv.model_id
    view = ModelVersion.model_validate(
        (await client.get(f"/v1/models/{model_id}/versions/1")).json()
    )
    assert view.scorecard == card
    assert view.scorecard.overall == "warn" and view.scorecard.items[0].check == "holdout.mape"
    assert view.scorecard.skipped == {"plausibility.roi_absurd": "the model has no response curves"}


async def test_spec_validation_checks_lift_tests_against_the_dataset(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    await _media_dataset(ctx)
    lift = (
        "calibration:\n  lift_tests:\n"
        "    - {channel: radio, start: 2023-03-06, end: 2023-04-02, spend_delta: -1,"
        " kpi_delta: -1, se: 1}\n"
        "    - {channel: tv, geo: DE, start: 2023-03-06, end: 2023-09-02, spend_delta: -1,"
        " kpi_delta: -1, se: 1}\n"
    )
    r = await client.post("/v1/specs:validate", json={"yaml": MMM_SPEC + lift})
    assert r.status_code == 200, r.text
    issues = {(i["pointer"], i["code"]) for i in r.json()["issues"]}
    assert ("/calibration/lift_tests/0/channel", "SCHEMA_INVALID") in issues
    assert ("/calibration/lift_tests/1/end", "SCHEMA_INVALID") in issues
    assert not r.json()["valid"]
    ok = MMM_SPEC + lift.replace("radio", "tv").replace("2023-09-02", "2023-04-02")
    assert (await client.post("/v1/specs:validate", json={"yaml": ok})).json()["valid"]


async def test_the_routes_are_in_the_openapi_document_with_their_problems() -> None:
    from mapi_server.app import openapi_document
    from mapi_spec.api import M3_ENDPOINTS

    doc = openapi_document()
    ops = {
        (method.upper(), path, op.get("operationId")): op
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
    }
    for decl in M3_ENDPOINTS:
        if decl.operation_id not in ("create_prior_translation", "get_prior_translation"):
            continue
        op = ops[(decl.method, decl.path, decl.operation_id)]
        assert str(decl.success) in op["responses"]
        assert "422" in op["responses"] or decl.method == "GET"
        assert "404" in op["responses"]
    post = ops[("POST", URL, "create_prior_translation")]
    assert "BUSINESS_PRIORS_INVALID" in post["responses"]["422"]["description"]
    assert "429" in post["responses"]
