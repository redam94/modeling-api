"""Statistical test nodes against statsmodels/scipy reference values and known answers."""

from __future__ import annotations

import warnings

import numpy as np
import pytest
from mapi_spec.errors import MapiError
from mapi_spec.stattests import StatTestResult
from mapi_workflows.runtime.stattests import (
    acf_significance,
    chow_test,
    johansen_rank,
    phillips_perron,
    seasonal_strength,
)
from mapi_workflows.runtime.values import ModelValue
from wf_ctx import ar1, panel_ds, random_walk, run_node, series_ds

pytestmark = pytest.mark.filterwarnings("ignore")


def _result(out: object) -> StatTestResult:
    return StatTestResult.model_validate(out.outputs["result"])  # type: ignore[attr-defined]


# ===================================================================== unit-root tests


def test_adf_matches_statsmodels_and_decides() -> None:
    from statsmodels.tsa.stattools import adfuller

    for series, expected in ((random_walk(200), "unit_root"), (ar1(200), "stationary")):
        out = run_node("test.adf", {"alpha": 0.05}, data=series_ds(series))
        r = _result(out)
        ref = adfuller(series, regression="c", autolag="AIC")
        assert r.statistic == pytest.approx(ref[0], rel=1e-12)
        assert r.p_value == pytest.approx(ref[1], rel=1e-12)
        assert r.lags == ref[2] and r.n_obs == ref[3]
        assert r.critical_values["5%"] == pytest.approx(ref[4]["5%"])
        assert r.decision == expected
        assert out.result["reject"] is (expected == "stationary")
    ct = _result(
        run_node(
            "test.adf", {"regression": "ct", "autolag": "BIC"}, data=series_ds(random_walk(150, 3))
        )
    )
    ref = adfuller(random_walk(150, 3), regression="ct", autolag="BIC")
    assert ct.statistic == pytest.approx(ref[0], rel=1e-12)


def test_kpss_matches_statsmodels_with_its_reversed_null() -> None:
    from statsmodels.tsa.stattools import kpss

    rw = random_walk(200)
    r = _result(run_node("test.kpss", data=series_ds(rw)))
    ref = kpss(rw, regression="c", nlags="auto")
    assert r.statistic == pytest.approx(ref[0])
    assert r.p_value == pytest.approx(ref[1])
    assert r.reject and r.decision == "unit_root"  # KPSS rejects stationarity
    stable = _result(run_node("test.kpss", data=series_ds(ar1(200, 0.2))))
    assert not stable.reject and stable.decision == "stationary"


# Reference values from arch 7.x: PhillipsPerron(y, trend=..., test_type="tau")
_PP_REF = {
    ("rw", "n"): (-0.6077375943177323, 0.4513886585335713, 15),
    ("rw", "c"): (-1.1769721369976143, 0.6834224568245381, 15),
    ("rw", "ct"): (-1.8516038193888824, 0.6793601623351069, 15),
    ("ar", "c"): (-6.7799086873092005, 2.51484176033677e-09, 15),
    ("trendy", "ct"): (-2.4290784731905015, 0.3642775547194182, 14),
}
_PP_CRIT_RW_C = {"1%": -3.463645, "5%": -2.876176, "10%": -2.574572}


def _pp_series() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(42)
    e = rng.normal(size=200)
    ar = np.zeros(200)
    for t in range(1, 200):
        ar[t] = 0.5 * ar[t - 1] + e[t]
    trendy = np.cumsum(rng.normal(size=150)) + 0.3 * np.arange(150)
    return {"rw": np.cumsum(e), "ar": ar, "trendy": trendy}


def test_phillips_perron_matches_the_arch_package() -> None:
    data = _pp_series()
    for (name, trend), (stat, p, lags) in _PP_REF.items():
        s, pv, crit, used, _ = phillips_perron(data[name], trend=trend)
        assert s == pytest.approx(stat, rel=1e-9)
        assert pv == pytest.approx(p, rel=1e-7)
        assert used == lags
    _, _, crit, _, _ = phillips_perron(data["rw"], trend="c")
    for k, v in _PP_CRIT_RW_C.items():
        assert crit[k] == pytest.approx(v, abs=1e-6)
    s4, _p4, _, used4, _ = phillips_perron(data["ar"], trend="c", lags=4)
    assert (s4, used4) == (pytest.approx(-7.547453837334709, rel=1e-9), 4)
    node = _result(run_node("test.pp", {"trend": "c"}, data=series_ds(data["rw"])))
    assert node.statistic == pytest.approx(-1.1769721369976143, rel=1e-9)
    assert node.decision == "unit_root"


