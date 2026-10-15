"""Stable error codes, RFC 7807 problem documents and the ``MapiError`` hierarchy.

Every error the platform returns carries a stable machine-readable ``code`` (one of
:class:`ErrorCode`). Codes never change meaning once released; new codes are additive.

Untrusted text (tracebacks, user-supplied values echoed back, plugin messages) is always
carried in a separate ``untrusted_text`` field and never merged into ``detail``/``message``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_TYPE_PREFIX = "urn:mapi:error:"


class Severity(StrEnum):
    """How bad an :class:`Issue` is. Only ``error`` blocks submission."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ErrorCode(StrEnum):
    """Stable error codes. See :data:`ERROR_INFO` for HTTP status and titles."""

    # --- spec / schema / static validation -------------------------------------------------
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SPEC_PARSE_ERROR = "SPEC_PARSE_ERROR"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_API_VERSION = "UNSUPPORTED_API_VERSION"
    REF_UNRESOLVED = "REF_UNRESOLVED"
    SETTINGS_INVALID = "SETTINGS_INVALID"
    BINDING_INVALID = "BINDING_INVALID"
    SPLIT_INVALID = "SPLIT_INVALID"
    LEAKAGE_RISK = "LEAKAGE_RISK"
    DIM_POLICY_LOOSENED = "DIM_POLICY_LOOSENED"
    SCHEMA_DYNAMIC = "SCHEMA_DYNAMIC"
    SIDE_TABLE_COVERAGE = "SIDE_TABLE_COVERAGE"
    # --- CoordPlan (score-time coordinate checks) -------------------------------------------
    UNSEEN_LEVELS = "UNSEEN_LEVELS"
    COORD_NOT_ALLOWED = "COORD_NOT_ALLOWED"
    HORIZON_EXCEEDS_FIT = "HORIZON_EXCEEDS_FIT"
    MISSING_FUTURE_COVARIATE = "MISSING_FUTURE_COVARIATE"
    NON_CONTIGUOUS_EXTENSION = "NON_CONTIGUOUS_EXTENSION"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    RECURSIVE_UNSUPPORTED = "RECURSIVE_UNSUPPORTED"
    # --- capability / quota -----------------------------------------------------------------
    MODE_NOT_SUPPORTED = "MODE_NOT_SUPPORTED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    # --- generic API ------------------------------------------------------------------------
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    # --- run failures -----------------------------------------------------------------------
    USER_CODE_ERROR = "USER_CODE_ERROR"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    INFRA_ERROR = "INFRA_ERROR"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    TIMEOUT = "TIMEOUT"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


