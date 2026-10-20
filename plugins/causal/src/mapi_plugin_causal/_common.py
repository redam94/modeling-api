"""Shared pieces of the ``causal.*`` models: windows, the ``impact`` group, summaries, tracking.

Every causal model stores the effect of the intervention as the plugin-owned ``impact``
bundle group (dims ``sample``, ``time`` and, for panel models, ``unit``):

* ``actual([unit,] time)`` - the observed target (NaN allowed);
* ``counterfactual(sample, [unit,] time)`` - predictive draws of the target without the
  intervention (observation noise included); in the pre-period this is the model's fit;
* ``effect`` = actual - counterfactual; ``cumulative_effect`` = its cumulative sum over the
  ``post`` cells (NaN outside them);
* ``post([unit,] time)`` - the cells of the effect window.

:func:`effect_summary` reduces the group to totals, averages and relative effects (summed
over the post cells and treated units); :func:`log_effect_summary` logs them with the v0.11
tracking API.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk.models import FitContext, ModelSettings
from mapi_sdk.summarise import select_draws, thin_index
from mapi_spec.errors import ErrorCode, MapiError
from mapi_spec.tracking import ParamValue
from pydantic import Field, field_validator, model_validator

__all__ = [
    "DONOR",
    "IMPACT",
    "IMPACT_DRAWS",
    "MIN_PRE",
    "SAMPLE",
    "TARGET",
    "TIME",
    "UNIT",
    "CausalBase",
    "CausalSettings",
    "Window",
    "crn_normal",
    "effect_summary",
    "impact_group",
    "label_value",
    "log_effect_summary",
    "parse_window",
    "split_labels",
    "thin_samples",
    "time_label",
    "unique_labels",
]

TIME: Final = "time"
UNIT: Final = "unit"
DONOR: Final = "donor"
SAMPLE: Final = "sample"
TARGET: Final = "y"
IMPACT: Final = "impact"
IMPACT_DRAWS: Final = 500
"""Most draws kept in a derived causal group (thinned with ``mapi_sdk.summarise``)."""
MIN_PRE: Final = 3
"""Fewest periods before the intervention start."""

_WINDOW_RE = r"^(\d{4}-\d{2}-\d{2}|-?\d+)$"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LABEL_RE = re.compile(r"[^A-Za-z0-9_.\-]")


class CausalSettings(ModelSettings):
    """The intervention window shared by every causal model."""

    intervention_start: str = Field(
        ...,
        pattern=_WINDOW_RE,
        description="First period of the intervention: an ISO date (YYYY-MM-DD) or a period number",
    )
    intervention_end: str | None = Field(
        None,
        description="Last period of the intervention (inclusive); empty: until the end of the data",
    )

    @field_validator("intervention_start", mode="before")
    @classmethod
    def _start_text(cls, v: Any) -> Any:
        return str(v).strip() if isinstance(v, (int, str)) and not isinstance(v, bool) else v

    @field_validator("intervention_start")
    @classmethod
    def _start_date(cls, v: str) -> str:
        return _calendar_date(v, "intervention_start")

    @field_validator("intervention_end", mode="before")
    @classmethod
    def _end_text(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            if re.match(_WINDOW_RE, v) is None:
                raise ValueError("intervention_end must be an ISO date (YYYY-MM-DD) or a number")
            _calendar_date(v, "intervention_end")
        return v

    @model_validator(mode="after")
    def _window_order(self) -> CausalSettings:
        start, end = self.intervention_start, self.intervention_end
        if end is None:
            return self
        start_date, end_date = bool(_DATE_RE.match(start)), bool(_DATE_RE.match(end))
        if start_date != end_date:
            raise ValueError("intervention_start and intervention_end must be the same kind")
        before = np.datetime64(end) < np.datetime64(start) if start_date else int(end) < int(start)
        if before:
            raise ValueError("intervention_end is before intervention_start")
        return self


def _calendar_date(v: str, name: str) -> str:
    """``v`` unchanged; a ``YYYY-MM-DD`` text must also be a real calendar date."""
    if _DATE_RE.match(v) is not None:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"{name} {v!r} is not a valid date") from None
    return v


def split_labels(v: str | list[str]) -> list[str]:
    """``"a, b"`` -> ``["a", "b"]`` (Guided "text" answers); lists are stripped, empties dropped."""
    items = v.split(",") if isinstance(v, str) else [str(x) for x in v]
    return [s.strip() for s in items if s.strip()]


@dataclass(frozen=True, slots=True)
class Window:
    """The intervention window as positions in the fitted time coords (inclusive)."""

    start: int
    end: int
    start_label: str
    end_label: str
    """ISO date / int as text; ``""`` when open (then ``end`` is the last position)."""
    open_end: bool

    def post_mask(self, n: int) -> np.ndarray:
        """``(n,)`` bool: the cells inside the window."""
        idx = np.arange(n)
        return (idx >= self.start) & (idx <= self.end)

    def pre_mask(self, n: int) -> np.ndarray:
        """``(n,)`` bool: the cells before the start."""
        return np.arange(n) < self.start


def time_label(value: Any) -> str:
    """One time coordinate as text: an ISO date for datetimes, ``str`` otherwise."""
    if isinstance(value, (np.datetime64, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    if isinstance(value, (np.integer, int)):
        return str(int(value))
    return str(value)


def parse_window(
    times: np.ndarray,
    start: str,
    end: str | None,
    *,
    min_pre: int = MIN_PRE,
    pointer: str = "/model/settings/intervention_start",
) -> Window:
    """Locate the intervention window in ``times`` (sorted ascending).

    Datetime coords: the start is the first period on or after the start date and the end
    the last period on or before the end date. Integer coords: both must match a period.
    """
    t = np.asarray(times)
    n = t.shape[0]
    end_pointer = pointer.rsplit("/", 1)[0] + "/intervention_end"
    if n == 0:
        raise MapiError("the data has no periods", code=ErrorCode.SETTINGS_INVALID, pointer=pointer)
    is_date = np.issubdtype(t.dtype, np.datetime64)

    def locate(text: str, ptr: str, *, first: bool) -> int:
        if is_date:
            if _DATE_RE.match(text) is None:
                raise MapiError(
                    f"{text!r} is not a date (YYYY-MM-DD); the time coordinates are dates",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer=ptr,
                )
            try:
                when = np.datetime64(text, "ns")
            except ValueError:
                raise MapiError(
                    f"{text!r} is not a valid date", code=ErrorCode.SETTINGS_INVALID, pointer=ptr
                ) from None
            tt = t.astype("datetime64[ns]")
            pos = int(np.searchsorted(tt, when, side="left" if first else "right"))
            return pos if first else pos - 1
        if np.issubdtype(t.dtype, np.integer):
            if _DATE_RE.match(text) is not None:
                raise MapiError(
                    f"{text!r} is a date, but the time coordinates are period numbers",
                    code=ErrorCode.SETTINGS_INVALID,
                    pointer=ptr,
                )
            hits = np.flatnonzero(t == int(text))
        else:
            hits = np.flatnonzero(t.astype(str) == text)
        if hits.size == 0:
            raise MapiError(
                f"the period {text!r} is not in the data",
                code=ErrorCode.SETTINGS_INVALID,
                pointer=ptr,
            )
        return int(hits[0])

    s = locate(start, pointer, first=True)
    if s >= n:
        raise MapiError(
            f"the intervention start {start!r} is after the last period of the data",
            code=ErrorCode.SETTINGS_INVALID,
            pointer=pointer,
        )
    if s < min_pre:
        raise MapiError(
            f"the intervention start {start!r} leaves {s} periods before it; "
            f"at least {min_pre} are necessary",
            code=ErrorCode.SETTINGS_INVALID,
            pointer=pointer,
        )
    if end is None or end == "":
        return Window(s, n - 1, time_label(t[s]), "", True)
    e = locate(end, end_pointer, first=False)
    if e < s:
        raise MapiError(
            f"no period of the data is in the window {start!r} .. {end!r}",
            code=ErrorCode.SETTINGS_INVALID,
            pointer=end_pointer,
        )
    e = min(e, n - 1)
    return Window(s, e, time_label(t[s]), time_label(t[e]), False)


def label_value(text: str) -> str:
    """A tracking label value (``^[A-Za-z0-9_.\\-]{1,63}$``) made from any text."""
    return _LABEL_RE.sub("_", str(text))[:63] or "_"


def unique_labels(texts: Sequence[str]) -> dict[str, str]:
    """Text -> a unique label value (``_2``, ``_3`` ... when two texts sanitise alike, e.g.
    ``store 1`` and ``store_1``), so per-unit metrics never overwrite each other."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for text in texts:
        key = str(text)
        if key in out:
            continue
        base = label_value(key)
        label, k = base, 1
        while label in used:
            k += 1
            suffix = f"_{k}"
            label = base[: 63 - len(suffix)] + suffix
        used.add(label)
        out[key] = label
    return out


