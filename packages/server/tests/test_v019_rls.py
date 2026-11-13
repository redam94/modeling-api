"""v0.19 tables (0022_plugins): row-level security and least-privilege grants on plugin
settings, uploader attestations, plugins, uploads, versions, describes and approvals; the
checks and keys that back the contracts (tenant code, namespace, state and scope, sizes, the
second-person rule, sha-pinned approvals); and ``runs.overlays`` (pins imply untrusted code,
found by a GIN index)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, get_args

import psycopg
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server_testkit.identity import make_tenant
from mapi_spec import plugins as spec
from mapi_spec.api import new_id
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError

pytestmark = pytest.mark.integration

V019_TABLES: tuple[type[m.Base], ...] = (
    m.TenantPluginSettings,
    m.UserVerification,
    m.Plugin,
    m.PluginUpload,
    m.PluginVersion,
    m.PluginDescribe,
    m.PluginApproval,
)
TABLE_NAMES = tuple(t.__tablename__ for t in V019_TABLES)
CODES = {"alpha": "k3vq7m", "beta": "b2c4d5", "gamma": "zzzzzz"}
HASH = "sha256:" + "0" * 64
OTHER_HASH = "sha256:" + "1" * 64
BASE = "sha256:" + "a" * 64
EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)


def _rows(tenant: str) -> dict[str, Any]:
    """One row of every v0.19 table for ``tenant``: a plugin ``acme`` whose version 1.0.0 is
    awaiting approval after one rejection."""
    code = CODES[tenant]
    plugin, version = new_id("plg"), new_id("plv")
    return {
        "settings": m.TenantPluginSettings(
            tenant_id=tenant, tenant_code=code, updated_by="usr_admin"
        ),
        "verification": m.UserVerification(
            tenant_id=tenant, user_id="usr_ed", method="admin_attested", verified_by="usr_admin"
        ),
        "plugin": m.Plugin(
            id=plugin, tenant_id=tenant, namespace=f"t_{code}_acme", short_name="acme",
            title="Acme models", created_by="usr_ana",
        ),
        "upload": m.PluginUpload(
            id=new_id("pup"), tenant_id=tenant, plugin_id=plugin, project_id="main",
            created_by="usr_ana", filename=f"t_{code}_acme-1.0.0-py3-none-any.whl",
            declared_size=4096, object_key=f"t/{tenant}/plugins/x.whl", state="completed",
            expires_at=EXPIRES,
        ),
        "version": m.PluginVersion(
            id=version, tenant_id=tenant, plugin_id=plugin, version="1.0.0",
            state="awaiting_approval", scope="private", project_id="main", uploaded_by="usr_ana",
            upload_sha256=HASH, size_bytes=4096, quarantine_key=f"t/{tenant}/plugins/x.whl",
            quarantine_generation=1, overlay_key=f"t/{tenant}/plugins/{plugin}/{version}.whl",
            overlay_generation=2, inspection={"ok": True}, sbom={"format": "mapi.sbom/v1"},
        ),
        "describe": m.PluginDescribe(
            version_id=version, base_image_digest=BASE, tenant_id=tenant, run_id="run_d1",
            describe_digest=HASH, exports={"models": []}, kit={"ok": True, "checks": []},
            ok=True,
        ),
        "approval": m.PluginApproval(
            id=new_id("pap"), tenant_id=tenant, version_id=version, overlay_sha256=HASH,
            describe_digest=HASH, base_image_digest=BASE, decision="reject",
            approver_id="usr_admin", uploader_id="usr_ana", note="Add a summary.",
        ),
    }  # fmt: skip


ORDER = ("settings", "verification", "plugin", "upload", "version", "describe", "approval")


async def _seed(ctx: ServerContext) -> dict[str, dict[str, Any]]:
    """Both tenants' rows; returns each tenant's ids (``plugin``, ``version``, ``approval``)."""
    ids: dict[str, dict[str, Any]] = {}
    for tenant in ("alpha", "beta"):
        await make_tenant(ctx, tenant, tier="internal", projects=("main",))
        rows = _rows(tenant)
        async with ctx.db.tenant_session(tenant) as s:
            for key in ORDER:
                s.add(rows[key])
                await s.flush()  # parents before children
        ids[tenant] = {
            "plugin": rows["plugin"].id,
            "version": rows["version"].id,
            "approval": rows["approval"].id,
        }
    return ids


def test_database_enums_match_the_contracts() -> None:
    """The checks in the database name exactly the contract's states, scopes, reasons,
    decisions and methods, and its patterns and caps agree with the contract's."""
    assert get_args(spec.PluginVersionState) == m.PLUGIN_VERSION_STATES
    assert get_args(spec.YankReason) == m.PLUGIN_YANK_REASONS
    fields = spec.PluginVersionSummary.model_fields
    assert get_args(fields["scope"].annotation) == m.PLUGIN_VERSION_SCOPES
    decision = spec.PluginDecisionRecord.model_fields["decision"].annotation
    assert get_args(decision) == m.PLUGIN_DECISIONS
    method = spec.VerifiedUploader.model_fields["method"].annotation
    assert get_args(method) == m.USER_VERIFICATION_METHODS
    assert m.TENANT_CODE_RE == spec.TENANT_CODE_RE
    assert m.PLUGIN_SHORT_RE == spec.PLUGIN_SHORT_RE
    assert re.sub(r"[()]", "", spec.UPLOAD_NAMESPACE_RE) == m.UPLOAD_NAMESPACE_RE
    assert spec.ArchiveLimits().max_bytes == m.PLUGIN_UPLOAD_MAX_BYTES
    assert m.PLUGIN_INSPECTION_MAX_BYTES > spec.INSPECTION_MAX_BYTES
    assert m.PLUGIN_EXPORTS_MAX_BYTES > spec.DESCRIBE_MAX_BYTES
    for _ in range(20):
        assert re.fullmatch(m.TENANT_CODE_RE.strip("^$"), spec.new_tenant_code())
    assert set(TABLE_NAMES) <= set(m.TENANT_TABLES)


