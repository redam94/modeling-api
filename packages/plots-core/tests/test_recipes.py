"""Every recipe on synthetic DataTrees (xarray only, no PyMC)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")

from mapi_plots import examples as ex  # noqa: E402
from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.packs.causal import causal_recipes  # noqa: E402
from mapi_plots.packs.ml import ml_recipes  # noqa: E402
from mapi_plots.packs.mmm import mmm_recipes  # noqa: E402
from mapi_plots.packs.net import net_recipes  # noqa: E402
from mapi_plots.packs.sci import sci_recipes  # noqa: E402
from mapi_plots.recipes import (  # noqa: E402
    Recipe,
    RecipeDataError,
    all_recipes,
    as_tree,
    get_recipe,
    standard_recipes,
)
from mapi_plots.recipes._base import RecipeInput  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402
from mapi_spec.catalog import Catalog  # noqa: E402
from mapi_spec.errors import ErrorCode  # noqa: E402
from mapi_spec.plots import PlotSpec  # noqa: E402

pytestmark = pytest.mark.unit

PLAN_IDS = {
    "core.data_overview",
    "core.prior_predictive",
    "core.convergence",
    "core.posterior_forest",
    "core.ppc",
    "core.ppc_fan",
    "core.decomposition",
    "core.model_comparison",
    "core.counterfactual",
    "core.decision_distribution",
    "mmm.response_curves",
    "mmm.marginal_cost",
    "mmm.budget_plans",
    "ml.feature_importance",
}
EXAMPLES = ex.example_inputs()


def frame(plot: PlotSpec, name: str) -> pd.DataFrame:
    return arrow_to_frame(plot.datasets[name])


def caption(plot: PlotSpec) -> str:
    return plot.caption_plain or ""


def check_plot(plot: PlotSpec, recipe: Recipe[Any]) -> None:
    """The contract every recipe output satisfies (the sci pack draws with Plotly)."""
    assert plot.kind == ("plotly" if recipe.id.startswith(("sci.", "net.")) else "vega-lite")
    assert plot.id == recipe.id
    assert plot.provenance.recipe == recipe.ref
    assert plot.persona == recipe.persona
    assert len(caption(plot)) > 20
    assert plot.datasets
    text = json.dumps(plot.spec)
    assert '"url"' not in text and '"values"' not in text
    for ds in plot.datasets.values():
        assert ds.kind == "inline_arrow"
    again = sanitize_plot(plot)
    assert again.plot == plot and again.changes == []


# ----------------------------------------------------------------------------- catalog


def test_catalog_covers_the_plan() -> None:
    ids = {r.id for r in all_recipes()}
    assert ids >= PLAN_IDS
    assert len(ids) == len(all_recipes())
    sci = {r.id for r in sci_recipes()}
    assert set(EXAMPLES) == ids  # every recipe has example data (the catalog renders it)
    packs = {r.id for r in mmm_recipes()} | {r.id for r in ml_recipes()} | sci
    packs |= {r.id for r in net_recipes()} | {r.id for r in causal_recipes()}
    assert {r.id for r in standard_recipes()} | packs == ids
    catalog = Catalog(plots=[r.catalog_entry() for r in all_recipes()])
    assert catalog.resolve_plot("core.ppc_fan@^1") is not None
    entry = get_recipe("core.convergence").catalog_entry()
    assert entry.requires_groups == ["posterior"] and entry.persona == "expert"
    assert [r.value for r in entry.requires_representation] == ["draws"]
    assert all(r.summary for r in all_recipes())
    with pytest.raises(KeyError):
        get_recipe("core.nope")


@pytest.mark.parametrize("recipe_id", sorted(EXAMPLES))
def test_every_recipe_renders_a_safe_plotspec(recipe_id: str) -> None:
    data, options = EXAMPLES[recipe_id]
    recipe = get_recipe(recipe_id)
    plot = recipe.render(data, **options)
    check_plot(plot, recipe)
    again = recipe(data, **options)
    assert again.model_copy(update={"provenance": plot.provenance}) == plot


def test_bundle_id_flows_into_provenance(bundle: Any) -> None:
    plot = get_recipe("core.posterior_forest").render(bundle, title="Custom")
    assert plot.provenance.bundle_id == "bundle-123"
    assert plot.title == "Custom"


def test_settings_are_validated(bundle: Any) -> None:
    with pytest.raises(RecipeDataError) as info:
        get_recipe("core.posterior_forest").render(bundle, nope=1)
    assert info.value.code is ErrorCode.SCHEMA_INVALID
    assert "nope" in (info.value.untrusted_text or "")
    with pytest.raises(RecipeDataError):
        get_recipe("core.ppc_fan").render(bundle, levels=[1.5])


def test_missing_groups_are_typed_errors(scored: Any) -> None:
    for rid in ("core.convergence", "core.ppc", "core.prior_predictive", "mmm.response_curves"):
        with pytest.raises(RecipeDataError):
            get_recipe(rid).render(scored)


# ------------------------------------------------------------------------------ inputs


def test_as_tree_accepts_datasets_arrays_and_mappings(bundle: Any) -> None:
    ds = ex.panel_data()
    assert as_tree(ds).to_dataset().equals(ds)
    assert "revenue" in as_tree(ds["revenue"]).to_dataset()
    tree = as_tree({"a": ds, "b/c": ds["revenue"], "m": bundle})
    assert RecipeInput(tree).find("c") is not None
    assert RecipeInput(tree).find("m/model/posterior") is not None
    with pytest.raises(RecipeDataError):
        as_tree(42)


def test_group_lookup(bundle: Any) -> None:
    inp = RecipeInput(bundle)
    assert inp.find("posterior") is not None
    assert inp.find("contributions") is not None  # under derived/
    assert inp.find("nothing") is None
    assert inp.bundle_id == "bundle-123"
    with pytest.raises(RecipeDataError, match="none of the groups"):
        inp.require("nothing", "else")
    assert RecipeInput(as_tree(ex.panel_data())).find("whatever") is not None  # bare Dataset


# ----------------------------------------------------------------------- data overview


def test_data_overview_series_missingness_and_groups() -> None:
    plot = get_recipe("core.data_overview").render(ex.panel_data())
    series = frame(plot, "series")
    assert set(series["variable"].astype(str)) == {"revenue", "spend"}
    assert set(series["group"].astype(str)) == set(ex.GEOS)
    miss = frame(plot, "missing")
    rev = miss[miss.variable == "revenue"]
    assert rev["missing"].max() == pytest.approx(1 / 3)
    groups = frame(plot, "groups")
    assert dict(zip(groups.group, groups.n, strict=True))["north"] == 19
    assert "sum over channel" in caption(plot)


def test_data_overview_without_time_or_facets() -> None:
    ds = xr.Dataset({"y": (("obs",), np.r_[np.arange(9.0), np.nan])})
    plot = get_recipe("core.data_overview").render(ds)
    assert set(frame(plot, "series")["group"].astype(str)) == {"all"}
    assert "groups" not in plot.datasets
    plot = get_recipe("core.data_overview").render(ex.panel_data(), vars=["revenue"], facet="geo")
    assert set(frame(plot, "series")["variable"].astype(str)) == {"revenue"}
    with pytest.raises(RecipeDataError):
        get_recipe("core.data_overview").render(ex.panel_data(), vars=["nope"])
    with pytest.raises(RecipeDataError):
        get_recipe("core.data_overview").render(xr.Dataset({"s": ("x", ["a", "b"])}))
    big = get_recipe("core.data_overview").render(ex.panel_data(), max_rows=100)
    assert len(frame(big, "series")) <= 200


# -------------------------------------------------------------------------- diagnostics


def test_convergence_flags_the_disagreeing_chain(bundle: Any) -> None:
    plot = get_recipe("core.convergence").render(bundle, max_traces=2)
    diag = frame(plot, "diagnostics")
    worst = diag.iloc[0]
    assert worst["parameter"] == "beta[tv]" and worst["status"] == "check"
    assert worst["rhat"] > 1.5
    traced = set(frame(plot, "trace")["parameter"].astype(str))
    assert traced == {"beta[tv]", str(diag.iloc[1]["parameter"])}
    rank = frame(plot, "rank")
    totals = rank.groupby(["parameter", "chain"], observed=True)["count"].sum().unique()
    assert totals.tolist() == [ex.DRAWS]
    assert "not converged" in caption(plot) and "3 divergent" in caption(plot)
    table = frame(plot, "table")
    assert set(table["column"].astype(str)) == {
        "parameter",
        "R-hat",
        "ESS bulk",
        "ESS tail",
        "status",
    }
    header = table[table["status"] == "header"]
    assert header["text"].tolist() == ["parameter", "R-hat", "ESS bulk", "ESS tail", "status"]
    assert header["x"].is_monotonic_increasing and header["x"].iloc[1] >= 0.5


def test_convergence_all_good_and_limits() -> None:
    rng = np.random.default_rng(0)
    post = xr.Dataset({"mu": (("chain", "draw"), rng.standard_normal((4, 500)))})
    plot = get_recipe("core.convergence").render({"posterior": post})
    assert caption(plot).startswith("All 1 parameters look converged")
    with pytest.raises(RecipeDataError):
        get_recipe("core.convergence").render({"posterior": post}, var_names=["nope"])
    with pytest.raises(RecipeDataError):
        get_recipe("core.convergence").render({"posterior": post.isel(chain=0)})
    many = xr.Dataset({"v": (("chain", "draw", "k"), rng.standard_normal((2, 50, 5)))})
    plot = get_recipe("core.convergence").render({"posterior": many}, max_elements=3)
    assert "first 3" in caption(plot)


def test_posterior_forest_intervals(bundle: Any) -> None:
    plot = get_recipe("core.posterior_forest").render(bundle, hdi_prob=0.8)
    forest = frame(plot, "forest")
    assert len(forest) == 6
    assert (forest["lo"] <= forest["lo_inner"]).all()
    assert (forest["hi_inner"] <= forest["hi"]).all()
    assert ((forest["lo"] <= forest["point"]) & (forest["point"] <= forest["hi"])).all()
    single = get_recipe("core.posterior_forest").render(bundle, var_names=["beta"], point="mean")
    assert set(frame(single, "forest")["variable"].astype(str)) == {"beta"}
    assert "facet" not in single.spec
    capped = get_recipe("core.posterior_forest").render(bundle, max_rows=2)
    assert len(frame(capped, "forest")) == 2


def test_ppc_density_pit_coverage(bundle: Any) -> None:
    plot = get_recipe("core.ppc").render(bundle, num_draws=10, bins=10)
    pit = frame(plot, "pit")
    assert pit["count"].sum() == 59  # one observation is NaN
    cov = frame(plot, "coverage")
    assert ((cov["empirical"] >= 0) & (cov["empirical"] <= 1)).all()
    dens = frame(plot, "density")
    assert set(dens["series"].astype(str)) == {
        "predictive draw",
        "predictive draw (all)",
        "observed",
    }
    assert dens.loc[dens.series == "predictive draw", "draw"].nunique() == 10


def test_ppc_captions_detect_bias_and_narrow_intervals() -> None:
    rng = np.random.default_rng(1)
    obs = xr.Dataset({"y": ("obs", rng.standard_normal(200))})
    dims = ("chain", "draw", "obs")
    narrow = xr.Dataset({"y": (dims, 0.1 * rng.standard_normal((2, 100, 200)))})
    biased = xr.Dataset({"y": (dims, 3 + rng.standard_normal((2, 100, 200)))})
    ppc = get_recipe("core.ppc")
    assert "too narrow" in caption(
        ppc.render({"posterior_predictive": narrow, "observed_data": obs})
    )
    assert "over-predicts" in caption(
        ppc.render({"posterior_predictive": biased, "observed_data": obs})
    )
    assert "under-predicts" in caption(
        ppc.render({"posterior_predictive": biased - 6, "observed_data": obs})
    )
    wide = xr.Dataset({"y": (dims, 20 * rng.standard_normal((2, 100, 200)))})
    assert "too wide" in caption(ppc.render({"posterior_predictive": wide, "observed_data": obs}))
    with pytest.raises(RecipeDataError):
        ppc.render({"posterior_predictive": biased})


def test_prior_predictive_fan_and_caption(bundle: Any) -> None:
    plot = get_recipe("core.prior_predictive").render(bundle, num_draws=5, levels=[0.8])
    fan = frame(plot, "fan")
    assert {"point", "lo_80", "hi_80", "observed"} <= set(fan.columns)
    assert len(fan) == 20
    assert "prior 90% range" in caption(plot)
    rng = np.random.default_rng(0)
    flat = xr.Dataset({"y": (("chain", "draw", "obs"), rng.normal(size=(1, 50, 30)))})
    plot = get_recipe("core.prior_predictive").render({"prior_predictive": flat})
    assert "fan" not in plot.datasets
    wide = xr.Dataset({"y": (("chain", "draw", "obs"), 1e6 * rng.normal(size=(1, 50, 30)))})
    obs = xr.Dataset({"y": ("obs", rng.normal(size=30))})
    cap = caption(
        get_recipe("core.prior_predictive").render({"prior_predictive": wide, "observed_data": obs})
    )
    assert "very wide" in cap
    narrow = xr.Dataset({"y": (("chain", "draw", "obs"), 1e-3 * rng.normal(size=(1, 50, 30)))})
    cap = caption(
        get_recipe("core.prior_predictive").render(
            {"prior_predictive": narrow, "observed_data": obs}
        )
    )
    assert "too narrow" in cap


# ---------------------------------------------------------------------------- forecasts


def test_fan_from_draws_detects_the_split(scored: Any) -> None:
    plot = get_recipe("core.ppc_fan").render(scored)
    fan = frame(plot, "fan")
    assert {"lo_90", "hi_90", "lo_50", "hi_50", "observed", "split"} <= set(fan.columns)
    split = pd.Timestamp(fan["split"].iloc[0])
    assert split == pd.Timestamp("2024-01-07") + pd.Timedelta(weeks=20)
    assert "facet" in plot.spec
    assert "training data ends" in caption(plot)


def test_fan_speaks_of_the_dataset_target_not_the_model_output() -> None:
    """Score outputs predict ``y_hat``; readers know the KPI by the dataset's name."""
    preds = xr.Dataset(
        {"y_hat": (("chain", "draw", "week"), np.ones((1, 20, 6)))},
        coords={"week": pd.date_range("2024-01-07", periods=6, freq="W-SUN")},
    )
    obs = xr.Dataset({"revenue": (("week",), np.ones(6))}, coords={"week": preds["week"]})
    plot = get_recipe("core.ppc_fan").render({"posterior_predictive": preds, "observed_data": obs})
    assert "estimate of revenue" in caption(plot) and "y_hat" not in caption(plot)
    assert '"title": "revenue"' in json.dumps(plot.spec)


