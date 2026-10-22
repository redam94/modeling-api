"""Prep presets (v0.18, D4): saved preprocessing steps with slots, filled at use time.

A :class:`~mapi_spec.library.PrepPresetDoc` is a list of ordinary ``StepCall`` whose
``with`` values may be ``"$slot:<name>"`` placeholders. :func:`apply_prep_preset` fills them
with the user's choices (the app pre-fills them from the dataset's roles), prefixes the step
ids so the steps stay unique inside a spec, and checks the result:

* every slot value has the right shape (one variable, a list of variables, one dim) and, with
  a dataset schema, names a variable or dim of the dataset;
* with a catalog, every step exists and its settings validate against the step's schema;
* with both, the steps' static schema effects are propagated (the effect chain is checked);
* ``core.formula`` steps get caps-only checks (length, printable ASCII). The formula grammar
  is parsed in the sandbox at fit, never in the control plane (no second grammar to drift).

Pure and JSON-only: no step code runs here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from pydantic import JsonValue, ValidationError

from mapi_spec._types import NAME_RE, STEP_ID_RE
from mapi_spec.catalog import Catalog, StepCatalogEntry
from mapi_spec.effects import EffectInput, propagate
from mapi_spec.errors import ErrorCode, Issue, Severity, join_pointer, json_pointer
from mapi_spec.library import PrepPresetDoc, Slot
from mapi_spec.schema import DataSchema
from mapi_spec.spec import StepCall
from mapi_spec.validate import apply_defaults, validate_settings

__all__ = [
    "FORMULA_STEP_ID",
    "MAX_EXPR_CHARS",
    "SLOT_PREFIX",
    "apply_prep_preset",
    "fill_slots",
    "slot_names",
]

SLOT_PREFIX: Final = "$slot:"
FORMULA_STEP_ID: Final = "core.formula"
MAX_EXPR_CHARS: Final = 4000
"""Longest ``core.formula`` expression (mirrors ``mapi_steps.formula.MAX_EXPR_CHARS``)."""
MAX_VAR_LIST: Final = 256

_NAME = re.compile(NAME_RE)
_STEP_ID = re.compile(STEP_ID_RE)
_PREFIX = re.compile(r"^[a-z_][a-z0-9_]{0,30}$")
_PRINTABLE = re.compile(r"^[\x20-\x7e\t\n\r]*$")
_DROP: Final = object()


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


def slot_names(value: Any) -> set[str]:
    """Every slot name referenced (as a whole value or a list element) inside ``value``."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str) and node.startswith(SLOT_PREFIX):
            found.add(node[len(SLOT_PREFIX) :])
        elif isinstance(node, Mapping):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(value)
    return found


def _check_value(
    name: str, slot: Slot, value: Any, schema: DataSchema | None, issues: list[Issue]
) -> JsonValue | None:
    """The normalised slot value (``var_list`` always a list), or ``None`` when wrong."""
    ptr = json_pointer("slots", name)
    names: list[str]
    if slot.kind == "var_list":
        raw = [value] if isinstance(value, str) else value
        if not isinstance(raw, list) or not raw or len(raw) > MAX_VAR_LIST:
            issues.append(
                _issue(ptr, f"Slot {name!r} needs a list of 1 to {MAX_VAR_LIST} variable names.")
            )
            return None
        names = raw
    else:
        if not isinstance(value, str):
            what = "a variable name" if slot.kind == "var" else "a dim name"
            issues.append(_issue(ptr, f"Slot {name!r} needs {what}."))
            return None
        names = [value]
    bad = [n for n in names if not isinstance(n, str) or not _NAME.match(n)]
    if bad:
        issues.append(_issue(ptr, f"Slot {name!r} has a value that is not a valid name."))
        return None
    if len(set(names)) != len(names):
        issues.append(_issue(ptr, f"Slot {name!r} names a variable more than once."))
        return None
    if schema is not None:
        if slot.kind == "dim":
            if names[0] not in schema.dims:
                issues.append(_issue(ptr, f"The dataset has no dim {names[0]!r}."))
                return None
        else:
            missing = [n for n in names if n not in schema.vars]
            if missing:
                issues.append(_issue(ptr, f"The dataset has no variable {missing[0]!r}."))
                return None
            if slot.role is not None:
                other = [n for n in names if schema.vars[n].role is not slot.role]
                if other:
                    issues.append(
                        _issue(
                            ptr,
                            f"Variable {other[0]!r} does not have the role {slot.role.value!r} "
                            "that the slot expects.",
                            severity=Severity.WARNING,
                        )
                    )
    return list(names) if slot.kind == "var_list" else names[0]


