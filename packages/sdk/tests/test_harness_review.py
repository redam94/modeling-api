"""Regression tests for the M0 review findings on the fit/score harness (spec-sdk area)."""

from __future__ import annotations

import json
from typing import Any

import pytest
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import load_model_spec, load_score_request
from mapi_spec.errors import ErrorCode, MapiError

REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/toy\n"


def req(extra: str = "mode: predict\n") -> Any:
    return load_score_request(REQ + extra)


LOG_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: toy-log, project: growth}
data:
  dataset: dataset://growth/sales@latest
  bind: {dims: {geo: market, time: date}, vars: {y: revenue_local}}
  split: {kind: time_holdout, dim: time, last: 3}
pipeline:
  - {id: weekly, use: toy.resample@^1, with: {dim: time, freq: W-SUN}}
  - {id: logy, use: toy.log1p@^1, with: {vars: [y]}}
  - {id: season, use: toy.fourier@^1, with: {along: time, period: 52.18, order: 2}}
model:
  use: toy.linear@^1
  settings: {features: [fourier], retransform: __RT__}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse, mae]}
"""


# ------------------------------------------------------------------- statistics-1 (smearing)


def test_smearing_residuals_come_from_the_target_shaped_output(
    registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    """``contributions`` (extra ``component`` dim) must not overwrite the y_hat residuals."""
    data = panel[0][["revenue_local"]]
    smear = fit_spec(load_model_spec(LOG_SPEC.replace("__RT__", "smearing")), data, None, registry)
    r = smear.residuals["y"].values
    # in-sample least-squares residuals of y_hat have (numerically) zero mean
    assert abs(float(r.mean())) < 1e-6
    median = fit_spec(load_model_spec(LOG_SPEC.replace("__RT__", "median")), data, None, registry)
    horizon = req("mode: forecast\nhorizon: {dim: time, periods: 3}\noutput: {names: canonical}\n")
    ys = score_bundle(smear, horizon, None, registry=registry)["summary"].to_dataset()["y_hat"]
    ym = score_bundle(median, horizon, None, registry=registry)["summary"].to_dataset()["y_hat"]
    # smearing corrects the retransformation bias by a small factor, never by an order of magnitude
    ratio = (ys / ym).values
    assert bool((ratio >= 0.999).all()) and bool((ratio < 1.5).all())
    assert (
        json.loads(score_bundle(smear, horizon, None, registry=registry).attrs["retransform"])[
            "y_hat"
        ]
        == "smearing"
    )


# ------------------------------------------------- plan-conformance-1 (counterfactual scenario)


@pytest.fixture
def prod(
    registry: Registry,
    e33_spec: Any,
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: Any,
) -> Bundle:
    data, fx = panel
    return fit_spec(e33_spec(), data, {"fx": fx}, registry, raw_schema=raw_schema)


CF_SCENARIO = (
    "mode: counterfactual\ndata: dataset://growth/base@v1\nscenario: dataset://growth/plan@v1\n"
)


def test_counterfactual_scores_the_scenario_against_the_baseline(
    prod: Bundle, registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = panel
    base = data.isel(date=slice(-28, None))
    plan = base.assign(spend_local=base["spend_local"] * 2)
    out = score_bundle(prod, req(CF_SCENARIO), base, scenario=plan, registry=registry)
    delta = out["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert bool((delta > 0).all())
    # interventions apply on top of the scenario
    both = score_bundle(
        prod,
        req(CF_SCENARIO + "interventions: [{var: spend_local, multiply: 1.5}]\n"),
        base,
        scenario=plan,
        registry=registry,
    )
    d2 = both["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert bool((d2 > delta).all())


def test_counterfactual_scenario_needs_a_baseline(
    prod: Bundle, registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = panel
    plan = data.isel(date=slice(-28, None))
    only_scenario = req("mode: counterfactual\nscenario: dataset://growth/plan@v1\n")
    with pytest.raises(MapiError) as err:
        score_bundle(prod, only_scenario, None, scenario=plan, registry=registry)
    assert err.value.code is ErrorCode.SCHEMA_INVALID and err.value.pointer == "/data"
    # a request naming a scenario that the caller did not supply is not silently a no-op
    with pytest.raises(MapiError) as err2:
        score_bundle(prod, req(CF_SCENARIO), plan, registry=registry)
    assert err2.value.code is ErrorCode.SCHEMA_INVALID and err2.value.pointer == "/scenario"


async def test_inproc_executor_resolves_baseline_and_scenario(
    prod: Bundle, registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    from mapi_sdk.executors import InprocExecutor
    from mapi_spec.executor import ExecState, RunKind, RunSubmit

    data, _ = panel
    base = data.isel(date=slice(-28, None))
    ex = InprocExecutor(
        registry=registry,
        resolver={
            "dataset://growth/base@v1": base,
            "dataset://growth/plan@v1": base.assign(spend_local=base["spend_local"] * 2),
        },
    )
    ex.put_bundle("b", prod)
    request = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://growth/toy",
        "mode": "counterfactual",
        "data": "dataset://growth/base@v1",
        "scenario": "dataset://growth/plan@v1",
    }
    h = await ex.submit(
        RunSubmit(run_id="cf-1", kind=RunKind.SCORE, payload={"request": request, "bundle": "b"}),
        nonce="nonce-cf-0001",
    )
    status = await ex.wait(h)
    assert status.state is ExecState.SUCCEEDED, status.problem
    tree = ex.result(h)
    delta = tree["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert float(abs(delta).max()) > 1.0


# ------------------------------------------------- quality-security-1 (pinned side tables)


def test_pinned_side_tables_cannot_be_replaced_at_score(
    prod: Bundle, registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    window = data.isel(date=slice(-28, None))
    assert prod.resolved.side_tables["fx"].value == "pinned"
    named = req("mode: predict\nside_tables: {fx: dataset://growth/fx_other@v2}\n")
    with pytest.raises(MapiError) as err:
        score_bundle(prod, named, window, side_tables={"fx": fx * 5}, registry=registry)
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    assert err.value.pointer == "/side_tables/fx"
    # a pinned table handed over by the caller (e.g. a runner passing every table) is ignored
    ref = score_bundle(prod, req(), window, registry=registry)["summary"].to_dataset()
    other = score_bundle(prod, req(), window, side_tables={"fx": fx * 5}, registry=registry)
    xr.testing.assert_identical(ref, other["summary"].to_dataset())
