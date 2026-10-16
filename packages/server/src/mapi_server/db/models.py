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
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "GLOBAL_TABLES",
    "TENANT_TABLES",
    "ApiToken",
    "AuditEvent",
    "Base",
    "Catalog",
    "CostLedgerEntry",
    "Dataset",
    "DatasetVersion",
    "ExecutorReport",
    "IdempotencyKey",
    "LoginTransactionRow",
    "Membership",
    "Model",
    "ModelVersion",
    "PlatformSetting",
    "Plot",
    "Project",
    "QuotaPolicy",
    "RateCounter",
    "Run",
    "RunAttempt",
    "RunEvent",
    "RunLog",
    "RunProgress",
    "Spec",
    "SpecRevision",
    "Tenant",
    "Upload",
    "User",
    "WebSession",
]

TENANT_TABLES: tuple[str, ...] = (
    "projects",
    "memberships",
    "api_tokens",
    "datasets",
    "uploads",
    "dataset_versions",
    "specs",
    "spec_revisions",
    "runs",
    "run_attempts",
    "run_progress",
    "run_events",
    "run_logs",
    "models",
    "model_versions",
    "plots",
    "idempotency_keys",
    "audit_events",
    "cost_ledger",
)
"""Tables with row-level security on ``tenant_id = current_setting('app.tenant_id')``
(``tenants`` itself is isolated on ``id``). New tenant tables must be added here and get
their policy in a migration (``test_rls`` checks every table with a ``tenant_id``)."""

GLOBAL_TABLES: tuple[str, ...] = (
    "users",
    "web_sessions",
    "login_transactions",
    "quota_policies",
    "platform_settings",
    "rate_counters",
    "executor_reports",
    "catalogs",
)
"""Platform tables without a tenant: only the platform role writes them; the request-path
role may read ``catalogs``, ``quota_policies`` and ``platform_settings``."""

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
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    status_reason: Mapped[str | None] = mapped_column(String(500))
    data_residency: Mapped[str] = mapped_column(String(32), default="us", server_default="us")
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint(
            "trust_tier in ('internal','verified','public','self-hosted-trusted')", name="tier"
        ),
        CheckConstraint("status in ('active','suspended','deleting')", name="status"),
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
    # M3 persona lock (0007_guided): non-admins see and write Guided specs only
    guided_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    __table_args__ = (CheckConstraint("project_id = id", name="project_id_is_id"),)


class User(Base):
    """A person. ``(issuer, subject)`` is the identity once they signed in; an *invited*
    user has only an ``email`` until the first sign-in with that verified address claims it.
    Global (no tenant): only the platform role reads it."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer: Mapped[str | None] = mapped_column(String(512))
    subject: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str | None] = mapped_column(String(200))
    operator: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    last_login_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        CheckConstraint("status in ('active','invited','disabled')", name="status"),
        UniqueConstraint("issuer", "subject"),
        Index(
            "uq_users_invited_email",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("issuer IS NULL"),
        ),
    )


class Membership(Base):
    """A role grant; ``project_id`` NULL = tenant-wide. ``email``/``display_name`` are copied
    from the user so tenant-scoped reads never touch the global ``users`` table."""

    __tablename__ = "memberships"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"))
    project_id: Mapped[str | None] = mapped_column(String(64))
    principal_id: Mapped[str] = mapped_column(String(64))
    principal_kind: Mapped[str] = mapped_column(String(32), default="user")
    role: Mapped[str] = mapped_column(String(16))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    created_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("role in ('viewer','editor','runner','admin')", name="role"),
        CheckConstraint("principal_kind in ('user','service_account')", name="principal_kind"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_memberships_grant",
            "tenant_id",
            text("coalesce(project_id, '')"),
            "principal_id",
            unique=True,
        ),
        Index("ix_memberships_principal", "principal_id"),
    )


class ApiToken(Base):
    """An API token. Only ``secret_hash`` (HMAC-SHA256 under the server pepper) is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[str] = mapped_column(String(64))
    owner_kind: Mapped[str] = mapped_column(String(32), default="user")
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(8), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(80))
    scopes: Mapped[list[Any]] = mapped_column()
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    vcpu_seconds_budget: Mapped[float | None] = mapped_column(Float)
    budget_period: Mapped[str] = mapped_column(String(8), default="month")
    created_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    last_used_at: Mapped[datetime | None] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("budget_period in ('day','month')", name="budget_period"),
        Index("ix_api_tokens_owner", "tenant_id", "project_id", "owner_id"),
    )


