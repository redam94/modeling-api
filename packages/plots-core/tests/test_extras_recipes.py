"""The extra core recipes: pair, ridge, rank matrix, calibration and state sequence."""

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
from mapi_plots.recipes import RecipeDataError, get_recipe, standard_recipes  # noqa: E402
from mapi_plots.recipes.extras import _ecdf_envelope  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402
from mapi_spec.plots import PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

IDS = ["core.pair", "core.ridge", "core.rank_matrix", "core.calibration", "core.state_sequence"]
INLINE_LIMIT = 2 * 1024 * 1024


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


def check(plot: PlotSpec, rid: str) -> None:
    r = get_recipe(rid)
    assert plot.kind == "vega-lite"
    assert plot.id == rid and plot.provenance.recipe == r.ref
    assert len(plot.caption_plain or "") > 40
    text = json.dumps(plot.spec)
    assert '"url"' not in text and '"values"' not in text
    for ds in plot.datasets.values():
        assert ds.kind == "inline_arrow"
        assert len(ds.data) < INLINE_LIMIT
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []


def test_the_recipes_are_in_the_core_pack() -> None:
    ids = [r.id for r in standard_recipes()]
    assert ids[-5:] == IDS
    for rid in IDS:
        entry = get_recipe(rid).catalog_entry()
        assert entry.about is not None and entry.summary
    assert get_recipe("core.pair").catalog_entry().requires_groups == ["posterior"]
    assert get_recipe("core.calibration").tags == ("diagnostic",)


@pytest.mark.parametrize("rid", IDS)
def test_example_renders(rid: str) -> None:
    data, options = ex.example_inputs()[rid]
    check(get_recipe(rid).render(data, **options), rid)


# ----------------------------------------------------------------------------------- pair


def test_pair_thins_draws_and_keeps_divergences() -> None:
    plot = get_recipe("core.pair").render(ex.bundle_tree(), max_params=3, max_draws=50)
    draws = frame(plot, "draws")
    assert [c for c in draws.columns if c.startswith("p")] == ["p0", "p1", "p2"]
    assert (draws["transition"] == "divergent").sum() == 3
    assert len(draws) <= 53
    hist = frame(plot, "hist")
    assert set(hist["param"]) == {"p0", "p1", "p2"}
    assert hist.groupby("param")["count"].sum().eq(240).all()
    assert "3 divergent" in (plot.caption_plain or "")
    # three rows of 1, 2 and 3 cells
    rows = plot.spec["vconcat"]
    assert [len(r["hconcat"]) for r in rows] == [1, 2, 3]


def test_pair_reports_strong_correlation_and_needs_two_parameters() -> None:
    rng = np.random.default_rng(0)
    a = rng.normal(size=(2, 200))
    post = xr.Dataset(
        {
            "a": (("chain", "draw"), a),
            "b": (("chain", "draw"), a * 2 + 0.05 * rng.normal(size=(2, 200))),
        }
    )
    plot = get_recipe("core.pair").render({"posterior": post})
    assert "reparameterisation" in (plot.caption_plain or "")
    assert "divergent" not in (plot.caption_plain or "")
    with pytest.raises(RecipeDataError, match="at least two"):
        get_recipe("core.pair").render({"posterior": post}, var_names=["a"])
    with pytest.raises(RecipeDataError):
        get_recipe("core.pair").render({"posterior": post}, var_names=["nope"])
    with pytest.raises(RecipeDataError):
        get_recipe("core.pair").render({"posterior": post}, max_params=1)


# ---------------------------------------------------------------------------------- ridge


