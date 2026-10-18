---
description: Drive the platform from Python with mapi_client - upload datasets, validate and fit specs, follow runs live, inspect signed model versions, score them and their endpoints into xarray, and wire up webhooks and plot renders.
---

# The Python client <span class="mapi-badge">v0.2</span>

`mapi-client` (`mapi_client`, Apache-2.0) is a typed client for the `/v1`
[HTTP API](http-api.md). It uses `httpx`. Each request and each response is a Pydantic model
from `mapi_spec.api`. The server uses the same models. Thus, if a field name has a typo, your
editor shows the error before production. Errors come back as `mapi_spec.errors.MapiError`
subclasses with the stable error code of the server.

The `mapi` CLI, `make seed` and the end-to-end tests use this client. This page shows one
script, `docs/snippets/client_tour.py`. `make e2e` runs this script against a live stack. The
outputs on this page come from that script. Some outputs are shorter than the real outputs.

```bash
uv add mapi-client              # or, for xr.Dataset uploads and xr.DataTree results:
uv add "mapi-client[xarray]"
```

The workspace already has the two packages. The examples need a live stack and the demo
files:

1. Run `make dev`.
2. Run `uv run python scripts/seed.py --export demo/`.

## Connect

```python
--8<-- "client_tour.py:connect"
```

```text
http://127.0.0.1:8000 ok 0.8.0
```

`Client()` finds its settings in the same sequence as the CLI:

1. The arguments (`Client("http://…", token=…, project=…)`).
2. `MAPI_URL` (or `MAPI_API_URL`), `MAPI_TOKEN`, `MAPI_PROJECT` and `MAPI_PROFILE`.
3. A profile in `~/.config/mapi/config.toml`.
4. `http://127.0.0.1:8000`.

`api.config.sources` tells where each value came from. Use the client as a context manager
(`with Client() as api:`), or call `close()`.

## Upload data

```python
--8<-- "client_tour.py:upload"
```

```text
dataset://default/eu_media_daily@v2 ready sha256:3062fa82622c
dataset://default/fx_daily@v2 ready sha256:7f27db4a2ac4
dataset://default/holidays_eu@v2 ready sha256:11abf028d5f5
dataset://default/eu_media_future@v2 ready sha256:2db88e1af79d
```

`datasets.upload` does all the steps of an upload:

1. If the name is new, it makes the dataset.
2. It opens an upload session.
3. It PUTs the bytes to the signed URL. It does not send your API credentials to that URL.
4. It starts the ingest run in the sandbox with a new idempotency key.
5. It waits until the version is `ready`. If the ingest fails, it raises the error of the
   ingest.

It accepts a path, `bytes` or an `xr.Dataset` (with the `xarray` extra). CSV and Parquet files
need a `mapping=IngestMapping(…)`. Zarr zips and netCDF files describe themselves.

The version has the schema that the ingest run checked:

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
hols ['channel', 'geo', 'time']
```

Validation is synchronous and static. The server never imports plugin code. It returns all
the issues at one time, each with a JSON pointer (`/pipeline/2/with/over`). It also returns
the schema at each stage. Here, the schema uses the canonical names of the dataset (`geo`,
`time`) after binding. The marketing-mix model (`mmm.bayesian`, on mmm-framework) scales the
data and carries spend over. Thus, the pipeline stops at the currency, weekly and holiday
steps.

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
   sampling (pymc)
   posterior ready
   fit complete
   posterior ready
state  rendering plots and views
   explorer: params
diagnostic RHAT 1.0485957785967657
diagnostic ESS_BULK 70.61247760811287
diagnostic ESS_TAIL 51.394768720375154
diagnostic DIVERGENCES 0.0
diagnostic PARETO_K_HIGH 3.0
metric coverage_90 0.9583333333333334
metric elpd_loo 89.0564020582058
metric mape 2.53957229942099
state  uploading outputs
complete  None
state  complete manifest uploaded
end: succeeded
succeeded mv_01m53hmgdhfby1esy1xe341hvh
```

(We got this output on a laptop against `make dev`. The demo spec has very small sampler
settings on purpose. "posterior ready" shows two times. First, the model fits on the training
weeks. Then it does an evaluation on the holdout. Then it fits again on all the weeks.)

`runs.stream_events` follows the server-sent event stream. It yields four types of items:

| Item | When | Notes |
|---|---|---|
| `RunSnapshot` | First, and after each change of state | The full `Run` |
| `RunEventFrame` | Persisted events: `state`, `warning`, `diagnostic`, `metric`, `checkpoint`, `complete`, `error` | Has an id. The stream sends it again after a reconnect. |
| `RunLiveFrame` | `progress` and `log` | Live only. The stream never sends it again. |
| `RunEnd` | One time, when the run is terminal | Then the stream closes. |

