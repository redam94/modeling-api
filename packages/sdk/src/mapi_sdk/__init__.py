"""xarray modelling runtime: steps, pipelines, coord plans, bundles, contract kit.

The SDK runs inside runner images and notebooks (never in the control plane). The main
entry points:

* :func:`fit_spec` / :func:`score_bundle` - the fit and score harness;
* :class:`Step`, :class:`StepState`, :class:`StepContext` - the preprocessing contract;
* :class:`CustomModel`, :class:`FitOutput`, :class:`Fitted` - the model contract;
* :class:`Pipeline`, :class:`CoordPlan`, :func:`canonicalise_coords` - the engine;
* :class:`Bundle` - fitted artifacts; :mod:`mapi_sdk.wire` - payload formats;
* :class:`Registry` / :func:`default_registry` - entry-point discovery;
* :class:`InprocExecutor` - the in-process executor;
* :mod:`mapi_sdk.tracking` - run tracking (v0.11): :class:`Tracker`, :class:`MetricRecorder`,
  :class:`TeeTracker`, :func:`with_labels`; media (v0.14): :class:`MediaTracker`,
  :class:`MediaItem` and :func:`encode_png` (:mod:`mapi_sdk.media`);
* :mod:`mapi_sdk.tree` - tree datasets (M4): :func:`select_nodes`, :func:`infer_tree_schema`,
  :func:`validate_tree`;
* :mod:`mapi_sdk.collection` - image collections and loaders (v0.15): :class:`Collection`,
  :class:`ImageLoader`, :class:`CollectionLoader`, :class:`ArrayImageLoader`,
  :class:`Augment`; :mod:`mapi_sdk.safetensors` and :mod:`mapi_sdk.checkpoints` - network
  weights and file checkpoints;
* :mod:`mapi_sdk.testing` - the contract kit for plugin authors.

Attributes are imported lazily (PEP 562), so ``import mapi_sdk`` is cheap and the pytest
plugin does not load the runtime until a fixture needs it.
"""

from __future__ import annotations

import importlib
from importlib import metadata as _metadata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mapi_sdk.actuals import ForecastActuals
    from mapi_sdk.binding import BindingMap
    from mapi_sdk.bundle import Bundle, BundleManifest, MetricsReport
    from mapi_sdk.checkpoints import FileCheckpoint, FileCheckpointSink
    from mapi_sdk.collection import (
        ArrayImageLoader,
        Augment,
        Batch,
        BlobSource,
        Collection,
        CollectionLoader,
        ImageLoader,
        LocalPackSource,
    )
    from mapi_sdk.context import ContextMark, drop_context, extract_context, prepend_context
    from mapi_sdk.coords import CanonicalPlan, CoordPlan, canonicalise_coords, horizon_coords
    from mapi_sdk.errors import (
        BundleError,
        PayloadLimitError,
        RunCancelled,
        SchemaValidationError,
        StepError,
        WireFormatError,
    )
    from mapi_sdk.executors import InprocExecutor
    from mapi_sdk.harness import FitHooks, fit_spec, score_bundle
    from mapi_sdk.media import MediaItem, encode_png
    from mapi_sdk.models import (
        PREDICTIONS,
        CustomModel,
        FitContext,
        FitOutput,
        Fitted,
        ModelPlugin,
        ModelSettings,
        ScoreContext,
        build_manifest,
        new_level_mask,
        requested_positions,
    )
    from mapi_sdk.pipeline import Pipeline, PipelineStep
    from mapi_sdk.registry import Registry, default_registry
    from mapi_sdk.safetensors import (
        read_safetensors,
        tensors_from_dataset,
        weights_dataset,
        write_safetensors,
    )
    from mapi_sdk.schema import (
        SchemaIssue,
        check_dataset,
        data_summary,
        infer_schema,
        summarize_coords,
        validate_dataset,
    )
    from mapi_sdk.split import SplitResult, split_dataset
    from mapi_sdk.steps import CancelToken, Step, StepContext, StepSettings, StepState, states_equal
    from mapi_sdk.summarise import aggregate, hdi, thin_index
    from mapi_sdk.tracking import (
        METRICS_HISTORY,
        MediaTracker,
        MetricRecorder,
        TeeTracker,
        Tracker,
        TrackingMixin,
        with_labels,
    )
    from mapi_sdk.tree import infer_tree_schema, select_nodes, validate_tree
    from mapi_sdk.wire import (
        read_datatree_zip,
        read_zarr_zip,
        stack_rows,
        unstack_rows,
        write_datatree_zip,
        write_zarr_zip,
    )

