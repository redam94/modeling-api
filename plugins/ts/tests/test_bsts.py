"""``ts.bsts`` without MCMC: settings, the post-sampling path on prior draws, groups and
score modes (plan v0.13 §2.2)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_ts import BSTSModel, BSTSSettings
from mapi_plugin_ts.bsts import (
    COMPONENTS,
    MAX_STATES,
    ONE_STEP,
    SCORE_DRAWS,
    TERMINAL_STATE,
    BSTSFit,
    Scaling,
    SeasonSettings,
    StructureSettings,
    build_model,
    build_statespace,
    components_group,
    fit_metrics,
    forecast,
    one_step_group,
    postprocess_draws,
    read_terminal,
    standardise,
    system_from_posterior,
    terminal_group,
    track_bsts,
)
from mapi_plugin_ts.bsts_data import BSTS_TRUTH, bsts_dataset
from mapi_plugin_ts.ss_math import kalman_filter
from mapi_sdk.models import FitContext, Fitted, ScoreContext
from mapi_sdk.tracking import MetricRecorder
from mapi_spec import ErrorCode, MapiError, load_score_request
from mapi_spec.spec import FitConfig
from pydantic import ValidationError

REGS = ["price"]
N_FIT = 36


def prior_draws(model: Any, draws: int, seed: int) -> xr.Dataset:
    """Prior draws of the free variables, shaped like a posterior (chain, draw)."""
    import pymc as pm

    free = [rv.name for rv in model.free_RVs]
    with model:
        tree = pm.sample_prior_predictive(draws=draws, var_names=free, random_seed=seed)
    prior = tree["prior"]
    return prior.to_dataset(inherit=False) if isinstance(prior, xr.DataTree) else prior


def obs_logp(model: Any, draws: xr.Dataset) -> np.ndarray:
    """``logp(obs)`` of the model at every (chain, draw) of ``draws`` (as test_ss_math)."""
    fn = model.compile_logp(vars=[model["obs"]])
    out = []
    for c in range(draws.sizes["chain"]):
        for d in range(draws.sizes["draw"]):
            point = {}
            for rv in model.free_RVs:
                value = draws[rv.name].isel(chain=c, draw=d).values
                tr = model.rvs_to_transforms[rv]
                if tr is not None:
                    value = tr.forward(value, *rv.owner.inputs).eval()
                point[model.rvs_to_values[rv].name] = value
            out.append(float(fn(point)))
    return np.asarray(out)


# ================================================================================== settings


def test_settings_defaults_and_state_count() -> None:
    s = BSTSSettings()
    assert s.trend == "local_linear" and s.seasonality == [] and s.regressors == []
    assert SeasonSettings(period=12).n_states == 11
    assert SeasonSettings(period=52.18, harmonics=3).n_states == 6
    assert SeasonSettings(period=52.18, harmonics=3).label == "52.18x3"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"seasonality": [{"period": 24}, {"period": 24}]}, f"more than {MAX_STATES}"),
        ({"seasonality": [{"period": 12, "harmonics": 7}]}, "at most period/2"),
        ({"seasonality": [{"period": 25}]}, "whole-number period"),
        ({"seasonality": [{"period": 7.5}]}, "whole-number period"),
        ({"regressors": ["price", "price"]}, "must not repeat"),
        ({"regressors": ["y"]}, "cannot use the names"),
        ({"regressors": ["not a name"]}, "cannot use the names"),
        ({"seasonality": [{"period": 4}] * 4}, "at most 3"),
    ],
)
def test_settings_validation(raw: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        BSTSSettings.model_validate(raw)


def test_model_rejects_bad_settings_as_settings_invalid() -> None:
    with pytest.raises(MapiError) as err:
        BSTSModel().parse_settings({"seasonality": [{"period": 30}]})
    assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_standardise_checks_the_data() -> None:
    y = np.array([1.0, 2.0, np.nan, 4.0])
    X = np.array([[1.0], [2.0], [3.0], [5.0]])
    y_std, X_std, sc = standardise(y, X, REGS)
    assert X_std is not None
    np.testing.assert_allclose(np.nanmean(y_std), 0.0, atol=1e-12)
    np.testing.assert_allclose(X_std.std(axis=0), 1.0)
    assert Scaling.from_json(sc.to_json()) == sc
    for bad_y, bad_X in [
        (np.array([1.0, np.nan, 2.0]), None),  # fewer than 3 observed
        (np.ones(5), None),  # constant
        (np.arange(4.0), np.ones((4, 1))),  # constant driver
    ]:
        with pytest.raises(MapiError) as err:
            standardise(bad_y, bad_X, REGS)
        assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_statespace_parameters_come_from_pymc_extras() -> None:
    s = StructureSettings.model_validate(
        {"trend": "smooth", "seasonality": [{"period": 7}, {"period": 52.18, "harmonics": 2}]}
    )
    ss = build_statespace(s, REGS)
    assert "x_price" in ss.state_names
    model, ss = build_model(s, np.linspace(-1, 1, 20), np.linspace(1, -1, 20)[:, None], REGS)
    named = set(model.named_vars)
    assert {p for p in ss.param_names if p != "P0"} <= named
    p0 = model["P0"].eval()
    reg = ss.state_names.index("x_price")
    assert p0[reg, reg] == 0.0 and p0[0, 0] == 1.0


# ======================================================================== prior-draw fit


@dataclass
class PriorFit:
    ds: xr.Dataset
    settings: BSTSSettings
    fit: BSTSFit
    model: Any
    ss: Any
    draws: xr.Dataset
    y_std: np.ndarray


def _canonical(n: int = N_FIT + 12) -> xr.Dataset:
    raw = bsts_dataset(periods=n, seed=2)
    return raw.rename({"date": "t", "sales": "y"})


@pytest.fixture(scope="module")
def prior_fit() -> PriorFit:
    ds = _canonical().isel(t=slice(0, N_FIT))
    s = BSTSSettings.model_validate(
        {
            "trend": "local_linear",
            "seasonality": [{"period": 12, "harmonics": 2}],
            "regressors": REGS,
        }
    )
    y = ds["y"].values
    X = ds["price"].values[:, None]
    y_std, X_std, scaling = standardise(y, X, REGS)
    model, ss = build_model(s, y_std, X_std, REGS)
    draws = prior_draws(model, draws=6, seed=4)
    fit = postprocess_draws(model, ss, draws, REGS, y_std=y_std, X_std=X_std, scaling=scaling)
    return PriorFit(ds, s, fit, model, ss, draws, y_std)


def test_system_keeps_structural_states_and_returns_beta(prior_fit: PriorFit) -> None:
    fit = prior_fit.fit
    assert fit.state_names == [n for n in prior_fit.ss.state_names if n != "x_price"]
    assert fit.system.k == 2 + 4 and fit.system.S == 6
    np.testing.assert_allclose(
        fit.beta[:, 0], prior_fit.draws["beta_reg"].values.reshape(-1), rtol=1e-12
    )
    assert fit.posterior["beta_reg"].coords["state_reg"].values.tolist() == ["price"]
    assert "P0" not in fit.posterior and "ss_T" not in fit.posterior
    assert fit.learned["n_states"] == 6 and fit.learned["missing_periods"] == 3


def test_regression_term_matches_the_pymc_likelihood(prior_fit: PriorFit) -> None:
    """beta enters as d = X beta: the zero P0 block keeps it a static coefficient. The
    regression states of pymc-extras get the filter's 1e-8 jitter each step, hence rtol."""
    fit = prior_fit.fit
    system, beta = system_from_posterior(prior_fit.model, prior_fit.ss, prior_fit.draws, REGS)
    assert fit.X_std is not None
    fr = kalman_filter(system, prior_fit.y_std, beta @ fit.X_std.T)
    np.testing.assert_allclose(fr.loglik, obs_logp(prior_fit.model, prior_fit.draws), rtol=1e-4)
    np.testing.assert_allclose(fit.filt.loglik, fr.loglik, rtol=1e-12)


