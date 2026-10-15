"""Audit events: who did what to which resource, and whether it worked (plan §9, §11 Admin).

Audit rows are append-only. They identify actors by platform id, never by e-mail, and never
store raw client IPs or user agents: :func:`client_fingerprint` keys an HMAC with a
deployment secret, so rows can be correlated ("same address as before") without being
reversible. Secrets (token values, signed URLs) never appear in ``details``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime, Field, JsonValue, field_validator

from mapi_spec._dto import (
    EXAMPLE_TS,
    Id,
    RequestModel,
    ResponseModel,
    TenantId,
    dto_examples,
    keyed_digest,
    new_id,
)
from mapi_spec.errors import ErrorCode
from mapi_spec.tenancy import PrincipalKind, ResourceKind
from mapi_spec.tokens import looks_like_token

__all__ = [
    "AUDIT_DETAILS_MAX_KEYS",
    "AuditAction",
    "AuditEvent",
    "AuditOutcome",
    "AuditQuery",
    "client_fingerprint",
    "make_audit_event",
]

AUDIT_DETAILS_MAX_KEYS: Final = 32
_FINGERPRINT = r"^hmac-sha256:[0-9a-f]{32}$"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    """Authentication or authorization refused it (including quota and kill switches)."""
    ERROR = "error"


class AuditAction(StrEnum):
    """Audited actions (``<resource>.<verb>``). New actions are additive."""

    AUTH_LOGIN = "auth.login"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGOUT = "auth.logout"
    TENANT_CREATE = "tenant.create"
    TENANT_UPDATE = "tenant.update"
    TENANT_SUSPEND = "tenant.suspend"
    TENANT_REACTIVATE = "tenant.reactivate"
    TENANT_DELETE = "tenant.delete"
    PROJECT_CREATE = "project.create"
    PROJECT_UPDATE = "project.update"
    PROJECT_DELETE = "project.delete"
    MEMBER_ADD = "member.add"
    MEMBER_UPDATE = "member.update"
    MEMBER_REMOVE = "member.remove"
    TOKEN_CREATE = "token.create"
    TOKEN_REVOKE = "token.revoke"
    TOKEN_AUTH_FAILED = "token.auth_failed"
    QUOTA_UPDATE = "quota.update"
    QUOTA_DELETE = "quota.delete"
    QUOTA_DENIED = "quota.denied"
    KILL_SWITCH_UPDATE = "kill_switch.update"
    EXECUTOR_EVIDENCE_UPDATE = "executor.evidence_update"
    """Conformance or security-suite results recorded for an executor (shared-tier gate)."""
    EXECUTOR_UPDATE = "executor.update"
    RUN_CREATE = "run.create"
    RUN_CANCEL = "run.cancel"
    RUN_INTEGRITY_FAILURE = "run.integrity_failure"
    DATASET_DOWNLOAD = "dataset.download"
    MODEL_ARTIFACT_LINK = "model.artifact_link"
    ENDPOINT_PROMOTE = "endpoint.promote"
    PLUGIN_PUBLISH = "plugin.publish"
    PLUGIN_YANK = "plugin.yank"
    EVENT_REJECTED = "event.rejected"
    """A pushed run event failed HMAC or identity checks (a security signal)."""


class AuditEvent(ResponseModel):
    """One audit row. ``tenant_id`` is ``None`` for platform-level actions."""

    id: Id = Field(..., pattern=r"^aud_[0-9a-z]{26}$")
    ts: AwareDatetime
    tenant_id: TenantId | None = None
    project_id: TenantId | None = None
    actor_kind: PrincipalKind | None = Field(None, description="None: unauthenticated caller")
    actor_id: str | None = Field(None, max_length=128)
    token_id: Id | None = Field(None, description="Set when the actor used an API token")
    operator: bool = Field(False, description="The actor acted as a platform operator")
    action: AuditAction
    resource_kind: ResourceKind
    resource_id: str | None = Field(None, max_length=256)
    outcome: AuditOutcome
    code: ErrorCode | None = Field(None, description="Problem code of a denial or error")
    ip_hash: str | None = Field(None, pattern=_FINGERPRINT)
    user_agent_hash: str | None = Field(None, pattern=_FINGERPRINT)
    request_id: str | None = Field(None, max_length=128)
    details: dict[str, JsonValue] = Field(default_factory=dict)

    model_config = dto_examples(
        {
            "id": "aud_01jaf3w1x2y3z4a5b6c7d8e9f0",
            "ts": EXAMPLE_TS,
            "tenant_id": "acme",
            "project_id": "growth",
            "actor_kind": "user",
            "actor_id": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
            "action": "token.create",
            "resource_kind": "token",
            "resource_id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
            "outcome": "success",
            "ip_hash": "hmac-sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f0",
            "user_agent_hash": "hmac-sha256:a1b2c3d4e5f60718293a4b5c6d7e8f90",
            "request_id": "req-7f3a",
            "details": {"scopes": ["score:geo-mmm-prod"], "expires_in_days": 30},
        },
        {
            "id": "aud_01jaf3w9x8y7z6a5b4c3d2e1f0",
            "ts": EXAMPLE_TS,
            "tenant_id": "acme",
            "project_id": "growth",
            "actor_kind": "token",
            "actor_id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
            "token_id": "tok_01jaf3r1s2t3v4w5x6y7z8a9b0",
            "action": "quota.denied",
            "resource_kind": "run",
            "outcome": "denied",
            "code": "QUOTA_EXCEEDED",
            "details": {"limit_name": "vcpu_seconds_per_day", "scope": "tenant"},
        },
    )

    @field_validator("details")
    @classmethod
    def _no_secrets(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(v) > AUDIT_DETAILS_MAX_KEYS:
            raise ValueError(f"details holds at most {AUDIT_DETAILS_MAX_KEYS} keys")
        if looks_like_token(repr(v)) or "X-Goog-Signature" in repr(v):
            raise ValueError("audit details must not contain tokens or signed URLs")
        return v


def client_fingerprint(value: str | None, key: bytes) -> str | None:
    """Keyed, truncated digest of a client IP or user agent (``None`` stays ``None``)."""
    if value is None or not value.strip():
        return None
    return keyed_digest(value.strip(), key, length=32)


def make_audit_event(
    *,
    action: AuditAction,
    resource_kind: ResourceKind,
    outcome: AuditOutcome,
    ts: datetime,
    fingerprint_key: bytes,
    tenant_id: str | None = None,
    project_id: str | None = None,
    actor_kind: PrincipalKind | None = None,
    actor_id: str | None = None,
    token_id: str | None = None,
    operator: bool = False,
    resource_id: str | None = None,
    code: ErrorCode | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    details: Mapping[str, JsonValue] | None = None,
) -> AuditEvent:
    """Build an :class:`AuditEvent`, hashing the client IP and user agent on the way in."""
    return AuditEvent(
        id=new_id("aud"),
        ts=ts,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        token_id=token_id,
        operator=operator,
        action=action,
        resource_kind=resource_kind,
        resource_id=resource_id,
        outcome=outcome,
        code=code,
        ip_hash=client_fingerprint(client_ip, fingerprint_key),
        user_agent_hash=client_fingerprint(user_agent, fingerprint_key),
        request_id=request_id,
        details=dict(details or {}),
    )


class AuditQuery(RequestModel):
    """Filters of ``GET /v1/admin/audit`` and ``GET /v1/tenants/{tenant_id}/audit``
    (sent as query parameters; tenant audits force ``tenant_id``)."""

    tenant_id: TenantId | None = None
    project_id: TenantId | None = None
    actor_id: str | None = Field(None, max_length=128)
    action: AuditAction | None = None
    outcome: AuditOutcome | None = None
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None

    model_config = dto_examples(
        {"tenant_id": "acme", "action": "token.create", "since": "2026-10-01T00:00:00Z"}
    )
