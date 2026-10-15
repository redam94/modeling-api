---
description: Drive the platform from Python with mapi_client - upload datasets, validate and fit specs, follow runs live, inspect signed model versions and score them into xarray.
---

# The Python client <span class="mapi-badge">v0.2</span>

`mapi-client` (`mapi_client`, Apache-2.0) is a typed client for the `/v1`
[HTTP API](http-api.md), built on `httpx`. Every request and response is a Pydantic model from
`mapi_spec.api`, the same models the server uses, so a typo in a field name fails in your
editor, not in production. Errors come back as `mapi_spec.errors.MapiError` subclasses with
the server's stable error code.

It is the client the `mapi` CLI, `make seed` and the end-to-end tests use. This page walks
through one script, `docs/snippets/client_tour.py`, which `make e2e` runs against a live stack;
the outputs shown come from running it (shortened in places).

```bash
uv add mapi-client              # or, for xr.Dataset uploads and xr.DataTree results:
uv add "mapi-client[xarray]"
```

In the workspace both are already installed. The examples need a running stack and the
demo files: `make dev`, then `uv run python scripts/seed.py --export demo/`.

## Connect

```python
--8<-- "client_tour.py:connect"
```

```text
http://127.0.0.1:8000 ok 0.2.0
```

`Client()` resolves its settings like the CLI: arguments (`Client("http://…", token=…)`),
then `MAPI_URL` (or `MAPI_API_URL`), `MAPI_TOKEN` and `MAPI_PROFILE`, then a profile in
`~/.config/mapi/config.toml`, then `http://127.0.0.1:8000`. `api.config.sources` says where
each value came from. Use the client as a context manager (`with Client() as api:`) or call
`close()`.

## Upload data

```python
--8<-- "client_tour.py:upload"
```

```text
dataset://default/eu_media_daily@v2 ready sha256:0073129ba8ee
dataset://default/fx_daily@v2 ready sha256:7f27db4a2ac4
dataset://default/holidays_eu@v2 ready sha256:11abf028d5f5
dataset://default/eu_media_future@v2 ready sha256:2db88e1af79d
```

`datasets.upload` does the whole dance: it creates the dataset if the name is new, opens an
upload session, PUTs the bytes to the signed URL (without sending your API credentials
there), starts the sandboxed ingest run with a fresh idempotency key, and waits until the
version is `ready`, raising the ingest's error if it fails. It accepts a path, `bytes`, or an
`xr.Dataset` (with the `xarray` extra). CSV and Parquet files need a `mapping=IngestMapping(…)`;
zarr zips and netCDF files describe themselves.

The version carries the schema the ingest run checked:

```python
--8<-- "client_tour.py:schema"
```

```text
{'date': 'extend', 'market': 'new_levels', 'channel': 'fixed'}
{'spend_local': 'spend', 'revenue_local': 'target'}
```

## Validate a spec

```python
--8<-- "client_tour.py:validate"
```

```text
valid
raw ['channel', 'geo', 'time']
to_eur ['channel', 'geo', 'time']
weekly ['channel', 'geo', 'time'] <- split
scale_spend ['channel', 'geo', 'time']
scale_y ['channel', 'geo', 'time']
lags ['channel', 'geo', 'lag', 'time']
season ['channel', 'fourier_mode', 'geo', 'lag', 'time']
hols ['channel', 'fourier_mode', 'geo', 'lag', 'time']
```

Validation is synchronous and static: the server never imports plugin code. It returns every
issue at once with a JSON pointer (`/pipeline/2/with/over`), plus the schema at every stage,
here in the dataset's canonical names (`geo`, `time`) after binding.

## Fit and follow the run

```python
--8<-- "client_tour.py:fit"
```

```text
state  bootstrap object read and deleted
warning DEV_MODE_ISOLATION worker isolation is reduced (development mode): …
state  downloading the dataset
state  fitting the pipeline and the model
   validated the spec and the data
   chain 1/2: tuning 116/150, 0 divergences
   chain 2/2: sampling 31/100, 0 divergences
   evaluated the holdout through the score path
state  rendering plots and views
diagnostic RHAT 1.0488798610868963
…
metric mape 7.589774995896182
state  uploading outputs
complete  None
state  complete manifest uploaded
end: succeeded
succeeded mv_01m4zxxfd6xbmbmnv1byrxenf3
```

`runs.stream_events` follows the server-sent event stream and yields four kinds of items:

| Item | When | Notes |
|---|---|---|
| `RunSnapshot` | First, and after every state change | The full `Run` |
| `RunEventFrame` | Persisted events: `state`, `warning`, `diagnostic`, `metric`, `checkpoint`, `complete`, `error` | Has an id; replayed after a reconnect |
| `RunLiveFrame` | `progress` and `log` | Live only, never replayed |
| `RunEnd` | Once, when the run is terminal | The stream then closes |

