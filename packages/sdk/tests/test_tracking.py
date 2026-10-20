"""Run tracking (v0.11): the TrackingMixin, MetricRecorder, TeeTracker/with_labels, the
bundle's metrics_history group and the harness wiring."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import ANY

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import FitHooks, fit_spec
from mapi_sdk.models import FitContext, FitOutput, ScoreContext
from mapi_sdk.registry import Registry
from mapi_sdk.testing import toy
from mapi_sdk.tracking import (
    MAX_HISTORY_POINTS,
    METRICS_HISTORY,
    MetricRecorder,
    TeeTracker,
    Tracker,
    spec_params,
    with_labels,
)
from mapi_spec import ModelSpec
from mapi_spec.run import ChainProgress
from mapi_spec.spec import FitConfig
from mapi_spec.tracking import TrackingLimits

# ------------------------------------------------------------------------------- the mixin


def test_mixin_validates_without_a_tracker() -> None:
    ctx = FitContext()
    assert not ctx.tracking
    with pytest.raises(ValueError, match="metric key 'train/loss'"):
        ctx.log_metric("train/loss", 1.0)
    with pytest.raises(ValueError, match="at most 8 labels"):
        ctx.log_metric("loss", 1.0, labels={f"l{i}": "x" for i in range(9)})
    with pytest.raises(ValueError, match="label value 'a b'"):
        ctx.log_metric("loss", 1.0, labels={"split": "a b"})
    with pytest.raises(ValueError, match="step must be >= 0"):
        ctx.log_metric("loss", 1.0, step=-1)
    with pytest.raises(ValueError, match="not a number"):
        ctx.log_metric("loss", "high")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="param key"):
        ctx.log_param("bad key", 1)
    with pytest.raises(ValueError, match="must be a bool, int, float or str"):
        ctx.log_param("p", [1, 2])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reserved"):
        ctx.set_tag("mapi.owner", "x")
    with pytest.raises(ValueError, match="log level"):
        ctx.log_line("x", level="fatal")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="log field key"):
        ctx.log_line("x", fields={"Bad": 1})
    ctx.log_param("p", 1)
    ctx.log_param("p", 1)  # the same value again is fine
    with pytest.raises(ValueError, match="write-once"):
        ctx.log_param("p", 2)


def test_mixin_forwards_clean_values_and_drops_non_finite() -> None:
    rec = MetricRecorder()
    ctx = ScoreContext(tracker=rec)
    assert ctx.tracking
    ctx.log_metrics({"a": 1.0, "b": float("nan"), "c": float("inf")}, step=np.int64(3))  # type: ignore[arg-type]
    ctx.log_metrics({})
    ctx.log_metric("only_nan", float("nan"))
    ctx.log_params({"n": np.int64(4), "flag": True})  # type: ignore[dict-item]
    ctx.set_tags({"family": "gaussian"})
    ctx.log_line("x" * 5000, level="warning", fields={"n": 1, "s": "y" * 400, "z": float("nan")})
    ctx.chains([ChainProgress(chain=0, draws_done=5, draws_total=10)])
    assert rec.series() == {("a", ""): [(3, 1.0)]}
    assert rec.params == {"n": 4, "flag": True}
    assert rec.tags == {"family": "gaussian"}
    level, line, fields = rec.lines[0]
    assert level == "warning" and len(line) == 4096
    assert fields["n"] == 1 and len(str(fields["s"])) == 256 and fields["z"] is None
    assert rec.last_chains[0].draws_done == 5
    with pytest.raises(ValueError, match="ChainProgress"):
        ctx.chains([{"chain": 0}])  # type: ignore[list-item]


def test_log_line_without_tracker_uses_the_plain_log() -> None:
    lines: list[str] = []
    FitContext(log=lines.append).log_line("refit done", level="info")
    assert lines == ["[info] refit done"]


def test_fitcontext_keeps_slots_and_keyword_construction() -> None:
    ctx = FitContext(seed=3, tracker=None)
    assert not hasattr(ctx, "__dict__")
    with pytest.raises(AttributeError):
        ctx.unknown = 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------- the recorder


def test_recorder_is_thread_safe() -> None:
    rec = MetricRecorder()
    ctx = FitContext(tracker=rec)

    def work(t: int) -> None:
        for i in range(1000):
            ctx.log_metric("loss", float(i), step=i, labels={"chain": str(t)})

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    series = rec.series()
    assert len(series) == 8 and all(len(v) == 1000 for v in series.values())
    assert rec.points == 8000 and not rec.dropped


def test_recorder_limits_count_dropped_reasons() -> None:
    lim = TrackingLimits(
        metric_points_per_series=3,
        metric_points_total=5,
        metric_series=2,
        params=1,
        tags=1,
        log_lines=1,
    )
    rec = MetricRecorder(lim)
    for i in range(4):
        rec.log_metrics({"a": float(i)}, step=i)  # 3 kept, 1 series_points_limit
    rec.log_metrics({"b": 1.0}, step=0)
    rec.log_metrics({"b": 2.0}, step=1)  # total 5
    rec.log_metrics({"b": 3.0}, step=2)  # points_limit
    rec.log_metrics({"c": 1.0}, step=0)  # series_limit
    rec.log_metrics({"a": 9.0}, step=0)  # duplicate step: first wins, not a drop
    rec.log_params({"p": 1, "q": 2})
    rec.log_params({"p": 5})  # first wins
    rec.set_tags({"t": "1", "u": "2"})
    rec.set_tags({"t": "3"})  # last wins
    rec.log_line("one")
    rec.log_line("two")
    assert rec.dropped == {
        "series_points_limit": 1,
        "points_limit": 1,
        "series_limit": 1,
        "params_limit": 1,
        "tags_limit": 1,
        "log_lines_limit": 1,
    }
    assert rec.series()[("a", "")][0] == (0, 0.0)
    assert rec.params == {"p": 1} and rec.tags == {"t": "3"}


def test_history_layout() -> None:
    rec = MetricRecorder()
    ctx = FitContext(tracker=rec)
    for i in range(1, 11):
        ctx.log_metric("loss", 10.0 / i, step=i)
    for c in range(2):
        for i in range(0, 20, 2):
            ctx.log_metric("step_size", 0.1 * (c + 1), step=i, labels={"chain": str(c)})
    ctx.log_metric("rmse", 0.5, labels={"split": "val"})
    ctx.log_metric("rmse", 0.4, labels={"split": "val"})  # scalars: the newest wins
    ctx.log_params({"solver": "lbfgs", "alpha": 0.1})
    ctx.set_tag("family", "gaussian")
    ds = rec.history()
    assert ds is not None
    assert set(ds.data_vars) == {"loss", "step_size"}
    assert ds["loss"].dims == ("step",)
    assert ds["step_size"].dims == ("step", "chain")
    assert ds["step"].dtype == np.int64
    assert list(ds["chain"].values) == ["0", "1"]
    assert float(ds["loss"].sel(step=10)) == pytest.approx(1.0)
    assert np.isnan(float(ds["loss"].sel(step=0)))  # step 0 comes from step_size only
    assert float(ds["step_size"].sel(step=4, chain="1")) == pytest.approx(0.2)
    assert json.loads(ds.attrs["mapi_scalars"]) == {"rmse": {"split=val": 0.4}}
    assert json.loads(ds.attrs["mapi_params"]) == {"solver": "lbfgs", "alpha": 0.1}
    assert json.loads(ds.attrs["mapi_tags"]) == {"family": "gaussian"}
    assert all(isinstance(v, str) for k, v in ds.attrs.items() if k != "mapi_dropped_keys")


def test_history_is_none_when_nothing_was_logged() -> None:
    rec = MetricRecorder()
    assert rec.history() is None
    rec.set_tags({"only": "tags"})
    assert rec.history() is None


def test_history_thins_long_series_and_keeps_the_last_step() -> None:
    rec = MetricRecorder()
    n = MAX_HISTORY_POINTS + 1234  # under the per-series budget (10 000)
    for i in range(n):
        rec.log_metrics({"loss": float(i)}, step=i)
    ds = rec.history()
    assert ds is not None
    assert ds.sizes["step"] <= MAX_HISTORY_POINTS
    assert int(ds["step"].values[-1]) == n - 1 and int(ds["step"].values[0]) == 0
    assert ds["loss"].attrs["thinned_from"] == n


def test_history_label_named_like_a_metric_gets_a_prefixed_dim() -> None:
    rec = MetricRecorder()
    rec.log_metrics({"fold": 1.0}, step=0)
    rec.log_metrics({"mae": 1.0}, step=0, labels={"fold": "0"})
    ds = rec.history()
    assert ds is not None
    assert ds["mae"].dims == ("step", "label_fold")


def test_history_metric_named_step_does_not_clash_with_the_step_dim() -> None:
    rec = MetricRecorder()
    ctx = FitContext(tracker=rec)
    for i in range(3):
        ctx.log_metric("step", float(i * 10), step=i)
        ctx.log_metric("loss", 1.0 / (i + 1), step=i, labels={"step": "x"})
    ds = rec.history()
    assert ds is not None
    assert set(ds.data_vars) == {"metric_step", "loss"}
    assert ds["metric_step"].attrs["mapi_key"] == "step"
    assert list(ds["metric_step"].values) == [0.0, 10.0, 20.0]
    assert ds["loss"].dims == ("step", "label_step")


# -------------------------------------------------------------------- tee and with_labels


def test_with_labels_merges_and_tee_skips_none() -> None:
    a, b = MetricRecorder(), MetricRecorder()
    assert with_labels(None, {"fit": "evaluation"}) is None
    labelled = with_labels(a, {"fit": "evaluation", "split": "train"})
    assert isinstance(labelled, Tracker)
    tee = TeeTracker(labelled, None, b)
    assert len(tee.trackers) == 2
    tee.log_metrics({"loss": 1.0}, step=1, labels={"split": "val"})
    tee.log_params({"p": 1})
    tee.set_tags({"t": "x"})
    tee.log_line("hello", level="info")
    tee.chains([])
    tee.flush()
    assert a.series() == {("loss", "fit=evaluation,split=val"): [(1, 1.0)]}
    assert b.series() == {("loss", "split=val"): [(1, 1.0)]}
    assert a.params == b.params == {"p": 1} and a.tags == b.tags == {"t": "x"}
    assert len(a.lines) == len(b.lines) == 1
    with pytest.raises(ValueError):
        with_labels(a, {"Bad": "x"})


def test_with_labels_prefixes_param_keys() -> None:
    rec = MetricRecorder()
    prefixed = with_labels(rec, {}, param_prefix="evaluation.")
    assert prefixed is not None and prefixed is not rec
    prefixed.log_params({"n_obs": 92, "k" * 249: 1})  # too long with the prefix: dropped
    assert rec.params == {"evaluation.n_obs": 92}
    assert with_labels(rec, {}) is rec


# -------------------------------------------------------------------------- media (v0.14)


class _CountingTracker:
    """A plain Tracker (no log_media): media calls must not encode anything for it."""

    def log_metrics(self, values: Any, *, step: Any = None, labels: Any = None) -> None: ...
    def log_params(self, params: Any) -> None: ...
    def set_tags(self, tags: Any) -> None: ...
    def log_line(self, message: Any, *, level: Any = "info", fields: Any = None) -> None: ...
    def chains(self, chains: Any) -> None: ...
    def flush(self) -> None: ...


class _CountingFigure:
    def __init__(self) -> None:
        self.saved = 0

    def savefig(self, buf: Any, **_kw: Any) -> None:
        from mapi_sdk.media import encode_png

        self.saved += 1
        buf.write(encode_png(np.zeros((2, 2), np.uint8))[0])


def test_media_calls_validate_without_a_media_tracker() -> None:
    for tracker in (None, _CountingTracker(), TeeTracker(_CountingTracker())):
        ctx = FitContext(tracker=tracker)  # type: ignore[arg-type]
        assert not ctx.tracking_media
        fig = _CountingFigure()
        ctx.log_figure("residuals", fig)
        ctx.log_image("samples", np.zeros((2, 2)), step=1)
        ctx.log_table("scores", {"a": [1]})
        ctx.log_image("samples", "not an image")  # never encoded: no error
        assert fig.saved == 0
        with pytest.raises(ValueError, match="media key"):
            ctx.log_image("bad/key", np.zeros((2, 2)))
        with pytest.raises(ValueError, match="step must be >= 0"):
            ctx.log_figure("residuals", fig, step=-1)
        with pytest.raises(ValueError, match="label"):
            ctx.log_table("scores", {"a": [1]}, labels={"Bad": "x"})
        with pytest.raises(ValueError, match="caption"):
            ctx.log_image("samples", np.zeros((2, 2)), caption="x" * 501)


def test_media_reach_a_recorder_through_tee_and_labels() -> None:
    a, b = MetricRecorder(), MetricRecorder()
    plain = _CountingTracker()
    tee = TeeTracker(with_labels(a, {"fit": "evaluation", "split": "train"}), plain, b)
    ctx = FitContext(tracker=tee)
    assert ctx.tracking_media
    fig = _CountingFigure()
    ctx.log_figure("residuals", fig, step=0, labels={"split": "val"}, caption="c")
    ctx.log_image("samples", np.ones((3, 3)), step=1)
    ctx.log_table("scores", [{"a": 1}])
    assert fig.saved == 1
    assert [(m.key, m.kind, m.step) for m in b.media] == [
        ("residuals", "image", 0),
        ("samples", "image", 1),
        ("scores", "table", None),
    ]
    assert b.media[0].labels == {"split": "val"} and b.media[0].caption == "c"
    assert a.media[0].labels == {"fit": "evaluation", "split": "val"}  # call labels win
    assert a.media[1].labels == {"fit": "evaluation", "split": "train"}
    assert a.media[0].data == b.media[0].data
    with pytest.raises(ValueError, match="log_figure takes"):
        ctx.log_figure("residuals", np.zeros((2, 2)))


def test_recorder_media_budgets() -> None:
    from mapi_sdk.media import image_item

    one, two = image_item("s", np.zeros((2, 2), np.uint8)), image_item("s", np.ones((2, 2)))
    rec = MetricRecorder(TrackingLimits(media_items=3, media_bytes=one.size + 1))
    rec.log_media(one)
    rec.log_media(one)  # the same bytes count once against media_bytes
    rec.log_media(two)  # new bytes over the byte budget
    rec.log_media(one)
    rec.log_media(one)  # over the item budget
    assert [m.data for m in rec.media] == [one.data] * 3
    assert rec.dropped == {"media_limit": 2}
    assert rec.history() is None  # media never go into the bundle


def test_recorder_without_a_media_budget_takes_no_media() -> None:
    none = MetricRecorder(TrackingLimits(media_items=0, media_bytes=0))
    assert not FitContext(tracker=none).tracking_media
    assert not FitContext(tracker=TeeTracker(none, _CountingTracker())).tracking_media
    other = MetricRecorder()
    ctx = FitContext(tracker=TeeTracker(none, other))
    assert ctx.tracking_media
    ctx.log_image("samples", np.zeros((2, 2), np.uint8))
    assert none.media == [] and none.dropped == {} and len(other.media) == 1


# ------------------------------------------------------------------------------- harness

REQ_PARAMS = {"model", "fit.mode", "fit.seed", "fit.draws", "fit.chains", "data.split"}


class _Logging(toy.LinearModel):
    id = "test.logging"

    def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        for i in range(1, 21):
            ctx.log_metric("loss", 1.0 / i, step=i)
        ctx.log_param("solver", "ridge")
        ctx.log_param("n_obs", max(ds.sizes.values()))  # data-dependent
        return super().fit(ds, s, cfg, ctx)


def _logging_registry(registry: Registry) -> Registry:
    reg = registry.copy()
    reg.register_model(_Logging)
    return reg


def test_fit_records_metrics_history_and_reports_to_the_hook_tracker(
    registry: Registry,
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    tmp_path: Path,
) -> None:
    data, fx = panel
    reg = _logging_registry(registry)
    hook = MetricRecorder()
    bundle = fit_spec(
        e33_spec(model="test.logging@^1", metrics="rmse"),
        data,
        {"fx": fx},
        reg,
        hooks=FitHooks(tracker=hook),
    )
    hist = bundle.model.groups[METRICS_HISTORY]
    assert list(hist["step"].values) == list(range(1, 21))
    assert json.loads(hist.attrs["mapi_params"]) == {"solver": "ridge", "n_obs": ANY}
    ev = bundle.evaluation
    assert ev is not None and METRICS_HISTORY in ev.model.groups
    # the hook tracker: the spec params once, evaluation points labelled, production plain
    assert set(hook.params) >= REQ_PARAMS
    assert hook.params["model"] == "test.logging@1.0.0"
    assert hook.params["fit.mode"] == "evaluate_then_refit"
    assert hook.params["data.split"] == "time_holdout"
    assert any(k.startswith("settings.") for k in hook.params)
    assert hook.params["solver"] == "ridge"
    # the evaluation fit's params are prefixed: the refit's value describes the model version
    assert hook.params["n_obs"] == json.loads(hist.attrs["mapi_params"])["n_obs"]
    assert hook.params["evaluation.n_obs"] < hook.params["n_obs"]
    assert hook.params["evaluation.solver"] == "ridge"
    series = hook.series()
    assert len(series[("loss", "fit=evaluation")]) == 20
    assert len(series[("loss", "")]) == 20
    # the group survives the bundle round trip
    bundle.write(tmp_path / "b")
    again = Bundle.read(tmp_path / "b", reg)
    back = again.model.groups[METRICS_HISTORY]
    assert list(back["step"].values) == list(range(1, 21))
    assert back.attrs["mapi_params"] == hist.attrs["mapi_params"]


class _MediaProbe(toy.LinearModel):
    id = "test.media_probe"
    seen: ClassVar[list[bool]] = []

    def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        _MediaProbe.seen.append(ctx.tracking_media)
        ctx.log_metric("loss", 1.0, step=1)
        return super().fit(ds, s, cfg, ctx)


def test_the_fits_own_recorder_takes_no_media(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    reg = registry.copy()
    reg.register_model(_MediaProbe)
    spec = e33_spec(model="test.media_probe@^1", mode="train_only", metrics="rmse")
    _MediaProbe.seen.clear()
    bundle = fit_spec(spec, data, {"fx": fx}, reg)
    assert METRICS_HISTORY in bundle.model.groups
    assert _MediaProbe.seen == [False]  # no tracker takes media: nothing is rendered
    fit_spec(spec, data, {"fx": fx}, reg, hooks=FitHooks(tracker=MetricRecorder()))
    assert _MediaProbe.seen == [False, True]


def test_record_metrics_false_writes_no_group(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    hook = MetricRecorder()
    bundle = fit_spec(
        e33_spec(model="test.logging@^1", mode="train_only", metrics="rmse"),
        data,
        {"fx": fx},
        _logging_registry(registry),
        hooks=FitHooks(tracker=hook),
        record_metrics=False,
    )
    assert METRICS_HISTORY not in bundle.model.groups
    # train_only: the single fit is the production fit, no evaluation label
    assert len(hook.series()[("loss", "")]) == 20


def test_a_model_without_logging_gets_no_group(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    bundle = fit_spec(e33_spec(model="toy.linear@^1", metrics="rmse"), data, {"fx": fx}, registry)
    assert METRICS_HISTORY not in bundle.model.groups


def test_spec_params_flatten_settings(e33_spec: Callable[..., ModelSpec]) -> None:
    from mapi_spec.capabilities import ModelManifest

    spec = e33_spec(model="toy.linear@^1")
    manifest = ModelManifest.model_validate(toy.LinearModel.manifest().model_dump(mode="json"))
    settings = {
        "alpha": 0.5,
        "nested": {"a": 1, "b": {"c": "x"}},
        "items": [1, 2, 3],
        "empty": {},
        "none": None,
        "bad key": 1,
        "long": list(range(400)),
    }
    out = spec_params(spec, settings, manifest)
    assert out["model"] == f"toy.linear@{manifest.version}"
    assert out["fit.seed"] == 7 and out["fit.draws"] == 60 and out["fit.chains"] == 2
    assert "fit.tune" not in out and "fit.engine" not in out
    assert out["settings.alpha"] == 0.5
    assert out["settings.nested.a"] == 1 and out["settings.nested.b.c"] == "x"
    assert out["settings.items"] == "[1,2,3]" and out["settings.empty"] == "{}"
    assert "settings.none" not in out and "settings.bad key" not in out
    assert len(str(out["settings.long"])) == 500


def test_loss_curve_renders_the_recorded_group(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    pytest.importorskip("altair")
    recipes = pytest.importorskip("mapi_plots.recipes")
    data, fx = panel
    bundle = fit_spec(
        e33_spec(model="test.logging@^1", mode="train_only", metrics="rmse"),
        data,
        {"fx": fx},
        _logging_registry(registry),
    )
    hist = bundle.model.groups[METRICS_HISTORY]
    plot = recipes.get_recipe("core.loss_curve").render({METRICS_HISTORY: hist})
    assert plot.id == "core.loss_curve"


# ------------------------------------------------------------------------------ inproc


async def test_inproc_executor_emits_tracking_events(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    from mapi_sdk.executors import InprocExecutor
    from mapi_spec.executor import ExecState, RunEventType, RunKind, RunSubmit
    from mapi_spec.run import MetricBatchPayload, ParamsPayload

    data, fx = panel
    ex = InprocExecutor(
        registry=_logging_registry(registry),
        resolver={
            "dataset://growth/eu_media_daily@latest": data,
            "dataset://growth/fx_daily@latest": fx,
        },
    )
    spec = e33_spec(model="test.logging@^1", mode="train_only", metrics="rmse")
    h = await ex.submit(
        RunSubmit(
            run_id="fit-t",
            kind=RunKind.FIT,
            payload={
                "spec": spec.model_dump(mode="json", by_alias=True, exclude_defaults=True),
                "side_tables": {"fx": "dataset://growth/fx_daily@latest"},
            },
        ),
        "nonce-tracking",
    )
    assert (await ex.wait(h, timeout=60)).state is ExecState.SUCCEEDED
    events = ex.events(h)
    batches = [
        MetricBatchPayload.model_validate(e.payload)
        for e in events
        if e.type is RunEventType.METRICS
    ]
    points = [p for b in batches for p in b.points()]
    assert [p[2] for p in points if p[0] == "loss"] == list(range(1, 21))
    params = {
        i.key: i.value
        for e in events
        if e.type is RunEventType.PARAMS
        for i in ParamsPayload.model_validate(e.payload).params
    }
    assert params["model"] == "test.logging@1.0.0" and params["solver"] == "ridge"
    # the completion events come after the last metrics batch
    kinds = [e.type for e in events]
    assert kinds.index(RunEventType.COMPLETE) > max(
        i for i, k in enumerate(kinds) if k is RunEventType.METRICS
    )
