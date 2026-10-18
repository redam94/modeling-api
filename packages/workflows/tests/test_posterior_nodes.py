"""M5 nodes on posterior draws: ``model.summary``, ``evaluate.ppc`` and ``compare.stacking``."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.workflow import NodeRunStatus, WorkflowRunStatus, load_workflow
from mapi_workflows.payloads import MetricsPayload, TablePayload
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.runtime.posterior import (
    posterior_summary,
    ppc_checks,
    pseudo_bma_weights,
    stacking_weights,
)
from mapi_workflows.runtime.values import ModelValue
from wf_ctx import run_node
from wf_models import registry

pytestmark = pytest.mark.filterwarnings("ignore")

_FIT = {"draws": 200, "chains": 2, "seed": 3}


def _panel(n: int = 40, *, slope: float = 1.5, seed: int = 0) -> xr.Dataset:
    """``y(geo, time)`` with a covariate ``x`` that drives it."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2020-01-05", periods=n, freq="W-SUN")
    x = rng.normal(size=(2, n))
    y = np.array([[10.0], [12.0]]) + slope * x + rng.normal(0, 1.0, size=(2, n))
    ds = xr.Dataset(
        {"y": (("geo", "time"), y), "x": (("geo", "time"), x)},
        coords={"geo": ["a", "b"], "time": t},
    )
    schema = DataSchema.model_validate(
        {
            "dims": {
                "geo": {"kind": "categorical"},
                "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
            },
            "vars": {
                "y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target"},
                "x": {"dims": ["geo", "time"], "dtype": "float64", "role": "covariate"},
            },
        }
    )
    ds.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    return ds


def _fit(features: list[str] | None = None, model: str = "toy.bayes_linear@^1") -> Any:
    settings: dict[str, Any] = {} if features is None else {"features": features}
    params: dict[str, Any] = {"model": model, "settings": settings, "fit": _FIT}
    if "bayes" in model:
        params["evaluate"] = {"metrics": ["elpd_loo"]}
    return run_node(
        "model.fit",
        params,
        data=_panel(),
        registry=registry(),
    )


# ============================================================================= math helpers


def test_stacking_puts_the_weight_on_the_model_that_predicts_better() -> None:
    rng = np.random.default_rng(0)
    good = rng.normal(-1.0, 0.1, 200)
    lpd = np.column_stack([good, good - 2.0])
    w = stacking_weights(lpd)
    assert w.sum() == pytest.approx(1.0)
    assert w[0] > 0.99
    pb = pseudo_bma_weights(lpd, n_bootstrap=200, seed=1)
    assert pb.sum() == pytest.approx(1.0) and pb[0] > 0.99


def test_stacking_mixes_models_that_are_good_on_different_points() -> None:
    # model A is right on the first half, B on the second: a mixture beats both
    a = np.r_[np.full(100, -0.5), np.full(100, -4.0)]
    b = np.r_[np.full(100, -4.0), np.full(100, -0.5)]
    w = stacking_weights(np.column_stack([a, b]))
    assert w == pytest.approx([0.5, 0.5], abs=1e-3)
    # a third model that is a copy of A gets no extra weight in total
    w3 = stacking_weights(np.column_stack([a, b, a]))
    assert w3[0] + w3[2] == pytest.approx(0.5, abs=1e-3)


def test_ppc_checks_tell_calibrated_from_overconfident_draws() -> None:
    rng = np.random.default_rng(2)
    y = rng.normal(0, 1, 300)
    good = rng.normal(0, 1, (500, 300))
    c = ppc_checks(good, y, "sd")
    assert c["coverage_90"] == pytest.approx(0.9, abs=0.06)
    assert c["coverage_50"] == pytest.approx(0.5, abs=0.08)
    assert c["pit_ks"] < 1.36 / np.sqrt(300)
    assert 0.05 < c["p_value"] < 0.95
    narrow = rng.normal(0, 0.3, (500, 300))
    bad = ppc_checks(narrow, y, "sd")
    assert bad["coverage_90"] < 0.5 and bad["p_value"] == 0.0
    assert bad["pit_ks"] > 0.2


