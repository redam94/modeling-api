"""Who may register which ids from entry points, what is reported when they may not, and who
provided each catalog entry (v0.13 reserved the embeddable namespaces; v0.18 takes the lists
from :mod:`mapi_spec.namespaces` and adds diagnostics and ``provider``)."""

from __future__ import annotations

import importlib.metadata
import json
from typing import Any

import mapi_spec.namespaces as spec_namespaces
import pytest
import xarray as xr
from mapi_sdk.registry import (
    BUILTIN_DISTRIBUTIONS,
    BUILTIN_NAMESPACES,
    ENTRY_POINT_GROUPS,
    PLATFORM_RESERVED,
    Registry,
)
from mapi_sdk.testing import toy
from mapi_spec.catalog import Catalog, ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry
from mapi_spec.namespaces import is_builtin_distribution
from mapi_spec.sharing import PLATFORM_RECIPE_NAMESPACES


class _Dist:
    def __init__(self, name: str) -> None:
        self.name = name


class _EP:
    def __init__(self, name: str, obj: Any, dist: str | None) -> None:
        self.name, self._obj = name, obj
        self.dist = None if dist is None else _Dist(dist)

    def load(self) -> Any:
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def _entries(cat: Catalog) -> list[StepCatalogEntry | ModelCatalogEntry | PlotCatalogEntry]:
    return [*cat.steps, *cat.models, *cat.plots]


def _install(monkeypatch: pytest.MonkeyPatch, eps: dict[str, list[_EP]]) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group: eps.get(group, []))


def _step(id_: str) -> type[toy.ScaleStep]:
    return type(f"Step_{id_.replace('.', '_')}", (toy.ScaleStep,), {"id": id_})


class _CausalStep(toy.ScaleStep):
    id = "causal.shadow"


class _AcmeScale(toy.ScaleStep):
    id = "acme.scale"


class _AcmeLinear(toy.LinearModel):
    id = "acme.linear"


class _AcmePlot:
    @classmethod
    def catalog_entry(cls) -> PlotCatalogEntry:
        return PlotCatalogEntry(id="acme.bars", title="Bars")

    def render(self, data: xr.DataTree, **options: Any) -> Any:
        return None


# ----------------------------------------------------------------------- the lists
def test_the_lists_are_the_spec_lists() -> None:
    """One source of truth: the SDK names are the :mod:`mapi_spec.namespaces` objects."""
    assert BUILTIN_NAMESPACES is spec_namespaces.BUILTIN_NAMESPACES
    assert BUILTIN_DISTRIBUTIONS is spec_namespaces.BUILTIN_DISTRIBUTIONS
    assert PLATFORM_RESERVED is spec_namespaces.PLATFORM_RESERVED
    # reserving a namespace and adding its distribution go together (plan R1)
    assert {"sci", "dl", "net", "toy", "demo", "examples"} <= BUILTIN_NAMESPACES
    assert {"mapi-plugin-science", "mapi-jax", "mapi-workflows"} <= BUILTIN_DISTRIBUTIONS


def test_every_embeddable_namespace_is_reserved() -> None:
    assert set(PLATFORM_RECIPE_NAMESPACES) <= BUILTIN_NAMESPACES


