---
description: Drive the platform from Python with mapi_client - upload datasets, validate and fit specs, follow runs live, inspect signed model versions, score them and their endpoints into xarray, and wire up webhooks and plot renders.
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
http://127.0.0.1:8000 ok 0.6.0
```

`Client()` resolves its settings like the CLI: arguments (`Client("http://…", token=…,
project=…)`), then `MAPI_URL` (or `MAPI_API_URL`), `MAPI_TOKEN`, `MAPI_PROJECT` and
`MAPI_PROFILE`, then a profile in
`~/.config/mapi/config.toml`, then `http://127.0.0.1:8000`. `api.config.sources` says where
each value came from. Use the client as a context manager (`with Client() as api:`) or call
`close()`.

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
hols ['channel', 'geo', 'time']
```

Validation is synchronous and static: the server never imports plugin code. It returns every
issue at once with a JSON pointer (`/pipeline/2/with/over`), plus the schema at every stage,
here in the dataset's canonical names (`geo`, `time`) after binding. The marketing-mix model
(`mmm.bayesian`, on mmm-framework) scales the data and carries spend over itself, so the
pipeline stops at the currency, weekly and holiday steps.

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

(Captured on a laptop against `make dev`, with the demo spec's deliberately tiny sampler
settings; "posterior ready" appears twice because the model is fitted on the training weeks,
evaluated on the holdout, then refitted on every week.)

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
model://default/eu-geo-mmm@v1 fail
{'mape': 2.53957229942099, 'elpd_loo': 89.0564020582058, 'coverage_90': 0.9583333333333334}
warn RHAT: The chains mostly agree, but not fully (R-hat 1.05).
fail ESS: Only 71 independent draws: the numbers are not stable.
pass DIVERGENCES: The sampler explored the model without problems.
pass HOLDOUT_MAPE: Forecasts were off by 3% on the held-out periods.
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

The scorecard is doing its job: the demo spec samples 100 draws on 2 chains from about half
a year of weekly data so the walkthrough stays fast, and the scorecard says exactly that - too
few independent draws and too little history - in plain words. A scorecard that fails blocks
promotion to an endpoint outright (`PROMOTION_BLOCKED`); one that only warns needs an admin's
audited reason ([the promotion gate](../concepts/integrations.md#promotion-gate)).

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
succeeded ['plots.json', 'result.json', 'score.zarr.zip', 'summary.json', 'ui/guided/forecast.json', 'ui/summary.arrow']
{"contributions": "model-owned", "y_hat": "model-owned"}
[272645. 254447. 259143. 258366. 265242. 269497. 268112. 266377.]
```

`model-owned` means the model returned its answers in its own stage's units, recorded in each
variable's `units` attribute: weekly EUR, because the pipeline converted and resampled before
the model.

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

