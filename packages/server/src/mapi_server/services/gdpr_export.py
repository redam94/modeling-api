"""GDPR export: one zip archive of everything a tenant stored (plan §9 "Data lifecycle").

``POST /v1/tenants/{id}/exports`` (tenant admins; allowed while suspended, including during
a deletion's grace period) queues an export; the orchestrator's procrastinate job
(:class:`ExportBuilder`) builds the archive and stores it at
``t/{tenant}/exports/{export_id}.zip`` in the data bucket; ``GET …/exports/{export_id}``
then answers with a short-lived signed download link (``MAPI_EXPORT_LINK_TTL_S``). The
archive is deleted after ``MAPI_EXPORT_RETENTION_S`` (and by a tenant deletion's purge).

A build holds a lease: ``started_at`` plus ``MAPI_EXPORT_BUILD_TIMEOUT_S`` (the builder gives
up at that point) plus :data:`LEASE_SLACK`. A row still ``running`` after its lease belongs to
a process that died (an OOM kill, a lost orchestrator): :meth:`ExportBuilder.due` queues it
again once, then fails it (:data:`INTERRUPTED`), so a tenant is never blocked from exporting.
Only the build that holds the current lease may mark the export ready.

Contents (``include`` selects the parts):

* ``metadata/<table>.jsonl``: every row of every tenant table (projects, members, tokens,
  datasets and versions with their schemas and profiles, specs and revisions with their
  YAML, runs, models and versions, plots, ...), discovered from the catalog so tables added
  later are exported too. Columns that hold credentials (token hashes, HMAC keys, nonces,
  webhook secrets) are redacted;
* ``datasets/<dataset>/v<n>/<object>``: the stored objects of every ready dataset version,
  copied byte for byte (never decoded), up to ``MAPI_EXPORT_MAX_INLINE_BYTES`` in total;
  the rest is listed with size and checksums in ``manifest.json``;
* ``models/<model>/v<n>/manifest.json`` (the signed bundle manifest) and
  ``model_version.json`` (lineage, diagnostics, scorecard, metrics, artifact list);
* ``audit/audit_events.jsonl`` and ``usage/cost_ledger.jsonl``;
* ``manifest.json`` (what is inside, with SHA-256 per entry) and ``README.txt``.

Tenant rows are read through the tenant's own row-level-security session wherever the
request-path role may read the table, so an export can only ever contain its own tenant's
rows. The control plane reads object bytes but never parses them.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import tempfile
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.errors import ErrorCode
from mapi_spec.storage import NotFound, StorageError
from mapi_spec.tenancy import ResourceKind
from mapi_spec.trust import EXPORT_PARTS, GdprExport, GdprExportRequest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.request import RequestAuth
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError, ProblemWithHeaders
from mapi_server.services.audit import write_audit
from mapi_server.services.runs import now as utcnow

__all__ = [
    "INTERRUPTED",
    "LEASE_SLACK",
    "REDACTED",
    "ExportBuilder",
    "create_export",
    "expire_exports",
    "export_dto",
    "export_object_name",
    "get_export",
]

log = logging.getLogger(__name__)

REDACTED: Final = "[redacted]"
_SECRET_COLUMN: Final = re.compile(r"(secret|hmac|nonce|password|token_hash|^key$|_key$)")
_SPLIT_TABLES: Final = {"audit_events": "audit", "cost_ledger": "usage"}
"""Tables exported under their own part instead of ``metadata``."""
INTERRUPTED: Final = "interrupted: built again"
"""``error`` of an export whose first build died: a second death fails it."""
LEASE_SLACK: Final = timedelta(minutes=5)
READ_CHUNK: Final = 8 * 1024 * 1024
ROW_BATCH: Final = 2000
_README: Final = """modeling-api data export
=========================

This archive holds everything your workspace stored on modeling-api when the export ran.

  manifest.json              what is inside, with a SHA-256 checksum per file
  metadata/<table>.jsonl     one JSON object per line for every record of your workspace
  datasets/...               your uploaded data as stored (zarr zip archives and sidecars)
  models/...                 the signed manifest and metadata of every model version
  audit/audit_events.jsonl   your workspace's audit log
  usage/cost_ledger.jsonl    metered compute usage

