"""The library (v0.18): analyst-made extensions stored as JSON documents, never code.

Four kinds of *library items* let every analyst add their own pieces without a sandbox for
tenant code. Each item is a DB row with immutable revisions; each revision holds one of the
documents below, which pure :mod:`mapi_spec` code compiles to things the platform already runs:

======================  =====================  ==============================================
item kind               document               compiles to (trusted)
======================  =====================  ==============================================
``model_recipe``        :class:`ModelRecipeDoc`  a spec pipeline + model block + binding
                                               (:mod:`mapi_spec.model_recipes`): a
                                               declarative GLM (``tabular.declarative_glm``)
                                               or a preset over any catalog model
``prep_preset``         :class:`PrepPresetDoc`   a list of ``StepCall`` with ``$slot:<name>``
                                               placeholders filled at use time
                                               (:mod:`mapi_spec.prep_presets`)
``chart_template``      :class:`ChartTemplateDoc`  a sanitized Vega-Lite PlotSpec over the
                                               datasets of a platform plot
                                               (``mapi_plots.chart_templates``)
``workflow_template``   :class:`WorkflowTemplateDoc`  a validated workflow document
======================  =====================  ==============================================

Nothing here executes tenant content: documents are closed Pydantic shapes, text bodies go
through the event-budgeted parser of :mod:`mapi_spec.loader`, and every user-authored string
(titles, summaries, labels) is untrusted text that is stored and returned verbatim and
rendered as text. Lineage is recorded with ``metadata.labels`` (:data:`LABEL_MODEL` ...),
never with a spec schema change.

This module is JSON-only (it runs in the control plane): no array libraries, no plugins.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import (
    Discriminator,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, dto_examples
from mapi_spec._types import LabelKey, LabelValue, Name, Slug, StrictModel, unique
from mapi_spec.errors import ErrorCode, Issue, MapiError
from mapi_spec.loader import Limits, parse_text, spec_hash, validation_issues
from mapi_spec.plots import PlotBindings, PlotSpec
from mapi_spec.refs import DatasetRef, UseRef
from mapi_spec.schema import Role
from mapi_spec.spec import MAX_PIPELINE_STEPS, ApiVersion, FitConfig, StepCall

__all__ = [
    "LABEL_MODEL",
    "LABEL_PREP",
    "LABEL_TEMPLATE",
    "LIBRARY_DOC_MAX_BYTES",
    "LIBRARY_ITEM_ID_RE",
    "MAX_ITEMS_PER_TENANT",
    "MAX_REVISIONS_PER_ITEM",
    "PLOT_SPEC_MAX_BYTES",
    "AboutLite",
    "ChartPreview",
    "ChartPreviewRequest",
    "ChartTemplateDoc",
    "DeclarativeModel",
    "GlmFamily",
    "GroupSpec",
    "InteractionSpec",
    "LibraryCreate",
    "LibraryDoc",
    "LibraryItem",
    "LibraryKind",
    "LibraryMetadata",
    "LibraryRef",
    "LibraryRevision",
    "LibraryRevisionCreate",
    "LibraryScope",
    "LibraryUseRequest",
    "LibraryUseResult",
    "LibraryValidateRequest",
    "LibraryValidation",
    "ModelRecipeDoc",
    "OutcomeSpec",
    "PrepPresetDoc",
    "PriorDist",
    "PriorSpec",
    "Slot",
    "TermSpec",
    "TransformCall",
    "TransformUse",
    "WorkflowTemplateDoc",
    "library_doc_hash",
    "library_kind",
    "library_label",
    "parse_library_doc",
    "parse_library_text",
    "workflow_template_issues",
]

LibraryKind = Literal["model_recipe", "prep_preset", "chart_template", "workflow_template"]
LibraryScope = Literal["project", "tenant"]

LIBRARY_DOC_MAX_BYTES: Final = 262_144
"""Largest library document (canonical JSON, and the ``text`` body that holds it)."""
MAX_ITEMS_PER_TENANT: Final = 500
MAX_REVISIONS_PER_ITEM: Final = 200
PLOT_SPEC_MAX_BYTES: Final = 65_536
"""Largest Vega-Lite spec of a chart template (the data stays in the source plot)."""

LIBRARY_ITEM_ID_RE: Final = r"^lib_[0-9a-z]{26}$"

LABEL_MODEL: Final = "mapi.library/model"
LABEL_PREP: Final = "mapi.library/prep"
LABEL_TEMPLATE: Final = "mapi.library/template"
"""Lineage labels a compiled spec or workflow carries: value ``lib_<id>-r<revision>``."""

_KIND_OF: Final[dict[str, LibraryKind]] = {
    "ModelRecipe": "model_recipe",
    "PrepPreset": "prep_preset",
    "ChartTemplate": "chart_template",
    "WorkflowTemplate": "workflow_template",
}


def library_label(item_id: str, revision: int | None = None) -> str:
    """The lineage label value of an item revision: ``lib_<id>-r<n>`` (``lib_<id>`` when the
    revision is not known). It fits :data:`~mapi_spec._types.LabelValue` (≤ 63 chars)."""
    return item_id if revision is None else f"{item_id}-r{revision}"


# ----------------------------------------------------------------------------- shared parts


class AboutLite(StrictModel):
    """A short About card for a library item (a subset of :class:`~mapi_spec.about.About`)."""

    title: str = Field(..., min_length=1, max_length=120)
    summary_plain: str = Field(..., min_length=1, max_length=300)
    use_when: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=5
    )
    avoid_when: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=5
    )


class LibraryMetadata(StrictModel):
    """Identity of a library document. ``name`` is unique per kind in its scope."""

    name: Annotated[Slug, Field(max_length=64)]
    title: str = Field(..., min_length=1, max_length=120)
    summary: str = Field("", max_length=300)
    tags: list[Annotated[Slug, Field(max_length=64)]] = Field(default_factory=list, max_length=8)
    about: AboutLite | None = None

    @field_validator("tags")
    @classmethod
    def _unique_tags(cls, v: list[str]) -> list[str]:
        if dups := unique(v):
            raise ValueError(f"duplicate tags: {dups}")
        return v


class LibraryRef(StrictModel):
    """A reference to another library item (a model recipe's prep preset or charts)."""

    item: str = Field(..., pattern=LIBRARY_ITEM_ID_RE)
    revision: Annotated[int, Field(ge=1)] | Literal["latest"] = "latest"


# ---------------------------------------------------------------------------- prep presets


class Slot(StrictModel):
    """A placeholder of a prep preset, filled when the preset is used.

    ``var`` takes one variable name, ``var_list`` one or more, ``dim`` one dim name. ``role``
    lets the app pre-fill the slot from the dataset's roles.
    """

    kind: Literal["var", "var_list", "dim"]
    label: str = Field(..., min_length=1, max_length=80)
    role: Role | None = None
    required: bool = True


class PrepPresetDoc(StrictModel):
    """Saved preprocessing steps with slots.

    A ``with`` value may be ``"$slot:<name>"`` (the whole value) or a list that holds such
    strings (each element is replaced; a ``var_list`` slot is spliced into the list).
    """

    api_version: ApiVersion = Field(..., alias="apiVersion")
    kind: Literal["PrepPreset"]
    metadata: LibraryMetadata
    slots: dict[Name, Slot] = Field(default_factory=dict, max_length=16)
    steps: list[StepCall] = Field(..., min_length=1, max_length=MAX_PIPELINE_STEPS)

    @model_validator(mode="after")
    def _unique_ids(self) -> PrepPresetDoc:
        if dups := unique(s.id for s in self.steps):
            raise ValueError(f"duplicate step ids: {dups}")
        return self


# --------------------------------------------------------------------------- model recipes

PriorDist = Literal["normal", "student_t", "laplace", "half_normal", "exponential", "lognormal"]
GlmFamily = Literal[
    "normal",
    "student_t",
    "poisson",
    "bernoulli",
    "negative_binomial",
    "zero_inflated_poisson",
    "zero_inflated_negative_binomial",
    "beta",
    "gamma",
    "ordinal",
]
"""The likelihood families of ``tabular.declarative_glm`` (the same ten as
``tabular.hierarchical_glm``)."""

TransformUse = Literal[
    "log1p", "standardize", "max_abs_scale", "mean_scale", "adstock", "saturation", "impute"
]
"""Built-in ``core.*`` steps a declarative term may apply to its variable."""

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class PriorSpec(StrictModel):
    """A prior from a closed set of distributions with numeric parameters (no expressions).

    ``mu`` is the location (the log-scale location for ``lognormal``; not used by
    ``half_normal`` and ``exponential``), ``sigma`` the scale, ``nu`` the degrees of freedom
    of ``student_t``.
    """

    dist: PriorDist = "normal"
    mu: FiniteFloat = 0.0
    sigma: float = Field(1.0, gt=0, le=1e6)
    nu: float | None = Field(None, gt=0, le=1000)


class TransformCall(StrictModel):
    """One built-in transform of a term's variable; ``with`` holds the step's other settings
    (``vars`` is set by the compiler)."""

    use: TransformUse
    with_: dict[str, JsonValue] = Field(default_factory=dict, alias="with")


class TermSpec(StrictModel):
    """One driver of a declarative model: its variable, transforms, prior and sign."""

    var: Name
    label: str = Field("", max_length=64)
    transform: list[TransformCall] = Field(default_factory=list, max_length=3)
    prior: PriorSpec
    sign: Literal["any", "positive", "negative"] = "any"
    varies_by: Name | None = None


class InteractionSpec(StrictModel):
    """A two-way interaction of two terms (their transformed variables are multiplied)."""

    a: Name
    b: Name
    prior: PriorSpec = PriorSpec(sigma=0.5)


class GroupSpec(StrictModel):
    """A group with its own intercept (partially pooled); ``sigma_prior`` is the prior of
    the spread of the group intercepts."""

    dim: Name
    sigma_prior: PriorSpec = PriorSpec(dist="half_normal")


class OutcomeSpec(StrictModel):
    """What the model explains: a variable and its likelihood family."""

    var: Name
    family: GlmFamily = "normal"
    n_categories: int | None = Field(None, ge=2, le=50)


class DeclarativeModel(StrictModel):
    """A GLM described with closed choices only (D3): families, priors, signs, groups."""

    outcome: OutcomeSpec
    intercept: PriorSpec = PriorSpec(sigma=2.0)
    terms: list[TermSpec] = Field(..., min_length=1, max_length=64)
    interactions: list[InteractionSpec] = Field(default_factory=list, max_length=16)
    groups: list[GroupSpec] = Field(default_factory=list, max_length=3)
    noise: PriorSpec = PriorSpec(dist="half_normal")


class ModelRecipeDoc(StrictModel):
    """A model recipe: a declarative model (``base: declarative``) or a preset over any
    catalog model (``base: <id>@<range>`` + ``settings`` + the ``exposed`` keys the user may
    change when using it)."""

    api_version: ApiVersion = Field(..., alias="apiVersion")
    kind: Literal["ModelRecipe"]
    metadata: LibraryMetadata
    base: Literal["declarative"] | UseRef = "declarative"
    declarative: DeclarativeModel | None = None
    settings: dict[str, JsonValue] = Field(default_factory=dict)
    exposed: list[Name] = Field(default_factory=list, max_length=12)
    prep: LibraryRef | None = None
    plots: list[UseRef] = Field(default_factory=list, max_length=16)
    charts: list[LibraryRef] = Field(default_factory=list, max_length=8)
    fit: FitConfig | None = None

    @property
    def is_declarative(self) -> bool:
        return self.base == "declarative"


# ------------------------------------------------------------------------- chart templates


class ChartTemplateDoc(StrictModel):
    """A Vega-Lite restyle of a platform plot (D5).

    ``plot`` is a Vega-Lite spec that reads only the named datasets of ``source_recipe``'s
    plot (``data: {name: ...}``); the data itself never leaves the stored plot. Optional
    ``bindings`` fill role/dim placeholders against the model's schema when applied.
    """

    api_version: ApiVersion = Field(..., alias="apiVersion")
    kind: Literal["ChartTemplate"]
    metadata: LibraryMetadata
    source_recipe: UseRef
    plot: dict[str, JsonValue]
    bindings: PlotBindings | None = None
    caption_plain: str = Field("", max_length=300)

    @field_validator("plot")
    @classmethod
    def _plot_size(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        size = len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode())
        if size > PLOT_SPEC_MAX_BYTES:
            raise ValueError(f"the Vega-Lite spec is {size} B; the limit is {PLOT_SPEC_MAX_BYTES}")
        return v


# ---------------------------------------------------------------------- workflow templates


class WorkflowTemplateDoc(StrictModel):
    """A team workflow template: a complete ``kind: Workflow`` document (validated with the
    merged node contracts when saved and again when used)."""

    api_version: ApiVersion = Field(..., alias="apiVersion")
    kind: Literal["WorkflowTemplate"]
    metadata: LibraryMetadata
    document: dict[str, JsonValue]


LibraryDoc = Annotated[
    ModelRecipeDoc | PrepPresetDoc | ChartTemplateDoc | WorkflowTemplateDoc,
    Discriminator("kind"),
]

_DOC_ADAPTER: TypeAdapter[LibraryDoc] = TypeAdapter(LibraryDoc)
_TEXT_LIMITS: Final = Limits(max_bytes=LIBRARY_DOC_MAX_BYTES)


def _invalid(issues: list[Issue], detail: str = "the library document is not valid") -> MapiError:
    return MapiError(
        detail,
        code=ErrorCode.LIBRARY_INVALID,
        pointer=issues[0].pointer if issues else "",
        issues=issues,
    )


def _json_size(obj: Any) -> int | None:
    try:
        return len(
            json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        )
    except (TypeError, ValueError):
        return None


def parse_library_doc(obj: Any) -> LibraryDoc:
    """Validate a parsed mapping as a library document.

    Raises :class:`~mapi_spec.errors.MapiError` ``LIBRARY_INVALID`` whose ``issues`` point
    into the document (``/declarative/terms/2/prior/sigma``).
    """
    if not isinstance(obj, Mapping):
        raise _invalid(
            [Issue(code=ErrorCode.LIBRARY_INVALID, message="A library document must be a mapping.")]
        )
    data = dict(obj)
    size = _json_size(data)
    if size is None:
        raise _invalid(
            [Issue(code=ErrorCode.LIBRARY_INVALID, message="The document is not plain JSON.")]
        )
    if size > LIBRARY_DOC_MAX_BYTES:
        raise _invalid(
            [
                Issue(
                    code=ErrorCode.LIBRARY_INVALID,
                    message=f"The document is {size} B. The limit is {LIBRARY_DOC_MAX_BYTES} B.",
                )
            ]
        )
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in _KIND_OF:
        raise _invalid(
            [
                Issue(
                    code=ErrorCode.LIBRARY_INVALID,
                    pointer="/kind",
                    message="kind must be one of " + ", ".join(_KIND_OF),
                )
            ]
        )
    try:
        return _DOC_ADAPTER.validate_python(data)
    except ValidationError as exc:
        issues = [
            i.model_copy(update={"code": ErrorCode.LIBRARY_INVALID})
            for i in validation_issues(exc, data)
        ]
        raise _invalid(issues) from None


def parse_library_text(text: str | bytes) -> LibraryDoc:
    """Parse a YAML/JSON text body with the budgeted loader, then :func:`parse_library_doc`.

    Parser errors keep their codes (``SPEC_PARSE_ERROR``, ``PAYLOAD_TOO_LARGE``).
    """
    return parse_library_doc(parse_text(text, _TEXT_LIMITS))


def library_kind(doc: LibraryDoc) -> LibraryKind:
    """The item kind that stores ``doc``."""
    return _KIND_OF[doc.kind]


def library_doc_hash(doc: LibraryDoc) -> str:
    """``sha256:<hex>`` of the canonical JSON of ``doc`` (defaults omitted)."""
    return spec_hash(doc)


def workflow_template_issues(
    doc: WorkflowTemplateDoc,
    *,
    nodes: Mapping[str, Any] | None = None,
    catalog: Any = None,
) -> list[Issue]:
    """Validate a team template's workflow document (issues point into the library
    document, under ``/document``). Pass the merged node contracts as ``nodes`` (built-ins
    win; see :func:`mapi_spec.library_workflows.node_contracts`)."""
    # imported here: mapi_spec.workflow pulls in the whole node catalog
    from mapi_spec.workflow import validate_workflow

    result = validate_workflow(doc.document, catalog=catalog, nodes=nodes)
    return [i.model_copy(update={"pointer": "/document" + i.pointer}) for i in result.issues]


# -------------------------------------------------------------------------------------- DTOs

_ITEM_ID = "lib_01jaf3q2x8m9v7k5h4g3f2e1d0"
_RECIPE_EXAMPLE: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "store-sales", "title": "Store sales with promo lift"},
    "base": "declarative",
    "declarative": {
        "outcome": {"var": "sales", "family": "negative_binomial"},
        "terms": [
            {
                "var": "price",
                "transform": [{"use": "log1p"}],
                "prior": {"sigma": 0.5},
                "sign": "negative",
            },
            {"var": "promo", "prior": {"sigma": 1.0}, "sign": "positive", "varies_by": "store"},
        ],
        "groups": [{"dim": "store"}],
    },
    "plots": ["core.ppc"],
}


