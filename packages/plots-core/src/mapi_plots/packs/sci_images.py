"""Scientific-model pack, image and spectrum recipes (part of the ``sci`` pack).

Plotly figures built on the field helpers of :mod:`mapi_plots.packs.sci`:

* ``sci.image_residuals``: observed image | model mean | residual | normalised residual
  ``(obs - mean) / sqrt(sd² + σ²)`` heatmaps, with a histogram of the normalised residuals
  against N(0, 1);
* ``sci.ppc_image_grid``: the observed image next to ``n_draws`` predictive draws on one
  colour scale; ``shuffle`` hides which panel is real (the caption gives the answer);
* ``sci.swatches``: colour swatches of an sRGB output ``(…, item, rgb)`` in [0, 1]: observed,
  mean and a few draws, as a list or painted as a map when the item dim has ``row``/``col``
  coords;
* ``sci.power_spectrum``: a binned spectrum ``(…, bin)`` against a multipole or frequency
  coord with posterior bands, an optional theory curve and observed points with error bars.

Image inputs: draws ``(chain, draw | sample, [frame], y, x)`` in ``posterior_predictive``,
``predictions`` or ``posterior`` (in that order) and the observed image ``(…, y, x)`` in
``observed_data`` (same name, ``observed_var``, or the only observed variable). The grid dims
default to the output's ``grid`` declaration, as for ``sci.field_map``.
"""

from __future__ import annotations

import math
import warnings
from typing import Any, Literal

import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field, field_validator

from mapi_plots.packs.sci import (
    FIELD_GROUPS,
    OBS_GROUPS,
    ROW_BUDGET,
    GroupName,
    _axis_id,
    _band_text,
    _budget,
    _coords,
    _domains,
    _field_figure,
    _field_input,
    _FieldInput,
    _FieldSettings,
    _find_draws,
    _frame_levels,
    _frame_names,
    _label,
    _numeric_coord,
    _observed,
    _only,
    _Panel,
    _range,
    _Raster,
    _scale,
    _select,
    _settings_from_grid,
    _size_note,
    _thin,
    _to_log,
    block_factor,
    block_mean,
)
from mapi_plots.palette import BAND, EMPTY, ESTIMATE, MUTED, OBSERVED, REFERENCE
from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    recipe,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    check_levels,
    coord_values,
    interval_columns,
    observed_name,
    pct,
    sample_dims,
    stack_samples,
)

__all__ = [
    "ImageResidualsSettings",
    "PowerSpectrumSettings",
    "PpcImageGridSettings",
    "SwatchesSettings",
    "image_residuals",
    "power_spectrum",
    "ppc_image_grid",
    "sci_image_recipes",
    "swatches",
]

#: images: the predictive draws first (they hold the noise), then the noise-free outputs
IMAGE_GROUPS = ("posterior_predictive", "predictions", "posterior")
SPECTRUM_GROUPS = ("posterior", "predictions", "posterior_predictive")
#: where a noise (sigma), theory or probability variable is looked up
AUX_GROUPS = (
    "constant_data",
    "observed_data",
    "posterior",
    "predictions",
    "posterior_predictive",
)
HIST_EDGES = np.linspace(-4.5, 4.5, 37)  # 0.25-wide bins of the normalised residual


# ------------------------------------------------------------------------ image inputs


class _ImageSettings(_FieldSettings):
    group: GroupName = Field(
        "auto",
        description="Group to read (auto: posterior_predictive, predictions, posterior)",
    )
    observed_var: str | None = Field(None, description="Observed image (default: same name)")


def _image_input(inp: RecipeInput, s: _ImageSettings) -> tuple[_FieldInput, xr.DataArray, str]:
    """``(field input, observed (…, y, x) on the field's coords, observed name)``."""
    need = [d for d in (s.y, s.x, s.frame) if d]
    da, group = _find_draws(inp, s.var, s.group, IMAGE_GROUPS, need=need, min_dims=2)
    fi = _field_input(inp, s.model_copy(update={"var": str(da.name), "group": group}))
    keep = [d for d in (fi.frame, fi.y, fi.x) if d]
    ref = fi.st.isel({SAMPLE: 0}, drop=True)
    obs, name = _observed(inp, fi.var, s.observed_var, ref, keep, s.select)
    if obs is None or name is None:
        raise RecipeDataError(
            f"no observed image for {fi.var!r}: expected a variable with the dims "
            f"{', '.join(keep)} in observed_data (pass observed_var=)"
        )
    return fi, obs, name


def _aux(inp: RecipeInput, name: str) -> xr.DataArray | None:
    for g in AUX_GROUPS:
        ds = inp.find(g)
        if ds is not None and name in ds.data_vars:
            return ds[name]
    return None


