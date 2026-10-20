"""One node in the runner sandbox: the ``workflow_node`` run kind.

The control plane sends a node that needs data to a ``workflow_node`` run
(:class:`~mapi_spec.workflow.WorkflowNodeJob`). Its inputs arrive as run inputs named
``in.<port>.<k>`` (the ``k``-th value bound to ``port``):

==============  =========================================================================
port type       run inputs
==============  =========================================================================
dataset         ``in.<port>.<k>``: the dataset's ``*.zarr.zip`` (its raw schema in the
                ``mapi_schema`` attribute)
model_version   ``in.<port>.<k>``: the bundle (``bundle.zarr.zip``),
                ``in.<port>.<k>.manifest``: its SDK manifest, ``in.<port>.<k>.data``: the
                dataset the model was fitted on (residual checks, refits)
JSON types      ``job.inline_inputs["in.<port>.<k>"]`` (test results, split plans,
                metrics, tables, evidence)
==============  =========================================================================

Outputs (:func:`output_files`): every dataset output port is written as
``out/<port>/data.zarr.zip`` with ``schema.json``, ``summary.json`` (coordinate summaries,
as an ingest writes them) and ``meta.json`` (the replayable pipeline that derives it from
its root input); JSON outputs are inlined in ``node_result.json`` up to
:data:`MAX_INLINE_OUTPUT` bytes (plots: :data:`MAX_INLINE_PLOT`, while the sidecar stays
under :data:`~mapi_spec.workflow.NODE_RESULT_MAX_BYTES`), else written as
``out/<port>.json``. Model versions cannot
come out of a ``workflow_node`` run: nodes that make one (``model.fit``) run as ``fit`` runs.

Driver context the control plane resolved travels in reserved, underscore-prefixed
parameters: ``_policies`` (the governance policies in force for ``governance.policy``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import xarray as xr
from mapi_sdk.registry import Registry
from mapi_sdk.schema import data_summary
from mapi_sdk.steps import CancelToken
from mapi_sdk.tracking import Tracker
from mapi_sdk.wire import write_zarr_zip
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.governance import GovernancePolicy
from mapi_spec.workflow import (
    NODE_RESULT_MAX_BYTES,
    WORKFLOW_RUN_INPUT_PREFIX,
    NodeOutputRef,
    NodeResultSidecar,
    WorkflowNodeJob,
)
from mapi_spec.workflow_nodes import PortType, get_node
from mapi_workflows.runtime.data import schema_of, with_schema
from mapi_workflows.runtime.governance import GovernanceServices
from mapi_workflows.runtime.nodes import node_registry
from mapi_workflows.runtime.values import (
    DatasetValue,
    ModelValue,
    NodeContext,
    NodeFn,
    NodeOutcome,
    Value,
)
from pydantic import JsonValue, ValidationError

__all__ = [
    "MAX_INLINE_OUTPUT",
    "MAX_INLINE_PLOT",
    "OUTPUT_PREFIX",
    "OutputFile",
    "execute_job",
    "input_name",
    "output_files",
    "parse_input_name",
]

MAX_INLINE_OUTPUT: Final = 64 * 1024
MAX_INLINE_PLOT: Final = 256 * 1024
"""A plot output is inlined up to this size (and while the sidecar fits its cap)."""
_SIDECAR_RESERVE: Final = 16 * 1024
OUTPUT_PREFIX: Final = "out/"
_MODEL_SUFFIXES: Final = (".manifest", ".data")


def input_name(port: str, k: int, suffix: str = "") -> str:
    """``in.<port>.<k>[suffix]``."""
    return f"{WORKFLOW_RUN_INPUT_PREFIX}{port}.{k}{suffix}"


def parse_input_name(name: str) -> tuple[str, int] | None:
    """``(port, k)`` of ``in.<port>.<k>`` (``None`` for side inputs such as ``.manifest``)."""
    if not name.startswith(WORKFLOW_RUN_INPUT_PREFIX) or name.endswith(_MODEL_SUFFIXES):
        return None
    rest = name.removeprefix(WORKFLOW_RUN_INPUT_PREFIX)
    port, _, k = rest.rpartition(".")
    if not port or not k.isdigit():
        return None
    return port, int(k)


@dataclass(frozen=True, slots=True)
class OutputFile:
    """One object a node run writes (``name`` relative to the run's prefix)."""

    name: str
    data: bytes


def _policies(params: Mapping[str, JsonValue]) -> list[GovernancePolicy]:
    raw = params.get("_policies")
    if not isinstance(raw, list):
        return []
    try:
        return [GovernancePolicy.model_validate(p) for p in raw]
    except ValidationError as exc:
        raise MapiError(
            "the resolved policies are not valid",
            code=ErrorCode.POLICY_INVALID,
            untrusted_text=str(exc)[:4000],
        ) from None


def execute_job(
    job: WorkflowNodeJob,
    inputs: Mapping[str, list[Value]],
    *,
    registry: Registry | None = None,
    nodes: Mapping[str, NodeFn] | None = None,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float, str], None] | None = None,
    cancel: CancelToken | None = None,
    tracker: Tracker | None = None,
) -> NodeOutcome:
    """Run the job's node on materialised inputs (raises ``MapiError`` on failure).

    ``tracker`` receives what the node logs (``NodeContext.tracker``).
    """
    impl = get_node(job.use)
    if impl is None:
        raise MapiError(f"unknown node implementation {job.use!r}", code=ErrorCode.NODE_UNKNOWN)
    table = node_registry(nodes)
    fn = table.get(impl.id)
    if fn is None:
        raise MapiError(f"{impl.id} does not run in the sandbox", code=ErrorCode.NODE_UNKNOWN)
    policies = _policies(job.params)
    gov = GovernanceServices(
        resolver=(lambda _m, _p: policies) if "_policies" in job.params else None
    )
    params = {k: v for k, v in job.params.items() if not k.startswith("_")}
    ctx = NodeContext(
        node_id=job.node_id,
        use=job.use,
        params=params,
        inputs=dict(inputs),
        registry=registry,
        seed=job.seed,
        cancel=cancel,
        log=log,
        progress=progress,
        input_refs={},
        services={"governance": gov, "sandbox": True},
        tracker=tracker,
    )
    return fn(ctx)


