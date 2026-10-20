"""Model-level run tracking of the MMM plugins (:mod:`mapi_plugin_mmm.tracking`)."""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_plugin_mmm import tracking
from mapi_plugin_mmm.derived import FitWindow
from mapi_plugin_mmm.forward import Posterior, Replay
from mapi_plugin_mmm.frequentist import MMMFrequentist
from mapi_plugin_mmm.planning import thin_index
from mapi_plugin_mmm.tracking import (
    channel_labels,
    label_value,
    log_mmm_summary,
    param_value,
    summary_window,
)
from mapi_sdk.models import FitContext
from mapi_sdk.tracking import MetricRecorder
from mapi_spec.spec import FitConfig
from mapi_spec.tracking import MAX_PARAM_VALUE_CHARS


def test_labels_are_sanitised_and_unique() -> None:
    assert label_value("TV / Brand") == "TV___Brand"
    assert label_value("") == "_"
    assert len(label_value("x" * 100)) == 63
    assert channel_labels(["a b", "a_b", "a/b", "ok"]) == {
        "a b": "a_b",
        "a_b": "a_b_2",
        "a/b": "a_b_3",
        "ok": "ok",
    }
    long = channel_labels(["y" * 70, "y" * 71])
    assert long["y" * 71] == "y" * 61 + "_2"


def _window(pf: Any) -> FitWindow:
    rp, fit = pf.plugin.replay(pf.fitted)
    return FitWindow(rp, fit)


def test_the_summary_logs_roi_shares_and_params(geo_prior_fit: Any) -> None:
    rec = MetricRecorder()
    ctx = FitContext(tracker=rec)
    win = _window(geo_prior_fit)
    log_mmm_summary(ctx, win, params={"mmm.n_channels": 2, "mmm.link": "identity"})
    assert rec.params == {"mmm.n_channels": 2, "mmm.link": "identity"}
    series = rec.series()
    for ch in ("tv", "search"):
        for key in ("roi", "roi_p05", "roi_p95", "mroi", "contribution_share"):
            ((step, _),) = series[(key, f"channel={ch}")]
            assert step is None
    spend, _ = win.arrays()
    roi = win.channel_totals(1.0).sum(axis=2)[:, 0] / spend[..., 0].sum()
    assert series[("roi", "channel=tv")][0][1] == pytest.approx(float(roi.mean()))
    lo, hi = series[("roi_p05", "channel=tv")][0][1], series[("roi_p95", "channel=tv")][0][1]
    assert lo <= float(roi.mean()) <= hi
    shares = sum(series[("contribution_share", f"channel={c}")][0][1] for c in ("tv", "search"))
    base = series[("baseline_share", "")][0][1]
    assert shares + base == pytest.approx(1.0, rel=0.05)


def test_extra_labels_and_dark_channels(geo_prior_fit: Any) -> None:
    rec = MetricRecorder()
    win = _window(geo_prior_fit)
    spend = win.fit.spend.copy()
    spend[..., 1] = 0.0
    dark = FitWindow(win.replay, dataclasses.replace(win.fit, spend=spend))
    log_mmm_summary(FitContext(tracker=rec), dark, labels={"outcome": "premium"})
    series = rec.series()
    assert ("roi", "channel=tv,outcome=premium") in series
    assert ("roi", "channel=search,outcome=premium") not in series
    assert ("contribution_share", "channel=search,outcome=premium") in series
    assert ("baseline_share", "outcome=premium") in series


def test_a_long_param_is_cut_short_not_refused(geo_prior_fit: Any) -> None:
    """50 time-varying channels of 125 characters join to over 6000 characters: the tracker
    used to refuse ``mmm.time_varying`` after sampling and fail the fit."""
    names = ",".join(f"{k:02d}" + "c" * 123 for k in range(50))
    assert len(names) > MAX_PARAM_VALUE_CHARS
    rec = MetricRecorder()
    win = _window(geo_prior_fit)
    log_mmm_summary(FitContext(tracker=rec), win, params={"mmm.time_varying": names})
    logged = rec.params["mmm.time_varying"]
    assert isinstance(logged, str) and len(logged) == MAX_PARAM_VALUE_CHARS
    assert logged.endswith("...") and names.startswith(logged[:-3])
    assert param_value("tv,search") == "tv,search"
    assert param_value(3) == 3


def test_no_tracker_no_work() -> None:
    class Boom:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("computed without a tracker")

    log_mmm_summary(FitContext(), Boom(), params={"mmm.link": "identity"})  # type: ignore[arg-type]


def test_the_summary_window_thins_the_draws(
    geo_prior_fit: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tracking, "SUMMARY_DRAWS", 7)
    rp, fit = geo_prior_fit.plugin.replay(geo_prior_fit.fitted)

    def factory(post: xr.Dataset) -> Replay:
        assert post.sizes["sample"] == 7
        return Replay(rp.model, fit, Posterior.from_dataset(post))

    win = summary_window(factory, geo_prior_fit.fitted.posterior, fit)
    assert win.replay.n == 7
    full = Posterior.from_dataset(geo_prior_fit.fitted.posterior).need("intercept")
    np.testing.assert_allclose(win.replay.intercept, full[thin_index(40, 7)])  # type: ignore[attr-defined]


@pytest.mark.slow
def test_a_frequentist_fit_logs_its_summary(geo_stage: xr.Dataset) -> None:
    plugin = MMMFrequentist()
    s = plugin.parse_settings(
        {"hierarchy": {"pool_over": ["geo"]}, "adstock": {"l_max": 3}, "bootstrap": 20,
         "search_budget": 10}
    )  # fmt: skip
    rec = MetricRecorder()
    plugin.fit(geo_stage, s, FitConfig(seed=1), FitContext(seed=1, tracker=rec))
    assert rec.params["mmm.link"] == "identity" and rec.params["mmm.n_cells"] == 3
    assert rec.params["mmm.n_periods"] == 52 and rec.params["mmm.framework_version"]
    assert ("roi", "channel=tv") in rec.series()
