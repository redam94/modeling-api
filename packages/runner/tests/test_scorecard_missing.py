"""The scorecard check ``data.missing`` on an MMM dataset: real gaps still count.

The check skips the roles ``constant``, ``index`` and ``mask`` and the variables a model
declares in ``structural_missing`` (the science tests cover those). An MMM model declares
nothing except ``mmm.nested``, whose mediator series may have gaps by design (a monthly survey
in weekly data). The target and spend are observations, so a gap in them counts as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import xarray as xr
from mapi_plugin_mmm.examples import E33_SPEC_YAML, e33_dataset, e33_raw_schema
from mapi_plugin_mmm.plugins import PLUGINS
from mapi_runner.worker.scorecard import _data_checks, missing_count, model_structural_missing
from mapi_sdk.binding import BindingMap
from mapi_sdk.registry import Registry
from mapi_spec import load_model_spec
from mapi_spec.schema import DataSchema
from mapi_spec.scorecard import SCORECARD_CHECKS, evaluate_check


def _bundle(raw: DataSchema) -> Any:
    spec = load_model_spec(E33_SPEC_YAML)
    return SimpleNamespace(
        spec=spec,
        raw_schema=raw,
        model_schema=None,
        binding=BindingMap(spec.data.bind),
        resolved=SimpleNamespace(
            model=SimpleNamespace(resolved="mmm.bayesian@1.0.0", input_schema=None),
            stages={},
            model_stage="raw",
        ),
        fit_coords={},
    )


def _with_gaps(data: xr.Dataset) -> xr.Dataset:
    """Half of the dates of every series missing."""
    out = data.copy(deep=True)
    half = out.sizes["date"] // 2
    for name in ("revenue_local", "spend_local"):
        out[name][{"date": slice(0, half)}] = np.nan
    return out


def _registry() -> Registry:
    reg = Registry()
    for plugin in PLUGINS:
        reg.register_model(plugin)
    return reg


def test_mmm_models_declare_no_structural_missing() -> None:
    reg = _registry()
    declared = {p.id: tuple(getattr(p, "structural_missing", ()) or ()) for p in PLUGINS}
    assert declared.pop("mmm.nested") == ("mediators",)
    assert not any(declared.values())
    data, _ = e33_dataset()
    bundle = _bundle(e33_raw_schema(data))
    bundle.resolved.model.resolved = reg.model(load_model_spec(E33_SPEC_YAML).model.use).ref()
    assert model_structural_missing(reg, bundle) == frozenset()


def test_a_real_gap_in_mmm_data_still_fails() -> None:
    data, _ = e33_dataset()
    gappy = _with_gaps(data)
    bundle = _bundle(e33_raw_schema(data).model_copy(deep=True))
    missing, cells, skipped = missing_count(bundle, gappy)
    naive = sum(int(gappy[v].isnull().sum()) for v in gappy.data_vars)
    assert (missing, cells) == (naive, sum(int(gappy[v].size) for v in gappy.data_vars))
    assert skipped == {}
    out: dict[str, Any] = {}
    why: dict[str, str] = {}
    _data_checks(bundle, gappy, out, why, model_structural_missing(_registry(), bundle))
    value, expert = out["missing_fraction"]
    assert value == naive / cells and "not counted" not in str(expert)
    assert evaluate_check(SCORECARD_CHECKS["data.missing"], value).status == "fail"


def test_missing_allow_does_not_hide_a_gap() -> None:
    """``missing: allow`` lets the data in; the check still counts the gap."""
    data, _ = e33_dataset()
    raw = e33_raw_schema(data)
    lenient = raw.model_copy(
        update={"vars": {n: v.model_copy(update={"missing": "allow"}) for n, v in raw.vars.items()}}
    )
    missing, cells, _ = missing_count(_bundle(lenient), _with_gaps(data))
    assert evaluate_check(SCORECARD_CHECKS["data.missing"], missing / cells).status == "fail"


def test_an_mmm_constant_with_a_gap_is_layout_not_data() -> None:
    """A ``constant`` (for example a lookup per market) is not an observation of the KPI."""
    data, _ = e33_dataset()
    raw = e33_raw_schema(data)
    pop = np.array([1.0, np.nan, 3.0, np.nan])
    extra = data.assign(population=("market", pop))
    schema = DataSchema.model_validate(
        {
            **raw.model_dump(mode="json"),
            "vars": {
                **raw.model_dump(mode="json")["vars"],
                "population": {
                    "dims": ["market"],
                    "dtype": "float64",
                    "role": "constant",
                    "missing": "allow",
                },
            },
        }
    )
    missing, _, skipped = missing_count(_bundle(schema), extra)
    assert missing == 0 and skipped == {"population": "role constant"}
