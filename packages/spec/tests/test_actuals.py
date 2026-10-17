"""Forecasts conditioned on actuals: the shared contract (``mapi_spec.actuals``).

The static rules (``validate_score_request`` with ``DataSummary.observed``), the capability,
the problem codes, the conditioning record, the JSON null check of inline payloads, the
endpoint example and the inverse of the frequency grid.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mapi_spec import Capabilities, Catalog, DataSchema, ModelSpec, ScoreRequest
from mapi_spec.actuals import (
    CONDITIONING_ATTR,
    MAX_CONDITIONING_SERIES,
    ForecastConditioning,
    SeriesConditioning,
    actuals_not_supported,
    build_conditioning,
    format_label,
)
from mapi_spec.api import ScoreSummary
from mapi_spec.coords import CoordSummary, DataSummary, parse_freq
from mapi_spec.endpoints import (
    EndpointSchema,
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    example_actuals_call,
)
from mapi_spec.errors import (
    ERROR_INFO,
    ActualsInvalid,
    ActualsNotSupported,
    ErrorCode,
    Issue,
    Severity,
    error_for_code,
    http_status,
)
from mapi_spec.loader import parse_yaml
from mapi_spec.validate import ScoreMetadata, validate_score_request, validate_spec
from mapi_spec.xrjson import XrJsonDataset, observed_labels

pytestmark = pytest.mark.unit

# ------------------------------------------------------------------------------- fixtures


def _manifest(*, actuals: bool, missing: str = "forbid", exog: bool = False) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "y": {"dims": ["t"], "dtype": "float64", "role": "target", "missing": missing}
    }
    if exog:
        variables["x"] = {
            "dims": ["t"],
            "dtype": "float64",
            "role": "covariate",
            "future_required": True,
        }
    caps: dict[str, Any] = {
        "inference": "kalman",
        "representation": "quantiles",
        "score_modes": ["predict", "forecast", "evaluate"],
        "loo_valid": False,
        "loo_invalid_reason": "point estimates",
    }
    if actuals:
        caps["conditions_on_actuals"] = True
    return {
        "id": "ts.sm",
        "version": "1.0.0",
        "capabilities": caps,
        "schema_template": {
            "template": {"dims": {"t": {"kind": "time", "policy": "extend"}}, "vars": variables}
        },
        "outputs": {"y_hat": {"dims": ["t"]}},
    }


SPEC = """\
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: air, project: default}
data:
  dataset: dataset://default/air@latest
model:
  use: ts.sm@^1
  fit: {mode: train_only}
