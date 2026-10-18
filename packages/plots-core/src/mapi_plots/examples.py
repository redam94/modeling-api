"""Synthetic inputs for every recipe: DataTrees shaped like fitted bundles and score outputs.

Built with xarray only (no PyMC), so docs, demos and tests can render the whole catalog::

    from mapi_plots.examples import example_inputs
    from mapi_plots.recipes import get_recipe

    for recipe_id, (data, options) in example_inputs().items():
        plot = get_recipe(recipe_id).render(data, **options)
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr

__all__ = [
    "area_map_tree",
    "bundle_tree",
    "calibration_tree",
    "contributions",
    "counterfactual_tree",
    "decision_dataset",
    "ensemble_tree",
    "example_inputs",
    "feature_importance_tree",
    "field_tree",
    "group_effects_tree",
    "panel_data",
    "phylogeny_tree",
    "plans_dataset",
    "prior_beliefs_dataset",
    "profile_tree",
    "regime_tree",
    "response_curves",
    "rose_tree",
    "score_tree",
    "vector_tree",
]

GEOS = ["north", "south", "east"]
CHANNELS = ["tv", "search"]
CHAINS, DRAWS = 2, 120
WEEKS = pd.date_range("2024-01-07", periods=20, freq="W-SUN")


def _rng(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def panel_data() -> xr.Dataset:
    rng = _rng(1)
    y = 100 + 10 * rng.standard_normal((3, 20)) + np.linspace(0, 20, 20)
    y[0, 3] = np.nan
    spend = rng.gamma(2.0, 5.0, (3, 20, 2))
    spend[2, :4, 1] = np.nan
    return xr.Dataset(
        {
            "revenue": (("geo", "week"), y, {"role": "target", "units": "EUR"}),
            "spend": (("geo", "week", "channel"), spend, {"role": "spend"}),
        },
        coords={"geo": GEOS, "week": WEEKS, "channel": CHANNELS},
    )


def _draws(shape: tuple[int, ...], seed: int, loc: float = 0.0, scale: float = 1.0) -> np.ndarray:
    return loc + scale * _rng(seed).standard_normal((CHAINS, DRAWS, *shape))


def bundle_tree(bias: float = 0.0) -> xr.DataTree:
    """A fitted-bundle-like tree: ``model/{posterior, sample_stats, prior_predictive, ...}``
    plus ``derived/{contributions, response_curves}``. ``bias`` (in noise SDs) shifts the
    predictions away from the data, making a worse model for comparisons."""
    obs = panel_data()
    mu = obs["revenue"].fillna(110).values
    coords = {"chain": np.arange(CHAINS), "draw": np.arange(DRAWS)}
    posterior = xr.Dataset(
        {
            "alpha": (("chain", "draw", "geo"), _draws((3,), 2, 100, 3)),
            "beta": (("chain", "draw", "channel"), _draws((2,), 3, 0.5, 0.2)),
            "sigma": (("chain", "draw"), np.abs(_draws((), 4, 10, 1))),
        },
        coords={**coords, "geo": GEOS, "channel": CHANNELS},
    )
    # make one parameter clearly unconverged: chains disagree
    posterior["beta"].values[1, :, 0] += 3.0
    centre = mu[None, None] + 10 * bias + 2 * _rng(13).standard_normal((CHAINS, DRAWS, 1, 1))
    pp = centre + 10 * _rng(5).standard_normal((CHAINS, DRAWS, 3, 20))
    prior = 100 + 60 * _rng(6).standard_normal((CHAINS, DRAWS, 3, 20))
    ll = -0.5 * ((mu[None, None] - centre) / 10) ** 2 - np.log(10 * np.sqrt(2 * np.pi))
    dims = ("chain", "draw", "geo", "week")
    pc = {**coords, "geo": GEOS, "week": WEEKS}
    diverging = np.zeros((CHAINS, DRAWS), dtype=bool)
    diverging[0, :3] = True
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-123"}),
            "/model/posterior": posterior,
            "/model/sample_stats": xr.Dataset(
                {"diverging": (("chain", "draw"), diverging)}, coords=coords
            ),
            "/model/posterior_predictive": xr.Dataset({"revenue": (dims, pp)}, coords=pc),
            "/model/prior_predictive": xr.Dataset({"revenue": (dims, prior)}, coords=pc),
            "/model/observed_data": obs[["revenue"]],
            "/model/log_likelihood": xr.Dataset({"revenue": (dims, ll)}, coords=pc),
            "/derived/contributions": contributions(),
            "/derived/response_curves": response_curves(),
        }
    )


def contributions() -> xr.Dataset:
    rng = _rng(7)
    comp = (
        np.abs(rng.standard_normal((CHAINS, DRAWS, 2, 3, 20)))
        * np.array([5.0, 2.0])[None, None, :, None, None]
    )
    base = 80 + rng.standard_normal((CHAINS, DRAWS, 3, 20))
    return xr.Dataset(
        {
            "channel_contribution": (("chain", "draw", "channel", "geo", "week"), comp),
            "baseline": (("chain", "draw", "geo", "week"), base),
        },
        coords={
            "chain": np.arange(CHAINS),
            "draw": np.arange(DRAWS),
            "channel": CHANNELS,
            "geo": GEOS,
            "week": WEEKS,
        },
    )


def response_curves() -> xr.Dataset:
    grid = np.linspace(0, 100, 21)
    rng = _rng(8)
    sat = np.array([30.0, 60.0])
    top = np.array([50.0, 20.0])
    noise = 1 + 0.05 * rng.standard_normal((CHAINS, DRAWS, 2, 1))
    resp = top[None, None, :, None] * (
        1 - np.exp(-grid[None, None, None, :] / sat[None, None, :, None])
    )
    return xr.Dataset(
        {
            "response": (("chain", "draw", "channel", "spend_level"), resp * noise),
            "current_spend": (("channel",), np.array([40.0, 25.0])),
        },
        coords={
            "chain": np.arange(CHAINS),
            "draw": np.arange(DRAWS),
            "channel": CHANNELS,
            "spend_level": grid,
        },
    )


def score_tree(with_draws: bool = True) -> xr.DataTree:
    """A score_bundle-like output: summary(stat, ...) + draws(sample, ...) + observed."""
    rng = _rng(9)
    weeks = pd.date_range("2024-01-07", periods=28, freq="W-SUN")
    mean = 100 + np.linspace(0, 30, 28)[None, :] + rng.standard_normal((3, 28))
    stats = ["mean", "hdi_90_lower", "hdi_90_upper", "hdi_50_lower", "hdi_50_upper"]
    summary = np.stack([mean, mean - 15, mean + 15, mean - 6, mean + 6])
    observed = mean[:, :20] + 5 * rng.standard_normal((3, 20))
    groups = {
        "/": xr.Dataset(attrs={"bundle_id": "bundle-9", "representation": "draws"}),
        "/summary": xr.Dataset(
            {"revenue": (("stat", "geo", "week"), summary)},
            coords={"stat": stats, "geo": GEOS, "week": weeks},
        ),
        "/observed": xr.Dataset(
            {"revenue": (("geo", "week"), observed)}, coords={"geo": GEOS, "week": weeks[:20]}
        ),
    }
    if with_draws:
        d = mean[None] + 8 * rng.standard_normal((50, 3, 28))
        groups["/draws"] = xr.Dataset(
            {"revenue": (("sample", "geo", "week"), d)},
            coords={
                "sample": np.arange(50),
                "chain": ("sample", np.zeros(50, int)),
                "draw": ("sample", np.arange(50)),
                "geo": GEOS,
                "week": weeks,
            },
        )
    return xr.DataTree.from_dict(groups)


def counterfactual_tree() -> xr.DataTree:
    rng = _rng(10)
    weeks = pd.date_range("2025-01-05", periods=12, freq="W-SUN")
    base = 100 + rng.standard_normal((CHAINS, DRAWS, 3, 12))
    lift = 5 + rng.standard_normal((CHAINS, DRAWS, 1, 1))
    c = {"chain": np.arange(CHAINS), "draw": np.arange(DRAWS), "geo": GEOS, "week": weeks}
    dims = ("chain", "draw", "geo", "week")
    stats = ["mean", "hdi_90_lower", "hdi_90_upper"]
    m = base.mean((0, 1))
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "b", "mode": "counterfactual"}),
            "/predictions": xr.Dataset({"revenue": (dims, base + lift)}, coords=c),
            "/baseline/predictions": xr.Dataset({"revenue": (dims, base)}, coords=c),
            "/baseline/summary": xr.Dataset(
                {"revenue": (("stat", "geo", "week"), np.stack([m, m - 2, m + 2]))},
                coords={"stat": stats, "geo": GEOS, "week": weeks},
            ),
            "/delta/predictions": xr.Dataset(
                {"revenue": (dims, np.broadcast_to(lift, base.shape).copy())}, coords=c
            ),
        }
    )


def decision_dataset(candidates: bool = True) -> xr.Dataset:
    rng = _rng(11)
    if candidates:
        vals = rng.standard_normal((CHAINS, DRAWS, 3)) + np.array([0.0, 1.0, 0.5])
        return xr.Dataset(
            {"objective": (("chain", "draw", "candidate"), vals)},
            coords={"candidate": ["keep", "shift_tv", "cut_search"]},
        )
    return xr.Dataset({"objective": (("chain", "draw"), 2 + rng.standard_normal((CHAINS, DRAWS)))})


def plans_dataset() -> xr.Dataset:
    rng = _rng(12)
    spend = np.array([[50.0, 30.0], [60.0, 20.0], [40.0, 40.0]])
    outcome = 1000 + rng.standard_normal((CHAINS, DRAWS, 3)) * 20 + np.array([0, 25, -10])
    return xr.Dataset(
        {
            "spend": (("plan", "channel"), spend),
            "outcome": (("chain", "draw", "plan"), outcome),
        },
        coords={"plan": ["current", "plan_b", "plan_c"], "channel": CHANNELS},
    )


def prior_beliefs_dataset() -> xr.Dataset:
    """A ``translate_priors`` preview: prior ROI/share draws per channel with stated ranges."""
    rng = _rng(13)
    n = 400
    roi = np.exp(rng.normal([0.9, 1.2], [0.3, 0.25], size=(n, 2)))
    share = roi * np.array([0.05, 0.03])
    baseline = 0.65 + 0.08 * rng.standard_normal(n)
    ds = xr.Dataset(
        {
            "roi": (("sample", "channel"), roi),
            "contribution_share": (("sample", "channel"), share),
            "baseline_share": (("sample",), baseline),
            "stated_quantity": (("channel",), np.array(["roi", "contribution_share"])),
            "stated_low": (("channel",), [1.5, 0.05]),
            "stated_high": (("channel",), [4.0, 0.15]),
            "stated_credibility": (("channel",), [0.8, 0.9]),
        },
        coords={"sample": np.arange(n), "channel": CHANNELS},
    )
    ds["baseline_share"].attrs = {"range_low": 0.5, "range_high": 0.8, "credibility": 0.8}
    return ds


def feature_importance_tree() -> xr.DataTree:
    """A tabular bundle shaped like ``mapi_sklearn``'s: tree importances and coefficients."""
    feats = ["price", "promo_lagged[1]", "promo_lagged[2]", "calendar[week_of_year]", "store"]
    imp = np.array([0.42, 0.18, 0.07, 0.21, 0.12])
    coef = np.array([[-1.8, 0.6, 0.2, 0.05, 0.3], [-1.2, 0.5, 0.1, 0.04, 0.2]])
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-tabular"}),
            "model/feature_importance": xr.Dataset(
                {"importance": (("feature",), imp)}, coords={"feature": feats}
            ),
            "model/coefficients": xr.Dataset(
                {
                    "coefficient": (("model", "feature"), coef),
                    "intercept": (("model",), np.array([3.1, 3.4])),
                },
                coords={"feature": feats, "model": [0, 1]},
            ),
        }
    )


