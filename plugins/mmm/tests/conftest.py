"""Shared fixtures: the real step registry plus the MMM plugins, small E33 data and one
tiny-draw fit of ``mmm.hierarchical_geo`` (``pymc`` engine, session scoped)."""

from __future__ import annotations

from collections.abc import Callable

import mapi_steps
import pytest
import xarray as xr
from mapi_plots.recipes import all_recipes
from mapi_plugin_mmm.examples import (
    E33_SPEC_YAML,
    E33Truth,
    e33_dataset,
    e33_raw_schema,
    e33_side_tables,
)
from mapi_plugin_mmm.plugins import PLUGINS
from mapi_sdk.bundle import Bundle
from mapi_sdk.harness import fit_spec
from mapi_sdk.registry import Registry
from mapi_spec import DataSchema, ModelSpec, ScoreRequest, load_model_spec, load_score_request

MARKETS = ("NL", "DE", "UK")
CHANNELS = ("tv", "search")
WEEKS = 30
REQ = (
    "apiVersion: mapi/v1\nkind: ScoreRequest\nmodel: endpoint://growth/eu-geo-mmm\n"
    "side_tables: {fx: dataset://growth/fx_daily@latest}\n"
)


def small_spec(**replace: str) -> str:
    """The example spec with a 4-week holdout and 50 UI draws (plus text replacements)."""
    text = E33_SPEC_YAML.replace("last: 8", "last: 4").replace("ui_draws: 100", "ui_draws: 50")
    for old, new in replace.items():
        assert old in text, old
        text = text.replace(old, new)
    return text


@pytest.fixture(scope="session")
def spec_text() -> Callable[..., str]:
    """``spec_text(**{old: new})``: the small example spec with text replacements."""
    return small_spec


@pytest.fixture(scope="session")
def request_for() -> Callable[[str], ScoreRequest]:
    """``request_for("mode: forecast\n...")``: a score request supplying the fx table."""

    def make(body: str) -> ScoreRequest:
        return load_score_request(REQ + body)

    return make


@pytest.fixture(scope="session")
def registry() -> Registry:
    reg = mapi_steps.registry()
    for plugin in PLUGINS:
        reg.register_model(plugin)
    reg.register(all_recipes())
    return reg


@pytest.fixture(scope="session")
def small() -> tuple[xr.Dataset, E33Truth]:
    return e33_dataset(markets=MARKETS, channels=CHANNELS, weeks=WEEKS, seed=1)


@pytest.fixture(scope="session")
def tables() -> dict[str, xr.Dataset]:
    return e33_side_tables()


@pytest.fixture(scope="session")
def raw_schema(small: tuple[xr.Dataset, E33Truth]) -> DataSchema:
    return e33_raw_schema(small[0])


@pytest.fixture(scope="session")
def spec() -> ModelSpec:
    return load_model_spec(small_spec())


@pytest.fixture(scope="session")
def bundle(
    spec: ModelSpec,
    small: tuple[xr.Dataset, E33Truth],
    tables: dict[str, xr.Dataset],
    registry: Registry,
    raw_schema: DataSchema,
) -> Bundle:
    return fit_spec(spec, small[0], tables, registry, raw_schema=raw_schema)
