"""The v0.5 Python client against the real server: governance policies (project and
tenant-wide), a validation approved by a second person with its evidence pack, the Data
Explorer and a budget optimisation - the platform's runs finish while the client waits."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mapi_client import AsyncClient
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.finalizer import Finalizer
from mapi_server_testkit.builders import ready_dataset, simulate_outputs
from mapi_server_testkit.client import SessionAuth, asgi_client
from mapi_server_testkit.identity import grant_role, make_user, session_caller
from mapi_server_testkit.integrations import fitted_version
from mapi_server_testkit.plots import _to_finalizing
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.governance import (
    GOVERNANCE_CHECKS,
    GOVERNANCE_OUTPUT,
    CheckResult,
    CheckStatus,
    GovernanceJob,
    GovernanceReport,
)
from mapi_spec.optimize import OPTIMIZE_OUTPUT, OptimizeResult
from mapi_spec.run import RunState
from mapi_spec.views import EXPLORE_INDEX, ExplorerIndex
from sqlalchemy import update

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]


@pytest.fixture
async def api(ctx: ServerContext) -> AsyncIterator[AsyncClient]:
    async with asgi_client(ctx) as c:
        yield c


# ============================================================================ governance


async def test_policies_project_and_tenant_wide(api: AsyncClient) -> None:
    seeded = [p async for p in api.governance.policies()]
    assert seeded and {p.scope for p in seeded} == {"tenant"}
    pol = await api.governance.create_policy(
        name="Forecasts we publish",
        applies_to={"families": ["ts"]},
        checks=[
            "baseline.beats_naive",
            {"check": "holdout.accuracy", "thresholds": {"warn": 0.1, "fail": 0.2}},
        ],
        require_approval=True,
    )
    assert pol.version == 1 and pol.scope == "project"
    edited = pol.model_copy(update={"name": "Forecasts we publish (v2)"})
    v2 = await api.governance.update_policy(pol.id, edited, expected_version=1)
    assert v2.version == 2 and v2.name.endswith("(v2)")
    with pytest.raises(MapiError) as info:
        await api.governance.update_policy(pol.id, edited, expected_version=1)
    assert info.value.code is ErrorCode.CONFLICT
    assert [p.version async for p in api.governance.policy_versions(pol.id)] == [2, 1]
    await api.governance.retire_policy(pol.id)
    assert not (await api.governance.policy(pol.id)).active
    tenant = [p async for p in api.governance.policies(tenant_id="default")]
    floor = await api.governance.update_policy(tenant[0].id, tenant[0], tenant_id="default")
    assert floor.version == 2 and floor.project_id is None


def _report(job: dict[str, Any]) -> bytes:
    j = GovernanceJob.model_validate(job)
    results = [
        CheckResult(
            check=c.check,
            severity=c.severity,
            metric=GOVERNANCE_CHECKS[c.check].metric,
            status=CheckStatus.PASS,
            value=0.01,
            message_plain=f"{c.check} measured",
        )
        for c in j.checks
    ]
    return (
        GovernanceReport(
            model_ref=j.model_ref,
            model_id="ts.hsgp_trend",
            family="ts",
            model_kind="bayesian",
            results=results,
            refits=21,
        )
        .model_dump_json()
        .encode()
    )


async def _finish_governance(ctx: ServerContext, run_id: str) -> None:
    run, att = await _to_finalizing(ctx, run_id)
    add = {GOVERNANCE_OUTPUT: (_report(run.payload), "application/json")}
    await simulate_outputs(ctx, run, att, add=add)
    assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED


async def test_a_validation_approved_by_a_second_person(
    api: AsyncClient, ctx: ServerContext, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    mv = await fitted_version(ctx)
    await api.governance.create_policy(
        name="Everything we promote",
        checks=["data.quality", "convergence"],
        require_approval=True,
    )
    val = await api.models.validate(mv.model_id, mv.version, note="Q4 refresh")
    assert val.status.value == "checks_running" and val.workflow_run_id
    assert val.workflow_run_id is not None
    await _finish_governance(ctx, val.workflow_run_id)
    val = await api.validations.wait(val.id)
    assert val.status.value == "awaiting_approval" and not val.failing_checks
    queue = [v.id async for v in api.validations.list(status="awaiting_approval")]
    assert queue == [val.id]
    assert {r.status.value async for r in api.validations.evidence(val.id)} == {"pass"}
    with pytest.raises(MapiError) as info:  # the submitter may not approve
        await api.validations.approve(val.id)
    assert info.value.code is ErrorCode.SELF_APPROVAL_FORBIDDEN

    rita = await make_user(ctx, "rita@default.test")
    await grant_role(ctx, rita, "default", "default", "editor")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Membership)
            .where(m.Membership.principal_id == rita.id)
            .values(capabilities=["reviewer"])
        )
    caller = await session_caller(ctx, client, rita, tenant_id="default", project_id="default")
    async with asgi_client(ctx, auth=SessionAuth(caller.headers, caller.cookies)) as reviewer:
        decided = await reviewer.validations.approve(val.id, comment="Evidence reviewed")
    assert decided.status.value == "validated" and len(decided.approvals) == 1

    html = await api.validations.download_evidence_pack(val.id, tmp_path / "pack.html")
    page = html.read_text("utf-8")
    assert "<script" not in page and val.id in page
    doc = json.loads(
        (await api.validations.download_evidence_pack(val.id, tmp_path / "pack.json")).read_text()
    )
    assert doc["format"] == "mapi.evidence-pack/v1" and doc["digest"].startswith("sha256:")
    retired = await api.validations.deprecate(val.id, "Superseded by v2")
    assert retired.status.value == "deprecated"
    listed = [v.id async for v in api.models.validations(mv.model_id, mv.version)]
    assert listed == [val.id]


# ============================================================ explorers and optimisation


async def test_the_data_explorer_and_its_views(api: AsyncClient, ctx: ServerContext) -> None:
    ds, _dv = await ready_dataset(ctx)
    about = await api.datasets.describe(ds.id, "Monthly CO2 from Mauna Loa")
    assert about.description_user == "Monthly CO2 from Mauna Loa"
    assert (await api.datasets.about(ds.id)).summary_generated

    async def finish(_: float) -> None:  # the platform computes while the client waits
        async with ctx.db.session() as s:
            row = (
                (await s.execute(m.Run.__table__.select().where(m.Run.kind == "explore")))
                .mappings()
                .first()
            )
        assert row is not None
        if row["state"] in ("succeeded", "failed"):
            return
        run, att = await _to_finalizing(ctx, row["id"])
        index = ExplorerIndex(
            target="dataset",
            ref=f"dataset://default/{ds.name}@v1",
            sections=[{"section": "stl", "view": "ui/explore/stl.json"}],  # type: ignore[list-item]
        )
        add = {
            EXPLORE_INDEX: (index.model_dump_json().encode(), "application/json"),
            "ui/explore/stl.json": (b'{"format": "mapi.data-explorer.stl/v1"}', "application/json"),
        }
        await simulate_outputs(ctx, run, att, add=add)
        assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED

    async with asgi_client(ctx, sleep=finish) as waiting:
        got = await waiting.datasets.explore(ds.id, 1, sections=["stl"], max_lag=12)
        assert got.done and got.ref.endswith("@v1")
        view = await waiting.explorations.view(got, "stl")
        assert view == {"format": "mapi.data-explorer.stl/v1"}
        assert (await waiting.explorations.get(got.run.id)).done
        cached = await waiting.datasets.explore(ds.id, 1, sections=["stl"], max_lag=12)
        assert cached.run.id == got.run.id  # asked before: answered from the cache


async def test_a_budget_is_optimised_while_the_client_waits(
    api: AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    async with ctx.db.session() as s:
        row = await s.get(m.ModelVersion, mv.id)
        assert row is not None
        row.capabilities = {**(row.capabilities or {}), "decision": True}
    result = OptimizeResult.model_validate(OptimizeResult.model_json_schema()["examples"][0])

    async def plan(_: float) -> None:
        async with ctx.db.session() as s:
            row = (
                (await s.execute(m.Run.__table__.select().where(m.Run.kind == "optimize")))
                .mappings()
                .first()
            )
        assert row is not None
        if row["state"] == "succeeded":
            return
        run, att = await _to_finalizing(ctx, row["id"])
        add = {OPTIMIZE_OUTPUT: (result.model_dump_json().encode(), "application/json")}
        await simulate_outputs(ctx, run, att, add=add)
        assert await Finalizer(ctx).finalize(run.id, att.attempt) is RunState.SUCCEEDED

    async with asgi_client(ctx, sleep=plan) as waiting:
        opt = await waiting.models.optimize(
            mv.model_id,
            mv.version,
            total_budget=1_000_000,
            channels={"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5}},
            objective="p10",
            wait=0,
        )
    assert opt.done and opt.result is not None
    assert opt.result.allocation == result.allocation
    assert (await api.models.optimization(opt.run.id)).done
