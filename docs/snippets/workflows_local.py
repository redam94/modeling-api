"""Workflows guide: run the canonical template in-process with the local runner."""

# --8<-- [start:run]
from mapi_spec.workflow_templates import TS_READINESS_ARIMAX, instantiate_template
from mapi_workflows.runtime.local import LocalRunner
from mapi_workflows.testing import test_registry, weekly_series

spec = instantiate_template(TS_READINESS_ARIMAX, name="weekly-readiness", project="growth")
data = weekly_series(156, random_walk=True, beta=3.0)  # 3 years of a wandering weekly KPI

runner = LocalRunner(registry=test_registry())
result = runner.run(
    spec,
    inputs={"data": data},
    params={"drivers": ["price"], "n_folds": 4, "gap": 1, "horizons": [1, 4, 8]},
)
print(result.status.value)  # succeeded
# --8<-- [end:run]

# --8<-- [start:inspect]
nodes = {n.node_id: n for n in result.nodes}
print(nodes["adf"].result["verdict_plain"])  # the plain-language verdict of the ADF test
print(nodes["wanders"].result)  # {'value': True, 'arm': 'then'}: the series was differenced
print(nodes["sdiff"].status.value)  # skipped: no strong yearly season
print(nodes["backtest"].params["settings"]["order"])  # [p, 1, q] chosen by the order search
print(nodes["select"].result["beats_baselines"])  # True: ARIMAX beat the naive forecasts
for d in result.decisions:
    print(d.node_id, d.arm, d.inputs)  # every branch with the values its condition read
# --8<-- [end:inspect]

# --8<-- [start:cache]
again = runner.run(
    spec,
    inputs={"data": data},
    params={"drivers": ["price"], "n_folds": 4, "gap": 1, "horizons": [1, 4, 8]},
)
print({n.node_id: n.status.value for n in again.nodes}["backtest"])  # cached
# --8<-- [end:cache]

assert result.status.value == "succeeded"
assert nodes["wanders"].result == {"value": True, "arm": "then"}
assert nodes["sdiff"].status.value == "skipped"
assert {n.node_id: n.status.value for n in again.nodes}["backtest"] == "cached"
