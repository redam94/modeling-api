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
    "Endpoint",
    "EndpointRevision",
    "EndpointRun",
    "ExecutorReport",
    "GovernancePolicyRow",
    "IdempotencyKey",
    "LoginTransactionRow",
    "Membership",
    "Model",
    "ModelValidation",
    "ModelVersion",
    "PlatformSetting",
    "Plot",
    "PlotEmbed",
    "Project",
    "QuotaPolicy",
    "RateCounter",
    "Run",
    "RunAttempt",
    "RunEvent",
    "RunLog",
    "RunProgress",
    "SavedPlot",
    "Signup",
    "Spec",
    "SpecRevision",
    "Tenant",
    "TenantDeletion",
    "TenantExport",
    "TenantVerification",
    "Upload",
    "User",
    "ValidationApproval",
    "WebSession",
    "Webhook",
    "WebhookDelivery",
    "WebhookEvent",
    "Workflow",
    "WorkflowEventRow",
    "WorkflowNodeRunRow",
    "WorkflowRevisionRow",
    "WorkflowRunRow",
    "WorkflowScheduleRow",
    "WorkflowTriggerRow",
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
    "endpoints",
    "endpoint_revisions",
    "endpoint_runs",
    "webhooks",
    "webhook_events",
    "webhook_deliveries",
    "saved_plots",
    "plot_embeds",
    # M3 trust lifecycle (0010_trust_lifecycle)
    "tenant_verifications",
    "tenant_deletions",
    "tenant_exports",
    # M3.5 workflows (0011_workflows)
    "workflows",
    "workflow_revisions",
    "workflow_runs",
    "workflow_node_runs",
    "workflow_events",
    "workflow_schedules",
    "workflow_triggers",
    # M3.5 governance (0012_governance)
    "governance_policies",
    "model_validations",
    "validation_approvals",
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
    "signups",
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
    capabilities: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'"))
    """M3.5: grants on top of the role (``reviewer``), see :class:`mapi_spec.tenancy.Capability`."""
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


class SavedPlot(_Scoped, Base):
    """A saved plot (``POST /v1/plots``, M3): a model version's plot or a user-authored spec.

    ``spec`` is the sanitized PlotSpec at save time (sanitized again on every read). A plot
    saved from a model version keeps ``model_version_id``: its ``data_ref`` datasets are
    bundle objects of that version, verified against the signed manifest when used.
    """

    __tablename__ = "saved_plots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    source_kind: Mapped[str] = mapped_column(String(16))
    model_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE")
    )
    source: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # ModelPlotRef JSON
    recipe: Mapped[str | None] = mapped_column(String(128))
    provenance: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict[str, Any]] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("source_kind in ('model_plot','spec')", name="source_kind"),
        CheckConstraint("provenance in ('recipe','plugin','author')", name="provenance"),
        CheckConstraint("kind in ('vega-lite','plotly')", name="kind"),
        Index("ix_saved_plots_project", "tenant_id", "project_id", "id"),
    )


class PlotEmbed(_Scoped, Base):
    """A tokenised read-only link to a saved plot. Only ``token_hash`` (HMAC-SHA256 under the
    server pepper) is stored; ``image`` is the static PNG fallback once copied out of the
    render run's scratch (``{bucket, name, generation}``)."""

    __tablename__ = "plot_embeds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plot_id: Mapped[str] = mapped_column(ForeignKey("saved_plots.id", ondelete="CASCADE"))
    label: Mapped[str | None] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(80), unique=True)
    display: Mapped[str] = mapped_column(String(16))
    allowed_origins: Mapped[list[Any]] = mapped_column(default=list)
    views: Mapped[int] = mapped_column(BigInteger, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column()
    render_run_id: Mapped[str | None] = mapped_column(String(64))
    image: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        Index("ix_plot_embeds_plot", "tenant_id", "project_id", "plot_id"),
    )


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


# ======================================================================= endpoints (M3)


class Endpoint(_Scoped, Base):
    """A stable alias (``endpoint://project/alias``) for the model version it serves now.

    ``revision`` increases with every promotion and rollback (:class:`EndpointRevision`);
    ``schema_snapshot`` is the promoted version's input contract (what callers send) and
    ``gate`` the promotion gate's decision for it (:mod:`mapi_spec.endpoints`)."""

    __tablename__ = "endpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    alias: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(String(64))
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    revision: Mapped[int] = mapped_column(Integer)
    evaluation_policy: Mapped[dict[str, Any]] = mapped_column()
    schema_snapshot: Mapped[dict[str, Any]] = mapped_column()
    gate: Mapped[dict[str, Any]] = mapped_column()
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    promoted_by: Mapped[str | None] = mapped_column(String(128))
    promoted_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "project_id", "alias"),
        CheckConstraint("revision >= 1", name="revision"),
    )


