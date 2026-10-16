"""Guided-mode outputs computed in the runner (M3), in process against tiny real fits:

* the sanity scorecard of a fit (every check measured or skipped with a reason, template
  selection, the promotion gate it drives);
* the what-if grid view, and "Run exact" agreeing with the grid at grid points;
* the forecast view (history + fan + split) and the counterfactual view;
* lift tests checked against the raw data.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from types import SimpleNamespace
from typing import Any

import mapi_steps
import numpy as np
import pytest
import xarray as xr
from mapi_plots.recipes import all_recipes, render_plots
from mapi_plugin_mmm.examples import E33_SPEC_YAML, e33_dataset, e33_raw_schema, e33_side_tables
from mapi_plugin_mmm.plugins import PLUGINS
from mapi_runner.worker.guided import (
    check_lift_tests,
    counterfactual_view,
    forecast_view,
    response_grid_view,
)
from mapi_runner.worker.scorecard import _holdout, _prior_overlap, fit_scorecard
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import ScoreRequest, load_model_spec, load_score_request
from mapi_spec.errors import ErrorCode, ValidationFailed
from mapi_spec.scorecard import SCORECARD_CHECKS, GateOutcome, evaluate_check, promotion_gate
from mapi_spec.spec import ModelSpec
from mapi_spec.whatif import (
    ChannelOff,
    WhatIfScenario,
    counterfactual_request,
    evaluate_scenario,
    scenario_score_request,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

MODEL = "model://growth/eu-geo-mmm@v1"
FX = {"fx": "dataset://growth/fx_daily@latest"}


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = mapi_steps.registry()
    reg.register(all_recipes())
    for plugin in PLUGINS:
        reg.register_model(plugin)
    return reg


@pytest.fixture(scope="module")
def e33() -> tuple[xr.Dataset, dict[str, xr.Dataset]]:
    data, _ = e33_dataset(markets=("NL", "DE", "UK"), channels=("tv", "search"), weeks=30, seed=1)
    return data, e33_side_tables()


@pytest.fixture(scope="module")
def bundle(registry: Registry, e33: tuple[xr.Dataset, dict[str, xr.Dataset]]) -> Bundle:
    text = (
        E33_SPEC_YAML.replace("last: 8", "last: 4")
        .replace("tune: 150", "tune: 60")
        .replace("ui_draws: 100", "ui_draws: 200")
    )
    data, tables = e33
    return fit_spec(load_model_spec(text), data, tables, registry, raw_schema=e33_raw_schema(data))


def _request(body: dict[str, Any]) -> ScoreRequest:
    body = {**body, "side_tables": FX}
    return load_score_request(__import__("json").dumps(body))


# ============================================================================ scorecard


def test_the_scorecard_measures_every_check_it_can(
    bundle: Bundle, e33: tuple[xr.Dataset, dict[str, xr.Dataset]]
) -> None:
    card = fit_scorecard(bundle, e33[0])
    checks = {i.check for i in card.items}
    assert checks | set(card.skipped) == set(SCORECARD_CHECKS)
    assert checks >= {
        "convergence.rhat",
        "convergence.ess",
        "convergence.divergences",
        "holdout.mape",
        "holdout.coverage_90",
        "plausibility.negative_roi",
        "plausibility.roi_absurd",
        "plausibility.share_outlier",
        "plausibility.baseline_share",
        "data.missing",
        "data.history",
        "data.spend_variation",
    }
    # the MMM keeps cheap prior draws of its media parameters; only choice models have a
    # hit rate
    assert set(card.skipped) == {"holdout.hit_rate"}
    assert "categorical target" in card.skipped["holdout.hit_rate"]
    overlap = next(i for i in card.items if i.check == "plausibility.prior_overlap")
    assert overlap.message_expert is not None
    assert any(p in overlap.message_expert for p in ("beta_channel", "alpha", "lam"))
    for item in card.items:
        assert item.message_plain and item.docs_anchor and item.category and item.metric
        assert item.message_expert  # what was measured, on which parameter/channel
        assert (item.suggested_action is None) == (item.status == "pass")
    by = {i.check: i for i in card.items}
    assert by["data.history"].value == pytest.approx(30 / 52)  # 30 weeks, in years
    # mapi_sdk reports MAPE in percent; the check's thresholds are fractions.
    assert by["holdout.mape"].value == pytest.approx(bundle.metrics.values["mape"] / 100)
    assert by["plausibility.negative_roi"].value == 0.0  # positive media effects
    assert 0 < by["plausibility.baseline_share"].value < 1
    # the rollup and the gate it drives
    worst = {"pass": 0, "warn": 1, "fail": 2}
    assert worst[card.overall] == max(worst[i.status] for i in card.items)
    gate = promotion_gate(card)
    if card.overall == "fail":
        assert gate.outcome is GateOutcome.BLOCKED
    assert set(gate.failing_checks) == {i.check for i in card.items if i.status != "pass"}


def test_a_guided_spec_runs_its_template_checks(
    bundle: Bundle, e33: tuple[xr.Dataset, dict[str, xr.Dataset]]
) -> None:
    labels = {**bundle.spec.metadata.labels, "guided": "weekly_forecast"}
    spec = bundle.spec.model_copy(
        update={"metadata": bundle.spec.metadata.model_copy(update={"labels": labels})}
    )
    card = fit_scorecard(dataclasses.replace(bundle, spec=spec), e33[0])
    wanted = {
        "convergence.rhat",
        "convergence.ess",
        "convergence.divergences",
        "holdout.mape",
        "holdout.coverage_90",
        "data.missing",
        "data.history",
    }
    assert {i.check for i in card.items} | set(card.skipped) == wanted
    history = next(i for i in card.items if i.check == "data.history")
    assert history.status == "warn" and history.threshold == 2  # template: fail below 0.5


def test_the_choice_template_checks_the_holdout_hit_rate(
    bundle: Bundle, e33: tuple[xr.Dataset, dict[str, xr.Dataset]]
) -> None:
    labels = {**bundle.spec.metadata.labels, "guided": "customer_choice"}
    spec = bundle.spec.model_copy(
        update={"metadata": bundle.spec.metadata.model_copy(update={"labels": labels})}
    )
    values = {"accuracy": 0.48, "accuracy_naive": 0.55, "accuracy_lift": -0.07}
    report = bundle.metrics.model_copy(update={"values": values, "n_holdout": 40})
    card = fit_scorecard(dataclasses.replace(bundle, spec=spec, metrics=report), e33[0])
    [item] = card.items
    assert item.check == "holdout.hit_rate" and item.status == "warn"
    assert item.message_plain.endswith("(-7% difference).")
    assert item.message_expert is not None and "0.480 vs 0.550" in item.message_expert
    empty = report.model_copy(update={"values": {}})
    missing = dataclasses.replace(bundle, spec=spec, metrics=empty)
    assert "holdout.hit_rate" in fit_scorecard(missing, e33[0]).skipped


def test_prior_posterior_overlap() -> None:
    rng = np.random.default_rng(0)
    prior = xr.Dataset(
        {"a": (("chain", "draw"), rng.normal(0, 1, (1, 500))),
         "b": (("chain", "draw", "channel"), rng.normal(0, 1, (1, 500, 2)))}
    )  # fmt: skip
    post = xr.Dataset(
        {"a": (("chain", "draw"), rng.normal(3, 0.1, (2, 200))),
         "b": (("chain", "draw", "channel"), np.stack([rng.normal(2, 0.2, (2, 200)),
                                                       rng.normal(0, 1, (2, 200))], -1))}
    )  # fmt: skip
    fake = SimpleNamespace(model=SimpleNamespace(groups={"prior": prior, "posterior": post}))
    out: dict[str, Any] = {}
    why: dict[str, str] = {}
    _prior_overlap(fake, out, why)  # type: ignore[arg-type]
    value, expert = out["prior_overlap_max"]
    assert value > 0.8 and "b[1]" in expert  # the second channel learned nothing


def test_fit_plots_of_the_evaluation_title_panels_with_the_dataset_s_names(
    bundle: Bundle, registry: Registry, e33: tuple[xr.Dataset, dict[str, xr.Dataset]]
) -> None:
    """E33 binds ``geo: market``: the holdout fan chart's panels said 'geo'."""
    from mapi_plots.recipes import get_recipe
    from mapi_plots.recipes._base import RecipeInput
    from mapi_runner.worker.fit import _evaluation_tree

    ctx = SimpleNamespace(registry=registry, warning=lambda *a, **k: None)
    tree = _evaluation_tree(ctx, bundle.spec, e33[0], bundle, e33[1])  # type: ignore[arg-type]
    assert tree is not None
    assert RecipeInput(tree).dim_label("geo") == "market"
    fan = get_recipe("core.ppc_fan").render(tree)
    assert fan.spec["facet"]["title"] == "market"


