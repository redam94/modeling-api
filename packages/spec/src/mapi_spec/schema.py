"""Data contract primitives: dims, variables, ragged structure and dataset schemas.

A :class:`DataSchema` describes an ``xr.Dataset`` without any arrays: its dims (each with a
:class:`DimPolicy` saying what may happen to it at score time), its variables (with a
:class:`Role`, dtype, units and missingness) and its ragged structure.

A :class:`DataSchemaTemplate` is a *static JSON function of enumerated settings*: plugins
export one per model so the control plane can compute the required input schema for given
settings without running plugin code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, JsonValue, ValidationError, field_validator, model_validator

from mapi_spec._types import Name, StrictModel, unique
from mapi_spec.coords import parse_freq
from mapi_spec.template import TemplateError, deep_merge, placeholders, render

__all__ = [
    "DataSchema",
    "DataSchemaTemplate",
    "DimKind",
    "DimPolicy",
    "DimSpec",
    "Dtype",
    "RaggedSpec",
    "Role",
    "TemplateCase",
    "TemplateParam",
    "VarSpec",
    "tighten_policy",
]


class DimPolicy(StrEnum):
    """What may happen to a dim's coordinates at score time."""

    FIXED = "fixed"
    """Score coords must be a subset of fit coords (channel, state, product)."""
    EXTEND = "extend"
    """New coords may be appended after the fit range, contiguously (time, obs)."""
    NEW_LEVELS = "new_levels"
    """Unseen levels are allowed through a declared strategy (market, county)."""


def policy_is_at_least_as_tight(candidate: DimPolicy, reference: DimPolicy) -> bool:
    """True if ``candidate`` allows nothing that ``reference`` forbids.

    ``fixed`` is the tightest; ``extend`` and ``new_levels`` are incomparable.
    """
    return candidate == reference or candidate is DimPolicy.FIXED


def tighten_policy(a: DimPolicy, b: DimPolicy) -> DimPolicy:
    """The loosest policy that is at least as tight as both ``a`` and ``b`` (lattice meet)."""
    return a if a == b else DimPolicy.FIXED


DimKind = Literal["categorical", "time", "integer", "float", "index"]
Dtype = Literal["float64", "float32", "int64", "int32", "bool", "str", "datetime64[ns]"]
NUMERIC_DTYPES: frozenset[str] = frozenset({"float64", "float32", "int64", "int32"})


class DimSpec(StrictModel):
    """A dimension and its score-time policy."""

    kind: DimKind
    policy: DimPolicy = DimPolicy.FIXED
    ordered: bool = False
    freq: str | None = Field(None, description="Pandas-style frequency, e.g. 'W-SUN'")
    max_extension: int | None = Field(None, ge=0, description="Periods allowed past the fit")
    new_level_strategy: Literal["error", "hyperprior", "pooled", "zero"] = "error"
    description: str | None = Field(None, max_length=2000)

    @field_validator("freq")
    @classmethod
    def _freq(cls, v: str | None) -> str | None:
        if v is not None:
            parse_freq(v)
        return v

    @model_validator(mode="after")
    def _consistency(self) -> DimSpec:
        if self.freq is not None and self.kind != "time":
            raise ValueError("freq is only valid on time dims")
        if self.max_extension is not None and self.policy is not DimPolicy.EXTEND:
            raise ValueError("max_extension requires policy 'extend'")
        if self.new_level_strategy != "error" and self.policy is not DimPolicy.NEW_LEVELS:
            raise ValueError("new_level_strategy requires policy 'new_levels'")
        return self


class Role(StrEnum):
    """What a variable means to a model."""

    TARGET = "target"
    COVARIATE = "covariate"
    MEDIA = "media"
    SPEND = "spend"
    CONTROL = "control"
    INDEX = "index"
    MASK = "mask"
    CONSTANT = "constant"
    WEIGHT = "weight"
    EXPOSURE = "exposure"


class VarSpec(StrictModel):
    """A data variable: its dims (by name), dtype, role, units and missingness."""

    dims: tuple[Name, ...]
    dtype: Dtype
    role: Role = Role.COVARIATE
    units: str | None = Field(None, max_length=64, description="pint-style, e.g. 'EUR'")
    description: str | None = Field(None, max_length=2000)
    missing: Literal["forbid", "allow", "mask"] = "forbid"
    missing_sentinel: float | None = None
    index_of: Name | None = Field(None, description="Long layout: this var indexes that dim")
    future_required: bool = Field(False, description="Must be supplied over a forecast horizon")
    bounds: tuple[float | None, float | None] | None = None

    @model_validator(mode="after")
    def _consistency(self) -> VarSpec:
        if dups := unique(self.dims):
            raise ValueError(f"duplicate dims in variable: {dups}")
        if self.bounds is not None:
            lo, hi = self.bounds
            if lo is not None and hi is not None and lo > hi:
                raise ValueError("bounds must satisfy lower <= upper")
        if self.index_of is not None and self.dtype not in ("int64", "int32"):
            raise ValueError("index_of requires an integer dtype")
        return self


