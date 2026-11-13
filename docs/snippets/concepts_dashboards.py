"""Concepts: the client role, the client allow-list, zones, accuracy, the two layouts, presets,
scenario rows, family accuracy and the tool surface (mapi_spec only).

Included by ``docs/concepts/client-dashboards.md``; run by ``conformance/tests/test_docs_snippets.py``.
"""

from __future__ import annotations

# --8<-- [start:client]
from mapi_spec.api import CLIENT_OPERATIONS
from mapi_spec.tenancy import Action, ProjectRole, ResourceKind, can

print(can(ProjectRole.CLIENT, Action.READ, ResourceKind.DASHBOARD))  # True
print(can(ProjectRole.CLIENT, Action.RUN, ResourceKind.DASHBOARD))  # True: what-ifs, uploads
print(can(ProjectRole.CLIENT, Action.READ, ResourceKind.DATASET))  # False: nothing else
print(can(ProjectRole.EDITOR, Action.CREATE, ResourceKind.DASHBOARD_SHARE))  # True
print("list_runs" in CLIENT_OPERATIONS, "get_dashboard" in CLIENT_OPERATIONS)  # False True
# --8<-- [end:client]

# --8<-- [start:zones]
from mapi_spec.dashboards import KpiDef, breakeven_of, metrics_of, zone_of

revenue = KpiDef(variable="revenue", label="Revenue", unit="currency", currency="EUR")
breakeven = breakeven_of(revenue)  # 1.0: one euro back for each euro of spend
assert breakeven is not None
print(zone_of(2.4, 1.9, breakeven=breakeven))  # headroom: the next euro still pays well
print(zone_of(1.6, 1.1, breakeven=breakeven))  # optimal
print(zone_of(1.3, 0.7, breakeven=breakeven))  # over: the next euro returns less than it costs
print(zone_of(0.8, 0.5, breakeven=breakeven))  # no_payback

accuracy = metrics_of(
    predicted=[100.0, 110.0, 120.0],
    actual=[104.0, 108.0, 131.0],
    low=[90.0, 95.0, 105.0],
    high=[110.0, 125.0, 130.0],
)
print(accuracy.n, round(accuracy.mape or 0, 2), accuracy.band_hit_rate)  # 3 4.7 66.66...
# --8<-- [end:zones]

# --8<-- [start:layouts]
from mapi_spec.dashboard_presets import PRESET_ORDER, PRESETS
from mapi_spec.dashboard_v2 import DashboardSpecV2, parse_dashboard_spec
from mapi_spec.dashboards import DashboardSpec

# a stored spec without layout_version is layout 1; layout_version 2 is layout 2
old = parse_dashboard_spec(DashboardSpec.model_json_schema()["examples"][0])
new = parse_dashboard_spec(DashboardSpecV2.model_json_schema()["examples"][0])
print(type(old).__name__, type(new).__name__, new.preset)  # DashboardSpec DashboardSpecV2 forecast
print(PRESET_ORDER)  # the order in which the presets are tried, most specific first
print(PRESETS["classifier"].title, sorted(PRESETS["classifier"].rules.inputs))
# --8<-- [end:layouts]

# --8<-- [start:rows]
from mapi_spec.dashboard_inputs import check_rows
from mapi_spec.dashboard_v2 import ScenarioInputDef, ScenarioRowsDef, VarBind

try_a_case = ScenarioRowsDef(
    enabled=True,
    outputs=["p_churn"],
    inputs=[
        ScenarioInputDef(
            name="tenure", label="Months as a customer", bind=VarBind(var="tenure"),
            type="integer", min=0, max=120, step=1, default=12,
        ),
        ScenarioInputDef(
            name="plan", label="Plan", bind=VarBind(var="plan"),
            type="choice", choices=["basic", "plus"], default="basic",
        ),
    ],
)  # fmt: skip
print(check_rows([{"tenure": 24, "plan": "plus"}], try_a_case))  # []: in range
for issue in check_rows([{"tenure": 300}, {"plan": "gold"}], try_a_case):
    print(issue.pointer, issue.message)  # refused with a pointer, never clipped
# /rows/0/tenure Months as a customer must be between 0 and 120
# /rows/1/plan Plan must be one of the choices
# --8<-- [end:rows]

# --8<-- [start:accuracy]
from mapi_spec.dashboard_metrics import classification_metrics_of, error_metrics_of

errors = error_metrics_of(
    [(100.0, 104.0), (110.0, 108.0), (120.0, 131.0)],  # (predicted, actual)
    bands=[(90.0, 110.0), (95.0, 125.0), (105.0, 130.0)],  # the likely range of each
)
print(round(errors.mae or 0, 2), round(errors.rmse or 0, 2))  # 5.67 6.86
print(errors.band_hit_rate)  # 66.66...: two of the three actual values are inside the range

right = classification_metrics_of(
    [0.9, 0.2, 0.7, 0.4],  # the chance of "churned" for each case
    ["churned", "stayed", "stayed", "churned"],  # what happened
    positive="churned",
    threshold=0.5,
)
print(right.accuracy, right.auc, round(right.brier or 0, 3))  # 0.5 0.75 0.225
# --8<-- [end:accuracy]

# --8<-- [start:tools]
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES, explain, tools_json_schema

print(DASHBOARD_TOOL_NAMES)
print(explain("return_on_media").text)
print(sorted(tools_json_schema()[0]))  # the shape of one declaration
# --8<-- [end:tools]