def _sigma2(
    inp: RecipeInput, s: ImageResidualsSettings, ref: xr.DataArray, group: str
) -> tuple[np.ndarray | None, str | None]:
    """The noise variance on ``ref``'s ``(…, y, x)`` grid (None: no noise term) and its name.

    Without ``sigma`` set, a variable named ``sigma`` is used for noise-free images only:
    predictive draws already hold the noise.
    """
    if isinstance(s.sigma, int | float):
        v = float(s.sigma)
        var2 = {"sd": v * v, "variance": v, "inverse_variance": 1 / v if v > 0 else np.nan}
        return np.full(ref.shape, var2[s.sigma_kind]), f"{v:g}"
    if s.sigma is None and group == "posterior_predictive":
        return None, None
    name = s.sigma or "sigma"
    da = _aux(inp, name)
    if da is None:
        if s.sigma is not None:
            raise RecipeDataError(f"no sigma variable {name!r} in {', '.join(AUX_GROUPS)}")
        return None, None
    da = _select(da, {d: v for d, v in (s.select or {}).items() if d in da.dims})
    with np.errstate(all="ignore"):
        if s.sigma_kind == "sd":
            da = da**2
        elif s.sigma_kind == "inverse_variance":
            da = xr.where(da > 0, 1.0 / da, np.nan)
    if sample_dims(da):
        da = stack_samples(da).mean(SAMPLE)  # the expected noise variance
    keep = [str(d) for d in ref.dims]
    extra = [str(d) for d in da.dims if d not in keep]
    if any(da.sizes[d] > 1 for d in extra):
        if s.sigma is None:
            return None, None  # an unrelated variable called sigma
        raise RecipeDataError(f"sigma {name!r} has dims the image does not have: {extra}")
    da = da.squeeze(extra, drop=True) if extra else da
    for d in da.dims:
        if d in ref.coords and d in da.coords:
            da = da.reindex({d: ref[d].values})
    out = da.broadcast_like(ref).transpose(*keep)
    return np.asarray(out.values, dtype=float), name


# ----------------------------------------------------------------------- image residuals


class ImageResidualsSettings(_ImageSettings):
    sigma: str | float | None = Field(
        None,
        description="Noise of the data: a variable (posterior draws or a (y, x) map) or a "
        "number. Default: a variable named sigma, if there is one",
    )
    sigma_kind: Literal["sd", "variance", "inverse_variance"] = Field(
        "sd", description="What the sigma values are"
    )
    columns: int = Field(2, ge=1, le=4)
    hist_height: int = Field(200, ge=0, le=800, description="Height of the histogram (0: none)")


def _stack_below(fig: dict[str, Any], extra_px: int) -> float:
    """Make room for a panel of ``extra_px`` under the heatmap grid; returns the fraction of
    the plot area the new panel and its gap take (the bottom of paper y)."""
    layout = fig["layout"]
    m = layout["margin"]
    plot_h = layout["height"] - m["t"] - m["b"]
    gap = 70
    layout["height"] += extra_px + gap
    f = (extra_px + gap) / (plot_h + extra_px + gap)

    def move(y: float) -> float:
        return round(f + y * (1 - f), 4)

    for key, ax in layout.items():
        if key.startswith("yaxis") and isinstance(ax, dict) and "domain" in ax:
            ax["domain"] = [move(ax["domain"][0]), move(ax["domain"][1])]
    for ann in layout.get("annotations", []):
        ann["y"] = move(ann["y"])
    for tr in fig["data"]:
        cb = tr.get("colorbar")
        if cb:
            cb["y"] = move(cb["y"])
            cb["len"] = round(cb["len"] * (1 - f), 4)
    return round(extra_px / (plot_h + extra_px + gap), 4)


