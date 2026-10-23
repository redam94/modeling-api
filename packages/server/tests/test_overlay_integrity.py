"""Uploaded code whose stored copy is not the one that was checked (v0.19 live findings F2,
F3; plan D17).

* **F2.** The pinned overlay object is gone at its generation (replaced through the storage
  API, which makes a new generation, or deleted). The dispatcher finds it before it mints
  any URL: the run fails ``FAILED(integrity)`` with ``INTEGRITY_FAILURE`` (not an infra
  outage, and never retried), the version is withdrawn with reason ``security`` and the
  uploader and every tenant admin are mailed (no uploaded text). The same holds when the
  supervisor finds it gone during the download: the reconciler looks at storage itself
  before it withdraws anything.
* **F3.** The supervisor refuses an overlay whose bytes do not hash to the pin (same
  generation, other bytes): the run's detail says so plainly, with failure class
  ``integrity``; the object still looks right from the control plane, so nothing is
  withdrawn on the supervisor's word alone.

The "wheels" are opaque bytes; nothing here opens or runs one.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from _v019_pipeline import NS, UntrustedFakeExecutor, pinned_fit, seed_version
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.dispatcher import Dispatcher
from mapi_server.services.plugins.review import INTEGRITY_YANK_NOTE
from mapi_server.services.reconciler import OVERLAY_GONE, OVERLAY_HASH_MISMATCH, Reconciler
from mapi_server.services.runs import load_run
from mapi_server_testkit.identity import grant_role, make_user
from mapi_server_testkit.trust import RecordingMailer
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ErrorCode
from mapi_spec.run import ErrorPayload, FailureClass, RunState
from sqlalchemy import select

pytestmark = pytest.mark.integration

TITLE = "Acme tools"  # the plugin title the seed stores: uploaded text, never mailed


@dataclass
class Kit:
    ctx: ServerContext
    mail: RecordingMailer
    executor: UntrustedFakeExecutor
    version_id: str
    run_id: str
    uploader: str
    admin: str

    async def run(self) -> m.Run:
        async with self.ctx.db.session() as s:
            row = await load_run(s, None, self.run_id)
            s.expunge(row)
            return row

    async def version(self) -> m.PluginVersion:
        async with self.ctx.db.session() as s:
            row = await s.get(m.PluginVersion, self.version_id)
            assert row is not None
            s.expunge(row)
            return row

    async def overlay(self) -> tuple[str, int]:
        v = await self.version()
        assert v.overlay_key is not None and v.overlay_generation is not None
        return v.overlay_key, int(v.overlay_generation)


@pytest.fixture
async def kit(ctx: ServerContext) -> Kit:
    """An approved upload (its checked copy in the plugins bucket), its uploader and a
    second tenant admin with e-mail addresses, and a queued fit pinned to it."""
    uploader = await make_user(ctx, "uploader@default.test")
    admin = await make_user(ctx, "admin@default.test")
    await grant_role(ctx, uploader, "default", "default", "editor")
    await grant_role(ctx, admin, "default", None, "admin")
    _, vid = await seed_version(ctx, state="approved", overlay=True, uploaded_by=uploader.id)
    run_id = await pinned_fit(ctx, vid, created_by=uploader.id)
    mail = RecordingMailer()
    assert ctx.trust is not None
    ctx.trust.mailer = mail
    return Kit(ctx, mail, UntrustedFakeExecutor(), vid, run_id, uploader.id, admin.id)


async def _assert_withdrawn(kit: Kit, actor: str) -> None:
    v = await kit.version()
    assert v.state == "yanked" and v.yank_reason == "security"
    assert v.yank_note == INTEGRITY_YANK_NOTE and v.yanked_by is None
    assert sorted(msg.to for msg in kit.mail.sent) == [
        "admin@default.test",
        "uploader@default.test",
    ]
    for msg in kit.mail.sent:
        assert TITLE not in msg.subject + msg.body
        assert "withdrawn" in msg.body and v.id in msg.body
    async with kit.ctx.db.session() as s:
        audit = (
            (
                await s.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == AuditAction.PLUGIN_YANK.value,
                        m.AuditEvent.resource_id == v.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audit) == 1
    assert audit[0].actor_id == actor
    assert audit[0].details["cause"] == "overlay_integrity"
    assert audit[0].details["run_id"] == kit.run_id


# ===================================================================== F2: at the dispatcher


@pytest.mark.parametrize("how", ["replaced", "deleted"])
async def test_a_replaced_or_missing_overlay_is_an_integrity_failure(kit: Kit, how: str) -> None:
    ctx = kit.ctx
    key, generation = await kit.overlay()
    # the storage API keeps no noncurrent generation (GCS without object versioning): the
    # pinned generation is gone; "replaced" leaves another generation live under the name
    await ctx.store.delete(ctx.buckets.plugins, key, generation=generation)
    if how == "replaced":
        await ctx.store.put(ctx.buckets.plugins, key, b"PK\x03\x04 another wheel")
    await Dispatcher(ctx, kit.executor).dispatch_once()
    run = await kit.run()
    assert run.state == RunState.FAILED.value
    assert run.failure_class == FailureClass.INTEGRITY.value
    assert run.problem is not None and run.problem["code"] == "INTEGRITY_FAILURE"
    assert "replaced or removed" in run.problem["detail"] and NS in run.problem["detail"]
    assert "executor did not accept" not in run.problem["detail"]
    assert run.infra_retries == 0 and run.attempt == 1  # never retried
    assert not kit.executor.executions  # nothing was submitted, no URL was minted
    await _assert_withdrawn(kit, "svc_dispatcher")


async def test_an_intact_overlay_still_dispatches(kit: Kit) -> None:
    """Regression: the check reads metadata only and passes the pinned generation."""
    await Dispatcher(kit.ctx, kit.executor).dispatch_once()
    run = await kit.run()
    assert run.state == RunState.PROVISIONING.value
    assert (await kit.version()).state == "approved"
    assert kit.mail.sent == []


# ============================================================ F2, F3: after the supervisor


async def _supervisor_refused(kit: Kit, message: str) -> m.Run:
    """Dispatch, then the supervisor exits 13 (INTEGRITY_ERROR) with ``message``."""
    ctx = kit.ctx
    await Dispatcher(ctx, kit.executor).dispatch_once()
    run = await kit.run()
    assert run.state == RunState.PROVISIONING.value
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (kit.run_id, 1))
        assert att is not None
        att.error_payload = ErrorPayload(
            code=ErrorCode.INTEGRITY_FAILURE,
            failure_class=FailureClass.INTEGRITY,
            message=message,
        ).model_dump(mode="json")
        nonce = att.nonce
    kit.executor.finish(nonce, 13)
    await Reconciler(ctx, kit.executor).reconcile_run(kit.run_id)
    return await kit.run()


async def test_a_tampered_overlay_says_so_plainly(kit: Kit) -> None:
    """F3: same generation, other bytes. The detail names the hash, not the CRC32C of an
    input; the failure class is integrity. Storage metadata still matches the pin, so the
    control plane does not withdraw the version on the supervisor's report."""
    run = await _supervisor_refused(
        kit, "the sha256 of the uploaded wheel does not match the pinned version"
    )
    assert run.state == RunState.FAILED.value
    assert run.failure_class == FailureClass.INTEGRITY.value
    assert run.problem is not None and run.problem["code"] == "INTEGRITY_FAILURE"
    assert run.problem["detail"] == OVERLAY_HASH_MISMATCH
    assert "did not match the hash" in run.problem["detail"]
    assert "CRC32C" not in run.problem["detail"]
    assert (await kit.version()).state == "approved"
    assert kit.mail.sent == []


