"""Regenerate the schema files vendored into ``mapi_plots.sanitize.schemas``.

The sanitizer runs in the control plane and never fetches anything at runtime, so the pinned
Vega-Lite JSON Schema and a compact structure table derived from the official plotly.js
``plot-schema.json`` are committed to the package. This script is the only way they change::

    uv run python packages/plots-core/scripts/vendor_schemas.py --download
    uv run python packages/plots-core/scripts/vendor_schemas.py \
        --vega-lite path/to/v6.4.1.json --plotly path/to/plot-schema.json

``--download`` fetches both sources from their official URLs (pinned versions below). The
Vega-Lite schema is copied byte for byte; the plotly schema is reduced to the attribute tree
(names, value types, enumerations, arrayOk flags, subplot and container-array markers), with
descriptions and edit metadata dropped and repeated sub-trees interned. ``VENDORED.json``
records the source URLs and SHA-256 digests; a unit test checks them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

VEGA_LITE_VERSION = "6.4.1"
PLOTLY_JS_VERSION = "4.1.1"
VEGA_LITE_URL = f"https://vega.github.io/schema/vega-lite/v{VEGA_LITE_VERSION}.json"
PLOTLY_URL = (
    f"https://raw.githubusercontent.com/plotly/plotly.js/v{PLOTLY_JS_VERSION}/dist/plot-schema.json"
)

OUT = Path(__file__).resolve().parents[1] / "src" / "mapi_plots" / "sanitize" / "schemas"
VL_FILE = f"vega-lite-v{VEGA_LITE_VERSION}.json"
PLOTLY_FILE = f"plotly-structure-v{PLOTLY_JS_VERSION}.json"

_META_KEYS = {"role", "editType", "description", "_isSubplotObj", "_arrayAttrRegexps", "items"}


def _leaf(spec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"t": spec["valType"]}
    if spec.get("arrayOk"):
        out["a"] = 1
    if spec["valType"] == "enumerated":
        out["v"] = spec.get("values", [])
    if spec["valType"] == "flaglist":
        out["f"] = spec.get("flags", [])
        out["x"] = spec.get("extras", [])
    if spec["valType"] == "subplotid":
        out["d"] = spec.get("dflt")
    if spec["valType"] == "info_array" and spec.get("dimensions") in (2, "1-2"):
        out["dim"] = spec["dimensions"]
    return out


def _node(spec: dict[str, Any]) -> dict[str, Any]:
    if "valType" in spec:
        return _leaf(spec)
    keys: dict[str, Any] = {}
    for name, child in spec.items():
        if name in _META_KEYS or name.startswith("_") or not isinstance(child, dict):
            continue
        keys[name] = _node(child)
    out: dict[str, Any] = {"k": keys}
    if spec.get("_isSubplotObj"):
        out["s"] = 1
    items = spec.get("items")
    if isinstance(items, dict) and spec.get("role") == "object":
        # container array (annotations, shapes, images, buttons ...): one item kind
        (item_name, item_spec), *_ = items.items()
        out["arr"] = item_name
        out["k"] = _node(item_spec)["k"]
    return out


class _Interner:
    """Replace repeated sub-trees by ``{"@": index}`` references into a shared table."""

    def __init__(self) -> None:
        self.table: list[Any] = []
        self.counts: dict[str, int] = {}
        self.index: dict[str, int] = {}

    def count(self, node: Any) -> None:
        if isinstance(node, dict):
            key = json.dumps(node, sort_keys=True)
            self.counts[key] = self.counts.get(key, 0) + 1
            if self.counts[key] == 1:
                for v in node.values():
                    self.count(v)

    def intern(self, node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        key = json.dumps(node, sort_keys=True)
        out = {k: self.intern(v) for k, v in node.items()}
        if self.counts.get(key, 0) > 1 and len(key) > 40:
            if key not in self.index:
                self.index[key] = len(self.table)
                self.table.append(out)
            return {"@": self.index[key]}
        return out


def build_plotly_structure(schema: dict[str, Any]) -> dict[str, Any]:
    traces: dict[str, Any] = {}
    layout_extra: dict[str, Any] = {}
    for name, trace in schema["traces"].items():
        traces[name] = _node(trace["attributes"])
        # the schema stores the trace's own type as a bare string; make it an enumeration
        traces[name]["k"]["type"] = {"t": "enumerated", "v": [name]}
        for k, v in trace.get("layoutAttributes", {}).items():
            if isinstance(v, dict):
                layout_extra[k] = _node(v)
    layout = _node(schema["layout"]["layoutAttributes"])
    layout["k"].update({k: v for k, v in layout_extra.items() if k not in layout["k"]})
    config = _node(schema["config"])
    frames = _node(schema["frames"])
    animation = _node(schema["animation"])
    tree = {"traces": traces, "layout": layout, "config": config, "frames": frames}
    tree["animation"] = animation
    interner = _Interner()
    interner.count(tree)
    body = interner.intern(tree)
    return {"version": PLOTLY_JS_VERSION, "table": interner.table, **body}


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        data: bytes = resp.read()
    return data


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--download", action="store_true", help="fetch both sources from upstream")
    p.add_argument("--vega-lite", type=Path, help=f"local copy of {VEGA_LITE_URL}")
    p.add_argument("--plotly", type=Path, help=f"local copy of {PLOTLY_URL}")
    args = p.parse_args(argv)
    if args.download:
        vl_bytes, pl_bytes = _fetch(VEGA_LITE_URL), _fetch(PLOTLY_URL)
    elif args.vega_lite and args.plotly:
        vl_bytes, pl_bytes = args.vega_lite.read_bytes(), args.plotly.read_bytes()
    else:
        p.error("pass --download or both --vega-lite and --plotly")
    json.loads(vl_bytes)  # must parse
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / VL_FILE).write_bytes(vl_bytes)
    structure = build_plotly_structure(json.loads(pl_bytes))
    text = json.dumps(structure, separators=(",", ":"), sort_keys=True)
    (OUT / PLOTLY_FILE).write_text(text + "\n", encoding="utf-8")
    vendored = {
        "vega-lite": {
            "version": VEGA_LITE_VERSION,
            "url": VEGA_LITE_URL,
            "file": VL_FILE,
            "sha256": hashlib.sha256(vl_bytes).hexdigest(),
        },
        "plotly": {
            "version": PLOTLY_JS_VERSION,
            "url": PLOTLY_URL,
            "source_sha256": hashlib.sha256(pl_bytes).hexdigest(),
            "file": PLOTLY_FILE,
            "sha256": hashlib.sha256((text + "\n").encode()).hexdigest(),
        },
    }
    (OUT / "VENDORED.json").write_text(json.dumps(vendored, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {VL_FILE} ({len(vl_bytes)} B) and {PLOTLY_FILE} ({len(text)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