def test_horseshoe_prior_feeds_the_regression_states() -> None:
    s = BSTSSettings.model_validate(
        {"trend": "level", "regressors": ["a", "b"], "regression_prior": "horseshoe"}
    )
    rng = np.random.default_rng(0)
    y_std, X_std, scaling = standardise(rng.normal(size=20), rng.normal(size=(20, 2)), ["a", "b"])
    model, ss = build_model(s, y_std, X_std, ["a", "b"])
    assert "beta_reg" in {d.name for d in model.deterministics}
    draws = prior_draws(model, draws=3, seed=1)
    fit = postprocess_draws(model, ss, draws, ["a", "b"], y_std=y_std, X_std=X_std, scaling=scaling)
    with model:
        import pymc as pm

        beta = pm.compute_deterministics(draws, var_names=["beta_reg"], progressbar=False)
    np.testing.assert_allclose(fit.beta, beta["beta_reg"].values.reshape(3, 2), rtol=1e-10)
    assert fit.posterior["beta_reg"].coords["state_reg"].values.tolist() == ["a", "b"]


def test_score_grid_is_a_thinned_rectangle() -> None:
    from mapi_plugin_ts.bsts import _score_grid

    post = xr.Dataset(coords={"chain": np.arange(4), "draw": np.arange(1000)})
    chain, draw = _score_grid(post)
    assert chain.size * draw.size <= SCORE_DRAWS and chain.size == 4
    assert draw.size == 62 and np.unique(np.diff(draw)).size == 1
    small = xr.Dataset(coords={"chain": np.arange(2), "draw": np.arange(30)})
    assert _score_grid(small)[1].size == 30


