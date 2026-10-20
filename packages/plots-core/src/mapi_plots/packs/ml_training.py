"""Deep-learning training recipes of the ``ml`` pack: curves, confusion over epochs, mistakes.

============================  ==========================================================
``ml.training_curves``        train and validation curves per metric (Vega-Lite), one
                              panel per metric, one colour per label (``split``)
``ml.confusion_epochs``       row-normalised validation confusion matrices at <= 6 epochs
                              (Vega-Lite small multiples)
``ml.misclassified``          the wrong validation images as a grid of Plotly ``image``
                              traces (inline RGB ``z`` pixels, never a ``source`` URL)
============================  ==========================================================

Inputs follow the groups the ``mapi_jax`` trainer writes in the bundle:

* ``metrics_history`` (the v0.11 tracker): one var per metric over ``step`` plus a label
  dim ``split`` (``train`` batch values at every logged step, ``val`` at each evaluation)
  and an unlabelled ``epoch(step)`` var (the epoch number logged at each epoch end);
* ``training``: per-epoch ``train_<metric>`` / ``val_<metric>`` pairs over ``epoch``, and
  ``confusion(epoch, true_class, pred_class)`` counts of the validation set; attrs
  ``best_epoch``, ``stopped_early``, ``epochs_run``, ``monitor``;
* ``misclassified``: ``thumb(example, ty, tx, channel)`` uint8, ``prob``/``true_prob``
  and the ``image_id``/``true_label``/``pred_label`` coords; attrs ``epoch``, ``n_wrong``,
  ``n_val``, ``color``.

Images never travel as URLs: the plot sanitizer accepts ``data:`` sources, but the static
renderer refuses every Plotly ``source`` key, so each thumbnail is an ``image`` trace whose
``z`` is the nested ``[row][column][r, g, b]`` pixel list (gray thumbnails repeat their one
channel three times). Thumbnails are subsampled so the whole grid stays far below the
sanitizer's node cap.
"""

from __future__ import annotations

import html
import math
from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field

from mapi_plots.palette import EMPTY, ESTIMATE, OBSERVED, SURFACE
from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
    wrap_facet,
)
from mapi_plots.recipes._util import coord_values, fmt, pct
from mapi_plots.recipes.extras import _CATEGORY, _thin

__all__ = [
    "ConfusionEpochsSettings",
    "MisclassifiedSettings",
    "TrainingCurvesSettings",
    "confusion_epochs",
    "misclassified",
    "training_curves",
]

HISTORY_GROUP = "metrics_history"
TRAINING_GROUP = "training"
MISCLASSIFIED_GROUP = "misclassified"
OTHER = "(other)"
MAX_PANELS = 4
MAX_EPOCH_PANELS = 6
# Inline pixel budget of the misclassified grid: about four JSON nodes per pixel (the
# [r, g, b] list and its three numbers), well under the sanitizer's 200 000 node cap.
PIXEL_NODE_BUDGET = 120_000
# Metrics where a smaller value is better; every other key (accuracy, ...) is "higher".
_LOWER_IS_BETTER = ("loss", "rmse", "mae", "mse", "error", "nll")
_SECONDARY = ("accuracy", "rmse", "mae")
_VAL_LEVELS = ("val", "validation", "valid", "eval", "test")


def _lower_is_better(key: str) -> bool:
    return any(token in key.lower() for token in _LOWER_IS_BETTER)


def _default_keys(available: list[str]) -> list[str]:
    keys = ["loss"] if "loss" in available else []
    keys += [k for k in _SECONDARY if k in available][:1]
    return keys or available[:1]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _val_level(levels: list[str]) -> str | None:
    for name in _VAL_LEVELS:
        if name in levels:
            return name
    return None


# ------------------------------------------------------------------------ training curves


class TrainingCurvesSettings(RecipeSettings):
    keys: list[str] | None = Field(
        None,
        min_length=1,
        max_length=MAX_PANELS,
        description="Metrics to draw, one panel each (default: loss and accuracy or rmse)",
    )
    label: str = Field("split", description="The label dim whose levels get one colour each")
    x: Literal["step", "epoch"] = Field("step", description="The x axis")
    smooth: int | None = Field(
        None, ge=1, le=100_000, description="Moving-average window of dense series (default: n/50)"
    )
    log_y: bool | None = Field(
        None, description="Log y scale (default: when all values are > 0 and span > 50x)"
    )
    max_points: int = Field(2000, ge=20, le=20_000, description="Points drawn per series")
    width: int = Field(320, ge=80, le=2000)
    height: int = Field(200, ge=60, le=2000)


