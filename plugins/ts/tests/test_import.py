import mapi_plugin_ts
from mapi_sdk.registry import default_registry


def test_import() -> None:
    assert mapi_plugin_ts.__version__


def test_entry_point_registers_every_plugin() -> None:
    reg = default_registry(reload=True)
    for plugin in mapi_plugin_ts.PLUGINS:
        assert reg.model(f"{plugin.id}@^1") is plugin
    ids = {m.manifest.id for m in reg.catalog().models}
    assert {"ts.hsgp_trend", "ts.gaussian_hmm"} <= ids
