"""Training history recipe (Vega-Lite): the loss of an iterative fit over its steps.

============================  ==========================================================
``core.loss_curve``           raw loss per step (light) and its moving average, log scale
                              when the loss falls over orders of magnitude
============================  ==========================================================

Reads the ``fit_history`` model group (an ADVI fit: ``loss(step)``, attrs ``objective`` and
``engine``; dataset attr ``thinned_from`` when the engine kept fewer steps) or a
``metrics_history`` group with the same layout: each data variable is one curve over
``step`` and any extra dims are labels (one series per level).
"""

from __future__ import annotations

from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field

from mapi_plots.palette import BAND, ESTIMATE
from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
)
from mapi_plots.recipes._util import coord_values, fmt, pct
from mapi_plots.recipes.extras import _CATEGORY, _thin

__all__ = ["LossCurveSettings", "loss_curve"]

_HISTORY_GROUPS = ("fit_history", "metrics_history")
_MAX_SERIES = 8


class LossCurveSettings(RecipeSettings):
    var: str | None = None
    group: Literal["auto", "fit_history", "metrics_history"] = "auto"
    along: str = "step"
    smooth: int | None = Field(
        None, ge=1, le=100_000, description="Moving-average window (default: steps / 50)"
    )
    log_y: bool | None = Field(
        None, description="Log scale (default: when all values are > 0 and span > 50x)"
    )
    max_points: int = Field(2000, ge=20, le=20_000, description="Steps drawn per series")
    width: int = Field(560, ge=80, le=2000)
    height: int = Field(220, ge=60, le=2000)


def _series(
    ds: xr.Dataset, s: LossCurveSettings
) -> list[tuple[str, np.ndarray, np.ndarray, xr.DataArray]]:
    """``(label, steps, values, source var)`` per curve: one per variable and extra level."""
    if s.var is not None:
        if s.var not in ds.data_vars:
            raise RecipeDataError(
                f"no variable {s.var!r} (available: {', '.join(map(str, ds.data_vars))})"
            )
        names = [s.var]
    else:
        names = [
            str(v)
            for v in ds.data_vars
            if s.along in ds[v].dims and np.issubdtype(ds[v].dtype, np.number)
        ]
    if not names:
        raise RecipeDataError(f"the history has no numeric variable over {s.along!r}")
    out: list[tuple[str, np.ndarray, np.ndarray, xr.DataArray]] = []
    for name in names:
        da = ds[name]
        if s.along not in da.dims:
            raise RecipeDataError(f"{name!r} has no dim {s.along!r}")
        steps = coord_values(da, s.along)
        other = [str(d) for d in da.dims if d != s.along]
        if not other:
            out.append((name, steps, np.asarray(da.values, dtype=float), da))
            continue
        st = da.stack(_series=other).transpose(s.along, "_series")
        for j in range(st.sizes["_series"]):
            levels = [str(v) for v in np.atleast_1d(st["_series"].values[j])]
            label = ", ".join(levels if len(names) == 1 else [name, *levels])
            out.append((label, steps, np.asarray(st.values[:, j], dtype=float), da))
    if len(out) > _MAX_SERIES:
        raise RecipeDataError(
            f"the history has {len(out)} series; the plot shows at most {_MAX_SERIES} "
            "(set `var` to one variable)"
        )
    return out


