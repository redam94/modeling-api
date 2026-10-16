"""Drift guard for the frozen contract (plan §13 "Drift"; M0 exit: contract v0.1 frozen).

``snapshots/contract.json`` pins ``CONTRACT_VERSION`` and a digest of every exported JSON
Schema. A schema change fails this test until the snapshot is regenerated on purpose:

* additive change (new optional field, new code): regenerate with
  ``MAPI_UPDATE_SNAPSHOTS=1 uv run pytest packages/spec/tests/test_contract_snapshot.py``;
* breaking change: bump ``mapi_spec.CONTRACT_VERSION`` first, then regenerate.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from mapi_spec import CONTRACT_VERSION, export_schemas

SNAPSHOT = Path(__file__).parent / "snapshots" / "contract.json"


def _digests() -> dict[str, str]:
    out = {}
    for name, schema in sorted(export_schemas().items()):
        text = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        out[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return out


def test_contract_version_is_pinned_in_every_exported_schema() -> None:
    assert CONTRACT_VERSION == "0.2"
    for name, schema in export_schemas().items():
        assert schema["x-mapi-contract-version"] == CONTRACT_VERSION, name


def test_exported_schemas_match_the_contract_snapshot() -> None:
    current = {"contract_version": CONTRACT_VERSION, "schemas": _digests()}
    if os.environ.get("MAPI_UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", "utf-8")
    pinned = json.loads(SNAPSHOT.read_text("utf-8"))
    assert pinned["contract_version"] == CONTRACT_VERSION, (
        "CONTRACT_VERSION changed: regenerate the snapshot (MAPI_UPDATE_SNAPSHOTS=1)"
    )
    changed = sorted(
        n
        for n in set(pinned["schemas"]) | set(current["schemas"])
        if pinned["schemas"].get(n) != current["schemas"].get(n)
    )
    assert not changed, (
        f"contract v{CONTRACT_VERSION} schemas changed: {changed}. Additive change: "
        "regenerate with MAPI_UPDATE_SNAPSHOTS=1; breaking change: bump CONTRACT_VERSION."
    )
