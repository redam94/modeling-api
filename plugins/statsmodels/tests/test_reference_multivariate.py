"""Reference values: VAR, VECM, VARMAX, DynamicFactor and Markov switching against direct
statsmodels calls and published results.

The VAR(2) on US macro growth reproduces the coefficient table of the statsmodels VAR
documentation (Lutkepohl-style example on ``macrodata``: realgdp, realcons, realinv).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plugin_statsmodels.datasets import (
    GROWTH_SCHEMA,
    MACRO_SCHEMA,
    MACRO_SERIES,
    macro_dataset,
)
from mapi_plugin_statsmodels.markov import mixture_quantiles
from mapi_plugin_statsmodels.multivariate import fevd_from_orth
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_spec import load_score_request
from mapi_spec.schema import DataSchema
from scipy import optimize, stats

MACRO_BIND = "{dims: {t: quarter, variable: series}, vars: {y: value}}"


def spec(model: str, settings: str, bind: str = MACRO_BIND) -> str:
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: ref, project: tests}}
data: {{dataset: "dataset://tests/ref@latest", bind: {bind}}}
model: {{use: "{model}@^1", settings: {settings}, fit: {{seed: 1}}}}
"""


def fit(
    registry: Registry,
    model: str,
    settings: str,
    data: xr.Dataset,
    schema: DataSchema,
    bind: str = MACRO_BIND,
) -> Bundle:
    return fit_spec(spec(model, settings, bind), data, registry=registry, raw_schema=schema)


def forecast(
    registry: Registry, bundle: Bundle, periods: int, payload: xr.Dataset | None = None
) -> xr.DataArray:
    req = load_score_request(
        "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://tests/ref\n"
        f"mode: forecast\nhorizon: {{dim: t, periods: {periods}}}\n"
    )
    tree = score_bundle(bundle, req, payload, registry=registry, return_full=True)
    node = tree["predictions"]
    assert isinstance(node, xr.DataTree)
    return node.to_dataset()["y_hat"]


def frame(ds: xr.Dataset) -> pd.DataFrame:
    return pd.DataFrame(
        np.asarray(ds["value"].transpose("quarter", "series").values), columns=list(MACRO_SERIES)
    )


@pytest.fixture(scope="module")
def growth_log() -> xr.Dataset:
    """Log differences (not in percent), as in the statsmodels VAR documentation."""
    return macro_dataset().diff("quarter")


# ===================================================================================== VAR

# statsmodels documentation, "Vector Autoregressions tsa.vector_ar": VAR(2) on
# log-differenced realgdp, realcons, realinv; equation realgdp.
PUBLISHED_VAR2_REALGDP = {
    "const": (0.001527, 0.001119),
    "L1.realgdp": (-0.279435, 0.169663),
    "L1.realcons": (0.675016, 0.131285),
    "L1.realinv": (0.033219, 0.026194),
    "L2.realgdp": (0.008221, 0.173522),
    "L2.realcons": (0.290458, 0.145904),
    "L2.realinv": (-0.007321, 0.025786),
}


def test_var2_reproduces_the_published_coefficients(
    registry: Registry, growth_log: xr.Dataset
) -> None:
    schema = MACRO_SCHEMA.model_copy()
    b = fit(registry, "ts.var", "{lags: 2}", growth_log, schema)
    params = b.model.groups["params"]
    for term, (coef, se) in PUBLISHED_VAR2_REALGDP.items():
        row = params.sel(param=f"{term}.realgdp")
        assert float(row["estimate"]) == pytest.approx(coef, abs=5e-7), term
        assert float(row["std_err"]) == pytest.approx(se, abs=5e-7), term
    assert b.model.learned["cells"][0]["lags"] == 2


