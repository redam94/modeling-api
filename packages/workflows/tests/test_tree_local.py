"""Tree datasets (M4) in workflows: ``nodes``/``join`` on model nodes and the local runner."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.harness import data_hash
from mapi_sdk.testing.toy import tree_dataset
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import ModelSpec
from mapi_spec.tree import NodeSelect
from mapi_spec.workflow import WorkflowRunStatus, validate_workflow
from mapi_workflows.runtime.local import LocalRunner, as_value
from mapi_workflows.runtime.values import DatasetValue, ModelValue
from mapi_workflows.specs import model_spec_document
from mapi_workflows.testing import test_registry, weekly_series

NODES = {"sales": {"path": "/sales"}, "media": {"path": "/media"}}
BIND = {"dims": {"geo": "market", "time": "date"}, "vars": {"y": "revenue"}}
MODEL = "toy.tree_linear@^1"  # refuses to fit without the tree (ctx.tree)


def _doc(**fit: Any) -> dict[str, Any]:
    params = {"model": MODEL, "bind": BIND, **fit}
    return {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "tree-fit", "project": "demo"},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": [
            {"id": "fit", "use": "model.fit", "with": params, "inputs": {"data": "inputs.data"}},
            {
                "id": "plan",
                "use": "split.expanding_window",
                "with": {"dim": "date", "horizons": [1, 2], "n_folds": 2},
                "inputs": {"data": "inputs.data"},
            },
            {
                "id": "bt",
                "use": "model.backtest",
                "with": params,
                "inputs": {"data": "inputs.data", "split": "plan.plan"},
            },
        ],
        "outputs": {"model": "fit.model", "metrics": "bt.metrics"},
    }


# ------------------------------------------------------------------- model_spec_document


def test_spec_document_carries_nodes_and_join() -> None:
    doc = model_spec_document(
        {"model": MODEL, "nodes": NODES, "join": "inner"},
        name="m",
        project="demo",
        dataset="dataset://demo/tree@latest",
    )
    assert doc["data"]["nodes"] == NODES and doc["data"]["join"] == "inner"
    spec = ModelSpec.model_validate(doc)
    assert spec.data.nodes == {k: NodeSelect.model_validate(v) for k, v in NODES.items()}
    exact = model_spec_document(
        {"model": MODEL, "nodes": NODES, "join": "exact"},
        name="m",
        project="demo",
        dataset="dataset://demo/tree@latest",
    )
    assert "join" not in exact["data"]
    plain = model_spec_document(
        {"model": MODEL, "join": "exact"}, name="m", project="demo", dataset="dataset://d/x@1"
    )
    assert "nodes" not in plain["data"] and "join" not in plain["data"]
    with pytest.raises(ValueError, match="join"):
        model_spec_document(
            {"model": MODEL, "join": "outer"}, name="m", project="demo", dataset="dataset://d/x@1"
        )


def test_catalog_accepts_nodes_and_join() -> None:
    res = validate_workflow(_doc(nodes=NODES, join="exact"), catalog=test_registry().catalog())
    assert res.valid, [(i.pointer, i.message) for i in res.issues]
    bad = validate_workflow(_doc(nodes=NODES, join="sideways"), catalog=test_registry().catalog())
    assert not bad.valid


# --------------------------------------------------------------------------- the values


def test_tree_input_value() -> None:
    tree = tree_dataset()
    v = as_value(tree)
    assert isinstance(v, DatasetValue) and v.tree is tree
    assert not v.data.data_vars and "date" in v.data.coords  # the pure-group root: coords
    sel = v.select({k: NodeSelect.model_validate(x) for k, x in NODES.items()})
    assert set(sel.data.data_vars) == {"revenue", "spend"} and sel.tree is tree
    assert sel.schema is not None and tuple(sel.schema.vars["revenue"].dims) == ("market", "date")
    assert v.derive(v.data).tree is tree  # transforms keep the tree
    rooted = DatasetValue.from_tree(xr.DataTree.from_dict({"/": weekly_series(20)}))
    assert set(rooted.data.data_vars) == {"sales", "price"}
    with pytest.raises(MapiError) as plain:
        DatasetValue(data=weekly_series(20)).select({"a": NodeSelect(path="/a")})
    assert plain.value.code is ErrorCode.SCHEMA_INVALID
    with pytest.raises(MapiError) as missing:
        v.select({"x": NodeSelect(path="/nope")})
    assert missing.value.pointer == "/data/nodes/x"


# --------------------------------------------------------------------------- local runs


def test_local_runner_fits_and_backtests_a_tree_dataset() -> None:
    tree = tree_dataset()
    res = LocalRunner(registry=test_registry()).run(_doc(nodes=NODES), inputs={"data": tree})
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    mv = res.values["model"]
    assert isinstance(mv, ModelValue) and mv.tree is tree
    assert mv.bundle.spec.data.nodes is not None and set(mv.bundle.spec.data.nodes) == set(NODES)
    assert mv.bundle.manifest.data_hash == data_hash(tree)
    assert "/adjust" in mv.bundle.model.learned["tree_nodes"]  # type: ignore[operator]
    assert mv.data is not None and set(mv.data.data_vars) == {"revenue", "spend"}
    metrics = res.values["metrics"]
    assert isinstance(metrics, dict) and res.node("bt").result["n_folds"] == 2  # type: ignore[index]


def test_nodes_on_a_plain_dataset_fail_the_node() -> None:
    res = LocalRunner(registry=test_registry()).run(
        _doc(nodes=NODES), inputs={"data": weekly_series(40)}
    )
    assert res.status is WorkflowRunStatus.FAILED
    err = res.node("fit").error
    assert err is not None and err.code is ErrorCode.SCHEMA_INVALID
    assert "plain dataset" in err.message


def test_unknown_node_fails_with_its_pointer() -> None:
    res = LocalRunner(registry=test_registry()).run(
        _doc(nodes={**NODES, "x": {"path": "/nope"}}), inputs={"data": tree_dataset()}
    )
    assert res.status is WorkflowRunStatus.FAILED
    err = res.node("fit").error
    assert err is not None and err.code is ErrorCode.SCHEMA_INVALID


# ------------------------------------------------------------- scoring a new tree dataset


def _score_doc() -> dict[str, Any]:
    fit = {"model": MODEL, "bind": BIND, "nodes": NODES}
    return {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "tree-score", "project": "demo"},
        "inputs": {"data": {"type": "dataset"}, "new": {"type": "dataset"}},
        "nodes": [
            {"id": "fit", "use": "model.fit", "with": fit, "inputs": {"data": "inputs.data"}},
            {
                "id": "new",
                "use": "model.score",
                "with": {},
                "inputs": {"model": "fit.model", "data": "inputs.new"},
            },
            {"id": "old", "use": "model.score", "with": {}, "inputs": {"model": "fit.model"}},
            {
                "id": "plot",
                "use": "plot.render",
                "with": {"recipe": "core.fit_vs_actual", "source": "predictions"},
                "inputs": {"model": "fit.model", "data": "inputs.new"},
            },
        ],
        "outputs": {"new": "new.scores", "old": "old.scores", "plot": "plot.plot"},
    }


def test_score_uses_the_tree_of_the_scored_dataset() -> None:
    # the model adds /adjust offset(market): B's offsets are 100 above A's
    a, b = tree_dataset(), tree_dataset(offset=(105.0, 95.0, 100.0))
    res = LocalRunner(registry=test_registry()).run(_score_doc(), inputs={"data": a, "new": b})
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    new, old = res.values["new"], res.values["old"]
    assert isinstance(new, DatasetValue) and isinstance(old, DatasetValue)
    diff = (new.data["y_hat"] - old.data["y_hat"]).values
    assert np.allclose(diff, 100.0)
    plot = res.node("plot").result
    assert plot is not None and plot["source"] == "predictions"


def test_plot_data_keeps_the_groups_of_a_tree_input() -> None:
    from mapi_workflows.runtime.plots import _data_tree

    tree = tree_dataset()
    v = as_value(tree)
    assert isinstance(v, DatasetValue)
    assert _data_tree(v) is tree
    derived = v.derive(weekly_series(10))
    assert set(_data_tree(derived).children) == set()
