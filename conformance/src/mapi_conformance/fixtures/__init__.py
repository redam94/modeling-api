"""Conformance fixtures: one subpackage per fixture id.

Each fixture directory holds ``spec.yaml`` (the ModelSpec), ``score_requests.yaml`` (named
score requests plus the payload each one is scored on), a data generator module with
``generate(seed, size='tiny'|'full') -> (xr.Dataset, side_tables)`` and
``expectations.py`` with the fixture's extra checks.
"""

FIXTURES: list[str] = [
    "e33",
    "pymc_marketing",
]
