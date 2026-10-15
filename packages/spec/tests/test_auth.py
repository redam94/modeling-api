"""BFF OIDC helpers (PKCE, return_to, cookies) and the Pub/Sub push contract."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from mapi_spec.auth import (
    LOGIN_TTL,
    SESSION_COOKIE,
    CookieSpec,
    LoginTransaction,
    PubSubPushEnvelope,
    PushAuthExpectation,
    build_authorize_url,
    check_push_claims,
    pkce_challenge,
    pkce_verifier,
    safe_return_to,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)


def test_pkce_matches_the_rfc_7636_appendix_b_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_verifiers_are_fresh_and_valid() -> None:
    a, b = pkce_verifier(), pkce_verifier()
    assert a != b and 43 <= len(a) <= 128
    assert pkce_challenge(a) != pkce_challenge(b)
    for bad in ("short", "x" * 129, "has space" + "x" * 40):
        with pytest.raises(ValueError):
            pkce_challenge(bad)
    with pytest.raises(ValueError):
        pkce_verifier(8)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "/"),
        ("/runs/run_01?tab=logs", "/runs/run_01?tab=logs"),
        ("/", "/"),
        ("//evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/", "/"),
        ("javascript:alert(1)", "/"),
        ("runs", "/"),
        ("/a\nb", "/"),
        ("/runs\n", "/"),
        ("/runs\r\nSet-Cookie: x=1", "/"),
        ("/" + "a" * 3000, "/"),
    ],
)
def test_return_to_only_accepts_same_origin_paths(value: str | None, expected: str) -> None:
    assert safe_return_to(value) == expected


def test_login_transaction_and_authorize_url() -> None:
    tx = LoginTransaction.start(now=NOW, return_to="//evil.example")
    assert tx.return_to == "/"
    assert not tx.expired(NOW + LOGIN_TTL) and tx.expired(NOW + LOGIN_TTL + timedelta(seconds=1))
    assert tx.code_verifier not in repr(tx) and tx.state not in repr(tx)
    url = build_authorize_url(
        "https://accounts.example/authorize",
        client_id="mapi-web",
        redirect_uri="https://app.example/auth/callback",
        tx=tx,
        extra={"prompt": "select_account"},
    )
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == pkce_challenge(tx.code_verifier)
    assert q["state"] == tx.state and q["nonce"] == tx.nonce
    assert q["scope"] == "openid email profile" and q["prompt"] == "select_account"
    assert "code_verifier" not in q
    with pytest.raises(ValueError, match="https"):
        build_authorize_url("http://idp/authorize", client_id="c", redirect_uri="r", tx=tx)
    with pytest.raises(ValueError, match="overridden"):
        build_authorize_url(
            "https://idp/a?x=1", client_id="c", redirect_uri="r", tx=tx, extra={"state": "x"}
        )
    with pytest.raises(ValidationError):
        LoginTransaction.model_validate({**tx.model_dump(), "return_to": "https://evil"})


def test_session_cookie_is_host_only_httponly_strict() -> None:
    header = CookieSpec.session().header("abc123")
    assert header.startswith(f"{SESSION_COOKIE}=abc123; Path=/; Max-Age=43200")
    for attr in ("Secure", "HttpOnly", "SameSite=Strict"):
        assert attr in header
    assert "Domain" not in header
    cleared = CookieSpec.session().clear()
    assert "Max-Age=0" in cleared and cleared.startswith(f"{SESSION_COOKIE}=;")
    dev = CookieSpec.session(secure=False).header("x")
    assert dev.startswith("mapi_session=") and "Secure" not in dev and "HttpOnly" in dev
    login = CookieSpec.login().header("tx")
    assert "SameSite=Lax" in login and "HttpOnly" in login
    with pytest.raises(ValueError):
        CookieSpec("__Host-x", 10, secure=False)
    with pytest.raises(ValueError):
        CookieSpec.session().header("a; Domain=evil")


# ---------------------------------------------------------------------------- Pub/Sub push


def _envelope(data: bytes, **extra: Any) -> dict[str, Any]:
    return {
        "message": {
            "data": base64.b64encode(data).decode(),
            "attributes": {"k": "v"},
            "messageId": "123",
            "publishTime": "2026-10-15T09:30:00.123Z",
            **extra,
        },
        "subscription": "projects/mapi-prod-control/subscriptions/mapi-run-events-push",
        "deliveryAttempt": 2,
    }


def test_push_envelope_parses_google_field_names_and_decodes() -> None:
    entry = {"jsonPayload": {"mapi_event": {"seq": 1}}}
    env = PubSubPushEnvelope.model_validate(_envelope(json.dumps(entry).encode()))
    assert env.message.message_id == "123" and env.delivery_attempt == 2
    assert json.loads(env.message.decoded()) == entry
    snake = PubSubPushEnvelope.model_validate(
        {
            "message": {"data": "", "message_id": "9"},
            "subscription": "projects/p-12345/subscriptions/sub",
            "unknown_google_field": True,
        }
    )
    assert snake.message.message_id == "9" and snake.message.decoded() == b""


def test_push_envelope_rejects_bad_data_and_subscriptions() -> None:
    env = PubSubPushEnvelope.model_validate(_envelope(b"x"))
    bad = env.message.model_copy(update={"data": "not base64!"})
    with pytest.raises(ValueError, match="base64"):
        bad.decoded()
    with pytest.raises(ValueError, match="size"):
        PubSubPushEnvelope.model_validate(_envelope(b"x" * 2048)).message.decoded(max_bytes=1024)
    with pytest.raises(ValidationError):
        PubSubPushEnvelope.model_validate({**_envelope(b"x"), "subscription": "subs/x"})
    with pytest.raises(ValidationError):
        PubSubPushEnvelope.model_validate({"subscription": "projects/p-12345/subscriptions/s"})


EXPECT = PushAuthExpectation(
    audience="https://api.example/internal/events",
    service_account_email="pubsub-push@mapi-prod-control.iam.gserviceaccount.com",
)


def _claims(**kw: Any) -> dict[str, Any]:
    base = {
        "iss": "https://accounts.google.com",
        "aud": EXPECT.audience,
        "email": EXPECT.service_account_email,
        "email_verified": True,
        "iat": int(NOW.timestamp()) - 10,
        "exp": int(NOW.timestamp()) + 3500,
    }
    base.update(kw)
    return base


def test_push_claims_accept_a_good_token() -> None:
    assert check_push_claims(_claims(), EXPECT, now=NOW) == []
    assert check_push_claims(_claims(iss="accounts.google.com"), EXPECT, now=NOW) == []
    assert check_push_claims(_claims(aud=[EXPECT.audience, "x"]), EXPECT, now=NOW) == []


@pytest.mark.parametrize(
    ("override", "problem"),
    [
        ({"iss": "https://evil.example"}, "issuer"),
        ({"aud": "https://other/"}, "audience"),
        ({"email": "attacker@evil.iam.gserviceaccount.com"}, "service account"),
        ({"email_verified": False}, "verified"),
        ({"exp": int(NOW.timestamp()) - 61}, "expired"),
        ({"exp": None}, "expired"),
        ({"iat": int(NOW.timestamp()) + 600}, "future"),
    ],
)
def test_push_claims_reject(override: dict[str, Any], problem: str) -> None:
    problems = check_push_claims(_claims(**override), EXPECT, now=NOW)
    assert any(problem in p for p in problems), problems


def test_push_expectation_requires_a_service_account() -> None:
    with pytest.raises(ValidationError):
        PushAuthExpectation(audience="a", service_account_email="someone@gmail.com")
