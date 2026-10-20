"""Causal-inference domain pack: effect of an intervention, pre-period fit, donors, event study.

Plugins opt in by listing the ids in their manifest's ``plots`` (``causal.impact`` ...). Inputs
follow the groups the causal plugins (``mapi_plugin_causal``) store in the bundle, all in
target units:

* ``impact``: ``actual([unit,] time)``, ``counterfactual(sample, [unit,] time)`` (predictive
  draws of the target without the intervention), ``effect(sample, [unit,] time)`` and
  ``post([unit,] time)`` (the effect window; ``(unit, time)`` for staggered adoption), with
  attrs ``intervention_start`` / ``intervention_end``. ``causal.impact`` has no ``unit`` dim;
  synthetic control and DiD have one (treated units only);
* ``donor_weights``: ``weight(sample, unit, donor)`` and ``pre_rmse(unit)``, attr ``prior``;
* ``event_study``: ``coef(sample, event_time)`` (the reference period is exactly 0),
  ``n_cells(event_time)``, ``att(sample)``, attrs ``reference``, ``binned``, ``design``.

Sample dims are ``sample`` or ``chain``/``draw``. With a ``unit`` dim and no ``unit`` setting
the charts show the sum over the units; the cumulative effect is recomputed from ``effect``
and ``post`` so that sums over units with different windows stay consistent. Datasets carry
medians and intervals per period, never the draws.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from pydantic import Field, field_validator

from mapi_plots.palette import ALERT, BAND, ESTIMATE, MUTED, OBSERVED, REFERENCE, STRONG
from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    VlType,
    named,
    recipe,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    coord_values,
    fmt,
    has_samples,
    pct,
    stack_samples,
    time_dim,
)

__all__ = [
    "DonorWeightSettings",
    "EventStudySettings",
    "ImpactSettings",
    "PreFitSettings",
    "causal_recipes",
    "donor_weights",
    "event_study",
    "impact",
    "pre_fit",
]

UNIT = "unit"
Panel = Literal["series", "pointwise", "cumulative"]
PANELS: tuple[Panel, ...] = ("series", "pointwise", "cumulative")
_PANEL_TITLES = {
    "series": "actual and without the intervention",
    "pointwise": "effect per period",
    "cumulative": "cumulative effect",
}
COVERAGE_FLAG = 0.8


# ------------------------------------------------------------------ helpers


def _var(ds: xr.Dataset, name: str, group: str) -> xr.DataArray:
    if name not in ds.data_vars:
        raise RecipeDataError(f"the {group} group needs a {name!r} variable")
    return ds[name]


def _draws(da: xr.DataArray) -> xr.DataArray:
    """``(sample, ...)`` draws: ``sample`` or ``chain``/``draw`` flattened; one draw without."""
    return stack_samples(da) if has_samples(da) else da.expand_dims({SAMPLE: [0]})


def _bands(mat: np.ndarray, prob: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(median, lo, hi)`` per column of a ``(sample, n)`` matrix (HDI at ``prob``); NaN for
    columns with fewer than two finite draws."""
    mat = np.asarray(mat, dtype=float)
    n = mat.shape[1]
    med, lo, hi = (np.full(n, np.nan) for _ in range(3))
    finite = np.isfinite(mat)
    full = finite.all(axis=0)
    if mat.shape[0] < 2:
        med[full] = lo[full] = hi[full] = mat[0, full]
        return med, lo, hi
    if full.any():
        sub = mat[:, full]
        med[full] = np.median(sub, axis=0)
        lo[full], hi[full] = _stats.hdi(sub, prob, axis=0)
    for j in np.flatnonzero(~full & (finite.sum(axis=0) >= 2)):
        x = mat[finite[:, j], j]
        med[j] = float(np.median(x))
        lo[j], hi[j] = (float(v) for v in _stats.hdi(x, prob))
    return med, lo, hi


