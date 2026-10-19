"""Structure detection for the Data Explorer: which data shapes a dataset holds.

:func:`detect_structure` is pure (numpy + xarray, no I/O). It reads the dims, coords,
variables and schema roles of a dataset (one node of a tree dataset) and sorts its variables
into data shapes (:data:`mapi_spec.views.ShapeKind`):

==================  =========================================================================
shape               found when
==================  =========================================================================
``tree``            an integer ``parent(node)`` with values in ``[-1, n)`` and a root
``areal_map``       ``ring_x`` and ``ring_y`` on the same two dims ``(area, vertex)``
``network``         a numeric variable on a pair of dims ``(X, X_)`` of the same size
``image``           a numeric variable on a spatial pair of dims (``row``/``col``, ``y``/``x``,
                    ``lat``/``lon``, with a prefix or suffix), else (no time dim, no media,
                    spend or control) on its last two numeric dims of 8+ levels
``vector_field``    an image with a ``component`` dim of size 2
``circular``        a numeric variable in degrees or radians (not a lat/lon/temperature), or in
                    hours/days/months with a name of a time of day (``hour_lst``)
``profile``         variables along a numeric non-time axis (position, multipole, ...)
``points``          two numeric variables with position names on one dim that is not time
                    (``x``/``y`` also need integers, length units or a dim of points)
``table``           a 2-D variable ``(item, columns)`` with a small categorical ``columns`` dim
``timeseries``      variables on a time dim (``kind: time`` or a datetime coord)
``sequence``        variables on an extend-policy, strictly increasing numeric dim (a label
                    index only with a time-word name or media/spend along it)
==================  =========================================================================

The geometry of a shape (polygon rings, neighbour and tree links, x/y positions) is not data:
the classic sections leave it out. A :class:`~mapi_spec.explore.StructureHints` overrides a
rule for one dim or variable. ``python -m mapi_runner.worker.structure <file.nc> [--node
/path]`` prints the shapes of a file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_spec.explore import StructureHints
from mapi_spec.schema import DataSchema, Role
from mapi_spec.views import DATA_SECTIONS

__all__ = [
    "ANGLE_PERIODS",
    "Shape",
    "Structure",
    "angle_period",
    "detect_structure",
    "is_label_index",
    "time_strength",
]

_AXIS_WORDS: Final = re.compile(
    r"(multipole|^ell$|wavelength|wavenumber|^freq|frequency|energy|position|depth|radius"
    r"|distance|^k$|^lag)"
)
_LOG_X_WORDS: Final = re.compile(r"^(multipole|ell|k|frequency|freq|wavenumber)$")
_SPATIAL_PAIRS: Final = (
    ("row", "col"), ("y", "x"), ("lat", "lon"), ("latitude", "longitude"), ("i", "j"),
    ("north", "east"), ("row", "column"),
)  # fmt: skip
_ANGLE_TOKENS: Final = frozenset(
    {"direction", "dir", "bearing", "angle", "heading", "azimuth", "hour", "clock", "phase",
     "month", "aspect", "orientation", "doy"}
)  # fmt: skip
"""Whole name parts (split at ``_``) that name an angle or a time of day: ``hour_lst`` and
``wind_dir`` match, ``hours_open`` and ``months_active`` (durations) do not."""
_ANGLE_PHRASES: Final = re.compile(r"(time_of_day|hour_of_day|day_of_year|_from$)")
_NOT_ANGLE_TOKENS: Final = frozenset(
    {"lat", "latitude", "lon", "lng", "long", "longitude", "temp", "temperature", "celsius",
     "fahrenheit", "slope", "elevation", "altitude", "zenith", "tilt", "incline"}
)  # fmt: skip
"""Name parts of a variable in degrees that is not circular (a position, a temperature)."""
ANGLE_PERIODS: Final[dict[str, tuple[float, str]]] = {
    "deg": (360.0, "deg"), "degree": (360.0, "deg"), "degrees": (360.0, "deg"),
    "rad": (2 * np.pi, "rad"), "radian": (2 * np.pi, "rad"), "radians": (2 * np.pi, "rad"),
    "h": (24.0, "h"), "hr": (24.0, "h"), "hour": (24.0, "h"), "hours": (24.0, "h"),
    "d": (365.25, "d"), "day": (365.25, "d"), "days": (365.25, "d"),
    "month": (12.0, "month"), "months": (12.0, "month"),
}  # fmt: skip
"""Units of a circular variable: ``units -> (period, canonical units)``."""
_COMPONENT_LABELS: Final = ({"x", "y"}, {"u", "v"}, {"0", "1"}, {"dx", "dy"}, {"east", "north"})
_CHANNEL_NAMES: Final = ("rgb", "channel", "channels", "band", "bands", "colour", "color")
_X_STRONG: Final = frozenset({"col", "column", "lon", "lng", "longitude", "easting"})
_Y_STRONG: Final = frozenset({"row", "lat", "latitude", "northing"})
_X_BARE: Final = frozenset({"x", "east"})
_Y_BARE: Final = frozenset({"y", "north"})
_POS_PARTS: Final = frozenset(
    {"pos", "position", "coord", "coords", "loc", "location", "centroid", "center", "centre",
     "site", "station", "grid", "cell", "pixel", "px", "point", "pt", "chip", "m", "km", "cm",
     "mm", "um", "nm", "pix", "deg", "ft", "mi"}
)  # fmt: skip
"""The other name parts of a position named ``x`` or ``y`` (``pos_x``, ``x_km``): ``sales_x``
(a pandas merge suffix) is not a position."""
_POINT_DIMS: Final = re.compile(
    r"(point|site|station|location|loc|cell|chip|plot|sensor|well|tree|stand|quadrat|sample)"
)
_LENGTH_UNITS: Final = frozenset(
    {"m", "km", "cm", "mm", "um", "nm", "px", "pixel", "pixels", "deg", "degree", "degrees",
     "degrees_east", "degrees_north", "ft", "mi", "au", "arcsec", "arcmin"}
)  # fmt: skip
_TIME_NAMES: Final = frozenset(
    {"t", "time", "times", "step", "steps", "period", "periods", "week", "weeks", "day", "days",
     "date", "dates", "month", "months", "quarter", "quarters", "year", "years", "epoch",
     "iteration", "iter", "generation", "tick", "wave", "visit"}
)  # fmt: skip
"""Name parts of a dim that orders observations in time (``week``, ``step``, ``t``)."""
_CALENDAR_NAMES: Final = re.compile(
    r"^(time|date|dates|week|weeks|day|days|month|months|quarter|quarters|year|years"
    r"|period|periods)$"
)
"""Names of an ``extend`` dim with numbers as coords that is real time (an integer week of an
MMM panel): the time patterns apply."""
_DRIVER_ROLES: Final = frozenset({Role.TARGET, Role.MEDIA, Role.SPEND, Role.CONTROL, Role.EXPOSURE})
_SPEND_ROLES: Final = frozenset({Role.MEDIA, Role.SPEND})
_LAB_TRIPLES: Final = (("cie_l", "cie_a", "cie_b"), ("L", "a", "b"), ("lab_l", "lab_a", "lab_b"))
_GEOMETRY_TREE: Final = ("parent", "branch_length", "is_tip", "tip_label")
_PICTURE_SECTION: Final = {
    "image": "images", "vector_field": "images", "profile": "profiles", "areal_map": "map",
    "network": "network", "tree": "tree", "circular": "circular", "points": "points",
    "timeseries": "timeseries", "sequence": "timeseries", "table": "correlation",
}  # fmt: skip
_TIME_SECTIONS: Final = ("ccf", "acf_pacf", "stationarity", "stl")
_DATA_ORDER: Final = (
    "overview", "quality", "distributions", "timeseries", "missingness", "outliers",
    "correlation", "ccf", "vif", "groups", "acf_pacf", "stationarity", "stl",
)  # fmt: skip
"""Today's tab order of the web app (a plain time-series panel keeps it)."""
_NOT_FOR: Final = {
    "images": "no variable lies on a 2-D grid (an image or a field)",
    "profiles": "no variable lies along a numeric axis that is not time",
    "map": "the data has no polygons (ring_x and ring_y on an area dimension)",
    "network": "no variable links a dimension to itself (a pair of dimensions X and X_)",
    "tree": "no variable gives the parent of each node of a tree",
    "circular": "no variable is an angle, a direction or a time of day",
    "points": "no two variables give the x and y positions of points",
}


