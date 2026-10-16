"""Optimise a budget against an endpoint with Atlas: ``mapi_client.atlas.RemoteModel``.

Runs offline (conformance/tests/test_docs_snippets.py): a stand-in endpoint answers through
``httpx.MockTransport``. It decodes each inline XrJSON payload and scores every
``scenario`` with a concave response, revenue = sum_c a_c * log(1 + spend_c / b_c), whose
optimum under a fixed total is known in closed form - so the page can check the optimiser.
Atlas itself is optional here: with it installed the result is Atlas's own
``OptimizationResult``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Mapping
from typing import Any

import httpx
import numpy as np
import xarray as xr
from mapi_client import Client, ClientConfig
from mapi_sdk.wire import from_xrjson, write_datatree_zip
from mapi_spec.api import EndpointSchema, Run
from mapi_spec.storage import crc32c, crc32c_to_base64
from mapi_spec.tokens import generate_token

BASE = "https://api.mapi.example"
STORAGE = "https://storage.mapi.example"
A = {"tv": 3000.0, "search": 2000.0, "social": 1000.0}
B = {"tv": 1000.0, "search": 2000.0, "social": 500.0}
BLOBS: dict[str, bytes] = {}
SCORE_CALLS: list[dict[str, Any]] = []


def _schema() -> dict[str, Any]:
    schema = copy.deepcopy(EndpointSchema.model_json_schema()["examples"][0])
    schema.update(alias="budget-planner", ref="endpoint://growth/budget-planner")
    schema["snapshot"]["input_schema"] = {
        "dims": {
            "scenario": {"kind": "categorical", "policy": "new_levels"},
            "channel": {"kind": "categorical"},
        },
        "vars": {
            "spend": {"dims": ["scenario", "channel"], "dtype": "float64", "role": "spend"},
            "revenue": {"dims": ["scenario"], "dtype": "float64", "role": "target"},
        },
    }
    schema["snapshot"]["score_modes"] = ["predict"]
    return schema


def _api(request: httpx.Request) -> httpx.Response:
    """A stand-in endpoint: ``…/schema`` and ``…/score`` (inline XrJSON -> zarr zip)."""
    if request.url.path.endswith("/schema"):
        return httpx.Response(200, json=_schema())
    body = json.loads(request.content)
    SCORE_CALLS.append(body)
    spend = from_xrjson(body["data"])["spend"]
    a = xr.DataArray(list(A.values()), dims=["channel"], coords={"channel": list(A)})
    b = xr.DataArray(list(B.values()), dims=["channel"], coords={"channel": list(B)})
    revenue = (a * np.log1p(spend / b)).sum("channel")
    summary = xr.Dataset({"revenue": revenue.expand_dims(stat=["mean"])})
    tree = xr.DataTree.from_dict({"/": xr.Dataset(), "/summary": summary})
    blob = write_datatree_zip(tree)
    assert blob is not None
    name = f"r{len(SCORE_CALLS)}.zarr.zip"
    BLOBS[name] = blob
    link = {
        "name": "score.zarr.zip", "url": f"{STORAGE}/{name}", "size": len(blob),
        "crc32c": crc32c_to_base64(crc32c(blob)), "generation": 1,
        "content_type": "application/zip", "expires_at": "2099-01-01T00:00:00Z",
    }  # fmt: skip
    run = Run.model_json_schema()["examples"][0] | {"state": "succeeded", "progress": None}
    return httpx.Response(200, json={"done": True, "run": run, "outputs": [link]})


def _storage(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=BLOBS[request.url.path.lstrip("/")])


config = ClientConfig(base_url=BASE, token=str(generate_token()), project="acme/growth")
api = Client(
    config=config, transport=httpx.MockTransport(_api), storage_transport=httpx.MockTransport(_storage)
)

# --8<-- [start:model]
from mapi_client.atlas import RemoteModel

model = RemoteModel(
    api,
    endpoint="budget-planner",
    batch_dim="scenario",  # the model scores every scenario independently
    output="mean",  # or "draws" for risk-aware objectives
)
x = model.allocation({"tv": 4000.0, "search": 4000.0, "social": 2000.0})
print(float(model.predict(x)))  # the KPI (revenue) of this allocation
# --8<-- [end:model]

# --8<-- [start:optimizer]
from scipy.optimize import minimize


class BudgetOptimizer:
    """An Atlas optimiser (``OptimizerProtocol``): maximise ``model.predict`` with a fixed
    total. Each iteration scores the point and its finite differences in one batch."""

    def optimize(
        self, model: Any, constraints: Mapping[str, Any], config: Mapping[str, Any]
    ) -> dict[str, Any]:
        channels, total, h = list(config["channels"]), float(constraints["total_budget"]), 1e-4

        def budget(w: np.ndarray) -> dict[str, float]:
            return {c: total * max(float(v), 0.0) for c, v in zip(channels, w, strict=True)}

        def objective(w: np.ndarray) -> tuple[float, np.ndarray]:
            steps = [np.zeros_like(w)] + [s * h * np.eye(len(w))[i]
                                          for i in range(len(w)) for s in (1, -1)]
            values = model.predict_batch([model.allocation(budget(w + e)) for e in steps])
            f = [float(v) for v in values]
            grad = [(f[1 + 2 * i] - f[2 + 2 * i]) / (2 * h) for i in range(len(w))]
            return -f[0], -np.asarray(grad)

        res = minimize(
            objective, np.full(len(channels), 1 / len(channels)), jac=True, method="SLSQP",
            bounds=[(h, 1 - h)] * len(channels),
            constraints=[{"type": "eq", "fun": lambda w: float(np.sum(w) - 1)}],
        )  # fmt: skip
        return {"optimal_budget": budget(res.x), "optimal_value": float(-res.fun)}


result = BudgetOptimizer().optimize(
    model, {"total_budget": 10_000}, {"channels": ["tv", "search", "social"]}
)
print({c: round(v) for c, v in result["optimal_budget"].items()})
print(model.cache_info())
# --8<-- [end:optimizer]

# the closed-form optimum: a_c / (b_c + x_c) is equal across channels
inv_lambda = (10_000 + sum(B.values())) / sum(A.values())
for c, v in result["optimal_budget"].items():
    assert abs(v - (A[c] * inv_lambda - B[c])) < 25, (c, v)
assert len(SCORE_CALLS) == model.cache_info().requests

if importlib.util.find_spec("atlas") is not None:  # with Atlas installed
    # --8<-- [start:atlas]
    from atlas.core.interfaces import AbstractModel
    from mapi_client.atlas import register_with_atlas

    register_with_atlas()  # RemoteModel becomes a virtual subclass of AbstractModel
    assert isinstance(model, AbstractModel)
    # --8<-- [end:atlas]

api.close()
