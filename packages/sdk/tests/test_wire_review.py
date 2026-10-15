"""Regression tests for the M0 review findings on the wire readers (hostile payloads)."""

from __future__ import annotations

import pytest
from mapi_sdk import wire
from mapi_sdk.errors import PayloadLimitError, WireFormatError

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


# --------------------------------------- quality-security-12 (declared dims, special dates)


def test_xrjson_caps_declared_dim_sizes() -> None:
    payload = {"dims": {"a": 0, "b": 10**15}, "data_vars": {"v": {"dims": ["a", "b"], "data": []}}}
    with pytest.raises(PayloadLimitError, match="'b'"):
        wire.from_xrjson(payload)


@pytest.mark.parametrize("value", ["now", "today", "2024-01-07 junk", "1e9"])
def test_xrjson_datetimes_must_be_iso_8601(value: str) -> None:
    payload = {"coords": {"t": {"dims": ["t"], "data": [value], "dtype": "datetime64[ns]"}}}
    with pytest.raises(WireFormatError, match="ISO-8601"):
        wire.from_xrjson(payload)


def test_xrjson_iso_datetimes_still_decode() -> None:
    values = ["2024-01-07", "2024-01-14T00:00:00", "2024-01-21T00:00:00.000000000", None]
    payload = {"coords": {"t": {"dims": ["t"], "data": values, "dtype": "datetime64[ns]"}}}
    t = wire.from_xrjson(payload)["t"].values
    assert str(t[2])[:10] == "2024-01-21" and str(t[3]) == "NaT"
