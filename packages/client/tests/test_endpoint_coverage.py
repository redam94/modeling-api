"""Every endpoint a program may call has a request builder whose method, path, success
statuses and project scoping match ``mapi_spec.api.ENDPOINTS`` and ``ROUTE_ACCESS`` (the
contract the server's route table is tested against)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from mapi_client import _calls as c
from mapi_client._http import Call
from mapi_spec.api import (
    ENDPOINTS,
    ROUTE_ACCESS,
    DatasetCreate,
    DatasetVersionCreate,
    EndpointDecl,
    IngestMapping,
    KillSwitchUpdate,
    MembershipCreate,
    MembershipUpdate,
    PlotSpec,
    ProjectCreate,
    ProjectUpdate,
    QuotaPolicyUpsert,
    RouteScope,
    RunCreate,
    RunRetry,
    ScoreCall,
    SpecCreate,
    SpecRevisionCreate,
    TenantAdminUpdate,
    TenantCreate,
    TenantUpdate,
    TokenCreate,
    UploadCreate,
    ValidateRequest,
)
from mapi_spec.quotas import QuotaLimits

_YAML = "apiVersion: mapi/v1\n"
_SCORE = ScoreCall.model_validate(ScoreCall.model_json_schema()["examples"][0])
_PLOT = PlotSpec.model_validate(PlotSpec.model_json_schema()["examples"][0])
_USR = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
_INVITE = MembershipCreate(email="ana@acme.example", role="runner")
_TOKEN = TokenCreate(name="ci", scopes=["models:read"])
_QUOTA = QuotaPolicyUpsert(scope="tenant", subject_id="acme", limits=QuotaLimits(concurrent_runs=4))

NOT_FOR_PROGRAMS = frozenset(
    {
        # browser sign-in (BFF): cookies and 302s, used by the web app only
        "auth_login",
        "auth_callback",
        "auth_logout",
        "auth_session",
        # Pub/Sub push with a Google-signed service-account token
        "ingest_event",
    }
)

BUILDERS: dict[str, Callable[[], Call[Any]]] = {
    "health": c.health,
    "ready": c.ready,
    "list_projects": lambda: c.list_projects(None, None),
    "get_project": lambda: c.get_project("default"),
    "get_catalog": c.get_catalog,
    "list_catalog_models": lambda: c.list_catalog_models(None, None),
    "get_catalog_model": lambda: c.get_catalog_model("mmm.geo", "1.0.0"),
    "list_catalog_steps": lambda: c.list_catalog_steps(None, None),
    "list_catalog_plots": lambda: c.list_catalog_plots(None, None),
    "get_schema": lambda: c.get_schema("modelspec.json"),
    "create_dataset": lambda: c.create_dataset(DatasetCreate(name="d")),
    "list_datasets": lambda: c.list_datasets(None, None),
    "get_dataset": lambda: c.get_dataset("ds_1"),
    "create_upload": lambda: c.create_upload("ds_1", UploadCreate(filename="a.csv", size_bytes=1)),
    "create_dataset_version": lambda: c.create_dataset_version(
        "ds_1",
        DatasetVersionCreate(upload_id="upl_1", mapping=IngestMapping(format="zarr_zip")),
        None,
    ),
    "list_dataset_versions": lambda: c.list_dataset_versions("ds_1", None, None),
    "get_dataset_version": lambda: c.get_dataset_version("ds_1", 3),
    "get_dataset_artifact": lambda: c.get_dataset_artifact("ds_1", 3, "preview.json"),
    "validate_spec": lambda: c.validate_spec(ValidateRequest(yaml=_YAML)),
    "create_spec": lambda: c.create_spec(SpecCreate(yaml=_YAML)),
    "list_specs": lambda: c.list_specs(None, None),
    "get_spec": lambda: c.get_spec("spc_1"),
    "create_spec_revision": lambda: c.create_spec_revision(
        "spc_1", SpecRevisionCreate(yaml=_YAML, parent_revision=1)
    ),
    "list_spec_revisions": lambda: c.list_spec_revisions("spc_1", None, None),
    "get_spec_revision": lambda: c.get_spec_revision("spc_1", 2),
    "dry_run_spec": lambda: c.dry_run_spec("spc_1", 2, None),
    "create_run": lambda: c.create_run(RunCreate(kind="fit", spec_id="spc_1", revision=1), None),
    "list_runs": lambda: c.list_runs(None, None),
    "get_run": lambda: c.get_run("run_1"),
    "run_events": lambda: c.list_run_events("run_1", None, None),
    "run_logs": lambda: c.run_logs("run_1", None, None),
    "list_run_plots": lambda: c.list_run_plots("run_1", None, None),
    "cancel_run": lambda: c.cancel_run("run_1"),
    "resume_run": lambda: c.resume_run("run_1"),
    "retry_run": lambda: c.retry_run("run_1", RunRetry(), None),
    "list_models": lambda: c.list_models(None, None),
    "get_model": lambda: c.get_model("mdl_1"),
    "list_model_versions": lambda: c.list_model_versions("mdl_1", None, None),
    "get_model_version": lambda: c.get_model_version("mdl_1", 1),
    "get_model_artifact": lambda: c.get_model_artifact("mdl_1", 1, "bundle.zarr.zip"),
    "score_model": lambda: c.score_model("mdl_1", 1, _SCORE, wait=60, idempotency_key="k"),
    "list_plots": lambda: c.list_plots("mdl_1", 1, None, None),
    "get_plot": lambda: c.get_plot("mdl_1", 1, "core.ppc_fan"),
    "validate_plot": lambda: c.validate_plot(_PLOT),
    # v0.3 identity
    "get_me": c.get_me,
    "list_tenants": lambda: c.list_tenants(None, None),
    "get_tenant": lambda: c.get_tenant("acme"),
    "update_tenant": lambda: c.update_tenant("acme", TenantUpdate(name="Acme")),
    "list_tenant_projects": lambda: c.list_tenant_projects("acme", None, None),
    "create_project": lambda: c.create_project("acme", ProjectCreate(id="growth", name="G")),
    "update_project": lambda: c.update_project("acme", "growth", ProjectUpdate(description="d")),
    "delete_project": lambda: c.delete_project("acme", "growth"),
    "list_tenant_members": lambda: c.list_members("acme", None, None, None),
    "add_tenant_member": lambda: c.add_member("acme", None, _INVITE),
    "update_tenant_member": lambda: c.update_member(
        "acme", None, _USR, MembershipUpdate(role="admin")
    ),
    "remove_tenant_member": lambda: c.remove_member("acme", None, _USR),
    "list_members": lambda: c.list_members("acme", "growth", None, None),
    "add_member": lambda: c.add_member("acme", "growth", _INVITE),
    "update_member": lambda: c.update_member(
        "acme", "growth", _USR, MembershipUpdate(role="viewer")
    ),
    "remove_member": lambda: c.remove_member("acme", "growth", _USR),
    "get_tenant_usage": lambda: c.get_tenant_usage("acme", period="day"),
    "list_tenant_audit": lambda: c.list_tenant_audit("acme", None, None),
    "list_tokens": lambda: c.list_tokens(None, None),
    "create_token": lambda: c.create_token(_TOKEN),
    "get_token": lambda: c.get_token("tok_1"),
    "revoke_token": lambda: c.revoke_token("tok_1"),
    "get_usage": c.get_usage,
    # v0.3 operators
    "admin_list_tenants": lambda: c.admin_list_tenants(None, None),
    "admin_create_tenant": lambda: c.admin_create_tenant(TenantCreate(id="acme", name="A")),
    "admin_get_tenant": lambda: c.admin_get_tenant("acme"),
    "admin_update_tenant": lambda: c.admin_update_tenant(
        "acme", TenantAdminUpdate(trust_tier="internal")
    ),
    "admin_list_quotas": lambda: c.admin_list_quotas(None, None),
    "admin_put_quota": lambda: c.admin_put_quota(_QUOTA),
    "admin_delete_quota": lambda: c.admin_delete_quota("tenant", "acme"),
    "admin_get_kill_switches": c.admin_get_kill_switches,
    "admin_put_kill_switches": lambda: c.admin_put_kill_switches(
        KillSwitchUpdate(untrusted_execution_enabled=False, reason="drill")
    ),
    "admin_list_executors": lambda: c.admin_list_executors(None, None),
    "admin_get_executor": lambda: c.admin_get_executor("cloudrun-jobs"),
    "admin_list_audit": lambda: c.admin_list_audit(None, None),
    "admin_get_usage": lambda: c.admin_get_usage(period="day"),
}

PROGRAM_ENDPOINTS = tuple(d for d in ENDPOINTS if d.operation_id not in NOT_FOR_PROGRAMS)


def _pattern(decl: EndpointDecl) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-z_]+\})", decl.path)
    return re.compile(
        "^" + "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + "$"
    )


def test_every_endpoint_has_a_builder() -> None:
    assert {d.operation_id for d in PROGRAM_ENDPOINTS} == set(BUILDERS)
    assert {d.operation_id for d in ENDPOINTS} >= NOT_FOR_PROGRAMS


@pytest.mark.parametrize("decl", PROGRAM_ENDPOINTS, ids=lambda d: d.operation_id)
def test_builder_matches_the_contract(decl: EndpointDecl) -> None:
    call = BUILDERS[decl.operation_id]()
    assert call.method == decl.method
    assert _pattern(decl).match(call.path), (call.path, decl.path)
    assert decl.success in call.expect
    assert (call.json is not None) == (decl.request is not None)
    for name in call.params:
        assert name in decl.query, (name, decl.query)
    if "Idempotency-Key" in decl.headers:
        assert "Idempotency-Key" in call.headers
    if decl.paginated:
        assert call.parse.__qualname__.startswith("parse_model")
        assert decl.response is not None and decl.response.startswith("Page[")


def test_path_segments_are_encoded() -> None:
    call = c.get_model_artifact("mdl_1", 1, "ui/plot data/a.json")
    assert call.path.endswith("/artifacts/ui/plot%20data/a.json")
    with pytest.raises(ValueError):
        c.get_model_artifact("mdl_1", 1, "ui/../secret")
    with pytest.raises(ValueError):
        c.get_run("..")
    assert c.get_run("a/b").path.endswith("/runs/a%2Fb")


def test_list_parameters() -> None:
    call = c.list_runs(10, "cur", kind="fit", state="running", spec_id="spc_1")
    assert call.params == {
        "limit": 10,
        "cursor": "cur",
        "kind": "fit",
        "state": "running",
        "spec_id": "spc_1",
    }
    with pytest.raises(ValueError):
        c.list_runs(500, None)
    with pytest.raises(ValueError):
        c.list_runs(None, None, state="bogus")
    with pytest.raises(ValueError):
        c.run_logs("run_1", None, None, stream="journal")


def test_prefer_wait_is_capped() -> None:
    call = c.score_model("m", 1, _SCORE, wait=30, idempotency_key="k")
    assert call.headers["Prefer"] == "wait=30"
    with pytest.raises(ValueError):
        c.score_model("m", 1, _SCORE, wait=61, idempotency_key="k")


@pytest.mark.parametrize("decl", PROGRAM_ENDPOINTS, ids=lambda d: d.operation_id)
def test_project_header_goes_only_where_the_project_is_selected_by_it(
    decl: EndpointDecl,
) -> None:
    """X-Mapi-Project is sent on project-scoped routes whose path does not name the project
    (a header naming a different project than the path is a 400)."""
    call = BUILDERS[decl.operation_id]()
    access = ROUTE_ACCESS[decl.operation_id]
    expected = access.scope is RouteScope.PROJECT and "{project_id}" not in decl.path
    assert call.scoped is expected


def test_m2_query_parameters() -> None:
    from datetime import UTC, datetime

    call = c.list_tokens(5, "cur", all_owners=True, status="revoked")
    assert call.params == {"limit": 5, "cursor": "cur", "all": "true", "status": "revoked"}
    call = c.get_usage(
        period="day", start=datetime(2026, 10, 1, tzinfo=UTC), group_by="resource_class"
    )
    assert call.params == {
        "period": "day",
        "start": "2026-10-01T00:00:00+00:00",
        "group_by": "resource_class",
    }
    with pytest.raises(ValueError):
        c.get_usage(period="year")
    with pytest.raises(ValueError):
        c.get_usage(group_by="colour")
    with pytest.raises(ValueError):
        c.get_usage(start=datetime(2026, 10, 1))  # naive
    audit = c.admin_list_audit(10, None, tenant_id="acme", action="token.create", outcome="denied")
    assert audit.params == {
        "limit": 10,
        "tenant_id": "acme",
        "action": "token.create",
        "outcome": "denied",
    }
    with pytest.raises(ValueError):
        c.admin_list_audit(None, None, action="token.steal")
    assert c.admin_delete_quota("global", "*").path == "/v1/admin/quotas/global/%2A"
    with pytest.raises(ValueError):
        c.admin_list_tenants(None, None, trust_tier="royal")


def test_minting_a_token_is_never_resent() -> None:
    """A lost response to POST /v1/tokens must not mint a second token."""
    assert not c.create_token(_TOKEN).idempotent
    assert c.revoke_token("tok_1").idempotent
