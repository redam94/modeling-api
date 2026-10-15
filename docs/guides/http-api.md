---
description: Use the /v1 HTTP API with curl - upload a CSV through a signed URL, validate a spec, fit, follow server-sent events and score with Prefer wait.
---

# The HTTP API <span class="mapi-badge">v0.2</span>

Everything the web app, the CLI and the Python client do is a call to the versioned `/v1`
HTTP API. This guide does the main flow with `curl` and `jq`, so you can see every request.
The script is `docs/snippets/http_tour.sh`; `make e2e` runs it against a live stack. The
outputs below are from one of those runs, shortened.

| Where | What |
|---|---|
| [HTTP API reference](../reference/http-api.md) | Conventions, errors, pagination, idempotency, SSE and every endpoint with its DTOs |
| [OpenAPI explorer](../reference/openapi.md) | The OpenAPI 3.1 document, browsable, with every schema and example |
| `GET /openapi.json` on a running API | The same document, served by the API itself |
| [Error codes](../reference/generated/error-codes.md) | Every stable error code and its HTTP status |

Start the stack and load the demo first: `make dev`, `make seed`,
`uv run python scripts/seed.py --export demo/` and
`uv run mapi datasets upload demo/eu_media_future.zarr.zip --name eu_media_future`
(see the [quickstart](../getting-started/quickstart.md)).

## Health and the catalog

```bash
--8<-- "http_tour.sh:health"
```

```text
{"status":"ok","version":"0.1.0","checks":{"postgres":"ok","object_store":"ok","catalog":"ok"}}
mmm.hierarchical_geo@1.3.0  draws
mmm.pymc_marketing@1.0.0  draws
sklearn.tabular_bootstrap@1.0.0  draws
…
ts.hsgp_trend@1.0.0  draws
```

`/healthz` says the process is alive; `/readyz` checks PostgreSQL, the object store and the
catalog, and answers 503 with the same body when one is not ready. The catalog is what the
runner image provides, exported by a sandboxed *describe* run; specs are validated against it.

## Upload a CSV

Data never passes through the API. You create a dataset, ask for an upload session, and PUT
the bytes to the signed URL it returns:

```bash
--8<-- "http_tour.sh:dataset"
--8<-- "http_tour.sh:upload"
```

```text
dataset ds_01m4zy65cdbxtaa4vgsvpeb4rt
PUT 200
```

The URL is a V4-style signed URL for one object in the quarantine bucket, valid for an hour.
Send the headers from `upload.headers` exactly, because they are part of the signature:
`content-type` must be the one you declared, `x-goog-content-length-range` caps the size at
`size_bytes`, and `x-goog-if-generation-match: 0` makes the object write-once, so a second PUT
to the same URL is rejected.

Then turn the upload into a dataset version. For CSV and Parquet the mapping names the
column of every dim and variable, with its kind, score-time policy and role:

```bash
--8<-- "http_tour.sh:ingest"
```

```json
{
  "ref": "dataset://default/weekly_sales@v1",
  "status": "ready",
  "dims": ["week"]
}
```

`POST …/versions` answers `202` with the version in status `ingesting` and the id of the
sandboxed ingest run that parses the file. It accepts an `Idempotency-Key`: a retried request
with the same key and body returns the same version instead of ingesting twice.

## Validate and fit a spec

```bash
--8<-- "http_tour.sh:validate"
```

```json
{
  "valid": true,
  "issues": [],
  "stages": ["raw", "to_eur", "weekly", "scale_spend", "scale_y", "lags", "season", "hols"]
}
```

`POST /v1/specs:validate` answers 200 even for an invalid spec: the verdict is in `valid` and
every problem is in `issues` with its code and JSON pointer. Non-2xx statuses are kept for
requests that cannot be validated at all (unparseable YAML, too large).

```bash
--8<-- "http_tour.sh:fit"
```

