"""One builder per endpoint of ``mapi_spec.api.ENDPOINTS`` (operation ids in comments).

Builders are pure: they validate arguments, encode paths and return a :class:`Call`.
``tests/test_endpoint_coverage.py`` checks that every endpoint a program may call has a
builder whose method, path, statuses and ``scoped`` flag match the contract (the browser
sign-in routes under ``/auth`` and ``POST /internal/events`` are not for programs).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

import httpx
from mapi_spec.api import (
    API_PREFIX,
    MAX_PREFER_WAIT_SECONDS,
    PAGE_LIMIT_MAX,
    PREFER_HEADER,
    AdminUsageReport,
    ApiToken,
    ArtifactLink,
    AuditEvent,
    CatalogResponse,
    Dataset,
    DatasetCreate,
    DatasetVersion,
    DatasetVersionCreate,
    ExecutorEvidence,
    ExecutorEvidenceUpdate,
    ExecutorHealth,
    ExecutorUpdate,
    Health,
    KillSwitches,
    KillSwitchUpdate,
    LogLine,
    Me,
    Membership,
    MembershipCreate,
    MembershipUpdate,
    Model,
    ModelVersion,
    Page,
    PlotListItem,
    PlotSpec,
    PlotValidateResponse,
    Project,
    ProjectCreate,
    ProjectUpdate,
    QuotaPolicy,
    QuotaPolicyUpsert,
    Run,
    RunCreate,
    RunEventView,
    RunRetry,
    ScoreCall,
    ScoreCallResult,
    SpecCreate,
    SpecDoc,
    SpecRevision,
    SpecRevisionCreate,
    Tenant,
    TenantAdminUpdate,
    TenantCreate,
    TenantUpdate,
    TokenCreate,
    TokenCreated,
    UploadCreate,
    UploadSession,
    UsageReport,
    ValidateRequest,
    ValidateResponse,
)
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.executor import RunKind
from mapi_spec.quotas import QuotaScope
from mapi_spec.run import RunState
from mapi_spec.tenancy import TenantStatus, TrustTier
from mapi_spec.tokens import TokenStatus

from mapi_client._http import (
    Call,
    artifact_path,
    dump,
    idempotency_headers,
    parse_json,
    parse_model,
    segment,
)

V1: Final = API_PREFIX

_OK: Final = frozenset({200})
_CREATED: Final = frozenset({201})
# an idempotent replay of a create returns the original resource with 200
_CREATED_OR_REPLAY: Final = frozenset({200, 201})
_ACCEPTED: Final = frozenset({202})
_ACCEPTED_OR_REPLAY: Final = frozenset({200, 202})
_NO_CONTENT: Final = frozenset({204})

USAGE_PERIODS: Final = ("day", "month")
USAGE_GROUPS: Final = ("resource_class", "project", "day", "token", "run")


def parse_none(response: httpx.Response) -> None:
    """A ``204 No Content`` answer (the body, if any, is ignored)."""
    return None


def page_params(
    limit: int | None = None, cursor: str | None = None, **filters: str | int | None
) -> dict[str, str | int]:
    """Query parameters of a list endpoint (``None`` values are dropped)."""
    params: dict[str, str | int] = {}
    if limit is not None:
        if not 1 <= limit <= PAGE_LIMIT_MAX:
            raise ValueError(f"limit must be between 1 and {PAGE_LIMIT_MAX}")
        params["limit"] = limit
    if cursor is not None:
        params["cursor"] = cursor
    for key, value in filters.items():
        if value is not None:
            params[key] = value
    return params


# ---------------------------------------------------------------------------------- health


def health() -> Call[Health]:  # health
    return Call("GET", "/healthz", parse_model(Health), _OK, scoped=False)


def ready() -> Call[Health]:  # ready (503 carries the same body)
    return Call("GET", "/readyz", parse_model(Health), frozenset({200, 503}), scoped=False)


# -------------------------------------------------------------------------------- projects


def list_projects(limit: int | None, cursor: str | None) -> Call[Page[Project]]:
    return Call(
        "GET",
        f"{V1}/projects",
        parse_model(Page[Project]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def get_project(project_id: str) -> Call[Project]:
    return Call("GET", f"{V1}/projects/{segment(project_id)}", parse_model(Project), scoped=False)


# --------------------------------------------------------------------------------- catalog


def get_catalog() -> Call[CatalogResponse]:
    return Call("GET", f"{V1}/catalog", parse_model(CatalogResponse), scoped=False)


def list_catalog_models(limit: int | None, cursor: str | None) -> Call[Page[ModelCatalogEntry]]:
    return Call(
        "GET",
        f"{V1}/catalog/models",
        parse_model(Page[ModelCatalogEntry]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def get_catalog_model(model_id: str, version: str) -> Call[ModelCatalogEntry]:
    path = f"{V1}/catalog/models/{segment(model_id)}/{segment(version)}"
    return Call("GET", path, parse_model(ModelCatalogEntry), scoped=False)


def list_catalog_steps(limit: int | None, cursor: str | None) -> Call[Page[StepCatalogEntry]]:
    return Call(
        "GET",
        f"{V1}/catalog/steps",
        parse_model(Page[StepCatalogEntry]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def list_catalog_plots(limit: int | None, cursor: str | None) -> Call[Page[PlotCatalogEntry]]:
    return Call(
        "GET",
        f"{V1}/catalog/plots",
        parse_model(Page[PlotCatalogEntry]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def get_schema(name: str) -> Call[dict[str, Any]]:
    return Call("GET", f"{V1}/schemas/{segment(name)}", parse_json, scoped=False)


# -------------------------------------------------------------------------------- datasets


def create_dataset(body: DatasetCreate) -> Call[Dataset]:
    return Call("POST", f"{V1}/datasets", parse_model(Dataset), _CREATED, json=dump(body))


def list_datasets(limit: int | None, cursor: str | None) -> Call[Page[Dataset]]:
    return Call(
        "GET", f"{V1}/datasets", parse_model(Page[Dataset]), params=page_params(limit, cursor)
    )


def get_dataset(dataset_id: str) -> Call[Dataset]:
    return Call("GET", f"{V1}/datasets/{segment(dataset_id)}", parse_model(Dataset))


def create_upload(dataset_id: str, body: UploadCreate) -> Call[UploadSession]:
    path = f"{V1}/datasets/{segment(dataset_id)}/uploads"
    return Call("POST", path, parse_model(UploadSession), _CREATED, json=dump(body))


def create_dataset_version(
    dataset_id: str, body: DatasetVersionCreate, idempotency_key: str | None
) -> Call[DatasetVersion]:
    path = f"{V1}/datasets/{segment(dataset_id)}/versions"
    return Call(
        "POST",
        path,
        parse_model(DatasetVersion),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def list_dataset_versions(
    dataset_id: str, limit: int | None, cursor: str | None
) -> Call[Page[DatasetVersion]]:
    path = f"{V1}/datasets/{segment(dataset_id)}/versions"
    return Call("GET", path, parse_model(Page[DatasetVersion]), params=page_params(limit, cursor))


def get_dataset_version(dataset_id: str, version: int) -> Call[DatasetVersion]:
    path = f"{V1}/datasets/{segment(dataset_id)}/versions/{segment(version)}"
    return Call("GET", path, parse_model(DatasetVersion))


def get_dataset_artifact(dataset_id: str, version: int, name: str) -> Call[ArtifactLink]:
    path = (
        f"{V1}/datasets/{segment(dataset_id)}/versions/{segment(version)}"
        f"/artifacts/{artifact_path(name)}"
    )
    return Call("GET", path, parse_model(ArtifactLink))


# ----------------------------------------------------------------------------------- specs


def validate_spec(body: ValidateRequest) -> Call[ValidateResponse]:
    return Call("POST", f"{V1}/specs:validate", parse_model(ValidateResponse), json=dump(body))


def create_spec(body: SpecCreate) -> Call[SpecDoc]:
    return Call("POST", f"{V1}/specs", parse_model(SpecDoc), _CREATED, json=dump(body))


def list_specs(limit: int | None, cursor: str | None) -> Call[Page[SpecDoc]]:
    return Call("GET", f"{V1}/specs", parse_model(Page[SpecDoc]), params=page_params(limit, cursor))


def get_spec(spec_id: str) -> Call[SpecDoc]:
    return Call("GET", f"{V1}/specs/{segment(spec_id)}", parse_model(SpecDoc))


def create_spec_revision(spec_id: str, body: SpecRevisionCreate) -> Call[SpecRevision]:
    path = f"{V1}/specs/{segment(spec_id)}/revisions"
    return Call("POST", path, parse_model(SpecRevision), _CREATED, json=dump(body))


def list_spec_revisions(
    spec_id: str, limit: int | None, cursor: str | None
) -> Call[Page[SpecRevision]]:
    path = f"{V1}/specs/{segment(spec_id)}/revisions"
    return Call("GET", path, parse_model(Page[SpecRevision]), params=page_params(limit, cursor))


def get_spec_revision(spec_id: str, revision: int) -> Call[SpecRevision]:
    path = f"{V1}/specs/{segment(spec_id)}/revisions/{segment(revision)}"
    return Call("GET", path, parse_model(SpecRevision))


def dry_run_spec(spec_id: str, revision: int, idempotency_key: str | None) -> Call[Run]:
    path = f"{V1}/specs/{segment(spec_id)}/revisions/{segment(revision)}:dry-run"
    return Call(
        "POST",
        path,
        parse_model(Run),
        _ACCEPTED_OR_REPLAY,
        headers=idempotency_headers(idempotency_key),
    )


# ------------------------------------------------------------------------------------ runs


def create_run(body: RunCreate, idempotency_key: str | None) -> Call[Run]:
    return Call(
        "POST",
        f"{V1}/runs",
        parse_model(Run),
        _CREATED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def list_runs(
    limit: int | None,
    cursor: str | None,
    *,
    kind: RunKind | str | None = None,
    state: RunState | str | None = None,
    spec_id: str | None = None,
) -> Call[Page[Run]]:
    params = page_params(
        limit,
        cursor,
        kind=RunKind(kind).value if kind is not None else None,
        state=RunState(state).value if state is not None else None,
        spec_id=spec_id,
    )
    return Call("GET", f"{V1}/runs", parse_model(Page[Run]), params=params)


def get_run(run_id: str) -> Call[Run]:
    return Call("GET", f"{V1}/runs/{segment(run_id)}", parse_model(Run))


def run_events_path(run_id: str) -> str:
    return f"{V1}/runs/{segment(run_id)}/events"


def list_run_events(run_id: str, after: int | None, limit: int | None) -> Call[Page[RunEventView]]:
    params: dict[str, str | int] = page_params(limit)
    if after is not None:
        if after < 0:
            raise ValueError("after must be >= 0")
        params["after"] = after
    return Call(
        "GET",
        run_events_path(run_id),
        parse_model(Page[RunEventView]),
        params=params,
        headers={"Accept": "application/json"},
    )


def run_logs(
    run_id: str,
    limit: int | None,
    cursor: str | None,
    *,
    attempt: int | None = None,
    stream: str | None = None,
) -> Call[Page[LogLine]]:
    if stream is not None and stream not in ("stdout", "stderr", "system"):
        raise ValueError("stream must be one of stdout, stderr, system")
    params = page_params(limit, cursor, attempt=attempt, stream=stream)
    return Call(
        "GET", f"{V1}/runs/{segment(run_id)}/logs", parse_model(Page[LogLine]), params=params
    )


def list_run_plots(run_id: str, limit: int | None, cursor: str | None) -> Call[Page[PlotSpec]]:
    path = f"{V1}/runs/{segment(run_id)}/plots"
    return Call("GET", path, parse_model(Page[PlotSpec]), params=page_params(limit, cursor))


def cancel_run(run_id: str) -> Call[Run]:
    # idempotent by definition (cancelling twice returns the same run): safe to resend
    return Call(
        "POST",
        f"{V1}/runs/{segment(run_id)}:cancel",
        parse_model(Run),
        _ACCEPTED_OR_REPLAY,
        retry_safe=True,
    )


def resume_run(run_id: str) -> Call[Run]:
    return Call("POST", f"{V1}/runs/{segment(run_id)}:resume", parse_model(Run), _ACCEPTED)


def retry_run(run_id: str, body: RunRetry, idempotency_key: str | None) -> Call[Run]:
    return Call(
        "POST",
        f"{V1}/runs/{segment(run_id)}:retry",
        parse_model(Run),
        _CREATED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


# ---------------------------------------------------------------------------------- models


def list_models(limit: int | None, cursor: str | None) -> Call[Page[Model]]:
    return Call("GET", f"{V1}/models", parse_model(Page[Model]), params=page_params(limit, cursor))


def get_model(model_id: str) -> Call[Model]:
    return Call("GET", f"{V1}/models/{segment(model_id)}", parse_model(Model))


def list_model_versions(
    model_id: str, limit: int | None, cursor: str | None
) -> Call[Page[ModelVersion]]:
    path = f"{V1}/models/{segment(model_id)}/versions"
    return Call("GET", path, parse_model(Page[ModelVersion]), params=page_params(limit, cursor))


def _mv(model_id: str, version: int) -> str:
    return f"{V1}/models/{segment(model_id)}/versions/{segment(version)}"


def get_model_version(model_id: str, version: int) -> Call[ModelVersion]:
    return Call("GET", _mv(model_id, version), parse_model(ModelVersion))


def get_model_artifact(model_id: str, version: int, name: str) -> Call[ArtifactLink]:
    path = f"{_mv(model_id, version)}/artifacts/{artifact_path(name)}"
    return Call("GET", path, parse_model(ArtifactLink))


def score_model(
    model_id: str,
    version: int,
    body: ScoreCall,
    *,
    wait: int | None,
    idempotency_key: str,
) -> Call[ScoreCallResult]:
    headers = idempotency_headers(idempotency_key)
    if wait is not None:
        if not 0 <= wait <= MAX_PREFER_WAIT_SECONDS:
            raise ValueError(f"wait must be between 0 and {MAX_PREFER_WAIT_SECONDS} seconds")
        headers[PREFER_HEADER] = f"wait={wait}"
    return Call(
        "POST",
        f"{_mv(model_id, version)}:score",
        parse_model(ScoreCallResult),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=headers,
        server_wait=float(wait) if wait else None,
    )


# ----------------------------------------------------------------------------------- plots


def list_plots(
    model_id: str, version: int, limit: int | None, cursor: str | None
) -> Call[Page[PlotListItem]]:
    path = f"{_mv(model_id, version)}/plots"
    return Call("GET", path, parse_model(Page[PlotListItem]), params=page_params(limit, cursor))


def get_plot(model_id: str, version: int, plot_id: str) -> Call[PlotSpec]:
    return Call("GET", f"{_mv(model_id, version)}/plots/{segment(plot_id)}", parse_model(PlotSpec))


def validate_plot(spec: PlotSpec) -> Call[PlotValidateResponse]:
    return Call("POST", f"{V1}/plots:validate", parse_model(PlotValidateResponse), json=dump(spec))


# ====================================================================== M2: identity (v0.3)


def get_me() -> Call[Me]:  # get_me
    return Call("GET", f"{V1}/me", parse_model(Me), scoped=False)


def list_tenants(limit: int | None, cursor: str | None) -> Call[Page[Tenant]]:
    return Call(
        "GET",
        f"{V1}/tenants",
        parse_model(Page[Tenant]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def _tenant(tenant_id: str) -> str:
    return f"{V1}/tenants/{segment(tenant_id)}"


def get_tenant(tenant_id: str) -> Call[Tenant]:
    return Call("GET", _tenant(tenant_id), parse_model(Tenant), scoped=False)


def update_tenant(tenant_id: str, body: TenantUpdate) -> Call[Tenant]:
    return Call("PATCH", _tenant(tenant_id), parse_model(Tenant), json=dump(body), scoped=False)


def list_tenant_projects(
    tenant_id: str, limit: int | None, cursor: str | None
) -> Call[Page[Project]]:
    return Call(
        "GET",
        f"{_tenant(tenant_id)}/projects",
        parse_model(Page[Project]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def create_project(tenant_id: str, body: ProjectCreate) -> Call[Project]:
    return Call(
        "POST",
        f"{_tenant(tenant_id)}/projects",
        parse_model(Project),
        _CREATED,
        json=dump(body),
        scoped=False,
    )


def _project(tenant_id: str, project_id: str) -> str:
    return f"{_tenant(tenant_id)}/projects/{segment(project_id)}"


def update_project(tenant_id: str, project_id: str, body: ProjectUpdate) -> Call[Project]:
    return Call(
        "PATCH",
        _project(tenant_id, project_id),
        parse_model(Project),
        json=dump(body),
        scoped=False,
    )


def delete_project(tenant_id: str, project_id: str) -> Call[None]:
    return Call("DELETE", _project(tenant_id, project_id), parse_none, _NO_CONTENT, scoped=False)


def _members(tenant_id: str, project_id: str | None) -> str:
    base = _tenant(tenant_id) if project_id is None else _project(tenant_id, project_id)
    return f"{base}/members"


def list_members(
    tenant_id: str, project_id: str | None, limit: int | None, cursor: str | None
) -> Call[Page[Membership]]:
    """``list_tenant_members`` (``project_id=None``) or ``list_members``."""
    return Call(
        "GET",
        _members(tenant_id, project_id),
        parse_model(Page[Membership]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def add_member(tenant_id: str, project_id: str | None, body: MembershipCreate) -> Call[Membership]:
    """``add_tenant_member`` (``project_id=None``) or ``add_member``."""
    return Call(
        "POST",
        _members(tenant_id, project_id),
        parse_model(Membership),
        _CREATED,
        json=dump(body),
        scoped=False,
    )


def update_member(
    tenant_id: str, project_id: str | None, principal_id: str, body: MembershipUpdate
) -> Call[Membership]:
    """``update_tenant_member`` (``project_id=None``) or ``update_member``."""
    return Call(
        "PATCH",
        f"{_members(tenant_id, project_id)}/{segment(principal_id)}",
        parse_model(Membership),
        json=dump(body),
        scoped=False,
    )


def remove_member(tenant_id: str, project_id: str | None, principal_id: str) -> Call[None]:
    """``remove_tenant_member`` (``project_id=None``) or ``remove_member``."""
    return Call(
        "DELETE",
        f"{_members(tenant_id, project_id)}/{segment(principal_id)}",
        parse_none,
        _NO_CONTENT,
        scoped=False,
    )


def usage_params(
    period: str | None, start: datetime | str | None, group_by: str | None
) -> dict[str, str | int]:
    if period is not None and period not in USAGE_PERIODS:
        raise ValueError(f"period must be one of {', '.join(USAGE_PERIODS)}")
    if group_by is not None and group_by not in USAGE_GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(USAGE_GROUPS)}")
    if isinstance(start, datetime):
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        start = start.isoformat()
    params: dict[str, str | int] = {}
    for key, value in (("period", period), ("start", start), ("group_by", group_by)):
        if value is not None:
            params[key] = value
    return params


def get_tenant_usage(
    tenant_id: str,
    *,
    period: str | None = None,
    start: datetime | str | None = None,
    group_by: str | None = None,
) -> Call[UsageReport]:
    return Call(
        "GET",
        f"{_tenant(tenant_id)}/usage",
        parse_model(UsageReport),
        params=usage_params(period, start, group_by),
        scoped=False,
    )


def audit_params(
    limit: int | None,
    cursor: str | None,
    *,
    tenant_id: str | None = None,
    project_id: str | None = None,
    actor_id: str | None = None,
    action: AuditAction | str | None = None,
    outcome: AuditOutcome | str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, str | int]:
    for name, when in (("since", since), ("until", until)):
        if when is not None and when.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
    return page_params(
        limit,
        cursor,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_id=actor_id,
        action=AuditAction(action).value if action is not None else None,
        outcome=AuditOutcome(outcome).value if outcome is not None else None,
        since=since.isoformat() if since is not None else None,
        until=until.isoformat() if until is not None else None,
    )


def list_tenant_audit(
    tenant_id: str, limit: int | None, cursor: str | None, **filters: Any
) -> Call[Page[AuditEvent]]:
    return Call(
        "GET",
        f"{_tenant(tenant_id)}/audit",
        parse_model(Page[AuditEvent]),
        params=audit_params(limit, cursor, **filters),
        scoped=False,
    )


# ================================================================== M2: tokens and usage


def list_tokens(
    limit: int | None,
    cursor: str | None,
    *,
    all_owners: bool = False,
    status: TokenStatus | str | None = None,
) -> Call[Page[ApiToken]]:
    params = page_params(
        limit,
        cursor,
        all="true" if all_owners else None,
        status=TokenStatus(status).value if status is not None else None,
    )
    return Call("GET", f"{V1}/tokens", parse_model(Page[ApiToken]), params=params)


def create_token(body: TokenCreate) -> Call[TokenCreated]:
    # not retried after a lost response: a second POST would mint a second token
    return Call("POST", f"{V1}/tokens", parse_model(TokenCreated), _CREATED, json=dump(body))


def get_token(token_id: str) -> Call[ApiToken]:
    return Call("GET", f"{V1}/tokens/{segment(token_id)}", parse_model(ApiToken))


def revoke_token(token_id: str) -> Call[None]:  # idempotent
    return Call("DELETE", f"{V1}/tokens/{segment(token_id)}", parse_none, _NO_CONTENT)


def get_usage(
    *,
    period: str | None = None,
    start: datetime | str | None = None,
    group_by: str | None = None,
) -> Call[UsageReport]:
    return Call(
        "GET",
        f"{V1}/usage",
        parse_model(UsageReport),
        params=usage_params(period, start, group_by),
    )


# ===================================================================== M2: operators (admin)

_ADMIN: Final = f"{V1}/admin"


def admin_list_tenants(
    limit: int | None,
    cursor: str | None,
    *,
    status: TenantStatus | str | None = None,
    trust_tier: TrustTier | str | None = None,
) -> Call[Page[Tenant]]:
    params = page_params(
        limit,
        cursor,
        status=TenantStatus(status).value if status is not None else None,
        trust_tier=TrustTier(trust_tier).value if trust_tier is not None else None,
    )
    return Call("GET", f"{_ADMIN}/tenants", parse_model(Page[Tenant]), params=params, scoped=False)


def admin_create_tenant(body: TenantCreate) -> Call[Tenant]:
    return Call(
        "POST",
        f"{_ADMIN}/tenants",
        parse_model(Tenant),
        _CREATED,
        json=dump(body),
        scoped=False,
    )


def admin_get_tenant(tenant_id: str) -> Call[Tenant]:
    path = f"{_ADMIN}/tenants/{segment(tenant_id)}"
    return Call("GET", path, parse_model(Tenant), scoped=False)


def admin_update_tenant(tenant_id: str, body: TenantAdminUpdate) -> Call[Tenant]:
    path = f"{_ADMIN}/tenants/{segment(tenant_id)}"
    return Call("PATCH", path, parse_model(Tenant), json=dump(body), scoped=False)


def admin_list_quotas(
    limit: int | None,
    cursor: str | None,
    *,
    scope: QuotaScope | str | None = None,
    subject_id: str | None = None,
) -> Call[Page[QuotaPolicy]]:
    params = page_params(
        limit,
        cursor,
        scope=QuotaScope(scope).value if scope is not None else None,
        subject_id=subject_id,
    )
    return Call(
        "GET", f"{_ADMIN}/quotas", parse_model(Page[QuotaPolicy]), params=params, scoped=False
    )


def admin_put_quota(body: QuotaPolicyUpsert) -> Call[QuotaPolicy]:
    return Call("PUT", f"{_ADMIN}/quotas", parse_model(QuotaPolicy), json=dump(body), scoped=False)


def admin_delete_quota(scope: QuotaScope | str, subject_id: str) -> Call[None]:
    path = f"{_ADMIN}/quotas/{segment(QuotaScope(scope).value)}/{segment(subject_id)}"
    return Call("DELETE", path, parse_none, _NO_CONTENT, scoped=False)


def admin_get_kill_switches() -> Call[KillSwitches]:
    return Call("GET", f"{_ADMIN}/kill-switches", parse_model(KillSwitches), scoped=False)


def admin_put_kill_switches(body: KillSwitchUpdate) -> Call[KillSwitches]:
    return Call(
        "PUT",
        f"{_ADMIN}/kill-switches",
        parse_model(KillSwitches),
        json=dump(body),
        scoped=False,
    )


def admin_list_executors(limit: int | None, cursor: str | None) -> Call[Page[ExecutorHealth]]:
    return Call(
        "GET",
        f"{_ADMIN}/executors",
        parse_model(Page[ExecutorHealth]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def admin_get_executor(name: str) -> Call[ExecutorHealth]:
    path = f"{_ADMIN}/executors/{segment(name)}"
    return Call("GET", path, parse_model(ExecutorHealth), scoped=False)


def admin_get_executor_evidence(name: str) -> Call[ExecutorEvidence]:
    path = f"{_ADMIN}/executors/{segment(name)}/evidence"
    return Call("GET", path, parse_model(ExecutorEvidence), scoped=False)


def admin_put_executor_evidence(name: str, body: ExecutorEvidenceUpdate) -> Call[ExecutorEvidence]:
    return Call(
        "PUT",
        f"{_ADMIN}/executors/{segment(name)}/evidence",
        parse_model(ExecutorEvidence),
        json=dump(body),
        scoped=False,
    )


def admin_get_usage(
    *, period: str | None = None, start: datetime | str | None = None
) -> Call[AdminUsageReport]:
    return Call(
        "GET",
        f"{_ADMIN}/usage",
        parse_model(AdminUsageReport),
        params=usage_params(period, start, None),
        scoped=False,
    )


def admin_put_executor(name: str, body: ExecutorUpdate) -> Call[ExecutorHealth]:
    path = f"{_ADMIN}/executors/{segment(name)}"
    return Call("PUT", path, parse_model(ExecutorHealth), json=dump(body), scoped=False)


def admin_list_audit(
    limit: int | None, cursor: str | None, **filters: Any
) -> Call[Page[AuditEvent]]:
    return Call(
        "GET",
        f"{_ADMIN}/audit",
        parse_model(Page[AuditEvent]),
        params=audit_params(limit, cursor, **filters),
        scoped=False,
    )
