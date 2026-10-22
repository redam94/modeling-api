"""The standard plot recipe catalog (plan §10.2). Extra ``recipes`` only.

Every recipe is a function ``(DataTree-like, settings) -> PlotSpec`` with its data in named
Arrow datasets (never URLs), a persona and a plain-language caption. They are generated in
the runner from a fitted bundle or a score output, so every plugin gets them for free:

============================  ==========================================================
``core.data_overview``        series per variable, missingness heatmap, group sizes
``core.prior_predictive``     prior predictive densities (+ fan over time)
``core.convergence``          R-hat / ESS per parameter, worst-parameter table, trace, rank
``core.posterior_forest``     point, 50% and 94% HDI per parameter
``core.ppc``                  predictive density overlay, PIT histogram, coverage
``core.ppc_fan``              fit + forecast fan with the split line
``core.fit_vs_actual``        predicted against actual
``core.residuals_over_time``  actual minus predicted over time
``core.decomposition``        stacked contributions and totals
``core.model_comparison``     elpd forest (precomputed LOO table, or WAIC)
``core.counterfactual``       scenario vs baseline band + total-change distribution
``core.decision_distribution`` scalar decision objective per option
``core.pair``                 corner plot of a few parameters, divergences highlighted
``core.ridge``                one density per level of a dim, ordered by mean
``core.rank_matrix``          P(rank = k) per item and pairwise P(i above j)
``core.calibration``          PIT-ECDF difference with an envelope, or a reliability diagram
``core.state_sequence``       observed series above stacked state probabilities
``core.roc_pr``               ROC and precision-recall curves of a binary classifier
``core.confusion``            2 x 2 confusion matrix at a probability threshold
``core.loss_curve``           loss of an iterative fit (ADVI) per step, moving average
``core.loo_pit``              LOO-PIT calibration and the PSIS Pareto k per observation
``core.energy``               marginal energy vs energy transitions, E-BFMI per chain
``core.prior_posterior``      prior and posterior densities per parameter, ranked by shift
============================  ==========================================================

The MMM domain pack lives in :mod:`mapi_plots.packs.mmm` and the tabular-ML pack
(``ml.feature_importance``) in :mod:`mapi_plots.packs.ml`, and the scientific-model pack
(``sci.field_map``, ``sci.vector_field``, ``sci.profile_bands``, ``sci.ensemble``; Plotly) in
:mod:`mapi_plots.packs.sci`, with the map, tree and polar recipes in
:mod:`mapi_plots.packs.sci_maps`, the image and spectrum recipes in
:mod:`mapi_plots.packs.sci_images` and ``sci.point_pattern`` in
:mod:`mapi_plots.packs.sci_points`, the network pack (``net.graph``; Plotly) in
:mod:`mapi_plots.packs.net`, and the causal pack (``causal.impact``, ``causal.pre_fit``,
``causal.donor_weights``, ``causal.event_study``) in :mod:`mapi_plots.packs.causal`. All are
exposed through the ``mapi.plots`` entry-point group, so
``mapi_sdk.registry.default_registry()`` finds them.

``all_recipes(installed=True)`` (v0.18) adds the recipes other installed distributions export
through ``mapi.plots`` (loaded on that call only), with the registry's namespace rules.
"""

from __future__ import annotations

import importlib.metadata
import warnings
from collections.abc import Iterable, Iterator
from typing import Any

from mapi_spec.namespaces import check_namespace
from mapi_spec.refs import Version

from mapi_plots.recipes._base import (
    Figure,
    Recipe,
    RecipeDataError,
    RecipeInput,
    RecipeSettings,
    as_tree,
    named,
    recipe,
)
from mapi_plots.recipes.batch import RenderReport, render_plots
from mapi_plots.recipes.bayes import energy, prior_posterior
from mapi_plots.recipes.classification import confusion, roc_pr
from mapi_plots.recipes.comparison import model_comparison
from mapi_plots.recipes.data import data_overview
from mapi_plots.recipes.diagnostics import convergence, posterior_forest, ppc, prior_predictive
from mapi_plots.recipes.extras import calibration, pair, rank_matrix, ridge, state_sequence
from mapi_plots.recipes.loo import loo_pit
from mapi_plots.recipes.predictions import (
    counterfactual,
    decision_distribution,
    decomposition,
    fit_vs_actual,
    ppc_fan,
    residuals_over_time,
)
from mapi_plots.recipes.templating import builtin_templates, infer_schema, render_template
from mapi_plots.recipes.training import loss_curve

