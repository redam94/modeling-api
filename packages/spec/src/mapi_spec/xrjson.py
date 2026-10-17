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
