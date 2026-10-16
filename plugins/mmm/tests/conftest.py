"""Shared fixtures of the mmm-framework plugin tests.

Fast tests never run MCMC: :func:`prior_fitted` builds a ``Fitted`` whose "posterior" is a
draw from mmm-framework's own prior of the model (``BayesianMMM.get_prior``), which
exercises the bundle layout, the replay, scoring, derived quantities and optimisation
exactly as a real fit would. Real fits (``slow``) use tiny settings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import mapi_steps
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from mapi_plots.recipes import all_recipes
from mapi_plugin_mmm.base import FrameworkMMM
from mapi_plugin_mmm.bayesian import MMMBayesian
from mapi_plugin_mmm.framework import PanelData, detect_controls, infer_frequency, make_model, quiet
from mapi_plugin_mmm.plugins import PLUGINS
from mapi_sdk.models import Fitted
from mapi_sdk.registry import Registry
from mapi_spec.spec import FitConfig

GEOS = ("north", "south", "west")
CHANNELS = ("tv", "search")
WEEKS = 52


def geo_stage_data(
    *, weeks: int = WEEKS, geos: tuple[str, ...] = GEOS, seed: int = 3, fourier: bool = True
) -> xr.Dataset:
    """Model-stage weekly geo data: ``y(geo, time)``, ``spend(geo, channel, time)``, a
    ``price(geo, time)`` control and (``fourier``) a national ``season(time, mode)``
    control with an extra dim."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2024-01-07", periods=weeks, freq="W-SUN").as_unit("ns")
    n_g, n_c = len(geos), len(CHANNELS)
    spend = rng.gamma(3.0, 200.0, (n_g, n_c, weeks))
    price = 10 + rng.normal(0, 0.5, (n_g, weeks))
    y = (
        2_000
        + 0.4 * spend[:, 0]
        + 0.6 * spend[:, 1]
        - 30 * (price - 10)
        + rng.normal(0, 20, (n_g, weeks))
    )
    data: dict[str, Any] = {
        "y": (("geo", "time"), y, {"units": "EUR"}),
        "spend": (("geo", "channel", "time"), spend, {"units": "EUR"}),
        "price": (("geo", "time"), price),
    }
    if fourier:
        pos = np.arange(weeks)
        season = np.stack([np.sin(2 * np.pi * pos / 52), np.cos(2 * np.pi * pos / 52)], axis=1)
        data["season"] = (("time", "mode"), season)
    coords = {"geo": list(geos), "channel": list(CHANNELS), "time": t}
    if fourier:
        coords["mode"] = ["sin", "cos"]
    return xr.Dataset(data, coords=coords)


@dataclass(slots=True)
class PriorFit:
    """A ``Fitted`` built from prior draws, with its plugin and data."""

    plugin: FrameworkMMM[Any]
    fitted: Fitted
    data: PanelData
    stage: xr.Dataset
    model: Any
    prior: xr.Dataset


def build_prior_fit(
    stage: xr.Dataset,
    settings: dict[str, Any],
    *,
    plugin: FrameworkMMM[Any] | None = None,
    draws: int = 40,
    seed: int = 5,
) -> PriorFit:
    plugin = plugin or MMMBayesian()
    s = plugin.parse_settings(settings)
    controls = detect_controls(stage, s.controls)
    data = PanelData.from_dataset(stage, s.panel_dims, controls, with_target=True)
    freq = infer_frequency(data.times, s.frequency)
    model = make_model(data, s, frequency=freq)
    with quiet():
        tree = model.get_prior(samples=draws, random_seed=seed)
    prior = tree["prior"].to_dataset(inherit=False)
    model._trace = xr.DataTree.from_dict({"posterior": prior})
    cfg = FitConfig(mode="train_only", seed=seed)
    groups = plugin._groups(model, data, cfg, bayesian=False)
    learned = plugin._learned(data, freq, model, {"engine": "prior"})
    fitted = Fitted(
        manifest=type(plugin).manifest(),
        settings=s.model_dump(mode="json"),
        fit_config=cfg,
        groups=groups,
        learned=learned,
    )
    return PriorFit(plugin, fitted, data, stage, model, prior)


@pytest.fixture(scope="session")
def registry() -> Registry:
    reg = mapi_steps.registry()
    for plugin in PLUGINS:
        reg.register_model(plugin)
    reg.register(all_recipes())
    return reg


@pytest.fixture(scope="session")
def geo_stage() -> xr.Dataset:
    return geo_stage_data()


@pytest.fixture(scope="session")
def geo_prior_fit(geo_stage: xr.Dataset) -> PriorFit:
    return build_prior_fit(
        geo_stage,
        {
            "hierarchy": {"pool_over": ["geo"]},
            "adstock": {"kind": "geometric", "l_max": 4},
            "saturation": {"kind": "hill"},
            "trend": {"kind": "linear"},
        },
    )


@pytest.fixture(scope="session")
def national_prior_fit() -> PriorFit:
    stage = geo_stage_data(geos=("all",)).isel(geo=0, drop=True)
    return build_prior_fit(
        stage,
        {"adstock": {"kind": "delayed", "l_max": 5}, "trend": {"kind": "piecewise"}},
    )


@pytest.fixture(scope="session")
def prior_fit_factory() -> Callable[..., PriorFit]:
    return build_prior_fit


@pytest.fixture(scope="session")
def stage_factory() -> Callable[..., xr.Dataset]:
    return geo_stage_data
