"""Spec templates for ``mapi new``, derived from the conformance corpus fixtures.

Each template is the fixture's ``spec.yaml`` with comments and ``{{name}}``/``{{project}}``
placeholders. ``tests/test_templates.py`` keeps them in sync with the fixtures (the
pipeline, model and evaluation blocks must match) and checks that each one validates
against the local catalog with the fixture's data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Final

from mapi_spec._types import SLUG_RE
from mapi_spec.loader import load_model_spec

__all__ = ["TEMPLATES", "Template", "render"]


@dataclass(frozen=True, slots=True)
class Template:
    name: str
    fixture: str
    """Conformance fixture id (``mapi_conformance.fixtures.<id>``) it is derived from."""
    summary: str
    side_tables: tuple[str, ...] = ()

    def text(self) -> str:
        return (resources.files(__package__) / f"{self.name}.yaml").read_text(encoding="utf-8")


TEMPLATES: Final[dict[str, Template]] = {
    t.name: t
    for t in (
        Template(
            "geo-mmm",
            "e33",
            "Hierarchical geo marketing-mix model (weekly EUR, adstock, saturation)",
            ("fx", "holidays"),
        ),
        Template("forecast", "c08", "Time-series forecast with an HSGP trend and seasonality"),
        Template("tabular", "xgb_store_demand", "XGBoost demand model with quantile uncertainty"),
        Template("glm", "e02", "Hierarchical GLM with varying group intercepts (long layout)"),
    )
}

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render(template: Template, *, name: str, project: str, file: str = "spec.yaml") -> str:
    """Fill the placeholders and check that the result is a valid ModelSpec document."""
    for label, value in (("name", name), ("project", project)):
        if not re.match(SLUG_RE, value):
            raise ValueError(
                f"{label} {value!r} must be a slug (lowercase letters, digits, '-' and '_')"
            )
    values = {"name": name, "project": project, "file": file}

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"template {template.name} uses unknown placeholder {key!r}")
        return values[key]

    text = _PLACEHOLDER.sub(sub, template.text())
    load_model_spec(text)  # raises ValidationFailed if the template is broken
    return text
