"""Request builders for ``mapi_spec.api.V016_ENDPOINTS`` (v0.16): client dashboards (drafts,
publishing, grants, share links, inputs, readouts and refit requests) and the share-link
routes (``/v1/share-sessions``, ``/v1/shared/...``).

The share-link routes are public: they are sent without the client's credentials and are
authorised by a share session in ``X-Mapi-Share`` (from :func:`create_share_session`).

Like the other builder modules they validate arguments before any request, encode paths and
return a :class:`~mapi_client._http.Call`.
"""

from __future__ import annotations

from typing import Final, get_args

from mapi_spec.api import (
    API_PREFIX,
    MAX_PREFER_WAIT_SECONDS,
    PREFER_HEADER,
    ActualsSubmit,
    ActualsTemplate,
    ClientDashboard,
    Dashboard,
    DashboardCreate,
    DashboardDraftUpdate,
    DashboardGrant,
    DashboardGrid,
    DashboardRefitRequest,
    DashboardRevision,
    DashboardShare,
    DashboardShareCreate,
    DashboardShareCreated,
    DashboardSummary,
    DashboardValidation,
    Page,
    Readout,
    ReadoutHeader,
    ReadoutList,
    RefitRequestDecision,
    ScenarioSave,
    ShareSession,
    ShareSessionCreate,
    WhatIfRequest,
    WhatIfResult,
)
from mapi_spec.dashboards import (
    SHARE_SESSION_HEADER,
    SHARE_SESSION_RE,
    DashboardStatus,
    ReadoutKind,
    ReadoutVisibility,
    RefitRequestStatus,
)

from mapi_client._calls import _ACCEPTED, _CREATED, _NO_CONTENT, page_params, parse_none
from mapi_client._http import Call, dump, idempotency_headers, parse_model, segment

__all__ = [
    "approve_dashboard_refit_request",
    "archive_dashboard",
    "create_dashboard",
    "create_dashboard_share",
    "create_share_session",
    "delete_dashboard_grant",
    "evaluate_dashboard_whatif",
    "evaluate_shared_whatif",
    "get_dashboard",
    "get_dashboard_actuals_template",
    "get_dashboard_grid",
    "get_dashboard_readout",
    "get_shared_actuals_template",
    "get_shared_dashboard",
    "get_shared_grid",
    "get_shared_readout",
    "hide_dashboard_readout",
    "list_dashboard_grants",
    "list_dashboard_readouts",
    "list_dashboard_refit_requests",
    "list_dashboard_revisions",
    "list_dashboard_shares",
    "list_dashboards",
    "list_shared_readouts",
    "publish_dashboard",
    "publish_dashboard_readout",
    "put_dashboard_grant",
    "readout_params",
    "refresh_dashboard",
    "refresh_share_session",
    "reject_dashboard_refit_request",
    "revoke_dashboard_share",
    "save_dashboard_scenario",
    "save_shared_scenario",
    "submit_dashboard_actuals",
    "submit_shared_actuals",
    "update_dashboard_draft",
    "validate_dashboard",
]

V1: Final = API_PREFIX
READOUT_LIMIT: Final = (1, 50)
_SHARED: Final = f"{V1}/shared"


def _dash(dashboard_id: str) -> str:
    return f"{V1}/dashboards/{segment(dashboard_id)}"


def _check(value: str | None, allowed: tuple[str, ...], name: str) -> None:
    if value is not None and value not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)}")


def _share(session: str) -> dict[str, str]:
    """The ``X-Mapi-Share`` header (a session token from :func:`create_share_session`)."""
    if not SHARE_SESSION_RE.fullmatch(session):
        raise ValueError("not a share session token")
    return {SHARE_SESSION_HEADER: session}


def _with_passcode(body: DashboardShareCreate | ShareSessionCreate) -> dict[str, object]:
    """The JSON body with the passcode revealed (a plain dump masks a ``SecretStr``)."""
    data: dict[str, object] = dump(body)
    if body.passcode is not None:
        data["passcode"] = body.passcode.get_secret_value()
    return data


