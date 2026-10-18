"""The catalog side of every built-in recipe: a complete About card, a summary, and an
example render that is self-contained and passes the server's sanitizer."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("xarray")
pytest.importorskip("altair")
pytest.importorskip("plotly")

from mapi_plots.examples import example_inputs
from mapi_plots.recipes import Figure, RecipeSettings, all_recipes, recipe
from mapi_plots.sanitize import sanitize_plot
from mapi_sdk.registry import Registry
from mapi_spec.about import about_issues
from mapi_spec.plot_abouts import PLOT_ABOUTS
from mapi_spec.plots import PlotExamples

pytestmark = pytest.mark.unit

RECIPES = {r.id: r for r in all_recipes()}


def test_every_builtin_recipe_has_a_card_and_example_data() -> None:
    assert set(PLOT_ABOUTS) == set(RECIPES)
    assert set(example_inputs()) == set(RECIPES)


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_catalog_entry_carries_the_card_and_summary(recipe_id: str) -> None:
    entry = RECIPES[recipe_id].catalog_entry()
    assert entry.about is PLOT_ABOUTS[recipe_id]
    assert not about_issues(entry.about, known_ids=RECIPES)
    assert entry.summary and len(entry.summary) <= 300
    assert set(entry.about.related) <= set(RECIPES)


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_example_is_inline_and_survives_the_sanitizer(recipe_id: str) -> None:
    r = RECIPES[recipe_id]
    spec = r.example_spec()
    assert spec is not None and spec.id == recipe_id
    PlotExamples(plots={r.ref: spec})  # inline datasets only
    clean = sanitize_plot(spec.model_dump(mode="json")).plot
    assert clean.datasets.keys() == spec.datasets.keys()


def test_a_recipe_example_and_about_can_be_given_explicitly() -> None:
    about = PLOT_ABOUTS["core.ppc_fan"].model_copy(update={"title": "My fan"})
    data, options = example_inputs()["core.ppc_fan"]
    base = RECIPES["core.ppc_fan"]

    @recipe("acme.fan", title="Fan", about=about, example=lambda: (data, options))
    def fan(inp: Any, s: RecipeSettings) -> Figure:
        return base.fn(inp, base.parse_settings({}))

    assert fan.catalog_entry().about is about
    spec = fan.example_spec()
    assert spec is not None and spec.id == "acme.fan"


def test_registry_examples_skip_a_failing_recipe() -> None:
    def boom() -> tuple[Any, dict[str, Any]]:
        raise RuntimeError("no data")

    @recipe("acme.broken", title="Broken", example=boom)
    def broken(inp: Any, s: RecipeSettings) -> Figure:
        raise AssertionError("never rendered")

    @recipe("acme.none", title="Without example")
    def plain(inp: Any, s: RecipeSettings) -> Figure:
        raise AssertionError("never rendered")

    reg = Registry()
    reg.register(RECIPES["core.ppc_fan"])
    reg.register(broken)
    reg.register(plain)
    errors: list[str] = []
    out = reg.plot_examples(on_error=lambda ref, exc: errors.append(f"{ref}: {exc}"))
    assert set(out.plots) == {"core.ppc_fan@1.0.0"}
    assert errors == ["acme.broken@1.0.0: no data"]
