"""``mmm.multivariate`` without MCMC: prior draws of the engine's own graph stand in for a
posterior (as :func:`conftest.build_prior_fit` does for ``mmm.bayesian``)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_mmm.examples_multivariate import multivariate_dataset
from mapi_plugin_mmm.framework import BASELINE, SEASONALITY, TREND, quiet
from mapi_plugin_mmm.multivariate import (
    MMMMultivariate,
    MultivariateFitWindow,
    MultivariateReplay,
    MultivariateSettings,
)
from mapi_sdk.models import PREDICTIONS, FitContext, Fitted, ScoreContext
from mapi_sdk.testing import check_about
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.optimize import OptimizeRequest
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ScoreRequest

WEEKS = 60
SETTINGS: dict[str, Any] = {
    "outcomes": [{"name": "premium", "weight": 3.0}, {"name": "basic", "weight": 1.0}],
    "primary_outcome": "basic",
    "cross_effects": [{"source": "premium", "target": "basic", "kind": "halo"}],
    "trend": {"kind": "linear"},
    "seasonality": {"yearly_order": 1},
}


def stage_data(weeks: int = WEEKS, seed: int = 0) -> xr.Dataset:
    """The example data on the model stage's canonical names."""
    ds, _ = multivariate_dataset(weeks=weeks, seed=seed)
    return ds.rename({"product": "outcome", "week": "time", "sales": "y"})


@dataclass(slots=True)
class MvPriorFit:
    plugin: MMMMultivariate
    fitted: Fitted
    stage: xr.Dataset
    prior: xr.Dataset


def build_mv_prior_fit(settings: dict[str, Any], *, draws: int = 40, seed: int = 5) -> MvPriorFit:
    plugin = MMMMultivariate()
    s = plugin.parse_settings(settings)
    stage = stage_data()
    model, learned, fit_data, observed = plugin.build(stage, s)
    with quiet():
        tree = model.sample_prior_predictive(samples=draws, random_seed=seed)
    prior = tree["prior"]
    prior = prior.to_dataset(inherit=False) if isinstance(prior, xr.DataTree) else prior
    model._trace = xr.DataTree.from_dict({"posterior": prior})
    cfg = FitConfig(mode="train_only", seed=seed)
    groups = plugin.bundle_groups(model, prior, learned, fit_data, observed, cfg)
    fitted = Fitted(
        manifest=MMMMultivariate.manifest(),
        settings=s.model_dump(mode="json"),
        fit_config=cfg,
        groups=groups,
        learned={**learned, "engine": "prior"},
    )
    return MvPriorFit(plugin, fitted, stage, prior)


@pytest.fixture(scope="module")
def mv_fit() -> MvPriorFit:
    return build_mv_prior_fit(SETTINGS)


@pytest.fixture(scope="module")
def mv_piecewise_fit() -> MvPriorFit:
    return build_mv_prior_fit(
        {
            **SETTINGS,
            "trend": {"kind": "piecewise", "n_changepoints": 4},
            "share_seasonality": False,
            "share_adstock": False,
            "cross_effects": [{"source": "basic", "target": "premium"}],
        }
    )


def _req() -> ScoreRequest:
    return ScoreRequest.model_validate(
        {"apiVersion": "mapi/v1", "kind": "ScoreRequest", "model": "endpoint://p/m",
         "mode": "predict"}
    )  # fmt: skip


def _score(pf: MvPriorFit, ds: xr.Dataset, **kw: Any) -> xr.Dataset:
    schema = DataSchema.model_validate(
        pf.plugin.manifest().schema_template.render(pf.fitted.settings)
    )
    tree = pf.plugin.score(pf.fitted, ds, _req(), ScoreContext(schema=schema, **kw))
    out = tree[PREDICTIONS]
    assert isinstance(out, xr.DataTree)
    return out.to_dataset()


def _spend(pf: MvPriorFit) -> np.ndarray:
    return np.asarray(pf.stage["spend"].transpose("time", "channel").values, dtype=float)