def test_fan_from_summary_and_explicit_split() -> None:
    tree = ex.score_tree(with_draws=False)
    plot = get_recipe("core.ppc_fan").render(tree, split_at="2024-03-03", point="mean")
    fan = frame(plot, "fan")
    assert pd.Timestamp(fan["split"].iloc[0]) == pd.Timestamp("2024-03-03")
    np.testing.assert_allclose(fan["hi_90"] - fan["lo_90"], 30.0, rtol=1e-5)
    assert "After it" in caption(plot)
    assert "The shaded bands are the 50% and 90% intervals" in caption(plot)
    # a summary with a 90% interval only: no empty 50% band layer, an honest caption
    only90 = tree["summary"].to_dataset().sel(stat=["mean", "hdi_90_lower", "hdi_90_upper"])
    plot = get_recipe("core.ppc_fan").render({"summary": only90}, point="mean")
    only = frame(plot, "fan")
    assert "lo_50" not in only.columns or only["lo_50"].isna().all()
    assert "lo_50" not in json.dumps(plot.spec)
    assert "The shaded band is the 90% interval." in caption(plot)


def test_fan_split_from_attrs_and_no_facet() -> None:
    tree = ex.score_tree()
    root = xr.Dataset(attrs={**tree.attrs, "split_at": "2024-02-04"})
    data = {
        "/": root,
        "draws": tree["draws"].to_dataset().sel(geo="north"),
        "observed": tree["observed"].to_dataset().sel(geo="north"),
    }
    plot = get_recipe("core.ppc_fan").render(data)
    fan = frame(plot, "fan")
    assert set(fan["group"].astype(str)) == {"all"} and "facet" not in plot.spec
    assert pd.Timestamp(fan["split"].iloc[0]) == pd.Timestamp("2024-02-04")
    numeric = xr.Dataset({"y": (("sample", "t"), np.ones((10, 6)))}, coords={"t": np.arange(6)})
    plot = get_recipe("core.ppc_fan").render({"draws": numeric}, split_at=3)
    assert frame(plot, "fan")["split"].iloc[0] == 3


