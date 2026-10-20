"""v0.10 metrics: binary targets (auc, brier, log_score, accuracy), MASE, Pareto k, and the
harness pairing of classifier probabilities and the MASE scale of fit-time evaluation."""

from __future__ import annotations

import re
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_sdk.bundle import ElpdDetail, MetricsReport
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.metrics import (
    BINARY_METRICS,
    MAX_POINTWISE_ELPD,
    _elpd_detail,
    binary_auc,
    compute_metrics,
    elpd,
    mase_scale,
)
from mapi_sdk.models import PREDICTIONS, Fitted, ScoreContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import toy
from mapi_spec import Representation, load_model_spec, load_score_request
from mapi_spec.spec import ScoreRequest
from mapi_spec.validate import KNOWN_METRICS

R = Representation
AUC_REASON = "AUC needs a 0/1 target and predicted probabilities in [0, 1]"
MASE_REASON = (
    "MASE needs the in-sample naive error of the training data (a fit with a time holdout)"
)
NOT_COMPUTED = "not computed by the SDK harness"
REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://kit/toy\n"


def _row(values: list[float]) -> xr.DataArray:
    return xr.DataArray(np.asarray(values, dtype=float), dims=["row"],
                        coords={"row": np.arange(len(values))})  # fmt: skip


# ------------------------------------------------------------------------------- binary AUC


def test_auc_by_hand_with_ties_perfect_and_one_class() -> None:
    assert binary_auc(np.array([0.1, 0.4, 0.35, 0.8]), np.array([0, 0, 1, 1])) == 0.75
    # a tie between a positive and a negative counts one half
    assert binary_auc(np.array([0.5, 0.5, 0.9]), np.array([0, 1, 1])) == 0.75
    assert binary_auc(np.array([0.1, 0.2, 0.8, 0.9]), np.array([0, 0, 1, 1])) == 1.0
    assert binary_auc(np.array([0.9, 0.8, 0.2, 0.1]), np.array([0, 0, 1, 1])) == 0.0
    assert binary_auc(np.array([0.1, 0.9]), np.array([1, 1])) is None
    assert binary_auc(np.array([0.1, 0.9]), np.array([0, 0])) is None


def test_binary_metrics_on_point_probabilities() -> None:
    p, y = [0.1, 0.4, 0.35, 0.8], [0.0, 0.0, 1.0, 1.0]
    values, skipped = compute_metrics(_row(p), _row(y), R.POINT, [*BINARY_METRICS, "rmse"])
    assert not skipped
    pa, ya = np.array(p), np.array(y)
    assert values["auc"] == pytest.approx(0.75)
    assert values["brier"] == pytest.approx(np.mean((pa - ya) ** 2))
    assert values["log_score"] == pytest.approx(np.mean(np.log([0.9, 0.6, 0.35, 0.8])))
    assert values["accuracy"] == pytest.approx(0.75)  # 0.35 for a positive is the miss
    # rmse of a probability is the root of the binary Brier score
    assert values["rmse"] == pytest.approx(np.sqrt(values["brier"] or 0.0))


def test_two_valued_target_uses_the_larger_value_as_positive() -> None:
    values, _ = compute_metrics(_row([0.1, 0.4, 0.35, 0.8]), _row([3.0, 3.0, 7.0, 7.0]),
                                R.POINT, ["auc", "accuracy"])  # fmt: skip
    assert values == {"auc": pytest.approx(0.75), "accuracy": pytest.approx(0.75)}


