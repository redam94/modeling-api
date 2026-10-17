"""Write ``apps/web/src/mocks/editor.gen.json`` for the mock API.

The mock-mode spec editor (``--mode mock``, the Playwright smoke suite) completes and describes
values from ``GET /v1/schemas/modelspec.json`` and ``GET /v1/catalog``. Both are generated
here exactly as the server builds them: :func:`mapi_spec.jsonschema_export.export_schemas`
and the default plugin registry's catalog (every installed step, model and plot), so the
hints the app shows in mock mode are the ones a real control plane serves.

Usage::

    uv run python apps/web/scripts/gen_editor_fixtures.py [--check]

``--check`` exits 1 when the committed file differs from what would be written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from mapi_sdk.registry import default_registry
from mapi_spec.jsonschema_export import export_schemas

WEB = Path(__file__).resolve().parent.parent
OUT = WEB / "src" / "mocks" / "editor.gen.json"

#: Fixed so the file is deterministic (the server stamps the catalog when it loads it).
GENERATED_AT = "2026-10-14T09:00:00Z"


def build() -> str:
    catalog = default_registry().catalog().model_dump(mode="json", exclude_none=True)
    # The server's digest is the runner image's; a content hash stands in for it here.
    digest = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
    doc = {
        "modelspec_schema": export_schemas()["modelspec.json"],
        "catalog": {**catalog, "digest": f"sha256:{digest}", "generated_at": GENERATED_AT},
    }
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args()
    text = build()
    if args.check:
        if not OUT.exists() or OUT.read_text("utf-8") != text:
            print(f"{OUT.relative_to(WEB)} is stale: run this script", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(WEB)} ({len(text):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
