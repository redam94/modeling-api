"""Client dashboards (v0.16): visibility and DTOs (:mod:`.access`), drafts, publishing and
grants (:mod:`.crud`), share links and sessions (:mod:`.shares`).

The inputs, readouts, the dashboard loop and the read-only tool surface live in their own
modules of this package (``runas``, ``inputs``, ``readouts``, ``loop``, ``tools``).
"""

from __future__ import annotations

from mapi_server.services.dashboards.access import (
    DashboardContext,
    caller_is_client,
    client_dashboard,
    dashboard_dto,
    effective_inputs,
    load_dashboard,
    load_revision,
    member_context,
    readout_dto,
    readout_header,
)
from mapi_server.services.dashboards.crud import (
    archive,
    create_dashboard,
    delete_grant,
    publish,
    put_grant,
    read_grid,
    update_draft,
    validate_draft,
)
from mapi_server.services.dashboards.shares import (
    DashboardShareRevoker,
    create_share,
    open_session,
    refresh_session,
    resolve_session,
    resolve_shared,
    revoke_share,
    share_token_hash,
    shared_dashboard,
)

__all__ = [
    "DashboardContext",
    "DashboardShareRevoker",
    "archive",
    "caller_is_client",
    "client_dashboard",
    "create_dashboard",
    "create_share",
    "dashboard_dto",
    "delete_grant",
    "effective_inputs",
    "load_dashboard",
    "load_revision",
    "member_context",
    "open_session",
    "publish",
    "put_grant",
    "read_grid",
    "readout_dto",
    "readout_header",
    "refresh_session",
    "resolve_session",
    "resolve_shared",
    "revoke_share",
    "share_token_hash",
    "shared_dashboard",
    "update_draft",
    "validate_draft",
]