class EndpointRevision(_Scoped, Base):
    """One promotion or rollback of an endpoint (append-only for the request-path role)."""

    __tablename__ = "endpoint_revisions"

    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))
    model_version_id: Mapped[str] = mapped_column(String(64))
    model_ref: Mapped[str] = mapped_column(String(300))
    previous_model_version_id: Mapped[str | None] = mapped_column(String(64))
    gate: Mapped[dict[str, Any]] = mapped_column()
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(1000))
    actor_id: Mapped[str | None] = mapped_column(String(128))
    token_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column()

    __table_args__ = (CheckConstraint("action in ('promote','rollback')", name="action"),)


class EndpointRun(_Scoped, Base):
    """Which endpoint (and revision) a score run was submitted through (webhooks, usage)."""

    __tablename__ = "endpoint_runs"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(String(64))
    alias: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (Index("ix_endpoint_runs_endpoint", "endpoint_id", "created_at"),)


# ======================================================================== webhooks (M3)


class Webhook(_Scoped, Base):
    """An outgoing webhook. The signing secret is stored sealed (AES-GCM under a key derived
    from the server secret, bound to the webhook id) and never returned after creation."""

    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    events: Mapped[list[Any]] = mapped_column()
    description: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    secret_sealed: Mapped[str] = mapped_column(Text)
    secret_hint: Mapped[str] = mapped_column(String(16))
    previous_secret_sealed: Mapped[str | None] = mapped_column(Text)
    previous_secret_expires_at: Mapped[datetime | None] = mapped_column()
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(String(500))
    last_delivery_at: Mapped[datetime | None] = mapped_column()
    last_success_at: Mapped[datetime | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        Index("ix_webhooks_scope", "tenant_id", "project_id", "active"),
        CheckConstraint("failure_count >= 0", name="failure_count"),
    )


class WebhookEvent(Base):
    """The transactional outbox: one event, written in the transaction that caused it.
    ``envelope`` is the :class:`mapi_spec.webhooks.WebhookEnvelope` JSON every delivery of it
    sends. ``project_id`` is NULL for tenant-wide events."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"))
    project_id: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[dict[str, Any]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column()

    __table_args__ = (Index("ix_webhook_events_tenant_time", "tenant_id", "created_at"),)


class WebhookDelivery(_Scoped, Base):
    """One event's delivery chain to one webhook (``attempt`` is the latest or next try).

    ``pending``/``retrying`` rows are due at ``next_attempt_at``; ``lease_until`` marks a try
    in flight (the relay skips it until the lease runs out)."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhooks.id", ondelete="CASCADE"))
    event_id: Mapped[str] = mapped_column(ForeignKey("webhook_events.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    response_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(500))
    response_excerpt: Mapped[str | None] = mapped_column(String(1024))
    next_attempt_at: Mapped[datetime | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column()
    replay_of: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','succeeded','retrying','failed','blocked')", name="status"
        ),
        CheckConstraint("attempt >= 1", name="attempt"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_webhook", "webhook_id", "created_at"),
    )


# ================================================================= trust lifecycle (M3)


class Signup(Base):
    """A self-service sign-up (``POST /auth/signup``) and its e-mail verification.

    Global: the tenant does not exist until the address is verified. ``token_hash`` is the
    keyed digest of the single-use verification token (never the token); ``ip_hash`` and
    ``device_hash`` are keyed digests of the client address and of the browser's device
    cookie, used by the rate limits and the Sybil heuristics. The API reads and writes it
    as the gateway role; operators review it (``GET /v1/admin/signups``).
    """

    __tablename__ = "signups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    email_domain: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(200))
    organisation: Mapped[str | None] = mapped_column(String(200))
    terms_version: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    review: Mapped[str] = mapped_column(String(16), default="none", server_default="none")
    flags: Mapped[list[Any]] = mapped_column(default=list)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    device_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column()
    verified_at: Mapped[datetime | None] = mapped_column()
    user_id: Mapped[str | None] = mapped_column(String(64))
    created_tenant_id: Mapped[str | None] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column()
    review_reason: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint("state in ('pending','verified','expired')", name="state"),
        CheckConstraint("review in ('none','flagged','approved','rejected')", name="review"),
        Index("ix_signups_ip", "ip_hash", "created_at"),
        Index("ix_signups_device", "device_hash", "created_at"),
        Index("ix_signups_domain", "email_domain", "created_at"),
        Index("ix_signups_email", text("lower(email)")),
        Index("ix_signups_review", "review", "created_at"),
        Index("ix_signups_tenant", "created_tenant_id"),
    )


class TenantVerification(Base):
    """E-mail and identity verification of a tenant (plan §9 "Compute abuse"). The
    request-path role may only read it; operators change it through the gateway role."""

    __tablename__ = "tenant_verifications"

    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified_at: Mapped[datetime | None] = mapped_column()
    identity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    method: Mapped[str] = mapped_column(String(16), default="none")
    verified_at: Mapped[datetime | None] = mapped_column()
    verified_by: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(String(500))
    provider_reference: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        CheckConstraint("method in ('none','email','card','document','manual')", name="method"),
    )


