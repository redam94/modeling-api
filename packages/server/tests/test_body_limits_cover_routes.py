"""Every dashboard input route's body is capped before it is parsed (v0.17, WP8, plan I-4).

The share routes are anonymous until the session check, and FastAPI reads and validates a
body before the handler runs: :class:`~mapi_server.api.problems.JsonBodyLimit` must cap and
depth-check every ``POST`` body of ``/v1/shared/*`` (``/v1/share-sessions`` reads its own,
4 KiB) and of the member and client input routes. The table comes from the app itself, so a
new input route without a cap fails here.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mapi_server.api import shares
from mapi_server.api.problems import JsonBodyLimit
from mapi_server.app import create_app, iter_api_routes
from mapi_server.context import ServerContext
from mapi_spec.api import CLIENT_OPERATIONS

pytestmark = pytest.mark.integration

_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")
SELF_READ = {"create_share_session"}
"""Routes that read their own (small) body after the per-address limit."""


def _limits(app: FastAPI) -> list[tuple[list[re.Pattern[str]], int]]:
    out = []
    for mw in app.user_middleware:
        if mw.cls is JsonBodyLimit:
            kwargs: dict[str, Any] = dict(mw.kwargs)
            out.append(([re.compile(p) for p in kwargs["paths"]], int(kwargs["max_bytes"])))
    return out


def _cap(app: FastAPI, path: str) -> int | None:
    caps = [n for patterns, n in _limits(app) if any(p.fullmatch(path) for p in patterns)]
    return min(caps) if caps else None


def _post_routes(app: FastAPI) -> list[tuple[str, str]]:
    out = []
    for route, template in iter_api_routes(app):
        if "POST" not in (route.methods or ()):
            continue
        path = _PARAM.sub("dsh_" + "0" * 26, template)
        out.append((route.operation_id or route.name, path))
    return out


def test_every_share_and_client_input_post_is_capped(ctx: ServerContext) -> None:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    checked = []
    for op, path in _post_routes(app):
        public = path.startswith("/v1/shared/")
        client_input = op in CLIENT_OPERATIONS and path.startswith("/v1/dashboards/")
        if not (public or client_input) or op in SELF_READ:
            continue
        assert _cap(app, path) is not None, f"POST {path} ({op}) has no JsonBodyLimit"
        checked.append(op)
    assert {
        "submit_shared_predictions", "submit_shared_batch", "submit_dashboard_predictions",
        "submit_dashboard_batch", "submit_shared_actuals", "save_shared_scenario",
    } <= set(checked)  # fmt: skip


def test_the_caps_are_the_planned_ones(ctx: ServerContext) -> None:
    app = create_app(ctx.settings, ctx=ctx, manage_context=False)
    dash = "/v1/dashboards/dsh_" + "0" * 26
    assert _cap(app, "/v1/shared/predictions") == shares.PREDICTION_BODY_MAX_BYTES == 32 * 1024
    assert _cap(app, f"{dash}/predictions") == 32 * 1024
    for path in ("/v1/shared/batch", f"{dash}/batch", "/v1/shared/actuals", f"{dash}/actuals"):
        assert _cap(app, path) == shares.ACTUALS_BODY_MAX_BYTES, path
    assert _cap(app, "/v1/shared/scenarios") == shares.INPUT_BODY_MAX_BYTES


async def test_an_oversized_body_is_refused_before_the_handler(
    client: httpx.AsyncClient,
) -> None:
    """33 KiB of scenario rows: ``413`` before the session check (no session is sent)."""
    rows = [{"note": "x" * 120} for _ in range(300)]
    r = await client.post("/v1/shared/predictions", json={"rows": rows})
    assert r.status_code == 413 and r.json()["code"] == "PAYLOAD_TOO_LARGE", r.text
    small = await client.post("/v1/shared/predictions", json={"rows": [{}]})
    assert small.status_code == 401, small.text  # under the cap: the session check answers
    r = await client.post("/v1/dashboards/dsh_" + "0" * 26 + "/predictions", json={"rows": rows})
    assert r.status_code == 413, r.text
