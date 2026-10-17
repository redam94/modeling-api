---
description: Deleting a whole workspace (tenant) on modeling-api and exporting its data first - the grace period, what is purged and what is kept, retention locks, progress per store, logs, and the GDPR export archive.
---

# Deleting data and exporting it <span class="mapi-badge">v0.4</span>

A tenant admin can export all the data that the workspace stored (GDPR access). The tenant
admin can also delete the full workspace (GDPR erasure). Both actions need a signed-in
session of a **tenant admin**. API tokens never administer a tenant. The
[HTTP API reference](../reference/http-api.md#sign-up-verification-deletion-and-export) gives
the HTTP operations. The [configuration reference](../operations/configuration.md) gives the
settings.

## Export first

```bash
# queue the export (everything by default: metadata, datasets, models, audit, usage)
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST "$API/v1/tenants/acme/exports" \
  -H 'Content-Type: application/json' -d '{}'
# poll it; once "ready", download_url is a signed link valid for 15 minutes
curl -s -b cookies "$API/v1/tenants/acme/exports/exp_01jaf3r1s2t3v4w5x6y7z8a9b0"
# every export of the workspace, newest first (any admin, any browser)
curl -s -b cookies "$API/v1/tenants/acme/exports"
```

Settings > Data shows the newest export from that list. Thus a different admin (or a
different browser) sees an export that the platform still makes. That admin can also
download a finished export. The Python client has `client.tenants.exports("acme")`.

The orchestrator makes the archive in the background, usually in less than a minute. The
archive is a zip:

| Path | Content |
|---|---|
| `README.txt`, `manifest.json` | What is in the archive, with the size and SHA-256 of each file |
| `metadata/<table>.jsonl` | Each record of the workspace, with one JSON object on each line. Examples are projects, members, API tokens, datasets and versions (schemas, profiles), specs and revisions (YAML), runs, models and versions, plots, endpoints and webhooks. The maximum total is `MAPI_EXPORT_MAX_METADATA_BYTES` (256 MiB). If the export stops a table early, `skipped` lists the table with the rows that it wrote. |
| `datasets/<dataset>/v<n>/...` | The stored dataset objects, byte for byte. The maximum total is `MAPI_EXPORT_MAX_INLINE_BYTES` (256 MiB). `manifest.json` lists the remaining objects under `skipped`, with sizes and checksums. |
| `models/<model>/v<n>/manifest.json`, `model_version.json` | The signed bundle manifest, lineage, diagnostics, scorecard and metrics of each model version |
| `audit/audit_events.jsonl`, `usage/cost_ledger.jsonl` | The audit log and the metered usage of the workspace |

The platform never exports credentials. Token hashes, signing keys, nonces and webhook
secrets show as `[redacted]`. The platform reads the records through the own
row-level-security session of the workspace. Thus an archive can contain only the rows of
its own workspace.

One export runs at a time. The maximum is `MAPI_EXPORTS_PER_DAY` (3) each day. More exports
get `429 QUOTA_EXCEEDED`. The platform deletes an archive after `MAPI_EXPORT_RETENTION_S`
(7 days). A suspended workspace can still export. This includes a workspace with a scheduled
deletion.

## Request the deletion

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST "$API/v1/tenants/acme/deletion" \
  -H 'Content-Type: application/json' -d '{"confirm": "acme", "reason": "closing the account"}'
```

`confirm` must contain the tenant id again. A deployment can also make a recent sign-in necessary
(`MAPI_DELETION_REAUTH_MAX_AGE_S`). Older sessions get `401` and must sign in again. You
cannot delete the own tenant of the platform.

The platform **schedules** the deletion for the end of a grace period
(`MAPI_TENANT_DELETION_GRACE_S`, 7 days). These things occur immediately:

* The platform **suspends** the workspace. You can still read all its data. You cannot
  create or run new items, and nothing spends compute.
* The platform cancels the unfinished runs of the workspace.

Until the grace period ends, the admin can still export. The admin can also cancel the
deletion:

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X DELETE "$API/v1/tenants/acme/deletion"
```

When you cancel, the workspace gets back the status that it had before. This is not the
case if an operator changed the status after the request. The audit log records
`tenant.deletion_request` and `tenant.deletion_cancel`.

## What happens when the grace period ends

The deletion becomes `deleting`. You cannot undo this. The platform does these steps:

1. The status of the tenant becomes `deleting`. The platform refuses each request for the
   tenant, from its members and from its tokens. Operators can still read the progress of
   the deletion.
2. The platform revokes the API tokens of the tenant. It ends the browser sessions of
   persons who belong to no other workspace. Components that give links (webhooks, embed
   links) revoke their links.
3. The platform cancels the unfinished runs again. The purge waits for them to stop, for a
   maximum of `MAPI_DELETION_RUN_STOP_TIMEOUT_S` (one hour). After that time, a run that has
   not stopped can possibly continue to upload results under the prefix of the workspace.
   It can do this while its signed upload links are valid (its timeout plus 30 minutes).
   Thus the purge then waits for those links to expire before it deletes anything. The
   detail of the `queue` store shows the time until which it waits.
4. The platform cancels exports that are in the queue or that it still makes (state
   `failed`). The purge waits for a build in progress to end. The maximum wait is the
   `MAPI_EXPORT_BUILD_TIMEOUT_S` lease of the build. A cancelled build never uploads its
   archive. If an archive arrived after the purge listed the objects, the build deletes it.

Then the purge goes from store to store. `GET /v1/tenants/{id}/deletion` shows the progress
of each store:

| Store | What is purged |
|---|---|
| `queue` | Background jobs of the runs of the tenant, and their bootstrap objects |
| `database` | Each record of the workspace in each table, children before parents. The platform also deletes persons who belonged only to this workspace. |
| `object_store` | Each object under `t/<tenant>/` in the quarantine, runs, bundles and data buckets: uploads, run scratch, model bundles, dataset versions and export archives |
| `logs` | Not purged (see below) |
| `webhooks` | The platform sends the `tenant.deletion.completed` webhook to the endpoints that were registered before the purge. |

When all stores are complete, the deletion is `purged`. The audit log gets the tombstone
event `tenant.purged`. This event contains the tenant id, but no content.

### What is kept, and why

| Record | Kept |
|---|---|
| Audit log | 1 year after deletion, pseudonymised: actor ids and keyed digests of client addresses, no content |
| Cost ledger | 7 years for tax law: amounts and run ids, no content |
| The own record of the deletion | Its ids, dates and counts for each store |
| The tenant id | A tombstone row (status `deleting`, no name), so that nobody can use the id again |
| Sign-up records | Only the keyed digests that the platform uses against repeated abuse. The platform erases the e-mail, name and organisation. |
| Platform logs | Not purged: see below |

### Retention locks

Model bundles are in a bucket with a retention policy (Terraform `bundle_retention_days`, 30
days, locked in production). Nobody can delete an object that is younger than that. The
purge records each such object with the time when it can be deleted (`time_created` plus
`MAPI_BUNDLE_RETENTION_DAYS`). It shows `object_store` as `running`, with a plain
explanation. When the retention expires, the purge continues automatically. All the other
data is already gone, and nobody can get access to the workspace.

### Logs

You cannot delete Cloud Logging entries one at a time. Thus a deletion does not purge logs.
The platform keeps tenant content out of the logs. Structured log lines contain tenant,
project, run and request ids, and keyed digests of client addresses. They never contain
request bodies, data or tracebacks of tenant code.

The logs expire with the retention of their bucket. Terraform sets the `_Default` log bucket
of the control project to `log_retention_days` (30 days by default,
`infra/terraform/modules/control/logging.tf`). The run-event sink only sends events to
Pub/Sub and stores nothing. Thus **30 days after a deletion, its logs are also gone**.

### When a store fails

If a store fails, the deletion becomes `failed`. The orchestrator tries it again with
back-off. The back-off starts at 5 minutes and doubles each time, up to 6 hours. The maximum
is `MAPI_DELETION_MAX_ATTEMPTS` (8) attempts. After that, an operator examines `last_error`
in the logs and starts the deletion again:

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST \
  "$API/v1/admin/tenants/acme/deletion/retry"
```

The purge is idempotent. A new attempt starts where the last pass stopped.

## For operators

* The scheduler runs in the orchestrator (`mapi-server worker`) each
  `MAPI_LIFECYCLE_INTERVAL_S`. Purges and exports run as procrastinate jobs on the
  `lifecycle` queue, one at a time for each deletion or export. If a worker stops
  unexpectedly, the platform puts the job in the queue again.
* An export build can take `MAPI_EXPORT_BUILD_TIMEOUT_S` (one hour). After that, it fails.
  Possibly, an export is still `running` five minutes after that time. Then it belongs to a
  process that stopped unexpectedly (an OOM kill, a lost orchestrator). The scheduler makes
  the export one more time. If that build also stops, the scheduler fails the export. Thus a
  workspace can always ask for a new export.
* The orchestrator makes the archive in memory. Give the orchestrator memory for
  `MAPI_EXPORT_MAX_INLINE_BYTES` plus `MAPI_EXPORT_MAX_METADATA_BYTES`.
* Where the request-path role can delete database rows, the platform deletes them through
  the own row-level-security session of the tenant. Thus a purge cannot touch the rows of a
  different tenant.
* Components that own credentials or links of a tenant register a
  `mapi_server.services.trust.TenantRevoker`. The webhooks component gives the
  `DeletionNotifier` that sends `tenant.deletion.completed`.