@dataclass(frozen=True)
class Shape:
    """One data shape: its kind, the variables it shows, the dims it uses by role
    (``y``, ``x``, ``frame``, ``component``, ``channel``, ``area``, ``vertex``, ``node``,
    ``along``, ``item``, ``columns``, ``time``, ``point``, ``tip``) and per-kind facts."""

    kind: str
    vars: tuple[str, ...]
    dims: Mapping[str, str]
    extra: Mapping[str, Any] = field(default_factory=dict)
    score: float = 0.5
    label_plain: str = ""

    @property
    def section(self) -> str:
        return _PICTURE_SECTION[self.kind]


@dataclass(frozen=True)
class Structure:
    """What :func:`detect_structure` found (see the module docstring)."""

    shapes: tuple[Shape, ...]
    geometry_vars: frozenset[str]
    mask_vars: frozenset[str]
    label_vars: frozenset[str]
    time_dim: str | None
    sequence_dim: str | None
    unit_dim: str | None
    categorical_dims: tuple[str, ...]
    group_vars: Mapping[str, str]
    """Grouping variables (``var -> labelling dim or ''``): an index var with ``index_of``,
    or an integer var with at most 12 values."""
    recommended: tuple[str, ...]
    not_applicable: Mapping[str, str]
    dim_roles: Mapping[str, str]
    var_use: Mapping[str, str]

    @property
    def plain(self) -> bool:
        """A plain time-series dataset (only a ``timeseries`` shape): the explorer behaves as
        before the scientific sections."""
        return all(s.kind == "timeseries" for s in self.shapes)

    @property
    def kinds(self) -> list[str]:
        return list(dict.fromkeys(s.kind for s in self.shapes))

    def of(self, *kinds: str) -> list[Shape]:
        return [s for s in self.shapes if s.kind in kinds]

    def first(self, *kinds: str) -> Shape | None:
        return next((s for s in self.shapes if s.kind in kinds), None)

    @property
    def non_data(self) -> frozenset[str]:
        """Variables that are not data: geometry, masks, labels and grouping indices."""
        return self.geometry_vars | self.mask_vars | self.label_vars | frozenset(self.group_vars)


# ----------------------------------------------------------------------- dim helpers


def _coord(ds: xr.Dataset, dim: str) -> np.ndarray:
    if dim in ds.coords:
        return np.asarray(ds[dim].values)
    return np.arange(int(ds.sizes[dim]))


def _numeric_coord(ds: xr.Dataset, dim: str) -> bool:
    return _coord(ds, dim).dtype.kind in "iuf"


