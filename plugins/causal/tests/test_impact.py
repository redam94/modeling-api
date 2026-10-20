"""``causal.impact`` without MCMC: prior draws stand in for the posterior (I-6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pymc as pm
import pytest
import xarray as xr
from mapi_plugin_causal._common import parse_window
from mapi_plugin_causal.datasets import ImpactTruth
from mapi_plugin_causal.impact import CausalImpactModel, ImpactSettings
from mapi_plugin_ts.bsts import BSTSFit, build_model, postprocess_draws, standardise
from mapi_sdk.models import PREDICTIONS, FitContext, FitOutput, Fitted, ScoreContext
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import ValidationError

SEED = 3
CONTROLS = ["control_a", "control_b"]


def _canonical(ds: xr.Dataset) -> xr.Dataset:
    return ds.rename({"week": "time", "sales": "y"})


def _settings(start: str, **extra: Any) -> ImpactSettings:
    return ImpactSettings.model_validate(
        {"controls": CONTROLS, "intervention_start": start, "trend": "level", **extra}
    )


def _prior_fit(ds: xr.Dataset, s: ImpactSettings, draws: int = 40) -> BSTSFit:
    times = ds["time"].values
    start = parse_window(times, s.intervention_start, s.intervention_end).start
    y = ds["y"].values[:start]
    X = np.stack([ds[c].values[:start] for c in CONTROLS], axis=1)
    y_std, X_std, scaling = standardise(y, X, CONTROLS)
    model, ss = build_model(s, y_std, X_std, CONTROLS)
    with model:
        tree = pm.sample_prior_predictive(draws=draws, random_seed=SEED)
    prior = tree["prior"]
    prior = prior.to_dataset(inherit=False) if isinstance(prior, xr.DataTree) else prior
    free = [rv.name for rv in model.free_RVs]
    return postprocess_draws(
        model, ss, prior[free], CONTROLS, y_std=y_std, X_std=X_std, scaling=scaling
    )


@dataclass
class PriorImpact:
    ds: xr.Dataset
    settings: ImpactSettings
    fit: BSTSFit
    out: FitOutput
    fitted: Fitted
    recorder: MetricRecorder
    truth: ImpactTruth


def _assemble(ds: xr.Dataset, s: ImpactSettings, fit: BSTSFit, **ctx_kw: Any) -> FitOutput:
    return CausalImpactModel().assemble(ds, s, fit, seed=SEED, ctx=FitContext(**ctx_kw))


@pytest.fixture(scope="module")
def prior_impact(impact_data: tuple[xr.Dataset, ImpactTruth]) -> PriorImpact:
    raw, truth = impact_data
    ds = _canonical(raw)
    s = _settings(truth.start)
    fit = _prior_fit(ds, s)
    rec = MetricRecorder()
    out = _assemble(ds, s, fit, tracker=rec)
    fitted = Fitted(
        manifest=CausalImpactModel.manifest(),
        settings=s.model_dump(mode="json"),
        fit_config=FitConfig(mode="train_only", seed=SEED),
        groups=out.groups,
        learned=out.learned,
        terminal_state=out.terminal_state,
    )
    return PriorImpact(ds, s, fit, out, fitted, rec, truth)


# ------------------------------------------------------------------------------ settings


def test_settings() -> None:
    s = _settings("2024-01-01", intervention_end="")
    assert s.intervention_end is None and s.controls == CONTROLS
    for bad in (["y"], ["a", "a"], ["not a name"], ["time"]):
        with pytest.raises(ValidationError):
            _settings("2024-01-01", controls=bad)


def test_manifest() -> None:
    m = CausalImpactModel.manifest()
    assert (m.id, m.version, m.model_family) == ("causal.impact", "1.0.0", "causal")
    caps = m.capabilities
    assert caps.score_modes == frozenset({"predict", "forecast"})
    assert not caps.loo_valid and caps.loo_invalid_reason
    assert "scaling" in caps.owns
    assert set(m.outputs) == {"y_hat", "effect"}
    assert m.plots[0] == "causal.impact"
    schema = DataSchema.model_validate(m.schema_template.render({"controls": CONTROLS}))
    assert set(schema.vars) == {"y", *CONTROLS}
    assert schema.vars["control_a"].future_required


# ------------------------------------------------------------------------------ fit


def test_groups_and_layouts(prior_impact: PriorImpact) -> None:
    g = prior_impact.out.groups
    n, start = prior_impact.ds.sizes["time"], 44
    assert {
        "posterior",
        "observed_data",
        "constant_data",
        "impact",
        "components",
        "one_step",
    } <= set(g)
    imp = g["impact"]
    assert imp["counterfactual"].dims == ("sample", "time")
    assert imp.sizes["time"] == n and "unit" not in imp.dims
    assert imp["post"].values.sum() == n - start and not imp["post"].values[:start].any()
    assert imp.attrs["intervention_start"] == prior_impact.truth.start
    assert imp.attrs["intervention_end"] == ""
    assert imp.attrs["model"] == "causal.impact@1.0.0"
    np.testing.assert_allclose(
        imp["effect"].values, imp["actual"].values[None] - imp["counterfactual"].values
    )
    assert g["one_step"]["mean"].dims == ("chain", "draw", "time")
    assert g["one_step"].sizes["time"] == start
    assert g["components"].sizes["time"] == start
    term = prior_impact.out.terminal_state
    assert term is not None and term.attrs["last_label"] == "2023-10-30"
    assert int(term.attrs["n_fit"]) == start
    learned = prior_impact.out.learned
    assert learned["n_pre"] == start and learned["controls"] == CONTROLS


def test_pre_period_counterfactual_tracks_the_actual(prior_impact: PriorImpact) -> None:
    imp = prior_impact.out.groups["impact"]
    pre = ~imp["post"].values
    actual = imp["actual"].values[pre]
    cf = imp["counterfactual"].values[:, pre]
    # one-step-ahead predictions follow the data up to their (prior-draw) noise
    lo, hi = np.quantile(cf, [0.02, 0.98], axis=0)
    inside = ((actual >= lo) & (actual <= hi))[5:]
    assert inside.mean() > 0.8
    assert np.corrcoef(np.median(cf, axis=0), actual)[0, 1] > 0.5


def test_post_forecast_uses_the_post_controls(prior_impact: PriorImpact) -> None:
    ds2 = prior_impact.ds.copy(deep=True)
    start = 44
    ds2["control_a"].values[start:] += 10.0
    out2 = _assemble(ds2, prior_impact.settings, prior_impact.fit)
    cf1 = prior_impact.out.groups["impact"]["counterfactual"].values
    cf2 = out2.groups["impact"]["counterfactual"].values
    np.testing.assert_allclose(cf2[:, :start], cf1[:, :start])
    sc = prior_impact.fit.scaling
    want = sc.y_scale * prior_impact.fit.beta[:, 0] * 10.0 / sc.x_scale[0]
    np.testing.assert_allclose(
        cf2[:, start:] - cf1[:, start:],
        np.repeat(want[:, None], cf1.shape[1] - start, 1),
        rtol=1e-6,
        atol=1e-6,
    )


def test_tracking_keys(prior_impact: PriorImpact) -> None:
    params = prior_impact.recorder.params
    for key in (
        "bsts.trend",
        "bsts.seasonality",
        "bsts.regressors",
        "bsts.n_states",
        "bsts.n_periods",
        "bsts.missing_periods",
        "causal.intervention_start",
        "causal.intervention_end",
        "causal.n_pre",
        "causal.n_post",
        "causal.n_treated",
    ):
        assert key in params, key
    assert params["bsts.regressors"] == "control_a,control_b"
    assert params["causal.n_pre"] == 44 and params["causal.n_treated"] == 1
    keys = {k for k, _ in prior_impact.recorder.series()}
    assert {"effect_total", "p_effect_positive", "pre_rmse", "effect_relative"} <= keys


def test_window_and_control_errors(prior_impact: PriorImpact) -> None:
    ds = prior_impact.ds
    with pytest.raises(MapiError) as err:
        _assemble(ds, _settings("2023-01-03"), prior_impact.fit)
    assert err.value.code == ErrorCode.SETTINGS_INVALID
    bad = ds.copy(deep=True)
    bad["control_b"].values[50] = np.nan
    with pytest.raises(MapiError) as err:
        _assemble(bad, prior_impact.settings, prior_impact.fit)
    assert err.value.code == ErrorCode.SCHEMA_INVALID


@pytest.mark.parametrize("drop", [[30, 31], [10, 50, 51]], ids=["gap", "mixed"])
def test_fit_rejects_unevenly_spaced_periods(prior_impact: PriorImpact, drop: list[int]) -> None:
    """Regression: the filter counts periods by position, so missing weeks used to fit without
    an error and shift the trend, the season and the post-period forecast."""
    ds = prior_impact.ds
    gappy = ds.isel(time=[i for i in range(ds.sizes["time"]) if i not in drop])
    with pytest.raises(MapiError, match="not evenly spaced") as err:
        CausalImpactModel().fit(
            gappy, prior_impact.settings, FitConfig(mode="train_only", seed=SEED), FitContext()
        )
    assert err.value.code == ErrorCode.SCHEMA_INVALID


def test_short_pre_period_diagnostic(prior_impact: PriorImpact) -> None:
    plugin = CausalImpactModel()
    codes = {d.code for d in plugin.diagnostics(prior_impact.fitted, prior_impact.ds)}
    assert "SHORT_PRE_PERIOD" not in codes
    learned = {**prior_impact.fitted.learned, "longest_season": 52.18}
    fitted = Fitted(
        manifest=prior_impact.fitted.manifest,
        settings=prior_impact.fitted.settings,
        fit_config=prior_impact.fitted.fit_config,
        groups=prior_impact.fitted.groups,
        learned=learned,
        terminal_state=prior_impact.fitted.terminal_state,
    )
    diags = [d for d in plugin.diagnostics(fitted, prior_impact.ds) if d.code == "SHORT_PRE_PERIOD"]
    assert len(diags) == 1 and diags[0].severity == "warning"


# ------------------------------------------------------------------------------ score


def _req(mode: str = "predict") -> ScoreRequest:
    doc: dict[str, Any] = {
        "apiVersion": "mapi/v1",
        "kind": "ScoreRequest",
        "model": "endpoint://p/m",
        "mode": mode,
    }
    if mode == "forecast":
        doc["horizon"] = {"dim": "time", "periods": 4}
    return ScoreRequest.model_validate(doc)


def _score(pi: PriorImpact, ds: xr.Dataset, mode: str = "predict", **kw: Any) -> xr.Dataset:
    tree = CausalImpactModel().score(pi.fitted, ds, _req(mode), ScoreContext(mode=mode, **kw))  # type: ignore[arg-type]
    out = tree[PREDICTIONS]
    assert isinstance(out, xr.DataTree)
    return out.to_dataset()


def test_score_predict_repeats_the_fit(prior_impact: PriorImpact) -> None:
    pred = _score(prior_impact, prior_impact.ds, kind="predictive", seed=SEED)
    assert pred["y_hat"].dims == ("chain", "draw", "time")
    y_hat = pred["y_hat"].values.reshape(-1, pred.sizes["time"])
    cf = prior_impact.out.groups["impact"]["counterfactual"].values
    np.testing.assert_allclose(y_hat, cf, rtol=1e-8, atol=1e-8)
    effect = pred["effect"].values.reshape(y_hat.shape)
    np.testing.assert_allclose(effect, prior_impact.ds["y"].values[None] - y_hat)
    # expected: the mean propagation, no noise
    exp = _score(prior_impact, prior_impact.ds.isel(time=slice(40, 50)), kind="expected")
    assert exp.sizes["time"] == 10
    assert np.isfinite(exp["y_hat"].values).all()


def _future(pi: PriorImpact, weeks: int = 4) -> xr.Dataset:
    from mapi_plugin_causal.datasets import impact_dataset

    n = pi.ds.sizes["time"]
    longer, _ = impact_dataset(weeks=n + weeks, start_week=44, lift=0.1, seed=0)
    return _canonical(longer).isel(time=slice(n, None)).drop_vars("y")


def test_score_forecast_past_the_data(prior_impact: PriorImpact) -> None:
    fut = _future(prior_impact)
    pred = _score(prior_impact, fut, mode="forecast", kind="expected")
    assert pred.sizes["time"] == 4
    assert np.isfinite(pred["y_hat"].values).all()
    assert "effect" not in pred  # no actual values: no effect
    # the forecast continues the fit's counterfactual (same terminal state and controls)
    both = xr.concat([prior_impact.ds.drop_vars("y"), fut], dim="time")
    whole = _score(prior_impact, both, mode="forecast", kind="expected")
    np.testing.assert_allclose(
        whole["y_hat"].values[..., -4:], pred["y_hat"].values, rtol=1e-8, atol=1e-8
    )


def test_score_errors(prior_impact: PriorImpact) -> None:
    fut = _future(prior_impact).drop_vars("control_b")
    with pytest.raises(MapiError) as err:
        _score(prior_impact, fut, mode="forecast")
    assert err.value.code == ErrorCode.MISSING_FUTURE_COVARIATE
    gap = _future(prior_impact, 6).isel(time=slice(2, None))
    with pytest.raises(MapiError) as err:
        _score(prior_impact, gap.assign_coords(time=gap["time"].values + np.timedelta64(1, "D")))
    assert err.value.code == ErrorCode.COORD_NOT_ALLOWED