@recipe(
    "sci.image_residuals",
    title="Image residuals",
    settings=ImageResidualsSettings,
    persona="both",
    requires_groups=["observed_data"],
    summary="The observed image, the model's image, the difference between them and the "
    "difference in units of the uncertainty, with a histogram to compare with a normal "
    "distribution.",
    tags=["sci", "image", "diagnostic"],
)
def image_residuals(inp: RecipeInput, s: ImageResidualsSettings) -> Figure:
    """Observed | mean | residual | normalised residual heatmaps and a residual histogram."""
    fi, obs, obs_name = _image_input(inp, s)
    colormap, log, origin = _settings_from_grid(s, fi.grid)
    st = fi.st
    sig2, sig_name = _sigma2(inp, s, obs, fi.group)
    panels = [
        _Panel("observed", "Observed", "value"),
        _Panel("mean", "Model mean", "value"),
        _Panel("resid", "Residual", "sd"),
        _Panel("norm", "Normalised residual", "z"),
    ]
    levels = _frame_levels(st, fi.frame, s.max_frames)
    ny, nx = st.sizes[fi.y], st.sizes[fi.x]
    k = block_factor(ny, nx, _budget(s.max_cells, 2 + len(panels) * len(levels)))
    values: dict[str, list[np.ndarray]] = {p.key: [] for p in panels}
    norms: list[np.ndarray] = []
    masked = False
    for lv in levels:
        sub = st.isel({fi.frame: lv}) if fi.frame else st
        draws = np.asarray(sub.values, dtype=float)
        o = np.asarray((obs.isel({fi.frame: lv}) if fi.frame else obs).values, dtype=float)
        s2 = (sig2[lv] if fi.frame else sig2) if sig2 is not None else 0.0
        if log:
            draws, o = _to_log(draws), _to_log(o)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mean = np.nanmean(draws, axis=0)
            sd = np.nanstd(draws, axis=0, ddof=1 if draws.shape[0] > 1 else 0)
            resid = o - mean
            scale = np.sqrt(sd**2 + s2)
            norm = np.where(scale > 0, resid / np.where(scale > 0, scale, 1.0), np.nan)
        masked = masked or bool((~np.isfinite(resid)).any())
        norms.append(norm[np.isfinite(norm)])
        got = {"observed": o, "mean": mean, "resid": resid, "norm": norm}
        for p in panels:
            values[p.key].append(block_mean(got[p.key], k))
    xs, ys, xl, yl = _coords(fi, inp, k)
    r = _Raster(
        panels=panels,
        values=values,
        xs=xs,
        ys=ys,
        x_label=xl,
        y_label=yl,
        frame_label=inp.dim_label(fi.frame) if fi.frame else None,
        frames=_frame_names(fi, levels),
        block=k,
        n_draws=st.sizes[SAMPLE],
        shape=(ny, nx),
        masked=masked,
    )
    fig, datasets = _field_figure(
        r,
        colormap=colormap,
        origin=origin,
        aspect=s.aspect,
        columns=s.columns,
        width=s.width,
        height=s.height,
    )
    # residuals: a diverging scale around zero; the normalised one at least ±3
    res_rng = _range(values["resid"], symmetric=True)
    hm = fig["data"]
    hm[2]["colorscale"] = _scale("diverging")
    if res_rng is not None:
        hm[2]["zmin"], hm[2]["zmax"] = res_rng
    z_rng = _range(values["norm"], symmetric=True)
    zm = max(3.0, z_rng[1] if z_rng else 3.0)
    hm[3]["zmin"], hm[3]["zmax"] = -zm, zm

    pooled = np.concatenate(norms) if norms else np.array([])
    if s.hist_height > 0:
        width = float(HIST_EDGES[1] - HIST_EDGES[0])
        centres = (HIST_EDGES[:-1] + HIST_EDGES[1:]) / 2
        hist: dict[str, np.ndarray] = {"centre": centres}
        for j, z in enumerate(norms):
            counts, _ = np.histogram(np.clip(z, HIST_EDGES[0], HIST_EDGES[-1]), HIST_EDGES)
            hist[f"density__{j}"] = counts / max(1, z.size) / width
        hist["normal"] = np.exp(-(centres**2) / 2) / math.sqrt(2 * math.pi)
        datasets["hist"] = pd.DataFrame({c: v.astype(np.float32) for c, v in hist.items()})
        top = _stack_below(fig, s.hist_height)
        aid = _axis_id(len(panels))
        n_heat = len(fig["data"])
        fig["data"] += [
            {
                "type": "bar",
                "x": {"$ref": "hist.centre"},
                "y": {"$ref": "hist.density__0"},
                "xaxis": f"x{aid}",
                "yaxis": f"y{aid}",
                "marker": {"color": BAND},
                "opacity": 0.6,
                "name": "normalised residuals",
                "hovertemplate": "%{x:.2f}: %{y:.3f}<extra></extra>",
            },
            {
                "type": "scatter",
                "mode": "lines",
                "x": {"$ref": "hist.centre"},
                "y": {"$ref": "hist.normal"},
                "xaxis": f"x{aid}",
                "yaxis": f"y{aid}",
                "line": {"color": REFERENCE, "width": 2, "shape": "spline"},
                "name": "N(0, 1)",
                "hoverinfo": "skip",
            },
        ]
        layout = fig["layout"]
        layout[f"xaxis{aid}"] = {
            "domain": [0.0, 1.0],
            "anchor": f"y{aid}",
            "range": [float(HIST_EDGES[0]), float(HIST_EDGES[-1])],
            "title": {"text": "Normalised residual"},
            "zeroline": False,
        }
        layout[f"yaxis{aid}"] = {
            "domain": [0.0, top],
            "anchor": f"x{aid}",
            "title": {"text": "Density"},
            "rangemode": "tozero",
        }
        layout["bargap"] = 0.02
        layout["annotations"].append(
            {
                "text": "<b>Normalised residuals</b> (bars) and N(0, 1) (line)",
                "xref": "paper",
                "yref": "paper",
                "x": 0.5,
                "y": top,
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
                "font": {"size": 12},
            }
        )
        if "frames" in fig:
            for j, frame in enumerate(fig["frames"]):
                frame["data"].append({"type": "bar", "y": {"$ref": f"hist.density__{j}"}})
                frame["traces"] = [*frame["traces"], n_heat]  # the list is shared

    what = "predictive draws" if fi.group == "posterior_predictive" else f"draws of {fi.group}"
    caption = (
        f"The first two panels show the observed values of {obs_name} and the mean of "
        f"{r.n_draws} {what} of {fi.var}, on one colour scale. The residual is the observed "
        "value minus the mean. In red cells the observed value is higher than the model, in "
        "blue cells it is lower. "
        "The normalised residual divides the residual by the uncertainty of the model"
    )
    caption += f" and the noise ({sig_name})." if sig_name else "."
    if log:
        caption += " All values are log10."
    if pooled.size:
        sd = float(np.std(pooled))
        out2 = float((np.abs(pooled) > 2).mean())
        caption += (
            f" The normalised residuals have an sd of {sd:.2f} and {pct(out2)} are outside "
            "±2. If the model fits, the sd is near 1, about 5% are outside ±2 and the image "
            "shows no pattern."
        )
        if sd > 1.5:
            caption += " Here the residuals are too large: the model misses structure or the "
            caption += "noise is too small."
        elif sd < 0.67:
            caption += " Here the residuals are too small: the uncertainty is too large."
    if r.frame_label is not None and len(r.frames) > 1:
        caption += f" Use the slider or Play to show a different {r.frame_label}."
    caption += _size_note(r)
    return Figure(chart=fig, datasets=datasets, caption=caption, title=f"Residuals of {fi.var}")


# ------------------------------------------------------------------------ ppc image grid


class PpcImageGridSettings(_ImageSettings):
    n_draws: int = Field(5, ge=1, le=11, description="Predictive draws next to the observed")
    shuffle: bool = Field(False, description="Hide which panel is the observed image")
    seed: int = 0


