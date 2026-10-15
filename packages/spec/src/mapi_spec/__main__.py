"""``python -m mapi_spec`` — small utilities (schema export, validate a document file)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mapi_spec.errors import MapiError
from mapi_spec.jsonschema_export import export_schemas
from mapi_spec.loader import canonical_json, load_document, spec_hash


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mapi_spec")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export-schemas", help="write JSON Schemas into a directory")
    ex.add_argument("out_dir", type=Path)
    chk = sub.add_parser("check", help="parse a YAML/JSON document; print kind and hash")
    chk.add_argument("path", type=Path)
    chk.add_argument("--canonical", action="store_true", help="print canonical JSON")
    args = parser.parse_args(argv)

    if args.cmd == "export-schemas":
        names = export_schemas(args.out_dir)
        print(f"wrote {len(names)} schemas to {args.out_dir}")
        return 0
    try:
        doc = load_document(args.path.read_bytes())
    except MapiError as exc:
        print(json.dumps(exc.to_problem().model_dump(mode="json"), indent=2), file=sys.stderr)
        return 1
    if args.canonical:
        print(canonical_json(doc))
    else:
        print(f"{doc.kind} {spec_hash(doc)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