__all__ = [
    "Figure",
    "Recipe",
    "RecipeDataError",
    "RecipeInput",
    "RecipeSettings",
    "RenderReport",
    "all_recipes",
    "as_tree",
    "builtin_templates",
    "get_recipe",
    "infer_schema",
    "named",
    "recipe",
    "render_plots",
    "render_template",
    "standard_recipes",
]

ENTRY_POINT_GROUP = "mapi.plots"
_OWN_DISTRIBUTION = "mapi-plots"

_STANDARD: tuple[Recipe[Any], ...] = (
    data_overview,
    prior_predictive,
    convergence,
    posterior_forest,
    ppc,
    ppc_fan,
    fit_vs_actual,
    residuals_over_time,
    decomposition,
    model_comparison,
    counterfactual,
    decision_distribution,
    pair,
    ridge,
    rank_matrix,
    calibration,
    state_sequence,
    roc_pr,
    confusion,
    loss_curve,
    loo_pit,
    energy,
    prior_posterior,
)


def standard_recipes() -> list[Recipe[Any]]:
    """The core catalog (entry point ``mapi.plots: core``)."""
    return list(_STANDARD)


def _bundled() -> list[Recipe[Any]]:
    from mapi_plots.packs.causal import causal_recipes
    from mapi_plots.packs.ml import ml_recipes
    from mapi_plots.packs.mmm import mmm_recipes
    from mapi_plots.packs.net import net_recipes
    from mapi_plots.packs.sci import sci_recipes

    return [
        *_STANDARD,
        *mmm_recipes(),
        *ml_recipes(),
        *sci_recipes(),
        *net_recipes(),
        *causal_recipes(),
    ]


def _norm_dist(name: str) -> str:
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _flatten(obj: Any) -> Iterator[Any]:
    if isinstance(obj, Recipe):
        yield obj
    elif isinstance(obj, Iterable) and not isinstance(obj, str | bytes):
        for item in obj:
            yield from _flatten(item)
    else:
        raise TypeError(f"{obj!r} is not a plot recipe")


def _installed() -> Iterator[Recipe[Any]]:
    """Recipes exported through ``mapi.plots`` by other distributions.

    Like the SDK registry: a callable entry point is called, nested lists are flattened, and
    a recipe in a reserved or built-in namespace from a third-party distribution is refused.
    One broken entry point is reported as a :class:`RuntimeWarning` and skipped (the
    registry's ``diagnostics`` are the record; this helper serves notebooks and tests)."""
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        dist = getattr(getattr(ep, "dist", None), "name", None)
        if dist is not None and _norm_dist(dist) == _OWN_DISTRIBUTION:
            continue  # the bundled packs, already listed
        try:
            obj = ep.load()
            if callable(obj) and not isinstance(obj, Recipe | type):
                obj = obj()
            found = list(_flatten(obj))
        except Exception as exc:  # plugin code: report it, keep the rest
            warnings.warn(
                f"{ENTRY_POINT_GROUP}:{ep.name}: {type(exc).__name__}: {exc}",
                RuntimeWarning,
                stacklevel=3,
            )
            continue
        for r in found:
            reason = check_namespace(r.id, dist)
            if reason is not None:
                msg = f"{ENTRY_POINT_GROUP}:{ep.name}: {reason}"
                warnings.warn(msg, RuntimeWarning, stacklevel=3)
                continue
            yield r


def all_recipes(*, installed: bool = False) -> list[Recipe[Any]]:
    """Core catalog plus the bundled domain packs.

    ``installed=True`` also loads the recipes other installed distributions export through
    the ``mapi.plots`` entry points (imported on this call, not at module import). A bundled
    recipe wins over an installed one with the same ``id@version``."""
    out = _bundled()
    if installed:
        seen = {r.ref for r in out}
        for r in _installed():
            if r.ref not in seen:
                seen.add(r.ref)
                out.append(r)
    return out


def get_recipe(id: str, *, installed: bool = False) -> Recipe[Any]:
    """Look a recipe up by id (e.g. ``"core.ppc_fan"``): the bundled ones, plus the installed
    ``mapi.plots`` packs with ``installed=True``. The highest version wins."""
    found = [r for r in all_recipes(installed=installed) if r.id == id]
    if not found:
        raise KeyError(f"no recipe {id!r}")
    return max(found, key=lambda r: Version.parse(r.version))
