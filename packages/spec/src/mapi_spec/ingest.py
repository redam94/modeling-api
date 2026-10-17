"""Ingest presets and file contracts (plan §3.2, §11 Guided task 2, Q8; M3).

An upload becomes a labelled dataset in a sandboxed ``ingest`` run. Besides an explicit
column mapping (:class:`mapi_spec.api.IngestMapping`), M3 adds **presets** for the formats
marketing teams already have:

=================  ===========================================================================
preset             file contract
=================  ===========================================================================
``none``           the explicit ``dims``/``vars``/``wide`` column mapping
``mff``            the MFF long table (:data:`MFF_COLUMNS`): one row per period, key columns
                   and variable, the value in ``VariableValue`` (:class:`MffOptions`)
``meridian``       Google Meridian's ``CoordToColumns`` wide table (:class:`MeridianColumns`)
=================  ===========================================================================

Wide tables (one column per channel) become one variable along a new dim through
:class:`WideVarMapping` - the shape Meridian and most spreadsheets use.

This module only *describes* files; every byte of an upload is parsed inside the sandbox.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from mapi_spec._dto import RequestModel, dto_examples
from mapi_spec._types import Name, StrictModel
from mapi_spec.schema import DimSpec, Role

__all__ = [
    "COMBINE_SEPARATOR",
    "MFF_COLUMNS",
    "MFF_KEY_COLUMNS",
    "MFF_PANEL_KEY_COLUMNS",
    "MFF_TOTAL_LABELS",
    "ColumnRef",
    "DimMapping",
    "IngestFormat",
    "IngestMapping",
    "IngestPreset",
    "MeridianColumns",
    "MffOptions",
    "VarMapping",
    "WideVarMapping",
]

IngestFormat = Literal["csv", "parquet", "mff", "zarr_zip", "netcdf"]
"""Upload formats. ``mff`` is a CSV laid out as an MFF long table (implies ``preset=mff``);
``zarr_zip`` (a single-object zarr v3 zip) and ``netcdf`` are self-describing."""

TABULAR_FORMATS: Final = frozenset({"csv", "parquet", "mff"})

ColumnRef = Annotated[str, Field(min_length=1, max_length=256)]
"""A column name of a tabular upload (any text; it is never interpreted as code)."""


class IngestPreset(StrEnum):
    NONE = "none"
    MFF = "mff"
    MERIDIAN = "meridian"


# ============================================================================================ MFF

MFF_COLUMNS: Final[tuple[str, ...]] = (
    "Period",
    "Geography",
    "Product",
    "Outlet",
    "Campaign",
    "Creative",
    "VariableValue",
    "VariableName",
)
"""The MFF long-format columns, in the order of the MFF convention (and mmm-framework).
Every row holds one value: ``VariableValue`` of variable ``VariableName`` for one
``Period`` and one combination of the key columns."""

MFF_KEY_COLUMNS: Final[tuple[str, ...]] = ("Geography", "Product", "Outlet", "Campaign", "Creative")
"""The MFF key columns besides ``Period`` (each becomes a dim when it has >1 value)."""


Allocation = Literal["equal", "kpi"]


def _default_allocation() -> dict[str, Allocation]:
    """mmm-framework's defaults: geographies equally, products by the KPI (sales)."""
    return {"Geography": "equal", "Product": "kpi"}


