"""The causal pack (impact, pre-period fit, donor weights, event study) on the groups the
causal plugins write: ``sample`` and ``chain``/``draw`` inputs, one unit or the sum over
units, staggered ``post`` cells, the DiD design and the numbers behind the captions."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.packs.causal import causal_recipes  # noqa: E402
from mapi_plots.recipes import Figure, RecipeDataError, all_recipes, get_recipe  # noqa: E402
from mapi_plots.recipes._base import RecipeInput, as_tree  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402

pytestmark = pytest.mark.unit

IDS = ("causal.impact", "causal.pre_fit", "causal.donor_weights", "causal.event_study")
GROUP = {
    "causal.impact": "impact",
    "causal.pre_fit": "impact",
    "causal.donor_weights": "donor_weights",
    "causal.event_study": "event_study",
}


def figure(recipe_id: str, data: Any, **options: Any) -> Figure:
    r = get_recipe(recipe_id)
    return r.fn(RecipeInput(as_tree(data)), r.parse_settings(options))


def to_chains(ds: Any, chains: int = 2) -> Any:
    """The same draws with ``sample`` split into ``(chain, draw)``."""
    n = ds.sizes["sample"] // chains * chains
    ds = ds.isel(sample=slice(0, n))
    idx = xr.Coordinates.from_pandas_multiindex(
        pd.MultiIndex.from_product([range(chains), range(n // chains)], names=["chain", "draw"]),
        "sample",
    )
    return ds.assign_coords(idx).unstack("sample")


def data_for(recipe_id: str) -> dict[str, Any]:
    data, _ = ex.example_inputs()[recipe_id]
    return dict(data)


# ------------------------------------------------------------------ all of them


def test_the_pack_is_registered_with_its_tags_and_groups() -> None:
    ids = [r.id for r in causal_recipes()]
    assert tuple(ids) == IDS
    catalog = {r.id: r for r in all_recipes()}
    for r in causal_recipes():
        assert catalog[r.id] is r
        assert r.persona == "both" and r.tags == ("causal",)
        assert r.requires_groups == (GROUP[r.id],)


@pytest.mark.parametrize("recipe_id", IDS)
def test_examples_render_and_pass_the_sanitizer(recipe_id: str) -> None:
    data, options = ex.example_inputs()[recipe_id]
    spec = get_recipe(recipe_id).render(data, **options)
    clean = sanitize_plot(spec.model_dump(mode="json")).plot
    assert clean.datasets.keys() == spec.datasets.keys()
    again = sanitize_plot(spec)
    assert again.plot == spec and again.changes == []
    assert spec.caption_plain and "90% interval" in spec.caption_plain
    # summaries per period or per donor, never the draws
    frame = next(iter(figure(recipe_id, data).datasets.values()))
    assert len(frame) <= 40 and "sample" not in frame.columns


@pytest.mark.parametrize("recipe_id", IDS)
def test_chain_draw_inputs_give_the_same_figure(recipe_id: str) -> None:
    data = data_for(recipe_id)
    group = GROUP[recipe_id]
    a = figure(recipe_id, data)
    b = figure(recipe_id, {**data, group: to_chains(data[group])})
    key = next(iter(a.datasets))
    pd.testing.assert_frame_equal(a.datasets[key], b.datasets[key])
    assert a.caption == b.caption


@pytest.mark.parametrize("recipe_id", IDS)
def test_a_missing_group_is_a_data_error(recipe_id: str) -> None:
    with pytest.raises(RecipeDataError):
        get_recipe(recipe_id).render({"other": xr.Dataset({"x": ("t", np.arange(3.0))})})


# ------------------------------------------------------------------ causal.impact


def test_impact_without_a_unit_dim_totals_the_effect_window() -> None:
    ds = ex.impact_dataset()
    fig = figure("causal.impact", {"impact": ds})
    frame = fig.datasets["impact"]
    post = ds["post"].values
    assert len(frame) == ds.sizes["time"]
    assert frame["cumulative"][~post].isna().all()
    assert frame["cumulative"][post].notna().all()
    total = (ds["effect"].where(ds["post"], 0.0)).sum("time")
    expected = float(np.median(total.values))
    assert frame["cumulative"].iloc[-1 - (len(post) - 1 - np.flatnonzero(post)[-1])] == (
        pytest.approx(expected)
    )
    assert "probability that the effect is above zero" in fig.caption
    assert "2024-06-09" in fig.caption and "2024-06-30" in fig.caption
    assert frame["marker_start"].sum() == 1 and frame["marker_end"].sum() == 1


def test_impact_sums_over_units_or_selects_one() -> None:
    ds = ex.impact_dataset(["a", "b"])
    summed = figure("causal.impact", {"impact": ds})
    assert "summed over 2 units" in summed.caption
    np.testing.assert_allclose(summed.datasets["impact"]["actual"], ds["actual"].sum("unit").values)
    one = figure("causal.impact", {"impact": ds}, unit="b")
    assert "for b" in one.caption
    np.testing.assert_allclose(one.datasets["impact"]["actual"], ds["actual"].sel(unit="b"))
    total_b = float(np.median(ds["effect"].sel(unit="b").where(ds["post"], 0).sum("time")))
    assert one.datasets["impact"]["cumulative"].dropna().iloc[-1] == pytest.approx(total_b)
    with pytest.raises(RecipeDataError, match="no unit 'zz'"):
        figure("causal.impact", {"impact": ds}, unit="zz")
    with pytest.raises(RecipeDataError, match="no unit dim"):
        figure("causal.impact", {"impact": ex.impact_dataset()}, unit="a")


def test_staggered_post_cells_are_summed_per_unit() -> None:
    ds = ex.impact_dataset(["a", "b", "c"], staggered=True)
    assert ds["post"].dims == ("unit", "time")
    fig = figure("causal.impact", {"impact": ds})
    frame = fig.datasets["impact"]
    post_any = ds["post"].any("unit").values
    # the window starts with the first unit and the cumulative effect only adds its cells
    assert frame["window"].eq("effect window").to_numpy().tolist() == post_any.tolist()
    total = ds["effect"].where(ds["post"], 0.0).sum(("unit", "time"))
    assert frame["cumulative"].iloc[-1] == pytest.approx(float(np.median(total.values)))
    # an open end draws no end rule
    assert not frame["marker_end"].any() and "dashed red line the end" not in fig.caption
    one = figure("causal.impact", {"impact": ds}, unit="c")
    first = int(np.argmax(one.datasets["impact"]["cumulative"].notna().to_numpy()))
    assert first == int(np.argmax(ds["post"].sel(unit="c").values))


def test_impact_panels_and_integer_time() -> None:
    ds = ex.impact_dataset().assign_coords(time=np.arange(30))
    ds.attrs.update(intervention_start="22", intervention_end="25")
    spec = get_recipe("causal.impact").render({"impact": ds}, panels=["cumulative", "series"])
    assert "vconcat" in spec.spec
    assert len(spec.spec["vconcat"]) == 2
    one = get_recipe("causal.impact").render({"impact": ds}, panels=["pointwise"])
    assert "vconcat" not in one.spec
    fig = figure("causal.impact", {"impact": ds})
    assert fig.datasets["impact"]["marker_start"].to_numpy().nonzero()[0].tolist() == [22]
    assert "(22)" in fig.caption


def test_an_effect_interval_with_zero_is_flagged() -> None:
    ds = ex.impact_dataset()
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 50, ds["effect"].shape)
    flat = ds.assign(
        effect=(ds["effect"].dims, noise), counterfactual=ds["counterfactual"] * 0 + 100.0
    )
    fig = figure("causal.impact", {"impact": flat})
    assert "includes zero" in fig.caption


# ------------------------------------------------------------------ causal.pre_fit


def test_pre_fit_uses_only_the_period_before_the_start() -> None:
    ds = ex.impact_dataset()
    fig = figure("causal.pre_fit", {"impact": ds})
    frame = fig.datasets["pre_fit"]
    assert len(frame) == int(np.argmax(ds["post"].values))
    assert "RMSE" in fig.caption and "MAPE" in fig.caption
    assert "follows the period before the start" in fig.caption


def test_pre_fit_flags_a_poor_fit() -> None:
    ds = ex.impact_dataset()
    bad = ds.assign(counterfactual=ds["counterfactual"] * 0.7)
    fig = figure("causal.pre_fit", {"impact": bad})
    assert "do not trust the effect" in fig.caption
    assert (fig.datasets["pre_fit"]["inside"] == "outside").mean() > 0.2


def test_pre_fit_needs_a_period_before_the_start() -> None:
    ds = ex.impact_dataset()
    with pytest.raises(RecipeDataError, match="before the start"):
        figure("causal.pre_fit", {"impact": ds.assign(post=ds["post"] | True)})


def test_pre_fit_staggered_stops_at_the_first_adoption() -> None:
    ds = ex.impact_dataset(["a", "b", "c"], staggered=True)
    fig = figure("causal.pre_fit", {"impact": ds})
    assert len(fig.datasets["pre_fit"]) == int(np.argmax(ds["post"].any("unit").values))
    one = figure("causal.pre_fit", {"impact": ds}, unit="c")
    assert len(one.datasets["pre_fit"]) == int(np.argmax(ds["post"].sel(unit="c").values))


# ------------------------------------------------------------------ causal.donor_weights


def test_donor_weights_top_n_and_the_rest() -> None:
    ds = ex.donor_weights_dataset()
    fig = figure("causal.donor_weights", {"donor_weights": ds}, top=3)
    frame = fig.datasets["weights"]
    assert set(frame["unit"]) == {"geo_03", "geo_07"}
    one = frame[frame["unit"] == "geo_03"]
    assert len(one) == 4 and one["donor"].iloc[-1].startswith("other donors")
    # simplex: the shown means and the rest add up to one
    assert one["weight"].sum() == pytest.approx(1.0)
    assert one["weight"].iloc[:3].is_monotonic_decreasing
    assert "add up to 1" in fig.caption and "RMSE" in fig.caption


def test_donor_weights_selects_a_unit_and_caps_the_panels() -> None:
    ds = ex.donor_weights_dataset()
    fig = figure("causal.donor_weights", {"donor_weights": ds}, unit="geo_07")
    assert set(fig.datasets["weights"]["unit"]) == {"geo_07"}
    many = ex.donor_weights_dataset(["geo_01", "geo_03", "geo_05", "geo_07", "geo_09"])
    many = many.assign(pre_rmse=("unit", np.arange(5.0)))
    capped = figure("causal.donor_weights", {"donor_weights": many})
    assert set(capped.datasets["weights"]["unit"]) == {"geo_01"}
    assert "5 treated units" in capped.caption
    horseshoe = ds.assign_attrs(prior="horseshoe")
    assert "can be negative" in figure("causal.donor_weights", {"donor_weights": horseshoe}).caption


# ------------------------------------------------------------------ causal.event_study


def test_event_study_marks_the_reference_and_checks_the_trends() -> None:
    ds = ex.event_study_dataset()
    fig = figure("causal.event_study", {"event_study": ds})
    frame = fig.datasets["event_study"]
    ref = frame[frame["period"] == "reference"]
    assert ref["event_time"].tolist() == [-1] and ref["coef"].iloc[0] == 0.0
    assert "no sign of different trends" in fig.caption
    assert "outside the window" in fig.caption
    shifted = ds.copy()
    coef = shifted["coef"].values.copy()
    coef[:, 0] += 5.0
    off = figure(
        "causal.event_study", {"event_study": shifted.assign(coef=(ds["coef"].dims, coef))}
    )
    assert "trends before the start are different" in off.caption and "-4" in off.caption


def test_event_study_with_the_did_design() -> None:
    ds = ex.event_study_dataset("did")
    fig = figure("causal.event_study", {"event_study": ds})
    assert fig.datasets["event_study"]["event_time"].tolist() == [-1, 0]
    assert "one effect for all the periods" in fig.caption
    assert "no periods before the start" in fig.caption
    att = float(np.median(ds["att"].values))
    assert f"{att:.3g}" in fig.caption
