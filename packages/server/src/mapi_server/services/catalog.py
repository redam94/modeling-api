"""The static catalog (steps, models, plots) the control plane validates against.

Catalog JSON is produced *outside* the control plane — by ``mapi-runner describe`` at image
build time (``MAPI_CATALOG_PATH``) or by a sandboxed ``describe`` run — and cached in the
``catalogs`` table per **image digest** (locally: ``sha256`` of ``uv.lock``). The server
only ever parses it as JSON with :class:`mapi_spec.catalog.Catalog`; it never imports step,
model or plot code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mapi_spec.catalog import Catalog
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.plots import PlotExamples, PlotSpec
from mapi_spec.run import catalog_digest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from mapi_server.db import models as m
from mapi_server.jsonlimits import loads_limited

__all__ = ["CatalogService", "CatalogSnapshot", "CatalogUnavailable", "parse_catalog"]

log = logging.getLogger(__name__)

MAX_CATALOG_BYTES = 16 * 1024 * 1024


class CatalogUnavailable(MapiError):
    """No catalog is cached for the current image digest yet (503, retryable)."""

    default_code = ErrorCode.INFRA_ERROR


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    image_digest: str
    digest: str
    catalog: Catalog
    generated_at: datetime | None
    source: str


def parse_catalog(raw: bytes | str | dict[str, Any]) -> Catalog:
    """Validate catalog JSON (size-capped); raises ``ValueError`` on any problem."""
    if isinstance(raw, bytes | str):
        if len(raw) > MAX_CATALOG_BYTES:
            raise ValueError(f"catalog JSON exceeds {MAX_CATALOG_BYTES} bytes")
        try:
            data = loads_limited(raw)
        except ValueError as exc:
            raise ValueError(f"catalog is not valid JSON: {exc}") from exc
    else:
        data = raw
    try:
        return Catalog.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"catalog does not match the Catalog schema: {exc}") from exc


class CatalogService:
    """Caches the catalog of one image digest (immutable once stored)."""

    def __init__(self, image_digest: str, *, catalog_path: Path | None = None) -> None:
        self.image_digest = image_digest
        self.catalog_path = catalog_path
        self._cached: CatalogSnapshot | None = None
        self._examples: PlotExamples | None = None

    def _snapshot(self, row: m.Catalog) -> CatalogSnapshot:
        cat = Catalog.model_validate(row.catalog)
        return CatalogSnapshot(
            row.image_digest, row.catalog_digest, cat, row.created_at, row.source
        )

    async def get(self, session: AsyncSession) -> CatalogSnapshot | None:
        if self._cached is not None:
            return self._cached
        row = await session.get(m.Catalog, self.image_digest)
        if row is not None:
            self._cached = self._snapshot(row)
        return self._cached

    async def require(self, session: AsyncSession) -> CatalogSnapshot:
        snap = await self.get(session)
        if snap is None:
            raise CatalogUnavailable(
                f"no catalog is available for image {self.image_digest} yet; a describe run "
                "must finish (or set MAPI_CATALOG_PATH) before specs can be validated"
            )
        return snap

    async def store(
        self,
        session: AsyncSession,
        catalog: Catalog,
        *,
        source: str,
        tenant_id: str,
        project_id: str,
        describe_run_id: str | None = None,
        image_digest: str | None = None,
        plot_examples: PlotExamples | None = None,
    ) -> CatalogSnapshot:
        """Insert the catalog for an image digest (first writer wins; it is immutable).

        ``plot_examples`` (from the same describe run) are stored with it, and only with
        it: a catalog that is already stored keeps the examples it has."""
        digest = catalog_digest(catalog)
        image = image_digest or self.image_digest
        stmt = (
            insert(m.Catalog)
            .values(
                image_digest=image,
                catalog_digest=digest,
                catalog=catalog.model_dump(mode="json"),
                source=source,
                describe_run_id=describe_run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                plot_examples=(
                    None
                    if plot_examples is None
                    else plot_examples.model_dump(mode="json", exclude_none=True)
                ),
            )
            .on_conflict_do_nothing(index_elements=["image_digest"])
        )
        await session.execute(stmt)
        row = (
            await session.execute(select(m.Catalog).where(m.Catalog.image_digest == image))
        ).scalar_one()
        snap = self._snapshot(row)
        if image == self.image_digest:
            self._cached = snap
        return snap

    async def load_file(
        self, session: AsyncSession, *, tenant_id: str, project_id: str
    ) -> CatalogSnapshot | None:
        """Import ``catalog_path`` if configured and nothing is cached yet."""
        existing = await self.get(session)
        if existing is not None or self.catalog_path is None:
            return existing
        catalog = parse_catalog(self.catalog_path.read_bytes())
        log.info("imported catalog from %s for image %s", self.catalog_path, self.image_digest)
        return await self.store(
            session, catalog, source="file", tenant_id=tenant_id, project_id=project_id
        )

    async def example(self, session: AsyncSession, ref: str) -> PlotSpec | None:
        """The example render of plot recipe ``ref`` (``id@version``) for this image digest;
        ``None`` when the catalog (or the recipe) has none."""
        if self._examples is None:
            row = (
                await session.execute(
                    select(m.Catalog)
                    .options(undefer(m.Catalog.plot_examples))
                    .where(m.Catalog.image_digest == self.image_digest)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            # validated by the finalizer; an unreadable value is treated as "no examples"
            try:
                self._examples = PlotExamples.model_validate(row.plot_examples or {})
            except ValidationError:
                log.warning("unreadable plot examples for image %s", self.image_digest)
                self._examples = PlotExamples()
        return self._examples.plots.get(ref)

    def invalidate(self) -> None:
        self._cached = None
        self._examples = None
