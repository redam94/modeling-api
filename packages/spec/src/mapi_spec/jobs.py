"""Run job payloads: what ``RunSpec.spec`` holds for each run kind, plus the input and
output object names shared by the control plane (which writes RunSpecs and reads outputs)
and the runner (which reads RunSpecs and writes outputs).

========== ============================================ ======================================
kind       ``RunSpec.spec``                             inputs (``RunSpec.inputs`` names)
========== ============================================ ======================================
ingest     :class:`IngestJob` (a bare ``IngestMapping`` ``upload`` (the raw file; ``dataset``
           is also accepted)                            is accepted as a fallback)
describe   :class:`DescribeJob`                         none
validate   a ``ModelSpec`` document                     ``dataset``, optional ``schema``,
                                                        ``side.<name>``
fit        a ``ModelSpec`` document                     ``dataset``, optional ``schema``,
                                                        ``side.<name>``
score      a ``ScoreRequest`` document, or              ``bundle``, optional
           :class:`ScoreJob`                            ``bundle.manifest``, ``dataset`` (the
                                                        request's ``data``), ``scenario``,
                                                        ``side.<name>``
evaluate   :class:`EvaluateJob`                         ``bundle``, ``dataset``, ...
derive     :class:`DeriveJob`                           ``bundle``, ``dataset``, ``side.<name>``
render     :class:`RenderJob`                           ``plots`` (a ``plots.json``)
translate_ :class:`~mapi_spec.calibration.Prior-         ``dataset``, optional ``schema``,
priors     TranslationJob` (M3)                         ``side.<name>``; output ``priors.json``
========== ============================================ ======================================

Datasets are single-object ``*.zarr.zip`` files. A dataset's raw schema comes from the
``schema`` input, else the zarr root attribute ``mapi_schema`` (written by ingest), else it is
inferred. ``bundle`` is the ``bundle.zarr.zip`` of a fit and ``bundle.manifest`` the SDK
bundle manifest that the fit uploaded as ``bundle.manifest.json``.

This module is pure Pydantic (no numpy/xarray): the control plane imports it.
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mapi_spec._types import StrictModel
from mapi_spec.api import IngestMapping
from mapi_spec.calibration import PRIOR_TRANSLATION_OUTPUT, PriorTranslationJob
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.ingest import MffOptions
from mapi_spec.loader import parse_document
from mapi_spec.run import INPUT_BUNDLE, INPUT_DATASET
from mapi_spec.spec import ModelSpec, ScoreRequest

__all__ = [
    "BUNDLE_MANIFEST_OUTPUT",
    "DATASET_OUTPUTS",
    "DATASET_SCHEMA_ATTR",
    "DESCRIBE_OUTPUT",
    "FIT_BUNDLE",
    "FIT_SIDECARS",
    "INGEST_DATA",
    "INGEST_OPTIONAL",
    "INGEST_SCHEMA",
    "INPUT_BUNDLE",
    "INPUT_BUNDLE_MANIFEST",
    "INPUT_DATASET",
    "INPUT_PLOTS",
    "INPUT_SCENARIO",
    "INPUT_SCHEMA",
    "INPUT_UPLOAD",
    "MAX_PLOT_DATA_INPUTS",
    "PLOT_DATA_INPUT_PREFIX",
    "PRIOR_TRANSLATION_OUTPUT",
    "RENDER_OUTPUT_PREFIX",
    "SCORE_INPUTS",
    "SCORE_OUTPUT",
    "SIDE_TABLE_PREFIX",
    "DeriveJob",
    "DescribeJob",
    "EvaluateJob",
    "IngestJob",
    "IngestSource",
    "Job",
    "MetricsSidecar",
    "MffOptions",
    "PriorTranslationJob",
    "RenderJob",
    "ScoreJob",
    "parse_job",
    "parse_model_job_params",
    "plot_data_input",
    "render_output",
    "side_table_input",
]

# ------------------------------------------------------------------------------- input names
INPUT_UPLOAD: Final = "upload"
INPUT_SCHEMA: Final = "schema"
INPUT_SCENARIO: Final = "scenario"
INPUT_BUNDLE_MANIFEST: Final = "bundle.manifest"
INPUT_PLOTS: Final = "plots"
SIDE_TABLE_PREFIX: Final = "side."
PLOT_DATA_INPUT_PREFIX: Final = "plotdata."
"""``render`` runs: a ``data_ref`` dataset whose ``path`` is ``plotdata.<n>`` is read from the
run input of the same name (the control plane pins the bundle object it points at)."""
MAX_PLOT_DATA_INPUTS: Final = 64
RENDER_OUTPUT_PREFIX: Final = "renders/"
"""``render`` runs write ``renders/<plot id>.<format>``."""

SCORE_INPUTS: Final = {"data": INPUT_DATASET, "scenario": INPUT_SCENARIO}
"""``ScoreRequest`` dataset fields → the input name the runner reads them from."""

_SIDE_NAME = re.compile(r"^[a-z][a-z0-9_\-]{0,57}$")


def plot_data_input(index: int) -> str:
    """The run input (and ``data_ref`` path) of a render run's ``index``-th external dataset."""
    if not 0 <= index < MAX_PLOT_DATA_INPUTS:
        raise ValueError(f"a render run takes at most {MAX_PLOT_DATA_INPUTS} data inputs")
    return f"{PLOT_DATA_INPUT_PREFIX}{index}"


