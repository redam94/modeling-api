"""Orchestrator nodes: comparisons, selections and reports computed from upstream JSON.

These nodes never see data - only the JSON results and inline payloads of earlier nodes -
so they run wherever the engine runs (the control plane included). Every function is pure.

* ``compare.models``: one table of every candidate's scores, ranked by a metric;
* ``select.best``: the best candidate, optionally only if it beats named benchmarks by a
  margin (its model version is the ``model`` output when one is bound);
* ``select.threshold``: the candidates that pass a bar;
* ``report.summary``: a plain-language report of the run (test verdicts, the decisions
  taken, the search results, the comparison, the chosen model);
* :func:`evidence_pack` builds the ``report.evidence_pack`` payload from governance
  evidence (drivers call it; the node runs as a child of the workflow run).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.stattests import StatTestResult
from mapi_spec.workflow import BranchDecision, NodeOutputRef, NodeRun, NodeRunStatus, WorkflowNode
from mapi_spec.workflow_nodes import NodeImplementation, NodeKind, PortType
from pydantic import JsonValue, ValidationError

from mapi_workflows.payloads import (
    EvidencePayload,
    MetricsCandidate,
    MetricsPayload,
    ReportPayload,
    ReportSection,
    TablePayload,
    clean_number,
    inline,
)

__all__ = [
    "ORCHESTRATOR_NODES",
    "BoundValue",
    "OrchestratorContext",
    "compare_models",
    "evidence_pack",
    "fmt_metric",
    "report_summary",
    "run_orchestrator_node",
    "select_best",
    "select_threshold",
]


@dataclass(frozen=True, slots=True)
class BoundValue:
    """A value bound to an input port, with what produced it."""

    ref: NodeOutputRef
    source: str | None = None
    source_params: Mapping[str, JsonValue] = field(default_factory=dict)
    source_result: Mapping[str, JsonValue] = field(default_factory=dict)
    source_use: str | None = None


@dataclass(frozen=True, slots=True)
class OrchestratorContext:
    """The run as an orchestrator node sees it (for reports)."""

    node_id: str
    node: WorkflowNode
    node_runs: Sequence[NodeRun] = ()
    decisions: Sequence[BranchDecision] = ()
    nodes: Mapping[str, WorkflowNode] = field(default_factory=dict)
    registry: Mapping[str, NodeImplementation] = field(default_factory=dict)
    title: str = ""


Result = tuple[dict[str, JsonValue], dict[str, NodeOutputRef]]


def _bad(message: str) -> MapiError:
    return MapiError(message, code=ErrorCode.WORKFLOW_INVALID)


# ================================================================================ metrics


def fmt_metric(name: str, value: float | None) -> str:
    """A metric for people: percentages for MAPE-like metrics, 3 significant digits."""
    if value is None or not math.isfinite(value):
        return "n/a"
    if name in ("mape", "smape", "coverage_90", "coverage"):
        return f"{value * 100:.1f}%" if abs(value) <= 10 else f"{value:.3g}"
    return f"{value:.3g}"


_PLAIN_METRIC: Final = {
    "mae": "average error",
    "rmse": "typical error (RMSE)",
    "mape": "average percentage error",
    "smape": "symmetric percentage error",
    "mase": "error relative to a naive forecast (MASE)",
    "coverage_90": "share of actual values in the 90% range",
    "crps": "probabilistic error (CRPS)",
}


def _candidates(values: Sequence[BoundValue]) -> list[MetricsCandidate]:
    out: list[MetricsCandidate] = []
    seen: set[str] = set()
    for v in values:
        if v.ref.type is not PortType.METRICS or not isinstance(v.ref.inline, dict):
            raise _bad(f"input from {v.source or 'the workflow'} is not a metrics payload")
        try:
            payload = MetricsPayload.model_validate(v.ref.inline)
        except ValidationError as exc:
            raise _bad(f"metrics from {v.source}: {str(exc)[:300]}") from None
        for c in payload.candidates:
            group = v.source or c.group
            name = group if c.name in ("", c.group) else c.name
            cand = c.model_copy(update={"group": group, "name": name})
            if cand.key in seen:
                continue
            seen.add(cand.key)
            out.append(cand)
    return out


def _sort_key(direction: str) -> Callable[[tuple[str, float | None]], tuple[int, float]]:
    def key(item: tuple[str, float | None]) -> tuple[int, float]:
        v = item[1]
        if v is None:
            return (1, 0.0)
        return (0, v if direction == "min" else -v)

    return key


def compare_models(params: Mapping[str, JsonValue], values: Sequence[BoundValue]) -> Result:
    """``compare.models``: a ranked comparison table of every candidate."""
    metric = str(params.get("metric") or "rmse")
    direction = str(params.get("direction") or "min")
    cands = _candidates(values)
    if not cands:
        raise _bad("compare.models got no candidates (the workflow skipped all the sources)")
    if not any(clean_number(c.metrics.get(metric)) is not None for c in cands):
        have = sorted({m for c in cands for m in c.metrics})
        raise _bad(f"no candidate has a score for {metric!r} (scores: {', '.join(have) or 'none'})")
    names = sorted({m for c in cands for m in c.metrics})
    scored = sorted(
        ((c.key, clean_number(c.metrics.get(metric))) for c in cands), key=_sort_key(direction)
    )
    rank = {k: i + 1 for i, (k, v) in enumerate(scored) if v is not None}
    by_key = {c.key: c for c in cands}
    rows: list[dict[str, JsonValue]] = []
    for key, _ in scored:
        c = by_key[key]
        row: dict[str, JsonValue] = {
            "candidate": key,
            "node": c.group,
            "kind": c.kind,
            "model": c.model,
            "rank": rank.get(key),
            "n_folds": c.n_folds,
        }
        for m in names:
            row[m] = clean_number(c.metrics.get(m))
        rows.append(row)
    ranking: list[JsonValue] = [k for k, v in scored if v is not None]
    best = ranking[0]
    table = TablePayload(
        title=f"Comparison by {metric}",
        kind="comparison",
        columns=["candidate", "node", "kind", "model", "rank", "n_folds", *names],
        rows=rows,
        meta={
            "metric": metric,
            "direction": direction,
            "candidates": {
                c.key: c.model_dump(mode="json", exclude={"errors": True, "elpd": True})
                for c in cands
            },
        },
    )
    result: dict[str, JsonValue] = {"ranking": ranking, "best": best}
    return result, {"table": inline(PortType.TABLE, table)}


# ================================================================================== select


def _table(values: Sequence[BoundValue]) -> TablePayload:
    if not values or not isinstance(values[0].ref.inline, dict):
        raise _bad("the node needs a table")
    try:
        return TablePayload.model_validate(values[0].ref.inline)
    except ValidationError as exc:
        raise _bad(f"not a table: {str(exc)[:300]}") from None


def _better(w: float, b: float, direction: str, margin: float) -> bool:
    if direction == "min":
        if b == 0:
            return w < b
        return (b - w) / abs(b) > margin if margin > 0 else w < b
    if b == 0:
        return w > b
    return (w - b) / abs(b) > margin if margin > 0 else w > b


def select_best(
    params: Mapping[str, JsonValue],
    table_values: Sequence[BoundValue],
    model_values: Sequence[BoundValue],
    *,
    name: Callable[[Mapping[str, JsonValue]], str] | None = None,
) -> Result:
    """``select.best``: the best candidate; the ``model`` output when it also beats the
    ``must_beat`` benchmarks (by ``margin``) and a model version is bound for it.

    ``name`` gives a comparison row's plain name for ``reason_plain`` (the step's label);
    ``selected`` stays the candidate key."""

    def called(row: Mapping[str, JsonValue]) -> str:
        return name(row) if name is not None else str(row.get("candidate"))

    table = _table(table_values)
    metric = str(params.get("metric") or table.meta.get("metric") or "rmse")
    direction = str(params.get("direction") or table.meta.get("direction") or "min")
    margin = float(clean_number(params.get("margin")) or 0.0)
    raw_beat = params.get("must_beat") or []
    must_beat = {str(x) for x in raw_beat} if isinstance(raw_beat, list) else set()

    def is_benchmark(row: Mapping[str, JsonValue]) -> bool:
        return str(row.get("candidate")) in must_beat or str(row.get("node")) in must_beat

    benches = [r for r in table.rows if is_benchmark(r)]
    contenders = [r for r in table.rows if not is_benchmark(r)]
    models_only = [r for r in contenders if r.get("kind") != "baseline"]
    if models_only:
        contenders = models_only
    scored: list[tuple[dict[str, JsonValue], float]] = []
    for r in contenders:
        val = clean_number(r.get(metric))
        if val is not None:
            scored.append((r, val))
    if not scored:
        raise _bad(f"no candidate (other than the benchmarks) has a {metric!r} score")
    scored.sort(key=lambda rv: rv[1] if direction == "min" else -rv[1])
    winner, score = scored[0]
    win_key = str(winner.get("candidate"))
    win_name = called(winner)
    names = {str(r.get("candidate")): called(r) for r in benches}
    bench_scores = [(str(r.get("candidate")), clean_number(r.get(metric))) for r in benches]
    bench_scores = [(k, v) for k, v in bench_scores if v is not None]
    beaten = [k for k, v in bench_scores if v is not None and _better(score, v, direction, margin)]
    beats = len(beaten) == len(bench_scores)
    if must_beat and not bench_scores:
        beats = False
    model_ref = _model_for(winner, model_values)
    plain_metric = _PLAIN_METRIC.get(metric, metric)
    if not must_beat:
        reason = (
            f"{win_name} has the best {plain_metric} ({fmt_metric(metric, score)}) of "
            f"{len(scored)} candidate(s)."
        )
    elif beats:
        worst = min(bench_scores, key=lambda kv: abs(kv[1] or 0.0)) if bench_scores else None
        reason = (
            f"{win_name} has the best {plain_metric} ({fmt_metric(metric, score)}) and is "
            f"better than all the benchmarks"
            + (
                f" (the strongest, {names.get(worst[0], worst[0])}, has "
                f"{fmt_metric(metric, worst[1])})"
                if worst
                else ""
            )
            + "."
        )
    else:
        losing = [names.get(k, k) for k, _ in bench_scores if k not in beaten]
        reason = (
            f"{win_name} is the best model ({fmt_metric(metric, score)}), but it is not better "
            f"than {', '.join(losing) or 'the benchmarks'}"
            + (f" by the necessary {margin * 100:.0f}%" if margin else "")
            + ". The node selects no model."
        )
    result: dict[str, JsonValue] = {
        "selected": win_key,
        "selected_model": winner.get("model") if isinstance(winner.get("model"), str) else None,
        "score": score,
        "beats_baselines": beats,
        "reason_plain": reason,
    }
    outputs: dict[str, NodeOutputRef] = {}
    if model_ref is not None and (beats or not must_beat):
        outputs["model"] = model_ref
        result["model_version"] = model_ref.ref
    return result, outputs


def _model_for(
    winner: Mapping[str, JsonValue], models: Sequence[BoundValue]
) -> NodeOutputRef | None:
    if not models:
        return None
    node, model = winner.get("node"), winner.get("model")
    for m in models:
        if m.source is not None and m.source == node:
            return m.ref
    for m in models:
        if model is not None and m.source_params.get("model") == model:
            return m.ref
    if len(models) == 1:
        return models[0].ref
    return None


def select_threshold(params: Mapping[str, JsonValue], values: Sequence[BoundValue]) -> Result:
    """``select.threshold``: the candidates whose ``metric`` is within ``[min, max]``."""
    table = _table(values)
    metric = str(params.get("metric"))
    lo, hi = clean_number(params.get("min")), clean_number(params.get("max"))
    if lo is None and hi is None:
        raise _bad("select.threshold needs a min or a max")
    passed_rows = []
    for row in table.rows:
        v = clean_number(row.get(metric))
        if v is None:
            continue
        if (lo is None or v >= lo) and (hi is None or v <= hi):
            passed_rows.append(row)
    passed: list[JsonValue] = [r.get("candidate") for r in passed_rows]
    out = table.model_copy(
        update={"rows": passed_rows, "title": f"Candidates that pass on {metric}"}
    )
    return {"passed": passed, "n_passed": len(passed)}, {"table": inline(PortType.TABLE, out)}


# ================================================================================== report


def _test_text(t: StatTestResult) -> str:
    bits = [t.verdict_plain]
    if t.p_value is not None:
        bits.append(f"(p = {t.p_value:.3g}")
        bits[-1] += f", tested at {t.alpha:g})"
    if t.reject_share is not None and t.per_series:
        bits.append(
            f"{round(t.reject_share * len(t.per_series))} of {len(t.per_series)} series "
            "reject the null."
        )
    return " ".join(bits)


def _metrics_text(payload: MetricsPayload) -> str:
    parts = []
    for c in payload.candidates:
        shown = ", ".join(
            f"{m.upper()} {fmt_metric(m, v)}" for m, v in sorted(c.metrics.items()) if v is not None
        )
        label = c.name if c.name == c.group else f"{c.group} ({c.name})"
        parts.append(
            f"{label}: {shown or 'no scores'}" + (f" over {c.n_folds} folds" if c.n_folds else "")
        )
    return "; ".join(parts) + "." if parts else "No scores."


def _table_text(t: TablePayload) -> str:
    if t.kind == "comparison":
        metric = str(t.meta.get("metric") or "")
        ranked = [r for r in t.rows if r.get("rank") is not None]
        if ranked:
            first = ranked[0]
            return (
                f"Rank order by {_PLAIN_METRIC.get(metric, metric)}: {first.get('candidate')} "
                f"is first of {len(ranked)} "
                f"({fmt_metric(metric, clean_number(first.get(metric)))})."
            )
    summary = t.meta.get("summary_plain")
    if isinstance(summary, str):
        return summary
    return f"{len(t.rows)} row(s)."


def report_summary(
    params: Mapping[str, JsonValue],
    inputs: Mapping[str, Sequence[BoundValue]],
    ctx: OrchestratorContext,
) -> Result:
    """``report.summary``: a plain-language report of the run so far."""
    title = str(params.get("title") or ctx.title or "Workflow report")
    only_raw = params.get("sections")
    only = {str(x) for x in only_raw} if isinstance(only_raw, list) else None
    sections: list[ReportSection] = []
    covered: set[str] = set()
    explained: dict[str, str] = {}

    def keep(nid: str | None) -> bool:
        return only is None or nid is None or nid in only

    for v in inputs.get("tests", ()):
        if not keep(v.source) or not isinstance(v.ref.inline, dict):
            continue
        try:
            t = StatTestResult.model_validate(v.ref.inline)
        except ValidationError:
            continue
        label = _label(ctx, v.source) or t.test
        sections.append(
            ReportSection(
                title=label, node_id=v.source, kind="test", status=t.decision, text=_test_text(t)
            )
        )
        covered.add(v.source or "")
    for d in ctx.decisions:
        if not keep(d.node_id):
            continue
        label = _label(ctx, d.node_id) or d.node_id
        gated = sorted(
            n.id
            for n in ctx.nodes.values()
            if n.when and n.when.startswith(d.node_id + ".") and n.when.endswith("." + d.arm)
        )
        values = ", ".join(f"{k} = {_short(v)}" for k, v in d.inputs.items())
        ran = [_label(ctx, g) or g for g in gated]
        technical = (
            f"The condition `{d.condition}` was {'true' if d.value else 'false'}"
            + (f" ({values})" if values else "")
            + (f", so {', '.join(ran)} ran." if ran else ".")
        )
        # The plain explanation first; the condition stays for experts.
        text = f"{d.explain_plain} {technical}" if d.explain_plain else technical
        if d.explain_plain:
            explained[d.node_id] = d.explain_plain
        sections.append(
            ReportSection(title=label, node_id=d.node_id, kind="decision", status=d.arm, text=text)
        )
        covered.add(d.node_id)
    for v in inputs.get("items", ()):
        if not keep(v.source) or not isinstance(v.ref.inline, dict):
            continue
        try:
            t2 = TablePayload.model_validate(v.ref.inline)
        except ValidationError:
            continue
        sections.append(
            ReportSection(
                title=_label(ctx, v.source) or t2.title or "Table",
                node_id=v.source,
                kind="table",
                text=_table_text(t2),
                table=t2,
            )
        )
        covered.add(v.source or "")
    for v in inputs.get("metrics", ()):
        if not keep(v.source) or not isinstance(v.ref.inline, dict):
            continue
        try:
            mp = MetricsPayload.model_validate(v.ref.inline)
        except ValidationError:
            continue
        sections.append(
            ReportSection(
                title=_label(ctx, v.source) or "Scores",
                node_id=v.source,
                kind="metrics",
                text=_metrics_text(mp),
            )
        )
        covered.add(v.source or "")
    # nodes that were skipped or failed are worth a line too
    for nr in ctx.node_runs:
        if nr.node_id in covered or nr.node_id == ctx.node_id or not keep(nr.node_id):
            continue
        if nr.status is NodeRunStatus.SKIPPED and nr.kind is not NodeKind.BRANCH:
            sections.append(
                ReportSection(
                    title=_label(ctx, nr.node_id) or nr.node_id,
                    node_id=nr.node_id,
                    kind="skipped",
                    status="skipped",
                    text=f"Skipped: {nr.skipped_reason or 'the workflow did not need it'}.",
                )
            )
        elif nr.status is NodeRunStatus.FAILED:
            msg = nr.error.message if nr.error else "it failed"
            sections.append(
                ReportSection(
                    title=_label(ctx, nr.node_id) or nr.node_id,
                    node_id=nr.node_id,
                    kind="failed",
                    status="failed",
                    text=f"Failed: {msg}",
                )
            )
        elif nr.kind is NodeKind.SELECT and nr.result is not None:
            reason = nr.result.get("reason_plain")
            if isinstance(reason, str):
                sections.append(
                    ReportSection(
                        title=_label(ctx, nr.node_id) or "Selection",
                        node_id=nr.node_id,
                        kind="selection",
                        text=reason,
                    )
                )
        elif nr.kind is NodeKind.SEARCH and nr.result is not None:
            sections.append(
                ReportSection(
                    title=_label(ctx, nr.node_id) or nr.node_id,
                    node_id=nr.node_id,
                    kind="search",
                    text=_search_text(nr.result),
                )
            )
    model_ref: str | None = None
    for v in inputs.get("model", ()):
        model_ref = v.ref.ref
    if model_ref:
        sections.append(
            ReportSection(
                title="Selected model",
                kind="model",
                text=f"The model of the workflow is {model_ref}.",
            )
        )
    summary = _summary(sections, model_ref, explained)
    report = ReportPayload(title=title, summary_plain=summary, sections=sections, model=model_ref)
    result: dict[str, JsonValue] = {
        "title": title,
        "summary_plain": summary,
        "n_sections": len(sections),
    }
    return result, {"report": inline(PortType.REPORT, report)}


def _search_text(result: Mapping[str, JsonValue]) -> str:
    if "best_order" in result:
        text = f"Best order {result['best_order']}"
        if "best_seasonal_order" in result:
            text += f" x {result['best_seasonal_order']}"
        if result.get("best_score") is not None:
            text += f" (score {fmt_metric('', clean_number(result.get('best_score')))})"
        return text + "."
    if "best_lag" in result:
        return f"Best lag {result['best_lag']}."
    if "rank" in result:
        return f"Selected rank {result['rank']}."
    if "best_settings" in result:
        return f"Best settings {result['best_settings']}."
    return "Search done."


def _short(v: object) -> str:
    if isinstance(v, float):
        return f"{v:.3g}"
    text = str(v)
    return text if len(text) <= 40 else text[:37] + "..."


def _candidate_name(ctx: OrchestratorContext, row: Mapping[str, JsonValue]) -> str:
    """A comparison row's plain name: its step's label ("ARIMAX"; "Naive forecast (seasonal
    naive)" for one of several candidates of a step), else its key."""
    key = str(row.get("candidate"))
    nid = row.get("node")
    node = ctx.nodes.get(nid) if isinstance(nid, str) else None
    if node is None or not node.label:
        return key
    if key == nid:
        return node.label
    rest = key[len(nid) + 1 :] if isinstance(nid, str) and key.startswith(nid + ".") else key
    return f"{node.label} ({rest.replace('_', ' ')})"


