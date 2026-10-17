# HTTP API (v1) <span class="mapi-badge">v0.2</span>

The platform API that ships with v0.2 (milestone M1, the local vertical slice). Every request
and response body is a Pydantic model in [`mapi_spec.api`](api/spec.md#rest-dtos). The server
serves an OpenAPI 3.1 document generated from those models, and each model carries examples.
The endpoint table on this page matches `mapi_spec.api.M1_ENDPOINTS`, and a test fails if the
two drift apart.

| See also | |
|---|---|
| [OpenAPI explorer](openapi.md) | The OpenAPI 3.1 document, browsable, with every schema and example ([full screen](openapi-viewer.html)) |
| [HTTP API guide](../guides/http-api.md) | The main flow with `curl` and `jq`: upload, validate, fit, follow, score |
| [Python client](../guides/python-client.md) | The same flow with `mapi_client` |

!!! info "M1 is single-tenant"
    A local install has one tenant and one project, both called `default`. Every resource
    still carries `tenant_id` and `project_id`, and every storage key starts with
    `t/{tenant}/`, so multi-tenancy in v0.3 adds row-level security without changing the
    schema. There is no authentication in M1. The API listens on `127.0.0.1` only.
    The tenants, sign-in, tokens and quotas that v0.3 adds are described in
    [Multi-tenant API](#multi-tenant-api).

## Conventions

| Topic | Rule |
|---|---|
| Base URL | `http://127.0.0.1:8000` locally. Resource paths start with `/v1`. |
| Bodies | `application/json` (UTF-8). Spec text travels as a string field (`yaml`) inside JSON. |
| Unknown fields | Request bodies **reject** unknown fields (`SCHEMA_INVALID`). Clients **ignore** unknown response fields, so the server can add fields without a new version. |
| Timestamps | RFC 3339, timezone-aware, UTC (`2026-10-15T09:30:00Z`). |
| Ids | `<prefix>_<26 lowercase Crockford base32>`, ULID layout, sortable by creation time (`run_01jaf3q2x8m9v7k5h4g3f2e1d0`). Prefixes: `ds`, `upl`, `spc`, `run`, `mdl`, `mv`. Projects and tenants use slugs (`default`). |
| Durations | `"45m"`, `"1h30m"`, `"90s"`. |
| Actions | Non-CRUD actions use a colon suffix: `POST /v1/runs/{run_id}:cancel`. |
| Untrusted text | Log lines, tracebacks and plugin messages only ever appear in `untrusted_text` or `LogLine.line`. Render them as text, never as HTML or markdown. |

### Errors

Every error is an [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807) problem document
(`application/problem+json`, model `mapi_spec.Problem`) with a stable `code`:

```json
{
  "type": "urn:mapi:error:LEAKAGE_RISK",
  "title": "A stateful step runs before the split",
  "status": 422,
  "code": "LEAKAGE_RISK",
  "detail": "validation failed with 1 error; first at /pipeline/2: …",
  "errors": [
    {"code": "LEAKAGE_RISK", "severity": "error", "pointer": "/pipeline/2",
     "message": "stateful step 'scale_y' runs before the split"}
  ]
}
```

- Malformed request bodies, path parameters and query parameters return **422
  `SCHEMA_INVALID`**. Each problem goes in `errors[]` with an RFC 6901 `pointer` into the
  request (`/body/kind`, `/query/limit`).
- Every endpoint can also return `RATE_LIMITED` (429), `INTERNAL_ERROR` (500) and
  `INFRA_ERROR` (503). These are not repeated in the table.
- The full list of codes and their HTTP statuses is in [Error codes](generated/error-codes.md).

### Pagination

List endpoints return `Page[T]`:

```json
{"items": [ … ], "next_cursor": "eyJpZCI6InJ1bl8wMWphZiJ9"}
```

- Pass `?limit=` (default 50, maximum 200) and `?cursor=<next_cursor>` to fetch the next
  page. A `null` cursor means the current page is the last one.
- Cursors are opaque and only valid for the same list and filters.
- Items are ordered newest first by id.

### Idempotency

`POST /v1/runs`, `…:score`, `…:retry`, `…:dry-run` and `POST …/versions` (ingest) accept an
`Idempotency-Key` header (1–255 printable ASCII characters, scoped to the project and kept for
24 hours):

| Situation | Response |
|---|---|
| First request with the key | The normal response, for example `201 Created` with the `Run` |
| Same key, same body (byte-equal after canonical JSON) | **`200 OK`** with the originally created resource. No second run is created. |
| Same key, different body | **`409 IDEMPOTENCY_CONFLICT`** |
| Same key while the first request is still in flight | `409 CONFLICT`. Retry later. |

Clients should generate one key per user intent, for example a UUIDv4 when the user clicks
**Fit**, and reuse it on every network retry.

### Long-running work and `Prefer: wait`

Runs are asynchronous. Creating one returns the `Run` at once in state `created` or `queued`.
`POST /v1/models/{model_id}/versions/{version}:score` also honours
[RFC 7240](https://www.rfc-editor.org/rfc/rfc7240) `Prefer: wait=N`, with N capped at 60
seconds:

- If the score run reaches a terminal state within N seconds, the response is **200** with
  `ScoreCallResult{done: true, run, outputs: [ArtifactLink…]}`.
- Otherwise the response is **202** with `done: false` and the run. Follow the run with
  `GET /v1/runs/{run_id}` or its event stream.
- The `Preference-Applied: wait=N` response header echoes the wait the server actually used.

## Run events (server-sent events)

`GET /v1/runs/{run_id}/events` has two representations:

- **`Accept: text/event-stream`**: a live SSE stream.
- **Any other `Accept`**: a JSON `Page[RunEventView]` of persisted events. Pass `?after=<id>`
  to read events after a given id.

The stream sends these frames, in this order:

1. `event: snapshot` with the full `Run` JSON. It is the first frame on every connection and
   has no `id:`.
2. A replay of the persisted events whose `id` is greater than the `Last-Event-ID` request
   header. Without the header the replay starts from the beginning. Every replayed frame
   carries `id: <RunEventView.id>`. The ids increase per run, so a reconnecting client
   resumes exactly where it stopped, including across the 60-minute request cut on Cloud
   Run.
3. Live frames as events arrive: `state`, `progress`, `log`, `diagnostic`, `warning`,
   `checkpoint`, `metric`, `complete` and `error`. The `data:` of each frame is one line of
   compact JSON (`RunEventView`).
4. `event: end` once the run is terminal (data: the final `Run`). The server then closes the
   stream. A client that sees `end` must not reconnect.

The server also sends a fresh `event: snapshot` (no `id:`) whenever the run's state changes,
so a client can re-render from the latest `Run` without reassembling it from events. `progress`
and `log` frames are *not* persisted: they are sent live without an `id:` (data: `{run_id,
attempt, seq, ts, type, payload}`) and are never replayed; `Last-Event-ID` only covers
persisted events.

Between events the server writes a heartbeat comment (`: keepalive`) every 15 seconds. The
first frame also carries `retry: 3000`.

An open stream checks its caller's credential again every minute (`MAPI_SSE_REAUTH_S`). Once
the token is revoked or expired, the session has ended, or the caller has lost access to the
project, the server closes the stream without `end`. The reconnect then answers `401` or
`403`, and clients should stop there rather than retry.

Progress frames are coalesced to at most one every 2–5 seconds. Only state changes,
warnings, diagnostics, checkpoints, metrics and terminal events are persisted and replayable.
A reconnecting client gets the latest progress from the `snapshot` frame. Log lines are paged
from `GET /v1/runs/{run_id}/logs`.

```text
retry: 3000
event: snapshot
data: {"id":"run_01jaf3q2x8m9v7k5h4g3f2e1d0","state":"running", …}

id: 17
event: state
data: {"id":17,"run_id":"run_01jaf3q2x8m9v7k5h4g3f2e1d0","attempt":1,"seq":3,"type":"state", …}

: keepalive

```

## Endpoints

Columns: **Request** and **Response** are DTO names in `mapi_spec.api`, unless they are
catalog or plot models from `mapi_spec`. **Errors** lists the endpoint-specific codes. The
common ones from the [Errors](#errors) section are not repeated.

### Health

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /healthz` | Process liveness | — | `Health` | 200 | — |
| `GET /readyz` | Postgres and the object store are reachable (503 with the same body if not) | — | `Health` | 200 | — |

### Projects and catalog

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/projects` | List projects | — | `Page[Project]` | 200 | — |
| `GET /v1/projects/{project_id}` | Get a project | — | `Project` | 200 | `NOT_FOUND` |
| `GET /v1/catalog` | The whole catalog with its digest | — | `CatalogResponse` | 200 | — |
| `GET /v1/catalog/models` | Model plugins | — | `Page[ModelCatalogEntry]` | 200 | — |
| `GET /v1/catalog/models/{model_id}/{version}` | One plugin version (manifest, sanitized schemas, schema template) | — | `ModelCatalogEntry` | 200 | `NOT_FOUND` |
| `GET /v1/catalog/steps` | Steps | — | `Page[StepCatalogEntry]` | 200 | — |
| `GET /v1/catalog/plots` | Plot recipes | — | `Page[PlotCatalogEntry]` | 200 | — |
| `GET /v1/schemas/{name}` | An exported JSON Schema, for example `modelspec.json` (drives Monaco and JSON Forms) | — | JSON Schema | 200 | `NOT_FOUND` |

### Datasets

Data reaches the platform in three steps:

1. Create the dataset.
2. `PUT` the file to the signed URL from an upload session. The URL points into the
   quarantine bucket and the API never sees the bytes.
3. Create a version. A **sandboxed ingest run** parses the upload, validates it, and writes
   the content-addressed `data.zarr.zip` with its schema, profile and preview.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/datasets` | Create a dataset | `DatasetCreate` | `Dataset` | 201 | `CONFLICT` (name taken) |
| `GET /v1/datasets` | List datasets | — | `Page[Dataset]` | 200 | — |
| `GET /v1/datasets/{dataset_id}` | Get a dataset | — | `Dataset` | 200 | `NOT_FOUND` |
| `POST /v1/datasets/{dataset_id}/uploads` | Signed PUT URL into quarantine | `UploadCreate` | `UploadSession` | 201 | `NOT_FOUND`, `PAYLOAD_TOO_LARGE`, `QUOTA_EXCEEDED` |
| `POST /v1/datasets/{dataset_id}/versions` | Ingest an upload as a new version; starts an `ingest` run. Accepts `Idempotency-Key`. | `DatasetVersionCreate` | `DatasetVersion` (status `ingesting`) | 202 | `NOT_FOUND`, `CONFLICT` (upload not finished or already consumed) |
| `GET /v1/datasets/{dataset_id}/versions` | List versions | — | `Page[DatasetVersion]` | 200 | `NOT_FOUND` |
| `GET /v1/datasets/{dataset_id}/versions/{version}` | Schema, summary, profile and preview refs | — | `DatasetVersion` | 200 | `NOT_FOUND` |
| `GET /v1/datasets/{dataset_id}/versions/{version}/artifacts/{name}` | Signed URL for `data.zarr.zip`, `schema.json`, `profile.json` or `preview.json` | — | `ArtifactLink` | 200 | `NOT_FOUND` |

### Specs

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/specs:validate` | Synchronous static validation. Returns every issue at once with JSON pointers, plus the schema at each pipeline stage. | `ValidateRequest` | `ValidateResponse` | 200 | `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE`, `SCHEMA_INVALID` |
| `POST /v1/specs` | Create a spec and its revision 1 | `SpecCreate` | `SpecDoc` | 201 | `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE`, `SCHEMA_INVALID`, `CONFLICT` |
| `GET /v1/specs` | List specs | — | `Page[SpecDoc]` | 200 | — |
| `GET /v1/specs/{spec_id}` | Get a spec with its latest revision | — | `SpecDoc` | 200 | `NOT_FOUND` |
| `POST /v1/specs/{spec_id}/revisions` | Add an immutable revision | `SpecRevisionCreate` | `SpecRevision` | 201 | `NOT_FOUND`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE`, `SCHEMA_INVALID`, `CONFLICT` (stale `parent_revision`) |
| `GET /v1/specs/{spec_id}/revisions` | List revisions | — | `Page[SpecRevision]` | 200 | `NOT_FOUND` |
| `GET /v1/specs/{spec_id}/revisions/{revision}` | Get one revision | — | `SpecRevision` | 200 | `NOT_FOUND` |
| `POST /v1/specs/{spec_id}/revisions/{revision}:dry-run` | Explicit sandboxed dry-run, the same as `POST /v1/runs` with `kind: validate`. Accepts `Idempotency-Key`. | — | `Run` | 202 | `NOT_FOUND`, `QUOTA_EXCEEDED` |

How validation reports results:

- `POST /v1/specs:validate` returns **200 even when the document is invalid**. The verdict is
  in `ValidateResponse.valid` and `issues`.
- The non-2xx statuses are kept for requests that cannot be validated at all: unparseable
  YAML, an oversized document, or a malformed request body.
- Creating a spec or a revision stores invalid revisions too, with `valid: false` and their
  `issues`, so the editor can save work in progress.
- `POST /v1/runs` refuses invalid revisions with the first error's code. Examples:
  `LEAKAGE_RISK`, `UNSEEN_LEVELS`.

### Runs

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/runs` | Submit a run (200 on an idempotent replay). Accepts `Idempotency-Key`. | `RunCreate` | `Run` | 201 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `QUOTA_EXCEEDED`, `MODE_NOT_SUPPORTED`, `LEAKAGE_RISK`, `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT`, `MISSING_FUTURE_COVARIATE`, `RECURSIVE_UNSUPPORTED` |
| `GET /v1/runs` | List runs (filters: `kind`, `state`, `spec_id`) | — | `Page[Run]` | 200 | — |
| `GET /v1/runs/{run_id}` | Get a run (state, failure class and problem, attempt, progress snapshot, cost: placeholders in v0.2, metered from v0.3) | — | `Run` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/events` | SSE stream or JSON page of persisted events (see [Run events](#run-events-server-sent-events)). Accepts `Last-Event-ID`. | — | `Page[RunEventView]` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/logs` | Captured log lines; filters `attempt`, `stream` | — | `Page[LogLine]` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/plots` | Plots a succeeded run rendered (`plots.json`: forecast fans, counterfactual bands), passed through the plot sanitizer on every read; rejected plots are left out. Empty until the run succeeds. | — | `Page[PlotSpec]` | 200 | `NOT_FOUND`, `INTEGRITY_FAILURE` |
| `POST /v1/runs/{run_id}:cancel` | Request cancellation. Idempotent: it returns the run in `cancelling`, or in its terminal state if the run had already finished. A `finalizing` run is left alone and returned as it is: its execution is already over. | — | `Run` | 202 | `NOT_FOUND`, `CONFLICT` |
| `POST /v1/runs/{run_id}:resume` | Resume a `partial` run from its checkpoints (new attempt) | — | `Run` | 202 | `NOT_FOUND`, `CONFLICT` (not `partial`) |
| `POST /v1/runs/{run_id}:retry` | Start a **new** run from a failed one, for example one resource class up after `RESOURCE_EXHAUSTED`. Accepts `Idempotency-Key`. | `RunRetry` | `Run` | 201 | `NOT_FOUND`, `CONFLICT` (not failed; a workflow, workflow node or governance run - start the workflow again or resubmit the validation) |

What `RunCreate` needs depends on `kind`:

| `kind` | Required | Not allowed |
|---|---|---|
| `fit`, `validate` (sandboxed dry-run) | `spec_id`, `revision` | `model_version_id`, `request` |
| `score` | `model_version_id`, `request` (a `ScoreRequest` document) | `spec_id`, `revision` |
| `evaluate`, `derive`, `render` | `model_version_id` (optional `params`) | `spec_id`, `revision`, `request` |

`ingest` runs are created by `POST /v1/datasets/{dataset_id}/versions`. `describe`,
`optimize` and the fan-out parents (`sweep`, `backtest`, `sbc`, `campaign`) cannot be created
in v0.2. Run states and the transitions between them are listed in
[Run protocol](run-protocol.md#run-states).

### Models and scoring

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/models` | List models | — | `Page[Model]` | 200 | — |
| `GET /v1/models/{model_id}` | Get a model | — | `Model` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions` | List versions | — | `Page[ModelVersion]` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions/{version}` | Signed bundle manifest, diagnostics, scorecard, lineage, metrics | — | `ModelVersion` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions/{version}/artifacts/{name}` | Signed URL for a bundle object or a `ui/…` view; `name` may contain `/` | — | `ArtifactLink` | 200 | `NOT_FOUND`, `INTEGRITY_FAILURE` |
| `POST /v1/models/{model_id}/versions/{version}:score` | Score with replayed preprocessing. Accepts `Prefer: wait=N` (200 if done, else 202) and `Idempotency-Key`. | `ScoreCall` | `ScoreCallResult` | 200 | `NOT_FOUND`, `MODE_NOT_SUPPORTED`, `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT`, `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`, `CAPABILITY_UNSUPPORTED`, `RECURSIVE_UNSUPPORTED`, `QUOTA_EXCEEDED` |

Artifact links are minted only after two checks pass:

1. The authorization check.
2. Verification of the model version's **signed bundle manifest**: tenant, model version,
   digests and object generations must all match.

If the second check fails, the response is `INTEGRITY_FAILURE` and no URL is issued. Each
link is pinned to an object generation, expires after 15 minutes, and supports HTTP `Range`
requests.

### Plots

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/models/{model_id}/versions/{version}/plots` | Plots of a model version | — | `Page[PlotListItem]` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions/{version}/plots/{plot_id}` | A sanitized `PlotSpec`. Render it only inside the plot-sandbox iframe, which has its own origin. | — | `PlotSpec` | 200 | `NOT_FOUND` |
| `POST /v1/plots:validate` | Run the server-side plot sanitizer on a user-authored spec | `PlotSpec` | `PlotValidateResponse` | 200 | `SCHEMA_INVALID` |

## Multi-tenant API <span class="mapi-badge">v0.3</span> { #multi-tenant-api }

v0.3 (milestone M2) adds tenants, projects, members, API tokens, quotas, cost and an operator
console. The models are in `mapi_spec.api`, `mapi_spec.tenancy`, `mapi_spec.tokens`,
`mapi_spec.quotas`, `mapi_spec.cost`, `mapi_spec.audit`, `mapi_spec.admin` and
`mapi_spec.auth`. The endpoint table below matches `mapi_spec.api.M2_ENDPOINTS`, and
`mapi_spec.api.ROUTE_ACCESS` gives every operation (v0.2 ones included) its scope and the
permission it checks. Tests fail if the code, this page and the server drift apart. Roles,
tokens and scopes are explained in [Tenancy and authentication](../concepts/tenancy-and-auth.md),
limits and prices in [Quotas and cost](../concepts/quotas-and-cost.md).

### Authentication

| Caller | Credential | Notes |
|---|---|---|
| Browser (the web app) | Session cookie `__Host-mapi_session` (`HttpOnly; Secure; SameSite=Strict; Path=/`), set by `GET /auth/callback` | OpenID Connect Authorization Code flow with PKCE (S256) and a `nonce`, run by the API as a backend-for-frontend. The browser never holds an OAuth token. |
| Programs, SDKs, CLI | `Authorization: Bearer mapi_<prefix>_<secret><checksum>` | An API token. It acts as its owner, narrowed by its scopes, in the one project it is bound to. |
| Google Pub/Sub | A Google-signed OIDC ID token on `POST /internal/events` | Audience, issuer and the push service account are checked. Nothing in the body authenticates. |

Responses for authentication and authorization:

| Situation | Response |
|---|---|
| No credential, or an expired session, on a non-public route | `401 UNAUTHENTICATED` |
| A malformed token, or one whose checksum fails | `401 UNAUTHENTICATED`. The presented value is never echoed. |
| A revoked or expired token | `401 UNAUTHENTICATED` |
| A resource in a project or tenant the caller cannot see | `404 NOT_FOUND`, the same as a missing one, so ids cannot be probed |
| A visible resource, but the role or the token's scopes do not allow the action | `403 FORBIDDEN` |
| A token used outside the project it is bound to | `403 FORBIDDEN` |
| The tenant is `suspended` | Reads work. Everything else returns `403 FORBIDDEN`. |

### Selecting the project

Every v0.2 resource (datasets, specs, runs, models, plots) belongs to a project. From v0.3 a
request names its project in one of these ways, in this order of precedence:

1. **Path parameters**, on routes under `/v1/tenants/{tenant_id}/projects/{project_id}`.
2. **The `X-Mapi-Project` header**: `X-Mapi-Project: <tenant_id>/<project_id>`, for example
   `X-Mapi-Project: acme/growth`. A bare `X-Mapi-Project: growth` is accepted when the tenant
   is unambiguous: the caller belongs to one tenant, or uses a token.
3. **The token's binding.** A token always acts in its own project, so token clients may omit
   the header. A header that names another project returns `403 FORBIDDEN`.
4. **The deployment default**, in the single-tenant local profile only (`default/default`, as
   in v0.2).

If none applies, or path and header disagree, the request fails with `422 SCHEMA_INVALID`.
`mapi_spec.tenancy.resolve_project_scope` implements these rules.

The scope of each v0.2 endpoint (by operation id) and the permission checked:

| Scope | Operations | Permission |
|---|---|---|
| Public | `health`, `ready`, `get_schema` | None |
| Authenticated | `list_projects`, `get_catalog`, `list_catalog_models`, `get_catalog_model`, `list_catalog_steps`, `list_catalog_plots` | Any signed-in caller. Lists are filtered to what the caller can see. |
| Project | `get_project` | `read` `project` |
| Project | `create_dataset` / `create_upload`, `create_dataset_version` | `create` / `update` `dataset` (editor) |
| Project | `list_datasets`, `get_dataset`, `list_dataset_versions`, `get_dataset_version`, `get_dataset_artifact` | `read` `dataset` |
| Project | `validate_spec`, `list_specs`, `get_spec`, `list_spec_revisions`, `get_spec_revision` | `read` `spec` |
| Project | `create_spec` / `create_spec_revision` | `create` / `update` `spec` (editor) |
| Project | `create_run`, `dry_run_spec`, `resume_run`, `retry_run` | `create` `run` (runner). Runs on a model version also need `run` `model`. |
| Project | `cancel_run` | `cancel` `run` (runner) |
| Project | `list_runs`, `get_run`, `run_events`, `run_logs`, `list_run_plots` | `read` `run` |
| Project | `list_models`, `get_model`, `list_model_versions`, `get_model_version`, `get_model_artifact` | `read` `model` |
| Project | `score_model` | `run` `model` (runner) |
| Project | `list_plots`, `get_plot`, `validate_plot` | `read` `plot` |

Storage keys and rows were already tenant- and project-qualified in v0.2 (`t/{tenant}/…`), so
the switch needs no data migration. Postgres enforces the boundary with row-level security
on `tenant_id` in every table.

### Endpoints

#### Identity, tenants and projects

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/me` | The caller (`Principal`), their tenants, projects and roles; for a token, the token too | — | `Me` | 200 | `UNAUTHENTICATED` |
| `GET /v1/tenants` | Tenants the caller belongs to | — | `Page[Tenant]` | 200 | — |
| `GET /v1/tenants/{tenant_id}` | Get a tenant | — | `Tenant` | 200 | `NOT_FOUND` |
| `PATCH /v1/tenants/{tenant_id}` | Rename a tenant (tenant admin) | `TenantUpdate` | `Tenant` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/tenants/{tenant_id}/projects` | Projects of the tenant that the caller can see | — | `Page[Project]` | 200 | `NOT_FOUND` |
| `POST /v1/tenants/{tenant_id}/projects` | Create a project (tenant admin) | `ProjectCreate` | `Project` | 201 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` (id taken) |
| `PATCH /v1/tenants/{tenant_id}/projects/{project_id}` | Rename or describe a project (project admin) | `ProjectUpdate` | `Project` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `DELETE /v1/tenants/{tenant_id}/projects/{project_id}` | Delete an empty project (tenant admin) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` (it still holds data) |

#### Members

A membership grants one role. A tenant-wide membership grants it on the tenant and on every
project in it; a project membership grants it on that project only. The effective role in a
project is the higher of the two. Add a member by `principal_id`, or by `email` to invite
someone who has not signed in yet (the invitation is claimed at first sign-in with that
verified e-mail). Removing or demoting the last admin returns `409 CONFLICT`.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/tenants/{tenant_id}/members` | Tenant-wide members | — | `Page[Membership]` | 200 | `NOT_FOUND` |
| `POST /v1/tenants/{tenant_id}/members` | Grant a tenant-wide role (tenant admin) | `MembershipCreate` | `Membership` | 201 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `PATCH /v1/tenants/{tenant_id}/members/{principal_id}` | Change a tenant-wide role | `MembershipUpdate` | `Membership` | 200 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `DELETE /v1/tenants/{tenant_id}/members/{principal_id}` | Remove a tenant-wide role | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `GET /v1/tenants/{tenant_id}/projects/{project_id}/members` | Project members, tenant-wide grants included | — | `Page[Membership]` | 200 | `NOT_FOUND` |
| `POST /v1/tenants/{tenant_id}/projects/{project_id}/members` | Grant a project role (project admin) | `MembershipCreate` | `Membership` | 201 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `PATCH /v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}` | Change a project role | `MembershipUpdate` | `Membership` | 200 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `DELETE /v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}` | Remove a project role | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |

#### API tokens

Tokens belong to the project selected by `X-Mapi-Project`. `POST /v1/tokens` returns the full
token **once**, in `TokenCreated.secret`; it cannot be shown again. Listings show the redacted
form `mapi_<prefix>_…`. Any member may mint, list and revoke their own tokens; listing
everyone's tokens (`?all=true`) or revoking someone else's needs `manage` on `token` (project
admin).

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/tokens` | The caller's tokens; filters `all`, `status` | — | `Page[ApiToken]` | 200 | — |
| `POST /v1/tokens` | Mint a token with scopes, an expiry, and an optional rate limit and vCPU-second budget | `TokenCreate` | `TokenCreated` | 201 | `FORBIDDEN` |
| `GET /v1/tokens/{token_id}` | Get a token (never its secret) | — | `ApiToken` | 200 | `NOT_FOUND` |
| `DELETE /v1/tokens/{token_id}` | Revoke a token. Idempotent. | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |

#### Usage

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/usage` | Cost and quota of the selected project for a UTC `period` (`day` or `month`), optionally grouped (`group_by`: `resource_class`, `project`, `day`, `token`, `run`) | — | `UsageReport` | 200 | — |
| `GET /v1/tenants/{tenant_id}/usage` | The same for the whole tenant (tenant admin) | — | `UsageReport` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/tenants/{tenant_id}/audit` | The tenant's audit log (tenant admin); filters `project_id`, `actor_id`, `action`, `outcome`, `since`, `until` | — | `Page[AuditEvent]` | 200 | `NOT_FOUND`, `FORBIDDEN` |

Every `Run` now carries a filled-in `cost` (`RunCost`): `estimated` is `true` until the run's
first attempt has been metered, `amount_micros` is in `currency` (1 USD = 1,000,000 micros) and
`price_version` names the price table used. The v0.2 fields `vcpu_seconds`, `gib_seconds` and
`usd` keep their meaning.

#### Operators

Operator endpoints need a platform operator. Tenant admins get `403 FORBIDDEN`. Operators see
tenant metadata, usage and audit logs, but no tenant content (datasets, specs, models, plots).
Every change is written to the audit log.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/admin/tenants` | All tenants; filters `status`, `trust_tier` | — | `Page[Tenant]` | 200 | `FORBIDDEN` |
| `POST /v1/admin/tenants` | Create a tenant, optionally inviting its first admin | `TenantCreate` | `Tenant` | 201 | `FORBIDDEN`, `CONFLICT` |
| `GET /v1/admin/tenants/{tenant_id}` | Get any tenant | — | `Tenant` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PATCH /v1/admin/tenants/{tenant_id}` | Change trust tier, status (`suspended` and `deleting` need a reason) or data residency | `TenantAdminUpdate` | `Tenant` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/quotas` | Quota policies; filters `scope`, `subject_id` | — | `Page[QuotaPolicy]` | 200 | `FORBIDDEN` |
| `PUT /v1/admin/quotas` | Create or replace the policy for one `(scope, subject_id)` | `QuotaPolicyUpsert` | `QuotaPolicy` | 200 | `FORBIDDEN` |
| `DELETE /v1/admin/quotas/{scope}/{subject_id}` | Remove a policy; the tier default applies again (`subject_id` is `*` for the global policy) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/kill-switches` | The kill switches | — | `KillSwitches` | 200 | `FORBIDDEN` |
| `PUT /v1/admin/kill-switches` | Change them; omitted fields keep their value, `reason` is required. `cancel_untrusted_runs` (with `untrusted_execution_enabled=false`) also cancels every unfinished run of uploaded code; without it in-flight runs continue | `KillSwitchUpdate` | `KillSwitches` | 200 | `FORBIDDEN` |
| `GET /v1/admin/executors` | Every executor: caps, health, submit rate, 429s, conformance report | — | `Page[ExecutorHealth]` | 200 | `FORBIDDEN` |
| `GET /v1/admin/executors/{name}` | One executor | — | `ExecutorHealth` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/executors/{name}/evidence` | The conformance and security-suite reports recorded for an executor | — | `ExecutorEvidence` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PUT /v1/admin/executors/{name}/evidence` | Record suite results (audited); shared tiers need both, passing, for the current caps | `ExecutorEvidenceUpdate` | `ExecutorEvidence` | 200 | `SCHEMA_INVALID`, `FORBIDDEN` |
| `GET /v1/admin/usage` | Cost by tenant for a UTC `period` (`day` or `month`, `start` inside it), biggest spender first, and the current spend against the global spend cap (`spend_cap`: always the cap's current window, with `fraction` = spent / cap) | — | `AdminUsageReport` | 200 | `FORBIDDEN` |
| `PUT /v1/admin/executors/{name}` | Enable or disable an executor (audited, `reason` required). Nothing new is leased onto a disabled one; orchestrator reports never re-enable it | `ExecutorUpdate` | `ExecutorHealth` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/audit` | The platform audit log; filters `tenant_id`, `project_id`, `actor_id`, `action`, `outcome`, `since`, `until` | — | `Page[AuditEvent]` | 200 | `FORBIDDEN` |

#### Browser session (backend-for-frontend)

These routes are not versioned and live at the root, next to the web app.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /auth/login` | Start sign-in. Redirects to the identity provider with `response_type=code`, a PKCE `code_challenge` (S256), `state` and `nonce`. `?return_to=` must be a same-origin path; anything else becomes `/`. | — | — | 302 | — |
| `GET /auth/callback` | Check `state`, exchange the code with the PKCE verifier, verify the ID token (issuer, audience, `nonce`, expiry), set the session cookie and redirect to `return_to` | — | — | 302 | `UNAUTHENTICATED` |
| `POST /auth/logout` | End the session and clear the cookies. Works without a valid session; with one it needs `X-CSRF-Token`. | — | — | 204 | — |
| `GET /auth/session` | Whether the browser is signed in, as whom and until when. `authenticated: false` is a 200, not a 401. | — | `SessionInfo` | 200 | — |

The login transaction (state, nonce, PKCE verifier) is held by the server for 10 minutes and
referenced by a `__Host-mapi_login` cookie with `SameSite=Lax`, because the identity
provider's redirect back is a cross-site navigation that would drop a `Strict` cookie. Sessions
expire after 2 hours idle or 12 hours in total. Every state-changing API route takes JSON,
which a cross-site form cannot send without a CORS preflight, and the session cookie is
`SameSite=Strict`, so cross-site requests never carry it.

On top of that, every **cookie-authenticated** `POST`, `PUT`, `PATCH` and `DELETE` (including
`POST /auth/logout` when a session is live) must echo the session's CSRF token in an
`X-CSRF-Token` header, and is refused (`403 FORBIDDEN`) when its `Origin` is neither the web
app nor the API or it says `Sec-Fetch-Site: cross-site`. The web app reads the token from the
`__Host-mapi_csrf` cookie (readable, `SameSite=Strict`) or from the `X-CSRF-Token` header of
`GET /auth/session`. Requests with an API token need no CSRF token.

#### Internal

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /internal/events` | Pub/Sub push of run events from the Cloud Logging sink | `PubSubPushEnvelope` | Ingest result | 200 | `UNAUTHENTICATED`, `SCHEMA_INVALID` |

The push envelope is `{message: {data, attributes, messageId, publishTime}, subscription,
deliveryAttempt}`. `message.data` is a base64 Cloud Logging entry whose `jsonPayload.mapi_event`
is one HMAC-signed run event (see [Run protocol](run-protocol.md)). The request must carry
`Authorization: Bearer <Google-signed ID token>`; after verifying the signature, the server
checks the claims with `mapi_spec.auth.check_push_claims`: issuer `https://accounts.google.com`,
the configured audience, the push service account's verified e-mail, and expiry. Events that
fail their HMAC are acknowledged with 200 (redelivery would not help) and audited as
`event.rejected`.

### Quotas and rate limits on every endpoint

Requests and submissions are checked against three levels of quota: token, tenant and global
(see [Quotas and cost](../concepts/quotas-and-cost.md)).

- A request over a requests-per-minute limit returns **`429 RATE_LIMITED`** with a
  `Retry-After` header. Retry after that many seconds.
- A submission that would exceed a compute, concurrency, fan-out, resource-class, storage or
  egress limit returns **`429 QUOTA_EXCEEDED`**. `Retry-After` is set when the limit resets with
  a window (the UTC day or month). The problem's `detail` names the level and the limit.
- The platform kill switches (uploaded code paused, global spend cap reached) also return
  `QUOTA_EXCEEDED`, at the global level.

## Guided mode and integrations <span class="mapi-badge">v0.4</span> { #guided-and-integrations }

v0.4 (milestone M3) adds the contracts for Guided mode and for connecting other applications:
guided templates and drafts, business priors and lift tests, what-if scenarios, endpoints and
aliases, code snippets, webhooks, saved and rendered plots with embed links, and the trust
lifecycle (sign-up, verification, tenant deletion and export). The endpoint table matches
`mapi_spec.api.M3_ENDPOINTS`; `mapi_spec.api.ROUTE_ACCESS` gives each operation its scope and
permission, and `mapi_spec.api.ENDPOINTS` is the whole v0.4 surface (M1, M2 and M3; the
server's route-table test checks that it serves every row). The ideas behind them are
explained in [Guided mode](../concepts/guided-mode.md) and
[Integrations](../concepts/integrations.md).

Project-scoped M3 routes take the project from `X-Mapi-Project`, like every v0.3 route.

### Guided mode

`GET /v1/guided/templates` lists the built-in templates (`GuidedTemplate`): plain-language
questions, the built-in plugin they target, a `ModelSpec` template with `{{placeholder}}`
strings, default settings, the scorecard checks that apply and `x-ui-level` hints. Role
detection (`POST /v1/guided/detect-roles`) ranks the columns of an upload for every question
from `ColumnProfile`s computed in the browser (or from a stored dataset version: its ingest's
`columns.json`, else its schema and summary) and returns `RoleSuggestions`, each candidate
with a confidence and a `reason_plain`. A draft (`POST /v1/guided/drafts`) turns
`GuidedAnswers` into an `IngestMapping` for the upload, a `ModelSpec` document and its YAML
("View as YAML") without storing anything, with the static validator's findings in `issues`;
save it with `POST /v1/specs`.
Business priors become
model-space priors in a sandboxed `translate_priors` run (`PriorTranslationRequest` →
`PriorTranslation`, whose `result` is a `PriorTranslationResult` with a prior-predictive
preview in business units). `POST /v1/guided/requests:compile` turns a what-if scenario, a
forecast or an intervention preset (`GuidedCompile`) into the `ScoreRequest` it stands for, so
"Run exact" submits a real score run. The persona lock is `Project.guided_locked`, changed
with `PATCH /v1/tenants/{tenant_id}/projects/{project_id}` (`ProjectUpdate.guided_locked`);
in a locked project, `POST /v1/specs` and `POST /v1/specs/{spec_id}/revisions` by members
below admin answer `403 FORBIDDEN` for anything but Guided specs that keep their template's
advanced and hidden fields. Served in this release: the templates, role detection, drafts
and the lock (see
[From a spreadsheet to a Guided draft](../guides/guided-data.md)).

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/guided/templates` | Guided templates (geo marketing mix, weekly forecast, demand by store, choice) | — | `Page[GuidedTemplate]` | 200 | — |
| `GET /v1/guided/templates/{template_id}` | One template: questions, spec template, scorecard checks, UI levels | — | `GuidedTemplate` | 200 | `NOT_FOUND` |
| `POST /v1/guided/detect-roles` | Suggest a column for every question from column profiles | `DetectRolesRequest` | `RoleSuggestions` | 200 | `SCHEMA_INVALID`, `NOT_FOUND` |
| `POST /v1/guided/drafts` | Answers → ingest mapping + ModelSpec draft (+ YAML); stores nothing | `GuidedAnswers` | `GuidedDraft` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `GUIDED_ANSWERS_INVALID` |
| `POST /v1/guided/requests:compile` | What-if scenario, forecast or preset → the ScoreRequest it stands for ('Run exact') | `GuidedCompile` | `ScoreRequest` | 200 | `SCHEMA_INVALID`, `MISSING_FUTURE_COVARIATE` |
| `POST /v1/guided/prior-translations` | Business priors → model-space priors + prior predictive (a translate_priors run) | `PriorTranslationRequest` | `PriorTranslation` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `BUSINESS_PRIORS_INVALID`, `CALIBRATION_UNSUPPORTED`, `QUOTA_EXCEEDED` |
| `GET /v1/guided/prior-translations/{run_id}` | The translation run and, once done, its priors and preview | — | `PriorTranslation` | 200 | `NOT_FOUND` |

### Endpoints and aliases

An endpoint is a stable alias (`endpoint://<project>/<alias>`) for one model version. `PUT`
promotes a version: the promotion gate (`PromotionGateDecision`) allows a passing scorecard,
asks for `override: true` with a `reason` from a signed-in project admin when it warns
(`409 OVERRIDE_REQUIRED`; the override is audited as `endpoint.promote_override`), and blocks
a failing one (`409 PROMOTION_BLOCKED`). The endpoint's `EvaluationPolicy` requires
`evaluate_then_refit` fits by default and blocks input-schema changes that would break callers.
`POST …/score` takes an `EndpointScoreCall` (a score request without `model`, whose `data` and
`scenario` may be inline XrJSON datasets up to 5 MB) and answers like `…:score` on a model
version: `Prefer: wait=60` gives `200` with the result when it is done in time, otherwise
`202` with the run. A `score:<alias>` token may read the endpoint, its schema, history and
snippets, and score it; `endpoints:promote` tokens may promote and roll back but never
override the gate.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/endpoints` | Endpoints of the project | — | `Page[Endpoint]` | 200 | — |
| `GET /v1/endpoints/{alias}` | An endpoint and the version it serves | — | `Endpoint` | 200 | `NOT_FOUND` |
| `PUT /v1/endpoints/{alias}` | Create or promote (gated on the scorecard; override = admin + reason, audited) | `EndpointPromote` | `Endpoint` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `PROMOTION_BLOCKED`, `OVERRIDE_REQUIRED`, `NOT_VALIDATED` |
| `DELETE /v1/endpoints/{alias}` | Delete the alias (model versions stay) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/endpoints/{alias}/history` | Promotions and rollbacks, newest first | — | `Page[EndpointRevision]` | 200 | `NOT_FOUND` |
| `POST /v1/endpoints/{alias}/score` | Score the current version (Prefer: wait=N → 200 if done, else 202 with the run) | `EndpointScoreCall` | `ScoreCallResult` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `MODE_NOT_SUPPORTED`, `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT`, `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`, `CAPABILITY_UNSUPPORTED`, `RECURSIVE_UNSUPPORTED`, `QUOTA_EXCEEDED`, `PAYLOAD_TOO_LARGE` |
| `GET /v1/endpoints/{alias}/schema` | Input schema of the current version and an example payload | — | `EndpointSchema` | 200 | `NOT_FOUND` |
| `POST /v1/endpoints/{alias}/validate` | Static checks of a score call without running it | `EndpointScoreCall` | `EndpointValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `PAYLOAD_TOO_LARGE` |
| `POST /v1/endpoints/{alias}/rollback` | Point the alias back at an earlier revision's version (audited) | `EndpointRollback` | `Endpoint` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `NOT_VALIDATED` |
| `GET /v1/endpoints/{alias}/snippets` | Python, TypeScript, curl, R, Apps Script and notebook code for the endpoint | — | `SnippetSet` | 200 | `NOT_FOUND` |

### Webhooks

`POST /v1/webhooks` returns the signing secret once (`WebhookCreated`). Deliveries carry a
`WebhookEnvelope` and the header `Mapi-Signature: t=<unix>,v1=<hex>`; see
[Integrations](../concepts/integrations.md#webhooks) for verification and the SSRF policy.
Managing webhooks needs a project admin or a `webhooks:manage` token.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/webhooks` | Webhooks of the project | — | `Page[Webhook]` | 200 | `FORBIDDEN` |
| `POST /v1/webhooks` | Add a webhook (the secret is shown once) | `WebhookCreate` | `WebhookCreated` | 201 | `SCHEMA_INVALID`, `FORBIDDEN`, `WEBHOOK_URL_BLOCKED` |
| `GET /v1/webhooks/{webhook_id}` | A webhook (never its secret) | — | `Webhook` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PATCH /v1/webhooks/{webhook_id}` | Change URL, events or active | `WebhookUpdate` | `Webhook` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `WEBHOOK_URL_BLOCKED` |
| `DELETE /v1/webhooks/{webhook_id}` | Delete a webhook and its delivery log | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/webhooks/{webhook_id}:rotate-secret` | New secret (shown once); the old one signs too for 24 h | — | `WebhookCreated` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/webhooks/{webhook_id}/deliveries` | Delivery log, newest first (filter: status) | — | `Page[WebhookDelivery]` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/webhooks/{webhook_id}/deliveries/{delivery_id}:replay` | Send an event again (a new delivery, replay_of set) | — | `WebhookDelivery` | 202 | `NOT_FOUND`, `FORBIDDEN`, `WEBHOOK_URL_BLOCKED` |

### Saved plots, rendering and embeds

A `SavedPlot` keeps a model version's plot or a user-authored `PlotSpec`. Rendering
(`PlotRenderRequest` → `PlotRender`) produces PNG, SVG or PDF in the sandbox. Embed links
(`PlotEmbedCreate` → `PlotEmbedCreated`, listed as `PlotEmbed`) are tokenised, read-only and
expire; only plots of platform recipes can be embedded (`422 EMBED_NOT_ALLOWED`). The public
`GET /v1/embeds/{embed_token}` returns an `EmbedView` with every dataset inline, or
`410 LINK_EXPIRED`; it reads no credential and answers cross-origin reads from the plot
sandbox origin only. See [Sharing plots](../guides/sharing-plots.md).

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/plots` | Save a model version's plot or a user-authored PlotSpec | `SavedPlotCreate` | `SavedPlot` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `PLOT_UNSAFE` |
| `GET /v1/plots` | Saved plots | — | `Page[SavedPlot]` | 200 | — |
| `GET /v1/plots/{plot_id}` | A saved plot with its sanitized spec | — | `SavedPlot` | 200 | `NOT_FOUND` |
| `DELETE /v1/plots/{plot_id}` | Delete a saved plot (revokes its embeds) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/plots:render` | PNG, SVG or PDF in the sandbox (Prefer: wait=N → 200 if done, else 202) | `PlotRenderRequest` | `PlotRender` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `PLOT_UNSAFE`, `MODE_NOT_SUPPORTED`, `QUOTA_EXCEEDED` |
| `GET /v1/plots/renders/{run_id}` | A render run and links to its files | — | `PlotRender` | 200 | `NOT_FOUND` |
| `POST /v1/plots/{plot_id}/embeds` | Tokenised read-only link (platform recipes only; shown once) | `PlotEmbedCreate` | `PlotEmbedCreated` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `EMBED_NOT_ALLOWED` |
| `GET /v1/plots/{plot_id}/embeds` | Embed links of a plot | — | `Page[PlotEmbed]` | 200 | `NOT_FOUND` |
| `DELETE /v1/plots/{plot_id}/embeds/{embed_id}` | Revoke an embed link (idempotent) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/embeds/{embed_token}` | Public: the embedded plot with inline data (410 when expired or revoked) | — | `EmbedView` | 200 | `NOT_FOUND`, `LINK_EXPIRED`, `RATE_LIMITED` |

### Sign-up, verification, deletion and export

Self-service sign-up (`SignupRequest` → `SignupAccepted`) answers the same for new and known
addresses. Verifying the e-mail (`EmailVerify` → `VerificationResult`) creates a
`public`-tier tenant. `VerificationStatus` says whether the tenant may upload code: never for
public tenants before cut line B, and only after identity verification (operators record it
with `VerificationUpdate`). Deletion is scheduled with a grace period during which the tenant
is suspended and can cancel (`TenantDeletionRequest` → `TenantDeletion`, with progress per
store); a GDPR export (`GdprExportRequest` → `GdprExport`) packages everything the tenant
stored.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /auth/signup` | Self-service sign-up (same answer for new and known addresses) | `SignupRequest` | `SignupAccepted` | 202 | `SCHEMA_INVALID`, `EMAIL_NOT_ALLOWED`, `TERMS_NOT_ACCEPTED`, `RATE_LIMITED` |
| `POST /auth/verify-email` | Verify the address; creates a public-tier tenant | `EmailVerify` | `VerificationResult` | 200 | `SCHEMA_INVALID`, `LINK_EXPIRED`, `RATE_LIMITED` |
| `GET /v1/tenants/{tenant_id}/verification` | E-mail and identity verification, and whether code upload is allowed | — | `VerificationStatus` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PUT /v1/admin/tenants/{tenant_id}/verification` | Record identity verification (operators; audited) | `VerificationUpdate` | `VerificationStatus` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/tenants/{tenant_id}/deletion` | Schedule deletion of the whole tenant (grace period, then purge) | `TenantDeletionRequest` | `TenantDeletion` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `GET /v1/tenants/{tenant_id}/deletion` | Deletion state and progress per store | — | `TenantDeletion` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `DELETE /v1/tenants/{tenant_id}/deletion` | Cancel a scheduled deletion (within the grace period) | — | `TenantDeletion` | 200 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `POST /v1/tenants/{tenant_id}/exports` | GDPR export of everything the tenant stored | `GdprExportRequest` | `GdprExport` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `QUOTA_EXCEEDED` |
| `GET /v1/tenants/{tenant_id}/exports` | The tenant's GDPR exports, newest first (`GET` one for its download link) | — | `Page[GdprExport]` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/tenants/{tenant_id}/exports/{export_id}` | An export and, once ready, its download link | — | `GdprExport` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/signups` | Sign-ups, newest first; filter by `review` state (the Sybil review queue) or `tenant_id` | — | `Page[SignupReview]` | 200 | `FORBIDDEN` |
| `POST /v1/admin/signups/{signup_id}/review` | Approve or reject a sign-up (a rejection suspends its tenant; audited) | `SignupReviewDecision` | `SignupReview` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `POST /v1/admin/tenants/{tenant_id}/deletion/retry` | Restart a failed tenant purge (operators; audited) | — | `TenantDeletion` | 202 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |

## Workflows, governance, explorers and About cards <span class="mapi-badge">v0.5</span> { #m35 }

v0.5 (milestone M3.5) adds the contracts for automated **workflows** (statistical tests, data
processing, splits, searches and models nested in one versioned DAG), model **governance**
(policies, checks with evidence, validation and approval), the **Data Explorer** and
**Model Explorer**, **budget optimisation** with mmm-framework, and the plain-language
**About cards** with the **Help me choose** chooser. The endpoint table matches
`mapi_spec.api.M35_ENDPOINTS`; `mapi_spec.api.ENDPOINTS` is the whole v0.5 surface
(`V04_ENDPOINTS` + `M35_ENDPOINTS`) and `ROUTE_ACCESS` gives each operation its scope and
permission. The ideas are explained in [Workflows](../concepts/workflows.md),
[Governance](../concepts/governance.md) and
[About cards and choosing](../concepts/about-and-choosing.md).

v0.5 also **removes** an importer route of v0.4, its ingest preset and one snippet language
(see the CHANGELOG): mmm-framework is the only MMM engine. The contract version is `0.2`.

**Permissions.** Workflows are read by viewers, written by editors, run (and scheduled) by
runners and deleted by admins. Project policies are managed by project admins, tenant-wide
policies by tenant admins; everyone in the project reads them. Submitting a model version for
validation needs `run` rights (`validate` on `model`); approving or rejecting needs the
**reviewer** capability (`Membership.capabilities`, granted with `MembershipUpdate`; project
admins hold it implicitly) and is refused to the person who submitted it
(`403 SELF_APPROVAL_FORBIDDEN`). No API token can approve. New token scopes:
`workflows:write`, `workflows:run`, `models:validate` and `policies:manage`.

**Long-running work.** A workflow run, an explore run and an optimize run are runs like any
other (`Idempotency-Key`, the quota checks, cost). `POST …:optimize` honours
`Prefer: wait=N`. Workflow runs stream their node events on
`GET /v1/workflow-runs/{run_id}/events` (SSE with `Last-Event-ID`, event types in
`WorkflowEventType`), and the terminal ones also go to webhooks (`workflow.run.succeeded`,
`workflow.run.failed`), as do validation transitions (`model.validated`, `model.rejected`,
...), `dataset.version.created` and `optimize.completed`.

**Promotion.** When a governance policy that applies to a model says
`require_validated_for_promotion`, `PUT /v1/endpoints/{alias}` refuses a version whose
validation status is not `validated` with `409 NOT_VALIDATED` (no override can pass it).

### Endpoints { #m35-endpoints }

#### About cards and the chooser

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/catalog/abouts` | About cards of catalog items (filter: kind) | — | `Page[CatalogAbout]` | 200 | — |
| `GET /v1/catalog/chooser` | The Help-me-choose questions | — | `ChooserQuestionnaire` | 200 | — |
| `GET /v1/catalog/recommendations` | Ranked recommendations for chooser answers (query: one parameter per question) | — | `ChooserResult` | 200 | `CHOOSER_ANSWER_INVALID` |
| `GET /v1/catalog/workflow-nodes` | Workflow node implementations with ports, parameters and About cards | — | `Page[NodeImplementation]` | 200 | — |
| `GET /v1/catalog/workflow-nodes/{node_id}` | One node implementation | — | `NodeImplementation` | 200 | `NOT_FOUND` |
| `GET /v1/catalog/governance-checks` | The governance check catalog | — | `Page[GovernanceCheckDef]` | 200 | — |

#### Datasets: About and the Data Explorer

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/datasets/{dataset_id}/about` | The owner's description and the generated summary of the latest version | — | `DatasetAbout` | 200 | `NOT_FOUND` |
| `PATCH /v1/datasets/{dataset_id}/about` | Change the owner's description | `DatasetAboutUpdate` | `DatasetAbout` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/datasets/{dataset_id}/versions/{version}/explore` | Compute Data Explorer sections (an explore run) | `DataExploreRequest` | `Exploration` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `EXPLORE_INVALID`, `QUOTA_EXCEEDED` |
| `GET /v1/explorations/{run_id}` | An explore run and, once done, links to its views | — | `Exploration` | 200 | `NOT_FOUND` |

#### Models: the Model Explorer and budget optimisation

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/models/{model_id}/versions/{version}/explore` | Compute Model Explorer sections (an explore run) | `ModelExploreRequest` | `Exploration` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `EXPLORE_INVALID`, `CAPABILITY_UNSUPPORTED`, `QUOTA_EXCEEDED` |
| `POST /v1/models:compare` | Compare several model versions side by side (an explore run: metrics, ranking, Diebold-Mariano tests) | `ModelCompareRequest` | `Exploration` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `EXPLORE_INVALID`, `QUOTA_EXCEEDED` |
| `POST /v1/models/{model_id}/versions/{version}:optimize` | Optimise a budget across channels (Prefer: wait=N -> 200 if done, else 202) | `OptimizeRequest` | `Optimization` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `OPTIMIZE_INFEASIBLE`, `CAPABILITY_UNSUPPORTED`, `QUOTA_EXCEEDED` |
| `GET /v1/optimizations/{run_id}` | An optimize run and, once done, its plan | — | `Optimization` | 200 | `NOT_FOUND` |

#### Workflows

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/workflows` | Workflows of the project | — | `Page[Workflow]` | 200 | — |
| `POST /v1/workflows` | Create a workflow from a document, YAML text or a gallery template | `WorkflowCreate` | `Workflow` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `WORKFLOW_INVALID`, `CONFLICT`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE` |
| `POST /v1/workflows:validate` | Static checks of a workflow document (JSON pointers); stores nothing | `WorkflowValidateRequest` | `WorkflowValidation` | 200 | `SCHEMA_INVALID`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE` |
| `GET /v1/workflows/{workflow_id}` | A workflow | — | `Workflow` | 200 | `NOT_FOUND` |
| `PATCH /v1/workflows/{workflow_id}` | Change the description or archive it | `WorkflowUpdate` | `Workflow` | 200 | `SCHEMA_INVALID`, `NOT_FOUND` |
| `DELETE /v1/workflows/{workflow_id}` | Delete a workflow (its runs, schedules and triggers go with it) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `GET /v1/workflows/{workflow_id}/revisions` | Revisions, newest first | — | `Page[WorkflowRevision]` | 200 | `NOT_FOUND` |
| `POST /v1/workflows/{workflow_id}/revisions` | Save a new revision (validated; errors make it unrunnable) | `WorkflowRevisionCreate` | `WorkflowRevision` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `CONFLICT`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE` |
| `GET /v1/workflows/{workflow_id}/revisions/{revision}` | A revision with its validation | — | `WorkflowRevision` | 200 | `NOT_FOUND` |
| `POST /v1/workflows/{workflow_id}/runs` | Run a revision (a workflow parent run) | `WorkflowRunCreate` | `WorkflowRun` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `WORKFLOW_INVALID`, `WORKFLOW_NOT_RUNNABLE`, `QUOTA_EXCEEDED`, `IDEMPOTENCY_CONFLICT` |
| `GET /v1/workflows/{workflow_id}/runs` | Runs of a workflow, newest first | — | `Page[WorkflowRun]` | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}` | A workflow run with every node, decision and output | — | `WorkflowRun` | 200 | `NOT_FOUND` |
| `POST /v1/workflow-runs/{run_id}:cancel` | Cancel a workflow run and its running children | — | `WorkflowRun` | 202 | `NOT_FOUND`, `CONFLICT` |
| `GET /v1/workflow-runs/{run_id}/events` | Workflow run events (SSE, Last-Event-ID) | — | SSE stream | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}/nodes` | The node runs of a workflow run | — | `Page[NodeRun]` | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}/nodes/{node_id}` | One node's status, result and outputs | — | `NodeRun` | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}/nodes/{node_id}/outputs/{port}` | A signed link to a node output's object | — | `ArtifactLink` | 200 | `NOT_FOUND` |
| `GET /v1/workflows/{workflow_id}/schedules` | Cron schedules | — | `Page[WorkflowSchedule]` | 200 | `NOT_FOUND` |
| `POST /v1/workflows/{workflow_id}/schedules` | Run the workflow on a cron schedule | `ScheduleCreate` | `WorkflowSchedule` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `SCHEDULE_INVALID` |
| `PATCH /v1/workflows/{workflow_id}/schedules/{schedule_id}` | Change or pause a schedule | `ScheduleUpdate` | `WorkflowSchedule` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `SCHEDULE_INVALID` |
| `DELETE /v1/workflows/{workflow_id}/schedules/{schedule_id}` | Delete a schedule | — | — | 204 | `NOT_FOUND` |
| `GET /v1/workflows/{workflow_id}/triggers` | Dataset triggers | — | `Page[WorkflowTrigger]` | 200 | `NOT_FOUND` |
| `POST /v1/workflows/{workflow_id}/triggers` | Run the workflow when a dataset gets a new version | `TriggerCreate` | `WorkflowTrigger` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `SCHEDULE_INVALID` |
| `DELETE /v1/workflows/{workflow_id}/triggers/{trigger_id}` | Delete a trigger | — | — | 204 | `NOT_FOUND` |
| `GET /v1/workflow-templates` | The gallery of workflow templates | — | `Page[WorkflowTemplate]` | 200 | — |
| `GET /v1/workflow-templates/{template_id}` | One gallery template | — | `WorkflowTemplate` | 200 | `NOT_FOUND` |

#### Governance policies

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/governance/policies` | Policies in force in the project (its own and the tenant's) | — | `Page[GovernancePolicy]` | 200 | — |
| `POST /v1/governance/policies` | Add a project policy (version 1) | `GovernancePolicyCreate` | `GovernancePolicy` | 201 | `SCHEMA_INVALID`, `FORBIDDEN`, `POLICY_INVALID` |
| `GET /v1/governance/policies/{policy_id}` | The latest version of a policy | — | `GovernancePolicy` | 200 | `NOT_FOUND` |
| `PUT /v1/governance/policies/{policy_id}` | Save a new version (expected_version guards concurrent edits) | `GovernancePolicyUpdate` | `GovernancePolicy` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `POLICY_INVALID` |
| `DELETE /v1/governance/policies/{policy_id}` | Retire a policy (its versions stay for audit) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/governance/policies/{policy_id}/versions` | Every version of a policy, newest first | — | `Page[GovernancePolicy]` | 200 | `NOT_FOUND` |
| `GET /v1/tenants/{tenant_id}/governance/policies` | Tenant-wide policies (every project's floor) | — | `Page[GovernancePolicy]` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/tenants/{tenant_id}/governance/policies` | Add a tenant-wide policy | `GovernancePolicyCreate` | `GovernancePolicy` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `POLICY_INVALID` |
| `GET /v1/tenants/{tenant_id}/governance/policies/{policy_id}` | A tenant-wide policy | — | `GovernancePolicy` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PUT /v1/tenants/{tenant_id}/governance/policies/{policy_id}` | Save a new version of a tenant-wide policy | `GovernancePolicyUpdate` | `GovernancePolicy` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `POLICY_INVALID` |
| `DELETE /v1/tenants/{tenant_id}/governance/policies/{policy_id}` | Retire a tenant-wide policy | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |

#### Validations, approvals and evidence

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/models/{model_id}/versions/{version}/validations` | Submit a model version: runs the policy checks as a governance workflow | `ValidationRequest` | `ModelValidation` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `VALIDATION_TRANSITION_INVALID`, `POLICY_INVALID`, `QUOTA_EXCEEDED` |
| `GET /v1/models/{model_id}/versions/{version}/validations` | Validations of a model version, newest first | — | `Page[ModelValidation]` | 200 | `NOT_FOUND` |
| `GET /v1/validations` | Validations of the project (filter: status, e.g. the reviewers' queue) | — | `Page[ModelValidation]` | 200 | — |
| `GET /v1/validations/{validation_id}` | A validation: status, results, approvals, history | — | `ModelValidation` | 200 | `NOT_FOUND` |
| `GET /v1/validations/{validation_id}/evidence` | Every check result with its evidence | — | `Page[CheckResult]` | 200 | `NOT_FOUND` |
| `POST /v1/validations/{validation_id}:cancel` | Stop running checks (back to draft) | `ValidationNote` | `ModelValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `VALIDATION_TRANSITION_INVALID` |
| `POST /v1/validations/{validation_id}:resubmit` | Run the checks again after a failure or a rejection | `ValidationNote` | `ModelValidation` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `VALIDATION_TRANSITION_INVALID`, `QUOTA_EXCEEDED` |
| `POST /v1/validations/{validation_id}/approvals` | A reviewer approves or rejects (never the submitter; audited) | `ApprovalCreate` | `ModelValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `SELF_APPROVAL_FORBIDDEN`, `VALIDATION_TRANSITION_INVALID`, `CONFLICT` |
| `POST /v1/validations/{validation_id}:deprecate` | Retire a validated model version (audited with the reason) | `ValidationNote` | `ModelValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `VALIDATION_TRANSITION_INVALID` |
| `POST /v1/validations/{validation_id}/evidence-pack` | The audit export (JSON, and a signed link to the HTML rendering) | `EvidencePackRequest` | `EvidencePack` | 200 | `SCHEMA_INVALID`, `NOT_FOUND` |

## DTO index

All of these models live in `mapi_spec.api` unless noted. Each one has a schema in the
OpenAPI document and in `export_schemas()` as `api-<Name>.json`.

| Area | Models |
|---|---|
| Envelope | `Page[T]`, `ObjectRef`, `ArtifactLink`, `Health`, `HealthCheck`, `Problem` (`mapi_spec`) |
| Projects | `Project` |
| Datasets | `DatasetCreate`, `Dataset`, `UploadCreate`, `UploadSession`, `IngestMapping`, `DimMapping`, `VarMapping`, `DatasetVersionCreate`, `DatasetVersion`, `DatasetVersionStatus` |
| Specs | `SpecCreate`, `SpecDoc`, `SpecRevisionCreate`, `SpecRevision`, `ValidateRequest`, `ValidateResponse`, `StageSchema` |
| Runs | `RunCreate`, `RunRetry`, `Run`, `RunCost`, `ProgressSnapshot`, `RunEventView`, `LogLine`, `SseEventName` |
| Models | `Model`, `ModelVersion`, `Scorecard`, `ScorecardItem`, `Lineage`, `ScoreCall`, `ScoreCallResult`, `BundleManifest` (`mapi_spec.run`) |
| Plots and catalog | `PlotListItem`, `PlotSpec` (`mapi_spec`), `PlotValidateResponse`, `CatalogResponse`, `ModelCatalogEntry`, `StepCatalogEntry`, `PlotCatalogEntry` (`mapi_spec`) |
| Identity (v0.3) | `Me`, `MeTenant`, `MeProject`, `Tenant`, `TenantCreate`, `TenantUpdate`, `TenantAdminUpdate`, `ProjectCreate`, `ProjectUpdate`, `Membership`, `MembershipCreate`, `MembershipUpdate`, `Principal` (`mapi_spec.tenancy`) |
| Tokens (v0.3) | `TokenCreate`, `TokenCreated`, `ApiToken` (`mapi_spec.tokens`) |
| Usage and cost (v0.3) | `UsageReport`, `CostTotals`, `RunCost`, `CostLedgerEntry`, `CostEstimate` (`mapi_spec.cost`), `QuotaLimitStatus` (`mapi_spec.quotas`) |
| Operators (v0.3) | `QuotaPolicy`, `QuotaPolicyUpsert`, `QuotaDecision` (`mapi_spec.quotas`), `KillSwitches`, `KillSwitchUpdate`, `ExecutorHealth` (`mapi_spec.admin`), `AdminUsageReport`, `TenantCost`, `GlobalSpend`, `AuditEvent` (`mapi_spec.audit`), `ConformanceReport` (`mapi_spec.conformance_executor`) |
| Session and internal (v0.3) | `SessionInfo`, `PubSubPushEnvelope` (`mapi_spec.auth`) |
| Guided mode (v0.4) | `GuidedTemplate`, `DetectRolesRequest`, `ColumnProfile`, `RoleSuggestions`, `GuidedAnswers`, `GuidedDraft` (`mapi_spec.guided`), `WideVarMapping`, `MeridianColumns`, `MffOptions` (`mapi_spec.ingest`) |
| Calibration and what-if (v0.4) | `PriorTranslationRequest`, `PriorTranslation`, `BusinessPriors`, `LiftTest`, `Calibration`, `PriorTranslationResult` (`mapi_spec.calibration`), `GuidedCompile`, `WhatIfScenario`, `WhatIfResult` (`mapi_spec.whatif`), `ResponseGridView` (`mapi_spec.views`), `ScoreRequest` (`mapi_spec`) |
| Endpoints (v0.4) | `Endpoint`, `EndpointPromote`, `EndpointRevision`, `EndpointRollback`, `EndpointScoreCall`, `EndpointSchema`, `EndpointValidation`, `EvaluationPolicy` (`mapi_spec.endpoints`), `PromotionGateDecision` (`mapi_spec.scorecard`), `Snippet`, `SnippetSet` (`mapi_spec.snippets`), `XrJsonDataset` (`mapi_spec.xrjson`) |
| Webhooks (v0.4) | `Webhook`, `WebhookCreate`, `WebhookCreated`, `WebhookUpdate`, `WebhookDelivery`, `WebhookEnvelope` (`mapi_spec.webhooks`) |
| Plots (v0.4) | `SavedPlot`, `SavedPlotCreate`, `PlotRenderRequest`, `PlotRender`, `PlotEmbed`, `PlotEmbedCreate`, `PlotEmbedCreated`, `EmbedView` (`mapi_spec.sharing`) |
| Trust lifecycle (v0.4) | `SignupRequest`, `SignupAccepted`, `EmailVerify`, `VerificationResult`, `VerificationStatus`, `VerificationUpdate`, `SignupReview`, `SignupReviewDecision`, `TenantDeletionRequest`, `TenantDeletion`, `GdprExportRequest`, `GdprExport` (`mapi_spec.trust`) |
| About cards (v0.5) | `About`, `CatalogAbout`, `ChooserQuestionnaire`, `ChooserResult`, `DatasetAbout`, `DatasetAboutUpdate` (`mapi_spec.about`) |
| Workflows (v0.5) | `Workflow`, `WorkflowCreate`, `WorkflowUpdate`, `WorkflowRevision`, `WorkflowRevisionCreate`, `WorkflowValidateRequest`, `WorkflowValidation`, `WorkflowRun`, `WorkflowRunCreate`, `NodeRun`, `ScheduleCreate`, `ScheduleUpdate`, `WorkflowSchedule`, `TriggerCreate`, `WorkflowTrigger` (`mapi_spec.workflow`), `NodeImplementation` (`mapi_spec.workflow_nodes`), `WorkflowTemplate` (`mapi_spec.workflow_templates`) |
| Governance (v0.5) | `GovernanceCheckDef`, `GovernancePolicy`, `GovernancePolicyCreate`, `GovernancePolicyUpdate`, `ValidationRequest`, `ValidationNote`, `ModelValidation`, `CheckResult`, `ApprovalCreate`, `EvidencePackRequest`, `EvidencePack` (`mapi_spec.governance`) |
| Explorers and optimisation (v0.5) | `DataExploreRequest`, `ModelExploreRequest`, `ModelCompareRequest` (`mapi_spec.explore`), `Exploration`, `ExplorerIndex` (`mapi_spec.views`), `OptimizeRequest`, `OptimizeResult` (`mapi_spec.optimize`), `Optimization` |
