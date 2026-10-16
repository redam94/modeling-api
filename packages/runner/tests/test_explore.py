"""The Data Explorer and Model Explorer (M3.5): every section validates against its
``mapi_spec.views`` contract, carries sensible numbers, plots through the platform
sanitizer, and an explore run writes the index with skipped sections explained."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from gov_fixtures import SCHEMA, FakeCtx, fit, linear_data, linear_spec
from mapi_plots.explorer import EXPLORER_RECIPES, explorer_plots
from mapi_plots.sanitize import sanitize_plot
from mapi_runner.worker.explore import write_data_explorer, write_sections
from mapi_runner.worker.explore_data import (
    CHEAP_SECTIONS,
    DATA_SECTION_FUNCS,
    SectionSkipped,
    data_context,
    explore_period,
    jsonable,
)
from mapi_runner.worker.explore_model import FIT_SECTIONS, MODEL_SECTION_FUNCS, ModelContext
from mapi_runner.worker.refit import Refitter
from mapi_sdk.registry import default_registry
from mapi_spec.explore import DataExploreRequest, ModelExploreRequest
from mapi_spec.schema import DataSchema
from mapi_spec.views import (
    DATA_SECTIONS,
    EXPLORE_INDEX,
    MODEL_SECTIONS,
    CorrelationView,
    ExplorerIndex,
    FevdView,
    IrfView,
    ResponseCurvesView,
)

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore::FutureWarning")]


def _panel(seed: int = 0) -> tuple[xr.Dataset, DataSchema]:
    rng = np.random.default_rng(seed)
    n = 156
    t = pd.date_range("2022-01-02", periods=n, freq="W-SUN")
    geos, channels = ["DE", "FR", "IT"], ["tv", "search"]
    spend = rng.gamma(2.0, 50.0, size=(n, 3, 2))
    season = 10 * np.sin(2 * np.pi * np.arange(n) / 52)
    y = 100 + season[:, None] + spend.sum(axis=2) * 0.3 + rng.normal(scale=5, size=(n, 3))
    price = 10 + rng.normal(size=(n, 3))
    price[5:12, 0] = np.nan
    ds = xr.Dataset(
        {
            "sales": (("week", "geo"), y),
            "spend": (("week", "geo", "channel"), spend),
            "price": (("week", "geo"), price),
        },
        coords={"week": t, "geo": geos, "channel": channels},
    )
    schema = DataSchema.model_validate(
        {
            "dims": {
                "week": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "geo": {"kind": "categorical"},
                "channel": {"kind": "categorical"},
            },
            "vars": {
                "sales": {"dims": ["week", "geo"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["week", "geo", "channel"], "dtype": "float64", "role": "spend"},
                "price": {
                    "dims": ["week", "geo"],
                    "dtype": "float64",
                    "role": "control",
                    "missing": "allow",
                },
            },
        }
    )
    return ds, schema


def _sanitized(view: dict[str, Any]) -> int:
    plots = explorer_plots(view)
    for p in plots:
        sanitize_plot(p)
    return len(plots)


def test_every_section_has_an_implementation_and_a_recipe() -> None:
    assert set(DATA_SECTION_FUNCS) == set(DATA_SECTIONS)
    assert set(MODEL_SECTION_FUNCS) == set(MODEL_SECTIONS)
    assert set(CHEAP_SECTIONS) <= set(DATA_SECTIONS) and set(FIT_SECTIONS) <= set(MODEL_SECTIONS)
    assert len(EXPLORER_RECIPES) == len(DATA_SECTIONS) + len(MODEL_SECTIONS)


@pytest.mark.parametrize("facet", [None, ["geo"]])
def test_data_explorer_sections_validate_and_plot(facet: list[str] | None) -> None:
    ds, schema = _panel()
    c = data_context(
        ds, schema, DataExploreRequest(facet_by=facet or [], prewhiten=True, max_lag=8)
    )
    assert c.time_dim == "week" and c.freq == "W-SUN" and c.period == 52 and c.kpi == "sales"
    for name, f in DATA_SECTION_FUNCS.items():
        view = f(c).model_dump(mode="json", exclude_none=True)
        json.dumps(view, allow_nan=False)
        assert _sanitized(view) >= 1, name


def test_data_explorer_numbers_make_sense() -> None:
    ds, schema = _panel()
    c = data_context(ds, schema)
    ov = DATA_SECTION_FUNCS["overview"](c)
    assert ov.n_cells == 156 * 3 * 4 and "sales" in ov.summary_plain  # type: ignore[attr-defined]
    corr = DATA_SECTION_FUNCS["correlation"](c)
    assert isinstance(corr, CorrelationView)
    assert set(corr.vars) == {"sales", "spend[tv]", "spend[search]", "price"}
    i, j = corr.vars.index("sales"), corr.vars.index("spend[tv]")
    assert corr.matrix[i][j] is not None and corr.matrix[i][j] > 0.3
    stl = DATA_SECTION_FUNCS["stl"](c).model_dump()
    sales = next(x for x in stl["items"] if x["var"] == "sales")
    assert sales["seasonal_strength"] > 0.3 and sales["period"] == 52
    q = DATA_SECTION_FUNCS["quality"](c).model_dump()
    codes = [w["code"] for w in q["items"]]
    assert "LONG_GAP" in codes and q["items"][0]["severity"] != "info"
    miss = DATA_SECTION_FUNCS["missingness"](c).model_dump()
    assert miss["longest_gap"]["price"] == 7 and miss["share_by_var"]["price"] > 0
    ccf = DATA_SECTION_FUNCS["ccf"](c).model_dump()
    tv = next(x for x in ccf["items"] if x["var"] == "spend[tv]")
    assert tv["values"][tv["lags"].index(0)] > 0.3


def test_quality_flags_impossible_data_in_plain_words() -> None:
    ds, schema = _panel()
    ds["spend"][3, 0, 0] = -5.0
    ds = ds.assign(flat=(("week",), np.ones(156)))
    schema = schema.model_copy(
        update={
            "vars": {
                **schema.vars,
                "flat": schema.vars["price"].model_copy(
                    update={"dims": ("week",), "role": "covariate"}
                ),
            }
        }
    )
    t = ds["week"].values.copy()
    t[1] = t[0]
    ds = ds.assign_coords(week=t)
    c = data_context(ds, schema)
    items = {w.code: w for w in DATA_SECTION_FUNCS["quality"](c).items}  # type: ignore[attr-defined]
    assert items["NEGATIVE_SPEND"].severity == "error"
    assert "flat" in items["CONSTANT_VAR"].message_plain and items["CONSTANT_VAR"].suggested_action
    assert "DUPLICATE_PERIODS" in items


def test_sections_that_do_not_apply_are_skipped_with_a_reason() -> None:
    ds = xr.Dataset({"a": ("obs", np.arange(10.0)), "b": ("obs", np.arange(10.0) ** 2)})
    c = data_context(ds, None)
    assert c.time_dim is None
    for section in ("timeseries", "ccf", "acf_pacf", "stationarity", "stl"):
        with pytest.raises(SectionSkipped):
            DATA_SECTION_FUNCS[section](c)
    DATA_SECTION_FUNCS["correlation"](c)
    with pytest.raises(SectionSkipped):
        data_context(ds, None, DataExploreRequest(vars=["nope"]))


def test_helpers() -> None:
    assert explore_period("W-SUN") == 52 and explore_period("D") == 7
    assert explore_period("MS") == 12 and explore_period(None) is None
    assert jsonable(np.datetime64("2024-01-07T00:00:00")) == "2024-01-07"
    assert jsonable(np.float64("nan")) is None and jsonable(np.int64(3)) == 3


def test_write_sections_writes_views_plots_and_an_index(tmp_path: Path) -> None:
    ds, schema = _panel()
    ctx = FakeCtx(tmp_path)
    index = write_data_explorer(ctx, ds, schema, ref="dataset://p/sales@v1")  # type: ignore[arg-type]
    assert index is not None and [s.section for s in index.sections] == list(CHEAP_SECTIONS)
    assert all(s.status == "ok" for s in index.sections)
    stored = ExplorerIndex.model_validate(ctx.written[EXPLORE_INDEX])
    assert stored.ref == "dataset://p/sales@v1"
    assert "ui/explore/overview.json" in ctx.written
    assert (
        ctx.written["ui/explore/index.json"]["sections"][0]["plots"]
        == "ui/explore/plots/overview.json"
    )
    for p in ctx.written["ui/explore/plots/timeseries.json"]:
        sanitize_plot(p)
    ds2 = xr.Dataset({"a": ("obs", np.arange(10.0))})
    c = data_context(ds2, None)
    idx = write_sections(ctx, DATA_SECTION_FUNCS, ["overview", "stl"], c, target="dataset", ref="x")  # type: ignore[arg-type]
    assert [s.status for s in idx.sections] == ["ok", "skipped"]
    assert idx.sections[1].message_plain and "time" in idx.sections[1].message_plain


@pytest.fixture(scope="module")
def model_ctx() -> ModelContext:
    reg = default_registry()
    data = linear_data()
    b = fit(linear_spec(), data, reg)
    ridge = fit(linear_spec("sklearn.Ridge"), data, reg)
    req = ModelExploreRequest(
        backtest={"kind": "expanding_window", "horizons": [1, 4], "n_folds": 3, "min_train": 40},  # type: ignore[arg-type]
        horizon=8,
    )
    return ModelContext(
        b,
        data,
        reg,
        req,
        ref="model://p/lin@v1",
        compare=[("model://p/ridge@v1", ridge)],
        refitter=Refitter(b, reg),
    )


def test_model_explorer_sections_validate_and_plot(model_ctx: ModelContext) -> None:
    done = set()
    for name, f in MODEL_SECTION_FUNCS.items():
        try:
            view = f(model_ctx).model_dump(mode="json", exclude_none=True)
        except SectionSkipped:
            continue
        done.add(name)
        json.dumps(view, allow_nan=False)
        _sanitized(view)
    assert done == {
        "params",
        "diagnostics",
        "fit",
        "residuals",
        "decomposition",
        "forecast",
        "backtest",
        "comparison",
    }


def test_model_explorer_numbers(model_ctx: ModelContext) -> None:
    params = MODEL_SECTION_FUNCS["params"](model_ctx).model_dump()
    coef = {
        r["coords"].get("feature"): r["mean"] for r in params["rows"] if r["name"] == "coefficient"
    }
    assert coef["spend"] == pytest.approx(1.5, abs=0.1) and coef["price"] == pytest.approx(
        -0.8, abs=0.2
    )
    fit_view = MODEL_SECTION_FUNCS["fit"](model_ctx).model_dump()
    assert fit_view["split_at"] == "2023-10-08"
    res = MODEL_SECTION_FUNCS["residuals"](model_ctx).model_dump()
    lb = next(t for t in res["tests"] if t["test"] == "test.ljung_box")
    assert lb["decision"] == "white_noise"
    bt = MODEL_SECTION_FUNCS["backtest"](model_ctx).model_dump()
    assert len(bt["folds"]) == 3 and bt["summary"]["skill_vs_best_benchmark"] > 0.5
    cmp = MODEL_SECTION_FUNCS["comparison"](model_ctx).model_dump()
    assert cmp["models"] == ["model://p/lin@v1", "model://p/ridge@v1"]
    assert set(cmp["ranking"]["rmse"]) == set(cmp["models"])
    assert cmp["tests"] and cmp["tests"][0]["test"] == "test.diebold_mariano"
    dec = MODEL_SECTION_FUNCS["decomposition"](model_ctx).model_dump()
    assert set(dec["components"]) == {"intercept", "spend", "price"}


def test_var_and_mmm_views_plot_through_the_sanitizer() -> None:
    irf = IrfView(
        variables=["y", "x"],
        horizons=3,
        values=[[[1, 0.5, 0.2, 0.1], [0, 0.3, 0.2, 0.1]], [[0, 0.1, 0.1, 0], [1, 0.4, 0.1, 0]]],
    )
    fevd = FevdView(
        variables=["y", "x"],
        horizons=2,
        shares=[[[1.0, 0.0], [0.9, 0.1]], [[0.2, 0.8], [0.3, 0.7]]],
    )
    rc = ResponseCurvesView(
        channels=["tv"],
        spend=[[0, 1, 2]],
        mean=[[0, 5, 7]],
        lower=[[0, 4, 5]],
        upper=[[0, 6, 9]],
        current_spend=[1.0],
    )
    for view in (irf, fevd, rc):
        assert _sanitized(view.model_dump(mode="json", exclude_none=True)) == 1


def test_ingest_style_explorer_on_the_linear_schema(tmp_path: Path) -> None:
    ctx = FakeCtx(tmp_path)
    index = write_data_explorer(
        ctx,
        linear_data(),
        SCHEMA,
        ref="dataset://p/sales@v1",  # type: ignore[arg-type]
        sections=list(DATA_SECTIONS),
    )
    assert index is not None
    status = {s.section: s.status for s in index.sections}
    assert status["groups"] == "skipped" and status["stl"] == "ok"
    assert not ctx.warnings