class WebSession(Base):
    """A browser session (BFF). ``id`` is the SHA-256 of the cookie value, never the value."""

    __tablename__ = "web_sessions"

    id: Mapped[str] = mapped_column(String(71), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column()
    last_seen_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (Index("ix_web_sessions_user", "user_id"),)


class LoginTransactionRow(Base):
    """A pending OIDC sign-in between ``/auth/login`` and ``/auth/callback`` (single use).
    ``id`` is the SHA-256 of the login cookie value."""

    __tablename__ = "login_transactions"

    id: Mapped[str] = mapped_column(String(71), primary_key=True)
    state: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[str] = mapped_column(String(128))
    code_verifier: Mapped[str] = mapped_column(String(128))
    return_to: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column()


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
    # columns.json of the ingest run (0007_guided): ColumnProfile per uploaded column
    columns: Mapped[list[Any] | None] = mapped_column(JSONB)

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
    finalize: Mapped[dict[str, Any]] = mapped_column(default=dict)  # finalizer reservations
    last_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_event_at: Mapped[datetime | None] = mapped_column()
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column()
    priority: Mapped[str] = mapped_column(String(16), default="batch", server_default="batch")
    task_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    token_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(64))
    untrusted_code: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    estimate: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # CostEstimate at submit
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    queued_at: Mapped[datetime | None] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        Index("ix_runs_scope_id", "tenant_id", "project_id", "id"),
        Index("ix_runs_state", "state", "queued_at"),
        Index("ix_runs_spec", "spec_id"),
        Index("ix_runs_tenant_state", "tenant_id", "state"),
        CheckConstraint("priority in ('interactive','batch')", name="priority"),
        CheckConstraint("task_count >= 1", name="task_count"),
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


# ===================================================================== accounting (M2)


class AuditEvent(Base):
    """Append-only audit rows (:class:`mapi_spec.audit.AuditEvent`). ``tenant_id`` NULL =
    a platform-level action (visible to operators only)."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[datetime] = mapped_column()
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(String(64))
    actor_kind: Mapped[str | None] = mapped_column(String(32))
    actor_id: Mapped[str | None] = mapped_column(String(128))
    token_id: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[bool] = mapped_column(Boolean, default=False)
    action: Mapped[str] = mapped_column(String(64))
    resource_kind: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(256))
    outcome: Mapped[str] = mapped_column(String(16))
    code: Mapped[str | None] = mapped_column(String(64))
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (
        Index("ix_audit_events_tenant_ts", "tenant_id", "ts"),
        Index("ix_audit_events_ts", "ts"),
    )


class CostLedgerEntry(Base):
    """Append-only cost ledger (:class:`mapi_spec.cost.CostLedgerEntry`): one ``actual`` row
    per metered attempt (unique), plus operator ``adjustment`` rows."""

    __tablename__ = "cost_ledger"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    project_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="actual")
    resource_class: Mapped[str] = mapped_column(String(4))
    executor: Mapped[str | None] = mapped_column(String(64))
    token_id: Mapped[str | None] = mapped_column(String(64))
    task_count: Mapped[int] = mapped_column(Integer, default=1)
    wall_seconds: Mapped[float] = mapped_column(Float)
    vcpu_seconds: Mapped[float] = mapped_column(Float)
    gib_seconds: Mapped[float] = mapped_column(Float)
    price_version: Mapped[str] = mapped_column(String(64))
    amount_micros: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    recorded_at: Mapped[datetime] = mapped_column()
    note: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint("kind in ('actual','adjustment')", name="kind"),
        Index(
            "uq_cost_ledger_actual",
            "run_id",
            "attempt",
            unique=True,
            postgresql_where=text("kind = 'actual'"),
        ),
        Index("ix_cost_ledger_tenant_time", "tenant_id", "recorded_at"),
        Index("ix_cost_ledger_project_time", "tenant_id", "project_id", "recorded_at"),
        Index("ix_cost_ledger_token_time", "token_id", "recorded_at"),
        Index("ix_cost_ledger_time", "recorded_at"),
    )


class QuotaPolicy(Base):
    """Operator-set quota policies (``scope``, ``subject_id``); tier defaults apply when no
    tenant row exists. Platform table: read-only for the request-path role."""

    __tablename__ = "quota_policies"

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    limits: Mapped[dict[str, Any]] = mapped_column()
    note: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column()
    updated_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (CheckConstraint("scope in ('token','tenant','global')", name="scope"),)


class PlatformSetting(Base):
    """Platform-wide documents (``kill_switches``). Read-only for the request-path role."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()


class RateCounter(Base):
    """Fixed-window request counters (``subject`` = ``token:<id>``, ``tenant:<id>``, ``*``)."""

    __tablename__ = "rate_counters"

    subject: Mapped[str] = mapped_column(String(96), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class ExecutorReport(Base):
    """What each orchestrator last reported about its executor (admin console)."""

    __tablename__ = "executor_reports"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    caps: Mapped[dict[str, Any]] = mapped_column()
    submits_last_minute: Mapped[int] = mapped_column(Integer, default=0)
    submit_rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    throttled_last_hour: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None] = mapped_column()
    reported_by: Mapped[str | None] = mapped_column(String(128))
    checked_at: Mapped[datetime] = mapped_column()
    message: Mapped[str | None] = mapped_column(String(500))
    disabled_reason: Mapped[str | None] = mapped_column(String(500))
    """Why an operator disabled it (``enabled`` is the operator's, never the report's)."""
