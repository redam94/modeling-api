"""Multiclass classification (v0.14): class_scores, one-vs-rest ROC/PR curves per class and the
K x K confusion matrix; binary inputs keep the binary layout."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.recipes import RecipeDataError, RecipeInput, as_tree, get_recipe  # noqa: E402
from mapi_plots.recipes.classification import binary_auc, class_scores  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402
from mapi_spec.plots import PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


def check(plot: PlotSpec, rid: str) -> None:
    assert plot.kind == "vega-lite" and plot.id == rid
    assert len(plot.caption_plain or "") > 40
    assert '"url"' not in json.dumps(plot.spec)
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []


def _numeric_labels(tree: xr.DataTree) -> xr.DataTree:
    """The same tree with integer class labels (``0..K-1``) in coords and observations."""
    pred = tree["predictions"].to_dataset()
    obs = tree["observed_data"].to_dataset()
    classes = [str(c) for c in pred["class"].values]
    codes = np.array([classes.index(v) for v in obs["label"].values], dtype=float)
    pred = pred.assign_coords({"class": [str(i) for i in range(len(classes))]})
    return xr.DataTree.from_dict(
        {
            "/predictions": pred,
            "/observed_data": xr.Dataset({"label": (("row",), codes)}, {"row": obs["row"].values}),
        }
    )


def test_class_scores_draws_and_point() -> None:
    sc = class_scores(RecipeInput(as_tree(ex.multiclass_tree())))
    assert sc is not None and sc.source == "draws" and sc.category == "class"
    assert sc.classes == ["bird", "cat", "dog", "fish"]
    assert sc.p.shape == (240, 4) and sc.draws is not None and sc.draws.shape == (80, 240, 4)
    np.testing.assert_allclose(sc.p.sum(axis=1), 1.0)
    assert sc.var == "y_hat" and sc.observed == "label" and sc.codes.max() == 3
    point = class_scores(RecipeInput(as_tree(ex.multiclass_tree(with_draws=False))))
    assert point is not None and point.source == "point" and point.draws is None
    numeric = class_scores(RecipeInput(as_tree(_numeric_labels(ex.multiclass_tree(False)))))
    assert numeric is not None and numeric.classes == ["0", "1", "2", "3"]
    np.testing.assert_array_equal(numeric.codes, point.codes)


def test_binary_inputs_are_not_multiclass() -> None:
    assert class_scores(RecipeInput(as_tree(ex.classifier_tree()))) is None
    no_meaning = ex.multiclass_tree(False)
    pred = no_meaning["predictions"].to_dataset()
    pred["y_hat"].attrs.clear()
    plain = xr.DataTree.from_dict(
        {"/predictions": pred, "/observed_data": no_meaning["observed_data"].to_dataset()}
    )
    assert class_scores(RecipeInput(as_tree(plain))) is None


def test_unmatched_and_missing_labels_are_left_out() -> None:
    tree = ex.multiclass_tree(False, n=50)
    obs = tree["observed_data"].to_dataset()
    labels = obs["label"].values.astype(object)
    labels[0], labels[1] = "", "zebra"
    data = {
        "/predictions": tree["predictions"].to_dataset(),
        "/observed_data": xr.Dataset({"label": (("row",), labels.astype(str))}, obs.coords),
    }
    sc = class_scores(RecipeInput(as_tree(xr.DataTree.from_dict(data))))
    assert sc is not None and sc.codes.size == 48 and sc.unmatched == 1
    plot = get_recipe("core.confusion").render(xr.DataTree.from_dict(data))
    assert "1 observations have a label that is not a class" in (plot.caption_plain or "")


@pytest.mark.parametrize("with_draws", [True, False])
def test_roc_pr_one_curve_per_class(with_draws: bool) -> None:
    tree = ex.multiclass_tree(with_draws)
    plot = get_recipe("core.roc_pr").render(tree, max_points=40)
    check(plot, "core.roc_pr")
    roc, pr = frame(plot, "roc"), frame(plot, "pr")
    assert sorted(roc["class"].unique()) == ["bird", "cat", "dog", "fish"]
    assert set(pr["class"]) == set(roc["class"])
    for _, g in roc.groupby("class"):
        assert g["fpr"].iloc[0] == 0 and g["tpr"].iloc[-1] == pytest.approx(1.0)
        assert len(g) <= 42
    sc = class_scores(RecipeInput(as_tree(tree)))
    assert sc is not None
    macro = np.mean([binary_auc(sc.p[:, j], (sc.codes == j).astype(float)) for j in range(4)])
    cap = plot.caption_plain or ""
    assert f"macro AUC (the mean over 4 classes) is {macro:.3f}" in cap
    assert f"macro AUC {macro:.3f}" in json.dumps(plot.spec)
    assert ("interval of the macro AUC" in cap) is with_draws


def test_roc_pr_class_subset_and_many_classes() -> None:
    tree = ex.multiclass_tree(False)
    plot = get_recipe("core.roc_pr").render(tree, classes=["dog", "cat"])
    assert sorted(frame(plot, "roc")["class"].unique()) == ["cat", "dog"]
    assert "2 of the 4 classes." in (plot.caption_plain or "")
    with pytest.raises(RecipeDataError, match="not classes"):
        get_recipe("core.roc_pr").render(tree, classes=["horse"])
    many = ex.multiclass_tree(False, n_classes=15, n=600)
    plot = get_recipe("core.roc_pr").render(many, positive=1)
    assert frame(plot, "roc")["class"].nunique() == 12
    cap = plot.caption_plain or ""
    assert "12 of the 15 classes (the most frequent)" in cap and "ignores them" in cap


def test_roc_pr_needs_two_observed_classes() -> None:
    tree = ex.multiclass_tree(False, n=30)
    obs = tree["observed_data"].to_dataset()
    one = xr.DataTree.from_dict(
        {
            "/predictions": tree["predictions"].to_dataset(),
            "/observed_data": obs.assign(label=("row", np.full(30, "dog"))),
        }
    )
    with pytest.raises(RecipeDataError, match="fewer than two classes"):
        get_recipe("core.roc_pr").render(one)


def test_confusion_k_by_k_on_the_most_probable_class() -> None:
    tree = ex.multiclass_tree(False)
    plot = get_recipe("core.confusion").render(tree)
    check(plot, "core.confusion")
    m = frame(plot, "matrix")
    assert len(m) == 16 and m["count"].sum() == 240
    sc = class_scores(RecipeInput(as_tree(tree)))
    assert sc is not None
    pred = np.argmax(sc.p, axis=1)
    hits = int(m[m["correct"]]["count"].sum())
    assert hits == int(np.sum(pred == sc.codes))
    cap = plot.caption_plain or ""
    assert f"Accuracy {hits / 240 * 100:.0f}%" in cap and "macro F1" in cap
    assert "ignores" not in cap
    rates = frame(get_recipe("core.confusion").render(tree, normalize="true"), "matrix")
    assert np.allclose(rates.groupby("observed")["value"].sum(), 1.0)
    noted = get_recipe("core.confusion").render(tree, threshold=0.3)
    assert "threshold settings apply to two classes only" in (noted.caption_plain or "")


def test_confusion_groups_classes_beyond_thirty() -> None:
    many = ex.multiclass_tree(False, n_classes=34, n=1000)
    plot = get_recipe("core.confusion").render(many)
    m = frame(plot, "matrix")
    assert m["observed"].nunique() == 30 and "(other)" in set(m["observed"])
    assert m["count"].sum() == 1000
    assert "(other) holds the other 5" in (plot.caption_plain or "")


def test_binary_layout_is_unchanged() -> None:
    plot = get_recipe("core.confusion").render(ex.classifier_tree(), threshold="auto")
    assert len(frame(plot, "matrix")) == 4
    roc = frame(get_recipe("core.roc_pr").render(ex.classifier_tree()), "roc")
    assert "class" not in roc.columns
