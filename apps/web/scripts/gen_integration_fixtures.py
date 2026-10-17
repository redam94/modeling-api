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

from mapi_spec.endpoints import (
    EndpointSchemaSnapshot,
    example_actuals_call,
    example_score_call,
    input_table,
)
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


#: A second endpoint: a monthly sales forecast whose model conditions forecasts on actuals
#: (``mapi_spec.actuals``), with a ``price`` driver, fitted through September 2026.
FORECAST_ALIAS = "sales-forecast"
FORECAST_SNAPSHOT: dict[str, Any] = {
    "input_schema": {
        "dims": {"month": {"kind": "time", "policy": "extend", "freq": "MS"}},
        "vars": {
            "sales": {"dims": ["month"], "dtype": "float64", "role": "target", "units": "kEUR"},
            "price": {"dims": ["month"], "dtype": "float64", "future_required": True},
        },
    },
    "schema_hash": "sha256:" + "cd" * 32,
    "inputs_stage": "raw",
    "horizon_dim": "month",
    "score_modes": ["evaluate", "forecast", "predict"],
    "representation": "quantiles",
    "future_required": ["price"],
    "fit_coords": {
        "month": {
            "kind": "time",
            "size": 69,
            "start": "2021-01-01",
            "end": "2026-09-01",
            "freq": "MS",
            "contiguous": True,
        }
    },
    "conditions_on_actuals": True,
}


def _endpoint(alias: str, raw: dict[str, Any]) -> dict[str, Any]:
    snapshot = EndpointSchemaSnapshot.model_validate(raw)
    # As the server's `_example`, `get_endpoint_schema` and `list_endpoint_snippets`.
    example = example_score_call(snapshot).model_dump(
        mode="json", by_alias=True, exclude_defaults=True
    )
    actuals = example_actuals_call(snapshot)
    table = input_table(snapshot)
    snippets = render_snippets(
        SnippetContext(
            base_url=BASE_URL, project=PROJECT, alias=alias, payload=example, input_table=table
        )
    )
    return {
        "alias": alias,
        "snapshot": snapshot.model_dump(mode="json", by_alias=True, exclude_none=True),
        "example": example,
        "actuals_example": (
            actuals.model_dump(mode="json", by_alias=True, exclude_defaults=True)
            if actuals is not None
            else None
        ),
        "input_table": table.model_dump(mode="json") if table is not None else None,
        "snippets": snippets.model_dump(mode="json", by_alias=True),
    }


def render() -> str:
    doc = {
        **_endpoint(ALIAS, SNAPSHOT),
        "base_url": BASE_URL,
        "project": PROJECT,
        "forecast": _endpoint(FORECAST_ALIAS, FORECAST_SNAPSHOT),
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
