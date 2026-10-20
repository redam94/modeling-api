"""Run tracking storage (v0.11): metric points and summaries, params, tags, history reads.

Shared by the event ingestor (``metrics``/``params``/``tags`` run events, ``source="runner"``)
and the HTTP API (``POST /v1/runs/{id}/metrics|params``, ``PATCH /v1/runs/{id}``,
``source="api"``). Callers hold the run row lock (``SELECT ... FOR UPDATE``), so writes to
one run never race and the summary recompute sees every committed point.

* **Points** (``run_metrics``): primary key ``(run, attempt, key, labels_key, step)``; a point
  without a step is stored at :data:`~mapi_spec.tracking.SCALAR_STEP`. A stepped point is
  write-once (the first value wins: a replayed event is a duplicate); a scalar is replaced
  by a write with a newer timestamp (the newest value wins).
* **Summaries** (``run_metric_summary``): recomputed from the points of the touched series in
  one statement per batch, and kept for the newest attempt that wrote the series. A series
  whose points retention pruned merges the new points into its stored summary.
* **Budgets** (:class:`~mapi_spec.tracking.TrackingLimits`): the runner path drops what is
  over the budget (the supervisor already did, this is the trusted second check); the API
  path refuses the whole write with ``QUOTA_EXCEEDED``.

The control plane imports no numpy: :func:`lttb` downsamples in pure Python.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from mapi_spec.api import MetricHistory, MetricHistorySeries, MetricSeries
from mapi_spec.errors import ErrorCode, MapiError, QuotaExceeded
from mapi_spec.tracking import (
    MAX_SERIES_PER_HISTORY,
    MAX_TAGS_PER_RUN,
    SCALAR_STEP,
    ParamValue,
    TrackingLimits,
    labels_key,
    param_text,
    parse_labels_key,
)
from sqlalchemy import Boolean, delete, func, literal_column, select, tuple_
from sqlalchemy.dialects.postgresql import aggregate_order_by, insert
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.services.runs import now

__all__ = [
    "HISTORY_FETCH_POINTS",
    "Point",
    "Source",
    "lttb",
    "metric_history",
    "run_tags",
    "series_dto",
    "tags_by_run",
    "write_metric_points",
    "write_params",
    "write_tags",
]

Source = Literal["runner", "api"]
Point = tuple[str, dict[str, str], int | None, float, datetime]
"""``(key, labels, step, value, ts)``; labels already validated."""

HISTORY_FETCH_POINTS = 20_000
"""Points of one series a history read fetches before downsampling."""
_CHUNK = 1000  # rows per INSERT statement (well under the bind-parameter limit)


def _quota(detail: str) -> QuotaExceeded:
    return QuotaExceeded(detail)


def _nextval() -> Any:
    return func.nextval(m.TRACKING_SEQ)


# ============================================================================== metric points


async def write_metric_points(
    session: AsyncSession,
    run: m.Run,
    attempt: int,
    points: Iterable[Point],
    *,
    source: Source,
    limits: TrackingLimits,
) -> tuple[int, int]:
    """Store points of ``run``/``attempt`` and refresh the touched summaries.

    Returns ``(accepted, duplicates)``: accepted points were inserted (or replaced an older
    scalar); duplicates were already stored (a replayed event, an older scalar). Points over
    the budget are dropped (``runner``) or refuse the whole write (``api``: ``QUOTA_EXCEEDED``)
    and count as neither.
    """
    # one row per primary key inside the batch: Postgres refuses an upsert that touches a
    # row twice. Stepped points keep the first; scalars keep the newest timestamp.
    series: dict[tuple[str, str], dict[int, tuple[float, datetime]]] = {}
    labels_of: dict[tuple[str, str], dict[str, str]] = {}
    submitted = 0
    for key, labels, step, value, ts in points:
        if not math.isfinite(value):
            continue
        submitted += 1
        sk = (key, labels_key(labels))
        labels_of.setdefault(sk, dict(labels))
        rows = series.setdefault(sk, {})
        st = SCALAR_STEP if step is None else step
        prev = rows.get(st)
        if prev is None or (st == SCALAR_STEP and ts >= prev[1]):
            rows[st] = (value, ts)
    if not series:
        return 0, 0
    repeated = submitted - sum(len(v) for v in series.values())  # same key twice in the batch
    series = await _within_budget(session, run, attempt, series, source=source, limits=limits)
    kept = sum(len(v) for v in series.values())
    accepted = 0
    inserted: dict[tuple[str, str], int] = {}
    if kept:
        values = [
            {
                "run_id": run.id,
                "attempt": attempt,
                "key": key,
                "labels_key": lk,
                "step": st,
                "ts": ts,
                "value": value,
                "source": source,
                "tenant_id": run.tenant_id,
                "project_id": run.project_id,
            }
            for (key, lk), rows in series.items()
            for st, (value, ts) in rows.items()
        ]
        for i in range(0, len(values), _CHUNK):
            ins = insert(m.RunMetric).values(values[i : i + _CHUNK])
            stmt = ins.on_conflict_do_update(
                index_elements=["run_id", "attempt", "key", "labels_key", "step"],
                set_={
                    "value": ins.excluded.value,
                    "ts": ins.excluded.ts,
                    "source": ins.excluded.source,
                },
                where=(m.RunMetric.step == SCALAR_STEP) & (ins.excluded.ts > m.RunMetric.ts),
            ).returning(
                m.RunMetric.key,
                m.RunMetric.labels_key,
                literal_column("xmax = 0", Boolean).label("inserted"),  # else a replaced scalar
            )
            changed = (await session.execute(stmt)).all()
            accepted += len(changed)
            for row in changed:
                if row.inserted:
                    inserted[(row.key, row.labels_key)] = (
                        inserted.get((row.key, row.labels_key), 0) + 1
                    )
        if accepted:
            await _refresh_summaries(session, run, attempt, labels_of, list(series), inserted)
    return accepted, repeated + kept - accepted


async def _within_budget(
    session: AsyncSession,
    run: m.Run,
    attempt: int,
    series: dict[tuple[str, str], dict[int, tuple[float, datetime]]],
    *,
    source: Source,
    limits: TrackingLimits,
) -> dict[tuple[str, str], dict[int, tuple[float, datetime]]]:
    """Trim ``series`` to the run's budgets (this attempt's stored counts); ``api`` raises."""
    rows = (
        await session.execute(
            select(
                m.RunMetricSummary.key,
                m.RunMetricSummary.labels_key,
                m.RunMetricSummary.count.label("n"),
            ).where(m.RunMetricSummary.run_id == run.id, m.RunMetricSummary.attempt == attempt)
        )
    ).all()
    counts = {(r.key, r.labels_key): int(r.n) for r in rows}
    n_series = len(counts)
    total = sum(counts.values())
    out: dict[tuple[str, str], dict[int, tuple[float, datetime]]] = {}
    for sk, points in series.items():
        have = counts.get(sk)
        if have is None:
            if n_series >= limits.metric_series:
                if source == "api":
                    raise _quota(
                        f"the run already has {n_series} metric series (at most "
                        f"{limits.metric_series})"
                    )
                continue
            n_series += 1
            have = 0
        room = min(limits.metric_points_per_series - have, limits.metric_points_total - total)
        if room < len(points):
            if source == "api":
                raise _quota(
                    f"metric {sk[0]!r} would exceed the run's point budget "
                    f"({limits.metric_points_per_series} per series, "
                    f"{limits.metric_points_total} in total)"
                )
            if room <= 0:
                continue
            points = dict(list(points.items())[:room])
        out[sk] = points
        total += len(points)
    return out


async def _refresh_summaries(
    session: AsyncSession,
    run: m.Run,
    attempt: int,
    labels_of: Mapping[tuple[str, str], dict[str, str]],
    touched: Sequence[tuple[str, str]],
    inserted: Mapping[tuple[str, str], int],
) -> None:
    """Recompute the summaries of ``touched`` series from this attempt's points (two reads,
    one upsert). A summary of a newer attempt is never replaced by an older one.

    When retention deleted points of a series (its stored points are fewer than the
    summary's count plus the ``inserted`` new rows), the new points are merged into the
    stored summary instead: the count, minimum, maximum and first time stay true.
    """
    p = m.RunMetric
    order = (p.ts.desc(), p.step.desc())
    agg = (
        select(
            p.key,
            p.labels_key,
            func.count().label("n"),
            func.min(p.ts).label("first_ts"),
            func.max(p.ts).label("last_ts"),
            func.min(p.value).label("min"),
            func.max(p.value).label("max"),
            func.array_agg(aggregate_order_by(p.value, *order))[1].label("last"),
            func.array_agg(aggregate_order_by(p.step, *order))[1].label("last_step"),
        )
        .where(
            p.run_id == run.id,
            p.attempt == attempt,
            tuple_(p.key, p.labels_key).in_(list(touched)),
        )
        .group_by(p.key, p.labels_key)
    )
    rows = (await session.execute(agg)).all()
    if not rows:
        return
    s = m.RunMetricSummary
    stored = {
        (x.key, x.labels_key): x
        for x in (
            await session.execute(
                select(s).where(
                    s.run_id == run.id,
                    s.attempt == attempt,
                    tuple_(s.key, s.labels_key).in_(list(touched)),
                )
            )
        ).scalars()
    }
    values = []
    for r in rows:
        sk = (r.key, r.labels_key)
        v: dict[str, Any] = {
            "run_id": run.id,
            "key": r.key,
            "labels_key": r.labels_key,
            "labels": labels_of.get(sk) or parse_labels_key(r.labels_key),
            "attempt": attempt,
            "count": int(r.n),
            "first_ts": r.first_ts,
            "last_ts": r.last_ts,
            "last": float(r.last),
            "last_step": None if r.last_step == SCALAR_STEP else int(r.last_step),
            "min": float(r.min),
            "max": float(r.max),
            "tenant_id": run.tenant_id,
            "project_id": run.project_id,
        }
        old = stored.get(sk)
        if old is not None and int(r.n) < old.count + inserted.get(sk, 0):  # points pruned
            v["count"] = old.count + inserted.get(sk, 0)
            v["first_ts"] = min(old.first_ts, r.first_ts)
            v["min"] = min(old.min, v["min"])
            v["max"] = max(old.max, v["max"])
            new_last = (r.last_ts, SCALAR_STEP if v["last_step"] is None else v["last_step"])
            old_last = (old.last_ts, SCALAR_STEP if old.last_step is None else old.last_step)
            if old_last > new_last:
                v["last_ts"], v["last"], v["last_step"] = old.last_ts, old.last, old.last_step
        values.append(v)
    ins = insert(m.RunMetricSummary).values(values)
    fields = (
        "labels",
        "attempt",
        "count",
        "first_ts",
        "last_ts",
        "last",
        "last_step",
        "min",
        "max",
    )
    await session.execute(
        ins.on_conflict_do_update(
            index_elements=["run_id", "key", "labels_key"],
            set_={**{f: ins.excluded[f] for f in fields}, "seq": _nextval()},
            where=m.RunMetricSummary.attempt <= ins.excluded.attempt,
        )
    )


def series_dto(s: m.RunMetricSummary) -> MetricSeries:
    return MetricSeries(
        key=s.key,
        labels=dict(s.labels or {}),
        attempt=s.attempt,
        count=s.count,
        last=s.last,
        last_step=s.last_step,
        min=s.min,
        max=s.max,
        first_ts=s.first_ts,
        last_ts=s.last_ts,
    )


# ===================================================================================== params


async def write_params(
    session: AsyncSession,
    run: m.Run,
    items: Mapping[str, ParamValue],
    *,
    source: Source,
    limits: TrackingLimits | None = None,
) -> tuple[int, int, list[str]]:
    """Store write-once params; returns ``(accepted, unchanged, conflicting keys)``.

    ``runner``: a key already stored keeps its first value (the new one is dropped, and keys
    over the ``params`` budget too). ``api``: when any key is stored with a different value,
    nothing is written and the conflicting keys come back (the caller answers 409); a write
    over the budget raises ``QUOTA_EXCEEDED``.
    """
    if not items:
        return 0, 0, []
    limits = limits or TrackingLimits()
    texts = {k: param_text(v) for k, v in items.items()}
    stored = {
        r.key: r.value_text
        for r in (
            await session.execute(
                select(m.RunParam.key, m.RunParam.value_text).where(
                    m.RunParam.run_id == run.id, m.RunParam.key.in_(list(texts))
                )
            )
        ).all()
    }
    conflicts = sorted(k for k, (t, _) in texts.items() if k in stored and stored[k] != t)
    unchanged = sum(1 for k, (t, _) in texts.items() if stored.get(k) == t)
    if conflicts and source == "api":
        return 0, unchanged, conflicts
    new = [k for k in texts if k not in stored]
    if new:
        have = (
            await session.execute(
                select(func.count()).select_from(m.RunParam).where(m.RunParam.run_id == run.id)
            )
        ).scalar_one()
        room = max(0, limits.params - int(have))
        if len(new) > room:
            if source == "api":
                raise _quota(f"the run would have more than {limits.params} params")
            new = new[:room]
    if not new:
        return 0, unchanged, conflicts
    ts = now()
    ins = insert(m.RunParam).values(
        [
            {
                "run_id": run.id,
                "key": k,
                "value_text": texts[k][0],
                "value_num": texts[k][1],
                "source": source,
                "logged_at": ts,
                "tenant_id": run.tenant_id,
                "project_id": run.project_id,
            }
            for k in new
        ]
    )
    written = (await session.execute(ins.on_conflict_do_nothing().returning(m.RunParam.key))).all()
    return len(written), unchanged, conflicts


# ======================================================================================= tags


async def write_tags(
    session: AsyncSession,
    run: m.Run,
    tags: Mapping[str, str | None],
    *,
    actor: str | None,
    max_tags: int = MAX_TAGS_PER_RUN,
    ts: datetime | None = None,
) -> tuple[int, int]:
    """Set (the last value wins) and delete (``None``) tags; returns ``(changed, deleted)``
    (a tag set to the value it already has is not counted).

    ``actor`` is the principal of an API write (``None`` for the runner). An API write that
    would leave more than ``max_tags`` tags raises ``SCHEMA_INVALID`` (422) before anything
    changes; the runner path drops new keys beyond it.

    ``ts`` is the time of a runner event: the tags are stamped with it, and a key changes
    only when ``ts`` is newer than its stored update. A replayed or reordered older event
    then never overwrites a newer value (from the runner or set through the API).
    """
    if not tags:
        return 0, 0
    existing = set(
        (await session.execute(select(m.RunTag.key).where(m.RunTag.run_id == run.id))).scalars()
    )
    drop = [k for k, v in tags.items() if v is None and k in existing]
    put = {k: v for k, v in tags.items() if v is not None}
    after = (existing - set(drop)) | set(put)
    if len(after) > max_tags:
        if actor is not None:
            raise MapiError(
                f"a run has at most {max_tags} tags (this change would leave {len(after)})",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/body/tags",
            )
        room = max_tags - len(existing - set(drop))
        fresh = [k for k in put if k not in existing][: max(0, room)]
        put = {k: v for k, v in put.items() if k in existing or k in fresh}
    deleted = 0
    if drop:
        res = await session.execute(
            delete(m.RunTag).where(m.RunTag.run_id == run.id, m.RunTag.key.in_(drop))
        )
        deleted = int(getattr(res, "rowcount", 0) or 0)
    if put:
        stamp = ts or now()
        ins = insert(m.RunTag).values(
            [
                {
                    "run_id": run.id,
                    "key": k,
                    "value": v,
                    "updated_at": stamp,
                    "updated_by": actor[:64] if actor else None,
                    "tenant_id": run.tenant_id,
                    "project_id": run.project_id,
                }
                for k, v in put.items()
            ]
        )
        changes = m.RunTag.value.is_distinct_from(ins.excluded.value)
        if ts is not None:
            changes = changes & (m.RunTag.updated_at < ins.excluded.updated_at)
        changed = (
            await session.execute(
                ins.on_conflict_do_update(
                    index_elements=["run_id", "key"],
                    set_={
                        "value": ins.excluded.value,
                        "updated_at": ins.excluded.updated_at,
                        "updated_by": ins.excluded.updated_by,
                        "seq": _nextval(),
                    },
                    # an unchanged value or an older runner event keeps the row (and its
                    # ``seq``): a replay changes nothing
                    where=changes,
                ).returning(m.RunTag.key)
            )
        ).all()
        return len(changed), deleted
    return 0, deleted


async def tags_by_run(session: AsyncSession, run_ids: Sequence[str]) -> dict[str, dict[str, str]]:
    """Tags of several runs (one query; runs without tags are absent)."""
    out: dict[str, dict[str, str]] = {}
    if not run_ids:
        return out
    rows = await session.execute(
        select(m.RunTag.run_id, m.RunTag.key, m.RunTag.value)
        .where(m.RunTag.run_id.in_(list(run_ids)))
        .order_by(m.RunTag.run_id, m.RunTag.key)
    )
    for run_id, key, value in rows.all():
        out.setdefault(run_id, {})[key] = value
    return out


async def run_tags(session: AsyncSession, run_id: str) -> dict[str, str]:
    return (await tags_by_run(session, [run_id])).get(run_id, {})


# ==================================================================================== history


def lttb(xs: Sequence[float], ys: Sequence[float], n: int) -> list[int]:
    """Largest-Triangle-Three-Buckets: indices of at most ``n`` points that keep the shape.

    The first and the last point are always kept; ``n >= len(xs)`` keeps every index.
    """
    size = len(xs)
    if len(ys) != size:
        raise ValueError("xs and ys must have the same length")
    if n >= size or size <= 2:
        return list(range(size))
    if n <= 2:
        return [0, size - 1][: max(n, 0)] if n > 0 else []
    out = [0]
    every = (size - 2) / (n - 2)
    a = 0
    for i in range(n - 2):
        # the next bucket's average is the third corner of the triangle
        nxt_start = math.floor((i + 1) * every) + 1
        nxt_end = min(math.floor((i + 2) * every) + 1, size)
        span = nxt_end - nxt_start
        avg_x = sum(xs[nxt_start:nxt_end]) / span if span > 0 else xs[size - 1]
        avg_y = sum(ys[nxt_start:nxt_end]) / span if span > 0 else ys[size - 1]
        start = math.floor(i * every) + 1
        end = math.floor((i + 1) * every) + 1
        ax, ay = xs[a], ys[a]
        best, best_area = start, -1.0
        for j in range(start, end):
            area = abs((ax - avg_x) * (ys[j] - ay) - (ax - xs[j]) * (avg_y - ay))
            if area > best_area:
                best, best_area = j, area
        out.append(best)
        a = best
    out.append(size - 1)
    return out


async def metric_history(
    session: AsyncSession,
    run: m.Run,
    key: str,
    *,
    max_points: int,
    attempt: int | None = None,
    labels: Mapping[str, str] | None = None,
) -> MetricHistory:
    """The points of every series of ``key`` (at most 64, by labels), each downsampled with
    :func:`lttb` on ``(step, value)`` to ``max_points``. ``attempt`` defaults to each series'
    summary attempt; ``labels`` keeps series whose labels contain every given pair."""
    s = m.RunMetricSummary
    stmt = select(s).where(s.run_id == run.id, s.key == key)
    if labels:
        stmt = stmt.where(s.labels.contains(dict(labels)))
    summaries = list(
        (
            await session.execute(stmt.order_by(s.labels_key).limit(MAX_SERIES_PER_HISTORY + 1))
        ).scalars()
    )
    truncated = len(summaries) > MAX_SERIES_PER_HISTORY
    out: list[MetricHistorySeries] = []
    p = m.RunMetric
    for summary in summaries[:MAX_SERIES_PER_HISTORY]:
        att = attempt if attempt is not None else summary.attempt
        rows = (
            await session.execute(
                select(p.step, p.value, p.ts)
                .where(
                    p.run_id == run.id,
                    p.attempt == att,
                    p.key == key,
                    p.labels_key == summary.labels_key,
                )
                .order_by(p.step, p.ts)
                .limit(HISTORY_FETCH_POINTS)
            )
        ).all()
        if not rows:
            continue
        keep = lttb([float(r.step) for r in rows], [r.value for r in rows], max_points)
        picked = [rows[i] for i in keep]
        out.append(
            MetricHistorySeries(
                labels=dict(summary.labels or {}),
                attempt=att,
                count=summary.count if att == summary.attempt else len(rows),
                downsampled=len(picked) < len(rows),
                steps=[None if r.step == SCALAR_STEP else int(r.step) for r in picked],
                values=[float(r.value) for r in picked],
                ts=[r.ts for r in picked],
            )
        )
    return MetricHistory(run_id=run.id, key=key, series=out, truncated=truncated)
