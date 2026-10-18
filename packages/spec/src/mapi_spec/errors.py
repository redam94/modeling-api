"""Stable error codes, RFC 7807 problem documents and the ``MapiError`` hierarchy.

Every error the platform returns carries a stable machine-readable ``code`` (one of
:class:`ErrorCode`). Codes never change meaning once released; new codes are additive.

Untrusted text (tracebacks, user-supplied values echoed back, plugin messages) is always
carried in a separate ``untrusted_text`` field and never merged into ``detail``/``message``.
"""

from __future__ import annotations

import re
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
    PLOT_UNSAFE = "PLOT_UNSAFE"
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
    # --- M3: guided mode, calibration and imports --------------------------------------------
    GUIDED_ANSWERS_INVALID = "GUIDED_ANSWERS_INVALID"
    BUSINESS_PRIORS_INVALID = "BUSINESS_PRIORS_INVALID"
    CALIBRATION_UNSUPPORTED = "CALIBRATION_UNSUPPORTED"
    IMPORT_UNSUPPORTED = "IMPORT_UNSUPPORTED"
    SCENARIO_OUT_OF_RANGE = "SCENARIO_OUT_OF_RANGE"
    # --- M3: endpoints and promotion --------------------------------------------------------
    PROMOTION_BLOCKED = "PROMOTION_BLOCKED"
    OVERRIDE_REQUIRED = "OVERRIDE_REQUIRED"
    # --- M3: integrations (webhooks, embeds) ------------------------------------------------
    WEBHOOK_URL_BLOCKED = "WEBHOOK_URL_BLOCKED"
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
    EMBED_NOT_ALLOWED = "EMBED_NOT_ALLOWED"
    LINK_EXPIRED = "LINK_EXPIRED"
    # --- M3: trust lifecycle ----------------------------------------------------------------
    EMAIL_NOT_ALLOWED = "EMAIL_NOT_ALLOWED"
    TERMS_NOT_ACCEPTED = "TERMS_NOT_ACCEPTED"
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    IDENTITY_VERIFICATION_REQUIRED = "IDENTITY_VERIFICATION_REQUIRED"
    # --- M3.5: About cards and the chooser -------------------------------------------------
    ABOUT_INCOMPLETE = "ABOUT_INCOMPLETE"
    CHOOSER_ANSWER_INVALID = "CHOOSER_ANSWER_INVALID"
    # --- M3.5: workflows ----------------------------------------------------------------------
    WORKFLOW_INVALID = "WORKFLOW_INVALID"
    EXPRESSION_INVALID = "EXPRESSION_INVALID"
    EXPRESSION_ERROR = "EXPRESSION_ERROR"
    NODE_UNKNOWN = "NODE_UNKNOWN"
    SCHEDULE_INVALID = "SCHEDULE_INVALID"
    WORKFLOW_NOT_RUNNABLE = "WORKFLOW_NOT_RUNNABLE"
    # --- M3.5: governance -----------------------------------------------------------------------
    POLICY_INVALID = "POLICY_INVALID"
    VALIDATION_TRANSITION_INVALID = "VALIDATION_TRANSITION_INVALID"
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"
    NOT_VALIDATED = "NOT_VALIDATED"
    # --- M3.5: explorers and optimisation -----------------------------------------------------
    EXPLORE_INVALID = "EXPLORE_INVALID"
    OPTIMIZE_INFEASIBLE = "OPTIMIZE_INFEASIBLE"
    # --- forecasts conditioned on actuals (additive, after v0.5) ----------------------------
    ACTUALS_NOT_SUPPORTED = "ACTUALS_NOT_SUPPORTED"
    ACTUALS_INVALID = "ACTUALS_INVALID"
    # --- M4: tree datasets, HDF5 and multi-file versions -------------------------------------
    NOT_READY = "NOT_READY"
    INGEST_UNSAFE_FILE = "INGEST_UNSAFE_FILE"


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
    ErrorCode.DIM_POLICY_LOOSENED: ErrorInfo(422, "A dim policy can only become stricter", "spec"),
    ErrorCode.SCHEMA_DYNAMIC: ErrorInfo(422, "Schema needs a dry-run to resolve", "spec"),
    ErrorCode.SIDE_TABLE_COVERAGE: ErrorInfo(422, "A side table is not available", "spec"),
    ErrorCode.PLOT_UNSAFE: ErrorInfo(422, "The plot spec violates the plot safety policy", "spec"),
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
    # M3 (additive)
    ErrorCode.GUIDED_ANSWERS_INVALID: ErrorInfo(
        422, "The guided answers do not fit the template", "spec"
    ),
    ErrorCode.BUSINESS_PRIORS_INVALID: ErrorInfo(
        422, "The business priors are inconsistent", "spec"
    ),
    ErrorCode.CALIBRATION_UNSUPPORTED: ErrorInfo(
        422, "The model cannot use this calibration data", "capability"
    ),
    ErrorCode.IMPORT_UNSUPPORTED: ErrorInfo(
        422, "The imported model uses an unsupported feature", "spec"
    ),
    ErrorCode.SCENARIO_OUT_OF_RANGE: ErrorInfo(
        422, "The scenario is outside the precomputed grid", "capability"
    ),
    ErrorCode.PROMOTION_BLOCKED: ErrorInfo(409, "The scorecard blocks the promotion", "api"),
    ErrorCode.OVERRIDE_REQUIRED: ErrorInfo(
        409, "Promotion needs an administrator override with a reason", "api"
    ),
    ErrorCode.WEBHOOK_URL_BLOCKED: ErrorInfo(
        422, "The egress policy does not allow the webhook URL", "api"
    ),
    ErrorCode.WEBHOOK_SIGNATURE_INVALID: ErrorInfo(401, "Webhook signature invalid", "api"),
    ErrorCode.EMBED_NOT_ALLOWED: ErrorInfo(
        422, "Only platform plot recipes can be embedded", "api"
    ),
    ErrorCode.LINK_EXPIRED: ErrorInfo(410, "The link has expired or was revoked", "api"),
    ErrorCode.EMAIL_NOT_ALLOWED: ErrorInfo(422, "This e-mail address cannot sign up", "api"),
    ErrorCode.TERMS_NOT_ACCEPTED: ErrorInfo(422, "The terms of service were not accepted", "api"),
    ErrorCode.EMAIL_NOT_VERIFIED: ErrorInfo(403, "The e-mail address is not verified", "api"),
    ErrorCode.IDENTITY_VERIFICATION_REQUIRED: ErrorInfo(
        403, "Identity verification is necessary", "api"
    ),
    # M3.5 (additive)
    ErrorCode.ABOUT_INCOMPLETE: ErrorInfo(422, "A catalog item has no complete About card", "spec"),
    ErrorCode.CHOOSER_ANSWER_INVALID: ErrorInfo(422, "A chooser answer is not valid", "api"),
    ErrorCode.WORKFLOW_INVALID: ErrorInfo(422, "The workflow is not valid", "spec"),
    ErrorCode.EXPRESSION_INVALID: ErrorInfo(
        422, "A branch condition is not a valid expression", "spec"
    ),
    ErrorCode.EXPRESSION_ERROR: ErrorInfo(422, "A branch condition could not be evaluated", "run"),
    ErrorCode.NODE_UNKNOWN: ErrorInfo(
        422, "A workflow node names an unknown implementation", "spec"
    ),
    ErrorCode.SCHEDULE_INVALID: ErrorInfo(422, "The schedule or trigger is not valid", "spec"),
    ErrorCode.WORKFLOW_NOT_RUNNABLE: ErrorInfo(
        409, "The workflow revision cannot be run (it has errors)", "api"
    ),
    ErrorCode.POLICY_INVALID: ErrorInfo(422, "The governance policy is not valid", "spec"),
    ErrorCode.VALIDATION_TRANSITION_INVALID: ErrorInfo(
        409, "The model's validation status does not allow this", "api"
    ),
    ErrorCode.SELF_APPROVAL_FORBIDDEN: ErrorInfo(
        403, "Approver and submitter must be different people", "api"
    ),
    ErrorCode.NOT_VALIDATED: ErrorInfo(409, "The governance policy needs a validated model", "api"),
    ErrorCode.EXPLORE_INVALID: ErrorInfo(422, "The explorer request is not valid", "spec"),
    ErrorCode.OPTIMIZE_INFEASIBLE: ErrorInfo(
        422, "The budget constraints cannot all be met", "capability"
    ),
    # forecasts conditioned on actuals (additive)
    ErrorCode.ACTUALS_NOT_SUPPORTED: ErrorInfo(
        422, "The model cannot update its forecast with supplied actuals", "capability"
    ),
    ErrorCode.ACTUALS_INVALID: ErrorInfo(
        422, "The supplied actuals cannot be used for this forecast", "coords"
    ),
    # M4: tree datasets, HDF5 and multi-file versions
    ErrorCode.NOT_READY: ErrorInfo(409, "The result is not ready yet", "api", retryable=True),
    ErrorCode.INGEST_UNSAFE_FILE: ErrorInfo(
        422, "The upload was refused: it is unsafe to read", "run"
    ),
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
        #: set by :func:`from_plugin_error` once plugin text has been moved out of ``detail``
        self.platform_authored = False

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
        summary = f"validation failed with {n} error{'s' if n != 1 else ''}"
        if errors:
            summary += f". The first error is at {errors[0].pointer or '/'}: {errors[0].message}"
        super().__init__(
            detail or summary[:2000],
            code=code,
            pointer=errors[0].pointer if errors else "",
            issues=issues,
        )


