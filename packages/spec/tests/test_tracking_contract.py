"""v0.11 run tracking contracts: keys and labels, param text, tracking payloads and their
splitting, the run-search grammar, the HTTP DTOs/endpoints/access and the reference docs."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mapi_spec import api
from mapi_spec.api import (
    ENDPOINTS,
    ROUTE_ACCESS,
    V011_ENDPOINTS,
    LogLine,
    MetricPointIn,
    MetricsWrite,
    ParamsWrite,
    RouteScope,
    Run,
    RunPatch,
    SseEventName,
)
from mapi_spec.audit import AuditAction
from mapi_spec.executor import RunEventType
from mapi_spec.run import (
    MAX_EVENT_LINE_BYTES,
    ChainProgress,
    LogPayload,
    MetricBatchPayload,
    MetricSeriesBatch,
    ParamItem,
    ParamsPayload,
    RunEvent,
    RunLimits,
    TagItem,
    TagsPayload,
    encode_event_line,
    make_event,
    parse_payload,
    split_for_events,
)
from mapi_spec.tenancy import PERMISSIONS, Action, ProjectRole, ResourceKind
from mapi_spec.tokens import ScopeName, scope_allows
from mapi_spec.tracking import (
    LOG_LEVELS,
    MAX_METRIC_LABELS,
    MAX_PARAM_VALUE_CHARS,
    MAX_POINTS_PER_EVENT,
    RESERVED_TAG_PREFIX,
    RunRef,
    TrackingLimits,
    check_labels,
    check_metric_key,
    check_param_key,
    check_tag_key,
    labels_key,
    param_text,
    parse_labels_key,
    parse_run_filter,
    parse_run_order,
    parse_run_ref,
)
from mapi_spec.views import BacktestView
from pydantic import ValidationError

T0 = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
KEY = b"k" * 32

# ================================================================================ keys, labels


@pytest.mark.parametrize("key", ["loss", "rmse", "step_size", "a", "x" * 64, "coverage_90"])
def test_valid_metric_keys(key: str) -> None:
    assert check_metric_key(key) == key


@pytest.mark.parametrize(
    "key", ["train/loss", "val.loss", "Loss", "1loss", "", "x" * 65, "loss-1", "loss rate"]
)
def test_invalid_metric_keys(key: str) -> None:
    with pytest.raises(ValueError, match="metric key"):
        check_metric_key(key)


def test_labels_are_validated_and_sorted() -> None:
    assert check_labels(None) == {} and check_labels({}) == {}
    out = check_labels({"split": "val", "chain": "0"})
    assert list(out) == ["chain", "split"]
    too_many = {f"k{i}": "v" for i in range(MAX_METRIC_LABELS + 1)}
    for bad, match in (
        (too_many, "at most"),
        ({"Chain": "0"}, "label key"),
        ({"chain": "a b"}, "label value"),
        ({"chain": ""}, "label value"),
        ({"chain": "x" * 64}, "label value"),
        ({"k" * 33: "v"}, "label key"),
    ):
        with pytest.raises(ValueError, match=match):
            check_labels(bad)


def test_labels_key_round_trip_and_sort() -> None:
    labels = {"split": "val", "chain": "3", "fold": "a-1.b_2"}
    text = labels_key(labels)
    assert text == "chain=3,fold=a-1.b_2,split=val"
    assert parse_labels_key(text) == labels
    assert labels_key({}) == "" and parse_labels_key("") == {}
    for bad in ("chain", "chain=0,chain=1", "Chain=0"):
        with pytest.raises(ValueError):
            parse_labels_key(bad)


def test_param_and_tag_keys() -> None:
    assert check_param_key("settings.adstock.l_max") == "settings.adstock.l_max"
    assert check_param_key("trial.3.adstock/l_max") == "trial.3.adstock/l_max"
    for bad in (".x", "a b", "x" * 251, ""):
        with pytest.raises(ValueError, match="param key"):
            check_param_key(bad)
    assert check_tag_key("team/owner") == "team/owner"
    with pytest.raises(ValueError, match="reserved"):
        check_tag_key(RESERVED_TAG_PREFIX + "source")
    assert check_tag_key("mapi.source", reserved=True) == "mapi.source"
    with pytest.raises(ValueError, match="tag key"):
        check_tag_key("x" * 129)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, ("true", None)),
        (False, ("false", None)),
        (0, ("0", 0.0)),
        (1000, ("1000", 1000.0)),
        (0.25, ("0.25", 0.25)),
        (1e-7, ("1e-07", 1e-7)),
        (math.inf, ("inf", None)),
        ("nuts", ("nuts", None)),
        ("", ("", None)),
    ],
)
def test_param_text(value: Any, expected: tuple[str, float | None]) -> None:
    assert param_text(value) == expected


def test_param_text_nan() -> None:
    text, num = param_text(math.nan)
    assert text == "nan" and num is None


def test_log_levels_ascend() -> None:
    assert LOG_LEVELS == ("debug", "info", "warning", "error")


# ==================================================================================== payloads


def _series(n: int, key: str = "loss", **labels: str) -> MetricSeriesBatch:
    return MetricSeriesBatch(
        key=key,
        labels=labels,
        steps=list(range(1, n + 1)),
        values=[1.0 / (i + 1) for i in range(n)],
        ts_ms=[10 * i for i in range(n)],
    )


def test_metric_series_batch_validators() -> None:
    with pytest.raises(ValidationError, match="same length"):
        MetricSeriesBatch(key="loss", steps=[1, 2], values=[1.0], ts_ms=[0, 1])
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError, match="finite"):
            MetricSeriesBatch(key="loss", steps=[1], values=[bad], ts_ms=[0])
    with pytest.raises(ValidationError):
        MetricSeriesBatch(key="loss", steps=[-1], values=[1.0], ts_ms=[0])
    with pytest.raises(ValidationError):
        MetricSeriesBatch(key="train/loss", steps=[1], values=[1.0], ts_ms=[0])
    with pytest.raises(ValidationError):
        MetricSeriesBatch(key="loss", steps=[], values=[], ts_ms=[])
    scalar = MetricSeriesBatch(key="rmse", steps=[None], values=[2.5], ts_ms=[0])
    assert scalar.steps == [None]


def test_metric_batch_payload_points_and_cap() -> None:
    p = MetricBatchPayload(t0=T0, series=[_series(2), _series(1, "step_size", chain="0")])
    pts = list(p.points())
    assert pts[0] == ("loss", {}, 1, 1.0, T0)
    assert pts[1][4] == T0 + timedelta(milliseconds=10)
    assert pts[2][:3] == ("step_size", {"chain": "0"}, 1)
    over = [_series(600), _series(401, "other")]
    with pytest.raises(ValidationError, match="at most 1000 points"):
        MetricBatchPayload(t0=T0, series=over)
    MetricBatchPayload(t0=T0, series=[_series(600), _series(400, "other")])
    with pytest.raises(ValidationError):
        MetricBatchPayload(t0=T0.replace(tzinfo=None), series=[_series(1)])


def test_log_payload_level_and_fields_only_for_user() -> None:
    user = LogPayload(stream="user", line="hi", level="warning", fields={"point": 3, "ok": None})
    assert user.level == "warning"
    LogPayload(stream="stderr", line="plain")
    for kw in ({"level": "info"}, {"fields": {"a": 1}}):
        with pytest.raises(ValidationError, match="stream 'user'"):
            LogPayload(stream="stdout", line="x", **kw)
    with pytest.raises(ValidationError):
        LogPayload(stream="user", line="x", fields={"Bad": 1})
    with pytest.raises(ValidationError):
        LogPayload(stream="user", line="x", fields={f"k{i}": i for i in range(17)})
    with pytest.raises(ValidationError):
        LogPayload(stream="user", line="x", level="fatal")


def test_tag_item_refuses_reserved_keys_and_params_are_typed() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        TagItem(key="mapi.source", value="x")
    TagItem(key="stage", value="x" * 1000)
    with pytest.raises(ValidationError):
        TagItem(key="stage", value="x" * 1001)
    assert ParamItem(key="fit.draws", value=1000).value == 1000
    assert ParamItem(key="flag", value=True).value is True
    with pytest.raises(ValidationError):
        ParamItem(key="x", value="v" * (MAX_PARAM_VALUE_CHARS + 1))
    with pytest.raises(ValidationError):
        ParamItem(key="x", value=[1, 2])


def test_chain_progress_has_optional_step_fields() -> None:
    c = ChainProgress(chain=0, draws_done=10, draws_total=100, step_size=0.12, num_steps=7)
    assert c.step_size == 0.12 and c.num_steps == 7
    assert ChainProgress(chain=1, draws_done=0, draws_total=1).step_size is None
    with pytest.raises(ValidationError):
        ChainProgress(chain=0, draws_done=0, draws_total=1, step_size=-1)


def test_run_limits_carry_tracking_budgets() -> None:
    limits = RunLimits(max_output_bytes=1)
    assert limits.tracking == TrackingLimits()
    assert limits.tracking.metric_points_per_series == 10_000
    with pytest.raises(ValidationError):
        TrackingLimits(tags=101)
    with pytest.raises(ValidationError):
        TrackingLimits(flush_interval_s=0.01)


@pytest.mark.parametrize(
    ("payload", "etype"),
    [
        (MetricBatchPayload(t0=T0, series=[_series(3)]), RunEventType.METRICS),
        (
            ParamsPayload(params=[ParamItem(key="model", value="mmm.bayesian@1")]),
            RunEventType.PARAMS,
        ),
        (TagsPayload(tags=[TagItem(key="stage", value="x")]), RunEventType.TAGS),
        (LogPayload(stream="user", line="x", level="info"), RunEventType.LOG),
    ],
)
def test_make_event_infers_the_tracking_types(payload: Any, etype: RunEventType) -> None:
    ev = make_event("run_01", 1, 4, payload, ts=T0, key=KEY)
    assert ev.type is etype
    assert parse_payload(ev) == payload
    again = RunEvent.model_validate_json(ev.model_dump_json())
    assert parse_payload(again) == payload


def _line_bytes(payload: Any) -> int:
    ev = make_event("r" * 128, 1, 10**12, payload, ts=T0, key=KEY)
    return len(encode_event_line(ev).encode("utf-8"))


def test_split_for_events_keeps_every_metric_part_under_the_line_cap() -> None:
    labels = {f"label_{i}": "v" * 63 for i in range(8)}
    keys = [f"{'m' * 58}{i:02d}" for i in range(50)]
    series = [
        MetricSeriesBatch(
            key=k,
            labels=labels,
            steps=list(range(j * 20, j * 20 + 20)),
            values=[math.pi * (i + 1) * 1e5 for i in range(20)],
            ts_ms=list(range(10**9, 10**9 + 20)),
        )
        for j, k in enumerate(keys)
    ]
    payload = MetricBatchPayload(t0=T0, series=series)
    assert sum(len(s.steps) for s in payload.series) == MAX_POINTS_PER_EVENT
    with pytest.raises(ValueError, match="exceeds"):
        encode_event_line(make_event("r", 1, 0, payload, ts=T0, key=KEY))
    parts = split_for_events(payload)
    assert len(parts) > 1
    for part in parts:
        assert part.series and _line_bytes(part) <= MAX_EVENT_LINE_BYTES
    assert [p for part in parts for p in part.points()] == list(payload.points())


def test_split_for_events_splits_one_long_series_by_points() -> None:
    payload = MetricBatchPayload(t0=T0, series=[_series(1000, "x" * 64, **{"a": "b" * 63})])
    parts = split_for_events(payload, max_event_bytes=8 * 1024)
    assert len(parts) > 1
    for part in parts:
        assert _line_bytes(part) <= 8 * 1024
    assert [p for part in parts for p in part.points()] == list(payload.points())


def test_split_for_events_params_and_tags_keep_order() -> None:
    params = ParamsPayload(params=[ParamItem(key=f"p{i}", value="é" * 6000) for i in range(30)])
    parts = split_for_events(params)
    assert len(parts) > 1
    assert all(_line_bytes(p) <= MAX_EVENT_LINE_BYTES and p.params for p in parts)
    assert [i for p in parts for i in p.params] == params.params
    tags = TagsPayload(tags=[TagItem(key=f"t{i}", value="x" * 1000) for i in range(100)])
    tparts = split_for_events(tags, max_event_bytes=16 * 1024)
    assert len(tparts) > 1 and [i for p in tparts for i in p.tags] == tags.tags
    small = TagsPayload(tags=[TagItem(key="a", value="b")])
    assert split_for_events(small) == [small]
    with pytest.raises(ValueError, match="exceeds"):
        split_for_events(ParamsPayload(params=[params.params[0]]), max_event_bytes=4096)


# ================================================================================ run search


@pytest.mark.parametrize(
    ("text", "kind", "key", "labels", "agg", "op", "value"),
    [
        ("metric.rmse < 0.5", "metric", "rmse", (), "last", "<", 0.5),
        ("metric.rmse<=1e3", "metric", "rmse", (), "last", "<=", 1000.0),
        (
            "metric.loss{split=val}.min >= -2",
            "metric",
            "loss",
            (("split", "val"),),
            "min",
            ">=",
            -2.0,
        ),
        (
            "metric.step_size{split=v,chain=0}.count != 3",
            "metric",
            "step_size",
            (("chain", "0"), ("split", "v")),
            "count",
            "!=",
            3.0,
        ),
        ("metric.rmse.max = 1", "metric", "rmse", (), "max", "=", 1.0),
        ("param.fit.draws >= 1000", "param", "fit.draws", (), "last", ">=", 1000.0),
        ("param.fit.engine = nutpie", "param", "fit.engine", (), "last", "=", "nutpie"),
        ("param.trial.3.adstock/l_max ~ 4", "param", "trial.3.adstock/l_max", (), "last", "~", "4"),
        ("param.note != a b", "param", "note", (), "last", "!=", "a b"),
        ("tag.stage=candidate", "tag", "stage", (), "last", "=", "candidate"),
        ("tag.team ~ Grow", "tag", "team", (), "last", "~", "Grow"),
        ("name ~ adstock 4", "name", None, (), "last", "~", "adstock 4"),
        ("name = x", "name", None, (), "last", "=", "x"),
    ],
)
def test_parse_run_filter_valid(
    text: str, kind: str, key: str | None, labels: tuple[tuple[str, str], ...], agg: str, op: str,
    value: Any,
) -> None:  # fmt: skip
    f = parse_run_filter(text)
    assert (f.ref.kind, f.ref.key, f.ref.labels, f.ref.agg, f.op, f.value) == (
        kind, key, labels, agg, op, value,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("metric.rmse", "no operator"),
        ("metric.rmse ~ 1", "takes one of"),
        ("metric.rmse < abc", "not a number"),
        ("metric.rmse < nan", "finite"),
        ("metric.rmse < inf", "finite"),
        ("metric.train/loss < 1", "metric key"),
        ("metric.rmse.median < 1", "aggregate"),
        ("metric.rmse{Split=val} < 1", "label key"),
        ("metric.rmse{split} < 1", "key=value"),
        ("metric.rmse{split=val < 1", "metric"),
        ("param.fit.draws < many", "not a number"),
        ("param.a b = 1", "param key"),
        ("tag.stage < 3", "takes one of"),
        ("tag.mapi.source = x", "reserved"),
        ("name != x", "takes one of"),
        ("created_at > 2026", "only in order_by"),
        ("owner = me", "must be"),
        ("=x", "must be"),
    ],
)
def test_parse_run_filter_errors(text: str, match: str) -> None:
    with pytest.raises(ValueError, match=match) as exc:
        parse_run_filter(text)
    assert repr(text) in str(exc.value) or text.split()[0] in str(exc.value)


def test_run_ref_text_is_canonical() -> None:
    assert parse_run_ref("metric.loss{split=val,chain=0}.last").text() == (
        "metric.loss{chain=0,split=val}"
    )
    assert parse_run_ref("metric.loss.min").text() == "metric.loss.min"
    assert parse_run_ref(" param.fit.draws ").text() == "param.fit.draws"
    assert parse_run_ref("tag.stage").text() == "tag.stage"
    assert parse_run_ref("name").text() == "name"
    assert parse_run_ref("created_at").text() == "created_at"
    for text in ("metric.loss{a=b}.max", "param.x/y", "tag.t", "metric.rmse"):
        assert parse_run_ref(parse_run_ref(text).text()).text() == text
    assert RunRef(kind="metric", key="x", labels=(("a", "1"),), agg="count").text() == (
        "metric.x{a=1}.count"
    )


def test_parse_run_order() -> None:
    o = parse_run_order("-metric.rmse")
    assert o.descending and o.ref == RunRef(kind="metric", key="rmse")
    o = parse_run_order("metric.loss{split=val}.min")
    assert not o.descending and o.ref.labels == (("split", "val"),) and o.ref.agg == "min"
    assert parse_run_order("created_at") == parse_run_order("created_at")
    assert parse_run_order("-created_at").descending
    with pytest.raises(ValueError, match="order_by"):
        parse_run_order("-bogus")


# ===================================================================================== HTTP


def test_v011_rows_are_part_of_the_table_with_access() -> None:
    assert ENDPOINTS[-len(V011_ENDPOINTS) :] == V011_ENDPOINTS
    by_op = {e.operation_id: e for e in V011_ENDPOINTS}
    assert set(by_op) == {
        "update_run", "list_run_metrics", "get_metric_history", "log_run_metrics",
        "list_run_params", "log_run_params",
    }  # fmt: skip
    assert (by_op["update_run"].method, by_op["update_run"].path) == ("PATCH", "/v1/runs/{run_id}")
    assert by_op["get_metric_history"].path == "/v1/runs/{run_id}/metrics/{key}"
    assert by_op["list_run_metrics"].response == "Page[MetricSeries]"
    assert api.ErrorCode.CONFLICT in by_op["log_run_params"].errors
    for op in ("update_run", "log_run_metrics", "log_run_params"):
        acc = ROUTE_ACCESS[op]
        assert acc.scope is RouteScope.PROJECT
        assert (acc.resource, acc.action) == (ResourceKind.RUN, Action.UPDATE)
    for op in ("list_run_metrics", "get_metric_history", "list_run_params"):
        assert (ROUTE_ACCESS[op].resource, ROUTE_ACCESS[op].action) == (
            ResourceKind.RUN, Action.READ,
        )  # fmt: skip
    rows = {e.operation_id: e for e in ENDPOINTS}
    for q in ("parent_run_id", "name", "tag", "filter", "order_by", "column"):
        assert q in rows["list_runs"].query
    assert api.ErrorCode.SCHEMA_INVALID in rows["list_runs"].errors
    assert "level" in rows["run_logs"].query


def test_permission_and_scopes() -> None:
    rule = PERMISSIONS[(ResourceKind.RUN, Action.UPDATE)]
    assert rule.min_role is ProjectRole.RUNNER
    assert scope_allows(["runs:write"], "update", "run")
    assert scope_allows(["runs:write"], "read", "run")
    assert not scope_allows(["runs:create"], "update", "run")
    assert not scope_allows(["models:read"], "update", "run")
    assert ScopeName.RUNS_WRITE.value == "runs:write"
    assert AuditAction.RUN_UPDATE.value == "run.update"
    assert AuditAction.RUN_LOG.value == "run.log"


def test_sse_has_a_tracking_frame() -> None:
    assert SseEventName.TRACKING.value == "tracking"


def test_run_and_log_line_dtos() -> None:
    ex = Run.model_json_schema()["examples"][0]
    run = Run.model_validate(ex)
    assert run.tags and run.name and run.columns is None
    assert "columns" not in json.loads(run.model_dump_json())
    with_cols = Run.model_validate({**ex, "columns": {"metric.rmse": 0.5, "tag.stage": None}})
    assert json.loads(with_cols.model_dump_json())["columns"]["metric.rmse"] == 0.5
    line = LogLine.model_validate(LogLine.model_json_schema()["examples"][1])
    assert line.stream == "user" and line.level == "warning" and line.fields == {"point": 3}
    plain = LogLine.model_validate(LogLine.model_json_schema()["examples"][0])
    assert "fields" not in json.loads(plain.model_dump_json())


def test_metric_and_param_writes() -> None:
    MetricPointIn(key="loss", value=1.0, step=0)
    for bad in ({"value": math.nan}, {"value": math.inf}, {"step": -1}, {"key": "a/b"},
                {"labels": {"A": "x"}}):  # fmt: skip
        with pytest.raises(ValidationError):
            MetricPointIn.model_validate({"key": "loss", "value": 1.0, **bad})
    with pytest.raises(ValidationError):
        MetricsWrite(metrics=[])
    with pytest.raises(ValidationError):
        MetricsWrite(metrics=[MetricPointIn(key="x", value=1.0)] * 1001)
    assert ParamsWrite(params={"a": 1, "b": "x", "c": True}).params["c"] is True
    with pytest.raises(ValidationError):
        ParamsWrite(params={})
    with pytest.raises(ValidationError):
        ParamsWrite(params={"a b": 1})


def test_run_patch_merge_semantics() -> None:
    assert RunPatch(name="  x  ").name == "x"
    blank = RunPatch(name="   ")
    assert blank.name is None and blank.model_fields_set == {"name"}
    cleared = RunPatch.model_validate({"note": None})
    assert cleared.model_fields_set == {"note"}
    tags = RunPatch(tags={"stage": "a", "old": None})
    assert tags.tags == {"stage": "a", "old": None}
    with pytest.raises(ValidationError, match="at least one"):
        RunPatch()
    with pytest.raises(ValidationError, match="reserved"):
        RunPatch(tags={"mapi.source": "x"})
    with pytest.raises(ValidationError):
        RunPatch(tags={f"t{i}": "x" for i in range(101)})
    with pytest.raises(ValidationError):
        RunPatch(name="x" * 201)
    with pytest.raises(ValidationError):
        RunPatch.model_validate({"name": "x", "extra": 1})


def test_backtest_view_has_pooled_by_horizon() -> None:
    view = BacktestView(by_horizon={"1": {"mae": 1.0}, "2": {"mae": None}})
    assert view.by_horizon["1"]["mae"] == 1.0
    assert BacktestView().by_horizon == {}


# ======================================================================================= docs


def _docs() -> Path:
    docs = next(
        (p / "docs" for p in Path(__file__).resolve().parents if (p / "docs").is_dir()), None
    )
    if docs is None or not (docs / "reference" / "http-api.md").exists():  # pragma: no cover
        pytest.skip("docs/ not available")
    return docs


def test_reference_docs_cover_tracking() -> None:
    docs = _docs()
    text = (docs / "reference" / "http-api.md").read_text("utf-8")
    for e in V011_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            assert f"`{dto}`" in text, dto
    for dto in (
        "MetricSeries",
        "MetricHistorySeries",
        "MetricPointIn",
        "TrackingFrame",
        "RunPatch",
    ):
        assert f"`{dto}`" in text, dto
    assert "`tracking`" in text and "`runs:write`" in text
    protocol = (docs / "reference" / "run-protocol.md").read_text("utf-8")
    for etype in ("metrics", "params", "tags"):
        assert f"`{etype}`" in protocol, etype
    assert (docs / "guides" / "tracking.md").exists()