Category = Literal["spec", "coords", "capability", "quota", "api", "run", "internal"]


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Static metadata for an :class:`ErrorCode`."""

    status: int
    title: str
    category: Category
    retryable: bool = False


ERROR_INFO: dict[ErrorCode, ErrorInfo] = {
    ErrorCode.SCHEMA_INVALID: ErrorInfo(422, "The document does not match its schema", "spec"),
    ErrorCode.SPEC_PARSE_ERROR: ErrorInfo(400, "The document could not be parsed", "spec"),
    ErrorCode.PAYLOAD_TOO_LARGE: ErrorInfo(413, "The document exceeds a size limit", "spec"),
    ErrorCode.UNSUPPORTED_API_VERSION: ErrorInfo(422, "Unsupported apiVersion", "spec"),
    ErrorCode.REF_UNRESOLVED: ErrorInfo(422, "A reference could not be resolved", "spec"),
    ErrorCode.SETTINGS_INVALID: ErrorInfo(422, "Settings do not match their schema", "spec"),
    ErrorCode.BINDING_INVALID: ErrorInfo(422, "The data binding is invalid", "spec"),
    ErrorCode.SPLIT_INVALID: ErrorInfo(422, "The train/holdout split is invalid", "spec"),
    ErrorCode.LEAKAGE_RISK: ErrorInfo(422, "A stateful step runs before the split", "spec"),
    ErrorCode.DIM_POLICY_LOOSENED: ErrorInfo(422, "A dim policy may only be tightened", "spec"),
    ErrorCode.SCHEMA_DYNAMIC: ErrorInfo(422, "Schema needs a dry-run to resolve", "spec"),
    ErrorCode.SIDE_TABLE_COVERAGE: ErrorInfo(422, "A side table is not available", "spec"),
    ErrorCode.UNSEEN_LEVELS: ErrorInfo(422, "Unseen levels on a dim that forbids them", "coords"),
    ErrorCode.COORD_NOT_ALLOWED: ErrorInfo(422, "Coordinates not allowed by dim policy", "coords"),
    ErrorCode.HORIZON_EXCEEDS_FIT: ErrorInfo(409, "Horizon exceeds the fitted range", "coords"),
    ErrorCode.MISSING_FUTURE_COVARIATE: ErrorInfo(422, "A future covariate is missing", "coords"),
    ErrorCode.NON_CONTIGUOUS_EXTENSION: ErrorInfo(422, "Extension is not contiguous", "coords"),
    ErrorCode.CAPABILITY_UNSUPPORTED: ErrorInfo(422, "The model lacks this capability", "coords"),
    ErrorCode.RECURSIVE_UNSUPPORTED: ErrorInfo(422, "Recursive forecasting unsupported", "coords"),
    ErrorCode.MODE_NOT_SUPPORTED: ErrorInfo(422, "Score mode not supported", "capability"),
    ErrorCode.QUOTA_EXCEEDED: ErrorInfo(429, "Quota exceeded", "quota"),
    ErrorCode.RATE_LIMITED: ErrorInfo(429, "Too many requests", "quota", retryable=True),
    ErrorCode.UNAUTHENTICATED: ErrorInfo(401, "Authentication required", "api"),
    ErrorCode.FORBIDDEN: ErrorInfo(403, "Not allowed", "api"),
    ErrorCode.NOT_FOUND: ErrorInfo(404, "Not found", "api"),
    ErrorCode.CONFLICT: ErrorInfo(409, "Conflict", "api"),
    ErrorCode.IDEMPOTENCY_CONFLICT: ErrorInfo(409, "Idempotency key reused", "api"),
    ErrorCode.PRECONDITION_FAILED: ErrorInfo(412, "Precondition failed", "api"),
    ErrorCode.USER_CODE_ERROR: ErrorInfo(422, "User or plugin code failed", "run"),
    ErrorCode.RESOURCE_EXHAUSTED: ErrorInfo(507, "Run exhausted its resources", "run"),
    ErrorCode.INFRA_ERROR: ErrorInfo(503, "Infrastructure error", "run", retryable=True),
    ErrorCode.INTEGRITY_FAILURE: ErrorInfo(500, "Output integrity check failed", "run"),
    ErrorCode.TIMEOUT: ErrorInfo(504, "Run timed out", "run"),
    ErrorCode.SANDBOX_VIOLATION: ErrorInfo(422, "Sandbox policy violated", "run"),
    ErrorCode.CANCELLED: ErrorInfo(409, "Run was cancelled", "run"),
    ErrorCode.INTERNAL_ERROR: ErrorInfo(500, "Internal error", "internal", retryable=True),
}


def http_status(code: ErrorCode | str) -> int:
    """HTTP status for a code (500 for unknown codes)."""
    try:
        return ERROR_INFO[ErrorCode(code)].status
    except ValueError:
        return 500


# --------------------------------------------------------------------------------- JSON pointer


def json_pointer(*parts: str | int) -> str:
    """Build an RFC 6901 JSON pointer from path segments (``""`` is the whole document)."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def join_pointer(base: str, *parts: str | int) -> str:
    """Append segments to an existing pointer."""
    return base + json_pointer(*parts)


def parse_pointer(pointer: str) -> list[str]:
    """Split an RFC 6901 pointer into unescaped segments."""
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("a JSON pointer must be empty or start with '/'")
    return [s.replace("~1", "/").replace("~0", "~") for s in pointer[1:].split("/")]


