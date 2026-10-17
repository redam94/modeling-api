"""Step kinds, SchemaEffects, context needs and static schema propagation.

A :class:`SchemaEffect` is a data-free description of how a preprocessing step changes a
:class:`~mapi_spec.schema.DataSchema`. Step authors export it as JSON (per image digest);
the control plane evaluates it with :func:`propagate` and never imports step code.

Effects may be *templates*: strings can reference the step's settings with the
:mod:`~mapi_spec.template` placeholder syntax (``"{dim}"``, ``"{*vars as v}"``). Every
referenced setting must be listed in ``template_params``. :func:`resolve_effect` renders a
template with concrete settings into a plain :class:`SchemaEffect`.

Within one effect, operations apply in this order:
``renames → drops → transforms_dims → stacks → unstacks → updates → derives → adds``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, JsonValue, ValidationError, model_validator

from mapi_spec._types import RESERVED_STAGE, Name, StrictModel, unique
from mapi_spec.errors import ErrorCode, Issue, Severity, json_pointer
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Dtype, Role, VarSpec
from mapi_spec.template import TemplateError, placeholders, render

__all__ = [
    "ContextNeed",
    "PropagationResult",
    "SchemaEffect",
    "Stage",
    "StepKind",
    "VarDerive",
    "VarPatch",
    "apply_effect",
    "check_effect_template",
    "propagate",
    "resolve_context",
    "resolve_effect",
]


class StepKind(StrEnum):
    """RESHAPE changes structure (not invertible past it); VALUE rescales; FEATURE adds."""

    RESHAPE = "reshape"
    VALUE = "value"
    FEATURE = "feature"


class ContextNeed(StrictModel):
    """Per-group tail a step needs at score time: ``n`` rows along ``along`` within groups.

    ``min_lag`` is the smallest lag the step applies (for recursive-forecast checks);
    it defaults to 1 when the step depends on the target.
    """

    along: Name
    n: int = Field(..., ge=0, le=100_000)
    within: list[Name] = Field(default_factory=list, max_length=16)
    min_lag: int | None = Field(None, ge=0)


class VarPatch(StrictModel):
    """Partial update of a variable. ``None`` fields are left unchanged."""

    dtype: Dtype | None = None
    role: Role | None = None
    units: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=2000)
    missing: Literal["forbid", "allow", "mask"] | None = None
    future_required: bool | None = None
    append_dims: list[Name] = Field(default_factory=list, max_length=16)
    remove_dims: list[Name] = Field(default_factory=list, max_length=16)

    def apply(self, var: dict[str, Any]) -> list[str]:
        """Patch a dumped VarSpec dict in place; return problems."""
        problems: list[str] = []
        for fld in ("dtype", "role", "units", "description", "missing", "future_required"):
            val = getattr(self, fld)
            if val is not None:
                var[fld] = val
        dims = list(var["dims"])
        for d in self.remove_dims:
            if d not in dims:
                problems.append(f"cannot remove dim {d!r}: variable does not use it")
            else:
                dims.remove(d)
        for d in self.append_dims:
            if d in dims:
                problems.append(f"cannot append dim {d!r}: variable already uses it")
            else:
                dims.append(d)
        var["dims"] = tuple(dims)
        return problems


class VarDerive(VarPatch):
    """A new variable copied from an existing one (``from``) and then patched."""

    from_: Name = Field(..., alias="from")


EFFECT_ORDER = (
    "renames",
    "drops",
    "transforms_dims",
    "stacks",
    "unstacks",
    "updates",
    "derives",
    "adds",
)


class SchemaEffect(StrictModel):
    """How a step changes the schema. See the module docstring for ordering rules."""

    adds: dict[Name, VarSpec] = Field(default_factory=dict)
    renames: dict[Name, Name] = Field(default_factory=dict)
    drops: list[Name] = Field(default_factory=list)
    transforms_dims: dict[Name, DimSpec] = Field(
        default_factory=dict, description="Set (add or replace) a dim spec, e.g. resample freq"
    )
    stacks: dict[Name, list[Name]] = Field(
        default_factory=dict, description="new dim -> component dims stacked into it"
    )
    unstacks: dict[Name, list[Name]] = Field(
        default_factory=dict,
        description="stacked dim -> component dims restored from it (inverse of stacks); "
        "component specs come from transforms_dims when declared there, else "
        "categorical/fixed",
    )
    updates: dict[Name, VarPatch] = Field(default_factory=dict)
    derives: dict[Name, VarDerive] = Field(default_factory=dict)
    template_params: list[str] = Field(default_factory=list)
    dynamic: bool = False

    @model_validator(mode="after")
    def _check(self) -> SchemaEffect:
        if dups := unique(self.drops):
            raise ValueError(f"duplicate drops: {dups}")
        targets = list(self.renames.values())
        if dups := unique(targets):
            raise ValueError(f"several names renamed to the same target: {dups}")
        for label, mapping in (("stack", self.stacks), ("unstack", self.unstacks)):
            for new, comps in mapping.items():
                if len(comps) < 2:
                    raise ValueError(f"{label} {new!r} needs at least two component dims")
                if dups := unique(comps):
                    raise ValueError(f"{label} {new!r} repeats dims {dups}")
        return self

    @property
    def is_identity(self) -> bool:
        return not any(getattr(self, f) for f in EFFECT_ORDER)


_EFFECT_KEYS = frozenset(SchemaEffect.model_fields) | {"from"}


def check_effect_template(raw: Mapping[str, JsonValue]) -> None:
    """Validate an exported (possibly templated) effect. Raises ``ValueError``."""
    unknown = set(raw) - set(SchemaEffect.model_fields)
    if unknown:
        raise ValueError(f"unknown SchemaEffect fields: {sorted(unknown)}")
    tp = raw.get("template_params", [])
    if not isinstance(tp, list) or not all(isinstance(p, str) for p in tp):
        raise ValueError("template_params must be a list of setting names")
    body = {k: v for k, v in raw.items() if k not in ("template_params", "dynamic")}
    used = placeholders(body)
    undeclared = used - set(tp)
    if undeclared:
        raise ValueError(
            f"effect uses settings not listed in template_params: {sorted(undeclared)}"
        )
    if not tp:
        SchemaEffect.model_validate(raw)


def resolve_effect(
    raw: SchemaEffect | Mapping[str, JsonValue], settings: Mapping[str, Any]
) -> SchemaEffect:
    """Render a templated effect with concrete ``settings``.

    Raises :class:`~mapi_spec.template.TemplateError` or ``pydantic.ValidationError``.
    """
    if isinstance(raw, SchemaEffect):
        return raw
    tp = raw.get("template_params", [])
    names = [p for p in tp if isinstance(p, str)] if isinstance(tp, list) else []
    params = {p: settings.get(p) for p in names}
    body = {k: v for k, v in raw.items() if k not in ("template_params", "dynamic")}
    rendered = render(body, params)
    assert isinstance(rendered, dict)
    rendered["template_params"] = list(names)
    rendered["dynamic"] = bool(raw.get("dynamic", False))
    return SchemaEffect.model_validate(rendered)


def resolve_context(
    raw: ContextNeed | Mapping[str, JsonValue] | None, settings: Mapping[str, Any]
) -> ContextNeed | None:
    """Render a templated :class:`ContextNeed` (placeholders reference any setting)."""
    if raw is None or isinstance(raw, ContextNeed):
        return raw
    rendered = render(dict(raw), settings)
    return ContextNeed.model_validate(rendered)


# ------------------------------------------------------------------------- application


def _stacked_policy(policies: Sequence[DimPolicy]) -> DimPolicy:
    if DimPolicy.EXTEND in policies:
        return DimPolicy.EXTEND
    if DimPolicy.NEW_LEVELS in policies:
        return DimPolicy.NEW_LEVELS
    return DimPolicy.FIXED


def apply_effect(schema: DataSchema, effect: SchemaEffect) -> tuple[DataSchema | None, list[str]]:
    """Apply one resolved effect. Returns the new schema (or ``None``) and problems."""
    problems: list[str] = []
    if effect.dynamic:
        return None, []
    data = schema.model_dump(mode="python")
    dims: dict[str, Any] = data["dims"]
    vars_: dict[str, Any] = data["vars"]
    ragged: list[dict[str, Any]] = data["ragged"]

    # 1. renames (dims and/or vars)
    if effect.renames:
        for old, new in effect.renames.items():
            if old not in dims and old not in vars_:
                problems.append(f"rename: {old!r} is neither a dim nor a variable")
            elif new != old and (new in dims or new in vars_) and new not in effect.renames:
                problems.append(f"rename: target name {new!r} already exists")
        if not problems:
            renamed = schema.renamed(effect.renames) if effect.renames else schema
            data = renamed.model_dump(mode="python")
            dims, vars_, ragged = data["dims"], data["vars"], data["ragged"]

    # 2. drops
    for name in effect.drops:
        if name in vars_:
            del vars_[name]
        elif name in dims:
            users = [v for v, spec in vars_.items() if name in spec["dims"]]
            if users:
                problems.append(f"drop: dim {name!r} is still used by {sorted(users)}")
            else:
                del dims[name]
                ragged[:] = [r for r in ragged if name not in (r["group_dim"], r["within_dim"])]
        else:
            problems.append(f"drop: {name!r} is neither a dim nor a variable")

    # 3. transforms_dims (add or replace)
    for name, dim_spec in effect.transforms_dims.items():
        if name in vars_:
            problems.append(f"transforms_dims: {name!r} is a variable, not a dim")
            continue
        dims[name] = dim_spec.model_dump(mode="python")

    # 4. stacks
    for new_dim, comps in effect.stacks.items():
        missing = [c for c in comps if c not in dims]
        if missing:
            problems.append(f"stack {new_dim!r}: unknown component dims {missing}")
            continue
        if new_dim in dims or new_dim in vars_:
            problems.append(f"stack {new_dim!r}: name already exists")
            continue
        for vname, v in vars_.items():
            present = [d for d in v["dims"] if d in comps]
            if not present:
                continue
            if len(present) != len(comps):
                problems.append(
                    f"stack {new_dim!r}: variable {vname!r} has only some components {present}"
                )
                continue
            first = min(v["dims"].index(c) for c in comps)
            kept = [d for d in v["dims"] if d not in comps]
            kept.insert(first, new_dim)
            v["dims"] = tuple(kept)
        policies = [DimPolicy(dims[c]["policy"]) for c in comps]
        for c in comps:
            del dims[c]
        dims[new_dim] = DimSpec(kind="index", policy=_stacked_policy(policies)).model_dump()

    # 4b. unstacks
    for old_dim, comps in effect.unstacks.items():
        if old_dim not in dims:
            problems.append(f"unstack {old_dim!r}: unknown dim")
            continue
        clash = [c for c in comps if c in vars_ or (c in dims and c not in effect.transforms_dims)]
        if clash:
            problems.append(f"unstack {old_dim!r}: names already exist {clash}")
            continue
        indexers = [v for v, spec in vars_.items() if spec.get("index_of") == old_dim]
        if indexers:
            problems.append(f"unstack {old_dim!r}: variables {sorted(indexers)} index it")
            continue
        for v in vars_.values():
            if old_dim in v["dims"]:
                at = list(v["dims"]).index(old_dim)
                v["dims"] = (*v["dims"][:at], *comps, *v["dims"][at + 1 :])
        del dims[old_dim]
        for c in comps:
            dims.setdefault(c, DimSpec(kind="categorical").model_dump())
        ragged[:] = [r for r in ragged if old_dim not in (r["group_dim"], r["within_dim"])]

    # 5. updates
    for vname, patch in effect.updates.items():
        if vname not in vars_:
            problems.append(f"update: unknown variable {vname!r}")
            continue
        problems += [f"update {vname!r}: {p}" for p in patch.apply(vars_[vname])]

    # 6. derives
    for vname, derive in effect.derives.items():
        if derive.from_ not in vars_:
            problems.append(f"derive {vname!r}: unknown source variable {derive.from_!r}")
            continue
        if vname in vars_ or vname in dims:
            problems.append(f"derive {vname!r}: name already exists")
            continue
        new_var = dict(vars_[derive.from_])
        new_var["index_of"] = None
        problems += [f"derive {vname!r}: {p}" for p in derive.apply(new_var)]
        vars_[vname] = new_var

    # 7. adds
    for vname, spec in effect.adds.items():
        if vname in vars_ or vname in dims:
            problems.append(f"add: {vname!r} already exists (use updates to change it)")
            continue
        vars_[vname] = spec.model_dump(mode="python")

    if problems:
        return None, problems
    try:
        return DataSchema.model_validate(data), []
    except ValidationError as exc:
        msgs = [str(e["msg"]) for e in exc.errors(include_url=False, include_input=False)]
        return None, [f"resulting schema is invalid: {m}" for m in msgs]


# ------------------------------------------------------------------------- propagation


@dataclass(frozen=True, slots=True)
class Stage:
    """The dataset schema after a step (``name`` = step id; ``raw`` before any step)."""

    name: str
    schema: DataSchema | None


@dataclass(slots=True)
class PropagationResult:
    """Per-stage schemas (index 0 is ``raw``) and the issues found while propagating."""

    stages: list[Stage]
    issues: list[Issue] = field(default_factory=list)

    @property
    def schemas(self) -> list[DataSchema | None]:
        return [s.schema for s in self.stages]

    @property
    def final(self) -> DataSchema | None:
        return self.stages[-1].schema

    def stage(self, name: str) -> DataSchema | None:
        for s in self.stages:
            if s.name == name:
                return s.schema
        raise KeyError(name)

    @property
    def ok(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)


EffectInput = SchemaEffect | Mapping[str, JsonValue]


def propagate(
    raw_schema: DataSchema,
    steps: Sequence[tuple[str, EffectInput, Mapping[str, Any]]],
    *,
    pointer_base: str = "/pipeline",
    pointers: Sequence[str] | None = None,
) -> PropagationResult:
    """Propagate ``raw_schema`` through ``steps`` = ``[(step_id, effect, settings), ...]``.

    Effects may be templates; ``settings`` are substituted into them. A ``dynamic`` effect
    stops static propagation: that stage and every later one are ``None`` and a
    ``SCHEMA_DYNAMIC`` warning is reported (an explicit dry-run can resolve it).
    """
    stages = [Stage(RESERVED_STAGE, raw_schema)]
    issues: list[Issue] = []
    current: DataSchema | None = raw_schema
    for i, (step_id, effect_in, settings) in enumerate(steps):
        ptr = pointers[i] if pointers is not None else pointer_base + json_pointer(i)
        if current is None:
            stages.append(Stage(step_id, None))
            continue
        try:
            effect = resolve_effect(effect_in, settings)
        except (TemplateError, ValidationError, ValueError) as exc:
            msg = (
                "; ".join(str(e["msg"]) for e in exc.errors(include_url=False, include_input=False))
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=ptr,
                    message=f"step {step_id!r}: could not resolve its schema effect: {msg}"[:2000],
                )
            )
            current = None
            stages.append(Stage(step_id, None))
            continue
        if effect.dynamic:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_DYNAMIC,
                    severity=Severity.WARNING,
                    pointer=ptr,
                    message=(
                        f"step {step_id!r} has a dynamic schema effect. The static check "
                        "cannot examine the later stages"
                    ),
                    suggested_action="Do a dry-run to resolve the schema.",
                )
            )
            current = None
            stages.append(Stage(step_id, None))
            continue
        new_schema, problems = apply_effect(current, effect)
        for p in problems:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=ptr,
                    message=f"step {step_id!r}: {p}"[:2000],
                )
            )
        current = new_schema
        stages.append(Stage(step_id, new_schema))
    return PropagationResult(stages, issues)
