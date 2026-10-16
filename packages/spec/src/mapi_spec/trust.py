"""Trust lifecycle: self-service sign-up, verification, tenant deletion and GDPR export
(plan §9 "Compute abuse", "Data lifecycle", §14 M3 and cut lines A/B; M3).

* **Sign-up** (``POST /auth/signup``): e-mail, name and accepted terms. The answer is the
  same whether or not the address is new (no account enumeration). A disposable-e-mail
  policy hook (:class:`DisposableEmailPolicy`) refuses throwaway domains.
* **E-mail verification** (``POST /auth/verify-email``) with a single-use token that
  expires after :data:`EMAIL_VERIFICATION_TTL` creates a ``public``-tier tenant with a
  ``default`` project, the signer as tenant admin.
* **Cut line A (M3):** public tenants run declarative specs and built-in plugins only -
  untrusted data, trusted code. **Cut line B (M5)** opens code upload to public tenants, and
  only after *identity* verification (:func:`uploaded_code_allowed`).
* **Tenant deletion** (``POST /v1/tenants/{id}/deletion``) is scheduled with a grace period
  (:data:`TENANT_DELETION_GRACE`, the tenant is suspended and can cancel), then purges
  every store and reports progress per store (:class:`TenantDeletion`).
* **GDPR export** (``POST /v1/tenants/{id}/exports``) packages everything the tenant stored
  into one archive with a short-lived download link (:class:`GdprExport`).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import timedelta
from enum import StrEnum
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, Field, model_validator

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, TenantId, dto_examples
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tenancy import TrustTier

__all__ = [
    "CUT_LINE_B_OPEN",
    "DEFAULT_DISPOSABLE_DOMAINS",
    "DELETION_TRANSITIONS",
    "EMAIL_VERIFICATION_TTL",
    "RETAINED_RECORDS",
    "SHARED_EMAIL_DOMAINS",
    "SYBIL_FLAGS",
    "TENANT_DELETION_GRACE",
    "TERMS_VERSION",
    "BlocklistEmailPolicy",
    "DeletionState",
    "DeletionStore",
    "DisposableEmailPolicy",
    "EmailVerify",
    "GdprExport",
    "GdprExportRequest",
    "SignupAccepted",
    "SignupRequest",
    "SignupReview",
    "SignupReviewDecision",
    "SignupReviewState",
    "SignupState",
    "StoreProgress",
    "TenantDeletion",
    "TenantDeletionRequest",
    "VerificationMethod",
    "VerificationResult",
    "VerificationStatus",
    "VerificationUpdate",
    "check_signup",
    "email_domain",
    "generate_verification_token",
    "is_shared_email_domain",
    "next_deletion_state",
    "uploaded_code_allowed",
]

CUT_LINE_B_OPEN: Final = False
"""M3 ships cut line A: public tenants never upload code, verified or not (M5 flips it)."""
EMAIL_VERIFICATION_TTL: Final = timedelta(hours=24)
TENANT_DELETION_GRACE: Final = timedelta(days=7)
TERMS_VERSION: Final = "2026-10"
"""The terms of service version a sign-up must accept."""
VERIFICATION_TOKEN_PREFIX: Final = "mev_"
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_EMAIL_RE = r"^[^@\s]{1,64}@[^@\s]{1,255}$"


def generate_verification_token() -> str:
    """A single-use e-mail verification token (200 random bits; store its keyed digest)."""
    return VERIFICATION_TOKEN_PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(40))


# ===================================================================================== sign-up


@runtime_checkable
class DisposableEmailPolicy(Protocol):
    """The hook the server consults at sign-up (a static list, a vendor API, ...)."""

    def is_disposable(self, domain: str) -> bool: ...


DEFAULT_DISPOSABLE_DOMAINS: Final = frozenset(
    {
        "10minutemail.com",
        "dispostable.com",
        "getnada.com",
        "guerrillamail.com",
        "mailinator.com",
        "maildrop.cc",
        "sharklasers.com",
        "tempmail.com",
        "trashmail.com",
        "yopmail.com",
    }
)
"""A small built-in blocklist; deployments plug in a maintained source."""


class BlocklistEmailPolicy:
    """Refuses a domain on the list and any of its subdomains."""

    def __init__(self, domains: frozenset[str] = DEFAULT_DISPOSABLE_DOMAINS) -> None:
        self.domains = frozenset(d.lower().rstrip(".") for d in domains)

    def is_disposable(self, domain: str) -> bool:
        labels = domain.lower().rstrip(".").split(".")
        return any(".".join(labels[i:]) in self.domains for i in range(len(labels) - 1))


SHARED_EMAIL_DOMAINS: Final = (
    "aol.com",
    "gmail.com",
    "gmx.de",
    "gmx.net",
    "googlemail.com",
    "hotmail.com",
    "icloud.com",
    "live.com",
    "mail.com",
    "me.com",
    "msn.com",
    "outlook.com",
    "pm.me",
    "proton.me",
    "protonmail.com",
    "qq.com",
    "web.de",
    "yahoo.com",
    "yandex.com",
    "zoho.com",
)
"""Large free-mail providers: their users are unrelated people, so sign-ups are not capped
per domain and a burst of them is no Sybil signal (``MAPI_SIGNUP_SHARED_EMAIL_DOMAINS``)."""


def is_shared_email_domain(domain: str | None, shared: Iterable[str]) -> bool:
    """Is ``domain`` one of the ``shared`` free-mail domains (exact, case-insensitive)?"""
    if not domain:
        return False
    d = domain.lower().rstrip(".")
    return any(d == x.strip().lower().rstrip(".") for x in shared)


def email_domain(email: str) -> str:
    """The normalised (lower-case, IDNA/punycode) domain of an address (``ValueError``)."""
    local, sep, domain = email.strip().rpartition("@")
    if not sep or not local or not domain:
        raise ValueError("not an e-mail address")
    try:
        return domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("the e-mail domain is not a valid host name") from None


class SignupRequest(RequestModel):
    """``POST /auth/signup`` (public, rate-limited). ``organisation`` names the tenant."""

    email: str = Field(..., max_length=320, pattern=_EMAIL_RE)
    name: str = Field(..., min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    organisation: str | None = Field(None, min_length=1, max_length=200)
    accepted_terms: bool
    terms_version: str = Field(TERMS_VERSION, max_length=32)
    challenge: str | None = Field(
        None, max_length=4096, description="A bot-check token (e.g. Turnstile), if configured"
    )

    model_config = dto_examples(
        {
            "email": "ana@acme.example",
            "name": "Ana Analyst",
            "organisation": "Acme Retail",
            "accepted_terms": True,
            "terms_version": TERMS_VERSION,
        }
    )


def check_signup(req: SignupRequest, policy: DisposableEmailPolicy | None = None) -> str:
    """Refuse a sign-up early; return the normalised e-mail domain.

    ``TERMS_NOT_ACCEPTED`` without accepted (current) terms; ``EMAIL_NOT_ALLOWED`` for a
    malformed or disposable address.
    """
    if not req.accepted_terms or req.terms_version != TERMS_VERSION:
        raise MapiError(
            f"accept the terms of service ({TERMS_VERSION}) to sign up",
            code=ErrorCode.TERMS_NOT_ACCEPTED,
            pointer="/accepted_terms",
        )
    try:
        domain = email_domain(req.email)
    except ValueError:
        raise MapiError(
            "this e-mail address cannot sign up", code=ErrorCode.EMAIL_NOT_ALLOWED, pointer="/email"
        ) from None
    if (policy or BlocklistEmailPolicy()).is_disposable(domain):
        raise MapiError(
            "disposable e-mail addresses cannot sign up; use your work address",
            code=ErrorCode.EMAIL_NOT_ALLOWED,
            pointer="/email",
        )
    return domain


class SignupAccepted(ResponseModel):
    """``202`` of ``POST /auth/signup``: identical for new and known addresses."""

    status: Literal["check_email"] = "check_email"
    expires_in_s: int = int(EMAIL_VERIFICATION_TTL.total_seconds())

    model_config = dto_examples({"status": "check_email", "expires_in_s": 86400})


class EmailVerify(RequestModel):
    """``POST /auth/verify-email``: the token from the e-mail link (single use)."""

    token: str = Field(..., pattern=rf"^{VERIFICATION_TOKEN_PREFIX}[0-9a-hjkmnp-tv-z]{{40}}$")

    model_config = dto_examples({"token": "mev_" + "0123456789abcdefghjkmnpqrstvwxyz01234567"})


class VerificationResult(ResponseModel):
    """The verified address and the public-tier tenant it now administers; sign in next."""

    email: str
    tenant_id: TenantId
    project_id: TenantId
    trust_tier: TrustTier = TrustTier.PUBLIC
    login_url: str = "/auth/login"

    model_config = dto_examples(
        {
            "email": "ana@acme.example",
            "tenant_id": "acme-retail",
            "project_id": "default",
            "trust_tier": "public",
            "login_url": "/auth/login?return_to=%2Fguided",
        }
    )


# ============================================================================ sign-up review


class SignupState(StrEnum):
    """Where one sign-up is: waiting for its e-mail link, done, or never completed."""

    PENDING = "pending"
    VERIFIED = "verified"
    EXPIRED = "expired"


class SignupReviewState(StrEnum):
    """The operators' review of a sign-up (Sybil heuristics flag, operators decide)."""

    NONE = "none"
    FLAGGED = "flagged"
    """A heuristic matched: the tenant works, and waits in the review queue."""
    APPROVED = "approved"
    REJECTED = "rejected"
    """An operator rejected it: the tenant is suspended (audited, reason kept)."""


