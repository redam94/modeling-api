"""Saved plots, static rendering and embed links (M3; plan §8.2 Plots, §10.3 items 4-5).

``POST /v1/plots`` saves a model version's plot or a user-authored PlotSpec (sanitized
first), ``POST /v1/plots:render`` turns one into PNG, SVG or PDF in the sandbox (a
``render`` run; ``Prefer: wait=N``), and ``POST /v1/plots/{plot_id}/embeds`` mints a
tokenised read-only link for platform-recipe plots. ``GET /v1/embeds/{embed_token}`` is
public: no credential is read (cookies and ``Authorization`` are dropped before routing, see
:class:`EmbedCorsMiddleware`), only the plot-sandbox origin may read it from a browser, and
it is rate limited per link and per client address.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Any, Final

from fastapi import APIRouter, Header, Path, Request, Response, status
from fastapi.responses import JSONResponse
from mapi_spec.api import (
    ALL_ENDPOINTS,
    PREFER_HEADER,
    PREFERENCE_APPLIED_HEADER,
    ArtifactLink,
    Page,
    PlotRender,
    parse_prefer_wait,
)
from mapi_spec.errors import ERROR_INFO, ErrorCode
from mapi_spec.executor import RunKind
from mapi_spec.jobs import RENDER_OUTPUT_PREFIX
from mapi_spec.run import RunState
from mapi_spec.sharing import (
    EmbedView,
    PlotEmbed,
    PlotEmbedCreate,
    PlotEmbedCreated,
    PlotRenderRequest,
    SavedPlot,
    SavedPlotCreate,
)
from sqlalchemy import select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mapi_server.api.common import (
    DEFAULT_LIMIT,
    Auth,
    Ctx,
    Idempotency,
    IdempotencyKeyHeader,
    Limit,
    Session,
    decode_cursor,
    encode_cursor,
)
from mapi_server.api.runs import run_view, wait_terminal
from mapi_server.context import ServerContext
from mapi_server.db import models as m
from mapi_server.errors import NotFoundError
from mapi_server.services.bundles import artifact_link
from mapi_server.services.quotas import charge_egress
from mapi_server.services.runs import load_run, now
from mapi_server.services.sharing import (
    create_embed,
    create_saved_plot,
    delete_saved_plot,
    embed_dto,
    embed_view,
    load_saved_plot,
    resolve_render_request,
    revoke_embed,
    saved_plot_dto,
    submit_render,
)

__all__ = ["EMBEDS_PATH", "EmbedCorsMiddleware", "embeds_router", "problem_responses", "router"]

router = APIRouter(prefix="/v1/plots", tags=["plots"])
embeds_router = APIRouter(prefix="/v1/embeds", tags=["plots"])

EMBEDS_PATH: Final = "/v1/embeds/"
PlotId = Annotated[str, Path(pattern=r"^plt_[0-9a-z]{26}$")]
EmbedId = Annotated[str, Path(pattern=r"^emb_[0-9a-z]{26}$")]
RunId = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")]
EmbedToken = Annotated[str, Path(min_length=1, max_length=128)]
_DECLS = {e.operation_id: e for e in ALL_ENDPOINTS}
_EXTRA_ERRORS: Final[dict[str, tuple[ErrorCode, ...]]] = {
    "render_plot": (ErrorCode.INTEGRITY_FAILURE,),
    "create_plot_embed": (ErrorCode.PLOT_UNSAFE, ErrorCode.INTEGRITY_FAILURE),
    "create_saved_plot": (ErrorCode.INTEGRITY_FAILURE,),
}
_PROBLEM = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}


def problem_responses(
    operation_id: str, extra: dict[int | str, dict[str, Any]] | None = None
) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` with the RFC 7807 problems an operation declares (its row of
    :data:`mapi_spec.api.ALL_ENDPOINTS`), grouped by status, codes named in the text."""
    decl = _DECLS[operation_id]
    codes = (*decl.errors, *_EXTRA_ERRORS.get(operation_id, ()))
    if operation_id != "get_embed":
        codes = (*codes, ErrorCode.UNAUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.RATE_LIMITED)
    by_status: dict[int, list[ErrorCode]] = defaultdict(list)
    for code in dict.fromkeys(codes):
        by_status[ERROR_INFO[code].status].append(code)
    out: dict[int | str, dict[str, Any]] = dict(extra or {})
    for code_status, items in sorted(by_status.items()):
        names = ", ".join(f"`{c.value}`" for c in items)
        out[code_status] = {"description": f"Problem ({names})", "content": _PROBLEM}
    return out


