"""A webhook receiver (FastAPI) that verifies ``Mapi-Signature`` with ``mapi_client.webhooks``.

Runs offline (conformance/tests/test_docs_snippets.py): after the receiver, the platform's
own signing (``mapi_spec.webhooks.signature_header``) produces a delivery exactly as the
server sends it, and FastAPI's test client posts it - signed, replayed, tampered and stale.
"""

from __future__ import annotations

import json
import os
import time

from mapi_spec.webhooks import WebhookEnvelope, generate_webhook_secret, signature_header

os.environ.setdefault("MAPI_WEBHOOK_SECRET", generate_webhook_secret())

# --8<-- [start:fastapi]
from fastapi import FastAPI, HTTPException, Request
from mapi_client.webhooks import verify
from mapi_spec.errors import MapiError

app = FastAPI()
SECRET = os.environ["MAPI_WEBHOOK_SECRET"]  # the secret shown once by `mapi webhooks create`
seen: set[str] = set()  # use your database: deliveries are at least once


@app.post("/mapi/webhook")
async def mapi_webhook(request: Request) -> dict[str, str]:
    body = await request.body()  # the raw bytes: a re-serialised body fails the signature
    try:
        event = verify(request.headers, body, SECRET)
    except MapiError:
        raise HTTPException(status_code=401, detail="invalid signature") from None
    if event.id in seen:
        return {"status": "duplicate"}
    seen.add(event.id)
    if event.type == "endpoint.promoted":
        print("now serving", event.data["ref"], "revision", event.data["revision"])
    elif event.type == "run.failed":
        print("run failed:", event.data["run_id"], event.data.get("code"))
    return {"status": "ok"}
# --8<-- [end:fastapi]


if __name__ == "__main__":
    from fastapi.testclient import TestClient

    envelope = WebhookEnvelope.model_json_schema()["examples"][0] | {
        "type": "endpoint.promoted",
        "data": {"alias": "geo-mmm-prod", "ref": "endpoint://growth/geo-mmm-prod",
                 "revision": 5, "model_version_id": "mv_01jaf3e1f2g3h4j5k6m7n8p9q0",
                 "overridden": False},
    }  # fmt: skip
    raw = json.dumps(envelope, sort_keys=True).encode()

    def headers(body: bytes, at: int | None = None) -> dict[str, str]:
        return {
            "Mapi-Signature": signature_header(body, [SECRET], timestamp=at),
            "Mapi-Event-Id": envelope["id"],
            "Mapi-Event-Type": envelope["type"],
            "Content-Type": "application/json",
        }

    with TestClient(app) as http:
        assert http.post("/mapi/webhook", content=raw, headers=headers(raw)).json() == {
            "status": "ok"
        }
        again = http.post("/mapi/webhook", content=raw, headers=headers(raw))
        assert again.json() == {"status": "duplicate"}
        tampered = raw.replace(b'"revision": 5', b'"revision": 6')
        assert http.post("/mapi/webhook", content=tampered, headers=headers(raw)).status_code == 401
        stale = headers(raw, at=int(time.time()) - 600)
        assert http.post("/mapi/webhook", content=raw, headers=stale).status_code == 401
