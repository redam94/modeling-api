"""XrJSON: small labelled datasets inline in JSON requests (plan §3.2; M3 endpoints).

The shape is ``xr.Dataset.to_dict()``::

    {"coords": {"time": {"dims": ["time"], "data": ["2026-11-01", ...]}, ...},
     "data_vars": {"spend": {"dims": ["geo", "channel", "time"], "data": [[[...]]]}},
     "dims": {"time": 8, ...},           # optional
     "attrs": {}}                        # optional

It is for payloads up to :data:`MAX_XRJSON_BYTES` (one scenario, an endpoint score call),
so curl, TypeScript and Apps Script callers need no upload. The control plane checks only
the *envelope* here (names, nesting, sizes, element and string caps, ragged lists) and
stores the JSON as a run input; the values are decoded into arrays only inside the runner
sandbox (``mapi_sdk`` wire format), which also checks dtypes and the schema.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Final

from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_spec._dto import RequestModel, dto_examples
from mapi_spec._types import Name
from mapi_spec.errors import ErrorCode, MapiError

__all__ = [
    "MAX_XRJSON_BYTES",
    "MAX_XRJSON_ELEMENTS",
    "MAX_XRJSON_STRING",
    "XrJsonDataset",
    "XrJsonVariable",
    "check_xrjson_size",
    "observed_labels",
    "table_cell",
    "xrjson_from_rows",
]

MAX_XRJSON_BYTES: Final = 5 * 1024 * 1024
MAX_XRJSON_ELEMENTS: Final = 1_000_000
"""Leaf values across every coord and data variable."""
MAX_XRJSON_STRING: Final = 1024
MAX_XRJSON_DIMS: Final = 16

Leaf = str | int | float | bool | None


class XrJsonVariable(RequestModel):
    """A coord or data variable: ``data`` nested ``len(dims)`` deep (a scalar for no dims)."""

    dims: list[Name] = Field(default_factory=list, max_length=MAX_XRJSON_DIMS)
    data: JsonValue
    attrs: dict[str, Leaf] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def _shape(self) -> XrJsonVariable:
        if len(set(self.dims)) != len(self.dims):
            raise ValueError("dims must be unique")
        _check_nesting(self.data, len(self.dims))
        return self


def _check_nesting(data: Any, depth: int) -> list[int]:
    """Return the shape of ``data`` (nested lists exactly ``depth`` deep, rectangular,
    scalar leaves of bounded size). Raises ``ValueError``."""
    if depth == 0:
        if isinstance(data, list | dict):
            raise ValueError("data is nested deeper than its dims")
        if isinstance(data, str) and len(data) > MAX_XRJSON_STRING:
            raise ValueError(f"strings are at most {MAX_XRJSON_STRING} characters")
        return []
    if not isinstance(data, list):
        raise ValueError("data is nested less deep than its dims")
    shapes = {tuple(_check_nesting(x, depth - 1)) for x in data}
    if len(shapes) > 1:
        raise ValueError("data is ragged (rows of different lengths)")
    inner = list(next(iter(shapes))) if shapes else [0] * (depth - 1)
    return [len(data), *inner]


class XrJsonDataset(RequestModel):
    """An inline dataset (``xr.Dataset.to_dict()`` shape). Dim lengths must agree across
    variables; every dim of a data variable should have a coord (checked in the sandbox
    against the model's schema)."""

    coords: dict[Name, XrJsonVariable] = Field(default_factory=dict, max_length=64)
    data_vars: dict[Name, XrJsonVariable] = Field(..., min_length=1, max_length=256)
    dims: dict[Name, int] = Field(default_factory=dict, max_length=MAX_XRJSON_DIMS)
    attrs: dict[str, Leaf] = Field(default_factory=dict, max_length=64)

    model_config = dto_examples(
        {
            "coords": {
                "time": {"dims": ["time"], "data": ["2026-11-01", "2026-11-08"]},
                "channel": {"dims": ["channel"], "data": ["tv", "search"]},
            },
            "data_vars": {
                "spend": {"dims": ["channel", "time"], "data": [[1000.0, 1200.0], [400.0, 380.0]]}
            },
        }
    )

    @field_validator("dims")
    @classmethod
    def _sizes(cls, v: dict[str, int]) -> dict[str, int]:
        if any(n < 0 for n in v.values()):
            raise ValueError("dim sizes cannot be negative")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> XrJsonDataset:
        sizes: dict[str, int] = dict(self.dims)
        elements = 0
        for name, var in [*self.coords.items(), *self.data_vars.items()]:
            shape = _check_nesting(var.data, len(var.dims))
            n = 1
            for dim, size in zip(var.dims, shape, strict=True):
                if sizes.setdefault(dim, size) != size:
                    raise ValueError(
                        f"{name!r}: dim {dim!r} has {size} values, expected {sizes[dim]}"
                    )
                n *= size
            elements += n
            if elements > MAX_XRJSON_ELEMENTS:
                raise ValueError(f"more than {MAX_XRJSON_ELEMENTS} values")
        if clash := sorted(set(self.coords) & set(self.data_vars)):
            raise ValueError(f"names used as both coord and data variable: {clash}")
        return self

    @property
    def var_names(self) -> list[str]:
        return list(self.data_vars)


def check_xrjson_size(payload: Any) -> int:
    """Serialized size of an inline payload; raises ``PAYLOAD_TOO_LARGE`` over
    :data:`MAX_XRJSON_BYTES` (check before model validation to bound the work)."""
    size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size > MAX_XRJSON_BYTES:
        raise MapiError(
            f"inline data is {size} bytes; upload larger data as a dataset "
            f"(max {MAX_XRJSON_BYTES} bytes inline)",
            code=ErrorCode.PAYLOAD_TOO_LARGE,
        )
    return size


def _any_value(node: Any) -> bool:
    if isinstance(node, list):
        return any(_any_value(x) for x in node)
    if isinstance(node, float):
        return node == node  # NaN is not a value (strict JSON has none, but stay safe)
    return node is not None and node != ""


def observed_labels(payload: XrJsonDataset, var: str, dim: str) -> list[Leaf] | None:
    """The labels of ``dim`` at which data variable ``var`` holds at least one value (not
    ``null``) - the periods with actuals of a forecast payload (:mod:`mapi_spec.actuals`).

    A null check over JSON leaves; the values themselves are never decoded. ``None`` when
    ``var`` is not a data variable, does not run along ``dim``, or ``dim`` has no 1-D coord.
    """
    spec = payload.data_vars.get(var)
    coord = payload.coords.get(dim)
    if spec is None or dim not in spec.dims or coord is None or list(coord.dims) != [dim]:
        return None
    labels = coord.data if isinstance(coord.data, list) else None
    if labels is None:
        return None
    axis = list(spec.dims).index(dim)
    hit: set[int] = set()

    def walk(node: Any, depth: int) -> None:
        children = node if isinstance(node, list) else []
        if depth == axis:
            hit.update(k for k, child in enumerate(children) if k not in hit and _any_value(child))
            return
        for child in children:
            walk(child, depth + 1)

    walk(spec.data, 0)
    out: list[Leaf] = []
    for k in sorted(hit):
        if k < len(labels):
            label = labels[k]
            if isinstance(label, str | int | float | bool) or label is None:
                out.append(label)
    return out


_EPOCH_NS_THRESHOLD: Final = 10**15
"""numpy ``datetime64[ns]`` scalars unbox to integers (nanoseconds) above this."""


def table_cell(value: Any) -> Leaf:
    """A table cell as an XrJSON leaf: empty (``None``, blank text, NaN, NaT) is ``null``;
    dates are ISO text (the date alone at midnight); numpy and pandas scalars are unboxed.
    Raises ``TypeError`` for anything else."""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else value
    try:
        if value != value:  # NaN-like: numpy NaN, pandas NaT (a datetime)
            return None
    except (TypeError, ValueError):  # pragma: no cover - exotic comparisons
        pass
    if isinstance(value, datetime):
        if value.tzinfo is None and value.time() == datetime.min.time():
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    to_py = getattr(value, "to_pydatetime", None)  # pandas Timestamp
    if callable(to_py):
        return table_cell(to_py())
    item = getattr(value, "item", None)  # numpy scalars
    if callable(item):
        unboxed = item()
        kind = getattr(getattr(value, "dtype", None), "kind", "")
        if kind == "M" and isinstance(unboxed, int) and unboxed > _EPOCH_NS_THRESHOLD:
            seconds, ns = divmod(unboxed, 10**9)
            unboxed = datetime(1970, 1, 1) + timedelta(seconds=seconds, microseconds=ns // 1000)
        return table_cell(unboxed)
    raise TypeError(
        f"a table cell must be a number, text, a date or empty, not {type(value).__name__}"
    )


def xrjson_from_rows(
    rows: Iterable[Mapping[str, Any]], variables: Mapping[str, Sequence[str]]
) -> XrJsonDataset:
    """A long table (one mapping per row: column -> cell) as an inline dataset.

    ``variables`` maps each value column to its dims; the dims' columns hold the
    coordinates, in the order they first appear. A variable no row has a column for is left
    out; empty cells (``None``, blank, NaN) are ``null`` - in a forecast an empty target
    marks a period to forecast (:mod:`mapi_spec.actuals`). Two rows that give one cell
    different values are an error. It is the layout of the Apps Script snippet's Input sheet
    (:mod:`mapi_spec.snippets`) and of ``EndpointSchema.input_table``.

    Raises ``ValueError`` for a missing or empty coordinate, conflicting duplicates or a
    table without any variable column.
    """
    table = [dict(r) for r in rows]
    present = {k for r in table for k in r}
    chosen = {n: list(dims) for n, dims in variables.items() if n in present}
    if not chosen:
        raise ValueError(
            f"the table needs a column for at least one of {sorted(variables)} "
            f"(it has {sorted(present)})"
        )
    index: dict[str, dict[Leaf, int]] = {}
    for dims in chosen.values():
        for dim in dims:
            if dim in index:
                continue
            if dim not in present:
                raise ValueError(f"the table needs a column named {dim!r} (a dim)")
            seen: dict[Leaf, int] = {}
            for k, row in enumerate(table):
                label = table_cell(row.get(dim))
                if label is None:
                    raise ValueError(f"row {k + 1} has no value in the column {dim!r}")
                seen.setdefault(label, len(seen))
            index[dim] = seen
    coords = {d: {"dims": [d], "data": list(labels)} for d, labels in index.items()}
    data_vars: dict[str, dict[str, Any]] = {}
    for name, dims in chosen.items():
        cells: dict[tuple[int, ...], Leaf] = {}
        for row in table:
            if name not in row:
                continue
            value = table_cell(row[name])
            if value is None:
                continue
            at = tuple(index[d][table_cell(row.get(d))] for d in dims)
            if at in cells and cells[at] != value:
                where = ", ".join(f"{d}={table_cell(row.get(d))}" for d in dims)
                raise ValueError(f"{name!r} has two different values at {where or 'its cell'}")
            cells[at] = value
        data_vars[name] = {"dims": dims, "data": _nest(cells, [len(index[d]) for d in dims])}
    return XrJsonDataset.model_validate({"coords": coords, "data_vars": data_vars})


def _nest(cells: Mapping[tuple[int, ...], Leaf], shape: Sequence[int]) -> Any:
    def fill(prefix: tuple[int, ...]) -> Any:
        if len(prefix) == len(shape):
            return cells.get(prefix)
        return [fill((*prefix, i)) for i in range(shape[len(prefix)])]

    return fill(())
