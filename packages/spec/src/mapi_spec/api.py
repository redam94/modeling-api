"""REST DTOs for the HTTP API (``/v1``; plan §8.2): M1 (v0.2) plus the M2 (v0.3) additions.

Conventions shared by every endpoint (see ``docs/reference/http-api.md``):

* **Requests** reject unknown fields (``extra="forbid"``); **responses** ignore unknown
  fields when parsed by a client, so older clients keep working when the server adds fields.
* Every resource carries ``tenant_id`` and ``project_id``. M1 is single-tenant: both are
  :data:`DEFAULT_TENANT_ID` / :data:`DEFAULT_PROJECT_ID`, but the identity is in every row
  and key so M2 can add row-level security without migrations churn.
* Lists return :class:`Page` (``items`` + opaque ``next_cursor``); errors are RFC 7807
  :class:`~mapi_spec.errors.Problem` documents with stable codes.
* Timestamps are timezone-aware RFC 3339 strings in UTC.

:data:`M1_ENDPOINTS` and :data:`M2_ENDPOINTS` (together :data:`ENDPOINTS`) are the
machine-readable endpoint tables; the server's route table and the reference docs are tested
against them. :data:`ROUTE_ACCESS` gives every operation its tenant/project scope and the
permission it checks. From v0.3 every M1 resource is project-scoped: the project comes from
the path or the ``X-Mapi-Project`` header (:func:`mapi_spec.tenancy.resolve_project_scope`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any, Final, Generic, Literal, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from mapi_spec._dto import Id as Id  # re-exported: clients annotate ids with it
from mapi_spec._dto import RequestModel as _Request
from mapi_spec._dto import ResponseModel as _Response
from mapi_spec._dto import Sha256 as Sha256
from mapi_spec._dto import TenantId as TenantId
from mapi_spec._dto import dto_examples as _examples
from mapi_spec._dto import new_id
from mapi_spec._types import Duration, LabelKey, LabelValue, MetricName, Slug
from mapi_spec.admin import ExecutorHealth, KillSwitches, KillSwitchUpdate
from mapi_spec.audit import AuditEvent
from mapi_spec.auth import PubSubPushEnvelope, SessionInfo
from mapi_spec.capabilities import Capabilities, Diagnostic
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.coords import DataSummary
from mapi_spec.cost import CostTotals, RunCost
from mapi_spec.errors import ErrorCode, Issue, Problem
from mapi_spec.executor import RunEventType, RunKind
from mapi_spec.plots import PlotSpec
from mapi_spec.quotas import QuotaLimitStatus, QuotaPolicy, QuotaPolicyUpsert
from mapi_spec.resources import ResourceClass
from mapi_spec.run import BundleManifest, ChainProgress, FailureClass, RunnerPhase, RunState
from mapi_spec.schema import DataSchema, DimSpec, Role
from mapi_spec.spec import CONTRACT_VERSION, DocumentKind, ScoreRequest
from mapi_spec.storage import Crc32c, SignedPut
from mapi_spec.tenancy import (
    DEFAULT_RESIDENCY,
    PROJECT_HEADER,
    Action,
    Membership,
    Principal,
    Project,
    ProjectRole,
    ResidencyRegion,
    ResourceKind,
    Tenant,
    TenantStatus,
    TrustTier,
)
from mapi_spec.tokens import ApiToken, TokenCreate, TokenCreated
from mapi_spec.validate import ResolvedSpec

__all__ = [
    "API_PREFIX",
    "ARTIFACT_LINK_TTL",
    "DEFAULT_PROJECT_ID",
    "DEFAULT_TENANT_ID",
    "ENDPOINTS",
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_RE",
    "IDEMPOTENCY_TTL_SECONDS",
    "LAST_EVENT_ID_HEADER",
    "M1_ENDPOINTS",
    "M2_ENDPOINTS",
    "MAX_PREFER_WAIT_SECONDS",
    "PAGE_LIMIT_DEFAULT",
    "PAGE_LIMIT_MAX",
    "PREFERENCE_APPLIED_HEADER",
    "PREFER_HEADER",
    "PROJECT_HEADER",
    "ROUTE_ACCESS",
    "RUN_CREATE_KINDS",
    "SSE_HEARTBEAT",
    "SSE_HEARTBEAT_SECONDS",
    "SSE_RETRY_MS",
    "ApiToken",
    "ArtifactLink",
    "AuditEvent",
    "CatalogResponse",
    "CostTotals",
    "Dataset",
    "DatasetCreate",
    "DatasetVersion",
    "DatasetVersionCreate",
    "DatasetVersionStatus",
    "DimMapping",
    "EndpointDecl",
    "ExecutorHealth",
    "Health",
    "HealthCheck",
    "IngestMapping",
    "KillSwitchUpdate",
    "KillSwitches",
    "Lineage",
    "LogLine",
    "Me",
    "MeProject",
    "MeTenant",
    "Membership",
    "MembershipCreate",
    "MembershipUpdate",
    "Model",
    "ModelVersion",
    "ObjectRef",
    "Page",
    "PlotListItem",
    "PlotSpec",
    "PlotValidateResponse",
    "ProgressSnapshot",
    "Project",
    "ProjectCreate",
    "ProjectUpdate",
    "PubSubPushEnvelope",
    "QuotaLimitStatus",
    "QuotaPolicy",
    "QuotaPolicyUpsert",
    "RouteAccess",
    "RouteScope",
    "Run",
    "RunCost",
    "RunCreate",
    "RunEventView",
    "RunRetry",
    "ScoreCall",
    "ScoreCallResult",
    "Scorecard",
    "ScorecardItem",
    "SessionInfo",
    "SpecCreate",
    "SpecDoc",
    "SpecRevision",
    "SpecRevisionCreate",
    "SseEventName",
    "StageSchema",
    "Tenant",
    "TenantAdminUpdate",
    "TenantCreate",
    "TenantUpdate",
    "TokenCreate",
    "TokenCreated",
    "UploadCreate",
    "UploadSession",
    "UsageReport",
    "ValidateRequest",
    "ValidateResponse",
    "VarMapping",
    "check_idempotency_key",
    "encode_sse",
    "loc_to_pointer",
    "new_id",
    "parse_last_event_id",
    "parse_prefer_wait",
    "route_access",
]

API_PREFIX: Final = "/v1"
DEFAULT_TENANT_ID: Final = "default"
DEFAULT_PROJECT_ID: Final = "default"

IDEMPOTENCY_KEY_HEADER: Final = "Idempotency-Key"
IDEMPOTENCY_TTL_SECONDS: Final = 24 * 3600
IDEMPOTENCY_KEY_RE: Final = re.compile(r"^[\x21-\x7e]{1,255}$")
"""1-255 printable, non-space ASCII characters."""
ARTIFACT_LINK_TTL: Final = timedelta(minutes=15)
"""Lifetime of signed artifact URLs handed to clients."""
LAST_EVENT_ID_HEADER: Final = "Last-Event-ID"
PREFER_HEADER: Final = "Prefer"
PREFERENCE_APPLIED_HEADER: Final = "Preference-Applied"
MAX_PREFER_WAIT_SECONDS: Final = 60
PAGE_LIMIT_DEFAULT: Final = 50
PAGE_LIMIT_MAX: Final = 200
SSE_HEARTBEAT_SECONDS: Final = 15
SSE_RETRY_MS: Final = 3000
SSE_HEARTBEAT: Final = ": keepalive\n\n"
"""Comment frame sent every :data:`SSE_HEARTBEAT_SECONDS` when no event was sent."""


# ===================================================================================== examples

_TS = "2026-10-15T09:30:00Z"
_RUN_ID = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
_DS_ID = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0"
_SPEC_ID = "spc_01jaf3b1c2d3e4f5g6h7j8k9m0"
_MODEL_ID = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0"
_UPLOAD_ID = "upl_01jaf3d1e2f3g4h5j6k7m8n9p0"
_SHA = "sha256:" + "3f" * 32
_SCHEMA_EXAMPLE: dict[str, Any] = {
    "dims": {
        "market": {"kind": "categorical"},
        "date": {"kind": "time", "policy": "extend", "freq": "D"},
    },
    "vars": {
        "revenue": {"dims": ["market", "date"], "dtype": "float64", "role": "target"},
    },
}
_E33_YAML_SHORT = (
    "apiVersion: mapi/v1\nkind: ModelSpec\nmetadata: {name: eu-geo-mmm, project: default}\n"
    "data:\n  dataset: dataset://default/eu_media_daily@latest\n"
    "model:\n  use: mmm.hierarchical_geo@^1.3\n"
)

T = TypeVar("T")


class Page(_Response, Generic[T]):  # noqa: UP046 - explicit TypeVar keeps pydantic naming stable
    """One page of a list endpoint. Pass ``next_cursor`` back as ``?cursor=`` for the next
    page; ``None`` means this is the last page. ``?limit=`` defaults to 50 (max 200)."""

    items: list[T]
    next_cursor: str | None = None

    model_config = _examples({"items": [], "next_cursor": "eyJpZCI6InJ1bl8wMWphZiJ9"})


class ObjectRef(_Response):
    """A stored object exposed through ``…/artifacts/{name}`` (fetch it as an ArtifactLink)."""

    name: str = Field(..., max_length=1024, examples=["preview.json"])
    size: int = Field(..., ge=0)
    content_type: str | None = None
    crc32c: Crc32c | None = None

    model_config = _examples(
        {"name": "preview.json", "size": 20480, "content_type": "application/json"}
    )


class ArtifactLink(_Response):
    """A short-lived, generation-pinned signed GET URL for one artifact (HTTP Range works)."""

    name: str = Field(..., max_length=1024)
    url: str
    method: Literal["GET"] = "GET"
    expires_at: AwareDatetime
    size: int = Field(..., ge=0)
    content_type: str | None = None
    crc32c: Crc32c
    generation: int = Field(..., ge=1)

    model_config = _examples(
        {
            "name": "bundle.zarr.zip",
            "url": "http://127.0.0.1:4443/download/storage/v1/b/mapi-local-bundles/o/"
            "t%2Fdefault%2Fmodels%2Fmdl_01%2F1%2Fbundle.zarr.zip?generation=7&X-Goog-Signature=…",
            "expires_at": "2026-10-15T09:45:00Z",
            "size": 2097152,
            "content_type": "application/zip",
            "crc32c": "4waSgw==",
            "generation": 7,
        }
    )


class HealthCheck(_Response):
    status: Literal["ok", "degraded", "fail"]
    detail: str | None = Field(None, max_length=500)
    latency_ms: float | None = Field(None, ge=0)

    model_config = _examples({"status": "ok", "latency_ms": 1.2})


class Health(_Response):
    """``/healthz`` (process liveness) and ``/readyz`` (dependencies) body."""

    status: Literal["ok", "degraded", "fail"]
    version: str
    contract_version: str = CONTRACT_VERSION
    api_version: Literal["v1"] = "v1"
    checks: dict[str, HealthCheck] = Field(default_factory=dict)

    model_config = _examples(
        {
            "status": "ok",
            "version": "0.2.0",
            "contract_version": "0.1",
            "api_version": "v1",
            "checks": {
                "postgres": {"status": "ok", "latency_ms": 1.2},
                "object_store": {"status": "ok", "latency_ms": 3.4},
            },
        }
    )


# ====================================================================================== datasets


class DatasetCreate(_Request):
    """Create an (empty) dataset; data arrives as versions."""

    name: Slug
    description: str | None = Field(None, max_length=4000)
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=64)

    model_config = _examples(
        {"name": "eu_media_daily", "description": "Daily spend and revenue per market"}
    )


class Dataset(_Response):
    id: Id
    tenant_id: TenantId
    project_id: TenantId
    name: Slug
    ref: str = Field(..., description="dataset://project/name (append @vN to pin)")
    description: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    latest_version: int | None = Field(None, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    model_config = _examples(
        {
            "id": _DS_ID,
            "tenant_id": "default",
            "project_id": "default",
            "name": "eu_media_daily",
            "ref": "dataset://default/eu_media_daily",
            "latest_version": 3,
            "created_at": _TS,
            "updated_at": _TS,
        }
    )


class UploadCreate(_Request):
    """Ask for a signed PUT URL into the quarantine bucket for one file."""

    filename: str = Field(..., min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f]+$")
    content_type: str = Field("application/octet-stream", max_length=128)
    size_bytes: int = Field(..., ge=1)

    model_config = _examples(
        {"filename": "eu_media_daily.csv", "content_type": "text/csv", "size_bytes": 5242880}
    )


class UploadSession(_Response):
    """Where to PUT the file. ``upload.headers`` must be sent verbatim; the URL expires."""

    id: Id
    tenant_id: TenantId
    project_id: TenantId
    dataset_id: Id
    filename: str
    content_type: str
    size_bytes: int
    status: Literal["pending", "uploaded", "consumed", "expired"]
    upload: SignedPut
    expires_at: AwareDatetime
    created_at: AwareDatetime

    model_config = _examples(
        {
            "id": _UPLOAD_ID,
            "tenant_id": "default",
            "project_id": "default",
            "dataset_id": _DS_ID,
            "filename": "eu_media_daily.csv",
            "content_type": "text/csv",
            "size_bytes": 5242880,
            "status": "pending",
            "upload": {
                "method": "PUT",
                "url": "http://127.0.0.1:4443/upload/storage/v1/b/mapi-local-quarantine/o"
                "?name=t%2Fdefault%2Fuploads%2Fupl_01&X-Goog-Signature=…",
                "name": f"t/default/uploads/{_UPLOAD_ID}",
                "expires_at": "2026-10-15T10:30:00Z",
                "headers": {
                    "content-type": "text/csv",
                    "x-goog-if-generation-match": "0",
                    "x-goog-content-length-range": "0,5242880",
                },
                "max_bytes": 5242880,
            },
            "expires_at": "2026-10-15T10:30:00Z",
            "created_at": _TS,
        }
    )


class DimMapping(_Request):
    """A dim of the dataset, read from one column of a tabular upload."""

    column: str = Field(..., min_length=1, max_length=256)
    spec: DimSpec

    model_config = _examples(
        {"column": "date", "spec": {"kind": "time", "policy": "extend", "freq": "D"}}
    )


class VarMapping(_Request):
    """A data variable read from one column (dtype is inferred during ingest)."""

    column: str = Field(..., min_length=1, max_length=256)
    role: Role = Role.COVARIATE
    units: str | None = Field(None, max_length=64)
    missing: Literal["forbid", "allow", "mask"] = "forbid"
    description: str | None = Field(None, max_length=2000)

    model_config = _examples({"column": "revenue", "role": "target", "units": "EUR"})


class IngestMapping(_Request):
    """How an upload becomes a labelled dataset (Meridian ``CoordToColumns`` style).

    Tabular formats need a mapping; ``zarr_zip``/``netcdf`` are self-describing and may omit
    ``dims``/``vars``. Parsing happens only inside a sandboxed ingest run.
    """

    format: Literal["csv", "parquet", "zarr_zip", "netcdf"]
    layout: Literal["dense", "long"] = "dense"
    dims: dict[str, DimMapping] = Field(default_factory=dict, max_length=32)
    vars: dict[str, VarMapping] = Field(default_factory=dict, max_length=1024)

    model_config = _examples(
        {
            "format": "csv",
            "dims": {
                "market": {"column": "market", "spec": {"kind": "categorical"}},
                "date": {
                    "column": "date",
                    "spec": {"kind": "time", "policy": "extend", "freq": "D"},
                },
            },
            "vars": {
                "revenue_local": {"column": "revenue", "role": "target", "units": "local"},
                "spend_local": {"column": "spend", "role": "spend", "units": "local"},
            },
        }
    )

    @model_validator(mode="after")
    def _tabular(self) -> IngestMapping:
        if self.format in ("csv", "parquet") and (not self.dims or not self.vars):
            raise ValueError("csv/parquet uploads need at least one dim and one var mapping")
        return self


class DatasetVersionCreate(_Request):
    """Turn a finished upload into a new version via a sandboxed ingest run (202)."""

    upload_id: Id
    mapping: IngestMapping
    message: str | None = Field(None, max_length=2000)

    model_config = _examples(
        {
            "upload_id": _UPLOAD_ID,
            "mapping": IngestMapping.model_json_schema()["examples"][0],
            "message": "Q3 refresh",
        }
    )


class DatasetVersionStatus(StrEnum):
    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"


class DatasetVersion(_Response):
    """An immutable, content-addressed version (``data.zarr.zip`` + schema + profile)."""

    dataset_id: Id
    tenant_id: TenantId
    project_id: TenantId
    version: int = Field(..., ge=1)
    ref: str = Field(..., description="dataset://project/name@vN")
    status: DatasetVersionStatus
    sha256: Sha256 | None = None
    size_bytes: int | None = Field(None, ge=0)
    data_schema: DataSchema | None = None
    summary: DataSummary | None = None
    profile: ObjectRef | None = None
    preview: ObjectRef | None = None
    ingest_run_id: Id | None = None
    issues: list[Issue] = Field(default_factory=list)
    message: str | None = None
    created_at: AwareDatetime
    ready_at: AwareDatetime | None = None

    model_config = _examples(
        {
            "dataset_id": _DS_ID,
            "tenant_id": "default",
            "project_id": "default",
            "version": 3,
            "ref": "dataset://default/eu_media_daily@v3",
            "status": "ready",
            "sha256": _SHA,
            "size_bytes": 1048576,
            "data_schema": _SCHEMA_EXAMPLE,
            "summary": {
                "vars": ["revenue"],
                "coords": {"market": {"kind": "categorical", "size": 2, "values": ["de", "fr"]}},
            },
            "preview": {"name": "preview.json", "size": 20480, "content_type": "application/json"},
            "ingest_run_id": _RUN_ID,
            "issues": [],
            "created_at": _TS,
            "ready_at": "2026-10-15T09:31:10Z",
        }
    )


# ========================================================================================= specs


class SpecCreate(_Request):
    """Create a spec document with its first revision (YAML 1.2 or JSON text)."""

    yaml: str = Field(..., min_length=1, max_length=256 * 1024)
    message: str | None = Field(None, max_length=2000)

    model_config = _examples({"yaml": _E33_YAML_SHORT, "message": "first draft"})


class SpecRevisionCreate(_Request):
    """Add an immutable revision. ``parent_revision`` guards against lost updates (409)."""

    yaml: str = Field(..., min_length=1, max_length=256 * 1024)
    parent_revision: int = Field(..., ge=1)
    message: str | None = Field(None, max_length=2000)

    model_config = _examples({"yaml": _E33_YAML_SHORT, "parent_revision": 1, "message": "tune"})


class SpecRevision(_Response):
    """One immutable, canonically hashed revision. ``issues`` holds its static validation."""

    spec_id: Id
    tenant_id: TenantId
    project_id: TenantId
    revision: int = Field(..., ge=1)
    ref: str = Field(..., description="spec://project/name@rN")
    kind: DocumentKind
    hash: Sha256
    yaml: str
    document: dict[str, JsonValue]
    valid: bool
    issues: list[Issue] = Field(default_factory=list)
    parent_revision: int | None = None
    message: str | None = None
    created_at: AwareDatetime

    model_config = _examples(
        {
            "spec_id": _SPEC_ID,
            "tenant_id": "default",
            "project_id": "default",
            "revision": 2,
            "ref": "spec://default/eu-geo-mmm@r2",
            "kind": "ModelSpec",
            "hash": _SHA,
            "yaml": _E33_YAML_SHORT,
            "document": {"apiVersion": "mapi/v1", "kind": "ModelSpec"},
            "valid": True,
            "issues": [],
            "parent_revision": 1,
            "created_at": _TS,
        }
    )


class SpecDoc(_Response):
    """A named spec and its latest revision."""

    id: Id
    tenant_id: TenantId
    project_id: TenantId
    name: Slug
    kind: DocumentKind
    ref: str = Field(..., description="spec://project/name")
    latest_revision: int = Field(..., ge=1)
    latest: SpecRevision | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    model_config = _examples(
        {
            "id": _SPEC_ID,
            "tenant_id": "default",
            "project_id": "default",
            "name": "eu-geo-mmm",
            "kind": "ModelSpec",
            "ref": "spec://default/eu-geo-mmm",
            "latest_revision": 2,
            "created_at": _TS,
            "updated_at": _TS,
        }
    )


class ValidateRequest(_Request):
    """Synchronous static validation of a document (exactly one of ``yaml``/``document``).

    ``dataset`` overrides the dataset version whose stored schema is the raw input schema
    (default: the spec's own ``data.dataset`` reference, resolved now).
    """

    yaml: str | None = Field(None, min_length=1, max_length=256 * 1024)
    document: dict[str, JsonValue] | None = None
    dataset: str | None = Field(None, examples=["dataset://default/eu_media_daily@v3"])

    model_config = _examples({"yaml": _E33_YAML_SHORT})

    @model_validator(mode="after")
    def _one(self) -> ValidateRequest:
        if (self.yaml is None) == (self.document is None):
            raise ValueError("give exactly one of 'yaml' or 'document'")
        return self


class StageSchema(_Response):
    """The data schema after one pipeline step (``raw`` = after binding, before any step)."""

    name: str
    data_schema: DataSchema | None = Field(None, description="None when the effect is dynamic")
    split: bool = Field(False, description="The train/holdout split happens after this stage")

    model_config = _examples({"name": "weekly", "data_schema": _SCHEMA_EXAMPLE, "split": True})


class ValidateResponse(_Response):
    """Every problem at once, located by JSON pointers, plus per-stage schemas."""

    valid: bool
    kind: DocumentKind | None = None
    hash: Sha256 | None = None
    issues: list[Issue] = Field(default_factory=list)
    stages: list[StageSchema] = Field(default_factory=list)
    resolved: ResolvedSpec | None = None
    catalog_digest: Sha256 | None = None

    model_config = _examples(
        {
            "valid": False,
            "kind": "ModelSpec",
            "hash": _SHA,
            "issues": [
                {
                    "code": "LEAKAGE_RISK",
                    "severity": "error",
                    "pointer": "/pipeline/2",
                    "message": "stateful step 'scale_y' runs before the split",
                    "suggested_action": "move the split to after 'weekly'",
                }
            ],
            "stages": [{"name": "raw", "data_schema": _SCHEMA_EXAMPLE, "split": False}],
            "catalog_digest": "sha256:" + "cd" * 32,
        }
    )


# ========================================================================================== runs

RUN_CREATE_KINDS: Final = frozenset(
    {RunKind.FIT, RunKind.VALIDATE, RunKind.SCORE, RunKind.EVALUATE, RunKind.DERIVE, RunKind.RENDER}
)
"""Kinds accepted by ``POST /v1/runs`` in M1 (ingest runs are created by dataset versions)."""

_SPEC_KINDS = frozenset({RunKind.FIT, RunKind.VALIDATE})


class RunCreate(_Request):
    """Submit a run. Send an ``Idempotency-Key`` header: a retry with the same key and body
    returns the original run (200); the same key with a different body is 409
    ``IDEMPOTENCY_CONFLICT``. Keys are scoped to the project and kept for 24 h.

    * ``fit`` / ``validate`` (sandboxed dry-run): ``spec_id`` + ``revision``;
    * ``score``: ``model_version_id`` + ``request``;
    * ``evaluate`` / ``derive`` / ``render``: ``model_version_id`` (+ ``params``).
    """

    kind: RunKind
    spec_id: Id | None = None
    revision: int | None = Field(None, ge=1)
    model_version_id: Id | None = None
    request: ScoreRequest | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    resource_class: ResourceClass | None = None
    timeout: Duration | None = None
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=64)

    model_config = _examples(
        {"kind": "fit", "spec_id": _SPEC_ID, "revision": 2, "resource_class": "M"},
        {
            "kind": "score",
            "model_version_id": "mv_01jaf3e1f2g3h4j5k6m7n8p9q0",
            "request": {
                "apiVersion": "mapi/v1",
                "kind": "ScoreRequest",
                "model": "model://default/eu-geo-mmm@v1",
                "mode": "forecast",
                "horizon": {"dim": "time", "periods": 8},
            },
        },
    )

    @model_validator(mode="after")
    def _shape(self) -> RunCreate:
        if self.kind not in RUN_CREATE_KINDS:
            raise ValueError(f"runs of kind {self.kind.value!r} cannot be created via POST /runs")
        has_spec = self.spec_id is not None or self.revision is not None
        if self.kind in _SPEC_KINDS:
            if self.spec_id is None or self.revision is None:
                raise ValueError(f"{self.kind.value} runs need spec_id and revision")
            if self.model_version_id is not None or self.request is not None:
                raise ValueError(f"{self.kind.value} runs take a spec, not a model version")
            return self
        if has_spec:
            raise ValueError(f"{self.kind.value} runs take a model_version_id, not a spec")
        if self.model_version_id is None:
            raise ValueError(f"{self.kind.value} runs need model_version_id")
        if (self.kind is RunKind.SCORE) != (self.request is not None):
            raise ValueError("'request' is required for score runs and only allowed for them")
        return self


class RunRetry(_Request):
    """``POST /runs/{id}:retry``: a *new* run from a failed one, optionally one class up."""

    resource_class: ResourceClass | None = None
    seed: int | None = Field(None, ge=0, lt=2**32)
    reason: str | None = Field(None, max_length=500)

    model_config = _examples({"resource_class": "L", "reason": "RESOURCE_EXHAUSTED at M"})


class ProgressSnapshot(_Response):
    """Latest coalesced progress (one upserted row per run)."""

    seq: int = Field(..., ge=0)
    phase: RunnerPhase
    fraction: float | None = Field(None, ge=0, le=1)
    message: str | None = None
    eta_seconds: float | None = Field(None, ge=0)
    chains: list[ChainProgress] = Field(default_factory=list)
    updated_at: AwareDatetime

    model_config = _examples(
        {
            "seq": 42,
            "phase": "sample",
            "fraction": 0.4,
            "message": "drawing samples",
            "eta_seconds": 95,
            "chains": [
                {"chain": 0, "draws_done": 400, "draws_total": 1000, "divergences": 0},
                {"chain": 1, "draws_done": 380, "draws_total": 1000, "divergences": 1},
            ],
            "updated_at": _TS,
        }
    )


class Run(_Response):
    """A unit of work and its lifecycle (see the state machine in ``mapi_spec.run``)."""

    id: Id
    tenant_id: TenantId
    project_id: TenantId
    kind: RunKind
    state: RunState
    failure_class: FailureClass | None = None
    problem: Problem | None = None
    attempt: int = Field(..., ge=1)
    spec_id: Id | None = None
    spec_revision: int | None = None
    spec_hash: Sha256 | None = None
    model_version_id: Id | None = None
    dataset_id: Id | None = None
    output_model_version_id: Id | None = Field(
        None, description="The model version a successful fit produced"
    )
    parent_run_id: Id | None = None
    retry_of: Id | None = None
    resource_class: ResourceClass
    timeout: Duration
    executor: str | None = None
    progress: ProgressSnapshot | None = None
    cost: RunCost = Field(default_factory=RunCost)
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: AwareDatetime
    queued_at: AwareDatetime | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    updated_at: AwareDatetime

    model_config = _examples(
        {
            "id": _RUN_ID,
            "tenant_id": "default",
            "project_id": "default",
            "kind": "fit",
            "state": "running",
            "attempt": 1,
            "spec_id": _SPEC_ID,
            "spec_revision": 2,
            "spec_hash": _SHA,
            "resource_class": "M",
            "timeout": "45m",
            "executor": "local-process",
            "progress": ProgressSnapshot.model_json_schema()["examples"][0],
            "cost": {"estimated": True},
            "created_at": _TS,
            "queued_at": _TS,
            "started_at": "2026-10-15T09:30:05Z",
            "updated_at": "2026-10-15T09:31:00Z",
        },
        {
            "id": _RUN_ID,
            "tenant_id": "default",
            "project_id": "default",
            "kind": "fit",
            "state": "failed",
            "failure_class": "resource_exhausted",
            "problem": {
                "type": "urn:mapi:error:RESOURCE_EXHAUSTED",
                "title": "Run exhausted its resources",
                "status": 507,
                "code": "RESOURCE_EXHAUSTED",
                "detail": "the worker was killed (exit 137); retry at class L",
            },
            "attempt": 1,
            "resource_class": "M",
            "timeout": "45m",
            "created_at": _TS,
            "finished_at": "2026-10-15T09:40:00Z",
            "updated_at": "2026-10-15T09:40:00Z",
        },
    )


class RunEventView(_Response):
    """A persisted run event (``id`` is the SSE event id, increasing per run)."""

    id: int = Field(..., ge=1)
    run_id: Id
    attempt: int = Field(..., ge=1)
    seq: int = Field(..., ge=0)
    ts: AwareDatetime
    type: RunEventType
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    model_config = _examples(
        {
            "id": 17,
            "run_id": _RUN_ID,
            "attempt": 1,
            "seq": 3,
            "ts": _TS,
            "type": "state",
            "payload": {"phase": "sample"},
        }
    )


class LogLine(_Response):
    """One captured line. ``line`` is untrusted text: render it as text only."""

    run_id: Id
    attempt: int = Field(..., ge=1)
    seq: int = Field(..., ge=0)
    ts: AwareDatetime
    stream: Literal["stdout", "stderr", "system"]
    line: str = Field(..., max_length=4096)
    truncated: bool = False

    model_config = _examples(
        {
            "run_id": _RUN_ID,
            "attempt": 1,
            "seq": 120,
            "ts": _TS,
            "stream": "stderr",
            "line": "Sampling 4 chains for 400 tune and 1000 draw iterations",
        }
    )


class SseEventName(StrEnum):
    """``event:`` names on ``GET /v1/runs/{id}/events`` (``text/event-stream``).

    ``snapshot`` (the full :class:`Run`, no id) is sent first on every connection; run-event
    types carry ``id:`` = :attr:`RunEventView.id`; ``end`` is sent once the run is terminal,
    then the server closes the stream.
    """

    SNAPSHOT = "snapshot"
    STATE = "state"
    PROGRESS = "progress"
    LOG = "log"
    DIAGNOSTIC = "diagnostic"
    WARNING = "warning"
    CHECKPOINT = "checkpoint"
    METRIC = "metric"
    COMPLETE = "complete"
    ERROR = "error"
    END = "end"


def encode_sse(
    event: SseEventName | str,
    data: BaseModel | dict[str, Any],
    *,
    id: int | str | None = None,
    retry_ms: int | None = None,
) -> str:
    """Serialise one SSE frame (``data`` is single-line compact JSON)."""
    name = SseEventName(event).value
    body = (
        data.model_dump_json(by_alias=True) if isinstance(data, BaseModel) else _compact_json(data)
    )
    lines = []
    if id is not None:
        text = str(id)
        if "\n" in text or "\r" in text:
            raise ValueError("SSE ids must be single-line")
        lines.append(f"id: {text}")
    if retry_ms is not None:
        lines.append(f"retry: {int(retry_ms)}")
    lines.append(f"event: {name}")
    lines.append(f"data: {body}")
    return "\n".join(lines) + "\n\n"


def _compact_json(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def parse_last_event_id(value: str | None) -> int | None:
    """Parse a ``Last-Event-ID`` header; anything but a positive integer means "from start"."""
    if value is None or not value.strip().isdigit():
        return None
    n = int(value.strip())
    return n if n > 0 else None


def check_idempotency_key(value: str) -> str:
    """Validate an ``Idempotency-Key`` header value (raises ``ValueError``)."""
    if not IDEMPOTENCY_KEY_RE.match(value):
        raise ValueError("Idempotency-Key must be 1-255 printable ASCII characters")
    return value


def loc_to_pointer(loc: tuple[str | int, ...] | list[str | int]) -> str:
    """Turn a request-validation location (``("body", "kind")``) into an RFC 6901 pointer
    (``/body/kind``) for ``Issue.pointer`` in 422 ``SCHEMA_INVALID`` problems."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in loc)


_WAIT_RE = re.compile(r"(?:^|[,;\s])wait\s*=\s*(\d{1,6})(?=$|[,;\s])", re.IGNORECASE)


def parse_prefer_wait(value: str | None) -> int | None:
    """``Prefer: wait=N`` (RFC 7240) capped at :data:`MAX_PREFER_WAIT_SECONDS`; else ``None``."""
    if not value:
        return None
    m = _WAIT_RE.search(value)
    if m is None:
        return None
    return min(int(m.group(1)), MAX_PREFER_WAIT_SECONDS)


# ======================================================================================== models


class ScorecardItem(_Response):
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    status: Literal["pass", "warn", "fail"]
    title: str = Field(..., max_length=200)
    message_plain: str = Field(..., max_length=2000)
    message_expert: str | None = Field(None, max_length=2000)
    metric: MetricName | None = None
    value: float | None = None
    threshold: float | None = None
    suggested_action: str | None = Field(None, max_length=2000)

    model_config = _examples(
        {
            "code": "RHAT_OK",
            "status": "pass",
            "title": "Chains agree",
            "message_plain": "All chains converged to the same answer.",
            "metric": "r_hat_max",
            "value": 1.004,
            "threshold": 1.01,
        }
    )


class Scorecard(_Response):
    """Sanity scorecard: convergence, holdout error and plausibility checks rolled up."""

    overall: Literal["pass", "warn", "fail"]
    items: list[ScorecardItem] = Field(default_factory=list)

    model_config = _examples(
        {
            "overall": "warn",
            "items": [
                {
                    "code": "RHAT_OK",
                    "status": "pass",
                    "title": "Chains agree",
                    "message_plain": "All chains converged to the same answer.",
                    "metric": "r_hat_max",
                    "value": 1.004,
                    "threshold": 1.01,
                },
                {
                    "code": "HOLDOUT_MAPE",
                    "status": "warn",
                    "title": "Holdout error",
                    "message_plain": "Forecasts were off by 14% on the last 8 weeks.",
                    "metric": "mape",
                    "value": 0.14,
                    "threshold": 0.1,
                    "suggested_action": "Add holidays or check data for the last weeks.",
                },
            ],
        }
    )


class Lineage(_Response):
    """Everything a model version came from (plan §8.6)."""

    run_id: Id
    attempt: int = Field(..., ge=1)
    spec_hash: Sha256
    dataset_sha: Sha256 | None = None
    side_tables: dict[str, Sha256] = Field(default_factory=dict)
    plugin: str | None = Field(None, examples=["mmm.hierarchical_geo@1.3.2"])
    base_image: str | None = None
    overlay_hash: Sha256 | None = None
    catalog_digest: Sha256 | None = None
    seed: int | None = None
    engine: str | None = None
    executor: str | None = None
    resource_class: ResourceClass | None = None
    lib_versions: dict[str, str] = Field(default_factory=dict)
    evaluation_of: Id | None = Field(
        None, description="evaluate_then_refit: the evaluation model version this refit pairs"
    )

    model_config = _examples(
        {
            "run_id": _RUN_ID,
            "attempt": 1,
            "spec_hash": _SHA,
            "dataset_sha": "sha256:" + "ef" * 32,
            "plugin": "mmm.hierarchical_geo@1.3.2",
            "base_image": "base-bayes@2026.10",
            "seed": 20261014,
            "engine": "nutpie",
            "executor": "local-process",
            "resource_class": "M",
            "lib_versions": {"pymc": "5.25.1"},
        }
    )


class Model(_Response):
    """A registry entry grouping the versions fitted from one spec name."""

    id: Id
    tenant_id: TenantId
    project_id: TenantId
    name: Slug
    ref: str = Field(..., description="model://project/name (append @vN or @latest)")
    latest_version: int | None = Field(None, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    model_config = _examples(
        {
            "id": _MODEL_ID,
            "tenant_id": "default",
            "project_id": "default",
            "name": "eu-geo-mmm",
            "ref": "model://default/eu-geo-mmm",
            "latest_version": 1,
            "created_at": _TS,
            "updated_at": _TS,
        }
    )


class ModelVersion(_Response):
    """A verified bundle: signed manifest, diagnostics, scorecard, lineage and metrics."""

    id: Id
    model_id: Id
    tenant_id: TenantId
    project_id: TenantId
    version: int = Field(..., ge=1)
    ref: str = Field(..., description="model://project/name@vN")
    status: Literal["ready", "partial"]
    run_id: Id
    spec_hash: Sha256
    manifest: BundleManifest
    capabilities: Capabilities | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    scorecard: Scorecard | None = None
    lineage: Lineage
    metrics: dict[MetricName, float] = Field(default_factory=dict)
    artifacts: list[ObjectRef] = Field(default_factory=list)
    created_at: AwareDatetime

    model_config = _examples(
        {
            "id": "mv_01jaf3e1f2g3h4j5k6m7n8p9q0",
            "model_id": _MODEL_ID,
            "tenant_id": "default",
            "project_id": "default",
            "version": 1,
            "ref": "model://default/eu-geo-mmm@v1",
            "status": "ready",
            "run_id": _RUN_ID,
            "spec_hash": _SHA,
            "manifest": BundleManifest.model_json_schema()["examples"][0],
            "diagnostics": [
                {
                    "code": "RHAT_OK",
                    "severity": "info",
                    "metric": "r_hat_max",
                    "value": 1.004,
                    "threshold": 1.01,
                    "message_expert": "max r-hat 1.004",
                    "message_plain": "All chains converged to the same answer.",
                }
            ],
            "scorecard": Scorecard.model_json_schema()["examples"][0],
            "lineage": Lineage.model_json_schema()["examples"][0],
            "metrics": {"mape": 0.14, "coverage_90": 0.88},
            "artifacts": [
                {"name": "bundle.zarr.zip", "size": 2097152, "content_type": "application/zip"}
            ],
            "created_at": _TS,
        }
    )


class ScoreCall(_Request):
    """Body of ``POST /v1/models/{model_id}/versions/{version}:score``.

    Send ``Prefer: wait=N`` (N ≤ 60) to wait for the result: the response is 200 with
    outputs if the run finished in time, else 202 with the run (poll it or follow its SSE).
    """

    request: ScoreRequest
    resource_class: ResourceClass | None = None
    timeout: Duration | None = None
    labels: dict[LabelKey, LabelValue] = Field(default_factory=dict, max_length=64)

    model_config = _examples(
        {
            "request": {
                "apiVersion": "mapi/v1",
                "kind": "ScoreRequest",
                "model": "model://default/eu-geo-mmm@v1",
                "mode": "forecast",
                "horizon": {"dim": "time", "periods": 8},
                "output": {"kind": "expected", "summary": ["mean", "hdi_90"]},
            }
        }
    )


class ScoreCallResult(_Response):
    """``done`` with output links (200), or the still-running run (202)."""

    done: bool
    run: Run
    outputs: list[ArtifactLink] = Field(default_factory=list)

    model_config = _examples(
        {
            "done": True,
            "run": {
                **Run.model_json_schema()["examples"][0],
                "kind": "score",
                "state": "succeeded",
                "progress": None,
            },
            "outputs": [ArtifactLink.model_json_schema()["examples"][0]],
        }
    )


# ========================================================================================= plots


class PlotListItem(_Response):
    """A plot available for a model version; fetch the sanitized :class:`PlotSpec` at href."""

    id: str = Field(..., pattern=r"^[A-Za-z0-9_.\-]{1,128}$")
    title: str = Field(..., max_length=300)
    kind: Literal["vega-lite", "plotly"]
    persona: Literal["guided", "expert", "both"] = "both"
    recipe: str | None = Field(None, max_length=128)
    caption_plain: str | None = Field(None, max_length=4000)
    href: str

    model_config = _examples(
        {
            "id": "core.ppc_fan",
            "title": "Posterior predictive fan",
            "kind": "vega-lite",
            "persona": "both",
            "recipe": "core.ppc_fan@1.0.0",
            "href": "/v1/models/mdl_01/versions/1/plots/core.ppc_fan",
        }
    )


class PlotValidateResponse(_Response):
    """Result of ``POST /v1/plots:validate`` (the server-side plot sanitizer)."""

    valid: bool
    issues: list[Issue] = Field(default_factory=list)
    sanitized: PlotSpec | None = None

    model_config = _examples(
        {
            "valid": False,
            "issues": [
                {
                    "code": "PLOT_UNSAFE",
                    "pointer": "/spec/data/url",
                    "message": "URL data sources are not allowed",
                }
            ],
        }
    )


# ======================================================================================= catalog


class CatalogResponse(_Response):
    """The static catalog the control plane validates against (no plugin code involved)."""

    digest: Sha256
    models: list[ModelCatalogEntry] = Field(default_factory=list)
    steps: list[StepCatalogEntry] = Field(default_factory=list)
    plots: list[PlotCatalogEntry] = Field(default_factory=list)
    generated_at: AwareDatetime | None = None

    model_config = _examples(
        {
            "digest": "sha256:" + "cd" * 32,
            "steps": [
                {
                    "id": "core.log1p",
                    "version": "1.0.0",
                    "kind": "value",
                    "stateful": False,
                    "invertible": True,
                }
            ],
            "plots": [{"id": "core.ppc_fan", "title": "PPC fan"}],
            "models": [],
        }
    )

    @field_validator("models", "steps", "plots", mode="before")
    @classmethod
    def _list(cls, v: Any) -> Any:
        return [] if v is None else v


# ===================================================================================== endpoints


@dataclass(frozen=True, slots=True)
class EndpointDecl:
    """One row of an endpoint table (used to test the server routes and the docs).

    ``response`` names a DTO (``Page[X]`` for lists); ``None`` means no JSON body
    (``204``, ``302``) or a server-defined body (``/internal/events``)."""

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    operation_id: str
    summary: str
    response: str | None
    success: int
    request: str | None = None
    errors: tuple[ErrorCode, ...] = ()
    headers: tuple[str, ...] = ()
    paginated: bool = False
    streaming: bool = False
    query: tuple[str, ...] = field(default=())


_E = ErrorCode
_NF = (_E.NOT_FOUND,)
_BODY = (_E.SCHEMA_INVALID,)
_PAGE = ("limit", "cursor")

M1_ENDPOINTS: Final[tuple[EndpointDecl, ...]] = (
    EndpointDecl("GET", "/healthz", "health", "Process liveness", "Health", 200),
    EndpointDecl("GET", "/readyz", "ready", "Dependencies reachable (503 if not)", "Health", 200),
    # identity / catalog
    EndpointDecl(
        "GET", "/v1/projects", "list_projects", "List projects", "Page[Project]", 200,
        paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/projects/{project_id}", "get_project", "Get a project", "Project", 200,
        errors=_NF,
    ),
    EndpointDecl("GET", "/v1/catalog", "get_catalog", "Full catalog", "CatalogResponse", 200),
    EndpointDecl(
        "GET", "/v1/catalog/models", "list_catalog_models", "Model plugins",
        "Page[ModelCatalogEntry]", 200, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/catalog/models/{model_id}/{version}", "get_catalog_model",
        "One model plugin version (manifest, sanitized schemas)", "ModelCatalogEntry", 200,
        errors=_NF,
    ),
    EndpointDecl(
        "GET", "/v1/catalog/steps", "list_catalog_steps", "Steps", "Page[StepCatalogEntry]",
        200, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/catalog/plots", "list_catalog_plots", "Plot recipes",
        "Page[PlotCatalogEntry]", 200, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/schemas/{name}", "get_schema", "Exported JSON Schema (e.g. modelspec.json)",
        None, 200, errors=_NF,
    ),
    # datasets
    EndpointDecl(
        "POST", "/v1/datasets", "create_dataset", "Create a dataset", "Dataset", 201,
        request="DatasetCreate", errors=(*_BODY, _E.CONFLICT),
    ),
    EndpointDecl(
        "GET", "/v1/datasets", "list_datasets", "List datasets", "Page[Dataset]", 200,
        paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/datasets/{dataset_id}", "get_dataset", "Get a dataset", "Dataset", 200,
        errors=_NF,
    ),
    EndpointDecl(
        "POST", "/v1/datasets/{dataset_id}/uploads", "create_upload",
        "Signed PUT URL into quarantine", "UploadSession", 201, request="UploadCreate",
        errors=(*_BODY, *_NF, _E.PAYLOAD_TOO_LARGE, _E.QUOTA_EXCEEDED),
    ),
    EndpointDecl(
        "POST", "/v1/datasets/{dataset_id}/versions", "create_dataset_version",
        "Ingest an upload as a new version (starts an ingest run)", "DatasetVersion", 202,
        request="DatasetVersionCreate", errors=(*_BODY, *_NF, _E.CONFLICT),
        headers=("Idempotency-Key",),
    ),
    EndpointDecl(
        "GET", "/v1/datasets/{dataset_id}/versions", "list_dataset_versions",
        "List versions", "Page[DatasetVersion]", 200, errors=_NF, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/datasets/{dataset_id}/versions/{version}", "get_dataset_version",
        "Schema, summary, profile and preview refs", "DatasetVersion", 200, errors=_NF,
    ),
    EndpointDecl(
        "GET", "/v1/datasets/{dataset_id}/versions/{version}/artifacts/{name}",
        "get_dataset_artifact", "Signed URL for data.zarr.zip / preview / profile / schema",
        "ArtifactLink", 200, errors=_NF,
    ),
    # specs
    EndpointDecl(
        "POST", "/v1/specs:validate", "validate_spec",
        "Synchronous static validation (issues at JSON pointers, per-stage schemas)",
        "ValidateResponse", 200, request="ValidateRequest",
        errors=(_E.SPEC_PARSE_ERROR, _E.PAYLOAD_TOO_LARGE, _E.SCHEMA_INVALID),
    ),
    EndpointDecl(
        "POST", "/v1/specs", "create_spec", "Create a spec with revision 1", "SpecDoc", 201,
        request="SpecCreate",
        errors=(_E.SPEC_PARSE_ERROR, _E.PAYLOAD_TOO_LARGE, _E.SCHEMA_INVALID, _E.CONFLICT),
    ),
    EndpointDecl(
        "GET", "/v1/specs", "list_specs", "List specs", "Page[SpecDoc]", 200,
        paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/specs/{spec_id}", "get_spec", "Get a spec", "SpecDoc", 200, errors=_NF,
    ),
    EndpointDecl(
        "POST", "/v1/specs/{spec_id}/revisions", "create_spec_revision",
        "Add an immutable revision (409 if parent_revision is stale)", "SpecRevision", 201,
        request="SpecRevisionCreate",
        errors=(*_NF, _E.SPEC_PARSE_ERROR, _E.PAYLOAD_TOO_LARGE, _E.SCHEMA_INVALID, _E.CONFLICT),
    ),
    EndpointDecl(
        "GET", "/v1/specs/{spec_id}/revisions", "list_spec_revisions", "List revisions",
        "Page[SpecRevision]", 200, errors=_NF, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/specs/{spec_id}/revisions/{revision}", "get_spec_revision",
        "Get one revision", "SpecRevision", 200, errors=_NF,
    ),
    EndpointDecl(
        "POST", "/v1/specs/{spec_id}/revisions/{revision}:dry-run", "dry_run_spec",
        "Sandboxed dry-run (a 'validate' run)", "Run", 202, errors=(*_NF, _E.QUOTA_EXCEEDED),
        headers=("Idempotency-Key",),
    ),
    # runs
    EndpointDecl(
        "POST", "/v1/runs", "create_run", "Submit a run (200 on idempotent replay)", "Run", 201,
        request="RunCreate",
        errors=(*_BODY, *_NF, _E.IDEMPOTENCY_CONFLICT, _E.QUOTA_EXCEEDED, _E.MODE_NOT_SUPPORTED,
                _E.LEAKAGE_RISK, _E.UNSEEN_LEVELS, _E.COORD_NOT_ALLOWED, _E.HORIZON_EXCEEDS_FIT,
                _E.MISSING_FUTURE_COVARIATE, _E.RECURSIVE_UNSUPPORTED),
        headers=("Idempotency-Key",),
    ),
    EndpointDecl(
        "GET", "/v1/runs", "list_runs", "List runs (filters: kind, state, spec_id)",
        "Page[Run]", 200, paginated=True, query=(*_PAGE, "kind", "state", "spec_id"),
    ),
    EndpointDecl("GET", "/v1/runs/{run_id}", "get_run", "Get a run", "Run", 200, errors=_NF),
    EndpointDecl(
        "GET", "/v1/runs/{run_id}/events", "run_events",
        "SSE stream (Accept: text/event-stream) or JSON page of persisted events",
        "Page[RunEventView]", 200, errors=_NF, headers=("Last-Event-ID",), paginated=True,
        streaming=True, query=("after", "limit"),
    ),
    EndpointDecl(
        "GET", "/v1/runs/{run_id}/logs", "run_logs", "Captured log lines (untrusted text)",
        "Page[LogLine]", 200, errors=_NF, paginated=True, query=(*_PAGE, "attempt", "stream"),
    ),
    EndpointDecl(
        "GET", "/v1/runs/{run_id}/plots", "list_run_plots",
        "Sanitized plots a finished run rendered (score/evaluate fans, counterfactuals)",
        "Page[PlotSpec]", 200, errors=(*_NF, _E.INTEGRITY_FAILURE), paginated=True,
        query=_PAGE,
    ),
    EndpointDecl(
        "POST", "/v1/runs/{run_id}:cancel", "cancel_run", "Request cancellation (idempotent)",
        "Run", 202, errors=(*_NF, _E.CONFLICT),
    ),
    EndpointDecl(
        "POST", "/v1/runs/{run_id}:resume", "resume_run", "Resume a PARTIAL run", "Run", 202,
        errors=(*_NF, _E.CONFLICT),
    ),
    EndpointDecl(
        "POST", "/v1/runs/{run_id}:retry", "retry_run", "New run from a failed one", "Run", 201,
        request="RunRetry", errors=(*_BODY, *_NF, _E.CONFLICT), headers=("Idempotency-Key",),
    ),
    # models
    EndpointDecl(
        "GET", "/v1/models", "list_models", "List models", "Page[Model]", 200,
        paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/models/{model_id}", "get_model", "Get a model", "Model", 200, errors=_NF,
    ),
    EndpointDecl(
        "GET", "/v1/models/{model_id}/versions", "list_model_versions", "List versions",
        "Page[ModelVersion]", 200, errors=_NF, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/models/{model_id}/versions/{version}", "get_model_version",
        "Signed manifest, diagnostics, scorecard, lineage, metrics", "ModelVersion", 200,
        errors=_NF,
    ),
    EndpointDecl(
        "GET", "/v1/models/{model_id}/versions/{version}/artifacts/{name}",
        "get_model_artifact", "Signed URL for a bundle object or ui/ view (name may contain /)",
        "ArtifactLink", 200, errors=(*_NF, _E.INTEGRITY_FAILURE),
    ),
    EndpointDecl(
        "POST", "/v1/models/{model_id}/versions/{version}:score", "score_model",
        "Score with replayed preprocessing (Prefer: wait=N → 200 if done, else 202)",
        "ScoreCallResult", 200, request="ScoreCall",
        errors=(*_BODY, *_NF, _E.MODE_NOT_SUPPORTED, _E.UNSEEN_LEVELS, _E.COORD_NOT_ALLOWED,
                _E.HORIZON_EXCEEDS_FIT, _E.MISSING_FUTURE_COVARIATE,
                _E.NON_CONTIGUOUS_EXTENSION, _E.CAPABILITY_UNSUPPORTED,
                _E.RECURSIVE_UNSUPPORTED, _E.QUOTA_EXCEEDED),
        headers=("Prefer", "Idempotency-Key"),
    ),
    # plots
    EndpointDecl(
        "GET", "/v1/models/{model_id}/versions/{version}/plots", "list_plots",
        "Plots of a model version", "Page[PlotListItem]", 200, errors=_NF, paginated=True,
        query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/models/{model_id}/versions/{version}/plots/{plot_id}", "get_plot",
        "A sanitized PlotSpec (render it in the plot sandbox origin)", "PlotSpec", 200,
        errors=_NF,
    ),
    EndpointDecl(
        "POST", "/v1/plots:validate", "validate_plot", "Run the plot sanitizer",
        "PlotValidateResponse", 200, request="PlotSpec", errors=_BODY,
    ),
)  # fmt: skip
"""The M1 endpoint table. ``request``/``response`` name DTOs in this module."""


# ================================================================================ M2: identity
#
# Multi-tenant additions (v0.3). Tenant-level resources live under /v1/tenants/{tenant_id};
# project-scoped resources (every M1 resource, tokens, usage) act in the project selected by
# the X-Mapi-Project header (see mapi_spec.tenancy.resolve_project_scope); operator
# endpoints live under /v1/admin; the browser session (BFF) under /auth.

_USR = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
_EMAIL_RE = r"^[^@\s]{1,64}@[^@\s]{1,255}$"


class MeProject(_Response):
    """A project the caller can act in, with the caller's effective role there."""

    tenant_id: TenantId
    project_id: TenantId
    name: str
    role: ProjectRole

    model_config = _examples(
        {"tenant_id": "acme", "project_id": "growth", "name": "Growth", "role": "runner"}
    )


class MeTenant(_Response):
    """A tenant the caller belongs to; ``role`` is the tenant-wide role (``None`` when the
    caller only has project memberships)."""

    tenant: Tenant
    role: ProjectRole | None = None
    projects: list[MeProject] = Field(default_factory=list)

    model_config = _examples(
        {
            "tenant": Tenant.model_json_schema()["examples"][0],
            "role": None,
            "projects": [MeProject.model_json_schema()["examples"][0]],
        }
    )


class Me(_Response):
    """``GET /v1/me``: who the caller is and where they may act.

    For a token, ``tenants`` holds exactly the token's project and ``token`` the token
    itself (its scopes narrow every role shown here).
    """

    principal: Principal
    auth: Literal["session", "token", "service"]
    operator: bool = False
    tenants: list[MeTenant] = Field(default_factory=list)
    token: ApiToken | None = None

    model_config = _examples(
        {
            "principal": {
                "kind": "user",
                "id": _USR,
                "issuer": "https://accounts.google.com",
                "subject": "110169484474386276334",
                "email": "ana@acme.example",
                "email_verified": True,
                "name": "Ana Analyst",
            },
            "auth": "session",
            "operator": False,
            "tenants": [MeTenant.model_json_schema()["examples"][0]],
        }
    )


class TenantCreate(_Request):
    """``POST /v1/admin/tenants`` (operators). ``initial_admin_email`` is invited as tenant
    admin; the invitation is claimed at first login with that verified e-mail."""

    id: TenantId
    name: str = Field(..., min_length=1, max_length=200)
    trust_tier: TrustTier = TrustTier.VERIFIED
    data_residency: ResidencyRegion = DEFAULT_RESIDENCY
    initial_admin_email: str | None = Field(None, max_length=320, pattern=_EMAIL_RE)

    model_config = _examples(
        {
            "id": "acme",
            "name": "Acme Retail",
            "trust_tier": "verified",
            "data_residency": "eu",
            "initial_admin_email": "it@acme.example",
        }
    )


class TenantUpdate(_Request):
    """``PATCH /v1/tenants/{tenant_id}`` (tenant admins): the display name only."""

    name: str = Field(..., min_length=1, max_length=200)

    model_config = _examples({"name": "Acme Retail Group"})


class TenantAdminUpdate(_Request):
    """``PATCH /v1/admin/tenants/{tenant_id}`` (operators). Changing ``data_residency`` only
    affects where *new* runs are placed; moving stored data is a separate procedure."""

    name: str | None = Field(None, min_length=1, max_length=200)
    trust_tier: TrustTier | None = None
    status: TenantStatus | None = None
    status_reason: str | None = Field(None, max_length=500)
    data_residency: ResidencyRegion | None = None

    model_config = _examples(
        {"status": "suspended", "status_reason": "payment overdue"},
        {"trust_tier": "internal"},
    )

    @model_validator(mode="after")
    def _some(self) -> TenantAdminUpdate:
        if not self.model_fields_set:
            raise ValueError("give at least one field to change")
        if (
            self.status is not None
            and self.status is not TenantStatus.ACTIVE
            and not self.status_reason
        ):
            raise ValueError("suspending or deleting a tenant needs a status_reason")
        return self


class ProjectCreate(_Request):
    """``POST /v1/tenants/{tenant_id}/projects`` (tenant admins)."""

    id: TenantId
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)

    model_config = _examples(
        {"id": "growth", "name": "Growth", "description": "EU media mix models"}
    )


class ProjectUpdate(_Request):
    """``PATCH /v1/tenants/{tenant_id}/projects/{project_id}`` (project admins)."""

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=4000)

    model_config = _examples({"description": "EU and UK media mix models"})

    @model_validator(mode="after")
    def _some(self) -> ProjectUpdate:
        if not self.model_fields_set:
            raise ValueError("give at least one field to change")
        return self


