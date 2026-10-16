"""A webhook receiver (Flask) that verifies ``Mapi-Signature`` with ``mapi_client.webhooks``.

Runs offline (conformance/tests/test_docs_snippets.py): deliveries signed exactly as the
server signs them (``mapi_spec.webhooks.signature_header``) are posted - signed, replayed,
tampered and stale. With Flask installed they go through Flask's test client; without it, a
stand-in with just the Flask names the receiver uses calls the view directly, so the
receiver's own logic is checked either way.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
from typing import Any

from mapi_spec.webhooks import WebhookEnvelope, generate_webhook_secret, signature_header

os.environ.setdefault("MAPI_WEBHOOK_SECRET", generate_webhook_secret())
REAL_FLASK = importlib.util.find_spec("flask") is not None


class _Abort(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


if not REAL_FLASK:  # just enough of Flask's names for the receiver below

    class _App:
        def __init__(self, name: str) -> None:
            self.views: dict[str, Any] = {}

        def post(self, rule: str) -> Any:
            def register(view: Any) -> Any:
                self.views[rule] = view
                return view

            return register

    def _abort(status: int) -> None:
        raise _Abort(status)

    sys.modules["flask"] = types.SimpleNamespace(  # type: ignore[assignment]
        Flask=_App, abort=_abort, request=types.SimpleNamespace(headers={}, get_data=bytes)
    )

# --8<-- [start:flask]
from flask import Flask, abort, request
from mapi_client.webhooks import verify
from mapi_spec.errors import MapiError

app = Flask(__name__)
SECRET = os.environ["MAPI_WEBHOOK_SECRET"]  # the secret shown once by `mapi webhooks create`
handled: set[str] = set()  # use your database: deliveries are at least once


@app.post("/mapi/webhook")
def mapi_webhook() -> dict[str, str]:
    try:  # request.get_data(): the raw body, before anything reads request.json
        event = verify(request.headers, request.get_data(), SECRET)
    except MapiError:
        abort(401)
    if event.id in handled:
        return {"status": "duplicate"}
    handled.add(event.id)
    if event.type == "model.version.ready":
        print("model ready:", event.data["ref"], "scorecard:", event.data["scorecard_overall"])
    return {"status": "ok"}
# --8<-- [end:flask]


if __name__ == "__main__":
    envelope = WebhookEnvelope.model_json_schema()["examples"][0] | {
        "type": "model.version.ready",
        "data": {"model_id": "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0", "ref": "model://growth/eu-geo-mmm@v4",
                 "model_version_id": "mv_01jaf3e1f2g3h4j5k6m7n8p9q0", "scorecard_overall": "pass"},
    }
    raw = json.dumps(envelope, sort_keys=True).encode()

    def headers(at: int | None = None) -> dict[str, str]:
        return {
            "Mapi-Signature": signature_header(raw, [SECRET], timestamp=at),
            "Mapi-Event-Id": envelope["id"],
            "Content-Type": "application/json",
        }

    def post(body: bytes, sent: dict[str, str]) -> tuple[int, Any]:
        if REAL_FLASK:
            res = app.test_client().post("/mapi/webhook", data=body, headers=sent)
            return res.status_code, res.get_json(silent=True)
        stand_in = sys.modules["flask"]
        stand_in.request = types.SimpleNamespace(headers=sent, get_data=lambda: body)  # type: ignore[attr-defined]
        globals()["request"] = stand_in.request
        try:
            return 200, mapi_webhook()
        except _Abort as exc:
            return exc.status, None

    try:
        assert post(raw, headers()) == (200, {"status": "ok"})
        assert post(raw, headers()) == (200, {"status": "duplicate"})
        assert post(raw.replace(b"pass", b"fail"), headers())[0] == 401  # tampered
        assert post(raw, headers(at=int(time.time()) - 600))[0] == 401  # stale
    finally:
        if not REAL_FLASK:
            del sys.modules["flask"]
