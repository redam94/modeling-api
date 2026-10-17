---
description: How the multi-tenant platform limits compute, schedules interactive and batch work fairly, and prices every run.
---

# Quotas and cost <span class="mapi-badge">v0.3</span>

Compute is the largest cost risk for a public modelling platform. Examples are an accidental
500-task sweep, a leaked token, or a person who mines on free fits. From v0.3, the platform
does these steps for each run:

1. It estimates the cost of the run before the run starts.
2. It compares the run with quotas at three levels.
3. It schedules the run fairly with the work of all other users.
4. It meters the run into a cost ledger when the run ends.

The contracts are in `mapi_spec.quotas`, `mapi_spec.cost` and `mapi_spec.admin`.

## Quotas are vCPU-seconds

The platform limits compute in **vCPU-seconds**, not in runs. A two-minute fit on class S
(2 vCPU) uses 240 vCPU-seconds. A five-minute fit on class M (4 vCPU) uses 1,200
vCPU-seconds. Each limit in the table is optional. If a limit is not set, there is no limit
at this level.

| Limit | Kind | Notes |
|---|---|---|
| `vcpu_seconds_per_day`, `vcpu_seconds_per_month` | Cumulative, UTC day and month | The platform compares the limit with the estimate of the run at submit. |
| `concurrent_runs` | Live count | Runs between `queued` and a terminal state |
| `max_fanout_tasks` | Per submission | The maximum for one sweep, backtest or SBC (`taskCount`) |
| `max_resource_class` | Per submission | For example, the free tier runs on class S only. |
| `storage_bytes` | Cumulative | Dataset versions, uploads in progress and model bundles (the stored objects of all model versions) |
| `egress_bytes_per_month` | Cumulative, UTC month | Downloads. This is the size of each object that the API gives a signed link for: dataset and model artifacts, `ui/` views, and score and render outputs. It also includes public embed views. The platform charges the bytes when it mints the link. The platform never charges GDPR exports. |
| `requests_per_minute` | Rate | API calls |

## Three levels

The platform compares each request with a maximum of three policies. It starts with the
most specific policy:

1. **Token**: the rate limit and the vCPU-second budget of the token. The person who mints
   the token sets these values.
2. **Tenant**: the policy of the tenant, or the defaults of its trust tier.
3. **Global**: platform-wide limits that protect the cloud project. Cloud Run permits 1,000
   running executions for each region. The global default stops at 700.

The first limit that a request goes above decides the answer:

| Code | HTTP | When | What the client does |
|---|---|---|---|
| `RATE_LIMITED` | 429 | Too many requests per minute at one level | Wait for `Retry-After` seconds. Then send the request again. |
| `QUOTA_EXCEEDED` | 429 | All other limits | Read `detail` to find the level and the limit. If the limit resets with the UTC day or month, the response sets `Retry-After`. |

Cumulative limits stop only the requests that ask for more. A tenant that is above its
storage quota can continue to read, download and delete.

```python
--8<-- "concepts_quotas.py:decide"
```

The table shows the default tenant limits for each trust tier. Operators can change them for
one tenant with `PUT /v1/admin/quotas`.

| Tier | vCPU-s / day | vCPU-s / month | Concurrent runs | Fan-out tasks | Largest class | Requests / min |
|---|---|---|---|---|---|---|
| `public` | 3,600 | 36,000 | 2 | 16 | S | 60 |
| `verified` | 86,400 | 1,000,000 | 20 | 256 | L | 600 |
| `internal` | — | — | 100 | 1,000 | — | 6,000 |
| `self-hosted-trusted` | — | — | — | — | — | — |

**Returned draws.** By default, a score request returns a summary and no draws. You can ask
for draws with `output.draws`. Each tier has a maximum (`mapi_spec.quotas.MAX_SCORE_DRAWS`).
The maximum is 300 on `public` and 500 on all other tiers. If you ask for more, the platform
refuses the request with `QUOTA_EXCEEDED` at `/output/draws`. This occurs for
`POST /v1/models/…:score`, for an endpoint, and for its `…/validate`. The score job also
contains the maximum, and the runner applies it again.

## Interactive and batch work

Each run has a priority class:

- **`interactive`**: score, render, derive, validate and describe runs. Usually, a person
  waits for these runs.
- **`batch`**: fits, ingest, evaluation, optimisation and fan-out parents.

