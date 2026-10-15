"""Plugin authoring: an sklearn plugin is one estimator() method."""

# --8<-- [start:model]
from typing import ClassVar

from mapi_sklearn import PointSettings, SklearnModel, capabilities_for
from mapi_spec import Capabilities
from sklearn.linear_model import Ridge


class LinearDemand(SklearnModel[PointSettings]):
    id = "t.acme.linear_demand"
    version = "1.0.0"
    capabilities: ClassVar[Capabilities] = capabilities_for("none").model_copy(
        update={"contribution_scale": "additive"}  # a linear model decomposes exactly
    )

    def estimator(self, s: PointSettings) -> Ridge:
        return Ridge(alpha=0.5)  # must be exactly an allow-listed class
# --8<-- [end:model]

# --8<-- [start:check]
from mapi_sdk.testing import ModelFixture, check_model
from mapi_sklearn.testing import demand_dataset, demand_registry, demand_schema, demand_spec

data = demand_dataset()  # a store x sku x week demand panel
report = check_model(
    LinearDemand,
    ModelFixture(
        spec=demand_spec("t.acme.linear_demand@^1", {"estimator": None}),
        data=data,
        raw_schema=demand_schema(data),
        registry=demand_registry(),
    ),
)
print(report.summary())
# ... [PASS] additive_contributions: contributions sum to 'y_hat' ...
# --8<-- [end:check]
assert report.ok
assert not report.get("additive_contributions").skipped
