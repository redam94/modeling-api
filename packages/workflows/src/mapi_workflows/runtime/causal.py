"""Nodes on causal models (v0.13): ``model.lift_test``.

``model.lift_test`` turns the effect a causal model measured (``causal.impact``,
``causal.synthetic_control``, ``causal.did``) into a :class:`~mapi_spec.calibration.LiftTest`
that a marketing mix model takes in its ``calibration.lift_tests`` (the ``geo_lift_mmm``
template passes ``[nodes.lift.lift_test]`` to the MMM's ``model.fit``).

It reads only the bundle's ``impact`` group (dims ``sample``, ``time`` and, for panel models,
``unit``; vars ``effect``, ``counterfactual``, ``post``) and never imports plugin code, so it
runs in any runner image. Per draw, the total effect is the sum of ``effect`` over the
``post`` cells of every unit; ``kpi_delta`` is its mean, ``se`` its sd and ``ci`` its HDI at
``hdi_prob``. The test dates are the first and last post periods (they must be dates). A
test with exactly one treated unit is a test in that ``geo``; with several it is national.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Final

import numpy as np
import xarray as xr
from mapi_spec.calibration import LiftTest
from mapi_spec.errors import ErrorCode, MapiError
from mapi_workflows.payloads import TablePayload, clean_number
from mapi_workflows.runtime.data import data_error
from mapi_workflows.runtime.values import NodeContext, NodeOutcome
from pydantic import JsonValue, ValidationError

__all__ = ["CAUSAL_NODES", "IMPACT_GROUP", "lift_test_from_impact"]

IMPACT_GROUP: Final = "impact"
_TIME: Final = "time"
_UNIT: Final = "unit"
_SAMPLE: Final = "sample"
_NO_IMPACT: Final = (
    "model.lift_test needs a causal model (causal.impact, causal.synthetic_control or causal.did)"
)


def _hdi(x: np.ndarray, prob: float) -> tuple[float, float]:
    # the numpy HDI the plot recipes and model.summary use (no ArviZ needed)
    from mapi_plots.recipes import _stats

    lo, hi = _stats.hdi(x, prob)
    return float(lo), float(hi)


def _draws(da: xr.DataArray) -> xr.DataArray:
    """``da`` with one draw dim ``sample`` (``chain``/``draw`` are stacked)."""
    if _SAMPLE in da.dims:
        return da
    if {"chain", "draw"} <= set(map(str, da.dims)):
        return da.stack({_SAMPLE: ("chain", "draw")}).reset_index(_SAMPLE, drop=True)
    raise data_error("the impact group of the model has no draws (dim 'sample')")


def _iso_date(value: Any) -> str:
    if isinstance(value, np.datetime64) or np.issubdtype(np.asarray(value).dtype, np.datetime64):
        return str(np.datetime_as_string(np.datetime64(value, "D")))
    raise data_error(
        "lift tests need dated periods, but the time labels of the causal model are not "
        "dates. Use a dataset with a date dimension."
    )


def lift_test_from_impact(
    impact: xr.Dataset,
    *,
    channel: str,
    spend_delta: float,
    test_id: str | None,
    hdi_prob: float,
) -> dict[str, JsonValue]:
    """The ``model.lift_test`` result of an ``impact`` group (I-4 layout).

    Keys: ``lift_test`` (a validated :class:`LiftTest` as JSON), ``kpi_delta``, ``se``,
    ``hdi_low``, ``hdi_high``, ``p_positive``, ``excludes_zero``, ``relative_effect``,
    ``start``, ``end``, ``geo``, ``n_treated``, ``n_cells``, ``n_missing``, ``model``.
    """
    for var in ("effect", "post"):
        if var not in impact.data_vars:
            raise data_error(f"the impact group of the model has no variable {var!r}")
    if _TIME not in impact.dims:
        raise data_error("the impact group of the model has no 'time' dim")
    effect = _draws(impact["effect"])
    post = impact["post"].astype(bool)
    other = [d for d in post.dims if d not in (_TIME, _UNIT)]
    if other:
        raise data_error(f"the post cells of the impact group have unexpected dims {other}")
    # the cells of the test window, and which units and periods have any
    by_time = post.any(_UNIT) if _UNIT in post.dims else post
    times = np.asarray(impact[_TIME].values)[np.asarray(by_time.values, dtype=bool)]
    if times.size == 0:
        raise data_error("the causal model has no periods in the test window (post cells)")
    start, end = _iso_date(times.min()), _iso_date(times.max())
    if _UNIT in post.dims:
        has_post = np.asarray(post.any(_TIME).values, dtype=bool)
        treated = [str(u) for u, k in zip(impact[_UNIT].values, has_post, strict=True) if k]
    else:
        treated = []
    n_treated = max(len(treated), 1)
    geo = treated[0] if len(treated) == 1 else None

    cell_dims = [d for d in effect.dims if d != _SAMPLE]
    masked = effect.where(post)
    total = np.asarray(masked.sum(cell_dims, skipna=True).values, dtype=float)
    n_cells = int(post.sum())
    observed = masked.isel({_SAMPLE: 0}).notnull()
    n_missing = n_cells - int(observed.sum())
    total = total[np.isfinite(total)]
    if total.size < 2:
        raise data_error("the impact group has fewer than two draws of the effect")
    kpi_delta = float(total.mean())
    se = float(total.std(ddof=1))
    lo, hi = _hdi(total, hdi_prob)
    rel: float | None = None
    if "counterfactual" in impact.data_vars:
        cf = _draws(impact["counterfactual"]).where(post & observed)
        cf_total = float(np.nanmean(np.asarray(cf.sum(cell_dims, skipna=True).values)))
        rel = kpi_delta / cf_total if cf_total != 0 and math.isfinite(cf_total) else None
    model_ref = str(impact.attrs.get("model", "") or "")
    where = f"in {geo}" if geo is not None else f"in {n_treated} units (national)"
    doc: dict[str, Any] = {
        "channel": channel,
        "geo": geo,
        "start": start,
        "end": end,
        "spend_delta": float(spend_delta),
        "kpi_delta": kpi_delta,
        "description": f"{model_ref or 'causal model'}: {times.size} periods {where}"[:500],
    }
    if test_id:
        doc["id"] = test_id
    if lo < kpi_delta < hi:
        doc["ci"] = [lo, hi]
        doc["ci_level"] = hdi_prob
    elif se > 0:  # a degenerate or very skewed interval: give the standard error
        doc["se"] = se
    else:
        raise data_error("the effect in the test window has no uncertainty (sd 0)")
    try:
        lift = LiftTest.model_validate(doc)
    except ValidationError as exc:
        msg = "; ".join(str(e.get("msg", "")) for e in exc.errors())
        raise data_error(f"the result is not a valid lift test: {msg}") from None
    return {
        "lift_test": lift.model_dump(mode="json", exclude_none=True),
        "kpi_delta": clean_number(kpi_delta),
        "se": clean_number(se),
        "hdi_low": clean_number(lo),
        "hdi_high": clean_number(hi),
        "hdi_prob": hdi_prob,
        "p_positive": clean_number(float((total > 0).mean())),
        "excludes_zero": bool(lo > 0 or hi < 0),
        "relative_effect": clean_number(rel) if rel is not None else None,
        "start": start,
        "end": end,
        "geo": geo,
        "n_treated": n_treated,
        "n_cells": n_cells,
        "n_missing": n_missing,
        "model": model_ref or None,
    }


def _lift_test_node(ctx: NodeContext) -> NodeOutcome:
    mv = ctx.model("model")
    assert mv is not None
    impact = mv.bundle.model.groups.get(IMPACT_GROUP)
    if impact is None:
        raise MapiError(_NO_IMPACT, code=ErrorCode.CAPABILITY_UNSUPPORTED)
    channel = ctx.params.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        raise data_error("model.lift_test needs the channel of the test ('channel')")
    spend = clean_number(ctx.params.get("spend_delta"))
    if spend is None or spend == 0:
        raise data_error("model.lift_test needs a non-zero extra spend ('spend_delta')")
    raw_id = ctx.params.get("id")
    prob = clean_number(ctx.params.get("hdi_prob"))
    result = lift_test_from_impact(
        impact,
        channel=channel.strip(),
        spend_delta=spend,
        test_id=raw_id if isinstance(raw_id, str) and raw_id else None,
        hdi_prob=0.9 if prob is None else prob,
    )
    lift = result["lift_test"]
    assert isinstance(lift, dict)
    cols = ["channel", "geo", "start", "end", "spend_delta", "kpi_delta", "se", "hdi_low",
            "hdi_high", "p_positive"]  # fmt: skip
    row: dict[str, JsonValue] = {
        "channel": lift["channel"],
        "geo": result["geo"],
        "start": result["start"],
        "end": result["end"],
        "spend_delta": lift["spend_delta"],
        **{k: result[k] for k in cols[5:]},
    }
    table = TablePayload(
        title="Lift test",
        kind="lift_test",
        columns=cols,
        rows=[row],
        meta={"hdi_prob": result["hdi_prob"], "model": mv.model, "lift_test": lift},
    )
    if ctx.tracking:
        ctx.log_metrics(
            {
                k: float(v)
                for k in ("kpi_delta", "se", "p_positive")
                if isinstance(v := result[k], int | float)
            }
        )
    warns: list[str] = []
    n_treated = result["n_treated"]
    if result["geo"] is None and isinstance(n_treated, int) and n_treated > 1:
        warns.append(
            f"the test has {n_treated} treated units, so the lift test is national (no geo). "
            "The effect is the sum over these units."
        )
    n_missing = result["n_missing"]
    if isinstance(n_missing, int) and n_missing > 0:
        warns.append(
            f"{n_missing} cells of the test window have no observed value. The total does "
            "not include them."
        )
    return NodeOutcome(result=result, outputs={"table": table.model_dump(mode="json")},
                       warnings_plain=warns)  # fmt: skip


CAUSAL_NODES: Final[dict[str, Callable[[NodeContext], NodeOutcome]]] = {
    "model.lift_test": _lift_test_node,
}
