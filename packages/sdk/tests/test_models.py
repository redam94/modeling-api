"""Model plugin contract helpers: manifests, contexts, Fitted and CustomModel defaults."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
from mapi_sdk.errors import BundleError, RunCancelled
from mapi_sdk.models import (
    CustomModel,
    FitContext,
    FitOutput,
    Fitted,
    ModelPlugin,
    ModelSettings,
    ScoreContext,
    build_manifest,
    new_level_mask,
)
from mapi_sdk.steps import CancelToken
from mapi_spec import Capabilities, DataSchema, Representation
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.spec import FitConfig

CAPS = Capabilities(
    inference="point", representation=Representation.POINT, score_modes=frozenset({"predict"})
)
SCHEMA = DataSchema.model_validate(
    {
        "dims": {
            "g": {"kind": "categorical", "policy": "new_levels", "new_level_strategy": "zero"}
        },
        "vars": {"y": {"dims": ["g"], "dtype": "float64", "role": "target"}},
    }
)


class _Settings(ModelSettings):
    alpha: float = 1.0


class _Bare(CustomModel[_Settings]):
    id = "test.bare"
    version = "0.1.0"
    Settings = _Settings
    capabilities = CAPS
    schema_template = SCHEMA


def test_build_manifest_variants() -> None:
    m1 = build_manifest(id="test.a", version="1.0.0", capabilities=CAPS, schema_template=SCHEMA)
    assert m1.schema_template.resolve() == SCHEMA and m1.settings_schema == {}
    m2 = build_manifest(
        id="test.b",
        version="1.0.0",
        capabilities=CAPS,
        schema_template=m1.schema_template.model_dump(),
        settings_model=_Settings,
        outputs={"y_hat": {"dims": ["g"], "units_of": "y"}},
        derived=[{"name": "contrib", "dims": ["g"]}],
        plots=["core.fit_vs_actual"],
        fit_schema={"type": "object"},
        description="test",
    )
    assert m2.outputs["y_hat"].units_of == "y" and m2.derived[0].name == "contrib"
    assert m2.settings_schema["properties"]["alpha"]["default"] == 1.0
    assert (
        build_manifest(
            id="test.c", version="1.0.0", capabilities=CAPS, schema_template=m1.schema_template
        ).schema_template
        == m1.schema_template
    )


def test_custom_model_defaults_and_settings() -> None:
    model = _Bare()
    assert isinstance(model, ModelPlugin)
    assert _Bare.ref() == "test.bare@0.1.0"
    assert _Bare.catalog_entry().manifest.id == "test.bare"
    s = model.parse_settings({"alpha": 2.0})
    assert s.alpha == 2.0 and model.parse_settings(s) is s
    assert model.parse_settings(_Settings(alpha=3.0).model_copy()).alpha == 3.0
    assert model.parse_settings(None).alpha == 1.0
    with pytest.raises(MapiError) as err:
        model.parse_settings({"nope": 1})
    assert err.value.code is ErrorCode.SETTINGS_INVALID
    fitted = Fitted(manifest=_Bare.manifest(), settings={}, fit_config=FitConfig())
    ds = xr.Dataset()
    ctx = ScoreContext()
    with pytest.raises(NotImplementedError):
        model.fit(ds, s, FitConfig(), FitContext())
    with pytest.raises(NotImplementedError):
        model.score(fitted, ds, None, ctx)  # type: ignore[arg-type]
    for call in (
        lambda: model.derive(fitted, "x", ds, ctx),
        lambda: model.objective(fitted, ds, ctx),
        lambda: model.recommend(fitted, ds, 1, ctx),
    ):
        with pytest.raises(NotImplementedError):
            call()
    assert model.diagnostics(fitted, ds) == []
    fo = FitOutput()
    assert model.postprocess(fo, ds, s) is fo


def test_contexts_and_fitted() -> None:
    progress: list[tuple[float, str]] = []
    logs: list[str] = []
    token = CancelToken()
    fctx = FitContext(
        seed=3, on_progress=lambda f, m: progress.append((f, m)), log=logs.append, cancel=token
    )
    fctx.progress(1.5, "done")
    fctx.info("hi")
    fctx.check_cancelled()
    assert progress == [(1.0, "done")] and logs == ["hi"]
    assert fctx.rng.integers(100) == np.random.default_rng(3).integers(100)
    FitContext().progress(0.5)
    token.cancel()
    with pytest.raises(RunCancelled):
        fctx.check_cancelled()
    sctx = ScoreContext(schema=SCHEMA, log=logs.append, cancel=CancelToken())
    sctx.info("s")
    sctx.check_cancelled()
    assert sctx.strategy("g") == "zero" and sctx.strategy("other") == "error"
    assert ScoreContext().strategy("g") == "error"
    fitted = Fitted(manifest=_Bare.manifest(), settings={}, fit_config=FitConfig())
    assert fitted.representation is Representation.POINT
    with pytest.raises(BundleError):
        _ = fitted.posterior
    ds = xr.Dataset(coords={"g": ["a", "b"]})
    assert new_level_mask(ds, "g").values.tolist() == [False, False]
    flagged = ds.assign_coords(g__is_new=("g", [False, True]))
    assert new_level_mask(flagged, "g").values.tolist() == [False, True]