"""

FIT_T = {"kind": "time", "size": 144, "start": "1949-01-01", "end": "1960-12-01", "freq": "MS"}


def _meta(*, actuals: bool = True, missing: str = "forbid", exog: bool = False) -> ScoreMetadata:
    catalog = Catalog.model_validate(
        {"models": [{"manifest": _manifest(actuals=actuals, missing=missing, exog=exog)}]}
    )
    raw: dict[str, Any] = {
        "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
        "vars": {"y": {"dims": ["t"], "dtype": "int64", "role": "target"}},
    }
    if exog:
        raw["vars"]["x"] = {"dims": ["t"], "dtype": "float64", "future_required": True}
    spec = parse_yaml(SPEC)
    assert isinstance(spec, dict)
    if exog:
        spec["model"]["settings"] = {}
    res = validate_spec(ModelSpec.model_validate(spec), catalog, DataSchema.model_validate(raw))
    rs = res.raise_for_errors()
    return ScoreMetadata(
        resolved=rs, fit_coords={s: {"t": CoordSummary.model_validate(FIT_T)} for s in rs.stages}
    )


def _req(periods: int = 8, **kw: Any) -> ScoreRequest:
    body: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://default/ts-forecast",
        "mode": "forecast",
        "horizon": {"dim": "t", "periods": periods},
        "output": {"summary": ["median", "q5", "q95"]},
        **kw,
    }
    return ScoreRequest.model_validate(body)


def _months(start: str, end: str, size: int, **kw: Any) -> dict[str, Any]:
    return {"kind": "time", "size": size, "start": start, "end": end, "freq": "MS", **kw}


def _inputs(
    payload: tuple[str, str, int],
    observed: tuple[str, str, int] | None,
    *,
    contiguous: bool = True,
    vars_: tuple[str, ...] = ("y",),
) -> DataSummary:
    doc: dict[str, Any] = {"vars": list(vars_), "coords": {"t": _months(*payload)}}
    if observed is not None:
        doc["observed"] = {"y": {"t": _months(*observed, contiguous=contiguous)}}
    return DataSummary.model_validate(doc)


def _codes(issues: list[Issue], severity: Severity = Severity.ERROR) -> list[str]:
    return [i.code.value for i in issues if i.severity is severity]


# ------------------------------------------------------------------- capability and codes


def test_capability_needs_the_forecast_mode_and_is_left_out_when_false() -> None:
    base = _manifest(actuals=False)["capabilities"]
    caps = Capabilities.model_validate(base)
    assert caps.conditions_on_actuals is False
    assert "conditions_on_actuals" not in caps.model_dump(mode="json")  # old digests stay
    on = Capabilities.model_validate({**base, "conditions_on_actuals": True})
    assert on.model_dump(mode="json")["conditions_on_actuals"] is True
    with pytest.raises(ValueError, match="forecast"):
        Capabilities.model_validate(
            {**base, "score_modes": ["predict"], "conditions_on_actuals": True}
        )


def test_problem_codes_are_422_with_their_own_exceptions() -> None:
    for code in (ErrorCode.ACTUALS_NOT_SUPPORTED, ErrorCode.ACTUALS_INVALID):
        assert http_status(code) == 422 and code in ERROR_INFO
    assert ERROR_INFO[ErrorCode.ACTUALS_NOT_SUPPORTED].category == "capability"
    assert isinstance(error_for_code(ErrorCode.ACTUALS_NOT_SUPPORTED, "x"), ActualsNotSupported)
    assert isinstance(error_for_code(ErrorCode.ACTUALS_INVALID, "x"), ActualsInvalid)


def test_the_refusal_says_what_was_sent_and_what_to_do() -> None:
    exc = actuals_not_supported(
        "ts.hsgp_trend@1.0.0",
        var="y",
        n=3,
        first="1961-01-01T00:00:00",
        last="1961-03-01T00:00:00",
        fit_end="1960-12-01T00:00:00",
    )
    problem = exc.to_problem()
    assert problem.status == 422 and problem.code is ErrorCode.ACTUALS_NOT_SUPPORTED
    assert problem.type == "urn:mapi:error:ACTUALS_NOT_SUPPORTED"
    issue = problem.errors[0]
    assert "3 periods (1961-01-01 to 1961-03-01)" in issue.message
    assert "1960-12-01" in issue.message and "T00:00:00" not in issue.message
    assert issue.suggested_action is not None and "Refit" in issue.suggested_action
    assert issue.context == {"target": "y", "actuals": 3, "fit_end": "1960-12-01"}


# ---------------------------------------------------------------------- conditioning record


def _series(n: int, *, through: str | None, revised: int = 0, **kw: Any) -> SeriesConditioning:
    return SeriesConditioning(
        n_actuals=n,
        first_actual="1961-01-01T00:00:00" if n else None,
        actuals_through=through,
        revised=revised,
        origin=through or "1960-12-01",
        **kw,
    )


def test_conditioning_record_for_one_series() -> None:
    rec = build_conditioning(
        dim="t",
        fit_end="1960-12-01T00:00:00",
        series=[
            _series(
                3,
                through="1961-03-01T00:00:00",
                forecast_from="1961-04-01",
                forecast_to="1961-11-01",
                n_forecast=8,
            )
        ],
        horizon_source="horizon",
    )
    assert rec.conditioned and rec.n_actuals == 3 and rec.periods == 8
    assert rec.fit_end == "1960-12-01" and rec.actuals_through == "1961-03-01"
    assert rec.series[0].first_actual == "1961-01-01" and rec.parameters == "fitted"
    assert rec.message == (
        "Forecast updated with 3 actuals (1961-01-01 to 1961-03-01) after the fitted history, "
        "which ends 1960-12-01; the model's parameters were not re-estimated. 8 periods "
        "forecast from 1961-04-01 to 1961-11-01."
    )
    assert ForecastConditioning.model_validate_json(rec.model_dump_json()) == rec


def test_conditioning_record_without_actuals_and_for_panels() -> None:
    plain = build_conditioning(
        dim="t",
        fit_end="1960-12-01",
        series=[_series(0, through=None, forecast_from="1961-01-01", forecast_to="1961-08-01",
                        n_forecast=8)],
        horizon_source="horizon",
    )  # fmt: skip
    assert not plain.conditioned and plain.actuals_through is None
    assert plain.message.startswith("Forecast from the end of the fitted history (1960-12-01)")
    panel = build_conditioning(
        dim="t",
        fit_end="1960-12-01",
        series=[
            _series(2, through="1961-02-01", revised=1, series={"geo": "de"}),
            _series(0, through=None, series={"geo": "fr"}),
        ],
        horizon_source="rows",
    )
    assert panel.series_conditioned == 1 and panel.n_series == 2 and panel.revised == 1
    assert "across 1 of 2 series" in panel.message and "1 of them replaced" in panel.message
    many = build_conditioning(
        dim="t",
        fit_end="1960-12-01",
        series=[_series(0, through=None, series={"s": str(i)}) for i in range(1005)],
        horizon_source="horizon",
    )
    assert many.truncated and len(many.series) == MAX_CONDITIONING_SERIES and many.n_series == 1005


def test_the_summary_carries_the_record_and_old_summaries_still_parse() -> None:
    assert ScoreSummary.model_validate({"tables": []}).conditioning is None
    example = ScoreSummary.model_json_schema()["examples"][1]
    assert ScoreSummary.model_validate(example).conditioning is not None
    assert CONDITIONING_ATTR == "conditioning"
    assert format_label("1961-01-01T00:00:00") == "1961-01-01"
    assert format_label("1961-01-01T06:00:00") == "1961-01-01T06:00:00" and format_label(3) == 3


# ------------------------------------------------------------------ inline JSON null check


def test_observed_labels_is_a_null_check_over_json_leaves() -> None:
    payload = XrJsonDataset.model_validate(
        {
            "coords": {
                "t": {"dims": ["t"], "data": ["1961-01-01", "1961-02-01", "1961-03-01"]},
                "geo": {"dims": ["geo"], "data": ["de", "fr"]},
            },
            "data_vars": {
                "y": {"dims": ["geo", "t"], "data": [[1.0, None, None], [None, 2.0, None]]},
                "x": {"dims": ["t"], "data": [None, None, None]},
                "z": {"dims": ["geo"], "data": [1.0, 2.0]},
                "e": {"dims": ["t"], "data": ["", 0, None]},
            },
        }
    )
    assert observed_labels(payload, "y", "t") == ["1961-01-01", "1961-02-01"]
    assert observed_labels(payload, "y", "geo") == ["de", "fr"]
    assert observed_labels(payload, "x", "t") == []
    assert observed_labels(payload, "e", "t") == ["1961-02-01"]  # empty cells are missing
    assert observed_labels(payload, "z", "t") is None  # not along t
    assert observed_labels(payload, "nope", "t") is None


# ---------------------------------------------------------------------- static rules


def test_payload_without_actuals_is_unchanged() -> None:
    meta = _meta(actuals=False)
    blank = _inputs(("1961-01-01", "1961-08-01", 8), ("1961-01-01", "1961-01-01", 0))
    assert _codes(validate_score_request(_req(), meta, blank)) == []
    unknown = _inputs(("1961-01-01", "1961-08-01", 8), None)  # the summary does not say
    assert _codes(validate_score_request(_req(), meta, unknown)) == []


def test_actuals_are_refused_by_a_model_that_cannot_condition() -> None:
    meta = _meta(actuals=False)
    sent = _inputs(("1961-01-01", "1961-03-01", 3), ("1961-01-01", "1961-03-01", 3))
    issues = validate_score_request(_req(), meta, sent)
    err = next(i for i in issues if i.code is ErrorCode.ACTUALS_NOT_SUPPORTED)
    assert err.severity is Severity.ERROR and err.pointer == "/data"
    assert "3 periods (1961-01-01 to 1961-03-01)" in err.message
    assert err.suggested_action is not None
    # the live plugin build may have gained the capability since the fit
    live = meta.resolved.model.capabilities.model_copy(update={"conditions_on_actuals": True})
    again = validate_score_request(_req(), meta, sent, live_capabilities=live)
    assert "ACTUALS_NOT_SUPPORTED" not in _codes(again)
    # other modes never read targets as actuals
    predict = _req(mode="predict", horizon=None, data="dataset://default/air_new@latest")
    assert "ACTUALS_NOT_SUPPORTED" not in _codes(validate_score_request(predict, meta, sent))


def test_actuals_followed_by_the_horizon() -> None:
    meta = _meta()
    sent = _inputs(("1961-01-01", "1961-03-01", 3), ("1961-01-01", "1961-03-01", 3))
    issues = validate_score_request(_req(), meta, sent)
    assert _codes(issues) == [] and _codes(issues, Severity.WARNING) == []


def test_future_rows_replace_the_horizon_with_a_warning() -> None:
    meta = _meta()
    sent = _inputs(("1961-01-01", "1961-06-01", 6), ("1961-01-01", "1961-03-01", 3))
    issues = validate_score_request(_req(8), meta, sent)
    assert _codes(issues) == []
    warn = next(i for i in issues if i.pointer == "/horizon/periods")
    assert "lists 3 period(s) after the last actual (1961-03-01)" in warn.message
    same = validate_score_request(_req(3), meta, sent)
    assert not any(i.pointer == "/horizon/periods" for i in same)


def test_actuals_may_overlap_the_fit() -> None:
    meta = _meta()
    sent = _inputs(("1960-10-01", "1961-02-01", 5), ("1960-10-01", "1961-02-01", 5))
    assert _codes(validate_score_request(_req(), meta, sent)) == []
    inside = _inputs(("1949-01-01", "1960-12-01", 144), ("1949-01-01", "1960-12-01", 144))
    issues = validate_score_request(_req(), meta, inside)
    assert _codes(issues) == [] and _codes(issues, Severity.WARNING) == []  # not "no extension"


def test_actuals_must_continue_the_fitted_history() -> None:
    meta = _meta()
    late = _inputs(("1961-01-01", "1961-04-01", 4), ("1961-03-01", "1961-04-01", 2))
    issues = validate_score_request(_req(), meta, late)
    assert _codes(issues) == ["ACTUALS_INVALID"]
    err = next(i for i in issues if i.code is ErrorCode.ACTUALS_INVALID)
    assert "start 2 period(s) after the fitted history ends (1960-12-01)" in err.message
    # coordinates that skip periods are a different problem
    skipped = _inputs(("1961-03-01", "1961-04-01", 2), ("1961-03-01", "1961-04-01", 2))
    assert "NON_CONTIGUOUS_EXTENSION" in _codes(validate_score_request(_req(), meta, skipped))
    holes = _inputs(("1961-01-01", "1961-04-01", 4), ("1961-01-01", "1961-04-01", 3),
                    contiguous=False)  # fmt: skip
    errs = [i for i in validate_score_request(_req(), meta, holes) if i.severity is Severity.ERROR]
    assert [i.code for i in errs] == [ErrorCode.ACTUALS_INVALID]
    assert "empty for 1 period(s)" in errs[0].message
    # a model that accepts missing observations filters the gaps instead
    lenient = _meta(missing="allow")
    assert _codes(validate_score_request(_req(), lenient, late)) == []
    assert _codes(validate_score_request(_req(), lenient, holes)) == []


def test_drivers_with_actuals_and_on_the_appended_horizon() -> None:
    meta = _meta(exog=True)
    no_x = _inputs(("1961-01-01", "1961-03-01", 3), ("1961-01-01", "1961-03-01", 3))
    codes = _codes(validate_score_request(_req(), meta, no_x))
    assert "ACTUALS_INVALID" in codes  # the actual rows need their drivers
    with_x = _inputs(
        ("1961-01-01", "1961-03-01", 3), ("1961-01-01", "1961-03-01", 3), vars_=("y", "x")
    )
    issues = validate_score_request(_req(), meta, with_x)
    err = next(i for i in issues if i.code is ErrorCode.MISSING_FUTURE_COVARIATE)
    assert "appended after it" in err.message  # the 8 appended periods have no x
    filled = validate_score_request(_req(future_covariates="carry_last"), meta, with_x)
    assert _codes(filled) == []
    rows = _inputs(
        ("1961-01-01", "1961-11-01", 11), ("1961-01-01", "1961-03-01", 3), vars_=("y", "x")
    )
    assert _codes(validate_score_request(_req(), meta, rows)) == []


# ------------------------------------------------------------------------ endpoint example


def _snapshot(**kw: Any) -> EndpointSchemaSnapshot:
    base: dict[str, Any] = {
        "input_schema": {
            "dims": {"t": {"kind": "time", "policy": "extend", "freq": "MS"}},
            "vars": {"y": {"dims": ["t"], "dtype": "int64", "role": "target"}},
        },
        "schema_hash": "sha256:" + "0" * 64,
        "score_modes": ["evaluate", "forecast", "predict"],
        "representation": "quantiles",
        "horizon_dim": "t",
        "fit_coords": {"t": FIT_T},
        "conditions_on_actuals": True,
    }
    base.update(kw)
    return EndpointSchemaSnapshot.model_validate(base)


def test_actuals_example_is_a_forecast_over_the_next_periods_with_empty_targets() -> None:
    call = example_actuals_call(_snapshot(), periods=3)
    assert call is not None and call.mode == "forecast" and call.horizon is not None
    assert call.future_covariates.value == "carry_last"
    assert isinstance(call.data, XrJsonDataset)
    assert call.data.coords["t"].data == ["1961-01-01", "1961-02-01", "1961-03-01"]
    assert call.data.data_vars["y"].data == [None, None, None]
    body = call.model_dump(mode="json", by_alias=True, exclude_defaults=True)
    assert EndpointScoreCall.model_validate(body) == call
    doc = EndpointSchema.model_json_schema()["examples"][0]
    assert EndpointSchema.model_validate({**doc, "actuals_example": body}).actuals_example
    assert example_actuals_call(_snapshot(conditions_on_actuals=False)) is None
    assert example_actuals_call(_snapshot(score_modes=["predict"])) is None


def test_actuals_example_for_panels_with_drivers() -> None:
    snap = _snapshot(
        input_schema={
            "dims": {
                "geo": {"kind": "categorical"},
                "t": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
            },
            "vars": {
                "y": {"dims": ["geo", "t"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["geo", "t"], "dtype": "float64", "future_required": True},
            },
        },
        fit_coords={
            "geo": {"kind": "categorical", "size": 3, "values": ["de", "fr", "it"]},
            "t": {"kind": "time", "size": 10, "start": "2026-01-04", "end": "2026-03-08",
                  "freq": "W-SUN"},
        },
    )  # fmt: skip
    call = example_actuals_call(snap, periods=2)
    assert call is not None and isinstance(call.data, XrJsonDataset)
    assert call.data.coords["t"].data == ["2026-03-15", "2026-03-22"]
    assert call.data.coords["geo"].data == ["de", "fr"]
    assert call.data.data_vars["spend"].data == [[None, None], [None, None]]


# --------------------------------------------------------------------- frequency inverse


@given(
    st.sampled_from(["D", "W-SUN", "W-WED", "2W-SUN", "MS", "ME", "QS", "QE", "YS", "YE", "h",
                     "15min", "3MS", "2QE"]),
    st.integers(min_value=-200, max_value=200),
)  # fmt: skip
def test_freq_label_inverts_ordinal(alias: str, step: int) -> None:
    freq = parse_freq(alias)
    base = next(
        o
        for d in (datetime(1960, 12, 1), datetime(1960, 12, 31), datetime(2024, 1, 7),
                  datetime(2024, 1, 3), datetime(2024, 1, 1), datetime(2024, 12, 31),
                  datetime(2024, 9, 30))
        if (o := freq.ordinal(d)) is not None
    )  # fmt: skip
    assert freq.ordinal(freq.label(base + step)) == base + step


def test_an_unused_horizon_is_never_silent() -> None:
    meta = _meta(actuals=False)
    rows = _inputs(("1961-01-01", "1961-06-01", 6), ("1961-01-01", "1961-01-01", 0))
    warn = [
        i for i in validate_score_request(_req(8), meta, rows) if i.pointer == "/horizon/periods"
    ]
    assert warn and "lists 6 period(s) after the fit" in warn[0].message
    exact = _inputs(("1961-01-01", "1961-08-01", 8), None)
    assert not any(
        i.pointer == "/horizon/periods" for i in validate_score_request(_req(8), meta, exact)
    )
