"""M3 webhooks: signing and verification (with tolerance and rotation), the retry schedule,
DTO rules and the SSRF policy (static URL checks and resolved-address checks)."""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

import pytest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.webhooks import (
    RETRY_SCHEDULE,
    SIGNATURE_TOLERANCE_S,
    SSRF_POLICY,
    WEBHOOK_SECRET_RE,
    Webhook,
    WebhookCreate,
    WebhookCreated,
    WebhookEnvelope,
    WebhookEventType,
    WebhookUpdate,
    check_resolved_addresses,
    check_signature,
    check_webhook_url,
    generate_webhook_secret,
    ip_is_blocked,
    next_retry_delay,
    parse_signature_header,
    sign_payload,
    signature_header,
    verify_signature,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

SECRET = "whsec_b3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5hJ7kQ2x9"
BODY = WebhookEnvelope.model_validate(WebhookEnvelope.model_json_schema()["examples"][0])
RAW = BODY.model_dump_json().encode()
T = 1_792_000_000


# ---------------------------------------------------------------------------- signatures


def test_the_signature_is_hmac_sha256_of_timestamp_dot_body() -> None:
    expected = hmac.new(SECRET.encode(), f"{T}.".encode() + RAW, hashlib.sha256).hexdigest()
    assert sign_payload(RAW, SECRET, T) == expected
    assert signature_header(RAW, [SECRET], T) == f"t={T},v1={expected}"


@pytest.mark.parametrize("skew", [0, 1, -1, SIGNATURE_TOLERANCE_S, -SIGNATURE_TOLERANCE_S])
def test_verify_accepts_within_the_tolerance(skew: int) -> None:
    header = signature_header(RAW, [SECRET], T)
    assert verify_signature(RAW, header, SECRET, now=T + skew)
    assert check_signature(RAW, header, [SECRET], now=T + skew) == T


@pytest.mark.parametrize("skew", [SIGNATURE_TOLERANCE_S + 1, -SIGNATURE_TOLERANCE_S - 1, 86400])
def test_verify_refuses_outside_the_tolerance(skew: int) -> None:
    header = signature_header(RAW, [SECRET], T)
    assert not verify_signature(RAW, header, SECRET, now=T + skew)
    with pytest.raises(MapiError) as exc:
        check_signature(RAW, header, SECRET, now=T + skew)
    assert exc.value.code is ErrorCode.WEBHOOK_SIGNATURE_INVALID


def test_tampering_and_wrong_secrets_fail() -> None:
    header = signature_header(RAW, [SECRET], T)
    assert not verify_signature(RAW + b" ", header, SECRET, now=T)
    assert not verify_signature(RAW, header, generate_webhook_secret(), now=T)
    # a replayed signature with a forged, fresh timestamp no longer matches
    forged = header.replace(f"t={T}", f"t={T + 1000}")
    assert not verify_signature(RAW, forged, SECRET, now=T + 1000)


def test_rotation_signs_with_every_active_secret() -> None:
    new = generate_webhook_secret()
    header = signature_header(RAW, [new, SECRET], T)
    t, sigs = parse_signature_header(header)
    assert t == T and len(sigs) == 2
    assert verify_signature(RAW, header, SECRET, now=T)  # receivers on the old secret
    assert verify_signature(RAW, header, [new], now=T)  # and on the new one
    with pytest.raises(ValueError):
        signature_header(RAW, [], T)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "v1=" + "a" * 64,
        f"t={T}",
        f"t={T},v1=xyz",
        f"t={T},t={T},v1=" + "a" * 64,
        "t=abc,v1=" + "a" * 64,
        "garbage",
        f"t={T},v1=" + "a" * 64 + ",x" * 3000,
    ],
)
def test_malformed_headers_are_refused(header: str | None) -> None:
    assert not verify_signature(RAW, header, SECRET, now=T)


def test_unknown_schemes_are_ignored() -> None:
    header = signature_header(RAW, [SECRET], T) + ",v0=deadbeef"
    assert verify_signature(RAW, header, SECRET, now=T)


def test_secrets_have_the_documented_shape_and_are_redacted() -> None:
    for _ in range(20):
        assert WEBHOOK_SECRET_RE.match(generate_webhook_secret())
    created = WebhookCreated.model_validate(WebhookCreated.model_json_schema()["examples"][0])
    assert created.secret not in repr(created) and created.secret not in str(created)
    assert Webhook.model_validate(Webhook.model_json_schema()["examples"][0]).failure_count == 0


# ------------------------------------------------------------------------------- retries