def test_binary_metrics_use_the_mean_of_draws_and_the_mask() -> None:
    rng = np.random.default_rng(1)
    p = np.array([0.1, 0.4, 0.35, 0.8])
    noise = rng.normal(0, 0.02, (2, 50, 4))
    noise -= noise.mean(axis=(0, 1))  # the mean over draws is exactly p
    draws = xr.DataArray(p + noise, dims=["chain", "draw", "row"], coords={"row": np.arange(4)})
    y = _row([0.0, 0.0, 1.0, 1.0])
    values, _ = compute_metrics(draws, y, R.DRAWS, ["auc", "brier"])
    assert values["auc"] == pytest.approx(0.75)
    assert values["brier"] == pytest.approx(np.mean((p - y.values) ** 2))
    keep = _row([1.0, 0.0, 1.0, 1.0]).astype(bool)  # drops the 0.4 negative
    masked, _ = compute_metrics(draws, y, R.DRAWS, ["auc"], mask=keep)
    assert masked["auc"] == 1.0


def test_binary_metrics_skip_reasons() -> None:
    names = ["auc", "brier", "log_score", "accuracy"]
    # one class: auc has no meaning, the others still do
    values, skipped = compute_metrics(_row([0.2, 0.7]), _row([1.0, 1.0]), R.POINT, names)
    assert skipped == {"auc": "AUC needs both classes in the holdout"}
    assert values["accuracy"] == 0.5
    # a continuous target: auc gets the I-1 reason, the others stay categorical-only
    _, why = compute_metrics(_row([1.0, 2.0, 3.0]), _row([1.5, 2.5, 3.5]), R.POINT, names)
    assert why["auc"] == AUC_REASON
    assert (
        why["brier"]
        == why["log_score"]
        == why["accuracy"]
        == ("defined for categorical targets only")
    )
    # a 0/1 target but predictions outside [0, 1]
    _, why2 = compute_metrics(_row([-0.5, 1.5]), _row([0.0, 1.0]), R.POINT, ["auc", "brier"])
    assert why2 == {"auc": AUC_REASON, "brier": "defined for categorical targets only"}


# -------------------------------------------------------------------------------------- MASE


def test_mase_scale_lags_and_fallbacks() -> None:
    train = np.array([1.0, 2.0, 4.0, 7.0])
    assert mase_scale(train, 2) == pytest.approx(np.mean([3.0, 5.0]))
    assert mase_scale(train, 10) == pytest.approx(2.0)  # season too long: lag 1
    assert mase_scale(train, None) == mase_scale(train, 0) == pytest.approx(2.0)
    assert mase_scale(np.ones(5), 1) is None
    cells = np.stack([train, 2 * train], axis=1)  # trailing axes are pooled cells
    assert mase_scale(cells, 1) == pytest.approx(3.0)


def test_mase_with_and_without_a_scale() -> None:
    pred, obs = _row([1.0, 2.0, 4.0]), _row([2.0, 2.0, 2.0])
    values, skipped = compute_metrics(pred, obs, R.POINT, ["mae", "mase"], scale=0.5)
    assert values["mase"] == pytest.approx(values["mae"] / 0.5)  # type: ignore[operator]
    assert not skipped
    _, why = compute_metrics(pred, obs, R.POINT, ["mase"])
    assert why == {"mase": MASE_REASON}
    _, why0 = compute_metrics(pred, obs, R.POINT, ["mase"], scale=0.0)
    assert why0 == {"mase": MASE_REASON}


def _known_metric_names() -> list[str]:
    """Every literal name the spec validator accepts (``_90`` for the coverage patterns)."""
    names: list[str] = []
    for pat in KNOWN_METRICS:
        text = pat.pattern
        if text.startswith("^elpd_"):
            continue
        if "[1-9]" in text:
            names.append(text[1:].split("_[")[0] + "_90")
            continue
        m = re.fullmatch(r"\^(\w*?)_?\(([\w|]+)\)\$", text)
        assert m is not None, text
        prefix = f"{m.group(1)}_" if m.group(1) else ""
        names += [prefix + alt for alt in m.group(2).split("|")]
    return names


