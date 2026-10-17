"""Statistical properties of the governance checks (M3.5 review): residual whiteness on
diffuse-start models, the size and power of the permutation test, placebo channels of
sign-constrained and calibrated models, prior sensitivity of the default MMM and the
multiplicity of the noise-regressor p-values."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from gov_fixtures import fit
from mapi_runner.worker.governance import GovernanceEnv, measure
from mapi_runner.worker.refit import Refitter
from mapi_sdk.registry import default_registry
from mapi_spec.governance import CheckStatus, PolicyCheck, RefitBudget
from mapi_spec.schema import DataSchema

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

TS_SCHEMA = DataSchema.model_validate(
    {
        "dims": {"time": {"kind": "time", "policy": "extend", "freq": "W-SUN"}},
        "vars": {"y": {"dims": ["time"], "dtype": "float64", "role": "target"}},
    }
)


def ts_data(y: np.ndarray) -> xr.Dataset:
    t = pd.date_range("2020-01-05", periods=len(y), freq="W-SUN")
    return xr.Dataset({"y": ("time", np.asarray(y, float))}, coords={"time": t})


def ts_spec(use: str, settings: dict[str, Any], *, holdout: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "dataset": "dataset://p/sales@latest",
        "bind": {"dims": {"t": "time"}},
    }
    if holdout:
        data["split"] = {"kind": "time_holdout", "dim": "t", "last": holdout}
    return {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {"name": "ts", "project": "p"},
        "data": data,
        "model": {"use": use, "settings": settings},
        "evaluate": {"metrics": ["rmse", "mae"]},
    }


def ts_env(y: np.ndarray, use: str, settings: dict[str, Any], **kw: Any) -> GovernanceEnv:
    reg = default_registry()
    data = ts_data(y)
    b = fit(ts_spec(use, settings, **kw), data, reg, schema=TS_SCHEMA)
    return GovernanceEnv(b, data, reg, Refitter(b, reg, RefitBudget(max_refits=200)))


def ar1(rng: np.random.Generator, n: int, phi: float, scale: float = 1.0) -> np.ndarray:
    e = rng.normal(scale=scale, size=n)
    out = np.zeros(n)
    for t in range(1, n):
        out[t] = phi * out[t - 1] + e[t]
    return out


# ================================================================ residuals.whiteness


def test_whiteness_drops_the_diffuse_start_of_unobserved_components() -> None:
    """Regression: the first one-step prediction of a diffuse-initialised model is 0, so the
    first residual was the level (about 500); that one outlier swamped every
    autocorrelation and strongly autocorrelated errors passed with p = 1.00."""
    rng = np.random.default_rng(0)
    n = 156
    y = 500 + 0.5 * np.arange(n) + ar1(rng, n, 0.8)
    env = ts_env(y, "ts.unobserved_components@^1", {"level": "deterministic trend"})
    r = measure(env, PolicyCheck(check="residuals.whiteness"))
    assert r.status is CheckStatus.FAIL, (r.status, r.value, r.message_plain)
    assert r.value is not None and r.value < 0.01
    # white errors around the same trend still pass
    white = 500 + 0.5 * np.arange(n) + rng.normal(size=n)
    ok = measure(
        ts_env(white, "ts.unobserved_components@^1", {"level": "deterministic trend"}),
        PolicyCheck(check="residuals.whiteness"),
    )
    assert ok.status is CheckStatus.PASS, (ok.value, ok.message_plain)


# ============================================================ null.permutation_target


def test_permuted_increments_keep_the_integration_order() -> None:
    from mapi_runner.worker.governance import _permute_increments

    rng = np.random.default_rng(0)
    x = np.cumsum(rng.normal(size=(50, 3)), axis=0) + 100
    out = _permute_increments(x, 0, 1, np.random.default_rng(1))
    assert out.shape == x.shape
    assert np.allclose(out[0], x[0])  # integrated from the original start
    # the same increments, reordered (one permutation for every series)
    assert np.allclose(np.sort(np.diff(out, axis=0), axis=0), np.sort(np.diff(x, axis=0), axis=0))
    assert not np.allclose(out, x)
    lv = _permute_increments(x, 0, 0, np.random.default_rng(1))
    assert np.allclose(np.sort(lv, axis=0), np.sort(x, axis=0))
    two = _permute_increments(x, 0, 2, np.random.default_rng(1))
    assert np.allclose(two[:2], x[:2])


def _skill(y: np.ndarray, fc: np.ndarray, h: int) -> float:
    naive = np.full(h, y[-h - 1])
    return 1.0 - float(np.sqrt(np.mean((y[-h:] - fc) ** 2))) / float(
        np.sqrt(np.mean((y[-h:] - naive) ** 2))
    )


@pytest.mark.slow
def test_permutation_design_has_its_size_on_random_walks_and_power_on_dynamics() -> None:
    """Simulation of the check's design (statistic and null exactly as implemented):
    ARIMA(1,1,0) refits, 12 held-out periods, K = 19 shuffles of the first differences.
    The old design (block-shuffled levels, raw holdout RMSE) rejected 35% of random walks
    and 33% of AR alternatives; the new one keeps its size on random walks and rejects
    more often on real dynamics."""
    from mapi_runner.worker.governance import _permute_increments
    from statsmodels.tsa.arima.model import ARIMA

    def skill_of(y: np.ndarray, h: int = 12) -> float:
        res = ARIMA(y[:-h], order=(1, 1, 0)).fit()
        return _skill(y, np.asarray(res.forecast(h)), h)

    def p_value(y: np.ndarray, rng: np.random.Generator, k: int = 19) -> float:
        real = skill_of(y)
        perms = [skill_of(_permute_increments(y, 0, 1, rng)) for _ in range(k)]
        return (1 + sum(s >= real for s in perms)) / (k + 1)

    reps, n = 30, 120
    size = power = 0
    for seed in range(reps):
        rng = np.random.default_rng(seed)
        e = rng.normal(size=n)
        size += p_value(100 + np.cumsum(e), rng) <= 0.05
        dy = np.zeros(n)
        for t in range(1, n):
            dy[t] = 0.8 * dy[t - 1] + e[t]
        power += p_value(100 + np.cumsum(dy), rng) <= 0.05
    assert size / reps <= 0.12, size / reps
    assert power > size and power / reps >= 0.25, (power / reps, size / reps)


@pytest.mark.slow
def test_permutation_check_on_statsmodels_and_on_drivers_end_to_end() -> None:
    """The real check through refits: an AR process in differences passes where a random
    walk does not; a regression whose drivers carry signal passes, and one whose result
    only follows a trend (drivers unrelated) does not - the drivers are shuffled, not
    the result, so the trend alone cannot pass."""
    from gov_fixtures import SCHEMA, linear_spec

    reg = default_registry()
    params = {"n_permutations": 19}
    rng = np.random.default_rng(0)
    n = 120
    e = rng.normal(size=n)
    ar = np.zeros(n)
    for t in range(1, n):
        ar[t] = 0.7 * ar[t - 1] + e[t]
    dyn = ts_env(100 + ar, "ts.arima@^1", {"order": [1, 0, 0]}, holdout=12)
    r = measure(dyn, PolicyCheck(check="null.permutation_target", params=params))
    assert r.status is CheckStatus.PASS, (r.value, r.message_plain)
    assert "values shuffled" in r.message_plain
    white = ts_env(100 + rng.normal(size=n), "ts.arima@^1", {"order": [1, 0, 0]}, holdout=12)
    r = measure(white, PolicyCheck(check="null.permutation_target", params=params))
    assert r.status is not CheckStatus.PASS, (r.value, r.message_plain)
    walk = ts_env(
        100 + np.cumsum(rng.normal(size=n)), "ts.arima@^1", {"order": [1, 1, 0]}, holdout=12
    )
    r = measure(walk, PolicyCheck(check="null.permutation_target", params=params))
    assert r.status is not CheckStatus.PASS, (r.value, r.message_plain)

    t = pd.date_range("2022-01-02", periods=104, freq="W-SUN")
    spend = np.abs(rng.normal(size=104)) * 10
    price = rng.normal(size=104)
    trend = 10 + 0.3 * np.arange(104)

    def env_of(y: np.ndarray) -> GovernanceEnv:
        data = xr.Dataset(
            {"y": ("time", y), "spend": ("time", spend), "price": ("time", price)},
            coords={"time": t},
        )
        b = fit(linear_spec(), data, reg, schema=SCHEMA)
        return GovernanceEnv(b, data, reg, Refitter(b, reg, RefitBudget(max_refits=200)))

    good = env_of(trend + 1.5 * spend - 0.8 * price + rng.normal(scale=0.5, size=104))
    r = measure(good, PolicyCheck(check="null.permutation_target", params=params))
    assert r.status is CheckStatus.PASS, (r.value, r.message_plain)
    assert "drivers" in r.message_plain
    only_trend = env_of(trend + rng.normal(scale=0.5, size=104))
    r = measure(only_trend, PolicyCheck(check="null.permutation_target", params=params))
    assert r.status is not CheckStatus.PASS, (r.value, r.message_plain)


# ============================================================== null.noise_regressors


def test_placebo_channels_get_the_default_prior_not_the_source_channels() -> None:
    """Regression: per-channel priors (business priors, calibrated ROI) were copied to the
    placebo channels, so pure noise got a tight informative ROI prior and a calibrated MMM
    failed because of the prior, not the data. Structural settings are still copied."""
    from mapi_runner.worker.governance import _walk_settings

    settings = {
        "priors": {"roi": {"tv": {"mu": 1.0986, "sigma": 0.1}, "search": {"mu": 0, "sigma": 1}}},
        "calibration": {"lift_tests": {"tv": {"roi": 2.0}, "search": {"roi": 1.0}}},
        "channels": {"tv": {"adstock": {"kind": "geometric", "l_max": 8}}, "search": {}},
        "media": ["tv", "search"],
    }
    out = _walk_settings(settings, {"tv", "search"}, ["noise_1", "noise_2"], "tv")
    assert set(out["priors"]["roi"]) == {"tv", "search"}
    assert set(out["calibration"]["lift_tests"]) == {"tv", "search"}
    assert out["channels"]["noise_1"] == {"adstock": {"kind": "geometric", "l_max": 8}}
    assert out["media"] == ["tv", "search", "noise_1", "noise_2"]


def test_noise_p_values_are_corrected_for_the_number_of_placebos() -> None:
    """Regression: each placebo's p-value was compared with 0.05 on its own, so with three
    placebos a good model was flagged 14% of the time under the null. Holm's correction
    keeps the family-wise rate at 5%."""
    from mapi_runner.worker.governance import _holm_significant

    assert _holm_significant({"noise_1": 0.03, "noise_2": 0.2, "noise_3": 0.5}) == []
    assert _holm_significant({"noise_1": 0.01, "noise_2": 0.2, "noise_3": 0.5}) == ["noise_1"]
    assert _holm_significant({"a": 0.01, "b": 0.02, "c": 0.04}) == ["a", "b", "c"]
    assert _holm_significant({}) == []
    rng = np.random.default_rng(0)
    flagged = sum(
        bool(_holm_significant({f"n{i}": float(rng.uniform()) for i in range(3)}))
        for _ in range(4000)
    )
    assert flagged / 4000 <= 0.06


def _positive_channel_registry() -> Any:
    from typing import ClassVar

    import pymc as pm
    from mapi_pymc.context import BuildContext
    from mapi_pymc.model import PyMCModel
    from mapi_sdk.models import ModelSettings

    class PosSettings(ModelSettings):
        beta_sigma: float = 1.0

    class PositiveChannels(PyMCModel[PosSettings]):
        """``y ~ Normal(a + sum_c beta_c spend_c, sigma)`` with ``beta_c >= 0`` (as an MMM's
        channel effects: every posterior draw of an effect is positive)."""

        id = "test.positive_channels"
        version = "1.0.0"
        Settings = PosSettings
        schema_template: ClassVar[dict[str, Any]] = {
            "template": {
                "dims": {
                    "time": {"kind": "time", "policy": "extend"},
                    "channel": {"kind": "categorical"},
                },
                "vars": {
                    "y": {"dims": ["time"], "dtype": "float64", "role": "target"},
                    "spend": {"dims": ["time", "channel"], "dtype": "float64", "role": "spend"},
                },
            }
        }
        outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["time"], "units_of": "y"}}
        expected: ClassVar[dict[str, str]] = {"y": "mu"}
        pad_extend = False

        def build(self, ds: xr.Dataset, s: PosSettings, ctx: BuildContext) -> pm.Model:
            with pm.Model(coords=ctx.coords()) as model:
                a = pm.Normal("a", 0.0, 20.0)
                beta = pm.HalfNormal("beta", s.beta_sigma, dims="channel")
                sigma = pm.HalfNormal("sigma", 2.0)
                media = (ctx.data("spend") * beta[None, :]).sum(axis=1)
                mu = pm.Deterministic("mu", a + media, dims="time")
                pm.Normal("y", mu, sigma, observed=ctx.observed("y", dims=["time"]), dims="time")
            return model

    reg = default_registry().copy()  # never leak test models into the shared registry
    reg.register_model(PositiveChannels)
    return reg


@pytest.mark.slow
def test_placebo_channels_of_a_sign_constrained_bayesian_model_can_pass() -> None:
    """Regression: every posterior draw of a positive-only effect is above 0, so a placebo
    channel's 90% interval never covered 0 and the check could never pass for a Bayesian
    MMM ("clearly not zero" at a 0.5% share). The Bayesian rule is now practical: is the
    placebo's share of the real channels' effect above the warn threshold with high
    posterior probability?"""
    rng = np.random.default_rng(0)
    n = 104
    t = pd.date_range("2022-01-02", periods=n, freq="W-SUN")
    spend = np.abs(rng.normal(size=(n, 2))) * 10
    y = 50 + spend @ np.array([1.0, 0.5]) + rng.normal(scale=1.0, size=n)
    data = xr.Dataset(
        {"y": ("time", y), "spend": (("time", "channel"), spend)},
        coords={"time": t, "channel": ["tv", "search"]},
    )
    schema = DataSchema.model_validate(
        {
            "dims": {
                "time": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                "channel": {"kind": "categorical"},
            },
            "vars": {
                "y": {"dims": ["time"], "dtype": "float64", "role": "target"},
                "spend": {"dims": ["time", "channel"], "dtype": "float64", "role": "spend"},
            },
        }
    )
    reg = _positive_channel_registry()
    spec = {
        "apiVersion": "mapi/v1",
        "kind": "ModelSpec",
        "metadata": {"name": "pos", "project": "p"},
        "data": {"dataset": "dataset://p/sales@latest"},
        "model": {
            "use": "test.positive_channels@^1",
            "settings": {"beta_sigma": 1.0},
            "fit": {"seed": 1, "draws": 100, "tune": 100, "chains": 2, "engine": "pymc"},
        },
    }
    b = fit(spec, data, reg, schema)
    env = GovernanceEnv(
        b, data, reg, Refitter(b, reg, RefitBudget(max_refits=5, draws=100, tune=100, chains=2))
    )
    assert env.kind == "bayesian"
    r = measure(env, PolicyCheck(check="null.noise_regressors", params={"kind": "gaussian"}))
    assert r.status is CheckStatus.PASS, (r.value, r.message_plain)
    assert "clearly not zero" not in r.message_plain
    probs = next(e for e in r.evidence if e.name == "noise_share_prob_material").inline
    assert all(v < 0.9 for v in probs.values())  # type: ignore[union-attr]


# ================================================================== sensitivity.prior


def test_prior_sensitivity_sees_the_default_mmm_priors() -> None:
    """Regression: a default mmm.bayesian spec has no ``priors`` block, so the check was
    always skipped ("the model's priors are not set in its settings"); the scale-key rule
    also missed ``seasonality.prior_sigma``/``trend.growth_prior_sigma`` and multiplied
    Beta/Gamma ``beta``/``b`` parameters (a location shift, not a width). The plugin's
    defaults (and its declared ROI default) are now materialised before scaling, and
    business priors translated at fit time are included."""
    from mapi_plugin_mmm.bayesian import MMMBayesian
    from mapi_runner.worker.governance import _prior_settings, _scale_priors

    base = _prior_settings(MMMBayesian, {"channels": {}}, {})
    scaled, found = _scale_priors(base, 2.0)
    assert found >= 4
    assert scaled["priors"]["roi_default"]["sigma"] == pytest.approx(
        2 * base["priors"]["roi_default"]["sigma"]
    )
    assert scaled["priors"]["roi_default"]["mu"] == base["priors"]["roi_default"]["mu"]
    assert scaled["seasonality"]["prior_sigma"] == pytest.approx(
        2 * base["seasonality"]["prior_sigma"]
    )
    assert scaled["trend"]["growth_prior_sigma"] == pytest.approx(
        2 * base["trend"]["growth_prior_sigma"]
    )
    # Beta(a, b) / Gamma rate parameters are not widths
    beta_like, n = _scale_priors({"priors": {"share": {"a": 2.0, "b": 5.0, "beta": 1.0}}}, 2.0)
    assert n == 0 and beta_like["priors"]["share"] == {"a": 2.0, "b": 5.0, "beta": 1.0}
    # priors translated from business beliefs at fit time are part of the model's priors
    learned = {"calibration": {"translated_priors": {"roi": {"tv": {"mu": 1.1, "sigma": 0.2}}}}}
    with_beliefs = _prior_settings(MMMBayesian, {}, learned)
    assert with_beliefs["priors"]["roi"]["tv"] == {"mu": 1.1, "sigma": 0.2}
    assert _scale_priors(with_beliefs, 2.0)[0]["priors"]["roi"]["tv"]["sigma"] == 0.4