def render_output(plot_id: str, fmt: str) -> str:
    """``renders/<plot id>.<format>``: the object a render run writes for one plot."""
    return f"{RENDER_OUTPUT_PREFIX}{plot_id}.{fmt}"


def side_table_input(name: str) -> str:
    """The input name of side table ``name`` (``side.<name>``)."""
    if not _SIDE_NAME.match(name):
        raise MapiError(
            f"side table name {name!r} cannot be used as an input name",
            code=ErrorCode.SCHEMA_INVALID,
        )
    return SIDE_TABLE_PREFIX + name


DATASET_SCHEMA_ATTR: Final = "mapi_schema"
"""Root attribute of a ``*.zarr.zip`` dataset holding its raw ``DataSchema`` as JSON. Ingest
writes it; uploads of self-describing datasets may carry it (dim policies, roles, units) —
otherwise ingest infers a schema (every dim ``fixed``, every variable a ``covariate``)."""

# ------------------------------------------------------------------------------ output names
INGEST_DATA: Final = "data.zarr.zip"
INGEST_SCHEMA: Final = "schema.json"
INGEST_OPTIONAL: Final = ("summary.json", "profile.json", "preview.json")
DATASET_OUTPUTS: Final = (INGEST_DATA, INGEST_SCHEMA, *INGEST_OPTIONAL)
"""What an ingest run uploads: the data, its ``DataSchema``, its ``DataSummary`` (used for
CoordPlan checks of score payloads), the profile and the UI preview."""

FIT_BUNDLE: Final = "bundle.zarr.zip"
FIT_SIDECARS: Final = ("diagnostics.json", "metrics.json", "plots.json", "coords.json")
"""JSON sidecars of a fit: ``list[Diagnostic]``, :class:`MetricsSidecar`, ``list[PlotSpec]``
and the fit coordinates ``{stage: {dim: CoordSummary}}`` (``ScoreMetadata.fit_coords``)."""
BUNDLE_MANIFEST_OUTPUT: Final = "bundle.manifest.json"
"""The SDK bundle manifest (content hashes, lineage). ``manifest.json`` is reserved for the
runner's :class:`~mapi_spec.run.OutputManifest`."""

DESCRIBE_OUTPUT: Final = "catalog.json"
SCORE_OUTPUT: Final = "score.zarr.zip"


# ------------------------------------------------------------------------------------- jobs


class IngestSource(StrictModel):
    """What the uploader declared about the raw file (informational; never trusted)."""

    filename: str = Field(..., min_length=1, max_length=255)
    content_type: str | None = Field(None, max_length=255)
    size_bytes: int | None = Field(None, ge=0)


class IngestJob(StrictModel):
    """An ingest run: an explicit column mapping, or the MFF preset.

    From M3 the mapping itself may name a preset (``mapping.preset``: ``mff``,
    ``meridian`` or ``bayesinsight``, with its options in the mapping); the top-level
    ``preset``/``mff`` pair stays for v0.2 callers.
    """

    mapping: IngestMapping | None = None
    preset: Literal["mff"] | None = None
    mff: MffOptions = Field(default_factory=MffOptions)
    preview_rows: int = Field(50, ge=1, le=1000)
    max_rows: int = Field(50_000_000, ge=1)
    dataset: str | None = Field(
        None, max_length=512, description="dataset://project/name@vN being created (lineage)"
    )
    source: IngestSource | None = None

    @model_validator(mode="after")
    def _one_of(self) -> IngestJob:
        if (self.mapping is None) == (self.preset is None):
            raise ValueError("give exactly one of 'mapping' or 'preset'")
        return self


class DescribeJob(StrictModel):
    """A describe run exports the image's catalog (``catalog.json``)."""

    image_digest: str | None = Field(
        None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="The image digest the catalog is cached under by the control plane",
    )


class ScoreJob(StrictModel):
    """A score run: the request plus runner-side options."""

    request: ScoreRequest
    plots: list[str] | None = Field(
        None, description="Plot recipes to render from the result (default: by mode)"
    )
    max_draws: int | None = Field(None, ge=0)


class EvaluateJob(StrictModel):
    """Evaluate a bundle on ``dataset`` (default request: the fit-time evaluation request;
    default data: the dataset version the model was fitted on)."""

    request: ScoreRequest | None = None
    metrics: list[str] | None = Field(None, max_length=64)


