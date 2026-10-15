"""SQLAlchemy 2 ORM models (the control plane's only state).

Every table carries ``tenant_id`` and ``project_id`` (M1 is single-tenant, so both are
``default``) so M2 can switch on row-level security without migration churn. JSON columns
hold Pydantic-validated documents from :mod:`mapi_spec`; they are never parsed for arrays.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "Catalog",
    "Dataset",
    "DatasetVersion",
    "IdempotencyKey",
    "Model",
    "ModelVersion",
    "Plot",
    "Project",
    "Run",
    "RunAttempt",
    "RunEvent",
    "RunLog",
    "RunProgress",
    "Spec",
    "SpecRevision",
    "Tenant",
    "Upload",
]

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Json = dict[str, Any]
TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        datetime: TS,
    }


def _now() -> Any:
    return func.now()


class _Scoped:
    """``tenant_id`` + ``project_id`` on every row (RLS keys in M2)."""

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)


# ====================================================================== identity


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    trust_tier: Mapped[str] = mapped_column(String(32), default="internal")
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        CheckConstraint(
            "trust_tier in ('internal','verified','public','self-hosted-trusted')", name="tier"
        ),
    )


class Project(Base):
    __tablename__ = "projects"

    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # project_id duplicates id so every table exposes the same (tenant_id, project_id) pair
    project_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (CheckConstraint("project_id = id", name="project_id_is_id"),)


# ====================================================================== datasets


class Dataset(_Scoped, Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    labels: Mapped[dict[str, Any]] = mapped_column(default=dict)
    latest_version: Mapped[int | None] = mapped_column(Integer)
    next_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "name"),)


class Upload(_Scoped, Base):
    """An upload session: a signed PUT into the quarantine bucket."""

    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    bucket: Mapped[str] = mapped_column(String(63))
    object_name: Mapped[str] = mapped_column(String(1024))
    upload: Mapped[dict[str, Any]] = mapped_column()  # SignedPut JSON
    expires_at: Mapped[datetime] = mapped_column()
    consumed_by_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        CheckConstraint("status in ('pending','uploaded','consumed','expired')", name="status"),
    )


class DatasetVersion(_Scoped, Base):
    __tablename__ = "dataset_versions"

    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="ingesting")
    sha256: Mapped[str | None] = mapped_column(String(71))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    data_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    preview: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    objects: Mapped[dict[str, Any]] = mapped_column(default=dict)  # name -> stored object
    ingest_run_id: Mapped[str | None] = mapped_column(String(64))
    upload_id: Mapped[str | None] = mapped_column(String(64))
    mapping: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    issues: Mapped[list[Any]] = mapped_column(default=list)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    ready_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("status in ('ingesting','ready','failed')", name="status"),
        Index("ix_dataset_versions_sha", "tenant_id", "sha256"),
    )


# ========================================================================= specs


class Spec(_Scoped, Base):
    __tablename__ = "specs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    latest_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "name"),)


class SpecRevision(_Scoped, Base):
    __tablename__ = "spec_revisions"

    spec_id: Mapped[str] = mapped_column(
        ForeignKey("specs.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    hash: Mapped[str] = mapped_column(String(71))
    yaml: Mapped[str] = mapped_column(Text)
    document: Mapped[dict[str, Any]] = mapped_column()
    valid: Mapped[bool] = mapped_column(Boolean)
    issues: Mapped[list[Any]] = mapped_column(default=list)
    resolved: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # ResolvedSpec JSON
    catalog_digest: Mapped[str | None] = mapped_column(String(71))
    dataset_id: Mapped[str | None] = mapped_column(String(64))
    dataset_version: Mapped[int | None] = mapped_column(Integer)
    parent_revision: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())


# ========================================================================== runs


class Run(_Scoped, Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    failure_class: Mapped[str | None] = mapped_column(String(32))
    problem: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    spec_id: Mapped[str | None] = mapped_column(String(64))
    spec_revision: Mapped[int | None] = mapped_column(Integer)
    spec_hash: Mapped[str | None] = mapped_column(String(71))
    model_version_id: Mapped[str | None] = mapped_column(String(64))
    dataset_id: Mapped[str | None] = mapped_column(String(64))
    dataset_version: Mapped[int | None] = mapped_column(Integer)
    output_model_version_id: Mapped[str | None] = mapped_column(String(64))
    parent_run_id: Mapped[str | None] = mapped_column(String(64))
    retry_of: Mapped[str | None] = mapped_column(String(64))
    resource_class: Mapped[str] = mapped_column(String(4))
    timeout_s: Mapped[int] = mapped_column(Integer)
    executor: Mapped[str | None] = mapped_column(String(64))
    labels: Mapped[dict[str, Any]] = mapped_column(default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)  # resolved RunSpec.spec
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)  # name -> stored object ref
    catalog_digest: Mapped[str | None] = mapped_column(String(71))
    checkpointable: Mapped[bool] = mapped_column(Boolean, default=False)
    infra_retries: Mapped[int] = mapped_column(Integer, default=0)
    finalize_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_event_at: Mapped[datetime | None] = mapped_column()
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    queued_at: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        Index("ix_runs_scope_id", "tenant_id", "project_id", "id"),
        Index("ix_runs_state", "state", "queued_at"),
        Index("ix_runs_spec", "spec_id"),
    )


class RunAttempt(_Scoped, Base):
    """The intent row ``(run, attempt, nonce)`` written *before* the executor call."""

    __tablename__ = "run_attempts"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    hmac_key: Mapped[str] = mapped_column(String(128))
    executor: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="intent")
    handle: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    external_id: Mapped[str | None] = mapped_column(String(512))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    exec_state: Mapped[str | None] = mapped_column(String(16))
    exec_message: Mapped[str | None] = mapped_column(Text)
    deadline: Mapped[datetime] = mapped_column()
    complete_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    outputs: Mapped[dict[str, Any]] = mapped_column(default=dict)  # verified ObjectMeta map
    violations: Mapped[int] = mapped_column(Integer, default=0)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=-1)
    log_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    submitted_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("status in ('intent','submitted','finished','abandoned')", name="status"),
    )


class RunProgress(_Scoped, Base):
    """The single coalesced progress row per run (upserted, never appended)."""

    __tablename__ = "run_progress"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(BigInteger)
    phase: Mapped[str] = mapped_column(String(32))
    fraction: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(Text)
    eta_seconds: Mapped[float | None] = mapped_column(Float)
    chains: Mapped[list[Any]] = mapped_column(default=list)
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())


class RunEvent(_Scoped, Base):
    """Persisted run events: state changes, warnings, diagnostics, checkpoints, metrics and
    terminal events. ``id`` increases per run and is the SSE event id."""

    __tablename__ = "run_events"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(16), default="runner")
    type: Mapped[str] = mapped_column(String(16))
    ts: Mapped[datetime] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        CheckConstraint("source in ('runner','platform')", name="source"),
        Index(
            "uq_run_events_dedupe",
            "run_id",
            "attempt",
            "seq",
            unique=True,
            postgresql_where=text("source = 'runner'"),
        ),
    )


class RunLog(_Scoped, Base):
    __tablename__ = "run_logs"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column()
    stream: Mapped[str] = mapped_column(String(8))
    line: Mapped[str] = mapped_column(Text)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)


# ======================================================================== models


class Model(_Scoped, Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    latest_version: Mapped[int | None] = mapped_column(Integer)
    next_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "name"),)


class ModelVersion(_Scoped, Base):
    __tablename__ = "model_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[str] = mapped_column(String(64), unique=True)
    attempt: Mapped[int] = mapped_column(Integer)
    spec_hash: Mapped[str] = mapped_column(String(71))
    manifest: Mapped[dict[str, Any]] = mapped_column()  # signed BundleManifest
    capabilities: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    diagnostics: Mapped[list[Any]] = mapped_column(default=list)
    scorecard: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    lineage: Mapped[dict[str, Any]] = mapped_column()
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    artifacts: Mapped[list[Any]] = mapped_column(default=list)
    score_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # ScoreMetadata JSON
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        UniqueConstraint("model_id", "version"),
        CheckConstraint("status in ('ready','partial')", name="status"),
    )


class Plot(_Scoped, Base):
    __tablename__ = "plots"

    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE"), primary_key=True
    )
    plot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(16))
    persona: Mapped[str] = mapped_column(String(16))
    recipe: Mapped[str | None] = mapped_column(String(128))
    caption_plain: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column()  # sanitized PlotSpec
    created_at: Mapped[datetime] = mapped_column(server_default=_now())


# ===================================================================== catalogs


class Catalog(_Scoped, Base):
    """Static catalog JSON (from a describe run or an image-build export) per image digest."""

    __tablename__ = "catalogs"

    image_digest: Mapped[str] = mapped_column(String(71), primary_key=True)
    catalog_digest: Mapped[str] = mapped_column(String(71))
    catalog: Mapped[dict[str, Any]] = mapped_column()
    source: Mapped[str] = mapped_column(String(16))
    describe_run_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (CheckConstraint("source in ('file','describe','api')", name="source"),)


# ================================================================== idempotency


class IdempotencyKey(Base):
    """``Idempotency-Key`` records (24 h). A replay re-renders the stored resource."""

    __tablename__ = "idempotency_keys"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(71))
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    response_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    expires_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
    )
