"""Sample projects: a real small fit per tenant, one click away (v0.18 D20).

A sample is **not** a precomputed bundle: model versions only come from finalized, signed,
sandboxed runs. Installing one takes our synthetic table down the same Guided path a user's
upload takes (``build_draft`` → ingest → spec → fit), with a short fit profile, in a project
of its own (``sample``; ``sample-2`` … ``sample-9`` when the id is taken).

* :data:`SAMPLES` holds the definitions (:class:`SampleDef`); today one, ``geo_mmm``.
* :func:`sample_draft` builds the draft and the spec to store (pure; the server and the
  tests call the same function).
* :class:`SampleInfo` is what ``GET /v1/tenants/{t}/samples`` lists and what the
  ``:install`` / ``:retry`` / ``:retire`` actions return.
* :data:`SAMPLE_TRANSITIONS` is the life cycle the sample loop follows
  (``provisioning → ingesting → fitting → ready``; ``failed`` and ``retired`` from any
  working state).

The table itself (``data_file``) ships with the server (``mapi_server/samples/data``);
``data_sha256`` pins its bytes. ``library_seed`` holds the three library documents (a model
recipe, a prep preset and a chart template) the server adds to the sample project when the
sample is ready, so "Yours" in the library is never empty. They are plain JSON here
(``mapi_spec.library`` parses them), so this module stays free of the library compilers.

Everything here is plain pydantic: no I/O, no numpy.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final, Literal, get_args

from pydantic import Field, JsonValue, field_validator

from mapi_spec._dto import Id, ResponseModel, dto_examples
from mapi_spec._types import LabelKey, LabelValue, Slug, StrictModel
from mapi_spec.errors import Problem
from mapi_spec.guided import (
    GUIDED_TEMPLATES,
    ColumnProfile,
    GuidedAnswers,
    GuidedDraft,
    build_draft,
)
from mapi_spec.spec import API_VERSION, ModelSpec

__all__ = [
    "ACTIVE_SAMPLE_STATES",
    "GEO_MMM_SAMPLE",
    "SAMPLES",
    "SAMPLE_LABEL",
    "SAMPLE_PROJECT_ID",
    "SAMPLE_STATES",
    "SAMPLE_TRANSITIONS",
    "SampleDef",
    "SampleInfo",
    "SampleState",
    "can_transition",
    "sample_draft",
    "sample_project_ids",
]

SampleState = Literal["provisioning", "ingesting", "fitting", "ready", "failed", "retired"]
"""``provisioning`` (project, grant, dataset upload) → ``ingesting`` → ``fitting`` →
``ready``; ``failed`` can be retried; ``retired`` is final (hidden, nothing deleted)."""

SAMPLE_STATES: Final[tuple[SampleState, ...]] = get_args(SampleState)

ACTIVE_SAMPLE_STATES: Final[frozenset[SampleState]] = frozenset({"ingesting", "fitting"})
"""States the sample loop polls (the table has a partial index on them)."""

SAMPLE_TRANSITIONS: Final[Mapping[SampleState, frozenset[SampleState]]] = MappingProxyType(
    {
        "provisioning": frozenset({"ingesting", "failed", "retired"}),
        "ingesting": frozenset({"fitting", "failed", "retired"}),
        "fitting": frozenset({"ready", "failed", "retired"}),
        "ready": frozenset({"retired"}),
        "failed": frozenset({"provisioning", "retired"}),
        "retired": frozenset(),
    }
)
"""Allowed state changes. ``failed → provisioning`` is a retry; retiring is final."""

SAMPLE_PROJECT_ID: Final = "sample"
SAMPLE_LABEL: Final = "mapi.sample"
"""Spec and run label naming the sample a spec or run belongs to."""

_MAX_PROJECT_SUFFIX: Final = 9


def can_transition(old: SampleState, new: SampleState) -> bool:
    """True when the sample loop may move a sample from ``old`` to ``new``."""
    return new in SAMPLE_TRANSITIONS[old]


def sample_project_ids() -> tuple[str, ...]:
    """Project ids to try, in order, for a new sample project: ``sample``, then
    ``sample-2`` … ``sample-9`` when a project already has the id."""
    extra = (f"{SAMPLE_PROJECT_ID}-{n}" for n in range(2, _MAX_PROJECT_SUFFIX + 1))
    return (SAMPLE_PROJECT_ID, *extra)


class SampleDef(StrictModel):
    """One sample project: its card, its data, the Guided answers and the fit profile."""

    id: Slug
    title: str = Field(..., min_length=1, max_length=80)
    summary: str = Field(..., min_length=1, max_length=300)
    minutes: float = Field(..., gt=0, le=60, description="Time to ready on a laptop CPU")
    template_id: Slug = Field(..., description="The Guided template the sample uses")
    project_name: str = Field(..., min_length=1, max_length=120)
    dataset: Slug = Field(..., description="Name of the sample's dataset (and spec)")
    data_file: str = Field(
        ...,
        pattern=r"^[a-z0-9_]+\.csv$",
        description="File name under mapi_server/samples/data",
    )
    data_sha256: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    answers: GuidedAnswers
    fit: dict[str, JsonValue] = Field(
        ..., description="Merged over the draft's fit block (a short, converging profile)"
    )
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=8)
    library_seed: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=8)

    @field_validator("template_id")
    @classmethod
    def _known_template(cls, v: str) -> str:
        if v not in GUIDED_TEMPLATES:
            raise ValueError(f"no guided template {v!r}")
        return v


class SampleInfo(ResponseModel):
    """A sample project as one tenant member sees it."""

    id: Slug
    title: str
    summary: str
    minutes: float
    state: SampleState | None = Field(None, description="None: not installed in the tenant")
    project_id: Id | None = None
    spec_id: Id | None = None
    run_id: Id | None = None
    model_id: Id | None = None
    model_version: int | None = Field(None, ge=1)
    progress: float | None = Field(None, ge=0.0, le=1.0)
    problem: Problem | None = Field(None, description="Why the sample failed (plain words)")
    attempts: int = Field(0, ge=0)
    can_install: bool = Field(False, description="Install, or join the installed sample")
    can_retry: bool = False
    can_retire: bool = False
    hidden: bool = Field(False, description="The caller hid the sample from the hub")

    model_config = dto_examples(
        {
            "id": "geo_mmm",
            "title": "Marketing mix in four regions",
            "summary": "Two years of weekly revenue and media spend.",
            "minutes": 1.0,
            "can_install": True,
        },
        {
            "id": "geo_mmm",
            "title": "Marketing mix in four regions",
            "summary": "Two years of weekly revenue and media spend.",
            "minutes": 1.0,
            "state": "fitting",
            "project_id": "sample",
            "spec_id": "spec_01jaf3q2x8m9v7k5h4g3f2e1d0",
            "run_id": "run_01jaf3q2x8m9v7k5h4g3f2e1d0",
            "progress": 0.6,
            "attempts": 1,
        },
        {
            "id": "geo_mmm",
            "title": "Marketing mix in four regions",
            "summary": "Two years of weekly revenue and media spend.",
            "minutes": 1.0,
            "state": "ready",
            "project_id": "sample",
            "spec_id": "spec_01jaf3q2x8m9v7k5h4g3f2e1d0",
            "run_id": "run_01jaf3q2x8m9v7k5h4g3f2e1d0",
            "model_id": "mdl_01jaf3q2x8m9v7k5h4g3f2e1d0",
            "model_version": 1,
            "progress": 1.0,
            "attempts": 1,
            "can_retire": True,
        },
    )


def sample_draft(sample: SampleDef, *, project: str) -> tuple[GuidedDraft, dict[str, Any]]:
    """The Guided draft of ``sample`` in ``project`` and the spec document to store: the
    draft's spec with ``sample.fit`` merged over its fit block and ``sample.labels`` added to
    its labels. Raises ``GUIDED_ANSWERS_INVALID`` like :func:`build_draft`."""
    draft = build_draft(GUIDED_TEMPLATES[sample.template_id], sample.answers, project=project)
    doc: dict[str, Any] = ModelSpec.model_validate(draft.spec).model_dump(
        mode="json", by_alias=True, exclude_defaults=True
    )
    doc["model"]["fit"] = {**doc["model"].get("fit", {}), **sample.fit}
    meta = doc.setdefault("metadata", {})
    meta["labels"] = {**meta.get("labels", {}), **sample.labels}
    spec = ModelSpec.model_validate(doc)
    return draft, spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)


# ======================================================================== geo_mmm


def _col(name: str, dtype: Any, **kw: Any) -> ColumnProfile:
    return ColumnProfile(name=name, dtype=dtype, n_rows=416, **kw)


_GEO_COLUMNS: Final = [
    _col("week", "date", n_distinct=104, min_date="2024-01-07", max_date="2025-12-28",
         detected_freq="W-SUN"),
    _col("region", "string", n_distinct=4),
    _col("Revenue (GBP)", "number", n_distinct=416, min=94710.63, max=296624.86),
    _col("tv_spend", "number", n_distinct=416, min=1777.44, max=45428.37),
    _col("search_spend", "number", n_distinct=416, min=2506.45, max=16455.96),
    _col("social_spend", "number", n_distinct=416, min=841.82, max=12472.22),
    _col("avg_price", "number", n_distinct=78, min=9.51, max=10.51),
]  # fmt: skip
"""The profile of the sample table (as the ingest profiles it; samples left out)."""

_SEED_META: Final[dict[str, JsonValue]] = {"tags": ["sample"]}

_SEED_MODEL_RECIPE: Final[dict[str, JsonValue]] = {
    "apiVersion": API_VERSION,
    "kind": "ModelRecipe",
    "metadata": {
        "name": "revenue-and-price",
        "title": "Revenue and price, region by region",
        "summary": "A simple model you can read: how revenue moves with price in each region.",
        **_SEED_META,
    },
    "base": "declarative",
    "declarative": {
        "outcome": {"var": "revenue_gbp", "family": "normal"},
        "terms": [
            {
                "var": "avg_price",
                "label": "Average price",
                "transform": [{"use": "standardize"}],
                "prior": {"dist": "normal", "sigma": 0.5},
                "sign": "negative",
                "varies_by": "region",
            }
        ],
        "groups": [{"dim": "region"}],
    },
}
"""A declarative model over the sample's dataset: revenue falls when price goes up."""