def field_tree() -> xr.DataTree:
    """``posterior.intensity(chain, draw, row, col)``: a lensed galaxy, a bright arc at the
    top left on a 32x32 image. The data fix the arc well and the edges of the image badly
    (the draws spread more away from the centre); one corner was not observed (NaN)."""
    rng = _rng(11)
    yy, xx = np.meshgrid(np.linspace(0, 1, 32), np.linspace(0, 1, 32), indexing="ij")
    r = np.hypot(xx - 0.55, yy - 0.45)
    angle = np.arctan2(yy - 0.45, xx - 0.55)
    arc = np.exp(-((r - 0.3) ** 2) / 0.003) * np.exp(-((angle + 2.3) ** 2) / 0.5)
    spread = 0.05 + 0.5 * r**2
    mean = 0.1 + 3.0 * arc + 0.8 * np.exp(-(r**2) / 0.004)
    draws = mean + rng.normal(size=(CHAINS, 40, 32, 32)) * spread
    draws = np.clip(draws, 0.0, None)
    draws[..., :5, -6:] = np.nan
    da = xr.DataArray(
        draws,
        dims=["chain", "draw", "row", "col"],
        coords={"row": np.linspace(0, 1, 32), "col": np.linspace(0, 1, 32)},
        name="intensity",
        attrs={"grid": json.dumps({"y": "row", "x": "col"})},
    )
    return xr.DataTree.from_dict(
        {"/": xr.Dataset(attrs={"bundle_id": "bundle-sci"}), "/posterior": da.to_dataset()}
    )