def _summary(x: np.ndarray, prob: float) -> tuple[float, float, float]:
    med, lo, hi = _bands(np.asarray(x, dtype=float).reshape(-1, 1), prob)
    return float(med[0]), float(lo[0]), float(hi[0])


def _when(x: Any) -> str:
    if isinstance(x, np.datetime64 | pd.Timestamp):
        return str(pd.Timestamp(x).date())
    if isinstance(x, np.generic):
        return str(x.item())
    return str(x)


def _x_type(xs: np.ndarray) -> VlType:
    if np.issubdtype(xs.dtype, np.datetime64):
        return "temporal"
    if np.issubdtype(xs.dtype, np.number):
        return "quantitative"
    return "ordinal"


def _signed(x: float, as_pct: bool = False) -> str:
    if not np.isfinite(x):
        return "n/a"
    text = pct(abs(x)) if as_pct else fmt(abs(x))
    return ("−" if x < 0 else "+" if x > 0 else "") + text


def _units_word(units: str) -> str:
    return f" {units}" if units else ""


@dataclass(slots=True)
class _Impact:
    """One series of an ``impact`` group: one unit or the sum over the units."""

    time: str
    xs: np.ndarray
    actual: np.ndarray  # (T,)
    counterfactual: np.ndarray  # (S, T)
    effect: np.ndarray  # (S, T)
    cumulative: np.ndarray  # (S, T), NaN outside the effect window
    post: np.ndarray  # (T,) bool: any unit in its effect window
    pre: np.ndarray  # (T,) bool: before the first window cell and in no window
    start: Any
    end: Any
    scope: str  # "" | "for <unit>" | "summed over n units"
    units: str
    n_units: int

    @property
    def x_type(self) -> VlType:
        return _x_type(self.xs)

    def total(self) -> np.ndarray:
        """Total effect over the effect window, per draw."""
        return np.nansum(np.where(self.post, np.nan_to_num(self.effect), 0.0), axis=1)

    def relative(self) -> np.ndarray:
        cf = np.nansum(np.where(self.post, np.nan_to_num(self.counterfactual), 0.0), axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(cf != 0, self.total() / np.abs(cf), np.nan)


def _labels(da: xr.DataArray | xr.Dataset, dim: str) -> list[str]:
    return [_when(v) for v in coord_values(da, dim)]


def _pick_unit(obj: xr.DataArray | xr.Dataset, unit: str | None) -> int | None:
    """Position of ``unit`` on the ``unit`` dim (``None``: no selection)."""
    if unit is None:
        return None
    if UNIT not in obj.dims:
        raise RecipeDataError(f"the data has no unit dim, so it cannot show unit {unit!r}")
    labels = _labels(obj, UNIT)
    if unit not in labels:
        raise RecipeDataError(f"no unit {unit!r} (units: {', '.join(labels)})")
    return labels.index(unit)


def _time_label(value: Any, xs: np.ndarray) -> Any:
    """An ``intervention_start``/``_end`` attr as a value of the time coordinate (``None``
    when empty or not on the axis)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if np.issubdtype(xs.dtype, np.datetime64):
        try:
            return pd.Timestamp(str(value)).to_datetime64()
        except ValueError:
            return None
    if np.issubdtype(xs.dtype, np.number):
        try:
            num = float(value)
            return int(num) if np.issubdtype(xs.dtype, np.integer) and num.is_integer() else num
        except (TypeError, ValueError):
            return None
    return str(value)


def _load_impact(inp: RecipeInput, unit: str | None) -> _Impact:
    ds = inp.require("impact")
    actual = _var(ds, "actual", "impact")
    cf = _draws(_var(ds, "counterfactual", "impact"))
    effect = _draws(ds["effect"]) if "effect" in ds.data_vars else actual - cf
    post = _var(ds, "post", "impact").astype(bool)
    t = time_dim(actual) or time_dim(cf)
    if t is None or t not in cf.dims:
        raise RecipeDataError("the impact group needs a time dim")
    has_unit = UNIT in cf.dims
    n_units = int(cf.sizes[UNIT]) if has_unit else 1
    scope = ""
    if has_unit:
        pos = _pick_unit(cf, unit)
        template = cf.isel({SAMPLE: 0}, drop=True)
        post_u = post.broadcast_like(template).transpose(UNIT, t)
        masked = effect.where(post_u, 0.0)
        if UNIT not in actual.dims:
            actual = actual.broadcast_like(template)
        if pos is not None:
            sel = {UNIT: pos}
            actual, cf, effect = actual.isel(sel), cf.isel(sel), effect.isel(sel)
            masked, post_any = masked.isel(sel), post_u.isel(sel)
            scope = f"for {unit}"
        else:
            actual = actual.sum(UNIT, skipna=False)
            cf, effect = cf.sum(UNIT, skipna=False), effect.sum(UNIT, skipna=False)
            masked, post_any = masked.fillna(0.0).sum(UNIT), post_u.any(UNIT)
            labels = _labels(post_u, UNIT)
            scope = f"summed over {n_units} units" if n_units > 1 else f"for {labels[0]}"
    else:
        if unit is not None:
            raise RecipeDataError(f"the data has no unit dim, so it cannot show unit {unit!r}")
        masked, post_any = effect.where(post, 0.0), post
    xs = np.asarray(coord_values(cf, t))
    cfm = np.asarray(cf.transpose(SAMPLE, t).values, dtype=float)
    eff = np.asarray(effect.transpose(SAMPLE, t).values, dtype=float)
    postv = np.asarray(post_any.values, dtype=bool).reshape(-1)
    cum = np.cumsum(np.nan_to_num(np.asarray(masked.transpose(SAMPLE, t).values)), axis=1)
    cum = np.where(postv, cum, np.nan)
    first = int(np.argmax(postv)) if postv.any() else len(xs)
    pre = ~postv & (np.arange(len(xs)) < first)
    start = _time_label(ds.attrs.get("intervention_start"), xs)
    if start is None and postv.any():
        start = xs[first]
    end = _time_label(ds.attrs.get("intervention_end"), xs)
    return _Impact(
        time=t,
        xs=xs,
        actual=np.asarray(actual.values, dtype=float).reshape(-1),
        counterfactual=cfm,
        effect=eff,
        cumulative=cum,
        post=postv,
        pre=pre,
        start=start,
        end=end,
        scope=scope,
        units=str(ds.attrs.get("units") or ""),
        n_units=n_units,
    )


def _rules(imp: _Impact, x: Any) -> list[Any]:
    """Vertical rules at the start (solid) and the end (dashed) of the intervention."""
    out: list[Any] = []
    for col, dash in (("start", []), ("end", [4, 4])):
        out.append(
            alt.Chart()
            .transform_filter(alt.FieldEqualPredicate(field="marker_" + col, equal=True))
            .mark_rule(color=ALERT, strokeDash=dash)
            .encode(x=x)
        )
    return out


def _markers(imp: _Impact, frame: pd.DataFrame) -> pd.DataFrame:
    """``marker_start``/``marker_end`` flags on the rows of the start and the end."""
    for col, value in (("start", imp.start), ("end", imp.end)):
        if value is None:
            frame["marker_" + col] = False
            continue
        if imp.x_type == "ordinal":
            hit = np.asarray([_when(v) == _when(value) for v in imp.xs])
        else:
            hit = np.asarray(imp.xs == value)
            if not hit.any() and imp.x_type == "quantitative":
                hit = np.isclose(imp.xs.astype(float), float(value))
        frame["marker_" + col] = hit
    return frame


def _x_values(imp: _Impact) -> Any:
    return [_when(v) for v in imp.xs] if imp.x_type == "ordinal" else imp.xs


# ------------------------------------------------------------------ impact


class ImpactSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    unit: str | None = Field(None, description="One treated unit (default: the sum over units)")
    panels: list[Panel] = Field(default_factory=lambda: list(PANELS), min_length=1)

    @field_validator("panels")
    @classmethod
    def _panels(cls, v: list[Panel]) -> list[Panel]:
        return [p for p in PANELS if p in v]


def _effect_sentence(imp: _Impact, prob: float) -> str:
    if not imp.post.any():
        return " The data has no period after the start, so the chart shows no effect."
    total = imp.total()
    point, lo, hi = _summary(total, prob)
    rel = _summary(imp.relative(), prob)[0]
    p_pos = float(np.mean(total > 0))
    u = _units_word(imp.units)
    text = (
        f" In the effect window the total effect is {_signed(point)}{u} "
        f"({pct(prob)} interval {fmt(lo)} to {fmt(hi)})."
    )
    if np.isfinite(rel):
        text += f" This is {_signed(rel, as_pct=True)} of the value without the intervention."
    text += f" The probability that the effect is above zero is {pct(p_pos)}."
    if lo <= 0 <= hi:
        text += " The interval includes zero, so do not report the effect as a fact."
    return text


@recipe(
    "causal.impact",
    title="Effect of the intervention",
    settings=ImpactSettings,
    persona="both",
    requires_groups=["impact"],
    summary="The actual values beside the values without the intervention, with the effect "
    "per period and the cumulative effect.",
    tags=["causal"],
)
def impact(inp: RecipeInput, s: ImpactSettings) -> Figure:
    """Actual vs counterfactual (median + HDI), pointwise and cumulative effect, start rule."""
    imp = _load_impact(inp, s.unit)
    cf, cf_lo, cf_hi = _bands(imp.counterfactual, s.hdi_prob)
    ef, ef_lo, ef_hi = _bands(imp.effect, s.hdi_prob)
    cu, cu_lo, cu_hi = _bands(imp.cumulative, s.hdi_prob)
    frame = pd.DataFrame(
        {
            "x": _x_values(imp),
            "actual": imp.actual,
            "cf": cf,
            "cf_lo": cf_lo,
            "cf_hi": cf_hi,
            "effect": ef,
            "effect_lo": ef_lo,
            "effect_hi": ef_hi,
            "cumulative": cu,
            "cumulative_lo": cu_lo,
            "cumulative_hi": cu_hi,
            "window": np.where(imp.post, "effect window", "before"),
        }
    )
    frame = _markers(imp, frame)
    xtype = imp.x_type
    x = alt.X("x", type=xtype, title=imp.time)
    tip_x = alt.Tooltip("x", type=xtype, title=imp.time)
    base = alt.Chart()
    zero = base.mark_rule(color=REFERENCE).encode(y=alt.datum(0))
    panels: list[Any] = []
    for p in s.panels:
        layers: list[Any] = []
        if p == "series":
            layers += [
                base.mark_area(opacity=0.2, color=BAND).encode(
                    x=x,
                    y=alt.Y("cf_lo:Q", title=imp.units or "value", scale=alt.Scale(zero=False)),
                    y2="cf_hi:Q",
                ),
                base.mark_line(color=ESTIMATE, strokeDash=[5, 3]).encode(x=x, y="cf:Q"),
                base.mark_line(color=OBSERVED).encode(
                    x=x,
                    y="actual:Q",
                    tooltip=[
                        tip_x,
                        alt.Tooltip("actual:Q", format=".4~s"),
                        alt.Tooltip("cf:Q", title="without the intervention", format=".4~s"),
                    ],
                ),
            ]
        else:
            col = "effect" if p == "pointwise" else "cumulative"
            layers += [
                zero,
                base.mark_area(opacity=0.2, color=BAND).encode(
                    x=x, y=alt.Y(f"{col}_lo:Q", title=imp.units or "effect"), y2=f"{col}_hi:Q"
                ),
                base.mark_line(color=ESTIMATE).encode(
                    x=x,
                    y=f"{col}:Q",
                    tooltip=[
                        tip_x,
                        alt.Tooltip(f"{col}:Q", format=".4~s"),
                        alt.Tooltip(f"{col}_lo:Q", title="from", format=".4~s"),
                        alt.Tooltip(f"{col}_hi:Q", title="to", format=".4~s"),
                    ],
                ),
            ]
        layers += _rules(imp, x)
        panels.append(
            alt.layer(*layers, data=named("impact")).properties(
                width=s.width, height=s.height, title=_PANEL_TITLES[p]
            )
        )
    chart = (
        panels[0]
        if len(panels) == 1
        else alt.vconcat(*panels).resolve_scale(x="shared", y="independent", color="independent")
    )
    scope = f" ({imp.scope})" if imp.scope else ""
    caption = (
        f"The line is the actual value{scope}. The dashed line and the band are the median and "
        f"the {pct(s.hdi_prob)} interval of the value without the intervention."
    )
    if imp.start is not None:
        caption += f" The red line marks the start ({_when(imp.start)})"
        caption += f" and the dashed red line the end ({_when(imp.end)})." if imp.end else "."
    caption += _effect_sentence(imp, s.hdi_prob)
    return Figure(chart=chart, datasets={"impact": frame}, caption=caption)


# ------------------------------------------------------------------ pre-period fit


class PreFitSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    unit: str | None = Field(None, description="One treated unit (default: the sum over units)")


@recipe(
    "causal.pre_fit",
    title="Fit before the intervention",
    settings=PreFitSettings,
    persona="both",
    requires_groups=["impact"],
    summary="How well the values without the intervention follow the actual values in the "
    "period before the start.",
    tags=["causal"],
)
def pre_fit(inp: RecipeInput, s: PreFitSettings) -> Figure:
    """Pre-period actual vs counterfactual band; RMSE, MAPE and band coverage in the caption."""
    imp = _load_impact(inp, s.unit)
    if not imp.pre.any():
        raise RecipeDataError("the impact group has no period before the start")
    idx = np.flatnonzero(imp.pre)
    cf_draws = imp.counterfactual[:, idx]
    med, lo, hi = _bands(cf_draws, s.hdi_prob)
    actual = imp.actual[idx]
    mean = np.nanmean(cf_draws, axis=0) if cf_draws.size else med
    ok = np.isfinite(actual) & np.isfinite(mean)
    resid = actual[ok] - mean[ok]
    rmse = float(np.sqrt(np.mean(resid**2))) if resid.size else np.nan
    nz = ok & (actual != 0)
    mape = float(np.mean(np.abs((actual[nz] - mean[nz]) / actual[nz]))) if nz.any() else np.nan
    band_ok = ok & np.isfinite(lo) & np.isfinite(hi)
    inside = (actual >= lo) & (actual <= hi)
    coverage = float(np.mean(inside[band_ok])) if band_ok.any() else np.nan
    frame = pd.DataFrame(
        {
            "x": np.asarray(_x_values(imp))[idx],
            "actual": actual,
            "cf": med,
            "cf_lo": lo,
            "cf_hi": hi,
            "inside": np.where(inside, "inside", "outside"),
        }
    )
    xtype = imp.x_type
    x = alt.X("x", type=xtype, title=imp.time)
    base = alt.Chart()
    chart = alt.layer(
        base.mark_area(opacity=0.2, color=BAND).encode(
            x=x, y=alt.Y("cf_lo:Q", title="value", scale=alt.Scale(zero=False)), y2="cf_hi:Q"
        ),
        base.mark_line(color=ESTIMATE).encode(x=x, y="cf:Q"),
        base.mark_point(filled=True, size=24).encode(
            x=x,
            y="actual:Q",
            color=alt.Color(
                "inside:N",
                title=None,
                scale=alt.Scale(domain=["inside", "outside"], range=[OBSERVED, ALERT]),
            ),
            tooltip=[
                alt.Tooltip("x", type=xtype, title=imp.time),
                alt.Tooltip("actual:Q", format=".4~s"),
                alt.Tooltip("cf:Q", title="model", format=".4~s"),
                "inside:N",
            ],
        ),
        data=named("pre_fit"),
    ).properties(width=s.width, height=s.height)
    scope = f" ({imp.scope})" if imp.scope else ""
    u = _units_word(imp.units)
    caption = (
        f"The dots are the actual values before the start{scope}. The line and the band are "
        f"the median and the {pct(s.hdi_prob)} interval of the model without the intervention. "
        f"The typical difference (RMSE) is {fmt(rmse)}{u}"
        + (f" and the mean percentage error (MAPE) is {pct(mape)}." if np.isfinite(mape) else ".")
        + f" {pct(coverage)} of the actual values are inside the band."
    )
    if np.isfinite(coverage) and coverage < COVERAGE_FLAG:
        caption += (
            f" This is less than {pct(COVERAGE_FLAG)}: the model does not follow the period "
            "before the start well, so do not trust the effect. Add controls or donors, or "
            "use a longer period before the start."
        )
    else:
        caption += " The model follows the period before the start."
    return Figure(chart=chart, datasets={"pre_fit": frame}, caption=caption)


# ------------------------------------------------------------------ donor weights


class DonorWeightSettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)
    top: int = Field(15, ge=1, le=100, description="Donors to show; the rest are summed")
    unit: str | None = Field(
        None, description="One treated unit (default: a panel per unit up to 4, else the first)"
    )


OTHER_DONORS = "other donors"
MAX_FACETS = 4


def _donor_rows(w: np.ndarray, donors: Sequence[str], unit: str, s: DonorWeightSettings) -> Any:
    """Rows for one treated unit: top donors by mean weight, the rest as one row."""
    mean = np.nanmean(w, axis=0)
    order = np.argsort(-np.abs(mean), kind="stable")
    keep, rest = order[: s.top], order[s.top :]
    rows = []
    for rank, j in enumerate(keep):
        _, lo, hi = _summary(w[:, j], s.hdi_prob)
        rows.append(
            {
                "unit": unit,
                "donor": donors[j],
                "weight": float(mean[j]),
                "lo": lo,
                "hi": hi,
                "order": rank,
            }
        )
    if rest.size:
        other = np.nansum(w[:, rest], axis=1)
        _, lo, hi = _summary(other, s.hdi_prob)
        rows.append(
            {
                "unit": unit,
                "donor": f"{OTHER_DONORS} ({rest.size})",
                "weight": float(np.nanmean(other)),
                "lo": lo,
                "hi": hi,
                "order": len(keep),
            }
        )
    return rows


@recipe(
    "causal.donor_weights",
    title="Donor weights",
    settings=DonorWeightSettings,
    persona="both",
    requires_groups=["donor_weights"],
    summary="The weight of each donor market in the synthetic copy of a treated market.",
    tags=["causal"],
)
def donor_weights(inp: RecipeInput, s: DonorWeightSettings) -> Figure:
    """Mean weight per donor (bar) with its HDI (rule), top N, the rest summed."""
    ds = inp.require("donor_weights")
    w = _var(ds, "weight", "donor_weights")
    if "donor" not in w.dims:
        raise RecipeDataError("weight needs a donor dim")
    w = _draws(w)
    if UNIT not in w.dims:
        w = w.expand_dims({UNIT: ["treated"]}, axis=1)
    units = _labels(w, UNIT)
    donors = _labels(w, "donor")
    pos = _pick_unit(w, s.unit)
    shown = [pos] if pos is not None else list(range(min(len(units), MAX_FACETS)))
    if pos is None and len(units) > MAX_FACETS:
        shown = [0]
    mat = np.asarray(w.transpose(SAMPLE, UNIT, "donor").values, dtype=float)
    rows: list[dict[str, Any]] = []
    for i in shown:
        rows += _donor_rows(mat[:, i, :], donors, units[i], s)
    frame = pd.DataFrame(rows)
    base = alt.Chart()
    y = alt.Y("donor:N", sort=alt.EncodingSortField(field="order", op="min"), title=None)
    layer = alt.layer(
        base.mark_bar(color=BAND, opacity=0.6).encode(
            x=alt.X("weight:Q", title="weight"),
            y=y,
            tooltip=[
                "unit:N",
                "donor:N",
                alt.Tooltip("weight:Q", title="mean", format=".2f"),
                alt.Tooltip("lo:Q", title="from", format=".2f"),
                alt.Tooltip("hi:Q", title="to", format=".2f"),
            ],
        ),
        base.mark_rule(color=STRONG).encode(x="lo:Q", x2="hi:Q", y=y),
        base.mark_rule(color=REFERENCE).encode(x=alt.datum(0)),
    )
    chart: Any
    if len(shown) > 1:
        chart = (
            layer.properties(width=s.width, height=alt.Step(16))
            .facet(
                row=alt.Row("unit:N", title=inp.dim_label(UNIT), sort=[units[i] for i in shown]),
                data=named("weights"),
            )
            .resolve_scale(y="independent")
            .resolve_axis(x="independent", y="independent")
        )
    else:
        chart = alt.layer(*layer.layer, data=named("weights")).properties(
            width=s.width, height=alt.Step(16), title=units[shown[0]]
        )
    prior = str(ds.attrs.get("prior") or "simplex")
    caption = (
        "Each bar is the mean weight of a donor in the synthetic copy of the treated unit. "
        f"The line is the {pct(s.hdi_prob)} interval."
    )
    caption += (
        " The weights are positive and add up to 1."
        if prior == "simplex"
        else " The weights can be negative and do not add up to 1 (horseshoe prior)."
    )
    rmse = ds["pre_rmse"] if "pre_rmse" in ds.data_vars else None
    for i in shown[:MAX_FACETS]:
        sub = frame[(frame["unit"] == units[i]) & ~frame["donor"].str.startswith(OTHER_DONORS)]
        big = sub[sub["weight"].abs() >= 0.05]
        if big.empty:
            caption += f" No donor of {units[i]} has a weight of 5% or more."
        else:
            parts = [
                f"{d} ({pct(v)})" for d, v in zip(big["donor"][:3], big["weight"][:3], strict=True)
            ]
            caption += (
                f" {units[i]}: {len(big)} donors have a weight of 5% or more, the largest are "
                + (parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1])
                + "."
            )
        if rmse is not None:
            r = rmse.isel({UNIT: i}) if UNIT in rmse.dims else rmse
            caption += f" The typical difference before the start (RMSE) is {fmt(float(r))}."
    if pos is None and len(units) > MAX_FACETS:
        caption += (
            f" The data has {len(units)} treated units. The chart shows {units[0]}: select a "
            "unit to see another one."
        )
    return Figure(chart=chart, datasets={"weights": frame}, caption=caption)


# ------------------------------------------------------------------ event study


class EventStudySettings(RecipeSettings):
    hdi_prob: float = Field(0.9, gt=0, lt=1)


@recipe(
    "causal.event_study",
    title="Effect by period since the start",
    settings=EventStudySettings,
    persona="both",
    requires_groups=["event_study"],
    summary="The effect for each period before and after the start, with the reference period "
    "at zero.",
    tags=["causal"],
)
def event_study(inp: RecipeInput, s: EventStudySettings) -> Figure:
    """Event-study coefficients (median + HDI) over event time, zero line, reference marked."""
    ds = inp.require("event_study")
    coef = _draws(_var(ds, "coef", "event_study"))
    if "event_time" not in coef.dims:
        raise RecipeDataError("coef needs an event_time dim")
    ev = np.asarray(coord_values(coef, "event_time")).astype(int)
    mat = np.asarray(coef.transpose(SAMPLE, "event_time").values, dtype=float)
    med, lo, hi = _bands(mat, s.hdi_prob)
    reference = int(ds.attrs.get("reference", -1))
    n_cells = (
        np.asarray(ds["n_cells"].values).reshape(-1).astype(int)
        if "n_cells" in ds.data_vars
        else np.full(len(ev), -1)
    )
    kind = np.where(ev == reference, "reference", np.where(ev < 0, "before", "after"))
    frame = pd.DataFrame(
        {
            "event_time": ev,
            "coef": med,
            "lo": lo,
            "hi": hi,
            "period": kind,
            "n_cells": n_cells,
        }
    )
    kinds = ["before", "reference", "after"]
    base = alt.Chart()
    x = alt.X("event_time:Q", title="periods since the start", axis=alt.Axis(tickMinStep=1))
    chart = alt.layer(
        base.mark_rule(color=REFERENCE).encode(y=alt.datum(0)),
        base.mark_rule(color=ALERT, strokeDash=[4, 4]).encode(x=alt.datum(-0.5)),
        base.mark_rule(color=STRONG).encode(x=x, y=alt.Y("lo:Q", title="effect"), y2="hi:Q"),
        base.mark_point(filled=True, size=60).encode(
            x=x,
            y="coef:Q",
            color=alt.Color(
                "period:N",
                title=None,
                sort=kinds,
                scale=alt.Scale(domain=kinds, range=[MUTED, REFERENCE, ESTIMATE]),
            ),
            shape=alt.Shape("period:N", sort=kinds, legend=None),
            tooltip=[
                "event_time:Q",
                "period:N",
                alt.Tooltip("coef:Q", title="median", format=".3g"),
                alt.Tooltip("lo:Q", title="from", format=".3g"),
                alt.Tooltip("hi:Q", title="to", format=".3g"),
                alt.Tooltip("n_cells:Q", title="cells"),
            ],
        ),
        data=named("event_study"),
    ).properties(width=s.width, height=s.height)
    design = str(ds.attrs.get("design") or "event_study")
    caption = (
        "Each dot is the median effect in a period before or after the start, and the line is "
        f"its {pct(s.hdi_prob)} interval. Period {reference} is the reference, so its effect "
        "is 0. The dashed red line marks the start."
    )
    if "att" in ds.data_vars:
        att = np.asarray(_draws(ds["att"]).values, dtype=float).reshape(-1)
    else:
        post_cols = ev >= 0
        att = mat[:, post_cols].mean(axis=1) if post_cols.any() else np.full(1, np.nan)
    a_med, a_lo, a_hi = _summary(att, s.hdi_prob)
    caption += (
        f" The average effect after the start is {_signed(a_med)} ({pct(s.hdi_prob)} interval "
        f"{fmt(a_lo)} to {fmt(a_hi)})."
    )
    if design == "did":
        caption += " The model has one effect for all the periods after the start."
    pre = (ev < 0) & (ev != reference) & np.isfinite(lo)
    if not pre.any():
        caption += " There are no periods before the start to compare the trends."
    else:
        off = [int(e) for e in ev[pre & ((lo > 0) | (hi < 0))]]
        if off:
            caption += (
                " The trends before the start are different: the intervals of periods "
                f"{', '.join(map(str, off))} do not include zero. Do not trust the effect."
            )
        else:
            caption += " There is no sign of different trends before the start."
    if int(ds.attrs.get("binned", 0)):
        caption += " The first and the last dots include all the periods outside the window."
    return Figure(chart=chart, datasets={"event_study": frame}, caption=caption)


def causal_recipes() -> list[Recipe[Any]]:
    """The causal domain pack (entry point ``mapi.plots: causal``)."""
    return [impact, pre_fit, donor_weights, event_study]
