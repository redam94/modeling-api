"""Static (JSON-only) validation of ModelSpecs and ScoreRequests.

:func:`validate_spec` checks a :class:`~mapi_spec.spec.ModelSpec` against a
:class:`~mapi_spec.catalog.Catalog` and the dataset's raw :class:`DataSchema`:

1. binding references exist and do not collide;
2. every ``use:`` resolves to a non-yanked version (semver range);
3. step and model settings validate against their JSON Schemas;
4. side-table references exist;
5. the schema is propagated through every step's SchemaEffect, with per-step input checks;
6. the split stage is resolved (default: end of the stateless prefix), its dims exist there,
   and ``LEAKAGE_RISK`` is raised if a stateful step precedes it;
7. the model's schema template is resolved and compared with the final stage; spec dim
   policies may only *tighten* the plugin's;
8. recursive forecasting (a target-dependent lag feeding a longer holdout) is rejected
   unless the model owns recursion; outputs, plots, metrics and resource class are checked.

:func:`validate_score_request` runs the static CoordPlan checks for a ScoreRequest from
metadata only (resolved spec + fit coordinate summaries + dim policies).

Every finding is an :class:`~mapi_spec.errors.Issue` with a JSON pointer into the document.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import Field, JsonValue

from mapi_spec._types import RESERVED_STAGE, Scalar, StrictModel
from mapi_spec.calibration import check_business_priors
from mapi_spec.capabilities import (
    REPRESENTATION_RULES,
    Capabilities,
    Representation,
    can_aggregate,
    summary_allowed,
)
from mapi_spec.catalog import Catalog, StepCatalogEntry
from mapi_spec.coords import CoordSummary, DataSummary, parse_freq
from mapi_spec.effects import ContextNeed, Stage, StepKind, propagate
from mapi_spec.errors import (
    ErrorCode,
    Issue,
    Severity,
    ValidationFailed,
    has_errors,
    join_pointer,
    json_pointer,
)
from mapi_spec.loader import spec_hash
from mapi_spec.schema import (
    NUMERIC_DTYPES,
    DataSchema,
    DimPolicy,
    DimSpec,
    Role,
    policy_is_at_least_as_tight,
    tighten_policy,
)
from mapi_spec.spec import Binding, FutureCovariates, ModelSpec, ScorePolicy, ScoreRequest
from mapi_spec.template import TemplateError
from mapi_spec.tree import NodeSelectionError, TreeSchema

__all__ = [
    "KNOWN_METRICS",
    "ResolvedModel",
    "ResolvedSpec",
    "ResolvedStep",
    "ScoreMetadata",
    "SpecValidation",
    "apply_defaults",
    "resolve_input_schema",
    "validate_score_request",
    "validate_settings",
    "validate_spec",
]

MAX_SETTINGS_ISSUES = 50

KNOWN_METRICS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"^elpd_(loo|waic)$",
        r"^coverage_[1-9][0-9]?$",
        r"^interval_width_[1-9][0-9]?$",
        r"^(mape|smape|wape|rmse|mae|mase|crps|r2|bias|log_score|accuracy|auc|brier)$",
        r"^accuracy_(naive|lift)$",
    )
)
_UNCERTAINTY_METRICS = re.compile(r"^(coverage_|interval_width_|crps$|elpd_|log_score$)")


# ---------------------------------------------------------------------------------- results


class ResolvedStep(StrictModel):
    """A pipeline step after ref resolution, with defaults applied and effects evaluated."""

    id: str
    use: str
    resolved: str
    kind: StepKind
    stateful: bool
    invertible: bool
    depends_on_target: bool
    context: ContextNeed | None = None
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    side_tables: list[str] = Field(default_factory=list)
    input_stage: str


class ResolvedModel(StrictModel):
    use: str
    resolved: str
    capabilities: Capabilities
    input_schema: DataSchema | None = None
    effective_policies: dict[str, DimPolicy] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)
    derived: list[str] = Field(default_factory=list)


class ResolvedSpec(StrictModel):
    """What the control plane stores with a run: everything score-time checks need."""

    spec_hash: str
    binding: Binding
    steps: list[ResolvedStep]
    stages: dict[str, DataSchema | None]
    split_stage: str | None
    side_tables: dict[str, ScorePolicy] = Field(default_factory=dict)
    model: ResolvedModel

    @property
    def stage_order(self) -> list[str]:
        return list(self.stages)

    @property
    def model_stage(self) -> str:
        return list(self.stages)[-1]

    def steps_after(self, stage: str) -> list[ResolvedStep]:
        """Steps executed when data enters at ``stage``."""
        if stage == RESERVED_STAGE:
            return list(self.steps)
        ids = [s.id for s in self.steps]
        return list(self.steps[ids.index(stage) + 1 :]) if stage in ids else []

    @property
    def last_reshape_stage(self) -> str:
        """Inverse transforms stop here (the output space of results)."""
        last = RESERVED_STAGE
        for s in self.steps:
            if s.kind is StepKind.RESHAPE:
                last = s.id
        return last


@dataclass(slots=True)
class SpecValidation:
    """Outcome of :func:`validate_spec`."""

    issues: list[Issue]
    stages: list[Stage]
    resolved: ResolvedSpec | None = None

    @property
    def ok(self) -> bool:
        return not has_errors(self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    def raise_for_errors(self) -> ResolvedSpec:
        if not self.ok or self.resolved is None:
            raise ValidationFailed(self.issues)
        return self.resolved


# ------------------------------------------------------------------------ JSON Schema bits


def apply_defaults(schema: Mapping[str, Any], instance: Mapping[str, Any]) -> dict[str, Any]:
    """Fill ``default`` values of (nested) object properties missing from ``instance``."""
    out = dict(instance)
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return out
    for name, sub in props.items():
        if not isinstance(sub, Mapping):
            continue
        if name not in out and "default" in sub:
            out[name] = sub["default"]
        if isinstance(out.get(name), Mapping) and sub.get("type") == "object":
            out[name] = apply_defaults(sub, out[name])
    return out


_KEYWORD_MESSAGES = {
    "required": "a required setting is missing",
    "additionalProperties": "unknown setting",
    "unevaluatedProperties": "unknown setting",
    "type": "wrong type",
    "enum": "value is not one of the allowed options",
    "const": "value is not the allowed constant",
    "minimum": "value is below the minimum",
    "exclusiveMinimum": "value is below the minimum",
    "maximum": "value is above the maximum",
    "exclusiveMaximum": "value is above the maximum",
    "minItems": "too few items",
    "maxItems": "too many items",
    "uniqueItems": "items must be unique",
    "minLength": "text is too short",
    "maxLength": "text is too long",
    "pattern": "text does not match the necessary pattern",
    "oneOf": "value matches none (or several) of the allowed shapes",
    "anyOf": "value matches none of the allowed shapes",
    "multipleOf": "value is not an allowed multiple",
}


def validate_settings(
    schema: Mapping[str, Any],
    instance: Any,
    pointer: str,
    *,
    code: ErrorCode = ErrorCode.SETTINGS_INVALID,
) -> list[Issue]:
    """Validate ``instance`` against a JSON Schema; issues point into the document.

    Messages are platform-authored; jsonschema's own text (which may echo user values) goes
    into ``untrusted_text``.
    """
    if not schema:
        return []
    validator = Draft202012Validator(schema)
    issues: list[Issue] = []
    for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
        path = list(err.absolute_path)
        ptr = join_pointer(pointer, *path)
        if err.validator == "required" and isinstance(err.instance, dict):
            missing = [r for r in (err.validator_value or []) if r not in err.instance]
            for name in missing:
                issues.append(
                    Issue(
                        code=code,
                        pointer=join_pointer(ptr, str(name)),
                        message=f"required setting {str(name)[:64]!r} is missing",
                    )
                )
            continue
        issues.append(
            Issue(
                code=code,
                pointer=ptr,
                message=_KEYWORD_MESSAGES.get(str(err.validator), "value is not correct"),
                untrusted_text=str(err.message)[:2000],
            )
        )
        if len(issues) >= MAX_SETTINGS_ISSUES:
            break
    return issues


# ------------------------------------------------------------------------------- binding


def _apply_binding(bind: Binding, raw: DataSchema, issues: list[Issue]) -> DataSchema | None:
    n_before = len(issues)
    for section, mapping, expect_dim in (("dims", bind.dims, True), ("vars", bind.vars, False)):
        for canon, ds in mapping.items():
            ptr = json_pointer("data", "bind", section, canon)
            present = ds in (raw.dims if expect_dim else raw.vars)
            if not present:
                other = ds in (raw.vars if expect_dim else raw.dims)
                what = "dim" if expect_dim else "variable"
                hint = f" (it is a {'variable' if expect_dim else 'dim'})" if other else ""
                issues.append(
                    Issue(
                        code=ErrorCode.BINDING_INVALID,
                        pointer=ptr,
                        message=f"the dataset has no {what} with the name {ds!r}{hint}",
                    )
                )
    bound_sources = set(bind.dims.values()) | set(bind.vars.values())
    for canon, ds in {**bind.dims, **bind.vars}.items():
        if canon != ds and raw.has_name(canon) and canon not in bound_sources:
            section = "dims" if canon in bind.dims else "vars"
            issues.append(
                Issue(
                    code=ErrorCode.BINDING_INVALID,
                    pointer=json_pointer("data", "bind", section, canon),
                    message=(
                        f"the canonical name {canon!r} is the same as an unbound dataset name. "
                        "Bind or remove that dataset name first."
                    ),
                )
            )
    if len(issues) > n_before:
        return None
    mapping = {ds: canon for canon, ds in {**bind.dims, **bind.vars}.items()}
    return raw.renamed(mapping) if mapping else raw


# ------------------------------------------------------------------------------ helpers


def _names(value: Any) -> list[tuple[str | None, Any]]:
    """Settings value → [(index-or-None, name)] for str or list[str] values."""
    if isinstance(value, str):
        return [(None, value)]
    if isinstance(value, list):
        return [(str(i), v) for i, v in enumerate(value) if isinstance(v, str)]
    return []


def _check_step_inputs(
    entry: StepCatalogEntry,
    settings: Mapping[str, Any],
    schema: DataSchema,
    ptr: str,
    context: ContextNeed | None,
) -> list[Issue]:
    issues: list[Issue] = []
    for param in entry.var_params:
        for idx, name in _names(settings.get(param)):
            if name not in schema.vars:
                p = join_pointer(ptr, "with", param, *(() if idx is None else (idx,)))
                issues.append(
                    Issue(
                        code=ErrorCode.SCHEMA_INVALID,
                        pointer=p,
                        message=f"variable {name!r} does not exist at this step's input stage",
                        context={"available": sorted(schema.vars)[:50]},
                    )
                )
    for param in entry.dim_params:
        for idx, name in _names(settings.get(param)):
            if name not in schema.dims:
                p = join_pointer(ptr, "with", param, *(() if idx is None else (idx,)))
                issues.append(
                    Issue(
                        code=ErrorCode.SCHEMA_INVALID,
                        pointer=p,
                        message=f"dim {name!r} does not exist at this step's input stage",
                        context={"available": sorted(schema.dims)[:50]},
                    )
                )
    if context is not None and not issues:
        if context.along not in schema.dims:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=join_pointer(ptr, "with"),
                    message=f"context dim {context.along!r} does not exist at this step's "
                    "input stage",
                )
            )
        for d in context.within:
            # dense layout: a dim; long layout: a variable along the row dim (``hh(obs)``)
            if d in schema.dims:
                continue
            var = schema.vars.get(d)
            if var is not None and list(var.dims) == [context.along]:
                continue
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=join_pointer(ptr, "with"),
                    message=f"context group {d!r} is neither a dim nor a variable along "
                    f"{context.along!r} at this step's input stage",
                )
            )
    return issues


def _depends_on_target(
    entry: StepCatalogEntry, settings: Mapping[str, Any], schema: DataSchema | None
) -> bool:
    if not entry.depends_on_target:
        return False
    if entry.target_vars_param is None or schema is None:
        return True
    names = {n for _, n in _names(settings.get(entry.target_vars_param))}
    return bool(names & set(schema.targets))


_OWNED_ROLES = frozenset({Role.TARGET, Role.MEDIA, Role.SPEND})


def _check_owned(
    caps: Capabilities,
    candidates: Sequence[tuple[int, str, list[str], list[str]]],
    issues: list[Issue],
) -> None:
    """A model that ``owns`` scaling or carry-over does it itself (plan §4.3): the same
    platform preprocessing on its target/media variables would apply it twice."""
    for idx, step_id, provides, touched in candidates:
        for what in provides:
            if what not in caps.owns:
                continue
            issues.append(
                Issue(
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                    pointer=json_pointer("pipeline", idx),
                    message=f"the model owns {what}. Step {step_id!r} would apply {what} to "
                    f"{sorted(touched)} a second time",
                    suggested_action=f"Remove the step. The model does its own {what}.",
                )
            )


def _template_targets(spec: ModelSpec, catalog: Catalog) -> list[str]:
    """Canonical target variables of the model's resolved schema template (best effort)."""
    entry = catalog.resolve_model(spec.model.use)
    if entry is None:
        return []
    man = entry.manifest
    settings = apply_defaults(man.settings_schema, spec.model.plugin_settings)
    model_schema, _ = man.schema_template.try_resolve(settings)
    return list(model_schema.targets) if model_schema is not None else []


