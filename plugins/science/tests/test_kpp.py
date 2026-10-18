"""``sci.fisher_kpp`` (E50): the PDE inside the model, the initial condition kept in the bundle,
forecasts past the last hour, the CFL check, recovery on synthetic data and the real assay."""

from __future__ import annotations

import math

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.kpp import (
    INITIAL,
    OBS_DIM,
    FisherKPPModel,
    FisherKPPSettings,
    cfl_number,
    simulate_kpp,
)
from mapi_plugin_science.kpp_data import (
    KPP_SCHEMA,
    KPP_SPEC_YAML,
    KPP_TRUTH,
    from_challenges,
    kpp_dataset,
)
from mapi_pymc.context import BuildContext
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import validate_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError

CHALLENGES_CSV = challenges_file("scratch_assay_pc3.csv")
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/kpp\n"


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry().copy()
    reg.register_model(FisherKPPModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def assay() -> xr.Dataset:
    return kpp_dataset(seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, assay: xr.Dataset) -> Bundle:
    return fit_spec(KPP_SPEC_YAML, assay, registry=registry, raw_schema=KPP_SCHEMA)


# ------------------------------------------------------------------------------- manifest


def test_manifest_declares_a_kymograph_and_a_scalar_speed() -> None:
    man = FisherKPPModel.manifest()
    assert man.id == "sci.fisher_kpp"
    assert man.capabilities.score_modes == frozenset({"predict", "forecast", "evaluate"})
    grid = man.outputs["density"].grid
    assert grid is not None and (grid.y, grid.x, grid.origin) == ("t", "x", "lower")
    assert man.outputs["density"].units_of == "count"
    assert man.outputs["wave_speed"].dims == [] and man.outputs["wave_speed"].units == "um/h"
    schema = man.schema_template.resolve({"max_extension": 3})
    assert schema.dims["t"].kind == "integer" and schema.dims["t"].max_extension == 3
    assert schema.dims["x"].policy.value == "fixed"
    assert set(man.plots) == {
        "core.convergence",
        "core.posterior_forest",
        "sci.profile_bands",
        "sci.field_map",
    }


def test_static_validation_resolves_the_spec(registry: Registry) -> None:
    res = validate_spec(load_model_spec(KPP_SPEC_YAML), registry.catalog(), KPP_SCHEMA)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]


# ------------------------------------------------------------------------------- numerics


def test_scan_matches_the_numpy_twin() -> None:
    """The PyTensor scan in build() and simulate_kpp() are the same scheme (with delay)."""
    ds = kpp_dataset(columns=8, times=(0, 4, 10), seed=1).rename(
        {"hours": "t", "position": "x", "cells": "count"}
    )
    s = FisherKPPSettings(steps_per_hour=3, delay=True)
    model = FisherKPPModel().build(ds, s, BuildContext(phase="fit", ds=ds))
    point = {"D_log__": math.log(900.0), "lam_log__": math.log(0.08), "K": 110.0}
    point["beta_log__"] = math.log(0.2)
    (mu_graph,) = model.replace_rvs_by_values([model["mu"]])
    mu = model.compile_fn(mu_graph, inputs=model.value_vars, on_unused_input="ignore")(point)
    want = simulate_kpp(
        np.maximum(ds["count"].values[0], s.initial_floor),
        np.array([0.0, 4.0, 10.0]),
        diffusivity=900.0,
        growth=0.08,
        capacity=110.0,
        delay_rate=0.2,
        dx=100.0,
        steps_per_hour=3,
    )
    np.testing.assert_allclose(mu, want, rtol=1e-8)
    # zero-flux edges and no growth: the total number of cells is conserved
    flat = simulate_kpp(
        want[0], np.array([0.0, 20.0]), diffusivity=900.0, growth=0.0, capacity=110.0,
        delay_rate=None, dx=100.0, steps_per_hour=3,
    )  # fmt: skip
    assert flat[1].sum() == pytest.approx(flat[0].sum(), rel=1e-10)


def test_unstable_euler_grid_raises_a_typed_error() -> None:
    ds = kpp_dataset(columns=6, dx=20.0, times=(0, 4), seed=0).rename(
        {"hours": "t", "position": "x", "cells": "count"}
    )
    s = FisherKPPSettings(steps_per_hour=1)
    assert cfl_number(10_000.0, 20.0, 1) > 0.5
    with pytest.raises(MapiError) as err:
        FisherKPPModel().build(ds, s, BuildContext(phase="fit", ds=ds))
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    assert "steps_per_hour" in str(err.value)


def test_times_off_the_euler_grid_are_rejected() -> None:
    ds = kpp_dataset(columns=6, times=(0, 4), seed=0).rename(
        {"hours": "t", "position": "x", "cells": "count"}
    )
    ds = ds.assign_coords(t=np.array([0.0, 4.05]))
    with pytest.raises(MapiError) as err:
        FisherKPPModel().build(ds, FisherKPPSettings(), BuildContext(phase="fit", ds=ds))
    assert err.value.code is ErrorCode.SCHEMA_INVALID


