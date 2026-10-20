"""Request builders for ``mapi_spec.api.V011_ENDPOINTS`` (run tracking, v0.11) and the
run-search query of ``list_runs`` / the ``level`` filter of ``run_logs``.

Like :mod:`mapi_client._calls` the builders validate arguments (the run-search grammar with
the :mod:`mapi_spec.tracking` parsers, so a bad filter fails before any request), encode
paths and return a :class:`~mapi_client._http.Call`. The helpers below them build request
bodies shared by :class:`~mapi_client.Client` and :class:`~mapi_client.AsyncClient`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final

from mapi_spec.api import (
    API_PREFIX,
    PAGE_LIMIT_MAX,
    MetricHistory,
    MetricPointIn,
    MetricSeries,
    MetricsWrite,
    MetricsWriteResult,
    Page,
    ParamsWrite,
    ParamsWriteResult,
    Run,
    RunParam,
    RunPatch,
)
from mapi_spec.tracking import (
    LOG_LEVELS,
    MAX_PARAMS_PER_WRITE,
    MAX_POINTS_PER_WRITE,
    MAX_RUN_COLUMNS,
    MAX_RUN_FILTERS,
    ParamValue,
    check_labels,
    check_metric_key,
    check_param_key,
    check_tag_key,
    parse_run_filter,
    parse_run_order,
    parse_run_ref,
)

from mapi_client._http import Call, dump, parse_model, segment

__all__ = [
    "LOG_STREAMS",
    "UNSET",
    "Unset",
    "get_metric_history",
    "list_run_metrics",
    "list_run_params",
    "log_level",
    "log_run_metrics",
    "log_run_params",
    "metric_points",
    "metrics_bodies",
    "params_bodies",
    "run_patch",
    "run_search_params",
    "tag_filters",
    "update_run",
]

V1: Final = API_PREFIX
LOG_STREAMS: Final = ("stdout", "stderr", "system", "user")
HISTORY_POINTS: Final = (10, 10_000)


class Unset:
    """Type of :data:`UNSET`: "leave this field out of the patch" (``None`` clears it)."""

    _instance: Unset | None = None

    def __new__(cls) -> Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = Unset()


def _runs(run_id: str) -> str:
    return f"{V1}/runs/{segment(run_id)}"


def _page(limit: int | None, cursor: str | None) -> dict[str, str | int | list[str]]:
    params: dict[str, str | int | list[str]] = {}
    if limit is not None:
        if not 1 <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be between 1 and {PAGE_LIMIT_MAX}")
        params["limit"] = limit
    if cursor is not None:
        params["cursor"] = cursor
    return params


def _attempt(value: int) -> int:
    if value < 1:
        raise ValueError("attempt must be >= 1")
    return value


# ------------------------------------------------------------------------ list_runs, run_logs


def tag_filters(tags: Mapping[str, str | None] | Iterable[str] | None) -> list[str]:
    """``tag`` query values: ``key`` (the run has the tag) or ``key:value`` (exact value).

    A mapping gives ``{key: value}`` (``None``: any value); strings are passed as written.
    """
    if tags is None:
        return []
    items: list[tuple[str, str | None]] = []
    if isinstance(tags, Mapping):
        items = [(str(k), v) for k, v in tags.items()]
    else:
        for text in tags:
            k, sep, v = str(text).partition(":")
            items.append((k, v if sep else None))
    out = []
    for key, val in items:
        check_tag_key(key, reserved=True)  # filtering on platform tags is allowed
        out.append(key if val is None else f"{key}:{val}")
    return out


def run_search_params(
    *,
    parent_run_id: str | None = None,
    name: str | None = None,
    tags: Mapping[str, str | None] | Iterable[str] | None = None,
    filters: Sequence[str] = (),
    order_by: str | None = None,
    columns: Sequence[str] = (),
) -> dict[str, str | int | list[str]]:
    """The v0.11 search parameters of ``list_runs``, checked against the grammar.

    ``filters`` are ``<ref> <op> <value>`` strings (``metric.rmse < 0.5``,
    ``param.fit.draws >= 1000``, ``tag.stage = candidate``); ``order_by`` is one ref with
    an optional leading ``-`` (descending); ``columns`` are refs whose values the server
    returns in ``Run.columns``. Raises :class:`ValueError` on bad grammar.
    """
    if isinstance(filters, str) or isinstance(columns, str):
        raise ValueError("filters and columns take a list of strings")
    filters, columns = list(filters), list(columns)
    if len(filters) > MAX_RUN_FILTERS:
        raise ValueError(f"at most {MAX_RUN_FILTERS} filters, got {len(filters)}")
    if len(columns) > MAX_RUN_COLUMNS:
        raise ValueError(f"at most {MAX_RUN_COLUMNS} columns, got {len(columns)}")
    for f in filters:
        parse_run_filter(f)
    for col in columns:
        parse_run_ref(col)
    if order_by is not None:
        parse_run_order(order_by)
    params: dict[str, str | int | list[str]] = {}
    if parent_run_id is not None:
        params["parent_run_id"] = parent_run_id
    if name is not None:
        params["name"] = name
    if tag_list := tag_filters(tags):
        params["tag"] = tag_list
    if filters:
        params["filter"] = [f.strip() for f in filters]
    if order_by is not None:
        params["order_by"] = order_by.strip()
    if columns:
        params["column"] = [c.strip() for c in columns]
    return params


def log_level(level: str | None) -> str | None:
    """The ``level`` filter of ``run_logs`` (minimum level), validated."""
    if level is not None and level not in LOG_LEVELS:
        raise ValueError(f"level must be one of {', '.join(LOG_LEVELS)}")
    return level


# -------------------------------------------------------------------------------- builders


def update_run(run_id: str, body: RunPatch) -> Call[Run]:
    # operation: update_run (a merge patch: sending it twice has the same result)
    return Call("PATCH", _runs(run_id), parse_model(Run), json=_patch_json(body), retry_safe=True)


def _patch_json(body: RunPatch) -> dict[str, object]:
    # a merge patch: only the fields that were set, with their nulls (null clears / deletes)
    return body.model_dump(mode="json", include=body.model_fields_set)


def list_run_metrics(
    run_id: str,
    limit: int | None,
    cursor: str | None,
    *,
    key: str | None = None,
    attempt: int | None = None,
) -> Call[Page[MetricSeries]]:
    # operation: list_run_metrics
    params = _page(limit, cursor)
    if key is not None:
        params["key"] = check_metric_key(key)
    if attempt is not None:
        params["attempt"] = _attempt(attempt)
    return Call("GET", f"{_runs(run_id)}/metrics", parse_model(Page[MetricSeries]), params=params)


def get_metric_history(
    run_id: str,
    key: str,
    *,
    max_points: int | None = None,
    attempt: int | None = None,
    labels: Mapping[str, str] | None = None,
) -> Call[MetricHistory]:
    # operation: get_metric_history (``label`` = exact match per label, repeatable)
    params: dict[str, str | int | list[str]] = {}
    if max_points is not None:
        lo, hi = HISTORY_POINTS
        if not lo <= max_points <= hi:
            raise ValueError(f"max_points must be between {lo} and {hi}")
        params["max_points"] = max_points
    if attempt is not None:
        params["attempt"] = _attempt(attempt)
    if labels:
        params["label"] = [f"{k}:{v}" for k, v in check_labels(labels).items()]
    path = f"{_runs(run_id)}/metrics/{segment(check_metric_key(key))}"
    return Call("GET", path, parse_model(MetricHistory), params=params)


def log_run_metrics(run_id: str, body: MetricsWrite) -> Call[MetricsWriteResult]:
    # operation: log_run_metrics; a resent stepped point is a duplicate (the first wins)
    return Call(
        "POST",
        f"{_runs(run_id)}/metrics",
        parse_model(MetricsWriteResult),
        json=dump(body),
        retry_safe=True,
    )


def list_run_params(
    run_id: str, limit: int | None, cursor: str | None, *, prefix: str | None = None
) -> Call[Page[RunParam]]:
    # operation: list_run_params
    params = _page(limit, cursor)
    if prefix is not None:
        params["prefix"] = prefix
    return Call("GET", f"{_runs(run_id)}/params", parse_model(Page[RunParam]), params=params)


def log_run_params(run_id: str, body: ParamsWrite) -> Call[ParamsWriteResult]:
    # operation: log_run_params; params are write-once, so a resend is "unchanged"
    return Call(
        "POST",
        f"{_runs(run_id)}/params",
        parse_model(ParamsWriteResult),
        json=dump(body),
        retry_safe=True,
    )


# ---------------------------------------------------------------------------------- bodies


def run_patch(
    *,
    name: str | Unset | None = UNSET,
    note: str | Unset | None = UNSET,
    tags: Mapping[str, str | None] | None = None,
) -> RunPatch:
    """A :class:`RunPatch` with only the given fields (``UNSET``: leave out; ``None``: clear)."""
    fields: dict[str, object] = {}
    if not isinstance(name, Unset):
        fields["name"] = name
    if not isinstance(note, Unset):
        fields["note"] = note
    if tags:
        fields["tags"] = {check_tag_key(k): v for k, v in tags.items()}
    if not fields:
        raise ValueError("set at least one of name, note or tags")
    return RunPatch.model_validate(fields)


def metric_points(
    values: Mapping[str, float] | Iterable[MetricPointIn],
    *,
    step: int | None = None,
    labels: Mapping[str, str] | None = None,
    ts: datetime | None = None,
) -> list[MetricPointIn]:
    """Points of a mapping ``{key: value}`` (sharing step, labels, ts) or the points as given.

    Non-finite values are dropped (as by the SDK's tracking API); bad keys and labels raise
    :class:`ValueError` before any request.
    """
    if not isinstance(values, Mapping):
        return [MetricPointIn.model_validate(p) for p in values]
    lab = check_labels(labels)
    out = []
    for key, value in values.items():
        x = float(value)
        if math.isfinite(x):
            out.append(
                MetricPointIn(key=check_metric_key(key), value=x, step=step, ts=ts, labels=lab)
            )
    return out


def metrics_bodies(points: Sequence[MetricPointIn]) -> list[MetricsWrite]:
    """``MetricsWrite`` bodies of at most 1000 points each (order kept)."""
    n = MAX_POINTS_PER_WRITE
    return [MetricsWrite(metrics=list(points[i : i + n])) for i in range(0, len(points), n)]


def params_bodies(params: Mapping[str, ParamValue]) -> list[ParamsWrite]:
    """``ParamsWrite`` bodies of at most 500 params each (order kept)."""
    items = [(check_param_key(k), v) for k, v in params.items()]
    n = MAX_PARAMS_PER_WRITE
    return [ParamsWrite(params=dict(items[i : i + n])) for i in range(0, len(items), n)]


def sum_metrics(results: Iterable[MetricsWriteResult]) -> MetricsWriteResult:
    acc, dup = 0, 0
    for r in results:
        acc, dup = acc + r.accepted, dup + r.duplicates
    return MetricsWriteResult(accepted=acc, duplicates=dup)


def sum_params(results: Iterable[ParamsWriteResult]) -> ParamsWriteResult:
    acc, same = 0, 0
    for r in results:
        acc, same = acc + r.accepted, same + r.unchanged
    return ParamsWriteResult(accepted=acc, unchanged=same)