def _wait(headers: dict[str, str], wait: int | None) -> float | None:
    if wait is None:
        return None
    if not 0 <= wait <= MAX_PREFER_WAIT_SECONDS:
        raise ValueError(f"wait must be between 0 and {MAX_PREFER_WAIT_SECONDS} seconds")
    headers[PREFER_HEADER] = f"wait={wait}"
    return float(wait) if wait else None


def readout_params(
    kind: str | None, before_seq: int | None, limit: int | None
) -> dict[str, str | int]:
    """Query parameters of a readout list (newest first; ``before_seq`` pages back)."""
    _check(kind, get_args(ReadoutKind), "kind")
    if before_seq is not None and before_seq < 1:
        raise ValueError("before_seq must be at least 1")
    if limit is not None and not READOUT_LIMIT[0] <= limit <= READOUT_LIMIT[1]:
        raise ValueError(f"limit must be between {READOUT_LIMIT[0]} and {READOUT_LIMIT[1]}")
    params: dict[str, str | int] = {}
    for key, value in (("kind", kind), ("before_seq", before_seq), ("limit", limit)):
        if value is not None:
            params[key] = value
    return params


# =============================================================================== dashboards


def list_dashboards(
    limit: int | None, cursor: str | None, *, status: str | None = None
) -> Call[Page[DashboardSummary]]:
    _check(status, get_args(DashboardStatus), "status")
    return Call(
        "GET",
        f"{V1}/dashboards",
        parse_model(Page[DashboardSummary]),
        params=page_params(limit, cursor, status=status),
    )


def create_dashboard(body: DashboardCreate) -> Call[Dashboard]:
    return Call("POST", f"{V1}/dashboards", parse_model(Dashboard), _CREATED, json=dump(body))


def get_dashboard(dashboard_id: str) -> Call[Dashboard]:
    # members get the full dashboard (a client user's view is ClientDashboard)
    return Call("GET", _dash(dashboard_id), parse_model(Dashboard))


def update_dashboard_draft(
    dashboard_id: str, body: DashboardDraftUpdate, etag: str
) -> Call[Dashboard]:
    # If-Match: the draft's ETag (412 when another edit landed first)
    if not etag:
        raise ValueError("etag is required")
    return Call(
        "PUT",
        f"{_dash(dashboard_id)}/draft",
        parse_model(Dashboard),
        json=dump(body),
        headers={"If-Match": etag},
    )


def validate_dashboard(dashboard_id: str) -> Call[DashboardValidation]:
    return Call("POST", f"{_dash(dashboard_id)}:validate", parse_model(DashboardValidation))


def publish_dashboard(dashboard_id: str) -> Call[Dashboard]:
    return Call("POST", f"{_dash(dashboard_id)}:publish", parse_model(Dashboard))


def archive_dashboard(dashboard_id: str) -> Call[Dashboard]:
    return Call("POST", f"{_dash(dashboard_id)}:archive", parse_model(Dashboard), retry_safe=True)


def list_dashboard_revisions(
    dashboard_id: str, limit: int | None, cursor: str | None
) -> Call[Page[DashboardRevision]]:
    return Call(
        "GET",
        f"{_dash(dashboard_id)}/revisions",
        parse_model(Page[DashboardRevision]),
        params=page_params(limit, cursor),
    )


# ----------------------------------------------------------------------------------- grants


def list_dashboard_grants(
    dashboard_id: str, limit: int | None, cursor: str | None
) -> Call[Page[DashboardGrant]]:
    return Call(
        "GET",
        f"{_dash(dashboard_id)}/grants",
        parse_model(Page[DashboardGrant]),
        params=page_params(limit, cursor),
    )


def put_dashboard_grant(dashboard_id: str, user_id: str) -> Call[DashboardGrant]:
    # the user must already hold a project membership
    return Call(
        "PUT", f"{_dash(dashboard_id)}/grants/{segment(user_id)}", parse_model(DashboardGrant)
    )


def delete_dashboard_grant(dashboard_id: str, user_id: str) -> Call[None]:
    return Call(
        "DELETE", f"{_dash(dashboard_id)}/grants/{segment(user_id)}", parse_none, _NO_CONTENT
    )


# ----------------------------------------------------------------------------------- shares


