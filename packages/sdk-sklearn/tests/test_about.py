"""Every scikit-learn plugin carries a complete plain-language About card (M3.5)."""

from __future__ import annotations

import pytest
from mapi_sdk.testing import check_about
from mapi_sklearn.plugins import PLUGINS
from mapi_spec.about import about_issues


@pytest.mark.parametrize("plugin", PLUGINS, ids=lambda p: p.id)
def test_every_plugin_has_a_complete_about(plugin: type) -> None:
    about = plugin.manifest().about  # type: ignore[attr-defined]
    assert about is not None
    assert about_issues(about) == []
    assert check_about(plugin).ok