def test_faceted_fan_gives_every_panel_its_own_date_axis_in_coordinate_order() -> None:
    """With 3 geos and 2 columns, a shared x axis floats under the empty cell and the top
    row has no date labels; each panel gets its own axis, panels keep the coordinate order
    and there are never more columns than panels."""
    plot = get_recipe("core.ppc_fan").render(ex.score_tree(), columns=2)
    spec = plot.spec
    assert spec["resolve"]["axis"]["x"] == "independent"
    assert spec["resolve"]["scale"]["y"] == "independent"
    assert spec["facet"]["sort"] == ex.GEOS != sorted(ex.GEOS)
    assert spec["columns"] == 2
    wide = get_recipe("core.ppc_fan").render(ex.score_tree(), columns=8)
    assert wide.spec["columns"] == len(ex.GEOS)


def test_facets_are_titled_with_the_dataset_s_own_names(bundle: Any) -> None:
    """A Guided geo MMM binds the analyst's column (``market``) to the spec dim ``geo``:
    panels used to be titled with the internal name."""
    tree = ex.score_tree()
    assert get_recipe("core.ppc_fan").render(tree).spec["facet"]["title"] == "geo"
    binding = {"dims": {"geo": "market", "channel": "Media"}, "vars": {}}
    tree["binding"] = xr.DataTree(xr.Dataset(attrs={"json": json.dumps(binding)}))
    assert RecipeInput(tree).dim_label("geo") == "market"
    assert RecipeInput(tree).dim_label("time") == "time"
    plot = get_recipe("core.ppc_fan").render(tree)
    assert plot.spec["facet"]["title"] == "market"
    bound = bundle.copy()
    bound["binding"] = xr.DataTree(xr.Dataset(attrs={"json": json.dumps(binding)}))
    curves = get_recipe("mmm.response_curves").render(bound, facet=True)
    assert curves.spec["facet"]["title"] == "Media"


