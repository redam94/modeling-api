"""v0.5 with the Python client: Help me choose, the workflow cookbook end to end, and the
governance record it leaves.

Included by the "What's new in 0.5", "Workflow cookbook" and "Choosing a model" pages. It
needs a running platform (``MAPI_URL``, ``mapi login`` or ``MAPI_TOKEN``, ``MAPI_PROJECT``),
the weekly KPI as ``dataset://growth/weekly_sales`` and ``weekly-readiness.yaml`` (the
cookbook document) in the working directory. ``packages/server/tests/test_docs_client_v05.py``
runs it, unchanged, against a real server.
"""

from __future__ import annotations

# --8<-- [start:choose]
from mapi_client import Client

api = Client()  # MAPI_URL, the `mapi login` sign-in (or MAPI_TOKEN) and MAPI_PROJECT

result = api.catalog.recommend(
    goal="forecast", data_shape="one_series", drivers="yes", experience="beginner", limit=3
)
for item in result.items:  # best first, with the reasons in plain words
    print(f"{item.score:.0%}  {item.title}  ({item.kind.value} {item.id})")
    for reason in item.reasons_plain:
        print("     +", reason)
    for caution in item.cautions_plain:
        print("     !", caution)

card = api.catalog.about("ts.sarimax", kind="model")  # any item's About card
print(card.summary_plain)
print("use it when:", "; ".join(card.use_when))
print("pick something else when:", "; ".join(card.avoid_when))
# --8<-- [end:choose]

# --8<-- [start:validate]
from pathlib import Path

check = api.workflows.validate(Path("weekly-readiness.yaml"))  # on the server; saves nothing
for issue in check.issues:  # every problem with a JSON pointer into the document
    print(issue.severity.value, issue.pointer, issue.message)
print(" -> ".join(check.order))  # the order the nodes will run in
# --8<-- [end:validate]

# --8<-- [start:run]
from mapi_client import WorkflowEnd, WorkflowEventFrame, WorkflowSnapshot

# create the workflow, or save the file as its next revision when it changed
workflow, revision = api.workflows.upsert(Path("weekly-readiness.yaml"))
run = api.workflows.run(
    workflow.id,
    inputs={"data": "dataset://growth/weekly_sales@latest"},  # pinned to a version at start
    params={"drivers": ["price"], "window": 80, "max_p": 2, "max_q": 2},
)
for item in api.workflows.stream_events(run.run_id):  # server-sent events, resumable
    match item:
        case WorkflowSnapshot(run=now):
            print(f"{len(now.nodes)} nodes, {now.status.value}")
        case WorkflowEventFrame(event=e) if e.type.value == "workflow.branch.decided":
            print(f"  {e.node_id}: took the {e.data['arm']} arm")
        case WorkflowEventFrame(event=e) if e.node_id is not None:
            print(f"  {e.node_id}: {e.type.value.rsplit('.', 1)[-1]}")
        case WorkflowEnd(status=status):
            print("run", status.value if status else "ended")
# --8<-- [end:run]

# --8<-- [start:inspect]
run = api.workflows.wait(run.run_id)  # raises the run's error if it failed
nodes = {n.node_id: n for n in run.nodes}
print(nodes["adf"].result["verdict_plain"])  # e.g. "The series wanders: consider differencing it."
for d in run.decisions:
    print(d.node_id, d.arm, d.inputs)  # every branch with the values its condition read
order, d = nodes["order"].result, 1 if nodes["wanders"].result["value"] else 0
print(f"ARIMA({order['best_p']}, {d}, {order['best_q']})")  # chosen by information criterion
print(nodes["dm"].result["verdict_plain"])  # is ARIMAX's edge over naive more than luck?
print(nodes["select"].result["reason_plain"])  # kept only if it beats the benchmarks
model_ref = run.outputs["model"].ref  # model://growth/wf-weekly-readiness-fit@v1
report = run.outputs["report"].inline  # mapi.report/v1: every test, decision and table
# --8<-- [end:inspect]

# --8<-- [start:governance]
gov = nodes["governance"].result  # the policy in force, measured on the selected model
print("passed" if gov["passed"] else f"failed: {gov['failed_checks']}")
validation = api.validations.get(gov["validation_id"])  # recorded because submit: true
print(validation.status.value)  # validated, or awaiting_approval when a reviewer must sign
for r in validation.results:
    print(f"{r.check:28} {r.status.value:7} {r.message_plain}")
api.validations.download_evidence_pack(validation.id, "evidence.html")  # for the auditor
# --8<-- [end:governance]

# --8<-- [start:automate]
weekly = api.workflows.schedule(
    workflow.id,
    "0 6 * * 1",  # Mondays at 06:00
    timezone="Europe/Berlin",
    inputs={"data": "dataset://growth/weekly_sales@latest"},
    params={"drivers": ["price"]},
)
on_upload = api.workflows.trigger(
    workflow.id, "dataset://growth/weekly_sales", "data", debounce_seconds=600
)
print(weekly.next_run_at, on_upload.id)
# --8<-- [end:automate]

assert check.valid and run.status.value == "succeeded"
assert validation.status.value in ("validated", "awaiting_approval")
assert Path("evidence.html").read_text("utf-8").startswith("<!")
