"""Scenario rows of a layout 2 dashboard: check the values a client sends and build the
inline dataset the model scores (v0.17, pure Python).

The server checks every value against the **published revision's** input defs
(:class:`~mapi_spec.dashboard_v2.ScenarioRowsDef`) and refuses, never clips, a value it does
not accept (D13): unknown names; numbers that are not finite, outside ``[min, max]`` or off
the step grid (tolerance 1e-9 × the range); integers with a fraction; choices that match no
choice (matched as :func:`~mapi_spec.dashboard_metrics.normalise_label` does); dates that are
not ISO dates in ``[date_min, date_max]``; toggles that are not true or false; text that is
not plain or longer than 128 characters; more rows than ``max_rows``. Each refusal is an
:class:`~mapi_spec.errors.Issue` with code ``DASHBOARD_INPUT_INVALID`` and the pointer
``/rows/<i>/<name>``.

:func:`rows_payload` then merges the defaults and the ``fixed`` values, gives each row its
coordinates (:class:`RowContext`: a new level ``case1..N`` on a ``new_levels`` row dim, the
next periods on an ``extend`` dim, only existing levels on a ``fixed`` dim) and returns the
XrJSON dataset (one cell per row, ``null`` elsewhere) for an inline predict.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Final

from pydantic import Field, model_validator

from mapi_spec._dto import RequestModel
from mapi_spec._types import Name
from mapi_spec.dashboard_metrics import match_label
from mapi_spec.dashboard_v2 import (
    DimBind,
    ScenarioInputDef,
    ScenarioRowsDef,
    VarBind,
)
from mapi_spec.dashboards import _plain
from mapi_spec.errors import ErrorCode, Issue, json_pointer
from mapi_spec.schema import DimPolicy
from mapi_spec.xrjson import XrJsonDataset, xrjson_from_rows

__all__ = [
    "HORIZON_KEY",
    "MAX_TEXT_CHARS",
    "STEP_TOLERANCE",
    "Coordinate",
    "InputValue",
    "RowContext",
    "check_rows",
    "check_value",
    "coerce_value",
    "resolve_row",
    "row_coordinates",
    "row_policy_issues",
    "rows_payload",
]

HORIZON_KEY: Final = "horizon"
"""The row key that sets how many periods ahead a forecast scenario row reaches (only when
the defs have a ``horizon`` and no input is named ``horizon``)."""
MAX_TEXT_CHARS: Final = 128
STEP_TOLERANCE: Final = 1e-9
"""A number is on the step grid within this fraction of the input's range."""
MAX_ISSUES: Final = 50
_ISO_DAY: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")

InputValue = str | float | int | bool
Coordinate = str | int


# ===================================================================================== values


def _issue(pointer: str, message: str) -> Issue:
    return Issue(code=ErrorCode.DASHBOARD_INPUT_INVALID, pointer=pointer, message=message)


def _is_number(v: object) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def check_value(value: object, idef: ScenarioInputDef) -> str | None:
    """Why ``value`` is not a valid value of the input (plain text), or ``None`` when it is."""
    label = idef.label
    if idef.type in ("number", "integer"):
        if not _is_number(value):
            return f"{label} must be a number"
        assert isinstance(value, int | float)
        v = float(value)
        if not math.isfinite(v):
            return f"{label} must be a finite number"
        lo, hi = idef.min, idef.max
        assert lo is not None and hi is not None  # the def validator makes sure
        if not lo <= v <= hi:
            return f"{label} must be between {lo:g} and {hi:g}"
        if idef.type == "integer" and not v.is_integer():
            return f"{label} must be a whole number"
        step = idef.step if idef.step is not None else (1.0 if idef.type == "integer" else None)
        if step is not None:
            k = round((v - lo) / step)
            if abs(v - (lo + k * step)) > STEP_TOLERANCE * (hi - lo):
                return f"{label} must be {lo:g} plus a multiple of {step:g}"
        return None
    if idef.type == "toggle":
        return None if isinstance(value, bool) else f"{label} must be true or false"
    if idef.type == "date":
        if not isinstance(value, str) or not _ISO_DAY.match(value):
            return f"{label} must be a date as YYYY-MM-DD"
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return f"{label} must be a date as YYYY-MM-DD"
        assert idef.date_min is not None and idef.date_max is not None
        if not idef.date_min <= day <= idef.date_max:
            return f"{label} must be between {idef.date_min} and {idef.date_max}"
        return None
    # choice
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return f"{label} must be one of the choices"
    if isinstance(value, str):
        if len(value) > MAX_TEXT_CHARS:
            return f"{label} holds more than {MAX_TEXT_CHARS} characters"
        try:
            _plain(value, multiline=False)
        except ValueError:
            return f"{label} must be plain text"
    elif not math.isfinite(float(value)):
        return f"{label} must be one of the choices"
    if match_label(value, idef.choices) < 0:
        return f"{label} must be one of the choices"
    return None


