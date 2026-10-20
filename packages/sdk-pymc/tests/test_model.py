"""PyMCModel end to end through the SDK harness: fit groups, new levels, forecasts, CRN."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_pymc import MemoryCheckpointSink, PyMCModel
from mapi_pymc.context import BuildContext
from mapi_pymc.examples import (
    RADON_SCHEMA,
    RADON_SPEC_YAML,
    RadonModel,
    RadonSettings,
    radon_dataset,
)
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.models import FitContext, ModelSettings
from mapi_sdk.registry import Registry
from mapi_spec import DataSchema, load_model_spec, load_score_request
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig, ScoreRequest

PREDICT = "mode: predict\noutput: {kind: expected}\n"
HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://demo/m\n"


def score_request(body: str) -> ScoreRequest:
    """A ScoreRequest from YAML lines appended to a common header."""
    return load_score_request(HEADER + body)


def _payload(counties: list[str], *, per: int = 3, start: int = 48, seed: int = 5) -> xr.Dataset:
    ds = radon_dataset(counties=len(counties), per_county=per, seed=seed, obs_start=start)
    return ds.assign_coords(county=counties).drop_vars("y")


def _pred(tree: xr.DataTree, group: str = "predictions") -> xr.Dataset:
    node = tree[group]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()


def _registry_with(*models: Any) -> Registry:
    reg = Registry()
    for m in models:
        reg.register_model(m)
    return reg


# ------------------------------------------------------------------------------------ fit


def test_fit_keeps_opt_in_groups_and_reports_diagnostics(radon_bundle: Bundle) -> None:
    groups = radon_bundle.model.groups
    assert set(groups) == {
        "posterior",
        "sample_stats",
        "observed_data",
        "constant_data",
        "log_likelihood",
        "prior",
        "prior_predictive",
        "posterior_predictive",
    }
    assert dict(groups["posterior"].sizes) == {"chain": 2, "draw": 150, "county": 6, "obs": 48}
    assert groups["posterior_predictive"].sizes["draw"] == 30  # thin: 5
    assert groups["prior"].sizes["draw"] == 40
    assert "perf_counter_diff" not in groups["sample_stats"]
    assert groups["posterior"].attrs == {
        "inference_library": "pymc",
        "inference_library_version": pm.__version__,
        "engine": "pymc",
    }
    assert set(groups["constant_data"].data_vars) == {"floor", "county_idx"}
    codes = {d.code for d in radon_bundle.diagnostics}
    assert codes >= {"RHAT", "ESS_BULK", "ESS_TAIL", "DIVERGENCES"}
    assert all(d.message_plain for d in radon_bundle.diagnostics)
    assert radon_bundle.metrics.values["elpd_loo"] is not None
    learned = radon_bundle.model.learned
    assert learned["engine"] == "pymc" and learned["hier_effects"] == {"a": "county"}
    post = groups["posterior"]
    assert abs(float(post["b"].mean()) + 0.7) < 0.3  # floor slope recovered


def test_bundle_round_trip_scores_identically(
    radon_bundle: Bundle, registry: Registry, tmp_path: Path
) -> None:
    radon_bundle.write(tmp_path)
    back = Bundle.read(tmp_path, registry)
    payload = _payload(["county_1", "county_new"])
    req = score_request("mode: predict\noutput: {kind: predictive}\n")
    a = score_bundle(radon_bundle, req, payload, registry=registry)
    b = score_bundle(back, req, payload, registry=registry)
    xr.testing.assert_identical(_pred(a, "summary"), _pred(b, "summary"))


def test_fit_checkpoints_through_the_harness(registry: Registry, radon_data: xr.Dataset) -> None:
    spec = load_model_spec(
        RADON_SPEC_YAML.replace("tune: 150, draws: 150", "tune: 30, draws: 40").replace(
            "engine: pymc,", "engine: pymc, checkpoint: {every_draws: 10},"
        )
    )
    sink = MemoryCheckpointSink()
    events: list[str] = []
    b = fit_spec(
        spec,
        radon_data,
        registry=registry,
        raw_schema=RADON_SCHEMA,
        hooks=FitHooks(checkpoint_sink=sink, on_progress=lambda f, m: events.append(m)),
    )
    assert [c for _, c in sink.history] == [10, 20, 30]
    assert b.model.learned["checkpoints"] == 3 and not sink.saved  # cleared after success
    assert any(m.startswith("chain ") for m in events)


def test_map_engine_gives_a_single_draw(registry: Registry, radon_data: xr.Dataset) -> None:
    spec = load_model_spec(RADON_SPEC_YAML.replace("engine: pymc", "engine: map"))
    b = fit_spec(spec, radon_data, registry=registry, raw_schema=RADON_SCHEMA)
    post = b.model.posterior
    assert (post.sizes["chain"], post.sizes["draw"]) == (1, 1)
    assert not [d for d in b.diagnostics if d.code == "RHAT"]
    tree = score_bundle(b, score_request(PREDICT), _payload(["county_0"]), registry=registry)
    assert np.isfinite(_pred(tree, "summary")["y"].values).all()


def test_advi_fit_keeps_the_loss_history(registry: Registry, radon_data: xr.Dataset) -> None:
    yaml = RADON_SPEC_YAML.replace("tune: 150", "tune: 200").replace("engine: pymc", "engine: advi")
    spec = load_model_spec(yaml)
    b = fit_spec(spec, radon_data, registry=registry, raw_schema=RADON_SCHEMA)
    hist = b.model.groups["fit_history"]
    assert hist["loss"].sizes["step"] == 200
    assert hist["loss"].attrs["objective"] == "negative ELBO"


# -------------------------------------------------------------------------------- scoring


def test_expected_scores_match_the_posterior_per_draw(
    radon_bundle: Bundle, registry: Registry
) -> None:
    payload = _payload(["county_3", "county_new"])
    tree = score_bundle(
        radon_bundle, score_request(PREDICT), payload, registry=registry, return_full=True
    )
    pred = _pred(tree)["y"]
    assert pred.attrs["kind"] == "expected" and pred.attrs["noise"] == "none"
    post = radon_bundle.model.posterior
    floor = payload["floor"].values
    known = post["a"].sel(county="county_3") + post["b"] * floor[0]
    xr.testing.assert_allclose(pred.isel(obs=0).drop_vars("obs"), known.drop_vars("county"))
    # the new county is drawn from the fitted hyperprior: wider than a known county
    new = pred.isel(obs=3) - post["b"] * floor[3]
    assert float(new.std()) > 2 * float(pred.isel(obs=0).std())
    assert abs(float(new.mean()) - float(post["a__mu"].mean())) < 3 * float(post["a__sigma"].mean())
    again = score_bundle(
        radon_bundle, score_request(PREDICT), payload, registry=registry, return_full=True
    )
    xr.testing.assert_identical(_pred(again)["y"], pred)
    reseeded = score_bundle(
        radon_bundle,
        score_request(PREDICT + "seed: 99\n"),
        payload,
        registry=registry,
        return_full=True,
    )
    assert not np.allclose(_pred(reseeded)["y"].isel(obs=3), pred.isel(obs=3))
    np.testing.assert_allclose(_pred(reseeded)["y"].isel(obs=0), pred.isel(obs=0))


def test_shuffled_and_subset_counties_score_the_same(
    radon_bundle: Bundle, registry: Registry
) -> None:
    payload = _payload(["county_2", "county_4", "county_0"])
    base = _pred(
        score_bundle(radon_bundle, score_request(PREDICT), payload, registry=registry), "summary"
    )
    shuffled = payload.isel(county=[2, 0, 1])
    mapping = {0: 1, 1: 2, 2: 0}  # old position -> new position after the shuffle
    shuffled = shuffled.assign(
        county_idx=("obs", np.array([mapping[int(i)] for i in payload["county_idx"].values]))
    )
    got = _pred(
        score_bundle(radon_bundle, score_request(PREDICT), shuffled, registry=registry), "summary"
    )
    xr.testing.assert_allclose(got, base)


def test_predictive_scores_use_frozen_icdf_noise(radon_bundle: Bundle, registry: Registry) -> None:
    payload = _payload(["county_1"])
    req = score_request("mode: predict\noutput: {kind: predictive}\n")
    a = _pred(score_bundle(radon_bundle, req, payload, registry=registry, return_full=True))["y"]
    b = _pred(score_bundle(radon_bundle, req, payload, registry=registry, return_full=True))["y"]
    assert a.attrs["kind"] == "predictive" and a.attrs["noise"] == "crn_icdf"
    assert a.attrs["output_space"] == "raw" and a.attrs["units"] == "log_pCi_L"
    xr.testing.assert_identical(a, b)
    expected = _pred(
        score_bundle(
            radon_bundle, score_request(PREDICT), payload, registry=registry, return_full=True
        )
    )["y"]
    resid = (a - expected) / radon_bundle.model.posterior["sigma"]
    assert 0.6 < float(resid.std()) < 1.4  # standard normal noise around E[y]


def test_crn_counterfactual_delta_has_less_variance_than_independent_noise(
    radon_bundle: Bundle, registry: Registry
) -> None:
    payload = _payload(["county_0", "county_1"], per=4).assign(floor=("obs", np.zeros(8)))
    cf = score_request(
        "mode: counterfactual\ninterventions: [{var: floor, set: 1.0}]\n"
        "output: {kind: predictive}\n"
    )
    tree = score_bundle(radon_bundle, cf, payload, registry=registry, return_full=True)
    delta = _pred(tree, "delta/predictions")["y"]
    b = radon_bundle.model.posterior["b"]
    # common random numbers: the noise cancels and the delta is exactly the floor effect
    xr.testing.assert_allclose(delta, b.broadcast_like(delta).transpose(*delta.dims))
    # independent noise (different seeds for scenario and baseline) inflates the variance
    scen = payload.assign(floor=("obs", np.ones(8)))
    pred = score_request("mode: predict\noutput: {kind: predictive}\nseed: 1\n")
    other = score_request("mode: predict\noutput: {kind: predictive}\nseed: 2\n")
    ys = _pred(score_bundle(radon_bundle, pred, scen, registry=registry, return_full=True))["y"]
    yb = _pred(score_bundle(radon_bundle, other, payload, registry=registry, return_full=True))["y"]
    var_indep = float((ys - yb).var(("chain", "draw")).mean())
    var_crn = float(delta.var(("chain", "draw")).mean())
    assert var_crn < 0.25 * var_indep
    exp = score_request(
        "mode: counterfactual\ninterventions: [{var: floor, set: 1.0}]\noutput: {kind: expected}\n"
    )
    d_exp = _pred(
        score_bundle(radon_bundle, exp, payload, registry=registry, return_full=True),
        "delta/predictions",
    )["y"]
    xr.testing.assert_allclose(d_exp, delta)


def test_seasonal_trend_forecast_keeps_its_phase(registry: Registry) -> None:
    from mapi_pymc.examples import TREND_SCHEMA, TREND_SPEC_YAML, trend_dataset

    data = trend_dataset(weeks=156, seed=1)
    t = np.arange(156)
    data = data.assign(y=data["y"] + 0.5 * np.sin(2 * np.pi * t / 52.18))
    spec = load_model_spec(
        TREND_SPEC_YAML.replace(
            "use: examples.trend@^1", "use: examples.trend@^1\n  settings: {order: 1}"
        )
        .replace("engine: pymc", "engine: map")
        .replace("evaluate_then_refit", "train_only")
    )
    b = fit_spec(spec, data, registry=registry, raw_schema=TREND_SCHEMA)
    assert "gamma" in b.model.posterior
    start = b.fit_coords["raw"].sizes["time"]  # train_only: the last 6 weeks are held out
    assert start == 150
    req = score_request(
        "mode: forecast\nhorizon: {dim: time, periods: 26}\noutput: {kind: expected}\n"
    )
    pred = _pred(score_bundle(b, req, None, registry=registry, return_full=True))["y"]
    truth = (
        2
        + 1.5 * (start + np.arange(26)) / 52
        + 0.5 * np.sin(2 * np.pi * (start + np.arange(26)) / 52.18)
    )
    np.testing.assert_allclose(pred.squeeze(("chain", "draw")).values, truth, atol=0.25)


def test_time_extension_forecast(trend_bundle: Bundle, registry: Registry) -> None:
    assert set(trend_bundle.metrics.values) >= {"rmse", "mape", "coverage_90"}
    assert trend_bundle.metrics.values["rmse"] < 0.5
    req = score_request(
        "mode: forecast\nhorizon: {dim: time, periods: 8}\noutput: {kind: expected}\n"
    )
    tree = score_bundle(trend_bundle, req, None, registry=registry, return_full=True)
    pred = _pred(tree)["y"]
    fit_time = trend_bundle.fit_coords["raw"]["time"].values
    assert pred.sizes["time"] == 8 and pred["time"].values[0] > fit_time[-1]
    post = trend_bundle.model.posterior
    t = (len(fit_time) + np.arange(8)) / 52.0
    want = post["a"].values[..., None] + post["b"].values[..., None] * t
    np.testing.assert_allclose(pred.transpose("chain", "draw", "time").values, want)
    too_far = score_request("mode: forecast\nhorizon: {dim: time, periods: 60}\n")
    with pytest.raises(MapiError) as err:
        score_bundle(trend_bundle, too_far, None, registry=registry)
    assert err.value.code is ErrorCode.HORIZON_EXCEEDS_FIT


# --------------------------------------------------------------------------- author errors


class _NotAModel(RadonModel):
    id = "test.not_a_model"

    def build(self, ds: xr.Dataset, s: RadonSettings, ctx: BuildContext) -> pm.Model:
        return "nope"  # type: ignore[return-value]


class _MissingExpected(RadonModel):
    id = "test.missing_expected"
    expected: ClassVar[dict[str, str]] = {"y": "nothing_here"}


class _FlatIntercepts(RadonModel):
    """Indexes a free parameter by a new_levels dim without hier_effect (an author bug)."""

    id = "test.flat"

    def build(self, ds: xr.Dataset, s: RadonSettings, ctx: BuildContext) -> pm.Model:
        with pm.Model(coords=ctx.coords()) as model:
            floor = ctx.data("floor")
            county = ctx.data("county_idx")
            a = pm.Normal("a", 1.0, 1.0, dims="county")
            mu = pm.Deterministic("mu", a[county] + 0 * floor, dims="obs")
            pm.Normal("y", mu, 1.0, observed=ctx.observed("y"), dims="obs")
        return model


@pytest.mark.parametrize("cls", [_NotAModel, _MissingExpected])
def test_build_contract_errors(cls: type[PyMCModel[Any]], radon_data: xr.Dataset) -> None:
    plugin = cls()
    cfg = FitConfig.model_validate({"engine": "map"})
    with pytest.raises(MapiError) as err:
        plugin.fit(radon_data, plugin.parse_settings({}), cfg, FitContext(schema=RADON_SCHEMA))
    assert err.value.code is ErrorCode.USER_CODE_ERROR


def test_parameters_over_new_levels_need_hier_effect(radon_data: xr.Dataset) -> None:
    reg = _registry_with(_FlatIntercepts)
    spec = load_model_spec(
        RADON_SPEC_YAML.replace("examples.radon", "test.flat").replace(
            "engine: pymc", "engine: map"
        )
    )
    b = fit_spec(spec, radon_data, registry=reg, raw_schema=RADON_SCHEMA)
    ok = score_bundle(b, score_request(PREDICT), _payload(["county_1"]), registry=reg)
    assert np.isfinite(_pred(ok, "summary")["y"].values).all()
    with pytest.raises(MapiError) as err:
        score_bundle(b, score_request(PREDICT), _payload(["county_1", "brand_new"]), registry=reg)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS
    assert "hier_effect" in (err.value.untrusted_text or "")


class _CountSettings(ModelSettings):
    pass


class _Counts(PyMCModel[_CountSettings]):
    """Poisson counts: no symbolic inverse CDF, so predictive noise uses a numeric one."""

    id = "test.counts"
    version = "1.0.0"
    Settings = _CountSettings
    schema_template: ClassVar[dict[str, Any]] = {
        "template": {
            "dims": {"obs": {"kind": "integer", "policy": "extend"}},
            "vars": {"y": {"dims": ["obs"], "dtype": "float64", "role": "target"}},
        }
    }
    outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["obs"], "units_of": "y"}}
    default_engine = "map"
    pad_extend = False

    def build(self, ds: xr.Dataset, s: _CountSettings, ctx: BuildContext) -> pm.Model:
        with pm.Model(coords=ctx.coords()) as model:
            rate = pm.Gamma("rate", 2.0, 1.0)
            pm.Poisson("y", rate, observed=ctx.observed("y", dims=["obs"]), dims="obs")
        return model


def test_distributions_without_icdf_fall_back_to_seeded_noise() -> None:
    reg = _registry_with(_Counts)
    data = xr.Dataset({"y": ("obs", np.array([1.0, 3, 2, 4]))}, coords={"obs": np.arange(4)})
    spec = load_model_spec(
        RADON_SPEC_YAML.replace("examples.radon", "test.counts")
        .replace(
            "keep: {log_likelihood: true, posterior_predictive: {thin: 5}},", "keep: {prior: true},"
        )
        .replace("prior_predictive: {draws: 40}", "engine: map")
        .replace("engine: pymc,", "")
    )
    schema = DataSchema.model_validate(
        {
            "dims": {"obs": {"kind": "integer", "policy": "extend"}},
            "vars": {"y": {"dims": ["obs"], "dtype": "float64", "role": "target"}},
        }
    )
    b = fit_spec(spec, data, registry=reg, raw_schema=schema)
    assert set(b.model.groups) >= {"prior"} and "prior_predictive" not in b.model.groups
    req = score_request(
        "mode: forecast\nhorizon: {dim: obs, periods: 3}\noutput: {kind: predictive}\n"
    )
    a = _pred(score_bundle(b, req, None, registry=reg, return_full=True))["y"]
    again = _pred(score_bundle(b, req, None, registry=reg, return_full=True))["y"]
    assert a.attrs["noise"] == "crn_ppf"  # frozen uniforms through the Poisson ppf
    xr.testing.assert_identical(a, again)
    with pytest.raises(MapiError) as err:
        score_bundle(
            b, score_request("mode: predict\noutput: {kind: expected}\n"), None, registry=reg
        )
    assert err.value.code in (ErrorCode.CAPABILITY_UNSUPPORTED, ErrorCode.SCHEMA_INVALID)


class _CountsX(PyMCModel[_CountSettings]):
    """Poisson counts with a covariate: CRN must keep untouched cells' noise identical."""

    id = "test.counts_x"
    version = "1.0.0"
    Settings = _CountSettings
    schema_template: ClassVar[dict[str, Any]] = {
        "template": {
            "dims": {"obs": {"kind": "integer", "policy": "extend"}},
            "vars": {
                "y": {"dims": ["obs"], "dtype": "float64", "role": "target"},
                "x": {"dims": ["obs"], "dtype": "float64"},
            },
        }
    }
    outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["obs"], "units_of": "y"}}
    expected: ClassVar[dict[str, str]] = {"y": "mu"}
    default_engine = "map"
    pad_extend = False

    def build(self, ds: xr.Dataset, s: _CountSettings, ctx: BuildContext) -> pm.Model:
        with pm.Model(coords=ctx.coords()) as model:
            a = pm.Normal("a", 1.5, 1.0)
            b = pm.Normal("b", 0.0, 1.0)
            mu = pm.Deterministic("mu", pm.math.exp(a + b * ctx.data("x")), dims="obs")
            pm.Poisson("y", mu, observed=ctx.observed("y", dims=["obs"]), dims="obs")
        return model


