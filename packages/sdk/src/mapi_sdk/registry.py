"""Registries of steps, models and plot recipes.

Discovery uses entry points (``mapi.steps``, ``mapi.models``, ``mapi.plots``) - only inside
runner images and notebooks, never in the control plane - plus in-memory registration for
tests and notebooks. :meth:`Registry.catalog` exports the static JSON
(:class:`~mapi_spec.catalog.Catalog`) that the control plane validates specs against.

An entry point may load a step class, a model class, a plot recipe, an iterable of those, or
a zero-argument callable returning one of those.

Who may register what comes from :mod:`mapi_spec.namespaces` (v0.18): only the built-in
distributions use the built-in namespaces, and nobody uses the platform-reserved ones. What an
entry point could not register is kept as :attr:`Registry.diagnostics` (and, as plain text,
in :attr:`Registry.load_errors`); entries from other distributions carry their ``provider``.

Uploaded code (v0.19) arrives as *overlays*: a wheel the supervisor verified and extracted
into a read-only directory, which the worker appends to ``sys.path``. A registry built with
``overlays`` (or :func:`default_registry` after :func:`set_process_overlays`) reads each
overlay's entry points from that directory only (``importlib.metadata.distributions(path=
[dir])``), so a distribution name never decides whether code was uploaded: the *path* does.
Everything an overlay exports gets ``provider="upload:<plugin version id>"`` and must use the
namespace the platform assigned to that upload (``check_namespace(..., claimed=ns)``); the
global scan skips every distribution that lives under an overlay directory. Entry points of
other groups in an overlay are refused with a ``load_error`` diagnostic. The workflow node,
template and governance loaders use the same helpers (:func:`plugin_entry_points`).
"""

from __future__ import annotations

import importlib.metadata
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol, TypeVar, runtime_checkable