def _mark_targets(schema: DataSchema, targets: Sequence[str], issues: list[Issue]) -> DataSchema:
    """``schema`` with the variables named in ``targets`` given role ``target``."""
    names = [t for t in targets if t in schema.vars and schema.vars[t].role is not Role.TARGET]
    if not names:
        return schema
    data = schema.model_dump()
    for n in names:
        issues.append(
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                severity=Severity.INFO,
                pointer="/model",
                message=f"variable {n!r} is the target of the model. The server uses the role "
                f"'target' for it (the data declares {schema.vars[n].role.value!r})",
            )
        )
        data["vars"][n]["role"] = Role.TARGET
    return DataSchema.model_validate(data)


def _metric_known(name: str) -> bool:
    return any(p.match(name) for p in KNOWN_METRICS)


def _dtype_family(dtype: str) -> str:
    return "numeric" if dtype in NUMERIC_DTYPES else dtype


# ---------------------------------------------------------------------------- validate_spec


def resolve_input_schema(
    spec: ModelSpec,
    raw_schema: DataSchema,
    tree_schema: TreeSchema | None = None,
) -> tuple[DataSchema | None, list[Issue]]:
    """The flat schema the pipeline of ``spec`` reads, and the issues found resolving it (M4).

    For a plain dataset (``tree_schema`` is ``None``) that is ``raw_schema``, and
    ``data.nodes`` is an error. For a tree dataset it is the merge of the nodes
    ``data.nodes`` selects (:meth:`TreeSchema.flat`), or the root node when ``data.nodes``
    is omitted and the root has variables. ``None`` when it cannot be resolved.
    """
    nodes = spec.data.nodes
    if tree_schema is None:
        if nodes is None:
            return raw_schema, []
        return raw_schema, [
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                pointer=json_pointer("data", "nodes"),
                message="this dataset is not a tree: data.nodes only applies to tree datasets",
                suggested_action="Remove data.nodes, or use a tree dataset version",
            )
        ]
    if nodes is None:
        if tree_schema.root.vars:
            return tree_schema.root, []
        return None, [
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                pointer=json_pointer("data"),
                message="this dataset is a tree: pick nodes in data.nodes",
                suggested_action="Add data.nodes naming the node(s) to read, e.g. node "
                f"{tree_schema.default_node()!r}",
            )
        ]
    try:
        return tree_schema.flat(nodes), []
    except NodeSelectionError as exc:
        ptr = (
            json_pointer("data", "nodes", exc.key)
            if exc.key is not None
            else json_pointer("data", "nodes")
        )
        return None, [Issue(code=ErrorCode.SCHEMA_INVALID, pointer=ptr, message=str(exc)[:2000])]


