# Runbook: suspected or confirmed sandbox escape

Triggered by **MapiIntegrityFailure**, **MapiSecurityProbeFailure**, a
**MapiSandboxViolationSpike**, or any external report of untrusted code reaching something it
should not. Treat as **SEV1** until disproven. This is the plan's "incident: confirmed
escape" response (§9): **kill switch → rotate keys → quarantine → notify.**

Signals and what they mean:

- `mapi_integrity_failures_total` increments — a run's outputs did not match the signed
  manifest and the GCS CRC32C/MD5/generation it claimed. Could be corruption, but assume
  tampering first. The finalizer already logged `SECURITY ALERT: integrity failure ...` with
  the `run_id` and `tenant_id`.
- `mapi_sandbox_violations_total{reason="bad_hmac"|"identity_mismatch"}` — forged events or
  an execution-label mismatch: user code may be writing stdout it should not, or an execution
  is impersonating another.
- `mapi_security_probe_unexpected > 0` — the nightly `mapi_security` suite found a probe that
  was ALLOWED on an enforcing executor (docker-gvisor / cloud) where it must be BLOCKED.

## 0. First 5 minutes — contain

1. **Flip the kill switch.** `PUT /v1/admin/kill-switches` with
   `untrusted_execution_enabled=false` and a reason. This denies all untrusted-code runs with
   `QUOTA_EXCEEDED` at the global scope. If a specific executor is implicated, also disable it
   (its `ExecutorHealth.enabled=false`) so the dispatcher stops placing runs on it.
2. **Freeze the blast radius.** Identify the tenant(s) and model version(s) from the alert's
   `run_id`/`tenant_id`. Suspend the tenant (`status=suspended` → read-only) if the escape is
   tenant-originated.
3. Declare SEV1, open the bridge, assign an incident commander and a security lead.

## 1. Rotate the credentials the escape could have reached (plan §9)

An escape may have read in-sandbox material. Rotate, in this order:

1. **The bundle-signing key.** Rotating the signer invalidates every outstanding
   object-scoped signed URL (they are checked when minted against the signed manifest). See
   [rotating keys](rotating-keys.md#bundle-signing-key). After rotation, re-mint URLs only
   after re-verifying bundles.
2. **The per-run HMAC root / bootstrap keys.** New runs already get a fresh per-run key; the
   concern is the signing/exec-token material. Rotate the GCS shim exec principal token
   (`gs://…/_shim/keys/exec.json` locally; the exec service-account key in cloud) so any
   leaked bootstrap token is dead. See [rotating keys](rotating-keys.md#hmac--exec-tokens).
3. **The internal events token** (`MAPI_INTERNAL_TOKEN`) if forged events were seen.

Rotation is only safe once every signer/verifier shares the new key — roll the API and worker
together (they both load the bundle-signing key written by `mapi-server migrate`).

## 2. Quarantine affected artifacts

- Move the implicated bundles/datasets out of the serving path. Do **not** delete — they are
  evidence. Copy to a `quarantine/` prefix with retention lock; record generations.
- Mark the affected model versions non-servable (do not mint score URLs for them).
- Snapshot the execution's logs (captured stderr → `mapi_log`), the attempt row
  (`run_attempts`: nonce, handle, violations, exec_message), and the Pub/Sub/Logging entries
  for the run before anything is swept by lifecycle rules.

## 3. Investigate

- Reproduce with the security suite against the implicated executor:
  `make security-suite EXECUTOR=<name>` (see `security/README.md`). Compare blocked/allowed
  to the expected-outcome matrix. A probe that flipped tells you which control failed
  (egress, DNS, filesystem, `/proc`, `.pth` injection, metadata server).
- Check whether egress/DNS controls held: in cloud, the authoritative DNS logs for the
  red-team zone and VPC-SC denials; locally, the docker-gvisor network namespace.
- Determine data exposure: which signed URLs were live, which objects were read (GCS access
  logs), whether cross-tenant reads were possible (RLS, `SET LOCAL app.tenant_id`).

## 4. Notify

- **Internal:** security, legal/privacy, leadership for any confirmed data exposure.
- **Tenants:** if a tenant's data was reachable, notify the affected tenants with: what
  happened, what data, what you did (kill switch, rotation, quarantine), and what they should
  do. Keep to confirmed facts.

Template:

> We detected and contained a security issue affecting <scope> on <date/time UTC>. We paused
> untrusted execution, rotated the affected credentials, and quarantined the affected
> artifacts. Our investigation found <data exposure statement>. No action is required from
> you / Please <action>. We will follow up with a postmortem by <date>.

## 5. Recover

- Only after the failed control is fixed and the security suite is green on the implicated
  executor: re-enable the executor, then clear the kill switch.
- Re-verify and re-mint URLs for quarantined-but-clean bundles; crypto-shred anything
  confirmed compromised (per-tenant CMEK where enabled).
- Keep untrusted execution paused until the suite stays green per policy (cut line B requires
  2 weeks green, §14 M5).

## 6. Postmortem

Mandatory. Include the exact control that failed, why the probe suite did not catch it
earlier (if it did not), and the detection/rotation timings.