def _dataset_files(port: str, value: DatasetValue) -> list[OutputFile]:
    schema = schema_of(value)
    ds = with_schema(value.data, schema)
    data = write_zarr_zip(ds)
    assert data is not None
    summary = data_summary(value.data, schema)
    meta = {
        "pipeline": value.pipeline,
        "replayable": value.replayable,
        "notes": value.notes,
        "root": value.root.ref if value.root is not None else value.ref,
    }
    base = f"{OUTPUT_PREFIX}{port}/"
    return [
        OutputFile(base + "data.zarr.zip", data),
        OutputFile(base + "schema.json", schema.model_dump_json(exclude_none=True).encode()),
        OutputFile(base + "summary.json", summary.model_dump_json().encode()),
        OutputFile(base + "meta.json", json.dumps(meta, sort_keys=True, default=str).encode()),
    ]


def output_files(
    job: WorkflowNodeJob, outcome: NodeOutcome
) -> tuple[NodeResultSidecar, list[OutputFile]]:
    """The ``node_result.json`` sidecar and the objects of a finished node."""
    impl = get_node(job.use)
    assert impl is not None
    files: list[OutputFile] = []
    outputs: dict[str, NodeOutputRef] = {}
    deferred: list[str] = []
    room = (
        NODE_RESULT_MAX_BYTES
        - _SIDECAR_RESERVE
        - len(json.dumps(outcome.result, separators=(",", ":"), default=str).encode())
    )
    for port, value in outcome.outputs.items():
        want = impl.outputs.get(port)
        if want is None:
            continue
        if isinstance(value, ModelValue):
            deferred.append(port)  # the control plane fits it as a fit run
            continue
        if isinstance(value, DatasetValue):
            files.extend(_dataset_files(port, value))
            outputs[port] = NodeOutputRef(
                type=PortType.DATASET, object=f"{OUTPUT_PREFIX}{port}/data.zarr.zip"
            )
            continue
        body = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
        ).encode()
        cap = MAX_INLINE_PLOT if want is PortType.PLOT else MAX_INLINE_OUTPUT
        if len(body) <= min(cap, room):
            outputs[port] = NodeOutputRef(type=want, inline=json.loads(body))
            room -= len(body)
        else:
            name = f"{OUTPUT_PREFIX}{port}.json"
            files.append(OutputFile(name, body))
            outputs[port] = NodeOutputRef(type=want, object=name)
    result = dict(outcome.result)
    for port, ptype in impl.outputs.items():
        if ptype is PortType.MODEL_VERSION and port not in outputs and port not in deferred:
            deferred.append(port)
    if deferred:
        result["deferred_outputs"] = list(deferred)
    sidecar = NodeResultSidecar(
        node_id=job.node_id,
        use=job.use,
        result=result,
        outputs=outputs,
        warnings_plain=[w[:500] for w in outcome.warnings_plain[:50]],
    )
    return sidecar, files


def dataset_value(
    ds: xr.Dataset, ref: str | None = None, meta: Mapping[str, Any] | None = None
) -> DatasetValue:
    """A dataset input of a node run. Its producer's lineage is kept for the record, but the
    root data is not in this sandbox: a model fitted here uses the derived data as it is
    (the control plane fits ``model.fit`` on the root with the replayed pipeline)."""
    notes = [str(n) for n in (meta or {}).get("notes") or []]
    return DatasetValue(
        data=ds,
        schema=schema_of(ds),
        ref=ref,
        notes=notes,
        replayable=bool((meta or {}).get("replayable", True)),
    )
