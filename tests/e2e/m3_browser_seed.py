"""Seed and clean up for the M3 browser journey (``apps/web/e2e/m3-guided.e2e.spec.ts``).

``seed --run <id>`` creates, as the platform operator (``admin@dev.local``, the signed-in
account of the browser suite), a fresh project ``m3ui-<id>`` in the ``default`` tenant (an
``internal`` tenant: the dev stack's local executor runs it) and writes the analyst's
spreadsheet - a weekly CSV of revenue, spend per channel and price for five regions over two
years - under ``$MAPI_DATA_DIR/e2e/m3ui-<id>/``. It prints one JSON line:
``{tenant, tenant_name, project, project_name, csv}``.

``cleanup --project <id>`` deletes the project's endpoints and the project.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from mapi_client import Client
from mapi_client.oidc import dev_idp_login
from mapi_conformance.fixtures.guided_mmm.data import SIZES, _simulate
from mapi_spec.localdev import LocalDev

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = "admin@dev.local"
TENANT = "default"
COLUMNS = {
    "week": "Week",
    "region": "Region",
    "revenue": "Revenue",
    "tv": "TV Spend",
    "search": "Search Spend",
    "social": "Social Spend",
    "avg_price": "Avg Price",
}


def _local() -> tuple[LocalDev, Path]:
    local = LocalDev.from_environ(os.environ)
    data_dir = Path(local.data_dir)
    return local, data_dir if data_dir.is_absolute() else ROOT / data_dir


def write_spreadsheet(path: Path, *, size: str = "full") -> Path:
    """The analyst's CSV (the ``guided_mmm`` simulation, named the way a planner would)."""
    ds, _ = _simulate(0, size, SIZES[size]["weeks"])  # type: ignore[arg-type]
    df = ds.to_dataframe().reset_index()
    spend = df.pivot_table(index=["week", "region"], columns="channel", values="spend")
    rest = df.groupby(["week", "region"])[["revenue", "avg_price"]].first()
    table = rest.join(spend).reset_index().rename(columns=COLUMNS)
    table["Week"] = table["Week"].dt.strftime("%Y-%m-%d")
    table = table[list(COLUMNS.values())]
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, index=False)
    return path


def seed(run: str) -> dict[str, str]:
    local, data_dir = _local()
    api = local.api_url.rstrip("/")
    with Client(api, token=dev_idp_login(api, OPERATOR)) as op:
        tenant = op.tenants.get(TENANT)
        project = op.tenants.create_project(TENANT, f"m3ui-{run}", f"Guided test {run}")
    csv = write_spreadsheet(data_dir / "e2e" / f"m3ui-{run}" / "weekly_media_by_region.csv")
    return {
        "tenant": TENANT,
        "tenant_name": tenant.name,
        "project": project.id,
        "project_name": project.name,
        "csv": str(csv),
    }


def cleanup(project: str) -> None:
    local, _ = _local()
    api = local.api_url.rstrip("/")
    with Client(api, token=dev_idp_login(api, OPERATOR)) as op:
        op.use(f"{TENANT}/{project}")
        for ep in op.endpoints.list():
            with contextlib.suppress(Exception):
                op.endpoints.delete(ep.alias)
        with contextlib.suppress(Exception):
            op.tenants.delete_project(TENANT, project)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--run", required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--project", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "seed":
        print(json.dumps(seed(args.run)))
    else:
        cleanup(args.project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
