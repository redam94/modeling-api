"""Id namespaces: which ones the platform owns and who may register in them (v0.18).

Every catalog id is ``<namespace>.<name>`` (``mmm.geo_mmm``, ``core.log1p``). Plugin workflow
nodes keep the two-segment id ``<kind>.<namespace>_<name>`` (``test.acme_zero_share``) and
name their namespace in ``NodeImplementation.plugin``; plugin workflow templates are
``<namespace>_<name>``.

* :data:`BUILTIN_NAMESPACES` belong to the built-in plugins: only a distribution in
  :data:`BUILTIN_DISTRIBUTIONS` may register an id there.
* :data:`PLATFORM_RESERVED` (and the prefixes in :data:`RESERVED_PREFIXES`) belong to the
  platform itself (library items, tenant uploads in v0.19): nobody may register there.
* v0.19: an uploaded plugin gets the namespace ``t_<tenant code>_<short name>``
  (:data:`UPLOAD_NAMESPACE_RE`, ``t_k3vq7m_acme``). Only the overlay loader, which knows the
  namespace the platform assigned to that upload, accepts ids there
  (``check_namespace(..., claimed=ns)``); everyone else is refused as before.

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
    "UPLOAD_NAMESPACE_RE",
    "check_namespace",
    "is_builtin_distribution",
    "is_uploaded_namespace",
    "namespace_of",
    "reserved_namespace",
    "uploaded_namespace_of",
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


UPLOAD_NAMESPACE_RE: Final = r"^t_([a-z2-7]{6})_([a-z][a-z0-9]{1,15})$"
"""v0.19: the namespace of an uploaded plugin, ``t_<tenant code>_<short name>``. The tenant
code is 6 base32 characters and the short name has no ``_``, so the parse is unambiguous."""

_CODE_CHARS: Final = frozenset("abcdefghijklmnopqrstuvwxyz234567")
_SHORT_HEAD: Final = frozenset("abcdefghijklmnopqrstuvwxyz")
_SHORT_CHARS: Final = _SHORT_HEAD | frozenset("0123456789")
_NAME_CHARS: Final = _SHORT_CHARS | {"_"}
# (string tests instead of ``re``: this module stays free of imports beyond ``typing``)


def is_uploaded_namespace(ns: str | None) -> bool:
    """True if ``ns`` has the form of an uploaded plugin's namespace (a pure string test,
    exactly :data:`UPLOAD_NAMESPACE_RE`)."""
    if ns is None:
        return False
    parts = ns.split("_")
    if len(parts) != 3 or parts[0] != "t":
        return False
    code, short = parts[1], parts[2]
    return (
        len(code) == 6
        and set(code) <= _CODE_CHARS
        and 2 <= len(short) <= 16
        and short[0] in _SHORT_HEAD
        and set(short) <= _SHORT_CHARS
    )


def _prefixed_namespace(name: str) -> str | None:
    """``t_<code>_<short>`` of ``t_<code>_<short>_<rest>`` (node names, template ids)."""
    parts = name.split("_", 3)
    if len(parts) != 4 or not parts[3] or not set(parts[3]) <= _NAME_CHARS:
        return None
    ns = "_".join(parts[:3])
    return ns if is_uploaded_namespace(ns) else None


def uploaded_namespace_of(id_: str) -> str | None:
    """The uploaded namespace of a catalog id or ref, ``None`` if it is not an uploaded id.

    The five id forms: models, steps and plots ``t_k3vq7m_acme.store_mean``; workflow nodes
    ``<kind>.t_k3vq7m_acme_<name>``; workflow templates ``t_k3vq7m_acme_<name>``. A version
    range (``@^1``) is ignored. A pure string test: the ``t_`` prefix is reserved, so nothing
    else can have these forms.
    """
    bare = id_.partition("@")[0]
    head, dot, rest = bare.partition(".")
    if dot:
        if is_uploaded_namespace(head):
            return head if rest else None
        # a node id: the kind is letters only, the namespace starts the name
        if head.isascii() and head.isalpha() and head.islower() and "." not in rest:
            return _prefixed_namespace(rest)
        return None
    return _prefixed_namespace(bare)


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


def check_namespace(
    id_: str,
    distribution: str | None,
    *,
    plugin: str | None = None,
    claimed: str | None = None,
) -> str | None:
    """Why ``distribution`` may not register ``id_`` (plain text), else ``None``.

    The built-in distributions may use :data:`BUILTIN_NAMESPACES`; nobody may use
    :data:`PLATFORM_RESERVED` or a :data:`RESERVED_PREFIXES` namespace. An id without a
    namespace is not judged here (the id's own pattern refuses it). ``distribution`` is
    ``None`` when the exporter is unknown, which never counts as built-in.

    ``claimed`` (v0.19) is the namespace the platform assigned to an uploaded overlay: the
    overlay loader passes it for entry points loaded from that overlay. An id in exactly that
    namespace is accepted (when it has the uploaded form); an id in any other namespace is
    refused, so an upload can register only under its own ``t_<code>_<short>``.
    """
    ns = namespace_of(id_, plugin=plugin)
    if claimed is not None:
        if ns is not None and ns == claimed and is_uploaded_namespace(ns):
            return None
        who = repr(distribution) if distribution else "an uploaded distribution"
        return (
            f"{id_!r} (exported by {who}): an upload may register ids only in its own "
            f"namespace {claimed!r}."
        )
    if ns is None:
        return None
    if ns in BUILTIN_NAMESPACES and is_builtin_distribution(distribution):
        return None
    reason = reserved_namespace(ns)
    if reason is None:
        return None
    who = repr(distribution) if distribution else "an unknown distribution"
    return f"{id_!r} (exported by {who}): {reason}"