def test_every_known_metric_is_computed() -> None:
    names = _known_metric_names()
    assert {"auc", "mase", "accuracy_lift", "coverage_90", "interval_width_90"} <= set(names)
    rng = np.random.default_rng(0)
    computed: dict[str, float | None] = {}
    # continuous draws with a MASE scale
    draws = xr.DataArray(rng.normal(5, 1, (2, 40, 6)), dims=["chain", "draw", "row"],
                         coords={"row": np.arange(6)})  # fmt: skip
    obs = _row([4.0, 5.0, 6.0, 5.5, 4.5, 5.0])
    values, skipped = compute_metrics(draws, obs, R.DRAWS, names, scale=1.0)
    computed.update(values)
    assert NOT_COMPUTED not in skipped.values(), skipped
    # binary probabilities
    prob = _row([0.1, 0.4, 0.35, 0.8])
    values, skipped = compute_metrics(prob, _row([0.0, 0.0, 1.0, 1.0]), R.POINT, names)
    computed.update({k: v for k, v in values.items() if k in BINARY_METRICS})
    assert NOT_COMPUTED not in skipped.values(), skipped
    # categorical labels
    cat = xr.DataArray([[0.7, 0.3], [0.2, 0.8]], dims=["row", "product"],
                       coords={"row": [0, 1], "product": ["a", "b"]})  # fmt: skip
    labels = xr.DataArray(["a", "a"], dims=["row"], coords={"row": [0, 1]})
    values, skipped = compute_metrics(cat, labels, R.POINT, names)
    computed.update(values)
    assert NOT_COMPUTED not in skipped.values(), skipped
    assert set(names) <= set(computed), set(names) - set(computed)


# ---------------------------------------------------------------------------------- Pareto k


def test_elpd_detail_round_trips_pareto_k() -> None:
    detail = ElpdDetail(elpd=-10.0, n=2, pointwise=[-4.0, -6.0], pareto_k=[0.12, 0.81])
    report = MetricsReport(elpd={"elpd_loo": detail})
    back = MetricsReport.model_validate_json(report.model_dump_json())
    assert back.elpd["elpd_loo"].pareto_k == [0.12, 0.81]
    # a bundle written before v0.10 has no pareto_k
    old = '{"elpd": {"elpd_loo": {"elpd": -1.0, "n": 1, "pointwise": [-1.0]}}}'
    assert MetricsReport.model_validate_json(old).elpd["elpd_loo"].pareto_k is None


def test_elpd_detail_keeps_pareto_k_of_an_arviz_loo() -> None:
    az = pytest.importorskip("arviz")
    rng = np.random.default_rng(3)
    post = xr.Dataset({"mu": (("chain", "draw"), rng.normal(size=(2, 100)))})
    ll = xr.Dataset({"y": (("chain", "draw", "obs"), rng.normal(-1, 0.3, (2, 100, 6)))})
    tree = xr.DataTree.from_dict({"posterior": post, "log_likelihood": ll})
    res = az.loo(tree, pointwise=True)
    d = _elpd_detail(res)
    k = np.asarray(res.pareto_k).ravel()
    assert d["pareto_k"] == [float(f"{v:.4g}") for v in k.tolist()]
    assert len(d["pareto_k"]) == len(d["pointwise"]) == 6
    assert ElpdDetail.model_validate(d).pareto_k == d["pareto_k"]
    detail: dict[str, dict[str, Any]] = {}
    elpd({"posterior": post, "log_likelihood": ll}, ["elpd_loo"], detail=detail)
    assert detail["elpd_loo"]["pareto_k"] is not None
    # above the cap, or a non-finite k: none
    big = res.pareto_k.values.repeat(MAX_POINTWISE_ELPD // 6 + 1)

    class _Big:
        elpd, pareto_k, n_data_points = -1.0, big, big.size

    assert _elpd_detail(_Big())["pareto_k"] is None

    class _Inf:
        elpd, pareto_k = -1.0, np.array([0.2, np.inf])

    assert _elpd_detail(_Inf())["pareto_k"] is None

    class _Waic:
        elpd, waic_i = -1.0, np.array([-0.5, -0.5])

    assert _elpd_detail(_Waic())["pareto_k"] is None


# ---------------------------------------------------------------------------------- harness

SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: toy-metrics, project: kit}
data:
  dataset: dataset://kit/panel@latest
  split: {kind: time_holdout, dim: time, last: __LAST__}
