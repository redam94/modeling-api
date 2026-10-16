"""Seed and clean up for the M3.5 browser journey (``apps/web/e2e/m35-live.e2e.spec.ts``).

``seed --run <id>`` signs in as the platform operator (``admin@dev.local``, the signed-in
account of the browser suite), creates the project ``m35ui-<id>`` in the ``default`` tenant
with ``analyst@dev.local`` as a reviewer, uploads the journey's data (see
:mod:`m35_stack`), creates the readiness workflow from the ``ts_readiness_arimax`` template
with ``price`` as its default driver, fits a statsmodels VAR, a tiny ``mmm.bayesian`` and an
ARIMAX on pure noise, and validates the noise model against a policy requiring the null
tests (it fails). The fits run concurrently. It prints one JSON line:
``{tenant, tenant_name, project, project_name, datasets, models, workflow, validation}``.

``cleanup --project <id>`` deletes the project's endpoints and the project.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import m35_stack as m35


def seed(run: str) -> dict[str, Any]:
    with m35.sign_in(m35.OPERATOR) as op:
        world = m35.create_world(op, "m35ui", run)
        p = world.project
        _, noise = m35.signal_and_noise()
        for name, ds in (
            ("weekly_sales", m35.unit_root_series()),
            ("noise", noise),
            ("macro", m35.macro_series()),
            ("geo_panel", m35.geo_panel()),
        ):
            m35.upload(op, world, name, ds)
        m35.fit_all(
            op,
            world,
            {
                "macro-var": m35.var_spec(p),
                "geo-mmm": m35.mmm_spec(p),
                "arimax-noise": m35.arimax_spec(p, "arimax-noise", "noise"),
            },
        )
        template = op.workflows.template("ts_readiness_arimax")
        doc: dict[str, Any] = json.loads(json.dumps(template.document))
        doc["metadata"] = {**doc["metadata"], "name": "weekly-readiness", "project": p}
        doc["params"]["drivers"]["default"] = ["price"]
        workflow = op.workflows.create(doc, message="from the gallery, price as the driver")
        op.governance.create_policy(**m35.NULL_POLICY)
        validation = op.models.validate(world.models["arimax-noise"], 1, wait=True)
        assert validation.status.value == "checks_failed", validation.status
        return {
            "tenant": m35.TENANT,
            "tenant_name": world.tenant_name,
            "project": p,
            "project_name": world.project_name,
            "datasets": world.datasets,
            "models": world.models,
            "workflow": workflow.id,
            "validation": validation.id,
            "timings": world.timings,
        }


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
        m35.cleanup(args.project)
    return 0


if __name__ == "__main__":
    sys.exit(main())
