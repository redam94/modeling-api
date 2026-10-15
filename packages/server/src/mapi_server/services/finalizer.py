"""FINALIZING (plan §8.6): verify the runner's claim, copy, sign, register.

1. **Metadata only.** Stat + read the (small, size-capped) ``manifest.json``, check its HMAC
   with the attempt key and its identity (tenant, run, attempt, kind, prefix, spec hash,
   catalog digest, input generations), then list the attempt prefix and compare every
   claimed object's size/CRC32C/MD5 with the *server-computed* metadata
   (:func:`~mapi_spec.run.verify_output_objects`). Unexpected objects, mismatches or a bad
   manifest → ``FAILED(integrity)`` plus a security alert. No object content is decoded
   except small JSON sidecars, validated with Pydantic under size caps (plots are sanitized).
2. **Server-side copy** of verified objects (generation-pinned, write-once) into the bundles
   bucket (fit) or the content-addressed data bucket (ingest).
3. **Sign** a :class:`~mapi_spec.run.BundleManifest` bound to the destination generations
   and register the model version / dataset version / catalog in one transaction with the
   ``verified`` (or ``verified_partial``) transition.

Transient storage errors propagate so the job is retried; after
``finalize_max_attempts`` the run fails as ``infra``. Every step is idempotent.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from mapi_plots.sanitize import SanitizeError, sanitize_plot
from mapi_spec._types import MetricName
from mapi_spec.api import Lineage, ObjectRef, Scorecard, ScorecardItem, new_id
from mapi_spec.capabilities import Diagnostic
from mapi_spec.catalog import Catalog
from mapi_spec.coords import CoordSummary, DataSummary
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.executor import RunEventType, RunKind
from mapi_spec.jobs import (
    DESCRIBE_OUTPUT,
    FIT_BUNDLE,
    FIT_SIDECARS,
    INGEST_DATA,
    INGEST_SCHEMA,
    SIDE_TABLE_PREFIX,
    DescribeJob,
    MetricsSidecar,
)
from mapi_spec.loader import canonical_json, spec_hash
from mapi_spec.plots import PlotSpec
from mapi_spec.run import (
    MANIFEST_OBJECT_NAME,
    Actor,
    BundleManifest,
    BundleObject,
    CompletePayload,
    ErrorPayload,
    FailureClass,
    OutputManifest,
    RunState,
    RunTrigger,
    verify_output_manifest,
    verify_output_objects,
)
from mapi_spec.schema import DataSchema
from mapi_spec.storage import (
    NotFound,
    ObjectMeta,
    PreconditionFailed,
    bundle_prefix,
    dataset_prefix,
    run_prefix,
)
from mapi_spec.validate import ResolvedSpec, ScoreMetadata
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.jsonlimits import JsonTooDeep, loads_limited
from mapi_server.observability import metrics as obs_metrics
from mapi_server.observability.tracing import run_span
from mapi_server.services.runs import append_event, apply_transition, now
from mapi_server.services.runspec import EMPTY_CATALOG_DIGEST, MANIFEST_MAX_BYTES
from mapi_server.storage import list_all

__all__ = [
    "DESCRIBE_OUTPUT",
    "FIT_BUNDLE",
    "FIT_SIDECARS",
    "INGEST_DATA",
    "INGEST_SCHEMA",
    "Finalizer",
    "IntegrityError",
    "build_scorecard",
]

log = logging.getLogger(__name__)

# Output names the runner writes (relative to the attempt prefix) come from mapi_spec.jobs.

_DIAGNOSTICS = TypeAdapter(list[Diagnostic])
_METRIC_NAMES = TypeAdapter(dict[MetricName, float])
_COORDS = TypeAdapter(dict[str, dict[str, CoordSummary]])
_MAX_PLOTS = 200


class IntegrityError(MapiError):
    """The runner's claim does not match what storage holds (→ ``FAILED(integrity)``)."""

    default_code = ErrorCode.INTEGRITY_FAILURE


@dataclass
class _Verified:
    run: m.Run
    attempt: m.RunAttempt
    manifest: OutputManifest
    manifest_meta: ObjectMeta
    objects: dict[str, ObjectMeta]
    partial: bool
    warnings: list[str] = field(default_factory=list)


