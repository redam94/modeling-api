"""Client dashboards, share links and continuous readouts (v0.16).

An analyst builds a **dashboard** on a fitted model version (or an endpoint) and publishes
it for people outside the team:

* signed-in users with the ``client`` project role see the dashboards granted to them
  (published revisions only, nothing else of the app);
* anonymous holders of a revocable **share link** (``mpd_<40 base32>``, optional passcode,
  expiry) see one dashboard through a short-lived session (``mps_…``, header
  :data:`SHARE_SESSION_HEADER`).

A dashboard has an editable draft (:class:`DashboardSpec`, stored as JSON) and immutable
published revisions (:class:`DashboardRevision`) that pin the model version. Its history is
an append-only list of **readouts** (:class:`Readout`): capped JSON snapshots of the KPI
tiles, the channel scorecard and a few series, built server-side from score summaries and
tracking metrics. Three inputs never refit the model: scenario what-ifs (the response grid
in the browser, or an exact score run), an upload of new actuals (compared with, or for
capable models conditioned on, the model's prediction) and a scheduled or event refresh.
An upload can also *ask* the analyst for a refit (:class:`DashboardRefitRequest`); only an
editor with runner rights starts it.

Everything here is plain pydantic: no numpy, no I/O. :func:`metrics_of` is a pure-Python
port of ``features/score/compare.ts`` ``metricsOf`` (the same golden values), and
:func:`zone_of` places a channel on its response curve from its return and marginal return.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Final, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from mapi_spec._dto import EXAMPLE_TS, Id, RequestModel, ResponseModel, Sha256, dto_examples
from mapi_spec._types import Name
from mapi_spec.actuals import ForecastConditioning
from mapi_spec.endpoints import InputTable
from mapi_spec.errors import Issue, Problem
from mapi_spec.loader import canonical_json, spec_hash
from mapi_spec.views import ResponseGridView
from mapi_spec.xrjson import XrJsonDataset

__all__ = [
    "DASHBOARD_SHARE_TOKEN_RE",
    "GLOSSARY",
    "MAX_ACTUALS_COLS",
    "MAX_ACTUALS_ROWS",
    "MAX_CHANNELS",
    "MAX_PASSCODE_LEN",
    "MAX_READOUT_BYTES",
    "MAX_SERIES_POINTS",
    "MAX_SHARE_DAYS",
    "MAX_SPEC_BYTES",
    "MIN_PASSCODE_LEN",
    "SHARE_SESSION_HEADER",
    "SHARE_SESSION_RE",
    "ActualsAccuracy",
    "ActualsInputs",
    "ActualsSubmit",
    "ActualsTemplate",
    "ChannelDef",
    "ChannelReadout",
    "ClientDashboard",
    "Dashboard",
    "DashboardCreate",
    "DashboardDraftUpdate",
    "DashboardGrant",
    "DashboardGrantPut",
    "DashboardGrid",
    "DashboardLimits",
    "DashboardRefitRequest",
    "DashboardRevision",
    "DashboardShare",
    "DashboardShareCreate",
    "DashboardShareCreated",
    "DashboardSource",
    "DashboardSpec",
    "DashboardStatus",
    "DashboardSummary",
    "DashboardValidation",
    "EffectiveInputs",
    "GlossaryKey",
    "KpiDef",
    "KpiValue",
    "Readout",
    "ReadoutHeader",
    "ReadoutKind",
    "ReadoutList",
    "ReadoutSnapshot",
    "ReadoutStatus",
    "ReadoutTrigger",
    "ReadoutVisibility",
    "RefitRequestDecision",
    "RefitRequestStatus",
    "RefreshPolicy",
    "RequesterKind",
    "ScenarioSave",
    "Series",
    "SeriesPoint",
    "ShareInput",
    "ShareSession",
    "ShareSessionCreate",
    "TabKey",
    "TileKey",
    "TileUnit",
    "WhatIfInputs",
    "WhatIfRequest",
    "WhatIfResult",
    "Zone",
    "breakeven_of",
    "dashboard_spec_hash",
    "generate_share_token",
    "metrics_of",
    "readout_cache_key",
    "share_display",
    "zone_of",
]

# ===================================================================================== limits

DASHBOARD_SHARE_TOKEN_RE: Final = re.compile(r"^mpd_[0-9a-hjkmnp-tv-z]{40}$")
"""Share tokens: ``mpd_`` + 40 Crockford base32 characters (200 random bits)."""
SHARE_SESSION_RE: Final = re.compile(r"^mps_[A-Za-z0-9_-]{16,400}\.[A-Za-z0-9_-]{43}$")
"""Share sessions: ``mps_<b64url(json body)>.<b64url(HMAC-SHA256)>`` (minted by the server)."""
SHARE_SESSION_HEADER: Final = "X-Mapi-Share"
"""The header that carries a share session on every ``/v1/shared/…`` request."""
MAX_SHARE_DAYS: Final = 365
MIN_PASSCODE_LEN: Final = 8
MAX_PASSCODE_LEN: Final = 128
MAX_READOUT_BYTES: Final = 262_144
"""Cap on a serialised :class:`ReadoutSnapshot` (256 KiB)."""
MAX_SERIES_POINTS: Final = 520
"""Points over every series of one snapshot (ten years of weeks)."""
MAX_SPEC_BYTES: Final = 65_536
"""Cap on a serialised :class:`DashboardSpec` (the database checks the same)."""
MAX_ACTUALS_ROWS: Final = 520
MAX_ACTUALS_COLS: Final = 64
MAX_CHANNELS: Final = 40
MAX_TILES: Final = 4
MAX_NOTES: Final = 20
MAX_NOTE_CHARS: Final = 400

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # the embed-token alphabet (mapi_spec.sharing)
_DSH = "dsh_01jaf3r1s2t3v4w5x6y7z8a9b0"
_DSR = "dsr_01jaf3s1t2v3w4x5y6z7a8b9c0"
_DSS = "dss_01jaf3t1v2w3x4y5z6a7b8c9d0"
_DRR = "drr_01jaf3v1w2x3y4z5a6b7c8d9e0"
_USR = "usr_01jaf3a9b8c7d6e5f4g3h2j1k0"
_CLIENT_USR = "usr_01jaf3c1l2i3e4n5t6a7b8c9d0"
_MV = "mv_01jaf3e1f2g3h4j5k6m7n8p9q0"
_RUN = "run_01jaf3q2x8m9v7k5h4g3f2e1d0"
_TOKEN_EXAMPLE = "mpd_7k2m9q4x0123456789abcdefghjkmnpqrstvwxyz"
_SESSION_EXAMPLE = "mps_eyJzaWQiOiJkc3NfMDEiLCJleHAiOjE3OTk5OTk5OTksInYiOjF9." + "A" * 43


def generate_share_token() -> str:
    """A new share token (shown once; the server stores only its keyed digest)."""
    return "mpd_" + "".join(secrets.choice(_ALPHABET) for _ in range(40))


def share_display(token: str) -> str:
    """The redacted form of a share token shown in lists: ``mpd_…x7k2`` (last 4 characters)."""
    return f"mpd_…{token[-4:]}"


# ================================================================================ plain text

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_LINE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MARKUP = re.compile(r"<[A-Za-z/!?]|&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});")


def _plain(value: str | None, *, multiline: bool) -> str | None:
    """Plain text only (clients see these strings; they are rendered as text, never HTML)."""
    if value is None:
        return None
    if (_CONTROL if multiline else _LINE_CONTROL).search(value):
        raise ValueError("use plain text: control characters are not allowed")
    if _MARKUP.search(value):
        raise ValueError("use plain text: HTML tags and entities are not allowed")
    return value


# ===================================================================================== spec


class DashboardSource(RequestModel):
    """The model a dashboard reads. ``model_version``: ``model_id`` (and ``version``; the
    latest ready version when omitted) is pinned at publish. ``endpoint``: ``alias`` is
    resolved to its current version and endpoint revision at publish."""

    kind: Literal["model_version", "endpoint"]
    model_id: Name | None = None
    version: int | None = Field(None, ge=1)
    alias: Name | None = None

    model_config = dto_examples(
        {"kind": "model_version", "model_id": "eu-geo-mmm", "version": 3},
        {"kind": "endpoint", "alias": "geo-mmm-prod"},
    )

    @model_validator(mode="after")
    def _one(self) -> DashboardSource:
        if self.kind == "model_version":
            if self.model_id is None or self.alias is not None:
                raise ValueError("a model_version source needs model_id (and no alias)")
        elif self.alias is None or self.model_id is not None or self.version is not None:
            raise ValueError("an endpoint source needs alias (and no model_id or version)")
        return self


KpiUnit = Literal["count", "currency", "ratio", "percent"]


class KpiDef(RequestModel):
    """The business result the dashboard reports (a model output variable).

    ``value_per_unit`` (currency per unit of a ``count`` KPI, e.g. the margin of one order)
    gives a breakeven return, so the channel zones can be shown for count KPIs too."""

    variable: Name
    label: str = Field(..., min_length=1, max_length=60)
    unit: KpiUnit
    currency: str | None = Field(None, pattern=r"^[A-Z]{3}$", description="ISO 4217 code")
    decimals: int = Field(0, ge=0, le=4)
    value_per_unit: float | None = Field(None, gt=0)

    model_config = dto_examples(
        {"variable": "orders", "label": "Orders", "unit": "count", "value_per_unit": 42.0},
        {"variable": "revenue", "label": "Revenue", "unit": "currency", "currency": "EUR"},
    )

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return _plain(v, multiline=False) or v

    @model_validator(mode="after")
    def _currency(self) -> KpiDef:
        if (self.unit == "currency") != (self.currency is not None):
            raise ValueError("give a currency exactly when the unit is currency")
        return self


class ChannelDef(RequestModel):
    """One media channel and the range its what-if slider covers (``1`` = the current plan)."""

    name: Name
    label: str = Field(..., min_length=1, max_length=60)
    min_mult: float = Field(0.5, ge=0)
    max_mult: float = Field(1.5, le=5)
    step: float = Field(0.05, gt=0)

    model_config = dto_examples({"name": "tv", "label": "TV", "min_mult": 0.5, "max_mult": 1.5})

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        return _plain(v, multiline=False) or v

    @model_validator(mode="after")
    def _range(self) -> ChannelDef:
        if not self.min_mult <= 1.0 <= self.max_mult or self.min_mult >= self.max_mult:
            raise ValueError("the slider range must contain 1 (the current plan): min < 1 < max")
        if self.step > self.max_mult - self.min_mult:
            raise ValueError("step must be smaller than the slider range")
        return self


class WhatIfInputs(RequestModel):
    """Scenario sliders (the response grid, free) and ``exact`` results (a score run)."""

    enabled: bool = True
    exact: bool = False


class ActualsInputs(RequestModel):
    """Uploads of new actuals. ``review``: an editor publishes each upload into the history;
    ``auto``: it goes straight in. ``allow_refit_requests`` lets the uploader ask for a refit
    with ``refit_workflow_id`` (a workflow of the same project whose input node takes the
    dashboard's dataset); an editor with runner rights approves it (§10.4)."""

    enabled: bool = False
    publish: Literal["auto", "review"] = "review"
    refit_workflow_id: Id | None = None
    allow_refit_requests: bool = False

    @model_validator(mode="after")
    def _refit(self) -> ActualsInputs:
        if self.allow_refit_requests and (not self.enabled or self.refit_workflow_id is None):
            raise ValueError("refit requests need actuals enabled and a refit_workflow_id")
        return self


class RefreshPolicy(RequestModel):
    """``workflow_id``: a successful run of this workflow (its ``model.score`` or
    ``model.forecast`` node) appends a refresh readout. ``follow_endpoint`` (endpoint
    sources only): a promotion publishes a new revision on the new version."""

    workflow_id: Id | None = None
    follow_endpoint: bool = False


class DashboardLimits(RequestModel):
    exact_runs_per_day: int = Field(20, ge=0, le=500)
    """Exact what-ifs, actuals scores and manual refreshes per dashboard per 24 h."""


TileKey = Literal[
    "expected", "vs_expected", "return_on_media", "in_range_spend", "accuracy", "index"
]
TabKey = Literal["this_period", "trends", "scenarios", "update", "history"]
_DEFAULT_TILES: Final[tuple[TileKey, ...]] = (
    "expected", "vs_expected", "return_on_media", "in_range_spend",
)  # fmt: skip
_DEFAULT_TABS: Final[tuple[TabKey, ...]] = ("this_period", "trends", "scenarios", "history")

_SPEC_EXAMPLE: Final[dict[str, Any]] = {
    "layout_version": 1,
    "title": "Weekly media results",
    "client_name": "Acme Retail",
    "summary": "How media spend drives online orders, updated every Monday.",
    "period_label": "week",
    "source": {"kind": "model_version", "model_id": "eu-geo-mmm", "version": 3},
    "kpi": {"variable": "orders", "label": "Orders", "unit": "count", "value_per_unit": 42.0},
    "channels": [
        {"name": "tv", "label": "TV"},
        {"name": "search", "label": "Paid search", "min_mult": 0.5, "max_mult": 2.0},
    ],
    "tiles": ["expected", "vs_expected", "return_on_media", "in_range_spend"],
    "tabs": ["this_period", "trends", "scenarios", "update", "history"],
    "whatif": {"enabled": True, "exact": True},
    "actuals": {
        "enabled": True,
        "publish": "review",
        "refit_workflow_id": "wf_01jaf3w1x2y3z4a5b6c7d8e9f0",
        "allow_refit_requests": True,
    },
    "refresh": {"workflow_id": "wf_01jaf3w1x2y3z4a5b6c7d8e9f0"},
    "limits": {"exact_runs_per_day": 20},
    "show_technical": False,
    "note": "Results use the model fitted on data to 18/10/2026.",
}


class DashboardSpec(RequestModel):
    """What a dashboard shows (the stored draft and each published revision).

    ``layout_version`` 1 is the fixed planner layout: up to :data:`MAX_TILES` KPI tiles, the
    channel scorecard and the tabs (``this_period`` always first). Text fields are plain
    text, rendered as text (never HTML or markdown). The serialised spec holds at most
    :data:`MAX_SPEC_BYTES`; :func:`dashboard_spec_hash` is its identity."""

    layout_version: Literal[1] = 1
    title: str = Field(..., min_length=1, max_length=120)
    client_name: str | None = Field(None, max_length=80)
    summary: str | None = Field(None, max_length=600)
    period_label: Literal["week", "month", "period"] = "week"
    source: DashboardSource
    kpi: KpiDef
    channels: list[ChannelDef] = Field(..., min_length=1, max_length=MAX_CHANNELS)
    tiles: list[TileKey] = Field(default_factory=lambda: list(_DEFAULT_TILES), max_length=MAX_TILES)
    tabs: list[TabKey] = Field(default_factory=lambda: list(_DEFAULT_TABS), min_length=1)
    whatif: WhatIfInputs = Field(default_factory=WhatIfInputs)
    actuals: ActualsInputs = Field(default_factory=ActualsInputs)
    refresh: RefreshPolicy = Field(default_factory=RefreshPolicy)
    limits: DashboardLimits = Field(default_factory=DashboardLimits)
    show_technical: bool = False
    note: str | None = Field(None, max_length=2000)

    model_config = dto_examples(_SPEC_EXAMPLE)

    @field_validator("title", "client_name")
    @classmethod
    def _line(cls, v: str | None) -> str | None:
        return _plain(v, multiline=False)

    @field_validator("summary", "note")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return _plain(v, multiline=True)

    @model_validator(mode="after")
    def _consistent(self) -> DashboardSpec:
        names = [c.name for c in self.channels]
        if len(set(names)) != len(names):
            raise ValueError("channel names must be unique")
        if len(set(self.tiles)) != len(self.tiles):
            raise ValueError("tiles must be unique")
        if len(set(self.tabs)) != len(self.tabs):
            raise ValueError("tabs must be unique")
        if self.tabs[0] != "this_period":
            raise ValueError("the first tab must be this_period")
        if self.refresh.follow_endpoint and self.source.kind != "endpoint":
            raise ValueError("follow_endpoint needs an endpoint source")
        size = len(self.model_dump_json().encode("utf-8"))
        if size > MAX_SPEC_BYTES:
            raise ValueError(f"the dashboard spec is {size} bytes (max {MAX_SPEC_BYTES})")
        return self


def dashboard_spec_hash(spec: DashboardSpec) -> str:
    """``sha256:<hex>`` of the spec's canonical JSON (stored with each revision and readout,
    so readouts made under different specs can be told apart)."""
    return spec_hash(spec)


def readout_cache_key(revision_id: str, value: BaseModel | Mapping[str, Any] | str) -> str:
    """The result-cache key of an exact readout: ``sha256(revision_id | canonical json)`` of
    the scenario, or of an actuals fingerprint (a string is used as it is)."""
    body = value if isinstance(value, str) else canonical_json(value)
    return "sha256:" + hashlib.sha256(f"{revision_id}|{body}".encode()).hexdigest()


# ================================================================================== readouts

DashboardStatus = Literal["draft", "published", "archived"]
ReadoutKind = Literal["baseline", "refresh", "scenario", "actuals"]
ReadoutTrigger = Literal["publish", "manual", "whatif", "actuals", "workflow", "endpoint"]
ReadoutVisibility = Literal["published", "private", "pending_review", "hidden"]
ReadoutStatus = Literal["queued", "running", "materialising", "ready", "failed"]
RefitRequestStatus = Literal["open", "approved", "rejected", "superseded", "failed"]
RequesterKind = Literal["member", "client", "share"]
TileUnit = Literal["count", "currency", "ratio", "percent", "index"]
Zone = Literal["headroom", "optimal", "over", "no_payback"]

_HEADER_EXAMPLE: Final[dict[str, Any]] = {
    "seq": 12,
    "kind": "refresh",
    "trigger": "workflow",
    "visibility": "published",
    "status": "ready",
    "title": "Week ending 18/10/2026",
    "period": ["2026-10-12", "2026-10-18"],
    "model_version_id": _MV,
    "model_ref": "model://growth/eu-geo-mmm@v3",
    "created_at": EXAMPLE_TS,
    "ready_at": EXAMPLE_TS,
}


class ReadoutHeader(ResponseModel):
    """A readout without its snapshot (lists, ``202`` answers while it is being built).

    ``refit_status`` is the state of the refit request made with an actuals upload (§10.4),
    shown to the uploader and the analysts."""

    seq: int = Field(..., ge=1)
    kind: ReadoutKind
    trigger: ReadoutTrigger
    visibility: ReadoutVisibility
    status: ReadoutStatus
    title: str = Field(..., max_length=200)
    period: tuple[date, date] | None = None
    model_version_id: str = Field(..., max_length=128)
    model_ref: str = Field(..., max_length=512)
    endpoint_revision: int | None = Field(None, ge=1)
    created_at: AwareDatetime
    ready_at: AwareDatetime | None = None
    refit_status: RefitRequestStatus | None = None

    model_config = dto_examples(
        _HEADER_EXAMPLE,
        {
            "seq": 13,
            "kind": "scenario",
            "trigger": "whatif",
            "visibility": "private",
            "status": "running",
            "title": "TV −20 %, search +30 %",
            "model_version_id": _MV,
            "model_ref": "model://growth/eu-geo-mmm@v3",
            "created_at": EXAMPLE_TS,
        },
    )


class KpiValue(ResponseModel):
    """One KPI tile: a value with its plain caption and the change against ``delta_basis``."""

    key: TileKey
    label: str = Field(..., max_length=80)
    value: float | None = None
    unit: TileUnit
    delta: float | None = None
    delta_basis: str | None = Field(None, max_length=80)
    delta_dir: Literal["up", "down", "flat", "alert"] = "flat"
    low: float | None = None
    high: float | None = None
    caption: str = Field("", max_length=200)


class ChannelReadout(ResponseModel):
    """One channel row of the scorecard (``roi`` per unit of spend, ``mroi`` at the margin).

    ``spend`` and ``contribution`` are per readout period (the response grid's window totals
    divided by its ``periods``), like the KPI tiles."""

    name: Name
    label: str = Field(..., max_length=60)
    spend: float | None = None
    contribution: float | None = None
    share: float | None = Field(None, description="Share of the media contribution, 0-1")
    roi: float | None = None
    roi_low: float | None = None
    roi_high: float | None = None
    mroi: float | None = None
    zone: Zone | None = None


class SeriesPoint(ResponseModel):
    t: str = Field(..., max_length=32, description="The period (ISO date)")
    mean: float | None = None
    low: float | None = None
    high: float | None = None
    actual: float | None = None


class Series(ResponseModel):
    name: str = Field(..., max_length=128)
    label: str = Field(..., max_length=80)
    points: list[SeriesPoint] = Field(default_factory=list, max_length=MAX_SERIES_POINTS)


class ActualsAccuracy(ResponseModel):
    """How close the model's predictions were to uploaded actuals (:func:`metrics_of`).

    ``mape`` and ``band_hit_rate`` are percentages (as ``compare.ts``); ``bias`` is the mean of
    predicted − actual in KPI units (above 0: the model expected more)."""

    mape: float | None = None
    bias: float | None = None
    band_hit_rate: float | None = None
    n: int = Field(0, ge=0)


class ReadoutSnapshot(ResponseModel):
    """What a readout shows, built server-side and capped at :data:`MAX_READOUT_BYTES`.

    ``lead`` is one plain sentence; ``notes_plain`` are at most :data:`MAX_NOTES` plain notes;
    ``technical`` is shown behind the "Show technical notes" toggle. Raw arrays, plots and
    summaries are never copied in: ``source_run_ids`` keep the provenance."""

    lead: str = Field(..., max_length=400)
    tiles: list[KpiValue] = Field(default_factory=list, max_length=6)
    channels: list[ChannelReadout] = Field(default_factory=list, max_length=MAX_CHANNELS)
    series: list[Series] = Field(default_factory=list, max_length=MAX_CHANNELS + 2)
    accuracy: ActualsAccuracy | None = None
    conditioning: ForecastConditioning | None = None
    scenario: dict[Name, float] | None = None
    source_run_ids: list[str] = Field(default_factory=list, max_length=16)
    input_fingerprint: str | None = Field(None, max_length=128)
    notes_plain: list[str] = Field(default_factory=list, max_length=MAX_NOTES)
    technical: dict[str, str | float] = Field(default_factory=dict, max_length=64)

    @field_validator("notes_plain")
    @classmethod
    def _notes(cls, v: list[str]) -> list[str]:
        for note in v:
            if len(note) > MAX_NOTE_CHARS:
                raise ValueError(f"a note holds at most {MAX_NOTE_CHARS} characters")
        return v

    @model_validator(mode="after")
    def _capped(self) -> ReadoutSnapshot:
        points = sum(len(s.points) for s in self.series)
        if points > MAX_SERIES_POINTS:
            raise ValueError(f"the series hold {points} points (max {MAX_SERIES_POINTS})")
        size = len(self.model_dump_json().encode("utf-8"))
        if size > MAX_READOUT_BYTES:
            raise ValueError(f"the readout snapshot is {size} bytes (max {MAX_READOUT_BYTES})")
        return self


_SNAPSHOT_EXAMPLE: Final[dict[str, Any]] = {
    "lead": "Media brought 4,210 orders this week. This is 3% more than the model expects.",
    "tiles": [
        {
            "key": "expected",
            "label": "Orders from media",
            "value": 4210.0,
            "unit": "count",
            "delta": 0.03,
            "delta_basis": "the model's expectation",
            "delta_dir": "up",
            "low": 3890.0,
            "high": 4530.0,
            "caption": "Likely range 3,890 to 4,530",
        },
        {
            "key": "return_on_media",
            "label": "Return on media",
            "value": 2.4,
            "unit": "ratio",
            "delta_dir": "flat",
            "caption": "Each 1 EUR of media brought 2.40 EUR",
        },
    ],
    "channels": [
        {
            "name": "tv",
            "label": "TV",
            "spend": 52000.0,
            "contribution": 2300.0,
            "share": 0.55,
            "roi": 1.86,
            "roi_low": 1.2,
            "roi_high": 2.5,
            "mroi": 0.9,
            "zone": "over",
        },
        {
            "name": "search",
            "label": "Paid search",
            "spend": 18000.0,
            "contribution": 1910.0,
            "share": 0.45,
            "roi": 4.46,
            "mroi": 3.1,
            "zone": "headroom",
        },
    ],
    "series": [
        {
            "name": "orders",
            "label": "Orders",
            "points": [
                {"t": "2026-10-11", "mean": 4100.0, "low": 3800.0, "high": 4400.0,
                 "actual": 4150.0},
                {"t": "2026-10-18", "mean": 4090.0, "low": 3790.0, "high": 4390.0,
                 "actual": 4210.0},
            ],
        }
    ],
    "source_run_ids": [_RUN],
    "notes_plain": ["The model was fitted on data to 18/10/2026."],
    "technical": {"model_ref": "model://growth/eu-geo-mmm@v3", "draws": 300},
}  # fmt: skip


class Readout(ReadoutHeader):
    """A readout with its snapshot (``None`` until ``ready``) or the problem that stopped it."""

    snapshot: ReadoutSnapshot | None = None
    problem: Problem | None = None

    model_config = dto_examples({**_HEADER_EXAMPLE, "snapshot": _SNAPSHOT_EXAMPLE})


class ReadoutList(ResponseModel):
    """Readouts, newest first; pass ``next_before_seq`` as ``before_seq`` for the next page."""

    items: list[ReadoutHeader] = Field(default_factory=list, max_length=50)
    next_before_seq: int | None = None

    model_config = dto_examples({"items": [_HEADER_EXAMPLE], "next_before_seq": 12})


# ================================================================================ dashboards


class EffectiveInputs(ResponseModel):
    """What the viewer may do: the published spec's inputs, narrowed by a share link's
    ``allowed_inputs``. ``refit_requests``: an upload may ask for a refit."""

    whatif: bool = False
    exact: bool = False
    actuals: bool = False
    actuals_mode: Literal["conditioned", "compared"] | None = None
    refit_requests: bool = False


class DashboardRevision(ResponseModel):
    """An immutable published revision: the spec and the model version it pins."""

    id: Id = Field(..., pattern=r"^dsr_[0-9a-z]{26}$")
    seq: int = Field(..., ge=1)
    spec: DashboardSpec
    spec_hash: Sha256
    model_version_id: str = Field(..., max_length=128)
    model_ref: str = Field(..., max_length=512)
    endpoint_alias: str | None = Field(None, max_length=128)
    endpoint_revision: int | None = Field(None, ge=1)
    created_by: str = Field(..., max_length=128)
    created_at: AwareDatetime

    model_config = dto_examples(
        {
            "id": _DSR,
            "seq": 2,
            "spec": _SPEC_EXAMPLE,
            "spec_hash": "sha256:" + "5a" * 32,
            "model_version_id": _MV,
            "model_ref": "model://growth/eu-geo-mmm@v3",
            "created_by": _USR,
            "created_at": EXAMPLE_TS,
        }
    )


class Dashboard(ResponseModel):
    """The analyst's view of a dashboard (``GET /v1/dashboards/{dashboard_id}`` for viewers
    and up; a client gets :class:`ClientDashboard`). ``run_as`` is the publisher whose
    compute the live inputs use; ``live_inputs`` is ``paused`` when that user lost the runner
    role. ``problem`` is a short machine word for the analyst (``new_version_available``)."""

    id: Id = Field(..., pattern=r"^dsh_[0-9a-z]{26}$")
    name: Name
    status: DashboardStatus
    draft_spec: DashboardSpec
    draft_etag: str = Field(..., max_length=128)
    published_revision: DashboardRevision | None = None
    run_as: str | None = Field(None, max_length=128)
    live_inputs: Literal["ok", "paused"] = "ok"
    problem: str | None = Field(None, max_length=200)
    created_by: str = Field(..., max_length=128)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    latest_readout: ReadoutHeader | None = None

    model_config = dto_examples(
        {
            "id": _DSH,
            "name": "acme-weekly",
            "status": "published",
            "draft_spec": _SPEC_EXAMPLE,
            "draft_etag": '"3"',
            "published_revision": DashboardRevision.model_json_schema()["examples"][0],
            "run_as": _USR,
            "live_inputs": "ok",
            "created_by": _USR,
            "created_at": EXAMPLE_TS,
            "updated_at": EXAMPLE_TS,
            "latest_readout": _HEADER_EXAMPLE,
        }
    )


class DashboardSummary(ResponseModel):
    """One row of ``GET /v1/dashboards``: no spec, no people (clients list with it too)."""

    id: Id = Field(..., pattern=r"^dsh_[0-9a-z]{26}$")
    name: Name
    title: str = Field(..., max_length=120)
    client_name: str | None = Field(None, max_length=80)
    status: DashboardStatus
    period_label: Literal["week", "month", "period"] = "week"
    live_inputs: Literal["ok", "paused"] = "ok"
    latest_readout: ReadoutHeader | None = None
    updated_at: AwareDatetime

    model_config = dto_examples(
        {
            "id": _DSH,
            "name": "acme-weekly",
            "title": "Weekly media results",
            "client_name": "Acme Retail",
            "status": "published",
            "period_label": "week",
            "live_inputs": "ok",
            "latest_readout": _HEADER_EXAMPLE,
            "updated_at": EXAMPLE_TS,
        }
    )


_CLIENT_EXAMPLE: Final[dict[str, Any]] = {
    "id": _DSH,
    "title": "Weekly media results",
    "client_name": "Acme Retail",
    "summary": "How media spend drives online orders, updated every Monday.",
    "period_label": "week",
    "kpi": _SPEC_EXAMPLE["kpi"],
    "channels": _SPEC_EXAMPLE["channels"],
    "tiles": _SPEC_EXAMPLE["tiles"],
    "tabs": _SPEC_EXAMPLE["tabs"],
    "inputs": {"whatif": True, "exact": True, "actuals": True, "actuals_mode": "compared",
               "refit_requests": True},
    "live_inputs": "ok",
    "latest_readout": {**_HEADER_EXAMPLE, "snapshot": _SNAPSHOT_EXAMPLE},
    "ends_at": "2026-11-14T09:30:00Z",
}  # fmt: skip


class ClientDashboard(ResponseModel):
    """What a client user or a share link sees: the published revision only, with no ids of
    people and no model internals. ``ends_at`` is the share link's expiry (share views)."""

    id: Id = Field(..., pattern=r"^dsh_[0-9a-z]{26}$")
    title: str = Field(..., max_length=120)
    client_name: str | None = Field(None, max_length=80)
    summary: str | None = Field(None, max_length=600)
    note: str | None = Field(None, max_length=2000)
    period_label: Literal["week", "month", "period"] = "week"
    kpi: KpiDef
    channels: list[ChannelDef] = Field(default_factory=list, max_length=MAX_CHANNELS)
    tiles: list[TileKey] = Field(default_factory=list, max_length=MAX_TILES)
    tabs: list[TabKey] = Field(default_factory=list)
    inputs: EffectiveInputs = Field(default_factory=EffectiveInputs)
    live_inputs: Literal["ok", "paused"] = "ok"
    show_technical: bool = False
    latest_readout: Readout | None = None
    ends_at: AwareDatetime | None = None

    model_config = dto_examples(_CLIENT_EXAMPLE)


class DashboardCreate(RequestModel):
    """``POST /v1/dashboards``: a new draft (unique ``name`` per project)."""

    name: Name
    spec: DashboardSpec

    model_config = dto_examples({"name": "acme-weekly", "spec": _SPEC_EXAMPLE})


class DashboardDraftUpdate(RequestModel):
    """``PUT …/draft`` with ``If-Match: <draft_etag>`` (a stale etag → ``DASHBOARD_SPEC_STALE``)."""

    spec: DashboardSpec

    model_config = dto_examples({"spec": _SPEC_EXAMPLE})


class DashboardValidation(ResponseModel):
    """``POST …:validate``: whether the draft can be published, and what to fix."""

    ok: bool
    issues: list[Issue] = Field(default_factory=list, max_length=200)

    model_config = dto_examples(
        {"ok": True, "issues": []},
        {
            "ok": False,
            "issues": [
                {
                    "code": "BINDING_INVALID",
                    "severity": "error",
                    "pointer": "/channels/1/name",
                    "message": "channel 'radio' is not in the model's response grid",
                }
            ],
        },
    )


_GRID_EXAMPLE: Final[dict[str, Any]] = {
    "format": "mapi.response-grid/v1",
    "kpi": "orders",
    "kpi_units": "orders",
    "spend_units": "EUR",
    "periods": 52,
    "channels": ["tv", "search"],
    "multipliers": [0.5, 1.0, 1.5],
    "draws": 2,
    "contribution": [[[700.0, 1000.0, 1150.0], [300.0, 500.0, 620.0]],
                     [[650.0, 950.0, 1100.0], [320.0, 520.0, 650.0]]],
    "spend": [[26000.0, 52000.0, 78000.0], [9000.0, 18000.0, 27000.0]],
    "current_spend": [52000.0, 18000.0],
    "baseline": [2600.0, 2700.0],
}  # fmt: skip


class DashboardGrid(ResponseGridView):
    """``GET …/grid``: the pinned version's response grid (``ui/grids/response_curves.json``),
    clipped to the dashboard's channels and slider ranges. The browser evaluates the
    sliders with it (``whatif.ts``); no run is made."""

    model_config = ConfigDict(
        extra="allow", frozen=True, json_schema_extra={"examples": [_GRID_EXAMPLE]}
    )


def _check_multipliers(v: dict[str, float]) -> dict[str, float]:
    for ch, m in v.items():
        if not math.isfinite(m) or m < 0:
            raise ValueError(f"multiplier of {ch!r} must be a finite number >= 0")
    return v


class WhatIfRequest(RequestModel):
    """Spend multipliers per channel (``1`` = the current plan; missing channels stay at 1)."""

    multipliers: dict[Name, float] = Field(default_factory=dict, max_length=MAX_CHANNELS)

    model_config = dto_examples({"multipliers": {"tv": 0.8, "search": 1.3}})

    _finite = field_validator("multipliers")(_check_multipliers)


class WhatIfResult(ResponseModel):
    """A scenario evaluated on the grid: tiles and channel rows as a readout shows them,
    ``delta_kpi`` against the current plan and one plain ``lead`` sentence. Every number is
    per readout period (the grid's window totals divided by its ``periods``)."""

    tiles: list[KpiValue] = Field(default_factory=list, max_length=6)
    channels: list[ChannelReadout] = Field(default_factory=list, max_length=MAX_CHANNELS)
    delta_kpi: float
    lead: str = Field(..., max_length=400)

    model_config = dto_examples(
        {
            "tiles": [_SNAPSHOT_EXAMPLE["tiles"][0]],
            "channels": _SNAPSHOT_EXAMPLE["channels"],
            "delta_kpi": 85.0,
            "lead": "This plan brings about 85 more orders a week than the current plan.",
        }
    )


class ScenarioSave(RequestModel):
    """``POST …/scenarios``: keep a scenario as a private readout (grid result, ready at
    once), or with ``exact`` (when the dashboard allows it) as the result of a score run."""

    multipliers: dict[Name, float] = Field(default_factory=dict, max_length=MAX_CHANNELS)
    exact: bool = False
    label: str | None = Field(None, max_length=80)

    model_config = dto_examples(
        {"multipliers": {"tv": 0.8, "search": 1.3}, "label": "Shift TV to search"},
        {"multipliers": {"tv": 0.8}, "exact": True},
    )

    _finite = field_validator("multipliers")(_check_multipliers)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return _plain(v, multiline=False)


class ActualsTemplate(ResponseModel):
    """``GET …/actuals/template``: the table an upload fills in (the pinned version's inline
    input table plus the KPI column). ``mode`` says what happens to the numbers:
    ``conditioned`` (the forecast is updated with them) or ``compared`` (the model's
    prediction is compared with them; the model does not change)."""

    mode: Literal["conditioned", "compared"]
    input_table: InputTable
    kpi_column: Name
    example_csv: str = Field(..., max_length=65_536)
    max_rows: int = Field(MAX_ACTUALS_ROWS, ge=1, le=MAX_ACTUALS_ROWS)

    model_config = dto_examples(
        {
            "mode": "compared",
            "input_table": {
                "horizon_dim": "week",
                "columns": [
                    {"name": "week", "role": "coord", "freq": "W-SUN"},
                    {"name": "orders", "role": "target", "dims": ["week"]},
                    {"name": "tv", "role": "driver", "dims": ["week"]},
                ],
                "rows": [["2026-10-25", None, None]],
            },
            "kpi_column": "orders",
            "example_csv": "week,orders,tv\n2026-10-25,,\n",
            "max_rows": MAX_ACTUALS_ROWS,
        }
    )


class ActualsSubmit(RequestModel):
    """``POST …/actuals``: new actuals as inline XrJSON (parsed in the browser from CSV or
    a paste). ``request_refit`` asks the analyst to refit with them (only when the dashboard
    allows refit requests; it never starts compute by itself)."""

    inputs: XrJsonDataset
    label: str | None = Field(None, max_length=80)
    request_refit: bool = False

    model_config = dto_examples(
        {
            "inputs": {
                "coords": {"week": {"dims": ["week"], "data": ["2026-10-25"]}},
                "data_vars": {
                    "orders": {"dims": ["week"], "data": [4302.0]},
                    "tv": {"dims": ["week"], "data": [51000.0]},
                },
            },
            "label": "Week ending 25/10/2026",
            "request_refit": False,
        }
    )

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return _plain(v, multiline=False)


# ============================================================================ grants, shares


class DashboardGrant(ResponseModel):
    """A client user who may see the dashboard (they hold a membership of the project)."""

    user_id: str = Field(..., max_length=128)
    email: str | None = Field(None, max_length=320)
    display_name: str | None = Field(None, max_length=200)
    granted_by: str = Field(..., max_length=128)
    created_at: AwareDatetime

    model_config = dto_examples(
        {
            "user_id": _CLIENT_USR,
            "email": "cleo@acme-retail.example",
            "display_name": "Cleo Client",
            "granted_by": _USR,
            "created_at": EXAMPLE_TS,
        }
    )


class DashboardGrantPut(RequestModel):
    user_id: str = Field(..., max_length=128)


ShareInput = Literal["whatif", "actuals"]


class DashboardShareCreate(RequestModel):
    """``POST …/shares``: a share link (read-only unless ``allowed_inputs`` names inputs,
    which needs the runner role: the link spends the publisher's compute). Passcodes are
    8-128 characters and cannot be changed: revoke the link and make a new one."""

    label: str | None = Field(None, max_length=80)
    expires_in_days: int = Field(30, ge=1, le=MAX_SHARE_DAYS)
    passcode: SecretStr | None = Field(
        None, min_length=MIN_PASSCODE_LEN, max_length=MAX_PASSCODE_LEN
    )
    allowed_inputs: set[ShareInput] = Field(default_factory=set)

    model_config = dto_examples(
        {"label": "Board pack", "expires_in_days": 30},
        {"label": "Acme marketing team", "expires_in_days": 90, "allowed_inputs": ["whatif"]},
    )  # passcodes are secrets: they never round-trip through a dump, so no example has one

    @field_validator("label")
    @classmethod
    def _label(cls, v: str | None) -> str | None:
        return _plain(v, multiline=False)


_SHARE_EXAMPLE: Final[dict[str, Any]] = {
    "id": _DSS,
    "label": "Board pack",
    "display": "mpd_…wxyz",
    "status": "active",
    "has_passcode": False,
    "allowed_inputs": [],
    "views": 4,
    "last_viewed_at": EXAMPLE_TS,
    "expires_at": "2026-11-14T09:30:00Z",
    "created_by": _USR,
    "created_at": EXAMPLE_TS,
}


class DashboardShare(ResponseModel):
    """A share link as listed (never its token; ``display`` is ``mpd_…<last 4>``)."""

    id: Id = Field(..., pattern=r"^dss_[0-9a-z]{26}$")
    label: str | None = None
    display: str = Field(..., max_length=16)
    status: Literal["active", "expired", "revoked"]
    has_passcode: bool = False
    allowed_inputs: list[ShareInput] = Field(default_factory=list, max_length=2)
    views: int = Field(0, ge=0)
    last_viewed_at: AwareDatetime | None = None
    locked_until: AwareDatetime | None = None
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    created_by: str = Field(..., max_length=128)
    created_at: AwareDatetime

    model_config = dto_examples(_SHARE_EXAMPLE)


class DashboardShareCreated(DashboardShare):
    """``201`` of ``POST …/shares``: the only time the token is returned. ``url_fragment`` is
    the path and fragment to append to the app origin (``/s#mpd_…``): the fragment never
    reaches server logs or a ``Referer``."""

    token: str = Field(..., pattern=DASHBOARD_SHARE_TOKEN_RE.pattern)
    url_fragment: str = Field(..., max_length=64)

    model_config = dto_examples(
        {**_SHARE_EXAMPLE, "token": _TOKEN_EXAMPLE, "url_fragment": "/s#" + _TOKEN_EXAMPLE}
    )

    def __repr__(self) -> str:
        return f"DashboardShareCreated(id={self.id!r}, token=<redacted>)"

    __str__ = __repr__


class ShareSessionCreate(RequestModel):
    """``POST /v1/share-sessions``: exchange a share token (and its passcode) for a session.
    The token is sent once, in this body; the page strips it from the URL first."""

    token: str = Field(..., pattern=DASHBOARD_SHARE_TOKEN_RE.pattern)
    passcode: SecretStr | None = Field(None, max_length=MAX_PASSCODE_LEN)

    model_config = dto_examples({"token": _TOKEN_EXAMPLE})

    def __repr__(self) -> str:
        return "ShareSessionCreate(token=<redacted>)"

    __str__ = __repr__


class ShareSession(ResponseModel):
    """A share session (held in memory by the page, sent as :data:`SHARE_SESSION_HEADER`)
    and the dashboard it opens. Refresh it before ``expires_at`` while the link is valid."""

    session: str = Field(..., pattern=SHARE_SESSION_RE.pattern)
    expires_at: AwareDatetime
    dashboard: ClientDashboard

    model_config = dto_examples(
        {"session": _SESSION_EXAMPLE, "expires_at": EXAMPLE_TS, "dashboard": _CLIENT_EXAMPLE}
    )

    def __repr__(self) -> str:
        return f"ShareSession(dashboard={self.dashboard.id!r}, session=<redacted>)"

    __str__ = __repr__


# ============================================================================ refit requests


class DashboardRefitRequest(ResponseModel):
    """A request, made with an actuals upload, that the analyst refits the model with those
    numbers (§10.4). At most one ``open`` request per dashboard (a newer one supersedes it).
    Approval publishes the readout, ingests the numbers as a new version of the dashboard's
    dataset and starts ``refit_workflow_id`` as the approver; nothing runs before that."""

    id: Id = Field(..., pattern=r"^drr_[0-9a-z]{26}$")
    dashboard_id: Id = Field(..., pattern=r"^dsh_[0-9a-z]{26}$")
    readout_seq: int = Field(..., ge=1)
    status: RefitRequestStatus
    requested_by_kind: RequesterKind
    requested_by: str = Field(..., max_length=128, description="A user id or a share id")
    created_at: AwareDatetime
    decided_by: str | None = Field(None, max_length=128)
    decided_at: AwareDatetime | None = None
    note: str | None = Field(None, max_length=500)
    workflow_run_id: str | None = Field(None, max_length=128)
    dataset_version_id: str | None = Field(None, max_length=256)
    problem: Problem | None = None

    model_config = dto_examples(
        {
            "id": _DRR,
            "dashboard_id": _DSH,
            "readout_seq": 14,
            "status": "open",
            "requested_by_kind": "client",
            "requested_by": _CLIENT_USR,
            "created_at": EXAMPLE_TS,
        },
        {
            "id": _DRR,
            "dashboard_id": _DSH,
            "readout_seq": 14,
            "status": "approved",
            "requested_by_kind": "share",
            "requested_by": _DSS,
            "created_at": EXAMPLE_TS,
            "decided_by": _USR,
            "decided_at": EXAMPLE_TS,
            "workflow_run_id": "wfr_01jaf3x1y2z3a4b5c6d7e8f9g0",
            "dataset_version_id": "dsv_01jaf3y1z2a3b4c5d6e7f8g9h0",
        },
    )


class RefitRequestDecision(RequestModel):
    """``POST …/refit-requests/{request_id}:approve`` or ``:reject``: an optional note."""

    note: str | None = Field(None, max_length=500)

    model_config = dto_examples({"note": "Refit with the October numbers."}, {})

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return _plain(v, multiline=True)


# ==================================================================================== metrics


def _finite(v: float | None) -> float | None:
    return v if v is not None and math.isfinite(v) else None


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def metrics_of(
    predicted: Sequence[float | None],
    actual: Sequence[float | None],
    low: Sequence[float | None],
    high: Sequence[float | None],
) -> ActualsAccuracy:
    """Accuracy of predictions against actuals, as ``features/score/compare.ts`` ``metricsOf``.

    Periods without both an actual and a prediction are left out; ``mape`` leaves out
    actuals of 0; ``band_hit_rate`` counts the periods whose actual is inside
    ``[low, high]`` among those that have both bounds. Percentages, as the web."""
    if not len(predicted) == len(actual) == len(low) == len(high):
        raise ValueError("predicted, actual, low and high must have the same length")
    pairs: list[tuple[float, float, float | None, float | None]] = []
    for p, a, lo, hi in zip(predicted, actual, low, high, strict=True):
        fp, fa = _finite(p), _finite(a)
        if fp is not None and fa is not None:
            pairs.append((fp, fa, _finite(lo), _finite(hi)))
    n = len(pairs)
    if not n:
        return ActualsAccuracy(n=0)
    pct = [abs((p - a) / a) * 100 for p, a, _, _ in pairs if a != 0]
    banded = [(a, lo, hi) for _, a, lo, hi in pairs if lo is not None and hi is not None]
    hits = sum(1 for a, lo, hi in banded if lo <= a <= hi)
    return ActualsAccuracy(
        n=n,
        mape=_mean(pct),
        bias=_mean([p - a for p, a, _, _ in pairs]),
        band_hit_rate=hits / len(banded) * 100 if banded else None,
    )


def zone_of(
    roi: float | None, mroi: float | None, *, breakeven: float, band: float = 0.2
) -> Zone | None:
    """Where a channel's spend sits on its response curve.

    ``no_payback`` when its return is below ``breakeven``; ``over`` when the next unit of
    spend returns less than breakeven; ``headroom`` when it returns more than
    ``breakeven * (1 + band)``; else ``optimal``. ``None`` when the numbers are missing."""
    if roi is not None and math.isfinite(roi) and roi < breakeven:
        return "no_payback"
    if roi is None or mroi is None or not math.isfinite(mroi):
        return None
    if mroi < breakeven:
        return "over"
    if mroi > breakeven * (1 + band):
        return "headroom"
    return "optimal"


def breakeven_of(kpi: KpiDef) -> float | None:
    """The return at which media pays back: 1 for a currency KPI, ``1 / value_per_unit`` for a
    count KPI with a value per unit, else unknown (no zones)."""
    if kpi.unit == "currency":
        return 1.0
    if kpi.unit == "count" and kpi.value_per_unit:
        return 1.0 / kpi.value_per_unit
    return None


# =================================================================================== glossary

GlossaryKey = Literal[
    "expected",
    "vs_expected",
    "return_on_media",
    "marginal_return",
    "contribution",
    "share",
    "range",
    "zones",
    "headroom",
    "optimal",
    "over",
    "no_payback",
    "in_range_spend",
    "accuracy",
    "bias",
    "band_hit_rate",
    "index",
    "scenario",
    "compared",
    "conditioned",
    "refit",
]

GLOSSARY: Final[dict[GlossaryKey, str]] = {
    "expected": "The result that the model calculates from your media spend and the other inputs.",
    "vs_expected": "The difference between the actual result and the result that the model "
    "expected. A positive number means that the actual result was better.",
    "return_on_media": "The result that each 1 unit of media spend brought. For money "
    "results, a value above 1 means that the media paid back.",
    "marginal_return": "The result that the next 1 unit of spend on a channel brings. It "
    "tells you if more spend on the channel is still useful.",
    "contribution": "The part of the result that a channel caused, as the model calculates it.",
    "share": "The part of all media contribution that comes from one channel.",
    "range": "The likely range of a value. The model is 90% sure that the true value is in "
    "this range.",
    "zones": "Where the spend on a channel sits on its response curve: more room, within "
    "range, past its best or not paying back.",
    "headroom": "More room: the next unit of spend still brings a good return. You can spend "
    "more on this channel.",
    "optimal": "Within range: the spend on this channel is near its best level.",
    "over": "Past its best: the next unit of spend brings back less than it costs.",
    "no_payback": "Not paying back: this channel brings back less than its spend.",
    "in_range_spend": "The part of media spend that is in channels within range or with more room.",
    "accuracy": "How near the model's predictions were to the actual numbers that you sent.",
    "bias": "The average difference between the prediction and the actual number. A positive "
    "number means that the model expected more.",
    "band_hit_rate": "The percentage of periods where the actual number was in the likely range.",
    "index": "The result compared with the model's expectation, where 100 is the expectation.",
    "scenario": "A plan with different spend per channel. The model calculates its result. "
    "The model does not change.",
    "compared": "We compare your numbers with what the model expected. The model does not change.",
    "conditioned": "The model uses your numbers to update its forecast. The model parameters "
    "do not change.",
    "refit": "The analyst fits the model again with new data. Only the analyst can start a refit.",
}
"""Plain (ASD-STE100) definitions behind the "What does this mean?" links and the
``explain_metric`` tool. Keys equal :data:`GlossaryKey`."""

if set(GLOSSARY) != set(get_args(GlossaryKey)):  # pragma: no cover - import-time contract check
    raise RuntimeError("GLOSSARY and GlossaryKey disagree")
