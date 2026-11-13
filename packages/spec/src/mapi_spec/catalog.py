"""Static catalog entries for steps, models and plots (exported per image digest).

The control plane validates specs against these JSON shapes only — it never imports the
step or model code that produced them.

v0.18 (additive; every new field is left out of the JSON when empty, so the digest of a
catalog without extensions does not change): ``provider`` on entries (the distribution of a
non-built-in plugin), plot ``settings_schema``/``tags``/``pack_label``, plugin workflow node
contracts and templates (:class:`TemplateCatalogEntry`), and :class:`CatalogDiagnostic`
rows for everything that could not be loaded.

v0.19 (additive, left out when ``None``): ``overlay_hash`` on step, plot and template entries
(models had it already) - the sha256 of the uploaded wheel an entry of the tenant catalog view
comes from; its ``provider`` is then ``upload:<plugin version id>``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final, Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from mapi_spec._types import Slug, StrictModel
from mapi_spec.about import About, about_issues
from mapi_spec.capabilities import ModelManifest, Representation, check_json_schema
from mapi_spec.effects import (
    ContextNeed,
    SchemaEffect,
    StepKind,
    check_effect_template,
    resolve_context,
    resolve_effect,
)
from mapi_spec.errors import ErrorCode, Issue, Severity, json_pointer
from mapi_spec.namespaces import PLATFORM_RESERVED, uploaded_namespace_of
from mapi_spec.refs import UseRef, Version
from mapi_spec.workflow_nodes import NodeImplementation

__all__ = [
    "CATALOG_DIAGNOSTICS_MAX",
    "DIAGNOSTICS_PER_DISTRIBUTION_MAX",
    "Catalog",
    "CatalogDiagnostic",
    "DiagnosticCode",
    "ModelCatalogEntry",
    "PlotCatalogEntry",
    "StepCatalogEntry",
    "TemplateCatalogEntry",
    "catalog_about_issues",
]

DiagnosticCode = Literal[
    "load_error",
    "namespace_reserved",
    "id_collision",
    "schema_unsafe",
    "about_incomplete",
    "node_contract_invalid",
    "template_invalid",
    "deprecated",
]
CATALOG_DIAGNOSTICS_MAX: Final = 500
DIAGNOSTICS_PER_DISTRIBUTION_MAX: Final = 20
"""Caps of :attr:`Catalog.diagnostics` (in total, and per distribution: the exporter keeps
the first ones and adds one row saying how many more there were)."""


def _none(v: object) -> bool:
    return v is None


def _empty(v: object) -> bool:
    return not v


class CatalogDiagnostic(StrictModel):
    """Something the runner could not put into the catalog, or put there with a change.

    ``message`` is platform-authored plain text; anything a plugin wrote (an exception
    message, an id it exported) goes in ``untrusted_text`` and is shown as text only.
    """

    code: DiagnosticCode
    group: str = Field(
        ..., max_length=64, description="The entry-point group (mapi.models, ...) or the check"
    )
    entry_point: str | None = Field(None, max_length=128)
    distribution: str | None = Field(None, max_length=128)
    id: str | None = Field(None, max_length=128, description="The catalog id concerned")
    message: str = Field(..., max_length=500, description="Trusted, platform-authored text")
    untrusted_text: str | None = Field(
        None, max_length=500, description="Plugin-originated text; render as text only"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "code": "namespace_reserved",
                    "group": "mapi.models",
                    "entry_point": "acme",
                    "distribution": "acme-models",
                    "id": "mmm.acme_mix",
                    "message": "The namespace 'mmm' belongs to the built-in plugins.",
                }
            ]
        }
    )


_TEMPLATE_ID = r"^[a-z][a-z0-9_]{0,63}$"
_SHA256 = r"^sha256:[0-9a-f]{64}$"


class TemplateCatalogEntry(StrictModel):
    """A plugin workflow template (entry-point group ``mapi.workflow_templates``).

    The sibling of :class:`~mapi_spec.workflow_templates.WorkflowTemplate` (which this module
    cannot import: ``workflow`` imports the catalog). The id is ``<namespace>_<name>`` so it
    fits ``POST /v1/workflows {template}``; ``document`` is the workflow document, validated
    against the merged node contracts when the control plane loads the catalog.
    """

    id: str = Field(..., pattern=_TEMPLATE_ID)
    version: str = "1.0.0"
    about: About
    document: dict[str, JsonValue]
    tags: list[Slug] = Field(default_factory=list, max_length=8, exclude_if=_empty)
    provider: str | None = Field(None, max_length=128, exclude_if=_none)
    overlay_hash: str | None = Field(None, pattern=_SHA256, exclude_if=_none)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        head, sep, name = v.partition("_")
        if not sep or not name:
            raise ValueError("a plugin template id is '<namespace>_<name>'")
        # v0.19: an uploaded template ('t_<code>_<short>_<name>'); whether the exporter may
        # use that namespace is decided by the loader (check_namespace(..., claimed=...))
        if uploaded_namespace_of(v) is not None:
            return v
        if head in PLATFORM_RESERVED:
            raise ValueError(f"the namespace {head!r} is reserved for the platform")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return _check_version(v)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


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
    about: About | None = Field(
        None,
        description="M3.5: the plain-language About card (the catalog rule requires one)",
        exclude_if=lambda v: v is None,
    )
    provider: str | None = Field(
        None,
        max_length=128,
        description="v0.18: the distribution that installed a non-built-in step",
        exclude_if=_none,
    )
    overlay_hash: str | None = Field(
        None,
        pattern=_SHA256,
        description="v0.19: the sha256 of the uploaded wheel the step comes from",
        exclude_if=_none,
    )

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
    provider: str | None = Field(
        None,
        max_length=128,
        description="v0.18: the distribution that installed a non-built-in model",
        exclude_if=_none,
    )

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def about(self) -> About | None:
        """The manifest's About card (M3.5)."""
        return self.manifest.about


