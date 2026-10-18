---
description: Serve a model version under a stable alias, score it from scripts and spreadsheets, roll it back, and get signed webhooks when runs finish or endpoints change.
---

# Endpoints and webhooks <span class="mapi-badge">v0.4</span>

An **endpoint** gives a model version a stable name, `endpoint://<project>/<alias>`. Other
applications call this name. You can promote a better version, or roll back a bad version,
and the applications do not change. A **webhook** tells another application when an event
occurs in a project. For example, a run finished, a model version is ready, or an endpoint
changed.

This guide uses `curl` for endpoints and webhooks. [Integrations](../concepts/integrations.md)
gives the concepts: the promotion gate, signatures and the SSRF policy. The
[HTTP API reference](../reference/http-api.md#guided-and-integrations) lists all the
operations.

In the examples, `MAPI_TOKEN` holds an API token and `MAPI_PROJECT` holds the
`<tenant>/<project>` for the token. `API` holds the base URL:

```bash
export API=http://127.0.0.1:8000
api() { curl -sS -H "Authorization: Bearer $MAPI_TOKEN" -H "X-Mapi-Project: $MAPI_PROJECT" \
  -H 'Content-Type: application/json' "$@"; }
```

## Promote a model version

`PUT /v1/endpoints/{alias}` makes the alias, or points the alias at a different version. Give
the version by reference or by id:

```bash
api -X PUT "$API/v1/endpoints/geo-mmm-prod" \
  --data '{"model": "model://growth/eu-geo-mmm@v3", "expected_revision": 0}'
```

```json
{
  "alias": "geo-mmm-prod",
  "ref": "endpoint://growth/geo-mmm-prod",
  "model_ref": "model://growth/eu-geo-mmm@v3",
  "revision": 1,
  "evaluation_policy": {"fit_mode": "evaluate_then_refit", "schema_change": "block"},
  "gate": {"outcome": "allowed", "scorecard_overall": "pass", "reasons": []},
  "overridden": false
}
```

`expected_revision` makes the call safe if a colleague promotes at the same time:

- `0` means "the alias must be new".
- `N` means "the alias must still be at revision N".
- If the alias does not agree, the answer is `409 CONFLICT`.

If you send the same body again, nothing changes.

The **promotion gate** decides if the version can serve:

| The version | Answer |
|---|---|
| Its scorecard passes, the fit used `evaluate_then_refit`, and it takes the input that the callers of the endpoint send | Promoted |
| Its scorecard warns, or it has no scorecard. Or it had no score on a holdout (no `data.split`). Or a check that its guided template asks for did not run (`NOT_EVALUATED`) | `409 OVERRIDE_REQUIRED`, unless a project admin overrides |
| Its scorecard fails, the fit stopped before it finished, or the fit used `train_only` | `409 PROMOTION_BLOCKED` |
| It expects different input (for example, a removed dim or a new necessary variable) | Blocked by default. The `schema_change` policy of the endpoint can change this to `warn` or `allow` |

A less strict `evaluation_policy` on the endpoint stops the gate, as an override does. For
example, `schema_change` from `block` to `warn` or `allow`, or `fit_mode` to `train_only`. Thus
only a signed-in project admin can send a less strict policy. An `endpoints:promote` token gets
`403 FORBIDDEN`. The token can send the same policy or a stricter policy. The audit event of
the promotion records the policy before and after.

Each refusal gives the reasons of the gate in the `errors` of the problem. Each reason has the
scorecard check that caused it:

```json
{
  "code": "OVERRIDE_REQUIRED",
  "status": 409,
  "errors": [
    {"code": "OVERRIDE_REQUIRED", "severity": "warning",
     "message": "Holdout error: The forecast error on the holdout periods was 14%.",
     "context": {"gate_code": "SCORECARD_WARN", "check": "holdout.mape"}}
  ]
}
```

**Evaluated, then refitted.** An endpoint serves versions with a fit that evaluated the
holdout and then fitted again on all the data. This is `fit.mode: evaluate_then_refit`, the
default of all specs. Thus forecasts start from the full history. The two steps occur in the
same fit run. The lineage of the version links the evaluation. To serve an exploration fit,
give the endpoint the policy `{"fit_mode": "train_only"}`.

### Overriding

An override is a human decision. A signed-in **project admin** sends `override: true` with a
reason. The admin sends it from the web app or from a session. An API token cannot override,
for all scopes:

```json
{"model_version_id": "mv_01jaf3e1f2g3h4j5k6m7n8p9q0", "override": true,
 "reason": "Holdout MAPE is high because of the strike in week 41; accepted by the CMO."}
```

The endpoint history and the audit log (`endpoint.promote_override`) keep the reason. The
reason is next to the checks that warned.

### History and rollback

```bash
api "$API/v1/endpoints/geo-mmm-prod/history"
api -X POST "$API/v1/endpoints/geo-mmm-prod/rollback" \
  --data '{"reason": "v4 over-forecasts after the price change"}'
```

A rollback points the alias at the version of an earlier revision. By default, this is the
last revision that served a different version. `to_revision` selects a revision. A rollback is
a new revision, and the audit log records it with its reason. The gate does not run again.
`DELETE /v1/endpoints/{alias}` removes the alias and its history. The model versions stay.

## Score an endpoint

Ask the endpoint what it expects. The answer has the input schema from the time of the
promotion. It also has an example body that is valid:

```bash
api "$API/v1/endpoints/geo-mmm-prod/schema" | jq .example
```

```json
{"mode": "forecast", "horizon": {"dim": "time", "periods": 8},
 "future_covariates": "carry_last", "output": {"kind": "expected", "summary": ["mean", "hdi_90"]}}
```

Then score the endpoint. `Prefer: wait=60` waits for a maximum of one minute:

- If the run finished, the answer is `200` with links to the outputs.
- If the run did not finish, the answer is `202` with the run.

A finished answer also has the numbers in `summary`. There is one table for each output. The
table has the coordinate columns, then one column for each statistic. By default, the
statistics are the mean and the 90% HDI. Thus a spreadsheet or a shell script does not need a
zarr or Arrow reader. If a result has more than 100,000 cells, the server cuts it (`truncated`).
`score.zarr.zip` always has all the data.

```json
"summary": {"tables": [{"variable": "revenue",
                        "columns": ["geo", "time", "mean", "hdi_90_lower", "hdi_90_upper"],
                        "rows": [["de", "2026-01-05", 1520.4, 1402.1, 1633.9]]}],
            "truncated": false}
```

```bash
api -X POST "$API/v1/endpoints/geo-mmm-prod/score" \
  -H 'Prefer: wait=60' -H 'Idempotency-Key: plan-2026-11' \
  --data '{"mode": "forecast", "horizon": {"dim": "time", "periods": 8},
           "future_covariates": "carry_last"}'
```

To get a result that took more time, **send the same call again with the same
`Idempotency-Key`**. The answer is for the run that the first call started. If you ask, the
call waits again. This works with the smallest token, `score:<alias>`. This token can read and
score one endpoint and nothing more.

### Inline data

`data` and `scenario` can be `dataset://` references. They can also be small datasets that you
send inline as XrJSON. XrJSON has the shape of `xr.Dataset.to_dict()`, with a maximum of 5 MB
each. Thus scripts and spreadsheets do not need an upload.

A caller that cannot read datasets (a `score:<alias>` token) sends data inline. Or it gives the
dataset that the model of the endpoint was fitted on. All other references get the same
`403 FORBIDDEN`. A reference to a dataset that does not exist also gets `403 FORBIDDEN`. Thus
such a token cannot score the other datasets of the project. It also cannot find which datasets
exist:

```json
{
  "mode": "predict",
  "data": {
    "coords": {
      "market": {"dims": ["market"], "data": ["de"]},
      "channel": {"dims": ["channel"], "data": ["search", "tv"]},
      "date": {"dims": ["date"], "data": ["2025-12-30"]}
    },
    "data_vars": {
      "spend_local": {"dims": ["market", "channel", "date"], "data": [[[1200.0], [5300.0]]]}
    }
  }
}
```

Before a run starts, the server checks the payload against the input contract of the endpoint:

- Each variable must be a variable that the endpoint takes, with its dims.
- Coordinates must be dims of the input.
- A `predict` call must send all the inputs.

Then the server does the usual [coordinate checks](../concepts/coord-plans.md): modes, horizon,
unseen levels and future covariates. For these checks, it reads only the coordinates of the
payload. The sandboxed score run decodes the values. The API never decodes them. The score run
gives each coordinate the type of the dim of the model. Dates arrive as ISO strings. Thus an
inline payload gives the same score as the same rows in an uploaded dataset.

The input contract (`GET …/schema` → `snapshot.input_schema`) uses the dim and variable names
that the spec uses after `data.bind`. For example, if the spec binds `time: week` and
`y: revenue`, an inline payload sends a `time` dim, not `week`. The score run changes the
payload names to the names of the dataset before it scores. The results use the names of the
dataset, as for all scores. `POST …/validate` does the same checks and answers `200` with the
issues. It does not run anything.

The server refuses bodies of more than 10.25 MB with `413 PAYLOAD_TOO_LARGE`. For larger data,
upload a dataset and give its reference.

### Forecasts with your latest actuals

A forecasting endpoint takes two things in one call, without a new fit
([Forecasts with actuals](../concepts/forecasting-with-actuals.md)):

- The actuals that arrived after the fit.
- The dates to forecast.

Send inline data along the time dim. Fill the target on the periods that you have. Leave the
target empty (`null`) on the periods to forecast.

```bash
api -X POST "$API/v1/endpoints/ts-forecast/score" -H 'Prefer: wait=60' \
  --data '{"mode": "forecast", "horizon": {"dim": "t", "periods": 8},
           "future_covariates": "carry_last",
           "data": {"coords": {"t": {"dims": ["t"], "data": ["1961-01-01", "1961-02-01",
                                     "1961-03-01", "1961-04-01", "1961-05-01"]}},
                    "data_vars": {"y": {"dims": ["t"], "data": [417, 391, 419, null, null]}}}}'
```

This call updates the model with January to March. It forecasts April and May, the two empty
rows. A warning tells you that the server did not use `horizon.periods`. If you send only the
three actuals, the server forecasts the 8 periods after March. The `summary.conditioning` of
the answer tells what occurred. It tells this in fields (`conditioned`, `n_actuals`,
`actuals_through`, `fit_end`, `horizon_source`, details for each series) and in words
(`message`).

`GET …/schema` gives the same table for spreadsheets and scripts:

- `input_table` lists the columns: the time column first, then the target, the drivers and
  other inputs. The periods after the fit are empty rows.
- `snapshot.conditions_on_actuals` tells if the model can use actuals.
- `actuals_example` is a call over those periods that is ready for you to fill.

If a model cannot use actuals, it refuses target values in a forecast with
`422 ACTUALS_NOT_SUPPORTED`. It does not ignore them. For inline data, it refuses before a run
starts. Actuals get `422 ACTUALS_INVALID` if they have a gap that the model cannot filter. They
also get it if they do not have the drivers that were observed with them.

In Python, `api.endpoints.forecast("ts-forecast", rows)` takes the same table as a DataFrame or
a list of rows. You can also give `actuals=` and `future=` separately.
`result.conditioning.message` tells what the forecast used. In a terminal,
`mapi endpoints forecast ts-forecast input.csv` reads the table from a CSV file
([The command line](cli.md#endpoints-webhooks-and-sharing)). In Google Sheets, the table is the
**Input** sheet ([Connect Google Sheets](../tutorials/google-sheets.md#5-add-your-latest-actuals-and-future-dates)).

### Snippets

```bash
api "$API/v1/endpoints/geo-mmm-prod/snippets?language=python&language=curl"
```

This call gives code that is ready to run for these languages:

- Python
- TypeScript
- curl
- R
- Google Sheets (Apps Script)
- A notebook cell.

The code has the address of this server and the example payload of the endpoint. For a
forecasting endpoint, it also has a call over the periods after the fit, for you to fill with
actuals. It also has an Apps Script that reads the actuals from an **Input** sheet.
`base_url=` puts a different address in the code, for example a public address behind a proxy.
Snippets read the token from `MAPI_TOKEN` and never contain a secret.

In Python, `Client().endpoint("geo-mmm-prod").score(ds)` sends an `xr.Dataset` and returns an
`xr.Dataset` ([The Python client](python-client.md#endpoints-webhooks-and-sharing)). The CLI
has `mapi endpoints score`. To optimise a budget, use the model version
([Optimise a budget](../tutorials/optimise-a-budget.md)).

### Tokens for integrations

| Scope | For |
|---|---|
| `score:geo-mmm-prod` | An application that scores this endpoint. It can also read the schema and snippets of the endpoint |
| `score:*` | All endpoints and models of the project |
| `endpoints:promote` | CI that promotes versions that pass and rolls back. It never overrides |
| `webhooks:manage` | A script that manages webhooks |

Each call counts against the rate limit of the token (`429 RATE_LIMITED` with `Retry-After`).
Each score run counts against the vCPU-second budget of the token (`429 QUOTA_EXCEEDED`).
These limits are in addition to the limits of the tenant.

## Webhooks

### Subscribe

```bash
api -X POST "$API/v1/webhooks" \
  --data '{"url": "https://hooks.example.com/mapi",
           "events": ["run.succeeded", "run.failed", "endpoint.promoted"]}'
```

The answer holds the signing secret, `whsec_…`. **You see it only one time.** Keep the secret
with the receiver. To manage webhooks, you need the project admin role, or a `webhooks:manage`
token of an admin. The reason is that deliveries show what a project does.

The URL must be `https` on port 443 or 8443, and it must give a public host. The server
resolves the host. It refuses (`422 WEBHOOK_URL_BLOCKED`) a host with an internal address. This
includes private, loopback, link-local and metadata addresses. The server checks again at each
delivery. It connects only to the address that it checked. Thus a DNS answer that changes
later cannot send a delivery into the platform.

### What a delivery looks like

```http
POST /mapi HTTP/1.1
Host: hooks.example.com
Content-Type: application/json
User-Agent: mapi-webhooks/1
Mapi-Signature: t=1792000000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd
Mapi-Event-Id: evt_01jaf3k1m2n3p4q5r6s7t8v9w0
Mapi-Event-Type: run.succeeded
Mapi-Webhook-Id: whk_01jaf3h1j2k3m4n5p6q7r8s9t0
Mapi-Delivery-Id: whd_01jaf3m1n2p3q4r5s6t7v8w9x0

{"api_version":"v1","created":"2026-10-15T09:30:00Z","data":{"kind":"fit","model_version_id":"mv_01jaf3e1f2g3h4j5k6m7n8p9q0","run_id":"run_01jaf3q2x8m9v7k5h4g3f2e1d0","state":"succeeded"},"id":"evt_01jaf3k1m2n3p4q5r6s7t8v9w0","project":"growth","tenant":"acme","type":"run.succeeded"}
```

| Event | `data` |
|---|---|
| `run.succeeded`, `run.failed` | `run_id`, `kind`, `state`, `model_version_id`. Failures also have `failure_class` and `code` |
| `model.version.ready` | `model_id`, `model_version_id`, `ref`, `scorecard_overall` |
| `endpoint.promoted` | `alias`, `ref`, `revision`, `model_version_id`, `overridden`, `action` (`promote` or `rollback`) |
| `score.completed` | `run_id`, `model_version_id`, `outputs` (names), and `endpoint` (`alias`, `ref`, `revision`) if the score came through an endpoint |
| `tenant.deletion.completed` | `deletion_id`, `requested_at`, `completed_at` |

The server records each event in the same database transaction as its cause. Thus the server
sends an event only if the event occurred, and always if it occurred. Delivery is *at least
once*. Use `Mapi-Event-Id` to remove duplicates. Envelopes have ids and states. They never have
data, signed URLs or secrets. Use the API to get the results.

**No plot attachments.** Webhooks do not attach rendered plots. The platform plan had them as
an option. A receiver is a third party on the internet. It is frequently a chat or automation
service that logs or sends on what it gets. Thus a chart in the body, or a signed link to a
chart, would publish model results outside the project. If a receiver wants a chart, it renders
the chart with its own token when the event arrives:

1. On `model.version.ready`, call `POST /v1/plots:render` with `Prefer: wait=60` and this body:
   `{"source": {"model_id": ..., "version": ..., "plot_id": "core.ppc_fan"}, "format": "png"}`.
2. Download the PNG from the link in the answer.

Render runs need a token with `runs:create`.

### Verify it

1. Calculate the HMAC again over the **raw** body, before you parse the body.
2. Compare the values in constant time.
3. Refuse timestamps that are more than five minutes from your clock.

=== "Python"

    ```python
    from mapi_client.webhooks import verify
    from mapi_spec.errors import MapiError

    def handle(raw_body: bytes, headers: dict[str, str]) -> int:
        try:
            event = verify(headers, raw_body, SECRET)  # a WebhookEnvelope
        except MapiError:
            return 401
        ...
    ```

    [Receiving webhooks](webhook-receivers.md) has complete FastAPI and Flask receivers.

=== "Node"

    ```js
    import crypto from "node:crypto";

    export function verify(rawBody, header, secret, now = Date.now() / 1000) {
      const parts = Object.groupBy(header.split(","), (p) => p.split("=")[0]);
      const t = Number(parts.t?.[0]?.split("=")[1]);
      if (!Number.isInteger(t) || Math.abs(now - t) > 300) return false;
      const expected = crypto.createHmac("sha256", secret)
        .update(`${t}.`).update(rawBody).digest("hex");
      return (parts.v1 ?? []).some((p) => {
        const got = Buffer.from(p.split("=")[1] ?? "", "hex");
        return got.length === 32 && crypto.timingSafeEqual(got, Buffer.from(expected, "hex"));
      });
    }
    ```

Answer with a `2xx` quickly, and do the work after. The platform waits for a maximum of 5
seconds. It reads a maximum of 256 KiB of the answer. The platform does not follow redirects.

### Retries, failures and replays

If a delivery does not get a `2xx`, the platform tries again after these times:
1 minute, 5 minutes and 30 minutes. Then it tries after 2, 6, 12 and 24 hours. Each time has a maximum of 10%
jitter. There are 8 tries in total. If an event is still not delivered, it counts as one
failure of the webhook. After 20 such events in a sequence, the platform **disables** the
webhook. The webhook tells the reason (`disabled_reason`). If a receiver answers `410 Gone`,
the platform disables the webhook immediately.

The audit log records the two types of disable (`webhook.disabled`). To enable the webhook
again:

1. Repair the receiver.
2. Enable the webhook:

    ```bash
    api -X PATCH "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0" --data '{"active": true}'
    ```

    Or, in the command line, use `mapi webhooks update whk_01jaf3h1j2k3m4n5p6q7r8s9t0 --enable`.

The delivery log has each delivery with its status (`pending`, `retrying`, `succeeded`,
`failed`, `blocked`). It also has the last response code, the duration and the start of the
answer:

```bash
api "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0/deliveries?status=failed"
api -X POST "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0/deliveries/whd_01jaf3m1n2p3q4r5s6t7v8w9x0:replay"
```

A replay sends the same event again (with the same event id) as a new delivery.

### Rotate the secret

```bash
api -X POST "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0:rotate-secret"
```

This call gives the new secret, one time. For 24 hours, deliveries have two `v1` signatures,
one for each secret. Thus the receiver can change to the new secret and lose no deliveries.

## Running it

The worker (`mapi-server worker`) sends deliveries. The API never sends them. A relay loop
looks for deliveries that are due at each `MAPI_WEBHOOK_POLL_INTERVAL_S`. It gives each try to
the `webhooks` procrastinate queue. The database keeps the schedule of the retries. Thus, if a
worker stops during a delivery, the delivery is only late.

The [configuration reference](../operations/configuration.md) gives the settings.
[Observability](../operations/observability.md) lists the metrics, for example:

- `mapi_webhook_deliveries_total`
- `mapi_webhook_ssrf_blocked_total`
- `mapi_webhooks_disabled_total`
- `mapi_endpoint_promotions_total`
- `mapi_endpoint_score_requests_total`.

For development, a receiver on your own machine is not a public host. You must allow it
explicitly. The `multi-tenant` profile refuses these settings:

```bash
export MAPI_WEBHOOK_DEV_ALLOWLIST=localhost
export MAPI_WEBHOOK_ALLOW_HTTP=true   # only for the allow-listed hosts
```