@recipe(
    "sci.ppc_image_grid",
    title="Predictive image grid",
    settings=PpcImageGridSettings,
    persona="both",
    requires_groups=["observed_data"],
    summary="The observed image next to images that the model makes, on one colour scale. "
    "If the model fits, the observed image looks like one of them.",
    tags=["sci", "image", "diagnostic"],
)
def ppc_image_grid(inp: RecipeInput, s: PpcImageGridSettings) -> Figure:
    """The observed image and ``n_draws`` predictive draws, optionally in a shuffled order."""
    fi, obs, obs_name = _image_input(inp, s)
    colormap, log, origin = _settings_from_grid(s, fi.grid)
    st = fi.st
    n_s = st.sizes[SAMPLE]
    rng = np.random.default_rng(s.seed)
    picks = np.sort(rng.choice(n_s, size=min(s.n_draws, n_s), replace=False))
    keys = ["observed", *(f"draw{j}" for j in range(len(picks)))]
    order = list(rng.permutation(len(keys))) if s.shuffle else list(range(len(keys)))
    letters = [chr(ord("A") + i) for i in range(len(keys))]
    panels: list[_Panel] = []
    for pos, i in enumerate(order):
        key = keys[i]
        if s.shuffle:
            title = f"Panel {letters[pos]}"
        else:
            title = "Observed" if key == "observed" else f"Draw {i}"
        panels.append(_Panel(key, title, "value"))
    levels = _frame_levels(st, fi.frame, s.max_frames)
    ny, nx = st.sizes[fi.y], st.sizes[fi.x]
    k = block_factor(ny, nx, _budget(s.max_cells, 2 + len(panels) * len(levels)))
    values: dict[str, list[np.ndarray]] = {p.key: [] for p in panels}
    masked = False
    for lv in levels:
        sub = st.isel({fi.frame: lv}) if fi.frame else st
        draws = np.asarray(sub.values, dtype=float)
        o = np.asarray((obs.isel({fi.frame: lv}) if fi.frame else obs).values, dtype=float)
        if log:
            draws, o = _to_log(draws), _to_log(o)
        masked = masked or bool(np.isnan(o).any())
        got = {"observed": o, **{f"draw{j}": draws[p] for j, p in enumerate(picks)}}
        for p in panels:
            values[p.key].append(block_mean(got[p.key], k))
    xs, ys, xl, yl = _coords(fi, inp, k)
    r = _Raster(
        panels=panels,
        values=values,
        xs=xs,
        ys=ys,
        x_label=xl,
        y_label=yl,
        frame_label=inp.dim_label(fi.frame) if fi.frame else None,
        frames=_frame_names(fi, levels),
        block=k,
        n_draws=n_s,
        shape=(ny, nx),
        masked=masked,
    )
    fig, datasets = _field_figure(
        r,
        colormap=colormap,
        origin=origin,
        aspect=s.aspect,
        columns=s.columns,
        width=s.width,
        height=s.height,
    )
    what = "the posterior predictive" if fi.group == "posterior_predictive" else fi.group
    if s.shuffle:
        real = letters[order.index(0)]
        caption = (
            f"One of the {len(panels)} panels shows the observed values of {obs_name}. The others "
            f"are {len(picks)} images of {fi.var} from {what}. All panels use one colour "
            "scale. Try to find the observed image. If you cannot, the model makes images "
            f"like the real one. The answer: the observed image is panel {real}."
        )
    else:
        caption = (
            f"The first panel shows the observed values of {obs_name}. The other panels are "
            f"{len(picks)} images of {fi.var} from {what}. All panels use one colour scale. "
            "If the model fits, the observed image looks like one more draw: the same "
            "shapes, the same contrast and the same noise."
        )
    if log:
        caption += " The colours show log10 of the value."
    if r.frame_label is not None and len(r.frames) > 1:
        caption += f" Use the slider or Play to show a different {r.frame_label}."
    caption += _size_note(r)
    return Figure(
        chart=fig,
        datasets=datasets,
        caption=caption,
        title=f"Predictive images of {fi.var}",
    )


# ------------------------------------------------------------------------------ swatches


class SwatchesSettings(RecipeSettings):
    var: str | None = Field(None, description="sRGB output in [0, 1] with a channel dim")
    group: GroupName = Field(
        "auto", description="Group to read (auto: posterior, predictions, posterior_predictive)"
    )
    observed_var: str | None = Field(None, description="Observed sRGB (default: same name)")
    channel: str = Field("rgb", description="Dim of length 3 with the r, g, b values")
    item: str | None = Field(None, description="Dim with one swatch per level (default: other)")
    row: str = Field("row", description="Coord on the item dim with the map row")
    col: str = Field("col", description="Coord on the item dim with the map column")
    arrange: Literal["auto", "list", "map"] = Field(
        "auto", description="auto: a map when the item dim has row and col coords"
    )
    prob: str | None = Field(None, description="Variable (…, item) with a probability in [0, 1]")
    prob_as: Literal["border", "opacity"] = "border"
    select: dict[str, str | int | float] | None = None
    n_draws: int = Field(3, ge=0, le=8, description="Panels of single draws")
    max_draws: int = Field(400, ge=1, le=10_000, description="Draws used for the mean")
    max_items: int = Field(60, ge=1, le=5000, description="Swatches in a list")
    tile: int = Field(20, ge=4, le=80, description="Size of one swatch in pixels")
    columns: int = Field(2, ge=1, le=6, description="Map panels per row")
    seed: int = 0
    width: int = Field(640, ge=80, le=2000)


def _hex(rgb: np.ndarray) -> np.ndarray:
    """``(n, 3)`` sRGB in [0, 1] -> ``#rrggbb`` strings (NaN -> the empty-cell colour)."""
    ok = np.isfinite(rgb).all(axis=1)
    v = np.clip(np.where(np.isfinite(rgb), rgb, 0.0), 0.0, 1.0)
    ints = np.round(v * 255).astype(int)
    out = np.array([f"#{a:02x}{b:02x}{c:02x}" for a, b, c in ints], dtype=object)
    out[~ok] = EMPTY
    return out


