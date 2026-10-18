"""The recipe contract: ``(DataTree-like input, settings) -> PlotSpec``.

A recipe is a plain function decorated with :func:`recipe`. The function receives a
:class:`RecipeInput` (the normalised ``xr.DataTree`` plus group lookup helpers) and a validated
Pydantic settings object, and returns a :class:`Figure`: an Altair chart that reads only
named data (``alt.Data(name=...)``), the DataFrames behind those names and a plain-language
caption. :meth:`Recipe.render` turns that into a sanitized :class:`PlotSpec` with recipe
provenance - datasets travel as inline Arrow, never as URLs.

A recipe can also return a Plotly figure (``plotly.graph_objects.Figure`` or a plain
``{data, layout, frames}`` dict). It goes through :func:`~mapi_plots.convert.from_plotly`
with the same provenance, caption and persona. Its data arrays become Arrow datasets; a
recipe can also pass its own DataFrames in ``datasets`` and point traces at their columns
with ``{"$ref": "<dataset>.<column>"}`` (one shared table instead of one per trace).

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
from mapi_spec.about import About
from mapi_spec.capabilities import Representation
from mapi_spec.catalog import PlotCatalogEntry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plots import PlotBindings, PlotSpec
from mapi_spec.spec import Binding
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mapi_plots.convert import from_altair, from_plotly

__all__ = [
    "ExampleFn",
    "Figure",
    "Recipe",
    "RecipeDataError",
    "RecipeInput",
    "RecipeSettings",
    "VlType",
    "as_tree",
    "is_plotly_figure",
    "named",
    "plotly_plot",
    "recipe",
    "wrap_facet",
]

Persona = Literal["guided", "expert", "both"]
VlType = Literal["quantitative", "temporal", "nominal", "ordinal"]


def named(name: str) -> Any:
    """``alt.Data(name=...)``: a reference to one of the figure's named datasets."""
    data_cls: Any = alt.Data  # altair's generated classes are untyped
    return data_cls(name=name)


def wrap_facet(
    layer: Any,
    field: str,
    levels: Iterable[Any],
    *,
    columns: int,
    title: str | None = None,
    data: Any = None,
    independent_y: bool = False,
) -> Any:
    """``layer`` wrapped into one panel per level of ``field``, in the given order.

    Every panel draws its own x axis (and y axis): with a shared axis, Vega-Lite puts the
    x axis only under the bottom row, so the top row has no labels and, when the last row
    is short, the axis floats under an empty cell. There are never more columns than panels.
    """
    order = [str(v) for v in dict.fromkeys(levels)]
    kw: dict[str, Any] = {} if data is None else {"data": data}
    facet = alt.Facet(f"{field}:N", title=title, sort=order)
    chart = layer.facet(facet=facet, columns=max(1, min(columns, len(order))), **kw)
    if independent_y:
        chart = chart.resolve_scale(y="independent")
    return chart.resolve_axis(x="independent", y="independent")


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
    """What a recipe function returns.

    ``chart`` is an Altair chart (or a Vega-Lite dict), or a Plotly figure (``go.Figure`` or a
    ``{data, layout, frames}`` dict). ``kind`` tells them apart; ``auto`` detects it.
    """

    chart: Any
    datasets: dict[str, pd.DataFrame]
    caption: str
    title: str | None = None
    bindings: PlotBindings | None = None
    kind: Literal["auto", "vega-lite", "plotly"] = "auto"

    @property
    def is_plotly(self) -> bool:
        if self.kind != "auto":
            return self.kind == "plotly"
        return is_plotly_figure(self.chart)


def is_plotly_figure(chart: Any) -> bool:
    """A ``plotly`` figure object, or a dict shaped like one (a ``data`` list of traces)."""
    if type(chart).__module__.startswith("plotly."):
        return True
    return (
        isinstance(chart, Mapping)
        and isinstance(chart.get("data"), list)
        and not ({"mark", "layer", "$schema", "encoding"} & set(chart))
    )


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

    def dim_label(self, dim: str) -> str:
        """What the dataset calls the canonical dim ``dim`` (``data.bind`` maps a spec's
        ``geo`` to the analyst's ``market``): read from the bundle's ``/binding`` node, else
        ``dim`` itself."""
        for path in ("binding", "model/binding"):
            try:
                node = self.tree[path]
            except KeyError:
                continue
            raw = node.attrs.get("json") if isinstance(node, xr.DataTree) else None
            if isinstance(raw, str):
                try:
                    return Binding.model_validate_json(raw).dataset_of().get(dim, dim)
                except ValidationError:
                    return dim
        return dim

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
ExampleFn = Callable[[], tuple[Any, Mapping[str, Any]]]
"""Synthetic ``(data, options)`` a recipe renders for its catalog example."""


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
    about: About | None = None
    example: ExampleFn | None = field(default=None, compare=False)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def catalog_entry(self) -> PlotCatalogEntry:
        from mapi_spec.plot_abouts import PLOT_ABOUTS

        return PlotCatalogEntry(
            id=self.id,
            version=self.version,
            title=self.title,
            persona=self.persona,
            requires_representation=list(self.requires_representation),
            requires_groups=list(self.requires_groups),
            summary=self.summary[:300] or None,
            about=self.about or PLOT_ABOUTS.get(self.id),
        )

    def example_spec(self) -> PlotSpec | None:
        """The recipe rendered on its synthetic example data (``None`` when it has none):
        ``example`` if given, else the bundled one (:mod:`mapi_plots.examples`)."""
        if self.example is not None:
            data, options = self.example()
        else:
            from mapi_plots.examples import example_inputs

            found = example_inputs().get(self.id)
            if found is None:
                return None
            data, options = found
        return self.render(data, **options)

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
        if fig.is_plotly:
            return plotly_plot(
                fig.chart,
                id=self.id,
                title=settings.title or fig.title or self.title,
                datasets=fig.datasets,
                persona=self.persona,
                caption_plain=fig.caption[:4000] if fig.caption else None,
                bindings=fig.bindings,
                recipe=self.ref,
                bundle_id=inp.bundle_id,
            )
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