def test_discrete_counterfactual_noise_is_common_in_untouched_cells() -> None:
    """Poisson has no symbolic inverse CDF: frozen uniforms go through a numeric one, so
    changing one cell's mean never reshuffles the noise of the others."""
    reg = _registry_with(_CountsX)
    n = 12
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 1, n)
    data = xr.Dataset(
        {"y": ("obs", rng.poisson(np.exp(1.5 + 0.5 * x)).astype(float)), "x": ("obs", x)},
        coords={"obs": np.arange(n)},
    )
    spec = load_model_spec(
        RADON_SPEC_YAML.replace("examples.radon", "test.counts_x")
        .replace("keep: {log_likelihood: true, posterior_predictive: {thin: 5}},", "")
        .replace("prior_predictive: {draws: 40}", "engine: map")
        .replace("engine: pymc,", "")
        .replace("evaluate: {metrics: [elpd_loo]}", "")
    )
    schema = DataSchema.model_validate(
        {
            "dims": {"obs": {"kind": "integer", "policy": "extend"}},
            "vars": {
                "y": {"dims": ["obs"], "dtype": "float64", "role": "target"},
                "x": {"dims": ["obs"], "dtype": "float64"},
            },
        }
    )
    b = fit_spec(spec, data, registry=reg, raw_schema=schema)
    cf = score_request(
        "mode: counterfactual\ninterventions: [{var: x, sel: {obs: [0]}, set: 3.0}]\n"
        "output: {kind: predictive}\n"
    )
    tree = score_bundle(b, cf, data.drop_vars("y"), registry=reg, return_full=True)
    scen = _pred(tree)["y"]
    delta = _pred(tree, "delta/predictions")["y"]
    assert scen.attrs["noise"] == "crn_ppf"
    assert float(abs(delta.sel(obs=slice(1, None))).max()) == 0.0
    assert float(delta.sel(obs=0).min()) > 0  # a much larger mean in the changed cell
    # still integer-valued Poisson draws, reproducible for a seed
    assert bool((scen == np.round(scen)).all()) and bool((scen >= 0).all())
    again = _pred(score_bundle(b, cf, data.drop_vars("y"), registry=reg, return_full=True))["y"]
    xr.testing.assert_identical(scen, again)