If the connection stops, the client continues it with `Last-Event-ID`. Thus, no persisted
event is lost or repeated. `runs.wait` polls (with backoff) and does not use the stream. With
`raise_on_failure=True`, it raises the error of the run. `runs.logs(run_id)` gives the
captured output page by page.

## Inspect the model version

```python
--8<-- "client_tour.py:inspect"
```

```text
model://default/eu-geo-mmm@v1 fail
{'mape': 2.53957229942099, 'elpd_loo': 89.0564020582058, 'coverage_90': 0.9583333333333334}
warn RHAT: The chains mostly agree, but not fully (R-hat 1.05).
fail ESS: Only 71 independent draws: the numbers are not stable.
pass DIVERGENCES: The sampler explored the model without problems.
pass HOLDOUT_MAPE: The forecast error on the holdout periods was 3%.
pass HOLDOUT_COVERAGE: 96% of held-out values fell inside the 90% range, as expected.
pass NEGATIVE_ROI: No channel is likely to have a negative return.
pass ROI_ABSURD: The highest channel return (4.34 per unit spent) is believable.
pass SHARE_OUTLIER: No single channel explains most of the KPI.
pass BASELINE_SHARE: The baseline (KPI without media) is 84%, a plausible share.
pass PRIOR_OVERLAP: The data moved every parameter away from its prior.
pass DATA_MISSING: Only 0% of the values are missing.
fail DATA_HISTORY: Only 0.6 years of history: too short to separate media from seasonality.
pass SPEND_VARIATION: Every channel's spend varies enough to measure its effect.
```

