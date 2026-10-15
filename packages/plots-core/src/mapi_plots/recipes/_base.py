"""The recipe contract: ``(DataTree-like input, settings) -> PlotSpec``.

A recipe is a plain function decorated with :func:`recipe`. The function receives a
:class:`RecipeInput` (the normalised ``xr.DataTree`` plus group lookup helpers) and a validated
Pydantic settings object, and returns a :class:`Figure`: an Altair chart that reads only
named data (``alt.Data(name=...)``), the DataFrames behind those names and a plain-language
caption. :meth:`Recipe.render` turns that into a sanitized :class:`PlotSpec` with recipe
provenance - datasets travel as inline Arrow, never as URLs.

Recipe objects satisfy the SDK's ``PlotRecipe`` protocol (``catalog_entry()`` + ``render``),
so ``Registry.register`` accepts them and the ``mapi.plots`` entry points expose them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import altair as alt
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import Representation
from mapi_spec.catalog import PlotCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plots import PlotBindings, PlotSpec
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mapi_plots.convert import from_altair

__all__ = [
    "Figure",
    "Recipe",
    "RecipeDataError",
    "RecipeInput",
    "RecipeSettings",
    "VlType",
    "as_tree",
    "named",
    "recipe",
]

Persona = Literal["guided", "expert", "both"]
VlType = Literal["quantitative", "temporal", "nominal", "ordinal"]


def named(name: str) -> Any:
    """``alt.Data(name=...)``: a reference to one of the figure's named datasets."""
    return alt.Data(name=name)  # type: ignore[no-untyped-call]


class RecipeDataError(MapiError):
    """The input tree lacks what a recipe needs (a group, a variable, a dim)."""

    default_code = ErrorCode.SCHEMA_INVALID


