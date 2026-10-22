"""v0.18 id namespaces: one source of truth for who may register where."""

from __future__ import annotations

import pytest
from mapi_spec import namespaces as ns
from mapi_spec.namespaces import (
    BUILTIN_DISTRIBUTIONS,
    BUILTIN_NAMESPACES,
    PLATFORM_RESERVED,
    check_namespace,
    is_builtin_distribution,
    namespace_of,
    reserved_namespace,
)


def test_the_sets_are_as_decided() -> None:
    assert {
        "core", "mmm", "ml", "sklearn", "ts", "tabular", "choice", "causal",
        "sci", "dl", "net", "toy", "demo", "examples",
    } == BUILTIN_NAMESPACES  # fmt: skip
    assert {
        "mapi", "platform", "builtin", "team", "library", "lib", "plugin", "t", "tenant",
    } == PLATFORM_RESERVED  # fmt: skip
    assert ns.RESERVED_PREFIXES == ("t_", "tenant_")
    # the distributions that own sci.*, dl.* and net.* are built in (reserving a namespace
    # without its owner would refuse the platform's own entry points)
    assert {"mapi-plugin-science", "mapi-jax", "mapi-workflows", "mapi-plots"} <= (
        BUILTIN_DISTRIBUTIONS
    )
    assert not BUILTIN_NAMESPACES & PLATFORM_RESERVED


@pytest.mark.parametrize(
    ("id_", "plugin", "want"),
    [
        ("acme.store_mean", None, "acme"),
        ("core.log1p", None, "core"),
        ("test.acme_zero_share", "acme", "acme"),  # a node: the contract names it
        ("acme_weekly_check", "acme", "acme"),  # a template
        ("nodot", None, None),
        (".x", None, None),
    ],
)
def test_namespace_of(id_: str, plugin: str | None, want: str | None) -> None:
    assert namespace_of(id_, plugin=plugin) == want


@pytest.mark.parametrize("name", sorted(BUILTIN_NAMESPACES | PLATFORM_RESERVED))
def test_reserved_names_have_a_plain_reason(name: str) -> None:
    reason = reserved_namespace(name)
    assert reason is not None and name in reason and reason.endswith(".")


@pytest.mark.parametrize("name", ["t_acme_x", "tenant_acme", "t_"])
def test_upload_prefixes_are_reserved(name: str) -> None:
    reason = reserved_namespace(name)
    assert reason is not None and "uploaded plugins" in reason


@pytest.mark.parametrize("name", ["acme", "acme_lab", "tx", "tenantx", "libx", "teams"])
def test_ordinary_names_are_free(name: str) -> None:
    assert reserved_namespace(name) is None


def test_check_namespace_by_distribution() -> None:
    # built-in namespaces: only from built-in distributions (PEP 503 name comparison)
    assert check_namespace("sci.areal_bym2", "mapi-plugin-science") is None
    assert check_namespace("dl.mlp", "mapi_jax") is None
    assert check_namespace("net.graph", "Mapi.Plots") is None
    for dist in ("acme-models", None):
        reason = check_namespace("sci.x", dist)
        assert reason is not None and "built-in plugins" in reason
    # the platform's namespaces: nobody, not even a built-in distribution
    for id_ in ("lib.x", "t.x", "mapi.x", "tenant_acme.x"):
        assert check_namespace(id_, "mapi-steps") is not None
        assert check_namespace(id_, "acme-models") is not None
    # third-party namespaces are free; ids without a namespace are not judged here
    assert check_namespace("acme.store_mean", "acme-models") is None
    assert check_namespace("nodot", "acme-models") is None
    # nodes: the plugin field names the namespace
    assert check_namespace("test.acme_zero", "acme-lab", plugin="acme") is None
    assert check_namespace("test.core_zero", "acme-lab", plugin="core") is not None
    reason = check_namespace("test.t_x_zero", "acme-lab", plugin="t_x")
    assert reason is not None and "acme-lab" in reason


def test_is_builtin_distribution_normalizes() -> None:
    assert is_builtin_distribution("mapi_plugin_mmm")
    assert is_builtin_distribution("MAPI-SDK")
    assert not is_builtin_distribution(None)
    assert not is_builtin_distribution("mapi-plugin-acme")


def test_the_module_is_dependency_free() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path(ns.__file__).read_text("utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imported <= {"__future__", "typing"}, imported
