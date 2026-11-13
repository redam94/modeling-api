"""v0.17: the dashboard ``meaning`` (and ``classes_dim``) each output declares in the manifest.

Outputs that do not set them leave the manifest canonical JSON unchanged (both fields are
excluded when unset), so only the models listed here change their manifest identity.
"""

from __future__ import annotations

from typing import Any

from mapi_plugin_tabular import PLUGINS
from mapi_spec.loader import canonical_json

#: model id -> output -> (meaning, classes_dim); every other output sets neither.
EXPECTED: dict[str, dict[str, tuple[str, str | None]]] = {
    "tabular.image_logistic": {"y_hat": ("class_probability", "class")},
    "choice.multinomial_logit": {"y_hat": ("class_probability", "product")},
}


def _declared(plugin: Any) -> dict[str, tuple[str | None, str | None]]:
    return {k: (o.meaning, o.classes_dim) for k, o in plugin.manifest().outputs.items()}


def test_meaning_is_set_where_expected() -> None:
    for plugin in PLUGINS:
        want = EXPECTED.get(plugin.id, {})
        for name, got in _declared(plugin).items():
            assert got == want.get(name, (None, None)), (plugin.id, name)
    assert set(EXPECTED) <= {p.id for p in PLUGINS}


def test_canonical_manifest_mentions_meaning_only_when_set() -> None:
    for plugin in PLUGINS:
        text = canonical_json(plugin.manifest())
        assert ('"meaning":' in text) == (plugin.id in EXPECTED), plugin.id
        assert ('"classes_dim":' in text) == any(
            c is not None for _, c in EXPECTED.get(plugin.id, {}).values()
        ), plugin.id


def test_classes_dim_is_a_dim_of_the_output() -> None:
    for plugin in PLUGINS:
        for name, decl in plugin.manifest().outputs.items():
            if decl.classes_dim is not None:
                assert decl.classes_dim in decl.dims, (plugin.id, name)
