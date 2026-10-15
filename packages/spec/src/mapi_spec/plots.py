"""The PlotSpec envelope: a Vega-Lite or Plotly spec plus named datasets and provenance.

Datasets are referenced **by name only** (Vega-Lite ``data: {name: ...}``, Plotly
``{"$ref": "name.column"}``) and never by URL. The full plot sanitiser (schema validation,
HTML stripping, URL rejection everywhere) lives in ``mapi_plots``; this module enforces the
envelope contract: shapes, name references and the no-URL data rule.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from mapi_spec._types import StrictModel
from mapi_spec.schema import Role

__all__ = [
    "DataRef",
    "InlineArrow",
    "PlotBindings",
    "PlotDataset",
    "PlotProvenance",
    "PlotSpec",
    "referenced_datasets",
]

MAX_INLINE_ARROW_B64 = 2 * 1024 * 1024
_PATH_RE = r"^[A-Za-z0-9_.\-][A-Za-z0-9_.\-/]{0,511}$"
_DATASET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]{0,63}$")
_BINDING_RE = r"^(dim|role|var|coord|stat):[A-Za-z_][A-Za-z0-9_.\-]{0,127}$"


class DataRef(StrictModel):
    """A dataset stored next to the bundle (path relative to the bundle root)."""

    kind: Literal["data_ref"]
    path: str = Field(..., pattern=_PATH_RE, examples=["ui/plotdata/contrib.arrow"])
    format: Literal["arrow", "json"] = "arrow"
    size: int | None = Field(None, ge=0)
    crc32c: str | None = Field(None, pattern=r"^[A-Za-z0-9+/]{6}==$")

    @field_validator("path")
    @classmethod
    def _relative_path(cls, v: str) -> str:
        if any(part in ("", ".", "..") for part in v.split("/")):
            raise ValueError("dataset paths may not contain empty, '.' or '..' segments")
        return v


class InlineArrow(StrictModel):
    """A small Arrow IPC stream embedded as base64."""

    kind: Literal["inline_arrow"]
    data: str = Field(..., max_length=MAX_INLINE_ARROW_B64, pattern=r"^[A-Za-z0-9+/]*={0,2}$")
    rows: int | None = Field(None, ge=0)


PlotDataset = Annotated[DataRef | InlineArrow, Field(discriminator="kind")]

BindingExpr = Annotated[str, Field(pattern=_BINDING_RE)]


class PlotBindings(StrictModel):
    """Role/dim template bindings, e.g. ``{x: "dim:time", y: "role:target"}``.

    Templates bound this way apply to every model of a family, not one bundle.
    """

    x: BindingExpr | None = None
    y: BindingExpr | None = None
    y2: BindingExpr | None = None
    color: BindingExpr | None = None
    facet: BindingExpr | None = None
    row: BindingExpr | None = None
    column: BindingExpr | None = None
    size: BindingExpr | None = None
    shape: BindingExpr | None = None
    band_lower: BindingExpr | None = None
    band_upper: BindingExpr | None = None
    tooltip: list[BindingExpr] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _roles(self) -> PlotBindings:
        exprs = [v for k, v in self if k != "tooltip" and isinstance(v, str)]
        for expr in [*exprs, *self.tooltip]:
            kind, _, name = expr.partition(":")
            if kind == "role" and name not in Role._value2member_map_:
                raise ValueError(f"unknown role {name!r} in binding {expr!r}")
        return self


class PlotProvenance(StrictModel):
    """Who produced a plot: exactly one of ``recipe``, ``plugin`` or ``author``."""

    bundle_id: str | None = Field(None, max_length=128)
    recipe: str | None = Field(None, max_length=128)
    plugin: str | None = Field(None, max_length=128)
    author: str | None = Field(None, max_length=128)
    generated_at: datetime

    @model_validator(mode="after")
    def _exactly_one(self) -> PlotProvenance:
        n = sum(x is not None for x in (self.recipe, self.plugin, self.author))
        if n != 1:
            raise ValueError("provenance needs exactly one of recipe, plugin or author")
        return self

    @property
    def source(self) -> Literal["recipe", "plugin", "author"]:
        if self.recipe is not None:
            return "recipe"
        return "plugin" if self.plugin is not None else "author"


def referenced_datasets(kind: str, spec: JsonValue) -> tuple[set[str], list[str]]:
    """Dataset names a spec refers to, plus any URL-data violations (JSON pointers)."""
    names: set[str] = set()
    violations: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if kind == "vega-lite" and isinstance(node.get("data"), dict):
                data = node["data"]
                if "url" in data:
                    violations.append(f"{path}/data/url")
                if isinstance(data.get("name"), str):
                    names.add(data["name"])
            if kind == "plotly" and isinstance(node.get("$ref"), str) and len(node) == 1:
                names.add(node["$ref"].split(".", 1)[0])
            for k, v in node.items():
                walk(v, f"{path}/{str(k).replace('~', '~0').replace('/', '~1')}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}/{i}")

    walk(spec, "/spec")
    return names, violations


class PlotSpec(StrictModel):
    """A renderable plot. ``spec`` is Vega-Lite JSON or Plotly ``{data, layout, ...}``."""

    id: str = Field(..., pattern=r"^[A-Za-z0-9_.\-]{1,128}$")
    title: str = Field(..., max_length=300)
    kind: Literal["vega-lite", "plotly"]
    spec: dict[str, JsonValue]
    datasets: dict[str, PlotDataset] = Field(default_factory=dict, max_length=64)
    bindings: PlotBindings | None = None
    schema_version: str = Field(..., pattern=r"^v?\d+(\.\d+){0,2}$", examples=["v5", "2.35.2"])
    persona: Literal["guided", "expert", "both"] = "both"
    caption_plain: str | None = Field(None, max_length=4000)
    provenance: PlotProvenance

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "core.ppc_fan",
                    "title": "Posterior predictive fan",
                    "kind": "vega-lite",
                    "spec": {
                        "mark": "area",
                        "data": {"name": "fan"},
                        "encoding": {
                            "x": {"field": "time", "type": "temporal"},
                            "y": {"field": "lo", "type": "quantitative"},
                            "y2": {"field": "hi"},
                        },
                    },
                    "datasets": {"fan": {"kind": "data_ref", "path": "ui/plotdata/ppc_fan.arrow"}},
                    "schema_version": "v5",
                    "persona": "both",
                    "caption_plain": "The shaded band is where the model expects 90% of weeks.",
                    "provenance": {
                        "recipe": "core.ppc_fan@1.0.0",
                        "generated_at": "2026-10-15T09:40:00Z",
                    },
                }
            ]
        }
    )

    @field_validator("datasets")
    @classmethod
    def _dataset_names(cls, v: dict[str, Any]) -> dict[str, Any]:
        for name in v:
            if not _DATASET_NAME_RE.match(name):
                raise ValueError(f"invalid dataset name {name!r}")
        return v

    @model_validator(mode="after")
    def _references(self) -> PlotSpec:
        names, violations = referenced_datasets(self.kind, self.spec)
        if violations:
            raise ValueError(f"datasets must be referenced by name, not URL: {violations[:5]}")
        declared = set(self.datasets)
        inline = self.spec.get("datasets")
        if self.kind == "vega-lite" and isinstance(inline, dict):
            declared |= set(inline)  # Vega-Lite top-level inline datasets
        missing = sorted(names - declared)
        if missing:
            raise ValueError(f"spec references undeclared datasets {missing}")
        return self