# ========================================================================= what-if grid


def test_the_grid_view_and_run_exact_agree_at_grid_points(
    bundle: Bundle, registry: Registry, e33: tuple[xr.Dataset, dict[str, xr.Dataset]]
) -> None:
    grid = response_grid_view(bundle)
    assert grid is not None
    rc = bundle.model.derived["response_curves"]
    assert grid.channels == ["tv", "search"] and grid.geos == ["NL", "DE", "UK"]
    assert grid.draws == 200 and grid.kpi == "revenue_local" and grid.kpi_units == "EUR"
    assert grid.periods == 30 and grid.baseline is not None
    np.testing.assert_allclose(grid.current_spend, rc["current_spend"].values, rtol=1e-10)
    # expected KPI at actual spend = baseline + channels
    one = grid.multipliers.index(1.0)
    totals = [grid.baseline[d] + sum(c[one] for c in grid.contribution[d]) for d in range(5)]
    assert all(t > 0 for t in totals)
    scenario = WhatIfScenario(multipliers={"tv": 0.5, "search": 1.5})
    answer = evaluate_scenario(grid, scenario)
    req = scenario_score_request(scenario, MODEL, spend_var="spend_local")
    body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
    body["output"] = {
        "kind": "expected",
        "draws": grid.draws,
        "aggregate": {"over": ["market", "date"], "how": "sum"},
    }
    tree = score_bundle(bundle, _request(body), e33[0], side_tables=e33[1], registry=registry)
    node = tree["delta/draws"]
    assert isinstance(node, xr.DataTree)
    exact = np.asarray(node.to_dataset()["y_hat"].values)
    # per draw, the same thinned draws: identical up to float rounding
    mean = float(exact.mean())
    assert answer.kpi_delta.mean == pytest.approx(mean, rel=1e-9)
    np.testing.assert_allclose(
        answer.kpi_delta.quantiles, np.quantile(exact, answer.kpi_delta.levels), rtol=1e-9
    )
    assert answer.spend_delta == pytest.approx(
        -0.5 * grid.current_spend[0] + 0.5 * grid.current_spend[1], rel=1e-9
    )