def test_posterior_summary_flattens_vectors_and_caps_entries() -> None:
    rng = np.random.default_rng(0)
    post = xr.Dataset(
        {
            "mu": (("chain", "draw"), rng.normal(size=(2, 100))),
            "beta": (("chain", "draw", "geo"), rng.normal(3, 1, size=(2, 100, 3))),
        },
        coords={"geo": ["n", "s", "w"]},
    )
    out, truncated = posterior_summary(post, hdi_prob=0.9)
    assert set(out) == {"mu", "beta[n]", "beta[s]", "beta[w]"} and not truncated
    b = out["beta[s]"]
    assert b["hdi_low"] < b["mean"] < b["hdi_high"]  # type: ignore[operator]
    assert b["rhat"] == pytest.approx(1.0, abs=0.05)
    capped, cut = posterior_summary(post, ["beta"], max_entries=2)
    assert list(capped) == ["beta[n]", "beta[s]"] and cut
    with pytest.raises(MapiError, match="no variable"):
        posterior_summary(post, ["nope"])


# ==================================================================================== nodes


def test_model_summary_node_reports_parameters_and_convergence() -> None:
    mv = _fit(["x"]).outputs["model"]
    out = run_node("model.summary", {"vars": ["beta", "sigma"]}, model=mv)
    params = out.result["params"]
    assert set(params) == {"beta[0]", "sigma"}  # type: ignore[arg-type]
    slope = params["beta[0]"]  # type: ignore[index]
    assert slope["hdi_low"] > 0.8 and slope["hdi_high"] < 2.2  # type: ignore[index]
    assert out.result["max_rhat"] == pytest.approx(1.0, abs=0.05)
    assert out.result["min_ess"] > 50  # type: ignore[operator]
    assert out.result["n_divergent"] is None  # the toy model stores no sample_stats
    table = TablePayload.model_validate(out.outputs["table"])
    assert table.kind == "posterior_summary" and len(table.rows) == 2
    every = run_node("model.summary", model=mv)
    assert {"alpha[a]", "alpha[b]", "beta[0]", "sigma"} <= set(every.result["params"])  # type: ignore[arg-type]


def test_model_summary_refuses_point_models() -> None:
    mv = _fit(["x"], model="toy.linear@^1").outputs["model"]
    with pytest.raises(MapiError) as exc:
        run_node("model.summary", model=mv)
    assert exc.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_evaluate_ppc_node_passes_a_well_specified_model() -> None:
    mv = _fit(["x"]).outputs["model"]
    assert isinstance(mv, ModelValue)
    out = run_node(
        "evaluate.ppc", {"statistic": "sd", "n_draws": 300}, model=mv, registry=registry()
    )
    r = out.result
    assert r["ok"] is True, r
    assert r["coverage_90"] == pytest.approx(0.9, abs=0.1)
    assert set(r["p_values"]) == {"y"}  # type: ignore[arg-type]
    assert r["n_obs"] == 80 and r["n_draws"] <= 300
    mp = MetricsPayload.model_validate(out.outputs["metrics"])
    assert {"coverage_50", "coverage_90", "pit_ks", "ppc_p_sd"} == set(mp.candidates[0].metrics)
    # with no tolerance the sampled coverages cannot match their levels exactly
    strict = run_node(
        "evaluate.ppc", {"coverage_tolerance": 0.0, "n_draws": 100}, model=mv, registry=registry()
    ).result
    assert strict["ok"] is False


def test_evaluate_ppc_refuses_models_without_draws() -> None:
    mv = _fit(["x"], model="toy.linear@^1").outputs["model"]
    with pytest.raises(MapiError) as exc:
        run_node("evaluate.ppc", model=mv)
    assert exc.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_model_fit_metrics_carry_pointwise_elpd() -> None:
    out = _fit(["x"])
    cand = MetricsPayload.model_validate(out.outputs["metrics"]).candidates[0]
    assert cand.elpd is not None
    loo = cand.elpd["elpd_loo"]
    assert loo.n == 80 and loo.pointwise is not None and len(loo.pointwise) == 80
    assert sum(loo.pointwise) == pytest.approx(loo.elpd, rel=1e-4)
    assert loo.se is not None and loo.se > 0
    assert cand.metrics["elpd_loo"] == pytest.approx(loo.elpd)


