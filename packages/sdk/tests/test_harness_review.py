"""Regression tests for the M0 review findings on the fit/score harness (spec-sdk area)."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.steps import Step, StepContext, StepSettings, StepState
from mapi_spec import load_model_spec, load_score_request
from mapi_spec.effects import StepKind
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import JsonValue

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


# ------------------------------------------------------ statistics-3 (unsorted score coords)

LAG_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: lagx, project: growth}
data:
  dataset: dataset://growth/sales@latest
pipeline:
  - {id: lags, use: toy.lag@^1, with: {vars: [x], along: time, within: [geo], lags: [1, 2]}}
model:
  use: toy.linear@^1
  settings: {features: [x_lag]}
  fit: {mode: train_only, seed: 1}
"""

FORECAST_4 = "mode: forecast\nhorizon: {dim: time, periods: 4}\noutput: {names: canonical}\n"


def _lag_panel(weeks: int = 30, extra: int = 4) -> xr.Dataset:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(0)
    time = pd.date_range("2024-01-07", periods=weeks + extra, freq="W-SUN").as_unit("ns")
    x = rng.uniform(0, 1, (3, weeks + extra))
    y = 1 + 2 * np.roll(x, 1, axis=1) + 0.01 * rng.standard_normal(x.shape)
    return xr.Dataset(
        {"y": (("geo", "time"), y), "x": (("geo", "time"), x)},
        coords={"geo": ["a", "b", "c"], "time": time},
    )


def test_unsorted_score_coords_are_ordered_before_the_pipeline(registry: Registry) -> None:
    full = _lag_panel()
    bundle = fit_spec(load_model_spec(LAG_SPEC), full.isel(time=slice(0, 30)), None, registry)
    fut = full.isel(time=slice(30, None)).drop_vars("y")
    a = score_bundle(bundle, req(FORECAST_4), fut, registry=registry)["summary"].to_dataset()
    shuffled = fut.isel(time=[3, 1, 2, 0])
    b = score_bundle(bundle, req(FORECAST_4), shuffled, registry=registry)["summary"].to_dataset()
    xr.testing.assert_allclose(a["y_hat"], b["y_hat"].sel(time=a["time"]))
    dup = fut.isel(time=[0, 0, 1, 2])
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, req(FORECAST_4), dup, registry=registry)
    assert err.value.code is ErrorCode.COORD_NOT_ALLOWED


# ----------------------------------------- plan-conformance-3 / statistics-5 (target lags)

YLAG_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: ylag, project: growth}
data:
  dataset: dataset://growth/sales@latest
  split: __SPLIT__
pipeline:
  - {id: ylag, use: toy.lag@^1, with: {vars: [y], along: time, within: [geo], lags: [1]}}
model:
  use: toy.linear@^1
  settings: {features: [x, y_lag]}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse]}