class RaggedSpec(StrictModel):
    """Ragged nesting (``hh → trip``): long form (obs + index vars) or padded + mask."""

    group_dim: Name
    within_dim: Name
    form: Literal["long", "padded"]
    mask_var: Name | None = None

    @model_validator(mode="after")
    def _consistency(self) -> RaggedSpec:
        if self.group_dim == self.within_dim:
            raise ValueError("group_dim and within_dim must differ")
        if self.form == "padded" and self.mask_var is None:
            raise ValueError("padded ragged data needs a mask_var")
        return self


class DataSchema(StrictModel):
    """Schema of an ``xr.Dataset``: dims, variables, layout and ragged structure."""

    layout: Literal["dense", "long"] = "dense"
    dims: dict[Name, DimSpec] = Field(default_factory=dict, max_length=256)
    vars: dict[Name, VarSpec] = Field(default_factory=dict, max_length=4096)
    ragged: list[RaggedSpec] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _referential_integrity(self) -> DataSchema:
        problems: list[str] = []
        clash = sorted(set(self.dims) & set(self.vars))
        if clash:
            problems.append(f"names used both as dim and variable: {clash}")
        for vname, v in self.vars.items():
            missing = [d for d in v.dims if d not in self.dims]
            if missing:
                problems.append(f"variable {vname!r} uses undeclared dims {missing}")
            if v.index_of is not None and v.index_of not in self.dims:
                problems.append(f"variable {vname!r} indexes undeclared dim {v.index_of!r}")
        for r in self.ragged:
            for d in (r.group_dim, r.within_dim):
                if d not in self.dims:
                    problems.append(f"ragged spec uses undeclared dim {d!r}")
            if r.mask_var is not None:
                mv = self.vars.get(r.mask_var)
                if mv is None:
                    problems.append(f"ragged mask_var {r.mask_var!r} is not a variable")
                elif mv.dtype != "bool":
                    problems.append(f"ragged mask_var {r.mask_var!r} must have dtype bool")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ helpers
    def vars_with_role(self, *roles: Role) -> list[str]:
        """Variable names whose role is one of ``roles`` (declaration order)."""
        return [n for n, v in self.vars.items() if v.role in roles]

    @property
    def targets(self) -> list[str]:
        return self.vars_with_role(Role.TARGET)

    def dims_in_use(self) -> set[str]:
        used = {d for v in self.vars.values() for d in v.dims}
        used |= {v.index_of for v in self.vars.values() if v.index_of}
        return used

    def has_name(self, name: str) -> bool:
        return name in self.dims or name in self.vars

    def renamed(self, mapping: Mapping[str, str]) -> DataSchema:
        """Rename dims and/or variables (``old -> new``), updating every reference."""

        def rn(n: str) -> str:
            return mapping.get(n, n)

        data = self.model_dump(mode="python")
        data["dims"] = {rn(k): v for k, v in data["dims"].items()}
        new_vars: dict[str, Any] = {}
        for k, v in data["vars"].items():
            v["dims"] = tuple(rn(d) for d in v["dims"])
            if v.get("index_of"):
                v["index_of"] = rn(v["index_of"])
            new_vars[rn(k)] = v
        data["vars"] = new_vars
        for r in data["ragged"]:
            r["group_dim"], r["within_dim"] = rn(r["group_dim"]), rn(r["within_dim"])
            if r.get("mask_var"):
                r["mask_var"] = rn(r["mask_var"])
        return DataSchema.model_validate(data)

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_defaults=True), indent=indent)

    @classmethod
    def from_json(cls, text: str | bytes) -> DataSchema:
        return cls.model_validate_json(text)


# ------------------------------------------------------------------------ schema templates

ParamType = Literal["string", "integer", "number", "boolean", "string_list", "integer_list"]