def coerce_value(value: InputValue, idef: ScenarioInputDef) -> InputValue:
    """A checked value in the def's own form: the choice as the analyst wrote it, an integer
    as an ``int``, a number as a ``float``."""
    if idef.type == "choice":
        return idef.choices[match_label(value, idef.choices)]
    if idef.type == "integer":
        return int(float(value))
    if idef.type == "number":
        return float(value)
    return value


def _whole(value: object) -> int | None:
    if not _is_number(value):
        return None
    assert isinstance(value, int | float)
    if not math.isfinite(float(value)) or not float(value).is_integer():
        return None
    return int(value)


def _horizon_key(defs: ScenarioRowsDef) -> str | None:
    if defs.horizon is None or any(i.name == HORIZON_KEY for i in defs.inputs):
        return None
    return HORIZON_KEY


def check_rows(rows: Sequence[Mapping[str, object]], defs: ScenarioRowsDef) -> list[Issue]:
    """Every value of the scenario rows that the revision's defs do not accept (pointers
    ``/rows/<i>/<name>``; at most 50). A missing or ``null`` value takes the default."""
    issues: list[Issue] = []
    if not defs.enabled:
        return [_issue("/rows", "this dashboard does not take scenario rows")]
    if not rows:
        return [_issue("/rows", "send at least one row")]
    if len(rows) > defs.max_rows:
        issues.append(_issue("/rows", f"send at most {defs.max_rows} rows (got {len(rows)})"))
    by_name = {i.name: i for i in defs.inputs}
    hkey = _horizon_key(defs)
    for r, row in enumerate(rows):
        if not isinstance(row, Mapping):
            issues.append(_issue(json_pointer("rows", r), "a row must be an object"))
            continue
        for name, value in row.items():
            pointer = json_pointer("rows", r, str(name))
            if value is None:
                continue
            if hkey is not None and name == hkey:
                h = defs.horizon
                assert h is not None
                periods = _whole(value)
                if periods is None:
                    issues.append(_issue(pointer, "the number of periods must be a whole number"))
                elif not h.min <= periods <= h.max:
                    issues.append(
                        _issue(pointer, f"the number of periods must be {h.min} to {h.max}")
                    )
                continue
            idef = by_name.get(str(name))
            if idef is None:
                issues.append(_issue(pointer, f"{name!r} is not an input of this dashboard"))
                continue
            why = check_value(value, idef)
            if why is not None:
                issues.append(_issue(pointer, why))
        if len(issues) >= MAX_ISSUES:
            break
    return issues[:MAX_ISSUES]


def resolve_row(row: Mapping[str, object], defs: ScenarioRowsDef) -> dict[str, InputValue]:
    """The full values of one checked row: the defaults, the row's values (coerced) and the
    ``fixed`` values, plus :data:`HORIZON_KEY` (the default horizon when the row has none)
    for forecast rows. Raises ``ValueError`` for a value :func:`check_rows` refuses."""
    out: dict[str, InputValue] = {}
    for idef in defs.inputs:
        value = row.get(idef.name)
        if value is None:
            value = idef.default
        why = check_value(value, idef)
        if why is not None:
            raise ValueError(why)
        assert isinstance(value, str | int | float | bool)
        out[idef.name] = coerce_value(value, idef)
    hkey = _horizon_key(defs)
    if hkey is not None:
        assert defs.horizon is not None
        periods = _whole(row.get(hkey))
        if row.get(hkey) is not None and (
            periods is None or not defs.horizon.min <= periods <= defs.horizon.max
        ):
            raise ValueError(
                f"the number of periods must be {defs.horizon.min} to {defs.horizon.max}"
            )
        out[hkey] = periods if periods is not None else defs.horizon.default
    for k, v in defs.fixed.items():
        out[k] = v
    return out


