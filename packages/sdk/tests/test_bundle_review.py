"""Regression tests for the M0 review findings on bundles (pickle checks, evaluation dir)."""

from __future__ import annotations

import io
import pickle
import zipfile
from typing import Any

import numpy as np
import pytest
from mapi_sdk.bundle import assert_no_pickle
from mapi_sdk.errors import BundleError

# ----------------------------------------------------- quality-security-7 (pickle detection)


def _npy(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, arr, allow_pickle=True)
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize(
    "data",
    [
        pickle.dumps({"w": [1, 2, 3]}, protocol=0),
        pickle.dumps({"w": [1, 2, 3]}, protocol=1),
        pickle.dumps({"w": [1, 2, 3]}, protocol=5),
        _npy(np.array([{"a": 1}], dtype=object)),
        _zip({"x.joblib": b"anything"}),
        _zip({"weights/model.bin": pickle.dumps([1.0], protocol=2)}),
        _zip({"arr.npy": _npy(np.array([None], dtype=object))}),
    ],
    ids=["proto0", "proto1", "proto5", "npy-object", "zip-joblib", "zip-member", "npz-object"],
)
def test_pickle_is_detected_whatever_the_wrapping(data: bytes) -> None:
    with pytest.raises(BundleError):
        assert_no_pickle({"model.bin": data})


@pytest.mark.parametrize(
    "data",
    [
        b'{"learner": {"trees": []}}',
        _npy(np.arange(10.0)),
        _zip({"schema.json": b"{}", "arr.npy": _npy(np.ones(3))}),
        bytes(range(256)) * 4,
    ],
    ids=["json", "npy-float", "zip-safe", "binary"],
)
def test_safe_native_formats_pass(data: bytes) -> None:
    assert_no_pickle({"model.bin": data})


# ------------------------------------------------- quality-security-11 (evaluation sub-bundle)


def test_evaluation_sub_bundle_is_verified_and_never_stale(
    tmp_path: Any, registry: Any, e33_spec: Any, panel: Any
) -> None:
    from mapi_sdk.bundle import Bundle
    from mapi_sdk.harness import fit_spec

    data, fx = panel
    bundle = fit_spec(e33_spec(metrics="rmse"), data, {"fx": fx}, registry)
    assert bundle.evaluation is not None
    root = tmp_path / "b"
    bundle.write(root)
    assert Bundle.read(root, registry).evaluation is not None
    # a replaced (internally consistent) evaluation dir no longer matches the parent
    other = fit_spec(e33_spec(metrics="mape"), data, {"fx": fx}, registry)
    assert other.evaluation is not None
    other.evaluation.write(root / "evaluation")
    with pytest.raises(BundleError, match="evaluation"):
        Bundle.read(root, registry)
    # rewriting without an evaluation removes the stale directory; nothing is loaded
    bundle.evaluation = None
    bundle.write(root)
    assert not (root / "evaluation").exists()
    assert Bundle.read(root, registry).evaluation is None
    # an evaluation dir the parent manifest does not declare is ignored
    other.evaluation.write(root / "evaluation")
    assert Bundle.read(root, registry).evaluation is None