def vector_tree() -> xr.DataTree:
    """``posterior.traction(chain, draw, comp, y, x)``: the traction force of a long cell.
    The forces point inwards and are largest at the two ends; the draws disagree most in
    the centre, where the force is small."""
    rng = _rng(12)
    y, x = np.meshgrid(np.linspace(-1, 1, 14), np.linspace(-1.5, 1.5, 20), indexing="ij")
    inside = np.exp(-(((x / 1.3) ** 2 + (y / 0.7) ** 2) ** 3))
    u = -1.6 * x * np.abs(x) * inside
    v = -0.6 * y * inside
    noise = 0.08 + 0.35 * np.exp(-(x**2 + y**2) / 0.3)
    draws = np.stack([u, v])[None, None] + rng.normal(size=(CHAINS, 30, 2, 14, 20)) * noise
    da = xr.DataArray(
        draws,
        dims=["chain", "draw", "comp", "y", "x"],
        coords={"y": np.linspace(-1, 1, 14), "x": np.linspace(-1.5, 1.5, 20)},
        name="traction",
        attrs={"grid": json.dumps({"y": "y", "x": "x", "component": "comp", "origin": "lower"})},
    )
    return xr.DataTree.from_dict(
        {"/": xr.Dataset(attrs={"bundle_id": "bundle-sci"}), "/posterior": da.to_dataset()}
    )


