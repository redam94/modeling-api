"""One builder per endpoint of ``mapi_spec.api.M1_ENDPOINTS`` (operation ids in comments).

Builders are pure: they validate arguments, encode paths and return a :class:`Call`.
``tests/test_endpoint_coverage.py`` checks that every M1 endpoint has a builder.
"""

from __future__ import annotations

from typing import Any, Final

from mapi_spec.api import (
    API_PREFIX,
    MAX_PREFER_WAIT_SECONDS,
    PAGE_LIMIT_MAX,
    PREFER_HEADER,
    ArtifactLink,
    CatalogResponse,
    Dataset,
    DatasetCreate,
    DatasetVersion,
    DatasetVersionCreate,
    Health,
    LogLine,
    Model,
    ModelVersion,
    Page,
    PlotListItem,
    PlotSpec,
    PlotValidateResponse,
    Project,
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
    UploadCreate,
    UploadSession,
    ValidateRequest,
    ValidateResponse,
)
from mapi_spec.catalog import ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.executor import RunKind
from mapi_spec.run import RunState

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
    return Call("GET", "/healthz", parse_model(Health), _OK)


def ready() -> Call[Health]:  # ready (503 carries the same body)
    return Call("GET", "/readyz", parse_model(Health), frozenset({200, 503}))


# -------------------------------------------------------------------------------- projects


def list_projects(limit: int | None, cursor: str | None) -> Call[Page[Project]]:
    return Call(
        "GET", f"{V1}/projects", parse_model(Page[Project]), params=page_params(limit, cursor)
    )


def get_project(project_id: str) -> Call[Project]:
    return Call("GET", f"{V1}/projects/{segment(project_id)}", parse_model(Project))


# --------------------------------------------------------------------------------- catalog


def get_catalog() -> Call[CatalogResponse]:
    return Call("GET", f"{V1}/catalog", parse_model(CatalogResponse))


def list_catalog_models(limit: int | None, cursor: str | None) -> Call[Page[ModelCatalogEntry]]:
    return Call(
        "GET",
        f"{V1}/catalog/models",
        parse_model(Page[ModelCatalogEntry]),
        params=page_params(limit, cursor),
    )


def get_catalog_model(model_id: str, version: str) -> Call[ModelCatalogEntry]:
    path = f"{V1}/catalog/models/{segment(model_id)}/{segment(version)}"
    return Call("GET", path, parse_model(ModelCatalogEntry))


def list_catalog_steps(limit: int | None, cursor: str | None) -> Call[Page[StepCatalogEntry]]:
    return Call(
        "GET",
        f"{V1}/catalog/steps",
        parse_model(Page[StepCatalogEntry]),
        params=page_params(limit, cursor),
    )


def list_catalog_plots(limit: int | None, cursor: str | None) -> Call[Page[PlotCatalogEntry]]:
    return Call(
        "GET",
        f"{V1}/catalog/plots",
        parse_model(Page[PlotCatalogEntry]),
        params=page_params(limit, cursor),
    )


def get_schema(name: str) -> Call[dict[str, Any]]:
    return Call("GET", f"{V1}/schemas/{segment(name)}", parse_json)


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