class PlotCatalogEntry(StrictModel):
    """A plot recipe the runner can produce."""

    id: str
    version: str = "1.0.0"
    title: str = Field(..., max_length=200)
    persona: Literal["guided", "expert", "both"] = "both"
    requires_representation: list[Representation] = Field(default_factory=list)
    requires_groups: list[str] = Field(default_factory=list)
    yanked: bool = False
    summary: str | None = Field(
        None,
        max_length=300,
        description="One line on what the chart shows (the recipe's summary)",
        exclude_if=lambda v: v is None,
    )
    about: About | None = Field(
        None,
        description="The plot's About card: what it shows, how to read it, what it needs",
        exclude_if=lambda v: v is None,
    )
    provider: str | None = Field(
        None,
        max_length=128,
        description="v0.18: the distribution that installed a non-built-in recipe",
        exclude_if=_none,
    )
    settings_schema: dict[str, JsonValue] | None = Field(
        None,
        description="v0.18: the recipe's settings as a safe JSON Schema (at most 16 KiB; "
        "absent when too large)",
        exclude_if=_none,
    )
    tags: list[Slug] = Field(default_factory=list, max_length=8, exclude_if=_empty)
    pack_label: str | None = Field(
        None,
        max_length=40,
        description="v0.18: the name of the recipe pack shown on the catalog card",
        exclude_if=_none,
    )
    overlay_hash: str | None = Field(
        None,
        pattern=_SHA256,
        description="v0.19: the sha256 of the uploaded wheel the recipe comes from",
        exclude_if=_none,
    )

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

    @field_validator("settings_schema")
    @classmethod
    def _schema(cls, v: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        return None if v is None else check_json_schema(v)


class Catalog(StrictModel):
    """A set of catalog entries with semver resolution helpers.

    ``workflow_nodes`` and ``workflow_templates`` hold only *plugin* contributions (the
    built-in nodes and templates are static in :mod:`mapi_spec`); ``diagnostics`` lists what
    the runner could not load. All three are left out of the JSON when empty.
    """

    steps: list[StepCatalogEntry] = Field(default_factory=list)
    models: list[ModelCatalogEntry] = Field(default_factory=list)
    plots: list[PlotCatalogEntry] = Field(default_factory=list)
    workflow_nodes: list[NodeImplementation] = Field(default_factory=list, exclude_if=_empty)
    workflow_templates: list[TemplateCatalogEntry] = Field(default_factory=list, exclude_if=_empty)
    diagnostics: list[CatalogDiagnostic] = Field(
        default_factory=list, max_length=CATALOG_DIAGNOSTICS_MAX, exclude_if=_empty
    )

    @model_validator(mode="after")
    def _unique(self) -> Catalog:
        for kind, refs in (
            ("step", [e.ref for e in self.steps]),
            ("model", [e.ref for e in self.models]),
            ("plot", [e.ref for e in self.plots]),
            ("workflow node", [e.id for e in self.workflow_nodes]),
            ("workflow template", [e.id for e in self.workflow_templates]),
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

    def about_issues(self, *, extra_ids: Iterable[str] = ()) -> list[Issue]:
        """The catalog rule (M3.5): every non-yanked step and model has a complete About.

        See :func:`catalog_about_issues`."""
        return catalog_about_issues(self, extra_ids=extra_ids)


def catalog_about_issues(
    catalog: Catalog, *, extra_ids: Iterable[str] = (), builtins: bool = True
) -> list[Issue]:
    """The catalog rule (M3.5): an ``ABOUT_INCOMPLETE`` **error** for every non-yanked step
    and model of ``catalog`` whose About is missing or incomplete
    (:func:`mapi_spec.about.about_issues`), and a **warning** for each ``related`` id that
    names no known item, located by JSON pointers into the catalog document.

    Known ids are the catalog's own, ``extra_ids`` and (``builtins``) every built-in card's id
    (:func:`mapi_spec.abouts.builtin_abouts`: splits, templates, workflow nodes, checks)."""
    known = (
        {e.id for e in catalog.steps}
        | {e.id for e in catalog.models}
        | {e.id for e in catalog.plots}
        | set(extra_ids)
    )
    if builtins:
        from mapi_spec.abouts import builtin_abouts

        known |= set(builtin_abouts())
    out: list[Issue] = []
    entries: list[tuple[str, int, str, About | None, bool]] = [
        ("steps", i, e.ref, e.about, e.yanked) for i, e in enumerate(catalog.steps)
    ] + [("models", i, e.ref, e.manifest.about, e.yanked) for i, e in enumerate(catalog.models)]
    for group, i, ref, about, yanked in entries:
        if yanked:
            continue
        pointer = (
            json_pointer(group, i, "about")
            if group == "steps"
            else json_pointer(group, i, "manifest", "about")
        )
        for problem in about_issues(about):
            out.append(
                Issue(
                    code=ErrorCode.ABOUT_INCOMPLETE,
                    severity=Severity.ERROR,
                    pointer=pointer,
                    message=f"{ref}: {problem}",
                    suggested_action="Write the About card in plain language (see "
                    "docs/concepts/about-and-choosing.md).",
                )
            )
        unknown = [r for r in (about.related if about else []) if r not in known]
        if unknown:
            out.append(
                Issue(
                    code=ErrorCode.ABOUT_INCOMPLETE,
                    severity=Severity.WARNING,
                    pointer=pointer + "/related",
                    message=f"{ref}: related names items that are not installed: {unknown}",
                )
            )
    return out