def test_faceted_response_curves_give_every_panel_its_own_axes(bundle: Any) -> None:
    plot = get_recipe("mmm.response_curves").render(bundle, facet=True)
    assert plot.spec["resolve"]["axis"] == {"x": "independent", "y": "independent"}
    assert plot.spec["columns"] == len(ex.CHANNELS)


def test_fan_errors() -> None:
    summary = ex.score_tree(with_draws=False)["summary"].to_dataset()
    with pytest.raises(RecipeDataError, match="cannot be added up"):
        get_recipe("core.ppc_fan").render({"summary": summary}, max_facets=1)
    no_time = xr.Dataset({"y": (("sample", "k"), np.ones((5, 3)))})
    with pytest.raises(RecipeDataError, match="time-like"):
        get_recipe("core.ppc_fan").render({"draws": no_time})
    with pytest.raises(RecipeDataError, match="no predictions"):
        get_recipe("core.ppc_fan").render({"other": no_time, "x": no_time})
    with pytest.raises(RecipeDataError, match="rows"):
        get_recipe("core.ppc_fan").render(ex.score_tree(), max_rows=10)


def test_fit_vs_actual_and_residuals(scored: Any) -> None:
    plot = get_recipe("core.fit_vs_actual").render(scored)
    pts = frame(plot, "points")
    assert len(pts) == 60 and pts["observed"].notna().all()
    assert "R²" in caption(plot)
    res = get_recipe("core.residuals_over_time").render(scored)
    r = frame(res, "residuals")
    np.testing.assert_allclose(r["residual"], pts["observed"] - pts["point"], rtol=1e-4)
    base = ex.score_tree()
    tree = {"draws": base["draws"].to_dataset(), "observed": base["observed"].to_dataset() + 50}
    assert "under-predicts" in caption(get_recipe("core.residuals_over_time").render(tree))
    with pytest.raises(RecipeDataError, match="observed"):
        get_recipe("core.fit_vs_actual").render({"draws": base["draws"].to_dataset()})


def test_point_vs_actual_compare_summaries_per_cell() -> None:
    """Quantile/interval summaries over extra dims cannot be summed; the cell-wise recipes
    keep every cell instead (the fan still refuses)."""
    base = ex.score_tree(with_draws=False)
    summ = base["summary"].to_dataset()
    obs = base["observed"].to_dataset()
    sku = ["k0", "k1"]
    tree = {
        "summary": xr.concat([summ, summ * 1.1], dim=pd.Index(sku, name="sku")),
        "observed": xr.concat([obs, obs * 1.1], dim=pd.Index(sku, name="sku")),
    }
    with pytest.raises(RecipeDataError, match="cannot be added up"):
        get_recipe("core.ppc_fan").render(tree)
    pts = frame(get_recipe("core.fit_vs_actual").render(tree), "points")
    # geo is picked as the colour facet; the remaining sku dim labels each cell
    assert len(pts) == 2 * 60 and set(pts["group"]) == {"north", "south", "east"}
    assert set(pts["cell"]) == {"k0", "k1"}
    res = get_recipe("core.residuals_over_time").render(tree)
    r = frame(res, "residuals")
    np.testing.assert_allclose(r["residual"], pts["observed"] - pts["point"], atol=1e-3)
    assert "one sku cell" in caption(res)
    by_sku = frame(get_recipe("core.fit_vs_actual").render(tree, facet="sku"), "points")
    assert set(by_sku["group"]) == {"k0", "k1"} and set(by_sku["cell"]) == set(ex.GEOS)


