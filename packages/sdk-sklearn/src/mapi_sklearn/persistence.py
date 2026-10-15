"""Safe estimator persistence: skops for scikit-learn, native JSON for xgboost. Never pickle.

* scikit-learn estimators are written with :func:`skops.io.dumps`. Loading lists the
  file's untrusted types first and refuses anything outside the estimator's allow-list
  (:attr:`~mapi_sklearn.estimators.EstimatorEntry.trusted`); only then are those types passed
  to :func:`skops.io.loads` as trusted. The loaded object must be exactly the expected class.
* xgboost models are written as the booster's JSON document (``save_raw("json")``) and
  read back with ``load_model``; the bytes must parse as a JSON object first.

Artifacts are named ``<role>/<index>.skops`` or ``<role>/<index>.xgb.json`` so the bundle
layout (``/native``) shows the format at a glance.
"""

from __future__ import annotations

import json
from typing import Any

from mapi_sdk.bundle import assert_no_pickle
from mapi_sdk.errors import BundleError

from mapi_sklearn.estimators import EstimatorEntry

__all__ = ["MAX_ARTIFACT_BYTES", "artifact_name", "dump_estimator", "load_estimator"]

MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_SUFFIX = {"skops": ".skops", "xgboost": ".xgb.json"}


def artifact_name(entry: EstimatorEntry, role: str, index: int) -> str:
    """The ``/native`` key for model ``index`` of ``role`` (``point``, ``quantile`` ...)."""
    return f"{role}/{index:04d}{_SUFFIX[entry.persistence]}"


def dump_estimator(entry: EstimatorEntry, est: Any) -> bytes:
    """Serialise a fitted estimator in its safe native format."""
    if entry.persistence == "xgboost":
        raw = bytes(est.get_booster().save_raw(raw_format="json"))
    else:
        import skops.io as sio

        raw = sio.dumps(est)
    assert_no_pickle({"artifact" + _SUFFIX[entry.persistence]: raw})
    return raw


def load_estimator(entry: EstimatorEntry, raw: bytes, *, name: str = "artifact") -> Any:
    """Load an estimator written by :func:`dump_estimator`.

    Raises :class:`~mapi_sdk.errors.BundleError` (``INTEGRITY_FAILURE``) for oversized,
    malformed or untrusted content.
    """
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise BundleError(f"native artifact {name!r} is larger than {MAX_ARTIFACT_BYTES} bytes")
    assert_no_pickle({name: raw})
    if entry.persistence == "xgboost":
        return _load_xgboost(entry, raw, name)
    return _load_skops(entry, raw, name)


def _load_xgboost(entry: EstimatorEntry, raw: bytes, name: str) -> Any:
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"native artifact {name!r} is not xgboost JSON: {exc}") from None
    if not isinstance(doc, dict) or "learner" not in doc:
        raise BundleError(f"native artifact {name!r} is not an xgboost model document")
    est = entry.load_class()()
    try:
        est.load_model(bytearray(raw))
    except Exception as exc:  # xgboost raises its own XGBoostError
        raise BundleError(f"native artifact {name!r} could not be loaded: {exc}") from None
    return est


def _load_skops(entry: EstimatorEntry, raw: bytes, name: str) -> Any:
    import skops.io as sio

    try:
        untrusted = list(sio.get_untrusted_types(data=raw))
    except Exception as exc:
        raise BundleError(f"native artifact {name!r} is not a skops file: {exc}") from None
    refused = sorted(set(untrusted) - entry.trusted)
    if refused:
        raise BundleError(
            f"native artifact {name!r} contains types outside the allow-list for "
            f"{entry.kind}: {', '.join(refused[:10])}"
        )
    try:
        est = sio.loads(raw, trusted=untrusted)
    except Exception as exc:
        raise BundleError(f"native artifact {name!r} could not be loaded: {exc}") from None
    if type(est) is not entry.load_class():
        raise BundleError(
            f"native artifact {name!r} holds a {type(est).__name__}, expected {entry.name}"
        )
    return est
