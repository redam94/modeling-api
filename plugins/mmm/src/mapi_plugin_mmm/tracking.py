"""Model-level run tracking of the MMM plugins (v0.11 tracking API).

At the end of a fit every MMM of this package logs, when the run has a tracker
(``ctx.tracking``):

* params - ``mmm.framework_version``, ``mmm.n_channels``, ``mmm.n_periods``,
  ``mmm.n_cells``, ``mmm.link`` and model-specific ones (``mmm.time_varying``,
  ``mmm.mediators``, ``mmm.outcomes``, ``mmm.cross_effects``); a text value longer than a
  param may be (``MAX_PARAM_VALUE_CHARS``, e.g. a long list of channel names) is cut short
  and ends in ``...``, so the fit never fails over it;
* scalar metrics per channel (label ``channel``): ``roi`` (posterior mean of the channel's
  KPI total over the fit window per unit of its spend) with ``roi_p05`` / ``roi_p95``,
  ``mroi`` (the marginal ROI at current spend, central difference at 1 +/- 1%) and
  ``contribution_share`` (of the expected KPI); and ``baseline_share``.

The numbers come from the fitted model's own forward pass over at most
:data:`SUMMARY_DRAWS` evenly thinned draws (:func:`summary_window`), in model-stage units.
A channel without spend gets no ROI.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Final

import numpy as np
import xarray as xr
from mapi_sdk.models import FitContext
from mapi_spec.tracking import MAX_PARAM_VALUE_CHARS, ParamValue

from mapi_plugin_mmm.base import draw_samples
from mapi_plugin_mmm.derived import FitWindow
from mapi_plugin_mmm.forward import ReplayLike
from mapi_plugin_mmm.framework import PanelData
from mapi_plugin_mmm.planning import thin_index

__all__ = [
    "SUMMARY_DRAWS",
    "channel_labels",
    "label_value",
    "log_mmm_summary",
    "param_value",
    "summary_window",
]

SUMMARY_DRAWS: Final = 200
_MROI_STEP: Final = 0.01
_LABEL_MAX: Final = 63


def label_value(text: str) -> str:
    """``text`` as a metric label value (``[A-Za-z0-9_.-]``, at most 63 characters)."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(text))[:_LABEL_MAX] or "_"


def param_value(value: ParamValue) -> ParamValue:
    """``value`` as a param value: text over ``MAX_PARAM_VALUE_CHARS`` is cut and ends in
    ``...`` (the tracker would refuse it after sampling and fail the fit)."""
    if isinstance(value, str) and len(value) > MAX_PARAM_VALUE_CHARS:
        return value[: MAX_PARAM_VALUE_CHARS - 3] + "..."
    return value


def channel_labels(channels: Sequence[str]) -> dict[str, str]:
    """Channel -> a unique label value (``_2``, ``_3`` ... when two sanitise alike)."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for ch in channels:
        base = label_value(ch)
        label, k = base, 1
        while label in used:
            k += 1
            suffix = f"_{k}"
            label = base[: _LABEL_MAX - len(suffix)] + suffix
        used.add(label)
        out[ch] = label
    return out


def _share(num: np.ndarray, den: np.ndarray) -> float:
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(den != 0, num / np.where(den != 0, den, 1.0), np.nan)
    finite = ratio[np.isfinite(ratio)]
    return float(finite.mean()) if finite.size else float("nan")


def log_mmm_summary(
    ctx: FitContext,
    win: FitWindow,
    *,
    params: Mapping[str, ParamValue] | None = None,
    labels: Mapping[str, str] | None = None,
) -> None:
    """Log ``params`` and the summary metrics of the fit window (see the module doc); a
    no-op without a tracker. ``labels`` are added to every metric."""
    if not ctx.tracking:
        return
    if params:
        ctx.log_params({k: param_value(v) for k, v in params.items()})
    extra = dict(labels or {})
    spend, _ = win.arrays()
    spend_c = spend.sum(axis=(0, 1))  # (C,)
    now = win.channel_totals(1.0).sum(axis=2)  # (S, C)
    lo = win.channel_totals(1.0 - _MROI_STEP).sum(axis=2)
    hi = win.channel_totals(1.0 + _MROI_STEP).sum(axis=2)
    expected = win.expected_cells().sum(axis=1)  # (S,)
    names = channel_labels(win.fit.channels)
    for c, ch in enumerate(win.fit.channels):
        metrics: dict[str, float] = {"contribution_share": _share(now[:, c], expected)}
        if spend_c[c] > 0:
            roi = now[:, c] / spend_c[c]
            metrics["roi"] = float(roi.mean())
            metrics["roi_p05"] = float(np.quantile(roi, 0.05))
            metrics["roi_p95"] = float(np.quantile(roi, 0.95))
            metrics["mroi"] = float(((hi[:, c] - lo[:, c]) / (2 * _MROI_STEP * spend_c[c])).mean())
        ctx.log_metrics(metrics, labels={"channel": names[ch], **extra})
    base = expected - now.sum(axis=1)
    ctx.log_metrics({"baseline_share": _share(base, expected)}, labels=extra or None)


def summary_window(
    replay_factory: Callable[[xr.Dataset], ReplayLike], posterior: xr.Dataset, fit: PanelData
) -> FitWindow:
    """The fit window over at most :data:`SUMMARY_DRAWS` evenly thinned draws of
    ``posterior`` (``(chain, draw)`` dims), replayed by ``replay_factory``."""
    chains, draws = int(posterior.sizes["chain"]), int(posterior.sizes["draw"])
    flat = thin_index(chains * draws, SUMMARY_DRAWS)
    index = np.stack([flat // draws, flat % draws], axis=1)
    return FitWindow(replay_factory(draw_samples(posterior, index)), fit)