class MembershipCreate(_Request):
    """Add a member (tenant-wide or to one project, by path). Give a known ``principal_id``
    or an ``email`` to invite (claimed at first login with that verified e-mail)."""

    principal_id: Id | None = Field(None, pattern=r"^(?:usr|sa)_[0-9a-z]{26}$")
    email: str | None = Field(None, max_length=320, pattern=_EMAIL_RE)
    role: ProjectRole

    model_config = _examples(
        {"email": "ana@acme.example", "role": "runner"},
        {"principal_id": _USR, "role": "viewer"},
    )

    @model_validator(mode="after")
    def _one(self) -> MembershipCreate:
        if (self.principal_id is None) == (self.email is None):
            raise ValueError("give exactly one of 'principal_id' or 'email'")
        return self


class MembershipUpdate(_Request):
    """Change a member's role (the last admin of a tenant or project cannot be demoted)."""

    role: ProjectRole

    model_config = _examples({"role": "editor"})


# =================================================================================== M2: usage

UsagePeriod = Literal["day", "month"]
UsageGroupBy = Literal["resource_class", "project", "day", "token", "run"]


class UsageReport(_Response):
    """``GET /v1/usage`` (one project) and ``GET /v1/tenants/{tenant_id}/usage`` (whole
    tenant): ledger totals for one UTC period and the quota limits in force now.

    Query: ``period=day|month`` (default ``month``), ``start`` (an RFC 3339 instant inside
    the period; default: now), ``group_by``.
    """

    tenant_id: TenantId
    project_id: TenantId | None = None
    period: UsagePeriod
    start: AwareDatetime
    end: AwareDatetime
    totals: CostTotals
    group_by: UsageGroupBy | None = None
    groups: dict[str, CostTotals] = Field(default_factory=dict)
    price_versions: list[str] = Field(default_factory=list)
    quotas: list[QuotaLimitStatus] = Field(default_factory=list)

    model_config = _examples(
        {
            "tenant_id": "acme",
            "project_id": "growth",
            "period": "month",
            "start": "2026-10-01T00:00:00Z",
            "end": "2026-11-01T00:00:00Z",
            "totals": CostTotals.model_json_schema()["examples"][0],
            "group_by": "resource_class",
            "groups": {"M": CostTotals.model_json_schema()["examples"][0]},
            "price_versions": ["cloudrun-list-2026-10"],
            "quotas": [QuotaLimitStatus.model_json_schema()["examples"][0]],
        }
    )

    @model_validator(mode="after")
    def _window(self) -> UsageReport:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


