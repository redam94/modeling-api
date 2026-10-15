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
| `GET /v1/runs/{run_id}` | Get a run (state, failure class and problem, attempt, progress snapshot, cost placeholders) | — | `Run` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/events` | SSE stream or JSON page of persisted events (see [Run events](#run-events-server-sent-events)). Accepts `Last-Event-ID`. | — | `Page[RunEventView]` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/logs` | Captured log lines; filters `attempt`, `stream` | — | `Page[LogLine]` | 200 | `NOT_FOUND` |
| `GET /v1/runs/{run_id}/plots` | Plots a succeeded run rendered (`plots.json`: forecast fans, counterfactual bands), passed through the plot sanitizer on every read; rejected plots are left out. Empty until the run succeeds. | — | `Page[PlotSpec]` | 200 | `NOT_FOUND`, `INTEGRITY_FAILURE` |
| `POST /v1/runs/{run_id}:cancel` | Request cancellation. Idempotent: it returns the run in `cancelling`, or in its terminal state if the run had already finished. | — | `Run` | 202 | `NOT_FOUND`, `CONFLICT` |
| `POST /v1/runs/{run_id}:resume` | Resume a `partial` run from its checkpoints (new attempt) | — | `Run` | 202 | `NOT_FOUND`, `CONFLICT` (not `partial`) |
| `POST /v1/runs/{run_id}:retry` | Start a **new** run from a failed one, for example one resource class up after `RESOURCE_EXHAUSTED`. Accepts `Idempotency-Key`. | `RunRetry` | `Run` | 201 | `NOT_FOUND`, `CONFLICT` (not failed) |

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