def validate_spec(
    spec: ModelSpec,
    catalog: Catalog,
    raw_schema: DataSchema,
    *,
    tree_schema: TreeSchema | None = None,
) -> SpecValidation:
    """Statically validate ``spec`` against ``catalog`` and the dataset's ``raw_schema``.

    For a tree dataset (M4) pass its ``tree_schema`` too (``raw_schema`` is then its root
    node's schema and is not used): the pipeline input is resolved from ``data.nodes`` by
    :func:`resolve_input_schema`.
    """
    # 0. tree datasets: the flat schema data.nodes selects ------------------------------
    flat, issues = resolve_input_schema(spec, raw_schema, tree_schema)

    # 1. binding ------------------------------------------------------------------------
    current = _apply_binding(spec.data.bind, flat, issues) if flat is not None else None
    if current is not None:
        # the model's template decides what the target is, whatever the raw schema says
        # (an inferred schema marks nothing as target); target dependence, recursion and
        # leakage checks below follow these roles
        current = _mark_targets(current, _template_targets(spec, catalog), issues)
    stages: list[Stage] = [Stage(RESERVED_STAGE, current)]

    # 2-5. steps ------------------------------------------------------------------------
    resolved_steps: list[ResolvedStep | None] = []
    owned_candidates: list[tuple[int, str, list[str], list[str]]] = []
    for i, call in enumerate(spec.pipeline):
        ptr = json_pointer("pipeline", i)
        input_stage = stages[-1].name
        entry = catalog.resolve_step(call.use)
        if entry is None:
            known = catalog.versions("step", call.use.id)
            msg = (
                f"no version of {call.use.id!r} satisfies {str(call.use.range)!r} "
                f"(available: {', '.join(known)})"
                if known
                else f"unknown step {call.use.id!r}"
            )
            issues.append(Issue(code=ErrorCode.REF_UNRESOLVED, pointer=f"{ptr}/use", message=msg))
            resolved_steps.append(None)
            current = None
            stages.append(Stage(call.id, None))
            continue

        issues += validate_settings(entry.settings_schema, call.with_, f"{ptr}/with")
        settings = apply_defaults(entry.settings_schema, call.with_)

        tables: list[str] = []
        for param in entry.side_table_params:
            for idx, name in _names(settings.get(param)):
                tables.append(name)
                if name not in spec.data.side_tables:
                    p = join_pointer(ptr, "with", param, *(() if idx is None else (idx,)))
                    issues.append(
                        Issue(
                            code=ErrorCode.REF_UNRESOLVED,
                            pointer=p,
                            message=f"side table {name!r} is not declared in data.side_tables",
                        )
                    )

        context: ContextNeed | None = None
        try:
            context = entry.resolved_context(settings)
        except (TemplateError, ValueError) as exc:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=f"{ptr}/with",
                    message=f"could not resolve the step's context need: {exc}"[:2000],
                )
            )

        if current is not None:
            input_issues = _check_step_inputs(entry, settings, current, ptr, context)
            issues += input_issues
            if input_issues:
                current = None
                stages.append(Stage(call.id, None))
            else:
                result = propagate(current, [(call.id, entry.effect, settings)], pointers=[ptr])
                issues += result.issues
                current = result.final
                stages.append(Stage(call.id, current))
        else:
            stages.append(Stage(call.id, None))

        # Best effort when the input stage is unknown (e.g. after a dynamic step): judge target
        # dependence against the last statically known stage.
        input_schema = next((st.schema for st in reversed(stages[:-1]) if st.schema), None)
        if entry.provides and input_schema is not None:
            touched = [
                name
                for param in entry.var_params
                for _, name in _names(settings.get(param))
                if name in input_schema.vars and input_schema.vars[name].role in _OWNED_ROLES
            ]
            if touched:
                owned_candidates.append((i, call.id, list(entry.provides), touched))
        resolved_steps.append(
            ResolvedStep(
                id=call.id,
                use=str(call.use),
                resolved=entry.ref,
                kind=entry.kind,
                stateful=entry.stateful,
                invertible=entry.invertible,
                depends_on_target=_depends_on_target(entry, settings, input_schema),
                context=context,
                settings=settings,
                side_tables=tables,
                input_stage=input_stage,
            )
        )

    # 6. split --------------------------------------------------------------------------
    split_stage = _check_split(spec, resolved_steps, stages, issues)

    # 7-8. model ------------------------------------------------------------------------
    resolved_model = _check_model(spec, catalog, stages[-1].schema, resolved_steps, issues)
    if resolved_model is not None:
        _check_owned(resolved_model.capabilities, owned_candidates, issues)
        _check_calibration(spec, resolved_model.capabilities, issues)

    resolved: ResolvedSpec | None = None
    if not has_errors(issues) and resolved_model is not None and all(resolved_steps):
        resolved = ResolvedSpec(
            spec_hash=spec_hash(spec),
            binding=spec.data.bind,
            steps=[s for s in resolved_steps if s is not None],
            stages={s.name: s.schema for s in stages},
            split_stage=split_stage,
            side_tables={k: v.score_policy for k, v in spec.data.side_tables.items()},
            model=resolved_model,
        )
    return SpecValidation(issues=issues, stages=stages, resolved=resolved)


def _check_calibration(spec: ModelSpec, caps: Capabilities, issues: list[Issue]) -> None:
    """The ``calibration`` block needs a model that declares what it carries (M3)."""
    cal = spec.calibration
    if cal is None:
        return
    if cal.business_priors is not None:
        issues.extend(check_business_priors(cal.business_priors, "/calibration/business_priors"))
    wanted: list[tuple[str, str, bool]] = [
        ("business_priors", "/calibration/business_priors", cal.business_priors is not None),
        ("lift_tests", "/calibration/lift_tests", bool(cal.lift_tests)),
    ]
    for kind, ptr, present in wanted:
        if present and kind not in caps.calibration:
            issues.append(
                _issue(
                    ErrorCode.CALIBRATION_UNSUPPORTED,
                    ptr,
                    f"model {spec.model.use.id!r} does not use {kind.replace('_', ' ')}",
                    suggested_action="Remove this calibration data, or select a model that "
                    "can use it.",
                )
            )


def _check_split(
    spec: ModelSpec,
    steps: Sequence[ResolvedStep | None],
    stages: Sequence[Stage],
    issues: list[Issue],
) -> str | None:
    split = spec.data.split
    if split is None:
        if spec.model.fit.mode.value == "evaluate_then_refit":
            issues.append(
                Issue(
                    code=ErrorCode.SPLIT_INVALID,
                    severity=Severity.WARNING,
                    pointer="/data",
                    message="fit.mode is evaluate_then_refit, but the spec declares no split. "
                    "There is no holdout for the evaluation",
                    suggested_action="Add data.split (for example time_holdout), or use "
                    "train_only.",
                )
            )
        return None

    # default stage: end of the stateless prefix (stateless steps without context needs)
    if split.after_step is None:
        prefix_end = 0
        for s in steps:
            if s is None or s.stateful or s.context is not None:
                break
            prefix_end += 1
        stage_index = prefix_end  # index into stages (0 = raw)
    elif split.after_step == RESERVED_STAGE:
        stage_index = 0
    else:
        stage_index = 1 + [s.name for s in stages[1:]].index(split.after_step)
    stage = stages[stage_index]

    for j, s in enumerate(steps[:stage_index]):
        if s is not None and s.stateful:
            sev = Severity.WARNING if split.allow_leakage else Severity.ERROR
            issues.append(
                Issue(
                    code=ErrorCode.LEAKAGE_RISK,
                    severity=sev,
                    pointer="/data/split/after_step"
                    if split.after_step is not None
                    else "/data/split",
                    message=(
                        f"stateful step {s.id!r} (pipeline[{j}]) runs before the split at "
                        f"stage {stage.name!r}. The step would learn its state from holdout data"
                    ),
                    suggested_action="Move the split earlier (after_step), or move the step "
                    "after the split.",
                )
            )

    schema = stage.schema
    if schema is not None:
        dim_fields: list[tuple[str, str]] = []
        if hasattr(split, "dim"):
            dim_fields.append(("/data/split/dim", split.dim))
        for k, d in enumerate(getattr(split, "within", []) or []):
            dim_fields.append((f"/data/split/within/{k}", d))
        for k, d in enumerate(getattr(split, "dims", []) or []):
            dim_fields.append((f"/data/split/dims/{k}", d))
        for ptr, d in dim_fields:
            if d not in schema.dims:
                issues.append(
                    Issue(
                        code=ErrorCode.SPLIT_INVALID,
                        pointer=ptr,
                        message=f"split dim {d!r} does not exist at stage {stage.name!r}",
                        context={"stage": stage.name, "dims": sorted(schema.dims)},
                        suggested_action="Set data.split.after_step to the stage where the dim "
                        "exists (for example, after a resample step).",
                    )
                )
        fdim = split.forecast_dim
        if fdim is not None and fdim in schema.dims:
            spec_ = schema.dims[fdim]
            if spec_.kind not in ("time", "integer", "index") and not spec_.ordered:
                issues.append(
                    Issue(
                        code=ErrorCode.SPLIT_INVALID,
                        pointer="/data/split/dim",
                        message=f"split {split.kind!r} needs an ordered dim. {fdim!r} is not "
                        "ordered",
                    )
                )
    return stage.name


