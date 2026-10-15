"""fit_spec / score_bundle end to end with toy steps and models, and bundle IO."""

# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.bundle import Bundle, assert_no_pickle, file_digest
from mapi_sdk.errors import BundleError, SchemaValidationError
from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
from mapi_sdk.registry import Registry
from mapi_sdk.steps import CancelToken
from mapi_sdk.wire import read_datatree_zip
from mapi_spec import DataSchema, ModelSpec, load_model_spec, load_score_request
from mapi_spec.errors import CapabilityError, CoordPlanError, ErrorCode, MapiError
from mapi_spec.spec import ScorePolicy

REQ = "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/toy\n"


def req(extra: str = "mode: predict\n") -> Any:
    return load_score_request(REQ + extra)


# ----------------------------------------------------------------------------------- fit modes


def test_evaluate_then_refit_links_evaluation(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    progress: list[float] = []
    bundle = fit_spec(
        e33_spec(),
        data,
        {"fx": fx},
        registry,
        hooks=FitHooks(on_progress=lambda f, m: progress.append(f)),
    )
    assert progress[-1] == 1.0
    ev = bundle.evaluation
    assert ev is not None and ev.manifest.role == "evaluation"
    assert bundle.manifest.role == "production"
    assert bundle.manifest.lineage["evaluation_bundle"] == ev.bundle_id
    assert bundle.metrics.evaluation_bundle == ev.bundle_id
    assert set(bundle.metrics.values) >= {"rmse", "mape", "coverage_90", "elpd_loo"}
    assert bundle.metrics.n_holdout == 3 * 4 and bundle.metrics.output_space == "weekly"
    full_end = bundle.fit_coords["season"]["time"].values[-1]
    train_end = ev.fit_coords["season"]["time"].values[-1]
    assert full_end > train_end  # production context/coords come from the full data
    assert bundle.states["lags"].context is not None
    assert bundle.states["lags"].context["time"].values[-1] == full_end
    assert bundle.thin is not None and bundle.thin.shape == (120, 2)
    assert bundle.raw_schema.vars["revenue_local"].role.value == "target"
    assert bundle.side_tables.keys() == {"fx"}


def test_train_only_and_no_split(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    b = fit_spec(e33_spec(mode="train_only", metrics="rmse"), data, {"fx": fx}, registry)
    assert b.evaluation is None and b.manifest.role == "production"
    assert b.manifest.lineage == {"fit_mode": "train_only"}
    assert "rmse" in b.metrics.values
    spec = e33_spec(metrics="rmse, elpd_loo")
    no_split = spec.model_copy(update={"data": spec.data.model_copy(update={"split": None})})
    nb = fit_spec(no_split, data, {"fx": fx}, registry)
    assert nb.metrics.skipped["rmse"] == "no data.split is declared"
    assert "elpd_loo" in nb.metrics.values


def test_fit_errors(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    with pytest.raises(MapiError) as err:
        fit_spec(e33_spec(), data, {}, registry)
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    bad_schema_data = data.drop_vars("spend_local")
    with pytest.raises(MapiError):
        fit_spec(e33_spec(), bad_schema_data, {"fx": fx}, registry)
    token = CancelToken()
    token.cancel("stop")
    with pytest.raises(MapiError) as cancelled:
        fit_spec(e33_spec(), data, {"fx": fx}, registry, hooks=FitHooks(cancel=token))
    assert cancelled.value.code is ErrorCode.CANCELLED
    stored = data.assign_attrs(mapi_schema=json.dumps({"dims": {}, "vars": {}}))
    with pytest.raises(MapiError):
        fit_spec(e33_spec(), stored, {"fx": fx}, registry)


# ------------------------------------------------------------------------------------ bundles


def test_bundle_roundtrip_tamper_and_pickle(
    tmp_path: Path,
    registry: Registry,
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
) -> None:
    data, fx = panel
    bundle = fit_spec(e33_spec(), data, {"fx": fx}, registry)
    manifest = bundle.write(tmp_path / "b")
    assert set(manifest.files) >= {"bundle.zarr.zip", "metrics.json", "evaluation/manifest.json"}
    tree = read_datatree_zip(tmp_path / "b" / "bundle.zarr.zip")
    paths = {n.path for n in tree.subtree}
    for group in (
        "spec",
        "schema/raw",
        "schema/model",
        "binding",
        "preprocess/lags/context",
        "model/posterior",
        "model/log_likelihood",
        "fit_coords/weekly",
        "side_tables/fx",
        "thin",
    ):
        assert f"/{group}" in paths, group
    back = Bundle.read(tmp_path / "b", registry)
    assert back.bundle_id == bundle.bundle_id
    assert back.evaluation is not None and back.evaluation.bundle_id == bundle.evaluation.bundle_id  # type: ignore[union-attr]
    assert back.metrics.values == bundle.metrics.values
    window = data.isel(date=slice(-28, None))
    a = score_bundle(bundle, req(), window, registry=registry)
    b = score_bundle(back, req(), window, registry=registry)
    xr.testing.assert_identical(a["summary"].to_dataset(), b["summary"].to_dataset())
    # tampering is detected through the manifest hashes
    metrics = tmp_path / "b" / "metrics.json"
    metrics.write_text(metrics.read_text().replace("rmse", "rmsx"))
    with pytest.raises(BundleError, match="manifest hash"):
        Bundle.read(tmp_path / "b", registry)
    assert Bundle.read(tmp_path / "b", registry, verify=False).metrics.values
    with pytest.raises(BundleError, match="not installed"):
        Bundle.read(tmp_path / "b", Registry(), verify=False)
    with pytest.raises(BundleError, match="manifest"):
        Bundle.read(tmp_path / "nope", registry)
    # pickle never enters a bundle
    with pytest.raises(BundleError):
        assert_no_pickle({"model.pkl": b"x"})
    with pytest.raises(BundleError):
        assert_no_pickle({"model.bin": b"\x80\x04\x95"})
    with pytest.raises(BundleError):
        assert_no_pickle({"weights.zip": b"PK\x03\x04....data.pkl"})
    assert_no_pickle({"model.json": b'{"trees": []}'})
    bundle.model.native["evil.joblib"] = b"x"
    with pytest.raises(BundleError):
        bundle.write(tmp_path / "c")
    assert file_digest(b"abc").crc32c == "Nks/tw=="


# ----------------------------------------------------------------------------------- scoring


@pytest.fixture
def prod(
    registry: Registry,
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
) -> Bundle:
    data, fx = panel
    return fit_spec(e33_spec(), data, {"fx": fx}, registry, raw_schema=raw_schema)


def groups(tree: xr.DataTree) -> set[str]:
    return {n.path.lstrip("/") for n in tree.subtree}


def _future(data: xr.Dataset, weeks: int = 4, scale: float = 1.0) -> xr.Dataset:
    last = data.isel(date=slice(-7 * weeks, None))
    shifted = last.assign_coords(date=last.date + np.timedelta64(7 * weeks, "D"))
    return shifted.drop_vars("revenue_local").assign(spend_local=shifted["spend_local"] * scale)


def test_forecast_uses_context_and_names(
    prod: Bundle, registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = panel
    out = score_bundle(
        prod,
        req("mode: forecast\nhorizon: {dim: time, periods: 4}\n"),
        _future(data),
        registry=registry,
    )
    s = out["summary"].to_dataset()
    assert set(s.dims) == {"stat", "market", "date"} and s.sizes["date"] == 4
    assert json.loads(out.attrs["output_space"]) == {"y_hat": "weekly"}
    canonical = score_bundle(
        prod,
        req(
            "mode: forecast\nhorizon: {dim: time, periods: 4}\noutput: {names: canonical, units: model}\n"
        ),
        _future(data),
        registry=registry,
    )
    assert set(canonical["summary"].dims) == {"stat", "geo", "time"}
    assert json.loads(canonical.attrs["output_space"]) == {"y_hat": "season"}
    gap = _future(data).isel(date=slice(7, None))
    with pytest.raises(CoordPlanError) as err:
        score_bundle(
            prod, req("mode: forecast\nhorizon: {dim: time, periods: 3}\n"), gap, registry=registry
        )
    assert err.value.code is ErrorCode.NON_CONTIGUOUS_EXTENSION


def test_context_window_equivalence_and_evaluate_path(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    """Scoring the holdout via the score path (context from train) equals scoring the full
    series in-sample with the same (train) fit, and reproduces the evaluation metrics."""
    data, fx = panel
    bundle = fit_spec(e33_spec(metrics="rmse, mape"), data, {"fx": fx}, registry)
    ev = bundle.evaluation
    assert ev is not None
    holdout_days = data.isel(date=slice(-28, None)).drop_vars("revenue_local")
    via_context = score_bundle(ev, req(), holdout_days, registry=registry, return_full=True)
    full = score_bundle(
        ev, req(), data.drop_vars("revenue_local"), registry=registry, return_full=True
    )
    a = via_context["predictions"].to_dataset()["y_hat"].mean(["chain", "draw"])
    b = full["predictions"].to_dataset()["y_hat"].mean(["chain", "draw"]).sel(date=a.date)
    xr.testing.assert_allclose(a, b)
    # evaluate path == score path: the same metrics come back from an explicit evaluate request
    explicit = score_bundle(
        ev,
        req(
            "mode: evaluate\ndata: dataset://growth/holdout@v1\noutput: {kind: predictive}\nseed: 7\n"
        ),
        data.isel(date=slice(-28, None)),
        registry=registry,
        metrics=["rmse", "mape"],
        return_full=True,
    )
    got = json.loads(explicit.attrs["metrics"])["values"]
    assert got == pytest.approx(bundle.metrics.values)
    assert "observed" in explicit


def test_shuffled_subset_and_new_levels(
    prod: Bundle, registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = panel
    window = data.isel(date=slice(-28, None))
    base = score_bundle(prod, req(), window, registry=registry)["summary"].to_dataset()
    shuffled = score_bundle(prod, req(), window.isel(market=[2, 0, 1]), registry=registry)[
        "summary"
    ].to_dataset()
    assert list(shuffled.market.values) == ["FR", "NL", "DE"]
    xr.testing.assert_allclose(shuffled.sel(market=base.market), base)
    subset = score_bundle(prod, req(), window.sel(market=["DE"]), registry=registry)[
        "summary"
    ].to_dataset()
    xr.testing.assert_allclose(subset, base.sel(market=["DE"]))
    new = window.sel(market=["NL"]).assign_coords(market=["XX"])
    mixed = xr.concat([window, new], dim="market")
    scored = score_bundle(prod, req(), mixed, registry=registry)
    s = scored["summary"].to_dataset()
    assert "XX" in s.market.values and bool(np.isfinite(s["y_hat"]).all())
    issues = json.loads(scored.attrs["issues"])
    assert any(i["code"] == "UNSEEN_LEVELS" and i["severity"] == "info" for i in issues)
    radio = window.isel(channel=[0]).assign_coords(channel=["radio"])
    with pytest.raises(CoordPlanError) as err:
        score_bundle(prod, req(), radio, registry=registry)
    assert err.value.code is ErrorCode.UNSEEN_LEVELS


def test_counterfactual_select_draws_and_aggregate(
    prod: Bundle, registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, _ = panel
    window = data.isel(date=slice(-28, None))
    cf = score_bundle(
        prod,
        req(
            "mode: counterfactual\ninterventions: [{var: spend_local, sel: {channel: tv}, multiply: 1.5}]\n"
            "output: {draws: 4, summary: [mean, hdi_90]}\nselect: {market: [NL, DE]}\n"
        ),
        window,
        registry=registry,
        return_full=True,
    )
    for group in (
        "summary",
        "draws",
        "baseline/summary",
        "delta/summary",
        "delta/draws",
        "predictions",
    ):
        assert group in groups(cf), group
    delta = cf["delta/summary"].to_dataset()["y_hat"].sel(stat="mean")
    assert bool((delta > 0).all())
    assert list(cf["summary"].to_dataset().market.values) == ["NL", "DE"]
    assert cf["draws"].to_dataset().sizes["sample"] == 4
    agg = score_bundle(
        prod, req("output: {aggregate: {over: [market], how: sum}}\n"), window, registry=registry
    )
    assert "market" not in agg["summary"].dims
    for sel_val in (
        "{var: spend_local, sel: {date: ['2024-06-09']}, add: 1.0}",
        "{var: spend_local, set: 0.0}",
    ):
        out = score_bundle(
            prod,
            req(f"mode: counterfactual\ninterventions: [{sel_val}]\n"),
            window,
            registry=registry,
        )
        assert "delta/summary" in groups(out)
    with pytest.raises(MapiError) as err:
        score_bundle(
            prod,
            req("mode: counterfactual\ninterventions: [{var: nope, multiply: 2}]\n"),
            window,
            registry=registry,
        )
    assert err.value.code is ErrorCode.SCHEMA_INVALID


def test_point_and_quantile_models(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    window = data.isel(date=slice(-28, None))
    lin = fit_spec(
        e33_spec(model="toy.linear@^1", metrics="rmse, mape"), data, {"fx": fx}, registry
    )
    out = score_bundle(lin, req(), window, registry=registry, return_full=True)
    preds = out["predictions"].to_dataset()
    # additive contributions sum to the prediction (model units)
    model_units = score_bundle(
        lin, req("output: {units: model}\n"), window, registry=registry, return_full=True
    )
    mp = model_units["predictions"].to_dataset()
    xr.testing.assert_allclose(mp["contributions"].sum("component"), mp["y_hat"])
    assert json.loads(out.attrs["retransform"]) == {"y_hat": "exact", "contributions": "exact"}
    assert preds["y_hat"].attrs["output_space"] == "weekly"
    with pytest.raises(MapiError) as err:
        score_bundle(lin, req("output: {draws: 3}\n"), window, registry=registry)
    assert err.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    q = fit_spec(
        e33_spec(model="toy.quantile@^1", metrics="rmse, coverage_80"), data, {"fx": fx}, registry
    )
    assert "coverage_80" in q.metrics.values
    qs = score_bundle(q, req(), window, registry=registry)["summary"].to_dataset()
    assert qs.stat.values.tolist() == ["median", "q5", "q95"] or "median" in qs.stat.values
    with pytest.raises(CapabilityError):
        score_bundle(q, req("output: {aggregate: {over: [market]}}\n"), window, registry=registry)
    with pytest.raises(MapiError) as err2:
        score_bundle(
            q,
            req("mode: counterfactual\ninterventions: [{var: spend_local, multiply: 2}]\n"),
            window,
            registry=registry,
        )
    assert err2.value.code is ErrorCode.MODE_NOT_SUPPORTED


LOG_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: toy-log, project: growth}
data:
  dataset: dataset://growth/sales@latest
  bind: {dims: {geo: market, time: date}, vars: {y: revenue_local}}
  split: {kind: time_holdout, dim: time, last: 3}
pipeline:
  - {id: weekly, use: toy.resample@^1, with: {dim: time, freq: W-SUN}}
  - {id: logy, use: toy.log1p@^1, with: {vars: [y]}}
  - {id: season, use: toy.fourier@^1, with: {along: time, period: 52.18, order: 2}}
model:
  use: toy.linear@^1
  settings: {features: [fourier], retransform: __RT__}
  fit: {mode: evaluate_then_refit, seed: 1}
evaluate: {metrics: [rmse, mae]}
"""


def test_point_retransformation_and_horizon_without_data(
    registry: Registry, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data = panel[0][["revenue_local"]]
    smear = fit_spec(load_model_spec(LOG_SPEC.replace("__RT__", "smearing")), data, None, registry)
    assert "y" in smear.residuals and smear.residuals["y"].size > 0
    median = fit_spec(load_model_spec(LOG_SPEC.replace("__RT__", "median")), data, None, registry)
    assert median.residuals == {}
    horizon = req("mode: forecast\nhorizon: {dim: time, periods: 3}\noutput: {names: canonical}\n")
    out_s = score_bundle(smear, horizon, None, registry=registry)
    out_m = score_bundle(median, horizon, None, registry=registry)
    assert json.loads(out_s.attrs["retransform"])["y_hat"] == "smearing"
    assert json.loads(out_m.attrs["retransform"])["y_hat"] == "median"
    ys, ym = out_s["summary"].to_dataset()["y_hat"], out_m["summary"].to_dataset()["y_hat"]
    assert ys.sizes["time"] == 3 and bool((ys >= ym * 0.999).all())
    with pytest.raises(MapiError, match="no data"):
        score_bundle(median, req(), None, registry=registry)


def test_seed_determinism(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    a = fit_spec(e33_spec(mode="train_only", metrics="rmse"), data, {"fx": fx}, registry)
    b = fit_spec(e33_spec(mode="train_only", metrics="rmse"), data, {"fx": fx}, registry)
    xr.testing.assert_identical(a.model.groups["posterior"], b.model.groups["posterior"])
    assert a.bundle_id == b.bundle_id
    pred = req("output: {kind: predictive}\nseed: 3\n")
    window = data.isel(date=slice(-42, -28))  # inside the training range of a train_only fit
    s1 = score_bundle(a, pred, window, registry=registry)["summary"].to_dataset()
    s2 = score_bundle(a, pred, window, registry=registry)["summary"].to_dataset()
    xr.testing.assert_identical(s1, s2)


def test_side_table_policies_at_score(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    spec = e33_spec(mode="train_only", metrics="rmse")
    tables = dict(spec.data.side_tables)
    tables["fx"] = tables["fx"].model_copy(update={"score_policy": ScorePolicy.SUPPLIED})
    supplied = spec.model_copy(
        update={"data": spec.data.model_copy(update={"side_tables": tables})}
    )
    b = fit_spec(supplied, data, {"fx": fx}, registry)
    assert b.side_tables == {}
    window = data.isel(date=slice(-42, -28))
    with pytest.raises(MapiError) as err:
        score_bundle(b, req(), window, registry=registry)
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE
    with_fx = load_score_request(REQ + "mode: predict\nside_tables: {fx: dataset://growth/fx@v1}\n")
    assert "summary" in score_bundle(b, with_fx, window, side_tables={"fx": fx}, registry=registry)


def test_schema_mismatch_from_steps_is_reported(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    data, fx = panel
    with pytest.raises((SchemaValidationError, MapiError)):
        fit_spec(
            e33_spec(),
            data.assign(revenue_local=data["revenue_local"].astype(str)),
            {"fx": fx},
            registry,
        )


def test_enter_at_a_later_stage_skips_earlier_side_tables(
    registry: Registry,
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
) -> None:
    """A weekly EUR plan enters after ``weekly``: the fx side table is not needed."""
    data, fx = panel
    spec = e33_spec(mode="evaluate_then_refit", metrics="rmse")
    tables = dict(spec.data.side_tables)
    tables["fx"] = tables["fx"].model_copy(update={"score_policy": ScorePolicy.SUPPLIED})
    spec = spec.model_copy(update={"data": spec.data.model_copy(update={"side_tables": tables})})
    b = fit_spec(spec, data, {"fx": fx}, registry, raw_schema=raw_schema)
    weekly_fit = b.fit_coords["weekly"]["time"].values
    future = np.array([weekly_fit[-1] + np.timedelta64(7 * k, "D") for k in range(1, 4)])
    plan = xr.Dataset(
        {"spend": (("geo", "channel", "time"), np.full((3, 2, 3), 700.0))},
        coords={"geo": ["NL", "DE", "FR"], "channel": ["tv", "search"], "time": future},
    )
    out = score_bundle(
        b,
        req(
            "mode: forecast\nhorizon: {dim: time, periods: 3}\ninputs_stage: weekly\noutput: {names: canonical}\n"
        ),
        plan,
        registry=registry,
        return_full=True,
    )
    pred = out["predictions"].to_dataset()["y_hat"]
    assert pred.sizes["time"] == 3 and bool(np.isfinite(pred).all())
    assert pred.attrs["freq"] == {"time": "W-SUN"}
    assert pred.attrs["output_space"] == "weekly" and pred.attrs["units"] == "EUR"
    with pytest.raises(MapiError) as err:
        score_bundle(
            b,
            req("mode: forecast\nhorizon: {dim: time, periods: 3}\n"),
            _future(data),
            registry=registry,
        )
    assert err.value.code is ErrorCode.SIDE_TABLE_COVERAGE


def test_fit_context_carries_the_stage_schema(
    registry: Registry, e33_spec: Callable[..., ModelSpec], panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    """Plugins see the model-stage schema (every variable and its role), not only the template."""
    from mapi_sdk.models import FitContext, FitOutput
    from mapi_sdk.testing import toy
    from mapi_spec.spec import FitConfig

    seen: list[FitContext] = []

    class _Spy(toy.LinearModel):
        id = "test.spy"

        def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput:
            seen.append(ctx)
            return super().fit(ds, s, cfg, ctx)

    reg = registry.copy()
    reg.register_model(_Spy)
    data, fx = panel
    spec = e33_spec(model="test.spy@^1", mode="train_only", metrics="rmse")
    fit_spec(spec, data, {"fx": fx}, reg)
    stage = seen[0].stage_schema
    assert stage is not None and {"spend", "spend_lag", "fourier"} <= set(stage.vars)
    assert stage.vars["y"].role.value == "target"
    assert seen[0].schema is not None and "spend" not in seen[0].schema.vars
