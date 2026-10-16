---
description: How the multi-tenant platform limits compute, schedules interactive and batch work fairly, and prices every run.
---

# Quotas and cost <span class="mapi-badge">v0.3</span>

A public modelling platform's real cost risk is compute: an accidental 500-task sweep, a
leaked token, or someone mining on free fits. From v0.3 every run is estimated before it
starts, checked against quotas at three levels, scheduled fairly against everyone else's
work, and metered into a cost ledger when it ends. The contracts are in `mapi_spec.quotas`,
`mapi_spec.cost` and `mapi_spec.admin`.

## Quotas are vCPU-seconds

Compute is limited in **vCPU-seconds**, not runs: a two-minute fit on class S (2 vCPU) uses
240 vCPU-seconds, a five-minute fit on class M (4 vCPU) 1,200. Each limit below is optional;
an unset limit means "no limit at this level".

| Limit | Kind | Notes |
|---|---|---|
| `vcpu_seconds_per_day`, `vcpu_seconds_per_month` | Cumulative, UTC day and month | Checked against the run's estimate at submit |
| `concurrent_runs` | Live count | Runs between `queued` and a terminal state |
| `max_fanout_tasks` | Per submission | Caps one sweep, backtest or SBC (`taskCount`) |
| `max_resource_class` | Per submission | For example, the free tier runs on class S only |
| `storage_bytes` | Cumulative | Datasets, bundles and uploads |
| `egress_bytes_per_month` | Cumulative, UTC month | Downloads through signed URLs |
| `requests_per_minute` | Rate | API calls |

## Three levels

Every request is checked against up to three policies, most specific first:

1. **Token**: the token's own rate limit and vCPU-second budget (set when it was minted).
2. **Tenant**: the tenant's policy, or the defaults of its trust tier.
3. **Global**: platform-wide limits that protect the cloud project itself (Cloud Run allows
   1,000 running executions per region; the global default stops at 700).

The first limit a request would exceed decides the answer:

| Code | HTTP | When | What the client does |
|---|---|---|---|
| `RATE_LIMITED` | 429 | Too many requests per minute at any level | Wait `Retry-After` seconds and retry |
| `QUOTA_EXCEEDED` | 429 | Any other limit | Read `detail` (which level, which limit); `Retry-After` is set when the limit resets with the UTC day or month |

Cumulative limits only block requests that ask for more: a tenant over its storage quota can
still read, download and delete.

```python
--8<-- "concepts_quotas.py:decide"
```

Default tenant limits by trust tier (operators override them per tenant with
`PUT /v1/admin/quotas`):

| Tier | vCPU-s / day | vCPU-s / month | Concurrent runs | Fan-out tasks | Largest class | Requests / min |
|---|---|---|---|---|---|---|
| `public` | 3,600 | 36,000 | 2 | 16 | S | 60 |
| `verified` | 86,400 | 1,000,000 | 20 | 256 | L | 600 |
| `internal` | — | — | 100 | 1,000 | — | 6,000 |
| `self-hosted-trusted` | — | — | — | — | — | — |

**Returned draws.** A score request returns a summary by default (no draws). Draws on request
(`output.draws`) are capped per tier: at most 300 on `public`, 500 on every other tier
(`mapi_spec.quotas.MAX_SCORE_DRAWS`). More is refused with `QUOTA_EXCEEDED` at
`/output/draws`, whether the call comes through `POST /v1/models/…:score`, an endpoint, or
its `…/validate`; the score job carries the cap and the runner enforces it again.

## Interactive and batch work

Each run has a priority class:

- **`interactive`**: score, render, derive, validate and describe runs. Someone is usually
  waiting for them.
- **`batch`**: fits, ingest, evaluation, optimisation and fan-out parents.

The dispatcher leases queued runs with **weighted fair sharing**. For each candidate it
computes `(tenant's running tasks + the run's tasks) / (tenant weight × class weight)`, with
weight 4 for interactive and 1 for batch, and leases the smallest first, until the free
capacity (and the Cloud Run `jobs.run` rate, 180 calls a minute) is used up. A tenant with 40
tasks running has to wait behind an idle tenant's score, and a 200-task SBC fills in
the capacity left over instead of blocking everyone:

```python
--8<-- "concepts_quotas.py:lease"
```

When the backend itself answers 429, the dispatcher backs off exponentially (1, 2, 4 … 64
seconds, never less than a `Retry-After`) and submits exactly once when the limit clears.

## Prices and the cost ledger

Every run's cost is computed from its resource class and wall time:

- vCPU-seconds = vCPUs of the class × billed seconds × tasks;
- GiB-seconds = memory of the class × billed seconds × tasks;
- billed seconds = wall time rounded up to 100 ms.

Prices come from a versioned **price table**. The default mirrors Cloud Run's list prices,
0.000018 USD per vCPU-second and 0.000002 USD per GiB-second; a deployment sets its own.
Unit prices are integer nanos and amounts are integer **micros** (1 USD = 1,000,000 micros),
so every amount is exact and can be recomputed from its ledger row. Every amount names the
price-table version used: new prices never rewrite old costs.

```python
--8<-- "concepts_quotas.py:cost"
```

The **cost ledger** is append-only. Each finished attempt adds one `actual` entry with its
run, attempt, resource class, task count, wall time, vCPU- and GiB-seconds, price version and
amount. Operators can add `adjustment` entries (for example, a refund after an infrastructure
failure). A run's `cost` field folds its entries together:

| `Run.cost` field | Meaning |
|---|---|
| `estimated` | `true` until the first attempt has been metered |
| `estimate_micros` | The estimate made at submit |
| `amount_micros`, `currency` | The total so far, over all attempts |
| `vcpu_seconds`, `gib_seconds` | The metered usage |
| `price_version` | The price table used |
| `attempts_billed` | How many attempts have been metered |
| `usd` | Kept from v0.2: the amount in dollars when the currency is USD |

`GET /v1/usage` reports a project's ledger totals for a UTC day or month (grouped by resource
class, day, token or run) and where it stands against each quota. Tenant admins see the
whole tenant at `GET /v1/tenants/{tenant_id}/usage`. Operators see cost by tenant for the
whole platform at `GET /v1/admin/usage` (the **Cost** tab of the admin console).

Retries are capped by cost as well as by count: an infrastructure failure is retried
automatically only while what the run has cost (its metered attempts plus the attempt that
just failed) stays below `MAPI_AUTO_RETRY_COST_FACTOR` (default 2) times its estimate, at most
`MAPI_MAX_INFRA_RETRIES` times. A retry resumes from the newest checkpoints any earlier
attempt wrote, and a run that has already used more than 30 minutes is only retried with the
user's consent.

## Kill switches

Operators have two platform-wide switches (`PUT /v1/admin/kill-switches`, audited, a reason
is required):

| Switch | Effect |
|---|---|
| `untrusted_execution_enabled` | When off, no run of tenant-uploaded plugin code is accepted or leased. Built-in models keep working. |
| `global_spend_cap_micros` | Once the platform's spend in the current UTC day (or month) reaches the cap, no new compute starts except for `internal` tenants. Spend counts the cost ledger plus the estimates of work not metered yet: admitted runs at admission, started runs (one by one) at dispatch |

Both answer `QUOTA_EXCEEDED` at the global level. `GET /v1/admin/usage` returns the spend
in the cap's current window next to the cap (`spend_cap`), and the admin console shows it as a
meter that warns at 70% and 90%. In a suspected sandbox escape, turning off
untrusted execution is the first step of the incident runbook.