def thin_samples(posterior: xr.Dataset, n: int = IMPACT_DRAWS, seed: int = 0) -> xr.Dataset:
    """At most ``n`` draws of a ``(chain, draw)`` posterior as a ``sample`` dim."""
    index = thin_index(posterior.sizes["chain"], posterior.sizes["draw"], seed=seed)
    return select_draws(posterior, index, n)


def crn_normal(seed: int, name: str, shape: tuple[int, ...]) -> np.ndarray:
    """Common-random-number standard normals (re-export of ``mapi_plugin_ts.ss_math``)."""
    from mapi_plugin_ts.ss_math import crn_normal as _crn

    return _crn(seed, name, shape)


def impact_group(
    times: np.ndarray,
    actual: np.ndarray,
    counterfactual: np.ndarray,
    post: np.ndarray,
    *,
    units: Sequence[str] | None,
    window: Window,
    model_ref: str,
    units_label: str | None,
) -> xr.Dataset:
    """The ``impact`` group (target units) from actual values and counterfactual draws.

    ``actual`` is ``(n,)`` or ``(U, n)``, ``counterfactual`` ``(S, n)`` or ``(S, U, n)`` and
    ``post`` ``(n,)`` or ``(U, n)``; ``units`` labels the ``unit`` dim (``None``: no unit dim).
    """
    act = np.asarray(actual, dtype=np.float64)
    cf = np.asarray(counterfactual, dtype=np.float64)
    if units is None:
        act, cf = act[None, :], cf[:, None, :]
        post_arr = np.broadcast_to(np.asarray(post, dtype=bool), act.shape)
    else:
        post_arr = np.broadcast_to(np.asarray(post, dtype=bool), act.shape)
    if cf.shape[1:] != act.shape:
        raise ValueError(f"counterfactual shape {cf.shape} does not match actual {act.shape}")
    effect = act[None] - cf
    cum = np.where(post_arr[None], np.nan_to_num(effect, nan=0.0), 0.0).cumsum(axis=-1)
    cum = np.where(post_arr[None], cum, np.nan)
    if units is None:
        dims_u: tuple[str, ...] = (TIME,)
        act, post_arr = act[0], post_arr[0]
        cf, effect, cum = cf[:, 0], effect[:, 0], cum[:, 0]
    else:
        dims_u = (UNIT, TIME)
    dims_s = (SAMPLE, *dims_u)
    coords: dict[str, Any] = {SAMPLE: np.arange(cf.shape[0]), TIME: np.asarray(times)}
    if units is not None:
        coords[UNIT] = [str(u) for u in units]
    return xr.Dataset(
        {
            "actual": (dims_u, act),
            "counterfactual": (dims_s, cf),
            "effect": (dims_s, effect),
            "cumulative_effect": (dims_s, cum),
            "post": (dims_u, np.array(post_arr, dtype=bool)),
        },
        coords=coords,
        attrs={
            "intervention_start": window.start_label,
            "intervention_end": "" if window.open_end else window.end_label,
            "model": model_ref,
            "units": units_label or "",
            "effect_kind": "predictive",
        },
    )


