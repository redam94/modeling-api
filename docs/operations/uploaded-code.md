---
description: Operator notes for uploaded code in modeling-api v0.19 - where uploaded code can run, the development flag and why it is refused, the kill switch, retention and purge, the rekit sweep, settings and the cloud gates before verified tenants use uploads.
---

# Uploaded code <span class="mapi-badge">v0.19</span>

From v0.19, tenant admins and confirmed uploaders can upload pure-Python wheels to a
deployment ([Upload your own code](../extend/upload.md)). This page is for the operator of the
deployment. It tells you where uploaded code runs, which settings control it, and what is not
verified yet.

## What the control plane does and does not do

The control plane stores the uploaded bytes, calculates their sha256 and signs URLs to them.
It never opens, extracts, builds, imports or unpickles a wheel. All work on the file runs in
the sandbox as runs:

| Run kind | Class | Timeout | What it does |
|---|---|---|---|
| `plugin_inspect` | S | `MAPI_PLUGIN_INSPECT_TIMEOUT_S` (60 s) | Reads the wheel as a zip archive with the standard library and writes the facts, the SBOM and the problems. Imports nothing from the wheel. |
| `plugin_describe` | M | `MAPI_PLUGIN_DESCRIBE_TIMEOUT_S` (600 s) | Mounts the wheel as an overlay, exports its catalog entries, runs the contract kits (each data check at most `MAPI_PLUGIN_DATA_CHECK_TIMEOUT_S`) and draws the recipe examples. |

Both kinds are untrusted runs: the kill switch and the placement rules below apply to them.
The control plane reads their JSON results with a size limit and does the checks again
(`check_describe`) before a version can be used. The results of `plugin_describe` go only to
the table `plugin_describes`, never to the shared `catalogs` table.

Each later run that uses an uploaded id gets **overlay pins** (`runs.overlays`: version id,
namespace, sha256) and `untrusted_code = true`. The supervisor downloads only the pinned
wheels, makes sure that the sha256 is correct before it extracts them, extracts them
read-only (files 0444, folders 0555) and puts them at the end of the worker's `sys.path`.
A run that has no pin for an uploaded id fails with `EXTENSION_UNAVAILABLE`.

## Where uploaded code can run

| Executor | Runs uploaded code | Isolation | Multi-tenant profile |
|---|---|---|---|
| `local-process` | No. Only with `MAPI_DEV_ALLOW_LOCAL_UNTRUSTED=1` (development only, see below). | None (a process of your user) | Never |
| `docker` | Yes | Process: read-only root, capabilities dropped, `no-new-privileges`, PID limit, `noexec` `/tmp` | No (needs gVisor and enforced egress and DNS) |
| `docker-gvisor` | Yes | gVisor | Yes, when the network is isolated (egress and DNS enforced) |
| `cloudrun-jobs` | Yes | microVM | Yes, when `MAPI_CLOUDRUN_EGRESS_ENFORCED` and `MAPI_CLOUDRUN_DNS_ENFORCED` are true |

The rule is `mapi_spec.tenancy.untrusted_placement_problem`. The control plane does a check
of it when it admits a run (409 `UNTRUSTED_PLACEMENT_UNAVAILABLE`) and again when the
dispatcher places the run (the run fails with the same code). The tier placement rules
(`TIER_PLACEMENT`) apply too: a verified-tier tenant needs gVisor, egress and DNS enforcement
for all its runs.

Which tenants can upload:

| Tier | Upload |
|---|---|
| `internal`, `self-hosted-trusted` | Yes |
| `verified` | Yes, after the identity verification of the tenant |
| `public` | Never in v0.19, also when `MAPI_PUBLIC_CODE_UPLOAD_ENABLED=true` |

In each allowed tenant, the uploader is a tenant admin or an editor that a tenant admin
confirmed (Settings › Workspace › Uploaded code). Approval needs a second tenant admin with a
web session. A single-tenant deployment has no web sessions, thus uploads there are for
private use only.

## The development flag

`MAPI_DEV_ALLOW_LOCAL_UNTRUSTED=1` lets the `local-process` executor run uploaded code
**without a sandbox**. Use it only on your own computer, to try the upload flow.

