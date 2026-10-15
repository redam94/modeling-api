"""Regression tests for the M0 review findings on the wire readers (hostile payloads)."""

from __future__ import annotations

import pytest
from mapi_sdk import wire
from mapi_sdk.errors import PayloadLimitError

# ------------------------------------------------- quality-security-3 (string amplification)


def test_xrjson_strings_are_budgeted_by_decoded_bytes() -> None:
    """One long string among many short ones would decode to a huge fixed-width array."""
    leaves = ["a"] * 20_000 + ["b" * 4096]
    payload = {"dims": {"i": len(leaves)}, "data_vars": {"s": {"dims": ["i"], "data": leaves}}}
    with pytest.raises(PayloadLimitError, match="decode"):
        wire.from_xrjson(payload)
    # ordinary label payloads are untouched
    ok = {"data_vars": {"s": {"dims": ["i"], "data": ["north", "south"] * 1000}}}
    assert wire.from_xrjson(ok)["s"].dtype.kind == "U"
