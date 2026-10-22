"""``tabular.declarative_glm`` (v0.18, D3): settings, priors and signs, every family, group
labels encoded at fit, the compiled recipe end to end, and ``hierarchical_glm`` unchanged."""

from __future__ import annotations

import hashlib
import json
import time
import warnings
from typing import Any, get_args

import numpy as np
import pymc as pm
import pytest
import scipy.stats
import xarray as xr
from mapi_plugin_tabular import (
    PLUGINS,
    DeclarativeGLM,
    DeclarativeGLMSettings,
    HierarchicalGLM,
    HierarchicalGLMSettings,
)
from mapi_plugin_tabular._families import FAMILIES
from mapi_plugin_tabular.declarative import LEVELS_KEY, TERM_EFFECTS, PriorDist
from mapi_pymc.context import BuildContext
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.models import ScoreContext
from mapi_sdk.registry import Registry, default_registry
from mapi_sdk.schema import infer_schema
from mapi_sdk.testing import ModelFixture, check_model
from mapi_spec import ErrorCode, MapiError, ModelSpec, load_score_request
from mapi_spec.about import about_issues
from mapi_spec.library import (
    DeclarativeModel,
    GlmFamily,
    ModelRecipeDoc,
    parse_library_doc,
)
from mapi_spec.library import PriorDist as DocPriorDist
from mapi_spec.model_recipes import compile_model_recipe, declarative_settings
from mapi_spec.schema_safety import sanitize_schema
from pydantic import ValidationError

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://retail/sales\n"
TRUTH = {"intercept": 2.0, "price": -0.5, "promo": 0.6, "temp": 0.2}
STORES = 4
HIERARCHICAL_GLM_MANIFEST_SHA256 = (
    "92280a05b85e1700833ca6bd96584dc9d3bce9b402ccb219ed239d7c8c121e2b"
)
"""``tabular.hierarchical_glm@1.1.0`` manifest before the family code moved to
``_families.py`` (v0.18): the move must not change it."""


def _req(body: str) -> Any:
    return load_score_request(HEADER + body)


def _sales(rows: int = 200, seed: int = 0) -> xr.Dataset:
    """A flat table with a string ``store`` label per row (what an analyst uploads)."""
    rng = np.random.default_rng(seed)
    store = rng.integers(0, STORES, rows)
    store[:STORES] = np.arange(STORES)
    base = np.array([0.3, -0.2, 0.1, -0.25])
    lift = np.array([0.45, 0.7, 0.55, 0.75])
    price = rng.normal(3.0, 0.5, rows)
    promo = (rng.uniform(size=rows) < 0.35).astype(float)
    temp = rng.normal(0.0, 1.0, rows)
    eta = (
        TRUTH["intercept"]
        + base[store]
        + TRUTH["price"] * (price - price.mean()) / price.std()
        + lift[store] * promo
        + TRUTH["temp"] * temp
    )
    return xr.Dataset(
        {
            "sales": (("row",), eta + 0.3 * rng.standard_normal(rows)),
            "price": (("row",), price),
            "promo": (("row",), promo),
            "temp": (("row",), temp),
            "store": (("row",), np.array([f"store_{i}" for i in store])),
        },
        coords={"row": np.arange(rows, dtype=np.int64)},
    )


RECIPE: dict[str, Any] = {
    "apiVersion": "mapi/v1",
    "kind": "ModelRecipe",
    "metadata": {"name": "store-sales", "title": "Store sales with promotion lift"},
    "base": "declarative",
    "declarative": {
        "outcome": {"var": "sales"},
        "terms": [
            {
                "var": "price",
                "transform": [{"use": "standardize"}],
                "prior": {"sigma": 0.5},
                "sign": "negative",
            },
            {"var": "promo", "prior": {"sigma": 1.0}, "sign": "positive", "varies_by": "store"},
            {"var": "temp", "prior": {"dist": "student_t", "sigma": 0.5}},
        ],
        "groups": [{"dim": "store"}],
    },
    "plots": ["core.convergence", "core.posterior_forest"],
    "fit": {"seed": 3, "tune": 300, "draws": 150, "chains": 2, "engine": "pymc"},
}