def test_var_matches_statsmodels(registry: Registry, growth_log: xr.Dataset) -> None:
    from statsmodels.tsa.api import VAR

    b = fit(registry, "ts.var", "{max_lags: 4, ic: aic, irf_horizon: 8}", growth_log, MACRO_SCHEMA)
    direct = VAR(frame(growth_log)).fit(maxlags=4, ic="aic", trend="c")
    assert b.model.learned["cells"][0]["lags"] == direct.k_ar
    fc = forecast(registry, b, 5)
    y = frame(growth_log).to_numpy()
    mid, lo, hi = direct.forecast_interval(y[-direct.k_ar :], 5, alpha=0.1)
    np.testing.assert_allclose(
        fc.sel(quantile=0.5).transpose("quarter", "series").values, mid, rtol=1e-8
    )
    np.testing.assert_allclose(
        fc.sel(quantile=0.05).transpose("quarter", "series").values, lo, rtol=1e-8
    )
    np.testing.assert_allclose(
        fc.sel(quantile=0.95).transpose("quarter", "series").values, hi, rtol=1e-8
    )

    irf = direct.irf(8)
    got = b.model.groups["irf"]["irf"].transpose("horizon", "response", "shock").values
    np.testing.assert_allclose(got, irf.orth_irfs, rtol=1e-8, atol=1e-14)
    se = irf.stderr(orth=True)
    upper = b.model.groups["irf"]["irf_upper"].transpose("horizon", "response", "shock").values
    np.testing.assert_allclose(
        upper, irf.orth_irfs + stats.norm.ppf(0.975) * se, rtol=1e-8, atol=1e-14
    )

    fevd = b.model.groups["fevd"]["share"].transpose("variable", "horizon", "shock").values
    np.testing.assert_allclose(fevd, direct.fevd(8).decomp, rtol=1e-8, atol=1e-12)

    granger = b.model.groups["granger"]
    for caused in MACRO_SERIES:
        for causing in MACRO_SERIES:
            if caused == causing:
                continue
            ref = direct.test_causality(caused, [causing], kind="f")
            got_p = float(granger["p_value"].sel(caused=caused, causing=causing))
            assert got_p == pytest.approx(float(ref.pvalue), rel=1e-9)


def test_fevd_from_orthogonalised_responses_matches_var_fevd(growth_log: xr.Dataset) -> None:
    from statsmodels.tsa.api import VAR

    res = VAR(frame(growth_log)).fit(3)
    np.testing.assert_allclose(
        fevd_from_orth(res.irf(10).orth_irfs, 10), res.fevd(10).decomp, rtol=1e-10
    )


# ==================================================================================== VECM


def test_vecm_matches_statsmodels(registry: Registry) -> None:
    from statsmodels.tsa.vector_ar.vecm import VECM, select_coint_rank

    levels = macro_dataset()
    b = fit(registry, "ts.vecm", "{k_ar_diff: 2, deterministic: ci}", levels, MACRO_SCHEMA)
    y = frame(levels)
    test = select_coint_rank(y, det_order=0, k_ar_diff=2, method="trace", signif=0.05)
    assert test.rank == 1
    coint = b.model.groups["cointegration"]
    np.testing.assert_allclose(coint["statistic"].values, test.test_stats, rtol=1e-10)
    np.testing.assert_allclose(coint["critical_value"].values, test.crit_vals, rtol=1e-10)
    direct = VECM(y, k_ar_diff=2, coint_rank=1, deterministic="ci").fit()
    np.testing.assert_allclose(coint["alpha"].values, direct.alpha, rtol=1e-8)
    np.testing.assert_allclose(coint["beta"].values, direct.beta, rtol=1e-8)
    params = b.model.groups["params"]
    for i, v in enumerate(MACRO_SERIES):
        row = params.sel(param=f"alpha.ec1.{v}")
        assert float(row["estimate"]) == pytest.approx(float(direct.alpha[i, 0]), rel=1e-8)
        assert float(row["p_value"]) == pytest.approx(float(direct.pvalues_alpha[i, 0]), rel=1e-6)

    fc = forecast(registry, b, 6)
    point, lower, upper = direct.predict(steps=6, alpha=0.1)
    np.testing.assert_allclose(
        fc.sel(quantile=0.5).transpose("quarter", "series").values, point, rtol=1e-8
    )
    np.testing.assert_allclose(
        fc.sel(quantile=0.05).transpose("quarter", "series").values, lower, rtol=1e-8
    )
    np.testing.assert_allclose(
        fc.sel(quantile=0.95).transpose("quarter", "series").values, upper, rtol=1e-8
    )

    irf = b.model.groups["irf"]["irf"].transpose("horizon", "response", "shock").values
    np.testing.assert_allclose(irf, direct.irf(12).orth_irfs, rtol=1e-8, atol=1e-14)
    g = direct.test_granger_causality(caused=0, causing=2)
    got = float(b.model.groups["granger"]["p_value"].sel(caused="realgdp", causing="realinv"))
    assert got == pytest.approx(float(g.pvalue), rel=1e-9)