def _check_model(
    spec: ModelSpec,
    catalog: Catalog,
    final: DataSchema | None,
    steps: Sequence[ResolvedStep | None],
    issues: list[Issue],
) -> ResolvedModel | None:
    mb = spec.model
    entry = catalog.resolve_model(mb.use)
    if entry is None:
        known = catalog.versions("model", mb.use.id)
        msg = (
            f"no version of {mb.use.id!r} satisfies {str(mb.use.range)!r} "
            f"(available: {', '.join(known)})"
            if known
            else f"unknown model {mb.use.id!r}"
        )
        issues.append(Issue(code=ErrorCode.REF_UNRESOLVED, pointer="/model/use", message=msg))
        return None
    man = entry.manifest
    caps = man.capabilities

    plugin_settings = mb.plugin_settings
    issues += validate_settings(man.settings_schema, plugin_settings, "/model/settings")
    fit_json = mb.fit.model_dump(mode="json", exclude_defaults=True)
    issues += validate_settings(man.fit_schema, fit_json, "/model/fit")
    settings = apply_defaults(man.settings_schema, plugin_settings)

    model_schema, err = man.schema_template.try_resolve(settings)
    if model_schema is None:
        issues.append(
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/model/settings",
                message=f"could not resolve the model's input schema template: {err}"[:2000],
            )
        )

    effective: dict[str, DimPolicy] = {}
    input_schema: DataSchema | None = None
    if model_schema is not None:
        effective, input_schema = _effective_schema(mb.dim_policies, model_schema, final, issues)
        if final is not None:
            _compare_schemas(model_schema, final, issues)

    # recursion vs split holdout
    split = spec.data.split
    if split is not None and "recursion" not in caps.owns:
        for idx, s in enumerate(steps):
            if s is None or not s.depends_on_target or s.context is None:
                continue
            if s.context.along != split.forecast_dim:
                continue
            min_lag = s.context.min_lag if s.context.min_lag is not None else 1
            periods = split.forecast_periods
            if periods is None:
                issues.append(
                    Issue(
                        code=ErrorCode.RECURSIVE_UNSUPPORTED,
                        severity=Severity.WARNING,
                        pointer=json_pointer("pipeline", idx),
                        message=f"step {s.id!r} lags the target along {s.context.along!r}. The "
                        "holdout length is not static, thus the forecast can possibly need "
                        "recursion",
                    )
                )
            elif periods > min_lag:
                issues.append(
                    Issue(
                        code=ErrorCode.RECURSIVE_UNSUPPORTED,
                        pointer=json_pointer("pipeline", idx),
                        message=(
                            f"step {s.id!r} lags the target by {min_lag} along "
                            f"{s.context.along!r}, but the holdout has {periods} periods. "
                            "Version 1 cannot do recursive forecasts"
                        ),
                        suggested_action="Lag the target by the holdout length or more, make "
                        "the holdout shorter, or use a model that owns recursion.",
                    )
                )

    # outputs / plots / metrics / resources
    derived = man.derived_names()
    for j, name in enumerate(spec.outputs.derived):
        if name not in derived:
            issues.append(
                Issue(
                    code=ErrorCode.REF_UNRESOLVED,
                    pointer=json_pointer("outputs", "derived", j),
                    message=f"model {man.id!r} does not declare derived quantity {name!r}",
                    context={"available": sorted(derived)},
                )
            )
    for j, plot in enumerate(spec.outputs.plots):
        pentry = catalog.resolve_plot(plot)
        if pentry is None:
            issues.append(
                Issue(
                    code=ErrorCode.REF_UNRESOLVED,
                    pointer=json_pointer("outputs", "plots", j),
                    message=f"unknown plot {plot.id!r}",
                )
            )
        elif pentry.requires_representation and caps.representation not in (
            pentry.requires_representation
        ):
            issues.append(
                Issue(
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                    pointer=json_pointer("outputs", "plots", j),
                    message=f"plot {plot.id!r} needs representation "
                    f"{'/'.join(r.value for r in pentry.requires_representation)}. The model "
                    f"gives {caps.representation.value}",
                )
            )
    for j, metric in enumerate(spec.evaluate.metrics):
        ptr = json_pointer("evaluate", "metrics", j)
        if not _metric_known(metric):
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    severity=Severity.WARNING,
                    pointer=ptr,
                    message=f"unknown metric {metric!r}. The server ignores it if the model "
                    "does not calculate it",
                )
            )
        if metric == "elpd_loo" and not caps.loo_valid:
            issues.append(
                Issue(
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                    pointer=ptr,
                    message="LOO is not valid for this model",
                    untrusted_text=caps.loo_invalid_reason,
                )
            )
        if caps.representation is Representation.POINT and _UNCERTAINTY_METRICS.match(metric):
            issues.append(
                Issue(
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                    pointer=ptr,
                    message=f"metric {metric!r} needs uncertainty. The model gives only "
                    "point predictions",
                )
            )
    if spec.execution.resource_class < man.resources.min_class:
        issues.append(
            Issue(
                code=ErrorCode.CAPABILITY_UNSUPPORTED,
                pointer="/execution/resource_class",
                message=f"model {man.id!r} needs at least resource class "
                f"{man.resources.min_class.value}",
            )
        )

    return ResolvedModel(
        use=str(mb.use),
        resolved=entry.ref,
        capabilities=caps,
        input_schema=input_schema,
        effective_policies=effective,
        outputs=sorted(man.outputs),
        derived=sorted(derived),
    )


def _effective_schema(
    overrides: Mapping[str, DimPolicy],
    model_schema: DataSchema,
    final: DataSchema | None,
    issues: list[Issue],
) -> tuple[dict[str, DimPolicy], DataSchema]:
    """Combine data and plugin dim policies; spec overrides may only tighten them."""
    effective: dict[str, DimPolicy] = {}
    dims: dict[str, DimSpec] = {}
    for name, mspec in model_schema.dims.items():
        policy = mspec.policy
        if final is not None and name in final.dims:
            policy = tighten_policy(policy, final.dims[name].policy)
        dims[name] = mspec
        effective[name] = policy
    for name, wanted in overrides.items():
        ptr = json_pointer("model", "settings", "dim_policies", name)
        if name not in effective:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer=ptr,
                    message=f"dim {name!r} is not a dim of the model's input schema",
                )
            )
            continue
        if not policy_is_at_least_as_tight(wanted, effective[name]):
            issues.append(
                Issue(
                    code=ErrorCode.DIM_POLICY_LOOSENED,
                    pointer=ptr,
                    message=f"you can only make the policy {effective[name].value!r} of dim "
                    f"{name!r} stricter. You cannot change it to {wanted.value!r}",
                )
            )
            continue
        effective[name] = wanted
    new_dims: dict[str, DimSpec] = {}
    for name, d in dims.items():
        pol = effective[name]
        update: dict[str, Any] = {"policy": pol}
        if pol is not DimPolicy.EXTEND:
            update["max_extension"] = None
        if pol is not DimPolicy.NEW_LEVELS:
            update["new_level_strategy"] = "error"
        if final is not None and name in final.dims:
            fd = final.dims[name]
            if d.freq is None and fd.freq is not None and d.kind == "time":
                update["freq"] = fd.freq
            if pol is DimPolicy.EXTEND and fd.max_extension is not None:
                cur = d.max_extension
                update["max_extension"] = (
                    fd.max_extension if cur is None else min(cur, fd.max_extension)
                )
        new_dims[name] = d.model_copy(update=update)
    return effective, model_schema.model_copy(update={"dims": new_dims})


def _compare_schemas(model: DataSchema, final: DataSchema, issues: list[Issue]) -> None:
    for name, mdim in model.dims.items():
        if name not in final.dims:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/model",
                    message=f"the model needs dim {name!r}, but the pipeline does not make it",
                    context={"pipeline_dims": sorted(final.dims)},
                )
            )
            continue
        fdim = final.dims[name]
        if mdim.kind != fdim.kind:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/model",
                    message=f"dim {name!r}: the model needs kind {mdim.kind!r}, but the pipeline "
                    f"makes {fdim.kind!r}",
                )
            )
        if mdim.freq is not None and fdim.freq is not None and mdim.freq != fdim.freq:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/model",
                    message=f"dim {name!r}: the model needs frequency {mdim.freq!r}, but the "
                    f"pipeline makes {fdim.freq!r}",
                )
            )
    for name, mvar in model.vars.items():
        fvar = final.vars.get(name)
        if fvar is None:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/model",
                    message=f"the model needs variable {name!r}, but the pipeline does not make it",
                    suggested_action="Bind a dataset variable to this name in data.bind.vars.",
                    context={"pipeline_vars": sorted(final.vars)[:100]},
                )
            )
            continue
        if set(mvar.dims) != set(fvar.dims):
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/model",
                    message=f"variable {name!r}: the model needs dims {list(mvar.dims)}, but the "
                    f"pipeline makes {list(fvar.dims)}",
                )
            )
        if _dtype_family(mvar.dtype) != _dtype_family(fvar.dtype):
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    pointer="/model",
                    message=f"variable {name!r}: the model needs dtype {mvar.dtype}, but the "
                    f"pipeline makes {fvar.dtype}",
                )
            )
        if mvar.role != fvar.role:
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    severity=Severity.WARNING,
                    pointer="/model",
                    message=f"variable {name!r}: the model expects role {mvar.role.value!r}, but "
                    f"the data declares {fvar.role.value!r}",
                )
            )


# ------------------------------------------------------------------- validate_score_request


