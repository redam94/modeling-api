"""v0.15: models read side tables (``side_table_params`` -> ``FitContext.side_tables``)."""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.harness import fit_spec
from mapi_sdk.models import (
    PREDICTIONS,
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelSettings,
    ScoreContext,
    requested_positions,
)
from mapi_sdk.registry import Registry
from mapi_sdk.safetensors import tensors_from_dataset, weights_dataset
from mapi_spec.capabilities import Capabilities, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest

pytestmark = pytest.mark.unit


class Settings(ModelSettings):
    weights: str = "pretrained"


class Offset(CustomModel[Settings]):
    """Predicts the mean of y plus the sum of a side table's weights."""

    id = "test.offset"
    version = "1.0.0"
    Settings = Settings
    side_table_params = ("weights",)
    capabilities = Capabilities(
        inference="point",
        representation=Representation.POINT,
        score_modes=frozenset({"predict"}),
    )
    schema_template: ClassVar[dict[str, Any]] = {
        "template": {
            "dims": {"row": {"kind": "categorical", "policy": "new_levels"}},
            "vars": {"y": {"dims": ["row"], "dtype": "float64", "role": "target"}},
        }
    }
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["row"], "units_of": "y"}}
    seen: ClassVar[list[dict[str, Any]]] = []

    def fit(self, ds: xr.Dataset, s: Settings, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        tables = dict(ctx.side_tables)
        type(self).seen.append(tables)
        tensors, _ = tensors_from_dataset(tables[s.weights])
        offset = float(sum(t.sum() for t in tensors.values()))
        return FitOutput(learned={"level": float(ds["y"].mean()) + offset})

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        n = ds.sizes["row"]
        assert requested_positions(ds, "row").size <= n
        y = xr.DataArray(np.full(n, fitted.learned["level"]), dims="row", coords={"row": ds.row})
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": y})})


SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: offset, project: p}
data:
  dataset: dataset://p/rows@latest
  side_tables:
    backbone: {ref: dataset://p/backbone@v1}
model:
  use: test.offset@^1
  settings: {weights: backbone}
"""


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    reg.register_model(Offset)
    return reg


def _rows() -> xr.Dataset:
    return xr.Dataset({"y": ("row", np.arange(4.0))}, coords={"row": ["a", "b", "c", "d"]})


def test_the_manifest_lists_the_side_table_params() -> None:
    assert Offset.manifest().side_table_params == ["weights"]


def test_a_model_gets_its_side_tables(registry: Registry) -> None:
    w = weights_dataset({"k": np.ones((2, 2), dtype=np.float32)})
    Offset.seen.clear()
    b = fit_spec(SPEC, _rows(), side_tables={"backbone": w}, registry=registry)
    assert list(Offset.seen[0]) == ["backbone"]
    assert b.model.learned["level"] == pytest.approx(1.5 + 4.0)
    assert b.side_tables == {}  # a model's table is read at fit only (not stored)


def test_a_missing_model_side_table_is_refused(registry: Registry) -> None:
    with pytest.raises(MapiError, match="side table 'backbone'") as err:
        fit_spec(SPEC, _rows(), registry=registry)
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