def test_zivot_andrews_finds_a_level_break() -> None:
    from statsmodels.tsa.stattools import zivot_andrews

    y = ar1(200, 0.3, seed=5)
    y[120:] += 8.0
    out = run_node("test.zivot_andrews", data=series_ds(y))
    r = _result(out)
    ref = zivot_andrews(y, trim=0.15, regression="c")
    assert r.statistic == pytest.approx(ref[0])
    assert r.p_value == pytest.approx(ref[1])
    assert r.decision == "stationary_with_break"
    assert abs(out.result["break_index"] - 120) <= 2
    assert str(out.result["break_at"]).startswith("2022")


# ====================================================================== cointegration


def _pair(seed: int = 1, tied: bool = True) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(size=300))
    y = 2.0 * x + rng.normal(size=300) if tied else np.cumsum(rng.normal(size=300))
    return y, x


def test_johansen_rank_matches_statsmodels() -> None:
    from statsmodels.tsa.vector_ar.vecm import coint_johansen, select_coint_rank

    y, x = _pair()
    ds = series_ds(y, extra={"x": x})
    out = run_node("test.johansen", {"vars": ["y", "x"]}, data=ds)
    ref = coint_johansen(np.column_stack([y, x]), 0, 1)
    assert out.result["trace_stats"] == pytest.approx(list(ref.lr1))
    assert out.result["rank"] == 1
    assert out.result["rank"] == select_coint_rank(np.column_stack([y, x]), 0, 1).rank
    r = _result(out)
    assert r.reject and r.decision == "cointegrated" and r.p_value is None
    y2, x2 = _pair(tied=False)
    none = run_node("test.johansen", {"vars": ["y", "x"]}, data=series_ds(y2, extra={"x": x2}))
    assert none.result["rank"] == 0 and none.result["decision"] == "not_cointegrated"
    assert johansen_rank([30.0, 10.0], [15.0, 3.8]) == 2


def test_engle_granger_matches_statsmodels() -> None:
    from statsmodels.tsa.stattools import coint

    y, x = _pair(seed=3)
    r = _result(
        run_node("test.engle_granger", {"y": "y", "x": "x"}, data=series_ds(y, extra={"x": x}))
    )
    ref = coint(y, x, trend="c")
    assert r.statistic == pytest.approx(ref[0])
    assert r.p_value == pytest.approx(ref[1])
    assert r.decision == "cointegrated"


# ========================================================================== residuals


def test_ljung_box_jarque_bera_arch_on_a_series() -> None:
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch
    from statsmodels.stats.stattools import jarque_bera

    y = ar1(300, 0.6, seed=9)
    ds = series_ds(y)
    lb = _result(run_node("test.ljung_box", {"lags": 10}, data=ds))
    ref = acorr_ljungbox(y, lags=list(range(1, 11)), return_df=True)
    assert lb.statistic == pytest.approx(float(ref["lb_stat"].iloc[-1]))
    assert lb.p_value == pytest.approx(float(ref["lb_pvalue"].iloc[-1]))
    assert lb.decision == "autocorrelated"
    jb = _result(run_node("test.jarque_bera", data=ds))
    ref_jb = jarque_bera(y)
    assert jb.statistic == pytest.approx(ref_jb[0])
    # scipy agrees on the statistic (an independent implementation)
    from scipy import stats as sps

    assert jb.statistic == pytest.approx(sps.jarque_bera(y).statistic)
    arch = _result(run_node("test.arch_lm", {"nlags": 4}, data=ds))
    assert arch.statistic == pytest.approx(het_arch(y, nlags=4)[0])
    noise = np.random.default_rng(0).normal(size=300)
    assert _result(run_node("test.ljung_box", data=series_ds(noise))).decision == "white_noise"


