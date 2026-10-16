"""Builders for the trust-lifecycle tests: a recording mailer and a tenant full of data.

:func:`seed_tenant` writes, through the platform role, one of everything a tenant can own
(project, member, token, dataset with a stored version, a finished run with its attempt and
bootstrap object, a model version with a plot, idempotency key, audit and cost rows) plus
objects under ``t/{tenant}/`` in every bucket, so deletion and export tests can check both
that all of it goes and that another tenant's copy stays.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.audit import add_audit
from mapi_server.services.mail import MailMessage
from mapi_server.services.runs import now
from mapi_server.settings import MailerKind
from mapi_spec.api import new_id
from mapi_spec.audit import AuditAction, AuditOutcome, make_audit_event
from mapi_spec.storage import bundle_prefix, dataset_prefix, quarantine_key, run_prefix
from mapi_spec.tenancy import ResourceKind

from mapi_server_testkit.identity import grant_role, make_tenant, make_token, make_user


class RecordingMailer:
    kind = MailerKind.OUTBOX

    def __init__(self) -> None:
        self.sent: list[MailMessage] = []

    async def send(self, message: MailMessage) -> None:
        self.sent.append(message)

    def token_for(self, to: str) -> str:
        for msg in reversed(self.sent):
            if msg.to == to:
                return msg.tags["token"]
        raise AssertionError(f"no e-mail to {to}")


@dataclass
class Seeded:
    tenant_id: str
    user: m.User
    token: str
    dataset_id: str
    run_id: str
    model_id: str
    nonce: str
    objects: list[tuple[str, str]] = field(default_factory=list)
    data_bytes: bytes = b""


async def seed_tenant(
    ctx: ServerContext, tenant_id: str, *, tier: str = "public", email: str | None = None
) -> Seeded:
    await make_tenant(ctx, tenant_id, tier=tier, projects=("main",))
    user = await make_user(ctx, email or f"owner@{tenant_id}.test")
    await grant_role(ctx, user, tenant_id, None, "admin")
    token = await make_token(ctx, user, tenant_id, "main", ["admin"])
    ts = now()
    b = ctx.buckets
    sha = secrets.token_hex(32)
    data_bytes = secrets.token_bytes(3000)
    ds_id, run_id, model_id, mv_id = new_id("ds"), new_id("run"), new_id("mdl"), new_id("mv")
    nonce = secrets.token_hex(32)
    data_key = dataset_prefix(tenant_id, sha) + "data.zarr.zip"
    run_key = run_prefix(tenant_id, run_id, 1) + "bundle.zarr.zip"
    bundle_key = bundle_prefix(tenant_id, model_id, 1) + "manifest.json"
    upload_key = quarantine_key(tenant_id, new_id("upl"))
    data_meta = await ctx.store.put(b.data, data_key, data_bytes)
    objects = [(b.data, data_key)]
    for bucket, key in ((b.runs, run_key), (b.bundles, bundle_key), (b.quarantine, upload_key)):
        await ctx.store.put(bucket, key, b"{}")
        objects.append((bucket, key))
    await ctx.store.put(b.bootstrap, nonce, b"{}")
    async with ctx.db.session() as s:
        s.add(
            m.Dataset(
                id=ds_id, tenant_id=tenant_id, project_id="main", name="sales", labels={},
                next_version=2, latest_version=1, created_at=ts, updated_at=ts,
            )
        )  # fmt: skip
        await s.flush()
        s.add(
            m.DatasetVersion(
                dataset_id=ds_id, version=1, tenant_id=tenant_id, project_id="main",
                status="ready", sha256=sha, size_bytes=len(data_bytes),
                data_schema={"dims": {}}, objects={"data.zarr.zip": {
                    "bucket": b.data, "name": data_key, "generation": data_meta.generation}},
                issues=[], created_at=ts, ready_at=ts,
            )
        )  # fmt: skip
        s.add(
            m.Run(
                id=run_id, tenant_id=tenant_id, project_id="main", kind="fit",
                state="succeeded", attempt=1, resource_class="S", timeout_s=60, labels={},
                payload={"secret_stuff": "no"}, inputs={}, checkpointable=False,
                infra_retries=0, finalize_attempts=0, finalize={}, last_event_id=0,
                created_at=ts, updated_at=ts,
            )
        )  # fmt: skip
        await s.flush()
        s.add(
            m.RunAttempt(
                run_id=run_id, attempt=1, tenant_id=tenant_id, project_id="main", nonce=nonce,
                hmac_key="k" * 64, executor="fake", status="finished", deadline=ts,
                outputs={}, violations=0, last_seq=0, log_bytes=0,
            )
        )  # fmt: skip
        s.add(
            m.RunLog(
                run_id=run_id, attempt=1, seq=1, tenant_id=tenant_id, project_id="main",
                ts=ts, stream="stdout", line="hello", truncated=False,
            )
        )  # fmt: skip
        s.add(
            m.Model(
                id=model_id, tenant_id=tenant_id, project_id="main", name="mmm",
                latest_version=1, next_version=2, created_at=ts, updated_at=ts,
            )
        )  # fmt: skip
        await s.flush()
        s.add(
            m.ModelVersion(
                id=mv_id, tenant_id=tenant_id, project_id="main", model_id=model_id,
                version=1, status="ready", run_id=run_id, attempt=1,
                spec_hash="sha256:" + "0" * 64, manifest={"tenant": tenant_id},
                diagnostics=[], lineage={"run": run_id}, metrics={}, artifacts=[],
                created_at=ts,
            )
        )  # fmt: skip
        await s.flush()
        s.add(
            m.Plot(
                model_version_id=mv_id, plot_id="core.ppc", tenant_id=tenant_id,
                project_id="main", title="PPC", kind="vega-lite", persona="both",
                spec={"id": "core.ppc"}, created_at=ts,
            )
        )  # fmt: skip
        s.add(
            m.IdempotencyKey(
                tenant_id=tenant_id, project_id="main", key=f"idem-{tenant_id}",
                operation="create_run", request_hash="sha256:" + "1" * 64,
                expires_at=ts + timedelta(days=1),
            )
        )  # fmt: skip
        s.add(
            m.CostLedgerEntry(
                id=new_id("cle"), tenant_id=tenant_id, project_id="main", run_id=run_id,
                attempt=1, kind="actual", resource_class="S", wall_seconds=1.0,
                vcpu_seconds=2.0, gib_seconds=8.0, price_version="test", amount_micros=10,
                currency="USD", recorded_at=ts,
            )
        )  # fmt: skip
        await add_audit(
            s,
            [
                make_audit_event(
                    action=AuditAction.RUN_CREATE, resource_kind=ResourceKind.RUN,
                    outcome=AuditOutcome.SUCCESS, ts=ts, fingerprint_key=b"k" * 32,
                    tenant_id=tenant_id, project_id="main", resource_id=run_id,
                )
            ],
        )  # fmt: skip
    return Seeded(
        tenant_id=tenant_id, user=user, token=token, dataset_id=ds_id, run_id=run_id,
        model_id=model_id, nonce=nonce, objects=objects, data_bytes=data_bytes,
    )  # fmt: skip