def _channel_order(da: xr.DataArray, channel: str) -> list[int]:
    vals = [str(v).strip().lower()[:1] for v in coord_values(da, channel)]
    if channel in da.coords and sorted(vals) == ["b", "g", "r"]:
        return [vals.index("r"), vals.index("g"), vals.index("b")]
    return [0, 1, 2]


def _positions(da: xr.DataArray, name: str) -> tuple[np.ndarray, dict[str, Any]]:
    """Map positions from a coord on the item dim, and tick settings for text labels."""
    vals = np.asarray(da[name].values)
    if np.issubdtype(vals.dtype, np.number):
        return vals.astype(float), {}
    levels = sorted({str(v) for v in vals})
    pos = np.array([levels.index(str(v)) for v in vals], dtype=float)
    return pos, {"tickmode": "array", "tickvals": list(range(len(levels))), "ticktext": levels}


def _prob(inp: RecipeInput, s: SwatchesSettings, item: str, ref: xr.DataArray) -> np.ndarray:
    assert s.prob is not None
    da = _aux(inp, s.prob)
    if da is None:
        raise RecipeDataError(f"no variable {s.prob!r} for prob in {', '.join(AUX_GROUPS)}")
    da = _select(da, {d: v for d, v in (s.select or {}).items() if d in da.dims})
    if sample_dims(da):
        da = stack_samples(da).mean(SAMPLE)
    if item not in da.dims:
        raise RecipeDataError(f"prob {s.prob!r} has no dim {item!r}")
    da = _only(da, [item])
    if item in da.coords and item in ref.coords:
        da = da.reindex({item: ref[item].values})
    return np.clip(np.asarray(da.values, dtype=float), 0.0, 1.0)