def plotly_plot(
    fig: Any,
    *,
    id: str,
    title: str,
    datasets: Mapping[str, pd.DataFrame] | None,
    persona: Persona,
    caption_plain: str | None,
    bindings: PlotBindings | None,
    recipe: str,
    bundle_id: str | None,
) -> PlotSpec:
    """:func:`~mapi_plots.convert.from_plotly` plus named DataFrames the figure refers to.

    Without ``datasets`` this is ``from_plotly``. With them, the figure's ``$ref`` nodes point
    at those tables; inline arrays still move into ``trace<i>`` datasets, and string columns
    get the same inline-text rules before the whole document goes through the sanitizer.
    """
    kw: dict[str, Any] = {
        "id": id,
        "title": title,
        "persona": persona,
        "caption_plain": caption_plain,
        "bindings": bindings,
        "recipe": recipe,
        "bundle_id": bundle_id,
    }
    if not datasets:
        return from_plotly(fig, **kw)
    import json

    import plotly.io as pio

    from mapi_plots import convert as cv

    raw = json.loads(pio.to_json(fig, validate=False, pretty=False))
    spec: dict[str, Any] = {k: raw[k] for k in ("data", "layout", "frames", "config") if k in raw}
    layout = spec.get("layout")
    if isinstance(layout, dict):
        layout.pop("template", None)
    ex = cv._PlotlyExtractor(1)
    spec["data"] = [
        ex.trace(tr, f"trace{i}") if isinstance(tr, dict) else tr
        for i, tr in enumerate(spec.get("data", []))
    ]
    for f, frame in enumerate(spec.get("frames", []) or []):
        if isinstance(frame, dict) and isinstance(frame.get("data"), list):
            frame["data"] = [
                ex.trace(tr, f"frame{f}_trace{i}") if isinstance(tr, dict) else tr
                for i, tr in enumerate(frame["data"])
            ]
    text = {
        name: {
            str(c): df[c].tolist()
            for c in df.columns
            if df[c].dtype == object or pd.api.types.is_string_dtype(df[c].dtype)
        }
        for name, df in datasets.items()
    }
    cv._sanitize_text_columns({**ex.datasets, **text})
    tables: dict[str, Any] = {name: cv._frame(cols) for name, cols in ex.datasets.items()}
    for name, df in datasets.items():
        if name in tables:
            raise ValueError(f"dataset name {name!r} is used twice")
        tables[name] = df.assign(**text[name]) if text[name] else df
    return cv._finish(
        id=id,
        title=title,
        kind="plotly",
        spec=spec,
        datasets=tables,
        schema_version=cv.PLOTLY_JS_VERSION,
        persona=persona,
        caption_plain=caption_plain,
        bindings=bindings,
        provenance=cv.make_provenance(recipe=recipe, bundle_id=bundle_id),
        sanitize=True,
        compact=True,
    )


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
    about: About | None = None,
    example: ExampleFn | None = None,
) -> Callable[[Callable[[RecipeInput, Any], Figure]], Recipe[Any]]:
    """Decorator turning ``fn(inp, settings) -> Figure`` into a registered :class:`Recipe`.

    ``about`` is the plot's About card (built-in recipes take theirs from
    :data:`mapi_spec.plot_abouts.PLOT_ABOUTS`); ``example`` returns synthetic ``(data,
    options)`` for the catalog's example render."""

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
            about=about,
            example=example,
        )
        _REGISTRY[id] = r
        return r

    return wrap


def registered() -> dict[str, Recipe[Any]]:
    """Every recipe defined so far (module import registers them)."""
    return dict(_REGISTRY)