Credentials (API token hashes, signing keys, webhook secrets) are never exported.
Dataset objects and metadata rows beyond the export size limits are listed in
manifest.json ("skipped").
"""


def export_object_name(tenant_id: str, export_id: str) -> str:
    return f"t/{tenant_id}/exports/{export_id}.zip"


# ===================================================================================== API


def export_dto(row: m.TenantExport, *, download_url: str | None = None) -> GdprExport:
    state = row.state
    if state == "ready" and row.expires_at is not None and row.expires_at <= utcnow():
        state = "expired"
    return GdprExport(
        id=row.id,
        tenant_id=row.tenant_id,
        state=state,
        include=list(row.include or []),
        requested_by=row.requested_by,
        created_at=row.created_at,
        ready_at=row.ready_at,
        expires_at=row.expires_at,
        size_bytes=row.size_bytes,
        download_url=download_url if state == "ready" else None,
    )


async def create_export(
    session: AsyncSession,
    ctx: ServerContext,
    auth: RequestAuth,
    tenant_id: str,
    body: GdprExportRequest,
) -> m.TenantExport:
    """Queue an export (tenant session). ``QUOTA_EXCEEDED`` while one is still being built
    or after ``MAPI_EXPORTS_PER_DAY`` exports in 24 hours."""
    at = utcnow()
    running = (
        await session.execute(
            select(func.count()).where(
                m.TenantExport.tenant_id == tenant_id,
                m.TenantExport.state.in_(("queued", "running")),
            )
        )
    ).scalar_one()
    recent = (
        await session.execute(
            select(func.count(), func.min(m.TenantExport.created_at)).where(
                m.TenantExport.tenant_id == tenant_id,
                m.TenantExport.created_at > at - timedelta(days=1),
            )
        )
    ).one()
    if running or int(recent[0]) >= ctx.settings.exports_per_day:
        oldest = recent[1]
        wait = (
            60 if running or oldest is None else (oldest + timedelta(days=1) - at).total_seconds()
        )
        raise ProblemWithHeaders(
            "an export is already being built" if running else "too many exports today",
            code=ErrorCode.QUOTA_EXCEEDED,
            headers={"Retry-After": str(max(1, int(wait)))},
        )
    include = [p for p in EXPORT_PARTS if p in set(body.include)]
    row = m.TenantExport(
        id=new_id("exp"),
        tenant_id=tenant_id,
        state="queued",
        include=include,
        requested_by=auth.principal_id,
        created_at=at,
    )
    session.add(row)
    await session.flush()
    await write_audit(
        ctx.db,
        [
            auth.audit(
                ctx,
                AuditAction.TENANT_EXPORT,
                ResourceKind.TENANT_EXPORT,
                resource_id=row.id,
                tenant_id=tenant_id,
                details={"include": list(include)},
            )
        ],
        session=session,
    )
    return row


async def get_export(
    session: AsyncSession, ctx: ServerContext, tenant_id: str, export_id: str
) -> GdprExport:
    row = await session.get(m.TenantExport, export_id)
    if row is None or row.tenant_id != tenant_id:
        raise NotFoundError("export", export_id)
    url = None
    live = row.expires_at is None or row.expires_at > utcnow()
    if row.state == "ready" and row.bucket and row.object_name and live:
        signed = await ctx.store.sign_get(
            row.bucket,
            row.object_name,
            expires_in=timedelta(seconds=ctx.settings.export_link_ttl_s),
        )
        url = signed.url
    return export_dto(row, download_url=url)


# ================================================================================= builder


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return REDACTED
    return str(value)


def _redact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (REDACTED if _SECRET_COLUMN.search(k) and v is not None else v) for k, v in row.items()
    }


def _safe_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned[:120] or "unnamed"


class _Archive:
    """A zip on a spooled temporary file, with a SHA-256 per entry."""

    def __init__(self) -> None:
        self.file = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)  # noqa: SIM115
        self.zip = zipfile.ZipFile(self.file, "w", compression=zipfile.ZIP_DEFLATED)
        self.entries: dict[str, dict[str, Any]] = {}

    def write(self, name: str, data: bytes) -> None:
        self.zip.writestr(name, data)
        self.entries[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    @asynccontextmanager
    async def stream(self, name: str) -> AsyncIterator[_Entry]:
        entry = _Entry(self.zip.open(name, "w", force_zip64=True))
        try:
            yield entry
        finally:
            entry.fh.close()
            self.entries[name] = {"size": entry.size, "sha256": entry.digest.hexdigest()}

    def finish(self, manifest: dict[str, Any]) -> bytes:
        self.write("manifest.json", json.dumps(manifest, indent=2, default=_json_default).encode())
        self.zip.close()
        self.file.seek(0)
        data = self.file.read()
        self.file.close()
        return data


class _Entry:
    def __init__(self, fh: Any) -> None:
        self.fh = fh
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> None:
        self.fh.write(data)
        self.size += len(data)
        self.digest.update(data)


class ExportBuilder:
    """Builds queued exports and expires old ones (orchestrator; procrastinate job)."""

    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    def _lease(self) -> timedelta:
        return timedelta(seconds=self.ctx.settings.export_build_timeout_s) + LEASE_SLACK

    async def recover(self, *, at: datetime | None = None) -> int:
        """Re-queue (once) or fail exports whose build died: ``running`` past the lease."""
        ts = at or utcnow()
        stale = (m.TenantExport.state == "running") & (
            m.TenantExport.started_at < ts - self._lease()
        )
        async with self.ctx.db.session() as s:
            failed = await s.execute(
                update(m.TenantExport)
                .where(stale, m.TenantExport.error == INTERRUPTED)
                .values(state="failed", error="the export was interrupted twice; ask again")
            )
            requeued = await s.execute(
                update(m.TenantExport)
                .where(
                    stale, (m.TenantExport.error.is_(None)) | (m.TenantExport.error != INTERRUPTED)
                )
                .values(state="queued", started_at=None, error=INTERRUPTED)
                .returning(m.TenantExport.id)
            )
            ids = list(requeued.scalars())
            n_failed = int(getattr(failed, "rowcount", 0) or 0)
        for export_id in ids:
            log.warning("export %s: its build was interrupted; building it again", export_id)
        if n_failed:
            log.warning("%d export(s) failed after a second interrupted build", n_failed)
        return len(ids) + n_failed

    async def due(self) -> list[str]:
        await self.recover()
        async with self.ctx.db.session() as s:
            return list(
                (
                    await s.execute(
                        select(m.TenantExport.id)
                        .where(m.TenantExport.state == "queued")
                        .order_by(m.TenantExport.created_at)
                    )
                ).scalars()
            )

    async def run(self, export_id: str) -> str:
        """Build one export (idempotent: only a ``queued`` export is built)."""
        claimed_at = utcnow()
        async with self.ctx.db.session() as s:
            row = (
                await s.execute(
                    update(m.TenantExport)
                    .where(m.TenantExport.id == export_id, m.TenantExport.state == "queued")
                    .values(state="running", started_at=claimed_at)
                    .returning(m.TenantExport.tenant_id, m.TenantExport.include)
                )
            ).first()
        if row is None:
            return "skipped"
        tenant_id, include = str(row[0]), [str(p) for p in row[1] or []]
        bucket = self.ctx.buckets.data
        name = export_object_name(tenant_id, export_id)
        mine = (
            (m.TenantExport.id == export_id)
            & (m.TenantExport.state == "running")
            & (m.TenantExport.started_at == claimed_at)
        )
        timeout = self.ctx.settings.export_build_timeout_s
        try:
            data, manifest = await asyncio.wait_for(
                self._build(tenant_id, export_id, include), timeout
            )
            await asyncio.wait_for(
                self.ctx.store.put(bucket, name, data, content_type="application/zip"), timeout
            )
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                log.error("export %s of tenant %s timed out after %ss", export_id, tenant_id,
                          timeout)  # fmt: skip
                error = f"timed out after {int(timeout)} s"
                await self._discard(bucket, name)  # an upload cut short may still land
            else:
                log.exception("export %s of tenant %s failed", export_id, tenant_id)
                error = f"{type(exc).__name__}"
            async with self.ctx.db.session() as s:
                await s.execute(
                    update(m.TenantExport).where(mine).values(state="failed", error=error[:500])
                )
            return "failed"
        at = utcnow()
        async with self.ctx.db.session() as s:
            done = await s.execute(
                update(m.TenantExport)
                .where(mine)
                .values(
                    state="ready",
                    ready_at=at,
                    expires_at=at + timedelta(seconds=self.ctx.settings.export_retention_s),
                    size_bytes=len(data),
                    sha256="sha256:" + hashlib.sha256(data).hexdigest(),
                    bucket=bucket,
                    object_name=name,
                    error=None,
                )
                .returning(m.TenantExport.id)
            )
            if done.first() is None:
                lost = True
            else:
                lost = False
                await write_audit(
                    self.ctx.db,
                    [
                        make_audit_event(
                            action=AuditAction.TENANT_EXPORT,
                            resource_kind=ResourceKind.TENANT_EXPORT,
                            outcome=AuditOutcome.SUCCESS,
                            ts=at,
                            fingerprint_key=self.ctx.secrets.audit_key,
                            tenant_id=tenant_id,
                            resource_id=export_id,
                            details={"state": "ready", "size_bytes": len(data),
                                     "skipped": len(manifest["skipped"])},
                        )
                    ],
                    session=s,
                )  # fmt: skip
        if lost:
            # the lease went to another build, the export was cancelled (a tenant deletion
            # started) or its row is gone (purged): this archive must not stay behind
            await self._discard(bucket, name)
            log.warning("export %s of tenant %s: lease lost, archive discarded", export_id,
                        tenant_id)  # fmt: skip
            return "discarded"
        return "ready"

    async def _discard(self, bucket: str, name: str) -> None:
        try:
            await self.ctx.store.delete(bucket, name)
        except NotFound:
            pass
        except Exception:
            log.exception("could not delete the export archive %s/%s", bucket, name)

    # -------------------------------------------------------------------------- content
    async def _tables(self) -> list[tuple[str, bool]]:
        """Tables with a ``tenant_id`` and whether the request-path role may read them."""
        async with self.ctx.db.session() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT c.table_name, has_table_privilege(:role, c.table_name, 'SELECT') "
                        "FROM information_schema.columns c JOIN information_schema.tables t "
                        "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                        "WHERE c.table_schema = current_schema() "
                        "AND c.column_name = 'tenant_id' AND t.table_type = 'BASE TABLE' "
                        "ORDER BY c.table_name"
                    ),
                    {"role": self.ctx.db.app_role},
                )
            ).all()
        return [(str(r[0]), bool(r[1])) for r in rows]

    def _session(self, tenant_id: str, through_rls: bool) -> Any:
        return self.ctx.db.tenant_session(tenant_id) if through_rls else self.ctx.db.session()

    async def _dump_table(
        self,
        archive: _Archive,
        path: str,
        table: str,
        tenant_id: str,
        through_rls: bool,
        budget: list[int],
    ) -> tuple[int, bool]:
        """Rows written, and whether the metadata budget (``budget[0]`` bytes left) cut the
        table short: the archive is built in memory, so its size is bounded."""
        quoted = '"' + table.replace('"', '""') + '"'
        n = 0
        async with self._session(tenant_id, through_rls) as s, archive.stream(path) as out:
            result = await s.stream(
                text(f"SELECT * FROM {quoted} WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
            async for part in result.mappings().partitions(ROW_BATCH):
                buf = io.BytesIO()
                for rec in part:
                    line = json.dumps(_redact(dict(rec)), default=_json_default, sort_keys=True)
                    data = line.encode() + b"\n"
                    if len(data) > budget[0]:
                        out.write(buf.getvalue())
                        budget[0] = 0
                        await result.close()
                        return n, True
                    budget[0] -= len(data)
                    buf.write(data)
                    n += 1
                out.write(buf.getvalue())
        return n, False

    async def _build(
        self, tenant_id: str, export_id: str, include: list[str]
    ) -> tuple[bytes, dict[str, Any]]:
        archive = _Archive()
        counts: dict[str, int] = {}
        skipped: list[dict[str, Any]] = []
        archive.write("README.txt", _README.encode())
        budget = [self.ctx.settings.export_max_metadata_bytes]
        for table, readable in await self._tables():
            part = _SPLIT_TABLES.get(table, "metadata")
            if part not in include:
                continue
            path = f"{part}/{table}.jsonl"
            counts[path], cut = await self._dump_table(
                archive, path, table, tenant_id, readable, budget
            )
            if cut:
                skipped.append(
                    {"path": path, "reason": "export size limit", "rows_written": counts[path]}
                )
        if "datasets" in include:
            skipped.extend(await self._datasets(archive, tenant_id))
        if "models" in include:
            await self._models(archive, tenant_id)
        manifest: dict[str, Any] = {
            "format": "mapi.gdpr-export/v1",
            "export_id": export_id,
            "tenant_id": tenant_id,
            "created_at": utcnow().isoformat(),
            "include": include,
            "rows": counts,
            "skipped": skipped,
            "entries": dict(sorted(archive.entries.items())),
        }
        return archive.finish(manifest), manifest

    async def _datasets(self, archive: _Archive, tenant_id: str) -> list[dict[str, Any]]:
        budget = self.ctx.settings.export_max_inline_bytes
        skipped: list[dict[str, Any]] = []
        async with self.ctx.db.tenant_session(tenant_id) as s:
            rows = (
                await s.execute(
                    select(m.Dataset.name, m.DatasetVersion.version, m.DatasetVersion.objects)
                    .join(m.Dataset, m.Dataset.id == m.DatasetVersion.dataset_id)
                    .where(
                        m.DatasetVersion.tenant_id == tenant_id,
                        m.DatasetVersion.status == "ready",
                    )
                    .order_by(m.Dataset.name, m.DatasetVersion.version)
                )
            ).all()
        for ds_name, version, objects in rows:
            for obj_name, ref in sorted((objects or {}).items()):
                if not isinstance(ref, dict) or "bucket" not in ref or "name" not in ref:
                    continue
                bucket, key = str(ref["bucket"]), str(ref["name"])
                if not key.startswith(f"t/{tenant_id}/"):  # defence in depth: own prefix only
                    continue
                generation = ref.get("generation")
                path = f"datasets/{_safe_part(ds_name)}/v{version}/{_safe_part(obj_name)}"
                try:
                    meta = await self.ctx.store.stat(
                        bucket, key, generation=int(generation) if generation else None
                    )
                except NotFound:
                    skipped.append({"path": path, "reason": "not found"})
                    continue
                if meta.size > budget:
                    skipped.append(
                        {"path": path, "reason": "export size limit", "size": meta.size,
                         "crc32c": meta.crc32c, "md5": meta.md5_hash}
                    )  # fmt: skip
                    continue
                budget -= meta.size
                async with archive.stream(path) as out:
                    start = 0
                    while start < meta.size:
                        end = min(start + READ_CHUNK, meta.size) - 1
                        out.write(
                            await self.ctx.store.get(
                                bucket, key, generation=meta.generation, byte_range=(start, end)
                            )
                        )
                        start = end + 1
        return skipped

    async def _models(self, archive: _Archive, tenant_id: str) -> None:
        async with self.ctx.db.tenant_session(tenant_id) as s:
            rows = (
                await s.execute(
                    select(m.Model.name, m.ModelVersion)
                    .join(m.Model, m.Model.id == m.ModelVersion.model_id)
                    .where(m.ModelVersion.tenant_id == tenant_id)
                    .order_by(m.Model.name, m.ModelVersion.version)
                )
            ).all()
        for model_name, mv in rows:
            base = f"models/{_safe_part(model_name)}/v{mv.version}"
            archive.write(
                f"{base}/manifest.json",
                json.dumps(mv.manifest, indent=2, default=_json_default).encode(),
            )
            info = {
                "id": mv.id, "model": model_name, "version": mv.version, "status": mv.status,
                "run_id": mv.run_id, "spec_hash": mv.spec_hash, "lineage": mv.lineage,
                "diagnostics": mv.diagnostics, "scorecard": mv.scorecard, "metrics": mv.metrics,
                "artifacts": mv.artifacts, "created_at": mv.created_at,
            }  # fmt: skip
            archive.write(
                f"{base}/model_version.json",
                json.dumps(info, indent=2, default=_json_default).encode(),
            )


async def expire_exports(ctx: ServerContext, *, at: datetime) -> int:
    """Delete archives past their retention (state ``expired``)."""
    async with ctx.db.session() as s:
        rows = (
            await s.execute(
                update(m.TenantExport)
                .where(m.TenantExport.state == "ready", m.TenantExport.expires_at <= at)
                .values(state="expired")
                .returning(m.TenantExport.bucket, m.TenantExport.object_name)
            )
        ).all()
    for bucket, name in rows:
        if not bucket or not name:
            continue
        try:
            await ctx.store.delete(bucket, name)
        except (NotFound, StorageError):
            log.warning("could not delete expired export %s/%s", bucket, name)
    return len(rows)