class PlotUnsafe(SpecError):
    """A plot spec was rejected by the plot sanitizer (URL data, scripts, tile maps ...)."""

    default_code = ErrorCode.PLOT_UNSAFE


class CoordPlanError(MapiError):
    """A score request's coordinates violate the model's dim policies."""

    default_code = ErrorCode.COORD_NOT_ALLOWED


class CapabilityError(MapiError):
    default_code = ErrorCode.CAPABILITY_UNSUPPORTED


class ModeNotSupported(CapabilityError):
    default_code = ErrorCode.MODE_NOT_SUPPORTED


class ActualsNotSupported(CapabilityError):
    """A forecast request carries actuals (target values) the model cannot condition on."""

    default_code = ErrorCode.ACTUALS_NOT_SUPPORTED


class ActualsInvalid(CoordPlanError):
    """The actuals of a forecast request leave gaps, start too late or lack their drivers."""

    default_code = ErrorCode.ACTUALS_INVALID


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


class UnsafeUpload(RunFailure):
    """An upload uses a file feature the sandbox refuses to read (HDF5 external links,
    virtual datasets, dynamic filter plugins ...; M4)."""

    default_code = ErrorCode.INGEST_UNSAFE_FILE


_CLASS_FOR_CODE: dict[ErrorCode, type[MapiError]] = {
    ErrorCode.SCHEMA_INVALID: SpecError,
    ErrorCode.SPEC_PARSE_ERROR: SpecParseError,
    ErrorCode.PAYLOAD_TOO_LARGE: PayloadTooLarge,
    ErrorCode.UNSUPPORTED_API_VERSION: UnsupportedApiVersion,
    ErrorCode.REF_UNRESOLVED: RefUnresolved,
    ErrorCode.PLOT_UNSAFE: PlotUnsafe,
    ErrorCode.UNSEEN_LEVELS: CoordPlanError,
    ErrorCode.COORD_NOT_ALLOWED: CoordPlanError,
    ErrorCode.HORIZON_EXCEEDS_FIT: CoordPlanError,
    ErrorCode.MISSING_FUTURE_COVARIATE: CoordPlanError,
    ErrorCode.NON_CONTIGUOUS_EXTENSION: CoordPlanError,
    ErrorCode.RECURSIVE_UNSUPPORTED: CoordPlanError,
    ErrorCode.CAPABILITY_UNSUPPORTED: CapabilityError,
    ErrorCode.MODE_NOT_SUPPORTED: ModeNotSupported,
    ErrorCode.ACTUALS_NOT_SUPPORTED: ActualsNotSupported,
    ErrorCode.ACTUALS_INVALID: ActualsInvalid,
    ErrorCode.QUOTA_EXCEEDED: QuotaExceeded,
    ErrorCode.USER_CODE_ERROR: UserCodeError,
    ErrorCode.RESOURCE_EXHAUSTED: ResourceExhausted,
    ErrorCode.INFRA_ERROR: InfraError,
    ErrorCode.INTEGRITY_FAILURE: IntegrityFailure,
    ErrorCode.TIMEOUT: RunTimeout,
    ErrorCode.SANDBOX_VIOLATION: SandboxViolation,
    ErrorCode.INGEST_UNSAFE_FILE: UnsafeUpload,
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


# ---------------------------------------------------------------- errors raised by plugin code

#: Codes that step, model and plot-recipe code may raise itself: spec, coordinate and
#: capability problems, ``USER_CODE_ERROR`` and ``INTEGRITY_FAILURE`` (platform checks such
#: as the no-pickle guard run inside plugin calls). Anything else (retryable infrastructure,
#: sandbox, quota, auth, timeout, internal ...) is the platform's to assert and becomes
#: ``USER_CODE_ERROR`` when plugin code raises it.
PLUGIN_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {c for c, info in ERROR_INFO.items() if info.category in ("spec", "coords", "capability")}
    | {ErrorCode.USER_CODE_ERROR, ErrorCode.INTEGRITY_FAILURE}
)