class RecipeSettings(BaseModel):
    """Common recipe options. Subclasses add their own (extra keys are rejected)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = Field(None, max_length=300, description="Override the plot title")
    width: int = Field(480, ge=80, le=2000)
    height: int = Field(180, ge=60, le=2000)


@dataclass(slots=True)
class Figure:
    """What a recipe function returns."""

    chart: Any
    datasets: dict[str, pd.DataFrame]
    caption: str
    title: str | None = None
    bindings: PlotBindings | None = None


def as_tree(data: Any) -> xr.DataTree:
    """Normalise a DataTree, Dataset or ``{group: Dataset}`` mapping into a DataTree."""
    if isinstance(data, xr.DataTree):
        return data
    if isinstance(data, xr.Dataset):
        return xr.DataTree(dataset=data)
    if isinstance(data, xr.DataArray):
        return xr.DataTree(dataset=data.to_dataset(name=data.name or "value"))
    if isinstance(data, Mapping):
        nodes: dict[str, Any] = {}
        for k, v in data.items():
            path = "/" + str(k).strip("/")
            if isinstance(v, xr.DataTree):
                for sub in v.subtree:
                    rel = sub.relative_to(v)
                    key = path if rel == "." else f"{path}/{rel}"
                    nodes[key] = sub.to_dataset(inherit=False)
            elif isinstance(v, xr.DataArray):
                nodes[path] = v.to_dataset(name=v.name or "value")
            else:
                nodes[path] = v
        return xr.DataTree.from_dict(nodes)
    raise RecipeDataError(f"cannot plot a {type(data).__name__}; pass a DataTree or Dataset")


@dataclass(slots=True)
class RecipeInput:
    """The input tree plus lookups that tolerate bundle (``model/…``) and score layouts."""

    tree: xr.DataTree

    @property
    def attrs(self) -> dict[str, Any]:
        return {str(k): v for k, v in self.tree.attrs.items()}

    @property
    def bundle_id(self) -> str | None:
        bid = self.tree.attrs.get("bundle_id")
        return str(bid) if bid is not None else None

    def find(self, *names: str) -> xr.Dataset | None:
        """The first group found among ``names`` (``a/b`` paths allowed), searched at the
        root, under ``model/`` and then anywhere in the tree (shallowest first)."""
        for name in names:
            for path in (name, f"model/{name}", f"derived/{name}"):
                try:
                    node = self.tree[path]
                except KeyError:
                    continue
                if isinstance(node, xr.DataTree) and node.has_data:
                    return node.to_dataset()
        for name in names:
            leaf = name.rsplit("/", 1)[-1]
            hits = sorted(
                (n for n in self.tree.subtree if n.name == leaf and n.has_data),
                key=lambda n: n.depth,
            )
            for node in hits:
                if name.count("/") == 0 or node.path.endswith("/" + name):
                    return node.to_dataset()
        if self.tree.has_data and not self.tree.children:
            return self.tree.to_dataset()  # a bare Dataset is the group itself
        return None

    def require(self, *names: str) -> xr.Dataset:
        ds = self.find(*names)
        if ds is None:
            raise RecipeDataError(
                "the data has none of the groups " + ", ".join(repr(n) for n in names)
            )
        return ds

    def root(self) -> xr.Dataset | None:
        return self.tree.to_dataset() if self.tree.has_data else None

    def children_with(self, group: str) -> dict[str, xr.Dataset]:
        """``{child name: group dataset}`` for direct children that contain ``group``."""
        out: dict[str, xr.Dataset] = {}
        for name, child in self.tree.children.items():
            sub = RecipeInput(child).find(group)
            if sub is not None:
                out[str(name)] = sub
        return out


RecipeFn = Callable[[RecipeInput, Any], Figure]


@dataclass(frozen=True, slots=True)
class Recipe[S: RecipeSettings]:
    """A registered plot recipe."""

    id: str
    title: str
    settings: type[S]
    fn: Callable[[RecipeInput, S], Figure]
    version: str = "1.0.0"
    persona: Persona = "both"
    requires_groups: tuple[str, ...] = ()
    requires_representation: tuple[Representation, ...] = ()
    summary: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def catalog_entry(self) -> PlotCatalogEntry:
        return PlotCatalogEntry(
            id=self.id,
            version=self.version,
            title=self.title,
            persona=self.persona,
            requires_representation=list(self.requires_representation),
            requires_groups=list(self.requires_groups),
        )

    def parse_settings(self, options: Mapping[str, Any]) -> S:
        try:
            return self.settings.model_validate(dict(options))
        except ValidationError as exc:
            raise RecipeDataError(
                f"invalid settings for {self.id}: {exc.error_count()} error(s)",
                untrusted_text=str(exc)[:4000],
            ) from None

    def render(self, data: Any, **options: Any) -> PlotSpec:
        """Render the recipe for ``data`` (DataTree, Dataset or mapping of groups)."""
        settings = self.parse_settings(options)
        inp = RecipeInput(as_tree(data))
        fig = self.fn(inp, settings)
        chart = fig.chart
        if isinstance(chart, alt.VConcatChart | alt.HConcatChart | alt.ConcatChart) and (
            chart.resolve is alt.Undefined
        ):
            # panels encode different fields with colour; never merge their colour scales
            chart = chart.resolve_scale(color="independent")
        return from_altair(
            chart,
            id=self.id,
            title=settings.title or fig.title or self.title,
            datasets=fig.datasets,
            persona=self.persona,
            caption_plain=fig.caption[:4000] if fig.caption else None,
            bindings=fig.bindings,
            recipe=self.ref,
            bundle_id=inp.bundle_id,
            compact=True,
        )

    def __call__(self, data: Any, **options: Any) -> PlotSpec:
        return self.render(data, **options)


_REGISTRY: dict[str, Recipe[Any]] = {}


def recipe(
    id: str,
    *,
    title: str,
    settings: type[RecipeSettings] = RecipeSettings,
    version: str = "1.0.0",
    persona: Persona = "both",
    requires_groups: Iterable[str] = (),
    requires_representation: Iterable[Representation] = (),
    summary: str = "",
    tags: Iterable[str] = (),
) -> Callable[[Callable[[RecipeInput, Any], Figure]], Recipe[Any]]:
    """Decorator turning ``fn(inp, settings) -> Figure`` into a registered :class:`Recipe`."""

    def wrap(fn: Callable[[RecipeInput, Any], Figure]) -> Recipe[Any]:
        doc = (fn.__doc__ or "").strip()
        r: Recipe[Any] = Recipe(
            id=id,
            title=title,
            settings=settings,
            fn=fn,
            version=version,
            persona=persona,
            requires_groups=tuple(requires_groups),
            requires_representation=tuple(requires_representation),
            summary=summary or (doc.splitlines()[0] if doc else ""),
            tags=tuple(tags),
        )
        _REGISTRY[id] = r
        return r

    return wrap


def registered() -> dict[str, Recipe[Any]]:
    """Every recipe defined so far (module import registers them)."""
    return dict(_REGISTRY)
