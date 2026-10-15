"""Concepts: labelled data, DataSchema, DimPolicy and roles."""

# --8<-- [start:infer]
import numpy as np
import pandas as pd
import xarray as xr
from mapi_sdk import infer_schema, validate_dataset
from mapi_spec import DimPolicy

days = pd.date_range("2026-03-01", periods=28, freq="D")
markets = ["DE", "FR", "UK"]
channels = ["tv", "search"]
rng = np.random.default_rng(0)
media = xr.Dataset(
    {
        "revenue": (("market", "date"), rng.gamma(50.0, 20.0, (3, 28))),
        "spend": (("market", "channel", "date"), rng.gamma(5.0, 100.0, (3, 2, 28))),
    },
    coords={"market": markets, "channel": channels, "date": days},
)

schema = infer_schema(
    media,
    targets=["revenue"],
    policies={"market": "new_levels"},
    units={"revenue": "EUR", "spend": "EUR"},
)
print(schema.dims["date"].policy, schema.dims["date"].freq)  # extend D
print(schema.dims["channel"].policy)  # fixed
print(schema.vars["revenue"].role)  # target
# --8<-- [end:infer]
assert schema.dims["date"].policy is DimPolicy.EXTEND
assert schema.dims["market"].policy is DimPolicy.NEW_LEVELS

# --8<-- [start:validate]
broken = media.copy(deep=True)
broken["revenue"].loc[{"market": ["FR", "UK"], "date": days[10:12]}] = np.nan

for issue in validate_dataset(schema, broken):
    print(issue.severity, issue.code, "-", issue.message)
# error SCHEMA_INVALID - 2 markets are missing 2 days of revenue in March 2026
# --8<-- [end:validate]
issues = validate_dataset(schema, broken)
assert issues and issues[0].var == "revenue"

# --8<-- [start:binding]
from mapi_sdk import BindingMap

binding = BindingMap({"dims": {"geo": "market", "time": "date"}, "vars": {"y": "revenue"}})
canonical = binding.apply(media)  # what steps and models see
print(sorted(canonical.dims))  # ['channel', 'geo', 'time']
print(sorted(binding.reverse(canonical).dims))  # ['channel', 'date', 'market'] - your names
# --8<-- [end:binding]