def build_scorecard(diagnostics: list[Diagnostic]) -> Scorecard | None:
    """Roll diagnostics up into the sanity scorecard (``info``→pass, ``warning``→warn,
    ``error``→fail; overall is the worst item)."""
    if not diagnostics:
        return None
    status_of = {"info": "pass", "warning": "warn", "error": "fail"}
    items = [
        ScorecardItem(
            code=d.code,
            status=status_of[d.severity],
            title=d.code.replace("_", " ").capitalize()[:200],
            message_plain=d.message_plain,
            message_expert=d.message_expert,
            metric=d.metric,
            value=d.value,
            threshold=d.threshold,
            suggested_action=d.suggested_action,
        )
        for d in diagnostics
    ]
    rank = {"pass": 0, "warn": 1, "fail": 2}
    overall = max((i.status for i in items), key=lambda s: rank[s])
    return Scorecard(overall=overall, items=items)


def dataset_sha(meta: ObjectMeta) -> str:
    """Content address of a dataset version: SHA-256 over the *server-computed* digests
    (size, CRC32C, MD5) of ``data.zarr.zip`` — no uploader-supplied hash is trusted."""
    basis = canonical_json({"size": meta.size, "crc32c": meta.crc32c, "md5": meta.md5_hash})
    return hashlib.sha256(basis.encode()).hexdigest()