# ------------------------------------------------------------------- the guard
def test_only_the_causal_plugin_registers_causal_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: v0.13 made ``causal.*`` recipes embeddable (platform recipes); another
    distribution must not be able to register an id in that namespace."""
    _install(monkeypatch, {"mapi.steps": [_EP("evil", _CausalStep, "evil-dist")]})
    reg = Registry()
    errors = reg.load_entry_points()
    assert len(errors) == 1 and "reserved" in errors[0] and "causal" in errors[0]
    assert [d.code for d in reg.diagnostics] == ["namespace_reserved"]
    _install(monkeypatch, {"mapi.steps": [_EP("own", _CausalStep, "mapi-plugin-causal")]})
    reg = Registry()
    assert reg.load_entry_points() == [] and reg.diagnostics == []
    assert reg.step("causal.shadow@^1") is _CausalStep


@pytest.mark.parametrize(
    "id_", ["sci.x", "dl.x", "net.x", "demo.x", "examples.x", "toy.x", "t.x", "lib.x", "t_acme.x"]
)
def test_a_third_party_cannot_claim_a_reserved_namespace(
    monkeypatch: pytest.MonkeyPatch, id_: str
) -> None:
    _install(monkeypatch, {"mapi.steps": [_EP("claim", _step(id_), "acme-models")]})
    reg = Registry()
    errors = reg.load_entry_points()
    assert len(errors) == 1 and "reserved" in errors[0]
    (diag,) = reg.diagnostics
    assert diag.code == "namespace_reserved"
    assert (diag.group, diag.entry_point, diag.distribution, diag.id) == (
        "mapi.steps",
        "claim",
        "acme-models",
        id_,
    )
    assert diag.untrusted_text is not None and id_ in diag.untrusted_text
    assert reg.steps() == []


@pytest.mark.parametrize("id_", ["lib.x", "platform.x", "tenant_acme.x"])
def test_nobody_claims_a_platform_namespace(monkeypatch: pytest.MonkeyPatch, id_: str) -> None:
    """The platform-reserved namespaces stay free for library items and uploads, even for
    the built-in distributions."""
    _install(monkeypatch, {"mapi.steps": [_EP("claim", _step(id_), "mapi-steps")]})
    reg = Registry()
    reg.load_entry_points()
    assert [d.code for d in reg.diagnostics] == ["namespace_reserved"]


def test_an_unknown_distribution_is_not_built_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"mapi.steps": [_EP("anon", _step("sci.anon"), None)]})
    reg = Registry()
    reg.load_entry_points()
    (diag,) = reg.diagnostics
    assert diag.code == "namespace_reserved" and diag.distribution is None


def test_built_in_distribution_names_are_compared_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``importlib.metadata`` may report ``mapi_plugin_science``; it is still built in."""
    _install(monkeypatch, {"mapi.steps": [_EP("own", _step("sci.own"), "mapi_plugin_science")]})
    reg = Registry()
    assert reg.load_entry_points() == []
    assert reg.provider_of("step", "sci.own") is None


def test_in_memory_registration_is_not_guarded() -> None:
    """Tests, notebooks and docs snippets register ``demo.*``/``examples.*`` directly."""
    reg = Registry()
    reg.register(_step("demo.node_count"))
    assert reg.step("demo.node_count@^1").id == "demo.node_count"


# ------------------------------------------------------------------ diagnostics
def test_each_refusal_is_one_diagnostic_with_a_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Shadow(_AcmeScale):
        """Claims acme.scale@1.0.0 from another distribution."""

    _install(
        monkeypatch,
        {
            "mapi.steps": [
                _EP("own", _AcmeScale, "acme-models"),
                _EP("shadow", _Shadow, "evil-dist"),
                _EP("broken", ImportError("no module named acme_extra"), "acme-extra"),
            ],
            "mapi.models": [_EP("junk", 42, "acme-models")],
        },
    )
    reg = Registry()
    errors = reg.load_entry_points()
    assert errors == reg.load_errors and len(errors) == 3
    by_ep = {d.entry_point: d for d in reg.diagnostics}
    assert by_ep["shadow"].code == "id_collision" and by_ep["shadow"].id == "acme.scale"
    assert by_ep["broken"].code == "load_error" and by_ep["broken"].id is None
    assert by_ep["broken"].untrusted_text == "ImportError: no module named acme_extra"
    assert by_ep["junk"].code == "load_error" and by_ep["junk"].group == "mapi.models"
    # the first registration wins
    assert reg.step("acme.scale@^1") is _AcmeScale
    assert reg.provider_of("step", "acme.scale") == "acme-models"
    # the rows survive the catalog JSON shape
    Catalog(diagnostics=reg.diagnostics)


def test_plugin_text_stays_out_of_the_trusted_message(monkeypatch: pytest.MonkeyPatch) -> None:
    long = "<b>" + "x" * 2000
    _install(
        monkeypatch,
        {
            "mapi.steps": [
                _EP("loud", RuntimeError(long), "acme-models"),
                _EP("claim", _step("t_x" + "y" * 150 + ".z"), "acme-models"),
            ]
        },
    )
    reg = Registry()
    reg.load_entry_points()
    loud, claim = reg.diagnostics
    assert "<b>" not in loud.message and loud.untrusted_text is not None
    assert len(loud.untrusted_text) == 500 and loud.untrusted_text.startswith("RuntimeError: <b>")
    assert claim.code == "namespace_reserved" and "yyy" not in claim.message
    assert claim.id is not None and len(claim.id) == 128


def test_one_bad_export_never_hides_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        {"mapi.steps": [_EP("many", lambda: [_step("sci.bad"), _AcmeScale], "acme-models")]},
    )
    reg = Registry()
    assert len(reg.load_entry_points()) == 1
    assert [d.id for d in reg.diagnostics] == ["sci.bad"]
    assert reg.step("acme.scale@^1") is _AcmeScale


