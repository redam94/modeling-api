"""Uploaded code over HTTP (v0.19 WP5, plan I-5): claims and the tenant code, upload tickets,
``:complete``, version reads, usage, the public-tier and client refusals, tenant deletion.

The sandboxed ``plugin_inspect`` run is replaced by a stand-in that only moves the version
to ``inspecting`` (the pipeline has its own tests); describe results are written as rows.
No test opens, unpacks or imports a wheel: the "wheels" here are opaque bytes.

The helpers at the top are shared with the other ``test_plugin_*`` modules.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.services.mail import MailMessage
from mapi_server.services.plugins import pipeline, store
from mapi_server.services.runs import Scope, apply_transition, create_run, now
from mapi_server.settings import MailerKind, Profile, Settings
from mapi_server_testkit.identity import (
    Caller,
    grant_role,
    make_tenant,
    make_token,
    make_user,
    session_caller,
    token_caller,
)
from mapi_spec.api import CLIENT_OPERATIONS, V019_ENDPOINTS, new_id
from mapi_spec.executor import RunKind
from mapi_spec.plots import PlotSpec
from mapi_spec.plugins import PluginExports, PluginKitReport, describe_digest
from mapi_spec.resources import ResourceClass
from mapi_spec.run import Actor, RunState, RunTrigger
from mapi_spec.storage import NotFound, plugin_key
from sqlalchemy import select, update

pytestmark = [pytest.mark.integration, pytest.mark.filterwarnings("ignore")]

URL = "/v1/plugins"
SHA_I = "sha256:" + "ab" * 32  # the test settings' image digest (conftest.IMAGE_DIGEST)


# ===================================================================================== helpers


class RecordingMailer:
    """Keeps every message (the outbox of a test)."""

    kind = MailerKind.OUTBOX

    def __init__(self) -> None:
        self.sent: list[MailMessage] = []

    async def send(self, message: MailMessage) -> None:
        self.sent.append(message)


@dataclass
class World:
    ctx: ServerContext
    client: httpx.AsyncClient
    callers: dict[str, Caller] = field(default_factory=dict)
    users: dict[str, m.User] = field(default_factory=dict)
    mail: RecordingMailer = field(default_factory=RecordingMailer)

    def __getitem__(self, name: str) -> Caller:
        return self.callers[name]

    def uid(self, name: str) -> str:
        return self.users[name].id


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"profile": Profile.MULTI_TENANT})


@pytest.fixture
def fake_inspect(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the sandboxed inspect run: record the version and move it to ``inspecting``."""
    started: list[str] = []

    async def start_inspect(
        session: Any, ctx: Any, version: m.PluginVersion, *, created_by: str | None
    ) -> None:
        started.append(version.id)
        store.set_state(version, "inspecting", now=now())

    monkeypatch.setattr(pipeline, "start_inspect", start_inspect)
    return started


async def make_world(
    ctx: ServerContext, client: httpx.AsyncClient, *, tier: str = "internal"
) -> World:
    """Tenant ``acme`` (projects growth, retail) with two tenant admins (``admin``,
    ``admin2``), a third (``admin3``), an attested editor (``editor``), an editor without an
    attestation (``plain``), a viewer, a client; tenant ``other`` with its admin."""
    w = World(ctx, client)
    await make_tenant(ctx, "acme", tier=tier, projects=("growth", "retail"))
    await make_tenant(ctx, "other", tier="internal", projects=("main",))
    for name, tenant, project, role in (
        ("admin", "acme", None, "admin"),
        ("admin2", "acme", None, "admin"),
        ("admin3", "acme", None, "admin"),
        ("editor", "acme", "growth", "editor"),
        ("plain", "acme", "growth", "editor"),
        ("viewer", "acme", "growth", "viewer"),
        ("client", "acme", "growth", "client"),
        ("outsider", "other", None, "admin"),
    ):
        user = await make_user(ctx, f"{name}@{tenant}.test")
        await grant_role(ctx, user, tenant, project, role)
        w.users[name] = user
        w.callers[name] = await session_caller(
            ctx,
            client,
            user,
            tenant_id=tenant,
            project_id="main" if tenant == "other" else "growth",
        )
    async with ctx.db.session() as s:
        s.add(
            m.UserVerification(
                tenant_id="acme",
                user_id=w.uid("editor"),
                method="admin_attested",
                verified_by=w.uid("admin"),
                verified_at=now(),
            )
        )
    assert ctx.trust is not None
    ctx.trust.mailer = w.mail
    return w