async def test_v019_tables_force_rls(ctx: ServerContext) -> None:
    """Every new table has forced row-level security with the tenant and platform policies,
    select/insert/update for the request path and nothing for the gateway: a tenant session
    sees only its own rows (no tenant filter in the query) and cannot reach, change or add
    the other tenant's."""
    ids = await _seed(ctx)
    async with await psycopg.AsyncConnection.connect(ctx.db.url) as conn:
        for table in TABLE_NAMES:
            rls = await (
                await conn.execute(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE relname = %s",
                    (table,),
                )
            ).fetchone()
            assert rls == (True,), table
            policies = {
                r[0]
                for r in await (
                    await conn.execute(
                        "SELECT policyname FROM pg_policies WHERE tablename = %s", (table,)
                    )
                ).fetchall()
            }
            assert policies == {"mapi_tenant_isolation", "mapi_platform_access"}, table
            grants = await (
                await conn.execute(
                    "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = %s",
                    (table,),
                )
            ).fetchall()
            assert {p for g, p in grants if g == "mapi_app"} == {
                "SELECT",
                "INSERT",
                "UPDATE",
            }, table
            assert not {p for g, p in grants if g == "mapi_gateway"}, table
    for tenant, other in (("alpha", "beta"), ("beta", "alpha")):
        theirs = ids[other]
        async with ctx.db.tenant_session(tenant) as s:
            for model in V019_TABLES:
                found = (await s.execute(select(model.tenant_id))).scalars().all()  # type: ignore[attr-defined]
                assert found == [tenant], model
            assert await s.get(m.TenantPluginSettings, other) is None
            assert await s.get(m.UserVerification, (other, "usr_ed")) is None
            assert await s.get(m.Plugin, theirs["plugin"]) is None
            assert await s.get(m.PluginVersion, theirs["version"]) is None
            assert await s.get(m.PluginDescribe, (theirs["version"], BASE)) is None
            assert await s.get(m.PluginApproval, theirs["approval"]) is None
            for stmt in (
                update(m.TenantPluginSettings)
                .where(m.TenantPluginSettings.tenant_id == other)
                .values(private_use=False),
                update(m.UserVerification)
                .where(m.UserVerification.tenant_id == other)
                .values(method="provider"),
                update(m.Plugin).where(m.Plugin.id == theirs["plugin"]).values(title="Mine"),
                update(m.PluginUpload)
                .where(m.PluginUpload.plugin_id == theirs["plugin"])
                .values(state="expired"),
                update(m.PluginVersion)
                .where(m.PluginVersion.id == theirs["version"])
                .values(state="approved", scope="tenant"),
                update(m.PluginDescribe)
                .where(m.PluginDescribe.version_id == theirs["version"])
                .values(ok=False),
                update(m.PluginApproval)
                .where(m.PluginApproval.id == theirs["approval"])
                .values(decision="approve"),
            ):
                res = await s.execute(stmt)
                assert res.rowcount == 0  # type: ignore[attr-defined]
    foreign = _rows("beta")
    for key in ORDER:
        with pytest.raises((ProgrammingError, IntegrityError)):
            async with ctx.db.tenant_session("alpha") as s:
                s.add(foreign[key])
                await s.flush()
    async with ctx.db.session() as s:  # the platform role sees both tenants, untouched
        states = (await s.execute(select(m.PluginVersion.state))).scalars().all()
        assert states == ["awaiting_approval", "awaiting_approval"]
        settings = (await s.execute(select(m.TenantPluginSettings.private_use))).scalars().all()
        assert settings == [True, True]