def _schema(ds: xr.Dataset) -> Any:
    """The raw schema an upload gives: rows extend (new rows can be scored)."""
    return infer_schema(ds, targets=["sales"], policies={"row": "extend"})


def _spec(fragment: dict[str, Any], **extra: Any) -> ModelSpec:
    metadata = {"name": "store-sales", "project": "retail", **fragment.get("metadata", {})}
    return ModelSpec.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ModelSpec",
            "metadata": metadata,
            "data": {"dataset": "dataset://retail/sales@latest", **fragment.get("data", {})},
            "pipeline": fragment["pipeline"],
            "model": fragment["model"],
            "outputs": fragment.get("outputs", {}),
            **extra,
        }
    )


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.fixture(scope="module")
def data() -> xr.Dataset:
    return _sales()


@pytest.fixture(scope="module")
def compiled_spec(registry: Registry, data: xr.Dataset) -> ModelSpec:
    doc = parse_library_doc(RECIPE)
    assert isinstance(doc, ModelRecipeDoc)
    schema = _schema(data)
    compiled = compile_model_recipe(
        doc, registry.catalog(), schema=schema, item_ref="lib_01jbq3w0v8d5k2m7n9p4r6s8t0-r1"
    )
    assert compiled.ok, [(i.pointer, i.message) for i in compiled.issues]
    return _spec(compiled.spec_fragment())


@pytest.fixture(scope="module")
def bundle(registry: Registry, data: xr.Dataset, compiled_spec: ModelSpec) -> Bundle:
    return fit_spec(compiled_spec, data, registry=registry, raw_schema=_schema(data))


def _settings(**over: Any) -> DeclarativeGLMSettings:
    base: dict[str, Any] = {
        "covariates": ["price", "promo"],
        "groups": ["store"],
        "terms": {
            "price": {"sign": "negative", "sigma": 0.5},
            "promo": {"sign": "positive", "varies_by": "store"},
        },
    }
    return DeclarativeGLMSettings.model_validate({**base, **over})


def _canonical(y: np.ndarray | None = None, rows: int = 60, seed: int = 0) -> xr.Dataset:
    """Model-stage data: ``obs`` rows, outcome ``y`` and a string ``store`` label."""
    raw = _sales(rows, seed)
    return xr.Dataset(
        {
            "y": (("obs",), raw["sales"].values if y is None else y),
            "price": (("obs",), raw["price"].values - 3.0),
            "promo": (("obs",), raw["promo"].values),
            "temp": (("obs",), raw["temp"].values),
            "store": (("obs",), raw["store"].values),
        },
        coords={"obs": np.arange(rows)},
    )


def _build(s: DeclarativeGLMSettings, ds: xr.Dataset) -> pm.Model:
    return DeclarativeGLM().build(ds, s, BuildContext(phase="fit", ds=ds, seed=1))


