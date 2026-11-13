"""One dashboard tool call for the v0.20 adapters (chat and MCP), capped and timed (plan I-6).

:func:`call_tool_capped` runs :func:`~mapi_server.services.dashboards.tools.call_tool` for a
:class:`~mapi_server.services.dashboards.access.DashboardContext` that came from the caller's
credential (never from model output), then fits the result to a byte budget with
:func:`mapi_spec.tool_results.cap_tool_result`. A refusal (``SCHEMA_INVALID`` for an unknown
tool or bad arguments, ``NOT_FOUND``, ``DASHBOARD_INPUT_DISABLED``,
``UPLOADED_CODE_NOT_SHAREABLE``, ...) is returned, not raised, so the chat loop can hand it
to the model as a tool error and MCP can answer ``isError: true``.

The v0.19 uploaded-code guard: :func:`~mapi_server.services.dashboards.tools.run_tool` refuses
client and share contexts itself. A member context that is not an analyst comes from a read
token (:func:`~mapi_server.services.dashboards.access.token_context`): it is checked here too,
before every call, so a readout made with uploaded code after the token was resolved never
reaches an external agent (plan D11). Analyst members keep every readout.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from mapi_spec.errors import MapiError
from mapi_spec.tool_results import CappedResult, cap_tool_result
from pydantic import JsonValue

from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.dashboards.access import DashboardContext
from mapi_server.services.dashboards.tools import call_tool
from mapi_server.services.sharing import require_shareable

__all__ = ["call_tool_capped", "guarded_like_clients"]


def guarded_like_clients(dctx: DashboardContext) -> bool:
    """Whether results made with uploaded code are refused to this context: client users,
    share links, and members acting through a read token (not analysts)."""
    return dctx.principal_kind != "member" or not dctx.is_analyst


async def _require_shareable(ctx: ServerContext, dctx: DashboardContext) -> None:
    async with ctx.db.tenant_session(dctx.tenant_id) as session:
        row = await session.get(m.Dashboard, dctx.dashboard_id)
        if row is None:
            raise NotFoundError("dashboard", dctx.dashboard_id)
        await require_shareable(session, dashboard=row)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


async def call_tool_capped(
    ctx: ServerContext,
    dctx: DashboardContext,
    name: str,
    args: Mapping[str, JsonValue],
    *,
    max_bytes: int,
) -> tuple[CappedResult | None, MapiError | None, int]:
    """Run the tool ``name`` for ``dctx`` and cap its JSON result at ``max_bytes``.

    Returns ``(result, None, ms)`` on success and ``(None, error, ms)`` when the tool refused
    the call; ``ms`` is the elapsed time. Errors other than :class:`MapiError` propagate."""
    started = time.monotonic()
    try:
        if dctx.principal_kind == "member" and guarded_like_clients(dctx):
            await _require_shareable(ctx, dctx)
        result = await call_tool(ctx, dctx, name, args)
    except MapiError as exc:
        return None, exc, _elapsed_ms(started)
    capped = cap_tool_result(name, result.model_dump(mode="json"), max_bytes)
    return capped, None, _elapsed_ms(started)
