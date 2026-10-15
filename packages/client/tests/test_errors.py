"""RFC 7807 problems become the most specific MapiError subclass."""

from __future__ import annotations

import pytest
import respx
from mapi_client import Client, TransportError, UnexpectedResponse, http_status_of, problem_of
from mapi_spec.errors import (
    CoordPlanError,
    ErrorCode,
    InfraError,
    Issue,
    MapiError,
    Problem,
    QuotaExceeded,
)

BASE = "http://api.test"
RUN = f"{BASE}/v1/runs/run_01"


def test_problem_maps_to_specific_class(client: Client, api: respx.MockRouter) -> None:
    problem = Problem.from_code(
        ErrorCode.UNSEEN_LEVELS,
        "2 markets were not seen at fit time",
        errors=[Issue(code=ErrorCode.UNSEEN_LEVELS, pointer="/select/geo", message="unseen")],
    )
    api.get(RUN).respond(
        422,
        content=problem.model_dump_json(),
        headers={"content-type": "application/problem+json"},
    )
    with pytest.raises(CoordPlanError) as err:
        client.runs.get("run_01")
    exc = err.value
    assert exc.code is ErrorCode.UNSEEN_LEVELS
    assert exc.pointer == "/select/geo"
    assert http_status_of(exc) == 422
    assert problem_of(exc) == problem


def test_problems_with_extra_fields_still_parse(client: Client, api: respx.MockRouter) -> None:
    body = Problem.from_code(ErrorCode.QUOTA_EXCEEDED, "too many runs").model_dump(mode="json")
    body["retry_hint"] = "tomorrow"  # added by a newer server
    api.get(RUN).respond(429, json=body)
    with pytest.raises(QuotaExceeded):
        client.runs.get("run_01")


def test_non_problem_errors_map_by_status(client: Client, api: respx.MockRouter) -> None:
    api.get(RUN).respond(404, text="<html>nope</html>")
    with pytest.raises(MapiError) as err:
        client.runs.get("run_01")
    assert err.value.code is ErrorCode.NOT_FOUND
    assert err.value.untrusted_text == "<html>nope</html>"
    assert problem_of(err.value) is None


def test_unknown_codes_fall_back_to_status(client: Client, api: respx.MockRouter) -> None:
    api.get(RUN).respond(409, json={"code": "SOMETHING_NEW", "title": "x", "status": 409})
    with pytest.raises(MapiError) as err:
        client.runs.get("run_01")
    assert err.value.code is ErrorCode.CONFLICT


def test_gateway_errors_are_infra_after_retries(
    client: Client, api: respx.MockRouter, sleeps: list[float]
) -> None:
    route = api.get(RUN).respond(502, text="bad gateway")
    with pytest.raises(InfraError):
        client.runs.get("run_01")
    assert route.call_count == 3  # 1 + max_retries
    assert len(sleeps) == 2


def test_invalid_bodies_raise_unexpected_response(client: Client, api: respx.MockRouter) -> None:
    api.get(RUN).respond(200, json={"id": "run_01"})
    with pytest.raises(UnexpectedResponse, match="not a valid Run"):
        client.runs.get("run_01")


def test_unexpected_success_status(client: Client, api: respx.MockRouter) -> None:
    api.get(RUN).respond(204)
    with pytest.raises(UnexpectedResponse, match="unexpected status 204"):
        client.runs.get("run_01")


def test_transport_errors(client: Client, api: respx.MockRouter) -> None:
    import httpx

    api.get(RUN).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(TransportError) as err:
        client.runs.get("run_01")
    assert err.value.code is ErrorCode.INFRA_ERROR
