from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_spec.errors import (
    ERROR_INFO,
    CoordPlanError,
    ErrorCode,
    Issue,
    MapiError,
    ModeNotSupported,
    Problem,
    ResourceExhausted,
    Severity,
    SpecError,
    UserCodeError,
    ValidationFailed,
    error_for_code,
    error_from_problem,
    has_errors,
    http_status,
    join_pointer,
    json_pointer,
    parse_pointer,
)

pytestmark = pytest.mark.unit

REQUIRED_CODES = [
    "SCHEMA_INVALID",
    "LEAKAGE_RISK",
    "UNSEEN_LEVELS",
    "COORD_NOT_ALLOWED",
    "HORIZON_EXCEEDS_FIT",
    "MISSING_FUTURE_COVARIATE",
    "NON_CONTIGUOUS_EXTENSION",
    "CAPABILITY_UNSUPPORTED",
    "RECURSIVE_UNSUPPORTED",
    "MODE_NOT_SUPPORTED",
    "QUOTA_EXCEEDED",
    "USER_CODE_ERROR",
    "RESOURCE_EXHAUSTED",
    "INFRA_ERROR",
    "INTEGRITY_FAILURE",
    "TIMEOUT",
    "SANDBOX_VIOLATION",
]


def test_plan_codes_exist_and_are_stable() -> None:
    for name in REQUIRED_CODES:
        assert ErrorCode(name).value == name


def test_every_code_has_info() -> None:
    assert set(ERROR_INFO) == set(ErrorCode)
    assert http_status(ErrorCode.HORIZON_EXCEEDS_FIT) == 409
    assert http_status(ErrorCode.UNSEEN_LEVELS) == 422
    assert http_status("SPEC_PARSE_ERROR") == 400
    assert http_status("NOPE") == 500


@given(st.lists(st.one_of(st.text(max_size=10), st.integers(0, 99)), max_size=6))
def test_pointer_roundtrip(parts: list[str | int]) -> None:
    ptr = json_pointer(*parts)
    assert parse_pointer(ptr) == [str(p) for p in parts]


def test_pointer_escaping() -> None:
    assert json_pointer("a/b", "c~d", 0) == "/a~1b/c~0d/0"
    assert join_pointer("/pipeline/1", "with", "vars") == "/pipeline/1/with/vars"
    assert parse_pointer("") == []
    with pytest.raises(ValueError):
        parse_pointer("a/b")


def test_problem_from_error_keeps_untrusted_text_separate() -> None:
    err = UserCodeError("plugin raised", untrusted_text="Traceback ...\n<script>")
    prob = err.to_problem(instance="/runs/1", trace_id="t1")
    assert prob.code is ErrorCode.USER_CODE_ERROR
    assert prob.status == 422 and prob.type.endswith("USER_CODE_ERROR")
    assert prob.detail == "plugin raised"
    assert "<script>" not in (prob.detail or "")
    assert prob.untrusted_text == "Traceback ...\n<script>"
    assert prob.errors[0].untrusted_text == "Traceback ...\n<script>"
    back = Problem.model_validate_json(prob.model_dump_json())
    assert back == prob


def test_validation_failed_uses_first_error_code() -> None:
    issues = [
        Issue(code=ErrorCode.SCHEMA_DYNAMIC, severity=Severity.WARNING, message="w"),
        Issue(code=ErrorCode.LEAKAGE_RISK, pointer="/data/split", message="leak"),
        Issue(code=ErrorCode.SCHEMA_INVALID, message="bad"),
    ]
    exc = ValidationFailed(issues)
    assert exc.code is ErrorCode.LEAKAGE_RISK
    assert exc.pointer == "/data/split"
    assert "2 errors" in exc.detail
    assert exc.to_problem().errors == issues
    assert has_errors(issues)
    assert not has_errors(issues[:1])
    assert isinstance(exc, SpecError)


def test_error_factories() -> None:
    e = error_for_code(ErrorCode.HORIZON_EXCEEDS_FIT, "too far", pointer="/horizon")
    assert isinstance(e, CoordPlanError) and e.status == 409 and e.pointer == "/horizon"
    assert isinstance(error_for_code(ErrorCode.MODE_NOT_SUPPORTED, "x"), ModeNotSupported)
    assert isinstance(error_for_code(ErrorCode.RESOURCE_EXHAUSTED, "x"), ResourceExhausted)
    assert type(error_for_code(ErrorCode.NOT_FOUND, "x")) is MapiError
    prob = Problem.from_code(
        ErrorCode.UNSEEN_LEVELS,
        "levels",
        errors=[Issue(code=ErrorCode.UNSEEN_LEVELS, pointer="/select/geo", message="m")],
    )
    err = error_from_problem(prob)
    assert isinstance(err, CoordPlanError)
    assert err.pointer == "/select/geo" and err.code is ErrorCode.UNSEEN_LEVELS
    assert "UNSEEN_LEVELS" in repr(err)


def test_issue_limits() -> None:
    with pytest.raises(ValueError):
        Issue(code=ErrorCode.SCHEMA_INVALID, message="x" * 2001)
    i = Issue(code=ErrorCode.SCHEMA_INVALID, message="ok")
    assert i.is_error and i.severity is Severity.ERROR


def test_plot_unsafe_code_maps_to_its_class() -> None:
    from mapi_spec.errors import PlotUnsafe

    err = error_for_code(ErrorCode.PLOT_UNSAFE, "url data")
    assert isinstance(err, PlotUnsafe) and isinstance(err, SpecError)
    assert err.status == 422
