"""JSON payloads that flow along workflow edges (inline :class:`NodeOutputRef` values).

Datasets and model versions travel as references; everything else is small JSON with a
``format`` tag so a reader can tell what it holds:

=============  ====================  ===================================================
port type      format                content
=============  ====================  ===================================================
test_result    (StatTestResult)      :class:`mapi_spec.stattests.StatTestResult`
split_plan     ``mapi.split-plan/v1`` the plan, the dim and length it was cut over, folds
metrics        ``mapi.metrics/v1``    one candidate per model or benchmark: averages, per
                                     horizon, per fold, and the model ref it came from
table          ``mapi.table/v1``      columns and rows (search grids, comparisons)
report         ``mapi.report/v1``     a plain-language report: summary and sections (a
                                     ``plot`` section carries a plot payload)
plot           (PlotSpec)            :class:`mapi_spec.plots.PlotSpec` JSON: ``kind``,
                                     ``spec`` and ``inline_arrow`` datasets
evidence       ``mapi.evidence/v1``   governance :class:`~mapi_spec.governance.CheckResult` s
=============  ====================  ===================================================

These models are pure Pydantic: the control plane reads them (orchestrator nodes,
reports), the runtime writes them.
"""

from __future__ import annotations

import math
from typing import Any, Final, Literal

from mapi_spec.workflow import NodeOutputRef
from mapi_spec.workflow_nodes import PortType
from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "EVIDENCE_FORMAT",
    "METRICS_FORMAT",
    "REPORT_FORMAT",
    "SPLIT_PLAN_FORMAT",
    "TABLE_FORMAT",
    "EvidencePayload",
    "MetricsCandidate",
    "MetricsPayload",
    "ReportPayload",
    "ReportSection",
    "SplitPlanPayload",
    "TablePayload",
    "clean_number",
    "inline",
]

METRICS_FORMAT: Final = "mapi.metrics/v1"
TABLE_FORMAT: Final = "mapi.table/v1"
REPORT_FORMAT: Final = "mapi.report/v1"
EVIDENCE_FORMAT: Final = "mapi.evidence/v1"
SPLIT_PLAN_FORMAT: Final = "mapi.split-plan/v1"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


def clean_number(value: object) -> float | None:
    """A finite float, or ``None`` (JSON has no NaN or infinity)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class MetricsCandidate(_Payload):
    """The scores of one model or benchmark on a set of test periods.

    ``name`` is the candidate's id within its node (the node id for a model, the method for
    a benchmark); ``group`` is the node id. ``kind`` tells benchmarks from models;
    ``model``/``settings`` are what a refit of the winner needs.
    """

    name: str
    group: str
    kind: Literal["model", "baseline"] = "model"
    model: str | None = None
    settings: dict[str, JsonValue] | None = None
    metrics: dict[str, float | None] = Field(default_factory=dict)
    by_horizon: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    by_fold: list[dict[str, float | None]] = Field(default_factory=list)
    n_folds: int = 0
    errors: list[list[float | None]] | None = Field(
        None, description="Per fold, the forecast errors of each test period (for tests)"
    )
    origins: list[int | None] | None = Field(
        None, description="Per fold, the position of its first test period (fold spacing)"
    )

    @property
    def key(self) -> str:
        return self.group if self.name == self.group else f"{self.group}.{self.name}"


class MetricsPayload(_Payload):
    format: Literal["mapi.metrics/v1"] = METRICS_FORMAT
    candidates: list[MetricsCandidate] = Field(default_factory=list)
    horizons: list[int] = Field(default_factory=list)
    note_plain: str | None = None


class TablePayload(_Payload):
    format: Literal["mapi.table/v1"] = TABLE_FORMAT
    title: str = ""
    kind: str = "table"
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, JsonValue]] = Field(default_factory=list)
    meta: dict[str, JsonValue] = Field(default_factory=dict)


class ReportSection(_Payload):
    """One section of a report. ``kind`` is ``text``, ``test``, ``decision``, ``table``,
    ``metrics``, ``plot``, ``selection``, ``search``, ``skipped``, ``failed`` or ``model``
    (evidence packs add ``evidence``, ``lineage`` and ``scorecard``)."""

    title: str
    node_id: str | None = None
    kind: str = "text"
    status: str | None = None
    text: str = ""
    table: TablePayload | None = None
    plot: dict[str, JsonValue] | None = Field(
        None,
        description="A plot section's PlotSpec JSON (None when it is too large to embed: "
        "the node output has it)",
    )


class ReportPayload(_Payload):
    format: Literal["mapi.report/v1"] = REPORT_FORMAT
    title: str
    summary_plain: str
    sections: list[ReportSection] = Field(default_factory=list)
    model: str | None = None


class EvidencePayload(_Payload):
    format: Literal["mapi.evidence/v1"] = EVIDENCE_FORMAT
    model: str | None = None
    status: Literal["checks_passed", "checks_failed"]
    passed: bool
    failed_checks: list[str] = Field(default_factory=list)
    results: list[dict[str, JsonValue]] = Field(default_factory=list)
    policies: list[str] = Field(default_factory=list)
    message_plain: str = ""


class SplitPlanPayload(_Payload):
    format: Literal["mapi.split-plan/v1"] = SPLIT_PLAN_FORMAT
    plan: dict[str, JsonValue]
    dim: str | None = None
    n: int | None = None
    labels: list[JsonValue] = Field(default_factory=list, description="First and last label")
    folds: list[dict[str, JsonValue]] = Field(default_factory=list)


def inline(type_: PortType, payload: BaseModel | dict[str, Any]) -> NodeOutputRef:
    """A :class:`NodeOutputRef` carrying ``payload`` inline."""
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return NodeOutputRef(type=type_, inline=data)