async def claim(caller: Caller, short: str = "acme", title: str = "Acme tools") -> dict[str, Any]:
    r = await caller.post(URL, json={"short_name": short, "title": title})
    assert r.status_code == 201, r.text
    return dict(r.json())


def wheel_name(namespace: str, version: str = "0.1.0") -> str:
    return f"{namespace}-{version}-py3-none-any.whl"


async def upload(
    w: World,
    caller: Caller,
    plugin: dict[str, Any],
    data: bytes = b"PK\x03\x04 opaque test bytes",
    *,
    version: str = "0.1.0",
    project: str = "growth",
    request_approval: bool = False,
) -> dict[str, Any]:
    """Ticket, then the bytes straight into the quarantine bucket (as the browser's POST
    would), then ``:complete``. Returns the version JSON."""
    r = await caller.post(
        f"{URL}/{plugin['id']}/uploads",
        json={"filename": wheel_name(plugin["namespace"], version), "size": len(data)},
    )
    assert r.status_code == 201, r.text
    ticket = r.json()
    key = ticket["post"]["fields"]["key"]
    await w.ctx.store.put(w.ctx.buckets.quarantine, key, data)
    r = await caller.post(
        f"{URL}/{plugin['id']}/uploads/{ticket['upload_id']}:complete",
        json={"project": project, "request_approval": request_approval},
    )
    assert r.status_code == 202, r.text
    return dict(r.json())


async def pass_kit(
    ctx: ServerContext,
    version_id: str,
    exports: PluginExports | None = None,
    *,
    examples: dict[str, Any] | None = None,
    base: str = SHA_I,
) -> str:
    """What the pipeline does after a clean inspect and describe: ``describing``, a describe
    row, ``kit_passed``. Returns the describe digest."""
    exports = exports or PluginExports()
    digest = describe_digest(exports)
    async with ctx.db.session() as s:
        v = await s.get(m.PluginVersion, version_id, with_for_update=True)
        assert v is not None
        store.set_state(v, "describing", now=now())
        s.add(
            m.PluginDescribe(
                version_id=v.id,
                base_image_digest=base,
                tenant_id=v.tenant_id,
                run_id=new_id("run"),
                describe_digest=digest,
                exports=exports.model_dump(mode="json", exclude_defaults=True),
                plot_examples=examples or {},
                kit=PluginKitReport(ok=True, checks=[]).model_dump(mode="json"),
                issues=[],
                ok=True,
            )
        )
        store.set_state(v, "kit_passed", now=now())
    return digest


async def move(ctx: ServerContext, version_id: str, *states: str) -> None:
    """Walk a version through ``states`` with the state writer (test set-up)."""
    async with ctx.db.session() as s:
        v = await s.get(m.PluginVersion, version_id, with_for_update=True)
        assert v is not None
        for state in states:
            store.set_state(v, state, now=now())  # type: ignore[arg-type]


async def version_row(ctx: ServerContext, version_id: str) -> m.PluginVersion:
    async with ctx.db.session() as s:
        v = await s.get(m.PluginVersion, version_id)
        assert v is not None
        return v


_PARAM = re.compile(r"\{([^}:]+)\}")


def endpoint_path(template: str, **values: str) -> str:
    defaults = {
        "plugin_id": "plg_" + "0" * 26,
        "version_id": "plv_" + "0" * 26,
        "upload_id": "pup_" + "0" * 26,
        "plot_id": "t_abcdef_acme.chart",
        "tenant_id": "acme",
        "user_id": "usr_" + "0" * 26,
    }
    return _PARAM.sub(lambda mt: values.get(mt.group(1), defaults[mt.group(1)]), template)


def body_for(operation_id: str) -> dict[str, Any] | None:
    sha = "sha256:" + "cd" * 32
    bodies: dict[str, dict[str, Any]] = {
        "claim_plugin": {"short_name": "zeta", "title": "Zeta"},
        "create_plugin_upload": {"filename": "x-0.1.0-py3-none-any.whl", "size": 10},
        "complete_plugin_upload": {"project": "growth"},
        "approve_plugin_version": {
            "overlay_sha256": sha, "describe_digest": sha, "base_image_digest": sha,
        },
        "reject_plugin_version": {
            "overlay_sha256": sha, "describe_digest": sha, "base_image_digest": sha, "note": "no",
        },
        "yank_plugin_version": {"reason": "withdrawn"},
        "update_plugin_settings": {"private_use": False},
    }  # fmt: skip
    return bodies.get(operation_id)