def test_compare_stacking_node_weighs_candidates() -> None:
    with_x = _fit(["x"]).outputs["metrics"]
    without = _fit([]).outputs["metrics"]

    def named(payload: Any, name: str) -> dict[str, Any]:
        doc = dict(payload)
        doc["candidates"] = [{**doc["candidates"][0], "name": name, "group": name}]
        return doc

    cands = [named(with_x, "with_x"), named(without, "without")]
    out = run_node("compare.stacking", candidates=cands)
    r = out.result
    assert r["method"] == "stacking" and r["best"] == "with_x"
    w: dict[str, float] = r["weights"]  # type: ignore[assignment]
    assert sum(w.values()) == pytest.approx(1.0) and w["with_x"] > 0.9
    assert r["note_plain"] is None
    table = TablePayload.model_validate(out.outputs["table"])
    assert table.rows[0]["candidate"] == "with_x"
    plus = run_node("compare.stacking", {"method": "pseudo_bma_plus"}, candidates=cands).result
    assert plus["method"] == "pseudo_bma_plus" and plus["weights"] == plus["pseudo_bma"]

    # no pointwise values: pseudo-BMA from elpd and se, and the node says so
    for c in cands:
        c["candidates"][0]["elpd"]["elpd_loo"]["pointwise"] = None
    fb = run_node("compare.stacking", candidates=cands)
    assert fb.result["method"] == "pseudo_bma_se" and fb.result["best"] == "with_x"
    assert "Stacking was not possible" in str(fb.result["note_plain"])
    assert fb.warnings_plain

    # a candidate without elpd: a plain error that names it
    bare = named(without, "bare")
    bare["candidates"][0].pop("elpd")
    bare["candidates"][0]["metrics"] = {"rmse": 1.0}
    with pytest.raises(MapiError, match="bare"):
        run_node("compare.stacking", candidates=[cands[0], bare])


_LADDER = """
apiVersion: mapi/v1
kind: Workflow
metadata: {name: ladder, project: docs}
inputs: {data: {type: dataset}}
nodes:
  - {id: plain, use: model.fit, inputs: {data: inputs.data},
     with: {model: toy.bayes_linear@^1, settings: {features: []},
            fit: {draws: 200, chains: 2, seed: 1}, evaluate: {metrics: [elpd_loo]}}}
  - {id: driver, use: model.fit, inputs: {data: inputs.data},
     with: {model: toy.bayes_linear@^1, settings: {features: [x]},
            fit: {draws: 200, chains: 2, seed: 1}, evaluate: {metrics: [elpd_loo]}}}
  - {id: weights, use: compare.stacking, inputs: {candidates: [plain.metrics, driver.metrics]}}
  - {id: s, use: model.summary, inputs: {model: driver.model}, with: {vars: [beta]}}
  - {id: ppc, use: evaluate.ppc, inputs: {model: driver.model}}
  - {id: robust, use: branch.if,
     with: {condition: "nodes.s.params['beta[0]'].hdi_low > 0 and nodes.s.max_rhat < 1.05"}}
  - {id: report, use: report.summary, when: robust.then,
     inputs: {items: [weights.table, s.table], metrics: [ppc.metrics]}}
outputs: {weights: weights.table, report: report.report}
"""


def test_a_stacking_ladder_runs_locally_and_branches_on_the_summary() -> None:
    spec = load_workflow(_LADDER)
    res = LocalRunner(registry=registry()).run(spec, inputs={"data": _panel()})
    assert res.status is WorkflowRunStatus.SUCCEEDED, res.error
    w = res.node("weights").result
    assert w is not None and w["best"] == "driver" and w["method"] == "stacking"
    assert res.node("robust").result == {"value": True, "arm": "then"}
    assert res.node("report").status is NodeRunStatus.SUCCEEDED
    assert res.node("ppc").result["ok"] is True  # type: ignore[index]