class ScoreMetadata(StrictModel):
    """Per-model-version metadata used for static CoordPlan checks.

    ``fit_coords`` maps a stage name to coordinate summaries of the *full* fit data at that
    stage (the production refit), at least for the model stage.
    """

    resolved: ResolvedSpec
    fit_coords: dict[str, dict[str, CoordSummary]] = Field(default_factory=dict)
    data_space: Literal["transformed"] | None = Field(
        None,
        exclude_if=lambda v: v is None,
        description="'transformed': the model was fitted on data a workflow transform changed "
        "without a preprocessing-step equivalent (mapi_spec.actuals.DATA_SPACE_LABEL); it "
        "refuses actuals and every score carries a warning",
    )


def _issue(code: ErrorCode, pointer: str, message: str, **kw: Any) -> Issue:
    return Issue(code=code, pointer=pointer, message=message[:2000], **kw)


def _preview(values: Sequence[Scalar] | set[Scalar], n: int = 10) -> str:
    vals = sorted(values, key=str)
    s = ", ".join(str(v) for v in vals[:n])
    return s + (f", ... (+{len(vals) - n})" if len(vals) > n else "")


def validate_score_request(
    req: ScoreRequest,
    meta: ScoreMetadata,
    inputs: DataSummary | None = None,
    *,
    max_draws: int | None = None,
    live_capabilities: Capabilities | None = None,
) -> list[Issue]:
    """Static CoordPlan checks for ``req`` against a fitted model's metadata.

    ``inputs`` summarises the payload (``data`` or ``scenario``) as it enters at
    ``req.inputs_stage``; dataset names are mapped through the binding at every stage, as
    the runtime does. When it says where the targets hold values (``inputs.observed``), a
    forecast's actuals are checked (:mod:`mapi_spec.actuals`). ``live_capabilities`` are
    those of the plugin build that will score, which may have gained score-time features
    (``conditions_on_actuals``) since the fit; the fitted ones are used otherwise.
    """
    issues: list[Issue] = []
    rs = meta.resolved
    caps = rs.model.capabilities
    rules = REPRESENTATION_RULES[caps.representation]

    # mode -----------------------------------------------------------------------------
    if req.mode not in caps.score_modes:
        issues.append(
            _issue(
                ErrorCode.MODE_NOT_SUPPORTED,
                "/mode",
                f"model {rs.model.resolved!r} does not support mode {req.mode!r}",
                context={"supported": sorted(caps.score_modes)},
            )
        )

    transformed = meta.data_space == "transformed"
    if transformed:
        from mapi_spec.actuals import transformed_space_issue

        issues.append(transformed_space_issue())

    # stage ----------------------------------------------------------------------------
    if req.inputs_stage not in rs.stages:
        issues.append(
            _issue(
                ErrorCode.REF_UNRESOLVED,
                "/inputs_stage",
                f"unknown stage {req.inputs_stage!r}",
                context={"stages": list(rs.stages)},
            )
        )
        stage_schema: DataSchema | None = None
        executed = []
    else:
        stage_schema = rs.stages[req.inputs_stage]
        executed = rs.steps_after(req.inputs_stage)

    model_schema = _model_view(rs)
    # The runtime applies the binding to the payload whatever the entry stage (dataset names
    # become canonical ones), so the static check does too.
    to_canon = rs.binding.canonical_of()

    def canon(name: str) -> str:
        return to_canon.get(name, name)

    # side tables ----------------------------------------------------------------------
    needed: set[str] = set()
    for step in executed:
        needed.update(step.side_tables)
    for name in req.side_tables:
        if name not in rs.side_tables:
            issues.append(
                _issue(
                    ErrorCode.REF_UNRESOLVED,
                    json_pointer("side_tables", name),
                    f"the model has no side table named {name!r}",
                )
            )
        elif rs.side_tables[name] is ScorePolicy.PINNED:
            issues.append(
                _issue(
                    ErrorCode.SIDE_TABLE_COVERAGE,
                    json_pointer("side_tables", name),
                    f"side table {name!r} has score_policy 'pinned'. The score uses the "
                    "version of the fit again, thus you cannot supply it",
                    suggested_action="Remove it from side_tables, or fit again with "
                    "score_policy 'latest' or 'supplied'.",
                )
            )
    for name in sorted(needed):
        if rs.side_tables.get(name) is ScorePolicy.SUPPLIED and name not in req.side_tables:
            issues.append(
                _issue(
                    ErrorCode.SIDE_TABLE_COVERAGE,
                    json_pointer("side_tables", name),
                    f"side table {name!r} has score_policy 'supplied'. You must supply it for "
                    f"stage {req.inputs_stage!r}",
                    suggested_action="Supply it in side_tables, or use a later "
                    "inputs_stage that is after the step that uses it.",
                )
            )

    # horizon --------------------------------------------------------------------------
    if req.horizon is not None:
        hdim, periods = req.horizon.dim, req.horizon.periods
        dspec = model_schema.dims.get(hdim) if model_schema is not None else None
        if dspec is None:
            issues.append(
                _issue(
                    ErrorCode.COORD_NOT_ALLOWED,
                    "/horizon/dim",
                    f"{hdim!r} is not a dim of the model",
                )
            )
        elif dspec.policy is not DimPolicy.EXTEND:
            issues.append(
                _issue(
                    ErrorCode.COORD_NOT_ALLOWED,
                    "/horizon/dim",
                    f"dim {hdim!r} has policy {dspec.policy.value!r}. Only 'extend' dims "
                    "accept a horizon",
                )
            )
        elif dspec.max_extension is not None and periods > dspec.max_extension:
            issues.append(
                _issue(
                    ErrorCode.HORIZON_EXCEEDS_FIT,
                    "/horizon/periods",
                    f"the horizon of {periods} is more than the {dspec.max_extension} periods that "
                    f"this fit supports along {hdim!r}. You must fit the model again",
                    suggested_action="Fit the model again with a larger expected range.",
                )
            )
        if "recursion" not in caps.owns:
            for step in executed:
                ctx = step.context
                if not step.depends_on_target or ctx is None or ctx.along != hdim:
                    continue
                min_lag = ctx.min_lag if ctx.min_lag is not None else 1
                if periods > min_lag:
                    issues.append(
                        _issue(
                            ErrorCode.RECURSIVE_UNSUPPORTED,
                            "/horizon/periods",
                            f"step {step.id!r} lags the target by {min_lag}. A horizon of "
                            f"{periods} would need a recursive forecast (version 1 cannot "
                            "do this)",
                        )
                    )

    # extension past the fit carried by the payload itself (no horizon needed) ---------
    inputs_ptr = "/scenario" if req.scenario is not None else "/data"
    extension: dict[str, int] = {}
    if inputs is not None and stage_schema is not None:
        extension = _payload_extension(req, meta, inputs, stage_schema, canon)
    if req.horizon is None and "recursion" not in caps.owns:
        for step in executed:
            ctx = step.context
            if not step.depends_on_target or ctx is None or ctx.along not in extension:
                continue
            step_stage = rs.stages.get(step.input_stage)
            sdim = step_stage.dims.get(ctx.along) if step_stage is not None else None
            if (
                stage_schema is None
                or sdim is None
                or (sdim.freq != stage_schema.dims[ctx.along].freq)
            ):
                continue  # a different frequency at the step's stage; judged in the sandbox
            min_lag = ctx.min_lag if ctx.min_lag is not None else 1
            periods = extension[ctx.along]
            if periods > min_lag:
                issues.append(
                    _issue(
                        ErrorCode.RECURSIVE_UNSUPPORTED,
                        inputs_ptr,
                        f"step {step.id!r} lags the target by {min_lag}. Input data that goes "
                        f"{periods} periods after the fit along {ctx.along!r} would need "
                        "a recursive forecast (version 1 cannot do this)",
                    )
                )

    # actuals: target values in a forecast payload (mapi_spec.actuals) ----------------
    supplied = {canon(v) for v in inputs.vars} if inputs is not None else set()
    actuals: _Actuals | None = None
    live = bool(live_capabilities and live_capabilities.conditions_on_actuals)
    can_condition = (caps.conditions_on_actuals or live) and not transformed
    if (
        req.mode == "forecast"
        and req.horizon is not None
        and inputs is not None
        and stage_schema is not None
    ):
        actuals = _check_actuals(
            req,
            meta,
            inputs,
            stage_schema,
            model_schema,
            canon,
            supplied=supplied,
            supported=can_condition,
            pointer=inputs_ptr,
            issues=issues,
            data_space=meta.data_space,
        )
    if req.mode == "predict" and inputs is not None and stage_schema is not None:
        _check_unused_targets(
            req,
            meta,
            inputs,
            stage_schema,
            canon,
            supported=can_condition and "forecast" in caps.score_modes,
            pointer=inputs_ptr,
            issues=issues,
        )
    if (
        actuals is None
        and req.mode == "forecast"
        and req.horizon is not None
        and extension.get(req.horizon.dim, 0) not in (0, req.horizon.periods)
        and _same_freq(stage_schema, model_schema, req.horizon.dim)
    ):
        listed = extension[req.horizon.dim]
        issues.append(
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                severity=Severity.WARNING,
                pointer="/horizon/periods",
                message=f"the server does not use horizon.periods ({req.horizon.periods}). The "
                f"payload lists {listed} period(s) after the fit, and the server forecasts "
                "only those periods",
            )
        )

    # future covariates ----------------------------------------------------------------
    extends = req.horizon is not None or any(v > 0 for v in extension.values())
    if (
        extends
        and stage_schema is not None
        and req.future_covariates in (None, FutureCovariates.SUPPLIED)
    ):
        required = {n for n, v in stage_schema.vars.items() if v.future_required}
        if model_schema is not None:
            required |= {
                n
                for n, v in model_schema.vars.items()
                if v.future_required and n in stage_schema.vars
            }
        required -= set(stage_schema.targets)
        missing = sorted(required - supplied)
        if missing:
            issues.append(
                _issue(
                    ErrorCode.MISSING_FUTURE_COVARIATE,
                    inputs_ptr,
                    f"the forecast needs future values of {missing} at stage {req.inputs_stage!r}",
                    suggested_action="Supply them in a scenario dataset.",
                )
            )
        elif required and inputs is not None and req.horizon is not None:
            listed = extension.get(req.horizon.dim, 0) > 0
            if actuals is None and not listed:
                _check_horizon_coverage(req, meta, inputs, canon, inputs_ptr, issues)
            elif actuals is None or not actuals.appended:
                # the payload lists the periods to forecast: their drivers must hold values
                _check_future_rows(req, meta, inputs, canon, required, actuals, inputs_ptr, issues)
            elif actuals.appended:
                issues.append(
                    _issue(
                        ErrorCode.MISSING_FUTURE_COVARIATE,
                        inputs_ptr,
                        f"the payload ends with an actual ({actuals.through}). Thus the server "
                        f"adds the {req.horizon.periods} horizon periods after it without "
                        f"values of {sorted(required)}",
                        suggested_action="In the payload, list the periods to forecast with "
                        "their driver values and an empty target. Or set future_covariates "
                        "(carry_last, seasonal_naive or zero).",
                    )
                )

    # input coords ---------------------------------------------------------------------
    if inputs is not None and stage_schema is not None:
        fit_stage = meta.fit_coords.get(req.inputs_stage, {})
        for raw_dim, cs in inputs.coords.items():
            dim = canon(raw_dim)
            in_spec = stage_schema.dims.get(dim)
            ptr = join_pointer(inputs_ptr)
            if in_spec is None:
                issues.append(
                    _issue(
                        ErrorCode.COORD_NOT_ALLOWED,
                        ptr,
                        f"the input has dim {dim!r}, but this dim does not exist at stage "
                        f"{req.inputs_stage!r}",
                    )
                )
                continue
            policy = in_spec.policy
            strategy: str = in_spec.new_level_strategy
            max_ext = in_spec.max_extension
            if model_schema is not None and dim in model_schema.dims:
                mdim = model_schema.dims[dim]
                policy = tighten_policy(policy, mdim.policy)
                if policy is DimPolicy.NEW_LEVELS:
                    strategy = mdim.new_level_strategy
                if req.inputs_stage == rs.model_stage or in_spec.freq == mdim.freq:
                    vals = [v for v in (max_ext, mdim.max_extension) if v is not None]
                    max_ext = min(vals) if vals else None
            fit = fit_stage.get(dim)
            if fit is None:
                continue
            mode: str = req.mode
            if actuals is not None and req.horizon is not None and dim == req.horizon.dim:
                mode = "forecast_actuals"  # the forecast starts after the last actual
            issues += _check_coord(dim, cs, fit, policy, strategy, max_ext, ptr, mode)

    # select ---------------------------------------------------------------------------
    model_fit = meta.fit_coords.get(rs.model_stage, {})
    dataset_to_canon = rs.binding.canonical_of()
    for raw_dim, values in req.select.items():
        dim = dataset_to_canon.get(raw_dim, raw_dim)
        ptr = json_pointer("select", raw_dim)
        sdim = model_schema.dims.get(dim) if model_schema is not None else None
        if sdim is None:
            issues.append(_issue(ErrorCode.COORD_NOT_ALLOWED, ptr, f"{dim!r} is not a model dim"))
            continue
        fit = model_fit.get(dim)
        levels = fit.level_set if fit is not None else None
        if levels is None or sdim.policy is DimPolicy.EXTEND:
            continue
        unseen = set(values) - levels
        new_ok = sdim.policy is DimPolicy.NEW_LEVELS and sdim.new_level_strategy != "error"
        if unseen and not new_ok:
            issues.append(
                _issue(
                    ErrorCode.UNSEEN_LEVELS,
                    ptr,
                    f"the fit did not see {len(unseen)} selected level(s) of {dim!r}, and the "
                    f"policy of the dim is {sdim.policy.value!r}",
                    untrusted_text=_preview(unseen),
                )
            )

    # interventions --------------------------------------------------------------------
    for k, iv in enumerate(req.interventions):
        ptr = json_pointer("interventions", k)
        if stage_schema is None:
            break
        var = canon(iv.var)
        vspec = stage_schema.vars.get(var)
        if vspec is None:
            issues.append(
                _issue(
                    ErrorCode.SCHEMA_INVALID,
                    f"{ptr}/var",
                    f"variable {var!r} does not exist at stage {req.inputs_stage!r}",
                )
            )
            continue
        if vspec.role is Role.TARGET:
            issues.append(
                _issue(
                    ErrorCode.SCHEMA_INVALID,
                    f"{ptr}/var",
                    "you cannot make an intervention on the target",
                )
            )
        fit_stage = meta.fit_coords.get(req.inputs_stage, {})
        for raw_dim, sel in iv.sel.items():
            dim = canon(raw_dim)
            if dim not in vspec.dims:
                issues.append(
                    _issue(
                        ErrorCode.COORD_NOT_ALLOWED,
                        join_pointer(ptr, "sel", raw_dim),
                        f"variable {var!r} has no dim {dim!r}",
                    )
                )
                continue
            sel_spec = stage_schema.dims[dim]
            fit = fit_stage.get(dim)
            levels = fit.level_set if fit is not None else None
            if sel_spec.policy is DimPolicy.FIXED and levels is not None:
                chosen = set(sel if isinstance(sel, list) else [sel])
                unseen = chosen - levels
                if unseen:
                    issues.append(
                        _issue(
                            ErrorCode.UNSEEN_LEVELS,
                            join_pointer(ptr, "sel", raw_dim),
                            f"{len(unseen)} level(s) of fixed dim {dim!r} are unknown",
                            untrusted_text=_preview(unseen),
                        )
                    )

    # output selector ------------------------------------------------------------------
    out = req.output
    if out.draws > 0 and not rules.draws_available:
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/output/draws",
                f"the request asks for draws, but the representation of the model is "
                f"{caps.representation.value!r}",
            )
        )
    if max_draws is not None and out.draws > max_draws:
        issues.append(
            _issue(
                ErrorCode.QUOTA_EXCEEDED,
                "/output/draws",
                f"this tier can return a maximum of {max_draws} draws",
            )
        )
    if out.summary is not None:
        for j, name in enumerate(out.summary):
            if not summary_allowed(caps.representation, name):
                issues.append(
                    _issue(
                        ErrorCode.CAPABILITY_UNSUPPORTED,
                        json_pointer("output", "summary", j),
                        f"the server cannot calculate summary {name!r} from "
                        f"{caps.representation.value!r} output",
                        suggested_action=rules.note,
                    )
                )
    if out.kind == "predictive" and not rules.predictive:
        issues.append(
            _issue(
                ErrorCode.CAPABILITY_UNSUPPORTED,
                "/output/kind",
                "predictive output (with observation noise) needs an uncertainty "
                "representation. This model gives point predictions",
            )
        )
    if out.aggregate is not None:
        for j, d in enumerate(out.aggregate.over):
            if model_schema is not None and dataset_to_canon.get(d, d) not in model_schema.dims:
                issues.append(
                    _issue(
                        ErrorCode.COORD_NOT_ALLOWED,
                        json_pointer("output", "aggregate", "over", j),
                        f"{d!r} is not a model dim",
                    )
                )
        verdict = can_aggregate(caps.representation, out.aggregate.over, out.aggregate.how)
        if not verdict.allowed:
            issues.append(
                _issue(
                    ErrorCode.CAPABILITY_UNSUPPORTED,
                    "/output/aggregate",
                    f"cannot aggregate {caps.representation.value!r} output: {verdict.reason}",
                )
            )
        elif verdict.flagged:
            issues.append(
                Issue(
                    code=ErrorCode.CAPABILITY_UNSUPPORTED,
                    severity=Severity.WARNING,
                    pointer="/output/aggregate",
                    message=verdict.reason,
                )
            )
    known_outputs = set(rs.model.outputs) | set(rs.model.derived)
    if known_outputs:
        for j, v in enumerate(out.vars):
            if v not in known_outputs:
                issues.append(
                    _issue(
                        ErrorCode.REF_UNRESOLVED,
                        json_pointer("output", "vars", j),
                        f"the model has no output or derived quantity {v!r}",
                    )
                )
    return issues


