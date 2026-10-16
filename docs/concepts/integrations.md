# Integrations <span class="mapi-badge">v0.4</span>

A fitted model is only useful once other applications can use it: a planning tool scores
budgets, a spreadsheet pulls a forecast, a dashboard embeds a chart, a pipeline hears when a
fit finishes. This page explains the integration concepts of v0.4 and the contracts in
`mapi_spec` behind them; the endpoints are in the
[HTTP API reference](../reference/http-api.md#guided-and-integrations).

## Endpoints and aliases

An **endpoint** is a stable alias for one model version: `endpoint://growth/geo-mmm-prod`.
Integrations call the alias, so a better model can be promoted - or a bad one rolled back -
without touching them. Every promotion and rollback is a numbered revision in the endpoint's
history, with who did it and why.

```http
PUT /v1/endpoints/geo-mmm-prod
X-Mapi-Project: acme/growth

{"model": "model://growth/eu-geo-mmm@v3", "expected_revision": 3}
```

### The promotion gate { #promotion-gate }

Promotion is gated on the [sanity scorecard](guided-mode.md#scorecard) and the endpoint's
`EvaluationPolicy` (`mapi_spec.scorecard.promotion_gate`):

| The version | Gate | What happens |
|---|---|---|
| Scorecard passes | `allowed` | Promoted |
| Scorecard warns, or has no scorecard | `needs_override` | `409 OVERRIDE_REQUIRED` unless a signed-in project admin sends `override: true` with a `reason`; the override is audited (`endpoint.promote_override`) |
| Scorecard fails, the fit was interrupted (`partial`) | `blocked` | `409 PROMOTION_BLOCKED`; no override |
| Fitted `train_only` | `blocked` | Endpoints need `evaluate_then_refit` (Q13): context and terminal state come from all the data. A policy with `fit_mode: train_only` accepts exploration fits |
| Expects input the callers do not send | `blocked` (policy `schema_change: block`), `needs_override` (`warn`) or ignored (`allow`) | A dim removed or changed, a tightened dim policy, a new required input, an input whose dims or type changed (`schema_breaks`) |

Overriding is a human decision: the `manage` permission on endpoints belongs to project admins
and **no token scope grants it**, not even `admin`. An `endpoints:promote` token (for CI) can
promote passing versions and roll back, never override.

### Scoring an endpoint

`POST /v1/endpoints/{alias}/score` takes an `EndpointScoreCall`: a score request without
`model`. `data` and `scenario` may be `dataset://` references or small **inline XrJSON**
datasets (`xr.Dataset.to_dict()` shape, up to 5 MB), so spreadsheets and scripts need no
upload. The control plane only checks the envelope of inline data (names, nesting, sizes); the
values are decoded in the sandbox. Send `Prefer: wait=60` to get the result in the response
when the run finishes in time (`200`), otherwise `202` with the run to follow.
`GET …/schema` describes what to send (`EndpointSchema`: the input schema frozen at promotion,
score modes, dims and an example payload) and `POST …/validate` checks a call without
running it.

A `score:<alias>` token can read the endpoint, its schema and snippets, and score it - and
nothing else.

## Snippets

`GET /v1/endpoints/{alias}/snippets` returns ready-to-run code that scores the endpoint with
its example payload, in Python (`mapi-client`), TypeScript (`fetch`), curl, R (`httr2`),
Google Sheets (Apps Script) and Atlas (`RemoteModel`). The generators are pure functions
(`mapi_spec.snippets.render_snippets`), so the server, the CLI and these docs produce the same
code. Snippets never contain a secret: they read the token from `MAPI_TOKEN` (a script property
in Apps Script). Every value is embedded with the language's own literal syntax, and tests
parse the generated code back to prove a hostile payload cannot escape its string.

```sh
curl -sS -X POST 'https://api.mapi.example/v1/endpoints/geo-mmm-prod/score' \
  -H "Authorization: Bearer $MAPI_TOKEN" \
  -H 'X-Mapi-Project: acme/growth' \
  -H 'Prefer: wait=60' \
  --data '{"mode": "forecast", "horizon": {"dim": "time", "periods": 8},
           "future_covariates": "carry_last"}'
```

## Webhooks

A webhook calls your URL when something happens in a project:

| Event | When |
|---|---|
| `run.succeeded`, `run.failed` | A run finished |
| `model.version.ready` | A fit produced a model version (with its scorecard verdict) |
| `endpoint.promoted` | An alias points at a new version (promotion or rollback) |
| `score.completed` | A score run finished |
| `tenant.deletion.completed` | The tenant was purged (the last event it sends) |

Each delivery is a JSON envelope `{id, type, created, tenant, project, api_version, data}`.
Envelopes carry ids and states, never data values, signed URLs or secrets - fetch results
with the API.

### Verifying a delivery

Deliveries are signed with the webhook's secret (shown once, when the webhook is created or
its secret rotated):

```text
Mapi-Signature: t=1792000000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd
```

`v1` is the hex HMAC-SHA256 of `"<t>.<raw body>"`. Recompute it over the **raw** body, compare
in constant time, and refuse timestamps more than 300 seconds from your clock - that bounds
replays. During a rotation the header carries one `v1` per active secret.

```python
from mapi_spec.webhooks import verify_signature

if not verify_signature(raw_body, request.headers["Mapi-Signature"], secret):
    return 401
```

Failed deliveries are retried after 1 min, 5 min, 30 min, 2 h, 6 h, 12 h and 24 h. A receiver
that answers `410 Gone` is disabled at once, and a webhook is disabled after 20 consecutive
failed events. The delivery log shows every attempt (status, response code, duration) and any
delivery can be replayed.

### SSRF protection

A webhook URL is a request the platform makes on a tenant's behalf, so the server only
delivers under a strict policy (`mapi_spec.webhooks.SSRF_POLICY`):

- `https` only, on port 443 or 8443, with no credentials or fragment in the URL, and a host
  name - never an IP literal, `localhost` or an internal suffix (`.internal`, `.local`, ...);
- the server **resolves the host itself and refuses if any address** is private, loopback,
  link-local, cloud metadata (`169.254.169.254`), CGNAT, unique-local, multicast or otherwise
  not globally routable - including IPv4 addresses embedded in IPv6;
- it then **connects to the address it checked** (no second lookup, so DNS rebinding cannot
  swap it), follows no redirects, gives up after 5 seconds and reads at most 256 KiB of the
  answer.

A URL that breaks the policy is refused with `422 WEBHOOK_URL_BLOCKED`; one that starts
resolving to a blocked address later fails its deliveries as `blocked` and is audited as a
security signal (`webhook.url_blocked`).

## Plots: rendering, sharing and embeds

- **Saved plots** keep a model version's plot, or a plot you wrote, under a name.
- **Static rendering** (`POST /v1/plots:render`) produces PNG, SVG or PDF in the sandbox
  for reports: vl-convert for Vega-Lite, with network access off; Plotly needs kaleido and a
  Chrome in the runner image and is refused with `MODE_NOT_SUPPORTED` until an operator
  enables it.
- **Embed links** are tokenised, read-only and expiring, for BI tools, Slack and Notion. The
  token is shown once, links can be revoked, and `allowed_origins` limits which sites may
  frame them. Only **platform recipes** can be embedded - plots the platform's own recipes
  rendered from a model version - never plots written by a plugin or a user: a sandboxed
  iframe can still navigate itself, so shared views only ever run code the platform wrote.
  Requests for anything else get `422 EMBED_NOT_ALLOWED`; expired or revoked links answer
  `410 LINK_EXPIRED`. The link opens the plot sandbox's embed page, which may fetch nothing
  but the API's public embed view. See [Sharing plots](../guides/sharing-plots.md).

## The trust lifecycle

Public sign-up opens with **cut line A**: public tenants run declarative specs and built-in
plugins only - untrusted data, trusted code.

1. **Sign-up** takes an e-mail, a name and accepted terms. The answer is the same for new and
   known addresses, so it cannot be used to find accounts; disposable e-mail domains are
   refused through a pluggable policy hook.
2. **E-mail verification** with a single-use link (valid 24 hours) creates a `public`-tier
   tenant with a `default` project, the new user as its admin.
3. **Identity verification** is required before any uploaded code: tenants on the `verified`
   tier (allow-listed, M4) once verified, public tenants only after **cut line B** (M5). Until
   then `VerificationStatus.uploaded_code_allowed` is false for every public tenant.
4. **Deleting a tenant** is scheduled with a 7-day grace period in which the tenant is
   suspended (read-only) and the deletion can be cancelled. Then every store is purged -
   database rows, objects, queued work, logs and webhooks - with progress shown per store.
   The audit log (pseudonymised) and the cost ledger (amounts only, for tax law) are kept for
   their legal retention.
5. A **GDPR export** packages everything the tenant stored into one archive with a
   short-lived download link. A suspended tenant can still ask for it.
