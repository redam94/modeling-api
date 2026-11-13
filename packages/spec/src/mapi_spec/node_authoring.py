"""Builders for workflow node contracts: the helpers the built-in nodes use, made public (v0.18).

A plugin exports a node as ``NodeExport(contract=node(...), fn=...)`` from the
``mapi.workflow_nodes`` entry-point group (see :mod:`mapi_workflows.kit`). The contract is
plain JSON data, so the control plane validates workflows against it without importing the
plugin::

    from mapi_spec.node_authoring import about, node, num

    ZERO_SHARE = node(
        "test.acme_zero_share",
        "test",
        about(
            "Share of zeros",
            "Counts how many values of the target are zero.",
            use=["Before a count model"],
            avoid=["The data has no zeros by design"],
            needs=["A numeric target"],
            assumes=["Zeros are real values, not missing values"],
            outputs=["The share of zeros and a verdict"],
            difficulty="beginner",
            runtime="instant",
            example="In weekly store sales, 12% of the weeks have zero sales.",
        ),
        plugin="acme",
        params={"threshold": num("The largest share that passes", 0.2, 0, 1)},
        outputs={"result": "test_result"},
        fields={"share": ("number", "The share of zeros")},
    )

Plugin node ids keep two segments: ``<kind>.<namespace>_<name>``, with ``plugin`` naming the
namespace. Every function here is pure and returns new objects.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from mapi_spec.about import About, AboutReference, Difficulty, TypicalRuntime

if TYPE_CHECKING:  # the models live in workflow_nodes, which builds its nodes with these helpers
    from mapi_spec.workflow_nodes import (
        NodeImplementation,
        NodeKind,
        PortSpec,
        PortType,
        ResultField,
        ResultType,
        RunsIn,
    )

__all__ = [
    "NAME_PATTERN",
    "about",
    "array",
    "boolean",
    "enum",
    "integer",
    "node",
    "num",
    "port",
    "schema",
    "string",
]

NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_.\-]{0,127}$"
"""The pattern of a variable or dim name (the ``mapi_spec`` ``Name`` type)."""


def about(
    title: str,
    summary: str,
    use: Sequence[str],
    avoid: Sequence[str],
    needs: Sequence[str],
    assumes: Sequence[str],
    outputs: Sequence[str],
    difficulty: Difficulty | str,
    runtime: TypicalRuntime | str,
    example: str,
    *,
    related: Sequence[str] = (),
    expert: str | None = None,
    refs: Sequence[tuple[str, str | None]] = (),
    fits: Mapping[str, Sequence[str]] | None = None,
) -> About:
    """An About card (every field the catalog rule requires, in plain language)."""
    return About(
        title=title, summary_plain=summary, use_when=list(use), avoid_when=list(avoid),
        data_needs=list(needs), assumptions=list(assumes), outputs=list(outputs),
        difficulty=Difficulty(difficulty), typical_runtime=TypicalRuntime(runtime),
        example=example, related=list(related), expert_notes=expert,
        references=[AboutReference(title=t, url=u) for t, u in refs],
        fits={k: list(v) for k, v in (fits or {}).items()},
    )  # fmt: skip


def schema(props: Mapping[str, dict[str, Any]], required: Sequence[str] = ()) -> dict[str, Any]:
    """A closed object schema (``additionalProperties: false``) of the node's parameters."""
    out: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(props),
    }
    if required:
        out["required"] = list(required)
    return out


def num(desc: str, default: float | None = None, lo: float | None = None,
        hi: float | None = None, *, exclusive: bool = False) -> dict[str, Any]:  # fmt: skip
    """A number parameter (``exclusive``: the bounds themselves are not allowed)."""
    s: dict[str, Any] = {"type": "number", "description": desc}
    if default is not None:
        s["default"] = default
    if lo is not None:
        s["exclusiveMinimum" if exclusive else "minimum"] = lo
    if hi is not None:
        s["exclusiveMaximum" if exclusive else "maximum"] = hi
    return s


