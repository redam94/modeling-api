---
description: Receive modeling-api webhooks safely - verify the Mapi-Signature HMAC over the raw body with mapi_client.webhooks.verify, refuse stale deliveries, ignore duplicates; FastAPI and Flask receivers.
---

# Receiving webhooks <span class="mapi-badge">v0.4</span>

A [webhook](endpoints-and-webhooks.md) `POST`s a small JSON envelope to your URL when a run
finishes, a model version is ready, an endpoint is promoted or a score completes. Anyone can
send your URL a request, so a receiver must check that a delivery came from the platform
before it acts on it. `mapi_client.webhooks.verify` does that check.

## What a delivery looks like

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
The envelope carries ids and states only - never data values, signed URLs or secrets - so
fetch anything else with the API.

## Verify with `mapi_client.webhooks`

```python
from mapi_client.webhooks import verify

event = verify(request_headers, raw_body, secret)  # a WebhookEnvelope, or MapiError
```

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

## FastAPI

```python
--8<-- "webhook_receiver.py:fastapi"
```

`docs/snippets/webhook_receiver.py` also posts deliveries signed exactly as the platform
signs them - and a replay, a tampered body and a stale timestamp - to this receiver in a
test.

## Flask

```python
import os

from flask import Flask, abort, request
from mapi_client.webhooks import verify
from mapi_spec.errors import MapiError

app = Flask(__name__)
SECRET = os.environ["MAPI_WEBHOOK_SECRET"]


@app.post("/mapi/webhook")
def mapi_webhook() -> dict[str, str]:
    try:
        event = verify(request.headers, request.get_data(), SECRET)
    except MapiError:
        abort(401)
    if already_handled(event.id):  # your store: deliveries are at least once
        return {"status": "duplicate"}
    handle(event)
    return {"status": "ok"}
```

`request.get_data()` returns the raw body (call it before anything reads `request.json`).

## Good receivers

- **Answer fast** with a `2xx`, then do the work in the background: the platform gives up on
  a delivery after 5 seconds and retries it (after 1 min, 5 min, 30 min, 2 h, 6 h, 12 h and
  24 h).
- **Expect duplicates.** Retries and replays (`mapi webhooks replay`) send the same event
  again; keep the `event.id`s you handled.
- **Answer `410 Gone`** to stop deliveries for good: the webhook is disabled at once. After
  20 consecutive failed events it is disabled anyway (`mapi webhooks ls` shows why;
  re-enable it with `api.webhooks.update(id, active=True)`).
- **Keep the secret secret.** It is shown once, by `mapi webhooks create` (or
  `api.webhooks.create`); store it in your secret manager. Rotate it with
  `mapi webhooks rotate <id>` and give the receiver both secrets until the old one expires.
- **Serve HTTPS on port 443 or 8443** under a public host name: the platform refuses to
  deliver to private, loopback or link-local addresses (see
  [SSRF protection](../concepts/integrations.md#ssrf-protection)).
