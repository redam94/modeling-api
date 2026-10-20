"""Run comparison (v0.14): ``GET /v1/run-compare``, 2 to 10 runs side by side.

Built on the v0.11 tracking store, nothing is stored for it:

* **summaries**: every metric series summary of the runs (at most 1000);
* **metrics**: the histories of the chosen keys (default: the
  :data:`~mapi_spec.tracking.MAX_COMPARE_KEYS` keys most of the runs have, ties by key), one
  :func:`~mapi_server.services.tracking.metric_history` per (run, key), at most
  :data:`~mapi_spec.tracking.MAX_COMPARE_SERIES` series each, downsampled to ``max_points``;
* **params**: the union of the param keys (at most
  :data:`~mapi_spec.tracking.MAX_COMPARE_PARAMS`, key order) with each run's text value and
  whether the runs differ (a run without the key differs too);
* **media**: for each run and chosen media key, every series (key + labels) with its items
  thinned evenly to :data:`~mapi_spec.tracking.MAX_COMPARE_MEDIA_ITEMS` (the last is kept),
  with signed links.

``truncated`` says which part was capped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from mapi_spec.api import (
    CompareMedia,
    CompareMetric,
    CompareParam,
    CompareSeries,
    CompareSummary,
    Run,
    RunCompare,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tracking import (
    MAX_COMPARE_KEYS,
    MAX_COMPARE_MEDIA_ITEMS,
    MAX_COMPARE_MEDIA_KEYS,
    MAX_COMPARE_PARAMS,
    MAX_COMPARE_RUNS,
    MAX_COMPARE_SERIES,
    check_metric_key,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.cost import run_costs
from mapi_server.services.media import media_views
from mapi_server.services.runs import Scope, run_dto
from mapi_server.services.tracking import metric_history, series_dto, tags_by_run

__all__ = ["MAX_COMPARE_SUMMARIES", "MAX_MEDIA_SERIES_PER_KEY", "compare_runs", "thin"]

MAX_COMPARE_SUMMARIES = 1000
MAX_MEDIA_SERIES_PER_KEY = 16
"""Media series (labels) per run and media key in a comparison."""

Part = Literal["summaries", "series", "params", "media"]


def _invalid(detail: str, pointer: str) -> MapiError:
    return MapiError(detail, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


def thin(n: int, k: int) -> list[int]:
    """At most ``k`` indices of ``n`` items, evenly spread; the first and the last are kept."""
    if n <= k:
        return list(range(n))
    if k <= 1:
        return [n - 1] if k == 1 else []
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def _check_keys(keys: Sequence[str], cap: int, pointer: str, what: str) -> list[str]:
    if len(keys) > cap:
        raise _invalid(f"at most {cap} {what} can be compared", pointer)
    out: list[str] = []
    for k in keys:
        try:
            check_metric_key(k)
        except ValueError as exc:
            raise _invalid(str(exc), pointer) from None
        if k not in out:
            out.append(k)
    return out


async def _runs(session: AsyncSession, scope: Scope, run_ids: Sequence[str]) -> list[m.Run]:
    if len(run_ids) < 2 or len(run_ids) > MAX_COMPARE_RUNS:
        raise _invalid(f"compare 2 to {MAX_COMPARE_RUNS} runs (got {len(run_ids)})", "/query/run")
    if len(set(run_ids)) != len(run_ids):
        raise _invalid("a run is given more than once", "/query/run")
    rows = {
        r.id: r
        for r in (
            await session.execute(
                select(m.Run).where(
                    m.Run.id.in_(list(run_ids)),
                    m.Run.tenant_id == scope.tenant_id,
                    m.Run.project_id == scope.project_id,
                )
            )
        ).scalars()
    }
    for rid in run_ids:
        if rid not in rows:
            raise NotFoundError("run", rid)
    return [rows[rid] for rid in run_ids]


async def _run_views(session: AsyncSession, runs: list[m.Run]) -> list[Run]:
    ids = [r.id for r in runs]
    progress = {
        p.run_id: p
        for p in (
            await session.execute(select(m.RunProgress).where(m.RunProgress.run_id.in_(ids)))
        ).scalars()
    }
    costs = await run_costs(session, runs)
    tags = await tags_by_run(session, ids)
    return [run_dto(r, progress.get(r.id), cost=costs.get(r.id), tags=tags.get(r.id)) for r in runs]


async def _default_keys(session: AsyncSession, ids: list[str]) -> list[str]:
    s = m.RunMetricSummary
    n = func.count(func.distinct(s.run_id)).label("n")
    rows = await session.execute(
        select(s.key, n)
        .where(s.run_id.in_(ids))
        .group_by(s.key)
        .order_by(n.desc(), s.key)
        .limit(MAX_COMPARE_KEYS)
    )
    return [r.key for r in rows.all()]


async def compare_runs(
    session: AsyncSession,
    ctx: ServerContext,
    scope: Scope,
    run_ids: Sequence[str],
    *,
    keys: Sequence[str] = (),
    media_keys: Sequence[str] = (),
    max_points: int = 300,
    params: bool = True,
) -> RunCompare:
    keys = _check_keys(keys, MAX_COMPARE_KEYS, "/query/key", "metric keys")
    media_keys = _check_keys(media_keys, MAX_COMPARE_MEDIA_KEYS, "/query/media_key", "media keys")
    runs = await _runs(session, scope, run_ids)
    ids = [r.id for r in runs]
    order = {rid: i for i, rid in enumerate(ids)}
    truncated: dict[Part, bool] = {}

    s = m.RunMetricSummary
    rows = list(
        (
            await session.execute(
                select(s)
                .where(s.run_id.in_(ids))
                .order_by(s.key, s.labels_key, s.run_id)
                .limit(MAX_COMPARE_SUMMARIES + 1)
            )
        ).scalars()
    )
    if len(rows) > MAX_COMPARE_SUMMARIES:
        truncated["summaries"] = True
        rows = rows[:MAX_COMPARE_SUMMARIES]
    rows.sort(key=lambda r: (r.key, r.labels_key, order[r.run_id]))
    summaries = [CompareSummary(**series_dto(r).model_dump(), run_id=r.run_id) for r in rows]

    metrics: list[CompareMetric] = []
    for key in keys or await _default_keys(session, ids):
        series: list[CompareSeries] = []
        for run in runs:
            hist = await metric_history(
                session, run, key, max_points=max_points, max_series=MAX_COMPARE_SERIES
            )
            if hist.truncated:
                truncated["series"] = True
            series.extend(
                CompareSeries(
                    run_id=run.id,
                    labels=h.labels,
                    attempt=h.attempt,
                    count=h.count,
                    downsampled=h.downsampled,
                    steps=h.steps,
                    values=h.values,
                )
                for h in hist.series
            )
        metrics.append(CompareMetric(key=key, series=series))

    compared_params: list[CompareParam] = []
    if params:
        compared_params = await _params(session, ids, truncated)

    media: list[CompareMedia] = []
    if media_keys:
        media = await _media(session, ctx, ids, media_keys, truncated)

    return RunCompare(
        runs=await _run_views(session, runs),
        summaries=summaries,
        metrics=metrics,
        params=compared_params,
        media=media,
        truncated=truncated,
    )


async def _params(
    session: AsyncSession, ids: list[str], truncated: dict[Part, bool]
) -> list[CompareParam]:
    p = m.RunParam
    keys = list(
        (
            await session.execute(
                select(p.key)
                .where(p.run_id.in_(ids))
                .group_by(p.key)
                .order_by(p.key)
                .limit(MAX_COMPARE_PARAMS + 1)
            )
        ).scalars()
    )
    if len(keys) > MAX_COMPARE_PARAMS:
        truncated["params"] = True
        keys = keys[:MAX_COMPARE_PARAMS]
    if not keys:
        return []
    values: dict[str, dict[str, str | None]] = {k: dict.fromkeys(ids) for k in keys}
    rows = await session.execute(
        select(p.run_id, p.key, p.value_text).where(p.run_id.in_(ids), p.key.in_(keys))
    )
    for run_id, key, text in rows.all():
        values[key][run_id] = text
    return [
        CompareParam(key=k, values=v, differs=len(set(v.values())) > 1) for k, v in values.items()
    ]


async def _media(
    session: AsyncSession,
    ctx: ServerContext,
    ids: list[str],
    media_keys: list[str],
    truncated: dict[Part, bool],
) -> list[CompareMedia]:
    r = m.RunMedia
    rows = list(
        (
            await session.execute(
                select(r)
                .where(r.run_id.in_(ids), r.key.in_(media_keys))
                .order_by(r.run_id, r.key, r.labels_key, r.step, r.attempt, r.id)
            )
        ).scalars()
    )
    groups: dict[tuple[str, str, str], list[m.RunMedia]] = {}
    for row in rows:
        groups.setdefault((row.run_id, row.key, row.labels_key), []).append(row)
    order = {rid: i for i, rid in enumerate(ids)}
    key_order = {k: i for i, k in enumerate(media_keys)}
    per_key: dict[tuple[str, str], int] = {}
    out: list[CompareMedia] = []
    for (run_id, key, _lk), items in sorted(
        groups.items(), key=lambda kv: (order[kv[0][0]], key_order[kv[0][1]], kv[0][2])
    ):
        n = per_key.get((run_id, key), 0)
        if n >= MAX_MEDIA_SERIES_PER_KEY:
            truncated["media"] = True
            continue
        per_key[(run_id, key)] = n + 1
        picked = [items[i] for i in thin(len(items), MAX_COMPARE_MEDIA_ITEMS)]
        if len(picked) < len(items):
            truncated["media"] = True
        out.append(
            CompareMedia(
                run_id=run_id,
                key=key,
                labels=dict(items[0].labels or {}),
                kind=items[0].kind,
                items=await media_views(ctx, picked, links=True),
            )
        )
    return out
