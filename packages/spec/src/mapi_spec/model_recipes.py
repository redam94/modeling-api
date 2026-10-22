"""Model recipes (v0.18, D3/D4): compile a :class:`~mapi_spec.library.ModelRecipeDoc` to the
parts of an ordinary ``ModelSpec`` (pipeline, model block, binding, outputs, lineage labels).

Two kinds of recipe:

* **declarative** (``base: declarative``): a GLM chosen from closed sets (family, per-term
  priors, signs, varying slopes, crossed group intercepts, two-way interactions, built-in
  transforms). It compiles to the trusted built-in ``tabular.declarative_glm@^1``
  (:func:`declarative_settings` is its exact settings JSON) or, when the integrator flips
  :data:`COMPILE_TARGET`, to the descoped fallback ``tabular.hierarchical_glm@^1.1`` (one
  group, scalar priors, no interactions or signs).
* **preset** (``base: <id>@<range>``): fixed settings over any catalog model plus the
  ``exposed`` keys a user may change when using it.

Data shape (WP0 probe (b)). The model reads one row per observation on the canonical row dim
``obs`` with the outcome bound to the canonical variable ``y``:

* a long table whose row dim is not ``obs`` is bound (``data.bind.dims: {obs: <row dim>}``);
  groups are variables on that dim (the model encodes their levels at fit), or dims with a
  ``{g}_idx`` index variable;
* a dense ``(time, group)`` panel (what the upload page makes of a flat CSV with a group
  column) gets the built-in ``core.to_long`` step (``group_dim`` = the group, ``within_dim``
  = the other dim), which gives ``obs`` rows, ``{g}_idx`` and the ``{g}`` dim;
* anything else is ``LIBRARY_DATA_SHAPE`` with a hint.

Transforms become steps ``t<i>_<use>`` (``use`` = ``core.<use>@^1``, ``vars`` = the terms'
variables in term order). Identical transforms (same use and settings at the same position
of the terms' transform lists) are merged into one step, so each variable keeps its order.

Every issue points into the recipe document (``/declarative/terms/2/prior/sigma``). This
module is pure and JSON-only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from pydantic import JsonValue

from mapi_spec.catalog import Catalog, ModelCatalogEntry
from mapi_spec.effects import EffectInput, propagate
from mapi_spec.errors import ErrorCode, Issue, Severity, has_errors, json_pointer
from mapi_spec.library import (
    LABEL_MODEL,
    LABEL_PREP,
    ChartTemplateDoc,
    DeclarativeModel,
    LibraryDoc,
    LibraryKind,
    LibraryRef,
    ModelRecipeDoc,
    PrepPresetDoc,
    PriorSpec,
    library_label,
)
from mapi_spec.prep_presets import apply_prep_preset
from mapi_spec.refs import UseRef
from mapi_spec.schema import DataSchema
from mapi_spec.schema_safety import sanitize_schema
from mapi_spec.spec import Binding, FitConfig, ModelBlock, OutputsBlock, StepCall
from mapi_spec.validate import apply_defaults, validate_settings

__all__ = [
    "COMPILE_TARGET",
    "DECLARATIVE_MODEL_ID",
    "DECLARATIVE_MODEL_RANGE",
    "FALLBACK_MODEL_ID",
    "FALLBACK_MODEL_RANGE",
    "ROW_DIM",
    "SHAPE_STEP_ID",
    "TARGET_VAR",
    "CompileTarget",
    "CompiledModel",
    "LibraryResolver",
    "compile_model_recipe",
    "declarative_settings",
    "exposed_settings_schema",
    "fallback_settings",
]

DECLARATIVE_MODEL_ID: Final = "tabular.declarative_glm"
DECLARATIVE_MODEL_RANGE: Final = "^1"
FALLBACK_MODEL_ID: Final = "tabular.hierarchical_glm"
FALLBACK_MODEL_RANGE: Final = "^1.1"
CompileTarget = Literal["declarative", "fallback"]
COMPILE_TARGET: Final[CompileTarget] = "declarative"
"""Flipped to ``"fallback"`` only by the integrator if ``tabular.declarative_glm`` slips."""

TARGET_VAR: Final = "y"
"""The canonical outcome variable of both target models (``data.bind.vars: {y: <outcome>}``)."""
ROW_DIM: Final = "obs"
"""The canonical row dim of both target models."""
SHAPE_STEP_ID: Final = "shape_long"
STEP_RANGE: Final = "^1"

_SCALE_STEPS: Final = frozenset({"standardize", "max_abs_scale", "mean_scale"})
"""Transforms whose ``over`` setting is required; it defaults to the row dim."""
_POSITIVE_DISTS: Final = frozenset({"half_normal", "exponential", "lognormal"})
_NO_MU_DISTS: Final = frozenset({"half_normal", "exponential"})
_SHAPE_HINT: Final = (
    "Map the row number as the dim (long layout), or upload one row for each period and group."
)

LibraryResolver = Callable[[LibraryRef, LibraryKind], LibraryDoc | None]
"""Look up a referenced library item (scope-aware); ``None`` when it does not exist."""


@dataclass(frozen=True, slots=True)
class CompiledModel:
    """The ModelSpec parts a recipe compiles to, plus every issue found on the way."""

    pipeline: list[StepCall]
    model: ModelBlock
    bind: Binding
    outputs: OutputsBlock
    labels: dict[str, str]
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not has_errors(self.issues)

    def spec_fragment(self) -> dict[str, JsonValue]:
        """The parts as a ModelSpec fragment (``data.bind``, ``pipeline``, ``model``,
        ``outputs``, ``metadata.labels``), defaults left out."""
        out: dict[str, JsonValue] = {}
        bind = self.bind.model_dump(mode="json", exclude_defaults=True)
        if bind:
            out["data"] = {"bind": bind}
        out["pipeline"] = [
            s.model_dump(mode="json", by_alias=True, exclude_defaults=True) for s in self.pipeline
        ]
        out["model"] = self.model.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        outputs = self.outputs.model_dump(mode="json", exclude_defaults=True)
        if outputs:
            out["outputs"] = outputs
        if self.labels:
            out["metadata"] = {"labels": dict(self.labels)}
        return out


def _issue(
    pointer: str,
    message: str,
    *,
    code: ErrorCode = ErrorCode.LIBRARY_INVALID,
    severity: Severity = Severity.ERROR,
    action: str | None = None,
) -> Issue:
    return Issue(
        code=code, severity=severity, pointer=pointer, message=message, suggested_action=action
    )


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------------ declarative JSON


def _prior(p: PriorSpec) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {"dist": p.dist, "mu": p.mu, "sigma": p.sigma}
    if p.nu is not None:
        out["nu"] = p.nu
    return out


def declarative_settings(m: DeclarativeModel) -> dict[str, JsonValue]:
    """The exact ``tabular.declarative_glm`` settings JSON of a declarative model::

        {"family": "negative_binomial",            # + "n_categories" for ordinal
         "covariates": ["price", "promo"],          # term variables, in term order
         "groups": ["store"],
         "terms": {"price": {"dist": "normal", "mu": 0.0, "sigma": 0.5, "sign": "negative"},
                   "promo": {..., "sign": "positive", "varies_by": "store"}},
         "interactions": [{"a": "price", "b": "promo", "prior": {"dist": ..., ...}}],
         "intercept": {"dist": "normal", "mu": 0.0, "sigma": 2.0},
         "group_sigma": {"store": {"dist": "half_normal", "mu": 0.0, "sigma": 1.0}},
         "noise": {"dist": "half_normal", "mu": 0.0, "sigma": 1.0}}

    A prior's ``nu`` is present only when set; a term's ``varies_by`` only when set.
    """
    out: dict[str, JsonValue] = {"family": m.outcome.family}
    if m.outcome.n_categories is not None:
        out["n_categories"] = m.outcome.n_categories
    terms: dict[str, JsonValue] = {}
    for t in m.terms:
        term: dict[str, JsonValue] = {**_prior(t.prior), "sign": t.sign}
        if t.varies_by is not None:
            term["varies_by"] = t.varies_by
        terms[t.var] = term
    out["covariates"] = [t.var for t in m.terms]
    out["groups"] = [g.dim for g in m.groups]
    out["terms"] = terms
    out["interactions"] = [{"a": x.a, "b": x.b, "prior": _prior(x.prior)} for x in m.interactions]
    out["intercept"] = _prior(m.intercept)
    out["group_sigma"] = {g.dim: _prior(g.sigma_prior) for g in m.groups}
    out["noise"] = _prior(m.noise)
    return out


def fallback_settings(m: DeclarativeModel) -> tuple[dict[str, JsonValue], list[Issue]]:
    """``tabular.hierarchical_glm`` settings for the descoped fallback (D3, R4), with an
    issue for every choice that model cannot express (pointers into the recipe)."""
    issues: list[Issue] = []
    base = "/declarative"
    if len(m.groups) != 1:
        issues.append(_issue(base + "/groups", "The fallback model needs exactly one group."))
    for i in range(len(m.interactions)):
        issues.append(
            _issue(
                json_pointer("declarative", "interactions", i),
                "The fallback model has no interactions.",
            )
        )
    for i, t in enumerate(m.terms):
        ptr = json_pointer("declarative", "terms", i)
        if t.sign != "any":
            issues.append(_issue(ptr + "/sign", "The fallback model has no sign constraints."))
        if t.prior.dist != "normal" or t.prior.mu != 0.0:
            issues.append(
                _issue(
                    ptr + "/prior",
                    "The fallback model uses one normal(0, sigma) prior for all terms. "
                    "It uses the largest sigma.",
                    severity=Severity.WARNING,
                )
            )
    if m.intercept.dist != "normal" or m.intercept.mu != 0.0:
        issues.append(
            _issue(
                base + "/intercept",
                "The fallback model uses a normal(0, sigma) intercept prior.",
                severity=Severity.WARNING,
            )
        )
    group = m.groups[0].dim if m.groups else "group"
    out: dict[str, JsonValue] = {
        "group_dim": group,
        "family": m.outcome.family,
        "covariates": [t.var for t in m.terms],
        "varying_slopes": [t.var for t in m.terms if t.varies_by is not None],
        "intercept_sigma": m.intercept.sigma,
        "group_sigma_scale": m.groups[0].sigma_prior.sigma if m.groups else 1.0,
        "beta_sigma": max(t.prior.sigma for t in m.terms),
        "noise_sigma": m.noise.sigma,
    }
    if m.outcome.n_categories is not None:
        out["n_categories"] = m.outcome.n_categories
    return out, issues


# --------------------------------------------------------------------- declarative checks


def _prior_issues(p: PriorSpec, ptr: str, *, positive: bool = False) -> list[Issue]:
    out: list[Issue] = []
    if p.nu is not None and p.dist != "student_t":
        out.append(_issue(ptr + "/nu", "Only the student_t prior uses nu."))
    if p.dist in _NO_MU_DISTS and p.mu != 0.0:
        out.append(
            _issue(ptr + "/mu", f"The {p.dist} prior does not use mu.", severity=Severity.WARNING)
        )
    if positive and p.dist not in _POSITIVE_DISTS:
        out.append(
            _issue(ptr + "/dist", "A scale prior must be half_normal, exponential or lognormal.")
        )
    return out


def _declarative_issues(m: DeclarativeModel) -> list[Issue]:
    issues: list[Issue] = []
    o = m.outcome
    if (o.family == "ordinal") != (o.n_categories is not None):
        issues.append(
            _issue(
                "/declarative/outcome/n_categories",
                "The ordinal family needs n_categories."
                if o.family == "ordinal"
                else "Only the ordinal family uses n_categories.",
            )
        )
    groups = [g.dim for g in m.groups]
    term_vars = [t.var for t in m.terms]
    seen: set[str] = set()
    for i, t in enumerate(m.terms):
        ptr = json_pointer("declarative", "terms", i)
        if t.var in seen:
            issues.append(_issue(ptr + "/var", f"Variable {t.var!r} is in more than one term."))
        seen.add(t.var)
        if t.var == o.var:
            issues.append(_issue(ptr + "/var", "A term cannot use the outcome variable."))
        elif t.var == TARGET_VAR:
            issues.append(
                _issue(
                    ptr + "/var",
                    f"The name {TARGET_VAR!r} is kept for the outcome. Rename the variable.",
                )
            )
        if t.var in groups:
            issues.append(_issue(ptr + "/var", f"{t.var!r} is a group, not a term."))
        if t.varies_by is not None and t.varies_by not in groups:
            issues.append(
                _issue(ptr + "/varies_by", f"varies_by {t.varies_by!r} is not one of the groups.")
            )
        issues.extend(_prior_issues(t.prior, ptr + "/prior"))
        if t.sign == "any" and t.prior.dist in _POSITIVE_DISTS:
            issues.append(
                _issue(
                    ptr + "/sign",
                    f"The {t.prior.dist} prior allows only positive effects. Set the sign to "
                    "positive or negative.",
                    severity=Severity.WARNING,
                )
            )
        for j, tr in enumerate(t.transform):
            if "vars" in tr.with_:
                issues.append(
                    _issue(
                        json_pointer("declarative", "terms", i, "transform", j, "with", "vars"),
                        "The term sets vars. Remove it.",
                    )
                )
    pairs: set[frozenset[str]] = set()
    for i, x in enumerate(m.interactions):
        ptr = json_pointer("declarative", "interactions", i)
        for side in ("a", "b"):
            name = getattr(x, side)
            if name not in term_vars:
                issues.append(_issue(f"{ptr}/{side}", f"{name!r} is not a term variable."))
        if x.a == x.b:
            issues.append(_issue(ptr + "/b", "An interaction needs two different terms."))
        elif frozenset((x.a, x.b)) in pairs:
            issues.append(_issue(ptr, "This interaction is listed twice."))
        pairs.add(frozenset((x.a, x.b)))
        issues.extend(_prior_issues(x.prior, ptr + "/prior"))
    gseen: set[str] = set()
    for i, g in enumerate(m.groups):
        ptr = json_pointer("declarative", "groups", i)
        if g.dim in gseen:
            issues.append(_issue(ptr + "/dim", f"Group {g.dim!r} is listed twice."))
        gseen.add(g.dim)
        if g.dim in (o.var, TARGET_VAR, ROW_DIM):
            issues.append(_issue(ptr + "/dim", f"{g.dim!r} cannot be a group."))
        issues.extend(_prior_issues(g.sigma_prior, ptr + "/sigma_prior", positive=True))
    issues.extend(_prior_issues(m.intercept, "/declarative/intercept"))
    issues.extend(_prior_issues(m.noise, "/declarative/noise", positive=True))
    return issues


# ----------------------------------------------------------------------------- transforms


@dataclass(slots=True)
class _Transform:
    use: str
    with_: dict[str, JsonValue]
    vars: list[str]
    pointer: str  # the first term transform merged into this step


def _transform_steps(
    m: DeclarativeModel, catalog: Catalog, issues: list[Issue]
) -> tuple[list[StepCall], list[str]]:
    """Merged transform steps (and the doc pointer of each)."""
    groups: dict[tuple[int, str, str], _Transform] = {}
    for i, t in enumerate(m.terms):
        for j, tr in enumerate(t.transform):
            ptr = json_pointer("declarative", "terms", i, "transform", j)
            with_ = {k: v for k, v in tr.with_.items() if k != "vars"}
            if tr.use in _SCALE_STEPS and "over" not in with_:
                with_["over"] = [ROW_DIM]
            ref = UseRef.parse(f"core.{tr.use}@{STEP_RANGE}")
            entry = catalog.resolve_step(ref)
            if entry is None:
                issues.append(
                    _issue(
                        ptr + "/use",
                        f"The step {str(ref)!r} is not in the current catalog.",
                        code=ErrorCode.EXTENSION_UNAVAILABLE,
                    )
                )
            else:
                issues.extend(
                    x.model_copy(update={"code": ErrorCode.LIBRARY_INVALID})
                    for x in validate_settings(
                        entry.settings_schema, {"vars": [t.var], **with_}, ptr + "/with"
                    )
                    if x.pointer != ptr + "/with/vars"
                )
            key = (j, tr.use, _canon(with_))
            if key in groups:
                groups[key].vars.append(t.var)
            else:
                groups[key] = _Transform(tr.use, with_, [t.var], ptr)
    ordered = sorted(groups.items(), key=lambda kv: kv[0][0])  # stable: first use per position
    steps: list[StepCall] = []
    pointers: list[str] = []
    for n, (_, step) in enumerate(ordered):
        steps.append(
            StepCall.model_validate(
                {
                    "id": f"t{n}_{step.use}",
                    "use": f"core.{step.use}@{STEP_RANGE}",
                    "with": {"vars": step.vars, **step.with_},
                }
            )
        )
        pointers.append(step.pointer)
    return steps, pointers


# ----------------------------------------------------------------------------- data shape


def _shape(
    m: DeclarativeModel, schema: DataSchema, target: CompileTarget, issues: list[Issue]
) -> tuple[Binding, list[StepCall]]:
    """The binding and shape steps that give the model one row per observation on ``obs``."""
    o = m.outcome
    ov = schema.vars.get(o.var)
    if ov is None:
        issues.append(_issue("/declarative/outcome/var", f"The dataset has no variable {o.var!r}."))
        return Binding(), []
    bad = False
    for i, t in enumerate(m.terms):
        if t.var not in schema.vars:
            issues.append(
                _issue(
                    json_pointer("declarative", "terms", i, "var"),
                    f"The dataset has no variable {t.var!r}.",
                )
            )
            bad = True
    for i, g in enumerate(m.groups):
        if g.dim not in schema.vars and g.dim not in schema.dims:
            issues.append(
                _issue(
                    json_pointer("declarative", "groups", i, "dim"),
                    f"The dataset has no variable or dim {g.dim!r}.",
                )
            )
            bad = True
    if bad:
        return Binding(), []

    def shape_error(pointer: str, message: str) -> tuple[Binding, list[StepCall]]:
        issues.append(
            _issue(pointer, message, code=ErrorCode.LIBRARY_DATA_SHAPE, action=_SHAPE_HINT)
        )
        return Binding(), []

    bind_vars = {TARGET_VAR: o.var} if o.var != TARGET_VAR else {}
    dims = ov.dims
    if len(dims) == 1:  # a long (or one-dim) table: one row per observation already
        row = dims[0]
        if row != ROW_DIM and (ROW_DIM in schema.dims or ROW_DIM in schema.vars):
            return shape_error(
                "/declarative/outcome/var",
                f"The dataset already has a {ROW_DIM!r} that is not its rows.",
            )
        for i, t in enumerate(m.terms):
            if schema.vars[t.var].dims != (row,):
                return shape_error(
                    json_pointer("declarative", "terms", i, "var"),
                    f"Variable {t.var!r} does not have one value for each row.",
                )
        for i, g in enumerate(m.groups):
            ptr = json_pointer("declarative", "groups", i, "dim")
            if g.dim in schema.vars:
                if schema.vars[g.dim].dims != (row,):
                    return shape_error(
                        ptr, f"Group {g.dim!r} does not have one value for each row."
                    )
                if target == "fallback":
                    return shape_error(
                        ptr,
                        f"The fallback model needs {g.dim!r} as a dim "
                        f"with an index variable {g.dim}_idx.",
                    )
            else:
                idx = schema.vars.get(f"{g.dim}_idx")
                if idx is None or idx.dims != (row,) or idx.index_of != g.dim:
                    return shape_error(
                        ptr,
                        f"Group dim {g.dim!r} needs an index variable {g.dim}_idx over the rows.",
                    )
        bind_dims = {ROW_DIM: row} if row != ROW_DIM else {}
        return Binding(dims=bind_dims, vars=bind_vars), []

    dim_groups = [g.dim for g in m.groups if g.dim in dims]
    if len(dims) == 2 and len(dim_groups) == 1:  # a dense (time, group) panel → long rows
        group = dim_groups[0]
        within = next(d for d in dims if d != group)
        if ROW_DIM in schema.dims or ROW_DIM in schema.vars:
            return shape_error(
                "/declarative/outcome/var",
                f"The dataset already has a {ROW_DIM!r}. It cannot be made long.",
            )
        grid = set(dims)
        for i, t in enumerate(m.terms):
            if set(schema.vars[t.var].dims) != grid:
                return shape_error(
                    json_pointer("declarative", "terms", i, "var"),
                    f"Variable {t.var!r} is not on the ({within}, {group}) grid of the outcome.",
                )
        for i, g in enumerate(m.groups):
            if g.dim != group and (
                g.dim not in schema.vars or set(schema.vars[g.dim].dims) != grid
            ):
                return shape_error(
                    json_pointer("declarative", "groups", i, "dim"),
                    f"Group {g.dim!r} is not on the ({within}, {group}) grid.",
                )
            if g.dim != group and target == "fallback":
                return shape_error(
                    json_pointer("declarative", "groups", i, "dim"),
                    "The fallback model needs exactly one group.",
                )
        # the step runs after the binding: it sees the outcome as ``y``
        on_grid = [
            TARGET_VAR if n == o.var else n for n, v in schema.vars.items() if set(v.dims) == grid
        ]
        step = StepCall.model_validate(
            {
                "id": SHAPE_STEP_ID,
                "use": f"core.to_long@{STEP_RANGE}",
                "with": {
                    "group_dim": group,
                    "within_dim": within,
                    "row_dim": ROW_DIM,
                    "vars": on_grid,
                },
            }
        )
        return Binding(vars=bind_vars), [step]
    return shape_error(
        "/declarative/outcome/var",
        f"The outcome {o.var!r} has dims {list(dims)}. The model needs one row "
        "for each observation, or a (time, group) grid with one of the groups.",
    )


# --------------------------------------------------------------------------------- presets


def _settings_schema(entry: ModelCatalogEntry) -> dict[str, Any]:
    schema = entry.manifest.settings_schema
    return dict(schema) if isinstance(schema, Mapping) else {}


def exposed_settings_schema(doc: ModelRecipeDoc, catalog: Catalog) -> dict[str, JsonValue] | None:
    """The "Use" form of a preset: the base model's settings schema restricted to the
    ``exposed`` keys (after the schema-safety checks, ``strip`` mode), with the recipe's
    settings as defaults. ``None`` for a declarative recipe or an unknown base."""
    if doc.is_declarative:
        return None
    entry = catalog.resolve_model(str(doc.base))
    if entry is None:
        return None
    schema = _settings_schema(entry)
    props = schema.get("properties")
    props = props if isinstance(props, Mapping) else {}
    restricted: dict[str, Any] = {}
    for key in doc.exposed:
        sub = props.get(key)
        if not isinstance(sub, Mapping):
            continue
        restricted[key] = {**sub, "default": doc.settings[key]} if key in doc.settings else sub
    out: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": restricted,
    }
    if isinstance(schema.get("$defs"), Mapping):
        out["$defs"] = schema["$defs"]
    safe, _ = sanitize_schema(out, mode="strip", where="")
    return safe


def _preset(
    doc: ModelRecipeDoc,
    catalog: Catalog,
    exposed: Mapping[str, JsonValue],
    fit: FitConfig,
    issues: list[Issue],
) -> ModelBlock:
    assert not isinstance(doc.base, str)
    if doc.declarative is not None:
        issues.append(
            _issue("/declarative", "Only a recipe with base: declarative uses declarative.")
        )
    for key in exposed:
        if key not in doc.exposed:
            issues.append(_issue(json_pointer("exposed", key), f"Setting {key!r} is not exposed."))
    settings = {**doc.settings, **{k: v for k, v in exposed.items() if k in doc.exposed}}
    entry = catalog.resolve_model(doc.base)
    if entry is None:
        issues.append(
            _issue(
                "/base",
                f"The model {str(doc.base)!r} is not in the current catalog.",
                code=ErrorCode.EXTENSION_UNAVAILABLE,
            )
        )
        return ModelBlock(use=doc.base, settings=settings, fit=fit)
    schema = _settings_schema(entry)
    props = schema.get("properties")
    props = props if isinstance(props, Mapping) else {}
    closed = schema.get("additionalProperties") is False
    for key in doc.settings:
        if closed and key not in props:
            issues.append(
                _issue(json_pointer("settings", key), f"{key!r} is not a setting of {entry.id}.")
            )
    seen: set[str] = set()
    for i, key in enumerate(doc.exposed):
        if key in seen:
            issues.append(_issue(json_pointer("exposed", i), f"{key!r} is exposed twice."))
        seen.add(key)
        if key not in props:
            issues.append(
                _issue(json_pointer("exposed", i), f"{key!r} is not a setting of {entry.id}.")
            )
    # unknown keys are reported above, by name; the schema checks the others
    known = {k: v for k, v in settings.items() if not closed or k in props}
    issues.extend(
        x.model_copy(update={"code": ErrorCode.LIBRARY_INVALID})
        for x in validate_settings(schema, known, "/settings")
    )
    return ModelBlock(use=doc.base, settings=settings, fit=fit)


# --------------------------------------------------------------------------------- compile


def _resolve_prep(
    doc: ModelRecipeDoc,
    resolve: LibraryResolver | None,
    slots: Mapping[str, JsonValue],
    schema: DataSchema | None,
    catalog: Catalog,
    bind: Binding,
    issues: list[Issue],
    labels: dict[str, str],
) -> list[StepCall]:
    ref = doc.prep
    if ref is None:
        return []
    labels[LABEL_PREP] = library_label(ref.item, ref.revision if ref.revision != "latest" else None)
    if resolve is None:
        issues.append(
            _issue(
                "/prep", "The prep preset is added when the recipe is used.", severity=Severity.INFO
            )
        )
        return []
    prep = resolve(ref, "prep_preset")
    if not isinstance(prep, PrepPresetDoc):
        issues.append(_issue("/prep/item", "The prep preset is not in the library."))
        return []
    # slot values name dataset variables; after the binding the steps see canonical names
    canon = bind.canonical_of()

    def rename(v: JsonValue) -> JsonValue:
        if isinstance(v, str):
            return canon.get(v, v)
        if isinstance(v, list):
            return [canon.get(x, x) if isinstance(x, str) else x for x in v]
        return v

    bound = schema.renamed(canon) if schema is not None and canon else schema
    steps, prep_issues = apply_prep_preset(
        prep, {k: rename(v) for k, v in slots.items()}, schema=bound, catalog=catalog
    )
    issues.extend(i.model_copy(update={"pointer": "/prep" + i.pointer}) for i in prep_issues)
    return steps


def compile_model_recipe(
    doc: ModelRecipeDoc,
    catalog: Catalog,
    *,
    item_ref: str | None = None,
    exposed: Mapping[str, JsonValue] | None = None,
    slots: Mapping[str, JsonValue] | None = None,
    resolve: LibraryResolver | None = None,
    schema: DataSchema | None = None,
    target: CompileTarget | None = None,
) -> CompiledModel:
    """Compile a model recipe against ``catalog`` (and, with ``schema``, the dataset's raw
    schema: binding, shape step and effect checks).

    ``item_ref`` (``lib_<id>-r<n>``) becomes the ``mapi.library/model`` label; ``exposed``
    and ``slots`` are the use-time values of the preset's exposed settings and the prep
    preset's slots; ``resolve`` looks up the referenced prep preset and chart templates.
    ``target`` overrides :data:`COMPILE_TARGET` (tests and the integrator's fallback).
    The result always has a model block; check :attr:`CompiledModel.ok`.
    """
    exposed = {} if exposed is None else exposed
    slots = {} if slots is None else slots
    target = COMPILE_TARGET if target is None else target
    issues: list[Issue] = []
    labels: dict[str, str] = {}
    if item_ref is not None:
        labels[LABEL_MODEL] = item_ref
    fit = doc.fit if doc.fit is not None else FitConfig()
    bind = Binding()
    shape_steps: list[StepCall] = []
    transform_steps: list[StepCall] = []
    transform_ptrs: list[str] = []

    if doc.is_declarative:
        m = doc.declarative
        if m is None:
            issues.append(
                _issue(
                    "/declarative",
                    "A recipe with base: declarative needs a declarative model.",
                    action="Add declarative.",
                )
            )
            model = ModelBlock(
                use=UseRef.parse(f"{DECLARATIVE_MODEL_ID}@{DECLARATIVE_MODEL_RANGE}")
            )
        else:
            for key, ptr in (("settings", "/settings"), ("exposed", "/exposed")):
                if getattr(doc, key):
                    issues.append(_issue(ptr, f"A declarative recipe does not use {key}."))
            for key in exposed:
                issues.append(
                    _issue(json_pointer("exposed", key), f"Setting {key!r} is not exposed.")
                )
            issues.extend(_declarative_issues(m))
            if target == "declarative":
                use = UseRef.parse(f"{DECLARATIVE_MODEL_ID}@{DECLARATIVE_MODEL_RANGE}")
                settings = declarative_settings(m)
            else:
                use = UseRef.parse(f"{FALLBACK_MODEL_ID}@{FALLBACK_MODEL_RANGE}")
                settings, fb = fallback_settings(m)
                issues.extend(fb)
            entry = catalog.resolve_model(use)
            if entry is None:
                issues.append(
                    _issue(
                        "/base",
                        f"The model {str(use)!r} is not in the current catalog.",
                        code=ErrorCode.EXTENSION_UNAVAILABLE,
                    )
                )
            elif not has_errors(issues):
                # the model's own schema; only when the recipe checks passed (no echo). The
                # settings are generated, so the issues point at the declarative block
                issues.extend(
                    x.model_copy(
                        update={"code": ErrorCode.LIBRARY_INVALID, "pointer": "/declarative"}
                    )
                    for x in validate_settings(_settings_schema(entry), settings, "/declarative")
                )
            model = ModelBlock(use=use, settings=settings, fit=fit)
            transform_steps, transform_ptrs = _transform_steps(m, catalog, issues)
            if schema is not None:
                bind, shape_steps = _shape(m, schema, target, issues)
            else:
                bind = Binding(
                    vars={TARGET_VAR: m.outcome.var} if m.outcome.var != TARGET_VAR else {}
                )
                issues.append(
                    _issue(
                        "",
                        "The data shape is checked when you choose a dataset.",
                        code=ErrorCode.LIBRARY_DATA_SHAPE,
                        severity=Severity.INFO,
                    )
                )
    else:
        model = _preset(doc, catalog, exposed, fit, issues)

    prep_steps = _resolve_prep(doc, resolve, slots, schema, catalog, bind, issues, labels)

    plots: list[UseRef] = []
    for i, ref in enumerate(doc.plots):
        if catalog.resolve_plot(ref) is None:
            issues.append(
                _issue(
                    json_pointer("plots", i),
                    f"The plot {str(ref)!r} is not in the current catalog.",
                    code=ErrorCode.EXTENSION_UNAVAILABLE,
                )
            )
        plots.append(ref)
    if resolve is not None:
        for i, cref in enumerate(doc.charts):
            if not isinstance(resolve(cref, "chart_template"), ChartTemplateDoc):
                issues.append(
                    _issue(
                        json_pointer("charts", i, "item"),
                        "The chart template is not in the library.",
                    )
                )

    pipeline = [*prep_steps, *shape_steps, *transform_steps]
    if schema is not None and not has_errors(issues):
        issues.extend(
            _effect_issues(
                schema,
                bind,
                pipeline,
                catalog,
                ["/prep"] * len(prep_steps)
                + ["/declarative/groups"] * len(shape_steps)
                + transform_ptrs,
            )
        )
    return CompiledModel(
        pipeline=pipeline,
        model=model,
        bind=bind,
        outputs=OutputsBlock(plots=plots),
        labels=labels,
        issues=issues,
    )


def _effect_issues(
    schema: DataSchema,
    bind: Binding,
    pipeline: list[StepCall],
    catalog: Catalog,
    pointers: list[str],
) -> list[Issue]:
    """Propagate the bound raw schema through the pipeline's static effects."""
    effects: list[tuple[str, EffectInput, Mapping[str, Any]]] = []
    for s in pipeline:
        entry = catalog.resolve_step(s.use)
        if entry is None:
            return []  # already reported
        effects.append((s.id, entry.effect, apply_defaults(entry.settings_schema, s.with_)))
    canon = bind.canonical_of()
    bound = schema.renamed(canon) if canon else schema
    return propagate(bound, effects, pointers=pointers).issues