def test_manifest_declares_fast_score_and_defaults() -> None:
    m = RadonModel.manifest()
    assert m.capabilities.fast_score and m.capabilities.representation.value == "draws"
    assert json.loads(m.model_dump_json())["outputs"]["y"]["units_of"] == "y"
    assert not PyMCModel.capabilities.deterministic_given_seed
    with pytest.raises(NotImplementedError):
        PyMCModel().build(xr.Dataset(), ModelSettings(), BuildContext("fit", xr.Dataset()))


class _OverlapRadon(RadonModel):
    """Radon with cheap prior draws of its floor slope for the scorecard's overlap check."""

    id = "test.overlap_radon"
    prior_overlap_vars = ("b", "not_a_parameter")
    prior_overlap_draws = 60


def test_prior_overlap_vars_keep_cheap_prior_draws(radon_data: xr.Dataset) -> None:
    reg = _registry_with(_OverlapRadon, RadonModel)
    text = (
        RADON_SPEC_YAML.replace("examples.radon", "test.overlap_radon")
        .replace("keep: {log_likelihood: true, posterior_predictive: {thin: 5}},", "")
        .replace("prior_predictive: {draws: 40}", "tune: 50")
        .replace("tune: 150, draws: 150", "draws: 50")
    )
    b = fit_spec(load_model_spec(text), radon_data, registry=reg, raw_schema=RADON_SCHEMA)
    prior = b.model.groups["prior"]
    assert set(prior.data_vars) == {"b"} and prior.sizes["draw"] == 60
    # a plugin that declares none keeps no prior unless the spec asks for it
    plain = fit_spec(
        load_model_spec(text.replace("test.overlap_radon", "examples.radon")),
        radon_data,
        registry=reg,
        raw_schema=RADON_SCHEMA,
    )
    assert "prior" not in plain.model.groups