!!! danger "Never in a shared deployment"
    With the flag, uploaded code runs as your user, with access to your files and your
    network. The server refuses to start with the flag when one of these is true:

    - the profile is `multi-tenant` (`MAPI_PROFILE`);
    - the executor is `cloudrun-jobs`;
    - the object store is `gcs`;
    - `K_SERVICE` or `CLOUD_RUN_JOB` is set (the server runs on Cloud Run).

    The message names the reason, for example: "MAPI_DEV_ALLOW_LOCAL_UNTRUSTED is for local
    development only and runs uploaded code without a sandbox. It is refused because the
    profile is multi-tenant. Remove MAPI_DEV_ALLOW_LOCAL_UNTRUSTED."

When the server accepts the flag, it writes a warning to the log at start-up. Each untrusted
run on `local-process` gets the run event `DEV_MODE_ISOLATION`, and its audit row has
`details.dev_untrusted = true`. `GET /v1/me` reports `dev_untrusted_code: true`, and the app
shows the banner "Development only: uploaded code runs without a sandbox here." on the upload
pages and in Catalog › Yours.

## The kill switch

The platform kill switch for uploaded code also stops the upload checks. In the admin
console, open **Kill switches**:

- **Allow uploaded code to run** off (pause): the platform accepts and stores uploads, but
  places no new untrusted run. Queued runs stay queued. A version in "Checking the files" or
  "Running the tests" stays there, and the page says "Paused by the platform".
- **Cancel runs of uploaded code**: pause, and cancel each unfinished untrusted run of each
  tenant. A cancelled check moves the version to `inspect_failed` or `kit_failed` with a plain
  reason. The uploader raises the version number and pushes it again later (a version number
  is never uploaded twice).

Use the cancel action to contain a possible sandbox escape. Then withdraw the version with the
reason `security` (this also cancels its runs) and look at its usage
(`GET /v1/plugins/{plugin_id}/versions/{version_id}/usage`).

## Limits

| Setting | Default | What it limits |
|---|---|---|
| `MAPI_PLUGINS_ENABLED` | `true` | Off: the plugin routes refuse uploads and the catalog shows no uploaded entries. |
| `MAPI_PLUGIN_UPLOAD_MAX_BYTES` | 20 971 520 (20 MiB) | Size of one wheel (the POST policy and the file check) |
| `MAPI_PLUGIN_UNCOMPRESSED_MAX_BYTES` | 104 857 600 | Total size of the files after decompression |
| `MAPI_PLUGIN_MEMBER_MAX_BYTES` | 20 971 520 | Size of one file after decompression |
| `MAPI_PLUGIN_MAX_MEMBERS` | 2 000 | Files in one wheel |
| `MAPI_PLUGIN_MAX_RATIO` | 100 | Compression ratio of one file and of the wheel |
| `MAPI_PLUGIN_UPLOADS_PER_USER_PER_DAY` | 20 | Upload tickets of one user each day (429 `RATE_LIMITED`) |
| `MAPI_PLUGIN_PIPELINE_CONCURRENCY_PER_TENANT` | 2 | Check runs of one tenant at the same time (the others stay queued) |
| `MAPI_PLUGIN_TICKET_TTL_S` | 900 | Life of an upload ticket |
| `MAPI_PLUGIN_APPROVE_MAX_AUTH_AGE_S` | 900 | Maximum age of the reviewer's sign-in, from the ID token's `auth_time`. The re-sign-in asks the identity provider for `max_age=0` and `prompt=login`; an identity provider that sends no `auth_time` cannot approve |
| `MAPI_PLUGIN_APPROVER_MIN_MEMBERSHIP_AGE_DAYS` | 7 | A tenant admin whom the uploader added or promoted more recently (directly or through someone they added) cannot approve |
| `MAPI_PLUGIN_RETENTION_DAYS` | 30 | Days before the purge deletes refused, failed, rejected and withdrawn files |

The [server settings reference](../reference/generated/server-settings.md) lists them all.

## Storage

| Object | Bucket | Key | Life |
|---|---|---|---|
| The uploaded file | quarantine | `t/{tenant}/plugins/{upload_id}.whl` | 7-day bucket lifecycle |
| A file that passed the file check | `plugins` (new) | `t/{tenant}/plugins/{plugin_id}/{version_id}.whl` | Until the purge; write-once |

