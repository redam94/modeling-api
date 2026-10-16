---
description: Receive modeling-api webhooks safely - create a webhook, verify the Mapi-Signature HMAC over the raw body with mapi_client.webhooks.verify in FastAPI or Flask, refuse stale deliveries, ignore duplicates, and test it all on your laptop.
---

# Receive webhooks <span class="mapi-badge">v0.4</span>

A [webhook](endpoints-and-webhooks.md) `POST`s a small JSON envelope to your URL when a run
finishes, a model version is ready, an endpoint is promoted or a score completes. In this
tutorial you create one, write a receiver that checks every delivery really came from the
platform, and watch it react to a new model, first on your laptop. Anyone can send your URL
a request, so the check is not optional: `mapi_client.webhooks.verify` does it in one call.

## 1. Create the webhook

You need the project **admin** role (or a token with the `webhooks:manage` scope).

=== "Web app"

    **Integrations → Overview → Webhook → Set up a webhook** (or the **Webhooks** tab → **New
    webhook**): an `https` URL, a description and the events. The **signing secret is shown
    once**; **Done** stays disabled until you copy it or tick that you stored it.

    ![The signing secret, shown once, with the lines that verify a delivery](../assets/screenshots/webhook-secret.png){ .mapi-shot style="max-width: 640px" }

=== "CLI"

    ```bash
    mapi webhooks create https://hooks.acme.example/mapi \
      -e model.version.ready -e run.failed --description "Planning pipeline"
    ```

=== "Python"

    ```python
    --8<-- "client_integrations.py:webhook"
    ```

