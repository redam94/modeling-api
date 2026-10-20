"""Pretrained weights: a weights dataset (a ``safetensors`` ingest) to a network and back.

The architecture comes from the ``mapi_weights`` document's ``arch`` (the file's
``__metadata__["mapi_arch"]`` or the ingest's ``weights.arch`` option); it must be an
:class:`~mapi_jax.nets.ArchSpec`. Tensor names follow :func:`mapi_jax.nets.network_tensors`
(``backbone.*`` is required; a ``head.*`` in the file is ignored).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import xarray as xr
from mapi_sdk.safetensors import file_sha256, tensors_from_dataset, write_safetensors
from mapi_spec.errors import ErrorCode, MapiError
from pydantic import ValidationError

from mapi_jax.nets import ArchSpec, Network, build_network, load_tensors, network_tensors

__all__ = ["ARCH_METADATA", "Pretrained", "pretrained_bytes", "read_pretrained"]

ARCH_METADATA = "mapi_arch"
"""The safetensors ``__metadata__`` key that holds the architecture JSON."""


@dataclass(frozen=True, slots=True)
class Pretrained:
    arch: ArchSpec
    tensors: dict[str, np.ndarray]
    sha256: str | None

    def network(self, n_out: int, *, seed: int) -> Network:
        """A network of this architecture with the pretrained backbone and a new head."""
        net = build_network(self.arch, n_out, seed=seed)
        load_tensors(net, self.tensors, prefix="backbone.", strict=True)
        return net


def read_pretrained(ds: xr.Dataset, *, name: str = "pretrained") -> Pretrained:
    """The architecture and tensors of a weights dataset (side table ``name``)."""
    tensors, doc = tensors_from_dataset(ds)
    arch_doc = doc.get("arch")
    if arch_doc is None:
        meta = doc.get("metadata")
        if isinstance(meta, Mapping) and isinstance(meta.get(ARCH_METADATA), str):
            arch_doc = json.loads(str(meta[ARCH_METADATA]))
    if not isinstance(arch_doc, Mapping):
        raise MapiError(
            f"the weights {name!r} have no architecture; give the safetensors metadata a "
            f"{ARCH_METADATA!r} entry or set weights.arch at ingest",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/side_tables/{name}",
        )
    try:
        arch = ArchSpec.model_validate(dict(arch_doc))
    except ValidationError as exc:
        raise MapiError(
            f"the architecture of the weights {name!r} is not valid",
            code=ErrorCode.SCHEMA_INVALID,
            pointer=f"/data/side_tables/{name}",
            untrusted_text=str(exc)[:2000],
        ) from None
    sha = doc.get("sha256")
    return Pretrained(arch, tensors, sha if isinstance(sha, str) else None)


def pretrained_bytes(net: Network, arch: ArchSpec, *, backbone_only: bool = True) -> bytes:
    """safetensors bytes of ``net`` with the ``mapi_arch`` metadata (what a fit's
    ``weights.safetensors`` or a test fixture holds)."""
    tensors = network_tensors(net)
    if backbone_only:
        tensors = {k: v for k, v in tensors.items() if k.startswith("backbone.")}
    return write_safetensors(tensors, {ARCH_METADATA: arch.model_dump_json(exclude_none=False)})


def bytes_sha256(data: bytes) -> str:
    return file_sha256(data)
