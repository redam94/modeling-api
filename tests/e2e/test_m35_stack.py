"""The M3.5 exit journeys through the *running* stack (``make dev``, then ``make e2e``).

From outside the processes, against a ``dev``-profile stack, in a fresh project of the
``default`` tenant (see :mod:`m35_stack` for the data and its known truth):

1. **The canonical time-series workflow.** A weekly KPI with a unit root (a random walk plus
   a price driver) through the ``ts_readiness_arimax`` gallery template: ADF, KPSS,
   Phillips-Perron and Zivot-Andrews say it wanders, the branch chooses differencing (and
   no seasonal difference), the information-criterion search picks the ARMA orders that
   reach the fitted ARIMAX, the expanding-window backtest scores ARIMAX and the naive
   benchmarks on the same folds per horizon, ARIMAX is selected because it beats naive, and
   the governance node measures it - all followed live over server-sent events.
2. **Governance.** A project policy requiring the shuffled-target and noise-driver null tests
   with a reviewer's approval: the model fitted on pure noise fails validation and cannot be
   promoted (``NOT_VALIDATED``); the model fitted on real signal passes, waits for approval,
   the submitter may not approve (``SELF_APPROVAL_FORBIDDEN``), a second member with the
   ``reviewer`` capability approves, the version is ``validated`` and the promotion goes
   through; the evidence pack exports as HTML and JSON.
3. **mmm-framework.** ``mmm.bayesian`` (tiny NUTS) on a small geo panel; the Model
   Explorer's contributions and response curves; a budget optimisation returns an
   allocation that keeps the budget and an efficient frontier.
4. **Explorers and the chooser.** The Data Explorer's stationarity view of the KPI; a
   statsmodels VAR's impulse responses and variance decomposition; *Help me choose* ranks
   ``ts.sarimax`` first for "forecast weekly sales with drivers", with its About card.
5. **The CLI.** ``mapi workflows validate / apply / run --watch`` on the cookbook's
   canonical document (``docs/snippets/cookbook_readiness_arimax.yaml``) exactly as
   ``docs/snippets/cli_v05.sh`` runs it, with only the project renamed.

The browser half is ``apps/web/e2e/m35-live.e2e.spec.ts``. Timings are written to
``$MAPI_DATA_DIR/e2e/m35-timings.json``. ``MAPI_E2E_KEEP=1`` keeps the project afterwards.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from mapi_client import Client, WorkflowEnd, WorkflowEventFrame, WorkflowSnapshot
from mapi_spec.errors import ErrorCode, MapiError

sys.path.insert(0, str(Path(__file__).parent))
import m35_stack as m35

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.slow]

RUN: Final = uuid.uuid4().hex[:6]
DOCS: Final = m35.ROOT / "docs" / "snippets"
TEMPLATE_PARAMS: Final = {"drivers": ["price"]}


@pytest.fixture(scope="module")
def op() -> Iterator[Client]:
    url = m35.api_url()
    try:
        ready = httpx.get(f"{url}/readyz", timeout=10)
    except httpx.TransportError as exc:
        pytest.fail(f"the API is not reachable at {url} ({exc}); start it with `make dev`")
    assert ready.status_code == 200, ready.text
    if httpx.get(f"{url}/dev/outbox", timeout=10).status_code != 200:
        pytest.skip("the M3.5 journeys need a dev-profile stack (make dev)")
    with m35.sign_in(m35.OPERATOR) as client:
        yield client


@pytest.fixture(scope="module")
def world(op: Client) -> Iterator[m35.World]:
    w = m35.create_world(op, "m35e2e", RUN)
    try:
        yield w
    finally:
        out = m35.data_dir() / "e2e" / "m35-timings.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        report = {"project": w.project, "timings_s": w.timings}
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nM3.5 journey timings ({out}):\n{json.dumps(w.timings, indent=2)}")
        if os.environ.get("MAPI_E2E_KEEP", "") != "1":
            m35.cleanup(w.project)


def _timed(world: m35.World, name: str, t0: float) -> None:
    world.timings[name] = round(time.monotonic() - t0, 2)


# ===================================================== 1. the canonical time-series workflow


def test_the_canonical_workflow_on_a_series_with_a_unit_root(op: Client, world: m35.World) -> None:
    m35.upload(op, world, "weekly_sales", m35.unit_root_series())
    wf = op.workflows.create(template="ts_readiness_arimax")
    assert wf.latest_revision == 1 and wf.ref.endswith("/ts-readiness-arimax")
    t0 = time.monotonic()
    started = op.workflows.run(
        wf.id, inputs={"data": world.ref("weekly_sales")}, params=TEMPLATE_PARAMS
    )
    # follow it live: a snapshot first, every node's events, the branch decisions, the end
    seen: list[str] = []
    snapshot_nodes = 0
    ended = None
    for item in op.workflows.stream_events(started.run_id):
        match item:
            case WorkflowSnapshot(run=now):
                snapshot_nodes = len(now.nodes)
            case WorkflowEventFrame(event=e):
                seen.append(f"{e.type.value}:{e.node_id or ''}")
            case WorkflowEnd(status=status):
                ended = status
    run = op.workflows.wait(started.run_id)
    _timed(world, "workflow.canonical", t0)
    assert run.status.value == "succeeded" and ended is not None and ended.value == "succeeded"
    assert snapshot_nodes == len(run.nodes)
    assert "workflow.branch.decided:wanders" in seen and "workflow.node.skipped:sdiff" in seen
    assert {f"workflow.node.succeeded:{n}" for n in ("adf", "kpss", "order", "select")} <= set(seen)
    # @latest was pinned to the exact version when the run started
    assert run.inputs["data"] == world.ref("weekly_sales", "v1")
    nodes = {n.node_id: n for n in run.nodes}
    res = {k: dict(n.result or {}) for k, n in nodes.items()}

    # unit-root tests: every test says the series wanders (it is a random walk)
    assert res["adf"]["reject"] is False and res["adf"]["decision"] == "unit_root"
    assert res["adf"]["p_value"] > 0.05 and "wanders" in res["adf"]["verdict_plain"]
    assert res["kpss"]["reject"] is True and res["kpss"]["decision"] == "unit_root"
    assert res["pp"]["reject"] is False and res["za"]["reject"] is False
    assert res["za"]["break_at"]  # Zivot-Andrews names its most likely break
    assert res["seas"]["seasonal"] is False

    # the branch chose differencing, with the values its condition read; no seasonal diff
    decisions = {d.node_id: d for d in run.decisions}
    assert decisions["wanders"].arm == "then" and decisions["seasonal"].arm == "else"
    assert decisions["wanders"].inputs == {
        "nodes.adf.reject": False,
        "nodes.kpss.reject": True,
        "nodes.pp.reject": False,
        "nodes.za.reject": False,
    }
    assert nodes["diff"].status.value == "succeeded" and res["diff"]["order"] == 1
    assert nodes["sdiff"].status.value == "skipped" and nodes["sdiff"].skipped_reason

    # the order search picked p and q; the fitted ARIMAX uses them with d = 1 and the driver
    p, q = res["order"]["best_p"], res["order"]["best_q"]
    assert 0 <= p <= 3 and 0 <= q <= 3 and res["order"]["n_failed"] == 0
    assert res["order"]["best_order"] == [p, 1, q]
    settings = nodes["fit"].params["settings"]
    assert settings["order"] == [p, 1, q] and settings["exog"] == ["price"]
    assert nodes["backtest"].params["settings"]["order"] == [p, 1, q]

    # an expanding-window backtest: ARIMAX and the benchmarks on the same folds, per horizon
    folds = res["folds"]["n_folds"]
    assert folds >= 3 and len(res["folds"]["folds"]) == folds
    assert res["backtest"]["n_folds"] == folds and res["naive"]["n_folds"] == folds
    assert len(res["backtest"]["by_horizon"]) == 3  # horizons 1, 4 and 8
    assert {"naive", "seasonal_naive"} <= set(res["naive"]["by_method"])

    # ARIMAX vs naive, then the selection (kept only because it beats the benchmarks)
    assert res["compare"]["best"] == "backtest" and len(res["compare"]["ranking"]) == 3
    assert res["select"]["selected"] == "backtest" and res["select"]["beats_baselines"]
    assert "beats every benchmark" in res["select"]["reason_plain"]
    model_ref = run.outputs["model"].ref
    assert model_ref == res["select"]["model_version"] == res["fit"]["model_version"]
    assert model_ref is not None and model_ref.startswith(f"model://{world.project}/")

    # governance: the policy in force measured the selected model in the sandbox
    gov = res["governance"]
    assert gov["passed"] is True and gov["failed_checks"] == [] and gov["n_checks"] >= 5
    evidence = nodes["governance"].outputs["evidence"].inline
    assert evidence is not None and {r["check"] for r in evidence["results"]} >= {
        "baseline.beats_naive",
        "residuals.whiteness",
    }
    assert (
        res["report"]["n_sections"] > 10 and "Difference? -> then" in res["report"]["summary_plain"]
    )
    # every sandboxed node ran as a child run; orchestrator nodes ran none
    assert nodes["adf"].child_runs and not nodes["select"].child_runs
    assert run.cost is not None and run.cost.attempts_billed >= 10
    world.models["canonical"] = model_ref
    world.runs["canonical"] = run.run_id


# ======================================================================== 5. the CLI
# (before the governance journey: its project policy would judge this run's model too)


def _mapi() -> str:
    found = shutil.which("mapi") or str(Path(sys.executable).with_name("mapi"))
    assert Path(found).exists(), "the mapi CLI is not installed (uv sync --all-packages)"
    return found


def test_cli_runs_the_cookbook_workflow_with_watch(
    op: Client, world: m35.World, tmp_path: Path
) -> None:
    p = world.project
    doc = (DOCS / "cookbook_readiness_arimax.yaml").read_text("utf-8")
    (tmp_path / "weekly-readiness.yaml").write_text(
        doc.replace("project: growth", f"project: {p}"), "utf-8"
    )
    token = op.config.token
    assert token is not None
    env = {
        **os.environ,
        "MAPI_URL": m35.api_url(),
        "MAPI_TOKEN": token,
        "MAPI_PROJECT": f"{m35.TENANT}/{p}",
        "MAPI_CONFIG": str(tmp_path / "config.toml"),
        "NO_COLOR": "1",
        "COLUMNS": "200",
    }

    def mapi(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [_mapi(), *args], cwd=tmp_path, env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )  # fmt: skip

    # the commands of docs/snippets/cli_v05.sh (section "workflow"), project renamed
    checked = mapi("workflows", "validate", "weekly-readiness.yaml")
    assert checked.returncode == 0, checked.stderr
    assert "valid (23 nodes)" in checked.stdout
    applied = mapi("workflows", "apply", "weekly-readiness.yaml", "-m", "first version")
    assert applied.returncode == 0, applied.stderr
    assert f"workflow://{p}/weekly-readiness at revision 1" in applied.stdout
    t0 = time.monotonic()
    watched = mapi(
        "workflows", "run", "weekly-readiness",
        "--input", f"data=dataset://{p}/weekly_sales@latest",
        "--param", 'drivers=["price"]', "--param", "window=104",
        "--watch", timeout=m35.RUN_TIMEOUT_S,
    )  # fmt: skip
    _timed(world, "cli.run_watch", t0)
    out = watched.stderr + watched.stdout  # the live board on stderr, the summary on stdout
    assert watched.returncode == 0, out[-4000:]
    assert "following workflow run run_" in out and "(23 nodes)" in out
    # the unit-root tests are the canonical run's, unchanged: reused from it, not recomputed
    assert "adf cached" in out and "kpss cached" in out
    for line in (
        "wanders took the then arm",
        "seasonal took the else arm",
        "sdiff skipped: branch 'seasonal' took the else path",
        "dm succeeded",
        "governance succeeded",
        "trusted took the then arm",
        "run succeeded",
    ):
        assert line in out, line
    assert f"model://{p}/wf-weekly-readiness-fit@v1" in out

    listed = mapi("--json", "workflows", "runs", "weekly-readiness", "-n", "1")
    assert listed.returncode == 0, listed.stderr
    runs: list[dict[str, Any]] = json.loads(listed.stdout)
    assert runs[0]["status"] == "succeeded"
    node = mapi("workflows", "node", runs[0]["run_id"], "order")
    assert node.returncode == 0 and "best_p" in node.stdout, node.stdout + node.stderr
    # the rolling-origin backtest of the cookbook: its window and folds were used
    nodes = {n.node_id: n for n in op.workflows.get_run(runs[0]["run_id"]).nodes}
    assert nodes["adf"].status.value == "cached" and not nodes["adf"].child_runs
    assert nodes["adf"].cached_from == world.runs["canonical"]
    assert nodes["folds"].use.startswith("split.rolling_origin")
    assert nodes["folds"].params["window"] == 104
    dm = dict(nodes["dm"].result or {})
    assert dm["verdict_plain"] and dm["p_value"] is not None


# ===================================================================== 2. governance


def test_governance_null_tests_block_noise_and_a_reviewer_approves_signal(
    op: Client, world: m35.World, tmp_path: Path
) -> None:
    signal, noise = m35.signal_and_noise()
    m35.upload(op, world, "signal", signal)
    m35.upload(op, world, "noise", noise)
    p = world.project
    m35.fit_all(
        op,
        world,
        {
            "arimax-signal": m35.arimax_spec(p, "arimax-signal", "signal"),
            "arimax-noise": m35.arimax_spec(p, "arimax-noise", "noise"),
        },
    )
    policy = op.governance.create_policy(**m35.NULL_POLICY)
    assert policy.scope == "project" and policy.require_approval

    # pure noise: the shuffled-target test fails, the version cannot be promoted
    t0 = time.monotonic()
    noisy = op.models.validate(world.models["arimax-noise"], 1, note="noise", wait=True)
    _timed(world, "validate.noise", t0)
    assert f"{policy.id}@1" in noisy.policies
    results = {r.check: r for r in noisy.results}
    assert {"null.permutation_target", "null.noise_regressors"} <= set(results)
    assert noisy.status.value == "checks_failed"
    assert "null.permutation_target" in noisy.failing_checks
    perm = results["null.permutation_target"]
    assert perm.status.value == "fail" and perm.value is not None and perm.value > 0.1
    assert perm.message_plain and perm.suggested_action
    with pytest.raises(MapiError) as refused:
        op.endpoints.promote("noise-forecast", f"model://{p}/arimax-noise@v1")
    assert refused.value.code is ErrorCode.NOT_VALIDATED

    # real signal: both null tests pass; it waits for a second person
    t0 = time.monotonic()
    good = op.models.validate(world.models["arimax-signal"], 1, note="signal", wait=True)
    _timed(world, "validate.signal", t0)
    results = {r.check: r for r in good.results}
    assert results["null.permutation_target"].status.value == "pass"
    assert results["null.permutation_target"].value is not None
    assert results["null.permutation_target"].value <= 0.05  # p = 1/(K+1) at best
    assert results["null.noise_regressors"].status.value in ("pass", "warn")
    assert good.status.value == "awaiting_approval" and not good.failing_checks
    with pytest.raises(MapiError) as refused:
        op.endpoints.promote("signal-forecast", f"model://{p}/arimax-signal@v1")
    assert refused.value.code is ErrorCode.NOT_VALIDATED
    queue = [v.id for v in op.validations.list(status="awaiting_approval")]
    assert queue == [good.id]
    with pytest.raises(MapiError) as own:
        op.validations.approve(good.id, comment="my own work")
    assert own.value.code is ErrorCode.SELF_APPROVAL_FORBIDDEN

    with m35.sign_in(m35.REVIEWER, p) as reviewer:
        evidence = list(reviewer.validations.evidence(good.id))
        assert {r.check for r in evidence} >= {"null.permutation_target"}
        decided = reviewer.validations.approve(good.id, comment="Null tests convincing")
    assert decided.status.value == "validated" and len(decided.approvals) == 1
    assert decided.approvals[0].approver_id == world.reviewer_id
    endpoint = op.endpoints.promote("signal-forecast", f"model://{p}/arimax-signal@v1")
    assert endpoint.alias == "signal-forecast"

    html = op.validations.download_evidence_pack(good.id, tmp_path / "pack.html")
    page = html.read_text("utf-8")
    assert page.startswith("<!") and "<script" not in page and good.id in page
    doc = json.loads(
        op.validations.download_evidence_pack(good.id, tmp_path / "pack.json").read_text("utf-8")
    )
    assert doc["format"] == "mapi.evidence-pack/v1" and doc["digest"].startswith("sha256:")
    statuses = [h.to_status.value for h in op.validations.get(good.id).history]
    assert statuses[-2:] == ["awaiting_approval", "validated"]


# ===================================================================== 3. mmm-framework


def test_mmm_framework_fit_explore_and_optimise(op: Client, world: m35.World) -> None:
    m35.upload(op, world, "geo_panel", m35.geo_panel())
    m35.fit_all(op, world, {"geo-mmm": m35.mmm_spec(world.project)})
    model_id = world.models["geo-mmm"]
    mv = op.models.version(model_id, 1)
    assert mv.lineage is not None and mv.lineage.plugin.startswith("mmm.bayesian@")

    t0 = time.monotonic()
    explored = op.models.explore(model_id, 1, sections=["decomposition", "response_curves"])
    _timed(world, "explore.mmm", t0)
    assert explored.done and explored.index is not None
    assert {s.section: s.status for s in explored.index.sections} == {
        "decomposition": "ok",
        "response_curves": "ok",
    }
    decomposition = op.explorations.view(explored, "decomposition")
    names = {
        str(c.get("name", c)) if isinstance(c, dict) else str(c)
        for c in decomposition["components"]
    }
    assert {"search", "tv"} & {n.split(":")[-1] for n in names}, names
    curves = op.explorations.view(explored, "response_curves")
    assert sorted(curves["channels"]) == ["search", "tv"]
    for row in curves["mean"]:  # more spend never adds less (diminishing, not negative)
        assert all(b >= a - 1e-9 for a, b in itertools.pairwise(row))

    t0 = time.monotonic()
    plan = op.models.optimize(
        model_id,
        1,
        budget_change_pct=0,
        channels={"tv": {"min_multiplier": 0.5, "max_multiplier": 1.5}},
        frontier={"n_points": 5},
    )
    _timed(world, "optimize.mmm", t0)
    result = plan.result
    assert plan.done and result is not None
    assert {a.channel for a in result.allocation} == {"search", "tv"}
    total = sum(a.optimal_spend for a in result.allocation)
    assert total == pytest.approx(result.current_total, rel=1e-3)  # reallocated, same budget
    tv = next(a for a in result.allocation if a.channel == "tv")
    assert 0.5 * tv.current_spend - 1e-6 <= tv.optimal_spend <= 1.5 * tv.current_spend + 1e-6
    assert result.prob_positive_uplift is not None and 0 <= result.prob_positive_uplift <= 1
    assert len(result.frontier) >= 3
    budgets = [f.total_budget for f in result.frontier]
    assert budgets == sorted(budgets)


# =========================================================== 4. explorers and the chooser


def test_data_explorer_var_impulse_responses_and_help_me_choose(
    op: Client, world: m35.World
) -> None:
    ds_id = world.datasets["weekly_sales"]
    explored = op.datasets.explore(ds_id, 1, sections=["overview", "stationarity"], kpi="sales")
    assert explored.done
    stationarity = op.explorations.view(explored, "stationarity")
    assert "sales" in json.dumps(stationarity)

    m35.upload(op, world, "macro", m35.macro_series())
    m35.fit_all(op, world, {"macro-var": m35.var_spec(world.project)})
    var = op.models.explore(world.models["macro-var"], 1, sections=["irf", "fevd", "params"])
    assert var.index is not None and {s.status for s in var.index.sections} == {"ok"}
    irf = op.explorations.view(var, "irf")
    assert irf["variables"] == ["a", "b", "c"] and irf["horizons"] > 1
    assert len(irf["values"]) == 3 and all(len(r) == 3 for r in irf["values"])  # i <- j
    assert irf["lower"] is not None and irf["upper"] is not None  # bands
    fevd = op.explorations.view(var, "fevd")
    for shares in fevd["shares"]:  # each variable's forecast variance splits into shocks
        for row in shares:
            assert sum(row) == pytest.approx(1.0, abs=1e-6)

    answers = {"goal": "forecast", "data_shape": "one_series", "frequency": "weekly",
               "drivers": "yes"}  # fmt: skip
    ranked = op.catalog.recommend(limit=5, **answers)
    top = ranked.items[0]
    assert top.id == "ts.sarimax" and top.kind.value == "model"
    assert len(top.reasons_plain) == 4 and not top.cautions_plain
    assert {i.id for i in ranked.items} >= {"workflow.ts_readiness_arimax"}
    card = op.catalog.about("ts.sarimax", kind="model")
    assert card.summary_plain and card.use_when and card.avoid_when