`POST /v1/runs` answers `201` with the run in state `created` or `queued`. The same
`Idempotency-Key` with the same body answers `200` with the same run; with a different body,
`409 IDEMPOTENCY_CONFLICT`. Generate one key per user intent and reuse it on retries.

## Follow the run

```bash
--8<-- "http_tour.sh:events"
```

```text
   3 event: snapshot
   1 event: state
   1 event: warning
   2 event: state
   6 event: progress
   1 event: state
   5 event: diagnostic
   3 event: metric
   1 event: state
   1 event: complete
   1 event: state
   2 event: snapshot
   1 event: end
{
  "state": "succeeded",
  "output_model_version_id": "mv_01m4zy6yc82qy97jzjmv9phv1j"
}
```

With `Accept: text/event-stream` the events endpoint is a live stream: a `snapshot` of the
run first, then the persisted events (each with an `id:`), live `progress` and `log` frames
(no id, never replayed), a fresh `snapshot` on every state change, and `end` when the run is
terminal, after which the server closes the stream. Reconnect with `Last-Event-ID: <id>` to
resume exactly after the last persisted event. Without that `Accept` header the same URL
returns the persisted events as a JSON page. See [Run events](../reference/http-api.md#run-events-server-sent-events).

## Read the model version

```bash
--8<-- "http_tour.sh:model"
```

```json
{
  "ref": "model://default/eu-geo-mmm@v1",
  "scorecard": "warn",
  "metrics": {"mape": 7.59, "elpd_loo": 183.47, "coverage_90": 0.875},
  "signed": "KMS-EC-P256-SHA256"
}
```

`GET …/artifacts/{name}` returns a signed GET URL for any bundle object or `ui/` view. It is
minted only after the server has verified the version's signed manifest; each link is pinned
to an object generation, supports HTTP `Range` requests and expires after 15 minutes.

## Score

```bash
--8<-- "http_tour.sh:score"
```

```json
{
  "done": true,
  "state": "succeeded",
  "outputs": [
    {"name": "plots.json", "size": 4122, "crc32c": "d8PGZA=="},
    {"name": "result.json", "size": 406, "crc32c": "PonvmQ=="},
    {"name": "score.zarr.zip", "size": 9396, "crc32c": "Uwymhw=="},
    {"name": "ui/summary.arrow", "size": 27266, "crc32c": "hxiS8w=="}
  ]
}
{
  "mode": "forecast",
  "representation": "draws",
  "output_space": {"contributions": "weekly", "y_hat": "weekly"}
}
```

`Prefer: wait=60` holds the request until the score run finishes (at most 60 seconds) and
answers `200` with `done: true` and signed links to the outputs. If it takes longer the
answer is `202` with `done: false`: repeat the request with the same `Idempotency-Key` to get
the same run's result, or follow the run. The response header `Preference-Applied` says
which wait the server used. Over HTTP the `ScoreRequest` needs its `apiVersion` and `kind`
(the Python client fills them in).

## Errors

```bash
--8<-- "http_tour.sh:problem"
```

```json
{
  "status": 422,
  "code": "MODE_NOT_SUPPORTED",
  "detail": "validation failed with 2 errors; first at /mode: model 'mmm.hierarchical_geo@1.3.0' does not support mode 'filter'"
}
```

Every error is an RFC 7807 problem document (`application/problem+json`) with a stable
`code`, an HTTP `status`, and every individual problem in `errors[]` with a JSON pointer.
Tracebacks and plugin messages only ever appear in an `untrusted_text` field; render them as
text. The score request above was rejected statically: no run was created.

## Other clients

The OpenAPI document is generated from the server itself (`apps/web/scripts/gen_openapi.py`
writes `apps/web/openapi.json`, and CI fails if it drifts), so any OpenAPI generator produces
a typed client. The web app's TypeScript client is generated that way (`openapi-typescript`
and `openapi-fetch`). For Python, use [`mapi_client`](python-client.md).