def fill_slots(
    value: JsonValue,
    filled: Mapping[str, JsonValue | None],
    slots: Mapping[str, Slot],
    pointer: str,
    issues: list[Issue],
) -> JsonValue:
    """Replace ``"$slot:<name>"`` placeholders in ``value``.

    ``filled`` maps a slot name to its value (``None``: an optional slot left empty, so the
    placeholder is removed: a whole value drops its key, a list element is left out). A
    ``var_list`` value is spliced into a list.
    """

    def fill(node: Any, ptr: str) -> Any:
        if isinstance(node, str):
            if node.startswith(SLOT_PREFIX):
                name = node[len(SLOT_PREFIX) :]
                if name not in slots:
                    issues.append(_issue(ptr, f"The preset has no slot {name!r}."))
                    return node
                v = filled.get(name)
                return _DROP if v is None else v
            if SLOT_PREFIX in node:
                issues.append(_issue(ptr, "A slot must be the whole value, not a part of a text."))
            return node
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for k, v in node.items():
                got = fill(v, join_pointer(ptr, k))
                if got is not _DROP:
                    out[k] = got
            return out
        if isinstance(node, list):
            items: list[Any] = []
            for i, v in enumerate(node):
                got = fill(v, join_pointer(ptr, i))
                if got is _DROP:
                    continue
                is_list_slot = isinstance(v, str) and v.startswith(SLOT_PREFIX)
                if is_list_slot and isinstance(got, list):
                    items.extend(got)
                else:
                    items.append(got)
            return items
        return node

    out = fill(value, pointer)
    return None if out is _DROP else out


def _formula_issues(settings: Mapping[str, Any], ptr: str) -> list[Issue]:
    expr = settings.get("expr")
    if not isinstance(expr, str):
        return []
    if len(expr) > MAX_EXPR_CHARS:
        return [
            _issue(
                join_pointer(ptr, "expr"),
                f"The formula is longer than {MAX_EXPR_CHARS} characters.",
            )
        ]
    if not _PRINTABLE.match(expr):
        return [
            _issue(
                join_pointer(ptr, "expr"),
                "The formula can contain only printable ASCII characters.",
            )
        ]
    return []


def apply_prep_preset(
    doc: PrepPresetDoc,
    slots: Mapping[str, JsonValue],
    *,
    schema: DataSchema | None,
    id_prefix: str = "p",
    catalog: Catalog | None = None,
) -> tuple[list[StepCall], list[Issue]]:
    """Fill a preset's slots and return its steps (ids ``<id_prefix>_<id>``) and issues.

    Issue pointers point into the preset document (``/slots/media``,
    ``/steps/2/with/over``). ``schema`` is the schema the preset's first step sees (the
    bound raw stage, or the stage after earlier steps); ``catalog`` enables the step,
    settings and effect-chain checks. Steps are returned even when there are errors (best
    effort, for display); callers check :func:`~mapi_spec.errors.has_errors`.
    """
    if not _PREFIX.match(id_prefix):
        raise ValueError(f"invalid step id prefix {id_prefix!r}")
    issues: list[Issue] = []

    # 1. slot values --------------------------------------------------------------------
    for name in slots:
        if name not in doc.slots:
            issues.append(_issue(json_pointer("slots", name), f"The preset has no slot {name!r}."))
    filled: dict[str, JsonValue | None] = {}
    for name, slot in doc.slots.items():
        value = slots.get(name)
        if value is None:
            if slot.required:
                issues.append(
                    _issue(
                        json_pointer("slots", name),
                        f"Slot {name!r} ({slot.label}) needs a value.",
                        action="Choose a value for the slot.",
                    )
                )
            filled[name] = None
            continue
        filled[name] = _check_value(name, slot, value, schema, issues)

    used = set().union(*(slot_names(s.with_) for s in doc.steps))
    for name in doc.slots:
        if name not in used:
            issues.append(
                _issue(
                    json_pointer("slots", name),
                    f"No step uses slot {name!r}.",
                    severity=Severity.WARNING,
                )
            )

    # 2. steps --------------------------------------------------------------------------
    steps: list[StepCall] = []
    effects: list[tuple[str, EffectInput, Mapping[str, Any]]] = []
    pointers: list[str] = []
    for i, call in enumerate(doc.steps):
        base = json_pointer("steps", i)
        with_ = fill_slots(call.with_, filled, doc.slots, base + "/with", issues)
        settings: dict[str, JsonValue] = dict(with_) if isinstance(with_, dict) else {}
        new_id = f"{id_prefix}_{call.id}"
        if not _STEP_ID.match(new_id):
            issues.append(_issue(base + "/id", f"The step id {call.id!r} is too long to prefix."))
            new_id = new_id[:63]
        if call.use.id == FORMULA_STEP_ID:
            issues.extend(_formula_issues(settings, base + "/with"))
        entry: StepCatalogEntry | None = None
        if catalog is not None:
            entry = catalog.resolve_step(call.use)
            if entry is None:
                issues.append(
                    _issue(
                        base + "/use",
                        f"The step {str(call.use)!r} is not in the current catalog.",
                        code=ErrorCode.EXTENSION_UNAVAILABLE,
                    )
                )
            else:
                issues.extend(
                    i.model_copy(update={"code": ErrorCode.LIBRARY_INVALID})
                    for i in validate_settings(entry.settings_schema, settings, base + "/with")
                )
        try:
            steps.append(StepCall.model_validate({"id": new_id, "use": call.use, "with": settings}))
        except ValidationError as exc:  # pragma: no cover - the inputs were valid StepCalls
            issues.append(_issue(base, f"The step cannot be built: {exc.errors()[0]['msg']}"))
            continue
        if entry is not None:
            effects.append((new_id, entry.effect, apply_defaults(entry.settings_schema, settings)))
            pointers.append(base)

    # 3. the effect chain ---------------------------------------------------------------
    if schema is not None and catalog is not None and len(effects) == len(doc.steps):
        issues.extend(propagate(schema, effects, pointers=pointers).issues)
    return steps, issues
