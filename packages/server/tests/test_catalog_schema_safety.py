"""Catalog hardening (v0.18): the JSON Schemas a plugin wrote are made safe before the
control plane validates anything with them.

A hostile catalog (a catastrophic ``pattern``, deep nesting, a remote ``$ref``, a 1 MiB
description) is stripped when it is parsed, stored or loaded; each change is a
``schema_unsafe`` diagnostic; a crafted settings value then validates in milliseconds; a
built-in catalog passes through unchanged (same object, same digest)."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.catalog import harden_catalog, parse_catalog
from mapi_server_testkit.builders import CATALOG
from mapi_spec.catalog import Catalog
from mapi_spec.run import catalog_digest
from mapi_spec.schema_safety import SETTINGS_SCHEMA_MAX_BYTES
from mapi_spec.validate import validate_settings

EVIL_PATTERN = "^(a+)+$"
CRAFTED = "a" * 40 + "!"  # catastrophic backtracking against EVIL_PATTERN


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CATALOG.read_text())
    return data


def _deep(levels: int) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "number"}
    for i in range(levels):
        node = {"type": "object", "properties": {f"p{i}": node}}
    return node


def hostile_catalog() -> dict[str, Any]:
    """The fixture catalog with an unsafe schema of every kind a plugin could ship."""
    data = _fixture()
    manifest = data["models"][0]["manifest"]
    props = manifest["settings_schema"]["properties"]
    props["name"] = {"type": "string", "pattern": EVIL_PATTERN, "default": "a"}
    props["remote"] = {"$ref": "https://evil.example/schema.json"}
    manifest["fit_schema"] = {
        "type": "object",
        "patternProperties": {"^(\\w+\\s?)*$": {"type": "number"}},
    }
    step = data["steps"][0]
    step["settings_schema"]["properties"]["deep"] = _deep(15)
    data["plots"][0]["settings_schema"] = {
        "type": "object",
        "description": "x" * (1024 * 1024),
        "properties": {"title": {"type": "string"}},
    }
    return data


def _walk(obj: Any) -> list[Any]:
    out = [obj]
    if isinstance(obj, dict):
        for v in obj.values():
            out += _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _walk(v)
    return out


# ============================================================================ pure


def test_a_built_in_catalog_is_returned_unchanged() -> None:
    catalog = Catalog.model_validate(_fixture())
    assert harden_catalog(catalog) is catalog
    assert parse_catalog(CATALOG.read_bytes()) == catalog
    assert catalog_digest(parse_catalog(CATALOG.read_bytes())) == catalog_digest(catalog)


def test_the_hostile_catalog_is_stripped_before_any_schema_is_used() -> None:
    raw = json.dumps(hostile_catalog())
    t0 = time.perf_counter()
    catalog = parse_catalog(raw)
    assert time.perf_counter() - t0 < 5.0  # no pathological work at load
    manifest = catalog.models[0].manifest
    # the plugin's text survives only in the diagnostics' untrusted_text
    text = json.dumps(catalog.model_dump(mode="json", exclude={"diagnostics"}))
    assert EVIL_PATTERN not in text
    assert "evil.example" not in text
    assert "x" * 3000 not in text
    name = manifest.settings_schema["properties"]["name"]  # type: ignore[index]
    assert name == {"type": "string", "default": "a"}
    assert "remote" in manifest.settings_schema["properties"]  # type: ignore[operator]
    assert manifest.fit_schema.get("patternProperties") == {}
    deep = catalog.steps[0].settings_schema["properties"]["deep"]  # type: ignore[index]
    assert max(len(json.dumps(x)) for x in _walk(deep)) < len(json.dumps(_deep(15)))
    assert catalog.plots[0].settings_schema is None  # too large to keep: left out
    codes = {(d.code, d.group, d.id) for d in catalog.diagnostics}
    assert codes == {
        ("schema_unsafe", "mapi.models", "ts.hsgp_trend@" + manifest.version),
        ("schema_unsafe", "mapi.steps", catalog.steps[0].ref),
        ("schema_unsafe", "mapi.plots", catalog.plots[0].ref),
    }
    assert all(len(d.message) <= 500 for d in catalog.diagnostics)
    assert all(
        d.untrusted_text is None or len(d.untrusted_text) <= 500 for d in catalog.diagnostics
    )
    # the two model schemas are one row each, both mentioning what was removed
    model_rows = [d for d in catalog.diagnostics if d.group == "mapi.models"]
    assert len(model_rows) == 2
    assert any("problems" in d.message for d in model_rows)


def test_hardening_is_idempotent() -> None:
    once = parse_catalog(json.dumps(hostile_catalog()))
    twice = harden_catalog(once)
    assert twice is once
    assert catalog_digest(twice) == catalog_digest(once)


def test_a_crafted_settings_value_validates_fast_after_hardening() -> None:
    catalog = parse_catalog(json.dumps(hostile_catalog()))
    schema = catalog.models[0].manifest.settings_schema
    settings = {"name": CRAFTED}
    t0 = time.perf_counter()
    issues = validate_settings(schema, settings, "/model/settings")
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.05, f"settings validation took {elapsed * 1000:.1f} ms"
    assert issues == []  # the unsafe pattern is gone, so the value is simply a string


def test_a_plot_settings_schema_under_the_cap_is_kept() -> None:
    data = _fixture()
    schema = {"type": "object", "properties": {"title": {"type": "string", "maxLength": 300}}}
    data["plots"][0]["settings_schema"] = schema
    catalog = parse_catalog(json.dumps(data))
    assert catalog.plots[0].settings_schema == schema
    assert catalog.diagnostics == []
    big = {
        "type": "object",
        "properties": {f"k{i}": {"type": "string", "title": "t" * 100} for i in range(200)},
    }
    assert len(json.dumps(big)) > SETTINGS_SCHEMA_MAX_BYTES
    data["plots"][0]["settings_schema"] = big
    catalog = parse_catalog(json.dumps(data))
    assert catalog.plots[0].settings_schema is None
    [diag] = catalog.diagnostics
    assert diag.code == "schema_unsafe" and "16 KiB" in diag.message


def test_the_runner_diagnostics_are_kept_first() -> None:
    data = hostile_catalog()
    data["diagnostics"] = [
        {"code": "load_error", "group": "mapi.models", "distribution": "acme-models",
         "message": "The entry point could not be loaded.", "untrusted_text": "ImportError"},
    ]  # fmt: skip
    catalog = parse_catalog(json.dumps(data))
    assert catalog.diagnostics[0].code == "load_error"
    assert len(catalog.diagnostics) == 5  # + one per stripped schema


# ===================================================================== integration


@pytest.fixture
def settings(settings: Any, tmp_path: Path) -> Any:
    path = tmp_path / "hostile.json"
    path.write_text(json.dumps(hostile_catalog()))
    return settings.model_copy(update={"catalog_path": path})


@pytest.mark.integration
async def test_the_stored_catalog_is_the_hardened_one(ctx: ServerContext) -> None:
    async with ctx.db.session() as s:
        snap = await ctx.catalogs.require(s)
        row = await s.get(m.Catalog, ctx.catalogs.image_digest)
        assert row is not None
        stored = copy.deepcopy(row.catalog)
    stored.pop("diagnostics")
    assert EVIL_PATTERN not in json.dumps(stored)
    assert snap.digest == catalog_digest(snap.catalog) == row.catalog_digest
    assert {d.code for d in snap.diagnostics} == {"schema_unsafe"}
    # a row stored before v0.18 (unsafe as written) is hardened when it is loaded
    async with ctx.db.session() as s:
        row = await s.get(m.Catalog, ctx.catalogs.image_digest)
        assert row is not None
        row.catalog = hostile_catalog()
    ctx.catalogs.invalidate()
    async with ctx.db.session() as s:
        snap = await ctx.catalogs.require(s)
    hardened = snap.catalog.model_dump(mode="json", exclude={"diagnostics"})
    assert EVIL_PATTERN not in json.dumps(hardened)
    assert snap.digest == row.catalog_digest  # the recorded digest is kept