def integer(desc: str, default: int | None = None, lo: int | None = 0,
            hi: int | None = None) -> dict[str, Any]:  # fmt: skip
    """An integer parameter (at least 0 unless ``lo`` says otherwise)."""
    s: dict[str, Any] = {"type": "integer", "description": desc}
    if default is not None:
        s["default"] = default
    if lo is not None:
        s["minimum"] = lo
    if hi is not None:
        s["maximum"] = hi
    return s


def enum(desc: str, values: Sequence[str], default: str | None = None) -> dict[str, Any]:
    """A parameter that takes one of ``values``."""
    s: dict[str, Any] = {"type": "string", "enum": list(values), "description": desc}
    if default is not None:
        s["default"] = default
    return s


def boolean(desc: str, default: bool) -> dict[str, Any]:
    """A yes/no parameter."""
    return {"type": "boolean", "description": desc, "default": default}


def string(
    desc: str,
    default: str | None = None,
    *,
    max_length: int | None = 200,
    pattern: str | None = None,
) -> dict[str, Any]:
    """A text parameter. A ``pattern`` must pass :func:`mapi_spec.schema_safety.is_safe_regex`
    (the contract kit checks it); :data:`NAME_PATTERN` is the one for variable names."""
    s: dict[str, Any] = {"type": "string", "description": desc}
    if pattern is not None:
        s["pattern"] = pattern
    if max_length is not None:
        s["maxLength"] = max_length
    if default is not None:
        s["default"] = default
    return s


def array(
    desc: str,
    items: dict[str, Any],
    *,
    min_items: int | None = None,
    max_items: int | None = 256,
) -> dict[str, Any]:
    """A list parameter whose items follow ``items``."""
    s: dict[str, Any] = {"type": "array", "items": items}
    if min_items is not None:
        s["minItems"] = min_items
    if max_items is not None:
        s["maxItems"] = max_items
    s["description"] = desc
    return s


def port(
    type: PortType | str,
    description: str = "",
    *,
    required: bool = True,
    multiple: bool = False,
) -> PortSpec:
    """An input port of a node."""
    from mapi_spec.workflow_nodes import PortSpec, PortType

    return PortSpec(
        type=PortType(type), required=required, multiple=multiple, description=description
    )


def node(
    id: str,
    kind: NodeKind | str,
    about: About,
    *,
    plugin: str | None = None,
    version: str = "1.0.0",
    params: Mapping[str, dict[str, Any]] | None = None,
    required: Sequence[str] = (),
    schema: dict[str, Any] | None = None,
    inputs: Mapping[str, PortSpec] | None = None,
    outputs: Mapping[str, PortType | str] | None = None,
    fields: Mapping[str, ResultField | tuple[ResultType, str]] | None = None,
    runs_in: RunsIn = "sandbox",
    cacheable: bool = True,
) -> NodeImplementation:
    """A node contract.

    ``params`` (with ``required``) builds a closed parameter schema; pass ``schema`` instead
    for a hand-written one. ``inputs`` defaults to one required dataset port ``data``.
    ``fields`` are the result fields branch conditions may read, as :class:`ResultField` or
    ``(type, description)`` pairs.
    """
    from mapi_spec.workflow_nodes import (
        NodeImplementation,
        NodeKind,
        PortSpec,
        PortType,
        ResultField,
    )

    default_inputs = {"data": PortSpec(type=PortType.DATASET, description="The data to use")}
    result_fields = {
        name: f if isinstance(f, ResultField) else ResultField(type=f[0], description=f[1])
        for name, f in (fields or {}).items()
    }
    params_schema = schema if schema is not None else _schema_of(params or {}, required)
    return NodeImplementation(
        id=id,
        version=version,
        kind=NodeKind(kind),
        inputs=dict(inputs if inputs is not None else default_inputs),
        outputs={k: PortType(v) for k, v in (outputs or {}).items()},
        params_schema=params_schema,
        result_fields=result_fields,
        runs_in=runs_in,
        cacheable=cacheable,
        about=about,
        plugin=plugin,
    )


_schema_of = schema  # ``node`` has a ``schema`` parameter
