"""``mmm.nested`` on prior draws (no MCMC): the NumPy replay against the engine's own
deterministics, scoring, derived quantities, the objective and the data checks.

The fit runs end to end except for the sampler: ``fit_extended`` is replaced by a draw
from mmm-framework's prior of the same ``NestedMMM`` graph, so the bundle groups, the
``learned`` record and the replay are exactly a real fit's.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_mmm import nested as nested_mod
from mapi_plugin_mmm.examples_nested import nested_dataset
from mapi_plugin_mmm.framework import BASELINE, SEASONALITY, TREND, quiet
from mapi_plugin_mmm.nested import MEDIATORS, MMMNested, NestedReplay, mediation_dataset
from mapi_sdk.models import PREDICTIONS, FitContext, Fitted, ScoreContext
from mapi_sdk.testing import check_about
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.optimize import OptimizeRequest
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ScoreRequest

DRAWS = 30


def nested_stage(weeks: int = 60, seed: int = 0) -> xr.Dataset:
    """Model-stage data: ``y(time)``, ``spend(channel, time)``, ``mediators(mediator, time)``."""
    ds, _ = nested_dataset(weeks=weeks, seed=seed)
    return ds.rename({"sales": "y", "week": "time"})


@dataclass(slots=True)
class NestedPriorFit:
    plugin: MMMNested
    fitted: Fitted
    stage: xr.Dataset
    prior: xr.Dataset  # the engine's prior draws, deterministics included


@contextmanager
def _prior_sampler(holder: dict[str, Any], draws: int, seed: int) -> Iterator[None]:
    def fake(model: Any, plan: Any, ctx: Any) -> Any:
        with quiet():
            tree = model.sample_prior_predictive(samples=draws, random_seed=seed)
        prior = tree["prior"].to_dataset(inherit=False)
        model._trace = xr.DataTree.from_dict({"posterior": prior})
        holder["prior"] = prior
        return SimpleNamespace(diagnostics={"fit_method": "prior", "approximate": False})

    with mock.patch.object(nested_mod, "fit_extended", fake):
        yield


def build_nested_prior_fit(
    stage: xr.Dataset,
    settings: dict[str, Any],
    *,
    draws: int = DRAWS,
    seed: int = 5,
    ctx: FitContext | None = None,
) -> NestedPriorFit:
    plugin = MMMNested()
    s = plugin.parse_settings(settings)
    cfg = FitConfig(mode="train_only", seed=seed, engine="map")
    holder: dict[str, Any] = {}
    with _prior_sampler(holder, draws, seed):
        out = plugin.fit(stage, s, cfg, ctx or FitContext(seed=seed))
    fitted = Fitted(
        manifest=MMMNested.manifest(),
        settings=s.model_dump(mode="json"),
        fit_config=cfg,
        groups=out.groups,
        learned=out.learned,
    )
    return NestedPriorFit(plugin, fitted, stage, holder["prior"])


SETTINGS: dict[str, Any] = {
    "mediators": [
        {"name": "visits"},
        {"name": "awareness", "channels": ["tv"], "outcome_sign": "negative"},
    ],
    "trend": {"kind": "linear"},
    "seasonality": {"yearly_order": 2},
}


@pytest.fixture(scope="module")
def stage() -> xr.Dataset:
    return nested_stage()


@pytest.fixture(scope="module")
def pf(stage: xr.Dataset) -> NestedPriorFit:
    return build_nested_prior_fit(stage, SETTINGS)


@pytest.fixture(scope="module")
def pf_latent(stage: xr.Dataset) -> NestedPriorFit:
    return build_nested_prior_fit(
        stage,
        {
            "mediators": [
                {"name": "visits", "channels": ["tv", "search"], "direct_effect": False},
                {
                    "name": "awareness",
                    "latent": True,
                    "media_sign": "none",
                    "channels": ["social"],
                    "direct_effect": False,
                },
            ],
            "trend": {"kind": "piecewise", "n_changepoints": 3},
            "seasonality": {"yearly_order": 1},
            "likelihood": "student_t",
        },
    )


def _flat(prior: xr.Dataset, name: str) -> np.ndarray:
    da = prior[name]
    arr = np.asarray(da.transpose("chain", "draw", ...).values, dtype=float)
    return arr.reshape(-1, *arr.shape[2:])


def _window(p: NestedPriorFit) -> tuple[NestedReplay, Any, np.ndarray, np.ndarray]:
    rp, fit = p.plugin.replay(p.fitted)
    spend = fit.spend.reshape(fit.n_periods, 1, -1)
    return rp, fit, spend, np.arange(fit.n_periods)


# ---------------------------------------------------------------------------- replay


@pytest.mark.parametrize("which", ["pf", "pf_latent"])
def test_replay_reproduces_the_engine_deterministics(which: str, request: Any) -> None:
    p: NestedPriorFit = request.getfixturevalue(which)
    rp, fit, spend, pos = _window(p)
    comps = rp.evaluate(spend, np.zeros((fit.n_periods, 1, 0)), pos, rp.fit_cells())
    np.testing.assert_allclose(comps.mu[:, 0], _flat(p.prior, "mu"), rtol=1e-9, atol=1e-6)
    ch = _flat(p.prior, "channel_contributions")  # (S, obs, channel)
    np.testing.assert_allclose(comps.channel[:, :, 0], ch.transpose(0, 2, 1), atol=1e-6)
    if "trend_component" in p.prior:
        np.testing.assert_allclose(comps.trend, _flat(p.prior, "trend_component"), atol=1e-6)
    np.testing.assert_allclose(
        comps.seasonality, _flat(p.prior, "seasonality_component"), atol=1e-6
    )
    med = rp.mediator_at(spend[:, 0], pos)
    scaling = p.fitted.learned["scaling"]
    for k, m in enumerate(rp.mediators):
        mean = scaling["mediator_mean"].get(m, 0.0)  # type: ignore[union-attr]
        sd = scaling["mediator_sd"].get(m, 1.0)  # type: ignore[union-attr]
        np.testing.assert_allclose((med[:, k] - mean) / sd, _flat(p.prior, f"{m}_latent"),
                                   atol=1e-8)  # fmt: skip


def test_the_sign_settings_reach_the_engine(pf: NestedPriorFit, pf_latent: NestedPriorFit) -> None:
    rp, *_ = _window(pf)
    k = rp.mediators.index("awareness")
    assert (rp.gamma[:, k] <= 0).all() and (rp.beta[:, :, k][:, 0] >= 0).all()
    # awareness is moved by tv only; visits by every channel; every channel acts directly
    assert not rp.beta[:, 1:, k].any() and rp.delta.any(axis=0).all()
    lat, *_ = _window(pf_latent)
    assert not lat.delta.any()  # neither mediator gives its channels a direct effect
    assert lat.beta[:, 2, 1].any() and not lat.beta[:, :2, 1].any()
    assert lat.nu is not None and "nu_y" in pf_latent.fitted.posterior
    assert pf_latent.fitted.learned["scaling"]["mediator_sd"] == {  # type: ignore[index]
        "visits": pytest.approx(float(np.nanstd(pf_latent.stage[MEDIATORS].sel(mediator="visits"))))
    }


def test_mediation_paths_add_up_to_the_channel_totals(pf: NestedPriorFit) -> None:
    win = pf.plugin._window(pf.fitted, None)
    med = mediation_dataset(win, None)
    total = med["direct"] + med["indirect"].sum("mediator")
    xr.testing.assert_allclose(total, med["total"])
    rc = pf.plugin.derive(pf.fitted, "response_curves", pf.stage, ScoreContext())
    np.testing.assert_allclose(
        rc["contribution"].sel(multiplier=1.0).values, med["total"].values, rtol=1e-9
    )
    contrib = pf.plugin.derive(pf.fitted, "contributions", pf.stage, ScoreContext())
    np.testing.assert_allclose(
        contrib["channel_contribution"].sum("time").values, med["total"].values, rtol=1e-9
    )
    share = med["share_mediated"].values
    finite = np.isfinite(share)
    np.testing.assert_allclose(
        share[finite],
        (med["indirect"].sum("mediator") / med["total"]).values[finite],
    )
    # awareness is moved by tv only
    assert not med["indirect"].sel(mediator="awareness", channel=["search", "social"]).any()


# ---------------------------------------------------------------------------- scoring


def _req() -> ScoreRequest:
    return ScoreRequest.model_validate(
        {
            "apiVersion": "mapi/v1",
            "kind": "ScoreRequest",
            "model": "endpoint://p/m",
            "mode": "predict",
        }
    )


def _score(p: NestedPriorFit, ds: xr.Dataset, **kw: Any) -> xr.Dataset:
    schema = DataSchema.model_validate(
        MMMNested.manifest().schema_template.render(p.fitted.settings)  # type: ignore[union-attr]
    )
    tree = p.plugin.score(p.fitted, ds, _req(), ScoreContext(schema=schema, **kw))
    out = tree[PREDICTIONS]
    assert isinstance(out, xr.DataTree)
    return out.to_dataset()


def test_scores_reproduce_the_fit_and_contributions_add_up(pf: NestedPriorFit) -> None:
    pred = _score(pf, pf.stage.drop_vars("y"))
    assert set(pred.y_hat.dims) == {"chain", "draw", "time"}
    total = pred.contributions.sum("component")
    xr.testing.assert_allclose(total, pred.y_hat.transpose(*total.dims))
    assert list(map(str, pred.component.values)) == [
        "tv", "search", "social", BASELINE, TREND, SEASONALITY,
    ]  # fmt: skip
    mu = _flat(pf.prior, "mu")
    np.testing.assert_allclose(pred.y_hat.stack(s=("chain", "draw")).transpose("s", ...).values,
                               mu, rtol=1e-9, atol=1e-6)  # fmt: skip
    assert pred.mediator_hat.dims == ("chain", "draw", "mediator", "time")
    assert list(pred.mediator_hat.mediator.values) == ["visits", "awareness"]


def test_forecasts_extend_beyond_the_window(pf: NestedPriorFit, pf_latent: NestedPriorFit) -> None:
    stage = pf.stage
    future_t = pd.date_range(stage.time.values[-1], periods=5, freq="W-SUN")[1:].values
    future = stage.isel(time=slice(-4, None)).assign_coords(time=future_t).drop_vars("y")
    pred = _score(pf, future.drop_vars(MEDIATORS))
    assert pred.y_hat.sizes["time"] == 4 and np.isfinite(pred.y_hat.values).all()
    xr.testing.assert_allclose(pred.contributions.sum("component"),
                               pred.y_hat.transpose("chain", "draw", "time"))  # fmt: skip
    # a linear trend continues; a piecewise trend holds its last fitted value
    rp, fit, *_ = _window(pf)
    t = rp.trend_at(np.arange(fit.n_periods + 4))
    step = np.diff(t[:, -6:], axis=1)
    np.testing.assert_allclose(step, np.repeat(step[:, :1], 5, axis=1))
    lat, lat_fit, *_ = _window(pf_latent)
    t2 = lat.trend_at(np.arange(lat_fit.n_periods + 4))
    np.testing.assert_allclose(t2[:, -5:], np.repeat(t2[:, -5:-4], 5, axis=1))
    with pytest.raises(MapiError) as err:
        _score(pf, future.drop_vars("spend"))
    assert err.value.code is ErrorCode.MISSING_FUTURE_COVARIATE
    gap = future.assign_coords(time=future_t + np.timedelta64(21, "D"))
    with pytest.raises(MapiError) as err:
        _score(pf, gap)
    assert err.value.code is ErrorCode.NON_CONTIGUOUS_EXTENSION


def test_predictive_scores_add_seeded_noise(pf: NestedPriorFit, pf_latent: NestedPriorFit) -> None:
    for p in (pf, pf_latent):
        ds = p.stage.drop_vars("y").isel(time=slice(-6, None))
        a = _score(p, ds, kind="predictive", seed=1)
        b = _score(p, ds, kind="predictive", seed=1)
        e = _score(p, ds)
        xr.testing.assert_identical(a.y_hat, b.y_hat)
        assert not np.allclose(a.y_hat.values, e.y_hat.values)
        assert a.y_hat.attrs["noise"] == "crn_seeded" and np.isfinite(a.y_hat.values).all()


# ---------------------------------------------------------------------------- derived


def test_derived_datasets(pf: NestedPriorFit) -> None:
    ctx = ScoreContext(draw_index=np.array([[0, i] for i in range(0, DRAWS, 3)]))
    n = len(ctx.draw_index)  # type: ignore[arg-type]
    contrib = pf.plugin.derive(pf.fitted, "contributions", pf.stage, ctx)
    assert contrib["channel_contribution"].dims == ("sample", "channel", "time")
    assert {"baseline", "trend", "seasonality"} <= set(contrib.data_vars)
    rc = pf.plugin.derive(pf.fitted, "response_curves", pf.stage, ctx)
    assert rc["contribution"].dims == ("sample", "channel", "multiplier")
    assert rc.sizes["sample"] == n and "baseline" in rc
    mc = pf.plugin.derive(pf.fitted, "marginal_cost", pf.stage, ctx)
    assert mc["marginal_roi_total"].dims == ("sample", "channel")
    med = pf.plugin.derive(pf.fitted, "mediation", pf.stage, ctx)
    assert med["indirect"].dims == ("sample", "channel", "mediator")
    assert med["indirect"].attrs["units_of"] == "y"
    # the KPI the response curves do not give to a channel is the rest of the expected KPI
    comps = contrib["baseline"] + contrib["trend"] + contrib["seasonality"]
    np.testing.assert_allclose(rc["baseline"].values, comps.sum("time").values, rtol=1e-9)
    with pytest.raises(MapiError) as err:
        pf.plugin.derive(pf.fitted, "nope", pf.stage, ctx)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED


def test_adstock_derived(pf: NestedPriorFit) -> None:
    ad = pf.plugin.derive(pf.fitted, "adstock", pf.stage, ScoreContext())
    assert ad["weight"].dims == ("sample", "channel", "lag") and ad.sizes["lag"] == 8
    np.testing.assert_allclose(ad["weight"].sum("lag").values, 1.0)
    assert ad.attrs["frequency"] == "W"
    rp, *_ = _window(pf)
    np.testing.assert_allclose(ad["weight"].sel(channel="tv").values, rp.kernels[0])


def test_a_tracked_fit_logs_the_summary(stage: xr.Dataset) -> None:
    rec = MetricRecorder()
    build_nested_prior_fit(stage, SETTINGS, draws=12, ctx=FitContext(seed=5, tracker=rec))
    assert rec.params["mmm.mediators"] == "visits,awareness"
    assert rec.params["mmm.n_channels"] == 3 and rec.params["mmm.link"] == "identity"
    series = rec.series()
    for ch in ("tv", "search", "social"):
        for name in ("roi", "roi_p05", "roi_p95", "mroi", "contribution_share"):
            assert (name, f"channel={ch}") in series, (name, ch)
    assert any(k[0] == "baseline_share" for k in series)


# ---------------------------------------------------------------------------- decisions


def test_objective_and_budget_plan(pf: NestedPriorFit) -> None:
    spend = xr.Dataset(
        {"spend": ("channel", [200.0, 100.0, 50.0])}, coords={"channel": ["tv", "search", "social"]}
    )
    spend.attrs["periods"] = 4
    obj = pf.plugin.objective(pf.fitted, spend, ScoreContext())
    assert obj.dims == ("chain", "draw") and np.isfinite(obj.values).all()
    rp, *_ = _window(pf)
    want = rp.steady_state(np.array([[200.0, 100.0, 50.0]]), rp.fit_cells()).sum(axis=(1, 2)) * 4
    np.testing.assert_allclose(obj.stack(s=("chain", "draw")).values, want)
    for bad, code in (
        (xr.Dataset({"spend": ("channel", [1.0])}, coords={"channel": ["radio"]}),
         ErrorCode.UNSEEN_LEVELS),
        (xr.Dataset({"spend": (("geo", "channel"), [[1.0]])},
                    coords={"geo": ["a"], "channel": ["tv"]}), ErrorCode.SCHEMA_INVALID),
    ):  # fmt: skip
        with pytest.raises(MapiError) as err:
            pf.plugin.objective(pf.fitted, bad, ScoreContext())
        assert err.value.code is code
    req = OptimizeRequest.model_validate({"max_draws": 20, "n_steps": 100})
    plan = pf.plugin.optimize(pf.fitted, req, ScoreContext(seed=3))
    assert {a.channel for a in plan.result.allocation} == {"tv", "search", "social"}
    total = sum(a.optimal_spend for a in plan.result.allocation)
    assert total == pytest.approx(plan.result.current_total, rel=0.01)


# ---------------------------------------------------------------------------- bundle


def test_bundle_groups_and_learned(pf: NestedPriorFit) -> None:
    f = pf.fitted
    assert {"posterior", "fit_data", "observed_data", "prior"} <= set(f.groups)
    post = f.posterior
    assert not [v for v in post.data_vars if "obs" in post[v].dims]
    assert "trend_by_period" not in post  # a linear trend replays from its slope
    fd = f.group("fit_data")
    assert fd[MEDIATORS].dims == ("time", "mediator")
    assert np.isnan(fd[MEDIATORS].sel(mediator="awareness").values).sum() > 0
    prior = f.group("prior")
    assert set(prior.data_vars) <= set(post.data_vars) and "alpha_y" in prior
    learned = f.learned
    assert learned["mediators"][1]["channels"] == ["tv"]  # type: ignore[index]
    assert learned["season_terms"] == [{"name": "yearly", "period": 52.0, "order": 2}]
    assert learned["trend"] == {"kind": "linear", "n_periods": 60}


def test_piecewise_trend_is_stored_per_period(pf_latent: NestedPriorFit) -> None:
    tb = pf_latent.fitted.posterior["trend_by_period"]
    assert tb.dims == ("chain", "draw", "trend_period") and tb.sizes["trend_period"] == 60


def test_manifest_and_about() -> None:
    m = MMMNested.manifest()
    assert (m.id, m.version, m.family) == ("mmm.nested", "1.0.0", "mmm")
    assert m.capabilities.calibration == frozenset({"lift_tests"})
    assert {d.name for d in m.derived} == {
        "contributions", "response_curves", "marginal_cost", "adstock", "mediation",
    }  # fmt: skip
    assert "mmm.mediation" in m.plots and "mediator_hat" in m.outputs
    schema = DataSchema.model_validate(m.schema_template.render({}))  # type: ignore[union-attr]
    assert schema.vars[MEDIATORS].missing == "allow"
    assert schema.vars["spend"].future_required
    report = check_about(MMMNested, raise_on_failure=False)
    assert report.ok, report.summary()


# ---------------------------------------------------------------------------- checks


@pytest.mark.parametrize(
    ("settings", "change", "code", "pointer"),
    [
        ({"mediators": [{"name": "footfall"}]}, None, ErrorCode.SETTINGS_INVALID,
         "/model/settings/mediators/0/name"),
        ({"mediators": [{"name": "visits", "channels": ["radio"]}]}, None,
         ErrorCode.SETTINGS_INVALID, "/model/settings/mediators/0/channels"),
        ({"mediators": [{"name": "visits", "channels": ["tv"]}]}, None,
         ErrorCode.SETTINGS_INVALID, "/model/settings/mediators"),
        ({}, "empty_mediator", ErrorCode.SCHEMA_INVALID, "/data/mediators"),
        ({}, "no_mediators", ErrorCode.SCHEMA_INVALID, "/data/mediators"),
        ({}, "nan_y", ErrorCode.SCHEMA_INVALID, "/data/y"),
    ],
)  # fmt: skip
def test_data_and_settings_checks(
    stage: xr.Dataset, settings: dict[str, Any], change: str | None, code: ErrorCode, pointer: str
) -> None:
    ds = stage
    if change == "empty_mediator":
        ds = stage.copy()
        ds[MEDIATORS] = stage[MEDIATORS].where(stage.mediator != "awareness")
    elif change == "no_mediators":
        ds = stage.drop_vars(MEDIATORS)
    elif change == "nan_y":
        ds = stage.copy()
        ds["y"] = stage.y.where(stage.time != stage.time[3])
    plugin = MMMNested()
    with pytest.raises(MapiError) as err:
        plugin.fit(ds, plugin.parse_settings(settings), FitConfig(engine="map"), FitContext())
    assert err.value.code is code and err.value.pointer == pointer


def test_settings_validation() -> None:
    plugin = MMMNested()
    for bad in (
        {"mediators": [{"name": "a"}, {"name": "a"}]},
        {"mediators": [{"name": "a", "channels": ["tv", "tv"]}]},
        {"mediators": [{"name": "a", "channels": []}]},
        {"mediators": [{"name": "a", "observation_sigma": 0}]},
        {"adstock": {"kind": "geometric"}},
    ):
        with pytest.raises(MapiError) as err:
            plugin.parse_settings(bad)
        assert err.value.code is ErrorCode.SETTINGS_INVALID


def test_business_priors_are_refused(stage: xr.Dataset) -> None:
    from mapi_spec.calibration import Calibration

    cal = Calibration.model_validate(
        {"business_priors": {"channels": {"tv": {"roi": [0.2, 0.6], "credibility": 0.9}}}}
    )
    plugin = MMMNested()
    with pytest.raises(MapiError) as err:
        plugin.fit(stage, plugin.parse_settings({}), FitConfig(engine="map"),
                   FitContext(calibration=cal))  # fmt: skip
    assert err.value.code is ErrorCode.CALIBRATION_UNSUPPORTED


def test_lift_tests_become_a_bundle_group(stage: xr.Dataset) -> None:
    calibration = pytest.importorskip("mapi_plugin_mmm.calibration")
    if not hasattr(calibration, "lift_test_group"):
        pytest.skip("calibration.lift_test_group has not landed")
    from mapi_spec.calibration import Calibration

    t = pd.Timestamp(stage.time.values[20]).date()
    cal = Calibration.model_validate(
        {"lift_tests": [{"channel": "tv", "start": str(t), "end": str(t + pd.Timedelta(weeks=3)),
                         "spend_delta": 500.0, "kpi_delta": 200.0, "se": 60.0}]}
    )  # fmt: skip
    p = build_nested_prior_fit(stage, SETTINGS, ctx=FitContext(seed=5, calibration=cal))
    assert p.fitted.learned["calibration"]["lift_tests"]["used"] == ["lift_0"]  # type: ignore[index]
    lt = p.fitted.group("lift_tests")
    assert lt["model"].dims == ("chain", "draw", "test")
    assert list(lt["channel"].values) == ["tv"]
    assert "lift_0_model_estimand" in p.fitted.posterior