def test_components_sum_to_the_smoothed_mean(prior_fit: PriorFit) -> None:
    fit = prior_fit.fit
    times = prior_fit.ds["t"].values
    comp = components_group(fit, times, "t")
    assert set(comp.data_vars) == {"level", "season_0", "regression"}
    assert comp["level"].dims == ("sample", "t")
    total = sum(comp[v] for v in comp.data_vars)
    sc, d = fit.scaling, fit.d
    assert d is not None
    want = (np.einsum("snk,sk->sn", fit.smoothed, fit.system.Z) + d) * sc.y_scale + sc.y_loc
    np.testing.assert_allclose(np.asarray(total), want, rtol=1e-10)
    sub = components_group(fit, times, "t", sample_index=np.array([0, 3]))
    np.testing.assert_allclose(sub["level"].values, comp["level"].values[[0, 3]])


def test_components_render_as_a_decomposition(prior_fit: PriorFit) -> None:
    recipes = pytest.importorskip("mapi_plots.recipes")
    comp = components_group(prior_fit.fit, prior_fit.ds["t"].values, "t")
    plot = recipes.get_recipe("core.decomposition").render({COMPONENTS: comp})
    assert plot is not None


def test_terminal_state_round_trip(prior_fit: PriorFit) -> None:
    fit = prior_fit.fit
    last = prior_fit.ds["t"].values[-1]
    group = terminal_group(fit, last)
    assert set(group.dims) == {"chain", "draw", "state", "state_aux", "regressor"}
    assert group.attrs["last_label"] == pd.Timestamp(last).date().isoformat()
    assert group.attrs["n_fit"] == N_FIT
    term = read_terminal(group)
    np.testing.assert_allclose(term.mean, fit.filt.last_mean)
    np.testing.assert_allclose(term.cov, fit.filt.last_cov)
    np.testing.assert_allclose(term.system.T, fit.system.T)
    np.testing.assert_allclose(term.beta, fit.beta)
    assert term.scaling == fit.scaling and term.n_fit == N_FIT
    with pytest.raises(MapiError) as err:
        read_terminal(group.drop_vars("cov"))
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE


