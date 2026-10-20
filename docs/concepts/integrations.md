# Integrations <span class="mapi-badge">v0.4</span>

A fitted model is useful only when other applications can use it. For example, a planning
tool scores budgets and a spreadsheet gets a forecast. A dashboard shows a chart, and a
pipeline gets a message when a fit is complete. This page tells about the integration concepts
of v0.4 and the contracts in `mapi_spec` that they use. The
[HTTP API reference](../reference/http-api.md#guided-and-integrations) gives the endpoints.
[Endpoints and webhooks](../guides/endpoints-and-webhooks.md) shows how to use them with
`curl`.

## Endpoints and aliases

An **endpoint** is a stable alias for one model version: `endpoint://growth/geo-mmm-prod`.
Integrations call the alias. Thus you can promote a better model, or roll back a bad model,
and the integrations do not change. Each promotion and rollback is a numbered revision in the
history of the endpoint. The revision records who did it and why.

```http
PUT /v1/endpoints/geo-mmm-prod
X-Mapi-Project: acme/growth

{"model": "model://growth/eu-geo-mmm@v3", "expected_revision": 3}
```

### The promotion gate { #promotion-gate }

The [sanity scorecard](guided-mode.md#scorecard) and the `EvaluationPolicy` of the endpoint
control promotion (`mapi_spec.scorecard.promotion_gate`):

| The version | Gate | Result |
|---|---|---|
| The scorecard passes | `allowed` | The platform promotes the version |
| The scorecard warns, or there is no scorecard. Or an `evaluate_then_refit` fit has a scorecard with no holdout check (no `data.split`, or no `evaluate.metrics`: the platform did not evaluate it). Or the checks of the template of a guided version did not all run (`NOT_EVALUATED`). | `needs_override` | `409 OVERRIDE_REQUIRED`, unless a signed-in project admin sends `override: true` with a `reason`. The audit log records the override (`endpoint.promote_override`). |
| The scorecard fails, or the fit stopped before it was complete (`partial`) | `blocked` | `409 PROMOTION_BLOCKED`. No override is possible. |
| The fit mode was `train_only` | `blocked` | Endpoints need `evaluate_then_refit` (Q13), because the context and terminal state must come from all the data. A policy with `fit_mode: train_only` accepts exploration fits. |
| The version needs input that the callers do not send | `blocked` (policy `schema_change: block`), `needs_override` (`warn`) or ignored (`allow`) | A dim that is removed or changed, a stricter dim policy, a new necessary input, or an input with changed dims or type (`schema_breaks`) |

An override is a human decision. Only project admins have the `manage` permission on
endpoints, and **no token scope gives it**, not even `admin`. A token with
`endpoints:promote` (for CI) can promote versions that pass and can roll back. It can never
override.

### Scoring an endpoint

`POST /v1/endpoints/{alias}/score` takes an `EndpointScoreCall`. This is a score request
without `model`. `data` and `scenario` can be `dataset://` references. They can also be small
**inline XrJSON** datasets (the `xr.Dataset.to_dict()` shape, a maximum of 5 MB). Thus
spreadsheets and scripts do not need an upload. The control plane does a check of only the
envelope of inline data (names, nesting, sizes). The sandbox decodes the values.

Send `Prefer: wait=60` to get the result in the response if the run is complete in time
(`200`). If the run is not complete in time, the response is `202` with the run to follow.
`GET …/schema` tells what to send (`EndpointSchema`). It gives the input schema that the
platform froze at promotion, the score modes, the dims and an example payload.
`POST …/validate` does a check of a call and does not run it. A forecast call can also contain
the actuals that arrived after the fit and the dates to forecast. Refer to
[Forecasts with actuals](forecasting-with-actuals.md) and the `actuals_example` of the schema.

A `score:<alias>` token can read the endpoint, its schema and its snippets, and can score it.
It can do nothing else.

## Snippets

`GET /v1/endpoints/{alias}/snippets` returns code that is ready to run. The code scores the
endpoint with its example payload. These languages are available:

- Python (`mapi-client`)
- TypeScript (`fetch`)
- curl
- R (`httr2`)
- Google Sheets (Apps Script)
- a notebook cell (`Client().endpoint(alias).score(...)`, which returns an `xarray.Dataset`).

The generators are pure functions (`mapi_spec.snippets.render_snippets`). Thus the server, the
CLI, the web app and these docs make the same code. The Integrations hub shows and copies the
snippets of the server with no changes. Snippets never contain a secret. They read the token
from `MAPI_TOKEN` (a script property in Apps Script). The generators write each value with the
literal syntax of the language. Tests parse the generated code to prove that a dangerous
payload cannot go out of its string.

```sh
curl -sS -X POST 'https://api.mapi.example/v1/endpoints/geo-mmm-prod/score' \
  -H "Authorization: Bearer $MAPI_TOKEN" \
  -H 'X-Mapi-Project: acme/growth' \
  -H 'Prefer: wait=60' \
  --data '{"mode": "forecast", "horizon": {"dim": "time", "periods": 8},
           "future_covariates": "carry_last"}'
```

## Webhooks

A webhook calls your URL when an event occurs in a project:

| Event | When |
|---|---|
| `run.succeeded`, `run.failed` | A run is complete |
| `model.version.ready` | A fit made a model version (with the result of its scorecard) |
| `endpoint.promoted` | An alias points to a new version (promotion or rollback) |
| `score.completed` | A score run is complete |
| `tenant.deletion.completed` | The platform purged the tenant (this is the last event that the tenant sends) |

Each delivery is a JSON envelope `{id, type, created, tenant, project, api_version, data}`.
Envelopes contain ids and states. They never contain data values, signed URLs or secrets. Use
the API to get the results.

### Verifying a delivery

The platform signs each delivery with the secret of the webhook. The platform shows the secret
one time, when you make the webhook or rotate its secret:

```text
Mapi-Signature: t=1792000000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd
```

`v1` is the hex HMAC-SHA256 of `"<t>.<raw body>"`. To verify a delivery, do these steps:

1. Calculate the HMAC again over the **raw** body.
2. Compare the two values in constant time.
3. Refuse timestamps that are more than 300 seconds from your clock. This limits replays.

During a rotation, the header contains one `v1` for each active secret.

```python
from mapi_spec.webhooks import verify_signature

if not verify_signature(raw_body, request.headers["Mapi-Signature"], secret):
    return 401
```

The platform tries failed deliveries again after 1 min, 5 min, 30 min, 2 h, 6 h, 12 h and
24 h. If a receiver answers `410 Gone`, the platform disables it immediately. The platform
disables a webhook after 20 failed events in sequence. The delivery log shows each attempt
(status, response code, duration). You can replay each delivery.

### SSRF protection

A webhook URL is a request that the platform makes for a tenant. Thus the server delivers only
under a strict policy (`mapi_spec.webhooks.SSRF_POLICY`):

- The URL must use `https` only, on port 443 or 8443. It must have no credentials or fragment.
  It must have a host name. An IP literal, `localhost` or an internal suffix (`.internal`,
  `.local`, ...) is not permitted.
- The server **resolves the host itself. It refuses the URL if one address** is private,
  loopback, link-local, cloud metadata (`169.254.169.254`), CGNAT, unique-local or multicast.
  It also refuses addresses that are not globally routable for other reasons. This includes
  IPv4 addresses in IPv6 addresses.
- The server then **connects to the address that it checked**. It does no second lookup, thus
  DNS rebinding cannot change the address. The server follows no redirects. It stops after 5
  seconds and reads a maximum of 256 KiB of the answer.

If a URL does not obey the policy, the server refuses it with `422 WEBHOOK_URL_BLOCKED`. A URL
can start to resolve to a blocked address later. Then its deliveries fail as `blocked`. The
audit log records this as a security signal (`webhook.url_blocked`).

## Plots: rendering, sharing and embeds

- **Saved plots** keep a plot of a model version, or a plot that you wrote, under a name.
- **Static rendering** (`POST /v1/plots:render`) makes PNG, SVG or PDF files in the sandbox
  for reports. It uses vl-convert for Vega-Lite, with no network access. Plotly needs kaleido
  and a Chrome in the runner image. Until an operator turns on Plotly, the platform refuses it
  with `MODE_NOT_SUPPORTED`.
- **Embed links** are read-only links with a token and an expiry date, for BI tools, Slack and
  Notion. The platform shows the token one time. You can revoke links. `allowed_origins`
  limits the sites that can show them in a frame. You can embed only **platform recipes**.
  These are plots that the recipes of the platform made from a model version. From v0.13, the
  `causal.*` recipes are also platform recipes. You cannot embed plots that a plugin or a user
  wrote. A sandboxed iframe can still navigate itself. Thus
  shared views run only code that the platform wrote. Requests for other plots get
  `422 EMBED_NOT_ALLOWED`. Links that expired or that you revoked answer `410 LINK_EXPIRED`.
  The link opens the embed page of the plot sandbox. This page can get only the public embed
  view of the API. Refer to [Sharing plots](../guides/sharing-plots.md).

## The trust lifecycle

Public sign-up starts with **cut line A**. Public tenants run only declarative specs and
built-in plugins. The data is not trusted, but the code is trusted.

1. **Sign-up** needs an e-mail address, a name and accepted terms. The answer is the same for
   new and known addresses. Thus nobody can use it to find accounts. A policy hook that you
   can replace refuses disposable e-mail domains.
2. **E-mail verification** uses a link that you can use one time (valid for 24 hours). It makes
   a tenant on the `public` tier with a `default` project. The new user is the admin of the
   project.
3. **Identity verification** is necessary before any uploaded code. Tenants on the `verified`
   tier (allow-listed, M4) can upload code after verification. Public tenants can upload code
   only after **cut line B** (planned). Until then, `VerificationStatus.uploaded_code_allowed` is
   false for all public tenants.
4. **Deleting a tenant** starts a grace period of 7 days. During this period, the tenant is
   suspended (read-only) and you can cancel the deletion. Then the platform purges each store:
   database rows, objects, queued work, logs and webhooks. It shows the progress for each
   store. The platform keeps the audit log (pseudonymised) and the cost ledger (amounts only,
   for tax law) for their legal retention period.
5. A **GDPR export** puts all the data that the tenant stored into one archive. It gives a
   download link that expires after a short time. A suspended tenant can also ask for it.
