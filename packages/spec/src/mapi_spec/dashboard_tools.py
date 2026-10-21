"""The read-only dashboard tool surface (v0.16; the v0.17 chat and MCP contract).

:data:`DASHBOARD_TOOLS` declares what an assistant may ask of one dashboard: read it, read
its readouts, compare two readouts, follow one channel over time, explain a word, and
evaluate a what-if on the response grid. Every tool is read-only: no tool submits a run or
writes a row. The server implements them over a dashboard context
(``mapi_server.services.dashboards.tools.call_tool``) and the member, client and share read
routes call those same functions, so the surface is exercised before any chat exists. The
results are the DTOs the HTTP routes return.

:func:`tools_json_schema` exports the declarations as ``{name, description, parameters,
result}`` JSON Schemas (the shape Gemini function declarations and MCP tools take).

v0.17 adds read-only tools for layout 2 dashboards (panels, accuracy, inputs, the caller's
own predictions and batch summaries) and ``panel_deltas`` on :class:`ReadoutComparison`.
The media tools (``get_channel``, ``evaluate_whatif``) answer ``DASHBOARD_INPUT_DISABLED`` on
dashboards without the media planner.

Do not rename or remove a tool, or remove an argument: the v0.18 adapters (chat and MCP) map
onto these 1:1. New tools and new optional arguments are additive.
"""

from __future__ import annotations

from typing import Final, Literal, NamedTuple, cast

from pydantic import AwareDatetime, Field, JsonValue

from mapi_spec._dto import EXAMPLE_TS, RequestModel, ResponseModel, dto_examples
from mapi_spec._types import Name
from mapi_spec.dashboard_v2 import (
    MAX_PANELS,
    PANEL_ID_RE,
    AccuracyDetail,
    AccuracyPoint,
    BatchForm,
    BatchSummary,
    PanelBlock,
    PanelMeta,
    PredictionResult,
    ScenarioForm,
)
from mapi_spec.dashboards import (
    GLOSSARY,
    ClientDashboard,
    EffectiveInputs,
    GlossaryKey,
    Readout,
    ReadoutHeader,
    ReadoutKind,
    ReadoutList,
    TileKey,
    WhatIfRequest,
    WhatIfResult,
    Zone,
)

__all__ = [
    "DASHBOARD_TOOLS",
    "DASHBOARD_TOOL_NAMES",
    "MAX_CHANNEL_HISTORY",
    "MAX_TOOL_READOUTS",
    "V016_TOOL_NAMES",
    "AccuracyReport",
    "BatchReport",
    "ChannelArgs",
    "ChannelDelta",
    "ChannelHistory",
    "ChannelPoint",
    "CompareArgs",
    "ExplainArgs",
    "InputsReport",
    "ListReadoutsArgs",
    "MetricExplanation",
    "NoArgs",
    "OptionalReadoutRef",
    "PanelArgs",
    "PanelDelta",
    "PanelList",
    "PanelReport",
    "PredictionReport",
    "PrincipalKind",
    "ReadoutComparison",
    "ReadoutRef",
    "TileDelta",
    "ToolDecl",
    "explain",
    "tool",
    "tools_json_schema",
]

MAX_TOOL_READOUTS: Final = 50
MAX_CHANNEL_HISTORY: Final = 26

PrincipalKind = Literal["member", "client", "share"]
"""Who calls a tool: a project member, a client user or a share-link session."""


# ===================================================================================== args


class NoArgs(RequestModel):
    """No arguments."""


class ListReadoutsArgs(RequestModel):
    """Readouts newest first; ``before_seq`` pages back."""

    kind: ReadoutKind | None = None
    before_seq: int | None = Field(None, ge=1)
    limit: int = Field(20, ge=1, le=MAX_TOOL_READOUTS)


class ReadoutRef(RequestModel):
    seq: int = Field(..., ge=1)


class CompareArgs(RequestModel):
    """Two readouts to compare: ``b`` against ``a`` (usually the older one)."""

    a_seq: int = Field(..., ge=1)
    b_seq: int = Field(..., ge=1)


class ChannelArgs(RequestModel):
    """One channel over the last ``last_n`` published readouts."""

    name: Name
    last_n: int = Field(13, ge=1, le=MAX_CHANNEL_HISTORY)


class ExplainArgs(RequestModel):
    metric: GlossaryKey


class OptionalReadoutRef(RequestModel):
    """One readout by its number; without ``seq``, the latest published readout."""

    seq: int | None = Field(None, ge=1)


class PanelArgs(RequestModel):
    """One panel of a readout (the latest published readout without ``seq``)."""

    panel_id: str = Field(..., pattern=PANEL_ID_RE)
    seq: int | None = Field(None, ge=1)


# ================================================================================== results