SYBIL_FLAGS: Final[dict[str, str]] = {
    "same_ip": "Several workspaces were created from the same network address",
    "same_device": "Several workspaces were created from the same browser",
    "same_email": "This address already created several workspaces",
    "domain_burst": "Many sign-ups from this e-mail domain in a short time",
}
"""The Sybil heuristics a sign-up can trip, with the plain-language reason shown to
operators. A flag never blocks the sign-up: it queues it for review."""


class SignupReview(ResponseModel):
    """``GET /v1/admin/signups`` (operators): one sign-up and why it was flagged.

    Client addresses and browsers appear only as keyed digests (``ip_hash``,
    ``device_hash``), never raw; e-mail addresses are shown to operators for the review."""

    id: Id = Field(..., pattern=r"^sgn_[0-9a-z]{26}$")
    email: str = Field(..., max_length=320)
    email_domain: str = Field(..., max_length=255)
    name: str = Field(..., max_length=200)
    organisation: str | None = Field(None, max_length=200)
    state: SignupState
    review: SignupReviewState = SignupReviewState.NONE
    flags: list[str] = Field(default_factory=list, max_length=len(SYBIL_FLAGS))
    reasons_plain: list[str] = Field(default_factory=list, max_length=len(SYBIL_FLAGS))
    tenant_id: TenantId | None = Field(None, description="The tenant the verification created")
    ip_hash: str | None = Field(None, max_length=64)
    device_hash: str | None = Field(None, max_length=64)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    verified_at: AwareDatetime | None = None
    reviewed_by: str | None = Field(None, max_length=128)
    reviewed_at: AwareDatetime | None = None
    review_reason: str | None = Field(None, max_length=500)

    model_config = dto_examples(
        {
            "id": "sgn_01jaf3s1t2v3w4x5y6z7a8b9c0",
            "email": "ana@acme.example",
            "email_domain": "acme.example",
            "name": "Ana Analyst",
            "organisation": "Acme Retail",
            "state": "verified",
            "review": "flagged",
            "flags": ["same_ip"],
            "reasons_plain": [SYBIL_FLAGS["same_ip"]],
            "tenant_id": "acme-retail",
            "ip_hash": "3f7a0c9e1b2d4f6a8c0e2b4d6f8a0c2e",
            "created_at": "2026-10-15T09:00:00Z",
            "expires_at": "2026-10-16T09:00:00Z",
            "verified_at": EXAMPLE_TS,
        }
    )


