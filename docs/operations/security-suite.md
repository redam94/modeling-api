---
description: The sandbox-escape probe suite, its expected-outcome matrix per isolation level, and how to run it locally, against gVisor, and in the cloud.
---

# Security suite <span class="mapi-badge">v0.3</span>

The security suite is a set of deliberately hostile probes. For each executor, it reports which
attacks were **BLOCKED** and which were **ALLOWED**, and compares the result with what that
executor's isolation level must guarantee. It runs nightly in the cloud. It has to stay green
for two weeks before public code upload (cut line B) can open.

The suite lives in `security/` at the root of the repository and is **never installed**:
`security/` is not a uv workspace member, so `uv sync --all-packages` leaves it out. Its
malicious plugin is not registered as a `mapi.models` entry point either, so the platform can
never discover it. The suite runs on the installed environment with `security/src` on
`PYTHONPATH`, which is what the Make targets do.

## Run it

```bash
make security-list                              # the probe corpus
make security-suite                             # EXECUTOR=local-process (always runnable)
make security-suite EXECUTOR=docker-gvisor      # sandbox probes inside a gVisor container
```

`make security-suite` exits non-zero when any outcome is unacceptable for the executor's
isolation level. It also writes `.local/security-<executor>.prom`, which exports
`mapi_security_probe_unexpected{executor}` for a textfile collector or a Pushgateway. The
`MapiSecurityProbeFailure` alert in `ops/alerts/` watches that series.

## The probes

**Isolation probes** test operating-system and sandbox controls. Each one tries a hostile
action, safely and with bounds, and reports ALLOWED or BLOCKED:

| Probe | Attempts |
|---|---|
| `metadata_token` | Fetch an instance token from the metadata server |
| `http_egress`, `dns_egress` | Open an outbound connection; resolve a unique subdomain (DNS exfiltration) |
| `write_outside_scratch` | Write outside the run's output area |
| `reuse_foreign_url` | Reach an object-store endpoint, the first step to reusing another run's signed URL |
| `read_proc1_environ`, `read_supervisor_mem` | Read the supervisor's environment or memory |
| `pth_injection` | Drop a `.pth` or `sitecustomize` file into site-packages |
| `fork_bomb`, `memory_bomb`, `disk_bomb` | Exceed the process, memory or scratch limits (all capped) |
| `log_flood` | Write past the per-run log cap |

**Platform probes** call the real control-plane defences in `mapi_spec` and `mapi_plots`. They
must be BLOCKED on every executor:

| Probe | Attempts |
|---|---|
| `forged_stdout_event` | A run event with the wrong HMAC |
| `tampered_manifest` | An output object the signed manifest never listed |
| `redos_schema` | A catastrophic-backtracking regex in a plot spec |
| `plot_xss_exfil` | A plot with an external image URL or a `<script>` caption |
| `reuse_foreign_manifest` | One run's signed manifest presented under another run's key |

## What each isolation level must block

| Isolation | Executors | Isolation probes | Platform probes |
|---|---|---|---|
| `PROCESS` | `local-process`, `docker` (runc) | Either outcome is accepted: these are not security boundaries | BLOCKED |
| `GVISOR` | `docker-gvisor` | BLOCKED | BLOCKED |
| `MICROVM` | `cloud` (the `cloudrun-jobs` executor) | BLOCKED | BLOCKED |

SKIPPED is always acceptable, for example when there is no procfs or no Docker daemon. ERROR
never is. This matrix is what lets an executor take `verified` and `public` tenants
([placement](authentication.md)). The 12-case executor conformance suite (`make test`, in
`packages/server/tests/test_executor_conformance.py`) is the other half of that gate.

## Where the probes run

- **local-process:** the suite process is the execution environment, so the probes run in
  process. On a developer machine most isolation probes are ALLOWED. That is expected and
  documented.
- **docker, docker-gvisor:** the isolation probes run in a locked-down container
  (`--network none`, read-only root, non-root user, no capabilities, `no-new-privileges`, and
  `--runtime runsc` for gVisor). Without a Docker daemon they report SKIPPED, and the platform
  probes still run.
- **cloud:** the probes must run *inside* a Cloud Run Jobs execution of a base image, judged
  against the `MICROVM` row. `--executor cloud` therefore refuses to run (exit status 2)
  anywhere `CLOUD_RUN_JOB` and `CLOUD_RUN_EXECUTION` are not set, because a report made on a
  laptop or a CI runner would describe the wrong sandbox. The base images do not ship the
  suite yet and Terraform defines no probe Job, so the nightly cloud run is still to be wired
  up: see [M2 verification](m2-verification.md#what-remains-for-real-gcp). The authoritative
  DNS-exfiltration check reads the query logs of a zone we control
  ([security hardening](security-hardening.md)).

`security/THREAT-MODEL.md` maps each probe to its threat and to the control expected to stop
it.
