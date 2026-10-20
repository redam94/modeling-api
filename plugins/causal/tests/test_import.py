from importlib.metadata import entry_points

import mapi_plugin_causal
import pytest
from mapi_sdk.registry import default_registry


def _installed() -> bool:
    return any(e.name == "causal" for e in entry_points(group="mapi.models"))


def test_import() -> None:
    assert mapi_plugin_causal.__version__
    ids = [p.id for p in mapi_plugin_causal.PLUGINS]
    assert ids[0] == "causal.impact"
    assert set(ids) <= {"causal.impact", "causal.synthetic_control", "causal.did"}


@pytest.mark.skipif(not _installed(), reason="mapi-plugin-causal is not installed yet")
def test_entry_point_registers_every_plugin() -> None:
    reg = default_registry(reload=True)
    for plugin in mapi_plugin_causal.PLUGINS:
        assert reg.model(f"{plugin.id}@^1") is plugin
    ids = {m.manifest.id for m in reg.catalog().models}
    assert {p.id for p in mapi_plugin_causal.PLUGINS} <= ids


def test_every_plugin_has_a_complete_about_card() -> None:
    from mapi_sdk.testing import check_about

    report = check_about(*mapi_plugin_causal.PLUGINS)
    assert report.ok and len(report.results) == len(mapi_plugin_causal.PLUGINS), report
    for plugin in mapi_plugin_causal.PLUGINS:
        assert plugin.manifest().model_family == "causal"
