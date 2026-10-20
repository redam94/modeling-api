"""Run tracking over HTTP (v0.11): PATCH a run, metric series and history, params, API
writes (audit, budgets, conflicts, permissions), run search, log levels, retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mapi_server.api import tracking as tracking_api
from mapi_server.api.common import encode_cursor
from mapi_server.auth.identity import LOCAL_USER_ID
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.housekeeping import Housekeeper
from mapi_server.services.runs import Scope, create_run
from mapi_server_testkit.identity import (
    grant_role,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.executor import RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.tracking import TrackingLimits
from sqlalchemy import func, select

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


async def _run(ctx: ServerContext, **fields: Any) -> str:
    async with ctx.db.session() as s:
        run = await create_run(
            s,
            Scope("default", "default"),
            kind=RunKind.FIT,
            resource_class=ResourceClass.S,
            timeout_s=600,
            payload={},
        )
        for k, v in fields.items():
            setattr(run, k, v)
        return run.id


async def _audit(ctx: ServerContext, action: str) -> list[m.AuditEvent]:
    async with ctx.db.session() as s:
        rows = await s.execute(select(m.AuditEvent).where(m.AuditEvent.action == action))
        return list(rows.scalars())


def _points(key: str, n: int, *, labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "value": float(i % 7) + 0.5,
            "step": i,
            "ts": (T0 + timedelta(seconds=i)).isoformat(),
            "labels": labels or {},
        }
        for i in range(n)
    ]


# ============================================================================== metrics


async def test_log_metrics_then_list_series_and_read_history(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    body = {
        "metrics": [
            *_points("loss", 300, labels={"chain": "0"}),
            *_points("loss", 5, labels={"chain": "1"}),
            {"key": "rmse", "value": 1.5, "ts": T0.isoformat()},
        ]
    }
    r = await client.post(f"/v1/runs/{run_id}/metrics", json=body)
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 306, "duplicates": 0}
    r = await client.post(f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 3)})
    assert r.json() == {"accepted": 3, "duplicates": 0}  # the unlabelled series is another one
    r = await client.post(
        f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 3, labels={"chain": "1"})}
    )
    assert r.json() == {"accepted": 0, "duplicates": 3}
    # a newer scalar replaces the stored one, an older one does not
    for ts, value in ((T0 + timedelta(hours=1), 0.9), (T0 - timedelta(hours=1), 9.0)):
        r = await client.post(
            f"/v1/runs/{run_id}/metrics",
            json={"metrics": [{"key": "rmse", "value": value, "ts": ts.isoformat()}]},
        )
    assert r.json() == {"accepted": 0, "duplicates": 1}

    page = (await client.get(f"/v1/runs/{run_id}/metrics?limit=2")).json()
    assert [(s["key"], s["labels"]) for s in page["items"]] == [
        ("loss", {}),
        ("loss", {"chain": "0"}),
    ]
    rest = (await client.get(f"/v1/runs/{run_id}/metrics?cursor={page['next_cursor']}")).json()
    assert [(s["key"], s["labels"]) for s in rest["items"]] == [
        ("loss", {"chain": "1"}),
        ("rmse", {}),
    ]
    assert rest["next_cursor"] is None
    rmse = rest["items"][1]
    assert (rmse["count"], rmse["last"], rmse["last_step"]) == (1, 0.9, None)
    chain0 = page["items"][1]
    assert (chain0["count"], chain0["last_step"], chain0["min"], chain0["max"]) == (
        300,
        299,
        0.5,
        6.5,
    )
    only = (await client.get(f"/v1/runs/{run_id}/metrics?key=rmse")).json()["items"]
    assert [s["key"] for s in only] == ["rmse"]
    by_attempt = (await client.get(f"/v1/runs/{run_id}/metrics?attempt=1&key=loss")).json()
    assert [s["count"] for s in by_attempt["items"]] == [3, 300, 5]
    assert (await client.get(f"/v1/runs/{run_id}/metrics?attempt=2")).json()["items"] == []

    hist = (await client.get(f"/v1/runs/{run_id}/metrics/loss?max_points=10")).json()
    assert hist["key"] == "loss" and not hist["truncated"]
    assert [s["labels"] for s in hist["series"]] == [{}, {"chain": "0"}, {"chain": "1"}]
    long = hist["series"][1]
    assert long["downsampled"] and long["count"] == 300 and len(long["steps"]) == 10
    assert long["steps"][0] == 0 and long["steps"][-1] == 299
    assert len(long["values"]) == len(long["ts"]) == 10
    short = hist["series"][2]
    assert not short["downsampled"] and short["steps"] == [0, 1, 2, 3, 4]
    one = (await client.get(f"/v1/runs/{run_id}/metrics/loss?label=chain:1")).json()["series"]
    assert [s["labels"] for s in one] == [{"chain": "1"}]
    scalar = (await client.get(f"/v1/runs/{run_id}/metrics/rmse")).json()["series"]
    assert scalar[0]["steps"] == [None] and scalar[0]["values"] == [0.9]
    assert (await client.get(f"/v1/runs/{run_id}/metrics/nope")).json()["series"] == []
    bad = await client.get(f"/v1/runs/{run_id}/metrics/loss?label=chain")
    assert bad.status_code == 422 and bad.json()["code"] == "SCHEMA_INVALID"
    bad = await client.get(f"/v1/runs/{run_id}/metrics/loss?max_points=5")
    assert bad.status_code == 422
    assert (await client.get("/v1/runs/run_nope/metrics")).status_code == 404

    logged = await _audit(ctx, "run.log")
    assert [a.details for a in logged][:1] == [{"metrics": 306}]


async def test_metric_writes_over_the_budget_are_refused_whole(
    client: httpx.AsyncClient, ctx: ServerContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracking_api, "TrackingLimits", lambda: TrackingLimits(metric_points_per_series=5)
    )
    run_id = await _run(ctx)
    r = await client.post(f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 4)})
    assert r.json()["accepted"] == 4
    r = await client.post(f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 10)})
    assert r.status_code == 429 and r.json()["code"] == "QUOTA_EXCEEDED"
    async with ctx.db.session() as s:
        n = (await s.execute(select(func.count()).select_from(m.RunMetric))).scalar_one()
    assert n == 4
    r = await client.post(
        f"/v1/runs/{run_id}/metrics",
        content=b'{"metrics": [{"key": "x", "value": NaN}]}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


# =============================================================================== params


async def test_params_are_write_once_with_conflicts_refused(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    url = f"/v1/runs/{run_id}/params"
    r = await client.post(
        url, json={"params": {"analyst": "kim", "holdout_weeks": 8, "lr": 0.1, "ok": True}}
    )
    assert r.status_code == 200 and r.json() == {"accepted": 4, "unchanged": 0}
    r = await client.post(url, json={"params": {"analyst": "kim", "new.key": "x"}})
    assert r.json() == {"accepted": 1, "unchanged": 1}
    r = await client.post(url, json={"params": {"holdout_weeks": 9, "other": 1}})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert "holdout_weeks" in r.json()["detail"]
    page = (await client.get(f"{url}?limit=3")).json()
    assert [p["key"] for p in page["items"]] == ["analyst", "holdout_weeks", "lr"]
    assert "other" not in [p["key"] for p in page["items"]]  # nothing of the 409 was written
    rest = (await client.get(f"{url}?cursor={page['next_cursor']}")).json()
    assert [p["key"] for p in rest["items"]] == ["new.key", "ok"]
    weeks = page["items"][1]
    assert (weeks["value"], weeks["value_num"], weeks["source"]) == ("8", 8.0, "api")
    assert rest["items"][1]["value"] == "true" and rest["items"][1]["value_num"] is None
    pre = (await client.get(f"{url}?prefix=new.")).json()["items"]
    assert [p["key"] for p in pre] == ["new.key"]
    assert (await client.get(f"{url}?prefix=%25")).json()["items"] == []  # LIKE is escaped
    assert [a.details for a in await _audit(ctx, "run.log")] == [{"params": 4}, {"params": 2}]


# ================================================================================ PATCH


async def test_patch_merges_name_note_and_tags_and_is_audited(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    url = f"/v1/runs/{run_id}"
    r = await client.patch(
        url,
        json={"name": "  adstock 4  ", "note": "secret note", "tags": {"stage": "a", "x": "1"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["note"], body["tags"]) == (
        "adstock 4",
        "secret note",
        {"stage": "a", "x": "1"},
    )
    r = await client.patch(url, json={"tags": {"x": None, "y": "2"}})
    body = r.json()
    assert body["name"] == "adstock 4" and body["note"] == "secret note"  # left out: unchanged
    assert body["tags"] == {"stage": "a", "y": "2"}
    r = await client.patch(url, json={"name": None, "note": None})
    assert (r.json()["name"], r.json()["note"]) == (None, None)
    assert (await client.get(url)).json()["tags"] == {"stage": "a", "y": "2"}
    listed = (await client.get("/v1/runs")).json()["items"]
    assert listed[0]["tags"] == {"stage": "a", "y": "2"}

    for bad in ({}, {"tags": {"mapi.kind": "x"}}, {"tags": {"bad key": "x"}}):
        r = await client.patch(url, json=bad)
        assert r.status_code == 422, bad
    many = {f"k{i}": "v" for i in range(99)}
    r = await client.patch(url, json={"tags": many})  # 2 + 99 > 100
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA_INVALID"
    assert (await client.get(url)).json()["tags"] == {"stage": "a", "y": "2"}
    assert (await client.patch("/v1/runs/run_nope", json={"name": "x"})).status_code == 404

    audits = await _audit(ctx, "run.update")
    assert [a.details for a in audits] == [
        {"fields": ["name", "note", "tags"], "tags_set": 2, "tags_deleted": 0},
        {"fields": ["tags"], "tags_set": 1, "tags_deleted": 1},
        {"fields": ["name", "note"], "tags_set": 0, "tags_deleted": 0},
    ]
    assert all("secret" not in str(a.details) for a in audits)


async def test_writes_need_run_update_and_reads_run_read(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    viewer = await make_user(ctx, "viewer@default.test")
    await grant_role(ctx, viewer, "default", "default", "viewer")
    vc = await session_caller(ctx, client, viewer)
    assert (await vc.get(f"/v1/runs/{run_id}/metrics")).status_code == 200
    assert (await vc.get(f"/v1/runs/{run_id}/params")).status_code == 200
    assert (await vc.get(f"/v1/runs/{run_id}/metrics/loss")).status_code == 200
    assert (await vc.patch(f"/v1/runs/{run_id}", json={"name": "x"})).status_code == 403
    r = await vc.post(f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 1)})
    assert r.status_code == 403
    assert (
        await vc.post(f"/v1/runs/{run_id}/params", json={"params": {"a": 1}})
    ).status_code == 403

    async with ctx.db.session() as s:
        owner = await s.get(m.User, LOCAL_USER_ID)
        assert owner is not None
    read_only = token_caller(
        client, await make_token(ctx, owner, "default", "default", ["models:read"])
    )
    assert (await read_only.get(f"/v1/runs/{run_id}/metrics")).status_code == 200
    r = await read_only.post(f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 1)})
    assert r.status_code == 403
    writer = token_caller(
        client, await make_token(ctx, owner, "default", "default", ["runs:write"])
    )
    r = await writer.post(f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 1)})
    assert r.status_code == 200, r.text
    assert (await writer.patch(f"/v1/runs/{run_id}", json={"name": "ci"})).status_code == 200
    assert (await writer.post(f"/v1/runs/{run_id}:cancel")).status_code == 403


# =========================================================================== run search


async def _metric(client: httpx.AsyncClient, run_id: str, key: str, value: float) -> None:
    r = await client.post(
        f"/v1/runs/{run_id}/metrics", json={"metrics": [{"key": key, "value": value}]}
    )
    assert r.status_code == 200, r.text


async def test_list_runs_filters_orders_and_columns(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    parent = await _run(ctx, name="sweep")
    a = await _run(ctx, name="Alpha run", parent_run_id=parent)
    b = await _run(ctx, name="beta", parent_run_id=parent)
    c = await _run(ctx, name="gamma 100%")
    for run_id, rmse in ((a, 0.5), (b, 0.2), (c, 0.9)):
        await _metric(client, run_id, "rmse", rmse)
    await client.post(f"/v1/runs/{a}/metrics", json={"metrics": _points("loss", 3)})
    await client.post(f"/v1/runs/{a}/params", json={"params": {"model": "ts.trend", "draws": 1000}})
    await client.post(f"/v1/runs/{b}/params", json={"params": {"model": "ts.ets", "draws": 200}})
    await client.patch(f"/v1/runs/{a}", json={"tags": {"stage": "prod", "team": "x"}})
    await client.patch(f"/v1/runs/{b}", json={"tags": {"stage": "dev"}})

    async def ids(query: str) -> set[str]:
        r = await client.get(f"/v1/runs?{query}")
        assert r.status_code == 200, r.text
        return {x["id"] for x in r.json()["items"]}

    assert await ids("filter=metric.rmse < 0.6") == {a, b}
    assert await ids("filter=metric.rmse >= 0.5&filter=metric.rmse != 0.9") == {a}
    assert await ids("filter=metric.loss.count = 3") == {a}
    assert await ids("filter=metric.loss.max > 2") == {a}
    assert await ids("filter=metric.rmse{chain=0} < 5") == set()  # another series
    assert await ids("filter=param.model = ts.ets") == {b}
    assert await ids("filter=param.model ~ TS.") == {a, b}
    assert await ids("filter=param.draws >= 500") == {a}
    assert await ids("filter=param.model != ts.ets") == {a}  # runs without it never match
    assert await ids("filter=tag.stage = prod") == {a}
    assert await ids("filter=tag.stage ~ DE") == {b}
    assert await ids("tag=stage") == {a, b}
    assert await ids("tag=stage:dev") == {b}
    assert await ids("tag=stage&tag=team") == {a}
    assert await ids("filter=name ~ ALPHA") == {a}
    assert await ids("filter=name = beta") == {b}
    assert await ids("name=100%25") == {c}  # % is literal
    assert await ids("name=A") == {a, b, c}
    assert await ids(f"parent_run_id={parent}") == {a, b}

    r = await client.get(
        "/v1/runs?order_by=metric.rmse&column=metric.rmse&column=param.draws&column=tag.stage"
        "&column=name&column=param.model"
    )
    items = r.json()["items"]
    assert [x["id"] for x in items[:3]] == [b, a, c]  # ascending, runs without rmse last
    assert items[0]["columns"] == {
        "metric.rmse": 0.2,
        "param.draws": 200.0,
        "tag.stage": "dev",
        "name": "beta",
        "param.model": "ts.ets",
    }
    assert items[3]["id"] == parent and items[3]["columns"]["metric.rmse"] is None
    assert "columns" not in (await client.get("/v1/runs")).json()["items"][0]

    # keyset paging over a metric (descending), the run without the value last
    seen: list[str] = []
    cursor = None
    while True:
        q = "order_by=-metric.rmse&limit=1" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(f"/v1/runs?{q}")).json()
        seen += [x["id"] for x in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [c, a, b, parent]
    # a param order: numbers first, then runs without it (newest first)
    page = (await client.get("/v1/runs?order_by=-param.draws&limit=2")).json()
    assert [x["id"] for x in page["items"]] == [a, b]
    rest = (await client.get(f"/v1/runs?order_by=-param.draws&cursor={page['next_cursor']}")).json()
    assert {x["id"] for x in rest["items"]} == {c, parent}
    # the default order (newest first) still pages by id
    first = (await client.get("/v1/runs?limit=2")).json()
    nxt = (await client.get(f"/v1/runs?limit=2&cursor={first['next_cursor']}")).json()
    assert len({x["id"] for x in first["items"] + nxt["items"]}) == 4
    oldest = (await client.get("/v1/runs?order_by=created_at")).json()["items"]
    assert [x["id"] for x in oldest] == sorted(x["id"] for x in oldest)

    for bad in (
        "filter=metric.rmse ~ 1",
        "filter=metric.train/loss < 1",
        "filter=created_at > 1",
        "filter=metric.rmse < abc",
        "order_by=metric.",
        "column=bogus",
        "tag=bad key",
        "&".join(["filter=metric.rmse < 1"] * 9),
        "order_by=-metric.rmse&cursor=" + first["next_cursor"],
        # a cursor value of the wrong type for its column is a 422, not a database error
        "order_by=metric.rmse&cursor=" + encode_cursor(["abc", a]),
        "order_by=metric.rmse&cursor=" + encode_cursor([True, a]),
        "order_by=param.draws&cursor=" + encode_cursor(["abc", "x", a]),
        "order_by=param.draws&cursor=" + encode_cursor([1, 2, a]),
        "order_by=tag.stage&cursor=" + encode_cursor([1.5, a]),
    ):
        r = await client.get(f"/v1/runs?{bad}")
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["code"] == "SCHEMA_INVALID"


# ================================================================================= logs


async def test_run_logs_filter_by_minimum_level(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    run_id = await _run(ctx)
    async with ctx.db.session() as s:
        for seq, (stream, level) in enumerate(
            (("stdout", None), ("user", "debug"), ("user", "info"), ("user", "error"))
        ):
            s.add(
                m.RunLog(
                    run_id=run_id,
                    tenant_id="default",
                    project_id="default",
                    attempt=1,
                    seq=seq,
                    ts=T0,
                    stream=stream,
                    line=f"line {seq}",
                    level=level,
                    fields={"n": seq} if stream == "user" else None,
                )
            )

    async def lines(query: str) -> list[str]:
        r = await client.get(f"/v1/runs/{run_id}/logs?{query}")
        assert r.status_code == 200, r.text
        return [x["line"] for x in r.json()["items"]]

    # a client older than v0.11 validates the stream as stdout/stderr/system: it never
    # gets a user line unless it asks for them
    assert await lines("") == ["line 0"]
    assert await lines("include_user=true") == ["line 0", "line 1", "line 2", "line 3"]
    assert await lines("include_user=true&level=debug") == ["line 0", "line 1", "line 2", "line 3"]
    assert await lines("include_user=true&level=info") == ["line 0", "line 2", "line 3"]
    assert await lines("include_user=true&level=warning") == ["line 3"]
    assert await lines("level=warning") == []
    assert await lines("stream=user&level=info") == ["line 2", "line 3"]
    item = (await client.get(f"/v1/runs/{run_id}/logs?stream=user&level=error")).json()["items"][0]
    assert item["level"] == "error" and item["fields"] == {"n": 3}
    plain = (await client.get(f"/v1/runs/{run_id}/logs?stream=stdout")).json()["items"][0]
    assert plain["level"] is None and "fields" not in plain
    assert (await client.get(f"/v1/runs/{run_id}/logs?level=loud")).status_code == 422


# ============================================================================ retention


async def test_retention_prunes_points_and_user_lines_of_old_runs(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    now = datetime.now(UTC)
    old = await _run(ctx, finished_at=now - timedelta(days=40), state="succeeded")
    recent = await _run(ctx, finished_at=now - timedelta(days=2), state="succeeded")
    live = await _run(ctx)
    for run_id in (old, recent, live):
        await client.post(f"/v1/runs/{run_id}/metrics", json={"metrics": _points("loss", 3)})
        await client.post(f"/v1/runs/{run_id}/params", json={"params": {"p": 1}})
        async with ctx.db.session() as s:
            for seq, stream in enumerate(("user", "stdout")):
                s.add(
                    m.RunLog(
                        run_id=run_id,
                        tenant_id="default",
                        project_id="default",
                        attempt=1,
                        seq=seq,
                        ts=T0,
                        stream=stream,
                        line="x",
                    )
                )

    async def counts() -> dict[str, tuple[int, int, int, int]]:
        out = {}
        async with ctx.db.session() as s:
            for run_id in (old, recent, live):
                row = []
                for model, extra in (
                    (m.RunMetric, ()),
                    (m.RunMetricSummary, ()),
                    (m.RunParam, ()),
                    (m.RunLog, (m.RunLog.stream == "user",)),
                ):
                    q = select(func.count()).select_from(model).where(model.run_id == run_id)
                    row.append(int((await s.execute(q.where(*extra))).scalar_one()))
                out[run_id] = tuple(row)
        return out  # type: ignore[return-value]

    hk = Housekeeper(ctx)
    assert await hk.prune_tracking(now=now) == 0  # off by default
    ctx.settings = ctx.settings.model_copy(update={"tracking_retention_days": 30})
    assert await hk.prune_tracking(now=now) == 4  # 3 points + 1 user line
    after = await counts()
    assert after[old] == (0, 1, 1, 0)  # summaries and params stay
    assert after[recent] == after[live] == (3, 1, 1, 1)
    async with ctx.db.session() as s:
        stdout = await s.execute(
            select(func.count()).select_from(m.RunLog).where(m.RunLog.run_id == old)
        )
        assert stdout.scalar_one() == 1  # captured lines are not tracking data


async def test_a_write_after_retention_merges_into_the_kept_summary(
    client: httpx.AsyncClient, ctx: ServerContext
) -> None:
    """Retention deletes the stepped points and keeps the summary (and a scalar's one point);
    a later write to the series adds to that summary instead of replacing it."""
    now = datetime.now(UTC)
    old = await _run(ctx, finished_at=now - timedelta(days=40), state="succeeded")
    await client.post(f"/v1/runs/{old}/metrics", json={"metrics": _points("loss", 5)})
    await _metric(client, old, "kpi", 3.0)
    ctx.settings = ctx.settings.model_copy(update={"tracking_retention_days": 30})
    assert await Housekeeper(ctx).prune_tracking(now=now) == 5  # the scalar point stays
    late_ts = (T0 + timedelta(hours=1)).isoformat()
    late = [{"key": "loss", "value": 0.25, "step": 50, "ts": late_ts}]
    r = await client.post(f"/v1/runs/{old}/metrics", json={"metrics": late})
    assert r.status_code == 200, r.text
    await _metric(client, old, "kpi", 4.0)
    series = {s["key"]: s for s in (await client.get(f"/v1/runs/{old}/metrics")).json()["items"]}
    loss = series["loss"]
    assert loss["count"] == 6
    assert (loss["min"], loss["max"]) == (0.25, 4.5)
    assert datetime.fromisoformat(loss["first_ts"]) == T0
    assert (loss["last"], loss["last_step"]) == (0.25, 50)
    kpi = series["kpi"]
    assert (kpi["count"], kpi["last"], kpi["min"], kpi["max"]) == (1, 4.0, 4.0, 4.0)
