"""Declarative priors: allow-listed distributions, support and dims checks, PyMC build."""

from __future__ import annotations

import numpy as np
import pymc as pm
import pytest
from mapi_plugin_mmm.priors import PRIOR_SLOTS, PriorSpec, build_prior, resolve_priors
from mapi_spec.errors import MapiError
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def spec(**kw: object) -> PriorSpec:
    return PriorSpec.model_validate(kw)


def test_plan_dict_form_parses() -> None:
    p = spec(distribution="HalfNormal", sigma=1.0, dims=["channel"])
    assert p.params == {"sigma": 1.0}
    assert p.support() == "positive"
    g = spec(distribution="Gamma", alpha=3, beta=1, dims=["channel"])
    assert g.params == {"alpha": 3.0, "beta": 1.0}


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"distribution": "HalfNormal", "mu": 1.0}, "does not take"),
        ({"distribution": "Exec", "sigma": 1.0}, "distribution"),
        ({"distribution": "Gamma"}, "needs parameters"),
        ({"distribution": "HalfNormal", "sigma": -1.0}, "must be > 0"),
        ({"distribution": "Normal", "mu": float("nan")}, "finite"),
        ({"distribution": "Normal", "mu": [0.0, 1.0]}, "declares no dims"),
        ({"distribution": "Uniform", "lower": 0.0}, "lower and upper"),
        ({"distribution": "Uniform", "lower": 1.0, "upper": 0.0}, "below upper"),
        ({"distribution": "Normal", "sigma": "1; import os"}, "valid number"),
    ],
)
def test_bad_priors_are_rejected(kw: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        PriorSpec.model_validate(kw)


def test_support_dims_and_names_are_checked_per_slot() -> None:
    assert set(resolve_priors({})) == set(PRIOR_SLOTS)
    with pytest.raises(ValueError, match="positive support"):
        resolve_priors({"beta_channel": spec(distribution="Normal", sigma=1.0)})
    with pytest.raises(ValueError, match="unit support"):
        resolve_priors({"alpha": spec(distribution="HalfNormal", sigma=1.0)})
    with pytest.raises(ValueError, match="may vary over"):
        resolve_priors({"sigma": spec(distribution="HalfNormal", sigma=1.0, dims=["geo"])})
    with pytest.raises(ValueError, match="unknown prior"):
        resolve_priors({"betaa": spec(distribution="HalfNormal", sigma=1.0)})
    ok = resolve_priors(
        {
            "alpha": spec(distribution="Uniform", lower=0.0, upper=0.9),
            "beta_channel": spec(distribution="TruncatedNormal", mu=0.5, sigma=1.0, lower=0.0),
        }
    )
    assert ok["alpha"].support() == "unit"
    assert ok["beta_channel"].support() == "positive"


def test_build_prior_shapes() -> None:
    coords = {"channel": ["tv", "search", "social"], "control_feature": ["a", "b"]}
    with pm.Model(coords=coords):
        shared = build_prior("lam", spec(distribution="Gamma", alpha=3, beta=1), dims=["channel"])
        per = build_prior(
            "beta_channel",
            spec(distribution="HalfNormal", sigma=[1.0, 0.5, 0.2], dims=["channel"]),
            dims=["channel"],
            coords=coords,
        )
        iid = build_prior(
            "gamma", spec(distribution="Normal", mu=0, sigma=0.5), iid_dims=["control_feature"]
        )
        draws = pm.draw([shared, per, iid], draws=2000, random_seed=1)
    assert draws[0].shape == (2000,)
    assert draws[1].shape == (2000, 3)
    assert draws[2].shape == (2000, 2)
    sd = draws[1].std(0) / np.sqrt(1 - 2 / np.pi)
    np.testing.assert_allclose(sd, [1.0, 0.5, 0.2], rtol=0.1)
    with pm.Model(coords=coords), pytest.raises(MapiError, match="has 2 values"):
        build_prior(
            "beta_channel",
            spec(distribution="HalfNormal", sigma=[1.0, 0.5], dims=["channel"]),
            dims=["channel"],
            coords=coords,
        )