# ------------------------------------------------------------------------------- fit / score


def test_fit_conditions_on_the_first_time_and_recovers_the_truth(
    bundle: Bundle, assay: xr.Dataset
) -> None:
    groups = bundle.model.groups
    obs = groups["observed_data"]["count"]
    assert obs.dims == (OBS_DIM, "x")
    np.testing.assert_array_equal(obs[OBS_DIM].values, assay["hours"].values[1:])
    np.testing.assert_allclose(
        groups["constant_data"][INITIAL].values, np.maximum(assay["cells"].values[0], 0.1)
    )
    post = groups["posterior"]
    d, lam = post["D"].values.ravel(), post["lam"].values.ravel()
    lo, hi = np.quantile(d, [0.01, 0.99])
    assert lo < KPP_TRUTH["D"] < hi, (lo, hi)
    lo, hi = np.quantile(lam, [0.01, 0.99])
    assert lo < KPP_TRUTH["lam"] < hi, (lo, hi)
    speed = 2 * math.sqrt(KPP_TRUTH["D"] * KPP_TRUTH["lam"])
    assert abs(float(post["wave_speed"].mean()) - speed) < 0.2 * speed
    assert bundle.model.learned["cfl_number_max"] < 0.5
    assert bundle.metrics.values["coverage_90"] > 0.5


def test_forecast_runs_the_pde_past_the_last_hour(
    bundle: Bundle, assay: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        bundle,
        _req("mode: forecast\nhorizon: {dim: t, periods: 2}\noutput: {draws: 10}\n"),
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()
    last = int(assay["hours"].values.max())
    np.testing.assert_array_equal(pred["hours"].values, [last + 8, last + 16])
    dens = pred["density"].mean(("chain", "draw"))
    assert dens.dims == ("hours", "position") and bool(np.isfinite(dens).all())
    # the wound keeps closing: the centre fills towards the capacity
    centre = dens.isel(position=slice(6, 10)).mean("position")
    assert float(centre.isel(hours=1)) >= float(centre.isel(hours=0)) - 1.0
    assert pred["wave_speed"].dims == ("chain", "draw")
    assert out["summary"].to_dataset()["density"].attrs["units"] == "cells"


def test_predictive_in_sample_includes_the_initial_profile(
    bundle: Bundle, assay: xr.Dataset, registry: Registry
) -> None:
    payload = assay.drop_vars("cells")
    out = score_bundle(
        bundle, _req("mode: predict\noutput: {kind: predictive}\n"), payload, registry=registry,
        return_full=True,
    )  # fmt: skip
    pred = out["predictions"].to_dataset()["density"]
    first = pred.isel(hours=0).mean(("chain", "draw")).values
    # the first time is the initial condition: draws scatter around the observed profile
    np.testing.assert_allclose(first, np.maximum(assay["cells"].values[0], 0.1), atol=6.0)
    assert set(np.unique(pred.values % 1)) == {0.0}  # Poisson counts


def test_one_label_payload_after_the_fit(bundle: Bundle, registry: Registry) -> None:
    payload = xr.Dataset(coords={"hours": [40], "position": kpp_dataset()["position"].values})
    # one label has no step of its own: the validator places it on the fit's 8 h grid
    out = score_bundle(
        bundle, _req("mode: predict\n"), payload, registry=registry, return_full=True
    )
    np.testing.assert_array_equal(out["predictions"].to_dataset()["hours"].values, [40])


@pytest.mark.mapi_contract
def test_fisher_kpp_passes_the_contract_kit(registry: Registry, assay: xr.Dataset) -> None:
    fixture = ModelFixture(spec=KPP_SPEC_YAML, data=assay, raw_schema=KPP_SCHEMA, registry=registry)
    report = check_model(FisherKPPModel, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "extend_typed_errors",
        "horizon_exceeds_fit",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "output_space_and_units",
        "outputs_match_manifest",
        "seed_determinism",
    } <= ran


# ------------------------------------------------------------------------------- real data


@pytest.mark.skipif(CHALLENGES_CSV is None, reason="pymc-challenges data not present")
def test_from_challenges_reads_the_pc3_assay() -> None:
    assert CHALLENGES_CSV is not None
    ds = from_challenges(CHALLENGES_CSV)
    assert validate_dataset(KPP_SCHEMA, ds) == []
    assert dict(ds.sizes) == {"hours": 5, "position": 38}
    np.testing.assert_array_equal(ds["hours"].values, [0, 12, 24, 36, 48])
    np.testing.assert_allclose(np.diff(ds["position"].values), 50.0)
    assert ds["cells"].dtype == np.int64 and int(ds["cells"].values[0, 0]) == 89
    # the notebook grid (dx = 50 um, 10 steps per hour) passes the stability check
    canon = ds.rename({"hours": "t", "position": "x", "cells": "count"})
    model = FisherKPPModel().build(canon, FisherKPPSettings(), BuildContext(phase="fit", ds=canon))
    assert model["mu"].ndim == 2 and "beta" in model.named_vars