A dropped connection is resumed with `Last-Event-ID`, so no persisted event is lost or
repeated. `runs.wait` polls instead (with backoff) and, with `raise_on_failure=True`, raises
the run's error; `runs.logs(run_id)` pages through the captured output.

## Inspect the model version

```python
--8<-- "client_tour.py:inspect"
```

```text
model://default/eu-geo-mmm@v4 warn
{'mape': 7.589774995896182, 'elpd_loo': 183.46594271421736, 'coverage_90': 0.875}
warn RHAT: The independent sampling runs disagree about 'beta_sigma', so its estimates are not reliable yet.
warn ESS_BULK: There is too little independent information about 'beta_channel' (about 53 effective draws) for stable averages.
warn ESS_TAIL: There is too little independent information about 'gamma_control' (about 35 effective draws) for stable intervals.
pass DIVERGENCES: The sampler explored the model without running into trouble.
warn PARETO_K_HIGH: Some data points strongly influence the model, so the leave-one-out score is only approximate.
```

A `ModelVersion` also carries the signed `manifest`, the `diagnostics`, the `capabilities`, the
`lineage` and the list of `artifacts`. Download any of them:

```python
--8<-- "client_tour.py:download"
```

The API checks the manifest's signature before minting the short-lived signed URL, and the
client checks the download against the CRC32C and size the object store computed.
`mapi_sdk.bundle.Bundle` opens the downloaded `bundle.zarr.zip` in a notebook.

## Score

```python
--8<-- "client_tour.py:score"
```

```text
succeeded ['plots.json', 'result.json', 'score.zarr.zip', 'ui/summary.arrow']
{"contributions": "weekly", "y_hat": "weekly"}
[69720. 60339. 61101. 60544. 63529. 64553. 61885. 60652.]
```

`models.score` sends the request with `Prefer: wait=60`. If the run is not done by then, it
repeats the *same* request with the *same* idempotency key: the server answers with the one
run it already created, so a slow score never turns into two runs. With the `xarray` extra
the result tree is downloaded and checked into `result.tree`.

The request is a [`ScoreRequest`](../reference/spec-format.md) as a dict, a YAML string or the
Pydantic model. `apiVersion` and `kind` are filled in for you.

## Errors

```python
--8<-- "client_tour.py:errors"
```

```text
SIDE_TABLE_COVERAGE validation failed with 1 error; first at /side_tables/fx: side table 'fx' has score_policy 'supplied' and must be provided for stage 'raw'
MODE_NOT_SUPPORTED
```

Every RFC 7807 problem from the server becomes a `MapiError` (or a subclass such as
`ModeNotSupported`, `ValidationFailed`, `CoordPlanError`, `QuotaExceeded`,
`IntegrityFailure`) with the stable `code`, the HTTP `status`, the JSON `pointer` and the
individual `issues`. Both requests above failed during static checks, before any run
was created. Transport failures raise `mapi_client.TransportError`; responses the client
cannot parse raise `UnexpectedResponse`.

Idempotent calls (`GET`s, and `POST`s with an idempotency key) are retried on 429, 502, 503,
504 and connection errors, honouring `Retry-After` (`ClientConfig.max_retries`, default 2).

## Async

`AsyncClient` has the same resources and arguments; calls are awaited and lists are
iterated with `async for`:

```python
--8<-- "client_tour.py:async"
```

## Reference

| Resource | Calls |
|---|---|
| `projects` | `list`, `get` |
| `catalog` | `get`, `models`, `model`, `steps`, `plots`, `schema` |
| `datasets` | `create`, `list`, `get`, `find`, `ensure`, `create_upload`, `create_version`, `versions`, `version`, `wait_version`, `upload`, `artifact`, `download` |
| `specs` | `validate`, `create`, `list`, `get`, `find`, `revise`, `upsert`, `revisions`, `revision`, `dry_run` |
| `runs` | `create`, `fit`, `get`, `list`, `wait`, `stream_events`, `events`, `logs`, `plots`, `cancel`, `resume`, `retry` |
| `models` | `list`, `get`, `versions`, `version`, `artifact`, `download`, `score` |
| `plots` | `list`, `get`, `validate` |

List calls return lazy paginators that follow `next_cursor`. `api.health()` and `api.ready()`
call `/healthz` and `/readyz`. The DTOs are documented in
[`mapi_spec`](../reference/api/spec.md#rest-dtos) and the endpoints in the
[HTTP API reference](../reference/http-api.md).
