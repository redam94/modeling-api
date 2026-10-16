"""Endpoints and aliases (plan §8.2 "Endpoints", Q13): promotion gated on the scorecard,
overrides, history and rollback, scoring through the alias, the schema with an example,
static validation, snippets, and score:<alias> tokens."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mapi_server.auth.identity import LOCAL_USER_ID
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import queued_fit, simulate_outputs, submit_fit
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.identity import (
    grant_role,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_server_testkit.integrations import fitted_version, refit_version
from mapi_spec.endpoints import EndpointSchema, EndpointScoreCall
from mapi_spec.run import Actor, RunTrigger
from mapi_spec.scorecard import SCORECARD_OUTPUT
from mapi_spec.snippets import SNIPPET_LANGUAGES
from sqlalchemy import select

pytestmark = pytest.mark.integration


async def _set(ctx: ServerContext, mv_id: str, **values: Any) -> None:
    async with ctx.db.session() as s:
        mv = await s.get(m.ModelVersion, mv_id)
        assert mv is not None
        for k, v in values.items():
            setattr(mv, k, v)


async def _fit_mode(ctx: ServerContext, mv: m.ModelVersion, mode: str) -> None:
    async with ctx.db.session() as s:
        run = await s.get(m.Run, mv.run_id)
        assert run is not None
        payload = json.loads(json.dumps(run.payload))
        payload["model"].setdefault("fit", {})["mode"] = mode
        run.payload = payload


WARN = {
    "overall": "warn",
    "items": [
        {
            "code": "HOLDOUT_MAPE",
            "status": "warn",
            "title": "Holdout error",
            "message_plain": "Forecasts were off by 14%.",
            "check": "holdout.mape",
        }
    ],
}
FAIL = {**WARN, "overall": "fail", "items": [{**WARN["items"][0], "status": "fail"}]}


async def _audit_actions(ctx: ServerContext) -> list[tuple[str, str]]:
    async with ctx.db.session() as s:
        rows = (await s.execute(select(m.AuditEvent).order_by(m.AuditEvent.ts))).scalars()
        return [(r.action, r.outcome) for r in rows]


async def test_promote_get_list_history_and_idempotent_put(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    r = await client.put(
        "/v1/endpoints/trend-prod",
        json={"model": "model://default/trend@v1", "expected_revision": 0},
    )
    assert r.status_code == 200, r.text
    ep = r.json()
    assert ep["alias"] == "trend-prod" and ep["revision"] == 1
    assert ep["ref"] == "endpoint://default/trend-prod"
    assert ep["model_ref"] == "model://default/trend@v1" and ep["model_version_id"] == mv.id
    assert ep["gate"]["outcome"] == "allowed" and not ep["overridden"]
    assert ep["evaluation_policy"] == {"fit_mode": "evaluate_then_refit", "schema_change": "block"}
    snap = ep["schema_snapshot"]
    assert snap["inputs_stage"] == "raw" and snap["horizon_dim"] == "t"
    assert set(snap["score_modes"]) == {"evaluate", "forecast", "predict"}
    assert snap["fit_coords"]["t"]["end"] == "2023-12-01"
    # the same PUT again changes nothing (no new revision)
    again = await client.put("/v1/endpoints/trend-prod", json={"model_version_id": mv.id})
    assert again.status_code == 200 and again.json()["revision"] == 1
    assert (await client.get("/v1/endpoints/trend-prod")).json()["id"] == ep["id"]
    page = (await client.get("/v1/endpoints")).json()
    assert [e["alias"] for e in page["items"]] == ["trend-prod"]
    hist = (await client.get("/v1/endpoints/trend-prod/history")).json()["items"]
    assert [(h["revision"], h["action"]) for h in hist] == [(1, "promote")]
    assert hist[0]["actor_id"] == LOCAL_USER_ID
    # a stale expected_revision is a conflict
    r = await client.put(
        "/v1/endpoints/trend-prod", json={"model_version_id": mv.id, "expected_revision": 0,
                                          "description": "x"},
    )  # fmt: skip
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert (await client.get("/v1/endpoints/nope")).status_code == 404
    actions = await _audit_actions(ctx)
    assert ("endpoint.create", "success") in actions
    assert ("endpoint.promote", "success") in actions


async def test_the_gate_needs_an_admin_override_with_a_reason(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    await _set(ctx, mv.id, scorecard=WARN)
    r = await client.put("/v1/endpoints/trend-prod", json={"model_version_id": mv.id})
    assert r.status_code == 409 and r.json()["code"] == "OVERRIDE_REQUIRED"
    reasons = r.json()["errors"]
    assert reasons and reasons[0]["context"]["gate_code"] == "SCORECARD_WARN"
    assert reasons[0]["context"]["check"] == "holdout.mape"
    # an API token never overrides, not even an admin-scoped one of an admin
    owner = await make_user(ctx, "owner@default.test")
    await grant_role(ctx, owner, "default", "default", "admin")
    secret = await make_token(ctx, owner, "default", "default", ["admin"])
    tok = token_caller(client, secret)
    r = await tok.put(
        "/v1/endpoints/trend-prod",
        json={"model_version_id": mv.id, "override": True, "reason": "accepted by the CMO"},
    )
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    # a signed-in project admin can, with a reason (audited)
    admin = await session_caller(ctx, client, owner)
    r = await admin.put(
        "/v1/endpoints/trend-prod",
        json={"model_version_id": mv.id, "override": True, "reason": "accepted by the CMO"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["overridden"] and r.json()["gate"]["outcome"] == "needs_override"
    hist = (await client.get("/v1/endpoints/trend-prod/history")).json()["items"]
    assert hist[0]["reason"] == "accepted by the CMO" and hist[0]["overridden"]
    actions = await _audit_actions(ctx)
    assert ("endpoint.promote_override", "denied") in actions
    assert ("endpoint.promote_override", "success") in actions
    # a runner may not override either
    runner = await make_user(ctx, "runner@default.test")
    await grant_role(ctx, runner, "default", "default", "runner")
    rc = await session_caller(ctx, client, runner)
    r = await rc.put("/v1/endpoints/other", json={"model_version_id": mv.id})
    assert r.status_code == 403


async def test_failing_partial_and_train_only_versions_are_blocked(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    mv = await fitted_version(ctx)
    await _set(ctx, mv.id, scorecard=FAIL)
    r = await client.put(
        "/v1/endpoints/e1",
        json={"model_version_id": mv.id, "override": True, "reason": "please"},
    )
    assert r.status_code == 409 and r.json()["code"] == "PROMOTION_BLOCKED"
    await _set(ctx, mv.id, scorecard=None)
    r = await client.put("/v1/endpoints/e1", json={"model_version_id": mv.id})
    assert r.status_code == 409 and r.json()["code"] == "OVERRIDE_REQUIRED"  # NO_SCORECARD
    assert r.json()["errors"][0]["context"]["gate_code"] == "NO_SCORECARD"
    await _set(ctx, mv.id, scorecard={"overall": "pass", "items": []})
    # Q13: endpoints need evaluate_then_refit fits unless the policy allows train_only
    await _fit_mode(ctx, mv, "train_only")
    r = await client.put("/v1/endpoints/e1", json={"model_version_id": mv.id})
    assert r.status_code == 409 and r.json()["code"] == "PROMOTION_BLOCKED"
    assert r.json()["errors"][0]["context"]["gate_code"] == "TRAIN_ONLY"
    r = await client.put(
        "/v1/endpoints/e1",
        json={"model_version_id": mv.id, "evaluation_policy": {"fit_mode": "train_only"}},
    )
    assert r.status_code == 200 and r.json()["evaluation_policy"]["fit_mode"] == "train_only"
    await _set(ctx, mv.id, status="partial")
    r = await client.put("/v1/endpoints/e2", json={"model_version_id": mv.id})
    assert r.status_code == 409 and r.json()["code"] == "PROMOTION_BLOCKED"


async def test_breaking_input_changes_follow_the_schema_policy_and_rollback(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    v1 = await fitted_version(ctx)
    v2 = await refit_version(ctx, v1)
    assert (
        await client.put("/v1/endpoints/p", json={"model_version_id": v1.id})
    ).status_code == 200
    meta = json.loads(json.dumps(v2.score_meta))
    meta["resolved"]["stages"]["raw"]["vars"]["price"] = {
        "dims": ["t"], "dtype": "float64", "role": "covariate",
    }  # fmt: skip
    await _set(ctx, v2.id, score_meta=meta)
    r = await client.put("/v1/endpoints/p", json={"model_version_id": v2.id})
    assert r.status_code == 409 and r.json()["code"] == "PROMOTION_BLOCKED"
    assert "new required input 'price'" in r.json()["errors"][0]["message"]
    r = await client.put(
        "/v1/endpoints/p",
        json={"model_version_id": v2.id, "evaluation_policy": {"schema_change": "allow"}},
    )
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert "price" in r.json()["schema_snapshot"]["input_schema"]["vars"]
    # rollback to the version before (no gate re-run; audited with the reason)
    r = await client.post("/v1/endpoints/p/rollback", json={"reason": "v2 over-forecasts"})
    assert r.status_code == 200, r.text
    assert r.json()["model_version_id"] == v1.id and r.json()["revision"] == 3
    assert "price" not in r.json()["schema_snapshot"]["input_schema"]["vars"]
    hist = (await client.get("/v1/endpoints/p/history?limit=2")).json()
    assert [h["action"] for h in hist["items"]] == ["rollback", "promote"]
    assert hist["items"][0]["previous_model_version_id"] == v2.id
    rest = (await client.get(f"/v1/endpoints/p/history?cursor={hist['next_cursor']}")).json()
    assert [h["revision"] for h in rest["items"]] == [1]
    r = await client.post("/v1/endpoints/p/rollback", json={"to_revision": 2, "reason": "back"})
    assert r.json()["model_version_id"] == v2.id
    r = await client.post("/v1/endpoints/p/rollback", json={"to_revision": 9, "reason": "x"})
    assert r.status_code == 409
    assert ("endpoint.rollback", "success") in await _audit_actions(ctx)
    # delete: the alias goes, model versions stay
    assert (await client.delete("/v1/endpoints/p")).status_code == 204
    assert (await client.get("/v1/endpoints/p")).status_code == 404
    async with ctx.db.session() as s:
        assert await s.get(m.ModelVersion, v1.id) is not None
        left = (await s.execute(select(m.EndpointRevision))).scalars().all()
        assert left == []
    assert ("endpoint.delete", "success") in await _audit_actions(ctx)


async def test_a_laxer_evaluation_policy_needs_an_admin(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """A promoter (an ``endpoints:promote`` token) cannot switch off the schema or
    train-only gates by sending a laxer policy with the promotion: that is an admin's
    decision, like an override, and the policy change is audited."""
    v1 = await fitted_version(ctx)
    v2 = await refit_version(ctx, v1)
    assert (
        await client.put("/v1/endpoints/p", json={"model_version_id": v1.id})
    ).status_code == 200
    meta = json.loads(json.dumps(v2.score_meta))
    meta["resolved"]["stages"]["raw"]["vars"]["price"] = {
        "dims": ["t"], "dtype": "float64", "role": "covariate",
    }  # fmt: skip
    await _set(ctx, v2.id, score_meta=meta)
    owner = await make_user(ctx, "owner@default.test")
    await grant_role(ctx, owner, "default", "default", "admin")
    promo = token_caller(
        client, await make_token(ctx, owner, "default", "default", ["endpoints:promote"])
    )
    r = await promo.put("/v1/endpoints/p", json={"model_version_id": v2.id})
    assert r.status_code == 409 and r.json()["code"] == "PROMOTION_BLOCKED"
    lax = {"schema_change": "allow", "fit_mode": "train_only"}
    r = await promo.put("/v1/endpoints/p", json={"model_version_id": v2.id,
                                                 "evaluation_policy": lax})  # fmt: skip
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN", r.text
    assert "evaluation policy" in r.json()["detail"]
    ep = (await client.get("/v1/endpoints/p")).json()
    assert ep["revision"] == 1 and ep["evaluation_policy"]["schema_change"] == "block"
    # a new endpoint starts from the default policy: a token cannot create a lax one either
    r = await promo.put("/v1/endpoints/q", json={"model_version_id": v1.id,
                                                 "evaluation_policy": lax})  # fmt: skip
    assert r.status_code == 403
    # the same or a stricter policy is fine for the token
    strict = {"model_version_id": v1.id, "evaluation_policy": {"schema_change": "block"}}
    r = await promo.put("/v1/endpoints/q", json=strict)
    assert r.status_code == 200, r.text
    # a signed-in admin may relax it; the audit records the change
    admin = await session_caller(ctx, client, owner)
    r = await admin.put("/v1/endpoints/p", json={"model_version_id": v2.id,
                                                 "evaluation_policy": lax})  # fmt: skip
    assert r.status_code == 200 and r.json()["revision"] == 2, r.text
    async with ctx.db.session() as s:
        rows = (
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "endpoint.promote", m.AuditEvent.outcome == "success"
                    )
                )
            )
            .scalars()
            .all()
        )
        changed = [r for r in rows if "evaluation_policy" in (r.details or {})]
        [ev] = [r for r in changed if r.details["alias"] == "p"]
        assert ev.details["evaluation_policy"] == {
            "from": {"fit_mode": "evaluate_then_refit", "schema_change": "block"},
            "to": lax,
        }
    assert ("endpoint.promote", "denied") in await _audit_actions(ctx)


async def test_schema_example_validate_and_snippets(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await fitted_version(ctx)
    await client.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    schema = EndpointSchema.model_validate(
        (await client.get("/v1/endpoints/trend-prod/schema")).json()
    )
    assert schema.revision == 1 and schema.max_inline_bytes == 5 * 1024 * 1024
    example = EndpointScoreCall.model_validate(schema.example)
    assert example.mode == "forecast" and example.horizon is not None
    # the example validates against the endpoint
    v = await client.post("/v1/endpoints/trend-prod/validate", json=schema.example)
    assert v.status_code == 200 and v.json()["valid"], v.json()
    assert v.json()["request"]["model"] == "endpoint://default/trend-prod"
    # inline data that does not match the input contract
    bad = {
        "mode": "predict",
        "data": {
            "coords": {"t": {"dims": ["t"], "data": ["2024-01-01"]},
                       "geo": {"dims": ["geo"], "data": ["de"]}},
            "data_vars": {"spend": {"dims": ["t"], "data": [1.0]},
                          "y": {"dims": ["geo"], "data": [1.0]}},
        },
    }  # fmt: skip
    v = (await client.post("/v1/endpoints/trend-prod/validate", json=bad)).json()
    assert not v["valid"]
    pointers = {(i["code"], i["pointer"]) for i in v["issues"]}
    assert ("SCHEMA_INVALID", "/data/data_vars/spend") in pointers
    assert ("SCHEMA_INVALID", "/data/data_vars/y/dims") in pointers
    assert ("COORD_NOT_ALLOWED", "/data/coords/geo") in pointers
    # an unsupported mode is reported by the CoordPlan checks
    v = (
        await client.post(
            "/v1/endpoints/trend-prod/validate",
            json={"mode": "counterfactual", "interventions": [{"var": "y", "multiply": 2}]},
        )
    ).json()
    assert not v["valid"] and v["issues"][0]["code"] == "MODE_NOT_SUPPORTED"
    # snippets: every language, the server's base URL, the example payload, no secret
    snips = (await client.get("/v1/endpoints/trend-prod/snippets")).json()
    assert [s["language"] for s in snips["items"]] == [str(x) for x in SNIPPET_LANGUAGES]
    curl = next(s for s in snips["items"] if s["language"] == "curl")["code"]
    assert f"{ctx.settings.api_url}/v1/endpoints/trend-prod/score" in curl
    assert "MAPI_TOKEN" in curl and "mapi_" not in curl.replace("MAPI_TOKEN", "")
    one = (
        await client.get(
            "/v1/endpoints/trend-prod/snippets",
            params={"language": "python", "base_url": "https://api.example.com"},
        )
    ).json()
    assert [s["language"] for s in one["items"]] == ["python"]
    assert "https://api.example.com" in one["items"][0]["code"]
    r = await client.get(
        "/v1/endpoints/trend-prod/snippets", params={"base_url": "https://x.com/'; rm -rf"}
    )
    assert r.status_code == 422


async def test_scoring_through_the_alias(client: httpx.AsyncClient, ctx: ServerContext) -> None:
    await fitted_version(ctx)
    await client.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    body = {"mode": "forecast", "horizon": {"dim": "t", "periods": 6}}
    r = await client.post(
        "/v1/endpoints/trend-prod/score",
        json=body,
        headers={"Prefer": "wait=1", "Idempotency-Key": "score-1"},
    )
    assert r.status_code == 202, r.text  # nobody runs it in this test: still queued
    assert r.headers["preference-applied"] == "wait=1"
    result = r.json()
    run_id = result["run"]["id"]
    assert result["run"]["kind"] == "score" and not result["done"]
    assert r.headers["location"] == f"/v1/runs/{run_id}"
    again = await client.post(
        "/v1/endpoints/trend-prod/score", json=body, headers={"Idempotency-Key": "score-1"}
    )
    assert again.json()["run"]["id"] == run_id  # the same run (collect the result later)
    clash = await client.post(
        "/v1/endpoints/trend-prod/score",
        json={**body, "seed": 3},
        headers={"Idempotency-Key": "score-1"},
    )
    assert clash.status_code == 409 and clash.json()["code"] == "IDEMPOTENCY_CONFLICT"
    async with ctx.db.session() as s:
        run = await s.get(m.Run, run_id)
        via = await s.get(m.EndpointRun, run_id)
        assert run is not None and via is not None
        assert run.payload["model"] == "model://default/trend@v1"
        assert run.payload["horizon"] == {"dim": "t", "periods": 6}
        assert (via.alias, via.revision) == ("trend-prod", 1)
    # inline data is stored (content-addressed) as a run input, decoded only in the sandbox
    inline = {
        "mode": "predict",
        "data": {
            "coords": {"t": {"dims": ["t"], "data": ["2023-11-01", "2023-12-01"]}},
            "data_vars": {"y": {"dims": ["t"], "data": [1.0, 2.0]}},
        },
    }
    r = await client.post("/v1/endpoints/trend-prod/score", json=inline)
    assert r.status_code == 202, r.text
    async with ctx.db.session() as s:
        run = await s.get(m.Run, r.json()["run"]["id"])
        assert run is not None
        ref = run.inputs["inline.data"]
        assert ref["bucket"] == ctx.buckets.quarantine and "/inline/" in ref["name"]
        assert "data" not in run.payload
    stored = json.loads(await ctx.store.get(ref["bucket"], ref["name"]))
    assert stored["data_vars"]["y"]["data"] == [1.0, 2.0]
    # invalid calls are refused before anything is created
    r = await client.post(
        "/v1/endpoints/trend-prod/score",
        json={"mode": "forecast", "horizon": {"dim": "nope", "periods": 2}},
    )
    assert r.status_code == 422 and r.json()["code"] == "COORD_NOT_ALLOWED"


async def test_score_bodies_are_capped_and_depth_checked(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await fitted_version(ctx)
    await client.put("/v1/endpoints/e", json={"model": "model://default/trend@v1"})
    deep = "[" * 5000 + "]" * 5000
    r = await client.post(
        "/v1/endpoints/e/score",
        content=f'{{"mode":"predict","labels":{deep}}}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    big = b'{"mode":"predict","labels":{"a":"' + b"x" * (11 * 1024 * 1024) + b'"}}'
    r = await client.post(
        "/v1/endpoints/e/score", content=big, headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE"
    # a single inline payload over 5 MB
    payload = {
        "mode": "predict",
        "data": {
            "coords": {"t": {"dims": ["t"], "data": ["2023-12-01"]}},
            "data_vars": {
                "y": {
                    "dims": ["t"],
                    "data": [1.0],
                    "attrs": {f"k{i}": "x" * 1000 for i in range(60)},
                }
            },
            "attrs": {f"a{i}": "y" * 1000 for i in range(60)},
        },
    }
    payload["data"]["data_vars"].update(  # type: ignore[index]
        {f"v{i}": {"dims": ["t"], "data": ["z" * 1000]} for i in range(5500)}
    )
    r = await client.post("/v1/endpoints/e/validate", json=payload)
    assert r.status_code in (413, 422)


async def test_score_alias_tokens_authorize_exactly_their_endpoint(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    await fitted_version(ctx)
    for alias in ("trend-prod", "other"):
        r = await client.put(f"/v1/endpoints/{alias}", json={"model": "model://default/trend@v1"})
        assert r.status_code == 200
    async with ctx.db.session() as s:
        owner = await s.get(m.User, LOCAL_USER_ID)
        assert owner is not None
    secret = await make_token(ctx, owner, "default", "default", ["score:trend-prod"])
    tok = token_caller(client, secret)
    body = {"mode": "forecast", "horizon": {"dim": "t", "periods": 2}}
    assert (await tok.post("/v1/endpoints/trend-prod/score", json=body)).status_code == 202
    assert (await tok.get("/v1/endpoints/trend-prod/schema")).status_code == 200
    assert (await tok.post("/v1/endpoints/other/score", json=body)).status_code == 403
    assert (await tok.get("/v1/endpoints/other")).status_code == 403
    r = await tok.put("/v1/endpoints/trend-prod", json={"model": "model://default/trend@v1"})
    assert r.status_code == 403
    assert (await tok.get("/v1/endpoints")).status_code == 403
    # score:* covers every endpoint; endpoints:promote promotes but cannot score
    star = token_caller(client, await make_token(ctx, owner, "default", "default", ["score:*"]))
    assert (await star.post("/v1/endpoints/other/score", json=body)).status_code == 202
    promo = token_caller(
        client, await make_token(ctx, owner, "default", "default", ["endpoints:promote"])
    )
    assert (await promo.post("/v1/endpoints/other/score", json=body)).status_code == 403
    r = await promo.put("/v1/endpoints/third", json={"model": "model://default/trend@v1"})
    assert r.status_code == 200
    # the per-token rate limit and vCPU budget apply to endpoint calls
    limited = token_caller(
        client,
        await make_token(
            ctx, owner, "default", "default", ["score:trend-prod"], rate_limit_per_minute=1
        ),
    )
    assert (await limited.get("/v1/endpoints/trend-prod/schema")).status_code == 200
    r = await limited.get("/v1/endpoints/trend-prod/schema")
    assert r.status_code == 429 and "retry-after" in r.headers
    broke = token_caller(
        client,
        await make_token(
            ctx, owner, "default", "default", ["score:trend-prod"], vcpu_seconds_budget=0.001
        ),
    )
    r = await broke.post("/v1/endpoints/trend-prod/score", json=body)
    assert r.status_code == 429 and r.json()["code"] == "QUOTA_EXCEEDED"


def test_openapi_documents_the_endpoint_operations() -> None:
    from mapi_server.app import openapi_document
    from mapi_spec.api import M3_ENDPOINTS

    doc = openapi_document()
    served = {
        (method.upper(), path, op.get("operationId"))
        for path, item in doc["paths"].items()
        for method, op in item.items()
        if isinstance(op, dict)
    }
    mine = [e for e in M3_ENDPOINTS if e.path.startswith(("/v1/endpoints", "/v1/webhooks"))]
    assert len(mine) == 18
    missing = [e.operation_id for e in mine if (e.method, e.path, e.operation_id) not in served]
    assert not missing
    for e in mine:
        op = doc["paths"][e.path][e.method.lower()]
        for code in e.errors:
            from mapi_spec.errors import ERROR_INFO

            status = str(ERROR_INFO[code].status)
            assert status in op["responses"], (e.operation_id, code)
            content = op["responses"][status]["content"]["application/problem+json"]
            assert code.value in content["examples"]
            assert content["schema"] == {"$ref": "#/components/schemas/Problem"}
    score = doc["paths"]["/v1/endpoints/{alias}/score"]["post"]
    assert "202" in score["responses"]
    body = score["requestBody"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/EndpointScoreCall"}
    assert doc["components"]["schemas"]["EndpointScoreCall"]["examples"]
    assert "Problem" in doc["components"]["schemas"]


# ============================================================ the runner's scorecard (M3)

RUNNER_CARD: dict[str, Any] = {
    "overall": "warn",
    "items": [
        {
            "code": "PLAUSIBILITY_NEGATIVE_ROI",
            "status": "warn",
            "title": "Negative ROI",
            "message_plain": "One channel has a likely negative return.",
            "check": "plausibility.negative_roi",
        }
    ],
    "skipped": {"holdout.coverage": "no interval forecasts"},
}


async def _fit_with_scorecard(
    ctx: ServerContext, card: dict[str, Any] | None, *, spec_id: str | None = None
) -> m.ModelVersion:
    run_id = await (submit_fit(ctx, spec_id) if spec_id else queued_fit(ctx))
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run.id, run.attempt))
        assert att is not None
    add = None
    if card is not None:
        add = {SCORECARD_OUTPUT: (json.dumps(card).encode(), "application/json")}
    await simulate_outputs(ctx, run, att, add=add)
    await Finalizer(ctx).finalize(run.id, run.attempt)
    async with ctx.db.session() as s:
        done = await load_run(s, None, run_id)
        mv = await s.get(m.ModelVersion, done.output_model_version_id)
        assert mv is not None
        return mv


async def test_promotion_gates_on_the_runner_scorecard(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    mv = await _fit_with_scorecard(ctx, RUNNER_CARD)
    assert mv.scorecard is not None and mv.scorecard["skipped"] == RUNNER_CARD["skipped"]
    assert mv.scorecard["items"][0]["check"] == "plausibility.negative_roi"
    r = await client.put("/v1/endpoints/mmm-prod", json={"model_version_id": mv.id})
    assert r.status_code == 409 and r.json()["code"] == "OVERRIDE_REQUIRED", r.text
    [reason] = r.json()["errors"]
    assert reason["context"]["check"] == "plausibility.negative_roi"
    async with ctx.db.session() as s:
        fit = await s.get(m.Run, mv.run_id)
        assert fit is not None and fit.spec_id is not None
    ok = await _fit_with_scorecard(
        ctx, {**RUNNER_CARD, "overall": "pass", "items": []}, spec_id=fit.spec_id
    )
    r = await client.put("/v1/endpoints/mmm-prod", json={"model_version_id": ok.id})
    assert r.status_code == 200, r.text
    assert r.json()["gate"]["outcome"] == "allowed"
