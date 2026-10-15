"""Write ``apps/web/openapi.json`` (and ``src/api/access.gen.json``) for the web client.

The document is the server's own OpenAPI 3.1 output (FastAPI route table + the
:mod:`mapi_spec.api` DTOs, plus the run-event payload components), produced without a
database, object store or catalog by :func:`mapi_server.app.openapi_document` -- the same
document a running server serves at ``/openapi.json``.

**Contract overlay (v0.3).** Operations of :data:`mapi_spec.api.M2_ENDPOINTS` that the
server does not serve yet are added from the contract: each row becomes a stub FastAPI route
whose request/response models are the real :mod:`mapi_spec` DTOs, so the overlay is rendered
by the same machinery (component names such as ``Page_ApiToken_`` match what the server will
emit). Operations in :data:`CONTRACT_OVERRIDES` replace the server's M1 shape. Once the
server implements a route it wins automatically; ``--server-only`` writes the server's
document alone.

``src/api/access.gen.json`` exports the role order and the permission matrix
(:data:`mapi_spec.tenancy.PERMISSIONS`) so the UI hides actions the caller's role cannot do
(the server still decides).

``src/api/schema.d.ts`` is generated from ``openapi.json`` with ``openapi-typescript``
(``pnpm --filter @mapi/web openapi:types``); the web code refers to component names only
through ``src/api/types.ts``.

Usage::

    uv run python apps/web/scripts/gen_openapi.py [--check] [--server-only]

``--check`` exits 1 when a committed file differs from what would be written (CI runs it,
so the web client cannot drift from the API).
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, Header, Query, Response
from mapi_server.app import openapi_document
from mapi_spec import api as spec_api
from mapi_spec.api import M2_ENDPOINTS, EndpointDecl, Page
from mapi_spec.audit import AuditAction, AuditOutcome
from mapi_spec.quotas import QuotaScope
from mapi_spec.tenancy import PERMISSIONS, ProjectRole, TenantStatus, TrustTier
from mapi_spec.tokens import (
    DEFAULT_TOKEN_TTL_DAYS,
    MAX_TOKEN_TTL_DAYS,
    ScopeName,
    TokenStatus,
)

WEB = Path(__file__).resolve().parent.parent
OUT = WEB / "openapi.json"
ACCESS_OUT = WEB / "src" / "api" / "access.gen.json"

CONTRACT_OVERRIDES = frozenset({"get_me"})
"""Operations whose v0.3 contract replaces the shape the v0.2 server still serves."""

_PATH_PARAM = re.compile(r"\{([a-z_]+)\}")

_TAGS: tuple[tuple[str, str], ...] = (
    ("/v1/admin", "admin"),
    ("/v1/tokens", "tokens"),
    ("/v1/usage", "usage"),
    ("/v1/tenants", "tenants"),
    ("/v1/me", "identity"),
    ("/auth", "auth"),
    ("/internal", "internal"),
)


def _tag(path: str) -> str:
    return next(tag for prefix, tag in _TAGS if path.startswith(prefix))


def _query_type(decl: EndpointDecl, name: str) -> tuple[Any, Any]:
    """(annotation, default) of one query parameter of an M2 row."""
    opt = None
    if name == "limit":
        return Annotated[int, Query(ge=1, le=200)], 50
    if name == "status":
        if decl.path.startswith("/v1/tokens"):
            return TokenStatus | None, opt
        return TenantStatus | None, opt
    table: dict[str, Any] = {
        "cursor": str | None,
        "all": bool,
        "trust_tier": TrustTier | None,
        "scope": QuotaScope | None,
        "subject_id": str | None,
        "tenant_id": str | None,
        "project_id": str | None,
        "actor_id": str | None,
        "action": AuditAction | None,
        "outcome": AuditOutcome | None,
        "since": datetime | None,
        "until": datetime | None,
        "period": Literal["day", "month"],
        "start": datetime | None,
        "group_by": spec_api.UsageGroupBy | None,
        "return_to": str | None,
        "code": str | None,
        "state": str | None,
        "error": str | None,
    }
    if name not in table:
        raise KeyError(f"no query type for {decl.operation_id}:{name}")
    default = False if name == "all" else "month" if name == "period" else opt
    return Annotated[table[name], Query()], default


def _model(name: str | None) -> Any:
    if name is None:
        return None
    m = re.fullmatch(r"Page\[(\w+)\]", name)
    if m:
        item: Any = getattr(spec_api, m.group(1))
        return Page[item]
    return getattr(spec_api, name)


def _stub(decl: EndpointDecl) -> Callable[..., Any]:
    params: list[inspect.Parameter] = []
    kind = inspect.Parameter.KEYWORD_ONLY
    for p in _PATH_PARAM.findall(decl.path):
        ann: Any = QuotaScope if p == "scope" else str
        params.append(inspect.Parameter(p, kind, annotation=ann))
    for q in decl.query:
        ann, default = _query_type(decl, q)
        params.append(inspect.Parameter(f"q_{q}", kind, annotation=_alias(ann, q), default=default))
    for h in decl.headers:
        params.append(
            inspect.Parameter(
                "h_" + h.lower().replace("-", "_"),
                kind,
                annotation=Annotated[str | None, Header(alias=h)],
                default=None,
            )
        )
    if decl.request is not None:
        params.append(inspect.Parameter("body", kind, annotation=_model(decl.request)))

    def endpoint(**_: Any) -> Any:  # pragma: no cover - never called
        raise NotImplementedError

    endpoint.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    endpoint.__name__ = decl.operation_id
    endpoint.__doc__ = None
    return endpoint


def _alias(ann: Any, name: str) -> Any:
    """Re-attach the wire name to a ``q_``-prefixed Python parameter."""
    origin = getattr(ann, "__metadata__", None)
    if origin is None:
        return Annotated[ann, Query(alias=name)]
    base = ann.__origin__
    return Annotated[base, Query(alias=name, **_query_kwargs(origin[0]))]


def _query_kwargs(q: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for meta in getattr(q, "metadata", []):
        for attr in ("ge", "le"):
            if getattr(meta, attr, None) is not None:
                out[attr] = getattr(meta, attr)
    return out


def contract_document(missing: list[EndpointDecl]) -> dict[str, Any]:
    """OpenAPI for ``missing`` rows, rendered by FastAPI from stub routes."""
    app = FastAPI(title="contract", version="0", openapi_version="3.1.0")
    for decl in missing:
        response_model = _model(decl.response)
        kwargs: dict[str, Any] = {
            "methods": [decl.method],
            "operation_id": decl.operation_id,
            "summary": decl.summary,
            "status_code": decl.success,
            "tags": [_tag(decl.path)],
        }
        if response_model is None:
            kwargs["response_class"] = Response
            kwargs["response_model"] = None
        else:
            kwargs["response_model"] = response_model
        app.add_api_route(decl.path, _stub(decl), **kwargs)
    return app.openapi()


def build(*, server_only: bool = False) -> dict[str, Any]:
    doc = openapi_document()
    if server_only:
        return doc
    served = {(path, method.upper()) for path, item in doc["paths"].items() for method in item}
    missing = [
        d
        for d in M2_ENDPOINTS
        if (d.path, d.method) not in served or d.operation_id in CONTRACT_OVERRIDES
    ]
    if not missing:
        return doc
    overlay = contract_document(missing)
    for path, item in overlay["paths"].items():
        doc["paths"].setdefault(path, {}).update(item)
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    schemas.update(overlay.get("components", {}).get("schemas", {}))
    known = {t["name"] for t in doc.get("tags", [])}
    for tag in sorted({_tag(d.path) for d in missing} - known):
        doc.setdefault("tags", []).append({"name": tag})
    doc["info"]["description"] = (
        doc["info"].get("description", "").split(" M1 is single-tenant")[0]
        + " v0.3 adds tenants, projects, members, API tokens, usage and operator endpoints"
        " (multi-tenant; browser sessions via the /auth backend-for-frontend)."
    )
    return doc


def access() -> dict[str, Any]:
    """Role order and permission matrix for the UI (generated; do not edit)."""
    return {
        "roles": [r.value for r in ProjectRole],
        "permissions": {
            f"{res.value}:{act.value}": {
                "min_role": None if rule.min_role is None else rule.min_role.value,
                "level": rule.level.value,
            }
            for (res, act), rule in sorted(PERMISSIONS.items(), key=lambda kv: kv[0])
        },
        "token_scopes": [s.value for s in ScopeName],
        "default_token_ttl_days": DEFAULT_TOKEN_TTL_DAYS,
        "max_token_ttl_days": MAX_TOKEN_TTL_DAYS,
    }


def _render(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write apps/web/openapi.json from mapi_server")
    parser.add_argument("--check", action="store_true", help="fail if a written file is stale")
    parser.add_argument(
        "--server-only", action="store_true", help="the server's document, no contract overlay"
    )
    args = parser.parse_args(argv)
    outputs = {OUT: _render(build(server_only=args.server_only)), ACCESS_OUT: _render(access())}
    if args.check:
        stale = [
            p
            for p, text in outputs.items()
            if (p.read_text(encoding="utf-8") if p.exists() else "") != text
        ]
        for p in stale:
            print(
                f"{p} is stale; run `uv run python apps/web/scripts/gen_openapi.py` and "
                "`pnpm --filter @mapi/web openapi:types`",
                file=sys.stderr,
            )
        return 1 if stale else 0
    for p, text in outputs.items():
        p.write_text(text, encoding="utf-8")
        print(f"wrote {p} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