class LibraryItem(ResponseModel):
    """A library item (its documents are in :class:`LibraryRevision`)."""

    id: str = Field(..., pattern=LIBRARY_ITEM_ID_RE)
    kind: LibraryKind
    scope: LibraryScope
    project_id: str | None = None
    name: str = Field(..., max_length=64)
    title: str = Field(..., max_length=120)
    summary: str = Field("", max_length=300)
    tags: list[str] = Field(default_factory=list, max_length=8)
    latest_revision: int = Field(..., ge=1)
    archived: bool = False
    created_by: str
    created_at: datetime
    updated_at: datetime
    etag: str = Field(..., description='The latest revision as an ETag: "r<latest_revision>"')

    model_config = dto_examples(
        {
            "id": _ITEM_ID,
            "kind": "model_recipe",
            "scope": "project",
            "project_id": "growth",
            "name": "store-sales",
            "title": "Store sales with promo lift",
            "summary": "",
            "tags": ["retail"],
            "latest_revision": 3,
            "archived": False,
            "created_by": "usr_analyst",
            "created_at": EXAMPLE_TS,
            "updated_at": EXAMPLE_TS,
            "etag": '"r3"',
        }
    )


class LibraryRevision(ResponseModel):
    """One immutable revision of a library item: the document, whether it compiled against
    the catalog of the time, and the compiled output."""

    item_id: str = Field(..., pattern=LIBRARY_ITEM_ID_RE)
    revision: int = Field(..., ge=1)
    document: dict[str, JsonValue]
    doc_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    valid: bool
    issues: list[Issue] = Field(default_factory=list)
    compiled: dict[str, JsonValue] | None = None
    catalog_digest: str | None = Field(None, pattern=r"^sha256:[0-9a-f]{64}$")
    created_by: str
    created_at: datetime
    message: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {
            "item_id": _ITEM_ID,
            "revision": 1,
            "document": _RECIPE_EXAMPLE,
            "doc_hash": "sha256:" + "0" * 64,
            "valid": True,
            "issues": [],
            "compiled": None,
            "catalog_digest": "sha256:" + "1" * 64,
            "created_by": "usr_analyst",
            "created_at": EXAMPLE_TS,
            "message": "first version",
        }
    )