def _label(ctx: OrchestratorContext, nid: str | None) -> str | None:
    if nid is None:
        return None
    node = ctx.nodes.get(nid)
    if node is None:
        return nid
    if node.label:
        return node.label
    impl = ctx.registry.get(node.impl_id)
    return impl.about.title if impl is not None else nid


def _decision_plain(s: ReportSection, explained: Mapping[str, str]) -> str:
    """A decision in the summary: its explanation, else "Difference? yes"."""
    text = explained.get(s.node_id or "")
    if text:
        return text.rstrip(".")
    answer = "yes" if s.status == "then" else "no"
    return f"{s.title} {answer}" if s.title.endswith("?") else f"{s.title}: {answer}"


def _summary(
    sections: Sequence[ReportSection],
    model_ref: str | None,
    explained: Mapping[str, str] | None = None,
) -> str:
    explained = explained or {}
    tests = [s for s in sections if s.kind == "test"]
    decisions = [s for s in sections if s.kind == "decision"]
    failed = [s for s in sections if s.kind == "failed"]
    selection = next((s for s in sections if s.kind == "selection"), None)
    parts = []
    if tests:
        parts.append(f"{len(tests)} test(s) ran")
    if decisions:
        said = [_decision_plain(s, explained) for s in decisions]
        # one sentence for each decision (an explanation can have more than one sentence)
        joined = ". ".join(said)
        every = all(s.node_id in explained for s in decisions)
        parts.append(joined if every else f"decisions: {joined}")
    if selection is not None:
        parts.append(selection.text.rstrip("."))
    elif model_ref:
        parts.append(f"the model is {model_ref}")
    if failed:
        parts.append(f"{len(failed)} step(s) failed")
    return (". ".join(p[0].upper() + p[1:] for p in parts if p) + ".") if parts else "Done."