# ================================================================================ row context


class RowContext(RequestModel):
    """How scenario rows become cells of the scored dataset (the server fills it from the
    revision's input contract).

    ``dims``: the dims of the scored variables, in order; ``var_dims``: the dims of a variable
    when they are not all of ``dims``; ``row_dim``: the dim each row adds a level on (when no
    input picks it); ``policies``: the dims' policies (``fixed`` dims take only ``levels``);
    ``next_labels``: the labels after the fit end of an ``extend`` row dim (else integers
    after the last integer level)."""

    dims: list[Name] = Field(..., min_length=1, max_length=16)
    var_dims: dict[Name, list[Name]] = Field(default_factory=dict, max_length=256)
    row_dim: Name | None = None
    policies: dict[Name, DimPolicy] = Field(default_factory=dict, max_length=16)
    levels: dict[Name, list[Coordinate]] = Field(default_factory=dict, max_length=16)
    next_labels: list[Coordinate] = Field(default_factory=list, max_length=104)
    label_prefix: str = Field("case", pattern=r"^[a-z][a-z0-9_]{0,15}$")

    @model_validator(mode="after")
    def _known(self) -> RowContext:
        if len(set(self.dims)) != len(self.dims):
            raise ValueError("dims must be unique")
        if self.row_dim is not None and self.row_dim not in self.dims:
            raise ValueError(f"row_dim {self.row_dim!r} is not one of the dims")
        for var, ds in self.var_dims.items():
            if not set(ds) <= set(self.dims):
                raise ValueError(f"the dims of {var!r} must be among the dims")
        return self

    def policy(self, dim: str) -> DimPolicy | None:
        return self.policies.get(dim)


def _bound_dims(defs: ScenarioRowsDef) -> dict[str, ScenarioInputDef]:
    return {i.bind.dim: i for i in defs.inputs if isinstance(i.bind, DimBind)}


def row_policy_issues(defs: ScenarioRowsDef, ctx: RowContext) -> list[Issue]:
    """Why scenario rows cannot be scored on this row context (checked at publish; pointers
    into the spec's ``/scenario_rows``): every dim needs a value from an input, a fixed value,
    the row label or a single level, and a ``fixed`` dim only takes its existing levels."""
    if not defs.enabled:
        return []
    issues: list[Issue] = []
    bound = _bound_dims(defs)
    for dim in ctx.dims:
        pol = ctx.policy(dim)
        if dim in bound:
            idef = bound[dim]
            levels = ctx.levels.get(dim)
            if pol == DimPolicy.FIXED and levels is not None:
                known = [str(x) for x in levels]
                picks = idef.choices if idef.type == "choice" else []
                bad = [c for c in picks if match_label(c, known) < 0]
                if idef.type != "choice" or bad:
                    issues.append(
                        Issue(
                            code=ErrorCode.BINDING_INVALID,
                            pointer=json_pointer(
                                "scenario_rows", "inputs", defs.inputs.index(idef), "choices"
                            ),
                            message=f"the dim {dim!r} only takes the fitted levels: offer them "
                            "as choices",
                        )
                    )
            continue
        if dim in defs.fixed or len(ctx.levels.get(dim, [])) == 1:
            continue
        if dim == ctx.row_dim and pol in (DimPolicy.NEW_LEVELS, DimPolicy.EXTEND):
            continue
        reason = (
            f"the rows are fixed on {dim!r}: add an input that picks one of its levels"
            if pol == DimPolicy.FIXED
            else f"nothing gives the {dim!r} of a row: add an input or a fixed value for it"
        )
        issues.append(
            Issue(
                code=ErrorCode.BINDING_INVALID,
                pointer="/scenario_rows/inputs",
                message=reason,
            )
        )
    return issues


