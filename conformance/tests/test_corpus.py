"""The conformance corpus at size ``tiny``: every registered fixture end to end
(spec → static validation → fit → score requests → evaluate → contract kit → plots)."""

from __future__ import annotations

import pytest
from mapi_conformance.fixtures import FIXTURES
from mapi_conformance.runner import CorpusReport, run_fixture
from mapi_sdk.registry import Registry, default_registry

pytestmark = [pytest.mark.conformance, pytest.mark.slow]


@pytest.fixture(scope="module")
def registry() -> Registry:
    return default_registry(reload=True)


@pytest.mark.parametrize("fixture_id", FIXTURES)
def test_fixture_passes_at_tiny_size(fixture_id: str, registry: Registry) -> None:
    report = run_fixture(fixture_id, size="tiny", registry=registry)
    corpus = CorpusReport([report])
    assert report.ok, corpus.table() + "\n" + corpus.failure_details()
    ran = {s.name for s in report.steps if not s.skipped}
    assert {"load", "validate", "fit", "score", "evaluate", "contract", "expectations"} <= ran
    contract = report.step("contract")
    assert contract is not None and contract.total and contract.passed == contract.total
