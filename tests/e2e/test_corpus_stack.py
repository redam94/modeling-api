"""The conformance corpus through the *running* local stack (plan §13: spec → ingest → fit →
score), not the in-process executor: uploads go through the sandboxed ingest, fits and scores
through the dispatcher, the runner and FINALIZING, and the bundle comes back through signed
URLs. ``make dev`` first; the API is found like ``scripts/dev.py`` does (``MAPI_API_URL``)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from mapi_client import Client
from mapi_client.errors import TransportError
from mapi_conformance.runner import CorpusReport
from mapi_conformance.stack import STACK_STEPS, StackAuth, run_fixture_on_stack
from mapi_spec.localdev import LocalDev

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

STACK_FIXTURES = ["e33", "xgb_store_demand", "sklearn_store_demand"]
TOKEN = uuid.uuid4().hex[:8]


@pytest.fixture(scope="module")
def api(stack_credentials: StackAuth) -> Iterator[Client]:
    url = LocalDev.from_environ(os.environ).api_url
    with Client(url, token=stack_credentials.token, project=stack_credentials.project) as client:
        try:
            ready = client.ready()
        except TransportError as exc:
            pytest.fail(f"the API is not reachable at {url} ({exc}); start it with `make dev`")
        assert ready.status == "ok", ready.model_dump()
        yield client


@pytest.mark.parametrize("fixture_id", STACK_FIXTURES)
def test_fixture_passes_through_the_stack(api: Client, fixture_id: str) -> None:
    report = run_fixture_on_stack(fixture_id, api, prefix="e2e-corpus", token=TOKEN)
    corpus = CorpusReport([report])
    assert report.ok, corpus.table() + "\n" + corpus.failure_details()
    ran = [s.name for s in report.steps if not s.skipped]
    assert {"ingest", "validate", "fit", "score", "expectations"} <= set(ran)
    assert set(ran) <= set(STACK_STEPS)
    score = report.step("score")
    assert score is not None and score.total and score.passed == score.total
