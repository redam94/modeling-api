"""v0.5 commands against a respx-mocked API: ``mapi workflows``, ``governance``,
``validate``, ``validations``, ``approve``, ``explore``, ``optimize``, ``choose`` and
``about``. Responses are the contract's own examples."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import respx
import yaml
from mapi_spec.about import CHOOSER_QUESTIONS
from mapi_spec.api import (
    CatalogAbout,
    CheckResult,
    ChooserQuestionnaire,
    ChooserResult,
    EvidencePack,
    Exploration,
    GovernanceCheckDef,
    GovernancePolicy,
    Model,
    ModelValidation,
    NodeRun,
    Optimization,
    Workflow,
    WorkflowRevision,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowTemplate,
    WorkflowTrigger,
    WorkflowValidation,
)
from mapi_spec.errors import ErrorCode, Problem
from mapi_spec.storage import crc32c, crc32c_to_base64
from mapi_spec.workflow import WorkflowEvent, WorkflowSpec
from mapi_spec.workflow_templates import WORKFLOW_TEMPLATES
from typer.testing import Result

Invoke = Callable[..., Result]
Ex = Callable[..., dict[str, Any]]

URL = "https://api.test"
V1 = f"{URL}/v1"
STORAGE = "https://storage.test"
HEADER = "X-Mapi-Project"
CONN = ("--url", URL, "--token", "tok-secret-value", "--project", "acme/growth")
RUN = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
WF = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0"
VAL = "val_01jaf3k1m2n3p4q5r6s7t8v9w0"
MDL = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0"


def page(items: list[Any]) -> dict[str, Any]:
    return {"items": items, "next_cursor": None}


def body_of(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def problem(code: ErrorCode, detail: str, status: int) -> httpx.Response:
    return httpx.Response(status, json=Problem.from_code(code, detail).model_dump(mode="json"))


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


def _sse(*frames: tuple[str, str, int | None]) -> bytes:
    out = []
    for event, data, ident in frames:
        head = f"id: {ident}\n" if ident is not None else ""
        out.append(f"{head}event: {event}\ndata: {data}\n\n")
    return "".join(out).encode()


# ============================================================================= workflows


def test_init_writes_a_template_that_validates_locally(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    out = tmp_path / "weekly.yaml"
    res = mapi(
        *CONN,
        "workflows",
        "init",
        "ts_readiness_arimax",
        "--name",
        "weekly",
        "--out",
        str(out),
        "--offline",
    )
    assert res.exit_code == 0, res.output
    text = out.read_text()
    assert text.startswith("# ") and "mapi workflows validate" in text
    spec = WorkflowSpec.model_validate(yaml.safe_load(text))
    assert spec.metadata.name == "weekly"
    assert len(spec.nodes) == len(WORKFLOW_TEMPLATES["ts_readiness_arimax"].spec.nodes)
    res = mapi(*CONN, "workflows", "init", "ts_readiness_arimax", "--out", str(out), "--offline")
    assert res.exit_code == 1 and "exists" in res.stderr
    # from the server: the selected project goes into the document
    tpl = example(WorkflowTemplate)
    api.get(f"{V1}/workflow-templates/readiness").respond(200, json=tpl)
    res = mapi(*CONN, "workflows", "init", "readiness", "--out", str(tmp_path / "r.yaml"))
    assert res.exit_code == 0, res.output
    doc = yaml.safe_load((tmp_path / "r.yaml").read_text())
    assert doc["metadata"]["project"] == "growth" and doc["metadata"]["name"] == "readiness"


def test_validate_reports_pointers_and_fails(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    f = tmp_path / "wf.yaml"
    f.write_text("apiVersion: mapi/v1\nkind: Workflow\n")
    bad = example(WorkflowValidation)
    route = api.post(f"{V1}/workflows:validate").respond(200, json=bad)
    res = mapi(*CONN, "workflows", "validate", str(f))
    assert res.exit_code == 1
    assert bad["issues"][0]["pointer"] in res.stdout and "invalid" in res.stderr
    assert body_of(route) == {"text": "apiVersion: mapi/v1\nkind: Workflow\n"}
    assert route.calls.last.request.headers[HEADER] == "acme/growth"
    good = {**bad, "valid": True, "issues": [], "order": ["adf", "kpss"]}
    route.respond(200, json=good)
    res = mapi(*CONN, "validate", "workflow", str(f))
    assert res.exit_code == 0 and "adf → kpss" in res.stdout


def test_apply_creates_then_revises(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    f = tmp_path / "weekly.yaml"
    spec = WORKFLOW_TEMPLATES["ts_readiness_arimax"].spec.model_copy()
    doc = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    doc["metadata"]["name"] = "weekly"
    f.write_text(yaml.safe_dump(doc))
    api.get(f"{V1}/workflows").respond(200, json=page([]))
    created = api.post(f"{V1}/workflows").respond(201, json=example(Workflow, name="weekly"))
    rev = example(WorkflowRevision)
    rev["validation"] = {"valid": True, "issues": [], "order": ["adf"], "resolved": {}}
    api.get(f"{V1}/workflows/{WF}/revisions/2").respond(200, json=rev)
    res = mapi(*CONN, "workflows", "apply", str(f), "-m", "first")
    assert res.exit_code == 0, res.output
    assert body_of(created)["message"] == "first" and "text" in body_of(created)
    assert "workflow://growth/" in res.stdout


def test_run_with_watch_follows_nodes_and_exits_1_on_failure(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    api.get(f"{V1}/workflows").respond(200, json=page([example(Workflow, name="weekly")]))
    start = api.post(f"{V1}/workflows/{WF}/runs").respond(
        202, json=example(WorkflowRun, status="queued")
    )
    snap = json.dumps(example(WorkflowRun, status="running"))

    def ev(seq: int, type_: str, node: str | None, **data: Any) -> str:
        return json.dumps(
            example(WorkflowEvent, seq=seq, type=type_, node_id=node, run_id=RUN, data=data)
        )

    stream = _sse(
        ("snapshot", snap, None),
        ("workflow.node.started", ev(1, "workflow.node.started", "adf"), 1),
        ("workflow.branch.decided", ev(2, "workflow.branch.decided", "wanders", arm="then"), 2),
        ("workflow.node.failed", ev(3, "workflow.node.failed", "adf", message="boom"), 3),
        ("end", json.dumps({"run_id": RUN, "status": "failed"}), None),
    )
    api.get(f"{V1}/workflow-runs/{RUN}/events").respond(
        200, content=stream, headers={"content-type": "text/event-stream"}
    )
    failed = example(WorkflowRun, status="failed")
    failed["error"] = {"code": "USER_CODE_ERROR", "message": "singular matrix"}
    api.get(f"{V1}/workflow-runs/{RUN}").respond(200, json=failed)
    res = mapi(
        *CONN, "workflows", "run", "weekly", "--input", "data=dataset://growth/sales@latest",
        "--param", "alpha=0.01", "--param", 'drivers=["price"]', "--watch",
    )  # fmt: skip
    assert res.exit_code == 1
    sent = body_of(start)
    assert sent["inputs"] == {"data": "dataset://growth/sales@latest"}
    assert sent["params"] == {"alpha": 0.01, "drivers": ["price"]}
    assert "wanders took the then arm" in res.stderr and "adf failed: boom" in res.stderr
    assert "singular matrix" in res.stderr and "failed" in res.stdout
    # --json: one JSON document per event
    res = mapi(*CONN, "--json", "workflows", "watch", RUN)
    lines = [json.loads(x) for x in res.stdout.splitlines()]
    assert [x["event"] for x in lines] == [
        "snapshot",
        "workflow.node.started",
        "workflow.branch.decided",
        "workflow.node.failed",
        "end",
    ]


def test_run_without_watch_and_inputs_are_checked(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    api.get(f"{V1}/workflows/{WF}").respond(200, json=example(Workflow))
    api.post(f"{V1}/workflows/{WF}/runs").respond(202, json=example(WorkflowRun, status="queued"))
    res = mapi(*CONN, "workflows", "run", WF, "-i", "data=dataset://growth/sales@v4")
    assert res.exit_code == 0 and f"mapi workflows watch {RUN}" in res.stderr
    res = mapi(*CONN, "workflows", "run", WF, "-i", "nonsense")
    assert res.exit_code == 1 and "name=value" in res.stderr
    api.get(f"{V1}/workflows").respond(200, json=page([]))
    res = mapi(*CONN, "workflows", "run", "nope")
    assert res.exit_code == 1 and "NOT_FOUND" in res.stderr


def test_nodes_node_status_and_output(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    api.get(f"{V1}/workflow-runs/{RUN}/nodes").respond(200, json=page([example(NodeRun)]))
    res = mapi(*CONN, "workflows", "nodes", RUN)
    assert "test.adf" in res.stdout and "unit_root" in res.stdout
    api.get(f"{V1}/workflow-runs/{RUN}/nodes/adf").respond(200, json=example(NodeRun))
    res = mapi(*CONN, "workflows", "node", RUN, "adf")
    assert '"p_value": 0.57' in res.stdout
    api.get(f"{V1}/workflow-runs/{RUN}").respond(200, json=example(WorkflowRun))
    res = mapi(*CONN, "workflows", "status", RUN)
    assert "Decisions" in res.stdout and "wanders" in res.stdout
    blob = b"PK\x03\x04data"
    api.get(f"{V1}/workflow-runs/{RUN}/nodes/diff/outputs/data").respond(
        200, json=link("data.zarr.zip", blob, "d.zip")
    )
    api.get(f"{STORAGE}/d.zip").respond(200, content=blob)
    dest = tmp_path / "diff.zarr.zip"
    res = mapi(*CONN, "workflows", "output", RUN, "diff", "data", "--out", str(dest))
    assert res.exit_code == 0 and dest.read_bytes() == blob


def test_templates_schedules_and_triggers(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/workflow-templates").respond(200, json=page([example(WorkflowTemplate)]))
    res = mapi(*CONN, "workflows", "templates")
    assert "Weekly totals" in res.stdout and "readiness" in res.stdout
    api.get(f"{V1}/workflow-templates/readiness").respond(200, json=example(WorkflowTemplate))
    res = mapi(*CONN, "workflows", "template", "readiness")
    assert "Use it when" in res.stdout and "test.adf" in res.stdout
    api.get(f"{V1}/workflows/{WF}").respond(200, json=example(Workflow))
    add = api.post(f"{V1}/workflows/{WF}/schedules").respond(201, json=example(WorkflowSchedule))
    res = mapi(
        *CONN,
        "workflows",
        "schedules",
        "add",
        WF,
        "0 6 * * 1",
        "--tz",
        "Europe/Berlin",
        "-i",
        "data=dataset://growth/s@latest",
    )
    assert res.exit_code == 0, res.output
    assert body_of(add)["timezone"] == "Europe/Berlin"
    assert body_of(add)["inputs"] == {"data": "dataset://growth/s@latest"}
    sid = example(WorkflowSchedule)["id"]
    patch = api.patch(f"{V1}/workflows/{WF}/schedules/{sid}").respond(
        200, json=example(WorkflowSchedule, active=False)
    )
    res = mapi(*CONN, "workflows", "schedules", "pause", WF, sid)
    assert body_of(patch) == {"active": False} and "paused" in res.stdout
    trig = api.post(f"{V1}/workflows/{WF}/triggers").respond(201, json=example(WorkflowTrigger))
    res = mapi(
        *CONN,
        "workflows",
        "triggers",
        "add",
        WF,
        "dataset://growth/weekly_sales",
        "--debounce",
        "600",
    )
    assert res.exit_code == 0 and body_of(trig)["debounce_seconds"] == 600
    rm = api.delete(f"{V1}/workflows/{WF}").respond(204)
    res = mapi(*CONN, "workflows", "rm", WF, input="n\n")
    assert res.exit_code == 1 and not rm.called
    res = mapi(*CONN, "workflows", "rm", WF, "--yes")
    assert res.exit_code == 0 and rm.called


# ============================================================ governance and validation


def test_checks_and_policies(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    chk = example(GovernanceCheckDef)
    api.get(f"{V1}/catalog/governance-checks").respond(200, json=page([chk]))
    res = mapi(*CONN, "governance", "checks")
    assert chk["id"] in res.stdout
    res = mapi(*CONN, "governance", "checks", "--about", chk["id"], "--expert")
    assert "Use it when" in res.stdout and "metric" in res.stdout
    pol = example(GovernancePolicy)
    api.get(f"{V1}/governance/policies").respond(200, json=page([pol]))
    res = mapi(*CONN, "governance", "policies", "ls")
    assert pol["name"] in res.stdout
    f = tmp_path / "policy.yaml"
    f.write_text(
        "name: Forecasts we publish\napplies_to: {families: [ts]}\n"
        "checks:\n  - check: baseline.beats_naive\n  - {check: holdout.accuracy, "
        "thresholds: {warn: 0.1, fail: 0.2}}\nrequire_approval: true\n"
    )
    create = api.post(f"{V1}/tenants/acme/governance/policies").respond(201, json=pol)
    res = mapi(*CONN, "governance", "policies", "create", str(f), "--tenant", "acme")
    assert res.exit_code == 0, res.output
    assert body_of(create)["checks"][1]["thresholds"] == {"warn": 0.1, "fail": 0.2}
    put = api.put(f"{V1}/governance/policies/{pol['id']}").respond(200, json=pol)
    res = mapi(
        *CONN, "governance", "policies", "update", pol["id"], str(f), "--expected-version", "3"
    )
    assert res.exit_code == 0 and body_of(put)["expected_version"] == 3
    dl = api.delete(f"{V1}/governance/policies/{pol['id']}").respond(204)
    res = mapi(*CONN, "governance", "policies", "retire", pol["id"], "-y")
    assert res.exit_code == 0 and dl.called


def test_validate_model_waits_and_shows_every_result(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    api.get(f"{V1}/models").respond(200, json=page([example(Model, name="geo-mmm")]))
    running = example(ModelValidation, status="checks_running")
    req = api.post(f"{V1}/models/{MDL}/versions/7/validations").respond(202, json=running)
    failed = example(
        ModelValidation, status="checks_failed", failing_checks=["null.permutation_target"]
    )
    failed["results"][0] |= {"status": "fail", "suggested_action": "add data"}
    api.get(f"{V1}/validations/{VAL}").mock(
        side_effect=[httpx.Response(200, json=running), httpx.Response(200, json=failed)]
    )
    res = mapi(*CONN, "validate", "model", "model://growth/geo-mmm@v7", "--note", "Q4")
    assert res.exit_code == 1
    assert body_of(req) == {"note": "Q4", "policy_ids": []}
    assert "null.permutation_target" in res.stdout and "add data" in res.stdout
    api.post(f"{V1}/models/{MDL}/versions/3/validations").respond(
        202, json=example(ModelValidation)
    )
    res = mapi(*CONN, "validate", "model", "geo-mmm", "--version", "3", "--no-wait")
    assert res.exit_code == 0 and f"mapi approve {VAL}" in res.stderr


def test_approve_confirms_and_rejections_need_a_comment(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    v = example(ModelValidation)
    api.get(f"{V1}/validations/{VAL}").respond(200, json=v)
    decide = api.post(f"{V1}/validations/{VAL}/approvals").respond(
        200, json=example(ModelValidation, status="validated")
    )
    res = mapi(*CONN, "approve", VAL, input="n\n")
    assert res.exit_code == 1 and not decide.called
    res = mapi(*CONN, "approve", VAL, "--comment", "convincing", "-y")
    assert res.exit_code == 0 and body_of(decide) == {
        "decision": "approve",
        "comment": "convincing",
    }
    assert "validated" in res.stdout
    res = mapi(*CONN, "approve", VAL, "--reject", "-y")
    assert res.exit_code == 1 and "--comment" in res.stderr
    decide.mock(return_value=problem(ErrorCode.SELF_APPROVAL_FORBIDDEN, "not yours", 403))
    res = mapi(*CONN, "approve", VAL, "-y")
    assert res.exit_code == 1 and "SELF_APPROVAL_FORBIDDEN" in res.stderr


def test_validations_queue_evidence_and_pack(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    queue = api.get(f"{V1}/validations").respond(200, json=page([example(ModelValidation)]))
    res = mapi(*CONN, "validations", "ls", "--queue")
    assert queue.calls.last.request.url.params["status"] == "awaiting_approval"
    assert "awaiting_approval" in res.stdout
    api.get(f"{V1}/validations/{VAL}/evidence").respond(200, json=page([example(CheckResult)]))
    res = mapi(*CONN, "validations", "evidence", VAL)
    assert "permutation_scores" in res.stdout or "Evidence" in res.stdout
    html = b"<!doctype html><title>evidence</title>"
    pack = {**example(EvidencePack), "download": link("e.html", html, "e.html")}
    api.post(f"{V1}/validations/{VAL}/evidence-pack").respond(200, json=pack)
    api.get(f"{STORAGE}/e.html").respond(200, content=html)
    dest = tmp_path / "pack.html"
    res = mapi(*CONN, "validations", "pack", VAL, "--out", str(dest))
    assert res.exit_code == 0 and dest.read_bytes() == html


# =========================================================== explorers and optimisation


def test_explore_dataset_writes_the_views(
    mapi: Invoke, api: respx.MockRouter, example: Ex, tmp_path: Path
) -> None:
    from mapi_spec.api import Dataset

    api.get(f"{V1}/datasets").respond(200, json=page([example(Dataset, name="sales")]))
    ds_id = example(Dataset)["id"]
    blob = b'{"format": "mapi.data-explorer.stationarity/v1"}'
    exp = example(Exploration)
    exp["views"] = [link("ui/explore/stationarity.json", blob, "st.json")]
    start = api.post(f"{V1}/datasets/{ds_id}/versions/4/explore").respond(200, json=exp)
    api.get(f"{STORAGE}/st.json").respond(200, content=blob)
    res = mapi(
        *CONN,
        "explore",
        "dataset",
        "dataset://growth/sales@v4",
        "-s",
        "stationarity",
        "--kpi",
        "revenue",
        "--out",
        str(tmp_path / "views"),
    )
    assert res.exit_code == 0, res.output
    assert body_of(start)["sections"] == ["stationarity"] and body_of(start)["kpi"] == "revenue"
    assert (tmp_path / "views" / "stationarity.json").read_bytes() == blob
    assert "stationarity" in res.stdout
    res = mapi(*CONN, "explore", "dataset", "sales@v4", "--show", "stationarity")
    assert json.loads(res.stdout) == json.loads(blob)


def test_compare_prints_metrics_and_tests(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    view = {
        "format": "mapi.model-explorer.comparison/v1",
        "models": ["model://g/a@v1", "model://g/b@v1"],
        "metrics": ["mae"],
        "table": {"model://g/a@v1": {"mae": 1.5}, "model://g/b@v1": {"mae": 2.25}},
        "ranking": {"mae": ["model://g/a@v1", "model://g/b@v1"]},
        "tests": [],
    }
    blob = json.dumps(view).encode()
    exp = example(Exploration, target="model")
    exp["views"] = [link("ui/explore/comparison.json", blob, "c.json")]
    route = api.post(f"{V1}/models:compare").respond(200, json=exp)
    api.get(f"{STORAGE}/c.json").respond(200, content=blob)
    res = mapi(*CONN, "explore", "compare", "model://g/a@v1", "model://g/b@v1")
    assert res.exit_code == 0, res.output
    assert body_of(route)["models"] == ["model://g/a@v1", "model://g/b@v1"]
    assert "2.25" in res.stdout and "best mae" in res.stdout


def test_optimize_prints_the_plan(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/models").respond(200, json=page([example(Model, name="geo-mmm")]))
    route = api.post(f"{V1}/models/{MDL}/versions/7:optimize").respond(
        200, json=example(Optimization)
    )
    res = mapi(
        *CONN, "optimize", "geo-mmm@v7", "--budget", "1000000", "--objective", "p10",
        "--channel", 'tv={"max_multiplier": 1.5}', "--frontier", "5",
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    sent = body_of(route)
    assert sent["total_budget"] == 1_000_000 and sent["objective"] == "p10"
    assert sent["channels"]["tv"]["max_multiplier"] == 1.5
    assert sent["frontier"] == {"n_points": 5}
    assert route.calls.last.request.headers["Prefer"] == "wait=60"
    assert "Plan" in res.stdout and "recommended" in res.stdout
    res = mapi(*CONN, "optimize", "geo-mmm@v7", "--channel", "tv=2")
    assert res.exit_code == 1 and "JSON object" in res.stderr
    api.post(f"{V1}/models/{MDL}/versions/7:optimize").mock(
        return_value=problem(ErrorCode.OPTIMIZE_INFEASIBLE, "bounds exceed the budget", 422)
    )
    res = mapi(*CONN, "optimize", "geo-mmm@v7", "--budget", "1")
    assert res.exit_code == 1 and "OPTIMIZE_INFEASIBLE" in res.stderr


# ================================================================== choose and about


def test_choose_asks_the_questions_and_shows_about_cards(
    mapi: Invoke, api: respx.MockRouter, example: Ex
) -> None:
    questions = ChooserQuestionnaire(questions=list(CHOOSER_QUESTIONS)).model_dump(mode="json")
    api.get(f"{V1}/catalog/chooser").respond(200, json=questions)
    result = example(ChooserResult)
    result["items"][0]["about"] = example(CatalogAbout)["about"]
    rec = api.get(f"{V1}/catalog/recommendations").respond(200, json=result)
    # goal: 3 (forecast) and 9; data_shape: a typo first, then 1; Enter skips the rest
    answers = "3,9\nx\n1\n" + "\n" * 6
    res = mapi(*CONN, "choose", input=answers)
    assert res.exit_code == 0, res.output
    params = rec.calls.last.request.url.params
    assert params.get_list("goal") == ["forecast", "check_data"]
    assert params.get_list("data_shape") == ["one_series"]
    assert "please type" in res.stderr
    assert "Weekly forecast" in res.stdout and "Use it when" in res.stdout
    # scripts answer up front: no questions
    res = mapi(
        *CONN,
        "--json",
        "choose",
        "-a",
        "goal=forecast,plan_budget",
        "-a",
        "speed=seconds",
        "--kind",
        "model",
    )
    assert json.loads(res.stdout)["items"][0]["id"] == "weekly_forecast"
    params = rec.calls.last.request.url.params
    assert params.get_list("goal") == ["forecast", "plan_budget"] and params["kind"] == "model"
    res = mapi(*CONN, "choose", "-a", "goal=teleport")
    assert res.exit_code == 1 and "teleport" in res.stderr


def test_about_shows_any_card(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    card = example(CatalogAbout, kind="model", id="ts.sarimax")
    api.get(f"{V1}/catalog/abouts").respond(200, json=page([card]))
    res = mapi(*CONN, "about", "ts.sarimax")
    assert res.exit_code == 0 and "Weekly totals" in res.stdout and "Example:" in res.stdout
    res = mapi(*CONN, "about", "ts.nope")
    assert res.exit_code == 1 and "no catalog item" in res.stderr


def test_v05_errors_come_with_a_hint(mapi: Invoke, api: respx.MockRouter, example: Ex) -> None:
    api.get(f"{V1}/workflows/{WF}").respond(200, json=example(Workflow))
    api.post(f"{V1}/workflows/{WF}/runs").mock(
        return_value=problem(ErrorCode.WORKFLOW_NOT_RUNNABLE, "revision 2 has errors", 409)
    )
    res = mapi(*CONN, "workflows", "run", WF)
    assert res.exit_code == 1 and "WORKFLOW_NOT_RUNNABLE" in res.stderr
    assert "mapi workflows apply" in res.stderr
    api.get(f"{V1}/validations/{VAL}").respond(200, json=example(ModelValidation))
    api.post(f"{V1}/validations/{VAL}/approvals").mock(
        return_value=problem(ErrorCode.SELF_APPROVAL_FORBIDDEN, "not your own", 403)
    )
    res = mapi(*CONN, "approve", VAL, "-y")
    assert "reviewer capability" in res.stderr


def test_the_live_board_redraws_on_a_terminal(example: Ex) -> None:
    """On a terminal the node board is redrawn in place (rich Live on stderr); the summary
    is printed afterwards."""
    import io

    from mapi_cli.output import Output
    from mapi_cli.workflow_view import print_run, watch_run
    from mapi_client import WorkflowEnd, WorkflowEventFrame, WorkflowSnapshot
    from mapi_spec.api import WorkflowRun as WR
    from mapi_spec.workflow import WorkflowEvent as WE
    from mapi_spec.workflow import WorkflowRunStatus
    from rich.console import Console

    run = WR.model_validate(example(WorkflowRun, status="succeeded"))
    started = WE.model_validate(
        example(WorkflowEvent, seq=1, type="workflow.node.started", node_id="adf", run_id=RUN)
    )

    class Fake:
        class workflows:
            @staticmethod
            def stream_events(run_id: str, last_event_id: int | None = None) -> Any:
                yield WorkflowSnapshot(run)
                yield WorkflowEventFrame(started)
                yield WorkflowEnd(RUN, WorkflowRunStatus.SUCCEEDED)

            @staticmethod
            def get_run(run_id: str) -> WR:
                return run

    err, out = io.StringIO(), io.StringIO()
    output = Output(
        out=Console(file=out, width=120, force_terminal=True),
        err=Console(file=err, width=120, force_terminal=True),
    )
    final = watch_run(Fake(), output, RUN)  # type: ignore[arg-type]
    print_run(output, final)
    assert final.status.value == "succeeded"
    assert "adf" in out.getvalue() and "Decisions" in out.getvalue()
