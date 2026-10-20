"""Sampler energy (E-BFMI) and prior-against-posterior overlays.

* ``core.energy`` reads ``sample_stats.energy(chain, draw)``: per chain, E-BFMI =
  mean(diff(E)²) / var(E) (Betancourt 2016), the marginal energy and the energy transitions
  as centred histograms;
* ``core.prior_posterior`` reads the ``prior`` and ``posterior`` groups: one panel per
  parameter cell with both densities, ranked by how far the data moved the parameter.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import numpy as np
import pandas as pd
import xarray as xr
from mapi_spec.capabilities import Representation
from pydantic import Field

from mapi_plots.palette import ALERT, BAND, OK, REFERENCE
from mapi_plots.recipes import _stats
from mapi_plots.recipes._base import (
    Figure,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    named,
    recipe,
    wrap_facet,
)
from mapi_plots.recipes._util import (
    SAMPLE,
    element_label,
    fmt,
    has_samples,
    pct,
    stack_samples,
)

__all__ = [
    "EnergySettings",
    "PriorPosteriorSettings",
    "bfmi",
    "energy",
    "overlap",
    "prior_posterior",
]


def bfmi(energy: np.ndarray) -> np.ndarray:
    """E-BFMI of each chain of a ``(chain, draw)`` energy array (ArviZ's ``bfmi``)."""
    e = np.atleast_2d(np.asarray(energy, dtype=float))
    with np.errstate(divide="ignore", invalid="ignore"):
        out: np.ndarray = np.square(np.diff(e, axis=1)).mean(axis=1) / np.var(e, axis=1)
    return out


def overlap(a: np.ndarray, b: np.ndarray, grid: np.ndarray) -> float:
    """Overlap coefficient ∫ min(p, q) of two densities on a shared grid."""
    return float(np.clip(np.trapezoid(np.minimum(a, b), grid), 0.0, 1.0))


# ------------------------------------------------------------------------------ energy


class EnergySettings(RecipeSettings):
    bfmi_threshold: float = Field(0.3, gt=0, le=2, description="E-BFMI below this is a problem")
    bins: int = Field(40, ge=5, le=200)


@recipe(
    "core.energy",
    title="Sampler energy",
    settings=EnergySettings,
    persona="expert",
    requires_groups=["sample_stats"],
    requires_representation=[Representation.DRAWS],
    summary="The energy of the sampler and its change between draws, and the E-BFMI of each "
    "chain against a threshold.",
)
def energy(inp: RecipeInput, s: EnergySettings) -> Figure:
    """Centred marginal-energy and energy-transition histograms; E-BFMI bars per chain."""
    stats = inp.require("sample_stats")
    if "energy" not in stats.data_vars:
        raise RecipeDataError("sample_stats has no energy variable (an NUTS/HMC sampler stat)")
    da = stats["energy"]
    if "chain" not in da.dims:
        if "draw" not in da.dims:
            raise RecipeDataError("energy needs a draw dim")
        da = da.expand_dims(chain=[0])
    e = np.asarray(da.transpose("chain", "draw").values, dtype=float)
    keep = np.all(np.isfinite(e), axis=1) & (e.shape[1] > 2)
    if not keep.any():
        raise RecipeDataError("no chain has finite energy values")
    chains = [str(c) for c in (da["chain"].values if "chain" in da.coords else range(len(e)))]
    e = e[keep]
    chains = [c for c, k in zip(chains, keep, strict=True) if k]
    values = bfmi(e)
    marginal = (e - e.mean(axis=1, keepdims=True)).ravel()
    trans = np.diff(e, axis=1)
    trans = (trans - trans.mean(axis=1, keepdims=True)).ravel()
    lo, hi = np.percentile(np.r_[marginal, trans], [0.5, 99.5])
    edges = np.linspace(lo, hi, s.bins + 1)
    width = edges[1] - edges[0]
    parts = []
    for kind, x in (("marginal energy", marginal), ("energy transition", trans)):
        counts, _ = np.histogram(np.clip(x, lo, hi), bins=edges)
        parts.append(
            pd.DataFrame(
                {
                    "kind": kind,
                    "x": edges[:-1],
                    "x2": edges[1:],
                    "density": counts / (counts.sum() * width) if counts.sum() else 0.0,
                }
            )
        )
    hist = pd.concat(parts, ignore_index=True)
    status = np.where(values < s.bfmi_threshold, "low", "ok")
    table = pd.DataFrame({"chain": chains, "bfmi": values, "status": status})
    kinds = ["marginal energy", "energy transition"]
    hist_chart = (
        alt.Chart(named("energy"))
        .mark_rect(opacity=0.55)
        .encode(
            x=alt.X("x:Q", title="energy minus its mean"),
            x2="x2:Q",
            y=alt.Y("density:Q", title="density", stack=None),
            color=alt.Color(
                "kind:N",
                title=None,
                sort=kinds,
                scale=alt.Scale(domain=kinds, range=[REFERENCE, BAND]),
            ),
            tooltip=[
                "kind:N",
                alt.Tooltip("x:Q", format=".3g"),
                alt.Tooltip("density:Q", format=".3g"),
            ],
        )
        .properties(width=s.width, height=s.height, title="Energy and its transitions")
    )
    bbase = alt.Chart()
    bars = alt.layer(
        bbase.mark_bar().encode(
            x=alt.X("bfmi:Q", title="E-BFMI"),
            y=alt.Y("chain:N", title="chain", sort=chains),
            color=alt.Color(
                "status:N", title=None, scale=alt.Scale(domain=["ok", "low"], range=[OK, ALERT])
            ),
            tooltip=["chain:N", alt.Tooltip("bfmi:Q", format=".3f")],
        ),
        bbase.mark_rule(color=ALERT, strokeDash=[4, 4]).encode(x=alt.datum(s.bfmi_threshold)),
        data=named("bfmi"),
    ).properties(width=s.width, height=alt.Step(18), title="E-BFMI of each chain")
    chart = alt.vconcat(hist_chart, bars)
    caption = (
        "The grey histogram is the energy of the sampler. The blue histogram is the change of "
        "energy from one draw to the next. When the blue histogram is much narrower than the "
        f"grey one, the sampler does not explore the tails well. E-BFMI is from "
        f"{values.min():.2f} to {values.max():.2f}."
    )
    low = [c for c, v in zip(chains, values, strict=True) if v < s.bfmi_threshold]
    if low:
        caption += (
            f" {len(low)} of {len(chains)} chains {'has' if len(low) == 1 else 'have'} an E-BFMI "
            f"below {s.bfmi_threshold:g} "
            f"(chain {', '.join(low[:6])}). Do not trust the tails of the results. Change the "
            "parameterisation of the model or make the priors narrower."
        )
    else:
        caption += f" Every chain is above {s.bfmi_threshold:g}."
    return Figure(chart=chart, datasets={"energy": hist, "bfmi": table}, caption=caption)


# ----------------------------------------------------------------------- prior vs posterior


class PriorPosteriorSettings(RecipeSettings):
    var_names: list[str] | None = Field(None, max_length=200)
    max_vars: int = Field(12, ge=1, le=60, description="Variables to show at most")
    max_cells: int = Field(8, ge=1, le=60, description="Cells of one variable to show at most")
    points: int = Field(80, ge=20, le=400, description="Density points per curve")
    little: float = Field(0.8, gt=0, lt=1, description="Overlap above which the data adds little")


def _draws(da: xr.DataArray) -> xr.DataArray:
    return stack_samples(da) if has_samples(da) else da.expand_dims({SAMPLE: [0]})


def _cells(
    name: str, prior: xr.DataArray, post: xr.DataArray
) -> list[tuple[str, np.ndarray, np.ndarray]]:
    """``(label, prior draws, posterior draws)`` per element shared by both."""
    p, q = _draws(prior), _draws(post)
    other = [str(d) for d in q.dims if d != SAMPLE]
    if set(other) != {str(d) for d in p.dims if d != SAMPLE}:
        return []
    p, q = xr.align(p, q, join="inner", exclude=[SAMPLE])
    p = p.transpose(SAMPLE, *other)
    q = q.transpose(SAMPLE, *other)
    pm = np.asarray(p.values, dtype=float).reshape(p.sizes[SAMPLE], -1)
    qm = np.asarray(q.values, dtype=float).reshape(q.sizes[SAMPLE], -1)
    labels: list[str] = []
    if other:
        stacked = q.isel({SAMPLE: 0}).stack(_el=other)
        for i in range(stacked.sizes["_el"]):
            el = stacked.isel(_el=i)
            labels.append(element_label(name, {d: el[d].item() for d in other if d in el.coords}))
    else:
        labels = [name]
    return [(labels[i], pm[:, i], qm[:, i]) for i in range(pm.shape[1])]


@recipe(
    "core.prior_posterior",
    title="Prior and posterior",
    settings=PriorPosteriorSettings,
    persona="expert",
    requires_groups=["prior", "posterior"],
    requires_representation=[Representation.DRAWS],
    summary="The prior and the posterior of each parameter in one panel, sorted by how far "
    "the data moved the parameter.",
)
def prior_posterior(inp: RecipeInput, s: PriorPosteriorSettings) -> Figure:
    """Prior and posterior densities (Gaussian KDE) per parameter cell, ranked by
    |Δmean| / prior sd, with the overlap coefficient of each pair."""
    post = inp.require("posterior")
    prior = inp.require("prior")
    shared = [
        str(v)
        for v in post.data_vars
        if v in prior.data_vars
        and np.issubdtype(post[v].dtype, np.number)
        and has_samples(post[v])
        and has_samples(prior[v])
    ]
    if s.var_names is not None:
        unknown = [v for v in s.var_names if v not in shared]
        if unknown:
            raise RecipeDataError(f"not in both the prior and the posterior: {', '.join(unknown)}")
        shared = list(s.var_names)
    if not shared:
        raise RecipeDataError("no variable has draws in both the prior and the posterior")
    by_var: dict[str, list[dict[str, Any]]] = {}
    for name in shared:
        for label, p, q in _cells(name, prior[name], post[name]):
            p, q = p[np.isfinite(p)], q[np.isfinite(q)]
            if p.size < 2 or q.size < 2:
                continue
            sd = float(np.std(p, ddof=1))
            if not sd > 0:
                continue
            shift = abs(float(q.mean() - p.mean())) / sd
            by_var.setdefault(name, []).append(
                {"label": label, "prior": p, "posterior": q, "shift": shift, "sd": sd}
            )
    if not by_var:
        raise RecipeDataError("no parameter has enough finite prior and posterior draws")
    n_all = sum(len(v) for v in by_var.values())
    for name in by_var:
        by_var[name] = sorted(by_var[name], key=lambda c: -c["shift"])[: s.max_cells]
    ranked_vars = sorted(by_var, key=lambda n: -by_var[n][0]["shift"])[: s.max_vars]
    cells = sorted((c for n in ranked_vars for c in by_var[n]), key=lambda c: -c["shift"])
    parts, summary = [], []
    for c in cells:
        both = np.r_[c["prior"], c["posterior"]]
        lo, hi = np.percentile(both, [0.5, 99.5])
        qlo, qhi = np.percentile(c["posterior"], [0.5, 99.5])
        lo, hi = min(lo, qlo), max(hi, qhi)
        if hi <= lo:
            hi = lo + 1e-9
        grid = np.linspace(lo, hi, s.points)
        _, dp = _stats.kde(c["prior"], grid)
        _, dq = _stats.kde(c["posterior"], grid)
        ov = overlap(dp, dq, grid)
        c["overlap"] = ov
        for group, dens in (("prior", dp), ("posterior", dq)):
            parts.append(
                pd.DataFrame({"parameter": c["label"], "group": group, "x": grid, "density": dens})
            )
        summary.append(
            {
                "parameter": c["label"],
                "shift": c["shift"],
                "overlap": ov,
                "prior_mean": float(c["prior"].mean()),
                "posterior_mean": float(c["posterior"].mean()),
            }
        )
    dens = pd.concat(parts, ignore_index=True)
    table = pd.DataFrame(summary)
    dens = dens.merge(table[["parameter", "overlap"]], on="parameter")
    order = list(table["parameter"])
    groups = ["prior", "posterior"]
    layer = (
        alt.Chart()
        .mark_area(opacity=0.45, interpolate="monotone")
        .encode(
            x=alt.X("x:Q", title=None),
            y=alt.Y("density:Q", title=None, axis=None, stack=None),
            color=alt.Color(
                "group:N",
                title=None,
                sort=groups,
                scale=alt.Scale(domain=groups, range=[REFERENCE, BAND]),
            ),
            tooltip=["parameter:N", "group:N", alt.Tooltip("overlap:Q", format=".0%")],
        )
        .properties(width=max(80, s.width // 3 - 10), height=max(60, s.height // 2))
    )
    chart = wrap_facet(
        layer, "parameter", order, columns=3, data=named("densities"), independent_y=True
    ).resolve_scale(x="independent")
    top = table.iloc[0]
    caption = (
        "Each panel shows the prior (grey) and the posterior (blue) of one parameter. The "
        "panels are sorted by how far the data moved the parameter. "
        f"{top.parameter} moved most: its mean changed by {fmt(float(top['shift']))} prior "
        "standard deviations."
    )
    little = table[table["overlap"] > s.little]
    if len(little):
        caption += (
            f" The data adds little to {', '.join(little['parameter'].head(5))}: the prior and "
            f"the posterior overlap by more than {pct(s.little)}."
        )
    caption += (
        " Overlap: "
        + ", ".join(f"{r.parameter} {pct(float(r.overlap))}" for r in table.head(12).itertuples())
        + "."
    )
    if len(cells) < n_all:
        caption += f" The chart shows {len(cells)} of {n_all} parameter cells."
    return Figure(chart=chart, datasets={"densities": dens}, caption=caption)