# ======================================================================= score views


def test_the_counterfactual_view_of_a_channel_off_preset(
    bundle: Bundle, registry: Registry, e33: tuple[xr.Dataset, dict[str, xr.Dataset]]
) -> None:
    req = counterfactual_request(ChannelOff(channel="tv"), MODEL, spend_var="spend_local")
    body = req.model_dump(mode="json", by_alias=True, exclude_none=True)
    tree = score_bundle(
        bundle, _request(body), e33[0], side_tables=e33[1], registry=registry, return_full=True
    )
    view = counterfactual_view(bundle, tree, _request(body))
    assert view is not None
    assert view.kpi == "y_hat" and view.units == "EUR" and view.dim == "date"
    assert view.delta_total.mean < 0 and view.delta_total.quantiles["0.95"] < 0
    assert view.delta_share is not None and -1 < view.delta_share.mean < 0
    assert view.summed_over == ["market"]
    assert view.interventions == [{"var": "spend_local", "sel": {"channel": "tv"}, "set": 0.0}]
    assert view.delta_fan is not None and len(view.delta_fan.coords) == 30
    assert view.scenario_total.mean == pytest.approx(
        view.baseline_total.mean + view.delta_total.mean, rel=1e-9
    )


def test_the_forecast_view_has_history_a_split_and_a_fan(registry: Registry) -> None:
    from mapi_plugin_ts.datasets import CO2_SCHEMA, CO2_SPEC_YAML, co2_dataset
    from mapi_sdk.registry import default_registry
    from mapi_spec.whatif import forecast_request

    reg = default_registry(reload=True)
    co2 = co2_dataset(years=6, seed=0)
    fitted = fit_spec(CO2_SPEC_YAML, co2, registry=reg, raw_schema=CO2_SCHEMA)
    req = forecast_request("model://ts/co2@v1", periods=12, dim="t")
    tree = score_bundle(fitted, req, None, registry=reg, return_full=True)
    got = forecast_view(fitted, tree, req, registry=reg, tables={})
    assert got is not None
    view, plot_tree = got
    n = view.history
    assert n > 60 and view.dim == "date" and view.units == "ppm"
    assert len(view.fan.coords) == n + 12 and view.split_at == view.fan.coords[n]
    assert all(v is None for v in view.fan.values[0][:n])
    assert all(v is not None for v in view.fan.values[0][n:])
    assert view.fan.observed is not None and view.fan.observed[n - 1] is not None
    assert view.fan.observed[n] is None
    assert view.total is not None and view.total.mean > 0
    plots = render_plots(plot_tree, ["core.ppc_fan"], registry=reg).to_json()
    assert plots and "dashed line marks where the training data ends" in str(
        plots[0]["caption_plain"]
    )