def test_retry_schedule() -> None:
    assert next_retry_delay(1, 503) == timedelta(minutes=1)
    assert next_retry_delay(len(RETRY_SCHEDULE), None) == RETRY_SCHEDULE[-1]
    assert next_retry_delay(len(RETRY_SCHEDULE) + 1, 500) is None
    assert next_retry_delay(1, 200) is None and next_retry_delay(3, 204) is None
    assert next_retry_delay(1, 410) is None  # Gone: never retried
    assert next_retry_delay(2, 500, jitter=0.5) == timedelta(minutes=5) * 1.05
    with pytest.raises(ValueError):
        next_retry_delay(1, 500, jitter=1.0)
    total = sum(RETRY_SCHEDULE, timedelta())
    assert timedelta(hours=40) < total < timedelta(hours=48)


# ---------------------------------------------------------------------------------- DTOs


def test_event_types_are_the_documented_set() -> None:
    assert {e.value for e in WebhookEventType} == {
        "run.succeeded",
        "run.failed",
        "model.version.ready",
        "endpoint.promoted",
        "score.completed",
        "tenant.deletion.completed",
    }


def test_create_and_update_rules() -> None:
    ok = WebhookCreate.model_validate(WebhookCreate.model_json_schema()["examples"][0])
    assert ok.active
    with pytest.raises(ValidationError, match="unique"):
        WebhookCreate(url="https://hooks.example.com/x", events=["run.failed", "run.failed"])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        WebhookCreate(url="http://hooks.example.com/x", events=["run.failed"])  # type: ignore[list-item]
    with pytest.raises(ValidationError, match="at least one"):
        WebhookUpdate()
    with pytest.raises(ValidationError):
        WebhookUpdate(url="http://hooks.example.com/x")


# ---------------------------------------------------------------------------------- SSRF

BLOCKED_IPS = [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1", "169.254.169.254",
    "100.64.0.1", "100.127.255.254", "0.0.0.0", "224.0.0.1", "239.255.255.250",
    "255.255.255.255", "198.18.0.1", "192.0.2.1", "240.0.0.1",
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "2001:db8::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:a00:1::1",
    "fd00:ec2::254", "not-an-ip",
]  # fmt: skip
PUBLIC_IPS = ["8.8.8.8", "1.1.1.1", "142.250.185.78", "2606:4700:4700::1111", "2a00:1450::1"]


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_private_and_special_addresses_are_blocked(ip: str) -> None:
    assert ip_is_blocked(ip)


@pytest.mark.parametrize("ip", PUBLIC_IPS)
def test_public_addresses_are_allowed(ip: str) -> None:
    assert not ip_is_blocked(ip)


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "ftp://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com@evil.example/x",
        "https://hooks.example.com:22/x",
        "https://hooks.example.com:80/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://8.8.8.8/x",
        "https://localhost/x",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://db.internal/x",
        "https://printer.local/x",
        "https://intranet/x",
        "https://hooks.example.com/x#frag",
        "https://hooks.example.com/a b",
        "https://hooks.exa\nmple.com/x",
        "https://0x7f000001/x",
        "https://" + "a" * 2100 + ".com/",
        "https:///nohost",
        "https://-bad-.example.com/x",
    ],
)
def test_static_url_checks_refuse(url: str) -> None:
    with pytest.raises(MapiError) as exc:
        check_webhook_url(url)
    assert exc.value.code is ErrorCode.WEBHOOK_URL_BLOCKED
    assert exc.value.pointer == "/url"


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://hooks.example.com/mapi", "hooks.example.com"),
        ("https://HOOKS.Example.com:443/a?b=c", "hooks.example.com"),
        ("https://api.partner.io:8443/hook", "api.partner.io"),
        ("https://hooks.example.com./x", "hooks.example.com"),
    ],
)
def test_static_url_checks_accept(url: str, host: str) -> None:
    assert check_webhook_url(url) == host


def test_every_resolved_address_must_be_public_and_the_first_is_pinned() -> None:
    assert check_resolved_addresses(["8.8.8.8", "1.1.1.1"]) == "8.8.8.8"
    for answers in ([], ["8.8.8.8", "10.0.0.1"], ["::ffff:169.254.169.254"]):
        with pytest.raises(MapiError) as exc:
            check_resolved_addresses(answers)
        assert exc.value.code is ErrorCode.WEBHOOK_URL_BLOCKED


def test_the_policy_is_the_documented_one() -> None:
    p = SSRF_POLICY
    assert p.schemes == ("https",) and not p.follow_redirects
    assert p.resolve_then_check and p.pin_resolved_ip and not p.allow_ip_literals
    assert p.timeout_s == 5.0 and p.max_response_bytes == 256 * 1024
