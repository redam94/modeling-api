"""Conformance corpus (pymc-challenges-style fixtures) and its runner (plan §13).

Run it with ``python -m mapi_conformance run [--size tiny|full] [--fixture ID]`` or from
Python::

    from mapi_conformance import run_corpus

    report = run_corpus(size="tiny")
    print(report.table())

Each fixture lives in :mod:`mapi_conformance.fixtures` (data generator, ``spec.yaml``,
``score_requests.yaml`` and optional ``expectations.py``).
"""

from importlib import metadata as _metadata

from mapi_conformance.fixtures import FIXTURES
from mapi_conformance.runner import (
    CorpusReport,
    Fixture,
    FixtureReport,
    ScoreCase,
    StepResult,
    load_fixture,
    run_corpus,
    run_fixture,
)

try:
    __version__ = _metadata.version("mapi-conformance")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.4.0"

__all__ = [
    "FIXTURES",
    "CorpusReport",
    "Fixture",
    "FixtureReport",
    "ScoreCase",
    "StepResult",
    "__version__",
    "load_fixture",
    "run_corpus",
    "run_fixture",
]