def test_vecm_rank_zero_is_a_plain_settings_error(registry: Registry) -> None:
    from mapi_spec.errors import ErrorCode, MapiError

    rng = np.random.default_rng(0)
    walks = np.cumsum(rng.normal(size=(3, 120)), axis=1)  # independent random walks
    date = pd.date_range("1990-01-01", periods=120, freq="QS-JAN").as_unit("ns")
    ds = xr.Dataset(
        {"value": (("series", "quarter"), walks)},
        coords={"series": list(MACRO_SERIES), "quarter": date},
    )
    with pytest.raises(MapiError) as info:
        fit(registry, "ts.vecm", "{k_ar_diff: 1}", ds, MACRO_SCHEMA)
    assert info.value.code is ErrorCode.SETTINGS_INVALID
    assert info.value.pointer == "/model/settings/coint_rank"
    assert "ts.var" in (info.value.untrusted_text or "")  # plugin text is untrusted


# ================================================================== VARMAX / DynamicFactor


def test_varmax_matches_statsmodels(registry: Registry) -> None:
    from statsmodels.tsa.statespace.varmax import VARMAX

    g = macro_dataset(quarters=120).diff("quarter") * 100
    g = g.sel(series=["realgdp", "realcons"])
    b = fit(registry, "ts.varmax", "{order: [1, 0]}", g, GROWTH_SCHEMA)
    y = pd.DataFrame(
        np.asarray(g["value"].transpose("quarter", "series").values),
        columns=["realgdp", "realcons"],
    )
    direct = VARMAX(y, order=(1, 0), trend="c").fit(disp=False, maxiter=200, method="lbfgs")
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.model.param_names))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=1e-6)
    fc = forecast(registry, b, 4)
    ref = direct.get_forecast(4)
    np.testing.assert_allclose(
        fc.sel(quantile=0.5).transpose("quarter", "series").values,
        np.asarray(ref.predicted_mean),
        rtol=1e-6,
    )
    ci = np.asarray(ref.conf_int(alpha=0.1))  # lower cols then upper cols
    np.testing.assert_allclose(
        fc.sel(quantile=0.05).transpose("quarter", "series").values, ci[:, :2], rtol=1e-6
    )


def test_dynamic_factor_matches_statsmodels(registry: Registry) -> None:
    from statsmodels.tsa.statespace.dynamic_factor import DynamicFactor

    g = macro_dataset(quarters=120).diff("quarter") * 100
    b = fit(registry, "ts.dynamic_factor", "{k_factors: 1, factor_order: 1}", g, GROWTH_SCHEMA)
    y = frame(g)
    direct = DynamicFactor(y, k_factors=1, factor_order=1).fit(
        disp=False, maxiter=200, method="lbfgs"
    )
    est = b.model.groups["params"]["estimate"].sel(param=list(direct.model.param_names))
    np.testing.assert_allclose(est.values, np.asarray(direct.params), rtol=1e-6)
    fc = forecast(registry, b, 4)
    np.testing.assert_allclose(
        fc.sel(quantile=0.5).transpose("quarter", "series").values,
        np.asarray(direct.get_forecast(4).predicted_mean),
        rtol=1e-6,
    )
    factors = b.model.groups["factors"]["factor"].sel(factor_name="f1").values
    np.testing.assert_allclose(factors, np.asarray(direct.factors.smoothed).reshape(-1), rtol=1e-6)


# ================================================================================== Markov

REGIME_BIND = "{dims: {t: week}, vars: {y: demand}}"
REGIME_RAW = DataSchema.model_validate(
    {
        "dims": {"week": {"kind": "integer", "policy": "extend"}},
        "vars": {"demand": {"dims": ["week"], "dtype": "float64", "role": "target"}},
    }
)


