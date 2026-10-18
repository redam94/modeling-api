"""Pure helpers of the M4 dataset calls shared by the sync and async clients: multi-file
versions (an :class:`~mapi_spec.ingest.IngestAssembly` built from simple arguments) and
upload inspection.

Nothing here does I/O: :func:`plan_assembly` checks the whole assembly (with placeholder
upload ids) *before* any byte is uploaded, and :func:`assembly_for` fills in the real ids.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from mapi_spec.api import IngestAssembly, IngestMapping, UploadInspection, UploadSession
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.ingest import AssemblySource, CombineRule, CoordValue
from mapi_spec.tree import ROOT, TreeJoin

from mapi_client.transfer import UploadSource, default_mapping, mapping_reads, upload_source

if TYPE_CHECKING:
    import xarray as xr

__all__ = [
    "AssemblyFile",
    "InspectedUpload",
    "PlannedSource",
    "assembly_for",
    "is_not_ready",
    "plan_assembly",
]

_PENDING: Final = "upl_pending_{}"


@dataclass(frozen=True, slots=True)
class AssemblyFile:
    """One file of a multi-file dataset version and where it goes (M4).

    ``source`` is anything :meth:`Client.datasets.upload` accepts (a path, bytes with
    ``filename``, an ``xr.Dataset`` or an ``xr.DataTree``). The file is ingested with
    ``mapping`` (default: the shared ``mapping`` of :meth:`Client.datasets.upload_files`,
    else the empty mapping of a self-describing format) into node ``node``. ``combine``
    defaults to ``"concat"`` when ``concat_dim`` is given and to ``"node"`` otherwise;
    ``join`` aligns coords for ``concat``/``merge``.
    """

    source: str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes
    mapping: IngestMapping | None = None
    node: str = ROOT
    combine: CombineRule | None = None
    concat_dim: str | None = None
    concat_label: CoordValue | None = None
    join: TreeJoin = "exact"
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedSource:
    """A checked file of an assembly: the bytes to upload and its source minus the id."""

    upload: UploadSource
    source: AssemblySource


@dataclass(frozen=True, slots=True)
class InspectedUpload:
    """An upload that was staged (not ingested) and what its inspect run found. Ingest it
    later with ``create_version(dataset_id, upload_id=upload.id, mapping=...)``."""

    upload: UploadSession
    inspection: UploadInspection

    @property
    def upload_id(self) -> str:
        return self.upload.id

    @property
    def dataset_id(self) -> str:
        return self.upload.dataset_id


def _as_file(
    item: AssemblyFile | str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes,
    index: int,
    *,
    node: str,
    concat: str | None,
    labels: Sequence[CoordValue] | None,
    merge: bool,
    join: TreeJoin,
) -> AssemblyFile:
    if isinstance(item, AssemblyFile):
        return item
    if concat is not None:
        assert labels is not None
        return AssemblyFile(
            item, node=node, combine="concat", concat_dim=concat, concat_label=labels[index],
            join=join,
        )  # fmt: skip
    return AssemblyFile(item, node=node, combine="merge" if merge else "node", join=join)


def _mapping_for(
    src: UploadSource, own: IngestMapping | None, shared: IngestMapping | None
) -> IngestMapping:
    """A file's own mapping (which must read it), else the shared one when it reads the file,
    else the empty mapping of a self-describing format."""
    if own is not None:
        if not mapping_reads(own.format, src.format):
            raise ValueError(
                f"{src.filename}: mapping.format {own.format!r} does not match {src.format!r}"
            )
        return own
    if shared is not None and mapping_reads(shared.format, src.format):
        return shared
    try:
        return default_mapping(src.format)
    except ValueError as exc:
        raise ValueError(f"{src.filename}: {exc}") from exc


def plan_assembly(
    files: Sequence[AssemblyFile | str | os.PathLike[str] | xr.Dataset | xr.DataTree | bytes],
    *,
    mapping: IngestMapping | None = None,
    node: str = ROOT,
    concat: str | None = None,
    labels: Sequence[CoordValue] | None = None,
    merge: bool = False,
    join: TreeJoin = "exact",
) -> list[PlannedSource]:
    """Check a multi-file version before uploading anything.

    Plain items (anything but an :class:`AssemblyFile`) all go to ``node`` and combine there
    by ``concat`` (the dim; ``labels`` gives one label per plain item, in order), ``merge``
    or - with a single plain item - as the node's only source. :class:`AssemblyFile` items
    carry their own placement. Raises ``ValueError`` (or the contract's validation error)
    when the assembly is not valid.
    """
    if not files:
        raise ValueError("give at least one file")
    if concat is not None and merge:
        raise ValueError("combine plain files by concat or by merge, not both")
    plain = sum(not isinstance(f, AssemblyFile) for f in files)
    if concat is not None:
        if labels is None or len(labels) != plain:
            raise ValueError(
                f"concat along {concat!r} needs one label per file: got "
                f"{0 if labels is None else len(labels)} labels for {plain} files"
            )
    elif labels is not None:
        raise ValueError("labels are only used with concat")
    planned: list[PlannedSource] = []
    k = 0
    for i, item in enumerate(files):
        spec = _as_file(item, k, node=node, concat=concat, labels=labels, merge=merge, join=join)
        if not isinstance(item, AssemblyFile):
            k += 1
        src = upload_source(spec.source, filename=spec.filename)
        ingest = _mapping_for(src, spec.mapping, mapping)
        combine = spec.combine or ("concat" if spec.concat_dim is not None else "node")
        source = AssemblySource(
            upload_id=_PENDING.format(i),
            mapping=ingest,
            node=spec.node,
            combine=combine,
            concat_dim=spec.concat_dim,
            concat_label=spec.concat_label,
            join=spec.join,
        )
        planned.append(PlannedSource(src, source))
    IngestAssembly(sources=[p.source for p in planned])  # the contract's checks, early
    return planned


def assembly_for(planned: Sequence[PlannedSource], upload_ids: Sequence[str]) -> IngestAssembly:
    """The assembly of ``planned`` with the ids of the uploads made for it."""
    if len(planned) != len(upload_ids):
        raise ValueError("one upload id per planned source")
    return IngestAssembly(
        sources=[
            p.source.model_copy(update={"upload_id": uid})
            for p, uid in zip(planned, upload_ids, strict=True)
        ]
    )


def is_not_ready(exc: MapiError) -> bool:
    return exc.code is ErrorCode.NOT_READY
