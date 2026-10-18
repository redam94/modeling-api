"""``sci.lgcp_grid`` (E07): HSGP maths, synthetic recovery, forecasts and the contract kit."""

from __future__ import annotations

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_plugin_science.challenges import challenges_file
from mapi_plugin_science.lgcp import (
    LGCPGridModel,
    grid_basis,
    inverse_gamma_from_interval,
    matern52_psd_2d,
    walk_matrix,
)
from mapi_plugin_science.lgcp_data import (
    CHALLENGES_CSV,
    LGCP_SCHEMA,
    LGCP_SPEC_YAML,
    LGCP_TRUTH,
    from_challenges,
    lgcp_dataset,
    matern52_cov,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import check_dataset
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import load_model_spec, load_score_request, validate_spec
from mapi_spec.errors import ErrorCode, MapiError
from scipy import stats

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://sci/lgcp\n"


def _req(body: str) -> object:
    return load_score_request(HEADER + body)


@pytest.fixture(scope="module")
def registry() -> Registry:
    reg = default_registry(reload=True).copy()
    reg.register_model(LGCPGridModel, replace=True)
    return reg


@pytest.fixture(scope="module")
def truth_data() -> xr.Dataset:
    return lgcp_dataset(seed=0, return_truth=True)


@pytest.fixture(scope="module")
def data(truth_data: xr.Dataset) -> xr.Dataset:
    return truth_data[["quakes"]]


@pytest.fixture(scope="module")
def bundle(registry: Registry, data: xr.Dataset) -> Bundle:
    return fit_spec(LGCP_SPEC_YAML, data, registry=registry, raw_schema=LGCP_SCHEMA)


# ----------------------------------------------------------------------- manifest / maths
def test_manifest_declares_a_grid_output_and_forecasts() -> None:
    man = LGCPGridModel.manifest()
    assert man.capabilities.score_modes == frozenset({"predict", "forecast", "evaluate"})
    grid = man.outputs["rate"].grid
    assert grid is not None
    assert (grid.y, grid.x, grid.frame, grid.origin, grid.log) == (
        "row",
        "col",
        "period",
        "lower",
        True,
    )
    schema = man.schema_template.resolve({"max_extension": 5})
    assert schema.dims["period"].max_extension == 5
    assert schema.dims["row"].policy.value == "fixed"
    assert man.resources is not None and man.resources.est_fit_seconds


def test_static_validation_resolves_the_spec(registry: Registry) -> None:
    res = validate_spec(load_model_spec(LGCP_SPEC_YAML), registry.catalog(), LGCP_SCHEMA)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    assert res.raise_for_errors().model.resolved == "sci.lgcp_grid@1.0.0"


def test_psd_matches_pymc_matern52_in_two_dimensions() -> None:
    w = np.linspace(0.0, 0.3, 7)
    ours = matern52_psd_2d(w, 30.0)
    # pymc takes a frequency vector per input dimension; put the norm on the diagonal
    theirs = pm.gp.cov.Matern52(2, ls=30.0).power_spectral_density(
        np.column_stack([w, w]) / np.sqrt(2.0)
    )
    np.testing.assert_allclose(ours, theirs.eval(), rtol=1e-6)


def test_product_basis_approximates_the_exact_covariance() -> None:
    rows, cols = np.arange(6), np.arange(5)
    phi, omega = grid_basis(rows, cols, cell_km=25.0, m_row=24, m_col=20, c=3.0)
    assert phi.shape == (30, 480)
    approx = phi @ np.diag(matern52_psd_2d(omega, 40.0)) @ phi.T
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    xy = np.column_stack([cc.ravel(), rr.ravel()]) * 25.0
    exact = matern52_cov(xy, 40.0, 1.0)  # same row-major cell order as the basis
    assert np.abs(approx - exact).max() < 0.01


def test_walk_matrix_anchors_every_era() -> None:
    w = walk_matrix(5, 2)
    assert (w[:, 2] == 1).all()
    np.testing.assert_array_equal(w[4], [0, 0, 1, 1, 1])  # anchor + increments 3, 4
    np.testing.assert_array_equal(w[0], [1, 1, 1, 0, 0])  # backward increments 0, 1
    np.testing.assert_array_equal(w[2], [0, 0, 1, 0, 0])


def test_lengthscale_prior_has_ninety_percent_in_the_interval() -> None:
    a, b = inverse_gamma_from_interval(15.0, 100.0)
    dist = stats.invgamma(a, scale=b)
    np.testing.assert_allclose(dist.ppf([0.05, 0.95]), [15.0, 100.0], rtol=1e-6)


def test_synthetic_data_has_the_schema_and_the_truth(truth_data: xr.Dataset) -> None:
    assert dict(truth_data["quakes"].sizes) == {"quarter": 16, "row": 6, "col": 5}
    assert truth_data["quakes"].dtype == np.int64
    np.testing.assert_allclose(truth_data["rate"].sum(("row", "col")), truth_data["total"])
    assert float(np.exp(np.log(truth_data["total"]).mean())) == pytest.approx(LGCP_TRUTH["total"])


# ----------------------------------------------------------------------- fit and scores
def test_fit_recovers_the_totals_and_hyperparameters(
    bundle: Bundle, truth_data: xr.Dataset
) -> None:
    post = bundle.model.posterior
    n_fit = truth_data.sizes["quarter"]
    assert post["step"].sizes["step_period"] == n_fit + 8  # the future is in the model
    assert post["field"].sizes["era"] == (n_fit + 8) // 4
    assert bundle.model.learned["anchor_era"] in range(n_fit // 4)
    total = post["total"].mean(("chain", "draw")).values
    true_total = truth_data["total"].values
    assert np.corrcoef(np.log(total), np.log(true_total))[0, 1] > 0.5
    assert np.abs(np.log(total / true_total)).mean() < 0.4
    assert 0.02 < float(post["inv_phi"].mean()) < 1.5
    assert 15.0 < float(post["ell"].mean()) < 150.0
    # where the events fall: the posterior map correlates with the true one
    rate = post["rate"].mean(("chain", "draw")).values.ravel()
    assert np.corrcoef(np.log(rate), np.log(truth_data["rate"].values.ravel()))[0, 1] > 0.6
    assert {"rmse", "mae", "coverage_90"} <= set(bundle.metrics.values)


def test_forecast_extends_the_walk_and_the_last_map(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(
        bundle,
        _req("mode: forecast\nhorizon: {dim: period, periods: 8}\noutput: {draws: 20}\n"),
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()
    assert dict(pred["rate"].sizes) == {"chain": 2, "draw": 100, "quarter": 8, "row": 6, "col": 5}
    assert pred["quarter"].values.min() > data["quarter"].values.max()
    assert pred["rate"].attrs["units"] == "events"
    np.testing.assert_allclose(pred["rate"].sum(("row", "col")), pred["total"], rtol=1e-8)
    # uncertainty grows with the horizon (random walk)
    spread = np.log(pred["total"]).std(("chain", "draw")).values
    assert spread[-1] > spread[0]
    assert out["draws"].to_dataset().sizes["sample"] == 20


def test_predictive_counts_are_whole_numbers(bundle: Bundle, registry: Registry) -> None:
    out = score_bundle(
        bundle,
        _req("mode: forecast\nhorizon: {dim: period, periods: 2}\noutput: {kind: predictive}\n"),
        registry=registry,
        return_full=True,
    )
    counts = out["predictions"].to_dataset()["rate"].values
    assert (counts >= 0).all() and np.allclose(counts, np.round(counts))


def test_horizon_past_max_extension_needs_a_refit(bundle: Bundle, registry: Registry) -> None:
    with pytest.raises(MapiError) as err:
        score_bundle(
            bundle, _req("mode: forecast\nhorizon: {dim: period, periods: 9}\n"), registry=registry
        )
    assert err.value.code is ErrorCode.HORIZON_EXCEEDS_FIT


@pytest.mark.mapi_contract
def test_lgcp_passes_the_contract_kit(registry: Registry, data: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=LGCP_SPEC_YAML, data=data, raw_schema=LGCP_SCHEMA, registry=registry
    )
    report = check_model(LGCPGridModel, fixture, raise_on_failure=False)
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
        "plots_sanitize",
    } <= ran


# ----------------------------------------------------------------------- the real catalogue
@pytest.mark.skipif(
    challenges_file(CHALLENGES_CSV) is None, reason="pymc-challenges data not present"
)
def test_from_challenges_bins_like_the_notebook() -> None:
    path = challenges_file(CHALLENGES_CSV)
    assert path is not None
    ds = from_challenges(path)
    q = ds["quakes"]
    assert dict(q.sizes) == {"quarter": 44, "row": 9, "col": 12}
    train, test = q.isel(quarter=slice(0, 36)), q.isel(quarter=slice(36, None))
    assert int(test.sum()) == 208  # "the observed two-year total (208)"
    assert int(train.max()) == 86  # "the observed 86"
    assert float((train == 0).mean()) == pytest.approx(0.86, abs=0.005)
    assert str(ds["quarter"].values[0])[:10] == "2009-01-01"
    check_dataset(LGCP_SCHEMA, ds)