Idempotent calls (`GET`s, and `POST`s with an idempotency key) are retried on 502, 503,
504 and connection errors, honouring `Retry-After` (`ClientConfig.max_retries`, default 2).
`429` answers are handled as described in [Quotas and rate limits](#quotas-and-rate-limits).

## Projects, tokens and usage <span class="mapi-badge">v0.3</span> { #projects-tokens-and-usage }

On a multi-tenant API the client needs credentials and a project
([Tenancy and authentication](../concepts/tenancy-and-auth.md)). Credentials come from an
API token (`Client(token=…)`, `MAPI_TOKEN` or the profile) or, when none is set, from the
sign-in `mapi login` stored for the profile; the client renews that sign-in by itself and
only sends it to the API it was made for. The project comes from `Client(project=…)`,
`MAPI_PROJECT` or the profile (`mapi use`), and is sent as `X-Mapi-Project` on
project-scoped requests only:

```python
--8<-- "client_tenancy.py:me"
```

Mint a token for a program. The secret is returned exactly once and is kept out of `repr`:

```python
--8<-- "client_tenancy.py:tokens"
```

Usage reports and runs carry exact costs in integer micro-units of the currency:

```python
--8<-- "client_tenancy.py:usage"
```

`api.tenants` covers tenants, projects and members; `api.admin` the operator endpoints
(tenants, quotas, kill switches, executors, the audit log). See the [reference](#reference).

### Quotas and rate limits

A `429` answer carries one of two codes. `RATE_LIMITED` means the request was refused before
the server acted on it, so the client waits (`Retry-After`, then exponential back-off with a
little jitter) and sends it again, whatever the method; it raises `RateLimited` only after
`RetryPolicy.rate_limit_retries` attempts, or at once when the server asks for a wait longer
than `max_rate_limit_wait`. `QUOTA_EXCEEDED` is raised at once as `QuotaExceeded`;
`retry_after_of(exc)` says when the quota window resets (`None` when only finishing running
work frees capacity):

```python
--8<-- "client_tenancy.py:quota"
```

Tune the behaviour with a `RetryPolicy` (`max_quota_wait` lets a short quota window be
waited out too):

```python
--8<-- "client_tenancy.py:retry"
```

## Endpoints, webhooks and sharing <span class="mapi-badge">v0.4</span> { #endpoints-webhooks-and-sharing }

The examples in this section come from `docs/snippets/client_integrations.py`, which runs
offline against a stand-in API. The concepts are in [Integrations](../concepts/integrations.md).

### Promote a model version to an endpoint

```python
--8<-- "client_integrations.py:promote"
```

```text
endpoint://growth/geo-mmm-prod -> model://growth/eu-geo-mmm@v3 allowed
```

Promotion goes through the [promotion gate](../concepts/integrations.md#promotion-gate): a
version whose scorecard warns raises `MapiError` with code `OVERRIDE_REQUIRED` until a
signed-in project admin repeats the call with `override=True, reason="…"`; `PROMOTION_BLOCKED`
cannot be overridden. `api.endpoints` also has `list`, `get`, `history`, `rollback(alias,
reason, to_revision=…)` and `delete`.

### Score an endpoint into xarray

`api.endpoint(alias)` returns a `RemoteEndpoint`: `schema()` says what to send, `validate(ds)`
checks a payload without running it, and `score(ds)` sends an `xr.Dataset` inline (as
XrJSON, up to 5 MB) and returns the scored `xr.Dataset`:

```python
--8<-- "client_integrations.py:score"
```

```text
['revenue_local', 'spend_local']
[[13260.]]
```

`score` returns the `summary` group (a `stat` dim: `mean`, `hdi_90_lower`, `hdi_90_upper`,
...); pass `group="draws"` with `output={"draws": 100}` for posterior draws along `sample`,
or `group="delta/summary"` for a counterfactual. Any other `EndpointScoreCall` field is a
keyword: `mode="forecast"`, `horizon={"dim": "time", "periods": 8}`, `scenario=plan_ds`,
`future_covariates="carry_last"`. Like `models.score`, it sends `Prefer: wait=60` and
repeats the call with the same idempotency key until the run is done (`timeout=` bounds the
wait), so a slow score never creates two runs. `api.endpoints.score(alias, call)` is the
lower-level call: it returns a `ScoreResult` with the run, the output links and the
downloaded tree. Inline payloads need the `xarray` extra; larger data is uploaded as a
dataset and sent as `data="dataset://growth/plan@v2"`.

`mapi_client.xrjson.encode(ds)` and `decode(payload)` convert between `xr.Dataset` and the
XrJSON payload (the SDK wire format: dates as ISO strings, `NaN` as `null`, size-checked
before anything is sent).

### Forecast with your latest actuals

`api.endpoints.forecast(alias, rows)` sends [actuals and the dates to
forecast](../concepts/forecasting-with-actuals.md) in one call, without a refit. `rows` is a
long table in the endpoint's input layout (`schema().input_table`: the time column, the
target, the drivers) - a pandas `DataFrame`, a list of row mappings or a mapping of columns.
Rows with the target filled are actuals; rows after them with it empty are forecast:

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

Or give them apart: `actuals=` (rows with the target) and `future=` - the dates to forecast,
rows with their driver values, or a number of periods to append after the last actual
(`future=6`). Empty driver values on future rows are carried forward
(`future_covariates="carry_last"`, the default here). A model that cannot use actuals refuses
them with `ActualsNotSupported` (`422 ACTUALS_NOT_SUPPORTED`). `api.endpoint(alias).forecast(rows)`
does the same and fetches the schema once; `mapi_client.forecast_call(schema, rows, ...)` only
builds the call. `ScoreResult.conditioning` is the record for any forecast, with
`conditioned=False` and the end of the fitted history when no actual was sent.

### Snippets

```python
--8<-- "client_integrations.py:snippets"
```

`api.endpoints.snippets(alias, languages)` returns the code the
[Integrations hub](../concepts/integrations.md#snippets) shows, in Python, TypeScript, curl,
R, Apps Script and a notebook cell. The Python snippet is exactly the
`api.endpoints.score(...)` call above, the notebook cell the `api.endpoint(alias).score(...)`
form; both run as generated (the client's tests execute them).

### Webhooks

```python
--8<-- "client_integrations.py:webhook"
```

The signing secret is returned once (by `create` and `rotate_secret`) and kept out of
`repr`. `api.webhooks` also has `list`, `get`, `update`, `delete`, `deliveries(id,
status=…)` and `replay(id, delivery_id)`. To check deliveries on the receiving side, see
[Receiving webhooks](webhook-receivers.md).

### Render a plot

```python
--8<-- "client_integrations.py:render"
```

`plots.render` takes a PlotSpec, a saved plot (`saved_plot_id=`) or a model version's plot,
waits for the sandboxed render like a score, and `download_render` fetches the file
(CRC32C-checked). `plots.save`, `saved`, `get_saved` and `delete_saved` manage saved plots;
`create_embed` returns a read-only embed link once (`embeds`, `revoke_embed`, and the public
`embed(token)` view). See [Sharing plots](sharing-plots.md).

### Sign-up, verification, deletion and export

```python
--8<-- "client_integrations.py:export"
```

`download_export` waits for the export to be ready, downloads it through the storage client
(never with your API credentials) to a temporary file and renames it into place once its
size matches. `api.tenants` also has `verification`, `request_deletion(tenant_id,
confirm=tenant_id)`, `deletion` and `cancel_deletion`; `api.signup(...)` and
`api.verify_email(token)` are the public sign-up calls (sent without credentials). Operators
have `api.admin.signups`, `review_signup`, `set_verification` and `retry_deletion`. See
[Deleting and exporting data](data-deletion.md).

### Guided mode

`api.guided` drives [Guided mode](../concepts/guided-mode.md) from code: `templates()` and
`template(id)`, `detect_roles(template_id, columns=[…] | dataset="dataset://…")`,
`draft(template_id=…, name=…, dataset=…, answers={…})` (the ingest mapping, the `ModelSpec`
and its YAML; nothing is stored), `compile({...})` (the `ScoreRequest` a what-if, forecast
or preset stands for) and `preview_priors(business_priors, spec_id=…, revision=…)` (a
sandboxed `translate_priors` run, waited for).

## Workflows, governance and explorers <span class="mapi-badge">v0.5</span> { #workflows-governance-and-explorers }

Everything v0.5 adds is on the same client, typed with the `mapi_spec` DTOs. The walkthrough
below runs, unchanged, against a real server in the documentation tests.

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

`stream_events` yields `WorkflowSnapshot` (the run with every node, first on each
connection), `WorkflowEventFrame` (node queued, started, succeeded, cached, skipped, failed or
retrying; branch decided) and `WorkflowEnd`; a dropped connection is resumed with
`Last-Event-ID`. `api.workflows.wait(run_id)` polls instead, and raises the failing node's
error.

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

`AsyncClient` has the same resources and arguments; calls are awaited and lists are
iterated with `async for`:

```python
--8<-- "client_tour.py:async"
```

The v0.5 resources work the same way:

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

List calls return lazy paginators that follow `next_cursor`. `api.health()` and `api.ready()`
call `/healthz` and `/readyz`; `api.me()` calls `/v1/me` and `api.use(project)` changes the
selected project; `api.signup()`, `api.verify_email()` and `api.endpoint(alias)` are v0.4.
`mapi_client.webhooks` verifies deliveries and `mapi_client.xrjson` encodes inline payloads
(budgets are optimised by the platform: see [Optimise a budget](../tutorials/optimise-a-budget.md)). The DTOs are documented in
[`mapi_spec`](../reference/api/spec.md#rest-dtos) and the endpoints in the
[HTTP API reference](../reference/http-api.md).