@recipe(
    "sci.swatches",
    title="Colour swatches",
    settings=SwatchesSettings,
    persona="both",
    summary="Squares painted in the colours that the model gives: the observed colour, the "
    "mean colour and a few possible colours for each item, as a list or as a map.",
    tags=["sci", "colour"],
)
def swatches(inp: RecipeInput, s: SwatchesSettings) -> Figure:
    """Swatches of an sRGB output: observed | mean | draws, as a list or on a row/col map."""
    da, group = _find_draws(
        inp, s.var, s.group, FIELD_GROUPS, need=[s.channel], min_dims=2, prefer_grid=False
    )
    var = str(da.name)
    if s.channel not in da.dims or da.sizes[s.channel] != 3:
        raise RecipeDataError(f"{var!r} needs a dim {s.channel!r} of length 3 (r, g, b)")
    st = stack_samples(_select(da, s.select))
    dims = [str(d) for d in st.dims if d not in (SAMPLE, s.channel)]
    item = s.item or (dims[0] if len(dims) == 1 else None)
    if item is None or item not in dims:
        raise RecipeDataError(
            f"{var!r} has dims {', '.join(dims)}: pass item=<dim> and select= for the others"
        )
    st = _thin(_only(st, [item, s.channel]).transpose(SAMPLE, item, s.channel), s.max_draws)
    ch = _channel_order(st, s.channel)
    has_map = all(c in st.coords and st[c].dims == (item,) for c in (s.row, s.col))
    if s.arrange == "map" and not has_map:
        raise RecipeDataError(f"arrange='map' needs {s.row!r} and {s.col!r} coords on {item!r}")
    as_map = has_map and s.arrange != "list"
    n_items = st.sizes[item]
    shown = n_items
    if not as_map and n_items > s.max_items:
        idx = np.unique(np.linspace(0, n_items - 1, s.max_items).round().astype(int))
        st = st.isel({item: idx})
        shown = len(idx)
    draws = np.asarray(st.values, dtype=float)[..., ch]  # (sample, item, 3)
    n_s = draws.shape[0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        mean = np.nanmean(draws, axis=0)

    # observed sRGB, when there is one with the item and channel dims
    obs: np.ndarray | None = None
    obs_ds = inp.find(*OBS_GROUPS)
    name = observed_name(obs_ds, var, s.observed_var)
    if obs_ds is not None and name is not None:
        o = obs_ds[name]
        o = _select(o, {d: v for d, v in (s.select or {}).items() if d in o.dims})
        if item in o.dims and s.channel in o.dims and o.sizes[s.channel] == 3:
            o = _only(o, [item, s.channel]).transpose(item, s.channel)
            if item in o.coords and item in st.coords:
                o = o.reindex({item: st[item].values})
            obs = np.asarray(o.values, dtype=float)[:, _channel_order(o, s.channel)]
        elif s.observed_var is not None:
            raise RecipeDataError(f"observed {name!r} needs the dims {item} and {s.channel}")

    rng = np.random.default_rng(s.seed)
    picks = np.sort(rng.choice(n_s, size=min(s.n_draws, n_s), replace=False))
    panels: list[tuple[str, np.ndarray]] = []
    if obs is not None:
        panels.append(("Observed", obs))
    panels.append(("Mean", mean))
    panels += [(f"Draw {j + 1}", draws[p]) for j, p in enumerate(picks)]
    prob = _prob(inp, s, item, st) if s.prob else None

    labels = [_label(v) for v in coord_values(st, item)]
    cols: dict[str, Any] = {}
    for j, (title, rgb) in enumerate(panels):
        hexes = _hex(rgb)
        cols[f"colour__{j}"] = hexes
        text = [f"{lab}<br>{title}: {h}" for lab, h in zip(labels, hexes, strict=True)]
        if prob is not None:
            text = [f"{t}<br>{s.prob} = {p:.2f}" for t, p in zip(text, prob, strict=True)]
        cols[f"text__{j}"] = text
    if prob is not None:
        if s.prob_as == "opacity":
            cols["opacity"] = (0.15 + 0.85 * prob).astype(np.float32)
        else:
            cols["border"] = (2.5 * prob).astype(np.float32)

    traces: list[dict[str, Any]] = []
    layout: dict[str, Any] = {"width": s.width, "showlegend": False, "hovermode": "closest"}
    if as_map:
        xpos, xticks = _positions(st, s.col)
        ypos, yticks = _positions(st, s.row)
        cols["x"], cols["y"] = xpos.astype(np.float32), ypos.astype(np.float32)
        n_cols, n_rows = len(np.unique(xpos)), len(np.unique(ypos))
        n = len(panels)
        ncol = max(1, min(s.columns, n))
        nrow = math.ceil(n / ncol)
        margin = {"l": 48, "r": 16, "t": 24, "b": 48}
        hgap = 0.06 if ncol > 1 else 0.0
        panel_w = (s.width - margin["l"] - margin["r"]) * (1 - (ncol - 1) * hgap) / ncol
        tile = max(2.0, min(float(s.tile), panel_w / n_cols))
        panel_h = tile * n_rows
        gap_px = 56  # tick labels under a panel and the title of the next one
        plot_h = nrow * panel_h + (nrow - 1) * gap_px
        vgap = gap_px / plot_h if nrow > 1 else 0.0
        doms = _domains(n, ncol, hgap=hgap, vgap=vgap)
        height = int(plot_h) + margin["t"] + margin["b"]
        layout.update(height=max(height, s.height), margin=margin, annotations=[])
        for j, ((title, _), (dx, dy)) in enumerate(zip(panels, doms, strict=True)):
            aid = _axis_id(j)
            traces.append(_swatch_trace(j, aid, "tiles.x", "tiles.y", tile, prob, s, title))
            xaxis: dict[str, Any] = {
                "domain": dx,
                "anchor": f"y{aid}",
                "range": [float(xpos.min()) - 0.6, float(xpos.max()) + 0.6],
                "showgrid": False,
                "zeroline": False,
                **xticks,
            }
            yaxis: dict[str, Any] = {
                "domain": dy,
                "anchor": f"x{aid}",
                "range": [float(ypos.max()) + 0.6, float(ypos.min()) - 0.6],
                "showgrid": False,
                "zeroline": False,
                "scaleanchor": f"x{aid}",
                "scaleratio": 1,
                **yticks,
            }
            if j // ncol == nrow - 1 or j + ncol >= n:
                xaxis["title"] = {"text": s.col}
            if j % ncol == 0:
                yaxis["title"] = {"text": s.row}
            layout[f"xaxis{aid}"] = xaxis
            layout[f"yaxis{aid}"] = yaxis
            layout["annotations"].append(
                {
                    "text": f"<b>{title}</b>",
                    "xref": "paper",
                    "yref": "paper",
                    "x": (dx[0] + dx[1]) / 2,
                    "y": dy[1],
                    "xanchor": "center",
                    "yanchor": "bottom",
                    "showarrow": False,
                    "font": {"size": 12},
                }
            )
    else:
        cols["y"] = np.arange(shown, dtype=np.float32)
        tile = float(s.tile)
        for j, (title, _) in enumerate(panels):
            cols[f"x__{j}"] = np.full(shown, float(j), dtype=np.float32)
            traces.append(_swatch_trace(j, "", f"tiles.x__{j}", "tiles.y", tile, prob, s, title))
        label_w = min(160, 8 + 7 * max((len(lab) for lab in labels), default=4))
        margin = {"l": label_w, "r": 16, "t": 16, "b": 40}
        height = int(shown * tile * 1.15) + margin["t"] + margin["b"]
        layout.update(
            height=max(height, s.height),
            margin=margin,
            xaxis={
                "tickmode": "array",
                "tickvals": list(range(len(panels))),
                "ticktext": [t for t, _ in panels],
                "range": [-0.6, len(panels) - 0.4],
                "showgrid": False,
                "zeroline": False,
                "side": "top",
            },
            yaxis={
                "tickmode": "array",
                "tickvals": list(range(shown)),
                "ticktext": labels,
                "autorange": "reversed",
                "showgrid": False,
                "zeroline": False,
                "title": {"text": inp.dim_label(item)},
            },
        )
        layout["width"] = min(s.width, label_w + 16 + int(len(panels) * tile * 2.2) + 40)
        layout["width"] = max(layout["width"], 240)
    table = pd.DataFrame(cols)
    if len(table) * max(1, len(panels)) > ROW_BUDGET:
        raise RecipeDataError(f"too many swatches ({n_items}): pick fewer with select=")

    noun = inp.dim_label(item)
    caption = (
        f"Each square is the colour of one {noun} of {var}. "
        + ("The first panel shows the observed colours. " if obs is not None else "")
        + f"The mean panel shows the average colour over {n_s} draws of {group}."
    )
    if len(picks):
        caption += (
            f" The {len(picks)} draw panels show possible colours from the model. Where they "
            "differ, the model is not sure of the colour."
        )
    if obs is not None:
        d = np.sqrt(np.nansum((mean - obs) ** 2, axis=1)) * 255
        d = d[np.isfinite(obs).all(axis=1)]
        if d.size:
            caption += (
                f" The mean colour is on average {float(np.mean(d)):.0f} units from the "
                "observed colour (sRGB, 0 to 255 per channel)."
            )
    if prob is not None:
        how = "thicker borders" if s.prob_as == "border" else "stronger colours"
        caption += f" {how.capitalize()} show a higher {s.prob}."
    if as_map:
        caption += f" The squares are on the {s.row} × {s.col} map of the {noun}s."
    elif shown < n_items:
        caption += f" The list shows {shown} of the {n_items} {noun}s."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets={"tiles": table},
        caption=caption,
        title=f"Colours of {var}",
    )


