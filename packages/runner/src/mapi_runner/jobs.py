"""What ``RunSpec.spec`` holds for each run kind, and the input/output naming conventions.

This module is pure Pydantic on top of :mod:`mapi_spec` (no numpy/xarray), so the control
plane could import the same shapes; it documents the contract between whoever writes a
:class:`~mapi_spec.run.RunSpec` (the dispatcher) and the runner.

========== ========================================== =========================================
kind       ``RunSpec.spec``                           inputs (``RunSpec.inputs`` names)
========== ========================================== =========================================
ingest     :class:`IngestJob` (or a bare              ``upload`` (the raw file; ``dataset`` is
           ``IngestMapping``)                         accepted as a fallback)
describe   ``{}``                                     none
validate   a ``ModelSpec`` document                   ``dataset``, optional ``schema``,
                                                      ``side.<name>``
fit        a ``ModelSpec`` document                   ``dataset``, optional ``schema``,
                                                      ``side.<name>``
score      a ``ScoreRequest`` document, or            ``bundle``, optional ``bundle.manifest``,
           :class:`ScoreJob`                          ``dataset`` (payload), ``scenario``,
                                                      ``side.<name>``
evaluate   :class:`EvaluateJob`                       ``bundle``, ``dataset``, ...
derive     :class:`DeriveJob`                         ``bundle``, ``dataset``, ``side.<name>``
render     :class:`RenderJob`                         ``plots`` (a ``plots.json``)
========== ========================================== =========================================

Datasets are single-object ``*.zarr.zip`` files; a dataset's raw schema comes from the
``schema`` input, else the zarr root attribute ``mapi_schema`` (written by ingest), else it is
inferred. ``bundle`` is the ``bundle.zarr.zip`` of a fit and ``bundle.manifest`` the SDK
bundle manifest that fit uploaded as ``bundle.manifest.json``.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from mapi_spec._types import StrictModel
from mapi_spec.api import IngestMapping
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.loader import parse_document
from mapi_spec.spec import ModelSpec, ScoreRequest
from pydantic import Field, ValidationError

__all__ = [
    "BUNDLE_MANIFEST_OUTPUT",
    "DATASET_OUTPUTS",
    "INPUT_BUNDLE_MANIFEST",
    "INPUT_PLOTS",
    "INPUT_SCENARIO",
    "INPUT_SCHEMA",
    "INPUT_UPLOAD",
    "SIDE_TABLE_PREFIX",
    "DeriveJob",
    "DescribeJob",
    "EvaluateJob",
    "IngestJob",
    "MffOptions",
    "RenderJob",
    "ScoreJob",
    "parse_job",
]

INPUT_UPLOAD: Final = "upload"
INPUT_SCHEMA: Final = "schema"
INPUT_SCENARIO: Final = "scenario"
INPUT_BUNDLE_MANIFEST: Final = "bundle.manifest"
SIDE_TABLE_PREFIX: Final = "side."
INPUT_PLOTS: Final = "plots"

DATASET_OUTPUTS: Final = ("data.zarr.zip", "schema.json", "profile.json", "preview.json")
BUNDLE_MANIFEST_OUTPUT: Final = "bundle.manifest.json"
"""The SDK bundle manifest (content hashes, lineage). ``manifest.json`` is reserved for the
runner's :class:`~mapi_spec.run.OutputManifest`."""


class MffOptions(StrictModel):
    """The MFF ("model feature format") long-table preset.

    One row per ``(period, geography, product, campaign, outlet, creative, variable)`` with
    the value in ``value_column``. ``Period`` becomes the time dim; the other key columns
    become dims only when they hold more than one distinct value; ``VariableName`` values
    become data variables.
    """

    period_column: str = "Period"
    variable_column: str = "VariableName"
    value_column: str = "VariableValue"
    key_columns: dict[str, str] = Field(
        default_factory=lambda: {
            "Geography": "geo",
            "Product": "product",
            "Campaign": "campaign",
            "Outlet": "outlet",
            "Creative": "creative",
        },
        description="Key column -> dim name",
    )
    time_dim: str = "date"
    freq: str | None = Field(None, description="Pandas offset alias; inferred when omitted")
    roles: dict[str, str] = Field(default_factory=dict, description="Variable -> role")
    units: dict[str, str] = Field(default_factory=dict, description="Variable -> units")
    missing: Literal["forbid", "allow", "mask"] = "allow"


class IngestJob(StrictModel):
    """An ingest run: an explicit column mapping, or the MFF preset."""

    mapping: IngestMapping | None = None
    preset: Literal["mff"] | None = None
    mff: MffOptions = Field(default_factory=MffOptions)
    preview_rows: int = Field(50, ge=1, le=1000)
    max_rows: int = Field(50_000_000, ge=1)

    def model_post_init(self, _ctx: Any) -> None:
        if (self.mapping is None) == (self.preset is None):
            raise ValueError("give exactly one of 'mapping' or 'preset'")


class DescribeJob(StrictModel):
    """A describe run exports the image's catalog; it takes no parameters."""


class ScoreJob(StrictModel):
    """A score run: the request plus runner-side options."""

    request: ScoreRequest
    plots: list[str] | None = Field(
        None, description="Plot recipes to render from the result (default: by mode)"
    )
    max_draws: int | None = Field(None, ge=0)


class EvaluateJob(StrictModel):
    """Evaluate a bundle on ``dataset`` (default request: the fit-time evaluation request)."""

    request: ScoreRequest | None = None
    metrics: list[str] | None = None


class DeriveJob(StrictModel):
    """Compute declared derived quantities of a bundle over the ``dataset`` input."""

    names: list[str] = Field(..., min_length=1, max_length=32)
    draws: int | None = Field(None, ge=1)


class RenderJob(StrictModel):
    """Render plots from a ``plots.json`` to static images."""

    plots: list[str] | None = Field(None, description="Plot ids (default: all)")
    format: Literal["png", "svg"] = "png"
    scale: float = Field(2.0, gt=0, le=8)


Job = IngestJob | DescribeJob | ModelSpec | ScoreJob | EvaluateJob | DeriveJob | RenderJob


def _invalid(kind: RunKind, exc: Exception) -> MapiError:
    return MapiError(
        f"the {kind.value} run's spec is invalid",
        code=ErrorCode.SCHEMA_INVALID,
        pointer="/spec",
        untrusted_text=str(exc)[:8000],
    )


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
    except (ValidationError, ValueError) as exc:
        raise _invalid(kind, exc) from None
    except MapiError:
        raise
    raise NotImplementedError(
        f"the runner does not execute {kind.value!r} runs yet (optimize is M4)"
    )
