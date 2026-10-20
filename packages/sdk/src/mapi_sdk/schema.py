"""Array-level schema checks, schema inference and coordinate summaries.

* :func:`validate_dataset` is the sandbox-side ``DataSchema.validate(ds)``: it returns
  :class:`SchemaIssue` objects with plain-language messages ("3 markets are missing 2 weeks
  of revenue in March 2024") that the UI can show as-is.
* :func:`infer_schema` drafts a :class:`~mapi_spec.schema.DataSchema` from a dataset.
* :func:`summarize_coords` / :func:`data_summary` produce the metadata-only
  :class:`~mapi_spec.coords.CoordSummary` objects the control plane uses for CoordPlan checks.
"""

from __future__ import annotations

import calendar
from collections.abc import Collection, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.coords import CoordSummary, DataSummary, parse_freq
from mapi_spec.errors import ErrorCode, Issue, Severity, json_pointer
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Role, VarSpec
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from mapi_sdk._util import dtype_family, py_scalar
from mapi_sdk.errors import SchemaValidationError

__all__ = [
    "SchemaIssue",
    "check_dataset",
    "data_summary",
    "infer_freq",
    "infer_schema",
    "summarize_coords",
    "validate_dataset",
]


class SchemaIssue(BaseModel):
    """One array-level problem, described for a non-expert reader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode = ErrorCode.SCHEMA_INVALID
    severity: Severity = Severity.ERROR
    message: str = Field(..., max_length=2000)
    var: str | None = None
    dim: str | None = None
    count: int | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)

    def to_issue(self) -> Issue:
        """As a :class:`~mapi_spec.errors.Issue` pointing into the schema."""
        if self.var is not None:
            ptr = json_pointer("vars", self.var)
        elif self.dim is not None:
            ptr = json_pointer("dims", self.dim)
        else:
            ptr = ""
        return Issue(
            code=self.code,
            severity=self.severity,
            pointer=ptr,
            message=self.message,
            context=self.context,
        )


# ------------------------------------------------------------------------------- wording

_FREQ_WORDS = {
    "D": "day",
    "h": "hour",
    "min": "minute",
    "s": "second",
    "W": "week",
    "MS": "month",
    "ME": "month",
    "QS": "quarter",
    "QE": "quarter",
    "YS": "year",
    "YE": "year",
}


def _plural(word: str, n: int) -> str:
    if n == 1:
        return word
    if word.endswith(("s", "x", "ch", "sh")):
        return word + "es"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def _are(n: int) -> str:
    return "is" if n == 1 else "are"


def _period_word(freq: str | None) -> str:
    if freq is None:
        return "period"
    try:
        return _FREQ_WORDS.get(parse_freq(freq).unit, "period")
    except ValueError:
        return "period"


def _fmt_time(ts: pd.Timestamp) -> str:
    if ts.hour == ts.minute == ts.second == 0:
        return str(ts.strftime("%Y-%m-%d"))
    return str(ts.isoformat())


def _span(times: Sequence[Any]) -> str:
    if not times:
        return ""
    ts = sorted(pd.Timestamp(t) for t in times)
    lo, hi = ts[0], ts[-1]
    if lo.year == hi.year and lo.month == hi.month:
        return f"in {calendar.month_name[lo.month]} {lo.year}"
    if lo == hi:
        return f"on {_fmt_time(lo)}"
    return f"between {_fmt_time(lo)} and {_fmt_time(hi)}"


def _preview(values: Sequence[Any], n: int = 5) -> str:
    shown = ", ".join(str(py_scalar(v)) for v in values[:n])
    return shown + (f", ... (+{len(values) - n} more)" if len(values) > n else "")


# ---------------------------------------------------------------------------- frequency


def infer_freq(index: pd.Index | np.ndarray | Sequence[Any]) -> str | None:
    """Infer a supported pandas frequency alias (``None`` when unknown or unsupported)."""
    idx = pd.DatetimeIndex(index)
    if len(idx) < 3:
        return None
    try:
        freq = pd.infer_freq(idx)
    except (TypeError, ValueError):
        return None
    if freq is None:
        return None
    text = str(freq)
    for candidate in (text, text.replace("-DEC", "").replace("-JAN", "")):
        try:
            parse_freq(candidate)
            return candidate
        except ValueError:
            continue
    return None


def _time_index(values: np.ndarray) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(values.astype("datetime64[ns]"))


def _grid(start: pd.Timestamp, end: pd.Timestamp, freq: str) -> pd.DatetimeIndex:
    return pd.date_range(start, end, freq=freq)


# --------------------------------------------------------------------------- validation


def _dtype_ok(schema_dtype: str, data_dtype: Any) -> tuple[bool, bool]:
    """(compatible, needs_conversion) for a schema dtype vs an array dtype."""
    want = dtype_family(schema_dtype if schema_dtype != "str" else np.dtype("U1"))
    have = dtype_family(data_dtype)
    if want == have:
        return True, False
    if want == "float" and have in ("int", "bool"):
        return True, True
    return False, False


def _missing_message(
    name: str, mask: xr.DataArray, schema: DataSchema, time_dims: Collection[str]
) -> tuple[str, int, dict[str, JsonValue]]:
    """Plain-language description of where ``name`` has missing values."""
    total = int(mask.sum())
    tdim = next((str(d) for d in mask.dims if d in time_dims), None)
    groups = [str(d) for d in mask.dims if d != tdim]
    ctx: dict[str, JsonValue] = {"missing_cells": total}
    if tdim is None or not groups:
        if tdim is not None:
            times = mask[tdim].values[mask.values]
            freq = schema.dims[tdim].freq if tdim in schema.dims else None
            word = _period_word(freq)
            return (
                f"{name} is missing for {total} {_plural(word, total)} {_span(list(times))}",
                total,
                ctx,
            )
        if groups and len(groups) == 1:
            levels = mask[groups[0]].values[mask.values] if groups[0] in mask.coords else []
            msg = f"{total} {_plural('value', total)} of {name} {_are(total)} missing"
            if len(levels):
                msg += f" ({groups[0]}: {_preview(list(levels))})"
            return msg, total, ctx
        return f"{total} {_plural('value', total)} of {name} {_are(total)} missing", total, ctx
    per_group = mask.any(tdim)
    n_groups = int(per_group.sum())
    per_time = mask.any(groups)
    times = mask[tdim].values[per_time.values] if tdim in mask.coords else []
    counts = mask.sum(tdim).values[per_group.values]
    freq = schema.dims[tdim].freq if tdim in schema.dims else None
    word = _period_word(freq)
    group_word = groups[0] if len(groups) == 1 else " × ".join(groups) + " combination"
    k = int(counts.max()) if counts.size else 0
    up_to = "" if counts.size and counts.min() == counts.max() else "up to "
    verb = _are(n_groups)
    msg = (
        f"{n_groups} {_plural(group_word, n_groups)} {verb} missing {up_to}{k} "
        f"{_plural(word, k)} of {name} {_span(list(times))}"
    ).rstrip()
    if len(groups) == 1 and groups[0] in mask.coords:
        ctx["groups"] = [py_scalar(v) for v in mask[groups[0]].values[per_group.values][:50]]
    return msg, total, ctx


def _check_dim_coords(name: str, spec: DimSpec, ds: xr.Dataset, issues: list[SchemaIssue]) -> None:
    if name not in ds.coords:
        if spec.kind not in ("index",):
            issues.append(
                SchemaIssue(
                    severity=Severity.WARNING,
                    dim=name,
                    message=f"dim {name!r} has no coordinate labels; positions will be used",
                )
            )
        return
    values = np.asarray(ds[name].values)
    if values.ndim != 1:
        return
    labels = pd.Index(values)
    if labels.has_duplicates:
        dups = labels[labels.duplicated()].unique()
        issues.append(
            SchemaIssue(
                dim=name,
                count=len(dups),
                message=f"the {name!r} coordinate has {len(dups)} duplicated "
                f"{_plural('label', len(dups))} ({_preview(list(dups))})",
            )
        )
    if spec.kind == "time":
        if values.dtype.kind != "M":
            issues.append(
                SchemaIssue(
                    dim=name,
                    message=f"dim {name!r} is a time dim but its labels are not dates "
                    f"(found {values.dtype})",
                )
            )
            return
        idx = _time_index(values)
        if idx.hasnans:
            issues.append(SchemaIssue(dim=name, message=f"dim {name!r} has missing dates"))
            return
        if len(idx) > 1 and not idx.is_monotonic_increasing:
            issues.append(
                SchemaIssue(dim=name, message=f"the dates of {name!r} are not in increasing order")
            )
            return
        if spec.freq is not None and len(idx):
            grid = _grid(idx[0], idx[-1], spec.freq)
            off = idx.difference(grid)
            word = _period_word(spec.freq)
            if len(off):
                issues.append(
                    SchemaIssue(
                        dim=name,
                        count=len(off),
                        message=f"{len(off)} {_plural('date', len(off))} of {name!r} do not fall "
                        f"on the {spec.freq} grid ({_preview([_fmt_time(t) for t in off])})",
                    )
                )
            gaps = grid.difference(idx)
            if len(gaps):
                issues.append(
                    SchemaIssue(
                        severity=Severity.WARNING,
                        code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
                        dim=name,
                        count=len(gaps),
                        message=f"{len(gaps)} {_plural(word, len(gaps))} {_are(len(gaps))} "
                        f"missing from "
                        f"{name!r} {_span(list(gaps))}",
                    )
                )
    elif spec.kind == "integer" and values.dtype.kind not in "iu":
        issues.append(SchemaIssue(dim=name, message=f"dim {name!r} should have integer labels"))
    elif spec.ordered or spec.kind == "integer":
        if len(labels) > 1 and not labels.is_monotonic_increasing:
            issues.append(
                SchemaIssue(
                    severity=Severity.WARNING,
                    dim=name,
                    message=f"the labels of ordered dim {name!r} are not in increasing order",
                )
            )


def validate_dataset(
    schema: DataSchema,
    ds: xr.Dataset,
    *,
    optional_roles: Collection[Role] = (),
    check_values: bool = True,
    check_coords: bool = True,
) -> list[SchemaIssue]:
    """Check ``ds`` against ``schema`` and describe every problem in plain language.

    ``optional_roles`` lists roles whose variables may be absent (e.g. the target when
    scoring). ``check_values`` controls missingness, sentinel and bounds checks.
    """
    issues: list[SchemaIssue] = []
    time_dims = {n for n, d in schema.dims.items() if d.kind == "time"}

    for name, spec in schema.dims.items():
        if name not in ds.dims:
            if name in ds.coords and ds[name].ndim == 0:
                continue
            used = [v for v, vs in schema.vars.items() if name in vs.dims and v in ds]
            if used or not schema.vars:
                issues.append(SchemaIssue(dim=name, message=f"the data has no dim named {name!r}"))
            continue
        if check_coords:
            _check_dim_coords(name, spec, ds, issues)
        if ds.sizes[name] == 0:
            issues.append(
                SchemaIssue(
                    severity=Severity.WARNING, dim=name, message=f"dim {name!r} has no entries"
                )
            )
    for d in ds.dims:
        if d not in schema.dims and not str(d).endswith("__rowmask"):
            issues.append(
                SchemaIssue(
                    severity=Severity.INFO,
                    dim=str(d),
                    message=f"dim {str(d)!r} is not in the schema and will be ignored",
                )
            )

    for vname, vspec in schema.vars.items():
        if vname not in ds.variables:
            if vspec.role in optional_roles:
                continue
            issues.append(
                SchemaIssue(var=vname, message=f"the data has no variable named {vname!r}")
            )
            continue
        da = ds[vname]
        have = tuple(str(d) for d in da.dims)
        if set(have) != set(vspec.dims):
            issues.append(
                SchemaIssue(
                    var=vname,
                    message=f"{vname!r} should have dims {list(vspec.dims)} but has {list(have)}",
                )
            )
            continue
        if have != tuple(vspec.dims):
            issues.append(
                SchemaIssue(
                    severity=Severity.INFO,
                    var=vname,
                    message=f"{vname!r} has its dims in a different order; it will be transposed",
                )
            )
        ok, convert = _dtype_ok(vspec.dtype, da.dtype)
        if not ok:
            issues.append(
                SchemaIssue(
                    var=vname,
                    message=f"{vname!r} should hold {vspec.dtype} values but holds {da.dtype}",
                )
            )
            continue
        if convert:
            issues.append(
                SchemaIssue(
                    severity=Severity.INFO,
                    var=vname,
                    message=f"{vname!r} holds {da.dtype} values; they will be read as "
                    f"{vspec.dtype}",
                )
            )
        if vspec.index_of is not None and vspec.index_of in ds.sizes:
            _check_index_var(vname, vspec, da, int(ds.sizes[vspec.index_of]), issues)
        if check_values:
            _check_values(vname, vspec, da, schema, time_dims, issues)
    return issues


def _check_index_var(
    name: str, spec: VarSpec, da: xr.DataArray, size: int, issues: list[SchemaIssue]
) -> None:
    values = np.asarray(da.values)
    if values.dtype.kind not in "iu":
        return
    bad = (values < 0) | (values >= size)
    if bad.any():
        issues.append(
            SchemaIssue(
                var=name,
                count=int(bad.sum()),
                message=f"{int(bad.sum())} entries of {name!r} point outside "
                f"{spec.index_of!r} (valid positions are 0 to {size - 1})",
            )
        )


def _check_values(
    name: str,
    spec: VarSpec,
    da: xr.DataArray,
    schema: DataSchema,
    time_dims: Collection[str],
    issues: list[SchemaIssue],
) -> None:
    fam = dtype_family(da.dtype)
    if fam in ("float", "datetime"):
        mask = da.isnull()
        if spec.missing == "forbid" and bool(mask.any()):
            msg, total, ctx = _missing_message(name, mask, schema, time_dims)
            issues.append(SchemaIssue(var=name, count=total, message=msg, context=ctx))
    if fam not in ("float", "int"):
        return
    if fam == "int" and spec.missing_sentinel is None and spec.bounds is None:
        return  # nothing to check: never copy a large integer array (image pixels) to floats
    values = np.asarray(da.values, dtype=float)
    if spec.missing_sentinel is not None:
        hits = int(np.sum(values == spec.missing_sentinel))
        if hits:
            issues.append(
                SchemaIssue(
                    severity=Severity.WARNING,
                    var=name,
                    count=hits,
                    message=f"{hits} {_plural('value', hits)} of {name!r} "
                    f"{'equals' if hits == 1 else 'equal'} the missing-data code "
                    f"{spec.missing_sentinel:g}; replace them before modelling",
                )
            )
            values = np.where(values == spec.missing_sentinel, np.nan, values)
    if spec.bounds is not None:
        lo, hi = spec.bounds
        with np.errstate(invalid="ignore"):
            if lo is not None:
                n = int(np.sum(values < lo))
                if n:
                    issues.append(
                        SchemaIssue(
                            var=name,
                            count=n,
                            message=f"{n} {_plural('value', n)} of {name!r} {_are(n)} below {lo:g}",
                        )
                    )
            if hi is not None:
                n = int(np.sum(values > hi))
                if n:
                    issues.append(
                        SchemaIssue(
                            var=name,
                            count=n,
                            message=f"{n} {_plural('value', n)} of {name!r} {_are(n)} above {hi:g}",
                        )
                    )
    if fam == "float" and spec.missing != "allow" and np.isinf(values).any():
        n = int(np.isinf(values).sum())
        issues.append(
            SchemaIssue(
                var=name,
                count=n,
                message=f"{n} {_plural('value', n)} of {name!r} {_are(n)} infinite",
            )
        )


def check_dataset(schema: DataSchema, ds: xr.Dataset, **kwargs: Any) -> list[SchemaIssue]:
    """:func:`validate_dataset`, raising :class:`SchemaValidationError` on any error."""
    issues = validate_dataset(schema, ds, **kwargs)
    errors = [i for i in issues if i.severity is Severity.ERROR]
    if errors:
        first = errors[0]
        more = f" (and {len(errors) - 1} more problem(s))" if len(errors) > 1 else ""
        raise SchemaValidationError(f"{first.message}{more}", issues=[i.to_issue() for i in issues])
    return issues


# ---------------------------------------------------------------------------- inference

_DTYPE_MAP = {
    "float": "float64",
    "int": "int64",
    "bool": "bool",
    "str": "str",
    "datetime": "datetime64[ns]",
}


def _var_dtype(da: xr.DataArray) -> str:
    if da.dtype == np.float32:
        return "float32"
    if da.dtype == np.int32:
        return "int32"
    if da.dtype == np.uint8:
        return "uint8"  # v0.14: image pixels; other unsigned ints infer as int64
    fam = dtype_family(da.dtype)
    if fam not in _DTYPE_MAP:
        raise ValueError(f"variable {da.name!r} has unsupported dtype {da.dtype}")
    return _DTYPE_MAP[fam]


def infer_schema(
    ds: xr.Dataset,
    *,
    targets: Collection[str] = (),
    roles: Mapping[str, Role | str] | None = None,
    policies: Mapping[str, DimPolicy | str] | None = None,
    units: Mapping[str, str] | None = None,
) -> DataSchema:
    """Draft a :class:`DataSchema` from ``ds``.

    Time dims get policy ``extend`` and an inferred frequency; other dims are ``fixed``
    unless ``policies`` says otherwise. Variables are covariates unless listed in
    ``targets`` or ``roles``. Missingness is ``allow`` when the data has missing values.
    """
    roles = dict(roles or {})
    policies = dict(policies or {})
    units = dict(units or {})
    dims: dict[str, DimSpec] = {}
    for d in ds.dims:
        name = str(d)
        kind: str
        freq: str | None = None
        if name in ds.coords:
            values = np.asarray(ds[name].values)
            fam = dtype_family(values.dtype)
            if fam == "datetime":
                kind = "time"
                freq = infer_freq(values)
            elif fam == "int":
                kind = "integer"
            elif fam == "float":
                kind = "float"
            else:
                kind = "categorical"
        else:
            kind = "index"
        default = DimPolicy.EXTEND if kind == "time" else DimPolicy.FIXED
        policy = DimPolicy(policies.get(name, default))
        dims[name] = DimSpec(kind=kind, policy=policy, freq=freq)
    vars_: dict[str, VarSpec] = {}
    for v in ds.data_vars:
        name = str(v)
        da = ds[v]
        dtype = _var_dtype(da)
        has_missing = dtype_family(da.dtype) in ("float", "datetime") and bool(da.isnull().any())
        role = Role(roles.get(name, Role.TARGET if name in targets else Role.COVARIATE))
        vars_[name] = VarSpec(
            dims=tuple(str(d) for d in da.dims),
            dtype=dtype,
            role=role,
            units=units.get(name, da.attrs.get("units")),
            missing="allow" if has_missing else "forbid",
        )
    return DataSchema(dims=dims, vars=vars_)


# ------------------------------------------------------------------------ coord summaries


def _summary(name: str, values: np.ndarray, spec: DimSpec | None) -> CoordSummary:
    n = int(values.size)
    fam = dtype_family(values.dtype)
    if fam == "datetime":
        idx = _time_index(values)
        freq = spec.freq if spec is not None and spec.freq else infer_freq(idx)
        contiguous = True
        if freq is not None and n:
            contiguous = len(_grid(idx.min(), idx.max(), freq)) == n
        return CoordSummary(
            kind="time",
            size=n,
            start=idx.min().isoformat() if n else None,
            end=idx.max().isoformat() if n else None,
            freq=freq,
            contiguous=contiguous,
        )
    levels = [py_scalar(v) for v in values.tolist()] if n <= CoordSummary.MAX_LEVELS else None
    if fam == "int":
        start, end = (int(values.min()), int(values.max())) if n else (None, None)
        step = 1
        if n > 1:
            diffs = np.diff(np.sort(values))
            step = int(diffs.min()) if diffs.min() > 0 else 1
        contiguous = n == 0 or (int(values.max()) - int(values.min())) // step + 1 == n
        kind = "index" if spec is not None and spec.kind == "index" else "integer"
        return CoordSummary(
            kind=kind,
            size=n,
            values=levels,
            start=start,
            end=end,
            step=step,
            contiguous=contiguous,
        )
    if fam == "float":
        return CoordSummary(kind="float", size=n, values=levels)
    return CoordSummary(kind="categorical", size=n, values=levels)


def summarize_coords(ds: xr.Dataset, schema: DataSchema | None = None) -> dict[str, CoordSummary]:
    """Metadata summary of every dim of ``ds`` (levels, ranges, frequency, contiguity)."""
    out: dict[str, CoordSummary] = {}
    for d in ds.dims:
        name = str(d)
        spec = schema.dims.get(name) if schema is not None else None
        if name in ds.coords and ds[name].ndim == 1:
            out[name] = _summary(name, np.asarray(ds[name].values), spec)
        else:
            size = int(ds.sizes[d])
            out[name] = CoordSummary(
                kind="index", size=size, start=0 if size else None, end=size - 1 if size else None
            )
    return out


def data_summary(ds: xr.Dataset, schema: DataSchema | None = None) -> DataSummary:
    """:class:`~mapi_spec.coords.DataSummary` of ``ds`` (variables plus coord summaries).

    With a ``schema``, ``observed`` says where each target - and each driver a forecast needs
    (``future_required``) - holds values along every ordered dim (time, ``extend`` or
    ``ordered``): the actuals of a forecast payload and the drivers on its rows.
    """
    coords = summarize_coords(ds, schema)
    observed: dict[str, dict[str, CoordSummary]] = {}
    names: list[str] = []
    if schema is not None:
        names = list(schema.targets) + [
            n
            for n, v in schema.vars.items()
            if v.future_required and v.role is not Role.TARGET and n not in schema.targets
        ]
    for name in names:
        if name not in ds.data_vars:
            continue
        da = ds[name]
        for d in da.dims:
            dim = str(d)
            spec = schema.dims.get(dim) if schema is not None else None
            ordered = spec is not None and (
                spec.kind == "time" or spec.ordered or spec.policy is DimPolicy.EXTEND
            )
            if not ordered or dim not in ds.coords or ds[dim].ndim != 1:
                continue
            others = [o for o in da.dims if o != d]
            has = da.notnull().any(others) if others else da.notnull()
            labels = np.asarray(ds[dim].values)[np.asarray(has.values, dtype=bool)]
            assert spec is not None
            if spec.freq is None and coords[dim].freq is not None:
                spec = spec.model_copy(update={"freq": coords[dim].freq})
            observed.setdefault(name, {})[dim] = _summary(dim, labels, spec)
    return DataSummary(vars=[str(v) for v in ds.data_vars], coords=coords, observed=observed)
