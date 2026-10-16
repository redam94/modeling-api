"""Build node contexts for runtime tests (defaults applied as the engine does)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.schema import DataSchema
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS
from mapi_workflows.runtime.nodes import builtin_nodes
from mapi_workflows.runtime.values import DatasetValue, NodeContext, NodeOutcome, Value


def node_ctx(use: str, params: dict[str, Any] | None = None, **inputs: Any) -> NodeContext:
    impl = NODE_IMPLEMENTATIONS[use]
    full = dict(params or {})
    for name, sub in (impl.params_schema.get("properties") or {}).items():  # type: ignore[union-attr]
        if name not in full and isinstance(sub, dict) and "default" in sub:
            full[name] = sub["default"]
    vals: dict[str, list[Value]] = {}
    registry = inputs.pop("registry", None)
    services = inputs.pop("services", {})
    for port, v in inputs.items():
        items = v if isinstance(v, list) else [v]
        vals[port] = [DatasetValue(data=x) if isinstance(x, xr.Dataset) else x for x in items]
    return NodeContext(node_id=use.replace(".", "_"), use=impl.ref, params=full, inputs=vals,
                       registry=registry, services=services)  # fmt: skip


def run_node(use: str, params: dict[str, Any] | None = None, **inputs: Any) -> NodeOutcome:
    return builtin_nodes()[use](node_ctx(use, params, **inputs))


def series_ds(values: np.ndarray, name: str = "y", freq: str = "W-SUN",
              extra: dict[str, np.ndarray] | None = None) -> xr.Dataset:  # fmt: skip
    t = pd.date_range("2020-01-05", periods=len(values), freq=freq)
    data = {name: ("time", np.asarray(values, float))}
    for k, v in (extra or {}).items():
        data[k] = ("time", np.asarray(v, float))
    ds = xr.Dataset(data, coords={"time": t})
    schema = DataSchema.model_validate({
        "dims": {"time": {"kind": "time", "policy": "extend", "freq": freq}},
        "vars": {k: {"dims": ["time"], "dtype": "float64",
                     "role": "target" if k == name else "covariate"} for k in data},
    })  # fmt: skip
    ds.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    return ds


def panel_ds(values: np.ndarray, geos: list[str], name: str = "y") -> xr.Dataset:
    t = pd.date_range("2020-01-05", periods=values.shape[1], freq="W-SUN")
    ds = xr.Dataset({name: (("geo", "time"), values)}, coords={"geo": geos, "time": t})
    schema = DataSchema.model_validate({
        "dims": {"geo": {"kind": "categorical"},
                 "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
        "vars": {name: {"dims": ["geo", "time"], "dtype": "float64", "role": "target"}},
    })  # fmt: skip
    ds.attrs["mapi_schema"] = schema.model_dump_json(exclude_none=True)
    return ds


def random_walk(n: int, seed: int = 0) -> np.ndarray:
    return np.cumsum(np.random.default_rng(seed).normal(size=n))


def ar1(n: int, phi: float = 0.5, seed: int = 0) -> np.ndarray:
    e = np.random.default_rng(seed).normal(size=n)
    out = np.zeros(n)
    for i in range(1, n):
        out[i] = phi * out[i - 1] + e[i]
    return out