@recipe(
    "core.loss_curve",
    title="Loss curve",
    settings=LossCurveSettings,
    persona="expert",
    summary="The loss of an iterative fit, such as ADVI, at each step, with a moving average. "
    "It shows if the fit converged.",
    tags=["diagnostic"],
)
def loss_curve(inp: RecipeInput, s: LossCurveSettings) -> Figure:
    """Raw loss (light) and its trailing moving average per step; log y when it spans decades."""
    groups = _HISTORY_GROUPS if s.group == "auto" else (s.group,)
    ds, group = None, ""
    for g in groups:
        ds, group = inp.find(g), g
        if ds is not None:
            break
    if ds is None:
        raise RecipeDataError(
            f"no {' or '.join(groups)} group (an ADVI fit writes its loss in fit_history)"
        )
    curves = _series(ds, s)
    parts = []
    window = 1
    for label, steps, values, _ in curves:
        n = values.size
        window = s.smooth or max(1, n // 50)
        smooth = pd.Series(values).rolling(window, min_periods=1).mean().to_numpy()
        keep = _thin(n, s.max_points)
        parts.append(
            pd.DataFrame(
                {
                    "step": steps[keep],
                    "series": label,
                    "value": values[keep],
                    "smooth": smooth[keep],
                }
            )
        )
    frame = pd.concat(parts, ignore_index=True)
    frame = frame[np.isfinite(frame["value"])].reset_index(drop=True)
    if frame.empty:
        raise RecipeDataError("the history has no finite values")
    vals = frame["value"].to_numpy()
    log_y = s.log_y
    if log_y is None:
        log_y = bool(np.all(vals > 0) and vals.max() / vals.min() > 50)
    elif log_y and not np.all(vals > 0):
        raise RecipeDataError("log_y needs values above 0")
    labels = [c[0] for c in curves]
    first = curves[0][3]
    y_title = str(first.attrs.get("objective") or (labels[0] if len(curves) == 1 else "value"))
    yscale = alt.Scale(type="log") if log_y else alt.Scale(zero=False)
    if len(curves) == 1:
        raw_color: Any = alt.value(BAND)
        smooth_color: Any = alt.value(ESTIMATE)
    else:
        colors = alt.Scale(domain=labels, range=_CATEGORY[: len(labels)])
        raw_color = alt.Color("series:N", scale=colors, title=None, sort=labels, legend=None)
        smooth_color = alt.Color("series:N", scale=colors, title=None, sort=labels)
    base = alt.Chart(named("loss")).encode(x=alt.X("step:Q", title=s.along))
    chart = alt.layer(
        base.mark_line(strokeWidth=0.8, opacity=0.35).encode(
            y=alt.Y("value:Q", title=y_title, scale=yscale), color=raw_color, detail="series:N"
        ),
        base.mark_line(strokeWidth=2).encode(
            y="smooth:Q",
            color=smooth_color,
            detail="series:N",
            tooltip=[
                "series:N",
                "step:Q",
                alt.Tooltip("value:Q", format=".4g"),
                alt.Tooltip("smooth:Q", format=".4g", title="moving average"),
            ],
        ),
    ).properties(width=s.width, height=s.height, title=f"{y_title} by {s.along}")

    label, steps, values, _ = curves[0]
    sm = pd.Series(values).rolling(window, min_periods=1).mean().to_numpy()
    ok = np.isfinite(sm)
    caption = (
        f"The light line is the value of {y_title} at each {s.along} and the dark line is "
        f"its moving average over {window} {s.along}s ({group})."
    )
    if ok.any():
        sm_ok = sm[ok]
        start, end = float(sm_ok[0]), float(sm_ok[-1])
        i0 = int(np.floor(0.9 * (sm_ok.size - 1)))
        ref = float(sm_ok[i0])
        change = (end - ref) / abs(ref) if ref != 0 else float("nan")
        of = f" for {label}" if len(curves) > 1 else ""
        caption += (
            f" The moving average{of} went from {fmt(start)} to {fmt(end)}. In the last 10% "
            f"of the {s.along}s it changed by {pct(change)}."
        )
        if np.isfinite(change) and change < -0.01:
            caption += " The loss still goes down at the end. More steps can improve the fit."
        elif np.isfinite(change) and abs(change) <= 0.01:
            caption += " The loss is stable at the end."
    if log_y:
        caption += " The y axis has a log scale."
    thinned = ds.attrs.get("thinned_from")
    if thinned:
        caption += f" The fit kept {len(steps)} of its {int(thinned)} {s.along}s."
    if len(steps) > s.max_points:
        caption += f" The plot shows {min(len(steps), s.max_points)} of {len(steps)} {s.along}s."
    return Figure(chart=chart, datasets={"loss": frame}, caption=caption)
