"""A fake driver for engine tests: node results without data."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mapi_spec.stattests import make_test_result
from mapi_spec.workflow import NodeError, NodeOutputRef
from mapi_spec.workflow_nodes import PortType
from mapi_workflows.engine import Action, WorkflowEngine
from mapi_workflows.payloads import MetricsCandidate, MetricsPayload, TablePayload, inline

Fn = Callable[[Action], tuple[dict[str, Any], dict[str, NodeOutputRef]]]


def stat_result(test: str, p: float, alpha: float = 0.05, **extra: Any) -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        r = make_test_result(test, statistic=-1.0, p_value=p, alpha=alpha, **extra)
        body = r.model_dump(mode="json")
        return body, {"result": inline(PortType.TEST_RESULT, body)}

    return fn


def dataset(ref: str) -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        return {"vars": ["y"]}, {"data": NodeOutputRef(type=PortType.DATASET, ref=ref)}

    return fn


def metrics(model: str | None, mae: float, kind: str = "model") -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        cand = MetricsCandidate(
            name=a.node_id,
            group=a.node_id,
            kind=kind,
            model=model,  # type: ignore[arg-type]
            metrics={"mae": mae, "rmse": mae * 1.2},
            n_folds=3,
        )
        p = MetricsPayload(candidates=[cand], horizons=[1, 4])
        return {"metrics": dict(cand.metrics)}, {"metrics": inline(PortType.METRICS, p)}

    return fn


def baselines(**maes: float) -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        cands = [
            MetricsCandidate(name=k, group=a.node_id, kind="baseline", metrics={"mae": v})
            for k, v in maes.items()
        ]
        best = min(maes, key=maes.__getitem__)
        return (
            {"by_method": {k: {"mae": v} for k, v in maes.items()}, "best_method": best},
            {"metrics": inline(PortType.METRICS, MetricsPayload(candidates=cands))},
        )

    return fn


def order(p: int, q: int) -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        t = TablePayload(title="grid", columns=["p", "q"], rows=[{"p": p, "q": q}])
        return (
            {"best_p": p, "best_q": q, "best_order": [p, 0, q], "best_score": 10.0},
            {"table": inline(PortType.TABLE, t)},
        )

    return fn


def plan() -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        body = {"format": "mapi.split-plan/v1", "plan": {"kind": "expanding_window"}, "folds": []}
        return {"n_folds": 3, "folds": []}, {"plan": inline(PortType.SPLIT_PLAN, body)}

    return fn


def model(ref: str) -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        return (
            {"model_version": ref, "metrics": {"mape": 0.1}, "scorecard": "pass"},
            {"model": NodeOutputRef(type=PortType.MODEL_VERSION, ref=ref),
             "metrics": inline(PortType.METRICS, MetricsPayload())},
        )  # fmt: skip

    return fn


def evidence(passed: bool = True) -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        body = {
            "format": "mapi.evidence/v1",
            "status": "checks_passed" if passed else "checks_failed",
            "passed": passed,
            "results": [],
        }
        return (
            {"passed": passed, "failed_checks": [], "status": body["status"]},
            {"evidence": inline(PortType.EVIDENCE, body)},
        )

    return fn


def fails(code: Any, message: str = "boom") -> Fn:
    def fn(a: Action) -> tuple[dict[str, Any], dict[str, NodeOutputRef]]:
        raise _Fail(NodeError(code=code, message=message))

    return fn


class _Fail(Exception):
    def __init__(self, error: NodeError) -> None:
        self.error = error


def drive(
    engine: WorkflowEngine, fns: Mapping[str, Fn], *, max_rounds: int = 100
) -> list[list[str]]:
    """Run ``engine`` to completion; ``fns`` maps node ids (or impl ids) to fakes.
    Returns the node ids started in each round."""
    rounds: list[list[str]] = []
    for _ in range(max_rounds):
        actions = engine.step()
        if engine.to_cancel:
            for nid in list(engine.to_cancel):
                engine.cancelled(nid)
        if not actions:
            if engine.finished:
                break
            if not engine.running():
                break
        rounds.append([a.node_id for a in actions])
        for a in actions:
            engine.started_node(a.node_id, run_id=f"run_{a.node_id}_{a.attempt}")
        for a in actions:
            fn = fns.get(a.node_id) or fns.get(a.impl.id)
            assert fn is not None, f"no fake for {a.node_id} ({a.impl.id})"
            try:
                result, outputs = fn(a)
            except _Fail as f:
                engine.failed(a.node_id, f.error)
                continue
            engine.succeeded(a.node_id, result, outputs)
    engine.finish_events()
    return rounds
