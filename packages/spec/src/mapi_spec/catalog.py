"""Static catalog entries for steps, models and plots (exported per image digest).

The control plane validates specs against these JSON shapes only — it never imports the
step or model code that produced them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from mapi_spec._types import StrictModel
from mapi_spec.capabilities import ModelManifest, Representation, check_json_schema
from mapi_spec.effects import (
    ContextNeed,
    SchemaEffect,
    StepKind,
    check_effect_template,
    resolve_context,
    resolve_effect,
)
from mapi_spec.refs import UseRef, Version

__all__ = [
    "Catalog",
    "ModelCatalogEntry",
    "PlotCatalogEntry",
    "StepCatalogEntry",
]


def _check_id(v: str) -> str:
    UseRef.parse(v)
    if "@" in v:
        raise ValueError("catalog ids must not carry a version range")
    return v


def _check_version(v: str) -> str:
    Version.parse(v)
    return v


class StepCatalogEntry(StrictModel):
    """Everything static validation needs to know about one step version.

    ``effect`` and ``context`` are JSON templates over the step's settings (see
    :mod:`mapi_spec.template`). ``var_params``/``dim_params`` name settings fields whose
    values are variable/dim names that must exist at the step's input stage, and
    ``side_table_params`` name fields that refer to ``data.side_tables`` entries.
    """

    id: str
    version: str
    kind: StepKind
    stateful: bool = Field(..., description="Has a fitted State (fit on train only)")
    invertible: bool = False
    depends_on_target: bool = False
    target_vars_param: str | None = Field(
        None,
        description="If set, the step depends on the target only when this settings field "
        "lists a target-role variable",
    )
    settings_schema: dict[str, JsonValue] = Field(default_factory=dict)
    effect: dict[str, JsonValue] = Field(default_factory=dict)
    context: dict[str, JsonValue] | None = None
    var_params: list[str] = Field(default_factory=list)
    dim_params: list[str] = Field(default_factory=list)
    side_table_params: list[str] = Field(default_factory=list)
    provides: list[Literal["scaling", "carryover"]] = Field(
        default_factory=list,
        description="Preprocessing a model may declare it owns (ModelManifest owns); such a "
        "step is rejected on the target/media variables of a model that owns it",
    )
    summary: str | None = Field(None, max_length=500)
    summary_plain: str | None = Field(None, max_length=500)
    yanked: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return _check_version(v)

    @field_validator("settings_schema")
    @classmethod
    def _schema(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return check_json_schema(v)

    @field_validator("effect")
    @classmethod
    def _effect(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        check_effect_template(v)
        return v

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def resolved_effect(self, settings: dict[str, Any]) -> SchemaEffect:
        return resolve_effect(self.effect, settings)

    def resolved_context(self, settings: dict[str, Any]) -> ContextNeed | None:
        return resolve_context(self.context, settings)


class ModelCatalogEntry(StrictModel):
    """A model plugin version: its manifest plus registry metadata."""

    manifest: ModelManifest
    base_image: str | None = Field(None, max_length=256, examples=["base-bayes@2026.10"])
    overlay_hash: str | None = Field(None, pattern=r"^sha256:[0-9a-f]{64}$")
    yanked: bool = False

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


class PlotCatalogEntry(StrictModel):
    """A plot recipe the runner can produce."""

    id: str
    version: str = "1.0.0"
    title: str = Field(..., max_length=200)
    persona: Literal["guided", "expert", "both"] = "both"
    requires_representation: list[Representation] = Field(default_factory=list)
    requires_groups: list[str] = Field(default_factory=list)
    yanked: bool = False

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return _check_version(v)


class Catalog(StrictModel):
    """A set of catalog entries with semver resolution helpers."""

    steps: list[StepCatalogEntry] = Field(default_factory=list)
    models: list[ModelCatalogEntry] = Field(default_factory=list)
    plots: list[PlotCatalogEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Catalog:
        for kind, refs in (
            ("step", [e.ref for e in self.steps]),
            ("model", [e.ref for e in self.models]),
            ("plot", [e.ref for e in self.plots]),
        ):
            if len(refs) != len(set(refs)):
                raise ValueError(f"duplicate {kind} catalog entries")
        return self

    @staticmethod
    def _resolve(entries: Iterable[Any], ref: UseRef | str) -> Any | None:
        r = UseRef.parse(ref) if isinstance(ref, str) else ref
        candidates = {Version.parse(e.version): e for e in entries if e.id == r.id and not e.yanked}
        best = r.resolve(candidates)
        return None if best is None else candidates[best]

    def resolve_step(self, ref: UseRef | str) -> StepCatalogEntry | None:
        """Highest non-yanked step version satisfying the ref's range."""
        out: StepCatalogEntry | None = self._resolve(self.steps, ref)
        return out

    def resolve_model(self, ref: UseRef | str) -> ModelCatalogEntry | None:
        out: ModelCatalogEntry | None = self._resolve(self.models, ref)
        return out

    def resolve_plot(self, ref: UseRef | str) -> PlotCatalogEntry | None:
        out: PlotCatalogEntry | None = self._resolve(self.plots, ref)
        return out

    def versions(self, kind: Literal["step", "model", "plot"], id_: str) -> list[str]:
        by_kind: dict[str, list[Any]] = {
            "step": list(self.steps),
            "model": list(self.models),
            "plot": list(self.plots),
        }
        entries = by_kind[kind]
        return sorted((e.version for e in entries if e.id == id_), key=Version.parse)
