"""v0.13 ``model.lift_test``: a causal model's ``impact`` group as an MMM lift test."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_spec.calibration import Calibration, LiftTest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_workflows.payloads import TablePayload
from mapi_workflows.runtime.causal import lift_test_from_impact
from mapi_workflows.runtime.nodes import builtin_nodes
from mapi_workflows.runtime.values import ModelValue
from mapi_workflows.specs import model_spec_document
from wf_ctx import run_node

S, N, START = 400, 12, 8


def _impact(
    units: list[str] | None = None,
    *,
    lift: float = 5.0,
    treated: list[str] | None = None,
    dated: bool = True,
    seed: int = 0,
    chain_draw: bool = False,
) -> xr.Dataset:
    """An I-4 ``impact`` group: ``lift`` per post cell of every treated unit (noise sd 1)."""
    rng = np.random.default_rng(seed)
    times: Any = pd.date_range("2026-01-05", periods=N, freq="W-MON") if dated else np.arange(N)
    post_t = np.arange(N) >= START
    if units is None:
        cell = (N,)
        post = post_t
        dims: tuple[str, ...] = ("time",)
        coords: dict[str, Any] = {"time": times}
    else:
        cell = (len(units), N)
        on = np.array([u in (treated or units) for u in units])
        post = on[:, None] & post_t[None, :]
        dims = ("unit", "time")
        coords = {"unit": units, "time": times}
    actual = 100.0 + lift * post
    cf = 100.0 + rng.normal(0.0, 1.0, size=(S, *cell))
    effect = actual[None] - cf
    cum = np.where(post[None], np.cumsum(np.where(post[None], effect, 0.0), axis=-1), np.nan)
    ds = xr.Dataset(
        {
            "actual": (dims, actual),
            "counterfactual": (("sample", *dims), cf),
            "effect": (("sample", *dims), effect),
            "cumulative_effect": (("sample", *dims), cum),
            "post": (dims, post),
        },
        coords=coords,
        attrs={
            "intervention_start": "2026-03-02",
            "intervention_end": "",
            "model": "causal.synthetic_control@1.0.0",
            "units": "",
            "effect_kind": "predictive",
        },
    )
    if chain_draw:
        idx = pd.MultiIndex.from_product([range(2), range(S // 2)], names=["chain", "draw"])
        ds = ds.assign_coords(xr.Coordinates.from_pandas_multiindex(idx, "sample"))
        ds = ds.unstack("sample")
    return ds


def test_one_treated_unit_is_a_geo_test() -> None:
    impact = _impact(["geo_01", "geo_02", "geo_03"], treated=["geo_03"])
    out = lift_test_from_impact(
        impact, channel="tv", spend_delta=20_000.0, test_id="tv-geo-03", hdi_prob=0.9
    )
    n_post = N - START
    assert out["geo"] == "geo_03" and out["n_treated"] == 1
    assert out["start"] == "2026-03-02" and out["end"] == "2026-03-23"
    assert out["n_cells"] == n_post and out["n_missing"] == 0
    kpi = out["kpi_delta"]
    assert isinstance(kpi, float)
    assert kpi == pytest.approx(5.0 * n_post, abs=1.0)
    assert out["se"] == pytest.approx(np.sqrt(n_post), rel=0.15)
    assert out["excludes_zero"] is True and out["p_positive"] == 1.0
    assert out["relative_effect"] == pytest.approx(5.0 * n_post / (100.0 * n_post), rel=0.05)
    lift = LiftTest.model_validate(out["lift_test"])
    assert lift.id == "tv-geo-03" and lift.channel == "tv" and lift.geo == "geo_03"
    assert lift.ci is not None and lift.ci_level == 0.9 and lift.se is None
    assert lift.ci[0] < lift.kpi_delta < lift.ci[1]
    # it is a valid calibration block of an MMM spec
    Calibration.model_validate({"lift_tests": [out["lift_test"]]})


def test_two_treated_units_are_national() -> None:
    out = lift_test_from_impact(
        _impact(["a", "b", "c"], treated=["a", "b"]),
        channel="search",
        spend_delta=1000.0,
        test_id=None,
        hdi_prob=0.8,
    )
    assert out["geo"] is None and out["n_treated"] == 2
    assert out["kpi_delta"] == pytest.approx(2 * 5.0 * (N - START), abs=1.5)
    lift = out["lift_test"]
    assert isinstance(lift, dict) and "geo" not in lift and "id" not in lift
    assert lift["ci_level"] == 0.8


def test_a_single_series_model_has_no_geo() -> None:
    out = lift_test_from_impact(
        _impact(None), channel="tv", spend_delta=10.0, test_id=None, hdi_prob=0.9
    )
    assert out["geo"] is None and out["n_treated"] == 1


def test_a_holdout_keeps_its_signs() -> None:
    out = lift_test_from_impact(
        _impact(["a", "b"], treated=["a"], lift=-4.0),
        channel="tv",
        spend_delta=-50_000.0,
        test_id=None,
        hdi_prob=0.9,
    )
    lift = LiftTest.model_validate(out["lift_test"])
    assert lift.spend_delta == -50_000.0 and lift.kpi_delta < 0
    assert out["p_positive"] == 0.0 and out["excludes_zero"] is True
    assert lift.implied_roi > 0


def test_chain_draw_groups_are_stacked() -> None:
    out = lift_test_from_impact(
        _impact(["a", "b"], treated=["a"], chain_draw=True),
        channel="tv",
        spend_delta=1.0,
        test_id=None,
        hdi_prob=0.9,
    )
    assert out["kpi_delta"] == pytest.approx(5.0 * (N - START), abs=1.0)


def test_missing_actuals_are_left_out_and_counted() -> None:
    impact = _impact(["a", "b"], treated=["a"])
    impact["effect"][:, 0, START] = np.nan
    out = lift_test_from_impact(impact, channel="tv", spend_delta=1.0, test_id=None, hdi_prob=0.9)
    assert out["n_missing"] == 1
    assert out["kpi_delta"] == pytest.approx(5.0 * (N - START - 1), abs=1.0)


def test_period_numbers_are_refused() -> None:
    with pytest.raises(MapiError, match="dated periods") as exc:
        lift_test_from_impact(
            _impact(["a", "b"], treated=["a"], dated=False),
            channel="tv",
            spend_delta=1.0,
            test_id=None,
            hdi_prob=0.9,
        )
    assert exc.value.code is ErrorCode.SCHEMA_INVALID


def test_no_post_cells_is_a_data_error() -> None:
    impact = _impact(["a", "b"], treated=["a"])
    impact["post"][:] = False
    with pytest.raises(MapiError, match="no periods in the test window"):
        lift_test_from_impact(impact, channel="tv", spend_delta=1.0, test_id=None, hdi_prob=0.9)


def _model(groups: dict[str, xr.Dataset]) -> ModelValue:
    bundle: Any = SimpleNamespace(model=SimpleNamespace(groups=groups))
    return ModelValue(bundle=bundle, model="causal.synthetic_control@1.0.0")


def test_the_node_gives_a_lift_test_and_a_table() -> None:
    assert "model.lift_test" in builtin_nodes()
    mv = _model({"impact": _impact(["a", "b", "c"], treated=["b", "c"])})
    out = run_node("model.lift_test", {"channel": "tv", "spend_delta": 500.0}, model=mv)
    assert out.result["geo"] is None and out.result["hdi_prob"] == 0.9
    assert any("national" in w for w in out.warnings_plain)
    table = TablePayload.model_validate(out.outputs["table"])
    assert table.kind == "lift_test" and table.rows[0]["channel"] == "tv"
    assert table.meta["lift_test"] == out.result["lift_test"]


def test_the_node_needs_a_causal_model() -> None:
    mv = _model({"posterior": xr.Dataset()})
    with pytest.raises(MapiError, match="needs a causal model") as exc:
        run_node("model.lift_test", {"channel": "tv", "spend_delta": 1.0}, model=mv)
    assert exc.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_model_fit_passes_the_calibration_through() -> None:
    lift = {"channel": "tv", "start": "2026-03-02", "end": "2026-04-06",
            "spend_delta": 1.0, "kpi_delta": 2.0, "se": 0.5}  # fmt: skip
    params: dict[str, Any] = {"model": "mmm.bayesian@^1", "calibration": {"lift_tests": [lift]}}
    doc = model_spec_document(params, name="m", project="p", dataset="dataset://p/d@v1")
    assert doc["calibration"] == {"lift_tests": [lift]}
    params["calibration"] = {}
    doc = model_spec_document(params, name="m", project="p", dataset="dataset://p/d@v1")
    assert "calibration" not in doc


@pytest.mark.slow
def test_geo_lift_mmm_template_calibrates_the_mmm_with_the_test() -> None:
    """The gallery's geo-lift workflow in-process: causal.synthetic_control on a simulated
    test in one market (tiny sampler settings), model.lift_test, the impact plot, then
    mmm.bayesian calibrated with that lift test (its bundle keeps a ``lift_tests`` group)."""
    import copy
    import json

    from mapi_conformance.runner import load_fixture
    from mapi_plugin_causal.datasets_panel import geo_lift_dataset
    from mapi_sdk.registry import default_registry
    from mapi_spec.workflow import WorkflowRunStatus, WorkflowSpec
    from mapi_spec.workflow_templates import GEO_LIFT_MMM
    from mapi_workflows.runtime.local import LocalRunner

    fx = load_fixture("guided_mmm")
    data, _tables = fx.generate(0, "tiny")
    data = data.copy()
    data.attrs["mapi_schema"] = fx.raw_schema(data).model_dump_json(exclude_none=True)
    # the experiment on the MMM's weeks and markets: the test is in "north", weeks 30..35
    exp, truth = geo_lift_dataset(geos=6, weeks=data.sizes["week"], start_week=30,
                                  test_weeks=6, lift=0.1)  # fmt: skip
    markets = ["east", "north", "south", "west", "central", "coast"]
    exp = exp.assign_coords(geo=markets, week=data["week"].values)
    schema = json.loads(exp.attrs["mapi_schema"])
    schema["dims"]["week"]["freq"] = "W-SUN"
    exp.attrs["mapi_schema"] = json.dumps(schema)
    weeks = data["week"].values
    start, end = (str(np.datetime_as_string(weeks[i], "D")) for i in (30, 35))

    doc = copy.deepcopy(GEO_LIFT_MMM.document)
    nodes = cast(list[dict[str, Any]], doc["nodes"])
    for n in nodes:
        if n["id"] in ("geolift", "mmm"):
            # only the sampler size: the rest of the template's fit block (geolift's
            # mode: train_only) is what the gallery runs
            n["with"]["fit"] = {
                **(n["with"].get("fit") or {}),
                **{"engine": "pymc", "tune": 60, "draws": 40, "chains": 2, "seed": 1},
            }
    geolift = next(n for n in nodes if n["id"] == "geolift")
    assert geolift["with"]["fit"]["mode"] == "train_only"
    spec = WorkflowSpec.model_validate(doc)
    res = LocalRunner(registry=default_registry()).run(
        spec,
        inputs={"experiment": exp, "data": data},
        params={
            "treated": ["north"],
            "test_start": start,
            "test_end": end,
            "channel": "tv",
            "spend_delta": 5000.0,
        },
    )
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    lift = res.node("lift").result
    assert lift is not None and lift["geo"] == "north"
    assert lift["start"] == start and lift["end"] == end
    LiftTest.model_validate(lift["lift_test"])
    assert truth.lift > 0
    test_fit = res.node_values["geolift"]["model"]
    assert isinstance(test_fit, ModelValue)
    assert test_fit.bundle.model.fit_config.mode == "train_only"  # the template's mode
    assert "impact" in test_fit.bundle.model.groups
    mmm = res.values["model"]
    assert isinstance(mmm, ModelValue)
    assert mmm.bundle.model.manifest.id == "mmm.bayesian"
    assert "lift_tests" in mmm.bundle.model.groups
    assert res.values["report"]["format"] == "mapi.report/v1"  # type: ignore[index]