def _kind(ds: xr.Dataset, schema: DataSchema | None, dim: str) -> tuple[str, str]:
    if schema is not None and dim in schema.dims:
        d = schema.dims[dim]
        return str(d.kind), str(d.policy.value)
    v = _coord(ds, dim)
    kind = {"M": "time", "i": "integer", "u": "integer", "f": "float"}.get(v.dtype.kind)
    return kind or "categorical", "fixed"


def is_label_index(ds: xr.Dataset, schema: DataSchema | None, dim: str) -> bool:
    """An integer dim labelled ``0..n-1`` or ``1..n`` (or without a coord) whose name is not
    an axis word: an id (observation, pixel row, node), never an axis or time."""
    if _AXIS_WORDS.search(dim.lower()):
        return False
    v = _coord(ds, dim)
    if v.dtype.kind not in "iu" or v.size < 2:
        return False
    return bool(np.array_equal(v, np.arange(v.size)) or np.array_equal(v, np.arange(1, v.size + 1)))


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _drives(ds: xr.Dataset, schema: DataSchema | None, dim: str) -> bool:
    """Media or spend varies along ``dim`` (an MMM panel on a week number). A target alone
    does not tell: each row of a table of observations has a target (E02 ``home``)."""
    if schema is None:
        return False
    return any(
        v in ds.data_vars and dim in ds[v].dims and spec.role in _SPEND_ROLES
        for v, spec in schema.vars.items()
    )


def time_strength(ds: xr.Dataset, schema: DataSchema | None, dim: str) -> int:
    """2: a real time dim (``kind: time``, a datetime coord, or an ``extend`` dim with a
    calendar name such as ``week``); 1: an ordered sequence (``policy: extend``, numeric,
    strictly increasing); 0: not time.

    A label index (coords ``0..n-1`` or ``1..n``) with ``policy: extend`` is a sequence only
    when its name is a time word (``step``, ``t``) or media or spend vary along it.
    Else it is a list of observations (E81 ``report``): never time."""
    kind, policy = _kind(ds, schema, dim)
    if kind == "time" or _coord(ds, dim).dtype.kind == "M":
        return 2
    if policy != "extend" or not _numeric_coord(ds, dim):
        return 0
    v = _coord(ds, dim).astype(float)
    if v.size < 2 or not bool(np.all(np.diff(v) > 0)):
        return 0
    if _CALENDAR_NAMES.match(dim.lower()):
        return 2
    if not is_label_index(ds, schema, dim):
        return 1
    if set(_tokens(dim)) & _TIME_NAMES or _drives(ds, schema, dim):
        return 1
    return 0


def _matches(name: str, word: str) -> bool:
    low = name.lower()
    return low == word or low.startswith(word + "_") or low.endswith("_" + word)


def _spatial_pair(
    ds: xr.Dataset, schema: DataSchema | None, dims: Sequence[str], fallback: bool = True
) -> tuple[tuple[str, str], bool] | None:
    """``((y, x), named)`` of a variable's dims: a named spatial pair, else (``fallback``)
    its last two numeric dims when both have 8 or more levels, the aspect ratio is 4 or
    less and no dim of the variable is time (``named`` False). ``fallback`` is False for data
    with a time dim or with media, spend or control variables.

    The fallback never takes a panel (``spend(date, geo, channel)`` with integer codes) or a
    design matrix ``(obs, feature)`` as an image: name the dims, or give the ``y_dim`` and
    ``x_dim`` hints."""
    nd = [d for d in dims if _numeric_coord(ds, d)]
    for a, b in _SPATIAL_PAIRS:
        for d1 in nd:
            for d2 in nd:
                if d1 != d2 and _matches(d1, a) and _matches(d2, b):
                    return (d1, d2), True
    if fallback and len(dims) >= 2:
        y, x = dims[-2], dims[-1]
        ny, nx = int(ds.sizes[y]), int(ds.sizes[x])
        if (
            y in nd
            and x in nd
            and ny >= 8
            and nx >= 8
            and max(ny, nx) <= 4 * min(ny, nx)
            and not _AXIS_WORDS.search(y.lower())
            and not _AXIS_WORDS.search(x.lower())
            and all(time_strength(ds, schema, d) == 0 for d in dims)
        ):
            return (y, x), False
    return None


def angle_period(
    ds: xr.Dataset, schema: DataSchema | None, var: str, hints: StructureHints | None = None
) -> tuple[float, str] | None:
    """``(period, units)`` of a circular variable, or ``None``."""
    da = ds[var]
    if hints is not None and var in hints.angle_vars:
        return ANGLE_PERIODS[hints.angle_vars[var]]
    units = schema.vars[var].units if schema is not None and var in schema.vars else None
    units = units or da.attrs.get("units")
    found = ANGLE_PERIODS.get(str(units).strip().lower()) if units else None
    toks = set(_tokens(var))
    named = bool(toks & _ANGLE_TOKENS or _ANGLE_PHRASES.search(var.lower()))
    if toks & _NOT_ANGLE_TOKENS and not named:
        return None  # a latitude, a longitude or a temperature in degrees
    if found is None:
        period = da.attrs.get("period")
        circular = str(da.attrs.get("circular", "")).lower() in ("1", "true", "yes")
        if circular or period:
            try:
                found = (float(period), str(units or "")) if period else None
            except (TypeError, ValueError):
                found = None
            if found is None and circular:
                found = (360.0, "deg")
        if found is None:
            return None
    elif found[1] in ("h", "d", "month") and not named:
        return None  # hours and days are durations too: need the name to tell
    fin = np.asarray(da.values, dtype=float)
    fin = fin[np.isfinite(fin)]
    if fin.size < 3:
        return None
    period, canon = found
    lo, hi = float(fin.min()), float(fin.max())
    if canon == "d":
        ok = lo >= 0 and hi <= 367
    elif canon == "month":
        ok = lo >= 0 and hi <= 12
    else:
        ok = lo >= -period - 1e-9 and hi <= period + 1e-9
    if canon == "h" and hi <= 1.0:
        ok = False  # a share of the day, not hours
    return found if ok else None


