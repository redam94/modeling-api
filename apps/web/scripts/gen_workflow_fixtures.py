"""Write ``apps/web/src/mocks/workflows.gen.json`` for the mock API (M3.5 web).

The mock-mode workflow and governance screens (``src/mocks/workflows.ts`` and
``src/mocks/governance.ts``: the dev server in ``--mode mock``, the Vitest component tests and
the Playwright smoke suite) serve exactly what the server serves for the static catalogs:

- the workflow node catalog (``GET /v1/catalog/workflow-nodes``), the node-kind About cards,
  the gallery templates (``GET /v1/workflow-templates``), the governance check catalog
  (``GET /v1/catalog/governance-checks``) and the platform's default policies, all dumped from
  ``mapi_spec`` as the server does;
- a real run of the canonical ``ts_readiness_arimax`` template, executed in-process by
  ``mapi_workflows.runtime.local.LocalRunner`` on simulated weekly data with the installed
  statsmodels models and governance check runners: its node runs, branch decisions and event
  log become the mock's finished run and the script of the mock's live runs;
- static-validation vectors (``mapi_spec.workflow.validate_workflow`` on broken documents)
  and cron vectors (``mapi_spec.cron.next_fire``) that the web unit tests replay against the
  mock validator and the cron preview.

Usage::

    uv run python apps/web/scripts/gen_workflow_fixtures.py [--check]

``--check`` exits 1 when the committed file differs from what would be written.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mapi_spec.about import AboutKind
from mapi_spec.abouts import builtin_about_kind, builtin_abouts
from mapi_spec.cron import next_fire
from mapi_spec.governance import DEFAULT_POLICIES, GOVERNANCE_CHECKS, GovernancePolicy, PolicyCheck
from mapi_spec.workflow import WorkflowRun, WorkflowRunStatus, validate_workflow
from mapi_spec.workflow_nodes import NODE_IMPLEMENTATIONS, NODE_KIND_ABOUTS
from mapi_spec.workflow_templates import (
    TS_READINESS_ARIMAX,
    WORKFLOW_TEMPLATES,
    instantiate_template,
)

WEB = Path(__file__).resolve().parent.parent
OUT = WEB / "src" / "mocks" / "workflows.gen.json"

TENANT = "acme"
PROJECT = "growth"
WORKFLOW_ID = "wf_01jaf3e1f2g3h4j5k6m7n8p9q0"
RUN_ID = "run_01jaf3w1x2y3z4a5b6c7d8e9f0"
T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
DATASET_REF = "dataset://growth/weekly_sales@v3"

#: Installed models with an About card (the integration report's model list).
MODELS = [
    "mmm.bayesian", "mmm.frequentist", "ts.sarimax", "ts.arima", "ts.unobserved_components",
    "ts.ets", "ts.theta", "ts.ardl", "ts.ecm", "ts.var", "ts.vecm", "ts.varmax",
    "ts.dynamic_factor", "ts.markov_switching", "ts.hsgp_trend", "ts.gaussian_hmm",
    "tabular.regressor", "tabular.hierarchical_glm", "choice.multinomial_logit",
    "sklearn.tabular_regressor", "sklearn.tabular_quantiles", "sklearn.tabular_bootstrap",
    "sklearn.tabular_conformal", "sklearn.tabular_classifier",
]  # fmt: skip

PARAMS = {
    "drivers": ["price"],
    "n_folds": 4,
    "gap": 1,
    "horizons": [1, 4, 8],
    "max_p": 2,
    "max_q": 2,
}


def dump(model: Any) -> Any:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def abouts() -> list[dict[str, Any]]:
    """``GET /v1/catalog/abouts`` for the kinds the workflow and governance screens read."""
    out: list[dict[str, Any]] = []
    for item_id, about in sorted(builtin_abouts().items()):
        kind = builtin_about_kind(item_id)
        if kind in (AboutKind.NODE_KIND, AboutKind.WORKFLOW_TEMPLATE, AboutKind.CHECK) or (
            kind is AboutKind.MODEL and item_id in MODELS
        ):
            out.append({"kind": kind.value, "id": item_id, "about": dump(about)})
    out.sort(key=lambda a: (a["kind"], a["id"]))
    return out


# ------------------------------------------------------------------------- the canonical run


def canonical_run() -> dict[str, Any]:
    """Run ``ts_readiness_arimax`` in-process and shape the result as the server's views."""
    from mapi_workflows.runtime.governance import GovernanceServices
    from mapi_workflows.runtime.local import LocalRunner
    from mapi_workflows.testing import test_registry, weekly_series

    spec = instantiate_template(TS_READINESS_ARIMAX, name="weekly-readiness", project=PROJECT)
    data = weekly_series(156, random_walk=True, beta=3.0)
    gov = GovernanceServices(resolver=lambda model, policy_id: [VALIDATION_POLICY])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = LocalRunner(registry=test_registry(), governance=gov).run(
            spec, inputs={"data": data}, params=PARAMS
        )
    if res.status is not WorkflowRunStatus.SUCCEEDED:
        raise SystemExit(f"the canonical run did not succeed: {res.error}")

    # The views a server run carries: workflow_node / fit child runs and costs per node.
    nodes = []
    events = []
    t = T0
    for i, e in enumerate(res.events):
        t = T0 + timedelta(seconds=4 * i)
        events.append(
            {"seq": i + 1, "type": e.type.value, "run_id": RUN_ID, "node_id": e.node_id,
             "ts": t.isoformat().replace("+00:00", "Z"), "data": e.data}
        )  # fmt: skip
    started = {e["node_id"]: e["ts"] for e in events if e["type"] == "workflow.node.queued"}
    finished = {
        e["node_id"]: e["ts"]
        for e in events
        if e["type"] in ("workflow.node.succeeded", "workflow.node.skipped", "workflow.node.cached")
    }
    impls = {n.id: NODE_IMPLEMENTATIONS[n.impl_id] for n in spec.nodes}
    for k, nr in enumerate(res.nodes):
        view = dump(nr)
        impl = impls[nr.node_id]
        if nr.status.value in ("succeeded", "cached") and impl.runs_in != "orchestrator":
            child = f"run_01jaf3w1x2y3z4a5b6c7d8e9{chr(97 + k % 26)}{chr(97 + (k // 26) % 26)}"
            if impl.runs_in == "child_run":
                view["child_runs"] = [child]
            else:
                view["run_id"] = child
            view["cost"] = {
                "vcpu_seconds": 12.0 + 3 * k, "gib_seconds": 24.0 + 6 * k,
                "amount_micros": 900 + 150 * k, "currency": "USD", "estimated": False,
                "attempts_billed": 1, "price_version": "2026-09",
            }  # fmt: skip
        if nr.node_id in started:
            view["started_at"] = started[nr.node_id]
        if nr.node_id in finished:
            view["finished_at"] = finished[nr.node_id]
        nodes.append(view)
    run = WorkflowRun.model_validate(
        {
            "run_id": RUN_ID,
            "workflow_id": WORKFLOW_ID,
            "revision": 2,
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "status": "succeeded",
            "params": {**{k: v.default for k, v in spec.params.items()}, **PARAMS},
            "inputs": {"data": DATASET_REF},
            "nodes": nodes,
            "decisions": [dump(d) for d in res.decisions],
            "outputs": {k: dump(v) for k, v in res.outputs.items()},
            "cost": {
                "vcpu_seconds": 410.0, "gib_seconds": 820.0, "amount_micros": 21_300,
                "currency": "USD", "estimated": False, "attempts_billed": 12,
                "price_version": "2026-09",
            },
            "created_at": T0,
            "started_at": T0,
            "finished_at": t,
            "created_by": "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
        }
    )  # fmt: skip
    return {"run": dump(run), "events": events}


