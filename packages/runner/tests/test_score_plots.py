"""Score plots by the shape of each output (:mod:`mapi_runner.worker.score_plots`): a map
with a frame slider for grid outputs, an area map for outputs over polygons, the state
sequence, the classification plots of a binary evaluate, a fan for an output over time, the
mode's defaults otherwise; the KPI of a gridded model, the summary tables' dims and plots,
and options reaching the recipes.

The bundles are fakes (a manifest, a raw schema, a binding and model groups), so the unit
tests need no plugin and no fit; the integration tests fit the earthquake LGCP
(``sci.lgcp_grid``), a logistic classifier and the BYM2 disease map and score them.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import xarray as xr
from gov_fixtures import FakeCtx
from mapi_runner.worker import plots as plots_mod
from mapi_runner.worker import score as score_mod
from mapi_runner.worker.guided import _kpi_var
from mapi_runner.worker.plots import render_for
from mapi_runner.worker.score import summary_document, summary_tables
from mapi_runner.worker.score_plots import (
    BINARY_EVALUATE_PLOTS,
    DEFAULT_PLOTS,
    ScorePlots,
    _state_probabilities,
    area_outputs,
    area_plot_tree,
    binary_output,
    default_score_plots,
    field_plot_tree,
    has_area_outputs,
    has_grid_outputs,
    state_outputs,
)
from mapi_spec import DataSchema
from mapi_spec.capabilities import OutputDecl, Representation
from mapi_spec.views import SCORE_FIELDS_VIEW, FieldsView

GRID: dict[str, Any] = {"y": "row", "x": "col", "frame": "period", "log": True}
LGCP: dict[str, dict[str, Any]] = {
    "rate": {"dims": ["period", "row", "col"], "units_of": "count", "grid": GRID},
    "total": {"dims": ["period"], "units_of": "count"},
}
RAW = DataSchema.model_validate(
    {
        "dims": {
            "quarter": {"kind": "time", "policy": "extend", "freq": "QS"},
            "row": {"kind": "integer", "policy": "fixed"},
            "col": {"kind": "integer", "policy": "fixed"},
        },
        "vars": {"quakes": {"dims": ["quarter", "row", "col"], "dtype": "int64"}},
    }
)
_TO_DATASET = {"period": "quarter", "count": "quakes", "area": "district"}
_TO_CANON = {v: k for k, v in _TO_DATASET.items()}


def _rename(ds: xr.Dataset, names: dict[str, str]) -> xr.Dataset:
    present = set(map(str, ds.variables)) | set(map(str, ds.dims))
    return ds.rename({k: v for k, v in names.items() if k in present and k != v})


def _bundle(
    outputs: dict[str, dict[str, Any]],
    targets: list[str] | None = None,
    groups: dict[str, xr.Dataset] | None = None,
) -> Any:
    manifest = SimpleNamespace(
        outputs={k: OutputDecl.model_validate(v) for k, v in outputs.items()}
    )
    return SimpleNamespace(
        model=SimpleNamespace(manifest=manifest, groups=groups or {}),
        raw_schema=RAW,
        binding=SimpleNamespace(
            to_dataset=lambda n: _TO_DATASET.get(n, n),
            to_canonical=lambda n: _TO_CANON.get(n, n),
            reverse=lambda ds: _rename(ds, _TO_DATASET),
        ),
        resolved=SimpleNamespace(
            model=SimpleNamespace(input_schema=SimpleNamespace(targets=targets or ["count"]))
        ),
    )


def _lgcp_tree(t: str = "quarter", *, full: bool = True, mode: str = "forecast") -> xr.DataTree:
    rng = np.random.default_rng(0)
    rate = rng.gamma(3.0, 1.0, (2, 10, 4, 3, 5))
    coords = {t: [f"2030Q{i + 1}" for i in range(4)]}
    pred = xr.Dataset(
        {
            "rate": (("chain", "draw", t, "row", "col"), rate),
            "total": (("chain", "draw", t), rate.sum(axis=(-2, -1))),
        },
        coords=coords,
    )
    stats = ["mean", "sd"]
    summary = xr.Dataset(
        {
            "rate": (("stat", t, "row", "col"), np.stack([rate.mean((0, 1)), rate.std((0, 1))])),
            "total": (("stat", t), np.ones((2, 4))),
        },
        coords={**coords, "stat": stats},
    )
    nodes = {"/": xr.Dataset(attrs={"mode": mode}), "/summary": summary}
    if full:
        nodes["/predictions"] = pred
    return xr.DataTree.from_dict(nodes)


# ---------------------------------------------------------------------- default plots


@pytest.mark.parametrize("mode", ["forecast", "predict"])
def test_a_gridded_forecast_gets_a_map_over_time_and_the_total_fan(mode: str) -> None:
    b = _bundle(LGCP)
    assert has_grid_outputs(b)
    got = default_score_plots(b, mode, _lgcp_tree(mode=mode))
    assert got.refs == ["sci.field_map", "core.ppc_fan"]
    assert got.options == {
        "sci.field_map": {"var": "rate", "y": "row", "x": "col", "frame": "quarter"},
        "core.ppc_fan": {"var": "total"},
    }


def test_canonical_names_resolve_the_frame_and_the_time_dim() -> None:
    got = default_score_plots(_bundle(LGCP), "forecast", _lgcp_tree("period"))
    assert got.options["sci.field_map"]["frame"] == "period"
    assert got.options["core.ppc_fan"] == {"var": "total"}


def test_evaluate_adds_the_map_to_its_defaults() -> None:
    got = default_score_plots(_bundle(LGCP), "evaluate", _lgcp_tree(mode="evaluate"))
    assert got.refs == [*DEFAULT_PLOTS["evaluate"], "sci.field_map"]
    assert set(got.options) == {"sci.field_map"}


@pytest.mark.parametrize("mode", ["predict", "forecast", "counterfactual", "evaluate"])
def test_models_without_grids_keep_the_defaults_of_their_mode(mode: str) -> None:
    b = _bundle({"y_hat": {"dims": ["period"], "units_of": "y"}}, ["y"])
    assert not has_grid_outputs(b) and not has_area_outputs(b)
    summary = xr.Dataset({"y_hat": (("stat", "quarter"), [[1.0]])})
    tree = xr.DataTree.from_dict({"/summary": summary})
    assert default_score_plots(b, mode, tree) == ScorePlots(DEFAULT_PLOTS[mode])


def test_an_evaluate_without_an_output_over_time_draws_no_residuals_over_time() -> None:
    b = _bundle({"y_hat": {"dims": ["row"], "units_of": "y"}}, ["y"])
    pred = xr.Dataset({"y_hat": (("row",), [1.5, 2.5, 3.5])})
    tree = xr.DataTree.from_dict({"/predictions": pred, "/observed": pred.rename(y_hat="y")})
    assert default_score_plots(b, "evaluate", tree) == ScorePlots(["core.fit_vs_actual"])


def test_counterfactuals_keep_their_plot() -> None:
    got = default_score_plots(_bundle(LGCP), "counterfactual", _lgcp_tree())
    assert got == ScorePlots(["core.counterfactual"])


def test_a_vector_grid_gets_a_vector_field() -> None:
    b = _bundle(
        {
            "traction": {
                "dims": ["component", "row", "col"],
                "grid": {"y": "row", "x": "col", "component": "component"},
            }
        }
    )
    da = xr.DataArray(np.ones((1, 2, 2, 3, 4)), dims=["chain", "draw", "component", "row", "col"])
    tree = xr.DataTree.from_dict({"/predictions": xr.Dataset({"traction": da})})
    got = default_score_plots(b, "predict", tree)
    assert got.refs == ["sci.vector_field"]
    assert got.options["sci.vector_field"] == {
        "var": "traction",
        "y": "row",
        "x": "col",
        "component": "component",
    }


def test_a_grid_without_an_output_over_time_gets_no_fan() -> None:
    b = _bundle(
        {"density": {"dims": ["t", "x"], "grid": {"y": "t", "x": "x"}}, "speed": {"dims": []}}
    )
    tree = xr.DataTree.from_dict(
        {
            "/predictions": xr.Dataset(
                {
                    "density": (("chain", "draw", "t", "x"), np.ones((1, 2, 3, 4))),
                    "speed": (("chain", "draw"), np.ones((1, 2))),
                }
            )
        }
    )
    assert default_score_plots(b, "forecast", tree).refs == ["sci.field_map"]


def test_the_first_grid_with_a_frame_is_drawn() -> None:
    b = _bundle(
        {
            "settled": {"dims": ["row", "col"], "grid": {"y": "row", "x": "col"}},
            "pattern": {
                "dims": ["t", "row", "col"],
                "grid": {"y": "row", "x": "col", "frame": "t"},
            },
        }
    )
    pred = xr.Dataset(
        {
            "settled": (("chain", "draw", "row", "col"), np.ones((1, 2, 3, 4))),
            "pattern": (("chain", "draw", "t", "row", "col"), np.ones((1, 2, 5, 3, 4))),
        }
    )
    got = default_score_plots(b, "predict", xr.DataTree.from_dict({"/predictions": pred}))
    assert got.options["sci.field_map"]["var"] == "pattern"


def test_a_grid_aggregated_away_is_not_drawn() -> None:
    tree = _lgcp_tree(full=False)
    summary = tree["summary"].to_dataset().assign(rate=lambda d: d["rate"].sum("row"))
    tree = xr.DataTree.from_dict({"/summary": summary})
    got = default_score_plots(_bundle(LGCP), "predict", tree)
    assert got.refs == ["core.ppc_fan"]


# ------------------------------------------------------------------------- plot trees


def test_the_field_plot_tree_keeps_the_grid_outputs_with_draws() -> None:
    b = _bundle(LGCP)
    assert field_plot_tree(b, _lgcp_tree(full=False)) is None
    ft = field_plot_tree(b, _lgcp_tree())
    assert ft is not None and ft.attrs["mode"] == "forecast"
    pred = ft["predictions"].to_dataset()
    assert list(pred.data_vars) == ["rate"] and pred["rate"].sizes["chain"] == 2


def test_the_kpi_of_a_gridded_model_is_its_total() -> None:
    pred = _lgcp_tree()["predictions"].to_dataset()
    assert _kpi_var(_bundle(LGCP), pred) == "total"
    assert _kpi_var(_bundle(LGCP), pred, skip_grids=False) == "rate"
    only_grid = _bundle({"rate": LGCP["rate"]})
    assert _kpi_var(only_grid, pred) == "rate"
    ts = _bundle({"y_hat": {"dims": ["time"], "units_of": "y"}}, ["y"])
    assert _kpi_var(ts, xr.Dataset({"y_hat": ("time", [1.0])})) == "y_hat"


def test_a_scalar_output_is_not_the_kpi_of_a_gridded_model() -> None:
    # sci.fisher_kpp: a density map in target units and a wave speed with no dims or units
    kpp = _bundle(
        {
            "density": {"dims": ["period", "row", "col"], "units_of": "count", "grid": GRID},
            "wave_speed": {"dims": []},
        }
    )
    rng = np.random.default_rng(0)
    pred = xr.Dataset(
        {
            "density": (("chain", "draw", "quarter", "row", "col"), rng.random((2, 5, 4, 3, 3))),
            "wave_speed": (("chain", "draw"), rng.random((2, 5))),
        },
        coords={"quarter": [f"2030Q{i + 1}" for i in range(4)]},
    )
    assert _kpi_var(kpp, pred, time="quarter") == "density"
    assert _kpi_var(kpp, pred) == "density"
    # a non-grid output over the horizon still comes first
    over_time = _bundle({**LGCP, "speed": {"dims": ["period"]}})
    assert (
        _kpi_var(over_time, pred.assign(speed=("quarter", np.ones(4))), time="quarter") == "speed"
    )


# ---------------------------------------------------------- full groups and field names


def test_an_evaluate_scored_in_full_for_its_grids_drops_the_observed_group() -> None:
    tree = _lgcp_tree(mode="evaluate")
    observed = tree["summary"].to_dataset()[["total"]]
    tree = xr.DataTree.from_dict(
        {**{n.path: n.to_dataset(inherit=False) for n in tree.subtree}, "/observed": observed}
    )
    trimmed = score_mod._without_full(tree, observed=True)
    assert {n.path for n in trimmed.subtree} == {"/", "/summary"}
    kept = score_mod._without_full(tree)
    assert {n.path for n in kept.subtree} == {"/", "/summary", "/observed"}


def test_score_fields_name_each_output_as_the_summary_tables_do(tmp_path: Path) -> None:
    # a grid output bound to another dataset name: the fields use the result's name
    bound = _bundle(LGCP)
    bound.model.manifest.capabilities = SimpleNamespace(representation=Representation.DRAWS)
    bound.model.groups, bound.model.derived = {}, {}
    names = {**_TO_DATASET, "rate": "quake_rate"}
    bound.binding.to_dataset = lambda n: names.get(n, n)
    tree = _lgcp_tree()
    pred = tree["predictions"].to_dataset().rename({"rate": "quake_rate"})
    tree = xr.DataTree.from_dict({"/": xr.Dataset(), "/predictions": pred})
    ctx = FakeCtx(tmp_path)
    score_mod._write_fields(ctx, bound, tree)  # type: ignore[arg-type]
    view = FieldsView.model_validate(ctx.written[SCORE_FIELDS_VIEW])
    assert [f.output for f in view.fields] == ["quake_rate"]


# --------------------------------------------------------------------- summary tables


def test_summary_tables_name_their_dims_and_time_and_put_small_tables_first() -> None:
    tree = _lgcp_tree(full=False)
    doc = summary_document(tree, None, _bundle(LGCP))
    assert doc is not None
    total, rate = doc["tables"]
    assert (total["variable"], rate["variable"]) == ("total", "rate")
    assert total["dims"] == ["quarter"] and total["time_dim"] == "quarter"
    assert rate["dims"] == ["quarter", "row", "col"] and rate["time_dim"] == "quarter"
    assert rate["columns"] == ["quarter", "row", "col", "mean", "sd"]
    # a truncation drops the grid, not the total
    ds = tree["summary"].to_dataset()
    cut = summary_tables(ds, max_cells=30)
    assert cut["truncated"] and len(cut["tables"][0]["rows"]) == 4
    # without a bundle: dims, no time
    plain = summary_document(tree)
    assert plain is not None and "time_dim" not in plain["tables"][0]


# --------------------------------------------------------------------------- render_for


def test_render_for_passes_the_options_to_the_recipes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Any] = []

    def fake(tree: Any, refs: Any, *, registry: Any, options: Any) -> Any:
        seen.append(options)
        return SimpleNamespace(to_json=lambda: [{"id": r} for r in refs], issues=[])

    monkeypatch.setattr(plots_mod, "render_plots", fake)
    opts = {"core.ppc_fan": {"var": "total"}}
    out = render_for(FakeCtx(tmp_path), ["core.ppc_fan"], [_lgcp_tree()], options=opts)  # type: ignore[arg-type]
    assert out == [{"id": "core.ppc_fan"}] and seen == [opts]


# ------------------------------------------------- state, area and binary outputs (v0.10)

HMM: dict[str, dict[str, Any]] = {
    "y_hat": {"dims": ["animal", "step"]},
    "state_prob": {"dims": ["animal", "step", "state"], "units": "probability"},
}


@pytest.mark.parametrize("mode", ["predict", "forecast", "evaluate", "simulate"])
def test_a_state_output_gets_the_state_sequence(mode: str) -> None:
    b = _bundle(HMM, ["y"])
    assert state_outputs(b) == {"state_prob": "state"}
    summary = xr.Dataset(
        {
            "y_hat": (("stat", "animal", "step"), np.ones((1, 2, 5))),
            "state_prob": (("stat", "animal", "step", "state"), np.full((1, 2, 5, 3), 1 / 3)),
        },
        coords={"stat": ["mean"]},
    )
    got = default_score_plots(b, mode, xr.DataTree.from_dict({"/summary": summary}))
    assert "core.state_sequence" in got.refs
    assert got.options["core.state_sequence"] == {"var": "state_prob", "state_dim": "state"}
    assert got.by_var() == {"state_prob": ["core.state_sequence"]}
    cf = default_score_plots(b, "counterfactual", xr.DataTree.from_dict({"/summary": summary}))
    assert cf == ScorePlots(["core.counterfactual"])


def test_hmm_draws_with_padded_steps_get_the_state_sequence() -> None:
    """Probabilities over ``state`` in ``/predictions`` (draws), with NaN padded steps."""
    b = _bundle(HMM, ["y"])
    p = np.random.default_rng(0).dirichlet(np.ones(3), (2, 4, 2, 5))  # chain, draw, animal, step
    p[:, :, 1, 3:] = np.nan  # the second sequence is shorter
    pred = xr.Dataset({"state_prob": (("chain", "draw", "animal", "step", "state"), p)})
    got = default_score_plots(b, "predict", xr.DataTree.from_dict({"/predictions": pred}))
    assert got.options["core.state_sequence"] == {"var": "state_prob", "state_dim": "state"}


PANEL_STATES: dict[str, dict[str, Any]] = {"sales": {"dims": ["time", "state"]}}


@pytest.mark.parametrize("n", [5, 50])
@pytest.mark.parametrize("mode", ["predict", "forecast", "evaluate"])
def test_a_panel_of_us_states_is_not_a_hidden_state(mode: str, n: int) -> None:
    """``sales(time, state)`` over US states: a ``state`` dim of sales, not probabilities, so
    no state plot (it drew each state's share of sales as "state probabilities" and, with
    more than 10 states, warned on every score run)."""
    b = _bundle(PANEL_STATES, ["sales"])
    assert state_outputs(b) == {"sales": "state"}  # a candidate by name only
    rng = np.random.default_rng(1)
    sales = rng.gamma(5.0, 100.0, (2, 10, 8, n))
    pred = xr.Dataset(
        {"sales": (("chain", "draw", "time", "state"), sales)},
        coords={"state": [f"S{i}" for i in range(n)]},
    )
    nodes = {
        "/predictions": pred,
        "/summary": pred.mean(("chain", "draw")).expand_dims(stat=["mean"]),
    }
    got = default_score_plots(b, mode, xr.DataTree.from_dict(nodes))
    assert "core.state_sequence" not in got.refs
    assert "core.state_sequence" not in got.options


def test_dims_that_only_contain_the_word_are_not_state_dims() -> None:
    outputs: dict[str, dict[str, Any]] = {
        "share": {"dims": ["time", "us_state"]},
        "price": {"dims": ["time", "real_estate"]},
        "mix": {"dims": ["time", "regimes_seen"]},
    }
    b = _bundle(outputs, ["share"])
    assert state_outputs(b) == {}
    # even when the values are probabilities over the dim (a share of a total)
    share = np.random.default_rng(2).dirichlet(np.ones(4), 6)
    summary = xr.Dataset(
        {"share": (("stat", "time", "us_state"), share[None])}, coords={"stat": ["mean"]}
    )
    got = default_score_plots(b, "predict", xr.DataTree.from_dict({"/summary": summary}))
    assert "core.state_sequence" not in got.refs


def test_state_probabilities_need_a_mean_values_in_range_and_sums_of_one() -> None:
    probs = np.full((2, 3), 1 / 3)
    ok = xr.DataArray(probs, dims=("step", "state"))
    assert _state_probabilities(ok, "state")
    assert not _state_probabilities(ok * 2, "state")  # sums of 2
    off = xr.DataArray([[1.5, -0.5, 0.0]], dims=("step", "state"))
    assert not _state_probabilities(off, "state")  # sums of 1, but out of [0, 1]
    by_stat = ok.expand_dims(stat=["median"])
    assert not _state_probabilities(by_stat, "state")  # no mean or point to check
    assert _state_probabilities(ok.expand_dims(stat=["point"]), "state")
    assert not _state_probabilities(ok.where(False), "state")  # no complete cell


def _rings(n: int) -> xr.Dataset:
    x = np.tile([0.0, 1.0, 1.0, 0.0, 0.0], (n, 1)) + np.arange(n)[:, None]
    y = np.tile([0.0, 0.0, 1.0, 1.0, 0.0], (n, 1))
    return xr.Dataset(
        {
            "ring_x": (("area", "vertex"), x),
            "ring_y": (("area", "vertex"), y),
            "adjacency": (("area", "area_"), np.eye(n, k=1) + np.eye(n, k=-1)),
            "expected": (("area",), np.ones(n)),
        }
    )


AREAL: dict[str, dict[str, Any]] = {
    "cases": {"dims": ["area"], "units_of": "count"},
    "rr": {"dims": ["area"], "units": "1"},
    "b0": {"dims": []},
}


def _areal_tree(area: str = "area", *, mode: str = "predict", names: str = "canonical") -> Any:
    rng = np.random.default_rng(1)
    pred = xr.Dataset(
        {
            "rr": (("chain", "draw", area), rng.gamma(4.0, 0.25, (2, 5, 4))),
            "cases": (("chain", "draw", area), rng.poisson(3.0, (2, 5, 4)).astype(float)),
        },
        coords={area: [f"d{i}" for i in range(4)]},
    )
    summary = pred.mean(("chain", "draw")).expand_dims(stat=["mean"])
    nodes = {
        "/": xr.Dataset(attrs={"mode": mode, "names": names}),
        "/predictions": pred,
        "/summary": summary,
    }
    if mode == "evaluate":
        nodes["/observed"] = xr.Dataset({"cases": ((area,), [2.0, 3.0, 4.0, 1.0])})
    return xr.DataTree.from_dict(nodes)


@pytest.mark.parametrize("mode", ["predict", "forecast", "simulate"])
def test_areal_outputs_get_an_area_map_and_no_fan(mode: str) -> None:
    b = _bundle(AREAL, groups={"constant_data": _rings(4)})
    assert area_outputs(b) == {"cases": "area", "rr": "area"} and has_area_outputs(b)
    got = default_score_plots(b, mode, _areal_tree(mode=mode))
    assert got.refs == ["sci.area_map"]
    assert got.options == {"sci.area_map": {"var": "cases"}}  # the first in manifest order


def test_an_areal_evaluate_draws_no_residuals_over_time() -> None:
    b = _bundle(AREAL, groups={"constant_data": _rings(4)})
    got = default_score_plots(b, "evaluate", _areal_tree(mode="evaluate"))
    assert got.refs == ["core.fit_vs_actual", "sci.area_map"]


def test_polygons_in_no_model_group_make_no_areal_outputs() -> None:
    assert area_outputs(_bundle(AREAL)) == {}
    bad = _rings(4).assign(ring_y=(("vertex", "area"), np.zeros((5, 4))))
    assert area_outputs(_bundle(AREAL, groups={"constant_data": bad})) == {}
    b = _bundle(AREAL, groups={"geometry": _rings(4)})
    assert default_score_plots(b, "predict", _areal_tree()).refs == ["sci.area_map"]


def test_the_area_plot_tree_carries_the_polygons_under_the_result_names() -> None:
    b = _bundle(AREAL, groups={"constant_data": _rings(4)})
    assert area_plot_tree(_bundle(AREAL), _areal_tree()) is None
    canon = area_plot_tree(b, _areal_tree())
    assert canon is not None
    assert {n.path for n in canon.subtree} == {"/", "/predictions", "/geometry"}
    geo = canon["geometry"].to_dataset()
    assert sorted(geo.data_vars) == ["adjacency", "ring_x", "ring_y"]  # not "expected"
    assert geo["ring_x"].dims == ("area", "vertex")
    assert sorted(canon["predictions"].to_dataset().data_vars) == ["cases", "rr"]
    # a result in dataset names: the polygons follow (area -> district)
    named = area_plot_tree(b, _areal_tree("district", mode="evaluate", names="dataset"))
    assert named is not None and named.attrs["names"] == "dataset"
    assert named["geometry"].to_dataset()["ring_x"].dims == ("district", "vertex")
    assert "observed" in named
    # a trimmed result: the summary
    trimmed = score_mod._without_full(_areal_tree())
    got = area_plot_tree(b, trimmed)
    assert got is not None and "summary" in got and "predictions" not in got


def _binary_tree(p: np.ndarray, y: np.ndarray, *, draws: bool = False) -> xr.DataTree:
    pred = (
        xr.Dataset({"y_hat": (("chain", "draw", "row"), np.broadcast_to(p, (1, 3, p.size)))})
        if draws
        else xr.Dataset({"y_hat": (("row",), p)})
    )
    return xr.DataTree.from_dict(
        {"/predictions": pred, "/observed": xr.Dataset({"churn": (("row",), y)})}
    )


@pytest.mark.parametrize("draws", [False, True])
def test_a_binary_evaluate_draws_the_classification_plots(draws: bool) -> None:
    b = _bundle({"y_hat": {"dims": ["row"], "units": "1"}}, ["target"])
    b.binding.to_dataset = lambda n: {"target": "churn"}.get(n, n)
    tree = _binary_tree(np.array([0.1, 0.8, 0.4, 0.9]), np.array([0.0, 1.0, 0.0, 1.0]), draws=draws)
    assert binary_output(b, tree) == ("y_hat", "churn")
    got = default_score_plots(b, "evaluate", tree)
    assert got.refs == list(BINARY_EVALUATE_PLOTS)
    assert got.options == {r: {"var": "y_hat", "observed_var": "churn"} for r in got.refs}
    assert got.by_var() == {"y_hat": list(BINARY_EVALUATE_PLOTS)}
    # predictions are not drawn as classes outside an evaluate
    assert default_score_plots(b, "predict", tree) == ScorePlots(DEFAULT_PLOTS["predict"])


@pytest.mark.parametrize(
    ("p", "y"),
    [
        ([0.1, 0.8, 0.4, 0.9], [1.0, 2.0, 3.0, 1.0]),  # three observed values
        ([0.1, 1.8, 0.4, 0.9], [0.0, 1.0, 0.0, 1.0]),  # a prediction above 1
        ([0.1, 0.8, 0.4, 0.9], [1.0, 1.0, 1.0, 1.0]),  # one class only
    ],
)
def test_a_continuous_evaluate_keeps_fit_vs_actual(p: list[float], y: list[float]) -> None:
    b = _bundle({"y_hat": {"dims": ["row"]}}, ["y"])
    tree = _binary_tree(np.array(p), np.array(y))
    assert binary_output(b, tree) is None
    assert default_score_plots(b, "evaluate", tree).refs == ["core.fit_vs_actual"]
    no_observed = xr.DataTree.from_dict({"/predictions": tree["predictions"].to_dataset()})
    assert binary_output(b, no_observed) is None


def test_by_var_maps_each_result_variable_to_its_recipes() -> None:
    plots = ScorePlots(
        ["sci.field_map@1.0.0", "core.ppc_fan", "core.convergence"],
        {"sci.field_map": {"var": "rate"}, "core.ppc_fan": {"var": "total"}},
    )
    assert plots.by_var() == {"rate": ["sci.field_map"], "total": ["core.ppc_fan"]}
    assert ScorePlots(["core.ppc_fan"]).by_var() == {}


# ------------------------------------------------------------ summary.json lists the plots


def test_summary_tables_list_only_the_plots_that_rendered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rendered: list[str] = ["sci.area_map", "core.fit_vs_actual"]
    seen: list[Any] = []

    def fake_render(ctx: Any, refs: Any, trees: Any, options: Any = None) -> Any:
        seen.append([t.attrs.get("mode") for t in trees])
        return [{"id": r} for r in refs if r in rendered]

    monkeypatch.setattr(score_mod, "render_for", fake_render)
    b = _bundle(AREAL, groups={"constant_data": _rings(4)})
    plots = ScorePlots(
        ["core.fit_vs_actual", "sci.area_map", "core.ppc_fan"],
        {"sci.area_map": {"var": "cases"}, "core.ppc_fan": {"var": "rr"}},
    )
    ctx = _ScoreCtx(tmp_path)
    tree = score_mod._without_full(_areal_tree())
    score_mod._write_tree(ctx, b, tree, plots)  # type: ignore[arg-type]
    tables = {t["variable"]: t for t in ctx.written["summary.json"]["tables"]}
    assert tables["cases"]["plots"] == ["sci.area_map"]
    assert "plots" not in tables["rr"]  # its fan did not render
    assert [p["id"] for p in ctx.written["plots.json"]] == ["core.fit_vs_actual", "sci.area_map"]
    doc = summary_document(tree, None, b, plots={"rr": ["core.ppc_fan"]})
    assert doc is not None
    assert {t["variable"]: t.get("plots") for t in doc["tables"]} == {
        "cases": None,
        "rr": ["core.ppc_fan"],
    }


# ------------------------------------------------------------------------- fit plots


def test_a_fit_with_a_loss_history_draws_the_loss_curve() -> None:
    from mapi_runner.worker.fit import fit_plot_refs

    spec = SimpleNamespace(outputs=SimpleNamespace(plots=["core.convergence"]))
    history = xr.Dataset({"loss": (("step",), np.linspace(10.0, 1.0, 50))})
    fitted = SimpleNamespace(model=SimpleNamespace(groups={"fit_history": history}))
    assert fit_plot_refs(spec, fitted) == ["core.convergence", "core.loss_curve"]  # type: ignore[arg-type]
    listed = SimpleNamespace(outputs=SimpleNamespace(plots=["core.loss_curve@1.0.0"]))
    assert fit_plot_refs(listed, fitted) == ["core.loss_curve@1.0.0"]  # type: ignore[arg-type]
    nuts = SimpleNamespace(model=SimpleNamespace(groups={"posterior": history}))
    assert fit_plot_refs(spec, nuts) == ["core.convergence"]  # type: ignore[arg-type]


# ------------------------------------------------------------------- end to end (fit)


class _ScoreCtx(FakeCtx):
    def write_output(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def fit_hooks(self, **_k: Any) -> None:
        return None

    def metric(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.filterwarnings("ignore")
def test_an_lgcp_forecast_writes_its_fields_and_a_map_over_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("mapi_plugin_science")
    from mapi_plugin_science.lgcp import LGCPGridModel
    from mapi_plugin_science.lgcp_data import LGCP_SCHEMA, LGCP_SPEC_YAML, lgcp_dataset
    from mapi_runner.worker import score as score_mod
    from mapi_sdk.harness import fit_spec
    from mapi_sdk.registry import default_registry
    from mapi_spec import load_score_request
    from mapi_spec.jobs import ScoreJob
    from mapi_spec.views import SCORE_FIELDS_VIEW, FieldsView

    reg = default_registry(reload=True).copy()
    reg.register_model(LGCPGridModel, replace=True)
    data = lgcp_dataset(seed=0)[["quakes"]]
    bundle = fit_spec(LGCP_SPEC_YAML, data, registry=reg, raw_schema=LGCP_SCHEMA)
    monkeypatch.setattr(score_mod, "load_bundle", lambda ctx: bundle)
    monkeypatch.setattr(score_mod, "load_side_tables", lambda ctx, spec: {})
    request = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/lgcp\n"
        "mode: forecast\nhorizon: {dim: period, periods: 4}\n"
    )
    ctx = _ScoreCtx(tmp_path / "forecast", reg)
    tree = score_mod.run_score(ctx, ScoreJob(request=request))  # type: ignore[arg-type]
    assert "predictions" not in tree  # the draws only fed the maps and the fan

    view = FieldsView.model_validate(ctx.written[SCORE_FIELDS_VIEW])
    [field] = view.fields
    assert field.output == "rate" and field.frame_dim == "quarter" and len(field.frames) == 4
    assert field.n_draws > 0
    plots = {p["id"]: p for p in ctx.written["plots.json"]}
    assert list(plots) == ["sci.field_map", "core.ppc_fan"], ctx.warnings
    assert "training data ends" in plots["core.ppc_fan"]["caption_plain"]
    assert ctx.written["ui/guided/forecast.json"]["kpi"] == "total"
    tables = {t["variable"]: t for t in ctx.written["summary.json"]["tables"]}
    assert tables["total"]["time_dim"] == "quarter"
    assert tables["rate"]["dims"] == ["quarter", "row", "col"]
    assert not [w for w in ctx.warnings if w[0] == "PLOT_SKIPPED"], ctx.warnings
    json.dumps(ctx.written["plots.json"], allow_nan=False)


_CLASSIFIER_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: stockouts, project: retail}
data:
  dataset: dataset://retail/stockouts@latest
  bind:
    dims: {time: week}
    vars: {y: sold_out, price: price, promo: promo_flag}
  split: {kind: time_holdout, dim: time, last: 8}
model:
  use: sklearn.tabular_classifier@^1
  settings: __SETTINGS__
  fit: {seed: 3}
evaluate: {metrics: [rmse]}
"""