async def test_foreign_rows_are_refused_by_row_level_security(ctx: ServerContext) -> None:
    """A tenant session cannot add a row that names another tenant, even for a table
    without a foreign key into that tenant's rows."""
    await _seed(ctx)
    for key in ("settings", "verification", "plugin"):
        row = _rows("gamma")[key]
        row.tenant_id = "beta"
        if key == "settings":
            row.tenant_code = "gggggg"
        if key == "plugin":
            row.namespace, row.short_name = "t_gggggg_other", "other"
        with pytest.raises(ProgrammingError, match="row-level security"):
            async with ctx.db.tenant_session("alpha") as s:
                s.add(row)
                await s.flush()


async def test_the_request_path_never_deletes_and_rows_go_with_their_tenant(
    ctx: ServerContext,
) -> None:
    """A purge, a revocation or an expired ticket keeps its row: the request path cannot
    delete any of them. Versions, describes, approvals and uploads go with their plugin;
    everything goes with the tenant."""
    ids = await _seed(ctx)
    for model in V019_TABLES:
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(delete(model))
    async with ctx.db.session() as s:  # the platform role may delete a plugin outright
        await s.execute(delete(m.Plugin).where(m.Plugin.id == ids["alpha"]["plugin"]))
    async with ctx.db.session() as s:
        for model in (m.PluginUpload, m.PluginVersion, m.PluginDescribe, m.PluginApproval):
            tenants = (await s.execute(select(model.tenant_id))).scalars().all()
            assert tenants == ["beta"], model
        await s.execute(delete(m.Tenant).where(m.Tenant.id == "beta"))
    async with ctx.db.session() as s:
        for model in V019_TABLES:
            tenants = (await s.execute(select(model.tenant_id))).scalars().all()  # type: ignore[attr-defined]
            assert set(tenants) <= {"alpha"}, model
        settings = (await s.execute(select(m.TenantPluginSettings.tenant_id))).scalars().all()
        assert settings == ["alpha"]


