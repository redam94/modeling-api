"""Static CoordPlan checks for ScoreRequests (validate_score_request)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from mapi_spec import Catalog, DataSchema, ModelSpec, Representation, ScoreRequest
from mapi_spec.coords import DataSummary
from mapi_spec.errors import ErrorCode, Issue, Severity, http_status
from mapi_spec.examples import E33_RAW, E33_YAML, SCORE_YAML, XGB_RAW, XGB_YAML
from mapi_spec.loader import load_score_request, parse_yaml
from mapi_spec.schema import DimPolicy
from mapi_spec.validate import ResolvedSpec, ScoreMetadata, validate_score_request, validate_spec

pytestmark = pytest.mark.unit

GEOS = ["DE", "FR", "IT"]
CHANNELS = ["meta", "search", "tv"]


def _cat(values: list[str]) -> dict[str, Any]:
    return {"kind": "categorical", "size": len(values), "values": values}


def _time(start: str, end: str, size: int, freq: str = "W-SUN", **kw: Any) -> dict[str, Any]:
    return {"kind": "time", "size": size, "start": start, "end": end, "freq": freq, **kw}


WEEKLY_FIT = {
    "geo": _cat(GEOS),
    "channel": _cat(CHANNELS),
    "time": _time("2024-01-07", "2026-06-28", 130),
}
DAILY_FIT = {
    "geo": _cat(GEOS),
    "channel": _cat(CHANNELS),
    "time": _time("2024-01-01", "2026-06-30", 912, "D"),
}


def _resolve(catalog: Catalog, data: dict[str, Any], raw: dict[str, Any]) -> ResolvedSpec:
    res = validate_spec(ModelSpec.model_validate(data), catalog, DataSchema.model_validate(raw))
    return res.raise_for_errors()


def _e33(**changes: Any) -> dict[str, Any]:
    d = parse_yaml(E33_YAML)
    assert isinstance(d, dict)
    for k, v in changes.items():
        d[k] = v
    return d


@pytest.fixture
def e33_meta(catalog: Catalog) -> ScoreMetadata:
    rs = _resolve(catalog, _e33(), E33_RAW)
    stages = {s: WEEKLY_FIT for s in rs.stages if s not in ("raw", "to_eur")}
    stages |= {"raw": DAILY_FIT, "to_eur": DAILY_FIT}
    return ScoreMetadata.model_validate(
        {"resolved": rs.model_dump(mode="json"), "fit_coords": stages}
    )


def _plan_inputs(**over: Any) -> DataSummary:
    coords = {
        "geo": _cat(GEOS),
        "channel": _cat(CHANNELS),
        "time": _time("2026-07-05", "2026-12-27", 26),
    }
    coords.update(over.pop("coords", {}))
    return DataSummary.model_validate({"vars": over.pop("vars", ["spend"]), "coords": coords})


def _req(**kw: Any) -> ScoreRequest:
    base = parse_yaml(SCORE_YAML)
    assert isinstance(base, dict)
    for k, v in kw.items():
        if v is None:
            base.pop(k, None)
        else:
            base[k] = v
    return ScoreRequest.model_validate(base)


def _errors(issues: list[Issue]) -> dict[str, Issue]:
    return {f"{i.code.value}@{i.pointer}": i for i in issues if i.severity is Severity.ERROR}


# ----------------------------------------------------------------------------- happy path


def test_plan_request_from_the_architecture_doc_is_valid(e33_meta: ScoreMetadata) -> None:
    req = load_score_request(SCORE_YAML)
    issues = validate_score_request(req, e33_meta, _plan_inputs())
    assert issues == []


def test_metadata_is_json(e33_meta: ScoreMetadata) -> None:
    assert ScoreMetadata.model_validate_json(e33_meta.model_dump_json()) == e33_meta


# ---------------------------------------------------------------------- mode / stage / tables


def test_mode_not_supported(e33_meta: ScoreMetadata) -> None:
    issues = validate_score_request(_req(mode="simulate"), e33_meta, _plan_inputs())
    err = _errors(issues)["MODE_NOT_SUPPORTED@/mode"]
    assert err.context["supported"] == ["counterfactual", "evaluate", "forecast", "predict"]


def test_unknown_inputs_stage(e33_meta: ScoreMetadata) -> None:
    issues = validate_score_request(_req(inputs_stage="nowhere"), e33_meta, _plan_inputs())
    assert "REF_UNRESOLVED@/inputs_stage" in _errors(issues)


def test_supplied_side_table_required_at_raw_stage(e33_meta: ScoreMetadata) -> None:
    raw_inputs = DataSummary.model_validate(
        {
            "vars": ["spend_local"],
            "coords": {
                "market": _cat(GEOS),
                "channel": _cat(CHANNELS),
                "date": _time("2026-07-01", "2026-12-31", 184, "D"),
            },
        }
    )
    issues = validate_score_request(_req(inputs_stage="raw"), e33_meta, raw_inputs)
    errs = _errors(issues)
    assert list(errs) == ["SIDE_TABLE_COVERAGE@/side_tables/fx"]
    ok = _req(inputs_stage="raw", side_tables={"fx": "dataset://growth/fx_plan@v1"})
    assert validate_score_request(ok, e33_meta, raw_inputs) == []
    bad = _req(side_tables={"rates": "dataset://growth/fx_plan@v1"})
    assert "REF_UNRESOLVED@/side_tables/rates" in _errors(
        validate_score_request(bad, e33_meta, _plan_inputs())
    )


def test_pinned_side_table_cannot_be_supplied(e33_meta: ScoreMetadata) -> None:
    """``pinned`` replays the fit-time version; a request may not swap it (replay contract)."""
    req = _req(side_tables={"holidays": "dataset://shared/holidays_other@v9"})
    errs = _errors(validate_score_request(req, e33_meta, _plan_inputs()))
    assert list(errs) == ["SIDE_TABLE_COVERAGE@/side_tables/holidays"]


# --------------------------------------------------------------------------------- horizon


def test_horizon_on_non_extend_or_unknown_dim(e33_meta: ScoreMetadata) -> None:
    issues = validate_score_request(_req(horizon={"dim": "geo", "periods": 4}), e33_meta, None)
    assert "COORD_NOT_ALLOWED@/horizon/dim" in _errors(issues)
    issues = validate_score_request(_req(horizon={"dim": "week", "periods": 4}), e33_meta, None)
    assert "is not a dim" in _errors(issues)["COORD_NOT_ALLOWED@/horizon/dim"].message


def test_horizon_exceeds_fit_409(catalog: Catalog) -> None:
    raw = {
        "dims": {"t": {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
        "vars": {"co2": {"dims": ["t"], "dtype": "float64", "role": "target"}},
    }
    spec = {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {"name": "mauna-loa", "project": "climate"},
        "data": {
            "dataset": "dataset://climate/co2@v1",
            "bind": {"dims": {"time": "t"}, "vars": {"y": "co2"}},
        },
        "model": {"use": "ts.hsgp@^1", "fit": {"mode": "train_only"}},
    }
    rs = _resolve(catalog, spec, raw)
    assert rs.model.input_schema is not None
    assert rs.model.input_schema.dims["time"].max_extension == 52
    fit = {"time": _time("2020-01-05", "2026-06-28", 339)}
    meta = ScoreMetadata(resolved=rs, fit_coords={"raw": fit})  # type: ignore[arg-type]
    req = ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "model://climate/mauna-loa@v1",
            "mode": "forecast",
            "horizon": {"dim": "time", "periods": 60},
        }
    )
    issues = validate_score_request(req, meta)
    err = _errors(issues)["HORIZON_EXCEEDS_FIT@/horizon/periods"]
    assert http_status(err.code) == 409
    # input data reaching past max_extension is caught from coordinates too
    long_input = DataSummary.model_validate(
        {"vars": [], "coords": {"t": _time("2026-07-05", "2027-12-26", 78)}}
    )
    req = req.model_copy(update={"horizon": None, "mode": "evaluate", "data": req.scenario})
    issues = validate_score_request(
        ScoreRequest.model_validate(
            {**req.model_dump(by_alias=True, mode="json"), "data": "dataset://climate/x"}
        ),
        meta,
        long_input,
    )
    assert "HORIZON_EXCEEDS_FIT@/data" in _errors(issues)


def test_recursive_forecast_rejected_at_score_time(catalog: Catalog) -> None:
    d = _e33()
    d["pipeline"].insert(
        4,
        {
            "id": "ylag",
            "use": "core.lag@^1",
            "with": {"vars": ["y"], "along": "time", "within": ["geo"], "lags": [8]},
        },
    )
    rs = _resolve(catalog, d, E33_RAW)
    meta = ScoreMetadata(resolved=rs, fit_coords={"scale_spend": WEEKLY_FIT})  # type: ignore[arg-type]
    issues = validate_score_request(_req(), meta, _plan_inputs())
    err = _errors(issues)["RECURSIVE_UNSUPPORTED@/horizon/periods"]
    assert "lags the target by 8" in err.message
    ok = _req(horizon={"dim": "time", "periods": 8})
    assert "RECURSIVE_UNSUPPORTED@/horizon/periods" not in _errors(
        validate_score_request(ok, meta, _plan_inputs())
    )
    # entering after the lag step means the caller supplies the lagged values
    late = _req(inputs_stage="ylag")
    assert "RECURSIVE_UNSUPPORTED@/horizon/periods" not in _errors(
        validate_score_request(late, meta, None)
    )


def test_recursion_checked_from_the_payload_without_a_horizon(catalog: Catalog) -> None:
    """A predict request whose data runs past the fit is a forecast in all but name."""
    d = _e33()
    d["pipeline"].insert(
        4,
        {
            "id": "ylag",
            "use": "core.lag@^1",
            "with": {"vars": ["y"], "along": "time", "within": ["geo"], "lags": [1]},
        },
    )
    d["data"]["split"]["last"] = 1
    rs = _resolve(catalog, d, E33_RAW)
    meta = ScoreMetadata(resolved=rs, fit_coords={"scale_spend": WEEKLY_FIT})  # type: ignore[arg-type]
    predict = _req(mode="predict", horizon=None, interventions=None)
    six = _plan_inputs(coords={"time": _time("2026-07-05", "2026-08-09", 6)})
    err = _errors(validate_score_request(predict, meta, six))["RECURSIVE_UNSUPPORTED@/scenario"]
    assert "lags the target by 1" in err.message and "6" in err.message
    one = _plan_inputs(coords={"time": _time("2026-07-05", "2026-07-05", 1)})
    in_range = _plan_inputs(coords={"time": _time("2026-05-03", "2026-06-28", 9)})
    for ok in (one, in_range):
        assert not any(
            k.startswith("RECURSIVE_UNSUPPORTED")
            for k in _errors(validate_score_request(predict, meta, ok))
        )


def test_future_covariates_checked_from_the_payload_without_a_horizon(
    e33_meta: ScoreMetadata,
) -> None:
    predict = _req(mode="predict", horizon=None, interventions=None)
    future = {"time": _time("2026-07-05", "2026-08-09", 6)}
    issues = validate_score_request(
        predict, e33_meta, _plan_inputs(vars=["holiday"], coords=future)
    )
    assert "spend" in _errors(issues)["MISSING_FUTURE_COVARIATE@/scenario"].message
    past = {"time": _time("2026-05-03", "2026-06-28", 9)}
    in_sample = _plan_inputs(vars=["holiday"], coords=past)
    assert "MISSING_FUTURE_COVARIATE@/scenario" not in _errors(
        validate_score_request(predict, e33_meta, in_sample)
    )


# ------------------------------------------------------------------------ future covariates


def test_missing_future_covariate(e33_meta: ScoreMetadata) -> None:
    issues = validate_score_request(_req(), e33_meta, _plan_inputs(vars=["holiday"]))
    err = _errors(issues)["MISSING_FUTURE_COVARIATE@/scenario"]
    assert "spend" in err.message
    issues = validate_score_request(_req(scenario=None), e33_meta, None)
    assert "MISSING_FUTURE_COVARIATE@/data" in _errors(issues)


def test_future_covariate_coverage(e33_meta: ScoreMetadata) -> None:
    short = _plan_inputs(coords={"time": _time("2026-07-05", "2026-11-29", 22)})
    err = _errors(validate_score_request(_req(), e33_meta, short))[
        "MISSING_FUTURE_COVARIATE@/scenario"
    ]
    assert "cover 22 of the 26" in err.message


# ----------------------------------------------------------------------------- coordinates


def test_non_contiguous_extension(e33_meta: ScoreMetadata) -> None:
    gap = _plan_inputs(coords={"time": _time("2026-07-12", "2027-01-03", 26)})
    errs = _errors(validate_score_request(_req(), e33_meta, gap))
    assert "gap of 1 period" in errs["NON_CONTIGUOUS_EXTENSION@/scenario"].message
    holes = _plan_inputs(coords={"time": _time("2026-07-05", "2026-12-27", 20, contiguous=False)})
    assert "NON_CONTIGUOUS_EXTENSION@/scenario" in _errors(
        validate_score_request(_req(), e33_meta, holes)
    )
    misaligned = _plan_inputs(coords={"time": _time("2026-07-06", "2026-12-28", 26)})
    errs = _errors(validate_score_request(_req(), e33_meta, misaligned))
    assert "not aligned" in errs["NON_CONTIGUOUS_EXTENSION@/scenario"].message


def test_coords_before_fit_start(e33_meta: ScoreMetadata) -> None:
    early = _plan_inputs(coords={"time": _time("2023-12-31", "2026-12-27", 157)})
    errs = _errors(validate_score_request(_req(), e33_meta, early))
    assert "COORD_NOT_ALLOWED@/scenario" in errs


def test_forecast_not_past_fit_warns(e33_meta: ScoreMetadata) -> None:
    inside = _plan_inputs(coords={"time": _time("2026-01-04", "2026-06-28", 26)})
    issues = validate_score_request(_req(), e33_meta, inside)
    warn = [i for i in issues if i.code is ErrorCode.NON_CONTIGUOUS_EXTENSION]
    assert warn and warn[0].severity is Severity.WARNING


def test_unseen_levels_on_fixed_dim(e33_meta: ScoreMetadata) -> None:
    new_geo = _plan_inputs(coords={"geo": _cat([*GEOS, "ES"])})
    err = _errors(validate_score_request(_req(), e33_meta, new_geo))["UNSEEN_LEVELS@/scenario"]
    assert "policy is 'fixed'" in err.message
    assert err.untrusted_text == "ES"  # user values live in untrusted_text only
    assert "ES" not in err.message


def test_new_levels_allowed_with_strategy(catalog: Catalog) -> None:
    d = _e33()
    del d["model"]["settings"]["dim_policies"]
    rs = _resolve(catalog, d, E33_RAW)
    meta = ScoreMetadata(resolved=rs, fit_coords={"scale_spend": WEEKLY_FIT, "hols": WEEKLY_FIT})  # type: ignore[arg-type]
    new_geo = _plan_inputs(coords={"geo": _cat([*GEOS, "ES"])})
    issues = validate_score_request(_req(), meta, new_geo)
    assert _errors(issues) == {}
    info = [i for i in issues if i.code is ErrorCode.UNSEEN_LEVELS]
    assert info[0].severity is Severity.INFO and "hyperprior" in info[0].message
    sel = validate_score_request(_req(select={"geo": ["ES"]}), meta, new_geo)
    assert _errors(sel) == {}


def test_new_levels_with_error_strategy(catalog: Catalog) -> None:
    raw = copy.deepcopy(E33_RAW)
    d = _e33()
    del d["model"]["settings"]["dim_policies"]
    data = catalog.model_dump(mode="json")
    for m in data["models"]:
        tpl = m["manifest"]["schema_template"]["template"]
        if "geo" in tpl.get("dims", {}):
            tpl["dims"]["geo"]["new_level_strategy"] = "error"
    rs = _resolve(Catalog.model_validate(data), d, raw)
    meta = ScoreMetadata(resolved=rs, fit_coords={"scale_spend": WEEKLY_FIT})  # type: ignore[arg-type]
    new_geo = _plan_inputs(coords={"geo": _cat([*GEOS, "ES"])})
    err = _errors(validate_score_request(_req(), meta, new_geo))["UNSEEN_LEVELS@/scenario"]
    assert "strategy 'error'" in err.message


def test_ordered_fixed_dim_by_range(e33_meta: ScoreMetadata) -> None:
    rs = e33_meta.resolved
    stage = rs.stages["scale_spend"]
    assert stage is not None
    # Make 'time' fixed at this stage to exercise range-based unseen detection
    fixed_time = stage.model_copy(
        update={
            "dims": {
                **stage.dims,
                "time": stage.dims["time"].model_copy(
                    update={"policy": DimPolicy.FIXED, "max_extension": None}
                ),
            }
        }
    )
    rs2 = rs.model_copy(update={"stages": {**rs.stages, "scale_spend": fixed_time}})
    model = rs2.model
    assert model.input_schema is not None
    mi = model.input_schema
    fixed_model = mi.model_copy(
        update={
            "dims": {
                **mi.dims,
                "time": mi.dims["time"].model_copy(update={"policy": DimPolicy.FIXED}),
            }
        }
    )
    rs2 = rs2.model_copy(update={"model": model.model_copy(update={"input_schema": fixed_model})})
    meta = ScoreMetadata(resolved=rs2, fit_coords=e33_meta.fit_coords)
    inside = _plan_inputs(coords={"time": _time("2025-01-05", "2025-06-29", 26)})
    req = _req(mode="predict", horizon=None)
    assert _errors(validate_score_request(req, meta, inside)) == {}
    outside = _plan_inputs(coords={"time": _time("2026-07-05", "2026-12-27", 26)})
    assert "UNSEEN_LEVELS@/scenario" in _errors(validate_score_request(req, meta, outside))


def test_unknown_input_dim(e33_meta: ScoreMetadata) -> None:
    extra = _plan_inputs(coords={"store": _cat(["s1"])})
    assert "COORD_NOT_ALLOWED@/scenario" in _errors(validate_score_request(_req(), e33_meta, extra))


def test_select_checks(e33_meta: ScoreMetadata) -> None:
    errs = _errors(
        validate_score_request(_req(select={"market": ["ES"]}), e33_meta, _plan_inputs())
    )
    assert "UNSEEN_LEVELS@/select/market" in errs  # dataset name accepted, mapped to geo
    errs = _errors(validate_score_request(_req(select={"store": ["x"]}), e33_meta, _plan_inputs()))
    assert "COORD_NOT_ALLOWED@/select/store" in errs
    ok = validate_score_request(
        _req(select={"geo": ["DE"], "time": ["2026-07-05"]}), e33_meta, _plan_inputs()
    )
    assert _errors(ok) == {}


# --------------------------------------------------------------------------- interventions


def test_intervention_checks(e33_meta: ScoreMetadata) -> None:
    ivs = [
        {"var": "budget", "multiply": 2},
        {"var": "y", "add": 1},
        {"var": "spend", "sel": {"store": "x"}, "multiply": 1.1},
        {"var": "spend", "sel": {"channel": ["meta", "radio"]}, "multiply": 1.1},
    ]
    errs = _errors(validate_score_request(_req(interventions=ivs), e33_meta, _plan_inputs()))
    assert "SCHEMA_INVALID@/interventions/0/var" in errs
    assert "target" in errs["SCHEMA_INVALID@/interventions/1/var"].message
    assert "COORD_NOT_ALLOWED@/interventions/2/sel/store" in errs
    assert errs["UNSEEN_LEVELS@/interventions/3/sel/channel"].untrusted_text == "radio"


# --------------------------------------------------------------------------- output selector


@pytest.fixture
def xgb_meta(catalog: Catalog) -> ScoreMetadata:
    d = parse_yaml(XGB_YAML)
    assert isinstance(d, dict)
    rs = _resolve(catalog, d, XGB_RAW)
    fit = {
        "store": _cat(["s1", "s2"]),
        "sku": _cat(["a"]),
        "time": _time("2024-01-07", "2026-06-28", 130),
    }
    return ScoreMetadata(resolved=rs, fit_coords={"logy": fit})  # type: ignore[arg-type]


def _xgb_req(**output: Any) -> ScoreRequest:
    return ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "endpoint://retail/demand",
            "output": output,
        }
    )


def test_quantile_output_rules(xgb_meta: ScoreMetadata) -> None:
    assert validate_score_request(_xgb_req(), xgb_meta) == []  # default summary adapts
    errs = _errors(validate_score_request(_xgb_req(draws=10, summary=["q10", "hdi_90"]), xgb_meta))
    assert set(errs) == {
        "CAPABILITY_UNSUPPORTED@/output/draws",
        "CAPABILITY_UNSUPPORTED@/output/summary/1",
    }
    errs = _errors(validate_score_request(_xgb_req(aggregate={"over": ["store"]}), xgb_meta))
    assert (
        "cannot aggregate 'quantiles'" in errs["CAPABILITY_UNSUPPORTED@/output/aggregate"].message
    )
    errs = _errors(validate_score_request(_xgb_req(aggregate={"over": ["region"]}), xgb_meta))
    assert "COORD_NOT_ALLOWED@/output/aggregate/over/0" in errs
    errs = _errors(validate_score_request(_xgb_req(vars=["y_hat", "roi"]), xgb_meta))
    assert list(errs) == ["REF_UNRESOLVED@/output/vars/1"]


def test_point_output_rules(catalog: Catalog, xgb_meta: ScoreMetadata) -> None:
    caps = xgb_meta.resolved.model.capabilities.model_copy(
        update={"representation": Representation.POINT}
    )
    model = xgb_meta.resolved.model.model_copy(update={"capabilities": caps})
    meta = xgb_meta.model_copy(
        update={"resolved": xgb_meta.resolved.model_copy(update={"model": model})}
    )
    errs = _errors(validate_score_request(_xgb_req(kind="predictive"), meta))
    assert "CAPABILITY_UNSUPPORTED@/output/kind" in errs
    issues = validate_score_request(_xgb_req(aggregate={"over": ["store"], "how": "sum"}), meta)
    assert _errors(issues) == {}
    assert issues[0].severity is Severity.WARNING


def test_draws_and_quota(e33_meta: ScoreMetadata) -> None:
    out = {"kind": "expected", "draws": 500}
    errs = _errors(
        validate_score_request(_req(output=out), e33_meta, _plan_inputs(), max_draws=300)
    )
    assert "QUOTA_EXCEEDED@/output/draws" in errs
    agg = {"aggregate": {"over": ["geo", "channel"], "how": "sum"}, "summary": ["mean", "hdi_90"]}
    assert validate_score_request(_req(output=agg), e33_meta, _plan_inputs()) == []