def _stub_payload(monkeypatch: pytest.MonkeyPatch, bundle: Any, data: xr.Dataset) -> None:
    from mapi_spec.run import INPUT_DATASET

    monkeypatch.setattr(score_mod, "load_bundle", lambda ctx: bundle)
    monkeypatch.setattr(score_mod, "load_side_tables", lambda ctx, spec: {})
    monkeypatch.setattr(
        score_mod, "_payload", lambda ctx, name, inline, b: data if name == INPUT_DATASET else None
    )


@pytest.mark.integration
@pytest.mark.filterwarnings("ignore")
def test_a_classifier_evaluate_draws_the_classification_plots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Score-page evaluate keeps /observed for its plots (it never reached the recipes
    before v0.10) and a binary classifier gets calibration, ROC/PR and confusion."""
    pytest.importorskip("mapi_sklearn")
    from mapi_sdk.harness import fit_spec
    from mapi_sdk.registry import default_registry
    from mapi_sdk.schema import infer_schema
    from mapi_sklearn.testing import demand_dataset
    from mapi_spec import load_model_spec, load_score_request
    from mapi_spec.jobs import ScoreJob

    data = demand_dataset()
    ds = data.assign(sold_out=(data["units_sold"] > data["units_sold"].median()).astype(float))
    ds = ds.drop_vars("units_sold")
    schema = infer_schema(ds, targets=["sold_out"], policies={"store": "new_levels"})
    settings = {
        "estimator": {"kind": "sklearn.LogisticRegression", "params": {"C": 0.5}},
        "rows": ["store", "sku", "time"],
        "coord_features": {"store": "onehot", "sku": "onehot"},
    }
    spec = load_model_spec(_CLASSIFIER_SPEC.replace("__SETTINGS__", json.dumps(settings)))
    reg = default_registry()
    bundle = fit_spec(spec, ds, registry=reg, raw_schema=schema)
    _stub_payload(monkeypatch, bundle, ds.isel(week=slice(-8, None)))
    request = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: model://retail/stockouts@latest\n"
        "mode: evaluate\ndata: dataset://retail/stockouts@latest\n"
    )
    ctx = _ScoreCtx(tmp_path, reg)
    tree = score_mod.run_score(ctx, ScoreJob(request=request))  # type: ignore[arg-type]
    assert "observed" not in tree and "predictions" not in tree  # the stored result
    plots = [p["id"] for p in ctx.written["plots.json"]]
    assert plots == list(BINARY_EVALUATE_PLOTS), ctx.warnings
    assert not [w for w in ctx.warnings if w[0] == "PLOT_SKIPPED"], ctx.warnings
    tables = {t["variable"]: t for t in ctx.written["summary.json"]["tables"]}
    assert tables["y_hat"]["plots"] == list(BINARY_EVALUATE_PLOTS)


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.filterwarnings("ignore")
def test_an_areal_score_draws_the_area_map(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("mapi_plugin_science")
    from mapi_plugin_science.areal import ArealBYM2Model
    from mapi_plugin_science.areal_data import AREAL_SCHEMA, AREAL_SPEC_YAML, areal_dataset
    from mapi_sdk.harness import fit_spec
    from mapi_sdk.registry import default_registry
    from mapi_spec import load_score_request
    from mapi_spec.jobs import ScoreJob

    reg = default_registry(reload=True).copy()
    reg.register_model(ArealBYM2Model, replace=True)
    yaml = AREAL_SPEC_YAML.replace("tune: 300, draws: 150", "tune: 60, draws: 40")
    yaml = yaml.replace("mode: evaluate_then_refit, ", "")
    data = areal_dataset()
    bundle = fit_spec(yaml, data, registry=reg, raw_schema=AREAL_SCHEMA)
    _stub_payload(monkeypatch, bundle, data)
    for mode in ("predict", "evaluate"):
        request = load_score_request(
            "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://epi/map\n"
            f"mode: {mode}\ndata: dataset://epidemiology/synthetic_disease_map@latest\n"
        )
        ctx = _ScoreCtx(tmp_path / mode, reg)
        score_mod.run_score(ctx, ScoreJob(request=request))  # type: ignore[arg-type]
        plots = [p["id"] for p in ctx.written["plots.json"]]
        want = ["sci.area_map"] if mode == "predict" else ["core.fit_vs_actual", "sci.area_map"]
        assert plots == want, ctx.warnings
        assert not [w for w in ctx.warnings if w[0] == "PLOT_SKIPPED"], ctx.warnings
        tables = {t["variable"]: t for t in ctx.written["summary.json"]["tables"]}
        assert tables["cases"]["plots"] == ["sci.area_map"]