def _stats(x: np.ndarray, hdi_prob: float) -> dict[str, float]:
    """Mean, sd and the equal-tailed interval of draws ``x`` (keys ``p05``/``p95`` at 0.9)."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    if x.size == 0:
        nan = float("nan")
        return {"mean": nan, "sd": nan, "p05": nan, "p95": nan, "p_positive": nan}
    tail = (1.0 - hdi_prob) / 2.0
    lo, hi = np.quantile(x, [tail, 1.0 - tail])
    return {
        "mean": float(x.mean()),
        "sd": float(x.std(ddof=1)) if x.size > 1 else 0.0,
        "p05": float(lo),
        "p95": float(hi),
        "p_positive": float((x > 0).mean()),
    }


def _as_units(impact: xr.Dataset) -> tuple[list[str], xr.Dataset]:
    """The group with a ``unit`` dim (a single ``"all"`` unit when it has none)."""
    if UNIT in impact.dims:
        return [str(u) for u in impact[UNIT].values], impact
    return [], impact.expand_dims({UNIT: ["all"]})


def _summarise_cells(
    act: np.ndarray, cf: np.ndarray, post: np.ndarray, hdi_prob: float
) -> dict[str, Any]:
    """Summaries over the cells of ``post`` (arrays ``(U, n)``, ``(S, U, n)``, ``(U, n)``)."""
    observed = np.isfinite(act)
    cells = post & observed
    eff = act[None] - cf
    total = np.where(cells[None], eff, 0.0).sum(axis=(1, 2))
    cf_sum = np.where(cells[None], cf, 0.0).sum(axis=(1, 2))
    n_cells = int(cells.sum())
    with np.errstate(divide="ignore", invalid="ignore"):
        average = total / n_cells if n_cells else np.full_like(total, np.nan)
        relative = np.where(cf_sum != 0, total / cf_sum, np.nan)
    # pre-period: cells before the first post cell of each unit
    first_post = np.where(post.any(axis=1), post.argmax(axis=1), post.shape[1])
    pre = (np.arange(post.shape[1])[None, :] < first_post[:, None]) & observed
    cf_mean = cf.mean(axis=0)
    resid = (act - cf_mean)[pre]
    actual_pre = act[pre]
    pre_rmse = float(np.sqrt(np.mean(resid**2))) if resid.size else float("nan")
    nz = actual_pre != 0
    pre_mape = float(np.mean(np.abs(resid[nz] / actual_pre[nz]))) if nz.any() else float("nan")
    return {
        "total": _stats(total, hdi_prob),
        "average": _stats(average, hdi_prob),
        "relative": _stats(relative, hdi_prob),
        "pre_rmse": pre_rmse,
        "pre_mape": pre_mape,
        "n_post": n_cells,
        "n_pre": int(pre.sum()),
    }


def effect_summary(impact: xr.Dataset, *, hdi_prob: float = 0.9) -> dict[str, Any]:
    """Totals of an ``impact`` group over its post cells and treated units.

    Returns ``{"total", "average", "relative"}`` (each ``{mean, sd, p05, p95, p_positive}``;
    ``p05``/``p95`` are the equal-tailed ``hdi_prob`` bounds), ``pre_rmse`` (target units),
    ``pre_mape`` (a fraction), ``n_pre``/``n_post`` (observed cells), ``n_treated`` and
    ``by_unit`` (``{label: {...}}``; empty without a ``unit`` dim). ``average`` is the effect
    per post cell, ``relative`` the total over the summed counterfactual.
    """
    labels, ds = _as_units(impact)
    act = np.asarray(ds["actual"].transpose(UNIT, TIME).values, dtype=np.float64)
    cf = np.asarray(ds["counterfactual"].transpose(SAMPLE, UNIT, TIME).values, dtype=np.float64)
    post = np.asarray(ds["post"].transpose(UNIT, TIME).values, dtype=bool)
    out = _summarise_cells(act, cf, post, hdi_prob)
    out["n_treated"] = max(len(labels), 1)
    out["by_unit"] = {
        label: _summarise_cells(act[i : i + 1], cf[:, i : i + 1], post[i : i + 1], hdi_prob)
        for i, label in enumerate(labels)
    }
    return out


def log_effect_summary(
    ctx: FitContext, impact: xr.Dataset, *, params: Mapping[str, ParamValue]
) -> None:
    """Log the I-10 causal params and effect metrics (no-op unless ``ctx.tracking``)."""
    if not ctx.tracking:
        return
    summary = effect_summary(impact)
    ctx.log_params(
        {
            "causal.intervention_start": str(impact.attrs.get("intervention_start", "")),
            "causal.intervention_end": str(impact.attrs.get("intervention_end", "")),
            "causal.n_pre": summary["n_pre"],
            "causal.n_post": summary["n_post"],
            "causal.n_treated": summary["n_treated"],
            **params,
        }
    )
    total = summary["total"]
    ctx.log_metrics(
        {
            "effect_total": total["mean"],
            "effect_total_sd": total["sd"],
            "effect_total_p05": total["p05"],
            "effect_total_p95": total["p95"],
            "effect_average": summary["average"]["mean"],
            "effect_relative": summary["relative"]["mean"],
            "p_effect_positive": total["p_positive"],
            "pre_rmse": summary["pre_rmse"],
            "pre_mape": summary["pre_mape"],
        }
    )
    if len(summary["by_unit"]) > 1:
        names = unique_labels(list(summary["by_unit"]))
        for label, unit in summary["by_unit"].items():
            ctx.log_metrics(
                {
                    "effect_total": unit["total"]["mean"],
                    "effect_relative": unit["relative"]["mean"],
                    "p_effect_positive": unit["total"]["p_positive"],
                },
                labels={"unit": names[str(label)]},
            )


class CausalBase:
    """Mixin with the data checks every causal model shares (optional for subclasses)."""

    @staticmethod
    def check_time(times: np.ndarray, *, pointer: str = "/data") -> None:
        """The time coords must be unique and increasing."""
        t = np.asarray(times)
        if t.size > 1 and not bool(np.all(t[1:] > t[:-1])):
            raise MapiError(
                "the time periods must be unique and in increasing order",
                code=ErrorCode.SCHEMA_INVALID,
                pointer=pointer,
            )