The dispatcher leases queued runs with **weighted fair sharing**. For each candidate run, it
calculates `(tenant's running tasks + the run's tasks) / (tenant weight × class weight)`. The
class weight is 4 for interactive and 1 for batch. The dispatcher leases the smallest value
first. It continues until it uses all the free capacity, or the Cloud Run `jobs.run` rate of
180 calls a minute. A tenant with 40 running tasks must wait for the score of an idle tenant.
A 200-task SBC uses the remaining capacity and does not stop the work of other tenants:

```python
--8<-- "concepts_quotas.py:lease"
```

If the backend answers 429, the dispatcher waits for an exponentially longer time before
each new attempt: 1, 2, 4 … 64 seconds. The time is never less than a `Retry-After`. The
dispatcher submits the run one time only when the limit clears.

## Prices and the cost ledger

The platform calculates the cost of each run from its resource class and its wall time:

- vCPU-seconds = vCPUs of the class × billed seconds × tasks
- GiB-seconds = memory of the class × billed seconds × tasks
- billed seconds = wall time, rounded up to 100 ms

The prices come from a versioned **price table**. The default uses the list prices of Cloud
Run: 0.000018 USD for each vCPU-second and 0.000002 USD for each GiB-second. A deployment can
set its own prices. Unit prices are integer nanos. Amounts are integer **micros**
(1 USD = 1,000,000 micros). Thus, each amount is exact, and you can calculate it again from
its ledger row.

Each amount names the version of the price table that the platform used. New prices never
change old costs.

```python
--8<-- "concepts_quotas.py:cost"
```

The **cost ledger** is append-only. Each finished attempt adds one `actual` entry. The entry
contains the run, the attempt, the resource class, the task count and the wall time. It also
contains the vCPU-seconds, the GiB-seconds, the price version and the amount. Operators can
add `adjustment` entries, for example a refund after an infrastructure failure. The `cost`
field of a run combines its entries:

| `Run.cost` field | Meaning |
|---|---|
| `estimated` | `true` until the platform meters the first attempt |
| `estimate_micros` | The estimate that the platform made at submit |
| `amount_micros`, `currency` | The total at this time, for all attempts |
| `vcpu_seconds`, `gib_seconds` | The metered usage |
| `price_version` | The price table that the platform used |
| `attempts_billed` | The number of metered attempts |
| `usd` | Kept from v0.2. The amount in dollars when the currency is USD. |

`GET /v1/usage` gives the ledger totals of a project for a UTC day or month. It groups the
totals by resource class, day, token or run. It also shows the usage of the project against
each quota. Tenant admins see the full tenant at `GET /v1/tenants/{tenant_id}/usage`.
Operators see the cost of each tenant for the full platform at `GET /v1/admin/usage`. This is
the **Cost** tab of the admin console.

The platform limits retries by cost and by count. After an infrastructure failure, the
platform tries the run again automatically only if these two conditions are true:

- The cost of the run stays below `MAPI_AUTO_RETRY_COST_FACTOR` (default 2) times its
  estimate. This cost includes the metered attempts and the attempt that failed.
- The number of retries is not more than `MAPI_MAX_INFRA_RETRIES`.

A retry continues from the newest checkpoints that an earlier attempt wrote. If a run used
more than 30 minutes, the platform tries it again only with the consent of the user.

## Kill switches

Operators have two platform-wide switches (`PUT /v1/admin/kill-switches`). The platform
audits each change, and the operator must give a reason:

| Switch | Effect |
|---|---|
| `untrusted_execution_enabled` | When this switch is off, the platform does not accept or lease runs of plugin code that tenants uploaded. Built-in models continue to work. |
| `global_spend_cap_micros` | When the spend of the platform in the current UTC day (or month) gets to the cap, no new compute starts. Only `internal` tenants can start compute. The spend is the cost ledger plus the estimates of work that the platform did not meter yet. Admitted runs count at admission. Started runs count one by one at dispatch. |

Both switches answer `QUOTA_EXCEEDED` at the global level. `GET /v1/admin/usage` returns the
spend in the current window of the cap, next to the cap (`spend_cap`). The admin console
shows this spend as a meter that gives a warning at 70% and at 90%. If you think that code
escaped the sandbox, turn off untrusted execution. This is the first step of the incident
runbook.
