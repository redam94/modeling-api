"""Recipe previews (v0.18, D21): a ``render`` run with ``RenderJob.recipe`` draws the recipe's
own catalog example with the playground's settings and writes only ``preview/plot.json``
(sanitized, every dataset inline). No input is read and no image is drawn."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from mapi_plots.recipes import get_recipe
from mapi_runner.worker.render import recipe_preview
from mapi_sdk.registry import Registry, default_registry
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.executor import RunKind
from mapi_spec.jobs import PREVIEW_OUTPUT, RecipePreview
from mapi_spec.plots import PlotSpec
from mapi_spec.run import ErrorPayload, RunnerExit
from runner_helpers import make_run, run_supervisor

FAN = "core.ppc_fan@1.0.0"


def _preview(ref: str = FAN, registry: Any = None, **settings: Any) -> PlotSpec:
    return recipe_preview(registry or default_registry(), RecipePreview(ref=ref, settings=settings))


def test_no_settings_is_the_catalog_example() -> None:
    example = get_recipe("core.ppc_fan").example_spec()
    assert example is not None
    plot = _preview()
    assert plot.spec == example.spec and plot.title == example.title
    assert set(plot.datasets) == set(example.datasets)


def test_settings_are_merged_over_the_example() -> None:
    plot = _preview(title="Sales: fit and forecast", width=320)
    assert plot.title == "Sales: fit and forecast"
    assert plot.datasets and all(d.kind == "inline_arrow" for d in plot.datasets.values())
    assert plot.provenance.recipe == FAN


@pytest.mark.parametrize(
    ("settings", "pointer"),
    [({"width": 5}, "/settings/width"), ({"not_a_setting": 1}, "/settings/not_a_setting")],
)
def test_refused_settings_are_schema_invalid_with_a_pointer(
    settings: dict[str, Any], pointer: str
) -> None:
    with pytest.raises(MapiError) as info:
        _preview(**settings)
    assert info.value.code is ErrorCode.SCHEMA_INVALID
    assert info.value.pointer == pointer


def _registry_with(recipe: Any) -> Registry:
    registry = Registry()
    registry.register_plot(recipe)
    return registry


def test_a_recipe_without_an_example_is_mode_not_supported() -> None:
    bare = dataclasses.replace(get_recipe("core.ppc_fan"), id="acme.fan", example=None)
    with pytest.raises(MapiError) as info:
        _preview("acme.fan@1.0.0", _registry_with(bare))
    assert info.value.code is ErrorCode.MODE_NOT_SUPPORTED


class _OldRecipe:
    """A recipe object written before v0.18: ``example_spec()`` takes no settings."""

    def __init__(self) -> None:
        self._fan = get_recipe("core.ppc_fan")

    def catalog_entry(self) -> Any:
        return self._fan.catalog_entry()

    def render(self, data: Any, **options: Any) -> PlotSpec:
        return self._fan.render(data, **options)

    def example_spec(self) -> PlotSpec | None:
        return self._fan.example_spec()


def test_an_old_recipe_previews_without_settings_only() -> None:
    registry = _registry_with(_OldRecipe())
    assert _preview(registry=registry).datasets
    with pytest.raises(MapiError) as info:
        _preview(registry=registry, title="x")
    assert info.value.code is ErrorCode.MODE_NOT_SUPPORTED


def test_a_preview_that_is_not_self_contained_is_refused() -> None:
    class _Remote(_OldRecipe):
        def example_spec(self) -> PlotSpec | None:
            plot = self._fan.example_spec()
            assert plot is not None
            name = next(iter(plot.datasets))
            doc = plot.model_dump(mode="json")
            doc["datasets"][name] = {"kind": "data_ref", "path": "outputs/x.arrow"}
            return PlotSpec.model_validate(doc)

    with pytest.raises(MapiError) as info:
        _preview(registry=_registry_with(_Remote()))
    assert info.value.code is ErrorCode.PLOT_UNSAFE


# ============================================================================ end to end


def test_preview_run_writes_only_the_preview(gcs: Any) -> None:
    result = run_supervisor(
        make_run(gcs, RunKind.RENDER, {"recipe": {"ref": FAN, "settings": {"title": "Try it"}}})
    )
    assert result.code == RunnerExit.OK, result.stderr[-2000:]
    objects = set(result.manifest().objects)
    assert objects == {PREVIEW_OUTPUT}
    plot = PlotSpec.model_validate_json(result.output(PREVIEW_OUTPUT))
    assert plot.title == "Try it"
    assert all(d.kind == "inline_arrow" for d in plot.datasets.values())


def test_preview_run_reports_bad_settings_with_the_pointer(gcs: Any) -> None:
    result = run_supervisor(
        make_run(gcs, RunKind.RENDER, {"recipe": {"ref": FAN, "settings": {"height": 1}}})
    )
    assert result.code == RunnerExit.USER_ERROR
    err = ErrorPayload.model_validate(result.of("error")[0].payload)
    assert [(i.code, i.pointer) for i in err.errors] == [
        (ErrorCode.SCHEMA_INVALID, "/settings/height")
    ]