def _op(operation_id: str, **extra: Any) -> dict[str, Any]:
    decl = _DECLS[operation_id]
    return {
        "operation_id": operation_id,
        "summary": decl.summary,
        "responses": problem_responses(operation_id, extra.pop("responses", None)),
        **extra,
    }


# ----------------------------------------------------------------------------- saved plots


@router.post(
    "", response_model=SavedPlot, status_code=status.HTTP_201_CREATED, **_op("create_saved_plot")
)
async def create_saved_plot_route(ctx: Ctx, session: Session, body: SavedPlotCreate) -> SavedPlot:
    """Save a model version's plot (``source``: verified against the signed bundle manifest)
    or a user-authored PlotSpec (``spec``: sanitized; its provenance is recorded as
    ``author`` and it can never be embedded). ``PLOT_UNSAFE`` when the sanitizer refuses it."""
    row = await create_saved_plot(session, ctx, body)
    return saved_plot_dto(row, with_spec=True)


@router.get("", response_model=Page[SavedPlot], **_op("list_saved_plots"))
async def list_saved_plots(
    ctx: Ctx, session: Session, limit: Limit = DEFAULT_LIMIT, cursor: str | None = None
) -> Page[SavedPlot]:
    """Saved plots of the project, newest first (without their specs)."""
    sc = ctx.scope
    stmt = select(m.SavedPlot).where(
        m.SavedPlot.tenant_id == sc.tenant_id, m.SavedPlot.project_id == sc.project_id
    )
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.SavedPlot.id < str(after))
    rows = list(
        (await session.execute(stmt.order_by(m.SavedPlot.id.desc()).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return Page[SavedPlot](
        items=[saved_plot_dto(r, with_spec=False) for r in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.get("/{plot_id}", response_model=SavedPlot, **_op("get_saved_plot"))
async def get_saved_plot(ctx: Ctx, session: Session, plot_id: PlotId) -> SavedPlot:
    """A saved plot with its spec, sanitized again on the way out. Render it only in the
    plot-sandbox origin."""
    return saved_plot_dto(await load_saved_plot(session, ctx, plot_id), with_spec=True)


@router.delete(
    "/{plot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    **_op("delete_saved_plot"),
)
async def delete_saved_plot_route(ctx: Ctx, session: Session, plot_id: PlotId) -> Response:
    """Delete a saved plot. Its embed links stop working (404) and their images are removed."""
    await delete_saved_plot(session, ctx, plot_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------------- rendering


async def _render_result(ctx: ServerContext, run_id: str) -> PlotRender:
    async with ctx.data_session() as session:
        run = await load_run(session, ctx.scope, run_id)
        if run.kind != RunKind.RENDER.value:
            raise NotFoundError("render", run_id)
        view = await run_view(session, run)
        outputs: list[ArtifactLink] = []
        if run.state == RunState.SUCCEEDED.value:
            att = await session.get(m.RunAttempt, (run.id, run.attempt))
            for name, meta in sorted(((att.outputs if att else None) or {}).items()):
                if not name.startswith(RENDER_OUTPUT_PREFIX):
                    continue
                outputs.append(
                    await artifact_link(
                        ctx.store,
                        name=name,
                        bucket=meta["bucket"],
                        key=meta["name"],
                        generation=int(meta["generation"]),
                    )
                )
    await charge_egress(ctx, run.tenant_id, sum(o.size or 0 for o in outputs))
    fmt = str((run.payload or {}).get("format", "png"))
    return PlotRender(
        done=RunState(run.state).terminal,
        run=view,
        format=fmt,
        outputs=outputs,
    )


@router.post(
    ":render",
    response_model=PlotRender,
    **_op(
        "render_plot",
        responses={202: {"model": PlotRender, "description": "Still rendering"}},
    ),
)
async def render_plot(
    ctx: Ctx,
    request: Request,
    body: PlotRenderRequest,
    prefer: Annotated[str | None, Header(alias=PREFER_HEADER)] = None,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Render one plot to PNG, SVG or PDF in the sandbox (Vega-Lite through vl-convert with
    network access disabled; Plotly only where the platform enables it, else
    ``MODE_NOT_SUPPORTED``). The plot is sanitized first and its data is passed to the run
    as pinned inputs: rendering never fetches a URL. ``Prefer: wait=N`` (≤ 60) waits up to
    N s: 200 with signed links to ``renders/<plot>.<format>`` when done, else 202 with the
    run (poll ``GET /v1/plots/renders/{run_id}``)."""
    raw = await request.json()
    async with ctx.data_session() as session:
        idem = Idempotency(session, ctx.scope, idempotency_key)
        replay = await idem.begin(f"POST {request.url.path}", raw)
        if replay is not None:
            run_id = replay.resource_id
        else:
            resolved = await resolve_render_request(
                session,
                ctx,
                plot=body.plot,
                saved_plot_id=body.saved_plot_id,
                source=body.source,
            )
            run = await submit_render(
                session,
                ctx,
                resolved,
                fmt=body.format,
                scale=body.scale,
                width=body.width,
                height=body.height,
            )
            run_id = run.id
            await idem.complete("run", run.id, 202)
    wait = parse_prefer_wait(prefer)
    headers: dict[str, str] = {}
    if wait:
        await wait_terminal(ctx, run_id, float(wait), disconnected=request.is_disconnected)
        headers[PREFERENCE_APPLIED_HEADER] = f"wait={wait}"
    result = await _render_result(ctx, run_id)
    return JSONResponse(
        result.model_dump(mode="json"), status_code=200 if result.done else 202, headers=headers
    )


@router.get("/renders/{run_id}", response_model=PlotRender, **_op("get_plot_render"))
async def get_plot_render(ctx: Ctx, run_id: RunId) -> PlotRender:
    """A render run and, once it succeeded, signed links to its files."""
    return await _render_result(ctx, run_id)


# ----------------------------------------------------------------------------- embeds


@router.post(
    "/{plot_id}/embeds",
    response_model=PlotEmbedCreated,
    status_code=status.HTTP_201_CREATED,
    **_op("create_plot_embed"),
)
async def create_plot_embed(
    ctx: Ctx, session: Session, plot_id: PlotId, body: PlotEmbedCreate
) -> PlotEmbedCreated:
    """Mint a read-only link (the token is in the response once; only its HMAC is stored).
    Only plots a model version drew with a platform recipe can be embedded
    (``EMBED_NOT_ALLOWED`` for plugin- and user-authored plots). When the caller may start
    render runs, a PNG of the plot is rendered in the sandbox as the embed page's fallback.
    Audited."""
    embed, token = await create_embed(session, ctx, plot_id, body)
    base = ctx.settings.plots_origin.rstrip("/")
    return PlotEmbedCreated(embed=embed_dto(embed), url=f"{base}/embed/{token}", token=token)


@router.get("/{plot_id}/embeds", response_model=Page[PlotEmbed], **_op("list_plot_embeds"))
async def list_plot_embeds(
    ctx: Ctx,
    session: Session,
    plot_id: PlotId,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page[PlotEmbed]:
    """Embed links of a saved plot, newest first (never their tokens)."""
    plot = await load_saved_plot(session, ctx, plot_id)
    stmt = select(m.PlotEmbed).where(m.PlotEmbed.plot_id == plot.id)
    after = decode_cursor(cursor)
    if after is not None:
        stmt = stmt.where(m.PlotEmbed.id < str(after))
    rows = list(
        (await session.execute(stmt.order_by(m.PlotEmbed.id.desc()).limit(limit + 1))).scalars()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    at = now()
    return Page[PlotEmbed](
        items=[embed_dto(r, at) for r in rows],
        next_cursor=encode_cursor(rows[-1].id) if more and rows else None,
    )


@router.delete(
    "/{plot_id}/embeds/{embed_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    **_op("revoke_plot_embed"),
)
async def revoke_plot_embed(
    ctx: Ctx, session: Session, plot_id: PlotId, embed_id: EmbedId
) -> Response:
    """Revoke an embed link: it answers ``410 LINK_EXPIRED`` from now on. Idempotent."""
    await revoke_embed(session, ctx, plot_id, embed_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


EMBED_RESPONSE_HEADERS: Final = {
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@embeds_router.get(
    "/{embed_token}",
    response_model=EmbedView,
    **_op("get_embed"),
)
async def get_embed(ctx: Ctx, auth: Auth, embed_token: EmbedToken) -> JSONResponse:
    """Public: the embedded plot with every dataset inline (``410 LINK_EXPIRED`` once
    expired or revoked, ``404`` for an unknown token, ``429`` past the per-link or
    per-address rate). Cookies and ``Authorization`` are ignored; browsers may read it only
    from the plot-sandbox origin (CORS), which renders it under its own CSP."""
    view = await embed_view(ctx, embed_token, auth.client_ip)
    # no browser or shared cache may answer for the server: a revoked link must stop
    # rendering at once, on the next load (the API's own reuse is MAPI_EMBED_CACHE_S)
    headers = {**EMBED_RESPONSE_HEADERS, "Cache-Control": "private, no-cache"}
    return JSONResponse(view.model_dump(mode="json"), headers=headers)


class EmbedCorsMiddleware:
    """CORS and credential handling of ``/v1/embeds/`` (outside the app's CORS middleware).

    The app's CORS policy is for the web origin, with credentials. Embed views are public
    and read by the plot-sandbox origin only, never with credentials, so for these paths
    this middleware

    * drops ``Cookie`` and ``Authorization`` from the request (no credential is ever looked
      at, even by mistake) and its ``Origin`` before the app's CORS middleware sees it;
    * answers preflights itself (``GET`` only, from the plot-sandbox origin);
    * adds ``Access-Control-Allow-Origin: <plots origin>`` (no credentials) to responses for
      that origin, and ``Vary: Origin`` to all.
    """

    def __init__(self, app: ASGIApp, *, allowed_origin: str) -> None:
        self.app = app
        self.allowed = allowed_origin.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(EMBEDS_PATH):
            await self.app(scope, receive, send)
            return
        origin = ""
        headers: list[tuple[bytes, bytes]] = []
        for name, value in scope.get("headers", []):
            lname = name.lower()
            if lname == b"origin":
                origin = value.decode("latin-1")
            if lname in (b"cookie", b"authorization", b"origin"):
                continue
            headers.append((name, value))
        allowed = origin.rstrip("/") == self.allowed
        if scope.get("method") == "OPTIONS":
            out = [(b"vary", b"Origin"), (b"content-length", b"0")]
            if allowed:
                out += [
                    (b"access-control-allow-origin", self.allowed.encode("latin-1")),
                    (b"access-control-allow-methods", b"GET"),
                    (b"access-control-max-age", b"600"),
                ]
            await send(
                {"type": "http.response.start", "status": 204 if allowed else 403, "headers": out}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        inner = {**scope, "headers": headers}

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not k.lower().startswith(b"access-control-")
                    and k.lower() not in (b"set-cookie", b"vary")
                ]
                raw.append((b"vary", b"Origin"))
                if allowed:
                    raw.append((b"access-control-allow-origin", self.allowed.encode("latin-1")))
                message = {**message, "headers": raw}
            await send(message)

        await self.app(inner, receive, send_with_cors)
