"""Audit events (keyed fingerprints, no secrets) and kill switches."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from mapi_spec.admin import (
    DEFAULT_KILL_SWITCHES,
    ExecutorHealth,
    KillSwitches,
    KillSwitchUpdate,
    kill_switch_decision,
)
from mapi_spec.audit import (
    AuditAction,
    AuditEvent,
    AuditOutcome,
    client_fingerprint,
    make_audit_event,
)
from mapi_spec.errors import ErrorCode
from mapi_spec.quotas import LimitName, QuotaScope
from mapi_spec.tenancy import PrincipalKind, ResourceKind, TrustTier
from mapi_spec.tokens import generate_token
from pydantic import ValidationError

NOW = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
KEY = b"audit-fingerprint-key"


def test_fingerprints_are_keyed_stable_and_short() -> None:
    a = client_fingerprint("203.0.113.7", KEY)
    assert a == client_fingerprint(" 203.0.113.7 ", KEY)
    assert a is not None and a.startswith("hmac-sha256:") and len(a) == 12 + 32
    assert a != client_fingerprint("203.0.113.7", b"another deployment")
    assert a != client_fingerprint("203.0.113.8", KEY)
    assert client_fingerprint(None, KEY) is None and client_fingerprint("  ", KEY) is None
    assert "203.0.113.7" not in a


def test_make_audit_event_hashes_client_attributes() -> None:
    ev = make_audit_event(
        action=AuditAction.TOKEN_CREATE,
        resource_kind=ResourceKind.TOKEN,
        outcome=AuditOutcome.SUCCESS,
        ts=NOW,
        fingerprint_key=KEY,
        tenant_id="acme",
        project_id="growth",
        actor_kind=PrincipalKind.USER,
        actor_id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
        resource_id="tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
        client_ip="198.51.100.4",
        user_agent="Mozilla/5.0",
        details={"scopes": ["models:read"]},
    )
    dumped = ev.model_dump_json()
    assert ev.id.startswith("aud_") and "198.51.100.4" not in dumped and "Mozilla" not in dumped
    assert ev.ip_hash == client_fingerprint("198.51.100.4", KEY)
    assert AuditEvent.model_validate_json(dumped) == ev


def test_details_refuse_secrets_and_bloat() -> None:
    base = AuditEvent.model_json_schema()["examples"][0]
    with pytest.raises(ValidationError, match="tokens or signed URLs"):
        AuditEvent.model_validate({**base, "details": {"oops": str(generate_token())}})
    with pytest.raises(ValidationError, match="tokens or signed URLs"):
        AuditEvent.model_validate({**base, "details": {"url": "https://x/?X-Goog-Signature=ab"}})
    with pytest.raises(ValidationError, match="at most"):
        AuditEvent.model_validate({**base, "details": {str(i): i for i in range(33)}})
    with pytest.raises(ValidationError):
        AuditEvent.model_validate({**base, "ip_hash": "203.0.113.7"})


def test_audit_actions_are_dotted_and_unique() -> None:
    values = [a.value for a in AuditAction]
    assert len(values) == len(set(values))
    assert all(v.count(".") == 1 and v == v.lower() for v in values)


# ----------------------------------------------------------------------------- kill switches


def test_defaults_allow_everything() -> None:
    d = kill_switch_decision(
        DEFAULT_KILL_SWITCHES, untrusted_code=True, tier=TrustTier.PUBLIC, spent_micros=10**12,
        now=NOW,
    )  # fmt: skip
    assert d.allowed


def test_untrusted_execution_switch_blocks_only_uploaded_code() -> None:
    sw = KillSwitches(untrusted_execution_enabled=False)
    blocked = kill_switch_decision(
        sw, untrusted_code=True, tier=TrustTier.INTERNAL, spent_micros=0, now=NOW
    )
    assert not blocked.allowed and blocked.code is ErrorCode.QUOTA_EXCEEDED
    assert (
        blocked.limit_name is LimitName.UNTRUSTED_EXECUTION and blocked.scope is QuotaScope.GLOBAL
    )
    builtins = kill_switch_decision(
        sw, untrusted_code=False, tier=TrustTier.PUBLIC, spent_micros=0, now=NOW
    )
    assert builtins.allowed


def test_global_spend_cap() -> None:
    sw = KillSwitches(global_spend_cap_micros=1_000_000)
    under = kill_switch_decision(
        sw, untrusted_code=False, tier=TrustTier.PUBLIC, spent_micros=999_999, now=NOW
    )
    assert under.allowed
    at = kill_switch_decision(
        sw, untrusted_code=False, tier=TrustTier.VERIFIED, spent_micros=1_000_000, now=NOW
    )
    assert not at.allowed and at.limit_name is LimitName.GLOBAL_SPEND
    assert at.retry_after_seconds == 14 * 3600 + 30 * 60
    assert at.message is not None and "daily" in at.message and "1.000000 USD" in at.message
    internal = kill_switch_decision(
        sw, untrusted_code=False, tier=TrustTier.INTERNAL, spent_micros=10**9, now=NOW
    )
    assert internal.allowed
    monthly = KillSwitches(global_spend_cap_micros=1, spend_cap_period="month")
    m = kill_switch_decision(
        monthly, untrusted_code=False, tier=TrustTier.PUBLIC, spent_micros=1, now=NOW
    )
    assert m.message is not None and "monthly" in m.message


def test_kill_switch_update_applies_partially_and_needs_a_reason() -> None:
    cur = KillSwitches(global_spend_cap_micros=5)
    upd = KillSwitchUpdate(untrusted_execution_enabled=False, reason="incident")
    new = upd.apply(cur, now=NOW, by="usr_01jaf3a0b0c0d0e0f0g0h0j0k0")
    assert not new.untrusted_execution_enabled and new.global_spend_cap_micros == 5
    assert new.reason == "incident" and new.updated_at == NOW
    cleared = KillSwitchUpdate(clear_spend_cap=True, reason="lifted").apply(new, now=NOW, by=None)
    assert cleared.global_spend_cap_micros is None and not cleared.untrusted_execution_enabled
    period = KillSwitchUpdate(spend_cap_period="month", global_spend_cap_micros=9, reason="x" * 3)
    assert period.apply(cur, now=NOW, by=None).spend_cap_period == "month"
    with pytest.raises(ValidationError):
        KillSwitchUpdate(untrusted_execution_enabled=True)
    with pytest.raises(ValidationError, match="either"):
        KillSwitchUpdate(global_spend_cap_micros=1, clear_spend_cap=True, reason="both")


def test_executor_health_example_is_consistent() -> None:
    h = ExecutorHealth.model_validate(ExecutorHealth.model_json_schema()["examples"][0])
    assert h.allows_public_tenants == h.caps.allows_public_tenants()
    assert h.conformance is not None and h.conformance.executor == h.name
