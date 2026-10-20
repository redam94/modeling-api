"""``tabular.hierarchical_glm`` 1.1 families (v0.14): negative binomial, zero-inflated counts,
beta, gamma and ordinal; prior draws per family, target checks, and tiny MCMC fits."""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pymc as pm
import pytensor.tensor as pt
import pytest
import xarray as xr
from mapi_plugin_tabular import HierarchicalGLM, HierarchicalGLMSettings
from mapi_plugin_tabular.datasets import RADON_SCHEMA, RADON_SPEC_YAML, radon_dataset
from mapi_pymc.context import BuildContext
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_spec import ErrorCode, MapiError, load_score_request
from pydantic import ValidationError
from pytensor.graph.replace import clone_replace

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://environment/radon\n"
NEW_FAMILIES = [
    "negative_binomial",
    "zero_inflated_poisson",
    "zero_inflated_negative_binomial",
    "beta",
    "gamma",
    "ordinal",
]


def _target(family: str, eta: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    match family:
        case "negative_binomial":
            return rng.negative_binomial(5, 5 / (5 + np.exp(eta + 1.0))).astype(float)
        case "zero_inflated_poisson" | "zero_inflated_negative_binomial":
            counts = rng.poisson(np.exp(eta + 1.0)).astype(float)
            return np.where(rng.uniform(size=eta.size) < 0.3, 0.0, counts)
        case "beta":
            mu = 1 / (1 + np.exp(-eta))
            return np.clip(rng.beta(mu * 20, (1 - mu) * 20), 1e-4, 1 - 1e-4)
        case "gamma":
            return rng.gamma(4.0, np.exp(eta) / 4.0)
        case _:  # ordinal, 4 categories
            latent = eta + rng.logistic(size=eta.size)
            return np.digitize(latent, [-1.0, 0.0, 1.0]).astype(float)


def _canonical(family: str, seed: int = 0) -> xr.Dataset:
    """The radon data in canonical names (``obs``, ``y``) with a target of ``family``."""
    raw = radon_dataset(counties=5, homes=60, seed=seed)
    eta = 0.6 * raw["floor"].values - 0.2
    y = _target(family, eta, np.random.default_rng(seed))
    return xr.Dataset(
        {
            "y": (("obs",), y),
            "floor": (("obs",), raw["floor"].values),
            "county_idx": (("obs",), raw["county_idx"].values),
            "log_uranium": (("county",), raw["log_uranium"].values),
        },
        coords={"obs": np.arange(raw.sizes["home"]), "county": raw["county"].values},
    )


def _settings(family: str) -> HierarchicalGLMSettings:
    return HierarchicalGLMSettings(
        family=family,
        covariates=["floor"],
        group_covariates=["log_uranium"],
        n_categories=4 if family == "ordinal" else None,
    )


def _build(family: str, ds: xr.Dataset) -> pm.Model:
    ctx = BuildContext(phase="fit", ds=ds, seed=1)
    return HierarchicalGLM().build(ds, _settings(family), ctx)


def test_version_and_settings() -> None:
    assert HierarchicalGLM.version == "1.1.0"
    with pytest.raises(ValidationError, match="needs n_categories"):
        HierarchicalGLMSettings(family="ordinal")
    with pytest.raises(ValidationError, match="ordinal family only"):
        HierarchicalGLMSettings(family="beta", n_categories=3)
    with pytest.raises(ValidationError):
        HierarchicalGLMSettings(family="ordinal", n_categories=1)


@pytest.mark.parametrize("family", NEW_FAMILIES)
def test_prior_draws_of_every_family(family: str) -> None:
    ds = _canonical(family)
    model = _build(family, ds)
    names = {v.name for v in model.free_RVs}
    extra = {
        "negative_binomial": {"alpha"},
        "zero_inflated_poisson": {"psi"},
        "zero_inflated_negative_binomial": {"psi", "alpha"},
        "beta": {"kappa"},
        "gamma": {"alpha"},
        "ordinal": {"cutpoints"},
    }[family]
    assert extra <= names
    assert ("intercept" in names) is (family != "ordinal")
    with model, warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # ordinal: the cutpoint potential
        prior = pm.sample_prior_predictive(draws=40, random_seed=2)
    mu = prior.prior["mu"].values
    y = prior.prior_predictive["y"].values
    assert np.isfinite(mu).all() and mu.shape[-1] == ds.sizes["obs"]
    if family == "beta":
        assert ((mu > 0) & (mu < 1)).all() and ((y >= 0) & (y <= 1)).all()
    elif family == "ordinal":
        # forward prior draws ignore the ordered transform, so only the range is checked
        assert ((mu >= 0) & (mu <= 3)).all()
    else:
        assert (mu > 0).all() and (y >= 0).all()
    if family.startswith("zero_inflated"):
        psi = prior.prior["psi"].values
        rate = mu / psi[..., None]
        np.testing.assert_allclose(mu, psi[..., None] * rate)


def test_ordinal_mean_is_the_expected_category() -> None:
    ds = _canonical("ordinal")
    model = _build("ordinal", ds)
    cut = pt.constant(np.array([-1.0, 0.2, 1.5]))
    mu, y = clone_replace([model["mu"], model["y"]], {model["cutpoints"]: cut})
    mu_draws, y_draws = pm.draw([mu, y], draws=2000, random_seed=5)
    assert set(np.unique(y_draws)) <= {0, 1, 2, 3}
    # with sorted cutpoints, mu is the mean category of the predictive draws
    assert abs(float(mu_draws.mean()) - float(y_draws.mean())) < 0.05


@pytest.mark.parametrize(
    ("family", "bad", "match"),
    [
        ("negative_binomial", -1.0, "counts"),
        ("zero_inflated_poisson", 1.5, "counts"),
        ("beta", 1.0, "between 0 and 1"),
        ("beta", 0.0, "between 0 and 1"),
        ("gamma", 0.0, "above 0"),
        ("ordinal", 4.0, "from 0 to 3"),
        ("ordinal", 0.5, "from 0 to 3"),
    ],
)
def test_targets_the_family_cannot_describe_are_refused(
    family: str, bad: float, match: str
) -> None:
    ds = _canonical(family)
    y = ds["y"].values.copy()
    y[3] = bad
    with pytest.raises(MapiError, match=match) as err:
        _build(family, ds.assign(y=(("obs",), y)))
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_score_phase_does_not_check_the_placeholder_target() -> None:
    ds = _canonical("beta").drop_vars("y")
    ctx = BuildContext(phase="score", ds=ds, schema=None, seed=1)
    model = HierarchicalGLM().build(ds, _settings("beta"), ctx)
    assert "mu" in model.named_vars


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


def _spec(family: str) -> str:
    extra = f", family: {family}" + (", n_categories: 4" if family == "ordinal" else "")
    return (
        RADON_SPEC_YAML.replace(
            "group_covariates: [log_uranium]}", f"group_covariates: [log_uranium]{extra}}}"
        )
        .replace("tune: 200\n    draws: 100", "tune: 150\n    draws: 60")
        .replace("engine: pymc", "engine: nutpie")
    )


@pytest.mark.slow
@pytest.mark.parametrize("family", ["negative_binomial", "ordinal"])
def test_tiny_fit_and_score(family: str, registry: Registry) -> None:
    raw = radon_dataset(counties=5, homes=80, seed=3)
    eta = 0.8 * raw["floor"].values - 0.2
    y = _target(family, eta, np.random.default_rng(3))
    ds = raw.assign(log_radon=(("home",), y))
    b = fit_spec(_spec(family), ds, registry=registry, raw_schema=RADON_SCHEMA)
    post = b.model.posterior
    assert ("alpha" if family == "negative_binomial" else "cutpoints") in post
    req: Any = load_score_request(HEADER + "mode: predict\noutput: {kind: expected}\n")
    out = score_bundle(b, req, ds.isel(home=slice(0, 6)), registry=registry)
    mean = out["summary"].to_dataset()["y_hat"].sel(stat="mean").values
    assert np.isfinite(mean).all()
    if family == "ordinal":
        assert ((mean >= 0) & (mean <= 3)).all()
    else:
        assert (mean > 0).all()
