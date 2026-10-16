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
``bayesinsight``   an MFF table plus BayesInsight's ``metadata.json``
                   (:class:`BayesInsightMetadata`); the model itself comes from
                   ``model_def.json`` (:class:`BayesInsightModelDef`, imported by
                   ``POST /v1/guided/import/bayesinsight`` as a spec for ``mmm.bayesinsight``)
=================  ===========================================================================

Wide tables (one column per channel) become one variable along a new dim through
:class:`WideVarMapping` - the shape Meridian and most spreadsheets use.

This module only *describes* files; every byte of an upload is parsed inside the sandbox.
``model_def.json`` and ``metadata.json`` are small JSON documents and are validated here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from mapi_spec._dto import RequestModel, dto_examples
from mapi_spec._types import Name, StrictModel
from mapi_spec.schema import DimSpec, Role

__all__ = [
    "BAYESINSIGHT_PLUGIN",
    "BAYESINSIGHT_VARIABLE_TYPES",
    "MFF_COLUMNS",
    "MFF_KEY_COLUMNS",
    "BayesInsightMetadata",
    "BayesInsightModelDef",
    "BayesInsightVarRef",
    "BayesInsightVariable",
    "ColumnRef",
    "DimMapping",
    "IngestFormat",
    "IngestMapping",
    "IngestPreset",
    "MeridianColumns",
    "MffOptions",
    "VarMapping",
    "WideVarMapping",
    "bayesinsight_var_ref",
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
    BAYESINSIGHT = "bayesinsight"


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
"""The MFF long-format columns, in BayesInsight's order (``bayesinsight.lib.constants``).
Every row holds one value: ``VariableValue`` of variable ``VariableName`` for one
``Period`` and one combination of the key columns."""

MFF_KEY_COLUMNS: Final[tuple[str, ...]] = ("Geography", "Product", "Outlet", "Campaign", "Creative")
"""The MFF key columns besides ``Period`` (each becomes a dim when it has >1 value)."""


class MffOptions(StrictModel):
    """The MFF ("model feature format") long-table preset.

    One row per ``(period, geography, product, campaign, outlet, creative, variable)`` with
    the value in ``value_column``. ``Period`` becomes the time dim; the other key columns
    become dims only when they hold more than one distinct value; ``VariableName`` values
    become data variables.
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


# =============================================================================== BayesInsight

BAYESINSIGHT_PLUGIN: Final = "mmm.bayesinsight@^1"
"""The built-in plugin that runs imported BayesInsight models (Q8)."""

BAYESINSIGHT_VARIABLE_TYPES: Final = ("control", "media", "exog", "localtrend", "season")
"""``variable_type`` values of ``model_def.json`` (``exog`` is the KPI, exactly one)."""


class BayesInsightMetadata(StrictModel):
    """BayesInsight's ``metadata.json`` (``{"metadata": {...}}`` is accepted too).

    ``row_ids`` are the MFF key columns the model is indexed by (``Period`` always last);
    the key columns *not* in ``row_ids`` are aggregated away and suffix variable names
    (``Promotion_Total_Total_Total``).
    """

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    allowed_geos: list[str] | None = Field(None, max_length=10_000)
    allowed_products: list[str] | None = Field(None, max_length=10_000)
    allowed_outlets: list[str] | None = Field(None, max_length=10_000)
    allowed_campaigns: list[str] | None = Field(None, max_length=10_000)
    allowed_creatives: list[str] | None = Field(None, max_length=10_000)
    necessary_variables: list[str] | None = Field(None, max_length=4096)
    start_period: str | None = Field(None, max_length=40)
    end_period: str | None = Field(None, max_length=40)
    periodicity: Literal["Weekly", "Daily"] = "Weekly"
    row_ids: tuple[str, ...] = ("Period",)

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, data: Any) -> Any:
        if isinstance(data, dict) and set(data) == {"metadata"}:
            return data["metadata"]
        return data

    @field_validator("row_ids")
    @classmethod
    def _row_ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        allowed = {"Period", *MFF_KEY_COLUMNS}
        if bad := [c for c in v if c not in allowed]:
            raise ValueError(f"unknown row ids {bad}; expected MFF key columns")
        keys = tuple(c for c in MFF_KEY_COLUMNS if c in v)  # BayesInsight's canonical order
        return (*keys, "Period")

    @property
    def suffix_columns(self) -> tuple[str, ...]:
        """Key columns that suffix analytic variable names, in MFF order."""
        return tuple(c for c in MFF_KEY_COLUMNS if c not in self.row_ids)

    @property
    def freq(self) -> str:
        return "W" if self.periodicity == "Weekly" else "D"


class BayesInsightVariable(StrictModel):
    """One entry of ``model_def.json``'s ``variable_details``.

    The fields the import interprets are typed; everything else (priors, transforms,
    likelihood parameters) is kept verbatim for the ``mmm.bayesinsight`` plugin, which
    validates it in the sandbox.
    """

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)

    variable_name: str = Field(..., min_length=1, max_length=512)
    variable_type: Literal["control", "media", "exog", "localtrend", "season"]
    normalization: str | None = Field(None, max_length=64)
    sign: Literal["positive", "negative"] | None = None
    random_coeff_dims: list[str] | None = Field(None, max_length=6)
    fixed_ind_coeff_dims: list[str] | None = Field(None, max_length=6)


class BayesInsightModelDef(StrictModel):
    """BayesInsight's ``model_def.json``. ``artifact``, ``fitted`` and ``VOF`` describe a
    local fit and are ignored by the import."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    variable_details: list[BayesInsightVariable] = Field(..., min_length=1, max_length=1024)
    artifact: str | None = Field(None, max_length=1024)
    fitted: bool = False
    vof: JsonValue | None = Field(None, alias="VOF")

    @property
    def kpi(self) -> BayesInsightVariable | None:
        """The single ``exog`` variable (the KPI), if there is exactly one."""
        exog = [v for v in self.variable_details if v.variable_type == "exog"]
        return exog[0] if len(exog) == 1 else None


class BayesInsightVarRef(StrictModel):
    """Where an analytic BayesInsight variable lives in the ingested dataset: data variable
    ``variable`` restricted to ``sel`` (one label per suffix key column that became a dim)."""

    analytic_name: str
    variable: str
    sel: dict[str, str] = Field(default_factory=dict)


def bayesinsight_var_ref(
    analytic_name: str,
    metadata: BayesInsightMetadata,
    mff: MffOptions | None = None,
) -> BayesInsightVarRef:
    """Map an analytic variable name (``<VariableName>_<Outlet>_<Campaign>_<Creative>`` for
    ``row_ids = [Geography, Product, Period]``) to the MFF ingest result.

    The MFF preset makes a key column a dim only when it has more than one value, which the
    metadata's ``allowed_*`` lists tell: suffix values of single-valued columns are dropped,
    the others become a selection on their dim. Raises ``ValueError`` when the name does not
    carry the expected number of suffixes.
    """
    mff = mff or MffOptions()
    suffix = metadata.suffix_columns
    if not suffix:
        return BayesInsightVarRef(analytic_name=analytic_name, variable=analytic_name)
    parts = analytic_name.rsplit("_", len(suffix))
    if len(parts) != len(suffix) + 1 or not parts[0]:
        raise ValueError(
            f"{analytic_name!r} does not end with one '_<value>' per column of {list(suffix)}"
        )
    allowed = {
        "Geography": metadata.allowed_geos,
        "Product": metadata.allowed_products,
        "Outlet": metadata.allowed_outlets,
        "Campaign": metadata.allowed_campaigns,
        "Creative": metadata.allowed_creatives,
    }
    sel: dict[str, str] = {}
    for col, value in zip(suffix, parts[1:], strict=True):
        values = allowed[col]
        if values is not None and len(values) <= 1:
            continue  # a single-valued key column does not become a dim
        sel[mff.key_columns.get(col, col.lower())] = value
    return BayesInsightVarRef(analytic_name=analytic_name, variable=parts[0], sel=sel)


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
        description="Options of the mff/bayesinsight presets",
        exclude_if=lambda v: v is None,
    )
    meridian: MeridianColumns | None = Field(
        None, description="The meridian preset's columns", exclude_if=lambda v: v is None
    )
    bayesinsight: BayesInsightMetadata | None = Field(
        None,
        description="BayesInsight's metadata.json (the bayesinsight preset)",
        exclude_if=lambda v: v is None,
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
                raise ValueError("format 'mff' needs the mff (or bayesinsight) preset")
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
        if self.mff is not None and self.preset not in (
            IngestPreset.MFF,
            IngestPreset.BAYESINSIGHT,
        ):
            raise ValueError("'mff' options are only used by the mff and bayesinsight presets")
        if self.bayesinsight is not None and self.preset is not IngestPreset.BAYESINSIGHT:
            raise ValueError("'bayesinsight' metadata is only used by the bayesinsight preset")
        if clash := sorted(set(self.vars) & set(self.wide)):
            raise ValueError(f"variables defined twice (vars and wide): {clash}")
        return self

    @property
    def effective_mff(self) -> MffOptions | None:
        """The MFF options the ingest run uses (defaults for the mff-based presets)."""
        if self.preset in (IngestPreset.MFF, IngestPreset.BAYESINSIGHT):
            return self.mff or MffOptions(
                freq=self.bayesinsight.freq if self.bayesinsight is not None else None
            )
        return None