class TileDelta(ResponseModel):
    key: TileKey
    label: str = Field(..., max_length=80)
    a: float | None = None
    b: float | None = None
    delta: float | None = Field(None, description="b - a")


class ChannelDelta(ResponseModel):
    name: Name
    label: str = Field(..., max_length=60)
    spend_a: float | None = None
    spend_b: float | None = None
    contribution_a: float | None = None
    contribution_b: float | None = None
    roi_a: float | None = None
    roi_b: float | None = None
    zone_a: Zone | None = None
    zone_b: Zone | None = None


class PanelDelta(ResponseModel):
    """The change of one KPI panel (layout 2) between two readouts."""

    panel_id: str = Field(..., pattern=PANEL_ID_RE)
    label: str = Field(..., max_length=80)
    a: float | None = None
    b: float | None = None
    delta: float | None = Field(None, description="b - a")


class ReadoutComparison(ResponseModel):
    """Tile, channel and (layout 2) KPI panel changes between two readouts. ``same_model``
    is false when the readouts used different model versions (the numbers are then less
    comparable)."""

    a: ReadoutHeader
    b: ReadoutHeader
    same_model: bool
    tiles: list[TileDelta] = Field(default_factory=list, max_length=6)
    channels: list[ChannelDelta] = Field(default_factory=list, max_length=40)
    lead: str = Field(..., max_length=400)
    panel_deltas: list[PanelDelta] = Field(default_factory=list, max_length=MAX_PANELS)

    model_config = dto_examples(
        {
            "a": {**ReadoutHeader.model_json_schema()["examples"][0], "seq": 11},
            "b": ReadoutHeader.model_json_schema()["examples"][0],
            "same_model": True,
            "tiles": [{"key": "expected", "label": "Orders from media", "a": 4090.0,
                       "b": 4210.0, "delta": 120.0}],
            "channels": [{"name": "tv", "label": "TV", "roi_a": 1.9, "roi_b": 1.86}],
            "lead": "Media brought 120 more orders than in the readout before.",
        }
    )  # fmt: skip


class ChannelPoint(ResponseModel):
    seq: int = Field(..., ge=1)
    created_at: AwareDatetime
    model_version_id: str = Field(..., max_length=128)
    spend: float | None = None
    contribution: float | None = None
    roi: float | None = None
    mroi: float | None = None
    zone: Zone | None = None


class ChannelHistory(ResponseModel):
    """One channel over published readouts, oldest first."""

    name: Name
    label: str = Field(..., max_length=60)
    points: list[ChannelPoint] = Field(default_factory=list, max_length=MAX_CHANNEL_HISTORY)

    model_config = dto_examples(
        {
            "name": "tv",
            "label": "TV",
            "points": [
                {"seq": 11, "created_at": EXAMPLE_TS, "model_version_id": "mv_01", "roi": 1.9},
                {"seq": 12, "created_at": EXAMPLE_TS, "model_version_id": "mv_01", "roi": 1.86,
                 "zone": "over"},
            ],
        }
    )  # fmt: skip


class MetricExplanation(ResponseModel):
    metric: GlossaryKey
    text: str = Field(..., max_length=600)

    model_config = dto_examples({"metric": "return_on_media", "text": GLOSSARY["return_on_media"]})


class PanelList(ResponseModel):
    """The panels of the published revision, in reading order (layout 2; empty on layout 1)."""

    panels: list[PanelMeta] = Field(default_factory=list, max_length=MAX_PANELS)

    model_config = dto_examples(
        {"panels": [{"id": "next", "kind": "kpi", "title": "Sales next month"},
                    {"id": "fan", "kind": "chart", "title": "Sales and forecast",
                     "intent": "fan"}]}
    )  # fmt: skip


class PanelReport(ResponseModel):
    """One panel block of a readout, as the readout shows it."""

    seq: int = Field(..., ge=1)
    panel: PanelBlock


class AccuracyReport(ResponseModel):
    """The accuracy of a readout's actuals (``detail``) and the history over published
    actuals readouts, oldest first."""

    seq: int | None = Field(None, ge=1)
    detail: AccuracyDetail | None = None
    history: list[AccuracyPoint] = Field(default_factory=list, max_length=104)


class InputsReport(ResponseModel):
    """What the caller may send (inputs, narrowed by a share link), the forms of scenario
    rows and batch files, and the daily caps."""

    inputs: EffectiveInputs
    scenario_form: ScenarioForm | None = None
    batch_form: BatchForm | None = None
    exact_runs_per_day: int | None = Field(None, ge=0)
    batch_runs_per_day: int | None = Field(None, ge=0)


class PredictionReport(ResponseModel):
    """The result of the caller's own scenario rows readout (private rows of others are
    never returned)."""

    readout: ReadoutHeader
    prediction: PredictionResult | None = None


