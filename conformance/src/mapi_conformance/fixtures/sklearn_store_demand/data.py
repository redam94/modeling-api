"""The store-demand panel of :mod:`mapi_conformance.fixtures.xgb_store_demand` (same data and
payload builders, so the two estimators are compared on identical inputs)."""

from __future__ import annotations

from mapi_conformance.fixtures.xgb_store_demand.data import (
    SIZES,
    generate,
    payload_future,
    payload_future_without_promo,
    payload_new_sku,
    schema,
)

__all__ = [
    "SIZES",
    "generate",
    "payload_future",
    "payload_future_without_promo",
    "payload_new_sku",
    "schema",
]