class DeriveJob(StrictModel):
    """Compute declared derived quantities of a bundle over the ``dataset`` input."""

    names: list[str] = Field(..., min_length=1, max_length=32)
    draws: int | None = Field(None, ge=1)
    data: str | None = Field(
        None,
        max_length=512,
        description="dataset://… to derive over (default: the model's fit dataset)",
    )


class RenderJob(StrictModel):
    """Render plots from a ``plots.json`` to static images (``renders/<plot id>.<format>``).

    ``pdf`` (M3) and an explicit ``width``/``height`` in CSS pixels (before ``scale``) are
    for reports; Vega-Lite renders with vl-convert, Plotly with kaleido, in the sandbox.
    """

    plots: list[str] | None = Field(None, description="Plot ids (default: all)")
    format: Literal["png", "svg", "pdf"] = "png"
    scale: float = Field(2.0, gt=0, le=8)
    width: int | None = Field(None, ge=16, le=4096)
    height: int | None = Field(None, ge=16, le=4096)


Job = (
    IngestJob
    | DescribeJob
    | ModelSpec
    | ScoreJob
    | EvaluateJob
    | DeriveJob
    | RenderJob
    | PriorTranslationJob
)


class MetricsSidecar(BaseModel):
    """``metrics.json`` as the control plane reads it: holdout metric values (``None`` when a
    metric could not be computed) and why metrics were skipped. Unknown fields are ignored so
    the SDK's report can grow."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    values: dict[str, float | None] = Field(default_factory=dict, max_length=256)
    skipped: dict[str, str] = Field(default_factory=dict, max_length=256)
    n_holdout: int = Field(0, ge=0)

    def finite(self) -> dict[str, float]:
        """The metrics that have a finite value."""
        return {
            k: v
            for k, v in self.values.items()
            if v is not None and v == v and v not in (float("inf"), float("-inf"))
        }


def _invalid(kind: RunKind, exc: Exception, pointer: str = "/spec") -> MapiError:
    return MapiError(
        f"the {kind.value} run's spec is invalid",
        code=ErrorCode.SCHEMA_INVALID,
        pointer=pointer,
        untrusted_text=str(exc)[:8000],
    )


_MODEL_JOBS: dict[RunKind, type[EvaluateJob | DeriveJob | RenderJob]] = {
    RunKind.EVALUATE: EvaluateJob,
    RunKind.DERIVE: DeriveJob,
    RunKind.RENDER: RenderJob,
}


def parse_model_job_params(
    kind: RunKind, params: dict[str, Any]
) -> EvaluateJob | DeriveJob | RenderJob:
    """Validate the ``params`` of an ``evaluate``/``derive``/``render`` run request."""
    model = _MODEL_JOBS.get(kind)
    if model is None:
        raise MapiError(
            f"{kind.value} runs take no params", code=ErrorCode.SCHEMA_INVALID, pointer="/params"
        )
    try:
        return model.model_validate(params)
    except (ValidationError, ValueError) as exc:
        raise _invalid(kind, exc, "/params") from None


def parse_job(kind: RunKind, spec: dict[str, Any]) -> Job:
    """Parse ``RunSpec.spec`` for ``kind`` (raises ``MapiError(SCHEMA_INVALID)``)."""
    try:
        if kind is RunKind.INGEST:
            if "format" in spec:
                return IngestJob(mapping=IngestMapping.model_validate(spec))
            return IngestJob.model_validate(spec)
        if kind is RunKind.DESCRIBE:
            return DescribeJob.model_validate(spec)
        if kind in (RunKind.FIT, RunKind.VALIDATE):
            doc = parse_document(dict(spec))
            if not isinstance(doc, ModelSpec):
                raise ValueError("expected a ModelSpec document")
            return doc
        if kind is RunKind.SCORE:
            if spec.get("kind") == "ScoreRequest":
                return ScoreJob(request=ScoreRequest.model_validate(spec))
            return ScoreJob.model_validate(spec)
        if kind is RunKind.EVALUATE:
            if spec.get("kind") == "ScoreRequest":
                return EvaluateJob(request=ScoreRequest.model_validate(spec))
            return EvaluateJob.model_validate(spec)
        if kind is RunKind.DERIVE:
            return DeriveJob.model_validate(spec)
        if kind is RunKind.RENDER:
            return RenderJob.model_validate(spec)
        if kind is RunKind.TRANSLATE_PRIORS:
            job = PriorTranslationJob.model_validate(spec)
            doc = parse_document(dict(job.spec))
            if not isinstance(doc, ModelSpec):
                raise ValueError("expected a ModelSpec document")
            return job
    except (ValidationError, ValueError) as exc:
        raise _invalid(kind, exc) from None
    except MapiError:
        raise
    raise NotImplementedError(
        f"the runner does not execute {kind.value!r} runs yet (optimize is M4)"
    )