class MffOptions(StrictModel):
    """The MFF ("model feature format") long-table preset.

    One row per ``(period, geography, product, campaign, outlet, creative, variable)`` with
    the value in ``value_column``. ``Period`` becomes the time dim; ``VariableName`` values
    become data variables. The rest follows mmm-framework's MFF semantics (its
    ``MFFLoader``), so both read a file the same way:

    * ``Geography`` and ``Product`` become dims when they hold more than one level. A blank
      label (or ``National``, ``Total``, ``All``) is the coarser level: a variable given
      only there (national TV) is spread across the levels - equal weights, or the KPI's
      share of each level (``allocation``; mmm-framework's defaults: geographies equally,
      products by sales). A variable may not mix the two levels.
    * ``Campaign``, ``Outlet`` and ``Creative`` are breakdowns: rows that differ only there
      are summed, unless the column is listed in ``split`` (then it becomes a dim).

    ``combine`` joins several key columns into one dim (``{"geo": ["Geography", "Product"]}``
    gives labels such as ``US|Watches``) - a model with one panel dim can then fit a
    geography x product panel. A combined dim is created even when it has a single label,
    so ``{"geo": ["Geography"]}`` keeps a national table's geo dim.
    """

    period_column: str = "Period"
    variable_column: str = "VariableName"
    value_column: str = "VariableValue"
    key_columns: dict[str, str] = Field(
        default_factory=lambda: {
            "Geography": "geo",
            "Product": "product",
            "Campaign": "campaign",
            "Outlet": "outlet",
            "Creative": "creative",
        },
        description="Key column -> dim name",
    )
    time_dim: str = "date"
    freq: str | None = Field(None, description="Pandas offset alias; inferred when omitted")
    roles: dict[str, str] = Field(default_factory=dict, description="Variable -> role")
    units: dict[str, str] = Field(default_factory=dict, description="Variable -> units")
    missing: Literal["forbid", "allow", "mask"] = "allow"
    combine: dict[Name, list[str]] = Field(
        default_factory=dict,
        max_length=4,
        description="M3: dim -> key columns whose labels are joined ('|') into that one dim",
        exclude_if=lambda v: not v,
    )
    split: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="v0.5: breakdown key columns (Campaign, Outlet, Creative) kept as dims "
        "instead of summed",
        exclude_if=lambda v: not v,
    )
    allocation: dict[str, Allocation] = Field(
        default_factory=_default_allocation,
        description="v0.5: how a variable given only at the coarser level (a blank or "
        "'National' label) is spread across the levels of a key column: equally, or by the "
        "KPI's share of each level (the variable with role 'target', or 'kpi')",
        exclude_if=lambda v: v == _default_allocation(),
    )
    kpi: str | None = Field(
        None,
        max_length=256,
        description="v0.5: the KPI variable of 'kpi' allocation",
        exclude_if=lambda v: v is None,
    )

    @field_validator("combine")
    @classmethod
    def _combine(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        seen: set[str] = set()
        for dim, cols in v.items():
            if not cols or len(cols) > len(MFF_KEY_COLUMNS):
                raise ValueError(f"combine.{dim}: name 1-{len(MFF_KEY_COLUMNS)} key columns")
            if seen & set(cols) or len(set(cols)) != len(cols):
                raise ValueError(f"combine.{dim}: a key column is combined twice")
            seen |= set(cols)
        return v


MFF_PANEL_KEY_COLUMNS: Final[tuple[str, ...]] = ("Geography", "Product")
"""Key columns that are panel levels (spread across when a variable is given in total);
the other key columns are breakdowns that are summed unless :attr:`MffOptions.split`."""
MFF_TOTAL_LABELS: Final[frozenset[str]] = frozenset({"", "national", "total", "all", "*"})
"""Key labels (case-insensitive) that mean "the coarser level", as a blank cell does."""

COMBINE_SEPARATOR: Final = "|"
"""Separator of the labels of key columns joined by :attr:`MffOptions.combine`."""


# ======================================================================================= wide


class WideVarMapping(RequestModel):
    """One variable read from several columns, one per label of ``dim`` (a wide table).

    ``{"dim": "channel", "columns": {"tv": "tv_spend", "search": "search_spend"}}`` gives a
    variable along ``channel`` (plus the table's other dims) with coords ``tv`` and
    ``search``. Several wide variables may share a dim; their labels must then agree.
    """

    dim: Name
    columns: dict[str, ColumnRef] = Field(..., min_length=1, max_length=256)
    dim_spec: DimSpec = Field(default_factory=lambda: DimSpec(kind="categorical"))
    role: Role = Role.COVARIATE
    units: str | None = Field(None, max_length=64)
    missing: Literal["forbid", "allow", "mask"] = "forbid"
    description: str | None = Field(None, max_length=2000)

    model_config = dto_examples(
        {
            "dim": "channel",
            "columns": {"tv": "tv_spend", "search": "search_spend", "social": "social_spend"},
            "role": "spend",
            "units": "EUR",
        }
    )

    @field_validator("columns")
    @classmethod
    def _distinct(cls, v: dict[str, str]) -> dict[str, str]:
        if len(set(v.values())) != len(v):
            raise ValueError("each label needs its own column")
        for label in v:
            if not label or len(label) > 128:
                raise ValueError("labels are 1-128 characters")
        return v


# =================================================================================== Meridian


class MeridianColumns(RequestModel):
    """Google Meridian's ``CoordToColumns`` contract for a wide CSV/Parquet table.

    ``media``/``media_spend`` (and ``reach``/``frequency``/``rf_spend``) are parallel lists:
    the *i*-th column of each belongs to the *i*-th entry of ``media_channels``
    (``rf_channels``). ``geo`` is omitted for a national model; ``kpi_type`` says whether
    the KPI is revenue (ROI in currency) or a count (``revenue_per_kpi`` converts it).
    """

    time: ColumnRef = "time"
    geo: ColumnRef | None = "geo"
    kpi: ColumnRef = "kpi"
    kpi_type: Literal["revenue", "non_revenue"] = "revenue"
    revenue_per_kpi: ColumnRef | None = None
    population: ColumnRef | None = None
    controls: list[ColumnRef] = Field(default_factory=list, max_length=128)
    media_channels: list[str] = Field(default_factory=list, max_length=128)
    media: list[ColumnRef] = Field(default_factory=list, max_length=128)
    media_spend: list[ColumnRef] = Field(default_factory=list, max_length=128)
    rf_channels: list[str] = Field(default_factory=list, max_length=128)
    reach: list[ColumnRef] = Field(default_factory=list, max_length=128)
    frequency: list[ColumnRef] = Field(default_factory=list, max_length=128)
    rf_spend: list[ColumnRef] = Field(default_factory=list, max_length=128)
    organic_media: list[ColumnRef] = Field(default_factory=list, max_length=128)
    non_media_treatments: list[ColumnRef] = Field(default_factory=list, max_length=128)
    freq: str | None = Field("W", max_length=16, description="Time frequency (pandas alias)")

    model_config = dto_examples(
        {
            "time": "time",
            "geo": "geo",
            "kpi": "conversions",
            "kpi_type": "non_revenue",
            "revenue_per_kpi": "revenue_per_conversion",
            "population": "population",
            "controls": ["competitor_sales", "sentiment"],
            "media_channels": ["tv", "search"],
            "media": ["tv_impressions", "search_impressions"],
            "media_spend": ["tv_spend", "search_spend"],
        }
    )

    @model_validator(mode="after")
    def _parallel(self) -> Self:
        n = len(self.media_channels)
        for name in ("media", "media_spend"):
            cols = getattr(self, name)
            if cols and len(cols) != n:
                raise ValueError(f"{name} needs one column per media channel ({n})")
        k = len(self.rf_channels)
        for name in ("reach", "frequency", "rf_spend"):
            cols = getattr(self, name)
            if cols and len(cols) != k:
                raise ValueError(f"{name} needs one column per reach/frequency channel ({k})")
        if (self.reach or self.frequency) and not (self.reach and self.frequency):
            raise ValueError("reach and frequency come together")
        if not (self.media or self.reach or self.organic_media or self.non_media_treatments):
            raise ValueError("a Meridian table needs at least one media or treatment column")
        if len(set(self.media_channels + self.rf_channels)) != n + k:
            raise ValueError("channel names must be unique")
        return self

    def wide_vars(self) -> dict[str, WideVarMapping]:
        """The channel-indexed variables this contract defines (``media``, ``spend``,
        ``reach``, ``frequency``, ``rf_spend``), as :class:`WideVarMapping` along
        ``channel`` (``rf_channel`` for reach and frequency)."""
        out: dict[str, WideVarMapping] = {}

        def add(var: str, dim: str, labels: list[str], cols: list[str], role: Role) -> None:
            if cols:
                out[var] = WideVarMapping(
                    dim=dim, columns=dict(zip(labels, cols, strict=True)), role=role
                )

        add("media", "channel", self.media_channels, self.media, Role.MEDIA)
        add("spend", "channel", self.media_channels, self.media_spend, Role.SPEND)
        add("reach", "rf_channel", self.rf_channels, self.reach, Role.MEDIA)
        add("frequency", "rf_channel", self.rf_channels, self.frequency, Role.MEDIA)
        add("rf_spend", "rf_channel", self.rf_channels, self.rf_spend, Role.SPEND)
        return out


# ============================================================================= mapping


class DimMapping(RequestModel):
    """A dim of the dataset, read from one column of a tabular upload."""

    column: str = Field(..., min_length=1, max_length=256)
    spec: DimSpec

    model_config = dto_examples(
        {"column": "date", "spec": {"kind": "time", "policy": "extend", "freq": "D"}}
    )


class VarMapping(RequestModel):
    """A data variable read from one column (dtype is inferred during ingest)."""

    column: str = Field(..., min_length=1, max_length=256)
    role: Role = Role.COVARIATE
    units: str | None = Field(None, max_length=64)
    missing: Literal["forbid", "allow", "mask"] = "forbid"
    description: str | None = Field(None, max_length=2000)

    model_config = dto_examples({"column": "revenue", "role": "target", "units": "EUR"})


class IngestMapping(RequestModel):
    """How an upload becomes a labelled dataset (Meridian ``CoordToColumns`` style).

    Tabular formats need a mapping; ``zarr_zip``/``netcdf`` are self-describing and may omit
    ``dims``/``vars``. Parsing happens only inside a sandboxed ingest run.

    With the ``mff`` preset the long table is first pivoted to one column
    per ``VariableName`` (plus ``Period`` and the key columns); ``vars`` and ``wide`` then
    pick and rename those variables (``column`` = a ``VariableName`` value) and ``dims`` may
    refine the dims the preset derives (``column`` = an MFF column). Without ``vars`` and
    ``wide`` every variable is kept under its own name.
    """

    format: IngestFormat
    layout: Literal["dense", "long"] = "dense"
    dims: dict[str, DimMapping] = Field(default_factory=dict, max_length=32)
    vars: dict[str, VarMapping] = Field(default_factory=dict, max_length=1024)
    # M3 fields are left out of serialized mappings unless used, so v0.3 servers keep
    # accepting the mappings they understand
    wide: dict[str, WideVarMapping] = Field(
        default_factory=dict,
        max_length=64,
        description="M3: variables read from one column per label of a dim (wide tables)",
        exclude_if=lambda v: not v,
    )
    preset: IngestPreset = Field(
        IngestPreset.NONE,
        description="M3: a file contract instead of (or refined by) the column mapping",
        exclude_if=lambda v: v is IngestPreset.NONE,
    )
    mff: MffOptions | None = Field(
        None,
        description="Options of the mff preset",
        exclude_if=lambda v: v is None,
    )
    meridian: MeridianColumns | None = Field(
        None, description="The meridian preset's columns", exclude_if=lambda v: v is None
    )

    model_config = ConfigDict(
        **dto_examples(
            {
                "format": "csv",
                "dims": {
                    "market": {"column": "market", "spec": {"kind": "categorical"}},
                    "date": {
                        "column": "date",
                        "spec": {"kind": "time", "policy": "extend", "freq": "D"},
                    },
                },
                "vars": {
                    "revenue_local": {"column": "revenue", "role": "target", "units": "local"},
                    "spend_local": {"column": "spend", "role": "spend", "units": "local"},
                },
            },
            {
                "format": "csv",
                "dims": {
                    "market": {"column": "market", "spec": {"kind": "categorical"}},
                    "week": {
                        "column": "week",
                        "spec": {"kind": "time", "policy": "extend", "freq": "W-SUN"},
                    },
                },
                "vars": {"revenue": {"column": "revenue", "role": "target", "units": "EUR"}},
                "wide": {"spend": WideVarMapping.model_json_schema()["examples"][0]},
            },
            {"format": "mff", "preset": "mff", "mff": {"freq": "W-MON"}},
            {
                "format": "parquet",
                "preset": "meridian",
                "meridian": {
                    "kpi": "revenue",
                    "media_channels": ["tv"],
                    "media": ["tv_impressions"],
                    "media_spend": ["tv_spend"],
                },
            },
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _mff_format(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("format") == "mff" and "preset" not in data:
            return {**data, "preset": IngestPreset.MFF.value}
        return data

    @model_validator(mode="after")
    def _tabular(self) -> IngestMapping:
        tabular = self.format in ("csv", "parquet", "mff")
        if self.preset is IngestPreset.NONE:
            if self.format == "mff":
                raise ValueError("format 'mff' needs the mff preset")
            if tabular and (not self.dims or not (self.vars or self.wide)):
                raise ValueError("csv/parquet uploads need at least one dim and one var mapping")
        elif not tabular:
            raise ValueError(f"the {self.preset.value} preset reads csv, parquet or mff files")
        if self.preset is IngestPreset.MERIDIAN and self.meridian is None:
            raise ValueError("the meridian preset needs 'meridian' (its column contract)")
        if self.meridian is not None and self.preset is not IngestPreset.MERIDIAN:
            raise ValueError("'meridian' is only used by the meridian preset")
        if self.preset is IngestPreset.MERIDIAN and self.format == "mff":
            raise ValueError("the meridian preset reads a wide csv or parquet table")
        if self.mff is not None and self.preset is not IngestPreset.MFF:
            raise ValueError("'mff' options are only used by the mff preset")
        if clash := sorted(set(self.vars) & set(self.wide)):
            raise ValueError(f"variables defined twice (vars and wide): {clash}")
        return self

    @property
    def effective_mff(self) -> MffOptions | None:
        """The MFF options the ingest run uses (defaults for the mff preset)."""
        if self.preset is IngestPreset.MFF:
            return self.mff or MffOptions()
        return None