#: The project policy the mock validation is held to (the evidence of :func:`validation`).
VALIDATION_POLICY = GovernancePolicy(
    id="pol_01jaf3h1j2k3m4n5p6q7r8s9t1",
    tenant_id=TENANT,
    project_id=PROJECT,
    scope="project",
    version=2,
    name="Forecasts we publish",
    applies_to={"families": ["ts"]},
    checks=[
        PolicyCheck(check="data.quality"),
        PolicyCheck(check="convergence"),
        PolicyCheck(check="null.permutation_target", params={"n_permutations": 19}),
        PolicyCheck(check="baseline.beats_naive"),
        PolicyCheck(check="holdout.accuracy", thresholds={"warn": 0.1, "fail": 0.2}),
        PolicyCheck(check="residuals.whiteness", severity="warning"),
        PolicyCheck(check="plausibility.signs", params={"expected": {"price": "negative"}}),
    ],
    require_approval=True,
    created_at=T0 - timedelta(days=10),
)


def validation(*, random_walk: bool, seed: int = 7) -> list[dict[str, Any]]:
    """Real check results: ARIMAX with a price driver, fitted with an 8-week holdout and held
    to :data:`VALIDATION_POLICY` by the installed check runners. On the random walk the
    differenced ARIMAX passes; on a stable series the same (over-differenced) model fails the
    shuffled-result, benchmark and whiteness checks."""
    from mapi_workflows.runtime.governance import GovernanceServices
    from mapi_workflows.runtime.local import LocalRunner
    from mapi_workflows.testing import test_registry, weekly_series

    doc = {
        "apiVersion": "mapi/v1",
        "kind": "Workflow",
        "metadata": {"name": "governed-arimax", "project": PROJECT},
        "inputs": {"data": {"type": "dataset"}},
        "nodes": [
            {"id": "holdout", "use": "split.holdout", "inputs": {"data": "inputs.data"},
             "with": {"split": {"kind": "time_holdout", "dim": "time", "last": 8}}},
            {"id": "fit", "use": "model.fit",
             "inputs": {"data": "inputs.data", "split": "holdout.plan"},
             "with": {"model": "ts.sarimax@^1",
                      "settings": {"order": [1, 1, 0], "exog": ["price"]},
                      "evaluate": {"metrics": ["mape", "coverage_90"]}}},
            {"id": "governance", "use": "governance.policy",
             "inputs": {"model": "fit.model", "data": "inputs.data"}},
        ],
        "outputs": {"evidence": "governance.evidence"},
    }  # fmt: skip
    data = weekly_series(156, random_walk=random_walk, beta=6.0, phi=0.3, seed=seed)
    gov = GovernanceServices(resolver=lambda model, policy_id: [VALIDATION_POLICY])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = LocalRunner(registry=test_registry(), governance=gov).run(doc, inputs={"data": data})
    if res.status is not WorkflowRunStatus.SUCCEEDED:
        raise SystemExit(f"the governed run did not succeed: {res.error}")
    evidence = res.values["evidence"]
    assert isinstance(evidence, dict)
    results = evidence["results"]
    assert isinstance(results, list)
    return results