class _DocumentOrText(RequestModel):
    document: dict[str, JsonValue] | None = None
    text: str | None = Field(
        None,
        max_length=LIBRARY_DOC_MAX_BYTES,
        description="The document as YAML or JSON text (instead of document)",
    )

    @model_validator(mode="after")
    def _one(self) -> _DocumentOrText:
        if (self.document is None) == (self.text is None):
            raise ValueError("give exactly one of document or text")
        return self


class LibraryCreate(_DocumentOrText):
    """Create a library item (revision 1)."""

    scope: LibraryScope = "project"
    message: str | None = Field(None, max_length=500)

    model_config = dto_examples({"scope": "project", "document": _RECIPE_EXAMPLE})


class LibraryRevisionCreate(_DocumentOrText):
    """Add a revision to a library item (``If-Match: "r<latest_revision>"``)."""

    message: str | None = Field(None, max_length=500)

    model_config = dto_examples({"document": _RECIPE_EXAMPLE, "message": "tighter price prior"})


class LibraryValidateRequest(_DocumentOrText):
    """Check a document; with ``dataset``, also its slots and variables against the data."""

    dataset: DatasetRef | None = None

    model_config = dto_examples(
        {"document": _RECIPE_EXAMPLE, "dataset": "dataset://growth/store_sales@latest"}
    )


