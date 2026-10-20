"""What node implementations receive and return.

Inline JSON values (test results, split plans, metrics, tables, reports, evidence) are
plain dicts. Datasets and fitted models are objects:

* :class:`DatasetValue` - an ``xr.Dataset`` with its raw :class:`DataSchema`, where it came
  from (``ref``), and the **replayable pipeline** of steps-core step calls that turns the
  workflow input it derives from into it. A model fitted on a derived dataset prepends that
  pipeline to its spec and fits on the original data, so the transform is stored in the
  bundle and replayed at score time (see :mod:`mapi_workflows.runtime.models`). A tree
  dataset (M4) is a :class:`DatasetValue` whose ``data`` is its root node and whose
  ``tree`` is the whole ``xr.DataTree`` (:meth:`DatasetValue.from_tree`); a model node with
  ``nodes`` reads the merge of those nodes instead (:meth:`DatasetValue.select`).
* :class:`ModelValue` - a fitted :class:`~mapi_sdk.bundle.Bundle` with the data it was
  fitted on and its reference (``model://…`` once registered).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import xarray as xr
from mapi_sdk.bundle import Bundle
from mapi_sdk.registry import Registry
from mapi_sdk.steps import CancelToken
from mapi_sdk.tracking import Tracker, TrackingMixin
from mapi_sdk.tree import (
    infer_tree_schema,
    select_nodes,
    selection_error,
    selection_schema,
    stored_tree_schema,
)
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
from mapi_spec.tree import NodeSelect, NodeSelectionError, TreeJoin, TreeSchema
from mapi_spec.workflow import NodeOutputRef
from mapi_spec.workflow_nodes import PortType
from pydantic import JsonValue

__all__ = [
    "DatasetValue",
    "ModelValue",
    "NodeContext",
    "NodeFn",
    "NodeOutcome",
    "Value",
    "value_type",
]


@dataclass(slots=True)
class DatasetValue:
    """A dataset flowing between nodes."""

    data: xr.Dataset
    schema: DataSchema | None = None
    ref: str | None = None
    pipeline: list[dict[str, JsonValue]] = field(default_factory=list)
    """steps-core step calls that turn ``root`` into this dataset (replayable)."""
    root: DatasetValue | None = None
    """The workflow input this dataset derives from (``None``: it is one)."""
    replayable: bool = True
    """False once a transform without a steps-core equivalent was applied."""
    notes: list[str] = field(default_factory=list)
    tree: xr.DataTree | None = None
    """M4: the tree dataset ``data`` was selected from (a model node can select other nodes
    of it, and a tree-input model gets it)."""

    @classmethod
    def from_tree(
        cls, tree: xr.DataTree, *, ref: str | None = None, schema: TreeSchema | None = None
    ) -> DatasetValue:
        """A tree dataset as a node input: ``data`` is its root node (only its coords when
        the root holds no variables, as in a pure group); ``tree`` keeps the whole tree."""
        schema = schema or stored_tree_schema(tree) or infer_tree_schema(tree)
        own = tree.to_dataset(inherit=False)
        if not own.data_vars:
            return cls(data=own, ref=ref, tree=tree)
        try:
            flat = select_nodes(tree, None, schema=schema)
            flat_schema = selection_schema(schema, None, data=flat)
        except NodeSelectionError as exc:
            raise selection_error(exc) from None
        return cls(data=flat, schema=flat_schema, ref=ref, tree=tree)

    def select(self, nodes: Mapping[str, NodeSelect], join: TreeJoin = "exact") -> DatasetValue:
        """The flat dataset ``nodes`` merges from this value's tree (``data.nodes``); the
        tree is kept. Raises ``SCHEMA_INVALID`` for a plain dataset or a bad selection."""
        if self.tree is None:
            raise MapiError(
                "'nodes' selects nodes of a tree dataset, but the input is a plain dataset. "
                "Remove 'nodes' or connect a tree dataset.",
                code=ErrorCode.SCHEMA_INVALID,
                pointer="/data/nodes",
            )
        schema = stored_tree_schema(self.tree) or infer_tree_schema(self.tree)
        try:
            flat = select_nodes(self.tree, nodes, join, schema=schema)
            flat_schema = selection_schema(schema, nodes, data=flat, join=join)
        except NodeSelectionError as exc:
            raise selection_error(exc) from None
        return DatasetValue(data=flat, schema=flat_schema, ref=self.ref, tree=self.tree)

    def derive(
        self,
        data: xr.Dataset,
        *,
        schema: DataSchema | None = None,
        steps: Sequence[Mapping[str, JsonValue]] = (),
        replayable: bool = True,
        note: str | None = None,
    ) -> DatasetValue:
        """A new dataset derived from this one by ``steps``."""
        return DatasetValue(
            data=data,
            schema=schema,
            ref=None,
            pipeline=[*self.pipeline, *(dict(s) for s in steps)],
            root=self.root or self,
            replayable=self.replayable and replayable,
            notes=[*self.notes, *([note] if note else [])],
            tree=self.tree,
        )


@dataclass(slots=True)
class ModelValue:
    """A fitted model version."""

    bundle: Bundle
    data: xr.Dataset | None = None
    ref: str | None = None
    model: str | None = None
    """The catalog model ref it was fitted with (``ts.sarimax@1.0.0``)."""
    settings: dict[str, JsonValue] = field(default_factory=dict)
    tree: xr.DataTree | None = None
    """M4: the tree dataset ``data`` was selected from (a tree-input model scores with it)."""


def scoring_tree(mv: ModelValue, dv: DatasetValue | None) -> xr.DataTree | None:
    """The tree a tree-input model scores ``dv`` with: the tree of the dataset it scores,
    and the tree it was fitted on only when that dataset has none (or is the fit data)."""
    if dv is not None and dv.tree is not None:
        return dv.tree
    return mv.tree


def score_payload(dv: DatasetValue) -> xr.Dataset | xr.DataTree:
    """What a score of ``dv`` sends: a workflow input tree whole (the model's ``data.nodes``
    select from it, as at fit time), any other dataset flat."""
    if dv.tree is not None and dv.root is None:
        return dv.tree
    return dv.data


def score_rows(dv: DatasetValue, bundle: Bundle) -> xr.Dataset:
    """``dv`` flat, as the model reads it: the ``data.nodes`` of its spec selected from a
    workflow input tree (whose ``data`` is only the root node)."""
    block = bundle.spec.data
    if dv.tree is not None and dv.root is None and block.nodes:
        return dv.select(block.nodes, block.join).data
    return dv.data


Value = DatasetValue | ModelValue | dict[str, Any]


def value_type(v: Value) -> PortType | None:
    """The port type of a runtime value (``None`` for inline JSON: the port says)."""
    if isinstance(v, DatasetValue):
        return PortType.DATASET
    if isinstance(v, ModelValue):
        return PortType.MODEL_VERSION
    return None


@dataclass(slots=True)
class NodeOutcome:
    """What a node implementation returns: its JSON result (what conditions read) and its
    outputs by port. ``warnings_plain`` are shown with the node."""

    result: dict[str, JsonValue]
    outputs: dict[str, Value] = field(default_factory=dict)
    warnings_plain: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NodeContext(TrackingMixin):
    """Everything a node implementation may use.

    ``tracker`` (v0.11) receives what the node logs with the
    :class:`~mapi_sdk.tracking.TrackingMixin` methods (a ``search.grid`` node's trial
    params and scores, for example); ``None``: validate only.
    """

    node_id: str
    use: str
    params: dict[str, JsonValue]
    inputs: dict[str, list[Value]]
    registry: Registry | None = None
    seed: int = 0
    cancel: CancelToken | None = None
    log: Callable[[str], None] | None = None
    progress: Callable[[float, str], None] | None = None
    input_refs: dict[str, list[NodeOutputRef]] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)
    """Driver-provided hooks (e.g. ``governance``: the policy resolver and check runners)."""
    tracker: Tracker | None = None
    _logged_params: dict[str, Any] = field(init=False, default_factory=dict, repr=False)

    def one(self, port: str) -> Value | None:
        vals = self.inputs.get(port) or []
        return vals[0] if vals else None

    def dataset(self, port: str = "data", *, required: bool = True) -> DatasetValue | None:
        v = self.one(port)
        if v is None:
            if required:
                raise MapiError(
                    f"the node needs a dataset on {port!r}", code=ErrorCode.SCHEMA_INVALID
                )
            return None
        if not isinstance(v, DatasetValue):
            raise MapiError(f"input {port!r} is not a dataset", code=ErrorCode.SCHEMA_INVALID)
        return v

    def model(self, port: str = "model", *, required: bool = True) -> ModelValue | None:
        v = self.one(port)
        if v is None:
            if required:
                raise MapiError(
                    f"the node needs a model on {port!r}", code=ErrorCode.SCHEMA_INVALID
                )
            return None
        if not isinstance(v, ModelValue):
            raise MapiError(f"input {port!r} is not a model", code=ErrorCode.SCHEMA_INVALID)
        return v

    def json(self, port: str) -> list[dict[str, Any]]:
        return [v for v in self.inputs.get(port, []) if isinstance(v, dict)]

    def check(self) -> None:
        if self.cancel is not None:
            self.cancel.check()

    def info(self, message: str) -> None:
        if self.log is not None:
            self.log(message)


NodeFn = Callable[[NodeContext], NodeOutcome]
