# HTTP API (v1) <span class="mapi-badge">v0.2</span>

This page describes the platform API of v0.2 (milestone M1, the local vertical slice). Each
request body and each response body is a Pydantic model in
[`mapi_spec.api`](api/spec.md#rest-dtos). The server serves an OpenAPI 3.1 document that it
makes from those models. Each model has examples. The endpoint table on this page agrees with
`mapi_spec.api.M1_ENDPOINTS`. If the two are different, a test fails.

| See also | |
|---|---|
| [OpenAPI explorer](openapi.md) | The OpenAPI 3.1 document, which you can browse, with all schemas and examples ([full screen](openapi-viewer.html)) |
| [HTTP API guide](../guides/http-api.md) | The main flow with `curl` and `jq`: upload, validate, fit, follow, score |
| [Python client](../guides/python-client.md) | The same flow with `mapi_client` |

!!! info "M1 is single-tenant"
    A local installation has one tenant and one project. Their names are `default`. Each
    resource has a `tenant_id` and a `project_id`, and each storage key starts with
    `t/{tenant}/`. Thus, the multi-tenancy of v0.3 adds row-level security and does not change
    the schema. M1 has no authentication. The API listens only on `127.0.0.1`.
    [Multi-tenant API](#multi-tenant-api) describes the tenants, sign-in, tokens and quotas
    that v0.3 adds.

## Conventions

| Topic | Rule |
|---|---|
| Base URL | `http://127.0.0.1:8000` locally. Resource paths start with `/v1`. |
| Bodies | `application/json` (UTF-8). The spec text is a string field (`yaml`) in the JSON. |
| Unknown fields | Request bodies **reject** unknown fields (`SCHEMA_INVALID`). Clients **ignore** unknown response fields. Thus, the server can add fields and not make a new version. |
| Timestamps | RFC 3339, with a timezone, in UTC (`2026-10-15T09:30:00Z`). |
| Ids | `<prefix>_<26 lowercase Crockford base32>`, with the ULID layout. You can sort them by the time of creation (`run_01jaf3q2x8m9v7k5h4g3f2e1d0`). The prefixes are `ds`, `upl`, `spc`, `run`, `mdl` and `mv`. Projects and tenants use slugs (`default`). |
| Durations | `"45m"`, `"1h30m"`, `"90s"`. |
| Actions | Actions that are not CRUD use a colon suffix: `POST /v1/runs/{run_id}:cancel`. |
| Untrusted text | Log lines, tracebacks and plugin messages occur only in `untrusted_text` or `LogLine.line`. Render them as text. Do not render them as HTML or markdown. |

### Errors

Each error is an [RFC 7807](https://www.rfc-editor.org/rfc/rfc7807) problem document
(`application/problem+json`, model `mapi_spec.Problem`). It has a stable `code`:

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

- Incorrect request bodies, path parameters and query parameters return **422
  `SCHEMA_INVALID`**. Each problem goes in `errors[]`, with an RFC 6901 `pointer` into the
  request (`/body/kind`, `/query/limit`).
- All endpoints can also return `RATE_LIMITED` (429), `INTERNAL_ERROR` (500) and
  `INFRA_ERROR` (503). The table does not show these codes again.
- [Error codes](generated/error-codes.md) has the full list of codes and their HTTP statuses.

### Pagination

List endpoints return `Page[T]`:

```json
{"items": [ … ], "next_cursor": "eyJpZCI6InJ1bl8wMWphZiJ9"}
```

- To get the next page, send `?limit=` (default 50, maximum 200) and `?cursor=<next_cursor>`.
  A `null` cursor tells you that the current page is the last page.
- Cursors are opaque. A cursor is valid only for the same list and the same filters.
- The items are in order of id, with the newest item first.

### Idempotency

`POST /v1/runs`, `…:score`, `…:retry`, `…:dry-run` and `POST …/versions` (ingest) accept an
`Idempotency-Key` header. The key has 1–255 printable ASCII characters. Its scope is the
project, and the server keeps it for 24 hours:

| Situation | Response |
|---|---|
| First request with the key | The usual response, for example `201 Created` with the `Run` |
| Same key, same body (the bytes are equal after canonical JSON) | **`200 OK`** with the resource that the first request made. The server does not make a second run. |
| Same key, different body | **`409 IDEMPOTENCY_CONFLICT`** |
| Same key while the first request is in progress | `409 CONFLICT`. Try again later. |

Clients must make one key for each user intent, for example a UUIDv4 when the user clicks
**Fit**. They must use that key again on each network retry.

### Long-running work and `Prefer: wait`

Runs are asynchronous. When you make a run, the response gives the `Run` immediately, in the
state `created` or `queued`. `POST /v1/models/{model_id}/versions/{version}:score` also
obeys the [RFC 7240](https://www.rfc-editor.org/rfc/rfc7240) header `Prefer: wait=N`. The
maximum value of N is 60 seconds:

- If the score run gets to a terminal state in N seconds, the response is **200** with
  `ScoreCallResult{done: true, run, outputs: [ArtifactLink…]}`.
- If not, the response is **202** with `done: false` and the run. Follow the run with
  `GET /v1/runs/{run_id}` or with its event stream.
- The response header `Preference-Applied: wait=N` gives the wait time that the server used.

### Forecasts with actuals

A `forecast` request (on a model version or on an endpoint) can send **actuals**. Actuals are
target values for periods after the fit, or new values for the last periods of the fit. The
request can also send the **periods to forecast**, with an empty target. The model uses its
fitted parameters to update its state with the actuals. Then it forecasts from the last
actual. The model does not estimate its parameters again.
[Forecasts with actuals](../concepts/forecasting-with-actuals.md) gives the rules, the plugin
hook and examples.

- The model forecasts exactly the periods after the last actual that have an empty target.
  When the payload ends with an actual, the model adds `horizon.periods` periods after it.
- `ScoreCallResult.summary.conditioning` (`ForecastConditioning`) tells what the model used
  to condition each forecast. It has `fit_end`, `conditioned`, `n_actuals`,
  `actuals_through`, `revised`, `horizon_source` (`rows` or `horizon`), details for each
  series and a plain `message`.
- A model without `capabilities.conditions_on_actuals` refuses actuals with
  **422 `ACTUALS_NOT_SUPPORTED`**. The suggested action is to fit again with the new data, or
  to keep the target empty.
- Gaps that the model cannot filter, and actuals without their drivers, give
  **422 `ACTUALS_INVALID`**.
- `EndpointSchema.actuals_example` is a forecast call that you can use immediately. It has
  inline data for the periods after the fit, and all values are empty. Endpoints have this
  example only when their model conditions on actuals.

## Run events (server-sent events)

`GET /v1/runs/{run_id}/events` has two representations:

- **`Accept: text/event-stream`**: a live SSE stream.
- **All other `Accept` values**: a JSON `Page[RunEventView]` of the persisted events. To read
  the events after a given id, send `?after=<id>`.

The stream sends these frames, in this order:

1. `event: snapshot` with the full `Run` JSON. It is the first frame on each connection. It
   has no `id:`.
2. A replay of the persisted events that have an `id` greater than the request header
   `Last-Event-ID`. If the request has no header, the replay starts from the start. Each
   replayed frame has `id: <RunEventView.id>`. The ids increase for each run. Thus, a client
   that connects again continues exactly where it stopped. This is also true after the
   60-minute request limit on Cloud Run.
3. Live frames when the events occur: `state`, `progress`, `log`, `diagnostic`, `warning`,
   `checkpoint`, `metric`, `complete` and `error`. The `data:` of each frame is one line of
   compact JSON (`RunEventView`).
4. `event: end` when the run is terminal (data: the final `Run`). Then the server closes the
   stream. A client that gets `end` must not connect again.

When the state of the run changes, the server also sends a new `event: snapshot` (no `id:`).
Thus, a client can show the latest `Run` again and does not have to assemble it from events.
The server does *not* persist `progress` and `log` frames. It sends them live without an
`id:` (data: `{run_id,
attempt, seq, ts, type, payload}`) and does not replay them. `Last-Event-ID` applies only to
persisted events.

Between events, the server writes a heartbeat comment (`: keepalive`) each 15 seconds. The
first frame also has `retry: 3000`.

An open stream does a check of the credential of its caller again each minute
(`MAPI_SSE_REAUTH_S`). The server closes the stream without `end` in these conditions:

- The token is revoked or expired.
- The session has ended.
- The caller does not have access to the project now.

Then the new connection gets `401` or `403`. Clients must stop at that point and not try
again.

The server combines progress frames into a maximum of one frame each 2–5 seconds. The server
persists only state changes, warnings, diagnostics, checkpoints, metrics and terminal events.
Only these events can replay. A client that connects again gets the latest progress from the
`snapshot` frame. To get the log lines in pages, use `GET /v1/runs/{run_id}/logs`.

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

The columns: **Request** and **Response** are DTO names in `mapi_spec.api`, but some are
catalog or plot models from `mapi_spec`. **Errors** lists the codes that are special to the
endpoint. This table does not show the common codes from the [Errors](#errors) section again.

### Health

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /healthz` | The process is live | — | `Health` | 200 | — |
| `GET /readyz` | The API can connect to Postgres and the object store. If not, it sends 503 with the same body | — | `Health` | 200 | — |

### Projects and catalog

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/projects` | List the projects | — | `Page[Project]` | 200 | — |
| `GET /v1/projects/{project_id}` | Get a project | — | `Project` | 200 | `NOT_FOUND` |
| `GET /v1/catalog` | The full catalog with its digest | — | `CatalogResponse` | 200 | — |
| `GET /v1/catalog/models` | Model plugins | — | `Page[ModelCatalogEntry]` | 200 | — |
| `GET /v1/catalog/models/{model_id}/{version}` | One plugin version (manifest, sanitized schemas, schema template) | — | `ModelCatalogEntry` | 200 | `NOT_FOUND` |
| `GET /v1/catalog/steps` | Steps | — | `Page[StepCatalogEntry]` | 200 | — |
| `GET /v1/catalog/plots` | Plot recipes | — | `Page[PlotCatalogEntry]` | 200 | — |
| `GET /v1/catalog/plots/{plot_id}/{version}/example` | The recipe drawn on synthetic example data (datasets inline), made by the describe run | — | `PlotSpec` | 200 | `NOT_FOUND` |
| `GET /v1/schemas/{name}` | An exported JSON Schema, for example `modelspec.json`. Monaco and JSON Forms use it | — | JSON Schema | 200 | `NOT_FOUND` |

### Datasets

Data goes into the platform in three steps:

1. Create the dataset.
2. `PUT` the file to the signed URL from an upload session. The URL points into the
   quarantine bucket. The API does not see the bytes.
3. Create a version. A **sandboxed ingest run** parses the upload and validates it. Then it
   writes the content-addressed `data.zarr.zip` with its schema, profile and preview.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/datasets` | Create a dataset | `DatasetCreate` | `Dataset` | 201 | `CONFLICT` (the name is in use) |
| `GET /v1/datasets` | List the datasets | — | `Page[Dataset]` | 200 | — |
| `GET /v1/datasets/{dataset_id}` | Get a dataset | — | `Dataset` | 200 | `NOT_FOUND` |
| `POST /v1/datasets/{dataset_id}/uploads` | Get a signed PUT URL into quarantine | `UploadCreate` | `UploadSession` | 201 | `NOT_FOUND`, `PAYLOAD_TOO_LARGE`, `QUOTA_EXCEEDED` |
| `POST /v1/datasets/{dataset_id}/versions` | Ingest an upload as a new version. This starts an `ingest` run. Accepts `Idempotency-Key`. | `DatasetVersionCreate` | `DatasetVersion` (status `ingesting`) | 202 | `NOT_FOUND`, `CONFLICT` (the upload is not complete, or a version already uses it) |
| `GET /v1/datasets/{dataset_id}/versions` | List the versions | — | `Page[DatasetVersion]` | 200 | `NOT_FOUND` |
| `GET /v1/datasets/{dataset_id}/versions/{version}` | Schema, summary, and references to the profile and the preview | — | `DatasetVersion` | 200 | `NOT_FOUND` |
| `GET /v1/datasets/{dataset_id}/versions/{version}/artifacts/{name}` | Signed URL for `data.zarr.zip`, `schema.json`, `profile.json` or `preview.json` | — | `ArtifactLink` | 200 | `NOT_FOUND` |

### Specs

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/specs:validate` | Synchronous static validation. It returns all issues at one time with JSON pointers. It also returns the schema at each pipeline stage. | `ValidateRequest` | `ValidateResponse` | 200 | `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE`, `SCHEMA_INVALID` |
| `POST /v1/specs` | Create a spec and its revision 1 | `SpecCreate` | `SpecDoc` | 201 | `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE`, `SCHEMA_INVALID`, `CONFLICT` |
| `GET /v1/specs` | List the specs | — | `Page[SpecDoc]` | 200 | — |
| `GET /v1/specs/{spec_id}` | Get a spec with its latest revision | — | `SpecDoc` | 200 | `NOT_FOUND` |
| `POST /v1/specs/{spec_id}/revisions` | Add an immutable revision | `SpecRevisionCreate` | `SpecRevision` | 201 | `NOT_FOUND`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE`, `SCHEMA_INVALID`, `CONFLICT` (the `parent_revision` is not the latest) |
| `GET /v1/specs/{spec_id}/revisions` | List the revisions | — | `Page[SpecRevision]` | 200 | `NOT_FOUND` |
| `GET /v1/specs/{spec_id}/revisions/{revision}` | Get one revision | — | `SpecRevision` | 200 | `NOT_FOUND` |
| `POST /v1/specs/{spec_id}/revisions/{revision}:dry-run` | An explicit sandboxed dry run. It is the same as `POST /v1/runs` with `kind: validate`. Accepts `Idempotency-Key`. | — | `Run` | 202 | `NOT_FOUND`, `QUOTA_EXCEEDED` |

How validation gives its results:

- `POST /v1/specs:validate` returns **200 also when the document is not valid**. The result
  is in `ValidateResponse.valid` and `issues`.
- The API uses the statuses that are not 2xx only for requests that it cannot validate. These
  are YAML that it cannot parse, a document that is too large, and a request body that is not
  correct.
- When you create a spec or a revision, the API also stores revisions that are not valid. They
  have `valid: false` and their `issues`. Thus, the editor can save work that is not complete.
- `POST /v1/runs` refuses revisions that are not valid. It uses the code of the first error.
  Examples: `LEAKAGE_RISK`, `UNSEEN_LEVELS`.

### Runs

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/runs` | Submit a run. It returns 200 on an idempotent replay. Accepts `Idempotency-Key`. | `RunCreate` | `Run` | 201 | `NOT_FOUND`, `IDEMPOTENCY_CONFLICT`, `QUOTA_EXCEEDED`, `MODE_NOT_SUPPORTED`, `LEAKAGE_RISK`, `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT`, `MISSING_FUTURE_COVARIATE`, `RECURSIVE_UNSUPPORTED` |
| `GET /v1/runs` | List the runs (filters: `kind`, `state`, `spec_id`) | — | `Page[Run]` | 200 | — |
| `GET /v1/runs/{run_id}` | Get a run: state, failure class and problem, attempt, progress snapshot and cost. The cost has placeholders in v0.2. From v0.3, the platform meters it | — | `Run` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/events` | An SSE stream or a JSON page of the persisted events (see [Run events](#run-events-server-sent-events)). Accepts `Last-Event-ID`. | — | `Page[RunEventView]` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/logs` | The captured log lines. Filters: `attempt`, `stream` | — | `Page[LogLine]` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/plots` | The plots that a succeeded run rendered (`plots.json`: forecast fans, counterfactual bands). The plot sanitizer does a check of them at each read. The response does not include rejected plots. The list is empty until the run succeeds. | — | `Page[PlotSpec]` | 200 | `NOT_FOUND`, `INTEGRITY_FAILURE` |
| `POST /v1/runs/{run_id}:cancel` | Request cancellation. It is idempotent. It returns the run in `cancelling`, or in its terminal state if the run is already complete. The API does not change a `finalizing` run and returns it as it is, because its execution is already complete. | — | `Run` | 202 | `NOT_FOUND`, `CONFLICT` |
| `POST /v1/runs/{run_id}:resume` | Resume a `partial` run from its checkpoints (new attempt) | — | `Run` | 202 | `NOT_FOUND`, `CONFLICT` (the run is not `partial`) |
| `POST /v1/runs/{run_id}:retry` | Start a **new** run from a failed run. For example, use the next larger resource class after `RESOURCE_EXHAUSTED`. Accepts `Idempotency-Key`. | `RunRetry` | `Run` | 201 | `NOT_FOUND`, `CONFLICT` (the run did not fail, or it is a workflow, workflow node or governance run. For these, start the workflow again or submit the validation again) |

The fields that `RunCreate` needs change with `kind`:

| `kind` | Required | Not permitted |
|---|---|---|
| `fit`, `validate` (sandboxed dry run) | `spec_id`, `revision` | `model_version_id`, `request` |
| `score` | `model_version_id`, `request` (a `ScoreRequest` document) | `spec_id`, `revision` |
| `evaluate`, `derive`, `render` | `model_version_id` (`params` is optional) | `spec_id`, `revision`, `request` |

`POST /v1/datasets/{dataset_id}/versions` creates the `ingest` runs. In v0.2, you cannot
create `describe` runs, `optimize` runs or the fan-out parents (`sweep`, `backtest`, `sbc`,
`campaign`). [Run protocol](run-protocol.md#run-states) lists the run states and the
transitions between them.

### Models and scoring

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/models` | List the models | — | `Page[Model]` | 200 | — |
| `GET /v1/models/{model_id}` | Get a model | — | `Model` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions` | List the versions | — | `Page[ModelVersion]` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions/{version}` | Signed bundle manifest, diagnostics, scorecard, lineage, metrics | — | `ModelVersion` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions/{version}/artifacts/{name}` | Signed URL for a bundle object or a `ui/…` view. `name` can contain `/` | — | `ArtifactLink` | 200 | `NOT_FOUND`, `INTEGRITY_FAILURE` |
| `POST /v1/models/{model_id}/versions/{version}:score` | Score with replayed preprocessing. Accepts `Prefer: wait=N` (200 if the run is complete, if not 202) and `Idempotency-Key`. `inline` holds `data`/`scenario` that you type in (XrJSON, maximum 5 MB each). The API does a check of them against the inputs of the version. | `ScoreCall` | `ScoreCallResult` | 200 | `NOT_FOUND`, `MODE_NOT_SUPPORTED`, `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT`, `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`, `CAPABILITY_UNSUPPORTED`, `RECURSIVE_UNSUPPORTED`, `QUOTA_EXCEEDED`, `ACTUALS_NOT_SUPPORTED`, `ACTUALS_INVALID`, `PAYLOAD_TOO_LARGE` |
| `GET /v1/models/{model_id}/versions/{version}/inputs` | The data that a score call can send. This is the input contract (canonical names, variables that the future needs, fitted coordinates) and the input table. Use `?periods=N` (1-200) for the number of periods after the fit | — | `ModelInputSchema` | 200 | `NOT_FOUND`, `CAPABILITY_UNSUPPORTED` |

The API makes artifact links only after two checks are satisfactory:

1. The authorization check.
2. The verification of the **signed bundle manifest** of the model version. The tenant, model
   version, digests and object generations must all agree.

If the second check fails, the response is `INTEGRITY_FAILURE` and the API gives no URL. Each
link is pinned to an object generation. It expires after 15 minutes. It supports HTTP `Range`
requests.

### Plots

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/models/{model_id}/versions/{version}/plots` | The plots of a model version | — | `Page[PlotListItem]` | 200 | `NOT_FOUND` |
| `GET /v1/models/{model_id}/versions/{version}/plots/{plot_id}` | A sanitized `PlotSpec`. Render it only in the plot-sandbox iframe, which has its own origin. | — | `PlotSpec` | 200 | `NOT_FOUND` |
| `POST /v1/plots:validate` | Run the server-side plot sanitizer on a spec that a user wrote | `PlotSpec` | `PlotValidateResponse` | 200 | `SCHEMA_INVALID` |

## Multi-tenant API <span class="mapi-badge">v0.3</span> { #multi-tenant-api }

v0.3 (milestone M2) adds tenants, projects, members, API tokens, quotas, cost and an operator
console. The models are in these modules:

- `mapi_spec.api`
- `mapi_spec.tenancy`
- `mapi_spec.tokens`
- `mapi_spec.quotas`
- `mapi_spec.cost`
- `mapi_spec.audit`
- `mapi_spec.admin`
- `mapi_spec.auth`

The endpoint table below agrees with `mapi_spec.api.M2_ENDPOINTS`. `mapi_spec.api.ROUTE_ACCESS`
gives each operation its scope and the permission that it checks. This includes the v0.2
operations. If the code, this page and the server are different, tests fail.
[Tenancy and authentication](../concepts/tenancy-and-auth.md) explains roles, tokens and
scopes. [Quotas and cost](../concepts/quotas-and-cost.md) explains limits and prices.

### Authentication

| Caller | Credential | Notes |
|---|---|---|
| Browser (the web app) | Session cookie `__Host-mapi_session` (`HttpOnly; Secure; SameSite=Strict; Path=/`). `GET /auth/callback` sets it | OpenID Connect Authorization Code flow with PKCE (S256) and a `nonce`. The API runs the flow as a backend-for-frontend. The browser does not hold an OAuth token. |
| Programs, SDKs, CLI | `Authorization: Bearer mapi_<prefix>_<secret><checksum>` | An API token. It acts as its owner, with only its scopes, in the one project that it is bound to. |
| Google Pub/Sub | A Google-signed OIDC ID token on `POST /internal/events` | The API does a check of the audience, the issuer and the push service account. No data in the body gives authentication. |

Responses for authentication and authorization:

| Situation | Response |
|---|---|
| No credential, or an expired session, on a route that is not public | `401 UNAUTHENTICATED` |
| A token that is not correctly formed, or a token with a bad checksum | `401 UNAUTHENTICATED`. The API does not show the value in the response. |
| A revoked or expired token | `401 UNAUTHENTICATED` |
| A resource in a project or tenant that the caller cannot see | `404 NOT_FOUND`, the same as for a resource that does not exist. Thus, callers cannot find ids by tests |
| The caller can see the resource, but the role or the token scopes do not permit the action | `403 FORBIDDEN` |
| A token used outside the project that it is bound to | `403 FORBIDDEN` |
| The tenant is `suspended` | Reads work. All other requests return `403 FORBIDDEN`. |

### Selecting the project

Each v0.2 resource (datasets, specs, runs, models, plots) is part of a project. From v0.3, a
request identifies its project in one of these ways. The list order is the order of
precedence:

1. **Path parameters**, on routes under `/v1/tenants/{tenant_id}/projects/{project_id}`.
2. **The `X-Mapi-Project` header**: `X-Mapi-Project: <tenant_id>/<project_id>`, for example
   `X-Mapi-Project: acme/growth`. The API also accepts only the project, for example
   `X-Mapi-Project: growth`, when the tenant is clear. This is when the caller is a member of
   one tenant, or uses a token.
3. **The token binding.** A token always acts in its own project. Thus, token clients do not
   have to send the header. A header that identifies a different project returns
   `403 FORBIDDEN`.
4. **The deployment default**, only in the single-tenant local profile (`default/default`, as
   in v0.2).

If no rule applies, or the path and the header are different, the request fails with
`422 SCHEMA_INVALID`. `mapi_spec.tenancy.resolve_project_scope` implements these rules.

The scope of each v0.2 endpoint (by operation id) and the permission that the API checks:

| Scope | Operations | Permission |
|---|---|---|
| Public | `health`, `ready`, `get_schema` | None |
| Authenticated | `list_projects`, `get_catalog`, `list_catalog_models`, `get_catalog_model`, `list_catalog_steps`, `list_catalog_plots` | All signed-in callers. The lists show only what the caller can see. |
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

In v0.2, the storage keys and rows already had the tenant and the project (`t/{tenant}/…`).
Thus, the change needs no data migration. Postgres enforces the boundary with row-level
security on `tenant_id` in all tables.

### Endpoints

#### Identity, tenants and projects

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/me` | The caller (`Principal`), with their tenants, projects and roles. For a token, the response also includes the token | — | `Me` | 200 | `UNAUTHENTICATED` |
| `GET /v1/tenants` | The tenants that the caller is a member of | — | `Page[Tenant]` | 200 | — |
| `GET /v1/tenants/{tenant_id}` | Get a tenant | — | `Tenant` | 200 | `NOT_FOUND` |
| `PATCH /v1/tenants/{tenant_id}` | Change the name of a tenant (tenant admin) | `TenantUpdate` | `Tenant` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/tenants/{tenant_id}/projects` | The projects of the tenant that the caller can see | — | `Page[Project]` | 200 | `NOT_FOUND` |
| `POST /v1/tenants/{tenant_id}/projects` | Create a project (tenant admin) | `ProjectCreate` | `Project` | 201 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` (the id is in use) |
| `PATCH /v1/tenants/{tenant_id}/projects/{project_id}` | Change the name or the description of a project (project admin) | `ProjectUpdate` | `Project` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `DELETE /v1/tenants/{tenant_id}/projects/{project_id}` | Delete an empty project (tenant admin) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` (the project still has data) |

#### Members

A membership gives one role. A tenant-wide membership gives the role on the tenant and on all
projects in it. A project membership gives the role only on that project. The effective role
in a project is the higher of the two. Add a member by `principal_id`. To invite a person who
did not sign in before, add the member by `email`. At the first sign-in with that verified
e-mail, the person gets the invitation. If you remove or demote the last admin, the API
returns `409 CONFLICT`.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/tenants/{tenant_id}/members` | Tenant-wide members | — | `Page[Membership]` | 200 | `NOT_FOUND` |
| `POST /v1/tenants/{tenant_id}/members` | Give a tenant-wide role (tenant admin) | `MembershipCreate` | `Membership` | 201 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `PATCH /v1/tenants/{tenant_id}/members/{principal_id}` | Change a tenant-wide role | `MembershipUpdate` | `Membership` | 200 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `DELETE /v1/tenants/{tenant_id}/members/{principal_id}` | Remove a tenant-wide role | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `GET /v1/tenants/{tenant_id}/projects/{project_id}/members` | Project members, with the tenant-wide grants | — | `Page[Membership]` | 200 | `NOT_FOUND` |
| `POST /v1/tenants/{tenant_id}/projects/{project_id}/members` | Give a project role (project admin) | `MembershipCreate` | `Membership` | 201 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `PATCH /v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}` | Change a project role | `MembershipUpdate` | `Membership` | 200 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `DELETE /v1/tenants/{tenant_id}/projects/{project_id}/members/{principal_id}` | Remove a project role | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |

#### API tokens

Tokens are part of the project that `X-Mapi-Project` selects. `POST /v1/tokens` returns the
full token **one time**, in `TokenCreated.secret`. The API cannot show it again. Lists show
the redacted form `mapi_<prefix>_…`. Each member can mint, list and revoke their own tokens.
To list the tokens of all members (`?all=true`) or to revoke the token of a different person,
you need `manage` on `token` (project admin).

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/tokens` | The tokens of the caller. Filters: `all`, `status` | — | `Page[ApiToken]` | 200 | — |
| `POST /v1/tokens` | Mint a token with scopes and an expiry. A rate limit and a vCPU-second budget are optional | `TokenCreate` | `TokenCreated` | 201 | `FORBIDDEN` |
| `GET /v1/tokens/{token_id}` | Get a token (the response never includes its secret) | — | `ApiToken` | 200 | `NOT_FOUND` |
| `DELETE /v1/tokens/{token_id}` | Revoke a token. It is idempotent. | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |

#### Usage

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/usage` | The cost and the quota of the selected project for a UTC `period` (`day` or `month`). You can also put them in groups (`group_by`: `resource_class`, `project`, `day`, `token`, `run`) | — | `UsageReport` | 200 | — |
| `GET /v1/tenants/{tenant_id}/usage` | The same data for the full tenant (tenant admin) | — | `UsageReport` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/tenants/{tenant_id}/audit` | The audit log of the tenant (tenant admin). Filters: `project_id`, `actor_id`, `action`, `outcome`, `since`, `until` | — | `Page[AuditEvent]` | 200 | `NOT_FOUND`, `FORBIDDEN` |

Each `Run` now has a `cost` with values (`RunCost`):

- `estimated` is `true` until the platform meters the first attempt of the run.
- `amount_micros` is in `currency` (1 USD = 1,000,000 micros).
- `price_version` identifies the price table that the platform used.

The v0.2 fields `vcpu_seconds`, `gib_seconds` and `usd` keep their meaning.

#### Operators

Operator endpoints need a platform operator. Tenant admins get `403 FORBIDDEN`. Operators see
tenant metadata, usage and audit logs. They do not see tenant content (datasets, specs,
models, plots). The API writes each change to the audit log.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/admin/tenants` | All tenants. Filters: `status`, `trust_tier` | — | `Page[Tenant]` | 200 | `FORBIDDEN` |
| `POST /v1/admin/tenants` | Create a tenant. You can also invite its first admin | `TenantCreate` | `Tenant` | 201 | `FORBIDDEN`, `CONFLICT` |
| `GET /v1/admin/tenants/{tenant_id}` | Get a tenant | — | `Tenant` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PATCH /v1/admin/tenants/{tenant_id}` | Change the trust tier, the status or the data residency. The statuses `suspended` and `deleting` need a reason | `TenantAdminUpdate` | `Tenant` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/quotas` | Quota policies. Filters: `scope`, `subject_id` | — | `Page[QuotaPolicy]` | 200 | `FORBIDDEN` |
| `PUT /v1/admin/quotas` | Create or replace the policy for one `(scope, subject_id)` | `QuotaPolicyUpsert` | `QuotaPolicy` | 200 | `FORBIDDEN` |
| `DELETE /v1/admin/quotas/{scope}/{subject_id}` | Remove a policy. The tier default then applies again. For the global policy, `subject_id` is `*` | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/kill-switches` | The kill switches | — | `KillSwitches` | 200 | `FORBIDDEN` |
| `PUT /v1/admin/kill-switches` | Change the kill switches. Fields that you do not send keep their value. `reason` is necessary. `cancel_untrusted_runs` (with `untrusted_execution_enabled=false`) also cancels all runs of uploaded code that are not complete. Without it, the runs that are in progress continue | `KillSwitchUpdate` | `KillSwitches` | 200 | `FORBIDDEN` |
| `GET /v1/admin/executors` | All executors: caps, health, submit rate, 429s, conformance report | — | `Page[ExecutorHealth]` | 200 | `FORBIDDEN` |
| `GET /v1/admin/executors/{name}` | One executor | — | `ExecutorHealth` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/executors/{name}/evidence` | The reports of the conformance suite and the security suite for an executor | — | `ExecutorEvidence` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PUT /v1/admin/executors/{name}/evidence` | Record the suite results (audited). Shared tiers need the two reports, with a pass, for the current caps | `ExecutorEvidenceUpdate` | `ExecutorEvidence` | 200 | `SCHEMA_INVALID`, `FORBIDDEN` |
| `GET /v1/admin/usage` | The cost for each tenant for a UTC `period` (`day` or `month`, with `start` in it). The tenant that spends most is first. The response also has the current spend against the global spend cap (`spend_cap`). This is always the current window of the cap, with `fraction` = spent / cap | — | `AdminUsageReport` | 200 | `FORBIDDEN` |
| `PUT /v1/admin/executors/{name}` | Enable or disable an executor (audited, `reason` is necessary). The platform does not lease new work to a disabled executor. Orchestrator reports never enable it again | `ExecutorUpdate` | `ExecutorHealth` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/audit` | The platform audit log. Filters: `tenant_id`, `project_id`, `actor_id`, `action`, `outcome`, `since`, `until` | — | `Page[AuditEvent]` | 200 | `FORBIDDEN` |

#### Browser session (backend-for-frontend)

These routes have no version. They are at the root, next to the web app.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /auth/login` | Start the sign-in. It redirects to the identity provider with `response_type=code`, a PKCE `code_challenge` (S256), `state` and `nonce`. `?return_to=` must be a path on the same origin. If it is not, the API uses `/`. | — | — | 302 | — |
| `GET /auth/callback` | Do a check of `state`. Exchange the code with the PKCE verifier. Verify the ID token (issuer, audience, `nonce`, expiry). Set the session cookie and redirect to `return_to` | — | — | 302 | `UNAUTHENTICATED` |
| `POST /auth/logout` | End the session and clear the cookies. It works without a valid session. With a valid session, it needs `X-CSRF-Token`. | — | — | 204 | — |
| `GET /auth/session` | Tells if the browser is signed in, the identity, and the expiry time. `authenticated: false` is a 200, not a 401. | — | `SessionInfo` | 200 | — |

The server keeps the login transaction (state, nonce, PKCE verifier) for 10 minutes. A
`__Host-mapi_login` cookie with `SameSite=Lax` refers to it. The cookie uses this setting because the
redirect back from the identity provider is a cross-site navigation, and that navigation does
not send a `Strict` cookie. Sessions expire after 2 hours with no activity or after 12 hours
in total. Each API route that changes state accepts only JSON. A cross-site form cannot send
JSON without a CORS preflight. The session cookie is `SameSite=Strict`, thus cross-site
requests never send it.

Also, each **cookie-authenticated** `POST`, `PUT`, `PATCH` and `DELETE` must send the CSRF
token of the session in an `X-CSRF-Token` header. This includes `POST /auth/logout` when a
session is live. The API refuses the request (`403 FORBIDDEN`) in these conditions:

- Its `Origin` is not the web app or the API.
- It has `Sec-Fetch-Site: cross-site`.

The web app reads the token from the `__Host-mapi_csrf` cookie (readable, `SameSite=Strict`)
or from the `X-CSRF-Token` header of `GET /auth/session`. Requests with an API token do not
need a CSRF token.

#### Internal

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /internal/events` | Pub/Sub push of run events from the Cloud Logging sink | `PubSubPushEnvelope` | Ingest result | 200 | `UNAUTHENTICATED`, `SCHEMA_INVALID` |

The push envelope is `{message: {data, attributes, messageId, publishTime}, subscription,
deliveryAttempt}`. `message.data` is a base64 Cloud Logging entry. Its `jsonPayload.mapi_event`
is one HMAC-signed run event (see [Run protocol](run-protocol.md)). The request must have
`Authorization: Bearer <Google-signed ID token>`. After the server verifies the signature, it
does a check of the claims with `mapi_spec.auth.check_push_claims`:

- The issuer is `https://accounts.google.com`.
- The audience is the configured audience.
- The verified e-mail is the e-mail of the push service account.
- The token is not expired.

If an event fails its HMAC, the server acknowledges it with 200, because a new delivery
cannot help. The server records it in the audit log as `event.rejected`.

### Quotas and rate limits on every endpoint

The platform does a check of requests and submissions against three levels of quota: token,
tenant and global (see [Quotas and cost](../concepts/quotas-and-cost.md)).

- If a request goes above a requests-per-minute limit, the API returns
  **`429 RATE_LIMITED`** with a `Retry-After` header. Try again after that number of seconds.
- If a submission goes above a compute, concurrency, fan-out, resource-class, storage or
  egress limit, the API returns **`429 QUOTA_EXCEEDED`**. The API sets `Retry-After` when the
  limit resets with a window (the UTC day or month). The `detail` of the problem gives the
  level and the limit.
- The platform kill switches (uploaded code paused, global spend cap reached) also return
  `QUOTA_EXCEEDED`, at the global level.

## Guided mode and integrations <span class="mapi-badge">v0.4</span> { #guided-and-integrations }

v0.4 (milestone M3) adds the contracts for Guided mode and for the connection of other
applications. These contracts are:

- Guided templates and drafts.
- Business priors and lift tests.
- What-if scenarios.
- Endpoints and aliases.
- Code snippets.
- Webhooks.
- Saved and rendered plots, with embed links.
- The trust lifecycle (sign-up, verification, tenant deletion and export).

The endpoint table agrees with `mapi_spec.api.M3_ENDPOINTS`. `mapi_spec.api.ROUTE_ACCESS`
gives the scope and the permission of each operation. `mapi_spec.api.ENDPOINTS` is the full
v0.4 surface (M1, M2 and M3). The route-table test of the server makes sure that the server
serves each row. [Guided mode](../concepts/guided-mode.md) and
[Integrations](../concepts/integrations.md) explain the ideas.

Project-scoped M3 routes get the project from `X-Mapi-Project`, as all v0.3 routes do.

### Guided mode

`GET /v1/guided/templates` lists the built-in templates (`GuidedTemplate`). Each template has
these items:

- Questions in plain language.
- The built-in plugin that the questions are for.
- A `ModelSpec` template with `{{placeholder}}` strings.
- Default settings.
- The scorecard checks that apply.
- `x-ui-level` hints.

Role detection (`POST /v1/guided/detect-roles`) ranks the columns of an upload for each
question. It uses `ColumnProfile`s that the browser calculates. It can also use a stored
dataset version: the `columns.json` of its ingest, or else its schema and summary. It returns
`RoleSuggestions`. Each candidate has a confidence and a `reason_plain`.

A draft (`POST /v1/guided/drafts`) changes `GuidedAnswers` into an `IngestMapping` for the
upload, a `ModelSpec` document and its YAML ("View as YAML"). It does not store anything. The
findings of the static validator are in `issues`. To save the draft, use `POST /v1/specs`.

A sandboxed `translate_priors` run changes business priors into model-space priors
(`PriorTranslationRequest` → `PriorTranslation`). The `result` of the translation is a
`PriorTranslationResult`, with a prior-predictive preview in business units.

`POST /v1/guided/requests:compile` changes a what-if scenario, a forecast or an intervention
preset (`GuidedCompile`) into the equivalent `ScoreRequest`. Thus, "Run exact" sends a real
score run.

The persona lock is `Project.guided_locked`. To change it, use
`PATCH /v1/tenants/{tenant_id}/projects/{project_id}` (`ProjectUpdate.guided_locked`). In a
locked project, members below admin get `403 FORBIDDEN` from `POST /v1/specs` and
`POST /v1/specs/{spec_id}/revisions`. The only exception is Guided specs that keep the
advanced and hidden fields of their template.

This release serves the templates, role detection, drafts and the lock (see
[From a spreadsheet to a Guided draft](../guides/guided-data.md)).

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/guided/templates` | Guided templates (geo marketing mix, weekly forecast, demand by store, choice) | — | `Page[GuidedTemplate]` | 200 | — |
| `GET /v1/guided/templates/{template_id}` | One template: questions, spec template, scorecard checks, UI levels | — | `GuidedTemplate` | 200 | `NOT_FOUND` |
| `POST /v1/guided/detect-roles` | Suggest a column for each question from column profiles | `DetectRolesRequest` | `RoleSuggestions` | 200 | `SCHEMA_INVALID`, `NOT_FOUND` |
| `POST /v1/guided/drafts` | Answers → ingest mapping + ModelSpec draft (+ YAML). It stores nothing | `GuidedAnswers` | `GuidedDraft` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `GUIDED_ANSWERS_INVALID` |
| `POST /v1/guided/requests:compile` | What-if scenario, forecast or preset → the equivalent ScoreRequest ('Run exact') | `GuidedCompile` | `ScoreRequest` | 200 | `SCHEMA_INVALID`, `MISSING_FUTURE_COVARIATE` |
| `POST /v1/guided/prior-translations` | Business priors → model-space priors + prior predictive (a translate_priors run) | `PriorTranslationRequest` | `PriorTranslation` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `BUSINESS_PRIORS_INVALID`, `CALIBRATION_UNSUPPORTED`, `QUOTA_EXCEEDED` |
| `GET /v1/guided/prior-translations/{run_id}` | The translation run. When it is done, also its priors and preview | — | `PriorTranslation` | 200 | `NOT_FOUND` |

### Endpoints and aliases

An endpoint is a stable alias (`endpoint://<project>/<alias>`) for one model version. `PUT`
promotes a version. The promotion gate (`PromotionGateDecision`) does a check of the
scorecard:

- If the scorecard passes, the gate permits the promotion.
- If the scorecard has a warning, the gate asks for `override: true` with a `reason` from a
  signed-in project admin (`409 OVERRIDE_REQUIRED`). The audit log records the override as
  `endpoint.promote_override`.
- If the scorecard fails, the gate blocks the promotion (`409 PROMOTION_BLOCKED`).

By default, the `EvaluationPolicy` of the endpoint needs `evaluate_then_refit` fits. It also
blocks changes to the input schema that would break callers.

`POST …/score` takes an `EndpointScoreCall`. This is a score request without `model`. Its
`data` and `scenario` can be inline XrJSON datasets of up to 5 MB. The answer is the same as
for `…:score` on a model version. With `Prefer: wait=60`, you get `200` with the result if the
run completes in time. If not, you get `202` with the run.

A `score:<alias>` token can read the endpoint, its schema, history and snippets, and can score
it. `endpoints:promote` tokens can promote and roll back, but they cannot override the gate.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/endpoints` | Endpoints of the project | — | `Page[Endpoint]` | 200 | — |
| `GET /v1/endpoints/{alias}` | An endpoint and the version that it serves | — | `Endpoint` | 200 | `NOT_FOUND` |
| `PUT /v1/endpoints/{alias}` | Create or promote. The scorecard is the gate. An override needs an admin and a reason, and goes into the audit log | `EndpointPromote` | `Endpoint` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `PROMOTION_BLOCKED`, `OVERRIDE_REQUIRED`, `NOT_VALIDATED` |
| `DELETE /v1/endpoints/{alias}` | Delete the alias (the model versions stay) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/endpoints/{alias}/history` | Promotions and rollbacks, newest first | — | `Page[EndpointRevision]` | 200 | `NOT_FOUND` |
| `POST /v1/endpoints/{alias}/score` | Score the current version (Prefer: wait=N → 200 if done, else 202 with the run) | `EndpointScoreCall` | `ScoreCallResult` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `MODE_NOT_SUPPORTED`, `UNSEEN_LEVELS`, `COORD_NOT_ALLOWED`, `HORIZON_EXCEEDS_FIT`, `MISSING_FUTURE_COVARIATE`, `NON_CONTIGUOUS_EXTENSION`, `CAPABILITY_UNSUPPORTED`, `RECURSIVE_UNSUPPORTED`, `QUOTA_EXCEEDED`, `PAYLOAD_TOO_LARGE`, `ACTUALS_NOT_SUPPORTED`, `ACTUALS_INVALID` |
| `GET /v1/endpoints/{alias}/schema` | Input schema of the current version and an example payload | — | `EndpointSchema` | 200 | `NOT_FOUND` |
| `POST /v1/endpoints/{alias}/validate` | Static checks of a score call. It does not run the call | `EndpointScoreCall` | `EndpointValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `PAYLOAD_TOO_LARGE` |
| `POST /v1/endpoints/{alias}/rollback` | Set the alias back to the version of an earlier revision (audited) | `EndpointRollback` | `Endpoint` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `NOT_VALIDATED` |
| `GET /v1/endpoints/{alias}/snippets` | Python, TypeScript, curl, R, Apps Script and notebook code for the endpoint | — | `SnippetSet` | 200 | `NOT_FOUND` |

### Webhooks

`POST /v1/webhooks` returns the signing secret one time only (`WebhookCreated`). Each delivery
has a `WebhookEnvelope` and the header `Mapi-Signature: t=<unix>,v1=<hex>`. For verification
and the SSRF policy, see [Integrations](../concepts/integrations.md#webhooks). To manage
webhooks, you must be a project admin or have a `webhooks:manage` token.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/webhooks` | Webhooks of the project | — | `Page[Webhook]` | 200 | `FORBIDDEN` |
| `POST /v1/webhooks` | Add a webhook (the API shows the secret one time only) | `WebhookCreate` | `WebhookCreated` | 201 | `SCHEMA_INVALID`, `FORBIDDEN`, `WEBHOOK_URL_BLOCKED` |
| `GET /v1/webhooks/{webhook_id}` | A webhook (never its secret) | — | `Webhook` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PATCH /v1/webhooks/{webhook_id}` | Change URL, events or active | `WebhookUpdate` | `Webhook` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `WEBHOOK_URL_BLOCKED` |
| `DELETE /v1/webhooks/{webhook_id}` | Delete a webhook and its delivery log | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/webhooks/{webhook_id}:rotate-secret` | New secret (shown one time only). The old secret also signs for 24 h | — | `WebhookCreated` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/webhooks/{webhook_id}/deliveries` | Delivery log, newest first (filter: status) | — | `Page[WebhookDelivery]` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/webhooks/{webhook_id}/deliveries/{delivery_id}:replay` | Send an event again (a new delivery, with replay_of set) | — | `WebhookDelivery` | 202 | `NOT_FOUND`, `FORBIDDEN`, `WEBHOOK_URL_BLOCKED` |

### Saved plots, rendering and embeds

A `SavedPlot` keeps a plot of a model version or a `PlotSpec` that a user wrote. Rendering
(`PlotRenderRequest` → `PlotRender`) makes PNG, SVG or PDF in the sandbox. Embed links
(`PlotEmbedCreate` → `PlotEmbedCreated`, listed as `PlotEmbed`) have tokens, are read-only and
expire. You can embed only plots of platform recipes (`422 EMBED_NOT_ALLOWED`).

The public `GET /v1/embeds/{embed_token}` returns an `EmbedView` with all datasets inline, or
`410 LINK_EXPIRED`. It does not read credentials. It answers cross-origin reads only from the
plot sandbox origin. See [Sharing plots](../guides/sharing-plots.md).

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/plots` | Save a plot of a model version or a PlotSpec that a user wrote | `SavedPlotCreate` | `SavedPlot` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `PLOT_UNSAFE` |
| `GET /v1/plots` | Saved plots | — | `Page[SavedPlot]` | 200 | — |
| `GET /v1/plots/{plot_id}` | A saved plot with its sanitized spec | — | `SavedPlot` | 200 | `NOT_FOUND` |
| `DELETE /v1/plots/{plot_id}` | Delete a saved plot (this revokes its embeds) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/plots:render` | PNG, SVG or PDF in the sandbox (Prefer: wait=N → 200 if done, else 202) | `PlotRenderRequest` | `PlotRender` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `PLOT_UNSAFE`, `MODE_NOT_SUPPORTED`, `QUOTA_EXCEEDED` |
| `GET /v1/plots/renders/{run_id}` | A render run and links to its files | — | `PlotRender` | 200 | `NOT_FOUND` |
| `POST /v1/plots/{plot_id}/embeds` | Read-only link with a token (platform recipes only, shown one time only) | `PlotEmbedCreate` | `PlotEmbedCreated` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `EMBED_NOT_ALLOWED` |
| `GET /v1/plots/{plot_id}/embeds` | Embed links of a plot | — | `Page[PlotEmbed]` | 200 | `NOT_FOUND` |
| `DELETE /v1/plots/{plot_id}/embeds/{embed_id}` | Revoke an embed link (idempotent) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/embeds/{embed_token}` | Public: the embedded plot with inline data (410 when expired or revoked) | — | `EmbedView` | 200 | `NOT_FOUND`, `LINK_EXPIRED`, `RATE_LIMITED` |

### Sign-up, verification, deletion and export

Self-service sign-up (`SignupRequest` → `SignupAccepted`) gives the same answer for new and
known addresses. Verification of the e-mail (`EmailVerify` → `VerificationResult`) creates a
`public`-tier tenant.

`VerificationStatus` tells if the tenant can upload code. Public tenants cannot upload code
before cut line B. After cut line B, they can upload code only after identity verification.
Operators record the identity verification with `VerificationUpdate`.

Deletion starts after a grace period (`TenantDeletionRequest` → `TenantDeletion`, with the
progress for each store). During the grace period, the tenant is suspended and can cancel the
deletion. A GDPR export (`GdprExportRequest` → `GdprExport`) puts all the data that the tenant
stored into a package.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /auth/signup` | Self-service sign-up (same answer for new and known addresses) | `SignupRequest` | `SignupAccepted` | 202 | `SCHEMA_INVALID`, `EMAIL_NOT_ALLOWED`, `TERMS_NOT_ACCEPTED`, `RATE_LIMITED` |
| `POST /auth/verify-email` | Verify the address. This creates a public-tier tenant | `EmailVerify` | `VerificationResult` | 200 | `SCHEMA_INVALID`, `LINK_EXPIRED`, `RATE_LIMITED` |
| `GET /v1/tenants/{tenant_id}/verification` | E-mail and identity verification, and if code upload is permitted | — | `VerificationStatus` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PUT /v1/admin/tenants/{tenant_id}/verification` | Record identity verification (operators, audited) | `VerificationUpdate` | `VerificationStatus` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/tenants/{tenant_id}/deletion` | Schedule deletion of the full tenant (grace period, then purge) | `TenantDeletionRequest` | `TenantDeletion` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `GET /v1/tenants/{tenant_id}/deletion` | Deletion state and progress for each store | — | `TenantDeletion` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `DELETE /v1/tenants/{tenant_id}/deletion` | Cancel a scheduled deletion (during the grace period) | — | `TenantDeletion` | 200 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `POST /v1/tenants/{tenant_id}/exports` | GDPR export of all the data that the tenant stored | `GdprExportRequest` | `GdprExport` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `QUOTA_EXCEEDED` |
| `GET /v1/tenants/{tenant_id}/exports` | The GDPR exports of the tenant, newest first (`GET` one export to get its download link) | — | `Page[GdprExport]` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/tenants/{tenant_id}/exports/{export_id}` | An export. When it is ready, also its download link | — | `GdprExport` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/admin/signups` | Sign-ups, newest first. Filter by `review` state (the Sybil review queue) or `tenant_id` | — | `Page[SignupReview]` | 200 | `FORBIDDEN` |
| `POST /v1/admin/signups/{signup_id}/review` | Approve or reject a sign-up (a rejection suspends its tenant, audited) | `SignupReviewDecision` | `SignupReview` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `POST /v1/admin/tenants/{tenant_id}/deletion/retry` | Start a failed tenant purge again (operators, audited) | — | `TenantDeletion` | 202 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |

## Workflows, governance, explorers and About cards <span class="mapi-badge">v0.5</span> { #m35 }

v0.5 (milestone M3.5) adds the contracts for these items:

- Automated **workflows**: statistical tests, data processing, splits, searches and models in
  one versioned DAG.
- Model **governance**: policies, checks with evidence, validation and approval.
- The **Data Explorer** and the **Model Explorer**.
- **Budget optimisation** with mmm-framework.
- The plain-language **About cards**, with the **Help me choose** chooser.

The endpoint table agrees with `mapi_spec.api.M35_ENDPOINTS`. `mapi_spec.api.ENDPOINTS` is the
full v0.5 surface (`V04_ENDPOINTS` + `M35_ENDPOINTS`). `ROUTE_ACCESS` gives the scope and the
permission of each operation. [Workflows](../concepts/workflows.md),
[Governance](../concepts/governance.md) and
[About cards and choosing](../concepts/about-and-choosing.md) explain the ideas.

v0.5 also **removes** an importer route of v0.4, its ingest preset and one snippet language
(see the CHANGELOG). Thus, mmm-framework is the only MMM engine. The contract version is `0.2`.

**Permissions.** These rules apply:

- Viewers read workflows. Editors write them. Runners run and schedule them. Admins delete
  them.
- Project admins manage project policies. Tenant admins manage tenant-wide policies. All
  members of the project can read the policies.
- To submit a model version for validation, you need `run` rights (`validate` on `model`).
- To approve or reject, you need the **reviewer** capability (`Membership.capabilities`, given
  with `MembershipUpdate`). Project admins have this capability automatically.
- The person who submitted a model version cannot approve or reject it
  (`403 SELF_APPROVAL_FORBIDDEN`).
- No API token can approve.

The new token scopes are `workflows:write`, `workflows:run`, `models:validate` and
`policies:manage`.

**Long-running work.** A workflow run, an explore run and an optimize run are the same as
other runs (`Idempotency-Key`, the quota checks, cost). `POST …:optimize` obeys
`Prefer: wait=N`. Workflow runs stream their node events on
`GET /v1/workflow-runs/{run_id}/events` (SSE with `Last-Event-ID`, event types in
`WorkflowEventType`). The terminal events also go to webhooks (`workflow.run.succeeded`,
`workflow.run.failed`). These events also go to webhooks: validation transitions
(`model.validated`, `model.rejected`, ...), `dataset.version.created` and
`optimize.completed`.

**Promotion.** A governance policy that applies to a model can set
`require_validated_for_promotion`. In this case, `PUT /v1/endpoints/{alias}` refuses a version
if its validation status is not `validated` (`409 NOT_VALIDATED`). No override can pass this
check.

### Endpoints { #m35-endpoints }

#### About cards and the chooser

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/catalog/abouts` | About cards of catalog items (filter: kind) | — | `Page[CatalogAbout]` | 200 | — |
| `GET /v1/catalog/chooser` | The Help-me-choose questions | — | `ChooserQuestionnaire` | 200 | — |
| `GET /v1/catalog/recommendations` | Ranked recommendations for chooser answers (query: one parameter for each question) | — | `ChooserResult` | 200 | `CHOOSER_ANSWER_INVALID` |
| `GET /v1/catalog/workflow-nodes` | Workflow node implementations with ports, parameters and About cards | — | `Page[NodeImplementation]` | 200 | — |
| `GET /v1/catalog/workflow-nodes/{node_id}` | One node implementation | — | `NodeImplementation` | 200 | `NOT_FOUND` |
| `GET /v1/catalog/governance-checks` | The governance check catalog | — | `Page[GovernanceCheckDef]` | 200 | — |

#### Datasets: About and the Data Explorer

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/datasets/{dataset_id}/about` | The description of the owner and the generated summary of the latest version | — | `DatasetAbout` | 200 | `NOT_FOUND` |
| `PATCH /v1/datasets/{dataset_id}/about` | Change the description of the owner | `DatasetAboutUpdate` | `DatasetAbout` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/datasets/{dataset_id}/versions/{version}/explore` | Calculate Data Explorer sections (an explore run) | `DataExploreRequest` | `Exploration` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `EXPLORE_INVALID`, `QUOTA_EXCEEDED` |
| `GET /v1/explorations/{run_id}` | An explore run. When it is done, also links to its views | — | `Exploration` | 200 | `NOT_FOUND` |

#### Models: the Model Explorer and budget optimisation

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/models/{model_id}/versions/{version}/explore` | Calculate Model Explorer sections (an explore run) | `ModelExploreRequest` | `Exploration` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `EXPLORE_INVALID`, `CAPABILITY_UNSUPPORTED`, `QUOTA_EXCEEDED` |
| `POST /v1/models:compare` | Compare two or more model versions side by side (an explore run: metrics, ranking, Diebold-Mariano tests) | `ModelCompareRequest` | `Exploration` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `EXPLORE_INVALID`, `QUOTA_EXCEEDED` |
| `POST /v1/models/{model_id}/versions/{version}:optimize` | Optimise a budget across channels (Prefer: wait=N -> 200 if done, else 202) | `OptimizeRequest` | `Optimization` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `OPTIMIZE_INFEASIBLE`, `CAPABILITY_UNSUPPORTED`, `QUOTA_EXCEEDED` |
| `GET /v1/optimizations/{run_id}` | An optimize run. When it is done, also its plan | — | `Optimization` | 200 | `NOT_FOUND` |

#### Workflows

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `GET /v1/workflows` | Workflows of the project | — | `Page[Workflow]` | 200 | — |
| `POST /v1/workflows` | Create a workflow from a document, YAML text or a gallery template | `WorkflowCreate` | `Workflow` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `WORKFLOW_INVALID`, `CONFLICT`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE` |
| `POST /v1/workflows:validate` | Static checks of a workflow document (JSON pointers). It stores nothing | `WorkflowValidateRequest` | `WorkflowValidation` | 200 | `SCHEMA_INVALID`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE` |
| `GET /v1/workflows/{workflow_id}` | A workflow | — | `Workflow` | 200 | `NOT_FOUND` |
| `PATCH /v1/workflows/{workflow_id}` | Change the description or archive the workflow | `WorkflowUpdate` | `Workflow` | 200 | `SCHEMA_INVALID`, `NOT_FOUND` |
| `DELETE /v1/workflows/{workflow_id}` | Delete a workflow (this also deletes its runs, schedules and triggers) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN`, `CONFLICT` |
| `GET /v1/workflows/{workflow_id}/revisions` | Revisions, newest first | — | `Page[WorkflowRevision]` | 200 | `NOT_FOUND` |
| `POST /v1/workflows/{workflow_id}/revisions` | Save a new revision (validated. If it has errors, you cannot run it) | `WorkflowRevisionCreate` | `WorkflowRevision` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `CONFLICT`, `SPEC_PARSE_ERROR`, `PAYLOAD_TOO_LARGE` |
| `GET /v1/workflows/{workflow_id}/revisions/{revision}` | A revision with its validation | — | `WorkflowRevision` | 200 | `NOT_FOUND` |
| `POST /v1/workflows/{workflow_id}/runs` | Run a revision (a workflow parent run) | `WorkflowRunCreate` | `WorkflowRun` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `WORKFLOW_INVALID`, `WORKFLOW_NOT_RUNNABLE`, `QUOTA_EXCEEDED`, `IDEMPOTENCY_CONFLICT` |
| `GET /v1/workflows/{workflow_id}/runs` | Runs of a workflow, newest first | — | `Page[WorkflowRun]` | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}` | A workflow run with all its nodes, decisions and outputs | — | `WorkflowRun` | 200 | `NOT_FOUND` |
| `POST /v1/workflow-runs/{run_id}:cancel` | Cancel a workflow run and its child runs that are in progress | — | `WorkflowRun` | 202 | `NOT_FOUND`, `CONFLICT` |
| `GET /v1/workflow-runs/{run_id}/events` | Workflow run events (SSE, Last-Event-ID) | — | SSE stream | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}/nodes` | The node runs of a workflow run | — | `Page[NodeRun]` | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}/nodes/{node_id}` | The status, result and outputs of one node | — | `NodeRun` | 200 | `NOT_FOUND` |
| `GET /v1/workflow-runs/{run_id}/nodes/{node_id}/outputs/{port}` | A signed link to the object of a node output | — | `ArtifactLink` | 200 | `NOT_FOUND` |
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
| `GET /v1/governance/policies` | Policies that apply in the project (its own policies and the policies of the tenant) | — | `Page[GovernancePolicy]` | 200 | — |
| `POST /v1/governance/policies` | Add a project policy (version 1) | `GovernancePolicyCreate` | `GovernancePolicy` | 201 | `SCHEMA_INVALID`, `FORBIDDEN`, `POLICY_INVALID` |
| `GET /v1/governance/policies/{policy_id}` | The latest version of a policy | — | `GovernancePolicy` | 200 | `NOT_FOUND` |
| `PUT /v1/governance/policies/{policy_id}` | Save a new version (expected_version prevents conflicts between concurrent edits) | `GovernancePolicyUpdate` | `GovernancePolicy` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `POLICY_INVALID` |
| `DELETE /v1/governance/policies/{policy_id}` | Retire a policy (its versions stay for audit) | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |
| `GET /v1/governance/policies/{policy_id}/versions` | All versions of a policy, newest first | — | `Page[GovernancePolicy]` | 200 | `NOT_FOUND` |
| `GET /v1/tenants/{tenant_id}/governance/policies` | Tenant-wide policies (the minimum for each project) | — | `Page[GovernancePolicy]` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `POST /v1/tenants/{tenant_id}/governance/policies` | Add a tenant-wide policy | `GovernancePolicyCreate` | `GovernancePolicy` | 201 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `POLICY_INVALID` |
| `GET /v1/tenants/{tenant_id}/governance/policies/{policy_id}` | A tenant-wide policy | — | `GovernancePolicy` | 200 | `NOT_FOUND`, `FORBIDDEN` |
| `PUT /v1/tenants/{tenant_id}/governance/policies/{policy_id}` | Save a new version of a tenant-wide policy | `GovernancePolicyUpdate` | `GovernancePolicy` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `POLICY_INVALID` |
| `DELETE /v1/tenants/{tenant_id}/governance/policies/{policy_id}` | Retire a tenant-wide policy | — | — | 204 | `NOT_FOUND`, `FORBIDDEN` |

#### Validations, approvals and evidence

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/models/{model_id}/versions/{version}/validations` | Submit a model version. This runs the policy checks as a governance workflow | `ValidationRequest` | `ModelValidation` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `VALIDATION_TRANSITION_INVALID`, `POLICY_INVALID`, `QUOTA_EXCEEDED` |
| `GET /v1/models/{model_id}/versions/{version}/validations` | Validations of a model version, newest first | — | `Page[ModelValidation]` | 200 | `NOT_FOUND` |
| `GET /v1/validations` | Validations of the project (filter: status, for example the queue of the reviewers) | — | `Page[ModelValidation]` | 200 | — |
| `GET /v1/validations/{validation_id}` | A validation: status, results, approvals, history | — | `ModelValidation` | 200 | `NOT_FOUND` |
| `GET /v1/validations/{validation_id}/evidence` | All check results with their evidence | — | `Page[CheckResult]` | 200 | `NOT_FOUND` |
| `POST /v1/validations/{validation_id}:cancel` | Stop the checks that are in progress (back to draft) | `ValidationNote` | `ModelValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `VALIDATION_TRANSITION_INVALID` |
| `POST /v1/validations/{validation_id}:resubmit` | Run the checks again after a failure or a rejection | `ValidationNote` | `ModelValidation` | 202 | `SCHEMA_INVALID`, `NOT_FOUND`, `VALIDATION_TRANSITION_INVALID`, `QUOTA_EXCEEDED` |
| `POST /v1/validations/{validation_id}/approvals` | A reviewer approves or rejects (never the submitter, audited) | `ApprovalCreate` | `ModelValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `SELF_APPROVAL_FORBIDDEN`, `VALIDATION_TRANSITION_INVALID`, `CONFLICT` |
| `POST /v1/validations/{validation_id}:deprecate` | Retire a validated model version (audited with the reason) | `ValidationNote` | `ModelValidation` | 200 | `SCHEMA_INVALID`, `NOT_FOUND`, `FORBIDDEN`, `VALIDATION_TRANSITION_INVALID` |
| `POST /v1/validations/{validation_id}/evidence-pack` | The audit export (JSON, and a signed link to the HTML version) | `EvidencePackRequest` | `EvidencePack` | 200 | `SCHEMA_INVALID`, `NOT_FOUND` |

## Tree datasets and multi-file versions <span class="mapi-badge">v0.8</span> { #m4 }

A dataset version can hold an `xr.DataTree` (layout `tree`) instead of one `xr.Dataset`
(layout `dataset`, the default). `DatasetVersion` then has `layout: "tree"`, the
`tree_schema` (a `TreeSchema`: one `DataSchema` per node such as `/` or `/instrument/psf`)
and the `sources` it was built from, and `data_schema` is the schema of the root node.

- **Several files in one version.** `DatasetVersionCreate` takes either `upload_id` +
  `mapping` (one file, as before) or `assembly` (an `IngestAssembly`): each source names its
  upload, its own `IngestMapping`, the `node` it goes to and how it joins the other sources
  of that node: `node` (the only source), `concat` (along `concat_dim`, one `concat_label`
  per file) or `merge` (variables on shared coords, aligned per `join`).
- **Raw HDF5.** Format `hdf5` needs `mapping.hdf5` (an `Hdf5Mapping`): which HDF5 dataset
  becomes which variable of which node, with its dims (`Hdf5Var`) and coords (`Hdf5Coord`).
  To see what is in a file first, inspect the upload (below). NetCDF and xarray-written HDF5
  files keep format `netcdf`; their groups become nodes.
- **Inspection.** An `inspect` run reads only the metadata of an upload (after the HDF5
  safety checks) and lists its groups and arrays. It is idempotent per upload: a finished
  inspection is returned as it is.

| Endpoint | Purpose | Request | Response | Success | Errors |
|---|---|---|---|---|---|
| `POST /v1/datasets/{dataset_id}/uploads/{upload_id}/inspect` | List the groups and arrays of an upload (an `inspect` run). Accepts `Idempotency-Key`. | — | `Run` | 202 | `NOT_FOUND`, `CONFLICT` (the upload is not complete), `QUOTA_EXCEEDED` |
| `GET /v1/datasets/{dataset_id}/uploads/{upload_id}/inspection` | What the inspect run found | — | `UploadInspection` | 200 | `NOT_FOUND` (never inspected), `NOT_READY` (409, the run has not finished) |

## DTO index

All of these models are in `mapi_spec.api`, unless the table gives a different module. Each
model has a schema in the OpenAPI document and in `export_schemas()` as `api-<Name>.json`.

| Area | Models |
|---|---|
| Envelope | `Page[T]`, `ObjectRef`, `ArtifactLink`, `Health`, `HealthCheck`, `Problem` (`mapi_spec`) |
| Projects | `Project` |
| Datasets | `DatasetCreate`, `Dataset`, `UploadCreate`, `UploadSession`, `IngestMapping`, `DimMapping`, `VarMapping`, `DatasetVersionCreate`, `DatasetVersion`, `DatasetVersionStatus` |
| Specs | `SpecCreate`, `SpecDoc`, `SpecRevisionCreate`, `SpecRevision`, `ValidateRequest`, `ValidateResponse`, `StageSchema` |
| Runs | `RunCreate`, `RunRetry`, `Run`, `RunCost`, `ProgressSnapshot`, `RunEventView`, `LogLine`, `SseEventName` |
| Models | `Model`, `ModelVersion`, `Scorecard`, `ScorecardItem`, `Lineage`, `ScoreCall`, `ScoreInline`, `ScoreCallResult`, `ModelInputSchema`, `BundleManifest` (`mapi_spec.run`) |
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
| Tree datasets (v0.8) | `TreeSchema`, `NodeSelect` (`mapi_spec.tree`), `IngestAssembly`, `AssemblySource`, `Hdf5Mapping`, `Hdf5Var`, `Hdf5Coord`, `UploadInspection`, `InspectedArray` (`mapi_spec.ingest`) |
| Explorers and optimisation (v0.5) | `DataExploreRequest`, `ModelExploreRequest`, `ModelCompareRequest` (`mapi_spec.explore`), `Exploration`, `ExplorerIndex` (`mapi_spec.views`), `OptimizeRequest`, `OptimizeResult` (`mapi_spec.optimize`), `Optimization` |