The scorecard works correctly here. The demo spec samples 100 draws on 2 chains from about
half a year of weekly data. This keeps the tour fast. The scorecard tells this in plain words:
too few independent draws and too little history. If a scorecard fails, you cannot promote
the model version to an endpoint (`PROMOTION_BLOCKED`). If a scorecard only warns, an admin
must give an audited reason ([the promotion gate](../concepts/integrations.md#promotion-gate)).

A `ModelVersion` also has the signed `manifest`, the `diagnostics`, the `capabilities`, the
`lineage` and the list of `artifacts`. You can download each of them:

```python
--8<-- "client_tour.py:download"
```

The API checks the signature of the manifest before it makes the short-lived signed URL. The
client compares the download with the CRC32C and the size that the object store calculated.
`mapi_sdk.bundle.Bundle` opens the downloaded `bundle.zarr.zip` in a notebook.

## Score

```python
--8<-- "client_tour.py:score"
```

```text
succeeded ['plots.json', 'result.json', 'score.zarr.zip', 'summary.json', 'ui/guided/forecast.json', 'ui/summary.arrow']
{"contributions": "model-owned", "y_hat": "model-owned"}
[272645. 254447. 259143. 258366. 265242. 269497. 268112. 266377.]
```

`model-owned` means that the model returned its answers in the units of its own stage. The
`units` attribute of each variable records these units. Here, the units are weekly EUR. The
pipeline converted and resampled the data before the model.

`models.score` sends the request with `Prefer: wait=60`. If the run is not complete at that
time, the client sends the *same* request again with the *same* idempotency key. The server
answers with the run that it made before. Thus, a slow score never becomes two runs. With the
`xarray` extra, the client downloads the result tree, does a check of it and puts it in
`result.tree`.

The request is a [`ScoreRequest`](../reference/spec-format.md) as a dict, a YAML string or the
Pydantic model. The client fills in `apiVersion` and `kind`.

## Errors

```python
--8<-- "client_tour.py:errors"
```

```text
SIDE_TABLE_COVERAGE validation failed with 1 error; first at /side_tables/fx: side table 'fx' has score_policy 'supplied' and must be provided for stage 'raw'
MODE_NOT_SUPPORTED
```

The client changes each RFC 7807 problem from the server into a `MapiError`. The error can
also be a subclass, for example `ModeNotSupported`, `ValidationFailed`, `CoordPlanError`,
`QuotaExceeded` or `IntegrityFailure`. It has the stable `code`, the HTTP `status`, the JSON
`pointer` and the individual `issues`. The two requests above failed in the static checks,
before the server made a run. Transport failures raise `mapi_client.TransportError`. If the
client cannot parse a response, it raises `UnexpectedResponse`.

The client tries idempotent calls again on 502, 503, 504 and connection errors. Idempotent
calls are `GET`s, and `POST`s with an idempotency key. The client obeys `Retry-After`
(`ClientConfig.max_retries`, default 2). For `429` answers, see
[Quotas and rate limits](#quotas-and-rate-limits).

## Projects, tokens and usage <span class="mapi-badge">v0.3</span> { #projects-tokens-and-usage }

On a multi-tenant API, the client needs credentials and a project
([Tenancy and authentication](../concepts/tenancy-and-auth.md)). The credentials come from an
API token (`Client(token=…)`, `MAPI_TOKEN` or the profile). If there is no token, they come
from the sign-in that `mapi login` stored for the profile. The client renews that sign-in
automatically. It sends the sign-in only to the API that made it.

The project comes from `Client(project=…)`, `MAPI_PROJECT` or the profile (`mapi use`). The
client sends the project as `X-Mapi-Project`, only on the requests for a project:

```python
--8<-- "client_tenancy.py:me"
```

Make a token for a program. The API returns the secret only one time. The secret does not
show in `repr`:

```python
--8<-- "client_tenancy.py:tokens"
```

Usage reports and runs have exact costs in integer micro-units of the currency:

```python
--8<-- "client_tenancy.py:usage"
```

`api.tenants` contains the calls for tenants, projects and members. `api.admin` contains the
operator endpoints: tenants, quotas, kill switches, executors and the audit log. See the
[reference](#reference).

### Quotas and rate limits

A `429` answer has one of two codes:

- `RATE_LIMITED`: The server refused the request before it did work on it. Thus, the client
  waits and sends the request again, for all methods. It waits for `Retry-After`, then for an
  exponential back-off with a small jitter. It raises `RateLimited` only after
  `RetryPolicy.rate_limit_retries` attempts. If the server asks for a wait longer than
  `max_rate_limit_wait`, the client raises `RateLimited` immediately.
- `QUOTA_EXCEEDED`: The client raises `QuotaExceeded` immediately. `retry_after_of(exc)` tells
  when the quota window starts again. It is `None` when capacity becomes free only after the
  work that runs now is complete.

```python
--8<-- "client_tenancy.py:quota"
```

Use a `RetryPolicy` to change this behavior. With `max_quota_wait`, the client can also wait
until a short quota window ends:

```python
--8<-- "client_tenancy.py:retry"
```

## Endpoints, webhooks and sharing <span class="mapi-badge">v0.4</span> { #endpoints-webhooks-and-sharing }

The examples in this section come from `docs/snippets/client_integrations.py`. This script
runs offline against a substitute API. For the concepts, see
[Integrations](../concepts/integrations.md).

### Promote a model version to an endpoint

```python
--8<-- "client_integrations.py:promote"
```

```text
endpoint://growth/geo-mmm-prod -> model://growth/eu-geo-mmm@v3 allowed
```

A promotion goes through the [promotion gate](../concepts/integrations.md#promotion-gate). If
the scorecard of a version warns, the call raises `MapiError` with code `OVERRIDE_REQUIRED`.
Then a signed-in project admin must do the call again with `override=True, reason="…"`.
Nobody can override `PROMOTION_BLOCKED`. `api.endpoints` also has `list`, `get`, `history`,
`rollback(alias, reason, to_revision=…)` and `delete`.

### Score an endpoint into xarray

`api.endpoint(alias)` returns a `RemoteEndpoint`:

- `schema()` tells what to send.
- `validate(ds)` does a check of a payload, but does not run it.
- `score(ds)` sends an `xr.Dataset` inline (as XrJSON, up to 5 MB) and returns the scored
  `xr.Dataset`.

```python
--8<-- "client_integrations.py:score"
```

```text
['revenue_local', 'spend_local']
[[13260.]]
```

`score` returns the `summary` group. This group has a `stat` dim (`mean`, `hdi_90_lower`,
`hdi_90_upper` and more). For posterior draws along `sample`, pass `group="draws"` with
`output={"draws": 100}`. For a counterfactual, pass `group="delta/summary"`. Each other
`EndpointScoreCall` field is a keyword, for example `mode="forecast"`,
`horizon={"dim": "time", "periods": 8}`, `scenario=plan_ds` and
`future_covariates="carry_last"`.

Like `models.score`, it sends `Prefer: wait=60`. It sends the call again with the same
idempotency key until the run is complete. `timeout=` sets the maximum wait. Thus, a slow
score never makes two runs. `api.endpoints.score(alias, call)` is the lower-level call. It
returns a `ScoreResult` with the run, the output links and the downloaded tree. Inline
payloads need the `xarray` extra. If the data is larger, upload it as a dataset and send it
as `data="dataset://growth/plan@v2"`.

`mapi_client.xrjson.encode(ds)` changes an `xr.Dataset` into the XrJSON payload.
`decode(payload)` changes the payload back into an `xr.Dataset`. XrJSON is the wire format of
the SDK. It has dates as ISO strings and `NaN` as `null`. The client does a check of the size
before it sends data.

### Forecast with your latest actuals

`api.endpoints.forecast(alias, rows)` sends [actuals and the dates to
forecast](../concepts/forecasting-with-actuals.md) in one call. It does not fit the model
again. `rows` is a long table in the input layout of the endpoint (`schema().input_table`).
This layout has the time column, the target and the drivers. `rows` can be a pandas
`DataFrame`, a list of row mappings or a mapping of columns. Rows that have a target value
are actuals. The model forecasts the rows after them that have no target value:

```python
import pandas as pd

rows = pd.DataFrame({
    "t": ["1961-01-01", "1961-02-01", "1961-03-01", "1961-04-01", "1961-05-01"],
    "y": [417, 391, 419, None, None],
})
result = api.endpoints.forecast("ts-forecast", rows)
print(result.conditioning.message)
# Forecast updated with 3 actuals (1961-01-01 to 1961-03-01) after the fitted history, which
# ends 1960-12-01; the model's parameters were not re-estimated. 2 periods forecast from
# 1961-04-01 to 1961-05-01.
```

You can also give the two parts separately:

- `actuals=`: the rows with the target.
- `future=`: the dates to forecast, rows with their driver values, or a number of periods to
  add after the last actual (`future=6`).

The client carries empty driver values on future rows forward
(`future_covariates="carry_last"`, the default here). If a model cannot use actuals, it
refuses them with `ActualsNotSupported` (`422 ACTUALS_NOT_SUPPORTED`).
`api.endpoint(alias).forecast(rows)` does the same and gets the schema one time.
`mapi_client.forecast_call(schema, rows, ...)` only makes the call. `ScoreResult.conditioning`
is the record for each forecast. If you sent no actual, it has `conditioned=False` and the
end of the fitted history.

### Snippets

```python
--8<-- "client_integrations.py:snippets"
```

`api.endpoints.snippets(alias, languages)` returns the code that the
[Integrations hub](../concepts/integrations.md#snippets) shows. The languages are Python,
TypeScript, curl, R, Apps Script and a notebook cell. The Python snippet is the same as the
`api.endpoints.score(...)` call above. The notebook cell uses the
`api.endpoint(alias).score(...)` form. The two snippets run without changes. The tests of the
client run them.

### Webhooks

```python
--8<-- "client_integrations.py:webhook"
```

`create` and `rotate_secret` return the signing secret only one time. The secret does not
show in `repr`. `api.webhooks` also has `list`, `get`, `update`, `delete`,
`deliveries(id, status=…)` and `replay(id, delivery_id)`. For the checks of deliveries on the
receiver side, see [Receiving webhooks](webhook-receivers.md).

### Render a plot

```python
--8<-- "client_integrations.py:render"
```

`plots.render` accepts a PlotSpec, a saved plot (`saved_plot_id=`) or a plot of a model
version. Like a score, it waits for the render in the sandbox. `download_render` gets the
file and does a CRC32C check. `plots.save`, `saved`, `get_saved` and `delete_saved` manage
saved plots. `create_embed` returns a read-only embed link one time. Also see `embeds`,
`revoke_embed` and the public `embed(token)` view. See [Sharing plots](sharing-plots.md).

### Sign-up, verification, deletion and export

```python
--8<-- "client_integrations.py:export"
```

`download_export` waits until the export is ready. Then it downloads the export through the
storage client to a temporary file. It never uses your API credentials for this download.
When the size is correct, it renames the file to its final name.

`api.tenants` also has `verification`, `request_deletion(tenant_id, confirm=tenant_id)`,
`deletion` and `cancel_deletion`. `api.signup(...)` and `api.verify_email(token)` are the
public sign-up calls. The client sends them without credentials. Operators have
`api.admin.signups`, `review_signup`, `set_verification` and `retry_deletion`. See
[Deleting and exporting data](data-deletion.md).

### Guided mode

`api.guided` controls [Guided mode](../concepts/guided-mode.md) from code. It has these
calls:

- `templates()` and `template(id)`.
- `detect_roles(template_id, columns=[…] | dataset="dataset://…")`.
- `draft(template_id=…, name=…, dataset=…, answers={…})`. This returns the ingest mapping,
  the `ModelSpec` and its YAML. It does not store data.
- `compile({...})`. This returns the `ScoreRequest` for a what-if, a forecast or a preset.
- `preview_priors(business_priors, spec_id=…, revision=…)`. This starts a `translate_priors`
  run in the sandbox and waits for it.

## Workflows, governance and explorers <span class="mapi-badge">v0.5</span> { #workflows-governance-and-explorers }

All the v0.5 calls are on the same client. The `mapi_spec` DTOs give their types. The
documentation tests run the tour below, without changes, against a real server.

### Help me choose

```python
--8<-- "client_v05.py:choose"
```

### Run a workflow and follow it live

```python
--8<-- "client_v05.py:validate"
```

```python
--8<-- "client_v05.py:run"
```

`stream_events` yields these items:

- `WorkflowSnapshot`: the run with all its nodes. It comes first on each connection.
- `WorkflowEventFrame`: a node is queued, started, succeeded, cached, skipped, failed or
  retrying, or a branch is decided.
- `WorkflowEnd`.

If the connection stops, the client continues it with `Last-Event-ID`.
`api.workflows.wait(run_id)` polls and does not use the stream. It raises the error of the
node that failed.

```python
--8<-- "client_v05.py:inspect"
```

### Governance

```python
--8<-- "client_v05.py:governance"
```

```python
--8<-- "client_v05_more.py:policies"
```

```python
--8<-- "client_v05_more.py:validate"
```

```python
--8<-- "client_v05_more.py:approve"
```

### Explorers and budget optimisation

```python
--8<-- "client_v05_more.py:data-explorer"
```

```python
--8<-- "client_v05_more.py:model-explorer"
```

```python
--8<-- "client_v05_more.py:optimize"
```

### Schedules and triggers

```python
--8<-- "client_v05.py:automate"
```

## Async

`AsyncClient` has the same resources and arguments. Use `await` for the calls. Use
`async for` for the lists:

```python
--8<-- "client_tour.py:async"
```

The v0.5 resources work in the same way:

```python
--8<-- "client_v05_more.py:async"
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
| `plots` | `list`, `get`, `validate`; v0.4: `save`, `saved`, `get_saved`, `delete_saved`, `render`, `render_status`, `render_output`, `download_render`, `create_embed`, `embeds`, `revoke_embed`, `embed` |
| `guided` (v0.4) | `templates`, `template`, `detect_roles`, `draft`, `compile`, `translate_priors`, `prior_translation`, `wait_prior_translation`, `preview_priors` |
| `endpoints` (v0.4) | `list`, `get`, `promote`, `delete`, `history`, `rollback`, `schema`, `validate`, `submit`, `score`, `snippets`; `api.endpoint(alias)`: `info`, `schema`, `history`, `snippets`, `validate`, `score`, `score_result` |
| `webhooks` (v0.4) | `list`, `create`, `get`, `update`, `delete`, `rotate_secret`, `deliveries`, `replay` |
| `tenants` (v0.3) | `list`, `get`, `rename`, `projects`, `create_project`, `update_project`, `delete_project`, `members`, `add_member`, `set_role`, `remove_member`, `usage`, `audit`; v0.4: `verification`, `request_deletion`, `deletion`, `cancel_deletion`, `export`, `export_status`, `wait_export`, `download_export` |
| `tokens` (v0.3) | `list`, `create`, `get`, `revoke` |
| `usage` (v0.3) | `get` (the project), `tenant` |
| `admin` (v0.3, operators) | `tenants`, `tenant`, `create_tenant`, `update_tenant`, `quotas`, `set_quota`, `delete_quota`, `kill_switches`, `set_kill_switches`, `executors`, `executor`, `audit`; v0.4: `signups`, `review_signup`, `set_verification`, `retry_deletion` |

List calls return lazy paginators that follow `next_cursor`. `api.health()` and
`api.ready()` call `/healthz` and `/readyz`. `api.me()` calls `/v1/me`. `api.use(project)`
changes the selected project. `api.signup()`, `api.verify_email()` and `api.endpoint(alias)`
are v0.4. `mapi_client.webhooks` verifies deliveries. `mapi_client.xrjson` encodes inline
payloads.

The platform optimizes budgets (see [Optimise a budget](../tutorials/optimise-a-budget.md)).
For the DTOs, see [`mapi_spec`](../reference/api/spec.md#rest-dtos). For the endpoints, see
the [HTTP API reference](../reference/http-api.md).