# ========================================================================== M2: route access


class RouteScope(StrEnum):
    """What a route needs before its permission check (see :data:`ROUTE_ACCESS`)."""

    PUBLIC = "public"
    """No credentials (health, schemas, the login flow)."""
    AUTHENTICATED = "authenticated"
    """Any principal; results are filtered to what the caller can see."""
    TENANT = "tenant"
    """``{tenant_id}`` from the path; tenant-level grants."""
    PROJECT = "project"
    """The project from the path or ``X-Mapi-Project`` (or the token's binding)."""
    PLATFORM = "platform"
    """Platform operators only."""
    INTERNAL = "internal"
    """Internal services only (Pub/Sub OIDC push, orchestrator)."""


@dataclass(frozen=True, slots=True)
class RouteAccess:
    """How one operation is scoped and authorized.

    ``resource``/``action`` name the :data:`~mapi_spec.tenancy.PERMISSIONS` entry checked
    with :meth:`~mapi_spec.tenancy.AuthContext.allows`; ``None`` means membership in the
    scope suffices (lists are filtered by visibility). Handlers add the secondary checks in
    ``also`` when they apply (e.g. a ``score`` run also needs ``run`` on the model).
    """

    scope: RouteScope
    resource: ResourceKind | None = None
    action: Action | None = None
    also: tuple[tuple[ResourceKind, Action], ...] = ()

    def __post_init__(self) -> None:
        if (self.resource is None) != (self.action is None):
            raise ValueError("resource and action go together")


