"""Recipe previews (v0.18): ``example_spec(settings)`` for the recipe playground, installed
``mapi.plots`` packs in ``all_recipes(installed=True)``, and the chart-template starters."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("xarray")
pytest.importorskip("altair")
pytest.importorskip("plotly")
pytest.importorskip("pyarrow")

import pyarrow as pa
from mapi_plots.examples import example_inputs
from mapi_plots.recipes import (
    Figure,
    RecipeDataError,
    RecipeSettings,
    all_recipes,
    get_recipe,
    recipe,
)
from mapi_plots.sanitize import check_plot
from mapi_plots.templates import STARTERS, starter_spec
from mapi_spec.errors import ErrorCode, Severity
from mapi_spec.plots import PlotSpec

pytestmark = pytest.mark.unit

RECIPES = {r.id: r for r in all_recipes()}
GOLDEN = Path(__file__).parent / "golden" / "recipe_examples.json"
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def _digest(spec: PlotSpec) -> str:
    """The example without its render time; colour literals masked (the palette is themed
    separately, ``test_recipes.py`` mirrors it), everything else exact."""
    doc = spec.model_dump(mode="json")
    doc["provenance"].pop("generated_at", None)
    text = _HEX.sub("#colour", json.dumps(doc, sort_keys=True, separators=(",", ":")))
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------- example_spec(settings)


def test_the_golden_covers_every_builtin_recipe() -> None:
    if os.environ.get("MAPI_REGEN_GOLDEN") == "1":  # a deliberate change to an example
        out = {r.ref: _digest(r.example_spec({}) or _missing(r.ref)) for r in RECIPES.values()}
        GOLDEN.write_text(json.dumps(dict(sorted(out.items())), indent=2) + "\n")
    golden = json.loads(GOLDEN.read_text())
    assert set(golden) == {r.ref for r in RECIPES.values()}


def _missing(ref: str) -> PlotSpec:
    raise AssertionError(f"{ref} has no example")


@pytest.mark.parametrize("recipe_id", sorted(RECIPES))
def test_empty_settings_render_the_old_example(recipe_id: str) -> None:
    r = RECIPES[recipe_id]
    golden = json.loads(GOLDEN.read_text())
    spec = r.example_spec({})
    assert spec is not None
    assert _digest(spec) == golden[r.ref]
    assert _digest(r.example_spec(None) or _missing(r.ref)) == golden[r.ref]
    data, options = example_inputs()[recipe_id]
    assert _digest(r.render(data, **options)) == golden[r.ref]


def test_settings_are_merged_over_the_example_options() -> None:
    r = RECIPES["core.residuals_over_time"]
    spec = r.example_spec({"title": "Weekly sales", "width": 640})
    base = r.example_spec()
    assert spec is not None and base is not None
    assert (spec.title, spec.spec["width"]) == ("Weekly sales", 640)
    assert base.title != "Weekly sales" and base.spec["width"] != 640
    # the example's own options stay unless the settings name them
    data, options = example_inputs()["core.residuals_over_time"]
    direct = r.render(data, **{**options, "title": "Weekly sales", "width": 640})
    assert _digest(direct) == _digest(spec)


def test_bad_settings_raise_schema_invalid_with_a_pointer() -> None:
    r = RECIPES["core.ppc_fan"]
    with pytest.raises(RecipeDataError) as exc:
        r.example_spec({"width": 5})
    assert exc.value.code == ErrorCode.SCHEMA_INVALID
    assert exc.value.pointer == "/width"
    with pytest.raises(RecipeDataError) as extra:
        r.example_spec({"no_such_setting": 1})
    assert extra.value.pointer == "/no_such_setting"


def test_a_recipe_without_example_previews_nothing() -> None:
    @recipe("acme.no_example", title="None")
    def fn(inp: Any, s: RecipeSettings) -> Figure:
        raise AssertionError("never rendered")

    assert fn.example_spec({"title": "x"}) is None


# --------------------------------------------------------------- installed mapi.plots packs


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name


class _EntryPoint:
    """Duck-typed ``importlib.metadata.EntryPoint`` (name, dist, load)."""

    def __init__(self, name: str, dist: str | None, obj: Any) -> None:
        self.name = name
        self.dist = _Dist(dist) if dist else None
        self._obj = obj

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def _pack_recipe(id: str) -> Any:
    @recipe(id, title="Pack chart")
    def fn(inp: Any, s: RecipeSettings) -> Figure:
        raise AssertionError("never rendered")

    return fn


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> list[_EntryPoint]:
    real = importlib.metadata.entry_points
    eps: list[_EntryPoint] = []

    def fake(**kw: Any) -> Any:
        if kw.get("group") == "mapi.plots":
            return [*real(**kw), *eps]
        return real(**kw)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)
    return eps


def test_installed_packs_are_merged_only_when_asked(installed: list[_EntryPoint]) -> None:
    lab = _pack_recipe("acme.lab_chart")
    installed.append(_EntryPoint("acme", "acme-lab", lambda: [lab]))
    bundled = {r.ref for r in all_recipes()}
    assert "acme.lab_chart@1.0.0" not in bundled
    merged = all_recipes(installed=True)
    refs = [r.ref for r in merged]
    assert refs[: len(bundled)] == [r.ref for r in all_recipes()]  # bundled first, same order
    assert "acme.lab_chart@1.0.0" in refs
    assert len(refs) == len(set(refs))  # the bundled mapi-plots entry points are not repeated
    assert get_recipe("acme.lab_chart", installed=True) is lab
    with pytest.raises(KeyError):
        get_recipe("acme.lab_chart")


def test_a_single_recipe_export_is_accepted(installed: list[_EntryPoint]) -> None:
    one = _pack_recipe("acme.single")
    installed.append(_EntryPoint("single", "acme-lab", one))
    assert get_recipe("acme.single", installed=True) is one


def test_reserved_namespaces_and_broken_packs_are_skipped_with_a_warning(
    installed: list[_EntryPoint],
) -> None:
    installed.append(_EntryPoint("core", "acme-evil", lambda: [_pack_recipe("core.evil")]))
    installed.append(_EntryPoint("lib", "acme-evil", lambda: [_pack_recipe("lib.evil")]))
    installed.append(_EntryPoint("broken", "acme-broken", ImportError("no module acme_x")))
    installed.append(_EntryPoint("ok", "acme-lab", lambda: [_pack_recipe("acme.fine")]))
    with pytest.warns(RuntimeWarning) as caught:
        refs = {r.ref for r in all_recipes(installed=True)}
    messages = " | ".join(str(w.message) for w in caught)
    assert "core.evil" in messages and "lib.evil" in messages
    assert "mapi.plots:broken: ImportError" in messages
    assert "acme.fine@1.0.0" in refs
    assert not {"core.evil@1.0.0", "lib.evil@1.0.0"} & refs


def test_a_bundled_recipe_wins_over_an_installed_copy(installed: list[_EntryPoint]) -> None:
    fan = RECIPES["core.ppc_fan"]
    installed.append(_EntryPoint("again", "mapi-plots", lambda: [fan]))
    refs = [r.ref for r in all_recipes(installed=True)]
    assert refs.count(fan.ref) == 1


# ----------------------------------------------------------------- chart-template starters


def _columns(spec: PlotSpec) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, ds in spec.datasets.items():
        data = getattr(ds, "data", None)
        assert isinstance(data, str), f"{name} is not inline"
        table = pa.ipc.open_stream(base64.b64decode(data)).read_all()
        out[name] = list(table.schema.names)
    return out


def _vl_type(arrow_type: Any) -> str:
    if pa.types.is_timestamp(arrow_type) or pa.types.is_date(arrow_type):
        return "temporal"
    if pa.types.is_dictionary(arrow_type) or pa.types.is_string(arrow_type):
        return "nominal"
    if pa.types.is_large_string(arrow_type):
        return "nominal"
    return "quantitative"


@pytest.mark.parametrize("recipe_id", sorted(STARTERS))
def test_starter_datasets_match_the_catalog_example(recipe_id: str) -> None:
    example = get_recipe(recipe_id).example_spec()
    assert example is not None
    entry = STARTERS[recipe_id]
    assert entry["datasets"] == _columns(example)
    assert set(entry["fields"]) == set(entry["datasets"])
    for name, ds in example.datasets.items():
        table = pa.ipc.open_stream(base64.b64decode(ds.data)).read_all()  # type: ignore[union-attr]
        types = {f.name: _vl_type(f.type) for f in table.schema}
        assert entry["fields"][name] == types


def _fields(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("field"), str):
            out.add(node["field"])
        for v in node.values():
            out |= _fields(v)
    elif isinstance(node, list):
        for v in node:
            out |= _fields(v)
    return out


def _data_names(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        data = node.get("data")
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            out.add(data["name"])
        for v in node.values():
            out |= _data_names(v)
    elif isinstance(node, list):
        for v in node:
            out |= _data_names(v)
    return out


@pytest.mark.parametrize(
    ("recipe_id", "name"),
    [(rid, s["name"]) for rid in sorted(STARTERS) for s in STARTERS[rid]["starters"]],
)
def test_every_starter_reads_only_the_source_datasets_and_sanitizes(
    recipe_id: str, name: str
) -> None:
    entry = STARTERS[recipe_id]
    starter = next(s for s in entry["starters"] if s["name"] == name)
    assert starter["title"] and starter["summary"]
    spec = starter_spec(recipe_id, name)
    used = _data_names(spec)
    assert used and used <= set(entry["datasets"])
    known = {c for cols in entry["datasets"].values() for c in cols}
    assert _fields(spec) <= known
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", json.dumps(spec)), "starters set no colours"
    example = get_recipe(recipe_id).example_spec()
    assert example is not None
    doc = {**example.model_dump(mode="json"), "spec": spec}
    issues = check_plot(doc)
    assert [i for i in issues if i.severity == Severity.ERROR] == []
    assert {i.pointer for i in issues} <= {"/spec/$schema"}  # only the sanitizer's $schema


def test_starters_are_plain_json_and_copies() -> None:
    assert json.loads(json.dumps(STARTERS)) == STARTERS
    spec = starter_spec("core.ppc_fan", "one_panel")
    spec["width"] = 1
    assert starter_spec("core.ppc_fan", "one_panel")["width"] != 1
    with pytest.raises(KeyError):
        starter_spec("core.ppc_fan", "nope")
