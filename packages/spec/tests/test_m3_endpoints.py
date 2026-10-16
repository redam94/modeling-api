"""M3 endpoints: promotion bodies, score calls (dataset refs or inline XrJSON), the input
schema snapshot with its compatibility rules and example payloads."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.endpoints import (
    INPUT_INLINE_DATA,
    INPUT_INLINE_SCENARIO,
    EndpointPromote,
    EndpointRollback,
    EndpointSchemaSnapshot,
    EndpointScoreCall,
    endpoint_ref,
    example_score_call,
    schema_breaks,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.xrjson import (
    MAX_XRJSON_BYTES,
    XrJsonDataset,
    check_xrjson_size,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

REF = endpoint_ref("growth", "geo-mmm-prod")
SNAP = EndpointSchemaSnapshot.model_validate(
    EndpointSchemaSnapshot.model_json_schema()["examples"][0]
)
INLINE = EndpointScoreCall.model_json_schema()["examples"][1]["data"]


def test_promotion_bodies() -> None:
    for ex in EndpointPromote.model_json_schema()["examples"]:
        EndpointPromote.model_validate(ex)
    with pytest.raises(ValidationError, match="exactly one"):
        EndpointPromote()
    with pytest.raises(ValidationError, match="exactly one"):
        EndpointPromote(model_version_id="mv_01jaf3e1f2g3h4j5k6m7n8p9q0", model="model://a/b@v1")
    with pytest.raises(ValidationError, match="reason"):
        EndpointPromote(model="model://a/b@v1", override=True)
    with pytest.raises(ValidationError):
        EndpointPromote(model="model://a/b@latest")
    with pytest.raises(ValidationError):
        EndpointRollback(reason="")


def test_score_calls_become_score_requests_for_the_endpoint() -> None:
    call = EndpointScoreCall.model_validate(EndpointScoreCall.model_json_schema()["examples"][0])
    req = call.to_score_request(REF)
    assert str(req.model) == REF and req.mode == "forecast"
    assert req.future_covariates is not None and req.future_covariates.value == "carry_last"
    assert call.inline_inputs() == {}
    ref = EndpointScoreCall(mode="evaluate", data="dataset://growth/holdout@v2")  # type: ignore[arg-type]
    assert str(ref.to_score_request(REF).data) == "dataset://growth/holdout@v2"


def test_inline_payloads_round_trip_and_become_run_inputs() -> None:
    call = EndpointScoreCall.model_validate(EndpointScoreCall.model_json_schema()["examples"][1])
    assert isinstance(call.data, XrJsonDataset)
    again = EndpointScoreCall.model_validate_json(call.model_dump_json())
    assert again == call
    req = call.to_score_request(REF)
    assert req.data is None  # carried as a run input instead
    assert set(call.inline_inputs()) == {INPUT_INLINE_DATA}
    both = call.model_copy(update={"scenario": call.data})
    assert set(both.inline_inputs()) == {INPUT_INLINE_DATA, INPUT_INLINE_SCENARIO}


def test_filter_and_evaluate_need_uploaded_data() -> None:
    for mode in ("filter", "evaluate"):
        with pytest.raises(ValidationError, match="dataset://"):
            EndpointScoreCall(mode=mode, data=XrJsonDataset.model_validate(INLINE))  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            EndpointScoreCall(mode=mode)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------- XrJSON


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"data_vars": {"x": {"dims": ["a"], "data": [[1]]}}}, "deeper"),
        ({"data_vars": {"x": {"dims": ["a", "b"], "data": [1, 2]}}}, "less deep"),
        ({"data_vars": {"x": {"dims": ["a", "b"], "data": [[1, 2], [3]]}}}, "ragged"),
        ({"data_vars": {"x": {"dims": ["a", "a"], "data": [[1]]}}}, "unique"),
        ({"data_vars": {"x": {"dims": ["market"], "data": [1, 2]}}}, "expected 1"),
        ({"data_vars": {}}, "at least 1"),
        ({"data_vars": {"x": {"dims": [], "data": "s" * 2000}}}, "strings"),
        ({"data_vars": {"market": {"dims": ["market"], "data": [1]}}}, "both coord"),
        ({"dims": {"t": -1}, "data_vars": {"x": {"dims": [], "data": 1}}}, "negative"),
    ],
)
def test_xrjson_envelope_rules(change: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        XrJsonDataset.model_validate({**copy.deepcopy(INLINE), **change})


def test_xrjson_caps() -> None:
    big = {"data_vars": {"x": {"dims": ["a"], "data": [0.0] * 1_000_001}}}
    with pytest.raises(ValidationError, match="1000000"):
        XrJsonDataset.model_validate(big)
    assert check_xrjson_size(INLINE) < 1000
    with pytest.raises(MapiError) as exc:
        check_xrjson_size({"x": "y" * MAX_XRJSON_BYTES})
    assert exc.value.code is ErrorCode.PAYLOAD_TOO_LARGE
    scalar = XrJsonDataset.model_validate({"data_vars": {"x": {"dims": [], "data": 1.5}}})
    assert scalar.var_names == ["x"]


# ------------------------------------------------------------------------- compatibility


def _schema(**patch: Any) -> DataSchema:
    data = copy.deepcopy(SNAP.input_schema.model_dump(mode="json"))
    for path, value in patch.items():
        node = data
        *parents, leaf = path.split("__")
        for p in parents:
            node = node[p]
        if value is None:
            node.pop(leaf)
        else:
            node[leaf] = value
    return DataSchema.model_validate(data)


def test_identical_and_loosened_schemas_are_compatible() -> None:
    old = SNAP.input_schema
    assert schema_breaks(old, old) == []
    looser = _schema(dims__channel={"kind": "categorical", "policy": "new_levels"})
    assert schema_breaks(old, looser) == []
    optional = _schema(vars__extra={"dims": ["date"], "dtype": "float64", "missing": "allow"})
    assert schema_breaks(old, optional) == []
    new_target = _schema(vars__units={"dims": ["date"], "dtype": "float64", "role": "target"})
    assert schema_breaks(old, new_target) == []


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"dims__market": None, "vars__revenue_local__dims": ["date"],
          "vars__spend_local__dims": ["channel", "date"]}, "'market' is gone"),
        ({"dims__date": {"kind": "integer", "policy": "extend"}}, "changed from time"),
        ({"dims__date": {"kind": "time", "policy": "fixed"}}, "policy changed"),
        ({"dims__market": {"kind": "categorical"}}, "policy changed"),
        ({"vars__price": {"dims": ["date"], "dtype": "float64"}}, "new required input"),
        ({"vars__spend_local__dims": ["channel", "market", "date"]}, "now has dims"),
        ({"vars__spend_local__dtype": "str"}, "changed type"),
    ],
)  # fmt: skip
def test_breaking_changes_are_explained(patch: dict[str, Any], needle: str) -> None:
    breaks = schema_breaks(SNAP.input_schema, _schema(**patch))
    assert any(needle in b for b in breaks), breaks


# -------------------------------------------------------------------------------- examples


def test_forecasting_endpoints_get_a_forecast_example() -> None:
    ex = example_score_call(SNAP)
    assert ex.mode == "forecast" and ex.horizon is not None and ex.horizon.dim == "time"
    ex.to_score_request(REF)


def test_other_endpoints_get_an_inline_predict_example() -> None:
    snap = SNAP.model_copy(update={"score_modes": ["predict"], "horizon_dim": None})
    ex = example_score_call(snap)
    assert ex.mode == "predict" and isinstance(ex.data, XrJsonDataset)
    assert set(ex.data.data_vars) == {"spend_local"}  # the target is not an input
    assert ex.data.coords["market"].data == ["de", "fr"]
    assert ex.data.coords["date"].data == ["2025-12-30"]
    req = ex.to_score_request(REF)
    assert req.mode == "predict"
    # the example survives the JSON round trip GET /schema serves it through
    assert EndpointScoreCall.model_validate_json(ex.model_dump_json()) == ex