class BatchReport(ResponseModel):
    """The summary of the caller's own batch readout (no CSV and no download link)."""

    readout: ReadoutHeader
    summary: BatchSummary | None = None


def explain(metric: GlossaryKey) -> MetricExplanation:
    """The ``explain_metric`` tool (pure: the glossary is static)."""
    return MetricExplanation(metric=metric, text=GLOSSARY[metric])


# ============================================================================== declarations


class ToolDecl(NamedTuple):
    """One read-only tool: its name, a plain summary, and its argument and result DTOs."""

    name: str
    summary_plain: str
    args: type[RequestModel]
    result: type[ResponseModel]
    read_only: Literal[True]


DASHBOARD_TOOLS: Final[tuple[ToolDecl, ...]] = (
    ToolDecl(
        "get_dashboard",
        "Get the dashboard: its title, KPI, channels, tabs, inputs and latest readout.",
        NoArgs, ClientDashboard, True,
    ),
    ToolDecl(
        "get_latest_readout",
        "Get the latest published readout with its tiles, channels and series.",
        NoArgs, Readout, True,
    ),
    ToolDecl(
        "list_readouts",
        "List the readouts, newest first. You can filter by kind and page back with "
        "before_seq.",
        ListReadoutsArgs, ReadoutList, True,
    ),
    ToolDecl(
        "get_readout",
        "Get one readout by its number (seq).",
        ReadoutRef, Readout, True,
    ),
    ToolDecl(
        "compare_readouts",
        "Compare two readouts: the change of each tile and channel from a_seq to b_seq.",
        CompareArgs, ReadoutComparison, True,
    ),
    ToolDecl(
        "get_channel",
        "Get one channel over the last published readouts: spend, contribution, return and "
        "zone.",
        ChannelArgs, ChannelHistory, True,
    ),
    ToolDecl(
        "explain_metric",
        "Explain a word or a metric of the dashboard in plain language.",
        ExplainArgs, MetricExplanation, True,
    ),
    ToolDecl(
        "evaluate_whatif",
        "Calculate the result of a spend plan on the response grid. Give a multiplier per "
        "channel (1 is the current plan). This does not start a run and saves nothing.",
        WhatIfRequest, WhatIfResult, True,
    ),
    # v0.17: layout 2 dashboards
    ToolDecl(
        "list_panels",
        "List the panels of the dashboard: their ids, kinds and titles, in reading order.",
        NoArgs, PanelList, True,
    ),
    ToolDecl(
        "get_panel",
        "Get one panel of a readout by its id (the latest published readout without seq).",
        PanelArgs, PanelReport, True,
    ),
    ToolDecl(
        "get_accuracy",
        "Get how near the model was to the actual numbers of a readout, and the accuracy "
        "over time.",
        OptionalReadoutRef, AccuracyReport, True,
    ),
    ToolDecl(
        "list_inputs",
        "List what you can send to the dashboard: the values of a case, a file of cases or "
        "actuals, with their ranges and daily limits.",
        NoArgs, InputsReport, True,
    ),
    ToolDecl(
        "get_prediction",
        "Get the result of one of your own scenario rows readouts by its number (seq).",
        ReadoutRef, PredictionReport, True,
    ),
    ToolDecl(
        "get_batch_summary",
        "Get the summary of one of your own batch readouts by its number (seq). This does "
        "not give the file.",
        ReadoutRef, BatchReport, True,
    ),
)  # fmt: skip
"""The tool surface. Every tool is read-only (``read_only`` is always ``True``)."""

DASHBOARD_TOOL_NAMES: Final[tuple[str, ...]] = tuple(t.name for t in DASHBOARD_TOOLS)

V016_TOOL_NAMES: Final[tuple[str, ...]] = (
    "get_dashboard", "get_latest_readout", "list_readouts", "get_readout",
    "compare_readouts", "get_channel", "explain_metric", "evaluate_whatif",
)  # fmt: skip
"""The v0.16 tools, unchanged in name and arguments (the v0.17 surface is a superset)."""

_BY_NAME: Final[dict[str, ToolDecl]] = {t.name: t for t in DASHBOARD_TOOLS}


def tool(name: str) -> ToolDecl:
    """The declaration of a tool (``KeyError`` for unknown names)."""
    return _BY_NAME[name]


def tools_json_schema() -> list[dict[str, JsonValue]]:
    """``[{name, description, parameters, result}]``: each tool's argument and result JSON
    Schemas (validation mode; ``$defs`` kept inline per tool), in :data:`DASHBOARD_TOOLS`
    order."""
    return [
        {
            "name": t.name,
            "description": t.summary_plain,
            "parameters": cast(JsonValue, t.args.model_json_schema(mode="validation")),
            "result": cast(JsonValue, t.result.model_json_schema(mode="serialization")),
        }
        for t in DASHBOARD_TOOLS
    ]
