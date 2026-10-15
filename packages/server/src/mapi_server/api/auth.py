"""Browser sign-in (BFF): ``/auth/login``, ``/auth/callback``, ``/auth/logout``, ``/auth/session``.

The SPA never holds an OAuth token. ``/auth/login`` stores a :class:`LoginTransaction`
(state, nonce, PKCE verifier, return path) server-side and redirects to the identity
provider; ``/auth/callback`` consumes it exactly once, checks ``state`` in constant time,
exchanges the code with the verifier, verifies the ID token (:mod:`mapi_server.auth.oidc`)
and starts a server-side session:

* session cookie ``__Host-mapi_session`` — ``HttpOnly; Secure; SameSite=Strict; Path=/``
  (``mapi_session`` without ``Secure`` on plain-HTTP development installs);
* CSRF cookie ``__Host-mapi_csrf`` — readable by the SPA, which echoes it in
  ``X-CSRF-Token`` on every state-changing request (also returned by ``/auth/session``).

Sign-in failures answer ``401`` with a generic detail; the reason goes to the audit log.
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Annotated

import httpx
from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from mapi_spec.audit import AuditAction, AuditOutcome, client_fingerprint, make_audit_event
from mapi_spec.auth import SESSION_IDLE_TTL, CookieSpec, LoginTransaction, SessionInfo
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.tenancy import PrincipalKind, ResourceKind
from sqlalchemy.ext.asyncio import AsyncSession

from mapi_server.auth.identity import (
    AccountDisabled,
    claim_or_create_user,
    create_session,
    delete_session,
    load_session,
    local_principal,
    pop_login,
    save_login,
    user_principal,
)
from mapi_server.auth.oidc import OidcError
from mapi_server.auth.request import CSRF_HEADER, _check_csrf, csrf_cookie, session_cookie
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import problem_response
from mapi_server.services.audit import write_audit
from mapi_server.services.runs import now

__all__ = ["router"]

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_ERROR_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _ctx(request: Request) -> ServerContext:
    ctx: ServerContext = request.app.state.ctx
    return ctx


def _client(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None, request.headers.get("user-agent"))


async def _login_failed(ctx: ServerContext, request: Request, reason: str) -> JSONResponse:
    ip, ua = _client(request)
    await write_audit(
        ctx.db,
        [
            make_audit_event(
                action=AuditAction.AUTH_LOGIN_FAILED,
                resource_kind=ResourceKind.TENANT,
                outcome=AuditOutcome.DENIED,
                ts=now(),
                fingerprint_key=ctx.secrets.audit_key,
                code=ErrorCode.UNAUTHENTICATED,
                client_ip=ip,
                user_agent=ua,
                details={"reason": reason[:200]},
            )
        ],
    )
    log.info("sign-in failed: %s", reason)
    problem = Problem.from_code(
        ErrorCode.UNAUTHENTICATED, "sign-in failed; start again", instance=request.url.path
    )
    resp = problem_response(problem)
    resp.headers.append("set-cookie", CookieSpec.login(secure=ctx.settings.secure_cookies).clear())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get(
    "/login",
    operation_id="auth_login",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={302: {"description": "Redirect to the identity provider"}},
)
async def auth_login(
    request: Request,
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
) -> Response:
    """Start sign-in: Authorization Code + PKCE (S256) + nonce, state kept server-side."""
    ctx = _ctx(request)
    if ctx.oidc is None:
        raise MapiError("sign-in is not configured on this server", code=ErrorCode.NOT_FOUND)
    tx = LoginTransaction.start(now=now(), return_to=return_to)
    url = await ctx.oidc.authorize_url(tx)
    async with ctx.db.session() as s:
        value = await save_login(s, tx)
    resp = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    resp.headers.append(
        "set-cookie", CookieSpec.login(secure=ctx.settings.secure_cookies).header(value)
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def _start_session(
    ctx: ServerContext,
    s: AsyncSession,
    user: m.User,
    issuer: str,
    ip: str | None,
    ua: str | None,
) -> tuple[str, str]:
    """A new session for a signed-in user, audited: ``(cookie value, CSRF token)``."""
    key = ctx.secrets.audit_key
    cookie_value, sess = await create_session(
        s,
        user,
        ip_hash=client_fingerprint(ip, key),
        user_agent_hash=client_fingerprint(ua, key),
    )
    await write_audit(
        ctx.db,
        [
            make_audit_event(
                action=AuditAction.AUTH_LOGIN,
                resource_kind=ResourceKind.TENANT,
                outcome=AuditOutcome.SUCCESS,
                ts=now(),
                fingerprint_key=key,
                actor_kind=PrincipalKind.USER,
                actor_id=user.id,
                operator=user.operator,
                client_ip=ip,
                user_agent=ua,
                details={"issuer": issuer},
            )
        ],
        session=s,
    )
    return cookie_value, sess.csrf_token


@router.get(
    "/callback",
    operation_id="auth_callback",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        302: {"description": "Signed in; redirect to the return path"},
        401: {"model": Problem, "description": "Sign-in failed"},
    },
)
async def auth_callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    """Finish sign-in: one-time transaction, state check, code exchange, ID-token checks."""
    ctx = _ctx(request)
    settings = ctx.settings
    login = CookieSpec.login(secure=settings.secure_cookies)
    value = request.cookies.get(login.name)
    if ctx.oidc is None:
        raise MapiError("sign-in is not configured on this server", code=ErrorCode.NOT_FOUND)
    tx: LoginTransaction | None = None
    if value:
        async with ctx.db.session() as s:
            tx = await pop_login(s, value)
    if tx is None:
        return await _login_failed(ctx, request, "no pending sign-in for this browser")
    if tx.expired(now()):
        return await _login_failed(ctx, request, "the sign-in took too long")
    if error is not None:
        safe = error if _ERROR_RE.fullmatch(error) else "invalid"
        return await _login_failed(ctx, request, f"the identity provider answered {safe}")
    if not state or not hmac.compare_digest(state.encode(), tx.state.encode()):
        return await _login_failed(ctx, request, "state mismatch")
    try:
        ident = await ctx.oidc.complete(code=code or "", tx=tx)
    except OidcError as exc:
        return await _login_failed(ctx, request, exc.reason)
    except httpx.HTTPError as exc:
        return await _login_failed(
            ctx, request, f"identity provider unreachable: {type(exc).__name__}"
        )
    ip, ua = _client(request)
    try:
        async with ctx.db.session() as s:
            user = await claim_or_create_user(
                s, ident, operator_emails=settings.effective_operator_emails()
            )
            cookie_value, csrf = await _start_session(ctx, s, user, ident.issuer, ip, ua)
    except AccountDisabled as exc:
        return await _login_failed(ctx, request, f"the account {exc} is disabled")
    # return_to is a path (safe_return_to); it belongs to the web app, which may be served
    # from another origin than the API (local dev; behind a load balancer they coincide)
    resp = RedirectResponse(
        settings.web_origin.rstrip("/") + tx.return_to, status_code=status.HTTP_302_FOUND
    )
    secure = settings.secure_cookies
    resp.headers.append("set-cookie", session_cookie(secure).header(cookie_value))
    resp.headers.append("set-cookie", csrf_cookie(secure).header(csrf))
    resp.headers.append("set-cookie", login.clear())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/logout", operation_id="auth_logout", status_code=status.HTTP_204_NO_CONTENT)
async def auth_logout(request: Request) -> Response:
    """End the session (idempotent) and clear the cookies. Cookie-authenticated, so it
    needs the CSRF header like every other mutation."""
    ctx = _ctx(request)
    secure = ctx.settings.secure_cookies
    value = request.cookies.get(session_cookie(secure).name)
    if value:
        async with ctx.db.session() as s:
            loaded = await load_session(s, value)
            if loaded is not None:
                sess, user = loaded
                _check_csrf(ctx, request, sess)
                await delete_session(s, value)
                ip, ua = _client(request)
                await write_audit(
                    ctx.db,
                    [
                        make_audit_event(
                            action=AuditAction.AUTH_LOGOUT,
                            resource_kind=ResourceKind.TENANT,
                            outcome=AuditOutcome.SUCCESS,
                            ts=now(),
                            fingerprint_key=ctx.secrets.audit_key,
                            actor_kind=PrincipalKind.USER,
                            actor_id=user.id,
                            client_ip=ip,
                            user_agent=ua,
                        )
                    ],
                    session=s,
                )
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.headers.append("set-cookie", session_cookie(secure).clear())
    resp.headers.append("set-cookie", csrf_cookie(secure).clear())
    return resp


@router.get("/session", response_model=SessionInfo, operation_id="auth_session")
async def auth_session(request: Request) -> JSONResponse:
    """Is this browser signed in? The CSRF token comes back in ``X-CSRF-Token``."""
    ctx = _ctx(request)
    value = request.cookies.get(session_cookie(ctx.settings.secure_cookies).name)
    headers = {"Cache-Control": "no-store"}
    info = SessionInfo(authenticated=False)
    if value:
        async with ctx.db.session() as s:
            loaded = await load_session(s, value)
            if loaded is not None:
                sess, user = loaded
                info = SessionInfo(
                    authenticated=True,
                    principal=user_principal(user),
                    operator=user.operator,
                    expires_at=min(sess.expires_at, sess.last_seen_at + SESSION_IDLE_TTL),
                )
                headers[CSRF_HEADER] = sess.csrf_token
    if not info.authenticated and not ctx.settings.requires_auth:
        # single-tenant profile: credential-less requests act as the local administrator,
        # so the browser is "signed in" without a cookie (no CSRF token: none is checked)
        info = SessionInfo(authenticated=True, principal=local_principal(), operator=True)
    return JSONResponse(info.model_dump(mode="json"), headers=headers)
