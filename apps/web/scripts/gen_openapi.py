"""Write ``apps/web/openapi.json`` (and ``src/api/access.gen.json``) for the web client.

The document is the server's own OpenAPI 3.1 output (FastAPI route table + the
:mod:`mapi_spec.api` DTOs, plus the run-event payload components), produced without a
database, object store or catalog by :func:`mapi_server.app.openapi_document` -- the same
document a running server serves at ``/openapi.json``. Every operation of
:data:`mapi_spec.api.ENDPOINTS` (M1, M2 and M3) is served by the server, so nothing is
overlaid.

``src/api/access.gen.json`` exports the role order and the permission matrix
(:data:`mapi_spec.tenancy.PERMISSIONS`) so the UI hides actions the caller's role cannot do
(the server still decides).

``src/api/schema.d.ts`` is generated from ``openapi.json`` with ``openapi-typescript``
(``pnpm --filter @mapi/web openapi:types``); the web code refers to component names only
through ``src/api/types.ts``.

Usage::

    uv run python apps/web/scripts/gen_openapi.py [--check]

``--check`` exits 1 when a committed file differs from what would be written (CI runs it,
so the web client cannot drift from the API).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mapi_server.app import openapi_document
from mapi_spec.tenancy import PERMISSIONS, ProjectRole
from mapi_spec.tokens import DEFAULT_TOKEN_TTL_DAYS, MAX_TOKEN_TTL_DAYS, ScopeName

WEB = Path(__file__).resolve().parent.parent
OUT = WEB / "openapi.json"
ACCESS_OUT = WEB / "src" / "api" / "access.gen.json"


def build() -> dict[str, Any]:
    """The server's OpenAPI document."""
    return openapi_document()


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
    args = parser.parse_args(argv)
    outputs = {OUT: _render(build()), ACCESS_OUT: _render(access())}
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
