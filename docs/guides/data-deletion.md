---
description: Deleting a whole workspace (tenant) on modeling-api and exporting its data first - the grace period, what is purged and what is kept, retention locks, progress per store, logs, and the GDPR export archive.
---

# Deleting data and exporting it <span class="mapi-badge">v0.4</span>

A tenant admin can export everything the workspace stored (GDPR access) and delete the whole
workspace (GDPR erasure). Both need a signed-in session of a **tenant admin**: API tokens never
administer a tenant. The HTTP operations are in the
[HTTP API reference](../reference/http-api.md#sign-up-verification-deletion-and-export); the
settings are in the [configuration reference](../operations/configuration.md).

## Export first

```bash
# queue the export (everything by default: metadata, datasets, models, audit, usage)
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST "$API/v1/tenants/acme/exports" \
  -H 'Content-Type: application/json' -d '{}'
# poll it; once "ready", download_url is a signed link valid for 15 minutes
curl -s -b cookies "$API/v1/tenants/acme/exports/exp_01jaf3r1s2t3v4w5x6y7z8a9b0"
```

The orchestrator builds the archive in the background (usually within a minute). It is a zip:

| Path | Content |
|---|---|
| `README.txt`, `manifest.json` | What is inside, with the size and SHA-256 of every file |
| `metadata/<table>.jsonl` | Every record of the workspace, one JSON object per line: projects, members, API tokens, datasets and versions (schemas, profiles), specs and revisions (YAML), runs, models and versions, plots, endpoints, webhooks…, up to `MAPI_EXPORT_MAX_METADATA_BYTES` (256 MiB) in total; a table cut short is listed under `skipped` with the rows written |
| `datasets/<dataset>/v<n>/...` | The stored dataset objects, byte for byte, up to `MAPI_EXPORT_MAX_INLINE_BYTES` (256 MiB) in total; the rest is listed in `manifest.json` under `skipped` with sizes and checksums |
| `models/<model>/v<n>/manifest.json`, `model_version.json` | The signed bundle manifest, lineage, diagnostics, scorecard and metrics of every model version |
| `audit/audit_events.jsonl`, `usage/cost_ledger.jsonl` | The workspace's audit log and metered usage |

Credentials are never exported: token hashes, signing keys, nonces and webhook secrets are
`[redacted]`. Records are read through the workspace's own row-level-security session, so an
archive can only contain its own workspace's rows. One export runs at a time, at most
`MAPI_EXPORTS_PER_DAY` (3) a day (`429 QUOTA_EXCEEDED` otherwise); an archive is deleted after
`MAPI_EXPORT_RETENTION_S` (7 days). A suspended workspace, including one whose deletion is
scheduled, can still export.

## Request the deletion

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST "$API/v1/tenants/acme/deletion" \
  -H 'Content-Type: application/json' -d '{"confirm": "acme", "reason": "closing the account"}'
```

`confirm` must repeat the tenant id. A deployment can also demand a recent sign-in
(`MAPI_DELETION_REAUTH_MAX_AGE_S`: older sessions get `401` and sign in again). The platform's
own tenant cannot be deleted.

The deletion is **scheduled** for the end of a grace period (`MAPI_TENANT_DELETION_GRACE_S`,
7 days). At once:

* the workspace is **suspended**: everything stays readable, nothing new can be created or
  run, and nothing spends compute;
* its unfinished runs are cancelled.

Until the grace period ends the admin can still export, and can cancel:

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X DELETE "$API/v1/tenants/acme/deletion"
```

Cancelling gives the workspace back the status it had (unless an operator changed it in the
meantime). The audit log records `tenant.deletion_request` and `tenant.deletion_cancel`.

## What happens when the grace period ends

The deletion becomes `deleting`. This cannot be undone:

1. The tenant's status becomes `deleting`: every request for it is refused, for its members
   and its tokens alike (operators can still read the deletion's progress).
2. Its API tokens are revoked, and the browser sessions of people who belong to no other
   workspace are ended. Components that hand out links (webhooks, embed links) revoke theirs.
3. Unfinished runs are cancelled again, and the purge waits for them to stop (at most
   `MAPI_DELETION_RUN_STOP_TIMEOUT_S`, one hour).

Then the purge goes store by store, and `GET /v1/tenants/{id}/deletion` shows each one's
progress:

| Store | What is purged |
|---|---|
| `queue` | Background jobs of the tenant's runs and their bootstrap objects |
| `database` | Every record of the workspace in every table, children before parents. People who belonged only to this workspace are deleted too |
| `object_store` | Every object under `t/<tenant>/` in the quarantine, runs, bundles and data buckets: uploads, run scratch, model bundles, dataset versions, export archives |
| `logs` | Not purged (see below) |
| `webhooks` | The `tenant.deletion.completed` webhook is sent to the endpoints registered before the purge |

When every store is done the deletion is `purged`, and the audit log gets the tombstone event
`tenant.purged` (by tenant id, without content).

### What is kept, and why

| Record | Kept |
|---|---|
| Audit log | 1 year after deletion, pseudonymised: actor ids and keyed digests of client addresses, no content |
| Cost ledger | 7 years for tax law: amounts and run ids, no content |
| The deletion's own record | Its ids, dates and counts per store |
| The tenant id | A tombstone row (status `deleting`, no name) so the id is never reused |
| Sign-up records | Only the keyed digests used against repeated abuse; the e-mail, name and organisation are erased |
| Platform logs | Not purged: see below |

### Retention locks

Model bundles live in a bucket with a retention policy (Terraform `bundle_retention_days`, 30
days; locked in production). An object younger than that cannot be deleted by anyone. The
purge records each such object with the time it may be deleted (`time_created` plus
`MAPI_BUNDLE_RETENTION_DAYS`), shows `object_store` as `running` with a plain explanation, and
resumes on its own when the retention expires. Everything else is gone already, and the
workspace stays inaccessible.

### Logs

Cloud Logging entries cannot be deleted one by one, so a deletion does not purge logs. The
platform keeps tenant content out of them: structured log lines carry tenant, project, run and
request ids and keyed digests of client addresses, never request bodies, data or tracebacks of
tenant code. They expire with their bucket's retention: Terraform sets the control project's
`_Default` log bucket to `log_retention_days` (30 days by default,
`infra/terraform/modules/control/logging.tf`). The run-event sink only forwards events to
Pub/Sub and stores nothing. So **30 days after a deletion, its logs are gone too**.

### When a store fails

A failing store makes the deletion `failed`. The orchestrator retries it with back-off (5
minutes doubling up to 6 hours) up to `MAPI_DELETION_MAX_ATTEMPTS` (8) times; after that an
operator looks at `last_error` in the logs and restarts it:

```bash
curl -s -b cookies -H "X-CSRF-Token: $CSRF" -X POST \
  "$API/v1/admin/tenants/acme/deletion/retry"
```

The purge is idempotent: a retry starts where the last pass stopped.

## For operators

* The scheduler runs in the orchestrator (`mapi-server worker`) every
  `MAPI_LIFECYCLE_INTERVAL_S`; purges and exports run as procrastinate jobs on the
  `lifecycle` queue, one at a time per deletion or export, and are re-queued if a worker dies.
* An export build may take `MAPI_EXPORT_BUILD_TIMEOUT_S` (one hour) and then fails. An
  export still `running` five minutes after that belongs to a process that died (an OOM
  kill, a lost orchestrator): the scheduler builds it once more, and fails it if that build
  dies too, so a workspace can always ask for a new export. The archive is built in memory,
  so size the orchestrator for `MAPI_EXPORT_MAX_INLINE_BYTES` plus
  `MAPI_EXPORT_MAX_METADATA_BYTES`.
* Database rows are deleted through the tenant's own row-level-security session wherever the
  request-path role may delete them, so a purge cannot touch another tenant's rows.
* Components that own credentials or links of a tenant register a
  `mapi_server.services.trust.TenantRevoker`; the webhooks component provides the
  `DeletionNotifier` that sends `tenant.deletion.completed`.
