"""The contract kit passes on the toy references and catches broken steps and models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.binding import BindingMap
from mapi_sdk.models import FitContext, FitOutput, Fitted, ScoreContext
from mapi_sdk.registry import Registry
from mapi_sdk.steps import StepContext, StepState
from mapi_sdk.testing import (
    ContractViolation,
    ModelFixture,
    check_model,
    check_step,
    toy,
)
from mapi_spec import DataSchema, ModelSpec, StepKind
from mapi_spec.spec import FitConfig, ScoreRequest
from pydantic import Field, JsonValue

BIND = {
    "dims": {"geo": "market", "time": "date"},
    "vars": {"y": "revenue_local", "spend": "spend_local"},
}


@pytest.fixture
def weekly(panel: tuple[xr.Dataset, xr.Dataset]) -> xr.Dataset:
    data = BindingMap(BIND).apply(panel[0])
    return data.resample(time="W-SUN").sum().isel(time=slice(1, -1))


STEP_CASES: list[tuple[Any, dict[str, Any]]] = [
    (toy.ScaleStep, {"vars": ["spend", "y"], "over": ["time"], "unseen_levels": "pooled"}),
    (toy.Log1pStep, {"vars": ["y"]}),
    (toy.LagStep, {"vars": ["spend"], "within": ["geo"], "lags": [1, 2]}),
    (toy.FourierStep, {"period": 52.18, "order": 2}),
    (toy.ResampleStep, {"dim": "time", "freq": "MS"}),
]


@pytest.mark.mapi_contract
@pytest.mark.parametrize(("step", "settings"), STEP_CASES, ids=[c[0].id for c in STEP_CASES])
def test_toy_steps_pass(
    step: Any,
    settings: dict[str, Any],
    weekly: xr.Dataset,
    raw_target_schema: Callable[[xr.Dataset], DataSchema],
) -> None:
    report = check_step(step, weekly, settings, schema=raw_target_schema(weekly))
    assert report.ok, report.summary()
    assert report.get("catalog_entry").passed


def test_fx_step_with_side_table(
    canonical_panel: xr.Dataset, panel: tuple[xr.Dataset, xr.Dataset]
) -> None:
    report = check_step(
        toy.FxStep,
        canonical_panel,
        {"vars": ["y", "spend"], "table": "fx", "table_along": "date"},
        side_tables={"fx": panel[1]},
    )
    assert report.ok, report.summary()
    assert not report.get("inverse_roundtrip").skipped


@pytest.fixture
def raw_target_schema() -> Callable[[xr.Dataset], DataSchema]:
    from mapi_sdk.schema import infer_schema

    return lambda ds: infer_schema(ds, targets=["y"])


# ------------------------------------------------------------------------------- broken steps


class _Learner(toy.ScaleStep):
    """Cheats: scales by the batch mean at transform time (learns from score data)."""

    id = "test.learner"

    def transform(self, ds: xr.Dataset, s: Any, st: Any, ctx: StepContext) -> xr.Dataset:
        return ds.assign({v: ds[v] / abs(ds[v]).mean() for v in s.vars if v in ds})


class _Liar(toy.Log1pStep):
    """Declares a variable it never adds."""

    id = "test.liar"
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "adds": {"ghost": {"dims": ["geo"], "dtype": "float64"}}
    }


class _Mutator(toy.Log1pStep):
    id = "test.mutator"

    def transform(self, ds: xr.Dataset, s: Any, st: StepState, ctx: StepContext) -> xr.Dataset:
        for v in s.vars:
            if v in ds:
                ds[v].values[...] = np.log1p(ds[v].values)
        return ds


class _BadInverse(toy.Log1pStep):
    id = "test.badinverse"

    def inverse(self, ds: xr.Dataset, s: Any, st: StepState, ctx: StepContext) -> xr.Dataset:
        return ds


class _NeedsTarget(toy.Log1pStep):
    id = "test.needstarget"

    def transform(self, ds: xr.Dataset, s: Any, st: StepState, ctx: StepContext) -> xr.Dataset:
        return ds.assign({v: np.log1p(ds[v]) for v in s.vars})


class _HiddenDefaultSettings(toy.VarsSettings):
    extra_dims: list[str] = Field(default_factory=list)


class _HiddenDefault(toy.Log1pStep):
    """Its default is invisible in the exported JSON Schema (default_factory)."""

    id = "test.hiddendefault"
    Settings = _HiddenDefaultSettings


class _FreqLiar(toy.ResampleStep):
    """Declares monthly output while resampling to the configured (weekly) frequency."""

    id = "test.freqliar"
    effect_template: ClassVar[dict[str, JsonValue]] = {
        "template_params": ["dim"],
        "transforms_dims": {"{dim}": {"kind": "time", "policy": "extend", "freq": "MS"}},
    }


class _UnitsLiar(toy.Log1pStep):
    """Relabels the units of its output without declaring it in the effect."""

    id = "test.unitsliar"

    def transform(self, ds: xr.Dataset, s: Any, st: StepState, ctx: StepContext) -> xr.Dataset:
        out = super().transform(ds, s, st, ctx)
        for v in s.vars:
            if v in out:
                out[v].attrs["units"] = "EUR"
        return out


def test_effect_check_compares_freq_and_units(
    weekly: xr.Dataset, raw_target_schema: Callable[[xr.Dataset], DataSchema]
) -> None:
    daily = weekly.resample(time="D").ffill()
    report = check_step(_FreqLiar, daily, {"dim": "time", "freq": "W-SUN"}, raise_on_failure=False)
    detail = report.get("effect_matches_output").detail
    assert not report.get("effect_matches_output").passed and "freq" in detail, detail
    honest = check_step(
        toy.ResampleStep, daily, {"dim": "time", "freq": "W-SUN"}, raise_on_failure=False
    )
    assert honest.get("effect_matches_output").passed, honest.summary()
    units = check_step(
        _UnitsLiar,
        weekly,
        {"vars": ["y"]},
        schema=raw_target_schema(weekly),
        raise_on_failure=False,
    )
    assert "units" in units.get("effect_matches_output").detail, units.summary()


def test_broken_steps_fail_specific_checks(
    weekly: xr.Dataset, raw_target_schema: Callable[[xr.Dataset], DataSchema]
) -> None:
    schema = raw_target_schema(weekly)
    cases = [
        (_Learner, {"vars": ["y"], "over": ["time"]}, "transform_never_learns"),
        (_Liar, {"vars": ["y"]}, "effect_matches_output"),
        (_Mutator, {"vars": ["y"]}, "transform_pure_and_deterministic"),
        (_BadInverse, {"vars": ["y"]}, "inverse_roundtrip"),
        (_NeedsTarget, {"vars": ["y"]}, "absent_target_tolerated"),
        (_HiddenDefault, {"vars": ["y"]}, "catalog_entry"),
        (_FreqLiar, {"dim": "time", "freq": "W-SUN"}, "effect_matches_output"),
        (_UnitsLiar, {"vars": ["y"]}, "effect_matches_output"),
    ]
    for step, settings, failing in cases:
        report = check_step(
            step, weekly.copy(deep=True), settings, schema=schema, raise_on_failure=False
        )
        names = [r.name for r in report.failures]
        assert failing in names, (step.id, report.summary())
        with pytest.raises(ContractViolation) as err:
            report.raise_for_failures()
        assert failing in str(err.value)
        assert report.to_json()["ok"] is False


# ------------------------------------------------------------------------------------- models


@pytest.mark.mapi_contract
@pytest.mark.parametrize("model", ["toy.bayes_linear", "toy.linear", "toy.quantile"])
def test_toy_models_pass(
    model: str,
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    data, fx = panel
    plugin = registry.model(f"{model}@^1")
    fixture = ModelFixture(
        spec=e33_spec(model=f"{model}@^1", metrics="rmse, mape"),
        data=data,
        side_tables={"fx": fx},
        raw_schema=raw_schema,
        registry=registry,
    )
    report = check_model(plugin, fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    ran = {r.name for r in report.results if not r.skipped}
    assert {
        "fit",
        "bundle_roundtrip_no_pickle",
        "shuffled_scoring",
        "new_level_policies",
        "extend_typed_errors",
        "context_window_equivalence",
        "evaluate_path_equals_score_path",
        "representation_aggregation",
    } <= ran
    if model == "toy.linear":
        assert not report.get("additive_contributions").skipped
    if model == "toy.quantile":
        assert "raised CAPABILITY_UNSUPPORTED" in report.get("representation_aggregation").detail


def test_supplied_side_tables_are_named_by_kit_requests(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    """With ``score_policy: supplied`` every score request must name the table; the kit's
    own requests do so for the tables the fixture provides."""
    data, fx = panel
    doc = e33_spec(model="toy.linear@^1", metrics="rmse, mape").model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )
    doc["data"]["side_tables"]["fx"]["score_policy"] = "supplied"
    spec = ModelSpec.model_validate(doc)
    fixture = ModelFixture(
        spec=spec, data=data, side_tables={"fx": fx}, raw_schema=raw_schema, registry=registry
    )
    report = check_model(registry.model("toy.linear@^1"), fixture, raise_on_failure=False)
    assert report.ok, report.summary()
    assert not report.get("shuffled_scoring").skipped


class _Leaky(toy.LinearModel):
    """Ignores its fit and the new-level strategy; non-deterministic predictions."""

    id = "test.leaky"

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        tree = super().score(fitted, ds, req, ctx)
        pred = tree["predictions"].to_dataset()
        noise = np.random.default_rng().normal(size=pred["y_hat"].shape)
        pred["y_hat"] = pred["y_hat"] + noise
        return xr.DataTree.from_dict({"predictions": pred})


class _NoFit(toy.LinearModel):
    id = "test.nofit"

    def fit(self, ds: xr.Dataset, s: Any, cfg: FitConfig, ctx: FitContext) -> FitOutput:
        raise RuntimeError("cannot fit")


def test_broken_models_fail(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    data, fx = panel
    fixture = ModelFixture(
        spec=e33_spec(model="test.leaky@^1", metrics="rmse"),
        data=data,
        side_tables={"fx": fx},
        raw_schema=raw_schema,
        registry=registry,
    )
    report = check_model(_Leaky, fixture, raise_on_failure=False)
    failed = {r.name for r in report.failures}
    assert {"bundle_roundtrip_no_pickle", "shuffled_scoring", "seed_determinism"} & failed
    broken = ModelFixture(
        spec=e33_spec(model="test.nofit@^1", metrics="rmse"),
        data=data,
        side_tables={"fx": fx},
        raw_schema=raw_schema,
        registry=registry,
    )
    nofit = check_model(_NoFit, broken, raise_on_failure=False)
    assert [r.name for r in nofit.failures] == ["fit"]
    assert all(r.skipped for r in nofit.results if r.name not in ("manifest", "fit"))
    only = check_model(
        toy.LinearModel,
        ModelFixture(
            spec=e33_spec(model="toy.linear@^1", metrics="rmse"),
            data=data,
            side_tables={"fx": fx},
            raw_schema=raw_schema,
            registry=registry,
        ),
        checks=["manifest", "fit", "unsupported_mode"],
    )
    assert [r.name for r in only.results] == ["manifest", "fit", "unsupported_mode"]


def test_pytest_plugin_fixtures(
    mapi_check_step: Any, mapi_check_model: Any, mapi_toy_registry: Registry, mapi_panel: Any
) -> None:
    assert mapi_check_step is check_step and mapi_check_model is check_model
    assert mapi_toy_registry.step("toy.lag@^1") is toy.LagStep
    data, fx = mapi_panel
    assert {"revenue_local", "spend_local"} <= set(data.data_vars) and "rate" in fx
    assert StepKind.VALUE.value == "value"


class _SameName(toy.LinearModel):
    """Names its output after the bound target ('y' comes back as 'revenue_local')."""

    id = "test.samename"
    outputs: ClassVar[dict[str, Any]] = {"y": {"dims": ["geo", "time"], "units_of": "y"}}

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        pred = super().score(fitted, ds, req, ctx)["predictions"].to_dataset()
        return xr.DataTree.from_dict({"predictions": pred.rename(y_hat="y")})


def test_output_units_follow_the_reverse_binding(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    data, fx = panel
    fixture = ModelFixture(
        spec=e33_spec(model="test.samename@^1", metrics="rmse"),
        data=data,
        side_tables={"fx": fx},
        raw_schema=raw_schema,
        registry=registry,
    )
    report = check_model(_SameName, fixture, checks=["manifest", "fit", "output_space_and_units"])
    result = report.get("output_space_and_units")
    assert result.passed and not result.skipped, report.summary()


class _ImplicitTarget(toy.QuantileModel):
    """Declares y_hat without units_of: the harness inverts it through the single target."""

    id = "test.implicit"
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["geo", "time"]}}


def test_output_space_covers_outputs_that_inherit_the_target(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    data, fx = panel
    fixture = ModelFixture(
        spec=e33_spec(model="test.implicit@^1", metrics="rmse"),
        data=data,
        side_tables={"fx": fx},
        raw_schema=raw_schema,
        registry=registry,
    )
    report = check_model(
        _ImplicitTarget, fixture, checks=["manifest", "fit", "output_space_and_units"]
    )
    result = report.get("output_space_and_units")
    assert result.passed and not result.skipped, report.summary()
    assert "y_hat in weekly (EUR)" in result.detail


# ------------------------------------------------------------- typed errors on extend dims


def _panel_template(time_kind: str, max_extension: int | None = None) -> dict[str, Any]:
    time: dict[str, Any] = {"kind": time_kind, "policy": "extend"}
    if max_extension is not None:
        time["max_extension"] = max_extension
    return {
        "template": {
            "dims": {
                "geo": {
                    "kind": "categorical",
                    "policy": "new_levels",
                    "new_level_strategy": "pooled",
                },
                "time": time,
            },
            "vars": {
                "y": {
                    "dims": ["geo", "time"],
                    "dtype": "float64",
                    "role": "target",
                    "missing": "allow",
                }
            },
        }
    }


class _IntTime(toy.LinearModel):
    """The toy linear model over an integer extend dim (observation index)."""

    id = "test.inttime"
    schema_template = _panel_template("integer")


class _Bounded(toy.LinearModel):
    """The toy linear model whose time dim supports at most 6 future periods (C08-style)."""

    id = "test.bounded"
    schema_template = _panel_template("time", max_extension=6)


INT_SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: int-panel, project: kit}
data:
  dataset: dataset://kit/int_panel@latest
  bind: {dims: {geo: geo, time: t}, vars: {y: y}}
  split: {kind: time_holdout, dim: time, last: 4}
pipeline:
  - {id: scale_x, use: toy.scale@^1, with: {vars: [x], over: [time], unseen_levels: pooled}}
model:
  use: test.inttime@^1
  fit: {mode: train_only, seed: 3}
evaluate: {metrics: [rmse]}
"""