# ---------------------------------------------------------------------------------- models


class Issue(BaseModel):
    """One problem found while validating a document, located by a JSON pointer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    severity: Severity = Severity.ERROR
    pointer: str = Field("", description="RFC 6901 JSON pointer into the submitted document")
    message: str = Field(..., max_length=2000, description="Trusted, platform-authored text")
    suggested_action: str | None = Field(None, max_length=2000)
    untrusted_text: str | None = Field(
        None, max_length=8000, description="User/plugin-originated text; render as text only"
    )
    context: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


def has_errors(issues: Iterable[Issue]) -> bool:
    """True if any issue is an error."""
    return any(i.severity is Severity.ERROR for i in issues)


class Problem(BaseModel):
    """RFC 7807 problem document with mapi extensions (``code``, ``errors``, ``untrusted_text``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = "about:blank"
    title: str
    status: int = Field(..., ge=100, le=599)
    detail: str | None = None
    instance: str | None = None
    code: ErrorCode
    errors: list[Issue] = Field(default_factory=list)
    untrusted_text: str | None = Field(None, max_length=65536)
    trace_id: str | None = None

    @classmethod
    def from_code(
        cls,
        code: ErrorCode,
        detail: str | None = None,
        *,
        errors: Sequence[Issue] = (),
        untrusted_text: str | None = None,
        instance: str | None = None,
        trace_id: str | None = None,
    ) -> Problem:
        info = ERROR_INFO[code]
        return cls(
            type=PROBLEM_TYPE_PREFIX + code.value,
            title=info.title,
            status=info.status,
            detail=detail,
            instance=instance,
            code=code,
            errors=list(errors),
            untrusted_text=untrusted_text,
            trace_id=trace_id,
        )


# ------------------------------------------------------------------------------- exceptions


class MapiError(Exception):
    """Base exception: carries a stable code, a JSON pointer and optional untrusted text."""

    default_code: ClassVar[ErrorCode] = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        detail: str,
        *,
        code: ErrorCode | None = None,
        pointer: str = "",
        untrusted_text: str | None = None,
        issues: Sequence[Issue] = (),
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code or self.default_code
        self.pointer = pointer
        self.untrusted_text = untrusted_text
        self.issues: list[Issue] = list(issues)

    @property
    def status(self) -> int:
        return ERROR_INFO[self.code].status

    def to_issue(self) -> Issue:
        return Issue(
            code=self.code,
            pointer=self.pointer,
            message=self.detail[:2000],
            untrusted_text=None if self.untrusted_text is None else self.untrusted_text[:8000],
        )

    def to_problem(self, *, instance: str | None = None, trace_id: str | None = None) -> Problem:
        errors = self.issues or [self.to_issue()]
        return Problem.from_code(
            self.code,
            self.detail,
            errors=errors,
            untrusted_text=self.untrusted_text,
            instance=instance,
            trace_id=trace_id,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value!r}, detail={self.detail!r})"


class SpecError(MapiError):
    """A document is structurally invalid."""

    default_code = ErrorCode.SCHEMA_INVALID


class SpecParseError(SpecError):
    """YAML/JSON could not be parsed or uses forbidden features (anchors, tags...)."""

    default_code = ErrorCode.SPEC_PARSE_ERROR


class PayloadTooLarge(SpecError):
    """Size, depth or node-count limit exceeded."""

    default_code = ErrorCode.PAYLOAD_TOO_LARGE


class UnsupportedApiVersion(SpecError):
    default_code = ErrorCode.UNSUPPORTED_API_VERSION


class RefUnresolved(SpecError):
    default_code = ErrorCode.REF_UNRESOLVED


