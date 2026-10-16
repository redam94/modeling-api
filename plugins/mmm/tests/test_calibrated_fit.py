"""A calibrated ``mmm.hierarchical_geo`` fit through the SDK harness: business priors are
translated at fit time (no ``translate_priors`` run recorded) and a lift test pulls the
model's implied lift towards the measured one."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_mmm.calibration import LIFT_VAR, lift_design, model_scales
from mapi_plugin_mmm.derived import draw_samples
from mapi_plugin_mmm.examples import E33Truth
from mapi_plugin_mmm.settings import HierarchicalGeoSettings
from mapi_plugin_mmm.transforms import adstock_weights
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, prepare_model_stage
from mapi_sdk.registry import Registry
from mapi_spec import DataSchema, load_model_spec
from mapi_spec.errors import ErrorCode, MapiError

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CALIBRATION = """
calibration:
  business_priors:
    channels:
      search: {roi: [0.5, 3.0], credibility: 0.8}
  lift_tests:
    - {id: tv-de, channel: tv, geo: DE, start: 2023-03-06, end: 2023-04-02,
       spend_delta: -15000, kpi_delta: KPI, se: 2000}
"""


def _lift_of(
    bundle: Bundle,
    spec_text: str,
    data: xr.Dataset,
    registry: Registry,
    tables: dict[str, xr.Dataset],
    raw_schema: DataSchema,
) -> tuple[float, float]:
    """Posterior-mean implied lift of the test (and its sd) under ``bundle``."""
    stage = prepare_model_stage(
        load_model_spec(spec_text), data, tables, registry, raw_schema=raw_schema
    )
    s = HierarchicalGeoSettings.model_validate(stage.settings)
    sc = model_scales(stage.data, s, stage.to_original)
    cal = stage.spec.calibration
    assert cal is not None
    design = lift_design(cal.lift_tests, sc)
    assert design is not None
    post = draw_samples(bundle.model.posterior, None)
    lags = np.arange(sc.x.shape[-1])
    values = []
    for k in range(post.sizes["sample"]):
        p = post.isel(sample=k)
        alpha = np.asarray(p["alpha"].values, dtype=float)
        w = adstock_weights("geometric", lags, alpha=alpha[:, None])
        values.append(
            design.lift(w, {"lam": np.asarray(p["lam"].values)}, np.asarray(p["beta"].values),
                        "logistic")[0]
        )  # fmt: skip
    return float(np.mean(values)), float(np.std(values))


def test_lift_tests_and_business_priors_calibrate_the_fit(
    spec_text: Callable[..., str],
    bundle: Bundle,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
    registry: Registry,
    raw_schema: DataSchema,
) -> None:
    data = small[0]
    probe = spec_text() + CALIBRATION.replace("KPI", "-1.0")
    before, sd = _lift_of(bundle, probe, data, registry, tables, raw_schema)
    assert before < 0  # removing TV spend loses revenue in the uncalibrated fit
    target = 2.5 * before  # the test claims a much larger effect, measured precisely
    text = spec_text(**{"seed: 20261014": "seed: 20261014\n    keep: {log_likelihood: true}"})
    text += CALIBRATION.replace("KPI", f"{target:.1f}").replace("se: 2000", f"se: {abs(sd):.1f}")
    calibrated = fit_spec(load_model_spec(text), data, tables, registry, raw_schema=raw_schema)
    record = calibrated.model.learned["calibration"]
    assert record["lift_tests"] == {"used": ["tv-de"], "skipped": []}
    translated = record["translated_priors"]["beta_channel"]
    assert translated["distribution"] == "LogNormal" and len(translated["mu"]) == 2
    post = calibrated.model.posterior
    assert f"{LIFT_VAR}__model" in post
    after = float(post[f"{LIFT_VAR}__model"].mean())
    # pulled towards the measured lift, and the KPI likelihood alone stays in log_likelihood
    assert abs(after - target) < abs(before - target)
    ll = calibrated.model.groups["log_likelihood"]
    assert LIFT_VAR not in ll and "y" in ll
    # the evaluation fit (train only) also used the test: its window is inside the train data
    assert calibrated.evaluation is not None
    assert calibrated.evaluation.model.learned["calibration"]["lift_tests"]["used"] == ["tv-de"]


def test_unknown_business_prior_channels_fail_the_fit(
    spec_text: Callable[..., str],
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
    registry: Registry,
    raw_schema: DataSchema,
) -> None:
    text = spec_text() + (
        "calibration:\n  business_priors:\n    channels:\n      radio: {roi: [1, 2]}\n"
    )
    with pytest.raises(MapiError) as err:
        fit_spec(load_model_spec(text), small[0], tables, registry, raw_schema=raw_schema)
    assert err.value.code is ErrorCode.BUSINESS_PRIORS_INVALID
    assert err.value.pointer == "/calibration/business_priors/channels/radio"