import xarray as xr
from mapi_spec.catalog import (
    Catalog,
    CatalogDiagnostic,
    DiagnosticCode,
    ModelCatalogEntry,
    PlotCatalogEntry,
    StepCatalogEntry,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.namespaces import (
    BUILTIN_DISTRIBUTIONS,
    BUILTIN_NAMESPACES,
    PLATFORM_RESERVED,
    check_namespace,
    is_builtin_distribution,
    is_uploaded_namespace,
    namespace_of,
    reserved_namespace,
)
from mapi_spec.plots import PlotExamples, PlotSpec
from mapi_spec.refs import UseRef, Version

from mapi_sdk.models import ModelPlugin
from mapi_sdk.steps import Step

__all__ = [
    "BUILTIN_DISTRIBUTIONS",
    "BUILTIN_NAMESPACES",
    "ENTRY_POINT_GROUPS",
    "OVERLAY_ENTRY_POINT_GROUPS",
    "PLATFORM_RESERVED",
    "EntryPointOrigin",
    "OverlayLike",
    "PlotRecipe",
    "Registry",
    "RegistryKind",
    "UploadOverlay",
    "as_overlays",
    "default_registry",
    "entry_point_origin",
    "overlay_of_distribution",
    "plugin_entry_points",
    "process_overlays",
    "set_process_overlays",
]

ENTRY_POINT_GROUPS = ("mapi.steps", "mapi.models", "mapi.plots")

OVERLAY_ENTRY_POINT_GROUPS: Final = (
    "mapi.models",
    "mapi.steps",
    "mapi.plots",
    "mapi.workflow_nodes",
    "mapi.workflow_templates",
)
"""The groups uploaded code may use (``mapi_spec.plugins.PLUGIN_ENTRY_POINT_GROUPS``; kept
here as a literal so the SDK does not import the upload DTOs). Uploaded
``mapi.governance_checks`` are refused by their loader; any other group by the registry."""
_GOVERNANCE_GROUP: Final = "mapi.governance_checks"

RegistryKind = Literal["step", "model", "plot"]
_E = TypeVar("_E", StepCatalogEntry, ModelCatalogEntry, PlotCatalogEntry)

# BUILTIN_NAMESPACES, BUILTIN_DISTRIBUTIONS and PLATFORM_RESERVED are re-exported from
# mapi_spec.namespaces: one list for the SDK, the CLI, the node loaders and the server.

# ================================================================================== overlays

OverlayLike = tuple[Path, str, str]
"""``(directory, namespace, plugin version id)`` of one mounted upload (the worker's
``OverlaySpec`` is such a tuple)."""


@dataclass(frozen=True, slots=True)
class UploadOverlay:
    """One uploaded wheel the worker mounted: its read-only directory (on ``sys.path``), the
    namespace the platform assigned to it and its plugin version id."""

    dir: Path
    namespace: str
    pvid: str

    @property
    def provider(self) -> str:
        """``upload:<plugin version id>``: the provider of everything the overlay exports."""
        return f"upload:{self.pvid}"

    def contains(self, path: Path) -> bool:
        """True if ``path`` is this overlay's directory or lies under it."""
        try:
            return path.resolve().is_relative_to(self.dir.resolve())
        except OSError:
            return False


def as_overlays(overlays: Iterable[OverlayLike | UploadOverlay]) -> tuple[UploadOverlay, ...]:
    """Normalize ``(dir, namespace, pvid)`` tuples (or overlays) and refuse bad ones."""
    out: list[UploadOverlay] = []
    for item in overlays:
        if isinstance(item, UploadOverlay):
            ov = item
        else:
            d, ns, pvid = item
            ov = UploadOverlay(Path(d), str(ns), str(pvid))
        if not is_uploaded_namespace(ov.namespace):
            raise ValueError(f"{ov.namespace!r} is not the namespace of an upload")
        if not ov.pvid or not ov.dir.is_absolute():
            raise ValueError("an overlay needs an absolute directory and a plugin version id")
        out.append(ov)
    if len({o.namespace for o in out}) != len(out):
        raise ValueError("a process mounts at most one version of each uploaded namespace")
    return tuple(out)


_process_overlays: tuple[UploadOverlay, ...] = ()


def set_process_overlays(overlays: Iterable[OverlayLike | UploadOverlay]) -> None:
    """Record the overlays this process mounted (the worker calls it once, after appending
    their directories to ``sys.path`` and before anything loads entry points). The
    default registry and the workflow loaders read them; tests reset them with ``()``."""
    global _process_overlays
    _process_overlays = as_overlays(overlays)


def process_overlays() -> tuple[UploadOverlay, ...]:
    """The overlays :func:`set_process_overlays` recorded (empty outside a worker)."""
    return _process_overlays


def overlay_of_distribution(dist: Any, overlays: Sequence[UploadOverlay]) -> UploadOverlay | None:
    """The overlay whose directory holds ``dist`` (a path test, never a name test).

    ``dist.locate_file("")`` is the ``sys.path`` entry the distribution was found in.
    Anything without a readable location (test doubles, odd finders) is not an overlay."""
    if not overlays or dist is None:
        return None
    locate = getattr(dist, "locate_file", None)
    if not callable(locate):
        return None
    try:
        where = Path(str(locate("")))
    except Exception:  # an exotic Distribution: treat it as installed code
        return None
    for ov in overlays:
        if ov.contains(where):
            return ov
    return None


@dataclass(frozen=True, slots=True)
class EntryPointOrigin:
    """Where one entry point comes from: an installed distribution or an overlay."""

    entry_point: importlib.metadata.EntryPoint
    distribution: str | None
    overlay: UploadOverlay | None = None

    @property
    def origin(self) -> str | None:
        """``upload:<pvid>`` for an overlay, else the PEP 503 name of the distribution."""
        return entry_point_origin(self.distribution, self.overlay)


def entry_point_origin(distribution: str | None, overlay: UploadOverlay | None) -> str | None:
    """The origin key :func:`mapi_runner.catalog_export.full_catalog` filters on."""
    if overlay is not None:
        return overlay.provider
    if distribution is None:
        return None
    return distribution.strip().lower().replace("_", "-").replace(".", "-")


def _dist_name(dist: Any) -> str | None:
    name = getattr(dist, "name", None)
    if name is None:
        metadata = getattr(dist, "metadata", None)
        name = metadata["Name"] if metadata is not None else None
    return str(name) if name else None


def _overlay_distributions(ov: UploadOverlay) -> list[Any]:
    try:
        return list(importlib.metadata.distributions(path=[str(ov.dir)]))
    except Exception:  # an unreadable overlay exports nothing
        return []


def plugin_entry_points(
    group: str, overlays: Sequence[UploadOverlay] | None = None
) -> list[EntryPointOrigin]:
    """Every entry point of ``group``: the installed ones (``importlib.metadata.entry_points``,
    minus any distribution that lives under an overlay directory), then each overlay's own,
    read from its directory only (``None``: :func:`process_overlays`).

    Reading overlays per directory matters: ``entry_points()`` keeps one distribution per
    normalized name, so an upload named like an installed distribution would otherwise be
    invisible (or hide the installed one)."""
    ovs = process_overlays() if overlays is None else overlays
    out: list[EntryPointOrigin] = []
    for ep in importlib.metadata.entry_points(group=group):
        dist = getattr(ep, "dist", None)
        if ovs and overlay_of_distribution(dist, ovs) is not None:
            continue  # loaded below, from its own directory
        out.append(EntryPointOrigin(ep, _dist_name(dist)))
    for ov in ovs:
        for dist in _overlay_distributions(ov):
            for ep in dist.entry_points:
                if ep.group == group:
                    out.append(EntryPointOrigin(ep, _dist_name(dist), ov))
    return out


def _foreign_overlay_entry_points(
    overlays: Sequence[UploadOverlay],
) -> list[EntryPointOrigin]:
    """Entry points of an overlay in a group uploads may not use (governance checks are
    reported by their own loader)."""
    allowed = {*OVERLAY_ENTRY_POINT_GROUPS, _GOVERNANCE_GROUP}
    out: list[EntryPointOrigin] = []
    for ov in overlays:
        for dist in _overlay_distributions(ov):
            for ep in dist.entry_points:
                if ep.group not in allowed:
                    out.append(EntryPointOrigin(ep, _dist_name(dist), ov))
    return out


_TRUSTED_MESSAGE: dict[DiagnosticCode, str] = {
    "load_error": "The entry point could not be loaded.",
    "id_collision": "This id and version are already registered. The first registration is kept.",
}
_FOREIGN_GROUP_MESSAGE: Final = (
    "Uploaded code may use only the mapi.models, mapi.steps, mapi.plots, mapi.workflow_nodes "
    "and mapi.workflow_templates entry-point groups. This entry point is ignored."
)
_NO_ID_MESSAGE: Final = "an uploaded export must have an id in the namespace of the upload"


class ReservedNamespaceError(ValueError):
    """An entry point exported an id in a namespace that its distribution may not use."""

    def __init__(self, message: str, *, namespace: str) -> None:
        super().__init__(message)
        self.namespace = namespace


class IdCollisionError(ValueError):
    """An ``id@version`` is already registered with another object."""


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _flatten(obj: Any) -> list[Any]:
    """The steps/models/plot recipes an entry point exported (lists are flattened)."""
    if isinstance(obj, type) or hasattr(obj, "catalog_entry"):
        return [obj]
    if isinstance(obj, Iterable) and not isinstance(obj, str | bytes):
        return [item for o in obj for item in _flatten(o)]
    return [obj]


def _plugin_id(obj: Any) -> str | None:
    pid = getattr(obj, "id", None)
    if isinstance(pid, str):
        return pid
    entry = getattr(obj, "catalog_entry", None)
    if callable(entry):
        eid = getattr(entry(), "id", None)
        return eid if isinstance(eid, str) else None
    return None


def _safe_plugin_id(obj: Any) -> str | None:
    """:func:`_plugin_id` for a diagnostic: plugin code that fails here gives no id."""
    try:
        return _plugin_id(obj)
    except Exception:  # plugin code; the registration error is reported instead
        return None


@runtime_checkable
class PlotRecipe(Protocol):
    """A plot recipe: static catalog metadata plus a renderer producing a PlotSpec."""

    @classmethod
    def catalog_entry(cls) -> PlotCatalogEntry: ...

    def render(self, data: xr.DataTree, **options: Any) -> PlotSpec: ...


@dataclass(slots=True)
class _Kind:
    items: dict[str, dict[Version, Any]] = field(default_factory=dict)
    # (id, version) -> distribution, only for entries from non-built-in distributions
    # (``upload:<pvid>`` for an overlay)
    providers: dict[tuple[str, Version], str] = field(default_factory=dict)
    # (id, version) -> origin of every entry an entry point registered (v0.19: the PEP 503
    # distribution name, or ``upload:<pvid>``); ``full_catalog(only_distribution=)`` reads it
    origins: dict[tuple[str, Version], str] = field(default_factory=dict)

    def add(
        self,
        id_: str,
        version: str,
        obj: Any,
        *,
        replace: bool,
        provider: str | None = None,
        origin: str | None = None,
    ) -> None:
        v = Version.parse(version)
        bucket = self.items.setdefault(id_, {})
        current = bucket.get(v)
        if current is not None and current is not obj and not replace:
            raise IdCollisionError(f"{id_}@{version} is already registered")
        if current is not obj:
            bucket[v] = obj
            self.providers.pop((id_, v), None)
            self.origins.pop((id_, v), None)
        if provider is not None:
            self.providers.setdefault((id_, v), provider)
        if origin is not None:
            self.origins.setdefault((id_, v), origin)

    def resolve(self, ref: str | UseRef, what: str) -> Any:
        r = UseRef.parse(ref) if isinstance(ref, str) else ref
        bucket = self.items.get(r.id, {})
        best = r.resolve(bucket)
        if best is None:
            known = ", ".join(str(v) for v in sorted(bucket))
            raise MapiError(
                f"no registered {what} satisfies {str(r)!r}"
                + (f" (available: {known})" if known else ""),
                code=ErrorCode.REF_UNRESOLVED,
            )
        return bucket[best]

    def provider(self, id_: str) -> str | None:
        """The provider of ``id`` (its highest version) or of ``id@version`` (exactly)."""
        name, _, version = id_.partition("@")
        bucket = self.items.get(name)
        if not bucket:
            return None
        v = Version.parse(version) if version else max(bucket)
        return self.providers.get((name, v))

    def all(self) -> list[Any]:
        return [obj for _, b in sorted(self.items.items()) for _, obj in sorted(b.items())]

    def origin(self, id_: str, version: str) -> str | None:
        return self.origins.get((id_, Version.parse(version)))

    def copy(self) -> _Kind:
        return _Kind(
            items={k: dict(v) for k, v in self.items.items()},
            providers=dict(self.providers),
            origins=dict(self.origins),
        )


class Registry:
    """Steps, models and plot recipes by id and semver version.

    ``diagnostics`` lists what :meth:`load_entry_points` could not register, with a code
    (``load_error``, ``namespace_reserved``, ``id_collision``); ``load_errors`` keeps the same
    rows as plain text for older callers.

    ``overlays`` (v0.19) are the uploads this process mounted, as ``(directory, namespace,
    plugin version id)``; their directories must already be on ``sys.path``. Entry points
    found under an overlay directory get the provider ``upload:<pvid>`` and may only use that
    overlay's namespace (see the module docs).
    """

    def __init__(self, *, overlays: Sequence[OverlayLike | UploadOverlay] = ()) -> None:
        self._steps = _Kind()
        self._models = _Kind()
        self._plots = _Kind()
        self.overlays: tuple[UploadOverlay, ...] = as_overlays(overlays)
        self.load_errors: list[str] = []
        self.diagnostics: list[CatalogDiagnostic] = []
        # the origin of each row of ``diagnostics`` (same order; see ``diagnostics_of``)
        self._diagnostic_origins: list[str | None] = []

    # -------------------------------------------------------------- registration
    def register_step(
        self, cls: type[Step[Any, Any]], *, replace: bool = False
    ) -> type[Step[Any, Any]]:
        """Register a step class (usable as a decorator)."""
        return self._add_step(cls, replace=replace, provider=None)

    def register_model(self, cls: type[Any], *, replace: bool = False) -> type[Any]:
        """Register a model plugin class (usable as a decorator)."""
        return self._add_model(cls, replace=replace, provider=None)

    def register_plot(self, recipe: Any, *, replace: bool = False) -> Any:
        """Register a plot recipe (anything with ``catalog_entry()`` and ``render``)."""
        return self._add_plot(recipe, replace=replace, provider=None)

    def register(self, obj: Any, *, replace: bool = False) -> None:
        """Register a step, model or plot recipe (or an iterable of them)."""
        self._add(obj, replace=replace, provider=None)

    def _add_step(
        self,
        cls: type[Step[Any, Any]],
        *,
        replace: bool,
        provider: str | None,
        origin: str | None = None,
    ) -> type[Step[Any, Any]]:
        if not (isinstance(cls, type) and issubclass(cls, Step)):
            raise TypeError(f"{cls!r} is not a Step subclass")
        cls.catalog_entry()  # validates the static metadata eagerly
        self._steps.add(cls.id, cls.version, cls, replace=replace, provider=provider, origin=origin)
        return cls

    def _add_model(
        self, cls: type[Any], *, replace: bool, provider: str | None, origin: str | None = None
    ) -> type[Any]:
        if not isinstance(cls, type) or not all(
            hasattr(cls, a) for a in ("id", "version", "manifest", "fit", "score")
        ):
            raise TypeError(f"{cls!r} is not a model plugin class")
        cls.manifest()
        self._models.add(
            cls.id, cls.version, cls, replace=replace, provider=provider, origin=origin
        )
        return cls

    def _add_plot(
        self, recipe: Any, *, replace: bool, provider: str | None, origin: str | None = None
    ) -> Any:
        entry = recipe.catalog_entry()
        if not isinstance(entry, PlotCatalogEntry):
            raise TypeError(f"{recipe!r}.catalog_entry() must return a PlotCatalogEntry")
        self._plots.add(
            entry.id, entry.version, recipe, replace=replace, provider=provider, origin=origin
        )
        return recipe

    def _add(
        self, obj: Any, *, replace: bool, provider: str | None, origin: str | None = None
    ) -> None:
        if isinstance(obj, type) and issubclass(obj, Step):
            self._add_step(obj, replace=replace, provider=provider, origin=origin)
        elif isinstance(obj, type) and hasattr(obj, "manifest") and hasattr(obj, "score"):
            self._add_model(obj, replace=replace, provider=provider, origin=origin)
        elif hasattr(obj, "catalog_entry") and hasattr(obj, "render"):
            self._add_plot(obj, replace=replace, provider=provider, origin=origin)
        elif isinstance(obj, Iterable) and not isinstance(obj, str | bytes):
            for item in obj:
                self._add(item, replace=replace, provider=provider, origin=origin)
        else:
            raise TypeError(f"cannot register {obj!r}: not a step, model or plot recipe")

    # ----------------------------------------------------------------- discovery
    def load_entry_points(
        self, groups: Iterable[str] = ENTRY_POINT_GROUPS, *, strict: bool = False
    ) -> list[str]:
        """Load and register every entry point in ``groups``; return the errors.

        Nothing an entry point exports may replace what is already registered (the first
        registration wins, whatever the iteration order of installed distributions); ids in
        the built-in namespaces (:data:`BUILTIN_NAMESPACES`) are only accepted from the
        built-in distributions, and ids in a platform-reserved namespace from nobody
        (:func:`mapi_spec.namespaces.check_namespace`). Every refusal is one row in
        ``diagnostics`` and in ``load_errors`` (raised with ``strict``); one bad export
        never hides the others of the same entry point.

        This guard covers entry points only: :meth:`register` (tests, notebooks, docs
        snippets) registers in any namespace.

        v0.19: entry points of the registry's ``overlays`` are read from their directories,
        get the provider ``upload:<pvid>`` and must use the overlay's namespace; an export
        without an id, and an overlay entry point of a group uploads may not use, are refused.
        """
        errors: list[str] = []
        for group in groups:
            for found in plugin_entry_points(group, self.overlays):
                ep, dist, ov = found.entry_point, found.distribution, found.overlay
                try:
                    obj = ep.load()
                    if callable(obj) and not isinstance(obj, type):
                        obj = obj()
                    items = _flatten(obj)
                except Exception as exc:  # report and keep going: one bad plugin
                    errors.append(
                        self._refused(group, ep.name, dist, None, exc, strict=strict, overlay=ov)
                    )
                    continue
                for item in items:
                    try:
                        self._register_entry(item, dist, ov)
                    except Exception as exc:  # report and keep going: one bad export
                        pid = _safe_plugin_id(item)
                        errors.append(
                            self._refused(group, ep.name, dist, pid, exc, strict=strict, overlay=ov)
                        )
        for found in _foreign_overlay_entry_points(self.overlays):
            errors.append(self._foreign_group(found, strict=strict))
        self.load_errors += errors
        return errors

    def _register_entry(
        self, obj: Any, dist: str | None, overlay: UploadOverlay | None = None
    ) -> None:
        pid = _plugin_id(obj)
        if overlay is not None:
            # uploaded code: the path decided it is an upload; it may register only under
            # the namespace the platform assigned (whatever its distribution is called)
            reason = (
                _NO_ID_MESSAGE
                if pid is None
                else check_namespace(pid, dist, claimed=overlay.namespace)
            )
            if reason is not None:
                raise ReservedNamespaceError(
                    f"reserved namespace: {reason}",
                    namespace=(namespace_of(pid) if pid else None) or "",
                )
            self._add(obj, replace=False, provider=overlay.provider, origin=overlay.provider)
            return
        if pid is not None:
            reason = check_namespace(pid, dist)
            if reason is not None:
                raise ReservedNamespaceError(
                    f"reserved namespace: {reason}", namespace=namespace_of(pid) or ""
                )
        provider = None if dist is None or is_builtin_distribution(dist) else dist
        self._add(
            obj,
            replace=False,
            provider=_clip(provider, 128),
            origin=entry_point_origin(dist, None),
        )

    def _foreign_group(self, found: EntryPointOrigin, *, strict: bool) -> str:
        ep = found.entry_point
        msg = f"{ep.group}:{ep.name}: uploaded code may not use this entry-point group"
        if strict:
            raise RuntimeError(msg)
        self._diagnose(
            CatalogDiagnostic(
                code="load_error",
                group=_clip(ep.group, 64) or "",
                entry_point=_clip(ep.name, 128),
                distribution=_clip(found.distribution, 128),
                message=_FOREIGN_GROUP_MESSAGE,
                untrusted_text=_clip(ep.value, 500),
            ),
            found.origin,
        )
        return msg

    def _diagnose(self, row: CatalogDiagnostic, origin: str | None) -> None:
        self.diagnostics.append(row)
        self._diagnostic_origins.append(origin)

    def diagnostics_of(self, origin: str) -> list[CatalogDiagnostic]:
        """The diagnostics of entry points from ``origin`` (a PEP 503 distribution name, or
        ``upload:<pvid>`` for an overlay)."""
        return [
            d
            for d, o in zip(self.diagnostics, self._diagnostic_origins, strict=False)
            if o == origin
        ]

    def overlay(self, namespace: str) -> UploadOverlay | None:
        """The mounted overlay of ``namespace`` (``None``: not mounted here)."""
        return next((o for o in self.overlays if o.namespace == namespace), None)

    def _refused(
        self,
        group: str,
        entry_point: str,
        dist: str | None,
        id_: str | None,
        exc: Exception,
        *,
        strict: bool,
        overlay: UploadOverlay | None = None,
    ) -> str:
        """Record one refused entry point (or export) and return its ``load_errors`` text."""
        msg = f"{group}:{entry_point}: {type(exc).__name__}: {exc}"
        if strict:
            raise RuntimeError(msg) from exc
        code: DiagnosticCode
        if isinstance(exc, ReservedNamespaceError):
            code = "namespace_reserved"
            # platform text: it names only a namespace from the fixed reserved sets or a prefix
            message = reserved_namespace(exc.namespace) or _TRUSTED_MESSAGE["load_error"]
        elif isinstance(exc, IdCollisionError):
            code, message = "id_collision", _TRUSTED_MESSAGE["id_collision"]
        else:
            code, message = "load_error", _TRUSTED_MESSAGE["load_error"]
        if overlay is not None and code == "namespace_reserved":
            # the reserved-namespace text names the fixed sets; an upload's rule is its own
            message = (
                f"An upload may register ids only in its own namespace {overlay.namespace!r}. "
                "This export is ignored."
            )
        self._diagnose(
            CatalogDiagnostic(
                code=code,
                group=_clip(group, 64) or "",
                entry_point=_clip(entry_point, 128),
                distribution=_clip(dist, 128),
                id=_clip(id_, 128),
                message=message,
                untrusted_text=_clip(f"{type(exc).__name__}: {exc}", 500),
            ),
            entry_point_origin(dist, overlay),
        )
        return msg

    # ------------------------------------------------------------------- lookup
    def step(self, ref: str | UseRef) -> type[Step[Any, Any]]:
        out: type[Step[Any, Any]] = self._steps.resolve(ref, "step")
        return out

    def model(self, ref: str | UseRef) -> type[ModelPlugin]:
        out: type[ModelPlugin] = self._models.resolve(ref, "model")
        return out

    def plot(self, ref: str | UseRef) -> Any:
        return self._plots.resolve(ref, "plot recipe")

    def steps(self) -> list[type[Step[Any, Any]]]:
        return self._steps.all()

    def models(self) -> list[type[ModelPlugin]]:
        return self._models.all()

    def plots(self) -> list[Any]:
        return self._plots.all()

    def provider_of(self, kind: RegistryKind, id_: str) -> str | None:
        """The distribution that registered ``id_`` (``"acme.x"`` = its highest version, or
        ``"acme.x@1.2.0"`` exactly) if it is not a built-in one, else ``None``.

        Entries registered in memory (:meth:`register`) and entries from the built-in
        distributions have no provider; entries from an overlay have ``upload:<pvid>``
        whatever their distribution is called (v0.19).
        """
        return self._store(kind).provider(id_)

    def origin_of(self, kind: RegistryKind, id_: str, version: str) -> str | None:
        """Where the entry point that registered ``id@version`` comes from: the PEP 503 name
        of its distribution, ``upload:<pvid>`` for an overlay, ``None`` for in-memory
        registrations (v0.19; :func:`mapi_runner.catalog_export.full_catalog` filters on it)."""
        return self._store(kind).origin(id_, version)

    def _store(self, kind: RegistryKind) -> _Kind:
        return {"step": self._steps, "model": self._models, "plot": self._plots}[kind]

    def copy(self) -> Registry:
        new = Registry()
        new.overlays = self.overlays
        new._steps, new._models, new._plots = (
            self._steps.copy(),
            self._models.copy(),
            self._plots.copy(),
        )
        return new

    # ------------------------------------------------------------------- export
    def catalog(self) -> Catalog:
        """Static catalog JSON for the control plane (no code, no data).

        Entries from non-built-in distributions carry ``provider``; built-in entries do not,
        so a catalog of built-ins only keeps its digest. ``diagnostics`` are not included:
        the runner's catalog export merges them with the workflow node and template rows.
        """
        return Catalog(
            steps=[
                self._provided(s.catalog_entry(), self._steps, s.id, s.version)
                for s in self.steps()
            ],
            models=[
                self._provided(
                    ModelCatalogEntry(manifest=m.manifest()), self._models, m.id, m.version
                )
                for m in self.models()
            ],
            plots=[self._plot_entry(p) for p in self.plots()],
        )

    def _plot_entry(self, recipe: Any) -> PlotCatalogEntry:
        entry: PlotCatalogEntry = recipe.catalog_entry()
        return self._provided(entry, self._plots, entry.id, entry.version)

    @staticmethod
    def _provided(entry: _E, store: _Kind, id_: str, version: str) -> _E:
        provider = store.providers.get((id_, Version.parse(version)))
        return entry if provider is None else entry.model_copy(update={"provider": provider})

    def plot_examples(
        self, *, on_error: Callable[[str, Exception], None] | None = None
    ) -> PlotExamples:
        """Every plot recipe rendered on its synthetic example data, keyed by ``id@version``
        (recipes without an ``example_spec()`` or without example data are left out).

        A recipe that fails, or whose example is not self-contained, is left out too and
        reported to ``on_error``: one broken example never hides the others."""
        out: dict[str, PlotSpec] = {}
        for p in self.plots():
            make = getattr(p, "example_spec", None)
            if make is None:
                continue
            ref = p.catalog_entry().ref
            try:
                spec = make()
                if spec is not None:
                    PlotExamples(plots={ref: spec})
                    out[ref] = spec
            except Exception as exc:  # plugin code: report it, keep the rest
                if on_error is not None:
                    on_error(ref, exc)
        return PlotExamples(plots=out)


_default: Registry | None = None
_lock = threading.Lock()


def default_registry(
    *, reload: bool = False, loader: Callable[[Registry], Any] | None = None
) -> Registry:
    """The process-wide registry, populated from entry points on first use (with the
    overlays :func:`set_process_overlays` recorded, v0.19)."""
    global _default
    with _lock:
        if _default is None or reload:
            reg = Registry(overlays=process_overlays())
            (loader or (lambda r: r.load_entry_points()))(reg)
            _default = reg
        return _default
