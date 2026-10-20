"""Score plots by the shape of each output (:mod:`mapi_runner.worker.score_plots`): a map
with a frame slider for grid outputs, a fan for an output over time, the mode's defaults for
models without grids; the KPI of a gridded model, the summary tables' dims, and options
reaching the recipes.

The bundles are fakes (a manifest, a raw schema and a binding), so the unit tests need no
plugin and no fit; the last test fits the earthquake LGCP (``sci.lgcp_grid``) and scores it.
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
    DEFAULT_PLOTS,
    ScorePlots,
    default_score_plots,
    field_plot_tree,
    has_grid_outputs,
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
_TO_DATASET = {"period": "quarter", "count": "quakes"}
_TO_CANON = {v: k for k, v in _TO_DATASET.items()}


def _bundle(outputs: dict[str, dict[str, Any]], targets: list[str] | None = None) -> Any:
    manifest = SimpleNamespace(
        outputs={k: OutputDecl.model_validate(v) for k, v in outputs.items()}
    )
    return SimpleNamespace(
        model=SimpleNamespace(manifest=manifest),
        raw_schema=RAW,
        binding=SimpleNamespace(
            to_dataset=lambda n: _TO_DATASET.get(n, n),
            to_canonical=lambda n: _TO_CANON.get(n, n),
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
    b = _bundle({"y_hat": {"dims": ["time"], "units_of": "y"}}, ["y"])
    assert not has_grid_outputs(b)
    tree = xr.DataTree.from_dict({"/summary": xr.Dataset({"y_hat": (("stat", "time"), [[1.0]])})})
    assert default_score_plots(b, mode, tree) == ScorePlots(DEFAULT_PLOTS[mode])


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