class SignupReviewDecision(RequestModel):
    """``POST /v1/admin/signups/{signup_id}/review`` (operators; audited).

    ``reject`` suspends the tenant the sign-up created; ``approve`` clears the flag (and
    reactivates a tenant an earlier rejection suspended)."""

    decision: Literal["approve", "reject"]
    reason: str = Field(..., min_length=1, max_length=500)

    model_config = dto_examples({"decision": "reject", "reason": "one of 14 tenants from one IP"})


# ================================================================================ verification


class VerificationMethod(StrEnum):
    NONE = "none"
    EMAIL = "email"
    CARD = "card"
    DOCUMENT = "document"
    MANUAL = "manual"
    """An operator verified the organisation out of band (contract, invoice)."""


class VerificationStatus(ResponseModel):
    """``GET /v1/tenants/{tenant_id}/verification``."""

    tenant_id: TenantId
    trust_tier: TrustTier
    email_verified: bool
    identity_verified: bool
    method: VerificationMethod = VerificationMethod.NONE
    verified_at: AwareDatetime | None = None
    uploaded_code_allowed: bool
    reason_plain: str = Field(..., max_length=500)

    model_config = dto_examples(
        {
            "tenant_id": "acme-retail",
            "trust_tier": "public",
            "email_verified": True,
            "identity_verified": False,
            "method": "email",
            "verified_at": EXAMPLE_TS,
            "uploaded_code_allowed": False,
            "reason_plain": "Public workspaces run built-in models only for now.",
        }
    )