# ====================================================================================== M2 table

_FB = (_E.FORBIDDEN,)
_PH = (PROJECT_HEADER,)

M2_ENDPOINTS: Final[tuple[EndpointDecl, ...]] = (
    # identity
    EndpointDecl(
        "GET", "/v1/me", "get_me", "The caller, their tenants, projects and roles", "Me", 200,
        errors=(_E.UNAUTHENTICATED,),
    ),
    EndpointDecl(
        "GET", "/v1/tenants", "list_tenants", "Tenants the caller belongs to", "Page[Tenant]",
        200, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/tenants/{tenant_id}", "get_tenant", "Get a tenant", "Tenant", 200,
        errors=_NF,
    ),
    EndpointDecl(
        "PATCH", "/v1/tenants/{tenant_id}", "update_tenant", "Rename a tenant (tenant admin)",
        "Tenant", 200, request="TenantUpdate", errors=(*_BODY, *_NF, *_FB),
    ),
    EndpointDecl(
        "GET", "/v1/tenants/{tenant_id}/projects", "list_tenant_projects",
        "Projects of a tenant visible to the caller", "Page[Project]", 200, errors=_NF,
        paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "POST", "/v1/tenants/{tenant_id}/projects", "create_project",
        "Create a project (tenant admin)", "Project", 201, request="ProjectCreate",
        errors=(*_BODY, *_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "PATCH", "/v1/tenants/{tenant_id}/projects/{project_id}", "update_project",
        "Rename or describe a project (project admin)", "Project", 200,
        request="ProjectUpdate", errors=(*_BODY, *_NF, *_FB),
    ),
    EndpointDecl(
        "DELETE", "/v1/tenants/{tenant_id}/projects/{project_id}", "delete_project",
        "Delete an empty project (tenant admin; 409 while it holds data)", None, 204,
        errors=(*_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "GET", "/v1/tenants/{tenant_id}/members", "list_tenant_members",
        "Tenant-wide members", "Page[Membership]", 200, errors=_NF, paginated=True,
        query=_PAGE,
    ),
    EndpointDecl(
        "POST", "/v1/tenants/{tenant_id}/members", "add_tenant_member",
        "Grant a tenant-wide role (tenant admin)", "Membership", 201,
        request="MembershipCreate", errors=(*_BODY, *_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "PATCH", "/v1/tenants/{tenant_id}/members/{principal_id}", "update_tenant_member",
        "Change a tenant-wide role (tenant admin)", "Membership", 200,
        request="MembershipUpdate", errors=(*_BODY, *_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "DELETE", "/v1/tenants/{tenant_id}/members/{principal_id}", "remove_tenant_member",
        "Remove a tenant-wide role (409 for the last admin)", None, 204,
        errors=(*_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "GET", "/v1/tenants/{tenant_id}/projects/{project_id}/members", "list_members",
        "Project members (including tenant-wide grants)", "Page[Membership]", 200,
        errors=_NF, paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "POST", "/v1/tenants/{tenant_id}/projects/{project_id}/members", "add_member",
        "Grant a project role (project admin)", "Membership", 201,
        request="MembershipCreate", errors=(*_BODY, *_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "PATCH", "/v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}",
        "update_member", "Change a project role (project admin)", "Membership", 200,
        request="MembershipUpdate", errors=(*_BODY, *_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "DELETE", "/v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}",
        "remove_member", "Remove a project role (409 for the last admin)", None, 204,
        errors=(*_NF, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "GET", "/v1/tenants/{tenant_id}/usage", "get_tenant_usage",
        "Cost and quota of the whole tenant for a period (tenant admin)", "UsageReport", 200,
        errors=(*_NF, *_FB), query=("period", "start", "group_by"),
    ),
    EndpointDecl(
        "GET", "/v1/tenants/{tenant_id}/audit", "list_tenant_audit",
        "The tenant's audit log (tenant admin)", "Page[AuditEvent]", 200,
        errors=(*_NF, *_FB), paginated=True,
        query=(*_PAGE, "project_id", "actor_id", "action", "outcome", "since", "until"),
    ),
    # tokens (project-scoped)
    EndpointDecl(
        "GET", "/v1/tokens", "list_tokens",
        "The caller's tokens in the project (all=true: everyone's, project admin)",
        "Page[ApiToken]", 200, paginated=True, query=(*_PAGE, "all", "status"), headers=_PH,
    ),
    EndpointDecl(
        "POST", "/v1/tokens", "create_token", "Mint a token (the secret is shown once)",
        "TokenCreated", 201, request="TokenCreate", errors=(*_BODY, *_FB), headers=_PH,
    ),
    EndpointDecl(
        "GET", "/v1/tokens/{token_id}", "get_token", "Get a token (never its secret)",
        "ApiToken", 200, errors=_NF, headers=_PH,
    ),
    EndpointDecl(
        "DELETE", "/v1/tokens/{token_id}", "revoke_token", "Revoke a token (idempotent)",
        None, 204, errors=(*_NF, *_FB), headers=_PH,
    ),
    # usage (project-scoped)
    EndpointDecl(
        "GET", "/v1/usage", "get_usage", "Cost and quota of the project for a period",
        "UsageReport", 200, query=("period", "start", "group_by"), headers=_PH,
    ),
    # operators
    EndpointDecl(
        "GET", "/v1/admin/tenants", "admin_list_tenants", "All tenants (filters: status, tier)",
        "Page[Tenant]", 200, errors=_FB, paginated=True,
        query=(*_PAGE, "status", "trust_tier"),
    ),
    EndpointDecl(
        "POST", "/v1/admin/tenants", "admin_create_tenant", "Create a tenant", "Tenant", 201,
        request="TenantCreate", errors=(*_BODY, *_FB, _E.CONFLICT),
    ),
    EndpointDecl(
        "GET", "/v1/admin/tenants/{tenant_id}", "admin_get_tenant", "Get any tenant", "Tenant",
        200, errors=(*_NF, *_FB),
    ),
    EndpointDecl(
        "PATCH", "/v1/admin/tenants/{tenant_id}", "admin_update_tenant",
        "Change a tenant's tier, status or residency", "Tenant", 200,
        request="TenantAdminUpdate", errors=(*_BODY, *_NF, *_FB),
    ),
    EndpointDecl(
        "GET", "/v1/admin/quotas", "admin_list_quotas", "Quota policies (filters: scope, subject)",
        "Page[QuotaPolicy]", 200, errors=_FB, paginated=True,
        query=(*_PAGE, "scope", "subject_id"),
    ),
    EndpointDecl(
        "PUT", "/v1/admin/quotas", "admin_put_quota", "Create or replace one quota policy",
        "QuotaPolicy", 200, request="QuotaPolicyUpsert", errors=(*_BODY, *_FB),
    ),
    EndpointDecl(
        "DELETE", "/v1/admin/quotas/{scope}/{subject_id}", "admin_delete_quota",
        "Remove a policy (the tier default applies again)", None, 204, errors=(*_NF, *_FB),
    ),
    EndpointDecl(
        "GET", "/v1/admin/kill-switches", "admin_get_kill_switches", "Current kill switches",
        "KillSwitches", 200, errors=_FB,
    ),
    EndpointDecl(
        "PUT", "/v1/admin/kill-switches", "admin_put_kill_switches",
        "Flip kill switches (audited; reason required)", "KillSwitches", 200,
        request="KillSwitchUpdate", errors=(*_BODY, *_FB),
    ),
    EndpointDecl(
        "GET", "/v1/admin/executors", "admin_list_executors",
        "Executor health, caps and conformance", "Page[ExecutorHealth]", 200, errors=_FB,
        paginated=True, query=_PAGE,
    ),
    EndpointDecl(
        "GET", "/v1/admin/executors/{name}", "admin_get_executor", "One executor",
        "ExecutorHealth", 200, errors=(*_NF, *_FB),
    ),
    EndpointDecl(
        "GET", "/v1/admin/audit", "admin_list_audit", "The platform audit log",
        "Page[AuditEvent]", 200, errors=_FB, paginated=True,
        query=(*_PAGE, "tenant_id", "project_id", "actor_id", "action", "outcome", "since",
               "until"),
    ),
    # browser session (BFF)
    EndpointDecl(
        "GET", "/auth/login", "auth_login",
        "Start OIDC sign-in (302 to the identity provider; Authorization Code + PKCE)",
        None, 302, query=("return_to",),
    ),
    EndpointDecl(
        "GET", "/auth/callback", "auth_callback",
        "Finish sign-in: verify state, exchange the code, set the session cookie, 302 back",
        None, 302, errors=(_E.UNAUTHENTICATED,), query=("code", "state", "error"),
    ),
    EndpointDecl(
        "POST", "/auth/logout", "auth_logout", "End the session and clear the cookie", None,
        204,
    ),
    EndpointDecl(
        "GET", "/auth/session", "auth_session", "Is the browser signed in, as whom",
        "SessionInfo", 200,
    ),
    # internal
    EndpointDecl(
        "POST", "/internal/events", "ingest_event",
        "Pub/Sub push of run events (OIDC-authenticated service account only)", None, 200,
        request="PubSubPushEnvelope", errors=(_E.UNAUTHENTICATED, _E.SCHEMA_INVALID),
    ),
)  # fmt: skip
"""The M2 endpoint table (additive to :data:`M1_ENDPOINTS`)."""

ENDPOINTS: Final[tuple[EndpointDecl, ...]] = M1_ENDPOINTS + M2_ENDPOINTS
"""Every endpoint of the v0.3 API."""

_RK, _AC = ResourceKind, Action


def _p(resource: ResourceKind, action: Action, *also: tuple[ResourceKind, Action]) -> RouteAccess:
    return RouteAccess(RouteScope.PROJECT, resource, action, tuple(also))


def _t(resource: ResourceKind | None = None, action: Action | None = None) -> RouteAccess:
    return RouteAccess(RouteScope.TENANT, resource, action)


def _op(resource: ResourceKind, action: Action) -> RouteAccess:
    return RouteAccess(RouteScope.PLATFORM, resource, action)


_PUBLIC = RouteAccess(RouteScope.PUBLIC)
_AUTH = RouteAccess(RouteScope.AUTHENTICATED)

ROUTE_ACCESS: Final[dict[str, RouteAccess]] = {
    # M1 (project-scoped from v0.3)
    "health": _PUBLIC,
    "ready": _PUBLIC,
    "get_schema": _PUBLIC,
    "list_projects": _AUTH,
    "get_project": _p(_RK.PROJECT, _AC.READ),
    "get_catalog": _AUTH,
    "list_catalog_models": _AUTH,
    "get_catalog_model": _AUTH,
    "list_catalog_steps": _AUTH,
    "list_catalog_plots": _AUTH,
    "create_dataset": _p(_RK.DATASET, _AC.CREATE),
    "list_datasets": _p(_RK.DATASET, _AC.READ),
    "get_dataset": _p(_RK.DATASET, _AC.READ),
    "create_upload": _p(_RK.DATASET, _AC.UPDATE),
    "create_dataset_version": _p(_RK.DATASET, _AC.UPDATE),
    "list_dataset_versions": _p(_RK.DATASET, _AC.READ),
    "get_dataset_version": _p(_RK.DATASET, _AC.READ),
    "get_dataset_artifact": _p(_RK.DATASET, _AC.READ),
    "validate_spec": _p(_RK.SPEC, _AC.READ),
    "create_spec": _p(_RK.SPEC, _AC.CREATE),
    "list_specs": _p(_RK.SPEC, _AC.READ),
    "get_spec": _p(_RK.SPEC, _AC.READ),
    "create_spec_revision": _p(_RK.SPEC, _AC.UPDATE),
    "list_spec_revisions": _p(_RK.SPEC, _AC.READ),
    "get_spec_revision": _p(_RK.SPEC, _AC.READ),
    "dry_run_spec": _p(_RK.RUN, _AC.CREATE, (_RK.SPEC, _AC.READ)),
    "create_run": _p(_RK.RUN, _AC.CREATE, (_RK.MODEL, _AC.RUN)),
    "list_runs": _p(_RK.RUN, _AC.READ),
    "get_run": _p(_RK.RUN, _AC.READ),
    "run_events": _p(_RK.RUN, _AC.READ),
    "run_logs": _p(_RK.RUN, _AC.READ),
    "list_run_plots": _p(_RK.RUN, _AC.READ),
    "cancel_run": _p(_RK.RUN, _AC.CANCEL),
    "resume_run": _p(_RK.RUN, _AC.CREATE),
    "retry_run": _p(_RK.RUN, _AC.CREATE),
    "list_models": _p(_RK.MODEL, _AC.READ),
    "get_model": _p(_RK.MODEL, _AC.READ),
    "list_model_versions": _p(_RK.MODEL, _AC.READ),
    "get_model_version": _p(_RK.MODEL, _AC.READ),
    "get_model_artifact": _p(_RK.MODEL, _AC.READ),
    "score_model": _p(_RK.MODEL, _AC.RUN),
    "list_plots": _p(_RK.PLOT, _AC.READ),
    "get_plot": _p(_RK.PLOT, _AC.READ),
    "validate_plot": _p(_RK.PLOT, _AC.READ),
    # M2 identity
    "get_me": _AUTH,
    "list_tenants": _AUTH,
    "get_tenant": _t(_RK.TENANT, _AC.READ),
    "update_tenant": _t(_RK.TENANT, _AC.UPDATE),
    "list_tenant_projects": _t(),
    "create_project": _t(_RK.PROJECT, _AC.CREATE),
    "update_project": _p(_RK.PROJECT, _AC.UPDATE),
    "delete_project": _t(_RK.PROJECT, _AC.DELETE),
    "list_tenant_members": _t(_RK.TENANT_MEMBER, _AC.READ),
    "add_tenant_member": _t(_RK.TENANT_MEMBER, _AC.CREATE),
    "update_tenant_member": _t(_RK.TENANT_MEMBER, _AC.UPDATE),
    "remove_tenant_member": _t(_RK.TENANT_MEMBER, _AC.DELETE),
    "list_members": _p(_RK.MEMBER, _AC.READ),
    "add_member": _p(_RK.MEMBER, _AC.CREATE),
    "update_member": _p(_RK.MEMBER, _AC.UPDATE),
    "remove_member": _p(_RK.MEMBER, _AC.DELETE),
    "get_tenant_usage": _t(_RK.BILLING, _AC.READ),
    "list_tenant_audit": _t(_RK.AUDIT, _AC.READ),
    "list_tokens": _p(_RK.TOKEN, _AC.READ),
    "create_token": _p(_RK.TOKEN, _AC.CREATE),
    "get_token": _p(_RK.TOKEN, _AC.READ),
    "revoke_token": _p(_RK.TOKEN, _AC.DELETE),
    "get_usage": _p(_RK.USAGE, _AC.READ),
    # operators
    "admin_list_tenants": _op(_RK.TENANT, _AC.READ),
    "admin_create_tenant": _op(_RK.TENANT, _AC.CREATE),
    "admin_get_tenant": _op(_RK.TENANT, _AC.READ),
    "admin_update_tenant": _op(_RK.TENANT, _AC.MANAGE),
    "admin_list_quotas": _op(_RK.QUOTA, _AC.READ),
    "admin_put_quota": _op(_RK.QUOTA, _AC.UPDATE),
    "admin_delete_quota": _op(_RK.QUOTA, _AC.DELETE),
    "admin_get_kill_switches": _op(_RK.KILL_SWITCH, _AC.READ),
    "admin_put_kill_switches": _op(_RK.KILL_SWITCH, _AC.UPDATE),
    "admin_list_executors": _op(_RK.EXECUTOR, _AC.READ),
    "admin_get_executor": _op(_RK.EXECUTOR, _AC.READ),
    "admin_list_audit": _op(_RK.AUDIT, _AC.READ),
    # session and internal
    "auth_login": _PUBLIC,
    "auth_callback": _PUBLIC,
    "auth_logout": _PUBLIC,
    "auth_session": _PUBLIC,
    "ingest_event": RouteAccess(RouteScope.INTERNAL, _RK.EVENT, _AC.CREATE),
}
"""Tenant/project scoping and the permission checked for every operation in
:data:`ENDPOINTS` (keyed by ``operation_id``).

Acting on *other principals'* tokens (``list_tokens?all=true``, revoking someone else's
token) additionally needs ``manage`` on ``token``. A ``create_run`` for a model version
(score, evaluate, derive, render) additionally needs ``run`` on ``model``.
"""


def route_access(operation_id: str) -> RouteAccess:
    """The :class:`RouteAccess` of an operation (``KeyError`` for unknown operations)."""
    return ROUTE_ACCESS[operation_id]
