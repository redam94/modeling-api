"""Builders for the v0.19 pipeline tests (WP6): plugin rows, wheel bytes in the stores,
executors that may run uploaded code, and simulated check-run outputs.

The "wheels" here are opaque bytes: the control plane never opens an upload, and nothing in
these tests imports, extracts or parses one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.finalizer import Finalizer
from mapi_server.services.runs import apply_transition, load_run
from mapi_server.services.runspec import EMPTY_CATALOG_DIGEST
from mapi_server.storage import MemoryObjectStore
from mapi_server_testkit.builders import CATALOG, submit_fit, trend_spec
from mapi_server_testkit.fake_executor import FakeExecutor
from mapi_server_testkit.fake_runner import SAFE_PLOT, UNSAFE_PLOT
from mapi_spec import Catalog
from mapi_spec.about import About
from mapi_spec.api import new_id
from mapi_spec.executor import ExecutorCaps, RunKind
from mapi_spec.resources import ResourceClass
from mapi_spec.run import (
    Actor,
    CompletePayload,
    InputDigest,
    ManifestObject,
    OutputManifest,
    RunState,
    RunTrigger,
    sign_output_manifest,
)
from mapi_spec.storage import (
    crc32c,
    crc32c_to_base64,
    md5_base64,
    plugin_quarantine_key,
    run_prefix,
)
from sqlalchemy import select

CODE = "abcdef"
NS = f"t_{CODE}_acme"
UPLOADER = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
TEAMMATE = "usr_01jaf3a9b8c7d6e5f4g3h2j1k9"
WHEEL = b"PK\x03\x04 not really a wheel: the control plane never opens it " * 64
WHEEL_SHA = "sha256:" + hashlib.sha256(WHEEL).hexdigest()
ABOUT: dict[str, Any] = About.model_json_schema()["examples"][0]


@dataclass
class UntrustedFakeExecutor(FakeExecutor):
    """A fake executor that may run uploaded code (``dev_only``: only through the
    development allowance, like local-process with ``MAPI_DEV_ALLOW_LOCAL_UNTRUSTED``)."""

    runs_untrusted: bool = True
    dev_only: bool = False
    egress: bool = False

    def caps(self) -> ExecutorCaps:
        return ExecutorCaps(
            isolation=self.isolation,
            egress_enforced=self.egress,
            dns_enforced=self.egress,
            classes=[ResourceClass.S, ResourceClass.M, ResourceClass.L],
            runs_untrusted_code=self.runs_untrusted,
            untrusted_dev_only=self.dev_only,
        )


async def seed_version(
    ctx: ServerContext,
    *,
    state: str = "uploaded",
    wheel: bytes = WHEEL,
    uploaded_by: str = UPLOADER,
    version: str = "0.1.0",
    overlay: bool = False,
    tenant_id: str = "default",
    project_id: str = "default",
    code: str = CODE,
    requested: bool = False,
    short: str = "acme",
) -> tuple[str, str]:
    """A plugin and one version in ``state`` with its wheel in quarantine (and in the
    plugins bucket when ``overlay``). Returns ``(plugin_id, version_id)``."""
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    ns = f"t_{code}_{short}"
    async with ctx.db.session() as s:
        settings_row = await s.get(m.TenantPluginSettings, tenant_id)
        if settings_row is None:
            s.add(m.TenantPluginSettings(tenant_id=tenant_id, tenant_code=code))
        plugin = (
            await s.execute(select(m.Plugin).where(m.Plugin.namespace == ns))
        ).scalar_one_or_none()
        if plugin is None:
            plugin = m.Plugin(
                id=new_id("plg"),
                tenant_id=tenant_id,
                namespace=ns,
                short_name=short,
                title="Acme tools",
                created_by=uploaded_by,
            )
            s.add(plugin)
            await s.flush()
        version_id = new_id("plv")
        qkey = plugin_quarantine_key(tenant_id, new_id("pup"))
        meta = await store.put(ctx.buckets.quarantine, qkey, wheel)
        row = m.PluginVersion(
            id=version_id,
            tenant_id=tenant_id,
            plugin_id=plugin.id,
            version=version,
            state=state,
            scope="tenant" if state == "approved" else "none",
            project_id=project_id,
            uploaded_by=uploaded_by,
            upload_sha256="sha256:" + hashlib.sha256(wheel).hexdigest(),
            size_bytes=len(wheel),
            quarantine_key=qkey,
            quarantine_generation=meta.generation,
            requested_by=uploaded_by if requested else None,
            requested_at=datetime.now(UTC) if requested else None,
        )
        if state in ("yanked",):
            row.yanked_at = datetime.now(UTC)
            row.yank_reason = "withdrawn"
        if overlay:
            okey = f"t/{tenant_id}/plugins/{plugin.id}/{version_id}.whl"
            ometa = await store.put(ctx.buckets.plugins, okey, wheel)
            row.overlay_key = okey
            row.overlay_generation = ometa.generation
        s.add(row)
        return plugin.id, version_id


async def queued_fit_once(ctx: ServerContext) -> str:
    """A queued fit; the dataset and the spec are stored on the first call per database."""
    async with ctx.db.session() as s:
        spec_id = (
            await s.execute(select(m.Spec.id).where(m.Spec.name == "trend").limit(1))
        ).scalar_one_or_none()
    if spec_id is None:
        spec_id = await trend_spec(ctx)
    return await submit_fit(ctx, spec_id)


async def pinned_fit(
    ctx: ServerContext, version_id: str, *, created_by: str = UPLOADER, sha: str = WHEEL_SHA
) -> str:
    """A queued fit whose pins name ``version_id`` (as admission would have stored them)."""
    from sqlalchemy import update

    run_id = await queued_fit_once(ctx)
    async with ctx.db.session() as s:
        await s.execute(
            update(m.Run)
            .where(m.Run.id == run_id)
            .values(
                overlays=[{"plugin_version_id": version_id, "namespace": NS, "sha256": sha}],
                untrusted_code=True,
                created_by=created_by,
            )
        )
    return run_id


async def load_version(ctx: ServerContext, version_id: str) -> m.PluginVersion:
    async with ctx.db.session() as s:
        row = await s.get(m.PluginVersion, version_id)
        assert row is not None
        s.expunge(row)
        return row


async def runs_of(ctx: ServerContext, kind: RunKind) -> list[m.Run]:
    async with ctx.db.session() as s:
        rows = list(
            (
                await s.execute(
                    select(m.Run).where(m.Run.kind == kind.value).order_by(m.Run.created_at)
                )
            ).scalars()
        )
        s.expunge_all()
        return rows


async def to_finalizing(
    ctx: ServerContext, run_id: str, executor: FakeExecutor | None = None
) -> tuple[m.Run, m.RunAttempt]:
    """Dispatch (``executor`` defaults to one that may run uploaded code), then move the run
    to FINALIZING like the events service and the reconciler would."""
    await Dispatcher(ctx, executor or UntrustedFakeExecutor()).dispatch_once()
    async with ctx.db.session() as s:
        run = await load_run(s, None, run_id, for_update=True)
        assert run.state == RunState.PROVISIONING.value, run.state
        await apply_transition(s, run, RunTrigger.EVENT, Actor.EVENTS)
        await apply_transition(s, run, RunTrigger.COMPLETE, Actor.RECONCILER)
        att = await s.get(m.RunAttempt, (run_id, run.attempt))
        assert att is not None
        s.expunge_all()
    return run, att


async def write_outputs(
    ctx: ServerContext,
    run: m.Run,
    attempt: m.RunAttempt,
    outputs: dict[str, bytes] | dict[str, tuple[bytes, str]],
    *,
    overlay_hashes: list[str] | None = None,
) -> None:
    """Write ``outputs`` and a signed manifest (with ``overlay_hashes``, default: the run's
    pins) and record the ``complete`` payload, like the supervisor would."""
    store = ctx.store
    assert isinstance(store, MemoryObjectStore)
    prefix = run_prefix(run.tenant_id, run.id, attempt.attempt)
    objects: dict[str, ManifestObject] = {}
    for name, value in outputs.items():
        data, ctype = value if isinstance(value, tuple) else (value, "application/json")
        await store.put(ctx.buckets.runs, prefix + name, data, content_type=ctype)
        objects[name] = ManifestObject(
            key=prefix + name,
            size=len(data),
            crc32c=crc32c_to_base64(crc32c(data)),
            md5=md5_base64(data),
            content_type=ctype,
        )
    if overlay_hashes is None:
        overlay_hashes = [p["sha256"] for p in run.overlays or []]
    manifest = OutputManifest(
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.id,
        attempt=attempt.attempt,
        kind=RunKind(run.kind),
        status="complete",
        prefix=prefix,
        spec_hash=run.spec_hash or "sha256:" + "0" * 64,
        catalog_digest=run.catalog_digest or EMPTY_CATALOG_DIGEST,
        inputs={
            n: InputDigest(generation=int(r["generation"]), size=1, crc32c="AAAAAA==")
            for n, r in run.inputs.items()
        },
        objects=objects,
        overlay_hashes=overlay_hashes,
        runner_version="fake-0",
        created_at=datetime.now(UTC),
    )
    signed = sign_output_manifest(manifest, attempt.hmac_key)
    body = signed.model_dump_json().encode()
    await store.put(
        ctx.buckets.runs, prefix + "manifest.json", body, content_type="application/json"
    )
    complete = CompletePayload(
        status="complete",
        manifest_name=prefix + "manifest.json",
        manifest_crc32c=crc32c_to_base64(crc32c(body)),
        objects=len(objects),
        total_bytes=sum(o.size for o in objects.values()),
    )
    async with ctx.db.session() as s:
        row = await s.get(m.RunAttempt, (run.id, attempt.attempt))
        assert row is not None
        row.complete_payload = complete.model_dump(mode="json")


async def finalize(ctx: ServerContext, run: m.Run, attempt: m.RunAttempt) -> RunState | None:
    return await Finalizer(ctx).finalize(run.id, attempt.attempt)


def inspection(*, sha: str = WHEEL_SHA, problems: list[dict[str, Any]] | None = None,
               dist: str = "t-abcdef-acme", version: str = "0.1.0",
               top_level: list[str] | None = None) -> bytes:  # fmt: skip
    """An ``inspection.json`` (ok unless ``problems``)."""
    doc: dict[str, Any] = {
        "ok": not problems,
        "archive_sha256": sha,
        "dist": dist,
        "version": version,
        "wheel_tags": ["py3-none-any"],
        "top_level": top_level if top_level is not None else [NS],
        "members": 4,
        "uncompressed_bytes": 2048,
        "entry_points": [{"group": "mapi.steps", "name": "acme", "value": f"{NS}.steps:STEPS"}],
        "sbom": {
            "dist": dist,
            "version": version,
            "files": [{"path": f"{NS}/__init__.py", "sha256": "sha256:" + "1" * 64, "size": 10}],
            "requires": [],
            "base_image_digest": "sha256:" + "ab" * 32,
        },
        "problems": problems or [],
    }
    return json.dumps(doc).encode()


def _catalog() -> Catalog:
    return Catalog.model_validate_json(CATALOG.read_text())


def describe(
    version_id: str,
    *,
    sha: str = WHEEL_SHA,
    kit_ok: bool = True,
    title: str = "Winsorize",
    examples: dict[str, Any] | None = None,
    step_name: str = "winsorize",
) -> bytes:
    """A ``describe.json`` exporting one step and one plot recipe of :data:`NS`."""
    step = _catalog().steps[0].model_dump(mode="json")
    step["id"] = f"{NS}.{step_name}"
    step["about"] = {**ABOUT, "title": title}
    step.pop("provider", None)
    plot = {
        "id": f"{NS}.weekly_total",
        "title": "Weekly total",
        "about": {**ABOUT, "title": "Weekly total"},
        "settings_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
    }
    checks: list[dict[str, Any]] = [
        {"entry_id": f"{NS}.{step_name}", "kind": "step", "check": "check_step",
         "status": "pass" if kit_ok else "fail", "message": "" if kit_ok else "bad output"},
    ]  # fmt: skip
    doc = {
        "plugin_version_id": version_id,
        "overlay_sha256": sha,
        "base_image_digest": "sha256:" + "ee" * 32,
        "exports": {"steps": [step], "plots": [plot]},
        "plot_examples": examples
        if examples is not None
        else {f"{NS}.weekly_total": {**SAFE_PLOT, "id": f"{NS}.weekly_total"}},
        "kit": {"ok": kit_ok, "checks": checks},
    }
    return json.dumps(doc).encode()


__all__ = [
    "ABOUT",
    "CODE",
    "NS",
    "TEAMMATE",
    "UNSAFE_PLOT",
    "UPLOADER",
    "WHEEL",
    "WHEEL_SHA",
    "UntrustedFakeExecutor",
    "describe",
    "finalize",
    "inspection",
    "load_version",
    "pinned_fit",
    "queued_fit_once",
    "runs_of",
    "seed_version",
    "to_finalizing",
    "write_outputs",
]