class VerificationUpdate(RequestModel):
    """``PUT /v1/admin/tenants/{tenant_id}/verification`` (operators; audited)."""

    identity_verified: bool
    method: VerificationMethod
    reason: str = Field(..., min_length=1, max_length=500)

    model_config = dto_examples(
        {"identity_verified": True, "method": "manual", "reason": "signed enterprise contract"}
    )


def uploaded_code_allowed(
    tier: TrustTier,
    *,
    identity_verified: bool,
    public_code_enabled: bool = CUT_LINE_B_OPEN,
) -> tuple[bool, str]:
    """May this tenant upload plugin code (and plain-language why)?

    * ``internal`` / ``self-hosted-trusted``: yes;
    * ``verified`` (allow-listed, M4): only once identity is verified;
    * ``public``: never before cut line B; afterwards only with identity verification.
    """
    if tier in (TrustTier.INTERNAL, TrustTier.SELF_HOSTED_TRUSTED):
        return True, "This workspace is trusted to run its own code."
    if tier is TrustTier.PUBLIC and not public_code_enabled:
        return False, "Public workspaces run built-in models only for now."
    if not identity_verified:
        return False, "Uploading code needs a verified identity."
    return True, "Identity verified: this workspace may upload code."


# ==================================================================================== deletion


class DeletionState(StrEnum):
    SCHEDULED = "scheduled"
    """Within the grace period: the tenant is suspended and the deletion can be cancelled."""
    CANCELLED = "cancelled"
    DELETING = "deleting"
    """Irreversible: the tenant is ``deleting`` (inaccessible) and stores are being purged."""
    PURGED = "purged"
    FAILED = "failed"
    """A store failed to purge; operators retry (the tenant stays inaccessible)."""


DeletionEvent = Literal["cancel", "start", "store_failed", "all_done", "retry"]

DELETION_TRANSITIONS: Final[dict[tuple[DeletionState, str], DeletionState]] = {
    (DeletionState.SCHEDULED, "cancel"): DeletionState.CANCELLED,
    (DeletionState.SCHEDULED, "start"): DeletionState.DELETING,
    (DeletionState.DELETING, "store_failed"): DeletionState.FAILED,
    (DeletionState.DELETING, "all_done"): DeletionState.PURGED,
    (DeletionState.FAILED, "retry"): DeletionState.DELETING,
}
"""The whole deletion state machine; any other ``(state, event)`` is refused."""


def next_deletion_state(state: DeletionState, event: DeletionEvent) -> DeletionState:
    """Apply ``event`` (``CONFLICT`` if the transition is not allowed)."""
    try:
        return DELETION_TRANSITIONS[(state, event)]
    except KeyError:
        raise MapiError(
            f"a {state.value} deletion cannot '{event}'", code=ErrorCode.CONFLICT
        ) from None


DeletionStore = Literal["database", "object_store", "queue", "logs", "webhooks"]

RETAINED_RECORDS: Final[dict[str, str]] = {
    "audit": "kept 1 year after deletion, pseudonymised (actor ids only, no content)",
    "cost_ledger": "kept 7 years for tax law (amounts and run ids, no content)",
    "deletion_record": "this deletion's own record and progress (ids and counts, no content)",
    "platform_logs": "request and run logs are not purged: they hold ids, not content, and "
    "expire with the log buckets' retention (30 days by default)",
}
"""What deletion keeps, and why (shown to the tenant before it confirms)."""


