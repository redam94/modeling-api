"""The v0.12 MMM pack (ROI, adstock, waterfall, spend vs effect, lift tests, effectiveness,
mediation, cross-effects) and the core sampler recipes (energy, prior vs posterior), on the
layouts the MMM plugins write: ``sample`` and ``chain``/``draw`` inputs, missing data,
outcome selection and the numbers behind the captions."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.packs.mmm import outcome_dim, select_outcome  # noqa: E402
from mapi_plots.recipes import Figure, RecipeDataError, get_recipe  # noqa: E402
from mapi_plots.recipes._base import RecipeInput, as_tree  # noqa: E402
from mapi_plots.recipes.bayes import bfmi, overlap  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402

pytestmark = pytest.mark.unit

NEW = (
    "mmm.roi",
    "mmm.adstock",
    "mmm.waterfall",
    "mmm.spend_effect",
    "mmm.lift_tests",
    "mmm.effectiveness",
    "mmm.mediation",
    "mmm.cross_effects",
    "core.energy",
    "core.prior_posterior",
)


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


# ------------------------------------------------------------------ all of them


@pytest.mark.parametrize("recipe_id", NEW)
def test_every_new_recipe_renders_its_example_through_the_sanitizer(recipe_id: str) -> None:
    data, options = ex.example_inputs()[recipe_id]
    spec = get_recipe(recipe_id).render(data, **options)
    clean = sanitize_plot(spec.model_dump(mode="json")).plot
    assert clean.datasets.keys() == spec.datasets.keys()
    assert spec.caption_plain and spec.provenance.recipe == f"{recipe_id}@1.0.0"


@pytest.mark.parametrize(
    ("recipe_id", "group"),
    [
        ("mmm.roi", "response_curves"),
        ("mmm.spend_effect", "response_curves"),
        ("mmm.adstock", "adstock"),
        ("mmm.waterfall", "contributions"),
        ("mmm.lift_tests", "lift_tests"),
        ("mmm.effectiveness", "effectiveness"),
        ("mmm.mediation", "mediation"),
        ("mmm.cross_effects", "cross_effects"),
        ("core.energy", "sample_stats"),
        ("core.prior_posterior", "prior"),
    ],
)
def test_a_missing_group_is_a_recipe_data_error(recipe_id: str, group: str) -> None:
    other = {"unrelated": xr.Dataset({"z": ("i", np.arange(3.0))})}
    if group == "prior":
        other = {"posterior": ex.bundle_tree()["model/posterior"].to_dataset()}
    with pytest.raises(RecipeDataError):
        figure(recipe_id, other)


@pytest.mark.parametrize(
    ("recipe_id", "group", "drop"),
    [
        ("mmm.adstock", "adstock", "weight"),
        ("mmm.lift_tests", "lift_tests", "model"),
        ("mmm.effectiveness", "effectiveness", "effectiveness"),
        ("mmm.mediation", "mediation", "indirect"),
        ("mmm.cross_effects", "cross_effects", "psi"),
    ],
)
def test_a_missing_variable_is_a_recipe_data_error(recipe_id: str, group: str, drop: str) -> None:
    builders = {
        "adstock": ex.adstock_dataset,
        "lift_tests": ex.lift_tests_dataset,
        "effectiveness": ex.effectiveness_dataset,
        "mediation": ex.mediation_dataset,
        "cross_effects": ex.cross_effects_dataset,
    }
    ds = builders[group]().drop_vars(drop)
    with pytest.raises(RecipeDataError, match=drop):
        figure(recipe_id, {group: ds})


# ------------------------------------------------------------------ ROI and spend vs effect


def test_roi_reads_the_grid_at_multiplier_one_and_the_marginal_cost_group() -> None:
    fig = figure("mmm.roi", ex.mmm_tree())
    frame = fig.datasets["roi"].set_index(["measure", "channel"])
    assert len(fig.datasets["roi"]) == 6  # a summary per channel and measure, no draws
    expected = ex._TOP * (1 - np.exp(-ex._SPEND / ex._SAT)) / ex._SPEND
    for ch, roi in zip(ex.MMM_CHANNELS, expected, strict=True):
        assert frame.loc[("ROI", ch), "point"] == pytest.approx(roi, rel=0.05)
    marginal = ex._TOP / ex._SAT * np.exp(-ex._SPEND / ex._SAT)
    assert frame.loc[("marginal ROI", "tv"), "point"] == pytest.approx(marginal[0], rel=0.05)
    assert frame.loc[("ROI", "social"), "hi"] < 1.0
    assert "social is below the break-even point" in fig.caption
    assert "In tv, the next unit of spend returns less than it costs" in fig.caption
    assert "from the marginal_cost group" in fig.caption


def test_roi_without_marginal_cost_uses_the_slope_of_each_curve() -> None:
    rc = ex.response_grid()
    fig = figure("mmm.roi", {"response_curves": rc})
    frame = fig.datasets["roi"].set_index(["measure", "channel"])
    marginal = ex._TOP / ex._SAT * np.exp(-ex._SPEND / ex._SAT)
    # a coarse grid: the central difference is close, not exact
    assert frame.loc[("marginal ROI", "search"), "point"] == pytest.approx(marginal[1], rel=0.15)
    assert "from the response curves" in fig.caption
    # the I-3 grid without current_spend: the spend at multiplier 1
    bare = figure("mmm.roi", {"response_curves": rc.drop_vars("current_spend")})
    assert bare.datasets["roi"]["point"].tolist() == pytest.approx(
        fig.datasets["roi"]["point"].tolist()
    )


def test_roi_takes_chain_draw_draws_and_the_old_spend_grid() -> None:
    chains = figure("mmm.roi", {"response_curves": to_chains(ex.response_grid())})
    assert len(chains.datasets["roi"]) == 6
    old = figure("mmm.roi", ex.bundle_tree())  # response(chain, draw, channel, spend_level)
    assert set(old.datasets["roi"]["channel"]) == set(ex.CHANNELS)


def test_roi_without_a_break_even_line() -> None:
    fig = figure("mmm.roi", ex.mmm_tree(), breakeven=None)
    assert "break-even" not in fig.caption
    spec = get_recipe("mmm.roi").render(ex.mmm_tree(), breakeven=None)
    assert '"strokeDash"' not in spec.model_dump_json()


def test_roi_needs_the_current_spend_off_a_multiplier_grid() -> None:
    rc = ex.response_curves().drop_vars("current_spend")
    with pytest.raises(RecipeDataError, match="current_spend"):
        figure("mmm.roi", {"response_curves": rc})


def test_spend_and_effect_shares_add_up_to_one() -> None:
    fig = figure("mmm.spend_effect", ex.mmm_tree())
    frame = fig.datasets["shares"]
    spend = frame[frame.measure == "share of spend"]["point"]
    effect = frame[frame.measure == "share of effect"]["point"]
    assert spend.sum() == pytest.approx(1.0)
    assert effect.sum() == pytest.approx(1.0, abs=0.05)  # medians of per-draw shares
    assert "social gets 27% of the spend but brings only" in fig.caption


# ------------------------------------------------------------------ adstock


def test_adstock_names_the_longest_carry_over_for_sample_and_chain_inputs() -> None:
    ds = ex.adstock_dataset()
    for data in (ds, to_chains(ds), ds.drop_vars("mean_lag")):
        fig = figure("mmm.adstock", {"adstock": data})
        delay = fig.datasets["delay"].set_index("channel")
        assert delay["point"].idxmax() == "tv"
        assert "tv has the longest carry-over" in fig.caption
        assert "weeks after the spend" in fig.caption
        assert len(fig.datasets["weights"]) == 3 * 8
    lags = np.arange(8)
    w = ds["weight"].sel(channel="search").values
    assert fig.datasets["delay"].set_index("channel").loc["search", "point"] == pytest.approx(
        np.median((w * lags).sum(-1))
    )


def test_adstock_without_a_frequency_talks_of_periods() -> None:
    ds = ex.adstock_dataset()
    ds.attrs = {}
    fig = figure("mmm.adstock", {"adstock": ds}, facet=True)
    assert "periods after the spend" in fig.caption


# ------------------------------------------------------------------ waterfall and outcomes


def test_the_waterfall_steps_end_at_the_total() -> None:
    fig = figure("mmm.waterfall", ex.mmm_tree())
    frame = fig.datasets["waterfall"]
    assert list(frame["component"])[:3] == ["baseline", "trend", "seasonality"]
    assert frame["component"].iloc[-1] == "total"
    steps = frame[frame.kind != "total"]
    assert steps["end"].iloc[-1] == pytest.approx(frame["end"].iloc[-1])
    channels = steps[steps.kind.isin(["adds", "removes"])]
    assert list(channels["mean"]) == sorted(channels["mean"], reverse=True)
    assert "Of the channels, tv gives the most" in fig.caption


def test_the_waterfall_takes_chain_draw_contributions_with_geos() -> None:
    fig = figure("mmm.waterfall", {"contributions": ex.contributions()})
    assert set(fig.datasets["waterfall"]["component"]) == {"tv", "search", "baseline", "total"}


def multi_outcome() -> Any:
    return ex.mmm_contributions(["premium", "basic"])


@pytest.mark.parametrize("recipe_id", ["mmm.waterfall", "mmm.decomposition"])
def test_one_outcome_is_shown_by_setting_attr_or_order(recipe_id: str) -> None:
    ds = multi_outcome()
    key = "waterfall" if recipe_id == "mmm.waterfall" else "totals"
    col = "mean"

    def total(fig: Figure) -> float:
        frame = fig.datasets[key].set_index("component")
        return float(frame.loc["tv", col])

    primary = figure(recipe_id, {"contributions": ds})
    assert "The chart shows the outcome premium" in primary.caption
    basic = figure(recipe_id, {"contributions": ds}, outcome="basic")
    assert "The chart shows the outcome basic" in basic.caption
    assert total(basic) == pytest.approx(total(primary) / 2)
    ds.attrs["primary_outcome"] = "basic"
    by_attr = figure(recipe_id, {"contributions": ds})
    assert total(by_attr) == pytest.approx(total(basic))
    ds.attrs.pop("primary_outcome")
    first = figure(recipe_id, {"contributions": ds})
    assert total(first) == pytest.approx(total(primary))
    with pytest.raises(RecipeDataError, match="no outcome 'gold'"):
        figure(recipe_id, {"contributions": ds}, outcome="gold")
    with pytest.raises(RecipeDataError, match="no 'outcome' dim"):
        figure(recipe_id, {"contributions": ex.mmm_contributions()}, outcome="basic")


@pytest.mark.parametrize("recipe_id", ["mmm.waterfall", "mmm.decomposition"])
@pytest.mark.parametrize("with_binding", [True, False])
def test_a_score_with_dataset_names_shows_one_outcome(recipe_id: str, with_binding: bool) -> None:
    """A score with ``output.names='dataset'`` renames ``outcome`` to the bound dataset dim
    (``product``): the recipes used to miss it and add the two products together."""
    key = "waterfall" if recipe_id == "mmm.waterfall" else "totals"

    def tv(fig: Figure) -> float:
        return float(fig.datasets[key].set_index("component").loc["tv", "mean"])

    canonical = figure(recipe_id, {"contributions": multi_outcome()})
    ds = multi_outcome().rename({"outcome": "product"})
    ds.attrs.pop("primary_outcome")
    ds["baseline"].attrs["primary_outcome"] = "premium"
    tree = as_tree({"derived/contributions": ds})
    if with_binding:
        binding = {"dims": {"outcome": "product"}, "vars": {}}
        tree["binding"] = xr.DataTree(xr.Dataset(attrs={"json": json.dumps(binding)}))
    fig = get_recipe(recipe_id).fn(RecipeInput(tree), get_recipe(recipe_id).parse_settings({}))
    assert tv(fig) == pytest.approx(tv(canonical))
    assert "The model has 2 outcomes. The chart shows the outcome premium." in fig.caption
    basic = get_recipe(recipe_id).fn(
        RecipeInput(tree), get_recipe(recipe_id).parse_settings({"outcome": "basic"})
    )
    assert tv(basic) == pytest.approx(tv(canonical) / 2)


def test_the_outcome_dim_is_not_guessed_without_a_primary_outcome() -> None:
    ds = multi_outcome().rename({"outcome": "product"})
    ds.attrs.pop("primary_outcome")
    assert outcome_dim(ds) is None
    assert outcome_dim(ds, "product") == "product"
    assert outcome_dim(multi_outcome(), "product") == "outcome"


def test_select_outcome_reads_the_attr_of_a_variable() -> None:
    ds = multi_outcome()
    ds.attrs.pop("primary_outcome")
    ds["baseline"].attrs["primary_outcome"] = "basic"
    _, pick = select_outcome(ds, None)
    assert pick == "basic"
    plain, none = select_outcome(ex.mmm_contributions(), None)
    assert none is None and "outcome" not in plain.dims


# ------------------------------------------------------------------ lift tests


def test_lift_tests_name_the_test_the_model_disagrees_with() -> None:
    fig = figure("mmm.lift_tests", ex.mmm_tree())
    frame = fig.datasets["tests"]
    assert len(frame) == 2 * 4
    bad = frame[frame.agrees == "no"]["test"].unique()
    assert list(bad) == ["lift_1: search"]
    assert "does not agree with 1 of 4 tests: lift_1" in fig.caption
    assert "lift_3: tv (north)" in set(frame["test"])
    assert set(frame["estimand"]) == {"contribution", "mroas"}


def test_lift_tests_take_sample_draws_and_a_wider_z() -> None:
    ds = ex.lift_tests_dataset()
    flat = ds.stack(sample=("chain", "draw")).reset_index("sample", drop=True)
    flat = flat.drop_vars(["chain", "draw"], errors="ignore").transpose("sample", "test")
    fig = figure("mmm.lift_tests", {"lift_tests": flat}, z=10.0)
    assert "agrees with all 4 tests" in fig.caption


# ------------------------------------------------------------------ effectiveness


def test_effectiveness_shows_only_the_time_varying_channels() -> None:
    ds = ex.effectiveness_dataset()
    fig = figure("mmm.effectiveness", {"effectiveness": ds})
    assert set(fig.datasets["effectiveness"]["channel"]) == {"tv"}
    assert "The effect of tv went from" in fig.caption and "(not shown)" in fig.caption
    every = figure("mmm.effectiveness", {"effectiveness": to_chains(ds)}, only_varying=False)
    assert set(every.datasets["effectiveness"]["channel"]) == set(ex.MMM_CHANNELS)
    unflagged = figure("mmm.effectiveness", {"effectiveness": ds.drop_vars("time_varying")})
    assert set(unflagged.datasets["effectiveness"]["channel"]) == {"tv"}
    flat = ds.assign(time_varying=ds["time_varying"] & False)
    none = figure("mmm.effectiveness", {"effectiveness": flat})
    assert "No channel has an effect that changes over time" in none.caption
    assert set(none.datasets["effectiveness"]["channel"]) == set(ex.MMM_CHANNELS)


# ------------------------------------------------------------------ mediation and cross-effects


def test_mediation_paths_add_up_and_name_the_largest_share() -> None:
    ds = ex.mediation_dataset()
    for data in (ds, to_chains(ds), ds.drop_vars(["total", "share_mediated"])):
        fig = figure("mmm.mediation", {"mediation": data})
        paths, totals = fig.datasets["paths"], fig.datasets["totals"].set_index("channel")
        summed = paths.groupby("channel")["mean"].sum()
        for ch in ex.MMM_CHANNELS:
            assert summed[ch] == pytest.approx(totals.loc[ch, "total"], rel=1e-6)
        assert "tv has the largest share through the mediators" in fig.caption
        assert "The intervals of social through visits and social through awareness" in (
            fig.caption
        )


def test_cross_effects_name_the_pairs_whose_interval_excludes_zero() -> None:
    ds = ex.cross_effects_dataset()
    fig = figure("mmm.cross_effects", {"cross_effects": ds})
    assert "For 1 of 2 pairs, the interval does not include zero: premium to basic (+0.2" in (
        fig.caption
    )
    assert "association" in fig.caption
    chains = figure("mmm.cross_effects", {"cross_effects": to_chains(ds)})
    assert list(chains.datasets["effects"]["pair"]) == ["premium→basic", "basic→premium"]
    none = ds.assign(psi=ds["psi"] * 0 + np.random.default_rng(0).normal(0, 1, ds["psi"].shape))
    assert (
        "The intervals of all 2 pairs include zero"
        in figure("mmm.cross_effects", {"cross_effects": none}).caption
    )


# ------------------------------------------------------------------ energy


def test_bfmi_of_known_series() -> None:
    alternating = np.tile([0.0, 1.0], 50)  # diff² = 1, var = 0.25
    assert bfmi(alternating[None]) == pytest.approx([4.0])
    rng = np.random.default_rng(0)
    z = rng.standard_normal(20_000)
    walk = np.empty_like(z)
    walk[0] = z[0]
    for i in range(1, z.size):
        walk[i] = 0.95 * walk[i - 1] + z[i]
    # white noise: 2; AR(rho): 2 (1 - rho)
    assert bfmi(np.stack([z, walk])) == pytest.approx([2.0, 0.1], abs=0.05)


def test_energy_names_the_chain_below_the_threshold() -> None:
    fig = figure("core.energy", ex.bundle_tree())
    table = fig.datasets["bfmi"].set_index("chain")
    assert table.loc["1", "status"] == "low" and table.loc["0", "status"] == "ok"
    assert "1 of 2 chains has an E-BFMI below 0.3 (chain 1)" in fig.caption
    assert set(fig.datasets["energy"]["kind"]) == {"marginal energy", "energy transition"}
    ok = figure("core.energy", ex.bundle_tree(), bfmi_threshold=0.1)
    assert "Every chain is above 0.1" in ok.caption
    stats = ex.bundle_tree()["model/sample_stats"].to_dataset().drop_vars("energy")
    with pytest.raises(RecipeDataError, match="energy"):
        figure("core.energy", {"sample_stats": stats})


# ------------------------------------------------------------------ prior vs posterior


def test_prior_posterior_ranks_by_shift_and_reports_overlap() -> None:
    fig = figure("core.prior_posterior", ex.bundle_tree())
    dens = fig.datasets["densities"]
    order = list(dict.fromkeys(dens["parameter"]))
    assert order[0] == "beta[tv]" and len(order) == 6
    assert set(dens["group"]) == {"prior", "posterior"}
    assert "Overlap: beta[tv]" in fig.caption
    capped = figure("core.prior_posterior", ex.bundle_tree(), max_vars=1, max_cells=1)
    assert dens["parameter"].nunique() > capped.datasets["densities"]["parameter"].nunique() == 1
    assert "The chart shows 1 of 6 parameter cells" in capped.caption
    with pytest.raises(RecipeDataError, match="gamma"):
        figure("core.prior_posterior", ex.bundle_tree(), var_names=["gamma"])


def test_prior_posterior_reports_parameters_the_data_did_not_move() -> None:
    rng = np.random.default_rng(1)
    prior = xr.Dataset({"theta": (("chain", "draw"), rng.normal(0, 1, (1, 400)))})
    post = xr.Dataset({"theta": (("sample",), rng.normal(0.05, 1, 400))})
    fig = figure("core.prior_posterior", {"prior": prior, "posterior": post})
    assert "The data adds little to theta" in fig.caption
    grid = np.linspace(-4, 4, 400)
    same = np.exp(-(grid**2) / 2) / np.sqrt(2 * np.pi)
    assert overlap(same, same, grid) == pytest.approx(1.0, abs=1e-3)


def test_the_curve_recipes_read_contribution_whatever_the_variable_order() -> None:
    rc = ex.response_grid()
    shuffled = rc[["spend_by_geo", "max_period_spend", "contribution", "spend", "current_spend"]]
    for recipe_id in ("mmm.roi", "mmm.response_curves"):
        a = figure(recipe_id, {"response_curves": rc})
        b = figure(recipe_id, {"response_curves": shuffled})
        assert a.caption == b.caption
