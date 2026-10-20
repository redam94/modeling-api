"""The shared extension-model helpers against mmm-framework's own graph pieces (prior
draws, no MCMC)."""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd
import pymc as pm
import pytest
from mapi_plugin_mmm.extended import (
    EXT_L_MAX,
    ExtSettings,
    ext_configs,
    ext_kernels,
    fourier_at,
    media_scale,
    national_panel,
    saturated_media,
    season_terms,
    trend_values,
)
from mapi_plugin_mmm.forward import Posterior
from mapi_plugin_mmm.framework import period_index
from mapi_plugin_mmm.settings import SeasonalitySettings
from mapi_spec.errors import ErrorCode, MapiError

_FREQ = {"W": "W-SUN", "D": "D", "M": "MS"}


def _prior(model: pm.Model, names: list[str], draws: int = 30) -> dict[str, np.ndarray]:
    with model, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = pm.sample_prior_predictive(draws=draws, random_seed=3)
    prior = tree["prior"].to_dataset()
    return {n: np.asarray(prior[n].values).reshape(draws, *prior[n].shape[2:]) for n in names}


@pytest.mark.parametrize(
    ("freq", "orders"),
    [
        ("W", {"yearly_order": 2, "monthly_order": 1}),
        ("W", {"yearly_order": 30 // 2, "weekly_order": 2}),  # weekly: not in the table
        ("D", {"yearly_order": 1, "weekly_order": 3}),
        ("M", {"yearly_order": 9}),  # clamped to period / 2
    ],
)
def test_fourier_design_matches_the_engine_seasonality(freq: str, orders: dict[str, int]) -> None:
    from mmm_framework.mmm_extensions.components.temporal import build_seasonality_contribution

    s = SeasonalitySettings(**orders)
    model_cfg, _ = ext_configs(ExtSettings(seasonality=s), frequency=freq)
    n = 80
    times = pd.date_range("2022-01-02", periods=n, freq=_FREQ[freq]).values
    with pm.Model() as model, warnings.catch_warnings():
        warnings.simplefilter("ignore")
        term = build_seasonality_contribution(
            "", period_index(times, freq), n, model_cfg.seasonality
        )
        assert term is not None
        pm.Deterministic("season", term)
    draws = _prior(model, ["seasonality_coefs", "season"])
    terms = season_terms(freq, s)
    design = fourier_at(np.arange(n), terms)
    assert design.shape[1] == draws["seasonality_coefs"].shape[1]
    np.testing.assert_allclose(draws["seasonality_coefs"] @ design.T, draws["season"], atol=1e-10)
    # a forecast position continues the same cycle
    later = fourier_at(np.arange(n, n + 5), terms)
    assert later.shape == (5, design.shape[1])


def test_season_terms_skip_and_clamp_like_the_engine() -> None:
    assert season_terms("W", SeasonalitySettings(yearly_order=0)) == []
    terms = season_terms("W", SeasonalitySettings(yearly_order=2, weekly_order=3))
    assert [(t.name, t.order) for t in terms] == [("yearly", 2)]
    (m,) = season_terms("M", SeasonalitySettings(yearly_order=26))
    assert (m.period, m.order) == (12.0, 6)
    assert fourier_at(np.arange(4), []).shape == (4, 0)


def test_saturated_media_matches_the_engine_transform() -> None:
    from mmm_framework.mmm_extensions.config import MediatorConfig, NestedModelConfig
    from mmm_framework.mmm_extensions.models import NestedMMM

    rng = np.random.default_rng(1)
    n, chans = 40, ["tv", "search"]
    spend = rng.gamma(2.0, 100.0, (n, 2)) * rng.binomial(1, 0.8, (n, 2))
    eng = NestedMMM(
        spend, rng.normal(size=n), chans, NestedModelConfig(mediators=(MediatorConfig("v"),))
    )
    with pm.Model() as model:
        for i, ch in enumerate(chans):
            alpha = pm.Beta(f"alpha_{ch}", 2, 2)
            lam = pm.Gamma(f"lambda_{ch}", 3, 1)
            pm.Deterministic(f"sat_{ch}", eng._media_transform_apply(i, alpha, lam)(spend[:, i]))
    names = [f"{p}_{ch}" for ch in chans for p in ("alpha", "lambda", "sat")]
    draws = _prior(model, names)
    scale = media_scale(spend)
    np.testing.assert_allclose(scale, eng._media_scale)
    kernels = [ext_kernels(draws[f"alpha_{ch}"]) for ch in chans]
    assert kernels[0].shape == (30, EXT_L_MAX)
    np.testing.assert_allclose(kernels[0].sum(axis=1), 1.0)
    lam = [draws[f"lambda_{ch}"] for ch in chans]
    pos = np.arange(n)
    got = saturated_media(spend, pos, kernels, lam, scale)
    for i, ch in enumerate(chans):
        np.testing.assert_allclose(got[:, i], draws[f"sat_{ch}"], rtol=1e-9, atol=1e-12)
    # a later window with the full history in front of it gives the same values
    np.testing.assert_allclose(saturated_media(spend, pos[10:], kernels, lam, scale),
                               got[:, :, 10:])  # fmt: skip


@pytest.mark.parametrize("kind", ["linear", "piecewise", "spline"])
def test_trend_values_match_the_engine_trend(kind: str) -> None:
    from mmm_framework.mmm_extensions.components.temporal import build_trend_contribution

    _, trend_cfg = ext_configs(ExtSettings(trend={"kind": kind}), frequency="W")
    n = 30
    with pm.Model() as model:
        term = build_trend_contribution("", n, trend_cfg)
        assert term is not None
        pm.Deterministic("trend", term)
    free = [rv.name for rv in model.free_RVs]
    draws = _prior(model, [*free, "trend"])
    post = Posterior(dict(draws), 30)
    by_period = draws["trend"] if kind != "linear" else None
    pos = np.arange(n + 6)
    got = trend_values(post, kind=kind, slope_name="trend_slope", by_period=by_period,
                       n_periods=n, positions=pos)  # fmt: skip
    np.testing.assert_allclose(got[:, :n], draws["trend"], atol=1e-10)
    if kind == "linear":  # extrapolated
        np.testing.assert_allclose(got[:, n:], draws["trend_slope"][:, None] * pos[n:] / (n - 1))
    else:  # held at the last fitted value
        np.testing.assert_allclose(got[:, n:], np.repeat(got[:, n - 1 : n], 6, axis=1))


def test_trend_values_edge_cases() -> None:
    post = Posterior({"x": np.zeros(4)}, 4)
    pos = np.arange(3)
    assert not trend_values(post, kind="none", slope_name="trend_slope", by_period=None,
                            n_periods=3, positions=pos).any()  # fmt: skip
    assert not trend_values(post, kind="linear", slope_name="trend_slope", by_period=None,
                            n_periods=3, positions=pos).any()  # fmt: skip
    with pytest.raises(MapiError) as err:
        trend_values(post, kind="gp", slope_name="trend_slope", by_period=None, n_periods=3,
                     positions=pos)  # fmt: skip
    assert err.value.code is ErrorCode.INTEGRITY_FAILURE


def test_ext_configs_map_the_settings() -> None:
    from mmm_framework.config.enums import LikelihoodFamily
    from mmm_framework.transforms.seasonality import SeasonalityPeriodSource

    s = ExtSettings(
        likelihood="student_t",
        trend={"kind": "piecewise", "n_changepoints": 4},
        seasonality={"yearly_order": 3, "prior_sigma": 0.7},
    )
    model_cfg, trend_cfg = ext_configs(s, frequency="W")
    assert model_cfg.seasonality.period_source is SeasonalityPeriodSource.FREQUENCY_TABLE
    assert (model_cfg.seasonality.yearly, model_cfg.seasonality.prior_sigma) == (3, 0.7)
    assert model_cfg.likelihood.family is LikelihoodFamily.STUDENT_T
    assert str(trend_cfg.type.value) == "piecewise" and trend_cfg.n_changepoints == 4
    normal, _ = ext_configs(ExtSettings(), frequency="W")
    assert getattr(normal.likelihood, "family", LikelihoodFamily.NORMAL) is LikelihoodFamily.NORMAL


def test_national_panel_layout() -> None:
    times = pd.date_range("2024-01-07", periods=5, freq="W-SUN").values
    spend: Any = np.arange(10.0).reshape(5, 2)
    panel = national_panel(times, ["a", "b"], spend, np.ones(5))
    assert panel.spend.shape == (5, 1, 1, 2) and panel.ctrl.shape == (5, 1, 1, 0)
    assert panel.y is not None and panel.y.shape == (5, 1, 1)
    assert (panel.n_cells, panel.panel, panel.channels) == (1, (), ["a", "b"])
    assert national_panel(times, ["a", "b"], spend, None).y is None