def list_dashboard_shares(
    dashboard_id: str, limit: int | None, cursor: str | None
) -> Call[Page[DashboardShare]]:
    return Call(
        "GET",
        f"{_dash(dashboard_id)}/shares",
        parse_model(Page[DashboardShare]),
        params=page_params(limit, cursor),
    )


def create_dashboard_share(
    dashboard_id: str, body: DashboardShareCreate
) -> Call[DashboardShareCreated]:
    # the link's token is shown once; a lost response is not resent (it would mint another)
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/shares",
        parse_model(DashboardShareCreated),
        _CREATED,
        json=_with_passcode(body),
    )


def revoke_dashboard_share(dashboard_id: str, share_id: str) -> Call[DashboardShare]:
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/shares/{segment(share_id)}:revoke",
        parse_model(DashboardShare),
        retry_safe=True,
    )


# ----------------------------------------------------------------------------------- inputs


def get_dashboard_grid(dashboard_id: str) -> Call[DashboardGrid]:
    return Call("GET", f"{_dash(dashboard_id)}/grid", parse_model(DashboardGrid))


def evaluate_dashboard_whatif(dashboard_id: str, body: WhatIfRequest) -> Call[WhatIfResult]:
    # evaluated on the grid: no run, so safe to resend
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/whatif:evaluate",
        parse_model(WhatIfResult),
        json=dump(body),
        retry_safe=True,
    )


def save_dashboard_scenario(
    dashboard_id: str,
    body: ScenarioSave,
    *,
    wait: int | None = None,
    idempotency_key: str | None = None,
) -> Call[Readout]:
    headers = idempotency_headers(idempotency_key)
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/scenarios",
        parse_model(Readout),
        json=dump(body),
        headers=headers,
        server_wait=_wait(headers, wait),
    )


def get_dashboard_actuals_template(dashboard_id: str) -> Call[ActualsTemplate]:
    return Call("GET", f"{_dash(dashboard_id)}/actuals/template", parse_model(ActualsTemplate))


def submit_dashboard_actuals(
    dashboard_id: str, body: ActualsSubmit, *, idempotency_key: str | None = None
) -> Call[ReadoutHeader]:
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/actuals",
        parse_model(ReadoutHeader),
        _ACCEPTED,
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def refresh_dashboard(
    dashboard_id: str, *, idempotency_key: str | None = None
) -> Call[ReadoutHeader]:
    # "Refresh now" (debounced server-side)
    return Call(
        "POST",
        f"{_dash(dashboard_id)}:refresh",
        parse_model(ReadoutHeader),
        _ACCEPTED,
        headers=idempotency_headers(idempotency_key),
    )


# --------------------------------------------------------------------------------- readouts


def list_dashboard_readouts(
    dashboard_id: str,
    *,
    kind: str | None = None,
    before_seq: int | None = None,
    limit: int | None = None,
    visibility: str | None = None,
) -> Call[ReadoutList]:
    _check(visibility, get_args(ReadoutVisibility), "visibility")
    params = readout_params(kind, before_seq, limit)
    if visibility is not None:
        params["visibility"] = visibility
    return Call("GET", f"{_dash(dashboard_id)}/readouts", parse_model(ReadoutList), params=params)


def get_dashboard_readout(dashboard_id: str, seq: int) -> Call[Readout]:
    return Call("GET", f"{_dash(dashboard_id)}/readouts/{segment(seq)}", parse_model(Readout))


def publish_dashboard_readout(dashboard_id: str, seq: int) -> Call[ReadoutHeader]:
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/readouts/{segment(seq)}:publish",
        parse_model(ReadoutHeader),
        retry_safe=True,
    )


def hide_dashboard_readout(dashboard_id: str, seq: int) -> Call[ReadoutHeader]:
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/readouts/{segment(seq)}:hide",
        parse_model(ReadoutHeader),
        retry_safe=True,
    )


# ---------------------------------------------------------------------------- refit requests


def list_dashboard_refit_requests(
    dashboard_id: str, limit: int | None, cursor: str | None, *, status: str | None = None
) -> Call[Page[DashboardRefitRequest]]:
    _check(status, get_args(RefitRequestStatus), "status")
    return Call(
        "GET",
        f"{_dash(dashboard_id)}/refit-requests",
        parse_model(Page[DashboardRefitRequest]),
        params=page_params(limit, cursor, status=status),
    )


