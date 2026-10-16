"""Request builders for the M3 rows of ``mapi_spec.api.ENDPOINTS`` (guided mode and
integrations, v0.4): one pure function per operation (operation ids in comments).

Like :mod:`mapi_client._calls` they validate arguments, encode paths and return a
:class:`~mapi_client._http.Call`; ``tests/test_endpoint_coverage.py`` checks every one of them
against the contract (method, path, statuses, body, query, headers and project scoping).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from mapi_spec.api import (
    API_PREFIX,
    MAX_PREFER_WAIT_SECONDS,
    PREFER_HEADER,
    BayesInsightImport,
    DetectRolesRequest,
    EmailVerify,
    EmbedView,
    Endpoint,
    EndpointPromote,
    EndpointRevision,
    EndpointRollback,
    EndpointSchema,
    EndpointScoreCall,
    EndpointValidation,
    GdprExport,
    GdprExportRequest,
    GuidedAnswers,
    GuidedCompile,
    GuidedDraft,
    GuidedTemplate,
    Page,
    PlotEmbed,
    PlotEmbedCreate,
    PlotEmbedCreated,
    PlotRender,
    PlotRenderRequest,
    PriorTranslation,
    PriorTranslationRequest,
    RoleSuggestions,
    SavedPlot,
    SavedPlotCreate,
    ScoreCallResult,
    SignupAccepted,
    SignupRequest,
    SignupReview,
    SignupReviewDecision,
    SnippetSet,
    TenantDeletion,
    TenantDeletionRequest,
    VerificationResult,
    VerificationStatus,
    VerificationUpdate,
    Webhook,
    WebhookCreate,
    WebhookCreated,
    WebhookDelivery,
    WebhookUpdate,
)
from mapi_spec.snippets import SnippetLanguage
from mapi_spec.spec import ScoreRequest
from mapi_spec.trust import SignupReviewState
from mapi_spec.webhooks import DeliveryStatus

from mapi_client._calls import page_params, parse_none
from mapi_client._http import Call, dump, idempotency_headers, parse_model, segment

V1: Final = API_PREFIX
MAX_WAIT: Final = MAX_PREFER_WAIT_SECONDS

_CREATED: Final = frozenset({201})
_ACCEPTED: Final = frozenset({202})
_ACCEPTED_OR_REPLAY: Final = frozenset({200, 202})
_NO_CONTENT: Final = frozenset({204})


def prefer_wait_headers(wait: int | None, idempotency_key: str | None) -> dict[str, str]:
    """``Idempotency-Key`` (given or fresh) and ``Prefer: wait=N`` (``0 <= N <= 60``)."""
    headers = idempotency_headers(idempotency_key)
    if wait is not None:
        if not 0 <= wait <= MAX_WAIT:
            raise ValueError(f"wait must be between 0 and {MAX_WAIT} seconds")
        headers[PREFER_HEADER] = f"wait={wait}"
    return headers


# ================================================================================== guided


def list_guided_templates(limit: int | None, cursor: str | None) -> Call[Page[GuidedTemplate]]:
    return Call(
        "GET",
        f"{V1}/guided/templates",
        parse_model(Page[GuidedTemplate]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def get_guided_template(template_id: str) -> Call[GuidedTemplate]:
    path = f"{V1}/guided/templates/{segment(template_id)}"
    return Call("GET", path, parse_model(GuidedTemplate), scoped=False)


def detect_roles(body: DetectRolesRequest) -> Call[RoleSuggestions]:
    return Call("POST", f"{V1}/guided/detect-roles", parse_model(RoleSuggestions), json=dump(body))


def create_guided_draft(body: GuidedAnswers) -> Call[GuidedDraft]:
    # stores nothing: safe to resend
    return Call(
        "POST",
        f"{V1}/guided/drafts",
        parse_model(GuidedDraft),
        json=dump(body),
        retry_safe=True,
    )


def import_bayesinsight(body: BayesInsightImport) -> Call[GuidedDraft]:
    return Call(
        "POST",
        f"{V1}/guided/import/bayesinsight",
        parse_model(GuidedDraft),
        json=dump(body),
        retry_safe=True,
    )


def compile_guided_request(body: GuidedCompile) -> Call[ScoreRequest]:
    return Call(
        "POST",
        f"{V1}/guided/requests:compile",
        parse_model(ScoreRequest),
        json=body.model_dump(mode="json", by_alias=True, exclude_none=True),
        retry_safe=True,
    )


def create_prior_translation(
    body: PriorTranslationRequest, idempotency_key: str | None
) -> Call[PriorTranslation]:
    return Call(
        "POST",
        f"{V1}/guided/prior-translations",
        parse_model(PriorTranslation),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def get_prior_translation(run_id: str) -> Call[PriorTranslation]:
    path = f"{V1}/guided/prior-translations/{segment(run_id)}"
    return Call("GET", path, parse_model(PriorTranslation))


# =============================================================================== endpoints


def _alias(alias: str) -> str:
    return f"{V1}/endpoints/{segment(alias)}"


def list_endpoints(limit: int | None, cursor: str | None) -> Call[Page[Endpoint]]:
    return Call(
        "GET", f"{V1}/endpoints", parse_model(Page[Endpoint]), params=page_params(limit, cursor)
    )


def get_endpoint(alias: str) -> Call[Endpoint]:
    return Call("GET", _alias(alias), parse_model(Endpoint))


def promote_endpoint(alias: str, body: EndpointPromote) -> Call[Endpoint]:
    return Call("PUT", _alias(alias), parse_model(Endpoint), json=dump(body))


def delete_endpoint(alias: str) -> Call[None]:
    return Call("DELETE", _alias(alias), parse_none, _NO_CONTENT)


def list_endpoint_history(
    alias: str, limit: int | None, cursor: str | None
) -> Call[Page[EndpointRevision]]:
    return Call(
        "GET",
        f"{_alias(alias)}/history",
        parse_model(Page[EndpointRevision]),
        params=page_params(limit, cursor),
    )


def rollback_endpoint(alias: str, body: EndpointRollback) -> Call[Endpoint]:
    return Call("POST", f"{_alias(alias)}/rollback", parse_model(Endpoint), json=dump(body))


def get_endpoint_schema(alias: str) -> Call[EndpointSchema]:
    return Call("GET", f"{_alias(alias)}/schema", parse_model(EndpointSchema))


def validate_endpoint_request(alias: str, body: EndpointScoreCall) -> Call[EndpointValidation]:
    # static checks only: safe to resend
    return Call(
        "POST",
        f"{_alias(alias)}/validate",
        parse_model(EndpointValidation),
        json=dump(body),
        retry_safe=True,
    )


def score_endpoint(
    alias: str, body: EndpointScoreCall, *, wait: int | None, idempotency_key: str
) -> Call[ScoreCallResult]:
    return Call(
        "POST",
        f"{_alias(alias)}/score",
        parse_model(ScoreCallResult),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=prefer_wait_headers(wait, idempotency_key),
        server_wait=float(wait) if wait else None,
    )


def list_endpoint_snippets(
    alias: str,
    languages: Sequence[SnippetLanguage | str] | None = None,
    base_url: str | None = None,
) -> Call[SnippetSet]:
    params: dict[str, str | int | list[str]] = {}
    if languages:
        params["language"] = [SnippetLanguage(x).value for x in languages]
    if base_url is not None:
        params["base_url"] = base_url
    return Call("GET", f"{_alias(alias)}/snippets", parse_model(SnippetSet), params=params)


# ================================================================================ webhooks


def _webhook(webhook_id: str) -> str:
    return f"{V1}/webhooks/{segment(webhook_id)}"


def list_webhooks(limit: int | None, cursor: str | None) -> Call[Page[Webhook]]:
    return Call(
        "GET", f"{V1}/webhooks", parse_model(Page[Webhook]), params=page_params(limit, cursor)
    )


def create_webhook(body: WebhookCreate) -> Call[WebhookCreated]:
    # not resent after a lost response: that would create a second webhook (and secret)
    return Call("POST", f"{V1}/webhooks", parse_model(WebhookCreated), _CREATED, json=dump(body))


def get_webhook(webhook_id: str) -> Call[Webhook]:
    return Call("GET", _webhook(webhook_id), parse_model(Webhook))


def update_webhook(webhook_id: str, body: WebhookUpdate) -> Call[Webhook]:
    return Call(
        "PATCH",
        _webhook(webhook_id),
        parse_model(Webhook),
        json=body.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )


def delete_webhook(webhook_id: str) -> Call[None]:
    return Call("DELETE", _webhook(webhook_id), parse_none, _NO_CONTENT)


def rotate_webhook_secret(webhook_id: str) -> Call[WebhookCreated]:
    # never resent: a second rotation would retire the secret the first one returned
    return Call("POST", f"{_webhook(webhook_id)}:rotate-secret", parse_model(WebhookCreated))


def list_webhook_deliveries(
    webhook_id: str,
    limit: int | None,
    cursor: str | None,
    *,
    status: DeliveryStatus | str | None = None,
) -> Call[Page[WebhookDelivery]]:
    params = page_params(
        limit, cursor, status=DeliveryStatus(status).value if status is not None else None
    )
    return Call(
        "GET",
        f"{_webhook(webhook_id)}/deliveries",
        parse_model(Page[WebhookDelivery]),
        params=params,
    )


def replay_webhook_delivery(webhook_id: str, delivery_id: str) -> Call[WebhookDelivery]:
    path = f"{_webhook(webhook_id)}/deliveries/{segment(delivery_id)}:replay"
    return Call("POST", path, parse_model(WebhookDelivery), _ACCEPTED)


# =================================================================== plots: saving, renders


def _saved(plot_id: str) -> str:
    return f"{V1}/plots/{segment(plot_id)}"


def create_saved_plot(body: SavedPlotCreate) -> Call[SavedPlot]:
    return Call("POST", f"{V1}/plots", parse_model(SavedPlot), _CREATED, json=dump(body))


def list_saved_plots(limit: int | None, cursor: str | None) -> Call[Page[SavedPlot]]:
    return Call(
        "GET", f"{V1}/plots", parse_model(Page[SavedPlot]), params=page_params(limit, cursor)
    )


def get_saved_plot(plot_id: str) -> Call[SavedPlot]:
    return Call("GET", _saved(plot_id), parse_model(SavedPlot))


def delete_saved_plot(plot_id: str) -> Call[None]:
    return Call("DELETE", _saved(plot_id), parse_none, _NO_CONTENT)


def render_plot(
    body: PlotRenderRequest, *, wait: int | None, idempotency_key: str
) -> Call[PlotRender]:
    return Call(
        "POST",
        f"{V1}/plots:render",
        parse_model(PlotRender),
        _ACCEPTED_OR_REPLAY,
        json=dump(body),
        headers=prefer_wait_headers(wait, idempotency_key),
        server_wait=float(wait) if wait else None,
    )


def get_plot_render(run_id: str) -> Call[PlotRender]:
    return Call("GET", f"{V1}/plots/renders/{segment(run_id)}", parse_model(PlotRender))


def create_plot_embed(plot_id: str, body: PlotEmbedCreate) -> Call[PlotEmbedCreated]:
    # not resent: a second link (and token) would be minted
    return Call(
        "POST",
        f"{_saved(plot_id)}/embeds",
        parse_model(PlotEmbedCreated),
        _CREATED,
        json=dump(body),
    )


def list_plot_embeds(plot_id: str, limit: int | None, cursor: str | None) -> Call[Page[PlotEmbed]]:
    return Call(
        "GET",
        f"{_saved(plot_id)}/embeds",
        parse_model(Page[PlotEmbed]),
        params=page_params(limit, cursor),
    )


def revoke_plot_embed(plot_id: str, embed_id: str) -> Call[None]:  # idempotent
    path = f"{_saved(plot_id)}/embeds/{segment(embed_id)}"
    return Call("DELETE", path, parse_none, _NO_CONTENT)


def get_embed(embed_token: str) -> Call[EmbedView]:
    """The public embed view: no credentials and no project are sent."""
    return Call(
        "GET",
        f"{V1}/embeds/{segment(embed_token)}",
        parse_model(EmbedView),
        scoped=False,
        public=True,
    )


# ========================================================================= trust lifecycle


def signup(body: SignupRequest) -> Call[SignupAccepted]:
    # public; never resent (each try sends another e-mail)
    return Call(
        "POST",
        "/auth/signup",
        parse_model(SignupAccepted),
        _ACCEPTED,
        json=dump(body),
        scoped=False,
        public=True,
    )


def verify_email(body: EmailVerify) -> Call[VerificationResult]:
    # public; the token is single-use, so a resend could only fail
    return Call(
        "POST",
        "/auth/verify-email",
        parse_model(VerificationResult),
        json=dump(body),
        scoped=False,
        public=True,
    )


def _tenant(tenant_id: str) -> str:
    return f"{V1}/tenants/{segment(tenant_id)}"


def get_tenant_verification(tenant_id: str) -> Call[VerificationStatus]:
    path = f"{_tenant(tenant_id)}/verification"
    return Call("GET", path, parse_model(VerificationStatus), scoped=False)


def admin_put_tenant_verification(
    tenant_id: str, body: VerificationUpdate
) -> Call[VerificationStatus]:
    return Call(
        "PUT",
        f"{V1}/admin/tenants/{segment(tenant_id)}/verification",
        parse_model(VerificationStatus),
        json=dump(body),
        scoped=False,
    )


def request_tenant_deletion(tenant_id: str, body: TenantDeletionRequest) -> Call[TenantDeletion]:
    return Call(
        "POST",
        f"{_tenant(tenant_id)}/deletion",
        parse_model(TenantDeletion),
        _ACCEPTED,
        json=dump(body),
        scoped=False,
    )


def get_tenant_deletion(tenant_id: str) -> Call[TenantDeletion]:
    path = f"{_tenant(tenant_id)}/deletion"
    return Call("GET", path, parse_model(TenantDeletion), scoped=False)


def cancel_tenant_deletion(tenant_id: str) -> Call[TenantDeletion]:
    path = f"{_tenant(tenant_id)}/deletion"
    return Call("DELETE", path, parse_model(TenantDeletion), scoped=False)


def create_tenant_export(tenant_id: str, body: GdprExportRequest) -> Call[GdprExport]:
    return Call(
        "POST",
        f"{_tenant(tenant_id)}/exports",
        parse_model(GdprExport),
        _ACCEPTED,
        json=dump(body),
        scoped=False,
    )


def list_tenant_exports(
    tenant_id: str, limit: int | None, cursor: str | None
) -> Call[Page[GdprExport]]:
    return Call(
        "GET",
        f"{_tenant(tenant_id)}/exports",
        parse_model(Page[GdprExport]),
        params=page_params(limit, cursor),
        scoped=False,
    )


def get_tenant_export(tenant_id: str, export_id: str) -> Call[GdprExport]:
    path = f"{_tenant(tenant_id)}/exports/{segment(export_id)}"
    return Call("GET", path, parse_model(GdprExport), scoped=False)


def admin_list_signups(
    limit: int | None,
    cursor: str | None,
    *,
    review: SignupReviewState | str | None = None,
    tenant_id: str | None = None,
) -> Call[Page[SignupReview]]:
    params = page_params(
        limit,
        cursor,
        review=SignupReviewState(review).value if review is not None else None,
        tenant_id=tenant_id,
    )
    return Call(
        "GET",
        f"{V1}/admin/signups",
        parse_model(Page[SignupReview]),
        params=params,
        scoped=False,
    )


def admin_review_signup(signup_id: str, body: SignupReviewDecision) -> Call[SignupReview]:
    return Call(
        "POST",
        f"{V1}/admin/signups/{segment(signup_id)}/review",
        parse_model(SignupReview),
        json=dump(body),
        scoped=False,
    )


def admin_retry_tenant_deletion(tenant_id: str) -> Call[TenantDeletion]:
    return Call(
        "POST",
        f"{V1}/admin/tenants/{segment(tenant_id)}/deletion/retry",
        parse_model(TenantDeletion),
        _ACCEPTED,
        scoped=False,
    )