def _fitted_model() -> ModelValue:
    from mapi_workflows.runtime.models import fit_node
    from wf_ctx import node_ctx
    from wf_models import registry, weekly_series

    ctx = node_ctx(
        "model.fit",
        {"model": "ts.sarimax@^1", "settings": {"order": [1, 0, 0], "exog": ["price"]}},
        data=weekly_series(160, beta=3.0),
        registry=registry(),
    )
    out = fit_node(ctx)
    mv = out.outputs["model"]
    assert isinstance(mv, ModelValue)
    return mv


def test_regression_residual_tests_match_statsmodels_on_model_residuals() -> None:
    from mapi_workflows.runtime.models import model_residuals
    from statsmodels.stats.diagnostic import (
        breaks_cusumolsresid,
        het_breuschpagan,
        het_white,
    )
    from wf_models import registry

    reg = registry()
    mv = _fitted_model()
    res = model_residuals(mv, reg)
    assert res.exog_names == ["price"]
    assert len(res.resid) > 150
    x = np.column_stack([np.ones(len(res.resid)), res.exog])
    bp = _result(run_node("test.breusch_pagan", model=mv, registry=reg))
    assert bp.statistic == pytest.approx(het_breuschpagan(res.resid, x, robust=True)[0])
    wh = _result(run_node("test.white", model=mv, registry=reg))
    assert wh.statistic == pytest.approx(het_white(res.resid, x)[0])
    cu = run_node("test.cusum", model=mv, registry=reg)
    assert cu.result["statistic"] == pytest.approx(breaks_cusumolsresid(res.resid, ddof=2)[0])
    lb = _result(run_node("test.ljung_box", {"lags": 8}, model=mv, registry=reg))
    assert lb.details["model_df"] == 1  # the AR(1) term
    assert lb.decision == "white_noise"


# ============================================================================ granger


