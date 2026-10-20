"""Run search (v0.11): ``filter`` / ``order_by`` / ``column`` / ``tag`` of ``GET /v1/runs``.

The grammar is :mod:`mapi_spec.tracking` (``parse_run_filter`` and friends); this module turns
parsed refs into SQL over the tracking tables:

* a **filter** is an ``EXISTS`` on the run's metric summary (one exact series), param or tag
  row, or a condition on ``runs.name``. A run without the key or series never matches, also
  not for ``!=``;
* **order_by** a metric, param, tag or the name sorts on a correlated value, ``NULLS LAST``,
  ties by id (newest first). ``created_at`` sorts on the id (ids are time-sortable), so the
  default order (``-created_at``) keeps the original id cursor;
* the **cursor** of a value order is ``[value..., id]`` (JSON ``null`` for a run without the
  value): the keyset predicate continues after it, nulls included;
* **columns** are read for one page at a time (one query per kind of ref);
* **run keys** (v0.14, :func:`run_keys`): the metric series, param keys and tag keys of the
  project's (or an experiment's) runs with how many runs have each, for the column picker.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mapi_spec.api import RunKeyCount, RunKeys
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tracking import (
    MAX_RUN_COLUMNS,
    MAX_RUN_FILTERS,
    RunFilter,
    RunOrder,
    RunRef,
    check_tag_key,
    labels_key,
    parse_labels_key,
    parse_run_filter,
    parse_run_order,
    parse_run_ref,
)
from sqlalchemy import ColumnElement, and_, exists, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.db import models as m
from mapi_server.services.runs import Scope

__all__ = [
    "MAX_KEY_METRICS",
    "MAX_KEY_PARAMS",
    "MAX_KEY_TAGS",
    "MAX_TAG_QUERIES",
    "RunQuery",
    "column_values",
    "parse_run_query",
    "run_keys",
]

MAX_TAG_QUERIES = 16
MAX_KEY_METRICS = 500
MAX_KEY_PARAMS = 500
MAX_KEY_TAGS = 200

_OPS: Mapping[str, Callable[[Any, Any], Any]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "=": operator.eq,
    "!=": operator.ne,
}

Value = float | int | str | None


def _invalid(detail: str, pointer: str) -> MapiError:
    return MapiError(detail, code=ErrorCode.SCHEMA_INVALID, pointer=pointer)


@dataclass(frozen=True, slots=True)
class RunQuery:
    """The parsed search parameters of one ``list_runs`` request."""

    filters: tuple[RunFilter, ...] = ()
    order: RunOrder | None = None
    columns: tuple[RunRef, ...] = ()
    tags: tuple[tuple[str, str | None], ...] = ()

    # ------------------------------------------------------------------ SQL pieces

    def where(self) -> list[ColumnElement[bool]]:
        out = [_filter_clause(f) for f in self.filters]
        for key, value in self.tags:
            t = m.RunTag
            cond = and_(t.run_id == m.Run.id, t.key == key)
            if value is not None:
                cond = and_(cond, t.value == value)
            out.append(exists().where(cond))
        return out

    @property
    def by_value(self) -> bool:
        """Whether the order sorts on a tracked value (else on the id)."""
        return self.order is not None and self.order.ref.kind != "created_at"

    def sort_keys(self) -> list[ColumnElement[Any]]:
        """The value expressions of a value order (empty for the id order)."""
        if self.order is None or not self.by_value:
            return []
        return _ref_values(self.order.ref)

    def order_clause(self, keys: Sequence[ColumnElement[Any]]) -> list[Any]:
        if self.order is None or not self.by_value:
            asc = self.order is not None and not self.order.descending
            return [m.Run.id.asc() if asc else m.Run.id.desc()]
        desc = self.order.descending
        return [
            *(k.desc().nulls_last() if desc else k.asc().nulls_last() for k in keys),
            m.Run.id.desc(),
        ]

    def after(self, keys: Sequence[ColumnElement[Any]], cursor: Any) -> ColumnElement[bool]:
        """Rows after ``cursor`` (decoded) in this order."""
        if not self.by_value:
            if not isinstance(cursor, str):
                raise _invalid("the cursor is invalid", "/query/cursor")
            asc = self.order is not None and not self.order.descending
            return m.Run.id > cursor if asc else m.Run.id < cursor
        assert self.order is not None
        kinds = _ref_kinds(self.order.ref)
        if (
            not isinstance(cursor, list)
            or len(cursor) != len(keys) + 1
            or len(kinds) != len(keys)
            or not isinstance(cursor[-1], str)
            # each value must have the type of its column (else a database type error)
            or not all(_of_kind(v, k) for v, k in zip(cursor[:-1], kinds, strict=True))
        ):
            raise _invalid("the cursor is invalid", "/query/cursor")
        cmp = operator.lt if self.order.descending else operator.gt
        values, last_id = cursor[:-1], cursor[-1]
        terms: list[ColumnElement[bool]] = []
        equal: list[ColumnElement[bool]] = []
        for key, value in zip(keys, values, strict=True):
            if value is None:
                # NULLS LAST: nothing but other nulls comes after a null
                equal.append(key.is_(None))
            else:
                terms.append(and_(*equal, or_(cmp(key, value), key.is_(None))))
                equal.append(key == value)
        terms.append(and_(*equal, m.Run.id < last_id))
        return or_(*terms)


def parse_run_query(
    filters: Sequence[str],
    order_by: str | None,
    columns: Sequence[str],
    tags: Sequence[str],
) -> RunQuery:
    """Parse the search parameters; grammar errors are 422 ``SCHEMA_INVALID``."""
    if len(filters) > MAX_RUN_FILTERS:
        raise _invalid(f"at most {MAX_RUN_FILTERS} filters are allowed", "/query/filter")
    if len(columns) > MAX_RUN_COLUMNS:
        raise _invalid(f"at most {MAX_RUN_COLUMNS} columns are allowed", "/query/column")
    if len(tags) > MAX_TAG_QUERIES:
        raise _invalid(f"at most {MAX_TAG_QUERIES} tag filters are allowed", "/query/tag")
    try:
        parsed_filters = tuple(parse_run_filter(f) for f in filters)
    except ValueError as exc:
        raise _invalid(str(exc), "/query/filter") from None
    try:
        order = parse_run_order(order_by) if order_by else None
    except ValueError as exc:
        raise _invalid(str(exc), "/query/order_by") from None
    try:
        refs = tuple(parse_run_ref(c) for c in columns)
    except ValueError as exc:
        raise _invalid(str(exc), "/query/column") from None
    parsed_tags: list[tuple[str, str | None]] = []
    for item in tags:
        key, sep, value = item.partition(":")
        try:
            check_tag_key(key, reserved=True)
        except ValueError as exc:
            raise _invalid(str(exc), "/query/tag") from None
        parsed_tags.append((key, value if sep else None))
    return RunQuery(parsed_filters, order, refs, tuple(parsed_tags))


# ---------------------------------------------------------------------------------- filters


def _series(ref: RunRef) -> ColumnElement[bool]:
    s = m.RunMetricSummary
    return and_(
        s.run_id == m.Run.id, s.key == ref.key, s.labels_key == labels_key(dict(ref.labels))
    )


def _agg(ref: RunRef) -> Any:
    return getattr(m.RunMetricSummary, ref.agg)  # last | min | max | count


def _filter_clause(f: RunFilter) -> ColumnElement[bool]:
    ref, op, value = f.ref, f.op, f.value
    if ref.kind == "metric":
        return exists().where(_series(ref), _OPS[op](_agg(ref), value))
    if ref.kind == "param":
        p = m.RunParam
        if op == "~":
            cond = p.value_text.icontains(str(value), autoescape=True)
        elif op in ("=", "!="):
            cond = _OPS[op](p.value_text, str(value))
        else:
            cond = _OPS[op](p.value_num, value)
        return exists().where(p.run_id == m.Run.id, p.key == ref.key, cond)
    if ref.kind == "tag":
        t = m.RunTag
        tag_cond = (
            t.value.icontains(str(value), autoescape=True)
            if op == "~"
            else _OPS[op](t.value, str(value))
        )
        return exists().where(t.run_id == m.Run.id, t.key == ref.key, tag_cond)
    if ref.kind == "name":
        if op == "~":
            return m.Run.name.icontains(str(value), autoescape=True)
        return m.Run.name == str(value)
    raise _invalid(f"{ref.text()!r} cannot be filtered on", "/query/filter")  # pragma: no cover


def _ref_kinds(ref: RunRef) -> tuple[str, ...]:
    """The type (``num`` or ``text``) of each value :func:`_ref_values` sorts on."""
    if ref.kind == "metric":
        return ("num",)
    if ref.kind == "param":
        return ("num", "text")
    return ("text",)  # tag, name


def _of_kind(value: Any, kind: str) -> bool:
    if value is None:
        return True
    if kind == "num":
        return isinstance(value, int | float) and not isinstance(value, bool)
    return isinstance(value, str)


def _ref_values(ref: RunRef) -> list[ColumnElement[Any]]:
    """Correlated value expressions a ref sorts on (a param sorts on number, then text)."""
    if ref.kind == "metric":
        return [select(_agg(ref)).where(_series(ref)).scalar_subquery()]
    if ref.kind == "param":
        p = m.RunParam
        where = (p.run_id == m.Run.id, p.key == ref.key)
        return [
            select(p.value_num).where(*where).scalar_subquery(),
            select(p.value_text).where(*where).scalar_subquery(),
        ]
    if ref.kind == "tag":
        t = m.RunTag
        return [select(t.value).where(t.run_id == m.Run.id, t.key == ref.key).scalar_subquery()]
    return [m.Run.name.expression]  # name (created_at never reaches here)


# ---------------------------------------------------------------------------------- columns


async def column_values(
    session: AsyncSession, runs: Sequence[m.Run], refs: Sequence[RunRef]
) -> dict[str, dict[str, Value]]:
    """``{run_id: {ref.text(): value}}`` for one page (``None`` where a run has no value).
    A param's value is its number when it has one, else its text."""
    ids = [r.id for r in runs]
    out: dict[str, dict[str, Value]] = {r.id: {ref.text(): None for ref in refs} for r in runs}
    if not ids or not refs:
        return out
    metrics = [r for r in refs if r.kind == "metric"]
    if metrics:
        s = m.RunMetricSummary
        series = {(str(r.key), labels_key(dict(r.labels))) for r in metrics}
        rows = (
            await session.execute(
                select(s).where(
                    s.run_id.in_(ids),
                    tuple_(s.key, s.labels_key).in_(sorted(series)),
                )
            )
        ).scalars()
        by = {(x.run_id, x.key, x.labels_key): x for x in rows}
        for run_id in ids:
            for ref in metrics:
                row = by.get((run_id, str(ref.key), labels_key(dict(ref.labels))))
                if row is not None:
                    out[run_id][ref.text()] = getattr(row, ref.agg)
    params = [r for r in refs if r.kind == "param"]
    if params:
        p = m.RunParam
        prows = await session.execute(
            select(p.run_id, p.key, p.value_text, p.value_num).where(
                p.run_id.in_(ids), p.key.in_({r.key for r in params})
            )
        )
        pvals = {
            (r.run_id, r.key): (r.value_num if r.value_num is not None else r.value_text)
            for r in prows.all()
        }
        for run_id in ids:
            for ref in params:
                out[run_id][ref.text()] = pvals.get((run_id, ref.key))
    tags = [r for r in refs if r.kind == "tag"]
    if tags:
        t = m.RunTag
        trows = await session.execute(
            select(t.run_id, t.key, t.value).where(
                t.run_id.in_(ids), t.key.in_({r.key for r in tags})
            )
        )
        tvals = {(r.run_id, r.key): r.value for r in trows.all()}
        for run_id in ids:
            for ref in tags:
                out[run_id][ref.text()] = tvals.get((run_id, ref.key))
    for run in runs:
        for ref in refs:
            if ref.kind == "name":
                out[run.id][ref.text()] = run.name
            elif ref.kind == "created_at":
                out[run.id][ref.text()] = run.created_at.isoformat().replace("+00:00", "Z")
    return out


