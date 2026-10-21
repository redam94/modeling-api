"""Concepts: the client role, the client allow-list, zones, accuracy and the tool surface (mapi_spec only).

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

# --8<-- [start:tools]
from mapi_spec.dashboard_tools import DASHBOARD_TOOL_NAMES, explain, tools_json_schema

print(DASHBOARD_TOOL_NAMES)
print(explain("return_on_media").text)
print(sorted(tools_json_schema()[0]))  # the shape of one declaration
# --8<-- [end:tools]
