"""Run tracking through the event ingestor: ``metrics``/``params``/``tags`` events, user log
lines, the legacy ``metric`` mirror, replays, budgets and attempts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services import events as events_mod
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.events import EventIngestor, IngestResult
from mapi_server.services.runs import apply_transition, load_run
from mapi_server_testkit.builders import queued_fit, signed_event
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_spec.run import (
    Actor,
    LogPayload,
    MetricBatchPayload,
    MetricPayload,
    MetricSeriesBatch,
    ParamItem,
    ParamsPayload,
    RunnerPhase,
    RunTrigger,
    StatePayload,
    TagItem,
    TagsPayload,
)
from mapi_spec.tracking import TrackingLimits
from sqlalchemy import func, select

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
async def provisioned(ctx: ServerContext) -> m.RunAttempt:
    run_id = await queued_fit(ctx)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (run_id, 1))
        assert att is not None
        return att


def batch(
    key: str,
    steps: list[int | None],
    values: list[float],
    *,
    labels: dict[str, str] | None = None,
    t0: datetime = T0,
) -> MetricBatchPayload:
    return MetricBatchPayload(
        t0=t0,
        series=[
            MetricSeriesBatch(
                key=key,
                labels=labels or {},
                steps=steps,
                values=values,
                ts_ms=[i * 10 for i in range(len(steps))],
            )
        ],
    )


async def _summaries(ctx: ServerContext, run_id: str) -> dict[tuple[str, str], m.RunMetricSummary]:
    async with ctx.db.session() as s:
        rows = (
            await s.execute(select(m.RunMetricSummary).where(m.RunMetricSummary.run_id == run_id))
        ).scalars()
        return {(r.key, r.labels_key): r for r in rows}


async def _points(ctx: ServerContext, run_id: str) -> int:
    async with ctx.db.session() as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(m.RunMetric)
                    .where(m.RunMetric.run_id == run_id)
                )
            ).scalar_one()
        )


async def test_metrics_events_store_points_and_summaries_and_replays_change_nothing(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    ev = signed_event(
        provisioned, 0, batch("step_size", [1, 2, 3], [0.5, 0.25, 0.3], labels={"chain": "0"})
    )
    assert await ing.ingest(ev) is IngestResult.ACCEPTED
    assert await ing.ingest(ev) is IngestResult.DUPLICATE
    # the same points in a new event (a re-sent flush) are duplicates too
    again = signed_event(
        provisioned, 1, batch("step_size", [2, 3], [9.0, 9.0], labels={"chain": "0"})
    )
    assert await ing.ingest(again) is IngestResult.DUPLICATE
    assert await _points(ctx, provisioned.run_id) == 3
    s = (await _summaries(ctx, provisioned.run_id))[("step_size", "chain=0")]
    assert (s.count, s.min, s.max, s.last, s.last_step) == (3, 0.25, 0.5, 0.3, 3)
    assert s.labels == {"chain": "0"} and s.attempt == 1
    assert s.first_ts == T0 and s.last_ts == T0 + timedelta(milliseconds=20)
    async with ctx.db.session() as sess:
        n_events = (await sess.execute(select(func.count()).select_from(m.RunEvent))).scalar_one()
        run = await load_run(sess, None, provisioned.run_id)
    assert n_events == 0  # tracking events are never appended to run_events
    assert run.state == "running"  # a tracking event is a verified first event too


async def test_scalar_points_keep_the_newest_value(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    newer = batch("rmse", [None], [2.0], t0=T0 + timedelta(seconds=5))
    older = batch("rmse", [None], [1.0], t0=T0)
    assert await ing.ingest(signed_event(provisioned, 0, newer)) is IngestResult.ACCEPTED
    assert await ing.ingest(signed_event(provisioned, 1, older)) is IngestResult.DUPLICATE
    s = (await _summaries(ctx, provisioned.run_id))[("rmse", "")]
    assert (s.count, s.last, s.last_step) == (1, 2.0, None)
    newest = batch("rmse", [None], [3.0], t0=T0 + timedelta(seconds=9))
    assert await ing.ingest(signed_event(provisioned, 2, newest)) is IngestResult.ACCEPTED
    s = (await _summaries(ctx, provisioned.run_id))[("rmse", "")]
    assert (s.count, s.last, s.min, s.max) == (1, 3.0, 3.0, 3.0)


async def test_points_over_the_budget_are_dropped(
    ctx: ServerContext, provisioned: m.RunAttempt, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = TrackingLimits(metric_points_per_series=3, metric_series=2)
    monkeypatch.setattr(events_mod, "TrackingLimits", lambda: small)
    ing = EventIngestor(ctx)
    await ing.ingest(signed_event(provisioned, 0, batch("loss", [1, 2], [1.0, 0.9])))
    later = T0 + timedelta(seconds=1)
    await ing.ingest(
        signed_event(provisioned, 1, batch("loss", [3, 4, 5], [0.8, 0.7, 0.6], t0=later))
    )
    await ing.ingest(signed_event(provisioned, 2, batch("acc", [1], [0.5])))
    third = signed_event(provisioned, 3, batch("extra", [1], [0.5]))
    assert await ing.ingest(third) is IngestResult.DUPLICATE  # a third series: dropped
    summaries = await _summaries(ctx, provisioned.run_id)
    assert set(summaries) == {("loss", ""), ("acc", "")}
    assert summaries[("loss", "")].count == 3 and summaries[("loss", "")].last_step == 3


async def test_legacy_metric_events_are_mirrored_as_scalar_points(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    await ing.ingest(signed_event(provisioned, 0, MetricPayload(name="rmse", value=0.5)))
    await ing.ingest(
        signed_event(provisioned, 1, MetricPayload(name="mape", value=0.1, labels={"Bad Key": "x"}))
    )
    await ing.ingest(
        signed_event(
            provisioned, 2, MetricPayload(name="loss", value=0.7, step=4, labels={"split": "val"})
        )
    )
    summaries = await _summaries(ctx, provisioned.run_id)
    assert set(summaries) == {("rmse", ""), ("loss", "split=val")}  # bad labels: event only
    assert summaries[("rmse", "")].last_step is None
    assert summaries[("loss", "split=val")].last_step == 4
    async with ctx.db.session() as s:
        types = list((await s.execute(select(m.RunEvent.type))).scalars())
    assert types == ["metric", "metric", "metric"]  # the events themselves are unchanged


async def test_a_new_attempt_resets_the_summary(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    await ing.ingest(signed_event(provisioned, 0, StatePayload(phase=RunnerPhase.FIT)))
    await ing.ingest(signed_event(provisioned, 1, batch("loss", [1, 2, 3], [3.0, 2.0, 1.0])))
    async with ctx.db.session() as s:
        run = await load_run(s, None, provisioned.run_id, for_update=True)
        await apply_transition(s, run, RunTrigger.INFRA_RETRY, Actor.RECONCILER)
    await Dispatcher(ctx, FakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        att2 = await s.get(m.RunAttempt, (provisioned.run_id, 2))
        assert att2 is not None
    late = signed_event(provisioned, 9, batch("loss", [4], [0.5]))
    assert await ing.ingest(late) is IngestResult.STALE_ATTEMPT
    assert await ing.ingest(signed_event(att2, 0, batch("loss", [1], [5.0]))) is (
        IngestResult.ACCEPTED
    )
    s2 = (await _summaries(ctx, provisioned.run_id))[("loss", "")]
    assert (s2.attempt, s2.count, s2.last, s2.min) == (2, 1, 5.0, 5.0)
    assert await _points(ctx, provisioned.run_id) == 4  # attempt 1's points are kept


async def test_params_are_write_once_and_tags_last_wins(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    params = ParamsPayload(
        params=[
            ParamItem(key="model", value="ts.trend@1"),
            ParamItem(key="fit.draws", value=1000),
            ParamItem(key="fit.draws", value=5),  # repeated in one event: the first wins
            ParamItem(key="settings.flag", value=True),
        ]
    )
    ev = signed_event(provisioned, 0, params)
    assert await ing.ingest(ev) is IngestResult.ACCEPTED
    assert await ing.ingest(ev) is IngestResult.DUPLICATE
    rewrite = ParamsPayload(params=[ParamItem(key="fit.draws", value=2000)])
    assert await ing.ingest(signed_event(provisioned, 1, rewrite)) is IngestResult.DUPLICATE
    tags = TagsPayload(tags=[TagItem(key="stage", value="a"), TagItem(key="stage", value="b")])
    tev = signed_event(provisioned, 2, tags)
    assert await ing.ingest(tev) is IngestResult.ACCEPTED
    assert await ing.ingest(tev) is IngestResult.DUPLICATE
    async with ctx.db.session() as s:
        stored = {
            r.key: (r.value_text, r.value_num, r.source)
            for r in (await s.execute(select(m.RunParam))).scalars()
        }
        tag_rows = {r.key: r.value for r in (await s.execute(select(m.RunTag))).scalars()}
    assert stored == {
        "model": ("ts.trend@1", None, "runner"),
        "fit.draws": ("1000", 1000.0, "runner"),
        "settings.flag": ("true", None, "runner"),
    }
    assert tag_rows == {"stage": "b"}


async def test_an_older_tags_event_never_overwrites_a_newer_value(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    """Runner tags are ordered by event time: a reordered older event and a replay that
    lands after a PATCH-style API write leave the newer value."""
    from mapi_server.services.tracking import write_tags
    from mapi_spec.run import make_event

    ing = EventIngestor(ctx)

    def tags_event(seq: int, value: str, at: datetime) -> object:
        payload = TagsPayload(tags=[TagItem(key="stage", value=value)])
        return make_event(provisioned.run_id, 1, seq, payload, ts=at, key=provisioned.hmac_key)

    async def stage() -> str:
        async with ctx.db.session() as s:
            row = await s.get(m.RunTag, (provisioned.run_id, "stage"))
            assert row is not None
            return row.value

    now = datetime.now(UTC)
    late = tags_event(5, "candidate", now - timedelta(seconds=2))
    assert await ing.ingest(tags_event(6, "shadow", now - timedelta(seconds=1))) is (
        IngestResult.ACCEPTED
    )
    assert await ing.ingest(late) is IngestResult.DUPLICATE  # reordered: older than seq 6
    assert await stage() == "shadow"
    async with ctx.db.session() as s:
        run = await load_run(s, None, provisioned.run_id, for_update=True)
        await write_tags(s, run, {"stage": "prod"}, actor="usr_test")  # a PATCH during the run
    assert await ing.ingest(tags_event(6, "shadow", now - timedelta(seconds=1))) is (
        IngestResult.DUPLICATE
    )
    assert await stage() == "prod"
    newer = tags_event(7, "retired", datetime.now(UTC) + timedelta(seconds=1))
    assert await ing.ingest(newer) is IngestResult.ACCEPTED
    assert await stage() == "retired"


async def test_user_log_lines_keep_level_and_fields(
    ctx: ServerContext, provisioned: m.RunAttempt
) -> None:
    ing = EventIngestor(ctx)
    line = LogPayload(stream="user", line="fold 2 done", level="warning", fields={"fold": 2})
    assert await ing.ingest(signed_event(provisioned, 0, line)) is IngestResult.ACCEPTED
    await ing.ingest(signed_event(provisioned, 1, LogPayload(stream="stdout", line="plain")))
    async with ctx.db.session() as s:
        rows = list((await s.execute(select(m.RunLog).order_by(m.RunLog.seq))).scalars())
    assert [(r.stream, r.level, r.fields) for r in rows] == [
        ("user", "warning", {"fold": 2}),
        ("stdout", None, None),
    ]
