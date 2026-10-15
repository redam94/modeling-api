"""Registries of steps, models and plot recipes.

Discovery uses entry points (``mapi.steps``, ``mapi.models``, ``mapi.plots``) - only inside
runner images and notebooks, never in the control plane - plus in-memory registration for
tests and notebooks. :meth:`Registry.catalog` exports the static JSON
(:class:`~mapi_spec.catalog.Catalog`) that the control plane validates specs against.

An entry point may load a step class, a model class, a plot recipe, an iterable of those, or
a zero-argument callable returning one of those.
"""

from __future__ import annotations

import importlib.metadata
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import xarray as xr
from mapi_spec.catalog import Catalog, ModelCatalogEntry, PlotCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plots import PlotSpec
from mapi_spec.refs import UseRef, Version

from mapi_sdk.models import ModelPlugin
from mapi_sdk.steps import Step

__all__ = [
    "ENTRY_POINT_GROUPS",
    "PlotRecipe",
    "Registry",
    "default_registry",
]

ENTRY_POINT_GROUPS = ("mapi.steps", "mapi.models", "mapi.plots")

BUILTIN_NAMESPACES: frozenset[str] = frozenset({"core", "mmm", "ml", "sklearn", "ts", "tabular"})
"""Id namespaces of the built-in (T0) plugins; other distributions never register in them."""
BUILTIN_DISTRIBUTIONS: frozenset[str] = frozenset(
    {
        "mapi-sdk",
        "mapi-steps",
        "mapi-plots",
        "mapi-pymc",
        "mapi-sklearn",
        "mapi-plugin-mmm",
        "mapi-plugin-ts",
        "mapi-plugin-tabular",
        "mapi-conformance",
    }
)


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


@runtime_checkable
class PlotRecipe(Protocol):
    """A plot recipe: static catalog metadata plus a renderer producing a PlotSpec."""

    @classmethod
    def catalog_entry(cls) -> PlotCatalogEntry: ...

    def render(self, data: xr.DataTree, **options: Any) -> PlotSpec: ...


@dataclass(slots=True)
class _Kind:
    items: dict[str, dict[Version, Any]] = field(default_factory=dict)

    def add(self, id_: str, version: str, obj: Any, *, replace: bool) -> None:
        v = Version.parse(version)
        bucket = self.items.setdefault(id_, {})
        if v in bucket and bucket[v] is not obj and not replace:
            raise ValueError(f"{id_}@{version} is already registered")
        bucket[v] = obj

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

    def all(self) -> list[Any]:
        return [obj for _, b in sorted(self.items.items()) for _, obj in sorted(b.items())]