# ========================================================================== lift tests


def _spec_with(tests: list[dict[str, Any]]) -> ModelSpec:
    text = E33_SPEC_YAML + "calibration:\n  lift_tests: " + __import__("json").dumps(tests) + "\n"
    return load_model_spec(text)


def test_lift_tests_are_checked_against_the_raw_data(
    e33: tuple[xr.Dataset, dict[str, xr.Dataset]],
) -> None:
    base = {
        "channel": "tv",
        "geo": "DE",
        "start": "2023-03-06",
        "end": "2023-04-02",
        "spend_delta": -100.0,
        "kpi_delta": -80.0,
        "se": 10.0,
    }
    check_lift_tests(_spec_with([base]), e33[0])  # fine
    bad = [
        {**base, "channel": "radio"},
        {**base, "geo": "FR"},
        {**base, "start": "2020-01-06", "end": "2020-02-02"},
    ]
    with pytest.raises(ValidationFailed) as err:
        check_lift_tests(_spec_with(bad), e33[0])
    pointers = {i.pointer for i in err.value.issues}
    assert pointers == {
        "/calibration/lift_tests/0/channel",
        "/calibration/lift_tests/1/geo",
        "/calibration/lift_tests/2/start",
    }
    assert {i.code for i in err.value.issues} == {ErrorCode.SCHEMA_INVALID}
    assert date(2023, 3, 6) < date(2023, 4, 2)


@pytest.mark.unit
def test_holdout_mape_in_percent_is_judged_as_a_fraction() -> None:
    """metrics.json carries MAPE in percent; a 1.4% error passes, a 14% one warns."""
    for pct, status, words in ((1.4, "pass", "1%"), (14.0, "warn", "14%"), (40.0, "fail", "40%")):
        report = SimpleNamespace(values={"mape": pct}, n_holdout=8, skipped={})
        out: dict[str, Any] = {}
        _holdout(SimpleNamespace(metrics=report), out, {})  # type: ignore[arg-type]
        value, expert = out["mape"]
        assert value == pytest.approx(pct / 100)
        assert f"mape {pct:.4g}% over 8" in expert
        item = evaluate_check("holdout.mape", value, message_expert=expert)
        assert item.status == status
        assert f"off by {words}" in item.message_plain