_SEED_PREP_PRESET: Final[dict[str, JsonValue]] = {
    "apiVersion": API_VERSION,
    "kind": "PrepPreset",
    "metadata": {
        "name": "weekly-totals-log",
        "title": "Weekly totals, then log",
        "summary": "Add up each week, then take log(1 + x) of the columns you pick.",
        **_SEED_META,
    },
    "slots": {
        "time": {"kind": "dim", "label": "Date or week"},
        "skewed": {"kind": "var_list", "label": "Columns to log"},
    },
    "steps": [
        {
            "id": "weekly",
            "use": "core.resample@^1",
            "with": {"dim": "$slot:time", "freq": "W-SUN", "how": "sum",
                     "full_periods_only": True},
        },
        {"id": "log", "use": "core.log1p@^1", "with": {"vars": ["$slot:skewed"]}},
    ],
}  # fmt: skip
"""A prep preset with a dim slot and a column-list slot."""

_SEED_CHART_TEMPLATE: Final[dict[str, JsonValue]] = {
    "apiVersion": API_VERSION,
    "kind": "ChartTemplate",
    "metadata": {
        "name": "fit-with-band",
        "title": "Fit with a 90% band",
        "summary": "The model's line, its 90% band and the actual values, one panel per group.",
        **_SEED_META,
    },
    "source_recipe": "core.ppc_fan@^1",
    "plot": {
        "data": {"name": "fan"},
        "facet": {"field": "group", "type": "nominal", "title": None},
        "columns": 2,
        "spec": {
            "width": 280,
            "height": 160,
            "layer": [
                {
                    "mark": {"type": "area", "opacity": 0.25},
                    "encoding": {
                        "x": {"field": "x", "type": "temporal", "title": None},
                        "y": {"field": "lo_90", "type": "quantitative", "title": None},
                        "y2": {"field": "hi_90"},
                    },
                },
                {
                    "mark": {"type": "line"},
                    "encoding": {
                        "x": {"field": "x", "type": "temporal"},
                        "y": {"field": "point", "type": "quantitative"},
                    },
                },
                {
                    "mark": {"type": "point", "size": 12, "filled": True},
                    "encoding": {
                        "x": {"field": "x", "type": "temporal"},
                        "y": {"field": "observed", "type": "quantitative"},
                    },
                },
            ],
        },
    },
    "caption_plain": "The band holds 90% of the model's predictions. Dots are the actual values.",
}
"""A chart template restyling ``core.ppc_fan`` (the sample's fit plots include it)."""

