"""HTTP-level errors raised by the shim, rendered either as GCS JSON-API or XML-API errors."""

from __future__ import annotations

from typing import Final
from xml.sax.saxutils import escape

__all__ = ["ShimError"]


class ShimError(Exception):
    """An error with a GCS-compatible HTTP status, JSON ``reason`` and XML ``Code``.

    ``headers`` are extra response headers (e.g. ``Content-Range`` on 416).
    """

    def __init__(
        self,
        status: int,
        reason: str,
        message: str,
        *,
        xml_code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.xml_code = xml_code or _XML_CODES.get(reason, "InvalidArgument")
        self.headers = dict(headers or {})

    def json_body(self) -> dict[str, object]:
        """The GCS JSON-API error document."""
        return {
            "error": {
                "code": self.status,
                "message": self.message,
                "errors": [{"message": self.message, "domain": "global", "reason": self.reason}],
            }
        }

    def xml_body(self) -> bytes:
        """The GCS XML-API error document."""
        return (
            "<?xml version='1.0' encoding='UTF-8'?><Error>"
            f"<Code>{escape(self.xml_code)}</Code><Message>{escape(self.message)}</Message>"
            "</Error>"
        ).encode()

    # ------------------------------------------------------------------ constructors

    @classmethod
    def bad_request(cls, message: str, *, xml_code: str = "InvalidArgument") -> ShimError:
        return cls(400, "invalid", message, xml_code=xml_code)

    @classmethod
    def unauthenticated(cls, message: str = "Anonymous caller") -> ShimError:
        return cls(401, "required", message, xml_code="AccessDenied")

    @classmethod
    def forbidden(cls, message: str, *, xml_code: str = "AccessDenied") -> ShimError:
        return cls(403, "forbidden", message, xml_code=xml_code)

    @classmethod
    def no_bucket(cls, bucket: str) -> ShimError:
        return cls(
            404,
            "notFound",
            f"The specified bucket does not exist: {bucket}",
            xml_code="NoSuchBucket",
        )

    @classmethod
    def no_object(cls, bucket: str, name: str, generation: int | None = None) -> ShimError:
        gen = "" if generation is None else f" (generation {generation})"
        return cls(404, "notFound", f"No such object: {bucket}/{name}{gen}", xml_code="NoSuchKey")

    @classmethod
    def precondition(cls, message: str) -> ShimError:
        return cls(412, "conditionNotMet", message, xml_code="PreconditionFailed")

    @classmethod
    def retention(cls, message: str) -> ShimError:
        return cls(403, "retentionPolicyNotMet", message, xml_code="AccessDenied")

    @classmethod
    def too_large(cls, message: str) -> ShimError:
        return cls(400, "invalid", message, xml_code="EntityTooLarge")

    @classmethod
    def too_small(cls, message: str) -> ShimError:
        return cls(400, "invalid", message, xml_code="EntityTooSmall")

    @classmethod
    def bad_digest(cls, message: str) -> ShimError:
        return cls(400, "invalid", message, xml_code="BadDigest")

    @classmethod
    def conflict(cls, message: str) -> ShimError:
        return cls(409, "conflict", message, xml_code="Conflict")

    @classmethod
    def not_allowed(cls, method: str) -> ShimError:
        return cls(
            405,
            "methodNotAllowed",
            f"Method {method} is not allowed here",
            xml_code="MethodNotAllowed",
        )


_XML_CODES: Final = {
    "invalid": "InvalidArgument",
    "required": "AccessDenied",
    "forbidden": "AccessDenied",
    "notFound": "NoSuchKey",
    "conditionNotMet": "PreconditionFailed",
    "retentionPolicyNotMet": "AccessDenied",
    "conflict": "Conflict",
    "methodNotAllowed": "MethodNotAllowed",
    "requestedRangeNotSatisfiable": "InvalidRange",
    "notModified": "NotModified",
    "backendError": "InternalError",
}