def _prior(model: pm.Model, draws: int = 300) -> xr.Dataset:
    with model, warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        tree = pm.sample_prior_predictive(draws=draws, random_seed=4)
    node = tree["prior"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset(inherit=False)


# ---------------------------------------------------------------------- settings


def test_compiled_recipe_settings_are_valid_settings() -> None:
    """The compiler's JSON (WP2) is exactly what this model accepts, for every choice."""
    assert set(FAMILIES) == set(get_args(GlmFamily))
    assert set(get_args(PriorDist)) == set(get_args(DocPriorDist))
    model = DeclarativeModel.model_validate(
        {
            "outcome": {"var": "sales", "family": "ordinal", "n_categories": 4},
            "terms": [
                {"var": "price", "prior": {"dist": "laplace", "sigma": 0.5}, "sign": "negative"},
                {"var": "promo", "prior": {"dist": "lognormal"}, "sign": "positive"},
                {"var": "temp", "prior": {"dist": "student_t", "nu": 5}, "varies_by": "region"},
            ],
            "interactions": [{"a": "price", "b": "promo"}],
            "groups": [{"dim": "store"}, {"dim": "region", "sigma_prior": {"dist": "exponential"}}],
        }
    )
    s = DeclarativeGLMSettings.model_validate(declarative_settings(model))
    assert s.covariates == ["price", "promo", "temp"] and s.groups == ["store", "region"]
    assert s.terms["temp"].varies_by == "region" and s.terms["temp"].nu == 5
    assert s.spread("region").dist == "exponential"


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"family": "ordinal"}, "needs n_categories"),
        ({"n_categories": 3}, "ordinal family only"),
        ({"covariates": ["price", "price"]}, "distinct"),
        ({"covariates": ["price"]}, "one entry for each covariate"),
        ({"groups": ["store", "price"]}, "both a covariate and a group"),
        ({"groups": ["obs"], "terms": {"price": {}, "promo": {}}}, "reserved"),
        ({"covariates": ["store_idx", "price", "promo"]}, "reserved"),
        ({"terms": {"price": {}, "promo": {"varies_by": "region"}}}, "not a group"),
        ({"interactions": [{"a": "price", "b": "temp"}]}, "not covariates"),
        ({"interactions": [{"a": "price", "b": "price"}]}, "two different"),
        (
            {"interactions": [{"a": "price", "b": "promo"}, {"a": "promo", "b": "price"}]},
            "listed twice",
        ),
        ({"group_sigma": {"region": {"dist": "half_normal"}}}, "not groups"),
        ({"group_sigma": {"store": {"dist": "normal"}}}, "half_normal, exponential"),
        ({"noise": {"dist": "laplace"}}, "half_normal, exponential"),
        ({"intercept": {"dist": "normal", "nu": 4}}, "only the student_t"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_settings_refuse_invalid_choices(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _settings(**over)


def test_manifest_template_about_and_schema_safety(registry: Registry) -> None:
    assert DeclarativeGLM in PLUGINS
    assert registry.model("tabular.declarative_glm@^1") is DeclarativeGLM
    man = DeclarativeGLM.manifest()
    assert (man.id, man.version) == ("tabular.declarative_glm", "1.0.0")
    assert man.plots == [
        "core.ppc",
        "core.convergence",
        "core.posterior_forest",
        "core.prior_posterior",
    ]
    assert sorted(man.capabilities.score_modes) == ["counterfactual", "evaluate", "predict"]
    assert [(d.name, d.cache) for d in man.derived] == [(TERM_EFFECTS, "on_demand")]
    schema = man.schema_template.resolve({"covariates": ["price", "promo"]})
    assert schema.layout == "long" and set(schema.dims) == {"obs"}
    assert set(schema.vars) == {"y", "price", "promo"}  # groups come in either shape
    catalog = registry.catalog()
    known = [m.manifest.id for m in catalog.models] + [s.id for s in catalog.steps]
    assert about_issues(man.about, known_ids=known) == []
    assert man.about is not None
    assert man.about.fits == {
        "goal": ["explain_drivers", "predict_values"],
        "data_shape": ["table", "panel"],
    }
    _, issues = sanitize_schema(man.settings_schema, mode="refuse", where="/settings_schema")
    assert issues == []


# ---------------------------------------------------------------------- hierarchical_glm


def test_hierarchical_glm_is_unchanged_by_the_family_move() -> None:
    """``glm.py`` only imports the family code now: same manifest, same graph."""
    man = HierarchicalGLM.manifest().model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(man, sort_keys=True).encode()).hexdigest()
    assert digest == HIERARCHICAL_GLM_MANIFEST_SHA256
    ds = _canonical(rows=40).drop_vars("store")
    ds = ds.assign(store_idx=(("obs",), np.arange(40) % 3)).assign_coords(store=["a", "b", "c"])
    s = HierarchicalGLMSettings(group_dim="store", family="negative_binomial", covariates=["price"])
    ds = ds.assign(y=(("obs",), np.arange(40) % 5 * 1.0))
    model = HierarchicalGLM().build(ds, s, BuildContext(phase="fit", ds=ds, seed=1))
    assert [v.name for v in model.free_RVs] == [
        "intercept",
        "a__sigma",
        "a__offset",
        "beta",
        "alpha",
    ]
    # the dispersion keeps HalfNormal(5 * noise_sigma): median 5 * 0.6745
    alpha = pm.draw(model["alpha"], draws=8000, random_seed=1)
    assert float(np.median(alpha)) == pytest.approx(5 * 0.6745, rel=0.05)


# ---------------------------------------------------------------------- priors and signs


@pytest.mark.parametrize("family", FAMILIES)
def test_every_family_builds_and_draws(family: str) -> None:
    rng = np.random.default_rng(0)
    y = {
        "poisson": rng.poisson(3.0, 60),
        "negative_binomial": rng.poisson(3.0, 60),
        "zero_inflated_poisson": rng.poisson(1.0, 60),
        "zero_inflated_negative_binomial": rng.poisson(1.0, 60),
        "bernoulli": rng.integers(0, 2, 60),
        "beta": rng.uniform(0.1, 0.9, 60),
        "gamma": rng.gamma(2.0, 1.0, 60),
        "ordinal": rng.integers(0, 4, 60),
    }.get(family, rng.normal(size=60))
    s = _settings(
        family=family,
        n_categories=4 if family == "ordinal" else None,
        covariates=["price", "promo", "temp"],
        terms={
            "price": {"sign": "negative", "sigma": 0.5},
            "promo": {"sign": "positive", "varies_by": "store"},
            "temp": {"dist": "laplace"},
        },
        interactions=[{"a": "price", "b": "promo"}],
    )
    model = _build(s, _canonical(np.asarray(y, dtype=float)))
    names = {v.name for v in model.free_RVs}
    assert {"b_price", "b_promo", "b_temp", "ix_0", "sigma_store", "a_store__offset"} <= names
    assert {"sigma_slope_promo", "slope_promo__offset"} <= names
    assert ("intercept" in names) is (family != "ordinal")
    prior = _prior(model, draws=60)
    assert np.isfinite(prior["mu"].values).all()
    assert prior["beta"].sizes["covariate"] == 3
    assert list(prior["beta_interaction"]["interaction"].values) == ["price:promo"]
    assert np.isfinite(model.point_logps()["y"])


@pytest.mark.parametrize(
    "dist", ["normal", "student_t", "laplace", "half_normal", "exponential", "lognormal"]
)
@pytest.mark.parametrize("sign", ["positive", "negative"])
def test_prior_predictive_respects_the_sign(dist: str, sign: str) -> None:
    s = _settings(
        groups=[],
        terms={"price": {"dist": dist, "sign": sign, "mu": 0.3}, "promo": {}},
    )
    prior = _prior(_build(s, _canonical()))
    price = prior["beta"].sel(covariate="price").values
    assert np.isfinite(price).all()
    assert (price < 0).all() if sign == "negative" else (price > 0).all()
    # the unsigned term keeps both signs
    promo = prior["beta"].sel(covariate="promo").values
    assert (promo < 0).any() and (promo > 0).any()


@pytest.mark.parametrize("dist", ["normal", "student_t", "laplace"])
@pytest.mark.parametrize("sign", ["positive", "negative"])
def test_signed_prior_is_the_truncated_prior(dist: str, sign: str) -> None:
    """The free size ``b_<v>`` has the prior's density on one side of 0, renormalised, and
    forward draws follow it (exact truncation, not a fold)."""
    mu, sigma = 0.4, 0.8
    s = _settings(
        groups=[],
        terms={"price": {"dist": dist, "sign": sign, "mu": mu, "sigma": sigma}, "promo": {}},
    )
    model = _build(s, _canonical())
    frozen = {
        "normal": scipy.stats.norm(mu, sigma),
        "student_t": scipy.stats.t(3.0, mu, sigma),
        "laplace": scipy.stats.laplace(mu, sigma),
    }[dist]
    flip = -1.0 if sign == "negative" else 1.0
    mass = frozen.cdf(0.0) if sign == "negative" else frozen.sf(0.0)
    logp = model.compile_logp(vars=[model["b_price"]], jacobian=False)
    point = model.initial_point()
    for v in (0.1, 0.7, 2.0):
        point["b_price_log__"] = np.log(v)
        want = frozen.logpdf(flip * v) - np.log(mass)
        assert float(logp(point)) == pytest.approx(want, abs=1e-6)
    size = _prior(model, draws=2000)["b_price"].values.ravel()
    assert (size > 0).all()
    reference = flip * frozen.rvs(size=20000, random_state=1)
    reference = reference[reference > 0]
    assert scipy.stats.ks_2samp(size, reference).pvalue > 0.01


def test_a_sign_the_prior_almost_excludes_is_refused() -> None:
    s = _settings(
        groups=[], terms={"price": {"mu": 50.0, "sigma": 1.0, "sign": "negative"}, "promo": {}}
    )
    with pytest.raises(MapiError) as err:
        _build(s, _canonical())
    assert err.value.code is ErrorCode.SETTINGS_INVALID and "b_price" in err.value.detail


def test_noise_prior_reaches_the_family_parameters() -> None:
    s = _settings(family="negative_binomial", noise={"dist": "lognormal", "mu": 0.0, "sigma": 0.5})
    model = _build(s, _canonical(np.arange(60) % 4 * 1.0))
    alpha = _prior(model, draws=2000)["alpha"].values
    # the negative binomial dispersion is the noise prior times 5: LogNormal(log 5, 0.5)
    assert np.median(alpha) == pytest.approx(5.0, rel=0.1)


# ---------------------------------------------------------------------- groups


def test_groups_come_as_labels_or_as_an_index_with_a_dim() -> None:
    s = _settings()
    labels = _canonical()
    levels = sorted(set(labels["store"].values.tolist()))
    idx = np.array([levels.index(v) for v in labels["store"].values])
    indexed = (
        labels.drop_vars("store").assign(store_idx=(("obs",), idx)).assign_coords(store=levels)
    )
    a, b = _build(s, labels), _build(s, indexed)
    assert list(a.coords["store"]) == list(b.coords["store"]) == levels
    np.testing.assert_array_equal(a["store__code"].get_value(), b["store__code"].get_value())
    # the order of the dim does not matter: rows keep their label
    perm = np.arange(len(levels))[::-1]
    shuffled = indexed.isel(store=perm).assign(store_idx=(("obs",), np.argsort(perm)[idx]))
    c = _build(s, shuffled)
    np.testing.assert_array_equal(a["store__code"].get_value(), c["store__code"].get_value())
    with pytest.raises(MapiError, match="has no group 'store'") as err:
        _build(s, labels.drop_vars("store"))
    assert err.value.code is ErrorCode.SCHEMA_INVALID
    holes = labels.assign(
        store=(("obs",), np.where(np.arange(60) == 3, None, labels["store"].values))
    )
    with pytest.raises(MapiError, match="without a label"):
        _build(s, holes)


def test_crossed_groups_build_one_intercept_each() -> None:
    ds = _canonical()
    ds = ds.assign(region=(("obs",), np.where(np.arange(60) % 2 == 0, "north", "south")))
    s = _settings(groups=["store", "region"], group_sigma={"region": {"dist": "exponential"}})
    model = _build(s, ds)
    names = {v.name for v in model.free_RVs}
    assert {"a_store__offset", "a_region__offset", "sigma_store", "sigma_region"} <= names
    assert list(model.coords["region"]) == ["north", "south"]


# ---------------------------------------------------------------------- end to end


@pytest.mark.slow
def test_compiled_recipe_fits_quickly_and_recovers_the_effects(
    registry: Registry, data: xr.Dataset, compiled_spec: ModelSpec
) -> None:
    """compile → spec → local fit on CPU in under 60 s; 3 terms and 1 group recovered."""
    assert compiled_spec.model.use.id == "tabular.declarative_glm"
    assert compiled_spec.metadata.labels["mapi.library/model"].startswith("lib_")
    started = time.perf_counter()
    b = fit_spec(compiled_spec, data, registry=registry, raw_schema=_schema(data))
    assert time.perf_counter() - started < 60
    post = b.model.posterior
    beta = post["beta"].mean(("chain", "draw"))
    assert float(beta.sel(covariate="price")) == pytest.approx(TRUTH["price"], abs=0.15)
    assert float(beta.sel(covariate="promo")) == pytest.approx(0.6, abs=0.2)
    assert float(beta.sel(covariate="temp")) == pytest.approx(TRUTH["temp"], abs=0.15)
    assert (post["beta"].sel(covariate="price") <= 0).all()
    assert (post["beta"].sel(covariate="promo") >= 0).all()
    assert b.model.learned[LEVELS_KEY] == {"store": [f"store_{i}" for i in range(STORES)]}
    assert post["a_store"].sizes["store"] == STORES and post["slope_promo"].sizes["store"] == STORES
    assert not [d for d in b.model.manifest.capabilities.score_modes if d == "forecast"]


@pytest.mark.slow
def test_scoring_matches_rows_by_label(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    req = _req("mode: predict\noutput: {kind: expected}\n")
    rows = data.drop_vars("sales").isel(row=slice(0, 12))
    straight = score_bundle(bundle, req, rows, registry=registry)["summary"].to_dataset()
    shuffled = score_bundle(bundle, req, rows.isel(row=np.arange(12)[::-1]), registry=registry)[
        "summary"
    ].to_dataset()
    xr.testing.assert_allclose(straight.sortby("row"), shuffled.sortby("row"))


@pytest.mark.slow
def test_unseen_store_is_drawn_from_the_group_spread(
    bundle: Bundle, data: xr.Dataset, registry: Registry
) -> None:
    start = int(data["row"].values.max()) + 1
    new = xr.Dataset(
        {
            "price": (("row",), np.full(6, 3.0)),
            "promo": (("row",), np.array([0.0, 1.0, 0.0, 1.0, 0.0, 0.0])),
            "temp": (("row",), np.zeros(6)),
            "store": (("row",), np.array(["store_0"] * 2 + ["store_new"] * 4)),
        },
        coords={"row": np.arange(start, start + 6)},
    )
    out = score_bundle(
        bundle, _req("mode: predict\noutput: {kind: expected, draws: 60}\n"), new, registry=registry
    )
    sd = out["draws"].to_dataset()["y_hat"].std("sample").values
    assert sd[2:].mean() > sd[:2].mean()
    cf = score_bundle(
        bundle,
        _req(
            "mode: counterfactual\noutput: {kind: expected}\n"
            "interventions: [{var: promo, set: 1.0}]\n"
        ),
        new,
        registry=registry,
    )
    delta = cf["delta/summary"].to_dataset()["y_hat"].sel(stat="mean").values
    promo = new["promo"].values
    assert (delta[promo == 0] > 0).all() and np.allclose(delta[promo == 1], 0.0)
    with pytest.raises(MapiError) as err:
        score_bundle(bundle, _req("mode: predict\n"), new.drop_vars("store"), registry=registry)
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


@pytest.mark.slow
def test_term_effects_are_derived_per_row(bundle: Bundle, registry: Registry) -> None:
    plugin = registry.model("tabular.declarative_glm@1.0.0")()
    const = bundle.model.groups["constant_data"]
    x = const["X"].transpose("obs", ...).values  # columns in covariate order
    ds = xr.Dataset(
        {v: (("obs",), x[:, j]) for j, v in enumerate(("price", "promo", "temp"))},
        coords={"obs": const["obs"].values},
    )
    ds = ds.assign(store=(("obs",), np.array(["store_1"] * (ds.sizes["obs"] - 1) + ["store_zz"])))
    ctx = ScoreContext(mode="predict", kind="expected", seed=0, draw_index=np.arange(25))
    effect = plugin.derive(bundle.model, TERM_EFFECTS, ds, ctx)["effect"]
    assert effect.dims == ("sample", "term", "obs") and effect.sizes["sample"] == 25
    assert list(effect["term"].values) == ["price", "promo", "temp"]
    post = bundle.model.posterior.stack({"sample": ("chain", "draw")}).isel(sample=slice(0, 25))
    promo_beta = post["beta"].sel(covariate="promo").values
    slope = post["slope_promo"].sel(store="store_1").values
    row = int(np.flatnonzero(ds["promo"].values == 1)[0])
    np.testing.assert_allclose(
        effect.sel(term="promo").isel(obs=row).values, promo_beta + slope, rtol=1e-6
    )
    last = ds.sizes["obs"] - 1  # an unseen store: the population slope only
    np.testing.assert_allclose(
        effect.sel(term="promo").isel(obs=last).values, promo_beta * ds["promo"].values[last]
    )
    with pytest.raises(MapiError) as err:
        plugin.derive(bundle.model, "contributions", ds, ctx)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def _panel(weeks: int = 20, seed: int = 3) -> xr.Dataset:
    """The upload page's default shape for a flat CSV with a store column: a (week, store)
    grid."""
    rng = np.random.default_rng(seed)
    stores = ["north", "south", "east"]
    price = rng.normal(3.0, 0.4, (weeks, len(stores)))
    promo = (rng.uniform(size=(weeks, len(stores))) < 0.4).astype(float)
    sales = 2.0 + np.array([0.2, -0.2, 0.0]) - 0.8 * (price - 3.0) + 0.5 * promo
    sales = sales + 0.2 * rng.standard_normal((weeks, len(stores)))
    start = np.datetime64("2025-01-06")
    return xr.Dataset(
        {
            n: (("week", "store"), v)
            for n, v in (("sales", sales), ("price", price), ("promo", promo))
        },
        coords={"week": start + np.arange(weeks) * np.timedelta64(7, "D"), "store": stores},
    )


def _panel_spec(registry: Registry, panel: xr.Dataset) -> ModelSpec:
    recipe = {
        **RECIPE,
        "declarative": {
            "outcome": {"var": "sales"},
            "terms": [
                {"var": "price", "prior": {"sigma": 1.0}, "sign": "negative"},
                {"var": "promo", "prior": {"sigma": 1.0}, "sign": "positive"},
            ],
            "groups": [{"dim": "store"}],
        },
    }
    doc = parse_library_doc(recipe)
    assert isinstance(doc, ModelRecipeDoc)
    compiled = compile_model_recipe(
        doc, registry.catalog(), schema=infer_schema(panel, targets=["sales"])
    )
    assert compiled.ok, [(i.pointer, i.message) for i in compiled.issues]
    assert compiled.pipeline[0].use.id == "core.to_long"
    return _spec(compiled.spec_fragment())


@pytest.mark.slow
def test_dense_panel_is_made_long_by_the_compiler(registry: Registry) -> None:
    """The compiler adds core.to_long; the model reads ``store_idx`` with the ``store`` dim."""
    panel = _panel()
    b = fit_spec(
        _panel_spec(registry, panel),
        panel,
        registry=registry,
        raw_schema=infer_schema(panel, targets=["sales"]),
    )
    beta = b.model.posterior["beta"].mean(("chain", "draw"))
    assert float(beta.sel(covariate="price")) == pytest.approx(-0.8, abs=0.25)
    assert b.model.learned[LEVELS_KEY] == {"store": ["east", "north", "south"]}


@pytest.mark.slow
@pytest.mark.mapi_contract
def test_declarative_glm_passes_the_contract_kit(
    registry: Registry, data: xr.Dataset, compiled_spec: ModelSpec
) -> None:
    """Label shape: no categorical dim, so the kit skips its shuffle and subset checks
    (test_scoring_matches_rows_by_label covers them for labels)."""
    fixture = ModelFixture(
        spec=compiled_spec, data=data, raw_schema=_schema(data), registry=registry
    )
    report = check_model(DeclarativeGLM, fixture, raise_on_failure=False, require_about=True)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "about",
        "bundle_roundtrip_no_pickle",
        "seed_determinism",
        "plots_sanitize",
    } <= ran


def test_group_labels_do_not_depend_on_the_column_dtype() -> None:
    # v0.18 review: a fit on an int store column and a score on a float one ('1.0') saw every
    # store as unseen and drew it from the hyperprior
    from mapi_plugin_tabular.declarative import _row_labels

    ints = xr.Dataset({"store": (("obs",), np.array([1, 2, 10], dtype=np.int64))})
    floats = xr.Dataset(
        {"store": (("obs",), np.array([1.0, 2.0, np.nan]))},
        coords={"obs__requested": (("obs",), np.array([True, True, False]))},
    )
    assert _row_labels(ints, "store", "fit") == ["1", "2", "10"]
    assert _row_labels(floats, "store", "score") == ["1", "2", None]
    halves = xr.Dataset({"store": (("obs",), np.array([1.5, 2.0]))})
    assert _row_labels(halves, "store", "score") == ["1.5", "2"]
    flags = xr.Dataset({"store": (("obs",), np.array([True, False]))})
    assert _row_labels(flags, "store", "fit") == ["True", "False"]
    text = xr.Dataset({"store": (("obs",), np.array(["store_1", "1.0"]))})
    assert _row_labels(text, "store", "fit") == ["store_1", "1.0"]  # text stays text