# ------------------------------------------------------------------------ detection


def _strong_position(name: str, strong: frozenset[str]) -> bool:
    """``wcs_col``, ``site_lat``, ``longitude``: a name part that is only a position."""
    toks = _tokens(name)
    return bool(toks) and toks[-1] in strong


def _position_name(name: str, strong: frozenset[str], bare: frozenset[str]) -> bool:
    """A position name: a strong word (``lon``, ``wcs_col``), or ``x``/``y`` (``east``/
    ``north``) alone or with position parts only (``pos_x``, ``x_km``)."""
    if _strong_position(name, strong):
        return True
    toks = _tokens(name)
    hits = [t for t in toks if t in bare]
    return len(hits) == 1 and all(t in bare or t in _POS_PARTS for t in toks)


def _position_proof(ds: xr.Dataset, schema: DataSchema | None, dim: str, xv: str, yv: str) -> bool:
    """``x`` and ``y`` are positions (not a predictor and a response): both are integers (a
    lattice), both have length or angle units, or the dim names points (``site``)."""
    if all(ds[v].dtype.kind in "iu" for v in (xv, yv)):
        return True

    def units(v: str) -> str:
        spec = schema.vars.get(v) if schema is not None else None
        u = (spec.units if spec is not None else None) or ds[v].attrs.get("units") or ""
        return str(u).strip().lower()

    if all(units(v) in _LENGTH_UNITS for v in (xv, yv)):
        return True
    return bool(_POINT_DIMS.search(dim.lower()))


def _num(da: xr.DataArray) -> bool:
    return bool(np.issubdtype(da.dtype, np.number) or np.issubdtype(da.dtype, np.bool_))


def _plural(n: int, word: str) -> str:
    """``3 classes``, ``40 steps``; ``186 species`` and ``56 cells`` keep their form."""
    if n == 1 or len(word) < 2:
        return f"{n} {word}"
    low = word.lower()
    if low.endswith(("ss", "us", "x", "z", "ch", "sh")):
        return f"{n} {word}es"
    return f"{n} {word}" + ("" if low.endswith("s") else "s")