def test_forecast_is_in_target_units_and_needs_the_drivers(prior_fit: PriorFit) -> None:
    term = read_terminal(terminal_group(prior_fit.fit, prior_fit.ds["t"].values[-1]))
    X = np.full((4, 1), 10.0)
    exp, pred = forecast(term, X, 4, seed=1, name="f")
    assert exp.shape == pred.shape == (6, 4)
    exp2, _ = forecast(term, X + 1.0, 4, seed=1, name="f")
    effect = term.beta[:, 0] * term.scaling.y_scale / term.scaling.x_scale[0]
    np.testing.assert_allclose(exp2 - exp, np.broadcast_to(effect[:, None], exp.shape))
    with pytest.raises(MapiError) as err:
        forecast(term, None, 4, seed=1, name="f")
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE


def test_fit_metrics_and_tracking_keys(prior_fit: PriorFit) -> None:
    fit = prior_fit.fit
    y = prior_fit.ds["y"].values
    metrics = fit_metrics(fit, y)
    assert {"sigma_obs", "sigma_level", "one_step_rmse", "one_step_coverage_90"} <= set(metrics)
    assert 0.0 <= metrics["one_step_coverage_90"] <= 1.0
    rec = MetricRecorder()
    track_bsts(FitContext(tracker=rec), fit, prior_fit.settings, y, prior_fit.ds["t"].values)
    assert rec.params == {
        "bsts.trend": "local_linear",
        "bsts.seasonality": "12x2",
        "bsts.regressors": "price",
        "bsts.n_states": 6,
        "bsts.n_periods": N_FIT,
        "bsts.missing_periods": 3,
    }
    assert {k for k, _ in rec.series()} == set(metrics)
    track_bsts(FitContext(), fit, prior_fit.settings, y, prior_fit.ds["t"].values)  # no-op


# ========================================================================= score on prior draws

HEADER = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://ts/bsts\n"


def _fitted(pf: PriorFit) -> Fitted:
    times = pf.ds["t"].values
    terminal = terminal_group(pf.fit, times[-1], extra={"freq": "MS"})
    return Fitted(
        manifest=BSTSModel.manifest(),
        settings=pf.settings.model_dump(mode="json"),
        fit_config=FitConfig(),
        groups={
            "posterior": pf.fit.posterior,
            ONE_STEP: one_step_group(pf.fit, times, "t"),
        },
        learned={**pf.fit.learned, "freq": "MS", "last_label": terminal.attrs["last_label"]},
        terminal_state=terminal,
    )


def _score(
    pf: PriorFit,
    ds: xr.Dataset,
    mode: str = "predict",
    kind: str = "expected",
    fitted: Fitted | None = None,
) -> xr.DataTree:
    req = load_score_request(
        HEADER
        + f"mode: {mode}\n"
        + {"filter": "data: dataset://x/y@v1\n", "forecast": "horizon: {dim: t, periods: 6}\n"}.get(
            mode, ""
        )
    )
    ctx = ScoreContext(mode=mode, kind=kind, seed=3)  # type: ignore[arg-type]
    return BSTSModel().score(fitted or _fitted(pf), ds, req, ctx)


def test_predict_inside_the_fit_is_one_step_ahead(prior_fit: PriorFit) -> None:
    pf = prior_fit
    payload = pf.ds.isel(t=slice(-5, None))
    pred = _score(pf, payload)["predictions"].to_dataset()
    assert pred["y_hat"].dims == ("chain", "draw", "t") and pred.sizes["t"] == 5
    d = pf.fit.d
    assert d is not None
    want = (pf.fit.filt.mean + d)[:, -5:] * pf.fit.scaling.y_scale + pf.fit.scaling.y_loc
    np.testing.assert_allclose(pred["y_hat"].values.reshape(6, 5), want)
    # predict never conditions on payload targets
    other = _score(pf, payload.drop_vars("y"))["predictions"].to_dataset()
    xr.testing.assert_identical(pred, other)
    shuffled = _score(pf, payload.isel(t=[3, 0, 4, 1, 2]), kind="predictive")
    base = _score(pf, payload, kind="predictive")["predictions"].to_dataset()
    xr.testing.assert_allclose(shuffled["predictions"].to_dataset().sortby("t"), base, rtol=1e-12)