async def test_an_overlay_gone_during_the_download_is_withdrawn(kit: Kit) -> None:
    """F2 when the URL was minted first (real Cloud Storage signs without a lookup): the
    supervisor reports the pinned generation gone; the reconciler checks storage itself and
    withdraws the version."""
    ctx = kit.ctx
    await Dispatcher(ctx, kit.executor).dispatch_once()
    key, generation = await kit.overlay()
    await ctx.store.delete(ctx.buckets.plugins, key, generation=generation)
    async with ctx.db.session() as s:
        att = await s.get(m.RunAttempt, (kit.run_id, 1))
        assert att is not None
        att.error_payload = ErrorPayload(
            code=ErrorCode.INTEGRITY_FAILURE,
            failure_class=FailureClass.INTEGRITY,
            message="the stored copy of the uploaded wheel is gone or was replaced (HTTP 404 "
            "for the pinned generation)",
        ).model_dump(mode="json")
        nonce = att.nonce
    kit.executor.finish(nonce, 13)
    await Reconciler(ctx, kit.executor).reconcile_run(kit.run_id)
    run = await kit.run()
    assert run.state == RunState.FAILED.value
    assert run.failure_class == FailureClass.INTEGRITY.value
    assert run.problem is not None and run.problem["detail"] == OVERLAY_GONE
    await _assert_withdrawn(kit, "svc_reconciler")


async def test_an_input_integrity_failure_keeps_its_text(kit: Kit) -> None:
    """Exit 13 for a dataset or bundle input (not an overlay): the CRC32C text, class
    integrity (the reconciler now has the live → fail_integrity edge)."""
    run = await _supervisor_refused(kit, "input 'dataset' failed its CRC32C check")
    assert run.failure_class == FailureClass.INTEGRITY.value
    assert run.problem is not None
    assert run.problem["detail"] == "an input object did not match its expected size or CRC32C"
    assert (await kit.version()).state == "approved"