_SAFE_POINTER = re.compile(r"^(?:/[^\x00-\x1f\x7f<>\"'`\\]{0,128}){0,32}$")


def _join_untrusted(*parts: str | None) -> str | None:
    text = "\n".join(p for p in parts if p)
    return text[:8000] or None


def _plugin_issue(issue: Issue) -> Issue:
    code = issue.code if issue.code in PLUGIN_ERROR_CODES else ErrorCode.USER_CODE_ERROR
    return issue.model_copy(
        update={
            "code": code,
            "message": ERROR_INFO[code].title,
            "pointer": issue.pointer if _SAFE_POINTER.match(issue.pointer or "") else "",
            "untrusted_text": _join_untrusted(issue.message, issue.untrusted_text),
        }
    )


def from_plugin_error(
    exc: MapiError, message: str, *, fallback: type[MapiError] | None = None
) -> MapiError:
    """Re-issue a :class:`MapiError` raised by plugin (step, model, recipe) code (plan §9).

    Every string the plugin chose (``detail``, issue messages) moves to ``untrusted_text``;
    the new ``detail`` is ``message`` (platform-authored) plus the code's title. The code is
    kept when it is in :data:`PLUGIN_ERROR_CODES` (the exception object is updated in place,
    so its class and traceback survive), otherwise a ``fallback`` (default
    :class:`UserCodeError`) carrying ``USER_CODE_ERROR`` is returned.
    Pointers that are not plain JSON pointers are dropped. Idempotent.
    """
    if exc.platform_authored:
        return exc
    keep = exc.code in PLUGIN_ERROR_CODES
    code = exc.code if keep else ErrorCode.USER_CODE_ERROR
    detail = f"{message}: {ERROR_INFO[code].title}"[:2000]
    untrusted = _join_untrusted(exc.detail, exc.untrusted_text)
    pointer = exc.pointer if _SAFE_POINTER.match(exc.pointer or "") else ""
    issues = [_plugin_issue(i) for i in exc.issues]
    out: MapiError
    if keep:
        out = exc
        out.args = (detail,)
        out.detail, out.pointer, out.untrusted_text, out.issues = detail, pointer, untrusted, issues
    else:
        cls = fallback or UserCodeError
        out = cls(detail, code=code, pointer=pointer, untrusted_text=untrusted, issues=issues)
        out.__cause__ = exc
    out.platform_authored = True
    return out
