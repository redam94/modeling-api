"""Precomputed UI views: small JSON documents the runner writes next to datasets and bundles
and the browser reads through signed URLs (the control plane never decodes them).

Every view carries a ``format`` tag so readers can refuse versions they do not understand:

=====================  ========================  ==========================================
file                   ``format``                content
=====================  ========================  ==========================================
``preview.json``       ``mapi.preview/v1``       a long-format sample: dim then var columns
``profile.json``       ``mapi.profile/v1``       per-variable statistics + missingness grid
``ui/tree_index.json`` ``mapi.tree-index/v1``    structure of the bundle DataTree (no arrays)
=====================  ========================  ==========================================

Views are produced inside the sandbox from untrusted data: strings are capped and readers
must render them as text only.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PREVIEW_FORMAT",
    "PROFILE_FORMAT",
    "TREE_INDEX_FORMAT",
    "MissingnessGrid",
    "PreviewColumn",
    "PreviewView",
    "ProfileView",
    "TreeCoord",
    "TreeGroup",
    "TreeIndexView",
    "TreeVar",
    "VarProfile",
    "ViewScalar",
]

PREVIEW_FORMAT: Final = "mapi.preview/v1"
PROFILE_FORMAT: Final = "mapi.profile/v1"
TREE_INDEX_FORMAT: Final = "mapi.tree-index/v1"

ViewScalar = str | int | float | bool | None
"""A JSON scalar (datetimes are ISO strings; NaN is ``null``)."""

MAX_PREVIEW_ROWS: Final = 1000
MAX_GRID_COORDS: Final = 1000
MAX_TREE_COORD_VALUES: Final = 1000


class _View(BaseModel):
    """Views tolerate extra producer fields (readers ignore what they do not know)."""

    model_config = ConfigDict(extra="allow", frozen=True)


class PreviewColumn(_View):
    name: str = Field(..., max_length=256)
    dtype: str = Field(..., max_length=64)
    role: Literal["dim", "var"]


class PreviewView(_View):
    """``preview.json``: the first rows of the dataset in long format."""

    format: Literal["mapi.preview/v1"] = PREVIEW_FORMAT
    columns: list[PreviewColumn] = Field(..., max_length=1024)
    rows: list[list[ViewScalar]] = Field(..., max_length=MAX_PREVIEW_ROWS)
    total_rows: int | None = Field(None, ge=0)


class VarProfile(_View):
    dtype: str = Field(..., max_length=64)
    dims: list[str] = Field(default_factory=list, max_length=32)
    size: int = Field(0, ge=0)
    count: int = Field(0, ge=0, description="non-missing values")
    missing: int = Field(0, ge=0)
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    std: float | None = None


class MissingnessGrid(_View):
    """Fraction missing per (variable, coordinate of the time-like dim ``along``)."""

    along: str
    labels: list[str] = Field(..., max_length=1024)
    coords: list[str] = Field(..., max_length=MAX_GRID_COORDS)
    fraction: list[list[float]] = Field(..., max_length=1024)


class ProfileView(_View):
    """``profile.json``: per-variable statistics and a missingness heatmap."""

    format: Literal["mapi.profile/v1"] = PROFILE_FORMAT
    vars: dict[str, VarProfile] = Field(default_factory=dict, max_length=1024)
    missingness: MissingnessGrid | None = None


class TreeCoord(_View):
    dims: list[str] = Field(default_factory=list)
    dtype: str
    size: int = Field(..., ge=0)
    values: list[ViewScalar] | None = Field(None, max_length=MAX_TREE_COORD_VALUES)


class TreeVar(_View):
    dims: list[str] = Field(default_factory=list)
    shape: list[int] = Field(default_factory=list)
    dtype: str
    units: str | None = None
    description: str | None = None


class TreeGroup(_View):
    dims: dict[str, int] = Field(default_factory=dict)
    coords: dict[str, TreeCoord] = Field(default_factory=dict)
    vars: dict[str, TreeVar] = Field(default_factory=dict)
    attrs: dict[str, ViewScalar] = Field(default_factory=dict)


class TreeIndexView(_View):
    """``ui/tree_index.json``: one entry per DataTree node (keyed by its path)."""

    format: Literal["mapi.tree-index/v1"] = TREE_INDEX_FORMAT
    groups: dict[str, TreeGroup] = Field(default_factory=dict, max_length=4096)