def _same_freq(a: DataSchema | None, b: DataSchema | None, dim: str) -> bool:
    """Whether ``dim`` has the same frequency in both schemas (periods compare)."""
    da = a.dims.get(dim) if a is not None else None
    db = b.dims.get(dim) if b is not None else None
    return da is not None and db is not None and da.freq == db.freq


@dataclass(frozen=True, slots=True)
class _Actuals:
    """What the static check learned about a forecast's actuals."""

    through: Scalar | None
    appended: bool
    end: int | None = None
    """Period index of the last actual (on the fit's grid), when known."""


def _check_actuals(
    req: ScoreRequest,
    meta: ScoreMetadata,
    inputs: DataSummary,
    stage_schema: DataSchema,
    model_schema: DataSchema | None,
    canon: Any,
    *,
    supplied: set[str],
    supported: bool,
    pointer: str,
    issues: list[Issue],
    data_space: Literal["transformed"] | None = None,
) -> _Actuals | None:
    """The static rules of :mod:`mapi_spec.actuals` from ``inputs.observed`` (the union over
    series; the sandbox checks every series). ``None`` when the payload has no actuals (or
    the summary does not say)."""
    from mapi_spec.actuals import actuals_not_supported_issue, format_label

    assert req.horizon is not None
    hdim, periods = req.horizon.dim, req.horizon.periods
    targets = set(stage_schema.targets)
    found: list[tuple[str, CoordSummary]] = []
    for raw_var, per_dim in inputs.observed.items():
        var = canon(raw_var)
        cs = next((c for d, c in per_dim.items() if canon(d) == hdim), None)
        if var in targets and cs is not None and cs.size > 0:
            found.append((var, cs))
    if not found:
        return None
    var, obs = max(found, key=lambda vc: vc[1].size)
    fit = meta.fit_coords.get(req.inputs_stage, {}).get(hdim)
    if not supported:
        issues.append(
            actuals_not_supported_issue(
                meta.resolved.model.resolved,
                var=var,
                n=obs.size,
                first=obs.start,
                last=obs.end,
                fit_end=fit.end if fit is not None else None,
                pointer=pointer,
                data_space=data_space,
            )
        )
        return None
    through = format_label(obs.end)
    payload = next((c for d, c in inputs.coords.items() if canon(d) == hdim), None)
    pe = payload.ordinal(payload.end) if payload is not None and payload.end is not None else None
    if fit is None or fit.end is None or obs.start is None or obs.end is None:
        return _Actuals(through, appended=pe is None)
    s, e, fe = obs.ordinal(obs.start), obs.ordinal(obs.end), fit.ordinal(fit.end)
    if s is None or e is None or fe is None:
        return _Actuals(through, appended=pe is None)
    mvar = model_schema.vars.get(var) if model_schema is not None else None
    missing_ok = mvar is not None and mvar.missing == "allow"
    if not missing_ok and s > fe + 1:
        issues.append(
            _issue(
                ErrorCode.ACTUALS_INVALID,
                pointer,
                f"the actuals of {var!r} start {s - fe - 1} period(s) after the end of the "
                f"fitted history ({format_label(fit.end)}). They must continue it",
                suggested_action="Send the target for the periods between them, or fit "
                "the model again with the new data.",
                untrusted_text=f"first actual {obs.start}",
            )
        )
    # only empty periods after the fit are gaps: inside the fitted range an empty target keeps
    # its fitted value. When the actuals straddle the fit's end and have holes, the summary
    # cannot say on which side they are (the sandbox checks every series exactly).
    if not missing_ok and s > fe and (not obs.contiguous or e - s + 1 != obs.size):
        issues.append(
            _issue(
                ErrorCode.ACTUALS_INVALID,
                pointer,
                f"the target {var!r} is empty for {e - s + 1 - obs.size} period(s) between "
                "its first and last actual, and the model needs a complete history",
                suggested_action="Fill the empty periods, or send only the actuals before "
                "the first empty period.",
            )
        )
    if e > fe:
        required = {n for n, v in stage_schema.vars.items() if v.future_required}
        if model_schema is not None:
            required |= {
                n
                for n, v in model_schema.vars.items()
                if v.future_required and n in stage_schema.vars
            }
        lacking = sorted(required - targets - supplied)
        lacking += sorted(
            n
            for n in (required - targets) & supplied
            if _covers(_observed_along(inputs, n, hdim, canon), max(s, fe + 1), e) is False
        )
        if lacking:
            issues.append(
                _issue(
                    ErrorCode.ACTUALS_INVALID,
                    pointer,
                    f"the actuals after the fitted history need the observed values of the "
                    f"drivers {lacking}",
                    suggested_action="Send the drivers for the periods with actuals.",
                )
            )
    future = periods if pe is None or pe <= e else pe - e
    appended = pe is None or pe <= e
    total = max(e - fe, 0) + future
    same = _same_freq(stage_schema, model_schema, hdim)  # else periods do not compare
    dspec = model_schema.dims.get(hdim) if model_schema is not None else None
    if (
        same
        and dspec is not None
        and dspec.max_extension is not None
        and total > dspec.max_extension
    ):
        issues.append(
            _issue(
                ErrorCode.HORIZON_EXCEEDS_FIT,
                "/horizon/periods" if appended else pointer,
                f"the forecast reaches {total} period(s) past the fit along {hdim!r} "
                f"(with the actuals). Without a new fit, the maximum is {dspec.max_extension}",
                suggested_action="Fit the model again with the new data.",
            )
        )
    if not appended and future != periods and same:
        issues.append(
            Issue(
                code=ErrorCode.SCHEMA_INVALID,
                severity=Severity.WARNING,
                pointer="/horizon/periods",
                message=f"the server does not use horizon.periods ({periods}). The payload lists "
                f"{future} period(s) after the last actual ({through}), and the server "
                "forecasts only those periods",
            )
        )
    return _Actuals(through, appended=appended, end=e)