def _swatch_trace(
    j: int,
    aid: str,
    x_ref: str,
    y_ref: str,
    tile: float,
    prob: np.ndarray | None,
    s: SwatchesSettings,
    title: str,
) -> dict[str, Any]:
    marker: dict[str, Any] = {
        "symbol": "square",
        "size": round(tile * 0.9, 2),
        "color": {"$ref": f"tiles.colour__{j}"},
        "line": {"width": 0},
    }
    model_panel = title != "Observed"
    if prob is not None and model_panel:
        if s.prob_as == "opacity":
            marker["opacity"] = {"$ref": "tiles.opacity"}
        else:
            marker["line"] = {"width": {"$ref": "tiles.border"}, "color": OBSERVED}
    return {
        "type": "scatter",
        "mode": "markers",
        "name": title,
        "x": {"$ref": x_ref},
        "y": {"$ref": y_ref},
        "xaxis": f"x{aid}",
        "yaxis": f"y{aid}",
        "marker": marker,
        "text": {"$ref": f"tiles.text__{j}"},
        "hovertemplate": "%{text}<extra></extra>",
    }


# ------------------------------------------------------------------------ power spectrum


class PowerSpectrumSettings(RecipeSettings):
    var: str | None = Field(None, description="Binned spectrum with draws (default: first 1-D)")
    group: GroupName = Field(
        "auto", description="Group to read (auto: posterior, predictions, posterior_predictive)"
    )
    observed_var: str | None = Field(None, description="Observed bandpowers (default: same name)")
    error_var: str | None = Field(
        None, description="Observed error bars (sd), default: <observed>_sd if it exists"
    )
    theory: str | None = Field(None, description="Theory curve variable (draws or values)")
    along: str | None = Field(None, description="Bin dim (default: the only other dim)")
    x_coord: str | None = Field(
        None,
        description="Coord on the bin dim for the x axis, for example ell (default: the "
        "first numeric coord)",
    )
    select: dict[str, str | int | float] | None = None
    levels: list[float] = Field(default_factory=lambda: [0.5, 0.9], max_length=3)
    point: Literal["median", "mean"] = "median"
    max_draws: int = Field(2000, ge=1, le=20_000)
    log_x: bool = False
    log_y: bool = False
    width: int = Field(640, ge=80, le=2000)
    height: int = Field(360, ge=60, le=2000)

    @field_validator("levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        return check_levels(v)


def _x_of(da: xr.DataArray, dim: str, name: str | None) -> tuple[np.ndarray, str]:
    """Numeric x positions along ``dim`` and their label."""
    if name is not None:
        if name not in da.coords or da[name].dims != (dim,):
            raise RecipeDataError(f"no coord {name!r} on the dim {dim!r}")
        return np.asarray(da[name].values, dtype=float), name
    for c in da.coords:
        cc = da[c]
        if c != dim and cc.dims == (dim,) and np.issubdtype(cc.dtype, np.number):
            return np.asarray(cc.values, dtype=float), str(c)
    xs, _ = _numeric_coord(da, dim)
    return xs, dim


@recipe(
    "sci.power_spectrum",
    title="Power spectrum",
    settings=PowerSpectrumSettings,
    persona="expert",
    summary="A binned spectrum against scale or frequency, with 50% and 90% bands, an "
    "optional theory curve and the observed points with error bars.",
    tags=["sci"],
)
def power_spectrum(inp: RecipeInput, s: PowerSpectrumSettings) -> Figure:
    """Bands of a binned spectrum, a theory curve and observed bandpowers with error bars."""
    need = [s.along] if s.along else []
    da, group = _find_draws(inp, s.var, s.group, SPECTRUM_GROUPS, need=need, prefer_grid=False)
    var = str(da.name)
    st = stack_samples(_select(da, s.select))
    dims = [str(d) for d in st.dims if d != SAMPLE]
    along = s.along or (dims[0] if len(dims) == 1 else None)
    if along is None:
        big = [d for d in dims if st.sizes[d] > 1]
        along = big[0] if len(big) == 1 else None
    if along is None or along not in dims:
        raise RecipeDataError(
            f"{var!r} has dims {', '.join(dims)}: pass along=<bin dim> and select= for the others"
        )
    st = _thin(_only(st, [along]).transpose(SAMPLE, along), s.max_draws)
    x, x_label = _x_of(st, along, s.x_coord)
    draws = np.asarray(st.values, dtype=float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        cols: dict[str, Any] = {"x": x.astype(np.float32)}
        for lv, lo_c, hi_c in interval_columns(s.levels):
            cols[lo_c] = np.nanquantile(draws, (1 - lv) / 2, 0).astype(np.float32)
            cols[hi_c] = np.nanquantile(draws, (1 + lv) / 2, 0).astype(np.float32)
        pt = np.nanmedian(draws, 0) if s.point == "median" else np.nanmean(draws, 0)
        cols["point"] = pt.astype(np.float32)
    ref = st.isel({SAMPLE: 0}, drop=True)
    obs, obs_name = _observed(inp, var, s.observed_var, ref, [along], s.select)
    err: np.ndarray | None = None
    if obs is not None and obs_name is not None:
        cols["observed"] = np.asarray(obs.values, dtype=np.float32)
        obs_ds = inp.find(*OBS_GROUPS)
        err_name = s.error_var or f"{obs_name}_sd"
        if obs_ds is not None and err_name in obs_ds.data_vars:
            e, _ = _observed(inp, var, err_name, ref, [along], s.select)
            if e is not None:
                err = np.asarray(e.values, dtype=np.float32)
                cols["error"] = err
        elif s.error_var is not None:
            raise RecipeDataError(f"no observed error variable {s.error_var!r}")
    datasets: dict[str, pd.DataFrame] = {"spectrum": pd.DataFrame(cols)}

    theory_ok = False
    if s.theory is not None:
        th = _aux(inp, s.theory)
        if th is None:
            raise RecipeDataError(f"no theory variable {s.theory!r} in {', '.join(AUX_GROUPS)}")
        th = _select(th, {d: v for d, v in (s.select or {}).items() if d in th.dims})
        if sample_dims(th):
            th = stack_samples(th).median(SAMPLE)
        tdims = [str(d) for d in th.dims if th.sizes[d] > 1]
        if len(tdims) != 1:
            raise RecipeDataError(f"theory {s.theory!r} must be a curve along one dim")
        th = th.squeeze([d for d in th.dims if d not in tdims], drop=True)
        tx, _ = _x_of(th, tdims[0], s.x_coord if s.x_coord in th.coords else None)
        datasets["theory"] = pd.DataFrame(
            {"x": tx.astype(np.float32), "y": np.asarray(th.values, dtype=np.float32)}
        )
        theory_ok = True

    traces: list[dict[str, Any]] = []
    for j, (_lv, lo_c, hi_c) in enumerate(interval_columns(s.levels)):
        for col, extra in (
            (lo_c, {}),
            (hi_c, {"fill": "tonexty", "fillcolor": BAND, "opacity": round(0.16 + 0.12 * j, 3)}),
        ):
            traces.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "x": {"$ref": "spectrum.x"},
                    "y": {"$ref": f"spectrum.{col}"},
                    "line": {"width": 0, "color": BAND},
                    "showlegend": False,
                    "hoverinfo": "skip",
                    **extra,
                }
            )
    traces.append(
        {
            "type": "scatter",
            "mode": "lines+markers",
            "name": f"model {s.point}",
            "x": {"$ref": "spectrum.x"},
            "y": {"$ref": "spectrum.point"},
            "line": {"color": ESTIMATE, "width": 2},
            "marker": {"color": ESTIMATE, "size": 4},
            "hovertemplate": f"{x_label} %{{x:.4g}}<br>{s.point} %{{y:.4g}}<extra></extra>",
        }
    )
    if theory_ok:
        traces.append(
            {
                "type": "scatter",
                "mode": "lines",
                "name": f"theory ({s.theory})",
                "x": {"$ref": "theory.x"},
                "y": {"$ref": "theory.y"},
                "line": {"color": REFERENCE, "width": 1.5, "dash": "dash"},
                "hovertemplate": f"theory<br>{x_label} %{{x:.4g}}: %{{y:.4g}}<extra></extra>",
            }
        )
    if obs is not None:
        ob: dict[str, Any] = {
            "type": "scatter",
            "mode": "markers",
            "name": "observed",
            "x": {"$ref": "spectrum.x"},
            "y": {"$ref": "spectrum.observed"},
            "marker": {"color": OBSERVED, "size": 6},
            "hovertemplate": f"observed<br>{x_label} %{{x:.4g}}: %{{y:.4g}}<extra></extra>",
        }
        if err is not None:
            ob["error_y"] = {
                "type": "data",
                "array": {"$ref": "spectrum.error"},
                "visible": True,
                "color": OBSERVED,
                "thickness": 1,
                "width": 2,
            }
        traces.append(ob)
    layout: dict[str, Any] = {
        "height": s.height,
        "width": s.width,
        "margin": {"l": 64, "r": 16, "t": 24, "b": 48},
        "xaxis": {"title": {"text": x_label}, **({"type": "log"} if s.log_x else {})},
        "yaxis": {
            "title": {"text": var},
            "linecolor": MUTED,
            **({"type": "log"} if s.log_y else {}),
        },
        "hovermode": "closest",
        "legend": {"orientation": "h", "y": 1.02, "yanchor": "bottom", "x": 0},
    }
    caption = (
        f"The line is the {s.point} of {var} in each bin of {x_label}, from {draws.shape[0]} "
        f"draws of {group}. The shaded bands are {_band_text(s.levels)}."
    )
    if theory_ok:
        caption += f" The dashed line is the theory curve {s.theory}."
    if obs is not None:
        caption += " The dots are the observed values"
        caption += " with their error bars." if err is not None else "."
        lv, lo_c, hi_c = interval_columns(s.levels)[0]
        o = cols["observed"]
        seen = np.isfinite(o)
        if seen.any():
            inside = float(((o >= cols[lo_c]) & (o <= cols[hi_c]))[seen].mean())
            caption += f" {pct(inside)} of the observed values are in the {pct(lv)} band."
    if (s.log_x and (x <= 0).any()) or (s.log_y and (np.asarray(cols["point"]) <= 0).any()):
        caption += " A log axis does not show values at or below zero."
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets=datasets,
        caption=caption,
        title=f"Spectrum of {var}",
    )


def sci_image_recipes() -> list[Recipe[Any]]:
    """The image and spectrum recipes of the ``sci`` pack."""
    return [image_residuals, ppc_image_grid, swatches, power_spectrum]
