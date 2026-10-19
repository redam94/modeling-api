---
description: Use the /v1 HTTP API with curl - upload a CSV through a signed URL, validate a spec, fit, follow server-sent events and score with Prefer wait.
---

# The HTTP API <span class="mapi-badge">v0.2</span>

All the actions of the web app, the CLI and the Python client are calls to the versioned
`/v1` HTTP API. This guide does the main flow with `curl` and `jq`. Thus you can see each
request. The script is `docs/snippets/http_tour.sh`. `make e2e` runs it against a live stack.
The outputs below come from one of those runs, and are shorter.

| Where | What |
|---|---|
| [HTTP API reference](../reference/http-api.md) | Conventions, errors, pagination, idempotency, SSE and all the endpoints with their DTOs |
| [OpenAPI explorer](../reference/openapi.md) | The OpenAPI 3.1 document, which you can browse, with all the schemas and examples |
| `GET /openapi.json` on a running API | The same document, from the API |
| [Error codes](../reference/generated/error-codes.md) | All the stable error codes and their HTTP status |

First, start the stack and load the demo
(see the [quickstart](../getting-started/quickstart.md)):

1. Run `make dev`.
2. Run `make seed`.
3. Run `uv run python scripts/seed.py --export demo/`.
4. Run `uv run mapi datasets upload demo/eu_media_future.zarr.zip --name eu_media_future`.

From v0.3, `make dev` needs sign-in. Set `MAPI_TOKEN` to an API token. You get the token from
the Tokens page of the web app, or from `mapi tokens create`. Set `MAPI_PROJECT` to the
`<tenant>/<project>` for the token. The `api` function of the script then adds
`Authorization: Bearer …` and `X-Mapi-Project` to each API call. It never sends them to signed
storage URLs, because these URLs have their own authorization. See
[authentication](../operations/authentication.md).

## Health and the catalog

```bash
--8<-- "http_tour.sh:health"
```

```text
{"status":"ok","version":"0.9.0","checks":{"postgres":"ok","object_store":"ok","catalog":"ok"}}
mmm.bayesian@1.0.0  draws
mmm.frequentist@1.0.0  draws
sklearn.tabular_bootstrap@1.0.0  draws
…
ts.hsgp_trend@1.0.0  draws
```

`/healthz` tells if the process is alive. `/readyz` checks PostgreSQL, the object store and the
catalog. If one of them is not ready, it answers 503 with the same body. The catalog is what the
runner image supplies. A sandboxed *describe* run exports it. The server validates specs
against the catalog.

## Upload a CSV

Data never goes through the API. Do these steps:

1. Make a dataset.
2. Ask for an upload session.
3. PUT the bytes to the signed URL that the session gives.

```bash
--8<-- "http_tour.sh:dataset"
--8<-- "http_tour.sh:upload"
```

```text
dataset ds_01m4zy65cdbxtaa4vgsvpeb4rt
PUT 200
```

The URL is a V4-style signed URL for one object in the quarantine bucket. It is valid for one
hour. Send the headers from `upload.headers` exactly, because they are part of the signature:

- `content-type` must be the type that you declared.
- `x-goog-content-length-range` limits the size to `size_bytes`.
- `x-goog-if-generation-match: 0` makes the object write-once. Thus the storage refuses a
  second PUT to the same URL.

Then change the upload into a dataset version. For CSV and Parquet, the mapping gives the
column of each dim and variable. It also gives its kind, its score-time policy and its role:

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

`POST …/versions` answers `202` with the version in status `ingesting`. The answer also has the
id of the sandboxed ingest run that parses the file. The call accepts an `Idempotency-Key`. If
you send the request again with the same key and body, you get the same version. The platform
does not ingest the file two times.

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

`POST /v1/specs:validate` answers 200, also for a spec that is not valid. The result is in
`valid`. Each problem is in `issues` with its code and JSON pointer. The server uses non-2xx
statuses only for requests that it cannot validate. Examples are YAML that it cannot parse, or
a request that is too large.

```bash
--8<-- "http_tour.sh:fit"
```

`POST /v1/runs` answers `201` with the run in state `created` or `queued`. If you send the same
`Idempotency-Key` with the same body, the answer is `200` with the same run. If you send it
with a different body, the answer is `409 IDEMPOTENCY_CONFLICT`. Make one key for each user
intent. Use the same key again when you try again.

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

With `Accept: text/event-stream`, the events endpoint is a live stream. It sends these items:

1. A `snapshot` of the run.
2. The persisted events. Each event has an `id:`.
3. Live `progress` and `log` frames. These have no id, and the server never sends them again.
4. A new `snapshot` at each change of state.
5. `end` when the run is terminal. Then the server closes the stream.

To continue exactly after the last persisted event, connect again with
`Last-Event-ID: <id>`. Without that `Accept` header, the same URL gives the persisted events
as a JSON page. See [Run events](../reference/http-api.md#run-events-server-sent-events).

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

`GET …/artifacts/{name}` gives a signed GET URL for a bundle object or a `ui/` view. The server
makes the link only after it verifies the signed manifest of the version. Each link is pinned
to an object generation. It supports HTTP `Range` requests and expires after 15 minutes.

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

`Prefer: wait=60` keeps the request open until the score run finishes, for a maximum of 60
seconds. Then it answers `200` with `done: true` and signed links to the outputs. If the run
takes more time, the answer is `202` with `done: false`. In this case, do one of these steps:

- To get the result of the same run, send the request again with the same `Idempotency-Key`.
- Follow the run.

The response header `Preference-Applied` tells which wait the server used. Over HTTP, the
`ScoreRequest` needs its `apiVersion` and `kind`. The Python client fills them in.

The request must give data to score: `data`, inline values, a `scenario` dataset or a
`horizon`. If it gives none of them, the server refuses it before a run starts
(`SCHEMA_INVALID` at `/data`, "the score request has no data").

`ui/summary.arrow` names its dim columns in the schema metadata `mapi.dims` (a JSON list).
Use this list to find the keys of a row. A dim with numeric labels, for example a grid
position in cm, has a numeric type, the same as a statistic. In `summary.json`, each table of
a circular output (`OutputDecl.circular`) has the field `period`. Its statistics are then on
the circle:

- `mean` is the circular mean, and `sd` is the circular standard deviation.
- Each interval and quantile is centred on the circular mean.
- All values are in `[0, period)`. An interval with `lower` more than `upper` crosses the seam.

## Errors

```bash
--8<-- "http_tour.sh:problem"
```

```json
{
  "status": 422,
  "code": "MODE_NOT_SUPPORTED",
  "detail": "validation failed with 2 errors; first at /mode: model 'mmm.bayesian@1.0.0' does not support mode 'filter'"
}
```

Each error is an RFC 7807 problem document (`application/problem+json`). It has a stable
`code`, an HTTP `status`, and each individual problem in `errors[]` with a JSON pointer.
Tracebacks and plugin messages show only in an `untrusted_text` field. Render them as text.
The server refused the score request above statically. It did not make a run.

## Other clients

The server makes the OpenAPI document itself. `apps/web/scripts/gen_openapi.py` writes
`apps/web/openapi.json`, and CI fails if the file and the server do not agree. Thus all OpenAPI generators can
make a typed client. The TypeScript client of the web app comes from this method
(`openapi-typescript` and `openapi-fetch`). For Python, use [`mapi_client`](python-client.md).
