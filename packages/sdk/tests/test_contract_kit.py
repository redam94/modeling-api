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