async def test_v019_checks_back_the_contracts(ctx: ServerContext) -> None:
    """Tenant codes, namespaces and short names follow their patterns; states, scopes,
    methods, reasons and decisions are closed; tenant scope needs approval; sizes are capped;
    paired columns are set together; nobody attests or approves for themselves; a rejection
    has a note."""
    await _seed(ctx)
    big = {"blob": "x" * 1_100_001}
    checks: tuple[tuple[Any, str], ...] = (
        (update(m.TenantPluginSettings).values(tenant_code="ABC123"), "tenant_code"),
        (update(m.TenantPluginSettings).values(tenant_code="k3vq1m"), "tenant_code"),
        (update(m.UserVerification).values(method="kyc"), "method"),
        (update(m.UserVerification).values(verified_by="usr_ed"), "not_self"),
        (update(m.UserVerification).values(revoked_by="usr_admin"), "revoked"),
        (update(m.Plugin).values(namespace="acme", short_name="acme"), "namespace"),
        (update(m.Plugin).values(namespace="t_k3vq7m_acme_x"), "namespace"),
        (update(m.Plugin).values(short_name="other"), "namespace_short"),
        (update(m.PluginUpload).values(state="done"), "state"),
        (update(m.PluginUpload).values(filename="acme.tar.gz"), "filename"),
        (update(m.PluginUpload).values(filename="../acme.whl"), "filename"),
        (update(m.PluginUpload).values(declared_size=20_971_521), "declared_size"),
        (update(m.PluginUpload).values(declared_size=0), "declared_size"),
        (update(m.PluginVersion).values(state="building"), "state"),
        (update(m.PluginVersion).values(scope="public"), "scope"),
        (update(m.PluginVersion).values(scope="tenant"), "tenant_scope"),
        (update(m.PluginVersion).values(upload_sha256="0" * 64), "upload_sha256"),
        (update(m.PluginVersion).values(size_bytes=20_971_521), "size_bytes"),
        (update(m.PluginVersion).values(overlay_generation=None), "overlay_object"),
        (update(m.PluginVersion).values(inspection=big), "inspection_size"),
        (
            update(m.PluginVersion).values(yank_reason="bored", yanked_at=EXPIRES),
            "yank_reason",
        ),
        (update(m.PluginVersion).values(yank_reason="security"), "yanked"),
        (update(m.PluginVersion).values(state="purged"), "purged"),
        (update(m.PluginVersion).values(purged_at=EXPIRES), "purged"),
        (update(m.PluginDescribe).values(exports={"blob": "x" * 2_200_001}), "exports_size"),
        (update(m.PluginDescribe).values(describe_digest="abc"), "describe_digest"),
        (update(m.PluginApproval).values(decision="maybe"), "decision"),
        (update(m.PluginApproval).values(approver_id="usr_ana"), "not_self"),
        (update(m.PluginApproval).values(note=None), "reject_note"),
    )
    for stmt, name in checks:
        table = stmt.table.name
        with pytest.raises(IntegrityError, match=f"ck_{table}_{name}"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(stmt)
    async with ctx.db.tenant_session("alpha") as s:  # the allowed shapes
        await s.execute(update(m.PluginVersion).values(state="approved", scope="tenant"))
        await s.execute(update(m.PluginVersion).values(inspection={"blob": "x" * 1_000_000}))
        await s.execute(
            update(m.PluginVersion).values(
                state="yanked", scope="none", yanked_at=EXPIRES, yanked_by="usr_admin",
                yank_reason="rekit_failed", purge_after=EXPIRES,
            )
        )  # fmt: skip
        await s.execute(
            update(m.PluginVersion).values(
                state="purged", purged_at=EXPIRES, overlay_key=None, overlay_generation=None
            )
        )
        await s.execute(update(m.PluginApproval).values(decision="approve", note=None))
        await s.execute(
            update(m.UserVerification).values(revoked_at=EXPIRES, revoked_by="usr_admin")
        )


async def test_codes_namespaces_and_versions_are_unique(ctx: ServerContext) -> None:
    """A tenant code and a namespace are unique on the platform, a short name per tenant
    and a version number per plugin."""
    ids = await _seed(ctx)
    await make_tenant(ctx, "gamma", tier="internal")
    gamma = _rows("gamma")
    taken = m.TenantPluginSettings(tenant_id="gamma", tenant_code=CODES["alpha"])
    with pytest.raises(IntegrityError, match="uq_tenant_plugin_settings_tenant_code"):
        async with ctx.db.tenant_session("gamma") as s:
            s.add(taken)
            await s.flush()
    gamma["plugin"].namespace = f"t_{CODES['alpha']}_acme"
    with pytest.raises(IntegrityError, match="uq_plugins_namespace"):
        async with ctx.db.tenant_session("gamma") as s:
            s.add(gamma["plugin"])
            await s.flush()
    again = _rows("alpha")["plugin"]
    again.namespace = "t_cccccc_acme"
    with pytest.raises(IntegrityError, match="uq_plugins_tenant_id_short_name"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(again)
            await s.flush()
    twice = _rows("alpha")["version"]
    twice.plugin_id = ids["alpha"]["plugin"]
    with pytest.raises(IntegrityError, match="uq_plugin_versions_plugin_id_version"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(twice)
            await s.flush()


async def test_keys_pin_the_tenant_and_the_approved_bytes(ctx: ServerContext) -> None:
    """An approval names the exact bytes of its version (``(version_id, overlay_sha256)``);
    uploads, versions and describes only ever join a parent of their own tenant, even for a
    role that bypasses row-level security."""
    ids = await _seed(ctx)
    alpha = ids["alpha"]
    stale = m.PluginApproval(
        id=new_id("pap"), tenant_id="alpha", version_id=alpha["version"],
        overlay_sha256=OTHER_HASH, describe_digest=HASH, base_image_digest=BASE,
        decision="approve", approver_id="usr_admin", uploader_id="usr_ana",
    )  # fmt: skip
    with pytest.raises(IntegrityError, match="fk_plugin_approvals_version_id_plugin_versions"):
        async with ctx.db.tenant_session("alpha") as s:
            s.add(stale)
            await s.flush()
    with pytest.raises(IntegrityError, match="fk_plugin_approvals_version_id_plugin_versions"):
        async with ctx.db.session() as s:  # the version's bytes cannot change under it
            await s.execute(
                update(m.PluginVersion)
                .where(m.PluginVersion.id == alpha["version"])
                .values(upload_sha256=OTHER_HASH)
            )
    beta_rows = _rows("beta")
    upload, version, describe = beta_rows["upload"], beta_rows["version"], beta_rows["describe"]
    upload.plugin_id = version.plugin_id = alpha["plugin"]
    version.version = "2.0.0"  # not a duplicate of alpha's version number
    describe.version_id = alpha["version"]
    describe.base_image_digest = "sha256:" + "b" * 64  # not alpha's describe of the version
    for row, key in (
        (upload, "fk_plugin_uploads_tenant_id_plugins"),
        (version, "fk_plugin_versions_tenant_id_plugins"),
        (describe, "fk_plugin_describes_tenant_id_plugin_versions"),
    ):
        with pytest.raises(IntegrityError, match=key):
            async with ctx.db.session() as s:
                s.add(row)
                await s.flush()


async def test_overlay_pins_imply_untrusted_code_and_are_indexed(ctx: ServerContext) -> None:
    """``runs.overlays`` defaults to ``[]``; a run with pins must be an untrusted-code run and
    the pins must be a list. The GIN index (``jsonb_path_ops``) answers "which runs mount
    this version"."""
    await make_tenant(ctx, "alpha", tier="internal")
    pin = {"plugin_version_id": new_id("plv"), "namespace": "t_k3vq7m_acme", "sha256": HASH}
    async with ctx.db.tenant_session("alpha") as s:
        for run_id in ("run_plain", "run_pinned"):
            s.add(
                m.Run(
                    id=run_id, tenant_id="alpha", project_id="main", kind="fit",
                    state="queued", resource_class="S", timeout_s=60,
                    untrusted_code=run_id == "run_pinned",
                )
            )  # fmt: skip
        await s.flush()
        plain = await s.get(m.Run, "run_plain")
        assert plain is not None
        assert plain.overlays == []
        await s.execute(update(m.Run).where(m.Run.id == "run_pinned").values(overlays=[pin]))
    for values in ({"overlays": [pin]}, {"overlays": [pin], "untrusted_code": False}):
        with pytest.raises(IntegrityError, match="ck_runs_overlays_untrusted"):
            async with ctx.db.tenant_session("alpha") as s:
                await s.execute(update(m.Run).where(m.Run.id == "run_plain").values(**values))
    with pytest.raises(IntegrityError, match="ck_runs_overlays_array"):
        async with ctx.db.tenant_session("alpha") as s:
            await s.execute(
                update(m.Run).where(m.Run.id == "run_pinned").values(overlays={"pin": pin})
            )
    async with ctx.db.tenant_session("alpha") as s:
        found = (
            (
                await s.execute(
                    select(m.Run.id).where(
                        m.Run.overlays.contains([{"plugin_version_id": pin["plugin_version_id"]}])
                    )
                )
            )
            .scalars()
            .all()
        )
        assert found == ["run_pinned"]
        definition = (
            await s.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_runs_overlays'")
            )
        ).scalar_one()
    assert "USING gin (overlays jsonb_path_ops)" in definition


async def test_partial_indexes_cover_the_sweeps(ctx: ServerContext) -> None:
    """The purge sweep's index skips purged versions; the ticket sweep finds pending tickets
    by expiry."""
    async with ctx.db.session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                    "('ix_plugin_versions_purge', 'ix_plugin_uploads_expiry', "
                    "'ix_plugin_versions_state')"
                )
            )
        ).all()
    defs: dict[str, str] = {name: definition for name, definition in rows}
    assert "WHERE (purged_at IS NULL)" in defs["ix_plugin_versions_purge"]
    assert "(state, expires_at)" in defs["ix_plugin_uploads_expiry"]
    assert "(tenant_id, state)" in defs["ix_plugin_versions_state"]
