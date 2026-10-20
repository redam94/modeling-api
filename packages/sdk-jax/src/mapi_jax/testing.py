"""Test helpers: tiny randomly initialised "pretrained" networks.

Fixtures are written at test time (never a committed binary). ``tiny_pretrained`` gives the
safetensors bytes a user would upload; ``tiny_weights_dataset`` the weights dataset a
``safetensors`` ingest makes of them (a ``data.side_tables`` entry for
``dl.finetune_classifier`` or ``dl.embed``).
"""

from __future__ import annotations

import xarray as xr
from mapi_sdk.safetensors import file_sha256, read_safetensors, weights_dataset
from mapi_sdk.schema import infer_schema
from mapi_spec.schema import DataSchema

from mapi_jax.nets import ArchSpec, build_network
from mapi_jax.weights import pretrained_bytes

__all__ = ["TINY_ARCH", "image_schema", "tiny_pretrained", "tiny_weights_dataset"]

TINY_ARCH = ArchSpec(kind="resnet", channels=1, widths=[4, 8], blocks=[1, 1], groups=2)


def tiny_pretrained(*, arch: ArchSpec = TINY_ARCH, seed: int = 0) -> bytes:
    """safetensors bytes of a randomly initialised backbone with ``mapi_arch`` metadata."""
    net = build_network(arch, 2, seed=seed)
    return pretrained_bytes(net, arch, backbone_only=True)


def tiny_weights_dataset(*, arch: ArchSpec = TINY_ARCH, seed: int = 0) -> xr.Dataset:
    """The weights dataset of :func:`tiny_pretrained` (as the ingest writes it)."""
    data = tiny_pretrained(arch=arch, seed=seed)
    tensors, metadata = read_safetensors(data)
    return weights_dataset(tensors, metadata, sha256=file_sha256(data))


def image_schema(ds: xr.Dataset, *, target: str = "label", dim: str = "image") -> DataSchema:
    """The raw schema an ``image_folder`` ingest gives ``ds``: ``target`` is the target
    (missing allowed) and ``dim`` takes new images (``new_levels``, strategy ``pooled``)."""
    schema = infer_schema(ds, targets=[target], policies={dim: "new_levels"})
    image = schema.dims[dim].model_copy(update={"new_level_strategy": "pooled"})
    label = schema.vars[target].model_copy(update={"missing": "allow"})
    return schema.model_copy(
        update={"dims": {**schema.dims, dim: image}, "vars": {**schema.vars, target: label}}
    )