def test_observed_variable_may_be_named_differently() -> None:
    base = ex.score_tree()
    obs = base["observed"].to_dataset().rename(revenue="revenue_local")
    data = {"draws": base["draws"].to_dataset(), "observed": obs}
    plot = get_recipe("core.ppc_fan").render(data)  # the only observed variable is used
    assert "observed" in frame(plot, "fan")
    two = obs.assign(other=obs["revenue_local"])
    assert "observed" not in frame(
        get_recipe("core.ppc_fan").render({**data, "observed": two}), "fan"
    )
    plot = get_recipe("core.fit_vs_actual").render(
        {**data, "observed": two}, observed_var="revenue_local"
    )
    assert len(frame(plot, "points")) == 60
    with pytest.raises(RecipeDataError, match="no observed variable"):
        get_recipe("core.ppc_fan").render(data, observed_var="nope")


# ------------------------------------------------------------------------ decomposition


def test_decomposition_components_and_shares(bundle: Any) -> None:
    plot = get_recipe("core.decomposition").render(bundle)
    tot = frame(plot, "totals")
    assert set(tot["component"].astype(str)) == {"tv", "search", "baseline"}
    assert tot["share"].sum() == pytest.approx(1.0)
    assert (tot["lo"] <= tot["mean"]).all() and (tot["mean"] <= tot["hi"]).all()
    mmm = get_recipe("mmm.decomposition").render(bundle)
    assert frame(mmm, "totals")["component"].tolist() == tot["component"].tolist()


def test_decomposition_point_estimates_and_vars() -> None:
    weeks = pd.date_range("2024-01-07", periods=4, freq="W")
    ds = xr.Dataset(
        {"base": ("week", np.full(4, 10.0)), "promo": ("week", np.array([1.0, -3.0, 2.0, -1.0]))},
        coords={"week": weeks},
    )
    plot = get_recipe("core.decomposition").render({"contributions": ds})
    tot = frame(plot, "totals").set_index("component")
    assert tot.loc["base", "mean"] == 40.0
    assert tot.loc["promo", "lo"] == tot.loc["promo", "mean"] == -1.0
    only = get_recipe("core.decomposition").render({"contributions": ds}, vars=["promo"])
    assert frame(only, "totals")["component"].tolist() == ["promo"]
    with pytest.raises(RecipeDataError):
        get_recipe("core.decomposition").render({"contributions": ds}, vars=["nope"])
    with pytest.raises(RecipeDataError, match="time-like"):
        get_recipe("core.decomposition").render(
            {"contributions": xr.Dataset({"a": ("k", [1.0, 2.0])})}
        )
    rng = np.random.default_rng(3)
    unsure = xr.Dataset(
        {"x": (("chain", "draw", "week"), rng.standard_normal((2, 50, 4)))}, coords={"week": weeks}
    )
    assert "not certain" in caption(
        get_recipe("core.decomposition").render({"contributions": unsure})
    )


def test_decomposition_skips_excluded_components_unless_named() -> None:
    weeks = pd.date_range("2024-01-07", periods=4, freq="W")
    ds = xr.Dataset(
        {
            "level": ("week", np.full(4, 10.0)),
            "trend": ("week", np.full(4, 0.5), {"decomposition": "exclude"}),
            "seasonal": ("week", np.array([1.0, -1.0, 1.0, -1.0])),
        },
        coords={"week": weeks},
    )
    plot = get_recipe("core.decomposition").render({"components": ds})
    assert frame(plot, "totals")["component"].tolist() == ["level", "seasonal"]
    named_ = get_recipe("core.decomposition").render({"components": ds}, vars=["level", "trend"])
    assert frame(named_, "totals")["component"].tolist() == ["level", "trend"]


def _legend_title(plot: PlotSpec) -> str:
    area = plot.spec["vconcat"][0]
    return str(area["encoding"]["color"]["title"])


def test_decomposition_legend_is_not_titled_channel_when_baseline_and_controls_join() -> None:
    weeks = pd.date_range("2024-01-07", periods=4, freq="W")
    ds = xr.Dataset(
        {
            "channel_contribution": (("week", "channel"), np.ones((4, 2))),
            "baseline": ("week", np.full(4, 5.0)),
            "holiday": ("week", np.zeros(4)),
        },
        coords={"week": weeks, "channel": ["tv", "search"]},
    )
    for rid in ("mmm.decomposition", "core.decomposition"):
        plot = get_recipe(rid).render({"contributions": ds})
        assert set(frame(plot, "totals")["component"]) == {"tv", "search", "baseline", "holiday"}
        assert _legend_title(plot) == "component", rid
    assert get_recipe("mmm.decomposition").title == "Contribution by component"
    only = get_recipe("mmm.decomposition").render(
        {"contributions": ds}, vars=["channel_contribution"]
    )
    assert _legend_title(only) == "channel"  # every component is a channel


# --------------------------------------------------------------------------- comparison


