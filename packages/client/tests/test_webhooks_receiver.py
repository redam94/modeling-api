"""``mapi_client.webhooks.verify``: the receiver side of the ``Mapi-Signature`` scheme, and
the FastAPI and Flask receivers the docs show."""

from __future__ import annotations

import importlib.util
import json
import time
from typing import Any

import pytest
from mapi_client.webhooks import is_valid, verify
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.webhooks import WebhookEnvelope, generate_webhook_secret, signature_header

try:  # FastAPI resolves the handler's annotations from the module's globals
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover - fastapi is optional
    FastAPI = None  # type: ignore[assignment,misc]

SECRET = generate_webhook_secret()
OLD = generate_webhook_secret()
EVENT = WebhookEnvelope.model_json_schema()["examples"][0]
BODY = json.dumps(EVENT, sort_keys=True).encode()


def headers(body: bytes = BODY, secrets: list[str] | None = None, **extra: str) -> dict[str, str]:
    return {
        "Mapi-Signature": signature_header(body, secrets or [SECRET]),
        "Mapi-Event-Id": EVENT["id"],
        "Mapi-Event-Type": EVENT["type"],
        "Content-Type": "application/json",
        **extra,
    }


def test_a_signed_delivery_verifies_and_parses() -> None:
    event = verify(headers(), BODY, SECRET)
    assert event.id == EVENT["id"] and event.type.value == "run.succeeded"
    assert event.data["run_id"] == EVENT["data"]["run_id"]
    assert verify(headers(), BODY.decode(), SECRET).id == EVENT["id"]  # str bodies too


def test_header_names_are_case_insensitive_and_pairs_work() -> None:
    lower = {k.lower(): v for k, v in headers().items()}
    assert verify(lower, BODY, SECRET).id == EVENT["id"]
    assert verify(list(headers().items()), BODY, SECRET).id == EVENT["id"]


def test_rotation_accepts_either_secret() -> None:
    both = headers(secrets=[SECRET, OLD])
    assert is_valid(both, BODY, OLD)
    assert is_valid(both, BODY, [SECRET])
    assert is_valid(headers(secrets=[OLD]), BODY, [SECRET, OLD])


@pytest.mark.parametrize(
    ("hdrs", "body", "secret"),
    [
        (headers(), BODY + b" ", SECRET),  # re-serialised / tampered body
        (headers(), BODY, OLD),  # wrong secret
        ({"Mapi-Event-Id": EVENT["id"]}, BODY, SECRET),  # no signature
        (headers() | {"Mapi-Signature": "t=1,v1=zz"}, BODY, SECRET),  # malformed
        (headers() | {"Mapi-Event-Id": "evt_01jaf3k1m2n3p4q5r6s7t8v9w9"}, BODY, SECRET),
    ],
    ids=["tampered", "wrong-secret", "missing", "malformed", "event-id-mismatch"],
)
def test_bad_deliveries_are_refused(hdrs: dict[str, str], body: bytes, secret: str) -> None:
    with pytest.raises(MapiError) as info:
        verify(hdrs, body, secret)
    assert info.value.code is ErrorCode.WEBHOOK_SIGNATURE_INVALID
    assert not is_valid(hdrs, body, secret)


def test_old_signatures_are_refused() -> None:
    stale = {"Mapi-Signature": signature_header(BODY, [SECRET], timestamp=int(time.time()) - 301)}
    assert not is_valid(stale, BODY, SECRET)
    assert is_valid(stale, BODY, SECRET, tolerance=600)
    assert is_valid(stale, BODY, SECRET, now=int(time.time()) - 300)


def test_a_signed_body_that_is_not_an_envelope_is_refused() -> None:
    body = b'{"hello": "world"}'
    with pytest.raises(MapiError, match="not a webhook envelope"):
        verify({"Mapi-Signature": signature_header(body, [SECRET])}, body, SECRET)
    with pytest.raises(ValueError):
        verify(headers(), BODY, "")
    huge = b"x" * (256 * 1024 + 1)
    assert not is_valid({"Mapi-Signature": signature_header(huge, [SECRET])}, huge, SECRET)


@pytest.mark.skipif(FastAPI is None, reason="needs fastapi")
def test_the_fastapi_receiver_from_the_docs() -> None:
    app = FastAPI()
    seen: set[str] = set()
    handled: list[Any] = []

    @app.post("/mapi/webhook")
    async def receive(request: Request) -> dict[str, bool]:
        try:
            event = verify(request.headers, await request.body(), SECRET)
        except MapiError:
            raise HTTPException(status_code=401, detail="bad signature") from None
        if event.id not in seen:  # deliveries are at least once
            seen.add(event.id)
            handled.append(event.type)
        return {"ok": True}

    with TestClient(app) as http:
        assert http.post("/mapi/webhook", content=BODY, headers=headers()).status_code == 200
        assert http.post("/mapi/webhook", content=BODY, headers=headers()).status_code == 200
        assert http.post("/mapi/webhook", content=b"{}", headers=headers()).status_code == 401
    assert handled == ["run.succeeded"]


@pytest.mark.skipif(importlib.util.find_spec("flask") is None, reason="needs flask")
def test_the_flask_receiver_from_the_docs() -> None:  # pragma: no cover - flask is optional
    from flask import Flask, abort, request

    app = Flask(__name__)

    @app.post("/mapi/webhook")
    def receive() -> dict[str, bool]:
        try:
            verify(request.headers, request.get_data(), SECRET)
        except MapiError:
            abort(401)
        return {"ok": True}

    http = app.test_client()
    assert http.post("/mapi/webhook", data=BODY, headers=headers()).status_code == 200
    assert http.post("/mapi/webhook", data=b"{}", headers=headers()).status_code == 401
