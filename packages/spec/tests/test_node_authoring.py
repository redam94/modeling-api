"""v0.18 node authoring: the public builders, the plugin node rules, and the built-in node
contracts unchanged by the helper refactor."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from mapi_spec import node_authoring as na
from mapi_spec.about import About
from mapi_spec.errors import ErrorCode
from mapi_spec.schema_safety import is_safe_regex
from mapi_spec.workflow_nodes import (
    NODE_IMPLEMENTATIONS,
    NODE_KIND_ABOUTS,
    PLUGIN_NODE_KINDS,
    NodeImplementation,
    NodeKind,
    PortType,
    plugin_contract_issues,
)
from pydantic import ValidationError

# sha256 of the sorted, compact JSON of every built-in contract (and of the kind cards),
# recorded before the helpers moved to mapi_spec.node_authoring: the move changed nothing
BUILTIN_NODES_SHA256 = "13dc502dde4e9f4f535ab1e343fb4d5bde3f5b3e610236857a920ca01fd825aa"
KIND_ABOUTS_SHA256 = "ba963ef06cd868cd9ea3adba013ab9afe8570242419e7bcb2cb3dfeeba00a1a4"


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_builtin_contracts_are_byte_identical_after_the_refactor() -> None:
    dumped = [n.model_dump(mode="json") for n in NODE_IMPLEMENTATIONS.values()]
    assert len(dumped) == 57
    assert _sha(dumped) == BUILTIN_NODES_SHA256
    assert _sha({k.value: v.model_dump(mode="json") for k, v in NODE_KIND_ABOUTS.items()}) == (
        KIND_ABOUTS_SHA256
    )
    assert all(n.plugin is None and "plugin" not in n.model_dump() for n in dumped_nodes())


def dumped_nodes() -> list[NodeImplementation]:
    return list(NODE_IMPLEMENTATIONS.values())


def card(title: str = "Share of zeros") -> About:
    return na.about(
        title,
        "Counts how many values of the target are zero, overall and per series.",
        use=["Before you choose a count model"],
        avoid=["The data has no zeros by design"],
        needs=["A numeric target variable"],
        assumes=["Zeros are real values, not missing values"],
        outputs=["The share of zeros and a pass or fail verdict"],
        difficulty="beginner",
        runtime="instant",
        example="In weekly store sales, 12% of the weeks have zero sales.",
    )


def zero_share(**kw: Any) -> NodeImplementation:
    base: dict[str, Any] = {
        "plugin": "acme",
        "params": {"threshold": na.num("The largest share that passes", 0.2, 0, 1)},
        "outputs": {"result": "test_result"},
        "fields": {"share": ("number", "The share of zeros")},
    }
    base.update(kw)
    return na.node("test.acme_zero_share", "test", card(), **base)


def test_builders_make_the_same_shapes_as_the_builtins() -> None:
    assert na.schema({"a": na.integer("A count", 3)}, ["a"]) == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "integer", "description": "A count", "default": 3,
                             "minimum": 0}},
        "required": ["a"],
    }  # fmt: skip
    assert na.num("x", lo=0, hi=1, exclusive=True) == {
        "type": "number", "description": "x", "exclusiveMinimum": 0, "exclusiveMaximum": 1,
    }  # fmt: skip
    assert na.enum("m", ["a", "b"], "a") == {
        "type": "string", "enum": ["a", "b"], "description": "m", "default": "a",
    }  # fmt: skip
    assert na.boolean("b", True) == {"type": "boolean", "description": "b", "default": True}
    assert na.string("s", pattern=na.NAME_PATTERN)["pattern"] == na.NAME_PATTERN
    assert is_safe_regex(na.NAME_PATTERN) is None
    assert na.array("v", na.string("item"), min_items=1) == {
        "type": "array", "items": {"type": "string", "description": "item", "maxLength": 200},
        "minItems": 1, "maxItems": 256, "description": "v",
    }  # fmt: skip
    p = na.port("model_version", "The fitted model", multiple=True)
    assert p.type is PortType.MODEL_VERSION and p.multiple


def test_a_plugin_node_contract_is_clean() -> None:
    c = zero_share()
    assert c.plugin == "acme" and c.kind is NodeKind.TEST
    assert c.inputs["data"].type is PortType.DATASET
    assert c.result_fields["share"].type == "number"
    assert plugin_contract_issues(c, distribution="acme-lab") == []
    assert c.model_dump(mode="json")["plugin"] == "acme"


def test_plugin_ids_keep_two_segments() -> None:
    with pytest.raises(ValidationError, match="acme_<name>"):
        na.node("test.zero_share", "test", card(), plugin="acme")
    with pytest.raises(ValidationError, match="acme_<name>"):
        na.node("test.acme_", "test", card(), plugin="acme")
    with pytest.raises(ValidationError):  # three segments never match the id pattern
        na.node("test.acme.zero", "test", card(), plugin="acme")
    with pytest.raises(ValidationError, match="reserved"):
        na.node("test.lib_zero", "test", card(), plugin="lib")
    with pytest.raises(ValidationError, match="uploaded plugins"):
        na.node("test.t_acme_zero", "test", card(), plugin="t_acme")
    with pytest.raises(ValidationError):  # the prefix is still the kind
        na.node("report.acme_zero", "test", card(), plugin="acme")


@pytest.mark.parametrize(
    ("change", "pointer", "code"),
    [
        ({"plugin": None}, "/plugin", ErrorCode.WORKFLOW_INVALID),
        ({"runs_in": "orchestrator"}, "/runs_in", ErrorCode.WORKFLOW_INVALID),
        ({"outputs": {}}, "/outputs", ErrorCode.WORKFLOW_INVALID),
        ({"outputs": {"Result": "test_result"}}, "/outputs/Result", ErrorCode.WORKFLOW_INVALID),
        ({"schema": {"type": "object", "properties": {"x": {"type": "string",
                                                            "pattern": "(a+)+"}}}},
         "/params_schema/properties/x/pattern", ErrorCode.SCHEMA_UNSAFE),
        ({"schema": {"type": "object", "properties": {"x": {"$ref": "https://e.x/s"}}}},
         "/params_schema/properties/x/$ref", ErrorCode.SCHEMA_UNSAFE),
    ],
)  # fmt: skip
def test_plugin_contract_issues(change: dict[str, Any], pointer: str, code: ErrorCode) -> None:
    plugin = change.pop("plugin", "acme")
    kw: dict[str, Any] = {
        "params": {"threshold": na.num("x", 0.2, 0, 1)},
        "outputs": {"result": "test_result"},
        **change,
    }
    c = na.node("test.acme_zero_share", "test", card(), plugin=plugin, **kw)
    issues = plugin_contract_issues(c, distribution="acme-lab")
    assert any(i.pointer == pointer and i.code is code for i in issues), [
        (i.pointer, i.message) for i in issues
    ]


@pytest.mark.parametrize("kind", ["branch", "compare", "select", "model", "subworkflow"])
def test_control_plane_and_child_run_kinds_are_refused(kind: str) -> None:
    assert NodeKind(kind) not in PLUGIN_NODE_KINDS
    c = na.node(f"{kind}.acme_x", kind, card(), plugin="acme", outputs={"result": "table"})
    issues = plugin_contract_issues(c, distribution="acme-lab")
    assert [i.pointer for i in issues] == ["/kind"]


def test_namespace_ownership_and_shadowing() -> None:
    sci = na.node("test.sci_zero", "test", card(), plugin="sci", outputs={"result": "table"})
    assert plugin_contract_issues(sci, distribution="mapi-plugin-science") == []
    issues = plugin_contract_issues(sci, distribution="acme-lab")
    assert [i.pointer for i in issues] == ["/plugin"]
    assert "built-in plugins" in issues[0].message
    builtin = NODE_IMPLEMENTATIONS["test.adf"]
    issues = plugin_contract_issues(builtin, distribution="acme-lab")
    assert {"/plugin", "/id"} <= {i.pointer for i in issues}


def test_an_incomplete_about_is_reported() -> None:
    thin = card().model_copy(update={"summary_plain": "Counts zeros. Per series. Overall too."})
    c = zero_share().model_copy(update={"about": thin})
    issues = plugin_contract_issues(c, distribution="acme-lab")
    assert {i.code for i in issues} == {ErrorCode.ABOUT_INCOMPLETE}
    assert all(i.message.startswith("test.acme_zero_share: ") for i in issues)
