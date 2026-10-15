"""Seed and clean up a tenant for the M2 browser flow (``apps/web/e2e/m2-flow.e2e.spec.ts``).

``seed --run <id>``: the platform operator creates tenant ``ui-<id>`` (``internal``, so the
local-process executor may run it) with ``MAPI_E2E_UI_ACCOUNT`` (default
``analyst@dev.local``) as its invited admin; that account signs in, creates project
``growth``, uploads the store-demand fixture data (signed PUT → sandboxed ingest) and saves
the fixture's spec. Prints one JSON object: ``tenant``, ``tenant_name``, ``project``,
``project_name``, ``spec_id``, ``spec_name``, ``account``.

``cleanup --tenant <id>``: removes any global spend cap and puts the tenant back on the
``internal`` tier (the browser flow changes both), so an interrupted run leaves the stack
usable.

The stack is found like every e2e suite finds it (``MAPI_API_URL``, see
:class:`mapi_spec.localdev.LocalDev`); it must run in the ``dev`` profile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from mapi_client import Client
from mapi_client.oidc import dev_idp_login
from mapi_conformance.runner import load_fixture
from mapi_conformance.stack import StackRefs, stack_spec_yaml
from mapi_spec.jobs import DATASET_SCHEMA_ATTR
from mapi_spec.localdev import LocalDev

OPERATOR = "admin@dev.local"
FIXTURE = "sklearn_store_demand"
PROJECT = "growth"


def _api() -> str:
    return LocalDev.from_environ(os.environ).api_url.rstrip("/")


def seed(run: str) -> dict[str, str]:
    api = _api()
    account = os.environ.get("MAPI_E2E_UI_ACCOUNT", "analyst@dev.local").strip().lower()
    tenant, tenant_name = f"ui-{run}", f"UI Retail {run}"
    with Client(api, token=dev_idp_login(api, OPERATOR)) as op:
        op.admin.create_tenant(
            {
                "id": tenant,
                "name": tenant_name,
                "trust_tier": "internal",
                "initial_admin_email": account,
            }
        )
    with Client(api, token=dev_idp_login(api, account)) as user:
        project = user.tenants.create_project(tenant, PROJECT, "Store demand")
        user.use(f"{tenant}/{project.id}")
        fx = load_fixture(FIXTURE)
        data, _ = fx.generate(0, "tiny")
        schema = fx.raw_schema(data)
        version = user.datasets.upload(
            data.assign_attrs({DATASET_SCHEMA_ATTR: schema.model_dump_json()}),
            dataset=f"store-sku-weekly-{run}",
            timeout=600,
        )
        if version.status.value != "ready":
            raise SystemExit(f"ingest ended {version.status.value}")
        name = f"store-demand-{run}"
        doc = json.loads(stack_spec_yaml(fx, "tiny", StackRefs(data=version.ref), name=name))
        doc["metadata"]["project"] = PROJECT
        spec = user.specs.create(json.dumps(doc, indent=1), message="m2 browser flow")
    return {
        "tenant": tenant,
        "tenant_name": tenant_name,
        "project": f"{tenant}/{project.id}",
        "project_name": project.name,
        "spec_id": spec.id,
        "spec_name": name,
        "account": account,
    }


def cleanup(tenant: str) -> None:
    api = _api()
    with Client(api, token=dev_idp_login(api, OPERATOR)) as op:
        if op.admin.kill_switches().global_spend_cap_micros is not None:
            op.admin.set_kill_switches({"clear_spend_cap": True, "reason": "m2 browser cleanup"})
        if op.admin.tenant(tenant).trust_tier.value != "internal":
            op.admin.update_tenant(tenant, {"trust_tier": "internal"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed")
    s.add_argument("--run", required=True)
    c = sub.add_parser("cleanup")
    c.add_argument("--tenant", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "seed":
        print(json.dumps(seed(args.run)))
    else:
        cleanup(args.tenant)
    return 0


if __name__ == "__main__":
    sys.exit(main())