def test_predict_after_the_fit_forecasts_with_the_drivers(prior_fit: PriorFit) -> None:
    pf = prior_fit
    future = _canonical().isel(t=slice(N_FIT, N_FIT + 6)).drop_vars("y")
    pred = _score(pf, future, mode="forecast")["predictions"].to_dataset()
    term = read_terminal(_fitted(pf).terminal_state)  # type: ignore[arg-type]
    exp, _ = forecast(term, future["price"].values[:, None], 6, seed=3, name="y_hat")
    np.testing.assert_allclose(pred["y_hat"].values.reshape(6, 6), exp)
    assert np.isfinite(pred["level"].values).all()
    with pytest.raises(MapiError) as err:
        _score(pf, future.drop_vars("price"), mode="forecast")
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE
    with pytest.raises(MapiError) as err2:
        _score(pf, future.assign_coords(t=future["t"].values + np.timedelta64(3, "D")))
    assert err2.value.code is ErrorCode.COORD_NOT_ALLOWED


def test_filter_returns_an_updated_terminal_state(prior_fit: PriorFit) -> None:
    pf = prior_fit
    fitted = _fitted(pf)
    new = _canonical().isel(t=slice(N_FIT, N_FIT + 4))
    out = _score(pf, new, mode="filter")
    term = out[TERMINAL_STATE].to_dataset()
    assert term.attrs["n_fit"] == N_FIT + 4
    assert term.attrs["last_label"] == pd.Timestamp(new["t"].values[-1]).date().isoformat()
    assert term.attrs["previous_label"] == fitted.terminal_state.attrs["last_label"]  # type: ignore[union-attr]
    # filtering in two batches equals one batch
    first = _score(pf, new.isel(t=slice(0, 2)), mode="filter")[TERMINAL_STATE].to_dataset()
    second = _score(
        pf, new.isel(t=slice(2, None)), mode="filter", fitted=replace(fitted, terminal_state=first)
    )[TERMINAL_STATE].to_dataset()
    np.testing.assert_allclose(second["mean"].values, term["mean"].values, atol=1e-10)
    with pytest.raises(MapiError) as err:
        _score(pf, pf.ds.isel(t=slice(-3, None)), mode="filter")
    assert err.value.code is ErrorCode.COORD_NOT_ALLOWED
    with pytest.raises(MapiError) as err2:
        _score(pf, new.drop_vars("y"), mode="filter")
    assert err2.value.code is ErrorCode.SCHEMA_INVALID


# ================================================================================== manifest


def test_manifest_and_about_card() -> None:
    from mapi_sdk.testing import check_about

    m = BSTSModel.manifest()
    caps = m.capabilities
    assert m.id == "ts.bsts" and m.version == "1.0.0" and m.model_family == "ts"
    assert caps.score_modes == {"predict", "forecast", "filter", "evaluate"}
    assert not caps.loo_valid and "Kalman" in (caps.loo_invalid_reason or "")
    assert caps.identified_quantities == ["sigma_obs"] and not caps.deterministic_given_seed
    assert set(m.outputs) == {"y_hat", "level"}
    assert list(m.plots) == [
        "core.ppc_fan",
        "core.decomposition",
        "core.convergence",
        "core.posterior_forest",
    ]
    assert check_about(BSTSModel).ok


def test_truth_is_documented() -> None:
    assert BSTS_TRUTH["price"] < 0
    ds = bsts_dataset(periods=24, missing=2)
    assert int(ds["sales"].isnull().sum()) == 2 and "mapi_schema" in ds.attrs
