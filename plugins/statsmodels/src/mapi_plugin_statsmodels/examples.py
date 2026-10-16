"""A small worked example (spec, data, raw schema) for every statsmodels plugin.

Used by the tests and the docs: :func:`examples` returns one
:class:`Example` per plugin id, sized to fit in a second or two. Univariate models run on
a two-geo panel where that shows the per-series loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import xarray as xr
from mapi_spec.schema import DataSchema

from mapi_plugin_statsmodels.datasets import (
    ECM_SCHEMA,
    GROWTH_SCHEMA,
    MACRO_SCHEMA,
    REGIME_SCHEMA,
    ecm_dataset,
    macro_dataset,
    macro_growth,
    regime_dataset,
    sales_dataset,
    sales_schema,
)

__all__ = ["Example", "example_spec", "examples"]


@dataclass(frozen=True, slots=True)
class Example:
    """A spec, its raw data and raw schema for one plugin."""

    model: str
    spec: str
    data: xr.Dataset
    schema: DataSchema


def example_spec(
    model: str,
    settings: str,
    bind: str,
    *,
    last: int = 6,
    mode: str = "evaluate_then_refit",
    metrics: str = "[rmse, mae, coverage_90]",
) -> str:
    """A ModelSpec (YAML) for ``model`` with inline ``settings`` and ``bind`` mappings and a
    ``last``-period time holdout."""
    return f"""
apiVersion: mapi/v1
kind: ModelSpec
metadata: {{name: sm-test, project: tests}}
data:
  dataset: dataset://tests/data@latest
  bind: {bind}
  split: {{kind: time_holdout, dim: t, last: {last}}}
model:
  use: {model}@^1
  settings: {settings}
  fit: {{mode: {mode}, seed: 7}}
evaluate: {{metrics: {metrics}}}
outputs: {{plots: [core.ppc_fan, core.fit_vs_actual]}}
"""


SALES_BIND = "{dims: {t: date}, vars: {y: sales}}"
MACRO_BIND = "{dims: {t: quarter, variable: series}, vars: {y: value}}"


def _build() -> dict[str, Example]:
    panel = sales_dataset(periods=60, geos=["north", "south"], seed=1)
    pschema = sales_schema(geo=True)
    single = sales_dataset(periods=70, seed=2)
    sschema = sales_schema()
    macro = macro_dataset(quarters=100)
    growth = macro_growth(quarters=90)
    regimes = regime_dataset(periods=110, seed=3)
    ecm = ecm_dataset(periods=90, seed=4)
    return {
        "ts.sarimax": Example(
            "ts.sarimax",
            example_spec(
                "ts.sarimax",
                "{order: [1, 0, 0], exog: [price, promo], series_dims: [geo]}",
                SALES_BIND,
            ),
            panel,
            pschema,
        ),
        "ts.arima": Example(
            "ts.arima",
            example_spec("ts.arima", "{order: [1, 1, 1], series_dims: [geo]}", SALES_BIND),
            panel,
            pschema,
        ),
        "ts.unobserved_components": Example(
            "ts.unobserved_components",
            example_spec(
                "ts.unobserved_components",
                "{level: local level, exog: [price], series_dims: [geo]}",
                SALES_BIND,
            ),
            panel,
            pschema,
        ),
        "ts.ets": Example(
            "ts.ets",
            example_spec("ts.ets", "{trend: add, damped_trend: true}", SALES_BIND),
            single,
            sschema,
        ),
        "ts.theta": Example(
            "ts.theta", example_spec("ts.theta", "{series_dims: [geo]}", SALES_BIND), panel, pschema
        ),
        "ts.ardl": Example(
            "ts.ardl",
            example_spec("ts.ardl", "{lags: 1, order: 1, exog: [price, promo]}", SALES_BIND),
            single,
            sschema,
        ),
        "ts.ecm": Example(
            "ts.ecm",
            example_spec(
                "ts.ecm",
                "{lags: 1, order: 1, exog: [income]}",
                "{dims: {t: month}, vars: {y: sales}}",
            ),
            ecm,
            ECM_SCHEMA,
        ),
        "ts.var": Example(
            "ts.var", example_spec("ts.var", "{lags: 2}", MACRO_BIND), growth, GROWTH_SCHEMA
        ),
        "ts.vecm": Example(
            "ts.vecm", example_spec("ts.vecm", "{k_ar_diff: 2}", MACRO_BIND), macro, MACRO_SCHEMA
        ),
        "ts.varmax": Example(
            "ts.varmax",
            example_spec("ts.varmax", "{order: [1, 0]}", MACRO_BIND),
            growth,
            GROWTH_SCHEMA,
        ),
        "ts.dynamic_factor": Example(
            "ts.dynamic_factor",
            example_spec("ts.dynamic_factor", "{k_factors: 1}", MACRO_BIND),
            growth,
            GROWTH_SCHEMA,
        ),
        "ts.markov_switching": Example(
            "ts.markov_switching",
            example_spec(
                "ts.markov_switching",
                "{k_regimes: 2, time_kind: integer}",
                "{dims: {t: week}, vars: {y: demand}}",
            ),
            regimes.drop_vars("regime"),
            REGIME_SCHEMA,
        ),
    }


@cache
def examples() -> dict[str, Example]:
    """One small example per plugin id (panels where the model is univariate)."""
    return _build()