GEO_MMM_SAMPLE: Final = SampleDef(
    id="geo_mmm",
    title="Marketing mix in four regions",
    summary="Two years of weekly revenue with TV, search and social spend in four UK regions. "
    "Fit a real model in about a minute, then move budget between channels and see what "
    "changes.",
    minutes=1.0,
    template_id="geo_mmm",
    project_name="Sample: marketing mix",
    dataset="sample-geo-mmm",
    data_file="geo_mmm.csv",
    data_sha256="sha256:8413b6c0623090ffd791adc4ccc9b4d6b0bc8802bac1e3ce18e7988e65c07b85",
    answers=GuidedAnswers(
        template_id="geo_mmm",
        name="sample-geo-mmm",
        dataset="sample-geo-mmm",
        answers={
            "kpi": "Revenue (GBP)",
            "date": "week",
            "geo": "region",
            "spend": ["search_spend", "social_spend", "tv_spend"],
            "controls": ["avg_price"],
            "frequency": "weekly",
            "holdout_periods": 8,
        },
        columns=_GEO_COLUMNS,
    ),
    # v0.18 WP0 (g): 4 chains × 300 draws after 300 tuning steps, fitted once on the train
    # window; ~30 s warm / ~55 s cold on a laptop, R-hat ≤ 1.03, no scorecard fail.
    fit={"mode": "train_only", "engine": "nutpie", "tune": 300, "draws": 300, "chains": 4},
    labels={SAMPLE_LABEL: "geo_mmm"},
    library_seed=[_SEED_MODEL_RECIPE, _SEED_PREP_PRESET, _SEED_CHART_TEMPLATE],
)
"""The geo marketing-mix sample: ``scripts/make_demo_grid.py --write-csv`` makes its table
(seed 20261021; 104 weeks × 4 regions; a known truth) and its playground grid."""

SAMPLES: Final[Mapping[str, SampleDef]] = MappingProxyType({GEO_MMM_SAMPLE.id: GEO_MMM_SAMPLE})
"""Every sample project, by id."""
