"""Write ``apps/web/src/mocks/editor.gen.json`` for the mock API.

The mock-mode spec editor (``--mode mock``, the Playwright smoke suite) completes and describes
values from ``GET /v1/schemas/modelspec.json`` and ``GET /v1/catalog``. Both are generated
here exactly as the server builds them: :func:`mapi_spec.jsonschema_export.export_schemas`
and the default plugin registry's catalog (every installed step, model and plot), so the
hints the app shows in mock mode are the ones a real control plane serves.

The Score page reads ``GET /v1/models/{id}/versions/{v}/inputs`` (``model_inputs``): the input
contract of the mock model (``eu-geo-mmm`` v1, the starter spec fitted on daily data through
2026-09-30, under its canonical names) and the input table the server builds from it
(:func:`mapi_spec.endpoints.input_table`).

Usage::

    uv run python apps/web/scripts/gen_editor_fixtures.py [--check]

``plot-examples.gen.json`` holds what the describe run renders for the catalog's plot cards
(``GET /v1/catalog/plots/{id}/{version}/example``): every recipe on its synthetic example
data, stamped with a fixed time. The mock API imports it lazily (it is the largest fixture).

``--check`` exits 1 when a committed file differs from what would be written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from mapi_sdk.registry import default_registry
from mapi_spec.endpoints import EndpointSchemaSnapshot, ModelInputSchema, input_table
from mapi_spec.jsonschema_export import export_schemas

WEB = Path(__file__).resolve().parent.parent
OUT = WEB / "src" / "mocks" / "editor.gen.json"
EXAMPLES_OUT = WEB / "src" / "mocks" / "plot-examples.gen.json"

#: Fixed so the file is deterministic (the server stamps the catalog when it loads it).
GENERATED_AT = "2026-10-14T09:00:00Z"

#: The mock model version's input contract: the starter spec's binding applied to the mock
#: dataset (``market`` → ``geo``, ``date`` → ``time``, ``revenue_local`` → ``y``,
#: ``spend_local`` → ``spend``), spend needed on the periods a forecast covers.
SNAPSHOT = {
    "inputs_stage": "raw",
    "input_schema": {
        "dims": {
            "geo": {"kind": "categorical", "policy": "fixed"},
            "channel": {"kind": "categorical", "policy": "fixed"},
            "time": {"kind": "time", "policy": "extend", "freq": "D"},
        },
        "vars": {
            "y": {"dims": ["geo", "time"], "dtype": "float64", "role": "target", "units": "EUR"},
            "spend": {"dims": ["geo", "channel", "time"], "dtype": "float64", "role": "spend",
                      "units": "EUR", "future_required": True},
        },
    },
    "schema_hash": "sha256:" + "ab" * 32,
    "score_modes": ["counterfactual", "evaluate", "forecast", "predict"],
    "representation": "draws",
    "horizon_dim": "time",
    "future_required": ["spend"],
    "fit_coords": {
        "geo": {"kind": "categorical", "size": 3, "values": ["de", "fr", "it"]},
        "channel": {"kind": "categorical", "size": 2, "values": ["search", "tv"]},
        "time": {"kind": "time", "size": 730, "start": "2024-10-01", "end": "2026-09-30",
                 "freq": "D", "contiguous": True},
    },
    "conditions_on_actuals": False,
}  # fmt: skip


def model_inputs() -> dict[str, object]:
    snapshot = EndpointSchemaSnapshot.model_validate(SNAPSHOT)
    doc = ModelInputSchema(
        model_ref="model://default/eu-geo-mmm@v1",
        snapshot=snapshot,
        input_table=input_table(snapshot, periods=8),
    )
    return doc.model_dump(mode="json", exclude_none=True)


def build() -> str:
    catalog = default_registry().catalog().model_dump(mode="json", exclude_none=True)
    # The server's digest is the runner image's; a content hash stands in for it here.
    digest = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
    doc = {
        "modelspec_schema": export_schemas()["modelspec.json"],
        "catalog": {**catalog, "digest": f"sha256:{digest}", "generated_at": GENERATED_AT},
        "model_inputs": model_inputs(),
    }
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def build_examples() -> str:
    examples = default_registry().plot_examples().model_dump(mode="json", exclude_none=True)
    for spec in examples["plots"].values():
        spec["provenance"]["generated_at"] = GENERATED_AT
    return json.dumps(examples, separators=(",", ":"), sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args()
    stale = 0
    for out, text in ((OUT, build()), (EXAMPLES_OUT, build_examples())):
        if args.check:
            if not out.exists() or out.read_text("utf-8") != text:
                print(f"{out.relative_to(WEB)} is stale: run this script", file=sys.stderr)
                stale = 1
            continue
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out.relative_to(WEB)} ({len(text):,} bytes)")
    return stale


if __name__ == "__main__":
    raise SystemExit(main())
