"""Write ``apps/web/src/mocks/integrations.gen.json`` for the mock API.

The mock-mode Integrations hub (``src/mocks/integrations.ts``: the dev server in ``--mode mock``,
the Playwright smoke suite and the guide's screenshots) serves the endpoint ``geo-mmm-prod``.
Its input-schema snapshot is validated here as :class:`mapi_spec.endpoints.EndpointSchemaSnapshot`,
and its example call and code snippets are made by the functions the server calls
(``example_score_call`` and :func:`mapi_spec.snippets.render_snippets`, as
``GET /v1/endpoints/{alias}/schema`` and ``…/snippets`` do), so the code the app shows and copies
is exactly the code the server generates.

Usage::

    uv run python apps/web/scripts/gen_integration_fixtures.py [--check]

``--check`` exits 1 when the committed file differs from what would be written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mapi_spec.endpoints import EndpointSchemaSnapshot, example_score_call
from mapi_spec.snippets import SnippetContext, render_snippets

WEB = Path(__file__).resolve().parent.parent
OUT = WEB / "src" / "mocks" / "integrations.gen.json"

#: The mock app's API base (``env.apiBase`` without ``VITE_API_BASE``) and endpoint scope.
BASE_URL = "http://127.0.0.1:8000"
PROJECT = "acme/growth"
ALIAS = "geo-mmm-prod"

SNAPSHOT: dict[str, Any] = {
    "input_schema": {
        "dims": {
            "date": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
            "market": {"kind": "categorical", "policy": "fixed"},
            "channel": {"kind": "categorical", "policy": "fixed"},
        },
        "vars": {
            "spend_local": {
                "dims": ["market", "channel", "date"],
                "dtype": "float64",
                "role": "spend",
                "units": "local",
            }
        },
    },
    "schema_hash": "sha256:" + "ab" * 32,
    "inputs_stage": "raw",
    "horizon_dim": "date",
    "score_modes": ["predict", "forecast", "counterfactual", "evaluate"],
    "future_required": [],
}


def render() -> str:
    snapshot = EndpointSchemaSnapshot.model_validate(SNAPSHOT)
    # As the server's `_example` in mapi_server.api.endpoints.
    example = example_score_call(snapshot).model_dump(
        mode="json", by_alias=True, exclude_defaults=True
    )
    snippets = render_snippets(
        SnippetContext(base_url=BASE_URL, project=PROJECT, alias=ALIAS, payload=example)
    )
    doc = {
        "alias": ALIAS,
        "base_url": BASE_URL,
        "project": PROJECT,
        "snapshot": snapshot.model_dump(mode="json", by_alias=True, exclude_none=True),
        "example": example,
        "snippets": snippets.model_dump(mode="json", by_alias=True),
    }
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text("utf-8") != text:
            print(
                f"{OUT} is stale: run uv run python apps/web/scripts/gen_integration_fixtures.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text, "utf-8")
    print(f"wrote {OUT.relative_to(WEB)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