Store the secret where your receiver reads it, for example `MAPI_WEBHOOK_SECRET` in your
secret manager. The platform refuses URLs on private, loopback or link-local addresses and
ports other than 443 and 8443 (`WEBHOOK_URL_BLOCKED`); [step 4](#4-try-it-on-your-laptop)
shows how a development stack reaches a receiver on your machine.

## 2. What a delivery looks like

```http
POST /mapi/webhook HTTP/1.1
Content-Type: application/json
Mapi-Signature: t=1792000000,v1=5257a869e7ecebeda32affa62cdca3fa51cad7e77a0e56ff536d0ce8e108d8bd
Mapi-Event-Id: evt_01jaf3k1m2n3p4q5r6s7t8v9w0
Mapi-Event-Type: endpoint.promoted
Mapi-Webhook-Id: whk_01jaf3h1j2k3m4n5p6q7r8s9t0
Mapi-Delivery-Id: whd_01jaf3m1n2p3q4r5s6t7v8w9x0

{"api_version": "v1", "created": "2026-10-15T09:30:00Z", "data": {"alias": "geo-mmm-prod", ...},
 "id": "evt_01jaf3k1m2n3p4q5r6s7t8v9w0", "project": "growth", "tenant": "acme",
 "type": "endpoint.promoted"}
```

`v1` is the hex HMAC-SHA256, under the webhook's signing secret, of the timestamp `t`, a dot
and the **raw body**. While a secret is being rotated the header carries one `v1` per active
secret, so receivers that know either secret keep working for the 24 hours of the rotation.

| Event | `data` |
|---|---|
| `run.succeeded`, `run.failed` | `run_id`, `kind`, `state`, and on failure `failure_class` and `code`; `model_version_id` for a fit |
| `model.version.ready` | `model_id`, `model_version_id`, `ref`, `scorecard_overall` (`pass`, `warn` or `fail`) |
| `endpoint.promoted` | `alias`, `ref`, `revision`, `model_version_id`, `overridden` |
| `score.completed` | `run_id`, `endpoint` (when it was an endpoint call), `model_version_id`, the names of the `outputs` |
| `tenant.deletion.completed` | `deletion_id`, `requested_at`, `completed_at` |

The envelope carries ids and states only, never data values, signed URLs or secrets: fetch
anything else with the API.

## 3. Write the receiver

`verify(headers, body, secret)`:

1. finds `Mapi-Signature` in any headers object (names are case-insensitive);
2. recomputes the HMAC over the raw body and compares it with every `v1` in **constant
   time** (`secret` may be a list: the old and new secret during a rotation);
3. refuses a timestamp more than **300 seconds** from your clock, which bounds replays of a
   captured delivery (`tolerance=` changes it);
4. parses the body as a `WebhookEnvelope` and checks that `Mapi-Event-Id` names the same
   event.

Any failure raises `MapiError` with code `WEBHOOK_SIGNATURE_INVALID`; answer it with `401`.
`is_valid(...)` is the same check as a boolean. Bodies over 256 KiB are refused before
anything is parsed.

!!! warning "Use the raw body"
    Verify the bytes exactly as they arrived. A body your framework parsed and serialised
    again (`json.dumps(await request.json())`) differs in spacing or key order and never
    matches its signature.

=== "FastAPI"

    ```python
    --8<-- "webhook_receiver.py:fastapi"
    ```

=== "Flask"

    ```python
    --8<-- "webhook_receiver_flask.py:flask"
    ```

Both receivers are run by the test suite (`docs/snippets/webhook_receiver.py` and
`webhook_receiver_flask.py`), which posts deliveries signed exactly as the platform signs
them, then a replay, a tampered body and a stale timestamp.

## 4. Try it on your laptop

A `make dev` stack already trusts a receiver on your own machine: the `dev` profile writes a
throwaway CA and a certificate for `127.0.0.1` and `localhost` to
`.local/dev/webhook-tls/`, trusts that CA and allow-lists both hosts
([configuration](../operations/configuration.md)). Save the FastAPI receiver as
`receiver.py`, create a webhook for it and serve it with that certificate, from the
repository (the workspace already has FastAPI and uvicorn):

```bash
uv run mapi webhooks create https://localhost:8443/mapi/webhook \
  -e endpoint.promoted -e run.failed               # prints the secret once
export MAPI_WEBHOOK_SECRET=whsec_…                 # that secret
uv run uvicorn receiver:app --port 8443 \
  --ssl-certfile .local/dev/webhook-tls/cert.pem --ssl-keyfile .local/dev/webhook-tls/key.pem
```

Promote a fitted model to an endpoint (the [Quickstart](../getting-started/quickstart.md)
fits one; promote it in the [Integrations hub](integrations-hub.md#promoting-through-the-gate)):
the receiver prints `now serving endpoint://<project>/<alias> revision 1`. In the web app, the webhook's
**Delivery log** shows the try with your receiver's response code and time; **Send test**
replays the newest delivery, so you can check a receiver you just changed
(`mapi webhooks replay <webhook_id> <delivery_id>` does the same). The M3 end-to-end suite
receives every event type this way.

You can also post a correctly signed delivery to your receiver yourself, without a stack:
`mapi_spec.webhooks.signature_header(body, [secret])` builds the header exactly as the
platform does.

## Good receivers

- **Answer fast** with a `2xx`, then do the work in the background: the platform gives up on
  a delivery after 5 seconds and retries it (after 1 min, 5 min, 30 min, 2 h, 6 h, 12 h and
  24 h).
- **Expect duplicates.** Retries and replays send the same event again; keep the `event.id`s
  you handled.
- **Answer `410 Gone`** to stop deliveries for good: the webhook is disabled at once. After
  20 consecutive failed events it is disabled anyway (`mapi webhooks ls` shows why;
  re-enable it with `api.webhooks.update(id, active=True)`).
- **Keep the secret secret.** It is shown once; store it in your secret manager. Rotate it
  with `mapi webhooks rotate <id>` and give the receiver both secrets until the old one
  expires.
- **Serve HTTPS on port 443 or 8443** under a public host name in production: the platform
  refuses to deliver to private, loopback or link-local addresses (see
  [SSRF protection](../concepts/integrations.md#ssrf-protection)).

## Next

- React to `endpoint.promoted` by refreshing a [Google Sheet](../tutorials/google-sheets.md)
  or a cache.
- Chain a pipeline: on `model.version.ready` with `scorecard_overall: pass`, promote it with
  a token scoped `endpoints:promote` ([Endpoints and webhooks](endpoints-and-webhooks.md)).