The browser uploads with a POST policy to the quarantine bucket (the CSP is not changed: the
quarantine origin is already in `connect-src`). After the file check, the control plane copies
the file to the `plugins` bucket and calculates the sha256 again. The orchestrator service
account can create, read and delete objects in `plugins`. The execution service account has
no role: runs get a signed GET for each pinned file, for their tenant only, for the run
timeout plus 10 minutes. The `plugins` bucket has no retention lock, because the purge must
delete files.

The local GCS shim creates the `plugins` bucket. For an older `.local` data folder, the shim
adds the missing bucket when it starts.

## Retention and purge

The housekeeping sweeps do these tasks:

- **Ticket expiry.** Upload tickets that were not completed expire after
  `MAPI_PLUGIN_TICKET_TTL_S`.
- **Purge.** After `MAPI_PLUGIN_RETENTION_DAYS`, the sweep deletes the file of each
  `inspect_failed`, `kit_failed`, `rejected` and `yanked` version, sets `purged_at` and writes
  the audit row `plugin.purge`. The row stays (state `purged`, "Removed" in the app). A
  quarantine file that the 7-day lifecycle deleted before is not an error.
- **Rekit.** See the next section.

## Rekit when the base image changes

The catalog key of the platform is the image digest (`Settings.resolve_image_digest()`, the
key of `plugin_describes.base_image_digest`). It is not a Docker image id. When it changes,
the rekit sweep queues one `plugin_describe` for each version in `kit_passed`,
`awaiting_approval` or `approved` that has no result for the new digest (in the limit of
`MAPI_PLUGIN_PIPELINE_CONCURRENCY_PER_TENANT`).

| Result | Effect |
|---|---|
| The exports are the same | The state does not change. |
| The exports changed | `approved` goes back to `awaiting_approval`. An approval never moves to different exports. |
| The tests fail | The version is withdrawn (`yank_reason = rekit_failed`). The uploader and the tenant admins get an e-mail and the webhook event `plugin.version.yanked`. |

Until the new result is available, the catalog uses the newest result of the version.

On a development stack the digest includes the source files of the checkout, thus it changes
when you edit the code, and each restart can start a rekit. Set `MAPI_IMAGE_DIGEST` to keep it
constant while you test.

## Checks you can run

```bash
make security-uploads             # the hostile-upload corpus (CI runs the same tests)
make security-uploads RUNTIME=1   # also the runtime cases on a local Docker daemon
make security-suite EXECUTOR=docker-gvisor   # the sandbox probes on gVisor
```

`make security-uploads` builds each hostile wheel in a temporary folder, never installs it,
and makes sure that each one is refused at its stage. An import sentinel fails the run if
any corpus module is imported outside the sandbox. See
[Security suite](security-suite.md) for the sandbox probes.

## Cloud gates

These items are **not verified** in v0.19. Do them before a hosted deployment lets a
verified-tier tenant upload code:

1. The probes of the security suite, packaged as an uploaded plugin, are BLOCKED on
   `docker-gvisor` and on Cloud Run Jobs, with egress and DNS enforced.
2. The `metadata_token` probe is BLOCKED on Cloud Run as an uploaded plugin, or the execution
   service account can only get and delete bootstrap objects (no list). Record the result as
   a residual risk.
3. The `plugins` bucket and its IAM work in a real project (write-once copy with
   `ifGenerationMatch=0`, generation-pinned signed GETs).
4. Real tenant admins use the reviewer panel before the first approval in production.

A local Docker run proves the upload pipeline, not the isolation: the local Docker executor
gives process isolation only, and gVisor is not available on most developer computers.

## Residual risks

- Kit results are a quality signal. Uploaded code runs in the same process as its tests.
- The base image contains a compiler. Uploaded code could compile native code at run time.
  The sandbox, the `noexec` `/tmp` and the file check (no native files in the wheel) limit
  this; they do not make it impossible.
- `RLIMIT_NPROC` is counted for each user id on a host, thus the worker does not set it on
  Docker or `local-process`. The Docker PID limit (512) applies to each container.