def profile_tree() -> xr.DataTree:
    """``posterior_predictive.u(chain, draw, t, x)`` and ``observed_data.u(t, x)``: the
    population along a river on four days. The front moves to the right and the model is
    less sure of where it is on later days; some points were not measured."""
    rng = _rng(13)
    t, xg = np.arange(4), np.linspace(0.0, 1.0, 40)
    front = 0.2 + 0.18 * t
    pred = np.empty((CHAINS, 60, 4, 40))
    for i in t:
        shift = rng.normal(0, 0.015 * (1 + i), size=(CHAINS, 60, 1))
        pred[:, :, i] = 1 / (1 + np.exp((xg - front[i] - shift) / 0.04))
    pred += rng.normal(0, 0.03, size=pred.shape)
    obs = 1 / (1 + np.exp((xg[None, :] - front[:, None]) / 0.04)) + rng.normal(0, 0.03, (4, 40))
    obs[:, ::3] = np.nan
    coords = {"t": t, "x": xg}
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-sci"}),
            "/posterior_predictive": xr.Dataset({"u": (("chain", "draw", "t", "x"), pred)}, coords),
            "/observed_data": xr.Dataset({"u": (("t", "x"), obs)}, coords),
        }
    )


def ensemble_tree() -> xr.DataTree:
    """``predictions.y(chain, draw, time)``: 60 simulated outbreaks over 24 weeks. Most
    peak in week 10, some in week 14; the observed weekly cases are inside the band."""
    rng = _rng(14)
    t = pd.date_range("2024-01-07", periods=24, freq="W-SUN")
    weeks = np.arange(24)
    peak = np.where(rng.random(60) < 0.75, 10.0, 14.0) + rng.normal(0, 0.6, 60)
    height = rng.normal(100, 15, 60)
    walk = height[:, None] * np.exp(-((weeks[None, :] - peak[:, None]) ** 2) / (2 * 2.5**2))
    walk = (walk + rng.normal(0, 4, walk.shape))[None]
    obs = 95 * np.exp(-((weeks - 10.5) ** 2) / (2 * 2.6**2)) + rng.normal(0, 4, 24)
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-sci"}),
            "/predictions": xr.Dataset({"y": (("chain", "draw", "time"), walk)}, {"time": t}),
            "/observed_data": xr.Dataset({"y": (("time",), obs)}, {"time": t}),
        }
    )