class Registry:
    """Steps, models and plot recipes by id and semver version."""

    def __init__(self) -> None:
        self._steps = _Kind()
        self._models = _Kind()
        self._plots = _Kind()
        self.load_errors: list[str] = []

    # -------------------------------------------------------------- registration
    def register_step(
        self, cls: type[Step[Any, Any]], *, replace: bool = False
    ) -> type[Step[Any, Any]]:
        """Register a step class (usable as a decorator)."""
        if not (isinstance(cls, type) and issubclass(cls, Step)):
            raise TypeError(f"{cls!r} is not a Step subclass")
        cls.catalog_entry()  # validates the static metadata eagerly
        self._steps.add(cls.id, cls.version, cls, replace=replace)
        return cls

    def register_model(self, cls: type[Any], *, replace: bool = False) -> type[Any]:
        """Register a model plugin class (usable as a decorator)."""
        if not isinstance(cls, type) or not all(
            hasattr(cls, a) for a in ("id", "version", "manifest", "fit", "score")
        ):
            raise TypeError(f"{cls!r} is not a model plugin class")
        cls.manifest()
        self._models.add(cls.id, cls.version, cls, replace=replace)
        return cls

    def register_plot(self, recipe: Any, *, replace: bool = False) -> Any:
        """Register a plot recipe (anything with ``catalog_entry()`` and ``render``)."""
        entry = recipe.catalog_entry()
        if not isinstance(entry, PlotCatalogEntry):
            raise TypeError(f"{recipe!r}.catalog_entry() must return a PlotCatalogEntry")
        self._plots.add(entry.id, entry.version, recipe, replace=replace)
        return recipe

    def register(self, obj: Any, *, replace: bool = False) -> None:
        """Register a step, model or plot recipe (or an iterable of them)."""
        if isinstance(obj, type) and issubclass(obj, Step):
            self.register_step(obj, replace=replace)
        elif isinstance(obj, type) and hasattr(obj, "manifest") and hasattr(obj, "score"):
            self.register_model(obj, replace=replace)
        elif hasattr(obj, "catalog_entry") and hasattr(obj, "render"):
            self.register_plot(obj, replace=replace)
        elif isinstance(obj, Iterable) and not isinstance(obj, str | bytes):
            for item in obj:
                self.register(item, replace=replace)
        else:
            raise TypeError(f"cannot register {obj!r}: not a step, model or plot recipe")

    # ----------------------------------------------------------------- discovery
    def load_entry_points(
        self, groups: Iterable[str] = ENTRY_POINT_GROUPS, *, strict: bool = False
    ) -> list[str]:
        """Load and register every entry point in ``groups``; return the errors.

        Nothing an entry point exports may replace what is already registered (the first
        registration wins, whatever the iteration order of installed distributions), and
        ids in the built-in namespaces (:data:`BUILTIN_NAMESPACES`) are only accepted from
        the built-in distributions. Collisions are reported in ``load_errors`` (raised
        with ``strict``).
        """
        errors: list[str] = []
        for group in groups:
            for ep in importlib.metadata.entry_points(group=group):
                dist = getattr(getattr(ep, "dist", None), "name", None)
                try:
                    obj = ep.load()
                    if callable(obj) and not isinstance(obj, type):
                        obj = obj()
                    for item in _flatten(obj):
                        self._register_entry(item, dist)
                except Exception as exc:  # report and keep going: one bad plugin
                    msg = f"{group}:{ep.name}: {type(exc).__name__}: {exc}"
                    if strict:
                        raise RuntimeError(msg) from exc
                    errors.append(msg)
        self.load_errors += errors
        return errors

    def _register_entry(self, obj: Any, dist: str | None) -> None:
        pid = _plugin_id(obj)
        namespace = pid.split(".", 1)[0] if pid and "." in pid else None
        if namespace in BUILTIN_NAMESPACES and dist not in BUILTIN_DISTRIBUTIONS:
            raise ValueError(
                f"{pid!r} is in the reserved {namespace!r} namespace of the built-in plugins "
                f"(exported by {dist or 'an unknown distribution'!r})"
            )
        self.register(obj, replace=False)

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

    def copy(self) -> Registry:
        new = Registry()
        for kind_from, kind_to in (
            (self._steps, new._steps),
            (self._models, new._models),
            (self._plots, new._plots),
        ):
            kind_to.items = {k: dict(v) for k, v in kind_from.items.items()}
        return new

    # ------------------------------------------------------------------- export
    def catalog(self) -> Catalog:
        """Static catalog JSON for the control plane (no code, no data)."""
        return Catalog(
            steps=[s.catalog_entry() for s in self.steps()],
            models=[ModelCatalogEntry(manifest=m.manifest()) for m in self.models()],
            plots=[p.catalog_entry() for p in self.plots()],
        )


_default: Registry | None = None
_lock = threading.Lock()


def default_registry(
    *, reload: bool = False, loader: Callable[[Registry], Any] | None = None
) -> Registry:
    """The process-wide registry, populated from entry points on first use."""
    global _default
    with _lock:
        if _default is None or reload:
            reg = Registry()
            (loader or (lambda r: r.load_entry_points()))(reg)
            _default = reg
        return _default