pipeline: []
model:
  use: __MODEL__
  settings: {features: [x]}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [__METRICS__]}
"""


def _spec(model: str, metrics: str, last: int = 4) -> Any:
    text = SPEC.replace("__MODEL__", model).replace("__METRICS__", metrics)
    return load_model_spec(text.replace("__LAST__", str(last)))


def _panel(weeks: int, seed: int = 0) -> xr.Dataset:
    rng = np.random.default_rng(seed)
    time = pd.date_range("2022-01-02", periods=weeks, freq="W-SUN").as_unit("ns")
    x = rng.uniform(0, 1, (3, weeks))
    season = np.sin(2 * np.pi * np.arange(weeks) / 52.0)
    y = 10 + 3 * x + 2 * season + rng.normal(0, 0.3, (3, weeks))
    return xr.Dataset({"y": (("geo", "time"), y), "x": (("geo", "time"), x)},
                      coords={"geo": ["a", "b", "c"], "time": time})  # fmt: skip


class ProbClassifier(toy.LinearModel):
    """The toy linear model as a classifier: ``y_hat`` is P(y = 1), clipped to [0, 1]."""

    id = "toy.prob_classifier"
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["geo", "time"], "units": "1"}}

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        out = super().score(fitted, ds, req, ctx)[PREDICTIONS].to_dataset()
        p = out["y_hat"].clip(0.0, 1.0)
        p.attrs = {"meaning": "probability", "positive_class": 1}
        return xr.DataTree.from_dict({PREDICTIONS: xr.Dataset({"y_hat": p})})


def test_classifier_probabilities_get_binary_metrics(registry: Registry) -> None:
    registry.register_model(ProbClassifier)
    data = _panel(30)
    rng = np.random.default_rng(2)
    data["y"] = (data["x"] + rng.normal(0, 0.15, data["x"].shape) > 0.5).astype(float)
    bundle = fit_spec(_spec("toy.prob_classifier@^1", "auc, brier, accuracy, rmse"), data,
                      None, registry)  # fmt: skip
    m = bundle.metrics
    assert set(m.values) == {"auc", "brier", "accuracy", "rmse"}, m.skipped
    assert 0.5 < (m.values["auc"] or 0) <= 1.0
    assert m.values["rmse"] == pytest.approx(np.sqrt(m.values["brier"] or 0.0))
    assert m.n_holdout == 3 * 4


def test_time_holdout_fit_computes_mase_from_the_training_data(registry: Registry) -> None:
    data = _panel(120)
    bundle = fit_spec(_spec("toy.linear@^1", "mae, mase"), data, None, registry)
    m = bundle.metrics
    assert not m.skipped, m.skipped
    train = data["y"].isel(time=slice(None, -4)).transpose("time", "geo").values
    scale = mase_scale(train, 52)  # weekly: one year is 52 periods
    assert scale is not None and scale != mase_scale(train, 1)
    assert m.values["mase"] == pytest.approx((m.values["mae"] or 0) / scale)


def test_mase_needs_the_training_data(registry: Registry) -> None:
    """Scoring with ``evaluate`` outside a fit has no training data: ``mase`` is skipped."""
    data = _panel(30)
    bundle = fit_spec(_spec("toy.linear@^1", "mae, mase"), data, None, registry)
    assert bundle.metrics.values["mase"] is not None
    req = load_score_request(REQ + "mode: evaluate\ndata: dataset://kit/holdout@latest\n")
    tree = score_bundle(bundle, req, data.isel(time=slice(-4, None)), registry=registry,
                        metrics=["mae", "mase"])  # fmt: skip
    report = MetricsReport.model_validate_json(tree.attrs["metrics"])
    assert report.values["mae"] is not None
    assert report.skipped == {"mase": MASE_REASON}