# ======================================================================================= claims


async def test_claim_assigns_the_tenant_code_once(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    w = await make_world(ctx, client)
    first = await claim(w["admin"], "acme")
    second = await claim(w["editor"], "beta", "Beta")
    code = first["namespace"].split("_")[1]
    assert re.fullmatch(r"[a-z2-7]{6}", code)
    assert first["namespace"] == f"t_{code}_acme"
    assert second["namespace"] == f"t_{code}_beta"
    assert first["versions"] == 0 and first["latest_version"] is None
    settings = await w["admin"].get("/v1/tenants/acme/plugin-settings")
    assert settings.json()["tenant_code"] == code
    # another tenant gets another code
    theirs = await claim(w["outsider"], "acme")
    assert theirs["namespace"] != first["namespace"]
    # the same short name again in the tenant
    r = await w["admin2"].post(URL, json={"short_name": "acme", "title": "Again"})
    assert r.status_code == 409 and r.json()["code"] == "PLUGIN_NAMESPACE_TAKEN", r.text
    async with ctx.db.session() as s:
        actions = list(
            (
                await s.execute(select(m.AuditEvent.action).where(m.AuditEvent.tenant_id == "acme"))
            ).scalars()
        )
    assert actions.count("plugin.claim") == 2


async def test_who_may_claim(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    r = await w["plain"].post(URL, json={"short_name": "plain", "title": "x"})
    assert r.status_code == 403 and r.json()["code"] == "FORBIDDEN"
    assert "tenant admin must confirm" in r.json()["detail"]
    r = await w["viewer"].post(URL, json={"short_name": "view", "title": "x"})
    assert r.status_code == 403
    await claim(w["editor"], "attested")
    # bad short names never reach the handler
    for short in ("a", "has_underscore", "Upper", "x" * 17):
        r = await w["admin"].post(URL, json={"short_name": short, "title": "x"})
        assert r.status_code == 422, short


async def test_verified_tier_needs_identity_verification(
    ctx: ServerContext, client: httpx.AsyncClient
) -> None:
    w = await make_world(ctx, client, tier="verified")
    r = await w["admin"].post(URL, json={"short_name": "acme", "title": "x"})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "IDENTITY_VERIFICATION_REQUIRED"
    settings = (await w["admin"].get("/v1/tenants/acme/plugin-settings")).json()
    assert settings["uploads_allowed"] is False and settings["reason"]


async def test_public_tier_is_refused_on_every_route(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ctx.settings, "public_code_upload_enabled", True)
    w = await make_world(ctx, client, tier="public")
    async with ctx.db.session() as s:  # even with identity verification
        s.add(
            m.TenantVerification(
                tenant_id="acme",
                email_verified=True,
                identity_verified=True,
                method="manual",
                updated_at=now(),
            )
        )
    seen = 0
    for e in V019_ENDPOINTS:
        path = endpoint_path(e.path, user_id=w.uid("editor"))
        r = await w["admin"].request(e.method, path, json=body_for(e.operation_id))
        assert r.status_code == 403, (e.operation_id, r.status_code, r.text)
        seen += 1
    assert seen == len(V019_ENDPOINTS) >= 18


async def test_clients_reach_no_plugin_route(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    assert not {e.operation_id for e in V019_ENDPOINTS} & set(CLIENT_OPERATIONS)
    for e in V019_ENDPOINTS:
        path = endpoint_path(e.path)
        r = await w["client"].request(e.method, path, json=body_for(e.operation_id))
        # a client is refused before any handler runs (404, like every route outside
        # CLIENT_OPERATIONS, so the route's existence is not revealed)
        assert r.status_code in (403, 404), (e.operation_id, r.status_code)
        assert r.status_code < 200 or r.status_code >= 400


async def test_tokens_push_but_never_approve_or_yank(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    secret = await make_token(ctx, w.users["editor"], "acme", "growth", ["plugins:publish"])
    tok = token_caller(client, secret)
    plugin = await claim(tok, "tok")
    v = await upload(w, tok, plugin)
    assert fake_inspect == [v["id"]]
    await pass_kit(ctx, v["id"])
    base = f"{URL}/{plugin['id']}/versions/{v['id']}"
    r = await tok.post(base + ":request-approval")
    assert r.status_code == 200 and r.json()["state"] == "awaiting_approval", r.text
    for action, body in (
        (":approve", body_for("approve_plugin_version")),
        (":yank", {"reason": "withdrawn"}),
    ):
        r = await tok.post(base + action, json=body)
        assert r.status_code == 403, (action, r.text)
    # a token cannot upload for another project
    r = await tok.post(
        f"{URL}/{plugin['id']}/uploads",
        json={"filename": wheel_name(plugin["namespace"], "0.2.0"), "size": 3},
    )
    assert r.status_code == 201
    r = await tok.post(
        f"{URL}/{plugin['id']}/uploads/{r.json()['upload_id']}:complete",
        json={"project": "retail"},
    )
    assert r.status_code == 403, r.text


# ====================================================================================== uploads


async def test_ticket_checks(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    ns = plugin["namespace"]
    tickets = f"{URL}/{plugin['id']}/uploads"
    r = await w["admin"].post(tickets, json={"filename": wheel_name(ns), "size": 20_971_521})
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE", r.text
    r = await w["admin"].post(tickets, json={"filename": "acme-0.1.0.tar.gz", "size": 10})
    assert r.status_code == 422, r.text
    for name, check in (
        (f"{ns}-0.1.0-cp312-cp312-linux_x86_64.whl", "wheel_tag"),
        ("t_aaaaaa_other-0.1.0-py3-none-any.whl", "dist_name"),
        ("mapi_plugin_mmm-0.1.0-py3-none-any.whl", "dist_name"),
    ):
        r = await w["admin"].post(tickets, json={"filename": name, "size": 10})
        assert r.status_code == 422, (name, r.text)
        body = r.json()
        assert body["code"] == "PLUGIN_REFUSED"
        assert body["errors"][0]["context"] == {"stage": "upload", "checks": [check]}
    r = await w["admin"].post(tickets, json={"filename": wheel_name(ns), "size": 10})
    assert r.status_code == 201, r.text
    t = r.json()
    post = t["post"]
    assert post["create_only"] is True and post["content_length_range"] == [1, 20_971_520]
    assert post["fields"]["key"] == f"t/acme/plugins/{t['upload_id']}/plugin.whl"
    assert post["prefix"] == f"t/acme/plugins/{t['upload_id']}/"
    assert t["max_bytes"] == 20_971_520


async def test_twenty_tickets_a_day(ctx: ServerContext, client: httpx.AsyncClient) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["editor"])
    tickets = f"{URL}/{plugin['id']}/uploads"
    for i in range(20):
        name = wheel_name(plugin["namespace"], f"0.{i}.0")
        r = await w["editor"].post(tickets, json={"filename": name, "size": 10})
        assert r.status_code == 201, (i, r.text)
    name = wheel_name(plugin["namespace"], "0.99.0")
    r = await w["editor"].post(tickets, json={"filename": name, "size": 10})
    assert r.status_code == 429 and r.json()["code"] == "RATE_LIMITED", r.text
    assert int(r.headers["Retry-After"]) > 0
    # the cap is per person
    r = await w["admin"].post(tickets, json={"filename": name, "size": 10})
    assert r.status_code == 201


async def test_complete(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["editor"])
    ns = plugin["namespace"]
    tickets = f"{URL}/{plugin['id']}/uploads"
    data = b"opaque wheel bytes " * 1000

    async def ticket(version: str, size: int) -> dict[str, Any]:
        r = await w["editor"].post(
            tickets, json={"filename": wheel_name(ns, version), "size": size}
        )
        assert r.status_code == 201, r.text
        return dict(r.json())

    def done(t: dict[str, Any]) -> str:
        return f"{tickets}/{t['upload_id']}:complete"

    # nothing uploaded yet
    t = await ticket("0.1.0", len(data))
    r = await w["editor"].post(done(t), json={"project": "growth"})
    assert r.status_code == 412 and r.json()["code"] == "PRECONDITION_FAILED", r.text
    # someone else's ticket
    await ctx.store.put(ctx.buckets.quarantine, t["post"]["fields"]["key"], data)
    r = await w["admin"].post(done(t), json={"project": "growth"})
    assert r.status_code == 404, r.text
    # the declared size must match
    t2 = await ticket("0.1.1", len(data) + 1)
    await ctx.store.put(ctx.buckets.quarantine, t2["post"]["fields"]["key"], data)
    r = await w["editor"].post(done(t2), json={"project": "growth"})
    assert r.status_code == 422 and r.json()["code"] == "PLUGIN_REFUSED", r.text
    # the editor cannot bill a project where they are not an editor
    r = await w["editor"].post(done(t), json={"project": "retail"})
    assert r.status_code == 403, r.text
    # ok: hashed, recorded, inspect started
    r = await w["editor"].post(done(t), json={"project": "growth", "request_approval": True})
    assert r.status_code == 202, r.text
    v = r.json()
    assert v["state"] == "inspecting" and v["scope"] == "none" and v["version"] == "0.1.0"
    assert v["overlay_sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert fake_inspect == [v["id"]]
    row = await version_row(ctx, v["id"])
    assert row.size_bytes == len(data) and row.requested_by == w.uid("editor")
    assert row.quarantine_key == t["post"]["fields"]["key"] and row.quarantine_generation
    # a ticket completes once
    r = await w["editor"].post(done(t), json={"project": "growth"})
    assert r.status_code == 412, r.text
    # the same version again: refused at the ticket and at `:complete`
    r = await w["editor"].post(tickets, json={"filename": wheel_name(ns), "size": 5})
    assert r.status_code == 409 and r.json()["code"] == "PLUGIN_VERSION_EXISTS", r.text
    async with ctx.db.session() as s:
        actions = list(
            (
                await s.execute(
                    select(m.AuditEvent.action, m.AuditEvent.details).where(
                        m.AuditEvent.resource_id == v["id"]
                    )
                )
            ).all()
        )
    assert [a for a, _ in actions] == ["plugin.publish"]
    assert actions[0][1]["upload_sha256"] == v["overlay_sha256"]


async def test_an_expired_ticket(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    r = await w["admin"].post(
        f"{URL}/{plugin['id']}/uploads",
        json={"filename": wheel_name(plugin["namespace"]), "size": 3},
    )
    t = r.json()
    await ctx.store.put(ctx.buckets.quarantine, t["post"]["fields"]["key"], b"abc")
    async with ctx.db.session() as s:
        await s.execute(
            update(m.PluginUpload)
            .where(m.PluginUpload.id == t["upload_id"])
            .values(expires_at=now() - timedelta(seconds=1))
        )
    r = await w["admin"].post(
        f"{URL}/{plugin['id']}/uploads/{t['upload_id']}:complete", json={"project": "growth"}
    )
    assert r.status_code == 412 and "expired" in r.json()["detail"], r.text
    assert fake_inspect == []


async def test_a_refused_inspect_admission_records_nothing(
    ctx: ServerContext, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``UNTRUSTED_PLACEMENT_UNAVAILABLE`` from the inspect admission: no version row, and
    the same ticket can be completed once an executor can run uploaded code."""
    from mapi_spec.errors import ErrorCode, MapiError

    async def refuse(*a: Any, **k: Any) -> None:
        raise MapiError("no executor", code=ErrorCode.UNTRUSTED_PLACEMENT_UNAVAILABLE)

    monkeypatch.setattr(pipeline, "start_inspect", refuse)
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    r = await w["admin"].post(
        f"{URL}/{plugin['id']}/uploads",
        json={"filename": wheel_name(plugin["namespace"]), "size": 3},
    )
    t = r.json()
    await ctx.store.put(ctx.buckets.quarantine, t["post"]["fields"]["key"], b"abc")
    done = f"{URL}/{plugin['id']}/uploads/{t['upload_id']}:complete"
    r = await w["admin"].post(done, json={"project": "growth"})
    assert r.status_code == 409 and r.json()["code"] == "UNTRUSTED_PLACEMENT_UNAVAILABLE"
    async with ctx.db.session() as s:
        assert (await s.execute(select(m.PluginVersion))).first() is None
        up = await s.get(m.PluginUpload, t["upload_id"])
        assert up is not None and up.state == "pending"


# ===================================================================================== reading


async def test_private_versions_are_hidden_from_teammates(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["editor"])
    v = await upload(w, w["editor"], plugin)
    await pass_kit(ctx, v["id"])
    one = f"{URL}/{plugin['id']}/versions/{v['id']}"
    for name in ("editor", "admin"):
        r = await w[name].get(one)
        assert r.status_code == 200 and r.json()["scope"] == "private", (name, r.text)
        assert r.headers["ETag"]
        listed = (await w[name].get(URL)).json()["items"]
        assert [p["latest_version"]["id"] for p in listed] == [v["id"]]
    # a viewer sees neither the version nor (it has no visible version) the plugin
    assert (await w["viewer"].get(one)).status_code == 404
    assert (await w["viewer"].get(URL)).json()["items"] == []
    assert (await w["viewer"].get(f"{URL}/{plugin['id']}")).status_code == 404
    assert (await w["outsider"].get(f"{URL}/{plugin['id']}")).status_code == 404
    # approved: everyone in the tenant
    await move(ctx, v["id"], "awaiting_approval", "approved")
    r = await w["viewer"].get(one)
    assert r.status_code == 200 and r.json()["scope"] == "tenant"
    page = (await w["viewer"].get(f"{URL}/{plugin['id']}/versions")).json()
    assert [i["id"] for i in page["items"]] == [v["id"]]
    # filters
    assert (await w["viewer"].get(URL, params={"state": "approved"})).json()["items"]
    assert not (await w["viewer"].get(URL, params={"state": "rejected"})).json()["items"]
    assert not (await w["viewer"].get(URL, params={"mine": True})).json()["items"]
    assert (await w["editor"].get(URL, params={"mine": True})).json()["items"]


async def test_version_detail_and_etag(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    v = await upload(w, w["admin"], plugin)
    digest = await pass_kit(ctx, v["id"])
    one = f"{URL}/{plugin['id']}/versions/{v['id']}"
    r = await w["admin"].get(one)
    body = r.json()
    assert body["describe_digest"] == digest and body["base_image_digest"] == SHA_I
    assert body["kit"] == {"ok": True, "checks": []}
    assert body["exports"] == {"model": 0, "step": 0, "plot": 0, "node": 0, "template": 0}
    r2 = await w["admin"].get(one, headers={"If-None-Match": r.headers["ETag"]})
    assert r2.status_code == 304
    await move(ctx, v["id"], "awaiting_approval")
    r3 = await w["admin"].get(one, headers={"If-None-Match": r.headers["ETag"]})
    assert r3.status_code == 200 and r3.headers["ETag"] != r.headers["ETag"]


async def test_example_is_sanitized_again(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    v = await upload(w, w["admin"], plugin)
    plot_id = f"{plugin['namespace']}.chart"
    spec = copy.deepcopy(PlotSpec.model_json_schema()["examples"][0])
    spec["id"] = plot_id
    await pass_kit(ctx, v["id"], examples={plot_id: spec, "bad.one": {"spec": "nope"}})
    base = f"{URL}/{plugin['id']}/versions/{v['id']}/examples"
    r = await w["admin"].get(f"{base}/{plot_id}")
    assert r.status_code == 200, r.text
    assert r.json()["title"] == spec["title"]
    assert (await w["admin"].get(f"{base}/bad.one")).status_code == 404
    assert (await w["admin"].get(f"{base}/missing.one")).status_code == 404


async def test_usage_counts(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    v = await upload(w, w["admin"], plugin)
    await pass_kit(ctx, v["id"])
    pin = {"plugin_version_id": v["id"], "namespace": plugin["namespace"],
           "sha256": v["overlay_sha256"]}  # fmt: skip
    async with ctx.db.tenant_session("acme") as s:
        for _ in range(2):
            run = await create_run(
                s, Scope("acme", "growth"), kind=RunKind.INGEST,
                resource_class=ResourceClass.S, timeout_s=60, payload={},
            )  # fmt: skip
            run.untrusted_code = True
            run.overlays = [pin]
        other = await create_run(
            s, Scope("acme", "growth"), kind=RunKind.INGEST,
            resource_class=ResourceClass.S, timeout_s=60, payload={},
        )  # fmt: skip
        await s.flush()
        model = m.Model(id=new_id("mdl"), tenant_id="acme", project_id="growth", name="m1")
        s.add(model)
        await s.flush()
        s.add(
            m.ModelVersion(
                id=new_id("mv"), tenant_id="acme", project_id="growth", model_id=model.id,
                version=1, status="ready", run_id=other.id, attempt=1, spec_hash=SHA_I,
                manifest={}, lineage={"overlays": [pin]},
            )
        )  # fmt: skip
    r = await w["admin"].get(f"{URL}/{plugin['id']}/versions/{v['id']}/usage")
    assert r.status_code == 200, r.text
    u = r.json()
    assert u["runs_30d"] == 2 and len(u["model_versions"]) == 1 and u["truncated"] is False


# ============================================================================ deletion, export


async def test_tenant_purge_deletes_uploaded_wheels(
    ctx: ServerContext,
    client: httpx.AsyncClient,
    fake_inspect: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mapi_server.services.deletion import PURGE_BUCKETS, TenantPurger
    from mapi_spec.storage import BucketPurpose

    assert BucketPurpose.PLUGINS in PURGE_BUCKETS
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    v = await upload(w, w["admin"], plugin)
    key = plugin_key("acme", plugin["id"], v["id"])
    await ctx.store.put(ctx.buckets.plugins, key, b"inspected copy")
    purger = TenantPurger(ctx)

    async def progress(*a: Any, **k: Any) -> None:  # no deletion row in this test
        return None

    monkeypatch.setattr(purger, "_update", progress)
    assert await purger._purge_objects("del_" + "0" * 26, "acme", now()) is True
    with pytest.raises(NotFound):
        await ctx.store.stat(ctx.buckets.plugins, key)
    row = await version_row(ctx, v["id"])
    with pytest.raises(NotFound):
        await ctx.store.stat(ctx.buckets.quarantine, row.quarantine_key)


async def test_pinned_runs_cancelled_on_security_yank(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    v = await upload(w, w["admin"], plugin)
    await pass_kit(ctx, v["id"])
    pin = {"plugin_version_id": v["id"], "namespace": plugin["namespace"],
           "sha256": v["overlay_sha256"]}  # fmt: skip
    async with ctx.db.tenant_session("acme") as s:
        pinned = await create_run(
            s, Scope("acme", "growth"), kind=RunKind.INGEST,
            resource_class=ResourceClass.S, timeout_s=60, payload={},
        )  # fmt: skip
        pinned.untrusted_code = True
        pinned.overlays = [pin]
        plain = await create_run(
            s, Scope("acme", "growth"), kind=RunKind.INGEST,
            resource_class=ResourceClass.S, timeout_s=60, payload={},
        )  # fmt: skip
        await s.flush()
        for run in (pinned, plain):
            await apply_transition(s, run, RunTrigger.ACCEPT, Actor.API)
        ids = (pinned.id, plain.id)
    r = await w["admin"].post(
        f"{URL}/{plugin['id']}/versions/{v['id']}:yank", json={"reason": "security"}
    )
    assert r.status_code == 200 and r.json()["state"] == "yanked", r.text
    async with ctx.db.session() as s:
        states = {
            run_id: state
            for run_id, state in (
                await s.execute(select(m.Run.id, m.Run.state).where(m.Run.id.in_(ids)))
            ).all()
        }
    assert states[ids[0]] in (RunState.CANCELLING.value, RunState.CANCELLED.value)
    assert states[ids[1]] == RunState.QUEUED.value


async def test_the_gdpr_export_holds_the_uploaded_wheels(
    ctx: ServerContext, client: httpx.AsyncClient, fake_inspect: list[str]
) -> None:
    """The wheels are copied byte for byte (with ``models``); their rows are in ``metadata``.
    The export's manifest is read here, never the wheel."""
    from mapi_server.services.gdpr_export import ExportBuilder

    w = await make_world(ctx, client)
    plugin = await claim(w["admin"])
    data = b"opaque wheel bytes for the export"
    v = await upload(w, w["admin"], plugin, data)
    _, manifest = await ExportBuilder(ctx)._build("acme", "exp_test", ["models", "metadata"])
    path = f"plugins/{plugin['namespace']}/0.1.0.whl"
    assert manifest["entries"][path]["sha256"] == v["overlay_sha256"].removeprefix("sha256:")
    assert manifest["entries"][path]["size"] == len(data)
    assert manifest["rows"]["metadata/plugin_versions.jsonl"] == 1
    _, without = await ExportBuilder(ctx)._build("acme", "exp_test2", ["metadata"])
    assert path not in without["entries"]