def _next_label(ctx: RowContext, i: int) -> Coordinate:
    if i < len(ctx.next_labels):
        return ctx.next_labels[i]
    assert ctx.row_dim is not None
    ints = [x for x in ctx.levels.get(ctx.row_dim, []) if isinstance(x, int)]
    if not ints and not ctx.next_labels:
        raise ValueError(f"no labels after the fit end of {ctx.row_dim!r}")
    if not ints:
        raise ValueError(f"send at most {len(ctx.next_labels)} rows")
    return max(ints) + 1 + i


def row_coordinates(
    rows: Sequence[Mapping[str, object]], defs: ScenarioRowsDef, ctx: RowContext
) -> list[dict[str, Coordinate]]:
    """The coordinates of each row (dim -> label) in the scored dataset. Raises
    ``ValueError`` when a dim has no value, a ``fixed`` dim gets a level it does not have, or
    two rows select the same cell."""
    bound = _bound_dims(defs)
    out: list[dict[str, Coordinate]] = []
    seen: dict[tuple[Coordinate, ...], int] = {}
    for i, row in enumerate(rows):
        values = resolve_row(row, defs)
        coords: dict[str, Coordinate] = {}
        for dim in ctx.dims:
            label: object
            if dim in bound:
                label = values[bound[dim].name]
            elif dim in defs.fixed:
                label = defs.fixed[dim]
            elif dim == ctx.row_dim and ctx.policy(dim) == DimPolicy.NEW_LEVELS:
                label = f"{ctx.label_prefix}{i + 1}"
            elif dim == ctx.row_dim and ctx.policy(dim) == DimPolicy.EXTEND:
                label = _next_label(ctx, i)
            elif len(ctx.levels.get(dim, [])) == 1:
                label = ctx.levels[dim][0]
            else:
                raise ValueError(f"nothing gives the {dim!r} of row {i + 1}")
            if isinstance(label, bool) or not isinstance(label, str | int | float):
                raise ValueError(f"the {dim!r} of row {i + 1} must be a label")
            if isinstance(label, float):
                if not label.is_integer():
                    raise ValueError(f"the {dim!r} of row {i + 1} must be a label")
                label = int(label)
            levels = ctx.levels.get(dim)
            if ctx.policy(dim) == DimPolicy.FIXED and levels is not None:
                j = match_label(label, [str(x) for x in levels])
                if j < 0:
                    raise ValueError(f"{label!r} is not a level of {dim!r} (the dim is fixed)")
                label = levels[j]
            assert isinstance(label, str | int)
            coords[dim] = label
        key = tuple(coords[d] for d in ctx.dims)
        if key in seen:
            raise ValueError(f"rows {seen[key] + 1} and {i + 1} select the same case")
        seen[key] = i
        out.append(coords)
    return out


def rows_payload(
    rows: Sequence[Mapping[str, object]], defs: ScenarioRowsDef, catalog_row: RowContext
) -> XrJsonDataset:
    """The inline dataset of checked scenario rows: one variable per bound input variable and
    per ``fixed`` value that is not a dim, with one cell per row at its coordinates
    (:func:`row_coordinates`) and ``null`` in the other cells. Raises ``ValueError`` (see
    :func:`row_coordinates`; also when no variable is bound)."""
    ctx = catalog_row
    coords = row_coordinates(rows, defs, ctx)
    var_inputs = [i for i in defs.inputs if isinstance(i.bind, VarBind)]
    dims = set(ctx.dims)
    fixed_vars = [k for k in defs.fixed if k not in dims]
    variables: dict[str, list[str]] = {}
    for idef in var_inputs:
        assert isinstance(idef.bind, VarBind)
        variables[idef.bind.var] = list(ctx.var_dims.get(idef.bind.var, ctx.dims))
    for k in fixed_vars:
        variables.setdefault(k, list(ctx.var_dims.get(k, ctx.dims)))
    if not variables:
        raise ValueError("the scenario inputs set no variable of the model")
    long: list[dict[str, object]] = []
    for row, at in zip(rows, coords, strict=True):
        values = resolve_row(row, defs)
        cells: dict[str, object] = dict(at)
        for idef in var_inputs:
            assert isinstance(idef.bind, VarBind)
            cells[idef.bind.var] = values[idef.name]
        for k in fixed_vars:
            cells[k] = defs.fixed[k]
        long.append(cells)
    return xrjson_from_rows(long, variables)
