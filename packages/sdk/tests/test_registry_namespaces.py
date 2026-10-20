"""The namespaces of embeddable recipes (``causal``, ``choice``) are reserved for the platform."""

from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest
from mapi_sdk.registry import BUILTIN_DISTRIBUTIONS, BUILTIN_NAMESPACES, Registry
from mapi_sdk.testing import toy
from mapi_spec.sharing import PLATFORM_RECIPE_NAMESPACES


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name


class _EP:
    def __init__(self, name: str, obj: Any, dist: str) -> None:
        self.name, self._obj, self.dist = name, obj, _Dist(dist)

    def load(self) -> Any:
        return self._obj


class _CausalStep(toy.ScaleStep):
    id = "causal.shadow"


def test_only_the_causal_plugin_registers_causal_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: v0.13 made ``causal.*`` recipes embeddable (platform recipes); another
    distribution must not be able to register an id in that namespace."""
    assert "causal" in BUILTIN_NAMESPACES and "mapi-plugin-causal" in BUILTIN_DISTRIBUTIONS
    eps = {"mapi.steps": [_EP("evil", _CausalStep, "evil-dist")]}
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps.get(group, []))
    errors = Registry().load_entry_points()
    assert len(errors) == 1 and "reserved" in errors[0] and "causal" in errors[0]
    eps = {"mapi.steps": [_EP("own", _CausalStep, "mapi-plugin-causal")]}
    reg = Registry()
    assert reg.load_entry_points() == []
    assert reg.step("causal.shadow@^1") is _CausalStep


def test_every_embeddable_namespace_is_reserved() -> None:
    assert set(PLATFORM_RECIPE_NAMESPACES) <= BUILTIN_NAMESPACES
