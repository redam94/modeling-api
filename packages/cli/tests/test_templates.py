"""Spec templates stay in sync with the conformance fixtures and validate against the
local catalog with each fixture's data schema."""

from __future__ import annotations

import re
from typing import Any

import pytest
from mapi_cli.templates import TEMPLATES, render
from mapi_conformance.runner import load_fixture
from mapi_sdk.registry import default_registry
from mapi_spec.errors import has_errors
from mapi_spec.loader import load_model_spec, parse_text
from mapi_spec.validate import validate_spec


def _refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_refs(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"^dataset://[a-z0-9_-]+/", "dataset://P/", value)
    return value


def _normalise(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop metadata and the project part of dataset refs (templates use {{project}})."""
    return {k: _refs(v) for k, v in doc.items() if k != "metadata"}


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_template_matches_its_fixture(name: str) -> None:
    tpl = TEMPLATES[name]
    fixture = load_fixture(tpl.fixture)
    rendered = parse_text(render(tpl, name="x", project="p"))
    assert _normalise(rendered) == _normalise(parse_text(fixture.spec_text))
    spec = load_model_spec(fixture.spec_text)
    assert set(tpl.side_tables) == set(spec.data.side_tables)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_template_validates_with_fixture_data(name: str) -> None:
    tpl = TEMPLATES[name]
    fixture = load_fixture(tpl.fixture)
    data, _ = fixture.generate(seed=0, size="tiny")
    spec = load_model_spec(render(tpl, name="check", project="default"))
    result = validate_spec(spec, default_registry().catalog(), fixture.raw_schema(data))
    assert not has_errors(result.issues), [i.message for i in result.issues]


def test_render_validates_names() -> None:
    tpl = TEMPLATES["glm"]
    text = render(tpl, name="radon", project="env")
    spec = load_model_spec(text)
    assert spec.metadata.name == "radon" and spec.metadata.project == "env"
    assert str(spec.data.dataset).startswith("dataset://env/")
    with pytest.raises(ValueError, match="slug"):
        render(tpl, name="Bad Name", project="env")