# ------------------------------------------------------------------------- vectors


def _broken(mutate: Any) -> dict[str, Any]:
    doc = copy.deepcopy(dict(TS_READINESS_ARIMAX.document))
    mutate(doc)
    return doc


def _node(doc: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in doc["nodes"] if n["id"] == node_id)


def validation_vectors() -> list[dict[str, Any]]:
    """Broken documents and the issues the server reports for them."""

    def unknown_use(d: dict[str, Any]) -> None:
        _node(d, "adf")["use"] = "test.nope"

    def wrong_port(d: dict[str, Any]) -> None:
        _node(d, "acf")["inputs"] = {"data": "adf.result"}

    def missing_port(d: dict[str, Any]) -> None:
        _node(d, "naive")["inputs"] = {"data": "inputs.data"}

    def bad_field(d: dict[str, Any]) -> None:
        _node(d, "wanders")["with"] = {"condition": "nodes.adf.p_valu < 0.05"}

    def syntax(d: dict[str, Any]) -> None:
        _node(d, "wanders")["with"] = {"condition": "nodes.adf.reject and (nodes.kpss.reject"}

    def cycle(d: dict[str, Any]) -> None:
        _node(d, "adf")["inputs"] = {"data": "diff.data"}

    def bad_param(d: dict[str, Any]) -> None:
        _node(d, "kpss")["with"] = {"alpha": 2}

    cases = {
        "unknown_use": unknown_use,
        "wrong_port_type": wrong_port,
        "missing_port": missing_port,
        "unknown_result_field": bad_field,
        "condition_syntax": syntax,
        "cycle": cycle,
        "param_out_of_range": bad_param,
    }
    out = []
    for name, mutate in cases.items():
        doc = _broken(mutate)
        v = validate_workflow(doc)
        out.append(
            {
                "name": name,
                "document": doc,
                "valid": v.valid,
                "issues": [
                    {"code": i.code.value, "pointer": i.pointer, "severity": i.severity.value}
                    for i in v.issues
                    if i.severity.value == "error"
                ],
            }
        )
    return out