def test_strict_raises_on_the_first_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"mapi.steps": [_EP("claim", _step("mmm.claim"), "acme-models")]})
    with pytest.raises(RuntimeError, match="reserved"):
        Registry().load_entry_points(strict=True)


# --------------------------------------------------------------------- provider
def test_provider_is_set_only_for_non_built_in_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        {
            "mapi.steps": [
                _EP("scale", toy.ScaleStep, "mapi-sdk"),
                _EP("acme", _AcmeScale, "acme-models"),
            ],
            "mapi.models": [_EP("acme", _AcmeLinear, "acme-models")],
            "mapi.plots": [_EP("acme", _AcmePlot(), "acme-plots")],
        },
    )
    reg = Registry()
    assert reg.load_entry_points() == []
    reg.register(toy.LagStep)  # in memory: no provider
    assert reg.provider_of("step", "acme.scale") == "acme-models"
    assert reg.provider_of("step", "acme.scale@1.0.0") == "acme-models"
    assert reg.provider_of("model", "acme.linear") == "acme-models"
    assert reg.provider_of("plot", "acme.bars") == "acme-plots"
    assert reg.provider_of("step", "toy.scale") is None
    assert reg.provider_of("step", "toy.lag") is None
    assert reg.provider_of("step", "nope.x") is None
    assert reg.provider_of("model", "acme.scale") is None  # per kind

    cat = reg.catalog()
    providers = {e.id: e.provider for e in _entries(cat)}
    assert providers == {
        "acme.scale": "acme-models",
        "toy.lag": None,
        "toy.scale": None,
        "acme.linear": "acme-models",
        "acme.bars": "acme-plots",
    }
    raw = json.loads(cat.model_dump_json())
    assert "provider" not in raw["steps"][[s["id"] for s in raw["steps"]].index("toy.scale")]
    assert Catalog.model_validate(raw) == cat
    assert reg.copy().catalog() == cat  # copies keep their providers


def test_replacing_an_entry_drops_its_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"mapi.steps": [_EP("acme", _AcmeScale, "acme-models")]})
    reg = Registry()
    reg.load_entry_points()

    class _Local(_AcmeScale):
        pass

    reg.register(_Local, replace=True)
    assert reg.provider_of("step", "acme.scale") is None


# ----------------------------------------------------------------- live registry
def _live() -> Registry:
    reg = Registry()
    reg.load_entry_points()
    return reg


def test_the_installed_registry_loads_cleanly_from_built_in_distributions() -> None:
    """Reserving ``sci``/``dl``/``net``/``toy``/``demo``/``examples`` breaks no built-in."""
    reg = _live()
    assert reg.load_errors == [] and reg.diagnostics == []
    for group in ENTRY_POINT_GROUPS:
        for ep in importlib.metadata.entry_points(group=group):
            dist = getattr(getattr(ep, "dist", None), "name", None)
            assert is_builtin_distribution(dist), f"{group}:{ep.name} from {dist}"
    cat = reg.catalog()
    for entry in _entries(cat):
        assert entry.id.split(".", 1)[0] in BUILTIN_NAMESPACES, entry.id
        assert entry.provider is None, entry.id


# v0.18 deliberately adds these plot fields (WP-X4); everything else is unchanged
_NEW_PLOT_KEYS = ("settings_schema", "tags", "pack_label")


def _without_new_plot_keys(raw: dict[str, Any]) -> dict[str, Any]:
    for plot in raw.get("plots", []):
        for key in _NEW_PLOT_KEYS:
            plot.pop(key, None)
    return raw


def test_the_built_in_catalog_is_byte_identical() -> None:
    """The v0.17 export (entries as the plugins describe them) and today's export agree, so
    the built-in catalog digest does not move (``provider`` and the empty v0.18 lists are
    left out of the JSON)."""
    reg = _live()
    legacy = Catalog(
        steps=[s.catalog_entry() for s in reg.steps()],
        models=[ModelCatalogEntry(manifest=m.manifest()) for m in reg.models()],
        plots=[p.catalog_entry() for p in reg.plots()],
    )
    now = json.loads(reg.catalog().model_dump_json())
    assert set(now) == {"steps", "models", "plots"}
    assert json.dumps(_without_new_plot_keys(now), sort_keys=True) == json.dumps(
        _without_new_plot_keys(json.loads(legacy.model_dump_json())), sort_keys=True
    )
