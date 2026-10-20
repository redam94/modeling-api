"""Difference outputs (``difference_of`` attr): inverted as a level minus its reference."""

from __future__ import annotations

import numpy as np
import xarray as xr
from mapi_sdk.harness import _difference_back, _difference_levels, _difference_refs
from mapi_sdk.models import DIFFERENCE_ATTR
from mapi_spec.capabilities import Representation


def _pred() -> tuple[xr.Dataset, np.ndarray]:
    rng = np.random.default_rng(0)
    y = rng.uniform(50, 150, 6)
    y_hat = np.log(y)[None, :] + rng.normal(0, 0.1, (4, 6))  # model stage: log
    effect = np.log(y)[None, :] - y_hat
    pred = xr.Dataset(
        {
            "y_hat": (("draw", "t"), y_hat),
            "effect": (("draw", "t"), effect, {DIFFERENCE_ATTR: "y_hat", "kind": "expected"}),
        }
    )
    return pred, y


def test_a_difference_is_inverted_as_level_minus_reference() -> None:
    pred, y = _pred()
    refs = _difference_refs(pred, Representation.DRAWS)
    assert set(refs) == {"effect"}
    sources = {"y_hat": "y", "effect": "y"}
    work, helpers = _difference_levels(pred[["effect"]], refs, sources)  # y_hat not requested
    assert helpers == {"effect__ref": "y"}
    inv = work.map(np.exp, keep_attrs=True)  # the target's inverse
    space = {"effect": "raw", "effect__ref": "raw"}
    out, space, _ = _difference_back(inv, helpers, space, {"effect__ref": "exact"})
    assert list(out.data_vars) == ["effect"] and space == {"effect": "raw"}
    want = y[None, :] - np.exp(pred["y_hat"].values)
    np.testing.assert_allclose(out["effect"].values, want)
    assert out["effect"].attrs[DIFFERENCE_ATTR] == "y_hat"
    # the plain inverse gives the ratio y / y_hat, not a difference
    assert not np.allclose(np.exp(pred["effect"].values), want)


def test_moments_and_unknown_references_keep_the_plain_inverse() -> None:
    pred, _ = _pred()
    assert _difference_refs(pred, Representation.MOMENTS) == {}
    pred["effect"].attrs[DIFFERENCE_ATTR] = "missing"
    assert _difference_refs(pred, Representation.DRAWS) == {}
    work, helpers = _difference_levels(pred, {}, {"effect": "y"})
    assert helpers == {} and work is pred