def cron_vectors() -> list[dict[str, Any]]:
    after = datetime(2026, 3, 27, 12, 0, tzinfo=UTC)  # the weekend Europe moves its clocks
    cases = [
        ("0 6 * * 1", "Europe/Berlin"),
        ("@daily", "UTC"),
        ("30 2 * * *", "Europe/Berlin"),  # 02:30 does not exist on 2026-03-29
        ("*/15 * * * *", "UTC"),
        ("0 9 1,15 * *", "America/New_York"),
        ("0 0 * * sun", "Asia/Tokyo"),
        ("15 1 * * *", "America/New_York"),
    ]
    out = []
    for cron, tz in cases:
        times = []
        t = after
        for _ in range(5):
            t = next_fire(cron, t, tz)
            times.append(t.isoformat().replace("+00:00", "Z"))
        out.append({"cron": cron, "timezone": tz, "after": "2026-03-27T12:00:00Z", "next": times})
    return out


def render() -> str:
    doc = {
        "nodes": [dump(n) for n in NODE_IMPLEMENTATIONS.values()],
        "node_kinds": [
            {"kind": "node_kind", "id": f"kind.{k.value}", "about": dump(a)}
            for k, a in NODE_KIND_ABOUTS.items()
        ],
        "templates": [dump(t) for t in WORKFLOW_TEMPLATES.values()],
        "checks": [dump(c) for c in GOVERNANCE_CHECKS.values()],
        "default_policies": [dump(p) for p in DEFAULT_POLICIES],
        "abouts": abouts(),
        "policy": dump(VALIDATION_POLICY),
        "canonical": canonical_run(),
        "validation_passed": validation(random_walk=True),
        "validation_failed": validation(random_walk=False, seed=11),
        "validation_vectors": validation_vectors(),
        "cron_vectors": cron_vectors(),
    }
    return stable(json.dumps(doc, indent=1, ensure_ascii=False, sort_keys=False)) + "\n"


def stable(text: str) -> str:
    """Replace what a local run makes up per process (wall-clock times, in-memory bundle and
    input references, local run ids) with the fixed values a server run would show, so the
    file is reproducible and reads like a server's answer."""
    stamp = (T0 + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    text = re.sub(r'"(decided_at|computed_at)": "[^"]+"', rf'"\1": "{stamp}"', text)
    text = re.sub(r'"(started_at|finished_at)": "[^"]+\.\d+Z"', rf'"\1": "{stamp}"', text)
    text = re.sub(r"local://fit/[0-9a-f]+", "model://growth/wf-weekly-readiness-fit@v1", text)
    text = re.sub(r"local://inputs/data/[0-9a-f:]+", DATASET_REF, text)
    text = re.sub(
        r"local://[a-z_]+/(?:sha256:)?[0-9a-f]+",
        "dataset://growth/wf-weekly-readiness-diff@v1",
        text,
    )
    return re.sub(r'"run_id": "local_[^"]+"', '"run_id": null', text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale")
    args = parser.parse_args()
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print(
                f"{OUT} is stale; run: uv run python {Path(__file__).relative_to(WEB.parents[1])}"
            )
            return 1
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
