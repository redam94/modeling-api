"""E33: hierarchical geo marketing-mix model (``mmm.hierarchical_geo``).

Daily local-currency spend for markets x channels (one market in GBP), an FX side table
(``score_policy: supplied``) and a holidays table (``pinned``); the pipeline converts to
EUR, resamples to W-SUN weeks (the split stage), mean-scales, builds the lag tensor and
adds Fourier/holiday controls. Score requests: an 8-week forecast, a new market, a weekly
EUR plan entering at the ``weekly`` stage and a counterfactual intervention.
"""

from __future__ import annotations

from pathlib import Path

from mapi_spec import ModelSpec

from mapi_conformance.fixtures._files import NamedRequest, read_requests, read_spec
from mapi_conformance.fixtures.e33.data import generate, payload, raw_schema, truth

__all__ = [
    "FIXTURE_ID",
    "HERE",
    "generate",
    "payload",
    "raw_schema",
    "score_requests",
    "spec",
    "truth",
]

FIXTURE_ID = "e33"
HERE = Path(__file__).parent


def spec() -> ModelSpec:
    return read_spec(HERE)


def score_requests() -> list[NamedRequest]:
    return read_requests(HERE)
