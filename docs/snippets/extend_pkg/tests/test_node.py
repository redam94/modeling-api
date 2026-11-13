"""Contract-kit tests for test.acme_zero_share (run them with ``mapi plugin test``).

The kit checks the contract (plugin rules, About card, safe parameter schema), runs the
node on the inputs below, then checks the outputs against their port types, the result
fields, the size of the result sidecar and, for a cacheable node, that two runs agree.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_workflows.kit import check_node

from acme.nodes import NODES, ZERO_SHARE


def weekly_sales(zeros: int = 9) -> xr.Dataset:
    """Sixty weeks of sales, the first ``zeros`` weeks at zero."""
    rng = np.random.default_rng(7)
    time = pd.date_range("2024-01-07", periods=60, freq="W-SUN")
    sales = 100.0 + rng.normal(0, 5, 60)
    sales[:zeros] = 0.0
    return xr.Dataset({"sales": (("time",), sales)}, coords={"time": time})


def test_the_node_is_exported() -> None:
    assert ZERO_SHARE in NODES
    assert ZERO_SHARE.contract.id == "test.acme_zero_share"


@pytest.mark.mapi_contract
def test_contract_kit() -> None:
    report = check_node(
        ZERO_SHARE,
        inputs={"data": weekly_sales()},
        params={"var": "sales", "threshold": 0.1},
        raise_on_failure=False,
    )
    assert report.ok, report.summary()