def approve_dashboard_refit_request(
    dashboard_id: str,
    request_id: str,
    body: RefitRequestDecision,
    *,
    idempotency_key: str | None = None,
) -> Call[DashboardRefitRequest]:
    # needs runner: approval ingests the actuals and starts the refit workflow
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/refit-requests/{segment(request_id)}:approve",
        parse_model(DashboardRefitRequest),
        json=dump(body),
        headers=idempotency_headers(idempotency_key),
    )


def reject_dashboard_refit_request(
    dashboard_id: str, request_id: str, body: RefitRequestDecision
) -> Call[DashboardRefitRequest]:
    return Call(
        "POST",
        f"{_dash(dashboard_id)}/refit-requests/{segment(request_id)}:reject",
        parse_model(DashboardRefitRequest),
        json=dump(body),
        retry_safe=True,
    )


# ============================================================== share links (public routes)


def create_share_session(body: ShareSessionCreate) -> Call[ShareSession]:
    # exchanges the link's token (and passcode) for a short-lived session
    return Call(
        "POST",
        f"{V1}/share-sessions",
        parse_model(ShareSession),
        json=_with_passcode(body),
        scoped=False,
        public=True,
    )


def refresh_share_session(session: str) -> Call[ShareSession]:
    return Call(
        "POST",
        f"{V1}/share-sessions:refresh",
        parse_model(ShareSession),
        headers=_share(session),
        scoped=False,
        public=True,
    )


def get_shared_dashboard(session: str) -> Call[ClientDashboard]:
    return Call(
        "GET",
        f"{_SHARED}/dashboard",
        parse_model(ClientDashboard),
        headers=_share(session),
        scoped=False,
        public=True,
    )


def list_shared_readouts(
    session: str,
    *,
    kind: str | None = None,
    before_seq: int | None = None,
    limit: int | None = None,
) -> Call[ReadoutList]:
    return Call(
        "GET",
        f"{_SHARED}/readouts",
        parse_model(ReadoutList),
        params=readout_params(kind, before_seq, limit),
        headers=_share(session),
        scoped=False,
        public=True,
    )


def get_shared_readout(session: str, seq: int) -> Call[Readout]:
    return Call(
        "GET",
        f"{_SHARED}/readouts/{segment(seq)}",
        parse_model(Readout),
        headers=_share(session),
        scoped=False,
        public=True,
    )


def get_shared_grid(session: str) -> Call[DashboardGrid]:
    return Call(
        "GET",
        f"{_SHARED}/grid",
        parse_model(DashboardGrid),
        headers=_share(session),
        scoped=False,
        public=True,
    )


def evaluate_shared_whatif(session: str, body: WhatIfRequest) -> Call[WhatIfResult]:
    return Call(
        "POST",
        f"{_SHARED}/whatif:evaluate",
        parse_model(WhatIfResult),
        json=dump(body),
        headers=_share(session),
        retry_safe=True,
        scoped=False,
        public=True,
    )


def save_shared_scenario(
    session: str,
    body: ScenarioSave,
    *,
    wait: int | None = None,
    idempotency_key: str | None = None,
) -> Call[Readout]:
    headers = {**_share(session), **idempotency_headers(idempotency_key)}
    return Call(
        "POST",
        f"{_SHARED}/scenarios",
        parse_model(Readout),
        json=dump(body),
        headers=headers,
        server_wait=_wait(headers, wait),
        scoped=False,
        public=True,
    )


def get_shared_actuals_template(session: str) -> Call[ActualsTemplate]:
    return Call(
        "GET",
        f"{_SHARED}/actuals/template",
        parse_model(ActualsTemplate),
        headers=_share(session),
        scoped=False,
        public=True,
    )


def submit_shared_actuals(
    session: str, body: ActualsSubmit, *, idempotency_key: str | None = None
) -> Call[ReadoutHeader]:
    return Call(
        "POST",
        f"{_SHARED}/actuals",
        parse_model(ReadoutHeader),
        _ACCEPTED,
        json=dump(body),
        headers={**_share(session), **idempotency_headers(idempotency_key)},
        scoped=False,
        public=True,
    )
