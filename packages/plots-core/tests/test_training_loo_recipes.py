"""The loss curve of an iterative fit and the LOO-PIT / Pareto k recipe."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.recipes import RecipeDataError, get_recipe  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402
from mapi_spec.plots import PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


def check(plot: PlotSpec, rid: str) -> None:
    assert plot.kind == "vega-lite" and plot.id == rid
    assert len(plot.caption_plain or "") > 40
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []


def _history(values: np.ndarray, **attrs: object) -> xr.DataTree:
    n = values.shape[0]
    ds = xr.Dataset(
        {"loss": (("step",), values, {"objective": "negative ELBO", "engine": "advi"})},
        coords={"step": np.arange(1, n + 1)},
        attrs=attrs,
    )
    return xr.DataTree.from_dict({"/model/fit_history": ds})


# ---------------------------------------------------------------------------------- loss


@pytest.mark.parametrize("rid", ["core.loss_curve", "core.loo_pit"])
def test_example_renders(rid: str) -> None:
    data, options = ex.example_inputs()[rid]
    check(get_recipe(rid).render(data, **options), rid)


def test_loss_curve_log_scale_auto_and_caption() -> None:
    step = np.arange(1, 1001)
    plot = get_recipe("core.loss_curve").render(_history(1e4 * np.exp(-step / 50) + 10))
    check(plot, "core.loss_curve")
    spec = json.dumps(plot.spec)
    assert '"type": "log"' in spec or '"type":"log"' in spec
    cap = plot.caption_plain or ""
    assert "negative ELBO" in cap and "moving average over 20 steps" in cap
    assert "stable at the end" in cap
    flat = get_recipe("core.loss_curve").render(_history(100 - step * 0.05))
    assert '"log"' not in json.dumps(flat.spec)
    assert "still goes down at the end. More steps can improve the fit." in (
        flat.caption_plain or ""
    )
    forced = get_recipe("core.loss_curve").render(_history(100 - step * 0.05), log_y=True)
    assert '"log"' in json.dumps(forced.spec)
    with pytest.raises(RecipeDataError, match="log_y"):
        get_recipe("core.loss_curve").render(_history(step - 500.0), log_y=True)


def test_loss_curve_thins_and_keeps_the_last_step() -> None:
    values = np.linspace(10, 1, 12_000)
    plot = get_recipe("core.loss_curve").render(_history(values, thinned_from=40_000))
    loss = frame(plot, "loss")
    assert len(loss) == 2000 and loss["step"].iloc[-1] == 12_000 and loss["step"].iloc[0] == 1
    cap = plot.caption_plain or ""
    assert "kept 12000 of its 40000 steps" in cap and "shows 2000 of 12000" in cap
    smooth = get_recipe("core.loss_curve").render(_history(values), smooth=1)
    s = frame(smooth, "loss")
    assert np.allclose(s["value"], s["smooth"])


def test_loss_curve_labelled_split_dim_gives_two_series() -> None:
    step = np.arange(1, 201)
    vals = np.stack([np.exp(-step / 40) + 0.1, np.exp(-step / 40) + 0.3], axis=1)
    ds = xr.Dataset(
        {"loss": (("step", "split"), vals)},
        coords={"step": step, "split": ["train", "val"]},
    )
    plot = get_recipe("core.loss_curve").render({"metrics_history": ds})
    loss = frame(plot, "loss")
    assert set(loss["series"]) == {"train", "val"} and len(loss) == 400
    assert "for train" in (plot.caption_plain or "")
    wide = xr.Dataset({"m": (("step", "k"), np.ones((10, 9)))}, coords={"step": np.arange(10)})
    with pytest.raises(RecipeDataError, match="at most 8"):
        get_recipe("core.loss_curve").render({"metrics_history": wide})
    with pytest.raises(RecipeDataError, match="fit_history"):
        get_recipe("core.loss_curve").render({"posterior": ds})
    with pytest.raises(RecipeDataError, match="no variable"):
        get_recipe("core.loss_curve").render({"metrics_history": ds}, var="nope")


# ----------------------------------------------------------------------------------- loo


def test_loo_pit_flags_the_outlier() -> None:
    plot = get_recipe("core.loo_pit").render(ex.loo_tree())
    check(plot, "core.loo_pit")
    k = frame(plot, "pareto_k")
    assert len(k) == 40
    worst = k.sort_values("k").iloc[-1]
    assert worst["index"] == 7 and worst["status"] == "bad" and worst["k"] > 0.7
    assert (k.drop(index=7)["k"] < 0.7).all()
    ecdf = frame(plot, "ecdf")
    assert set(ecdf.columns) >= {"z", "diff", "lo", "hi", "status"}
    cap = plot.caption_plain or ""
    assert "1 observation (2%) has k above 0.7" in cap and "the observation 7" in cap


def test_loo_pit_without_predictive_draws_shows_only_k() -> None:
    tree = ex.loo_tree()
    only = {"log_likelihood": tree["log_likelihood"].to_dataset()}
    plot = get_recipe("core.loo_pit").render(only, k_threshold=0.5)
    assert "ecdf" not in plot.datasets and "pareto_k" in plot.datasets
    assert "only the Pareto k" in (plot.caption_plain or "")
    point = {"log_likelihood": xr.Dataset({"y": (("obs",), np.zeros(5))})}
    with pytest.raises(RecipeDataError, match="no draws"):
        get_recipe("core.loo_pit").render(point)
