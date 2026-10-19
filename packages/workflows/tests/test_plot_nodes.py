"""M5: ``model.score``, ``plot.render``, the plot outputs of ``model.forecast`` and
``evaluate.residuals``, and ``plot`` sections in ``report.summary``."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plots import PlotSpec
from mapi_spec.workflow import (
    NODE_RESULT_MAX_BYTES,
    NodeOutputRef,
    NodeResultSidecar,
    WorkflowNode,
    WorkflowNodeJob,
    WorkflowRunStatus,
    load_workflow,
    validate_workflow,
)
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, PortType
from mapi_workflows.orchestrator import (
    MAX_REPORT_PLOT,
    BoundValue,
    OrchestratorContext,
    report_summary,
)
from mapi_workflows.payloads import ReportPayload
from mapi_workflows.runtime import plots as plots_mod
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.runtime.sandbox import MAX_INLINE_OUTPUT, MAX_INLINE_PLOT, output_files
from mapi_workflows.runtime.values import DatasetValue, ModelValue, NodeOutcome
from wf_ctx import run_node
from wf_models import registry, weekly_series

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def fitted() -> tuple[Any, ModelValue]:
    reg = registry()
    out = run_node(
        "model.fit",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0], "exog": ["price"]}},
        data=weekly_series(80),
        registry=reg,
    )
    mv = out.outputs["model"]
    assert isinstance(mv, ModelValue)
    return reg, mv


def _plot(value: Any) -> PlotSpec:
    assert isinstance(value, dict)
    return PlotSpec.model_validate(value)


def field_draws() -> xr.Dataset:
    rng = np.random.default_rng(0)
    return xr.Dataset(
        {"u": (("chain", "draw", "y", "x"), rng.normal(size=(2, 10, 6, 7)))},
        coords={"chain": [0, 1], "draw": np.arange(10), "y": np.arange(6), "x": np.arange(7)},
    )


# ================================================================================ catalog


def test_the_nodes_are_in_the_catalog() -> None:
    score = NODE_IMPLEMENTATIONS["model.score"]
    assert score.runs_in == "sandbox" and score.outputs == {"scores": PortType.DATASET}
    assert {"mode", "n_vars"} <= set(score.result_fields)
    plot = NODE_IMPLEMENTATIONS["plot.render"]
    assert plot.kind.value == "plot" and plot.outputs == {"plot": PortType.PLOT}
    assert set(plot.inputs) == {"model", "data"}
    assert not any(p.required for p in plot.inputs.values())
    report = NODE_IMPLEMENTATIONS["report.summary"].inputs["plots"]
    assert report.type is PortType.PLOT and report.multiple and not report.required


# ============================================================================ model.score


def test_score_predict_and_forecast(fitted: tuple[Any, ModelValue]) -> None:
    reg, mv = fitted
    pred = run_node("model.score", {"mode": "predict"}, model=mv, registry=reg)
    ds = pred.outputs["scores"]
    assert isinstance(ds, DatasetValue)
    # a quantile model: the summary (a stat dim) is the default output
    assert pred.result["group"] == "summary" and "stat" in ds.data.dims
    assert ds.data.sizes["time"] == 80 and pred.result["n_vars"] == len(ds.data.data_vars)
    assert ds.data.attrs["mode"] == "predict"
    fut = weekly_series(86).isel(time=slice(80, 86)).drop_vars("sales")
    fc = run_node(
        "model.score",
        {
            "mode": "forecast",
            "request": {"future_covariates": "carry_last"},
            "group": "predictions",
        },
        model=mv,
        data=DatasetValue(data=fut),
        registry=reg,
    )
    out = fc.outputs["scores"]
    assert isinstance(out, DatasetValue)
    assert fc.result["mode"] == "forecast" and fc.result["group"] == "predictions"
    np.testing.assert_array_equal(out.data["time"].values, fut["time"].values)


def test_score_refusals(fitted: tuple[Any, ModelValue]) -> None:
    reg, mv = fitted
    with pytest.raises(MapiError) as err:
        run_node("model.score", {"mode": "simulate"}, model=mv, registry=reg)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    with pytest.raises(MapiError) as err:
        run_node("model.score", {"mode": "predict", "request": {"seed": -1}}, model=mv,
                 registry=reg)  # fmt: skip
    assert err.value.code is ErrorCode.WORKFLOW_INVALID
    with pytest.raises(MapiError) as err:
        run_node("model.score", {"group": "nope"}, model=mv, registry=reg)
    assert "nope" in err.value.detail


# ============================================================================ plot.render


def test_render_any_recipe_from_a_dataset() -> None:
    out = run_node("plot.render", {"recipe": "sci.field_map", "title": "Field"},
                   data=field_draws())  # fmt: skip
    spec = _plot(out.outputs["plot"])
    assert spec.kind == "plotly" and spec.title == "Field"
    assert all(d.kind == "inline_arrow" for d in spec.datasets.values())
    assert out.result["recipe"] == "sci.field_map@1.0.0" and out.result["source"] == "data"
    assert out.result["bytes"] == len(json.dumps(out.outputs["plot"], separators=(",", ":")))


def test_render_from_a_model_and_from_its_scores(fitted: tuple[Any, ModelValue]) -> None:
    reg, mv = fitted
    # the bundle has no predictions: the node falls back to the in-sample predictions
    fan = run_node("plot.render", {"recipe": "core.fit_vs_actual"}, model=mv, registry=reg)
    assert fan.result["source"] == "predictions"
    _plot(fan.outputs["plot"])
    scores = run_node("model.score", {}, model=mv, registry=reg).outputs["scores"]
    out = run_node("plot.render", {"recipe": "core.ppc_fan@^1"}, data=scores, registry=reg)
    assert out.result["source"] == "data" and _plot(out.outputs["plot"]).id == "core.ppc_fan"
    with pytest.raises(MapiError) as err:  # restricted to the data, which has no draws
        run_node("plot.render", {"recipe": "sci.field_map", "source": "data"}, data=scores,
                 model=mv, registry=reg)  # fmt: skip
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.untrusted_text


def test_render_refusals() -> None:
    with pytest.raises(MapiError) as err:
        run_node("plot.render", {"recipe": "core.no_such_plot"}, data=field_draws())
    assert err.value.code is ErrorCode.REF_UNRESOLVED
    with pytest.raises(MapiError) as err:
        run_node("plot.render", {"recipe": "sci.field_map"})
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(MapiError) as err:
        run_node("plot.render", {"recipe": "sci.field_map", "source": "model"}, data=field_draws())
    assert "model" in err.value.detail


def test_render_caps_the_plot_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plots_mod, "MAX_PLOT_BYTES", 1000)
    with pytest.raises(MapiError) as err:
        run_node("plot.render", {"recipe": "sci.field_map"}, data=field_draws())
    assert err.value.code is ErrorCode.PAYLOAD_TOO_LARGE


def test_render_a_registry_recipe() -> None:
    """Recipes resolve through the node's registry first: plugin recipes work too."""
    import altair as alt
    from mapi_plots.recipes._base import Figure, Recipe, RecipeSettings
    from mapi_sdk.registry import Registry

    def draw(inp: Any, s: RecipeSettings) -> Figure:
        ds = inp.find("posterior")
        assert ds is not None
        frame = pd.DataFrame({"v": np.asarray(ds["u"].values).ravel()[:50]})
        chart = alt.Chart(alt.NamedData("vals")).mark_tick().encode(x="v:Q")
        return Figure(chart=chart, datasets={"vals": frame}, caption="Values.")

    rec = Recipe(id="test.ticks", title="Ticks", settings=RecipeSettings, fn=draw)
    reg = Registry()
    reg.register_plot(rec)
    out = run_node("plot.render", {"recipe": "test.ticks"}, data=field_draws(), registry=reg)
    assert _plot(out.outputs["plot"]).id == "test.ticks"
    with pytest.raises(MapiError):  # not bundled and no registry has it
        run_node("plot.render", {"recipe": "test.ticks"}, data=field_draws())