def test_ridge_orders_by_mean_with_hdi() -> None:
    plot = get_recipe("core.ridge").render(ex.group_effects_tree(), hdi_prob=0.9)
    summary = frame(plot, "summary")
    assert list(summary["base"]) == list(range(8))
    assert summary["mean"].is_monotonic_increasing  # bottom row = lowest mean
    assert summary.iloc[-1]["level"] == "A"
    assert (summary["lo"] < summary["mean"]).all() and (summary["mean"] < summary["hi"]).all()
    dens = frame(plot, "ridge")
    assert (dens["top"] >= dens["base"]).all()
    peaks = dens.assign(h=dens["top"] - dens["base"]).groupby("level")["h"].max()
    assert np.allclose(peaks, 1.6)
    flipped = frame(
        get_recipe("core.ridge").render(ex.group_effects_tree(), descending=False), "summary"
    )
    assert flipped.iloc[0]["level"] == "A"


def test_ridge_inputs() -> None:
    tree = ex.group_effects_tree()
    capped = get_recipe("core.ridge").render(tree, max_levels=3)
    assert len(frame(capped, "summary")) == 3
    assert "5 levels are not shown" in (capped.caption_plain or "")
    rng = np.random.default_rng(1)
    two = xr.Dataset(
        {"theta": (("chain", "draw", "g", "k"), rng.normal(size=(2, 50, 4, 3)))},
        coords={"g": list("abcd"), "k": [0, 1, 2]},
    )
    with pytest.raises(RecipeDataError, match="exactly one dim"):
        get_recipe("core.ridge").render({"posterior": two})
    with pytest.raises(RecipeDataError, match="select"):
        get_recipe("core.ridge").render({"posterior": two}, dim="g")
    plot = get_recipe("core.ridge").render({"posterior": two}, dim="g", select={"k": 1})
    assert len(frame(plot, "summary")) == 4
    preds = xr.Dataset({"y": (("sample", "g"), rng.normal(size=(100, 3)))})
    plot = get_recipe("core.ridge").render({"predictions": preds})
    assert "from predictions" in (plot.caption_plain or "")
    with pytest.raises(RecipeDataError):
        get_recipe("core.ridge").render({"posterior": two}, var="nope")
    with pytest.raises(RecipeDataError, match="with the dim 'zz'"):
        get_recipe("core.ridge").render({"posterior": two}, dim="zz")
    with pytest.raises(RecipeDataError, match="no dim 'zz'"):
        get_recipe("core.ridge").render({"posterior": two}, var="theta", dim="zz")
    with pytest.raises(RecipeDataError, match="no draws"):
        get_recipe("core.ridge").render(
            {"posterior": xr.Dataset({"m": (("g",), np.arange(3.0))})}, var="m"
        )


# ---------------------------------------------------------------------------- rank matrix


def test_rank_matrix_probabilities() -> None:
    rng = np.random.default_rng(2)
    strength = np.array([3.0, 0.0, 1.0]) + 0.3 * rng.normal(size=(2, 200, 3))
    post = xr.Dataset(
        {"strength": (("chain", "draw", "team"), strength)}, coords={"team": ["x", "y", "z"]}
    )
    plot = get_recipe("core.rank_matrix").render({"posterior": post})
    ranks = frame(plot, "ranks")
    assert np.allclose(ranks.groupby("item")["prob"].sum(), 1.0)
    assert np.allclose(ranks.groupby("rank")["prob"].sum(), 1.0)
    assert list(dict.fromkeys(ranks["item"])) == ["x", "z", "y"]  # by expected rank
    top = ranks[(ranks["item"] == "x") & (ranks["rank"] == 1)]["prob"].item()
    assert top == 1.0
    pairs = frame(plot, "pairs")
    xz = pairs[(pairs["item"] == "x") & (pairs["other"] == "z")]["prob"].item()
    zx = pairs[(pairs["item"] == "z") & (pairs["other"] == "x")]["prob"].item()
    assert xz == 1.0 and zx == 0.0
    assert pairs[pairs["item"] == pairs["other"]]["prob"].isna().all()
    assert "x has the best expected rank" in (plot.caption_plain or "")
    low = get_recipe("core.rank_matrix").render({"posterior": post}, descending=False)
    assert next(iter(dict.fromkeys(frame(low, "ranks")["item"]))) == "y"