def _observed_along(inputs: DataSummary, var: str, dim: str, canon: Any) -> CoordSummary | None:
    """Where ``var`` holds values along ``dim`` (``inputs.observed``; ``None``: unknown)."""
    for raw, per_dim in inputs.observed.items():
        if canon(raw) == var:
            return next((c for d, c in per_dim.items() if canon(d) == dim), None)
    return None


def _covers(cs: CoordSummary | None, lo: int, hi: int) -> bool | None:
    """Whether the labels ``cs`` summarises include every period ``lo..hi`` (period
    indices); ``None`` when the summary cannot say."""
    if cs is None:
        return None
    if hi < lo:
        return True
    if cs.size == 0:
        return False
    if cs.start is None or cs.end is None:
        return None
    s, e = cs.ordinal(cs.start), cs.ordinal(cs.end)
    if s is None or e is None:
        return None
    if s > lo or e < hi:
        return False
    return True if cs.contiguous else None


def _check_future_rows(
    req: ScoreRequest,
    meta: ScoreMetadata,
    inputs: DataSummary,
    canon: Any,
    required: set[str],
    actuals: _Actuals | None,
    pointer: str,
    issues: list[Issue],
) -> None:
    """Drivers on the periods a forecast payload lists after its last actual (or after the
    fit) must hold values when ``future_covariates`` is ``supplied``."""
    from mapi_spec.actuals import format_label

    assert req.horizon is not None
    hdim = req.horizon.dim
    cs = next((c for d, c in inputs.coords.items() if canon(d) == hdim), None)
    fit = meta.fit_coords.get(req.inputs_stage, {}).get(hdim)
    model_fit = meta.fit_coords.get(meta.resolved.model_stage, {}).get(hdim)
    if cs is None or fit is None or cs.end is None or fit.end is None:
        return
    if (model_fit is not None and model_fit.freq != fit.freq) or (
        cs.kind == "time" and cs.freq != fit.freq
    ):
        return  # another frequency at the input stage; checked in the sandbox
    pe, fe = cs.ordinal(cs.end), fit.ordinal(fit.end)
    if pe is None or fe is None:
        return
    after = fe if actuals is None or actuals.end is None else max(fe, actuals.end)
    empty = sorted(
        n
        for n in required
        if _covers(_observed_along(inputs, n, hdim, canon), after + 1, pe) is False
    )
    if empty:
        freq = parse_freq(fit.freq) if fit.kind == "time" and fit.freq else None
        span = ""
        if freq is not None:
            span = f" ({format_label(freq.label(after + 1).isoformat())} to {format_label(cs.end)})"
        issues.append(
            _issue(
                ErrorCode.MISSING_FUTURE_COVARIATE,
                pointer,
                f"the forecast needs values of {empty} on each period to forecast{span}. "
                "Some of these values are empty",
                suggested_action="Fill the drivers on the periods to forecast, or set "
                "future_covariates (carry_last, seasonal_naive or zero).",
            )
        )