class LibraryValidation(ResponseModel):
    """The result of a check: issues point into the document."""

    valid: bool
    issues: list[Issue] = Field(default_factory=list)
    compiled: dict[str, JsonValue] | None = None

    model_config = dto_examples(
        {
            "valid": False,
            "issues": [
                {
                    "code": "LIBRARY_INVALID",
                    "severity": "error",
                    "pointer": "/declarative/terms/1/varies_by",
                    "message": "varies_by 'region' is not one of the groups",
                }
            ],
            "compiled": None,
        }
    )


class LibraryUseRequest(RequestModel):
    """Use an item: the values of its slots and exposed settings, the dataset to check
    against, and for chart templates the model version to apply to."""

    revision: int | None = Field(None, ge=1)
    dataset: DatasetRef | None = None
    slots: dict[Name, JsonValue] = Field(default_factory=dict, max_length=16)
    exposed: dict[Name, JsonValue] = Field(default_factory=dict, max_length=12)
    model_id: Id | None = None
    model_version: int | None = Field(None, ge=1)

    model_config = dto_examples(
        {"dataset": "dataset://growth/store_sales@latest", "slots": {"media": ["tv", "search"]}}
    )


class LibraryUseResult(ResponseModel):
    """What an item compiles to: a spec fragment (model recipes), prep steps (prep presets),
    an applied chart (chart templates) or a workflow document (workflow templates)."""

    kind: LibraryKind
    spec_fragment: dict[str, JsonValue] | None = None
    pipeline: list[dict[str, JsonValue]] | None = None
    plot: PlotSpec | None = None
    workflow_document: dict[str, JsonValue] | None = None
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)

    model_config = dto_examples(
        {
            "kind": "prep_preset",
            "pipeline": [
                {
                    "id": "p_scale",
                    "use": "core.max_abs_scale@^1",
                    "with": {"vars": ["tv", "search"], "over": ["time"]},
                },
            ],
            "labels": {"mapi.library/prep": f"{_ITEM_ID}-r2"},
            "issues": [],
        }
    )


class ChartPreviewRequest(RequestModel):
    """Preview a chart template document on its source recipe's catalog example."""

    document: dict[str, JsonValue]

    model_config = dto_examples(
        {
            "document": {
                "apiVersion": "mapi/v1",
                "kind": "ChartTemplate",
                "metadata": {"name": "fit-dots", "title": "Fit as dots"},
                "source_recipe": "core.fit_vs_actual@^1",
                "plot": {
                    "data": {"name": "points"},
                    "mark": "point",
                    "encoding": {
                        "x": {"field": "observed", "type": "quantitative"},
                        "y": {"field": "point", "type": "quantitative"},
                    },
                },
            }
        }
    )


class ChartPreview(ResponseModel):
    """A previewed chart (``None`` when it was refused) and what the checks found."""

    plot: PlotSpec | None = None
    issues: list[Issue] = Field(default_factory=list)

    model_config = dto_examples({"plot": None, "issues": []})
