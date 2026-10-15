"""Tabular machine-learning domain pack: feature importance.

Plugins opt in by listing ``ml.feature_importance`` in their manifest's or spec's ``plots``.
Inputs follow the groups the tabular plugins (``mapi_sklearn``) store in the bundle:

* ``feature_importance``: ``importance(feature)`` from tree ensembles (impurity based);
* ``coefficients``: ``coefficient(feature)`` (optionally ``(model, feature)`` when one model
  is fitted per quantile level) plus a scalar ``intercept`` from linear estimators.

Both are looked up at the root, under ``model/`` or anywhere in the tree, so the recipe works on
a bundle tree (``Bundle.to_tree()``) as well as on the bare group.
"""

from __future__ import annotations

from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field

from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
)
from mapi_plots.recipes._util import fmt, pct

__all__ = ["FeatureImportanceSettings", "feature_importance", "ml_recipes"]

IMPORTANCE_GROUPS = ("feature_importance", "importance")
COEFFICIENT_GROUPS = ("coefficients",)


class FeatureImportanceSettings(RecipeSettings):
    top: int = Field(20, ge=1, le=200, description="Show at most this many features")
    feature_dim: str = "feature"
    source: Literal["auto", "importance", "coefficients"] = Field(
        "auto", description="Which group to plot (auto: importances, else coefficients)"
    )


def _per_feature(
    ds: xr.Dataset, var: str, feature_dim: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, np.ndarray | None]:
    """(features, value, lo, hi): the mean over any extra dims plus its min/max range."""
    if var not in ds.data_vars:
        raise RecipeDataError(f"the group has no {var!r} variable")
    da = ds[var]
    if feature_dim not in da.dims:
        raise RecipeDataError(f"{var!r} has no {feature_dim!r} dim")
    extra = [str(d) for d in da.dims if d != feature_dim]
    feats = np.asarray(da[feature_dim].values).astype(str)
    if not extra:
        return feats, np.asarray(da.values, dtype=float), None, None
    return (
        feats,
        np.asarray(da.mean(extra).values, dtype=float),
        np.asarray(da.min(extra).values, dtype=float),
        np.asarray(da.max(extra).values, dtype=float),
    )


@recipe(
    "ml.feature_importance",
    title="Feature importance",
    settings=FeatureImportanceSettings,
    persona="expert",
    summary="Which features the tabular model relies on most (importances or coefficients).",
    tags=["ml"],
)
def feature_importance(inp: RecipeInput, s: FeatureImportanceSettings) -> Figure:
    """Ranked bars of tree importances, or of signed coefficients for linear estimators."""
    imp = inp.find(*IMPORTANCE_GROUPS) if s.source in ("auto", "importance") else None
    coef = inp.find(*COEFFICIENT_GROUPS) if imp is None and s.source != "importance" else None
    if imp is not None:
        kind = "importance"
        feats, val, lo, hi = _per_feature(imp, "importance", s.feature_dim)
        rank = val
    elif coef is not None:
        kind = "coefficient"
        feats, val, lo, hi = _per_feature(coef, "coefficient", s.feature_dim)
        rank = np.abs(val)
    else:
        raise RecipeDataError(
            "the data has neither a 'feature_importance' nor a 'coefficients' group; "
            "only tree and linear tabular estimators store one"
        )
    keep = np.isfinite(val)
    if not keep.any():
        raise RecipeDataError(f"every {kind} value is missing")
    frame = pd.DataFrame({"feature": feats, "value": val, "rank": rank})
    if lo is not None and hi is not None:
        frame["lo"], frame["hi"] = lo, hi
    frame = frame[keep].sort_values("rank", ascending=False, kind="stable")
    n_total = len(frame)
    frame = frame.head(s.top).drop(columns="rank").reset_index(drop=True)
    order = frame["feature"].tolist()
    y = alt.Y("feature:N", sort=order, title=None)
    title = "importance" if kind == "importance" else "coefficient (model scale)"
    layers: list[Any] = [
        alt.Chart()
        .mark_bar()
        .encode(
            x=alt.X("value:Q", title=title),
            y=y,
            color=(
                alt.condition("datum.value < 0", alt.value("#d62728"), alt.value("#1f77b4"))
                if kind == "coefficient"
                else alt.value("#1f77b4")
            ),
            tooltip=["feature:N", alt.Tooltip("value:Q", format=".4g")],
        )
    ]
    if "lo" in frame:
        layers.append(
            alt.Chart().mark_rule(color="#555").encode(x="lo:Q", x2="hi:Q", y=y),
        )
    chart = alt.layer(*layers, data=named("features")).properties(
        width=s.width, height=max(60, 18 * len(frame))
    )
    top = frame.iloc[0]
    shown = f"the top {len(frame)} of {n_total}" if n_total > len(frame) else f"all {n_total}"
    if kind == "importance":
        total = float(np.nansum(val[keep]))
        share = float(top["value"]) / total if total > 0 else float("nan")
        caption = (
            f"Showing {shown} features. The model relies most on {top['feature']}"
            + (f", which accounts for {pct(share)} of the total importance." if total > 0 else ".")
            + " Importances show what the model uses, not cause and effect."
        )
    else:
        direction = "raises" if float(top["value"]) >= 0 else "lowers"
        caption = (
            f"Showing {shown} features by the size of their coefficient. A one-unit increase "
            f"in {top['feature']} {direction} the prediction by {fmt(abs(float(top['value'])))} "
            "in model space, holding the other features fixed."
        )
    return Figure(chart=chart, datasets={"features": frame}, caption=caption)


def ml_recipes() -> list[Recipe[Any]]:
    """The tabular-ML domain pack (entry point ``mapi.plots: ml``)."""
    return [feature_importance]
