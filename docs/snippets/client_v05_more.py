"""v0.5 with the Python client, continued: explorers, comparisons, budget optimisation,
policies, validations and approvals, async.

Included by the v0.5 pages. It needs a platform with a fitted marketing-mix model and a
reviewer's sign-in, so the docs tests only compile it and check that every name it imports
exists (``conformance/tests/test_docs_v05.py``); the same calls run against a real server in
``packages/server/tests/test_client_m35_governance.py``.
"""

from __future__ import annotations

import os

from mapi_client import Client

api = Client()

# --8<-- [start:data-explorer]
ds = api.datasets.find("weekly_sales")
assert ds is not None and ds.latest_version is not None
api.datasets.describe(ds.id, "Weekly sales and price by region, from the finance warehouse")
print(api.datasets.about(ds.id).summary_generated)  # "Weekly data from ... for 5 regions ..."

explored = api.datasets.explore(  # computed in the sandbox; the same request is cached
    ds.id,
    ds.latest_version,
    sections=["overview", "stationarity", "ccf", "stl", "quality"],
    kpi="sales",
    max_lag=12,
)
for section in explored.index.sections if explored.index else []:
    print(section.section, section.status, section.message_plain or "")
stationarity = api.explorations.view(explored, "stationarity")  # the view, as JSON
# --8<-- [end:data-explorer]

# --8<-- [start:model-explorer]
model_id, version = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0", 3
explored = api.models.explore(
    model_id, version, sections=["params", "residuals", "forecast"], horizon=13
)
residuals = api.explorations.view(explored, "residuals")

compared = api.models.compare(
    ["model://growth/arimax@v3", "model://growth/ets@v1", "model://growth/theta@v2"], horizon=4
)
table = api.explorations.view(compared, "comparison")  # metrics, ranking, Diebold-Mariano
# --8<-- [end:model-explorer]

# --8<-- [start:optimize]
plan = api.models.optimize(  # mmm-framework's planner on the posterior; waits for the plan
    model_id,
    version,
    budget_change_pct=0,  # reallocate today's total
    channels={"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5}},
    objective="p10",  # plan for a cautious outcome, not the average one
    frontier={"n_points": 8},
)
assert plan.result is not None
for a in plan.result.allocation:
    print(f"{a.channel:10} {a.current_spend:>12,.0f} -> {a.optimal_spend:>12,.0f}")
uplift = plan.result.uplift
print("uplift", uplift.mean if uplift else None, "P(better)", plan.result.prob_positive_uplift)
# --8<-- [end:optimize]

# --8<-- [start:policies]
import yaml

with open("forecast-policy.yaml", encoding="utf-8") as fh:
    policy = api.governance.create_policy(yaml.safe_load(fh))
print(policy.id, policy.version, [c.check for c in policy.checks])

# edit and save the next version (refused with 409 if someone saved one meanwhile)
stricter = api.governance.update_policy(
    policy.id, policy.model_copy(update={"approvals_required": 2}), expected_version=1
)
for p in api.governance.policies():  # in force in the project: tenant-wide first
    print(p.scope, p.name, f"v{p.version}")
# --8<-- [end:policies]

# --8<-- [start:validate]
validation = api.models.validate(model_id, version, note="Q4 refresh", wait=True)
print(validation.status.value)  # validated, awaiting_approval or checks_failed
for r in validation.results:
    print(r.check, r.status.value, r.message_plain, r.suggested_action or "")
if validation.status.value == "checks_failed":
    validation = api.validations.resubmit(validation.id, note="refitted on clean data")
# --8<-- [end:validate]

# --8<-- [start:approve]
# a reviewer, signed in with `mapi login` (never the submitter, never an API token)
reviewer = Client(project=os.environ.get("MAPI_PROJECT"))
for pending in reviewer.validations.list(status="awaiting_approval"):
    evidence = list(reviewer.validations.evidence(pending.id))
    if all(r.status.value in ("pass", "skipped") for r in evidence):
        reviewer.validations.approve(pending.id, comment="Evidence reviewed")
    else:
        reviewer.validations.reject(pending.id, "The null test is not convincing yet")
# --8<-- [end:approve]

# --8<-- [start:async]
import asyncio

from mapi_client import AsyncClient


async def main() -> None:
    async with AsyncClient() as aapi:
        wf = await aapi.workflows.find("weekly-readiness")
        assert wf is not None
        run = await aapi.workflows.run(
            wf.id, inputs={"data": "dataset://growth/weekly_sales@latest"}, wait=True
        )
        async for node in aapi.workflows.nodes(run.run_id):
            print(node.node_id, node.status.value)


asyncio.run(main())
# --8<-- [end:async]
