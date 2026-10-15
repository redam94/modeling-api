"""Plugin authoring: the contract kit as pytest fixtures (installed with mapi-sdk).

Run with ``pytest test_my_plugin.py``. Swap the toy reference step and model for your own.
"""

# --8<-- [start:pytest]
import pytest
from mapi_sdk.testing import ModelFixture, toy  # toy: small reference steps and models


@pytest.mark.mapi_contract
def test_step_follows_the_contract(mapi_check_step, mapi_panel):
    data, _fx = mapi_panel  # a daily market x channel panel and its FX side table
    report = mapi_check_step(
        toy.ScaleStep, data, {"vars": ["spend_local"], "over": ["date"], "unseen_levels": "pooled"}
    )
    assert report.ok, report.summary()


SPEC = """
apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: toy, project: demo}
data:
  dataset: dataset://demo/panel@latest
  bind: {dims: {geo: market, time: date}, vars: {y: revenue_local, spend: spend_local}}
  split: {kind: time_holdout, dim: time, last: 14}
model: {use: toy.linear@^1, fit: {seed: 1}}
evaluate: {metrics: [rmse]}
"""


@pytest.mark.mapi_contract
def test_model_follows_the_contract(mapi_check_model, mapi_panel, mapi_toy_registry):
    data, _fx = mapi_panel
    fixture = ModelFixture(spec=SPEC, data=data, registry=mapi_toy_registry)
    report = mapi_check_model(toy.LinearModel, fixture)  # raises ContractViolation on failure
    assert report.ok
# --8<-- [end:pytest]
