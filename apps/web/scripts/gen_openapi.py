"""Write ``apps/web/openapi.json`` from the real control plane (``mapi_server``).

The document is the server's own OpenAPI 3.1 output (FastAPI route table + the
:mod:`mapi_spec.api` DTOs, plus the run-event payload components), produced without a
database, object store or catalog by :func:`mapi_server.app.openapi_document` -- the same
document a running server serves at ``/openapi.json``. ``src/api/schema.d.ts`` is generated
from it with ``openapi-typescript`` (``pnpm --filter @mapi/web openapi:types``); the web code
refers to component names only through ``src/api/types.ts``.

Usage::

    uv run python apps/web/scripts/gen_openapi.py [--check]

``--check`` exits 1 when the committed ``openapi.json`` differs from the server's document
(CI runs it, so the web client cannot drift from the API).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mapi_server.app import openapi_document

OUT = Path(__file__).resolve().parent.parent / "openapi.json"


def render() -> str:
    return json.dumps(openapi_document(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write apps/web/openapi.json from mapi_server")
    parser.add_argument("--check", action="store_true", help="fail if openapi.json is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(
                f"{OUT} is stale; run `uv run python apps/web/scripts/gen_openapi.py` and "
                "`pnpm --filter @mapi/web openapi:types`",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