# ============================================================================ evidence pack


def evidence_pack(
    params: Mapping[str, JsonValue],
    model_ref: str | None,
    evidence: Sequence[BoundValue],
    *,
    lineage: Mapping[str, JsonValue] | None = None,
    scorecard: Mapping[str, JsonValue] | None = None,
) -> Result:
    """The ``report.evidence_pack`` payload: governance evidence, lineage and scorecard of a
    model version as one plain-language report (the HTML rendering is the governance
    export's)."""
    sections: list[ReportSection] = []
    passed: bool | None = None
    for v in evidence:
        if not isinstance(v.ref.inline, dict):
            continue
        try:
            ev = EvidencePayload.model_validate(v.ref.inline)
        except ValidationError:
            continue
        passed = ev.passed if passed is None else passed and ev.passed
        rows: list[dict[str, JsonValue]] = [
            {
                k: r.get(k)
                for k in (
                    "check",
                    "status",
                    "severity",
                    "metric",
                    "value",
                    "threshold",
                    "message_plain",
                )
            }
            for r in ev.results
        ]
        sections.append(
            ReportSection(
                title="Governance checks",
                node_id=v.source,
                kind="evidence",
                status=ev.status,
                text=ev.message_plain or ev.status,
                table=TablePayload(
                    title="Check results",
                    kind="evidence",
                    columns=[
                        "check",
                        "status",
                        "severity",
                        "metric",
                        "value",
                        "threshold",
                        "message_plain",
                    ],
                    rows=rows,
                ),
            )
        )
    if lineage:
        sections.append(
            ReportSection(
                title="Lineage",
                kind="lineage",
                text="; ".join(f"{k}: {_short(v)}" for k, v in sorted(lineage.items())),
            )
        )
    if scorecard:
        sections.append(
            ReportSection(
                title="Scorecard",
                kind="scorecard",
                text=f"Overall: {scorecard.get('overall', 'n/a')}",
            )
        )
    if passed is None:
        summary = "This model has no governance evidence."
    elif passed:
        summary = "All the blocking governance checks passed."
    else:
        summary = "At least one blocking governance check failed."
    report = ReportPayload(
        title=f"Evidence pack for {model_ref or 'the model'}",
        summary_plain=summary,
        sections=sections,
        model=model_ref,
    )
    result: dict[str, JsonValue] = {
        "format": str(params.get("format") or "json"),
        "passed": passed,
        "summary_plain": summary,
    }
    return result, {"report": inline(PortType.REPORT, report)}


# ================================================================================ dispatch

ORCHESTRATOR_NODES: Final = frozenset(
    {"compare.models", "select.best", "select.threshold", "report.summary"}
)


def run_orchestrator_node(
    impl_id: str,
    params: Mapping[str, JsonValue],
    inputs: Mapping[str, Sequence[BoundValue]],
    ctx: OrchestratorContext,
) -> Result:
    """Run one orchestrator node (raises ``MapiError`` with a plain message)."""
    if impl_id == "compare.models":
        return compare_models(params, inputs.get("candidates", ()))
    if impl_id == "select.best":
        return select_best(
            params,
            inputs.get("table", ()),
            inputs.get("models", ()),
            name=lambda row: _candidate_name(ctx, row),
        )
    if impl_id == "select.threshold":
        return select_threshold(params, inputs.get("table", ()))
    if impl_id == "report.summary":
        return report_summary(params, inputs, ctx)
    raise MapiError(f"{impl_id} is not an orchestrator node", code=ErrorCode.NODE_UNKNOWN)