def _history_series(
    ds: xr.Dataset, keys: list[str], s: TrainingCurvesSettings
) -> tuple[list[tuple[str, str, np.ndarray, np.ndarray]], str]:
    """``(key, level, x, value)`` series of a ``metrics_history`` group, and the x title."""
    epoch_of: Any = None
    x_title = "step"
    if s.x == "epoch":
        if "epoch" not in ds.data_vars or "step" not in ds["epoch"].dims:
            raise RecipeDataError("x='epoch' needs an epoch(step) metric in the history")
        ep = ds["epoch"]
        extra = [str(d) for d in ep.dims if d != "step"]
        if extra:
            ep = ep.max(extra)
        ok = np.isfinite(np.asarray(ep.values, dtype=float))
        st = coord_values(ds, "step")[ok].astype(float)
        ev = np.asarray(ep.values, dtype=float)[ok]
        if st.size == 0:
            raise RecipeDataError("the epoch metric has no values")
        # an epoch ends at its logged step: steps in between get fractional epochs
        xs, ys = np.concatenate([[0.0], st]), np.concatenate([[0.0], ev])

        def epoch_of(steps: np.ndarray) -> np.ndarray:
            return np.asarray(np.interp(steps.astype(float), xs, ys))

        x_title = "epoch"
    out: list[tuple[str, str, np.ndarray, np.ndarray]] = []
    steps = coord_values(ds, "step")
    for key in keys:
        da = ds[key]
        others = [str(d) for d in da.dims if d not in ("step", s.label)]
        if others:
            da = da.mean(others)
        if s.label in da.dims:
            levels = [str(v) for v in coord_values(da, s.label)]
            pairs = [
                (lev, np.asarray(da.isel({s.label: i}).values, dtype=float))
                for i, lev in enumerate(levels)
            ]
        else:
            pairs = [("all", np.asarray(da.values, dtype=float))]
        for level, values in pairs:
            ok = np.isfinite(values)
            if not ok.any():
                continue
            x = steps[ok].astype(float)
            out.append((key, level, epoch_of(x) if epoch_of is not None else x, values[ok]))
    return out, x_title


def _training_series(
    ds: xr.Dataset, keys: list[str]
) -> list[tuple[str, str, np.ndarray, np.ndarray]]:
    epochs = coord_values(ds, "epoch").astype(float)
    out: list[tuple[str, str, np.ndarray, np.ndarray]] = []
    for key in keys:
        for level in ("train", "val"):
            name = f"{level}_{key}" if key != "lr" else "lr"
            if name not in ds.data_vars or (key == "lr" and level == "val"):
                continue
            values = np.asarray(ds[name].values, dtype=float)
            ok = np.isfinite(values)
            if ok.any():
                out.append((key, level if key != "lr" else "all", epochs[ok], values[ok]))
    return out


def _training_keys(ds: xr.Dataset) -> list[str]:
    keys: list[str] = []
    for name in ds.data_vars:
        n = str(name)
        for prefix in ("train_", "val_"):
            if n.startswith(prefix) and n[len(prefix) :] not in keys:
                keys.append(n[len(prefix) :])
    if "lr" in ds.data_vars:
        keys.append("lr")
    return keys


