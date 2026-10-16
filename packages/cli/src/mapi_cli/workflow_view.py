"""Workflow runs in the terminal: a live node-status board while a run goes, and the
summary afterwards.

The board follows the run's server-sent events (``Client.workflows.stream_events``): the
``snapshot`` fills it, every ``workflow.node.*`` event moves one node, and every
``workflow.branch.decided`` shows which arm was taken. On a terminal it redraws in place
(rich ``Live``, on stderr); elsewhere it prints one line per change, so logs stay readable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final

import typer
from mapi_client import Client, WorkflowEnd, WorkflowEventFrame, WorkflowSnapshot
from mapi_spec.api import NodeRun, WorkflowRun
from mapi_spec.workflow import NodeRunStatus
from rich.console import Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from mapi_cli.output import Output, safe

__all__ = ["STATUS_STYLE", "node_summary", "print_run", "watch_run"]

STATUS_STYLE: Final = {
    "pending": "dim",
    "queued": "cyan",
    "running": "bold cyan",
    "succeeded": "green",
    "cached": "green",
    "skipped": "dim",
    "failed": "bold red",
    "cancelled": "yellow",
    "cancelling": "yellow",
    "retrying": "yellow",
}
_ICON: Final = {
    "pending": "·",
    "queued": "○",
    "running": "▶",
    "succeeded": "✓",
    "cached": "✓",
    "skipped": "–",
    "failed": "✗",
    "cancelled": "■",
    "retrying": "↻",
}
_EVENT_STATUS: Final = {
    "workflow.node.queued": "queued",
    "workflow.node.started": "running",
    "workflow.node.succeeded": "succeeded",
    "workflow.node.cached": "cached",
    "workflow.node.skipped": "skipped",
    "workflow.node.failed": "failed",
    "workflow.node.retrying": "retrying",
}
_SUMMARY_KEYS: Final = (
    "verdict_plain",
    "reason_plain",
    "summary_plain",
    "message_plain",
    "decision",
)


def _short(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def node_summary(node: NodeRun) -> str:
    """One line about what a node found (its plain verdict when it has one)."""
    if node.error is not None:
        return f"{node.error.code.value}: {node.error.message}"
    if node.skipped_reason:
        return node.skipped_reason
    result = node.result or {}
    for key in _SUMMARY_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value:
            return _short(value)
    if "passed" in result:
        failed = result.get("failed_checks") or []
        return "passed" if result["passed"] else f"failed: {_short(failed, 60)}"
    if "arm" in result:
        return f"took the {result['arm']} arm"
    picked = {
        k: result[k] for k in ("p_value", "statistic", "best_p", "best_q", "rank") if k in result
    }
    return _short(picked) if picked else ""


def _duration(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return ""
    seconds = (end - start).total_seconds()
    return f"{seconds:.1f}s" if seconds < 60 else f"{seconds / 60:.1f}m"


def _status_text(status: str) -> Text:
    return Text(f"{_ICON.get(status, '?')} {status}", style=STATUS_STYLE.get(status, ""))


class _Board:
    """The live state of one run's nodes, fed by snapshot and events."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.order: list[str] = []
        self.status: dict[str, str] = {}
        self.kind: dict[str, str] = {}
        self.note: dict[str, str] = {}
        self.run_status = "queued"

    def load(self, run: WorkflowRun) -> None:
        self.run_status = run.status.value
        for n in run.nodes:
            if n.node_id not in self.status:
                self.order.append(n.node_id)
            self.status[n.node_id] = n.status.value
            self.kind[n.node_id] = n.kind.value
            if n.status.done:
                self.note[n.node_id] = node_summary(n)
        for d in run.decisions:
            self.note[d.node_id] = f"took the {d.arm} arm"

    def apply(self, frame: WorkflowEventFrame) -> str | None:
        """Update from an event; returns a one-line description of the change."""
        node = frame.node_id
        data: Mapping[str, Any] = frame.event.data
        status = _EVENT_STATUS.get(frame.type)
        if node is not None and status is not None:
            if node not in self.status:
                self.order.append(node)
            self.status[node] = status
            reason = data.get("reason") or data.get("message") or data.get("error")
            if isinstance(reason, str):
                self.note[node] = _short(reason)
            return f"{node} {status}" + (
                f": {_short(reason, 80)}" if isinstance(reason, str) else ""
            )
        if frame.type == "workflow.branch.decided" and node is not None:
            arm = data.get("arm")
            self.note[node] = f"took the {arm} arm"
            return f"{node} took the {arm} arm"
        if frame.type.startswith("workflow.run."):
            self.run_status = frame.type.removeprefix("workflow.run.")
            return f"run {self.run_status}"
        return None

    def render(self) -> Table:
        done = sum(1 for s in self.status.values() if s in ("succeeded", "cached", "skipped"))
        table = Table(
            title=f"workflow run {self.run_id} - {self.run_status} ({done}/{len(self.order)})",
            show_edge=False,
            header_style="bold",
        )
        for col in ("node", "kind", "status", "note"):
            table.add_column(col, overflow="fold")
        for nid in self.order:
            st = self.status.get(nid, "pending")
            table.add_row(
                safe(nid),
                safe(self.kind.get(nid, "")),
                _status_text(st),
                safe(self.note.get(nid, "")),
            )
        return table


