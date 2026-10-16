"""``mapi_client.atlas.RemoteModel`` against a respx-mocked endpoint serving a synthetic
concave response, and an optimiser that finds its known optimum through the model.

Atlas (``atlas.core.interfaces``) ships the model and optimiser *protocols*, not optimiser
implementations, so the optimiser here is a small SLSQP budget optimiser written against
those protocols: ``optimize(model, constraints, config) -> result``. The last test runs it
with Atlas installed (``make test-atlas``) and returns Atlas's own ``OptimizationResult``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_client import Client, ClientConfig
from mapi_client.atlas import RemoteModel, register_with_atlas
from mapi_spec.errors import ErrorCode, MapiError

BASE = "http://api.test"
CHANNELS = ("tv", "search", "social")
A = {"tv": 3000.0, "search": 2000.0, "social": 1000.0}
B = {"tv": 1000.0, "search": 2000.0, "social": 500.0}
TOTAL = 10_000.0
# KKT of max sum a_c log(1 + x_c / b_c) s.t. sum x_c = TOTAL: a_c / (b_c + x_c) = lambda
_INV_LAMBDA = (TOTAL + sum(B.values())) / sum(A.values())
OPTIMUM = {c: A[c] * _INV_LAMBDA - B[c] for c in CHANNELS}  # tv 5750, search 2500, social 1750

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mapi_sdk") is None, reason="needs the xarray extra"
)


def concave(ds: xr.Dataset) -> xr.Dataset:
    """revenue = sum_c a_c * log(1 + spend_c / b_c), per scenario (if any)."""
    spend = ds["spend"]
    a = xr.DataArray([A[c] for c in CHANNELS], dims=["channel"], coords={"channel": list(CHANNELS)})
    b = xr.DataArray([B[c] for c in CHANNELS], dims=["channel"], coords={"channel": list(CHANNELS)})
    revenue = (a * np.log1p(spend / b)).sum("channel")
    return xr.Dataset({"revenue": revenue, "contribution": a * np.log1p(spend / b)})


def revenue_of(budget: Mapping[str, float]) -> float:
    return float(sum(A[c] * np.log1p(budget[c] / B[c]) for c in CHANNELS))


@pytest.fixture
def api_client(api: Any, sleeps: Any) -> Any:
    cfg = ClientConfig(base_url=BASE, project="acme/growth", max_retries=0)
    with Client(config=cfg, sleep=sleeps) as c:
        yield c


# ===================================================================== a protocol optimiser


class Result:
    """The fields of ``atlas.core.interfaces.OptimizationResult`` this optimiser fills."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class SlsqpBudgetOptimizer:
    """Maximise ``model.predict`` over budgets with a fixed total (an Atlas
    ``OptimizerProtocol``). Gradients are central finite differences scored in one batch
    per iteration when the model offers ``predict_batch``."""

    def __init__(self, result_type: Callable[..., Any] = Result) -> None:
        self.result_type = result_type
        self.evaluations = 0

    def _values(self, model: Any, budgets: Sequence[Mapping[str, float]]) -> list[float]:
        xs = [model.allocation(b) for b in budgets]
        self.evaluations += len(xs)
        if hasattr(model, "predict_batch"):
            preds = model.predict_batch(xs)
        else:  # pragma: no cover - any ModelProtocol
            preds = [model.predict(x) for x in xs]
        return [float(p.mean()) for p in preds]

    def optimize(self, model: Any, constraints: dict[str, Any], config: dict[str, Any]) -> Any:
        from scipy.optimize import minimize

        channels = list(config["channels"])
        total = float(constraints["total_budget"])
        h = float(config.get("step", 1e-4))

        def budget(w: np.ndarray) -> dict[str, float]:
            return {c: float(total * max(v, 0.0)) for c, v in zip(channels, w, strict=True)}

        def f_and_grad(w: np.ndarray) -> tuple[float, np.ndarray]:
            points = [budget(w)]
            for i in range(len(w)):
                for sign in (1.0, -1.0):
                    e = np.zeros_like(w)
                    e[i] = sign * h
                    points.append(budget(w + e))
            vals = self._values(model, points)
            grad = np.array([(vals[1 + 2 * i] - vals[2 + 2 * i]) / (2 * h) for i in range(len(w))])
            return -vals[0], -grad

        n = len(channels)
        res = minimize(
            f_and_grad,
            np.full(n, 1.0 / n),
            jac=True,
            method="SLSQP",
            bounds=[(h, 1.0 - h)] * n,
            constraints=[{"type": "eq", "fun": lambda w: float(np.sum(w) - 1.0)}],
            options={"ftol": 1e-12, "maxiter": 200},
        )
        best = budget(res.x)
        return self.result_type(
            optimal_budget=best,
            optimal_value=float(-res.fun),
            metadata={"evaluations": self.evaluations},
            convergence_info={"success": bool(res.success), "iterations": int(res.nit)},
        )