"""


def _ylag_spec(split: str) -> Any:
    return load_model_spec(YLAG_SPEC.replace("__SPLIT__", split))


def test_recursion_is_rejected_whatever_raw_schema_is_given(registry: Registry) -> None:
    from mapi_sdk.schema import infer_schema

    data = _lag_panel(weeks=30, extra=0)
    spec = _ylag_spec("{kind: time_holdout, dim: time, last: 4}")
    inferred = infer_schema(data)  # marks no target
    assert not inferred.targets
    for schema in (None, inferred):
        with pytest.raises(MapiError) as err:
            fit_spec(spec, data, None, registry, raw_schema=schema)
        assert err.value.code is ErrorCode.RECURSIVE_UNSUPPORTED


@pytest.mark.parametrize(
    "split",
    [
        "{kind: time_holdout, dim: time, fraction: 0.15}",
        "{kind: time_holdout, dim: time, after: '2024-06-30'}",
        "{kind: as_of, dim: time, as_of: '2024-06-30'}",
    ],
)
def test_runtime_recursion_check_once_the_holdout_length_is_known(
    registry: Registry, split: str
) -> None:
    """A holdout whose length is only known at run time is still checked against the lag."""
    data = _lag_panel(weeks=30, extra=0)
    with pytest.raises(MapiError) as err:
        fit_spec(_ylag_spec(split), data, None, registry)
    assert err.value.code is ErrorCode.RECURSIVE_UNSUPPORTED
    assert err.value.pointer == "/pipeline/0"


def test_cell_level_split_scores_holdout_without_its_own_targets(registry: Registry) -> None:
    """mask_block: held-out targets never feed lag features of the evaluation score."""
    import numpy as np
    from mapi_sdk.models import FitContext, FitOutput, Fitted, ScoreContext
    from mapi_sdk.testing import toy
    from mapi_spec.spec import FitConfig, ScoreRequest

    seen: list[xr.Dataset] = []

    class _Spy(toy.LinearModel):
        id = "test.spy_lag"

        def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput:
            return super().fit(ds, s, cfg, ctx)

        def score(
            self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
        ) -> xr.DataTree:
            seen.append(ds)
            return super().score(fitted, ds, req, ctx)

    reg = registry.copy()
    reg.register_model(_Spy)
    data = _lag_panel(weeks=30, extra=0)
    split = "{kind: mask_block, dims: [geo, time], block: {time: 5}, fraction: 0.3, seed: 3}"
    text = YLAG_SPEC.replace("__SPLIT__", split).replace("toy.linear@^1", "test.spy_lag@^1")
    bundle = fit_spec(load_model_spec(text), data, None, reg)
    assert bundle.metrics.n_holdout is not None and bundle.metrics.n_holdout > 0
    assert np.isfinite(bundle.metrics.values["rmse"])
    ev_inputs = seen[0]  # the evaluation's score call (holdout through the score path)
    ylag = ev_inputs["y_lag"].sel(lag=1)
    # wherever the true previous target is known, the feature equals it; held-out cells
    # give NaN lags, so the number of NaN lag features exceeds the series starts
    starts = ev_inputs.sizes["geo"]
    assert int(ylag.isnull().sum()) > starts


# --------------------------------------------------------------- statistics-8 (as_of gap)


def test_as_of_split_with_a_gap_evaluates_past_the_gap(registry: Registry) -> None:
    data = _lag_panel(weeks=30, extra=0)
    as_of = str(data["time"].values[21])[:10]
    split = f"{{kind: as_of, dim: time, as_of: '{as_of}', gap: 2}}"
    spec = load_model_spec(
        LAG_SPEC.replace("pipeline:", f"  split: {split}\npipeline:").replace(
            "mode: train_only", "mode: evaluate_then_refit"
        )
    )
    bundle = fit_spec(spec, data, None, registry)
    # 30 weeks: 22 train, 2 gap weeks (scored for continuity, never counted), 6 holdout
    assert bundle.metrics.n_holdout == 3 * 6
    assert bundle.metrics.values["rmse"] is not None
    no_gap = load_model_spec(
        LAG_SPEC.replace(
            "pipeline:", f"  split: {{kind: as_of, dim: time, as_of: '{as_of}'}}\npipeline:"
        ).replace("mode: train_only", "mode: evaluate_then_refit")
    )
    assert fit_spec(no_gap, data, None, registry).metrics.n_holdout == 3 * 8


# ---------------------------------------------- plan-conformance-9 (predict past the fit)


def test_predict_past_the_fit_runs_the_recursion_check(registry: Registry) -> None:
    full = _lag_panel(weeks=30, extra=6)
    spec = load_model_spec(
        YLAG_SPEC.replace("  split: __SPLIT__\n", "").replace("evaluate_then_refit", "train_only")
    )
    bundle = fit_spec(spec, full.isel(time=slice(0, 30)), None, registry)
    future = full.isel(time=slice(30, None)).drop_vars("y")
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, req("mode: predict\n"), future, registry=registry)
    assert err.value.code is ErrorCode.RECURSIVE_UNSUPPORTED
    # one period ahead is covered by the lag (the target column is there, unknown)
    one = full.isel(time=[30]).assign(y=lambda d: d["y"] * np.nan)
    out = score_bundle(bundle, req("mode: predict\n"), one, registry=registry)
    assert out["summary"].to_dataset()["y_hat"].sizes["time"] == 1


# ------------------------------------------------- statistics-2 (additive decompositions)


def test_contributions_are_not_inverted_through_a_non_additive_chain(
    registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    """expm1 per component would not sum to expm1(y_hat): keep them in model units."""
    data = panel[0][["revenue_local"]]
    b = fit_spec(load_model_spec(LOG_SPEC.replace("__RT__", "median")), data, None, registry)
    window = data.isel(date=slice(-28, None))
    out = score_bundle(
        b, req("output: {names: canonical}\n"), window, registry=registry, return_full=True
    )
    p = out["predictions"].to_dataset()
    space = json.loads(out.attrs["output_space"])
    assert space["y_hat"] == "weekly" and space["contributions"] == "logy"
    model_units = score_bundle(
        b,
        req("output: {names: canonical, units: model}\n"),
        window,
        registry=registry,
        return_full=True,
    )["predictions"].to_dataset()
    xr.testing.assert_allclose(p["contributions"], model_units["contributions"])
    xr.testing.assert_allclose(p["contributions"].sum("component"), model_units["y_hat"])
    assert p["contributions"].attrs["output_space"] == "logy"
    issues = json.loads(out.attrs["issues"])
    assert any(
        i["code"] == "CAPABILITY_UNSUPPORTED" and "contributions" in i["message"] for i in issues
    )


def test_contributions_invert_through_a_multiplicative_chain(
    registry: Registry, e33_spec: Any, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    lin = fit_spec(e33_spec(model="toy.linear@^1", metrics="rmse"), data, {"fx": fx}, registry)
    window = data.isel(date=slice(-28, None))
    p = score_bundle(lin, req(), window, registry=registry, return_full=True)[
        "predictions"
    ].to_dataset()
    assert p["contributions"].attrs["output_space"] == "weekly"
    xr.testing.assert_allclose(p["contributions"].sum("component"), p["y_hat"])


# --------------------------------------------------- statistics-4 (chained look-back steps)


class _ShiftSettings(StepSettings):
    var: str
    out: str
    k: int = 1


class _ShiftStep(Step[_ShiftSettings, StepState]):
    """``out = var`` shifted by ``k`` along ``time`` (a minimal look-back step)."""

    id = "test.shift"
    version = "1.0.0"
    kind = StepKind.FEATURE
    Settings = _ShiftSettings
    context_template: ClassVar[dict[str, JsonValue]] = {"along": "time", "n": "{k}"}

    def transform(
        self, ds: xr.Dataset, s: _ShiftSettings, st: StepState, ctx: StepContext
    ) -> xr.Dataset:
        return ds.assign({s.out: ds[s.var].shift(time=s.k)})


def _series(n: int, start: int = 0) -> xr.Dataset:
    t = np.arange(start, start + n)
    return xr.Dataset(
        {"x": (("geo", "time"), np.vstack([t, 10 * t]).astype(float))},
        coords={"geo": ["a", "b"], "time": t},
    )


def test_chained_look_back_steps_replay_the_fit_time_history() -> None:
    """A later step's stored context wins over rows an earlier step replayed without
    their own look-back history."""
    from mapi_sdk.context import drop_context
    from mapi_sdk.pipeline import Pipeline

    pipe = Pipeline.build(
        [
            ("s1", _ShiftStep, {"var": "x", "out": "x1", "k": 1}),
            ("s2", _ShiftStep, {"var": "x1", "out": "x12", "k": 2}),
        ]
    )
    full = _series(22)
    trace = pipe.fit(full.isel(time=slice(0, 20)))
    rp = pipe.transform(full.isel(time=slice(20, None)), trace.states)
    out = drop_context(rp.data, rp.marks)
    ref = pipe.transform(full, trace.states, replay=False).data
    # x12[t] = x[t - 3]: 17 and 18 for the first geo
    xr.testing.assert_allclose(out["x12"], ref["x12"].sel(time=[20, 21]))
    assert out["x12"].sel(geo="a").values.tolist() == [17.0, 18.0]
    assert not rp.issues


# ---------------------------------------------------- statistics-7 (long-layout group context)


def test_long_layout_group_context_survives_canonicalisation() -> None:
    """``lag(along=obs, within=[hh])``: synthetic context rows never trip the extend checks."""
    from mapi_sdk.context import drop_context
    from mapi_sdk.coords import canonicalise_coords
    from mapi_sdk.pipeline import Pipeline
    from mapi_sdk.testing import toy
    from mapi_spec import DataSchema

    ds = xr.Dataset(
        {"x": ("obs", np.arange(1.0, 9.0)), "hh": ("obs", np.array([0, 1] * 4))},
        coords={"obs": np.arange(8)},
    )
    pipe = Pipeline.build(
        [("lag", toy.LagStep, {"vars": ["x"], "along": "obs", "within": ["hh"], "lags": [1]})]
    )
    trace = pipe.fit(ds.isel(obs=slice(0, 6)))
    rp = pipe.transform(ds.isel(obs=[6, 7]), trace.states)
    assert rp.marks and int(rp.data.sizes["obs"]) == 4  # two context rows, one per household
    schema = DataSchema.model_validate(
        {
            "dims": {"obs": {"kind": "index", "policy": "extend"}},
            "vars": {
                "x": {"dims": ["obs"], "dtype": "float64"},
                "hh": {"dims": ["obs"], "dtype": "int64"},
            },
        }
    )
    for pad in (False, True):
        canon, plan = canonicalise_coords(
            rp.data, trace.coords["lag"], schema, pad_extend=pad, marks=rp.marks
        )
        out = plan.restore(drop_context(canon, rp.marks))
        assert out["obs"].values.tolist() == [6, 7]
        # each household's previous trip: x at obs 4 (hh 0) and obs 5 (hh 1)
        assert out["x_lag"].sel(lag=1).values.tolist() == [5.0, 6.0]