def watch_run(
    api: Client, out: Output, run_id: str, *, last_event_id: int | None = None
) -> WorkflowRun:
    """Follow a run until it ends; returns the final run (fetched afresh)."""
    if out.json_mode:
        for item in api.workflows.stream_events(run_id, last_event_id=last_event_id):
            if isinstance(item, WorkflowSnapshot):
                doc: dict[str, Any] = {"event": "snapshot", "run": item.run.model_dump(mode="json")}
            elif isinstance(item, WorkflowEventFrame):
                doc = {"event": item.type, "data": item.event.model_dump(mode="json")}
            else:
                doc = {
                    "event": "end",
                    "run_id": item.run_id,
                    "status": getattr(item.status, "value", None),
                }
            typer.echo(json.dumps(doc))
        return api.workflows.get_run(run_id)
    board = _Board(run_id)
    if out.err.is_terminal:
        with Live(board.render(), console=out.err, refresh_per_second=8, transient=True) as live:
            for item in api.workflows.stream_events(run_id, last_event_id=last_event_id):
                if isinstance(item, WorkflowSnapshot):
                    board.load(item.run)
                elif isinstance(item, WorkflowEventFrame):
                    board.apply(item)
                live.update(Group(board.render()))
                if isinstance(item, WorkflowEnd):
                    break
    else:
        for item in api.workflows.stream_events(run_id, last_event_id=last_event_id):
            if isinstance(item, WorkflowSnapshot):
                board.load(item.run)
                out.info(f"following workflow run {run_id} ({len(board.order)} nodes)")
            elif isinstance(item, WorkflowEventFrame):
                line = board.apply(item)
                if line:
                    out.info(f"  {line}")
    return api.workflows.get_run(run_id)


def print_run(out: Output, run: WorkflowRun) -> None:
    """The run's summary: status, every node with what it found, decisions, outputs."""
    if out.json_mode:
        out.emit_json(run)
        return
    style = STATUS_STYLE.get(run.status.value, "")
    out.kv(
        [
            ("run", run.run_id),
            ("workflow", f"{run.workflow_id} (revision {run.revision})"),
            ("status", safe(run.status.value, style)),
            ("trigger", run.trigger.value),
            ("inputs", ", ".join(f"{k}={v}" for k, v in run.inputs.items())),
            ("params", json.dumps(run.params) if run.params else None),
            ("took", _duration(run.started_at, run.finished_at) or None),
        ]
    )
    out.table(
        ["node", "kind", "status", "took", "found"],
        [
            (
                n.node_id,
                n.kind.value,
                _status_text(n.status.value),
                _duration(n.started_at, n.finished_at),
                node_summary(n) + (f" (cached from {n.cached_from})" if n.cached_from else ""),
            )
            for n in run.nodes
        ],
    )
    if run.decisions:
        out.table(
            ["branch", "arm", "condition", "read"],
            [
                (d.node_id, d.arm, d.condition, json.dumps(d.inputs, default=str))
                for d in run.decisions
            ],
            title="Decisions",
        )
    if run.outputs:
        out.table(
            ["output", "type", "where"],
            [
                (name, ref.type.value, ref.ref or (ref.object or "") or _short(ref.inline))
                for name, ref in run.outputs.items()
            ],
            title="Outputs",
        )
    if run.error is not None:
        out.err.print(
            Text("error ", style="bold red")
            + safe(run.error.code.value)
            + Text(": ")
            + safe(run.error.message)
        )
    failed = [n for n in run.nodes if n.status is NodeRunStatus.FAILED and n.error is not None]
    for n in failed:
        if n.error is not None and n.error.untrusted_text:
            out.err.print(safe(f"[{n.node_id}] {n.error.untrusted_text}", "dim"))
