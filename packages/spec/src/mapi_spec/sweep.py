"""Expand wrapper documents into child ModelSpecs (pure JSON manipulation)."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import JsonValue, ValidationError

from mapi_spec.errors import ErrorCode, Issue, ValidationFailed, json_pointer, parse_pointer
from mapi_spec.loader import validation_issues
from mapi_spec.spec import ModelSpec, Sweep

__all__ = ["expand_sweep", "set_pointer"]


def set_pointer(doc: Any, pointer: str, value: JsonValue) -> Any:
    """Return a copy of ``doc`` with ``value`` written at RFC 6901 ``pointer``.

    Missing intermediate mapping keys are created; list indices must exist (``-`` appends).
    Raises ``KeyError``/``IndexError``/``TypeError`` when the path cannot be written.
    """
    parts = parse_pointer(pointer)
    if not parts:
        return copy.deepcopy(value)
    out = copy.deepcopy(doc)
    node = out
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(node, dict):
            if last:
                node[part] = copy.deepcopy(value)
            else:
                node = node.setdefault(part, {})
        elif isinstance(node, list):
            if part == "-" and last:
                node.append(copy.deepcopy(value))
                break
            if not part.isdigit() or int(part) >= len(node):
                raise IndexError(f"list index {part!r} out of range")
            if last:
                node[int(part)] = copy.deepcopy(value)
            else:
                node = node[int(part)]
        else:
            raise TypeError(f"cannot descend into a scalar at {part!r}")
    return out


def expand_sweep(sweep: Sweep, base: ModelSpec) -> list[ModelSpec]:
    """Every grid point applied to ``base`` (in :meth:`Sweep.points` order).

    The child ``metadata.name`` gets a ``-<index>`` suffix. Raises
    :class:`~mapi_spec.errors.ValidationFailed` with pointers into ``/grid`` when a point
    cannot be applied or yields an invalid spec.
    """
    base_doc = base.model_dump(mode="json", by_alias=True)
    children: list[ModelSpec] = []
    issues: list[Issue] = []
    for idx, point in enumerate(sweep.points()):
        doc = base_doc
        failed = False
        for ptr, value in point.items():
            try:
                doc = set_pointer(doc, ptr, value)
            except (KeyError, IndexError, TypeError) as exc:
                issues.append(
                    Issue(
                        code=ErrorCode.SCHEMA_INVALID,
                        pointer=json_pointer("grid", ptr),
                        message=f"cannot apply grid path: {exc}",
                    )
                )
                failed = True
        if failed:
            continue
        suffix = f"-{idx}"
        name = base.metadata.name[: 128 - len(suffix)] + suffix
        doc = set_pointer(doc, "/metadata/name", name)
        try:
            children.append(ModelSpec.model_validate(doc))
        except ValidationError as exc:
            for issue in validation_issues(exc, doc):
                issues.append(
                    issue.model_copy(
                        update={
                            "pointer": _grid_pointer(issue.pointer, point),
                            "context": {"point": idx},
                        }
                    )
                )
    if issues:
        raise ValidationFailed(issues)
    return children


def _grid_pointer(pointer: str, point: dict[str, JsonValue]) -> str:
    for ptr in point:
        if pointer == ptr or pointer.startswith(ptr + "/"):
            return json_pointer("grid", ptr)
    return pointer
