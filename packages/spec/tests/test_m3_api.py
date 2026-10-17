"""M3 endpoint table: consistent with the M1/M2 conventions, every operation has an access
rule, token scopes reach exactly what they should, and the reference docs list it all."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mapi_spec import api
from mapi_spec.api import (
    ALL_ENDPOINTS,
    ENDPOINTS,
    M1_ENDPOINTS,
    M2_ENDPOINTS,
    M3_ENDPOINTS,
    ROUTE_ACCESS,
    V03_ENDPOINTS,
    RouteScope,
)
from mapi_spec.audit import AuditAction
from mapi_spec.errors import ERROR_INFO, ErrorCode
from mapi_spec.tenancy import PROJECT_HEADER, Action, AuthContext, ProjectRole, UserPrincipal
from mapi_spec.tokens import scope_allows
from pydantic import BaseModel

pytestmark = pytest.mark.unit


def _resolve(name: str) -> type[BaseModel]:
    m = re.fullmatch(r"Page\[(\w+)\]", name)
    obj = getattr(api, m.group(1) if m else name)
    assert isinstance(obj, type) and issubclass(obj, BaseModel), name
    return obj


def test_the_served_table_is_m1_m2_m3_and_m3_is_additive() -> None:
    assert V03_ENDPOINTS == M1_ENDPOINTS + M2_ENDPOINTS
    assert api.V04_ENDPOINTS == V03_ENDPOINTS + M3_ENDPOINTS
    assert ENDPOINTS == api.V04_ENDPOINTS + api.M35_ENDPOINTS  # M3.5 is additive
    assert ALL_ENDPOINTS == ENDPOINTS
    keys = [(e.method, e.path) for e in ALL_ENDPOINTS]
    assert len(keys) == len(set(keys))
    ops = [e.operation_id for e in ALL_ENDPOINTS]
    assert len(ops) == len(set(ops))
    assert len(M3_ENDPOINTS) >= 40


@pytest.mark.parametrize("e", M3_ENDPOINTS, ids=lambda e: e.operation_id)
def test_m3_rows_follow_the_conventions(e: api.EndpointDecl) -> None:
    assert e.path.startswith(("/v1/", "/auth/")), e.path
    assert e.success in (200, 201, 202, 204), e
    assert (e.success == 204) <= (e.response is None), e
    for param in re.findall(r"\{([^}]+)\}", e.path):
        assert re.fullmatch(r"[a-z_]+", param), e.path
    if e.request is not None:
        assert e.method in ("POST", "PUT", "PATCH"), e
        _resolve(e.request)
    if e.response is not None:
        _resolve(e.response)
    assert e.paginated == (e.response or "").startswith("Page["), e
    if e.paginated:
        assert "limit" in e.query, e
    if e.method == "PATCH":
        assert e.request is not None
    assert not e.streaming
    access = ROUTE_ACCESS[e.operation_id]
    if access.scope is RouteScope.PROJECT:
        assert PROJECT_HEADER in e.headers, e.operation_id
    for code in e.errors:
        assert code in ERROR_INFO


def test_public_m3_routes_are_exactly_sign_up_verification_and_embeds() -> None:
    public = {e.operation_id for e in M3_ENDPOINTS if ROUTE_ACCESS[e.operation_id].scope
              is RouteScope.PUBLIC}  # fmt: skip
    assert public == {"signup", "verify_email", "get_embed"}


def test_endpoint_routes_are_named_by_alias() -> None:
    alias_ops = [e for e in M3_ENDPOINTS if "{alias}" in e.path]
    assert len(alias_ops) == 9
    for e in alias_ops:
        assert ROUTE_ACCESS[e.operation_id].name_param == "alias", e.operation_id


@pytest.mark.parametrize(
    ("scope", "allowed"),
    [
        ("score:geo-mmm-prod", {"get_endpoint", "score_endpoint", "get_endpoint_schema",
                                "validate_endpoint_request", "list_endpoint_snippets",
                                "list_endpoint_history"}),
        ("score:other", set()),
        ("endpoints:promote", {"get_endpoint", "get_endpoint_schema",
                               "validate_endpoint_request", "list_endpoint_snippets",
                               "list_endpoint_history", "promote_endpoint", "rollback_endpoint",
                               "delete_endpoint"}),
        ("runs:create", {"get_endpoint", "score_endpoint", "get_endpoint_schema",
                         "validate_endpoint_request", "list_endpoint_snippets",
                         "list_endpoint_history"}),
    ],
)  # fmt: skip
def test_token_scopes_on_endpoint_routes(scope: str, allowed: set[str]) -> None:
    got = set()
    for e in M3_ENDPOINTS:
        acc = ROUTE_ACCESS[e.operation_id]
        if acc.name_param != "alias" or acc.resource is None or acc.action is None:
            continue
        needs = [(acc.resource, acc.action), *acc.also]
        if all(
            scope_allows([scope], a.value, r.value, resource_name="geo-mmm-prod") for r, a in needs
        ):
            got.add(e.operation_id)
    assert got == allowed


def test_webhook_routes_need_webhooks_manage_or_admin() -> None:
    hooks = [e for e in M3_ENDPOINTS if e.path.startswith("/v1/webhooks")]
    assert len(hooks) == 8
    for e in hooks:
        acc = ROUTE_ACCESS[e.operation_id]
        assert acc.resource is not None and acc.action is not None
        assert scope_allows(["webhooks:manage"], acc.action.value, acc.resource.value)
        assert not scope_allows(["runs:create"], acc.action.value, acc.resource.value)


def test_only_admins_reach_webhooks_and_overrides() -> None:
    user = UserPrincipal(
        id="usr_01jaf3a9b8c7d6e5f4g3h2j1k0", issuer="https://accounts.google.com", subject="1"
    )
    for role, ok in ((ProjectRole.RUNNER, False), (ProjectRole.ADMIN, True)):
        ctx = AuthContext(principal=user, tenant_id="acme", project_id="growth", role=role)
        assert ctx.allows(Action.CREATE, "webhook") is ok
        assert ctx.allows(Action.MANAGE, "endpoint") is ok


def test_m3_codes_and_audit_actions_are_stable() -> None:
    for name in ("PROMOTION_BLOCKED", "OVERRIDE_REQUIRED", "WEBHOOK_URL_BLOCKED",
                 "WEBHOOK_SIGNATURE_INVALID", "EMBED_NOT_ALLOWED", "LINK_EXPIRED",
                 "GUIDED_ANSWERS_INVALID", "BUSINESS_PRIORS_INVALID", "CALIBRATION_UNSUPPORTED",
                 "IMPORT_UNSUPPORTED", "SCENARIO_OUT_OF_RANGE", "EMAIL_NOT_ALLOWED",
                 "TERMS_NOT_ACCEPTED", "EMAIL_NOT_VERIFIED",
                 "IDENTITY_VERIFICATION_REQUIRED"):  # fmt: skip
        assert ErrorCode(name).value == name
    assert ERROR_INFO[ErrorCode.PROMOTION_BLOCKED].status == 409
    assert ERROR_INFO[ErrorCode.LINK_EXPIRED].status == 410
    assert ERROR_INFO[ErrorCode.WEBHOOK_SIGNATURE_INVALID].status == 401
    for name in ("endpoint.promote_override", "webhook.url_blocked", "tenant.purged",
                 "plot.embed_create", "auth.signup", "project.guided_lock"):  # fmt: skip
        assert AuditAction(name).value == name


def _docs() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs" / "reference" / "http-api.md").exists():
            return parent / "docs"
    return None


def test_reference_and_concept_docs_cover_m3() -> None:
    docs = _docs()
    if docs is None:  # pragma: no cover - sdist without the docs tree
        pytest.skip("docs/ not available")
    text = (docs / "reference" / "http-api.md").read_text("utf-8")
    for e in M3_ENDPOINTS:
        assert f"`{e.method} {e.path}`" in text, f"http-api.md is missing {e.method} {e.path}"
        for dto in filter(None, (e.request, e.response)):
            assert dto.removeprefix("Page[").removesuffix("]") in text, dto
    guided = (docs / "concepts" / "guided-mode.md").read_text("utf-8")
    from mapi_spec.guided import GUIDED_TEMPLATES
    from mapi_spec.scorecard import SCORECARD_CHECKS

    for template in GUIDED_TEMPLATES:
        assert f"`{template}`" in guided, template
    for check in SCORECARD_CHECKS.values():
        assert f"`{check.id}`" in guided, check.id
        assert f'id="{check.docs_anchor}"' in guided or f"#{check.docs_anchor}" in guided, check.id
    integrations = (docs / "concepts" / "integrations.md").read_text("utf-8")
    for needle in ("Mapi-Signature", "evaluate_then_refit", "override", "platform recipes",
                   "identity verification", "SSRF"):  # fmt: skip
        assert needle.lower() in integrations.lower(), needle
