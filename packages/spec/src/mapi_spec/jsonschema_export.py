"""Export JSON Schemas (2020-12) for every public document and contract model.

``GET /schemas/modelspec.json`` serves :func:`export_schemas` output; Monaco and JSON Forms
use it, and CI snapshots it to detect drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

from mapi_spec import api
from mapi_spec.calibration import Calibration, PriorTranslationResult
from mapi_spec.capabilities import Diagnostic, ModelManifest, OutputSelector
from mapi_spec.catalog import Catalog, ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.coords import DataSummary
from mapi_spec.effects import SchemaEffect
from mapi_spec.errors import Problem
from mapi_spec.executor import RunEvent
from mapi_spec.plots import PlotSpec
from mapi_spec.run import BundleManifest, OutputManifest, RunSpec
from mapi_spec.schema import DataSchema, DataSchemaTemplate
from mapi_spec.spec import (
    CONTRACT_VERSION,
    SBC,
    AnyDocument,
    Backtest,
    Campaign,
    ModelSpec,
    ScoreRequest,
    Sweep,
)
from mapi_spec.storage import ObjectMeta, PostPolicy, SignedGet, SignedPut
from mapi_spec.views import ResponseGridView

__all__ = ["API_SCHEMA_MODELS", "SCHEMA_BASE_URI", "SCHEMA_MODELS", "export_schemas"]

SCHEMA_BASE_URI = "https://schemas.mapi.dev/v1/"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "modelspec.json": ModelSpec,
    "scorerequest.json": ScoreRequest,
    "sweep.json": Sweep,
    "backtest.json": Backtest,
    "sbc.json": SBC,
    "campaign.json": Campaign,
    "dataschema.json": DataSchema,
    "dataschematemplate.json": DataSchemaTemplate,
    "datasummary.json": DataSummary,
    "schemaeffect.json": SchemaEffect,
    "modelmanifest.json": ModelManifest,
    "outputselector.json": OutputSelector,
    "diagnostic.json": Diagnostic,
    "plotspec.json": PlotSpec,
    "problem.json": Problem,
    "catalog.json": Catalog,
    "catalog-step.json": StepCatalogEntry,
    "catalog-model.json": ModelCatalogEntry,
    "catalog-plot.json": PlotCatalogEntry,
    # run protocol (M1)
    "runspec.json": RunSpec,
    "runevent.json": RunEvent,
    "outputmanifest.json": OutputManifest,
    "bundlemanifest.json": BundleManifest,
    "objectmeta.json": ObjectMeta,
    "signedget.json": SignedGet,
    "signedput.json": SignedPut,
    "postpolicy.json": PostPolicy,
    # M3: sandbox outputs and views the control plane and the web app read
    "priortranslation.json": PriorTranslationResult,
    "responsegrid.json": ResponseGridView,
    "calibration.json": Calibration,
}

API_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    f"api-{name}.json": model
    for name, model in sorted(
        (n, getattr(api, n))
        for n in api.__all__
        if isinstance(getattr(api, n), type)
        and issubclass(getattr(api, n), BaseModel)
        and getattr(api, n) is not api.Page
        and getattr(api, n) is not PlotSpec
    )
}
"""REST DTOs of :mod:`mapi_spec.api` (``api-<Name>.json``); ``Page[T]`` is per-endpoint."""


def _finish(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": SCHEMA_BASE_URI + name,
        "x-mapi-contract-version": CONTRACT_VERSION,
        **schema,
    }


def export_schemas(out_dir: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{filename: JSON Schema}``; also write the files if ``out_dir`` is given."""
    schemas: dict[str, dict[str, Any]] = {}
    for name, model in (SCHEMA_MODELS | API_SCHEMA_MODELS).items():
        schemas[name] = _finish(name, model.model_json_schema(by_alias=True, mode="validation"))
    schemas["document.json"] = _finish(
        "document.json", TypeAdapter(AnyDocument).json_schema(by_alias=True, mode="validation")
    )
    if out_dir is not None:
        path = Path(out_dir)
        path.mkdir(parents=True, exist_ok=True)
        for name, schema in schemas.items():
            (path / name).write_text(
                json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    return schemas