def test_rank_matrix_limits() -> None:
    rng = np.random.default_rng(3)
    many = xr.Dataset({"s": (("chain", "draw", "item"), rng.normal(size=(1, 50, 40)))})
    with pytest.raises(RecipeDataError, match="at most 30"):
        get_recipe("core.rank_matrix").render({"posterior": many})
    plot = get_recipe("core.rank_matrix").render({"posterior": many}, max_items=40)
    assert "mark_text" not in json.dumps(plot.spec)
    assert '"text"' not in json.dumps(plot.spec)  # no labels above 12 items
    labelled = get_recipe("core.rank_matrix").render(
        {"posterior": many}, max_items=40, labels="always"
    )
    assert '"text"' in json.dumps(labelled.spec)


# ---------------------------------------------------------------------------- calibration


def test_ecdf_envelope_covers_uniform_samples() -> None:
    n = 100
    z = np.arange(1, 101) / 101
    lo, hi = _ecdf_envelope(n, z, 0.95, 2000, 0)
    assert np.all(lo < 0) and np.all(hi > 0) and np.allclose(lo, -hi)
    rng = np.random.default_rng(9)
    inside = 0
    for _ in range(400):
        u = np.sort(rng.random(n))
        d = np.searchsorted(u, z, side="right") / n - z
        inside += bool(np.all((d >= lo) & (d <= hi)))
    assert 0.9 < inside / 400 <= 1.0


def test_calibration_continuous_flags_narrow_ranges() -> None:
    plot = get_recipe("core.calibration").render(ex.calibration_tree())
    ecdf = frame(plot, "ecdf")
    assert set(ecdf.columns) >= {"z", "diff", "lo", "hi", "status"}
    assert (ecdf["status"] == "outside").any()
    assert "too narrow" in (plot.caption_plain or "")
    rng = np.random.default_rng(4)
    mu = rng.normal(size=300)
    good = {
        "posterior_predictive": xr.Dataset(
            {"y": (("chain", "draw", "i"), mu + rng.normal(size=(2, 100, 300)))}
        ),
        "observed_data": xr.Dataset({"y": (("i",), mu + rng.normal(size=300))}),
    }
    ok = get_recipe("core.calibration").render(good)
    assert "inside the band" in (ok.caption_plain or "")
    pp = good["posterior_predictive"]
    high = get_recipe("core.calibration").render({**good, "posterior_predictive": pp + 1.5})
    assert "over-predicts" in (high.caption_plain or "")
    wide = get_recipe("core.calibration").render({**good, "posterior_predictive": pp * 3})
    assert "too wide" in (wide.caption_plain or "")


def test_calibration_binary_reliability() -> None:
    plot = get_recipe("core.calibration").render(ex.calibration_tree(binary=True), bins=5)
    check(plot, "core.calibration")
    rel = frame(plot, "reliability")
    assert rel["count"].sum() == 200 and len(rel) <= 5
    assert ((rel["lo"] <= rel["predicted"]) & (rel["predicted"] <= rel["hi"])).all()
    assert "Brier score" in (plot.caption_plain or "")
    with pytest.raises(RecipeDataError, match="kind=binary"):
        get_recipe("core.calibration").render(ex.calibration_tree(), kind="binary")
    forced = get_recipe("core.calibration").render(
        ex.calibration_tree(binary=True), kind="continuous"
    )
    assert "ecdf" in forced.datasets


def test_calibration_needs_observed_values() -> None:
    tree = ex.calibration_tree()
    preds = tree["posterior_predictive"].to_dataset()
    with pytest.raises(RecipeDataError, match="observed"):
        get_recipe("core.calibration").render({"posterior_predictive": preds})
    with pytest.raises(RecipeDataError):
        get_recipe("core.calibration").render(tree, var="nope")
    with pytest.raises(RecipeDataError):
        get_recipe("core.calibration").render({"observed_data": preds})