def calibration_tree(binary: bool = False) -> xr.DataTree:
    """``posterior_predictive.y(chain, draw, obs)`` and ``observed_data.y(obs)`` for 200
    observations. The model's ranges are too narrow: the data spread 1.4 times more than the
    predictions. With ``binary``, ``y`` is 0/1 and the model is too sure at both ends."""
    rng = _rng(17)
    n = 200
    mu = rng.normal(0.0, 2.0, n)
    coords = {"obs": np.arange(n)}
    if binary:
        p_true = 1 / (1 + np.exp(-mu))
        y = (rng.random(n) < p_true).astype(float)
        p_model = 1 / (1 + np.exp(-1.6 * mu))
        draws = (rng.random((CHAINS, DRAWS, n)) < p_model).astype(float)
    else:
        y = mu + 1.4 * rng.standard_normal(n)
        draws = mu + rng.standard_normal((CHAINS, DRAWS, n))
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-calibration"}),
            "/posterior_predictive": xr.Dataset({"y": (("chain", "draw", "obs"), draws)}, coords),
            "/observed_data": xr.Dataset({"y": (("obs",), y)}, coords),
        }
    )


def group_effects_tree() -> xr.DataTree:
    """``posterior.effect(chain, draw, school)``: the effect of a programme in eight
    schools (the classic hierarchical example). School A is clearly the best, the middle
    schools overlap, and the small schools have wide ranges."""
    rng = _rng(15)
    schools = list("ABCDEFGH")
    centre = np.array([14.0, 8.0, 6.0, 7.5, 4.0, 5.5, 10.0, 8.5])
    spread = np.array([3.0, 4.0, 6.0, 4.5, 3.5, 5.0, 3.5, 7.0])
    draws = centre + spread * rng.standard_normal((CHAINS, DRAWS, 8))
    post = xr.Dataset(
        {"effect": (("chain", "draw", "school"), draws)},
        coords={"chain": np.arange(CHAINS), "draw": np.arange(DRAWS), "school": schools},
    )
    return xr.DataTree.from_dict(
        {"/": xr.Dataset(attrs={"bundle_id": "bundle-groups"}), "/posterior": post}
    )


def regime_tree() -> xr.DataTree:
    """``posterior.state_prob(chain, draw, state, time)`` and ``observed_data.y(time)``: a
    two-state market (calm, volatile) over 150 days. The volatile spells are clear; the model
    is unsure at the switches."""
    rng = _rng(16)
    n = 150
    truth = np.zeros(n, dtype=int)
    truth[40:65] = 1
    truth[100:120] = 1
    y = np.where(truth == 1, 3.0, 0.8) * rng.standard_normal(n)
    logit = np.where(truth == 1, 2.5, -2.5)
    for edge in (40, 65, 100, 120):  # softer at the switches
        idx = np.arange(max(0, edge - 3), min(n, edge + 3))
        logit[idx] *= np.abs(idx - edge + 0.5) / 3.5
    p1 = 1 / (1 + np.exp(-(logit[None, None] + 0.6 * rng.standard_normal((CHAINS, 40, n)))))
    probs = np.stack([1 - p1, p1], axis=2)
    days = pd.date_range("2024-01-01", periods=n, freq="D")
    coords = {"state": ["calm", "volatile"], "time": days}
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-regimes"}),
            "/posterior": xr.Dataset(
                {"state_prob": (("chain", "draw", "state", "time"), probs)}, coords
            ),
            "/observed_data": xr.Dataset({"y": (("time",), y)}, {"time": days}),
        }
    )