# ================================================================= forecast and residuals


def test_forecast_and_residuals_draw_their_plots(fitted: tuple[Any, ModelValue]) -> None:
    reg, mv = fitted
    fut = weekly_series(86).isel(time=slice(80, 86)).drop_vars("sales")
    fc = run_node("model.forecast", {"horizon": 6}, model=mv, data=DatasetValue(data=fut),
                  registry=reg)  # fmt: skip
    assert _plot(fc.outputs["plot"]).id == plots_mod.FORECAST_PLOT
    res = run_node("evaluate.residuals", model=mv, registry=reg)
    assert _plot(res.outputs["plot"]).id == plots_mod.RESIDUALS_PLOT
    assert not res.warnings_plain


def test_a_side_plot_that_fails_is_a_warning(fitted: tuple[Any, ModelValue],
                                             monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    reg, mv = fitted
    monkeypatch.setattr(plots_mod, "RESIDUALS_PLOT", "core.no_such_plot")
    res = run_node("evaluate.residuals", model=mv, registry=reg)
    assert "plot" not in res.outputs and "table" in res.outputs
    assert res.warnings_plain and "not drawn" in res.warnings_plain[0]


# ================================================================================= report


def _ctx() -> OrchestratorContext:
    node = WorkflowNode.model_validate({"id": "rep", "use": "report.summary"})
    return OrchestratorContext(node_id="rep", node=node, registry=NODE_IMPLEMENTATIONS)


def test_report_plot_sections() -> None:
    small = run_node("plot.render", {"recipe": "sci.field_map"}, data=field_draws())
    plot = small.outputs["plot"]
    assert isinstance(plot, dict)
    big = {**plot, "caption_plain": "Big.", "spec": {**plot["spec"], "pad": "x" * MAX_REPORT_PLOT}}
    values = [
        BoundValue(ref=NodeOutputRef(type=PortType.PLOT, inline=plot), source="a"),
        BoundValue(ref=NodeOutputRef(type=PortType.PLOT, inline=big), source="b"),
        BoundValue(ref=NodeOutputRef(type=PortType.PLOT, object="out/plot.json"), source="c"),
    ]
    result, outs = report_summary({}, {"plots": values}, _ctx())
    report = ReportPayload.model_validate(outs["report"].inline)
    secs = [s for s in report.sections if s.kind == "plot"]
    assert [s.node_id for s in secs] == ["a", "b", "c"]
    assert secs[0].plot == plot and secs[0].text == plot.get("caption_plain")
    assert secs[1].plot is None and "too large" in secs[1].text and secs[1].text.startswith("Big.")
    assert secs[2].plot is None and "in a file" in secs[2].text
    assert result["n_sections"] == 3


WORKFLOW = """
apiVersion: mapi/v1
kind: Workflow
metadata: {name: plots, project: default}
inputs:
  data: {type: dataset}
nodes:
  - id: fit
    use: model.fit
    with: {model: "ts.sarimax@^1", settings: {order: [1, 0, 0]}}
    inputs: {data: inputs.data}
  - id: scores
    use: model.score
    inputs: {model: fit.model}
  - id: fan
    use: plot.render
    label: Fit and forecast
    with: {recipe: core.ppc_fan}
    inputs: {data: scores.scores}
  - id: fc
    use: model.forecast
    with: {horizon: 4}
    inputs: {model: fit.model}
  - id: report
    use: report.summary
    with: {title: Plots}
    inputs: {plots: [fan.plot, fc.plot]}
outputs:
  report: report.report
"""


def test_a_workflow_puts_its_plots_in_the_report() -> None:
    spec = load_workflow(WORKFLOW)
    reg = registry()
    v = validate_workflow(spec, catalog=reg.catalog())
    assert v.valid, v.issues
    res = LocalRunner(registry=reg).run(spec, inputs={"data": weekly_series(80)})
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    assert res.node("fan").result["recipe"] == "core.ppc_fan@1.0.0"  # type: ignore[index]
    report = ReportPayload.model_validate(res.outputs["report"].inline)
    plots = [s for s in report.sections if s.kind == "plot"]
    assert [s.node_id for s in plots] == ["fan", "fc"]
    assert plots[0].title.startswith("Fit and forecast") and plots[0].plot is not None
    assert all(PlotSpec.model_validate(s.plot) for s in plots)


# ================================================================================ sandbox


def _job(use: str) -> WorkflowNodeJob:
    return WorkflowNodeJob(
        workflow_run_id="run_01jaf3q2x8m9v7k5h4g3f2e1d0", node_id="n", use=use, params={}
    )


def test_the_sandbox_inlines_plots_up_to_their_cap() -> None:
    small = {"kind": "plotly", "pad": "x" * (MAX_INLINE_OUTPUT + 10)}
    side, files = output_files(_job("plot.render@1.0.0"), NodeOutcome({}, {"plot": small}))
    assert side.outputs["plot"].inline == small and not files
    big = {"kind": "plotly", "pad": "x" * (MAX_INLINE_PLOT + 10)}
    side, files = output_files(_job("plot.render@1.0.0"), NodeOutcome({}, {"plot": big}))
    assert side.outputs["plot"].object == "out/plot.json" and files[0].name == "out/plot.json"
    # the sidecar stays under its cap: a second large output goes to a file
    two: dict[str, Any] = {
        "table": {"pad": "y" * (MAX_INLINE_OUTPUT - 100)},
        "plot": {"pad": "x" * 200_000},
    }
    side, files = output_files(_job("evaluate.residuals@1.0.0"), NodeOutcome({}, two))
    assert len(side.model_dump_json()) <= NODE_RESULT_MAX_BYTES
    assert {f.name for f in files} == {"out/plot.json"}
    NodeResultSidecar.model_validate_json(side.model_dump_json())


def test_the_sandbox_writes_score_datasets(fitted: tuple[Any, ModelValue]) -> None:
    reg, mv = fitted
    out = run_node("model.score", {"group": "predictions"}, model=mv, registry=reg)
    side, files = output_files(_job("model.score@1.0.0"), out)
    assert side.outputs["scores"].object == "out/scores/data.zarr.zip"
    assert "out/scores/schema.json" in {f.name for f in files}


def test_validation_resolves_the_recipe_of_plot_render() -> None:
    from mapi_workflows.testing import test_registry

    def doc(recipe: str) -> dict[str, Any]:
        return {
            "apiVersion": "mapi/v1",
            "kind": "Workflow",
            "metadata": {"name": "p", "project": "demo"},
            "inputs": {"data": {"type": "dataset"}},
            "nodes": [
                {
                    "id": "p",
                    "use": "plot.render",
                    "with": {"recipe": recipe},
                    "inputs": {"data": "inputs.data"},
                }
            ],
        }

    catalog = test_registry().catalog()
    for good in ("core.ppc_fan", "sci.field_map@^1"):
        assert validate_workflow(doc(good), catalog=catalog).valid
    for bad in ("core.no_such_recipe", "ts.nope@^1"):
        res = validate_workflow(doc(bad), catalog=catalog)
        assert not res.valid
        assert [(i.code, i.pointer) for i in res.issues] == [
            (ErrorCode.REF_UNRESOLVED, "/nodes/0/with/recipe")
        ]
    assert validate_workflow(doc("core.no_such_recipe")).valid  # no catalog: no check
