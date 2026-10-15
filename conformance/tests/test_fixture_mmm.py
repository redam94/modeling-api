"""Conformance corpus: the E33 and pymc-marketing MMM fixtures, tiny size.

Each fixture runs spec → static validation → fit (through ``default_registry()``, i.e.
the installed entry points for steps, models and plots) → every named score request in
``score_requests.yaml`` → the fixture's expectations. Fits use tiny draws (PR runs); the
nightly corpus runs ``size="full"``.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
import xarray as xr
from mapi_conformance.fixtures import FIXTURES
from mapi_conformance.fixtures import e33 as e33_fixture
from mapi_conformance.fixtures import pymc_marketing as pmm_fixture
from mapi_conformance.fixtures.e33 import expectations as e33_expect
from mapi_conformance.fixtures.pymc_marketing import expectations as pmm_expect
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec, score_bundle
from mapi_sdk.registry import Registry, default_registry
from mapi_spec import validate_spec

pytestmark = [pytest.mark.conformance, pytest.mark.integration]

slow = pytest.mark.slow  # each fixture fits twice (evaluate_then_refit), ~15 s

SEED = 0


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


def _fit(fixture: ModuleType, registry: Registry) -> tuple[Bundle, dict[str, xr.Dataset]]:
    data, tables = fixture.generate(SEED, "tiny")
    spec = fixture.spec()
    schema = fixture.raw_schema(data)
    res = validate_spec(spec, registry.catalog(), schema)
    assert res.ok, [(i.pointer, i.message) for i in res.errors]
    return fit_spec(spec, data, tables, registry, raw_schema=schema), tables


def _score_all(
    fixture: ModuleType,
    expect: Any,
    bundle: Bundle,
    tables: dict[str, xr.Dataset],
    registry: Registry,
) -> dict[str, xr.DataTree]:
    results: dict[str, xr.DataTree] = {}
    for named in fixture.score_requests():
        payload = fixture.payload(named.payload, seed=SEED, size="tiny")
        tree = score_bundle(
            bundle,
            named.request,
            payload,
            side_tables=tables if named.side_tables else None,
            registry=registry,
        )
        results[named.name] = tree
        expect.check_score(named.name, tree, bundle=bundle, registry=registry, results=results)
    return results


def test_fixtures_are_registered() -> None:
    assert {"e33", "pymc_marketing"} <= set(FIXTURES)
    names = [r.name for r in e33_fixture.score_requests()]
    assert names == ["forecast_8w", "new_market", "weekly_eur_plan", "counterfactual_tv"]
    assert {r.request.inputs_stage for r in e33_fixture.score_requests()} == {"raw", "weekly"}


@slow
def test_e33_fixture(registry: Registry) -> None:
    bundle, tables = _fit(e33_fixture, registry)
    e33_expect.check_bundle(bundle, registry, e33_fixture.truth(SEED, "tiny"))
    results = _score_all(e33_fixture, e33_expect, bundle, tables, registry)
    assert set(results) == {"forecast_8w", "new_market", "weekly_eur_plan", "counterfactual_tv"}


@slow
def test_pymc_marketing_fixture(registry: Registry) -> None:
    bundle, tables = _fit(pmm_fixture, registry)
    pmm_expect.check_bundle(bundle, registry)
    results = _score_all(pmm_fixture, pmm_expect, bundle, tables, registry)
    assert set(results) == {"forecast_4w", "recent_window", "full_series", "counterfactual_search"}