def area_map_tree() -> xr.DataTree:
    """``posterior.rr(chain, draw, area)``: relative risk of a disease in 30 districts (a
    6 x 5 grid; the last district also owns an island), the observed ratio ``smr`` and the
    expected counts in ``observed_data``, polygons and neighbours in ``constant_data``. The
    risk is high in the north-east; small districts have extreme observed ratios."""
    rng = _rng(15)
    nx, ny = 6, 5
    names = [f"D{i + 1:02d}" for i in range(nx * ny)]
    ring_x = np.full((nx * ny, 11), np.nan)
    ring_y = np.full((nx * ny, 11), np.nan)
    adjacency = np.zeros((nx * ny, nx * ny), dtype=np.int8)
    for a in range(nx * ny):
        r, c = divmod(a, nx)
        ring_x[a, :5] = [c, c + 1, c + 1, c, c]
        ring_y[a, :5] = [r, r, r + 1, r + 1, r]
        for rr, cc in ((r + 1, c), (r, c + 1)):
            if rr < ny and cc < nx:
                adjacency[a, rr * nx + cc] = adjacency[rr * nx + cc, a] = 1
    ring_x[-1, 6:10] = [6.3, 6.8, 6.6, 6.3]  # an island after the NaN gap
    ring_y[-1, 6:10] = [5.2, 5.3, 5.8, 5.2]
    col, row = np.arange(nx * ny) % nx, np.arange(nx * ny) // nx
    log_rr = 0.5 * (col / nx + row / ny) - 0.5
    expected = rng.uniform(2.0, 40.0, nx * ny)
    cases = rng.poisson(expected * np.exp(log_rr))
    sd = 1 / np.sqrt(expected + 4)
    draws = np.exp(log_rr + rng.normal(size=(CHAINS, 60, nx * ny)) * sd)
    area = {"area": names}
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-sci"}),
            "/posterior": xr.Dataset({"rr": (("chain", "draw", "area"), draws)}, area),
            "/observed_data": xr.Dataset(
                {"smr": (("area",), cases / expected), "expected": (("area",), expected)}, area
            ),
            "/constant_data": xr.Dataset(
                {
                    "ring_x": (("area", "vertex"), ring_x),
                    "ring_y": (("area", "vertex"), ring_y),
                    "adjacency": (("area", "area_"), adjacency),
                },
                {"area": names, "area_": names},
            ),
        }
    )


def phylogeny_tree() -> xr.DataTree:
    """``posterior_predictive.body_mass(chain, draw, species)`` on a tree of 16 species
    (``parent``, ``branch_length``, ``is_tip`` and ``tip_label`` in ``constant_data``) and the
    observed trait with three species missing. Close relatives have close values."""
    rng = _rng(16)
    # a balanced tree: node 0 is the root; nodes 1..14 are internal, 15..30 are tips
    parent = np.array([-1] + [(i - 1) // 2 for i in range(1, 31)])
    length = np.concatenate([[0.0], rng.uniform(0.3, 1.2, 30)])
    is_tip = np.arange(31) >= 15
    species = [f"sp_{chr(97 + i)}" for i in range(16)]
    labels = [""] * 15 + species
    trait = np.zeros(31)
    for v in range(1, 31):
        trait[v] = trait[parent[v]] + rng.normal(0, np.sqrt(length[v]))
    truth = trait[15:] + 3.0
    draws = truth + rng.normal(size=(CHAINS, 80, 16)) * rng.uniform(0.2, 0.6, 16)
    observed = truth + rng.normal(0, 0.2, 16)
    observed[[2, 9, 13]] = np.nan
    node = {"node": np.arange(31)}
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-sci"}),
            "/posterior_predictive": xr.Dataset(
                {"body_mass": (("chain", "draw", "species"), draws)}, {"species": species}
            ),
            "/observed_data": xr.Dataset(
                {"body_mass": (("species",), observed)}, {"species": species}
            ),
            "/constant_data": xr.Dataset(
                {
                    "parent": (("node",), parent),
                    "branch_length": (("node",), length),
                    "is_tip": (("node",), is_tip),
                    "tip_label": (("node",), labels),
                },
                node,
            ),
        }
    )


