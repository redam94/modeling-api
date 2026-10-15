"""pymc-marketing MMM adapter (``mmm.pymc_marketing``), the plan's M0 ``owns`` fixture.

The same simulated EUR media data as E33, but the model owns scaling, carry-over and the
inverse to original units: the pipeline only converts currencies, resamples to weeks and
adds holidays. Score requests: a 4-week forecast, a recent window and the full series
(carry-over equivalence) and a counterfactual intervention.
"""

from __future__ import annotations

from pathlib import Path

from mapi_spec import ModelSpec

from mapi_conformance.fixtures._files import NamedRequest, read_requests, read_spec
from mapi_conformance.fixtures.pymc_marketing.data import generate, payload, raw_schema

__all__ = ["FIXTURE_ID", "HERE", "generate", "payload", "raw_schema", "score_requests", "spec"]

FIXTURE_ID = "pymc_marketing"
HERE = Path(__file__).parent


def spec() -> ModelSpec:
    return read_spec(HERE)


def score_requests() -> list[NamedRequest]:
    return read_requests(HERE)
