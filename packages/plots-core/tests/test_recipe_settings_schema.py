"""The catalog side of a recipe's settings (v0.18): a safe JSON Schema of at most 16 KiB, its
tags and the pack name the catalog card shows."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("xarray")
pytest.importorskip("altair")
pytest.importorskip("plotly")

from jsonschema import Draft202012Validator
from mapi_plots.recipes import Figure, RecipeSettings, all_recipes, recipe
from mapi_plots.recipes._base import BUILTIN_PACK_LABELS, recipe_tags, settings_schema_of
from mapi_spec.schema_safety import SETTINGS_SCHEMA_MAX_BYTES, sanitize_schema
from pydantic import Field, create_model

pytestmark = pytest.mark.unit

RECIPES = {r.id: r for r in all_recipes()}

OMITTED: dict[str, str] = {}
"""Built-in recipes whose settings schema the catalog leaves out, with the reason (none: every
built-in schema fits in 16 KiB; a recipe added here must say why)."""


def _size(schema: Any) -> int:
    return len(json.dumps(schema, separators=(",", ":"), ensure_ascii=False).encode())


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_every_builtin_entry_publishes_its_settings_schema(recipe_id: str) -> None:
    r = RECIPES[recipe_id]
    entry = r.catalog_entry()
    if recipe_id in OMITTED:
        assert entry.settings_schema is None
        return
    schema = entry.settings_schema
    assert schema is not None
    assert _size(schema) <= SETTINGS_SCHEMA_MAX_BYTES
    # the built-in schema is published untouched: nothing in it is unsafe
    assert schema == r.settings.model_json_schema()
    assert sanitize_schema(schema, mode="refuse", where="")[1] == []
    validator = Draft202012Validator(schema)
    assert validator.is_valid({}), "the playground's first form (all defaults) is valid"
    assert not validator.is_valid({"no_such_setting": 1})
    assert validator.is_valid({"title": "My chart", "width": 600})
    assert not validator.is_valid({"width": 5})


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_every_builtin_entry_has_a_pack_label_and_slug_tags(recipe_id: str) -> None:
    r = RECIPES[recipe_id]
    entry = r.catalog_entry()
    assert entry.pack_label == BUILTIN_PACK_LABELS[recipe_id.split(".", 1)[0]]
    assert entry.tags == list(dict.fromkeys(r.tags))[:8]


def test_the_catalog_json_carries_the_new_fields() -> None:
    doc = RECIPES["core.ppc_fan"].catalog_entry().model_dump(mode="json")
    assert doc["pack_label"] == "Core"
    assert doc["settings_schema"]["type"] == "object"
    assert "tags" not in doc  # empty lists are left out (catalog digest stays stable)
    assert RECIPES["mmm.roi"].catalog_entry().model_dump(mode="json")["tags"] == ["mmm"]


def test_every_call_returns_a_fresh_copy() -> None:
    a = settings_schema_of(RECIPES["core.ppc_fan"].settings)
    assert a is not None
    a["title"] = "changed"
    assert settings_schema_of(RECIPES["core.ppc_fan"].settings) != a


def _plugin(settings: type[RecipeSettings], **kw: Any) -> Any:
    @recipe("acme.schema", title="Schema", settings=settings, **kw)
    def fn(inp: Any, s: RecipeSettings) -> Figure:
        raise AssertionError("never rendered")

    return fn


def test_a_plugin_pack_label_and_tags_reach_the_catalog() -> None:
    entry = _plugin(
        RecipeSettings, pack_label="Acme lab", tags=["lab", "lab", "Bad Tag"]
    ).catalog_entry()
    assert entry.pack_label == "Acme lab"
    assert entry.tags == ["lab"]  # repeats and non-slugs left out; the kit reports them


def test_a_plugin_without_pack_label_has_none() -> None:
    assert _plugin(RecipeSettings).catalog_entry().pack_label is None


def test_tags_are_capped_at_eight() -> None:
    assert recipe_tags([f"t{i}" for i in range(12)]) == [f"t{i}" for i in range(8)]


def test_unsafe_parts_of_a_plugin_schema_are_stripped() -> None:
    class Unsafe(RecipeSettings):
        label: str = Field("a", pattern=r"^(a+)+$")

    schema = _plugin(Unsafe).catalog_entry().settings_schema
    assert schema is not None
    assert "pattern" not in schema["properties"]["label"]
    assert schema["properties"]["label"]["default"] == "a"


def test_an_oversized_schema_is_left_out_of_the_catalog() -> None:
    fields: dict[str, Any] = {
        f"opt_{i}": (int, Field(0, description="Long help text. " * 110)) for i in range(10)
    }
    big = create_model("BigSettings", __base__=RecipeSettings, **fields)
    entry = _plugin(big).catalog_entry()
    assert entry.settings_schema is None
    assert "settings_schema" not in entry.model_dump(mode="json")