class Finalizer:
    def __init__(self, ctx: ServerContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ entry point
    async def finalize(self, run_id: str, attempt: int) -> RunState | None:
        """Finalize one attempt. Returns the new state, or ``None`` if nothing to do."""
        ctx = self.ctx
        async with ctx.db.session() as session:
            run = await self._lock(session, run_id)
            if run.state != RunState.FINALIZING.value or run.attempt != attempt:
                return None
            run.finalize_attempts += 1
            if run.finalize_attempts > ctx.settings.finalize_max_attempts:
                await apply_transition(
                    session,
                    run,
                    RunTrigger.FAIL_INFRA,
                    Actor.FINALIZER,
                    problem=Problem.from_code(
                        ErrorCode.INFRA_ERROR, "finalization kept failing; giving up"
                    ),
                )
                obs_metrics.record_finalize_outcome(run.kind, "infra")
                return RunState.FAILED
            att = await session.get(m.RunAttempt, (run_id, attempt))
            assert att is not None
            session.expunge_all()
        with run_span("finalize", run_id=run_id, attempt=attempt, kind=run.kind):
            try:
                verified = await self._verify(run, att)
                state = await self._dispatch(verified)
            except IntegrityError as exc:
                obs_metrics.record_integrity_failure(run.tenant_id)
                obs_metrics.record_finalize_outcome(run.kind, "integrity")
                return await self._integrity_failure(run, exc)
            if state is RunState.SUCCEEDED:
                obs_metrics.record_finalize_outcome(run.kind, "succeeded")
            elif state is RunState.PARTIAL:
                obs_metrics.record_finalize_outcome(run.kind, "partial")
            return state

    async def _lock(self, session: AsyncSession, run_id: str) -> m.Run:
        return (
            await session.execute(select(m.Run).where(m.Run.id == run_id).with_for_update())
        ).scalar_one()

    # ------------------------------------------------------------------ verification
    async def _read_json(self, bucket: str, meta: ObjectMeta, cap: int) -> Any:
        if meta.size > cap:
            raise IntegrityError(f"{meta.name!r} exceeds the {cap}-byte sidecar limit")
        raw = await self.ctx.store.get(bucket, meta.name, generation=meta.generation, max_bytes=cap)
        name = meta.name.rsplit("/", 1)[-1]
        try:
            return loads_limited(raw)
        except JsonTooDeep as exc:
            raise IntegrityError(f"{name!r}: {exc}") from None
        except ValueError as exc:
            raise IntegrityError(f"{name!r} is not valid JSON") from exc

    async def _verify(self, run: m.Run, att: m.RunAttempt) -> _Verified:
        store, runs_bucket = self.ctx.store, self.ctx.buckets.runs
        prefix = run_prefix(run.tenant_id, run.id, att.attempt)
        try:
            manifest_meta = await store.stat(runs_bucket, prefix + MANIFEST_OBJECT_NAME)
        except NotFound as exc:
            raise IntegrityError("the run's manifest.json does not exist") from exc
        raw = await self._read_json(runs_bucket, manifest_meta, MANIFEST_MAX_BYTES)
        try:
            manifest = OutputManifest.model_validate(raw)
        except ValidationError as exc:
            raise IntegrityError(
                "manifest.json is not a valid output manifest", untrusted_text=str(exc)[:8000]
            ) from exc
        if not verify_output_manifest(manifest, att.hmac_key):
            raise IntegrityError("manifest.json HMAC verification failed")
        expected = {
            "tenant_id": run.tenant_id,
            "project_id": run.project_id,
            "run_id": run.id,
            "attempt": att.attempt,
            "kind": run.kind,
            "prefix": prefix,
            "spec_hash": run.spec_hash or spec_hash(run.payload),
            "catalog_digest": run.catalog_digest or EMPTY_CATALOG_DIGEST,
        }
        claimed = manifest.model_dump(mode="json")
        for key, value in expected.items():
            if claimed[key] != value:
                raise IntegrityError(f"manifest {key} does not match the run", pointer=f"/{key}")
        if att.complete_payload is None:
            raise IntegrityError("no verified 'complete' event was received")
        complete = CompletePayload.model_validate(att.complete_payload)
        if complete.manifest_crc32c != manifest_meta.crc32c:
            raise IntegrityError("manifest.json changed after the 'complete' event")
        if complete.status != manifest.status:
            raise IntegrityError("manifest status does not match the 'complete' event")
        for name, ref in run.inputs.items():
            digest = manifest.inputs.get(name)
            if digest is None or digest.generation != int(ref["generation"]):
                raise IntegrityError(
                    f"input {name!r} was not read at its pinned generation",
                    pointer=f"/inputs/{name}",
                )
        listing = await list_all(store, runs_bucket, prefix)
        result = verify_output_objects(manifest, listing, expected_prefix=prefix)
        if not result.ok:
            raise IntegrityError(
                f"{len(result.issues)} output object(s) failed verification: "
                f"{result.issues[0].message}"[:2000],
                issues=result.issues,
            )
        return _Verified(
            run=run,
            attempt=att,
            manifest=manifest,
            manifest_meta=manifest_meta,
            objects=result.verified,
            partial=manifest.status == "partial",
        )

    async def _integrity_failure(self, run: m.Run, exc: IntegrityError) -> RunState | None:
        log.error(
            "SECURITY ALERT: integrity failure finalizing run %s attempt %s: %s",
            run.id,
            run.attempt,
            exc.detail,
            extra={"security_alert": True, "run_id": run.id, "tenant_id": run.tenant_id},
        )
        issues = exc.issues or [exc.to_issue()]
        async with self.ctx.db.session() as session:
            row = await self._lock(session, run.id)
            if row.state != RunState.FINALIZING.value or row.attempt != run.attempt:
                return None
            await apply_transition(
                session,
                row,
                RunTrigger.FAIL_INTEGRITY,
                Actor.FINALIZER,
                problem=Problem.from_code(
                    ErrorCode.INTEGRITY_FAILURE,
                    exc.detail,
                    errors=issues[:100],
                    untrusted_text=exc.untrusted_text,
                ),
            )
            payload = ErrorPayload(
                code=ErrorCode.INTEGRITY_FAILURE,
                failure_class=FailureClass.INTEGRITY,
                message=exc.detail[:2000],
            )
            await append_event(
                session, row, type=RunEventType.ERROR, payload=payload.model_dump(mode="json")
            )
        return RunState.FAILED

    # ------------------------------------------------------------------ per kind
    async def _dispatch(self, v: _Verified) -> RunState | None:
        kind = RunKind(v.run.kind)
        if kind is RunKind.FIT:
            return await self._fit(v)
        if kind is RunKind.INGEST:
            return await self._ingest(v)
        if kind is RunKind.DESCRIBE:
            return await self._describe(v)
        return await self._outputs_only(v)

    async def _commit(
        self,
        v: _Verified,
        apply: Any = None,
    ) -> RunState | None:
        """Lock the run, run ``apply(session, run)`` and fire ``verified``/``verified_partial``."""
        async with self.ctx.db.session() as session:
            run = await self._lock(session, v.run.id)
            if run.state != RunState.FINALIZING.value or run.attempt != v.attempt.attempt:
                return None
            att = await session.get(m.RunAttempt, (run.id, v.attempt.attempt))
            assert att is not None
            att.outputs = {k: meta.model_dump(mode="json") for k, meta in v.objects.items()}
            if apply is not None:
                await apply(session, run)
            for text in v.warnings:
                await append_event(
                    session,
                    run,
                    type=RunEventType.WARNING,
                    payload={"code": "OUTPUT_DROPPED", "message": text[:2000]},
                )
            trigger = RunTrigger.VERIFIED_PARTIAL if v.partial else RunTrigger.VERIFIED
            rule = await apply_transition(session, run, trigger, Actor.FINALIZER)
            return rule.target

    async def _outputs_only(self, v: _Verified) -> RunState | None:
        return await self._commit(v)

    async def _copy(self, src: ObjectMeta, dst_bucket: str, dst_name: str) -> ObjectMeta:
        store = self.ctx.store
        try:
            return await store.copy(
                src.bucket,
                src.name,
                dst_bucket,
                dst_name,
                source_generation=src.generation,
                if_generation_match=0,
            )
        except PreconditionFailed:
            existing = await store.stat(dst_bucket, dst_name)
            if not existing.same_content(src):
                raise IntegrityError(
                    f"destination {dst_name!r} already holds different content"
                ) from None
            return existing

    async def _put_once(self, bucket: str, name: str, data: bytes) -> ObjectMeta:
        store = self.ctx.store
        try:
            return await store.put(
                bucket, name, data, content_type="application/json", if_generation_match=0
            )
        except PreconditionFailed:
            existing = await store.stat(bucket, name)
            body = await store.get(bucket, name, generation=existing.generation)
            if body != data:
                raise IntegrityError(f"{name!r} already exists with different content") from None
            return existing

    # .............................................................................. fit
    async def _sidecars(
        self, v: _Verified
    ) -> tuple[list[Diagnostic], dict[str, float], list[PlotSpec], dict[str, Any]]:
        cap = self.ctx.settings.max_sidecar_bytes
        diagnostics: list[Diagnostic] = []
        metrics: dict[str, float] = {}
        plots: list[PlotSpec] = []
        coords: dict[str, Any] = {}
        try:
            if (meta := v.objects.get("diagnostics.json")) is not None:
                diagnostics = _DIAGNOSTICS.validate_python(
                    await self._read_json(meta.bucket, meta, cap)
                )
            if (meta := v.objects.get("metrics.json")) is not None:
                report = MetricsSidecar.model_validate(
                    await self._read_json(meta.bucket, meta, cap)
                )
                metrics = _METRIC_NAMES.validate_python(report.finite())
            if (meta := v.objects.get("coords.json")) is not None:
                coords = _COORDS.validate_python(await self._read_json(meta.bucket, meta, cap))
        except ValidationError as exc:
            raise IntegrityError(
                "a JSON sidecar does not match its schema", untrusted_text=str(exc)[:8000]
            ) from exc
        if (meta := v.objects.get("plots.json")) is not None:
            raw = await self._read_json(meta.bucket, meta, cap)
            if not isinstance(raw, list):
                raise IntegrityError("plots.json must be a JSON array of plot specs")
            for i, doc in enumerate(raw[:_MAX_PLOTS]):
                try:
                    plots.append(sanitize_plot(doc).plot)
                except SanitizeError as exc:
                    v.warnings.append(f"plot #{i} was dropped by the plot sanitizer: {exc.detail}")
            if len(raw) > _MAX_PLOTS:
                v.warnings.append(f"only the first {_MAX_PLOTS} plots were kept")
        return diagnostics, metrics, plots, coords

    async def _reserve_model(self, run: m.Run) -> tuple[str, int, str]:
        """Allocate (model_id, version, model_version_id) once per run (idempotent)."""
        async with self.ctx.db.session() as session:
            row = await self._lock(session, run.id)
            res = row.finalize.get("model")
            if res:
                return str(res["model_id"]), int(res["version"]), str(res["model_version_id"])
            name = str(row.payload.get("metadata", {}).get("name") or f"run-{row.id}")
            model = (
                await session.execute(
                    select(m.Model)
                    .where(
                        m.Model.tenant_id == row.tenant_id,
                        m.Model.project_id == row.project_id,
                        m.Model.name == name,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if model is None:
                model = m.Model(
                    id=new_id("mdl"),
                    tenant_id=row.tenant_id,
                    project_id=row.project_id,
                    name=name,
                    next_version=1,
                )
                session.add(model)
                await session.flush()
            version = model.next_version
            model.next_version += 1
            mv_id = new_id("mv")
            row.finalize = {
                **row.finalize,
                "model": {"model_id": model.id, "version": version, "model_version_id": mv_id},
            }
            return model.id, version, mv_id

    async def _fit(self, v: _Verified) -> RunState | None:
        run = v.run
        if FIT_BUNDLE not in v.objects:
            if not v.partial:
                raise IntegrityError(f"a complete fit must upload {FIT_BUNDLE}")
            return await self._commit(v)  # partial without a bundle: checkpoints only
        diagnostics, metrics, plots, coords = await self._sidecars(v)
        model_id, version, mv_id = await self._reserve_model(run)
        dst_prefix = bundle_prefix(run.tenant_id, model_id, version)
        bundles = self.ctx.buckets.bundles
        objects: dict[str, BundleObject] = {}
        for name, meta in sorted(v.objects.items()):
            copied = await self._copy(meta, bundles, dst_prefix + name)
            objects[name] = BundleObject(
                bucket=bundles,
                key=copied.name,
                generation=copied.generation,
                size=copied.size,
                crc32c=copied.crc32c,
                md5=_md5(copied, meta),
                content_type=copied.content_type,
            )
        data_hash = await self._data_hash(run)
        manifest = BundleManifest(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            model_id=model_id,
            model_version=version,
            run_id=run.id,
            attempt=v.attempt.attempt,
            kind=RunKind.FIT,
            status=v.manifest.status,
            spec_hash=v.manifest.spec_hash,
            data_hash=data_hash,
            catalog_digest=v.manifest.catalog_digest,
            base_image=v.manifest.base_image,
            overlay_hash=v.manifest.overlay_hash,
            objects=objects,
            source_manifest_crc32c=v.manifest_meta.crc32c,
            signed_at=now(),
        )
        signed = self.ctx.signer.sign(manifest)
        assert signed.signature is not None
        unsigned_json = canonical_json(signed.model_copy(update={"signature": None}))
        await self._put_once(bundles, dst_prefix + "manifest.json", unsigned_json.encode())
        await self._put_once(
            bundles, dst_prefix + "manifest.sig", signed.signature.model_dump_json().encode()
        )

        async def apply(session: AsyncSession, row: m.Run) -> None:
            await self._register_model_version(
                session,
                row,
                v,
                signed,
                mv_id,
                model_id,
                version,
                diagnostics,
                metrics,
                plots,
                coords,
            )

        return await self._commit(v, apply)

    async def _side_table_shas(self, session: AsyncSession, run: m.Run) -> dict[str, str]:
        """``{name: sha256}`` of the side-table versions the run read (lineage, plan §8.6)."""
        out: dict[str, str] = {}
        for key, ref in sorted((run.inputs or {}).items()):
            if not key.startswith(SIDE_TABLE_PREFIX) or not isinstance(ref, dict):
                continue
            stored = m.DatasetVersion.objects[INGEST_DATA]
            stmt = select(m.DatasetVersion).where(
                m.DatasetVersion.tenant_id == run.tenant_id,
                stored["bucket"].astext == str(ref.get("bucket")),
                stored["name"].astext == str(ref.get("name")),
            )
            dv = (await session.execute(stmt)).scalars().first()
            if dv is not None and dv.sha256:
                out[key.removeprefix(SIDE_TABLE_PREFIX)] = dv.sha256
        return out

    async def _data_hash(self, run: m.Run) -> str | None:
        if run.dataset_id is None or run.dataset_version is None:
            return None
        async with self.ctx.db.session() as session:
            dv = await session.get(m.DatasetVersion, (run.dataset_id, run.dataset_version))
            return dv.sha256 if dv is not None else None

    async def _register_model_version(
        self,
        session: AsyncSession,
        run: m.Run,
        v: _Verified,
        signed: BundleManifest,
        mv_id: str,
        model_id: str,
        version: int,
        diagnostics: list[Diagnostic],
        metrics: dict[str, float],
        plots: list[PlotSpec],
        coords: dict[str, Any],
    ) -> None:
        existing = (
            await session.execute(select(m.ModelVersion).where(m.ModelVersion.run_id == run.id))
        ).scalar_one_or_none()
        if existing is not None:
            run.output_model_version_id = existing.id
            return
        resolved: ResolvedSpec | None = None
        if run.spec_id is not None and run.spec_revision is not None:
            rev = await session.get(m.SpecRevision, (run.spec_id, run.spec_revision))
            if rev is not None and rev.resolved:
                resolved = ResolvedSpec.model_validate(rev.resolved)
        fit = (run.payload.get("model") or {}).get("fit") or {}
        lineage = Lineage(
            run_id=run.id,
            attempt=v.attempt.attempt,
            spec_hash=signed.spec_hash,
            dataset_sha=signed.data_hash,
            plugin=resolved.model.resolved if resolved else None,
            base_image=signed.base_image,
            overlay_hash=signed.overlay_hash,
            catalog_digest=signed.catalog_digest,
            seed=fit.get("seed") if isinstance(fit.get("seed"), int) else None,
            engine=fit.get("engine") if isinstance(fit.get("engine"), str) else None,
            executor=run.executor,
            resource_class=run.resource_class,
            lib_versions=v.manifest.lib_versions,
            side_tables=await self._side_table_shas(session, run),
        )
        artifacts = [
            ObjectRef(name=name, size=o.size, content_type=o.content_type, crc32c=o.crc32c)
            for name, o in sorted(signed.objects.items())
        ]
        score_meta = (
            ScoreMetadata(resolved=resolved, fit_coords=coords).model_dump(mode="json")
            if resolved is not None
            else None
        )
        scorecard = build_scorecard(diagnostics)
        session.add(
            m.ModelVersion(
                id=mv_id,
                model_id=model_id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                version=version,
                status="partial" if v.partial else "ready",
                run_id=run.id,
                attempt=v.attempt.attempt,
                spec_hash=signed.spec_hash,
                manifest=signed.model_dump(mode="json"),
                capabilities=resolved.model.capabilities.model_dump(mode="json")
                if resolved
                else None,
                diagnostics=[d.model_dump(mode="json") for d in diagnostics],
                scorecard=scorecard.model_dump(mode="json") if scorecard else None,
                lineage=lineage.model_dump(mode="json"),
                metrics=dict(metrics),
                artifacts=[a.model_dump(mode="json", exclude_none=True) for a in artifacts],
                score_meta=score_meta,
            )
        )
        await session.flush()
        for plot in plots:
            session.add(
                m.Plot(
                    model_version_id=mv_id,
                    plot_id=plot.id,
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                    title=plot.title,
                    kind=plot.kind,
                    persona=plot.persona,
                    recipe=plot.provenance.recipe if plot.provenance else None,
                    caption_plain=plot.caption_plain,
                    spec=plot.model_dump(mode="json", by_alias=True),
                )
            )
        model = await session.get(m.Model, model_id, with_for_update=True)
        assert model is not None
        model.latest_version = max(model.latest_version or 0, version)
        model.updated_at = now()
        run.output_model_version_id = mv_id

    # ........................................................................... ingest
    async def _ingest(self, v: _Verified) -> RunState | None:
        run = v.run
        data = v.objects.get(INGEST_DATA)
        schema_meta = v.objects.get(INGEST_SCHEMA)
        if data is None or schema_meta is None:
            raise IntegrityError(f"an ingest run must upload {INGEST_DATA} and {INGEST_SCHEMA}")
        if data.md5_hash is None:
            raise IntegrityError(f"{INGEST_DATA} has no server-computed MD5")
        cap = self.ctx.settings.max_sidecar_bytes
        try:
            schema = DataSchema.model_validate(
                await self._read_json(run_bucket(v), schema_meta, cap)
            )
            summary = (
                DataSummary.model_validate(
                    await self._read_json(run_bucket(v), v.objects["summary.json"], cap)
                )
                if "summary.json" in v.objects
                else None
            )
        except ValidationError as exc:
            raise IntegrityError(
                "an ingest sidecar does not match its schema", untrusted_text=str(exc)[:8000]
            ) from exc
        sha_hex = dataset_sha(data)
        prefix = dataset_prefix(run.tenant_id, sha_hex)
        data_bucket = self.ctx.buckets.data
        stored: dict[str, Any] = {}
        for name, meta in sorted(v.objects.items()):
            copied = await self._copy(meta, data_bucket, prefix + name)
            stored[name] = {
                "bucket": data_bucket,
                "name": copied.name,
                "generation": copied.generation,
                "size": copied.size,
                "crc32c": copied.crc32c,
                "md5": copied.md5_hash,
                "content_type": copied.content_type,
            }

        def ref(name: str) -> dict[str, Any] | None:
            o = stored.get(name)
            if o is None:
                return None
            return ObjectRef(
                name=name, size=o["size"], content_type=o["content_type"], crc32c=o["crc32c"]
            ).model_dump(mode="json", exclude_none=True)

        async def apply(session: AsyncSession, row: m.Run) -> None:
            if row.dataset_id is None or row.dataset_version is None:
                raise IntegrityError("the ingest run is not linked to a dataset version")
            dv = await session.get(
                m.DatasetVersion, (row.dataset_id, row.dataset_version), with_for_update=True
            )
            ds = await session.get(m.Dataset, row.dataset_id, with_for_update=True)
            if dv is None or ds is None:
                raise IntegrityError("the dataset version of this ingest run is gone")
            dv.status = "ready"
            dv.sha256 = f"sha256:{sha_hex}"
            dv.size_bytes = data.size
            dv.data_schema = schema.model_dump(mode="json")
            dv.summary = summary.model_dump(mode="json") if summary else None
            dv.profile = ref("profile.json")
            dv.preview = ref("preview.json")
            dv.objects = stored
            dv.ready_at = now()
            ds.latest_version = max(ds.latest_version or 0, dv.version)
            ds.updated_at = now()

        return await self._commit(v, apply)

    # ......................................................................... describe
    async def _describe(self, v: _Verified) -> RunState | None:
        meta = v.objects.get(DESCRIBE_OUTPUT)
        if meta is None:
            raise IntegrityError(f"a describe run must upload {DESCRIBE_OUTPUT}")
        raw = await self._read_json(meta.bucket, meta, 16 * 1024 * 1024)
        try:
            catalog = Catalog.model_validate(raw)
        except ValidationError as exc:
            raise IntegrityError(
                "catalog.json does not match the Catalog schema", untrusted_text=str(exc)[:8000]
            ) from exc
        try:
            job = DescribeJob.model_validate(v.run.payload)
        except ValidationError:
            job = DescribeJob()
        image = job.image_digest or self.ctx.catalogs.image_digest

        async def apply(session: AsyncSession, row: m.Run) -> None:
            await self.ctx.catalogs.store(
                session,
                catalog,
                source="describe",
                tenant_id=row.tenant_id,
                project_id=row.project_id,
                describe_run_id=row.id,
                image_digest=str(image),
            )

        return await self._commit(v, apply)


def _md5(copied: ObjectMeta, source: ObjectMeta) -> str:
    md5 = copied.md5_hash or source.md5_hash
    if md5 is None:  # verify_output_objects only passes objects with an MD5
        raise IntegrityError(f"{copied.name!r} has no server-computed MD5")
    return md5


def run_bucket(v: _Verified) -> str:
    return v.manifest_meta.bucket
