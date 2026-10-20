"""``safetensors`` ingest (v0.15): a file of pretrained network weights becomes a weights
dataset, which models and steps read as a side table.

The file is parsed with :func:`mapi_sdk.safetensors.read_safetensors` under strict caps
(header size, tensor count, ``WeightsOptions.max_parameters``) and stored as one flat
``weights(param)`` float32 variable plus the tensor table, the file's metadata, the network
architecture and the file's sha256 in the ``mapi_weights`` attribute (a dataset allows few
dims, so one dim per tensor axis is not possible). Integer and bool tensors are kept only when
float32 holds them exactly; F64/F16/BF16 tensors are cast (a note says so).

The architecture comes from the file's ``__metadata__["mapi_arch"]`` (what ``mapi-jax``
writes), else from ``WeightsOptions.arch``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from mapi_sdk.safetensors import file_sha256, read_safetensors, tensor_dtypes, weights_dataset
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import WEIGHTS_ATTR, WEIGHTS_DIM, WEIGHTS_VAR, WeightsOptions
from mapi_spec.schema import DataSchema, DimPolicy, DimSpec, Role, VarSpec

from mapi_runner.worker.tabular import IngestLimits, problem
from mapi_runner.worker.tree_ingest import Upload

__all__ = ["read_weights", "weights_schema"]

_POINTER: Final = "/mapping/weights"
_ARCH_META: Final = "mapi_arch"
_MAX_LISTED: Final = 10


def weights_schema() -> DataSchema:
    """The schema of every weights dataset: ``weights(param)`` float32, role constant."""
    return DataSchema(
        dims={WEIGHTS_DIM: DimSpec(kind="integer", policy=DimPolicy.FIXED)},
        vars={
            WEIGHTS_VAR: VarSpec(
                dims=(WEIGHTS_DIM,),
                dtype="float32",
                role=Role.CONSTANT,
                missing="forbid",
                description="Network weights: every tensor flattened, in name order",
            )
        },
    )


def read_weights(path: Path, opts: WeightsOptions | None, limits: IngestLimits) -> Upload:
    """Parse a safetensors upload into a weights dataset (see the module docstring)."""
    opts = opts or WeightsOptions()
    size = path.stat().st_size
    if size > limits.max_uncompressed_bytes:
        raise problem(
            ErrorCode.PAYLOAD_TOO_LARGE,
            f"the weights file has {size:,} bytes; the limit of this run is "
            f"{limits.max_uncompressed_bytes:,} bytes",
            pointer=_POINTER,
            action="Use a larger resource class, or a smaller network.",
        )
    data = path.read_bytes()
    try:
        tensors, metadata = read_safetensors(data, max_parameters=opts.max_parameters)
        dtypes = tensor_dtypes(data, max_parameters=opts.max_parameters)
    except MapiError as exc:
        raise problem(
            exc.code,
            f"the file is not a valid safetensors file: {exc.detail}",
            pointer=_POINTER,
            action="Save the weights with safetensors (or mapi_sdk.write_safetensors), or "
            "increase max_parameters when the network is larger.",
            untrusted=exc.untrusted_text,
        ) from None
    if not tensors:
        raise problem(
            ErrorCode.SCHEMA_INVALID, "the safetensors file has no tensors", pointer=_POINTER
        )
    notes: list[dict[str, str]] = []
    arch = None if _ARCH_META in metadata else opts.arch
    if _ARCH_META in metadata and opts.arch is not None:
        notes.append(
            {
                "pointer": f"{_POINTER}/arch",
                "message": "The file has its own architecture (mapi_arch). The arch option "
                "is not used.",
            }
        )
    ds = weights_dataset(tensors, metadata, arch=arch, sha256=file_sha256(data), dtypes=dtypes)
    parameters = int(ds.sizes[WEIGHTS_DIM])
    notes.append(
        {
            "pointer": _POINTER,
            "message": f"The file has {len(tensors)} tensors and {parameters:,} parameters.",
        }
    )
    cast = sorted(name for name, dtype in dtypes.items() if dtype != "F32")
    if cast:
        listed = ", ".join(cast[:_MAX_LISTED]) + (" ..." if len(cast) > _MAX_LISTED else "")
        notes.append(
            {
                "pointer": _POINTER,
                "message": f"{len(cast)} tensors are stored as float32 (from other types): "
                f"{listed}",
            }
        )
    doc = json.loads(str(ds[WEIGHTS_VAR].attrs.get(WEIGHTS_ATTR, "{}")))
    if doc.get("arch") is None:
        notes.append(
            {
                "pointer": f"{_POINTER}/arch",
                "message": "The weights have no architecture. A model that loads them needs "
                "one: add mapi_arch to the file metadata, or set the arch option.",
            }
        )
    return Upload(ds, weights_schema(), None, [], notes, size)
