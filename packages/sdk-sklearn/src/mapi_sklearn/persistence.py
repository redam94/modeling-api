"""Safe estimator persistence: skops for scikit-learn, native text/JSON for the boosting
libraries. Never pickle.

* scikit-learn estimators are written with :func:`skops.io.dumps`. Loading lists the
  file's untrusted types first and refuses anything outside the estimator's allow-list
  (:attr:`~mapi_sklearn.estimators.EstimatorEntry.trusted`); only then are those types passed
  to :func:`skops.io.loads` as trusted. The loaded object must be exactly the expected class.
* xgboost models are written as the booster's JSON document (``save_raw("json")``) and
  read back with ``load_model``; the bytes must parse as a JSON object first.
* LightGBM models are the booster's text model (``booster_.model_to_string()``); the text
  must start with ``tree\n``. They load as a :class:`lightgbm.Booster` wrapped in
  :class:`_BoosterClassifier` (``predict_proba`` and ``classes_ = 0..K-1``).
* CatBoost models are CatBoost's JSON model (``save_model(format="json")`` through a
  temporary file); the bytes must be a JSON object with ``oblivious_trees`` whose structure
  is checked first (:func:`_check_catboost_doc`: CatBoost's JSON reader crashes the process
  on some malformed documents). They load through a temporary file into a
  ``CatBoostClassifier``.

Artifacts are named ``<role>/<index>.skops``, ``.xgb.json``, ``.lgb.txt`` or ``.cb.json``
so the bundle layout (``/native``) shows the format at a glance.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import numpy as np
from mapi_sdk.bundle import assert_no_pickle
from mapi_sdk.errors import BundleError

from mapi_sklearn.estimators import EstimatorEntry

__all__ = ["MAX_ARTIFACT_BYTES", "artifact_name", "dump_estimator", "load_estimator"]

MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
_SUFFIX = {
    "skops": ".skops",
    "xgboost": ".xgb.json",
    "lightgbm": ".lgb.txt",
    "catboost": ".cb.json",
}


class _BoosterClassifier:
    """A loaded LightGBM booster with the classifier surface the models use.

    ``predict_proba(X)`` returns ``(n, K)`` probabilities (a binary booster predicts the
    positive class only; the negative column is added) and ``classes_`` is ``0..K-1``: the
    platform always fits on integer codes.
    """

    def __init__(self, booster: Any) -> None:
        self.booster = booster
        k = int(booster.num_model_per_iteration())
        self.n_classes = max(2, k)
        self.classes_ = np.arange(self.n_classes)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        p = np.asarray(self.booster.predict(X), dtype=float)
        if p.ndim == 1:
            return np.stack([1.0 - p, p], axis=1)
        return p

    def predict(self, X: np.ndarray) -> np.ndarray:
        return np.argmax(self.predict_proba(X), axis=1)

    @property
    def feature_importances_(self) -> np.ndarray:
        return np.asarray(self.booster.feature_importance(importance_type="split"), dtype=float)


def artifact_name(entry: EstimatorEntry, role: str, index: int) -> str:
    """The ``/native`` key for model ``index`` of ``role`` (``point``, ``quantile`` ...)."""
    return f"{role}/{index:04d}{_SUFFIX[entry.persistence]}"


def dump_estimator(entry: EstimatorEntry, est: Any) -> bytes:
    """Serialise a fitted estimator in its safe native format."""
    if entry.persistence == "xgboost":
        raw = bytes(est.get_booster().save_raw(raw_format="json"))
    elif entry.persistence == "lightgbm":
        raw = est.booster_.model_to_string().encode("utf-8")
    elif entry.persistence == "catboost":
        with tempfile.TemporaryDirectory(prefix="mapi-cb-") as tmp:
            path = os.path.join(tmp, "model.json")
            est.save_model(path, format="json")
            with open(path, "rb") as fh:
                raw = fh.read()
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
    if entry.persistence == "lightgbm":
        return _load_lightgbm(entry, raw, name)
    if entry.persistence == "catboost":
        return _load_catboost(entry, raw, name)
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


def _load_lightgbm(entry: EstimatorEntry, raw: bytes, name: str) -> Any:
    if not raw.startswith(b"tree\n"):
        raise BundleError(f"native artifact {name!r} is not a LightGBM text model")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleError(f"native artifact {name!r} is not LightGBM text: {exc}") from None
    entry.load_class()  # SETTINGS_INVALID when lightgbm is not installed
    import lightgbm

    try:
        booster = lightgbm.Booster(model_str=text)
    except Exception as exc:  # LightGBMError
        raise BundleError(f"native artifact {name!r} could not be loaded: {exc}") from None
    return _BoosterClassifier(booster)


def _load_catboost(entry: EstimatorEntry, raw: bytes, name: str) -> Any:
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"native artifact {name!r} is not CatBoost JSON: {exc}") from None
    if not isinstance(doc, dict) or "oblivious_trees" not in doc:
        raise BundleError(f"native artifact {name!r} is not a CatBoost model document")
    problem = _check_catboost_doc(doc)
    if problem is not None:
        raise BundleError(f"native artifact {name!r} is not a valid CatBoost model: {problem}")
    est = entry.load_class()()
    with tempfile.TemporaryDirectory(prefix="mapi-cb-") as tmp:
        path = os.path.join(tmp, "model.json")
        with open(path, "wb") as fh:
            fh.write(raw)
        try:
            est.load_model(path, format="json")
        except Exception as exc:  # CatBoostError
            raise BundleError(f"native artifact {name!r} could not be loaded: {exc}") from None
    return est


_CATBOOST_KEYS = frozenset({"features_info", "model_info", "oblivious_trees", "scale_and_bias"})
_MAX_CATBOOST_DEPTH = 16


def _number(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and np.isfinite(v)


def _index(v: Any, size: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < size


def _check_catboost_doc(doc: dict[str, Any]) -> str | None:
    """The first structural problem of a CatBoost JSON model, or ``None``.

    The platform fits on numeric columns only, so the model may hold float features and
    oblivious trees of float splits only; every index must point into the feature and
    border tables and every leaf table must match its tree depth.
    """
    extra = sorted(set(doc) - _CATBOOST_KEYS)
    if extra or set(doc) != _CATBOOST_KEYS:
        return f"unexpected or missing keys ({', '.join(extra) or 'missing'})"
    info, trees, scale = doc["features_info"], doc["oblivious_trees"], doc["scale_and_bias"]
    if not isinstance(doc["model_info"], dict) or not isinstance(info, dict):
        return "model_info and features_info must be objects"
    if set(info) != {"float_features"} or not isinstance(info["float_features"], list):
        return "only float features are supported"
    features = info["float_features"]
    n_borders = 0
    for f in features:
        if not isinstance(f, dict) or not isinstance(f.get("borders", []), list):
            return "a float feature is malformed"
        if not all(_number(b) for b in f.get("borders", [])):
            return "a feature border is not a finite number"
        if not _index(f.get("flat_feature_index"), len(features)):
            return "a feature index is out of range"
        n_borders += len(f.get("borders", []))
    if not isinstance(trees, list) or not trees:
        return "oblivious_trees must be a non-empty list"
    dim: int | None = None
    for tree in trees:
        if not isinstance(tree, dict) or not isinstance(tree.get("splits"), list):
            return "a tree is malformed"
        splits, leaves = tree["splits"], tree.get("leaf_values")
        if len(splits) > _MAX_CATBOOST_DEPTH:
            return "a tree is too deep"
        for sp in splits:
            if (
                not isinstance(sp, dict)
                or sp.get("split_type") != "FloatFeature"
                or not _index(sp.get("float_feature_index"), len(features))
                or not _index(sp.get("split_index"), n_borders)
                or not _number(sp.get("border"))
            ):
                return "a split is malformed"
        n_leaves = 2 ** len(splits)
        if not isinstance(leaves, list) or not leaves or len(leaves) % n_leaves:
            return "a leaf table does not match its tree depth"
        if not all(_number(v) for v in leaves):
            return "a leaf value is not a finite number"
        weights = tree.get("leaf_weights", [])
        if not isinstance(weights, list) or (weights and len(weights) != n_leaves):
            return "a leaf weight table does not match its tree depth"
        if dim is None:
            dim = len(leaves) // n_leaves
        elif len(leaves) // n_leaves != dim:
            return "the trees disagree on the number of outputs"
    if (
        not isinstance(scale, list)
        or len(scale) != 2
        or not _number(scale[0])
        or not isinstance(scale[1], list)
        or len(scale[1]) != dim
        or not all(_number(v) for v in scale[1])
    ):
        return "scale_and_bias does not match the number of outputs"
    return None


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