def test_model_comparison_waic_prefers_unbiased_model(bundle: Any) -> None:
    worse = ex.bundle_tree(bias=1.5)
    plot = get_recipe("core.model_comparison").render({"good": bundle, "worse": worse})
    comp = frame(plot, "comparison")
    assert comp["model"].astype(str).tolist() == ["good", "worse"]
    assert comp.loc[1, "d_elpd"] < 0 and comp.loc[0, "d_elpd"] == 0
    assert comp.loc[1, "dse"] > 0
    assert "good has the best" in caption(plot) and "WAIC" in caption(plot)


def test_model_comparison_from_precomputed_table() -> None:
    table = xr.Dataset(
        {
            "elpd_loo": ("model", [-100.0, -120.0, -101.0]),
            "se": ("model", [5.0, 6.0, 5.0]),
            "dse": ("model", [0.0, 4.0, 2.0]),
            "weight": ("model", [0.6, 0.0, 0.4]),
        },
        coords={"model": ["a", "b", "c"]},
    )
    render = get_recipe("core.model_comparison").render
    plot = render({"comparison": table})
    comp = frame(plot, "comparison")
    assert comp["model"].astype(str).tolist() == ["a", "c", "b"]
    assert set(comp["ic"].astype(str)) == {"loo"}
    assert "very small difference" in caption(plot)
    wider = table.assign(elpd_loo=("model", [-100.0, -120.0, -110.0]))
    assert "clear difference" in caption(render({"comparison": wider}))
    unsure = table.assign(
        elpd_loo=("model", [-100.0, -120.0, -110.0]), dse=("model", [0.0, 9.0, 9.0])
    )
    assert "smaller than its uncertainty" in caption(render({"comparison": unsure}))
    with pytest.raises(RecipeDataError):
        render({"comparison": table.rename(model="m")})
    with pytest.raises(RecipeDataError):
        render({"comparison": table.drop_vars("elpd_loo")})
    with pytest.raises(RecipeDataError, match="at least two"):
        render({"only": ex.bundle_tree()})


# ------------------------------------------------------------------------ counterfactual


def test_counterfactual_delta_distribution(cf_tree: Any) -> None:
    plot = get_recipe("core.counterfactual").render(cf_tree)
    summ = frame(plot, "delta_summary").iloc[0]
    assert summ["mean"] == pytest.approx(5 * 36, rel=0.1)
    assert summ["p_increase"] == 1.0
    assert set(frame(plot, "lines")["series"].astype(str)) == {"scenario", "baseline"}
    assert "Probability of an increase: 100%" in caption(plot)


def test_counterfactual_without_delta_or_draws(cf_tree: Any) -> None:
    no_delta = {
        "scenario": cf_tree["predictions"].to_dataset(),
        "baseline": cf_tree["baseline/predictions"].to_dataset(),
    }
    plot = get_recipe("core.counterfactual").render(no_delta)
    assert frame(plot, "delta_summary")["mean"].iloc[0] == pytest.approx(5 * 36, rel=0.1)
    summary = cf_tree["baseline/summary"].to_dataset()
    point_only = {"summary": summary.sum("geo") + 1, "baseline/summary": summary.sum("geo")}
    plot = get_recipe("core.counterfactual").render(point_only)
    assert "delta" not in plot.datasets and "no draws" in caption(plot)
    summed = get_recipe("core.counterfactual").render(
        {"summary": summary + 1, "baseline/summary": summary}
    )
    lines = frame(summed, "lines")
    assert (lines["lo"] == lines["hi"]).all()  # summed means carry no band
    no_mean = summary.sel(stat=["hdi_90_lower", "hdi_90_upper"])
    with pytest.raises(RecipeDataError, match="extra dims"):
        get_recipe("core.counterfactual").render({"summary": no_mean, "baseline/summary": no_mean})
    scen = cf_tree["predictions"].to_dataset()
    rebuilt = {
        "draws": scen,
        "baseline/summary": summary,
        "delta/draws": cf_tree["delta/predictions"].to_dataset(),
    }
    plot = get_recipe("core.counterfactual").render(rebuilt)
    base_line = frame(plot, "lines").query("series == 'baseline'")
    assert (base_line["lo"] < base_line["hi"]).all()  # baseline draws = scenario - delta
    with pytest.raises(RecipeDataError, match="scenario and baseline"):
        get_recipe("core.counterfactual").render({"summary": summary, "x": summary})


# ------------------------------------------------------------------------------ decision


def test_decision_with_candidates_and_single_objective() -> None:
    render = get_recipe("core.decision_distribution").render
    plot = render(ex.decision_dataset())
    summ = frame(plot, "summary")
    assert summ["p_best"].sum() == pytest.approx(1.0)
    assert str(summ.sort_values("p_best").iloc[-1]["option"]) == "shift_tv"
    single = render({"objective": ex.decision_dataset(candidates=False)})
    assert len(frame(single, "summary")) == 1 and "below zero" in caption(single)
    lower = render(ex.decision_dataset(), higher_is_better=False)
    assert str(frame(lower, "summary").sort_values("p_best").iloc[-1]["option"]) == "keep"
    bad = xr.Dataset({"o": (("chain", "draw", "a", "b"), np.zeros((1, 5, 2, 2)))})
    with pytest.raises(RecipeDataError):
        render({"decision": bad})
    with pytest.raises(RecipeDataError):
        render({"decision": bad}, candidate="zz")


# ----------------------------------------------------------------------------- MMM pack


