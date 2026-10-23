"""0022_plugins up and down: it upgrades a database holding v0.18 rows (every existing run
gets ``overlays = []``), matches the models, steps down only once no plugin, plugin version
or run with overlays is left (naming the counts) and steps up again cleanly."""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.script import ScriptDirectory
from mapi_server.db.migrate import alembic_config, downgrade, upgrade

pytestmark = pytest.mark.integration

BEFORE = "0021_v018_library_onboarding"
HASH = "sha256:" + "0" * 64
V019_NEW = {
    "tenant_plugin_settings",
    "user_verifications",
    "plugins",
    "plugin_uploads",
    "plugin_versions",
    "plugin_describes",
    "plugin_approvals",
}


@pytest.fixture
def scratch_db(pg_admin_url: str) -> Iterator[str]:
    name = f"mapi_mig_{secrets.token_hex(4)}"
    with psycopg.connect(pg_admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    parts = urlsplit(pg_admin_url)
    url = urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
    try:
        yield url
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {r[0] for r in rows}


def _columns(url: str, table: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)
        ).fetchall()
    return {r[0] for r in rows}


def _v018_seed(url: str) -> None:
    """What a v0.18 database holds: a tenant, a project, a library item, a trusted run and
    an untrusted one (an uploaded model in v0.18 terms)."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, trust_tier, status) VALUES ('t1', 't1', "
            "'internal', 'active')"
        )
        conn.execute(
            "INSERT INTO projects (tenant_id, id, project_id, name) VALUES ('t1', 'p1', 'p1', 'p1')"
        )
        conn.execute(
            "INSERT INTO library_items (id, tenant_id, project_id, scope, kind, name, title, "
            "created_by) VALUES ('lib_1', 't1', 'p1', 'project', 'prep_preset', 'p', 'P', "
            "'usr_1')"
        )
        for run_id, untrusted in (("run_1", "false"), ("run_2", "true")):
            conn.execute(
                "INSERT INTO runs (id, tenant_id, project_id, kind, state, attempt, "
                "resource_class, timeout_s, labels, payload, inputs, checkpointable, "
                "infra_retries, finalize_attempts, finalize, last_event_id, untrusted_code) "
                f"VALUES ('{run_id}', 't1', 'p1', 'fit', 'succeeded', 1, 'S', 60, '{{}}', '{{}}', "
                f"'{{}}', false, 0, 0, '{{}}', 0, {untrusted})"
            )


def _v019_seed(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """One claimed plugin with an approved version, its describe and approval, the tenant's
    settings, an attestation and a run that mounted the version."""
    conn.execute(
        "INSERT INTO tenant_plugin_settings (tenant_id, tenant_code) VALUES ('t1', 'k3vq7m')"
    )
    conn.execute(
        "INSERT INTO user_verifications (tenant_id, user_id, method, verified_by) "
        "VALUES ('t1', 'usr_ed', 'admin_attested', 'usr_admin')"
    )
    conn.execute(
        "INSERT INTO plugins (id, tenant_id, namespace, short_name, title, created_by) "
        "VALUES ('plg_1', 't1', 't_k3vq7m_acme', 'acme', 'Acme', 'usr_ed')"
    )
    conn.execute(
        "INSERT INTO plugin_versions (id, tenant_id, plugin_id, version, state, scope, "
        "project_id, uploaded_by, upload_sha256, size_bytes, quarantine_key, "
        "quarantine_generation) VALUES ('plv_1', 't1', 'plg_1', '1.0.0', 'approved', 'tenant', "
        f"'p1', 'usr_ed', '{HASH}', 100, 'k', 1)"
    )
    conn.execute(
        "INSERT INTO plugin_describes (version_id, base_image_digest, tenant_id, run_id, "
        f"describe_digest, exports, kit, ok) VALUES ('plv_1', '{HASH}', 't1', 'run_d', "
        f"'{HASH}', '{{}}', '{{}}', true)"
    )
    conn.execute(
        "INSERT INTO plugin_approvals (id, tenant_id, version_id, overlay_sha256, "
        "describe_digest, base_image_digest, decision, approver_id, uploader_id) VALUES "
        f"('pap_1', 't1', 'plv_1', '{HASH}', '{HASH}', '{HASH}', 'approve', 'usr_admin', "
        "'usr_ed')"
    )
    pin = json.dumps([{"plugin_version_id": "plv_1", "namespace": "t_k3vq7m_acme", "sha256": HASH}])
    conn.execute("UPDATE runs SET overlays = %s::jsonb WHERE id = 'run_2'", (pin,))


def test_0022_chains_off_0021_and_0023_is_the_head(scratch_db: str) -> None:
    """0022 steps from 0021; 0023 (session auth_time, v0.19) steps from 0022 and is the single
    head (v0.20's next migration must step from it)."""
    script = ScriptDirectory.from_config(alembic_config(scratch_db))
    assert script.get_heads() == ["0023_session_auth_time"]
    assert script.get_revision("0023_session_auth_time").down_revision == "0022_plugins"
    assert script.get_revision("0022_plugins").down_revision == BEFORE


def test_0023_adds_session_auth_time_and_steps_down(scratch_db: str) -> None:
    """review-flow-3: ``web_sessions.auth_time`` and ``login_transactions.reauth`` come and
    go with 0023; the models and the migrations agree."""
    upgrade(scratch_db)
    assert "auth_time" in _columns(scratch_db, "web_sessions")
    assert "reauth" in _columns(scratch_db, "login_transactions")
    command.check(alembic_config(scratch_db))
    downgrade(scratch_db, "0022_plugins")
    assert "auth_time" not in _columns(scratch_db, "web_sessions")
    assert "reauth" not in _columns(scratch_db, "login_transactions")
    upgrade(scratch_db)
    assert "auth_time" in _columns(scratch_db, "web_sessions")


def test_upgrade_keeps_v018_rows_and_downgrade_refuses_with_uploaded_code(
    scratch_db: str,
) -> None:
    """0022 upgrades a database with v0.18 rows (every run gets no overlays; the models and
    the migration agree), refuses to step down while a plugin, a version or a run with
    overlays exists, naming the counts, and steps down and up again once they are gone.
    Settings and attestations go without asking."""
    upgrade(scratch_db, BEFORE)
    _v018_seed(scratch_db)
    upgrade(scratch_db)
    assert _tables(scratch_db) >= V019_NEW
    command.check(alembic_config(scratch_db))  # models and migrations agree
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        overlays = conn.execute("SELECT id, overlays FROM runs ORDER BY id").fetchall()
        assert overlays == [("run_1", []), ("run_2", [])]
        with pytest.raises(psycopg.errors.CheckViolation, match="ck_runs_overlays_untrusted"):
            conn.execute("UPDATE runs SET overlays = '[{}]'::jsonb WHERE id = 'run_1'")
        _v019_seed(conn)
    with pytest.raises(Exception, match=r"1 plugins, 1 plugin versions, 1 runs with overlays"):
        downgrade(scratch_db, BEFORE)
    assert _tables(scratch_db) >= V019_NEW
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("UPDATE runs SET overlays = '[]'::jsonb")
    with pytest.raises(Exception, match=r"1 plugins, 1 plugin versions, 0 runs with overlays"):
        downgrade(scratch_db, BEFORE)
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        conn.execute("DELETE FROM plugins")  # versions, describes and approvals go with it
        left = conn.execute(
            "SELECT (SELECT count(*) FROM plugin_versions), (SELECT count(*) FROM "
            "plugin_describes), (SELECT count(*) FROM plugin_approvals)"
        ).fetchone()
        assert left == (0, 0, 0)
    downgrade(scratch_db, BEFORE)
    assert not V019_NEW & _tables(scratch_db)
    assert "overlays" not in _columns(scratch_db, "runs")
    with psycopg.connect(scratch_db) as conn:
        runs = conn.execute("SELECT id, untrusted_code FROM runs ORDER BY id").fetchall()
        assert runs == [("run_1", False), ("run_2", True)]
        assert conn.execute("SELECT id FROM library_items").fetchall() == [("lib_1",)]
        gone = conn.execute(
            "SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_runs_overlays%'"
        ).fetchall()
        assert gone == []
    upgrade(scratch_db)
    assert _tables(scratch_db) >= V019_NEW
    with psycopg.connect(scratch_db) as conn:
        settings = conn.execute("SELECT count(*) FROM tenant_plugin_settings").fetchone()
        assert settings == (0,)
    command.check(alembic_config(scratch_db))
