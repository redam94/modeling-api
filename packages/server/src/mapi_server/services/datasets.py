"""Dataset rows and versions outside a route (v0.18; extracted from ``api/datasets.py``).

The dataset routes and the sample loop (:mod:`mapi_server.services.samples`) create datasets
and ingest versions the same way:

* :func:`create_dataset_row` adds a dataset to a project (``CONFLICT`` when the name is taken);
* :func:`admit_upload` admits bytes against the tenant's storage quota (``QUOTA_EXCEEDED``);
* :func:`create_version` turns uploads whose files arrived into a new ``ingesting`` version
  and submits its ``ingest`` run.

``ctx`` decides who acts: a request's context in the routes, a synthetic context of the
sample's installer in the loop (so admission, attribution and quotas work as for a request).
The checks of the uploads themselves (they belong to the dataset, arrived, are used once)
stay with the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mapi_spec.api import new_id
from mapi_spec.ingest import IngestAssembly
from sqlalchemy.exc import IntegrityError as DBIntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import Conflict, ProblemWithHeaders
from mapi_server.services.quotas import admit_storage
from mapi_server.services.runs import Scope, now
from mapi_server.services.submit import submit_ingest

__all__ = ["admit_upload", "create_dataset_row", "create_version"]


async def create_dataset_row(
    session: AsyncSession,
    scope: Scope,
    *,
    name: str,
    description: str | None = None,
    labels: Mapping[str, str] | None = None,
) -> m.Dataset:
    """A new dataset in ``scope`` (no versions yet). ``CONFLICT`` when the project already
    has a dataset named ``name``; the caller's transaction stays usable."""
    ts = now()
    ds = m.Dataset(
        id=new_id("ds"),
        tenant_id=scope.tenant_id,
        project_id=scope.project_id,
        name=name,
        description=description,
        labels=dict(labels or {}),
        next_version=1,
        created_at=ts,
        updated_at=ts,
    )
    try:
        async with session.begin_nested():
            session.add(ds)
    except DBIntegrityError:
        raise Conflict(f"a dataset named {name!r} already exists") from None
    return ds


async def admit_upload(
    ctx: ServerContext, session: AsyncSession, tenant_id: str, nbytes: int
) -> None:
    """Admit ``nbytes`` of upload against the tenant's storage quota (429/403 otherwise)."""
    async with ctx.db.session() as system:
        decision = await admit_storage(
            session, system, tenant_id=tenant_id, nbytes=nbytes, now=now()
        )
    if not decision.allowed:
        assert decision.code is not None
        raise ProblemWithHeaders(
            decision.message or "storage quota exceeded",
            code=decision.code,
            headers=decision.headers(),
        )


async def create_version(
    session: AsyncSession,
    ctx: ServerContext,
    ds: m.Dataset,
    uploads: Sequence[tuple[m.Upload, int]],
    *,
    mapping: dict[str, Any] | None = None,
    assembly: IngestAssembly | None = None,
    message: str | None = None,
) -> m.DatasetVersion:
    """The next version of ``ds`` from ``uploads`` (each paired with the generation its file
    is pinned to, in source order): the uploads become ``consumed``, the version is stored
    ``ingesting`` and one ``ingest`` run is submitted in ``ctx`` (its admission may refuse
    it, which rolls the caller's transaction back). Give a ``mapping`` (one upload) or an
    ``assembly``. ``ds`` should be row-locked by the caller (the version number comes
    from it)."""
    if (mapping is None) == (assembly is None) or not uploads:
        raise ValueError("give a mapping (one upload) or an assembly")
    version = ds.next_version
    ds.next_version += 1
    ds.updated_at = now()
    for upload, _ in uploads:
        upload.status = "consumed"
        upload.consumed_by_version = version
    stored_mapping = (
        {"assembly": assembly.model_dump(mode="json", exclude_none=True)}
        if assembly is not None
        else mapping
    )
    dv = m.DatasetVersion(
        dataset_id=ds.id,
        version=version,
        tenant_id=ds.tenant_id,
        project_id=ds.project_id,
        status="ingesting",
        upload_id=uploads[0][0].id,
        sources=[upload.id for upload, _ in uploads],
        mapping=stored_mapping,
        message=message,
        objects={},
        issues=[],
        created_at=now(),
    )
    session.add(dv)
    await session.flush()
    run = await submit_ingest(
        session,
        ctx,
        dataset=ds,
        version=version,
        uploads=list(uploads),
        mapping=mapping,
        assembly=assembly,
    )
    dv.ingest_run_id = run.id
    await session.flush()
    return dv
