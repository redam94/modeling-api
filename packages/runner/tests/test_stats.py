"""The statistical building blocks of the explorers and governance checks, against
reference values: statsmodels/scipy outputs on the same data, textbook formulas computed by
hand, and published definitions (Ljung-Box Q, Diebold-Mariano with the HLN correction,
FPP3 benchmark forecasts, Wang-Smith-Hyndman strengths, VIF = 1 / (1 - R^2))."""

from __future__ import annotations

import math

import numpy as np
import pytest
from mapi_runner.worker import stats
from scipy import stats as sps

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore::FutureWarning")]


def _ar1(n: int, phi: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    e = rng.normal(size=n)
    y = np.zeros(n)
    for t in range(1, n):
        y[t] = phi * y[t - 1] + e[t]
    return y


def test_adf_matches_statsmodels_and_decides_both_ways() -> None:
    from statsmodels.tsa.stattools import adfuller

    walk = np.cumsum(np.random.default_rng(1).normal(size=300))
    stable = _ar1(300, 0.3, seed=2)
    for y in (walk, stable):
        ref = adfuller(y, regression="c", autolag="AIC")
        res = stats.adf_test(y)
        assert res.statistic == pytest.approx(ref[0])
        assert res.p_value == pytest.approx(ref[1])
        assert res.lags == ref[2] and res.n_obs == ref[3]
        assert res.critical_values["5%"] == pytest.approx(ref[4]["5%"])
    assert stats.adf_test(walk).decision == "unit_root"
    assert stats.adf_test(stable).decision == "stationary"


def test_kpss_reverses_the_null_and_stationarity_verdicts_combine() -> None:
    walk = np.cumsum(np.random.default_rng(3).normal(size=300))
    stable = _ar1(300, 0.2, seed=4)
    kw, ks = stats.kpss_test(walk), stats.kpss_test(stable)
    assert kw.reject and kw.decision == "unit_root"
    assert not ks.reject and ks.decision == "stationary"
    assert stats.stationarity_verdict(stats.adf_test(stable), ks)[0] == "stable"
    assert stats.stationarity_verdict(stats.adf_test(walk), kw)[0] == "difference"


def test_ljung_box_equals_the_textbook_q_statistic() -> None:
    y = _ar1(120, 0.5, seed=5)
    lags = 6
    n = y.size
    z = y - y.mean()
    denom = float(np.sum(z**2))
    r = [float(np.sum(z[k:] * z[:-k]) / denom) for k in range(1, lags + 1)]
    q = n * (n + 2) * sum(rk**2 / (n - k) for k, rk in enumerate(r, start=1))
    res = stats.ljung_box_test(y, lags=lags)
    assert res.statistic == pytest.approx(q, rel=1e-10)
    assert res.p_value == pytest.approx(float(sps.chi2.sf(q, lags)), rel=1e-8)
    assert res.decision == "autocorrelated"
    white = np.random.default_rng(6).normal(size=200)
    assert stats.ljung_box_test(white, lags=10).decision == "white_noise"
    # ARMA terms remove degrees of freedom
    adj = stats.ljung_box_test(y, lags=lags, model_df=2)
    assert adj.p_value == pytest.approx(float(sps.chi2.sf(q, lags - 2)), rel=1e-8)


def test_jarque_bera_breusch_pagan_and_arch_match_their_references() -> None:
    from statsmodels.stats.diagnostic import het_arch, het_breuschpagan

    rng = np.random.default_rng(7)
    x = rng.exponential(size=300)
    jb = stats.jarque_bera_test(x)
    ref = sps.jarque_bera(x)
    assert jb.statistic == pytest.approx(ref.statistic) and jb.decision == "not_normal"
    fitted = np.linspace(1, 10, 300)
    resid = rng.normal(scale=fitted)  # spread grows with the fit
    bp = stats.breusch_pagan_test(resid, fitted)
    lm = het_breuschpagan(resid, np.column_stack([np.ones(300), fitted]))[0]
    assert bp.statistic == pytest.approx(lm) and bp.decision == "unequal_spread"
    arch = stats.arch_lm_test(resid, lags=4)
    assert arch.statistic == pytest.approx(het_arch(resid, nlags=4)[0])


def test_acf_and_pacf_suggest_the_ar_order_of_an_ar2_process() -> None:
    rng = np.random.default_rng(8)
    n = 600
    y = np.zeros(n)
    e = rng.normal(size=n)
    for t in range(2, n):
        y[t] = 0.5 * y[t - 1] + 0.3 * y[t - 2] + e[t]
    acf, pacf = stats.acf_profile(y, 20), stats.pacf_profile(y, 20)
    assert acf.bound == pytest.approx(1.96 / math.sqrt(n))
    assert acf.values[0] == pytest.approx(1.0)
    assert {1, 2} <= set(pacf.significant)
    assert stats.suggest_arma_orders(acf, pacf)["p"] >= 2


def test_ccf_sign_convention_finds_a_driver_that_leads_by_two() -> None:
    rng = np.random.default_rng(9)
    x = rng.normal(size=200)
    y = np.r_[np.zeros(2), x[:-2]] + 0.1 * rng.normal(size=200)
    prof = stats.ccf_profile(x, y, 5)
    assert prof.peak_lag == 2
    k = prof.lags.index(2)
    assert prof.values[k] == pytest.approx(float(np.corrcoef(x[:-2], y[2:])[0, 1]))
    assert 2 in prof.significant
    xs, ys, p = stats.prewhiten(np.cumsum(x), np.cumsum(y))
    assert p >= 1 and xs.shape == ys.shape


def _ar1_from(rng: np.random.Generator, n: int, phi: float) -> np.ndarray:
    e = rng.normal(size=n)
    out = np.zeros(n)
    for t in range(1, n):
        out[t] = phi * out[t - 1] + e[t]
    return out


def test_ccf_of_independent_persistent_series_flags_few_lags() -> None:
    """Regression (simulation): the CCF band was the white-noise 1.96/sqrt(n), valid only
    when one series is white; for two independent AR(1) series (phi = 0.9) 53% of the
    lags were flagged significant. The band now follows Bartlett's formula for two
    autocorrelated series, and the Data Explorer prewhitens by default."""
    from mapi_spec.explore import DataExploreRequest

    assert DataExploreRequest().prewhiten is True
    reps, n, max_lag = 120, 104, 26
    raw_flags = pw_flags = total = 0
    for seed in range(reps):
        rng = np.random.default_rng(seed)
        x, y = _ar1_from(rng, n, 0.9), _ar1_from(rng, n, 0.9)
        prof = stats.ccf_profile(x, y, max_lag)
        raw_flags += len(prof.significant)
        xs, ys, _p = stats.prewhiten(x, y)
        pw_flags += len(stats.ccf_profile(xs, ys, max_lag).significant)
        total += 2 * max_lag
    assert raw_flags / total <= 0.10, raw_flags / total
    assert pw_flags / total <= 0.10, pw_flags / total
    # a white driver keeps the classic band
    rng = np.random.default_rng(1)
    white = stats.ccf_profile(rng.normal(size=400), rng.normal(size=400), 5)
    assert white.bound == pytest.approx(1.96 / math.sqrt(400), rel=0.15)


def test_stl_strengths_separate_a_seasonal_series_from_noise() -> None:
    rng = np.random.default_rng(10)
    t = np.arange(208)
    seasonal = 10 * np.sin(2 * np.pi * t / 52) + rng.normal(size=t.size)
    parts = stats.stl_decompose(seasonal, 52)
    assert parts is not None
    fs, _ft = stats.season_strengths(parts)
    assert fs > 0.8
    noise_parts = stats.stl_decompose(rng.normal(size=208), 52)
    assert noise_parts is not None and stats.season_strengths(noise_parts)[0] < 0.5
    assert stats.stl_decompose(seasonal[:60], 52) is None


def test_vif_equals_one_over_one_minus_r_squared() -> None:
    rng = np.random.default_rng(11)
    a = rng.normal(size=500)
    b = a + 0.3 * rng.normal(size=500)
    c = rng.normal(size=500)
    table = stats.vif_table({"a": a, "b": b, "c": c})
    design = np.column_stack([np.ones(500), b, c])
    coef, *_ = np.linalg.lstsq(design, a, rcond=None)
    r2 = 1 - np.sum((a - design @ coef) ** 2) / np.sum((a - a.mean()) ** 2)
    assert table["a"] == pytest.approx(1 / (1 - r2))
    assert table["c"] is not None and table["c"] < 1.1
    assert stats.condition_number({"a": a, "b": b}) is not None


def test_outlier_scores_agree_under_normality_and_flag_spikes() -> None:
    rng = np.random.default_rng(12)
    x = rng.normal(size=5000)
    x[10] = 12.0
    for method in ("mad", "iqr", "zscore"):
        s = stats.outlier_scores(x, method)  # type: ignore[arg-type]
        assert abs(s[10]) > 8
        assert np.mean(np.abs(s) > 3.5) < 0.005


def test_outlier_scores_ignore_binary_variables() -> None:
    """The ties of a network (E21 cowork: 85 % zeros, 15 % ones) are not unusual values."""
    ties = np.zeros(5041)
    ties[:756] = 1.0
    ties[5] = np.nan
    for method in ("mad", "iqr", "zscore"):
        assert np.isnan(stats.outlier_scores(ties, method)).all()  # type: ignore[arg-type]
    three = np.r_[np.zeros(100), 1.0, 9.0]
    assert np.isfinite(stats.outlier_scores(three, "mad")).any()
    dose = np.r_[np.full(60, 0.2), np.full(40, 0.7)]  # two levels, both frequent
    assert np.isnan(stats.outlier_scores(dose, "mad")).all()


def test_outlier_scores_flag_one_spike_in_a_constant_series() -> None:
    """A price of 9.99 with one typo of 999.0 has two values, but the spike is unusual."""
    price = np.r_[np.full(103, 9.99), 999.0]
    for method in ("mad", "iqr", "zscore"):
        s = stats.outlier_scores(price, method)  # type: ignore[arg-type]
        assert np.isfinite(s[-1]) and abs(s[-1]) > 3.5, method


def test_block_permutation_is_a_permutation_that_keeps_blocks() -> None:
    rng = np.random.default_rng(13)
    perm = stats.block_permutation(103, 10, rng)
    assert sorted(perm.tolist()) == list(range(103))
    starts = [i for i in range(103) if i == 0 or perm[i] != perm[i - 1] + 1]
    assert len(starts) <= 11


def test_naive_benchmarks_follow_fpp3() -> None:
    hist = np.array([1.0, 2, 3, 4, 5, 6, 7, 8])
    fc = stats.naive_forecasts(
        hist, 3, season=4, methods=("naive", "seasonal_naive", "mean", "drift")
    )
    assert fc["naive"].tolist() == [8, 8, 8]
    assert fc["seasonal_naive"].tolist() == [5, 6, 7]
    assert fc["mean"].tolist() == [4.5] * 3
    assert fc["drift"].tolist() == pytest.approx([9, 10, 11])


def test_diebold_mariano_with_the_hln_correction() -> None:
    rng = np.random.default_rng(14)
    e1 = rng.normal(scale=1.0, size=60)
    e2 = rng.normal(scale=2.0, size=60)
    stat, p = stats.diebold_mariano(e1, e2)  # type: ignore[misc]
    d = e1**2 - e2**2
    n = d.size
    dm = d.mean() / math.sqrt(np.mean((d - d.mean()) ** 2) / n)
    assert stat == pytest.approx(dm * math.sqrt((n - 1) / n))
    assert p == pytest.approx(2 * sps.t.sf(abs(stat), n - 1))
    assert stat < 0 and p < 0.05
    assert stats.diebold_mariano(e1, e1) is None


def test_degenerate_input_is_skipped_with_a_reason() -> None:
    res = stats.adf_test([1.0, 1.0, 1.0])
    assert res.status == "skipped" and res.message_plain
    assert stats.ljung_box_test(np.ones(50)).status == "skipped"