_LAZY: dict[str, str] = {
    "ArrayImageLoader": "mapi_sdk.collection",
    "Augment": "mapi_sdk.collection",
    "Batch": "mapi_sdk.collection",
    "BindingMap": "mapi_sdk.binding",
    "BlobSource": "mapi_sdk.collection",
    "Bundle": "mapi_sdk.bundle",
    "BundleError": "mapi_sdk.errors",
    "BundleManifest": "mapi_sdk.bundle",
    "CancelToken": "mapi_sdk.steps",
    "CanonicalPlan": "mapi_sdk.coords",
    "Collection": "mapi_sdk.collection",
    "CollectionLoader": "mapi_sdk.collection",
    "ContextMark": "mapi_sdk.context",
    "CoordPlan": "mapi_sdk.coords",
    "CustomModel": "mapi_sdk.models",
    "FileCheckpoint": "mapi_sdk.checkpoints",
    "FileCheckpointSink": "mapi_sdk.checkpoints",
    "FitContext": "mapi_sdk.models",
    "FitHooks": "mapi_sdk.harness",
    "FitOutput": "mapi_sdk.models",
    "Fitted": "mapi_sdk.models",
    "ForecastActuals": "mapi_sdk.actuals",
    "ImageLoader": "mapi_sdk.collection",
    "InprocExecutor": "mapi_sdk.executors",
    "LocalPackSource": "mapi_sdk.collection",
    "METRICS_HISTORY": "mapi_sdk.tracking",
    "MediaItem": "mapi_sdk.media",
    "MediaTracker": "mapi_sdk.tracking",
    "MetricRecorder": "mapi_sdk.tracking",
    "MetricsReport": "mapi_sdk.bundle",
    "ModelPlugin": "mapi_sdk.models",
    "ModelSettings": "mapi_sdk.models",
    "PREDICTIONS": "mapi_sdk.models",
    "PayloadLimitError": "mapi_sdk.errors",
    "Pipeline": "mapi_sdk.pipeline",
    "PipelineStep": "mapi_sdk.pipeline",
    "Registry": "mapi_sdk.registry",
    "RunCancelled": "mapi_sdk.errors",
    "SchemaIssue": "mapi_sdk.schema",
    "SchemaValidationError": "mapi_sdk.errors",
    "ScoreContext": "mapi_sdk.models",
    "SplitResult": "mapi_sdk.split",
    "Step": "mapi_sdk.steps",
    "StepContext": "mapi_sdk.steps",
    "StepError": "mapi_sdk.errors",
    "StepSettings": "mapi_sdk.steps",
    "StepState": "mapi_sdk.steps",
    "TeeTracker": "mapi_sdk.tracking",
    "Tracker": "mapi_sdk.tracking",
    "TrackingMixin": "mapi_sdk.tracking",
    "WireFormatError": "mapi_sdk.errors",
    "aggregate": "mapi_sdk.summarise",
    "build_manifest": "mapi_sdk.models",
    "canonicalise_coords": "mapi_sdk.coords",
    "check_dataset": "mapi_sdk.schema",
    "data_summary": "mapi_sdk.schema",
    "default_registry": "mapi_sdk.registry",
    "drop_context": "mapi_sdk.context",
    "encode_png": "mapi_sdk.media",
    "extract_context": "mapi_sdk.context",
    "fit_spec": "mapi_sdk.harness",
    "hdi": "mapi_sdk.summarise",
    "horizon_coords": "mapi_sdk.coords",
    "infer_schema": "mapi_sdk.schema",
    "infer_tree_schema": "mapi_sdk.tree",
    "new_level_mask": "mapi_sdk.models",
    "prepend_context": "mapi_sdk.context",
    "read_datatree_zip": "mapi_sdk.wire",
    "read_safetensors": "mapi_sdk.safetensors",
    "read_zarr_zip": "mapi_sdk.wire",
    "requested_positions": "mapi_sdk.models",
    "score_bundle": "mapi_sdk.harness",
    "select_nodes": "mapi_sdk.tree",
    "split_dataset": "mapi_sdk.split",
    "stack_rows": "mapi_sdk.wire",
    "states_equal": "mapi_sdk.steps",
    "summarize_coords": "mapi_sdk.schema",
    "tensors_from_dataset": "mapi_sdk.safetensors",
    "thin_index": "mapi_sdk.summarise",
    "unstack_rows": "mapi_sdk.wire",
    "validate_dataset": "mapi_sdk.schema",
    "validate_tree": "mapi_sdk.tree",
    "weights_dataset": "mapi_sdk.safetensors",
    "with_labels": "mapi_sdk.tracking",
    "write_datatree_zip": "mapi_sdk.wire",
    "write_safetensors": "mapi_sdk.safetensors",
    "write_zarr_zip": "mapi_sdk.wire",
}

