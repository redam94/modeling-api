"""The v0.3 Python client: project selection, tokens, usage and quota errors.

Runs offline (conformance/tests/test_docs_snippets.py): a stand-in for the API answers through
``httpx.MockTransport`` with the contract's own DTO examples, so every call below is a real
client call with real request building, parsing, retry and error handling.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
from mapi_client import Client, ClientConfig, RetryPolicy, retry_after_of
from mapi_spec.api import Me, Run, TokenCreated, UsageReport
from mapi_spec.cost import RunCost
from mapi_spec.errors import ErrorCode, Problem, QuotaExceeded
from mapi_spec.tokens import generate_token


def _example(model: Any, index: int = 0) -> dict[str, Any]:
    return copy.deepcopy(model.model_json_schema()["examples"][index])


SEEN: list[httpx.Request] = []


def _api(request: httpx.Request) -> httpx.Response:
    """A stand-in for the v0.3 API (only what this page calls)."""
    SEEN.append(request)
    path = request.url.path
    if path == "/v1/me":
        return httpx.Response(200, json=_example(Me))
    if path == "/v1/tokens" and request.method == "POST":
        return httpx.Response(201, json=_example(TokenCreated))
    if path == "/v1/usage":
        return httpx.Response(200, json=_example(UsageReport))
    if path.startswith("/v1/runs/") and request.method == "GET":
        run = _example(Run)
        run["cost"] = _example(RunCost, 1)
        return httpx.Response(200, json=run)
    if path == "/v1/runs":  # the tenant used its daily compute
        problem = Problem.from_code(
            ErrorCode.QUOTA_EXCEEDED, "tenant acme used its 86400 vCPU-seconds today"
        )
        return httpx.Response(
            429, json=problem.model_dump(mode="json"), headers={"Retry-After": "5400"}
        )
    return httpx.Response(404, json=Problem.from_code(ErrorCode.NOT_FOUND).model_dump())


config = ClientConfig(
    base_url="https://api.mapi.example", token=str(generate_token()), project="acme/growth"
)
api = Client(config=config, transport=httpx.MockTransport(_api))

# --8<-- [start:me]
me = api.me()
for tenant in me.tenants:
    for project in tenant.projects:
        print(f"{project.tenant_id}/{project.project_id}: {project.role.value}")
api.use("acme/growth")  # X-Mapi-Project on every project-scoped request from now on
# --8<-- [end:me]
assert SEEN[-1].headers.get("X-Mapi-Project") is None  # /v1/me is not project-scoped

# --8<-- [start:tokens]
created = api.tokens.create(
    "atlas-optimiser",
    ["score:geo-mmm-prod", "models:read"],
    expires_in_days=30,
    rate_limit_per_minute=120,
)
print(created.token.display, created.token.expires_at.date())
secret = created.secret  # the only copy: put it in your secret store now
# --8<-- [end:tokens]
assert SEEN[-1].headers["X-Mapi-Project"] == "acme/growth"
assert secret not in repr(created)
assert json.loads(SEEN[-1].content)["scopes"] == ["score:geo-mmm-prod", "models:read"]

# --8<-- [start:usage]
report = api.usage.get(period="month", group_by="resource_class")
print(report.totals.amount_micros / 1e6, report.totals.currency)
for quota in report.quotas:
    print(quota.limit_name.value, f"{quota.fraction_used:.0%}", quota.resets_at)
run = api.runs.get("run_01jaf3q2x8m9v7k5h4g3f2e1d0")
print(run.cost.amount_micros, run.cost.currency, run.cost.estimated)
# --8<-- [end:usage]
assert run.cost.amount_micros == 31200

# --8<-- [start:quota]
try:
    api.runs.fit("spc_01jaf3q2x8m9v7k5h4g3f2e1d0", 1)
except QuotaExceeded as exc:
    wait = retry_after_of(exc)  # seconds until the quota window resets (None: no reset)
    print(exc.code.value, exc.detail, wait)
# --8<-- [end:quota]
assert wait == 5400.0

# --8<-- [start:retry]
patient = Client(
    config=config,
    retry=RetryPolicy(rate_limit_retries=8, max_rate_limit_wait=300, max_quota_wait=0),
)
# --8<-- [end:retry]
patient.close()
api.close()
