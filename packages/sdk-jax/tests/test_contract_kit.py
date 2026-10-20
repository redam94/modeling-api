"""The SDK contract kit on ``dl.cnn_classifier`` (dataset path and collection batch path)."""

from __future__ import annotations

import pytest
import xarray as xr
from mapi_jax.models import CNNClassifier
from mapi_jax.testing import image_schema
from mapi_sdk.collection import Collection
from mapi_sdk.registry import Registry
from mapi_sdk.testing import ModelFixture, check_model

from .conftest import cnn_spec


@pytest.mark.slow
def test_contract_kit(shapes: xr.Dataset, collection: Collection, registry: Registry) -> None:
    fixture = ModelFixture(
        spec=cnn_spec(fit="{seed: 3}"),
        data=shapes,
        raw_schema=image_schema(shapes),
        registry=registry,
    )
    report = check_model(CNNClassifier, fixture, collection=collection, require_about=True)
    assert report.ok, report
