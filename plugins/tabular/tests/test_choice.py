"""``choice.multinomial_logit``: the built-in model of the Guided "customer choice" template,
checked end to end on a ragged synthetic household panel and with the contract kit."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_tabular import MultinomialLogit, MultinomialLogitSettings
from mapi_plugin_tabular.choice import choice_key
from mapi_plugin_tabular.datasets import (
    CHOICE_SCHEMA,
    CHOICE_SPEC_YAML,
    CHOICE_TRUTH,
    choice_panel_dataset,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, load_model_spec, load_score_request, validate_spec
from mapi_spec.guided import GUIDED_TEMPLATES
from pydantic import ValidationError

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://retail/soda\n"


def _req(body: str) -> Any:
    return load_score_request(HEADER + body)


def _y(tree: xr.DataTree) -> xr.DataArray:
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()["y_hat"]


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="module")
def data() -> xr.Dataset:
    return choice_panel_dataset(households=80, seed=2)


@pytest.fixture(scope="module")
def bundle(registry: Registry, data: xr.Dataset) -> Bundle:
    return fit_spec(CHOICE_SPEC_YAML, data, registry=registry, raw_schema=CHOICE_SCHEMA)


def test_settings_and_manifest() -> None:
    with pytest.raises(ValidationError):
        MultinomialLogitSettings(covariates=["price", "price"])
    with pytest.raises(ValidationError):
        MultinomialLogitSettings(covariates=["y"])
    with pytest.raises(ValidationError):
        MultinomialLogitSettings(random_effects=["store"])  # type: ignore[list-item]
    m = MultinomialLogit.manifest()
    assert m.id == "choice.multinomial_logit" and m.capabilities.representation.value == "draws"
    assert m.capabilities.inference == "bootstrap" and not m.capabilities.loo_valid
    assert m.outputs["y_hat"].units == "probability"
    assert choice_key(" Coke Zero! ") == "coke_zero"


def test_the_guided_template_targets_this_plugin() -> None:
    template = GUIDED_TEMPLATES["customer_choice"]
    assert template.plugin_available
    assert template.plugin.split("@")[0] == MultinomialLogit.id


def test_static_validation(registry: Registry) -> None:
    res = validate_spec(load_model_spec(CHOICE_SPEC_YAML), registry.catalog(), CHOICE_SCHEMA)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    bad = load_model_spec(CHOICE_SPEC_YAML.replace("{random_effects: [hh]}",
                                                   "{covariates: [discount]}"))  # fmt: skip
    res = validate_spec(bad, registry.catalog(), CHOICE_SCHEMA)
    assert not res.ok and any("discount" in i.message for i in res.errors)


def test_fit_recovers_the_price_and_promotion_effects(bundle: Bundle) -> None:
    post = bundle.model.groups["posterior"]
    assert dict(post.sizes) == {"chain": 1, "draw": 20, "product": 3, "covariate": 2, "hh": 80}
    beta = post["beta"].mean(("chain", "draw"))
    assert float(beta.sel(covariate="price")) == pytest.approx(CHOICE_TRUTH["price"], abs=0.6)
    assert float(beta.sel(covariate="promo")) == pytest.approx(CHOICE_TRUTH["promo"], abs=0.5)
    assert float(post["asc"].sel(product="cola").max()) == 0.0  # the reference product
    assert float(post["beta"].std(("chain", "draw")).min()) > 0  # the bootstrap spreads
    hit = next(d for d in bundle.diagnostics if d.code == "CHOICE_HIT_RATE")
    assert hit.value is not None and 0.4 < hit.value <= 1.0 and "%" in hit.message_plain
    assert bundle.metrics.split is not None and bundle.metrics.split["kind"] == "last_per_group"


def test_probabilities_respect_availability_and_ragged_trips(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    out = score_bundle(bundle, _req("mode: predict\n"), data.drop_vars("brand"),
                       registry=registry, return_full=True)  # fmt: skip
    p = _y(out)
    assert p.dims == ("chain", "draw", "household", "trip", "product")
    trip_made = data["brand"] != ""
    total = p.sum("product", skipna=False).transpose("chain", "draw", "household", "trip")
    made = np.broadcast_to(trip_made.transpose("household", "trip").values, total.shape)
    np.testing.assert_allclose(total.values[made], 1.0, rtol=1e-9)
    assert float(abs(p.where(~trip_made, 0.0)).max()) == 0.0  # trips never made: no purchase
    off = trip_made & data["price"].sel(product="store").isnull()
    assert int(off.sum()) > 0
    assert float(p.sel(product="store").where(off).max()) == 0.0  # off the shelf: never


def test_predictive_choices_are_one_hot(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    rows = data.isel(household=slice(0, 5)).drop_vars("brand")
    out = score_bundle(bundle, _req("mode: predict\noutput: {kind: predictive}\n"), rows,
                       registry=registry, return_full=True)  # fmt: skip
    p = _y(out)
    made = (data["brand"] != "").isel(household=slice(0, 5))
    np.testing.assert_array_equal(p.sum("product").where(made, 1.0), 1.0)
    np.testing.assert_array_equal(p.sum("product").where(~made, 0.0), 0.0)
    assert set(np.unique(p.values)) <= {0.0, 1.0}


def test_a_new_household_draws_its_preferences(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    known = data.isel(household=[0]).drop_vars("brand")
    new = known.assign_coords(household=["h_new"])
    req = _req("mode: predict\n")
    p_new = _y(score_bundle(bundle, req, new, registry=registry, return_full=True))
    p_known = _y(score_bundle(bundle, req, known, registry=registry, return_full=True))
    spread_new = float(p_new.std(("chain", "draw")).mean())
    spread_known = float(p_known.std(("chain", "draw")).mean())
    assert bool(np.isfinite(p_new.isel(trip=0)).all())
    assert spread_new > spread_known  # the household effect is unknown for a new one


def test_a_price_rise_shifts_the_choice(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    rows = data.drop_vars("brand")
    out = score_bundle(
        bundle,
        _req("mode: counterfactual\ninterventions: [{var: price, sel: {product: cola}, "
             "multiply: 1.5}]\n"),
        rows,
        registry=registry,
    )  # fmt: skip
    delta = out["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert float(delta.sel(product="cola").mean()) < -0.02
    assert float(delta.sel(product="fizz").mean()) > 0


def test_bad_choices_are_refused(registry: Registry, data: xr.Dataset) -> None:
    unknown = data.copy()
    unknown["brand"] = unknown["brand"].where(unknown["brand"] != "fizz", "pepsi")
    with pytest.raises(MapiError) as exc:
        fit_spec(CHOICE_SPEC_YAML, unknown, registry=registry, raw_schema=CHOICE_SCHEMA)
    assert exc.value.code is ErrorCode.SCHEMA_INVALID
    chose_missing = data.copy()
    chose_missing["price"] = chose_missing["price"].where(chose_missing["product"] != "cola")
    with pytest.raises(MapiError):
        fit_spec(CHOICE_SPEC_YAML, chose_missing, registry=registry, raw_schema=CHOICE_SCHEMA)


def test_the_contract_kit_passes(registry: Registry) -> None:
    ragged = choice_panel_dataset(households=30, seed=4)
    fixture = ModelFixture(
        spec=CHOICE_SPEC_YAML, data=ragged, raw_schema=CHOICE_SCHEMA, registry=registry
    )
    report = check_model(MultinomialLogit, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "subset_scoring",
        "new_level_policies",
        "unsupported_mode",
        "representation_aggregation",
        "seed_determinism",
    } <= ran
    assert "hyperprior" in report.get("new_level_policies").detail
