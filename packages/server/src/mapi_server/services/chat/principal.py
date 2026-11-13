"""Who is chatting, checked again before every step (v0.20, plan D1 and D9).

A :class:`ChatPrincipal` is built from the caller's credential by the chat routes, never from
anything the model says:

* :meth:`ChatPrincipal.for_member` — a signed-in member or client user (web session);
* :meth:`ChatPrincipal.for_share` — a share-link session (``X-Mapi-Share``).

:meth:`ChatPrincipal.recheck` returns a fresh
:class:`~mapi_server.services.dashboards.access.DashboardContext` and runs before every model
call and every tool call of a turn, so revoking a link, ending a session, removing a grant or
a membership, or unpublishing the dashboard stops the turn at the next step with the
credential's own error (``401``, ``404``, ``410 LINK_EXPIRED``, ``409
DASHBOARD_NOT_PUBLISHED``). For a share session only the first check of a turn counts against
the link's request limits (``count=False`` after it): one turn makes up to 14 checks.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from mapi_spec.errors import ErrorCode, MapiError

from mapi_server.auth.request import RequestAuth, still_authorized
from mapi_server.errors import NotFoundError, unauthenticated
from mapi_server.services.dashboards.access import (
    DashboardContext,
    caller_is_client,
    member_context,
)
from mapi_server.services.dashboards.shares import _session_claims, resolve_session
from mapi_server.services.runs import now

if TYPE_CHECKING:
    from starlette.requests import Request

    from mapi_server.context import ServerContext

__all__ = ["ChatPrincipal", "ChatPrincipalKind", "ip_key"]

ChatPrincipalKind = Literal["member", "client", "share"]


def ip_key(ctx: ServerContext, client_ip: str | None) -> str:
    """A keyed 16-character digest of a client address (rate-limit subjects; never the
    address itself)."""
    digest = hmac.new(
        ctx.secrets.audit_key, b"chat-ip:" + (client_ip or "-").encode(), hashlib.sha256
    ).hexdigest()
    return digest[:16]


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    auth = getattr(request.state, "auth", None)
    if isinstance(auth, RequestAuth) and auth.client_ip:
        return auth.client_ip
    return request.client.host if request.client is not None else None


@dataclass
class ChatPrincipal:
    """The caller of one chat turn.

    ``kind`` and ``id`` (user id, or share id for share links) bind signed history, budgets
    and transcripts; ``share_id`` is set for share links; ``ip_key`` is the keyed digest of the
    client address for share links (the per-address turn limit)."""

    kind: ChatPrincipalKind
    id: str
    share_id: str | None
    ip_key: str | None
    dashboard_id: str | None = None
    """The dashboard of the credential: the route's for members, the link's for share
    sessions (known after the first :meth:`recheck`)."""
    _ctx: ServerContext | None = field(default=None, repr=False)
    _request: Request | None = field(default=None, repr=False)
    _auth: RequestAuth | None = field(default=None, repr=False)
    _session: str | None = field(default=None, repr=False)
    _client_ip: str | None = field(default=None, repr=False)
    _checks: int = field(default=0, repr=False)

    def __repr__(self) -> str:  # never the session token
        return f"ChatPrincipal(kind={self.kind!r}, id={self.id!r})"

    # ----------------------------------------------------------------------- constructors

    @classmethod
    def for_member(
        cls,
        ctx: ServerContext,
        request: Request | None,
        auth: RequestAuth,
        dashboard_id: str,
    ) -> ChatPrincipal:
        """A signed-in member or client user on ``dashboard_id``. ``request`` is the HTTP
        request whose credential :meth:`recheck` checks again (``None`` only for in-process
        callers that have no request; then only the dashboard access is checked again)."""
        user = auth.user_id or auth.principal_id
        if auth.context is None or auth.tenant_id is None or not user:
            raise unauthenticated()
        rctx = ctx if ctx.request is auth else ctx.for_request(auth)
        return cls(
            kind="client" if caller_is_client(auth) else "member",
            id=user,
            share_id=None,
            ip_key=None,
            dashboard_id=dashboard_id,
            _ctx=rctx,
            _request=request,
            _auth=auth,
        )

    @classmethod
    def for_share(
        cls, ctx: ServerContext, request: Request | None, session_token: str | None
    ) -> ChatPrincipal:
        """A share-link session (the ``X-Mapi-Share`` value): ``401`` for a missing, forged
        or expired session. The link itself is checked by the first :meth:`recheck` (the
        one that counts against the link's limits)."""
        share_id, _ = _session_claims(ctx, session_token, now())
        client_ip = _client_ip(request)
        return cls(
            kind="share",
            id=share_id,
            share_id=share_id,
            ip_key=ip_key(ctx, client_ip),
            _ctx=ctx,
            _request=request,
            _session=session_token,
            _client_ip=client_ip,
        )

    # ------------------------------------------------------------------------------ check

    @property
    def checks(self) -> int:
        """How many times the credential was checked (tests and counters)."""
        return self._checks

    def mark_counted(self) -> None:
        """The route already made the counted share-session check of this request: every
        :meth:`recheck` then skips the link's request limits."""
        self._checks = max(self._checks, 1)

    async def recheck(self) -> DashboardContext:
        """Check the credential again and return a fresh dashboard context; raises the
        credential's own error when access has ended or changed."""
        if self._ctx is None:
            raise unauthenticated()
        first = self._checks == 0
        self._checks += 1
        if self.kind == "share":
            dctx = await resolve_session(self._ctx, self._session, self._client_ip, count=first)
            if dctx.principal_kind != "share" or dctx.principal_id != self.share_id:
                raise unauthenticated("the share session has ended; open the link again")
            if self.dashboard_id is None:
                self.dashboard_id = dctx.dashboard_id
            elif dctx.dashboard_id != self.dashboard_id:
                raise unauthenticated("the share session has ended; open the link again")
            return dctx
        auth = self._auth
        assert auth is not None and self.dashboard_id is not None
        if self._request is not None and not await still_authorized(self._request, auth):
            raise unauthenticated("your session has ended; sign in again")
        dctx = await member_context(self._ctx, auth, self.dashboard_id)
        if dctx.principal_kind != self.kind or dctx.principal_id != self.id:
            raise MapiError(
                "your access to this dashboard changed: open the chat again",
                code=ErrorCode.FORBIDDEN,
            )
        if dctx.dashboard_id != self.dashboard_id:  # pragma: no cover - member_context keys it
            raise NotFoundError("dashboard", self.dashboard_id)
        return dctx
