"""Recipes on real SDK artifacts: a toy E33-like bundle and its score outputs."""

from __future__ import annotations

import json
from typing import Any

import pytest

xr = pytest.importorskip("xarray")
pytest.importorskip("altair")
harness = pytest.importorskip("mapi_sdk.harness")
toy = pytest.importorskip("mapi_sdk.testing.toy")

from mapi_plots.convert import arrow_to_frame  # noqa: E402
from mapi_plots.recipes import get_recipe  # noqa: E402
from mapi_plots.sanitize import sanitize_plot  # noqa: E402
from mapi_spec import load_model_spec, load_score_request  # noqa: E402

pytestmark = pytest.mark.integration

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: toy-geo, project: growth}
data:
  dataset: dataset://growth/eu_media_daily@latest
  side_tables:
    fx: {ref: dataset://growth/fx_daily@latest, score_policy: pinned}
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 4, after_step: weekly}
pipeline:
  - {id: to_eur, use: toy.fx@^1, with: {vars: [y, spend], table: fx, table_along: date}}
  - {id: weekly, use: toy.resample@^1, with: {dim: time, freq: W-SUN}}
  - {id: scale_spend, use: toy.scale@^1, with: {vars: [spend], over: [time]}}
  - {id: scale_y, use: toy.scale@^1, with: {vars: [y], over: [time]}}
model:
  use: toy.bayes_linear@^1
  fit: {mode: evaluate_then_refit, seed: 7, draws: 60, chains: 2}
evaluate: {metrics: [rmse]}
"""
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/toy\n"


@pytest.fixture(scope="module")
def fitted() -> tuple[Any, Any, Any]:
    registry = toy.toy_registry()
    data, fx = toy.panel_dataset(days=7 * 24)
    bundle = harness.fit_spec(load_model_spec(SPEC), data, {"fx": fx}, registry)
    return bundle, registry, data


def test_bundle_tree_feeds_the_inference_recipes(fitted: tuple[Any, Any, Any]) -> None:
    bundle, _, _ = fitted
    tree = bundle.to_tree()
    for rid in ("core.convergence", "core.posterior_forest"):
        plot = get_recipe(rid).render(tree)
        assert plot.provenance.bundle_id == bundle.bundle_id
        sanitize_plot(plot)


def test_evaluate_output_feeds_fan_and_fit_vs_actual(fitted: tuple[Any, Any, Any]) -> None:
    bundle, registry, data = fitted
    holdout = data.isel(date=slice(-28, None))
    out = harness.score_bundle(
        bundle.evaluation,
        load_score_request(
            REQ + "mode: evaluate\ndata: dataset://growth/holdout@v1\noutput: {draws: 40}\n"
        ),
        holdout,
        registry=registry,
        metrics=["rmse"],
        return_full=True,
    )
    fan = get_recipe("core.ppc_fan").render(out, facet="market")
    frame = arrow_to_frame(fan.datasets["fan"])
    assert {"observed", "lo_90", "hi_90"} <= set(frame.columns)
    assert set(frame["group"].astype(str)) == set(map(str, holdout["market"].values))
    fit = get_recipe("core.fit_vs_actual").render(out)
    assert "R²" in (fit.caption_plain or "")


def test_counterfactual_output_feeds_the_counterfactual_recipe(
    fitted: tuple[Any, Any, Any],
) -> None:
    bundle, registry, data = fitted
    window = data.isel(date=slice(-28, None))
    cf = harness.score_bundle(
        bundle,
        load_score_request(
            REQ
            + "mode: counterfactual\n"
            + "interventions: [{var: spend_local, sel: {channel: tv}, multiply: 1.5}]\n"
            + "output: {draws: 40}\n"
        ),
        window,
        registry=registry,
    )
    plot = get_recipe("core.counterfactual").render(cf)
    summary = arrow_to_frame(plot.datasets["delta_summary"]).iloc[0]
    expected = cf["delta/summary"].to_dataset()["y_hat"].sel(stat="mean").sum().item()
    assert summary["mean"] == pytest.approx(expected, rel=0.2)
    assert json.loads(cf.attrs["output_space"])  # a real score tree
