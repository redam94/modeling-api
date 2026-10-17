"""The Guided templates against the real catalog, and the catalog fixture the server's
Guided API tests use (kept in step with the installed plugins).

Regenerate the fixture after a plugin or step change with
``MAPI_UPDATE_SNAPSHOTS=1 uv run pytest conformance/tests/test_guided_catalog.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mapi_sdk.registry import Registry, default_registry
from mapi_spec.abouts import PLANNED_MODEL_IDS
from mapi_spec.catalog import Catalog
from mapi_spec.guided import GUIDED_TEMPLATES

pytestmark = pytest.mark.conformance

FIXTURE = (
    Path(__file__).resolve().parents[2] / "packages" / "server" / "tests" / "fixtures"
    / "catalog_guided.json"
)  # fmt: skip


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


def _guided_catalog(registry: Registry) -> Catalog:
    full = registry.catalog()
    plugins = {t.plugin.split("@")[0] for t in GUIDED_TEMPLATES.values()}
    models = [e for e in full.models if e.manifest.id in plugins]
    return full.model_copy(update={"models": models})


def test_every_template_plugin_is_a_built_in(registry: Registry) -> None:
    catalog = registry.catalog()
    for template in GUIDED_TEMPLATES.values():
        use = template.plugin
        assert template.plugin_available, template.id
        entry = catalog.resolve_model(use)
        if entry is None and use.split("@")[0] in PLANNED_MODEL_IDS:
            continue  # a v0.5 plugin (mmm-framework, statsmodels) not installed here
        assert entry is not None, f"{template.id}: no built-in plugin satisfies {use}"


def test_the_server_catalog_fixture_is_current(registry: Registry) -> None:
    current = json.loads(_guided_catalog(registry).model_dump_json())
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        FIXTURE.write_text(json.dumps(current, sort_keys=True, separators=(",", ":")) + "\n")
    pinned = json.loads(FIXTURE.read_text())
    assert pinned == current, "regenerate with MAPI_UPDATE_SNAPSHOTS=1"
    assert FIXTURE.stat().st_size < 1024 * 1024
