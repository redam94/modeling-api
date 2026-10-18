from __future__ import annotations

import mapi_plugin_science

EXPECTED_IDS = {
    "sci.lgcp_grid",
    "sci.fisher_kpp",
    "sci.traction_force",
    "sci.strong_lens",
    "sci.heat_plate",
    "sci.turing_pattern",
    "sci.storm_localisation",
    "sci.ibp_features",
    "sci.colour_naming",
    "sci.cmb_field",
}


def test_plugins_tuple() -> None:
    ids = {p.id for p in mapi_plugin_science.PLUGINS}
    # Exact match and no duplicates: dropping or repeating a plugin fails here.
    assert ids == EXPECTED_IDS
    assert len(mapi_plugin_science.PLUGINS) == len(EXPECTED_IDS)