# ============================================================================ the adapter


def test_predict_scores_one_allocation(
    api_client: Client, fake_endpoint: Callable[..., Any]
) -> None:
    ep = fake_endpoint(concave)
    model = RemoteModel(api_client, "plan-prod", spend_var="spend", kpi="revenue")
    budget = {"tv": 1000.0, "search": 500.0, "social": 250.0}
    value = model.predict(model.allocation(budget))
    assert value.dims == ()
    assert float(value) == pytest.approx(revenue_of(budget))
    body = ep.bodies[-1]
    assert body["mode"] == "predict"
    assert body["output"]["kind"] == "expected" and body["output"]["summary"] == ["mean"]
    assert body["output"]["draws"] == 0
    assert body["data"]["data_vars"]["spend"]["dims"] == ["channel"]
    assert ep.headers[-1]["x-mapi-project"] == "acme/growth"
    assert ep.headers[-1]["prefer"] == "wait=60"


def test_kpi_defaults_to_the_target_and_contributions_keep_everything(
    api_client: Client,
    fake_endpoint: Callable[..., Any],
    scenario_snapshot: dict[str, Any],
) -> None:
    fake_endpoint(concave, snapshot=scenario_snapshot)
    model = RemoteModel(api_client, "plan-prod")
    x = model.allocation({"tv": 100.0, "search": 100.0, "social": 100.0})
    assert model.required_dimensions == ["scenario", "channel"]
    assert model.model_type == "mapi.endpoint"
    assert float(model.predict(x)) == pytest.approx(
        revenue_of({"tv": 100.0, "search": 100.0, "social": 100.0})
    )
    contrib = model.contributions(x)
    assert set(contrib.data_vars) == {"revenue", "contribution"}
    assert contrib["contribution"].dims == ("channel",)
    assert model.cache_info().hits == 1  # contributions reused the scored payload


def test_cache_avoids_repeat_requests(
    api_client: Client, fake_endpoint: Callable[..., Any]
) -> None:
    ep = fake_endpoint(concave)
    model = RemoteModel(api_client, "plan-prod", spend_var="spend", kpi="revenue", cache_size=2)
    budgets = [{"tv": float(i), "search": 1.0, "social": 1.0} for i in range(3)]
    for b in [*budgets[:2], *budgets[:2]]:
        model.predict(model.allocation(b))
    info = model.cache_info()
    assert (info.hits, info.misses, info.requests) == (2, 2, 2)
    assert ep.score_calls == 2
    model.predict(model.allocation(budgets[2]))  # evicts the oldest (LRU of 2)
    model.predict(model.allocation(budgets[0]))
    assert model.cache_info().requests == 4 and model.cache_info().size == 2
    model.clear_cache()
    assert model.cache_info().size == 0


def test_batching_stacks_candidates_into_one_request(
    api_client: Client,
    fake_endpoint: Callable[..., Any],
    scenario_snapshot: dict[str, Any],
) -> None:
    ep = fake_endpoint(concave, snapshot=scenario_snapshot)
    model = RemoteModel(api_client, "plan-prod", batch_dim="scenario", max_batch=4)
    budgets = [{"tv": 100.0 * i, "search": 50.0, "social": 10.0 * i} for i in range(6)]
    values = model.predict_batch([model.allocation(b) for b in budgets])
    assert [float(v) for v in values] == pytest.approx([revenue_of(b) for b in budgets])
    assert ep.score_calls == 2  # 6 candidates in batches of 4
    sent = ep.bodies[0]["data"]
    assert sent["coords"]["scenario"]["data"] == [f"candidate-{i}" for i in range(4)]
    assert sent["data_vars"]["spend"]["dims"] == ["scenario", "channel"]
    # a repeated candidate in a later batch comes from the cache
    again = model.predict_batch([model.allocation(budgets[1]), model.allocation(budgets[5])])
    assert float(again[1]) == pytest.approx(revenue_of(budgets[5]))
    assert ep.score_calls == 2


