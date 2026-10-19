"""Representation-aware summaries, aggregation rules, draw thinning and metrics."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from hypothesis import given, settings
from hypothesis import strategies as st
from mapi_sdk.metrics import compute_metrics, elpd, point_estimate
from mapi_sdk.summarise import aggregate, hdi, select_draws, summarise, thin_index
from mapi_spec import Representation
from mapi_spec.capabilities import Aggregation
from mapi_spec.errors import CapabilityError, ErrorCode

R = Representation
RNG = np.random.default_rng(0)
DRAWS = xr.Dataset(
    {"y_hat": (("chain", "draw", "geo", "time"), RNG.normal(10, 1, (2, 200, 2, 3)))},
    coords={"chain": [0, 1], "draw": np.arange(200), "geo": ["a", "b"], "time": [1, 2, 3]},
)
QUANT = xr.Dataset(
    {"y_hat": (("quantile", "geo"), np.array([[1.0, 2.0], [2.0, 3.0], [3.0, 4.0]]))},
    coords={"quantile": [0.1, 0.5, 0.9], "geo": ["a", "b"]},
)
INTERVAL = xr.Dataset(
    {"y_hat": (("bound", "geo"), np.array([[1.0, 2.0], [3.0, 6.0]]), {"alpha": 0.2})},
    coords={"bound": ["lower", "upper"], "geo": ["a", "b"]},
)
POINT = xr.Dataset({"y_hat": (("geo",), np.array([1.0, 2.0]))}, coords={"geo": ["a", "b"]})
MOMENTS = xr.Dataset(
    {"y_hat": (("moment", "geo"), np.array([[1.0, 2.0], [0.5, 1.0]]))},
    coords={"moment": ["mean", "sd"], "geo": ["a", "b"]},
)


def _brute_hdi(x: np.ndarray, prob: float) -> tuple[float, float]:
    s = np.sort(x)
    n = len(s)
    k = max(int(np.floor(prob * n)), 1)
    best = min(range(n - k), key=lambda i: s[i + k] - s[i])
    return float(s[best]), float(s[best + k])


@settings(max_examples=60, deadline=None)
@given(
    values=st.lists(st.floats(-1e6, 1e6, allow_nan=False), min_size=2, max_size=60),
    prob=st.floats(0.05, 0.95),
)
def test_property_hdi_matches_brute_force(values: list[float], prob: float) -> None:
    lo, hi = hdi(np.array(values), prob)
    blo, bhi = _brute_hdi(np.array(values), prob)
    assert (float(lo), float(hi)) == (blo, bhi)
    assert hi - lo <= _quantile_width(np.array(values), prob) + 1e-6


def _quantile_width(x: np.ndarray, prob: float) -> float:
    s = np.sort(x)
    n = len(s)
    k = max(int(np.floor(prob * n)), 1)
    return float(s[min(k, n - 1)] - s[0])


def test_hdi_edge_cases() -> None:
    lo, _hi = hdi(np.array([[1.0, np.nan, 3.0], [1.0, 2.0, 3.0]]), 0.5)
    assert np.isnan(lo[0]) and not np.isnan(lo[1])
    assert hdi(np.array([4.0]), 0.9) == (4.0, 4.0)
    e_lo, _ = hdi(np.zeros((2, 0)), 0.9)
    assert np.isnan(e_lo).all()
    with pytest.raises(ValueError):
        hdi(np.ones(3), 1.5)


def test_thin_index_and_select_draws() -> None:
    idx = thin_index(2, 200, seed=3)
    assert idx.shape == (400, 2)
    assert len({tuple(r) for r in idx}) == 400
    assert (idx == thin_index(2, 200, seed=3)).all()
    picked = select_draws(DRAWS, idx, 5)
    assert picked.sizes["sample"] == 5
    assert picked["chain"].values.tolist() == idx[:5, 0].tolist()
    np.testing.assert_array_equal(
        picked["y_hat"].isel(sample=0).values,
        DRAWS["y_hat"].isel(chain=idx[0, 0], draw=idx[0, 1]).values,
    )
    with pytest.raises(CapabilityError):
        select_draws(POINT, idx, 2)


def test_summaries_per_representation() -> None:
    s = summarise(DRAWS, R.DRAWS, ["mean", "sd", "median", "hdi_90", "eti_80", "q5", "point"])
    assert s["y_hat"].dims[0] == "stat"
    assert s.stat.values.tolist() == [
        "mean",
        "sd",
        "median",
        "hdi_90_lower",
        "hdi_90_upper",
        "eti_80_lower",
        "eti_80_upper",
        "q5",
        "point",
    ]
    m = s["y_hat"].sel(stat="mean")
    assert bool((s["y_hat"].sel(stat="hdi_90_lower") < m).all())
    q = summarise(QUANT, R.QUANTILES, ["median", "q30", "eti_80"])
    assert q["y_hat"].sel(stat="median").values.tolist() == [2.0, 3.0]
    assert q["y_hat"].sel(stat="q30").values.tolist() == pytest.approx([1.5, 2.5])
    assert q["y_hat"].sel(stat="eti_80_upper").values.tolist() == [3.0, 4.0]
    with pytest.raises(CapabilityError, match="outside the stored quantiles"):
        summarise(QUANT, R.QUANTILES, ["q99"])
    with pytest.raises(CapabilityError) as err:
        summarise(QUANT, R.QUANTILES, ["mean"])
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    i = summarise(INTERVAL, R.INTERVAL, ["interval", "point"])
    assert i["y_hat"].sel(stat="point").values.tolist() == [2.0, 4.0]
    with_point = INTERVAL.reindex(bound=["lower", "upper", "point"], fill_value=0.0)
    assert summarise(with_point, R.INTERVAL, ["point"])["y_hat"].values.tolist() == [[0.0, 0.0]]
    p = summarise(POINT, R.POINT, ["point", "mean"])
    assert p["y_hat"].values.tolist() == [[1.0, 2.0], [1.0, 2.0]]
    with pytest.raises(CapabilityError):
        summarise(POINT, R.POINT, ["hdi_90"])
    mo = summarise(MOMENTS, R.MOMENTS, ["mean", "sd", "eti_95"])
    assert mo["y_hat"].sel(stat="eti_95_lower").values.tolist() == pytest.approx(
        [0.02, 0.04], abs=1e-2
    )
    scalar = xr.Dataset({"note": ((), 1.0)})
    assert summarise(scalar, R.DRAWS, ["mean"]).data_vars == {}


def test_aggregation_rules() -> None:
    agg, flagged = aggregate(DRAWS, R.DRAWS, Aggregation(over=["geo"], how="sum"))
    assert "geo" not in agg["y_hat"].dims and not flagged
    np.testing.assert_allclose(agg["y_hat"].values, DRAWS["y_hat"].sum("geo").values)
    for how in ("mean", "min", "max"):
        assert (
            "geo"
            not in aggregate(DRAWS, R.DRAWS, Aggregation(over=["geo"], how=how))[0]["y_hat"].dims
        )
    pagg, pflag = aggregate(POINT, R.POINT, Aggregation(over=["geo"]))
    assert pflag and pagg["y_hat"].item() == 3.0 and "aggregation_note" in pagg["y_hat"].attrs
    with pytest.raises(CapabilityError):
        aggregate(POINT, R.POINT, Aggregation(over=["geo"], how="max"))
    with pytest.raises(CapabilityError, match="quantile"):
        aggregate(QUANT, R.QUANTILES, Aggregation(over=["geo"]))
    assert aggregate(POINT, R.POINT, None) == (POINT, False)
    untouched, _ = aggregate(DRAWS, R.DRAWS, Aggregation(over=["nope"]))
    assert untouched["y_hat"].dims == DRAWS["y_hat"].dims


# ------------------------------------------------------------------------------------ metrics

OBS = xr.DataArray([10.0, 12.0], dims=["geo"], coords={"geo": ["a", "b"]})


def test_point_metrics_and_mask() -> None:
    pred = xr.DataArray([11.0, 12.0], dims=["geo"], coords={"geo": ["a", "b"]})
    values, skipped = compute_metrics(
        pred,
        OBS,
        R.POINT,
        ["rmse", "mae", "bias", "mape", "smape", "wape", "r2", "coverage_90", "crps", "auc"],
    )
    assert values["rmse"] == pytest.approx(np.sqrt(0.5))
    assert values["mae"] == 0.5 and values["bias"] == 0.5
    assert values["mape"] == pytest.approx(5.0)
    assert values["wape"] == pytest.approx(100 / 22)
    assert values["r2"] == pytest.approx(1 - 1 / 2)
    assert set(skipped) == {"coverage_90", "crps", "auc"}
    masked, _ = compute_metrics(
        pred,
        OBS,
        R.POINT,
        ["mae"],
        mask=xr.DataArray([False, True], dims=["geo"], coords={"geo": ["a", "b"]}),
    )
    assert masked["mae"] == 0.0
    none, why = compute_metrics(pred, OBS * np.nan, R.POINT, ["rmse"])
    assert none == {} and why["rmse"] == "no holdout observations"
    flat, _ = compute_metrics(pred, xr.full_like(OBS, 1.0), R.POINT, ["r2"])
    assert flat["r2"] is None


def test_interval_metrics_by_representation() -> None:
    obs = xr.DataArray([2.0, 3.0], dims=["geo"], coords={"geo": ["a", "b"]})
    q, _ = compute_metrics(
        QUANT["y_hat"], obs, R.QUANTILES, ["coverage_80", "interval_width_80", "rmse"]
    )
    assert (
        q["coverage_80"] == 1.0
        and q["interval_width_80"] == pytest.approx(2.0)
        and q["rmse"] == 0.0
    )
    _, sk = compute_metrics(QUANT["y_hat"], obs, R.QUANTILES, ["coverage_95"])
    assert "do not cover" in sk["coverage_95"]
    i, _ = compute_metrics(INTERVAL["y_hat"], obs, R.INTERVAL, ["coverage_80"])
    assert i["coverage_80"] == 1.0
    _, sk2 = compute_metrics(INTERVAL["y_hat"], obs, R.INTERVAL, ["coverage_90"])
    assert "not a 90%" in sk2["coverage_90"]
    m, _ = compute_metrics(MOMENTS["y_hat"], obs, R.MOMENTS, ["coverage_95", "rmse"])
    assert m["coverage_95"] == 0.5  # 2 lies outside 1 ± 1.96*0.5; 3 inside 2 ± 1.96
    d_obs = DRAWS["y_hat"].mean(["chain", "draw"])
    d, _ = compute_metrics(DRAWS["y_hat"], d_obs, R.DRAWS, ["coverage_90", "crps", "rmse"])
    assert d["coverage_90"] == 1.0 and d["rmse"] == pytest.approx(0.0)
    assert 0 < d["crps"] < 1  # type: ignore[operator]


def test_crps_matches_brute_force() -> None:
    samples = RNG.normal(size=(1, 50, 1))
    da = xr.DataArray(samples, dims=["chain", "draw", "g"], coords={"g": ["a"]})
    obs = xr.DataArray([0.3], dims=["g"], coords={"g": ["a"]})
    got, _ = compute_metrics(da, obs, R.DRAWS, ["crps"])
    x = samples.reshape(-1)
    brute = np.abs(x - 0.3).mean() - 0.5 * np.abs(x[:, None] - x[None, :]).mean()
    assert got["crps"] == pytest.approx(brute)
    assert point_estimate(INTERVAL["y_hat"], R.INTERVAL).values.tolist() == [2.0, 4.0]
    q_no_median = QUANT.isel(quantile=[0, 2])
    assert point_estimate(q_no_median["y_hat"], R.QUANTILES).values.tolist() == [2.0, 3.0]


def test_elpd_paths() -> None:
    post = xr.Dataset({"mu": (("chain", "draw"), RNG.normal(size=(2, 50)))})
    ll = xr.Dataset({"y": (("chain", "draw", "obs"), RNG.normal(-1, 0.05, (2, 50, 8)))})
    values, skipped, diags = elpd({"posterior": post, "log_likelihood": ll}, ["elpd_loo", "rmse"])
    assert "elpd_loo" in values and not skipped
    assert isinstance(diags, list)
    _, sk, _ = elpd({"posterior": post}, ["elpd_loo"])
    assert "log_likelihood" in sk["elpd_loo"]
    _, sk2, _ = elpd({}, ["elpd_loo"], loo_valid=False, reason="state-space model")
    assert sk2["elpd_loo"] == "state-space model"
    assert elpd({}, ["rmse"]) == ({}, {}, [])
    spiky = ll.copy()
    spiky["y"][..., 0] = RNG.normal(-50, 30, (2, 50))
    _, _, d2 = elpd({"posterior": post, "log_likelihood": spiky}, ["elpd_loo"])
    assert d2 and d2[0].code == "PARETO_K_HIGH"


def test_elpd_detail_pointwise_cap_and_non_finite() -> None:
    from mapi_sdk.bundle import MetricsReport
    from mapi_sdk.metrics import MAX_POINTWISE_ELPD

    post = xr.Dataset({"mu": (("chain", "draw"), RNG.normal(size=(2, 50)))})
    ll = xr.Dataset({"y": (("chain", "draw", "obs"), RNG.normal(-1, 0.05, (2, 50, 8)))})
    detail: dict[str, dict[str, object]] = {}
    values, _, _ = elpd({"posterior": post, "log_likelihood": ll}, ["elpd_loo"], detail=detail)
    d = detail["elpd_loo"]
    assert d["n"] == 8 and isinstance(d["pointwise"], list) and len(d["pointwise"]) == 8
    assert sum(d["pointwise"]) == pytest.approx(values["elpd_loo"], rel=1e-4)
    # above the cap: n is kept, the pointwise values are not
    big = xr.Dataset(
        {"y": (("chain", "draw", "obs"), RNG.normal(-1, 0.05, (2, 20, MAX_POINTWISE_ELPD + 1)))}
    )
    detail = {}
    elpd({"posterior": post.isel(draw=slice(20)), "log_likelihood": big},
         ["elpd_loo"], detail=detail)  # fmt: skip
    assert detail["elpd_loo"]["pointwise"] is None
    assert detail["elpd_loo"]["n"] == MAX_POINTWISE_ELPD + 1
    # a zero likelihood (-inf): no value and no detail, and the report still round-trips
    dead = ll.copy(deep=True)
    dead["y"][..., 0] = -np.inf
    detail = {}
    values, _, _ = elpd({"posterior": post, "log_likelihood": dead}, ["elpd_loo"], detail=detail)
    assert values["elpd_loo"] is None and "elpd_loo" not in detail
    report = MetricsReport(values=values, elpd={})
    assert MetricsReport.model_validate_json(report.model_dump_json()) == report
    # a bundle written before this fix (a null elpd) loads without the entry
    old = '{"values": {"elpd_loo": null}, "elpd": {"elpd_loo": {"elpd": null, "n": 8}}}'
    assert MetricsReport.model_validate_json(old).elpd == {}


def test_default_summaries_fit_the_stored_quantile_grid() -> None:
    """Defaults never ask for quantiles the grid cannot reach; explicit requests still fail."""
    from mapi_sdk.summarise import default_summary_names

    assert default_summary_names(QUANT, R.QUANTILES) == ["median", "q10", "q90"]
    wide = QUANT.assign_coords(quantile=[0.025, 0.5, 0.975])
    assert default_summary_names(wide, R.QUANTILES) == ["median", "q5", "q95"]
    upper = QUANT.assign_coords(quantile=[0.6, 0.8, 0.9])
    assert default_summary_names(upper, R.QUANTILES) == ["q60", "q90"]
    low = QUANT.assign_coords(quantile=[0.0125, 0.3, 0.4])
    assert default_summary_names(low, R.QUANTILES) == ["q5", "q40"]
    odd = QUANT.assign_coords(quantile=[0.0625, 0.5, 0.9375])
    assert default_summary_names(odd, R.QUANTILES) == ["median", "q6_25", "q93_75"]
    out = summarise(QUANT, R.QUANTILES, default_summary_names(QUANT, R.QUANTILES))
    assert list(out["stat"].values) == ["median", "q10", "q90"]
    assert default_summary_names(DRAWS, R.DRAWS) == ["mean", "hdi_90"]
    assert default_summary_names(POINT, R.QUANTILES) == ["median", "q5", "q95"]
    with pytest.raises(CapabilityError):
        summarise(QUANT, R.QUANTILES, ["q5"])


def _choice_pred() -> xr.DataArray:
    """Two identical draws of choice probabilities over (hh, trip, product)."""
    p = np.array(
        [
            [[0.7, 0.2, 0.1], [0.1, 0.8, 0.1]],  # hh a
            [[0.2, 0.3, 0.5], [0.0, 0.0, 0.0]],  # hh b: no choice set on trip 2
        ]
    )
    return xr.DataArray(
        np.stack([p, p])[None],
        dims=["chain", "draw", "hh", "trip", "product"],
        coords={"hh": ["a", "b"], "trip": [1, 2], "product": ["cola", "fizz", "Store Brand"]},
    )


def test_categorical_targets_get_hit_rate_log_score_and_brier() -> None:
    from mapi_sdk.metrics import CATEGORICAL_METRICS, categorical_dim

    pred = _choice_pred()
    obs = xr.DataArray(
        [["cola", "cola"], ["store_brand", ""]],
        dims=["hh", "trip"],
        coords={"hh": ["a", "b"], "trip": [1, 2]},
    )
    assert categorical_dim(pred, obs) == "product"
    assert categorical_dim(pred.isel(product=0), obs) is None
    values, skipped = compute_metrics(pred, obs, R.DRAWS, [*CATEGORICAL_METRICS, "rmse"])
    # cells: (a,1) cola p=.7 hit; (a,2) cola p=.1 miss; (b,1) Store Brand (loose match) hit;
    # (b,2) has no label and no choice set
    assert values["accuracy"] == pytest.approx(2 / 3)
    assert values["accuracy_naive"] == pytest.approx(2 / 3)  # cola is 2 of the 3 labels
    assert values["accuracy_lift"] == pytest.approx(0.0)
    assert values["log_score"] == pytest.approx(np.mean(np.log([0.7, 0.1, 0.5])))
    brier = np.mean([0.3**2 + 0.2**2 + 0.1**2, 0.9**2 + 0.8**2 + 0.1**2,
                     0.2**2 + 0.3**2 + 0.5**2])  # fmt: skip
    assert values["brier"] == pytest.approx(brier)
    assert "categorical" in skipped["rmse"]
    trip1 = xr.DataArray([True, False], dims=["trip"], coords={"trip": [1, 2]})
    masked, _ = compute_metrics(pred, obs, R.DRAWS, ["accuracy"], mask=trip1)
    assert masked["accuracy"] == 1.0


def test_unknown_labels_are_misses_and_other_shapes_are_skipped() -> None:
    pred = _choice_pred().isel(trip=[0])
    obs = xr.DataArray([["pepsi"], ["fizz"]], dims=["hh", "trip"],
                       coords={"hh": ["a", "b"], "trip": [1]})  # fmt: skip
    values, _ = compute_metrics(pred, obs, R.DRAWS, ["accuracy", "log_score", "brier"])
    assert values["accuracy"] == 0.0
    assert values["log_score"] == pytest.approx(np.mean(np.log([1e-12, 0.3])))
    assert values["brier"] == pytest.approx(np.mean([0.49 + 0.04 + 0.01, 0.04 + 0.49 + 0.25]))
    _, why = compute_metrics(pred.isel(chain=0, draw=0), obs, R.QUANTILES, ["accuracy"])
    assert "probabilities" in why["accuracy"]
    flat = xr.DataArray([0.5, 0.5], dims=["hh"], coords={"hh": ["a", "b"]})
    _, why2 = compute_metrics(flat, obs.isel(trip=0), R.POINT, ["mae", "elpd_loo"])
    assert why2 == {"mae": "the target holds labels but the prediction has no category dim"}
    numeric = obs.isel(trip=0).copy(data=[1.0, 2.0])
    _, why3 = compute_metrics(pred.isel(trip=0), numeric, R.DRAWS, ["accuracy"])
    assert "categorical" in why3["accuracy"]


def test_circular_metrics_wrap_at_the_seam() -> None:
    """Draws around north (350..10 degrees) against observations near 0: the circular mean,
    wrapped errors and seam-straddling intervals (a linear mean would put them near 180)."""
    rng = np.random.default_rng(0)
    draws = (rng.normal(0.0, 5.0, (2, 400, 3))) % 360
    pred = xr.DataArray(draws, dims=("chain", "draw", "obs"))
    obs = xr.DataArray([359.0, 1.0, 180.0], dims=("obs",))
    names = ["mae", "rmse", "bias", "coverage_90", "interval_width_90", "crps", "mape"]
    linear, _ = compute_metrics(pred, obs, Representation.DRAWS, names)
    assert linear["mae"] is not None and linear["mae"] > 100  # the defect this guards
    values, skipped = compute_metrics(pred, obs, Representation.DRAWS, names, period=360.0)
    # point near 0: errors of about +1, -1 and 180
    assert values["mae"] == pytest.approx((1 + 1 + 180) / 3, abs=1.5)
    assert values["coverage_90"] == pytest.approx(2 / 3)
    assert values["interval_width_90"] == pytest.approx(2 * 1.645 * 5, rel=0.15)
    crps = values["crps"]
    assert crps is not None and 0 < crps < values["mae"]
    assert "mape" in skipped and "mape" not in values
    # in hours the same wrap applies (23:30 against 00:30 is one hour)
    hours = xr.DataArray(np.full((1, 4, 1), 0.5), dims=("chain", "draw", "obs"))
    v, _ = compute_metrics(
        hours, xr.DataArray([23.5], dims=("obs",)), Representation.DRAWS, ["mae"], period=24.0
    )
    assert v["mae"] == pytest.approx(1.0)
    # a summarised prediction or a bad period is skipped with a reason, never linear numbers
    _, why = compute_metrics(pred, obs, Representation.QUANTILES, ["mae"], period=360.0)
    assert "draws" in why["mae"]
    _, why = compute_metrics(pred, obs, Representation.DRAWS, ["mae"], period=float("nan"))
    assert "period" in why["mae"]


def _seam_draws(centre: float, sd: float, n_obs: int = 1, period: float = 360.0) -> xr.DataArray:
    rng = np.random.default_rng(1)
    draws = (centre + rng.normal(0.0, sd, (2, 500, n_obs))) % period
    return xr.DataArray(draws, dims=("chain", "draw", "obs"), attrs={"period": period})


def _gap(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_circular_summaries_at_the_seam() -> None:
    """Draws around north (350..10 degrees): the circular mean is near 0, not 180; the
    intervals are centred on it, cross the seam (lower > upper) and are as narrow as the
    spread; a linear summary (no ``periods``) shows the defect this guards."""
    da = _seam_draws(0.0, 5.0)
    names = ["mean", "sd", "median", "hdi_90", "eti_90", "q5", "q95"]
    linear = summarise(xr.Dataset({"d": da}), Representation.DRAWS, names)["d"]
    assert 100 < linear.sel(stat="mean").item() < 260  # the linear mean points south
    assert "circular_period" not in linear.attrs
    out = summarise(xr.Dataset({"d": da}), Representation.DRAWS, names, periods={"d": 360.0})
    s = out["d"].isel(obs=0)
    assert out["d"].attrs["circular_period"] == 360.0 and out["d"].attrs["period"] == 360.0
    vals = {str(k): float(s.sel(stat=k)) for k in s["stat"].values}
    assert all(0.0 <= v < 360.0 for v in vals.values()), vals
    assert _gap(vals["mean"], 0.0) < 1.0 and _gap(vals["median"], 0.0) < 1.0
    assert vals["sd"] == pytest.approx(5.0, rel=0.1)
    for lo, hi in (("hdi_90_lower", "hdi_90_upper"), ("eti_90_lower", "eti_90_upper")):
        assert vals[lo] > 300.0 and vals[hi] < 60.0, vals  # crosses the seam
        width = (vals[hi] - vals[lo]) % 360.0
        assert width == pytest.approx(2 * 1.645 * 5.0, rel=0.15)
    assert vals["q5"] == pytest.approx(vals["eti_90_lower"])
    assert vals["q95"] == pytest.approx(vals["eti_90_upper"])


def test_circular_summaries_agree_with_linear_ones_away_from_the_seam() -> None:
    """Draws around 180 degrees: on the circle and on a line the summaries are the same
    (to the difference of the circular and the arithmetic mean), in hours too."""
    da = _seam_draws(180.0, 4.0, n_obs=2)
    names = ["mean", "hdi_90", "eti_80"]
    ds = xr.Dataset({"d": da, "other": da})
    linear = summarise(ds, Representation.DRAWS, names)
    out = summarise(ds, Representation.DRAWS, names, periods={"d": 360.0})
    np.testing.assert_allclose(out["d"].values, linear["d"].values, atol=0.3)
    xr.testing.assert_identical(out["other"], linear["other"])
    hours = xr.DataArray(np.full((1, 4, 1), 23.5), dims=("chain", "draw", "obs"))
    hours[0, :2, 0] = 0.5  # 23:30 and 00:30: the mean is midnight, not noon
    mean = summarise(xr.Dataset({"h": hours}), Representation.DRAWS, ["mean"], periods={"h": 24})
    assert _gap(mean["h"].sel(stat="mean").item() * 15.0, 0.0) == pytest.approx(0.0, abs=1e-6)


def test_signed_circular_summaries_of_a_difference() -> None:
    """A difference of two angles is wrapped to [-period/2, period/2): a scenario 10 degrees
    west of a baseline near north gives -10, not 350."""
    da = (_seam_draws(-10.0, 2.0) + 180.0) % 360.0 - 180.0
    out = summarise(
        xr.Dataset({"d": da}),
        Representation.DRAWS,
        ["mean", "eti_90"],
        periods={"d": 360.0},
        signed=True,
    )["d"].isel(obs=0)
    assert float(out.sel(stat="mean")) == pytest.approx(-10.0, abs=0.5)
    assert -20.0 < float(out.sel(stat="eti_90_lower")) < float(out.sel(stat="eti_90_upper")) < 0


def test_circular_periods_ignore_other_representations_and_bad_periods() -> None:
    """Only draws are summarised on the circle; a model's own quantiles and an invalid
    period keep the plain summary."""
    q = xr.DataArray(
        [[350.0, 0.0, 10.0]], dims=("obs", "quantile"), coords={"quantile": [0.05, 0.5, 0.95]}
    )
    got = summarise(xr.Dataset({"d": q}), Representation.QUANTILES, ["median"], periods={"d": 360})
    assert got["d"].sel(stat="median").item() == 0.0 and "circular_period" not in got["d"].attrs
    ds = xr.Dataset({"d": _seam_draws(0.0, 5.0)})
    bad = summarise(ds, Representation.DRAWS, ["mean"], periods={"d": 0.0})
    xr.testing.assert_identical(bad, summarise(ds, Representation.DRAWS, ["mean"]))