class StoreProgress(ResponseModel):
    store: DeletionStore
    state: Literal["pending", "running", "done", "failed"]
    items_total: int | None = Field(None, ge=0)
    items_done: int = Field(0, ge=0)
    detail: str | None = Field(None, max_length=500)
    updated_at: AwareDatetime | None = None


class TenantDeletionRequest(RequestModel):
    """``POST /v1/tenants/{tenant_id}/deletion``: ``confirm`` must repeat the tenant id."""

    confirm: TenantId
    reason: str | None = Field(None, max_length=1000)

    model_config = dto_examples({"confirm": "acme-retail", "reason": "closing the account"})


class TenantDeletion(ResponseModel):
    """A tenant deletion and its progress per store."""

    id: Id = Field(..., pattern=r"^del_[0-9a-z]{26}$")
    tenant_id: TenantId
    state: DeletionState
    requested_by: str | None = Field(None, max_length=128)
    requested_at: AwareDatetime
    purge_after: AwareDatetime
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    cancelled_at: AwareDatetime | None = None
    stores: list[StoreProgress] = Field(default_factory=list)
    retained: dict[str, str] = Field(default_factory=lambda: dict(RETAINED_RECORDS))

    model_config = dto_examples(
        {
            "id": "del_01jaf3q1r2s3t4v5w6x7y8z9a0",
            "tenant_id": "acme-retail",
            "state": "deleting",
            "requested_by": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
            "requested_at": "2026-10-08T09:30:00Z",
            "purge_after": EXAMPLE_TS,
            "started_at": EXAMPLE_TS,
            "stores": [
                {"store": "database", "state": "done", "items_total": 412, "items_done": 412},
                {"store": "object_store", "state": "running", "items_total": 96,
                 "items_done": 40},
                {"store": "queue", "state": "done", "items_total": 0, "items_done": 0},
                {"store": "logs", "state": "pending"},
                {"store": "webhooks", "state": "pending"},
            ],
        }
    )  # fmt: skip

    @model_validator(mode="after")
    def _times(self) -> TenantDeletion:
        if self.purge_after < self.requested_at:
            raise ValueError("purge_after must not be before requested_at")
        return self

    @property
    def fraction_done(self) -> float:
        """Share of stores finished (``done``)."""
        if not self.stores:
            return 1.0 if self.state is DeletionState.PURGED else 0.0
        return sum(s.state == "done" for s in self.stores) / len(self.stores)


ExportPart = Literal["metadata", "datasets", "models", "audit", "usage"]
EXPORT_PARTS: Final[tuple[ExportPart, ...]] = ("metadata", "datasets", "models", "audit", "usage")


class GdprExportRequest(RequestModel):
    """``POST /v1/tenants/{tenant_id}/exports``: what to include (default: everything)."""

    include: list[ExportPart] = Field(
        default_factory=lambda: list(EXPORT_PARTS),
        min_length=1,
        max_length=5,
    )

    model_config = dto_examples({"include": ["metadata", "datasets", "models", "audit", "usage"]})


class GdprExport(ResponseModel):
    """An export job; ``download_url`` is a short-lived signed link once ``ready``."""

    id: Id = Field(..., pattern=r"^exp_[0-9a-z]{26}$")
    tenant_id: TenantId
    state: Literal["queued", "running", "ready", "expired", "failed"]
    include: list[ExportPart]
    requested_by: str | None = Field(None, max_length=128)
    created_at: AwareDatetime
    ready_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    size_bytes: int | None = Field(None, ge=0)
    download_url: str | None = None

    model_config = dto_examples(
        {
            "id": "exp_01jaf3r1s2t3v4w5x6y7z8a9b0",
            "tenant_id": "acme-retail",
            "state": "ready",
            "include": ["metadata", "datasets", "models", "audit", "usage"],
            "requested_by": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
            "created_at": EXAMPLE_TS,
            "ready_at": "2026-10-15T09:42:00Z",
            "expires_at": "2026-10-16T09:42:00Z",
            "size_bytes": 73400320,
            "download_url": "https://storage.example/exports/exp_01…?X-Goog-Signature=…",
        }
    )