def _overfits(
    series: list[tuple[str, str, np.ndarray, np.ndarray]], key: str, val: str | None
) -> bool:
    """Over the last 25 % of the x range the validation value rises by more than 2 % and the
    training value falls (both read where the window starts and where it ends)."""
    if val is None or not _lower_is_better(key):
        return False
    train = next((x for x in series if x[0] == key and x[1] == "train"), None)
    held = next((x for x in series if x[0] == key and x[1] == val), None)
    if train is None or held is None or held[2].size < 2 or train[2].size < 2:
        return False
    lo = min(train[2].min(), held[2].min())
    hi = max(train[2].max(), held[2].max())
    cut = lo + 0.75 * (hi - lo)
    vx, vy = held[2], held[3]
    inside = np.flatnonzero(vx >= cut)
    if inside.size == 0:
        return False
    start = max(0, int(inside[0]) - 1) if inside.size < 2 else int(inside[0])
    v0, v1 = float(vy[start]), float(vy[-1])
    tx, ty = train[2], train[3]
    window = max(1, ty.size // 20)
    smooth = pd.Series(ty).rolling(window, min_periods=1).mean().to_numpy()
    t0 = float(np.interp(float(vx[start]), tx, smooth))
    t1 = float(smooth[-1])
    return v0 != 0 and (v1 - v0) / abs(v0) > 0.02 and t1 < t0


@recipe(
    "ml.training_curves",
    title="Training curves",
    settings=TrainingCurvesSettings,
    persona="both",
    summary="The training and validation loss and accuracy of a network at each step. It "
    "shows if the training converged and if the model starts to overfit.",
    tags=["ml", "diagnostic"],
)
def training_curves(inp: RecipeInput, s: TrainingCurvesSettings) -> Figure:
    """One panel per metric; a colour per label level; validation drawn with points."""
    hist = inp.find(HISTORY_GROUP)
    train = inp.find(TRAINING_GROUP)
    # a bare dataset is returned for any group name: keep it only when it has the dims
    hist = hist if hist is not None and "step" in hist.dims else None
    train = train if train is not None and "epoch" in train.dims else None
    use_history = hist is not None and (
        any(s.label in hist[v].dims for v in hist.data_vars) or train is None
    )
    if use_history:
        assert hist is not None
        available = [
            str(v)
            for v in hist.data_vars
            if "step" in hist[v].dims
            and np.issubdtype(hist[v].dtype, np.number)
            and str(v) != "epoch"
        ]
        group = HISTORY_GROUP
    elif train is not None:
        available = _training_keys(train)
        group = TRAINING_GROUP
    else:
        raise RecipeDataError(
            "the data has neither a metrics_history nor a training group; a deep-learning fit "
            "writes them"
        )
    keys = list(s.keys) if s.keys else _default_keys(available)
    missing = [k for k in keys if k not in available]
    if missing:
        raise RecipeDataError(
            f"no metric {missing[0]!r} in {group} (available: {', '.join(available) or 'none'})"
        )
    if use_history:
        assert hist is not None
        series, x_title = _history_series(hist, keys, s)
        attrs: dict[str, Any] = dict(train.attrs) if train is not None else {}
    else:
        assert train is not None
        series, x_title = _training_series(train, keys), "epoch"
        attrs = dict(train.attrs)
    if not series:
        raise RecipeDataError(f"the metrics {', '.join(keys)} have no finite values")

    levels = list(dict.fromkeys(level for _, level, _, _ in series))
    val = _val_level(levels)
    parts = []
    windows: dict[tuple[str, str], int] = {}
    for key, level, x, values in series:
        n = values.size
        dense = level != val and n > 50
        window = (s.smooth or max(1, n // 50)) if dense else 1
        windows[key, level] = window
        smooth = pd.Series(values).rolling(window, min_periods=1).mean().to_numpy()
        keep = _thin(n, s.max_points)
        parts.append(
            pd.DataFrame(
                {
                    "key": key,
                    "label": level,
                    "x": x[keep],
                    "value": values[keep],
                    "smooth": smooth[keep],
                    "point": not dense,
                }
            )
        )
    frame = pd.concat(parts, ignore_index=True)
    colors = alt.Scale(
        domain=levels, range=[_CATEGORY[i % len(_CATEGORY)] for i in range(len(levels))]
    )
    color = alt.Color("label:N", scale=colors, sort=levels, title=s.label)
    panels = []
    log_keys: list[str] = []
    for key in keys:
        sub = frame[frame["key"] == key]
        if sub.empty:
            continue
        vals = sub["value"].to_numpy()
        log_y = s.log_y
        if log_y is None:
            log_y = bool(np.all(vals > 0) and vals.max() / vals.min() > 50)
        elif log_y and not np.all(vals > 0):
            raise RecipeDataError(f"log_y needs values above 0 ({key!r} has others)")
        if log_y:
            log_keys.append(key)
        yscale = alt.Scale(type="log") if log_y else alt.Scale(zero=False)
        base = (
            alt.Chart(named("curves"))
            .transform_filter(alt.datum.key == key)
            .encode(x=alt.X("x:Q", title=x_title))
        )
        layers = [
            base.mark_line(strokeWidth=0.8, opacity=0.3).encode(
                y=alt.Y("value:Q", title=key, scale=yscale), color=color, detail="label:N"
            ),
            base.mark_line(strokeWidth=2).encode(
                y="smooth:Q",
                color=color,
                detail="label:N",
                tooltip=[
                    "label:N",
                    alt.Tooltip("x:Q", title=x_title, format=".4g"),
                    alt.Tooltip("value:Q", format=".4g"),
                ],
            ),
            base.transform_filter(alt.datum.point)
            .mark_point(filled=True, size=28)
            .encode(y="smooth:Q", color=color),
        ]
        panels.append(alt.layer(*layers).properties(width=s.width, height=s.height, title=key))
    chart = alt.concat(*panels, columns=2).resolve_scale(color="shared")

    caption = (
        f"The plot shows {_join(keys)} at each {x_title} of the training, with one line "
        f"for each {s.label}: {_join(levels)}."
    )
    smoothed = [(lev, w) for (_, lev), w in windows.items() if w > 1]
    if smoothed:
        lev, w = smoothed[0]
        caption += (
            f" The dark line of {lev} is the moving average over {w} points. The light line "
            "is the value of each batch."
        )
    if val is not None:
        for key in keys:
            held = next((x for x in series if x[0] == key and x[1] == val), None)
            if held is None:
                continue
            i = int(np.argmin(held[3]) if _lower_is_better(key) else np.argmax(held[3]))
            at = f"{x_title} {fmt(float(held[2][i]))}"
            value = float(held[3][i])
            shown = pct(value) if "accuracy" in key.lower() else fmt(value)
            caption += f" The best validation {key} is {shown} at {at}."
        loss_key = next((k for k in keys if _lower_is_better(k)), None)
        if loss_key is not None and _overfits(series, loss_key, val):
            caption += (
                " The validation loss goes up while the training loss goes down: the model "
                "starts to overfit."
            )
    best = attrs.get("best_epoch")
    if int(attrs.get("stopped_early", 0) or 0) and attrs.get("epochs_run") is not None:
        caption += f" The training stopped early after epoch {int(attrs['epochs_run'])}."
    if best is not None and int(best) > 0:
        caption += f" The model keeps the weights of epoch {int(best)}."
    if log_keys:
        caption += f" The y axis of {', '.join(log_keys)} has a log scale."
    if group == TRAINING_GROUP and s.x == "step":
        caption += " The data has no values for each step, so the x axis is the epoch."
    return Figure(chart=chart, datasets={"curves": frame}, caption=caption)


# -------------------------------------------------------------------- confusion over epochs


class ConfusionEpochsSettings(RecipeSettings):
    epochs: list[int] | None = Field(
        None,
        min_length=1,
        max_length=MAX_EPOCH_PANELS,
        description="Epochs to show (default: the first, best and last, and evenly spaced)",
    )
    normalize: Literal["true", "none"] = Field(
        "true", description="true: the share of each true class (rows); none: counts"
    )
    max_classes: int = Field(20, ge=2, le=50, description="Other classes become (other)")
    columns: int = Field(3, ge=1, le=6)
    width: int = Field(220, ge=80, le=2000)
    height: int = Field(220, ge=60, le=2000)


def _pick_epochs(available: list[int], best: int | None, count: int) -> list[int]:
    """First, best and last, then evenly spaced epochs until ``count``; sorted."""
    chosen = {available[0], available[-1]}
    if best is not None and best in available:
        chosen.add(best)
    for i in _thin(len(available), count):
        if len(chosen) >= count:
            break
        chosen.add(available[int(i)])
    return sorted(chosen)


@recipe(
    "ml.confusion_epochs",
    title="Confusion matrix over epochs",
    settings=ConfusionEpochsSettings,
    persona="expert",
    summary="The validation confusion matrix at some epochs of the training. It shows which "
    "classes the network learns first and which classes it mixes up.",
    tags=["ml", "classification"],
)
def confusion_epochs(inp: RecipeInput, s: ConfusionEpochsSettings) -> Figure:
    """Small multiples of the row-normalised confusion matrix at <= 6 epochs."""
    ds = inp.find(TRAINING_GROUP)
    if ds is None or "confusion" not in ds.data_vars:
        raise RecipeDataError(
            "the data has no training.confusion; a classifier fit with validation data writes it"
        )
    da = ds["confusion"]
    for dim in ("epoch", "true_class", "pred_class"):
        if dim not in da.dims:
            raise RecipeDataError(f"training.confusion has no {dim!r} dim")
    da = da.transpose("epoch", "true_class", "pred_class")
    available = [int(e) for e in coord_values(da, "epoch")]
    if not available:
        raise RecipeDataError("training.confusion has no epochs")
    best_attr = ds.attrs.get("best_epoch")
    best = int(best_attr) if best_attr is not None else None
    if s.epochs:
        epochs = [e for e in dict.fromkeys(s.epochs) if e in available]
        if not epochs:
            raise RecipeDataError(
                f"none of the epochs {s.epochs} has a confusion matrix "
                f"(available: {available[0]} to {available[-1]})"
            )
        epochs.sort()
    else:
        epochs = _pick_epochs(available, best, MAX_EPOCH_PANELS)
    true_cls = [str(c) for c in coord_values(da, "true_class")]
    pred_cls = [str(c) for c in coord_values(da, "pred_class")]
    if true_cls != pred_cls:
        raise RecipeDataError("true_class and pred_class must list the same classes")
    counts = np.nan_to_num(np.asarray(da.values, dtype=float)).astype(np.int64)
    pos = {e: available.index(e) for e in epochs}
    k = len(true_cls)
    if k > s.max_classes:
        support = counts[pos[epochs[-1]]].sum(axis=1)
        top = sorted(range(k), key=lambda j: (-int(support[j]), true_cls[j]))
        shown = sorted(top[: s.max_classes - 1])
        group = np.full(k, len(shown), dtype=np.int64)
        group[shown] = np.arange(len(shown))
        labels = [true_cls[j] for j in shown] + [OTHER]
    else:
        group = np.arange(k)
        labels = true_cls
    m = len(labels)

    def panel(e: int) -> str:
        tags = [
            t
            for t, hit in (
                ("first", e == available[0]),
                ("best", e == best),
                ("last", e == available[-1]),
            )
            if hit
        ]
        return f"epoch {e}" + (f" ({', '.join(tags)})" if tags else "")

    rows = []
    accuracy: dict[int, float] = {}
    for e in epochs:
        full = counts[pos[e]]
        total = int(full.sum())
        accuracy[e] = float(np.trace(full) / total) if total else float("nan")
        mat = np.zeros((m, m), dtype=np.int64)
        np.add.at(mat, (group[:, None].repeat(k, 1), group[None, :].repeat(k, 0)), full)
        for i, act in enumerate(labels):
            row_total = int(mat[i].sum())
            for j, predicted in enumerate(labels):
                count = int(mat[i, j])
                rate = count / row_total if row_total else float("nan")
                value = rate if s.normalize == "true" else float(count)
                rows.append(
                    {
                        "panel": panel(e),
                        "epoch": e,
                        "true": act,
                        "predicted": predicted,
                        "count": count,
                        "rate": rate,
                        "value": value,
                        "label": pct(rate) if s.normalize == "true" else str(count),
                    }
                )
    matrix = pd.DataFrame(rows)
    top_value = float(np.nanmax(matrix["value"].to_numpy())) if len(matrix) else 1.0
    if not np.isfinite(top_value) or top_value <= 0:
        top_value = 1.0
    matrix["dark"] = matrix["value"] > 0.6 * top_value
    title = "share of the true class" if s.normalize == "true" else "validation images"
    base = alt.Chart().encode(
        x=alt.X("predicted:N", sort=labels, title="predicted class"),
        y=alt.Y("true:N", sort=labels, title="true class"),
    )
    layers: list[Any] = [
        base.mark_rect(stroke=SURFACE, strokeWidth=1).encode(
            color=alt.Color(
                "value:Q",
                title=title,
                scale=alt.Scale(domain=[0, top_value], range=[EMPTY, ESTIMATE]),
            ),
            tooltip=[
                "panel:N",
                "true:N",
                "predicted:N",
                "count:Q",
                alt.Tooltip("rate:Q", format=".1%", title="share of the row"),
            ],
        )
    ]
    if m <= 8:
        layers.append(
            base.mark_text(fontSize=10).encode(
                text="label:N",
                color=alt.condition(alt.datum.dark, alt.value(SURFACE), alt.value(OBSERVED)),
            )
        )
    side = max(s.width, s.height, 16 * m)
    chart = wrap_facet(
        alt.layer(*layers).properties(width=side, height=side),
        "panel",
        [panel(e) for e in epochs],
        columns=s.columns,
        data=named("matrix"),
    )
    first, last = epochs[0], epochs[-1]
    parts = [f"{pct(accuracy[first])} at epoch {first}"]
    if best is not None and best in accuracy and best not in (first, last):
        parts.append(f"{pct(accuracy[best])} at the best epoch ({best})")
    if last != first:
        parts.append(f"{pct(accuracy[last])} at epoch {last}")
    caption = (
        f"Each matrix counts the validation images by the true class (rows) and the "
        f"predicted class (columns) at one epoch, for {k} classes. "
        + (
            "Each row shows the share of the images of a true class. "
            if s.normalize == "true"
            else ""
        )
        + "The validation accuracy is "
        + _join(parts)
        + "."
    )
    if k > s.max_classes:
        caption += (
            f" The matrices show the {s.max_classes - 1} classes with the most images. "
            f"{OTHER} holds the other {k - s.max_classes + 1}."
        )
    if len(available) > len(epochs) and not s.epochs:
        caption += f" The plot shows {len(epochs)} of the {len(available)} kept epochs."
    return Figure(chart=chart, datasets={"matrix": matrix}, caption=caption)


# ---------------------------------------------------------------- misclassified examples


class MisclassifiedSettings(RecipeSettings):
    max_items: int = Field(24, ge=1, le=32, description="Show at most this many images")
    columns: int = Field(6, ge=1, le=12)
    width: int = Field(900, ge=200, le=2000)
    height: int = Field(160, ge=60, le=2000, description="Height of each row of images")


def _domains(
    n: int, columns: int, hgap: float, vgap: float
) -> list[tuple[list[float], list[float]]]:
    cols = max(1, min(columns, n))
    rows = math.ceil(n / cols)
    w = (1 - (cols - 1) * hgap) / cols
    h = (1 - (rows - 1) * vgap) / rows
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        x0 = c * (w + hgap)
        y1 = 1 - r * (h + vgap)
        out.append(([round(x0, 4), round(x0 + w, 4)], [round(y1 - h, 4), round(y1, 4)]))
    return out


def _rgb(thumb: np.ndarray) -> np.ndarray:
    """``(h, w, 3)`` uint8 from a ``(h, w, c)`` thumbnail (gray repeated, alpha dropped)."""
    arr = np.asarray(thumb)
    if arr.dtype != np.uint8:
        f = np.nan_to_num(arr.astype(float))
        f = f * 255.0 if f.size and f.max() <= 1.0 else f
        arr = np.clip(np.rint(f), 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = arr[:, :, None]
    if arr.shape[2] == 1:
        return np.repeat(arr, 3, axis=2)
    return arr[:, :, :3]


def _text(value: Any, limit: int = 40) -> str:
    """Untrusted label text for a Plotly annotation: escaped and cut."""
    raw = str(value)
    raw = raw if len(raw) <= limit else raw[: limit - 1] + "…"
    return html.escape(raw, quote=False)


@recipe(
    "ml.misclassified",
    title="Wrong validation images",
    settings=MisclassifiedSettings,
    persona="both",
    summary="The validation images that the network classified wrong, with the true and the "
    "predicted class.",
    tags=["ml", "classification", "image"],
)
def misclassified(inp: RecipeInput, s: MisclassifiedSettings) -> Figure:
    """A grid of the wrong validation images (Plotly ``image`` traces with inline pixels)."""
    ds = inp.find(MISCLASSIFIED_GROUP)
    if ds is None or "thumb" not in ds.data_vars:
        raise RecipeDataError(
            "the data has no misclassified group; a classifier fit with validation images writes it"
        )
    thumb = ds["thumb"]
    dims = [str(d) for d in thumb.dims]
    if len(dims) not in (3, 4):
        raise RecipeDataError("thumb must have the dims (example, ty, tx[, channel])")
    ex_dim = dims[0]
    n_total = int(thumb.sizes[ex_dim])
    attrs = ds.attrs
    n_wrong = int(attrs.get("n_wrong", n_total))
    if n_total == 0:
        raise RecipeDataError("the misclassified group has no images")

    def column(name: str, default: Any) -> np.ndarray:
        if name in ds.coords or name in ds.data_vars:
            return np.asarray(ds[name].values)
        return np.full(n_total, default, dtype=object)

    prob = column("prob", np.nan).astype(float)
    true_prob = column("true_prob", np.nan).astype(float)
    true_label = column("true_label", "?")
    pred_label = column("pred_label", "?")
    image_id = column("image_id", "")
    order = np.argsort(-np.nan_to_num(prob, nan=-1.0), kind="stable")[: s.max_items]
    pixels = np.asarray(thumb.values)
    if pixels.ndim == 3:
        pixels = pixels[..., None]
    h, w = int(pixels.shape[1]), int(pixels.shape[2])
    n = len(order)
    nodes = n * h * (4 * w + 1)
    stride = max(1, math.ceil(math.sqrt(nodes / PIXEL_NODE_BUDGET)))
    cols = max(1, min(s.columns, n))
    rows = math.ceil(n / cols)
    doms = _domains(n, cols, hgap=0.02, vgap=0.12 / max(1, rows) if rows > 1 else 0.0)
    traces: list[dict[str, Any]] = []
    layout: dict[str, Any] = {
        "width": s.width,
        "height": rows * s.height + 30,
        "margin": {"l": 8, "r": 8, "t": 30, "b": 8},
        "showlegend": False,
        "annotations": [],
    }
    for i, (idx, (dx, dy)) in enumerate(zip(order, doms, strict=True)):
        aid = "" if i == 0 else str(i + 1)
        img = _rgb(pixels[int(idx)])[::stride, ::stride]
        traces.append(
            {
                "type": "image",
                "z": img.astype(int).tolist(),
                "colormodel": "rgb",
                "xaxis": f"x{aid}",
                "yaxis": f"y{aid}",
                "hoverinfo": "skip",
            }
        )
        axis = {"showticklabels": False, "showgrid": False, "zeroline": False, "ticks": ""}
        layout[f"xaxis{aid}"] = {**axis, "domain": dx, "anchor": f"y{aid}"}
        layout[f"yaxis{aid}"] = {
            **axis,
            "domain": dy,
            "anchor": f"x{aid}",
            "scaleanchor": f"x{aid}",
            "autorange": "reversed",
        }
        p = prob[int(idx)]
        p_text = f" (p={p:.2f})" if np.isfinite(p) else ""
        layout["annotations"].append(
            {
                "text": f"{_text(true_label[int(idx)])} → {_text(pred_label[int(idx)])}{p_text}",
                "xref": "paper",
                "yref": "paper",
                "x": (dx[0] + dx[1]) / 2,
                "y": dy[1],
                "xanchor": "center",
                "yanchor": "bottom",
                "showarrow": False,
                "font": {"size": 10},
            }
        )
    n_val = attrs.get("n_val")
    epoch = attrs.get("epoch")
    caption = (
        f"{n_wrong} of {int(n_val)} validation images are wrong"
        if n_val is not None
        else f"{n_wrong} validation images are wrong"
    )
    caption += f" at epoch {int(epoch)}." if epoch is not None else "."
    caption += (
        f" The grid shows {n} of them, in order of the probability of the wrong class. Each "
        "title gives the true class, the predicted class and its probability."
    )
    low = true_prob[order]
    if np.isfinite(low).any():
        caption += (
            f" The mean probability of the true class for these images is "
            f"{fmt(float(np.nanmean(low)))}."
        )
    if stride > 1:
        caption += f" The images show one pixel in {stride} in each direction."
    if n_wrong > n:
        caption += f" The plot shows {n} of the {n_wrong} wrong images."
    # the shown images as a table (export and data inspection); the pixels stay inline
    shown = pd.DataFrame(
        {
            "image_id": [str(v) for v in image_id[order]],
            "true_label": [str(v) for v in true_label[order]],
            "pred_label": [str(v) for v in pred_label[order]],
            "prob": prob[order],
            "true_prob": true_prob[order],
        }
    )
    return Figure(
        chart={"data": traces, "layout": layout},
        datasets={"examples": shown},
        caption=caption,
        kind="plotly",
    )
