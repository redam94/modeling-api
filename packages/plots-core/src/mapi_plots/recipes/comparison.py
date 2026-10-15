"""Model comparison: an elpd forest with differences relative to the best model."""

from __future__ import annotations

from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field

from mapi_plots.palette import REFERENCE
from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
)
from mapi_plots.recipes._util import coord_values, flatten_samples, pick_var

__all__ = ["ComparisonSettings", "model_comparison"]

_ELPD_NAMES = ("elpd", "elpd_loo", "elpd_waic", "elpd_kfold")
_SE_NAMES = ("se", "elpd_se", "se_loo", "se_waic")


class ComparisonSettings(RecipeSettings):
    ic: Literal["auto", "waic"] = Field(
        "auto",
        description="auto: use a precomputed 'comparison' group (e.g. PSIS-LOO from the runner) "
        "or else WAIC from each model's log_likelihood; waic: always compute WAIC",
    )
    var: str | None = Field(None, description="log_likelihood variable (default: first)")
    model_dim: str = "model"


def _from_table(ds: xr.Dataset, dim: str) -> pd.DataFrame:
    if dim not in ds.dims:
        raise RecipeDataError(f"the comparison group needs a {dim!r} dim")
    elpd = next((n for n in _ELPD_NAMES if n in ds.data_vars), None)
    if elpd is None:
        raise RecipeDataError("the comparison group needs an 'elpd' variable")
    se = next((n for n in _SE_NAMES if n in ds.data_vars), None)
    frame = pd.DataFrame(
        {
            "model": [str(m) for m in coord_values(ds, dim)],
            "elpd": np.asarray(ds[elpd].values, dtype=float),
            "se": np.asarray(ds[se].values, dtype=float) if se else np.nan,
            "dse": np.asarray(ds["dse"].values, dtype=float) if "dse" in ds else np.nan,
        }
    )
    frame["ic"] = elpd.removeprefix("elpd_") if elpd != "elpd" else "elpd"
    if "weight" in ds:
        frame["weight"] = np.asarray(ds["weight"].values, dtype=float)
    return frame


def _from_log_lik(models: dict[str, xr.Dataset], var: str | None) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    pointwise: dict[str, np.ndarray] = {}
    for name, ll in models.items():
        v = pick_var(ll, var, what="log-likelihood variable")
        mat = flatten_samples(ll[v])
        elpd, se, p, pw = _stats.waic(mat)
        rows.append({"model": name, "elpd": elpd, "se": se, "p": p, "ic": "waic"})
        pointwise[name] = pw
    frame = pd.DataFrame(rows)
    best = frame.loc[frame["elpd"].idxmax(), "model"]
    dse = []
    for name in frame["model"]:
        a, b = pointwise[name], pointwise[str(best)]
        dse.append(float(np.sqrt(a.size * np.var(a - b))) if a.size == b.size else np.nan)
    frame["dse"] = dse
    return frame


@recipe(
    "core.model_comparison",
    title="Model comparison",
    settings=ComparisonSettings,
    persona="expert",
    summary="Expected predictive accuracy (elpd) per model with standard errors.",
)
def model_comparison(inp: RecipeInput, s: ComparisonSettings) -> Figure:
    """elpd ± SE per model and the difference to the best model ± its SE."""
    table = inp.find("comparison") if s.ic == "auto" else None
    if table is not None:
        frame = _from_table(table, s.model_dim)
    else:
        models = inp.children_with("log_likelihood")
        if len(models) < 2:
            raise RecipeDataError(
                "compare at least two models: give a 'comparison' group or one child per model "
                "with a log_likelihood group"
            )
        frame = _from_log_lik(models, s.var)
    frame = frame.sort_values("elpd", ascending=False).reset_index(drop=True)
    best = frame.iloc[0]
    frame["d_elpd"] = frame["elpd"] - best["elpd"]
    frame["lo"] = frame["elpd"] - frame["se"]
    frame["hi"] = frame["elpd"] + frame["se"]
    frame["d_lo"] = frame["d_elpd"] - frame["dse"].fillna(0)
    frame["d_hi"] = frame["d_elpd"] + frame["dse"].fillna(0)
    frame["rank"] = np.arange(len(frame))
    ic = str(frame["ic"].iloc[0])
    y = alt.Y("model:N", sort=alt.SortField("rank"), title=None)
    base = alt.Chart()
    left = alt.layer(
        base.mark_rule().encode(
            x=alt.X("lo:Q", title=f"elpd ({ic}) ± SE", scale=alt.Scale(zero=False)), x2="hi:Q", y=y
        ),
        base.mark_point(filled=True, size=70).encode(
            x="elpd:Q",
            y=y,
            tooltip=[
                "model:N",
                alt.Tooltip("elpd:Q", format=".1f"),
                alt.Tooltip("se:Q", format=".1f"),
            ],
        ),
        base.mark_rule(strokeDash=[4, 4], color=REFERENCE).encode(x=alt.datum(float(best["elpd"]))),
        data=named("comparison"),
    ).properties(width=s.width // 2, height=28 * len(frame), title="Predictive accuracy")
    right = alt.layer(
        base.mark_rule().encode(
            x=alt.X("d_lo:Q", title="difference to best ± SE"), x2="d_hi:Q", y=y
        ),
        base.mark_point(size=70).encode(
            x="d_elpd:Q",
            y=alt.Y("model:N", sort=alt.SortField("rank"), axis=None),
            tooltip=[
                "model:N",
                alt.Tooltip("d_elpd:Q", format=".1f"),
                alt.Tooltip("dse:Q", format=".1f"),
            ],
        ),
        base.mark_rule(color=REFERENCE).encode(x=alt.datum(0)),
        data=named("comparison"),
    ).properties(width=s.width // 2, height=28 * len(frame), title="Relative to the best")
    chart = alt.hconcat(left, right)
    caption = f"{best['model']} has the best expected predictive accuracy ({ic})."
    if len(frame) > 1:
        runner = frame.iloc[1]
        d, dse = abs(float(runner["d_elpd"])), float(runner["dse"])
        if d < 4:
            verdict = "a negligible difference"
        elif np.isfinite(dse) and d > 2 * dse:
            verdict = "a clear difference"
        else:
            verdict = "a difference smaller than its uncertainty"
        caption += (
            f" {runner['model']} is {d:.1f}"
            + (f" ± {dse:.1f}" if np.isfinite(dse) else "")
            + f" elpd worse: {verdict}."
        )
    if ic == "waic":
        caption += (
            " WAIC was computed from the stored log-likelihood; PSIS-LOO is preferred when "
            "available."
        )
    keep = ["model", "elpd", "se", "lo", "hi", "d_elpd", "dse", "d_lo", "d_hi", "rank", "ic"]
    return Figure(chart=chart, datasets={"comparison": frame[keep]}, caption=caption)
