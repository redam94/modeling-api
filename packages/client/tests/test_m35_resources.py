"""v0.5 resources (About cards and the chooser, explorers, optimisation, workflows,
governance, validations) against a respx-mocked API, sync and async. Responses are the
contract's own examples (``mapi_spec.api`` DTOs), so a drift in either side fails here."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mapi_client import (
    AsyncClient,
    Client,
    ClientConfig,
    WorkflowEnd,
    WorkflowEventFrame,
    WorkflowSnapshot,
)
from mapi_client.errors import TransportError
from mapi_spec.api import (
    CatalogAbout,
    CheckResult,
    ChooserQuestionnaire,
    ChooserResult,
    DatasetAbout,
    EvidencePack,
    Exploration,
    GovernanceCheckDef,
    GovernancePolicy,
    ModelValidation,
    NodeRun,
    Optimization,
    Run,
    Workflow,
    WorkflowRevision,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowTemplate,
    WorkflowTrigger,
    WorkflowValidation,
)
from mapi_spec.errors import ErrorCode, MapiError, Problem
from mapi_spec.storage import crc32c, crc32c_to_base64
from mapi_spec.workflow import WorkflowEvent, workflow_hash
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX, instantiate_template

from .conftest import _example as ex

BASE = "http://api.test"
V1 = f"{BASE}/v1"
STORAGE = "http://storage.test"
HEADER = "X-Mapi-Project"
RUN = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
WF = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0"
VAL = "val_01jaf3k1m2n3p4q5r6s7t8v9w0"
POL = "pol_01jaf3h1j2k3m4n5p6q7r8s9t0"


def page(items: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"items": items, "next_cursor": cursor}


def problem(code: ErrorCode, detail: str, status: int) -> httpx.Response:
    return httpx.Response(status, json=Problem.from_code(code, detail).model_dump(mode="json"))


def body_of(route: respx.Route, n: int = -1) -> Any:
    return json.loads(route.calls[n].request.content)


def link(name: str, data: bytes, path: str) -> dict[str, Any]:
    return {
        "name": name,
        "url": f"{STORAGE}/{path}?sig=1",
        "size": len(data),
        "crc32c": crc32c_to_base64(crc32c(data)),
        "generation": 1,
        "content_type": "application/json",
        "expires_at": "2099-01-01T00:00:00Z",
    }


def _run(state: str, **changes: Any) -> dict[str, Any]:
    return ex(Run, id=RUN, state=state, progress=None, **changes)


def _wf_run(status: str, **changes: Any) -> dict[str, Any]:
    return ex(WorkflowRun, status=status, **changes)


@pytest.fixture
def scoped(api: respx.MockRouter) -> Iterator[Client]:
    cfg = ClientConfig(
        base_url=BASE, project="acme/growth", token="t0k", allow_insecure_token=True, max_retries=0
    )
    with Client(config=cfg, sleep=lambda s: None) as c:
        yield c


@pytest.fixture
async def ascoped(api: respx.MockRouter) -> Any:
    cfg = ClientConfig(
        base_url=BASE, project="acme/growth", token="t0k", allow_insecure_token=True, max_retries=0
    )

    async def nosleep(_: float) -> None:
        return None

    async with AsyncClient(config=cfg, sleep=nosleep) as c:
        yield c


# ============================================================ About cards and the chooser


def test_abouts_chooser_and_recommendations(api: respx.MockRouter, scoped: Client) -> None:
    cards = api.get(f"{V1}/catalog/abouts").respond(
        200, json=page([ex(CatalogAbout, kind="model", id="ts.sarimax")])
    )
    got = list(scoped.catalog.abouts(kind="model"))
    assert got[0].id == "ts.sarimax" and got[0].about.title
    request = cards.calls.last.request
    assert request.url.params["kind"] == "model"
    assert HEADER not in request.headers  # catalog data is not project-scoped
    assert scoped.catalog.about("ts.sarimax", kind="model").title == got[0].about.title
    with pytest.raises(ValueError, match="no catalog item"):
        scoped.catalog.about("ts.nope")

    api.get(f"{V1}/catalog/chooser").respond(200, json=ex(ChooserQuestionnaire))
    assert scoped.catalog.chooser().questions[0].id == "goal"

    rec = api.get(f"{V1}/catalog/recommendations").respond(200, json=ex(ChooserResult))
    result = scoped.catalog.recommend(
        {"goal": "forecast"}, data_shape=["one_series", "panel"], experience="beginner", limit=3
    )
    assert result.items[0].id == "weekly_forecast"
    params = rec.calls.last.request.url.params
    assert params.get_list("data_shape") == ["one_series", "panel"]
    assert params["goal"] == "forecast" and params["limit"] == "3"
    with pytest.raises(ValueError):
        scoped.catalog.recommend(goal="teleport")
    assert rec.call_count == 1  # the bad answer never left the client


def test_node_and_check_catalogs(api: respx.MockRouter, scoped: Client) -> None:
    from mapi_spec.api import NodeImplementation

    api.get(f"{V1}/catalog/workflow-nodes").respond(200, json=page([ex(NodeImplementation)]))
    api.get(f"{V1}/catalog/workflow-nodes/test.adf").respond(200, json=ex(NodeImplementation))
    api.get(f"{V1}/catalog/governance-checks").respond(200, json=page([ex(GovernanceCheckDef)]))
    assert [n.id for n in scoped.catalog.workflow_nodes(kind="test")] == ["test.adf"]
    assert scoped.catalog.workflow_node("test.adf").outputs == {"result": "test_result"}
    assert next(iter(scoped.governance.checks())).id == ex(GovernanceCheckDef)["id"]


# ===================================================================== datasets, explorers


def test_dataset_about_and_description(api: respx.MockRouter, scoped: Client) -> None:
    api.get(f"{V1}/datasets/ds_1/about").respond(200, json=ex(DatasetAbout))
    patch = api.patch(f"{V1}/datasets/ds_1/about").respond(200, json=ex(DatasetAbout))
    assert scoped.datasets.about("ds_1").summary_generated
    scoped.datasets.describe("ds_1", "Weekly sales by region")
    assert body_of(patch) == {"description_user": "Weekly sales by region"}
    assert patch.calls.last.request.headers[HEADER] == "acme/growth"


def _exploration(done: bool, views: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data = ex(Exploration, done=done, run=_run("succeeded" if done else "running", kind="explore"))
    if views is not None:
        data["views"] = views
    if not done:
        data["index"] = None
        data["views"] = []
    return data


def test_explore_waits_for_the_views_and_reads_them(api: respx.MockRouter, scoped: Client) -> None:
    view = {"format": "mapi.stationarity/v1", "series": [{"adf_p": 0.4}]}
    blob = json.dumps(view).encode()
    done = _exploration(True, [link("ui/explore/stationarity.json", blob, "x/stationarity.json")])
    start = api.post(f"{V1}/datasets/ds_1/versions/4/explore").respond(
        202, json=_exploration(False)
    )
    polls = api.get(f"{V1}/explorations/{RUN}").mock(
        side_effect=[httpx.Response(200, json=_exploration(False)), httpx.Response(200, json=done)]
    )
    api.get(f"{STORAGE}/x/stationarity.json").respond(200, content=blob)
    got = scoped.datasets.explore("ds_1", 4, sections=["stationarity"], kpi="revenue")
    assert got.done and polls.call_count == 2
    assert body_of(start)["sections"] == ["stationarity"] and body_of(start)["kpi"] == "revenue"
    assert start.calls.last.request.headers["Idempotency-Key"]
    assert scoped.explorations.view(got, "stationarity") == view
    with pytest.raises(ValueError, match=r"has no section"):
        scoped.explorations.view(got, "stl")
    # the index says why a section is missing
    skipped = Exploration.model_validate(done)
    assert skipped.index is not None
    msg = "Less than two seasons of data."
    idx = skipped.index.model_copy(
        update={
            "sections": [
                *skipped.index.sections,
                type(skipped.index.sections[0])(
                    section="stl", view="ui/explore/stl.json", status="skipped", message_plain=msg
                ),
            ]
        }
    )
    with pytest.raises(ValueError, match="Less than two seasons"):
        scoped.explorations.view(skipped.model_copy(update={"index": idx}), "stl")


def test_a_failed_exploration_raises_its_problem(api: respx.MockRouter, scoped: Client) -> None:
    failed = _exploration(False)
    failed["run"] = _run(
        "failed",
        kind="explore",
        problem=Problem.from_code(ErrorCode.EXPLORE_INVALID, "no such var").model_dump(),
    )
    api.post(f"{V1}/models/mdl_1/versions/3/explore").respond(202, json=failed)
    with pytest.raises(MapiError) as info:
        scoped.models.explore("mdl_1", 3, sections=["irf"], horizon=12)
    assert info.value.code is ErrorCode.EXPLORE_INVALID
    got = scoped.models.explore("mdl_1", 3, sections=["irf"], raise_on_failure=False)
    assert got.run.state.value == "failed"


def test_compare_sends_the_model_refs(api: respx.MockRouter, scoped: Client) -> None:
    route = api.post(f"{V1}/models:compare").respond(200, json=_exploration(True))
    refs = ["model://growth/a@v1", "model://growth/b@v2"]
    got = scoped.models.compare(refs, horizon=8)
    assert got.done and body_of(route)["models"] == refs and body_of(route)["horizon"] == 8


def test_optimize_repeats_the_keyed_request_until_the_plan_is_ready(
    api: respx.MockRouter, scoped: Client
) -> None:
    done = ex(Optimization)
    running = {**done, "done": False, "result": None, "run": _run("running", kind="optimize")}
    route = api.post(f"{V1}/models/mdl_1/versions/3:optimize").mock(
        side_effect=[httpx.Response(202, json=running), httpx.Response(200, json=done)]
    )
    plan = scoped.models.optimize("mdl_1", 3, total_budget=1_000_000, objective="p10", wait=5)
    assert plan.result is not None and plan.result.allocation
    keys = {c.request.headers["Idempotency-Key"] for c in route.calls}
    assert len(keys) == 1 and route.calls[0].request.headers["Prefer"] == "wait=5"
    assert body_of(route)["total_budget"] == 1_000_000 and body_of(route)["objective"] == "p10"
    api.get(f"{V1}/optimizations/{RUN}").respond(200, json=done)
    assert scoped.models.optimization(RUN).done


def test_an_infeasible_plan_is_a_typed_error(api: respx.MockRouter, scoped: Client) -> None:
    api.post(f"{V1}/models/mdl_1/versions/3:optimize").mock(
        return_value=problem(ErrorCode.OPTIMIZE_INFEASIBLE, "bounds exceed the budget", 422)
    )
    with pytest.raises(MapiError) as info:
        scoped.models.optimize("mdl_1", 3, total_budget=10)
    assert info.value.code is ErrorCode.OPTIMIZE_INFEASIBLE


# ============================================================================== workflows


def _doc() -> dict[str, Any]:
    spec = instantiate_template(TS_READINESS_ARIMAX, name="weekly", project="growth")
    return spec.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_create_validate_and_revise_from_files(
    api: respx.MockRouter, scoped: Client, tmp_path: Path
) -> None:
    path = tmp_path / "wf.yaml"
    path.write_text("apiVersion: mapi/v1\nkind: Workflow\n", "utf-8")
    val = api.post(f"{V1}/workflows:validate").respond(200, json=ex(WorkflowValidation))
    assert scoped.workflows.validate(path).issues[0].pointer  # every issue has a pointer
    assert body_of(val) == {"text": "apiVersion: mapi/v1\nkind: Workflow\n"}
    create = api.post(f"{V1}/workflows").respond(201, json=ex(Workflow))
    scoped.workflows.create(_doc(), message="first")
    assert body_of(create)["document"]["metadata"]["name"] == "weekly"
    scoped.workflows.create(template="ts_readiness_arimax")
    assert body_of(create) == {"template": "ts_readiness_arimax"}
    with pytest.raises(ValueError):
        scoped.workflows.create(_doc(), template="ts_readiness_arimax")
    rev = api.post(f"{V1}/workflows/{WF}/revisions").respond(201, json=ex(WorkflowRevision))
    scoped.workflows.revise(WF, path, expected_revision=1)
    assert body_of(rev)["expected_revision"] == 1 and "text" in body_of(rev)


def test_upsert_adds_a_revision_only_when_the_document_changed(
    api: respx.MockRouter, scoped: Client
) -> None:
    doc = _doc()
    wf = ex(Workflow, id=WF, name="weekly", latest_revision=2)
    api.get(f"{V1}/workflows").respond(200, json=page([wf]))
    same = ex(WorkflowRevision, workflow_id=WF, revision=2, spec_hash=workflow_hash(doc))
    api.get(f"{V1}/workflows/{WF}/revisions/2").respond(200, json=same)
    rev = api.post(f"{V1}/workflows/{WF}/revisions").respond(201, json=ex(WorkflowRevision))
    _, got = scoped.workflows.upsert(doc)
    assert got.revision == 2 and not rev.called
    api.get(f"{V1}/workflows/{WF}").respond(200, json=wf)
    changed = {**doc, "params": {**doc.get("params", {})}}
    changed["nodes"] = doc["nodes"][:-1]
    scoped.workflows.upsert(changed, message="drop the report")
    assert rev.called and body_of(rev)["expected_revision"] == 2


def test_run_and_wait_until_the_run_succeeds(api: respx.MockRouter, scoped: Client) -> None:
    start = api.post(f"{V1}/workflows/{WF}/runs").respond(202, json=_wf_run("queued"))
    api.get(f"{V1}/workflow-runs/{RUN}").mock(
        side_effect=[
            httpx.Response(200, json=_wf_run("running")),
            httpx.Response(200, json=_wf_run("succeeded")),
        ]
    )
    run = scoped.workflows.run(
        WF,
        inputs={"data": "dataset://growth/sales@latest"},
        params={"alpha": 0.01},
        wait=True,
    )
    assert run.status.value == "succeeded"
    sent = body_of(start)
    assert sent["inputs"] == {"data": "dataset://growth/sales@latest"}
    assert sent["params"] == {"alpha": 0.01} and sent["use_cache"] is True
    assert start.calls.last.request.headers["Idempotency-Key"]


def test_a_failed_run_names_the_failed_node(api: respx.MockRouter, scoped: Client) -> None:
    failed = _wf_run("failed")
    err = {"code": "USER_CODE_ERROR", "message": "singular matrix"}
    failed["error"] = err
    failed["nodes"][0] = {**failed["nodes"][0], "status": "failed", "error": err}
    api.get(f"{V1}/workflow-runs/{RUN}").respond(200, json=failed)
    with pytest.raises(MapiError, match="singular matrix") as info:
        scoped.workflows.wait(RUN)
    assert info.value.code is ErrorCode.USER_CODE_ERROR
    assert scoped.workflows.wait(RUN, raise_on_failure=False).status.value == "failed"


def _sse(*frames: tuple[str, str, int | None]) -> bytes:
    out = []
    for event, data, ident in frames:
        head = f"id: {ident}\n" if ident is not None else "retry: 10\n"
        out.append(f"{head}event: {event}\ndata: {data}\n\n")
    return ("".join(out)).encode()


def _event(seq: int, type_: str, node: str | None = None) -> str:
    return json.dumps(
        ex(WorkflowEvent, seq=seq, type=type_, node_id=node, run_id=RUN, data={"n": seq})
    )


def test_stream_events_resumes_without_repeating_events(
    api: respx.MockRouter, scoped: Client
) -> None:
    snapshot = json.dumps(_wf_run("running"))
    first = _sse(
        ("snapshot", snapshot, None),
        ("workflow.run.started", _event(1, "workflow.run.started"), 1),
        ("workflow.node.started", _event(2, "workflow.node.started", "adf"), 2),
    )
    second = _sse(
        ("snapshot", snapshot, None),
        ("workflow.node.started", _event(2, "workflow.node.started", "adf"), 2),
        ("workflow.future.thing", '{"x": 1}', 3),  # a newer server's type: skipped
        ("workflow.node.succeeded", _event(4, "workflow.node.succeeded", "adf"), 4),
        ("end", json.dumps({"run_id": RUN, "status": "succeeded"}), None),
    )
    sse = {"content-type": "text/event-stream"}
    route = api.get(f"{V1}/workflow-runs/{RUN}/events").mock(
        side_effect=[
            httpx.Response(200, content=first, headers=sse),
            httpx.Response(200, content=second, headers=sse),
        ]
    )
    items = list(scoped.workflows.stream_events(RUN))
    kinds = [type(i).__name__ for i in items]
    assert kinds == [
        "WorkflowSnapshot",
        "WorkflowEventFrame",
        "WorkflowEventFrame",
        "WorkflowSnapshot",
        "WorkflowEventFrame",
        "WorkflowEnd",
    ]
    frames = [i for i in items if isinstance(i, WorkflowEventFrame)]
    assert [f.id for f in frames] == [1, 2, 4]
    assert frames[2].node_id == "adf" and frames[2].type == "workflow.node.succeeded"
    assert isinstance(items[0], WorkflowSnapshot) and items[0].run.nodes
    end = items[-1]
    assert isinstance(end, WorkflowEnd) and end.status is not None
    assert end.status.value == "succeeded"
    second_call = route.calls[1].request
    assert second_call.headers["Last-Event-ID"] == "2"
    assert second_call.headers["Accept"] == "text/event-stream"
    assert second_call.headers[HEADER] == "acme/growth"


def test_stream_events_gives_up_after_repeated_empty_connections(
    api: respx.MockRouter, scoped: Client
) -> None:
    api.get(f"{V1}/workflow-runs/{RUN}/events").respond(
        200, content=b": keepalive\n\n", headers={"content-type": "text/event-stream"}
    )
    with pytest.raises(TransportError, match="ended before the run did"):
        list(scoped.workflows.stream_events(RUN, max_reconnects=2))
    api.get(f"{V1}/workflow-runs/run_x/events").mock(
        return_value=problem(ErrorCode.NOT_FOUND, "no such run", 404)
    )
    with pytest.raises(MapiError):
        list(scoped.workflows.stream_events("run_x"))


def test_events_pages_nodes_and_outputs(api: respx.MockRouter, scoped: Client) -> None:
    events = api.get(f"{V1}/workflow-runs/{RUN}/events").mock(
        side_effect=[
            httpx.Response(200, json=page([json.loads(_event(1, "workflow.run.started"))], "x")),
            httpx.Response(200, json=page([json.loads(_event(2, "workflow.node.started"))])),
        ]
    )
    assert [e.seq for e in scoped.workflows.events(RUN)] == [1, 2]
    assert events.calls[1].request.url.params["after"] == "1"
    api.get(f"{V1}/workflow-runs/{RUN}/nodes").respond(200, json=page([ex(NodeRun)]))
    api.get(f"{V1}/workflow-runs/{RUN}/nodes/adf").respond(200, json=ex(NodeRun))
    assert [n.node_id for n in scoped.workflows.nodes(RUN, status="succeeded")] == ["adf"]
    assert scoped.workflows.node(RUN, "adf").result
    blob = b"PK\x03\x04zarr"
    lk = link("data.zarr.zip", blob, "out/data.zarr.zip")
    api.get(f"{V1}/workflow-runs/{RUN}/nodes/diff/outputs/data").respond(200, json=lk)
    storage = api.get(f"{STORAGE}/out/data.zarr.zip").respond(200, content=blob)
    assert scoped.workflows.download_output(RUN, "diff", "data") == blob
    assert "authorization" not in storage.calls.last.request.headers
    cancel = api.post(f"{V1}/workflow-runs/{RUN}:cancel").respond(202, json=_wf_run("cancelling"))
    assert scoped.workflows.cancel(RUN).status.value == "cancelling" and cancel.called


def test_schedules_triggers_and_gallery(api: respx.MockRouter, scoped: Client) -> None:
    sched = api.post(f"{V1}/workflows/{WF}/schedules").respond(201, json=ex(WorkflowSchedule))
    scoped.workflows.schedule(
        WF, "0 6 * * 1", timezone="Europe/Berlin", inputs={"data": "dataset://growth/s@latest"}
    )
    assert body_of(sched)["cron"] == "0 6 * * 1" and body_of(sched)["timezone"] == "Europe/Berlin"
    upd = api.patch(f"{V1}/workflows/{WF}/schedules/wsc_1").respond(200, json=ex(WorkflowSchedule))
    scoped.workflows.update_schedule(WF, "wsc_1", active=False)
    assert body_of(upd) == {"active": False}
    api.delete(f"{V1}/workflows/{WF}/schedules/wsc_1").respond(204)
    scoped.workflows.delete_schedule(WF, "wsc_1")
    trig = api.post(f"{V1}/workflows/{WF}/triggers").respond(201, json=ex(WorkflowTrigger))
    scoped.workflows.trigger(WF, "dataset://growth/weekly_sales", "data", debounce_seconds=600)
    assert body_of(trig)["debounce_seconds"] == 600 and body_of(trig)["input"] == "data"
    api.get(f"{V1}/workflow-templates").respond(200, json=page([ex(WorkflowTemplate)]))
    gallery = list(scoped.workflows.templates())
    assert gallery[0].about.summary_plain


# ============================================================== governance, validations


def test_policies_project_and_tenant(api: respx.MockRouter, scoped: Client) -> None:
    pol = ex(GovernancePolicy)
    create = api.post(f"{V1}/governance/policies").respond(201, json=pol)
    scoped.governance.create_policy(
        name="Forecasts we publish",
        checks=[
            "baseline.beats_naive",
            {"check": "holdout.accuracy", "thresholds": {"warn": 0.1, "fail": 0.2}},
        ],
        applies_to={"families": ["ts"]},
        require_approval=True,
    )
    sent = body_of(create)
    assert sent["checks"][0]["check"] == "baseline.beats_naive"
    assert sent["applies_to"]["families"] == ["ts"] and sent["require_approval"] is True
    with pytest.raises(ValueError):
        scoped.governance.create_policy(name="x", checks=["null.does_not_exist"])
    put = api.put(f"{V1}/governance/policies/{POL}").respond(200, json=pol)
    scoped.governance.update_policy(POL, pol | {"name": "Stricter"}, expected_version=3)
    assert body_of(put)["expected_version"] == 3 and body_of(put)["name"] == "Stricter"
    tenant = api.get(f"{V1}/tenants/acme/governance/policies").respond(200, json=page([pol]))
    assert len(list(scoped.governance.policies(tenant_id="acme"))) == 1
    assert HEADER not in tenant.calls.last.request.headers
    api.delete(f"{V1}/tenants/acme/governance/policies/{POL}").respond(204)
    scoped.governance.retire_policy(POL, tenant_id="acme")
    with pytest.raises(ValueError):
        scoped.governance.policies(scope="project", tenant_id="acme")


def test_validation_request_waits_and_reviewers_decide(
    api: respx.MockRouter, scoped: Client
) -> None:
    running = ex(ModelValidation, status="checks_running", results=[])
    final = ex(ModelValidation)
    req = api.post(f"{V1}/models/mdl_1/versions/7/validations").respond(202, json=running)
    api.get(f"{V1}/validations/{VAL}").mock(
        side_effect=[httpx.Response(200, json=running), httpx.Response(200, json=final)]
    )
    got = scoped.models.validate("mdl_1", 7, note="Q4 refresh", wait=True)
    assert got.status.value == "awaiting_approval"
    assert body_of(req) == {"note": "Q4 refresh", "policy_ids": []}
    decide = api.post(f"{V1}/validations/{VAL}/approvals").respond(200, json=final)
    scoped.validations.approve(VAL, comment="convincing")
    assert body_of(decide) == {"decision": "approve", "comment": "convincing"}
    decide.mock(return_value=problem(ErrorCode.SELF_APPROVAL_FORBIDDEN, "not your own", 403))
    with pytest.raises(MapiError) as info:
        scoped.validations.reject(VAL, "no")
    assert info.value.code is ErrorCode.SELF_APPROVAL_FORBIDDEN
    queue = api.get(f"{V1}/validations").respond(200, json=page([final]))
    assert len(list(scoped.validations.list(status="awaiting_approval"))) == 1
    assert queue.calls.last.request.url.params["status"] == "awaiting_approval"
    api.get(f"{V1}/validations/{VAL}/evidence").respond(200, json=page([ex(CheckResult)]))
    assert next(iter(scoped.validations.evidence(VAL))).message_plain


def test_evidence_pack_downloads_as_html_or_json(
    api: respx.MockRouter, scoped: Client, tmp_path: Path
) -> None:
    html = b"<!doctype html><title>evidence</title>"
    pack = ex(EvidencePack)
    with_link = {**pack, "download": link("evidence.html", html, "packs/evidence.html")}
    route = api.post(f"{V1}/validations/{VAL}/evidence-pack").mock(
        side_effect=[httpx.Response(200, json=with_link), httpx.Response(200, json=pack)]
    )
    api.get(f"{STORAGE}/packs/evidence.html").respond(200, content=html)
    out = scoped.validations.download_evidence_pack(VAL, tmp_path / "pack.html")
    assert out.read_bytes() == html and body_of(route, 0)["format"] == "html"
    out = scoped.validations.download_evidence_pack(VAL, tmp_path / "pack.json")
    assert json.loads(out.read_text())["format"] == "mapi.evidence-pack/v1"
    assert body_of(route, 1)["format"] == "json"


# ================================================================================== async


async def test_async_recommend_optimize_and_run(api: respx.MockRouter, ascoped: Any) -> None:
    c: AsyncClient = ascoped
    api.get(f"{V1}/catalog/recommendations").respond(200, json=ex(ChooserResult))
    assert (await c.catalog.recommend(goal="forecast")).items
    done = ex(Optimization)
    running = {**done, "done": False, "result": None, "run": _run("running", kind="optimize")}
    route = api.post(f"{V1}/models/mdl_1/versions/3:optimize").mock(
        side_effect=[httpx.Response(202, json=running), httpx.Response(200, json=done)]
    )
    plan = await c.models.optimize("mdl_1", 3, total_budget=5.0)
    assert plan.done and len({r.request.headers["Idempotency-Key"] for r in route.calls}) == 1
    api.post(f"{V1}/workflows/{WF}/runs").respond(202, json=_wf_run("queued"))
    api.get(f"{V1}/workflow-runs/{RUN}").respond(200, json=_wf_run("succeeded"))
    run = await c.workflows.run(WF, inputs={"data": "dataset://g/s@v1"}, wait=True)
    assert run.status.value == "succeeded"
    api.get(f"{V1}/workflows").respond(200, json=page([ex(Workflow, name="weekly")]))
    assert (await c.workflows.find("weekly")) is not None
    assert [w.name async for w in c.workflows.list()] == ["weekly"]


async def test_async_stream_events(api: respx.MockRouter, ascoped: Any) -> None:
    c: AsyncClient = ascoped
    body = _sse(
        ("snapshot", json.dumps(_wf_run("running")), None),
        ("workflow.branch.decided", _event(1, "workflow.branch.decided", "wanders"), 1),
        ("end", json.dumps({"run_id": RUN, "status": "failed"}), None),
    )
    api.get(f"{V1}/workflow-runs/{RUN}/events").respond(
        200, content=body, headers={"content-type": "text/event-stream"}
    )
    items = [i async for i in c.workflows.stream_events(RUN)]
    assert isinstance(items[1], WorkflowEventFrame) and items[1].node_id == "wanders"
    assert isinstance(items[-1], WorkflowEnd) and items[-1].status is not None
    assert items[-1].status.value == "failed"


async def test_async_explore_validation_and_pack(
    api: respx.MockRouter, ascoped: Any, tmp_path: Path
) -> None:
    c: AsyncClient = ascoped
    api.post(f"{V1}/datasets/ds_1/versions/4/explore").respond(202, json=_exploration(False))
    blob = json.dumps({"format": "x"}).encode()
    done = _exploration(True, [link("ui/explore/overview.json", blob, "x/overview.json")])
    api.get(f"{V1}/explorations/{RUN}").respond(200, json=done)
    api.get(f"{STORAGE}/x/overview.json").respond(200, content=blob)
    got = await c.datasets.explore("ds_1", 4, sections=["overview"])
    assert await c.explorations.view(got, "overview") == {"format": "x"}
    api.post(f"{V1}/models/mdl_1/versions/7/validations").respond(
        202, json=ex(ModelValidation, status="checks_running")
    )
    api.get(f"{V1}/validations/{VAL}").respond(200, json=ex(ModelValidation))
    v = await c.validations.request("mdl_1", 7, wait=True)
    assert v.status.value == "awaiting_approval"
    api.post(f"{V1}/validations/{VAL}/evidence-pack").respond(200, json=ex(EvidencePack))
    out = await c.validations.download_evidence_pack(VAL, tmp_path / "p.json")
    assert out.exists()
    api.get(f"{V1}/governance/policies").respond(200, json=page([ex(GovernancePolicy)]))
    assert len([p async for p in c.governance.policies(scope="tenant")]) == 1
