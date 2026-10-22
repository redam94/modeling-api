"""Id namespaces: which ones the platform owns and who may register in them (v0.18).

Every catalog id is ``<namespace>.<name>`` (``mmm.geo_mmm``, ``core.log1p``). Plugin workflow
nodes keep the two-segment id ``<kind>.<namespace>_<name>`` (``test.acme_zero_share``) and
name their namespace in ``NodeImplementation.plugin``; plugin workflow templates are
``<namespace>_<name>``.

* :data:`BUILTIN_NAMESPACES` belong to the built-in plugins: only a distribution in
  :data:`BUILTIN_DISTRIBUTIONS` may register an id there.
* :data:`PLATFORM_RESERVED` (and the prefixes in :data:`RESERVED_PREFIXES`) belong to the
  platform itself (library items, tenant uploads in v0.19): nobody may register there.

This module is the one source of truth: the SDK registry, the ``mapi plugin`` CLI, the node and
template loaders and the server import it. It is JSON-only (no third-party import).
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BUILTIN_DISTRIBUTIONS",
    "BUILTIN_NAMESPACES",
    "PLATFORM_RESERVED",
    "RESERVED_PREFIXES",
    "check_namespace",
    "is_builtin_distribution",
    "namespace_of",
    "reserved_namespace",
]

BUILTIN_NAMESPACES: Final[frozenset[str]] = frozenset(
    {
        "core",
        "mmm",
        "ml",
        "sklearn",
        "ts",
        "tabular",
        "choice",
        "causal",
        "sci",
        "dl",
        "net",
        # references and examples registered in memory (SDK toys, mapi_pymc examples, docs)
        "toy",
        "demo",
        "examples",
    }
)
"""Id namespaces of the built-in plugins; other distributions never register in them."""

PLATFORM_RESERVED: Final[frozenset[str]] = frozenset(
    {"mapi", "platform", "builtin", "team", "library", "lib", "plugin", "t", "tenant"}
)
"""Namespaces the platform keeps for itself (library items, uploads): nobody registers there."""

RESERVED_PREFIXES: Final[tuple[str, ...]] = ("t_", "tenant_")
"""Namespace prefixes kept for tenant uploads (v0.19 puts them under ``t_<tenant>_<ns>``)."""

BUILTIN_DISTRIBUTIONS: Final[frozenset[str]] = frozenset(
    {
        "mapi-sdk",
        "mapi-steps",
        "mapi-plots",
        "mapi-pymc",
        "mapi-sklearn",
        "mapi-jax",
        "mapi-workflows",
        "mapi-runner",
        "mapi-plugin-mmm",
        "mapi-plugin-ts",
        "mapi-plugin-tabular",
        "mapi-plugin-statsmodels",
        "mapi-plugin-causal",
        "mapi-plugin-science",
        "mapi-conformance",
    }
)
"""The distributions that ship with the platform (they may use :data:`BUILTIN_NAMESPACES`)."""


def _norm_dist(name: str) -> str:
    # importlib.metadata reports the name as written in the wheel (``mapi_plugin_mmm`` or
    # ``mapi-plugin-mmm``); compare the normalized (PEP 503) form
    return name.strip().lower().replace("_", "-").replace(".", "-")


def is_builtin_distribution(distribution: str | None) -> bool:
    """True if ``distribution`` ships with the platform (PEP 503 name comparison)."""
    return distribution is not None and _norm_dist(distribution) in BUILTIN_DISTRIBUTIONS


def namespace_of(id_: str, *, plugin: str | None = None) -> str | None:
    """The namespace of a catalog id (``"acme.x"`` -> ``"acme"``), ``None`` without one.

    A workflow node id (``test.acme_zero``) starts with its kind, not its namespace: pass the
    contract's ``plugin`` (``"acme"``), which is returned as is. A template id
    (``acme_weekly``) has no dot: pass its namespace as ``plugin`` too.
    """
    if plugin is not None:
        return plugin
    head, dot, _ = id_.partition(".")
    return head if dot and head else None


def reserved_namespace(ns: str) -> str | None:
    """Why a *third-party* plugin may not use namespace ``ns`` (plain text), else ``None``."""
    if ns in PLATFORM_RESERVED:
        return f"The namespace {ns!r} is reserved for the platform. Use a different namespace."
    for prefix in RESERVED_PREFIXES:
        if ns.startswith(prefix):
            return (
                f"Namespaces that start with {prefix!r} are reserved for uploaded plugins. "
                "Use a different namespace."
            )
    if ns in BUILTIN_NAMESPACES:
        return f"The namespace {ns!r} belongs to the built-in plugins. Use a different namespace."
    return None


def check_namespace(id_: str, distribution: str | None, *, plugin: str | None = None) -> str | None:
    """Why ``distribution`` may not register ``id_`` (plain text), else ``None``.

    The built-in distributions may use :data:`BUILTIN_NAMESPACES`; nobody may use
    :data:`PLATFORM_RESERVED` or a :data:`RESERVED_PREFIXES` namespace. An id without a
    namespace is not judged here (the id's own pattern refuses it). ``distribution`` is
    ``None`` when the exporter is unknown, which never counts as built-in.
    """
    ns = namespace_of(id_, plugin=plugin)
    if ns is None:
        return None
    if ns in BUILTIN_NAMESPACES and is_builtin_distribution(distribution):
        return None
    reason = reserved_namespace(ns)
    if reason is None:
        return None
    who = repr(distribution) if distribution else "an unknown distribution"
    return f"{id_!r} (exported by {who}): {reason}"