def test_response_curves_and_saturation_caption(bundle: Any) -> None:
    plot = get_recipe("mmm.response_curves").render(bundle)
    curves = frame(plot, "curves")
    assert set(curves["channel"].astype(str)) == {"tv", "search"} and len(curves) == 42
    assert (curves["lo"] <= curves["hi"]).all()
    current = curves.groupby("channel", observed=True)["current_spend"].first()
    assert current.to_dict() == {"search": 25.0, "tv": 40.0}
    assert "closest to saturation" in caption(plot)
    assert "facet" in get_recipe("mmm.response_curves").render(bundle, facet=True).spec


def test_response_curves_with_per_channel_spend_grid() -> None:
    grid = np.arange(5.0)
    spend = np.stack([grid * 10, grid * 20])
    ds = xr.Dataset(
        {
            "response": (("sample", "channel", "g"), np.repeat(np.sqrt(spend)[None], 3, axis=0)),
            "spend": (("channel", "g"), spend),
        },
        coords={"channel": ["a", "b"]},
    )
    plot = get_recipe("mmm.response_curves").render({"response_curves": ds})
    curves = frame(plot, "curves")
    assert curves.loc[curves.channel == "b", "spend"].max() == 80.0
    assert "current_spend" not in curves.columns
    point = xr.Dataset({"response": (("channel", "g"), np.sqrt(spend))}, coords={"g": grid})
    c = frame(get_recipe("mmm.response_curves").render({"response_curves": point}), "curves")
    assert (c["lo"] == c["hi"]).all()
    with pytest.raises(RecipeDataError):
        get_recipe("mmm.response_curves").render({"response_curves": point}, channel="geo")
    # per-geo scales next to the response are never mistaken for it, whatever the order
    scaled = xr.Dataset({"target_scale": ("geo", [2.0, 3.0])}).merge(ds)
    assert next(iter(scaled.data_vars)) == "target_scale"
    again = frame(get_recipe("mmm.response_curves").render({"response_curves": scaled}), "curves")
    assert again.loc[again.channel == "b", "spend"].max() == 80.0


def test_marginal_cost_and_roi(bundle: Any) -> None:
    cost = frame(get_recipe("mmm.marginal_cost").render(bundle), "marginal")
    roi = frame(get_recipe("mmm.marginal_cost").render(bundle, metric="roi"), "marginal")
    tv_cost = cost[cost.channel == "tv"]["point"].to_numpy()
    tv_roi = roi[roi.channel == "tv"]["point"].to_numpy()
    assert np.all(np.diff(tv_roi[1:-1]) < 0)  # saturating: each extra unit returns less
    assert np.all(np.diff(tv_cost[1:-3]) > 0)  # ... and costs more (top clipped)


def test_budget_plans() -> None:
    ds = ex.plans_dataset()
    render = get_recipe("mmm.budget_plans").render
    plot = render({"plans": ds})
    out = frame(plot, "outcomes").set_index("plan")
    assert np.isnan(out.loc["current", "p_better"]) and out.loc["current", "delta"] == 0
    assert out.loc["plan_b", "delta"] > 0 and out.loc["plan_b", "total_spend"] == 80
    assert "plan_b is expected to deliver" in caption(plot)
    ref = render({"plans": ds}, reference="plan_b")
    assert frame(ref, "outcomes").set_index("plan").loc["plan_b", "delta"] == 0
    with pytest.raises(RecipeDataError):
        render({"plans": ds}, reference="zzz")
    with pytest.raises(RecipeDataError):
        render({"plans": ds.drop_vars("outcome")})
    with pytest.raises(RecipeDataError):
        render({"plans": ds.rename(plan="option")})


# ------------------------------------------------------------------------ custom recipes


def test_custom_recipe_as_documented() -> None:
    import altair as alt
    from mapi_plots.recipes import Figure, RecipeSettings, named, recipe

    class Settings(RecipeSettings):
        var: str = "revenue"

    @recipe("acme.weekly_total", title="Weekly total", settings=Settings, persona="guided")
    def weekly_total(inp: RecipeInput, s: Settings) -> Figure:
        """Total of the mean forecast per week."""
        ds = inp.require("summary")
        frame = ds[s.var].sel(stat="mean").sum("geo").to_dataframe().reset_index()
        chart = alt.Chart(named("weekly")).mark_line().encode(x="week:T", y=f"{s.var}:Q")
        return Figure(chart, {"weekly": frame}, caption=f"Total {s.var} per week.")

    plot = weekly_total.render(ex.score_tree())
    assert plot.persona == "guided" and plot.provenance.recipe == "acme.weekly_total@1.0.0"
    assert len(frame(plot, "weekly")) == 28
    assert weekly_total.summary == "Total of the mean forecast per week."
    registry_mod = pytest.importorskip("mapi_sdk.registry")
    reg = registry_mod.Registry()
    reg.register(weekly_total)
    assert reg.plot("acme.weekly_total") is weekly_total


# ------------------------------------------------------------------------- registration


def test_entry_points_and_sdk_registry() -> None:
    from importlib.metadata import entry_points

    eps = {
        ep.name: ep for ep in entry_points(group="mapi.plots") if ep.value.startswith("mapi_plots")
    }
    assert {"core", "mmm", "ml"} <= set(eps)
    loaded = eps["core"].load()()
    assert [r.id for r in loaded] == [r.id for r in standard_recipes()]
    registry_mod = pytest.importorskip("mapi_sdk.registry")
    reg = registry_mod.Registry()
    errors = reg.load_entry_points(groups=("mapi.plots",))
    assert not [e for e in errors if "mapi_plots" in e]
    assert reg.plot("core.ppc_fan@^1") is get_recipe("core.ppc_fan")
    assert isinstance(reg.plot("mmm.budget_plans"), registry_mod.PlotRecipe)
    assert {p.id for p in reg.catalog().plots} >= PLAN_IDS