def rose_tree() -> xr.DataTree:
    """``posterior_predictive.wind_dir(chain, draw, report)`` and ``observed_data.wind_dir``:
    400 wind directions in degrees. Most winds come from the south-west, some from the
    north-east; the model has the south-west peak but too few north-east winds."""
    rng = _rng(17)
    n = 400
    obs = np.where(
        rng.random(n) < 0.7, rng.vonmises(np.radians(225), 3.0, n), rng.vonmises(0.8, 4.0, n)
    )
    pred = np.where(
        rng.random((CHAINS, 50, n)) < 0.82,
        rng.vonmises(np.radians(225), 2.5, (CHAINS, 50, n)),
        rng.vonmises(0.8, 3.0, (CHAINS, 50, n)),
    )
    report = {"report": np.arange(n)}
    return xr.DataTree.from_dict(
        {
            "/": xr.Dataset(attrs={"bundle_id": "bundle-sci"}),
            "/posterior_predictive": xr.Dataset(
                {"wind_dir": (("chain", "draw", "report"), np.degrees(pred) % 360)}, report
            ),
            "/observed_data": xr.Dataset(
                {"wind_dir": (("report",), np.degrees(obs) % 360)}, report
            ),
        }
    )


def example_inputs() -> dict[str, tuple[Any, dict[str, Any]]]:
    """``{recipe id: (input data, render options)}`` for every bundled recipe."""
    bundle = bundle_tree()
    scored = score_tree()
    return {
        "core.data_overview": (panel_data(), {}),
        "core.prior_predictive": (bundle, {}),
        "core.convergence": (bundle, {}),
        "core.posterior_forest": (bundle, {}),
        "core.ppc": (bundle, {}),
        "core.ppc_fan": (scored, {}),
        "core.fit_vs_actual": (scored, {}),
        "core.residuals_over_time": (scored, {}),
        "core.decomposition": (bundle, {}),
        "core.model_comparison": ({"unbiased": bundle, "biased": bundle_tree(1.0)}, {}),
        "core.counterfactual": (counterfactual_tree(), {}),
        "core.decision_distribution": (decision_dataset(), {}),
        "mmm.response_curves": (bundle, {}),
        "mmm.marginal_cost": (bundle, {}),
        "mmm.budget_plans": ({"plans": plans_dataset()}, {}),
        "mmm.decomposition": (bundle, {}),
        "mmm.prior_beliefs": ({"prior_beliefs": prior_beliefs_dataset()}, {}),
        "ml.feature_importance": (feature_importance_tree(), {}),
        "sci.field_map": (field_tree(), {}),
        "sci.vector_field": (vector_tree(), {}),
        "sci.profile_bands": (profile_tree(), {}),
        "sci.ensemble": (ensemble_tree(), {}),
        "core.pair": (bundle, {}),
        "core.ridge": (group_effects_tree(), {}),
        "core.rank_matrix": (group_effects_tree(), {}),
        "core.calibration": (calibration_tree(), {}),
        "core.state_sequence": (regime_tree(), {}),
        "sci.area_map": (area_map_tree(), {"observed": "smr", "show_edges": True}),
        "sci.phylogeny": (phylogeny_tree(), {}),
        "sci.rose": (rose_tree(), {"units": "degrees", "bins": 16}),
    }
