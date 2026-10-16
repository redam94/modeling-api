"""The trust-lifecycle rows of ``mapi_spec.api.M3_ENDPOINTS`` are served exactly as
declared: method, path, operation id, success status, request and response schemas, and an
RFC 7807 problem response for every documented error status."""

from __future__ import annotations

from typing import Any

import pytest
from mapi_server.app import openapi_document
from mapi_spec.api import M3_ENDPOINTS, EndpointDecl
from mapi_spec.errors import ERROR_INFO

pytestmark = pytest.mark.unit

TRUST_OPS = frozenset(
    {
        "signup",
        "verify_email",
        "get_tenant_verification",
        "admin_put_tenant_verification",
        "request_tenant_deletion",
        "get_tenant_deletion",
        "cancel_tenant_deletion",
        "create_tenant_export",
        "get_tenant_export",
        "admin_list_signups",
        "admin_review_signup",
        "admin_retry_tenant_deletion",
    }
)
ROWS = [e for e in M3_ENDPOINTS if e.operation_id in TRUST_OPS]


@pytest.fixture(scope="module")
def doc() -> dict[str, Any]:
    return openapi_document()


def test_every_trust_row_is_listed() -> None:
    assert {e.operation_id for e in ROWS} == TRUST_OPS


def _ref_name(schema: dict[str, Any]) -> str:
    ref = schema.get("$ref", "")
    return str(ref).rsplit("/", 1)[-1]


@pytest.mark.parametrize("e", ROWS, ids=lambda e: e.operation_id)
def test_served_as_declared(doc: dict[str, Any], e: EndpointDecl) -> None:
    op = doc["paths"][e.path][e.method.lower()]
    assert op["operationId"] == e.operation_id
    responses = op["responses"]
    assert str(e.success) in responses
    if e.response is not None:
        schema = responses[str(e.success)]["content"]["application/json"]["schema"]
        name = _ref_name(schema)
        expected = e.response.replace("[", "_").replace("]", "_").rstrip("_")
        assert name.replace("_", "") == expected.replace("_", ""), (name, e.response)
    if e.request is not None:
        body = op["requestBody"]["content"]["application/json"]["schema"]
        assert _ref_name(body) == e.request
    for code in e.errors:
        status = str(ERROR_INFO[code].status)
        assert status in responses, (e.operation_id, code)
        problem = responses[status]["content"]["application/json"]["schema"]
        assert _ref_name(problem) == "Problem"
        assert f"`{code.value}`" in responses[status]["description"]
    assert op.get("summary") and op.get("description")


def test_request_and_response_schemas_carry_examples(doc: dict[str, Any]) -> None:
    schemas = doc["components"]["schemas"]
    for name in ("SignupRequest", "SignupAccepted", "EmailVerify", "VerificationResult",
                 "VerificationStatus", "VerificationUpdate", "TenantDeletionRequest",
                 "TenantDeletion", "GdprExportRequest", "GdprExport", "SignupReview",
                 "SignupReviewDecision"):  # fmt: skip
        assert schemas[name].get("examples"), name


def test_the_dev_outbox_is_not_documented(doc: dict[str, Any]) -> None:
    assert "/dev/outbox" not in doc["paths"]