# ------------------------------------------------------------------------------- run keys


async def run_keys(
    session: AsyncSession, scope: Scope, experiment_id: str | None, prefix: str | None
) -> RunKeys:
    """Metric series, param keys and tag keys (platform ``mapi.`` tags included) of the runs
    of the project, or of one experiment, with the number of runs that have each; most
    common first, then by key. ``prefix`` keeps keys that start with it."""
    truncated = False

    async def count(
        model: type[m.RunMetricSummary | m.RunParam | m.RunTag], cap: int, *, series: bool
    ) -> list[RunKeyCount]:
        nonlocal truncated
        cols: list[Any] = [model.key]
        if series:
            cols.append(m.RunMetricSummary.labels_key)
        n = func.count(func.distinct(model.run_id)).label("n")
        stmt = select(*cols, n).where(
            model.tenant_id == scope.tenant_id, model.project_id == scope.project_id
        )
        if experiment_id is not None:
            stmt = stmt.join(m.Run, m.Run.id == model.run_id).where(
                m.Run.experiment_id == experiment_id
            )
        if prefix:
            stmt = stmt.where(model.key.startswith(prefix, autoescape=True))
        rows = (
            await session.execute(stmt.group_by(*cols).order_by(n.desc(), *cols).limit(cap + 1))
        ).all()
        if len(rows) > cap:
            truncated = True
        return [
            RunKeyCount(
                key=r.key,
                labels=parse_labels_key(r.labels_key) if series else {},
                runs=int(r.n),
            )
            for r in rows[:cap]
        ]

    metrics = await count(m.RunMetricSummary, MAX_KEY_METRICS, series=True)
    params = await count(m.RunParam, MAX_KEY_PARAMS, series=False)
    tags = await count(m.RunTag, MAX_KEY_TAGS, series=False)
    return RunKeys(metrics=metrics, params=params, tags=tags, truncated=truncated)