TYPED = ["manifest", "fit", "extend_typed_errors"]


def test_extend_errors_cover_integer_dims(registry: Registry) -> None:
    from mapi_sdk.schema import infer_schema

    rng = np.random.default_rng(1)
    data = xr.Dataset(
        {
            "y": (("geo", "t"), rng.uniform(1, 2, (3, 24))),
            "x": (("geo", "t"), rng.uniform(0, 1, (3, 24))),
        },
        coords={"geo": ["a", "b", "c"], "t": np.arange(100, 148, 2)},  # step 2
    )
    schema = infer_schema(data, targets=["y"], policies={"t": "extend"})
    fixture = ModelFixture(spec=INT_SPEC, data=data, raw_schema=schema, registry=registry)
    report = check_model(_IntTime, fixture, checks=TYPED)
    result = report.get("extend_typed_errors")
    assert result.passed and not result.skipped, report.summary()
    assert "int64" in result.detail and "NON_CONTIGUOUS_EXTENSION" in result.detail


def _toy_fixture(
    spec: ModelSpec, panel: tuple[xr.Dataset, xr.Dataset], schema: DataSchema, reg: Registry
) -> ModelFixture:
    data, fx = panel
    return ModelFixture(
        spec=spec, data=data, side_tables={"fx": fx}, raw_schema=schema, registry=reg
    )


