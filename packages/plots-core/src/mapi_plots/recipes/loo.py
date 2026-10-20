"""Leave-one-out recipe (Vega-Lite): LOO-PIT calibration and the Pareto k of each observation.

============================  ==========================================================
``core.loo_pit``              LOO-PIT ECDF difference with a simultaneous envelope, and
                              the PSIS Pareto k per observation against 0.5 / threshold
============================  ==========================================================

The importance weights come from :func:`mapi_plots.recipes._stats.psis` on the negative
pointwise log-likelihood (Vehtari, Gelman and Gabry 2017; Vehtari et al. 2024), so the
recipe needs only numpy. The LOO-PIT of observation ``i`` is
``sum_s w_si [yrep_si < y_i] + 0.5 sum_s w_si [yrep_si = y_i]``: like the PIT of
``core.calibration``, but each observation is predicted by a posterior that did not see it.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import Representation
from pydantic import Field

from mapi_plots.palette import ALERT, BAND, ESTIMATE, OK, REFERENCE, SCENARIO
from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
)
from mapi_plots.recipes._util import SAMPLE, observed_name, pct, pick_var, stack_samples
from mapi_plots.recipes.extras import _thin

__all__ = ["LooPitSettings", "loo_pit"]


class LooPitSettings(RecipeSettings):
    var: str | None = Field(None, description="log_likelihood variable (default: the first)")
    observed_var: str | None = None
    prob: float = Field(0.95, gt=0.5, lt=1, description="Envelope probability")
    n_sim: int = Field(1000, ge=100, le=10_000, description="Simulations for the envelope")
    seed: int = 0
    max_draws: int = Field(2000, ge=100, le=20_000)
    k_threshold: float = Field(0.7, ge=0.5, le=1.0, description="Pareto k above which LOO fails")


def _cells(da: xr.DataArray) -> tuple[np.ndarray, list[str], list[str]]:
    """``(values (sample, cell), cell labels, cell dims)`` of a sample-stacked array."""
    other = [str(d) for d in da.dims if d != SAMPLE]
    if not other:
        raise RecipeDataError(f"{da.name!r} has no observation dims (one value per draw)")
    st = da.stack(_cell=other).transpose(SAMPLE, "_cell")
    raw = st["_cell"].values
    labels = [", ".join(str(v) for v in np.atleast_1d(x)) for x in raw]
    return np.asarray(st.values, dtype=float), labels, other


def _ppc(
    inp: RecipeInput, name: str, s: LooPitSettings, ll: xr.DataArray, idx: np.ndarray
) -> tuple[np.ndarray, np.ndarray, str] | str:
    """``(yrep (S, N), y (N,), observed name)`` aligned with the log-likelihood cells, or the
    reason why the LOO-PIT panel is left out."""
    pp = inp.find("posterior_predictive")
    obs = inp.find("observed_data", "observed")
    if pp is None or obs is None:
        return "There are no posterior predictive draws, so the plot shows only the Pareto k."
    pname = name if name in pp.data_vars else None
    if pname is None and s.observed_var is not None and s.observed_var in pp.data_vars:
        pname = s.observed_var
    if pname is None and len(pp.data_vars) == 1:
        pname = str(next(iter(pp.data_vars)))
    oname = observed_name(obs, pname or name, s.observed_var)
    if pname is None or oname is None:
        return f"No predictive draws match {name!r}, so the plot shows only the Pareto k."
    rep = stack_samples(pp[pname])
    if rep.sizes[SAMPLE] != ll.sizes[SAMPLE]:
        return "The predictive draws and the log-likelihood have different draws."
    rep = rep.isel({SAMPLE: idx})
    o = obs[oname]
    dims = [d for d in ll.dims if d != SAMPLE]
    if set(rep.dims) != set(ll.dims) or set(o.dims) != set(dims):
        return "The predictive draws have other dims than the log-likelihood."
    try:
        rep, o, _ = xr.align(rep, o, ll, join="exact", exclude=[SAMPLE])
    except ValueError:
        return "The predictive draws have other labels than the log-likelihood."
    yrep = np.asarray(rep.transpose(SAMPLE, *dims).values, dtype=float).reshape(idx.size, -1)
    y = np.asarray(o.transpose(*dims).values, dtype=float).ravel()
    return yrep, y, oname


@recipe(
    "core.loo_pit",
    title="LOO-PIT and Pareto k",
    settings=LooPitSettings,
    persona="expert",
    requires_groups=["log_likelihood"],
    requires_representation=[Representation.DRAWS],
    summary="Checks if the model predicts each observation well when that observation is left "
    "out, and shows where the leave-one-out estimate is not reliable.",
    tags=["diagnostic"],
)
def loo_pit(inp: RecipeInput, s: LooPitSettings) -> Figure:
    """LOO-PIT ECDF difference with an envelope, plus PSIS Pareto k per observation."""
    lik = inp.require("log_likelihood")
    name = pick_var(lik, s.var, what="log-likelihood variable")
    ll = stack_samples(lik[name])
    idx = _thin(ll.sizes[SAMPLE], s.max_draws)
    ll = ll.isel({SAMPLE: idx})
    values, labels, _ = _cells(ll)
    finite = np.all(np.isfinite(values), axis=0)
    if not finite.any():
        raise RecipeDataError(f"{name!r} has no observation with finite log-likelihood")
    log_w, k = _stats.psis(-values[:, finite])
    cell = np.flatnonzero(finite)
    k_shown = np.where(np.isfinite(k), k, np.nan)
    status = np.where(k <= 0.5, "good", np.where(k <= s.k_threshold, "ok", "bad"))
    kframe = pd.DataFrame(
        {"index": cell, "observation": [labels[i] for i in cell], "k": k_shown, "status": status}
    )
    rules = pd.DataFrame({"k": [0.5, s.k_threshold], "rule": ["0.5", "threshold"]})
    k_scale = alt.Scale(domain=["good", "ok", "bad"], range=[OK, SCENARIO, ALERT])
    kbase = alt.Chart(named("pareto_k"))
    k_chart = alt.layer(
        alt.Chart(named("rules"))
        .mark_rule(color=REFERENCE, strokeDash=[4, 4])
        .encode(y=alt.Y("k:Q", title="Pareto k")),
        kbase.mark_point(filled=True, size=28).encode(
            x=alt.X("index:Q", title="observation"),
            y="k:Q",
            color=alt.Color("status:N", scale=k_scale, title="k"),
            tooltip=["observation:N", alt.Tooltip("k:Q", format=".2f"), "status:N"],
        ),
    ).properties(width=s.width, height=max(s.height, 200), title="Pareto k per observation")
    datasets: dict[str, pd.DataFrame] = {"pareto_k": kframe, "rules": rules}
    charts: list[Any] = []
    n = cell.size
    n_bad = int(np.sum(status == "bad"))
    caption = ""
    got = _ppc(inp, name, s, ll, idx)
    if isinstance(got, tuple):
        yrep, y, _ = got
        yrep, y = yrep[:, finite], y[finite]
        ok = np.isfinite(y) & np.all(np.isfinite(yrep), axis=0)
        w = np.exp(log_w[:, ok])
        pit = np.clip(
            (w * (yrep[:, ok] < y[None, ok])).sum(axis=0)
            + 0.5 * (w * (yrep[:, ok] == y[None, ok])).sum(axis=0),
            0.0,
            1.0,
        )
        pit = np.sort(pit)
        m = pit.size
        grid = int(min(max(m, 20), 200))
        z = np.arange(1, grid + 1) / (grid + 1)
        diff = np.searchsorted(pit, z, side="right") / m - z
        lo, hi = _stats.ecdf_envelope(m, z, s.prob, s.n_sim, s.seed)
        outside = (diff < lo) | (diff > hi)
        datasets["ecdf"] = pd.DataFrame(
            {
                "z": z,
                "diff": diff,
                "lo": lo,
                "hi": hi,
                "status": np.where(outside, "outside", "inside"),
            }
        )
        base = alt.Chart(named("ecdf"))
        charts.append(
            alt.layer(
                base.mark_area(color=BAND, opacity=0.18).encode(
                    x=alt.X("z:Q", title="LOO-PIT value", scale=alt.Scale(domain=[0, 1])),
                    y=alt.Y("lo:Q", title="ECDF − uniform"),
                    y2="hi:Q",
                ),
                base.mark_rule(color=REFERENCE, strokeDash=[4, 4]).encode(y=alt.datum(0.0)),
                base.mark_line(color=ESTIMATE, strokeWidth=1.8).encode(
                    x="z:Q",
                    y="diff:Q",
                    tooltip=[alt.Tooltip("z:Q", format=".2f"), alt.Tooltip("diff:Q", format=".3f")],
                ),
                base.transform_filter(alt.FieldEqualPredicate(field="status", equal="outside"))
                .mark_circle(color=ALERT, size=24)
                .encode(x="z:Q", y="diff:Q"),
            ).properties(
                width=s.width,
                height=max(s.height, 200),
                title=f"LOO-PIT ECDF difference ({pct(s.prob)} simultaneous envelope)",
            )
        )
        caption += (
            f"The left plot shows the LOO-PIT values of {name}: the line is the share of the "
            f"{m} observations below each value minus the share for a calibrated model. Each "
            "observation is predicted by the model without that observation."
        )
        if outside.any():
            caption += " The line goes outside the band, so the model is not calibrated."
        else:
            caption += " The line stays inside the band: no sign of bad calibration."
        caption += " The right plot"
    else:
        caption += got + " The plot"
    charts.append(k_chart)
    caption += (
        f" shows the Pareto k of each of the {n} observations. "
        f"{n_bad} {'observation' if n_bad == 1 else 'observations'} ({pct(n_bad / n)}) "
        f"{'has' if n_bad == 1 else 'have'} k above {s.k_threshold:g}."
    )
    if n_bad:
        worst = int(np.nanargmax(np.where(np.isfinite(k), k, np.inf)))
        caption += (
            f" The largest k is at the observation {labels[cell[worst]]}. For these "
            "observations, the leave-one-out estimate is not reliable, because each one has a "
            "large effect on the model. Do a check of these observations, or use a more robust "
            "model."
        )
    else:
        caption += " The leave-one-out estimates are reliable."
    chart = charts[0] if len(charts) == 1 else alt.hconcat(*charts)
    return Figure(chart=chart, datasets=datasets, caption=caption)