def test_granger_matches_statsmodels() -> None:
    from statsmodels.tsa.stattools import grangercausalitytests

    rng = np.random.default_rng(4)
    x = rng.normal(size=300)
    y = np.zeros(300)
    for t in range(2, 300):
        y[t] = 0.3 * y[t - 1] + 0.8 * x[t - 2] + rng.normal(scale=0.5)
    out = run_node(
        "test.granger", {"y": "y", "x": "x", "maxlag": 3}, data=series_ds(y, extra={"x": x})
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ref = grangercausalitytests(np.column_stack([y, x]), maxlag=3)
    for lag in (1, 2, 3):
        assert out.result["p_values"][str(lag)] == pytest.approx(ref[lag][0]["ssr_ftest"][1])
    assert out.result["best_lag"] in (2, 3)
    assert out.result["decision"] == "predictive"


# ============================================================================== ACF/PACF


def test_acf_pacf_suggests_the_ar_order() -> None:
    from statsmodels.tsa.stattools import acf, pacf

    rng = np.random.default_rng(8)
    e = rng.normal(size=600)
    y = np.zeros(600)
    for t in range(2, 600):
        y[t] = 0.5 * y[t - 1] + 0.3 * y[t - 2] + e[t]
    out = acf_significance(y, 20, 5)
    assert out["acf"] == pytest.approx(list(acf(y, nlags=20, fft=True)))
    assert out["pacf"] == pytest.approx(list(pacf(y, nlags=20, method="ywm")))
    assert out["suggested_p"] == 2
    assert out["suggested_q"] >= 3  # an AR process: the ACF decays slowly
    node = run_node("test.acf_pacf", {"nlags": 20, "max_order": 5}, data=series_ds(y))
    assert node.result["suggested_p"] == 2
    assert node.result["decision"] == "autocorrelated"


# ===================================================================== seasonality, VIF


def test_seasonal_strength_by_hand_and_by_node() -> None:
    from statsmodels.tsa.seasonal import STL

    n = 52 * 4
    rng = np.random.default_rng(2)
    seasonal = 10 * np.sin(2 * np.pi * np.arange(n) / 52) + rng.normal(size=n)
    fs, _ft = seasonal_strength(seasonal, 52)
    res = STL(seasonal, period=52, robust=True).fit()
    expect = max(0.0, 1 - np.var(res.resid) / np.var(res.seasonal + res.resid))
    assert fs == pytest.approx(expect)
    assert fs > 0.9
    out = run_node("test.seasonal_strength", data=series_ds(seasonal))
    assert out.result["period"] == 52  # from the weekly frequency
    assert out.result["seasonal"] is True and out.result["decision"] == "seasonal"
    flat = run_node("test.seasonal_strength", data=series_ds(rng.normal(size=n)))
    assert flat.result["seasonal"] is False
    with pytest.raises(MapiError):
        run_node("test.seasonal_strength", data=series_ds(seasonal[:60]))


def test_vif_matches_statsmodels() -> None:
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    rng = np.random.default_rng(6)
    a = rng.normal(size=200)
    b = a + rng.normal(scale=0.1, size=200)
    c = rng.normal(size=200)
    ds = series_ds(rng.normal(size=200), extra={"a": a, "b": b, "c": c})
    out = run_node("test.vif", {"vars": ["a", "b", "c"]}, data=ds)
    exog = np.column_stack([np.ones(200), a, b, c])
    for i, n in enumerate(["a", "b", "c"]):
        assert out.result["table"][n] == pytest.approx(variance_inflation_factor(exog, i + 1))
    # and against the definition 1 / (1 - R^2)
    beta, *_ = np.linalg.lstsq(np.column_stack([np.ones(200), b, c]), a, rcond=None)
    r2 = 1 - np.sum((a - np.column_stack([np.ones(200), b, c]) @ beta) ** 2) / np.sum(
        (a - a.mean()) ** 2
    )
    assert out.result["table"]["a"] == pytest.approx(1 / (1 - r2))
    assert sorted(out.result["high_vif_vars"]) == ["a", "b"]
    assert out.result["decision"] == "collinear"


# ================================================================================ chow


def test_chow_matches_the_textbook_f_test() -> None:
    import statsmodels.api as sm
    from scipy import stats as sps

    rng = np.random.default_rng(7)
    x = rng.normal(size=120)
    y = 1 + 2 * x + rng.normal(scale=0.5, size=120)
    y[60:] = 3 + 0.5 * x[60:] + rng.normal(scale=0.5, size=60)
    f, p, df1, df2 = chow_test(y, x[:, None], 60)
    X = sm.add_constant(x)
    ssr_p = sm.OLS(y, X).fit().ssr
    ssr_s = sm.OLS(y[:60], X[:60]).fit().ssr + sm.OLS(y[60:], X[60:]).fit().ssr
    expect = ((ssr_p - ssr_s) / 2) / (ssr_s / (120 - 4))
    assert f == pytest.approx(expect)
    assert p == pytest.approx(sps.f.sf(expect, 2, 116))
    assert (df1, df2) == (2, 116)
    ds = series_ds(y, extra={"x": x})
    at = str(ds["time"].values[60])[:10]
    out = run_node("test.chow", {"y": "y", "x": ["x"], "break_at": at}, data=ds)
    assert out.result["break_index"] == 60
    assert out.result["decision"] == "break"
    same = run_node(
        "test.chow",
        {"y": "y", "x": ["x"], "break_at": 30},
        data=series_ds(1 + 2 * x + rng.normal(size=120), extra={"x": x}),
    )
    assert same.result["decision"] == "no_break"


# ================================================================================ panels


def test_panels_are_tested_per_series() -> None:
    values = np.vstack([random_walk(150, 1), random_walk(150, 4), ar1(150, 0.2, 3)])
    ds = panel_ds(values, ["DE", "FR", "IT"])
    out = run_node("test.adf", data=ds)
    r = _result(out)
    assert len(r.per_series) == 3
    assert [s.coords for s in r.per_series] == [{"geo": "DE"}, {"geo": "FR"}, {"geo": "IT"}]
    assert [s.decision for s in r.per_series] == ["unit_root", "unit_root", "stationary"]
    assert r.reject_share == pytest.approx(1 / 3)
    assert r.decision == "unit_root"  # most series wander
    assert "1 of 3 series" in r.verdict_plain
    total = _result(run_node("test.adf", {"per_series": False}, data=ds))
    assert total.per_series == []


def test_short_series_fail_with_a_plain_message() -> None:
    with pytest.raises(MapiError) as exc:
        run_node("test.adf", data=series_ds(np.arange(8.0)))
    assert "at least 12 observations" in exc.value.detail
