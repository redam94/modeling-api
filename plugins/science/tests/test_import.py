from __future__ import annotations

import mapi_plugin_science


def test_plugins_tuple() -> None:
    ids = {p.id for p in mapi_plugin_science.PLUGINS}
    assert {
        "sci.lgcp_grid",
        "sci.fisher_kpp",
        "sci.traction_force",
        "sci.strong_lens",
        "sci.heat_plate",
    } <= ids
