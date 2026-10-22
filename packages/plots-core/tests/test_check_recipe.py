"""The plot recipe contract kit (v0.18): every built-in recipe passes it, and each check fails
with its own name and a plain reason."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("xarray")
pytest.importorskip("altair")
pytest.importorskip("plotly")

import altair as alt
import pandas as pd
from mapi_plots.examples import example_inputs
from mapi_plots.palette import ESTIMATE
from mapi_plots.recipes import Figure, Recipe, RecipeSettings, all_recipes, named, recipe
from mapi_plots.testing import CHECKS, RecipeContractError, RecipeReport, check_recipe
from mapi_spec.plot_abouts import PLOT_ABOUTS
from mapi_spec.plots import DataRef, PlotSpec
from pydantic import BaseModel, ConfigDict, Field

pytestmark = pytest.mark.unit

RECIPES = {r.id: r for r in all_recipes()}
ABOUT = PLOT_ABOUTS["core.fit_vs_actual"]


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [2.0, 1.0, 3.0]})


def _example() -> tuple[Any, dict[str, Any]]:
    return example_inputs()["core.fit_vs_actual"]


def _make(
    id: str = "acme.line",
    *,
    settings: type[RecipeSettings] = RecipeSettings,
    caption: str = "The value of y at each x.",
    colour: str = ESTIMATE,
    **kw: Any,
) -> Recipe[Any]:
    kw.setdefault("about", ABOUT)
    kw.setdefault("example", _example)

    @recipe(id, title="Line", settings=settings, **kw)
    def line(inp: Any, s: RecipeSettings) -> Figure:
        chart = alt.Chart(named("d")).mark_line(color=colour).encode(x="x:Q", y="y:Q")
        return Figure(chart, {"d": _frame()}, caption)

    return line


def _failed(report: RecipeReport) -> list[str]:
    return [name for name, reason in report.checks if reason is not None]


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_every_builtin_recipe_passes_the_kit(recipe_id: str) -> None:
    report = check_recipe(RECIPES[recipe_id])
    assert report.ok
    assert [name for name, _ in report.checks] == list(CHECKS)


def test_a_good_plugin_recipe_passes_and_reports_every_check() -> None:
    report = check_recipe(_make())
    assert report.ok and report.failures == [] and report.warnings == []
    assert "[PASS] example_sanitized" in report.summary()
    as_json = report.to_json()
    assert as_json["ok"] is True
    assert [c["name"] for c in as_json["checks"]] == list(CHECKS)


def test_failures_raise_an_assertion_with_the_summary() -> None:
    bad = _make(about=None)
    with pytest.raises(RecipeContractError) as exc:
        check_recipe(bad)
    assert isinstance(exc.value, AssertionError)
    assert "[FAIL] about" in str(exc.value)
    assert exc.value.report.reason("about") == "the item has no About card"


def test_about_is_optional_without_require_about() -> None:
    report = check_recipe(_make(about=None), require_about=False, raise_on_failure=False)
    assert report.ok


def test_an_incomplete_about_fails_even_without_require_about() -> None:
    about = ABOUT.model_copy(update={"summary_plain": "Too short"})
    report = check_recipe(_make(about=about), require_about=False, raise_on_failure=False)
    assert _failed(report) == ["about"]
    assert "summary_plain" in (report.reason("about") or "")


def test_bad_tags_fail_the_catalog_entry_check() -> None:
    report = check_recipe(_make(tags=["Not A Slug", "ok"]), raise_on_failure=False)
    assert _failed(report) == ["catalog_entry"]
    assert "'Not A Slug'" in (report.reason("catalog_entry") or "")


def test_a_long_pack_label_fails_the_catalog_entry_check() -> None:
    report = check_recipe(_make(pack_label="x" * 41), raise_on_failure=False)
    assert _failed(report) == ["catalog_entry"]


@pytest.mark.parametrize("ns", ["core", "sci", "lib", "team", "t_acme", "tenant_x"])
def test_reserved_and_builtin_namespaces_fail_for_a_plugin(ns: str) -> None:
    report = check_recipe(_make(f"{ns}.line"), raise_on_failure=False)
    assert _failed(report) == ["namespace"]
    assert "namespace" in (report.reason("namespace") or "").lower()


def test_an_unsafe_settings_schema_fails() -> None:
    class Unsafe(RecipeSettings):
        label: str = Field("a", pattern=r"^(a+)+$")

    report = check_recipe(_make(settings=Unsafe), raise_on_failure=False)
    assert _failed(report) == ["settings_schema"]
    assert "pattern" in (report.reason("settings_schema") or "")


def test_an_oversized_settings_schema_fails() -> None:
    fields: dict[str, Any] = {
        f"opt_{i}": (int, Field(0, description="Long help text. " * 110)) for i in range(10)
    }
    from pydantic import create_model

    big = create_model("Big", __base__=RecipeSettings, **fields)
    report = check_recipe(_make(settings=big), raise_on_failure=False)
    assert _failed(report) == ["settings_schema"]
    assert "16384" in (report.reason("settings_schema") or "")


def test_a_setting_without_default_fails() -> None:
    class Needs(RecipeSettings):
        var: str

    report = check_recipe(_make(settings=Needs), raise_on_failure=False)
    assert "settings_defaults" in _failed(report)
    assert "var" in (report.reason("settings_defaults") or "")


def test_settings_that_accept_unknown_keys_fail() -> None:
    class Loose(BaseModel):
        model_config = ConfigDict(extra="ignore", frozen=True)

        title: str | None = None
        width: int = 480
        height: int = 180

    report = check_recipe(_make(settings=Loose), raise_on_failure=False)  # type: ignore[arg-type]
    assert _failed(report) == ["settings_extra_refused"]


def test_a_recipe_without_example_fails_and_skips_the_example_checks() -> None:
    @recipe("acme.bare", title="Bare", about=ABOUT)
    def bare(inp: Any, s: RecipeSettings) -> Figure:
        raise AssertionError("never rendered")

    report = check_recipe(bare, raise_on_failure=False)
    assert _failed(report) == ["example_renders", "example_inline", "example_sanitized", "caption"]
    assert "example=" in (report.reason("example_renders") or "")
    assert report.reason("caption") == "not checked: the example did not render"


def test_an_example_that_crashes_fails_with_the_error() -> None:
    def boom() -> tuple[Any, dict[str, Any]]:
        raise RuntimeError("no data here")

    report = check_recipe(_make(example=boom), raise_on_failure=False)
    assert "example_renders" in _failed(report)
    assert report.reason("example_renders") == "RuntimeError: no data here"


def test_an_empty_caption_fails() -> None:
    report = check_recipe(_make(caption=""), raise_on_failure=False)
    assert _failed(report) == ["caption"]


class _Patched(Recipe[Any]):
    """A recipe whose example is replaced (the kit only reads ``example_spec()``)."""

    __slots__ = ()

    def example_spec(self, settings: Any = None) -> PlotSpec | None:
        return _PATCHED[self.id]


_PATCHED: dict[str, PlotSpec] = {}


def _patched(id: str, spec: PlotSpec) -> Recipe[Any]:
    base = _make(id)
    _PATCHED[id] = spec
    return _Patched(
        id=base.id,
        title=base.title,
        settings=base.settings,
        fn=base.fn,
        about=base.about,
        example=base.example,
    )


def test_an_example_with_referenced_datasets_fails() -> None:
    good = _make("acme.ref").example_spec()
    assert good is not None
    ref = good.model_copy(
        update={"datasets": {"d": DataRef(kind="data_ref", path="ui/plotdata/d.arrow")}}
    )
    report = check_recipe(_patched("acme.ref", ref), raise_on_failure=False)
    assert _failed(report) == ["example_inline"]


def test_an_example_the_sanitizer_changes_fails() -> None:
    good = _make("acme.unsanitized").example_spec()
    assert good is not None
    spec = {k: v for k, v in good.spec.items() if k != "$schema"}
    report = check_recipe(
        _patched("acme.unsanitized", good.model_copy(update={"spec": spec})),
        raise_on_failure=False,
    )
    assert _failed(report) == ["example_sanitized"]
    assert "changes" in (report.reason("example_sanitized") or "")


def test_an_example_the_sanitizer_refuses_fails() -> None:
    good = _make("acme.unsafe").example_spec()
    assert good is not None
    spec = {**good.spec, "usermeta": {"embedOptions": {"loader": {"target": "_blank"}}}}
    report = check_recipe(
        _patched("acme.unsafe", good.model_copy(update={"spec": spec})), raise_on_failure=False
    )
    assert _failed(report) == ["example_sanitized"]
    assert "refuses" in (report.reason("example_sanitized") or "")


def test_colours_outside_the_palette_are_a_warning_only() -> None:
    report = check_recipe(_make("acme.pink", colour="#ff00aa"))
    assert report.ok
    assert [name for name, _ in report.warnings] == ["colours"]
    assert "#ff00aa" in report.warnings[0][1]
    assert "[WARN] colours" in report.summary()
