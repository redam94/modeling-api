"""Every endpoint a program may call has a request builder whose method, path, success
statuses and project scoping match ``mapi_spec.api.ALL_ENDPOINTS`` (the v0.5 table: M1, M2,
M3 and M3.5, plus the M4 rows) and ``ROUTE_ACCESS`` - the contract the server's route table
is tested against."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest
from mapi_client import _calls as c
from mapi_client import _calls_m3 as m3
from mapi_client import _calls_m4 as m4
from mapi_client import _calls_m35 as m35
from mapi_client import _calls_tracking as tr
from mapi_client._http import Call
from mapi_spec.api import (
    ALL_ENDPOINTS,
    ENDPOINTS,
    IDEMPOTENCY_KEY_HEADER,
    PREFER_HEADER,
    ROUTE_ACCESS,
    ApprovalCreate,
    DataExploreRequest,
    DatasetAboutUpdate,
    DatasetCreate,
    DatasetVersionCreate,
    DetectRolesRequest,
    EmailVerify,
    EndpointDecl,
    EndpointPromote,
    EndpointRollback,
    EndpointScoreCall,
    EvidencePackRequest,
    ExecutorEvidenceUpdate,
    ExecutorUpdate,
    GdprExportRequest,
    GovernancePolicyCreate,
    GovernancePolicyUpdate,
    GuidedAnswers,
    GuidedCompile,
    IngestMapping,
    KillSwitchUpdate,
    MembershipCreate,
    MembershipUpdate,
    ModelCompareRequest,
    ModelExploreRequest,
    OptimizeRequest,
    PlotEmbedCreate,
    PlotRenderRequest,
    PlotSpec,
    PriorTranslationRequest,
    ProjectCreate,
    ProjectUpdate,
    QuotaPolicyUpsert,
    RouteScope,
    RunCreate,
    RunRetry,
    SavedPlotCreate,
    ScheduleCreate,
    ScheduleUpdate,
    ScoreCall,
    SignupRequest,
    SignupReviewDecision,
    SpecCreate,
    SpecRevisionCreate,
    TenantAdminUpdate,
    TenantCreate,
    TenantDeletionRequest,
    TenantUpdate,
    TokenCreate,
    TriggerCreate,
    UploadCreate,
    ValidateRequest,
    ValidationNote,
    ValidationRequest,
    VerificationUpdate,
    WebhookCreate,
    WebhookUpdate,
    WorkflowCreate,
    WorkflowRevisionCreate,
    WorkflowRunCreate,
    WorkflowUpdate,
    WorkflowValidateRequest,
)
from mapi_spec.quotas import QuotaLimits

_YAML = "apiVersion: mapi/v1\n"
_SCORE = ScoreCall.model_validate(ScoreCall.model_json_schema()["examples"][0])
_PLOT = PlotSpec.model_validate(PlotSpec.model_json_schema()["examples"][0])
_USR = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
_INVITE = MembershipCreate(email="ana@acme.example", role="runner")
_TOKEN = TokenCreate(name="ci", scopes=["models:read"])
_QUOTA = QuotaPolicyUpsert(scope="tenant", subject_id="acme", limits=QuotaLimits(concurrent_runs=4))


def _ex[M](model: type[M], index: int = 0) -> M:
    """A request DTO from its published schema example."""
    schema = model.model_json_schema()  # type: ignore[attr-defined]
    return model.model_validate(schema["examples"][index])  # type: ignore[attr-defined]


_CALL = _ex(EndpointScoreCall)
_WH = "whk_01jaf3h1j2k3m4n5p6q7r8s9t0"
_PLT = "plt_01jaf3h1j2k3m4n5p6q7r8s9t0"
_WF = "wfl_01jaf3h1j2k3m4n5p6q7r8s9t0"
_POL = "pol_01jaf3h1j2k3m4n5p6q7r8s9t0"
_VAL = "val_01jaf3h1j2k3m4n5p6q7r8s9t0"

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
    "get_catalog_plot_example": lambda: c.get_catalog_plot_example("core.ppc", "1.0.0"),
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
    "get_model_inputs": lambda: c.get_model_inputs("mdl_1", 1, 12),
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
    "admin_get_executor_evidence": lambda: c.admin_get_executor_evidence("cloudrun-jobs"),
    "admin_put_executor_evidence": lambda: c.admin_put_executor_evidence(
        "cloudrun-jobs",
        ExecutorEvidenceUpdate.model_validate(
            ExecutorEvidenceUpdate.model_json_schema()["examples"][0]
        ),
    ),
    "admin_put_executor": lambda: c.admin_put_executor(
        "cloudrun-jobs", ExecutorUpdate(enabled=False, reason="incident")
    ),
    "admin_list_audit": lambda: c.admin_list_audit(None, None),
    "admin_get_usage": lambda: c.admin_get_usage(period="day"),
    # v0.4 guided mode
    "list_guided_templates": lambda: m3.list_guided_templates(None, None),
    "get_guided_template": lambda: m3.get_guided_template("geo_mmm"),
    "detect_roles": lambda: m3.detect_roles(_ex(DetectRolesRequest)),
    "create_guided_draft": lambda: m3.create_guided_draft(_ex(GuidedAnswers)),
    "compile_guided_request": lambda: m3.compile_guided_request(_ex(GuidedCompile)),
    "create_prior_translation": lambda: m3.create_prior_translation(
        _ex(PriorTranslationRequest), None
    ),
    "get_prior_translation": lambda: m3.get_prior_translation("run_1"),
    # v0.4 endpoints
    "list_endpoints": lambda: m3.list_endpoints(None, None),
    "get_endpoint": lambda: m3.get_endpoint("geo-mmm-prod"),
    "promote_endpoint": lambda: m3.promote_endpoint("geo-mmm-prod", _ex(EndpointPromote)),
    "delete_endpoint": lambda: m3.delete_endpoint("geo-mmm-prod"),
    "list_endpoint_history": lambda: m3.list_endpoint_history("geo-mmm-prod", None, None),
    "score_endpoint": lambda: m3.score_endpoint(
        "geo-mmm-prod", _CALL, wait=60, idempotency_key="k"
    ),
    "get_endpoint_schema": lambda: m3.get_endpoint_schema("geo-mmm-prod"),
    "validate_endpoint_request": lambda: m3.validate_endpoint_request("geo-mmm-prod", _CALL),
    "rollback_endpoint": lambda: m3.rollback_endpoint("geo-mmm-prod", _ex(EndpointRollback)),
    "list_endpoint_snippets": lambda: m3.list_endpoint_snippets(
        "geo-mmm-prod", ["python", "curl"], "https://api.example.com"
    ),
    # v0.4 webhooks
    "list_webhooks": lambda: m3.list_webhooks(None, None),
    "create_webhook": lambda: m3.create_webhook(_ex(WebhookCreate)),
    "get_webhook": lambda: m3.get_webhook(_WH),
    "update_webhook": lambda: m3.update_webhook(_WH, _ex(WebhookUpdate)),
    "delete_webhook": lambda: m3.delete_webhook(_WH),
    "rotate_webhook_secret": lambda: m3.rotate_webhook_secret(_WH),
    "list_webhook_deliveries": lambda: m3.list_webhook_deliveries(_WH, None, None, status="failed"),
    "replay_webhook_delivery": lambda: m3.replay_webhook_delivery(_WH, "whd_1"),
    # v0.4 plots, renders and embeds
    "create_saved_plot": lambda: m3.create_saved_plot(_ex(SavedPlotCreate)),
    "list_saved_plots": lambda: m3.list_saved_plots(None, None),
    "get_saved_plot": lambda: m3.get_saved_plot(_PLT),
    "delete_saved_plot": lambda: m3.delete_saved_plot(_PLT),
    "render_plot": lambda: m3.render_plot(_ex(PlotRenderRequest), wait=30, idempotency_key="k"),
    "get_plot_render": lambda: m3.get_plot_render("run_1"),
    "create_plot_embed": lambda: m3.create_plot_embed(_PLT, _ex(PlotEmbedCreate)),
    "list_plot_embeds": lambda: m3.list_plot_embeds(_PLT, None, None),
    "revoke_plot_embed": lambda: m3.revoke_plot_embed(_PLT, "emb_1"),
    "get_embed": lambda: m3.get_embed("mpe_token"),
    # v0.4 trust lifecycle
    "signup": lambda: m3.signup(_ex(SignupRequest)),
    "verify_email": lambda: m3.verify_email(_ex(EmailVerify)),
    "get_tenant_verification": lambda: m3.get_tenant_verification("acme"),
    "admin_put_tenant_verification": lambda: m3.admin_put_tenant_verification(
        "acme", _ex(VerificationUpdate)
    ),
    "request_tenant_deletion": lambda: m3.request_tenant_deletion(
        "acme", TenantDeletionRequest(confirm="acme")
    ),
    "get_tenant_deletion": lambda: m3.get_tenant_deletion("acme"),
    "cancel_tenant_deletion": lambda: m3.cancel_tenant_deletion("acme"),
    "create_tenant_export": lambda: m3.create_tenant_export("acme", GdprExportRequest()),
    "list_tenant_exports": lambda: m3.list_tenant_exports("acme", None, None),
    "get_tenant_export": lambda: m3.get_tenant_export("acme", "exp_1"),
    "admin_list_signups": lambda: m3.admin_list_signups(None, None, review="flagged"),
    "admin_review_signup": lambda: m3.admin_review_signup("sgn_1", _ex(SignupReviewDecision)),
    "admin_retry_tenant_deletion": lambda: m3.admin_retry_tenant_deletion("acme"),
    # v0.5 About cards, chooser and catalogs
    "list_catalog_abouts": lambda: m35.list_catalog_abouts(None, None, kind="model"),
    "get_chooser": m35.get_chooser,
    "recommend_catalog_items": lambda: m35.recommend_catalog_items(
        {"goal": "forecast", "data_shape": ["one_series", "panel"]}, kind="model", limit=5
    ),
    "list_workflow_nodes": lambda: m35.list_workflow_nodes(None, None, kind="test"),
    "get_workflow_node": lambda: m35.get_workflow_node("test.adf"),
    "list_governance_checks": lambda: m35.list_governance_checks(None, None),
    # v0.5 datasets and explorers
    "get_dataset_about": lambda: m35.get_dataset_about("ds_1"),
    "update_dataset_about": lambda: m35.update_dataset_about("ds_1", _ex(DatasetAboutUpdate)),
    "explore_dataset_version": lambda: m35.explore_dataset_version(
        "ds_1", 4, _ex(DataExploreRequest), None
    ),
    "get_exploration": lambda: m35.get_exploration("run_1"),
    "explore_model_version": lambda: m35.explore_model_version(
        "mdl_1", 3, _ex(ModelExploreRequest), None
    ),
    "compare_model_versions": lambda: m35.compare_model_versions(_ex(ModelCompareRequest), None),
    "optimize_model_version": lambda: m35.optimize_model_version(
        "mdl_1", 3, _ex(OptimizeRequest), wait=30, idempotency_key="k"
    ),
    "get_optimization": lambda: m35.get_optimization("run_1"),
    # v0.5 workflows
    "list_workflows": lambda: m35.list_workflows(None, None, archived=False),
    "create_workflow": lambda: m35.create_workflow(_ex(WorkflowCreate)),
    "validate_workflow": lambda: m35.validate_workflow(_ex(WorkflowValidateRequest)),
    "get_workflow": lambda: m35.get_workflow(_WF),
    "update_workflow": lambda: m35.update_workflow(_WF, _ex(WorkflowUpdate)),
    "delete_workflow": lambda: m35.delete_workflow(_WF),
    "list_workflow_revisions": lambda: m35.list_workflow_revisions(_WF, None, None),
    "create_workflow_revision": lambda: m35.create_workflow_revision(
        _WF, _ex(WorkflowRevisionCreate)
    ),
    "get_workflow_revision": lambda: m35.get_workflow_revision(_WF, 2),
    "start_workflow_run": lambda: m35.start_workflow_run(_WF, _ex(WorkflowRunCreate), "k"),
    "list_workflow_runs": lambda: m35.list_workflow_runs(_WF, None, None, status="running"),
    "get_workflow_run": lambda: m35.get_workflow_run("run_1"),
    "cancel_workflow_run": lambda: m35.cancel_workflow_run("run_1"),
    "workflow_run_events": lambda: m35.workflow_run_events("run_1", 7, 100),
    "list_workflow_node_runs": lambda: m35.list_workflow_node_runs(
        "run_1", None, None, status="failed"
    ),
    "get_workflow_node_run": lambda: m35.get_workflow_node_run("run_1", "adf"),
    "get_workflow_node_output": lambda: m35.get_workflow_node_output("run_1", "diff", "data"),
    "list_workflow_schedules": lambda: m35.list_workflow_schedules(_WF, None, None),
    "create_workflow_schedule": lambda: m35.create_workflow_schedule(_WF, _ex(ScheduleCreate)),
    "update_workflow_schedule": lambda: m35.update_workflow_schedule(
        _WF, "wsc_1", _ex(ScheduleUpdate)
    ),
    "delete_workflow_schedule": lambda: m35.delete_workflow_schedule(_WF, "wsc_1"),
    "list_workflow_triggers": lambda: m35.list_workflow_triggers(_WF, None, None),
    "create_workflow_trigger": lambda: m35.create_workflow_trigger(_WF, _ex(TriggerCreate)),
    "delete_workflow_trigger": lambda: m35.delete_workflow_trigger(_WF, "wtr_1"),
    "list_workflow_templates": lambda: m35.list_workflow_templates(None, None),
    "get_workflow_template": lambda: m35.get_workflow_template("ts_readiness_arimax"),
    # v0.5 governance policies
    "list_policies": lambda: m35.list_policies(None, None, scope="project"),
    "create_policy": lambda: m35.create_policy(_ex(GovernancePolicyCreate)),
    "get_policy": lambda: m35.get_policy(_POL),
    "update_policy": lambda: m35.update_policy(_POL, _ex(GovernancePolicyUpdate)),
    "delete_policy": lambda: m35.delete_policy(_POL),
    "list_policy_versions": lambda: m35.list_policy_versions(_POL, None, None),
    "list_tenant_policies": lambda: m35.list_tenant_policies("acme", None, None),
    "create_tenant_policy": lambda: m35.create_tenant_policy("acme", _ex(GovernancePolicyCreate)),
    "get_tenant_policy": lambda: m35.get_tenant_policy("acme", _POL),
    "update_tenant_policy": lambda: m35.update_tenant_policy(
        "acme", _POL, _ex(GovernancePolicyUpdate)
    ),
    "delete_tenant_policy": lambda: m35.delete_tenant_policy("acme", _POL),
    # v0.5 validations
    "request_model_validation": lambda: m35.request_model_validation(
        "mdl_1", 3, _ex(ValidationRequest), None
    ),
    "list_model_validations": lambda: m35.list_model_validations("mdl_1", 3, None, None),
    "list_validations": lambda: m35.list_validations(None, None, status="awaiting_approval"),
    "get_validation": lambda: m35.get_validation(_VAL),
    "list_validation_evidence": lambda: m35.list_validation_evidence(_VAL, None, None),
    "cancel_validation": lambda: m35.cancel_validation(_VAL, _ex(ValidationNote)),
    "resubmit_validation": lambda: m35.resubmit_validation(_VAL, _ex(ValidationNote), None),
    "decide_validation": lambda: m35.decide_validation(_VAL, _ex(ApprovalCreate)),
    "deprecate_validation": lambda: m35.deprecate_validation(_VAL, _ex(ValidationNote)),
    "export_evidence_pack": lambda: m35.export_evidence_pack(_VAL, _ex(EvidencePackRequest)),
    # M4 tree datasets: upload inspection
    "inspect_upload": lambda: m4.inspect_upload("ds_1", "upl_1", None),
    "get_upload_inspection": lambda: m4.get_upload_inspection("ds_1", "upl_1"),
    # v0.11 run tracking
    "update_run": lambda: tr.update_run("run_1", tr.run_patch(name="weekly")),
    "list_run_metrics": lambda: tr.list_run_metrics("run_1", None, None, key="loss"),
    "get_metric_history": lambda: tr.get_metric_history("run_1", "loss", max_points=100),
    "log_run_metrics": lambda: tr.log_run_metrics(
        "run_1", tr.metrics_bodies(tr.metric_points({"loss": 1.0}))[0]
    ),
    "list_run_params": lambda: tr.list_run_params("run_1", None, None, prefix="fit."),
    "log_run_params": lambda: tr.log_run_params("run_1", tr.params_bodies({"a": 1})[0]),
}

CLIENT_ENDPOINTS = ALL_ENDPOINTS
"""The table the client covers: every row of the contract (the v0.5 API plus the M4 rows)."""
PROGRAM_ENDPOINTS = tuple(d for d in CLIENT_ENDPOINTS if d.operation_id not in NOT_FOR_PROGRAMS)


def _pattern(decl: EndpointDecl) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-z_]+\})", decl.path)
    return re.compile(
        "^" + "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + "$"
    )


def test_the_client_covers_the_m35_table() -> None:
    assert {d.operation_id for d in ENDPOINTS} - NOT_FOR_PROGRAMS <= set(BUILDERS)


def test_every_endpoint_has_a_builder() -> None:
    assert {d.operation_id for d in PROGRAM_ENDPOINTS} == set(BUILDERS)
    assert {d.operation_id for d in CLIENT_ENDPOINTS} >= NOT_FOR_PROGRAMS


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


@pytest.mark.parametrize("decl", PROGRAM_ENDPOINTS, ids=lambda d: d.operation_id)
def test_prefer_and_idempotency_headers_follow_the_contract(decl: EndpointDecl) -> None:
    call = BUILDERS[decl.operation_id]()
    if PREFER_HEADER in decl.headers:
        assert call.headers.get(PREFER_HEADER, "").startswith("wait=")
        assert call.server_wait  # the read timeout covers the server's wait
    else:
        assert PREFER_HEADER not in call.headers
    if IDEMPOTENCY_KEY_HEADER not in decl.headers:
        assert IDEMPOTENCY_KEY_HEADER not in call.headers


def test_m3_public_routes_carry_no_credentials() -> None:
    public = {"signup", "verify_email", "get_embed"}
    for op, make in BUILDERS.items():
        assert make().public is (op in public), op


def test_m3_waits_are_capped() -> None:
    with pytest.raises(ValueError):
        m3.score_endpoint("a", _CALL, wait=61, idempotency_key="k")
    with pytest.raises(ValueError):
        m3.render_plot(_ex(PlotRenderRequest), wait=-1, idempotency_key="k")
    call = m3.score_endpoint("a", _CALL, wait=None, idempotency_key="k")
    assert PREFER_HEADER not in call.headers


def test_m3_snippet_languages_are_repeated_and_checked() -> None:
    call = m3.list_endpoint_snippets("a", ["python", "notebook"])
    assert call.params == {"language": ["python", "notebook"]}
    with pytest.raises(ValueError):  # only the documented snippet languages
        m3.list_endpoint_snippets("a", ["cobol"])
    assert m3.list_endpoint_snippets("a").params == {}
    with pytest.raises(ValueError):
        m3.list_endpoint_snippets("a", ["cobol"])


def test_m3_calls_that_mint_secrets_or_send_mail_are_never_resent() -> None:
    """A lost response must not create a second webhook, secret, link or e-mail."""
    assert not m3.create_webhook(_ex(WebhookCreate)).idempotent
    assert not m3.rotate_webhook_secret(_WH).idempotent
    assert not m3.create_plot_embed(_PLT, _ex(PlotEmbedCreate)).idempotent
    assert not m3.signup(_ex(SignupRequest)).idempotent
    assert not m3.verify_email(_ex(EmailVerify)).idempotent
    deletion = m3.request_tenant_deletion("acme", TenantDeletionRequest(confirm="acme"))
    assert not deletion.idempotent
    # static checks and keyed submissions are safe to resend
    assert m3.validate_endpoint_request("a", _CALL).idempotent
    assert m3.score_endpoint("a", _CALL, wait=1, idempotency_key="k").idempotent
    assert m3.revoke_plot_embed(_PLT, "emb_1").idempotent


def test_m3_filters_are_validated() -> None:
    with pytest.raises(ValueError):
        m3.list_webhook_deliveries(_WH, None, None, status="lost")
    with pytest.raises(ValueError):
        m3.admin_list_signups(None, None, review="maybe")
    assert m3.admin_list_signups(5, None, tenant_id="acme").params == {
        "limit": 5,
        "tenant_id": "acme",
    }
    with pytest.raises(ValueError):
        m3.get_endpoint("..")
    assert m3.get_embed("a/b").path == "/v1/embeds/a%2Fb"


def test_m4_inspection_calls() -> None:
    post = m4.inspect_upload("ds 1", "upl_1", "k")
    assert post.path == "/v1/datasets/ds%201/uploads/upl_1/inspect"
    assert post.expect == frozenset({200, 202}) and post.json is None and post.idempotent
    get = m4.get_upload_inspection("ds_1", "upl_1")
    assert get.path.endswith("/uploads/upl_1/inspection") and get.idempotent
    with pytest.raises(ValueError):
        m4.get_upload_inspection("ds_1", "..")


# ------------------------------------------------------------------------ v0.11 run tracking


def test_list_runs_search_parameters() -> None:
    call = c.list_runs(
        5,
        None,
        parent_run_id="run_0",
        name="weekly",
        tags={"stage": "candidate", "reviewed": None},
        filters=["metric.rmse < 0.5", "param.fit.draws >= 1000"],
        order_by="-metric.loss{split=val}.min",
        columns=["metric.rmse", "param.fit.draws"],
    )
    assert call.params == {
        "limit": 5,
        "parent_run_id": "run_0",
        "name": "weekly",
        "tag": ["stage:candidate", "reviewed"],
        "filter": ["metric.rmse < 0.5", "param.fit.draws >= 1000"],
        "order_by": "-metric.loss{split=val}.min",
        "column": ["metric.rmse", "param.fit.draws"],
    }
    assert c.list_runs(None, None, tags=["mapi.source:api", "x"]).params == {
        "tag": ["mapi.source:api", "x"]
    }
    for bad in (
        {"filters": ["metric.train/loss < 1"]},
        {"filters": ["metric.rmse"]},
        {"filters": ["metric.rmse < high"]},
        {"filters": ["created_at > 1"]},
        {"filters": "metric.rmse < 1"},
        {"filters": ["metric.rmse < 1"] * 9},
        {"order_by": "-metric.rmse.median"},
        {"columns": ["nope"]},
        {"columns": ["metric.rmse"] * 17},
        {"tags": ["bad key"]},
    ):
        with pytest.raises(ValueError):
            c.list_runs(None, None, **bad)


def test_run_logs_level_and_user_stream() -> None:
    call = c.run_logs("run_1", None, None, stream="user", level="warning")
    assert call.params == {"stream": "user", "level": "warning"}
    # the server sends user lines without a stream filter only when asked (old clients)
    assert c.run_logs("run_1", None, None).params == {"include_user": "true"}
    assert c.run_logs("run_1", None, None, include_user=False).params == {}
    with pytest.raises(ValueError):
        c.run_logs("run_1", None, None, level="fatal")


def test_tracking_query_parameters() -> None:
    hist = tr.get_metric_history(
        "run_1", "step_size", max_points=500, attempt=2, labels={"chain": "0", "fit": "evaluation"}
    )
    assert hist.path == "/v1/runs/run_1/metrics/step_size"
    assert hist.params == {
        "max_points": 500,
        "attempt": 2,
        "label": ["chain:0", "fit:evaluation"],
    }
    for kwargs in ({"max_points": 5}, {"max_points": 10_001}, {"attempt": 0}):
        with pytest.raises(ValueError):
            tr.get_metric_history("run_1", "loss", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        tr.get_metric_history("run_1", "train/loss")
    with pytest.raises(ValueError):
        tr.get_metric_history("run_1", "loss", labels={"chain": "a b"})
    metrics = tr.list_run_metrics("run_1", 50, "cur", key="loss", attempt=1)
    assert metrics.params == {"limit": 50, "cursor": "cur", "key": "loss", "attempt": 1}
    assert tr.list_run_params("run_1", None, None, prefix="settings.").params == {
        "prefix": "settings."
    }


def test_run_patch_is_a_merge_patch() -> None:
    call = tr.update_run("run_1", tr.run_patch(note=None, tags={"old": None, "stage": "a"}))
    assert call.method == "PATCH" and call.idempotent
    assert call.json == {"note": None, "tags": {"old": None, "stage": "a"}}
    assert tr.update_run("run_1", tr.run_patch(name="  x ")).json == {"name": "x"}
    with pytest.raises(ValueError):
        tr.run_patch()
    with pytest.raises(ValueError):
        tr.run_patch(tags={"mapi.owner": "me"})


def test_tracking_writes_split_into_bodies() -> None:
    points = tr.metric_points({f"m{i}": float(i) for i in range(2500)}, step=3)
    bodies = tr.metrics_bodies(points)
    assert [len(b.metrics) for b in bodies] == [1000, 1000, 500]
    assert tr.metric_points({"a": float("nan"), "b": float("inf"), "c": 1.0}) == [
        tr.metric_points({"c": 1.0})[0]
    ]
    params = tr.params_bodies({f"p{i}": i for i in range(1200)})
    assert [len(b.params) for b in params] == [500, 500, 200]
    with pytest.raises(ValueError):
        tr.params_bodies({"bad key": 1})