def detect_structure(
    ds: xr.Dataset,
    schema: DataSchema | None,
    hints: StructureHints | None = None,
    *,
    time_dim: str | None = None,
) -> Structure:
    """The data shapes of ``ds`` (see the module docstring). ``time_dim`` (the request's)
    names the time dim and wins over the rules."""
    hints = hints or StructureHints()
    roles: dict[str, Role] = (
        {n: v.role for n, v in schema.vars.items()} if schema is not None else {}
    )
    data_vars = [str(v) for v in ds.data_vars]
    num = [v for v in data_vars if _num(ds[v])]
    sizes = {str(d): int(n) for d, n in ds.sizes.items()}
    dims = list(sizes)
    shapes: list[Shape] = []
    geometry: set[str] = set()
    labels: set[str] = set()
    group_vars: dict[str, str] = {}
    consumed: set[str] = set()
    masks = {v for v in data_vars if roles.get(v) is Role.MASK}
    index_vars = {v for v in data_vars if roles.get(v) is Role.INDEX}
    dim_roles: dict[str, str] = {}

    mmm = any(roles.get(v) in (Role.MEDIA, Role.SPEND, Role.CONTROL) for v in data_vars)
    strength = {d: time_strength(ds, schema, d) for d in dims}
    hinted_time = time_dim or hints.time_dim
    if hinted_time is not None and hinted_time in sizes:
        strength[hinted_time] = 2

    def role_of(v: str) -> str | None:
        r = roles.get(v)
        return r.value if r is not None else None

    # 1. tree: parent(node) in [-1, n) with a root
    node_hint = hints.node_dim
    for v in num:
        da = ds[v]
        if v != "parent" or da.ndim != 1 or da.dtype.kind not in "iu":
            continue
        nd = str(da.dims[0])
        if node_hint is not None and nd != node_hint:
            continue
        p = np.asarray(da.values).astype(np.int64)
        idx = np.arange(p.size)
        if p.size == 0 or p.min() < -1 or p.max() >= p.size:
            continue
        roots = int(np.sum((p == -1) | (p == idx)))
        if roots < 1:
            continue
        geo = [g for g in _GEOMETRY_TREE if g in ds.data_vars and ds[g].dims == (nd,)]
        geometry.update(geo)
        consumed.update(geo)
        tip_dims = [
            str(ds[t].dims[0])
            for t in num
            if t not in consumed and ds[t].ndim == 1 and str(ds[t].dims[0]) != nd
        ]
        tip_dim = max(set(tip_dims), key=tip_dims.count) if tip_dims else None
        traits = tuple(
            t for t in num if t not in consumed and ds[t].dims == ((tip_dim,) if tip_dim else ())
        )
        n_tips = int(np.asarray(ds["is_tip"].values).sum()) if "is_tip" in geo else None
        dim_roles[nd] = "node"
        if tip_dim:
            dim_roles[tip_dim] = "tip"
        shapes.append(
            Shape(
                "tree",
                traits,
                {"node": nd, **({"tip": tip_dim} if tip_dim else {})},
                {"geometry": tuple(geo), "n_tips": n_tips, "roots": roots},
                0.95,
                f"tree: {_plural(sizes[nd], 'node')}"
                + (f", {n_tips} tips" if n_tips is not None else "")
                + (f", {len(traits)} traits on {tip_dim}" if traits else ""),
            )
        )
        break

    # 2. areal map: ring_x(area, vertex), ring_y(area, vertex) (+ neighbours (area, area_))
    if "ring_x" in num and "ring_y" in num:
        rx, ry = ds["ring_x"], ds["ring_y"]
        if rx.dims == ry.dims and rx.ndim == 2:
            area, vert = (str(d) for d in rx.dims)
            nb = [v for v in num if ds[v].dims == (area, area + "_")]
            geo = ["ring_x", "ring_y", *nb]
            geometry.update(geo)
            consumed.update(geo)
            vals = tuple(
                v
                for v in num
                if v not in consumed
                and v not in masks
                and v not in index_vars
                and area in ds[v].dims
                and ds[v].ndim <= 2
            )
            dim_roles.update({area: "area", vert: "vertex"})
            if area + "_" in sizes:
                dim_roles[area + "_"] = "pair"
            shapes.append(
                Shape(
                    "areal_map",
                    vals,
                    {"area": area, "vertex": vert},
                    {"neighbours": nb[0] if nb else None, "rings": ("ring_x", "ring_y")},
                    0.95,
                    f"map: {_plural(sizes[area], 'area')}"
                    + (", with neighbours" if nb else "")
                    + (f", {len(vals)} variables" if vals else ""),
                )
            )

    # 3. networks: a numeric var on (X, X_)
    for v in num:
        if v in consumed or v in masks:
            continue
        da = ds[v]
        vdims = [str(d) for d in da.dims]
        for d in vdims:
            if node_hint is not None and d != node_hint:
                continue
            if d + "_" in vdims and sizes[d] == sizes[d + "_"]:
                facets = tuple(e for e in vdims if e not in (d, d + "_"))
                attrs = tuple(
                    m for m in num if ds[m].dims == (d,) and m not in consumed and m not in masks
                )
                consumed.add(v)
                if roles.get(v) is Role.CONSTANT:
                    geometry.add(v)
                dim_roles.setdefault(d, "node")
                dim_roles.setdefault(d + "_", "pair")
                shapes.append(
                    Shape(
                        "network",
                        (v,),
                        {"node": d},
                        {"facets": facets, "node_attrs": attrs},
                        0.9 if roles.get(v) is not Role.CONSTANT else 0.85,
                        f"network: {v} links {_plural(sizes[d], d)}"
                        + (f", one per {', '.join(facets)}" if facets else ""),
                    )
                )
                break

    # 4. images and vector fields (the unnamed fallback: no time dim, no MMM roles)
    fallback_ok = not any(strength.values()) and not mmm
    image_groups: dict[tuple[Any, ...], list[str]] = {}
    image_facts: dict[tuple[Any, ...], dict[str, Any]] = {}
    for v in num:
        if v in consumed or v in masks or v in index_vars:
            continue
        da = ds[v]
        vdims = [str(d) for d in da.dims]
        if da.ndim < 2:
            continue
        named = False
        if hints.y_dim is not None and hints.x_dim is not None:
            if hints.y_dim not in vdims or hints.x_dim not in vdims:
                continue
            pair: tuple[str, str] | None = (hints.y_dim, hints.x_dim)
            named = True
        else:
            found = _spatial_pair(ds, schema, vdims, fallback_ok)
            pair, named = found if found is not None else (None, False)
        if pair is None:
            continue
        y, x = pair
        rest = [d for d in vdims if d not in pair]
        if not rest and (strength[y] or strength[x]):
            continue  # a 2-D variable along time: a profile with frames, not an image
        comp = next(
            (
                d
                for d in rest
                if sizes[d] == 2
                and (
                    d.lower().startswith("comp")
                    or {str(c).lower() for c in _coord(ds, d)} in _COMPONENT_LABELS
                )
            ),
            None,
        )
        chan = next(
            (d for d in rest if sizes[d] in (3, 4) and d.lower() in _CHANNEL_NAMES and d != comp),
            None,
        )
        rest2 = [d for d in rest if d not in (comp, chan)]
        frame = None
        if hints.frame_dim is not None and hints.frame_dim in rest2:
            frame = hints.frame_dim
        elif rest2:
            frame = sorted(rest2, key=lambda d: (-strength[d], -sizes[d]))[0]
        facets = tuple(d for d in rest2 if d != frame)
        mask = next(
            (
                m
                for m in sorted(masks)
                if set(ds[m].dims) <= set(vdims) and {y, x} <= set(ds[m].dims)
            ),
            None,
        )
        key = (y, x, frame, comp, chan, facets)
        image_groups.setdefault(key, []).append(v)
        image_facts[key] = {"named": named, "mask": mask}
        consumed.add(v)
    for (y, x, frame, comp, chan, facets), vs in image_groups.items():
        facts = image_facts[(y, x, frame, comp, chan, facets)]
        kind = "vector_field" if comp else "image"
        dim_roles.update({y: "y", x: "x"})
        if frame:
            dim_roles.setdefault(frame, "frame")
        if comp:
            dim_roles[comp] = "component"
        if chan:
            dim_roles[chan] = "channel"
        group_coord = None
        if frame is not None:
            for cname, coord in ds.coords.items():
                cn = str(cname)
                if cn != frame and coord.dims == (frame,):
                    cvals = np.asarray(coord.values)
                    if 2 <= np.unique(cvals.astype(str)).size <= 12:
                        group_coord = cn
                        break
        dims_map = {"y": y, "x": x}
        for role, d in (("frame", frame), ("component", comp), ("channel", chan)):
            if d is not None:
                dims_map[role] = d
        label = f"{'vector field' if comp else 'image'}: {sizes[y]} x {sizes[x]} cells"
        if frame is not None:
            label += f", {sizes[frame]} {frame}"
        shapes.append(
            Shape(
                kind,
                tuple(vs),
                dims_map,
                {
                    "facets": facets,
                    "mask": facts["mask"],
                    "named": facts["named"],
                    "group_coord": group_coord,
                },
                0.9 if facts["named"] else 0.6,
                label,
            )
        )
        if facts["mask"]:
            masks.add(facts["mask"])

    # 5. circular variables
    circ: list[tuple[str, float, str]] = []
    for v in num:
        if v in consumed or v in masks or v in index_vars:
            continue
        found_a = angle_period(ds, schema, v, hints)
        if found_a is not None:
            circ.append((v, *found_a))
    if circ:
        for v, _p, _u in circ:
            consumed.add(v)
        by_dims: dict[tuple[str, ...], list[tuple[str, float, str]]] = {}
        for item in circ:
            by_dims.setdefault(tuple(str(d) for d in ds[item[0]].dims), []).append(item)
        for vdims_t, items in by_dims.items():
            group_var: str | None = None
            group_dim: str | None = None
            for m, spec in schema.vars.items() if schema is not None else []:
                if spec.index_of and m in ds.data_vars and ds[m].dims == ds[items[0][0]].dims:
                    group_var, group_dim = m, spec.index_of
                    break
            if group_var is None:
                for m in num:
                    if (
                        m in consumed
                        or ds[m].dims != ds[items[0][0]].dims
                        or ds[m].dtype.kind not in "iu"
                    ):
                        continue
                    uniq = np.unique(np.asarray(ds[m].values))
                    if 2 <= uniq.size <= 12:
                        group_var = m
                        break
            if group_var is not None:
                group_vars[group_var] = group_dim or ""
                consumed.add(group_var)
            for d in vdims_t:
                if is_label_index(ds, schema, d):
                    dim_roles.setdefault(d, "index")
            shapes.append(
                Shape(
                    "circular",
                    tuple(i[0] for i in items),
                    {"item": vdims_t[0]} if len(vdims_t) == 1 else {},
                    {
                        "periods": {i[0]: i[1] for i in items},
                        "units": {i[0]: i[2] for i in items},
                        "group_var": group_var,
                        "group_dim": group_dim,
                    },
                    0.85,
                    "circular: "
                    + ", ".join(f"{i[0]} ({i[2]})" for i in items)
                    + (f", by {group_dim or group_var}" if group_var else ""),
                )
            )

    # 6. points: two numeric vars with x-like and y-like names on one dim (>= 10 levels) that
    #    is not time; a target or a driver is never a position
    for d in dims:
        if strength[d]:
            continue
        one = [v for v in num if ds[v].dims == (d,) and v not in consumed and v not in masks]
        cand = [v for v in one if roles.get(v) not in _DRIVER_ROLES]
        xs = [v for v in cand if _position_name(v, _X_STRONG, _X_BARE)]
        ys = [v for v in cand if _position_name(v, _Y_STRONG, _Y_BARE)]
        pairs = [
            (xv, yv)
            for xv in xs
            for yv in ys
            if _strong_position(xv, _X_STRONG)
            or _strong_position(yv, _Y_STRONG)
            or _position_proof(ds, schema, d, xv, yv)
        ]
        if not pairs or sizes[d] < 10:
            continue
        xv, yv = pairs[0]
        lattice = all(ds[v].dtype.kind in "iu" for v in (xv, yv))
        lab = next((t for t in _LAB_TRIPLES if all(c in one for c in t)), None)
        answers = next(
            (
                v
                for v in data_vars
                if v not in num
                and ds[v].ndim == 2
                and d in ds[v].dims
                and ds[v].dtype.kind in "UOS"
            ),
            None,
        )
        geometry.update((xv, yv))
        consumed.update((xv, yv))
        others = tuple(v for v in one if v not in (xv, yv))
        consumed.update(others)
        dim_roles[d] = "item"
        shapes.append(
            Shape(
                "points",
                others,
                {"point": d},
                {"x": xv, "y": yv, "lattice": lattice, "lab": lab, "answers": answers},
                0.8,
                f"points: {_plural(sizes[d], d)} at ({xv}, {yv})"
                + (" on a lattice" if lattice else "")
                + (", with CIE Lab colours" if lab else "")
                + (f", answers in {answers}" if answers else ""),
            )
        )
        break

    # 7. profiles: variables along a numeric non-time axis
    map_vertex = {s.dims["vertex"] for s in shapes if s.kind == "areal_map"}
    for d in dims:
        if (
            not _numeric_coord(ds, d)
            or d not in ds.coords
            or strength[d]
            or sizes[d] < 5
            or d in map_vertex
            or dim_roles.get(d) in ("y", "x", "node", "pair", "area", "item", "tip")
        ):
            continue
        axis_word = bool(_AXIS_WORDS.search(d.lower()))
        if not axis_word and (is_label_index(ds, schema, d) or set(_tokens(d)) & _TIME_NAMES):
            continue
        pvs = tuple(
            v
            for v in num
            if v not in consumed
            and v not in masks
            and v not in index_vars
            and d in ds[v].dims
            and ds[v].ndim <= 2
        )
        if not pvs:
            continue
        if (
            not axis_word
            and _coord(ds, d).dtype.kind in "iu"
            and (mmm or any(strength[str(e)] == 2 for v in pvs for e in ds[v].dims))
        ):
            continue  # integer codes (a geo or a channel id of a panel), not an axis
        frames = sorted({str(e) for v in pvs for e in ds[v].dims if e != d})
        consumed.update(pvs)
        dim_roles[d] = "along"
        for f in frames:
            dim_roles.setdefault(f, "frame")
        fin = np.concatenate([np.asarray(ds[v].values, dtype=float).ravel() for v in pvs])
        fin = fin[np.isfinite(fin)]
        log_y = bool(fin.size and fin.min() > 0 and fin.max() / fin.min() > 1e3)
        shapes.append(
            Shape(
                "profile",
                pvs,
                {"along": d, **({"frame": frames[0]} if frames else {})},
                {"log_x": bool(_LOG_X_WORDS.match(d.lower())), "log_y": log_y},
                0.75 if axis_word or _kind(ds, schema, d)[0] == "float" else 0.6,
                f"profile: {', '.join(pvs[:3])}{' ...' if len(pvs) > 3 else ''} along {d} "
                f"({sizes[d]} points)" + (f", one curve per {frames[0]}" if frames else ""),
            )
        )

    # 8. table: (item, columns) with a small categorical columns dim
    for v in num:
        if v in consumed or v in masks or ds[v].ndim != 2:
            continue
        d0, d1 = (str(d) for d in ds[v].dims)
        for item_dim, col in ((d0, d1), (d1, d0)):
            ckind, _ = _kind(ds, schema, col)
            if (
                ckind == "categorical"
                and sizes[col] <= 64
                and sizes[item_dim] >= 30
                and not strength[item_dim]
                and not strength[col]
            ):
                consumed.add(v)
                dim_roles.setdefault(item_dim, "item")
                dim_roles.setdefault(col, "columns")
                shapes.append(
                    Shape(
                        "table",
                        (v,),
                        {"item": item_dim, "columns": col},
                        {},
                        0.5,
                        f"table: {v} with {_plural(sizes[item_dim], item_dim)}"
                        f" x {sizes[col]} {col}",
                    )
                )
                break

    # 9. time series or sequence
    t_dim: str | None = None
    seq_dim: str | None = None
    timed = [d for d in dims if strength[d]]
    if timed:
        best = max(timed, key=lambda d: (strength[d], sizes[d]))
        if strength[best] == 2:
            t_dim = best
        else:
            seq_dim = best
        tv = tuple(
            v
            for v in num
            if best in ds[v].dims
            and v not in geometry
            and v not in masks
            and v not in index_vars
            and v not in group_vars
        )
        reduce = sorted(
            {
                s.dims[r]
                for s in shapes
                if s.kind in ("image", "vector_field") and s.dims.get("frame") == best
                for r in ("y", "x")
            }
        )
        if tv:
            dim_roles[best] = "time" if t_dim else "sequence"
            non_time = [s for s in shapes if s.kind != "timeseries"]
            kind = "timeseries" if t_dim else "sequence"
            shapes.append(
                Shape(
                    kind,
                    tv,
                    {"time": best},
                    {"reduce": tuple(reduce)},
                    1.0 if not non_time and t_dim else (0.6 if t_dim else 0.5),
                    f"{'time series' if t_dim else 'sequence'}: {_plural(sizes[best], best)}"
                    + (f" ({' and '.join(reduce)} reduced)" if reduce else ""),
                )
            )

    # labels: string vars and index vars that are not data
    for v in data_vars:
        if v in geometry or v in masks:
            continue
        if v in index_vars or (
            not _num(ds[v]) and not any(v == s.extra.get("answers") for s in shapes)
        ):
            labels.add(v)

    shapes.sort(key=lambda s: -s.score)
    for d in dims:
        if d in dim_roles:
            continue
        kind, _ = _kind(ds, schema, d)
        if is_label_index(ds, schema, d):
            dim_roles[d] = "index"
        elif kind == "categorical":
            dim_roles[d] = "category"
        else:
            dim_roles[d] = "other"
    for d in dims:  # the pair dim of a network node dim
        if d.endswith("_") and d[:-1] in sizes and dim_roles.get(d) in ("category", "other"):
            dim_roles[d] = "pair"

    excluded = {"y", "x", "node", "pair", "area", "vertex", "item", "index", "tip", "component"}
    non_data = geometry | masks | labels | set(group_vars)
    # a dim that only splits a network into layers (E21 homophily per covariate) has no
    # groups of values to compare
    net_vars = {v for s in shapes if s.kind == "network" for v in s.vars}
    categorical = tuple(
        d
        for d in dims
        if _kind(ds, schema, d)[0] == "categorical"
        and 2 <= sizes[d] <= 50
        and dim_roles.get(d) not in excluded
        and not strength[d]
        and any(d in ds[v].dims for v in num if v not in non_data and v not in net_vars)
    )
    unit_dim = _unit_dim(ds, num, non_data, shapes)
    var_use: dict[str, str] = {}
    for v in data_vars:
        if v in geometry:
            var_use[v] = "geometry"
        elif v in masks:
            var_use[v] = "mask"
        elif v in index_vars or v in group_vars:
            var_use[v] = "index"
        elif v in labels:
            var_use[v] = "label"
        elif roles.get(v) is Role.WEIGHT:
            var_use[v] = "weight"
        else:
            var_use[v] = "data"
    structure = Structure(
        shapes=tuple(shapes),
        geometry_vars=frozenset(geometry),
        mask_vars=frozenset(masks),
        label_vars=frozenset(labels),
        time_dim=t_dim,
        sequence_dim=seq_dim,
        unit_dim=unit_dim,
        categorical_dims=categorical,
        group_vars=group_vars,
        recommended=(),
        not_applicable={},
        dim_roles=dim_roles,
        var_use=var_use,
    )
    na = _not_applicable(structure, ds, schema, roles, num)
    rec = _recommended(structure, na, len(dims))
    return Structure(
        **{
            **structure.__dict__,
            "recommended": rec,
            "not_applicable": na,
        }
    )