class TenantDeletion(Base):
    """A tenant deletion: grace period, purge progress per store, objects a retention lock
    still holds. Kept after the purge (it is the deletion's own, content-free record)."""

    __tablename__ = "tenant_deletions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"))
    state: Mapped[str] = mapped_column(String(16))
    previous_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(1000))
    requested_by: Mapped[str | None] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column()
    purge_after: Mapped[datetime] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    cancelled_at: Mapped[datetime | None] = mapped_column()
    stores: Mapped[list[Any]] = mapped_column(default=list)
    retained_objects: Mapped[list[Any]] = mapped_column(default=list)
    notify: Mapped[list[Any]] = mapped_column(default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint(
            "state in ('scheduled','cancelled','deleting','purged','failed')", name="state"
        ),
        Index(
            "uq_tenant_deletions_open",
            "tenant_id",
            unique=True,
            postgresql_where=text("state in ('scheduled','deleting','failed')"),
        ),
        Index("ix_tenant_deletions_due", "state", "next_attempt_at"),
    )


class TenantExport(Base):
    """A GDPR export job and the archive it produced (``t/{tenant}/exports/`` in the data
    bucket, deleted when it expires)."""

    __tablename__ = "tenant_exports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(16), default="queued")
    include: Mapped[list[Any]] = mapped_column(default=list)
    requested_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column()
    started_at: Mapped[datetime | None] = mapped_column()
    ready_at: Mapped[datetime | None] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(71))
    bucket: Mapped[str | None] = mapped_column(String(63))
    object_name: Mapped[str | None] = mapped_column(String(1024))
    error: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint("state in ('queued','running','ready','expired','failed')", name="state"),
        Index("ix_tenant_exports_tenant", "tenant_id", "created_at"),
        Index("ix_tenant_exports_state", "state"),
    )


# ============================================================================ workflows (M3.5)


def _scope_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"], ondelete="CASCADE"
    )


class Workflow(_Scoped, Base):
    """A saved workflow (``workflow://project/name``); documents are its revisions."""

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    latest_revision: Mapped[int] = mapped_column(Integer, default=1)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    template: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        _scope_fk(),
        UniqueConstraint("tenant_id", "project_id", "name"),
        CheckConstraint("latest_revision >= 1", name="latest_revision"),
    )


class WorkflowRevisionRow(_Scoped, Base):
    """An immutable, hashed workflow document and its static validation at save time."""

    __tablename__ = "workflow_revisions"

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column()
    spec_hash: Mapped[str] = mapped_column(String(71))
    validation: Mapped[dict[str, Any]] = mapped_column()
    valid: Mapped[bool] = mapped_column(Boolean)
    message: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())


class WorkflowRunRow(_Scoped, Base):
    """The orchestration state of a ``workflow`` run (its ``runs`` row is the parent run)."""

    __tablename__ = "workflow_runs"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    trigger_id: Mapped[str | None] = mapped_column(String(64))
    parent_run_id: Mapped[str | None] = mapped_column(String(64))
    parent_node_id: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    input_refs: Mapped[dict[str, Any]] = mapped_column(default=dict)  # name -> NodeOutputRef
    input_identities: Mapped[dict[str, Any]] = mapped_column(default=dict)
    decisions: Mapped[list[Any]] = mapped_column(default=list)
    outputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    use_cache: Mapped[bool] = mapped_column(Boolean, default=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    resource_class: Mapped[str | None] = mapped_column(String(4))
    seed: Mapped[int] = mapped_column(BigInteger, default=0)
    last_event_id: Mapped[int] = mapped_column(BigInteger, default=0)
    deadline: Mapped[datetime | None] = mapped_column()
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelling','cancelled')",
            name="status",
        ),
        Index("ix_workflow_runs_workflow", "workflow_id", "created_at"),
        Index("ix_workflow_runs_active", "status"),
    )


class WorkflowNodeRunRow(_Scoped, Base):
    """One node of a workflow run (a :class:`mapi_spec.workflow.NodeRun`)."""

    __tablename__ = "workflow_node_runs"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    use: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    child_run_id: Mapped[str | None] = mapped_column(String(64))
    child_runs: Mapped[list[Any]] = mapped_column(default=list)
    phase: Mapped[str | None] = mapped_column(String(32))
    cache_key: Mapped[str | None] = mapped_column(String(71))
    cached_from: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    outputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    skipped_reason: Mapped[str | None] = mapped_column(String(500))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column()
    finished_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        Index("ix_workflow_node_runs_cache", "tenant_id", "project_id", "cache_key"),
        Index("ix_workflow_node_runs_child", "child_run_id"),
    )


