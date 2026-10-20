"""v0.15 resources of the asynchronous client (see :mod:`mapi_client._v015`): resumable
uploads, the ``blobs/missing`` query, image collections and pretrained weights (mixed into
:class:`~mapi_client._async.AsyncDatasets`). Hashing and part files run in a worker thread."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from mapi_spec.api import BlobMissing, DatasetVersion, UploadSession
from mapi_spec.collection import DEFAULT_PART_BYTES, CollectionOptions
from mapi_spec.errors import ErrorCode, MapiError

from mapi_client import _calls_v015 as vcalls
from mapi_client._v015_common import (
    MANIFEST_FILENAME,
    Progress,
    blob_queries,
    collection_key,
    collection_options,
    collection_source,
    merge_missing,
    next_base,
    part_filename,
    upload_action,
    weights_mapping,
)
from mapi_client.collections import (
    PartFile,
    UploadState,
    client_manifest_csv,
    scan_images,
    write_parts,
)
from mapi_client.transfer import UploadSource

if TYPE_CHECKING:
    from mapi_client._async import AsyncClient

__all__ = ["AsyncDatasetsV015Mixin"]


class AsyncDatasetsV015Mixin:
    """Resumable uploads, image collections and weights of ``client.datasets`` (v0.15)."""

    _c: AsyncClient

    # ------------------------------------------------------------------ upload sessions
    async def get_upload(self, dataset_id: str, upload_id: str) -> UploadSession:
        """An upload session; ``received`` says whether its file arrived."""
        return await self._c.send(vcalls.get_upload(dataset_id, upload_id))

    async def renew_upload(self, dataset_id: str, upload_id: str) -> UploadSession:
        """A fresh signed PUT (1 hour) for an upload whose file has not arrived."""
        return await self._c.send(vcalls.renew_upload(dataset_id, upload_id))

    async def missing_images(
        self, dataset_id: str, shas: Sequence[str], *, base_version: int | None = None
    ) -> BlobMissing:
        """Which image sha256 the base version lacks (``None``: the latest ready collection
        version; ``0``: no base, everything is missing). Any number of sha256: they are sent
        in chunks, every chunk compared against the same version."""
        queries = blob_queries(shas, base_version)
        if not queries:
            raise ValueError("give at least one sha256")
        first = await self._c.send(vcalls.find_missing_images(dataset_id, queries[0]))
        results = [first]
        pinned = next_base(first)
        for query in queries[1:]:
            query = query.model_copy(update={"base_version": pinned})
            results.append(await self._c.send(vcalls.find_missing_images(dataset_id, query)))
        return merge_missing(results, list(shas))

    # --------------------------------------------------------------------- collections
    async def upload_collection(
        self,
        dataset: str,
        root: str | os.PathLike[str],
        *,
        manifest: str | os.PathLike[str] | None = None,
        label_from_folder: bool = False,
        options: CollectionOptions | None = None,
        base_version: int | None = None,
        part_bytes: int = DEFAULT_PART_BYTES,
        state_file: str | os.PathLike[str] | None = None,
        message: str | None = None,
        wait: bool = False,
        timeout: float | None = None,
        progress: Progress | None = None,
    ) -> DatasetVersion:
        """Upload a folder of images as a new **collection** version of ``dataset`` (a name,
        created when missing, or a ``ds_...`` id).

        The images are hashed; only those the base version lacks go up, packed into
        deterministic zip parts of at most ``part_bytes``. With ``state_file`` an
        interrupted upload resumes: parts that arrived are skipped, expired upload links are
        renewed. ``base_version``: ``None`` the latest ready collection version, ``0`` none.
        """
        report = progress or (lambda _s, _d, _t: None)
        datasets = self._c.datasets
        ds_id = dataset if dataset.startswith("ds_") else (await datasets.ensure(dataset)).id
        plan = await asyncio.to_thread(
            scan_images,
            Path(root),
            None if manifest is None else Path(manifest),
            path_column=(options.path_column if options and options.path_column else "path"),
            label_from_folder=label_from_folder,
        )
        report("hash", len(plan.images), len(plan.images))
        opts = collection_options(plan, options, label_from_folder=label_from_folder)
        manifest_bytes = client_manifest_csv(plan)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        state_path = None if state_file is None else Path(state_file)
        state = (
            UploadState(ds_id, manifest_sha)
            if state_path is None
            else UploadState.load(state_path, dataset_id=ds_id, manifest_sha256=manifest_sha)
        )

        def save() -> None:
            if state_path is not None:
                state.save(state_path)

        shas = plan.shas
        report("check", 0, len(shas))
        found = await self.missing_images(ds_id, shas, base_version=base_version)
        report("check", len(shas), len(shas))
        base = next_base(found)
        manifest_src = UploadSource(
            MANIFEST_FILENAME, len(manifest_bytes), "csv", "text/csv", data=manifest_bytes
        )
        session, arrived = await self._session_for(ds_id, state.manifest_upload_id, manifest_src)
        manifest_id = state.manifest_upload_id = session.id
        save()
        if not arrived:
            await self._put(ds_id, session, manifest_src)
        with tempfile.TemporaryDirectory(prefix="mapi-parts-") as tmp:
            parts = await asyncio.to_thread(write_parts, plan, found.missing, Path(tmp), part_bytes)
            total = sum(p.size for p in parts)
            done = 0
            report("upload", done, total)
            part_ids: list[str] = []
            for part in parts:
                part_ids.append(await self._upload_part(ds_id, state, part, save))
                done += part.size
                report("upload", done, total)
        report("create", 0, 1)
        created = await datasets.create_version(
            ds_id,
            collection=collection_source(manifest_id, part_ids, base, opts),
            message=message,
            idempotency_key=collection_key(ds_id, manifest_id, part_ids),
        )
        report("create", 1, 1)
        if state_path is not None:
            state_path.unlink(missing_ok=True)
        if not wait:
            return created
        report("wait", 0, 1)
        return await datasets.wait_version(ds_id, created.version, timeout=timeout)

    async def _upload_part(
        self, dataset_id: str, state: UploadState, part: PartFile, save: Callable[[], None]
    ) -> str:
        """Upload one part (skipped when the saved session of the same bytes has it)."""
        saved = state.part(part.index, part.sha256)
        previous = saved.get("upload_id") if saved else None
        src = UploadSource(
            part_filename(part.index), part.size, "zarr_zip", "application/zip", path=part.path
        )
        session, arrived = await self._session_for(
            dataset_id, previous if isinstance(previous, str) else None, src
        )
        entry = {"index": part.index, "sha256": part.sha256, "size": part.size}
        state.set_part({**entry, "upload_id": session.id, "received": arrived})
        save()  # before the PUT: an interrupted PUT resumes on this session
        if not arrived:
            await self._put(dataset_id, session, src)
            state.set_part({**entry, "upload_id": session.id, "received": True})
            save()
        return session.id

    async def _session_for(
        self, dataset_id: str, upload_id: str | None, src: UploadSource
    ) -> tuple[UploadSession, bool]:
        """The session to upload ``src`` with and whether its file already arrived: the saved
        session ``upload_id`` when it can still be used (renewed when its link expired), else
        a new one."""
        if upload_id is not None:
            session: UploadSession | None = None
            try:
                session = await self.get_upload(dataset_id, upload_id)
            except MapiError as exc:
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
            if session is not None and session.size_bytes == src.size:
                action = upload_action(session)
                if action == "done":
                    return session, True
                if action == "renew":
                    return (await self.renew_upload(dataset_id, session.id)), False
                if action == "put":
                    return session, False
        created = await self._c.datasets.create_upload(
            dataset_id, filename=src.filename, size_bytes=src.size, content_type=src.content_type
        )
        return created, False

    async def _put(self, dataset_id: str, session: UploadSession, src: UploadSource) -> None:
        """PUT ``src``; when that fails (the link may have expired during a long upload),
        renew the link once and try again - unless the file arrived after all."""
        try:
            await self._c.put_signed(session, src)
        except MapiError:
            if (await self.get_upload(dataset_id, session.id)).received:
                return
            await self._c.put_signed(await self.renew_upload(dataset_id, session.id), src)

    # ------------------------------------------------------------------------ weights
    async def upload_weights(
        self,
        source: str | os.PathLike[str],
        *,
        dataset: str | None = None,
        dataset_id: str | None = None,
        arch: dict[str, object] | str | os.PathLike[str] | None = None,
        message: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
    ) -> DatasetVersion:
        """Upload a ``.safetensors`` file of pretrained network weights as a dataset version
        (format ``safetensors``). ``arch`` is the architecture JSON (a mapping or a file) for
        a file whose metadata has no ``mapi_arch``."""
        path = Path(source).expanduser()
        if path.suffix.lower() != ".safetensors":
            raise ValueError(f"{path.name} is not a .safetensors file")
        mapping = weights_mapping(arch if isinstance(arch, dict | None) else Path(arch))
        return await self._c.datasets.upload(
            path,
            dataset=dataset,
            dataset_id=dataset_id,
            mapping=mapping,
            message=message,
            wait=wait,
            timeout=timeout,
        )