class TemplateParam(StrictModel):
    """A settings value that a :class:`DataSchemaTemplate` depends on."""

    type: ParamType
    path: str | None = Field(
        None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$",
        description="Dotted path into the settings (defaults to the parameter name)",
    )
    enum: list[JsonValue] | None = Field(None, max_length=256)
    default: JsonValue = None
    required: bool = False

    def check(self, name: str, value: JsonValue) -> JsonValue:
        ok: bool
        match self.type:
            case "string":
                ok = isinstance(value, str)
            case "integer":
                ok = isinstance(value, int) and not isinstance(value, bool)
            case "number":
                ok = isinstance(value, int | float) and not isinstance(value, bool)
            case "boolean":
                ok = isinstance(value, bool)
            case "string_list":
                ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            case "integer_list":
                ok = isinstance(value, list) and all(
                    isinstance(v, int) and not isinstance(v, bool) for v in value
                )
        if not ok:
            raise TemplateError(f"template parameter {name!r} must be of type {self.type}")
        if self.enum is not None and value not in self.enum:
            raise TemplateError(f"template parameter {name!r} is not one of the allowed values")
        return value


class TemplateCase(StrictModel):
    """Conditional fragment merged into the template when all ``when`` params match."""

    when: dict[str, JsonValue] = Field(..., min_length=1)
    merge: dict[str, JsonValue]


def _get_path(settings: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    cur: Any = settings
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


class DataSchemaTemplate(StrictModel):
    """A DataSchema-shaped JSON template plus the settings parameters it depends on.

    Resolution is pure and static: ``resolve(settings)`` extracts each declared parameter
    from the settings (falling back to its default), checks its type/enum, renders the
    :mod:`~mapi_spec.template` placeholders, deep-merges every matching ``case`` and
    validates the result as a :class:`DataSchema`.
    """

    params: dict[str, TemplateParam] = Field(default_factory=dict, max_length=64)
    template: dict[str, JsonValue]
    cases: list[TemplateCase] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _placeholders_declared(self) -> DataSchemaTemplate:
        used = placeholders(self.template)
        for case in self.cases:
            used |= placeholders(case.merge)
            unknown_when = set(case.when) - set(self.params)
            if unknown_when:
                raise ValueError(f"case conditions use undeclared params {sorted(unknown_when)}")
        unknown = used - set(self.params)
        if unknown:
            raise ValueError(f"template uses undeclared params {sorted(unknown)}")
        return self

    @classmethod
    def from_schema(cls, schema: DataSchema) -> DataSchemaTemplate:
        """A constant template (no parameters)."""
        return cls(template=schema.model_dump(mode="json", exclude_defaults=True))

    def param_values(self, settings: Mapping[str, Any]) -> dict[str, JsonValue]:
        """Extract and type-check every declared parameter from ``settings``."""
        values: dict[str, JsonValue] = {}
        for name, p in self.params.items():
            found, value = _get_path(settings, p.path or name)
            if not found or value is None:
                if p.required:
                    raise TemplateError(f"required template parameter {name!r} is missing")
                value = p.default
            if value is not None:
                p.check(name, value)
            values[name] = value
        return values

    def render(self, settings: Mapping[str, Any]) -> dict[str, JsonValue]:
        """Render to plain JSON (not yet validated as a DataSchema)."""
        values = self.param_values(settings)
        out = render(self.template, values)
        for case in self.cases:
            if all(values.get(k) == v for k, v in case.when.items()):
                out = deep_merge(out, render(case.merge, values))
        if not isinstance(out, dict):  # pragma: no cover - template is a dict
            raise TemplateError("template must render to a mapping")
        return out

    def resolve(self, settings: Mapping[str, Any] | None = None) -> DataSchema:
        """Resolve to a :class:`DataSchema` (raises TemplateError or ValidationError)."""
        return DataSchema.model_validate(self.render(settings or {}))

    def try_resolve(
        self, settings: Mapping[str, Any] | None = None
    ) -> tuple[DataSchema | None, str | None]:
        """Like :meth:`resolve` but returns ``(None, message)`` instead of raising."""
        try:
            return self.resolve(settings), None
        except TemplateError as exc:
            return None, str(exc)
        except ValidationError as exc:
            return None, _summarise_validation_error(exc)


def _summarise_validation_error(exc: ValidationError, limit: int = 5) -> str:
    parts: list[str] = []
    for err in exc.errors(include_url=False, include_input=False)[:limit]:
        loc = "/".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else str(err["msg"]))
    more = exc.error_count() - limit
    if more > 0:
        parts.append(f"... and {more} more")
    return "; ".join(parts)


def names_of(values: Iterable[str]) -> list[str]:
    """Stable de-duplicated list (helper for messages)."""
    return list(dict.fromkeys(values))