def _unit_dim(
    ds: xr.Dataset, num: Sequence[str], non_data: set[str], shapes: Sequence[Shape]
) -> str | None:
    """The dim whose levels are the rows of tabular statistics: the table's item dim, else
    the dim with the most 1-D numeric data variables."""
    table = next((s for s in shapes if s.kind == "table"), None)
    if table is not None:
        return table.dims["item"]
    counts: dict[str, int] = {}
    for v in num:
        if v in non_data or ds[v].ndim != 1:
            continue
        d = str(ds[v].dims[0])
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda d: (counts[d], -list(ds.sizes).index(d)))


def _not_applicable(
    s: Structure,
    ds: xr.Dataset,
    schema: DataSchema | None,
    roles: Mapping[str, Role],
    num: Sequence[str],
) -> dict[str, str]:
    out: dict[str, str] = {}
    kinds = set(s.kinds)
    need = {
        "images": {"image", "vector_field"},
        "profiles": {"profile"},
        "map": {"areal_map"},
        "network": {"network"},
        "tree": {"tree"},
        "circular": {"circular"},
        "points": {"points"},
    }
    for section, ks in need.items():
        if not kinds & ks:
            out[section] = _NOT_FOR[section]
    if s.time_dim is None and s.sequence_dim is None:
        out["timeseries"] = "the data has no time dimension"
    if s.time_dim is None:
        for section in _TIME_SECTIONS:
            out[section] = (
                "the data has no time dimension"
                if s.sequence_dim is None
                else f"{s.sequence_dim} is an ordered sequence, not time"
            )
    if not s.categorical_dims and not s.group_vars:
        out["groups"] = "the data has no dimension or variable with groups to compare"
    drivers = [
        v
        for v in num
        if v not in s.non_data
        and roles.get(v, Role.COVARIATE)
        in (Role.MEDIA, Role.SPEND, Role.COVARIATE, Role.CONTROL, Role.EXPOSURE)
        and roles.get(v) is not Role.TARGET
    ]
    split = s.time_dim is not None or s.sequence_dim is not None  # small categories split
    if len(drivers) < 2 and not split and not any(sh.kind == "table" for sh in s.shapes):
        out["vif"] = "collinearity needs at least two drivers"
    return out


