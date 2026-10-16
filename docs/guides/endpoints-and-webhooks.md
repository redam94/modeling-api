---
description: Serve a model version under a stable alias, score it from scripts and spreadsheets, roll it back, and get signed webhooks when runs finish or endpoints change.
---

# Endpoints and webhooks <span class="mapi-badge">v0.4</span>

An **endpoint** gives a model version a stable name, `endpoint://<project>/<alias>`, that
other applications call. A better version can then be promoted, or a bad one rolled back,
without changing any of them. A **webhook** tells another application when something happens
in a project: a run finished, a model version is ready, an endpoint changed.

This guide does both with `curl`. The concepts behind them (the promotion gate, signatures,
the SSRF policy) are in [Integrations](../concepts/integrations.md), and every operation is
listed in the [HTTP API reference](../reference/http-api.md#guided-and-integrations).

The examples assume `MAPI_TOKEN` holds an API token, `MAPI_PROJECT` the `<tenant>/<project>`
it acts in, and `API` the base URL:

```bash
export API=http://127.0.0.1:8000
api() { curl -sS -H "Authorization: Bearer $MAPI_TOKEN" -H "X-Mapi-Project: $MAPI_PROJECT" \
  -H 'Content-Type: application/json' "$@"; }
```

## Promote a model version

`PUT /v1/endpoints/{alias}` creates the alias, or points it at another version. Name the
version by reference or by id:

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

`expected_revision` makes the call safe against a colleague promoting at the same time: `0`
means "the alias must be new", `N` means "it must still be at revision N" (`409 CONFLICT`
otherwise). Sending the same body again changes nothing.

The **promotion gate** decides whether the version may serve:

| The version | Answer |
|---|---|
| Its scorecard passes, it was fitted with `evaluate_then_refit`, and it takes the input the endpoint's callers send | Promoted |
| Its scorecard warns, or it has none | `409 OVERRIDE_REQUIRED`, unless a project admin overrides |
| Its scorecard fails, the fit was interrupted, or it was fitted `train_only` | `409 PROMOTION_BLOCKED` |
| It expects different input (a dim removed, a new required variable, ...) | Blocked by default; the endpoint's `schema_change` policy can make it `warn` or `allow` |

Each refusal lists the gate's reasons in the problem's `errors`, with the scorecard check
that caused it:

```json
{
  "code": "OVERRIDE_REQUIRED",
  "status": 409,
  "errors": [
    {"code": "OVERRIDE_REQUIRED", "severity": "warning",
     "message": "Holdout error: Forecasts were off by 14%.",
     "context": {"gate_code": "SCORECARD_WARN", "check": "holdout.mape"}}
  ]
}
```

**Evaluated, then refitted.** An endpoint serves versions whose fit evaluated the holdout and
then refitted on all the data (`fit.mode: evaluate_then_refit`, the default of every spec),
so forecasts start from the full history. Both happen inside the one fit run; the version's
lineage links the evaluation. To serve an exploration fit, give the endpoint the policy
`{"fit_mode": "train_only"}`.

### Overriding

An override is a human decision. A signed-in **project admin** sends `override: true` with a
reason, from the web app or a session; no API token can override, whatever its scopes:

```json
{"model_version_id": "mv_01jaf3e1f2g3h4j5k6m7n8p9q0", "override": true,
 "reason": "Holdout MAPE is high because of the strike in week 41; accepted by the CMO."}
```

The reason is kept in the endpoint's history and in the audit log
(`endpoint.promote_override`), next to the checks that warned.

### History and rollback

```bash
api "$API/v1/endpoints/geo-mmm-prod/history"
api -X POST "$API/v1/endpoints/geo-mmm-prod/rollback" \
  --data '{"reason": "v4 over-forecasts after the price change"}'
```

A rollback points the alias at the version of an earlier revision (by default the last one
that served another version; `to_revision` picks one). It is a new revision, audited with its
reason; the gate is not run again. `DELETE /v1/endpoints/{alias}` removes the alias and its
history - model versions stay.

## Score an endpoint

Ask the endpoint what it expects. The answer has the input schema frozen at promotion and an
example body that validates:

```bash
api "$API/v1/endpoints/geo-mmm-prod/schema" | jq .example
```

```json
{"mode": "forecast", "horizon": {"dim": "time", "periods": 8},
 "future_covariates": "carry_last", "output": {"kind": "expected", "summary": ["mean", "hdi_90"]}}
```

Then score. `Prefer: wait=60` waits up to a minute: `200` with links to the outputs if the run
finished, `202` with the run otherwise.

```bash
api -X POST "$API/v1/endpoints/geo-mmm-prod/score" \
  -H 'Prefer: wait=60' -H 'Idempotency-Key: plan-2026-11' \
  --data '{"mode": "forecast", "horizon": {"dim": "time", "periods": 8},
           "future_covariates": "carry_last"}'
```

To collect a result that took longer, **send the same call again with the same
`Idempotency-Key`**: it answers for the run the first call started, waiting again if you ask.
This works with the narrowest token there is, `score:<alias>`, which can read and score one
endpoint and nothing else.

### Inline data

`data` and `scenario` can be `dataset://` references, or small datasets sent inline as XrJSON
(the shape of `xr.Dataset.to_dict()`, up to 5 MB each), so scripts and spreadsheets need no
upload:

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

Before anything runs, the server checks the payload against the endpoint's input contract -
every variable must be one the endpoint takes, with its dims; coordinates must be dims of the
input; a `predict` call must send every input - and then runs the usual
[coordinate checks](../concepts/coord-plans.md) (modes, horizon, unseen levels, future
covariates). It reads only the payload's coordinates for that: the values are decoded inside
the sandboxed score run, never by the API. The score run gives each coordinate the type of the
model's dim (dates arrive as ISO strings), so an inline payload scores exactly like the same
rows uploaded as a dataset. `POST …/validate` runs the same checks and answers
`200` with the issues, without running anything.

Bodies above 10.25 MB are refused with `413 PAYLOAD_TOO_LARGE`; upload larger data as a
dataset and reference it.

### Snippets

```bash
api "$API/v1/endpoints/geo-mmm-prod/snippets?language=python&language=curl"
```

returns ready-to-run code for Python, TypeScript, curl, R, Google Sheets (Apps Script) and
Atlas, with this server's address and the endpoint's example payload. `base_url=` puts
another address in (a public one, behind a proxy). Snippets read the token from `MAPI_TOKEN`
and never contain a secret.

### Tokens for integrations

| Scope | For |
|---|---|
| `score:geo-mmm-prod` | An application that scores this endpoint (reads its schema and snippets too) |
| `score:*` | Every endpoint and model of the project |
| `endpoints:promote` | CI that promotes passing versions and rolls back (never overrides) |
| `webhooks:manage` | Managing webhooks from a script |

Every call counts against the token's own rate limit (`429 RATE_LIMITED` with `Retry-After`)
and every score run against its vCPU-second budget (`429 QUOTA_EXCEEDED`), on top of the
tenant's.

## Webhooks

### Subscribe

```bash
api -X POST "$API/v1/webhooks" \
  --data '{"url": "https://hooks.example.com/mapi",
           "events": ["run.succeeded", "run.failed", "endpoint.promoted"]}'
```

The answer holds the signing secret, `whsec_…`. **It is the only time you see it**: store it
with the receiver. Managing webhooks needs the project admin role (or a `webhooks:manage`
token of an admin): deliveries reveal what a project is doing.

The URL must be `https` on port 443 or 8443 and name a public host: the server resolves it
and refuses (`422 WEBHOOK_URL_BLOCKED`) any host with a private, loopback, link-local,
metadata or otherwise internal address. It checks again at every delivery and connects only
to the address it checked, so a DNS answer that changes later cannot point a delivery inside
the platform.

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
| `run.succeeded`, `run.failed` | `run_id`, `kind`, `state`, `model_version_id`; failures add `failure_class` and `code` |
| `model.version.ready` | `model_id`, `model_version_id`, `ref`, `scorecard_overall` |
| `endpoint.promoted` | `alias`, `ref`, `revision`, `model_version_id`, `overridden`, `action` (`promote` or `rollback`) |
| `score.completed` | `run_id`, `model_version_id`, `outputs` (names), and `endpoint` (`alias`, `ref`, `revision`) when it came through one |
| `tenant.deletion.completed` | `deletion_id`, `requested_at`, `completed_at` |

Events are recorded in the same database transaction as what caused them, so an event is sent
if and only if it happened. Delivery is *at least once*: deduplicate on `Mapi-Event-Id`.
Envelopes carry ids and states, never data, signed URLs or secrets; fetch results with the
API.

### Verify it

Recompute the HMAC over the **raw** body before parsing it, compare in constant time, and
refuse timestamps more than five minutes away from your clock:

=== "Python"

    ```python
    from mapi_spec.webhooks import verify_signature

    def handle(raw_body: bytes, headers: dict[str, str]) -> int:
        if not verify_signature(raw_body, headers.get("Mapi-Signature"), SECRET):
            return 401
        ...
    ```

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

Answer any `2xx` quickly and do the work afterwards: the platform waits at most 5 seconds and
reads at most 256 KiB of the answer. Redirects are not followed.

### Retries, failures and replays

A delivery that does not get a `2xx` is retried after 1 minute, 5 minutes, 30 minutes, 2, 6,
12 and 24 hours (each with up to 10% jitter), 8 tries in all. An event that still could not be
delivered counts as one failure of the webhook; after 20 such events in a row the webhook is
**disabled** and says why (`disabled_reason`). A receiver that answers `410 Gone` disables it
at once. Both are audited (`webhook.disabled`). Fix the receiver, then turn it back on:

```bash
api -X PATCH "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0" --data '{"active": true}'
```

The delivery log has every delivery with its status (`pending`, `retrying`, `succeeded`,
`failed`, `blocked`), the last response code, duration and the start of the answer:

```bash
api "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0/deliveries?status=failed"
api -X POST "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0/deliveries/whd_01jaf3m1n2p3q4r5s6t7v8w9x0:replay"
```

A replay sends the same event again (same event id) as a new delivery.

### Rotate the secret

```bash
api -X POST "$API/v1/webhooks/whk_01jaf3h1j2k3m4n5p6q7r8s9t0:rotate-secret"
```

returns the new secret, once. For 24 hours deliveries carry two `v1` signatures, one per
secret, so the receiver can switch over without dropping any.

## Running it

Deliveries are sent by the worker (`mapi-server worker`), never by the API: a relay loop
looks for due deliveries every `MAPI_WEBHOOK_POLL_INTERVAL_S` and hands each try to the
`webhooks` procrastinate queue. Retries are scheduled in the database, so a worker that dies
mid-delivery only delays it. The settings are in the
[configuration reference](../operations/configuration.md); the metrics
(`mapi_webhook_deliveries_total`, `mapi_webhook_ssrf_blocked_total`,
`mapi_webhooks_disabled_total`, `mapi_endpoint_promotions_total`,
`mapi_endpoint_score_requests_total`, ...) are listed in
[Observability](../operations/observability.md).

For development, a receiver on your own machine is not a public host. Allow it explicitly -
these settings are refused in the `multi-tenant` profile:

```bash
export MAPI_WEBHOOK_DEV_ALLOWLIST=localhost
export MAPI_WEBHOOK_ALLOW_HTTP=true   # only for the allow-listed hosts
```
