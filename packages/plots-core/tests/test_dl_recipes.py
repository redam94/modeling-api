"""Deep-learning training recipes: training curves, confusion over epochs, wrong images."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")
pytest.importorskip("plotly")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.packs.ml import ml_recipes  # noqa: E402
from mapi_plots.packs.ml_training import PIXEL_NODE_BUDGET  # noqa: E402
from mapi_plots.recipes import RecipeDataError, all_recipes, get_recipe  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402
from mapi_spec.plots import PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

RECIPES = ("ml.training_curves", "ml.confusion_epochs", "ml.misclassified")
# what the static renderer (runner/worker/render.py) treats as a network fetch
_FETCH_KEYS = {"url", "href", "src", "source", "images", "mapbox", "map"}


def fetching_keys(node: Any, path: str = "") -> list[str]:
    """JSON pointers of keys that could make a renderer load a resource."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}/{key}"
            if key in _FETCH_KEYS:
                found.append(here)
            found += fetching_keys(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += fetching_keys(value, f"{path}/{i}")
    return found


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


def check(plot: PlotSpec, rid: str) -> None:
    assert plot.id == rid
    assert len(plot.caption_plain or "") > 40
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []
    assert fetching_keys(plot.spec) == []


def test_recipes_are_in_the_ml_pack_and_the_catalog() -> None:
    ids = {r.id for r in ml_recipes()}
    assert set(RECIPES) <= ids
    assert set(RECIPES) <= {r.id for r in all_recipes()}
    assert set(RECIPES) <= set(ex.example_inputs())


@pytest.mark.parametrize("rid", RECIPES)
def test_example_renders_and_survives_the_sanitizer(rid: str) -> None:
    data, options = ex.example_inputs()[rid]
    plot = get_recipe(rid).render(data, **options)
    check(plot, rid)
    assert plot.kind == ("plotly" if rid == "ml.misclassified" else "vega-lite")


# --------------------------------------------------------------------- training curves


def test_training_curves_by_label_from_the_history() -> None:
    plot = get_recipe("ml.training_curves").render(ex.training_tree())
    curves = frame(plot, "curves")
    assert set(curves["key"]) == {"loss", "accuracy"}
    assert set(curves["label"]) == {"train", "val"}
    # validation points at each epoch end only; train at every step
    val = curves[(curves["key"] == "loss") & (curves["label"] == "val")]
    assert len(val) == 10 and val["point"].all()
    train = curves[(curves["key"] == "loss") & (curves["label"] == "train")]
    assert len(train) == 400 and not train["point"].any()
    cap = plot.caption_plain or ""
    assert "loss and accuracy" in cap and "train and val" in cap
    assert "The best validation loss is" in cap and "The best validation accuracy is" in cap
    assert "starts to overfit" in cap
    assert "weights of epoch 7" in cap


def test_no_overfit_sentence_when_validation_keeps_falling() -> None:
    plot = get_recipe("ml.training_curves").render(ex.training_tree(overfit=False))
    assert "overfit" not in (plot.caption_plain or "")


def test_training_curves_x_epoch_maps_steps_to_epochs() -> None:
    plot = get_recipe("ml.training_curves").render(ex.training_tree(), x="epoch", keys=["loss"])
    curves = frame(plot, "curves")
    assert set(curves["key"]) == {"loss"}
    val = curves[curves["label"] == "val"]
    assert val["x"].tolist() == pytest.approx(list(range(1, 11)))
    assert curves["x"].max() == pytest.approx(10.0)
    assert "at each epoch" in (plot.caption_plain or "")


def test_training_curves_fall_back_to_the_training_group() -> None:
    tree = ex.training_tree()
    only = xr.DataTree.from_dict({"/model/training": tree["model/training"].to_dataset()})
    plot = get_recipe("ml.training_curves").render(only)
    check(plot, "ml.training_curves")
    curves = frame(plot, "curves")
    assert set(curves["label"]) == {"train", "val"}
    assert curves["x"].max() == 10
    assert "the x axis is the epoch" in (plot.caption_plain or "")


def test_training_curves_unknown_key_and_no_data() -> None:
    with pytest.raises(RecipeDataError, match="no metric 'f1'"):
        get_recipe("ml.training_curves").render(ex.training_tree(), keys=["f1"])
    with pytest.raises(RecipeDataError, match="neither a metrics_history nor a training"):
        get_recipe("ml.training_curves").render(xr.Dataset({"a": (("i",), [1.0])}))


def test_training_curves_label_dim_is_a_setting() -> None:
    tree = ex.training_tree()
    hist = tree["model/metrics_history"].to_dataset().rename({"split": "phase"})
    plot = get_recipe("ml.training_curves").render({"metrics_history": hist}, label="phase")
    assert set(frame(plot, "curves")["label"]) == {"train", "val"}
    assert "for each phase" in (plot.caption_plain or "")


# ---------------------------------------------------------------- confusion over epochs


def test_confusion_epochs_panels_are_first_best_last_and_rows_normalised() -> None:
    plot = get_recipe("ml.confusion_epochs").render(ex.training_tree())
    m = frame(plot, "matrix")
    epochs = sorted(set(m["epoch"]))
    assert len(epochs) <= 6 and {1, 7, 10} <= set(epochs)
    assert any("(best)" in p for p in m["panel"])
    sums = m.groupby(["epoch", "true"])["value"].sum()
    assert np.allclose(sums.to_numpy(), 1.0)
    cap = plot.caption_plain or ""
    assert "at epoch 1" in cap and "the best epoch (7)" in cap and "at epoch 10" in cap


def test_confusion_epochs_explicit_epochs_and_counts() -> None:
    plot = get_recipe("ml.confusion_epochs").render(
        ex.training_tree(), epochs=[2, 4, 99], normalize="none"
    )
    m = frame(plot, "matrix")
    assert sorted(set(m["epoch"])) == [2, 4]
    assert m.groupby("epoch")["value"].sum().tolist() == [60.0, 60.0]
    with pytest.raises(RecipeDataError, match="none of the epochs"):
        get_recipe("ml.confusion_epochs").render(ex.training_tree(), epochs=[99])


def test_confusion_epochs_caps_the_classes() -> None:
    rng = np.random.default_rng(0)
    k, e = 8, 3
    classes = [f"c{i}" for i in range(k)]
    counts = rng.integers(0, 5, size=(e, k, k)) + np.eye(k, dtype=int)[None] * 20
    ds = xr.Dataset(
        {"confusion": (("epoch", "true_class", "pred_class"), counts)},
        coords={"epoch": [1, 2, 3], "true_class": classes, "pred_class": classes},
        attrs={"best_epoch": 2},
    )
    plot = get_recipe("ml.confusion_epochs").render({"training": ds}, max_classes=4)
    m = frame(plot, "matrix")
    assert set(m["true"]) == set(m["predicted"])
    assert len(set(m["true"])) == 4 and "(other)" in set(m["true"])
    # merging classes keeps every image
    assert m.groupby("epoch")["count"].sum().tolist() == counts.sum(axis=(1, 2)).tolist()
    assert "(other) holds the other 5" in (plot.caption_plain or "")


def test_confusion_epochs_needs_the_confusion_var() -> None:
    tree = ex.training_tree()
    ds = tree["model/training"].to_dataset().drop_vars("confusion")
    with pytest.raises(RecipeDataError, match=r"training\.confusion"):
        get_recipe("ml.confusion_epochs").render({"training": ds})


# ------------------------------------------------------------------- misclassified grid


def _traces(plot: PlotSpec) -> list[dict[str, Any]]:
    data = plot.spec["data"]
    assert isinstance(data, list)
    return data


def test_misclassified_rgb_traces_have_inline_pixels_and_titles() -> None:
    plot = get_recipe("ml.misclassified").render(ex.training_tree())
    traces = _traces(plot)
    assert len(traces) == 12 and all(t["type"] == "image" for t in traces)
    z = np.asarray(traces[0]["z"])
    assert z.shape == (16, 16, 3) and z.min() >= 0 and z.max() <= 255
    assert not any("source" in t for t in traces)
    titles = [a["text"] for a in plot.spec["layout"]["annotations"]]
    assert all("→" in t and "(p=" in t for t in titles)
    cap = plot.caption_plain or ""
    assert cap.startswith("17 of 60 validation images are wrong at epoch 7.")


def test_misclassified_gray_is_repeated_to_rgb() -> None:
    plot = get_recipe("ml.misclassified").render(ex.training_tree(rgb=False), max_items=3)
    traces = _traces(plot)
    assert len(traces) == 3
    z = np.asarray(traces[0]["z"])
    assert z.shape == (16, 16, 3)
    assert (z[..., 0] == z[..., 1]).all() and (z[..., 1] == z[..., 2]).all()


def test_misclassified_large_grids_are_subsampled_under_the_node_cap() -> None:
    rng = np.random.default_rng(3)
    n, side = 32, 48
    ds = xr.Dataset(
        {
            "thumb": (("example", "ty", "tx", "channel"), rng.integers(0, 256, (n, side, side, 3))),
            "prob": (("example",), rng.uniform(0.5, 1.0, n)),
        },
        coords={
            "example": np.arange(n),
            "true_label": ("example", ["<b>cat</b>"] * n),
            "pred_label": ("example", ["dog"] * n),
        },
        attrs={"epoch": 3, "n_wrong": n, "n_val": 200},
    )
    plot = get_recipe("ml.misclassified").render({"misclassified": ds}, max_items=32)
    check(plot, "ml.misclassified")
    traces = _traces(plot)
    pixels = sum(np.asarray(t["z"]).shape[0] * np.asarray(t["z"]).shape[1] for t in traces)
    assert 4 * pixels <= PIXEL_NODE_BUDGET
    assert np.asarray(traces[0]["z"]).shape == (24, 24, 3)
    assert "one pixel in 2" in (plot.caption_plain or "")
    # the labels are untrusted text: escaped, never HTML
    assert all("<b>" not in a["text"] for a in plot.spec["layout"]["annotations"])
    # most confident mistake first
    probs = ds["prob"].values
    assert plot.spec["layout"]["annotations"][0]["text"].endswith(f"(p={probs.max():.2f})")


def test_misclassified_needs_the_group() -> None:
    with pytest.raises(RecipeDataError, match="no misclassified group"):
        get_recipe("ml.misclassified").render(ex.loss_history_tree())