try:
    __version__ = _metadata.version("mapi-sdk")
except _metadata.PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.15.1"


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'mapi_sdk' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


__all__ = [
    "METRICS_HISTORY",
    "PREDICTIONS",
    "ArrayImageLoader",
    "Augment",
    "Batch",
    "BindingMap",
    "BlobSource",
    "Bundle",
    "BundleError",
    "BundleManifest",
    "CancelToken",
    "CanonicalPlan",
    "Collection",
    "CollectionLoader",
    "ContextMark",
    "CoordPlan",
    "CustomModel",
    "FileCheckpoint",
    "FileCheckpointSink",
    "FitContext",
    "FitHooks",
    "FitOutput",
    "Fitted",
    "ForecastActuals",
    "ImageLoader",
    "InprocExecutor",
    "LocalPackSource",
    "MediaItem",
    "MediaTracker",
    "MetricRecorder",
    "MetricsReport",
    "ModelPlugin",
    "ModelSettings",
    "PayloadLimitError",
    "Pipeline",
    "PipelineStep",
    "Registry",
    "RunCancelled",
    "SchemaIssue",
    "SchemaValidationError",
    "ScoreContext",
    "SplitResult",
    "Step",
    "StepContext",
    "StepError",
    "StepSettings",
    "StepState",
    "TeeTracker",
    "Tracker",
    "TrackingMixin",
    "WireFormatError",
    "__version__",
    "aggregate",
    "build_manifest",
    "canonicalise_coords",
    "check_dataset",
    "data_summary",
    "default_registry",
    "drop_context",
    "encode_png",
    "extract_context",
    "fit_spec",
    "hdi",
    "horizon_coords",
    "infer_schema",
    "infer_tree_schema",
    "new_level_mask",
    "prepend_context",
    "read_datatree_zip",
    "read_safetensors",
    "read_zarr_zip",
    "requested_positions",
    "score_bundle",
    "select_nodes",
    "split_dataset",
    "stack_rows",
    "states_equal",
    "summarize_coords",
    "tensors_from_dataset",
    "thin_index",
    "unstack_rows",
    "validate_dataset",
    "validate_tree",
    "weights_dataset",
    "with_labels",
    "write_datatree_zip",
    "write_safetensors",
    "write_zarr_zip",
]