def test_horizon_beyond_max_extension_is_a_typed_error(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    spec = e33_spec(model="test.bounded@^1", mode="train_only", metrics="rmse")
    report = check_model(
        _Bounded,
        _toy_fixture(spec, panel, raw_schema, registry),
        checks=["manifest", "fit", "horizon_exceeds_fit"],
    )
    result = report.get("horizon_exceeds_fit")
    assert result.passed and not result.skipped, report.summary()
    assert "HORIZON_EXCEEDS_FIT for 7 periods" in result.detail
    # without max_extension the check does not apply
    plain = check_model(
        toy.LinearModel,
        _toy_fixture(e33_spec(model="toy.linear@^1", metrics="rmse"), panel, raw_schema, registry),
        checks=["manifest", "fit", "horizon_exceeds_fit", "recursive_unsupported"],
    )
    assert plain.get("horizon_exceeds_fit").skipped
    assert plain.get("recursive_unsupported").skipped


def test_forecast_without_a_future_covariate_is_a_typed_error(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    spend = raw_schema.vars["spend_local"].model_copy(update={"future_required": True})
    schema = raw_schema.model_copy(update={"vars": {**raw_schema.vars, "spend_local": spend}})
    spec = e33_spec(model="toy.linear@^1", mode="train_only", metrics="rmse")
    report = check_model(
        toy.LinearModel,
        _toy_fixture(spec, panel, schema, registry),
        checks=["manifest", "fit", "missing_future_covariate"],
    )
    result = report.get("missing_future_covariate")
    assert result.passed and not result.skipped, report.summary()
    assert "spend_local" in result.detail and "MISSING_FUTURE_COVARIATE" in result.detail


def test_horizon_longer_than_a_target_lag_is_a_typed_error(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    doc = e33_spec(model="toy.linear@^1", mode="train_only", metrics="rmse").model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )
    lags = next(s for s in doc["pipeline"] if s["id"] == "lags")
    lags["with"] = {"vars": ["y"], "along": "time", "within": ["geo"], "lags": [4, 5], "fill": 0.0}
    spec = ModelSpec.model_validate(doc)
    report = check_model(
        toy.LinearModel,
        _toy_fixture(spec, panel, raw_schema, registry),
        checks=["manifest", "fit", "recursive_unsupported"],
    )
    result = report.get("recursive_unsupported")
    assert result.passed and not result.skipped, report.summary()
    assert "horizon of 5 (minimum target lag 4)" in result.detail


# ------------------------------------------------------------- fast scoring and plots

_FAST_CAPS = toy.LinearModel.capabilities.model_copy(update={"fast_score": True})
FAST_SEEN: dict[str, Any] = {}


class _FastNoHook(toy.LinearModel):
    """Claims fast scoring but gives the kit no way to compare it with exact scoring."""

    id = "test.fastnohook"
    capabilities = _FAST_CAPS


class _FastHook(_FastNoHook):
    id = "test.fasthook"

    def check_fast_score(self, fitted: Fitted, ds: xr.Dataset, *, seed: int = 0) -> str:
        FAST_SEEN.update(ds=ds, seed=seed)
        if FAST_SEEN.get("fail"):
            raise AssertionError("fast scoring disagrees with exact scoring beyond 4 MCSE")
        return "fast == exact"


def test_fast_score_claims_are_checked_against_exact_scoring(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    checks = ["manifest", "fit", "fast_score_matches_exact"]
    results = {}
    for plugin, fail in ((_FastNoHook, False), (_FastHook, False), (_FastHook, True)):
        FAST_SEEN.clear()
        FAST_SEEN["fail"] = fail
        spec = e33_spec(model=f"{plugin.id}@^1", mode="train_only", metrics="rmse")
        report = check_model(
            plugin,
            _toy_fixture(spec, panel, raw_schema, registry),
            checks=checks,
            raise_on_failure=False,
        )
        results[(plugin.id, fail)] = report.get("fast_score_matches_exact")
    assert not results[("test.fastnohook", False)].passed
    assert "check_fast_score" in results[("test.fastnohook", False)].detail
    ok = results[("test.fasthook", False)]
    assert ok.passed and not ok.skipped and ok.detail == "fast == exact"
    assert not results[("test.fasthook", True)].passed
    # the hook sees model-stage data (weekly, scaled, lagged) and the fit seed
    assert "spend_lag" in FAST_SEEN["ds"] and FAST_SEEN["seed"] == 7
    plain = check_model(
        toy.LinearModel,
        _toy_fixture(e33_spec(model="toy.linear@^1", metrics="rmse"), panel, raw_schema, registry),
        checks=checks,
    )
    assert plain.get("fast_score_matches_exact").skipped


def _leaky_recipe() -> Any:
    from mapi_plots.recipes import Figure, RecipeSettings, recipe

    @recipe("test.leaky", title="Leaky", settings=RecipeSettings)
    def leaky(inp: Any, s: RecipeSettings) -> Figure:
        spec = {"data": {"url": "https://evil.example/x.csv"}, "mark": "point"}
        return Figure(chart=spec, datasets={}, caption="fetches data from a remote server")

    return leaky


class _SafePlots(toy.BayesLinearModel):
    id = "test.safeplots"
    plots: ClassVar[tuple[str, ...]] = ("core.posterior_forest", "core.ppc_fan")


class _UnsafePlots(toy.BayesLinearModel):
    id = "test.unsafeplots"
    plots: ClassVar[tuple[str, ...]] = ("core.posterior_forest", "test.leaky")


def test_manifest_plots_must_sanitize(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    pytest.importorskip("altair")
    reg = registry.copy()
    reg.register_plot(_leaky_recipe())
    checks = ["manifest", "fit", "plots_sanitize"]
    reports = {}
    for plugin in (_SafePlots, _UnsafePlots):
        spec = e33_spec(model=f"{plugin.id}@^1", mode="train_only", metrics="rmse")
        reports[plugin.id] = check_model(
            plugin,
            _toy_fixture(spec, panel, raw_schema, reg),
            checks=checks,
            raise_on_failure=False,
        )
    safe = reports["test.safeplots"].get("plots_sanitize")
    assert safe.passed and not safe.skipped, reports["test.safeplots"].summary()
    assert "core.posterior_forest" in safe.detail and "core.ppc_fan" in safe.detail
    unsafe = reports["test.unsafeplots"].get("plots_sanitize")
    assert not unsafe.passed and "test.leaky" in unsafe.detail, unsafe.detail
    plain = check_model(
        toy.LinearModel,
        _toy_fixture(e33_spec(model="toy.linear@^1", metrics="rmse"), panel, raw_schema, reg),
        checks=checks,
    )
    assert plain.get("plots_sanitize").skipped


# ------------------------------------------------------------- outputs vs the manifest


class _Undeclared(toy.QuantileModel):
    """Returns a variable the manifest does not declare."""

    id = "test.undeclared"

    def score(
        self, fitted: Fitted, ds: xr.Dataset, req: ScoreRequest, ctx: ScoreContext
    ) -> xr.DataTree:
        pred = super().score(fitted, ds, req, ctx)["predictions"].to_dataset()
        return xr.DataTree.from_dict({"predictions": pred.assign(extra=pred["y_hat"])})


class _ShortDims(toy.QuantileModel):
    """Declares y_hat over the quantile dim only (like the sklearn row models used to)."""

    id = "test.shortdims"
    outputs: ClassVar[dict[str, Any]] = {"y_hat": {"dims": ["quantile"], "units_of": "y"}}


class _DimsOf(_ShortDims):
    """The same, with the data dims taken from the target: matches what score returns."""

    id = "test.dimsof"
    outputs: ClassVar[dict[str, Any]] = {
        "y_hat": {"dims": ["quantile"], "dims_of": "y", "units_of": "y"}
    }


def test_returned_predictions_must_match_the_declared_outputs(
    e33_spec: Callable[..., ModelSpec],
    panel: tuple[xr.Dataset, xr.Dataset],
    raw_schema: DataSchema,
    registry: Registry,
) -> None:
    checks = ["manifest", "fit", "outputs_match_manifest"]
    results = {}
    for plugin in (toy.QuantileModel, _Undeclared, _ShortDims, _DimsOf):
        spec = e33_spec(model=f"{plugin.id}@^1", mode="train_only", metrics="rmse")
        report = check_model(
            plugin,
            _toy_fixture(spec, panel, raw_schema, registry),
            checks=checks,
            raise_on_failure=False,
        )
        results[plugin.id] = report.get("outputs_match_manifest")
    assert results["toy.quantile"].passed and not results["toy.quantile"].skipped
    assert not results["test.undeclared"].passed
    assert "['extra']" in results["test.undeclared"].detail
    short = results["test.shortdims"]
    assert not short.passed and "declared dims []" in short.detail, short.detail
    assert results["test.dimsof"].passed, results["test.dimsof"].detail


# ---------------------------------------------------------------------------- About (M3.5)


def test_the_about_check(
    weekly: xr.Dataset, raw_target_schema: Callable[[xr.Dataset], DataSchema]
) -> None:
    from mapi_sdk.testing import check_about
    from mapi_spec.about import About

    settings = {"vars": ["y"]}
    schema = raw_target_schema(weekly)
    lenient = check_step(toy.Log1pStep, weekly, settings, schema=schema)
    assert lenient.get("about").skipped and "requires one" in lenient.get("about").detail
    strict = check_step(toy.Log1pStep, weekly, settings, schema=schema, require_about=True,
                        raise_on_failure=False)  # fmt: skip
    assert [r.name for r in strict.failures] == ["about"]

    card = About.model_validate(About.model_json_schema()["examples"][0])

    class _Described(toy.Log1pStep):
        about: ClassVar[About | None] = card

    described = check_step(_Described, weekly, settings, schema=schema, require_about=True)
    assert described.get("about").passed and "complete" in described.get("about").detail

    class _Jargon(toy.Log1pStep):
        about: ClassVar[About | None] = card.model_copy(
            update={"summary_plain": "Takes the posterior of a stationarity transform here."}
        )

    jargon = check_step(_Jargon, weekly, settings, schema=schema, raise_on_failure=False)
    assert [r.name for r in jargon.failures] == ["about"]
    assert "jargon" in jargon.get("about").detail

    report = check_about(_Described, toy.Log1pStep, raise_on_failure=False)
    assert [r.passed for r in report.results] == [True, False]
    with pytest.raises(ContractViolation):
        check_about(toy.LinearModel)
    assert check_about(_Described, known_ids=["core.calendar"]).ok
    assert not check_about(_Described, known_ids=[], raise_on_failure=False).ok