def test_batching_needs_a_dim_the_endpoint_can_extend(
    api_client: Client,
    fake_endpoint: Callable[..., Any],
    scenario_snapshot: dict[str, Any],
) -> None:
    fake_endpoint(concave, snapshot=scenario_snapshot)
    x = xr.Dataset({"spend": ("channel", [1.0, 2.0, 3.0])}, coords={"channel": list(CHANNELS)})
    with pytest.raises(ValueError, match="cannot batch along 'channel'"):
        RemoteModel(api_client, "plan-prod", batch_dim="channel").predict_batch([x, x * 2])
    with pytest.raises(ValueError, match="no 'week' dim"):
        RemoteModel(api_client, "plan-prod", batch_dim="week").predict_batch([x, x * 2])


def test_draws_output_keeps_a_sample_dim(
    api_client: Client, fake_endpoint: Callable[..., Any]
) -> None:
    ep = fake_endpoint(concave)
    model = RemoteModel(
        api_client, "plan-prod", spend_var="spend", kpi="revenue", output="draws", draws=20
    )
    budget = {"tv": 2000.0, "search": 1000.0, "social": 400.0}
    value = model.predict(model.allocation(budget))
    assert value.dims == ("sample",) and value.sizes["sample"] == 20
    assert float(value.mean()) == pytest.approx(revenue_of(budget))
    assert ep.bodies[-1]["output"]["draws"] == 20
    assert model.predict_budget(budget) == pytest.approx(revenue_of(budget))
    with pytest.raises(ValueError, match="draws"):
        RemoteModel(api_client, "plan-prod", output="draws", draws=301)


def test_rate_limited_calls_are_retried_with_backoff(
    api_client: Client, fake_endpoint: Callable[..., Any]
) -> None:
    """The client gives up on a long ``Retry-After``; RemoteModel waits and tries again."""
    ep = fake_endpoint(concave, throttle=2, retry_after=600)
    waits: list[float] = []
    model = RemoteModel(
        api_client, "plan-prod", spend_var="spend", kpi="revenue", sleep=waits.append,
        backoff=2.0, max_backoff=30.0,
    )  # fmt: skip
    budget = {"tv": 10.0, "search": 10.0, "social": 10.0}
    assert float(model.predict(model.allocation(budget))) == pytest.approx(revenue_of(budget))
    assert ep.score_calls == 3 and len(waits) == 2
    assert all(30.0 <= w <= 33.0 for w in waits)  # Retry-After capped at max_backoff
    # the retry reuses the request's Idempotency-Key
    assert len({h["idempotency-key"] for h in ep.headers}) == 1
    ep.throttle = 10
    stubborn = RemoteModel(
        api_client,
        "plan-prod",
        spend_var="spend",
        kpi="revenue",
        sleep=waits.append,
        rate_limit_retries=1,
    )
    with pytest.raises(MapiError) as info:
        stubborn.predict(model.allocation({"tv": 1.0, "search": 1.0, "social": 1.0}))
    assert info.value.code is ErrorCode.RATE_LIMITED


def test_allocation_keeps_the_base_flighting(
    api_client: Client, fake_endpoint: Callable[..., Any]
) -> None:
    fake_endpoint(concave)
    weeks = ["2026-11-02", "2026-11-09"]
    base = xr.Dataset(
        {
            "spend": (("channel", "week"), [[300.0, 100.0], [50.0, 50.0], [0.0, 0.0]]),
            "price": ("week", [9.99, 8.99]),
        },
        coords={"channel": list(CHANNELS), "week": weeks},
    )
    model = RemoteModel(api_client, "plan-prod", spend_var="spend", base=base, reduce="none")
    x = model.allocation({"tv": 800.0, "search": 10.0, "social": 6.0})
    assert x["spend"].sel(channel="tv").values.tolist() == [600.0, 200.0]
    assert x["spend"].sel(channel="social").values.tolist() == [3.0, 3.0]  # spread evenly
    assert x["spend"].dims == ("channel", "week")
    payload = model._payload(x)
    assert "price" in payload  # the template's other inputs are sent too


def test_quantile_endpoints_are_read_at_the_median(
    api_client: Client,
    fake_endpoint: Callable[..., Any],
    scenario_snapshot: dict[str, Any],
) -> None:
    """Quantile models (tabular.regressor) have no mean: the default stat follows the
    endpoint's representation, and draws are refused up front."""
    ep = fake_endpoint(concave, snapshot=scenario_snapshot | {"representation": "quantiles"})
    model = RemoteModel(api_client, "plan-prod", kpi="revenue")
    budget = {"tv": 500.0, "search": 500.0, "social": 500.0}
    assert float(model.predict(model.allocation(budget))) == pytest.approx(
        1.01 * revenue_of(budget)
    )
    assert ep.bodies[-1]["output"]["summary"] == ["median"]
    pinned = RemoteModel(api_client, "plan-prod", kpi="revenue", stat="mean")
    assert float(pinned.predict(pinned.allocation(budget))) == pytest.approx(revenue_of(budget))
    with pytest.raises(ValueError, match="needs an endpoint with draws"):
        RemoteModel(api_client, "plan-prod", output="draws").predict(model.allocation(budget))


