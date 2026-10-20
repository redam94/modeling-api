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

import json
import math
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import (
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from mapi_spec._dto import Id, RequestModel, ResponseModel, dto_examples
from mapi_spec._types import Name, StrictModel, unique
from mapi_spec.media import MAX_IMAGE_SIDE
from mapi_spec.schema import DimSpec, Role
from mapi_spec.tree import ROOT, NodePath, TreeJoin, is_ancestor

__all__ = [
    "COMBINE_SEPARATOR",
    "IMAGE_FOLDER_MAX_CELLS",
    "IMAGE_SUFFIXES",
    "MAX_ASSEMBLY_SOURCES",
    "MAX_IMAGES",
    "MAX_IMAGE_FILE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_INSPECTION_BYTES",
    "MAX_INSPECT_ARRAYS",
    "MAX_INSPECT_ATTRS",
    "MAX_INSPECT_ATTR_CHARS",
    "MAX_SAFETENSORS_HEADER",
    "MAX_WEIGHT_PARAMETERS",
    "MAX_WEIGHT_TENSORS",
    "MFF_COLUMNS",
    "MFF_KEY_COLUMNS",
    "MFF_PANEL_KEY_COLUMNS",
    "MFF_TOTAL_LABELS",
    "RESERVED_IMAGE_DIMS",
    "WEIGHTS_ATTR",
    "WEIGHTS_DIM",
    "WEIGHTS_VAR",
    "AssemblySource",
    "ColumnRef",
    "CombineRule",
    "DimMapping",
    "Hdf5Coord",
    "Hdf5Mapping",
    "Hdf5Var",
    "ImageFolderOptions",
    "IngestAssembly",
    "IngestFormat",
    "IngestMapping",
    "IngestPreset",
    "InspectedArray",
    "JsonScalar",
    "MeridianColumns",
    "MffOptions",
    "UploadInspection",
    "VarMapping",
    "WeightsOptions",
    "WideVarMapping",
]

IngestFormat = Literal[
    "csv", "parquet", "mff", "zarr_zip", "netcdf", "hdf5", "image_folder", "safetensors"
]
"""Upload formats. ``mff`` is a CSV laid out as an MFF long table (implies ``preset=mff``);
``zarr_zip`` (a single-object zarr v3 zip) and ``netcdf`` are self-describing (``netcdf``:
NetCDF-3/4 or an xarray/h5netcdf-written HDF5 file, read as a tree when it has groups).
``hdf5`` (M4) is a raw HDF5 file without dim metadata, described by
:class:`Hdf5Mapping`. ``image_folder`` (v0.14) is a zip of PNG/JPEG/TIFF images and a
manifest table, described by :class:`ImageFolderOptions`. ``safetensors`` (v0.15) is a file of
pretrained network weights, read into a weights dataset (:class:`WeightsOptions`)."""

TABULAR_FORMATS: Final = frozenset({"csv", "parquet", "mff"})

# --------------------------------------------------------------------- image folders (v0.14)

IMAGE_SUFFIXES: Final = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
"""File suffixes (lowercase) an ``image_folder`` upload reads."""
RESERVED_IMAGE_DIMS: Final = frozenset({"sample", "chain", "draw", "quantile", "bound", "moment"})
"""Dim names the image dim may not take: plots and metrics read them as draws or summaries."""
MAX_IMAGES: Final = 100_000
MAX_IMAGE_PIXELS: Final = 4096 * 4096
"""Pixels of one image (from its header, before it is decoded)."""
MAX_IMAGE_FILE_BYTES: Final = 32 * 1024**2
IMAGE_FOLDER_MAX_CELLS: Final = 256 * 1024**2
"""Values of the ``pixels`` variable (images x height x width x channels); equals the wire
limit ``WireLimits.max_store_elements`` of the SDK."""
_MANIFEST_SUFFIXES: Final = (".csv", ".tsv", ".parquet")

# ------------------------------------------------------------------ weights (v0.15)

WEIGHTS_VAR: Final = "weights"
"""The one variable of a weights dataset: every tensor flattened, float32."""
WEIGHTS_DIM: Final = "param"
WEIGHTS_ATTR: Final = "mapi_weights"
"""Attr of the weights variable: JSON of the tensor table (name, shape, source dtype,
offset), the file's ``__metadata__``, the architecture, the file sha256 and the
parameter count. A weights dataset uses one flat variable because a DataSchema allows at
most 256 dims."""
MAX_WEIGHT_TENSORS: Final = 4096
MAX_WEIGHT_PARAMETERS: Final = 500_000_000
MAX_SAFETENSORS_HEADER: Final = 8 * 1024**2
_MAX_ARCH_JSON: Final = 64 * 1024

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


# ======================================================================================= HDF5

HDF5_PATH_RE: Final = r"^/[^\x00]{1,1024}$"
"""An HDF5 object path inside the uploaded file (``/group/dataset``)."""

Hdf5Path = Annotated[str, Field(pattern=HDF5_PATH_RE)]
CoordValue = int | float | str


def _no_bool(value: Any) -> Any:
    """A coord value (a label, a range start or step): a number or a string, never a
    boolean, NaN or an infinity (a NaN label never matches itself)."""
    if isinstance(value, bool):
        raise ValueError("expected a number or a string, not a boolean")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("expected a finite number, not NaN or an infinity")
    return value


def _label_key(value: CoordValue) -> tuple[str, Any]:
    """Equal labels compare equal (``2024`` and ``2024.0`` are the same coord value)."""
    return ("str", value) if isinstance(value, str) else ("number", float(value))


class Hdf5Var(RequestModel):
    """A data variable read from one HDF5 dataset of a raw HDF5 upload (M4).

    ``dims`` names the array's axes in order (its length must equal the array's ``ndim``;
    the sandbox checks it). The variable is stored as ``name`` in tree node ``node``.
    """

    path: Hdf5Path
    node: NodePath = ROOT
    name: Name
    dims: list[Name] = Field(default_factory=list, max_length=32)
    role: Role = Role.COVARIATE
    units: str | None = Field(None, max_length=64)
    missing: Literal["forbid", "allow", "mask"] = "forbid"
    description: str | None = Field(None, max_length=2000)

    model_config = dto_examples(
        {
            "path": "/observations/image",
            "node": "/",
            "name": "image",
            "dims": ["row", "col"],
            "units": "counts",
        }
    )

    @field_validator("dims")
    @classmethod
    def _distinct(cls, v: list[str]) -> list[str]:
        if dups := unique(v):
            raise ValueError(f"duplicate dims: {dups}")
        return v


class Hdf5Coord(RequestModel):
    """The coordinate of dim ``dim`` in node ``node`` of a raw HDF5 upload (M4).

    Exactly one source: ``path`` (a 1-D HDF5 dataset holding the labels), or ``start`` with
    an optional ``step`` (a range: numbers, or a date and a pandas frequency such as
    ``"2024-01-01"`` + ``"W-MON"``), or neither (an integer index ``0..n-1``). ``spec``
    overrides the inferred :class:`~mapi_spec.schema.DimSpec`.
    """

    dim: Name
    node: NodePath = ROOT
    path: Hdf5Path | None = None
    start: CoordValue | None = None
    step: CoordValue | None = None
    spec: DimSpec | None = None

    model_config = dto_examples(
        {"dim": "row", "start": 0.0, "step": 0.05, "spec": {"kind": "float", "ordered": True}},
        {"dim": "date", "path": "/axes/date", "spec": {"kind": "time", "policy": "extend"}},
    )

    _scalars = field_validator("start", "step", mode="before")(_no_bool)

    @model_validator(mode="after")
    def _one_source(self) -> Hdf5Coord:
        if self.path is not None and self.start is not None:
            raise ValueError("give either path or start (a range), not both")
        if self.step is not None and self.start is None:
            raise ValueError("step needs start")
        if self.step is not None:
            if isinstance(self.start, str) != isinstance(self.step, str):
                raise ValueError(
                    "a range is numbers (start and step) or a date and a frequency string "
                    "(start '2024-01-01', step 'W-MON'), not a mix"
                )
            if not isinstance(self.step, str) and self.step == 0:
                raise ValueError("step must not be 0")
        return self


class Hdf5Mapping(RequestModel):
    """How a raw HDF5 upload becomes a (tree) dataset (M4): which arrays become which
    variables of which node, with which dims and coords. Arrays that are not mapped are
    reported in one warning, never read."""

    vars: list[Hdf5Var] = Field(..., min_length=1, max_length=1024)
    coords: list[Hdf5Coord] = Field(default_factory=list, max_length=64)

    model_config = dto_examples(
        {
            "vars": [
                Hdf5Var.model_json_schema()["examples"][0],
                {"path": "/psf/kernel", "node": "/psf", "name": "psf", "dims": ["prow", "pcol"]},
            ],
            "coords": [Hdf5Coord.model_json_schema()["examples"][0]],
        }
    )

    @model_validator(mode="after")
    def _check(self) -> Hdf5Mapping:
        keys = [f"{v.node}:{v.name}" for v in self.vars]
        if dups := unique(keys):
            raise ValueError(f"variables mapped twice (node:name): {dups}")
        coord_keys = [f"{c.node}:{c.dim}" for c in self.coords]
        if dups := unique(coord_keys):
            raise ValueError(f"coords given twice (node:dim): {dups}")
        for v in self.vars:
            if v.name in v.dims:
                raise ValueError(f"variable {v.name!r} has the name of one of its dims")
        node_dims: dict[str, set[str]] = {}
        node_vars: dict[str, set[str]] = {}
        for v in self.vars:
            node_dims.setdefault(v.node, set()).update(v.dims)
            node_vars.setdefault(v.node, set()).add(v.name)
        for node, names in node_vars.items():
            if clash := sorted(names & node_dims[node]):
                raise ValueError(f"node {node!r}: names used both as dim and variable: {clash}")
        for node, names in node_vars.items():
            # a child sees its ancestors' dims as inherited coords, which hide a variable
            # of the same name
            for anc, dims in node_dims.items():
                if is_ancestor(anc, node) and (clash := sorted(names & dims)):
                    raise ValueError(
                        f"node {node!r}: variable(s) {clash} have the name of a dim of its "
                        f"ancestor {anc!r}"
                    )
        for c in self.coords:
            # a coord serves its node and every node below it (inherited coords)
            users = [
                n
                for n, dims in node_dims.items()
                if c.dim in dims and (n == c.node or c.node == ROOT or n.startswith(c.node + "/"))
            ]
            if not users:
                raise ValueError(
                    f"coord {c.dim!r} of node {c.node!r} is not a dim of any variable mapped "
                    "to that node or below it"
                )
        return self


# ============================================================================ image folders


class ImageFolderOptions(RequestModel):
    """How a zip of images and a manifest table becomes ``pixels(image, y, x, channel)``.

    The manifest has one row per image: the image path in the zip and, optionally, an id, a
    class label and a train/test split. The images must have 8 bits per channel and one
    size, unless ``size`` resizes them. Every byte is decoded inside the sandboxed ingest run.
    """

    manifest: str = Field(
        "manifest.csv", min_length=1, max_length=256, description="Path of the manifest in the zip"
    )
    path_column: ColumnRef = Field("path", description="Image paths relative to the zip root")
    id_column: ColumnRef | None = Field(
        None, description="Unique labels of the image dim (default: the row number)"
    )
    label_column: ColumnRef | None = Field(
        None, description="Class labels: the text variable 'label(image)', role target"
    )
    split_column: ColumnRef | None = Field(
        None, description="Train/test assignment: the text coord 'split(image)'"
    )
    columns: list[ColumnRef] | None = Field(
        None, max_length=64, description="Other columns kept as coords (default: all others)"
    )
    var: Name = "pixels"
    dim: Name = "image"
    mode: Literal["auto", "gray", "rgb"] = Field(
        "auto", description="auto: rgb when an image has colour, else gray"
    )
    size: tuple[int, int] | None = Field(
        None, description="(height, width) to resize every image to; each 1..4096"
    )
    resize: Literal["stretch", "crop", "pad"] = Field(
        "stretch", description="How images are resized (only with size)"
    )
    max_images: int = Field(20_000, ge=1, le=MAX_IMAGES)

    model_config = dto_examples(
        {"manifest": "manifest.csv", "path_column": "file", "label_column": "class",
         "split_column": "split"},
        {"manifest": "labels.parquet", "id_column": "id", "label_column": "label",
         "mode": "rgb", "size": [32, 32], "resize": "crop", "max_images": 5000},
    )  # fmt: skip

    @field_validator("manifest")
    @classmethod
    def _manifest(cls, v: str) -> str:
        parts = v.split("/")
        if (
            v.startswith("/")
            or "\\" in v
            or "\x00" in v
            or any(p in ("", ".", "..") for p in parts)
        ):
            raise ValueError(f"manifest {v!r} must be a relative path in the zip, without '..'")
        if not v.lower().endswith(_MANIFEST_SUFFIXES):
            raise ValueError(f"manifest {v!r} must end with {', '.join(_MANIFEST_SUFFIXES)}")
        return v

    @field_validator("dim")
    @classmethod
    def _dim(cls, v: str) -> str:
        if v in RESERVED_IMAGE_DIMS or v in ("y", "x", "channel"):
            raise ValueError(
                f"dim {v!r} is reserved; use another name for the image dim (such as 'image')"
            )
        return v

    @field_validator("size")
    @classmethod
    def _size(cls, v: tuple[int, int] | None) -> tuple[int, int] | None:
        if v is not None and not all(1 <= n <= MAX_IMAGE_SIDE for n in v):
            raise ValueError(f"size (height, width) must be 1..{MAX_IMAGE_SIDE} each")
        return v

    @model_validator(mode="after")
    def _columns_distinct(self) -> ImageFolderOptions:
        named = [
            c
            for c in (self.path_column, self.id_column, self.label_column, self.split_column)
            if c is not None
        ]
        if dups := unique(named):
            raise ValueError(f"a manifest column has two roles: {dups}")
        if self.var == self.dim:
            raise ValueError("var and dim need different names")
        if self.resize != "stretch" and self.size is None:
            raise ValueError("resize is only used with size")
        return self


# ============================================================================ weights


class WeightsOptions(RequestModel):
    """A safetensors file of pretrained network weights (read in the sandboxed ingest run).

    The file becomes a weights dataset: ``weights(param)`` float32 plus the tensor table in
    the ``mapi_weights`` attr. Models and steps read it as a side table.
    """

    arch: dict[str, JsonValue] | None = Field(
        None,
        description="Architecture JSON, when the file's __metadata__ has no 'mapi_arch'",
    )
    max_parameters: int = Field(200_000_000, ge=1, le=MAX_WEIGHT_PARAMETERS)

    model_config = dto_examples(
        {"max_parameters": 50_000_000},
        {"arch": {"kind": "resnet", "channels": 3, "widths": [16, 32, 64], "blocks": [1, 1, 1]}},
    )

    @field_validator("arch")
    @classmethod
    def _arch_size(cls, v: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if v is not None:
            text = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if len(text.encode("utf-8")) > _MAX_ARCH_JSON:
                raise ValueError(f"arch JSON must be at most {_MAX_ARCH_JSON} bytes")
        return v


# ======================================================================= IngestMapping


class IngestMapping(RequestModel):
    """How an upload becomes a labelled dataset (Meridian ``CoordToColumns`` style).

    Tabular formats need a mapping; ``zarr_zip``/``netcdf`` are self-describing and may omit
    ``dims``/``vars`` (with groups, they rename/select per node by name). A raw ``hdf5``
    file (M4) needs ``hdf5`` (:class:`Hdf5Mapping`) instead of ``dims``/``vars``. Parsing
    happens only inside a sandboxed ingest run.

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
    hdf5: Hdf5Mapping | None = Field(
        None,
        description="M4: the arrays, dims and coords of a raw HDF5 upload (format 'hdf5')",
        exclude_if=lambda v: v is None,
    )
    images: ImageFolderOptions | None = Field(
        None,
        description="v0.14: options of an 'image_folder' upload (defaults when left out)",
        exclude_if=lambda v: v is None,
    )
    weights: WeightsOptions | None = Field(
        None,
        description="v0.15: options of a 'safetensors' upload (defaults when left out)",
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
            {"format": "hdf5", "hdf5": Hdf5Mapping.model_json_schema()["examples"][0]},
            {
                "format": "image_folder",
                "images": ImageFolderOptions.model_json_schema()["examples"][0],
            },
            {"format": "safetensors", "weights": {"max_parameters": 50_000_000}},
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
        if self.format == "hdf5":
            if self.hdf5 is None:
                raise ValueError("format 'hdf5' needs 'hdf5' (which arrays become variables)")
            if self.dims or self.vars or self.wide:
                raise ValueError("a raw HDF5 upload is described by 'hdf5', not dims/vars/wide")
        elif self.hdf5 is not None:
            raise ValueError("'hdf5' is only used with format 'hdf5'")
        if self.format == "image_folder":
            if self.dims or self.vars or self.wide or self.preset is not IngestPreset.NONE:
                raise ValueError(
                    "an image_folder upload is described by 'images', not dims/vars/wide or a "
                    "preset"
                )
        elif self.images is not None:
            raise ValueError("'images' is only used with format 'image_folder'")
        if self.format == "safetensors":
            if (
                self.dims
                or self.vars
                or self.wide
                or self.preset is not IngestPreset.NONE
                or self.layout != "dense"
            ):
                raise ValueError(
                    "a safetensors upload is described by 'weights', not dims/vars/wide or a preset"
                )
        elif self.weights is not None:
            raise ValueError("'weights' is only used with format 'safetensors'")
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


# ===================================================================== multi-file versions

MAX_ASSEMBLY_SOURCES: Final = 64
"""Uploads combined into one dataset version."""

CombineRule = Literal["node", "concat", "merge"]
"""How a source joins the other sources of its node: ``node`` (it is the node's only
source), ``concat`` (along ``concat_dim``, one label per file: a year, a region) or
``merge`` (variables of files on shared coords)."""


class AssemblySource(RequestModel):
    """One upload of a multi-file dataset version (M4) and where it goes.

    The upload is ingested with its own ``mapping`` into node ``node``; a file that is
    itself a tree mounts its nodes *under* ``node`` (file group ``/psf`` at node
    ``/instrument`` becomes ``/instrument/psf``). Sources of the same node are combined by
    ``combine`` (see :data:`CombineRule`).
    """

    upload_id: Id
    mapping: IngestMapping
    node: NodePath = ROOT
    combine: CombineRule = "node"
    concat_dim: Name | None = Field(None, description="combine 'concat': the dim to stack on")
    concat_label: CoordValue | None = Field(
        None, description="combine 'concat': this file's label along concat_dim"
    )
    join: TreeJoin = Field("exact", description="How coords align when sources are combined")

    model_config = dto_examples(
        {
            "upload_id": "upl_01jaf3d1e2f3g4h5j6k7m8n9p0",
            "mapping": {"format": "netcdf"},
            "node": "/",
            "combine": "concat",
            "concat_dim": "year",
            "concat_label": 2024,
        }
    )

    _label = field_validator("concat_label", mode="before")(_no_bool)

    @model_validator(mode="after")
    def _concat_fields(self) -> AssemblySource:
        if self.combine == "concat":
            if self.concat_dim is None or self.concat_label is None:
                raise ValueError("combine 'concat' needs concat_dim and concat_label")
        elif self.concat_dim is not None or self.concat_label is not None:
            raise ValueError("concat_dim and concat_label are only used by combine 'concat'")
        return self


class IngestAssembly(RequestModel):
    """A dataset version built from several uploads (M4): every source is ingested in the
    same sandboxed run (inputs ``upload.<i>``, ``i`` = the source's index), then combined
    per node. The result is a tree dataset unless every source lands in the root."""

    sources: list[AssemblySource] = Field(..., min_length=1, max_length=MAX_ASSEMBLY_SOURCES)

    model_config = dto_examples(
        {
            "sources": [
                AssemblySource.model_json_schema()["examples"][0],
                {
                    "upload_id": "upl_01jaf3d1e2f3g4h5j6k7m8n9p1",
                    "mapping": {"format": "netcdf"},
                    "combine": "concat",
                    "concat_dim": "year",
                    "concat_label": 2025,
                },
                {
                    "upload_id": "upl_01jaf3d1e2f3g4h5j6k7m8n9p2",
                    "mapping": {"format": "netcdf"},
                    "node": "/instrument",
                },
            ]
        }
    )

    @model_validator(mode="after")
    def _combine(self) -> IngestAssembly:
        if dups := unique(s.upload_id for s in self.sources):
            raise ValueError(f"an upload may be used once: {dups}")
        if len(self.sources) > 1 and any(s.mapping.format == "image_folder" for s in self.sources):
            raise ValueError("an image_folder upload must be the only source of a dataset version")
        if len(self.sources) > 1 and any(s.mapping.format == "safetensors" for s in self.sources):
            raise ValueError("a safetensors upload must be the only source of a dataset version")
        by_node: dict[str, list[AssemblySource]] = {}
        for s in self.sources:
            by_node.setdefault(s.node, []).append(s)
        for node, group in by_node.items():
            rules = {s.combine for s in group}
            if len(rules) > 1:
                raise ValueError(
                    f"node {node!r}: sources combine in different ways {sorted(rules)}"
                )
            rule = group[0].combine
            if rule == "node" and len(group) > 1:
                raise ValueError(
                    f"node {node!r} has {len(group)} sources: combine them with 'concat' or 'merge'"
                )
            if rule == "concat":
                if len({s.concat_dim for s in group}) > 1:
                    raise ValueError(f"node {node!r}: concat sources need the same concat_dim")
                keys = [_label_key(s.concat_label) for s in group if s.concat_label is not None]
                if len({k for k, _ in keys}) > 1:
                    raise ValueError(f"node {node!r}: concat labels mix numbers and strings")
                if len(set(keys)) != len(keys):
                    raise ValueError(f"node {node!r}: concat labels must be unique")
            if rule in ("concat", "merge") and len({s.join for s in group}) > 1:
                raise ValueError(f"node {node!r}: {rule} sources need the same join")
        return self

    def nodes(self) -> dict[str, list[int]]:
        """Node → indices of its sources (the ``upload.<i>`` input of each)."""
        out: dict[str, list[int]] = {}
        for i, s in enumerate(self.sources):
            out.setdefault(s.node, []).append(i)
        return out


# ================================================================================ inspect

MAX_INSPECT_ARRAYS: Final = 2000
"""Arrays an inspection lists; past it the listing is ``truncated``."""
MAX_INSPECT_ATTRS: Final = 64
"""Attributes kept per array (small scalars only; the rest are dropped)."""
MAX_INSPECT_ATTR_CHARS: Final = 1024
"""Characters of one string attribute value an inspection keeps (longer ones are dropped
or truncated by the inspector)."""
MAX_INSPECTION_BYTES: Final = 8 * 1024**2
"""The whole inspection as JSON (it is stored on the upload row and served as is)."""

JsonScalar = str | int | float | bool | None
_InspectStr = Annotated[str, StringConstraints(max_length=MAX_INSPECT_ATTR_CHARS)]
InspectAttr = _InspectStr | int | float | bool | None
_AttrKey = Annotated[str, StringConstraints(max_length=256)]
_ObjectPath = Annotated[str, StringConstraints(max_length=1025)]
_ShortStr = Annotated[str, StringConstraints(max_length=256)]
_Message = Annotated[str, StringConstraints(max_length=2000)]


class InspectedArray(ResponseModel):
    """One array of an inspected upload (from its metadata only; no data is read).

    ``dims_hint`` comes from the file's own dim metadata where there is some (HDF5
    ``DIMENSION_LIST``, zarr ``_ARRAY_DIMENSIONS``, NetCDF dims).
    """

    path: str = Field(..., max_length=1025)
    shape: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list, max_length=64)
    dtype: str = Field(..., max_length=64)
    chunks: list[Annotated[int, Field(ge=1)]] | None = Field(None, max_length=64)
    filters: list[_ShortStr] = Field(default_factory=list, max_length=32)
    dims_hint: list[_ShortStr] | None = Field(None, max_length=64)
    attrs: dict[_AttrKey, InspectAttr] = Field(default_factory=dict, max_length=MAX_INSPECT_ATTRS)

    model_config = dto_examples(
        {
            "path": "/observations/image",
            "shape": [64, 64],
            "dtype": "float32",
            "chunks": [64, 64],
            "filters": ["deflate", "shuffle"],
            "dims_hint": None,
            "attrs": {"units": "counts"},
        }
    )


class UploadInspection(ResponseModel):
    """What an ``inspect`` run found in an upload (``inspection.json``; M4): its groups and
    arrays, so a raw HDF5 file can be mapped (:class:`Hdf5Mapping`) and a NetCDF/zarr file's
    groups previewed before ingest."""

    format: IngestFormat
    size_bytes: int = Field(..., ge=0)
    groups: list[_ObjectPath] = Field(default_factory=list, max_length=10_000)
    arrays: list[InspectedArray] = Field(default_factory=list, max_length=MAX_INSPECT_ARRAYS)
    truncated: bool = False
    warnings: list[_Message] = Field(default_factory=list, max_length=1000)

    model_config = dto_examples(
        {
            "format": "hdf5",
            "size_bytes": 2_097_152,
            "groups": ["/", "/observations", "/psf"],
            "arrays": [
                InspectedArray.model_json_schema()["examples"][0],
                {"path": "/psf/kernel", "shape": [9, 9], "dtype": "float64"},
            ],
            "truncated": False,
            "warnings": [],
        }
    )

    @model_validator(mode="after")
    def _size(self) -> Self:
        size = len(self.model_dump_json().encode())
        if size > MAX_INSPECTION_BYTES:
            raise ValueError(
                f"the inspection is {size} bytes of JSON, over {MAX_INSPECTION_BYTES}: "
                "list fewer arrays (truncated) or fewer attributes"
            )
        return self