def _recommended(s: Structure, na: Mapping[str, str], ndims: int) -> tuple[str, ...]:
    """Sections in order of relevance (a plain time-series panel keeps today's order)."""
    if s.plain:
        return tuple([x for x in _DATA_ORDER if x not in na] + ["structure"])

    order: list[str] = ["overview"]
    if ndims >= 3 or not s.plain:
        order.append("structure")
    for shape in s.shapes:
        sec = shape.section
        if sec in ("timeseries", "correlation") or sec in order:
            continue
        order.append(sec)
    order += ["quality", "distributions"]
    if s.time_dim is not None or s.sequence_dim is not None:
        order.append("timeseries")
    if s.time_dim is not None:
        order += ["ccf", "acf_pacf", "stationarity", "stl"]
    order += ["correlation", "groups", "vif", "missingness", "outliers"]
    if "structure" not in order:
        order.append("structure")
    seen: list[str] = []
    for x in order:
        if x not in seen and x not in na and x in DATA_SECTIONS:
            seen.append(x)
    return tuple(seen)


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(description="Print the data shapes the Data Explorer finds.")
    p.add_argument("path", help="a .nc file (a tree file: give --node)")
    p.add_argument("--node", default=None, help="a node of a tree dataset (/image)")
    a = p.parse_args(argv)
    if a.node:
        tree = xr.open_datatree(a.path, engine="h5netcdf")
        ds = tree[a.node].to_dataset().load()
        schema = None
        raw = tree.attrs.get("mapi_tree_schema")
        if raw:
            from mapi_spec.tree import TreeSchema

            schema = TreeSchema.model_validate_json(raw).nodes.get(a.node)
    else:
        ds = xr.open_dataset(a.path, engine="h5netcdf").load()
        raw = ds.attrs.get("mapi_schema")
        schema = DataSchema.model_validate_json(raw) if raw else None
    s = detect_structure(ds, schema)
    for shape in s.shapes:
        print(f"{shape.kind:13s} {shape.score:.2f} {shape.label_plain}")
        print(f"              vars={list(shape.vars)} dims={dict(shape.dims)}")
    print("geometry:", sorted(s.geometry_vars), "masks:", sorted(s.mask_vars))
    print("time:", s.time_dim, "sequence:", s.sequence_dim, "unit:", s.unit_dim)
    print("categorical:", list(s.categorical_dims), "group vars:", dict(s.group_vars))
    print("order:", ", ".join(s.recommended))
    print("not applicable:", json.dumps(dict(s.not_applicable), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
