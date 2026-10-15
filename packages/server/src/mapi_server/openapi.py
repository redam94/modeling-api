"""Print the OpenAPI 3.1 document: ``python -m mapi_server.openapi > apps/web/openapi.json``.

No database, object store or catalog is needed: the document is generated from the route
table and the :mod:`mapi_spec.api` DTOs.
"""

from __future__ import annotations

import json
import sys

from mapi_server.app import openapi_document

__all__ = ["main"]


def main() -> int:
    doc = openapi_document()
    sys.stdout.write(json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
