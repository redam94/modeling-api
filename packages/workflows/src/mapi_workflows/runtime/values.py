"""What node implementations receive and return.

Inline JSON values (test results, split plans, metrics, tables, reports, evidence) are
plain dicts. Datasets and fitted models are objects:

* :class:`DatasetValue` - an ``xr.Dataset`` with its raw :class:`DataSchema`, where it came
  from (``ref``), and the **replayable pipeline** of steps-core step calls that turns the
  workflow input it derives from into it. A model fitted on a derived dataset prepends that
  pipeline to its spec and fits on the original data, so the transform is stored in the
  bundle and replayed at score time (see :mod:`mapi_workflows.runtime.models`).
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
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.schema import DataSchema
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
class NodeContext:
    """Everything a node implementation may use."""

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