# ------------------------------------------------------------------------- state sequence


def test_state_sequence_probabilities() -> None:
    plot = get_recipe("core.state_sequence").render(ex.regime_tree())
    states = frame(plot, "states")
    assert set(states["state"]) == {"calm", "volatile"}
    assert np.allclose(states.groupby("t")["prob"].sum(), 1.0)
    obs = frame(plot, "observed")
    assert len(obs) == 150 and set(obs["state"]) == {"calm", "volatile"}
    assert "changes 4 times" in (plot.caption_plain or "")


def _discrete_tree(**extra: Any) -> dict[str, Any]:
    rng = np.random.default_rng(5)
    truth = np.r_[np.zeros(30), np.ones(20), np.full(10, 2)].astype(int)
    flip = rng.random((2, 50, 60)) < 0.1
    draws = np.where(flip, rng.integers(0, 3, (2, 50, 60)), truth)
    t = np.arange(60)
    return {
        "posterior": xr.Dataset({"z": (("chain", "draw", "t"), draws)}, {"t": t}),
        **extra,
    }


def test_state_sequence_counts_discrete_draws() -> None:
    plot = get_recipe("core.state_sequence").render(_discrete_tree())
    states = frame(plot, "states")
    assert set(states["state"]) == {"0", "1", "2"}
    assert np.allclose(states.groupby("t")["prob"].sum(), 1.0)
    assert "observed" not in plot.datasets
    assert "no observed series" in (plot.caption_plain or "")
    assert "changes 2 times" in (plot.caption_plain or "")
    thinned = get_recipe("core.state_sequence").render(_discrete_tree(), max_times=20)
    assert frame(thinned, "states")["t"].nunique() == 20


def test_state_sequence_observed_series_and_errors() -> None:
    rng = np.random.default_rng(6)
    obs = xr.Dataset(
        {"y": (("t", "sensor"), rng.normal(size=(60, 2)))},
        coords={"t": np.arange(60), "sensor": ["a", "b"]},
    )
    plot = get_recipe("core.state_sequence").render(_discrete_tree(observed_data=obs))
    assert set(frame(plot, "observed")["series"]) == {"a", "b"}
    short = obs.isel(t=slice(0, 10))
    with pytest.raises(RecipeDataError, match="times"):
        get_recipe("core.state_sequence").render(_discrete_tree(observed_data=short))
    with pytest.raises(RecipeDataError, match="no observed variable"):
        get_recipe("core.state_sequence").render(
            _discrete_tree(observed_data=obs), observed_var="nope"
        )
    with pytest.raises(RecipeDataError, match="no state variable"):
        get_recipe("core.state_sequence").render(
            {"posterior": xr.Dataset({"x": (("chain", "draw"), rng.normal(size=(2, 9)))})}
        )
    with pytest.raises(RecipeDataError, match="not a discrete state"):
        get_recipe("core.state_sequence").render(
            {"posterior": xr.Dataset({"x": (("chain", "draw", "t"), rng.normal(size=(2, 9, 5)))})},
            var="x",
        )
    with pytest.raises(RecipeDataError, match="no variable"):
        get_recipe("core.state_sequence").render(_discrete_tree(), var="nope")


def test_state_sequence_accepts_summary_probabilities() -> None:
    p = np.array([[0.9, 0.2, 0.1], [0.1, 0.8, 0.9]]) * 2  # not normalised
    ds = xr.Dataset({"prob": (("state", "t"), p)}, coords={"state": [0, 1], "t": [0, 1, 2]})
    plot = get_recipe("core.state_sequence").render({"posterior": ds})
    states = frame(plot, "states")
    assert np.allclose(states.groupby("t")["prob"].sum(), 1.0)
    first = states[(states["state"] == "0") & (states["t"] == 0)]["prob"].item()
    assert first == pytest.approx(0.9)