class ValidationFailed(SpecError):
    """Static validation produced at least one error; ``issues`` holds them all."""

    def __init__(self, issues: Sequence[Issue], detail: str | None = None) -> None:
        errors = [i for i in issues if i.severity is Severity.ERROR]
        code = errors[0].code if errors else ErrorCode.SCHEMA_INVALID
        n = len(errors)
        super().__init__(
            detail or f"validation failed with {n} error{'s' if n != 1 else ''}",
            code=code,
            pointer=errors[0].pointer if errors else "",
            issues=issues,
        )


class CoordPlanError(MapiError):
    """A score request's coordinates violate the model's dim policies."""

    default_code = ErrorCode.COORD_NOT_ALLOWED


class CapabilityError(MapiError):
    default_code = ErrorCode.CAPABILITY_UNSUPPORTED


class ModeNotSupported(CapabilityError):
    default_code = ErrorCode.MODE_NOT_SUPPORTED


class QuotaExceeded(MapiError):
    default_code = ErrorCode.QUOTA_EXCEEDED


class RunFailure(MapiError):
    """Base for failures of a run (as opposed to request validation)."""

    default_code = ErrorCode.INFRA_ERROR


class UserCodeError(RunFailure):
    default_code = ErrorCode.USER_CODE_ERROR


class ResourceExhausted(RunFailure):
    default_code = ErrorCode.RESOURCE_EXHAUSTED


class InfraError(RunFailure):
    default_code = ErrorCode.INFRA_ERROR


class IntegrityFailure(RunFailure):
    default_code = ErrorCode.INTEGRITY_FAILURE


class RunTimeout(RunFailure):
    default_code = ErrorCode.TIMEOUT


class SandboxViolation(RunFailure):
    default_code = ErrorCode.SANDBOX_VIOLATION


_CLASS_FOR_CODE: dict[ErrorCode, type[MapiError]] = {
    ErrorCode.SCHEMA_INVALID: SpecError,
    ErrorCode.SPEC_PARSE_ERROR: SpecParseError,
    ErrorCode.PAYLOAD_TOO_LARGE: PayloadTooLarge,
    ErrorCode.UNSUPPORTED_API_VERSION: UnsupportedApiVersion,
    ErrorCode.REF_UNRESOLVED: RefUnresolved,
    ErrorCode.UNSEEN_LEVELS: CoordPlanError,
    ErrorCode.COORD_NOT_ALLOWED: CoordPlanError,
    ErrorCode.HORIZON_EXCEEDS_FIT: CoordPlanError,
    ErrorCode.MISSING_FUTURE_COVARIATE: CoordPlanError,
    ErrorCode.NON_CONTIGUOUS_EXTENSION: CoordPlanError,
    ErrorCode.RECURSIVE_UNSUPPORTED: CoordPlanError,
    ErrorCode.CAPABILITY_UNSUPPORTED: CapabilityError,
    ErrorCode.MODE_NOT_SUPPORTED: ModeNotSupported,
    ErrorCode.QUOTA_EXCEEDED: QuotaExceeded,
    ErrorCode.USER_CODE_ERROR: UserCodeError,
    ErrorCode.RESOURCE_EXHAUSTED: ResourceExhausted,
    ErrorCode.INFRA_ERROR: InfraError,
    ErrorCode.INTEGRITY_FAILURE: IntegrityFailure,
    ErrorCode.TIMEOUT: RunTimeout,
    ErrorCode.SANDBOX_VIOLATION: SandboxViolation,
}


def error_for_code(code: ErrorCode, detail: str, **kwargs: Any) -> MapiError:
    """Instantiate the most specific :class:`MapiError` subclass for ``code``."""
    cls = _CLASS_FOR_CODE.get(code, MapiError)
    return cls(detail, code=code, **kwargs)


def error_from_problem(problem: Problem) -> MapiError:
    """Re-hydrate a :class:`MapiError` from a received :class:`Problem` (client side)."""
    err = error_for_code(
        problem.code,
        problem.detail or problem.title,
        untrusted_text=problem.untrusted_text,
        issues=problem.errors,
        pointer=problem.errors[0].pointer if problem.errors else "",
    )
    return err
