"""Binary classification recipes: ROC/PR curves, the confusion matrix and their input lookup."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.recipes import RecipeDataError, RecipeInput, as_tree, get_recipe  # noqa: E402
from mapi_plots.recipes.classification import binary_auc, binary_scores  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402
from mapi_spec.plots import PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

IDS = ["core.roc_pr", "core.confusion"]


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


def check(plot: PlotSpec, rid: str) -> None:
    assert plot.kind == "vega-lite" and plot.id == rid
    assert len(plot.caption_plain or "") > 40
    assert '"url"' not in json.dumps(plot.spec)
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []


def _point(n: int = 200, seed: int = 0) -> dict[str, Any]:
    """A scikit-learn-like score: ``predictions.y_hat(row)`` = P(1), ``observed.y(row)``."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(size=n)
    y = (rng.uniform(size=n) < p).astype(float)
    return {
        "predictions": xr.Dataset({"y_hat": (("row",), p)}),
        "observed": xr.Dataset({"y": (("row",), y)}),
    }


def test_auc_by_ranks_with_ties() -> None:
    p = np.array([0.1, 0.4, 0.35, 0.8])
    y = np.array([0.0, 0.0, 1.0, 1.0])
    assert binary_auc(p, y) == pytest.approx(0.75)
    assert binary_auc(np.array([0.5, 0.5]), np.array([0.0, 1.0])) == pytest.approx(0.5)
    assert binary_auc(np.array([0.1, 0.9]), np.array([0.0, 1.0])) == 1.0
    assert binary_auc(np.array([0.1, 0.9]), np.array([1.0, 1.0])) is None


@pytest.mark.parametrize("rid", IDS)
def test_example_renders(rid: str) -> None:
    data, options = ex.example_inputs()[rid]
    plot = get_recipe(rid).render(data, **options)
    check(plot, rid)
    reps = get_recipe(rid).catalog_entry().requires_representation
    assert [r.value for r in reps] == ["draws", "point"]


def test_binary_scores_sources() -> None:
    draws = binary_scores(RecipeInput(as_tree(ex.classifier_tree())), None, None, None)
    assert draws.source == "draws" and draws.draws is not None
    assert draws.var == "p" and draws.observed == "churn" and draws.positive == 1.0
    assert np.allclose(draws.p, draws.draws.mean(axis=0))
    point = binary_scores(RecipeInput(as_tree(_point())), None, None, None)
    assert point.source == "point" and point.draws is None and point.p.shape == (200,)
    tree = _point()
    summary = xr.Dataset(
        {"y_hat": (("stat", "row"), np.stack([tree["predictions"]["y_hat"].values] * 2))},
        coords={"stat": ["sd", "mean"]},
    )
    got = binary_scores(
        RecipeInput(as_tree({"summary": summary, "observed": tree["observed"]})), None, None, None
    )
    assert got.source == "summary" and np.allclose(got.p, tree["predictions"]["y_hat"].values)


def test_positive_class_maps_labels_and_nan_rows_drop() -> None:
    tree = _point(50, 1)
    y = tree["observed"]["y"].values * 5 + 2  # classes 2 and 7
    y[3] = np.nan
    tree["observed"] = xr.Dataset({"y": (("row",), y)})
    sc = binary_scores(RecipeInput(as_tree(tree)), None, None, None)
    assert sc.positive == 7.0 and set(np.unique(sc.y)) <= {0.0, 1.0} and sc.y.size == 49
    low = binary_scores(RecipeInput(as_tree(tree)), None, None, 2.0)
    assert np.allclose(low.y, 1 - sc.y)
    with pytest.raises(RecipeDataError, match="positive=3"):
        binary_scores(RecipeInput(as_tree(tree)), None, None, 3.0)


def test_roc_pr_point_probabilities() -> None:
    plot = get_recipe("core.roc_pr").render(_point())
    check(plot, "core.roc_pr")
    roc, pr = frame(plot, "roc"), frame(plot, "pr")
    assert roc["fpr"].iloc[0] == 0 and roc["tpr"].iloc[-1] == pytest.approx(1.0)
    assert (np.diff(roc["fpr"]) >= 0).all() and (np.diff(roc["tpr"]) >= 0).all()
    assert pr["precision"].between(0, 1).all()
    cap = plot.caption_plain or ""
    assert "The AUC is" in cap and "interval of the AUC" not in cap
    perfect = _point()
    perfect["predictions"] = xr.Dataset({"y_hat": (("row",), perfect["observed"]["y"].values)})
    best = get_recipe("core.roc_pr").render(perfect)
    assert "AUC is 1.000: the model separates the classes very well" in (best.caption_plain or "")


def test_roc_pr_draws_interval_and_thinning() -> None:
    plot = get_recipe("core.roc_pr").render(ex.classifier_tree(), max_points=20)
    assert len(frame(plot, "roc")) <= 22
    assert "90% interval of the AUC" in (plot.caption_plain or "")


def test_roc_pr_errors() -> None:
    rng = np.random.default_rng(3)
    cont = {
        "predictions": xr.Dataset({"y_hat": (("row",), rng.uniform(size=30))}),
        "observed": xr.Dataset({"y": (("row",), rng.normal(size=30))}),
    }
    with pytest.raises(RecipeDataError, match="not binary"):
        get_recipe("core.roc_pr").render(cont)
    big = _point()
    big["predictions"] = big["predictions"] * 10
    with pytest.raises(RecipeDataError):  # point values outside [0, 1]: not a probability
        get_recipe("core.roc_pr").render(big)
    with pytest.raises(RecipeDataError, match="not probabilities"):
        get_recipe("core.roc_pr").render(big, var="y_hat")
    one = _point()
    one["observed"] = xr.Dataset({"y": (("row",), np.ones(200))})
    with pytest.raises(RecipeDataError, match="only one class"):
        get_recipe("core.roc_pr").render(one)
    with pytest.raises(RecipeDataError, match="no predicted variable"):
        get_recipe("core.roc_pr").render(_point(), var="nope")


def test_confusion_counts_rates_and_auto_threshold() -> None:
    tree = _point(300, 2)
    plot = get_recipe("core.confusion").render(tree)
    check(plot, "core.confusion")
    m = frame(plot, "matrix")
    assert m["count"].sum() == 300 and len(m) == 4
    p = tree["predictions"]["y_hat"].values
    y = tree["observed"]["y"].values
    tp = m[(m["observed"] == "1") & (m["predicted"] == "1")]["count"].item()
    assert tp == int(np.sum((p >= 0.5) & (y == 1)))
    assert "0.5 or more" in (plot.caption_plain or "")
    rates = get_recipe("core.confusion").render(tree, normalize="true")
    r = frame(rates, "matrix")
    assert np.allclose(r.groupby("observed")["value"].sum(), 1.0)
    auto = get_recipe("core.confusion").render(tree, threshold="auto")
    assert "largest difference" in (auto.caption_plain or "")
    with pytest.raises(RecipeDataError):
        get_recipe("core.confusion").render(tree, threshold=1.5)