# ------------------------------------------------------------------------------ ml pack


def test_feature_importance_ranks_importances_and_falls_back_to_coefficients() -> None:
    tree = ex.feature_importance_tree()
    render = get_recipe("ml.feature_importance").render
    imp = frame(render(tree), "features")
    assert imp["feature"].iloc[0] == "price"
    assert imp["value"].is_monotonic_decreasing
    assert "42%" in caption(render(tree))
    top2 = frame(render(tree, top=2), "features")
    assert len(top2) == 2 and "top 2 of 5" in caption(render(tree, top=2))
    coef = render(tree, source="coefficients")
    cf = frame(coef, "features")
    # ranked by |coefficient|, signed values and the per-quantile-model range are kept
    assert cf["feature"].iloc[0] == "price" and cf["value"].iloc[0] == pytest.approx(-1.5)
    assert cf["lo"].iloc[0] == pytest.approx(-1.8) and cf["hi"].iloc[0] == pytest.approx(-1.2)
    assert "lowers" in caption(coef)
    only_coef = {"coefficients": tree["model/coefficients"].to_dataset()}
    assert frame(render(only_coef), "features")["feature"].iloc[0] == "price"


def test_feature_importance_needs_a_known_group() -> None:
    render = get_recipe("ml.feature_importance").render
    with pytest.raises(RecipeDataError):
        render(ex.score_tree())
    with pytest.raises(RecipeDataError):
        render(ex.feature_importance_tree(), source="importance", feature_dim="column")


# ------------------------------------------------------------------------------ colours

_COLOUR = re.compile(r"^(#[0-9a-fA-F]{3,8}|white|black|red|blue|green|gray|grey|orange)$")
_LIGHT_CARD, _DARK_CARD = "#ffffff", "#1b212d"


_SCALE_COLOUR = re.compile(r"^rgba?\([0-9., ]+\)$")
# Colours sampled from a colour scale encode data (a category or a value), as a colorscale
# does, so the sandbox does not swap them in dark mode. Only these trace keys may hold them.
_DATA_COLOUR_KEYS = {
    ("net.graph", "marker.color"),  # node groups (category_colours)
    ("sci.point_pattern", "marker.color"),  # point groups (category_colours)
    ("sci.area_map", "fillcolor"),  # the value class of each area
    ("sci.profile_bands", "fillcolor"),  # one colour for each curve of `by`
    ("sci.profile_bands", "line.color"),
    ("sci.profile_bands", "marker.color"),
    ("sci.vector_field", "line.color"),  # arrows: dark or light by the field underneath
}


def _colours(
    node: Any, out: set[str], scale: set[tuple[str, ...]] | None = None, path: tuple[str, ...] = ()
) -> set[str]:
    """Explicit colours of a spec. A Vega-Lite ``data`` block and the ``datasets`` hold data;
    a Plotly ``data`` holds the traces, which are searched too (``scale`` collects the key
    paths of colours sampled from a colour scale)."""
    if isinstance(node, dict):
        if set(node) == {"$ref"}:
            return out
        for k, v in node.items():
            if k == "datasets" or k == "colorscale":
                continue
            if k == "data" and not (path == () and isinstance(v, list) and scale is not None):
                continue
            _colours(v, out, scale, (*path, k))
    elif isinstance(node, list):
        for v in node:
            _colours(v, out, scale, path)
    elif isinstance(node, str) and _COLOUR.match(node):
        out.add(node.lower())
    elif isinstance(node, str) and scale is not None and _SCALE_COLOUR.match(node):
        scale.add(path)
    return out


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    rgb = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_recipes_use_only_palette_colours_and_every_one_has_a_dark_variant() -> None:
    """Explicit mark colours override any theme config, so recipes draw only palette colours,
    which the plot sandbox swaps for their dark variants in dark mode (no black dots on the
    dark card)."""
    from mapi_plots.palette import PALETTE, dark_variants

    used: set[str] = set()
    scale_keys: set[tuple[str, str]] = set()
    for recipe_id, (data, options) in EXAMPLES.items():
        plot = get_recipe(recipe_id).render(data, **options)
        paths: set[tuple[str, ...]] = set()
        _colours(plot.spec, used, paths if plot.kind == "plotly" else None)
        scale_keys |= {(recipe_id, ".".join(p[1:])) for p in paths}
    variants = dark_variants()
    assert used, "the recipes use no explicit colours at all?"
    assert used <= set(variants), sorted(used - set(variants))
    assert scale_keys <= _DATA_COLOUR_KEYS, sorted(scale_keys - _DATA_COLOUR_KEYS)
    for role, swatch in PALETTE.items():
        assert variants[swatch.light] == swatch.dark, role
        if swatch.legible:  # strokes, dots and text: WCAG 3:1 for graphics on both cards
            assert _contrast(swatch.light, _LIGHT_CARD) >= 3.0, role
            assert _contrast(swatch.dark, _DARK_CARD) >= 3.0, role


def test_the_plot_sandbox_mirrors_the_dark_variants() -> None:
    from mapi_plots.palette import dark_variants

    ts = Path(__file__).parents[3] / "apps" / "plot-sandbox" / "src" / "recipe-palette.ts"
    if not ts.parent.is_dir():
        pytest.skip("the plot sandbox sources are not in this checkout")
    pairs = dict(re.findall(r'"(#[0-9a-f]{6})":\s*"(#[0-9a-f]{6})"', ts.read_text()))
    assert pairs == dark_variants()
