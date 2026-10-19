"""The scorecard check ``data.missing`` on the science datasets with NaN by design.

E18 (``sci.areal_bym2``) pads the district outlines ``ring_x``/``ring_y`` with NaN (role
``constant``), and E51 (``sci.heat_plate``) has no reading in the cells without a thermocouple
(the model declares ``structural_missing``). Neither is a gap in the data, so the check must
not fail on them. A real gap in an observed variable still counts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_science.areal import ArealBYM2Model
from mapi_plugin_science.areal_data import AREAL_SCHEMA, AREAL_SPEC_YAML, areal_dataset
from mapi_plugin_science.heat_plate import HeatPlateModel
from mapi_plugin_science.heat_plate_data import HEAT_SCHEMA, HEAT_SPEC_YAML, heat_plate_dataset
from mapi_runner.worker.scorecard import _data_checks, missing_count, model_structural_missing
from mapi_sdk.binding import BindingMap
from mapi_sdk.registry import Registry
from mapi_spec import load_model_spec
from mapi_spec.schema import DataSchema
from mapi_spec.scorecard import SCORECARD_CHECKS, evaluate_check


def _bundle(spec_yaml: str, raw: DataSchema, model: type[Any]) -> Any:
    """The parts of a fitted bundle that the data checks read."""
    spec = load_model_spec(spec_yaml)
    settings = dict(spec.model.settings or {})
    return SimpleNamespace(
        raw_schema=raw,
        model_schema=model.manifest().schema_template.resolve(settings),
        binding=BindingMap(spec.data.bind),
        resolved=SimpleNamespace(
            model=SimpleNamespace(resolved=model.ref(), input_schema=None),
            stages={},
            model_stage="raw",
        ),
        fit_coords={},
    )


def _status(fraction: float) -> str:
    return str(evaluate_check(SCORECARD_CHECKS["data.missing"], fraction).status)


def _padded_outlines() -> xr.Dataset:
    """The synthetic map with the outlines padded to 12 vertices, as in the Scotland data."""
    ds = areal_dataset()
    pad = ds.pad(vertex=(0, 7), constant_values=np.nan)
    pad = pad.assign_coords(vertex=np.arange(pad.sizes["vertex"]))
    for name in ("cases", "expected", "aff", "adjacency"):
        pad[name] = ds[name]
    return pad


def test_e18_outline_padding_is_not_counted() -> None:
    data = _padded_outlines()
    assert int(data["ring_x"].isnull().sum()) > 0.5 * data["ring_x"].size
    bundle = _bundle(AREAL_SPEC_YAML, AREAL_SCHEMA, ArealBYM2Model)
    missing, cells, skipped = missing_count(bundle, data)
    assert missing == 0 and cells == 3 * data.sizes["district"]
    assert {"ring_x", "ring_y", "adjacency"} <= set(skipped)
    out: dict[str, Any] = {}
    why: dict[str, str] = {}
    _data_checks(bundle, data, out, why)
    value, expert = out["missing_fraction"]
    assert _status(value) == "pass"
    assert "ring_x (role constant)" in expert


def test_e18_a_real_gap_in_the_counts_still_fails() -> None:
    data = _padded_outlines()
    half = data.sizes["district"] // 2
    for name in ("cases", "aff"):
        values = data[name].values.copy()
        values[:half] = np.nan
        data[name] = ("district", values)
    bundle = _bundle(AREAL_SPEC_YAML, AREAL_SCHEMA, ArealBYM2Model)
    missing, cells, _ = missing_count(bundle, data)
    assert missing == 2 * half
    assert _status(missing / cells) == "fail"


def test_e51_cells_without_a_thermocouple_are_declared_by_the_model() -> None:
    data = heat_plate_dataset()
    assert int(data["reading_c"].isnull().sum()) > 0.5 * data["reading_c"].size
    bundle = _bundle(HEAT_SPEC_YAML, HEAT_SCHEMA, HeatPlateModel)
    registry = Registry()
    registry.register_model(HeatPlateModel)
    structural = model_structural_missing(registry, bundle)
    assert structural == {"temperature"}

    missing, cells, skipped = missing_count(bundle, data, structural)
    assert missing == 0 and cells == data["heater_w"].size
    assert skipped == {"reading_c": "missing by design of the model"}
    out: dict[str, Any] = {}
    why: dict[str, str] = {}
    _data_checks(bundle, data, out, why, structural)
    assert _status(out["missing_fraction"][0]) == "pass"

    # without the declaration the cells without a thermocouple count, as before
    before, total, _ = missing_count(bundle, data)
    assert _status(before / total) == "fail"


def test_an_unknown_plugin_declares_nothing() -> None:
    bundle = _bundle(HEAT_SPEC_YAML, HEAT_SCHEMA, HeatPlateModel)
    assert model_structural_missing(Registry(), bundle) == frozenset()
    assert model_structural_missing(None, bundle) == frozenset()
