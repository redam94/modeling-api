"""``tabular.hierarchical_glm`` end to end on long-layout radon data (E02)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_tabular import HierarchicalGLM, HierarchicalGLMSettings
from mapi_plugin_tabular.datasets import RADON_SCHEMA, RADON_SPEC_YAML, RADON_TRUTH, radon_dataset
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec
from pydantic import ValidationError

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://environment/radon\n"


def _req(body: str) -> Any:
    return load_score_request(HEADER + body)


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="module")
def data() -> xr.Dataset:
    return radon_dataset(counties=8, homes=80, seed=0)


@pytest.fixture(scope="module")
def bundle(registry: Registry, data: xr.Dataset) -> Bundle:
    return fit_spec(RADON_SPEC_YAML, data, registry=registry, raw_schema=RADON_SCHEMA)


def _new_county(uranium: float, homes: int = 6, start: int = 80) -> xr.Dataset:
    return xr.Dataset(
        {
            "floor": (("home",), np.zeros(homes)),
            "county_idx": (("home",), np.zeros(homes, dtype=np.int64)),
            "log_uranium": (("county",), [uranium]),
        },
        coords={"home": np.arange(start, start + homes), "county": ["county_new"]},
    )


def test_settings_validation() -> None:
    with pytest.raises(ValidationError, match="varying_slopes"):
        HierarchicalGLMSettings(covariates=["x"], varying_slopes=["z"])
    with pytest.raises(ValidationError, match="distinct"):
        HierarchicalGLMSettings(covariates=["x"], group_covariates=["x"])
    with pytest.raises(ValidationError, match="cannot be covariates"):
        HierarchicalGLMSettings(covariates=["county_idx"])
    with pytest.raises(ValidationError, match="differ"):
        HierarchicalGLMSettings(group_dim="obs")


def test_template_declares_covariates_and_group_dim() -> None:
    tmpl = HierarchicalGLM.manifest().schema_template
    schema = tmpl.resolve(
        {"group_dim": "school", "covariates": ["x1"], "group_covariates": ["size"]}
    )
    assert schema.layout == "long"
    assert schema.dims["school"].new_level_strategy == "hyperprior"
    assert schema.vars["school_idx"].index_of == "school"
    assert schema.vars["x1"].dims == ("obs",) and schema.vars["size"].dims == ("school",)


def test_static_validation_reports_missing_covariates(registry: Registry) -> None:
    ok = validate_spec(load_model_spec(RADON_SPEC_YAML), registry.catalog(), RADON_SCHEMA)
    assert ok.ok, [(i.pointer, i.message) for i in ok.errors]
    bad = load_model_spec(RADON_SPEC_YAML.replace("covariates: [floor]", "covariates: [basement]"))
    res = validate_spec(bad, registry.catalog(), RADON_SCHEMA)
    assert not res.ok and any("basement" in i.message for i in res.errors)


@pytest.mark.slow
def test_fit_recovers_the_generating_effects(bundle: Bundle) -> None:
    post = bundle.model.posterior
    assert float(post["beta"].sel(covariate="floor").mean()) == pytest.approx(
        RADON_TRUTH["floor"], abs=0.3
    )
    assert float(post["gamma"].mean()) == pytest.approx(RADON_TRUTH["gamma_uranium"], abs=0.5)
    assert post["a"].sizes["county"] == 8
    assert bundle.model.learned["hier_effects"] == {"a": "county"}
    assert bundle.metrics.values["elpd_loo"] is not None
    assert "log_likelihood" in bundle.model.groups


@pytest.mark.slow
def test_new_county_is_drawn_from_the_hyperprior(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    fitted_rows = data.isel(home=np.flatnonzero(data["county_idx"].values == 0)[:3])
    fitted_rows = fitted_rows.assign(floor=fitted_rows["floor"] * 0)
    low, high = _new_county(-0.8), _new_county(0.8)  # new homes continue the fitted ones
    req = _req("mode: predict\noutput: {kind: expected}\n")
    out_low = score_bundle(bundle, req, low, registry=registry, return_full=True)
    out_high = score_bundle(bundle, req, high, registry=registry, return_full=True)
    known = score_bundle(
        bundle, req, fitted_rows.drop_vars("log_radon"), registry=registry, return_full=True
    )
    y_low = out_low["predictions"].to_dataset()["y_hat"]
    y_high = out_high["predictions"].to_dataset()["y_hat"]
    y_known = known["predictions"].to_dataset()["y_hat"]
    # uranium shifts the new county's prior mean; its spread is the hyperprior's
    assert float(y_high.mean()) > float(y_low.mean())
    assert float(y_low.std(("chain", "draw")).mean()) > float(y_known.std(("chain", "draw")).mean())
    issues = out_low.attrs["issues"]
    assert "UNSEEN_LEVELS" in issues


@pytest.mark.slow
def test_counterfactual_on_a_covariate(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    rows = data.isel(home=slice(0, 10))
    out = score_bundle(
        bundle,
        _req(
            "mode: counterfactual\noutput: {kind: predictive}\n"
            "interventions: [{var: floor, set: 1.0}]\n"
        ),
        rows.drop_vars("log_radon"),
        registry=registry,
    )
    delta = out["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    on_ground = rows["floor"].values == 0
    # moving to the first floor lowers radon by ~|floor effect|; CRN keeps noise shared
    assert float(delta[on_ground].mean()) < -0.3
    np.testing.assert_allclose(delta[~on_ground], 0.0, atol=1e-9)


@pytest.mark.slow
def test_missing_covariate_at_score_is_typed(bundle: Bundle, registry: Registry) -> None:
    payload = _new_county(0.1).drop_vars("floor")
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req("mode: predict\n"), payload, registry=registry)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


@pytest.mark.slow
@pytest.mark.parametrize("family", ["poisson", "bernoulli", "student_t"])
def test_other_families_and_varying_slopes(family: str, registry: Registry) -> None:
    rng = np.random.default_rng(1)
    ds = radon_dataset(counties=5, homes=50, seed=2)
    eta = 0.5 * ds["floor"].values - 0.2
    if family == "poisson":
        y = rng.poisson(np.exp(eta + 1.0)).astype(float)
    elif family == "bernoulli":
        y = (rng.uniform(size=eta.size) < 1 / (1 + np.exp(-eta))).astype(float)
    else:
        y = eta + 0.3 * rng.standard_t(4, size=eta.size)
    ds = ds.assign(log_radon=(("home",), y))
    spec = RADON_SPEC_YAML.replace(
        "group_covariates: [log_uranium]}",
        f"group_covariates: [log_uranium], family: {family}, varying_slopes: [floor]}}",
    ).replace("tune: 200\n    draws: 100", "tune: 60\n    draws: 40")
    b = fit_spec(spec, ds, registry=registry, raw_schema=RADON_SCHEMA)
    assert "slope_floor" in b.model.posterior
    out = score_bundle(b, _req("mode: predict\n"), ds.isel(home=slice(0, 5)), registry=registry)
    mean = out["summary"].to_dataset()["y_hat"].sel(stat="mean").values
    assert np.isfinite(mean).all()
    if family == "bernoulli":
        assert ((mean > 0) & (mean < 1)).all()
    if family == "poisson":
        assert (mean > 0).all()


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_hierarchical_glm_passes_the_contract_kit(registry: Registry, data: xr.Dataset) -> None:
    fixture = ModelFixture(
        spec=RADON_SPEC_YAML, data=data, raw_schema=RADON_SCHEMA, registry=registry
    )
    report = check_model(HierarchicalGLM, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "output_space_and_units",
        "seed_determinism",
    } <= ran
    assert "hyperprior" in report.get("new_level_policies").detail