def test_payloads_follow_the_schema_dim_order(
    api_client: Client,
    fake_endpoint: Callable[..., Any],
    scenario_snapshot: dict[str, Any],
) -> None:
    """The server checks each variable's dims in order; stacking a batch puts the batch dim
    first, so the payload is transposed to the endpoint's order."""
    scenario_snapshot["input_schema"]["vars"]["spend"]["dims"] = ["channel", "scenario"]
    ep = fake_endpoint(concave, snapshot=scenario_snapshot)
    model = RemoteModel(api_client, "plan-prod", batch_dim="scenario")
    budgets = [{"tv": 10.0 * i, "search": 1.0, "social": 2.0} for i in range(3)]
    values = model.predict_batch([model.allocation(b) for b in budgets])
    assert [float(v) for v in values] == pytest.approx([revenue_of(b) for b in budgets])
    assert ep.bodies[-1]["data"]["data_vars"]["spend"]["dims"] == ["channel", "scenario"]
    remote = api_client.endpoint("plan-prod")
    ds = xr.Dataset(
        {"spend": (("scenario", "channel"), [[1.0, 2.0, 3.0]])},
        coords={"scenario": ["a"], "channel": list(CHANNELS)},
    )
    remote.validate(ds)
    assert ep.bodies[-1]["data"]["data_vars"]["spend"]["dims"] == ["channel", "scenario"]
    remote.validate(ds, conform=False)
    assert ep.bodies[-1]["data"]["data_vars"]["spend"]["dims"] == ["scenario", "channel"]


def test_invalid_options() -> None:
    api = Client(BASE)
    for bad in ({"output": "median"}, {"reduce": "max"}, {"max_batch": 0}):
        with pytest.raises(ValueError):
            RemoteModel(api, "plan-prod", **bad)
    with pytest.raises(ValueError, match="RemoteModel sends it"):
        RemoteModel(api, "plan-prod", call={"output": {"kind": "predictive"}})


# =========================================================================== optimisation


def test_an_optimizer_finds_the_optimum_through_the_endpoint(
    api_client: Client,
    fake_endpoint: Callable[..., Any],
    scenario_snapshot: dict[str, Any],
) -> None:
    pytest.importorskip("scipy")
    ep = fake_endpoint(concave, snapshot=scenario_snapshot)
    model = RemoteModel(api_client, "plan-prod", batch_dim="scenario")
    optimizer = SlsqpBudgetOptimizer()
    result = optimizer.optimize(
        model, {"total_budget": TOTAL}, {"channels": CHANNELS, "step": 1e-4}
    )
    assert result.convergence_info["success"]
    for c in CHANNELS:
        assert result.optimal_budget[c] == pytest.approx(OPTIMUM[c], rel=2e-3)
    assert result.optimal_value == pytest.approx(revenue_of(OPTIMUM), rel=1e-6)
    # every SLSQP iteration was one batched score request (point + 2 per channel)
    assert ep.score_calls == model.cache_info().requests
    assert ep.score_calls <= result.convergence_info["iterations"] + 3
    assert optimizer.evaluations >= 7 * ep.score_calls - 7


@pytest.mark.skipif(importlib.util.find_spec("atlas") is None, reason="Atlas is not installed")
def test_with_atlas_installed(
    api_client: Client,
    fake_endpoint: Callable[..., Any],
    scenario_snapshot: dict[str, Any],
) -> None:
    """Atlas's interfaces accept RemoteModel; the optimiser returns Atlas's result type."""
    pytest.importorskip("scipy")
    from atlas.core.interfaces import AbstractModel, ModelProtocol, OptimizationResult

    fake_endpoint(concave, snapshot=scenario_snapshot)
    model: ModelProtocol = RemoteModel(api_client, "plan-prod", batch_dim="scenario")
    assert register_with_atlas()
    assert isinstance(model, AbstractModel)
    result = SlsqpBudgetOptimizer(OptimizationResult).optimize(
        model, {"total_budget": TOTAL}, {"channels": CHANNELS}
    )
    assert isinstance(result, OptimizationResult)
    for c in CHANNELS:
        assert result.optimal_budget[c] == pytest.approx(OPTIMUM[c], rel=2e-3)