def _observed(pf: MvPriorFit) -> np.ndarray:
    order = pf.fitted.learned["outcomes"]
    return np.asarray(pf.stage["y"].sel(outcome=order).transpose("time", "outcome").values)


def _engine_mu(pf: MvPriorFit) -> np.ndarray:
    mu = pf.prior["mu"].transpose("chain", "draw", "outcome", "obs").values
    return np.asarray(mu.reshape(-1, *mu.shape[2:]))


# ---------------------------------------------------------------------- settings


@pytest.mark.parametrize(
    ("settings", "match"),
    [
        ({"outcomes": [{"name": "a"}, {"name": "a"}]}, "once"),
        ({"outcomes": [{"name": "a", "weight": 0}, {"name": "b", "weight": 0}]}, "weight"),
        ({"outcomes": [{"name": "a"}], "primary_outcome": "b"}, "primary_outcome"),
        ({"cross_effects": [{"source": "a", "target": "a"}]}, "self-loop"),
        (
            {
                "outcomes": [{"name": "a"}, {"name": "b"}],
                "cross_effects": [{"source": "a", "target": "c"}],
            },
            "not in outcomes",
        ),
        (
            {
                "cross_effects": [
                    {"source": "a", "target": "b"},
                    {"source": "a", "target": "b", "kind": "halo"},
                ]
            },
            "twice",
        ),
    ],
)
def test_settings_validation(settings: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        MultivariateSettings.model_validate(settings)


def test_settings_must_match_the_data_outcomes() -> None:
    s = MultivariateSettings.model_validate({"outcomes": [{"name": "premium"}]})
    with pytest.raises(MapiError, match="basic") as err:
        s.resolve(["premium", "basic"])
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    s = MultivariateSettings.model_validate({"primary_outcome": "gold"})
    with pytest.raises(MapiError, match="gold"):
        s.resolve(["premium", "basic"])
    default = MultivariateSettings().resolve(["premium", "basic"])
    assert [o.name for o in default] == ["premium", "basic"]
    assert all(o.weight == 1.0 for o in default)


def test_fit_data_checks() -> None:
    plugin = MMMMultivariate()
    s = plugin.parse_settings({})
    one = stage_data().isel(outcome=[0])
    with pytest.raises(MapiError, match="two or more"):
        plugin.build(one, s)
    bad = stage_data()
    bad["y"][0, 3] = np.nan
    with pytest.raises(MapiError, match="missing"):
        plugin.build(bad, s)
    geo = stage_data().expand_dims(geo=["a"])
    with pytest.raises(MapiError, match="national"):
        plugin.build(geo, s)


# ---------------------------------------------------------------------- replay


@pytest.mark.parametrize("fixture", ["mv_fit", "mv_piecewise_fit"])
def test_observed_form_matches_the_engine_mu(fixture: str, request: pytest.FixtureRequest) -> None:
    pf: MvPriorFit = request.getfixturevalue(fixture)
    rp, _ = pf.plugin.replay(pf.fitted)
    oc = rp.evaluate_outcomes(_spend(pf), np.arange(WEEKS), observed=_observed(pf))
    np.testing.assert_allclose(oc.mu, _engine_mu(pf), rtol=1e-6, atol=1e-6)


def test_reduced_form_equals_observed_form_without_cross_effects(mv_fit: MvPriorFit) -> None:
    rp = MultivariateReplay(mv_fit.fitted.learned, mv_fit.plugin.replay(mv_fit.fitted)[0].post)
    rp.psi = np.zeros_like(rp.psi)
    pos = np.arange(WEEKS)
    reduced = rp.evaluate_outcomes(_spend(mv_fit), pos)
    observed = rp.evaluate_outcomes(_spend(mv_fit), pos, observed=_observed(mv_fit))
    np.testing.assert_allclose(reduced.mu, observed.mu, rtol=1e-10)
    np.testing.assert_allclose(reduced.channel, observed.channel, rtol=1e-10)


def test_reduced_form_solves_the_cross_effects(mv_fit: MvPriorFit) -> None:
    rp, _ = mv_fit.plugin.replay(mv_fit.fitted)
    pos = np.arange(WEEKS)
    y = rp.evaluate_outcomes(_spend(mv_fit), pos).mu  # (S, K, n)
    # Y = c + P Y: fed back as the observed outcomes, the solution reproduces itself
    for s in range(0, rp.n, 7):
        again = rp.evaluate_outcomes(_spend(mv_fit), pos, observed=y[s].T)
        np.testing.assert_allclose(again.mu[s], y[s], rtol=1e-9)


def test_a_channel_moves_the_halo_target(mv_fit: MvPriorFit) -> None:
    rp, _ = mv_fit.plugin.replay(mv_fit.fitted)
    pos = np.arange(WEEKS)
    reduced = rp.evaluate_outcomes(_spend(mv_fit), pos)
    direct = rp.evaluate_outcomes(_spend(mv_fit), pos, observed=_observed(mv_fit))
    k_p, k_b = rp.outcomes.index("premium"), rp.outcomes.index("basic")
    psi = rp.psi[:, k_p, k_b]
    assert (psi > 0).all() and np.allclose(rp.psi[:, k_b, k_p], 0.0)
    np.testing.assert_allclose(reduced.channel[:, :, k_p], direct.channel[:, :, k_p])
    np.testing.assert_allclose(
        reduced.channel[:, :, k_b],
        direct.channel[:, :, k_b] + psi[:, None, None] * direct.channel[:, :, k_p],
    )


def test_loops_without_a_solution_are_refused(mv_fit: MvPriorFit) -> None:
    rp = MultivariateReplay(mv_fit.fitted.learned, mv_fit.plugin.replay(mv_fit.fitted)[0].post)
    psi = np.zeros_like(rp.psi)
    psi[:, 0, 1] = psi[:, 1, 0] = 1.0  # I - P is singular
    rp.psi = psi
    with pytest.raises(MapiError, match="loop") as err:
        rp.evaluate_outcomes(_spend(mv_fit), np.arange(4))
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    # the observed form (engine parity) needs no solve
    rp.evaluate_outcomes(_spend(mv_fit), np.arange(4), observed=_observed(mv_fit)[:4])


def test_noise_follows_the_residual_correlation(mv_fit: MvPriorFit) -> None:
    rp, _ = mv_fit.plugin.replay(mv_fit.fitted)
    long = np.tile(_spend(mv_fit), (40, 1))
    pos = np.arange(long.shape[0])
    oc = rp.evaluate_outcomes(long, pos, observed=np.zeros((pos.size, 2)))
    eps = oc.noise(seed=3)
    corr = mv_fit.prior["Y_obs_correlation"].values.reshape(rp.n, 2, 2)[:, 0, 1]
    strong = np.abs(corr) > 0.3
    assert strong.sum() >= 3
    got = np.array([np.corrcoef(eps[s, 0], eps[s, 1])[0, 1] for s in range(rp.n)])
    assert (np.sign(got[strong]) == np.sign(corr[strong])).all()
    np.testing.assert_allclose(got[strong], corr[strong], atol=0.15)
    np.testing.assert_array_equal(oc.noise(seed=3), eps)
    assert not np.allclose(oc.noise(seed=4), eps)


# ---------------------------------------------------------------------- score


def test_scores_add_up_per_outcome_and_carry_the_primary_outcome(mv_fit: MvPriorFit) -> None:
    pred = _score(mv_fit, mv_fit.stage.drop_vars("y"))
    assert set(pred.y_hat.dims) == {"chain", "draw", "outcome", "time"}
    assert list(pred.outcome.values) == ["premium", "basic"]
    total = pred.contributions.sum("component")
    xr.testing.assert_allclose(total, pred.y_hat.transpose(*total.dims))
    comps = list(map(str, pred.component.values))
    assert comps == ["tv", "search", "social", BASELINE, TREND, SEASONALITY]
    for v in ("y_hat", "contributions"):
        assert pred[v].attrs["primary_outcome"] == "basic"


def test_forecasts_extend_the_window_and_predictive_noise_is_seeded(mv_fit: MvPriorFit) -> None:
    stage = mv_fit.stage
    future_t = pd.date_range(stage.time.values[-1], periods=5, freq="W-SUN")[1:].values
    future = stage.isel(time=slice(-4, None)).assign_coords(time=future_t).drop_vars("y")
    pred = _score(mv_fit, future)
    assert pred.y_hat.sizes["time"] == 4 and np.isfinite(pred.y_hat.values).all()
    a = _score(mv_fit, future, kind="predictive", seed=1)
    b = _score(mv_fit, future, kind="predictive", seed=1)
    xr.testing.assert_identical(a.y_hat, b.y_hat)
    assert not np.allclose(a.y_hat.values, pred.y_hat.values)


# ---------------------------------------------------------------------- derived


def _draws(n: int) -> np.ndarray:
    return np.array([(0, i) for i in range(n)])


def _ctx(pf: MvPriorFit, n: int = 10) -> ScoreContext:
    return ScoreContext(seed=0, draw_index=_draws(n))


def test_derived_contributions_by_outcome(mv_fit: MvPriorFit) -> None:
    ds = mv_fit.plugin.derive(mv_fit.fitted, "contributions", xr.Dataset(), _ctx(mv_fit))
    assert ds.channel_contribution.dims == ("sample", "channel", "outcome", "time")
    for v in (BASELINE, TREND, SEASONALITY):
        assert ds[v].dims == ("sample", "outcome", "time")
    assert ds.attrs["primary_outcome"] == "basic"
    total = ds.channel_contribution.sum("channel") + ds.baseline + ds.trend + ds.seasonality
    rp, _ = mv_fit.plugin.replay(mv_fit.fitted)
    mu = rp.evaluate_outcomes(_spend(mv_fit), np.arange(WEEKS)).mu[:10]
    np.testing.assert_allclose(total.transpose("sample", "outcome", "time").values, mu)


def test_weighted_curves_are_the_weighted_sum_of_the_outcome_curves(mv_fit: MvPriorFit) -> None:
    ds = mv_fit.plugin.derive(mv_fit.fitted, "response_curves", xr.Dataset(), _ctx(mv_fit))
    assert ds.contribution.dims == ("sample", "channel", "multiplier")
    assert ds.contribution_by_outcome.dims == ("sample", "channel", "multiplier", "outcome")
    assert ds.baseline_by_outcome.dims == ("sample", "outcome")
    w = xr.DataArray([3.0, 1.0], dims=["outcome"], coords={"outcome": ["premium", "basic"]})
    xr.testing.assert_allclose(
        ds.contribution, (ds.contribution_by_outcome * w).sum("outcome"), check_dim_order=False
    )
    np.testing.assert_allclose(ds.baseline.values, (ds.baseline_by_outcome * w).sum("outcome"))
    assert ds.attrs["kpi"] == "weighted total of the outcomes"
    assert json.loads(ds.attrs["outcome_weights"]) == {"premium": 3.0, "basic": 1.0}
    for v in ("spend", "current_spend", "max_period_spend"):
        assert v in ds
    zero = ds.contribution.sel(multiplier=0.0)
    np.testing.assert_allclose(zero.values, 0.0, atol=1e-9)


def test_one_weighted_outcome_names_the_kpi() -> None:
    pf = build_mv_prior_fit(
        {**SETTINGS, "outcomes": [{"name": "premium", "weight": 0}, {"name": "basic"}]},
        draws=8,
    )
    ds = pf.plugin.derive(pf.fitted, "response_curves", xr.Dataset(), _ctx(pf, 4))
    assert ds.attrs["kpi"] == "basic"
    xr.testing.assert_allclose(
        ds.contribution, ds.contribution_by_outcome.sel(outcome="basic", drop=True),
        check_dim_order=False,
    )  # fmt: skip


def test_marginal_cost_adstock_and_cross_effects(mv_fit: MvPriorFit) -> None:
    mc = mv_fit.plugin.derive(mv_fit.fitted, "marginal_cost", xr.Dataset(), _ctx(mv_fit))
    assert mc.marginal_roi_total.dims == ("sample", "channel")
    ad = mv_fit.plugin.derive(mv_fit.fitted, "adstock", xr.Dataset(), _ctx(mv_fit))
    assert ad.weight.dims == ("sample", "channel", "lag") and ad.sizes["lag"] == 8
    np.testing.assert_allclose(ad.weight.sum("lag"), 1.0)
    ce = mv_fit.plugin.derive(mv_fit.fitted, "cross_effects", xr.Dataset(), _ctx(mv_fit))
    assert ce.psi.dims == ("sample", "pair")
    assert list(ce.pair.values) == ["premium→basic"]
    assert list(ce.source.values) == ["premium"] and list(ce.target.values) == ["basic"]
    assert list(ce.kind.values) == ["halo"]
    assert (ce.psi.values > 0).all()
    assert ce.attrs["units"] == "target units per unit of source"
    with pytest.raises(MapiError) as err:
        mv_fit.plugin.derive(mv_fit.fitted, "mediation", xr.Dataset(), _ctx(mv_fit))
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_unit_map_probes_the_outcome_dim(mv_fit: MvPriorFit) -> None:
    seen: list[tuple[str, ...]] = []

    def to_original(da: xr.DataArray, src: str) -> xr.DataArray:
        seen.append(tuple(map(str, da.dims)))
        out = da * (2.0 if src == "y" else 1.0)
        out.attrs = {"units": "EUR", "output_space": "original"}
        return out

    ctx = ScoreContext(seed=0, draw_index=_draws(5), to_original=to_original)
    a = mv_fit.plugin.derive(mv_fit.fitted, "contributions", xr.Dataset(), ctx)
    b = mv_fit.plugin.derive(mv_fit.fitted, "contributions", xr.Dataset(), _ctx(mv_fit, 5))
    np.testing.assert_allclose(a.baseline.values, 2.0 * b.baseline.values)
    assert ("outcome", "time") in seen
    assert a.baseline.attrs["output_space"] == "original"


def test_window_is_a_fit_window(mv_fit: MvPriorFit) -> None:
    win = mv_fit.plugin._window(mv_fit.fitted, _draws(5))
    assert isinstance(win, MultivariateFitWindow)
    np.testing.assert_allclose(
        win.expected_cells()[:, 0],
        (win.mv.evaluate_outcomes(_spend(mv_fit), np.arange(WEEKS)).mu.sum(axis=2)
         * win.mv.weights).sum(axis=1),
    )  # fmt: skip


# ---------------------------------------------------------------------- decisions


def test_optimize_and_objective_use_the_weighted_total(mv_fit: MvPriorFit) -> None:
    req = OptimizeRequest.model_validate({"max_draws": 20, "n_steps": 100})
    plan = mv_fit.plugin.optimize(mv_fit.fitted, req, ScoreContext(seed=3))
    res = plan.result
    assert {a.channel for a in res.allocation} == {"tv", "search", "social"}
    assert sum(a.optimal_spend for a in res.allocation) == pytest.approx(
        res.current_total, rel=0.005
    )
    assert res.kpi == "weighted total of the outcomes"
    assert res.notes_plain[0] == (
        "The KPI is the weighted total of the outcomes: premium × 3, basic × 1."
    )
    decision = xr.Dataset(
        {"spend": ("channel", [100.0, 50.0])}, coords={"channel": ["tv", "search"]}
    )
    decision.attrs["periods"] = 4
    obj = mv_fit.plugin.objective(mv_fit.fitted, decision, ScoreContext())
    assert obj.dims == ("chain", "draw") and np.isfinite(obj.values).all()
    rp, _ = mv_fit.plugin.replay(mv_fit.fitted)
    by = rp.steady_state(np.array([[100.0, 50.0, 0.0]]), rp.fit_cells())
    np.testing.assert_allclose(obj.values.reshape(-1), by.sum(axis=(1, 2)) * 4)
    with pytest.raises(MapiError):
        mv_fit.plugin.objective(
            mv_fit.fitted,
            xr.Dataset({"spend": (("geo", "channel"), [[1.0]])},
                       coords={"geo": ["a"], "channel": ["tv"]}),
            ScoreContext(),
        )  # fmt: skip


@pytest.mark.parametrize(("weight", "note"), [(1, "basic"), (2, "basic × 2")])
def test_a_plan_for_one_weighted_outcome_names_it(weight: float, note: str) -> None:
    """With one non-zero weight the plan used to keep ``kpi='y'``: the runner then labelled it
    with the whole target's name and units, and no note said which outcome (or weight)."""
    pf = build_mv_prior_fit(
        {**SETTINGS, "outcomes": [{"name": "premium", "weight": 0},
                                  {"name": "basic", "weight": weight}]},
        draws=8,
    )  # fmt: skip
    req = OptimizeRequest.model_validate({"max_draws": 20, "n_steps": 50})
    res = pf.plugin.optimize(pf.fitted, req, ScoreContext(seed=3)).result
    assert res.kpi == "basic"
    assert res.notes_plain[0] == f"The KPI is the outcome {note}. The other outcomes have weight 0."
    curves = pf.plugin.derive(pf.fitted, "response_curves", xr.Dataset(), _ctx(pf, 4))
    assert curves.attrs["kpi"] == res.kpi


# ---------------------------------------------------------------------- manifest


def test_manifest_and_about() -> None:
    m = MMMMultivariate.manifest()
    assert (m.id, m.version, m.family) == ("mmm.multivariate", "1.0.0", "mmm")
    assert m.capabilities.calibration == frozenset()
    assert "mmm.cross_effects" in m.plots
    assert {d.name for d in m.derived} == {
        "contributions", "response_curves", "marginal_cost", "adstock", "cross_effects"
    }  # fmt: skip
    check_about(MMMMultivariate)


def test_bundle_keeps_the_trend_by_period_and_a_prior(mv_piecewise_fit: MvPriorFit) -> None:
    post = mv_piecewise_fit.fitted.posterior
    assert post["trend_by_period"].dims == ("chain", "draw", "outcome", "trend_period")
    assert "mu" not in post and not any(str(v).startswith("mapi_trend_") for v in post)
    assert "prior" in mv_piecewise_fit.fitted.groups
    assert mv_piecewise_fit.fitted.group("observed_data")["y"].dims == ("outcome", "time")


def test_tracking_logs_the_weighted_summary_and_roi_per_outcome(mv_fit: MvPriorFit) -> None:
    rec = MetricRecorder()
    fitted = mv_fit.fitted
    mv_fit.plugin.log_summary(
        FitContext(tracker=rec), fitted.posterior, fitted.learned, fitted.group("fit_data")
    )
    assert rec.params["mmm.outcomes"] == "premium,basic"
    assert rec.params["mmm.cross_effects"] == 1
    assert rec.params["mmm.n_cells"] == 1 and rec.params["mmm.link"] == "identity"
    keys = set(rec.series())
    assert ("roi", "channel=tv") in keys
    assert ("roi", "channel=tv,outcome=basic") in keys
    assert ("roi", "channel=social,outcome=premium") in keys
    assert ("baseline_share", "") in keys
    # without a tracker nothing is computed
    mv_fit.plugin.log_summary(
        FitContext(), fitted.posterior, fitted.learned, fitted.group("fit_data")
    )