@pytest.fixture(scope="module")
def regimes() -> xr.Dataset:
    rng = np.random.default_rng(1)
    n = 150
    s = np.zeros(n, int)
    for t in range(1, n):
        s[t] = s[t - 1] if rng.uniform() < 0.9 else 1 - s[t - 1]
    y = np.where(s == 0, 1.0, 4.0) + rng.normal(size=n) * 0.7
    return xr.Dataset({"demand": (("week",), y)}, coords={"week": np.arange(n)})


def test_markov_regression_matches_statsmodels(registry: Registry, regimes: xr.Dataset) -> None:
    from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression

    b = fit(
        registry,
        "ts.markov_switching",
        "{k_regimes: 2, time_kind: integer}",
        regimes,
        REGIME_RAW,
        REGIME_BIND,
    )
    y = pd.Series(np.asarray(regimes["demand"].values), name="y")
    direct = MarkovRegression(y, k_regimes=2, trend="c", switching_variance=True).fit(disp=False)
    assert float(b.model.groups["fit_stats"]["llf"]) == pytest.approx(float(direct.llf), rel=1e-6)
    got = b.model.groups["params"]["estimate"].sel(param=["const[0]", "const[1]"]).values
    np.testing.assert_allclose(
        np.sort(got), np.sort(np.asarray(direct.params[["const[0]", "const[1]"]])), rtol=1e-4
    )
    probs = b.model.groups["regimes"]["smoothed_prob"].values
    ref = np.asarray(direct.smoothed_marginal_probabilities)
    theirs = np.argsort(np.asarray(direct.params[["const[0]", "const[1]"]]))
    np.testing.assert_allclose(probs[:, np.argsort(got)], ref[:, theirs], atol=1e-3)


def test_markov_forecast_is_the_propagated_regime_mixture(
    registry: Registry, regimes: xr.Dataset
) -> None:
    b = fit(
        registry,
        "ts.markov_switching",
        "{k_regimes: 2, time_kind: integer}",
        regimes,
        REGIME_RAW,
        REGIME_BIND,
    )
    fc = forecast(registry, b, 30)
    p = b.model.groups["params"]["estimate"]
    trans = b.model.groups["regimes"]["transition"].values  # [from, to]
    probs = b.model.groups["regimes"]["filtered_prob"].values[-1]
    const = p.sel(param=["const[0]", "const[1]"]).values
    for _ in range(30):
        probs = probs @ trans
    # far ahead the median approaches the stationary mixture's median
    sd = np.sqrt(p.sel(param=["sigma2[0]", "sigma2[1]"]).values)
    target = optimize.brentq(
        lambda x: (probs * stats.norm.cdf((x - const) / sd)).sum() - 0.5, -50, 50
    )
    assert float(fc.sel(quantile=0.5).values[-1]) == pytest.approx(target, abs=1e-6)


def test_markov_autoregression_forecasts_are_seeded(
    registry: Registry, regimes: xr.Dataset
) -> None:
    b = fit(
        registry,
        "ts.markov_switching",
        "{k_regimes: 2, order: 1, time_kind: integer}",
        regimes,
        REGIME_RAW,
        REGIME_BIND,
    )
    a = forecast(registry, b, 5)
    c = forecast(registry, b, 5)
    xr.testing.assert_identical(a, c)
    assert bool(np.isfinite(a).all()) and bool((a.diff("quantile") >= 0).all())


def test_mixture_quantiles_match_root_finding() -> None:
    mu = np.array([[0.0, 1.0], [3.0, -2.0]])
    w = np.array([[0.3, 0.9], [0.7, 0.1]])
    sd = np.array([[1.0, 0.5], [2.0, 1.5]])
    q = np.array([0.05, 0.5, 0.95])
    got = mixture_quantiles(mu, w, sd, q)
    for j in range(2):
        for i, level in enumerate(q):
            ref = optimize.brentq(
                lambda x, j=j, level=level: (
                    (w[:, j] * stats.norm.cdf((x - mu[:, j]) / sd[:, j])).sum() - level
                ),
                -50,
                50,
            )
            assert got[i, j] == pytest.approx(ref, abs=1e-8)