def _check_unused_targets(
    req: ScoreRequest,
    meta: ScoreMetadata,
    inputs: DataSummary,
    stage_schema: DataSchema,
    canon: Any,
    *,
    supported: bool,
    pointer: str,
    issues: list[Issue],
) -> None:
    """``predict`` never reads targets as actuals: say so when the payload has target values
    after the fitted history, instead of ignoring them silently."""
    from mapi_spec.actuals import format_label

    fit_stage = meta.fit_coords.get(req.inputs_stage, {})
    for raw_var, per_dim in inputs.observed.items():
        var = canon(raw_var)
        if var not in stage_schema.targets:
            continue
        for raw_dim, obs in per_dim.items():
            dim = canon(raw_dim)
            spec = stage_schema.dims.get(dim)
            fit = fit_stage.get(dim)
            if spec is None or spec.policy is not DimPolicy.EXTEND or fit is None:
                continue
            if obs.size == 0 or obs.end is None or fit.end is None:
                continue
            e, fe = obs.ordinal(obs.end), fit.ordinal(fit.end)
            if e is None or fe is None or e <= fe:
                continue
            action = (
                "Use mode: forecast to update the forecast with them. The model conditions on "
                "actuals and does not fit again."
                if supported
                else "Leave the target empty in a predict call. To use the new values, fit "
                "the model again with them."
            )
            issues.append(
                Issue(
                    code=ErrorCode.SCHEMA_INVALID,
                    severity=Severity.WARNING,
                    pointer=pointer,
                    message=f"the target {var!r} has values after the fitted history (through "
                    f"{format_label(obs.end)}). The predict mode does not use these values",
                    suggested_action=action,
                )
            )
            return


def _payload_extension(
    req: ScoreRequest,
    meta: ScoreMetadata,
    inputs: DataSummary,
    stage_schema: DataSchema,
    canon: Any,
) -> dict[str, int]:
    """Periods the payload reaches past the fit along each ``extend`` dim of the entry
    stage (positive entries only)."""
    out: dict[str, int] = {}
    fit_stage = meta.fit_coords.get(req.inputs_stage, {})
    for raw_dim, cs in inputs.coords.items():
        dim = canon(raw_dim)
        spec = stage_schema.dims.get(dim)
        fit = fit_stage.get(dim)
        if spec is None or spec.policy is not DimPolicy.EXTEND or fit is None:
            continue
        if cs.end is None or fit.end is None:
            continue
        e, fe = cs.ordinal(cs.end), fit.ordinal(fit.end)
        if e is not None and fe is not None and e > fe:
            out[dim] = e - fe
    return out


def _model_view(rs: ResolvedSpec) -> DataSchema | None:
    """Final-stage schema overlaid with the model's input schema (effective policies)."""
    final = rs.stages.get(rs.model_stage)
    inp = rs.model.input_schema
    if final is None or inp is None:
        return inp if final is None else final
    return final.model_copy(
        update={"dims": {**final.dims, **inp.dims}, "vars": {**final.vars, **inp.vars}}
    )


def _check_coord(
    dim: str,
    cs: CoordSummary,
    fit: CoordSummary,
    policy: DimPolicy,
    strategy: str,
    max_ext: int | None,
    ptr: str,
    mode: str,
) -> list[Issue]:
    issues: list[Issue] = []
    if policy is not DimPolicy.EXTEND:
        new_vals = cs.level_set
        fit_vals = fit.level_set
        unseen: set[Scalar] | None = None
        if new_vals is not None and fit_vals is not None:
            unseen = new_vals - fit_vals
        elif cs.start is not None and fit.start is not None and cs.end is not None:
            lo, hi = cs.ordinal(cs.start), cs.ordinal(cs.end)
            flo, fhi = fit.ordinal(fit.start), fit.ordinal(fit.end) if fit.end is not None else None
            if None not in (lo, hi, flo, fhi):
                assert lo is not None and hi is not None and flo is not None and fhi is not None
                unseen = {cs.start} if lo < flo else ({cs.end} if hi > fhi else set())
        if not unseen:
            return issues
        if policy is DimPolicy.FIXED or strategy == "error":
            issues.append(
                _issue(
                    ErrorCode.UNSEEN_LEVELS,
                    ptr,
                    f"the fit did not see {len(unseen)} level(s) of {dim!r}, and the policy of "
                    f"the dim is {policy.value!r}"
                    + ("" if policy is DimPolicy.FIXED else " with strategy 'error'"),
                    untrusted_text=_preview(unseen),
                )
            )
        else:
            issues.append(
                Issue(
                    code=ErrorCode.UNSEEN_LEVELS,
                    severity=Severity.INFO,
                    pointer=ptr,
                    message=f"{len(unseen)} new level(s) of {dim!r} will use strategy {strategy!r}",
                    untrusted_text=_preview(unseen),
                )
            )
        return issues

    # EXTEND
    if cs.start is None or cs.end is None or fit.start is None or fit.end is None:
        return issues
    # the payload is placed on the fit's grid: a payload with one label has no step (or
    # frequency) of its own, so its own summary would count periods of 1
    s, e = fit.ordinal(cs.start), fit.ordinal(cs.end)
    fs, fe = fit.ordinal(fit.start), fit.ordinal(fit.end)
    if None in (s, e, fs, fe):
        issues.append(
            Issue(
                code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
                severity=Severity.ERROR if None in (s, e) else Severity.WARNING,
                pointer=ptr,
                message=f"the coords of {dim!r} do not align with its frequency or step. The "
                "server cannot do a check for gaps",
            )
        )
        return issues
    assert s is not None and e is not None and fs is not None and fe is not None
    if s < fs:
        issues.append(
            _issue(
                ErrorCode.COORD_NOT_ALLOWED,
                ptr,
                f"input coords of {dim!r} start before the fitted range",
                untrusted_text=f"{cs.start} < {fit.start}",
            )
        )
    if e > fe:
        if s > fe + 1:
            issues.append(
                _issue(
                    ErrorCode.NON_CONTIGUOUS_EXTENSION,
                    ptr,
                    f"input coords of {dim!r} leave a gap of {s - fe - 1} period(s) after the "
                    "fitted range",
                    untrusted_text=f"fit ends {fit.end}; input starts {cs.start}",
                )
            )
        if not cs.contiguous:
            issues.append(
                _issue(
                    ErrorCode.NON_CONTIGUOUS_EXTENSION,
                    ptr,
                    f"input coords of {dim!r} have gaps",
                )
            )
        if max_ext is not None and e - fe > max_ext:
            issues.append(
                _issue(
                    ErrorCode.HORIZON_EXCEEDS_FIT,
                    ptr,
                    f"the input goes {e - fe} period(s) after the fit along {dim!r}. Without a "
                    f"new fit, the maximum is {max_ext}",
                )
            )
    elif mode == "forecast" and e <= fe:
        issues.append(
            Issue(
                code=ErrorCode.NON_CONTIGUOUS_EXTENSION,
                severity=Severity.WARNING,
                pointer=ptr,
                message=f"forecast input along {dim!r} does not extend past the fitted range",
            )
        )
    return issues


def _check_horizon_coverage(
    req: ScoreRequest,
    meta: ScoreMetadata,
    inputs: DataSummary,
    canon: Any,
    inputs_ptr: str,
    issues: list[Issue],
) -> None:
    assert req.horizon is not None
    hdim = req.horizon.dim
    cs = next((c for d, c in inputs.coords.items() if canon(d) == hdim), None)
    fit = meta.fit_coords.get(req.inputs_stage, {}).get(hdim)
    model_fit = meta.fit_coords.get(meta.resolved.model_stage, {}).get(hdim)
    if cs is None or fit is None or cs.end is None or fit.end is None:
        return
    if model_fit is not None and model_fit.freq != fit.freq:
        return  # different frequency at the input stage; checked in the sandbox
    e, fe = cs.ordinal(cs.end), fit.ordinal(fit.end)
    if e is None or fe is None:
        return
    covered = e - fe
    if covered < req.horizon.periods:
        issues.append(
            _issue(
                ErrorCode.MISSING_FUTURE_COVARIATE,
                inputs_ptr,
                f"future covariates cover {max(covered, 0)} of the {req.horizon.periods} "
                f"horizon periods along {hdim!r}",
            )
        )
