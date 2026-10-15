"""SDK error types.

Every error raised by the runtime is a :class:`mapi_spec.errors.MapiError` with a stable
:class:`~mapi_spec.errors.ErrorCode`, so the runner can turn it into an RFC 7807 problem
without guessing. User- or plugin-originated text goes into ``untrusted_text``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from mapi_spec.errors import (
    CapabilityError,
    CoordPlanError,
    ErrorCode,
    IntegrityFailure,
    Issue,
    MapiError,
    ModeNotSupported,
    Severity,
    SpecError,
    UserCodeError,
    ValidationFailed,
    error_for_code,
)

__all__ = [
    "BundleError",
    "CapabilityError",
    "CoordPlanError",
    "MapiError",
    "ModeNotSupported",
    "PayloadLimitError",
    "RunCancelled",
    "SchemaValidationError",
    "StepError",
    "UserCodeError",
    "ValidationFailed",
    "WireFormatError",
    "raise_for_issues",
]


class WireFormatError(SpecError):
    """A payload (XrJSON, zarr zip, Arrow) is malformed or uses a forbidden feature."""

    default_code = ErrorCode.SCHEMA_INVALID


class PayloadLimitError(WireFormatError):
    """A payload exceeds an element, depth, string or size cap."""

    default_code = ErrorCode.PAYLOAD_TOO_LARGE


class SchemaValidationError(SpecError):
    """A dataset does not match its :class:`~mapi_spec.schema.DataSchema`."""

    default_code = ErrorCode.SCHEMA_INVALID


class BundleError(IntegrityFailure):
    """A bundle is unreadable, inconsistent or contains forbidden content (e.g. pickle)."""


class StepError(UserCodeError):
    """A preprocessing step failed (or broke its contract) at fit or transform time."""


class RunCancelled(MapiError):
    """The run was cancelled cooperatively (checked between pipeline phases)."""

    default_code = ErrorCode.CANCELLED


def raise_for_issues(issues: Iterable[Issue], *, detail: str | None = None) -> None:
    """Raise the most specific :class:`MapiError` for the first error in ``issues``.

    Every issue (including warnings) is attached to the exception so callers can report
    them all. Does nothing when there are no errors.
    """
    all_issues: Sequence[Issue] = list(issues)
    errors = [i for i in all_issues if i.severity is Severity.ERROR]
    if not errors:
        return
    first = errors[0]
    msg = detail or first.message
    if len(errors) > 1:
        msg = f"{msg} (and {len(errors) - 1} more error(s))"
    raise error_for_code(
        first.code,
        msg[:2000],
        pointer=first.pointer,
        untrusted_text=first.untrusted_text,
        issues=all_issues,
    )