class WorkflowEventRow(_Scoped, Base):
    """A workflow run event (``id`` increases per run: the SSE event id)."""

    __tablename__ = "workflow_events"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(48))
    node_id: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column()
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)


class WorkflowScheduleRow(_Scoped, Base):
    """A cron schedule of a workflow (fired by the orchestrator's workflow loop)."""

    __tablename__ = "workflow_schedules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    cron: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(String(500))
    next_run_at: Mapped[datetime | None] = mapped_column()
    last_run_id: Mapped[str | None] = mapped_column(String(64))
    last_run_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (Index("ix_workflow_schedules_due", "active", "next_run_at"),)


class WorkflowTriggerRow(_Scoped, Base):
    """Run a workflow when a dataset gets a new version (debounced)."""

    __tablename__ = "workflow_triggers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    event: Mapped[str] = mapped_column(String(48))
    dataset: Mapped[str] = mapped_column(String(300))
    dataset_name: Mapped[str] = mapped_column(String(128))
    input: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    debounce_seconds: Mapped[int] = mapped_column(Integer, default=300)
    pending_version: Mapped[int | None] = mapped_column(Integer)
    pending_since: Mapped[datetime | None] = mapped_column()
    last_fired_at: Mapped[datetime | None] = mapped_column()
    last_run_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        Index("ix_workflow_triggers_dataset", "tenant_id", "project_id", "dataset_name"),
        Index("ix_workflow_triggers_pending", "pending_since"),
    )


# ============================================================================ governance (M3.5)


class GovernancePolicyRow(Base):
    """One version of a governance policy (:class:`mapi_spec.governance.GovernancePolicy`).

    ``(id, version)`` is the key: an update inserts the next version and the old ones stay
    for audit; ``retired_at`` on the latest version retires the policy. ``project_id`` is
    NULL for tenant-wide policies (every project's floor)."""

    __tablename__ = "governance_policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"))
    project_id: Mapped[str | None] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    applies_to: Mapped[dict[str, Any]] = mapped_column(default=dict)
    checks: Mapped[list[Any]] = mapped_column(default=list)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approvals_required: Mapped[int] = mapped_column(Integer, default=1)
    require_validated_for_promotion: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    retired_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    created_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        CheckConstraint("scope in ('tenant','project')", name="scope"),
        CheckConstraint("(scope = 'tenant') = (project_id IS NULL)", name="scope_project"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("approvals_required between 1 and 3", name="approvals_required"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        Index("ix_governance_policies_scope", "tenant_id", "project_id"),
    )


class ModelValidation(_Scoped, Base):
    """A validation of one model version (:class:`mapi_spec.governance.ModelValidation`):
    status, the policies and checks it was held to, results, history. ``round`` counts
    submissions (approvals belong to the current round)."""

    __tablename__ = "model_validations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(64))
    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_versions.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    round: Mapped[int] = mapped_column(Integer, default=1)
    policies: Mapped[list[Any]] = mapped_column(default=list)
    required_checks: Mapped[list[Any]] = mapped_column(default=list)
    results: Mapped[list[Any]] = mapped_column(default=list)
    failing_checks: Mapped[list[Any]] = mapped_column(default=list)
    require_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    approvals_required: Mapped[int] = mapped_column(Integer, default=1)
    history: Mapped[list[Any]] = mapped_column(default=list)
    run_id: Mapped[str | None] = mapped_column(String(64))
    dataset: Mapped[str | None] = mapped_column(String(512))
    note: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[str | None] = mapped_column(String(128))
    submitted_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(server_default=_now())

    __table_args__ = (
        CheckConstraint(
            "status in ('draft','checks_running','checks_passed','checks_failed',"
            "'awaiting_approval','validated','rejected','deprecated')",
            name="status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="CASCADE",
        ),
        Index("ix_model_validations_version", "model_version_id", "created_at"),
        Index("ix_model_validations_status", "tenant_id", "project_id", "status"),
        Index("ix_model_validations_run", "run_id"),
    )


class ValidationApproval(_Scoped, Base):
    """A reviewer's decision on one round of a validation (append-only for requests)."""

    __tablename__ = "validation_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    validation_id: Mapped[str] = mapped_column(
        ForeignKey("model_validations.id", ondelete="CASCADE")
    )
    round: Mapped[int] = mapped_column(Integer)
    approver_id: Mapped[str] = mapped_column(String(128))
    decision: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        CheckConstraint("decision in ('approve','reject')", name="decision"),
        UniqueConstraint("validation_id", "round", "approver_id"),
        Index("ix_validation_approvals_validation", "validation_id", "round"),
    )
