"""M3 what-if: the response-grid view, the reference interpolation (golden values computed
by hand), and the score requests that scenarios, forecasts and presets compile to."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FutureCovariates, ScoreRequest
from mapi_spec.views import RESPONSE_GRID_FORMAT, ResponseGridView
from mapi_spec.whatif import (
    BudgetScale,
    ChannelOff,
    GuidedCompile,
    WhatIfScenario,
    compile_guided_request,
    counterfactual_request,
    evaluate_scenario,
    forecast_request,
    preset_interventions,
    scenario_score_request,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

GRID: dict[str, Any] = {
    "format": RESPONSE_GRID_FORMAT,
    "kpi": "revenue",
    "kpi_units": "EUR",
    "spend_units": "EUR",
    "periods": 52,
    "channels": ["tv", "search"],
    "geos": ["de", "fr"],
    "multipliers": [0.0, 1.0, 2.0],
    "draws": 2,
    "contribution": [[[0, 10, 15], [0, 4, 6]], [[0, 20, 30], [0, 2, 3]]],
    "spend": [[0, 100, 200], [0, 50, 100]],
    "current_spend": [100, 50],
    "baseline": [100, 200],
    "contribution_by_geo": [
        [[[0, 0], [6, 4], [9, 6]], [[0, 0], [1, 3], [2, 4]]],
        [[[0, 0], [12, 8], [18, 12]], [[0, 0], [1, 1], [2, 1]]],
    ],
    "spend_by_geo": [[[0, 60, 120], [0, 20, 40]], [[0, 40, 80], [0, 30, 60]]],
}
MODEL = "model://growth/eu-geo-mmm@v3"


def _grid(**over: Any) -> ResponseGridView:
    return ResponseGridView.model_validate({**copy.deepcopy(GRID), **over})


# ---------------------------------------------------------------------------------- the view


@pytest.mark.parametrize(
    "over",
    [
        {"multipliers": [0.0, 0.5, 2.0]},  # no 1
        {"multipliers": [1.0, 0.0, 2.0]},  # not ascending
        {"multipliers": [-1.0, 1.0, 2.0]},
        {"contribution": [[[0, 10], [0, 4, 6]], [[0, 20, 30], [0, 2, 3]]]},
        {"draws": 3},
        {"spend": [[0, 100, 200]]},
        {"current_spend": [1]},
        {"baseline": [1]},
        {"channels": ["tv", "tv"]},
        {"geos": None},
        {"spend_by_geo": [[[0, 60, 120], [0, 20, 40]]]},
        {"draws": 501},
    ],
)
def test_grid_shapes_are_checked(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _grid(**over)


def test_grid_tolerates_extra_producer_fields() -> None:
    assert _grid(produced_by="mapi-runner 0.4").draws == 2


# ----------------------------------------------------------------------- the interpolation


def test_golden_interpolation() -> None:
    res = evaluate_scenario(_grid(), WhatIfScenario(multipliers={"tv": 0.5, "search": 1.5}))
    assert res.draws == 2
    assert res.channels["tv"].spend == 50 and res.channels["search"].spend == 75
    assert res.total_spend == 125 and res.spend_delta == -25
    assert res.channels["tv"].contribution.mean == 7.5  # draws 5 and 10
    assert res.channels["search"].contribution.mean == 3.75  # draws 5 and 2.5
    assert res.kpi_delta.mean == -6.75
    assert res.kpi_delta.levels == [0.05, 0.5, 0.95]
    assert res.kpi_delta.quantiles == pytest.approx([-9.225, -6.75, -4.275])
    assert res.kpi is not None and res.kpi.mean == pytest.approx((110 + 212.5) / 2)
    assert res.roi_delta is not None and res.roi_delta.mean == pytest.approx((0.16 + 0.38) / 2)


def test_identity_scenario_changes_nothing() -> None:
    res = evaluate_scenario(_grid(), WhatIfScenario())
    assert res.spend_delta == 0 and res.roi_delta is None
    assert res.kpi_delta.quantiles == [0, 0, 0]
    on_grid = evaluate_scenario(_grid(), WhatIfScenario(multipliers={"tv": 2.0}))
    assert on_grid.channels["tv"].contribution.mean == 22.5  # exact grid point, no rounding


def test_per_geo_scenarios() -> None:
    res = evaluate_scenario(_grid(), WhatIfScenario(multipliers={"tv": 2.0}, geo="fr"))
    assert res.channels["tv"].spend == 80  # spend_by_geo[fr][tv][at 2.0]
    assert res.kpi is None  # the baseline is not split by geo
    assert res.kpi_delta.mean == pytest.approx(((6 - 4) + (12 - 8)) / 2)


@pytest.mark.parametrize(
    "scenario",
    [
        WhatIfScenario(multipliers={"tv": 2.5}),
        WhatIfScenario(multipliers={"radio": 1.0}),
        WhatIfScenario(geo="it"),
    ],
)
def test_out_of_range_scenarios_are_refused(scenario: WhatIfScenario) -> None:
    with pytest.raises(MapiError) as exc:
        evaluate_scenario(_grid(), scenario)
    assert exc.value.code is ErrorCode.SCENARIO_OUT_OF_RANGE


def test_scenario_multipliers_are_finite_and_non_negative() -> None:
    for bad in (-0.1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            WhatIfScenario(multipliers={"tv": bad})


# ------------------------------------------------------------------------- score requests


def test_run_exact_is_a_counterfactual_with_one_intervention_per_changed_channel() -> None:
    req = scenario_score_request(WhatIfScenario(multipliers={"tv": 0.8, "search": 1.0}), MODEL)
    assert req.mode == "counterfactual" and str(req.model) == MODEL
    [iv] = req.interventions
    assert (iv.var, iv.sel, iv.multiply) == ("spend", {"channel": "tv"}, 0.8)
    assert req.output.summary == ["mean", "hdi_90"] and req.output.draws == 0  # Q2
    geo = scenario_score_request(WhatIfScenario(multipliers={"tv": 2}, geo="de"), MODEL)
    assert geo.interventions[0].sel == {"channel": "tv", "geo": "de"}
    with pytest.raises(MapiError):
        scenario_score_request(WhatIfScenario(multipliers={"tv": 1.0}), MODEL)


def test_presets() -> None:
    [off] = preset_interventions(ChannelOff(channel="tv"))
    assert off.set_ == 0.0 and off.sel == {"channel": "tv"}
    [scale] = preset_interventions(BudgetScale(k=1.2, channels=["tv", "search"]))
    assert scale.multiply == 1.2 and scale.sel == {"channel": ["tv", "search"]}
    [all_ch] = preset_interventions(BudgetScale(k=0.5))
    assert all_ch.sel == {}
    cf = counterfactual_request(ChannelOff(channel="tv"), MODEL)
    assert cf.mode == "counterfactual" and cf.horizon is None
    future = counterfactual_request(
        BudgetScale(k=1.1),
        "endpoint://growth/geo-mmm-prod",
        horizon=forecast_request(MODEL, periods=13).horizon,
        future=FutureCovariates.CARRY_LAST,
    )
    assert future.mode == "forecast" and future.horizon is not None
    assert future.future_covariates is FutureCovariates.CARRY_LAST


def test_forecasts() -> None:
    req = forecast_request(MODEL, periods=13)
    assert req.mode == "forecast" and req.horizon is not None and req.horizon.periods == 13
    assert req.future_covariates is FutureCovariates.CARRY_LAST
    with pytest.raises(MapiError) as exc:
        forecast_request(MODEL, periods=4, future=FutureCovariates.SUPPLIED)
    assert exc.value.code is ErrorCode.MISSING_FUTURE_COVARIATE
    supplied = forecast_request(
        MODEL, periods=4, future=FutureCovariates.SUPPLIED, scenario="dataset://growth/plan@v1"
    )
    assert supplied.future_covariates is None  # the default stays off the wire
    assert "future_covariates" not in supplied.model_dump(exclude_none=True)


@pytest.mark.parametrize("example", GuidedCompile.model_json_schema()["examples"])
def test_compile_endpoint_body(example: dict[str, Any]) -> None:
    body = GuidedCompile.model_validate(example)
    req = compile_guided_request(body.root)
    assert isinstance(req, ScoreRequest)
    assert req.mode == {"whatif": "counterfactual", "forecast": "forecast",
                        "counterfactual": "counterfactual"}[example["kind"]]  # fmt: skip
    plain = compile_guided_request(
        GuidedCompile.model_validate({"kind": "forecast", "model": MODEL, "periods": 8}).root
    )
    assert plain.interventions == []


def test_future_covariate_policy_relaxes_the_static_check(catalog: Catalog) -> None:
    from mapi_spec.coords import DataSummary
    from mapi_spec.examples import E33_RAW, E33_YAML
    from mapi_spec.loader import load_model_spec
    from mapi_spec.schema import DataSchema
    from mapi_spec.validate import ScoreMetadata, validate_score_request, validate_spec

    rs = validate_spec(load_model_spec(E33_YAML), catalog, DataSchema.model_validate(E33_RAW))
    assert rs.resolved is not None
    weekly = {"kind": "time", "size": 104, "start": "2024-07-07", "end": "2026-06-28",
              "freq": "W-SUN"}  # fmt: skip
    meta = ScoreMetadata.model_validate(
        {"resolved": rs.resolved.model_dump(mode="json"),
         "fit_coords": {s: {"time": weekly} for s in rs.resolved.stages}}
    )  # fmt: skip
    base = {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": MODEL, "mode": "forecast",
            "horizon": {"dim": "time", "periods": 8}, "inputs_stage": "scale_spend"}  # fmt: skip
    inputs = DataSummary.model_validate({"vars": [], "coords": {}})
    strict = validate_score_request(ScoreRequest.model_validate(base), meta, inputs)
    assert ErrorCode.MISSING_FUTURE_COVARIATE in {i.code for i in strict}
    relaxed = ScoreRequest.model_validate({**base, "future_covariates": "carry_last"})
    assert ErrorCode.MISSING_FUTURE_COVARIATE not in {
        i.code for i in validate_score_request(relaxed, meta, inputs)
    }
